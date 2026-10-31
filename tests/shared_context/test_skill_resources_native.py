"""One real fixed-SDK manifest/read sequence; scripted provider, no cloud or quality claim."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from sdk_checkpoint_local import sse
from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_skill_resources_prompt import SKILL_RESOURCES_CAPABILITIES
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_instruction_prompt import saved_turn, submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_skill_resources_api import configured_resources


def test_fixed_sdk_loads_manifest_then_exact_resource_without_new_authority(assistant, monkeypatch):
    h, control, _, reference = configured_resources(assistant)
    calls, bridges = [], []
    initialize = hosted.AssistantSession.__init__
    def capture(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)
    monkeypatch.setattr(hosted.AssistantSession, "__init__", capture)

    def provider(request):
        assert str(request.url) == "https://provider.invalid/openai/v1/chat/completions"
        assert len(calls) < 3
        packet = json.loads(request.content)
        names = {tool["function"]["name"] for tool in packet["tools"]}
        assert names == set(SKILL_RESOURCES_CAPABILITIES)
        results = [message for message in packet["messages"] if message["role"] == "tool"]
        if not calls:
            assert "PURPOSE_BODY_CANARY" not in json.dumps(packet)
            delta = {"tool_calls": [{"index": 0, "id": "resource-manifest", "type": "function",
                "function": {"name": "read_skill", "arguments": '{"name":"resource-guide"}'}}]}
        else:
            content = results[-1]["content"]
            loaded = json.loads(content if isinstance(content, str) else "".join(item["text"] for item in content))
            if len(calls) == 1:
                assert loaded["schema_version"] == "fa-approved-skill-read-v3"
                assert loaded["resources"]["entries"] == [reference.model_dump(mode="json")]
                delta = {"tool_calls": [{"index": 0, "id": "read-resource", "type": "function",
                    "function": {"name": "read_skill_resource",
                                 "arguments": '{"name":"resource-guide","resource":"reference.json"}'}}]}
            else:
                assert loaded["schema_version"] == "fa-skill-resource-read-v1"
                assert loaded["resource"] == reference.model_dump(mode="json")
                assert loaded["text"] == '{"value":42}\n'
                delta = {"content": "The approved reference data contains value 42."}
        calls.append(True)
        return sse(request, delta, len(calls))

    gateway = AssistantProvider(
        "https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_RESOURCE_FIXTURE")),
        transport=httpx.MockTransport(provider),
    )
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    parent = Path(__file__).resolve().parents[2] / ".azure"
    parent.mkdir(exist_ok=True)
    with TestClient(h.app) as client, TemporaryDirectory(prefix="skill-resources-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory)
        accepted, _ = submit(h, "resource-native", "Read the approved resource-guide and its reference data.")
        assert h.runtime.controller.step(accepted["binding_id"]).state == "succeeded"
        assert len(calls) == 3 and len(bridges) == 1
        assert bridges[0]._sdk_stop.graceful and bridges[0]._sdk_stop.runtime_version == "1.0.83"
        archive = saved_turn(h, h.runtime.bindings[accepted["binding_id"]].job_id)
        assert archive.prompt_assembly.declared_capabilities == SKILL_RESOURCES_CAPABILITIES
