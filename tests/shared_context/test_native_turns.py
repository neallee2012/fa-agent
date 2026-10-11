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
