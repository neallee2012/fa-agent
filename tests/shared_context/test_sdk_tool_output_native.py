"""Pinned SDK custom reader through the original SessionFs; scripted model transport, no cloud or shell."""

import asyncio
import json

import httpx
import pytest

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.m2_copilot import run_copilot
from shared_context.native_sdk import NativeToolOutputPage, tool_output_reader
from shared_context.native_turns import NativeTurnControl
from test_m2_copilot import ScriptedEndpoint, runtime_path, workspace
from test_instruction_responses_native import response_bytes

OUTPUT_PATH = "/session-state/temp/offline-copilot-tool-output.txt"
PRIVATE = "OFFLINE-private-session-data-must-not-reach-model"


@pytest.mark.parametrize("responses", [False, True])
def test_pinned_sdk_reads_output_range_and_directory_but_not_private_state(workspace, runtime_path, responses):
    files = SdkSessionFiles("offline-tool-output", model_provider="openai-responses" if responses else None)
    content = "".join(f"original-output-line-{number}\n" for number in range(6000))

    def call(identifier, path, offset=0, limit=8000):
        arguments = {"path": path}
        arguments.update(offset=offset, limit=limit)
        return {"tool_calls": [{"index": 0, "id": identifier, "type": "function",
                                "function": {"name": "read_tool_output", "arguments": json.dumps(arguments)}}]}

    endpoint = ScriptedEndpoint([
        call("read-range", OUTPUT_PATH, content.index("original-output-line-29\n"), 100),
        call("read-directory", "/session-state/temp"),
        call("deny-private", "/session-state/private.txt"),
        {"content": "Read the original output without shell."},
    ])
    endpoint.unbounded = True
    response_requests = []

    def response_provider(request):
        response_requests.append(json.loads(request.content))
        number = len(response_requests)
        arguments = [
            {"path": OUTPUT_PATH, "offset": content.index("original-output-line-29\n"), "limit": 100},
            {"path": "/session-state/temp"},
            {"path": "/session-state/private.txt"},
        ]
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=response_bytes(
            number, tool="read_tool_output" if number <= 3 else None,
            arguments=arguments[number - 1] if number <= 3 else None, text="Read the original output without shell."))

    async def reserve(_):
        return None

    async def check():
        await files.write_file(OUTPUT_PATH, content)
        await files.write_file("/session-state/private.txt", PRIVATE)
        result = await run_copilot(
            state_dir=workspace / "state", working_directory=workspace / "work",
            sdk_session_id=files.session_id, resume=False, prompt="Read the saved tool output with read_tool_output.",
            tools=[tool_output_reader(files, NativeTurnControl())],
            api_url="https://context.invalid", token_provider=endpoint.token, on_event=lambda _: None,
            timeout_seconds=60, runtime_path=runtime_path,
            http_transport=httpx.MockTransport(response_provider) if responses else endpoint.transport,
            model="gpt-5.4-mini" if responses else "gpt-4.1-mini",
            model_provider="openai-responses" if responses else None,
            instruction=True, session_files=files,
            behavior_boundary=AssistantModelBoundary(
                reserve, text_reply=True, streaming=True, capability_results={"read_tool_output": NativeToolOutputPage}),
        )
        assert result["final_message"] == "Read the original output without shell."
        assert not files.failed

    asyncio.run(check())
    requests = response_requests if responses else endpoint.requests
    assert len(requests) == 4
    names = ([tool["name"] for tool in requests[0]["tools"]] if responses else
             [tool["function"]["name"] for tool in requests[0]["tools"]])
    assert names == ["read_tool_output"]
    if responses:
        replies = {item["call_id"]: json.dumps(item["output"])
                   for request in requests for item in request["input"] if item["type"] == "function_call_output"}
        original, directory = replies["call_1"], replies["call_2"]
    else:
        replies = {message["tool_call_id"]: message["content"]
                   for request in requests for message in request["messages"] if message["role"] == "tool"}
        original, directory = replies["read-range"], replies["read-directory"]
    assert "original-output-line-29" in original and "original-output-line-31" in original
    assert "offline-copilot-tool-output.txt" in directory
    assert "original-output-line-100" not in original
    assert PRIVATE not in json.dumps(requests)
    assert files.files["temp/offline-copilot-tool-output.txt"] == content
