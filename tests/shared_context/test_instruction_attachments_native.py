"""Fixed SDK selection attachments through real source authority and a scripted provider."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from copilot.session import CopilotSession
from fastapi.testclient import TestClient

from sdk_checkpoint_local import sse
from shared_context import behavior_assistant_hosted as hosted
from shared_context import m2_copilot
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_instruction_attachments import BODY, configured_attachments, post
from test_instruction_flow import check_history, instruction_models
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key


def test_fixed_sdk_selection_uses_exact_text_and_retains_it_on_cold_resume(assistant, monkeypatch, caplog):
    h, control, _, attachment = configured_attachments(assistant)
    calls, sent, bridges, observed = [], [], [], []
    original_send = CopilotSession.send
    initialize = hosted.AssistantSession.__init__
    def bridge(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)
    async def send(instance, prompt, **options):
        sent.append((prompt, options.get("attachments")))
        return await original_send(instance, prompt, **options)
    monkeypatch.setattr(CopilotSession, "send", send)
    monkeypatch.setattr(hosted.AssistantSession, "__init__", bridge)

    def provider(request):
        assert str(request.url) == "https://provider.invalid/openai/v1/chat/completions"
        assert len(calls) < 2
        packet = json.loads(request.content)
        observed.append({"tools": [tool["function"]["name"] for tool in packet["tools"]],
                         "body_present": "ATTACHED_SOURCE_CANARY" in json.dumps(packet, ensure_ascii=False),
                         "name_present": "reference.txt" in json.dumps(packet)})
        assert {tool["function"]["name"] for tool in packet["tools"]} == {"read_context", "execute_operation"}
        assert "ATTACHED_SOURCE_CANARY" in json.dumps(packet, ensure_ascii=False)
        assert "reference.txt" in json.dumps(packet)
        calls.append(packet)
        return sse(request, {"content": "The retained attachment contains value 42."}, len(calls))

    gateway = AssistantProvider(
        "https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_ATTACHMENT_FIXTURE")),
        transport=httpx.MockTransport(provider),
    )
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    parent = Path(__file__).resolve().parents[2] / ".azure"
    parent.mkdir(exist_ok=True)
    with TestClient(h.app) as client, TemporaryDirectory(prefix="text-attachments-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory) / "first"
        original = "  Read the supplied attachment.\r\n"
        accepted = post(h, InstructionSubmit(request_id="native-attachment-first", content=original,
                                             attachments=(attachment,)))
        assert accepted.status_code == 200, accepted.text
        first = accepted.json()
        assert h.runtime.controller.step(first["binding_id"]).state == "succeeded", observed
        h.executor.root = Path(directory) / "second"
        second = post(h, InstructionSubmit(request_id="native-attachment-second", content="What was the supplied value?",
                                           conversation_id=first["conversation_id"]))
        assert second.status_code == 200, second.text
        assert h.runtime.controller.step(second.json()["binding_id"]).state == "succeeded"
        assert len(calls) == 2 and len(bridges) == 2
        # G-D: the original text is followed by the fixed file block; no SDK attachment (only its first was shown).
        assert sent[0] == (original + "\n\n" + m2_copilot._TEXT_FILES_HEADER + "\n"
                           + m2_copilot._text_file_block("reference.txt", BODY), None)
        assert sent[1][1] is None
        assert bridges[0].client.assignment.sdk_session_id == bridges[1].client.assignment.sdk_session_id
        assert all(item._sdk_stop.graceful and item._sdk_stop.runtime_version == "1.0.83" for item in bridges)
        assert not any((item.root / "work" / "reference.txt").exists() for item in bridges)
        assert "Exception in callback" not in caplog.text
        check_history(h)
