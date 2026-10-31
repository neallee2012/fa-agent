"""Fixed SDK capacity configuration and runtime observations; offline provider only."""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from copilot import CopilotClient
from copilot._jsonrpc import JsonRpcClient
from copilot.session import CopilotSession
from fastapi.testclient import TestClient

from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_sdk_compaction import events_from
from shared_context.behavior_work_policy import work_policy_key
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from sdk_checkpoint_local import sse
from test_behavior_assistant import assistant
from test_instruction_capacity import configured_capacity, declared_facts, submit_capacity
from test_instruction_flow import instruction_models
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key


def test_fixed_sdk_transmits_model_specific_capacity_and_reports_runtime_observations(assistant, monkeypatch):
    facts = declared_facts(monkeypatch)
    facts["gpt-4.1-mini"]["max_prompt_tokens"] = 60000
    facts["configured-second-model"]["max_prompt_tokens"] = 14000
    facts["unknown-context-model"] = {"max_output_tokens": 768,
        "input_microusd_per_million": 400000, "output_microusd_per_million": 1600000}
    Path(os.environ["FA_INSTRUCTION_MODEL_FACTS_FILE"]).write_text(json.dumps(facts), encoding="utf-8")
    catalog_path = Path(os.environ["FA_INSTRUCTION_MODEL_CATALOG_FILE"])
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog.append({"deployment": "unknown-context-model", "provisioning": "Succeeded",
                    "capabilities": {"chatCompletion": "true"}})
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    h, control, _ = configured_capacity(assistant)
    phase, calls, configurations, observed, bridges, forced = 0, [], [], [], [], []
    wire = []
    observed_disconnects = set()
    models = ("gpt-4.1-mini", "configured-second-model", "unknown-context-model")
    initialize = hosted.AssistantSession.__init__
    create, resume = CopilotClient.create_session, CopilotClient.resume_session
    disconnect, force_stop = CopilotSession.disconnect, CopilotClient.force_stop
    rpc_request = JsonRpcClient.request

    async def observe_wire(instance, method, params=None, **kwargs):
        if method in {"session.create", "session.resume"}:
            wire.append({"method": method, "model": params.get("model"),
                "modelCapabilities": params.get("modelCapabilities"),
                "provider_limits": {key: params.get("provider", {}).get(key) for key in (
                    "maxPromptTokens", "maxOutputTokens", "maxContextWindowTokens")
                    if key in params.get("provider", {})}})
        return await rpc_request(instance, method, params, **kwargs)

    def observe_bridge(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    async def runtime_context(session, point):
        model = await session.rpc.model.get_current()
        attribution = (await session.rpc.metadata.get_context_attribution()).context_attribution
        observed.append({"phase": phase, "point": point, "selected_model": model.model_id,
            "context": None if attribution is None else {
                "model_id": attribution.model_id, "model_source": attribution.model_source,
                "prompt_token_limit": attribution.prompt_token_limit,
                "display_context_limit": attribution.limit, "total_tokens": attribution.total_tokens}})

    async def observe_create(instance, **options):
        configurations.append(options)
        session = await create(instance, **options)
        await runtime_context(session, "created-before-send")
        return session

    async def observe_resume(instance, session_id, **options):
        configurations.append(options)
        session = await resume(instance, session_id, **options)
        await runtime_context(session, "resumed-before-send")
        return session

    async def observe_disconnect(instance):
        if (phase, instance.session_id) not in observed_disconnects:
            observed_disconnects.add((phase, instance.session_id))
            await runtime_context(instance, "after-turn-before-disconnect")
        return await disconnect(instance)

    async def observe_force_stop(instance):
        forced.append(True)
        return await force_stop(instance)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", observe_bridge)
    monkeypatch.setattr(CopilotClient, "create_session", observe_create)
    monkeypatch.setattr(CopilotClient, "resume_session", observe_resume)
    monkeypatch.setattr(CopilotSession, "disconnect", observe_disconnect)
    monkeypatch.setattr(CopilotClient, "force_stop", observe_force_stop)
    monkeypatch.setattr(JsonRpcClient, "request", observe_wire)

    def provider(request):
        body = json.loads(request.content)
        output = body.get("max_tokens", body.get("max_completion_tokens"))
        calls.append({"phase": phase, "model": body.get("model"), "output_token_limit": output,
                      "snapshot_not_in_model_prompt": "fa-model-capacity-snapshot-v1" not in json.dumps(body["messages"])})
        assert body["model"] == models[phase]
        # The pinned official E2E documents that completions do not echo this configured cap.
        assert output is None, calls
        assert calls[-1]["snapshot_not_in_model_prompt"]
        assert sum(call["phase"] == phase for call in calls) == 1
        return sse(request, {"content": f"Offline reply for declared model {models[phase]}."}, len(calls))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_CAPACITY_SECRET")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client = h.client
    h.executor.model_transport = None
    conversation, retained, snapshots, sdk_ids = None, {}, [], []
    with TemporaryDirectory(prefix="capacity-sdk-") as directory:
        for phase, name in enumerate(models):
            h.executor.root = Path(directory) / str(phase)
            accepted, command = submit_capacity(h, f"capacity-native-{phase}", name, conversation)
            conversation = accepted["conversation_id"]
            binding = h.runtime.bindings[accepted["binding_id"]]
            registration = control.records[binding.binding_id]
            snapshot = registration.profile.instruction.model_capacity
            snapshots.append(snapshot.model_dump(mode="json"))
            assert snapshot.model == name
            try:
                done = h.runtime.controller.step(binding.binding_id)
            except Problem as error:
                raise AssertionError({"error_code": error.body["error"]["code"],
                                      "calls": calls, "observed": observed}) from None
            assert done.state == "succeeded" and done.result_forwarded, {"calls": calls, "observed": observed}
            assert h.store.read_snapshot(tuple(retained)) == retained
            retained.update(h.store.read_snapshot((
                work_policy_key("registration", registration.admission_id), key("text-archive", binding.job_id))))
            sdk_ids.append(bridges[-1].client.assignment.sdk_session_id)
            events = events_from(bridges[-1]._session_files.snapshot().files)
            assert [event["data"]["content"] for event in events if event["type"] == "user.message"] == [command.content] * (phase + 1)
            assert not any(event["type"] in {"session.compaction_start", "skill.invoked", "newskill.invoked"} for event in events)
            options = configurations[-1]
            limits = options["model_capabilities"].limits
            assert limits.max_context_window_tokens == facts[name].get("context_window_tokens")
            assert limits.max_prompt_tokens == facts[name].get("max_prompt_tokens")
            assert limits.max_output_tokens == facts[name]["max_output_tokens"]
            expected_wire = {"maxOutputTokens": facts[name]["max_output_tokens"]}
            if "max_prompt_tokens" in facts[name]:
                expected_wire["maxPromptTokens"] = facts[name]["max_prompt_tokens"]
            if "context_window_tokens" in facts[name]:
                expected_wire["maxContextWindowTokens"] = facts[name]["context_window_tokens"]
            assert wire[-1]["modelCapabilities"] == {"limits": expected_wire}
            assert wire[-1]["model"] == name
            assert options["model"] == options["provider"]["model_id"] == options["provider"]["wire_model"] == name
            if phase:
                assert options["continue_pending_work"] is False
            assert options["enable_config_discovery"] is False and options["mcp_servers"] == {}
            assert all(options[field] == [] for field in (
                "skill_directories", "plugin_directories", "instruction_directories", "custom_agents"))
        projection = h.client.get("/work-policy/v1/conversations/" + conversation, headers=h.headers())
        assert projection.status_code == 200 and projection.json()["can_follow_up"]
        assert [turn["model_capacity"] for turn in projection.json()["turns"]] == snapshots

    assert len(calls) == len(bridges) == 3 and len(set(sdk_ids)) == 1 and forced == []
    for item in observed:
        assert item["selected_model"] == models[item["phase"]]
        context = item["context"]
        if context is not None:
            configured = facts[models[item["phase"]]].get("max_prompt_tokens")
            item["configured_prompt_matches_observation"] = (None if configured is None
                                                            else context["prompt_token_limit"] == configured)
    assert all(bridge._sdk_stop.graceful for bridge in bridges)
    if os.environ.get("FA_CAPACITY_EVIDENCE_DIR"):
        target = Path(os.environ["FA_CAPACITY_EVIDENCE_DIR"])
        target.mkdir(parents=True, exist_ok=True)
        (target / "capacity-native.json").write_text(json.dumps({
            "evidence": "fixed SDK create/cold resume with synthetic server facts and offline model transport",
            "sdk_version": importlib.metadata.version("github-copilot-sdk"), "runtime_version": "1.0.83",
            "configured_snapshots": snapshots, "sdk_capacity_wire": wire,
            "runtime_observations": observed, "provider_requests": calls,
            "stops": [bridge._sdk_stop.model_dump(mode="json") for bridge in bridges], "force_stop_calls": 0,
            "original_archives_and_tasks_retained": True, "sdk_user_events_exact": True,
            "same_conversation_and_sdk": True, "cloud_requests": 0, "paid_model_requests": 0,
            "provider_hard_limit": "NOT_PROVEN", "long_history_preflight": "NOT_RUN",
            "effective_override_enforcement": "NOT_PROVEN; public context-attribution values differ from configured limits.",
            "input_formula": "No derived input=context-output; explicit server values only.",
        }, indent=2) + "\n", encoding="utf-8")


def test_opted_in_capacity_profile_preserves_structured_sdk_save_and_finish(assistant, monkeypatch):
    from conftest import A
    from shared_context.behavior_assistant_wire import AssistantSubmit, Task

    declared_facts(monkeypatch)
    h, control, _ = configured_capacity(assistant)
    configurations, bridges, force_stops, stops = [], [], [], []
    create = CopilotClient.create_session
    initialize = hosted.AssistantSession.__init__
    force_stop = CopilotClient.force_stop
    complete = h.executor._complete

    async def observe_create(instance, **options):
        configurations.append(options)
        return await create(instance, **options)

    def observe_bridge(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    async def observe_force_stop(instance):
        force_stops.append(True)
        return await force_stop(instance)

    def observe_complete(start, outcome):
        stops.append(outcome["sdk_stop"])
        return complete(start, outcome)

    monkeypatch.setattr(CopilotClient, "create_session", observe_create)
    monkeypatch.setattr(hosted.AssistantSession, "__init__", observe_bridge)
    monkeypatch.setattr(CopilotClient, "force_stop", observe_force_stop)
    monkeypatch.setattr(h.executor, "_complete", observe_complete)
    h.endpoint.expected_url = "https://provider.invalid/openai/v1/chat/completions"
    gateway = AssistantProvider(h.endpoint.expected_url,
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_STRUCTURED_CAPACITY_SECRET")),
        transport=h.endpoint.transport)
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client = h.client
    h.executor.model_transport = None
    command = AssistantSubmit(request_id="structured-with-instruction-capacity-enabled")
    response = h.client.post("/work-policy/v1/submit", content=command.model_dump_json(), headers=h.headers())
    assert response.status_code == 200, response.text
    binding = h.runtime.bindings[response.json()["binding_id"]]
    record = control.records[binding.binding_id]
    assert record.profile.model_capacity_profile == "fa-model-capacity-v1"
    original_task = h.runtime.job_input(binding)
    assert Task.model_validate_json(original_task).instruction is None
    assert "model_capacity" not in json.loads(original_task)
    registration_key = work_policy_key("registration", record.admission_id)
    registration = h.store.read_snapshot((registration_key,))[registration_key]
    with TemporaryDirectory(prefix="capacity-structured-") as directory:
        h.executor.root = Path(directory)
        done = h.runtime.controller.step(binding.binding_id)
    assert done.state == "succeeded" and done.result_forwarded
    assert h.runtime.job_input(binding) == original_task
    assert h.store.read_snapshot((registration_key,))[registration_key] == registration
    assert len(configurations) == len(bridges) == 1
    options = configurations[0]
    assert options["model"] == options["provider"]["model_id"] == options["provider"]["wire_model"] == "gpt-4.1-mini"
    limits = options["model_capabilities"].limits
    assert (limits.max_prompt_tokens, limits.max_output_tokens, limits.max_context_window_tokens) == (32768, 1024, 33792)
    assert options["provider"]["max_prompt_tokens"] == 32768
    assert options["provider"]["max_output_tokens"] == 1024
    assert len(options["system_message"]["content"].encode("utf-8")) == 264
    prefix_sha = hashlib.sha256(options["system_message"]["content"].encode("utf-8")).hexdigest()
    assert prefix_sha == "68daaf7242fa325f8bfb107c4cfc7bb44e065b9eed93d91780b3f402fb3b6f71"
    assert {tool.name for tool in options["tools"]} == {
        "read_context", "save_result", "finish_task", "execute_operation"}
    assert options["enable_config_discovery"] is False and options["mcp_servers"] == {}
    assert len(h.endpoint.requests) == 3
    assert all(request["model"] == "gpt-4.1-mini" and request["max_tokens"] == 1024
               for request in h.endpoint.requests)
    assert [result["tool_call_id"] for request in h.endpoint.requests for result in request["messages"]
            if result["role"] == "tool"] == ["fa_call_1", "fa_call_1", "fa_call_2"]
    version = h.artifacts.version(A, A.team, record.profile.artifact_id, record.profile.artifact_version)
    _, raw = h.artifacts.download(A, A.team, record.profile.artifact_id, version.version_id, version.blob.etag)
    saved = json.loads(raw)
    assert (saved["item_count"], saved["completed_count"], saved["remaining_count"]) == (3, 2, 1)
    result = h.assistant.result(h.workers.inspect(binding.job_id).binding)
    assert result.state == "succeeded" and set(result.passed_checks) == {
        "answer_verified", "artifact_verified", "finish_verified"}
    pull = h.client.post(f"/behavior/v1/bindings/{binding.binding_id}/delivery/pull",
                         json={"request_id": "read-original-structured-result"}, headers=h.headers())
    assert pull.status_code == 200 and pull.json()["work_state"] == "succeeded"
    assert not done.obligation_fulfilled
    assert len(stops) == 1 and stops[0]["graceful"] and stops[0]["runtime_version"] == "1.0.83"
    assert force_stops == []
    if os.environ.get("FA_CAPACITY_EVIDENCE_DIR"):
        target = Path(os.environ["FA_CAPACITY_EVIDENCE_DIR"])
        target.mkdir(parents=True, exist_ok=True)
        (target / "structured-capacity-regression.json").write_text(json.dumps({
            "evidence": "real signed API/controller/M3 with fixed SDK and scripted offline provider",
            "sdk_version": importlib.metadata.version("github-copilot-sdk"), "runtime_version": "1.0.83",
            "server_capacity_profile": record.profile.model_capacity_profile,
            "structured_task_has_instruction_or_capacity_snapshot": False,
            "sdk_create_calls": len(configurations), "offline_model_requests": len(h.endpoint.requests),
            "configured_fixed_limits": {"input": 32768, "output": 1024, "context": 33792},
            "model": options["model"], "prefix_sha256": prefix_sha,
            "original_task_and_registration_unchanged": True, "saved_metrics": [3, 2, 1],
            "save_finish_checks_verified": True, "result_forwarded": done.result_forwarded,
            "delivery_pull_succeeded": True, "human_ack_not_claimed": True,
            "sdk_stop": stops[0], "force_stop_calls": 0, "cloud_requests": 0, "paid_model_requests": 0,
            "limitations": ["Structured fixed-limit regression only; no instruction enforcement/preflight claim."],
        }, indent=2) + "\n", encoding="utf-8")
