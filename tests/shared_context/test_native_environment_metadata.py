"""Signed owner metadata reads never invoke the old Hosted session."""

import httpx
import pytest
from azure.ai.projects.models import AgentSessionResource
from azure.core.exceptions import ResourceNotFoundError

from shared_context.behavior_native_sessions import (
    NativeSessions, _read_platform_session as sdk_read_session,
    _latest_version_reader as sdk_latest_reader, native_session_id,
)
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_native_session_prepare_api import project_client
from test_native_environment_api import environment_at
from test_native_turn_api import PREFIX, native_api


@pytest.mark.parametrize("path", ["/environment", "/preparation"])
def test_drifted_session_remains_visible_without_waking_it(native_api, monkeypatch, path):
    case = native_api
    case.composition._native_sessions = NativeSessions(case.composition)
    source_sid = native_session_id(
        case.foundry.target, case.composition.config.identity.managed_identity_client_id, version="3")
    source = AgentSessionResource({
        "agent_session_id": source_sid, "status": "idle",
        "version_indicator": {"type": "version_ref", "agent_version": "4"},
    })
    reads = []

    def get_session(agent_name, session_id):
        assert agent_name == case.foundry.target.agent_name
        reads.append(session_id)
        if session_id == source_sid:
            return source
        raise ResourceNotFoundError("the target version has no session")

    def list_sessions(agent_name, **kwargs):
        assert agent_name == case.foundry.target.agent_name
        assert kwargs["limit"] <= 100
        return iter((source,))

    def no_invoke(request):
        pytest.fail("a metadata read must not invoke, create, stop, or submit to Hosted")

    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", sdk_read_session)
    monkeypatch.setattr("shared_context.behavior_native_sessions._latest_version_reader", sdk_latest_reader)
    project_client(monkeypatch, get_session=get_session, list_sessions=list_sessions)
    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(no_invoke))

    response = case.client.get(PREFIX + path, headers=case.headers)

    assert response.status_code == 200, response.text
    metadata = response.json()
    assert metadata["schema_version"] == "fa-native-environment-metadata-v1"
    assert metadata["state_scope"] == "platform-metadata-only"
    assert metadata["state"] == "needs_follow"
    assert metadata["hosted_agent_version"] == "41"
    assert metadata["preparation_verified"] is False
    assert metadata["model_started"] is False
    assert metadata["original_work_replayed"] is False
    assert metadata["observed_sessions"] == [{
        "hosted_session_id": source_sid, "hosted_agent_version": "4", "status": "idle",
    }]
    assert reads


def test_confirmed_follow_resolves_source_from_new_reader_not_from_metadata(native_api, monkeypatch):
    case = native_api
    source, calls, sessions, _ = environment_at(case, monkeypatch)
    sessions[source.session_id] = source.model_copy(update={"agent_version": "42"})
    original = case.remote.preparation
    case.composition._native_sessions = NativeSessions(case.composition)
    preview = case.client.get(PREFIX + "/environment", headers=case.headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()["preparation_verified"] is False
    assert not any(action in {"session_read", "session_follow", "create", "stop"} for action, _ in calls)

    command = {"request_id": "metadata-follow-original", "expected_agent_version": "42"}
    followed = case.client.post(PREFIX + "/environment", headers=case.headers, json=command)
    # d53a0292: the source comes from the reader at its original version, so metadata reporting "42" for that SID is a
    # proven drift; FollowFiles.require_source_version refuses it before any stop or Files read.
    assert followed.status_code == 409, followed.text
    assert followed.json()["error"]["code"] == "NATIVE_FILES_SOURCE_UNAVAILABLE"
    case.composition._native_sessions = NativeSessions(case.composition)
    replay = case.client.post(PREFIX + "/environment", headers=case.headers, json=command)
    assert replay.status_code == 409 and replay.json() == followed.json()
    assert case.remote.preparation == original
    assert not any(action in {"stop", "session_files"} for action, _ in calls)
    assert sum(action == "create" for action, _ in calls) == 1
    assert not any(action == "session_read" and sid == source.session_id for action, sid in calls)
    assert not case.remote.records


@pytest.mark.parametrize("source_kind", ["versioned", "bootstrap"])
def test_receipt_read_cannot_wake_an_old_version_when_current_reader_is_missing(native_api, monkeypatch, source_kind):
    case = native_api
    case.composition._native_sessions = NativeSessions(case.composition)
    sid = native_session_id(case.foundry.target, case.composition.config.identity.managed_identity_client_id,
                            version="3" if source_kind == "versioned" else None)
    old = AgentSessionResource({
        "agent_session_id": sid, "status": "idle",
        "version_indicator": {"type": "version_ref", "agent_version": "3" if source_kind == "versioned" else "41"},
    })

    def get_session(_name, identifier):
        if identifier == sid:
            return old
        raise ResourceNotFoundError("the current reader does not exist")

    def no_invoke(request):
        pytest.fail("reading a receipt must not wake an old-version reader")

    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", sdk_read_session)
    monkeypatch.setattr("shared_context.behavior_native_sessions._latest_version_reader", sdk_latest_reader)
    project_client(monkeypatch, get_session=get_session, list_sessions=lambda *_a, **_k: iter((old,)))
    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(no_invoke))
    response = case.client.get(PREFIX + "/requests/original-rid", headers=case.headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert "not_accepted" not in response.json()["error"]


def test_current_prepared_destination_only_confirms_its_original_environment(native_api, monkeypatch):
    case = native_api
    original = case.remote.preparation
    monkeypatch.setattr("shared_context.behavior_native_sessions._require_native_version",
                        lambda _: pytest.fail("an existing current session must not be recreated"))
    response = case.client.post(PREFIX + "/environment", headers=case.headers, json={
        "request_id": "verify-current-original", "expected_agent_version": "41"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "current"
    assert response.json()["previous_hosted_session_id"] == original.hosted_session_id
    assert response.json()["hosted_session_id"] == original.hosted_session_id
    assert case.remote.preparation == original and not case.remote.records
    assert all(command.operation == "session_read" for _, command in case.remote.calls)


def test_preparation_reconcile_reads_the_original_receipt_without_repeating_prepare(native_api):
    case = native_api
    original = case.remote.preparation
    response = case.client.post(PREFIX + "/prepare/reconcile", headers=case.headers,
                                json={"request_id": original.request_id})
    assert response.status_code == 200, response.text
    assert response.json()["preparation"] == original.model_dump(mode="json")
    assert case.remote.preparation == original and not case.remote.records
    assert [command.operation for _, command in case.remote.calls] == ["session_read"]


def test_preparation_reconcile_does_not_forward_the_vms_version_to_a_strict_client(native_api):
    """A newer image names its own version in the session state; a workbench older than that field decodes this
    reply with ``extra="forbid"`` (agent_onboarding), so the API leaves it out and the reply stays as it was."""
    case = native_api
    case.remote.actual_version = case.remote.target.agent_version
    response = case.client.post(PREFIX + "/prepare/reconcile", headers=case.headers,
                                json={"request_id": case.remote.preparation.request_id})
    assert response.status_code == 200, response.text
    assert "actual_version" not in response.json()
    case.remote.actual_version = None
    unchanged = case.client.post(PREFIX + "/prepare/reconcile", headers=case.headers,
                                 json={"request_id": case.remote.preparation.request_id})
    assert unchanged.json() == response.json()


def test_pre_admission_version_refusal_is_distinct_from_a_receipt_read_failure(native_api, monkeypatch):
    from test_native_turn_api import submit

    case = native_api
    case.composition._native_sessions = NativeSessions(case.composition)
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", lambda _: None)
    monkeypatch.setattr("shared_context.behavior_native_sessions._latest_version_reader", lambda *_: None)
    refused = submit(case, rid="not-dispatched")
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert refused.json()["error"]["not_dispatched"] is True
    assert refused.json()["error"]["request_id"] == "not-dispatched"
    assert not case.remote.calls and not case.remote.records
    read = case.client.get(PREFIX + "/requests/not-dispatched", headers=case.headers)
    assert read.status_code == 409
    assert "not_dispatched" not in read.json()["error"]


def test_a_cached_old_binding_cannot_receive_a_new_request_after_publication_changes(native_api, monkeypatch):
    from test_native_turn_api import submit

    case = native_api
    old = case.remote.target

    def get_session(name, sid):
        if sid != old.session_id:
            raise ResourceNotFoundError("the newly published version has not been prepared")
        return AgentSessionResource({
            "agent_session_id": sid, "status": "idle",
            "version_indicator": {"type": "version_ref", "agent_version": "41"}})

    project_client(monkeypatch, get_session=get_session,
                   list_sessions=lambda name, **_: iter((get_session(name, old.session_id),)))
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", sdk_read_session)
    monkeypatch.setattr("shared_context.behavior_native_sessions._latest_version_reader", sdk_latest_reader)
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "42")
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView
    advances = []

    def advance(self, client, command, *, before_send):
        advances.append(command.expected_agent_version)
        return NativeEnvironmentView(
            agent_id=old.agent_id, request_id=command.request_id, state="pending", reason="source_active",
            previous_hosted_session_id=old.session_id, previous_agent_version="41",
            hosted_session_id="fa-native-destination", hosted_agent_version="42")

    monkeypatch.setattr(NativeSessions, "advance", advance)
    response = submit(case, rid="new-after-publication")
    error = response.json()["error"]
    # Only the 41 session exists: it is never invoked (invoking it lets the platform bring it back). 2d-i P7: the
    # submit follows to 42 instead of README (a)'s refusal, and the same RID is sent again (``server_following``).
    assert error["not_dispatched"] is True and error["request_id"] == "new-after-publication", response.text
    assert error["server_following"] is True
    assert advances == ["42"]
    assert case.remote.calls == []
    assert not case.remote.records


@pytest.mark.parametrize("blocker", ["admission", "receipt", "changed"])
def test_absence_observation_never_overrides_unresolved_or_existing_admission(native_api, monkeypatch, blocker):
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView
    from test_native_turn_api import submit

    case = native_api
    if blocker == "receipt":
        assert submit(case, rid="original-rid").status_code == 202
    case.remote.environment = NativeEnvironmentView(
        agent_id=case.remote.target.agent_id, request_id="original-follow",
        previous_hosted_session_id="original-old-sid", previous_agent_version="40",
        hosted_session_id=case.remote.target.session_id, hosted_agent_version="41", state="ready")
    original_transport = case.foundry.http._transport
    reads = 0

    def uncertain(request):
        nonlocal reads
        import json

        response = original_transport.handle_request(request)
        if json.loads(request.content)["operation"] == "session_read":
            reads += 1
            body = response.json()
            if blocker == "admission":
                body["admission_request_id"] = "original-rid"
            if blocker == "changed" and reads == 2:
                body["etag"] = "changed-by-another-request"
            return httpx.Response(200, json=body, headers=response.headers)
        return response

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(uncertain))
    response = case.client.get(PREFIX + "/requests/original-rid/admission-observation", headers=case.headers)
    assert response.status_code == {"admission": 404, "receipt": 409, "changed": 503}[blocker]
    assert "not_accepted" not in response.text
