"""One fixed SDK 1.0.13 / runtime 1.0.83 turn: history-skill-draft plus the captured fa-skills-v1 capability.

Scripted provider; no cloud, network or real model.
"""

import importlib.metadata
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from copilot import CopilotClient
from fastapi.testclient import TestClient

from sdk_checkpoint_local import sse
from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_capability_prompt import assemble_capabilities
from shared_context.behavior_history_skill_draft_prompt import HISTORY_SKILL_DRAFT_CAPABILITIES
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select
from test_instruction_flow import instruction_models
from test_instruction_prompt import saved_turn, submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_skills_capability import GRANTS, skill_template

TOOLS = (*HISTORY_SKILL_DRAFT_CAPABILITIES, "read_skill")


def test_fixed_sdk_reads_an_approved_skill_through_the_skills_capability(assistant, monkeypatch, signing_key, jwk_client):
    from test_skill_purpose_api import add_purpose_skill
    version = add_purpose_skill(assistant, signing_key, jwk_client, name="receipt-guide")
    h, control, _ = configured(assistant)
    h.runtime.capability_grants = GRANTS
    chosen, _ = select(h, request_id="choose-skills-native", value=skill_template(version))
    assert chosen.status_code == 200, chosen.text
    options, requests, reads = [], [], []
    create = CopilotClient.create_session

    async def created(instance, **kwargs):
        options.append(kwargs)
        return await create(instance, **kwargs)
    monkeypatch.setattr(CopilotClient, "create_session", created)

    def provider(request):
        body = json.loads(request.content)
        number = len(requests) + 1
        assert {item["function"]["name"] for item in body["tools"]} == set(TOOLS)
        requests.append([item["content"] for item in body["messages"] if item["role"] == "system"])
        if number == 1:
            delta = {"tool_calls": [{"index": 0, "id": "read-guide", "type": "function", "function": {
                "name": "read_skill", "arguments": json.dumps({"name": "receipt-guide"})}}]}
        else:
            tool = [item for item in body["messages"] if item["role"] == "tool"][-1]
            reads.append(tool["content"] if isinstance(tool["content"], str)
                         else "".join(part["text"] for part in tool["content"]))
            delta = {"content": "Followed the approved receipt guide."}
        assert number <= 2
        return sse(request, delta, number)

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_SKILLS_CAPABILITY_PROVIDER")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: None, require_current_native_evidence=lambda: None))
    parent = Path(__file__).resolve().parents[2] / ".azure"
    parent.mkdir(exist_ok=True)
    with TestClient(h.app) as client, TemporaryDirectory(prefix="skills-capability-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory)
        accepted, _ = submit(h, "native-skills-capability", "Use the approved receipt guide.")
        completed = h.runtime.controller.step(accepted["binding_id"])
        assert completed.state == "succeeded" and completed.result_forwarded
        task = control.records[accepted["binding_id"]].profile.instruction
        combined = assemble_capabilities(task.prompt_profile, task.capabilities)
        assert len(options) == 1 and len(requests) == 2 and len(reads) == 1
        assert sorted(options[0]["available_tools"].to_list()) == sorted("custom:" + name for name in TOOLS)
        assert options[0]["mcp_servers"] == {}
        assert combined.system_message in requests[0]
        read = json.loads(reads[0])
        assert read["skill"]["name"] == "receipt-guide" and read["text"].strip()
        archive = saved_turn(h, h.runtime.bindings[accepted["binding_id"]].job_id)
        assert archive.capability_assembly == combined.record
        assert [entry.name for entry in archive.skill_catalog.entries] == ["receipt-guide"]
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
