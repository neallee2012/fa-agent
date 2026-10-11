"""A real SDK reads an original file after compaction using the existing authorized user lineage."""

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
from shared_context.behavior_assistant_hosted import AssistantClient, AssistantSession
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_text_files import UserFileRead, UserTextFile
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_conversation_routing import conversation_view
from test_instruction_flow import check_history, instruction_models
from test_instruction_text_files import post
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_read_user_files import BODY, configured_reader


def test_native_tool_reads_exact_past_file_after_compaction_without_promoting_file_instructions(assistant, monkeypatch, caplog):
    h, control, _ = configured_reader(assistant, checkpoint=True)
    requests, session_ids, stops = [], [], []
    compacting = False
    original_model, original_send, original_stop = AssistantClient.send_model, CopilotSession.send, AssistantSession._capture_stop
    async def model(client, request, *, checkpoint_compaction=False):
        nonlocal compacting
        assert not compacting
        compacting = checkpoint_compaction
        try:
            return await original_model(client, request, checkpoint_compaction=checkpoint_compaction)
        finally:
            compacting = False
    async def sent(session, prompt, **options):
        session_ids.append(session.session_id)
        return await original_send(session, prompt, **options)
    def stopped(session, value):
        original_stop(session, value)
        stops.append(session._sdk_stop)
    monkeypatch.setattr(AssistantClient, "send_model", model)
    monkeypatch.setattr(CopilotSession, "send", sent)
    monkeypatch.setattr(AssistantSession, "_capture_stop", stopped)

    def provider(request):
        body = json.loads(request.content)
        assert str(request.url) == "https://provider.invalid/openai/v1/chat/completions"
        assert len(requests) < 4
        phase = "compaction" if compacting else "normal"
        requests.append((phase, body))
        if phase == "compaction":
            assert len(requests) == 2 and "ORIGINAL_USER_FILE_CANARY" in json.dumps(body["messages"])
            return sse(request, {"content": (
                "<overview>The user supplied a literal file named input.txt.</overview>"
                "<history>The file body is omitted. Use the original file reader if needed.</history>"
                "<checkpoint_title>User file</checkpoint_title>"
            )}, len(requests))
        assert {item["function"]["name"] for item in body["tools"]} == {
            "read_context", "execute_operation", "read_user_turn", "read_user_file",
        }
        system = [item for item in body["messages"] if item["role"] == "system"]
        assert len(system) == 1 and "ORIGINAL_USER_FILE_CANARY" not in json.dumps(system)
        if len(requests) == 1:
            assert "ORIGINAL_USER_FILE_CANARY" in json.dumps(body["messages"])
            return sse(request, {"content": "Original user file accepted."}, len(requests))
        if len(requests) == 3:
            # G-D: the file now travels in the first user message, which the runtime keeps verbatim through
            # compaction, so the model sees the summary plus that single retained copy; it still reads the
            # exact original by tool.
            messages = json.dumps(body["messages"])
            assert "The file body is omitted" in messages and messages.count("ORIGINAL_USER_FILE_CANARY") == 1
            return sse(request, {"tool_calls": [{
                "index": 0, "id": "read-file-after-summary", "type": "function",
                "function": {"name": "read_user_file", "arguments": '{"turn_number":1,"name":"input.txt"}'},
            }]}, len(requests))
        result = [item for item in body["messages"] if item["role"] == "tool"][-1]
        assert result["tool_call_id"] == "read-file-after-summary"
        content = result["content"]
        text = content if isinstance(content, str) else "".join(item["text"] for item in content)
        original = UserFileRead.model_validate_json(text)
        assert original.text == BODY and original.file.name == "input.txt"
        assert original.turn_source.request_id == "native-original-file"
        return sse(request, {"content": "Exact original file data was retrieved."}, len(requests))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_FILE_READER")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant), installation=lambda: None,
        require_current_native_evidence=lambda: None))
    parent = Path(__file__).resolve().parents[2] / ".azure"
    parent.mkdir(exist_ok=True)
    with TestClient(h.app) as client, TemporaryDirectory(prefix="user-file-reader-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory) / "first"
        first = post(h, InstructionSubmit(request_id="native-original-file", content="Retain this exact input file.",
            files=(UserTextFile(name="input.txt", text=BODY),))).json()
        assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
        h.executor.root = Path(directory) / "second"
        second = post(h, InstructionSubmit(request_id="native-past-file", content="Read input.txt from turn 1 exactly.",
                                           conversation_id=first["conversation_id"])).json()
        assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
        assert [phase for phase, _ in requests] == ["normal", "compaction", "normal", "normal"]
        assert len(session_ids) == len(stops) == 2 and session_ids[0] == session_ids[1]
        assert all(stop.graceful and stop.runtime_version == "1.0.83" for stop in stops)
        projected = conversation_view(h, second)
        events = projected["turns"][-1]["progress"]["events"]
        starts = [event for event in events if event["kind"] == "tool_started" and event["tool_name"] == "read_user_file"]
        completed = [event for event in events if event["kind"] == "tool_completed" and event["tool_name"] == "read_user_file"]
        assert len(starts) == len(completed) == 1
        assert completed[0]["tool_start_seq"] == starts[0]["seq"] and completed[0]["status"] == "succeeded"
        assert "read-file-after-summary" not in json.dumps(events) and "ORIGINAL_USER_FILE_CANARY" not in json.dumps(events)
        assert "Exception in callback" not in caplog.text
        check_history(h)
