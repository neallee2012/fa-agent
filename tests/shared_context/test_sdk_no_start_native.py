"""One fixed-runtime normal-entry control; offline provider, not cloud proof."""

import importlib.metadata
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from copilot import CopilotClient
from fastapi.testclient import TestClient

from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_sdk_compaction import events_from
from shared_context.behavior_skill_prompt import SKILL_CAPABILITIES
from shared_context.behavior_work_policy_api import install_work_policy_routes
from sdk_checkpoint_local import sse
from test_approved_skill_api import add_skill
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_instruction_prompt import saved_turn, submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_no_start import configured_no_start


def test_fixed_sdk_constructs_only_after_durable_entry_intent_and_keeps_real_stop(
        assistant, monkeypatch, *, profile="fa-sdk-start-gate-v1"):
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
    if profile == "fa-sdk-start-gate-v1":
        add_skill(assistant)
        h, _, _ = configured_no_start(assistant)
        capabilities = SKILL_CAPABILITIES
    else:
        from test_memory_no_sdk import configured_memory_gate
        from shared_context.behavior_work_memory_prompt import WORK_MEMORY_CAPABILITIES
        assert profile == "fa-sdk-start-gate-v2"
        h, _, _ = configured_memory_gate(assistant, profile)
        capabilities = WORK_MEMORY_CAPABILITIES
    bridges, entries, requests, forced, protocol = [], [], [], [], []
    initialize = hosted.AssistantSession.__init__
    construct = CopilotClient.__init__
    force_stop = CopilotClient.force_stop

    def observe_bridge(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    def observe_construction(instance, *args, **kwargs):
        gate = bridges[-1]._sdk_start_gate
        with gate.journal.connect() as db:
            raw, = db.execute("SELECT body FROM assistant_sdk_start_gates").fetchone()
        record = json.loads(raw)
        assert record["state"] == "entry_attempted" and record["refusal_code"] is None
        entries.append(record["binding"]["code_sha256"])
        construct(instance, *args, **kwargs)

    async def observe_force_stop(instance):
        forced.append(True)
        return await force_stop(instance)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", observe_bridge)
    monkeypatch.setattr(CopilotClient, "__init__", observe_construction)
    monkeypatch.setattr(CopilotClient, "force_stop", observe_force_stop)
    original = "  Normal gated entry; preserve this exact user input.\r\n"

    def provider(request):
        body = json.loads(request.content)
        requests.append(True)
        users = [message["content"] for message in body["messages"] if message["role"] == "user"]
        parts = [part for user in users for part in
                 ([user] if isinstance(user, str) else [piece["text"] for piece in user if piece.get("type") == "text"])]
        observation = {
            "tools_exact": {tool["function"]["name"] for tool in body["tools"]} == set(capabilities),
            "private_start_binding_absent": "fa-sdk-start-binding-" not in json.dumps(body),
            "model_user_verbatim_occurrences": sum(part.count(original) for part in parts),
            "model_user_trimmed_occurrences": sum(part.count(original.strip()) for part in parts),
        }
        protocol.append(observation)
        assert observation["tools_exact"] and observation["private_start_binding_absent"]
        if profile == "fa-sdk-start-gate-v2":
            assert "TEAM_MEMORY_CANARY" not in json.dumps(body) and "claim_version" in json.dumps(body)
        assert observation["model_user_trimmed_occurrences"] == 1
        assert len(requests) == 1
        return sse(request, {"content": "Offline normal SDK reply after the original entry intent."}, 1)

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_SDK_GATE_SECRET")),
        transport=httpx.MockTransport(provider))
    h.client = TestClient(create_assistant_app(h.assistant, h.verifier, provider=gateway))
    install_work_policy_routes(h.client.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.executor.http_client = h.client
    h.executor.model_transport = None
    with TemporaryDirectory(prefix="no-sdk-control-") as directory:
        h.executor.root = Path(directory)
        accepted, _ = submit(h, "native-normal-gate", original)
        done = h.runtime.controller.step(accepted["binding_id"])
        assert done.state == "succeeded" and done.result_forwarded
        binding = h.runtime.bindings[accepted["binding_id"]]
        archive = saved_turn(h, binding.job_id)
        assert archive.failure is None and archive.sdk_stop == bridges[0]._sdk_stop
        no_sdk_key = key("no-sdk-turn", binding.job_id)
        assert h.store.read_snapshot((no_sdk_key,))[no_sdk_key] is None
        assert bridges[0]._sdk_start_gate.observe_closed() is None
        events = events_from(bridges[0]._session_files.snapshot().files)
        assert [event["data"]["content"] for event in events if event["type"] == "user.message"] == [original]
    assert len(entries) == len(requests) == 1 and forced == []
    assert bridges[0]._sdk_stop.runtime_version == "1.0.83" and bridges[0]._sdk_stop.graceful
    if os.environ.get("FA_NO_SDK_EVIDENCE_DIR"):
        destination = Path(os.environ["FA_NO_SDK_EVIDENCE_DIR"])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "normal-sdk-entry-control.json").write_text(json.dumps({
            "scope": "fixed SDK/runtime with real local controller and API; offline provider",
            "sdk_start_profile": profile,
            "sdk_version": importlib.metadata.version("github-copilot-sdk"),
            "runtime_version": bridges[0]._sdk_stop.runtime_version,
            "constructor_calls": len(entries), "offline_provider_requests": len(requests),
            "model_facing_observations": protocol, "sdk_user_event_verbatim": True,
            "entry_intent_was_durable_before_construction": True,
            "assigned_code_sha256": entries[0], "sdk_stop": bridges[0]._sdk_stop.model_dump(mode="json"),
            "no_sdk_disposition_created": False, "force_stop_calls": 0,
            "paid_model_requests": 0, "cloud_requests": 0,
        }, indent=2) + "\n", encoding="utf-8")
