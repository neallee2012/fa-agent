"""Offline reproduction of the live unknown response's missing failure-origin evidence."""

import json
import httpx
import pytest

from test_native_turn_api import native_api, PREFIX
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from shared_context.behavior_http_diagnostics import native_response_diagnostics
from shared_context.behavior_native_turn_wire import NativeRead, NativeSessionRead
from test_behavior_foundry import Credential, TARGET
from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.errors import Problem


@pytest.mark.parametrize("failure", [401, 404, 503, "ReadTimeout"])
def test_unknown_native_reply_retains_http_or_transport_origin_in_logs(native_api, caplog, failure):
    case = native_api
    calls = []

    def failed(request):
        calls.append(request)
        if failure == "ReadTimeout":
            raise httpx.ReadTimeout("private synthetic failure text", request=request)
        return httpx.Response(
            failure, headers={"content-type": "application/json", "x-platform-error-source": "System",
                              "x-request-id": "12345678-1234-1234-1234-123456789abc"},
            json={"error": {"code": "OFFLINE_PLATFORM_ERROR", "message": "private synthetic failure text"}},
        )

    original = case.foundry.http
    try:
        with httpx.Client(transport=httpx.MockTransport(failed)) as transport:
            case.foundry.http = transport
            response = case.client.get(PREFIX + "/requests/original-rid", headers=case.headers)
    finally:
        case.foundry.http = original
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "FOUNDRY_INVOCATION_UNKNOWN"
    assert len(calls) == 1
    assert "private synthetic" not in response.text
    entries = [record.getMessage() for record in caplog.records
               if "NATIVE_INVOCATION_FAILURE" in record.getMessage()]
    assert len(entries) == 1, (
        f"API={response.status_code}; injected upstream={failure}; "
        "the retained log cannot distinguish an HTTP refusal from a transport timeout"
    )
    details = json.loads(entries[0].removeprefix("NATIVE_INVOCATION_FAILURE "))
    assert details["status"] == (None if isinstance(failure, str) else failure)
    assert details["exception_type"] == (failure if isinstance(failure, str) else None)
    assert details["phase"] == ("request" if isinstance(failure, str) else "response_status")
    assert (details["agent_id"], details["session_id"]) == (
        case.foundry.target.agent_id, case.foundry.target.session_id)
    assert isinstance(details["elapsed_ms"], int) and details["elapsed_ms"] >= 0
    if isinstance(failure, int):
        assert details["request_id"] == "12345678-1234-1234-1234-123456789abc"
        assert details["platform_error_source"] == "System"
    assert "private synthetic" not in entries[0]


def test_response_diagnostics_reject_duplicate_unknown_and_sensitive_headers():
    headers = httpx.Headers([
        ("x-request-id", "12345678-1234-1234-1234-123456789abc"),
        ("x-request-id", "abcdefab-cdef-abcd-efab-cdefabcdefab"),
        ("x-ms-request-id", "private-unstructured-value"),
        ("apim-request-id", "0123456789abcdef0123456789abcdef"),
        ("x-platform-error-source", "private-upstream-text"),
        ("Authorization", "Bearer private-header-token"),
        ("Set-Cookie", "private-cookie"),
        ("x-agent-foundry-call-id", "private-caller-context"),
    ])
    assert native_response_diagnostics(headers) == {"apim_request_id": "0123456789abcdef0123456789abcdef"}


def test_body_timeout_keeps_observed_status_and_does_not_retry(caplog):
    calls = []

    class Unfinished(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"private":"must-not-log"'
            raise httpx.ReadTimeout("private timeout message")

    def transport(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=Unfinished())

    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(Problem) as error:
            client.invoke_native(NativeRead(request_id="original"))
    finally:
        client.close()
    assert error.value.status == 503 and error.value.body["error"]["code"] == "FOUNDRY_INVOCATION_UNKNOWN"
    event, = [json.loads(record.getMessage().removeprefix("NATIVE_INVOCATION_FAILURE "))
              for record in caplog.records if record.getMessage().startswith("NATIVE_INVOCATION_FAILURE ")]
    assert (event["phase"], event["status"], event["exception_type"]) == ("response_body", 200, "ReadTimeout")
    assert len(calls) == 1 and "must-not-log" not in json.dumps(event) and "private timeout" not in json.dumps(event)


@pytest.mark.parametrize("command,name", [
    (NativeRead(request_id="not-yet-sent"), "NATIVE_RID_ABSENT"),
    (NativeSessionRead(), "NATIVE_INVOCATION_FAILURE"),
])
def test_an_absent_rid_read_is_named_as_an_answer_not_a_failure(caplog, command, name):
    """Before a first send the API reads `rid:<RID>`; a 404 there is the expected answer, not an outage."""
    def transport(request):
        return httpx.Response(404, json={"error": {"code": "NATIVE_RID_MISSING", "message": "private detail"}})

    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(Problem) as error:
            client.invoke_native(command)
    finally:
        client.close()
    assert error.value.status == 404 and error.value.body["error"]["code"] == "NATIVE_RID_MISSING"
    messages = [record.getMessage() for record in caplog.records
                if record.getMessage().startswith(("NATIVE_RID_ABSENT ", "NATIVE_INVOCATION_FAILURE "))]
    assert len(messages) == 1 and messages[0].startswith(name + " ")
    event = json.loads(messages[0].removeprefix(name + " "))
    assert (event["status"], event["upstream_error_code"]) == (404, "NATIVE_RID_MISSING")
    assert event["native_operation"] == command.operation and "private detail" not in messages[0]


def test_successful_native_transport_does_not_emit_failure_or_private_material(caplog):
    def transport(request):
        return httpx.Response(200, json={"public": "original response"}, headers={
            "x-agent-session-id": TARGET.session_id, "x-agent-invocation-id": "platform-generated",
        })
    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(transport))
    try:
        response = client.invoke_native(NativeRead(request_id="original"))
    finally:
        client.close()
    assert response.status_code == 200 and json.loads(response.body) == {"public": "original response"}
    assert not any("NATIVE_INVOCATION_FAILURE" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("code,logged", [
    ("NATIVE_SESSION_CHANGED", "NATIVE_SESSION_CHANGED"),
    ("AgentSessionVersionMismatch", "AgentSessionVersionMismatch"),
    ("private secret\nhttps://example.invalid", "UNCLASSIFIED"),
    ("a" * 64, "UNCLASSIFIED"),
])
def test_source_refusal_logs_only_safe_error_code_and_actual_control_operation(caplog, code, logged):
    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(lambda request:
        httpx.Response(409, json={"error": {"code": code, "message": "PRIVATE BODY MUST NOT BE LOGGED"}})))
    try:
        with pytest.raises(Problem):
            client.invoke_native(NativeSessionRead())
    finally:
        client.close()
    event, = [json.loads(record.getMessage().removeprefix("NATIVE_INVOCATION_FAILURE "))
              for record in caplog.records if record.getMessage().startswith("NATIVE_INVOCATION_FAILURE ")]
    assert event["upstream_error_code"] == logged
    assert event["native_operation"] == "session_read" and event["native_stage"] is None
    assert event["expected_agent_version"] == TARGET.agent_version
    assert "PRIVATE BODY" not in json.dumps(event)


@pytest.mark.parametrize("valid", [True, False])
def test_file_check_diagnostic_only_carries_a_valid_category_and_short_ids(caplog, valid):
    from shared_context.behavior_native_turn_wire import NativeSessionFilesOmission, NativeSessionFollow

    detail = {"category": "active_waiting", "request_id_prefix": "abcd1234",
              "conversation_id_prefix": "conv1234"}
    if not valid:
        detail["request_id_prefix"] = "private-full-request-and-content"
    command = NativeSessionFilesOmission(
        request_id="original-confirmation", stage="check", missing="source",
        follow=NativeSessionFollow(request_id="original-follow", expected_session_id="original-source",
                                   expected_etag="original-etag", stage="prepare"))
    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(lambda _request:
        httpx.Response(409, json={"error": {"code": "NATIVE_FILES_CONFIRMATION_REQUIRED",
                                          "files_check": detail, "message": "private explanation"}})))
    try:
        with pytest.raises(Problem) as caught:
            client.invoke_native(command)
    finally:
        client.close()
    error = caught.value
    event, = [json.loads(record.getMessage().removeprefix("NATIVE_INVOCATION_FAILURE "))
              for record in caplog.records if record.getMessage().startswith("NATIVE_INVOCATION_FAILURE ")]
    if valid:
        assert error.status == 409 and error.body["error"]["files_check"] == detail
        assert event["files_check"] == detail
    else:
        assert error.status == 502 and error.body["error"]["code"] == "NATIVE_STATE_INVALID"
        assert "files_check" not in event
    assert "private" not in json.dumps(event) and "private" not in str(error.body)


@pytest.mark.parametrize("status,code,cursor_source", [
    (True, "InvalidCursor", "page_last_id"), (400, "private_secret_code", "none"),
    (700, "BadRequest", None), (400, "bad_request", "private_secret_cursor"),
])
def test_file_check_rejects_unallowlisted_backend_diagnostics_before_api_logging(caplog, status, code, cursor_source):
    from shared_context.behavior_native_turn_wire import NativeSessionFilesOmission, NativeSessionFollow

    command = NativeSessionFilesOmission(
        request_id="original-confirmation", stage="check", missing="source",
        follow=NativeSessionFollow(request_id="original-follow", expected_session_id="original-source",
                                   expected_etag="etag", stage="prepare"))
    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(lambda _request:
        httpx.Response(503, json={"error": {"code": "NATIVE_STATE_STORE_UNKNOWN", "files_check": {
            "category": "storage_read_failed", "stage": "list", "key_category": "receipt",
            "backend_status": status, "backend_code": code, "cursor_source": cursor_source}}})))
    try:
        with pytest.raises(Problem) as caught:
            client.invoke_native(command)
    finally:
        client.close()
    assert caught.value.status == 502 and caught.value.body["error"]["code"] == "NATIVE_STATE_INVALID"
    assert not any("private_secret" in record.getMessage() for record in caplog.records)
