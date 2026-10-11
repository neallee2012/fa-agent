"""Original start and RID write failures must not become successful pending work."""

import asyncio
from types import SimpleNamespace

import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.core.exceptions import ServiceResponseError
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeCancel, NativeRead
from shared_context.native_turns import NativeTaskInput, NativeTurnRuntime
from test_native_turn_wire import agent_receive, submit
from test_native_turns import Harness, IDENTITY, OfflineSdk, harness, local_env, native_retry_policy


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
