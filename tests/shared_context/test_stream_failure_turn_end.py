"""A provider stream that fails mid-body after several tool rounds ends the turn explicitly; nothing is resent.

Pinned SDK 1.0.13 / runtime 1.0.83 behind the real API app, Hosted session and checkpoint store; the tool is an
in-process stand-in for run_command and the provider is a scripted local transport.
"""

import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from shared_context import behavior_assistant_hosted as hosted
from shared_context import behavior_shell_capability as shell
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_history_prompt import HISTORY_PROFILE
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.m2_copilot import ToolOutput, ToolSpec
from sdk_checkpoint_local import sse
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models
from test_instruction_prompt import submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key

ROUNDS = 3


class _Broken(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"gpt-4.1-mini","choices":[]}\n\n'
        raise httpx.ReadError("upstream stream aborted")


def _quick_tool(session, grant):
    async def handle(packet):
        text = "ok\n"
        return ToolOutput(shell.CommandResult(exit_code=0, output=text, output_bytes=len(text),
                                              truncated=False).model_dump_json())
    tool = ToolSpec("run_command", "Run one bash command.", shell.RunCommandArguments.model_json_schema(), handle)
    return (tool,), {tool.name: shell.CommandResult}


def test_a_provider_stream_failure_after_tool_rounds_ends_the_turn(assistant, monkeypatch):
    h, control, human = configured(assistant)
    h.runtime.capability_grants = available_grants(None, ("fa-shell-v1",))
    chosen, _ = select(h, request_id="choose-shell", previous=None, value=template(
        HISTORY_PROFILE, user_text_file_profile="fa-user-text-files-v1",
        capabilities=(h.runtime.capability_grants["fa-shell-v1"].ref,)))
    assert chosen.status_code == 200, chosen.text
    monkeypatch.setitem(hosted.CAPABILITY_HANDLERS, "fa-shell-v1", _quick_tool)
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        index = len(requests)
        if index <= ROUNDS:
            return sse(request, {"tool_calls": [{"index": 0, "id": f"call-{index}", "type": "function",
                "function": {"name": "run_command", "arguments": json.dumps({"command": "echo ok"})}}]}, index)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Broken(), request=request)

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_STREAM_PROVIDER")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: None, require_current_native_evidence=lambda: None))
    parent = Path(__file__).resolve().parents[2] / ".azure"
    parent.mkdir(exist_ok=True)
    with TestClient(h.app, raise_server_exceptions=False) as client, \
            TemporaryDirectory(prefix="stream-fail-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory)
        accepted, _ = submit(h, "stream-fail", "Run a few commands.")
        began = time.monotonic()
        status = h.runtime.controller.step(accepted["binding_id"])
        elapsed = time.monotonic() - began
        conversation = control.conversation(human, accepted["conversation_id"])
    print("STREAM_FAIL", json.dumps({"state": status.state, "forwarded": status.result_forwarded,
        "elapsed": round(elapsed, 1), "provider_requests": len(requests),
        "can_follow_up": conversation["can_follow_up"], "blocker": conversation.get("follow_up_blocker")}))
    assert len(requests) == ROUNDS + 1, "the failed model request was resent"
    assert status.state == "failed" and status.result_forwarded, status
    assert elapsed < 60
