"""Assistant provider and model-boundary secret handling; no Azure or model calls."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from shared_context.errors import Problem


def test_upstream_error_records_status_without_response_body_or_credentials(caplog):
    from shared_context.behavior_assistant_gateway import AssistantProvider
    provider = AssistantProvider(
        "https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="SYNTHETIC_UPSTREAM_PRIVATE")),
        transport=httpx.MockTransport(lambda _: httpx.Response(503, text="SYNTHETIC_SECRET_CANARY")),
    )
    body = {"model": "gpt-4.1-mini", "max_tokens": 1024, "stream": True,
            "messages": [{"role": "user", "content": "Synthetic request"}]}
    with pytest.raises(Problem):
        asyncio.run(provider.send(body))
    assert "ASSISTANT_PROVIDER_HTTP_FAILURE status=503" in caplog.text
    assert "SYNTHETIC_SECRET_CANARY" not in caplog.text
    assert "SYNTHETIC_UPSTREAM_PRIVATE" not in caplog.text


@pytest.mark.parametrize("kind", ["http-error", "stream-error", "tool-success", "tool-error"])
def test_secret_boundary_refuses_unknown_output_before_any_general_channel(kind):
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    boundary = AssistantModelBoundary(None)
    if kind.startswith("tool"):
        value = '{"state":"saved","secret":"SYNTHETIC_SECRET_CANARY"}' if kind == "tool-success" else "SYNTHETIC_SECRET_CANARY"
        with pytest.raises((ValueError, KeyError)):
            boundary.tool_result("save_result", value)
        return
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"SYNTHETIC_'
            yield b'SECRET_CANARY"}}]}\n\n'
            raise RuntimeError("SYNTHETIC_SECRET_CANARY")
    response = (httpx.Response(500, content=b"SYNTHETIC_SECRET_CANARY") if kind == "http-error"
                else httpx.Response(200, stream=Stream(), headers={"content-type": "text/event-stream"}))
    with pytest.raises(RuntimeError) as error:
        asyncio.run(boundary.response(response))
    assert "SYNTHETIC_SECRET_CANARY" not in str(error.value)

