"""One pinned SDK/runtime turn, two scripted model replies, and the production Responses handler."""

import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
from shared_context.native_sdk import NativeSdkExecutor
from shared_context.native_turns import NativeTurnRuntime
from test_m2_copilot import runtime_path
from test_native_response_streaming import sse_events
from test_native_responses import ResponsesHarness, build_responses_host, response_submit
from test_native_turns import IDENTITY, local_env


def test_pinned_sdk_public_events_and_display_failure_do_not_change_the_saved_turn(
        local_env, monkeypatch, runtime_path, caplog):
    supplied = os.environ.get("N6_NATIVE_WORKSPACE")
    if not supplied:
        pytest.fail("Set a unique session-private N6_NATIVE_WORKSPACE; never run against the real user HOME")
    root = Path(supplied).resolve()
    root.mkdir(parents=True, exist_ok=False)
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        directory = root / name.lower()
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH",
                 "FOUNDRY_HOSTING_ENVIRONMENT", "FA_NATIVE_TURNS", "FA_N0_STATE_PROBE",
                 "FA_N6_RESPONSES_PROBE", "FA_N6_RESPONSES_INTERRUPT_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.chdir(root)
    import _socket

    def loopback_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("The native Responses case permits only loopback connections")
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    final_text = "OFFLINE-native-response-finished"
    requests, stops, event_types = [], [], []
    display_failure = []
    model_root = "https://offline.services.ai.azure.com/api/projects/offline/openai/v1"

    def provider(request):
        assert str(request.url) == model_root + "/chat/completions"
        payload = json.loads(request.content)
        requests.append(payload)
        assert len(requests) <= 2, "Only the two scripted model replies are permitted"
        assert payload["stream"] is True and payload["model"] == "OFFLINE-model"
        common = {"id": "offline-scripted-model", "object": "chat.completion.chunk",
                  "created": 1, "model": "OFFLINE-model"}
        if len(requests) == 1:
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call-original-context", "type": "function",
                "function": {"name": "read_context", "arguments": "{}"},
            }]}
            reason = "tool_calls"
        else:
            assert any(message.get("role") == "tool" for message in payload["messages"])
            assert "foundry-responses-v1" in json.dumps(payload, ensure_ascii=False)
            delta, reason = {"role": "assistant", "content": final_text}, "stop"
        chunks = [
            {**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
            {**common, "choices": [], "usage": {"prompt_tokens": 17, "completion_tokens": 7, "total_tokens": 24}},
        ]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    class ObserveSdk(NativeSdkExecutor):
        async def __call__(self, receipt, scope, previous, control):
            original = control.on_event
            assert original is not None

            def display(event):
                event_types.append(event.get("type"))
                if not display_failure and event.get("type") not in {"assistant.message_delta", "tool.execution_start"}:
                    display_failure.append(True)
                    raise OSError("PRIVATE-display-error-detail")
                original(event)

            control.on_event = display
            result = await super().__call__(receipt, scope, previous, control)
            stops.append(result.sdk_stop)
            return result

    executor = ObserveSdk(
        root=root / "native", runtime_path=runtime_path, api_url="https://api.offline.invalid",
        model_url=model_root, token_provider=lambda: "OFFLINE-model-token",
        model_transport=httpx.MockTransport(provider),
    )
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    values = response_submit(text="Read this turn context, then reply with the scripted final answer.").model_dump(mode="json")
    values["capture"]["template"]["settings"].update(
        model_provider=None, turn_origin_profile="fa-instruction-context-origin-v1")
    request = NATIVE_REQUEST.validate_json(json.dumps(values))
    journal = root / "journal"
    journal.mkdir()
    contexts = {}
    with TestClient(build_responses_host(runtime, journal, contexts)) as client:
        harness = ResponsesHarness(runtime, SimpleNamespace(controls={}), client, contexts)
        accepted = harness.invoke(request)
        assert accepted.status_code == 202, accepted.text
        original = harness.call(runtime.read, "rid-1")
        response = harness.respond(original, stream=True)
        assert response.status_code == 200
        events = sse_events(response)
        assert events[-1]["type"] == "response.completed", events[-1]
        completed = harness.wait("rid-1", "completed")
        assert len(requests) == 2 and len(stops) == 1
        # ADR-0005: the idle turn keeps its SDK; the checkpoint is saved from the live runtime's files.
        assert stops[0].runtime_version == "1.0.83" and stops[0].graceful
        assert stops[0].release == "fa-resident-worker-poc-v1"
        assert "tool.execution_start" in event_types and "assistant.message_delta" in event_types
        output = events[-1]["response"]["output"]
        assert [item["phase"] for item in output] == ["commentary", "final_answer"]
        commentary = output[0]["content"][0]["text"]
        # RTT B: the parent answer also streams into commentary as a display-only preview; the saved answer
        # still arrives whole, and only as final_answer.
        assert "read_context" in commentary and commentary.endswith(final_text)
        assert commentary.index("read_context") < commentary.index(final_text)
        assert output[-1]["content"][0]["text"] == final_text
        assert completed.usage.usage[0].requests == 2
        assert completed.usage.usage[0].input_tokens == 34
        assert display_failure == [True]
        assert events[-1]["response"].get("usage") is None
        assert "PRIVATE-display-error-detail" not in caplog.text and "PRIVATE-display-error-detail" not in response.text
        assert "FA_NATIVE_PROGRESS_DROPPED" in caplog.text
        (root / "proof.json").write_text(json.dumps({
            "runtime_version": stops[0].runtime_version, "sdk_stop": stops[0].model_dump(mode="json"),
            "model_requests": len(requests), "response_id": completed.response_id,
            "result_digest": completed.result.result_digest, "checkpoint_sha256": completed.result.checkpoint_sha256,
            "public_event_types": sorted(set(event_types)), "status": completed.status,
            "cloud_model_or_storage": False,
        }, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("model_provider", [None, "openai-responses"], ids=["completions", "responses"])
def test_an_attached_image_reaches_the_model_on_the_responses_lane(local_env, monkeypatch, runtime_path, model_provider):
    """B1 PA-08 on N6: upload, then a Responses-lane turn hands the same PNG bytes to the model."""
    from shared_context.behavior_user_images import NativeUserImageUpload
    from test_instruction_responses_native import response_bytes
    from test_user_images_native import encoded, reference

    supplied = os.environ.get("N6_NATIVE_WORKSPACE")
    if not supplied:
        pytest.fail("Set a unique session-private N6_NATIVE_WORKSPACE; never run against the real user HOME")
    root = Path(supplied).resolve() / ("images-" + (model_provider or "completions"))
    root.mkdir(parents=True, exist_ok=False)
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        (root / name.lower()).mkdir()
        monkeypatch.setenv(name, str(root / name.lower()))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH",
                 "FOUNDRY_HOSTING_ENVIRONMENT", "FA_NATIVE_TURNS", "FA_N0_STATE_PROBE",
                 "FA_N6_RESPONSES_PROBE", "FA_N6_RESPONSES_INTERRUPT_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.chdir(root)
    import _socket

    def loopback_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("The native Responses case permits only loopback connections")
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    model_root = "https://offline.services.ai.azure.com/api/projects/offline/openai/v1"
    sent = []

    def provider(request):
        payload = json.loads(request.content)
        sent.append(payload)
        assert len(sent) == 1, "one model request for one turn"
        if model_provider == "openai-responses":
            assert str(request.url) == model_root + "/responses"
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                  content=response_bytes(1, text="I see the chart."))
        assert str(request.url) == model_root + "/chat/completions"
        common = {"id": "offline-image", "object": "chat.completion.chunk", "created": 1, "model": "OFFLINE-model"}
        chunks = [
            {**common, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "I see the chart."},
                                    "finish_reason": None}]},
            {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**common, "choices": [], "usage": {"prompt_tokens": 17, "completion_tokens": 7, "total_tokens": 24}},
        ]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    executor = NativeSdkExecutor(root=root / "native", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                                 model_url=model_root, token_provider=lambda: "OFFLINE-model-token",
                                 model_transport=httpx.MockTransport(provider))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    values = response_submit(text="Describe the attached chart.").model_dump(mode="json")
    values["capture"]["template"]["settings"]["model_provider"] = model_provider
    values["capture"]["model"]["supports_image_input"] = True
    values["command"]["images"] = [reference().model_dump(mode="json")]
    request = NATIVE_REQUEST.validate_json(json.dumps(values))
    journal = root / "journal"
    journal.mkdir()
    contexts = {}
    with TestClient(build_responses_host(runtime, journal, contexts)) as client:
        harness = ResponsesHarness(runtime, SimpleNamespace(controls={}), client, contexts)
        uploaded = harness.invoke(NativeUserImageUpload(png_base64=encoded()))
        assert uploaded.status_code == 200 and uploaded.json()["sha256"] == reference().sha256
        accepted = harness.invoke(request)
        assert accepted.status_code == 202, accepted.text
        original = harness.call(runtime.read, "rid-1")
        assert original.state_profile == "foundry-responses-v1" and original.command.images == (reference(),)
        assert harness.respond(original, stream=True).status_code == 200
        harness.wait("rid-1", "completed")
    url = "data:image/png;base64," + encoded()
    text = json.dumps(sent[0], ensure_ascii=False)
    if model_provider == "openai-responses":
        parts = [part for item in sent[0]["input"] if item.get("role") == "user" and isinstance(item.get("content"), list)
                 for part in item["content"] if part.get("type") == "input_image"]
        assert [part["image_url"] for part in parts] == [url]
    else:
        parts = [part for message in sent[0]["messages"] if message["role"] == "user"
                 and isinstance(message["content"], list) for part in message["content"] if part.get("type") == "image_url"]
        assert [part["image_url"]["url"] for part in parts] == [url]
    assert "Describe the attached chart." in text


@pytest.mark.parametrize("model_provider", [None, "openai-responses"], ids=["completions", "responses"])
def test_gd_every_attached_text_file_reaches_the_model(local_env, monkeypatch, runtime_path, model_provider):
    """G-D (dev 419e): two files in one turn; the pinned runtime showed only the first `selection` attachment."""
    from shared_context.behavior_text_files import UserTextFile
    from test_instruction_responses_native import response_bytes

    supplied = os.environ.get("N6_NATIVE_WORKSPACE")
    if not supplied:
        pytest.fail("Set a unique session-private N6_NATIVE_WORKSPACE; never run against the real user HOME")
    root = Path(supplied).resolve() / ("files-" + (model_provider or "completions"))
    root.mkdir(parents=True, exist_ok=False)
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        (root / name.lower()).mkdir()
        monkeypatch.setenv(name, str(root / name.lower()))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH",
                 "FOUNDRY_HOSTING_ENVIRONMENT", "FA_NATIVE_TURNS", "FA_N0_STATE_PROBE",
                 "FA_N6_RESPONSES_PROBE", "FA_N6_RESPONSES_INTERRUPT_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.chdir(root)
    import _socket

    def loopback_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("The native Responses case permits only loopback connections")
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    model_root = "https://offline.services.ai.azure.com/api/projects/offline/openai/v1"
    sent = []

    def provider(request):
        sent.append(json.loads(request.content))
        assert len(sent) == 1, "one model request for one turn"
        if model_provider == "openai-responses":
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                  content=response_bytes(1, text="收到"))
        common = {"id": "offline-files", "object": "chat.completion.chunk", "created": 1, "model": "OFFLINE-model"}
        chunks = [
            {**common, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "收到"}, "finish_reason": None}]},
            {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**common, "choices": [], "usage": {"prompt_tokens": 17, "completion_tokens": 7, "total_tokens": 24}},
        ]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    executor = NativeSdkExecutor(root=root / "native", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                                 model_url=model_root, token_provider=lambda: "OFFLINE-model-token",
                                 model_transport=httpx.MockTransport(provider))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    files = (UserTextFile(name="a-e9b021.txt", text="倉庫 A\napple,12\n"),
             UserTextFile(name="b-e9b021.txt", text="倉庫 B\nplum,9\n"))
    values = response_submit(text="這是兩個倉庫的庫存，先回收到。").model_dump(mode="json")
    values["capture"]["template"]["settings"].update(model_provider=model_provider,
                                                     user_text_file_profile="fa-user-text-files-v1")
    values["command"]["files"] = [item.model_dump(mode="json") for item in files]
    request = NATIVE_REQUEST.validate_json(json.dumps(values))
    journal = root / "journal"
    journal.mkdir()
    contexts = {}
    with TestClient(build_responses_host(runtime, journal, contexts)) as client:
        harness = ResponsesHarness(runtime, SimpleNamespace(controls={}), client, contexts)
        accepted = harness.invoke(request)
        assert accepted.status_code == 202, accepted.text
        original = harness.call(runtime.read, "rid-1")
        assert original.command.files == files
        assert harness.respond(original, stream=True).status_code == 200
        harness.wait("rid-1", "completed")
    items = sent[0]["input"] if model_provider == "openai-responses" else sent[0]["messages"]
    user = json.dumps([item for item in items if item.get("role") == "user"], ensure_ascii=False)
    for item in files:
        assert item.name in user and item.text.split("\n")[1] in user and item.text.split("\n")[0] in user, user
    assert "input_file" not in user and "ide_selection" not in user, user


def _offline_responses(monkeypatch, name):
    supplied = os.environ.get("N6_NATIVE_WORKSPACE")
    if not supplied:
        pytest.fail("Set a unique session-private N6_NATIVE_WORKSPACE; never run against the real user HOME")
    root = Path(supplied).resolve() / name
    root.mkdir(parents=True, exist_ok=False)
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        (root / key.lower()).mkdir()
        monkeypatch.setenv(key, str(root / key.lower()))
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH",
                "FOUNDRY_HOSTING_ENVIRONMENT", "FA_NATIVE_TURNS", "FA_N0_STATE_PROBE",
                "FA_N6_RESPONSES_PROBE", "FA_N6_RESPONSES_INTERRUPT_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.chdir(root)
    import _socket

    def loopback_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("The native Responses case permits only loopback connections")
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    return root


MODEL_ROOT = "https://offline.services.ai.azure.com/api/projects/offline/openai/v1"
# The 2026-10-04 stage A answer for gpt-6-luna; the message is never logged, saved or shown.
UNSUPPORTED_EFFORT = {"error": {"code": "unsupported_parameter", "type": "invalid_request_error",
                                "param": "reasoning.effort", "message": "CANARY provider message"}}


def _model_request(rid, cid, text, model):
    values = response_submit(rid=rid, cid=cid, text=text).model_dump(mode="json")
    values["capture"]["model"].update(id=model, display_name=model)
    return NATIVE_REQUEST.validate_json(json.dumps(values))


def test_a_model_refusal_before_any_output_is_a_definite_failure_and_the_conversation_takes_another_model(
        local_env, monkeypatch, runtime_path):
    from shared_context.native_progress import read_progress
    from shared_context.native_progress_wire import NativeProgressRead
    from test_instruction_responses_native import response_bytes

    root = _offline_responses(monkeypatch, "model-refused")
    sent = []

    def provider(request):
        assert str(request.url) == MODEL_ROOT + "/responses"
        payload = json.loads(request.content)
        sent.append(payload)
        assert len(sent) <= 2, "one model request per turn"
        if payload["model"] == "OFFLINE-refusing-model":
            return httpx.Response(400, request=request, json=UNSUPPORTED_EFFORT)
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                              content=response_bytes(1, text="Answer from the other model."))

    executor = NativeSdkExecutor(root=root / "native", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                                 model_url=MODEL_ROOT, token_provider=lambda: "OFFLINE-model-token",
                                 model_transport=httpx.MockTransport(provider))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    journal = root / "journal"
    journal.mkdir()
    with TestClient(build_responses_host(runtime, journal, {})) as client:
        harness = ResponsesHarness(runtime, SimpleNamespace(controls={}), client, {})

        def turn(rid, cid, text, model, status):
            accepted = harness.invoke(_model_request(rid, cid, text, model))
            assert accepted.status_code == 202, accepted.text
            harness.respond(harness.call(runtime.read, rid), stream=True)
            return harness.wait(rid, status)

        refused = turn("rid-1", None, "First question for the refusing model.", "OFFLINE-refusing-model", "unknown")
        assert refused.failure_code == "NATIVE_MODEL_REFUSED" and refused.usage.usage[0].requests == 1
        progress = harness.call(read_progress, runtime, NativeProgressRead(
            conversation_id=refused.conversation_id, request_id="rid-1"))
        assert "Model provider refused: status=400 code=unsupported_parameter param=reasoning.effort" in [
            event.summary for event in progress.progress.events if event.kind == "error"]
        assert not any(event.kind == "tool_started" for event in progress.progress.events)
        assert "CANARY" not in progress.model_dump_json() and "OFFLINE-model-token" not in progress.model_dump_json()

        # Same conversation, new RID, another model: nothing was saved, so nothing of the refused model is resumed.
        answered = turn("rid-2", refused.conversation_id, "Second question for another model.", "OFFLINE-model",
                        "completed")
        assert answered.turn_number == 2 and answered.conversation_id == refused.conversation_id
        assert harness.call(runtime.conversation, refused.conversation_id).capture.model.id == "OFFLINE-model"
        # A completed turn saved SDK state: from now on the conversation keeps its model.
        changed = harness.invoke(_model_request("rid-3", refused.conversation_id, "Third.", "OFFLINE-refusing-model"))
        assert changed.status_code == 409 and changed.json()["error"]["code"] == "NATIVE_CAPTURE_CHANGED"
    assert [body["model"] for body in sent] == ["OFFLINE-refusing-model", "OFFLINE-model"]
    second = json.dumps(sent[1], ensure_ascii=False)
    assert "Second question for another model." in second and "First question" not in second


def test_a_refusal_after_the_model_already_called_a_tool_stays_unknown(local_env, monkeypatch, runtime_path):
    from shared_context.native_progress import read_progress
    from shared_context.native_progress_wire import NativeProgressRead
    from test_instruction_responses_native import response_bytes

    root = _offline_responses(monkeypatch, "refused-after-tool")
    sent = []

    def provider(request):
        sent.append(json.loads(request.content))
        assert len(sent) <= 2, "the runtime never resends after the refusal"
        if len(sent) == 1:
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                  content=response_bytes(1, tool="read_tool_output", arguments={"path": "missing.txt"}))
        return httpx.Response(400, request=request, json=UNSUPPORTED_EFFORT)

    executor = NativeSdkExecutor(root=root / "native", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                                 model_url=MODEL_ROOT, token_provider=lambda: "OFFLINE-model-token",
                                 model_transport=httpx.MockTransport(provider))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    journal = root / "journal"
    journal.mkdir()
    with TestClient(build_responses_host(runtime, journal, {})) as client:
        harness = ResponsesHarness(runtime, SimpleNamespace(controls={}), client, {})
        assert harness.invoke(_model_request("rid-1", None, "Read a file, then answer.", "OFFLINE-model")).status_code == 202
        harness.respond(harness.call(runtime.read, "rid-1"), stream=True)
        unknown = harness.wait("rid-1", "unknown")
        progress = harness.call(read_progress, runtime, NativeProgressRead(
            conversation_id=unknown.conversation_id, request_id="rid-1"))
    # A tool already ran on the model's output: the turn may have acted, so it is never a resendable refusal.
    assert unknown.failure_code != "NATIVE_MODEL_REFUSED", unknown.failure_code
    assert len(sent) == 2 and any(item.get("type") == "function_call_output" for item in sent[1]["input"])
    assert any(event.kind == "tool_started" for event in progress.progress.events)
