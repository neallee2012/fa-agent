"""Probe exception results distinguish unknown outcomes without private text."""

import json
from types import SimpleNamespace

from azure.core.exceptions import ClientAuthenticationError
import httpx
import pytest
from starlette.testclient import TestClient

from test_environment import private_home
from test_hosted import client


CANARY = "HOSTED_REQUEST_PRIVATE_DIAGNOSTIC_CANARY"
TOKEN = "HOSTED_SYNTHETIC_REQUEST_TOKEN"


@pytest.mark.parametrize("operation", ["api", "direct_storage", "direct_memory"])
@pytest.mark.parametrize("failure", ["authentication", "transport"])
def test_probe_exception_result_preserves_unknown_without_private_diagnostics(operation, failure):
    calls, scopes = [], []

    def remote(request):
        calls.append((request.method, request.url.host))
        raise httpx.ReadError(CANARY + " " + TOKEN, request=request)

    hosted, _ = client(remote)

    def token(scope):
        scopes.append(scope)
        if failure == "authentication":
            raise ClientAuthenticationError(CANARY)
        return SimpleNamespace(token=TOKEN)

    hosted.credential = SimpleNamespace(get_token=token)
    payload = ({"operation": "api", "method": "GET", "path": "/teams/team-one/works/work-1"}
               if operation == "api" else {"operation": operation, "team": "team-one"})
    with hosted.http:
        result = hosted.dispatch(payload)["outcome"]
    assert len(scopes) == 1 and len(calls) == (0 if failure == "authentication" else 1)
    assert result["http_status"] is None
    assert result["error"]["code"] == (
        "TOKEN_ACQUISITION_FAILED" if failure == "authentication" else "HTTP_TRANSPORT_ERROR"
    )
    assert result["error"]["type"] == ("ClientAuthenticationError" if failure == "authentication" else "ReadError")
    assert CANARY not in json.dumps(result) and TOKEN not in json.dumps(result)
    if operation != "api":
        assert result["access_denied"] is False and result["unexpected_success"] is False


def test_partial_response_read_failure_does_not_publish_partial_content():
    calls, closed = [], []

    class Partial(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"private_fragment":"HOSTED_PARTIAL_BODY_CANARY'
            raise httpx.ReadError(CANARY)

        def close(self):
            closed.append(True)

    def remote(request):
        calls.append(request.method)
        return httpx.Response(200, stream=Partial())

    hosted, credential = client(remote)
    with hosted.http:
        result = hosted.dispatch({
            "operation": "api", "method": "GET", "path": "/teams/team-one/works/work-1",
        })["outcome"]
    assert result["http_status"] is None and result["error"]["code"] == "HTTP_TRANSPORT_ERROR"
    assert "content" not in result
    assert CANARY not in json.dumps(result) and "HOSTED_PARTIAL_BODY_CANARY" not in json.dumps(result)
    assert calls == ["GET"] and closed and len(credential.scopes) == 1


def test_patch_applied_before_response_loss_remains_unknown_without_resending():
    state, calls = {"revision": 1, "progress": "before"}, []

    def remote(request):
        calls.append((request.method, request.headers["if-match"]))
        assert request.url.path == "/teams/team-one/works/work-1"
        state.update(revision=2, progress=json.loads(request.content)["progress"])
        raise httpx.ReadTimeout(CANARY, request=request)

    hosted, credential = client(remote)
    with hosted.http:
        result = hosted.dispatch({
            "operation": "api", "method": "PATCH", "path": "/teams/team-one/works/work-1",
            "etag": 'W/"original"', "body": {"progress": "after"},
        })["outcome"]
    assert result["http_status"] is None
    assert result["error"]["code"] == "HTTP_TRANSPORT_ERROR" and CANARY not in json.dumps(result)
    assert "content" not in result and calls == [("PATCH", 'W/"original"')]
    assert state == {"revision": 2, "progress": "after"} and len(credential.scopes) == 1


def test_invocation_http_200_does_not_turn_a_token_failure_into_a_denial_pass(private_home, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    import hosted_main

    hosted, credential = client(lambda _request: pytest.fail("No service request may be sent"))
    calls = []

    def unavailable(scope):
        calls.append(scope)
        raise ClientAuthenticationError(CANARY)

    credential.get_token = unavailable
    metadata = hosted_main.runtime_metadata
    monkeypatch.setattr(hosted_main, "runtime_metadata", lambda **options: metadata(private_home, **options))
    monkeypatch.setattr(hosted_main, "client", hosted)
    with hosted.http, TestClient(hosted_main.app) as invocations:
        response = invocations.post("/invocations?agent_session_id=synthetic-session-a", json={
            "operation": "direct_memory", "team": "team-one",
        })
    assert response.status_code == 200
    result = response.json()["outcome"]
    assert result["http_status"] is None and result["access_denied"] is False
    assert result["unexpected_success"] is False and result["error"]["code"] == "TOKEN_ACQUISITION_FAILED"
    assert CANARY not in response.text and calls == ["https://ai.azure.com/.default"]
