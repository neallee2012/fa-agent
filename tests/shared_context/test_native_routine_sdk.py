"""Pinned real SDK/runtime, official local N1, synthetic model/API HTTP only; no external network."""

import asyncio
import json
import socket
from types import SimpleNamespace

import httpx
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeRead, NativeSubmit, NativeTurnCapture
from shared_context.native_sdk import NativeSdkExecutor
from shared_context.native_turns import NativeTurnRuntime
from shared_context.native_work_context import LONG_FORM_STYLE
from test_instruction_responses_native import OPAQUE, response_bytes
from test_m2_copilot import runtime_path
from test_native_routine_context import routine_client
from test_native_routine_turns import routine_request
from test_native_turn_wire import capture, submit
from test_native_turns import Harness, IDENTITY, local_env


def test_real_sdk_keeps_scheduled_origin_and_reads_work_and_style_without_changing_human_input(
        local_env, monkeypatch, tmp_path, runtime_path):
    import _socket

    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        path = tmp_path / name.lower()
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH"):
        monkeypatch.delenv(name, raising=False)

    def loopback_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("this SDK qualification cannot open external sockets")
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    values = capture().model_dump(mode="json")
    values["model"]["id"] = "gpt-5.4-mini"
    values["template"]["settings"]["turn_origin_profile"] = "fa-instruction-context-origin-v1"
    selected = NativeTurnCapture.model_validate_json(json.dumps(values))
    requests, returned, terminals = [], [], {}

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        number = len(requests)
        assert number <= 5 and body["store"] is False
        text = json.dumps(body, ensure_ascii=False)
        if number == 1:
            assert "## Scheduled routine event" not in text and LONG_FORM_STYLE not in text
        if number == 2:
            assert "## Scheduled routine event" in text
        if number in {3, 5}:
            output = [item for item in body["input"] if item.get("type") == "function_call_output"][-1]["output"]
            if isinstance(output, list):
                output = next(item["text"] for item in output if item.get("type") in {"text", "input_text", "output_text"})
            returned.append(json.loads(output))
        tool = {2: "read_work_context", 4: "read_writing_style"}.get(number)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=response_bytes(number, tool=tool, text=f"OFFLINE final {number}"))

    async def terminal(receipt):
        terminals[receipt.request_id] = receipt.status

    executor = NativeSdkExecutor(
        root=tmp_path / "sdk", runtime_path=runtime_path, api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-model-token", model_transport=httpx.MockTransport(provider),
        routine_client=routine_client())
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight, on_terminal=terminal)
    executor.reader = runtime
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)

        def finish(request):
            assert harness.invoke(request).status_code == 202

            async def wait():
                async with asyncio.timeout(90):
                    while request.command.request_id not in terminals:
                        await asyncio.sleep(0.02)

            harness.call(wait)
            response = harness.invoke(NativeRead(request_id=request.command.request_id))
            assert response.status_code == 200, response.text
            receipt = harness.call(runtime.read, request.command.request_id)
            assert receipt.status == "completed", receipt.failure_code
            return receipt

        first = finish(NativeSubmit(command=submit("human-first").command, capture=selected))
        request = routine_request(first.conversation_id).model_copy(update={"capture": selected})
        scheduled = finish(request)
        long = finish(NativeSubmit(command=submit(
            "human-long", first.conversation_id, text="Write a long-form report from the synthetic facts.").command,
            capture=selected))
        assert harness.invoke(request).json()["submit_digest"] == scheduled.submit_digest
    assert len(requests) == 5
    assert returned[0]["data"]["source_kind"] == "routine"
    assert returned[0]["request_id"] == scheduled.request_id
    assert returned[1]["request_id"] == "human-long" and returned[1]["body"] == LONG_FORM_STYLE
    assert {part.name: part for part in scheduled.observed_assembly.parts}["routines"].state == "included"
    assert {part.name: part for part in long.observed_assembly.parts}["writing_style"].state == "read"
    assert [item.usage.usage[0].requests for item in (first, scheduled, long)] == [1, 2, 2]
    assert OPAQUE not in json.dumps(returned)
