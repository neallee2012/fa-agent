"""Official 2.2 Responses/Invocations host and State store; only SDK/model I/O is synthetic."""

from dataclasses import dataclass, field
import json
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NATIVE_REQUEST, NativeCancel, NativeResult
from shared_context.native_responses import native_responses_handler_factory
from shared_context.native_checkpoint import open_durable_store
from shared_context.native_turns import TURNS_STORE, NativeTurnRuntime
from test_native_turn_wire import agent_receive, submit
from test_native_turns import Harness, IDENTITY, OfflineSdk, local_env


def response_submit(rid="rid-1", cid=None, text="Original user text"):
    values = submit(rid, cid, text).model_dump(mode="json")
    values["state_profile"] = "foundry-responses-v1"
    if cid is None:
        values["new_conversation_id"] = "responses-" + rid
    return NATIVE_REQUEST.validate_json(json.dumps(values))


@dataclass
class ResponsesHarness(Harness):
    contexts: dict = field(default_factory=dict)

    def respond(self, receipt, *, input_text=None, stream=False):
        return self.client.post("/responses", json={
            "input": receipt.command.content if input_text is None else input_text,
            "background": True, "store": True, "stream": stream,
            "agent_session_id": IDENTITY.session_id,
        }, headers={
            "x-agent-session-id": IDENTITY.session_id,
            "x-agent-foundry-call-id": "OFFLINE-responses-call",
            "x-client-fa-rid": receipt.request_id,
            "x-client-fa-cid": receipt.conversation_id,
            "x-client-fa-submit-digest": receipt.submit_digest,
        })

    def response_result(self, response_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            response = self.client.get("/responses/" + response_id, headers={
                "x-agent-session-id": IDENTITY.session_id,
            })
            assert response.status_code == 200, response.text
            if response.json()["status"] in {"completed", "failed", "cancelled"}:
                return response.json()
            time.sleep(0.01)
        pytest.fail("The original official response did not reach a terminal state")

    def queued(self, rid):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            receipt = self.call(self.runtime.read, rid)
            if receipt.dispatch is not None and receipt.dispatch.state == "already_queued":
                return receipt
            time.sleep(0.01)
        pytest.fail("The original RID did not enter the Agent queue")


def build_responses_host(runtime, journal_root, contexts):
    def factory(hosted, native):
        handler = native_responses_handler_factory(hosted, native)

        async def observed(request, context, cancellation_signal):
            contexts.setdefault(context.client_headers.get("x-client-fa-rid"), []).append(
                (context, cancellation_signal))
            async for event in handler(request, context, cancellation_signal):
                yield event

        return observed

    hosted = SimpleNamespace(
        identity=IDENTITY, supervisor=SimpleNamespace(journal=SimpleNamespace(root=journal_root)))
    return create_host(hosted, native_runtime=runtime, responses_handler_factory=factory)


@pytest.fixture
def responses(local_env, monkeypatch, tmp_path):
    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    contexts = {}
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    journal_root = tmp_path / "journal"
    journal_root.mkdir()
    app = build_responses_host(runtime, journal_root, contexts)

    with TestClient(app) as client:
        yield ResponsesHarness(runtime, sdk, client, contexts)
        sdk.block = False


def test_control_accepts_without_chain_then_official_responses_publishes_original_result(responses):
    request = response_submit()
    result = responses.invoke(request)
    assert result.status_code == 202, result.text
    accepted = responses.call(responses.runtime.read, "rid-1")
    assert accepted.status == "accepted"
    assert accepted.command == request.command and accepted.capture == request.capture
    assert accepted.conversation_id == request.new_conversation_id
    assert accepted.dispatch.state == "needs_response"
    assert accepted.dispatch.inflight_request_id == "rid-1"
    assert accepted.response_id is None
    assert not responses.sdk.calls

    async def admission_started():
        async with await open_durable_store(TURNS_STORE, create=False) as store:
            return (await store.get_item("agent")).value["admission_started"]

    assert responses.call(admission_started) is True
    created = responses.respond(accepted)
    assert created.status_code == 200, created.text
    completed = responses.wait("rid-1", "completed")
    assert completed.response_id == created.json()["id"]
    assert completed.dispatch is None and completed.result is not None
    page = responses.invoke(NativeResult(conversation_id=completed.conversation_id, request_id="rid-1"))
    assert page.status_code == 200
    assert page.json()["text"] == "Complete public answer"
    assert responses.sdk.calls == [
        ("rid-1", completed.conversation_id, None, "OFFLINE-responses-call"),
    ]


def test_nine_waiting_excludes_active_and_full_agent_messages_are_not_discarded(responses):
    responses.sdk.block = True
    assert responses.invoke(response_submit("active")).status_code == 202
    active = responses.call(responses.runtime.read, "active")
    assert responses.respond(active).status_code == 200
    responses.wait("active", "started")
    for number in range(9):
        rid = f"waiting-{number}"
        assert responses.invoke(response_submit(rid)).status_code == 202
        original = responses.call(responses.runtime.read, rid)
        assert responses.respond(original).status_code == 200
        assert responses.queued(rid).status == "accepted"
    assert len(responses.sdk.calls) == 1

    assert responses.invoke(response_submit("human-full")).status_code == 202
    human = responses.call(responses.runtime.read, "human-full")
    assert responses.respond(human).status_code == 200
    refused = responses.wait("human-full", "busy_refused")
    assert refused.failure_code == "NATIVE_TURN_BUSY" and refused.response_id is None

    values = agent_receive("agent-full").model_dump(mode="json")
    values.update(state_profile="foundry-responses-v1", new_conversation_id="responses-agent-full")
    request = NATIVE_REQUEST.validate_json(json.dumps(values))
    assert responses.invoke(request).status_code == 202
    agent = responses.call(responses.runtime.read, "agent-full")
    created = responses.respond(agent)
    assert created.status_code == 200
    assert responses.response_result(created.json()["id"])["error"]["code"] == "NATIVE_TURN_NOT_TAKEN"
    retained = responses.call(responses.runtime.read, "agent-full")
    assert retained.status == "accepted" and retained.response_id is None
    assert retained.dispatch.state == "not_taken"
    assert retained.command == request.command and retained.source == request.source
    assert len(responses.sdk.calls) == 1


@pytest.mark.parametrize("send_second_step", [False, True], ids=["before-dispatch", "waiting-for-lock"])
def test_legacy_active_and_cancelled_response_share_one_interruptible_lock(responses, send_second_step):
    responses.sdk.block = True
    assert responses.invoke(submit("legacy-active")).status_code == 202
    responses.wait("legacy-active", "started")
    assert responses.invoke(response_submit("cancel-me")).status_code == 202
    waiting = responses.call(responses.runtime.read, "cancel-me")
    if send_second_step:
        assert responses.respond(waiting).status_code == 200
        responses.queued("cancel-me")
    assert [call[0] for call in responses.sdk.calls] == ["legacy-active"]
    cancelled = responses.invoke(NativeCancel(
        request_id="cancel-me", conversation_id=waiting.conversation_id))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "unknown"
    assert cancelled.json()["failure_code"] == "NATIVE_TURN_CANCELLED"
    assert "response_id" not in cancelled.json()
    assert not responses.sdk.controls["legacy-active"].stop_requested()
    assert responses.invoke(response_submit("next-response")).status_code == 202
    next_turn = responses.call(responses.runtime.read, "next-response")
    assert responses.respond(next_turn).status_code == 200
    responses.queued("next-response")
    responses.sdk.block = False
    responses.wait("legacy-active", "completed")
    responses.wait("next-response", "completed")
    assert [call[0] for call in responses.sdk.calls] == ["legacy-active", "next-response"]
    assert responses.sdk.max_active == 1


@pytest.mark.parametrize("kind", ["human", "agent_request", "agent_notice"])
def test_cid_slot_is_claimed_before_transport_and_unowned_agent_work_can_follow(responses, kind):
    assert responses.invoke(response_submit("slot-owner")).status_code == 202
    owner = responses.call(responses.runtime.read, "slot-owner")
    if kind == "human":
        request = response_submit("slot-contender", owner.conversation_id)
    else:
        values = agent_receive("slot-contender", owner.conversation_id).model_dump(mode="json")
        values["state_profile"] = "foundry-responses-v1"
        if kind == "agent_notice":
            values["source"].update(
                kind="agent_notice", message_kind="blocked", in_reply_to="original-request",
                outcome={"status": "unknown", "receipt_digest": "a" * 64, "failure_code": "NATIVE_TURN_UNKNOWN"},
            )
        request = NATIVE_REQUEST.validate_json(json.dumps(values))
    response = responses.invoke(request)
    assert response.status_code == 202, response.text
    contender = responses.call(responses.runtime.read, "slot-contender")
    assert not responses.sdk.calls
    if kind == "human":
        assert contender.status == "busy_refused" and contender.failure_code == "NATIVE_TURN_BUSY"
        return
    assert contender.status == "accepted" and contender.dispatch.state == "not_taken"
    assert contender.dispatch.inflight_request_id == "slot-owner"
    unowned = responses.respond(contender)
    assert responses.response_result(unowned.json()["id"])["error"]["code"] == "NATIVE_TURN_NOT_TAKEN"
    assert not responses.sdk.calls

    assert responses.respond(owner).status_code == 200
    completed = responses.wait("slot-owner", "completed")
    assert responses.invoke(request).status_code == 202
    following = responses.call(responses.runtime.read, "slot-contender")
    assert following.dispatch.state == "needs_response"
    assert following.dispatch.previous_response_id == completed.response_id
    assert responses.respond(following).status_code == 200
    responses.wait("slot-contender", "completed")
    assert [call[0] for call in responses.sdk.calls] == ["slot-owner", "slot-contender"]
    assert responses.sdk.calls[1][2] is not None


async def stored_rid(rid):
    async with await open_durable_store(TURNS_STORE, create=False) as store:
        return await store.get_item("rid:" + rid)


def test_duplicate_queued_response_keeps_the_original_receipt_etag_and_token(responses):
    responses.sdk.block = True
    assert responses.invoke(response_submit("active")).status_code == 202
    assert responses.respond(responses.call(responses.runtime.read, "active")).status_code == 200
    responses.wait("active", "started")
    assert responses.invoke(response_submit("queued")).status_code == 202
    accepted = responses.call(responses.runtime.read, "queued")
    first = responses.respond(accepted)
    queued = responses.queued("queued")
    before = responses.call(stored_rid, "queued")
    repeated = responses.respond(accepted)
    assert responses.response_result(repeated.json()["id"])["error"]["code"] == "NATIVE_TURN_QUEUED"
    after = responses.call(stored_rid, "queued")
    assert after.etag == before.etag and after.value == before.value
    assert after.value["run_token"] is None
    assert responses.call(responses.runtime.read, "queued") == queued
    responses.sdk.block = False
    completed = responses.wait("queued", "completed")
    assert completed.response_id == first.json()["id"]
    assert [call[0] for call in responses.sdk.calls] == ["active", "queued"]


def test_admission_reuses_what_it_just_read_or_wrote_under_the_admission_lock(responses, monkeypatch):
    """v17: one follow-up admission took about 1.1 s of sequential State store round trips (about 60 ms each)."""
    from collections import Counter
    from azure.ai.agentserver.core.storage import FoundryStateStore

    reads = Counter()
    original = FoundryStateStore.get_item

    async def counted(self, key, *args, **kwargs):
        reads[key if key.startswith("rid:") else key.split(":")[0]] += 1
        return await original(self, key, *args, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "get_item", counted)
    assert responses.invoke(response_submit()).status_code == 202
    # Before #709: the Agent record 5 times, the new slot 3 times, the new RID 3 times (#709: 2, 1, 1).
    assert (reads["agent"], reads["meta"], reads["rid:rid-1"]) == (1, 0, 1)
    assert responses.respond(responses.call(responses.runtime.read, "rid-1")).status_code == 200
    done = responses.wait("rid-1", "completed")
    time.sleep(0.3)
    reads.clear()
    later = response_submit("rid-2", done.conversation_id, text="Continue")
    accepted = responses.invoke(later, call_id="OFFLINE-next-call")
    assert accepted.status_code == 202 and accepted.json()["dispatch"]["state"] == "needs_response"
    # Before #709: the Agent record 5 times, the slot 3 times, the new RID 3 times (#709: 2, 2, 1).
    assert (reads["agent"], reads["meta"], reads["rid:rid-2"]) == (1, 1, 1)
    assert accepted.json()["dispatch"]["previous_response_id"] == done.response_id


def test_a_record_changed_after_the_admission_mark_is_read_again_before_its_clear(responses, monkeypatch, caplog):
    """The kept ETag only saves reads: a concurrent change (e.g. an environment follow) is kept, never overwritten."""
    import logging
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from shared_context.native_turns import _changed, _json

    runtime, changed = responses.runtime, []
    original = FoundryStateStore.create_item

    async def create(self, key, value, *args, **kwargs):
        if key.startswith("rid:") and not changed:
            current, etag = await runtime._agent_record()
            await self.set_item("agent", _json(_changed(
                current, following_supported=not current.following_supported)), if_match=etag)
            changed.append(not current.following_supported)
        return await original(self, key, value, *args, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "create_item", create)
    caplog.set_level(logging.INFO, logger="shared_context.native_turns")
    accepted = responses.invoke(response_submit())
    assert accepted.status_code == 202, accepted.text
    current, _ = responses.call(runtime._agent_record)
    assert current.admitting is None and current.following_supported is changed[0]
    assert "FA_NATIVE_ADMISSION_SETTLE_REREAD" in caplog.text


def test_admission_reads_the_agent_record_rid_and_conversation_together(responses, monkeypatch):
    """None of the three first reads depends on another, so a follow-up admission issues them at once."""
    import asyncio
    from azure.ai.agentserver.core.storage import FoundryStateStore

    assert responses.invoke(response_submit()).status_code == 202
    assert responses.respond(responses.call(responses.runtime.read, "rid-1")).status_code == 200
    done = responses.wait("rid-1", "completed")
    time.sleep(0.3)
    flight, peak, original = [0], [0], FoundryStateStore.get_item

    async def counted(self, key, *args, **kwargs):
        flight[0] += 1
        peak[0] = max(peak[0], flight[0])
        try:
            await asyncio.sleep(0.02)
            return await original(self, key, *args, **kwargs)
        finally:
            flight[0] -= 1

    monkeypatch.setattr(FoundryStateStore, "get_item", counted)
    later = response_submit("rid-2", done.conversation_id, text="Continue")
    assert responses.invoke(later, call_id="OFFLINE-next-call").status_code == 202
    assert peak[0] >= 3


def test_a_record_changed_before_the_admission_mark_is_checked_and_marked_again(responses, monkeypatch, caplog):
    """The settle read's ETag only saves a read: an environment write before the mark is kept, never overwritten."""
    import logging
    from shared_context import native_user_images
    from shared_context.native_turns import TURNS_STORE, _changed, _json

    runtime, changed = responses.runtime, []
    images = native_user_images.check_user_images

    async def change_then_check(runtime_, request):
        if not changed:
            current, etag = await runtime._agent_record()
            async with runtime._store(TURNS_STORE) as store:
                await store.set_item("agent", _json(_changed(
                    current, following_supported=not current.following_supported)), if_match=etag)
            changed.append(not current.following_supported)
        return await images(runtime_, request)

    monkeypatch.setattr(native_user_images, "check_user_images", change_then_check)
    caplog.set_level(logging.INFO, logger="shared_context.native_turns")
    accepted = responses.invoke(response_submit())
    assert accepted.status_code == 202, accepted.text
    current, _ = responses.call(runtime._agent_record)
    assert current.admitting is None and current.following_supported is changed[0]
    assert "FA_NATIVE_ADMISSION_MARK_REREAD" in caplog.text


def test_a_slot_changed_after_its_check_is_read_again_before_it_is_taken(responses, monkeypatch, caplog):
    """The checked meta's ETag only saves a read: a meta written since (e.g. the previous turn's save) is reread."""
    import logging
    from azure.ai.agentserver.core.storage import FoundryStateStore

    assert responses.invoke(response_submit()).status_code == 202
    assert responses.respond(responses.call(responses.runtime.read, "rid-1")).status_code == 200
    done = responses.wait("rid-1", "completed")
    time.sleep(0.3)
    rewritten, create = [], FoundryStateStore.create_item

    async def rewrite_then_create(self, key, value, *args, **kwargs):
        if key == "rid:rid-2" and not rewritten:
            from shared_context.native_checkpoint import conversation_store_name
            async with responses.runtime._store(conversation_store_name(done.conversation_id)) as conversation:
                item = await conversation.get_item("meta")
                await conversation.set_item("meta", item.value, if_match=item.etag)
            rewritten.append(True)
        return await create(self, key, value, *args, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "create_item", rewrite_then_create)
    caplog.set_level(logging.INFO, logger="shared_context.native_turns")
    later = response_submit("rid-2", done.conversation_id, text="Continue")
    accepted = responses.invoke(later, call_id="OFFLINE-next-call")
    assert accepted.status_code == 202 and accepted.json()["dispatch"]["state"] == "needs_response", accepted.text
    assert rewritten and "FA_NATIVE_SLOT_REREAD" in caplog.text


def test_a_precondition_failure_after_our_own_write_is_read_back_not_redone(responses, monkeypatch, caplog):
    """A transport retry can answer 412 after the first attempt already applied this request's own write."""
    import logging
    from azure.ai.agentserver.core.storage import FoundryStateStore, FoundryStoragePreconditionError

    applied, original = [], FoundryStateStore.set_item

    async def applied_then_refused(self, key, value, *args, **kwargs):
        result = await original(self, key, value, *args, **kwargs)
        mark = key == "agent" and value.get("admitting") is not None
        slot = key == "meta" and value.get("inflight_rid") == "rid-2"
        if (mark or slot) and key not in applied:
            applied.append(key)
            raise FoundryStoragePreconditionError("OFFLINE retried write after the first applied", status_code=412)
        return result

    monkeypatch.setattr(FoundryStateStore, "set_item", applied_then_refused)
    caplog.set_level(logging.INFO, logger="shared_context.native_turns")
    assert responses.invoke(response_submit()).status_code == 202
    assert responses.respond(responses.call(responses.runtime.read, "rid-1")).status_code == 200
    done = responses.wait("rid-1", "completed")
    time.sleep(0.3)
    later = response_submit("rid-2", done.conversation_id, text="Continue")
    accepted = responses.invoke(later, call_id="OFFLINE-next-call")
    assert accepted.status_code == 202 and accepted.json()["dispatch"]["state"] == "needs_response", accepted.text
    current, _ = responses.call(responses.runtime._agent_record)
    assert applied == ["agent", "meta"] and current.admitting is None
    # The mark read back its own record (no re-mark); the slot was read again and found this RID already in it.
    assert "FA_NATIVE_ADMISSION_MARK_REREAD" not in caplog.text and "FA_NATIVE_SLOT_REREAD" in caplog.text


def test_an_accepted_response_waits_for_an_earlier_unexited_cli_before_it_starts(responses):
    """Review P3 on #903: as on the Invocations chain, a Responses turn whose slot opens while an earlier turn's CLI
    still runs stays accepted (no SDK call, so no NATIVE_EXECUTION_UNAVAILABLE after started) and runs once it exits."""
    unexited = [True]
    responses.sdk.runtime_unexited = lambda: unexited[0]
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    assert responses.respond(accepted).status_code == 200
    time.sleep(0.5)
    assert responses.call(responses.runtime.read, "rid-1").status == "accepted" and not responses.sdk.calls
    unexited[0] = False
    assert responses.wait("rid-1", "completed").result is not None
    assert len(responses.sdk.calls) == 1


def test_a_tail_append_on_responses_is_resent_after_the_original_ends_and_runs(responses):
    """Review P1 on #916: Responses refuses a human turn while the slot holder runs (no queue), so the workbench resends
    a turn_finished append only after the original ends; that resend then runs."""
    from test_native_turns import append_command, read_append, steer

    responses.sdk.block = True
    assert responses.invoke(response_submit()).status_code == 202
    assert responses.respond(responses.call(responses.runtime.read, "rid-1")).status_code == 200
    active = responses.wait("rid-1", "started")
    responses.call(steer(responses).channel.close)  # the reply is shown; the SDK still stops and saves
    answer = responses.invoke(append_command(active))
    assert (answer.json()["status"], answer.json().get("reason")) == ("not_applied", "turn_finished"), answer.text
    assert responses.invoke(response_submit("too-early", active.conversation_id)).status_code == 202
    early = responses.call(responses.runtime.read, "too-early")
    assert (early.status, early.failure_code) == ("busy_refused", "NATIVE_TURN_BUSY"), "why the workbench waits here"
    responses.sdk.block = False
    responses.wait("rid-1", "completed")
    assert responses.invoke(response_submit("resent", active.conversation_id, "OFFLINE appended text")).status_code == 202
    resent = responses.call(responses.runtime.read, "resent")
    assert responses.respond(resent).status_code == 200
    assert responses.wait("resent", "completed").result is not None
    assert [call[0] for call in responses.sdk.calls] == ["rid-1", "resent"] and responses.sdk.calls[1][2] is not None
    assert read_append(responses, active).status == "not_applied"


def test_a_response_waiting_on_a_cli_that_never_exits_ends_resendable_within_the_bound(responses, monkeypatch):
    """Review P2-1 on #903: the wait is bounded in every case. A CLI that never exits and a recovery restart that never
    comes end the accepted Responses turn as NATIVE_TURN_NOT_STARTED (resendable) without any SDK call."""
    from shared_context import native_turns

    monkeypatch.setattr(native_turns, "UNEXITED_WAIT_SECONDS", 0.5, raising=False)  # absent before the bound
    responses.sdk.runtime_unexited = lambda: True
    assert responses.invoke(response_submit()).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    assert responses.respond(accepted).status_code == 200
    settled = responses.wait("rid-1", "unknown")
    assert settled.failure_code == "NATIVE_TURN_NOT_STARTED" and not responses.sdk.calls
