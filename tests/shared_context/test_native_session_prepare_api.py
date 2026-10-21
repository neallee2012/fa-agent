"""Owner -> API-MI platform session -> explicit N1 bootstrap, with cloud transport replaced."""

import json
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.behavior_native_sessions import (
    NativeSessions, native_session_id, _read_platform_session as sdk_read_session,
)
from shared_context.errors import Problem
from test_native_turn_api import native_api, PREFIX, submit
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token
from test_behavior_foundry import TARGET, Credential


def platform_sessions(monkeypatch, targets):
    from azure.ai.projects.models import AgentSessionResource
    from azure.core.exceptions import ResourceNotFoundError

    def get_session(name, sid):
        target = targets.get(sid)
        if target is None:
            raise ResourceNotFoundError("no such platform session")
        assert name == target.agent_name
        return AgentSessionResource({
            "agent_session_id": sid, "status": "active",
            "version_indicator": {"type": "version_ref", "agent_version": target.agent_version}})

    project_client(monkeypatch, get_session=get_session,
                   list_sessions=lambda name, **_: iter(get_session(name, sid) for sid in targets))


def project_client(monkeypatch, **operations):
    calls = []
    class Project:
        def __init__(self, *, endpoint, credential, **kwargs):
            calls.append((endpoint, credential, kwargs))
            self.agents = SimpleNamespace(**operations)
        def __enter__(self): return self
        def __exit__(self, *_): pass
    monkeypatch.setattr("azure.ai.projects.AIProjectClient", Project)
    return calls


def new_environment(case, monkeypatch, *, lost_create=False, invisible=False):
    case.composition._native_sessions = NativeSessions(case.composition)
    case.remote.preparation = None
    created, calls = {}, []

    def read(client):
        return created.get(client.target.session_id)

    def create(client, *, before_send):
        assert client.credential is case.foundry.credential
        assert client.target.session_id == native_session_id(
            case.c.native.targets[0], case.composition.config.identity.managed_identity_client_id, version="41")
        assert client.target.session_id != case.c.native.targets[0].session_id
        before_send()
        calls.append(client.target)
        case.remote.target = client.target
        if not invisible:
            created[client.target.session_id] = client.target
        if lost_create or invisible:
            raise Problem(503, "FOUNDRY_SESSION_UNKNOWN", "synthetic create acknowledgment lost")
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", read)
    monkeypatch.setattr("shared_context.behavior_native_sessions._require_native_version", lambda client: None)
    monkeypatch.setattr(FoundryHttpClient, "create_session", create)
    platform_sessions(monkeypatch, created)
    return created, calls


@pytest.mark.parametrize("reader_exists", [False, True])
def test_unprepared_bootstrap_uses_current_build_reader_and_preserves_old_session(
        native_api, monkeypatch, reader_exists):
    case = native_api
    case.composition._native_sessions = NativeSessions(case.composition)
    case.remote.preparation = None
    old = case.remote.target
    original_etag = case.remote.session_etag
    current = old.model_copy(update={
        "agent_version": "42",
        "session_id": native_session_id(
            case.c.native.targets[0], case.composition.config.identity.managed_identity_client_id,
            version="42"),
    })
    sessions, created = {old.session_id: old}, []
    if reader_exists:
        sessions[current.session_id] = current

    def read(client):
        actual = sessions.get(client.target.session_id)
        if actual is not None:
            case.remote.target = actual
        return actual

    def create(client, *, before_send):
        assert client.credential is case.foundry.credential
        assert client.target == current
        before_send()
        created.append(client.target)
        sessions[current.session_id] = current

    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "42")
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", read)
    monkeypatch.setattr("shared_context.behavior_native_sessions._require_native_version", lambda _: None)
    monkeypatch.setattr(FoundryHttpClient, "create_session", create)
    platform_sessions(monkeypatch, sessions)
    snapshot = case.client.get(PREFIX + "/preparation", headers=case.headers)
    assert snapshot.status_code == 200 and snapshot.json()["preparation_verified"] is False
    assert submit(case).status_code == 409
    reconciled = case.client.post(PREFIX + "/reconcile", headers=case.headers,
                                 json={"request_id": "native-rid", "content": "Original user text"})
    assert reconciled.status_code == (404 if reader_exists else 409)
    assert not created and all(command.operation in {"session_read", "read"} for _, command in case.remote.calls)
    response = case.client.post(PREFIX + "/prepare", headers=case.headers,
                                json={"request_id": "same-build-bootstrap"})
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert prepared["hosted_agent_version"] == "42"
    assert prepared["hosted_session_id"] == current.session_id
    assert prepared["previous_hosted_session_id"] == old.session_id
    assert prepared["model_started"] is False
    case.composition._native_sessions = NativeSessions(case.composition)
    replay = case.client.post(PREFIX + "/prepare", headers=case.headers,
                              json={"request_id": "same-build-bootstrap"})
    assert replay.status_code == 200 and replay.json() == prepared
    assert sessions[old.session_id] == old
    assert created == ([] if reader_exists else [current])
    writes = [(request, command) for request, command in case.remote.calls
              if command.operation == "session_prepare"]
    assert len(writes) == 1
    request, command = writes[0]
    assert request.url.params["agent_session_id"] == current.session_id
    assert (command.expected_session_id, command.expected_etag) == (old.session_id, original_etag)
    assert all(command.operation in {"read", "session_read", "session_prepare"} for _, command in case.remote.calls)


def test_new_build_prepare_never_invokes_the_original_version_session_nor_creates_one(native_api, monkeypatch):
    case = native_api
    created, calls = new_environment(case, monkeypatch)
    prepared = case.client.post(PREFIX + "/prepare", headers=case.headers,
                                json={"request_id": "original-version-bootstrap"})
    assert prepared.status_code == 200, prepared.text
    original = case.remote.target
    case.composition._native_sessions = NativeSessions(case.composition)
    case.remote.calls.clear()
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "42")
    monkeypatch.setattr("shared_context.behavior_native_sessions._latest_version_reader", lambda *_: original)

    def create(client, *, before_send):
        before_send()
        calls.append(client.target)
        created[client.target.session_id] = client.target
        case.remote.target = client.target

    monkeypatch.setattr(FoundryHttpClient, "create_session", create)
    repeated = case.client.post(PREFIX + "/prepare", headers=case.headers,
                                json={"request_id": "not-an-implicit-upgrade"})
    # Only the 41 session exists: invoking it would let the platform bring it back; the follow creates 42's.
    assert (repeated.status_code, repeated.json()["error"]["code"]) == (503, "AGENT_VERSION_FOLLOWING"), repeated.text
    assert len(created) == len(calls) == 1
    assert case.remote.session_binding == original.session_id
    assert case.remote.calls == []


def test_existing_fixed_bootstrap_preparation_is_not_invoked_on_new_build(native_api, monkeypatch):
    case = native_api
    original = case.remote.preparation.model_dump(mode="json")
    case.composition._native_sessions = NativeSessions(case.composition)
    created = []
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "42")
    monkeypatch.setattr("shared_context.behavior_native_sessions._require_native_version", lambda _: None)
    monkeypatch.setattr(FoundryHttpClient, "create_session", lambda client, **_: created.append(client.target))
    monkeypatch.setattr("shared_context.behavior_native_sessions._latest_version_reader", lambda *_: case.remote.target)
    response = case.client.post(PREFIX + "/prepare", headers=case.headers,
                                json={"request_id": "keep-original-preparation"})
    assert (response.status_code, response.json()["error"]["code"]) == (503, "AGENT_VERSION_FOLLOWING"), response.text
    assert not created
    assert case.remote.calls == []
    assert case.remote.preparation.model_dump(mode="json") == original


@pytest.mark.parametrize("code", ["NATIVE_BOOTSTRAP_HAS_WORK", "NATIVE_BOOTSTRAP_CONFLICT"])
def test_n1_refusal_preserves_original_work_without_fallback(native_api, monkeypatch, code):
    case = native_api
    assert submit(case, rid="original-work").status_code == 202
    original_records = dict(case.remote.records)
    original_binding, original_etag = case.remote.session_binding, case.remote.session_etag
    _, created = new_environment(case, monkeypatch)
    rejected = []
    original_http = case.foundry.http

    def send(request):
        if json.loads(request.content)["operation"] == "session_prepare":
            rejected.append(request)
            return httpx.Response(409, json={"error": {"code": code}})
        return case.remote(request)

    try:
        with httpx.Client(transport=httpx.MockTransport(send)) as client:
            case.foundry.http = client
            response = case.client.post(PREFIX + "/prepare", headers=case.headers,
                                        json={"request_id": "do-not-replace-original-work"})
    finally:
        case.foundry.http = original_http
    assert response.status_code == 409 and response.json()["error"]["code"] == code
    assert len(created) == len(rejected) == 1
    assert rejected[0].url.params["agent_session_id"] == created[0].session_id
    assert case.remote.records == original_records and case.remote.preparation is None
    assert (case.remote.session_binding, case.remote.session_etag) == (original_binding, original_etag)


@pytest.mark.parametrize("readback,status,code", [
    ("wrong-sid", 409, "NATIVE_SESSION_CHANGED"),
    ("wrong-version", 409, "NATIVE_SESSION_CHANGED"),
    ("not-ready", 503, "NATIVE_BOOTSTRAP_UNKNOWN"),
    ("missing", 503, "NATIVE_BOOTSTRAP_UNKNOWN"),
])
def test_bootstrap_requires_exact_current_version_readback(native_api, monkeypatch, readback, status, code):
    from azure.ai.projects.models import AgentSessionResource
    from azure.core.exceptions import ResourceNotFoundError

    case = native_api
    created, calls = new_environment(case, monkeypatch)

    def get_session(name, sid):
        target = created.get(sid)
        if target is None or readback == "missing":
            raise ResourceNotFoundError("synthetic session not visible")
        assert name == target.agent_name
        return AgentSessionResource({
            "agent_session_id": "different-session" if readback == "wrong-sid" else sid,
            "status": "creating" if readback == "not-ready" else "active",
            "version_indicator": {
                "type": "version_ref", "agent_version": "40" if readback == "wrong-version" else "41",
            },
        })

    project_client(monkeypatch, get_session=get_session)
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", sdk_read_session)
    for _ in range(2):
        response = case.client.post(PREFIX + "/prepare", headers=case.headers,
                                    json={"request_id": "exact-reader-only"})
        assert response.status_code == status and response.json()["error"]["code"] == code
    assert len(calls) == 1 and not case.remote.calls
    assert case.remote.preparation is None


@pytest.mark.parametrize("lost_create", [False, True])
def test_prepare_uses_original_mi_and_same_agent_returns_same_sid_across_restart(native_api, monkeypatch, lost_create):
    case = native_api
    created, calls = new_environment(case, monkeypatch, lost_create=lost_create)
    before = case.c.control.store._guard()
    response = case.client.post(PREFIX + "/prepare", json={"request_id": "prepare-once"}, headers=case.headers)
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["state"] == "ready" and first["model_started"] is False
    assert len(calls) == 1 and len(created) == 1
    assert case.c.control.store._guard() == before and not case.c.control.records
    assert {command.operation for _, command in case.remote.calls} == {"session_read", "session_prepare"}
    assert first["hosted_session_id"] == calls[0].session_id
    assert first["hosted_agent_version"] == "41"
    case.composition._native_sessions = NativeSessions(case.composition)
    repeat = case.client.post(PREFIX + "/prepare", json={"request_id": "another-request"}, headers=case.headers)
    assert repeat.status_code == 200 and repeat.json() == first and len(calls) == 1
    assert sum(command.operation == "session_prepare" for _, command in case.remote.calls) == 1
    assert submit(case).status_code == 202


def test_unknown_create_without_readback_does_not_create_another_or_invoke(native_api, monkeypatch):
    case = native_api
    _, calls = new_environment(case, monkeypatch, invisible=True)
    for _ in range(2):
        response = case.client.post(PREFIX + "/prepare", json={"request_id": "prepare-once"}, headers=case.headers)
        assert response.status_code == 503 and response.json()["error"]["code"] == "NATIVE_BOOTSTRAP_UNKNOWN"
        assert response.json()["error"]["reconcile_original"] and not response.json()["error"]["automatic_retry"]
    assert len(calls) == 1 and case.remote.calls == []
    assert submit(case).status_code == 409
    assert case.client.get(PREFIX + "/requests/absent", headers=case.headers).status_code == 409
    assert len(calls) == 1 and case.remote.calls == []


def test_explicit_prepare_after_restart_uses_only_original_named_session(native_api, monkeypatch):
    case = native_api
    created, calls = new_environment(case, monkeypatch)
    visible = False
    def read(client):
        return created.get(client.target.session_id) if visible else None
    def create(client, *, before_send):
        nonlocal visible
        before_send()
        calls.append(client.target)
        sid = client.target.session_id
        if sid in created:
            visible = True
            raise Problem(503, "FOUNDRY_SESSION_UNKNOWN", "synthetic duplicate named session", upstream_status=409)
        created[sid] = client.target
        case.remote.target = client.target
        raise Problem(503, "FOUNDRY_SESSION_UNKNOWN", "synthetic lost create acknowledgement")
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", read)
    monkeypatch.setattr(FoundryHttpClient, "create_session", create)
    first = case.client.post(PREFIX + "/prepare", json={"request_id": "prepare-once"}, headers=case.headers)
    assert first.status_code == 503 and len(calls) == 1 and not case.remote.calls
    case.composition._native_sessions = NativeSessions(case.composition)
    assert submit(case).status_code == 409 and len(calls) == 1
    second = case.client.post(PREFIX + "/prepare", json={"request_id": "prepare-once"}, headers=case.headers)
    assert second.status_code == 200, second.text
    assert len(calls) == 2 and len(created) == 1 and calls[0] == calls[1]
    assert second.json()["hosted_session_id"] == calls[0].session_id
    assert second.json()["model_started"] is False
    assert all(command.operation in {"session_read", "session_prepare"} for _, command in case.remote.calls)


@pytest.mark.parametrize("reader_exists", [False, True])
def test_revocation_before_bootstrap_send_does_not_write_or_claim_create(native_api, monkeypatch, reader_exists):
    from shared_context.behavior_native_turn_api import NativeTurnGateway
    case = native_api
    created, calls = new_environment(case, monkeypatch)
    if reader_exists:
        target = case.composition._native_sessions.candidate(case.foundry, version="41").target
        created[target.session_id] = target
        case.remote.target = target
    original = NativeTurnGateway._context
    checks = 0
    def context(gateway, *args, **kwargs):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise Problem(403, "WORK_POLICY_REVOKED", "synthetic revocation before create")
        return original(gateway, *args, **kwargs)
    monkeypatch.setattr(NativeTurnGateway, "_context", context)
    refused = case.client.post(PREFIX + "/prepare", json={"request_id": "prepare-once"}, headers=case.headers)
    assert refused.status_code == 403 and calls == []
    assert not case.composition._native_sessions.environment_creates
    assert all(command.operation == "session_read" for _, command in case.remote.calls)
    monkeypatch.setattr(NativeTurnGateway, "_context", original)
    prepared = case.client.post(PREFIX + "/prepare", json={"request_id": "prepare-once"}, headers=case.headers)
    assert prepared.status_code == 200 and len(calls) == (0 if reader_exists else 1)


def test_native_turns_never_implicitly_prepare_or_switch_on_403(native_api, monkeypatch):
    case = native_api
    _, calls = new_environment(case, monkeypatch)
    for response in (submit(case), case.client.get(PREFIX + "/requests/absent", headers=case.headers)):
        assert response.status_code == 409 and response.json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert calls == [] and case.remote.calls == []
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", lambda _: (_ for _ in ()).throw(
        Problem(403, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE", "original access denied")))
    result = case.client.post(PREFIX + "/prepare", json={"request_id": "prepare-once"}, headers=case.headers)
    assert result.status_code == 403 and calls == []


def test_prepare_rpc_lost_ack_reads_original_agent_result_without_second_write(native_api, monkeypatch):
    case = native_api
    _, calls = new_environment(case, monkeypatch)
    original_http = case.foundry.http
    lost = False
    def send(request):
        nonlocal lost
        response = case.remote(request)
        if json.loads(request.content)["operation"] == "session_prepare" and not lost:
            lost = True
            raise httpx.ReadTimeout("synthetic preparation ack loss", request=request)
        return response
    try:
        with httpx.Client(transport=httpx.MockTransport(send)) as client:
            case.foundry.http = client
            first = case.client.post(PREFIX + "/prepare", json={"request_id": "prepare-once"}, headers=case.headers)
            assert first.status_code == 503
            second = case.client.post(PREFIX + "/prepare", json={"request_id": "prepare-once"}, headers=case.headers)
            assert second.status_code == 200
    finally:
        case.foundry.http = original_http
    assert len(calls) == 1 and sum(command.operation == "session_prepare" for _, command in case.remote.calls) == 1


def test_cold_api_reads_prepared_state_without_cloud_or_bootstrap_writes(native_api, monkeypatch):
    case = native_api
    _, calls = new_environment(case, monkeypatch)
    prepared = case.client.post(PREFIX + "/prepare", json={"request_id": "prepare-once"}, headers=case.headers)
    assert prepared.status_code == 200
    case.composition._native_sessions = NativeSessions(case.composition)
    case.remote.calls.clear()
    snapshot = case.client.get(PREFIX + "/preparation", headers=case.headers)
    assert snapshot.status_code == 200
    assert snapshot.json()["state"] == "unverified" and snapshot.json()["preparation_verified"] is False
    assert snapshot.json()["observed_sessions"][0]["hosted_session_id"] == prepared.json()["hosted_session_id"]
    assert len(calls) == 1 and not case.remote.calls


def test_create_permission_refusal_is_not_hidden_as_a_successful_prepare(native_api, monkeypatch):
    case = native_api
    created, calls = new_environment(case, monkeypatch)
    reads_after_denial = []
    def read(client):
        if calls:
            reads_after_denial.append(client.target)
        return created.get(client.target.session_id)
    def denied(client, *, before_send):
        before_send()
        calls.append(client.target)
        created[client.target.session_id] = client.target
        raise Problem(503, "FOUNDRY_SESSION_UNKNOWN", "original creation was refused", upstream_status=403)
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", read)
    monkeypatch.setattr(FoundryHttpClient, "create_session", denied)
    response = case.client.post(PREFIX + "/prepare", json={"request_id": "prepare-once"}, headers=case.headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE"
    assert len(calls) == 1 and not case.remote.calls and not reads_after_denial


def test_disabled_selected_version_does_not_create_a_native_session(native_api, monkeypatch):
    case = native_api
    _, calls = new_environment(case, monkeypatch)
    def disabled(_):
        raise Problem(409, "NATIVE_TURNS_DISABLED", "the selected version has no native flag")
    monkeypatch.setattr("shared_context.behavior_native_sessions._require_native_version", disabled)
    response = case.client.post(PREFIX + "/prepare", json={"request_id": "prepare-once"}, headers=case.headers)
    assert response.status_code == 409 and response.json()["error"]["code"] == "NATIVE_TURNS_DISABLED"
    assert calls == [] and case.remote.calls == []


def test_prepare_is_owner_only_and_body_cannot_choose_sid_or_privileges(native_api, monkeypatch, signing_key):
    case = native_api
    _, calls = new_environment(case, monkeypatch)
    for field in ("hosted_session_id", "agent_id", "expected_session_id", "api_client_id", "source_inactive"):
        response = case.client.post(PREFIX + "/prepare", headers=case.headers,
                                    json={"request_id": "prepare-once", field: "client-supplied"})
        assert response.status_code == 422
    agent_headers = {"Authorization": "Bearer " + token(signing_key, {
        "oid": case.c.control.policy.template.agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",))}
    denied = case.client.post(PREFIX + "/prepare", headers=agent_headers, json={"request_id": "prepare-once"})
    assert denied.status_code == 403 and calls == [] and case.remote.calls == []


@pytest.mark.parametrize("enabled", [True, False])
def test_selected_version_uses_fixed_projects_sdk_model_and_original_credential(monkeypatch, enabled):
    from azure.ai.projects.models import AgentVersionDetails
    from shared_context.behavior_native_sessions import _require_native_version
    credential = Credential()
    value = AgentVersionDetails({
        "name": TARGET.agent_name, "version": TARGET.agent_version,
        "definition": {"kind": "hosted", "environment_variables": {
            "FA_NATIVE_TURNS": "foundry-native-v2"} if enabled else {}},
    })
    def get_version(name, version):
        assert (name, version) == (TARGET.agent_name, TARGET.agent_version)
        return value
    calls = project_client(monkeypatch, get_version=get_version)
    client = FoundryHttpClient(TARGET, credential, transport=httpx.MockTransport(lambda _: pytest.fail("no invoke")))
    try:
        if enabled:
            _require_native_version(client)
        else:
            with pytest.raises(Problem) as denied:
                _require_native_version(client)
            assert denied.value.body["error"]["code"] == "NATIVE_TURNS_DISABLED"
    finally:
        client.close()
    assert calls[0][0] == TARGET.project_endpoint and calls[0][1] is credential
    assert calls[0][2]["retry_total"] == 1  # one bounded retry for this idempotent platform GET


@pytest.mark.parametrize("changes,error", [
    ({}, None),
    ({"status": "idle"}, None),
    ({"agent_session_id": "different-session"}, "NATIVE_SESSION_CHANGED"),
    ({"version_indicator": {"type": "version_ref", "agent_version": "latest"}}, "NATIVE_SESSION_CHANGED"),
    ({"status": "updating"}, "NATIVE_BOOTSTRAP_UNKNOWN"),
])
def test_platform_read_uses_fixed_projects_sdk_session_shape(monkeypatch, changes, error):
    from azure.ai.projects.models import AgentSessionResource
    from shared_context.behavior_native_sessions import _read_platform_session
    credential = Credential()
    value = AgentSessionResource({
        "agent_session_id": TARGET.session_id, "status": "active",
        "version_indicator": {"type": "version_ref", "agent_version": "41"}, **changes,
    })
    def get_session(name, sid):
        assert (name, sid) == (TARGET.agent_name, TARGET.session_id)
        return value
    calls = project_client(monkeypatch, get_session=get_session)
    client = FoundryHttpClient(TARGET, credential, transport=httpx.MockTransport(lambda _: pytest.fail("no invoke")))
    try:
        if error is None:
            result = _read_platform_session(client)
            assert result.session_id == TARGET.session_id and result.agent_version == "41"
        else:
            with pytest.raises(Problem) as denied:
                _read_platform_session(client)
            assert denied.value.body["error"]["code"] == error
    finally:
        client.close()
    assert len(calls) == 1 and calls[0][0] == TARGET.project_endpoint and calls[0][1] is credential
    assert calls[0][2]["retry_total"] == 1  # one bounded retry for this idempotent platform GET
