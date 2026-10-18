"""Owner API version following; platform I/O and the remote N1 transport are offline."""

import json
from types import SimpleNamespace

import httpx
import pytest
from azure.ai.projects.models import AgentSessionResource
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.behavior_native_sessions import (
    NativeSessions, _read_platform_session as sdk_read_session, _latest_version_reader as sdk_latest_reader,
)
from shared_context.behavior_native_turn_wire import NativeEnvironmentView, NativeSessionState
from shared_context.native_files_wire import (
    FILES_PROFILE, NativeFilesListing, NativeFilesManifest, NativeFilesProgress, NativeFilesView,
)
from shared_context.errors import Problem
from test_native_turn_api import PREFIX, NativeTransport, native_api
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token
from test_native_session_prepare_api import project_client

sdk_stop_session = FoundryHttpClient.stop_session


def environment_at(case, monkeypatch):
    source = case.remote.target
    calls, sessions, stopped = [], {source.session_id: source}, set()
    binding, original = [source.session_id], case.remote.preparation
    view = [None]

    def files_for(target, rid):
        # These original stop/retarget cases start with confirmed empty files; real transfers have their own fixture.
        return NativeFilesManifest(
            agent_id=source.agent_id, request_id=rid, source_session_id=source.session_id,
            source_agent_version=source.agent_version, target_session_id=target.session_id,
            target_agent_version=target.agent_version, root_mode=0o700, entries=())

    def get(client):
        calls.append(("get", client.target.session_id))
        return sessions.get(client.target.session_id)

    def create(client, *, before_send):
        before_send()
        assert client.credential is case.foundry.credential
        calls.append(("create", client.target.session_id))
        sessions[client.target.session_id] = client.target

    def stop(client):
        assert client.target.session_id == source.session_id
        calls.append(("stop", client.target.session_id))
        stopped.add(client.target.session_id)

    def status(agent_name, sid):
        if sid not in sessions:
            raise ResourceNotFoundError("the requested session does not exist")
        target = sessions[sid]
        assert agent_name == target.agent_name
        calls.append(("status", sid))
        return AgentSessionResource({
            "agent_session_id": sid,
            "version_indicator": {"type": "version_ref", "agent_version": target.agent_version},
            "status": "idle" if sid in stopped else "active",
        })

    def remote(request):
        sid = request.url.params["agent_session_id"]
        target = sessions[sid]
        command = json.loads(request.content)
        calls.append((command["operation"], sid))
        if command["operation"] == "session_read":
            value = NativeSessionState(
                agent_id=source.agent_id, hosted_session_id=binding[0], preparation=original,
                etag="original-etag" if view[0] is None else "following-etag", environment=view[0],
                files_profile=FILES_PROFILE)
        elif command["operation"] == "session_files":
            manifest = files_for(target, command["request_id"])
            value = NativeFilesView(
                **{name: getattr(manifest, name) for name in (
                    "agent_id", "request_id", "source_session_id", "source_agent_version",
                    "target_session_id", "target_agent_version")},
                progress=NativeFilesProgress(
                    manifest=manifest, source_verified=True, stop_started=True,
                    source_listing=NativeFilesListing(directory=1, complete=True),
                    stopped_listing=NativeFilesListing(directory=1, complete=True),
                    source_stopped_verified=True, publishing=True, published=True))
        elif command["operation"] == "session_retarget":
            assert view[0] is not None
            if (view[0].request_id, view[0].hosted_session_id) != (command["request_id"], sid):
                assert view[0].state == "pending"
                assert (command["pending_request_id"], command["pending_session_id"]) == (
                    view[0].request_id, view[0].hosted_session_id)
                assert command["expected_session_id"] == source.session_id
                view[0] = view[0].model_copy(update={
                    "request_id": command["request_id"], "hosted_session_id": sid,
                    "hosted_agent_version": target.agent_version, "reason": "source_stop_required",
                    "files_manifest_sha256": files_for(target, command["request_id"]).sha256,
                })
            value = view[0]
        elif command["operation"] == "session_follow":
            if view[0] is None:
                assert command["stage"] == "prepare"
                assert command["expected_session_id"] == source.session_id
                assert command["expected_etag"] == "original-etag"
                view[0] = NativeEnvironmentView(
                    agent_id=source.agent_id, request_id=command["request_id"],
                    previous_hosted_session_id=source.session_id, previous_agent_version=source.agent_version,
                    hosted_session_id=sid, hosted_agent_version=target.agent_version,
                    state="pending", reason="source_stop_required",
                    files_manifest_sha256=files_for(target, command["request_id"]).sha256)
            if command["stage"] == "commit":
                assert source.session_id in stopped
                binding[0] = sid
                view[0] = view[0].model_copy(update={"state": "ready", "reason": None})
            value = view[0]
        elif command["operation"] == "continuation_ready":
            from shared_context.behavior_native_turn_wire import continuation_ready_id
            from shared_context.native_continuation_wire import NativeContinuationReadyReceipt
            assert view[0] is not None and view[0].state == "ready"
            assert (command["environment_request_id"], command["hosted_session_id"],
                    command["files_manifest_sha256"]) == (
                        view[0].request_id, sid, view[0].files_manifest_sha256)
            assert command["request_id"] == continuation_ready_id(
                view[0].request_id, sid, view[0].files_manifest_sha256)
            value = NativeContinuationReadyReceipt(
                agent_id=source.agent_id, hosted_session_id=sid, request_id=command["request_id"],
                environment_request_id=view[0].request_id, pending_request_ids=())
        else:
            raise AssertionError("Environment preparation must not submit a turn or account usage")
        return httpx.Response(200, content=value.model_dump_json().encode(), headers={
            "content-type": "application/json", "x-agent-session-id": sid,
            "x-agent-invocation-id": "offline-environment",
        })

    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "42")
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", get)
    monkeypatch.setattr("shared_context.behavior_native_sessions._require_native_version", lambda _: None)
    monkeypatch.setattr(FoundryHttpClient, "create_session", create)
    monkeypatch.setattr(FoundryHttpClient, "stop_session", stop)
    project_client(monkeypatch, get_session=status,
                   list_sessions=lambda name, **_: iter(status(name, sid) for sid in sessions))
    original_http = case.foundry.http
    class EnvironmentTransport(httpx.BaseTransport):
        def handle_request(self, request):
            return remote(request)
    monkeypatch.setattr(original_http, "_transport", EnvironmentTransport())
    return source, calls, sessions, view


def test_owner_explicit_follow_stops_original_and_replays_after_api_restart(native_api, monkeypatch):
    case = native_api
    source, calls, sessions, view = environment_at(case, monkeypatch)
    before = case.c.control.store._guard()
    command = {"request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42"}
    response = case.client.post(PREFIX + "/environment", json=command, headers=case.headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["state"] == "ready" and result["hosted_agent_version"] == "42"
    assert result["previous_hosted_session_id"] == source.session_id
    assert result["hosted_session_id"] != source.session_id
    assert result["scope"] == "entire-agent"
    assert result["model_started"] is False and result["original_work_replayed"] is False
    assert len(sessions) == 2 and sum(action == "create" for action, _ in calls) == 1
    assert sum(action == "stop" for action, _ in calls) == 1
    assert case.c.control.store._guard() == before
    case.composition._native_sessions = NativeSessions(case.composition)
    repeated = case.client.post(PREFIX + "/environment", json=command, headers=case.headers)
    assert repeated.status_code == 200 and repeated.json() == result
    assert sum(action == "create" for action, _ in calls) == 1
    assert sum(action == "stop" for action, _ in calls) == 1
    assert view[0].request_id == "follow-original"
    case.composition._native_sessions = NativeSessions(case.composition)
    preparation = case.client.post(PREFIX + "/prepare", headers=case.headers, json={"request_id": "not-a-new-bootstrap"})
    assert preparation.status_code == 200, preparation.text
    assert preparation.json() == case.remote.preparation.model_dump(mode="json")
    assert sum(action == "create" for action, _ in calls) == 1


def test_pending_follow_refuses_the_original_sid_once_its_observed_version_changed(native_api, monkeypatch):
    # d53a0292: FollowFiles.require_source_version runs before before_stop; a metadata-proven source drift is refused
    # with no stop and no Files read (FA-AGENT-BEHAVIOR-SPEC 2026-10-04 "正常Files階段先核source版本").
    case = native_api
    source, calls, sessions, view = environment_at(case, monkeypatch)
    original_preparation = case.remote.preparation
    sessions[source.session_id] = source.model_copy(update={"agent_version": "42"})
    response = case.client.post(PREFIX + "/environment", headers=case.headers, json={
        "request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42",
    })
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "NATIVE_FILES_SOURCE_UNAVAILABLE"
    assert not any(action in {"stop", "session_files"} for action, _ in calls)
    assert view[0].previous_agent_version == original_preparation.hosted_agent_version
    assert case.remote.preparation == original_preparation and case.remote.records == {}
    with pytest.raises(Problem) as strict:
        case.foundry.for_target(source).session_status()
    assert strict.value.status == 409 and strict.value.body["error"]["code"] == "CONVERSATION_SESSION_CHANGED"


@pytest.mark.parametrize("changed", [
    {"agent_session_id": "different-source"},
    {"version_indicator": {"type": "version_ref", "agent_version": "latest"}},
    {"version_indicator": {"type": "version_ref", "agent_version": None}},
    {"status": "unrecognized-state"},
    {"status": []},
])
def test_source_drift_tolerance_keeps_identity_version_shape_and_status_checks(native_api, monkeypatch, caplog, changed):
    case = native_api
    source, calls, _, view = environment_at(case, monkeypatch)

    def get_session(name, sid):
        assert (name, sid) == (source.agent_name, source.session_id)
        # The source reports its own version: since d53a0292 any other version is a proven drift (refused below).
        return AgentSessionResource({
            "agent_session_id": sid, "status": "active",
            "version_indicator": {"type": "version_ref", "agent_version": source.agent_version},
            "diagnostic": "PRIVATE-RESPONSE-MUST-NOT-BE-LOGGED", **changed,
        })

    project_client(monkeypatch, get_session=get_session)
    response = case.client.post(PREFIX + "/environment", headers=case.headers, json={
        "request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42",
    })
    if "status" in changed:
        assert response.status_code == 200 and response.json()["state"] == "pending"
        assert response.json()["reason"] == "source_unavailable" and view[0].state == "pending"
        assert "NATIVE_ENVIRONMENT_PENDING stage=source_status" in caplog.text
    else:
        # d53a0292: FollowFiles.require_source_version reads the same metadata first; _session_metadata refuses an
        # identity or concrete-version change before any stop or Files read.
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert not any(action == "stop" for action, _ in calls)
    assert "PRIVATE-RESPONSE" not in caplog.text


@pytest.mark.parametrize("phase,status", [
    ("precheck", 403), ("precheck", 503),
    ("source_status", 403), ("stop_session", 403), ("stop_session", 409), ("source_readback", 403),
])
def test_source_observation_and_stop_failures_remain_pending_with_distinct_phases(
        native_api, monkeypatch, caplog, phase, status):
    case = native_api
    source, _, _, view = environment_at(case, monkeypatch)
    requests = []

    def failure():
        error = HttpResponseError("PRIVATE-UPSTREAM-MESSAGE")
        error.status_code = status
        raise error

    def get_session(name, sid):
        assert (name, sid) == (source.agent_name, source.session_id)
        # d53a0292: FollowFiles.require_source_version reads the source metadata once before the status read.
        current_phase = "precheck" if not requests else "source_status" if "stop" not in requests else "source_readback"
        requests.append("get")
        if phase == current_phase:
            failure()
        return AgentSessionResource({
            "agent_session_id": sid, "status": "active",
            "version_indicator": {"type": "version_ref", "agent_version": source.agent_version},
        })

    def stop_session(name, sid):
        assert (name, sid) == (source.agent_name, source.session_id)
        requests.append("stop")
        if phase == "stop_session":
            failure()

    project_client(monkeypatch, get_session=get_session, stop_session=stop_session)
    monkeypatch.setattr(FoundryHttpClient, "stop_session", sdk_stop_session)
    response = case.client.post(PREFIX + "/environment", headers=case.headers, json={
        "request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42",
    })
    assert response.status_code == 200 and response.json()["state"] == "pending"
    assert response.json()["reason"] == "source_unavailable" and view[0].reason == "source_stop_required"
    assert requests.count("stop") == (0 if phase in {"precheck", "source_status"} else 1)
    assert f"NATIVE_ENVIRONMENT_PENDING stage={phase}" in caplog.text
    if phase == "stop_session":
        assert f"upstream_status={status}" in caplog.text
    assert "PRIVATE-UPSTREAM" not in caplog.text


def test_stop_acknowledgment_without_inactive_readback_does_not_complete_follow(native_api, monkeypatch):
    case = native_api
    source, calls, _, view = environment_at(case, monkeypatch)
    monkeypatch.setattr(FoundryHttpClient, "stop_session", lambda client: calls.append(("stop", client.target.session_id)))
    response = case.client.post(PREFIX + "/environment", headers=case.headers, json={
        "request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42",
    })
    assert response.status_code == 200 and response.json()["reason"] == "source_active"
    assert response.json()["state"] == view[0].state == "pending"
    assert calls.count(("stop", source.session_id)) == 1
    # d53a0292 added the pre-stop FollowFiles.require_source_version metadata read before status and readback.
    assert calls.count(("status", source.session_id)) == 3


def test_a_new_build_does_not_retarget_the_original_pending_operation(native_api, monkeypatch):
    case = native_api
    source, calls, sessions, view = environment_at(case, monkeypatch)

    def unavailable(_client):
        raise Problem(503, "FOUNDRY_SESSION_STOP_UNKNOWN", "original stop is unconfirmed", upstream_status=403)

    monkeypatch.setattr(FoundryHttpClient, "stop_session", unavailable)
    command = {"request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42"}
    response = case.client.post(PREFIX + "/environment", headers=case.headers, json=command)
    assert response.status_code == 200 and response.json()["state"] == "pending"
    original = view[0].model_dump(mode="json")
    before = list(calls)
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "43")
    case.composition._native_sessions = NativeSessions(case.composition)
    repeated = case.client.post(PREFIX + "/environment", headers=case.headers, json=command)
    assert repeated.status_code == 409 and repeated.json()["error"]["code"] == "NATIVE_ENVIRONMENT_CONFLICT"
    assert calls == before and len(sessions) == 2
    assert view[0].model_dump(mode="json") == original


def test_owner_explicit_retarget_uses_a_new_id_and_preserves_old_destination(native_api, monkeypatch):
    case = native_api
    source, calls, sessions, view = environment_at(case, monkeypatch)
    stop = FoundryHttpClient.stop_session

    def unavailable(_client):
        raise Problem(503, "FOUNDRY_SESSION_STOP_UNKNOWN", "original stop is unconfirmed")

    monkeypatch.setattr(FoundryHttpClient, "stop_session", unavailable)
    pending = case.client.post(PREFIX + "/environment", headers=case.headers, json={
        "request_id": "original-pending", "expected_session_id": source.session_id, "expected_agent_version": "42",
    })
    assert pending.status_code == 200 and pending.json()["state"] == "pending"
    original = view[0]
    old_target = sessions[original.hosted_session_id]
    monkeypatch.setattr(FoundryHttpClient, "stop_session", stop)
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "43")
    command = {
        "request_id": "new-explicit-operation", "expected_session_id": source.session_id, "expected_agent_version": "43",
        "pending_request_id": original.request_id, "pending_session_id": original.hosted_session_id,
    }
    result = case.client.post(PREFIX + "/environment/retarget", headers=case.headers, json=command)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["request_id"] == command["request_id"] and body["hosted_agent_version"] == "43"
    assert body["state"] == "ready" and body["original_work_replayed"] is False
    assert body["hosted_session_id"] not in {source.session_id, original.hosted_session_id}
    assert len(sessions) == 3 and sessions[old_target.session_id] == old_target
    assert original.state == "pending" and original.hosted_agent_version == "42"
    assert calls.count(("stop", source.session_id)) == 1
    assert case.remote.records == {}
    case.composition._native_sessions = NativeSessions(case.composition)
    repeated = case.client.post(PREFIX + "/environment/retarget", headers=case.headers, json=command)
    assert repeated.status_code == 200 and repeated.json() == body
    assert len(sessions) == 3 and calls.count(("stop", source.session_id)) == 1


def test_retarget_requires_owner_new_id_and_server_selected_destination(native_api, monkeypatch, signing_key):
    case = native_api
    source, calls, _, _ = environment_at(case, monkeypatch)
    command = {
        "request_id": "new-operation", "expected_session_id": source.session_id, "expected_agent_version": "42",
        "pending_request_id": "original-pending", "pending_session_id": "original-target",
    }
    headers = {"Authorization": "Bearer " + token(signing_key, {
        "oid": case.c.control.policy.template.agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",))}
    denied = case.client.post(PREFIX + "/environment/retarget", headers=headers, json=command)
    assert denied.status_code == 403 and calls == []
    repeated_id = case.client.post(PREFIX + "/environment/retarget", headers=case.headers, json={
        **command, "request_id": command["pending_request_id"],
    })
    assert repeated_id.status_code == 422 and calls == []
    for field in ("agent_id", "target_session_id", "api_client_id", "drained"):
        response = case.client.post(PREFIX + "/environment/retarget", headers=case.headers,
                                    json={**command, field: "caller-supplied"})
        assert response.status_code == 422 and calls == []


def test_explicit_follow_reads_authority_from_new_version_not_an_uninvokable_old_sid(native_api, monkeypatch):
    case = native_api
    source, calls, _, _ = environment_at(case, monkeypatch)
    case.composition._native_sessions = NativeSessions(case.composition)
    original_http = case.foundry.http
    old_invocations = []

    def transport(request):
        if request.url.params["agent_session_id"] == source.session_id:
            old_invocations.append(request)
            return httpx.Response(403, json={"error": {"code": "original-session-refused"}})
        return original_http.send(request)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        case.foundry.http = client
        response = case.client.post(PREFIX + "/environment", headers=case.headers, json={
            "request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42",
        })
    case.foundry.http = original_http
    assert response.status_code == 200 and response.json()["state"] == "ready", response.text
    assert old_invocations == []
    assert sum(action == "stop" for action, _ in calls) == 1


@pytest.mark.parametrize("change", [
    {"previous_hosted_session_id": "not-the-approved-source"},
    {"hosted_session_id": "not-the-server-destination"},
    {"hosted_agent_version": "99"},
])
def test_follow_response_cannot_authorize_stopping_another_session(native_api, monkeypatch, change):
    case = native_api
    source, calls, _, _ = environment_at(case, monkeypatch)
    original = case.foundry.http

    def changed(request):
        response = original.send(request)
        command = json.loads(request.content)
        if command.get("operation") == "session_follow" and command["stage"] == "prepare":
            return httpx.Response(200, json={**response.json(), **change}, headers=response.headers)
        return response

    with httpx.Client(transport=httpx.MockTransport(changed)) as client:
        case.foundry.http = client
        response = case.client.post(PREFIX + "/environment", headers=case.headers, json={
            "request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42",
        })
    case.foundry.http = original
    assert response.status_code in {409, 502}
    assert not any(action == "stop" for action, _ in calls)


def test_next_build_cold_reader_finds_published_environment_without_creating_one(native_api, monkeypatch):
    case = native_api
    source, calls, sessions, _ = environment_at(case, monkeypatch)
    result = case.client.post(PREFIX + "/environment", headers=case.headers, json={
        "request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42",
    })
    assert result.status_code == 200
    published = sessions[result.json()["hosted_session_id"]]
    case.composition._native_sessions = NativeSessions(case.composition)
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "43")
    monkeypatch.setattr("shared_context.behavior_native_sessions._latest_version_reader", lambda *_: published)
    original_http = case.foundry.http

    def transport(request):
        if request.url.params["agent_session_id"] == source.session_id:
            return httpx.Response(403, json={"error": {"code": "old-version-unreachable"}})
        return original_http.send(request)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        case.foundry.http = client
        response = case.client.get(PREFIX + "/preparation", headers=case.headers)
    case.foundry.http = original_http
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "needs_follow"
    assert response.json()["preparation_verified"] is False
    assert published.session_id in {item["hosted_session_id"] for item in response.json()["observed_sessions"]}
    assert sum(action == "create" for action, _ in calls) == 1


@pytest.mark.parametrize("denied", [True, False])
def test_stop_denial_or_lost_ack_stays_pending_and_never_changes_original_requests(native_api, monkeypatch, denied):
    case = native_api
    source, calls, _, view = environment_at(case, monkeypatch)
    original_stop = FoundryHttpClient.stop_session

    def stop(client):
        if denied:
            raise Problem(503, "FOUNDRY_SESSION_UNKNOWN", "original permission denied", upstream_status=403)
        original_stop(client)
        raise Problem(503, "FOUNDRY_SESSION_UNKNOWN", "original stop acknowledgement lost")

    monkeypatch.setattr(FoundryHttpClient, "stop_session", stop)
    command = {"request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42"}
    result = case.client.post(PREFIX + "/environment", headers=case.headers, json=command)
    assert result.status_code == 200 and result.json()["state"] == "pending"
    assert result.json()["reason"] == "source_unavailable"
    assert view[0].state == "pending" and case.remote.records == {}
    if not denied:
        case.composition._native_sessions = NativeSessions(case.composition)
        completed = case.client.post(PREFIX + "/environment", headers=case.headers, json=command)
        assert completed.status_code == 200 and completed.json()["state"] == "ready"
        assert sum(action == "create" for action, _ in calls) == sum(action == "stop" for action, _ in calls) == 1


@pytest.mark.parametrize("retired_status", ["failed", "expired"])
def test_retired_bootstrap_does_not_hide_the_published_version(native_api, monkeypatch, retired_status):
    from azure.ai.projects.models import AgentSessionResource
    from azure.core.exceptions import ResourceNotFoundError
    from test_native_session_prepare_api import project_client

    case = native_api
    source, calls, sessions, _ = environment_at(case, monkeypatch)
    prepared = case.client.post(PREFIX + "/environment", headers=case.headers, json={
        "request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42",
    })
    assert prepared.status_code == 200
    published = sessions[prepared.json()["hosted_session_id"]]
    reads = []

    def model(target, status):
        return AgentSessionResource({
            "agent_session_id": target.session_id, "status": status,
            "version_indicator": {"type": "version_ref", "agent_version": target.agent_version},
        })

    def get_session(name, sid):
        assert name == source.agent_name
        reads.append(sid)
        if sid == source.session_id:
            return model(source, retired_status)
        if sid == published.session_id:
            return model(published, "active")
        raise ResourceNotFoundError("No current-version session has been created")

    def list_sessions(name, *, limit, order):
        assert (name, limit, order) == (source.agent_name, 100, "desc")
        reads.append("list")
        return [model(published, "active")]

    project_client(monkeypatch, get_session=get_session, list_sessions=list_sessions)
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", sdk_read_session)
    monkeypatch.setattr("shared_context.behavior_native_sessions._latest_version_reader", sdk_latest_reader)
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "43")
    case.composition._native_sessions = NativeSessions(case.composition)
    response = case.client.get(PREFIX + "/preparation", headers=case.headers)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "needs_follow" and response.json()["preparation_verified"] is False
    assert published.session_id in {item["hosted_session_id"] for item in response.json()["observed_sessions"]}
    assert "list" in reads
    assert sum(action == "create" for action, _ in calls) == 1
