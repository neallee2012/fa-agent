"""ADR-0009 §10: a follow never wakes a reclaimed (idle) source session to inspect, copy, verify or stop it."""

import pytest

from shared_context.behavior_native_turn_wire import NativeRead
from test_native_automatic_environment import advance
from test_native_environment import invoke
from test_native_files_api import (
    PREFIX, file_api, file_loops, follow, instruction_models, jwk_client, local_env, native_api,
    signing_key, synthetic_standing_v1, token,
)

FILE_CALLS = {"get", "list", "page", "put"}


def source_calls(fixture, *, since=(0, 0)):
    """Invocations, Session Files calls and stops that reached the original source SID."""
    sid = fixture.source.session_id
    controls, files = since
    invoked = [operation for called, _, operation in fixture.control_versions[controls:] if called == sid]
    read = [call[0] for call in fixture.files.calls[files:] if call[0] in FILE_CALLS and call[1] == sid]
    stopped = [call for call in fixture.files.calls[files:] if call[0] == "stop"]
    return invoked, read, stopped


@pytest.mark.parametrize("file_api", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
def test_idle_source_follows_without_files_and_is_never_called(file_api):
    fixture = file_api
    fixture.files.stopped.add(fixture.source.session_id)  # the platform reclaimed the source compute
    refused = follow(fixture)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "NATIVE_FILES_SOURCE_UNAVAILABLE"
    assert source_calls(fixture) == ([], [], [])

    ready = advance(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    view = ready.json()
    # The API records the omission; the workbench does not show a notice for it today (ADR-0009 §8, phase 2).
    assert view["files_migrated"] is False and view["files_skip_request_id"]
    assert view["files_omission_basis"] == "system_policy" and view["files_omission_reason"] == "source"
    assert view["previous_hosted_session_id"] == fixture.source.session_id
    assert source_calls(fixture) == ([], [], [])
    assert not (fixture.target_workspace.root / "nested" / "draft.txt").exists()
    original = invoke(fixture.hosted, fixture.identity, NativeRead(request_id="original-rid"))
    assert original.json() == fixture.saved.model_dump(mode="json")


@pytest.mark.parametrize("file_api", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
def test_active_source_still_copies_files_then_stops_once(file_api):
    fixture = file_api
    ready = follow(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["files_manifest_sha256"]
    assert (fixture.target_workspace.root / "nested" / "draft.txt").read_bytes() == b"uncommitted original\n"
    invoked, read, stopped = source_calls(fixture)
    assert {"session_files"} == set(invoked) and "get" in read and len(stopped) == 1


@pytest.mark.parametrize("file_api", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
def test_source_reclaimed_between_copy_and_stop_is_not_verified_stopped_or_read(file_api, monkeypatch):
    from shared_context.behavior_session_files import FollowFiles

    fixture = file_api
    copy_step, mark = FollowFiles.copy_step, []

    def copy_then_reclaim(self):
        copy_step(self)
        fixture.files.stopped.add(fixture.source.session_id)
        mark.append((len(fixture.control_versions), len(fixture.files.calls)))

    monkeypatch.setattr(FollowFiles, "copy_step", copy_then_reclaim)
    refused = follow(fixture)
    assert mark, "the original copy must run before the source is reclaimed"
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "NATIVE_FILES_SOURCE_UNAVAILABLE"
    # A compute-reclaimed source needs no stop; FA does not wake it to verify or re-read its files.
    assert source_calls(fixture, since=mark[0]) == ([], [], [])
    assert not any(call[0] == "stop" for call in fixture.files.calls)
    assert not (fixture.target_workspace.root / "nested" / "draft.txt").exists()

    monkeypatch.setattr(FollowFiles, "copy_step", copy_step)
    ready = advance(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["files_migrated"] is False
    assert ready.json()["files_omission_reason"] == "source_not_migratable"
    assert source_calls(fixture, since=mark[0]) == ([], [], [])
    assert not (fixture.target_workspace.root / "nested" / "draft.txt").exists()


@pytest.mark.parametrize("status", ["creating", "updating", "deleting"])
def test_follow_stops_only_an_active_source(native_api, monkeypatch, status):
    from azure.ai.projects.models import AgentSessionResource
    from test_native_environment_api import environment_at
    from test_native_session_prepare_api import project_client

    case = native_api
    source, calls, sessions, view = environment_at(case, monkeypatch)

    def get_session(name, sid):
        calls.append(("status", sid))
        return AgentSessionResource({
            "agent_session_id": sid, "status": status if sid == source.session_id else "active",
            "version_indicator": {"type": "version_ref", "agent_version": sessions[sid].agent_version}})

    project_client(monkeypatch, get_session=get_session,
                   list_sessions=lambda name, **_: iter(get_session(name, sid) for sid in sessions))
    response = case.client.post(PREFIX + "/environment", headers=case.headers, json={
        "request_id": "follow-original", "expected_session_id": source.session_id, "expected_agent_version": "42",
    })
    assert response.status_code == 200 and response.json()["state"] == "pending", response.text
    assert response.json()["reason"] == "source_active" and view[0].state == "pending"
    assert [action for action, sid in calls if sid == source.session_id and action != "status"] == []


@pytest.mark.parametrize("file_api", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
def test_source_reclaimed_after_a_failed_stop_is_never_read(file_api, monkeypatch):
    """FA's own stop fails, then the platform reclaims the source: inactive is not FA's stop, so no Files read."""
    from azure.core.exceptions import HttpResponseError
    from test_native_session_prepare_api import project_client

    fixture, files = file_api, file_api.files
    refused = []

    def stop_unavailable(name, sid):
        refused.append(sid)
        error = HttpResponseError("OFFLINE stop unavailable")
        error.status_code = 503
        raise error

    project_client(
        monkeypatch, get_session=files.get_session, stop_session=stop_unavailable,
        list_sessions=lambda name, **_: iter(files.get_session(name, sid) for sid in files.targets),
        list_session_files=files.list_session_files,
        download_session_file=files.download_session_file, upload_session_file=files.upload_session_file)
    pending = follow(fixture)
    assert pending.status_code == 200 and pending.json()["state"] == "pending", pending.text
    assert pending.json()["reason"] == "source_unavailable" and refused == [fixture.source.session_id]
    files.stopped.add(fixture.source.session_id)  # reclaimed by the platform; FA's stop never took effect
    mark = (len(fixture.control_versions), len(files.calls))

    ready = advance(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["files_migrated"] is False
    assert ready.json()["files_omission_reason"] == "source_not_migratable"
    assert source_calls(fixture, since=mark) == ([], [], [])
    assert not (fixture.target_workspace.root / "nested" / "draft.txt").exists()


@pytest.mark.parametrize("status", ["active", "idle", "creating"])
def test_automatic_retarget_keeps_files_only_from_an_active_source(monkeypatch, status):
    """`advance` retargets an older pending follow (which still copies files) only while its source is active."""
    from types import SimpleNamespace
    from shared_context import behavior_native_sessions
    from shared_context.behavior_native_sessions import NativeSessions
    from shared_context.behavior_native_turn_wire import NativeEnvironmentCommand
    from shared_context.errors import Problem
    from test_behavior_foundry import TARGET

    pending = SimpleNamespace(
        state="pending", request_id="older-follow", hosted_session_id="older-destination", hosted_agent_version="8",
        previous_hosted_session_id="original-source", previous_agent_version="7", source_invocation_refused=False)
    destination = SimpleNamespace(target=TARGET.model_copy(update={"session_id": "newer-destination", "agent_version": "9"}))
    state = SimpleNamespace(following=None, environment=pending)
    sessions, calls, metadata = NativeSessions(SimpleNamespace()), [], []

    def refused(*_args, **_kwargs):
        raise Problem(409, "NATIVE_FILES_SOURCE_UNAVAILABLE", "OFFLINE the newer follow cannot copy files")

    def read_metadata(client):
        metadata.append(client.target.session_id)
        return SimpleNamespace(status=status, version_indicator=SimpleNamespace(agent_version="7"))

    def retarget(client, command, **_options):
        calls.append(("retarget", command.pending_request_id))
        return "retargeted"

    def without_files(client, decision, **options):
        calls.append(("without_files", decision, options["refusal_code"]))
        return "without-files"

    monkeypatch.setattr(behavior_native_sessions, "_session_metadata", read_metadata)
    monkeypatch.setattr(sessions, "follow", refused)
    monkeypatch.setattr(sessions, "_destination", lambda *_args: destination)
    monkeypatch.setattr(sessions, "_state", lambda _destination: state)
    monkeypatch.setattr(sessions, "retarget", retarget)
    monkeypatch.setattr(sessions, "_automatic_decision", lambda *_args: "automatic-decision")
    monkeypatch.setattr(sessions, "without_files", without_files)
    client = SimpleNamespace(target=TARGET, for_target=lambda target: SimpleNamespace(target=target))
    command = NativeEnvironmentCommand(request_id="newer-follow", expected_agent_version="9", idle_only=True)

    result = sessions.advance(client, command, before_send=lambda: None)
    assert metadata == ["original-source"]
    if status == "active":
        assert result == "retargeted" and calls == [("retarget", "older-follow")]
    else:
        # A reclaimed or changing source is not called again: no retarget, the follow ends without files.
        assert result == "without-files"
        assert calls == [("without_files", "automatic-decision", "NATIVE_FILES_SOURCE_UNAVAILABLE")]
