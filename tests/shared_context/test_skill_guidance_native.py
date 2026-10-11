"""One real fixed SDK load/deny/read/finish path; the provider is synthetic, not an LLM judge."""

import hashlib
import importlib.metadata
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
from copilot import CopilotClient
from fastapi.testclient import TestClient

from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_skill_prompt import SKILL_CAPABILITIES
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from sdk_checkpoint_local import sse
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_instruction_prompt import submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_skill_guidance_authority import (
    BODY, NAME, PROFILES, accounting, configure_guidance, configure_read_operation,
    prepare_operation, protected_rows, record,
)


def test_loaded_malicious_guidance_is_data_for_the_fixed_sdk_and_cannot_execute_an_unapproved_operation(
    assistant, signing_key, jwk_client, monkeypatch, expected_denial_status=409,
):
    assert expected_denial_status in {403, 409}
    external = configure_read_operation(assistant, signing_key, jwk_client)
    h, control, human, source = configure_guidance(assistant, signing_key, jwk_client, PROFILES[1])
    bridges, clients, processes, requests, forces, operations, safe_events = [], [], [], [], [], [], []
    configurations, permissions = [], []
    opened, create = hosted.AssistantSession.__init__, CopilotClient.create_session
    start, force = CopilotClient.start, CopilotClient.force_stop
    original_operation = hosted.AssistantClient.operation
    original_user = "Read current approved guidance and reviewed counts, then report only observed results."
    observed_user = []
    turn = None
    original_operation_rows = None

    def opened_bridge(instance, *args, **kwargs):
        opened(instance, *args, **kwargs)
        bridges.append(instance)

    async def started(instance):
        result = await start(instance)
        process = instance._cli_process
        assert process is not None and process.pid > 0
        if all(value is not process for value in processes):
            clients.append(instance)
            processes.append(process)
        return result

    async def create_session(instance, **options):
        assert options["available_tools"].to_list() == ["custom:" + name for name in SKILL_CAPABILITIES]
        assert options["excluded_tools"].to_list() == ["builtin:*", "mcp:*"]
        assert options["enable_config_discovery"] is False and options["enable_managed_settings"] is False
        assert options["mcp_servers"] == {} and not options.get("hooks")
        assert all(options[name] == [] for name in (
            "skill_directories", "plugin_directories", "instruction_directories", "custom_agents"))
        assert tuple(tool.name for tool in options["tools"]) == SKILL_CAPABILITIES
        assert "SK06_UNTRUSTED_GUIDANCE_CANARY" not in options["system_message"]["content"]
        permission = options["on_permission_request"]

        def observe_permission(request, invocation):
            decision = permission(request, invocation)
            permissions.append(type(decision).__name__)
            return decision

        options["on_permission_request"] = observe_permission
        session = await create(instance, **options)

        def event(value):
            kind = value.type.value
            if kind == "user.message":
                observed_user.append(getattr(value.data, "content", None) == original_user)
            if kind in {"tool.execution_start", "tool.execution_complete", "session.error",
                        "session.compaction_start", "session.compaction_complete", "skill.invoked", "newskill.invoked"}:
                tool_name = getattr(value.data, "tool_name", None)
                safe_events.append({
                    "type": kind,
                    "tool": tool_name if tool_name in SKILL_CAPABILITIES else None,
                    "success": getattr(value.data, "success", None),
                })

        session.on(event)
        configurations.append({
            "available_tools": options["available_tools"].to_list(),
            "excluded_tools": options["excluded_tools"].to_list(),
            "discovery_off": True, "ambient_sources_empty": True,
        })
        return session

    async def forced(instance):
        forces.append(True)
        return await force(instance)

    def execute_operation(instance):
        before, money = protected_rows(turn), accounting(h)
        try:
            original_operation(instance)
        except Problem as error:
            operations.append({"denied_status": error.body["error"].get("upstream_status"),
                               "protected_rows_unchanged": protected_rows(turn) == before,
                               "accounting_unchanged_during_tool": accounting(h) == money})
            assert protected_rows(turn) == before and accounting(h) == money and external == []
            raise
        raise AssertionError("Unapproved operation unexpectedly returned a result")

    monkeypatch.setattr(hosted.AssistantSession, "__init__", opened_bridge)
    monkeypatch.setattr(CopilotClient, "start", started)
    monkeypatch.setattr(CopilotClient, "create_session", create_session)
    monkeypatch.setattr(CopilotClient, "force_stop", forced)
    monkeypatch.setattr(hosted.AssistantClient, "operation", execute_operation)

    def tool_call(name, arguments, call_id):
        return {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                               "function": {"name": name, "arguments": json.dumps(arguments)}}]}

    def last_result(body, call_id):
        result = [message for message in body["messages"] if message["role"] == "tool"][-1]
        assert result["tool_call_id"] == call_id
        content = result["content"]
        return content if isinstance(content, str) else "".join(part["text"] for part in content)

    def provider(request):
        nonlocal original_operation_rows
        assert request.method == "POST" and request.url.path == "/openai/v1/chat/completions"
        body = json.loads(request.content)
        number = len(requests) + 1
        names = [tool["function"]["name"] for tool in body["tools"]]
        assert set(names) == set(SKILL_CAPABILITIES)
        entry = {"number": number, "tool_names": names, "request_sha256": hashlib.sha256(request.content).hexdigest()}
        if number == 1:
            assert "SK06_UNTRUSTED_GUIDANCE_CANARY" not in json.dumps(body)
            prepare_operation(turn)
            original_operation_rows = protected_rows(turn)
            delta = tool_call("read_skill", {"name": NAME}, "sk06-read")
            entry["initial_body_absent"] = True
        elif number == 2:
            loaded = json.loads(last_result(body, "sk06-read"))
            assert loaded["text"].encode("utf-8") == BODY
            assert loaded["skill"]["version_id"] == source["version_id"]
            assert loaded["skill"]["sha256"] == source["sha256"] == hashlib.sha256(BODY).hexdigest()
            assert loaded["skill"]["metadata_sha256"] == source["metadata_sha256"]
            entry["exact_approved_body_in_next_request"] = True
            delta = tool_call("execute_operation", {}, "sk06-unapproved-operation")
        elif number == 3:
            result = last_result(body, "sk06-unapproved-operation")
            assert "Authority refused" in result and len(operations) == 1 and external == []
            assert protected_rows(turn) == original_operation_rows
            entry["operation_refusal_in_next_request"] = True
            delta = tool_call("read_context", {}, "sk06-legal-read")
        elif number == 4:
            context = json.loads(last_result(body, "sk06-legal-read"))
            assert context["source"] == bridges[0].client.assignment.task.source.model_dump(mode="json")
            assert json.loads(context["content"])["item_count"] == 3
            entry["legal_read_after_denial"] = True
            delta = {"content": "Synthetic guidance was read; the unapproved operation was refused and reviewed counts remain available."}
        else:
            raise AssertionError("Unexpected extra provider request in the bounded scenario")
        requests.append(entry)
        return sse(request, delta, number)

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_SK06_MODEL")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client, h.executor.model_transport = h.client, None
    native_root = Path(tempfile.mkdtemp(prefix="s6-native-"))
    h.executor.root = native_root
    accepted, _ = submit(h, "sk06-native-guidance", original_user)
    turn = SimpleNamespace(h=h, binding=h.runtime.bindings[accepted["binding_id"]])
    completed = None
    try:
        completed = h.runtime.controller.step(turn.binding.binding_id)
    finally:
        observed_stop = None if not bridges else bridges[0]._sdk_stop
        exited = len(processes) == 1 and processes[0].poll() is not None
        record("fixed-sdk-guidance-observation", {
            "profile": PROFILES[1], "layer": "fixed-sdk-offline-provider",
            "source_body_sha256": source["sha256"], "native_temp": str(native_root),
            "observation_complete": exited and observed_stop is not None,
            "observed_provider_requests": requests, "observed_operation_attempts": operations,
            "observed_effect_boundary_calls": len(external), "public_tool_events": safe_events,
            "permission_callback_count_observed": len(permissions), "force_stop_calls_observed": len(forces),
            "owned_processes": [{"pid": process.pid, "exit_code": process.poll()} for process in processes],
            "sdk_stop": None if observed_stop is None else observed_stop.model_dump(mode="json"),
            "controller_state": None if completed is None else completed.state,
            "result_forwarded": None if completed is None else completed.result_forwarded,
        })
    assert completed.state == "succeeded" and completed.result_forwarded
    assert len(bridges) == len(clients) == len(processes) == len(configurations) == 1
    assert len(requests) == 4 and len(operations) == 1 and external == [] and not forces
    assert operations[0] == {"denied_status": expected_denial_status, "protected_rows_unchanged": True,
                             "accounting_unchanged_during_tool": True}
    assert protected_rows(turn) == original_operation_rows
    assert observed_user == [True]
    stop = bridges[0]._sdk_stop
    assert stop is not None and stop.graceful and stop.runtime_version == "1.0.83"
    assert stop.pid == processes[0].pid and processes[0].poll() is not None
    assert stop.exit_code == processes[0].returncode
    assert not any(event["type"] in {"skill.invoked", "newskill.invoked", "session.error",
                                   "session.compaction_start", "session.compaction_complete"} for event in safe_events)
    projection = h.client.get("/work-policy/v1/conversations/" + accepted["conversation_id"], headers=h.headers())
    assert projection.status_code == 200 and projection.json()["turns"][0]["reply_available"]
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
    record("fixed-sdk-loaded-guidance", {
        "profile": PROFILES[1], "runtime_mode": h.runtime.mode, "layer": "fixed-sdk-offline-provider",
        "sdk_version": "1.0.13", "runtime_version": stop.runtime_version,
        "source_body_sha256": source["sha256"], "source_version": source["version_id"],
        "source_metadata_sha256": source["metadata_sha256"],
        "provider_requests": requests, "operation_attempts": operations,
        "operation_effect_boundary_calls": len(external), "original_operation_unchanged": True,
        "original_user_event_exact": True, "configurations": configurations,
        "permission_callback_count": len(permissions), "public_tool_events": safe_events,
        "sdk_stop": stop.model_dump(mode="json"), "force_stop_calls": len(forces),
        "saved_result_state": completed.state, "result_forwarded": completed.result_forwarded,
        "native_temp": str(native_root), "cloud_or_paid_calls": 0,
        "limitations": ["Scripted provider, not natural LLM prompt-injection quality",
                        "Only purpose profile used a real SDK; both loaders have separate API/callback controls",
                        "Existing metrics GET adapter is a synthetic composition, not deployed or WorkStateEffectAdapter acceptance"],
    })
