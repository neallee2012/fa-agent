"""Responses use the original instruction authority, checkpoint and cost ledger."""
import json
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_instruction_cost import usage_from_sse
from shared_context.errors import Problem
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_inventory, instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key


def responses_usage(input_tokens=21, output_tokens=13, reasoning_tokens=8):
    return ("data: " + json.dumps({
        "type": "response.completed", "response": {"status": "completed", "usage": {
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        }},
    }) + "\n\n").encode()


@pytest.mark.parametrize("input_tokens,output_tokens,reasoning_tokens", [
    (True, 13, 8), (21, -1, 0), (21, 13, True), (21, 13, -1), (21, 13, 14), (None, 13, 8),
])
def test_responses_invalid_usage_remains_unknown(input_tokens, output_tokens, reasoning_tokens):
    with pytest.raises(Problem) as error:
        usage_from_sse(responses_usage(input_tokens, output_tokens, reasoning_tokens), wire_api="responses")
    assert error.value.body["error"]["code"] == "INSTRUCTION_USAGE_UNKNOWN"


def test_responses_missing_usage_is_unknown_and_reasoning_is_not_counted_twice():
    assert usage_from_sse(b'data: {"type":"response.completed","response":{"usage":null}}\n\n',
                          wire_api="responses") is None
    assert usage_from_sse(responses_usage()[:-1], wire_api="responses") is None
    assert usage_from_sse(responses_usage(21, 13, None), wire_api="responses") == (21, 13)
    assert usage_from_sse(responses_usage(21, 13, 0), wire_api="responses") == (21, 13, 0)
    with pytest.raises(Problem):
        usage_from_sse(responses_usage() + responses_usage(21, 14, 8), wire_api="responses")


@pytest.mark.parametrize("changed", [
    {"store": True}, {"previous_response_id": "provider-owned"},
    {"conversation": "provider-owned"}, {"input": [{"type": "item_reference", "id": "provider-owned"}]},
    {"input": [{"type": ["unknown"]}]}, {"reasoning": {"summary": {"private": "not-a-mode"}}},
    {"tools": [{"type": "function", "name": "unapproved"}]},
    {"tools": [{"type": "function", "name": ["unapproved"]}]},
])
def test_responses_gateway_refuses_provider_state_and_unknown_tools_without_sending(changed):
    from shared_context.behavior_assistant_gateway import AssistantProvider
    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions", None)
    body = {"model": "gpt-5.4-mini", "input": [{"role": "user", "content": "Local history"}],
            "stream": True, "store": False, **changed}
    with pytest.raises(Problem) as error:
        gateway.validate(body, text_reply=True, model="gpt-5.4-mini", model_provider="openai-responses")
    assert error.value.status in {403, 422}


@pytest.mark.parametrize("selected", [None, "openai-responses"], ids=["completions", "responses"])
def test_current_server_provider_forwards_the_captured_protocol_and_before_send(selected):
    from shared_context.behavior_assistant_server import _CurrentAssistantProvider

    endpoint = "https://provider.invalid/openai/v1/chat/completions"
    composition = SimpleNamespace(config=SimpleNamespace(assistant=SimpleNamespace(provider_endpoint=endpoint)))
    calls, checked = [], []

    def transport(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, content=responses_usage() if selected else (
            b'data: {"choices":[{"delta":{"content":"Original reply."}}]}\n\ndata: [DONE]\n\n'),
            headers={"content-type": "text/event-stream"})

    gateway = _CurrentAssistantProvider(composition,
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE-provider-token")),
        transport=httpx.MockTransport(transport))
    body = {"model": "gpt-4.1-mini", "stream": True}
    if selected:
        body.update(input=[{"role": "user", "content": "Original local input."}], store=False)
    else:
        body["messages"] = [{"role": "user", "content": "Original local input."}]

    async def before_send():
        checked.append("original-authority")

    async def run():
        try:
            response = await gateway.send(body, before_send=before_send, text_reply=True,
                model="gpt-4.1-mini", stream_response=True, diagnostic_keys=("original-binding",),
                **({"model_provider": selected} if selected is not None else {}))
            await response.aread()
            await response.aclose()
        finally:
            await gateway.aclose()

    asyncio.run(run())
    assert checked == ["original-authority"] and len(calls) == 1
    assert calls[0][0] == ("/openai/v1/responses" if selected else "/openai/v1/chat/completions")
    assert calls[0][1]["store"] is False
    assert ("input" in calls[0][1]) is bool(selected)
    assert "diagnostic_keys" not in calls[0][1] and "model_provider" not in calls[0][1]


def test_responses_model_transport_preserves_the_main_screenshot_budget_behavior():
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.m2_copilot import MODEL_IMAGE_URL_CHARS, _ModelGateway
    endpoint = httpx.URL("https://context.invalid/model/v1/responses")
    parts = [
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        {"type": "input_image", "image_url": "data:image/jpeg;base64,AAAA"},
        {"type": "input_image", "image_url": "data:image/png;base64," + "A"*MODEL_IMAGE_URL_CHARS},
    ]
    body = {"model": "gpt-5.4-mini", "input": [{"type": "function_call_output", "call_id": "call", "output": parts}],
            "stream": True, "store": False}
    sent, reserved = [], []

    async def reserve(identity):
        reserved.append(identity)

    async def token(_):
        return "OFFLINE"

    def provider(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=responses_usage())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            gateway = _ModelGateway(endpoint, client, lambda _: None, model="gpt-5.4-mini", instruction=True,
                private_token=token, boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True))
            response = await gateway.send_request(httpx.Request("POST", endpoint, json=body),
                                                  SimpleNamespace(request_id="request", session_id="session"))
            await response.aread()
            await response.aclose()

    asyncio.run(run())
    assert len(sent) == len(reserved) == 1
    assert [part["type"] for part in sent[0]["input"][0]["output"]] == ["input_image", "input_text", "input_text"]
    assert all(part["type"] == "input_image" for part in body["input"][0]["output"])


def reasoning_message():
    return {"type": "assistant.message", "timestamp": "2026-09-30T08:00:00Z", "data": {
        "content": "The final answer is separate.",
        "reasoning_text": "PRIVATE_RAW_REASONING", "encrypted_content": "PRIVATE_ENCRYPTED_REASONING",
        "reasoning_blocks": {"provider": "openai-responses", "blocks": [{
            "type": "reasoning", "id": "reason-one", "encrypted_content": "PRIVATE_ENCRYPTED_REASONING",
            "summary": [{"type": "summary_text", "text": "I checked the two public sources."}],
        }]},
    }}


def test_only_explicit_official_summary_items_are_projected_for_the_selected_provider():
    from shared_context.behavior_assistant_progress import public_event
    message = reasoning_message()
    event = public_event(message, 1, {}, model_provider="openai-responses", reasoning_summary=True)
    assert event.reasoning_summary == "I checked the two public sources."
    assert event.kind == "stage" and "PRIVATE_" not in event.model_dump_json()
    assert public_event(message, 1, {}) is None
    assert public_event(message, 1, {}, model_provider="openai-responses") is None
    del message["data"]["reasoning_blocks"]
    assert public_event(message, 1, {}, model_provider="openai-responses", reasoning_summary=True) is None


@pytest.mark.parametrize("variant", ["provider", "type", "text", "child", "untyped", "empty"])
def test_unrecognized_or_child_reasoning_never_falls_back_to_raw_text(variant):
    from shared_context.behavior_assistant_progress import public_event
    message = reasoning_message()
    container = message["data"]["reasoning_blocks"]
    if variant == "provider":
        container["provider"] = "unrecognized-provider"
    elif variant == "type":
        container["blocks"][0]["summary"][0]["type"] = "reasoning_text"
    elif variant == "text":
        container["blocks"][0]["summary"][0]["text"] = {"raw": "PRIVATE_REASONING"}
    elif variant == "child":
        message["agent_id"] = "another-agent"
    elif variant == "untyped":
        message["data"]["reasoning_blocks"] = ["PRIVATE_REASONING"]
    else:
        container["blocks"][0]["summary"] = []
    assert public_event(message, 1, {}, model_provider="openai-responses", reasoning_summary=True) is None


@pytest.mark.parametrize("ordered, projected", [(None, True), ([], False), ("x", False)])
def test_the_newer_runtimes_empty_ordered_blocks_keeps_the_public_summary(ordered, projected):
    from shared_context.behavior_assistant_progress import public_event
    message = reasoning_message()
    message["data"]["reasoning_blocks"]["ordered_blocks"] = ordered
    event = public_event(message, 1, {}, model_provider="openai-responses", reasoning_summary=True)
    assert (event is not None) is projected


def ordered_blocks_files(ordered):
    from shared_context.behavior_sdk_files import SdkSessionFiles
    message = reasoning_message()
    message["data"]["reasoning_blocks"]["ordered_blocks"] = ordered
    files = SdkSessionFiles("ordered-blocks", model_provider="openai-responses")
    files.files = {"workspace.yaml": "id: ordered-blocks\n", "events.jsonl": json.dumps({
        "type": "session.start", "data": {"sessionId": "ordered-blocks"}}) + "\n" + json.dumps(message) + "\n"}
    return files


def test_the_responses_checkpoint_accepts_empty_ordered_blocks_and_drops_the_private_reasoning():
    assert "PRIVATE_" not in ordered_blocks_files(None).snapshot().encoded().decode()


@pytest.mark.parametrize("ordered", [[], [{"type": "reasoning"}]])
def test_the_responses_checkpoint_refuses_ordered_blocks_that_carry_anything(ordered):
    with pytest.raises(Problem) as error:
        ordered_blocks_files(ordered).snapshot()
    assert error.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"


def test_reasoning_summary_metadata_cannot_claim_a_missing_or_private_source():
    from pydantic import ValidationError
    for value in (
        {"availability": "provided", "source": "copilot-sdk-1.0.13", "text": "not-public"},
        {"availability": "provided", "source": "openai-responses", "text": None},
        {"availability": "not_provided", "source": "openai-responses", "text": "not-public"},
    ):
        with pytest.raises(ValidationError):
            aw.PublicReasoningSummary.model_validate(value)


def test_responses_public_checkpoint_removes_encryption_without_rewriting_native_files():
    from shared_context.behavior_sdk_files import SdkSessionFiles
    message = reasoning_message()
    data = message["data"]
    data["reasoningBlocks"] = data.pop("reasoning_blocks")
    data["encryptedContent"] = data.pop("encrypted_content")
    events = [{"type": "session.start", "id": "start", "data": {
        "sessionId": "responses-private", "provider": "openai-responses"}},
        {"id": "reply", "parentId": "start", **message}]
    originals = {"events.jsonl": "".join(json.dumps(event) + "\n" for event in events), "workspace.yaml": "id: responses-private\n"}
    files = SdkSessionFiles("responses-private", model_provider="openai-responses")
    files.files = dict(originals)
    saved = files.snapshot()
    assert "PRIVATE_" not in saved.encoded().decode()
    assert files.files == originals
    retained = [json.loads(line) for line in saved.files["events.jsonl"].splitlines()]
    assert [(event["id"], event.get("parentId")) for event in retained] == [("start", None), ("reply", "start")]
    assert retained[-1]["data"]["content"] == "The final answer is separate."


def test_observed_response_id_block_metadata_is_accepted_and_dropped_with_the_reasoning_block():
    from shared_context.behavior_sdk_files import SdkSessionFiles
    message = {"id":"reply","parentId":"start",**reasoning_message()}
    block = message["data"]["reasoning_blocks"]["blocks"][0]
    block["response_id"] = "resp_synthetic_metadata"
    block["content"] = [{"type":"reasoning_text","text":"PRIVATE_RAW_CONTENT"}]
    assert set(block) == {"content","encrypted_content","id","response_id","summary","type"}
    files = SdkSessionFiles("response-id", model_provider="openai-responses")
    files.files = {"workspace.yaml":"id: response-id\n","events.jsonl": json.dumps({
        "type":"session.start","id":"start","data":{"sessionId":"response-id"}}) + "\n" + json.dumps(message) + "\n"}
    original = dict(files.files)
    saved = files.snapshot()
    encoded = saved.encoded().decode()
    assert "PRIVATE_" not in encoded and "resp_synthetic_metadata" not in encoded
    retained = [json.loads(line) for line in saved.files["events.jsonl"].splitlines()]
    assert [(event["id"],event.get("parentId")) for event in retained] == [("start",None),("reply","start")]
    assert retained[-1]["data"]["content"] == message["data"]["content"]
    assert files.files == original


@pytest.mark.parametrize("change", [
    {"response_id": 7}, {"response_id": None}, {"response_id": {"id": "resp"}},
    {"response_id": "resp", "future_private_content": "PRIVATE_UNKNOWN"},
])
def test_response_id_is_accepted_only_as_the_observed_string_metadata(change):
    from shared_context.behavior_sdk_files import SdkSessionFiles
    message = reasoning_message()
    message["data"]["reasoning_blocks"]["blocks"][0].update(change)
    originals = {"workspace.yaml": "id: response-id-shape\n", "events.jsonl": json.dumps({
        "type": "session.start", "data": {"sessionId": "response-id-shape"}}) + "\n" + json.dumps(message) + "\n"}
    files = SdkSessionFiles("response-id-shape", model_provider="openai-responses")
    files.files = dict(originals)
    with pytest.raises(Problem) as error:
        files.snapshot()
    assert error.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    assert files.files == originals


@pytest.mark.parametrize("variant", ["summary-in-answer", "completed-status"])
def test_responses_checkpoint_preserves_public_summary_and_metadata_repeated_in_public_content(variant):
    from shared_context.behavior_sdk_files import SdkSessionFiles
    message = reasoning_message()
    data = message["data"]
    block = data["reasoning_blocks"]["blocks"][0]
    if variant == "summary-in-answer":
        data["content"] = data["reasoning_text"] = block["summary"][0]["text"]
    else:
        block["status"] = data["responses_message_status"] = "completed"
    files = SdkSessionFiles("public-duplicates", model_provider="openai-responses")
    files.files = {"workspace.yaml": "id: public-duplicates\n", "events.jsonl": json.dumps({
        "type": "session.start", "data": {"sessionId": "public-duplicates"}}) + "\n" + json.dumps(message) + "\n"}
    before = dict(files.files)
    saved = files.snapshot()
    retained = json.loads(saved.files["events.jsonl"].splitlines()[-1])
    assert retained["data"]["content"] == data["content"]
    if variant == "completed-status":
        assert retained["data"]["responses_message_status"] == "completed"
    assert "PRIVATE_" not in saved.encoded().decode() and files.files == before


@pytest.mark.parametrize("private", ["encrypted", "raw-reasoning"])
def test_public_summary_does_not_allow_real_private_payload_copies(private):
    from shared_context.behavior_sdk_files import SdkSessionFiles
    message = reasoning_message()
    data = message["data"]
    if private == "encrypted":
        data["content"] = data["encrypted_content"]
    else:
        data["content"] = "PRIVATE_RAW_CONTENT"
        data["reasoning_blocks"]["blocks"][0]["content"] = [{"type": "reasoning_text", "text": data["content"]}]
    files = SdkSessionFiles("private-copies", model_provider="openai-responses")
    files.files = {"workspace.yaml": "id: private-copies\n", "events.jsonl": json.dumps({
        "type": "session.start", "data": {"sessionId": "private-copies"}}) + "\n" + json.dumps(message) + "\n"}
    with pytest.raises(Problem) as error:
        files.snapshot()
    assert error.value.body["error"]["code"] == "SDK_STATE_PRIVATE_MATERIAL"


def test_responses_checkpoint_rejects_unknown_reasoning_shapes_before_publication():
    from shared_context.behavior_sdk_files import SdkSessionFiles
    message = reasoning_message()
    message["data"]["reasoning_blocks"]["blocks"][0]["future_private_content"] = "PRIVATE_UNKNOWN"
    originals = {"workspace.yaml": "id: responses-private\n", "events.jsonl":
        json.dumps({"type": "session.start", "data": {"sessionId": "responses-private"}}) + "\n" + json.dumps(message) + "\n"}
    files = SdkSessionFiles("responses-private", model_provider="openai-responses")
    files.files = dict(originals)
    with pytest.raises(Problem) as error:
        files.snapshot()
    assert error.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    assert files.files == originals


def test_unknown_nested_private_item_refuses_the_checkpoint_and_retains_the_last_legal_state():
    from shared_context.behavior_sdk_files import SdkSessionFiles
    files = SdkSessionFiles("responses-last-valid", model_provider="openai-responses")
    files.files = {"workspace.yaml": "id: responses-last-valid\n", "events.jsonl": json.dumps({
        "type": "session.start", "id": "start", "data": {"sessionId": "responses-last-valid"}}) + "\n"}
    prior = files.snapshot()
    resumed = SdkSessionFiles("responses-last-valid", prior, model_provider="openai-responses")
    inner = {"role": "assistant", "content": "Keep the public answer.", "refusal": None,
             "reasoningBlocks": {"provider": "openai-responses", "blocks": [{
                 "type": "reasoning", "id": "reasoning", "summary": [],
                 "content": [{"type": "reasoning_text", "future_private": "PRIVATE_UNKNOWN"}],
             }]}}
    event = {"type": "model.response", "id": "response", "parentId": "start", "timestamp": "2026-09-30T08:00:00Z",
             "data": {"kind": "response", "turn": 2, "modelCall": {"model": "gpt-5.4-mini", "api_id": "offline"},
                      "response": inner}}
    resumed.files["events.jsonl"] += json.dumps(event) + "\n"
    before = dict(resumed.files)
    with pytest.raises(Problem) as error:
        resumed.snapshot()
    assert error.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    assert resumed.files == before and resumed.previous == prior
    assert "PRIVATE_UNKNOWN" not in prior.encoded().decode()
