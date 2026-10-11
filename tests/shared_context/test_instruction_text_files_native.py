"""Native SDK input-file transport and cold resume using only a scripted local provider."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from copilot.session import CopilotSession
from fastapi.testclient import TestClient

from sdk_checkpoint_local import sse
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_hosted import AssistantSession
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_text_files import UserTextFile
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_instruction_flow import check_history, instruction_models
from test_instruction_text_files import BODY, configured_files, post
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key


def test_literal_file_is_user_data_in_actual_sdk_requests_and_cold_resume(assistant, monkeypatch, caplog):
    h, control, _ = configured_files(assistant)
    packets, sent, stops = [], [], []
    original_send = CopilotSession.send
    original_stop = AssistantSession._capture_stop
    async def observe(session, prompt, **options):
        sent.append((session.session_id, prompt, options.get("attachments")))
        return await original_send(session, prompt, **options)
    monkeypatch.setattr(CopilotSession, "send", observe)
    def stopped(session, value):
        original_stop(session, value)
        stops.append(session._sdk_stop)
    monkeypatch.setattr(AssistantSession, "_capture_stop", stopped)
    for method in ("read_graph", "read_exact"):
        monkeypatch.setattr(h.sources, method, lambda *args, **kwargs: pytest.fail("No referenced source was requested"))

    def provider(request):
        assert str(request.url) == "https://provider.invalid/openai/v1/chat/completions"
        assert len(packets) < 2
        body = json.loads(request.content)
        assert {tool["function"]["name"] for tool in body["tools"]} == {"read_context", "execute_operation"}
        containing = [message for message in body["messages"] if "USER_FILE_CANARY" in json.dumps(message, ensure_ascii=False)]
        assert containing and all(message["role"] == "user" for message in containing)
        packets.append(body)
        return sse(request, {"content": "The input file contains value 42."}, len(packets))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_LITERAL_FILE")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant), installation=lambda: None,
        require_current_native_evidence=lambda: None))
    parent = Path(__file__).resolve().parents[2] / ".azure"
    parent.mkdir(exist_ok=True)
    with TestClient(h.app) as client, TemporaryDirectory(prefix="literal-files-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory) / "first"
        prompt = "  Read the uploaded text without using tools.\r\n"
        file = UserTextFile(name="data.csv", text=BODY)
        first = post(h, InstructionSubmit(request_id="native-file-first", content=prompt, files=(file,)))
        assert first.status_code == 200, first.text
        first = first.json()
        assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
        h.executor.root = Path(directory) / "second"
        second = post(h, InstructionSubmit(request_id="native-file-followup", content="What value was in the original file?",
                                           conversation_id=first["conversation_id"]))
        assert second.status_code == 200, second.text
        assert h.runtime.controller.step(second.json()["binding_id"]).state == "succeeded"
        assert len(packets) == len(sent) == 2 and sent[0][0] == sent[1][0]
        assert len(stops) == 2 and all(stop.graceful and stop.runtime_version == "1.0.83" for stop in stops)
        assert sent[0][1] == prompt and sent[0][2][0]["text"] == BODY and sent[1][2] is None
        assert sent[0][2][0]["filePath"] == "data.csv"
        assert not list(Path(directory).rglob("data.csv"))
        assert "Exception in callback" not in caplog.text
        saved = client.get(f"/work-policy/v1/conversations/{first['conversation_id']}/turns/1/files/data.csv",
                           headers=h.headers("read-only"))
        assert saved.status_code == 200 and saved.content == BODY.encode("utf-8")
        assert control.records[first["binding_id"]].profile.instruction.files == (file,)
        check_history(h)
