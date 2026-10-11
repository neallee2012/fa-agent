"""Only cursors returned as page.last_id can address a subsequent receipt page."""

import asyncio
import json
from collections import Counter
from types import SimpleNamespace

import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.ai.agentserver.core.storage._errors import FoundryStorageBadRequestError
from azure.core.exceptions import ServiceResponseError

from shared_context.behavior_native_turn_wire import NativeSubmit, canonical_bytes
from shared_context.native_turns import TURNS_STORE, _changed, _json
from test_native_files_api import (
    file_api, file_loops, follow, instruction_models, jwk_client, local_env,
    native_api, signing_key, synthetic_standing_v1, token, legacy_source,
)
from test_native_files_omission import omit


@pytest.fixture
def boundary_history(file_api, monkeypatch):
    from shared_context import native_environment

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    sample = fixture.hosted.portal.call(fixture.runtime._rid, "original-rid").value
    items = []
    for index in range(1000):
        rid = f"boundary-{index:04d}"
        submitted = NativeSubmit(
            command=fixture.saved.command.model_copy(update={
                "request_id": rid, "conversation_id": fixture.saved.conversation_id}),
            capture=fixture.saved.capture)
        value = _changed(sample, receipt=_changed(
            sample.receipt, request_id=rid, command=submitted.command,
            submit_digest=submitted.sha256, status="unknown", result=None))
        items.append(SimpleNamespace(key="rid:" + rid, id=f"opaque-id+{index:04d}/=:%",
                                     etag=f"immutable-{index}", value=_json(value)))
    receipts = {item.key: item for item in items}
    original_get, original_list = FoundryStateStore.get_item, FoundryStateStore.list_keys
    case = SimpleNamespace(fixture=fixture, items=items, calls=[], reads=[], issued={None},
                           elapsed=0.0, tick=0.03, change=None, fail_cursor=False,
                           page_rows=[], scan_keys=set())

    async def page(store, *, limit=None, after=None, **kwargs):
        if store._name != TURNS_STORE:
            return await original_list(store, limit=limit, after=after, **kwargs)
        case.calls.append(after)
        assert after in case.issued, "Only a platform-returned page.last_id may be sent as after"
        if case.fail_cursor:
            raise FoundryStorageBadRequestError(
                "OFFLINE rejected page", status_code=400,
                response_body={"error": {"code": "bad_request", "message": "OFFLINE rejected page"}})
        start = 0 if after is None else next(index for index, item in enumerate(items) if item.id == after) + 1
        selected = list(items[start:start + limit])
        if case.change == "order":
            selected[0], selected[1] = selected[1], selected[0]
        elif case.change == "new":
            selected = [SimpleNamespace(key="rid:new-unread", id="new-unread-id", etag="new"),
                        *selected[:-1]]
        first_id, last_id = (selected[0].id, selected[-1].id) if selected else (None, None)
        if last_id is not None:
            case.issued.add(last_id)
        case.page_rows.append(tuple(item.id for item in selected))
        return SimpleNamespace(keys=selected, first_id=first_id, last_id=last_id,
                               has_more=start + len(selected) < len(items))

    async def item(store, key, **kwargs):
        if store._name == TURNS_STORE and key in receipts:
            case.reads.append(key)
            case.elapsed += case.tick
            return receipts[key]
        found = await original_get(store, key, **kwargs)
        if store._name == TURNS_STORE and key.startswith("files-check:") and found is not None:
            case.scan_keys.add(key)
        return found

    monkeypatch.setattr(FoundryStateStore, "list_keys", page)
    monkeypatch.setattr(FoundryStateStore, "get_item", item)
    monkeypatch.setattr(native_environment, "_SCAN_SECONDS", 1.0)
    monkeypatch.setattr(native_environment, "monotonic", lambda: case.elapsed)
    return case


def test_thousand_keys_with_mid_page_yields_use_only_page_boundaries(boundary_history):
    case = boundary_history
    before = [canonical_bytes(item.value) for item in case.items]
    for _ in range(40):
        prior_calls = len(case.calls)
        response = omit(case.fixture, preview=True)
        assert response.status_code == 200, response.text
        assert len(case.calls) == prior_calls + 1
        if response.json()["reason"] is None:
            break
    assert response.json()["reason"] is None
    assert 10 < len(case.calls) <= 40, "Several partial-page windows must finish all 1,000 keys"
    assert case.calls[:3] == [None, None, None]
    assert all(count == 1 for count in Counter(case.reads).values())
    assert len(set(case.reads)) == 1000
    case.tick = 0
    ready = omit(case.fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["files_migrated"] is False
    assert before == [canonical_bytes(item.value) for item in case.items]
    assert not case.fixture.runtime.executor.calls


@pytest.mark.parametrize("change", ["order", "new"])
def test_changed_refetched_page_is_not_skipped_or_used_for_confirmation(boundary_history, change):
    case = boundary_history
    first = omit(case.fixture, preview=True)
    assert first.status_code == 200 and first.json()["reason"] == "scan_pending"
    read_count = len(case.reads)
    case.change = change
    second = omit(case.fixture, preview=True)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "NATIVE_ENVIRONMENT_CONFLICT"
    assert case.calls == [None, None]
    assert len(case.reads) == read_count, "No offset may be applied to a changed page"
    assert not case.fixture.files.stopped and not case.fixture.runtime.executor.calls


def test_idle_follow_restarts_a_changed_receipt_page_without_skipping_work(boundary_history, monkeypatch):
    from shared_context import native_environment

    case = boundary_history
    del case.items[16:]
    case.tick = 0
    original_get = FoundryStateStore.get_item

    async def phase_record():
        async with case.fixture.runtime._store(TURNS_STORE) as store:
            key = native_environment._key(case.fixture.runtime)
            item = await store.get_item(key)
            await store.set_item(key, {**item.value, "phase": "draining"}, if_match=item.etag)

    async def slow_read(store, key, **kwargs):
        if store._name == TURNS_STORE and key.startswith("rid:boundary-"):
            await asyncio.sleep(0.02)
        return await original_get(store, key, **kwargs)

    case.fixture.hosted.portal.call(phase_record)
    with monkeypatch.context() as slow:
        slow.setattr(native_environment, "_SCAN_SECONDS", 0.07)
        slow.setattr(FoundryStateStore, "get_item", slow_read)
        partial = follow(case.fixture)
    assert partial.status_code == 200 and partial.json()["reason"] == "scan_pending", partial.text
    assert 0 < len(case.reads) < 16
    before = [canonical_bytes(item.value) for item in case.items]
    case.items[0], case.items[1] = case.items[1], case.items[0]
    read_count = len(case.reads)
    changed = follow(case.fixture)
    assert changed.status_code == 200 and changed.json()["reason"] == "scan_pending", changed.text
    assert changed.json()["request_id"] == partial.json()["request_id"]
    assert len(case.reads) == read_count, "A stale page offset must never be used after keys move"
    resumed = follow(case.fixture)
    assert resumed.status_code in {200, 409}, resumed.text
    assert case.calls[-1] is None
    assert len(case.reads) >= read_count + 16
    assert sorted(before) == sorted(canonical_bytes(item.value) for item in case.items)
    assert not case.fixture.runtime.executor.calls


def test_system_omission_rescans_changed_keys_before_confirming_no_files(boundary_history):
    from test_native_automatic_environment import advance

    case = boundary_history
    first = advance(case.fixture)
    assert first.status_code == 200 and first.json()["reason"] == "scan_pending", first.text
    read_count = len(case.reads)
    case.change = "order"
    changed = advance(case.fixture)
    assert changed.status_code == 200 and changed.json()["reason"] == "scan_pending", changed.text
    assert changed.json()["request_id"] == first.json()["request_id"]
    assert len(case.reads) == read_count and not case.fixture.files.stopped
    case.tick = 0
    for _ in range(15):
        result = advance(case.fixture)
        assert result.status_code == 200, result.text
        if result.json()["state"] == "ready":
            break
    assert result.json()["state"] == "ready" and result.json()["files_migrated"] is False
    assert len(set(case.reads)) == 1000
    assert not case.fixture.runtime.executor.calls


@pytest.mark.parametrize("legacy", [False, True], ids=["files-helper", "no-files"])
def test_busy_receipt_completion_unsticks_the_same_environment_without_rewriting_work(file_api, monkeypatch, legacy):
    from test_native_automatic_environment import advance

    fixture = file_api
    if legacy:
        legacy_source(fixture)
    original = fixture.hosted.portal.call(fixture.runtime._rid, fixture.saved.request_id).value
    ended = [False]
    list_keys = FoundryStateStore.list_keys

    async def changing_ids(store, **kwargs):
        page = await list_keys(store, **kwargs)
        if store._name != TURNS_STORE:
            return page
        keys = [SimpleNamespace(
            key=key.key, id=key.id + ("-updated" if ended[0] and key.key == "rid:" + fixture.saved.request_id else ""))
            for key in page.keys]
        keys.sort(key=lambda key: key.key == "rid:" + fixture.saved.request_id)
        return SimpleNamespace(keys=keys, first_id=page.first_id, last_id=page.last_id, has_more=page.has_more)

    async def write_receipt(value):
        current = await fixture.runtime._rid(fixture.saved.request_id)
        async with fixture.runtime._store(TURNS_STORE) as store:
            await store.set_item("rid:" + fixture.saved.request_id, _json(value), if_match=current.etag)

    fixture.hosted.portal.call(write_receipt, _changed(
        original, receipt=_changed(original.receipt, status="started", result=None)))
    monkeypatch.setattr(FoundryStateStore, "list_keys", changing_ids)
    first = advance(fixture)
    assert first.status_code == 200 and first.json()["reason"] == "accepted_or_started", first.text
    assert not fixture.files.stopped
    ended[0] = True
    fixture.hosted.portal.call(write_receipt, original)
    for _ in range(6):
        next_ = advance(fixture)
        assert next_.status_code == 200, next_.text
        assert next_.json()["request_id"] == first.json()["request_id"]
        if next_.json()["state"] == "ready":
            break
    assert next_.json()["state"] == "ready"
    assert canonical_bytes(fixture.hosted.portal.call(fixture.runtime._rid, fixture.saved.request_id).value) == canonical_bytes(original)
    assert not fixture.runtime.executor.calls


@pytest.mark.parametrize("source_state,reader_reentry", [
    ("idle", False), ("busy", False), ("unknown-upload", False), ("busy", True),
])
def test_a_newer_published_version_can_follow_an_older_pending_scan(file_api, monkeypatch, source_state, reader_reentry):
    import httpx
    from shared_context.behavior_native_sessions import native_session_id
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
    from shared_context.errors import Problem
    from shared_context.native_turns import NativeTurnRuntime
    from shared_context.native_workspace_files import NativeWorkspaceFiles
    from test_native_turns import OfflineSdk
    from test_native_automatic_environment import advance

    fixture = file_api
    original = fixture.hosted.portal.call(fixture.runtime._rid, fixture.saved.request_id).value

    async def write(value):
        current = await fixture.runtime._rid(fixture.saved.request_id)
        async with fixture.runtime._store(TURNS_STORE) as store:
            await store.set_item("rid:" + fixture.saved.request_id, _json(value), if_match=current.etag)

    fixture.hosted.portal.call(write, _changed(original, receipt=_changed(
        original.receipt, status="started", result=None)))
    pending = advance(fixture)
    assert pending.status_code == 200 and pending.json()["reason"] == "accepted_or_started", pending.text
    if source_state != "busy" and not reader_reentry:
        fixture.hosted.portal.call(write, original)
    if source_state == "unknown-upload":
        fixture.files.upload_error, fixture.files.target_read_error = True, 403
        assert advance(fixture).status_code == 403
    file_calls = list(fixture.files.calls)
    target = fixture.destination.model_copy(update={
        "agent_version": "43", "session_id": native_session_id(
            fixture.source, fixture.case.composition.config.identity.managed_identity_client_id, version="43")})
    fixture.files.targets[target.session_id] = target
    identity = fixture.identity.model_copy(update={"session_id": target.session_id, "agent_version": "43"})
    workspace = NativeWorkspaceFiles(fixture.target_workspace.home.parent / "new-target")
    fixture.files.homes[target.session_id] = workspace.home
    runtime = NativeTurnRuntime(identity, OfflineSdk(), workspace_files=workspace,
                               task_observer=fixture.runtime.task_observer,
                               bootstrap_source_inactive=fixture.runtime.bootstrap_source_inactive)
    transport = fixture.case.foundry.http._transport
    controls = []

    def newer(packet):
        if packet.url.params["agent_session_id"] != identity.session_id:
            return transport.handle_request(packet)
        command = NATIVE_REQUEST.validate_json(packet.content)
        try:
            value = fixture.hosted.portal.call(runtime.dispatch, command)
        except Problem as error:
            controls.append((command.operation, error.body["error"]))
            return httpx.Response(error.status, json=error.body)
        controls.append((command.operation, getattr(value, "reason", None)))
        return httpx.Response(200, content=value.model_dump_json().encode(), headers={
            "content-type": "application/json", "x-agent-session-id": identity.session_id,
            "x-agent-invocation-id": "newer-after-busy"})

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(newer))
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "43")
    fixture.command = {**fixture.command, "request_id": "newer-published-follow", "expected_agent_version": "43"}
    if reader_reentry and source_state != "unknown-upload":
        from test_native_automatic_environment import observe
        from shared_context.native_environment_sync import automatic_environment_command, follow_agent
        fixture.command = automatic_environment_command(target, "43").model_dump(mode="json")
        waiting = advance(fixture)
        assert waiting.status_code == 200 and waiting.json()["reason"] == "accepted_or_started", waiting.text
        before = observe(fixture)
        assert before.status_code == 200 and before.json()["progress_token"], before.text
        stable = observe(fixture)
        assert stable.json()["progress_token"] == before.json()["progress_token"]
        fixture.hosted.portal.call(write, original)
        after = observe(fixture)
        assert after.status_code == 200, after.text
        assert after.json()["request_id"] == fixture.command["request_id"]
        assert after.json()["progress_token"] != before.json()["progress_token"]
        fixture.hosted.portal.call(write, _changed(original, receipt=_changed(
            original.receipt, status="started", result=None)))
        clock = [0]

        def release_after_batch(seconds):
            clock[0] += seconds
            if clock[0] == 24:
                fixture.hosted.portal.call(write, original)

        # Cross the ten-advance boundary, then wake only when the original receipt actually changes.
        driven = follow_agent(fixture.case.composition, fixture.source.agent_id, wait_seconds=60,
                              sleep=release_after_batch, clock=lambda: clock[0])
        assert driven["state"] == "ok", driven
        assert 24 <= clock[0] < 60
        source_state = "idle"
    for _ in range(6):
        result = advance(fixture)
        if source_state == "unknown-upload":
            assert result.status_code >= 400 and result.json()["error"]["code"] in {
                "NATIVE_FILES_UNKNOWN", "NATIVE_FILES_ACCESS_DENIED"}, (result.text, controls)
            assert fixture.files.calls == file_calls and not fixture.files.stopped
            return
        assert result.status_code == 200, (result.text, controls)
        if source_state == "busy":
            assert result.json()["reason"] == "accepted_or_started" and not fixture.files.stopped
            fixture.hosted.portal.call(write, original)
            source_state = "idle"
        if result.json()["state"] == "ready":
            break
    assert result.json()["state"] == "ready" and result.json()["hosted_agent_version"] == "43"
    assert (workspace.root / "nested" / "draft.txt").read_bytes() == b"uncommitted original\n"
    assert fixture.destination.session_id in fixture.files.targets
    assert canonical_bytes(fixture.hosted.portal.call(fixture.runtime._rid, fixture.saved.request_id).value) == canonical_bytes(original)
    assert not runtime.executor.calls


def test_session_observation_keeps_old_wire_bytes_without_an_original_retarget_selector():
    from shared_context.behavior_native_turn_wire import NativeSessionRead, NATIVE_REQUEST
    assert NativeSessionRead().model_dump_json() == '{"operation":"session_read"}'
    original = NativeSessionRead(environment_request_id="original-retarget")
    assert NATIVE_REQUEST.validate_json(original.model_dump_json()) == original


@pytest.mark.parametrize("complete_first_page", [False, True])
def test_page_failure_reports_the_actual_official_cursor_source(boundary_history, complete_first_page, caplog):
    case = boundary_history
    if complete_first_page:
        for _ in range(3):
            first = omit(case.fixture, preview=True)
            assert first.status_code == 200
        assert len(case.reads) == 100
    case.fail_cursor = True
    response = omit(case.fixture, preview=True)
    assert response.status_code == 503, response.text
    detail = response.json()["error"]["files_check"]
    assert detail["backend_status"] == 400 and detail["backend_code"] == "bad_request"
    assert detail["cursor_source"] == ("page_last_id" if complete_first_page else "none")
    events = [json.loads(record.getMessage().removeprefix("NATIVE_INVOCATION_FAILURE "))
              for record in caplog.records if record.getMessage().startswith("NATIVE_INVOCATION_FAILURE ")]
    assert events[-1]["files_check"]["cursor_source"] == detail["cursor_source"]
    assert "opaque-id" not in response.text and "opaque-id" not in json.dumps(events)
    assert not case.fixture.runtime.executor.calls


def test_legacy_check_cursor_restarts_from_the_top_without_laundering_its_source(boundary_history):
    case = boundary_history
    assert omit(case.fixture, preview=True).status_code == 200
    first_read = list(case.reads)
    key, = case.scan_keys

    async def legacy_progress():
        async with case.fixture.runtime._store(TURNS_STORE) as store:
            item = await store.get_item(key)
            value = dict(item.value)
            for name in ("cursor_profile", "page_offset", "page_digest"):
                value.pop(name, None)
            value["after"] = case.items[27].id
            await store.set_item(key, value, if_match=item.etag)

    case.fixture.hosted.portal.call(legacy_progress)
    next_page = omit(case.fixture, preview=True)
    assert next_page.status_code == 200, next_page.text
    assert case.calls == [None, None]
    assert case.reads == first_read * 2
    assert next_page.json()["files_check"]["cursor_source"] == "none"
    assert not case.fixture.files.stopped and not case.fixture.runtime.executor.calls


def test_legacy_follow_cursor_cannot_reenter_with_an_unproven_mid_page_id(boundary_history):
    from shared_context import native_environment

    case = boundary_history

    async def legacy_follow():
        async with case.fixture.runtime._store(TURNS_STORE) as store:
            key = native_environment._key(case.fixture.runtime)
            item = await store.get_item(key)
            value = dict(item.value)
            value.pop("cursor_profile", None)
            value.update(phase="draining", after=case.items[27].id)
            await store.set_item(key, value, if_match=item.etag)

    case.fixture.hosted.portal.call(legacy_follow)
    case.tick = 0
    response = follow(case.fixture)
    assert case.calls[0] is None, "An old unproven cursor must be rechecked, not relabeled page_last_id"
    assert response.status_code in {200, 409}, response.text
    assert not case.fixture.runtime.executor.calls


def test_partial_page_counts_metadata_and_reserves_its_own_progress_before_listing(boundary_history, monkeypatch):
    case = boundary_history
    del case.items[80:]
    for index, item in enumerate(case.items):
        if index % 2 == 0:
            item.key = f"retained-metadata:{index}"
    original_create = FoundryStateStore.create_item
    added = []

    async def include_progress(store, key, value, **kwargs):
        result = await original_create(store, key, value, **kwargs)
        if store._name == TURNS_STORE and key.startswith("files-check:"):
            entry = SimpleNamespace(key=key, id="own-progress-id", etag="created")
            added.append(entry)
            case.items.insert(0, entry)
        return result

    monkeypatch.setattr(FoundryStateStore, "create_item", include_progress)
    for index in range(3):
        response = omit(case.fixture, preview=True)
        assert response.status_code == 200, response.text
        added[0].etag = f"updated-progress-{index}"
        if response.json()["reason"] is None:
            break
    assert len(added) == 1 and case.page_rows[0][0] == "own-progress-id"
    assert response.json()["reason"] is None
    assert len(case.calls) > 1 and all(after is None for after in case.calls)
    assert len(case.reads) == len(set(case.reads)) == 40
    assert not case.fixture.files.stopped and not case.fixture.runtime.executor.calls


def test_one_partial_page_timeout_keeps_the_processed_prefix(boundary_history, monkeypatch):
    from shared_context import native_environment

    case = boundary_history
    first = omit(case.fixture, preview=True)
    assert first.status_code == 200 and first.json()["reason"] == "scan_pending"
    first_reads = list(case.reads)
    original_list = FoundryStateStore.list_keys
    stalled = []

    async def one_timeout(store, **kwargs):
        if store._name == TURNS_STORE and not stalled:
            stalled.append(kwargs["after"])
            await asyncio.sleep(1)
        return await original_list(store, **kwargs)

    monkeypatch.setattr(native_environment, "_CHECK_READ_SECONDS", 0.2)
    monkeypatch.setattr(FoundryStateStore, "list_keys", one_timeout)
    timeout = omit(case.fixture, preview=True)
    assert timeout.status_code == 200 and timeout.json()["reason"] == "scan_pending", timeout.text
    assert timeout.json()["files_check"]["read_attempt"] == 1
    assert timeout.json()["files_check"]["cursor_source"] == "none"
    resumed = omit(case.fixture, preview=True)
    assert resumed.status_code == 200 and resumed.json()["reason"] == "scan_pending", resumed.text
    assert stalled == [None] and case.calls == [None, None]
    assert len(case.reads) > len(first_reads)
    assert all(case.reads.count(key) == 1 for key in first_reads)


@pytest.mark.parametrize("phase", ["files", "draining"])
def test_confirmed_follow_finishes_a_page_slower_than_one_scan_window(boundary_history, phase):
    from shared_context import native_environment

    case = boundary_history
    del case.items[16:]
    if phase == "draining":
        async def still_draining():
            async with case.fixture.runtime._store(TURNS_STORE) as store:
                key = native_environment._key(case.fixture.runtime)
                item = await store.get_item(key)
                value = dict(item.value)
                value["phase"] = "draining"
                await store.set_item(key, value, if_match=item.etag)

        case.fixture.hosted.portal.call(still_draining)
    preview = omit(case.fixture, preview=True)
    assert preview.status_code == 200 and preview.json()["reason"] is None, preview.text
    case.reads.clear()
    case.tick = 0.2
    result = omit(case.fixture)
    for _ in range(10):
        assert result.status_code == 200, result.text
        if result.json()["state"] == "ready":
            break
        assert result.json()["reason"] == "scan_pending"
        result = follow(case.fixture)
    assert result.json()["state"] == "ready", "A slow page must not restart its checked prefix forever"
    expected_reads = 2 if phase == "draining" else 1
    assert len(set(case.reads)) == 16
    assert all(count == expected_reads for count in Counter(case.reads).values())
    assert sum(command.get("stage") == "confirm" for command, _ in case.fixture.controls) == 1
    assert result.json()["files_migrated"] is False and not case.fixture.runtime.executor.calls


@pytest.mark.parametrize("saved_unactivated", [False, True])
def test_confirmation_metadata_restarts_only_the_mutable_follow_page(boundary_history, monkeypatch, saved_unactivated):
    from shared_context import native_environment

    case = boundary_history
    del case.items[16:]
    original_create, original_set = FoundryStateStore.create_item, FoundryStateStore.set_item
    metadata, snapshots, activation_failed = [], [], []

    async def include_new_metadata(store, key, value, **kwargs):
        result = await original_create(store, key, value, **kwargs)
        if store._name == TURNS_STORE and key.startswith(("files-check:", "files-confirmation:", "files-omission:")):
            metadata.append(key)
            case.items.insert(0, SimpleNamespace(key=key, id=f"new-metadata-{len(metadata)}", etag="new"))
            if key.startswith("files-omission:"):
                snapshots.append(canonical_bytes(value))
        return result

    async def activation_write(store, key, value, **kwargs):
        assert not key.startswith("files-omission:"), "The immutable confirmation must not be reset"
        if (saved_unactivated and not activation_failed and key.startswith("following:")
                and value["view"].get("files_migrated") is False):
            activation_failed.append(key)
            raise ServiceResponseError("OFFLINE failure before activating the saved immutable confirmation")
        return await original_set(store, key, value, **kwargs)

    async def draining():
        async with case.fixture.runtime._store(TURNS_STORE) as store:
            key = native_environment._key(case.fixture.runtime)
            item = await store.get_item(key)
            await store.set_item(key, {**item.value, "phase": "draining"}, if_match=item.etag)

    case.fixture.hosted.portal.call(draining)
    item_reader = FoundryStateStore.get_item

    async def slow_receipt(store, key, **kwargs):
        if store._name == TURNS_STORE and key.startswith("rid:boundary-"):
            await asyncio.sleep(0.04)
        return await item_reader(store, key, **kwargs)

    with monkeypatch.context() as slow:
        slow.setattr(native_environment, "_SCAN_SECONDS", 0.15)
        slow.setattr(FoundryStateStore, "get_item", slow_receipt)
        partial = follow(case.fixture)
    assert partial.status_code == 200 and partial.json()["reason"] == "scan_pending", partial.text
    assert 0 < len(case.reads) < 16
    monkeypatch.setattr(FoundryStateStore, "create_item", include_new_metadata)
    monkeypatch.setattr(FoundryStateStore, "set_item", activation_write)
    preview = omit(case.fixture, preview=True)
    assert preview.status_code == 200 and preview.json()["reason"] is None, preview.text
    result = omit(case.fixture)
    if saved_unactivated:
        assert result.status_code == 503 and len(activation_failed) == 1, result.text
        result = omit(case.fixture, resume=True)
    assert result.status_code == 200 and result.json()["state"] == "ready", result.text
    assert len(metadata) == 3 and len(snapshots) == 1
    retained = json.loads(snapshots[0])["original"]
    assert retained["page_offset"] > 0 and retained["page_digest"], "The immutable original must remain partial"
    assert sum(command.get("stage") == "confirm" for command, _ in case.fixture.controls) == 1
    assert not case.fixture.runtime.executor.calls
