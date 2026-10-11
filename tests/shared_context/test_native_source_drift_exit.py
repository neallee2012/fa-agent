"""Source reactivation after Session Files GET through the original signed follow API."""

import asyncio

import httpx
import pytest

from shared_context.behavior_native_sessions import NativeSessions
from shared_context.behavior_native_turn_wire import canonical_bytes
from test_native_files_api import (
    PREFIX, OfflineSessionFiles, file_api, file_loops, follow, instruction_models, jwk_client,
    legacy_source, local_env, native_api, signing_key, synthetic_standing_v1, token,
)
from test_native_files_omission import omit
from test_native_session_prepare_api import project_client


@pytest.fixture
def stopped_source_drift(file_api, monkeypatch):
    fixture = file_api
    download = OfflineSessionFiles.download_session_file

    def resume_on_get(transport, name, sid, *, path):
        if sid == transport.source_sid and sid in transport.stopped:
            transport.targets[sid] = fixture.source.model_copy(
                update={"agent_version": fixture.destination.agent_version})
            transport.stopped.remove(sid)
        return download(transport, name, sid, path=path)

    files = fixture.files
    project_client(
        monkeypatch, get_session=files.get_session, stop_session=files.stop_session,
        list_sessions=lambda name, **_: iter(files.get_session(name, sid) for sid in files.targets),
        list_session_files=files.list_session_files,
        download_session_file=lambda name, sid, *, path: resume_on_get(files, name, sid, path=path),
        upload_session_file=files.upload_session_file)
    failed = follow(fixture)
    assert failed.status_code == 409, failed.text
    assert failed.json()["error"]["code"] == "NATIVE_FILES_UNAVAILABLE"
    assert fixture.controls[-1][0]["stage"] == "source_stopped"
    assert fixture.files.get_session(fixture.source.agent_name, fixture.source.session_id).status == "active"
    assert fixture.files.targets[fixture.source.session_id].agent_version == fixture.destination.agent_version
    assert sum(call[0] == "put" for call in fixture.files.calls) == 2
    assert not fixture.target_workspace.root.exists()
    return fixture


def test_reconcile_only_observes_original_destination_after_source_drift(stopped_source_drift):
    fixture = stopped_source_drift
    files_before = list(fixture.files.calls)
    before = len(fixture.control_versions)
    fixture.case.composition._native_sessions = NativeSessions(fixture.case.composition)
    observed = follow(fixture, reconcile=True)
    assert observed.status_code == 200 and observed.json()["state"] == "pending", observed.text
    assert observed.json()["reason"] == "source_version_changed"
    assert fixture.files.calls == files_before
    assert all(sid == fixture.destination.session_id for sid, _, _ in fixture.control_versions[before:])
    assert not fixture.runtime.executor.calls


def test_known_source_drift_does_not_stop_and_read_source_again(stopped_source_drift):
    fixture = stopped_source_drift
    files_before = list(fixture.files.calls)
    observed = follow(fixture, reconcile=True)
    assert observed.status_code == 200
    repeated = follow(fixture)
    assert repeated.status_code == 409, repeated.text
    assert repeated.json()["error"]["code"] == "NATIVE_FILES_SOURCE_UNAVAILABLE"
    assert fixture.files.calls == files_before, "A metadata-proven drift must not issue another source stop or Files GET"
    assert not fixture.runtime.executor.calls


@pytest.mark.parametrize("file_api", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
def test_owner_can_omit_confirmed_staging_after_source_drift(stopped_source_drift):
    from shared_context.native_environment import _key
    from shared_context.native_turns import TURNS_STORE

    fixture = stopped_source_drift
    key = _key(fixture.runtime)

    async def read_records():
        async with fixture.runtime._store(TURNS_STORE) as store:
            page = await store.list_keys(limit=100)
            return {entry.key: (await store.get_item(entry.key)).value for entry in page.keys}

    before = fixture.hosted.portal.call(read_records)
    files_bytes = canonical_bytes(before[key]["files"])
    files_before = list(fixture.files.calls)
    preview = omit(fixture, preview=True)
    assert preview.status_code == 200, preview.text
    assert preview.json()["state"] == "pending"
    assert not fixture.target_workspace.root.exists()
    ready = omit(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["files_migrated"] is False and "files_manifest_sha256" not in ready.json()
    assert fixture.target_workspace.root.is_dir() and not tuple(fixture.target_workspace.root.iterdir())
    after = fixture.hosted.portal.call(read_records)
    omitted = next(value for name, value in after.items() if name.startswith("files-omission:"))
    assert omitted["missing"] == "source_changed"
    assert canonical_bytes(omitted["original"]["files"]) == canonical_bytes(after[key]["files"]) == files_bytes
    assert after["rid:original-rid"] == before["rid:original-rid"]
    assert all(call[0] == "stop" for call in fixture.files.calls[len(files_before):])
    for call in files_before:
        if call[0] == "put":
            assert (fixture.target_workspace.home / call[2]).read_bytes() == call[3]
    assert omit(fixture, reconcile=True).json() == ready.json()
    assert not fixture.runtime.executor.calls


@pytest.mark.parametrize("old_target_drift", [False, True])
@pytest.mark.parametrize("slow_retarget", [False, True], ids=["direct", "paged"])
@pytest.mark.parametrize("system_policy", [False, True], ids=["human", "automatic"])
@pytest.mark.parametrize("file_api", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
def test_new_target_can_omit_retained_staging_but_does_not_reuse_the_old_target(
        stopped_source_drift, monkeypatch, old_target_drift, slow_retarget, system_policy):
    from shared_context.behavior_native_sessions import native_session_id
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
    from shared_context.errors import Problem
    from shared_context.native_environment import _key
    from shared_context.native_turns import NativeTurnRuntime, TURNS_STORE
    from shared_context.native_workspace_files import NativeWorkspaceFiles
    from test_native_turns import OfflineSdk

    fixture = stopped_source_drift
    old_key = _key(fixture.runtime)

    async def original_files():
        async with fixture.runtime._store(TURNS_STORE) as store:
            return (await store.get_item(old_key)).value["files"]

    original = canonical_bytes(fixture.hosted.portal.call(original_files))
    if old_target_drift:
        fixture.files.targets[fixture.destination.session_id] = fixture.destination.model_copy(
            update={"agent_version": "43"})
    target = fixture.destination.model_copy(update={
        "agent_version": "43", "session_id": native_session_id(
            fixture.source, fixture.case.composition.config.identity.managed_identity_client_id, version="43")})
    fixture.files.targets[target.session_id] = target
    identity = fixture.identity.model_copy(update={"session_id": target.session_id, "agent_version": "43"})
    workspace = NativeWorkspaceFiles(fixture.target_workspace.home.parent / "new-reader")
    runtime = NativeTurnRuntime(identity, OfflineSdk(), workspace_files=workspace,
                               task_observer=fixture.runtime.task_observer,
                               bootstrap_source_inactive=fixture.runtime.bootstrap_source_inactive)
    original_transport = fixture.case.foundry.http._transport
    missing, confirmations = [], []
    files_before = list(fixture.files.calls)

    def forward(packet):
        if packet.url.params["agent_session_id"] != identity.session_id:
            return original_transport.handle_request(packet)
        command = NATIVE_REQUEST.validate_json(packet.content)
        if command.operation == "session_files_omission" and command.stage in {"check", "confirm"}:
            missing.append(command.missing)
            if command.stage == "confirm":
                confirmations.append((command.request_id, command.follow.request_id))
        try:
            result = fixture.hosted.portal.call(runtime.dispatch, command)
        except Problem as error:
            return httpx.Response(error.status, json=error.body)
        return httpx.Response(200, content=result.model_dump_json().encode(), headers={
            "content-type": "application/json", "x-agent-session-id": identity.session_id,
            "x-agent-invocation-id": "new-source-drift-reader"})

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(forward))
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "43")
    delayed = []
    if slow_retarget:
        from azure.ai.agentserver.core.storage import FoundryStateStore
        from shared_context import native_environment

        listing = FoundryStateStore.list_keys

        async def one_slow_retarget_page(store, **options):
            if options.get("limit") == 16 and not delayed:
                delayed.append(options.get("after"))
                await asyncio.sleep(0.8)
            return await listing(store, **options)

        monkeypatch.setattr(native_environment, "_SCAN_SECONDS", 0.4)
        monkeypatch.setattr(FoundryStateStore, "list_keys", one_slow_retarget_page)
    body = {"request_id": "new-confirmation-new-reader", "follow": fixture.command,
            "expected_agent_version": "43", "confirmation": "do-not-transfer-workspace-files"}
    if system_policy:
        command = {"request_id": "original-system-follow", "expected_agent_version": "43", "idle_only": True}

        def resume():
            return fixture.case.client.post(PREFIX + "/environment/advance", json=command,
                                            headers=fixture.case.headers)
    else:
        checked = omit(fixture, preview=True, body=body)
        assert checked.status_code == 200, checked.text

        def resume():
            return omit(fixture, body=body)

    ready = resume()
    if slow_retarget:
        assert delayed
        assert ready.status_code == 200 and ready.json()["state"] == "pending", ready.text
        assert ready.json()["reason"] == "scan_pending"
        assert ready.json()["files_confirmation_pending"] == confirmations[0][0]
        assert ready.json().get("files_migrated") is None
        assert not workspace.root.exists()
        for _ in range(10):
            if ready.status_code != 200 or ready.json()["state"] == "ready":
                break
            ready = resume()
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["hosted_session_id"] == target.session_id
    assert ready.json()["files_migrated"] is False
    assert ready.json()["request_id"] != fixture.command["request_id"]
    assert canonical_bytes(fixture.hosted.portal.call(original_files)) == original
    assert not tuple(workspace.root.iterdir())
    assert not fixture.runtime.executor.calls and not runtime.executor.calls
    assert missing and set(missing) == {"source_changed"}
    assert confirmations and len(set(confirmations)) == 1
    assert all(call[0] == "stop" for call in fixture.files.calls[len(files_before):])
    if system_policy:
        assert ready.json()["files_omission_basis"] == "system_policy"


def test_pending_omission_receipt_cannot_name_a_different_confirmation(file_api, monkeypatch):
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    transport = fixture.case.foundry.http._transport

    def wrong_confirmation(packet):
        result = transport.handle_request(packet)
        command = NATIVE_REQUEST.validate_json(packet.content)
        if command.operation != "session_files_omission" or command.stage != "confirm":
            return result
        assert result.status_code == 200
        value = result.json()
        for name in ("files_migrated", "files_skip_request_id", "files_omission_basis", "files_omission_reason"):
            value.pop(name, None)
        value.update(state="pending", reason="scan_pending", files_confirmation_pending="another-confirmation",
                     files_check={"category": "scan_pending"})
        return httpx.Response(200, json=value, headers={
            "x-agent-session-id": fixture.destination.session_id,
            "x-agent-invocation-id": "offline-wrong-confirmation"})

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(wrong_confirmation))
    refused = omit(fixture)
    assert refused.status_code == 502 and refused.json()["error"]["code"] == "NATIVE_STATE_SCOPE", refused.text
    assert not fixture.files.stopped and not fixture.runtime.executor.calls


@pytest.mark.parametrize("remote", ["absent", "saved"])
def test_staged_source_drift_reconciles_only_the_original_confirmation(stopped_source_drift, remote):
    fixture = stopped_source_drift
    before = len(fixture.files.calls)
    if remote == "saved":
        fixture.lost.update(operation="session_files_omission", stage="confirm")
        assert omit(fixture).status_code == 503
    ready = omit(fixture, reconcile=True)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["files_skip_request_id"] == "owner-confirms-no-files-once"
    assert sum(command.get("stage") == "confirm" for command, _ in fixture.controls
               if command.get("operation") == "session_files_omission") == 1
    assert all(call[0] == "stop" for call in fixture.files.calls[before:])
    assert not tuple(fixture.target_workspace.root.iterdir())
    assert not fixture.runtime.executor.calls


def test_source_drift_does_not_hide_an_unknown_upload(file_api):
    fixture = file_api
    fixture.files.upload_error = True
    fixture.files.target_read_error = 403
    assert follow(fixture).status_code == 403
    fixture.files.targets[fixture.source.session_id] = fixture.source.model_copy(update={"agent_version": "42"})
    puts = [call for call in fixture.files.calls if call[0] == "put"]
    before = list(fixture.files.calls)
    blocked = omit(fixture, preview=True)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "NATIVE_FILES_CONFLICT"
    assert fixture.files.calls == before
    assert omit(fixture, reconcile=True).status_code == 409
    assert fixture.files.calls == before
    assert follow(fixture, reconcile=True).status_code == 403
    assert [call for call in fixture.files.calls if call[0] == "put"] == puts
    assert not fixture.files.stopped and not fixture.target_workspace.root.exists()


def test_source_drift_does_not_hide_unknown_publication(file_api, monkeypatch):
    from shared_context.errors import Problem
    from shared_context.native_workspace_files import NativeWorkspaceFiles

    fixture = file_api
    attempted = []
    def unknown_publish(_self, _manifest):
        attempted.append(_manifest.sha256)
        raise Problem(503, "NATIVE_FILES_UNKNOWN", "OFFLINE publication outcome was not confirmed")

    monkeypatch.setattr(NativeWorkspaceFiles, "publish", unknown_publish)
    failed = follow(fixture)
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "NATIVE_FILES_UNKNOWN", failed.text
    assert len(attempted) == 1, "A setup failure before publication is not an unknown publication"
    fixture.files.targets[fixture.source.session_id] = fixture.source.model_copy(update={"agent_version": "42"})
    before = list(fixture.files.calls)
    blocked = omit(fixture, preview=True)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "NATIVE_FILES_CONFLICT", blocked.text
    assert fixture.files.calls == before
    assert not fixture.runtime.executor.calls


def test_confirmed_staging_does_not_override_active_execution(stopped_source_drift):
    fixture = stopped_source_drift
    fixture.hosted.portal.call(fixture.runtime.execution_lock.acquire)
    try:
        blocked = omit(fixture, preview=True)
        assert blocked.status_code == 409
        assert blocked.json()["error"]["files_check"]["category"] == "execution_active"
        assert not fixture.target_workspace.root.exists()
    finally:
        fixture.hosted.portal.call(fixture.runtime.execution_lock.release)


def test_saved_reason_cannot_appear_without_an_original_confirmation_reference():
    from pydantic import ValidationError
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView

    with pytest.raises(ValidationError):
        NativeEnvironmentView(
            agent_id="agent", request_id="follow", state="pending",
            previous_hosted_session_id="source", previous_agent_version="41",
            hosted_session_id="destination", hosted_agent_version="42",
            files_confirmation_reason="source_changed")
