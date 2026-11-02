"""Official nullable OpenAI delta fields; offline wire compatibility, not model evidence."""
import asyncio
import json

import httpx
import pytest

from shared_context.behavior_assistant_boundary import AssistantModelBoundary


def stream(deltas, *, finish_reason="tool_calls", trailing_chunks=(), include_done=True):
    chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas]
    if finish_reason is not None:
        chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]})
    chunks.extend(trailing_chunks)
    body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
    if include_done:
        body += "data: [DONE]\n\n"
    return httpx.Response(
        200, content=body.encode(), headers={"content-type": "text/event-stream"},
        request=httpx.Request("POST", "https://synthetic.invalid/chat/completions"),
    )


def test_nullable_stream_fields_and_fragment_names_preserve_exact_allowed_tool_call():
    # ChoiceDelta and ChoiceDeltaToolCallFunction in openai-python declare these nullable.
    deltas = [
        {"role": "assistant", "content": "", "refusal": None, "function_call": None, "tool_calls": None},
        {"role": None, "content": None, "refusal": None, "function_call": None, "tool_calls": [{
            "index": 0, "id": "untrusted-provider-id", "type": "function",
            "function": {"name": "read_context", "arguments": ""},
        }]},
        {"tool_calls": [{"index": 0, "id": None, "type": None,
                         "function": {"name": None, "arguments": "{}"}}]},
        {"role": None, "content": None, "refusal": None, "function_call": None, "tool_calls": None},
    ]
    result = asyncio.run(AssistantModelBoundary(None).response(stream(deltas)))
    assert result.status_code == 200
    values = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: {")]
    tool = values[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tool["function"] == {"name": "read_context", "arguments": "{}"}
    assert tool["id"] == "fa_call_1"
    assert "untrusted-provider-id" not in result.text
    assert "refusal" not in result.text and "function_call" not in result.text


@pytest.mark.parametrize("finish_reason", [None, "length", "content_filter", "function_call", "unknown"])
@pytest.mark.parametrize("has_tool", [False, True], ids=["silent", "tool"])
def test_incomplete_or_filtered_stream_never_becomes_a_successful_sdk_response(finish_reason, has_tool):
    delta = {"tool_calls": [{
        "index": 0, "function": {"name": "read_context", "arguments": "{}"},
    }]} if has_tool else {}
    with pytest.raises(RuntimeError, match="refused before SDK ingress"):
        asyncio.run(AssistantModelBoundary(None).response(stream([delta], finish_reason=finish_reason)))


@pytest.mark.parametrize("has_tool,finish_reason", [(True, "stop"), (False, "tool_calls")])
def test_finish_reason_must_match_the_output_kind(has_tool, finish_reason):
    delta = {"tool_calls": [{
        "index": 0, "function": {"name": "read_context", "arguments": "{}"},
    }]} if has_tool else {}
    with pytest.raises(RuntimeError, match="refused before SDK ingress"):
        asyncio.run(AssistantModelBoundary(None).response(stream([delta], finish_reason=finish_reason)))


def test_explicit_silent_stop_with_usage_chunk_is_preserved():
    response = stream([{}], finish_reason="stop", trailing_chunks=(
        {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 0}},
    ))
    result = asyncio.run(AssistantModelBoundary(None).response(response))
    values = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: {")]
    assert values[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert values[1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize("chunk", [
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [{
        "index": 0, "function": {"name": "read_context", "arguments": "{}"},
    }]}, "finish_reason": None}]},
])
def test_stream_cannot_continue_or_finish_again_after_terminal_choice(chunk):
    with pytest.raises(RuntimeError, match="refused before SDK ingress"):
        asyncio.run(AssistantModelBoundary(None).response(
            stream([{}], finish_reason="stop", trailing_chunks=(chunk,)),
        ))


def test_terminal_choice_without_done_is_not_complete():
    with pytest.raises(RuntimeError, match="refused before SDK ingress"):
        asyncio.run(AssistantModelBoundary(None).response(stream([{}], finish_reason="stop", include_done=False)))


def test_tool_delta_and_terminal_reason_may_share_one_chunk():
    result = asyncio.run(AssistantModelBoundary(None).response(stream([], finish_reason=None, trailing_chunks=(
        {"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "function": {"name": "read_context", "arguments": "{}"},
        }]}, "finish_reason": "tool_calls"}]},
    ))))
    assert '"finish_reason": "tool_calls"' in result.text


def test_duplicate_done_is_rejected():
    response = stream([{}], finish_reason="stop")
    response = httpx.Response(
        200, content=response.content + b"data: [DONE]\n\n",
        headers=response.headers, request=response.request,
    )
    with pytest.raises(RuntimeError, match="refused before SDK ingress"):
        asyncio.run(AssistantModelBoundary(None).response(response))


@pytest.mark.parametrize("delta", [
    {"refusal": "SYNTHETIC_SECRET_CANARY"},
    {"function_call": {"name": "shell", "arguments": "SYNTHETIC_SECRET_CANARY"}},
    {"role": "system"},
    {"content": "SYNTHETIC_SECRET_CANARY"},
    {"unregistered": None},
    {"tool_calls": {"index": 0}},
    {"tool_calls": [{"index": False, "function": {"name": "read_context", "arguments": "{}"}}]},
    {"tool_calls": [{"index": 0, "type": "custom", "function": {"name": "read_context", "arguments": "{}"}}]},
    {"tool_calls": [{"index": 0, "function": {"name": 1, "arguments": "{}"}}]},
    {"tool_calls": [{"index": 0, "function": {"name": "read_context", "arguments": []}}]},
])
def test_nullable_support_does_not_admit_text_unknown_fields_or_other_tool_types(delta):
    with pytest.raises(RuntimeError) as denied:
        asyncio.run(AssistantModelBoundary(None).response(stream([delta])))
    assert "SYNTHETIC_SECRET_CANARY" not in str(denied.value)


def raw(body, *, status=200, content_type="text/event-stream"):
    return httpx.Response(status, content=body, headers={"content-type": content_type},
                          request=httpx.Request("POST", "https://synthetic.invalid/chat/completions"))


def frames(*chunks, done=True):
    body = "".join("data: " + (chunk if isinstance(chunk, str) else json.dumps(chunk)) + "\n\n" for chunk in chunks)
    return raw((body + ("data: [DONE]\n\n" if done else "")).encode())


def delta_chunk(delta, finish_reason=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def call(part):
    return delta_chunk({"tool_calls": [part]})


def function(name="read_context", arguments="{}"):
    return {"index": 0, "function": {"name": name, "arguments": arguments}}


STOP = delta_chunk({}, "stop")


def refused(response, boundary=None):
    boundary = boundary or AssistantModelBoundary(None)
    with pytest.raises(RuntimeError, match="refused before SDK ingress"):
        asyncio.run(boundary.response(response))
    return boundary.failure["boundary_phase"]


def chunks(result):
    return [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: {")]


def test_status_and_headers_are_their_own_phases():
    assert refused(raw(b"data: [DONE]\n\n", status=500)) == "response_status"
    assert refused(raw(b"data: [DONE]\n\n", status=201)) == "response_status"
    assert refused(raw(b"{}", content_type="application/json")) == "response_headers"
    assert refused(raw(b"data: [DONE]\n\n", content_type="text/plain; charset=utf-8")) == "response_headers"
    assert chunks(asyncio.run(AssistantModelBoundary(None).response(httpx.Response(
        200, content=frames(STOP).content, headers={"content-type": "text/event-stream; charset=utf-8"},
        request=httpx.Request("POST", "https://synthetic.invalid/chat/completions")))))


def test_body_limits_differ_by_profile():
    padded = frames(STOP).content
    padded += b"\n" * (65536 - len(padded))
    assert asyncio.run(AssistantModelBoundary(None).response(raw(padded))).status_code == 200
    assert refused(raw(padded + b"\n")) == "response_body"
    limit = 4 * 1024 * 1024
    text = AssistantModelBoundary(None, text_reply=True)
    assert asyncio.run(text.response(raw(b"x" * limit))).content == b"x" * limit
    assert refused(raw(b"x" * (limit + 1)), AssistantModelBoundary(None, text_reply=True)) == "response_body"


def test_free_instruction_bytes_pass_unchanged_to_the_sdk_parser():
    body = b'data: {"choices":[{"delta":{"content":"kept \xe4\xb8\xad"}}]}\n\nnot-a-frame\xff\n'
    result = asyncio.run(AssistantModelBoundary(None, text_reply=True).response(raw(body)))
    assert result.content == body and result.headers["content-type"] == "text/event-stream"
    assert result.status_code == 200 and str(result.request.url).endswith("/chat/completions")


def test_undecodable_legacy_body_stops_at_decode():
    assert refused(raw(b"data: \xff\n\n")) == "response_decode"


TOOL_END = delta_chunk({}, "tool_calls")


def silent(*chunks):
    """Well terminated, so a frame rule that stopped refusing would let the stream pass."""
    return frames(*chunks, STOP)


def tooled(*chunks):
    return frames(*chunks, TOOL_END)


FRAME_REFUSALS = [
    pytest.param(raw(b"data: [DONE]\n\n"), id="done-without-finish"),
    pytest.param(raw(b"event: x\n\n" + silent().content), id="not-a-data-line"),
    pytest.param(raw(b'data:{"choices":[]}\n\n' + silent().content), id="data-without-space"),
    pytest.param(raw(silent().content + b'data: {"choices":[]}\n\n'), id="frame-after-done"),
    pytest.param(raw(silent().content + b"data: [DONE]\n\n"), id="second-done"),
    pytest.param(silent('{"choices":[],"choices":[]}'), id="duplicate-json-key"),
    pytest.param(silent("{}"), id="choices-missing"),
    pytest.param(silent({"choices": {}}), id="choices-not-a-list"),
    pytest.param(silent({"choices": [{}, {}]}), id="two-choices"),
    pytest.param(silent(STOP), id="choice-after-finish"),
    pytest.param(frames(delta_chunk({}, "length")), id="unknown-finish"),
    pytest.param(silent(delta_chunk([])), id="delta-not-a-dict"),
    pytest.param(silent(delta_chunk({"unknown": 1})), id="unknown-delta-field"),
    pytest.param(silent(delta_chunk({"content": "text"})), id="content"),
    pytest.param(silent(delta_chunk({"role": "system"})), id="role"),
    pytest.param(silent(delta_chunk({"refusal": ""})), id="refusal"),
    pytest.param(silent(delta_chunk({"function_call": {}})), id="function-call"),
    pytest.param(silent(delta_chunk({"tool_calls": {}})), id="tool-calls-not-a-list"),
    pytest.param(tooled(delta_chunk({"tool_calls": [function(), function()]})), id="two-calls"),
    pytest.param(tooled(delta_chunk({"tool_calls": ["x"]})), id="call-not-a-dict"),
    pytest.param(tooled(call({**function(), "unknown": 1})), id="unknown-call-field"),
    pytest.param(tooled(call({**function(), "index": True})), id="bool-index"),
    pytest.param(tooled(call({**function(), "index": 1})), id="second-index"),
    pytest.param(tooled(call({**function(), "type": "custom"})), id="custom-type"),
    pytest.param(tooled(call({"function": []})), id="function-not-a-dict"),
    pytest.param(tooled(call({"function": {"name": "read_context", "arguments": "{}", "unknown": 1}})),
                 id="unknown-function-field"),
    pytest.param(tooled(call({"function": {"name": 1}})), id="name-not-text"),
    pytest.param(tooled(call({"function": {"arguments": 1}})), id="arguments-not-text"),
    pytest.param(tooled(call(function("n" * 65, ""))), id="name-too-long"),
    pytest.param(tooled(call(function("read_context", "x" * 1025))), id="arguments-too-long"),
    pytest.param(tooled(call(function("n" * 33, "")), call(function("n" * 32, ""))), id="name-fragments-too-long"),
    pytest.param(tooled(call(function("", "x" * 513)), call(function("", "x" * 512))), id="argument-fragments-too-long"),
]


@pytest.mark.parametrize("response", FRAME_REFUSALS)
def test_each_frame_rule_refuses_on_its_own(response):
    boundary = AssistantModelBoundary(None)
    assert refused(response, boundary) == "sse_frame"
    assert boundary.failure["exception_type"] == "ValueError"


@pytest.mark.parametrize("response", [
    pytest.param(silent("1"), id="json-scalar"),
    pytest.param(silent("{not json"), id="not-json"),
    pytest.param(silent({"choices": [None]}), id="null-choice"),
    pytest.param(silent({"choices": [1]}), id="scalar-choice"),
])
def test_malformed_frames_stop_in_the_frame_phase_with_an_unclassified_type(response):
    boundary = AssistantModelBoundary(None)
    assert refused(response, boundary) == "sse_frame"
    assert boundary.failure["exception_type"] == "UNCLASSIFIED"


@pytest.mark.parametrize("chunk", [
    delta_chunk({}),
    delta_chunk({"tool_calls": []}),
    delta_chunk({"tool_calls": None}),
    delta_chunk({"role": "assistant", "content": ""}),
    {"choices": []},
    {"choices": [{}]},
])
def test_frames_without_a_call_leave_a_silent_stop_silent(chunk):
    result = asyncio.run(AssistantModelBoundary(None).response(silent(chunk)))
    assert chunks(result)[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}


@pytest.mark.parametrize("response", [
    pytest.param(raw(b""), id="empty"),
    pytest.param(frames(STOP, done=False), id="no-done"),
    pytest.param(frames(call(function()), STOP), id="tool-with-stop"),
    pytest.param(frames(delta_chunk({}, "tool_calls")), id="silent-with-tool-calls"),
])
def test_incomplete_streams_stop_in_the_terminal_phase(response):
    assert refused(response) == "sse_terminal"


def test_an_empty_choice_is_a_frame_and_a_call_without_a_function_still_opens_a_tool():
    assert refused(frames({"choices": [{}]}, done=False)) == "sse_terminal"
    assert refused(frames(call({"index": 0}), STOP)) == "sse_terminal"
    assert refused(tooled(call({"index": 0}))) == "tool_schema"


def tool_stream(name="read_context", arguments="{}"):
    return tooled(call(function(name, arguments)))


@pytest.mark.parametrize("name,arguments", [
    ("shell", "{}"),
    ("", "{}"),
    ("n" * 64, "{}"),
    ("read_skill", "{}"),
    ("read_context", ""),
    ("read_context", "[]"),
    ("read_context", '{"extra":1}'),
    ("read_context", "x" * 1024),
    ("finish_task", '{"extra":1}'),
    ("execute_operation", '{"extra":1}'),
    ("save_result", "{}"),
    ("save_result", '{"item_count":1,"completed_count":1,"remaining_count":-1}'),
    ("save_result", '{"item_count":1,"item_count":1,"completed_count":1,"remaining_count":0}'),
])
def test_only_declared_tools_with_typed_arguments_pass(name, arguments):
    assert refused(tool_stream(name, arguments)) == "tool_schema"


def test_split_call_rebuilds_with_server_ids_and_canonical_arguments():
    response = frames(
        call({"index": 0, "id": "provider-id", "type": "function", "function": {"name": "save_", "arguments": '{"item_count"'}}),
        call({"function": None}),
        call({"function": {"name": "result", "arguments": ':1, "completed_count":1,'}}),
        delta_chunk({"tool_calls": []}),
        delta_chunk({"tool_calls": None}),
        call({"function": {"arguments": ' "remaining_count":0}'}}),
        delta_chunk({}, "tool_calls"),
        {"choices": [], "usage": {"prompt_tokens": 1}},
    )
    result = asyncio.run(AssistantModelBoundary(None).response(response))
    assert result.headers["content-type"] == "text/event-stream"
    assert str(result.request.url).endswith("/chat/completions")
    assert result.text.endswith("data: [DONE]\n\n") and result.text.count("data: ") == 3
    common = {"id": "fa_response_1", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4.1-mini"}
    assert chunks(result) == [
        {**common, "choices": [{"index": 0, "finish_reason": None, "delta": {"role": "assistant", "tool_calls": [{
            "index": 0, "id": "fa_call_1", "type": "function", "function": {
                "name": "save_result",
                "arguments": '{"item_count":1,"completed_count":1,"remaining_count":0}'}}]}}]},
        {**common, "choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {}}]},
    ]


@pytest.mark.parametrize("name", ["read_context", "finish_task", "execute_operation"])
def test_empty_tools_pass_with_canonical_arguments(name):
    result = asyncio.run(AssistantModelBoundary(None).response(tool_stream(name, " { } ")))
    assert chunks(result)[0]["choices"][0]["delta"]["tool_calls"][0]["function"] == {"name": name, "arguments": "{}"}


def test_silent_stop_frames_are_exact():
    result = asyncio.run(AssistantModelBoundary(None).response(frames(STOP)))
    assert result.text == "".join("data: " + json.dumps({
        "id": "fa_response_1", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4.1-mini",
        "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}) + "\n\n" for delta, reason in (
            ({"role": "assistant", "content": ""}, None), ({}, "stop"))) + "data: [DONE]\n\n"


def test_counter_moves_after_the_terminal_check_and_before_the_schema_check():
    boundary = AssistantModelBoundary(None)
    assert refused(frames(STOP, done=False), boundary) == "sse_terminal"
    assert boundary.counter == 0
    boundary.failure = None
    assert refused(tool_stream("shell"), boundary) == "tool_schema"
    assert boundary.counter == 1
    first = chunks(asyncio.run(boundary.response(tool_stream())))
    assert first[0]["id"] == "fa_response_2"
    assert first[0]["choices"][0]["delta"]["tool_calls"][0]["id"] == "fa_call_2"
    assert chunks(asyncio.run(boundary.response(frames(STOP))))[0]["id"] == "fa_response_3"


def test_first_failure_is_kept_when_the_same_boundary_fails_again():
    boundary = AssistantModelBoundary(None)
    assert refused(raw(b"{}", status=500), boundary) == "response_status"
    assert refused(raw(b"{}", content_type="application/json"), boundary) == "response_status"


class Recording(httpx.AsyncByteStream):
    def __init__(self, parts=(), *, error=None, close_error=None):
        self.parts, self.error, self.close_error, self.closed = parts, error, close_error, 0

    async def __aiter__(self):
        for part in self.parts:
            yield part
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def over(stream):
    return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"},
                          request=httpx.Request("POST", "https://synthetic.invalid/chat/completions"))


@pytest.mark.parametrize("parts,text_reply", [([b"x"], True), ([frames(STOP).content], False), ([b"bad"], False)])
def test_a_buffered_response_is_closed_once(parts, text_reply):
    stream = Recording(parts)
    boundary = AssistantModelBoundary(None, text_reply=text_reply)
    try:
        asyncio.run(boundary.response(over(stream)))
    except RuntimeError:
        pass
    assert stream.closed == 1


def test_streaming_hands_the_open_response_to_the_sdk():
    stream = Recording([b"data: 1\n\n", b"data: 2\n\n"])
    boundary = AssistantModelBoundary(None, text_reply=True, streaming=True)

    async def run():
        result = await boundary.response(over(stream))
        assert stream.closed == 0
        assert result.status_code == 200 and result.headers["content-type"] == "text/event-stream"
        assert str(result.request.url).endswith("/chat/completions")
        body = b"".join([part async for part in result.aiter_raw()])
        await result.aclose()
        return body

    assert asyncio.run(run()) == b"data: 1\n\ndata: 2\n\n"
    assert stream.closed == 1 and boundary.failure is None


def test_a_stream_failure_after_hand_off_is_recorded_for_the_turn():
    stream = Recording([b"data: 1\n\n"], error=httpx.ReadTimeout("x"))
    boundary = AssistantModelBoundary(None, text_reply=True, streaming=True)

    async def run():
        result = await boundary.response(over(stream))
        with pytest.raises(httpx.ReadTimeout):
            async for _ in result.aiter_raw():
                pass

    asyncio.run(run())
    assert boundary.failure["upstream_error_phase"] == "stream"
    assert boundary.failure["exception_type"] == "ReadTimeout"
    assert stream.closed == 1


@pytest.mark.parametrize("text_reply", [False, True], ids=["legacy", "free"])
def test_only_both_flags_hand_off_the_response(text_reply):
    stream = Recording([frames(STOP).content])
    boundary = AssistantModelBoundary(None, text_reply=text_reply, streaming=not text_reply)
    assert asyncio.run(boundary.response(over(stream))).status_code == 200
    assert stream.closed == 1


def test_streaming_still_checks_status_and_headers_first():
    boundary = AssistantModelBoundary(None, text_reply=True, streaming=True)
    assert refused(raw(b"{}", status=500), boundary) == "response_status"
    boundary = AssistantModelBoundary(None, text_reply=True, streaming=True)
    assert refused(raw(b"{}", content_type="application/json"), boundary) == "response_headers"


class CloseFails:
    """httpx closes a fully read body itself, so a late close failure needs a response that does not."""
    status_code, headers, request = 200, {"content-type": "text/event-stream"}, None

    def __init__(self, body):
        self.body = body

    async def aiter_bytes(self):
        yield self.body

    async def aclose(self):
        raise OSError("x")


def test_close_failure_after_success_refuses_the_result():
    boundary = AssistantModelBoundary(None)
    assert refused(CloseFails(frames(STOP).content), boundary) == "response_close"
    assert boundary.counter == 1


def test_close_failure_after_refusal_keeps_the_refusal_and_logs(caplog):
    boundary = AssistantModelBoundary(None)
    assert refused(CloseFails(b"bad"), boundary) == "sse_frame"
    assert "ASSISTANT_MODEL_RESPONSE_CLOSE_UNRESOLVED" in caplog.text


def test_cancelling_the_read_is_recorded_and_not_rewritten():
    stream = Recording([b"data: "], error=asyncio.CancelledError())
    boundary = AssistantModelBoundary(None)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await boundary.response(over(stream))

    asyncio.run(run())
    assert boundary.failure["boundary_phase"] == "response_body" and stream.closed == 1
