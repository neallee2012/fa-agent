"""Responses use the original instruction authority, checkpoint and cost ledger."""
import json
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_instruction_cost import cost_key, record_usage, replay_cost_write, usage_from_sse
from shared_context.behavior_operations import _row
from shared_context.behavior_work_policy import CostLedger
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.source_materials import digest
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_inventory, instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn
from test_work_policy_lifecycle import enabled


def responses_usage(input_tokens=21, output_tokens=13, reasoning_tokens=8):
    return ("data: " + json.dumps({
        "type": "response.completed", "response": {"status": "completed", "usage": {
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        }},
    }) + "\n\n").encode()


def test_responses_usage_moves_pending_to_the_original_ledger_without_losing_reasoning(assistant):
    h, control, human, _ = enabled(assistant)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    turn = open_turn(h, control, human, "responses-usage")
    try:
        body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "Synthetic cost seam."}], "stream": True}
        identity = digest(body)
        turn.client.reserve_model(identity)
        principal = h.verifier(h.client.build_request("GET", "/", headers=h.headers("agent")))
        before = _row(h.store.read_snapshot((control.cost_key,)), control.cost_key, CostLedger)
        h.assistant.consume_model(turn.session, aw.ModelRequest(request_id=identity, body=body), principal)
        pending = _row(h.store.read_snapshot((control.cost_key,)), control.cost_key, CostLedger)
        assert pending.model_pending_microusd > 0
        counts = usage_from_sse(responses_usage(), wire_api="responses")
        assert counts == (21, 13, 8)
        record_usage(h.assistant, turn.session, identity, counts)
        identifier = cost_key(turn.assignment.session_id, identity)
        saved = h.store.read_snapshot((identifier, control.cost_key))
        cost = saved[identifier].payload["data"]
        ledger = _row(saved, control.cost_key, CostLedger)
        assert (cost["input_tokens"], cost["output_tokens"], cost["reasoning_tokens"]) == (21, 13, 8)
        assert cost["metered_microusd"] == 30
        assert ledger.model_pending_microusd == 0 and ledger.model_metered_microusd == 30
        assert ledger.legacy_unresolved_reserve_microusd == pending.legacy_unresolved_reserve_microusd
        assert ledger.work_reservations_microusd == pending.work_reservations_microusd
        from shared_context.behavior_recovery_history import _Inventory
        inventory = instruction_inventory(h)
        view = _Inventory(inventory, SimpleNamespace(table=SimpleNamespace(scope=h.store._scope), sha256="f"*64,
            workers=h.workers.contract, delivery=h.delivery.contract))
        replayed = before
        for receipt in sorted((receipt for receipt in view.receipts.values() if any(
                inventory.rows[key].payload["data"].get("kind") == "instruction_model_cost"
                for key, _ in receipt.writes)), key=lambda receipt: receipt.guard_revision):
            replayed = replay_cost_write(view, receipt, replayed, control.records, control.policy)
        assert replayed == ledger
        assert not h.endpoint.requests
    finally:
        turn.client.close()


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


def test_responses_checkpoint_publication_and_restore_remain_bound_to_the_captured_provider(assistant):
    from shared_context.behavior_sdk_checkpoint import SdkCheckpointStore
    from test_conversation_profile_selection import configured, select, template
    from test_sdk_compaction_product import ordinary, settle
    h, control, human = configured(assistant)
    assert select(h, value=template(model_provider="openai-responses"))[0].status_code == 200
    first = open_turn(h, control, human, "responses-checkpoint")
    try:
        old_shape = ordinary(first.assignment.sdk_session_id)
        before = h.store._guard()
        with pytest.raises(Problem):
            first.client.save_sdk_state(old_shape, first.stop)
        assert h.store._guard() == before
        state = old_shape.model_copy(update={"model_provider": "openai-responses", "codec": "session-fs-responses-v1"})
        reference = settle(first, state)
        assert reference.model_provider == "openai-responses"
        second = open_turn(h, control, human, "responses-restore", first.assignment.task.instruction.conversation_id)
        try:
            assert second.client.restore() == state
            with pytest.raises(Problem) as error:
                SdkCheckpointStore(h.assistant.artifacts.store).load(reference, first.binding,
                    conversation_id=reference.conversation_id, sdk_session_id=reference.sdk_session_id,
                    job_id=reference.job_id)
            assert error.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
            from test_instruction_flow import check_history
            check_history(h)
        finally:
            second.client.close()
    finally:
        first.client.close()


def test_responses_history_tools_read_original_text_file_and_answer_with_the_saved_provider(assistant):
    from shared_context.behavior_assistant import key
    from shared_context.behavior_text_files import UserTextFile
    from test_conversation_profile_selection import configured, select, template
    from test_sdk_compaction_product import ordinary, settle
    h, control, human = configured(assistant)
    assert select(h, value=template("fa-instruction-history-v1", model_provider="openai-responses",
        user_text_file_profile="fa-user-text-files-v1"))[0].status_code == 200
    first = open_turn(h, control, human, "responses-originals",
                      files=(UserTextFile(name="original.txt", text="Original file bytes.\r\n"),))
    second = None
    try:
        state = ordinary(first.assignment.sdk_session_id).model_copy(
            update={"model_provider": "openai-responses", "codec": "session-fs-responses-v1"})
        settle(first, state)
        keys = (key("text-archive", first.binding.job_id), key("result", first.binding.work.work_id))
        original = h.store.read_snapshot(keys)
        second = open_turn(h, control, human, "responses-read-originals", first.assignment.task.instruction.conversation_id)
        assert second.client.read_user_turn(1).text == first.assignment.task.instruction.content
        assert second.client.read_user_file(1, "original.txt").text == "Original file bytes.\r\n"
        assert second.client.read_assistant_reply(1).reply.text == "Original inventory response."
        assert h.store.read_snapshot(keys) == original and h.endpoint.requests == []
    finally:
        if second is not None:
            second.client.close()
        first.client.close()


@pytest.mark.parametrize("model_provider", [None, "openai-responses"])
def test_responses_cannot_publish_a_legacy_inline_archive_without_a_provider_binding(assistant, tmp_path, model_provider):
    from shared_context.behavior_assistant_checkpoint import export
    from test_conversation_profile_selection import configured, select, template
    from test_sdk_compaction_product import ordinary
    h, control, human = configured(assistant)
    assert select(h, value=template(model_provider=model_provider))[0].status_code == 200
    turn = open_turn(h, control, human, "responses-no-inline")
    try:
        state = ordinary(turn.assignment.sdk_session_id)
        folder = tmp_path / "legacy" / "config" / "session-state" / state.sdk_session_id
        folder.mkdir(parents=True)
        for name, content in state.files.items():
            (folder / name).write_text(content, encoding="utf-8")
        archive = export(tmp_path / "legacy", state.sdk_session_id, public_only=True)
        before = h.store._guard()
        if model_provider is None:
            turn.client.save_text_reply("No legacy fallback.", archive, turn.stop)
            assert h.store._guard() != before
        else:
            with pytest.raises(Problem) as error:
                turn.client.save_text_reply("No legacy fallback.", archive, turn.stop)
            assert error.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
            assert h.store._guard() == before
    finally:
        turn.client.close()


def test_selected_responses_model_route_uses_the_original_send_and_records_provider_usage(assistant):
    from shared_context.behavior_assistant_api import create_assistant_app
    from shared_context.behavior_assistant_gateway import AssistantProvider
    from test_approved_skill_api import post_skill
    from test_conversation_profile_selection import configured, select, template

    h, control, human = configured(assistant)
    selected, _ = select(h, value=template(model_provider="openai-responses"))
    assert selected.status_code == 200, selected.text
    calls = []
    raw = responses_usage()

    def provider(request):
        calls.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=raw)

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE")), transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client = h.client
    turn = open_turn(h, control, human, "responses-route")
    try:
        body = {"model": "gpt-4.1-mini", "input": [{"role": "user", "content": "Original Responses request."}],
                "stream": True, "store": False}
        identity = digest(body)
        turn.client.reserve_model(identity)
        response = post_skill(turn, {"request_id": identity, "body": body}, action="model")
        assert response.status_code == 200, response.text
        assert response.content == raw
        assert calls == [("https://provider.invalid/openai/v1/responses", body)]
        identifier = cost_key(turn.assignment.session_id, identity)
        saved = h.store.read_snapshot((identifier, control.cost_key))
        cost = saved[identifier].payload["data"]
        assert (cost["input_tokens"], cost["output_tokens"], cost["reasoning_tokens"]) == (21, 13, 8)
        assert cost["metered_microusd"] == 30
        ledger = _row(saved, control.cost_key, CostLedger)
        assert ledger.model_pending_microusd == 0 and ledger.model_metered_microusd == 30
        assert post_skill(turn, {"request_id": identity, "body": body}, action="model").status_code == 409
        assert len(calls) == 1 and h.store.read_snapshot((identifier, control.cost_key)) == saved
    finally:
        turn.client.close()
