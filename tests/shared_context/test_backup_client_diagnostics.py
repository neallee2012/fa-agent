"""Backup error envelopes expose fixed diagnostics, never private transport text."""

import copy
import hashlib
import traceback
from types import SimpleNamespace

from azure.core.exceptions import ClientAuthenticationError
import httpx
import pytest
from starlette.testclient import TestClient

from shared_context.backup_client import BackupClient
from shared_context.errors import Problem
from test_environment import external_tools, private_home, request
from test_environment_backup import cloud_backup, replacement_home


CANARY = "BACKUP_PRIVATE_DIAGNOSTIC_CANARY"
TOKEN = "BACKUP_SYNTHETIC_ACCESS_TOKEN"


@pytest.mark.parametrize("action", ["backup", "restore"])
@pytest.mark.parametrize("failure", ["authentication", "transport", "remote-code"])
def test_backup_invocation_errors_do_not_echo_private_diagnostics(
        cloud_backup, private_home, external_tools, monkeypatch, action, failure):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    import hosted_main

    hosted, remote, seeded, receipt = cloud_backup
    original_objects = copy.deepcopy(remote.objects)
    retained_receipt = copy.deepcopy(receipt)
    original_files = {path.relative_to(private_home): path.read_bytes()
                      for path in private_home.rglob("*") if path.is_file()}
    if action == "restore":
        destination = replacement_home(private_home, monkeypatch)
        payload = request("restore", backup=receipt)
        session = "replacement-session"
    else:
        destination = None
        payload = request("backup", backup_id="2" * 32, checkpoint_sha256=seeded["checkpoint_sha256"])
        session = "synthetic-session-a"
    token_calls, http_calls = [], []

    def token(scope):
        token_calls.append(scope)
        if failure == "authentication":
            raise ClientAuthenticationError(CANARY)
        return SimpleNamespace(token=TOKEN)

    def fail(call):
        http_calls.append((call.method, call.url.path))
        if failure == "transport":
            raise httpx.ReadTimeout(CANARY + " " + TOKEN, request=call)
        return httpx.Response(503, json={"error": {"code": CANARY, "message": CANARY}})

    with httpx.Client(transport=httpx.MockTransport(fail)) as transport:
        monkeypatch.setattr(hosted, "credential", SimpleNamespace(get_token=token))
        monkeypatch.setattr(hosted, "http", transport)
        monkeypatch.setattr(hosted_main, "client", hosted)
        with TestClient(hosted_main.app) as invocations:
            response = invocations.post("/invocations?agent_session_id=" + session, json=payload)

    assert response.status_code == (502 if failure == "transport" else 503)
    assert CANARY not in response.text and TOKEN not in response.text
    assert response.json()["error"]["code"] == {
        "authentication": "BACKUP_AUTHENTICATION_FAILED",
        "transport": "BACKUP_TRANSPORT_FAILED",
        "remote-code": "ENVIRONMENT_BACKUP_FAILED",
    }[failure]
    assert token_calls == [hosted.api_audience + "/.default"]
    assert len(http_calls) == (0 if failure == "authentication" else 1)
    assert remote.objects == original_objects and receipt == retained_receipt
    assert {path.relative_to(private_home): path.read_bytes()
            for path in private_home.rglob("*") if path.is_file()} == original_files
    if destination is not None:
        assert list(destination.iterdir()) == []


@pytest.mark.parametrize("status,code", [
    (401, "UNAUTHENTICATED"), (403, "ACTOR_NOT_ALLOWED"), (404, "BACKUP_NOT_FOUND"),
    (409, "BACKUP_EXISTS"), (412, "ETAG_CONFLICT"), (422, "INVALID_BACKUP"),
])
def test_known_backup_failure_codes_remain_readable_without_remote_message(status, code):
    requests, scopes = [], []

    def remote(call):
        requests.append(call.method)
        return httpx.Response(status, json={"error": {"code": code, "message": CANARY, "details": CANARY}})

    def token(scope):
        scopes.append(scope)
        return SimpleNamespace(token=TOKEN)

    with httpx.Client(transport=httpx.MockTransport(remote)) as http:
        backup = BackupClient("https://backup.invalid", "api://backup-fixture", SimpleNamespace(get_token=token), http)
        with pytest.raises(Problem) as failed:
            backup.put("3" * 32, b"synthetic archive")
    assert failed.value.status == status
    assert failed.value.body["error"]["upstream_status"] == status
    assert failed.value.body["error"]["upstream_code"] == code
    assert failed.value.body["error"]["code"] == "ENVIRONMENT_BACKUP_FAILED"
    assert CANARY not in str(failed.value.body)
    assert requests == ["PUT"] and scopes == ["api://backup-fixture/.default"]


@pytest.mark.parametrize("code", [CANARY, {"detail": CANARY}, [CANARY], None, 123, True])
def test_unknown_or_malformed_upstream_codes_remain_failed_without_reflecting_values(code):
    with httpx.Client(transport=httpx.MockTransport(
        lambda _call: httpx.Response(503, json={"error": {"code": code, "message": CANARY}})
    )) as http:
        backup = BackupClient("https://backup.invalid", "api://backup-fixture",
                              SimpleNamespace(get_token=lambda _scope: SimpleNamespace(token=TOKEN)), http)
        with pytest.raises(Problem) as failed:
            backup.put("3" * 32, b"synthetic archive")
    assert failed.value.status == 503
    assert failed.value.body["error"]["upstream_code"] is None
    assert failed.value.body["error"]["upstream_status"] == 503
    assert failed.value.body["error"]["code"] == "ENVIRONMENT_BACKUP_FAILED"
    assert CANARY not in str(failed.value.body)


@pytest.mark.parametrize("failure", ["authentication", "transport"])
def test_public_backup_exception_does_not_chain_private_auth_or_transport_text(failure):
    calls = []

    def token(_scope):
        if failure == "authentication":
            raise ClientAuthenticationError(CANARY)
        return SimpleNamespace(token=TOKEN)

    def remote(call):
        calls.append(call.method)
        raise httpx.ReadError(CANARY + " " + TOKEN, request=call)

    with httpx.Client(transport=httpx.MockTransport(remote)) as http:
        backup = BackupClient("https://backup.invalid", "api://backup-fixture", SimpleNamespace(get_token=token), http)
        with pytest.raises(Problem) as failed:
            backup.put("3" * 32, b"synthetic archive")
    assert failed.value.__cause__ is None and failed.value.__suppress_context__
    text = "".join(traceback.TracebackException.from_exception(failed.value).format())
    assert CANARY not in text and TOKEN not in text
    assert calls == ([] if failure == "authentication" else ["PUT"])


def test_backup_response_loss_keeps_the_original_saved_object_without_retry(
        cloud_backup, private_home, external_tools, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    import hosted_main

    hosted, remote, seeded, receipt = cloud_backup
    before = copy.deepcopy(remote.objects)
    source_files = {path.relative_to(private_home): path.read_bytes()
                    for path in private_home.rglob("*") if path.is_file()}
    backup_id, calls, scopes = "4" * 32, [], []

    def token(scope):
        scopes.append(scope)
        return SimpleNamespace(token=TOKEN)

    def accepted_then_lost(call):
        calls.append((call.method, call.url.path, hashlib.sha256(call.content).hexdigest()))
        response = remote(call)
        assert response.status_code == 201
        raise httpx.ReadError(CANARY + " " + TOKEN, request=call)

    with httpx.Client(transport=httpx.MockTransport(accepted_then_lost)) as transport:
        monkeypatch.setattr(hosted, "credential", SimpleNamespace(get_token=token))
        monkeypatch.setattr(hosted, "http", transport)
        monkeypatch.setattr(hosted_main, "client", hosted)
        with TestClient(hosted_main.app) as invocations:
            result = invocations.post("/invocations?agent_session_id=synthetic-session-a", json=request(
                "backup", backup_id=backup_id, checkpoint_sha256=seeded["checkpoint_sha256"],
            ))
    assert result.status_code == 502 and result.json()["error"]["code"] == "BACKUP_TRANSPORT_FAILED"
    assert CANARY not in result.text and TOKEN not in result.text
    assert "outcome" not in result.json()
    assert set(remote.objects) == set(before) | {backup_id}
    assert all(remote.objects[key] == value for key, value in before.items())
    saved_bytes, stored_receipt = remote.objects[backup_id]
    assert calls == [("PUT", "/private-environments/backups/" + backup_id, stored_receipt["sha256"])]
    assert hashlib.sha256(saved_bytes).hexdigest() == stored_receipt["sha256"]
    assert receipt == before["1" * 32][1]
    assert scopes == [hosted.api_audience + "/.default"]
    assert {path.relative_to(private_home): path.read_bytes()
            for path in private_home.rglob("*") if path.is_file()} == source_files
