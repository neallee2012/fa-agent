"""Agent-bound Responses HTTP calls; no platform, token acquisition, or model execution."""

import json

import httpx
import pytest

from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.behavior_foundry_responses import FoundryResponsesClient
from shared_context.errors import Problem
from shared_context.native_responses_wire import NativeResponseHeaders
from test_behavior_foundry import Credential, TARGET


@pytest.mark.parametrize("mode,previous,thread", [
    ("conversation", None, {"conversation": "conv-original"}),
    ("previous_response_id", None, {"metadata": {"fa_agent_id": TARGET.agent_id, "fa_team_id": "team-one"}}),
    ("previous_response_id", "resp-previous", {
        "previous_response_id": "resp-previous", "metadata": {"fa_agent_id": TARGET.agent_id, "fa_team_id": "team-one"}}),
])
def test_stored_background_request_keeps_exact_headers_and_body_session(mode, previous, thread):
    calls, permissions = [], []

    def server(request):
        assert permissions == [True]
        calls.append(request)
        return httpx.Response(200, json={
            "object": "response", "id": "resp-platform", "status": "queued",
            "agent_session_id": TARGET.session_id,
        })

    native = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(server))
    try:
        client = FoundryResponsesClient(native, team_id="team-one", conversation_mode=mode)
        result = client.start(
            NativeResponseHeaders(request_id="original-rid", conversation_id="conv-original", submit_digest="a" * 64),
            "原始訊息 🎯", previous_response_id=previous, before_send=lambda: permissions.append(True),
        )
        assert result.id == "resp-platform" and result.status == "queued" and len(calls) == 1
        request = calls[0]
        assert request.method == "POST"
        assert request.url.path == "/api/projects/project/agents/checksum-agent/endpoint/protocols/openai/responses"
        assert dict(request.url.params) == {"api-version": "v1"}
        assert json.loads(request.content) == {
            "input": "原始訊息 🎯", "background": True, "store": True, "stream": False,
            "agent_session_id": TARGET.session_id, **thread,
        }
        assert {key: value for key, value in request.headers.items() if key.startswith("x-")} == {
            "x-client-fa-rid": "original-rid", "x-client-fa-cid": "conv-original",
            "x-client-fa-submit-digest": "a" * 64,
        }
        assert request.headers["Authorization"] == "Bearer synthetic-test-token-never-persist"
        assert "extra_body" not in json.loads(request.content)
    finally:
        native.close()


def test_conversation_creation_and_scoped_history_use_only_the_platform_and_original_mi():
    calls = []
    metadata = {"fa_agent_id": TARGET.agent_id, "fa_team_id": "team-one"}

    def server(request):
        calls.append(request)
        if request.url.path.endswith("/conversations"):
            assert request.method == "POST"
            assert json.loads(request.content) == {"metadata": metadata}
            return httpx.Response(200, json={"object": "conversation", "id": "conv-platform", "metadata": metadata})
        if request.url.path.endswith("/conversations/conv-platform"):
            assert request.method == "GET" and not request.content
            return httpx.Response(200, json={"object": "conversation", "id": "conv-platform", "metadata": metadata})
        assert request.url.path.endswith("/conversations/conv-platform/items") and request.method == "GET"
        assert dict(request.url.params) == {"api-version": "v1", "limit": "2", "order": "asc", "after": "msg-before"}
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": "msg-user", "type": "message", "role": "user", "content": [{"type": "input_text", "text": "原問題"}]},
            {"id": "msg-answer", "type": "message", "role": "assistant", "phase": "final_answer",
             "content": [{"type": "output_text", "text": "原答案"}]},
        ], "has_more": True, "first_id": "msg-user", "last_id": "msg-answer"})

    native = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(server))
    try:
        client = FoundryResponsesClient(native, team_id="team-one", conversation_mode="conversation")
        cid = client.create_conversation(before_send=lambda: None)
        assert cid == "conv-platform"
        page = client.conversation_items(cid, after="msg-before", limit=2)
        assert page["last_id"] == "msg-answer" and page["has_more"] is True
        assert len(calls) == 3
        assert all("/invocations" not in call.url.path and "/endpoint/sessions/" not in call.url.path for call in calls)
    finally:
        native.close()


@pytest.mark.parametrize("changed", [
    {"fa_agent_id": "another-agent", "fa_team_id": "team-one"},
    {"fa_agent_id": TARGET.agent_id, "fa_team_id": "another-team"},
    {},
])
def test_history_refuses_scope_mismatch_before_reading_items(changed):
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(200, json={"object": "conversation", "id": "conv-platform", "metadata": changed})

    native = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(server))
    try:
        with pytest.raises(Problem) as denied:
            FoundryResponsesClient(native, team_id="team-one", conversation_mode="conversation").conversation_items(
                "conv-platform", limit=20)
        assert denied.value.status == 403 and denied.value.body["error"]["code"] == "NATIVE_STATE_SCOPE"
        assert len(calls) == 1 and calls[0].url.path.endswith("/conversations/conv-platform")
    finally:
        native.close()


def test_chain_history_and_cancel_address_the_original_response_without_a_vm_read():
    calls = []
    scope = {"fa_agent_id": TARGET.agent_id, "fa_team_id": "team-one"}

    def server(request):
        calls.append(request)
        if request.url.path.endswith("/responses/resp-original/input_items"):
            return httpx.Response(200, json={
                "object": "list", "data": [], "has_more": False, "first_id": None, "last_id": None})
        if request.method == "POST":
            assert request.url.path.endswith("/responses/resp-original/cancel")
            return httpx.Response(200, json={"object": "response", "id": "resp-original", "status": "cancelled"})
        assert request.url.path.endswith("/responses/resp-original")
        return httpx.Response(200, json={
            "object": "response", "id": "resp-original", "status": "completed", "metadata": scope,
            "previous_response_id": "resp-previous", "output": [],
        })

    native = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(server))
    try:
        client = FoundryResponsesClient(native, team_id="team-one", conversation_mode="previous_response_id")
        response, page = client.response_items("resp-original")
        assert response["previous_response_id"] == "resp-previous" and page["has_more"] is False
        cancelled = client.cancel("resp-original", before_send=lambda: None)
        assert cancelled.id == "resp-original" and cancelled.status == "cancelled"
        assert [call.method for call in calls] == ["GET", "GET", "POST"]
        assert all("/invocations" not in call.url.path and "/sessions/" not in call.url.path for call in calls)
    finally:
        native.close()


@pytest.mark.parametrize("operation", ["create", "history", "cancel"])
@pytest.mark.parametrize("failure", [502, "timeout"])
def test_other_platform_operations_keep_their_original_unknown_classification(operation, failure):
    calls = []

    def server(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("OFFLINE missing response", request=request)
        return httpx.Response(failure, json={"error": {"code": "unconfirmed"}})

    native = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(server))
    try:
        client = FoundryResponsesClient(native, team_id="team-one", conversation_mode="conversation")
        with pytest.raises(Problem) as unknown:
            if operation == "create":
                client.create_conversation(before_send=lambda: None)
            elif operation == "history":
                client.history_record("conversation", "conv-original")
            else:
                client.cancel("resp-original", before_send=lambda: None)
        assert unknown.value.status == 503 and unknown.value.body["error"]["code"] == "NATIVE_TURN_UNKNOWN"
        assert len(calls) == 1
    finally:
        native.close()


@pytest.mark.parametrize("status,service_code,expected", [
    (400, "invalid_request_error", (503, "NATIVE_RESPONSE_UNCONFIRMED")),
    (404, "not_found", (503, "NATIVE_RESPONSE_UNCONFIRMED")),
    (403, "permission_denied", (403, "NATIVE_TURNS_DISABLED")),
    (409, "conversation_locked", (409, "NATIVE_TURN_BUSY")),
    (502, "transient", (503, "NATIVE_RESPONSE_UNCONFIRMED")),
    (307, "redirect", (503, "NATIVE_RESPONSE_UNCONFIRMED")),
])
def test_unknown_failures_and_redirects_never_retry_or_prove_noncreation(status, service_code, expected, caplog):
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"code": service_code, "message": "PRIVATE-UPSTREAM"}},
                              headers={"Location": "https://untrusted.invalid", "x-request-id": "PRIVATE-CORRELATION"})

    native = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(server))
    try:
        with pytest.raises(Problem) as failed:
            FoundryResponsesClient(native, team_id="team-one", conversation_mode="conversation").start(
                NativeResponseHeaders(request_id="original-rid", conversation_id="conv-original", submit_digest="b" * 64),
                "original", before_send=lambda: None,
            )
        assert (failed.value.status, failed.value.body["error"]["code"]) == expected
        assert len(calls) == 1 and "PRIVATE" not in str(failed.value.body) and "PRIVATE" not in caplog.text
    finally:
        native.close()


def test_lost_response_ack_is_one_request_and_preserves_the_original_scope(caplog):
    calls = []

    def server(request):
        calls.append(request)
        raise httpx.ReadTimeout("PRIVATE-TOKEN", request=request)

    native = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(server))
    try:
        with pytest.raises(Problem) as failed:
            FoundryResponsesClient(native, team_id="team-one", conversation_mode="conversation").start(
                NativeResponseHeaders(request_id="original-rid", conversation_id="conv-original", submit_digest="a" * 64),
                "original", before_send=lambda: None,
            )
        assert failed.value.body["error"]["code"] == "NATIVE_RESPONSE_UNCONFIRMED"
        assert len(calls) == 1 and "PRIVATE" not in caplog.text
    finally:
        native.close()

def _event(value):
    return ("data: " + json.dumps(value) + "\n\n").encode()


CREATED = {"type": "response.created", "sequence_number": 0, "response": {
    "object": "response", "id": "resp-platform", "status": "queued", "agent_session_id": TARGET.session_id}}


def _streamed_start(body, status=200, content_type="text/event-stream"):
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(status, headers={"content-type": content_type}, content=body)

    native = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(server))
    try:
        return calls, FoundryResponsesClient(native, team_id="team-one", conversation_mode="conversation").start(
            NativeResponseHeaders(request_id="original-rid", conversation_id="conv-original", submit_digest="a" * 64),
            "original", before_send=lambda: None, stream=True)
    finally:
        native.close()


def test_streamed_owner_send_acknowledges_on_created_and_stops_reading_the_turn():
    """RTT B: the background stream's first event is the ACK; closing it never cancels a background turn."""
    read_after_ack = []

    def body():
        yield b": keep-alive\n\n"
        yield _event(CREATED)
        read_after_ack.append(True)
        yield _event({"type": "response.in_progress", "sequence_number": 1})

    calls, result = _streamed_start(body())
    assert result.id == "resp-platform" and result.status == "queued" and len(calls) == 1
    assert json.loads(calls[0].content)["stream"] is True and calls[0].headers["accept"] == "text/event-stream"
    assert read_after_ack == []


@pytest.mark.parametrize("body", [
    [_event({"type": "error", "sequence_number": 0, "message": "PRIVATE-UPSTREAM"})],
    [b": keep-alive\n\n"],
    [b"data: {not json\n\n"],
    [_event({**CREATED, "response": {**CREATED["response"], "status": "invented"}})],
])
def test_streamed_send_without_a_valid_created_event_is_unconfirmed_never_retried(body, caplog):
    with pytest.raises(Problem) as failed:
        _streamed_start(iter(body))
    assert failed.value.body["error"]["code"] in {"NATIVE_RESPONSE_UNCONFIRMED", "NATIVE_STATE_INVALID"}
    assert "PRIVATE" not in str(failed.value.body) and "PRIVATE" not in caplog.text


def test_streamed_send_keeps_pre_stream_refusals_and_a_json_acknowledgment():
    with pytest.raises(Problem) as locked:
        _streamed_start(json.dumps({"error": {"code": "conversation_locked"}}).encode(), status=409,
                        content_type="application/json")
    assert (locked.value.status, locked.value.body["error"]["code"]) == (409, "NATIVE_TURN_BUSY")
    _, result = _streamed_start(json.dumps(CREATED["response"]).encode(), content_type="application/json")
    assert result.id == "resp-platform"


def test_stream_source_replays_only_the_named_response_with_the_original_mi():
    native = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(lambda request: None))
    try:
        client = FoundryResponsesClient(native, team_id="team-one", conversation_mode="conversation")
        url, params, headers = client.stream_source("resp-original", 7)
        assert url.endswith("/agents/checksum-agent/endpoint/protocols/openai/responses/resp-original")
        assert params == {"api-version": "v1", "stream": "true", "starting_after": "7"}
        assert headers["Accept"] == "text/event-stream" and headers["Authorization"].startswith("Bearer ")
        assert client.stream_source("resp-original")[1] == {"api-version": "v1", "stream": "true"}
        for invalid in (-1, True, "7"):
            with pytest.raises(Problem) as refused:
                client.stream_source("resp-original", invalid)
            assert refused.value.body["error"]["code"] == "NATIVE_REQUEST_INVALID"
    finally:
        native.close()
