"""Free instructions keep raw input and SDK replies; legacy metrics bytes stay unchanged."""
import json

import pytest
from pathlib import Path
from assistant_api_helpers import assistant_api
from assistant_api_helpers import assistant_api
from test_m3_auth import jwk_client, signing_key
from instruction_flow_helpers import instruction_models
from hosted_versions_stub import deployed_versions


@pytest.fixture
def runtime_path():
    # Admission-only tests cannot launch a runtime. SDK I/O has its own boundary test.
    return Path(__file__)


def test_free_input_preserves_original_text_and_legacy_submit_shape():
    from shared_context.behavior_assistant_wire import InstructionSubmit, AssistantSubmit
    text = "  請寫一首關於雨的短詩。\n不要計算數字。  "
    command = InstructionSubmit(request_id="free-one", content=text)
    assert command.content == text
    assert json.loads(command.model_dump_json()) == {
        "request_id": "free-one", "content": text, "authorize_own_result": True,
    }
    assert AssistantSubmit(request_id="old").model_dump_json() == (
        '{"request_id":"old","content":"Use the reviewed metrics to compute remaining items and save my result.",'
        '"authorize_own_result":true}')
    with pytest.raises(ValueError):
        InstructionSubmit(request_id="blank", content=" \r\n\t ")


@pytest.mark.parametrize("answer", ["雨落窗沿，晚風把城市寫成一行詩。", "Use sorted(items, key=lambda x: x.name)."])
def test_free_provider_keeps_exact_sdk_stream_instead_of_metrics_only(answer):
    import asyncio
    import httpx
    from types import SimpleNamespace
    from shared_context.behavior_assistant_gateway import AssistantProvider
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    raw = ("data: " + json.dumps({"id": "original-provider-id", "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"content": answer, "refusal": None}, "finish_reason": None}]},
        ensure_ascii=False) + '\n\ndata: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n').encode()
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=raw, headers={"content-type":"text/event-stream","x-private":"not-forwarded"})
    provider = AssistantProvider("https://offline.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _:SimpleNamespace(token="OFFLINE")),transport=httpx.MockTransport(respond))
    async def run():
        response = await provider.send({"model":"gpt-4.1-mini","messages":[{"role":"user","content":"自由指令"}],
                                      "stream":True,"max_tokens":1024},text_reply=True,model="gpt-4.1-mini")
        assert response.content == raw and "x-private" not in response.headers
        sdk_response = await AssistantModelBoundary(None,text_reply=True).response(response)
        assert sdk_response.content == raw
    asyncio.run(run())
    assert len(requests) == 1


@pytest.mark.parametrize("free", [False, True])
@pytest.mark.parametrize("revoked", [False, True])
def test_production_provider_preserves_options_and_checks_current_work(monkeypatch, free, revoked):
    import asyncio
    from types import SimpleNamespace
    import httpx
    from shared_context.behavior_assistant_server import _CurrentAssistantProvider, AssistantServer
    from shared_context.errors import Problem

    calls, gates, legacy = [], [], []
    model = "configured-second-model" if free else "gpt-4.1-mini"
    raw = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"})
    def evidence(_):
        legacy.append(True)
        if free:
            raise AssertionError("instructions cannot inherit the old global expiry gate")
    monkeypatch.setattr(AssistantServer, "acquire_evidence", evidence)
    composition = SimpleNamespace(config=SimpleNamespace(assistant=SimpleNamespace(
        provider_endpoint="https://offline.invalid/openai/v1/chat/completions")),
        runtime=SimpleNamespace(work_policy=SimpleNamespace(models=SimpleNamespace(
            select=lambda _: SimpleNamespace(supports_reasoning_effort=None)))))
    provider = _CurrentAssistantProvider(composition, SimpleNamespace(
        get_token=lambda _: SimpleNamespace(token="OFFLINE")), transport=httpx.MockTransport(respond))
    async def current():
        gates.append(True)
        if revoked:
            raise Problem(403, "ASSISTANT_EXECUTION_FENCED", "original work revoked")
    async def run():
        body = {"model": model, "messages": [{"role": "user", "content": "OFFLINE"}],
                "stream": True, **({} if free else {"max_tokens": 1024})}
        if revoked:
            with pytest.raises(Problem) as error:
                await provider.send(body, before_send=current, text_reply=free, model=model if free else None)
            assert error.value.body["error"]["code"] == "ASSISTANT_EXECUTION_FENCED"
        else:
            result = await provider.send(body, before_send=current, text_reply=free, model=model if free else None)
            if free:
                assert result.content == raw
            else:
                chunks = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: {")]
                assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    asyncio.run(run())
    assert gates == [True] and legacy == ([] if free else [True])
    assert len(calls) == (0 if revoked else 1)
    if calls:
        assert calls[0]["model"] == model
        if free:
            assert calls[0]["stream_options"]["include_usage"] is True
            assert "max_tokens" not in calls[0]


@pytest.mark.parametrize("supported", [False, True, None])
def test_production_provider_uses_declared_reasoning_support_without_changing_reserved_body(monkeypatch, supported):
    import asyncio
    import copy
    from types import SimpleNamespace
    from shared_context.behavior_assistant_server import _CurrentAssistantProvider
    from shared_context.behavior_assistant_gateway import AssistantProvider
    sent = []
    async def capture(self, body, **options):
        await options["before_send"]()
        sent.append(body)
    monkeypatch.setattr(AssistantProvider, "send", capture)
    composition = SimpleNamespace(config=SimpleNamespace(assistant=SimpleNamespace(
        provider_endpoint="https://offline.invalid/openai/v1/chat/completions")),
        runtime=SimpleNamespace(work_policy=SimpleNamespace(models=SimpleNamespace(
            select=lambda _: SimpleNamespace(supports_reasoning_effort=supported)))))
    provider = _CurrentAssistantProvider(composition, object())
    body = {"model": "configured-model", "messages": [{"role": "user", "content": "exact original"}],
            "reasoning_effort": "medium", "stream": True}
    original = copy.deepcopy(body)
    async def current():
        pass
    asyncio.run(provider.send(body, before_send=current, text_reply=True, model="configured-model"))
    assert body == original
    assert sent == [{k: v for k, v in original.items() if supported is not False or k != "reasoning_effort"}]

