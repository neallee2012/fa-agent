from __future__ import annotations

import json
import ssl
from pathlib import Path
from types import SimpleNamespace
from threading import Barrier, Lock

import httpx
import pytest
from starlette.testclient import TestClient

from conftest import AUDIENCE
from shared_context.errors import Problem
from shared_context.hosted import HostedClient, runtime_metadata


class Credential:
    def __init__(self):
        self.scopes = []

    def get_token(self, scope):
        self.scopes.append(scope)
        return SimpleNamespace(token="synthetic-token-never-expose")


def client(handler, audience=AUDIENCE):
    credential = Credential()
    instance = HostedClient(
        "https://context.example", audience, "https://synthetic.table.core.windows.net", "PocTable",
        "https://synthetic.services.ai.azure.com/api/projects/poc", "poc-team-memory",
        credential, httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return instance, credential


def test_hosted_preserves_complete_resource_uri_in_token_request():
    audience = "api://00000000-0000-0000-0000-000000000013/fa-context-poc-api"
    hosted, credential = client(lambda request: httpx.Response(200, json={"revision": 1}), audience)
    response = hosted.dispatch({"operation": "api", "method": "GET", "path": "/teams/team-one/works/work-1"})
    assert response["outcome"]["http_status"] == 200
    assert credential.scopes == [audience + "/.default"]


@pytest.mark.parametrize("bundle_variable", ["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"])
def test_hosted_uses_platform_ca_bundle_without_enabling_proxies_or_disabling_tls(monkeypatch, bundle_variable):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    context = ssl.create_default_context()
    monkeypatch.setenv(bundle_variable, "synthetic-platform-ca.pem")
    loaded, options = [], []

    def tls_context(*, cafile=None):
        loaded.append(cafile)
        return context

    monkeypatch.setattr(ssl, "create_default_context", tls_context)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: options.append(kwargs) or SimpleNamespace())
    HostedClient(
        "https://context.example", AUDIENCE, "https://synthetic.table.core.windows.net", "PocTable",
        "https://synthetic.services.ai.azure.com/api/projects/poc", "poc-team-memory", Credential(),
    )
    assert loaded == ["synthetic-platform-ca.pem"]
    assert options[0]["verify"] is context
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False


def test_api_race_issues_exactly_two_concurrent_same_identity_same_etag_updates():
    entered = Barrier(2, timeout=5)
    lock, seen = Lock(), []

    def handler(request):
        entered.wait()
        with lock:
            seen.append(request)
            status = 200 if len(seen) == 1 else 412
        return httpx.Response(status, json={"synthetic_status": status})

    hosted, credential = client(handler)
    response = hosted.dispatch({
        "operation": "api_race", "path": "/teams/team-one/works/work-1", "etag": 'W/"same-version"',
        "updates": [{"progress": "synthetic contender 1"}, {"progress": "synthetic contender 2"}],
    })["outcome"]
    assert response["concurrency"] == 2 and response["same_client_identity"]
    assert response["cas_expected_outcome"] is True
    assert sorted(response["http_statuses"]) == [200, 412]
    assert len(seen) == 2 and all(request.method == "PATCH" for request in seen)
    assert {request.headers["if-match"] for request in seen} == {'W/"same-version"'}
    assert len({request.headers["authorization"] for request in seen}) == 1
    assert sorted(json.loads(request.content)["progress"] for request in seen) == [
        "synthetic contender 1", "synthetic contender 2",
    ]
    assert credential.scopes == [AUDIENCE + "/.default", AUDIENCE + "/.default"]


@pytest.mark.parametrize("status", [200, 403, 412, 500])
def test_api_race_does_not_claim_expected_cas_for_other_outcomes(status):
    hosted, _ = client(lambda request: httpx.Response(status, json={"synthetic": True}))
    outcome = hosted.dispatch({
        "operation": "api_race", "path": "/teams/team-one/works/work-1", "etag": 'W/"same"',
        "updates": [{"progress": "one"}, {"progress": "two"}],
    })["outcome"]
    assert outcome["cas_expected_outcome"] is False
    assert outcome["http_statuses"] == [status, status]


@pytest.mark.parametrize("changes", [
    {"updates": [{"progress": "one"}]},
    {"updates": [{"progress": "one"}, {"progress": "two"}, {"progress": "three"}]},
    {"updates": [{"owner": "other"}, {"progress": "two"}]},
    {"path": "/teams/team-one/works/work-1/publish"},
    {"path": "/teams/team-one/memory/team"},
    {"etag": "*"},
])
def test_api_race_is_bounded_and_cannot_publish_or_change_authority_fields(changes):
    hosted, credential = client(lambda request: pytest.fail("must not call API"))
    with pytest.raises(Problem) as exc:
        hosted.dispatch({
            "operation": "api_race", "path": "/teams/team-one/works/work-1", "etag": 'W/"same"',
            "updates": [{"progress": "one"}, {"progress": "two"}], **changes,
        })
    assert exc.value.status == 422 and credential.scopes == []


def test_api_probe_uses_managed_identity_audience_etag_and_returns_outcome():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(412, json={"error": {"code": "ETAG_CONFLICT"}}, headers={"etag": 'W/"new"'})

    hosted, credential = client(handler)
    result = hosted.dispatch({
        "operation": "api", "method": "PATCH", "path": "/teams/team-one/works/work-1",
        "etag": 'W/"stale"', "body": {"progress": "synthetic"},
    })
    assert result["outcome"]["http_status"] == 412
    assert result["outcome"]["content"]["error"]["code"] == "ETAG_CONFLICT"
    assert credential.scopes == [AUDIENCE + "/.default"]
    assert seen[0].headers["if-match"] == 'W/"stale"'
    assert seen[0].headers["authorization"] == "Bearer synthetic-token-never-expose"
    assert "synthetic-token" not in json.dumps(result)


@pytest.mark.parametrize("operation,scope", [
    ("direct_storage", "https://storage.azure.com/.default"),
    ("direct_memory", "https://ai.azure.com/.default"),
])
@pytest.mark.parametrize("status,denied,unexpected", [(403, True, False), (401, True, False), (200, False, True), (404, False, False), (500, False, False)])
def test_direct_probe_reports_denial_only_for_auth_denial(operation, scope, status, denied, unexpected):
    hosted, credential = client(lambda request: httpx.Response(status, json={"code": "synthetic"}))
    result = hosted.dispatch({"operation": operation, "team": "team-one", "work": "work-1"})["outcome"]
    assert result["access_denied"] is denied and result["unexpected_success"] is unexpected
    assert result["http_status"] == status
    assert credential.scopes == [scope]


def test_token_failure_is_not_a_denial_pass():
    hosted, _ = client(lambda request: pytest.fail("must not send"))
    hosted.credential.get_token = lambda scope: (_ for _ in ()).throw(RuntimeError("MI unavailable"))
    result = hosted.dispatch({"operation": "direct_memory", "team": "team-one"})["outcome"]
    assert result["http_status"] is None and result["access_denied"] is False
    assert result["error"]["code"] == "TOKEN_ACQUISITION_FAILED"


@pytest.mark.parametrize("path", ["https://evil.example", "//evil.example", "/teams/t/works/../token", "/healthz",
                                  "/teams/t/works/w?scope=bad", "/teams/t/works/%2e%2e"])
def test_api_probe_rejects_arbitrary_urls_and_paths(path):
    hosted, credential = client(lambda request: pytest.fail("must not send"))
    with pytest.raises(Problem):
        hosted.dispatch({"operation": "api", "method": "GET", "path": path})
    assert credential.scopes == []


def test_probe_redacts_echoed_bearer_and_tokens():
    hosted, _ = client(lambda request: httpx.Response(403, json={
        "error": "Bearer synthetic-token-never-expose", "access_token": "other-secret",
    }))
    response = hosted.dispatch({"operation": "direct_memory", "team": "team-one"})
    text = json.dumps(response)
    assert "synthetic-token" not in text and "other-secret" not in text


def test_home_metadata_is_not_fixed_vm_proof():
    # Owned project path only: do not use pytest tmp_path or the user's actual HOME.
    home = Path(__file__).resolve().parent / ".runtime-fixture"
    home.mkdir(exist_ok=False)
    try:
        first = runtime_metadata(home)
        second = runtime_metadata(home)
        assert first["home_marker"] == second["home_marker"]
        assert first["process_boot_marker"] == second["process_boot_marker"]
        assert first["fixed_vm_validated"] is False and first["pid"] > 0
    finally:
        (home / ".shared-context-poc" / "home-marker").unlink(missing_ok=True)
        (home / ".shared-context-poc").rmdir()
        home.rmdir()


def test_unwritable_platform_home_reports_diagnostic_without_masking_api_result(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    import hosted_main

    def denied(_path, *args, **kwargs):
        raise PermissionError(13, "synthetic platform HOME is not writable")

    monkeypatch.setattr(Path, "mkdir", denied)
    monkeypatch.setattr(hosted_main, "client", client(
        lambda request: httpx.Response(201, json={"revision": 1, "receipt": "synthetic"}),
    )[0])
    with TestClient(hosted_main.app) as http:
        response = http.post("/invocations?agent_session_id=synthetic-session", json={
            "operation": "api", "method": "POST", "path": "/teams/team-one/works/work-1",
            "body": {"progress": "synthetic"},
        })
    assert response.status_code == 200
    assert response.json()["outcome"]["http_status"] == 201
    runtime = response.json()["runtime"]
    assert runtime["home_marker"] is None
    assert runtime["home_probe"]["status"] == "unavailable"
    assert runtime["home_probe"]["error"]["type"] == "PermissionError"
    assert runtime["home_probe"]["error"]["errno"] == 13
    assert runtime["process_boot_marker"]
    assert runtime["fixed_vm_validated"] is False


def test_unexpected_runtime_metadata_failure_happens_before_api_mutation(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    import hosted_main

    def fail_metadata():
        raise RuntimeError("synthetic unexpected metadata failure")

    seen = []

    def send(request):
        seen.append(request)
        return httpx.Response(201, json={"revision": 1})

    monkeypatch.setattr(hosted_main, "runtime_metadata", fail_metadata)
    monkeypatch.setattr(hosted_main, "client", client(send)[0])
    with pytest.raises(RuntimeError, match="metadata failure"):
        hosted_main.invoke({
            "operation": "api", "method": "POST", "path": "/teams/team-one/works/work-1",
        })
    assert seen == []


def test_real_hosted_invocation_entry_handler(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    import hosted_main

    monkeypatch.setattr(hosted_main, "client", client(lambda request: httpx.Response(403, json={"code": "Denied"}))[0])
    monkeypatch.setattr(hosted_main, "runtime_metadata", lambda: {"pid": 1, "fixed_vm_validated": False})
    with TestClient(hosted_main.app) as http:
        response = http.post("/invocations?agent_session_id=synthetic-session", json={
            "operation": "direct_memory", "team": "team-one",
        })
        assert response.status_code == 200
        assert response.json()["outcome"]["http_status"] == 403
        assert response.json()["runtime"]["fixed_vm_validated"] is False
        assert response.json()["runtime"]["session_id"] == "synthetic-session"
        assert response.json()["runtime"]["invocation_id"]
        assert http.post("/invocations", content=b"x" * 65537).status_code == 413
        assert http.post("/invocations", content=b"not-json").status_code == 400
