"""B1 PA-11 on the pinned SDK 1.0.13 / runtime 1.0.83: ask_user reaches the turn's wait and its answer the model."""

import asyncio
import json

import httpx
import pytest

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.m2_copilot import run_copilot
from test_instruction_responses_native import response_bytes
from test_m2_copilot import ScriptedEndpoint, runtime_path, workspace  # noqa: F401

QUESTION = {"question": "Which colour should the chart use?", "choices": ["red", "blue"]}


class Wait:
    """Stands in for NativeHitlRuntime at the SDK seam only; its own wait is covered offline."""

    def __init__(self):
        self.asked = []

    def observe(self, event):
        pass

    async def user_input(self, request, invocation):
        self.asked.append((dict(request), invocation))
        return {"answer": "blue", "wasFreeform": False}


async def reserve(identity):
    return None


@pytest.mark.parametrize("enabled", [True, False])
def test_chat_completions_lane(workspace, runtime_path, enabled):
    call = {"tool_calls": [{"index": 0, "id": "call-ask", "type": "function",
                            "function": {"name": "ask_user", "arguments": json.dumps(QUESTION)}}]}
    endpoint = ScriptedEndpoint([call, {"content": "Using the answer."}])
    endpoint.unbounded = True
    wait = Wait() if enabled else None
    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work", sdk_session_id="ask-user-chat",
        resume=False, prompt="Draw the chart; ask me which colour.", tools=[], api_url="https://context.invalid",
        token_provider=endpoint.token, on_event=lambda _: None, timeout_seconds=60, runtime_path=runtime_path,
        http_transport=endpoint.transport, instruction=True, native_hitl=wait,
        behavior_boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True)))
    assert result["final_message"] == "Using the answer."
    names = [tool["function"]["name"] for tool in endpoint.requests[0].get("tools", [])]
    replies = [message for message in endpoint.requests[1]["messages"] if message["role"] == "tool"]
    if enabled:
        assert names == ["ask_user"]
        assert wait.asked == [({**QUESTION, "allowFreeform": True}, {"session_id": "ask-user-chat"})]
        assert replies[0]["tool_call_id"] == "call-ask" and replies[0]["content"] == "User selected: blue"
    else:
        assert names == []
        assert "User selected" not in json.dumps(replies)


@pytest.mark.parametrize("enabled", [True, False])
def test_responses_lane(workspace, runtime_path, enabled):
    requests = []

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=response_bytes(
            len(requests), tool="ask_user" if len(requests) == 1 else None,
            arguments=QUESTION if len(requests) == 1 else None, text="Using the answer."))

    wait = Wait() if enabled else None
    result = asyncio.run(run_copilot(
        state_dir=workspace / "responses", working_directory=workspace / "work", sdk_session_id="ask-user-responses",
        resume=False, prompt="Draw the chart; ask me which colour.", tools=[], api_url="https://context.invalid",
        token_provider=lambda: "OFFLINE", on_event=lambda _: None, timeout_seconds=60, runtime_path=runtime_path,
        http_transport=httpx.MockTransport(provider), instruction=True, model="gpt-5.4-mini",
        model_provider="openai-responses", native_hitl=wait,
        session_files=SdkSessionFiles("ask-user-responses", model_provider="openai-responses"),
        behavior_boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True)))
    assert result["final_message"] == "Using the answer."
    names = [tool.get("name") for tool in requests[0].get("tools", [])]
    outputs = [item for item in requests[1]["input"] if item.get("type") == "function_call_output"]
    if enabled:
        assert names == ["ask_user"]
        assert wait.asked == [({**QUESTION, "allowFreeform": True}, {"session_id": "ask-user-responses"})]
        assert outputs[0]["call_id"] == "call_1" and "User selected: blue" in json.dumps(outputs[0]["output"])
    else:
        assert names == []
        assert "User selected" not in json.dumps(outputs)
