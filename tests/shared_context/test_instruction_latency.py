"""Latency regressions use gated transports, not sleeps or paid models."""
import asyncio
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_assistant_gateway import AssistantProvider
from test_web_test_console_frozen_interface import real_interface, post
from test_m3_auth import signing_key, jwk_client
from test_behavior_assistant import assistant
from test_instruction_flow import runtime_path, instruction_models


def test_chat_submission_needs_only_one_backend_post_and_keeps_a_durable_intent(real_interface):
    c = real_interface
    draft = c.browser.ui.get("/api/state").json()["composer"]
    c.calls.clear()
    response = post(c, f"/api/instructions/{draft['request_id']}/submit",
                    {"content": "One original message.", "model": draft["model"]})
    assert response.status_code == 200, response.text
    # A new conversation reads its server-selected template once (fae25c09), then writes exactly one submit.
    expected = [("GET", "/work-policy/v1/agent-template"), ("POST", "/work-policy/v1/submit")]
    assert [(call["method"], call["path"]) for call in c.calls] == expected
    saved = c.browser.app.state.console.journal.get(draft["request_id"], "submit")
    assert saved["state"] == "CONFIRMED"
    assert saved["intent"]["instruction_context"]["conversation_id"] == saved["admission"]["conversation_id"]
    assert saved["intent"]["body"]["content"] == "One original message."
    assert post(c, f"/api/instructions/{draft['request_id']}/submit",
                {"content": "One original message.", "model": draft["model"]}).status_code == 200
    assert [(call["method"], call["path"]) for call in c.calls] == expected


def test_state_only_builds_message_views_for_the_selected_conversation(real_interface,monkeypatch):
    c=real_interface
    first=c.browser.ui.get("/api/state").json()["composer"]
    assert post(c,f"/api/instructions/{first['request_id']}/submit",
                {"content":"First thread","model":first["model"]}).status_code==200
    second=post(c,"/api/instructions/conversations/new").json()["composer"]
    assert post(c,f"/api/instructions/{second['request_id']}/submit",
                {"content":"Second thread","model":second["model"]}).status_code==200
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


def test_real_http_stream_yields_preview_before_done_and_never_replays(assistant):
    from fastapi.testclient import TestClient
    from fastapi import APIRouter
    from shared_context.behavior_assistant_api import install_assistant_routes, TICKET_HEADER
    from shared_context.behavior_api import create_behavior_app
    from shared_context.behavior_assistant_wire import InstructionSubmit, Reservation, ModelRequest
    from shared_context.behavior_wire import Command
    from shared_context.behavior_work_policy import CostLedger
    from shared_context.behavior_operations import _row
    from shared_context.source_materials import digest
    from test_work_policy_lifecycle import enabled
    from test_behavior_model_deadlines import asgi_server
    h, control, human, _ = enabled(assistant)
    released, closed = Event(), Event()
    calls=[]
    first=b'data: {"choices":[{"index":0,"delta":{"content":"Visible","reasoning_content":"PRIVATE"}}]}\n\n'
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first
            assert await asyncio.to_thread(released.wait,5)
            yield (b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                   b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n'
                   b'data: [DONE]\n\n')
        async def aclose(self):
            closed.set()
    def send(request):
        calls.append(request)
        return httpx.Response(200,request=request,headers={"content-type":"text/event-stream"},stream=Body())
    provider=AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _:SimpleNamespace(token="OFFLINE")),transport=httpx.MockTransport(send))
    app=create_behavior_app(h.runtime,h.verifier)
    routes=APIRouter()
    install_assistant_routes(routes,h.assistant,h.verifier,provider=provider)
    app.include_router(routes)
    from shared_context.behavior_work_policy_api import install_work_policy_routes
    install_work_policy_routes(app,SimpleNamespace(runtime=h.runtime,verifier=h.verifier))
    binding,_=control.submit(human,InstructionSubmit(request_id="gated-stream",content="OFFLINE",model="gpt-4.1-mini"))
    assigned=[]
    h.workers.adapter=replace(h.workers.adapter,start=lambda start:assigned.append(h.assistant.issue(start)))
    h.workers.start(binding.job_id,binding.owner)
    assignment,ticket=assigned[0]
    path=f"/behavior/v1/bindings/{binding.binding_id}/assistant/sessions/{assignment.session_id}/"
    headers={**h.headers("agent"),TICKET_HEADER:ticket.get_secret_value()}
    body={"model":"gpt-4.1-mini","stream":True,"messages":[{"role":"user","content":"OFFLINE"}]}
    command=ModelRequest(request_id=digest(body),body=body)
    with TestClient(app) as setup:
        assert setup.post(path+"acquire",content=Command(request_id="sdk-start").model_dump_json(),
                          headers=headers).status_code==200
        assert setup.post(path+"reserve-model",content=Reservation(request_id=command.request_id,
            request_sha256=command.request_id).model_dump_json(),headers=headers).status_code==200
        try:
            with asgi_server(app) as port,httpx.Client(base_url=f"http://127.0.0.1:{port}",trust_env=False,timeout=5) as client:
                with client.stream("POST",path+"model",content=command.model_dump_json(),headers=headers) as response:
                    assert response.status_code==200
                    iterator=response.iter_bytes()
                    assert next(iterator)==first
                    assert not released.is_set()
                    preview=app.state.instruction_live_replies.read(binding.job_id)
                    assert preview["text"]=="Visible" and not preview["durable"] and not preview["complete"]
                    cid=control.records[binding.binding_id].profile.instruction.conversation_id
                    status=client.get("/work-policy/v1/conversations/"+cid,headers=h.headers())
                    assert status.status_code==200,status.text
                    assert status.json()["turns"][-1]["stream"]["text"]=="Visible"
                    assert not status.json()["turns"][-1]["reply_available"]
                    assert "PRIVATE" not in status.text
                    unsaved=client.get(f"/work-policy/v1/conversations/{cid}/turns/1/reply",
                                       headers=h.headers())
                    assert unsaved.status_code==404
                    forbidden=client.get("/work-policy/v1/conversations/"+cid,headers=h.headers("agent"))
                    assert forbidden.status_code==403
                    released.set()
                    assert b"[DONE]" in b"".join(iterator)
                repeated=client.post(path+"model",content=command.model_dump_json(),headers=headers)
                assert repeated.status_code==409 and len(calls)==1
        finally:
            released.set()
    ledger=_row(h.store.read_snapshot((control.cost_key,)),control.cost_key,CostLedger)
    assert ledger.model_pending_microusd==0 and ledger.model_metered_microusd>0 and closed.is_set()


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


def test_stream_keeps_absolute_deadline_and_closes_on_partial_failure():
    from shared_context.behavior_assistant_api import _ModelStream
    from shared_context.behavior_assistant_deadlines import ModelCall,_server_call
    async def run():
        closed=[]
        sent=[]
        async def body():
            try:
                yield b"partial"
                await asyncio.Event().wait()
            finally:
                closed.append(True)
        call=ModelCall(0.05,"server_model_deadline")
        response=_ModelStream(body(),call)
        async def receive():
            await asyncio.Event().wait()
        async def send(packet):
            sent.append(packet)
        with pytest.raises(TimeoutError):
            await response({"type":"http","asgi":{"spec_version":"2.4"}},receive,send)
        assert closed and call.cancelled.is_set() and _server_call.get() is None
        assert any(packet.get("body")==b"partial" for packet in sent)
        assert not any(packet.get("type")=="http.response.body" and packet.get("more_body") is False for packet in sent)
    asyncio.run(run())
