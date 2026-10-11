"""Signed API -> real native runtime/follow/store -> named offline Session Files transport."""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from azure.ai.projects.models import AgentSessionResource, SessionDirectoryEntry
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceResponseError
from azure.core.paging import ItemPaged
from starlette.testclient import TestClient

from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.behavior_native_sessions import NativeSessions, native_session_id
from shared_context.behavior_native_turn_wire import NativeRead, NativeSessionRead
from shared_context.behavior_wire import HostedIdentity
from shared_context.native_files_wire import WORKSPACE_ROOT
from shared_context.native_turns import NativeTurnRuntime
from shared_context.native_workspace_files import NativeWorkspaceFiles
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token
from test_native_environment import invoke
from test_native_responses import response_submit
from test_native_responses_environment import responses_host
from test_native_session_prepare_api import project_client
from test_native_turn_api import PREFIX, native_api
from test_native_turn_wire import submit
from test_native_turns import Harness, OfflineSdk, local_env


class OfflineSessionFiles:
    def __init__(self, targets, homes, source_sid):
        self.targets, self.homes, self.source_sid = targets, homes, source_sid
        self.calls, self.stopped = [], set()
        self.upload_error = None
        self.target_read_error = None
        self.on_stop = None
        self.on_upload = None
        self.list_seconds = 0
        self.elapsed = 0

    def get_session(self, name, sid):
        target = self.targets.get(sid)
        if target is None:
            raise ResourceNotFoundError("OFFLINE no session")
        assert name == target.agent_name
        return AgentSessionResource({
            "agent_session_id": sid, "status": "idle" if sid in self.stopped else "active",
            "version_indicator": {"type": "version_ref", "agent_version": target.agent_version}})

    def stop_session(self, name, sid):
        assert sid == self.source_sid and name == self.targets[sid].agent_name
        self.calls.append(("stop", sid))
        self.stopped.add(sid)
        if self.on_stop:
            self.on_stop()

    def list_session_files(self, name, sid, *, path, limit):
        assert name == self.targets[sid].agent_name and limit == 100
        self.calls.append(("list", sid, path))
        folder = self.homes[sid] / path
        if not folder.is_dir():
            raise ResourceNotFoundError("OFFLINE original folder missing")
        entries = [SessionDirectoryEntry({
            "name": child.name, "size": 0 if child.is_dir() else child.stat().st_size,
            "is_directory": child.is_dir(), "modified_time": int(child.stat().st_mtime),
        }) for child in sorted(folder.iterdir())]

        def page(cursor=None):
            self.elapsed += self.list_seconds
            self.calls.append(("page", sid, path, cursor))
            index = int(cursor or "0")
            end = min(index + limit, len(entries))
            return (str(end) if end < len(entries) else None, entries[index:end])

        return ItemPaged(page, lambda pair: (pair[0], iter(pair[1])))

    def download_session_file(self, name, sid, *, path):
        assert name == self.targets[sid].agent_name
        self.calls.append(("get", sid, path))
        if sid != self.source_sid and self.target_read_error is not None:
            error = HttpResponseError("OFFLINE target read refused")
            error.status_code = self.target_read_error
            raise error
        target = self.homes[sid] / path
        if not target.is_file():
            raise ResourceNotFoundError("OFFLINE original file absent")
        return iter((target.read_bytes(),))

    def upload_session_file(self, name, sid, content, *, path):
        assert name == self.targets[sid].agent_name and sid != self.source_sid
        assert isinstance(content, bytes) and "/.fa-transfers/" in path and "/upload/" in path
        self.calls.append(("put", sid, path, content))
        if self.on_upload:
            self.on_upload()
        (self.homes[sid] / path).write_bytes(content)
        if self.upload_error:
            raise ServiceResponseError("OFFLINE PUT acknowledgment lost")


@pytest.fixture
def file_loops():
    loops = [asyncio.new_event_loop() for _ in range(3)]
    yield loops
    for loop in loops:
        if not loop.is_closed():
            loop.close()


@pytest.fixture
def file_api(local_env, file_loops, native_api, monkeypatch, tmp_path, request):
    case = native_api
    setting = getattr(request, "param", "foundry-native-v1")
    options = setting if isinstance(setting, dict) else {}
    lane = setting["lane"] if isinstance(setting, dict) else setting
    recover = isinstance(setting, dict) and setting.get("recover") is True
    api_capture = isinstance(setting, dict) and not recover
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    source = case.remote.target
    source_version = options.get("source_version", source.agent_version)
    target_version = options.get("target_version", "42")
    if source_version != source.agent_version:
        source = source.model_copy(update={"agent_version": source_version, "session_id": native_session_id(
            source, case.composition.config.identity.managed_identity_client_id, version=source_version)})
    identity = HostedIdentity(
        agent_id=source.agent_id, agent_name=source.agent_name, agent_version=source.agent_version,
        session_id=source.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", identity.agent_name)
    workspace = NativeWorkspaceFiles(tmp_path / "source")
    workspace.root.mkdir(parents=True)
    (workspace.root / "nested").mkdir()
    (workspace.root / "empty").mkdir()
    (workspace.root / "nested" / "draft.txt").write_bytes(b"uncommitted original\n")
    (workspace.root / "binary.bin").write_bytes(b"\x00\xff\x80")
    old = NativeTurnRuntime(identity, OfflineSdk(), workspace_files=workspace)
    source_sdk = old.executor
    interrupted = continuation = None

    async def not_ready(_view):
        from shared_context.errors import Problem
        raise Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE destination is not ready yet")

    if recover:
        from shared_context.native_effects import NativeEffects

        async def tracked(receipt, scope, previous, control):
            await NativeEffects(old, receipt, control).start_operations()
            return await source_sdk(receipt, scope, previous, control)

        old.executor, old.on_continuation = tracked, not_ready
    old_app = responses_host(identity, old, tmp_path / "official-journal")
    with TestClient(old_app, backend_options={"loop_factory": lambda: file_loops[2]}) as original:
        state = invoke(original, identity, NativeSessionRead()).json()
        prepared = invoke(original, identity, {
            "operation": "session_prepare", "request_id": "prepare-original",
            "expected_session_id": identity.session_id, "expected_etag": state["etag"],
        })
        assert prepared.status_code == 200, prepared.text
        command = response_submit("original-rid") if lane == "foundry-responses-v1" else submit("original-rid")
        if api_capture:
            from shared_context.behavior_native_capture import resolve_capture
            captured = resolve_capture(case.c.control, case.c.authority, case.c.human, command.command)
            command = command.model_copy(update={"capture": captured})
        admitted = invoke(original, identity, command)
        assert admitted.status_code == 202, admitted.text
        if lane == "foundry-responses-v1":
            receipt = admitted.json()
            created = original.post("/responses", json={
                "input": receipt["command"]["content"], "background": True, "store": True, "stream": False,
                "agent_session_id": identity.session_id,
            }, headers={
                "x-agent-session-id": identity.session_id, "x-agent-foundry-call-id": "OFFLINE-original",
                "x-client-fa-rid": receipt["request_id"], "x-client-fa-cid": receipt["conversation_id"],
                "x-client-fa-submit-digest": receipt["submit_digest"],
            })
            assert created.status_code == 200, created.text
        saved = Harness(old, source_sdk, original).wait("original-rid", "completed")
        if lane == "foundry-responses-v1":
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                terminal = original.get("/responses/" + created.json()["id"], headers={
                    "x-agent-session-id": identity.session_id})
                assert terminal.status_code == 200, terminal.text
                if terminal.json()["status"] == "completed":
                    break
                time.sleep(0.01)
            else:
                pytest.fail("the original stored background response did not finish before the idle transition")
        if recover:
            source_sdk.block = True
            command = (response_submit("interrupted-root", saved.conversation_id)
                       if lane == "foundry-responses-v1" else submit("interrupted-root", saved.conversation_id))
            admitted = invoke(original, identity, command)
            assert admitted.status_code == 202, admitted.text
            if lane == "foundry-responses-v1":
                receipt = admitted.json()
                response = original.post("/responses", json={
                    "input": receipt["command"]["content"], "background": True, "store": True, "stream": False,
                    "agent_session_id": identity.session_id,
                }, headers={
                    "x-agent-session-id": identity.session_id, "x-client-fa-rid": receipt["request_id"],
                    "x-client-fa-cid": receipt["conversation_id"], "x-client-fa-submit-digest": receipt["submit_digest"],
                })
                assert response.status_code == 200, response.text
            interrupted = Harness(old, source_sdk, original).wait("interrupted-root", "started")
    if recover:
        from shared_context.behavior_native_turn_wire import NativeContinuationRead
        from shared_context.native_continuations import pending_handoffs

        recovered = NativeTurnRuntime(identity, OfflineSdk(), workspace_files=workspace)
        recovered.on_continuation = not_ready
        old_app = responses_host(identity, recovered, tmp_path / "official-journal")
        with TestClient(old_app, backend_options={"loop_factory": lambda: file_loops[0]}) as original:
            harness = Harness(recovered, recovered.executor, original)
            interrupted = harness.wait(interrupted.request_id, "unknown")
            continuation = invoke(original, identity, NativeContinuationRead(request_id=interrupted.request_id)).json()

            async def retained():
                for _ in range(100):
                    if interrupted.request_id in await pending_handoffs(recovered):
                        return
                    await asyncio.sleep(0.01)
                pytest.fail("The official recovery did not retain its pending handoff")

            harness.call(retained)
            assert recovered.executor.calls == []
    destination = source.model_copy(update={
        "session_id": native_session_id(source, case.composition.config.identity.managed_identity_client_id, version=target_version),
        "agent_version": target_version})
    new_identity = identity.model_copy(update={"session_id": destination.session_id, "agent_version": target_version})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", new_identity.session_id)
    target_workspace = NativeWorkspaceFiles(tmp_path / "target")
    targets = {source.session_id: source}
    files = OfflineSessionFiles(targets, {
        source.session_id: workspace.home, destination.session_id: target_workspace.home}, source.session_id)

    async def inactive(sid):
        assert sid == source.session_id
        return sid in files.stopped

    async def quiet(sid, *, task_id=None, include_responses=False):
        assert sid == source.session_id
        return True

    runtime = NativeTurnRuntime(
        new_identity, OfflineSdk(), workspace_files=target_workspace,
        bootstrap_source_inactive=inactive, task_observer=SimpleNamespace(quiet=quiet))
    new_app = responses_host(new_identity, runtime, tmp_path / "official-journal")

    def create(client, *, before_send):
        before_send()
        assert client.credential is case.foundry.credential
        assert client.target == destination
        files.calls.append(("create", client.target.session_id))
        targets[destination.session_id] = destination

    monkeypatch.setattr(FoundryHttpClient, "create_session", create)
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_a, **_k: target_version)
    monkeypatch.setattr("shared_context.behavior_native_responses_api.current_hosted_version", lambda *_a, **_k: target_version)
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session",
                        lambda client: targets.get(client.target.session_id))
    monkeypatch.setattr("shared_context.behavior_native_sessions._require_native_version", lambda _: None)
    project_calls = project_client(
        monkeypatch, get_session=files.get_session, stop_session=files.stop_session,
        list_sessions=lambda name, **_: iter(files.get_session(name, sid) for sid in targets),
        list_session_files=files.list_session_files,
        download_session_file=files.download_session_file, upload_session_file=files.upload_session_file)
    controls, control_versions, response_posts, conversations, lost = [], [], [], [], {}
    with TestClient(new_app, backend_options={"loop_factory": lambda: file_loops[1]}) as hosted:
        def forward(packet):
            is_response = packet.url.path.endswith("/responses") and "/protocols/openai/" in packet.url.path
            body = json.loads(packet.content)
            if packet.method == "POST" and packet.url.path.endswith("/protocols/openai/conversations"):
                cid = "conv-new-user-" + str(len(conversations) + 1)
                conversations.append(cid)
                return httpx.Response(200, json={"object": "conversation", "id": cid, "metadata": body["metadata"]})
            sid = body["agent_session_id"] if is_response else packet.url.params["agent_session_id"]
            control_versions.append((sid, packet.headers.get("x-client-fa-expected-version"), body.get("operation")))
            app = old_app if sid == source.session_id else new_app

            async def relay():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as client:
                    return await client.post("/responses" if is_response else "/invocations", content=packet.content, headers={
                        **{name: value for name, value in packet.headers.items() if name.startswith("x-client-")},
                        "x-client-fa-state-profile": "foundry-native-v1", "x-agent-session-id": sid,
                        "x-agent-invocation-id": "offline-files", "x-agent-foundry-call-id": "offline-control",
                    })

            result = hosted.portal.call(relay)
            if is_response:
                response_posts.append(body)
                return httpx.Response(result.status_code, content=result.content, headers=result.headers)
            if result.status_code >= 400:
                print(json.dumps({"control": json.loads(packet.content), "previous": controls[-1:],
                                  "error": result.json()}, ensure_ascii=False))
            controls.append((json.loads(packet.content), result.json()))
            command = json.loads(packet.content)
            if lost and command.get("operation") == lost.get("operation") and command.get("stage") == lost.get("stage"):
                lost.clear()
                raise httpx.ReadTimeout("OFFLINE original control ACK lost", request=packet)
            return httpx.Response(result.status_code, content=result.content, headers={
                "content-type": "application/json", "x-agent-session-id": sid,
                "x-agent-invocation-id": "offline-files",
            })

        monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(forward))
        case.composition._native_sessions = NativeSessions(case.composition)
        yield SimpleNamespace(
            case=case, source=source, destination=destination, original=old, runtime=runtime,
            workspace=workspace, target_workspace=target_workspace, files=files, saved=saved,
            interrupted=interrupted, continuation=continuation, source_sdk=source_sdk,
            hosted=hosted, identity=new_identity, project_calls=project_calls, controls=controls, lost=lost,
            response_posts=response_posts, control_versions=control_versions,
            command={"request_id": "follow-files-original", "expected_agent_version": target_version, "idle_only": True})


@pytest.mark.parametrize("file_api", [
    {"lane": "foundry-native-v1", "recover": True},
    {"lane": "foundry-responses-v1", "recover": True},
], indirect=True)
def test_official_recovery_waits_for_real_file_publication_then_continues_once_in_the_new_sid(file_api):
    from shared_context.behavior_native_turn_wire import NativeContinuationSubmit, NativeContinuationRead

    fixture = file_api
    runtime = fixture.runtime
    delivered = []

    async def dispatch(view):
        assert (fixture.target_workspace.root / "nested" / "draft.txt").read_bytes() == b"uncommitted original\n"
        assert (fixture.target_workspace.root / "binary.bin").read_bytes() == b"\x00\xff\x80"
        source = await runtime.read(view.original_request_id)
        delivered.append(view.request_id)
        result = await runtime.continue_turn(NativeContinuationSubmit(
            command=source.command.model_copy(update={
                "request_id": view.request_id, "conversation_id": source.conversation_id}),
            capture=source.capture, source=source.source, state_profile=source.state_profile,
            continuation=view.continuation))
        if result.status == "accepted" and result.dispatch is not None:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fixture.hosted.app), base_url="http://offline",
            ) as client:
                response = await client.post("/responses", json={
                    "input": result.command.content, "background": True, "store": True, "stream": False,
                    "agent_session_id": fixture.identity.session_id,
                }, headers={
                    "x-agent-session-id": fixture.identity.session_id, "x-client-fa-rid": result.request_id,
                    "x-client-fa-cid": result.conversation_id, "x-client-fa-submit-digest": result.submit_digest,
                })
                assert response.status_code == 200
            return await runtime.read(result.request_id)
        return result

    runtime.on_continuation = dispatch
    fixture.command["idle_only"] = False
    assert not fixture.target_workspace.root.exists() and runtime.executor.calls == []
    ready = follow(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["files_manifest_sha256"]
    current = Harness(runtime, runtime.executor, fixture.hosted)
    child = current.wait(fixture.continuation["request_id"], "completed")
    assert child.hosted_session_id == fixture.identity.session_id
    assert child.conversation_id == fixture.interrupted.conversation_id and child.continuation.attempt == 1
    assert child.continuation.continuation_of == fixture.interrupted.request_id
    assert runtime.executor.calls[0][2].sha256 == fixture.source_sdk.calls[-1][2].sha256
    assert current.call(runtime.read, fixture.interrupted.request_id) == fixture.interrupted
    view = invoke(fixture.hosted, fixture.identity, NativeContinuationRead(request_id=fixture.interrupted.request_id))
    assert view.json()["state"] == "admitted" and view.json()["request_id"] == child.request_id
    before = list(fixture.files.calls)
    repeated = follow(fixture)
    assert repeated.json() == ready.json() and fixture.files.calls == before
    assert delivered == [child.request_id] and len(runtime.executor.calls) == 1
    assert len([call for call in fixture.files.calls if call[0] == "put"]) == 2
    assert len([call for call in fixture.files.calls if call[0] == "stop"]) == 1


@pytest.mark.parametrize("file_api", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
def test_two_lanes_follow_publish_files_before_ready_without_replaying_original(file_api):
    fixture = file_api
    result = fixture.case.client.post(PREFIX + "/environment", json=fixture.command, headers=fixture.case.headers)
    assert result.status_code == 200, (result.text, fixture.controls[-2:])
    assert result.json()["state"] == "ready", result.text
    assert result.json()["files_manifest_sha256"]
    assert (fixture.target_workspace.root / "nested" / "draft.txt").read_bytes() == b"uncommitted original\n"
    assert (fixture.target_workspace.root / "binary.bin").read_bytes() == b"\x00\xff\x80"
    assert (fixture.target_workspace.root / "empty").is_dir()
    original = invoke(fixture.hosted, fixture.identity, NativeRead(request_id="original-rid"))
    assert original.json() == fixture.saved.model_dump(mode="json")
    assert len(fixture.original.executor.calls) == 1 and not fixture.runtime.executor.calls
    actions = [call[0] for call in fixture.files.calls]
    assert actions.count("put") == 2 and actions.count("stop") == 1
    assert actions.index("stop") > max(i for i, action in enumerate(actions) if action == "put")
    for _, credential, options in fixture.project_calls:
        assert credential is fixture.case.foundry.credential
        assert all(options[key] == 0 for key in ("retry_total", "retry_connect", "retry_read", "retry_status"))
    before = list(fixture.files.calls)
    fixture.case.composition._native_sessions = NativeSessions(fixture.case.composition)
    repeated = fixture.case.client.post(PREFIX + "/environment", json=fixture.command, headers=fixture.case.headers)
    assert repeated.json() == result.json()
    assert fixture.files.calls == before


def legacy_source(fixture):
    """A pre-feature source has no persisted helper declaration, even when served by a new test runtime."""
    from shared_context.native_turns import TURNS_STORE, _changed, _json
    fixture.original.workspace_files = None

    async def restore_old_shape():
        current, etag = await fixture.runtime._agent_record()
        prepared = _changed(current.preparation, files_profile=None)
        async with fixture.runtime._store(TURNS_STORE) as store:
            await store.set_item("agent", _json(_changed(current, preparation=prepared)), if_match=etag)
    fixture.hosted.portal.call(restore_old_shape)


def follow(fixture, *, reconcile=False, command=None):
    return fixture.case.client.post(
        PREFIX + ("/environment/reconcile" if reconcile else "/environment"),
        json=fixture.command if command is None else command, headers=fixture.case.headers)


@pytest.mark.parametrize("status", [403, 404, 503])
def test_unknown_put_only_reads_same_target_path_after_restart(file_api, status):
    fixture = file_api
    fixture.files.upload_error = True
    fixture.files.target_read_error = status
    response = follow(fixture)
    assert response.status_code in {403, 503}, response.text
    original_put = [call for call in fixture.files.calls if call[0] == "put"]
    assert len(original_put) == 1
    assert not fixture.files.stopped and not fixture.target_workspace.root.exists()
    fixture.case.composition._native_sessions = NativeSessions(fixture.case.composition)
    assert follow(fixture).status_code in {403, 503}
    assert [call for call in fixture.files.calls if call[0] == "put"] == original_put
    fixture.files.target_read_error = None
    observed = follow(fixture, reconcile=True)
    assert observed.status_code == 200 and observed.json()["state"] == "pending", observed.text
    assert [call for call in fixture.files.calls if call[0] == "put"] == original_put
    assert not fixture.files.stopped
    fixture.files.upload_error = None
    completed = follow(fixture)
    assert completed.status_code == 200 and completed.json()["state"] == "ready", completed.text
    puts = [call for call in fixture.files.calls if call[0] == "put"]
    assert len(puts) == 2 and puts[0] == original_put[0]


def test_lost_put_ack_with_matching_get_completes_without_second_put(file_api):
    fixture = file_api
    fixture.files.upload_error = True
    completed = follow(fixture)
    assert completed.status_code == 200 and completed.json()["state"] == "ready", completed.text
    assert len([call for call in fixture.files.calls if call[0] == "put"]) == 2


@pytest.mark.parametrize("when", ["before-stop", "after-stop"])
def test_source_change_prevents_ready_and_keeps_original_manifest(file_api, when):
    fixture = file_api
    change = lambda: (fixture.workspace.root / "nested" / "draft.txt").write_bytes(b"changed original")
    if when == "before-stop":
        fixture.files.on_upload = change
    else:
        fixture.files.on_stop = change
    result = follow(fixture)
    assert result.status_code == 409, result.text
    assert not fixture.target_workspace.root.exists() and not fixture.runtime.executor.calls
    if when == "before-stop":
        assert not fixture.files.stopped
    fixture.files.on_upload = fixture.files.on_stop = None
    fixture.case.composition._native_sessions = NativeSessions(fixture.case.composition)
    assert follow(fixture).status_code == 409
    assert not fixture.target_workspace.root.exists()


@pytest.mark.parametrize("file_api", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
@pytest.mark.parametrize("idle_only", [False, True])
def test_unsupported_source_does_not_bootstrap_files_or_stop_original(file_api, idle_only):
    fixture = file_api
    if not idle_only:
        fixture.command.pop("idle_only")
    legacy_source(fixture)
    result = follow(fixture)
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_FILES_UNSUPPORTED", result.text
    assert not any(call[0] in {"put", "get", "list", "stop"} for call in fixture.files.calls)
    assert not fixture.target_workspace.root.exists() and not fixture.runtime.executor.calls
    original = invoke(fixture.hosted, fixture.identity, NativeRead(request_id="original-rid"))
    assert original.json() == fixture.saved.model_dump(mode="json")
    state = invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()
    assert state["hosted_session_id"] == fixture.source.session_id
    assert (fixture.workspace.root / "nested" / "draft.txt").read_bytes() == b"uncommitted original\n"
    lane = fixture.saved.state_profile
    path = "/work-policy/v1/" + ("responses-turns" if lane == "foundry-responses-v1" else "native-turns") + "/submit"
    fresh = fixture.case.client.post(path, headers=fixture.case.headers, json={
        "request_id": "new-conversation-after-unsupported", "content": "Hi"})
    assert fresh.status_code == 409, fresh.text
    assert fresh.json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert fresh.json()["error"]["not_dispatched"] is True
    assert not fixture.runtime.executor.calls


def test_destination_different_content_is_never_overwritten(file_api):
    fixture = file_api
    fixture.target_workspace.root.mkdir(parents=True)
    path = fixture.target_workspace.root / "existing"
    path.write_bytes(b"keep destination edit")
    result = follow(fixture)
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_FILES_CONFLICT", result.text
    assert path.read_bytes() == b"keep destination edit"
    assert not fixture.files.stopped


def test_bounded_file_steps_keep_original_rid_manifest_and_staging(file_api, monkeypatch):
    fixture = file_api
    monkeypatch.setattr("shared_context.behavior_session_files.MAX_FILE_STEPS", 1)
    digests, states = set(), []
    for _ in range(10):
        result = follow(fixture)
        assert result.status_code == 200, result.text
        value = result.json()
        assert value["request_id"] == fixture.command["request_id"]
        if value.get("files_manifest_sha256"):
            digests.add(value["files_manifest_sha256"])
        states.append(value["state"])
        if value["state"] == "ready":
            break
        assert value["reason"] == "files_pending"
        fixture.case.composition._native_sessions = NativeSessions(fixture.case.composition)
    assert states[-1] == "ready" and len(digests) == 1
    puts = [call for call in fixture.files.calls if call[0] == "put"]
    assert len(puts) == 2 and len({call[2] for call in puts}) == 2


def test_slow_source_directory_pages_resume_after_restart_without_rescanning_prefix(file_api, monkeypatch):
    fixture = file_api
    for index in range(100):
        (fixture.workspace.root / f"dir-{index:03}").mkdir()
    fixture.files.list_seconds = 0.11
    monkeypatch.setattr("shared_context.behavior_session_files.monotonic", lambda: fixture.files.elapsed)
    outcomes = []
    for _ in range(30):
        result = follow(fixture)
        assert result.status_code == 200, result.text
        outcomes.append(result.json())
        if result.json()["state"] == "ready":
            break
        assert result.json()["reason"] == "files_pending"
        fixture.case.composition._native_sessions = NativeSessions(fixture.case.composition)
    assert outcomes[-1]["state"] == "ready"
    pages = [call for call in fixture.files.calls if call[0] == "page"]
    for index in range(100):
        path = WORKSPACE_ROOT + f"/dir-{index:03}"
        assert len([call for call in pages if call[2] == path]) == 2
    assert len([call for call in fixture.files.calls if call[0] == "put"]) == 2


def test_late_upload_after_ready_only_changes_operation_staging(file_api):
    fixture = file_api
    result = follow(fixture)
    assert result.status_code == 200 and result.json()["state"] == "ready", result.text
    for call in fixture.files.calls:
        if call[0] == "put":
            (fixture.target_workspace.home / call[2]).write_bytes(b"late upload")
    assert (fixture.target_workspace.root / "binary.bin").read_bytes() == b"\x00\xff\x80"
    assert (fixture.target_workspace.root / "nested" / "draft.txt").read_bytes() == b"uncommitted original\n"


def test_selected_version_change_cannot_reuse_the_original_transfer(file_api, monkeypatch):
    fixture = file_api
    fixture.files.upload_error, fixture.files.target_read_error = True, 404
    assert follow(fixture).status_code == 503
    puts = [call for call in fixture.files.calls if call[0] == "put"]
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "43")
    result = follow(fixture)
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_ENVIRONMENT_CONFLICT"
    assert [call for call in fixture.files.calls if call[0] == "put"] == puts and not fixture.files.stopped


@pytest.mark.parametrize("target_drift", [False, True])
def test_new_selector_reads_the_original_files_target_without_creating_a_new_reader(file_api, monkeypatch, target_drift):
    fixture = file_api
    fixture.files.upload_error, fixture.files.target_read_error = True, 404
    assert follow(fixture).status_code == 503
    puts = [call for call in fixture.files.calls if call[0] == "put"]
    fixture.files.target_read_error = None
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "43")
    if target_drift:
        fixture.files.targets[fixture.destination.session_id] = fixture.destination.model_copy(
            update={"agent_version": "43"})
        fixture.runtime.identity = fixture.identity.model_copy(update={"agent_version": "43"})
    fixture.case.composition._native_sessions = NativeSessions(fixture.case.composition)
    controls_before, files_before = len(fixture.controls), len(fixture.files.calls)
    result = follow(fixture, reconcile=True)
    assert result.status_code == 200 and result.json()["state"] == "pending", result.text
    assert result.json()["hosted_session_id"] == fixture.destination.session_id
    assert result.json()["hosted_agent_version"] == "42"
    assert [call for call in fixture.files.calls if call[0] == "put"] == puts
    new_calls = fixture.files.calls[files_before:]
    assert new_calls and all(call[0] == "get" and call[1:3] == puts[0][1:3] for call in new_calls)
    assert not any(command.get("operation") == "session_follow" for command, _ in fixture.controls[controls_before:])
    assert len(fixture.files.targets) == 2 and not fixture.runtime.executor.calls


def test_unknown_file_upload_cannot_be_bypassed_by_a_retarget_id(file_api):
    from shared_context.behavior_native_turn_wire import NativeSessionRetarget
    from shared_context.errors import Problem
    from shared_context.native_environment import retarget_session

    fixture = file_api
    fixture.files.upload_error, fixture.files.target_read_error = True, 404
    assert follow(fixture).status_code == 503
    state = invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()
    newer = NativeTurnRuntime(
        fixture.identity.model_copy(update={"session_id": "different-destination", "agent_version": "43"}), OfflineSdk(),
        task_observer=fixture.runtime.task_observer)
    with pytest.raises(Problem) as error:
        fixture.hosted.portal.call(retarget_session, newer, NativeSessionRetarget(
            request_id="new-retarget-must-not-bypass", expected_session_id=fixture.source.session_id,
            expected_etag=state["etag"], expected_agent_version="43", pending_request_id=fixture.command["request_id"],
            pending_session_id=fixture.destination.session_id))
    assert error.value.body["error"]["code"] == "NATIVE_FILES_UNKNOWN"
    unchanged = invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()
    assert unchanged["environment"] == state["environment"]
    assert len([call for call in fixture.files.calls if call[0] == "put"]) == 1


def test_confirmed_partial_files_can_retarget_but_fence_the_old_next_put_before_pointer_cas(file_api, monkeypatch):
    from shared_context import native_environment
    from shared_context.behavior_native_turn_wire import NativeSessionRetarget
    from shared_context.errors import Problem
    from shared_context.native_files_wire import NativeSessionFiles

    fixture = file_api
    fixture.files.upload_error, fixture.files.target_read_error = True, 404
    assert follow(fixture).status_code == 503
    fixture.files.target_read_error = None
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "43")
    assert follow(fixture, reconcile=True).status_code == 200
    state = invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()
    newer = NativeTurnRuntime(
        fixture.identity.model_copy(update={"session_id": "explicit-next-target", "agent_version": "43"}),
        OfflineSdk(), workspace_files=NativeWorkspaceFiles(fixture.target_workspace.home.parent / "third"),
        task_observer=fixture.runtime.task_observer)
    late = NativeSessionFiles(
        agent_id=fixture.identity.agent_id, request_id=fixture.command["request_id"],
        source_session_id=fixture.source.session_id, source_agent_version="41",
        target_session_id=fixture.destination.session_id, target_agent_version="42",
        stage="begin", index=1, send_token="late-old-next-put")
    save_agent, blocked = native_environment._save_agent, []

    async def raced(runtime, original, etag, replacement):
        if runtime is newer:
            with pytest.raises(Problem) as error:
                await fixture.runtime.dispatch(late)
            assert error.value.body["error"]["code"] == "NATIVE_ENVIRONMENT_CONFLICT"
            blocked.append(True)
        return await save_agent(runtime, original, etag, replacement)

    monkeypatch.setattr(native_environment, "_save_agent", raced)
    changed = fixture.hosted.portal.call(native_environment.retarget_session, newer, NativeSessionRetarget(
        request_id="explicit-retarget-after-readback", expected_session_id=fixture.source.session_id,
        expected_etag=state["etag"], expected_agent_version="43", pending_request_id=fixture.command["request_id"],
        pending_session_id=fixture.destination.session_id))
    assert changed.state == "pending" and changed.hosted_session_id == "explicit-next-target"
    assert blocked == [True] and len([call for call in fixture.files.calls if call[0] == "put"]) == 1


def test_source_manifest_ack_loss_cannot_recompute_a_different_intent(file_api):
    fixture = file_api
    fixture.lost.update(operation="session_files", stage="inspect")
    assert follow(fixture).status_code == 503
    (fixture.workspace.root / "binary.bin").write_bytes(b"different source")
    fixture.case.composition._native_sessions = NativeSessions(fixture.case.composition)
    result = follow(fixture)
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_FILES_CONFLICT", result.text
    assert len([command for command, _ in fixture.controls if command.get("stage") == "inspect"]) == 1
    assert not any(call[0] == "put" for call in fixture.files.calls)


def test_lost_send_intent_ack_leaves_only_get_even_if_no_put_was_sent(file_api):
    fixture = file_api
    fixture.lost.update(operation="session_files", stage="begin")
    assert follow(fixture).status_code == 503
    fixture.case.composition._native_sessions = NativeSessions(fixture.case.composition)
    assert follow(fixture).status_code == 503
    assert not any(call[0] in {"put", "stop"} for call in fixture.files.calls)
    reads = [call for call in fixture.files.calls if call[0] == "get" and call[1] == fixture.destination.session_id]
    assert len(reads) == 1 and "/upload/" in reads[0][2]


def test_lost_ready_ack_reads_original_binding_without_copying_again(file_api):
    fixture = file_api
    fixture.lost.update(operation="session_follow", stage="commit")
    assert follow(fixture).status_code == 503
    before = list(fixture.files.calls)
    fixture.case.composition._native_sessions = NativeSessions(fixture.case.composition)
    result = follow(fixture, reconcile=True)
    assert result.status_code == 200 and result.json()["state"] == "ready", result.text
    assert fixture.files.calls == before
    assert not fixture.runtime.executor.calls


def test_upload_can_only_run_after_its_exact_intent_is_persisted(file_api):
    fixture = file_api
    observed = []

    def retained_intent():
        command, result = fixture.controls[-1]
        assert command["operation"] == "session_files" and command["stage"] == "begin"
        assert result["send_allowed"] is True
        assert result["progress"]["sending"] == command["send_token"]
        assert result["progress"]["next_index"] == command["index"]
        observed.append(command["send_token"])

    fixture.files.on_upload = retained_intent
    result = follow(fixture)
    assert result.status_code == 200 and result.json()["state"] == "ready", result.text
    assert len(set(observed)) == 2


@pytest.mark.parametrize("pending", ["admission", "task"])
def test_idle_follow_does_not_clear_original_admission_or_active_task(file_api, pending):
    from shared_context.native_turns import TURNS_STORE

    fixture = file_api
    if pending == "admission":
        async def reserve():
            async with fixture.runtime._store(TURNS_STORE) as store:
                item = await store.get_item("agent")
                value = dict(item.value)
                value["admitting"] = {"request_id": "original-rid", "submit_digest": fixture.saved.submit_digest}
                await store.set_item("agent", value, if_match=item.etag)
        fixture.hosted.portal.call(reserve)
    else:
        async def busy(*args, **kwargs):
            return False
        fixture.runtime.task_observer = SimpleNamespace(quiet=busy)
    result = follow(fixture)
    assert result.status_code == 200 and result.json()["state"] == "pending", result.text
    assert result.json()["reason"] == ("admission_pending" if pending == "admission" else "task_active")
    state = invoke(fixture.hosted, fixture.identity, NativeSessionRead()).json()
    if pending == "admission":
        assert state["admission_request_id"] == "original-rid"
    assert state["hosted_session_id"] == fixture.source.session_id
    assert not fixture.files.stopped and not any(call[0] == "put" for call in fixture.files.calls)
    assert invoke(fixture.hosted, fixture.identity, NativeRead(request_id="original-rid")).json() == fixture.saved.model_dump(mode="json")


@pytest.mark.parametrize("file_api", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
def test_idle_deployment_drift_follows_before_a_fresh_user_turn(file_api):
    fixture = file_api
    fixture.files.targets[fixture.source.session_id] = fixture.source.model_copy(update={"agent_version": "42"})
    fixture.original.identity = fixture.original.identity.model_copy(update={"agent_version": "42"})
    result = follow(fixture)
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_FILES_SOURCE_UNAVAILABLE"
    from test_native_files_omission import omit
    result = omit(fixture)
    assert result.status_code == 200 and result.json()["state"] == "ready", result.text
    assert result.json()["previous_agent_version"] == "41"
    assert result.json()["hosted_agent_version"] == "42"
    assert invoke(fixture.hosted, fixture.identity, NativeRead(request_id="original-rid")).json() == fixture.saved.model_dump(mode="json")
    lane = fixture.saved.state_profile
    command = (response_submit("fresh-user-rid", fixture.saved.conversation_id, "New user input")
               if lane == "foundry-responses-v1" else
               submit("fresh-user-rid", fixture.saved.conversation_id, "New user input"))
    accepted = invoke(fixture.hosted, fixture.identity, command)
    assert accepted.status_code == 202, accepted.text
    if lane == "foundry-responses-v1":
        receipt = accepted.json()
        created = fixture.hosted.post("/responses", json={
            "input": receipt["command"]["content"], "background": True, "store": True, "stream": False,
            "agent_session_id": fixture.identity.session_id,
        }, headers={
            "x-agent-session-id": fixture.identity.session_id, "x-agent-foundry-call-id": "OFFLINE-fresh",
            "x-client-fa-rid": receipt["request_id"], "x-client-fa-cid": receipt["conversation_id"],
            "x-client-fa-submit-digest": receipt["submit_digest"],
        })
        assert created.status_code == 200, created.text
    completed = Harness(fixture.runtime, fixture.runtime.executor, fixture.hosted).wait("fresh-user-rid", "completed")
    assert completed.conversation_id == fixture.saved.conversation_id and completed.turn_number == 2
    assert completed.hosted_session_id == fixture.destination.session_id
    assert [call[0] for call in fixture.runtime.executor.calls] == ["fresh-user-rid"]
    assert fixture.runtime.executor.calls[0][2] is not None
    assert result.json()["files_migrated"] is False
    assert not (fixture.target_workspace.root / "nested" / "draft.txt").exists()


@pytest.mark.parametrize("file_api", [{"lane": "foundry-native-v1"}, {"lane": "foundry-responses-v1"}], indirect=True)
def test_published_version_and_files_are_ready_before_the_real_api_first_submit(file_api):
    fixture = file_api
    prepared = follow(fixture)
    assert prepared.status_code == 200 and prepared.json()["state"] == "ready", prepared.text
    lane = fixture.saved.state_profile
    path = "/work-policy/v1/" + ("responses-turns" if lane == "foundry-responses-v1" else "native-turns") + "/submit"
    response = fixture.case.client.post(path, headers=fixture.case.headers, json={
        "request_id": "first-api-submit", "content": "New input after the idle deployment",
        "conversation_id": fixture.saved.conversation_id})
    assert response.status_code == 202, response.text
    completed = Harness(fixture.runtime, fixture.runtime.executor, fixture.hosted).wait("first-api-submit", "completed")
    assert completed.conversation_id == fixture.saved.conversation_id
    assert completed.hosted_session_id == fixture.destination.session_id
    assert [call[0] for call in fixture.runtime.executor.calls] == ["first-api-submit"]
    assert len([command for command, _ in fixture.controls if command["operation"] == "submit"]) == 1
    assert len(fixture.response_posts) == (1 if lane == "foundry-responses-v1" else 0)
    assert (fixture.target_workspace.root / "nested" / "draft.txt").read_bytes() == b"uncommitted original\n"
    assert invoke(fixture.hosted, fixture.identity, NativeRead(request_id="original-rid")).json() == fixture.saved.model_dump(mode="json")
