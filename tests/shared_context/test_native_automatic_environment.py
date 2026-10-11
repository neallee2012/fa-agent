"""System-policy environment management through the real owner API and N1 records."""

import pytest

from test_native_files_api import (
    PREFIX, file_api, file_loops, follow, instruction_models, jwk_client, local_env, native_api,
    signing_key, synthetic_standing_v1, token, legacy_source,
)
from test_native_files_omission import omit
from test_native_source_drift_exit import stopped_source_drift


def advance(fixture):
    return fixture.case.client.post(PREFIX + "/environment/advance", json=fixture.command,
                                    headers=fixture.case.headers)


def observe(fixture):
    return fixture.case.client.post(PREFIX + "/environment/observe-automatic", json=fixture.command,
                                    headers=fixture.case.headers)


@pytest.mark.parametrize("file_api", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
def test_legacy_source_automatically_follows_without_files_once(file_api):
    fixture = file_api
    legacy_source(fixture)
    ready = advance(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    view = ready.json()
    assert view["files_migrated"] is False
    assert view["files_omission_basis"] == "system_policy"
    assert view["files_omission_reason"] == "source"
    assert view["files_skip_request_id"]
    assert not fixture.runtime.executor.calls
    assert not any(call[0] in {"put", "get", "list"} for call in fixture.files.calls)
    before = list(fixture.files.calls)
    assert advance(fixture).json() == view
    assert fixture.files.calls == before
    confirmations = [command for command, _ in fixture.controls
                     if command.get("operation") == "session_files_omission" and command.get("stage") == "confirm"]
    assert len(confirmations) == 1 and confirmations[0]["decision_basis"] == "system_policy"


def test_drifted_source_automatically_omits_acknowledged_staging(stopped_source_drift):
    fixture = stopped_source_drift
    ready = advance(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["files_omission_reason"] == "source_changed"
    assert ready.json()["files_omission_basis"] == "system_policy"
    assert not tuple(fixture.target_workspace.root.iterdir())
    assert not fixture.runtime.executor.calls


def test_readonly_progress_changes_only_after_original_task_finishes(file_api):
    from types import SimpleNamespace

    fixture = file_api
    legacy_source(fixture)
    busy = [True]

    async def quiet(*_args, **_kwargs):
        return not busy[0]

    fixture.runtime.task_observer = SimpleNamespace(quiet=quiet)
    pending = advance(fixture)
    assert pending.status_code == 200 and pending.json()["reason"] == "task_active", pending.text
    first = observe(fixture)
    second = observe(fixture)
    assert first.status_code == second.status_code == 200
    assert first.json()["progress_token"] == second.json()["progress_token"]
    assert not fixture.files.stopped
    busy[0] = False
    changed = observe(fixture)
    assert changed.status_code == 200
    assert changed.json()["progress_token"] != first.json()["progress_token"]
    assert not fixture.files.stopped
    ready = advance(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert not fixture.runtime.executor.calls


def test_missing_destination_waits_with_the_new_target_scope(file_api):
    from types import SimpleNamespace

    fixture = file_api
    fixture.runtime.workspace_files = None
    busy = [True]

    async def quiet(*_args, **_kwargs):
        return not busy[0]

    fixture.runtime.task_observer = SimpleNamespace(quiet=quiet)
    waiting = advance(fixture)
    assert waiting.status_code == 200 and waiting.json()["state"] == "pending", waiting.text
    assert waiting.json()["hosted_session_id"] == fixture.destination.session_id
    assert waiting.json()["reason"] == "task_active"
    first = observe(fixture)
    busy[0] = False
    finished = observe(fixture)
    assert first.status_code == finished.status_code == 200
    assert first.json()["progress_token"] != finished.json()["progress_token"]
    ready = advance(fixture)
    assert ready.status_code == 200 and ready.json()["files_migrated"] is False, ready.text


def test_automatic_policy_reuses_an_existing_manual_decision_after_lost_ack(file_api, monkeypatch):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from azure.core.exceptions import ServiceResponseError

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    create, writes = FoundryStateStore.create_item, []

    async def saved_no_ack(store, key, value, **kwargs):
        result = await create(store, key, value, **kwargs)
        if key.startswith("files-omission:"):
            writes.append(key)
            raise ServiceResponseError("OFFLINE original manual decision was saved")
        return result

    with monkeypatch.context() as lost:
        lost.setattr(FoundryStateStore, "create_item", saved_no_ack)
        assert omit(fixture).status_code == 503
    ready = advance(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["files_skip_request_id"] == "owner-confirms-no-files-once"
    assert "files_omission_basis" not in ready.json(), "Do not rewrite an old human decision as a new system decision"
    assert len(writes) == 1 and not fixture.runtime.executor.calls


def test_automatic_policy_stops_on_unknown_upload(file_api):
    fixture = file_api
    fixture.files.upload_error, fixture.files.target_read_error = True, 403
    assert advance(fixture).status_code == 403
    puts = [call for call in fixture.files.calls if call[0] == "put"]
    assert len(puts) == 1
    assert observe(fixture).status_code == 403
    assert [call for call in fixture.files.calls if call[0] == "put"] == puts
    assert not fixture.files.stopped and not fixture.runtime.executor.calls


@pytest.mark.parametrize("code", ["NATIVE_FILES_UNSAFE", "NATIVE_FILES_LIMIT"])
def test_known_source_migration_refusal_uses_system_policy_not_a_human_prompt(file_api, monkeypatch, code):
    from shared_context.errors import Problem
    from shared_context.native_workspace_files import NativeWorkspaceFiles

    fixture = file_api

    def refused(_self, **_scope):
        raise Problem(409, code, "OFFLINE source cannot be safely included")

    monkeypatch.setattr(NativeWorkspaceFiles, "snapshot", refused)
    ready = advance(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["files_migrated"] is False
    assert ready.json()["files_omission_reason"] == "source_not_migratable"
    assert ready.json()["files_omission_basis"] == "system_policy"
    assert not fixture.runtime.executor.calls and not any(call[0] == "put" for call in fixture.files.calls)


def test_automatic_policy_keeps_the_requested_source_precondition(file_api):
    fixture = file_api
    legacy_source(fixture)
    fixture.command["expected_session_id"] = "not-the-original-source"
    result = advance(fixture)
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_ENVIRONMENT_CONFLICT"
    assert not fixture.files.stopped and not fixture.runtime.executor.calls


@pytest.mark.parametrize("status", ["accepted", "started"])
def test_automatic_follow_waits_without_reclassifying_unfinished_work(file_api, status):
    from shared_context.behavior_native_turn_wire import canonical_bytes
    from shared_context.native_turns import TURNS_STORE, _changed, _json

    fixture = file_api
    legacy_source(fixture)

    async def unfinished():
        current = await fixture.runtime._rid("original-rid")
        receipt = _changed(current.value.receipt, status=status, result=None,
                           turn_number=None if status == "accepted" else 1)
        async with fixture.runtime._store(TURNS_STORE) as store:
            value = _json(_changed(current.value, receipt=receipt,
                                  run_token=None if status == "accepted" else "original-token"))
            await store.set_item("rid:original-rid", value, if_match=current.etag)
            return canonical_bytes(value)

    raw = fixture.hosted.portal.call(unfinished)
    pending = advance(fixture)
    assert pending.status_code == 200 and pending.json()["reason"] == "accepted_or_started", pending.text
    assert not fixture.files.stopped and not fixture.runtime.executor.calls
    assert not any(command.get("stage") == "confirm" for command, _ in fixture.controls)

    async def retained():
        async with fixture.runtime._store(TURNS_STORE) as store:
            return canonical_bytes((await store.get_item("rid:original-rid")).value)

    assert fixture.hosted.portal.call(retained) == raw


def test_system_policy_does_not_skip_a_bad_checkpoint(file_api):
    from shared_context.native_checkpoint import conversation_store_name

    fixture = file_api
    legacy_source(fixture)

    async def corrupt():
        async with fixture.runtime._store(conversation_store_name(fixture.saved.conversation_id)) as store:
            item = await store.get_item("turn:1")
            value = dict(item.value)
            value["checkpoints"][0]["sha256"] = "f" * 64
            await store.set_item("turn:1", value, if_match=item.etag)

    fixture.hosted.portal.call(corrupt)
    blocked = advance(fixture)
    assert blocked.status_code >= 400
    assert blocked.json()["error"]["files_check"]["category"] == "checkpoint_unconfirmed"
    assert not fixture.files.stopped and not fixture.runtime.executor.calls
    assert not any(command.get("stage") == "confirm" for command, _ in fixture.controls)


def test_system_policy_cannot_advance_an_unconfirmed_publication(file_api, monkeypatch):
    from shared_context.errors import Problem

    fixture = file_api

    def unconfirmed(_manifest):
        raise Problem(503, "NATIVE_FILES_UNKNOWN", "OFFLINE original publication unknown")

    monkeypatch.setattr(fixture.target_workspace, "publish", unconfirmed)
    blocked = advance(fixture)
    assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "NATIVE_FILES_UNKNOWN"
    assert observe(fixture).status_code == 503
    assert not fixture.runtime.executor.calls


def test_readonly_source_stop_completion_wakes_the_same_automatic_follow(file_api, monkeypatch):
    from test_native_session_prepare_api import project_client

    fixture = file_api
    legacy_source(fixture)
    stops = []

    def delayed_stop(name, sid):
        assert name == fixture.source.agent_name and sid == fixture.source.session_id
        stops.append(sid)

    files = fixture.files
    project_client(monkeypatch, get_session=files.get_session, stop_session=delayed_stop,
                   list_sessions=lambda name, **_: iter(files.get_session(name, sid) for sid in files.targets))
    pending = advance(fixture)
    assert pending.status_code == 200 and pending.json()["reason"] == "source_active", pending.text
    assert pending.json()["files_migrated"] is False
    before = observe(fixture)
    repeated = observe(fixture)
    assert before.status_code == repeated.status_code == 200
    assert before.json()["progress_token"] == repeated.json()["progress_token"]
    files.stopped.add(fixture.source.session_id)
    after = observe(fixture)
    assert after.status_code == 200 and after.json()["state"] == "pending", after.text
    assert after.json()["progress_token"] != before.json()["progress_token"]
    assert stops == [fixture.source.session_id], "Only metadata may be read while waiting for stop completion"
    ready = advance(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["request_id"] == pending.json()["request_id"]
    assert ready.json()["files_skip_request_id"] == pending.json()["files_skip_request_id"]
    assert not fixture.runtime.executor.calls
