"""Pinned SDK/runtime -> signed FA API/store -> scripted provider; no network or real model."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from copilot import CopilotClient
from fastapi.testclient import TestClient

from shared_context import m2_copilot
from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_capability_prompt import session_needs
from shared_context.behavior_delegation_runtime import DelegationRuntime
from shared_context.behavior_delegation_wire import DelegationReply
from shared_context.behavior_delegation import DelegationLedger, capture_parent
from shared_context.errors import Problem
from shared_context.behavior_instruction_cost import cost_key
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.behavior_wire import Command
from sdk_checkpoint_local import sse
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models
from test_instruction_prompt import saved_turn as read_saved_turn, submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn
from test_skill_isolation_native import child_environment


TASK = {"name": "synthetic-checker", "description": "Compare the supplied Team fragments",
        "prompt": "Synthetic approved Team A v1: total 850, assigned 320. Team B v1: remaining 530.",
        "agent_type": "fa-delegate-v1", "mode": "sync"}


def test_hosted_turn_registers_sdk_builtin_and_saves_only_the_parent_final(assistant, monkeypatch, tmp_path):
    h, control, human = configured(assistant)
    grants = available_grants(None, ("fa-delegate-v1",))
    h.runtime.capability_grants = grants
    chosen, _ = select(h, value=template("fa-instruction-history-v1", user_text_file_profile="fa-user-text-files-v1",
                                       capabilities=(grants["fa-delegate-v1"].ref,)))
    assert chosen.status_code == 200, chosen.text
    requests, sessions = [], []
    initialize = hosted.AssistantSession.__init__

    def initialized(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        sessions.append(instance)

    class PrivateClient(CopilotClient):
        def __init__(self, *args, **kwargs):
            kwargs["env"] = child_environment(tmp_path / "private")
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(hosted.AssistantSession, "__init__", initialized)
    monkeypatch.setattr(m2_copilot, "CopilotClient", PrivateClient)

    def respond(request):
        body = json.loads(request.content)
        tools = {item["function"]["name"] for item in body.get("tools", [])}
        requests.append(tools)
        count = len(requests)
        if count == 1:
            assert tools == {"task", "read_context", "execute_operation", "read_user_turn", "read_user_file", "read_assistant_reply"}
            delta = {"tool_calls": [{"index": 0, "id": "hosted-task", "type": "function",
                                    "function": {"name": "task", "arguments": json.dumps(TASK)}}]}
        elif count == 2:
            assert tools == set()
            delta = {"content": "Child public result: 530."}
        elif count == 3:
            assert "task" in tools
            value = sessions[0]._delegation.reply
            assert value.status == "completed" and value.result == "Child public result: 530."
            delta = {"content": "Parent final: 530 remaining."}
        else:
            assert count == 4 and "task" in tools
            assert "Parent final: 530 remaining." in json.dumps(body["messages"])
            delta = {"content": "Continued the same conversation without spawning another child."}
        return sse(request, delta, count)

    provider = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="SYNTHETIC_DELEGATION_PROVIDER")),
        transport=httpx.MockTransport(respond))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=provider)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant), installation=lambda: None, require_current_native_evidence=lambda: None))
    with TestClient(h.app) as client:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        accepted, _ = submit(h, "hosted-wired-delegation", "Delegate the comparison, then summarize the child result.")
        outcome = h.runtime.controller.step(accepted["binding_id"])
        assert outcome.state == "succeeded" and outcome.result_forwarded
        assert len(requests) == 3 and len(sessions) == 1
        assignment = sessions[0].client.assignment
        readback = client.post(
            f"/behavior/v1/bindings/{assignment.binding_id}/assistant/sessions/{assignment.session_id}/delegation-read",
            content=Command(request_id="delegation-read").model_dump_json(),
            headers={**h.headers("agent"), "x-behavior-assistant-ticket": sessions[0].client.ticket.get_secret_value()})
        assert readback.status_code == 200, readback.text
        retained = DelegationReply.model_validate_json(readback.content)
        assert retained.status == "completed" and retained.tool_call_id == "hosted-task"
        assert sessions[0]._sdk_stop.graceful
        archive = read_saved_turn(h, h.runtime.bindings[accepted["binding_id"]].job_id)
        assert archive.capability_assembly.declared_capabilities == (
            "read_context", "execute_operation", "read_user_turn", "read_user_file", "read_assistant_reply", "task")
        assert archive.failure is None
        reply = control.read_reply(human, accepted["conversation_id"], 1)
        assert reply.text == "Parent final: 530 remaining."
        following, _ = submit(h, "hosted-delegation-followup", "Continue without another task.", accepted["conversation_id"])
        continued = h.runtime.controller.step(following["binding_id"])
        assert continued.state == "succeeded" and continued.result_forwarded
        assert len(requests) == 4 and len(sessions) == 2
        assert sessions[1].client.assignment.sdk_session_id == assignment.sdk_session_id
        assert sessions[1]._delegation.child_id is None and sessions[1]._sdk_stop.graceful


@pytest.mark.parametrize("scenario", ["completed", "large-material", "cancelled", "background", "model-override"])
def test_pinned_task_saves_original_child_and_root_usage_before_parent_continues(
        assistant, monkeypatch, tmp_path, runtime_path, scenario):
    cancelled = scenario == "cancelled"
    refused = scenario in {"background", "model-override"}
    h, control, human = configured(assistant)
    grants = available_grants(None, ("fa-delegate-v1",))
    h.runtime.capability_grants = grants
    chosen, _ = select(h, value=template("fa-instruction-history-v1", user_text_file_profile="fa-user-text-files-v1",
                                       capabilities=(grants["fa-delegate-v1"].ref,)))
    assert chosen.status_code == 200, chosen.text
    observed, reservations, sessions, stopped = [], [], [], []
    child_ready, upstream_cancelled = asyncio.Event(), asyncio.Event()
    bridge = None
    parent = None
    child_request_sha = None

    class PrivateClient(CopilotClient):
        def __init__(self, *args, **kwargs):
            kwargs["env"] = child_environment(tmp_path / "private")
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(m2_copilot, "CopilotClient", PrivateClient)

    async def respond(request):
        nonlocal child_request_sha
        body = json.loads(request.content)
        tools = {item["function"]["name"] for item in body.get("tools", [])}
        observed.append(tools)
        number = len(observed)
        if number == 1:
            assert tools == {"task"}
            arguments = {**TASK, **({"mode": "background"} if scenario == "background" else
                                   {"model": "gpt-4.1-mini"} if scenario == "model-override" else
                                   {"prompt": "Synthetic approved Team material.\n" * 1200}
                                   if scenario == "large-material" else {})}
            delta = {"tool_calls": [{"index": 0, "id": "original-task-call", "type": "function",
                                    "function": {"name": "task", "arguments": json.dumps(arguments)}}]}
        elif refused:
            assert number == 2 and tools == {"task"} and bridge.child_id is None
            assert any(item["role"] == "tool" for item in body["messages"])
            delta = {"content": "Delegation was refused before a child started."}
        elif number == 2:
            assert tools == set()
            assert bridge.child_id is not None
            persisted = DelegationLedger(h.store).read(parent)
            assert persisted.status == "running" and persisted.child_agent_id == bridge.child_id
            from shared_context.behavior_delegation import usage_key
            child_request_sha = reservations[-1]
            link_id = usage_key(cost_key(parent.session_id, child_request_sha))
            assert h.store.read_snapshot((link_id,))[link_id] is not None
            if cancelled:
                child_ready.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    upstream_cancelled.set()
                    raise
            delta = {"content": "Complete child result: 530 remain; Team A v1 and Team B v1."}
        else:
            assert number == 3 and tools == {"task"}
            original = DelegationLedger(h.store).read(parent)
            assert original.status == "completed"
            assert h.app.state.instruction_live_replies.read(parent.execution.job_id) is None
            text = next(item["content"] for item in reversed(body["messages"]) if item["role"] == "tool")
            if isinstance(text, list):
                text = "".join(item["text"] for item in text if item.get("type") == "text")
            result = DelegationReply.model_validate_json(text)
            assert result.tool_call_id == "original-task-call" and result.child_agent_id == original.child_agent_id
            assert result.result == original.result
            delta = {"content": "Parent integrated the original child result."}
        return sse(request, delta, number)

    provider = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="SYNTHETIC_DELEGATION_PROVIDER")),
        transport=httpx.MockTransport(respond))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=provider)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant), installation=lambda: None, require_current_native_evidence=lambda: None))
    with TestClient(h.app) as http:
        h.client = http
        turn = open_turn(h, control, human, "real-sdk-delegation")
        turn.client.model_async_transport = httpx.ASGITransport(app=h.app)
        parent = capture_parent(h.assistant, turn.session, capability_sha256=grants["fa-delegate-v1"].entry_sha256)
        task = turn.assignment.task.instruction

        async def execute():
            nonlocal bridge
            async def current():
                await asyncio.to_thread(turn.client.capability_current)
                return True
            async def call(action, command):
                return await asyncio.to_thread(turn.client.delegation, action, command)
            async def reserve(identity):
                reservations.append(identity)
                await asyncio.to_thread(turn.client.reserve_model, identity)
            bridge = DelegationRuntime(turn.binding.job_id, turn.assignment.sdk_session_id, call, current)
            boundary = AssistantModelBoundary(reserve, sender=turn.client.send_model, text_reply=True, streaming=True)
            (tmp_path / "work").mkdir(exist_ok=True)
            running = asyncio.create_task(m2_copilot.run_copilot(
                state_dir=tmp_path / "sdk", working_directory=tmp_path / "work",
                sdk_session_id=turn.assignment.sdk_session_id, resume=False,
                prompt=task.content, tools=[], api_url="https://context.invalid",
                token_provider=lambda: "SYNTHETIC_NO_RUNTIME_CREDENTIAL", on_event=lambda _: None,
                runtime_path=runtime_path, behavior_boundary=boundary, timeout_seconds=None, instruction=True,
                model=task.model, system_message="Synthetic read-only delegation task.",
                capability_session=session_needs(task.capabilities), capability_current=current,
                delegation_runtime=bridge, session_files=SdkSessionFiles(turn.assignment.sdk_session_id),
                on_session_ready=sessions.append, on_sdk_stopped=stopped.append))
            if not cancelled:
                return await running
            try:
                await asyncio.wait_for(child_ready.wait(), timeout=30)
                await asyncio.to_thread(control.cancel, human, turn.binding.binding_id)
                await bridge.request_cancel()
                pending = DelegationLedger(h.store).read(parent)
                assert pending.cancel_requested and not pending.stop_confirmed
                await asyncio.wait_for(sessions[0].abort(), timeout=10)
                with pytest.raises(RuntimeError):
                    await asyncio.wait_for(running, timeout=20)
                assert upstream_cancelled.is_set()
            finally:
                if not running.done():
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
        try:
            outcome = asyncio.run(execute())
            assert len(stopped) == 1 and stopped[0]["graceful"]
            if refused:
                assert len(observed) == 2 and bridge.child_id is None and bridge.tool_call_id is None
                with pytest.raises(Problem) as missing:
                    DelegationLedger(h.store).read(parent)
                assert missing.value.body["error"]["code"] == "DELEGATION_UNKNOWN"
                assert outcome["final_message"] == "Delegation was refused before a child started."
                return
            original = DelegationLedger(h.store).read(parent)
            usage = DelegationLedger(h.store).read_model_usage(parent, child_request_sha)
            if cancelled:
                assert len(observed) == 2 and original.status == "cancelled" and original.stop_confirmed
                assert original.child_cancelled and original.parent_idle
                assert usage.stage == "pending" and usage.input_tokens is None and usage.pending_microusd > 0
            else:
                assert outcome["sdk_stop"]["graceful"]
                assert outcome["final_message"] == "Parent integrated the original child result."
                assert len(observed) == 3 and original.status == "completed"
                assert usage.stage == "usage" and (usage.input_tokens, usage.output_tokens) == (1500, 100)
            assert usage.job_id == turn.binding.job_id
        finally:
            turn.client.close()
