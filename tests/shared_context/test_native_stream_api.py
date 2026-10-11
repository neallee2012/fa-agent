"""RTT B read-only preview relay: T1 + original receipt, whitelisted official stream fields, no new authority."""

import json

import httpx
import pytest

from shared_context import behavior_native_stream_api as relay_api
from test_native_responses_api import PREFIX, responses_api
from test_agent_versions import Foundry, REAL_CURRENT
from test_native_turn_api import native_api
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token


def submitted(env, rid="stream-rid", *, started=True):
    first = env.case.client.post(PREFIX + "/submit", headers=env.case.headers, json={"request_id": rid, "content": "原始"})
    assert first.status_code == 202, first.text
    original = env.case.remote.records[rid]
    if started:
        original = original.model_copy(update={"status": "started", "response_id": "resp-original", "dispatch": None})
        env.case.remote.records[rid] = original
    return original


def path(original, conversation_id=None):
    return (PREFIX + "/conversations/" + (conversation_id or original.conversation_id)
            + "/requests/" + original.request_id + "/stream")


def frames(text):
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]


@pytest.fixture
def platform(monkeypatch):
    seen, state = [], {"status": 200, "events": []}

    def server(request):
        seen.append(request)
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"error": {"code": "invalid_request_error"}})
        body = b": keep-alive\n\n" + b"".join(
            ("data: " + json.dumps(event) + "\n\n").encode() for event in state["events"])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    monkeypatch.setattr(relay_api, "_transport", httpx.MockTransport(server))
    return seen, state


def test_a_turn_that_has_not_started_asks_the_caller_to_retry_without_any_platform_read(responses_api, platform):
    env, (seen, _) = responses_api, platform
    original = submitted(env, started=False)
    result = env.case.client.get(path(original), headers=env.case.headers)
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "NATIVE_STREAM_NOT_STARTED"
    assert result.json()["error"]["automatic_retry"] is True and seen == []


def test_only_whitelisted_fields_of_the_original_response_stream_reach_the_caller(responses_api, platform):
    env, (seen, state) = responses_api, platform
    original = submitted(env)
    state["events"] = [
        {"type": "response.created", "sequence_number": 0, "response": {"id": "resp-original", "metadata": "PRIVATE"}},
        {"type": "response.in_progress", "sequence_number": 1, "response": {"output": [{"text": "PRIVATE-old"}]}},
        {"type": "response.output_item.added", "sequence_number": 2,
         "item": {"type": "message", "id": "msg_c", "phase": "commentary", "content": [{"text": "PRIVATE"}]}},
        {"type": "response.output_item.added", "sequence_number": 3, "item": {"type": "reasoning", "id": "rs_1"}},
        {"type": "response.reasoning_text.delta", "sequence_number": 4, "item_id": "rs_1", "delta": "PRIVATE-reason"},
        {"type": "response.output_text.delta", "sequence_number": 5, "item_id": "msg_c", "delta": "預覽"},
        {"type": "response.content_part.done", "sequence_number": 6, "part": {"text": "PRIVATE-part"}},
        {"type": "response.completed", "sequence_number": 7, "response": {
            "status": "completed", "output": [{"content": [{"text": "PRIVATE-final"}]}], "usage": {"x": 1}}},
        {"type": "response.output_text.delta", "sequence_number": 8, "item_id": "msg_c", "delta": "PRIVATE-after"},
    ]
    result = env.case.client.get(path(original) + "?starting_after=0", headers=env.case.headers)
    assert result.status_code == 200 and result.headers["content-type"].startswith("text/event-stream"), result.text
    assert "PRIVATE" not in result.text
    assert frames(result.text) == [
        {"type": "response.created", "sequence_number": 0},
        {"type": "response.in_progress", "sequence_number": 1},
        {"type": "response.output_item.added", "sequence_number": 2, "item_id": "msg_c", "phase": "commentary"},
        {"type": "response.output_text.delta", "sequence_number": 5, "item_id": "msg_c", "delta": "預覽"},
        {"type": "response.completed", "sequence_number": 7, "status": "completed"},
    ]
    assert [line for line in result.text.splitlines() if line.startswith("id:")] == [
        "id: 0", "id: 1", "id: 2", "id: 5", "id: 7"]
    assert len(seen) == 1 and seen[0].method == "GET"
    assert seen[0].url.path.endswith("/endpoint/protocols/openai/responses/resp-original")
    assert dict(seen[0].url.params) == {"api-version": "v1", "stream": "true", "starting_after": "0"}
    assert not any(call.url.path.endswith("/responses") and call.method == "POST"
                   for call in env.calls if call.url.path.endswith("resp-original"))


@pytest.mark.parametrize("status,code", [(400, 409), (404, 409), (502, 503)])
def test_a_response_without_a_replayable_stream_keeps_the_caller_on_status_reads(responses_api, platform, status, code):
    env, (seen, state) = responses_api, platform
    original = submitted(env)
    state["status"] = status
    result = env.case.client.get(path(original), headers=env.case.headers)
    assert result.status_code == code and result.json()["error"]["code"] == "NATIVE_STREAM_UNAVAILABLE"
    assert result.json()["error"]["automatic_retry"] is False and len(seen) == 1


@pytest.mark.parametrize("query", ["?starting_after=-1", "?starting_after=x", "?other=1"])
def test_only_one_non_negative_cursor_is_accepted(responses_api, platform, query):
    env, (seen, _) = responses_api, platform
    original = submitted(env)
    result = env.case.client.get(path(original) + query, headers=env.case.headers)
    assert result.status_code == 422, result.text
    assert seen == []


def test_a_malformed_request_id_is_an_invalid_request_not_a_dependency_failure(responses_api, platform):
    env, (seen, _) = responses_api, platform
    original = submitted(env)
    result = env.case.client.get(PREFIX + "/conversations/" + original.conversation_id + "/requests/bad.id/stream",
                                 headers=env.case.headers)
    assert result.status_code == 422, result.text
    assert seen == []


def test_the_relay_keeps_an_idle_stream_alive_and_ends_on_its_terminal_event(monkeypatch):
    import asyncio

    monkeypatch.setattr(relay_api, "KEEP_ALIVE_SECONDS", 0.05)

    async def slow():
        await asyncio.sleep(0.2)
        yield b'data: {"type":"response.in_progress","sequence_number":1}\n\n'
        yield b'data: {"type":"response.completed","sequence_number":2,"response":{"status":"completed"}}\n\n'
        yield b'data: {"type":"response.output_text.delta","sequence_number":3,"item_id":"m","delta":"late"}\n\n'

    async def collect():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=slow())))
        response = await client.send(client.build_request("GET", "https://offline.invalid/s"), stream=True)
        return [frame async for frame in relay_api.relay(client, response)]

    frames_out = asyncio.run(collect())
    assert frames_out[0] == ": keep-alive\n\n"
    data = [json.loads(frame.split("data: ", 1)[1]) for frame in frames_out if "data: " in frame]
    assert [value["type"] for value in data] == ["response.in_progress", "response.completed"]


def test_a_reply_sized_delta_is_left_for_the_saved_answer():
    big = {"type": "response.output_text.delta", "sequence_number": 9, "item_id": "msg_final",
           "delta": "x" * (relay_api.MAX_DELTA_CHARS + 1)}
    assert relay_api.forwarded(big) is None
    assert relay_api.forwarded({**big, "delta": "x"}) == {**big, "delta": "x"}


def test_another_conversation_cannot_replay_this_turn(responses_api, platform):
    env, (seen, _) = responses_api, platform
    original = submitted(env)
    result = env.case.client.get(path(original, "conv-another"), headers=env.case.headers)
    assert result.status_code in {403, 409} and seen == [], result.text


@pytest.mark.parametrize("value", [
    {"type": "response.output_text.delta", "sequence_number": True, "item_id": "m", "delta": "x"},
    {"type": "response.output_text.delta", "sequence_number": -1, "item_id": "m", "delta": "x"},
    {"type": "response.output_text.delta", "sequence_number": 1, "item_id": "bad id", "delta": "x"},
    {"type": "response.output_item.added", "sequence_number": 1, "item": {"type": "message", "id": "m", "phase": "x"}},
    {"type": "response.failed", "sequence_number": 1, "response": {"error": {"code": "PRIVATE detail"}}},
    {"type": "error", "sequence_number": 1, "message": "PRIVATE"},
    ["not", "an", "event"],
])
def test_whitelist_drops_anything_it_cannot_prove_safe(value):
    result = relay_api.forwarded(value)
    assert result is None or (result == {"type": "response.failed", "sequence_number": 1})


class _Upstream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, forever=None):
        self.chunks, self.forever, self.closed = chunks, forever, False

    async def __aiter__(self):
        import asyncio
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk
        while self.forever is not None:
            await asyncio.sleep(self.forever[0])
            yield self.forever[1]

    async def aclose(self):
        self.closed = True


def _run_relay(upstream, *, pause=0.0):
    import asyncio

    async def collect():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=upstream)))
        response = await client.send(client.build_request("GET", "https://offline.invalid/s"), stream=True)
        out = []
        async for frame in relay_api.relay(client, response):
            out.append(frame)
            await asyncio.sleep(pause)
        return out

    return asyncio.run(collect())


def _sse(value):
    return ("event: " + value["type"] + "\ndata: " + json.dumps(value) + "\n\n").encode()


def test_final_answer_ends_the_relay_before_the_saved_reply_copies_are_read():
    upstream = _Upstream([
        _sse({"type": "response.output_item.added", "sequence_number": 1,
              "item": {"type": "message", "id": "msg_c", "phase": "commentary"}}),
        _sse({"type": "response.output_text.delta", "sequence_number": 2, "item_id": "msg_c", "delta": "預覽"}),
        _sse({"type": "response.output_item.added", "sequence_number": 3,
              "item": {"type": "message", "id": "msg_f", "phase": "final_answer"}}),
        _sse({"type": "response.output_text.delta", "sequence_number": 4, "item_id": "msg_f", "delta": "PRIVATE"}),
    ], forever=(0.01, b": keep-alive\n\n"))
    out = _run_relay(upstream)
    data = [json.loads(frame.split("data: ", 1)[1]) for frame in out if "data: " in frame]
    assert [value["sequence_number"] for value in data] == [1, 2, 3] and data[-1]["phase"] == "final_answer"
    assert upstream.closed and "PRIVATE" not in "".join(out)


def test_upstream_keep_alives_still_reach_the_caller_and_a_slow_reader_never_leaks_the_upstream(monkeypatch):
    monkeypatch.setattr(relay_api, "KEEP_ALIVE_SECONDS", 0.05)
    monkeypatch.setattr(relay_api, "STREAM_SECONDS", 0.4)
    upstream = _Upstream([], forever=(0.0, b": keep-alive\n\n" * 64))
    out = _run_relay(upstream, pause=0.1)
    assert out and set(out) == {": keep-alive\n\n"}
    assert upstream.closed


def test_frames_skip_an_oversized_event_and_split_across_chunks():
    frames = relay_api._Frames(64)
    assert list(frames.feed(b"data: " + b"x" * 100)) == []
    assert list(frames.feed(b"y" * 100 + b"\n")) == []
    assert list(frames.feed(b"\nevent: a\ndata: {\"n\":1}\n")) == []
    assert list(frames.feed(b"\n: comment\n\ndata: 2\n\n")) == [b'{"n":1}', b"2"]
