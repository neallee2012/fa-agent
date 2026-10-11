"""One fixed-SDK copy tool with signed local M3/runtime authority and offline provider."""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from copilot import CopilotClient
from fastapi.testclient import TestClient

from conftest import A
from sdk_checkpoint_local import sse
from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_skill_copy import SopCopyReceipt
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_sdk_compaction import events_from
from shared_context.behavior_work_policy_api import install_work_policy_routes
from web_test_console.freeform import ConversationProjection
from test_behavior_assistant import assistant
from test_instruction_flow import check_history, instruction_models
from test_instruction_prompt import submit, saved_turn
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sop_skill_copy import approved_sop, copy_scope, configured_copy, BODY, PROFILE
from test_sop_skill_copy_wire import PREFIX_HASH


def test_fixed_sdk_copy_returns_only_one_unapproved_receipt_without_model_body_input(
        assistant, signing_key, jwk_client, monkeypatch):
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, _ = configured_copy(h, copy_scope(source))
    bridges, forced, requests, receipts, options = [], [], [], [], []
    initialize = hosted.AssistantSession.__init__
    create = CopilotClient.create_session
    force_stop = CopilotClient.force_stop

    def bridge(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    async def created(instance, **kwargs):
        options.append(kwargs)
        return await create(instance, **kwargs)

    async def forced_stop(instance):
        forced.append(True)
        return await force_stop(instance)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", bridge)
    monkeypatch.setattr(CopilotClient, "create_session", created)
    monkeypatch.setattr(CopilotClient, "force_stop", forced_stop)
    prefix = assemble_prompt(PROFILE)

    def provider(request):
        body = json.loads(request.content)
        number = len(requests) + 1
        tools = {item["function"]["name"]: item["function"] for item in body["tools"]}
        assert set(tools) == {"read_context", "execute_operation", "copy_sop_to_skill_draft"}
        assert tools["copy_sop_to_skill_draft"]["parameters"]["properties"] == {}
        assert prefix.system_message in [item["content"] for item in body["messages"] if item["role"] == "system"]
        assert "APPROVED_TEAM_SOP_CANARY" not in json.dumps(body)
        if number == 1:
            result = {"tool_calls": [{"index": 0, "id": "copy-first", "type": "function",
                      "function": {"name": "copy_sop_to_skill_draft", "arguments": "{}"}}]}
        else:
            tool = [item for item in body["messages"] if item["role"] == "tool"][-1]
            text = tool["content"]
            text = text if isinstance(text, str) else "".join(part["text"] for part in text)
            receipt = SopCopyReceipt.model_validate_json(text)
            assert receipt.approved is False and receipt.effective_version_id is None
            assert receipt.sha256 == hashlib.sha256(BODY).hexdigest()
            receipts.append(receipt)
            result = ({"tool_calls": [{"index": 0, "id": "copy-repeat", "type": "function",
                       "function": {"name": "copy_sop_to_skill_draft", "arguments": "{}"}}]}
                      if number == 2 else {"content": "One Team skill draft copied; it is not human-approved or active."})
        requests.append({"number": number, "source_body_absent": True, "only_receipt_after_tool": number > 1})
        assert number <= 3
        return sse(request, result, number)

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_SOP_COPY_PROVIDER")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client, h.executor.model_transport = h.client, None
    original = "  Copy only the approved, shared SOP into the assigned draft; never approve it.\r\n"
    with TemporaryDirectory(prefix="sop-copy-") as directory:
        h.executor.root = Path(directory)
        with patch.object(h.artifacts, "create", wraps=h.artifacts.create) as new_artifact, \
                patch.object(h.artifacts, "upload", wraps=h.artifacts.upload) as upload:
            accepted, _ = submit(h, "sdk-sop-copy", original)
            completed = h.runtime.controller.step(accepted["binding_id"])
        assert completed.state == "succeeded" and completed.result_forwarded
        assert new_artifact.call_count == upload.call_count == 1
        assert len(receipts) == 2 and receipts[0] == receipts[1]
        binding = h.runtime.bindings[accepted["binding_id"]]
        archive = saved_turn(h, binding.job_id)
        assert archive.sop_copy_receipt == receipts[0] and archive.prompt_assembly == prefix.record
        assert archive.sdk_stop == bridges[0]._sdk_stop and archive.sdk_stop.graceful
        version = h.artifacts.version(A, A.team, receipts[0].artifact_id, receipts[0].version_id)
        assert h.artifacts.download(A, A.team, receipts[0].artifact_id, receipts[0].version_id, version.blob.etag)[1] == BODY
        assert version.approval is None and h.assistant.skills.catalog(A, A.team, (receipts[0].artifact_id,)).entries == ()
        events = events_from(bridges[0]._session_files.snapshot().files)
        assert [event["data"]["content"] for event in events if event["type"] == "user.message"] == [original]
        check_history(h)
        projection = h.client.get("/work-policy/v1/conversations/" + accepted["conversation_id"], headers=h.headers())
        parsed = ConversationProjection.model_validate_json(projection.content)
        assert parsed.can_follow_up and parsed.turns[0].prompt_assembly == prefix.record
        assert "APPROVED_TEAM_SOP_CANARY" not in parsed.model_dump_json()
    assert len(bridges) == len(options) == 1 and len(requests) == 3 and not forced
    assert options[0]["enable_config_discovery"] is False and options[0]["mcp_servers"] == {}
    assert all(options[0][field] == [] for field in ("skill_directories", "plugin_directories", "custom_agents"))
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
    if os.environ.get("FA_SOP_COPY_EVIDENCE_DIR"):
        destination = Path(os.environ["FA_SOP_COPY_EVIDENCE_DIR"])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "fixed-sdk-copy.json").write_text(json.dumps({
            "scope": "Signed local M3 and current Runtime ticket; fixed SDK with scripted offline provider",
            "runtime_mode": h.runtime.mode, "sdk_version": "1.0.13", "runtime_version": "1.0.83",
            "prefix_sha256": PREFIX_HASH, "prefix_utf8_bytes": prefix.record.prefix_utf8_bytes,
            "offline_requests": requests, "sdk_stop": archive.sdk_stop.model_dump(mode="json"),
            "force_stop_calls": len(forced), "artifact_creates": 1, "version_uploads": 1,
            "same_repeated_receipt": True, "target_exact_source_bytes": True,
            "draft_not_effective_or_in_catalog": True, "user_sdk_event_unchanged": True,
            "receipt": receipts[0].model_dump(mode="json"),
            "source_body_in_model": False, "native_loader_enabled": False,
            "cloud_paid_iam": "NOT_RUN", "full_SK08_SK10_M": "NOT_RUN",
        }, indent=2) + "\n", encoding="utf-8")
