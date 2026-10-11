"""One fixed-SDK catalog scenario with scripted model responses, not model quality."""

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
from shared_context.behavior_sdk_compaction import events_from
from shared_context.behavior_skill_prompt import SKILL_CAPABILITIES
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_instruction_flow import check_history, instruction_models
from test_instruction_prompt import saved_turn, submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_skill_catalog_budget_api import NAMES, configure, tokenizer
from test_skill_purpose import BODY


def test_fixed_sdk_reads_an_omitted_authorized_name_under_the_description_budget(
    assistant, signing_key, jwk_client, tokenizer, monkeypatch,
):
    h, control, _, budget = configure(assistant, signing_key, jwk_client, tokenizer)
    calls, bridges = [], []
    selected = None
    initialize = hosted.AssistantSession.__init__

    def capture(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", capture)

    def provider(request):
        nonlocal selected
        assert str(request.url) == "https://provider.invalid/openai/v1/chat/completions"
        assert len(calls) < 2, "Unexpected extra scripted model request"
        packet = json.loads(request.content)
        tools = {entry["function"]["name"]: entry["function"] for entry in packet["tools"]}
        assert set(tools) == set(SKILL_CAPABILITIES)
        reader = tools["read_skill"]
        assert tokenizer.count(reader["description"].encode()) <= budget
        assert "enum" not in reader["parameters"]["properties"]["name"]
        display = json.loads(reader["description"].split("\n", 1)[1])
        assert display["omitted_count"] > 0
        if not calls:
            assert "PURPOSE_BODY_CANARY" not in json.dumps(packet)
            shown = {entry["name"] for entry in display["entries"]}
            selected = next(name for name in NAMES if name not in shown)
            delta = {"tool_calls": [{
                "index": 0, "id": "read-omitted-skill", "type": "function",
                "function": {"name": "read_skill", "arguments": json.dumps({"name": selected})},
            }]}
        else:
            result = next(message for message in packet["messages"]
                          if message.get("role") == "tool" and message.get("tool_call_id") == "read-omitted-skill")
            content = result["content"]
            loaded = json.loads(content if isinstance(content, str) else "".join(item["text"] for item in content))
            assert loaded["skill"]["name"] == selected
            assert loaded["text"].encode() == BODY
            delta = {"content": "The omitted authorized skill was read without truncating its body."}
        calls.append({"description_tokens": tokenizer.count(reader["description"].encode()),
                      "omitted_count": display["omitted_count"]})
        return sse(request, delta, len(calls))

    gateway = AssistantProvider(
        "https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_CATALOG_FIXTURE")),
        transport=httpx.MockTransport(provider),
    )
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    parent = Path(__file__).resolve().parents[2] / ".azure"
    parent.mkdir(exist_ok=True)
    with TestClient(h.app) as client, TemporaryDirectory(prefix="catalog-budget-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory)
        accepted, _ = submit(h, "catalog-budget-native", "Read one currently approved guide, including omitted entries.")
        assert h.runtime.controller.step(accepted["binding_id"]).state == "succeeded"
        assert len(calls) == 2 and len(bridges) == 1
        bridge = bridges[0]
        assert bridge._sdk_stop.graceful and bridge._sdk_stop.runtime_version == "1.0.83"
        archive = saved_turn(h, h.runtime.bindings[accepted["binding_id"]].job_id)
        assert tuple(entry.name for entry in archive.skill_catalog.entries) == NAMES
        events = events_from(bridge._session_files.snapshot().files)
        assert not any(event["type"] in {"skill.invoked", "newskill.invoked", "session.compaction_start"}
                       for event in events)
        assert any(event["type"] == "tool.execution_complete" and event["data"].get("success") is True
                   for event in events)
        check_history(h)
