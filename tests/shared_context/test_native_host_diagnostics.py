"""Fixed local host routes; diagnostics do not run a model or change a response."""

import json
import hashlib

from shared_context.behavior_native_turn_wire import NativeRead, NATIVE_STATE_PROFILE, NATIVE_TURN_HEADER
from shared_context.behavior_http_diagnostics import native_correlation_id
from test_native_turns import harness, local_env, native_retry_policy, IDENTITY, accept_without_chain


def events(caplog, prefix):
    return [json.loads(record.getMessage().removeprefix(prefix + " "))
            for record in caplog.records if record.getMessage().startswith(prefix + " ")]


def test_native_missing_rid_has_dispatch_entry_and_return_logs(harness, caplog):
    response = harness.invoke(NativeRead(request_id="original-rid"),
                              call_id="private-caller-context-must-not-be-logged")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NATIVE_RID_MISSING"
    dispatch, = events(caplog, "FA_HOSTED_DISPATCH")
    assert dispatch["lane"] == "native" and dispatch["native_header_count"] == 1
    start, finish = events(caplog, "FA_NATIVE_HTTP")
    assert start["phase"] == "entered" and start["operation"] is None
    assert finish["phase"] == "returned" and finish["operation"] == "read"
    assert finish["status"] == 404 and finish["code"] == "NATIVE_RID_MISSING"
    assert finish["request_id_sha256"] == hashlib.sha256(b"original-rid").hexdigest()
    assert start["invocation_id"] == finish["invocation_id"]
    assert finish["session_id"] == IDENTITY.session_id
    assert "private-caller-context" not in json.dumps((dispatch, start, finish))
    assert not harness.sdk.calls


def test_missing_native_marker_records_legacy_route_without_changing_it(harness, caplog):
    response = harness.client.post("/invocations", json={"operation": "read", "request_id": "original"},
        headers={"x-agent-session-id": IDENTITY.session_id, "x-agent-invocation-id": "platform-original"})
    assert response.status_code == 422
    dispatch, = events(caplog, "FA_HOSTED_DISPATCH")
    assert dispatch["lane"] == "legacy" and dispatch["native_header_count"] == 0
    assert events(caplog, "FA_NATIVE_HTTP") == [] and not harness.sdk.calls


def test_invalid_native_body_is_not_in_diagnostics(harness, caplog):
    response = harness.client.post("/invocations",
        json={"private-message": "private-body-must-not-be-logged"},
        headers={NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE, "x-agent-session-id": IDENTITY.session_id,
                 "x-agent-invocation-id": "platform-original"})
    assert response.status_code == 422
    start, finish = events(caplog, "FA_NATIVE_HTTP")
    assert start["operation"] is None and finish["operation"] is None
    assert finish["code"] == "NATIVE_REQUEST_INVALID" and finish["status"] == 422
    assert "private-body" not in json.dumps((start, finish)) and not harness.sdk.calls


def test_successful_read_logs_only_metadata_and_keeps_original_receipt(harness, caplog):
    accepted = accept_without_chain(harness)
    caplog.clear()
    response = harness.invoke(NativeRead(request_id="rid-1"))
    assert response.status_code == 200 and response.json() == accepted.value.receipt.model_dump(mode="json")
    start, finish = events(caplog, "FA_NATIVE_HTTP")
    assert finish["status"] == 200 and finish["code"] is None and finish["phase"] == "returned"
    captured = json.dumps((start, finish))
    assert accepted.value.receipt.command.content not in captured
    assert accepted.value.receipt.capture.role_prompt.text not in captured
    assert not harness.sdk.calls


def test_unhandled_exception_is_categorized_and_still_reaches_sdk_error_path(harness, caplog, monkeypatch):
    async def fail(_):
        raise ValueError("private exception message must not be in FA diagnostics")
    monkeypatch.setattr(harness.runtime, "dispatch", fail)
    response = harness.invoke(NativeRead(request_id="rid-1"))
    assert response.status_code == 500
    start, failure = events(caplog, "FA_NATIVE_HTTP")
    assert failure["phase"] == "raised" and failure["status"] is None
    assert failure["exception_type"] == "ValueError"
    assert "private exception" not in json.dumps((start, failure))
    assert not harness.sdk.calls


def test_invocation_correlation_omits_unbounded_or_credential_shaped_values():
    assert native_correlation_id("platform_original-123") == "platform_original-123"
    for value in (None, ["duplicate"], "Bearer private-token", "private.header.token", "line\nbreak", "x" * 65):
        assert native_correlation_id(value) is None
