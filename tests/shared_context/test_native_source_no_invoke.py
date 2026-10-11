"""A retired or unverified source is never invoked just to decide whether files can be omitted."""

import json

import httpx
import pytest

from shared_context.behavior_native_turn_wire import NativeRead, NativeSessionRead
from test_native_environment import invoke
from test_native_files_api import (
    file_api, file_loops, follow, instruction_models, jwk_client, local_env,
    native_api, signing_key, synthetic_standing_v1, token, legacy_source,
)
from test_native_files_omission import omit


@pytest.mark.parametrize("file_api", [
    {"lane": "foundry-native-v1", "source_version": "37", "target_version": "47"},
    {"lane": "foundry-responses-v1", "source_version": "37", "target_version": "47"},
], indirect=True)
def test_reconcile_drifted_destination_never_invokes_the_original_sid(file_api, monkeypatch):
    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    fixture.files.targets[fixture.source.session_id] = fixture.source.model_copy(update={"agent_version": "47"})
    fixture.files.targets[fixture.destination.session_id] = fixture.destination.model_copy(update={"agent_version": "51"})
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_a, **_k: "51")
    calls = []

    def changed_session(packet):
        calls.append((packet.url.params["agent_session_id"], json.loads(packet.content)))
        return httpx.Response(409, json={"error": {"code": "NATIVE_SESSION_CHANGED"}})

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(changed_session))
    response = fixture.case.client.post("/work-policy/v1/native-turns/environment/reconcile",
        json=fixture.command, headers=fixture.case.headers)
    assert response.status_code == 409 and response.json()["error"]["code"] == "NATIVE_SESSION_CHANGED", response.text
    assert calls == [], "Metadata already proves drift; reconciliation must not invoke either original SID"
    assert not fixture.runtime.executor.calls and not fixture.files.stopped


@pytest.mark.parametrize("file_api", [
    {"lane": "foundry-native-v1", "source_version": "37", "target_version": "47"},
    {"lane": "foundry-responses-v1", "source_version": "37", "target_version": "47"},
], indirect=True)
@pytest.mark.parametrize("blocker", [
    "none", "helper", "source-only", "unchanged-helpers", "accepted", "started",
    "unknown", "admission", "task", "checkpoint", "sending",
])
def test_drifted_pending_follow_checks_metadata_then_confirms_one_new_reader(file_api, monkeypatch, blocker):
    from types import SimpleNamespace
    from shared_context.behavior_foundry import FoundryHttpClient
    from shared_context.behavior_native_sessions import native_session_id
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
    from shared_context.errors import Problem
    from shared_context.native_turns import NativeTurnRuntime, TURNS_STORE, _changed, _json
    from shared_context.native_workspace_files import NativeWorkspaceFiles
    from test_native_turns import OfflineSdk

    fixture = file_api
    if blocker in {"helper", "source-only", "unchanged-helpers"}:
        fixture.lost.update(operation="session_follow", stage="prepare")
        assert follow(fixture).status_code == 503
    elif blocker == "sending":
        fixture.files.upload_error, fixture.files.target_read_error = True, 404
        assert follow(fixture).status_code == 503
    else:
        legacy_source(fixture)
        assert follow(fixture).status_code == 409
    original = invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()["environment"]
    old_io = list(fixture.files.calls)

    async def pending_work():
        if blocker in {"accepted", "started", "unknown"}:
            item = await fixture.runtime._rid("original-rid")
            receipt = _changed(item.value.receipt, status=blocker, result=None,
                               turn_number=None if blocker == "accepted" else 1,
                               response_id=None if blocker == "accepted" else item.value.receipt.response_id)
            async with fixture.runtime._store(TURNS_STORE) as store:
                await store.set_item("rid:original-rid", _json(_changed(
                    item.value, receipt=receipt, run_token=None if blocker == "accepted" else "original-token")),
                    if_match=item.etag)
        elif blocker == "admission":
            current, etag = await fixture.runtime._agent_record()
            async with fixture.runtime._store(TURNS_STORE) as store:
                raw = _json(current)
                raw["admitting"] = {"request_id": "undecided-admission", "submit_digest": "e" * 64}
                await store.set_item("agent", raw, if_match=etag)
        elif blocker == "checkpoint":
            from shared_context.native_checkpoint import conversation_store_name
            async with fixture.runtime._store(conversation_store_name(fixture.saved.conversation_id)) as store:
                item = await store.get_item("turn:1")
                raw = dict(item.value)
                raw["checkpoints"][0]["sha256"] = "f" * 64
                await store.set_item("turn:1", raw, if_match=item.etag)

    fixture.hosted.portal.call(pending_work)
    original_receipt = fixture.hosted.portal.call(
        fixture.runtime.dispatch, NativeRead(request_id="original-rid")).model_dump(mode="json")
    if blocker not in {"helper", "unchanged-helpers"}:
        fixture.files.targets[fixture.source.session_id] = fixture.source.model_copy(update={"agent_version": "47"})
    if blocker not in {"source-only", "unchanged-helpers"}:
        fixture.files.targets[fixture.destination.session_id] = fixture.destination.model_copy(update={"agent_version": "51"})
    target = fixture.destination.model_copy(update={
        "agent_version": "51", "session_id": native_session_id(
            fixture.source, fixture.case.composition.config.identity.managed_identity_client_id, version="51")})
    identity = fixture.identity.model_copy(update={"session_id": target.session_id, "agent_version": "51"})
    runtime = NativeTurnRuntime(
        identity, OfflineSdk(), workspace_files=NativeWorkspaceFiles(fixture.target_workspace.home.parent / "fresh-51"),
        task_observer=fixture.runtime.task_observer, bootstrap_source_inactive=fixture.runtime.bootstrap_source_inactive)
    if blocker == "task":
        async def task_active(*_args, **_kwargs):
            return False
        runtime.task_observer = SimpleNamespace(quiet=task_active)
    calls, creates = [], []

    def create(client, *, before_send):
        before_send()
        assert client.target == target and client.credential is fixture.case.foundry.credential
        creates.append(client.target.session_id)
        fixture.files.targets[target.session_id] = target

    def forward(packet):
        sid = packet.url.params["agent_session_id"]
        calls.append((sid, json.loads(packet.content)))
        assert sid == target.session_id, "No RPC may target either drifted original session"
        assert packet.headers["x-client-fa-expected-version"] == "51"
        try:
            value = fixture.hosted.portal.call(runtime.dispatch, NATIVE_REQUEST.validate_json(packet.content))
        except Problem as error:
            return httpx.Response(error.status, json=error.body)
        return httpx.Response(200, content=value.model_dump_json().encode(), headers={
            "content-type": "application/json", "x-agent-session-id": sid, "x-agent-invocation-id": "fresh-reader"})

    monkeypatch.setattr(FoundryHttpClient, "create_session", create)
    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(forward))
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_a, **_k: "51")
    body = {"request_id": "confirm-original-drift-once", "follow": fixture.command,
            "expected_agent_version": "51", "confirmation": "do-not-transfer-workspace-files"}
    preview = omit(fixture, preview=True, body=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["state"] == "confirmation_required"
    assert preview.json()["original_work_checked"] is False
    assert preview.json()["original_hosted_agent_version"] == "47"
    assert preview.json()["observed_agent_version"] == ("47" if blocker in {"source-only", "unchanged-helpers"} else "51")
    assert calls == [] and creates == [], "Metadata preview cannot start a reader or claim the original work is idle"
    ready = omit(fixture, body=body)
    if blocker not in {"none", "helper", "source-only", "unknown"}:
        assert ready.status_code >= 400, ready.text
        if blocker != "checkpoint":
            assert ready.json()["error"]["confirmation_not_dispatched"] is True
            assert ready.json()["error"]["request_id"] == body["request_id"]
        after = fixture.hosted.portal.call(runtime.dispatch, NativeSessionRead())
        assert after.environment.model_dump(mode="json") == original
        assert fixture.files.calls == old_io and not runtime.executor.calls
        assert not any(call[1]["operation"] in {"session_follow", "session_retarget", "session_files"} for call in calls)
        assert not any(call[1]["operation"] == "session_files_omission" and call[1]["stage"] == "confirm" for call in calls)
        return
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["request_id"] != original["request_id"]
    assert ready.json()["hosted_session_id"] == target.session_id
    assert ready.json()["previous_hosted_session_id"] == fixture.source.session_id
    assert ready.json()["previous_agent_version"] == "37"
    assert ready.json()["files_migrated"] is False
    assert creates == [target.session_id]
    assert not runtime.executor.calls
    assert not any(call[0] in {"put", "get", "list"} for call in fixture.files.calls)
    assert omit(fixture, reconcile=True, body=body).json() == ready.json()
    assert [call[0] for call in calls if call[1].get("operation") == "session_files"] == []
    assert fixture.hosted.portal.call(runtime.dispatch, NativeRead(request_id="original-rid")).model_dump(mode="json") == original_receipt


@pytest.mark.parametrize("file_api", [
    {"lane": "foundry-native-v1", "source_version": "35", "target_version": "48"},
    {"lane": "foundry-responses-v1", "source_version": "35", "target_version": "48"},
], indirect=True)
def test_legacy_source_invocation_would_return_409_but_manual_cutover_never_invokes_it(file_api, monkeypatch):
    fixture = file_api
    legacy_source(fixture)
    transport = fixture.case.foundry.http._transport
    source_calls = []

    def reject_source(packet):
        if packet.url.params["agent_session_id"] == fixture.source.session_id:
            source_calls.append(json.loads(packet.content))
            return httpx.Response(409, json={"error": {
                "code": "NATIVE_SESSION_CHANGED", "message": "OFFLINE source would resume under another version"}})
        return transport.handle_request(packet)

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(reject_source))
    result = follow(fixture)
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] in {"NATIVE_FILES_UNSUPPORTED", "NATIVE_FILES_SOURCE_UNAVAILABLE"}, source_calls
    preview = omit(fixture, preview=True)
    assert preview.status_code == 200, preview.text
    ready = omit(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["files_migrated"] is False
    assert source_calls == []
    state = invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()
    assert state["environment"] == ready.json()
    assert invoke(fixture.hosted, fixture.identity, NativeRead(request_id="original-rid")).json() == fixture.saved.model_dump(mode="json")
    assert not fixture.runtime.executor.calls
    assert not any(call[0] in {"get", "list", "put"} for call in fixture.files.calls)


def test_declared_source_version_drift_uses_metadata_only(file_api, monkeypatch):
    fixture = file_api
    fixture.files.targets[fixture.source.session_id] = fixture.source.model_copy(update={"agent_version": "42"})
    transport = fixture.case.foundry.http._transport

    def no_source(packet):
        assert packet.url.params["agent_session_id"] != fixture.source.session_id
        return transport.handle_request(packet)

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(no_source))
    response = follow(fixture)
    assert response.status_code == 409 and response.json()["error"]["code"] == "NATIVE_FILES_SOURCE_UNAVAILABLE"
    assert omit(fixture, preview=True).status_code == 200
    ready = omit(fixture)
    assert ready.status_code == 200 and ready.json()["files_migrated"] is False


@pytest.mark.parametrize("code", ["NATIVE_SESSION_CHANGED", "AgentSessionVersionMismatch"])
def test_source_409_is_recorded_once_and_never_invoked_again_for_preview_or_cutover(file_api, monkeypatch, code):
    fixture = file_api
    transport = fixture.case.foundry.http._transport
    calls = []

    def refuse(packet):
        if packet.url.params["agent_session_id"] == fixture.source.session_id:
            calls.append(json.loads(packet.content))
            return httpx.Response(409, json={"error": {"code": code}})
        return transport.handle_request(packet)

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(refuse))
    assert follow(fixture).json()["error"]["code"] == "NATIVE_FILES_SOURCE_UNAVAILABLE"
    assert len(calls) == 1 and calls[0]["operation"] == "session_files" and calls[0]["stage"] == "inspect"
    assert follow(fixture).json()["error"]["code"] == "NATIVE_FILES_SOURCE_UNAVAILABLE"
    assert omit(fixture, preview=True).status_code == 200
    assert omit(fixture).json()["files_migrated"] is False
    assert len(calls) == 1


@pytest.mark.parametrize("file_api", [
    {"lane": "foundry-native-v1", "source_version": "37", "target_version": "47"},
    {"lane": "foundry-responses-v1", "source_version": "37", "target_version": "47"},
], indirect=True)
def test_known_source_37_uses_its_own_version_when_target_selector_is_47(file_api):
    fixture = file_api
    ready = follow(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["previous_agent_version"] == "37" and ready.json()["hosted_agent_version"] == "47"
    assert ready.json()["files_migrated"] is True
    source = [entry for entry in fixture.control_versions if entry[0] == fixture.source.session_id]
    destination = [entry for entry in fixture.control_versions if entry[0] == fixture.destination.session_id]
    assert source and all(version == "37" and operation == "session_files" for _, version, operation in source)
    assert destination and all(version == "47" for _, version, _ in destination)
    assert (fixture.target_workspace.root / "nested" / "draft.txt").read_bytes() == b"uncommitted original\n"
    assert not fixture.runtime.executor.calls


@pytest.mark.parametrize("status", ["accepted", "started"])
def test_manual_follow_with_unavailable_source_keeps_pending_original_work(file_api, monkeypatch, status):
    from shared_context.native_turns import TURNS_STORE, _changed, _json

    fixture = file_api
    legacy_source(fixture)
    fixture.command.pop("idle_only")
    source_calls = []
    transport = fixture.case.foundry.http._transport

    def no_source(packet):
        if packet.url.params["agent_session_id"] == fixture.source.session_id:
            source_calls.append(True)
            pytest.fail("pending work must be checked in the destination's original FA store, not the source")
        return transport.handle_request(packet)

    async def pending():
        current = await fixture.runtime._rid("original-rid")
        receipt = _changed(current.value.receipt, status=status, result=None,
                           turn_number=None if status == "accepted" else 1)
        value = _changed(current.value, receipt=receipt, run_token=None if status == "accepted" else "original-token")
        async with fixture.runtime._store(TURNS_STORE) as store:
            await store.set_item("rid:original-rid", _json(value), if_match=current.etag)
        return receipt.model_dump(mode="json")

    original = fixture.hosted.portal.call(pending)
    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(no_source))
    result = follow(fixture)
    assert result.status_code == 200 and result.json()["reason"] == "accepted_or_started", result.text
    assert invoke(fixture.hosted, fixture.identity, NativeRead(request_id="original-rid")).json() == original
    assert not source_calls and not fixture.files.stopped and not fixture.runtime.executor.calls


@pytest.mark.parametrize("file_api", [{"lane": "foundry-native-v1", "source_version": "37", "target_version": "47"}], indirect=True)
@pytest.mark.parametrize("legacy", [False, True])
def test_saved_source_refusal_survives_lost_agent_cas_and_retarget_to_48(file_api, monkeypatch, legacy):
    from shared_context import native_environment
    from shared_context.behavior_native_sessions import native_session_id
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
    from shared_context.errors import Problem
    from shared_context.native_turns import NativeTurnRuntime
    from shared_context.native_workspace_files import NativeWorkspaceFiles
    from test_native_turns import OfflineSdk

    fixture = file_api
    if legacy:
        legacy_source(fixture)
    original_http = fixture.case.foundry.http._transport
    save_agent, source_calls = native_environment._save_agent, []
    target = fixture.destination.model_copy(update={
        "agent_version": "48", "session_id": native_session_id(
            fixture.source, fixture.case.composition.config.identity.managed_identity_client_id, version="48")})
    identity = fixture.identity.model_copy(update={"session_id": target.session_id, "agent_version": "48"})
    runtime = NativeTurnRuntime(
        identity, OfflineSdk(), workspace_files=NativeWorkspaceFiles(fixture.target_workspace.home.parent / "v48"),
        task_observer=fixture.runtime.task_observer, bootstrap_source_inactive=fixture.runtime.bootstrap_source_inactive)

    async def lose_agent(runtime_, current, etag, replacement):
        if runtime_ is fixture.runtime and replacement.environment.source_invocation_refused:
            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "OFFLINE second CAS not committed")
        return await save_agent(runtime_, current, etag, replacement)

    def forward(packet):
        sid = packet.url.params["agent_session_id"]
        if sid == fixture.source.session_id:
            source_calls.append(json.loads(packet.content))
            return httpx.Response(409, json={"error": {"code": "NATIVE_SESSION_CHANGED"}})
        if sid != identity.session_id:
            return original_http.handle_request(packet)
        try:
            value = fixture.hosted.portal.call(runtime.dispatch, NATIVE_REQUEST.validate_json(packet.content))
        except Problem as error:
            return httpx.Response(error.status, json=error.body)
        return httpx.Response(200, content=value.model_dump_json().encode(), headers={
            "content-type": "application/json", "x-agent-session-id": sid, "x-agent-invocation-id": "v48-reader"})

    monkeypatch.setattr(native_environment, "_save_agent", lose_agent)
    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(forward))
    assert follow(fixture).status_code == (409 if legacy else 503)
    state = invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()
    assert state["following"].get("source_invocation_refused", False) is not legacy
    assert not state["environment"].get("source_invocation_refused", False)
    fixture.files.targets[target.session_id] = target
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_a, **_k: "48")
    body = {"request_id": "no-files-after-agent-cas-loss", "follow": fixture.command,
            "expected_agent_version": "48", "confirmation": "do-not-transfer-workspace-files"}
    preview = omit(fixture, preview=True, body=body)
    assert preview.status_code == 200, preview.text
    if legacy:
        ready = omit(fixture, body=body)
        assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
        assert ready.json()["files_migrated"] is False and ready.json()["hosted_agent_version"] == "48"
        assert ready.json()["previous_agent_version"] == "37"
        assert source_calls == []
        return
    changed = fixture.case.client.post("/work-policy/v1/native-turns/environment/retarget",
        headers=fixture.case.headers, json={
            "request_id": "retarget-after-source-refusal", "expected_session_id": fixture.source.session_id,
            "expected_agent_version": "48", "pending_request_id": fixture.command["request_id"],
            "pending_session_id": fixture.destination.session_id})
    assert changed.status_code == 200 and changed.json()["source_invocation_refused"] is True, changed.text
    result = follow(fixture, command={"request_id": "retarget-after-source-refusal", "expected_agent_version": "48"})
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_FILES_SOURCE_UNAVAILABLE"
    assert len(source_calls) == 1
