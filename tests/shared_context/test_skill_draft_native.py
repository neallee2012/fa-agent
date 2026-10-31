"""One fixed-SDK save_skill_draft run; scripted provider, no cloud or real model."""

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

from conftest import A
from sdk_checkpoint_local import sse
from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_capability_prompt import assemble_capabilities
from shared_context.behavior_sdk_compaction import events_from
from shared_context.behavior_history_skill_draft_prompt import HISTORY_SKILL_DRAFT_CAPABILITIES
from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE, SKILL_DRAFT_PROFILE, SkillDraftReceipt
from shared_context.behavior_skill_draft_prompt import SKILL_DRAFT_CAPABILITIES
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models
from test_instruction_prompt import saved_turn, submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_skill_draft import BODY, PURPOSE, configured_draft
from test_skillify_capability import GRANTS, SKILLIFY_REF, V2_PROFILE, V3_PROFILE


@pytest.mark.parametrize("profile", [SKILL_DRAFT_PROFILE, HISTORY_SKILL_DRAFT_PROFILE, V2_PROFILE, V3_PROFILE],
                         ids=["installed_original", "history_selected", "v2_skillify_selected", "v3_skillify_selected"])
def test_fixed_sdk_saves_user_procedure_as_one_unapproved_skill_draft(assistant, monkeypatch, profile, record_property):
    if profile == SKILL_DRAFT_PROFILE:
        h, control, _ = configured_draft(assistant)
        capabilities = SKILL_DRAFT_CAPABILITIES
    else:
        h, control, _ = configured(assistant)
        if profile in {V2_PROFILE, V3_PROFILE}:
            h.runtime.capability_grants = GRANTS
        selected, _ = select(h, value=template(profile, user_text_file_profile="fa-user-text-files-v1",
            capabilities=(SKILLIFY_REF,) if profile in {V2_PROFILE, V3_PROFILE} else ()))
        assert selected.status_code == 200, selected.text
        capabilities = SKILL_DRAFT_CAPABILITIES if profile in {V2_PROFILE, V3_PROFILE} else HISTORY_SKILL_DRAFT_CAPABILITIES
    bridges, options, requests, receipts = [], [], [], []
    force_stops = []
    initialize = hosted.AssistantSession.__init__
    create = CopilotClient.create_session
    force_stop = CopilotClient.force_stop

    def bridge(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    async def created(instance, **kwargs):
        options.append(kwargs)
        return await create(instance, **kwargs)

    async def forced(instance, *args, **kwargs):
        force_stops.append(True)
        return await force_stop(instance, *args, **kwargs)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", bridge)
    monkeypatch.setattr(CopilotClient, "create_session", created)
    monkeypatch.setattr(CopilotClient, "force_stop", forced)
    prompt = assemble_prompt(profile)
    expected_prefix = prompt.system_message

    def provider(request):
        body = json.loads(request.content)
        number = len(requests) + 1
        tools = {item["function"]["name"]: item["function"] for item in body["tools"]}
        assert set(tools) == set(capabilities)
        assert set(tools["save_skill_draft"]["parameters"]["properties"]) == {"purpose", "body"}
        assert expected_prefix in [item["content"] for item in body["messages"] if item["role"] == "system"]
        if number == 1:
            delta = {"tool_calls": [{"index": 0, "id": "draft-procedure", "type": "function",
                "function": {"name": "save_skill_draft", "arguments": json.dumps({
                    "purpose": PURPOSE, "body": BODY,
                })}}]}
        else:
            tool = [item for item in body["messages"] if item["role"] == "tool"][-1]
            text = tool["content"] if isinstance(tool["content"], str) else "".join(
                part["text"] for part in tool["content"])
            receipt = SkillDraftReceipt.model_validate_json(text)
            assert receipt.approved is False and receipt.effective_version_id is None
            assert receipt.sha256 == hashlib.sha256(BODY.encode("utf-8")).hexdigest()
            receipts.append(receipt)
            delta = {"content": "Saved one unapproved Team-visible skill draft; it still needs human review."}
        requests.append({"number": number})
        assert number <= 2
        return sse(request, delta, number)

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_SKILL_DRAFT_PROVIDER")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: None, require_current_native_evidence=lambda: None))
    parent = Path(os.environ.get("FA_PROMPT_TEST_ROOT", str(Path(__file__).resolve().parents[2] / ".azure")))
    parent.mkdir(parents=True, exist_ok=True)
    with TestClient(h.app) as client, TemporaryDirectory(prefix="skill-draft-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory)
        user_text = "Please save this non-secret operating procedure as a skill draft."
        if profile == V3_PROFILE:
            user_text += "\n" + BODY
        accepted, _ = submit(h, "native-skill-draft", user_text)
        captured = control.records[accepted["binding_id"]].profile.instruction.capabilities
        combined = assemble_capabilities(profile, captured)
        if combined is not None:
            expected_prefix = combined.system_message
        completed = h.runtime.controller.step(accepted["binding_id"])
        assert completed.state == "succeeded" and completed.result_forwarded
        assert len(bridges) == len(options) == 1 and len(requests) == 2 and len(receipts) == 1
        assert bridges[0]._sdk_stop.graceful and bridges[0]._sdk_stop.runtime_version == "1.0.83"
        assert force_stops == []
        assert options[0]["available_tools"].to_list() == ["custom:" + name for name in capabilities]
        assert options[0]["excluded_tools"].to_list() == ["builtin:*", "mcp:*"]
        assert options[0]["mcp_servers"] == {} and options[0]["enable_config_discovery"] is False
        assert all(options[0][field] == [] for field in (
            "skill_directories", "plugin_directories", "instruction_directories", "custom_agents"))
        binding = h.runtime.bindings[accepted["binding_id"]]
        assert (control.records[binding.binding_id].profile_selection_id is None) == (profile == SKILL_DRAFT_PROFILE)
        archive = saved_turn(h, binding.job_id)
        assert archive.skill_draft_receipt == receipts[0] and archive.prompt_assembly == prompt.record
        assert archive.capability_assembly == (None if combined is None else combined.record)
        version = h.artifacts.version(A, A.team, receipts[0].artifact_id, receipts[0].version_id)
        assert version.approval is None and h.artifacts.get(A, A.team, receipts[0].artifact_id).payload["effective_version_id"] is None
        events = events_from(bridges[0]._session_files.snapshot().files)
        assert [event["data"]["content"] for event in events if event["type"] == "user.message"] == [user_text]
        projection = client.get("/work-policy/v1/conversations/" + accepted["conversation_id"], headers=h.headers())
        assert projection.status_code == 200 and projection.json()["can_follow_up"]
        assert projection.json()["turns"][0]["prompt_assembly"] == prompt.record.model_dump(mode="json")
        record_property("offline_wiring", json.dumps({
            "profile": profile,
            "sdk_version": importlib.metadata.version("github-copilot-sdk"),
            "runtime_instances": len(bridges),
            "sdk_stop": bridges[0]._sdk_stop.model_dump(mode="json"),
            "force_stop_calls": len(force_stops),
            "provider_requests": len(requests),
            "tools": list(capabilities),
            "prompt_assembly": prompt.record.model_dump(mode="json"),
            "capability_assembly": None if combined is None else combined.record.model_dump(mode="json"),
            "draft_approved": receipts[0].approved,
            "effective_version_id": receipts[0].effective_version_id,
            "original_user_events_exact": True,
            "history_validated": True,
            "paid_model_requests": 0,
            "cloud_requests": 0,
        }))
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
