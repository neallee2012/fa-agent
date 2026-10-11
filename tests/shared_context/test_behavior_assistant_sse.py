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
