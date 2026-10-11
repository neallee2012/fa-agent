"""True pinned SDK + original N1/N3, only synthetic model transport and official local State store."""

import asyncio
import json
import socket
from types import SimpleNamespace

import httpx
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeResult, NativeSubmit, NativeTurnCapture
from shared_context.native_sdk import NativeSdkExecutor
from shared_context.native_turns import NativeTurnRuntime
from test_instruction_responses_native import OPAQUE, response_bytes
from test_m2_copilot import runtime_path
from test_native_profiles import profile_capture
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, local_env
from test_sdk_compaction_product import SUMMARY


def test_real_sdk_native_responses_origin_manual_checkpoint_and_three_cold_turns(
        local_env, monkeypatch, tmp_path, runtime_path):
    import _socket

    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        root = tmp_path / name.lower()
        root.mkdir()
        monkeypatch.setenv(name, str(root))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")

    def loopback_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("C0 cannot open external sockets")
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    selected = profile_capture(platform="win32-x64").model_dump(mode="json")
    selected["model"]["id"] = "gpt-5.4-mini"
    selected = NativeTurnCapture.model_validate_json(json.dumps(selected))
    checkpoint_summary = SUMMARY.replace("</overview>", "</overview>\n").replace("</history>", "</history>\n")
    model_url = "https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1"
    requests, sources, outcomes = [], [], []

    def provider(request):
        assert str(request.url) == model_url + "/responses"
        body = json.loads(request.content)
        assert body["model"] == selected.model.id and body["store"] is False
        assert not body.get("previous_response_id")
        assert all(item.get("type") != "item_reference" for item in body["input"])
        assert "OFFLINE-token" not in request.content.decode("utf-8")
        requests.append(body)
        ordinal = len(requests)
        assert ordinal <= 5, "The offline script permits only the original five model calls"
        if ordinal == 2:
            returned = next(item for item in body["input"] if item.get("type") == "function_call_output")
            text = returned["output"]
            if isinstance(text, list):
                text = next(item["text"] for item in text if item.get("type") in {"input_text", "text"})
            origin = json.loads(text)
            assert origin["turn_origin"]["source_kind"] == "human"
            assert origin["turn_origin"]["request_id"] == "rid-1"
            assert origin["content"] == "Original user text"
        answers = {2: "FIRST-answer", 3: checkpoint_summary, 4: "SECOND-answer", 5: "THIRD-answer"}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response_bytes(
            ordinal, tool="read_context" if ordinal == 1 else None, text=answers.get(ordinal),
            public_summary=checkpoint_summary if ordinal == 3 else "Public offline reasoning summary.",
        ))

    class ObservedExecutor(NativeSdkExecutor):
        async def __call__(self, receipt, scope, previous, control):
            sources.append(control.checkpoint_turn)
            result = await super().__call__(receipt, scope, previous, control)
            outcomes.append(result)
            return result

    executor = ObservedExecutor(
        root=tmp_path / "native", runtime_path=runtime_path, api_url="https://api.offline.invalid",
        model_url=model_url, token_provider=lambda: "OFFLINE-token", model_transport=httpx.MockTransport(provider),
    )
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        cid = None
        for index, expected in enumerate(("FIRST-answer", "SECOND-answer", "THIRD-answer"), start=1):
            request = NativeSubmit(command=submit(f"rid-{index}", cid).command, capture=selected)
            assert harness.invoke(request).status_code == 202

            async def finished():
                async with asyncio.timeout(90):
                    while True:
                        receipt = await runtime.read(request.command.request_id)
                        if receipt.status in {"completed", "unknown", "busy_refused"}:
                            return receipt
                        await asyncio.sleep(0.02)

            receipt = client.portal.call(finished)
            assert receipt.status == "completed", receipt.failure_code
            cid = receipt.conversation_id
            saved = harness.call(runtime.result, NativeResult(conversation_id=cid, request_id=receipt.request_id))
            assert saved.text == expected
            assert receipt.usage.usage[0].requests == (2 if index < 3 else 1)
            assert receipt.usage.state == "complete"
        assert harness.invoke(request).status_code == 202
    assert len(requests) == 5 and len(outcomes) == 3
    assert len({result.sdk_stop.pid for result in outcomes}) == 3
    assert all(result.sdk_stop.graceful and result.sdk_stop.exit_code is not None
               and result.sdk_stop.release is None for result in outcomes)
    assert outcomes[0].state.codec == "session-fs-responses-v1"
    assert [result.state.codec for result in outcomes[1:]] == ["session-fs-responses-diag-v1"] * 2
    assert sources[2].previous.source.execution == outcomes[1].compaction
    assert sources[2].previous.source.execution.request_id == "rid-2"
    assert outcomes[2].compaction is None
    assert all(OPAQUE not in result.state.encoded().decode("utf-8") for result in outcomes)
