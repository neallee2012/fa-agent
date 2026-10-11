"""Recover only an indexed original send task before settling the sender; never rerun its model."""

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_agent_wire import NativePeerRequest, NativeRelayAcceptance, NativeRelaySend
from shared_context.behavior_native_turn_wire import NativeCancel, NativeEnvironmentView
from shared_context.errors import Problem
from shared_context.native_agent_tools import NativeAgentRelayClient, _WaitingEnvironment
from shared_context.native_effects import NativeEffects
from shared_context.native_operations import NativeOperationGuard
from shared_context.native_turns import NativeTurnRuntime
from test_native_agent_client import token
from test_native_responses import ResponsesHarness, build_responses_host, response_submit
from test_native_responses_relay import fast_follow, task_results, task_runs
from test_native_turn_wire import submit
from test_native_turns import (
    Harness, IDENTITY, OfflineSdk, harness, leave_saved_turn, local_env, native_retry_policy, seed_started, task_context,
)


def test_production_factory_binds_queue_recovery_to_its_own_sender(local_env, tmp_path, monkeypatch):
    from shared_context import native_sdk
    from test_native_sdk import AssistantSupervisor, MODEL_ROOT

    executable = tmp_path / "OFFLINE-not-executed"
    executable.write_text("OFFLINE factory only", encoding="utf-8")
    hosted = SimpleNamespace(
        identity=IDENTITY, supervisor=AssistantSupervisor(tmp_path / "owned", runtime_path=executable),
        api=SimpleNamespace(api_url="https://api.offline.invalid", audience="api://OFFLINE"))
    calls = []

    async def observed(client, runtime, original):
        calls.append((client.runtime, runtime, original))

    monkeypatch.setattr(NativeAgentRelayClient, "recover_queued", observed)
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    monkeypatch.setenv("FOUNDRY_HOSTING_ENVIRONMENT", "OFFLINE-platform")
    monkeypatch.setenv("AGENTSERVER_TASKS_BACKEND", "hosted")
    monkeypatch.setenv("BEHAVIOR_ASSISTANT_RUNTIME_PATH", str(executable))
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", MODEL_ROOT.removesuffix("/openai/v1"))
    runtime = native_sdk.native_runtime_from_environment(hosted)
    original = object()
    asyncio.run(runtime.on_recover_queued(original))
    assert calls == [(runtime, runtime, original)]
    assert runtime.executor.reader is runtime


@pytest.mark.parametrize("input_shape", ["environment", "original-send"])
@pytest.mark.parametrize("task_finishes_first", [False, True])
@pytest.mark.parametrize("checkpoint_saved", [False, True], ids=["no-checkpoint", "saved-checkpoint"])
def test_recovered_sender_joins_the_original_queue_before_becoming_unknown(
    harness, fast_follow, input_shape, task_finishes_first, checkpoint_saved,
):
    _, original = seed_started(harness)
    if checkpoint_saved:
        from shared_context.native_checkpoint import conversation_store_name

        original, meta = harness.call(leave_saved_turn, harness.runtime, original)

        async def saved_turn_bytes():
            async with harness.runtime._store(conversation_store_name(meta.conversation_id)) as store:
                return json.dumps((await store.get_item(f"turn:{meta.n}")).value, sort_keys=True)

        before = harness.call(saved_turn_bytes)
    receipt = original.value.receipt
    message = NativePeerRequest(recipient_agent_id="peer", kind="request", content="Original queued brief.")
    queued = NativeRelaySend(
        conversation_id=receipt.conversation_id, request_id=receipt.request_id, tool_call_id="original-send", message=message)
    calls = []
    accepted = NativeRelayAcceptance(
        message_id="original-message", recipient_agent_id="peer", conversation_id="original-peer-cid",
        submit_digest="a" * 64, status="accepted")

    async def relay(packet):
        calls.append(packet.url.path)
        sender = await harness.runtime.read(receipt.request_id)
        if sender.status != "started":
            return httpx.Response(403, json={"error": {"code": "NATIVE_AGENT_SOURCE_INVALID"}})
        if packet.url.path.endswith("/environment"):
            # The real API requires this status; returning UNKNOWN first is the review's failure.
            return httpx.Response(200, content=NativeEnvironmentView(
                agent_id="peer", request_id="peer-follow", previous_hosted_session_id="old-peer",
                previous_agent_version="41", hosted_session_id="new-peer", hosted_agent_version="42",
                state="ready").model_dump_json().encode())
        assert packet.url.path.endswith("/send")
        return httpx.Response(202, content=accepted.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    client.runtime = harness.runtime
    controls = SimpleNamespace(stop_requested=lambda: False)
    effects = NativeEffects(harness.runtime, receipt, controls)
    guard = NativeOperationGuard(effects, {"send_agent_message": "b" * 64})
    packet = {"tool_name": "send_agent_message", "tool_call_id": queued.tool_call_id,
              "arguments": message.model_dump(mode="json")}

    async def setup_and_recover():
        from shared_context import native_agent_tools

        await guard.start()
        await guard.prepare(packet)
        await guard.dispatched(packet)
        identifier = native_agent_tools.material_digest({
            "kind": "fa-native-version-message-v1", "agent": receipt.agent_id,
            "command": queued.model_dump(mode="json")})
        task = await client._following.start(
            task_id=identifier, input=_WaitingEnvironment(command=queued) if input_shape == "environment" else queued)
        if task_finishes_first:
            assert await task.result() == accepted
        harness.runtime.on_recover_queued = lambda bound: client.recover_queued(harness.runtime, bound)
        result = await harness.runtime.handle_turn(task_context(original))
        assert result.status == ("completed" if checkpoint_saved else "unknown")
        assert (result.result is not None) == checkpoint_saved, "Do not invent a checkpoint or discard a saved one"
        assert result.delegation == accepted, "Retain the real recipient acceptance before settling the sender"
        assert await task.result() == accepted

    harness.call(setup_and_recover)
    assert not harness.sdk.calls
    assert sum(path.endswith("/send") for path in calls) == 1
    retained = harness.call(harness.runtime.read, receipt.request_id)
    assert retained.delegation == accepted
    if checkpoint_saved:
        assert harness.call(saved_turn_bytes) == before


def test_queued_acceptance_cannot_rewrite_a_sender_that_became_unknown(harness, fast_follow, monkeypatch):
    from azure.ai.agentserver.core.tasks import TaskFailed

    _, original = seed_started(harness)
    receipt = original.value.receipt
    command = NativeRelaySend(
        conversation_id=receipt.conversation_id, request_id=receipt.request_id, tool_call_id="late-acceptance",
        message=NativePeerRequest(recipient_agent_id="peer", kind="request", content="Original late reply."))
    accepted = NativeRelayAcceptance(
        message_id="late-message", recipient_agent_id="peer", conversation_id="late-peer-cid",
        submit_digest="a" * 64, status="accepted")
    calls, unknown_bytes = [], []
    retain = harness.runtime._retain_delegation

    async def interrupted_before_retention(bound, value, **options):
        await harness.runtime._unknown(await harness.runtime._rid(receipt.request_id), "NATIVE_TURN_UNKNOWN")
        unknown_bytes.append((await harness.runtime._rid(receipt.request_id)).value.model_dump_json())
        return await retain(bound, value, **options)

    monkeypatch.setattr(harness.runtime, "_retain_delegation", interrupted_before_retention)

    def relay(packet):
        calls.append(packet.url.path)
        return httpx.Response(202, content=accepted.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    client.runtime = harness.runtime

    async def finish():
        run = await client._following.start(task_id="late-original-task", input=command)
        return await run.result()

    with pytest.raises(TaskFailed):
        harness.call(finish)
    current = harness.call(harness.runtime._rid, receipt.request_id)
    assert current.value.model_dump_json() == unknown_bytes[0]
    assert len(calls) == 1 and calls[0].endswith("/send")
    assert not harness.sdk.calls


@pytest.mark.parametrize("input_shape", ["original-send", "environment"])
def test_known_acceptance_keeps_dispatching_without_rewriting_the_original_receipt(
    harness, fast_follow, task_runs, input_shape,
):
    from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE, NativeResponseDispatch

    _, original = seed_started(harness)
    receipt = original.value.receipt
    command = NativeRelaySend(
        conversation_id=receipt.conversation_id, request_id=receipt.request_id, tool_call_id="confirmed-send",
        message=NativePeerRequest(recipient_agent_id="peer", kind="request", content="Original confirmed request."))
    accepted = NativeRelayAcceptance(
        message_id="confirmed-message", recipient_agent_id="peer", conversation_id="confirmed-peer-cid",
        submit_digest="a" * 64, status="accepted", state_profile=NATIVE_RESPONSES_PROFILE,
        dispatch=NativeResponseDispatch(state="not_taken", inflight_request_id="previous-busy"))
    harness.call(harness.runtime._retain_delegation, original, accepted)
    current = harness.call(harness.runtime._rid, receipt.request_id)
    harness.call(harness.runtime._unknown, current, "NATIVE_TURN_UNKNOWN")
    before = harness.call(harness.runtime._rid, receipt.request_id).value.model_dump_json()
    calls, paths = [], []

    async def relay(packet):
        paths.append(packet.url.path)
        calls.append(bytes(packet.content))
        if "/environment" in packet.url.path:
            sender = await harness.runtime.read(receipt.request_id)
            assert sender.status == "unknown"
            return httpx.Response(403, json={"error": {"code": "NATIVE_AGENT_SOURCE_INVALID"}})
        response = accepted.model_copy(update={"dispatch": NativeResponseDispatch(
            state="needs_response" if len(calls) == 1 else "already_queued",
            inflight_request_id=accepted.message_id)})
        return httpx.Response(202, content=response.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    client.runtime = harness.runtime

    async def finish():
        run = await client._following.start(
            task_id="confirmed-original-task",
            input=_WaitingEnvironment(command=command) if input_shape == "environment" else command)
        await run.result()
        return await task_results(task_runs)

    results = harness.call(finish)
    assert any(result.dispatch.state == "already_queued" for result in results)
    assert all(path.endswith("/send") for path in paths)
    assert len(calls) == 2 and len(set(calls)) == 1
    assert harness.call(harness.runtime._rid, receipt.request_id).value.model_dump_json() == before
    assert not harness.sdk.calls


def test_late_queue_ack_during_checkpoint_commit_does_not_leave_a_completed_sender_busy(harness, monkeypatch):
    accepted = NativeRelayAcceptance(
        message_id="concurrent-message", recipient_agent_id="peer", conversation_id="concurrent-peer-cid",
        submit_digest="a" * 64, status="accepted")
    cas, injected = harness.runtime._cas, []

    async def concurrent_acceptance(original, replacement):
        if replacement.receipt.status == "completed" and not injected:
            injected.append(True)
            await harness.runtime._retain_delegation(original, accepted, started_only=True)
        return await cas(original, replacement)

    monkeypatch.setattr(harness.runtime, "_cas", concurrent_acceptance)
    assert harness.invoke(submit()).status_code == 202
    result = harness.wait("rid-1", "completed")
    assert result.delegation == accepted and result.result is not None
    assert len(harness.sdk.calls) == 1


@pytest.mark.parametrize("responses_peer", [False, True], ids=["native-peer", "responses-peer"])
def test_official_task_restart_after_ack_before_dispatch_uses_only_the_original_message(
    local_env, fast_follow, responses_peer,
):
    from azure.ai.agentserver.core.tasks import TaskContext, task
    from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE, NativeResponseDispatch

    state, calls, posted = {}, [], []
    parked, dispatched = threading.Event(), threading.Event()
    accepted = NativeRelayAcceptance(
        message_id="ack-before-dispatch", recipient_agent_id="peer", conversation_id="ack-peer-cid",
        submit_digest="c" * 64, status="accepted",
        state_profile=NATIVE_RESPONSES_PROFILE if responses_peer else "foundry-native-v1",
        dispatch=NativeResponseDispatch(state="needs_response", inflight_request_id="ack-before-dispatch")
        if responses_peer else None)

    async def relay(packet):
        command = NativeRelaySend.model_validate_json(packet.content)
        sender = await state["runtime"].read(command.request_id)
        calls.append((packet.url.path, bytes(packet.content)))
        if "/environment" in packet.url.path:
            assert sender.status == "unknown"
            return httpx.Response(403, json={"error": {"code": "NATIVE_AGENT_SOURCE_INVALID"}})
        assert packet.url.path.endswith("/send")
        if not posted:
            assert sender.status == "started"
            posted.append(command)
            return httpx.Response(202, content=accepted.model_dump_json().encode())
        assert command == posted[0] and sender.status == "unknown"
        dispatched.set()
        response = accepted.model_copy(update={
            "dispatch": NativeResponseDispatch(state="already_queued", inflight_request_id=accepted.message_id)
        }) if responses_peer else accepted
        return httpx.Response(202, content=response.model_dump_json().encode())

    first = NativeTurnRuntime(IDENTITY, OfflineSdk())
    sender_client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    sender_client.runtime = first
    state["runtime"] = first

    @task(name="fa-native-version-message-v1")
    async def interrupted_at_ack(ctx: TaskContext[_WaitingEnvironment]):
        command = ctx.input.command
        reply = await sender_client._request("/send", NativeRelayAcceptance, command)
        await sender_client._retain_queued_acceptance(first, await first._rid(command.request_id), reply)
        parked.set()
        await ctx.shutdown.wait()
        return await ctx.exit_for_recovery()

    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first)) as http:
        h = Harness(first, first.executor, http)
        _, original = seed_started(h)
        receipt = original.value.receipt
        command = NativeRelaySend(
            conversation_id=receipt.conversation_id, request_id=receipt.request_id, tool_call_id="interrupted-send",
            message=NativePeerRequest(recipient_agent_id="peer", kind="request", content="Original accepted request."))

        async def begin():
            await interrupted_at_ack.start(task_id="interrupted-original-task", input=_WaitingEnvironment(command=command))

        h.call(begin)
        assert parked.wait(5)
        result = h.call(first.handle_turn, task_context(original))
        assert result.status == "unknown" and result.delegation == accepted
        before = h.call(first._rid, receipt.request_id).value.model_dump_json()

    restored = NativeTurnRuntime(IDENTITY, OfflineSdk())
    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
    client.runtime = restored
    state["runtime"] = restored
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=restored)) as http:
        assert dispatched.wait(5), "The original accepted work must resume dispatch without environment admission"
        assert http.portal.call(restored._rid, receipt.request_id).value.model_dump_json() == before
    assert len(posted) == 1 and len(calls) == 2 and len({body for _path, body in calls}) == 1
    assert not first.executor.calls and not restored.executor.calls


@pytest.mark.parametrize("difference", ["handler", "input", "task-read"])
def test_recovery_does_not_join_a_task_that_differs_from_the_indexed_original(harness, monkeypatch, difference):
    from azure.ai.agentserver.core.tasks import Task, TaskContext, task
    from azure.ai.agentserver.core.tasks._manager import get_task_manager
    from azure.core.exceptions import ServiceResponseError
    from shared_context.behavior_native_turn_wire import material_digest

    _, original = seed_started(harness)
    receipt = original.value.receipt
    command = NativeRelaySend(
        conversation_id=receipt.conversation_id, request_id=receipt.request_id, tool_call_id="scoped-send",
        message=NativePeerRequest(recipient_agent_id="peer", kind="request", content="Original scoped input."))
    client = NativeAgentRelayClient("https://api.invalid", token)
    client.runtime = harness.runtime
    harness.runtime.on_recover_queued = lambda bound: client.recover_queued(harness.runtime, bound)

    @task(name="offline-unrelated-task" if difference == "handler" else "fa-native-version-message-v1")
    async def unrelated(ctx: TaskContext[_WaitingEnvironment]):
        await ctx.shutdown.wait()
        return await ctx.exit_for_recovery()

    async def never_join(*args, **kwargs):
        raise AssertionError("Mismatched stored input must be rejected before acquiring or joining its task")

    monkeypatch.setattr(Task, "get_active_run", never_join)

    async def recover():
        guard = NativeOperationGuard(NativeEffects(harness.runtime, receipt), {"send_agent_message": "b" * 64})
        packet = {"tool_name": "send_agent_message", "tool_call_id": command.tool_call_id,
                  "arguments": command.message.model_dump(mode="json")}
        await guard.start()
        await guard.prepare(packet)
        await guard.dispatched(packet)
        identifier = material_digest({
            "kind": "fa-native-version-message-v1", "agent": receipt.agent_id,
            "command": command.model_dump(mode="json")})
        different = (command.model_copy(update={"conversation_id": "another-cid"})
                     if difference == "input" else command)
        await unrelated.start(task_id=identifier, input=_WaitingEnvironment(command=different))
        if difference == "task-read":
            provider, caller = get_task_manager().provider, asyncio.current_task()
            get = provider.get

            async def unavailable(key):
                if key == identifier and asyncio.current_task() is caller:
                    raise ServiceResponseError("OFFLINE original task metadata read failed")
                return await get(key)

            monkeypatch.setattr(provider, "get", unavailable)
        return await harness.runtime.handle_turn(task_context(original))

    result = harness.call(recover)
    assert result.status == "unknown" and result.failure_code == (
        "NATIVE_RELAY_OUTCOME_UNKNOWN" if difference == "task-read" else "NATIVE_STATE_SCOPE")
    assert result.delegation is None and not harness.sdk.calls


@pytest.mark.parametrize("lane", ["invocations", "responses"])
@pytest.mark.parametrize("restarts", [1, 2])
@pytest.mark.parametrize("ending", ["ready", "cancel", "unknown-put"])
def test_legacy_sender_and_queue_recover_together_before_delivering_once(
    local_env, tmp_path, monkeypatch, fast_follow, lane, restarts, ending,
):
    from azure.ai.agentserver.core.tasks import Task

    current = [None]
    ready, waiting, blocked, joining = (threading.Event() for _ in range(4))
    deliveries, calls = [], []
    active_run = Task.get_active_run

    async def observe_join(task, *args, **kwargs):
        run = await active_run(task, *args, **kwargs)
        if run is not None:
            joining.set()
        return run

    monkeypatch.setattr(Task, "get_active_run", observe_join)
    accepted = NativeRelayAcceptance(
        message_id="retained-message", recipient_agent_id="peer", conversation_id="retained-peer-cid",
        submit_digest="c" * 64, status="accepted")

    async def relay(packet):
        command = NativeRelaySend.model_validate_json(packet.content)
        calls.append((packet.url.path, bytes(packet.content)))
        sender = await current[0].read(command.request_id)
        # Named HTTP boundary double; the real API checks the same persisted sender, not a UI flag.
        if sender.status != "started" or sender.cancel_requested:
            return httpx.Response(403, json={"error": {"code": "NATIVE_AGENT_SOURCE_INVALID"}})
        if "/environment" in packet.url.path:
            waiting.set()
            if blocked.is_set():
                return httpx.Response(503, json={"error": {"code": "NATIVE_FILES_UNKNOWN"}})
            return httpx.Response(200, content=NativeEnvironmentView(
                agent_id="peer", request_id="retained-follow", previous_hosted_session_id="old-peer",
                previous_agent_version="41", hosted_session_id="new-peer", hosted_agent_version="42",
                state="ready" if ready.is_set() else "pending",
                reason=None if ready.is_set() else "scan_pending", progress_token="a" * 64,
            ).model_dump_json().encode())
        assert packet.url.path.endswith("/send")
        if not ready.is_set():
            return httpx.Response(503, json={"error": {"code": "AGENT_VERSION_FOLLOWING"}})
        deliveries.append(command)
        return httpx.Response(202, content=accepted.model_dump_json().encode())

    class InterruptedSender(OfflineSdk):
        async def __call__(self, receipt, scope, previous, control):
            self.calls.append(receipt.request_id)
            self.controls[receipt.request_id] = control
            message = NativePeerRequest(recipient_agent_id="peer", kind="request", content="Retained original brief.")
            guard = NativeOperationGuard(
                NativeEffects(current[0], receipt, control), {"send_agent_message": "b" * 64})
            packet = {"tool_name": "send_agent_message", "tool_call_id": "retained-call",
                      "arguments": message.model_dump(mode="json")}
            await guard.start()
            await guard.prepare(packet)
            await guard.dispatched(packet)
            async def legacy_send():
                from shared_context.native_agent_tools import NativeRecipientFollowing
                from shared_context.behavior_native_agent_wire import relay_task_id
                command = NativeRelaySend(conversation_id=receipt.conversation_id,
                                          request_id=receipt.request_id, tool_call_id="retained-call", message=message)
                try:
                    return await self.relay._request("/send", NativeRelayAcceptance, command)
                except NativeRecipientFollowing:
                    run = await self.relay._following.start(
                        task_id=relay_task_id(receipt.agent_id, command), input=_WaitingEnvironment(command=command))
                    return await run.result()

            # Pre-async records have the task/input but no queued_relay reference on the sender.
            sent = asyncio.create_task(legacy_send())
            try:
                while not sent.done():
                    if control.stop_requested():
                        raise Problem(503, "NATIVE_TURN_UNKNOWN", "OFFLINE interrupted SDK")
                    await asyncio.sleep(0.01)
                await sent
                pytest.fail("The first sender must stop while the recipient is still waiting")
            finally:
                if not sent.done():
                    sent.cancel()
                await asyncio.gather(sent, return_exceptions=True)

    if lane == "responses":
        monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    journal = tmp_path / "journal"
    journal.mkdir()

    def build(executor):
        runtime = NativeTurnRuntime(IDENTITY, executor)
        client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(relay))
        client.runtime = runtime
        runtime.on_recover_queued = lambda bound: client.recover_queued(runtime, bound)
        executor.relay = client
        current[0] = runtime
        contexts = {}
        app = (build_responses_host(runtime, journal, contexts) if lane == "responses" else
               create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime))
        return runtime, app, contexts

    first_sdk = InterruptedSender()
    original, app, contexts = build(first_sdk)
    with TestClient(app) as http:
        harness_type = ResponsesHarness if lane == "responses" else Harness
        first = harness_type(original, first_sdk, http)
        command = response_submit() if lane == "responses" else submit()
        assert first.invoke(command).status_code == 202
        if lane == "responses":
            first.respond(first.call(original.read, "rid-1"))
        assert waiting.wait(5)
        retained = first.call(original.read, "rid-1")
        assert retained.status == "started" and retained.delegation is None and not deliveries
    assert first_sdk.calls == ["rid-1"]

    for _ in range(restarts - 1):
        waiting.clear()
        joining.clear()
        interrupted_sdk = OfflineSdk()
        intermediate, app, _ = build(interrupted_sdk)
        with TestClient(app) as http:
            assert waiting.wait(5)
            assert joining.wait(5)
            pending = http.portal.call(intermediate.read, "rid-1")
            assert pending.status == "started" and pending.delegation is None and not deliveries
        assert not interrupted_sdk.calls

    waiting.clear()
    joining.clear()
    if ending == "ready":
        ready.set()
    second_sdk = OfflineSdk()
    restored, app, _ = build(second_sdk)
    with TestClient(app) as http:
        second = Harness(restored, second_sdk, http)
        if ending != "ready":
            assert waiting.wait(5)
            assert joining.wait(5)
            if ending == "cancel":
                result = second.invoke(NativeCancel(
                    request_id="rid-1", conversation_id=retained.conversation_id))
                assert result.status_code == 200 and result.json()["cancel_requested"]
            else:
                blocked.set()
        result = second.wait("rid-1", "unknown")
        assert result.delegation == (accepted if ending == "ready" else None)
        if ending != "ready":
            assert result.failure_code == (
                "NATIVE_TURN_CANCELLED" if ending == "cancel" else "NATIVE_RELAY_OUTCOME_UNKNOWN")
        assert result.result is None, "A recovered send is not a fabricated sender checkpoint"
        assert result.submit_digest == retained.submit_digest and result.capture_digest == retained.capture_digest
    assert not second_sdk.calls
    assert len(deliveries) == int(ending == "ready")
    assert len({body for _path, body in calls}) == 1
    if deliveries:
        assert json.loads(calls[0][1]) == deliveries[0].model_dump(mode="json")
