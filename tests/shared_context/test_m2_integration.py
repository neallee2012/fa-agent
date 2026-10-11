from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from conftest import A
from shared_context.api import create_app
from shared_context.m2_assistant import DesktopAction
from shared_context.m2_copilot import ToolOutput, ToolSpec, run_copilot
from shared_context.m2_gateway import M2Gateway, M2Settings

RUNTIME = Path(os.environ.get("M2_TEST_RUNTIME", str(
    Path(__file__).resolve().parents[2] / ".azure" / "m2-runtime-1.0.83" / "prebuilds" / "win32-x64" / "copilot-runtime.exe"
)))
PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg+M/w"
    "HwAEAQH/cetH5QAAAABJRU5ErkJggg=="
)


def completion(index, delta, finish):
    common = {"id": f"chatcmpl-synthetic-{index}", "object": "chat.completion.chunk", "created": 1,
              "model": "gpt-4.1-mini-2025-04-14"}
    chunks = [
        {**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        {**common, "choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}},
    ]
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


@pytest.mark.skipif(not RUNTIME.is_file(), reason="requires explicitly provisioned SDK runtime 1.0.83")
def test_real_sdk_reaches_gateway_with_native_tool_image_and_server_enforced_limits(system, tmp_path):
    service = system[0]
    requests, calls, scopes, events = [], [], [], []

    class UpstreamCredential:
        def get_token(self, scope):
            scopes.append(scope)
            return SimpleNamespace(token="synthetic-upstream-credential")

    async def upstream(request):
        assert str(request.url) == "https://synthetic.services.ai.azure.com/api/projects/poc/openai/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_bridge", "type": "function",
                "function": {"name": "capture_frame", "arguments": "{}"},
            }]}
            content = completion(1, delta, "tool_calls")
        else:
            assert len(requests) == 2
            content = completion(2, {"role": "assistant", "content": "Bridge verified"}, "stop")
        return httpx.Response(200, content=content.encode(), headers={
            "content-type": "text/event-stream", "x-ms-request-id": "synthetic-request",
        })

    async def capture(packet):
        calls.append(packet["tool_call_id"])
        return ToolOutput(text="Synthetic screenshot supplied through the binary image channel.", png_base64=PNG)

    async def run():
        with_runtime = tmp_path / "work"
        with_runtime.mkdir()
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
            gateway = M2Gateway(service, M2Settings("bridge-unit", "gpt-4.1-mini", 4),
                                "https://synthetic.services.ai.azure.com/api/projects/poc", UpstreamCredential(), http)
            app = create_app(service, lambda: A, model_gateway=gateway)

            outcome = await run_copilot(
                state_dir=tmp_path / "sdk", working_directory=with_runtime, sdk_session_id=str(uuid4()),
                resume=False, prompt="Capture one frame and report the result.",
                tools=[ToolSpec("capture_frame", "Capture the synthetic frame.",
                                {"type": "object", "properties": {}, "additionalProperties": False}, capture)],
                api_url="https://context.example", token_provider=lambda: "synthetic-hosted-credential",
                on_event=events.append, timeout_seconds=45, runtime_path=RUNTIME,
                http_transport=httpx.ASGITransport(app),
            )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://context.example") as client:
                response = await client.get("/model/v1/budget")
                assert response.status_code == 200
                budget = response.json()
            return outcome, budget

    outcome, budget = asyncio.run(run())
    assert outcome["final_message"] == "Bridge verified"
    assert calls == ["call_bridge"] and len(requests) == 2
    for body in requests:
        assert body["model"] == "gpt-4.1-mini" and body["max_tokens"] == 1024
        assert body["n"] == 1 and body["store"] is False and body.get("parallel_tool_calls") is False
        assert "reasoning_effort" not in body and "snippy" not in body
    wire = json.dumps(requests[1]["messages"])
    assert "call_bridge" in wire and "data:image/png;base64," + PNG in wire
    assert scopes == ["https://ai.azure.com/.default"] * 2
    assert budget["attempts"] == 2 and budget["remaining"] == 2
    assert [record["usage"]["total_tokens"] for record in budget["records"]] == [16, 16]
    assert "synthetic-upstream-credential" not in json.dumps(outcome)
    assert base64.b64decode(PNG).startswith(b"\x89PNG")


@pytest.mark.skipif(not RUNTIME.is_file(), reason="requires explicitly provisioned SDK runtime 1.0.83")
def test_real_sdk_desktop_schema_rejects_captured_mixed_action_before_handler(system, tmp_path):
    requests, accepted = [], []
    actions = [
        {"type": "type", "x": 75, "y": 182, "text": "amber sample"},
        {"type": "click", "x": 124, "y": 199},
        {"type": "type", "text": "amber sample"},
    ]

    async def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        index = len(requests)
        if index == 2:
            failures = [message for message in body["messages"] if message["role"] == "tool"]
            assert failures and "Custom tool failed" not in json.dumps(failures)
        if index <= len(actions):
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": f"call_desktop_{index}", "type": "function",
                "function": {"name": "desktop_act", "arguments": json.dumps(actions[index - 1])},
            }]}
            content = completion(index, delta, "tool_calls")
        else:
            assert index == 4
            content = completion(index, {"role": "assistant", "content": "Schema boundary verified"}, "stop")
        return httpx.Response(200, content=content.encode(), headers={"content-type": "text/event-stream"})

    async def handler(packet):
        accepted.append(DesktopAction.model_validate(packet["arguments"]).model_dump(exclude_none=True))
        return ToolOutput(text="Boundary fixture only; no actual desktop action claimed.", png_base64=PNG)

    async def run():
        working = tmp_path / "work"
        working.mkdir()
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
            credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="synthetic-upstream"))
            gateway = M2Gateway(system[0], M2Settings("desktop-schema", "gpt-4.1-mini", 5),
                                "https://synthetic.services.ai.azure.com/api/projects/poc", credential, http)
            app = create_app(system[0], lambda: A, model_gateway=gateway)
            return await run_copilot(
                state_dir=tmp_path / "sdk", working_directory=working, sdk_session_id=str(uuid4()),
                resume=False, prompt="Exercise the supplied desktop action schema.",
                tools=[ToolSpec("desktop_act", "One exact click, type or key action.",
                                DesktopAction.model_json_schema(), handler)],
                api_url="https://context.example", token_provider=lambda: "synthetic-hosted",
                on_event=lambda event: None, timeout_seconds=45, runtime_path=RUNTIME,
                http_transport=httpx.ASGITransport(app),
            )

    outcome = asyncio.run(run())
    assert outcome["final_message"] == "Schema boundary verified"
    assert accepted == actions[1:]
    assert len(requests) == 4
    assert "data:image/png;base64," + PNG in json.dumps(requests[-1]["messages"])
