"""Real pinned SDK pipeline with offline HTTP/identity; never a cloud retry."""

import importlib.metadata
import json
import logging
import socket
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import VersionRefIndicator
from azure.core.credentials import AccessToken
from azure.core.exceptions import ServiceRequestError
from azure.core.pipeline.transport import HttpResponse, HttpTransport

from shared_context.behavior_foundry import FoundryExecutor, FoundryHttpClient, FOUNDRY_TOKEN_SCOPE
from shared_context.errors import Problem
from test_behavior_foundry import TARGET, PERMIT
from test_behavior_delivery import CONTEXT
from test_behavior_workers import WorkerHarness

TOKEN = "OFFLINE_CREDENTIAL_CANARY"
PRIVATE = "OFFLINE_PRIVATE_RESPONSE_CANARY"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("real network is forbidden")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setenv("AZURE_AI_PROJECTS_CONSOLE_LOGGING", "false")


class Credential:
    def __init__(self):
        self.scopes = []

    def get_token(self, *scopes, **kwargs):
        self.scopes.append(list(scopes))
        return AccessToken(TOKEN, 9999999999)


class Response(HttpResponse):
    def __init__(self, request, status, body):
        super().__init__(request, None)
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self.content_type = "application/json"
        self.reason = "OFFLINE"
        self._body = b"" if body is None else json.dumps(body).encode()

    def body(self):
        return self._body

    def read(self):
        return self._body

    def json(self):
        return json.loads(self._body)

    def stream_download(self, pipeline, **kwargs):
        return iter((self._body,))


class SessionTransport(HttpTransport):
    def __init__(self, *, fail_phase=None, fail_status=400, session_change=None):
        self.calls = []
        self.fail_phase, self.fail_status = fail_phase, fail_status
        self.session_change = session_change or {}

    def open(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def send(self, request, **kwargs):
        parsed = urlsplit(request.url)
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        phase = "get_session" if request.method == "GET" else (
            "stop_session" if parsed.path.endswith(":stop") else "create_session")
        self.calls.append({"phase": phase, "method": request.method, "path": parsed.path,
                           "query": parse_qs(parsed.query),
                           "body": None if not request.body else json.loads(request.body)})
        if self.fail_phase == phase:
            return Response(request, self.fail_status, {"error": {
                "code": "OFFLINE_ERROR", "message": PRIVATE + " " + TOKEN}})
        if phase == "stop_session":
            return Response(request, 204, None)
        return Response(request, 200 if phase == "get_session" else 201, {
            "agent_session_id": TARGET.session_id,
            "version_indicator": {"type": "version_ref", "agent_version": TARGET.agent_version},
            "status": "idle" if phase == "get_session" else "creating",
            "created_at": 1, "last_accessed_at": 1, "expires_at": 9999999999,
            **self.session_change,
        })


def inject_sdk(monkeypatch, credential, transport):
    import azure.ai.projects
    import shared_context.behavior_foundry as foundry

    def create(**options):
        assert options["credential"] is credential
        return AIProjectClient(transport=transport, **options)

    monkeypatch.setattr(azure.ai.projects, "AIProjectClient", create)
    monkeypatch.setattr(foundry.time, "sleep", lambda _: None)


def wire(explicit):
    credential, transport = Credential(), SessionTransport()
    options = {"api_version": "v1", "credential_scopes": [FOUNDRY_TOKEN_SCOPE]} if explicit else {}
    with AIProjectClient(endpoint=TARGET.project_endpoint, credential=credential, transport=transport,
                         retry_total=0, retry_connect=0, retry_read=0, retry_status=0, **options) as project:
        project.agents.create_session(TARGET.agent_name,
            version_indicator=VersionRefIndicator(agent_version=TARGET.agent_version),
            agent_session_id=TARGET.session_id)
        read = project.agents.get_session(TARGET.agent_name, TARGET.session_id)
        assert isinstance(read.version_indicator, VersionRefIndicator) and read.status == "idle"
        project.agents.stop_session(TARGET.agent_name, TARGET.session_id)
    return transport.calls, credential.scopes


def test_pinned_sdk_defaults_are_identical_to_explicit_api_version_and_scope():
    assert importlib.metadata.version("azure-ai-projects") == "2.3.0"
    default, explicit = wire(False), wire(True)
    assert default == explicit
    assert [call["phase"] for call in default[0]] == ["create_session", "get_session", "stop_session"]
    assert all(call["query"] == {"api-version": ["v1"]} for call in default[0])
    assert all(scopes == [FOUNDRY_TOKEN_SCOPE] for scopes in default[1])


@pytest.mark.parametrize("action", ["create", "verify"])
def test_same_session_version_change_has_a_specific_refusal_before_invocation(monkeypatch, action):
    credential = Credential()
    transport = SessionTransport(session_change={
        "version_indicator": {"type": "version_ref", "agent_version": "999"}, "status": "idle",
    })
    inject_sdk(monkeypatch, credential, transport)
    client = FoundryHttpClient(TARGET, credential, transport=httpx.MockTransport(
        lambda _: pytest.fail("version-changed session must not be invoked")))
    try:
        with pytest.raises(Problem) as failed:
            if action == "create":
                client.create_session(before_send=lambda: None)
            else:
                client.verify_session(before_send=lambda: None)
        error = failed.value.body["error"]
        assert error["code"] == "CONVERSATION_SESSION_VERSION_CHANGED"
        assert error["expected_agent_version"] == TARGET.agent_version
        assert error["observed_agent_version"] == "999"
        assert len(transport.calls) == 1
        assert TOKEN not in json.dumps(error) and TARGET.session_id not in json.dumps(error)
    finally:
        client.close()


@pytest.mark.parametrize("change", [
    {"agent_session_id": "another-session", "version_indicator": {"type": "version_ref", "agent_version": "999"}},
    {"version_indicator": {"type": "version_ref", "agent_version": "latest"}},
    {"version_indicator": {"type": "version_ref", "agent_version": ""}},
])
def test_unknown_or_foreign_session_metadata_is_not_a_version_change_proof(monkeypatch, change):
    credential = Credential()
    transport = SessionTransport(session_change=change)
    inject_sdk(monkeypatch, credential, transport)
    client = FoundryHttpClient(TARGET, credential, transport=httpx.MockTransport(
        lambda _: pytest.fail("unverified metadata must not invoke")))
    try:
        with pytest.raises(Problem) as failed:
            client.verify_session(before_send=lambda: None)
        assert failed.value.body["error"]["code"] == "CONVERSATION_SESSION_UNAVAILABLE"
        assert len(transport.calls) == 1
    finally:
        client.close()


@pytest.mark.parametrize("phase,status", [
    ("create_session", 400), ("create_session", 401), ("get_session", 409), ("stop_session", 503),
])
def test_session_http_failure_keeps_phase_and_status_without_secret_or_retry(monkeypatch, caplog, phase, status):
    credential, transport = Credential(), SessionTransport(fail_phase=phase, fail_status=status)
    inject_sdk(monkeypatch, credential, transport)
    client = FoundryHttpClient(TARGET, credential, transport=httpx.MockTransport(
        lambda _: pytest.fail("invocation must not occur")))
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(Problem) as failed:
            if phase == "stop_session":
                client.stop_session()
            else:
                client.create_session(before_send=lambda: None)
    finally:
        client.close()
    error = failed.value.body["error"]
    assert error["code"] == ("FOUNDRY_SESSION_STOP_UNKNOWN" if phase == "stop_session" else "FOUNDRY_SESSION_UNKNOWN")
    assert error.get("upstream_phase") == phase and error.get("upstream_status") == status
    assert error.get("api_version") == "v1"
    assert sum(call["phase"] == phase for call in transport.calls) == 1
    assert "FOUNDRY_SESSION_UNRESOLVED" in caplog.text
    for value in (TOKEN, PRIVATE, TARGET.project_endpoint, TARGET.session_id, "Authorization"):
        assert value not in json.dumps(error) + caplog.text


def test_missing_http_response_never_invents_status(monkeypatch, caplog):
    credential, transport = Credential(), SessionTransport()
    def unavailable(*args, **kwargs):
        raise ServiceRequestError(PRIVATE + " " + TOKEN)
    monkeypatch.setattr(credential, "get_token", unavailable)
    inject_sdk(monkeypatch, credential, transport)
    client = FoundryHttpClient(TARGET, credential)
    try:
        with pytest.raises(Problem) as failed:
            client.create_session(before_send=lambda: None)
    finally:
        client.close()
    error = failed.value.body["error"]
    assert error.get("upstream_phase") == "create_session"
    assert error["upstream_status"] is None and transport.calls == []
    assert PRIVATE not in json.dumps(error) + caplog.text and TOKEN not in caplog.text


class StalledOnce(SessionTransport):
    """The first `stalls` GETs end in a read timeout (stage 2026-10-06 12:40); later ones answer."""

    def __init__(self, stalls):
        super().__init__()
        self.stalls = stalls

    def send(self, request, **kwargs):
        from azure.core.exceptions import ServiceResponseTimeoutError
        if request.method == "GET" and self.stalls:
            self.stalls -= 1
            self.calls.append({"phase": "stalled", "method": request.method})
            raise ServiceResponseTimeoutError("synthetic read timeout")
        return super().send(request, **kwargs)

    def sleep(self, duration):
        pass


@pytest.mark.parametrize("stalls", [1, 2])
def test_a_platform_session_read_retries_one_stalled_get(monkeypatch, caplog, stalls):
    from shared_context.behavior_native_sessions import _session_metadata
    credential, transport = Credential(), StalledOnce(stalls)
    inject_sdk(monkeypatch, credential, transport)
    client = SimpleNamespace(target=TARGET, credential=credential)
    if stalls == 1:
        assert _session_metadata(client).agent_session_id == TARGET.session_id
    else:
        with caplog.at_level(logging.WARNING), pytest.raises(Problem) as failed:
            _session_metadata(client)
        assert failed.value.status == 503 and failed.value.body["error"]["code"] == "NATIVE_BOOTSTRAP_UNKNOWN"
        assert "NATIVE_BOOTSTRAP_PLATFORM_READ status=None" in caplog.text
    # One bounded retry for this idempotent GET: never a third attempt.
    assert [call["method"] for call in transport.calls] == ["GET", "GET"]


def test_a_platform_status_failure_is_not_retried_so_no_retry_after_wait(monkeypatch):
    from shared_context.behavior_native_sessions import _session_metadata
    credential, transport = Credential(), StalledOnce(0)
    transport.fail_phase, transport.fail_status = "get_session", 503
    inject_sdk(monkeypatch, credential, transport)
    with pytest.raises(Problem) as failed:
        _session_metadata(SimpleNamespace(target=TARGET, credential=credential))
    assert failed.value.status == 503 and failed.value.body["error"]["code"] == "NATIVE_BOOTSTRAP_UNKNOWN"
    assert [call["method"] for call in transport.calls] == ["GET"]


def test_a_platform_session_write_is_still_never_retried(monkeypatch):
    credential, transport = Credential(), StalledOnce(0)
    inject_sdk(monkeypatch, credential, transport)
    transport.fail_phase, transport.fail_status = "create_session", 503
    client = FoundryHttpClient(TARGET, credential)
    try:
        with pytest.raises(Problem):
            client.create_session(before_send=lambda: None)
    finally:
        client.close()
    assert sum(call["phase"] == "create_session" for call in transport.calls) == 1


@pytest.mark.parametrize("status", [True, "401", 99, 600, PRIVATE])
def test_untrusted_status_is_not_copied_to_diagnostics(status, caplog):
    from shared_context.behavior_foundry import _session_failure
    error = ServiceRequestError(PRIVATE)
    error.status_code = status
    result = _session_failure(error, "create_session")
    assert result.body["error"]["upstream_status"] is None
    assert PRIVATE not in json.dumps(result.body) + caplog.text


def test_unexpected_exception_names_and_messages_never_enter_start_log(monkeypatch, caplog):
    error_type = type(PRIVATE, (RuntimeError,), {})
    def denied():
        raise error_type(TOKEN)
    executor = FoundryExecutor.__new__(FoundryExecutor)
    monkeypatch.setattr(executor, "_configured", denied)
    with caplog.at_level(logging.WARNING), pytest.raises(error_type):
        executor.start(None)
    assert "phase=configure" in caplog.text and "exception_type=UNCLASSIFIED" in caplog.text
    assert PRIVATE not in caplog.text and TOKEN not in caplog.text


@pytest.mark.parametrize("failure", ["session_http", "session_binding", "invocation_http", "invocation_timeout"])
def test_worker_wrapper_keeps_launch_stage_in_logs_and_never_restarts(monkeypatch, caplog, failure):
    credential = Credential()
    transport = SessionTransport(
        fail_phase="create_session" if failure == "session_http" else None, fail_status=401,
        session_change={"version_indicator": {"type": "version_ref", "agent_version": "999"}}
        if failure == "session_binding" else None,
    )
    inject_sdk(monkeypatch, credential, transport)
    calls = []
    def invoke(request):
        calls.append(request)
        if failure == "invocation_timeout":
            raise httpx.ReadTimeout(PRIVATE, request=request)
        return httpx.Response(503, content=PRIVATE, request=request)
    client = FoundryHttpClient(TARGET, credential, transport=httpx.MockTransport(invoke))
    h = WorkerHarness()
    h.job()
    launch = SimpleNamespace(target=TARGET, launch_id="launch", api_binding_id="binding-one")
    authority = SimpleNamespace(
        workers=SimpleNamespace(instruction=lambda _: True, work_policy=SimpleNamespace(
            check_dispatch=lambda *_: None,
            _job_record=lambda _: SimpleNamespace(hosted_session_id=None))),
        prepare=lambda _: (launch, PERMIT),
    )
    executor = FoundryExecutor.__new__(FoundryExecutor)
    executor.authority, executor.clients = authority, {TARGET.executor_id: client}
    monkeypatch.setattr(executor, "_configured", lambda: None)
    h.workers.adapter = replace(h.workers.adapter, start=executor.start)
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(Problem) as failed:
            h.workers.start("job-one", CONTEXT)
        assert failed.value.body["error"]["code"] == "WORKER_START_UNKNOWN"
        assert h.workers.inspect("job-one").state == "starting" and len(h.occupied()) == 1
        assert "FOUNDRY_START_UNRESOLVED" in caplog.text
        expected = "session" if failure.startswith("session") else "invocation"
        assert f"phase={expected}" in caplog.text
        if failure == "session_http":
            assert "status=401" in caplog.text
        elif failure == "invocation_http":
            assert "status=503" in caplog.text
        else:
            assert "status=None" in caplog.text
        counts = len(transport.calls), len(calls)
        h.workers.start("job-one", CONTEXT)
        assert (len(transport.calls), len(calls)) == counts
        assert len(calls) == (0 if failure.startswith("session") else 1)
        for value in (TOKEN, PRIVATE, TARGET.project_endpoint, TARGET.session_id, PERMIT.get_secret_value()):
            assert value not in json.dumps(failed.value.body) + caplog.text
    finally:
        client.close()
