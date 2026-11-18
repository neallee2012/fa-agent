"""Latency regressions use gated transports, not sleeps or paid models."""
import asyncio
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_assistant_gateway import AssistantProvider
from test_web_test_console_frozen_interface import real_interface
from web_test_console_frozen_interface_helpers import post
from test_m3_auth import signing_key, jwk_client
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import runtime_path, instruction_models
from legacy_console_rows_helpers import stored_console


def test_state_only_builds_message_views_for_the_selected_conversation(real_interface,monkeypatch):
    c=real_interface
    # Two old conversations, each with one work the retired submit admitted.
    facts=stored_console(c,"console-two-threads")
    first,second=facts["first"],facts["second"]
    console=c.browser.app.state.console
    viewed=[]
    original=console.task_view
    def view(task):
        viewed.append(task.task_id)
        return original(task)
    monkeypatch.setattr(console,"task_view",view)
    response=c.browser.ui.get("/api/state",params={"thread_id":second["thread_id"]})
    assert response.status_code==200
    assert first["request_id"] not in viewed and second["request_id"] in viewed
    assert {t["thread_id"] for t in response.json()["composer"]["threads"]}>={first["thread_id"],second["thread_id"]}
    old=c.browser.ui.get("/api/state",params={"thread_id":first["thread_id"]})
    assert any(t["task_id"]==first["request_id"] for t in old.json()["tasks"])


def test_provider_first_chunk_does_not_wait_for_the_last_chunk():
    async def run():
        tail = asyncio.Event()
        closed = []
        first = b'data: {"choices":[{"delta":{"content":"first"},"index":0}]}\n\n'
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield first
                await tail.wait()
                yield b"data: [DONE]\n\n"
            async def aclose(self):
                closed.append(True)
        def send(request):
            return httpx.Response(200, stream=Stream(), headers={"content-type": "text/event-stream"}, request=request)
        provider = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
            SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE")),
            transport=httpx.MockTransport(send))
        body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "OFFLINE"}]}
        response = await asyncio.wait_for(
            provider.send(body, text_reply=True, model="gpt-4.1-mini", stream_response=True), 1)
        from shared_context.behavior_assistant_boundary import AssistantModelBoundary
        boundary=AssistantModelBoundary(None,text_reply=True,streaming=True)
        response=await boundary.response(response)
        assert not closed, "the boundary must not close a stream before the SDK consumes it"
        iterator = response.aiter_bytes()
        assert await asyncio.wait_for(anext(iterator), 1) == first
        assert not tail.is_set()
        tail.set()
        assert await anext(iterator) == b"data: [DONE]\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        await response.aclose()
        assert closed
    asyncio.run(run())


def test_preview_is_bounded_transient_and_never_contains_reasoning_or_tools():
    from shared_context.behavior_instruction_stream import LiveReplies,AnswerEvents
    live=LiveReplies()
    live.begin("job","request")
    parser=AnswerEvents(lambda text:live.append("job","request",text))
    parser.feed(b'data: {"choices":[{"delta":{"reasoning_content":"PRIVATE","tool_calls":[{"x":"PRIVATE"}]}}]}\n\n')
    assert live.read("job") is None
    parser.feed(b'data: {"choices":[{"delta":{"content":"hel')
    assert live.read("job") is None
    parser.feed(b'lo"}}]}\n\n')
    assert live.read("job")["text"]=="hello"
    live.begin("job","next-request")
    live.append("job","request","late old content")
    assert live.read("job") is None

