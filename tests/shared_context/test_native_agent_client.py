"""Native Hosted relay transport and completed-hook reentry, using the official local N1 chain."""

import asyncio
import json
import time

import httpx
import pytest

from shared_context.behavior_native_agent_wire import NativePeerRequest, NativeRelayAcceptance
from shared_context.errors import Problem
from shared_context.native_agent_tools import NativeAgentRelayClient
from test_native_agent_relay import sender_case
from test_native_turns import harness, local_env, native_retry_policy
from test_native_turn_wire import agent_receive, submit
from test_native_agent_tools import collaboration_turn


async def token():
    return "synthetic-relay-token"


def test_quota_uncertainty_is_not_reported_as_a_send_refusal_and_has_no_retry():
    _, _scope, recipient, _principal, sender = sender_case()
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(409, json={"error": {
            "code": "NATIVE_RELAY_QUOTA_UNKNOWN", "message": "PRIVATE_UPSTREAM_MESSAGE"}})

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(transport))
    with pytest.raises(Problem) as unknown:
        asyncio.run(client.send(sender, "original-call", NativePeerRequest(
            recipient_agent_id=recipient.target.agent_id, kind="request", content="Public brief.")))
    assert unknown.value.body["error"]["code"] == "NATIVE_RELAY_OUTCOME_UNKNOWN"
    assert "PRIVATE" not in str(unknown.value.body) and len(calls) == 1


def test_completed_native_turn_reenters_the_same_wake_after_lost_ack_without_another_sdk_run(harness):
    calls, accepted = [], {}
    request = agent_receive()

    def transport(packet):
        assert packet.url.path.endswith("/native-agent-messages/result")
        value = json.loads(packet.content)
        calls.append(value)
        accepted.setdefault(value["request_id"], value)
        if len(calls) == 1:
            raise httpx.ReadTimeout("synthetic wake receipt lost", request=packet)
        reply = NativeRelayAcceptance(
            message_id=request.source.message_id, recipient_agent_id=request.source.sender.agent_id,
            conversation_id=request.source.sender.conversation_id, submit_digest="a" * 64, status="accepted",
            source_conversation_id=value["conversation_id"], source_request_id=value["request_id"])
        return httpx.Response(202, content=reply.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(transport))
    harness.runtime.on_completed = client.completed
    assert harness.invoke(request).status_code == 202
    completed = harness.wait(request.command.request_id, "completed")
    deadline = time.monotonic() + 8
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 2
    for _ in range(2):
        assert harness.call(harness.runtime.read, completed.request_id) == completed
    assert len(calls) == 2 and calls[0] == calls[1]
    assert len(accepted) == 1 and len(harness.sdk.calls) == 1
    assert harness.call(harness.runtime.read, completed.request_id) == completed


@pytest.mark.parametrize("unavailable", ["usage", "wake"])
def test_saved_completion_recovers_notification_without_manual_replay(harness, unavailable):
    usage_calls, wake_calls, delivered = [], [], set()

    async def usage(receipt):
        usage_calls.append(receipt.request_id)
        if unavailable == "usage" and len(usage_calls) == 1:
            raise Problem(503, "NATIVE_POST_TURN_UNKNOWN", "synthetic usage connection failure")

    async def wake(receipt, reference):
        wake_calls.append((receipt.request_id, reference))
        if unavailable == "wake" and len(wake_calls) == 1:
            raise Problem(503, "NATIVE_RELAY_OUTCOME_UNKNOWN", "synthetic wake connection failure")
        delivered.add(receipt.request_id)

    harness.runtime.on_terminal = usage
    harness.runtime.on_completed = wake
    request = agent_receive()
    assert harness.invoke(request).status_code == 202
    original = harness.wait(request.command.request_id, "completed")
    deadline = time.monotonic() + 8
    while (not delivered or len(usage_calls if unavailable == "usage" else wake_calls) < 2) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert delivered == {original.request_id}, "a saved result must reach its wake without another input or manual replay"
    assert len(usage_calls if unavailable == "usage" else wake_calls) >= 2
    assert len(harness.sdk.calls) == 1
    assert harness.call(harness.runtime.read, original.request_id) == original


def test_unavailable_directory_does_not_discard_an_accepted_agent_turn(harness):
    from shared_context.native_agent_tools import NativeAgentTools
    from shared_context.behavior_native_turn_wire import NativeAgentReceive

    class MissingDirectory:
        async def directory(self, _conversation_id=None):
            raise Problem(503, "NATIVE_RELAY_OUTCOME_UNKNOWN", "synthetic directory outage")

    sdk = harness.runtime.executor
    observations = []

    async def execute(receipt, scope, previous, control):
        tools = NativeAgentTools(MissingDirectory(), receipt, control)
        specs = await tools.prepare()
        observations.append((specs, tools.unavailable_reason))
        return await sdk(receipt, scope, previous, control)

    harness.runtime.executor = execute
    original = agent_receive()
    request = NativeAgentReceive(command=original.command, source=original.source, capture=collaboration_turn()[0].capture)
    assert harness.invoke(request).status_code == 202
    completed = harness.wait(request.command.request_id, "completed")
    assert observations and not any(tool.name == "send_agent_message" for tool in observations[0][0])
    assert observations[0][1] is not None
    assert len(harness.sdk.calls) == 1
    assert harness.invoke(request).json()["status"] == "completed"
    assert len(harness.sdk.calls) == 1 and completed.result is not None


def test_a_full_official_queue_keeps_the_same_agent_input_until_space_is_available(harness):
    harness.sdk.block = True
    assert harness.invoke(submit("active-human")).status_code == 202
    active = harness.wait("active-human", "started")
    full = False
    for number in range(12):
        response = harness.invoke(submit(f"queued-human-{number}"))
        assert response.status_code == 202
        if response.json()["status"] == "busy_refused":
            full = True
            break
    assert full
    request = agent_receive("original-pending-wake", active.conversation_id)
    response = harness.invoke(request)
    assert response.status_code == 202
    assert response.json()["status"] == "accepted", "queue capacity must not permanently poison an Agent's wake RID"
    harness.sdk.block = False
    completed = harness.wait(request.command.request_id, "completed")
    assert completed.conversation_id == active.conversation_id
    assert sum(call[0] == request.command.request_id for call in harness.sdk.calls) == 1


def test_exhausted_notification_retry_is_explicit_without_replacing_the_saved_result(harness):
    calls = []

    async def unavailable(receipt, _reference):
        calls.append(receipt.request_id)
        raise Problem(503, "NATIVE_RELAY_OUTCOME_UNKNOWN", "synthetic sustained outage")

    harness.runtime.on_completed = unavailable
    request = agent_receive()
    assert harness.invoke(request).status_code == 202
    original = harness.wait(request.command.request_id, "completed")
    deadline = time.monotonic() + 8
    current = original
    while current.notification_error is None and time.monotonic() < deadline:
        time.sleep(0.03)
        current = harness.call(harness.runtime.read, original.request_id)
    assert current.notification_error == "NATIVE_POST_TURN_UNKNOWN"
    assert current.status == "completed" and current.result == original.result and current.usage == original.usage
    assert len(calls) == 10 and len(harness.sdk.calls) == 1


def test_failed_peer_turn_sends_one_unknown_notice_without_another_model(harness):
    calls = []
    request = agent_receive()

    def transport(packet):
        assert packet.url.path.endswith("/native-agent-messages/outcome")
        value = json.loads(packet.content)
        calls.append(value)
        response = NativeRelayAcceptance(
            message_id=request.source.message_id, recipient_agent_id=request.source.sender.agent_id,
            conversation_id=request.source.sender.conversation_id, submit_digest="b" * 64, status="accepted",
            source_request_id=value["request_id"], source_conversation_id=value["conversation_id"])
        return httpx.Response(202, content=response.model_dump_json().encode())

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(transport))
    executions = []

    async def failed(receipt, *_args):
        executions.append(receipt.request_id)
        raise Problem(503, "NATIVE_TURN_TIMEOUT", "synthetic model turn timeout")

    harness.runtime.executor = failed
    harness.runtime.on_unconfirmed = client.unconfirmed
    assert harness.invoke(request).status_code == 202
    original = harness.wait(request.command.request_id, "unknown")
    deadline = time.monotonic() + 8
    while not calls and time.monotonic() < deadline:
        time.sleep(0.02)
    assert calls == [{"conversation_id": original.conversation_id, "request_id": original.request_id}]
    assert executions == [original.request_id] and original.failure_code == "NATIVE_TURN_TIMEOUT"
    assert original.result is None


def test_full_peer_queue_keeps_the_original_accepted_input_until_capacity_returns(harness, monkeypatch):
    harness.sdk.block = True
    harness.invoke(submit("busy-original"))
    harness.wait("busy-original", "started")
    for number in range(12):
        response = harness.invoke(submit(f"full-queue-{number}"))
        if response.json()["status"] == "busy_refused":
            break
    notices, attempts = [], []
    start = type(harness.runtime.chain).start

    async def counted(task, **options):
        if getattr(options.get("input"), "request_id", None) == "same-full-queue-peer":
            attempts.append(True)
        return await start(task, **options)

    monkeypatch.setattr(type(harness.runtime.chain), "start", counted)

    async def notice(receipt):
        notices.append((receipt.request_id, receipt.status, receipt.failure_code))

    harness.runtime.on_unconfirmed = notice
    request = agent_receive("same-full-queue-peer")
    response = harness.invoke(request)
    assert response.status_code == 202 and response.json()["status"] == "accepted"
    deadline = time.monotonic() + 8
    while len(attempts) <= harness.runtime._queue_retry.max_attempts and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    retained = harness.call(harness.runtime.read, request.command.request_id)
    assert retained.status == "accepted" and notices == []
    assert retained.command == request.command and retained.source == request.source
    assert all(call[0] != request.command.request_id for call in harness.sdk.calls)
    harness.sdk.block = False
    completed = harness.wait(request.command.request_id, "completed")
    assert completed.source == retained.source
    assert [call[0] for call in harness.sdk.calls].count(request.command.request_id) == 1


def test_delegation_receipt_is_persisted_and_not_published_as_the_upstream_final(harness):
    from dataclasses import replace
    from shared_context.behavior_native_turn_wire import NativeHistory

    sdk = harness.runtime.executor
    delegated = NativeRelayAcceptance(
        message_id="c" * 64, recipient_agent_id="child-agent", conversation_id="child-cid",
        submit_digest="d" * 64, status="accepted")

    async def send_onward(receipt, scope, previous, control):
        result = await sdk(receipt, scope, previous, control)
        return replace(result, text="已交給下一位助理。", delegation=delegated)

    def forbidden(_request):
        pytest.fail("a delegation acknowledgment is not the upstream business result")

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(forbidden))
    harness.runtime.executor = send_onward
    harness.runtime.on_completed = client.completed
    request = agent_receive("upstream-original")
    assert harness.invoke(request).status_code == 202
    completed = harness.wait(request.command.request_id, "completed")
    assert completed.delegation == delegated
    assert harness.call(harness.runtime.read, completed.request_id).delegation == delegated
    history = harness.call(harness.runtime.history, NativeHistory(conversation_id=completed.conversation_id))
    assert history.turns[0].delegation == delegated
    assert len(harness.sdk.calls) == 1


def test_cancel_after_child_acceptance_preserves_delegation_before_checkpoint_and_does_not_notify_failure(harness):
    from dataclasses import replace
    from shared_context.behavior_native_turn_wire import NativeCancel
    from test_native_turns import sdk_result

    delegated = NativeRelayAcceptance(
        message_id="e" * 64, recipient_agent_id="child-agent", conversation_id="child-cid",
        submit_digest="f" * 64, status="accepted")
    retained = []

    async def send_then_cancel(receipt, scope, _previous, control):
        harness.sdk.controls[receipt.request_id] = control
        await control.retain_delegation(delegated)
        retained.append(receipt.request_id)
        while not control.stop_requested():
            await asyncio.sleep(0.01)
        return replace(sdk_result(scope, "已交給下一位助理。"), delegation=delegated)

    def forbidden(_packet):
        pytest.fail("an accepted child must not be preempted by an upstream unknown notice")

    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(forbidden))
    harness.runtime.executor = send_then_cancel
    harness.runtime.on_unconfirmed = client.unconfirmed
    request = agent_receive("cancel-after-child")
    assert harness.invoke(request).status_code == 202
    started = harness.wait(request.command.request_id, "started")
    deadline = time.monotonic() + 5
    while not retained and time.monotonic() < deadline:
        time.sleep(0.01)
    assert retained == [started.request_id]
    assert harness.call(harness.runtime.read, started.request_id).delegation == delegated
    assert harness.invoke(NativeCancel(
        conversation_id=started.conversation_id, request_id=started.request_id)).status_code == 200
    stopped = harness.wait(started.request_id, "unknown")
    assert stopped.failure_code == "NATIVE_TURN_CANCELLED" and stopped.delegation == delegated
    assert stopped.result is None


@pytest.mark.parametrize("native_retry_policy", [None], indirect=True)
def test_ten_second_notification_outage_recovers_with_production_delays_without_replay(harness):
    began = time.monotonic()
    delivered, calls = set(), []

    async def recovered(receipt, _reference):
        calls.append(receipt.request_id)
        if time.monotonic() - began < 10:
            raise Problem(503, "NATIVE_RELAY_OUTCOME_UNKNOWN", "synthetic ten-second API outage")
        delivered.add(receipt.request_id)

    harness.runtime.on_completed = recovered
    request = agent_receive("ten-second-outage")
    assert harness.invoke(request).status_code == 202
    original = harness.wait(request.command.request_id, "completed")
    deadline = began + 30
    while not delivered and time.monotonic() < deadline:
        time.sleep(0.05)
    assert delivered == {original.request_id}
    assert len(calls) >= 4 and len(harness.sdk.calls) == 1
    assert harness.call(harness.runtime.read, original.request_id).notification_error is None


@pytest.mark.parametrize("native_retry_policy", [None], indirect=True)
def test_ten_second_full_queue_eventually_executes_original_wake_once(harness):
    harness.sdk.block = True
    harness.invoke(submit("ten-second-active"))
    active = harness.wait("ten-second-active", "started")
    for number in range(12):
        response = harness.invoke(submit(f"long-queued-{number}"))
        if response.json()["status"] == "busy_refused":
            break
    request = agent_receive("ten-second-wake", active.conversation_id)
    assert harness.invoke(request).json()["status"] == "accepted"
    time.sleep(10)
    assert harness.call(harness.runtime.read, request.command.request_id).status == "accepted"
    harness.sdk.block = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = harness.call(harness.runtime.read, request.command.request_id)
        if result.status == "completed":
            break
        time.sleep(0.03)
    assert result.status == "completed"
    assert result.conversation_id == active.conversation_id
    assert sum(call[0] == result.request_id for call in harness.sdk.calls) == 1


def test_a_peer_reply_waits_for_the_original_a_queue_instead_of_being_discarded(harness):
    from dataclasses import replace
    from shared_context.behavior_native_turn_wire import (
        NativeAgentCommand, NativeAgentOrigin, NativeAgentReceive, NativeResultReference, NativeTurnAddress,
    )

    sdk = harness.runtime.executor
    delegated = NativeRelayAcceptance(
        message_id="a" * 64, recipient_agent_id="peer-b", conversation_id="peer-b-cid",
        submit_digest="b" * 64, status="accepted")

    async def original_handoff(receipt, scope, previous, control):
        await control.retain_delegation(delegated)
        return replace(await sdk(receipt, scope, previous, control), delegation=delegated)

    harness.runtime.executor = original_handoff
    harness.invoke(submit("original-a"))
    original = harness.wait("original-a", "completed")
    harness.runtime.executor = sdk
    harness.sdk.block = True
    harness.invoke(submit("busy-other-cid"))
    harness.wait("busy-other-cid", "started")
    for number in range(12):
        queued = harness.invoke(submit(f"root-full-{number}"))
        if queued.json()["status"] == "busy_refused":
            break
    address = NativeTurnAddress(
        agent_id=original.agent_id, conversation_id=original.conversation_id, request_id=original.request_id)
    reference = NativeResultReference(
        turn_number=1, turn_key="turn:1", result_digest="c" * 64, text_sha256="d" * 64,
        text_bytes=10, text_parts=1, checkpoint_sha256="e" * 64)
    wake = NativeAgentReceive(
        command=NativeAgentCommand(
            request_id=delegated.message_id, conversation_id=original.conversation_id, content="Original peer result."),
        capture=original.capture, source=NativeAgentOrigin(
            kind="agent_result", message_kind="result", message_id=delegated.message_id,
            sender=NativeTurnAddress(agent_id="peer-b", conversation_id="peer-b-cid", request_id=delegated.message_id),
            recipient_agent_id=original.agent_id, root_id="f" * 64, root=address, hop=0,
            in_reply_to=delegated.message_id, result=reference, continuation=address))

    async def root_notice(_receipt):
        return None

    harness.runtime.on_unconfirmed = root_notice
    assert harness.invoke(wake).json()["status"] == "accepted"
    time.sleep(0.5)
    waiting = harness.call(harness.runtime.read, wake.command.request_id)
    assert waiting.status == "accepted"
    observed = harness.call(harness.runtime.read, original.request_id)
    assert observed.notification_error is None
    assert observed.result == original.result and observed.status == "completed"
    harness.sdk.block = False
    completed = harness.wait(wake.command.request_id, "completed")
    assert completed.conversation_id == original.conversation_id
    assert [call[0] for call in harness.sdk.calls].count(wake.command.request_id) == 1


@pytest.mark.parametrize("seam", ["sid-read", "chain-start"])
def test_deferred_enqueue_recovers_one_transient_platform_failure(harness, monkeypatch, seam):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from azure.ai.agentserver.core.tasks import SteeringQueueFull
    from azure.core.exceptions import ServiceResponseError

    harness.sdk.block = True
    assert harness.invoke(submit("transient-active")).status_code == 202
    active = harness.wait("transient-active", "started")
    for number in range(12):
        if harness.invoke(submit(f"transient-q-{number}")).json()["status"] == "busy_refused":
            break
    request = agent_receive("same-transient-wake", active.conversation_id)
    start, read = harness.runtime.chain.start, FoundryStateStore.get_item
    seen, fail_sid, injected = [], [], []

    async def starting(**arguments):
        if arguments["input_id"] == request.command.request_id:
            seen.append(arguments["input_id"])
            if seam == "chain-start" and len(seen) == 2:
                injected.append(seam)
                raise ServiceResponseError("synthetic task-store acknowledgment unavailable")
            try:
                return await start(**arguments)
            except SteeringQueueFull:
                if seam == "sid-read" and len(seen) == 1:
                    fail_sid.append(True)
                raise
        return await start(**arguments)

    async def reading(store, key, **kwargs):
        if key == "agent" and fail_sid:
            fail_sid.clear()
            injected.append(seam)
            raise ServiceResponseError("synthetic original-SID read unavailable")
        return await read(store, key, **kwargs)

    monkeypatch.setattr(harness.runtime.chain, "start", starting)
    monkeypatch.setattr(FoundryStateStore, "get_item", reading)
    assert harness.invoke(request).json()["status"] == "accepted"
    harness.sdk.block = False
    completed = harness.wait(request.command.request_id, "completed")
    assert injected == [seam]
    assert completed.conversation_id == active.conversation_id
    assert sum(call[0] == request.command.request_id for call in harness.sdk.calls) == 1


@pytest.mark.parametrize("seam", ["read", "cas"])
def test_child_acceptance_stays_successful_when_retain_is_unconfirmed_then_sender_is_cancelled(harness, monkeypatch, seam):
    from dataclasses import replace
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from azure.core.exceptions import ServiceResponseError
    from shared_context.behavior_agent_message_wire import AgentMessageRecipient
    from shared_context.behavior_native_agent_wire import NativePeerDirectory
    from shared_context.behavior_native_turn_wire import NativeAgentReceive, NativeCancel
    from shared_context.native_agent_tools import NativeAgentTools
    from test_native_turns import sdk_result

    delegated = NativeRelayAcceptance(
        message_id="a" * 64, recipient_agent_id="accepted-child", conversation_id="child-cid",
        submit_digest="b" * 64, status="accepted")
    original = agent_receive("retain-interrupted")
    request = NativeAgentReceive(command=original.command, source=original.source, capture=collaboration_turn()[0].capture)
    read, write = FoundryStateStore.get_item, FoundryStateStore.set_item
    owner_task, injected, accepted, returned = [], [], [], []

    async def reading(store, key, **kwargs):
        if (seam == "read" and not injected and owner_task and asyncio.current_task() is owner_task[0]
                and key == "rid:" + request.command.request_id):
            injected.append(seam)
            raise ServiceResponseError("synthetic retain read unavailable")
        return await read(store, key, **kwargs)

    async def writing(store, key, value, **kwargs):
        if (seam == "cas" and not injected and key == "rid:" + request.command.request_id
                and value["receipt"].get("delegation") is not None and value["receipt"]["status"] == "started"):
            injected.append(seam)
            raise ServiceResponseError("synthetic retain CAS unconfirmed")
        return await write(store, key, value, **kwargs)

    class Client:
        async def directory(self, _conversation_id=None):
            return NativePeerDirectory(agents=(
                AgentMessageRecipient(agent_id="accepted-child", display_name="Child"),))

        async def send(self, receipt, call_id, message):
            accepted.append(call_id)
            owner_task.append(asyncio.current_task())
            return delegated

    async def send_then_cancel(receipt, scope, _previous, control):
        harness.sdk.controls[receipt.request_id] = control
        tools = NativeAgentTools(Client(), receipt, control)
        spec = next(value for value in await tools.prepare() if value.name == "send_agent_message")
        value = await spec.handler({"tool_call_id": "the-original-call", "arguments": {
            "recipient_agent_id": "accepted-child", "kind": "request", "content": "Check the original sample."}})
        returned.append((NativeRelayAcceptance.model_validate_json(value.text), tools.handoff_texts[-1]))
        while not control.stop_requested():
            await asyncio.sleep(0.01)
        return replace(sdk_result(scope, tools.handoff_texts[-1]), delegation=tools.delegation)

    def forbidden(_packet):
        pytest.fail("the confirmed child must not be overwritten by an upstream failure notice")

    monkeypatch.setattr(FoundryStateStore, "get_item", reading)
    monkeypatch.setattr(FoundryStateStore, "set_item", writing)
    harness.runtime.executor = send_then_cancel
    client = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(forbidden))
    harness.runtime.on_unconfirmed = client.unconfirmed
    assert harness.invoke(request).status_code == 202
    started = harness.wait(request.command.request_id, "started")
    deadline = time.monotonic() + 3
    while not returned and time.monotonic() < deadline:
        time.sleep(0.01)
    assert injected == [seam] and accepted == ["the-original-call"]
    assert returned and returned[0][0] == delegated and returned[0][1].startswith("已交給")
    assert harness.invoke(NativeCancel(
        conversation_id=started.conversation_id, request_id=started.request_id)).status_code == 200
    cancelled = harness.wait(started.request_id, "unknown")
    assert cancelled.delegation == delegated and cancelled.failure_code == "NATIVE_TURN_CANCELLED"


def test_deferred_task_store_retry_exhaustion_closes_original_accepted_input_and_notifies(harness, monkeypatch):
    from azure.core.exceptions import ServiceResponseError

    harness.sdk.block = True
    assert harness.invoke(submit("exhaust-active")).status_code == 202
    active = harness.wait("exhaust-active", "started")
    for number in range(12):
        response = harness.invoke(submit(f"exhaust-queued-{number}"))
        assert response.status_code == 202, response.text
        if response.json()["status"] == "busy_refused":
            break
    request = agent_receive("exhaust-original-peer", active.conversation_id)
    start, calls, notices = harness.runtime.chain.start, [], []

    async def unavailable(**arguments):
        if arguments["input_id"] == request.command.request_id:
            calls.append(arguments["input_id"])
            if len(calls) > 1:
                raise ServiceResponseError("synthetic sustained task-store failure")
        return await start(**arguments)

    async def notice(receipt):
        notices.append((receipt.request_id, receipt.status, receipt.failure_code))

    monkeypatch.setattr(harness.runtime.chain, "start", unavailable)
    harness.runtime.on_unconfirmed = notice
    assert harness.invoke(request).json()["status"] == "accepted"
    failed = harness.wait(request.command.request_id, "unknown")
    deadline = time.monotonic() + 3
    while not notices and time.monotonic() < deadline:
        time.sleep(0.01)
    assert notices == [(request.command.request_id, "unknown", "NATIVE_TURN_UNKNOWN")]
    assert len(calls) == 11 and failed.command == request.command and failed.source == request.source
    assert all(call[0] != request.command.request_id for call in harness.sdk.calls)
    harness.sdk.block = False
