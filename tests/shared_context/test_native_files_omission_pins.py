"""Literal outcomes of confirm_omission branches, reached through the Hosted invocation route."""

import pytest

from shared_context.behavior_native_turn_wire import material_digest
from shared_context.native_environment import _key, _read
from shared_context.native_files_omission import scan_observation
from shared_context.native_turns import TURNS_STORE, _json
from test_native_environment import invoke
from test_native_files_api import (  # noqa: F401 - pytest fixtures
    file_api, file_loops, follow, instruction_models, jwk_client, local_env, native_api, signing_key,
    synthetic_standing_v1, token, legacy_source,
)
from test_native_files_omission import omit

RID = "owner-confirms-no-files-once"


def _check_command(fixture):
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    assert omit(fixture, preview=True).status_code == 200
    return next(command for command, _ in reversed(fixture.controls)
                if command.get("operation") == "session_files_omission" and command.get("stage") == "check")


def _send(fixture, command, **changes):
    return invoke(fixture.hosted, fixture.identity, {**command, **changes})


def _error(response):
    error = response.json()["error"]
    return response.status_code, error["code"], error["message"]


def _edit_following(fixture, view=(), **fields):
    async def edit():
        async with fixture.runtime._store(TURNS_STORE) as store:
            item = await store.get_item(_key(fixture.runtime))
            value = {**item.value, **fields}
            value["view"] = {**value["view"], **dict(view)}
            await store.set_item(item.key, value, if_match=item.etag)
    fixture.hosted.portal.call(edit)


def _edit_agent(fixture, **fields):
    async def edit():
        current, etag = await fixture.runtime._agent_record()
        async with fixture.runtime._store(TURNS_STORE) as store:
            await store.set_item("agent", {**_json(current), **fields}, if_match=etag)
    fixture.hosted.portal.call(edit)


def _admit(fixture):
    _edit_agent(fixture, admitting={"request_id": "later-admission", "submit_digest": "e" * 64})


def _saved_without_activation(fixture, monkeypatch, command, **changes):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from azure.core.exceptions import ServiceResponseError
    create = FoundryStateStore.create_item

    async def lose_ack(store, key, value, **kwargs):
        result = await create(store, key, value, **kwargs)
        if key.startswith("files-omission:"):
            raise ServiceResponseError("OFFLINE immutable confirmation committed; ACK lost before activation")
        return result

    with monkeypatch.context() as lost:
        lost.setattr(FoundryStateStore, "create_item", lose_ack)
        assert _send(fixture, command, stage="confirm", **changes).status_code == 503


def test_activate_needs_a_saved_confirmation(file_api):
    command = _check_command(file_api)
    assert _error(_send(file_api, command, stage="activate")) == (
        503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original file omission has not been confirmed")


def test_available_destination_helper_cannot_be_skipped(file_api):
    command = _check_command(file_api)
    assert _error(_send(file_api, command, missing="destination")) == (
        409, "NATIVE_FILES_CONFLICT", "the destination helper is available; do not skip its files")


def test_other_idle_mode_is_not_the_original_follow(file_api):
    command = _check_command(file_api)
    response = _send(file_api, command, follow={**command["follow"], "idle_only": False})
    assert _error(response) == (
        409, "NATIVE_FILES_CONFIRMATION_REQUIRED", "confirm the exact original source and destination")


@pytest.mark.parametrize("admitted", [False, True])
def test_unsaved_read_reports_absence_with_the_observed_progress_token(file_api, admitted):
    fixture = file_api
    command = _check_command(fixture)
    if admitted:
        _admit(fixture)
    response = _send(fixture, command, stage="read", missing=None)
    assert response.status_code == 200, response.text
    value = response.json()
    record, _ = fixture.hosted.portal.call(_read, fixture.runtime)
    scan = fixture.hosted.portal.call(scan_observation, fixture.runtime, record.view, RID)
    assert scan is not None and scan["complete"] is True
    admitting = {"request_id": "later-admission", "submit_digest": "e" * 64} if admitted else None
    assert value["files_confirmation_absent"] == RID and value["operation_idle_only"] is True
    assert value["progress_token"] == material_digest({
        "record": record.model_dump(mode="json"), "scan": scan, "admitting": admitting, "executing": False,
        "tasks_complete": True})
    assert value["state"] == "pending" and value.get("files_migrated") is None


@pytest.mark.parametrize("stage", ["check", "confirm"])
def test_unsaved_policy_confirmation_waits_for_admission(file_api, stage):
    fixture = file_api
    command = _check_command(fixture)
    _admit(fixture)
    waiting = _send(fixture, command, stage=stage, decision_basis="system_policy")
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["reason"] == "admission_pending" and waiting.json().get("files_check") is None
    assert waiting.json().get("files_migrated") is None and waiting.json()["state"] == "pending"
    owner = _send(fixture, command, stage=stage)
    assert _error(owner) == (
        409, "NATIVE_FILES_CONFIRMATION_REQUIRED", "resolve the original work before confirming no files")
    assert owner.json()["error"]["files_check"] == {"category": "admission_pending", "request_id_prefix": "later-ad"}


@pytest.mark.parametrize("changes", [{"request_id": "another-confirmation"}, {"missing": "destination"}],
                         ids=["other-request", "other-reason"])
def test_ready_omission_rejects_another_confirmation(file_api, changes):
    fixture = file_api
    command = _check_command(fixture)
    assert omit(fixture).status_code == 200
    assert _error(_send(fixture, command, stage="confirm", **changes)) == (
        409, "NATIVE_FILES_CONFLICT", "the original file omission has another confirmation request")


def test_ready_omission_check_and_read_return_the_saved_decision(file_api):
    fixture = file_api
    command = _check_command(fixture)
    ready = omit(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    checked = _send(fixture, command)
    assert checked.status_code == 200, checked.text
    record, _ = fixture.hosted.portal.call(_read, fixture.runtime)
    assert checked.json() == record.view.model_dump(mode="json")
    assert checked.json()["files_migrated"] is False and checked.json()["files_skip_request_id"] == RID
    assert "files_manifest_sha256" not in checked.json()
    read = _send(fixture, command, stage="read", missing=None)
    assert read.status_code == 200 and read.json() == ready.json(), read.text


def test_active_omission_naming_another_confirmation_is_a_conflict(file_api):
    fixture = file_api
    command = _check_command(fixture)
    assert omit(fixture).status_code == 200
    _edit_following(fixture, view={"files_skip_request_id": "someone-else-confirmed"})
    assert _error(_send(fixture, command, stage="read", missing=None)) == (
        409, "NATIVE_FILES_CONFLICT", "the active file omission names another confirmation")


def test_saved_confirmation_rejects_a_changed_following(file_api, monkeypatch):
    fixture = file_api
    command = _check_command(fixture)
    _saved_without_activation(fixture, monkeypatch, command)
    _edit_following(fixture, view={"reason": "source_stop_required"})
    assert _error(_send(fixture, command, stage="activate")) == (
        409, "NATIVE_FILES_CONFLICT", "the original following changed after its saved confirmation")
    saved = _send(fixture, command, stage="read", missing=None)
    assert saved.status_code == 200, saved.text
    assert saved.json()["files_confirmation_saved"] == RID and saved.json()["files_confirmation_reason"] == "source"


def test_saved_policy_confirmation_waits_for_admission_before_activation(file_api, monkeypatch):
    fixture = file_api
    command = _check_command(fixture)
    _saved_without_activation(fixture, monkeypatch, command, decision_basis="system_policy")
    _admit(fixture)
    waiting = _send(fixture, command, stage="activate", decision_basis="system_policy")
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["reason"] == "admission_pending" and waiting.json().get("files_check") is None
    assert waiting.json().get("files_migrated") is None and waiting.json()["state"] == "pending"


def test_saved_confirmation_activates_only_for_its_reason(file_api, monkeypatch):
    fixture = file_api
    command = _check_command(fixture)
    _saved_without_activation(fixture, monkeypatch, command)
    assert _error(_send(fixture, command, stage="activate", missing="destination")) == (
        409, "NATIVE_FILES_CONFLICT", "the original file omission has another confirmation request")


def test_unsaved_confirmation_needs_the_original_source(file_api):
    fixture = file_api
    command = _check_command(fixture)
    record, _ = fixture.hosted.portal.call(_read, fixture.runtime)
    _edit_agent(fixture, environment={**record.view.model_dump(mode="json"), "request_id": "another-follow"})
    assert _error(_send(fixture, command)) == (
        409, "NATIVE_ENVIRONMENT_CONFLICT", "another original environment transition won the comparison")


def test_unsaved_confirmation_cannot_discard_another_transition(file_api):
    fixture = file_api
    command = _check_command(fixture)
    _edit_following(fixture, retarget_request_id="another-follow")
    assert _error(_send(fixture, command)) == (
        409, "NATIVE_FILES_CONFLICT", "file material or another transition cannot be discarded")


def _first_command(fixture, stage):
    legacy_source(fixture)
    return {"operation": "session_files_omission", "request_id": RID, "stage": stage,
            "missing": None if stage == "read" else "source",
            "follow": {"operation": "session_follow", "request_id": "follow-files-original",
                       "expected_session_id": fixture.source.session_id, "expected_etag": '"unused"',
                       "stage": "prepare", "idle_only": True, "files_required": True}}


def test_read_before_any_follow_describes_the_original_follow(file_api):
    fixture = file_api
    response = _send(fixture, _first_command(fixture, "read"))
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["files_confirmation_absent"] == RID and value["request_id"] == "follow-files-original"
    assert value["state"] == "pending" and value["reason"] == "source_stop_required"
    assert value["previous_hosted_session_id"] == fixture.source.session_id
    assert value["hosted_session_id"] == fixture.identity.session_id
    assert fixture.hosted.portal.call(_read, fixture.runtime) == (None, None)


def test_confirmation_restarts_the_follow_scan_cursor(file_api):
    fixture = file_api
    command = _check_command(fixture)
    _edit_following(fixture, after="z-history:999")
    confirmed = _send(fixture, command, stage="confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["files_migrated"] is False and confirmed.json()["files_skip_request_id"] == RID
    record, _ = fixture.hosted.portal.call(_read, fixture.runtime)
    assert record.after is None and record.page_offset == 0 and record.page_digest is None
    assert record.view.model_dump(mode="json") == confirmed.json()


def test_first_confirmation_refuses_files_written_by_the_follow_it_starts(file_api, monkeypatch):
    from shared_context import native_environment
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
    from shared_context.native_files_omission import _initial_record
    from shared_context.native_files_wire import NativeFilesProgress
    from shared_context.native_turns import _changed

    fixture = file_api
    command = _first_command(fixture, "confirm")
    manifest = fixture.workspace.snapshot(
        agent_id=fixture.identity.agent_id, request_id="follow-files-original",
        source_session_id=fixture.source.session_id, source_agent_version="41",
        target_session_id=fixture.destination.session_id, target_agent_version="42")
    omission = NATIVE_REQUEST.validate_python(command)

    async def files_won_the_follow(runtime, _follow):
        current, _ = await runtime._agent_record()
        record = _initial_record(runtime, current, omission)
        value = _json(_changed(record, files=NativeFilesProgress(manifest=manifest),
                               view=_changed(record.view, files_manifest_sha256=manifest.sha256)))
        async with runtime._store(TURNS_STORE) as store:
            await store.create_item(_key(runtime), value)

    monkeypatch.setattr(native_environment, "follow_session", files_won_the_follow)
    assert _error(_send(fixture, command)) == (
        409, "NATIVE_FILES_CONFLICT", "the original file operation changed during confirmation")


@pytest.mark.parametrize("reason", ["task_access_denied", "scan_pending"])
def test_retarget_that_leaves_another_follow_selected_reports_it(file_api, monkeypatch, reason):
    from shared_context import native_environment
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView

    fixture = file_api
    command = _check_command(fixture)
    record, _ = fixture.hosted.portal.call(_read, fixture.runtime)
    older = {**record.view.model_dump(mode="json"), "request_id": "older-follow",
             "hosted_session_id": "fa-native-older-destination", "hosted_agent_version": "41"}

    async def block_older():
        async with fixture.runtime._store(TURNS_STORE) as store:
            await store.create_item(native_environment._key(fixture.runtime, session_id=older["hosted_session_id"]),
                                    {**_json(record), "view": older})
    fixture.hosted.portal.call(block_older)
    _edit_agent(fixture, environment=older)
    transitions = []

    async def unselected(runtime, retarget):
        transitions.append(retarget)
        return NativeEnvironmentView.model_validate({**older, "reason": reason})

    monkeypatch.setattr(native_environment, "retarget_session", unselected)
    response = _send(fixture, command, stage="confirm", pending_request_id="older-follow",
                     pending_session_id=older["hosted_session_id"])
    assert [(item.request_id, item.pending_request_id, item.idle_only) for item in transitions] == [
        ("follow-files-original", "older-follow", True)]
    if reason == "task_access_denied":
        assert _error(response) == (
            409, "NATIVE_FILES_CONFIRMATION_REQUIRED", "resolve the original work before confirming no files")
        assert response.json()["error"]["files_check"] == {"category": "task_access_denied"}
        return
    assert response.status_code == 200, response.text
    assert response.json()["request_id"] == "older-follow" and response.json()["files_confirmation_pending"] == RID
    assert response.json()["reason"] == "scan_pending" and response.json()["files_check"] == {
        "category": "scan_pending", "request_id_prefix": None, "conversation_id_prefix": None}
