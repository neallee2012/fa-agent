import builtins
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from conftest import A
from shared_context import behavior_assistant_hosted as hosted
from shared_context import behavior_wire as wire
from shared_context.behavior_assistant import AssistantAuthority, AssistantRuntime, _LocalHttp, _Session, _row, key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_hosted import AssistantClient, AssistantSupervisor
from shared_context.behavior_foundry import (
    FoundryExecutor, FoundryHttpClient, FoundryTarget, NativeExecutionGrant, NativeJobRoute,
    NativeLaunchAuthority, native_key,
)
from shared_context.behavior_hosted import BehaviorExecutorClient, HostedInvocation
from shared_context.behavior_operations import control_key
from shared_context.errors import Problem
from test_behavior_assistant import assistant, submit
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def runtime_path(tmp_path):
    # These cases must fail before SDK/runtime launch, not invoke a model.
    return tmp_path / "never-started"


@pytest.fixture
def native_startup(assistant, tmp_path, runtime_path):
    """Real authority producers and HTTP consumers, with synthetic identities."""
    h = assistant
    target = FoundryTarget(
        executor_id=h.workers.contract.executor_id, project_endpoint="https://native.invalid/api/projects/test",
        agent_id="11111111-1111-1111-1111-111111111111", agent_name="assistant",
        agent_version="8", session_id="offline-startup-session", agent_oid=A.oid,
    )
    native = NativeLaunchAuthority(
        h.workers, team_id=A.team, controller_id="internal-controller", targets=(target,),
        jobs=(NativeJobRoute(job_id="job-one", api_binding_id="binding-one", grant_id="native-grant"),),
        verifier_version=h.config.verifier_version, clock=lambda: h.time,
    )
    starts = []
    request_paths = []
    invocation_id = "offline-startup-invocation"

    def native_http(request):
        assert request.method == "POST" and "x-client-behavior-launch-permit" in request.headers
        starts.append(HostedInvocation(**json.loads(request.content), launch_permit=SecretStr(
            request.headers["x-client-behavior-launch-permit"])))
        try:
            result = host.start(starts[-1], invocation_id, target.session_id)
        except (Problem, RuntimeError):
            return httpx.Response(503)
        return httpx.Response(202, json=result, headers={
            "x-agent-invocation-id": invocation_id, "x-agent-session-id": target.session_id,
        })

    native_client = FoundryHttpClient(
        target, SimpleNamespace(get_token=lambda _: SimpleNamespace(token="SYNTHETIC_NATIVE")),
        transport=httpx.MockTransport(native_http),
    )
    h.workers.adapter = FoundryExecutor(native, (native_client,)).adapter()
    h.dep = replace(h.dep, native_launches=native)
    h.runtime = AssistantRuntime(
        h.dep, (h.binding,), profiles=tuple(h.runtime.profiles.values()),
        clock=lambda: h.time, mode="offline_contract", controller_id="internal-controller",
    )
    h.assistant = AssistantAuthority(h.runtime, h.artifacts, (A,))
    h.client = TestClient(create_assistant_app(h.assistant, h.verifier))
    transport = _LocalHttp(h.client)

    def api_http(request):
        request_paths.append(request.url.path)
        return transport.handle_request(request)

    api = BehaviorExecutorClient(
        "https://context.invalid", h.verifier.auth.audience,
        SimpleNamespace(get_token=lambda _: SimpleNamespace(
            token=h.headers("agent")["Authorization"].removeprefix("Bearer "))),
        transport=httpx.MockTransport(api_http),
    )
    supervisor = AssistantSupervisor(tmp_path / "native-host", runtime_path=runtime_path)
    host = hosted.AssistantHostedRuntime(wire.HostedIdentity(
        agent_id=target.agent_id, agent_name=target.agent_name, agent_version=target.agent_version,
        session_id=target.session_id,
    ), supervisor, api, clock=lambda: h.time)
    submit(h)
    job = h.workers.inspect("job-one")
    h.seed([(native_key("grant", "native-grant"), NativeExecutionGrant(
        grant_id="native-grant", controller_id="internal-controller", owner=job.owner,
        policy_revision=1, job_id="job-one", definition=h.binding.work,
        input_digest=hashlib.sha256(job.spec.input).hexdigest(), targets=(target,),
        active=True, remaining_uses=1, not_before=h.time - timedelta(minutes=1),
        expires_at=h.time + timedelta(minutes=5), workload_version="assistant-v1.2",
    ))])
    try:
        yield SimpleNamespace(h=h, host=host, api=api, supervisor=supervisor,
                              starts=starts, request_paths=request_paths, invocation_id=invocation_id)
    finally:
        for thread in supervisor.threads.values():
            if thread.ident is not None:
                thread.join(timeout=5)
                assert not thread.is_alive()
        api.close()
        native_client.close()
        h.client.close()


@pytest.mark.parametrize("failure", ["create_thread", "start_thread"])
def test_native_thread_start_failure_is_diagnosed_before_acquire(native_startup, monkeypatch, caplog, failure):
    case = native_startup

    def unavailable_thread(*args, **kwargs):
        def start():
            raise RuntimeError("SYNTHETIC_PRIVATE_CANARY")
        if failure == "create_thread":
            start()
        return SimpleNamespace(start=start, ident=None, is_alive=lambda: False)

    monkeypatch.setattr(hosted, "Thread", unavailable_thread)
    with pytest.raises(Problem) as error:
        case.h.runtime.controller.step("binding-one")
    assert error.value.body["error"]["code"] == "WORKER_START_UNKNOWN"
    assert len(case.starts) == 1
    launch_id = case.starts[0].launch_id
    state = case.supervisor.observe(launch_id)
    assert state.phase == "spawn_unknown" and state.child_pid is None and not state.child_exited
    assert len(case.request_paths) == 2 and all(path.endswith("/claim") for path in case.request_paths)
    assert case.h.endpoint.requests == []
    with case.supervisor.journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "assistant_failures" in tables, "Hosted thread-start failure was lost before SDK acquire"
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id=?", (launch_id,)).fetchone()
    saved = json.loads(raw)
    assert saved["phase"] == failure
    assert saved["exception_type"] == "RuntimeError"
    assert "SYNTHETIC_PRIVATE_CANARY" not in raw and "SYNTHETIC_PRIVATE_CANARY" not in caplog.text
    assert case.host.start(case.starts[0], case.invocation_id, case.host.identity.session_id)["status"] == "pending"
    assert len(case.request_paths) == 2


@pytest.mark.parametrize("failure, phase, upstream_phase, status, exception_type", [
    ("engine_import", "load_engine", None, None, "ModuleNotFoundError"),
    ("token", "acquire", "token", None, "RuntimeError"),
    ("connect", "acquire", "request", None, "ConnectTimeout"),
    ("headers", "acquire", "request", None, "ReadTimeout"),
    ("body", "acquire", "response_body", 200, "ReadError"),
    ("decode", "acquire", "response_decode", 200, "ValidationError"),
])
def test_native_startup_failure_preserves_first_category(
    native_startup, monkeypatch, caplog, failure, phase, upstream_phase, status, exception_type,
):
    case = native_startup
    original_import = builtins.__import__
    original_http = case.api.http._transport
    get_token = case.api.credential.get_token
    canary = "SYNTHETIC_PRIVATE_CANARY"
    token_calls = 0

    def import_module(name, *args, **kwargs):
        if failure == "engine_import" and name == "m2_assistant":
            raise ModuleNotFoundError(canary, name=canary)
        return original_import(name, *args, **kwargs)

    def token(scope):
        nonlocal token_calls
        token_calls += 1
        if failure == "token" and token_calls == 3:
            raise RuntimeError(canary)
        return get_token(scope)

    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            yield canary.encode()
            raise httpx.ReadError(canary)

    def send(request):
        if request.url.path.endswith("/acquire"):
            case.request_paths.append(request.url.path)
            if failure in {"connect", "headers"}:
                error = httpx.ConnectTimeout if failure == "connect" else httpx.ReadTimeout
                raise error(canary, request=request)
            if failure == "body":
                return httpx.Response(200, stream=BrokenBody())
            if failure == "decode":
                return httpx.Response(200, text=canary)
            pytest.fail("failure injection must precede the acquire request")
        return original_http.handle_request(request)

    monkeypatch.setattr(builtins, "__import__", import_module)
    monkeypatch.setattr(case.api.credential, "get_token", token)
    case.api.http._transport = httpx.MockTransport(send)
    case.h.runtime.controller.step("binding-one")
    case.supervisor.join(timeout=5)
    assert len(case.starts) == 1
    launch_id = case.starts[0].launch_id
    state = case.supervisor.observe(launch_id)
    assert state.phase == "spawn_unknown" and state.child_pid is None and not state.child_exited
    assert case.h.endpoint.requests == []
    with case.supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id=?", (launch_id,)).fetchone()
    saved = json.loads(raw)
    assert (saved["phase"], saved["upstream_phase"], saved["upstream_status"]) == (phase, upstream_phase, status)
    assert saved["exception_type"] == exception_type
    assert canary not in raw and canary not in caplog.text
    assert len(case.request_paths) == (2 if failure in {"engine_import", "token"} else 3)
    before = list(case.request_paths)
    assert case.host.start(case.starts[0], case.invocation_id, case.host.identity.session_id)["status"] == "pending"
    assert case.request_paths == before


def test_real_authority_denial_at_acquire_retains_status_and_accepted_state(native_startup):
    case = native_startup
    transport = case.api.http._transport
    statuses = []

    def send(request):
        if request.url.path.endswith("/acquire"):
            case.h.mutate(control_key("owner", "owner"), epoch=2)
            response = transport.handle_request(request)
            statuses.append(response.status_code)
            return response
        return transport.handle_request(request)

    case.api.http._transport = httpx.MockTransport(send)
    case.h.runtime.controller.step("binding-one")
    case.supervisor.join(timeout=5)
    launch_id = case.starts[0].launch_id
    assert statuses == [403]
    assignment = case.supervisor.clients[launch_id].assignment
    session_key = key("session", assignment.session_id)
    session = _row(case.h.assistant.store.read_snapshot((session_key,)), session_key, _Session)
    assert session.state == "accepted"
    assert session.model_requests == 0 and not session.context_read
    with case.supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id=?", (launch_id,)).fetchone()
    saved = json.loads(raw)
    assert saved["phase"] == "acquire" and saved["upstream_status"] == 403
    assert saved["upstream_phase"] == "response_status"
    assert saved["exception_type"] == "Problem"
    state = case.supervisor.observe(launch_id)
    assert state.phase == "spawn_unknown" and state.sdk_stop is None and state.child_pid is None
    assert case.h.endpoint.requests == []


def test_native_claim_credential_failure_is_not_reported_as_http_failure(native_startup, monkeypatch, caplog):
    case = native_startup
    get_token = case.api.credential.get_token
    calls = 0

    def token(scope):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("SYNTHETIC_PRIVATE_CANARY")
        return get_token(scope)

    monkeypatch.setattr(case.api.credential, "get_token", token)
    with pytest.raises(Problem) as error:
        case.h.runtime.controller.step("binding-one")
    assert error.value.body["error"]["code"] == "WORKER_START_UNKNOWN"
    launch_id = case.starts[0].launch_id
    assert len(case.request_paths) == 1
    assert case.h.endpoint.requests == []
    with case.supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id=?", (launch_id,)).fetchone()
        assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    saved = json.loads(raw)
    assert saved["phase"] == "native_claim" and saved["upstream_phase"] == "token"
    assert saved["upstream_status"] is None and saved["exception_type"] == "RuntimeError"
    assert "SYNTHETIC_PRIVATE_CANARY" not in raw and "SYNTHETIC_PRIVATE_CANARY" not in caplog.text


def test_native_claim_replay_logs_only_original_code_and_request_id(native_startup, caplog):
    case, reservations = native_startup, []
    h, native = case.h, case.h.runtime.dep.native_launches
    original = h.workers.adapter
    h.workers.adapter = replace(original, start=lambda start: reservations.append(native.prepare(start)))
    try:
        h.workers.start(h.binding.job_id, h.binding.owner)
    finally:
        h.workers.adapter = original
    launch, permit = reservations[0]
    root = f"/behavior/v1/bindings/{h.binding.binding_id}"
    command = wire.HostedClaimRequest(
        request_id="native-original-claim", invocation_id=case.invocation_id,
        identity=case.host.identity, process_id="original-claim-process", launch_permit=permit)
    claimed = h.client.post(root + f"/hosted/launches/{launch.launch_id}/claim",
        json=command.model_dump(mode="json", exclude={"launch_permit"}),
        headers={**h.headers("agent"), "x-behavior-launch-permit": permit.get_secret_value()})
    assert claimed.status_code == 200, claimed.text
    receipt = wire.HostedClaimReceipt.model_validate_json(claimed.content)
    body = wire.HostedProbeBody(
        request_id="original-assignment-claim", invocation_id=receipt.assignment.invocation_id,
        identity=case.host.identity, process_id=receipt.assignment.process_id,
        claim_id=receipt.assignment.claim_id).model_dump(mode="json")
    headers = {**h.headers("agent"), "x-behavior-report-token": claimed.headers["x-behavior-report-token"]}
    path = root + f"/assistant/launches/{launch.launch_id}/claim"
    assigned = h.client.post(path, json=body, headers=headers)
    assert assigned.status_code == 200, assigned.text
    original_job = h.workers.inspect(h.binding.job_id)
    caplog.clear()
    with caplog.at_level("WARNING"):
        replay = h.client.post(path, json=body, headers=headers)
    assert replay.status_code == 409
    assert replay.json() == {"detail": "The original assistant acquire could not be completed."}
    assert replay.headers["x-behavior-assistant-error-code"] == "ASSISTANT_NATIVE_UNKNOWN"
    assert replay.headers["x-behavior-assistant-error-phase"] == "native_claim"
    assert "x-behavior-assistant-ticket" not in replay.headers
    failures = [json.loads(record.getMessage().removeprefix("BEHAVIOR_REQUEST_FAILED "))
                for record in caplog.records if record.getMessage().startswith("BEHAVIOR_REQUEST_FAILED ")]
    assert failures == [{"code": "ASSISTANT_NATIVE_UNKNOWN", "request_id": "original-assignment-claim"}]
    for secret in (permit.get_secret_value(), claimed.headers["x-behavior-report-token"],
                   assigned.headers["x-behavior-assistant-ticket"], headers["Authorization"]):
        assert secret not in caplog.text
    assert h.workers.inspect(h.binding.job_id) == original_job
    assert not case.supervisor.threads and h.endpoint.requests == []


@pytest.mark.parametrize("failure, upstream_phase, status, exception_type", [
    ("connect", "request", None, "ConnectTimeout"),
    ("headers", "request", None, "ReadTimeout"),
    ("body", "response_body", 200, "ReadError"),
    ("status", "response_status", 412, "Problem"),
])
def test_first_hosted_claim_preserves_transport_boundary_without_replay(
    native_startup, caplog, failure, upstream_phase, status, exception_type,
):
    case = native_startup
    canary = "SYNTHETIC_PRIVATE_CANARY"

    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            yield canary.encode()
            raise httpx.ReadError(canary)

    def send(request):
        case.request_paths.append(request.url.path)
        assert "/hosted/launches/" in request.url.path and request.url.path.endswith("/claim")
        if failure in {"connect", "headers"}:
            error = httpx.ConnectTimeout if failure == "connect" else httpx.ReadTimeout
            raise error(canary, request=request)
        if failure == "body":
            return httpx.Response(200, stream=BrokenBody())
        return httpx.Response(412, text=canary, headers={
            "x-behavior-assistant-error-code": "BEHAVIOR_CONFLICT",
            "x-behavior-assistant-error-phase": "native_evidence",
        })

    case.api.http._transport = httpx.MockTransport(send)
    with pytest.raises(Problem) as error:
        case.h.runtime.controller.step("binding-one")
    assert error.value.body["error"]["code"] == "WORKER_START_UNKNOWN"
    assert len(case.starts) == 1 and len(case.request_paths) == 1
    launch_id = case.starts[0].launch_id
    assert case.h.endpoint.requests == []
    with case.supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id=?", (launch_id,)).fetchone()
        assert db.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    saved = json.loads(raw)
    assert saved["phase"] == "hosted_claim" and saved["code"] == "HOSTED_API_OUTCOME_UNKNOWN"
    assert saved["upstream_phase"] == upstream_phase
    assert saved["upstream_status"] == status and saved["exception_type"] == exception_type
    if failure == "status":
        assert saved["upstream_error_code"] == "BEHAVIOR_CONFLICT"
        assert saved["upstream_error_phase"] == "native_evidence"
    assert canary not in raw and canary not in caplog.text
    assert case.host.start(case.starts[0], case.invocation_id, case.host.identity.session_id)["status"] == "pending"
    assert len(case.request_paths) == 1


@pytest.mark.parametrize("status", [412, 503])
def test_failed_sdk_acquire_retains_status_not_upstream_body(tmp_path, status, caplog):
    from shared_context.behavior_assistant_wire import Assignment, Task

    private = "SYNTHETIC_PRIVATE_CANARY"
    assignment = Assignment(
        binding_id="binding", session_id="a" * 64, sdk_session_id="diagnostic-sdk", resume=False,
        task=Task(
            job_id="diagnostic-job", work_id="diagnostic-work", definition_digest="c" * 64,
            profile_digest="d" * 64, operation_id="diagnostic-operation",
            source={"source_id": "diagnostic-source", "version": 1, "sha256": "e" * 64, "byte_length": 1},
            artifact_id="diagnostic-artifact", artifact_version="f" * 32,
        ),
    )
    requests = []

    def send(request):
        requests.append(request)
        return httpx.Response(status, text=private)

    client = AssistantClient(
        "https://context.invalid", assignment, SecretStr("b" * 64),
        token_provider=lambda: private, transport=httpx.MockTransport(send),
    )
    supervisor = AssistantSupervisor(tmp_path, runtime_path=tmp_path / "never-started")
    try:
        supervisor._execute(SimpleNamespace(launch_id="a" * 64), client)
    finally:
        client.close()
    assert len(requests) == 1
    with supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id=?", ("a" * 64,)).fetchone()
        assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    saved = json.loads(raw)
    assert saved["phase"] == "acquire"
    assert saved["code"] == "ASSISTANT_API_UNKNOWN"
    assert saved["upstream_status"] == status
    assert saved["upstream_phase"] == "response_status"
    assert private not in raw and private not in caplog.text
    assert "child_pid" not in saved and "entire_tree_idle" not in saved


@pytest.mark.parametrize("failure", ["invalid-json", "timeout"])
def test_acquire_protocol_and_transport_errors_remain_distinct(failure):
    private = "SYNTHETIC_PRIVATE_CANARY"

    def send(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(private, request=request)
        return httpx.Response(200, text=private)

    client = AssistantClient(
        "https://context.invalid", SimpleNamespace(binding_id="binding", session_id="a" * 64),
        SecretStr("b" * 64), token_provider=lambda: private, transport=httpx.MockTransport(send),
    )
    try:
        with pytest.raises(Problem) as error:
            client.acquire()
    finally:
        client.close()
    detail = error.value.body["error"]
    assert detail["upstream_status"] == (200 if failure == "invalid-json" else None)
    assert detail["upstream_phase"] == ("response_decode" if failure == "invalid-json" else "request")
    assert private not in json.dumps(detail)


def test_first_startup_failure_is_not_overwritten_or_treated_as_stop(tmp_path):
    supervisor = AssistantSupervisor(tmp_path, runtime_path=tmp_path / "never-started")
    error = Problem(503, "ASSISTANT_API_UNKNOWN", "private", upstream_status=412,
                    upstream_phase="response_status", authorization="private")
    assert supervisor.record_failure("original-launch", "acquire", error)
    assert not supervisor.record_failure("original-launch", "acquire", RuntimeError("different private"))
    with supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id='original-launch'").fetchone()
        assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    assert json.loads(raw)["upstream_status"] == 412
    assert "private" not in raw


@pytest.mark.parametrize("code", sorted(hosted.SKILL_ERRORS))
def test_restore_skill_refusal_keeps_only_typed_safe_journal_diagnostics(tmp_path, caplog, code):
    private = "SYNTHETIC_PRIVATE_RESTORE_CANARY"
    status = 413 if code == "SKILL_CATALOG_LIMIT" else 403
    client = AssistantClient(
        "https://context.invalid", SimpleNamespace(binding_id="binding", session_id="a" * 64),
        SecretStr("b" * 64), token_provider=lambda: private,
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text=private, headers={
            "x-behavior-assistant-error-code": code,
            "x-behavior-assistant-error-phase": "assistant_authority",
            "x-private-body": private,
        })),
    )
    try:
        with pytest.raises(Problem) as refused:
            client.restore()
    finally:
        client.close()
    assert refused.value.status == status
    supervisor = AssistantSupervisor(tmp_path / "journal", runtime_path=tmp_path / "never-started")
    assert supervisor.record_failure("original-launch", "restore", refused.value)
    with supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id='original-launch'").fetchone()
        assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    data = json.loads(raw)
    assert data["code"] == code and data["phase"] == "restore"
    assert data["upstream_status"] == status and data["upstream_phase"] == "response_status"
    assert data["upstream_error_code"] == code
    assert data["upstream_error_phase"] == "assistant_authority"
    assert set(data) == {"phase", "code", "upstream_phase", "upstream_status", "exception_type",
                         "recorded_at", "upstream_error_code", "upstream_error_phase"}
    assert private not in raw and private not in caplog.text


def test_untrusted_error_details_cannot_become_diagnostic_fields(tmp_path, caplog):
    supervisor = AssistantSupervisor(tmp_path, runtime_path=tmp_path / "never-started")
    private = "SYNTHETIC_PRIVATE_CANARY"
    error = Problem(503, private, private, upstream_phase=private, upstream_status=True, exception_type=private)
    assert supervisor.record_failure("original-launch", private, error)
    with supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id='original-launch'").fetchone()
    data = json.loads(raw)
    assert data["phase"] == "unknown" and data["code"] == "UNCLASSIFIED"
    assert data["upstream_phase"] is None and data["upstream_status"] is None
    assert data["exception_type"] == "Problem"
    assert private not in raw and private not in caplog.text


def test_exception_class_name_is_not_copied_to_diagnostics(tmp_path, caplog):
    supervisor = AssistantSupervisor(tmp_path, runtime_path=tmp_path / "never-started")
    private = "SYNTHETIC_PRIVATE_CANARY"
    error_type = type(private, (RuntimeError,), {})
    assert supervisor.record_failure("original-launch", "load_engine", error_type(private))
    with supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id='original-launch'").fetchone()
    assert json.loads(raw)["exception_type"] == "UNCLASSIFIED"
    assert private not in raw and private not in caplog.text


def test_diagnostic_storage_failure_is_reported_without_exposing_exception(tmp_path, monkeypatch, caplog):
    supervisor = AssistantSupervisor(tmp_path, runtime_path=tmp_path / "never-started")

    def failed_storage():
        raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_CANARY")

    monkeypatch.setattr(supervisor.journal, "connect", failed_storage)
    assert not supervisor.record_failure("original-launch", "acquire", RuntimeError("private"))
    assert "ASSISTANT_DIAGNOSTIC_WRITE_FAILED" in caplog.text
    assert "diagnostic_saved=False" in caplog.text
    assert "SYNTHETIC_PRIVATE_CANARY" not in caplog.text
