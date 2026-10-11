"""Free-instruction observers share SSE framing; they do not replace the SDK loop."""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context.behavior_instruction_cost import usage_from_sse
from shared_context.behavior_instruction_stream import AnswerEvents
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_inventory, instruction_models, runtime_path
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


@pytest.mark.parametrize("variant", ["no-space", "multiline", "bom-cr", "incomplete-usage"])
def test_signed_model_route_keeps_wire_bytes_and_original_usage_responsibility(
        assistant, monkeypatch, variant):
    from shared_context.behavior_assistant import key
    from shared_context.behavior_assistant_api import create_assistant_app
    from shared_context.behavior_assistant_gateway import AssistantProvider
    from shared_context.behavior_instruction_cost import cost_key
    from shared_context.behavior_operations import _row, control_key
    from shared_context.behavior_work_policy import CostLedger
    from shared_context.behavior_work_policy_api import install_work_policy_routes
    from shared_context.source_materials import digest
    from test_approved_skill_api import post_skill
    from test_sdk_compaction_product import open_turn
    from test_work_policy_lifecycle import enabled

    def no_process(*args, **kwargs):
        pytest.fail("The signed SSE qualification cannot launch an SDK process")

    monkeypatch.setattr("subprocess.Popen", no_process)
    h, control, human, _ = enabled(assistant)
    packet = {"choices": [{"index": 0, "delta": {"content": "Original visible \u2028 text",
                "reasoning_content": "PRIVATE_REASONING_CANARY"}, "finish_reason": "stop"}]}
    usage = {"choices": [], "usage": {"prompt_tokens": 17, "completion_tokens": 3}}
    newline = b"\r" if variant == "bom-cr" else b"\n"
    prefix = b"\xef\xbb\xbf" if variant == "bom-cr" else b""

    def frame(value):
        data = json.dumps(value, ensure_ascii=False, indent=2 if variant == "multiline" else None).encode()
        return b"".join(b"data:" + line + newline for line in data.split(b"\n")) + newline

    raw = prefix + frame(packet)
    if variant == "incomplete-usage":
        raw += b"data: " + json.dumps(usage).encode() + b"\n"
    else:
        raw += frame(usage) + b"data:[DONE]" + newline * 2
    calls, tokens, closed = [], [], []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for offset in range(0, len(raw), 3):
                yield raw[offset:offset + 3]

        async def aclose(self):
            closed.append(True)

    def credential(_scope):
        tokens.append(True)
        return SimpleNamespace(token="OFFLINE_USAGE_TOKEN")

    def provider(request):
        calls.append(digest(json.loads(request.content)))
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                              stream=Body())

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=credential), transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client = h.client
    turn = open_turn(h, control, human, "framing-" + variant)
    try:
        body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "Original request."}], "stream": True}
        identity = digest(body)
        before = _row(h.store.read_snapshot((control.cost_key,)), control.cost_key, CostLedger)
        assert turn.client.reserve_model(identity).state == "accepted"
        response = post_skill(turn, {"request_id": identity, "body": body}, action="model")
        assert response.status_code == 200 and response.content == raw
        assert len(calls) == len(tokens) == 1 and closed
        preview = h.app.state.instruction_live_replies.read(turn.binding.job_id)
        assert preview["text"] == "Original visible \u2028 text" and preview["complete"] is False
        assert "PRIVATE_REASONING_CANARY" not in json.dumps(preview)
        cost_id = cost_key(turn.assignment.session_id, identity)
        intent_id = key("model-intent", turn.assignment.session_id, identity)
        keys = (control.cost_key, cost_id, intent_id)
        saved = h.store.read_snapshot(keys)
        cost = saved[cost_id].payload["data"]
        ledger = _row(saved, control.cost_key, CostLedger)
        assert cost["request_sha256"] == identity and cost["job_id"] == turn.binding.job_id
        assert saved[intent_id].payload["data"]["consumed"] is True
        receipt = instruction_inventory(h).receipts.get(control_key("instruction-model-usage", cost_id))
        if variant == "incomplete-usage":
            assert cost["stage"] == "pending" and cost["input_tokens"] is None and cost["output_tokens"] is None
            assert ledger.model_pending_microusd == before.model_pending_microusd + cost["pending_microusd"]
            assert ledger.model_metered_microusd == before.model_metered_microusd
            assert receipt is None
        else:
            assert cost["stage"] == "usage" and (cost["input_tokens"], cost["output_tokens"]) == (17, 3)
            assert cost["metered_microusd"] == 12
            assert ledger.model_pending_microusd == before.model_pending_microusd
            assert ledger.model_metered_microusd == before.model_metered_microusd + 12
            assert receipt.payload["result"] == {
                "request_sha256": identity, "usage_recorded": True, "retail_estimate_microusd": 12,
            }
        repeated = post_skill(turn, {"request_id": identity, "body": body}, action="model")
        assert repeated.status_code == 409 and len(calls) == len(tokens) == 1
        assert h.store.read_snapshot(keys) == saved
    finally:
        turn.client.close()
        h.client.close()
