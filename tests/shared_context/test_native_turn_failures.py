"""Original start and RID write failures must not become successful pending work."""

import asyncio
from functools import partial
from types import SimpleNamespace

import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.ai.agentserver.core.tasks import TaskContext
from azure.core.exceptions import ServiceResponseError
from starlette.testclient import TestClient

from shared_context import native_continuations
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeCancel, NativeRead
from shared_context.errors import Problem
from shared_context.native_turns import NativePostTurnPending, NativeTaskInput, NativeTurnRuntime, _changed
from test_native_turn_wire import agent_receive, submit
from test_native_turns import (
    Harness, IDENTITY, OfflineSdk, accept_without_chain, harness, local_env, native_retry_policy, seed_started,
)


@pytest.mark.parametrize("lease_applied", [False, True], ids=["not-applied", "reply-lost"])
def test_official_start_transport_failure_closes_only_the_unstarted_rid(harness, monkeypatch, caplog, lease_applied):
    from azure.ai.agentserver.core.tasks._client import TransportClassifiedError
    from azure.ai.agentserver.core.tasks._local_provider import LocalFileTaskProvider

    runs, attempts = [], []
    start = harness.runtime.chain.start

    async def capture_run(**kwargs):
        run = await start(**kwargs)
        runs.append(run)
        return run

    monkeypatch.setattr(harness.runtime.chain, "start", capture_run)
    assert harness.invoke(submit()).status_code == 202
    harness.call(runs[0].result)
    completed = harness.call(harness.runtime.read, "rid-1")
    assert completed.status == "completed"
    update = LocalFileTaskProvider.update

    async def fail_lease(provider, task_id, patch):
        if task_id == IDENTITY.agent_id and patch.status == "in_progress":
            original = await provider.get(task_id)
            assert original.status == "suspended" and original.lease is None
            assert original.payload["input"]["request_id"] == "rid-2"
            assert original.payload["last_input_id"] == "rid-2"
            attempts.append(original.payload["input"])
            if lease_applied:
                await update(provider, task_id, patch)
            raise TransportClassifiedError(
                status=502, classification="transient", message="PRIVATE-service-error",
                request_id="offline-task-request", body_prefix="PRIVATE-response-body")
        return await update(provider, task_id, patch)

    monkeypatch.setattr(LocalFileTaskProvider, "update", fail_lease)
    request = submit("rid-2", completed.conversation_id)
    response = harness.invoke(request)
    assert response.status_code == 202, response.text
    closed = harness.call(harness.runtime.read, "rid-2")
    assert closed.status == "unknown" and closed.failure_code == "NATIVE_TURN_NOT_STARTED"
    assert closed.turn_number is None and closed.result is None and closed.usage is None
    assert closed.command == request.command and closed.capture == request.capture
    assert harness.invoke(request).json() == closed.model_dump(mode="json")
    assert harness.call(harness.runtime.read, "rid-1") == completed
    assert len(attempts) == 1 and len(runs) == 1 and len(harness.sdk.calls) == 1
    assert "PRIVATE-" not in caplog.text
    if not lease_applied:
        monkeypatch.setattr(LocalFileTaskProvider, "update", update)

        async def late_handler():
            run = await start(task_id=IDENTITY.agent_id, input_id="rid-2", if_last_input_id="rid-2",
                              input=NativeTaskInput.model_validate(attempts[0]))
            return await run.result()

        harness.call(late_handler)
        assert harness.call(harness.runtime.read, "rid-2") == closed
        assert len(harness.sdk.calls) == 1


@pytest.mark.parametrize("winner", ["started", "completed", "unknown"])
def test_start_error_never_overwrites_a_started_or_terminal_winner(harness, monkeypatch, winner):
    from azure.ai.agentserver.core.tasks._client import TransportClassifiedError

    entered, runs = asyncio.Event(), []
    sdk = harness.runtime.executor
    harness.sdk.block = winner == "started"

    async def execute(*args):
        entered.set()
        if winner == "unknown":
            raise OSError("OFFLINE original execution outcome is unknown")
        return await sdk(*args)

    harness.runtime.executor = execute
    start = harness.runtime.chain.start

    async def response_lost(**kwargs):
        run = await start(**kwargs)
        runs.append(run)
        if winner == "started":
            await entered.wait()
        else:
            await run.result()
        raise TransportClassifiedError(status=502, classification="transient", message="OFFLINE response lost")

    monkeypatch.setattr(harness.runtime.chain, "start", response_lost)
    response = harness.invoke(submit())
    assert response.status_code == 202 and response.json()["status"] == winner, response.text
    original = harness.call(harness.runtime.read, "rid-1")
    assert original.failure_code != "NATIVE_TURN_NOT_STARTED" and original.turn_number == 1
    assert len(runs) == 1
    harness.sdk.block = False
    harness.call(runs[0].result)
    retained = harness.call(harness.runtime.read, "rid-1")
    assert harness.invoke(submit()).json() == retained.model_dump(mode="json")
    assert len(runs) == 1 and len(harness.sdk.calls) == (0 if winner == "unknown" else 1)


def test_deferred_enqueue_transport_failure_does_not_leave_an_unowned_accepted_rid(harness, monkeypatch):
    from azure.ai.agentserver.core.tasks import SteeringQueueFull
    from azure.ai.agentserver.core.tasks._client import TransportClassifiedError
    from azure.ai.agentserver.core.tasks._local_provider import LocalFileTaskProvider

    attempts = []

    async def full(**kwargs):
        raise SteeringQueueFull(IDENTITY.agent_id, 10, 10)

    async def not_started(_provider, request):
        assert request.source["name"] == "fa-native-agent-enqueue-v1"
        attempts.append(request.id)
        raise TransportClassifiedError(status=502, classification="transient", message="OFFLINE enqueue start failed")

    monkeypatch.setattr(harness.runtime.chain, "start", full)
    monkeypatch.setattr(LocalFileTaskProvider, "create", not_started)
    request = agent_receive("original-peer")
    response = harness.invoke(request)
    assert response.status_code == 202, response.text
    receipt = harness.call(harness.runtime.read, request.command.request_id)
    assert receipt.status == "unknown" and receipt.failure_code == "NATIVE_TURN_NOT_STARTED"
    assert receipt.turn_number is None and receipt.source == request.source
    assert harness.invoke(request).json() == receipt.model_dump(mode="json")
    assert len(attempts) == 1 and not harness.sdk.calls


@pytest.mark.parametrize("transition", ["started", "completed"])
def test_not_applied_cas_is_unknown_not_successful_pending_task(local_env, monkeypatch, transition):
    write = FoundryStateStore.set_item
    attempts = []

    async def not_applied(store, key, value, **options):
        if key == "rid:rid-1" and value.get("receipt", {}).get("status") == transition:
            attempts.append(transition)
            if len(attempts) == 1:
                raise PermissionError(13, "OFFLINE deterministic not-applied write")
        return await write(store, key, value, **options)

    monkeypatch.setattr(FoundryStateStore, "set_item", not_applied)
    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        response = harness.invoke(submit())
        assert response.status_code == 202

        async def after_chain():
            # Await this original task result; no polling or new input to start another execution.
            active = await runtime.chain.get_active_run(IDENTITY.agent_id, "rid-1")
            if active is not None:
                await active.result()
            return await runtime.read("rid-1")

        receipt = client.portal.call(after_chain)
        assert receipt.status == ("unknown" if transition == "started" else "completed")
        if transition == "started":
            assert receipt.failure_code == "NATIVE_STATE_STORE_UNKNOWN"
            assert not sdk.calls
        else:
            assert receipt.result is not None and len(sdk.calls) == 1
        retained = receipt
        assert harness.invoke(submit()).status_code == 202
        assert harness.call(runtime.read, "rid-1") == retained
        assert len(sdk.calls) == (0 if transition == "started" else 1)
        assert attempts == ([transition] if transition == "started" else [transition, transition])


@pytest.mark.parametrize("race", ["applied-ack-lost", "other-writer"])
def test_started_cas_retains_original_applied_token_or_winning_receipt(local_env, monkeypatch, race):
    write = FoundryStateStore.set_item
    observed = []
    winner = None

    async def raced(store, key, value, **options):
        nonlocal winner
        if key == "rid:rid-1" and value.get("receipt", {}).get("status") == "started" and not observed:
            observed.append(value["run_token"])
            if race == "applied-ack-lost":
                await write(store, key, value, **options)
            else:
                original = (await store.get_item(key)).value
                winner = {**original, "run_token": "OFFLINE-other-writer", "receipt": {
                    **original["receipt"], "status": "unknown", "failure_code": "NATIVE_TURN_CANCELLED",
                    "cancel_requested": True}}
                await write(store, key, winner, **options)
                # The original ETag now really loses against the official local store.
                return await write(store, key, value, **options)
            raise ServiceResponseError("OFFLINE original update outcome lost")
        return await write(store, key, value, **options)

    monkeypatch.setattr(FoundryStateStore, "set_item", raced)
    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        assert harness.invoke(submit()).status_code == 202
        terminal = harness.wait("rid-1", "completed" if race == "applied-ack-lost" else "unknown")
        if race == "other-writer":
            assert terminal.model_dump(mode="json") == winner["receipt"]
        else:
            assert sdk.calls[0][:2] == ("rid-1", terminal.conversation_id)
        assert harness.invoke(submit()).status_code == 202
        assert harness.call(runtime.read, "rid-1") == terminal
    assert len(observed) == 1 and len(sdk.calls) == (1 if race == "applied-ack-lost" else 0)


def test_started_cas_lost_to_another_session_keeps_its_started_receipt_unrun(local_env, monkeypatch):
    # The winner is still ``started``: re-entering it here (instead of leaving it to the session that took it over)
    # would read its conversation and close it as unknown.
    write = FoundryStateStore.set_item
    winner = None

    async def taken_over(store, key, value, **options):
        nonlocal winner
        if key == "rid:rid-1" and value.get("receipt", {}).get("status") == "started" and winner is None:
            original = (await store.get_item(key)).value
            winner = {**original, "run_token": "OFFLINE-other-session-run", "receipt": {
                **original["receipt"], "status": "started", "hosted_session_id": "OFFLINE-other-session"}}
            await write(store, key, winner, **options)
            return await write(store, key, value, **options)  # the original ETag now loses
        return await write(store, key, value, **options)

    monkeypatch.setattr(FoundryStateStore, "set_item", taken_over)
    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    runs = []
    start = runtime.chain.start

    async def capture_run(**kwargs):
        run = await start(**kwargs)
        runs.append(run)
        return run

    monkeypatch.setattr(runtime.chain, "start", capture_run)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        assert harness.invoke(submit()).status_code == 202
        harness.call(runs[0].result)
        stored = harness.call(runtime._rid, "rid-1").value
        assert stored.run_token == "OFFLINE-other-session-run"
        assert stored.receipt.model_dump(mode="json") == winner["receipt"]
        assert stored.receipt.status == "started" and stored.receipt.hosted_session_id == "OFFLINE-other-session"
        assert stored.receipt.failure_code is None and stored.receipt.result is None
    assert sdk.calls == []


@pytest.mark.parametrize("original_status", ["accepted", "started"])
@pytest.mark.parametrize("write_outcome", ["not-applied", "applied-ack-lost"])
def test_unknown_write_is_confirmed_or_a_task_error_without_restart(
        local_env, monkeypatch, original_status, write_outcome):
    from azure.ai.agentserver.core.tasks import TaskFailed

    write = FoundryStateStore.set_item
    runs = []
    transitions = []
    sdk_calls = []

    async def failed_write(store, key, value, **options):
        if key == "rid:rid-1":
            status = value.get("receipt", {}).get("status")
            transitions.append(status)
            if status == "started" and original_status == "accepted":
                raise ServiceResponseError("OFFLINE first started write not applied")
            if status == "unknown":
                if write_outcome == "applied-ack-lost":
                    await write(store, key, value, **options)
                raise ServiceResponseError("OFFLINE unknown write outcome lost")
        return await write(store, key, value, **options)

    async def sdk(receipt, *_args):
        sdk_calls.append(receipt.request_id)
        raise OSError("OFFLINE SDK outcome unconfirmed")

    monkeypatch.setattr(FoundryStateStore, "set_item", failed_write)
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    start = runtime.chain.start

    async def observed_start(**kwargs):
        run = await start(**kwargs)
        runs.append(run)
        return run

    runtime.chain.start = observed_start
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        assert harness.invoke(submit()).status_code == 202
        if write_outcome == "not-applied":
            with pytest.raises(TaskFailed):
                client.portal.call(runs[0].result)
        else:
            returned = client.portal.call(runs[0].result)
            assert returned.status == "unknown"
        receipt = harness.call(runtime.read, "rid-1")
        assert receipt.status == (original_status if write_outcome == "not-applied" else "unknown")
        assert receipt.result is None
        assert harness.invoke(NativeRead(request_id="rid-1")).json() == receipt.model_dump(mode="json")
        if write_outcome == "applied-ack-lost":
            assert returned == receipt
            assert receipt.failure_code == (
                "NATIVE_STATE_STORE_UNKNOWN" if original_status == "accepted" else "NATIVE_TURN_UNKNOWN")
            assert harness.invoke(submit()).json() == receipt.model_dump(mode="json")
            assert harness.invoke(NativeCancel(
                conversation_id=receipt.conversation_id, request_id="rid-1")).json() == receipt.model_dump(mode="json")
        assert len(runs) == 1
        assert transitions == (["started", "unknown"] if original_status == "accepted"
                               else ["started", "started", "unknown"])
        assert sdk_calls == ([] if original_status == "accepted" else ["rid-1"])


def test_applied_started_with_lost_ack_and_failed_readback_is_a_task_error(local_env, monkeypatch):
    from azure.ai.agentserver.core.tasks import TaskFailed

    write, read = FoundryStateStore.set_item, FoundryStateStore.get_item
    transitions, runs = [], []
    fail_readback = False

    async def lose_ack(store, key, value, **options):
        nonlocal fail_readback
        if key == "rid:rid-1":
            transitions.append(value["receipt"]["status"])
            if transitions == ["started"]:
                await write(store, key, value, **options)
                fail_readback = True
                raise ServiceResponseError("OFFLINE started applied but ACK lost")
        return await write(store, key, value, **options)

    async def lose_readback(store, key, **options):
        nonlocal fail_readback
        if key == "rid:rid-1" and fail_readback:
            fail_readback = False
            raise ServiceResponseError("OFFLINE started readback unavailable")
        return await read(store, key, **options)

    monkeypatch.setattr(FoundryStateStore, "set_item", lose_ack)
    monkeypatch.setattr(FoundryStateStore, "get_item", lose_readback)
    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    start = runtime.chain.start

    async def observed_start(**kwargs):
        run = await start(**kwargs)
        runs.append(run)
        return run

    runtime.chain.start = observed_start
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, sdk, client)
        assert harness.invoke(submit()).status_code == 202
        with pytest.raises(TaskFailed):
            client.portal.call(runs[0].result)
        receipt = harness.call(runtime.read, "rid-1")
        assert receipt.status == "started" and receipt.result is None
        assert harness.invoke(NativeRead(request_id="rid-1")).json() == receipt.model_dump(mode="json")
        assert transitions == ["started"] and len(runs) == 1
        assert not sdk.calls


# The enqueue task (``fa-native-agent-enqueue-v1``) puts an accepted input on the Agent chain once the chain has room;
# these drive one attempt of it the way core does (``TaskContext``), under the fixture's 10-attempt retry policy.


def enqueue_context(original, *, rid="rid-1", retry_attempt=0, continuation_of=None):
    return TaskContext(
        task_id=IDENTITY.agent_id, session_id=IDENTITY.session_id, input_id=rid, entry_mode="fresh",
        retry_attempt=retry_attempt, input=NativeTaskInput(
            request_id=rid, accepted_etag="continuation-intent" if original is None else original.etag,
            call_id=None if original is None else original.value.call_id,
            execution_version=None if continuation_of else IDENTITY.agent_version, continuation_of=continuation_of))


def chain_starts(harness, monkeypatch, failure=None):
    starts = []

    async def start(**kwargs):
        starts.append(kwargs["input_id"])
        if failure is not None:
            raise failure

    monkeypatch.setattr(harness.runtime.chain, "start", start)
    return starts


@pytest.mark.parametrize(("retry_attempt", "busy"), [(8, False), (9, True)])
def test_an_enqueued_input_on_a_full_chain_is_retried_and_refused_busy_only_on_the_last_attempt(
        harness, monkeypatch, retry_attempt, busy):
    from azure.ai.agentserver.core.tasks import SteeringQueueFull
    from test_native_marker_store import settle, sender_notices

    accepted = accept_without_chain(harness)
    notices = sender_notices(harness)
    starts = chain_starts(harness, monkeypatch, SteeringQueueFull())
    ctx = enqueue_context(accepted, retry_attempt=retry_attempt)
    if busy:
        assert harness.call(harness.runtime._enqueue_attempt, ctx) == "rid-1"
    else:
        with pytest.raises(SteeringQueueFull):  # core retries it after its delay
            harness.call(harness.runtime._enqueue_attempt, ctx)
    receipt = harness.call(harness.runtime.read, "rid-1")
    expected = ("busy_refused", "NATIVE_TURN_BUSY") if busy else ("accepted", None)
    assert (receipt.status, receipt.failure_code) == expected
    assert settle(notices, 1) == ([("rid-1", "busy_refused", "NATIVE_TURN_BUSY")] if busy else [])
    assert starts == ["rid-1"] and not harness.sdk.calls


@pytest.mark.parametrize(("timed_out", "code"), [(False, "NATIVE_TURN_CANCELLED"), (True, "NATIVE_TURN_TIMEOUT")])
def test_a_peer_input_whose_enqueue_task_was_cancelled_or_timed_out_ends_unknown_without_starting(
        harness, monkeypatch, timed_out, code):
    from test_native_marker_store import accept_peer_without_chain, settle, sender_notices

    accepted = accept_peer_without_chain(harness)
    notices = sender_notices(harness)
    starts = chain_starts(harness, monkeypatch)
    ctx = enqueue_context(accepted)
    ctx.cancel.set()  # core's request-level cancel; the owner never asked (cancel_requested stays False)
    ctx.timeout_exceeded = timed_out
    assert harness.call(harness.runtime._enqueue_attempt, ctx) == "rid-1"
    receipt = harness.call(harness.runtime.read, "rid-1")
    assert (receipt.status, receipt.failure_code) == ("unknown", code)
    assert settle(notices, 1) == [("rid-1", "unknown", code)]
    assert starts == [] and not harness.sdk.calls


def test_an_enqueued_input_its_owner_cancelled_ends_cancelled_without_starting(harness, monkeypatch):
    from test_native_marker_store import settle, sender_notices

    accepted = accept_without_chain(harness)
    cancelled, saved = harness.call(harness.runtime._cas, accepted, _changed(
        accepted.value, receipt=_changed(accepted.value.receipt, cancel_requested=True)))
    assert saved
    notices = sender_notices(harness)
    starts = chain_starts(harness, monkeypatch)
    assert harness.call(harness.runtime._enqueue_attempt, enqueue_context(cancelled)) == "rid-1"
    receipt = harness.call(harness.runtime.read, "rid-1")
    assert (receipt.status, receipt.failure_code) == ("unknown", "NATIVE_TURN_CANCELLED")
    assert settle(notices, 1) == [("rid-1", "unknown", "NATIVE_TURN_CANCELLED")]
    assert starts == [] and not harness.sdk.calls


def test_an_enqueued_input_that_already_ended_only_tells_its_sender_again(harness, monkeypatch):
    from test_native_marker_store import accept_peer_without_chain, settle, sender_notices

    accepted = accept_peer_without_chain(harness)
    ended, saved = harness.call(harness.runtime._cas, accepted, _changed(accepted.value, receipt=_changed(
        accepted.value.receipt, status="unknown", failure_code="NATIVE_TURN_NOT_STARTED")))
    assert saved
    notices = sender_notices(harness)
    starts = chain_starts(harness, monkeypatch)
    assert harness.call(harness.runtime._enqueue_attempt, enqueue_context(accepted)) == "rid-1"
    assert harness.call(harness.runtime._rid, "rid-1") == ended
    assert settle(notices, 1) == [("rid-1", "unknown", "NATIVE_TURN_NOT_STARTED")]
    assert starts == [] and not harness.sdk.calls


@pytest.mark.parametrize(("failure", "retry_attempt", "ended"), [
    ("transport", 8, None), ("transport", 9, "NATIVE_STATE_STORE_UNKNOWN"), ("500", 9, "OFFLINE_STORE_ANSWER")])
def test_an_unconfirmed_rid_read_in_the_enqueue_task_is_retried_and_ends_unknown_only_on_the_last_attempt(
        harness, monkeypatch, failure, retry_attempt, ended):
    from test_native_marker_store import settle, sender_notices

    accepted = accept_without_chain(harness)
    notices = sender_notices(harness)
    starts = chain_starts(harness, monkeypatch)
    read, lost = FoundryStateStore.get_item, []

    async def lose_first_read(store, key, **options):
        if key == "rid:rid-1" and not lost:
            lost.append(key)
            if failure == "500":
                raise Problem(500, "OFFLINE_STORE_ANSWER", "OFFLINE RID read answer")
            raise ServiceResponseError("OFFLINE RID read unavailable")
        return await read(store, key, **options)

    monkeypatch.setattr(FoundryStateStore, "get_item", lose_first_read)
    ctx = enqueue_context(accepted, retry_attempt=retry_attempt)
    if ended is not None:
        assert harness.call(harness.runtime._enqueue_attempt, ctx) == "rid-1"
    else:
        with pytest.raises(NativePostTurnPending):
            harness.call(harness.runtime._enqueue_attempt, ctx)
    receipt = harness.call(harness.runtime.read, "rid-1")
    assert (receipt.status, receipt.failure_code) == (("accepted", None) if ended is None else ("unknown", ended))
    assert settle(notices, 1) == ([] if ended is None else [("rid-1", "unknown", ended)])
    assert lost == ["rid:rid-1"] and starts == [] and not harness.sdk.calls


def test_a_missing_rid_ends_the_enqueue_task_with_its_definite_answer(harness, monkeypatch):
    starts = chain_starts(harness, monkeypatch)
    with pytest.raises(Problem) as missing:
        harness.call(harness.runtime._enqueue_attempt, enqueue_context(None, rid="never-seen", retry_attempt=9))
    assert (missing.value.status, missing.value.body["error"]["code"]) == (404, "NATIVE_RID_MISSING")
    assert starts == [] and not harness.sdk.calls


@pytest.mark.parametrize(("failure", "retry_attempt", "ended"), [
    ("500", 8, None), ("500", 9, "OFFLINE_START_ANSWER"), ("transport", 8, None), ("transport", 9, "NATIVE_TURN_UNKNOWN"),
    ("499", 9, None)])
def test_an_unconfirmed_chain_start_is_retried_and_ends_unknown_on_the_last_attempt_and_a_4xx_ends_the_task(
        harness, monkeypatch, failure, retry_attempt, ended):
    from test_native_marker_store import settle, sender_notices

    accepted = accept_without_chain(harness)
    notices = sender_notices(harness)
    raised = {"500": Problem(500, "OFFLINE_START_ANSWER", "OFFLINE start answer"),
              "499": Problem(499, "OFFLINE_START_ANSWER", "OFFLINE start answer"),
              "transport": ServiceResponseError("OFFLINE start transport")}[failure]
    starts = chain_starts(harness, monkeypatch, raised)
    ctx = enqueue_context(accepted, retry_attempt=retry_attempt)
    if ended is not None:
        assert harness.call(harness.runtime._enqueue_attempt, ctx) == "rid-1"
    else:
        with pytest.raises(Problem if failure == "499" else NativePostTurnPending) as error:
            harness.call(harness.runtime._enqueue_attempt, ctx)
        assert failure != "499" or error.value is raised
    receipt = harness.call(harness.runtime.read, "rid-1")
    assert (receipt.status, receipt.failure_code) == (("accepted", None) if ended is None else ("unknown", ended))
    assert settle(notices, 1) == ([] if ended is None else [("rid-1", "unknown", ended)])
    assert starts == ["rid-1"] and not harness.sdk.calls


def test_a_full_chain_keeps_a_peer_input_waiting_instead_of_refusing_it_busy(harness, monkeypatch):
    """A peer's sender waits on its RID, so a full chain never ends it ``busy_refused``, even on the last attempt: the
    enqueue task waits and tries again until there is room, or until core cancels the task."""
    from azure.ai.agentserver.core.tasks import SteeringQueueFull
    from test_native_marker_store import accept_peer_without_chain, settle, sender_notices

    accepted = accept_peer_without_chain(harness)
    notices = sender_notices(harness)
    ctx = enqueue_context(accepted, retry_attempt=9)
    starts = []

    async def full(**kwargs):
        starts.append(kwargs["input_id"])
        if len(starts) == 2:
            ctx.cancel.set()  # core cancels the task while it waits
        raise SteeringQueueFull()

    monkeypatch.setattr(harness.runtime.chain, "start", full)
    assert harness.call(harness.runtime._enqueue_pending, ctx) == "rid-1"
    receipt = harness.call(harness.runtime.read, "rid-1")
    assert (receipt.status, receipt.failure_code) == ("unknown", "NATIVE_TURN_CANCELLED")
    assert settle(notices, 1) == [("rid-1", "unknown", "NATIVE_TURN_CANCELLED")]
    assert starts == ["rid-1", "rid-1"] and not harness.sdk.calls


@pytest.mark.parametrize("version", [None, IDENTITY.agent_version])
def test_an_enqueued_input_is_queued_on_the_chain_pinned_to_this_version_and_its_accepted_receipt(
        harness, monkeypatch, version):
    """An input queued before inputs named a version (None) starts like a current one; what goes on the chain names
    this version, so the chain turn refuses it if the Hosted version changes before it runs (``_refuse_unrunnable``)."""
    accepted = accept_without_chain(harness)
    queued = []

    async def start(**kwargs):
        queued.append(kwargs)

    monkeypatch.setattr(harness.runtime.chain, "start", start)
    ctx = enqueue_context(accepted)
    ctx.input = ctx.input.model_copy(update={"execution_version": version})
    assert harness.call(harness.runtime._enqueue_attempt, ctx) == "rid-1"
    binding = harness.call(harness.runtime._require_original_session)
    assert queued == [{"task_id": binding.task_id, "input_id": "rid-1", "input": NativeTaskInput(
        request_id="rid-1", accepted_etag=accepted.etag, call_id=accepted.value.call_id,
        execution_version=IDENTITY.agent_version)}]
    assert harness.call(harness.runtime._rid, "rid-1") == accepted and not harness.sdk.calls


def test_a_busy_refusal_that_loses_to_a_concurrent_end_tells_the_sender_how_it_really_ended(harness, monkeypatch):
    from azure.ai.agentserver.core.tasks import SteeringQueueFull
    from test_native_marker_store import settle, sender_notices

    accepted = accept_without_chain(harness)
    notices = sender_notices(harness)

    async def full(**kwargs):
        # The owner's cancel ends the RID while the start is refused.
        await harness.runtime._unknown(await harness.runtime._rid("rid-1"), "NATIVE_TURN_CANCELLED")
        raise SteeringQueueFull()

    monkeypatch.setattr(harness.runtime.chain, "start", full)
    assert harness.call(harness.runtime._enqueue_attempt, enqueue_context(accepted, retry_attempt=9)) == "rid-1"
    receipt = harness.call(harness.runtime.read, "rid-1")
    assert (receipt.status, receipt.failure_code) == ("unknown", "NATIVE_TURN_CANCELLED")
    assert settle(notices, 1) == [("rid-1", "unknown", "NATIVE_TURN_CANCELLED")]
    assert not harness.sdk.calls



@pytest.mark.parametrize("continuation", [False, True])
def test_an_enqueue_task_entered_during_shutdown_exits_for_recovery_and_touches_nothing(
        harness, monkeypatch, continuation):
    accepted = accept_without_chain(harness)
    starts, dispatched = chain_starts(harness, monkeypatch), []

    async def dispatch(runtime, supplied):
        dispatched.append(supplied.request_id)

    monkeypatch.setattr(native_continuations, "dispatch_continuation", dispatch)
    ctx = (enqueue_context(None, rid="continuation-rid", continuation_of="rid-1") if continuation
           else enqueue_context(accepted))
    ctx.shutdown.set()
    assert type(harness.call(harness.runtime._enqueue_attempt, ctx)).__name__ == "_ExitForRecovery"
    assert harness.call(harness.runtime._rid, "rid-1") == accepted
    assert starts == [] and dispatched == [] and not harness.sdk.calls


@pytest.mark.parametrize(("cancelled", "timed_out", "code"), [
    (True, False, "NATIVE_TURN_CANCELLED"), (False, True, "NATIVE_TURN_TIMEOUT"), (True, True, "NATIVE_TURN_CANCELLED")])
def test_a_continuation_dispatch_stopped_by_core_records_the_stop_and_never_dispatches(
        harness, monkeypatch, cancelled, timed_out, code):
    _, started = seed_started(harness)
    # A started original whose recovery found no dispatcher: its continuation intent is saved (blocked) and it ends.
    harness.call(partial(native_continuations.prepare_continuation, failure_code="NATIVE_TURN_UNKNOWN"),
                 harness.runtime, started)
    dispatched = []

    async def dispatch(runtime, supplied):
        dispatched.append(supplied.request_id)

    monkeypatch.setattr(native_continuations, "dispatch_continuation", dispatch)
    ctx = enqueue_context(None, rid="continuation-rid", continuation_of="rid-1")
    ctx.cancel_requested, ctx.timeout_exceeded = cancelled, timed_out
    with pytest.raises(Problem) as stopped:
        harness.call(harness.runtime._enqueue_attempt, ctx)
    assert (stopped.value.status, stopped.value.body["error"]["code"]) == (409, code)
    assert stopped.value.body["error"]["message"] == "the original continuation dispatch stopped before submission"
    view = harness.call(native_continuations.read_continuation, harness.runtime, "rid-1")
    assert (view.failure_code, view.cancel_requested) == (code, code == "NATIVE_TURN_CANCELLED")
    assert dispatched == [] and not harness.sdk.calls


@pytest.mark.parametrize("failure", ["500", "499", "transport"])
def test_a_continuation_dispatch_failure_is_retried_only_when_it_is_a_5xx_answer(harness, monkeypatch, failure):
    raised = {"500": Problem(500, "OFFLINE_DISPATCH", "OFFLINE dispatch answer"),
              "499": Problem(499, "OFFLINE_DISPATCH", "OFFLINE dispatch answer"),
              "transport": ServiceResponseError("OFFLINE dispatch transport")}[failure]

    async def dispatch(runtime, supplied):
        assert supplied.continuation_of == "rid-1"
        raise raised

    monkeypatch.setattr(native_continuations, "dispatch_continuation", dispatch)
    expected = {"500": NativePostTurnPending, "499": Problem, "transport": ServiceResponseError}[failure]
    with pytest.raises(expected) as error:
        harness.call(harness.runtime._enqueue_attempt,
                     enqueue_context(None, rid="continuation-rid", continuation_of="rid-1"))
    if failure != "500":
        assert error.value is raised  # passed on as it came: a transport error is not retried here
