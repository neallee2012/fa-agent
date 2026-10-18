"""ADR-0009 §5 and the lifecycle table's "Hosted 標記 store": the expiring marker store beside ``fa/v1/turns``.

Real core local task/store and host, as in ``test_native_turns``; only SDK/model I/O is synthetic.
"""

import asyncio
from types import SimpleNamespace

import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore, FoundryStorageNotFoundError
from azure.ai.agentserver.core.tasks import TaskContext
from azure.core.exceptions import ServiceResponseError
from starlette.testclient import TestClient

from shared_context import behavior_native_turn_api, native_checkpoint, native_turns
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import (
    NATIVE_LOCATOR_BINDING_ROW, NATIVE_LOCATOR_HEADER, NATIVE_STATE_PROFILE, NATIVE_TURN_HEADER, NativeRead,
    canonical_bytes,
)
from shared_context.errors import Problem
from shared_context.native_checkpoint import open_durable_store
from shared_context.native_markers import EARLIER_VERSION_UNFINISHED, MARKER_TTL_SECONDS, MARKERS_STORE
from shared_context.native_turns import TURNS_STORE, NativeTaskInput, NativeTurnRuntime
from test_native_turn_wire import submit
from test_native_turns import (  # noqa: F401 - fixtures
    IDENTITY, Harness, OfflineSdk, accept_without_chain, harness, local_env, native_retry_policy, seed_started,
    task_context,
)


async def stored(key):
    """The marker as the store holds it; None when the store or the item does not exist."""
    store = FoundryStateStore(MARKERS_STORE)
    try:
        assert (await store.get()).item_ttl_seconds == MARKER_TTL_SECONDS
        item = await store.get_item(key)
    except FoundryStorageNotFoundError:
        return None
    finally:
        await store.aclose()
    return None if item is None else item.value


def test_a_started_turns_shutdown_deferral_leaves_its_marker_and_the_restart_still_reenters_once(local_env):
    sdk = OfflineSdk()
    sdk.block = True
    first = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first)) as client:
        h = Harness(first, sdk, client)
        h.invoke(submit())
        h.wait("rid-1", "started")
    token = asyncio.run(first._rid("rid-1")).value.run_token
    marker = asyncio.run(stored(f"deferred:{token}"))
    assert marker is not None and marker["schema_version"] == "fa-native-deferred-v1"
    assert (marker["run_token"], marker["request_id"]) == (token, "rid-1")
    assert marker["expires_at"] - marker["at"] == MARKER_TTL_SECONDS
    restored_sdk = OfflineSdk()
    restored = NativeTurnRuntime(IDENTITY, restored_sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=restored)) as client:
        assert Harness(restored, restored_sdk, client).wait("rid-1", "unknown").result is None
        assert not restored_sdk.calls


@pytest.mark.parametrize("failure", ["error", "slow"])
def test_a_failed_or_slow_marker_write_never_holds_up_the_deferral(harness, monkeypatch, failure):
    _, started = seed_started(harness)
    create = FoundryStateStore.create_item

    async def broken(store, key, value, **kwargs):
        if not key.startswith("deferred:"):
            return await create(store, key, value, **kwargs)
        if failure == "slow":
            await asyncio.sleep(60)
        raise ServiceResponseError("OFFLINE marker store unavailable")

    monkeypatch.setattr(FoundryStateStore, "create_item", broken)
    monkeypatch.setattr(native_turns, "_MARKER_WRITE_SECONDS", 0.05)
    ctx = task_context(started)
    ctx.shutdown.set()
    exited = harness.call(harness.runtime._exit_for_recovery, ctx, started)
    assert type(exited).__name__ == "_ExitForRecovery"
    assert harness.call(harness.runtime._rid, "rid-1").value == started.value
    assert harness.call(stored, "deferred:OFFLINE-run") is None


def test_no_marker_without_a_started_run_in_hand_or_without_a_deferral(harness):
    """Inert for the existing flow: a completed turn, an accepted RID's deferral and a deferral before the RID is
    read write nothing, so the store is never even created."""
    harness.invoke(submit("rid-done"))
    harness.wait("rid-done", "completed")
    accepted, _ = seed_started(harness)
    ctx = task_context(accepted)
    ctx.shutdown.set()
    assert type(harness.call(harness.runtime.handle_turn, ctx)).__name__ == "_ExitForRecovery"
    with pytest.raises(FoundryStorageNotFoundError):
        harness.call(FoundryStateStore(MARKERS_STORE).get)


def crashes(harness):
    return [(crash.request_id, crash.conversation_id) for crash in harness.call(harness.runtime.session_state).recent_crashes]


def test_a_recovered_reentry_without_a_deferral_counts_one_crash_and_a_same_rid_retry_does_not_count_again(harness):
    accepted, started = seed_started(harness)
    assert harness.call(harness.runtime.handle_turn, task_context(accepted)).status == "unknown"
    cid = started.value.receipt.conversation_id
    assert crashes(harness) == [("rid-1", cid)]
    harness.call(harness.runtime._record_crash, started.value.receipt)  # the same RID failing again elsewhere
    assert crashes(harness) == [("rid-1", cid)]
    assert not harness.sdk.calls


def test_a_recovered_reentry_after_a_graceful_deferral_is_no_crash(harness):
    accepted, started = seed_started(harness)
    harness.call(harness.runtime._mark_deferred, "rid-1", started.value.run_token)
    assert harness.call(harness.runtime.handle_turn, task_context(accepted)).status == "unknown"
    assert crashes(harness) == []


@pytest.mark.parametrize("entry_mode, cancelled", [
    ("resumed", False), ("fresh", False), ("recovered", True), ("recovered", False)])
def test_a_forced_end_that_comes_back_suspended_or_cancelled_is_no_crash(harness, entry_mode, cancelled):
    """A forced end in the grace period leaves the chain ``suspended`` (no recovered re-entry); a cancel is the owner's.
    The last case is the control: the same started run re-entered recovered and uncancelled counts one."""
    accepted, started = seed_started(harness)
    ctx = task_context(accepted, entry_mode=entry_mode)
    ctx.cancel_requested = cancelled
    harness.call(harness.runtime.handle_turn, ctx)
    counted = entry_mode == "recovered" and not cancelled
    assert crashes(harness) == ([("rid-1", started.value.receipt.conversation_id)] if counted else [])


def test_a_ninth_crash_evicts_the_oldest(harness):
    for n in range(9):
        harness.call(harness.runtime._record_crash, SimpleNamespace(request_id=f"rid-{n}", conversation_id="cid-x"))
    assert [rid for rid, _ in crashes(harness)] == [f"rid-{n}" for n in range(1, 9)]


def test_a_checkpoint_the_turn_cannot_load_counts_one_crash(harness, monkeypatch):
    harness.invoke(submit())
    cid = harness.wait("rid-1", "completed").conversation_id

    async def refused(*args, **kwargs):
        raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "OFFLINE checkpoint codec refused")

    monkeypatch.setattr(native_checkpoint, "load_checkpoint", refused)
    harness.invoke(submit("rid-2", cid))
    assert harness.wait("rid-2", "unknown").failure_code == "SDK_STATE_UNSUPPORTED_FORMAT"
    assert crashes(harness) == [("rid-2", cid)]
    assert len(harness.sdk.calls) == 1


def test_no_crashes_read_back_as_an_empty_session_state_field(harness):
    harness.invoke(submit())
    harness.wait("rid-1", "completed")
    assert "recent_crashes" not in harness.call(harness.runtime.session_state).model_dump(mode="json")


async def rid_item(rid="rid-1"):
    async with await open_durable_store(TURNS_STORE, create=False) as store:
        item = await store.get_item(f"rid:{rid}")
    return item.etag, canonical_bytes(item.value)


def refuse(harness, accepted, entry, *, version="2", entry_mode="fresh"):
    ctx = TaskContext(task_id=IDENTITY.agent_id, session_id=IDENTITY.session_id, input_id="rid-1",
                      entry_mode=entry_mode, input=NativeTaskInput(
                          request_id="rid-1", accepted_etag=accepted.etag, call_id=accepted.value.call_id,
                          execution_version=version))
    with pytest.raises(Problem) as refused:
        harness.call(getattr(harness.runtime, entry), ctx)
    assert refused.value.body["error"]["code"] == "NATIVE_SESSION_CHANGED"


@pytest.mark.parametrize("entry", ["handle_turn", "_enqueue_attempt"])
def test_an_input_refused_by_version_is_marked_once_and_reads_final_with_the_notice_and_the_receipt_unchanged(
        harness, entry):
    accepted = accept_without_chain(harness)
    before = harness.call(rid_item)
    first = harness.invoke(NativeRead(request_id="rid-1")).json()
    assert first["status"] == "accepted" and "environment_notice" not in first

    refuse(harness, accepted, entry)
    marker = harness.call(stored, "refused:rid-1")
    assert (marker["schema_version"], marker["request_id"], marker["execution_version"],
            marker["hosted_agent_version"]) == ("fa-native-refused-v1", "rid-1", "2", IDENTITY.agent_version)
    refuse(harness, accepted, entry)
    assert harness.call(stored, "refused:rid-1") == marker
    assert harness.call(rid_item) == before
    # The API's §7a (e) shape, so the workbench and the Teams relay end it with "請用新訊息再送一次" (never queued).
    read = harness.invoke(NativeRead(request_id="rid-1")).json()
    assert (read["status"], read["failure_code"], read["environment_notice"]) == (
        "unknown", EARLIER_VERSION_UNFINISHED, {"kind": "earlier_version_unfinished"})
    assert EARLIER_VERSION_UNFINISHED == behavior_native_turn_api.EARLIER_VERSION_UNFINISHED
    assert harness.call(rid_item) == before and not harness.sdk.calls


def test_a_refused_rid_leaves_its_conversation_queue_so_later_messages_do_not_wait_behind_it(harness):
    from shared_context.native_left_receipts import _head, _queue, enqueue

    accepted = accept_without_chain(harness)
    cid = accepted.value.receipt.conversation_id
    harness.call(enqueue, harness.runtime, accepted)
    queue, etag = harness.call(_queue, harness.runtime, cid)
    assert queue.request_ids == ("rid-1",) and harness.call(_head, harness.runtime, cid, queue, etag) == "rid-1"
    refuse(harness, accepted, "handle_turn")
    queue, etag = harness.call(_queue, harness.runtime, cid)
    assert queue.request_ids == () and harness.call(_head, harness.runtime, cid, queue, etag) is None
    assert not harness.sdk.calls


def test_a_missing_markers_store_is_read_once_per_process_until_a_refusal_creates_it(harness, monkeypatch):
    accepted = accept_without_chain(harness)
    opened = []
    original_open = native_checkpoint.open_durable_store

    async def counting(name, **kwargs):
        if name == MARKERS_STORE:
            opened.append(kwargs["create"])
        return await original_open(name, **kwargs)

    monkeypatch.setattr(native_checkpoint, "open_durable_store", counting)
    for _ in range(3):
        assert harness.invoke(NativeRead(request_id="rid-1")).json()["status"] == "accepted"
    assert opened == [False]
    refuse(harness, accepted, "handle_turn")
    assert harness.invoke(NativeRead(request_id="rid-1")).json()["status"] == "unknown"
    assert opened == [False, True]


async def agent_item():
    async with await open_durable_store(TURNS_STORE, create=False) as store:
        return canonical_bytes((await store.get_item("agent")).value)


def test_both_replaced_refusals_name_the_current_sid_and_leave_the_agent_record_as_it_was(local_env, monkeypatch):
    sdk = OfflineSdk()
    first = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first)) as client:
        Harness(first, sdk, client).invoke(submit())
        Harness(first, sdk, client).wait("rid-1", "completed")
    before = asyncio.run(agent_item())
    other = IDENTITY.model_copy(update={"session_id": "OFFLINE-other-session"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", other.session_id)
    runtime = NativeTurnRuntime(other, OfflineSdk())
    with TestClient(create_host(SimpleNamespace(identity=other), native_runtime=runtime)) as client:
        with pytest.raises(Problem) as admission:
            client.portal.call(runtime.submit, submit("rid-new"))
        read = client.post("/invocations", json=NativeRead(request_id="rid-1").model_dump(mode="json"), headers={
            NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE, NATIVE_LOCATOR_HEADER: NATIVE_LOCATOR_BINDING_ROW,
            "x-agent-session-id": other.session_id, "x-agent-invocation-id": "platform-offline-read",
            "x-agent-foundry-call-id": "OFFLINE-read-call"})
    assert admission.value.status == 409
    assert admission.value.body["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert admission.value.body["error"]["current_hosted_session_id"] == IDENTITY.session_id
    assert read.status_code == 409
    body = read.json()["error"]
    assert (body["code"], body["reason"], body["current_hosted_session_id"]) == (
        "NATIVE_SESSION_CHANGED", "replaced", IDENTITY.session_id)
    after = asyncio.run(agent_item())
    assert after == before and b"current_hosted_session_id" not in after


def test_a_receipt_of_another_sid_on_the_current_session_names_no_sid_to_resend_to(harness):
    """Only the receipt's SID differs: the current SID is this session, so naming it would send a 4c resend back here."""
    accepted = accept_without_chain(harness)
    receipt = accepted.value.receipt.model_copy(update={"hosted_session_id": "OFFLINE-other-session"})
    with pytest.raises(Problem) as changed:
        harness.call(lambda: harness.runtime._require_original_session(receipt=receipt))
    assert changed.value.status == 409 and changed.value.body["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert "current_hosted_session_id" not in changed.value.body["error"]


def final(response):
    body = response.json()
    assert (body["status"], body["failure_code"], body["environment_notice"]) == (
        "unknown", EARLIER_VERSION_UNFINISHED, {"kind": "earlier_version_unfinished"}), response.text


def test_a_resend_of_a_refused_rid_reads_final_and_never_runs(harness):
    """Review P2 on #889: the owner was told "請用新訊息再送一次", so the same RID sent again (relay resend, API retry)
    is never started at the current version; it answers the same final view and the receipt keeps its bytes."""
    accepted = accept_without_chain(harness)
    refuse(harness, accepted, "handle_turn")
    before = harness.call(rid_item)
    resent = harness.invoke(submit())
    assert resent.status_code < 300, resent.text
    final(resent)
    # The chain refuses it too at the current version (an input that reached the chain before the resend's answer).
    refuse(harness, accepted, "handle_turn", version=IDENTITY.agent_version)
    final(harness.invoke(NativeRead(request_id="rid-1")))
    assert harness.call(rid_item) == before and not harness.sdk.calls


def test_a_wait_task_rerun_after_a_recycle_never_runs_a_refused_rid(harness):
    """Review P2 on #889: a wait task recovered after a VM recycle puts the RID back on the chain at the current
    version; the chain refuses it, so the SDK is never called."""
    from shared_context.native_left_receipts import _queue, _wait_input, enqueue, wait_turn

    accepted = accept_without_chain(harness)
    harness.call(enqueue, harness.runtime, accepted)
    refuse(harness, accepted, "handle_turn")
    before = harness.call(rid_item)
    started, chain = [], harness.runtime.chain

    async def captured(**kwargs):
        started.append(kwargs["input"])

    harness.runtime.chain = SimpleNamespace(start=captured)
    try:
        ctx = TaskContext(task_id="fa-left-wait-rerun", session_id=IDENTITY.session_id, input_id="rid-1",
                          entry_mode="recovered", input=_wait_input(harness.runtime, accepted))
        assert harness.call(wait_turn, harness.runtime, ctx) == "rid-1"
    finally:
        harness.runtime.chain = chain
    assert [value.execution_version for value in started] == [IDENTITY.agent_version]
    refuse(harness, accepted, "handle_turn", version=started[0].execution_version, entry_mode="recovered")
    final(harness.invoke(NativeRead(request_id="rid-1")))
    queue, _ = harness.call(_queue, harness.runtime, accepted.value.receipt.conversation_id)
    assert queue.request_ids == () and harness.call(rid_item) == before and not harness.sdk.calls


def test_a_miss_cached_while_the_first_refusal_waits_to_create_the_store_never_resurfaces(harness, monkeypatch):
    """Review P3 on #889: a read gets its 404 while the first refusal's create waits for the lock; once the store client
    is evicted (``STORE_CLIENTS``), that miss must not hide ``refused:<RID>``."""
    import asyncio

    accepted = accept_without_chain(harness)
    original_open = native_checkpoint.open_durable_store

    async def slow_miss(name, **kwargs):
        if name == MARKERS_STORE and not kwargs["create"]:
            await asyncio.sleep(0.1)  # the refusal's create is waiting for the lock meanwhile
        return await original_open(name, **kwargs)

    monkeypatch.setattr(native_checkpoint, "open_durable_store", slow_miss)
    runtime = harness.runtime

    async def race():
        reader = asyncio.create_task(runtime._refused("rid-1"))
        await asyncio.sleep(0.02)
        await runtime._mark_refused(SimpleNamespace(request_id="rid-1", execution_version="2"))
        assert await reader is False
        await runtime._clients.stores.pop(MARKERS_STORE).aclose()  # evicted
        return await runtime._refused("rid-1")

    assert harness.call(race) is True
    final(harness.invoke(NativeRead(request_id="rid-1")))
    assert accepted.value.receipt.status == "accepted" and not harness.sdk.calls


def at_current_version(accepted, *, entry_mode="fresh"):
    return TaskContext(task_id=IDENTITY.agent_id, session_id=IDENTITY.session_id, input_id="rid-1",
                       entry_mode=entry_mode, input=NativeTaskInput(
                           request_id="rid-1", accepted_etag=accepted.etag, call_id=accepted.value.call_id,
                           execution_version=IDENTITY.agent_version))


def test_an_unconfirmed_markers_read_at_the_gate_retries_later_and_never_runs_a_refused_rid(harness, monkeypatch):
    """Review P2 on #889: the owner already read "請用新訊息再送一次"; a wait-task re-run or recover_owned that cannot
    confirm ``refused:<RID>`` must not run it (the owner's resend would run it twice). It is NativePostTurnPending,
    which the chain's retry policy retries; the retry reads the marker and refuses it."""
    accepted = accept_without_chain(harness)
    refuse(harness, accepted, "handle_turn")
    get, failing = FoundryStateStore.get_item, [True]

    async def unconfirmed(store, key, **kwargs):
        if failing[0] and key.startswith("refused:"):
            raise ServiceResponseError("OFFLINE marker store unavailable")
        return await get(store, key, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "get_item", unconfirmed)
    for entry_mode in ("fresh", "recovered"):
        with pytest.raises(native_turns.NativePostTurnPending):
            harness.call(harness.runtime.handle_turn, at_current_version(accepted, entry_mode=entry_mode))
    assert native_turns.NativePostTurnPending in harness.runtime._queue_retry.retry_on and not harness.sdk.calls
    failing[0] = False
    refuse(harness, accepted, "handle_turn", version=IDENTITY.agent_version, entry_mode="recovered")
    final(harness.invoke(NativeRead(request_id="rid-1")))
    assert not harness.sdk.calls


@pytest.mark.parametrize("cached", [False, True])
def test_a_missing_markers_store_at_the_gate_is_no_refusal_and_the_rid_runs(harness, cached):
    accepted = accept_without_chain(harness)
    if cached:
        assert harness.invoke(NativeRead(request_id="rid-1")).json()["status"] == "accepted"
        assert MARKERS_STORE in harness.runtime._clients.missing
    assert harness.call(harness.runtime.handle_turn, at_current_version(accepted)).status == "completed"
    assert len(harness.sdk.calls) == 1
    with pytest.raises(FoundryStorageNotFoundError):
        harness.call(FoundryStateStore(MARKERS_STORE).get)


def test_an_adopted_rid_another_process_refused_is_refused_at_the_gate_despite_a_cached_miss(harness):
    """Review P2 on #889: ``adopt`` reads leniently, so a store error there adopts a refused RID; the gate then looks
    again (``fresh``) instead of trusting this process's cached miss, so it is refused, not run."""
    accepted = accept_without_chain(harness)
    adopted, _ = harness.call(harness.runtime._cas, accepted,
                              native_turns._changed(accepted.value, adopted_from="OFFLINE-retired-sid"))
    assert harness.invoke(NativeRead(request_id="rid-1")).json()["status"] == "accepted"
    assert MARKERS_STORE in harness.runtime._clients.missing
    other = NativeTurnRuntime(IDENTITY, OfflineSdk())  # another process: its own store clients
    harness.call(other._mark_refused, SimpleNamespace(request_id="rid-1", execution_version="2"))
    refuse(harness, adopted, "handle_turn", version=IDENTITY.agent_version)
    assert not harness.sdk.calls

@pytest.mark.parametrize("entry", ["handle_turn", "_enqueue_attempt"])
def test_a_session_that_no_longer_holds_the_receipt_refuses_its_input_without_marking_it(harness, entry):
    """Review P3 on #889: another session adopted the RID (its receipt names that SID) and runs it; this session's
    refusal by version must not write ``refused:<RID>``, or reads would say "請用新訊息再送一次" while it runs."""
    accepted = accept_without_chain(harness)
    moved, _ = harness.call(harness.runtime._cas, accepted, native_turns._changed(
        accepted.value, receipt=native_turns._changed(accepted.value.receipt, hosted_session_id="OFFLINE-new-sid")))
    refuse(harness, moved, entry)
    assert harness.call(stored, "refused:rid-1") is None and not harness.sdk.calls