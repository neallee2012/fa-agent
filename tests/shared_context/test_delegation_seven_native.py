"""Seven captured capabilities, real SDK/API hooks, synthetic services/provider only."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import jsonschema
import pytest
from fastapi.testclient import TestClient

from sdk_checkpoint_local import sse
from shared_context import behavior_assistant_hosted as hosted, mcp_client
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_delegation_wire import DelegationReply, DelegationTask
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_capability_live_mix_native import live_like_catalog, refusing_desktop
from test_capability_proxy_native import KEY, served
from test_conversation_profile_selection import configured, select
from test_instruction_flow import instruction_models
from test_instruction_prompt import submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_skill_purpose_api import add_purpose_skill
from test_skills_capability import skill_template


@pytest.mark.parametrize("legal", [True, False], ids=["fixed-child", "stock-agent-refused"])
def test_seven_capabilities_declare_only_the_executable_task_contract(assistant, monkeypatch, signing_key,
                                                                    jwk_client, caplog, legal):
    version = add_purpose_skill(assistant, signing_key, jwk_client, name="receipt-guide")
    h, control, human = configured(assistant)
    catalog = live_like_catalog()
    grants = available_grants(catalog, ("fa-desktop-v1", "fa-delegate-v1", "fa-memory-candidate-v1",
                                      "fa-skillify-v1", "fa-skills-v1"))
    assert len(grants) == 7
    h.runtime.capability_grants, h.runtime.capability_catalog = grants, catalog
    model = control.models.default
    control.models.models[model] = control.models.models[model].model_copy(update={"supports_image_input": True})
    monkeypatch.setitem(hosted.CAPABILITY_HANDLERS, "fa-desktop-v1", refusing_desktop)
    monkeypatch.setenv("FA_CAPABILITY_SECRET_WEBIQ_API_KEY", KEY)
    mcp_calls = []
    monkeypatch.setattr(mcp_client, "list_tools", served(mcp_client.list_tools, mcp_calls))
    monkeypatch.setattr(mcp_client, "call_tool", served(mcp_client.call_tool, mcp_calls))
    selected = skill_template(version, capabilities=tuple(grants[name].ref for name in sorted(grants)))
    choice, _ = select(h, request_id="seven-capabilities", value=selected)
    assert choice.status_code == 200, choice.text
    requests, events, sessions = [], [], []
    original_event, initialize = hosted.AssistantSession._public_event, hosted.AssistantSession.__init__

    def initialized(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        sessions.append(instance)
    def event(instance, packet):
        events.append(packet)
        return original_event(instance, packet)
    monkeypatch.setattr(hosted.AssistantSession, "__init__", initialized)
    monkeypatch.setattr(hosted.AssistantSession, "_public_event", event)

    def provider(request):
        body = json.loads(request.content)
        tools = {tool["function"]["name"]: tool["function"] for tool in body.get("tools", [])}
        requests.append(set(tools))
        number = len(requests)
        if number == 1:
            assert {"task", "read_skill", "save_skill_draft", "stage_memory_candidate",
                    "desktop_act", "WebIQ-MCP-web", "playwright-browser_navigate"} <= set(tools)
            schema = tools["task"]["parameters"]
            assert schema == DelegationTask.model_json_schema()
            arguments = {"name": "one-child", "description": "Synthetic HTTP comparison",
                         "prompt": "List GET, PUT and DELETE.",
                         "agent_type": "fa-delegate-v1" if legal else "task", "mode": "sync"}
            if legal:
                jsonschema.validate(arguments, schema)
            else:
                with pytest.raises(jsonschema.ValidationError):
                    jsonschema.validate(arguments, schema)
            delta = {"tool_calls": [{"index": 0, "id": "seven-task", "type": "function",
                                     "function": {"name": "task", "arguments": json.dumps(arguments)}}]}
        elif legal and number == 2:
            assert tools == {}
            delta = {"content": "GET, PUT and DELETE are idempotent."}
        else:
            assert number == (3 if legal else 2) and "task" in tools
            delta = {"content": "Parent integrated the original tool outcome."}
        return sse(request, delta, number)

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="SYNTHETIC_SEVEN_PROVIDER")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: None, require_current_native_evidence=lambda: None))
    parent = Path(__file__).resolve().parents[2] / ".azure"
    parent.mkdir(exist_ok=True)
    with TestClient(h.app) as client, TemporaryDirectory(prefix="seven-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory)
        accepted, _ = submit(h, "seven-fixed" if legal else "seven-refused", "Delegate the synthetic comparison.")
        finished = h.runtime.controller.step(accepted["binding_id"])
        assert finished.state == "succeeded" and finished.result_forwarded
        readback = client.get(f"/work-policy/v1/conversations/{accepted['conversation_id']}/turns/1/delegation",
                              headers=h.headers("human"))
        if legal:
            assert readback.status_code == 200, readback.text
            retained = DelegationReply.model_validate_json(readback.content)
            assert retained.status == "completed"
            assert retained.tool_call_id == "seven-task" and retained.result == "GET, PUT and DELETE are idempotent."
        else:
            assert readback.status_code == 204, readback.text
            assert "reason=unsupported_arguments" in caplog.text
        task_events = [packet["public_progress"] for packet in events
                       if packet.get("public_progress", {}).get("tool_name") == "task"]
        assert [item["kind"] for item in task_events] == ["tool_started", "tool_completed"]
        assert task_events[-1]["status"] == ("succeeded" if legal else "failed")
        assert len(sessions) == 1 and sessions[0]._sdk_stop.graceful
        assert mcp_calls == ["list_tools"]
        assert KEY not in caplog.text
