"""A deployment declared without reasoning effort never receives the runtime's default reasoning options."""

import asyncio
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture
from shared_context.m2_copilot import _ModelGateway
from shared_context.native_sdk import NativeSdkExecutor
from shared_context.native_turns import NativeTurnRuntime
from test_m2_copilot import runtime_path  # noqa: F401
from test_native_turn_wire import capture, submit
from test_native_turns import IDENTITY

COMPLETIONS = httpx.URL("https://context.invalid/model/v1/chat/completions")
RESPONSES = httpx.URL("https://context.invalid/model/v1/responses")


def completions_stream(model):
    common = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": model}
    events = [
        {**common, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
        {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {**common, "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
    ]
    return ("".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n").encode()


def gateway_send(endpoint, body, supports):
    sent = []

    async def reserve(_identity):
        return None

    async def token(_):
        return "OFFLINE"

    def provider(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                              content=completions_stream(body["model"]))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            gateway = _ModelGateway(endpoint, client, lambda _: None, model=body["model"], instruction=True,
                                    private_token=token, supports_reasoning_effort=supports,
                                    boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True))
            response = await gateway.send_request(httpx.Request("POST", endpoint, json=body),
                                                  SimpleNamespace(request_id="request", session_id="session"))
            await response.aread()
            await response.aclose()

    asyncio.run(run())
    assert len(sent) == 1
    return sent[0]


COMPLETIONS_BODY = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                    "stream_options": {"include_usage": True}, "temperature": 0, "reasoning_effort": "medium"}
RESPONSES_BODY = {"model": "gpt-4.1-mini", "input": [{"role": "user", "content": "hi"}], "stream": True,
                  "store": False, "reasoning": {"effort": "medium", "summary": "auto"},
                  "include": ["reasoning.encrypted_content"]}


def test_completions_without_declared_reasoning_effort_drops_only_that_field():
    sent = gateway_send(COMPLETIONS, COMPLETIONS_BODY, False)
    assert sent == {name: value for name, value in COMPLETIONS_BODY.items() if name != "reasoning_effort"}


def test_responses_without_declared_reasoning_effort_drops_reasoning_and_its_include():
    sent = gateway_send(RESPONSES, RESPONSES_BODY, False)
    assert sent == {name: value for name, value in RESPONSES_BODY.items() if name not in {"reasoning", "include"}}
    kept = gateway_send(RESPONSES, {**RESPONSES_BODY, "include": ["reasoning.encrypted_content", "other"]}, False)
    assert kept["include"] == ["other"] and "reasoning" not in kept


@pytest.mark.parametrize("supports", [None, True])
def test_a_deployment_not_declared_without_it_keeps_the_runtime_reasoning_options(supports):
    assert gateway_send(COMPLETIONS, COMPLETIONS_BODY, supports) == COMPLETIONS_BODY
    assert gateway_send(RESPONSES, RESPONSES_BODY, supports) == RESPONSES_BODY


def test_pinned_runtime_native_turns_never_send_reasoning_effort_to_a_model_declared_without_it(
        monkeypatch, runtime_path):
    """Runtime 1.0.83 adds reasoning_effort=medium for a model ID it knows (live 400 on gpt-4.1-mini, text-a11d739e)."""
    root_value = os.environ.get("N1_NATIVE_WORKSPACE")
    if not root_value:
        pytest.fail("Set a session-private N1_NATIVE_WORKSPACE; never use the real user HOME.")
    root = Path(root_value).resolve() / "reasoning-facts"
    root.mkdir(parents=True, exist_ok=False)
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        isolated = root / key.lower()
        isolated.mkdir()
        monkeypatch.setenv(key, str(isolated))
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH",
                "FOUNDRY_HOSTING_ENVIRONMENT", "FA_N0_STATE_PROBE", "FA_NATIVE_TURNS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")
    monkeypatch.setenv("AGENTSERVER_TASKS_BACKEND", "local")
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(root / "state"))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", IDENTITY.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", IDENTITY.agent_name)
    import _socket

    def deny_external(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("The native case may not open external network connections")
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", deny_external)
    sent = []
    url = "https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1"

    def provider(request):
        payload = json.loads(request.content)
        sent.append(payload)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=completions_stream(payload["model"]))

    async def token():
        return "OFFLINE-native-MI"

    values = capture().model_dump(mode="json")
    values["template"]["settings"]["model_provider"] = None
    values["model"].update(id="gpt-4.1-mini", supports_reasoning_effort=False)
    selected = NativeTurnCapture.model_validate_json(json.dumps(values))
    executor = NativeSdkExecutor(root=root / "run", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                                 model_url=url, token_provider=token, model_transport=httpx.MockTransport(provider))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime

    async def finished(rid):
        while True:
            receipt = await runtime.read(rid)
            if receipt.status in {"completed", "unknown", "busy_refused"}:
                return receipt
            await asyncio.sleep(0.02)

    headers = {"x-client-fa-state-profile": "foundry-native-v1", "x-agent-session-id": IDENTITY.session_id,
               "x-agent-invocation-id": "reasoning-facts", "x-agent-foundry-call-id": "reasoning-facts"}
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        first = NativeSubmit(command=submit(text="Reply ok.").command, capture=selected)
        assert client.post("/invocations", json=first.model_dump(mode="json"), headers=headers).status_code == 202
        receipt = client.portal.call(lambda: asyncio.wait_for(finished("rid-1"), 120))
        assert receipt.status == "completed"
        # The resumed session must not bring the runtime default back either.
        second = NativeSubmit(command=submit(rid="rid-2", cid=receipt.conversation_id, text="Again.").command,
                              capture=selected)
        assert client.post("/invocations", json=second.model_dump(mode="json"), headers=headers).status_code == 202
        assert client.portal.call(lambda: asyncio.wait_for(finished("rid-2"), 120)).status == "completed"
    assert len(sent) == 2 and all(payload["model"] == "gpt-4.1-mini" for payload in sent)
    assert all("reasoning_effort" not in payload for payload in sent)
