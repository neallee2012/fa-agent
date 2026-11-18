"""Free-instruction observers share SSE framing; they do not replace the SDK loop."""

import json

import pytest

from shared_context.behavior_instruction_cost import usage_from_sse
from shared_context.behavior_instruction_stream import AnswerEvents
from shared_context.errors import Problem
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key


def test_standard_data_field_without_a_space_preserves_reported_usage():
    raw = b'data:{"choices":[],"usage":{"prompt_tokens":17,"completion_tokens":3}}\n\n'
    assert usage_from_sse(raw) == (17, 3)


def test_multiline_data_is_one_complete_json_event_for_preview_and_usage():
    raw = (
        b'data: {"choices":[{"index":0,"delta":{"content":"Exact preview"}}],\n'
        b'data: "usage":{"prompt_tokens":17,"completion_tokens":3}}\n\n'
    )
    visible = []
    AnswerEvents(visible.append).feed(raw)
    assert visible == ["Exact preview"]
    assert usage_from_sse(raw) == (17, 3)


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r"])
def test_a_complete_json_line_without_an_event_delimiter_does_not_confirm_usage(ending):
    raw = b'data: {"choices":[],"usage":{"prompt_tokens":17,"completion_tokens":3}}' + ending
    assert usage_from_sse(raw) is None


@pytest.mark.parametrize("newline", [b"\n", b"\r", b"\r\n"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_every_byte_split_preserves_multiline_unicode_and_sse_boundaries(newline, bom):
    text = "Exact \u0085 \u2028 \u2029 text"
    value = {"choices": [{"index": 0, "delta": {"content": text, "reasoning_content": "PRIVATE",
                                              "tool_calls": [{"private": "PRIVATE"}]}}],
             "usage": {"prompt_tokens": 17, "completion_tokens": 3}}
    lines = json.dumps(value, ensure_ascii=False, indent=2).encode().split(b"\n")
    raw = bom + b": keepalive" + newline + b"event: message" + newline
    raw += b"".join(b"data:" + line + newline for line in lines) + newline + b"data: [DONE]" + newline * 2
    for cut in range(len(raw) + 1):
        visible = []
        parser = AnswerEvents(visible.append)
        parser.feed(raw[:cut])
        parser.feed(b"")
        parser.feed(raw[cut:])
        assert visible == [text], cut
    assert usage_from_sse(raw) == (17, 3)


def test_preview_waits_for_an_event_not_merely_a_json_line():
    visible = []
    parser = AnswerEvents(visible.append)
    parser.feed(b'data: {"choices":[{"delta":{"content":"original"}}]}\r')
    assert visible == []
    parser.feed(b"\n")
    assert visible == []
    parser.feed(b"\r")
    assert visible == ["original"]
    parser.feed(b"\n")
    assert visible == ["original"]


@pytest.mark.parametrize("prefix", [b"Data:", b"data :", b":data:", b"id:data:"])
def test_only_the_exact_data_field_is_interpreted(prefix):
    raw = prefix + b' {"usage":{"prompt_tokens":17,"completion_tokens":3}}\n\n'
    assert usage_from_sse(raw) is None
    visible = []
    AnswerEvents(visible.append).feed(raw)
    assert visible == []


def test_usage_is_not_salvaged_from_one_line_of_an_invalid_multiline_event():
    raw = (b'data: {"choices":[],"usage":{"prompt_tokens":17,"completion_tokens":3}}\n'
           b'data: {"another":"JSON value in the same event"}\n\n')
    assert usage_from_sse(raw) is None
    with pytest.raises(Problem) as denied:
        AnswerEvents(lambda _text: pytest.fail("invalid event cannot become preview")).feed(raw)
    assert denied.value.body["error"]["code"] == "ASSISTANT_MODEL_STREAM_INVALID"


@pytest.mark.parametrize("counts", [
    {"prompt_tokens": True, "completion_tokens": 3},
    {"prompt_tokens": 17, "completion_tokens": -1},
    {"prompt_tokens": 17},
])
def test_invalid_usage_is_still_unknown(counts):
    raw = b"data:" + json.dumps({"choices": [], "usage": counts}).encode() + b"\n\n"
    with pytest.raises(Problem) as denied:
        usage_from_sse(raw)
    assert denied.value.body["error"]["code"] == "INSTRUCTION_USAGE_UNKNOWN"


def test_duplicate_equal_usage_is_one_observation_but_conflict_is_not_accepted():
    first = b'data:{"choices":[],"usage":{"prompt_tokens":17,"completion_tokens":3}}\n\n'
    assert usage_from_sse(first + first) == (17, 3)
    conflict = b'data:{"choices":[],"usage":{"prompt_tokens":18,"completion_tokens":3}}\n\n'
    with pytest.raises(Problem) as denied:
        usage_from_sse(first + conflict)
    assert denied.value.body["error"]["code"] == "INSTRUCTION_USAGE_UNKNOWN"
