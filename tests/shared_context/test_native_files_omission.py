"""Explicit owner confirmation for a helper-less endpoint; no automatic file omission."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_native_turn_wire import NativeEnvironmentWithoutFiles, NativeRead, NativeSessionRead, canonical_bytes, material_digest
from shared_context.native_checkpoint import conversation_store_name
from shared_context.native_turns import TURNS_STORE, _json, _changed
from test_native_environment import invoke
from test_native_files_api import (
    PREFIX, file_api, file_loops, follow, instruction_models, jwk_client, local_env,
    native_api, signing_key, synthetic_standing_v1, token, legacy_source,
)
from test_native_turns import Harness


def omit(fixture, *, reconcile=False, preview=False, resume=False, body=None):
    body = body or {
        "request_id": "owner-confirms-no-files-once", "follow": fixture.command,
        "confirmation": "do-not-transfer-workspace-files",
    }
    return fixture.case.client.post(PREFIX + "/environment/without-files" + (
        "/reconcile" if reconcile else "/preview" if preview else "/continue" if resume else ""),
        json=body, headers=fixture.case.headers)


def original_confirmation_read(fixture):
    command = next(command for command, _ in reversed(fixture.controls)
                   if command.get("operation") == "session_files_omission" and command.get("stage") == "confirm")
    return invoke(fixture.hosted, fixture.identity, {**command, "stage": "read", "missing": None})


@pytest.mark.parametrize("missing", ["source", "destination"])
def test_owner_can_confirm_no_files_without_replacing_the_original_follow(file_api, missing):
    fixture = file_api
    if missing == "source":
        legacy_source(fixture)
    else:
        fixture.runtime.workspace_files = None
    failed = follow(fixture)
    assert failed.status_code == 409 and failed.json()["error"]["code"] == "NATIVE_FILES_UNSUPPORTED"
    assert not fixture.files.stopped and not fixture.runtime.executor.calls
    confirmed = fixture.case.client.post(PREFIX + "/environment/without-files", json={
        "request_id": "owner-confirms-no-files-once", "follow": fixture.command,
        "confirmation": "do-not-transfer-workspace-files",
    }, headers=fixture.case.headers)
    assert confirmed.status_code == 200, confirmed.text
    receipt = confirmed.json()
    assert receipt["state"] == "ready" and receipt["request_id"] == fixture.command["request_id"]
    assert receipt["files_migrated"] is False
    assert receipt["files_skip_request_id"] == "owner-confirms-no-files-once"
    assert "files_manifest_sha256" not in receipt
    state = invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()
    assert state["hosted_session_id"] == fixture.destination.session_id
    assert state["environment"] == receipt
    assert not any(call[0] in {"put", "get", "list"} for call in fixture.files.calls)
    assert (fixture.workspace.root / "nested" / "draft.txt").read_bytes() == b"uncommitted original\n"
    if missing == "source":
        assert fixture.target_workspace.root.is_dir() and not tuple(fixture.target_workspace.root.iterdir())
    else:
        assert not fixture.target_workspace.root.exists()
    assert not fixture.runtime.executor.calls
    assert invoke(fixture.hosted, fixture.identity, NativeRead(request_id="original-rid")).json() == fixture.saved.model_dump(mode="json")


@pytest.mark.parametrize("file_api", [{"lane": "foundry-native-v1"}, {"lane": "foundry-responses-v1"}], indirect=True)
@pytest.mark.parametrize("continue_cid", [False, True])
def test_confirmation_restores_new_conversations_and_original_conversation_without_replay(file_api, continue_cid, signing_key):
    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    before = list(fixture.files.calls)
    preview = omit(fixture, preview=True)
    assert preview.status_code == 200 and preview.json()["state"] == "pending", preview.text
    assert preview.json().get("files_migrated") is None
    assert fixture.files.calls == before
    confirmed = omit(fixture)
    assert confirmed.status_code == 200 and confirmed.json()["files_migrated"] is False, confirmed.text
    path = "/work-policy/v1/" + (
        "responses-turns" if fixture.saved.state_profile == "foundry-responses-v1" else "native-turns") + "/submit"
    body = {"request_id": "fresh-after-explicit-confirmation", "content": "New instruction"}
    if continue_cid:
        body["conversation_id"] = fixture.saved.conversation_id
    elif fixture.saved.state_profile == "foundry-responses-v1":
        from test_native_responses_api import choose_responses
        choose_responses(fixture.case, signing_key)
    accepted = fixture.case.client.post(path, json=body, headers=fixture.case.headers)
    assert accepted.status_code == 202, accepted.text
    completed = Harness(fixture.runtime, fixture.runtime.executor, fixture.hosted).wait(body["request_id"], "completed")
    assert (completed.conversation_id == fixture.saved.conversation_id) is continue_cid
    assert [call[0] for call in fixture.runtime.executor.calls] == [body["request_id"]]
    assert (fixture.runtime.executor.calls[0][2] is not None) is continue_cid
    assert invoke(fixture.hosted, fixture.identity, NativeRead(request_id="original-rid")).json() == fixture.saved.model_dump(mode="json")


@pytest.mark.parametrize("legacy_cursor_profile", [False, True])
def test_required_files_intent_is_retained_with_separate_once_confirmation(file_api, legacy_cursor_profile):
    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409

    async def records():
        async with fixture.runtime._store(TURNS_STORE) as store:
            page = await store.list_keys(limit=100)
            return {key.key: (await store.get_item(key.key)).value for key in page.keys}

    async def old_shape(key):
        async with fixture.runtime._store(TURNS_STORE) as store:
            item = await store.get_item(key)
            value = dict(item.value)
            value.pop("cursor_profile", None)
            await store.set_item(key, value, if_match=item.etag)

    original = fixture.hosted.portal.call(records)
    key = next(key for key in original if key.startswith("following:"))
    if legacy_cursor_profile:
        fixture.hosted.portal.call(old_shape, key)
        original = fixture.hosted.portal.call(records)
    raw = canonical_bytes(original[key])
    assert original[key]["files_required"] is True and "files" not in original[key]
    confirmed = omit(fixture)
    assert confirmed.status_code == 200, confirmed.text
    after = fixture.hosted.portal.call(records)
    consent = next(value for key, value in after.items() if key.startswith("files-omission:"))
    assert canonical_bytes(consent["original"]) == raw
    assert after[key]["files_required"] is True
    assert after[key]["view"]["request_id"] == fixture.command["request_id"]
    before_io = list(fixture.files.calls)
    assert omit(fixture, reconcile=True).json() == confirmed.json()
    assert omit(fixture).json() == confirmed.json()
    assert fixture.files.calls == before_io and not fixture.runtime.executor.calls


@pytest.mark.parametrize("file_api", [
    {"lane": "foundry-native-v1"}, {"lane": "foundry-responses-v1"},
], indirect=True)
def test_terminal_unknown_allows_explicit_no_files_without_rewriting_or_replaying_history(file_api, signing_key):
    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409

    async def retained_unknown():
        original = await fixture.runtime._rid("original-rid")
        value = _changed(original.value, receipt=_changed(
            original.value.receipt, status="unknown", result=None, failure_code="NATIVE_TURN_UNKNOWN"))
        async with fixture.runtime._store(TURNS_STORE) as store:
            await store.set_item("rid:original-rid", _json(value), if_match=original.etag)
            return canonical_bytes((await store.get_item("rid:original-rid")).value)

    raw = fixture.hosted.portal.call(retained_unknown)
    calls = list(fixture.files.calls)
    preview = omit(fixture, preview=True)
    assert preview.status_code == 200, preview.text
    assert fixture.files.calls == calls
    ready = omit(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["files_migrated"] is False

    async def read_original():
        async with fixture.runtime._store(TURNS_STORE) as store:
            return canonical_bytes((await store.get_item("rid:original-rid")).value)

    assert fixture.hosted.portal.call(read_original) == raw
    assert not fixture.runtime.executor.calls and not fixture.original.executor.calls[1:]
    assert not any(call[0] in {"put", "get", "list"} for call in fixture.files.calls)
    responses = fixture.saved.state_profile == "foundry-responses-v1"
    if responses:
        from test_native_responses_api import choose_responses
        choose_responses(fixture.case, signing_key)
    accepted = fixture.case.client.post(
        "/work-policy/v1/" + ("responses-turns" if responses else "native-turns") + "/submit",
        headers=fixture.case.headers, json={"request_id": "new-work-after-history", "content": "A new conversation"})
    assert accepted.status_code == 202, accepted.text
    completed = Harness(fixture.runtime, fixture.runtime.executor, fixture.hosted).wait("new-work-after-history", "completed")
    assert completed.conversation_id != fixture.saved.conversation_id
    assert [call[0] for call in fixture.runtime.executor.calls] == ["new-work-after-history"]
    assert fixture.hosted.portal.call(read_original) == raw


@pytest.mark.parametrize("active", [False, True])
def test_file_check_deadline_resumes_the_original_cursor_before_deciding(file_api, monkeypatch, active):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from shared_context import native_environment
    from shared_context.behavior_native_sessions import NativeSessions

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409

    async def history():
        async with fixture.runtime._store(TURNS_STORE) as store:
            for index in range(40):
                await store.create_item(f"z-history:{index:03}", {"record": "unrelated retained state"})
            if active:
                from shared_context.behavior_native_turn_wire import NativeSubmit
                original = await fixture.runtime._rid("original-rid")
                submitted = NativeSubmit(command=fixture.saved.command.model_copy(update={
                    "request_id": "late-active", "conversation_id": fixture.saved.conversation_id}),
                    capture=fixture.saved.capture)
                value = _changed(original.value, receipt=_changed(
                    original.value.receipt, status="started", result=None, request_id="late-active",
                    command=submitted.command, submit_digest=submitted.sha256))
                await store.create_item("rid:late-active", _json(value))

    fixture.hosted.portal.call(history)
    page, cursors = native_environment._scan_page, []
    read_item, stopped = FoundryStateStore.get_item, []

    async def slow_active(store, key, **kwargs):
        if active and key == "rid:late-active" and not stopped:
            stopped.append(True)
            await asyncio.sleep(1)
        return await read_item(store, key, **kwargs)

    async def one_page_then_deadline(runtime, after, *args, **kwargs):
        cursors.append(after)
        result = await page(runtime, after, *args, **kwargs)
        if result[0] == "scan_pending":
            raise TimeoutError("OFFLINE deadline after one fully checked page")
        return result

    monkeypatch.setattr(native_environment, "_scan_page", one_page_then_deadline)
    monkeypatch.setattr(native_environment, "_CHECK_PAGE_KEYS", 16)
    if active:
        monkeypatch.setattr(native_environment, "_SCAN_SECONDS", 0.5)
    monkeypatch.setattr(FoundryStateStore, "get_item", slow_active)
    first = omit(fixture, preview=True)
    if not active:
        assert first.status_code == 200 and first.json()["reason"] == "scan_pending", first.text
        assert first.json()["files_check"]["category"] == "scan_pending"
        assert first.json().get("files_migrated") is None
    fixture.case.composition._native_sessions = NativeSessions(fixture.case.composition)
    result = first
    for _ in range(5):
        if result.status_code != 200 or result.json()["reason"] != "scan_pending":
            break
        result = omit(fixture, preview=True)
    assert cursors[0] is None
    if not active:
        assert all(value is not None for value in cursors[1:])
        assert len(cursors) == len(set(cursors)), "No restart may silently reset the scan to the first page"
    monkeypatch.setattr(native_environment, "_scan_page", page)
    if active:
        assert result.status_code == 409, result.text
        assert result.json()["error"]["files_check"] == {
            "category": "active_waiting", "request_id_prefix": "late-act",
            "conversation_id_prefix": fixture.saved.conversation_id[:8]}
        assert not fixture.files.stopped
    else:
        assert result.status_code == 200 and result.json()["reason"] != "scan_pending", result.text
        ready = omit(fixture)
        assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert not fixture.runtime.executor.calls


@pytest.mark.parametrize("queued", [False, True])
def test_completed_file_check_does_not_hide_a_new_admission(file_api, queued):
    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    assert omit(fixture, preview=True).status_code == 200

    async def admission():
        current, etag = await fixture.runtime._agent_record()
        value = _json(current)
        if not queued:
            value["admitting"] = {"request_id": "later-admission", "submit_digest": "e" * 64}
        async with fixture.runtime._store(TURNS_STORE) as store:
            if queued:
                original = await fixture.runtime._rid("original-rid")
                receipt = _changed(original.value.receipt, status="accepted", result=None, turn_number=None)
                await store.set_item("rid:original-rid", _json(_changed(original.value, receipt=receipt, run_token=None)),
                                     if_match=original.etag)
            await store.set_item("agent", value, if_match=etag)

    fixture.hosted.portal.call(admission)
    response = omit(fixture)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["files_check"]["category"] == ("active_waiting" if queued else "admission_pending")
    assert response.json()["error"]["files_check"]["request_id_prefix"] == ("original" if queued else "later-ad")
    assert response.json()["error"]["confirmation_not_dispatched"] is True
    assert not fixture.files.stopped and not fixture.runtime.executor.calls


@pytest.mark.parametrize("saved_unactivated", [False, True])
def test_completed_check_does_not_repeat_the_original_follow_scan_reason(file_api, monkeypatch, saved_unactivated):
    from shared_context import native_environment

    fixture = file_api
    legacy_source(fixture)

    async def pending(*_args, **_kwargs):
        return "scan_pending", None, False

    with monkeypatch.context() as slow:
        slow.setattr(native_environment, "_drain_pages", pending)
        original = follow(fixture)
    assert original.status_code == 200 and original.json()["reason"] == "scan_pending"
    before, _ = fixture.hosted.portal.call(native_environment._read, fixture.runtime)
    checked = omit(fixture, preview=True)
    assert checked.status_code == 200 and checked.json()["reason"] is None, checked.text
    after, _ = fixture.hosted.portal.call(native_environment._read, fixture.runtime)
    assert canonical_bytes(before) == canonical_bytes(after)
    if saved_unactivated:
        from azure.ai.agentserver.core.storage import FoundryStateStore
        from azure.core.exceptions import ServiceResponseError
        create = FoundryStateStore.create_item

        async def saved_but_not_active(store, key, value, **kwargs):
            result = await create(store, key, value, **kwargs)
            if key.startswith("files-omission:"):
                raise ServiceResponseError("OFFLINE saved original confirmation without activation")
            return result

        with monkeypatch.context() as interrupted:
            interrupted.setattr(FoundryStateStore, "create_item", saved_but_not_active)
            assert omit(fixture).status_code == 503
        assert original_confirmation_read(fixture).json()["files_confirmation_saved"]
        before, _ = fixture.hosted.portal.call(native_environment._read, fixture.runtime)
        checked = omit(fixture, preview=True)
        assert checked.status_code == 200 and checked.json()["reason"] is None, checked.text
        assert checked.json()["files_confirmation_saved"] == "owner-confirms-no-files-once"
        after, _ = fixture.hosted.portal.call(native_environment._read, fixture.runtime)
        assert canonical_bytes(before) == canonical_bytes(after)
        ready = omit(fixture, resume=True)
    else:
        ready = omit(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["request_id"] == original.json()["request_id"]
    assert not fixture.runtime.executor.calls


@pytest.mark.parametrize("pending", ["admission", "accepted", "started", "task", "checkpoint", "sending", "publishing"])
def test_no_files_never_bypasses_unresolved_work_or_file_material(file_api, pending, monkeypatch):
    fixture = file_api
    if pending in {"sending", "publishing"}:
        if pending == "sending":
            fixture.files.upload_error, fixture.files.target_read_error = True, 404
            assert follow(fixture).status_code == 503
        else:
            original_publish = fixture.target_workspace.publish
            def lose_publish(manifest):
                original_publish(manifest)
                from shared_context.errors import Problem
                raise Problem(503, "NATIVE_FILES_UNKNOWN", "OFFLINE publication ACK lost")
            monkeypatch.setattr(fixture.target_workspace, "publish", lose_publish)
            assert follow(fixture).status_code == 503
    else:
        legacy_source(fixture)
        assert follow(fixture).status_code == 409
    legacy_source(fixture)

    async def mutate():
        if pending == "admission":
            current, etag = await fixture.runtime._agent_record()
            async with fixture.runtime._store(TURNS_STORE) as store:
                value = _json(current)
                value["admitting"] = {"request_id": "new-unknown-admission", "submit_digest": "f" * 64}
                await store.set_item("agent", value, if_match=etag)
        elif pending in {"accepted", "started"}:
            current = await fixture.runtime._rid("original-rid")
            receipt = _changed(current.value.receipt, status=pending,
                               result=None, turn_number=None if pending == "accepted" else 1)
            async with fixture.runtime._store(TURNS_STORE) as store:
                await store.set_item("rid:original-rid", _json(_changed(
                    current.value, receipt=receipt, run_token=None if pending == "accepted" else "original-token")),
                    if_match=current.etag)
        elif pending == "checkpoint":
            async with fixture.runtime._store(conversation_store_name(fixture.saved.conversation_id)) as store:
                item = await store.get_item("turn:1")
                value = dict(item.value)
                value["checkpoints"][0]["sha256"] = "f" * 64
                await store.set_item("turn:1", value, if_match=item.etag)

    fixture.hosted.portal.call(mutate)
    if pending == "task":
        async def busy(*args, **kwargs):
            return False
        fixture.runtime.task_observer = SimpleNamespace(quiet=busy)
    before = list(fixture.files.calls)
    preview, result = omit(fixture, preview=True), omit(fixture)
    assert preview.status_code >= 400, preview.text
    assert result.status_code >= 400, result.text
    if pending in {"accepted", "started"}:
        assert preview.json()["error"]["files_check"]["category"] == "active_waiting"
        assert preview.json()["error"]["files_check"]["request_id_prefix"] == "original"
        assert result.json()["error"]["files_check"] == preview.json()["error"]["files_check"]
    if pending == "admission":
        assert preview.json()["error"]["files_check"]["category"] == "admission_pending"
        assert preview.json()["error"]["files_check"]["request_id_prefix"] == "new-unkn"
    assert fixture.files.calls == before
    assert not fixture.runtime.executor.calls


def test_pre_patch_destination_is_rejected_before_sending_any_new_control_shape(file_api, monkeypatch):
    fixture = file_api
    transport = fixture.case.foundry.http._transport
    commands = []

    def legacy_state(packet):
        command = json.loads(packet.content)
        commands.append(command)
        assert command["operation"] == "session_read", "An old image must not receive the new omission command"
        reply = transport.handle_request(packet)
        state = reply.json()
        state.pop("files_profile", None)
        state.pop("files_omission_profile", None)
        return httpx.Response(200, json=state, headers=reply.headers)

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(legacy_state))
    failed = follow(fixture)
    assert failed.status_code == 409
    result = omit(fixture)
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_FILES_CONFIRMATION_UNSUPPORTED"
    assert not fixture.runtime.executor.calls and all(command["operation"] == "session_read" for command in commands)


@pytest.mark.parametrize("saved_unactivated,original_unknown_stage,later_source_drift", [
    (False, "check", False), (False, "confirm", False), (True, None, False), (True, None, True),
], ids=["old-check-unknown", "old-confirm-unknown", "saved-new-confirmation", "saved-new-confirmation-source-drifts"])
def test_blocked_old_destination_is_linked_to_one_new_follow_without_changing_its_original_intent(
        file_api, monkeypatch, saved_unactivated, original_unknown_stage, later_source_drift):
    from shared_context.behavior_native_sessions import native_session_id
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
    from shared_context.errors import Problem
    from shared_context.native_environment import _key
    from shared_context.native_turns import NativeTurnRuntime
    from shared_context.native_workspace_files import NativeWorkspaceFiles
    from test_native_turns import OfflineSdk

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    old_key = _key(fixture.runtime)
    if not saved_unactivated:
        fixture.lost.update(operation="session_files_omission", stage=original_unknown_stage)
        old_confirmation = {"request_id": "unknown-confirmation-for-old-target", "follow": fixture.command,
                            "confirmation": "do-not-transfer-workspace-files"}
        assert omit(fixture, body=old_confirmation).status_code == 503

    async def read_original():
        async with fixture.runtime._store(TURNS_STORE) as store:
            return (await store.get_item(old_key)).value

    original = fixture.hosted.portal.call(read_original)
    if saved_unactivated:
        fixture.files.targets[fixture.destination.session_id] = fixture.destination.model_copy(update={"agent_version": "43"})
    next_target = fixture.destination.model_copy(update={
        "agent_version": "43", "session_id": native_session_id(
            fixture.source, fixture.case.composition.config.identity.managed_identity_client_id, version="43")})
    fixture.files.targets[next_target.session_id] = next_target
    identity = fixture.identity.model_copy(update={"session_id": next_target.session_id, "agent_version": "43"})
    target_files = NativeWorkspaceFiles(fixture.target_workspace.home.parent / "new-control-image")
    runtime = NativeTurnRuntime(identity, OfflineSdk(), workspace_files=target_files,
                                task_observer=fixture.runtime.task_observer,
                                bootstrap_source_inactive=fixture.runtime.bootstrap_source_inactive)
    transport = fixture.case.foundry.http._transport

    def forwarded(packet):
        if packet.url.params["agent_session_id"] != identity.session_id:
            return transport.handle_request(packet)
        command = NATIVE_REQUEST.validate_json(packet.content)
        try:
            value = fixture.hosted.portal.call(runtime.dispatch, command)
        except Problem as error:
            return httpx.Response(error.status, json=error.body)
        return httpx.Response(200, content=value.model_dump_json().encode(), headers={
            "content-type": "application/json", "x-agent-session-id": identity.session_id,
            "x-agent-invocation-id": "new-control-image"})

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(forwarded))
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_a, **_kw: "43")
    body = {"request_id": "explicit-new-version-no-files", "follow": fixture.command,
            "expected_agent_version": "43", "confirmation": "do-not-transfer-workspace-files"}
    preview = omit(fixture, preview=True, body=body)
    assert preview.status_code == 200 and preview.json()["state"] in {"pending", "confirmation_required"}, preview.text
    assert fixture.hosted.portal.call(read_original) == original
    if saved_unactivated:
        from azure.ai.agentserver.core.storage import FoundryStateStore
        from azure.core.exceptions import ServiceResponseError
        create, confirmations = FoundryStateStore.create_item, []

        async def lost_after_save(store, key, value, **kwargs):
            result = await create(store, key, value, **kwargs)
            if key.startswith("files-omission:"):
                confirmations.append(key)
                raise ServiceResponseError("OFFLINE immutable confirmation saved, activation not run")
            return result

        with monkeypatch.context() as lost:
            lost.setattr(FoundryStateStore, "create_item", lost_after_save)
            assert omit(fixture, body=body).status_code == 503
        digest = material_digest(NativeEnvironmentWithoutFiles.model_validate(body))
        state = fixture.hosted.portal.call(runtime.dispatch, NativeSessionRead())
        readback = fixture.hosted.portal.call(runtime.dispatch, NATIVE_REQUEST.validate_python({
            "operation": "session_files_omission", "request_id": body["request_id"], "stage": "read",
            "confirmation_digest": digest,
            "follow": {"operation": "session_follow", "request_id": state.environment.request_id,
                       "expected_session_id": fixture.source.session_id, "expected_etag": state.etag,
                       "stage": "prepare", "idle_only": True, "files_required": True},
            "pending_request_id": fixture.command["request_id"], "pending_session_id": fixture.destination.session_id}))
        assert readback.files_confirmation_saved == body["request_id"]
        if later_source_drift:
            fixture.files.targets[fixture.source.session_id] = fixture.source.model_copy(update={"agent_version": "43"})
        checked = omit(fixture, preview=True, body=body)
        assert checked.status_code == 200 and checked.json().get("files_confirmation_saved") == body["request_id"], checked.text
        confirmed = omit(fixture, resume=True, body=body)
        assert len(confirmations) == 1
    else:
        confirmed = omit(fixture, body=body)
    assert confirmed.status_code == 200 and confirmed.json()["state"] == "ready", confirmed.text
    value = confirmed.json()
    assert value["request_id"] != fixture.command["request_id"]
    assert value["files_migrated"] is False and value["hosted_session_id"] == identity.session_id
    if not saved_unactivated:
        assert value["files_skip_request_id"] != old_confirmation["request_id"]
    retained = dict(fixture.hosted.portal.call(read_original))
    assert retained.pop("retarget_request_id") == value["request_id"]
    assert retained == original and retained["files_required"] is True
    assert omit(fixture, reconcile=True, body=body).json() == value
    assert target_files.root.is_dir() and not tuple(target_files.root.iterdir())
    assert not any(call[0] in {"put", "get", "list"} for call in fixture.files.calls)
    assert not runtime.executor.calls


def test_available_helpers_cannot_be_overridden_by_confirmation(file_api):
    result = omit(file_api)
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_FILES_CONFLICT"
    assert not file_api.files.stopped
    completed = follow(file_api)
    assert completed.status_code == 200 and completed.json()["files_migrated"] is True, completed.text
    assert completed.json()["files_manifest_sha256"]
    assert "files_skip_request_id" not in completed.json()


@pytest.mark.parametrize("remote", ["absent", "saved", "ready"])
def test_same_confirmation_id_reentry_reaches_ready_without_a_second_effect(file_api, monkeypatch, remote):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from azure.core.exceptions import ServiceResponseError

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    create, writes = FoundryStateStore.create_item, []
    body = {"request_id": "same-confirmation-through-unknown", "follow": fixture.command,
            "confirmation": "do-not-transfer-workspace-files"}

    async def counted_create(store, key, value, **kwargs):
        result = await create(store, key, value, **kwargs)
        if key.startswith("files-omission:"):
            writes.append(canonical_bytes(value))
            if remote == "saved" and len(writes) == 1:
                raise ServiceResponseError("OFFLINE original confirmation saved before ACK loss")
        return result

    monkeypatch.setattr(FoundryStateStore, "create_item", counted_create)
    if remote != "absent":
        first = omit(fixture, body=body)
        assert first.status_code == (503 if remote == "saved" else 200), first.text
    recovered = omit(fixture, body=body, reconcile=True)
    assert recovered.status_code == 200 and recovered.json()["state"] == "ready", recovered.text
    assert recovered.json()["files_skip_request_id"] == body["request_id"]
    assert recovered.json()["files_migrated"] is False
    assert omit(fixture, body=body, reconcile=True).json() == recovered.json()
    assert len(writes) == 1
    assert not fixture.runtime.executor.calls
    assert invoke(fixture.hosted, fixture.identity, NativeRead(request_id="original-rid")).json() == fixture.saved.model_dump(mode="json")


def test_one_confirmation_id_cannot_acquire_a_different_body_digest(file_api):
    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    body = {"request_id": "body-bound-confirmation", "follow": fixture.command,
            "confirmation": "do-not-transfer-workspace-files"}
    original = omit(fixture, body=body)
    assert original.status_code == 200 and original.json()["state"] == "ready"
    changed = {**body, "follow": {**fixture.command, "expected_session_id": fixture.source.session_id}}
    different = omit(fixture, body=changed, reconcile=True)
    assert different.status_code == 409, different.text
    assert different.json()["error"]["code"] == "NATIVE_FILES_CONFLICT"
    assert not fixture.runtime.executor.calls


@pytest.mark.parametrize("explicit_source,lose_new_ack", [(False, False), (True, True)])
def test_new_target_uses_the_current_ready_source_after_the_old_ready_ack_was_lost(
        file_api, monkeypatch, explicit_source, lose_new_ack):
    from shared_context.behavior_native_sessions import native_session_id
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
    from shared_context.errors import Problem
    from shared_context.native_turns import NativeTurnRuntime
    from shared_context.native_workspace_files import NativeWorkspaceFiles
    from test_native_turns import OfflineSdk

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    fixture.lost.update(operation="session_follow", stage="commit")
    assert omit(fixture).status_code == 503
    old_ready = fixture.hosted.portal.call(fixture.runtime.session_state).environment
    assert old_ready.state == "ready" and old_ready.hosted_session_id == fixture.destination.session_id
    fixture.files.targets[fixture.destination.session_id] = fixture.destination.model_copy(update={"agent_version": "43"})
    target = fixture.destination.model_copy(update={
        "agent_version": "43", "session_id": native_session_id(
            fixture.source, fixture.case.composition.config.identity.managed_identity_client_id, version="43")})
    identity = fixture.identity.model_copy(update={"session_id": target.session_id, "agent_version": "43"})
    fixture.files.targets[target.session_id] = target
    fixture.files.source_sid = fixture.destination.session_id

    async def inactive(sid):
        assert sid == fixture.destination.session_id
        return sid in fixture.files.stopped

    async def quiet(sid, **_kwargs):
        assert sid == fixture.destination.session_id
        return True

    runtime = NativeTurnRuntime(
        identity, OfflineSdk(), workspace_files=NativeWorkspaceFiles(fixture.target_workspace.home.parent / "fresh-ready-43"),
        task_observer=SimpleNamespace(quiet=quiet), bootstrap_source_inactive=inactive)
    calls, lost = [], []

    def forward(packet):
        assert packet.url.params["agent_session_id"] == target.session_id
        command = NATIVE_REQUEST.validate_json(packet.content)
        calls.append(command.operation)
        try:
            value = fixture.hosted.portal.call(runtime.dispatch, command)
        except Problem as error:
            return httpx.Response(error.status, json=error.body)
        if (lose_new_ack and not lost and command.operation == "session_files_omission"
                and command.stage == "confirm"):
            lost.append(True)
            raise httpx.ReadTimeout("OFFLINE new confirmation ACK lost", request=packet)
        return httpx.Response(200, content=value.model_dump_json().encode(), headers={
            "content-type": "application/json", "x-agent-session-id": target.session_id,
            "x-agent-invocation-id": "fresh-current-reader"})

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(forward))
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_a, **_k: "43")
    old_follow = {**fixture.command, **({"expected_session_id": fixture.source.session_id} if explicit_source else {})}
    body = {"request_id": "new-confirmation-after-ready-ack-loss", "follow": old_follow,
            "expected_agent_version": "43", "confirmation": "do-not-transfer-workspace-files"}
    preview = omit(fixture, preview=True, body=body)
    assert preview.status_code == 200, preview.text
    ready = omit(fixture, body=body)
    if lose_new_ack:
        assert ready.status_code == 503 and lost, ready.text
        ready = omit(fixture, body=body, reconcile=True)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["previous_hosted_session_id"] == fixture.destination.session_id
    assert ready.json()["previous_agent_version"] == fixture.destination.agent_version
    assert ready.json()["hosted_session_id"] == target.session_id
    assert "session_retarget" not in calls
    assert omit(fixture, body=body, reconcile=True).json() == ready.json()
    assert not runtime.executor.calls
    assert fixture.hosted.portal.call(runtime.dispatch, NativeRead(request_id="original-rid")).model_dump(mode="json") == fixture.saved.model_dump(mode="json")


def test_late_original_and_same_id_reentry_create_one_immutable_confirmation(file_api, monkeypatch):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    assert omit(fixture, preview=True).status_code == 200
    command = next(command for command, _ in reversed(fixture.controls)
                   if command.get("operation") == "session_files_omission" and command.get("stage") == "check")
    command = NATIVE_REQUEST.validate_python({**command, "stage": "confirm"})
    create, attempts, committed = FoundryStateStore.create_item, [], []
    both = asyncio.Event()

    async def create_together(store, key, value, **kwargs):
        if key.startswith("files-omission:"):
            attempts.append(key)
            if len(attempts) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 5)
        result = await create(store, key, value, **kwargs)
        if key.startswith("files-omission:"):
            committed.append(canonical_bytes(value))
        return result

    async def concurrent():
        return await asyncio.gather(fixture.runtime.dispatch(command), fixture.runtime.dispatch(command))

    monkeypatch.setattr(FoundryStateStore, "create_item", create_together)
    results = fixture.hosted.portal.call(concurrent)
    assert results[0] == results[1]
    assert results[0].files_skip_request_id == command.request_id
    assert len(committed) == 1
    ready = omit(fixture, reconcile=True)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert len(committed) == 1 and not fixture.runtime.executor.calls


def test_confirmation_ack_loss_completes_the_original_follow_without_rewriting_confirmation_or_turn(file_api):
    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    fixture.lost.update(operation="session_files_omission", stage="confirm")
    assert omit(fixture).status_code == 503
    before = list(fixture.files.calls)
    value = omit(fixture, reconcile=True)
    assert value.status_code == 200 and value.json()["state"] == "ready", value.text
    assert value.json()["files_migrated"] is False
    assert fixture.files.calls[len(before):] == [("stop", fixture.source.session_id)]
    completed = omit(fixture, reconcile=True)
    assert completed.status_code == 200 and completed.json() == value.json()
    assert sum(command.get("stage") == "confirm" for command, _ in fixture.controls) == 1
    assert completed.json()["request_id"] == fixture.command["request_id"]
    assert not fixture.runtime.executor.calls


@pytest.mark.parametrize("original_reason,later_source_drift,reconcile,restore_helper", [
    ("source", False, False, False), ("destination", True, False, False),
    ("destination", True, True, False), ("destination", True, False, True),
], ids=["source", "destination-resume", "destination-reconcile", "original-reason-no-longer-valid"])
def test_saved_immutable_confirmation_without_activation_can_be_read_and_explicitly_continued(
        file_api, monkeypatch, original_reason, later_source_drift, reconcile, restore_helper):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from azure.core.exceptions import ServiceResponseError

    fixture = file_api
    if original_reason == "source":
        legacy_source(fixture)
    else:
        fixture.runtime.workspace_files = None
    assert follow(fixture).status_code == 409
    create, writes, saved_bytes = FoundryStateStore.create_item, [], []
    workspace = fixture.target_workspace

    async def lose_ack(store, key, value, **kwargs):
        result = await create(store, key, value, **kwargs)
        if key.startswith("files-omission:"):
            writes.append(key)
            saved_bytes.append(canonical_bytes(value))
            raise ServiceResponseError("OFFLINE immutable confirmation committed; ACK lost before activation")
        return result

    monkeypatch.setattr(FoundryStateStore, "create_item", lose_ack)
    assert omit(fixture).status_code == 503
    before = list(fixture.files.calls)
    for _ in range(2):
        observed = original_confirmation_read(fixture)
        assert observed.status_code == 200, observed.text
        assert observed.json()["files_confirmation_saved"] == "owner-confirms-no-files-once"
        assert observed.json()["files_confirmation_reason"] == original_reason
        assert observed.json().get("files_migrated") is None and observed.json()["state"] == "pending"
    assert fixture.files.calls == before and not fixture.files.stopped
    if later_source_drift:
        fixture.files.targets[fixture.source.session_id] = fixture.source.model_copy(update={"agent_version": "42"})
    if restore_helper:
        fixture.runtime.workspace_files = workspace
    ready = omit(fixture, resume=not reconcile, reconcile=reconcile)
    if restore_helper:
        assert ready.status_code == 409 and ready.json()["error"]["code"] == "NATIVE_FILES_CONFLICT", ready.text
        assert fixture.files.calls == before
    else:
        assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
        assert ready.json()["files_migrated"] is False
    assert len(writes) == 1 and not fixture.runtime.executor.calls

    async def unchanged():
        async with fixture.runtime._store(TURNS_STORE) as store:
            return canonical_bytes((await store.get_item(writes[0])).value)

    assert fixture.hosted.portal.call(unchanged) == saved_bytes[0]


def test_staged_consent_does_not_override_a_file_manifest_that_wins_the_original_cas(file_api, monkeypatch):
    from shared_context.native_environment import _key
    from shared_context.native_files_wire import NativeFilesProgress
    from shared_context.native_turns import NativeTurnRuntime

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    immutable = NativeTurnRuntime._immutable
    original_manifest = fixture.workspace.snapshot(
        agent_id=fixture.identity.agent_id, request_id=fixture.command["request_id"],
        source_session_id=fixture.source.session_id, source_agent_version="41",
        target_session_id=fixture.destination.session_id, target_agent_version="42")

    async def raced(runtime, store, key, value):
        await immutable(runtime, store, key, value)
        if key.startswith("files-omission:"):
            item = await store.get_item(_key(runtime))
            current = dict(item.value)
            current["files"] = NativeFilesProgress(manifest=original_manifest).model_dump(mode="json")
            current["view"]["files_manifest_sha256"] = original_manifest.sha256
            await store.set_item(_key(runtime), current, if_match=item.etag)

    monkeypatch.setattr(NativeTurnRuntime, "_immutable", raced)
    response = omit(fixture)
    assert response.status_code == 503, response.text
    assert not fixture.files.stopped and not fixture.runtime.executor.calls
    readback = omit(fixture, reconcile=True)
    assert readback.status_code == 409 and readback.json()["error"]["code"] == "NATIVE_FILES_CONFLICT"
    state = invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()
    assert state["following"]["files_manifest_sha256"] == original_manifest.sha256
    assert "files_migrated" not in state["following"]


@pytest.mark.parametrize("tamper", ["source", "follow", "idle"])
def test_same_confirmation_cannot_be_used_for_a_different_original_operation(file_api, tamper):
    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    assert omit(fixture).status_code == 200
    changed = dict(fixture.command)
    if tamper == "source":
        changed["expected_session_id"] = "another-source"
    elif tamper == "follow":
        changed["request_id"] = "another-follow"
    else:
        changed["idle_only"] = False
    response = omit(fixture, body={
        "request_id": "owner-confirms-no-files-once", "follow": changed,
        "confirmation": "do-not-transfer-workspace-files"})
    assert response.status_code == 409, response.text
    assert not fixture.runtime.executor.calls
