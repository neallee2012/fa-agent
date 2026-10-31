"""One real SDK cancellation after a confirmed copy and signed source update; offline provider."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from copilot import CopilotClient
from copilot.session import CopilotSession
from fastapi.testclient import TestClient

from sdk_checkpoint_local import sse
from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_skill_copy import SopCopyReceipt
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.behavior_sdk_compaction import events_from
from web_test_console.freeform import ConversationProjection
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_instruction_prompt import submit, saved_turn
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sop_skill_copy import approved_sop, change_source, copy_scope, configured_copy


def test_fixed_sdk_cancel_after_known_copy_keeps_historical_effect_and_real_stop(
        assistant, signing_key, jwk_client, monkeypatch):
    h = assistant
    source_http, agent, human_headers, source = approved_sop(h, signing_key, jwk_client)
    h, control, _ = configured_copy(h, copy_scope(source))
    bridges, requests, receipts, forced, aborts = [], [], [], [], []
    initialize, force_stop, abort = hosted.AssistantSession.__init__, CopilotClient.force_stop, CopilotSession.abort

    def opened(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    async def stop_forced(instance):
        forced.append(True)
        return await force_stop(instance)

    async def aborted(instance, *args, **kwargs):
        aborts.append(True)
        return await abort(instance, *args, **kwargs)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", opened)
    monkeypatch.setattr(CopilotClient, "force_stop", stop_forced)
    monkeypatch.setattr(CopilotSession, "abort", aborted)

    def provider(request):
        payload = json.loads(request.content)
        assert "APPROVED_TEAM_SOP_CANARY" not in json.dumps(payload)
        requests.append(True)
        if len(requests) == 1:
            return sse(request, {"tool_calls": [{"index": 0, "id": "original-copy", "type": "function",
                "function": {"name": "copy_sop_to_skill_draft", "arguments": "{}"}}]}, 1)
        assert len(requests) == 2
        result = [message for message in payload["messages"] if message["role"] == "tool"][-1]["content"]
        text = result if isinstance(result, str) else "".join(part["text"] for part in result)
        receipt = SopCopyReceipt.model_validate_json(text)
        assert receipt.state_scope == "at-original-copy-write" and receipt.approved is False
        receipts.append(receipt)
        change_source(source_http, agent, human_headers)
        binding_id = bridges[0].client.assignment.binding_id
        cancelled = h.client.post("/work-policy/v1/bindings/" + binding_id + "/cancel",
            headers=h.headers(), json={"request_id": "native-original-copy-cancel"})
        assert cancelled.status_code == 200, cancelled.text
        bridges[0].request_cancel()
        return sse(request, {"content": "The original task was cancelled after its copy was recorded."}, 2)

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_CANCELLED_COPY")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client, h.executor.model_transport = h.client, None
    original = "  Copy the original approved shared SOP; cancellation must preserve its original result.\r\n"
    with TemporaryDirectory(prefix="copy-cancel-") as directory:
        h.executor.root = Path(directory)
        with patch.object(h.artifacts, "create", wraps=h.artifacts.create) as creates, \
                patch.object(h.artifacts, "upload", wraps=h.artifacts.upload) as uploads:
            accepted, _ = submit(h, "native-copy-cancel", original)
            done = h.runtime.controller.step(accepted["binding_id"])
        assert done.state == "cancelled" and done.result_forwarded
        assert len(bridges) == 1 and len(receipts) == 1 and len(requests) == 2
        target_creates = [call for call in creates.call_args_list if call.args[2] == receipts[0].artifact_id]
        target_uploads = [call for call in uploads.call_args_list if call.args[2] == receipts[0].artifact_id]
        assert len(target_creates) == len(target_uploads) == 1
        binding = h.runtime.bindings[accepted["binding_id"]]
        job = h.workers.inspect(binding.job_id)
        result = h.assistant.result(job.binding)
        archive = saved_turn(h, binding.job_id)
        assert result.known_effects == (receipts[0].receipt_id,) and result.unresolved_operations == ()
        assert result.state == "cancelled" and not result.passed_checks
        assert archive.sop_copy_receipt == receipts[0] and archive.sop_copy_outcome.stage == "saved"
        assert archive.sdk_stop == bridges[0]._sdk_stop and archive.sdk_stop.graceful
        assert archive.sdk_stop.runtime_version == "1.0.83" and not forced and aborts
        events = events_from(bridges[0]._session_files.snapshot().files)
        assert [event["data"]["content"] for event in events if event["type"] == "user.message"] == [original]
        response = h.client.get("/work-policy/v1/conversations/" + accepted["conversation_id"], headers=h.headers())
        view = ConversationProjection.model_validate_json(response.content)
        assert view.turns[0].state == "cancelled" and view.turns[0].sop_copy_outcome.stage == "saved"
    if os.environ.get("FA_COPY_RESULT_EVIDENCE_DIR"):
        target = Path(os.environ["FA_COPY_RESULT_EVIDENCE_DIR"])
        target.mkdir(parents=True, exist_ok=True)
        (target / "fixed-sdk-copy-cancel.json").write_text(json.dumps({
            "scope": "One real fixed SDK with signed copy, signed source update and signed cancellation; offline provider",
            "runtime_mode": h.runtime.mode, "sdk_version": "1.0.13", "runtime_version": "1.0.83",
            "offline_requests": len(requests), "original_target_creates": len(target_creates),
            "original_target_uploads": len(target_uploads), "official_abort_calls": len(aborts),
            "sdk_stop": archive.sdk_stop.model_dump(mode="json"), "force_stop_calls": len(forced),
            "work_state": result.state, "known_effects_count": len(result.known_effects),
            "unresolved_effects_count": len(result.unresolved_operations),
            "historical_receipt": receipts[0].model_dump(mode="json"),
            "archive_kind": type(archive.archive).__name__, "archive_failure": archive.failure,
            "copy_outcome": archive.sop_copy_outcome.model_dump(mode="json"),
            "original_user_sdk_event_exact": True, "raw_source_in_model": False,
            "actual_graceful_stop": True, "cloud_paid_iam": "NOT_RUN",
        }, indent=2) + "\n", encoding="utf-8")
