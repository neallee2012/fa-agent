"""Fixed SDK custom tools through real local HTTP authority; offline model transport only."""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import pytest
from copilot import CopilotClient
from fastapi.testclient import TestClient

from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_operations import control_key
from shared_context.behavior_sdk_compaction import events_from
from shared_context.behavior_skill_prompt import SKILL_CAPABILITIES, assemble_prompt
from shared_context.behavior_work_policy_api import install_work_policy_routes
from sdk_checkpoint_local import sse
from test_approved_skill_api import BODY, add_skill, approve_skill_version, change_artifact, configured
from test_behavior_assistant import assistant
from test_instruction_flow import check_history, instruction_models
from test_instruction_prompt import saved_turn, submit
from test_instruction_prompt_native import sdk_root
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key


def test_fixed_sdk_custom_reader_discloses_only_selected_body_and_rechecks_cold_resume(
    assistant, sdk_root, monkeypatch,
):
    add_skill(assistant)
    add_skill(assistant, "unselected-guide", text="UNSELECTED_SKILL_CANARY")
    h, control, human = configured(assistant)
    h.executor.root = sdk_root / "first"
    expected = assemble_prompt("fa-instruction-skills-v1")
    phase, calls, bridges, stops, configurations = "first", [], [], [], []
    initialize = hosted.AssistantSession.__init__

    def observe_bridge(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", observe_bridge)
    create, resume = CopilotClient.create_session, CopilotClient.resume_session

    async def observe_create(instance, **options):
        configurations.append(options)
        return await create(instance, **options)

    async def observe_resume(instance, session_id, **options):
        configurations.append(options)
        return await resume(instance, session_id, **options)

    monkeypatch.setattr(CopilotClient, "create_session", observe_create)
    monkeypatch.setattr(CopilotClient, "resume_session", observe_resume)
    force_stop = CopilotClient.force_stop

    async def observe_force_stop(instance):
        stops.append("forced")
        return await force_stop(instance)

    monkeypatch.setattr(CopilotClient, "force_stop", observe_force_stop)

    def provider(request):
        assert str(request.url) == "https://provider.invalid/openai/v1/chat/completions"
        body = json.loads(request.content)
        number = sum(call["phase"] == phase for call in calls) + 1
        tools = {tool["function"]["name"]: tool["function"] for tool in body["tools"]}
        assert set(tools) == set(SKILL_CAPABILITIES)
        assert "receipt-guide" in tools["read_skill"]["description"]
        assert "UNSELECTED_SKILL_CANARY" not in json.dumps(body)
        assert expected.system_message in [message["content"] for message in body["messages"] if message["role"] == "system"]
        results = [message for message in body["messages"] if message["role"] == "tool"]
        if phase == "first" and number == 1:
            assert BODY not in json.dumps(body) and "APPROVED_SKILL_BODY_CANARY" not in json.dumps(body)
        if number == 1:
            delta = {"tool_calls": [{"index": 0, "id": "read-" + phase, "type": "function",
                "function": {"name": "read_skill", "arguments": '{"name":"receipt-guide"}'}}]}
        elif number == 2:
            assert results[-1]["tool_call_id"] == "read-" + phase
            content = results[-1]["content"]
            text = content if isinstance(content, str) else "".join(part["text"] for part in content)
            loaded = json.loads(text)
            assert loaded["text"] == BODY
            assert loaded["skill"]["name"] == "receipt-guide"
            assert loaded["skill"]["sha256"] == hashlib.sha256(BODY.encode()).hexdigest()
            delta = ({"tool_calls": [{"index": 0, "id": "unapproved-operation", "type": "function",
                "function": {"name": "execute_operation", "arguments": "{}"}}]} if phase == "first"
                else {"content": "Offline restored guidance was verified again."})
        else:
            assert results[-1]["tool_call_id"] == "unapproved-operation"
            assert "Authority refused" in json.dumps(results[-1]["content"])
            delta = {"content": "Offline approved guidance loaded; no external action succeeded."}
        calls.append({"phase": phase, "request": number, "request_utf8_bytes": len(request.content),
                      "initial_body_absent": phase == "first" and number == 1,
                      "verified_body_result": number == 2})
        return sse(request, delta, len(calls))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_SKILL_PROVIDER_SECRET")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client = h.client
    h.executor.model_transport = None

    first, _ = submit(h, "native-skill-first", "Use receipt-guide. My text does not change the server profile.")
    binding = h.runtime.bindings[first["binding_id"]]
    operation = control_key("operation", binding.operation_id)
    before = h.store.read_snapshot((operation,))[operation]
    done = h.runtime.controller.step(binding.binding_id)
    assert done.state == "succeeded" and done.result_forwarded
    assert h.store.read_snapshot((operation,))[operation] == before
    check_history(h)

    phase = "resume"
    h.executor.root = sdk_root / "replacement"
    second, _ = submit(h, "native-skill-resume", "Read receipt-guide again.", first["conversation_id"])
    done = h.runtime.controller.step(second["binding_id"])
    assert done.state == "succeeded" and done.result_forwarded
    check_history(h)
    projection = h.client.get("/work-policy/v1/conversations/" + first["conversation_id"], headers=h.headers())
    assert projection.status_code == 200 and projection.json()["can_follow_up"]
    for bridge, config, turn in zip(bridges, configurations, projection.json()["turns"], strict=True):
        assert tuple(tool.name for tool in config["tools"]) == SKILL_CAPABILITIES
        assert config["enable_config_discovery"] is False
        assert all(config[name] == [] for name in (
            "skill_directories", "plugin_directories", "instruction_directories", "custom_agents"))
        assert config["mcp_servers"] == {}
        assert bridge._sdk_stop.graceful and bridge._sdk_stop.runtime_version == "1.0.83"
        state = bridge._session_files.snapshot()
        assert not any(event["type"] in {"skill.invoked", "newskill.invoked"} for event in events_from(state.files))
        assert all(secret not in state.encoded().decode("utf-8") for secret in bridge.client._checkpoint_secrets)
        assert json.dumps(expected.system_message)[1:-1] not in state.encoded().decode("utf-8")
        saved = saved_turn(h, turn["job_id"])
        assert saved.prompt_assembly == expected.record
        assert len(saved.skill_catalog.entries) == 1
        public = json.dumps(turn["prompt_assembly"])
        assert "CANARY" not in public and "receipt-guide" not in public and "SECRET" not in public
        assert turn["prompt_assembly"]["prefix_tokens"] is None
    assert len(calls) == 5 and not stops
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
    evidence = {
        "evidence": "controlled approved-skill source; fixed SDK custom tool and real local API; offline transport",
        "sdk_version": "1.0.13", "runtime_version": "1.0.83",
        "profile": "fa-instruction-skills-v1", "calls": calls,
        "prefix_sha256": expected.record.prefix_sha256, "prefix_utf8_bytes": expected.record.prefix_utf8_bytes,
        "prefix_tokens": None, "catalog_tokens": None,
        "body_sha256": hashlib.sha256(BODY.encode()).hexdigest(),
        "graceful_stops": [bridge._sdk_stop.model_dump(mode="json") for bridge in bridges],
        "force_stop_calls": 0, "unapproved_operation_unchanged": True,
        "native_skill_loader_enabled": False, "cloud_requests": 0, "paid_model_requests": 0,
        "limitations": ["synthetic response, not skill selection or scope quality",
                        "not native-loader acceptance or complete SK-09/76-target acceptance",
                        "no Azure, Hosted deployment, Blob, paid model or token-budget measurement"],
    }
    if os.environ.get("FA_SKILL_EVIDENCE_DIR"):
        from pathlib import Path
        directory = Path(os.environ["FA_SKILL_EVIDENCE_DIR"])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "approved-skill-native.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


def test_fixed_sdk_new_turns_replace_only_current_catalog_and_preserve_historical_bodies(
    assistant, monkeypatch,
):
    from shared_context.behavior_assistant import key

    v2 = "CURRENT_V2_BODY_CANARY: inspect the newly approved receipt."
    future = "NEWLY_AVAILABLE_BODY_CANARY: report the approved source."
    add_skill(assistant)
    add_skill(assistant, "unused-guide", text="UNUSED_BODY_CANARY")
    h, control, human = configured(assistant, ("receipt-guide", "future-guide", "unused-guide"))
    prompt = assemble_prompt("fa-instruction-skills-v1")
    phase, requests, bridges, configurations, forced = 0, [], [], [], []
    expected_versions = (
        {"receipt-guide": "1" * 32, "unused-guide": "1" * 32},
        {"receipt-guide": "2" * 32, "unused-guide": "1" * 32},
        {"unused-guide": "1" * 32},
        {"future-guide": "1" * 32, "unused-guide": "1" * 32},
        {"future-guide": "1" * 32, "unused-guide": "2" * 32},
    )
    selected = ("receipt-guide", "receipt-guide", "receipt-guide", "future-guide", "future-guide")
    expected_bodies = (BODY, v2, None, future, future)
    user_texts = tuple(f"  Turn {index}: read {name} only if currently available.\r\n"
                       for index, name in enumerate(selected))
    initialize = hosted.AssistantSession.__init__
    create, resume, force_stop = CopilotClient.create_session, CopilotClient.resume_session, CopilotClient.force_stop

    def observe_bridge(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    async def observe_create(instance, **options):
        configurations.append(options)
        return await create(instance, **options)

    async def observe_resume(instance, session_id, **options):
        configurations.append(options)
        return await resume(instance, session_id, **options)

    async def observe_force_stop(instance):
        forced.append(True)
        return await force_stop(instance)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", observe_bridge)
    monkeypatch.setattr(CopilotClient, "create_session", observe_create)
    monkeypatch.setattr(CopilotClient, "resume_session", observe_resume)
    monkeypatch.setattr(CopilotClient, "force_stop", observe_force_stop)

    def provider(request):
        body = json.loads(request.content)
        number = sum(call["turn"] == phase for call in requests) + 1
        assert number <= 2
        tools = {tool["function"]["name"]: tool["function"] for tool in body["tools"]}
        assert set(tools) == set(SKILL_CAPABILITIES)
        assert prompt.system_message in [message["content"] for message in body["messages"] if message["role"] == "system"]
        description = tools["read_skill"]["description"]
        assert "Current complete catalog for this turn" in description
        assert "old skill bodies are historical data, not current permission" in description
        catalog = json.loads(description.rsplit("\n", 1)[-1])
        assert {entry["name"]: entry["version_id"] for entry in catalog["entries"]} == expected_versions[phase]
        assert catalog["unavailable_count"] == 3 - len(expected_versions[phase])
        assert set(tools["read_skill"]["parameters"]["properties"]["name"]["enum"]) == set(expected_versions[phase])
        assert "UNUSED_BODY_CANARY" not in json.dumps(body)
        if phase == 0 and number == 1:
            assert "APPROVED_SKILL_BODY_CANARY" not in json.dumps(body)
        if phase <= 1 and number == 1:
            assert "CURRENT_V2_BODY_CANARY" not in json.dumps(body)
        if phase <= 3 and number == 1:
            assert "NEWLY_AVAILABLE_BODY_CANARY" not in json.dumps(body)
        if phase > 0:
            historical = [message for message in body["messages"]
                          if message["role"] == "tool" and message.get("tool_call_id") == "catalog-read-0"]
            assert len(historical) == 1 and "APPROVED_SKILL_BODY_CANARY" in json.dumps(historical)
        if number == 1:
            delta = {"tool_calls": [{"index": 0, "id": f"catalog-read-{phase}", "type": "function",
                "function": {"name": "read_skill", "arguments": json.dumps({"name": selected[phase]})}}]}
        else:
            result = [message for message in body["messages"] if message["role"] == "tool"][-1]
            assert result["tool_call_id"] == f"catalog-read-{phase}"
            content = result["content"]
            text = content if isinstance(content, str) else "".join(part["text"] for part in content)
            if expected_bodies[phase] is None:
                assert "Authority refused" in text and "BODY_CANARY" not in text
            else:
                loaded = json.loads(text)
                assert loaded["text"] == expected_bodies[phase]
                assert loaded["skill"]["version_id"] == expected_versions[phase][selected[phase]]
            delta = {"content": f"Offline turn {phase} settled; no external operation performed."}
        requests.append({"turn": phase, "request": number, "catalog_versions": expected_versions[phase],
                         "tool_description_sha256": hashlib.sha256(description.encode("utf-8")).hexdigest()})
        return sse(request, delta, len(requests))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_CATALOG_SECRET")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client = h.client
    h.executor.model_transport = None
    archives, original_events, conversation, sdk_id = {}, [], None, None

    with TemporaryDirectory(prefix="sk09-") as directory:
        for phase in range(5):
            if phase == 1:
                approve_skill_version(h, "receipt-guide", v2)
            elif phase == 2:
                change_artifact(h, lambda value: value.update(effective_version_id=None))
            elif phase == 3:
                add_skill(h, "future-guide", text=future)
            elif phase == 4:
                approve_skill_version(h, "unused-guide", "UNUSED_BODY_CANARY_V2")
            h.executor.root = Path(directory) / str(phase)
            accepted, _ = submit(h, f"native-catalog-{phase}", user_texts[phase], conversation)
            conversation = accepted["conversation_id"]
            binding = h.runtime.bindings[accepted["binding_id"]]
            operation_key = control_key("operation", binding.operation_id)
            operation = h.store.read_snapshot((operation_key,))[operation_key]
            done = h.runtime.controller.step(binding.binding_id)
            assert done.state == "succeeded" and done.result_forwarded
            assert h.store.read_snapshot((operation_key,))[operation_key] == operation
            check_history(h)
            bridge, options = bridges[-1], configurations[-1]
            state = bridge._session_files.snapshot()
            assert sdk_id is None or state.sdk_session_id == sdk_id
            sdk_id = state.sdk_session_id
            events = events_from(state.files)
            assert [event["data"]["content"] for event in events if event["type"] == "user.message"] == list(user_texts[:phase + 1])
            retained = [event for event in events if event["type"] in {"user.message", "tool.execution_complete"}]
            assert retained[:len(original_events)] == original_events
            original_events = retained
            assert not any(event["type"] in {"skill.invoked", "newskill.invoked"} for event in events)
            assert options["enable_config_discovery"] is False and options["mcp_servers"] == {}
            assert all(options[name] == [] for name in (
                "skill_directories", "plugin_directories", "instruction_directories", "custom_agents"))
            if phase:
                assert options["continue_pending_work"] is False
            archive_key = key("text-archive", binding.job_id)
            archives[archive_key] = h.store.read_snapshot((archive_key,))[archive_key]
            assert h.store.read_snapshot(tuple(archives)) == archives
            saved = saved_turn(h, binding.job_id)
            assert {entry.name: entry.version_id for entry in saved.skill_catalog.entries} == expected_versions[phase]
            assert saved.prompt_assembly == assemble_prompt("fa-instruction-skills-v1").record
            projection = h.client.get("/work-policy/v1/conversations/" + conversation, headers=h.headers())
            assert projection.status_code == 200 and projection.json()["can_follow_up"]
            assert len(projection.json()["turns"]) == phase + 1

    assert len(requests) == 10 and len(bridges) == 5 and not forced
    assert all(bridge._sdk_stop.graceful and bridge._sdk_stop.runtime_version == "1.0.83" for bridge in bridges)
    if os.environ.get("FA_SKILL_EVIDENCE_DIR"):
        evidence = {
            "scenario": "completed catalog transitions; real API/controller/restore/custom tool; offline transport",
            "sdk_version": importlib.metadata.version("github-copilot-sdk"), "runtime_version": "1.0.83",
            "requests": requests, "graceful_stops": [bridge._sdk_stop.model_dump(mode="json") for bridge in bridges],
            "prefix_sha256": prompt.record.prefix_sha256,
            "archive_references": [{
                "state_sha256": row.payload["data"]["archive"]["sha256"],
                "metadata_sha256": hashlib.sha256(json.dumps(
                    row.payload["data"], sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
            } for row in archives.values()],
            "force_stop_calls": 0, "historical_events_unchanged": True, "prior_archive_rows_unchanged": True,
            "exact_user_events": True, "unapproved_operations_unchanged": True,
            "native_skill_loader_enabled": False, "cloud_requests": 0, "paid_model_requests": 0,
            "limits": ["synthetic responses, not model quality or semantic revocation",
                       "Team-level M3 ACL, not new private artifact ACL",
                       "revocation is synthetic effective-state change, not a new revocation API"],
        }
        target = Path(os.environ["FA_SKILL_EVIDENCE_DIR"])
        target.mkdir(parents=True, exist_ok=True)
        (target / "catalog-transitions-native.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("failure", ["source-exception", "schema-spoof"])
def test_fixed_sdk_custom_tool_failure_never_discloses_body_or_enables_native_loading(
    assistant, sdk_root, monkeypatch, failure,
):
    add_skill(assistant)
    h, control, human = configured(assistant)
    h.executor.root = sdk_root / failure
    bridges, calls, force_stops = [], [], []
    initialize = hosted.AssistantSession.__init__

    def observe(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", observe)
    original_stop = CopilotClient.force_stop

    async def forced(instance):
        force_stops.append(True)
        return await original_stop(instance)

    monkeypatch.setattr(CopilotClient, "force_stop", forced)
    if failure == "source-exception":
        def unavailable(*args):
            raise RuntimeError("PRIVATE_SOURCE_EXCEPTION_CANARY")
        monkeypatch.setattr(h.artifacts.store, "read", unavailable)

    def provider(request):
        body = json.loads(request.content)
        assert "APPROVED_SKILL_BODY_CANARY" not in json.dumps(body)
        assert "PRIVATE_SOURCE_EXCEPTION_CANARY" not in json.dumps(body)
        assert {tool["function"]["name"] for tool in body["tools"]} == set(SKILL_CAPABILITIES)
        if not calls:
            args = {"name": "receipt-guide"}
            if failure == "schema-spoof":
                args["path"] = "../SKILL.md"
            delta = {"tool_calls": [{"index": 0, "id": "refused-read", "type": "function",
                "function": {"name": "read_skill", "arguments": json.dumps(args)}}]}
        else:
            results = [message for message in body["messages"] if message["role"] == "tool"]
            assert results[-1]["tool_call_id"] == "refused-read"
            assert "Authority refused" in json.dumps(results[-1]["content"])
            delta = {"content": "Offline source access was refused; no guidance body was loaded."}
        calls.append({"body_absent": True, "private_error_absent": True})
        return sse(request, delta, len(calls))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_FAILURE_SECRET")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client = h.client
    h.executor.model_transport = None
    accepted, _ = submit(h, "native-denied-" + failure, "Use receipt-guide if it is available.")
    done = h.runtime.controller.step(accepted["binding_id"])
    assert done.state == "succeeded" and done.result_forwarded
    assert len(calls) == 2 and len(bridges) == 1 and not force_stops
    bridge = bridges[0]
    assert bridge._sdk_stop.graceful and bridge._sdk_stop.runtime_version == "1.0.83"
    events = events_from(bridge._session_files.files)
    assert any(event["type"] == "tool.execution_complete" and event["data"].get("success") is False for event in events)
    assert not any(event["type"] in {"skill.invoked", "newskill.invoked"} for event in events)
    assert "APPROVED_SKILL_BODY_CANARY" not in bridge._session_files.snapshot().encoded().decode()
    check_history(h)
    if os.environ.get("FA_SKILL_EVIDENCE_DIR"):
        from pathlib import Path
        directory = Path(os.environ["FA_SKILL_EVIDENCE_DIR"])
        directory.mkdir(parents=True, exist_ok=True)
        evidence = {"scenario": failure, "sdk_version": importlib.metadata.version("github-copilot-sdk"),
            "runtime_version": bridge._sdk_stop.runtime_version, "calls": calls,
            "sdk_tool_failed": True, "body_disclosed": False, "native_skill_loader_enabled": False,
            "graceful_stop": bridge._sdk_stop.model_dump(mode="json"), "force_stop_calls": 0,
            "cloud_requests": 0, "paid_model_requests": 0}
        (directory / (failure + ".json")).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


def test_fixed_sdk_new_user_turn_continues_after_a_midturn_catalog_fence(assistant, monkeypatch):
    from shared_context.behavior_assistant import key
    from shared_context.behavior_assistant_wire import StoredSdkArchive
    from shared_context.errors import Problem

    add_skill(assistant)
    h, control, _ = configured(assistant)
    bodies = (BODY, "NATIVE_V2_BODY_CANARY", "NATIVE_V3_BODY_CANARY")
    user_texts = ("  Read receipt-guide.\r\n", " Use the current receipt-guide.\n",
                  "\tA new user turn after the failure: read current receipt-guide.  ")
    phase, requests, bridges, resumed, refused, forced = 0, [], [], [], [], []
    initialize = hosted.AssistantSession.__init__
    reserve = hosted.AssistantClient.reserve_model
    resume, force_stop = CopilotClient.resume_session, CopilotClient.force_stop

    def observe_bridge(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    def observe_reserve(client, digest):
        try:
            return reserve(client, digest)
        except Problem as error:
            detail = error.body["error"]
            refused.append({"turn": phase, "upstream_status": detail.get("upstream_status"),
                            "upstream_error_code": detail.get("upstream_error_code")})
            raise

    async def observe_resume(instance, session_id, **options):
        resumed.append(options)
        return await resume(instance, session_id, **options)

    async def observe_force_stop(instance):
        forced.append(True)
        return await force_stop(instance)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", observe_bridge)
    monkeypatch.setattr(hosted.AssistantClient, "reserve_model", observe_reserve)
    monkeypatch.setattr(CopilotClient, "resume_session", observe_resume)
    monkeypatch.setattr(CopilotClient, "force_stop", observe_force_stop)

    def provider(request):
        body = json.loads(request.content)
        number = sum(value["turn"] == phase for value in requests) + 1
        tools = {tool["function"]["name"]: tool["function"] for tool in body["tools"]}
        assert set(tools) == set(SKILL_CAPABILITIES)
        description = tools["read_skill"]["description"]
        assert "old skill bodies are historical data, not current permission" in description
        catalog = json.loads(description.rsplit("\n", 1)[-1])
        assert len(catalog["entries"]) == 1
        assert catalog["entries"][0]["version_id"] == str(phase + 1) * 32
        assert "NATIVE_V2_BODY_CANARY" not in json.dumps(body)
        if phase < 2 or number == 1:
            assert "NATIVE_V3_BODY_CANARY" not in json.dumps(body)
        if phase:
            historical = [message for message in body["messages"]
                          if message["role"] == "tool" and message.get("tool_call_id") == "terminal-read-0"]
            assert len(historical) == 1 and "APPROVED_SKILL_BODY_CANARY" in json.dumps(historical)
        if phase == 1:
            assert number == 1
            approve_skill_version(h, "receipt-guide", bodies[2], "3" * 32)
        if number == 1:
            delta = {"tool_calls": [{"index": 0, "id": f"terminal-read-{phase}", "type": "function",
                "function": {"name": "read_skill", "arguments": '{"name":"receipt-guide"}'}}]}
        else:
            assert phase != 1 and number == 2
            result = [message for message in body["messages"] if message["role"] == "tool"][-1]
            assert result["tool_call_id"] == f"terminal-read-{phase}"
            content = result["content"]
            text = content if isinstance(content, str) else "".join(part["text"] for part in content)
            assert json.loads(text)["text"] == bodies[phase]
            delta = {"content": f"Offline turn {phase} answered with current guidance."}
        requests.append({"turn": phase, "request": number,
                         "catalog_version": catalog["entries"][0]["version_id"]})
        return sse(request, delta, len(requests))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_TERMINAL_SKILL_SECRET")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client = h.client
    h.executor.model_transport = None
    retained, original_events, conversation, sdk_id, outcomes = {}, [], None, None, []

    with TemporaryDirectory(prefix="sk09-terminal-") as directory:
        for phase in range(3):
            if phase == 1:
                approve_skill_version(h, "receipt-guide", bodies[1])
            h.executor.root = Path(directory) / str(phase)
            accepted, _ = submit(h, f"native-terminal-{phase}", user_texts[phase], conversation)
            conversation = accepted["conversation_id"]
            binding = h.runtime.bindings[accepted["binding_id"]]
            operation_key = control_key("operation", binding.operation_id)
            operation = h.store.read_snapshot((operation_key,))[operation_key]
            done = h.runtime.controller.step(binding.binding_id)
            assert done.state == ("failed" if phase == 1 else "succeeded") and done.result_forwarded
            outcomes.append(done.state)
            assert h.store.read_snapshot((operation_key,))[operation_key] == operation
            check_history(h)
            saved = saved_turn(h, binding.job_id)
            assert isinstance(saved.archive, StoredSdkArchive)
            assert saved.skill_catalog.entries[0].version_id == str(phase + 1) * 32
            assert saved.failure == ("sdk_failed" if phase == 1 else None) and saved.sdk_stop.graceful
            state = bridges[-1]._session_files.snapshot()
            assert sdk_id is None or state.sdk_session_id == sdk_id
            sdk_id = state.sdk_session_id
            events = events_from(state.files)
            assert [event["data"]["content"] for event in events if event["type"] == "user.message"] == list(user_texts[:phase + 1])
            preserved = [event for event in events if event["type"] in {"user.message", "tool.execution_complete"}]
            assert preserved[:len(original_events)] == original_events
            original_events = preserved
            completed_tools = [event for event in events if event["type"] == "tool.execution_complete"]
            assert len(completed_tools) == phase + 1
            assert sum(event["data"].get("success") is False for event in completed_tools) == (1 if phase else 0)
            assert not any(event["type"] in {"skill.invoked", "newskill.invoked"} for event in events)
            if phase == 1:
                assert any(event["type"] == "tool.execution_complete" and event["data"].get("success") is False
                           for event in events)
                assert refused == [{"turn": 1, "upstream_status": 403, "upstream_error_code": "SKILL_SOURCE_CHANGED"}]
            retained.update(h.store.read_snapshot((key("text-archive", binding.job_id),
                                                   key("result", binding.work.work_id))))
            assert h.store.read_snapshot(tuple(retained)) == retained
            projection = h.client.get("/work-policy/v1/conversations/" + conversation, headers=h.headers())
            assert projection.status_code == 200 and projection.json()["can_follow_up"]

    assert len(requests) == 5 and len(bridges) == 3 and len(resumed) == 2 and not forced
    for options in resumed:
        assert options["continue_pending_work"] is False and options["enable_config_discovery"] is False
        assert options["mcp_servers"] == {}
        assert all(options[name] == [] for name in (
            "skill_directories", "plugin_directories", "instruction_directories", "custom_agents"))
    assert all(bridge._sdk_stop.graceful and bridge._sdk_stop.runtime_version == "1.0.83" for bridge in bridges)
    if os.environ.get("FA_SKILL_EVIDENCE_DIR"):
        evidence = {
            "scenario": "C2 to C3 during a real SDK turn; API reserve fenced; graceful failed archive; new user turn",
            "sdk_version": importlib.metadata.version("github-copilot-sdk"), "runtime_version": "1.0.83",
            "requests": requests, "refused_model_reserves": refused, "outcomes": outcomes,
            "graceful_stops": [bridge._sdk_stop.model_dump(mode="json") for bridge in bridges],
            "force_stop_calls": 0, "prior_archives_and_results_unchanged": True,
            "failed_result_not_rewritten": True, "same_conversation_and_sdk": True, "exact_user_events": True,
            "historical_tool_events_retained": True, "original_failed_tool_not_replayed": True,
            "native_skill_loader_enabled": False, "cloud_requests": 0, "paid_model_requests": 0,
            "limits": ["scripted offline responses, not semantic model quality",
                       "cancellation covered by signed API with a published stopped archive, not this native case"],
        }
        target = Path(os.environ["FA_SKILL_EVIDENCE_DIR"])
        target.mkdir(parents=True, exist_ok=True)
        (target / "terminal-catalog-native.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
