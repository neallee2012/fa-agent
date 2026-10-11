"""HITL HTTP failures keep their original behavior and emit only four safe diagnostic fields."""

import asyncio
import json

import httpx
import pytest
from azure.core.exceptions import AzureError, ClientAuthenticationError, ServiceRequestError

from shared_context.errors import Problem
from shared_context.native_hitl_runtime import NativeHitlClient
from shared_context.native_hitl_wire import (
    NATIVE_HITL_HTTP_BYTES, NativeHitlControlReceipt, NativeHitlEnd, NativeHitlRules, NativeHitlTurn,
)

PREFIX = "NATIVE_HITL_AUTHORITY_FAILURE "
CANARY = "OFFLINE-sensitive-canary-do-not-log"


def diagnostics(caplog):
    records = [
        record for record in caplog.records
        if record.name == "shared_context.native_hitl_runtime" and record.getMessage().startswith(PREFIX)
    ]
    assert all(record.exc_info is None and record.stack_info is None for record in records)
    messages = [record.getMessage() for record in records]
    assert all(CANARY not in message for message in messages)
    values = [json.loads(message[len(PREFIX):]) for message in messages]
    assert all(set(value) == {"operation", "status", "code", "exception_class"} for value in values)
    return values


def test_invalid_success_response_records_real_status_not_wrapped_status(caplog):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"private": CANARY})

    client = NativeHitlClient(
        "https://offline.invalid", lambda: CANARY, transport=httpx.MockTransport(transport))
    with pytest.raises(Problem) as failure:
        asyncio.run(client.rules())
    assert failure.value.status == 503
    assert failure.value.body["error"]["code"] == "NATIVE_HITL_AUTHORITY_UNAVAILABLE"
    assert len(calls) == 1
    assert diagnostics(caplog) == [{
        "operation": "rules_read", "status": 200, "code": "NATIVE_HITL_AUTHORITY_UNAVAILABLE",
        "exception_class": "ValidationError",
    }]


@pytest.mark.parametrize("status,body,returned_code,logged_code,exception", [
    (403, {"error": {"code": "BEHAVIOR_ACCESS_DENIED", "message": CANARY}},
     "NATIVE_HITL_AUTHORITY_UNAVAILABLE", "BEHAVIOR_ACCESS_DENIED", "Problem"),
    (401, {"error": {"code": "BEHAVIOR_UNAUTHENTICATED", "message": CANARY}},
     "NATIVE_HITL_AUTHORITY_UNAVAILABLE", "BEHAVIOR_UNAUTHENTICATED", "Problem"),
    (409, {"error": {"code": "NATIVE_HITL_RECORD_INVALID", "message": CANARY}},
     "NATIVE_HITL_RECORD_INVALID", "NATIVE_HITL_RECORD_INVALID", "Problem"),
    (410, {"error": {"code": "NATIVE_HITL_EXPIRED", "message": CANARY}},
     "NATIVE_HITL_EXPIRED", "NATIVE_HITL_EXPIRED", "Problem"),
    (412, {"error": {"code": "BEHAVIOR_CONFLICT", "message": CANARY}},
     "BEHAVIOR_CONFLICT", "BEHAVIOR_CONFLICT", "Problem"),
    (503, {"error": {"code": "BEHAVIOR_COMMIT_UNKNOWN", "message": CANARY}},
     "BEHAVIOR_COMMIT_UNKNOWN", "BEHAVIOR_COMMIT_UNKNOWN", "Problem"),
    (429, {"error": {"code": "BEHAVIOR_CAPACITY_EXHAUSTED", "message": CANARY}},
     "NATIVE_HITL_AUTHORITY_UNAVAILABLE", "BEHAVIOR_CAPACITY_EXHAUSTED", "Problem"),
    (502, {"error": {"code": CANARY, "message": CANARY}},
     "NATIVE_HITL_AUTHORITY_UNAVAILABLE", "UNCLASSIFIED", "Problem"),
    (503, CANARY, "NATIVE_HITL_AUTHORITY_UNAVAILABLE", "UNCLASSIFIED", "JSONDecodeError"),
])
def test_non_success_status_and_codes_are_not_reclassified_or_retried(
        caplog, status, body, returned_code, logged_code, exception):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(status, content=body if isinstance(body, str) else json.dumps(body))

    client = NativeHitlClient("https://offline.invalid", lambda: CANARY, transport=httpx.MockTransport(transport))
    with pytest.raises(Problem) as failure:
        asyncio.run(client.read(CANARY))
    assert (failure.value.status, failure.value.body["error"]["code"]) == (status, returned_code)
    assert len(calls) == 1
    assert diagnostics(caplog) == [{
        "operation": "pending_read", "status": status, "code": logged_code, "exception_class": exception,
    }]


@pytest.mark.parametrize("body,exception", [
    (CANARY.encode(), "ValidationError"),
    (b'{"private":"' + CANARY.encode() + b'","padding":"' + b"x" * NATIVE_HITL_HTTP_BYTES + b'"}', "Problem"),
], ids=["invalid-json", "oversize"])
def test_invalid_json_and_oversize_keep_http_200(caplog, body, exception):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, content=body)

    client = NativeHitlClient("https://offline.invalid", lambda: CANARY, transport=httpx.MockTransport(transport))
    with pytest.raises(Problem) as failure:
        asyncio.run(client.rules())
    assert (failure.value.status, failure.value.body["error"]["code"]) == (503, "NATIVE_HITL_AUTHORITY_UNAVAILABLE")
    assert len(calls) == 1
    assert diagnostics(caplog) == [{
        "operation": "rules_read", "status": 200, "code": "NATIVE_HITL_AUTHORITY_UNAVAILABLE",
        "exception_class": exception,
    }]


@pytest.mark.parametrize("token", [None, "", 123, CANARY + "\r\n"])
def test_invalid_token_has_no_http_status_or_request(caplog, token):
    client = NativeHitlClient("https://offline.invalid", lambda: token, transport=httpx.MockTransport(
        lambda _: pytest.fail("invalid credentials must not send HTTP")))
    with pytest.raises(Problem) as failure:
        asyncio.run(client.rules())
    assert (failure.value.status, failure.value.body["error"]["code"]) == (503, "NATIVE_HITL_AUTHORITY_UNAVAILABLE")
    assert diagnostics(caplog) == [{
        "operation": "rules_read", "status": None, "code": "NATIVE_HITL_AUTHORITY_UNAVAILABLE",
        "exception_class": "Problem",
    }]


@pytest.mark.parametrize("error,exception", [
    (AzureError(CANARY), "AzureError"),
    (ClientAuthenticationError(CANARY), "ClientAuthenticationError"),
    (ServiceRequestError(CANARY), "ServiceRequestError"),
    (OSError(CANARY), "OSError"),
    (ValueError(CANARY), "ValueError"),
    (type(CANARY, (ValueError,), {})(CANARY), "UNCLASSIFIED"),
])
def test_token_exception_logs_only_a_known_class_before_http(caplog, error, exception):
    async def token():
        raise error

    client = NativeHitlClient("https://offline.invalid", token, transport=httpx.MockTransport(
        lambda _: pytest.fail("token exception must not send HTTP")))
    with pytest.raises(Problem) as failure:
        asyncio.run(client.rules())
    assert (failure.value.status, failure.value.body["error"]["code"]) == (503, "NATIVE_HITL_AUTHORITY_UNAVAILABLE")
    assert diagnostics(caplog) == [{
        "operation": "rules_read", "status": None, "code": "NATIVE_HITL_AUTHORITY_UNAVAILABLE",
        "exception_class": exception,
    }]


@pytest.mark.parametrize("after_headers", [False, True])
def test_transport_failure_distinguishes_no_response_from_http_200(caplog, after_headers):
    calls = []

    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"private":"' + CANARY.encode()
            raise httpx.ReadError(CANARY)

    def transport(request):
        calls.append(request)
        if after_headers:
            return httpx.Response(200, stream=BrokenBody())
        raise httpx.ConnectError(CANARY, request=request)

    client = NativeHitlClient("https://offline.invalid", lambda: CANARY, transport=httpx.MockTransport(transport))
    with pytest.raises(Problem) as failure:
        asyncio.run(client.rules())
    assert (failure.value.status, failure.value.body["error"]["code"]) == (503, "NATIVE_HITL_AUTHORITY_UNAVAILABLE")
    assert len(calls) == 1
    assert diagnostics(caplog) == [{
        "operation": "rules_read", "status": 200 if after_headers else None,
        "code": "NATIVE_HITL_AUTHORITY_UNAVAILABLE",
        "exception_class": "ReadError" if after_headers else "ConnectError",
    }]


@pytest.mark.parametrize("reconcile", [False, True])
@pytest.mark.parametrize("field", ["request_id", "kind"])
def test_mismatched_write_receipt_logs_http_200_without_an_extra_request(caplog, reconcile, field):
    command = NativeHitlEnd(
        request_id="original-end", turn=NativeHitlTurn(
            agent_id="offline-agent", conversation_id="offline-cid", request_id="offline-rid"))
    calls = []

    def transport(request):
        calls.append(request)
        if reconcile and len(calls) == 1:
            return httpx.Response(503, json={"error": {"code": "BEHAVIOR_COMMIT_UNKNOWN", "message": CANARY}})
        return httpx.Response(200, json={
            "control_request_id": "offline-control", "control_request_digest": "a" * 64,
            "result": {"request_id": command.request_id, "kind": command.kind, field: CANARY},
        })

    client = NativeHitlClient("https://offline.invalid", lambda: CANARY, transport=httpx.MockTransport(transport))
    with pytest.raises(Problem) as failure:
        asyncio.run(client.write("/end", command))
    assert (failure.value.status, failure.value.body["error"]["code"]) == (503, "NATIVE_HITL_AUTHORITY_UNAVAILABLE")
    assert len(calls) == (2 if reconcile else 1)
    assert all(request.content == command.model_dump_json().encode("utf-8") for request in calls)
    entries = diagnostics(caplog)
    assert len(entries) == len(calls)
    assert entries[-1] == {
        "operation": "end_reconcile" if reconcile else "end", "status": 200,
        "code": "NATIVE_HITL_AUTHORITY_UNAVAILABLE", "exception_class": "Problem",
    }
    if reconcile:
        assert calls[1].url.path.endswith("/end/reconcile")
        assert entries[0]["code"] == "BEHAVIOR_COMMIT_UNKNOWN" and entries[0]["status"] == 503


@pytest.mark.parametrize("path,operation", [
    ("/rules", "rules_write"), ("/pending", "pending_create"), ("/decisions", "decision_write"),
    ("/observations", "observation_write"), ("/authorize", "authorize"), ("/end", "end"),
    ("/questions", "question_create"), ("/answers", "answer_write"),
    ("/turn-pending", "turn_pending_read"), ("/turn-questions", "turn_questions_read"),
    ("/pending/reconcile", "pending_create_reconcile"), ("/" + CANARY, "unknown"),
])
def test_write_operations_are_fixed_names_not_body_or_path_text(caplog, path, operation):
    command = NativeHitlEnd(
        request_id=CANARY, turn=NativeHitlTurn(agent_id="offline", conversation_id="offline", request_id="offline"))
    client = NativeHitlClient("https://offline.invalid", lambda: CANARY, transport=httpx.MockTransport(
        lambda _: httpx.Response(403, json={"error": {"code": "NATIVE_HITL_SCOPE_DENIED"}})))
    with pytest.raises(Problem):
        asyncio.run(client._request(path, NativeHitlControlReceipt, command))
    assert diagnostics(caplog) == [{
        "operation": operation, "status": 403, "code": "NATIVE_HITL_SCOPE_DENIED", "exception_class": "Problem",
    }]


def test_success_has_no_failure_log(caplog):
    rules = NativeHitlRules(team_id="offline", revision=0, etag=None)
    client = NativeHitlClient("https://offline.invalid", lambda: CANARY, transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=rules.model_dump_json())))
    assert asyncio.run(client.rules()) == rules
    assert diagnostics(caplog) == []


@pytest.mark.parametrize("path,operation", [
    ("/questions/" + CANARY, "question_read"), ("/" + CANARY, "unknown"),
])
def test_other_read_operations_never_log_the_path(caplog, path, operation):
    client = NativeHitlClient("https://offline.invalid", lambda: CANARY, transport=httpx.MockTransport(
        lambda _: httpx.Response(401, json={"error": {"code": "BEHAVIOR_UNAUTHENTICATED"}})))
    with pytest.raises(Problem):
        asyncio.run(client._request(path, NativeHitlRules))
    assert diagnostics(caplog) == [{
        "operation": operation, "status": 401, "code": "BEHAVIOR_UNAUTHENTICATED", "exception_class": "Problem",
    }]
