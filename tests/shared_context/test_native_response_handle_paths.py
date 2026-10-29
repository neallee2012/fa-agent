"""Pins the Responses handler's less travelled paths through the official host: each one keeps the original RID."""

import asyncio
import copy
import time
from types import SimpleNamespace

import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.ai.agentserver.responses.hosting import _orchestrator
from azure.core.exceptions import ServiceResponseError
from starlette.testclient import TestClient

from shared_context import native_turns
from shared_context.behavior_hosted import create_host
from shared_context.native_checkpoint import open_durable_store
from shared_context.native_responses import native_responses_handler_factory
from shared_context.native_turns import TURNS_STORE, NativeTurnRuntime, _Versioned, _changed
from test_native_responses import ResponsesHarness, build_responses_host, response_submit, responses, stored_rid
from test_native_turns import IDENTITY, OfflineSdk, local_env


async def rewrite_rid(rid, receipt=None, value=None):
    """Another writer changes the saved RID record (its own CAS on the current ETag)."""
    async with await open_durable_store(TURNS_STORE, create=False) as store:
        item = await store.get_item("rid:" + rid)
        changed = copy.deepcopy(item.value)
        changed.update(value or {})
        changed["receipt"].update(receipt or {})
        await store.set_item("rid:" + rid, changed, if_match=item.etag)


def waiting_behind_active(responses, monkeypatch, rid):
    monkeypatch.setattr(native_turns, "SLOT_WAIT_POLL_SECONDS", 0.05)
    responses.sdk.block = True
    assert responses.invoke(response_submit("active")).status_code == 202
    assert responses.respond(responses.call(responses.runtime.read, "active")).status_code == 200
    responses.wait("active", "started")
    assert responses.invoke(response_submit(rid)).status_code == 202
    created = responses.respond(responses.call(responses.runtime.read, rid))
    assert created.status_code == 200
    responses.queued(rid)
    return created


def finish_active(responses):
    responses.sdk.block = False
    responses.wait("active", "completed")
    time.sleep(0.3)


def test_a_waiting_rid_another_session_adopted_is_left_untouched_to_it(responses, monkeypatch):
    created = waiting_behind_active(responses, monkeypatch, "moved")
    responses.call(rewrite_rid, "moved", {"hosted_session_id": "OFFLINE-another-session"})
    result = responses.response_result(created.json()["id"])
    assert (result["status"], result["error"]["code"]) == ("failed", "NATIVE_SESSION_CHANGED")
    assert result["error"]["message"] == "The original RID now belongs to another Hosted session."
    saved = responses.call(stored_rid, "moved").value
    assert saved["receipt"]["status"] == "accepted" and saved["receipt"].get("response_id") is None
    assert saved["receipt"]["hosted_session_id"] == "OFFLINE-another-session"
    assert "moved" not in responses.runtime.responses_queued
    finish_active(responses)
    assert [call[0] for call in responses.sdk.calls] == ["active"]


def test_a_waiting_rid_the_slot_observer_fenced_is_not_started_and_stays_accepted(responses, monkeypatch):
    """One observer read showing another SID fences the wait (a race); the handler's own reread finds it unchanged."""
    created = waiting_behind_active(responses, monkeypatch, "fenced")
    read, foreign = responses.runtime._rid, []

    async def one_foreign_read(rid):
        current = await read(rid)
        if rid == "fenced" and not foreign:
            foreign.append(rid)
            moved = _changed(current.value.receipt, hosted_session_id="OFFLINE-another-session")
            return _Versioned(_changed(current.value, receipt=moved), current.etag)
        return current

    monkeypatch.setattr(responses.runtime, "_rid", one_foreign_read)
    result = responses.response_result(created.json()["id"])
    assert foreign == ["fenced"]
    assert (result["status"], result["error"]["code"]) == ("failed", "NATIVE_TURN_NOT_TAKEN")
    assert result["error"]["message"] == "The original native receipt did not complete; retain its RID."
    saved = responses.call(stored_rid, "fenced").value
    assert saved["receipt"]["status"] == "accepted" and saved["run_token"] is None
    assert saved["receipt"].get("failure_code") is None and saved["receipt"].get("response_id") is None
    assert saved["receipt"]["hosted_session_id"] == IDENTITY.session_id
    assert "fenced" not in responses.runtime.responses_queued
    finish_active(responses)
    assert [call[0] for call in responses.sdk.calls] == ["active"]


def test_a_cancel_already_saved_on_an_accepted_rid_closes_it_at_admission(responses):
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    responses.call(rewrite_rid, "rid-1", {"cancel_requested": True})
    created = responses.respond(accepted)
    result = responses.response_result(created.json()["id"])
    assert (result["status"], result["error"]["code"]) == ("failed", "NATIVE_TURN_CANCELLED")
    closed = responses.call(responses.runtime.read, "rid-1")
    assert (closed.status, closed.failure_code, closed.response_id) == ("unknown", "NATIVE_TURN_CANCELLED", None)
    assert not responses.sdk.calls and "rid-1" not in responses.runtime.responses_queued


def test_a_saved_answer_longer_than_one_result_part_is_published_whole(responses):
    responses.sdk.text = "".join(f"{number:07d}\n" for number in range(30000))  # 240000 bytes: two parts
    assert len(responses.sdk.text.encode("utf-8")) > native_turns.TEXT_PART_BYTES
    assert responses.invoke(response_submit()).status_code == 202
    created = responses.respond(responses.call(responses.runtime.read, "rid-1"))
    result = responses.response_result(created.json()["id"])
    assert result["status"] == "completed"
    assert [item["phase"] for item in result["output"]] == ["final_answer"]
    assert result["output"][0]["content"][0]["text"] == responses.sdk.text
    assert len(responses.sdk.calls) == 1


@pytest.mark.parametrize("winner, expected", [
    ("another-session", ("NATIVE_TURN_NOT_TAKEN", "started", "OFFLINE-other-token")),
    ("same-session", ("NATIVE_TURN_UNKNOWN", "unknown", "OFFLINE-other-token")),
], ids=["another-session", "same-session"])
def test_a_start_write_another_writer_won_is_not_run_here(responses, monkeypatch, winner, expected):
    """An unconfirmed start CAS whose record now differs from both ours and the original: never this response's SDK."""
    write = FoundryStateStore.set_item
    raced = []

    async def other_writer_wins(store, key, value, **options):
        if key == "rid:rid-1" and value["receipt"]["status"] == "started" and not raced:
            raced.append(value)
            other = copy.deepcopy(value)
            other["run_token"] = "OFFLINE-other-token"
            if winner == "another-session":
                other["receipt"]["hosted_session_id"] = "OFFLINE-another-session"
            await write(store, key, other, **options)
            raise ServiceResponseError("OFFLINE start acknowledgment is unconfirmed")
        return await write(store, key, value, **options)

    monkeypatch.setattr(FoundryStateStore, "set_item", other_writer_wins)
    assert responses.invoke(response_submit()).status_code == 202
    created = responses.respond(responses.call(responses.runtime.read, "rid-1"))
    result = responses.response_result(created.json()["id"])
    saved = responses.call(stored_rid, "rid-1").value
    assert raced and not responses.sdk.calls
    assert result["status"] == "failed"
    assert (result["error"]["code"], saved["receipt"]["status"], saved["run_token"]) == expected


def client_cancel(context, signal):
    """A client cancel as the handler sees it: the client named as the cause, then the signal."""
    context.client_cancelled = True
    signal.set()


def signal_during_admission(responses, monkeypatch, rid, set_signal):
    """The platform's signal arrives while the handler checks the original session, before it reserves a queue place.

    Returns the response ids whose terminal the host has resolved: a GET before that, while the signal is set, gets the
    host's own "cancelled" (_refresh_background_status), however fast the handler is."""
    check = responses.runtime._require_original_session
    resolve, resolved = _orchestrator._bg_resolve_terminal_status, set()

    def resolved_here(record, *args, **options):
        resolve(record, *args, **options)
        resolved.add(options["response_id"])

    monkeypatch.setattr(_orchestrator, "_bg_resolve_terminal_status", resolved_here)

    async def then_signal(**options):
        result = await check(**options)
        context, signal = responses.contexts[rid][-1]
        set_signal(context, signal)
        return result

    monkeypatch.setattr(responses.runtime, "_require_original_session", then_signal)
    return resolved


def resolved_result(responses, resolved, created):
    """GET the signalled response only once the host has resolved its terminal from the handler's own events."""
    response_id = created.json()["id"]
    deadline = time.monotonic() + 8
    while response_id not in resolved and time.monotonic() < deadline:
        time.sleep(0.01)
    assert response_id in resolved, "the host did not resolve the signalled response's terminal"
    return responses.response_result(response_id)


def test_a_platform_signal_before_the_queue_place_keeps_the_rid_accepted_and_resendable(responses, monkeypatch):
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    resolved = signal_during_admission(responses, monkeypatch, "rid-1", lambda context, signal: signal.set())
    created = responses.respond(accepted)
    result = resolved_result(responses, resolved, created)
    assert (result["status"], result["error"]["code"]) == ("failed", "NATIVE_TURN_NOT_TAKEN")
    kept = responses.call(responses.runtime.read, "rid-1")
    assert (kept.status, kept.failure_code, kept.response_id) == ("accepted", None, None)
    assert not responses.sdk.calls and "rid-1" not in responses.runtime.responses_queued


def test_a_client_cancel_before_the_queue_place_closes_the_accepted_rid(responses, monkeypatch):
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    resolved = signal_during_admission(responses, monkeypatch, "rid-1", client_cancel)
    created = responses.respond(accepted)
    result = resolved_result(responses, resolved, created)
    assert (result["status"], result["error"]["code"]) == ("failed", "NATIVE_TURN_CANCELLED")
    closed = responses.call(responses.runtime.read, "rid-1")
    assert (closed.status, closed.failure_code, closed.response_id) == ("unknown", "NATIVE_TURN_CANCELLED", None)
    assert not responses.sdk.calls and "rid-1" not in responses.runtime.responses_queued


def test_a_shutdown_during_admission_defers_the_accepted_rid_untouched(responses, monkeypatch, caplog):
    """Only ``context.shutdown`` is set (not the host's own shutdown), so the host reports the deferral it was asked for
    as a misuse: that report is the evidence the handler deferred here, before any queue place or SDK."""
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    before = responses.call(stored_rid, "rid-1")
    # A full queue would refuse (and rewrite) a human RID that went on past the deferral.
    monkeypatch.setattr(responses.runtime, "response_queue_full", lambda: True)
    signal_during_admission(responses, monkeypatch, "rid-1", lambda context, signal: context.shutdown.set())
    created = responses.respond(accepted)
    assert created.status_code == 200
    deadline = time.monotonic() + 8
    while "exit_for_recovery() may only be called" not in caplog.text and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "exit_for_recovery() may only be called" in caplog.text
    after = responses.call(stored_rid, "rid-1")
    assert after.etag == before.etag and after.value == before.value
    assert not responses.sdk.calls and "rid-1" not in responses.runtime.responses_queued


@pytest.mark.parametrize("set_signal, expected", [
    (client_cancel, ("NATIVE_TURN_CANCELLED", "unknown", "NATIVE_TURN_CANCELLED")),
    (lambda context, signal: signal.set(), ("NATIVE_TURN_NOT_TAKEN", "accepted", None)),
], ids=["client-cancel", "platform-signal"])
def test_an_admission_signal_is_answered_before_a_full_queue_refuses_the_rid(responses, monkeypatch, set_signal,
                                                                             expected):
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    monkeypatch.setattr(responses.runtime, "response_queue_full", lambda: True)
    resolved = signal_during_admission(responses, monkeypatch, "rid-1", set_signal)
    created = responses.respond(accepted)
    result = resolved_result(responses, resolved, created)
    kept = responses.call(responses.runtime.read, "rid-1")
    assert (result["error"]["code"], kept.status, kept.failure_code) == expected
    assert not responses.sdk.calls


def started_elsewhere(responses, session_id):
    """An accepted RID rewritten as started by an earlier run of ``session_id`` that left no handler behind."""
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    responses.call(rewrite_rid, "rid-1", {
        "status": "started", "response_id": "OFFLINE-earlier-response", "hosted_session_id": session_id,
    }, {"run_token": "OFFLINE-earlier-run"})
    return accepted


def test_a_started_rid_another_session_holds_is_refused_by_the_admission_check(responses):
    accepted = started_elsewhere(responses, "OFFLINE-another-session")
    created = responses.respond(accepted)
    result = responses.response_result(created.json()["id"])
    assert result["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert result["error"]["message"].startswith("read-only: the official chain belongs to the original Hosted session")
    assert not responses.sdk.calls


def test_a_platform_signal_during_the_cli_exit_wait_keeps_the_rid_unstarted(responses):
    polled = []

    def unexited():
        polled.append(True)
        return True

    responses.sdk.runtime_unexited = unexited
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    created = responses.respond(accepted)
    deadline = time.monotonic() + 8
    while not polled and time.monotonic() < deadline:
        time.sleep(0.01)
    assert polled and responses.runtime.execution_owner == "rid-1"  # the slot is taken; the CLI exit wait runs
    responses.call(responses.contexts["rid-1"][0][1].set)
    result = responses.response_result(created.json()["id"])
    assert (result["status"], result.get("error")) == ("cancelled", None)  # the host's own answer to its signal
    while "rid-1" in responses.runtime.responses_queued and time.monotonic() < deadline:
        time.sleep(0.01)  # the host answers at once; the handler itself ends when it leaves the queue place
    assert "rid-1" not in responses.runtime.responses_queued
    kept = responses.call(responses.runtime.read, "rid-1")
    assert (kept.status, kept.failure_code, kept.response_id) == ("accepted", None, None)
    assert not responses.sdk.calls


def test_the_queue_place_is_released_before_the_terminal_notice(responses, monkeypatch):
    notify, queued = responses.runtime._notify_terminal, []

    async def observed(receipt, **options):
        queued.append(receipt.request_id in responses.runtime.responses_queued)
        return await notify(receipt, **options)

    monkeypatch.setattr(responses.runtime, "_notify_terminal", observed)
    assert responses.invoke(response_submit()).status_code == 202
    created = responses.respond(responses.call(responses.runtime.read, "rid-1"))
    assert responses.response_result(created.json()["id"])["status"] == "completed"
    assert queued == [False]


def test_a_tool_start_and_its_live_text_share_one_display_delta(responses):
    execute = responses.runtime.executor

    async def progress(receipt, scope, previous, control):
        control.on_event({"type": "tool.execution_start", "live_text": "Reading",
                          "public_progress": {"kind": "tool_started", "tool_name": "read_context"}})
        await asyncio.sleep(0)
        return await execute(receipt, scope, previous, control)

    responses.runtime.executor = progress
    assert responses.invoke(response_submit()).status_code == 202
    created = responses.respond(responses.call(responses.runtime.read, "rid-1"))
    result = responses.response_result(created.json()["id"])
    assert [item["phase"] for item in result["output"]] == ["commentary", "final_answer"]
    assert result["output"][0]["content"][0]["text"] == "\n正在使用工具：read_context\nReading"


def test_a_host_shutdown_stops_the_running_turn_as_shutdown_not_as_a_fence(local_env, monkeypatch, tmp_path):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    root = tmp_path / "journal"
    root.mkdir()
    sdk = OfflineSdk()
    sdk.block = True
    runtime, contexts = NativeTurnRuntime(IDENTITY, sdk), {}
    with TestClient(build_responses_host(runtime, root, contexts)) as client:
        first = ResponsesHarness(runtime, sdk, client, contexts)
        assert first.invoke(response_submit("started")).status_code == 202
        first.respond(first.call(runtime.read, "started"))
        first.wait("started", "started")
    control = sdk.controls["started"]
    assert (control.shutdown, control.fenced, control.cancelled) == (True, False, False)


def test_closing_the_stream_early_stops_the_sdk_turn_and_frees_the_slot_at_once(local_env, monkeypatch, tmp_path):
    """The host may close the handler's generator mid-stream: the SDK task and the slot end inside that close."""
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    root = tmp_path / "journal"
    root.mkdir()
    sdk = OfflineSdk()
    sdk.block = True
    runtime, closed = NativeTurnRuntime(IDENTITY, sdk), []

    async def previewing(receipt, scope, previous, control):
        control.on_event({"type": "assistant.message_delta", "live_text": "Partial"})
        return await sdk(receipt, scope, previous, control)

    runtime.executor = previewing

    def factory(hosted, native):
        handler = native_responses_handler_factory(hosted, native)

        async def closing_early(request, context, cancellation_signal):
            events = handler(request, context, cancellation_signal)
            async for event in events:
                yield event
                if event["type"] == "response.output_text.delta":
                    break
            await events.aclose()
            closed.append((runtime.execution_lock.locked(), sdk.active))

        return closing_early

    hosted = SimpleNamespace(identity=IDENTITY, supervisor=SimpleNamespace(journal=SimpleNamespace(root=root)))
    with TestClient(create_host(hosted, native_runtime=runtime, responses_handler_factory=factory)) as client:
        harness = ResponsesHarness(runtime, sdk, client, {})
        assert harness.invoke(response_submit()).status_code == 202
        harness.respond(harness.call(runtime.read, "rid-1"))
        deadline = time.monotonic() + 8
        while not closed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert closed == [(False, 0)]
        assert [call[0] for call in sdk.calls] == ["rid-1"] and "rid-1" not in runtime.responses_queued


def test_a_platform_signal_on_a_started_rid_names_its_reentry_unknown_not_the_checkpoint_gap(responses, monkeypatch):
    """The saved turn's terminal CAS was lost and its checkpoint is gone; a signalled replay re-enters it as unknown."""
    write, lost = FoundryStateStore.set_item, len(native_turns.SAVE_RETRY_SECONDS) + 2
    attempts = []
    monkeypatch.setattr(native_turns, "SAVE_RETRY_SECONDS", (0.01,) * len(native_turns.SAVE_RETRY_SECONDS))

    async def completed_unconfirmed(store, key, value, **options):
        if key == "rid:rid-1" and value["receipt"]["status"] == "completed" and len(attempts) < lost:
            attempts.append(value)
            raise ServiceResponseError("OFFLINE terminal CAS was not applied")
        return await write(store, key, value, **options)

    monkeypatch.setattr(FoundryStateStore, "set_item", completed_unconfirmed)
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    responses.response_result(responses.respond(accepted).json()["id"])
    assert responses.call(responses.runtime.read, "rid-1").status == "started"
    read = FoundryStateStore.get_item

    async def checkpoint_missing(store, key):
        return None if key.startswith("ckpt:") else await read(store, key)

    monkeypatch.setattr(FoundryStateStore, "get_item", checkpoint_missing)
    resolved = signal_during_admission(responses, monkeypatch, "rid-1", lambda context, signal: signal.set())
    result = resolved_result(responses, resolved, responses.respond(accepted))
    kept = responses.call(responses.runtime.read, "rid-1")
    assert (result["error"]["code"], kept.status, kept.failure_code) == (
        "NATIVE_TURN_UNKNOWN", "unknown", "NATIVE_TURN_UNKNOWN")
    assert len(responses.sdk.calls) == 1
