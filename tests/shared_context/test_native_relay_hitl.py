"""Real signed relay/HITL routes and model feedback; only transport, model and StateStore I/O is synthetic."""

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from azure.core.exceptions import AzureError
from azure.ai.agentserver.core.tasks import Task

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_native_agent_wire import NATIVE_AGENT_PREFIX, NativeRelayAcceptance, NativeRelaySend
from shared_context.behavior_native_sessions import native_sessions
from shared_context.behavior_native_turn_wire import NativeSubmit
from shared_context.m2_copilot import ToolFailure
from shared_context.native_agent_tools import NativeAgentRelayClient, NativeAgentTools
from shared_context.native_capabilities import guard_native_tool
from shared_context.native_hitl_runtime import NativeHitlRuntime
from shared_context.native_operations import NativeOperationGuard
from test_native_agent_api import (
    hide_receiver_session, instruction_models, jwk_client, peer_runtime,
    relay_case, relay_client_http, relay_http, signing_key, synthetic_standing_v1,
)
from test_native_hitl import hitl
from test_native_hitl_h2 import human_decide, pending_view, permit, setup_gate, verdict


def relay_with_hitl(hitl, relay_client_http, tmp_path, *, ask=False):
    runner, (relay, api, agent_headers, _human_headers) = relay_client_http
    case = setup_gate(hitl, tmp_path, (verdict("ask" if ask else "allow"),),
                      entries=("fa-agent-collaboration-v1",))
    capture = case.context.receipt.capture
    submitted = NativeSubmit(command=relay.sender.command, capture=capture)
    relay.sender = relay.sender.model_copy(update={
        "capture": capture, "capture_digest": capture.sha256, "submit_digest": submitted.sha256,
    })
    relay.remotes[0].records[relay.sender.request_id] = relay.sender
    case.context.receipt = case.runtime.receipt = relay.sender
    case.context.scope.agent_id = case.runtime.identity.agent_id = relay.sender.agent_id
    case.context.scope.conversation_id = relay.sender.conversation_id
    resolve = hitl.config.agent_scope
    hitl.config.targets += (SimpleNamespace(agent_id=relay.sender.agent_id),)
    hitl.config.agent_scope = lambda agent_id: resolve(
        "native-a" if agent_id == relay.sender.agent_id else agent_id)
    case.gate = NativeHitlRuntime(case.context, case.client, case.gate.reviewer, clock=lambda: hitl.now)
    case.context.hitl = case.gate
    requests = []

    def transport(request):
        requests.append(request)
        response = api.request(request.method, request.url.path, content=request.content, headers=agent_headers)
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    async def token():
        return "OFFLINE relay credential"

    client = NativeAgentRelayClient("https://api.offline.invalid", token, transport=httpx.MockTransport(transport))
    tools = NativeAgentTools(client, relay.sender, case.context.control)
    packet = {
        **case.packet, "tool_name": "send_agent_message",
        "arguments": {"recipient_agent_id": relay.base.b.agent_id, "kind": "request",
                      "content": "Check the synthetic VM capacity.", "refs": []},
    }
    return runner, relay, case, tools, requests, packet


def test_once_review_can_finish_with_a_typed_queue_reference_without_an_acceptance(
    hitl, relay_client_http, tmp_path, monkeypatch,
):
    from shared_context.native_relay_delivery import reference_for

    runner, relay, case, tools, requests, packet = relay_with_hitl(hitl, relay_client_http, tmp_path, ask=True)
    queued = []

    async def persist(receipt, call_id, message):
        value = reference_for(receipt, NativeRelaySend(
            conversation_id=receipt.conversation_id, request_id=receipt.request_id,
            tool_call_id=call_id, message=message))
        queued.append(value)
        return value

    monkeypatch.setattr(tools.client, "send", persist)

    async def check():
        spec = next(tool for tool in await tools.prepare() if tool.name == "send_agent_message")
        operations = NativeOperationGuard(case.context.effects, {spec.name: "a" * 64})
        await operations.start()
        await operations.prepare(packet)
        permission = asyncio.create_task(permit(case, packet))
        pending = await pending_view(case)
        assert not queued
        human_decide(case, pending)
        assert await permission == (True, None)
        output = await operations.invoke(packet, guard_native_tool(case.context, spec).handler)
        boundary = AssistantModelBoundary(None, text_reply=True, capability_results=tools.result_schemas)
        assert json.loads(boundary.tool_result("send_agent_message", output.text))["status"] == "queued"
        assert tools.delegation is None and "排隊" in tools.handoff_texts[0]
        assert "已收到" not in tools.handoff_texts[0]
        await operations.invoke(packet, guard_native_tool(case.context, spec).handler)
        assert len(queued) == 1
        await case.gate.close()
        assert (await case.client.read(pending.approval.pending_id)).grant.uses == 1

    runner.run(check())
    assert not any(request.url.path.endswith("/send") for request in requests)
    assert not relay.remotes[1].records


@pytest.mark.parametrize("ask", [False, True])
def test_recipient_session_changed_reaches_model_as_not_sent(
        hitl, relay_client_http, tmp_path, monkeypatch, caplog, ask):
    runner, relay, case, tools, requests, packet = relay_with_hitl(hitl, relay_client_http, tmp_path, ask=ask)
    native_sessions(relay.base.b).prepared = None
    hide_receiver_session(relay, monkeypatch)

    async def check():
        spec = next(tool for tool in await tools.prepare() if tool.name == "send_agent_message")
        operations = NativeOperationGuard(case.context.effects, {spec.name: "a" * 64})
        await operations.start()
        assert await operations.prepare(packet) == "execute"
        pending = None
        permission = asyncio.create_task(permit(case, packet))
        if ask:
            pending = await pending_view(case)
            human_decide(case, pending)
        assert await permission == (True, None)
        with pytest.raises(ToolFailure) as refused:
            await operations.invoke(packet, guard_native_tool(case.context, spec).handler)
        assert refused.value.public_code == "NATIVE_RELAY_RECIPIENT_UNAVAILABLE"
        boundary = AssistantModelBoundary(None, text_reply=True, capability_results={
            "send_agent_message": NativeRelayAcceptance,
        })
        feedback = boundary.public_tool_failure("send_agent_message", refused.value.public_code)
        assert "沒有送出" in feedback and "可以稍後再送一次" in feedback
        assert "NATIVE_HITL_EFFECT_UNKNOWN" not in feedback
        assert tools.delegation is None and tools.handoff_texts == []
        if pending is not None:
            closed = await case.client.read(pending.approval.pending_id)
            assert closed.status == "interrupted"
            assert closed.pending == pending.pending and closed.authorization is not None
            assert closed.grant.uses == 1
            await case.gate.close()
            assert (await case.client.read(pending.approval.pending_id)).status == "interrupted"

    runner.run(check())
    assert "NATIVE_RELAY_RECIPIENT_UNAVAILABLE cause=NATIVE_SESSION_CHANGED" in caplog.text
    assert len([request for request in requests if request.url.path.endswith("/send")]) == 1
    assert not relay.remotes[1].records and not relay.repository.data
    assert not any(command.operation == "agent_receive" for _, command in relay.remotes[1].calls)


def test_lost_ack_then_unavailable_does_not_erase_an_earlier_delivery(
        hitl, relay_client_http, tmp_path, monkeypatch):
    runner, relay, case, tools, requests, packet = relay_with_hitl(hitl, relay_client_http, tmp_path)
    _runner, (_case, api, headers, _human) = relay_client_http
    command = NativeRelaySend.model_validate_json(json.dumps({
        "conversation_id": relay.sender.conversation_id, "request_id": relay.sender.request_id,
        "tool_call_id": packet["tool_call_id"], "message": packet["arguments"],
    }))
    relay.remotes[1].lose_submit = True
    first = api.post(NATIVE_AGENT_PREFIX + "/send", json=command.model_dump(mode="json"), headers=headers)
    assert first.status_code == 503
    relay.remotes[1].lose_submit = False
    saved, quota = copy.deepcopy(relay.remotes[1].records), copy.deepcopy(relay.repository.data)
    native_sessions(relay.base.b).prepared = None
    hide_receiver_session(relay, monkeypatch)

    async def check():
        spec = next(tool for tool in await tools.prepare() if tool.name == "send_agent_message")
        assert await permit(case, packet) == (True, None)
        with pytest.raises(ToolFailure) as refused:
            await guard_native_tool(case.context, spec).handler(packet)
        assert refused.value.public_code == "NATIVE_RELAY_RECIPIENT_UNAVAILABLE"
        assert "先前同 ID" in refused.value.public_message and "不要換 ID 重送" in refused.value.public_message
        assert tools.delegation is None

    runner.run(check())
    assert len(saved) == len(quota) == 1
    assert relay.remotes[1].records == saved and relay.repository.data == quota
    assert len([request for request in requests if request.url.path.endswith("/send")]) == 1
    assert sum(command.operation == "agent_receive" for _, command in relay.remotes[1].calls) == 1


def test_actual_lost_receive_ack_stays_unknown_through_hitl(
        hitl, relay_client_http, tmp_path, monkeypatch):
    runner, relay, case, tools, requests, packet = relay_with_hitl(hitl, relay_client_http, tmp_path, ask=True)
    relay.remotes[1].lose_submit = True
    follows = []

    async def unavailable_follow(_task, **options):
        follows.append(options["input"])
        raise AzureError("OFFLINE follow service unavailable")

    monkeypatch.setattr(Task, "start", unavailable_follow)

    async def check():
        spec = next(tool for tool in await tools.prepare() if tool.name == "send_agent_message")
        permission = asyncio.create_task(permit(case, packet))
        pending = await pending_view(case)
        human_decide(case, pending)
        assert await permission == (True, None)
        with pytest.raises(ToolFailure) as unknown:
            await guard_native_tool(case.context, spec).handler(packet)
        assert unknown.value.public_code == "NATIVE_HITL_EFFECT_UNKNOWN"
        assert "沒有送出" not in unknown.value.public_message
        view = await case.client.read(pending.approval.pending_id)
        assert view.status == "effect_unknown" and view.authorization is not None
        await case.gate.close()
        assert (await case.client.read(pending.approval.pending_id)).status == "effect_unknown"

    runner.run(check())
    assert len(relay.remotes[1].records) == len(relay.repository.data) == 1
    assert len([request for request in requests if request.url.path.endswith("/send")]) == 1
    assert len(follows) == 1 and follows[0].tool_call_id == packet["tool_call_id"]


def test_later_attempt_after_pre_send_refusal_can_send_once(
        hitl, relay_client_http, tmp_path, monkeypatch):
    runner, relay, case, tools, requests, packet = relay_with_hitl(hitl, relay_client_http, tmp_path)
    state = native_sessions(relay.base.b)
    preparation = state.prepared

    async def check():
        spec = next(tool for tool in await tools.prepare() if tool.name == "send_agent_message")
        with monkeypatch.context() as unavailable:
            state.prepared = None
            hide_receiver_session(relay, unavailable)
            assert await permit(case, packet) == (True, None)
            with pytest.raises(ToolFailure) as refused:
                await guard_native_tool(case.context, spec).handler(packet)
            assert refused.value.public_code == "NATIVE_RELAY_RECIPIENT_UNAVAILABLE"
            assert not relay.remotes[1].records
        state.prepared = preparation
        later = {**packet, "tool_call_id": "later-explicit-attempt"}
        assert await permit(case, later) == (True, None)
        result = await guard_native_tool(case.context, spec).handler(later)
        assert NativeRelayAcceptance.model_validate_json(result.text).status == "accepted"
        with pytest.raises(ToolFailure):
            await guard_native_tool(case.context, spec).handler(later)

    runner.run(check())
    assert len(relay.remotes[1].records) == len(relay.repository.data) == 1
    assert len([request for request in requests if request.url.path.endswith("/send")]) == 2
    assert sum(command.operation == "agent_receive" for _, command in relay.remotes[1].calls) == 1


def test_untrusted_same_code_on_a_different_tool_does_not_claim_no_effect(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path, (verdict("allow"),))

    async def handler(_packet):
        raise ToolFailure("not a relay receipt", public_code="NATIVE_RELAY_RECIPIENT_UNAVAILABLE")

    async def check():
        assert await permit(case) == (True, None)
        with pytest.raises(ToolFailure) as unknown:
            await case.gate.invoke(case.packet, handler)
        assert unknown.value.public_code == "NATIVE_HITL_EFFECT_UNKNOWN"

    asyncio.run(check())
