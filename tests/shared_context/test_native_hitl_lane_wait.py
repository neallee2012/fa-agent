"""Both real Hosted handlers through HITL; SDK/model/tool I/O only is synthetic."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import subprocess
from threading import Event
import time
from types import SimpleNamespace
from unittest.mock import create_autospec

import httpx
import pytest
from copilot import CopilotClient
from copilot.client import GetStatusResponse
from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.session import CopilotSession
from copilot.session_events import AssistantMessageData, SessionIdleData

from conftest import A, TENANT
from shared_context import m2_copilot, native_sdk
from shared_context.behavior_native_turn_wire import NativeResult, NativeSubmit
from shared_context.m2_copilot import ToolOutput, ToolSpec
from shared_context.native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider
from shared_context.native_effects import NativeEffects
from shared_context.native_hitl_wire import NativeHitlPendingQuery, NativeHitlReadback, NativeHitlTurn, hitl_key
from test_m3_auth import jwk_client, signing_key
from test_native_capabilities import OfflineResult, selected_capture
from test_native_hitl import change, decision, hitl, post, read
from test_native_hitl_h2 import MODEL_ROOT, api_client, permission_request, verdict
from test_native_responses import response_submit, responses
from test_native_turn_wire import submit
from test_native_turns import IDENTITY, local_env


def sdk_wait_io(monkeypatch, *, model_io=False):
    calls, sessions = [], []

    def denied(*args, **kwargs):
        pytest.fail("no SDK executable or real subprocess is permitted")

    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", denied)

    def factory(**connection):
        client = create_autospec(CopilotClient, instance=True)
        exited = Event()
        client._cli_process = SimpleNamespace(pid=999, poll=lambda: 0 if exited.is_set() else None, returncode=0)
        client.stop.side_effect = exited.set
        client.get_status.return_value = GetStatusResponse(version="1.0.83", protocol_version=2)

        async def create(**options):
            sid = options["session_id"]
            session = create_autospec(CopilotSession, instance=True)
            session.session_id = sid
            session.get_events.return_value = []
            handlers = []

            def subscribe(handler):
                handlers.append(handler)
                return lambda: handlers.remove(handler)

            session.on.side_effect = subscribe
            sessions.append(session)

            async def model(prompt, phase):
                body = {"model": options["model"], "stream": True, "input": [{"role": "user", "content": prompt}]}
                response = await connection["request_handler"].send_request(
                    httpx.Request("POST", options["provider"]["base_url"] + "/responses", json=body),
                    SimpleNamespace(request_id="offline-execution-" + phase, session_id=sid))
                assert response.status_code == 200
                await response.aread()
                await response.aclose()

            async def send(prompt, **kwargs):
                tool = next(tool for tool in options["tools"] if tool.name == "run_command")
                assert tool.skip_permission is False
                packet = {"session_id": sid, "tool_call_id": "lane-ask-call",
                          "tool_name": tool.name, "arguments": {"command": "echo synthetic"}}
                call = {"packet": packet, "prompt": prompt, "permission_returned": False, "provider_returned": False}
                calls.append(call)
                if model_io:
                    await model(prompt, "before")
                request = permission_request(packet)
                waiting = options["on_permission_request"](request, {"session_id": sid})
                assert inspect.isawaitable(waiting)
                assert isinstance(await waiting, PermissionDecisionApproveOnce)
                call["permission_returned"] = True
                output = await tool.handler(SimpleNamespace(**packet))
                assert output.result_type == "success"
                assert json.loads(output.text_result_for_llm)["text"] == "original effect result"
                call["provider_returned"] = True
                assert isinstance(await options["on_permission_request"](request, {"session_id": sid}),
                                  PermissionDecisionReject)
                assert (await tool.handler(SimpleNamespace(**packet))).result_type == "failure"
                if model_io:
                    await model("The original synthetic effect returned.", "after")
                fs = options["create_session_fs_handler"](SimpleNamespace(session_id=sid))
                await fs.write_file("/session-state/events.jsonl", json.dumps(
                    {"type": "session.start", "data": {"sessionId": sid}}) + "\n")
                await fs.write_file("/session-state/workspace.yaml", "id: " + sid + "\n")
                final = SimpleNamespace(data=AssistantMessageData(
                    content="OFFLINE approved original call", message_id="offline-answer"))
                for handler in handlers:
                    handler(final)
                    handler(SimpleNamespace(data=SessionIdleData()))
                return "offline-original-message"

            session.send.side_effect = send
            return session

        client.create_session.side_effect = create
        return client

    monkeypatch.setattr(m2_copilot, "CopilotClient", create_autospec(CopilotClient, side_effect=factory))
    return calls, sessions


@pytest.mark.parametrize("lane", ["foundry-native-v1", "foundry-responses-v1"])
@pytest.mark.parametrize("choice,review_failure", [
    ("allow_once", None), ("allow_rule", None), ("allow_once", "unavailable"), ("allow_once", "stream_unknown"),
])
def test_each_lane_waits_for_signed_owner_then_executes_the_same_call_once(
        hitl, responses, tmp_path, monkeypatch, lane, choice, review_failure):
    hitl.now = datetime.now(timezone.utc)
    original_scope = hitl.config.agent_scope
    hitl.config.targets += (SimpleNamespace(agent_id=IDENTITY.agent_id),)
    hitl.config.agent_scope = lambda identifier: (
        SimpleNamespace(tenant_id=TENANT, team_id=A.team, identity=A) if identifier == IDENTITY.agent_id
        else original_scope(identifier))
    client = api_client(hitl)
    assert post(hitl, "/rules", change(
        hitl, "original-ask", text="Ask before running the synthetic command.", behavior="ask_first")).status_code == 200
    sdk_calls, sessions = sdk_wait_io(monkeypatch, model_io=True)
    effects, reviews, executions = [], [], []

    async def bind(context, grants):
        async def effect(packet):
            effects.append((context.receipt.request_id, packet["session_id"], packet["tool_call_id"]))
            return ToolOutput(OfflineResult(text="original effect result").model_dump_json())

        return NativeCapabilityBinding(
            tools=(ToolSpec("run_command", "OFFLINE effect", {
                "type": "object", "properties": {"command": {"type": "string"}},
                "required": ["command"], "additionalProperties": False,
            }, effect),), result_schemas={"run_command": OfflineResult})

    def model(request):
        body = json.loads(request.content)
        is_review = body.get("text", {}).get("format", {}).get("name") == "fa_auto_review"
        if is_review:
            assert "tools" not in body and "tool_choice" not in body
            reviews.append(body)
            payload = json.loads(body["input"][0]["content"])
            text = json.dumps(verdict("allow", [rule["rule_id"] for rule in payload["rules"]]))
            counts = (7, 3) if payload["rules_revision"] == 1 else (11, 5)
            if review_failure == "unavailable":
                return httpx.Response(503, json={"error": {"code": "OFFLINE-review-unavailable"}})
            if review_failure == "stream_unknown":
                class Interrupted(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n'
                        raise httpx.ReadError("OFFLINE review stream lost")
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Interrupted())
        else:
            executions.append(body)
            counts = (100, 10) if len(executions) == 1 else (200, 20)
            text = "OFFLINE execution response"
        event = {"type": "response.completed", "response": {
            "status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": counts[0], "output_tokens": counts[1]}}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=("data: " + json.dumps(event) + "\n\n").encode())

    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=Path(__file__),
        api_url="https://api.offline.invalid", model_url=MODEL_ROOT, token_provider=lambda: "OFFLINE",
        model_transport=httpx.MockTransport(model), hitl_client=client,
        capability_providers=(NativeCapabilityProvider(
            accepts=lambda grant: grant.entry_id == "fa-shell-v1", bind=bind),))
    executor.reader = responses.runtime
    responses.runtime.executor, responses.runtime.preflight = executor, executor.preflight
    original = response_submit() if lane == "foundry-responses-v1" else submit()
    data = original.model_dump(mode="json")
    data["capture"] = selected_capture("fa-shell-v1").model_dump(mode="json")
    command = NativeSubmit.model_validate_json(json.dumps(data))
    accepted = responses.invoke(command)
    assert accepted.status_code == 202, accepted.text
    accepted = responses.call(responses.runtime.read, "rid-1")
    response_id = None
    if lane == "foundry-responses-v1":
        response = responses.respond(accepted)
        assert response.status_code == 200, response.text
        response_id = response.json()["id"]

    turn = NativeHitlTurn(agent_id=IDENTITY.agent_id, conversation_id=accepted.conversation_id, request_id="rid-1")
    query = NativeHitlPendingQuery(turn=turn)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        page = post(hitl, "/turn-pending", query)
        assert page.status_code == 200, page.text
        if page.json()["attention"]:
            break
        time.sleep(0.01)
    else:
        pytest.fail("the original lane did not persist an ask while awaiting its owner")
    assert len(page.json()["attention"]) == len(page.json()["references"]) == 1
    reference = page.json()["references"][0]
    pending = NativeHitlReadback.model_validate_json(read(hitl, reference["pending_id"]).content)
    action = pending.pending.command.action
    started = responses.call(responses.runtime.read, "rid-1")
    assert started.status == "started" and started.state_profile == lane
    assert pending.status == "pending" and pending.decision is pending.authorization is None
    assert pending.pending.command.review.outcome == "ask" and pending.pending.command.review.rules_revision == 1
    assert len(sdk_calls) == len(reviews) == len(sessions) == 1
    assert sdk_calls[0]["permission_returned"] is sdk_calls[0]["provider_returned"] is False
    assert effects == []
    assert (action.call.request_id, action.call.conversation_id, action.call.sdk_session_id, action.call.tool_call_id) == (
        "rid-1", accepted.conversation_id, sessions[0].session_id, "lane-ask-call")
    reviewed = json.loads(reviews[0]["input"][0]["content"])
    assert reviewed["action"] == action.model_dump(mode="json")
    assert reviewed["verified_human_request"]["initial"] == command.command.content
    material = {"kind": "native-hitl-action-v1", "submit_digest": started.submit_digest,
                "capture_digest": started.capture_digest, "action": action.model_dump(mode="json")}
    ledger = NativeEffects(responses.runtime, started)
    before = responses.call(ledger.read, "lane-ask-call", material)
    assert before.status == "pending" and before.hitl.authorization is None

    chosen = decision(hitl, reference, request_id="signed-owner-once", choice=choice)
    decisions = []
    if choice == "allow_rule":
        state_key = hitl_key("state", hitl.owner.tenant_id, hitl.owner.team, reference["pending_id"])

        def state_read(row_key):
            if row_key != "behavior:v1:row:" + state_key:
                hitl.table.after_get = state_read
                return
            with ThreadPoolExecutor(max_workers=1) as owner:
                decisions.append(owner.submit(post, hitl, "/decisions", chosen, hitl.owner).result())
            assert decisions[0].status_code == 200

        hitl.table.after_get = state_read
    else:
        decisions.append(post(hitl, "/decisions", chosen, hitl.owner))
        assert decisions[0].status_code == 200, decisions[0].text
    completed = responses.wait("rid-1", "completed")
    after = responses.call(ledger.read, "lane-ask-call", material)
    final = NativeHitlReadback.model_validate_json(read(hitl, reference["pending_id"]).content)
    assert effects == [("rid-1", sessions[0].session_id, "lane-ask-call")]
    assert after.status == "done" and json.loads(after.result["text"]) == {"text": "original effect result"}
    assert after.token == before.token == final.authorization.effect_token
    assert after.hitl.action == action and final.authorization.call == action.call
    assert final.decision == chosen and final.grant.uses == 1 and final.turn_closed
    assert completed.response_id == response_id
    assert completed.command == command.command
    result = responses.invoke(NativeResult(conversation_id=completed.conversation_id, request_id="rid-1"))
    assert result.status_code == 200 and result.json()["text"] == "OFFLINE approved original call"

    assert post(hitl, "/decisions", chosen, hitl.owner).json() == decisions[0].json()
    assert post(hitl, "/decisions/reconcile", chosen, hitl.owner).status_code == 200
    assert responses.invoke(command).status_code == 202
    assert responses.call(ledger.read, "lane-ask-call", material) == after
    assert effects == [("rid-1", sessions[0].session_id, "lane-ask-call")]
    assert len(sdk_calls) == 1 and len(reviews) == (2 if choice == "allow_rule" else 1)
    assert final.authorization.review.rules_revision == (2 if choice == "allow_rule" else 1)
    assert final.authorization.review.outcome == "ask"
    assert len(executions) == 2
    usage = completed.usage.usage[0]
    expected_requests = 4 if choice == "allow_rule" else 3
    expected_measured = expected_requests if review_failure is None else 2
    assert (usage.requests, usage.measured_requests) == (expected_requests, expected_measured)
    assert (usage.input_tokens, usage.output_tokens) == (
        (318, 38) if choice == "allow_rule" else (307, 33) if review_failure is None else (300, 30))
    assert (responses.call(responses.runtime.read, "rid-1")).usage == completed.usage
    assert sdk_calls[0]["permission_returned"] is sdk_calls[0]["provider_returned"] is True
    sessions[0].send.assert_awaited_once()
    if lane == "foundry-responses-v1":
        assert len(responses.contexts["rid-1"]) == 1
        assert responses.response_result(response_id)["status"] == "completed"
