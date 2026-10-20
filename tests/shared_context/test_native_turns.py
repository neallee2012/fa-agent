"""Real core 2.1.0 local task/store and host. Only SDK/model I/O is synthetic."""

import asyncio
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from azure.ai.agentserver.core import get_request_context
from azure.ai.agentserver.core.storage import FoundryStateStore, FoundryStoragePreconditionError
from azure.ai.agentserver.core.tasks import RetryPolicy, SteeringQueueFull, TaskConflictError, TaskContext, resilient_tasks_enabled, set_resilient_tasks_enabled
from azure.core.exceptions import ServiceResponseError
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import (
    NATIVE_STATE_PROFILE, NATIVE_TURN_HEADER, NativeCancel, NativeDelivered, NativeHistory,
    NativeRead, NativeResult, NativeSubmit,
)
from shared_context.behavior_sdk_state import SdkState
from shared_context.behavior_turn_usage import TURN_USAGE_SCHEMA, ModelUsage, TurnModelUsage
from shared_context.behavior_wire import AssistantSdkStop, HostedIdentity
from shared_context.errors import Problem
from shared_context.native_checkpoint import conversation_store_name, open_durable_store, segment_key
from shared_context.native_progress_wire import NativeTurnStatusRead
from shared_context.native_turns import (
    EFFECTS_STORE, TURNS_STORE, NativePostTurnPending, NativeSdkResult, NativeTaskInput, NativeTurnControl, NativeTurnRuntime, _changed,
)
from test_native_turn_wire import agent_receive, submit

IDENTITY = HostedIdentity(
    agent_id="11111111-1111-1111-1111-111111111111", agent_name="OFFLINE-agent",
    agent_version="1", session_id="OFFLINE-host-session",
)


def sdk_result(scope, text):
    state = SdkState(
        sdk_session_id=scope.sdk_session_id, model_provider=scope.model_provider,
        codec="session-fs-responses-v1" if scope.model_provider else "session-fs-v1",
        files={"events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": scope.sdk_session_id}}) + "\n",
               "workspace.yaml": "id: " + scope.sdk_session_id + "\n"},
    )
    return NativeSdkResult(text, state, AssistantSdkStop(pid=123, exit_code=0), TurnModelUsage(
        schema_version=TURN_USAGE_SCHEMA, usage=(ModelUsage(
            model="OFFLINE-model", wire_api="responses", requests=1, measured_requests=1,
            input_tokens=10, output_tokens=5),)))


class OfflineSdk:
    def __init__(self):
        self.calls = []
        self.block = False
        self.active = 0
        self.max_active = 0
        self.text = "Complete public answer"
        self.controls = {}

    async def __call__(self, receipt, scope, previous, control):
        self.calls.append((receipt.request_id, receipt.conversation_id, previous, get_request_context().call_id))
        self.controls[receipt.request_id] = control
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            while self.block and not control.stop_requested():
                await asyncio.sleep(0.01)
            if control.stop_requested():
                raise Problem(409, "NATIVE_TURN_CANCELLED", "OFFLINE SDK abort")
            return sdk_result(scope, self.text)
        finally:
            self.active -= 1


@dataclass
class Harness:
    runtime: NativeTurnRuntime
    sdk: OfflineSdk
    client: TestClient

    def invoke(self, command, *, call_id="OFFLINE-original-call"):
        return self.client.post("/invocations", json=command.model_dump(mode="json"), headers={
            NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE,
            "x-agent-session-id": IDENTITY.session_id, "x-agent-invocation-id": "platform-" + uuid4().hex,
            "x-agent-foundry-call-id": call_id,
        })

    def call(self, function, *args):
        return self.client.portal.call(function, *args)

    def wait(self, rid, *statuses):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            receipt = self.call(self.runtime.read, rid)
            if receipt.status in statuses:
                if receipt.status != "started" or rid in self.sdk.controls:
                    return receipt
            elif receipt.status in {"unknown", "busy_refused", "completed"}:
                pytest.fail(f"OFFLINE {rid}: {receipt.status}/{receipt.failure_code}, expected {statuses}")
            time.sleep(0.01)
        pytest.fail(f"OFFLINE {rid} did not reach {statuses}; last status={receipt.status}")


@pytest.fixture
def local_env(monkeypatch, tmp_path):
    old = resilient_tasks_enabled()
    monkeypatch.delenv("FOUNDRY_HOSTING_ENVIRONMENT", raising=False)
    monkeypatch.delenv("FA_NATIVE_TURNS", raising=False)
    monkeypatch.delenv("FA_N0_STATE_PROBE", raising=False)
    monkeypatch.setenv("AGENTSERVER_TASKS_BACKEND", "local")
    state_root = str((tmp_path / "OFFLINE-official-backend").resolve())
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", "\\\\?\\" + state_root if os.name == "nt" else state_root)
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", IDENTITY.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", IDENTITY.agent_name)
    yield
    set_resilient_tasks_enabled(old)


@pytest.fixture
def native_retry_policy(request):
    if hasattr(request, "param"):
        return request.param
    return RetryPolicy(max_attempts=10, initial_delay=0.01, max_delay=0.02, jitter=False,
                       retry_on=(SteeringQueueFull, NativePostTurnPending))


@pytest.fixture
def harness(local_env, tmp_path, native_retry_policy):
    sdk = OfflineSdk()
    cache = str((tmp_path / "OFFLINE-cache").resolve())
    runtime = NativeTurnRuntime(IDENTITY, sdk, cache_root=Path("\\\\?\\" + cache if os.name == "nt" else cache),
                                delivery_retry=native_retry_policy)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        yield Harness(runtime, sdk, client)
        sdk.block = False


def test_real_official_chain_preserves_caller_capture_and_resumes_conversation(harness):
    request = submit()
    response = harness.invoke(request)
    assert response.status_code == 202, response.text
    first = harness.wait("rid-1", "completed")
    status = harness.call(harness.runtime.turn_status, NativeTurnStatusRead(
        conversation_id=first.conversation_id, request_id="rid-1"))
    assert status.receipt == first and status.text == "Complete public answer" and status.next_part is None
    assert status.progress is not None and status.progress.request_id == "rid-1"
    assert status.finished_at is not None and status.finished_at.tzinfo is not None
    assert "finished_at" not in first.model_dump(mode="json")
    assert first.command == request.command and first.capture == request.capture
    assert harness.sdk.calls[0][3] == "OFFLINE-original-call"
    assert first.conversation_id != first.request_id
    assert first.usage.usage[0].requests == 1
    second = submit("rid-2", first.conversation_id, text="Continue original conversation")
    assert harness.invoke(second, call_id="OFFLINE-next-call").status_code == 202
    completed = harness.wait("rid-2", "completed")
    assert completed.turn_number == 2
    assert harness.sdk.calls[1][2] is not None
    assert harness.sdk.calls[1][3] == "OFFLINE-next-call"
    history = harness.call(harness.runtime.history, NativeHistory(conversation_id=first.conversation_id, limit=1))
    assert [turn.request_id for turn in history.turns] == ["rid-1"] and history.next_after_turn == 1
    assert history.turns[0].finished_at == status.finished_at
    page = harness.call(harness.runtime.result, NativeResult(
        conversation_id=first.conversation_id, request_id="rid-1"))
    assert page.complete and page.text == "Complete public answer"
    assert "events.jsonl" not in page.model_dump_json()


@pytest.mark.parametrize("refusal", ["preflight-409", "missing-conversation", "recovery-following", "preflight-503"])
def test_a_definite_refusal_before_the_rid_exists_is_marked_not_dispatched(harness, refusal):
    """Only a refusal raised before ``rid:<RID>`` is created says nothing was admitted; the same RID then goes through."""
    def preflight(_capture):
        raise Problem(503 if refusal == "preflight-503" else 409, "SDK_START_GATE_FENCED", "OFFLINE start gate")

    if refusal.startswith("preflight"):
        harness.runtime.preflight = preflight
    if refusal == "recovery-following":
        harness.runtime._recovery_error = "task_unconfirmed"
    request = submit("rid-refused", "never-created-cid" if refusal == "missing-conversation" else None)
    refused = harness.invoke(request)
    assert 400 <= refused.status_code < 600 and refused.status_code != 202, refused.text
    assert refused.json()["error"].get("not_dispatched") is (None if refusal == "preflight-503" else True)
    with pytest.raises(Problem) as missing:
        harness.call(harness.runtime.read, "rid-refused")
    assert missing.value.body["error"]["code"] == "NATIVE_RID_MISSING"
    harness.runtime.preflight = harness.runtime._recovery_error = None
    # The refused body itself goes through unchanged; a missing CID is resent as a new conversation.
    accepted = harness.invoke(submit("rid-refused") if refusal == "missing-conversation" else request)
    assert accepted.status_code == 202, accepted.text
    assert harness.wait("rid-refused", "completed").request_id == "rid-refused"
    assert [call[0] for call in harness.sdk.calls] == ["rid-refused"]


def test_a_rid_held_by_another_admission_is_a_conflict_not_a_free_rid(harness):
    """NATIVE_RID_CONFLICT means the RID is taken by other material, so it is never marked not_dispatched."""
    from shared_context.native_turns import _Admission, _json

    async def other_admission():
        current, etag = await harness.runtime._agent_record()
        async with harness.runtime._store(TURNS_STORE) as store:
            await store.set_item("agent", _json(_changed(current, admitting=_Admission(
                request_id="rid-taken", submit_digest="0" * 64))), if_match=etag)

    harness.call(other_admission)
    refused = harness.invoke(submit("rid-taken"))
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_RID_CONFLICT", refused.text
    assert "not_dispatched" not in refused.json()["error"]


def test_duplicate_rid_cas_and_two_cids_share_one_official_queue(harness):
    harness.sdk.block = True
    first = submit()
    assert harness.invoke(first).status_code == 202
    active = harness.wait("rid-1", "started")
    for _ in range(3):
        duplicate = harness.invoke(first).json()
        assert duplicate["conversation_id"] == active.conversation_id
    second_response = harness.invoke(submit("rid-2"))
    assert second_response.status_code == 202, second_response.text
    second = second_response.json()
    assert second["status"] == "accepted"
    time.sleep(0.15)
    assert not harness.sdk.controls["rid-1"].stop_requested()
    assert len(harness.sdk.calls) == 1
    harness.sdk.block = False
    harness.wait("rid-1", "completed")
    other = harness.wait("rid-2", "completed")
    assert other.conversation_id != active.conversation_id
    assert [call[0] for call in harness.sdk.calls] == ["rid-1", "rid-2"]
    assert harness.sdk.max_active == 1
    assert harness.sdk.calls[1][2] is None
    for cid, expected in ((active.conversation_id, "rid-1"), (other.conversation_id, "rid-2")):
        page = harness.call(harness.runtime.history, NativeHistory(conversation_id=cid))
        assert [turn.request_id for turn in page.turns] == [expected]


def test_same_rid_different_content_or_capture_is_refused(harness):
    harness.invoke(submit())
    original = harness.wait("rid-1", "completed")
    changed = harness.invoke(submit(text="Different text"))
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "NATIVE_RID_CONFLICT"
    assert harness.invoke(submit()).json()["capture_digest"] == original.capture_digest
    assert len(harness.sdk.calls) == 1


def test_lost_acceptance_ack_reads_original_cid_and_does_not_repeat_sdk(harness, monkeypatch):
    create = FoundryStateStore.create_item
    lost = False

    async def lose(store, key, value, **kwargs):
        nonlocal lost
        result = await create(store, key, value, **kwargs)
        if key == "rid:rid-1" and not lost:
            lost = True
            raise ServiceResponseError("OFFLINE acceptance was saved but ACK lost")
        return result

    monkeypatch.setattr(FoundryStateStore, "create_item", lose)
    accepted = harness.invoke(submit())
    assert accepted.status_code == 202, accepted.text
    first = harness.wait("rid-1", "completed")
    assert harness.invoke(submit()).json()["conversation_id"] == first.conversation_id
    assert len(harness.sdk.calls) == 1


@pytest.mark.parametrize("failure", [ServiceResponseError, FoundryStoragePreconditionError])
def test_lost_started_ack_with_own_token_may_finish_once(harness, monkeypatch, failure):
    update = FoundryStateStore.set_item
    lost = False

    async def lose(store, key, value, **kwargs):
        nonlocal lost
        result = await update(store, key, value, **kwargs)
        if key.startswith("rid:") and value["receipt"]["status"] == "started" and not lost:
            lost = True
            raise failure("OFFLINE started ACK lost")
        return result

    monkeypatch.setattr(FoundryStateStore, "set_item", lose)
    harness.invoke(submit())
    assert harness.wait("rid-1", "completed").result is not None
    assert len(harness.sdk.calls) == 1


@pytest.mark.parametrize("prefix", ["ckpt:", "text:"])
def test_one_unconfirmed_save_write_after_the_sdk_is_retried_not_dropped(harness, monkeypatch, prefix):
    """Audit #1: a 5xx on a checkpoint segment or text part after the SDK finished saves the same reply again."""
    import shared_context.native_turns as native_turns

    create = FoundryStateStore.create_item
    failed = []

    async def fail_first(store, key, value, **kwargs):
        if key.startswith(prefix) and not failed:
            failed.append(key)
            raise ServiceResponseError("OFFLINE segment write unconfirmed")
        return await create(store, key, value, **kwargs)

    monkeypatch.setattr(native_turns, "SAVE_RETRY_SECONDS", (0.01, 0.01))
    monkeypatch.setattr(FoundryStateStore, "create_item", fail_first)
    harness.invoke(submit())
    completed = harness.wait("rid-1", "completed")
    page = harness.call(harness.runtime.result, NativeResult(conversation_id=completed.conversation_id, request_id="rid-1"))
    assert failed and page.complete and page.text == "Complete public answer"
    assert len(harness.sdk.calls) == 1


def accept_without_chain(harness):
    chain = harness.runtime.chain

    async def bound_elsewhere(**kwargs):
        raise TaskConflictError("OFFLINE original chain is bound elsewhere")

    harness.runtime.chain = SimpleNamespace(start=bound_elsewhere)
    try:
        response = harness.invoke(submit())
        assert response.status_code == 409
    finally:
        harness.runtime.chain = chain
    return harness.call(harness.runtime._rid, "rid-1")


def task_context(original, *, entry_mode="recovered"):
    return TaskContext(
        task_id=IDENTITY.agent_id, session_id=IDENTITY.session_id, input_id="rid-1", entry_mode=entry_mode,
        input=NativeTaskInput(request_id="rid-1", accepted_etag=original.etag, call_id=original.value.call_id),
    )


def seed_started(harness):
    accepted = accept_without_chain(harness)
    started = _changed(accepted.value, run_token="OFFLINE-run",
                       receipt=_changed(accepted.value.receipt, status="started"))
    current, saved = harness.call(harness.runtime._cas, accepted, started)
    assert saved
    return accepted, current


@pytest.mark.parametrize(("code", "recorded"), [
    # Raised only before the SDK starts: the cancel is recorded as one.
    ("NATIVE_TURN_NOT_STARTED", "NATIVE_TURN_CANCELLED"), ("NATIVE_EXECUTION_UNAVAILABLE", "NATIVE_TURN_CANCELLED"),
    ("NATIVE_TURN_CANCELLED", "NATIVE_TURN_CANCELLED"),
    # The SDK may have run: each keeps its own code.
    *((code, code) for code in (
        "NATIVE_TURN_TIMEOUT", "NATIVE_TURN_UNKNOWN", "NATIVE_STATE_STORE_UNKNOWN", "NATIVE_RID_CONFLICT",
        "NATIVE_RESULT_INCOMPLETE", "NATIVE_STATE_INVALID", "ASSISTANT_MODEL_OUTCOME_UNKNOWN",
        "SDK_STATE_UPLOAD_UNKNOWN", "NATIVE_MODEL_REFUSED", "NATIVE_POST_TURN_UNKNOWN",
        "NATIVE_RELAY_OUTCOME_UNKNOWN"))])
def test_unknown_after_a_concurrent_cancel_write_merges_instead_of_leaving_the_rid_started(harness, code, recorded):
    """Audit #3: the ``unknown`` CAS loses to a cancel_requested write; the RID still ours ends ``unknown``. The cancel
    becomes ``NATIVE_TURN_CANCELLED`` only for a code raised before the SDK starts; any other code is kept, and
    ``cancel_requested`` stays on the receipt."""
    _, started = seed_started(harness)
    cancelled = _changed(started.value, receipt=_changed(started.value.receipt, cancel_requested=True))
    _, saved = harness.call(harness.runtime._cas, started, cancelled)
    assert saved
    receipt = harness.call(harness.runtime._unknown, started, code)  # the stale ETag
    current = harness.call(harness.runtime._rid, "rid-1").value
    assert receipt.status == current.receipt.status == "unknown" and receipt.failure_code == recorded
    assert current.receipt.cancel_requested and current.run_token == "OFFLINE-run"


def test_cancel_landing_during_queued_recovery_keeps_the_unconfirmed_relay_code(harness):
    """The recovery path only runs for a sender whose tool call already dispatched, so the SDK has run: a cancel that
    lands after the entry check but before the re-read is kept as ``cancel_requested``, not as a cancelled turn."""
    _, started = seed_started(harness)

    async def cancel_then_fail(original):
        cancelled = _changed(original.value, receipt=_changed(original.value.receipt, cancel_requested=True))
        _, saved = await harness.runtime._cas(original, cancelled)
        assert saved
        raise Problem(503, "NATIVE_RELAY_OUTCOME_UNKNOWN", "the original queued send result is unconfirmed")

    harness.runtime.on_recover_queued = cancel_then_fail
    receipt = harness.call(lambda: harness.runtime._reenter(started, recovered=True))
    current = harness.call(harness.runtime._rid, "rid-1").value.receipt
    assert receipt.status == current.status == "unknown"
    assert receipt.failure_code == current.failure_code == "NATIVE_RELAY_OUTCOME_UNKNOWN"
    assert current.cancel_requested
    assert not harness.sdk.calls


def test_accepted_gap_read_is_pure_and_exact_resubmit_requeues_original(harness):
    accepted = accept_without_chain(harness)
    for _ in range(2):
        read = harness.invoke(NativeRead(request_id="rid-1"))
        assert read.json()["status"] == "accepted"
    assert not harness.sdk.calls
    assert harness.invoke(submit(), call_id="OFFLINE-new-request-context").status_code == 202
    completed = harness.wait("rid-1", "completed")
    assert completed.conversation_id == accepted.value.receipt.conversation_id
    assert harness.sdk.calls[0][3] == "OFFLINE-original-call"


@pytest.mark.parametrize("entry_mode", ["fresh", "resumed", "recovered"])
def test_started_reentry_never_guesses_from_entry_mode_or_repeats_sdk(harness, entry_mode):
    accepted, _ = seed_started(harness)
    returned = harness.call(harness.runtime.handle_turn, task_context(accepted, entry_mode=entry_mode))
    assert returned.status == "unknown"
    assert not harness.sdk.calls
    assert harness.invoke(submit()).json()["status"] == "unknown"


async def leave_saved_turn(runtime, current):
    current, meta, previous = await runtime._allocate(current)
    assert previous is None
    cas = runtime._cas

    async def stop_before_completed(original, replacement):
        if replacement.receipt.status == "completed":
            raise ServiceResponseError("OFFLINE saved turn, lost completion request")
        return await cas(original, replacement)

    runtime._cas = stop_before_completed
    try:
        with pytest.raises(ServiceResponseError):
            await runtime._save_turn(current, meta, sdk_result(runtime._scope(meta), "Original durable answer"))
    finally:
        runtime._cas = cas
    return current, meta


@pytest.mark.parametrize("missing", [False, True])
def test_reentry_completes_only_with_every_durable_segment_not_home_cache(harness, missing):
    accepted, started = seed_started(harness)
    current, meta = harness.call(leave_saved_turn, harness.runtime, started)

    async def drop():
        async with await open_durable_store(conversation_store_name(meta.conversation_id), create=False) as store:
            turn = await harness.runtime._turn(store, current.value.receipt)
            await store.delete_item(segment_key(turn.checkpoints[0].sha256, 0))

    if missing:
        harness.call(drop)
    returned = harness.call(harness.runtime.handle_turn, task_context(accepted))
    assert returned.status == ("unknown" if missing else "completed")
    assert not harness.sdk.calls
    if not missing:
        assert returned.result is not None and returned.usage.usage[0].output_tokens == 5


def test_large_answer_has_complete_unicode_pages_not_the_legacy_32k_limit(harness):
    harness.sdk.text = "完整答案🎯\n" * 60_000
    harness.invoke(submit())
    receipt = harness.wait("rid-1", "completed")
    assert receipt.result.text_parts > 1
    texts, part = [], 0
    while True:
        page = harness.call(harness.runtime.result, NativeResult(
            conversation_id=receipt.conversation_id, request_id="rid-1", part=part))
        assert page.complete is False
        texts.append(page.text)
        if page.next_part is None:
            break
        part = page.next_part
    assert "".join(texts) == harness.sdk.text
    assert receipt.result.text_bytes == len(harness.sdk.text.encode("utf-8"))


def test_read_history_cancel_ack_never_create_missing_stores_or_chain(harness, tmp_path):
    commands = (
        NativeRead(request_id="absent"), NativeHistory(conversation_id="absent"),
        NativeResult(conversation_id="absent", request_id="absent"),
        NativeCancel(conversation_id="absent", request_id="absent"),
        NativeDelivered(conversation_id="absent", request_id="absent", result_digest="a" * 64),
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    for command in commands:
        response = harness.invoke(command)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == (
            "NATIVE_STATE_STORE_MISSING" if isinstance(command, NativeHistory) else "NATIVE_RID_MISSING")
    after = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    assert before == after and not harness.sdk.calls


@pytest.mark.parametrize("store_name", [TURNS_STORE, EFFECTS_STORE])
def test_default_ttl_stops_startup_even_when_get_or_create_ignores_it(local_env, store_name):
    async def create():
        async with await FoundryStateStore.get_or_create(store_name) as store:
            assert (await store.get()).item_ttl_seconds != -1

    asyncio.run(create())
    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with pytest.raises(Problem) as error:
        with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)):
            pytest.fail("An expiring store must not become ready")
    assert error.value.body["error"]["code"] == "NATIVE_STATE_STORE_TTL"
    assert not sdk.calls


def test_cancel_names_one_queued_cid_rid_and_does_not_abort_the_active_one(harness):
    harness.sdk.block = True
    harness.invoke(submit())
    active = harness.wait("rid-1", "started")
    queued = harness.invoke(submit("rid-2")).json()
    wrong = harness.invoke(NativeCancel(conversation_id=active.conversation_id, request_id="rid-2"))
    assert wrong.status_code == 403
    response = harness.invoke(NativeCancel(conversation_id=queued["conversation_id"], request_id="rid-2"))
    assert response.json()["cancel_requested"] is True
    time.sleep(0.15)
    assert harness.sdk.controls["rid-1"].cancelled is False
    harness.sdk.block = False
    harness.wait("rid-1", "completed")
    cancelled = harness.wait("rid-2", "unknown")
    assert cancelled.failure_code == "NATIVE_TURN_CANCELLED"
    assert [call[0] for call in harness.sdk.calls] == ["rid-1"]


def test_real_targeted_cancel_aborts_only_the_active_sdk_then_drains_queue(harness):
    harness.sdk.block = True
    harness.invoke(submit())
    active = harness.wait("rid-1", "started")
    harness.invoke(submit("rid-2"))
    response = harness.invoke(NativeCancel(conversation_id=active.conversation_id, request_id="rid-1"))
    assert response.json()["cancel_requested"]
    harness.wait("rid-1", "unknown")
    harness.sdk.block = False
    harness.wait("rid-2", "completed")
    assert harness.sdk.controls["rid-1"].cancelled and not harness.sdk.controls["rid-2"].cancelled


@pytest.mark.parametrize("cause", ["cancel", "timeout", "shutdown"])
def test_official_cancel_causes_win_over_steering_without_false_shutdown_terminal(harness, cause):
    accepted = accept_without_chain(harness)
    ctx = task_context(accepted)
    ctx._pending_count_provider = lambda: 1
    ctx.cancel.set()
    if cause == "cancel":
        ctx.cancel_requested = True
    elif cause == "timeout":
        ctx.timeout_exceeded = True
    else:
        ctx.shutdown.set()
    harness.call(harness.runtime.handle_turn, ctx)
    stored = harness.call(harness.runtime.read, "rid-1")
    assert stored.status == ("accepted" if cause == "shutdown" else "unknown")
    assert not harness.sdk.calls
    if cause != "shutdown":
        assert stored.failure_code == ("NATIVE_TURN_CANCELLED" if cause == "cancel" else "NATIVE_TURN_TIMEOUT")


def test_a_drained_input_whose_handler_starts_after_shutdown_ends_resendable(harness):
    """Review P3-2 on #903: core's re-entry of a drained input gets payload.input=None and can never run it, so a
    drained handler that starts after shutdown settles its unstarted RID before the recovery exit (an undrained one
    stays accepted for its re-entry, above)."""
    accepted = accept_without_chain(harness)
    ctx = task_context(accepted, entry_mode="resumed")
    ctx.is_steered_turn = True
    ctx.shutdown.set()
    harness.call(harness.runtime.handle_turn, ctx)
    stored = harness.call(harness.runtime.read, "rid-1")
    assert (stored.status, stored.failure_code) == ("unknown", "NATIVE_TURN_NOT_STARTED")
    assert not harness.sdk.calls


def test_a_turn_waiting_on_a_cli_that_never_exits_ends_resendable_within_the_bound(harness, monkeypatch):
    """Review P2-1 on #903: the Invocations wait before "started" is bounded too; a CLI that never exits and a
    recovery restart that never comes end the RID as NATIVE_TURN_NOT_STARTED without any SDK call."""
    from shared_context import native_turns

    monkeypatch.setattr(native_turns, "UNEXITED_WAIT_SECONDS", 0.5, raising=False)  # absent before the bound
    harness.sdk.runtime_unexited = lambda: True
    harness.invoke(submit())
    assert harness.wait("rid-1", "unknown").failure_code == "NATIVE_TURN_NOT_STARTED"
    assert not harness.sdk.calls


@pytest.mark.parametrize("status", ["accepted", "started"])
def test_a_recovered_entry_without_input_reads_the_drained_input_from_the_task_record(harness, monkeypatch, status):
    """Review P3-1 on #903: core re-enters a drained input with payload.input=None and steering.active_input=<input>.
    Accepted, that RID can never run again and ends resendable; started, it re-enters as any recovered start."""
    accepted = accept_without_chain(harness) if status == "accepted" else seed_started(harness)[0]
    active = task_context(accepted).input.model_dump(mode="json")
    reentered = []

    async def original(*, task_id=None):
        assert task_id == IDENTITY.agent_id
        return {"payload": {"input": None, "steering": {"active_input": active, "drain_in_progress": False}}}

    async def reenter(original, **kwargs):
        reentered.append(original.value.receipt.request_id)
        return original.value.receipt

    harness.runtime.task_observer = SimpleNamespace(original=original)
    monkeypatch.setattr(harness.runtime, "_reenter", reenter)
    ctx = task_context(accepted)
    ctx.input = None
    harness.call(harness.runtime.handle_turn, ctx)
    stored = harness.call(harness.runtime.read, "rid-1")
    if status == "accepted":
        assert (stored.status, stored.failure_code) == ("unknown", "NATIVE_TURN_NOT_STARTED") and not reentered
    else:
        assert stored.status == "started" and reentered == ["rid-1"]
    assert not harness.sdk.calls


def test_delivery_binds_the_complete_digest_not_a_human_read_claim(harness):
    harness.invoke(submit())
    receipt = harness.wait("rid-1", "completed")
    wrong = harness.invoke(NativeDelivered(
        conversation_id=receipt.conversation_id, request_id="rid-1", result_digest="f" * 64))
    assert wrong.status_code == 409
    command = NativeDelivered(conversation_id=receipt.conversation_id, request_id="rid-1",
                              result_digest=receipt.result.result_digest)
    assert harness.invoke(command).json()["delivered"] is True
    assert harness.invoke(command).json()["result"] == receipt.result.model_dump(mode="json")
    assert len(harness.sdk.calls) == 1


def test_startup_initializes_only_durable_agent_stores_for_first_read(harness):
    async def descriptors():
        for name in (TURNS_STORE, EFFECTS_STORE):
            async with await open_durable_store(name, create=False) as store:
                assert (await store.get()).item_ttl_seconds == -1

    harness.call(descriptors)
    response = harness.invoke(NativeRead(request_id="never-seen"))
    assert response.status_code == 404 and response.json()["error"]["code"] == "NATIVE_RID_MISSING"
    assert not harness.sdk.calls


def test_new_version_same_sid_reads_original_but_changed_sid_cannot_replace_chain(local_env, monkeypatch):
    sdk = OfflineSdk()
    first = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first)) as client:
        h = Harness(first, sdk, client)
        h.invoke(submit())
        completed = h.wait("rid-1", "completed")
    for sid in (IDENTITY.session_id, "OFFLINE-new-session"):
        identity = IDENTITY.model_copy(update={"agent_version": "2", "session_id": sid})
        monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", sid)
        next_sdk = OfflineSdk()
        next_runtime = NativeTurnRuntime(identity, next_sdk)
        with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=next_runtime)) as client:
            assert client.portal.call(next_runtime.read, "rid-1") == completed
            if sid != IDENTITY.session_id:
                with pytest.raises(Problem) as denied:
                    client.portal.call(next_runtime.submit, submit("rid-new"))
                assert denied.value.body["error"]["code"] == "NATIVE_SESSION_CHANGED"
                assert not next_sdk.calls


def test_real_host_shutdown_keeps_started_and_restart_reentry_never_repeats_sdk(local_env):
    sdk = OfflineSdk()
    sdk.block = True
    first = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first)) as client:
        h = Harness(first, sdk, client)
        h.invoke(submit())
        h.wait("rid-1", "started")
    assert asyncio.run(first.read("rid-1")).status == "started"
    restored_sdk = OfflineSdk()
    restored = NativeTurnRuntime(IDENTITY, restored_sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=restored)) as client:
        resumed = Harness(restored, restored_sdk, client).wait("rid-1", "unknown")
        assert resumed.result is None and not restored_sdk.calls


def test_official_steering_queue_full_returns_original_busy_refusal(harness):
    seen = []
    handler = harness.runtime.handle_turn

    async def observe(ctx):
        seen.append((ctx.input_id, ctx.input.request_id))
        return await handler(ctx)

    harness.runtime.handle_turn = observe
    harness.sdk.block = True
    harness.invoke(submit())
    harness.wait("rid-1", "started")
    queued = []
    for index in range(2, 11):
        request = submit(f"rid-{index}")
        response = harness.invoke(request)
        assert response.status_code == 202
        assert response.json()["status"] == "accepted"
        queued.append(request.command.request_id)
    refused = harness.invoke(submit("rid-overflow"))
    assert refused.json()["status"] == "busy_refused"
    assert refused.json()["failure_code"] == "NATIVE_TURN_BUSY"
    assert harness.invoke(submit("rid-overflow")).json()["status"] == "busy_refused"
    harness.sdk.block = False
    for rid in ("rid-1", *queued):
        harness.wait(rid, "completed")
    assert len(harness.sdk.calls) == 10
    assert [rid for _, rid in seen] == ["rid-1", *queued]
    assert any(reported != original for reported, original in seen[1:])


def test_concurrent_same_accepted_etag_starts_the_model_only_once(harness):
    accepted = accept_without_chain(harness)
    harness.sdk.block = True
    first, duplicate = task_context(accepted), task_context(accepted)

    async def raced():
        first_task = asyncio.create_task(harness.runtime.handle_turn(first))
        while "rid-1" not in harness.sdk.controls:
            await asyncio.sleep(0.01)
        second = await harness.runtime.handle_turn(duplicate)
        harness.sdk.block = False
        return await first_task, second

    result, duplicate_result = harness.call(raced)
    assert result.status == duplicate_result.status == "unknown"
    assert len(harness.sdk.calls) == 1
    assert harness.call(harness.runtime.read, "rid-1").result is None


def test_stale_accepted_etag_cannot_execute_after_a_cancel_flag(harness):
    accepted = accept_without_chain(harness)
    original = accepted.value.receipt
    harness.call(harness.runtime.cancel, NativeCancel(
        conversation_id=original.conversation_id, request_id=original.request_id))
    returned = harness.call(harness.runtime.handle_turn, task_context(accepted))
    assert returned.status == "unknown" and not harness.sdk.calls


def test_saved_result_with_lost_completed_ack_is_read_back_without_another_model(harness, monkeypatch):
    update = FoundryStateStore.set_item
    lost = False

    async def lose(store, key, value, **kwargs):
        nonlocal lost
        result = await update(store, key, value, **kwargs)
        if key.startswith("rid:") and value["receipt"]["status"] == "completed" and not lost:
            lost = True
            raise ServiceResponseError("OFFLINE completed ACK lost")
        return result

    monkeypatch.setattr(FoundryStateStore, "set_item", lose)
    harness.invoke(submit())
    receipt = harness.wait("rid-1", "completed")
    assert harness.invoke(submit()).json()["result"] == receipt.result.model_dump(mode="json")
    assert len(harness.sdk.calls) == 1


def test_an_answer_above_the_segmented_bound_is_explicit_unknown_not_truncated(harness):
    from shared_context.backup_models import MAX_BACKUP_BYTES

    harness.sdk.text = "x" * (MAX_BACKUP_BYTES + 1)
    harness.invoke(submit())
    receipt = harness.wait("rid-1", "unknown")
    assert receipt.failure_code == "NATIVE_RESULT_TOO_LARGE" and receipt.result is None
    assert receipt.usage.usage[0].requests == 1
    assert harness.invoke(submit()).json()["status"] == "unknown"
    assert len(harness.sdk.calls) == 1


def test_state_store_size_guard_counts_the_pinned_sdk_json_encoding():
    from shared_context.native_turns import MAX_ITEM_BYTES, _json

    overhead = len(json.dumps({"text": ""}).encode("utf-8"))
    largest = {"text": "x" * (MAX_ITEM_BYTES - overhead)}
    assert _json(largest) == largest
    for too_large in ({"text": largest["text"] + "x"}, {"text": "界" * (MAX_ITEM_BYTES // 6)}):
        with pytest.raises(Problem) as rejected:
            _json(too_large)
        assert rejected.value.status == 413 and rejected.value.body["error"]["code"] == "NATIVE_REQUEST_TOO_LARGE"


def test_agent_receive_uses_same_chain_and_public_history_keeps_provenance(harness):
    message = agent_receive(rid="rid-1")
    wrong = harness.invoke(agent_receive(rid="another", recipient="another-agent"))
    assert wrong.status_code == 403
    accepted = harness.invoke(message)
    assert accepted.status_code == 202, accepted.text
    receipt = harness.wait("rid-1", "completed")
    assert receipt.source == message.source and receipt.command.authorize_own_result is False
    history = harness.call(harness.runtime.history, NativeHistory(conversation_id=receipt.conversation_id))
    assert history.turns[0].user_text is None and history.turns[0].agent_text == message.command.content
    assert history.turns[0].source == message.source
    assert harness.invoke(message).json()["result"] == receipt.result.model_dump(mode="json")
    assert len(harness.sdk.calls) == 1
    changed = message.model_copy(update={"source": message.source.model_copy(update={"root_id": "changed"})})
    assert harness.invoke(changed).status_code == 409


def test_completion_hook_observes_only_saved_originals_and_read_never_calls_it(harness):
    calls = []

    async def completed(receipt, reference):
        current = await harness.runtime.read(receipt.request_id)
        result = await harness.runtime.result(NativeResult(
            conversation_id=receipt.conversation_id, request_id=receipt.request_id))
        assert current.status == "completed" and result.result == reference == current.result
        calls.append((receipt.request_id, reference.result_digest))

    harness.runtime.on_completed = completed
    message = agent_receive(rid="rid-1")
    harness.invoke(message)
    receipt = harness.wait("rid-1", "completed")
    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == [("rid-1", receipt.result.result_digest)]
    harness.invoke(NativeRead(request_id="rid-1"))
    harness.call(harness.runtime.history, NativeHistory(conversation_id=receipt.conversation_id))
    assert len(calls) == 1
    # Framework reentry may repeat a callback; it must retain the original outcome and not execute the SDK.
    harness.invoke(message)
    assert all(call == calls[0] for call in calls)
    assert len(harness.sdk.calls) == 1


def test_lost_completion_callback_preserves_terminal_usage_and_never_reruns_sdk(harness):
    calls = []

    async def unknown(receipt, reference):
        calls.append(reference.result_digest)
        raise OSError("OFFLINE downstream ACK lost")

    harness.runtime.on_completed = unknown
    harness.invoke(agent_receive(rid="rid-1"))
    receipt = harness.wait("rid-1", "completed")
    response = harness.invoke(agent_receive(rid="rid-1"))
    assert response.status_code == 202
    deadline = time.monotonic() + 8
    current = receipt
    while current.notification_error is None and time.monotonic() < deadline:
        time.sleep(0.02)
        current = harness.call(harness.runtime.read, "rid-1")
    assert current.notification_error == "NATIVE_POST_TURN_UNKNOWN"
    assert current.result == receipt.result and current.usage == receipt.usage and current.status == "completed"
    assert len(harness.sdk.calls) == 1 and all(digest == receipt.result.result_digest for digest in calls)


def test_disappearing_initialized_store_is_not_recreated_on_submit(harness):
    async def remove():
        async with await open_durable_store(TURNS_STORE, create=False) as store:
            await store.delete()

    harness.call(remove)
    response = harness.invoke(submit())
    assert response.status_code == 404 and response.json()["error"]["code"] == "NATIVE_STATE_STORE_MISSING"
    assert not harness.sdk.calls


def test_turn_phases_share_one_hashed_id_without_logging_the_rid(harness, caplog):
    from shared_context.native_turns import timing_id

    caplog.set_level(logging.WARNING, logger="shared_context.behavior_timing")
    assert harness.invoke(submit()).status_code == 202
    harness.wait("rid-1", "completed")
    timings = [json.loads(record.getMessage().removeprefix("FA_TIMING ")) for record in caplog.records
               if record.getMessage().startswith("FA_TIMING ")]
    phases = {value["phase"]: value["job_id"] for value in timings}
    expected = {"native_queue_wait", "native_start", "native_allocate", "native_executor", "native_save_turn"}
    assert expected <= phases.keys()
    assert {phases[name] for name in expected} == {timing_id("rid-1")}
    assert all("rid-1" not in json.dumps(value) for value in timings)


def test_admission_segments_share_the_turns_hashed_id(harness, caplog):
    """v17: Hosted spent about 1.1 s admitting one turn with nothing timed inside ``_submit_locked``."""
    from shared_context.native_turns import timing_id

    caplog.set_level(logging.WARNING, logger="shared_context.behavior_timing")
    assert harness.invoke(submit()).status_code == 202
    harness.wait("rid-1", "completed")
    timings = [json.loads(record.getMessage().removeprefix("FA_TIMING ")) for record in caplog.records
               if record.getMessage().startswith("FA_TIMING ")]
    admission = [value for value in timings if value["phase"].startswith("native_submit_")]
    assert [value["phase"] for value in admission] == [
        "native_submit_pending", "native_submit_rid_read", "native_submit_checks", "native_submit_create",
        "native_submit_settle", "native_submit_session", "native_submit_start"]
    assert {value["job_id"] for value in admission} == {timing_id("rid-1")}
    assert all("rid-1" not in json.dumps(value) for value in timings)


def test_a_later_turn_reuses_store_clients_and_reads_each_ttl_once(harness, monkeypatch):
    """Official clients are long-lived; a store's TTL cannot change after creation (ADR-0007 §1)."""
    from shared_context import native_checkpoint

    assert harness.invoke(submit()).status_code == 202
    first = harness.wait("rid-1", "completed")
    opened, ttl_reads = [], []
    original_open, original_get = native_checkpoint.open_durable_store, FoundryStateStore.get

    async def counted_open(name, **kwargs):
        opened.append(name)
        return await original_open(name, **kwargs)

    async def counted_get(store):
        ttl_reads.append(store)
        return await original_get(store)

    monkeypatch.setattr(native_checkpoint, "open_durable_store", counted_open)
    monkeypatch.setattr(FoundryStateStore, "get", counted_get)
    later = submit("rid-2", first.conversation_id, text="Continue original conversation")
    assert harness.invoke(later, call_id="OFFLINE-next-call").status_code == 202
    assert harness.wait("rid-2", "completed").turn_number == 2
    assert opened == [] and ttl_reads == []


def test_official_reentry_after_completed_before_envelope_recovers_one_original_wake(harness):
    from azure.ai.agentserver.core.storage import FoundryStorageConflictError

    accepted = accept_without_chain(harness)
    missed = True
    envelopes = []

    async def completed(receipt, reference):
        nonlocal missed
        if missed:
            missed = False
            raise OSError("OFFLINE crash before durable completion envelope")
        envelope = {"rid": receipt.request_id, "result_digest": reference.result_digest, "wake_id": "same-original-wake"}
        async with await open_durable_store(EFFECTS_STORE, create=False) as store:
            try:
                await store.create_item("wake:same-original-wake", envelope)
            except FoundryStorageConflictError:
                assert (await store.get_item("wake:same-original-wake")).value == envelope
            envelopes.append((await store.get_item("wake:same-original-wake")).value)

    harness.runtime.on_completed = completed

    async def run():
        context = task_context(accepted).input
        first = await harness.runtime.chain.start(task_id=IDENTITY.agent_id, input_id="rid-1", input=context)
        await first.result()
        original = await harness.runtime.read("rid-1")
        assert original.status == "completed"
        async with asyncio.timeout(8):
            while not envelopes:
                await asyncio.sleep(0.02)
        replay = await harness.runtime.chain.start(task_id=IDENTITY.agent_id, input_id="rid-1", input=context)
        await replay.result()
        assert await harness.runtime.read("rid-1") == original
        async with await open_durable_store(EFFECTS_STORE, create=False) as store:
            assert len((await store.list_keys()).keys) == 1

    harness.call(run)
    assert len(harness.sdk.calls) == 1
    assert envelopes and all(envelope == envelopes[0] for envelope in envelopes)


def flaky_reads(monkeypatch, key):
    """``mode``: None reads the store; "unknown" raises a transport error; "missing" returns no item once."""
    get = FoundryStateStore.get_item
    state = SimpleNamespace(mode=None, hits=0, reads=0)

    async def read(store, item_key, **kwargs):
        state.reads += item_key == key
        if item_key == key and state.mode is not None:
            state.hits += 1
            if state.mode == "missing":
                state.mode = None
                return None
            raise ServiceResponseError("OFFLINE store read outcome unknown")
        return await get(store, item_key, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "get_item", read)
    return state


def until(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("OFFLINE condition was not reached")
        time.sleep(0.01)


@pytest.fixture
def quick_control(monkeypatch):
    from shared_context import native_turns
    monkeypatch.setattr(native_turns, "CONTROL_POLL_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(native_turns, "SLOT_WAIT_POLL_SECONDS", 0.05, raising=False)


def test_an_unconfirmed_control_read_keeps_the_running_turn(harness, monkeypatch, quick_control):
    """A 503 from one RID read is not an answer: the turn keeps running and saves (audit finding 4)."""
    harness.sdk.block = True
    harness.invoke(submit())
    harness.wait("rid-1", "started")
    flaky = flaky_reads(monkeypatch, "rid:rid-1")
    flaky.mode = "unknown"
    until(lambda: flaky.hits >= 2)
    flaky.mode = None
    assert not harness.sdk.controls["rid-1"].stop_requested()
    harness.sdk.block = False
    assert harness.wait("rid-1", "completed").result is not None
    assert len(harness.sdk.calls) == 1


def test_an_unconfirmed_control_read_keeps_a_turn_waiting_for_the_slot(harness, monkeypatch, quick_control):
    """The execution-slot waiter shares the observer: a 503 keeps it waiting instead of failing the RID."""
    accepted = accept_without_chain(harness)
    flaky = flaky_reads(monkeypatch, "rid:rid-1")

    async def run():
        lock = harness.runtime.execution_lock
        await lock.acquire()
        task = asyncio.create_task(harness.runtime.handle_turn(task_context(accepted, entry_mode="fresh")))
        try:
            async with asyncio.timeout(10):
                await asyncio.sleep(0.3)
                flaky.mode = "unknown"
                while flaky.hits < 2:
                    await asyncio.sleep(0.01)
                flaky.mode = None
                assert not task.done()
        finally:
            lock.release()
        async with asyncio.timeout(10):
            return await task

    receipt = harness.call(run)
    assert receipt.status == "completed" and receipt.result is not None
    assert [call[0] for call in harness.sdk.calls] == ["rid-1"]


def test_a_changed_run_token_still_fences_the_running_turn(harness, quick_control):
    harness.sdk.block = True
    harness.invoke(submit())
    harness.wait("rid-1", "started")
    current = harness.call(harness.runtime._rid, "rid-1")
    harness.call(harness.runtime._cas, current, _changed(current.value, run_token="OFFLINE-other-run"))
    until(lambda: harness.sdk.controls["rid-1"].fenced)
    harness.sdk.block = False
    assert harness.call(harness.runtime.read, "rid-1").result is None


def test_a_missing_rid_is_a_definite_answer_and_fences_the_turn(harness, monkeypatch, quick_control):
    harness.sdk.block = True
    harness.invoke(submit())
    harness.wait("rid-1", "started")
    flaky = flaky_reads(monkeypatch, "rid:rid-1")
    flaky.mode = "missing"
    until(lambda: harness.sdk.controls["rid-1"].fenced)
    harness.sdk.block = False
    stopped = harness.wait("rid-1", "unknown")
    assert stopped.failure_code == "NATIVE_TURN_UNKNOWN" and stopped.result is None


def test_a_running_turn_reads_its_rid_about_once_a_second(harness, monkeypatch):
    """Audit finding 14: the control observer read the RID every 0.1 s (10 reads/s per turn)."""
    harness.sdk.block = True
    harness.invoke(submit())
    harness.wait("rid-1", "started")
    counted = flaky_reads(monkeypatch, "rid:rid-1")
    time.sleep(2.2)
    reads = counted.reads
    harness.sdk.block = False
    harness.wait("rid-1", "completed")
    assert 1 <= reads <= 4, reads


def test_a_turn_waiting_for_the_slot_reads_its_rid_at_most_every_two_seconds(harness, monkeypatch):
    accepted = accept_without_chain(harness)
    counted = flaky_reads(monkeypatch, "rid:rid-1")

    async def run():
        lock = harness.runtime.execution_lock
        await lock.acquire()
        task = asyncio.create_task(harness.runtime.handle_turn(task_context(accepted, entry_mode="fresh")))
        try:
            await asyncio.sleep(0.5)
            before = counted.reads
            await asyncio.sleep(2.2)
            during = counted.reads - before
            assert not task.done()
        finally:
            released = time.monotonic()
            lock.release()
        async with asyncio.timeout(10):
            while not harness.sdk.calls:
                await asyncio.sleep(0.01)
            wake = time.monotonic() - released
            return during, wake, await task

    during, wake, receipt = harness.call(run)
    assert receipt.status == "completed" and 1 <= during <= 2, during
    # The released lock wakes the waiter at once; it does not wait for its next RID read.
    assert wake < 1.5, wake


def running_observer_reads(harness, monkeypatch):
    """``cancel_requested`` as each RID read of the running turn's control observer returned it (not the slot
    waiter's reads, nor the test's own)."""
    from contextvars import ContextVar

    running, reads = ContextVar("running_observer", default=False), []
    observe, read = harness.runtime._observe_control, harness.runtime._rid

    async def observing(*args, waiting=False, **kwargs):
        running.set(not waiting)  # the observer's own task context
        return await observe(*args, waiting=waiting, **kwargs)

    async def reading(rid):
        value = await read(rid)
        if running.get():
            reads.append(value.value.receipt.cancel_requested)
        return value

    monkeypatch.setattr(harness.runtime, "_observe_control", observing)
    monkeypatch.setattr(harness.runtime, "_rid", reading)
    return reads


def test_a_cancel_saved_here_wakes_the_control_read_at_once(harness, monkeypatch):
    """The poll is the fallback; a cancel this process saved is seen without waiting for it. The observer's first read
    has returned before the cancel and its next poll is an hour away, so the read that sees the cancel is the wake's."""
    from shared_context import native_turns

    monkeypatch.setattr(native_turns, "CONTROL_POLL_SECONDS", 3600.0)
    reads = running_observer_reads(harness, monkeypatch)
    harness.sdk.block = True
    harness.invoke(submit())
    active = harness.wait("rid-1", "started")
    until(lambda: reads)
    assert reads == [False]
    cancel = harness.invoke(NativeCancel(conversation_id=active.conversation_id, request_id="rid-1"))
    assert cancel.status_code == 200 and cancel.json()["cancel_requested"], cancel.text
    until(lambda: harness.sdk.controls["rid-1"].cancelled)
    assert reads == [False, True]
    assert harness.wait("rid-1", "unknown").failure_code == "NATIVE_TURN_CANCELLED"


class OfflineSteering:
    """The SDK session behind a steering channel: every send is enqueued and its delivery event observed at once."""

    def __init__(self):
        self.sent = []
        self.channel = None

    async def send(self, content, mode):
        self.sent.append((content, mode))
        identifier = f"OFFLINE-sdk-message-{len(self.sent)}"
        self.channel.delivered[identifier] = "queued"
        return identifier


def steer(harness, rid="rid-1"):
    """Open the running turn's steering channel, as the SDK executor does once its session is ready."""
    from shared_context.copilot_steering import SdkSteeringChannel

    control, session = harness.sdk.controls[rid], OfflineSteering()

    async def open_channel():
        session.channel = SdkSteeringChannel(control.stop_requested)
        session.channel.open(session)
        control.steering = session.channel

    harness.call(open_channel)
    return session


def append_command(active, append_id="append-1", content="OFFLINE appended text"):
    from shared_context.native_append_wire import NativeAppend

    return NativeAppend(request_id=active.request_id, conversation_id=active.conversation_id, append_id=append_id,
                        expected_submit_digest=active.submit_digest, content=content)


def read_append(harness, active, append_id="append-1"):
    from shared_context.native_append_wire import NativeAppendRead
    from shared_context.native_appends import read_append

    return harness.call(read_append, harness.runtime, NativeAppendRead(
        request_id=active.request_id, conversation_id=active.conversation_id, append_id=append_id))


def appends_key(rid="rid-1"):
    from shared_context.native_appends import _key

    return _key(rid)


def test_a_running_turn_with_steering_reads_its_appends_about_once_a_second(harness, monkeypatch):
    """Audit finding 14, second half: append delivery read ``appends:`` every 0.1 s while steering was ready."""
    harness.sdk.block = True
    harness.invoke(submit())
    harness.wait("rid-1", "started")
    steer(harness)
    counted = flaky_reads(monkeypatch, appends_key())
    time.sleep(2.2)
    reads = counted.reads
    harness.sdk.block = False
    harness.wait("rid-1", "completed")
    assert 1 <= reads <= 4, reads


def test_an_append_saved_here_is_delivered_at_once(harness, monkeypatch):
    """The poll is the fallback; an append this process saved wakes the delivery without waiting for it."""
    from shared_context import native_turns

    monkeypatch.setattr(native_turns, "CONTROL_POLL_SECONDS", 30.0)
    harness.sdk.block = True
    harness.invoke(submit())
    active = harness.wait("rid-1", "started")
    session = steer(harness)
    time.sleep(0.3)
    started = time.monotonic()
    assert harness.invoke(append_command(active)).status_code == 202
    until(lambda: session.sent, seconds=5)
    assert time.monotonic() - started < 2
    harness.sdk.block = False
    harness.wait("rid-1", "completed")
    assert session.sent == [("OFFLINE appended text", "enqueue")]
    assert read_append(harness, active).status == "applied"


def test_an_append_saved_by_another_process_is_delivered_within_the_poll(harness, monkeypatch):
    """No wake reaches this process for another process's append; the 1 s poll still finds it."""
    harness.sdk.block = True
    harness.invoke(submit())
    active = harness.wait("rid-1", "started")
    session = steer(harness)
    time.sleep(0.3)
    monkeypatch.setattr(harness.runtime, "_wake_control", lambda rid: None)
    started = time.monotonic()
    assert harness.invoke(append_command(active)).status_code == 202
    until(lambda: session.sent, seconds=5)
    assert time.monotonic() - started < 1.8
    harness.sdk.block = False
    harness.wait("rid-1", "completed")
    assert len(session.sent) == 1 and read_append(harness, active).status == "applied"


def test_appends_saved_together_are_delivered_one_after_another_without_waiting_for_the_poll(harness, monkeypatch):
    from shared_context import native_turns

    monkeypatch.setattr(native_turns, "CONTROL_POLL_SECONDS", 30.0)
    harness.sdk.block = True
    harness.invoke(submit())
    active = harness.wait("rid-1", "started")
    time.sleep(0.3)
    for index in range(3):
        assert harness.invoke(append_command(active, f"append-{index}", f"OFFLINE text {index}")).status_code == 202
    session = steer(harness)
    until(lambda: len(session.sent) == 3, seconds=5)
    harness.sdk.block = False
    harness.wait("rid-1", "completed")
    assert [content for content, _ in session.sent] == [f"OFFLINE text {index}" for index in range(3)]


def test_the_save_reads_each_result_part_back_once_and_writes_them_concurrently(harness, monkeypatch):
    """Audit finding 17: ``_immutable`` already reads every part back; the second full read is gone."""
    create, get = FoundryStateStore.create_item, FoundryStateStore.get_item
    seen = SimpleNamespace(reads=0, active=0, most=0)

    async def counted_create(store, key, value, **kwargs):
        if not key.startswith("text:"):
            return await create(store, key, value, **kwargs)
        seen.active += 1
        seen.most = max(seen.most, seen.active)
        try:
            await asyncio.sleep(0.02)
            return await create(store, key, value, **kwargs)
        finally:
            seen.active -= 1

    async def counted_get(store, key, **kwargs):
        seen.reads += key.startswith("text:")
        return await get(store, key, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "create_item", counted_create)
    monkeypatch.setattr(FoundryStateStore, "get_item", counted_get)
    harness.sdk.text = "完整答案🎯\n" * 60_000
    harness.invoke(submit())
    receipt = harness.wait("rid-1", "completed")
    assert receipt.result.text_parts > 4
    assert seen.reads == receipt.result.text_parts
    assert 1 < seen.most <= 4


def test_a_result_part_that_reads_back_different_is_never_completed(harness, monkeypatch):
    get = FoundryStateStore.get_item

    async def altered(store, key, **kwargs):
        item = await get(store, key, **kwargs)
        if key.startswith("text:") and key.endswith(":1") and item is not None:
            return SimpleNamespace(key=item.key, etag=item.etag, value={**item.value, "text": item.value["text"] + "!"})
        return item

    monkeypatch.setattr(FoundryStateStore, "get_item", altered)
    harness.sdk.text = "完整答案🎯\n" * 20_000
    harness.invoke(submit())
    receipt = harness.wait("rid-1", "unknown")
    assert receipt.failure_code == "NATIVE_STATE_INVALID" and receipt.result is None
    assert len(harness.sdk.calls) == 1


def test_an_unconfirmed_append_remember_write_is_retried_and_the_append_sent_once(harness, monkeypatch):
    """A 503 from append delivery is no answer: the turn keeps running. The entry was claimed (``sending`` + token)
    before the SDK send, so the retry never sends it again; it writes the unconfirmed SDK message ID once more."""
    set_item = FoundryStateStore.set_item
    lost = []

    async def unconfirmed(store, key, value, **kwargs):
        if (key == appends_key() and not lost
                and any(entry.get("sdk_message_id") for entry in value.get("entries", ()))):
            lost.append(key)
            raise ServiceResponseError("OFFLINE append remember write outcome unknown")
        return await set_item(store, key, value, **kwargs)

    harness.sdk.block = True
    harness.invoke(submit())
    active = harness.wait("rid-1", "started")
    session = steer(harness)
    monkeypatch.setattr(FoundryStateStore, "set_item", unconfirmed)
    assert harness.invoke(append_command(active)).status_code == 202
    until(lambda: lost and read_append(harness, active).status == "applied")
    assert not harness.sdk.controls["rid-1"].stop_requested()
    harness.sdk.block = False
    assert harness.wait("rid-1", "completed").result is not None
    assert len(session.sent) == 1 and len(harness.sdk.calls) == 1
    applied = read_append(harness, active)
    assert applied.status == "applied" and applied.sdk_message_id == "OFFLINE-sdk-message-1"


def test_an_unconfirmed_sdk_handoff_keeps_the_turn_and_never_resends_the_append(harness):
    """The SDK send itself raised (503 ``NATIVE_APPEND_UNKNOWN``): the turn goes on; the claimed entry is not sent
    again and ends ``unknown`` (``sdk_unconfirmed``), so the owner reads it instead of getting a duplicate."""
    harness.sdk.block = True
    harness.invoke(submit())
    active = harness.wait("rid-1", "started")
    session = steer(harness)
    attempts = []

    async def failing(content, mode):
        attempts.append(content)
        raise ConnectionError("OFFLINE SDK handoff outcome unknown")

    session.send = failing
    assert harness.invoke(append_command(active)).status_code == 202
    until(lambda: attempts)
    time.sleep(2.5)  # past the first retry
    assert not harness.sdk.controls["rid-1"].stop_requested()
    harness.sdk.block = False
    assert harness.wait("rid-1", "completed").result is not None
    assert len(attempts) == 1
    ended = read_append(harness, active)
    assert ended.status == "unknown" and ended.reason == "sdk_unconfirmed"


def test_a_running_turn_with_no_confirmed_control_read_for_the_bound_is_fenced(harness, monkeypatch, quick_control):
    """Audit finding 4's outer bound: a VM whose RID reads stay 5xx no longer runs its turn up to the SDK's 1-day
    timeout, past N (ADR-0009 §7a) when a new session may count it lost; it stops after half of N without one
    confirmed read and ends ``unknown`` once the store answers again."""
    from shared_context import native_turns
    from shared_context.native_left_receipts import STALL_SECONDS

    assert native_turns.CONTROL_UNCONFIRMED_FENCE_SECONDS < STALL_SECONDS
    offset = SimpleNamespace(seconds=0.0)
    monkeypatch.setattr(native_turns, "monotonic", lambda: time.monotonic() + offset.seconds)
    get = FoundryStateStore.get_item
    outage = SimpleNamespace(on=False, hits=0)

    async def read(store, key, **kwargs):
        if key == "rid:rid-1" and outage.on and not harness.sdk.controls["rid-1"].fenced:
            outage.hits += 1
            raise ServiceResponseError("OFFLINE store read outcome unknown")
        return await get(store, key, **kwargs)

    harness.sdk.block = True
    harness.invoke(submit())
    harness.wait("rid-1", "started")
    monkeypatch.setattr(FoundryStateStore, "get_item", read)
    outage.on = True
    until(lambda: outage.hits >= 2)
    offset.seconds = native_turns.CONTROL_UNCONFIRMED_FENCE_SECONDS - 30
    until(lambda: outage.hits >= 3)
    assert not harness.sdk.controls["rid-1"].stop_requested()
    offset.seconds = native_turns.CONTROL_UNCONFIRMED_FENCE_SECONDS + 1
    until(lambda: harness.sdk.controls["rid-1"].fenced)
    stopped = harness.wait("rid-1", "unknown")
    assert stopped.failure_code == "NATIVE_TURN_UNKNOWN" and stopped.result is None
    assert len(harness.sdk.calls) == 1



def skewed_clock(monkeypatch):
    """``native_turns.monotonic`` plus an offset the test moves forward (the observer's fence clock)."""
    from shared_context import native_turns

    offset = SimpleNamespace(seconds=0.0)
    monkeypatch.setattr(native_turns, "monotonic", lambda: time.monotonic() + offset.seconds)
    return offset


def test_reads_that_keep_succeeding_past_the_bound_never_fence_the_turn(harness, monkeypatch, quick_control):
    """The bound counts from the last confirmed read, not from the start: a turn whose reads keep succeeding runs on
    past it, and one later unconfirmed read is still no answer."""
    from shared_context import native_turns

    offset = skewed_clock(monkeypatch)
    harness.sdk.block = True
    harness.invoke(submit())
    harness.wait("rid-1", "started")
    flaky = flaky_reads(monkeypatch, "rid:rid-1")
    offset.seconds = native_turns.CONTROL_UNCONFIRMED_FENCE_SECONDS + 1
    reads = flaky.reads
    until(lambda: flaky.reads >= reads + 2)  # a read that started after the jump has been confirmed
    flaky.mode = "unknown"
    until(lambda: flaky.hits >= 2)
    flaky.mode = None
    assert not harness.sdk.controls["rid-1"].stop_requested()
    harness.sdk.block = False
    assert harness.wait("rid-1", "completed").result is not None
    assert len(harness.sdk.calls) == 1


def test_a_turn_waiting_for_the_slot_past_the_bound_is_not_fenced_and_runs_once(harness, monkeypatch, quick_control):
    """The slot waiter has started nothing: unconfirmed reads past the bound keep it waiting, and it runs once."""
    from shared_context import native_turns

    offset = skewed_clock(monkeypatch)
    accepted = accept_without_chain(harness)
    flaky = flaky_reads(monkeypatch, "rid:rid-1")

    async def run():
        lock = harness.runtime.execution_lock
        await lock.acquire()
        task = asyncio.create_task(harness.runtime.handle_turn(task_context(accepted, entry_mode="fresh")))
        try:
            async with asyncio.timeout(10):
                await asyncio.sleep(0.3)
                offset.seconds = native_turns.CONTROL_UNCONFIRMED_FENCE_SECONDS + 1
                flaky.mode = "unknown"
                while flaky.hits < 2:
                    await asyncio.sleep(0.01)
                flaky.mode = None
                assert not task.done()
        finally:
            lock.release()
        async with asyncio.timeout(10):
            return await task

    receipt = harness.call(run)
    assert receipt.status == "completed" and receipt.result is not None
    assert [call[0] for call in harness.sdk.calls] == ["rid-1"]

def lose_the_claim_reply(monkeypatch, append_id="append-0", landed=None):
    """The claim write of ``append_id`` lands, but its reply and the read-back fail: ``_update`` answers 503 although
    the stored entry is ``sending`` under this process's token. ``landed`` runs once the write is in the store."""
    set_item, get_item = FoundryStateStore.set_item, FoundryStateStore.get_item
    state = SimpleNamespace(lost=False, reread=False)

    async def write(store, key, value, **kwargs):
        result = await set_item(store, key, value, **kwargs)
        if key == appends_key() and not state.lost and any(
                entry["command"]["append_id"] == append_id and entry["status"] == "sending"
                for entry in value.get("entries", ())):
            state.lost = state.reread = True
            if landed is not None:
                landed()
            raise ServiceResponseError("OFFLINE append claim reply lost")
        return result

    async def read(store, key, **kwargs):
        if key == appends_key() and state.reread:
            state.reread = False
            raise ServiceResponseError("OFFLINE append read outcome unknown")
        return await get_item(store, key, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "set_item", write)
    monkeypatch.setattr(FoundryStateStore, "get_item", read)
    return state


def test_an_append_whose_claim_landed_unconfirmed_is_sent_once_and_before_later_appends(
        harness, monkeypatch, quick_control):
    """The claim write landed but ``_update`` answered 503: the entry is ``sending`` under a token this process still
    holds as unsent. It is sent once, before the append saved after it (the model never sees #2 without #1)."""
    harness.sdk.block = True
    harness.invoke(submit())
    active = harness.wait("rid-1", "started")
    for index in range(2):
        assert harness.invoke(append_command(active, f"append-{index}", f"OFFLINE text {index}")).status_code == 202
    lost = lose_the_claim_reply(monkeypatch)
    session = steer(harness)
    until(lambda: read_append(harness, active, "append-1").status == "applied")
    assert lost.lost and not harness.sdk.controls["rid-1"].stop_requested()
    harness.sdk.block = False
    assert harness.wait("rid-1", "completed").result is not None
    assert [content for content, _ in session.sent] == ["OFFLINE text 0", "OFFLINE text 1"]
    assert [read_append(harness, active, f"append-{index}").status for index in range(2)] == ["applied", "applied"]
    assert len(harness.sdk.calls) == 1


def test_an_append_claimed_here_but_never_sent_reads_not_applied_when_the_turn_ends(harness, monkeypatch):
    """The SDK turn ends right after a claim whose ``_update`` answered 503: this process never handed the append to the
    SDK, so it reads ``not_applied`` (the owner may send it again), not ``unknown``."""
    harness.sdk.block = True
    harness.invoke(submit())
    active = harness.wait("rid-1", "started")
    session = steer(harness)

    def turn_ends():
        session.channel.close()
        harness.sdk.block = False

    lost = lose_the_claim_reply(monkeypatch, "append-1", landed=turn_ends)
    assert harness.invoke(append_command(active)).status_code == 202
    assert harness.wait("rid-1", "completed").result is not None
    assert lost.lost and session.sent == []
    ended = read_append(harness, active)
    assert (ended.status, ended.reason) == ("not_applied", "turn_finished")


def turn_tail(harness):
    """The turn's tail: its last SDK round is over (the channel closed, the reply shown) while the executor still stops
    its SDK (``block``) and has not saved."""
    harness.sdk.block = True
    harness.invoke(submit())
    active = harness.wait("rid-1", "started")
    session = steer(harness)
    harness.call(session.channel.close)
    return active, session


def test_an_append_one_second_after_the_reply_is_answered_at_once_not_after_the_stop_and_save(harness):
    active, session = turn_tail(harness)
    time.sleep(1)
    response = harness.invoke(append_command(active))
    assert response.status_code == 202, response.text
    assert (response.json()["status"], response.json().get("reason")) == ("not_applied", "turn_finished")
    assert harness.call(harness.runtime.read, "rid-1").status == "started", "answered while the SDK still stops"
    harness.sdk.block = False
    harness.wait("rid-1", "completed")
    ended = read_append(harness, active)
    assert (ended.status, ended.reason) == ("not_applied", "turn_finished") and session.sent == []


def test_an_append_accepted_before_the_close_ends_with_the_one_after_it_and_keeps_its_place(harness):
    from shared_context.copilot_steering import SdkSteeringChannel

    harness.sdk.block = True
    harness.invoke(submit())
    active = harness.wait("rid-1", "started")
    first = harness.invoke(append_command(active, "append-1", "A1"))
    assert first.json()["status"] == "accepted", first.text  # no SDK channel yet: kept, never delivered
    control = harness.sdk.controls["rid-1"]

    async def closed_channel():  # the last SDK round ended before A1 was handed over
        control.steering = SdkSteeringChannel(control.stop_requested)
        control.steering.close()

    harness.call(closed_channel)
    second = harness.invoke(append_command(active, "append-2", "A2"))
    assert (second.json()["status"], second.json()["sequence"]) == ("not_applied", 2), second.text
    earlier = read_append(harness, active, "append-1")
    assert (earlier.status, earlier.reason, earlier.sequence) == ("not_applied", "turn_finished", 1)
    assert harness.call(harness.runtime.read, "rid-1").status == "started", "both settled before the stop and save"
    harness.sdk.block = False
    harness.wait("rid-1", "completed")
    assert [read_append(harness, active, f"append-{n}").status for n in (1, 2)] == ["not_applied"] * 2


def test_a_crash_between_the_reply_and_the_save_keeps_the_append_resendable_and_never_reruns(local_env, monkeypatch):
    from shared_context import native_appends

    sdk, settle = OfflineSdk(), native_appends.settle_appends
    first = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first)) as client:
        active, _ = turn_tail(Harness(first, sdk, client))
        assert Harness(first, sdk, client).invoke(append_command(active)).status_code == 202

        async def died(*_args, **_kwargs):  # the process dies here: nothing after the reply is written, not this settle
            return None

        monkeypatch.setattr(native_appends, "settle_appends", died)
    monkeypatch.setattr(native_appends, "settle_appends", settle)
    restored_sdk = OfflineSdk()
    restored = NativeTurnRuntime(IDENTITY, restored_sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=restored)) as client:
        again = Harness(restored, restored_sdk, client)
        resumed = again.wait("rid-1", "unknown")
        assert resumed.result is None and len(sdk.calls) == 1 and not restored_sdk.calls
        ended = read_append(again, active)
        # Final before the crash, so the owner may still send it again as a new turn (never ``unknown``).
        assert (ended.status, ended.reason) == ("not_applied", "turn_finished")


def test_a_new_turn_sent_in_the_tail_is_accepted_at_once_and_runs_after_the_first(harness):
    active, _ = turn_tail(harness)
    response = harness.invoke(submit("rid-2", active.conversation_id, text="Next message"))
    assert response.status_code == 202 and response.json()["status"] == "accepted", response.text
    assert harness.call(harness.runtime.read, "rid-1").status == "started"
    harness.sdk.block = False
    first, second = harness.wait("rid-1", "completed"), harness.wait("rid-2", "completed")
    assert (first.turn_number, second.turn_number) == (1, 2)
    assert [call[0] for call in harness.sdk.calls] == ["rid-1", "rid-2"] and harness.sdk.calls[1][2] is not None
    assert harness.sdk.max_active == 1
