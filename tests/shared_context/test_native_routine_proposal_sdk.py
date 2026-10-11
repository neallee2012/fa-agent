"""Pinned real SDK/runtime 1.0.83, official local N1, synthetic model/API HTTP: one tool list for every native turn.

dev 10-07 (#732): after a scheduled turn, the next human turn in the same conversation could no longer propose a
schedule. The pinned runtime tells the model a tool it used earlier is "no longer available" when a later turn drops
it, and never says it is back. Every turn now offers the same tools, so the resident SDK is also kept, not restarted.
"""

import asyncio
import json
import logging
import socket
from types import SimpleNamespace

import httpx
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture
from shared_context.native_sdk import NativeSdkExecutor
from shared_context.native_turns import NativeTurnRuntime
from test_instruction_responses_native import response_bytes
from test_m2_copilot import runtime_path  # noqa: F401
from test_native_routine_context import routine_client
from test_native_routine_turns import routine_request
from test_native_turn_wire import capture, submit
from test_native_turns import Harness, IDENTITY, local_env  # noqa: F401


class QuietHitl:
    """No card is opened in these turns; the end-of-turn close finds nothing pending."""

    async def write(self, path, command):
        return {}

    async def pending(self, query):
        return SimpleNamespace(references=(), next_after=None)


def test_a_scheduled_turn_keeps_the_human_tools_so_the_next_human_turn_can_use_them(
        local_env, monkeypatch, tmp_path, runtime_path, caplog):  # noqa: F811
    import _socket

    for name, value in {"FA_NATIVE_ROUTINES_ENABLED": "1", "FA_NATIVE_ROUTINE_PROPOSALS": "1",
                        "TZ": "Asia/Taipei", "COPILOT_SKIP_CLI_DOWNLOAD": "1"}.items():
        monkeypatch.setenv(name, value)
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
    tools, notices, terminals = [], [], {}

    def provider(request):
        body = json.loads(request.content)
        tools.append(sorted(tool["name"] for tool in body.get("tools", ())))
        for item in body["input"]:
            content = item.get("content")
            for part in content if isinstance(content, list) else ({"text": content or ""},):
                if "tools_changed_notice" in str(part.get("text", "")):
                    notices.append(part["text"])
        number = len(tools)
        # The first human turn uses a human-only tool, as a schedule proposal does in its first turn.
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response_bytes(
            number, tool="read_writing_style" if number == 1 else None, text=f"OFFLINE final {number}"))

    async def terminal(receipt):
        terminals[receipt.request_id] = receipt.status

    executor = NativeSdkExecutor(
        root=tmp_path / "sdk", runtime_path=runtime_path, api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-model-token", model_transport=httpx.MockTransport(provider),
        routine_client=routine_client(), hitl_client=QuietHitl())
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight, on_terminal=terminal)
    executor.reader = runtime
    caplog.set_level(logging.WARNING, logger="shared_context.m2_copilot")
    caplog.set_level(logging.WARNING, logger="shared_context.native_resident")
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)

        def finish(request):
            assert harness.invoke(request).status_code == 202

            async def wait():
                async with asyncio.timeout(90):
                    while request.command.request_id not in terminals:
                        await asyncio.sleep(0.02)

            harness.call(wait)
            receipt = harness.call(runtime.read, request.command.request_id)
            assert receipt.status == "completed", receipt.failure_code
            return receipt

        first = finish(NativeSubmit(command=submit(
            "human-first", text="Write a long-form report from the synthetic facts.").command, capture=selected))
        finish(routine_request(first.conversation_id).model_copy(update={"capture": selected}))
        finish(NativeSubmit(command=submit("human-again", first.conversation_id, text="Again, please.").command,
                            capture=selected))
    assert len(tools) == 4
    assert all(names == tools[0] for names in tools), tools
    assert {"propose_routine", "read_writing_style"} <= set(tools[0])
    assert notices == []
    assert not [record for record in caplog.records if record.getMessage().startswith(
        ("RESIDENT_SDK_RESTARTED", "RESIDENT_SDK_DISCARDED"))], "the kept SDK continues across turn origins"
