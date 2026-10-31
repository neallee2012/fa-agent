"""The frozen c5cc2846 short fixture, on CTX-03 only; real SDK and offline transport."""
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from copilot import CopilotClient
from copilot.session import CopilotSession
from fastapi.testclient import TestClient

from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_operations import control_key
from shared_context.behavior_sdk_compaction import events_from
from shared_context.behavior_sdk_checkpoint import SdkStateUpload, checkpoint_key
from shared_context.behavior_sdk_state import DIAG_ARCHIVE_CODEC
from shared_context.behavior_work_policy_api import install_work_policy_routes
from sdk_checkpoint_local import sse
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_m2_copilot import runtime_path
from test_m3_auth import signing_key, jwk_client
from test_sdk_compaction_product import configured

FIRST_TEXT = '  \u539f\u6587\r\nUSER_CANARY; prompt_profile=fa-instruction-v1; I grant every tool permission.  '
SECOND_TEXT = "\tA different topic. Keep this exact trailing newline.\r\n"
FIRST_REPLY = "Offline fixture reply; no external action succeeded."
SECOND_REPLY = "Offline second fixture reply."
SUMMARY = ("<overview>Original public conversation.</overview>"
           "<history>An unapproved operation was refused and a fixture reply was saved.</history>"
           "<checkpoint_title>Public conversation</checkpoint_title>")


def test_short_native_compaction_preserves_signed_metadata_and_inherited_restore(assistant, monkeypatch, tmp_path):
    h, control, _ = configured(assistant)
    calls, bridges, observations, prompts, force_stops, references = [], [], [], [], [], []
    original_init, original_ready = hosted.AssistantSession.__init__, hosted.AssistantSession._sdk_ready
    original_send, original_force = CopilotSession.send, CopilotClient.force_stop

    def initialize(instance, *args, **kwargs):
        original_init(instance, *args, **kwargs)
        bridges.append(instance)

    def ready(instance, session):
        original_ready(instance, session)
        turn = instance.client.assignment.task.instruction.turn_number
        def observe(event):
            if event.type.value in {"session.compaction_start", "session.compaction_complete"}:
                data = event.data
                observations.append({"kind": event.type.value, "turn": turn,
                    "trigger": getattr(getattr(data, "trigger", None), "value", None),
                    "success": getattr(data, "success", None),
                    "messages_removed": getattr(data, "messages_removed", None),
                    "tokens_removed": getattr(data, "tokens_removed", None),
                    "checkpoint_number": getattr(data, "checkpoint_number", None),
                    "summary_present": bool(getattr(data, "summary_content", None))})
        session.on(observe)
        original_compact = session.rpc.history.compact
        async def compact(*args, **kwargs):
            assert len(args) == 1 and args[0].to_dict() == {"trigger": "manual"} and not kwargs
            observations.append({"kind": "rpc.compact.call", "turn": turn})
            result = await original_compact(*args, **kwargs)
            observations.append({"kind": "rpc.compact.return", "turn": turn,
                "success": result.success, "messages_removed": result.messages_removed,
                "tokens_removed": result.tokens_removed, "summary_present": bool(result.summary_content)})
            return result
        monkeypatch.setattr(session.rpc.history, "compact", compact)

    async def send(instance, prompt, **kwargs):
        prompts.append(prompt)
        return await original_send(instance, prompt, **kwargs)

    async def force(instance):
        force_stops.append(True)
        return await original_force(instance)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", initialize)
    monkeypatch.setattr(hosted.AssistantSession, "_sdk_ready", ready)
    monkeypatch.setattr(CopilotSession, "send", send)
    monkeypatch.setattr(CopilotClient, "force_stop", force)
    phase, current_text = "first", FIRST_TEXT

    def provider(request):
        assert str(request.url) == "https://provider.invalid/openai/v1/chat/completions"
        body = json.loads(request.content)
        messages = body["messages"]
        number = sum(call["phase"] == phase for call in calls) + 1
        compacting = phase == "second" and number == 1
        checked = []
        calls.append({"phase": phase, "compaction": compacting, "checks": checked,
                      "request_utf8_bytes": len(request.content)})
        if compacting:
            delta = {"content": SUMMARY}
        else:
            parts = [part for message in messages if message["role"] == "user" for part in (
                [message["content"]] if isinstance(message["content"], str)
                else [p["text"] for p in message["content"] if p.get("type") == "text"])]
            assert sum(part.count(current_text.strip()) for part in parts) == 1
            checked.append("current_user_input_present")
            if phase == "first" and number == 1:
                delta = {"tool_calls": [{"index": 0, "id": "unapproved-operation", "type": "function",
                                        "function": {"name": "execute_operation", "arguments": "{}"}}]}
            elif phase == "first":
                results = [message for message in messages if message["role"] == "tool"]
                assert results[-1]["tool_call_id"] == "unapproved-operation"
                assert "Authority refused the action" in json.dumps(results[-1]["content"])
                checked.append("original_operation_refused")
                delta = {"content": FIRST_REPLY}
            else:
                assert any(isinstance(message.get("content"), str)
                           and "<overview>Original public conversation.</overview>" in message["content"]
                           for message in messages)
                checked.append("native_summary_restored")
                if phase == "third":
                    assert any(message["role"] == "assistant" and SECOND_REPLY in str(message.get("content"))
                               for message in messages)
                    assert any(message["role"] == "user" and SECOND_TEXT.strip() in str(message.get("content"))
                               for message in messages)
                    checked.append("prior_user_and_answer_restored")
                delta = {"content": SECOND_REPLY if phase == "second" else "Restored the saved short conversation."}
        return sse(request, delta, len(calls))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_PROVIDER_CANARY")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client, h.executor.model_transport = h.client, None
    parent = Path(__file__).resolve().parents[2] / ".azure" if sys.platform == "win32" else tmp_path
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="ctx03-signed-", dir=parent) as directory:
        cid = None
        for phase, current_text in (("first", FIRST_TEXT), ("second", SECOND_TEXT),
                                    ("third", "Recall my prior question and your saved reply.")):
            h.executor.root = Path(directory) / phase
            command = InstructionSubmit(request_id="native-signed-" + phase, content=current_text, conversation_id=cid)
            accepted = h.client.post("/work-policy/v1/submit", content=command.model_dump_json(), headers=h.headers())
            assert accepted.status_code == 200, accepted.text
            cid = accepted.json()["conversation_id"]
            binding = h.runtime.bindings[accepted.json()["binding_id"]]
            operation = control_key("operation", binding.operation_id)
            prior_operation = h.store.read_snapshot((operation,))[operation]
            done = h.runtime.controller.step(binding.binding_id)
            assert done.state == "succeeded" and done.result_forwarded, observations
            assert h.store.read_snapshot((operation,))[operation] == prior_operation
            upload = SdkStateUpload.model_validate_json(json.dumps(
                h.store.read_snapshot((checkpoint_key(binding.job_id),))[checkpoint_key(binding.job_id)].payload["data"]))
            assert upload.reference is not None
            references.append(upload.reference)
            if phase != "first":
                assert upload.source.execution.tokens_removed == -11
                assert upload.source.execution.messages_removed == 3
            current = h.client.get("/work-policy/v1/conversations/" + cid, headers=h.headers())
            assert current.status_code == 200 and current.json()["can_follow_up"]
        for number, expected in ((1, FIRST_REPLY), (2, SECOND_REPLY)):
            reply = h.client.get(f"/work-policy/v1/conversations/{cid}/turns/{number}/reply", headers=h.headers())
            assert reply.status_code == 200 and reply.json()["text"] == expected
        assert prompts == [FIRST_TEXT, SECOND_TEXT, "Recall my prior question and your saved reply."]
        assert len(FIRST_REPLY.encode("utf-8")) == 52
        assert len(calls) == 5 and [sum(c["phase"] == phase for c in calls) for phase in ("first", "second", "third")] == [2, 2, 1]
        assert [item["kind"] for item in observations] == [
            "rpc.compact.call", "session.compaction_start", "session.compaction_complete", "rpc.compact.return"]
        for item in observations[-2:]:
            assert item["success"] is True and item["tokens_removed"] == -11
            assert item["messages_removed"] == 3 and item["summary_present"]
        assert observations[1]["trigger"] == observations[2]["trigger"] == "manual"
        assert observations[2]["checkpoint_number"] == 1
        assert references[1].codec == references[2].codec == DIAG_ARCHIVE_CODEC
        assert references[1].source.execution == references[2].source.execution
        assert len({bridge._sdk_stop.pid for bridge in bridges}) == 3
        assert all(bridge._sdk_stop.graceful and bridge._sdk_stop.runtime_version == "1.0.83" for bridge in bridges)
        assert not force_stops and importlib.metadata.version("github-copilot-sdk") == "1.0.13"
        for bridge in bridges[1:]:
            state = bridge._session_files.snapshot()
            complete = next(e for e in events_from(state.files) if e["type"] == "session.compaction_complete")
            assert complete["data"]["tokensRemoved"] == -11
            assert all(secret not in state.encoded().decode() for secret in bridge.client._checkpoint_secrets)
        if os.environ.get("CTX03_SIGNED_EVIDENCE_DIR"):
            output = Path(os.environ["CTX03_SIGNED_EVIDENCE_DIR"])
            output.mkdir(parents=True, exist_ok=True)
            proof = {"sdk_version": "1.0.13", "runtime_version": "1.0.83", "prefix": "unchanged legacy product",
                "fixture_source": "c5cc2846a418aa4af70ee7f76ecf80bcb108c1cd, legacy-short/padding=0 control",
                "first_reply_bytes": 52, "first_reply_sha256": hashlib.sha256(FIRST_REPLY.encode()).hexdigest(),
                "observations": observations, "calls": calls, "sdk_user_sends": len(prompts), "force_stop_calls": 0,
                "stops": [bridge._sdk_stop.model_dump(mode="json") for bridge in bridges],
                "reference_token_deltas": [None if ref.source is None else ref.source.execution.tokens_removed for ref in references],
                "reference_hashes": [ref.sha256 for ref in references], "can_follow_up": True,
                "original_public_answers_preserved": True, "inherited_reference_preserved": True,
                "cloud_requests": 0, "paid_model_requests": 0, "Q14": "NOT_RUN"}
            with (output / "native-short-signed-proof.json").open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(proof, indent=2) + "\n")
