"""Heavy-history checks use the real API/runtime and named offline storage reads."""

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.core.exceptions import ServiceResponseError

from shared_context.native_checkpoint import conversation_store_name
from test_native_files_api import (
    file_api, file_loops, follow, instruction_models, jwk_client, local_env,
    native_api, signing_key, synthetic_standing_v1, token, legacy_source,
)
from test_native_files_omission import omit


@pytest.mark.parametrize("status,platform_code,expected", [
    (400, "invalid_request_error", "invalid_request_error"),
    (400, "InvalidCursor", "InvalidCursor"),
    (403, "Forbidden", "Forbidden"),
    (429, "TooManyRequests", "TooManyRequests"),
    (503, "ServiceUnavailable", "ServiceUnavailable"),
    (400, "PRIVATE-key:rid-secret/token", "UNCLASSIFIED"),
    (400, "private_value_without_punctuation", "UNCLASSIFIED"),
])
def test_backend_list_error_retains_only_allowlisted_status_and_code(
        file_api, monkeypatch, caplog, status, platform_code, expected):
    from azure.ai.agentserver.core.storage._errors import raise_for_storage_error

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    calls = []

    async def refused_page(*_args, **_kwargs):
        calls.append(True)
        raise_for_storage_error(SimpleNamespace(
            status_code=status, headers={},
            text=lambda: json.dumps({"error": {
                "code": platform_code, "message": "PRIVATE request keys and content",
                "param": "PRIVATE-cursor", "innererror": {"token": "PRIVATE-token"},
            }})))

    monkeypatch.setattr(FoundryStateStore, "list_keys", refused_page)
    result = omit(fixture, preview=True)
    assert result.status_code == 503, result.text
    detail = result.json()["error"]["files_check"]
    assert detail["category"] == "storage_read_failed" and detail["stage"] == "list"
    assert detail["failure_kind"] == "backend"
    assert detail["backend_status"] == status
    assert detail["backend_code"] == expected
    events = [json.loads(record.getMessage().removeprefix("NATIVE_INVOCATION_FAILURE "))
              for record in caplog.records if record.getMessage().startswith("NATIVE_INVOCATION_FAILURE ")]
    assert events[-1]["files_check"]["backend_status"] == status
    assert events[-1]["files_check"]["backend_code"] == expected
    assert "PRIVATE" not in result.text and "PRIVATE" not in json.dumps(events)
    assert len(calls) == 1 and not fixture.runtime.executor.calls


@pytest.mark.parametrize("last_id", ["cursor-with+/=:%value", "尾頁/+%=:", "abc%2Fdef"])
def test_fixed_sdk_second_list_uses_first_last_id_without_double_encoding(monkeypatch, last_id):
    monkeypatch.setenv("FOUNDRY_HOSTING_ENVIRONMENT", "OFFLINE-request-builder-only")
    store = FoundryStateStore(
        "fa/v1/turns", credential=SimpleNamespace(), endpoint="https://offline.example.invalid/api/projects/test")
    requests = []

    async def response(request):
        requests.append(request)
        if len(requests) == 1:
            payload = {"object": "list", "data": [
                {"id": "not-the-smallest-key", "object": "state_store.item", "key": "aaa-first-key",
                 "etag": "etag", "created_at": 20, "updated_at": 20},
                {"id": last_id, "object": "state_store.item", "key": "zzz-last-key",
                 "etag": "etag", "created_at": 10, "updated_at": 10}],
                "first_id": "not-the-smallest-key", "last_id": last_id, "has_more": True}
        else:
            payload = {"object": "list", "data": [], "first_id": None, "last_id": None, "has_more": False}
        return SimpleNamespace(status_code=200, headers={}, text=lambda: json.dumps(payload))

    monkeypatch.setattr(store._client, "send_request", response)

    async def read_two():
        try:
            first = await store.list_keys(limit=100, order="desc")
            assert first.last_id == last_id
            return await store.list_keys(limit=100, order="desc", after=first.last_id)
        finally:
            await store.aclose()

    final = asyncio.run(read_two())
    assert len(requests) == 2 and not final.has_more
    assert parse_qs(urlsplit(requests[0].url).query) == {"api-version": ["v1"], "limit": ["100"], "order": ["desc"]}
    assert parse_qs(urlsplit(requests[1].url).query) == {
        "api-version": ["v1"], "limit": ["100"], "order": ["desc"], "after": [last_id]}


def test_fixed_sdk_list_keys_requests_one_page_with_an_opaque_after_cursor(monkeypatch):
    monkeypatch.setenv("FOUNDRY_HOSTING_ENVIRONMENT", "OFFLINE-request-builder-only")
    requests = []
    store = FoundryStateStore(
        "fa/v1/turns", credential=SimpleNamespace(), endpoint="https://offline.example.invalid/api/projects/test")

    async def send(request):
        requests.append(request)
        return SimpleNamespace(text=lambda: json.dumps({
            "object": "list", "data": [{
                "id": "opaque-item-id", "object": "state_store.item", "key": "rid:not-the-cursor",
                "etag": "etag", "created_at": 1, "updated_at": 1,
            }], "has_more": True, "first_id": "opaque-item-id", "last_id": "opaque-item-id"}))

    monkeypatch.setattr(store, "_send_storage_request", send)

    async def read():
        try:
            return await store.list_keys(limit=100, after="opaque +/=:cursor", order="desc")
        finally:
            await store.aclose()

    page = asyncio.run(read())
    assert len(requests) == 1, "The fixed SDK must not consume the following pages inside list_keys"
    request = requests[0]
    assert request.method == "GET"
    assert urlsplit(request.url).path.endswith("/storage/state_stores/ZmEvdjEvdHVybnM/items:keys")
    assert parse_qs(urlsplit(request.url).query) == {
        "api-version": ["v1"], "limit": ["100"], "after": ["opaque +/=:cursor"], "order": ["desc"]}
    assert page.has_more and page.last_id == "opaque-item-id"
    assert page.keys[0].key == "rid:not-the-cursor"


@pytest.mark.parametrize("scenario", ["cost", "active", "corrupt-latest"])
def test_omission_still_reads_every_receipt_but_does_not_reread_a_superseding_head_for_each_old_turn(
    file_api, monkeypatch, scenario,
):
    from shared_context.behavior_native_turn_wire import NativeSubmit
    from shared_context.native_turns import TURNS_STORE, _Meta, _Turn, _changed, _decode, _json

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    cid = fixture.saved.conversation_id

    async def history():
        async with fixture.runtime._store(conversation_store_name(cid)) as store:
            original = await store.get_item("meta")
            meta = _decode(original.value, _Meta)
            turn = _decode((await store.get_item("turn:1")).value, _Turn)
            checkpoints = tuple(_changed(ref, lineage=_changed(ref.lineage, turn_number=31))
                                if ref.lineage is not None else ref for ref in turn.checkpoints)
            latest = _changed(turn, n=31, checkpoints=checkpoints)
            await store.create_item("turn:31", _json(latest))
            await store.set_item("meta", _json(_changed(meta, n=31)), if_match=original.etag)
        async with fixture.runtime._store(TURNS_STORE) as store:
            row = await store.get_item("rid:original-rid")
            original = (await fixture.runtime._rid("original-rid")).value
            current = _changed(original, receipt=_changed(
                original.receipt, turn_number=31, result=latest.reference()))
            await store.set_item("rid:original-rid", _json(current), if_match=row.etag)
            for number in range(1, 31):
                rid = f"history-{number:03d}"
                submitted = NativeSubmit(command=fixture.saved.command.model_copy(update={
                    "request_id": rid, "conversation_id": cid}), capture=fixture.saved.capture)
                receipt = _changed(original.receipt, request_id=rid, command=submitted.command,
                    submit_digest=submitted.sha256, turn_number=number, result=original.receipt.result.model_copy(
                        update={"turn_number": number, "turn_key": f"turn:{number}"}))
                if scenario == "active" and number == 5:
                    receipt = _changed(receipt, status="started", result=None)
                await store.create_item("rid:" + rid, _json(_changed(original, receipt=receipt)))

    fixture.hosted.portal.call(history)
    reads, original_get = [], FoundryStateStore.get_item

    async def measured(store, key, **options):
        reads.append((store._name, key))
        if scenario == "corrupt-latest" and store._name == conversation_store_name(cid) and key.startswith("ckpt:"):
            return None
        return await original_get(store, key, **options)

    monkeypatch.setattr(FoundryStateStore, "get_item", measured)
    checked = omit(fixture, preview=True)
    if scenario != "cost":
        expected = "NATIVE_FILES_CONFIRMATION_REQUIRED" if scenario == "active" else "SDK_STATE_INCOMPLETE"
        assert checked.status_code == 409 and checked.json()["error"]["code"] == expected, checked.text
        assert not fixture.files.stopped and not fixture.runtime.executor.calls
        return
    assert checked.status_code == 200 and checked.json()["reason"] != "scan_pending", checked.text
    history_reads = {key for store, key in reads if store == TURNS_STORE and key.startswith("rid:")}
    assert history_reads == {"rid:original-rid", *(f"rid:history-{n:03d}" for n in range(1, 31))}
    meta_reads = [key for store, key in reads if store == conversation_store_name(cid) and key == "meta"]
    assert len(meta_reads) <= 2, "one verified newer head makes older same-CID turn heads redundant within this page"
    assert (conversation_store_name(cid), "turn:31") in reads
    assert any(store == conversation_store_name(cid) and key.startswith("ckpt:") for store, key in reads)
    assert not fixture.files.stopped and not fixture.runtime.executor.calls


@pytest.mark.parametrize("persistent_timeout", [False, True])
def test_thousand_receipts_resume_one_page_at_a_time_after_a_page_timeout(file_api, monkeypatch, persistent_timeout):
    from shared_context import native_environment
    from shared_context.behavior_native_turn_wire import NativeSubmit, canonical_bytes
    from shared_context.native_turns import TURNS_STORE, _changed, _json

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    original_get, original_list = FoundryStateStore.get_item, FoundryStateStore.list_keys
    sample = fixture.hosted.portal.call(fixture.runtime._rid, "original-rid")
    receipts = {}
    for index in range(999):
        rid = f"list-history-{index:04d}"
        command = NativeSubmit(command=fixture.saved.command.model_copy(update={
            "request_id": rid, "conversation_id": fixture.saved.conversation_id}), capture=fixture.saved.capture)
        value = _changed(sample.value, receipt=_changed(
            sample.value.receipt, request_id=rid, command=command.command, submit_digest=command.sha256,
            status="unknown", result=None))
        receipts["rid:" + rid] = SimpleNamespace(
            key="rid:" + rid, id=f"opaque-page-item-{index:04d}", etag="fixed-" + rid, value=_json(value))
    receipts["rid:original-rid"] = SimpleNamespace(
        key="rid:original-rid", id="opaque-page-item-0999", etag=sample.etag, value=_json(sample.value))
    items = list(receipts.values())
    positions = {item.id: index for index, item in enumerate(items)}
    pages, read_keys, timeouts = [], [], []
    before = {key: canonical_bytes(item.value) for key, item in receipts.items()}
    failed_after = "opaque-page-item-0299"

    async def page_read(store, *, limit=None, after=None, **kwargs):
        if store._name != TURNS_STORE:
            return await original_list(store, limit=limit, after=after, **kwargs)
        pages.append({"limit": limit, "after": after})
        if after == failed_after and (persistent_timeout or not timeouts):
            timeouts.append(after)
            await asyncio.sleep(1)
        start = 0 if after is None else positions[after] + 1
        selected = items[start:start + limit]
        return SimpleNamespace(keys=selected, has_more=start + limit < len(items),
                               first_id=selected[0].id if selected else None,
                               last_id=selected[-1].id if selected else None)

    async def receipt_read(store, key, **kwargs):
        if store._name == TURNS_STORE and key in receipts:
            read_keys.append(key)
            return receipts[key]
        return await original_get(store, key, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "list_keys", page_read)
    monkeypatch.setattr(FoundryStateStore, "get_item", receipt_read)
    monkeypatch.setattr(native_environment, "_CHECK_READ_SECONDS", 0.2)
    results = []
    for _ in range(11):
        previous_pages = len(pages)
        result = omit(fixture)
        if result.status_code != 200 or result.json().get("state") != "ready":
            assert len(pages) == previous_pages + 1, "One check slice must request only one key page"
        assert pages[previous_pages]["limit"] == 100
        if result.status_code == 503:
            assert persistent_timeout and len(timeouts) == 2
            detail = result.json()["error"]["files_check"]
            assert detail["stage"] == "list" and detail["failure_kind"] == "deadline"
            assert detail["read_attempt"] == 2
            assert not any(command.get("stage") == "confirm" for command, _ in fixture.controls)
            return
        assert result.status_code == 200, result.text
        results.append(result.json())
        if result.json()["reason"] != "scan_pending":
            break
        assert result.json()["files_confirmation_pending"] == "owner-confirms-no-files-once"
    assert len(timeouts) == 1
    assert pages[3]["after"] == pages[4]["after"] == failed_after
    assert len(results) == 11 and results[9]["reason"] == "scan_pending"
    assert results[-1]["state"] == "ready"
    assert results[-1]["files_migrated"] is False
    assert set(read_keys) == set(receipts)
    assert {key: canonical_bytes(item.value) for key, item in receipts.items()} == before
    assert sum(command.get("operation") == "session_files_omission" and command.get("stage") == "confirm"
               for command, _ in fixture.controls) == 1
    assert not fixture.runtime.executor.calls


@pytest.mark.parametrize("failed_key,key_category", [
    ("rid:original-rid", "receipt"), ("meta", "conversation"), ("turn:1", "turn"), ("ckpt:", "checkpoint"),
])
def test_store_read_failure_reports_the_exact_stage_category_without_private_data(file_api, monkeypatch, failed_key, key_category):
    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    original_get = FoundryStateStore.get_item

    async def unavailable(store, key, **kwargs):
        if key == failed_key or failed_key == "ckpt:" and key.startswith("ckpt:"):
            raise ServiceResponseError("PRIVATE backend URI and token must not appear")
        return await original_get(store, key, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "get_item", unavailable)
    response = omit(fixture, preview=True)
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "NATIVE_STATE_STORE_UNKNOWN"
    assert error["files_check"]["category"] == "storage_read_failed"
    assert error["files_check"]["stage"] == "read"
    assert error["files_check"]["key_category"] == key_category
    assert error["files_check"]["request_id_prefix"] == "original"
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize("boundary", ["open", "list", "read_timeout"])
def test_read_bounds_identify_open_list_or_individual_read_deadlines(file_api, monkeypatch, boundary):
    from shared_context import native_environment

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    original_get = FoundryStateStore.get
    original_item = FoundryStateStore.get_item

    async def failed_open(store, **kwargs):
        if store._name == conversation_store_name(fixture.saved.conversation_id):
            raise ServiceResponseError("PRIVATE open failure")
        return await original_get(store, **kwargs)

    async def failed_list(*_args, **_kwargs):
        raise ServiceResponseError("PRIVATE list failure")

    async def stalled_item(store, key, **kwargs):
        if key == "rid:original-rid":
            await asyncio.sleep(1)
        return await original_item(store, key, **kwargs)

    if boundary == "open":
        monkeypatch.setattr(FoundryStateStore, "get", failed_open)
    elif boundary == "list":
        monkeypatch.setattr(FoundryStateStore, "list_keys", failed_list)
    else:
        monkeypatch.setattr(FoundryStateStore, "get_item", stalled_item)
        monkeypatch.setattr(native_environment, "_CHECK_READ_SECONDS", 0.2)
    result = omit(fixture, preview=True)
    assert result.status_code == 503 and result.json()["error"]["code"] == "NATIVE_STATE_STORE_UNKNOWN"
    detail = result.json()["error"]["files_check"]
    assert detail["category"] == "storage_read_failed"
    assert detail["stage"] == ("read" if boundary == "read_timeout" else boundary)
    assert detail["key_category"] == ("conversation" if boundary == "open" else "receipt")
    assert "backend_status" not in detail and "backend_code" not in detail
    assert "PRIVATE" not in result.text


def test_cancelled_store_open_closes_its_client_instead_of_leaving_the_read_running(monkeypatch):
    from shared_context import native_checkpoint

    closed = []

    class SlowStore:
        async def get(self):
            await asyncio.sleep(1)

        async def aclose(self):
            closed.append(True)

    storage = native_checkpoint._storage()
    monkeypatch.setattr(native_checkpoint, "_storage", lambda: SimpleNamespace(
        FoundryStateStore=lambda *_a, **_k: SlowStore(),
        FoundryStorageNotFoundError=storage.FoundryStorageNotFoundError,
        FoundryStorageError=storage.FoundryStorageError))

    async def interrupted():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await native_checkpoint.open_durable_store("test", create=False)

    asyncio.run(interrupted())
    assert closed == [True]


def test_confirmation_check_progress_never_dispatches_confirmation_until_scanning_finishes(file_api, monkeypatch):
    from shared_context import native_environment

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    drain, reads = native_environment._drain_pages, []

    async def delayed_read(*args, **kwargs):
        if kwargs.get("require_complete") and len(reads) < 2:
            reads.append(True)
            return "scan_pending", None, False
        return await drain(*args, **kwargs)

    monkeypatch.setattr(native_environment, "_drain_pages", delayed_read)
    for _ in range(2):
        result = omit(fixture)
        assert result.status_code == 200 and result.json()["reason"] == "scan_pending", result.text
        assert result.json()["files_confirmation_pending"] == "owner-confirms-no-files-once"
        assert result.json().get("files_migrated") is None
        assert not any(command.get("operation") == "session_files_omission" and command.get("stage") == "confirm"
                       for command, _ in fixture.controls)
    result = omit(fixture)
    assert result.status_code == 200 and result.json()["state"] == "ready", result.text
    assert sum(command.get("operation") == "session_files_omission" and command.get("stage") == "confirm"
               for command, _ in fixture.controls) == 1
    assert not fixture.runtime.executor.calls


@pytest.mark.parametrize("history_state,fail_once", [("unknown", False), ("completed", False), ("completed", True)])
def test_large_history_resumes_inside_a_multi_read_checkpoint_instead_of_restarting_it(file_api, monkeypatch, history_state, fail_once):
    from shared_context import native_environment
    from shared_context.behavior_native_turn_wire import NativeSubmit, canonical_bytes
    from shared_context.native_turns import TURNS_STORE, _changed, _json

    fixture = file_api
    legacy_source(fixture)
    assert follow(fixture).status_code == 409
    old_get, old_keys = FoundryStateStore.get_item, FoundryStateStore.list_keys
    sample = fixture.hosted.portal.call(fixture.runtime._rid, "original-rid").value
    receipts = {}
    for index in range(299):
        rid = f"history-{index:04d}"
        command = NativeSubmit(command=fixture.saved.command.model_copy(update={
            "request_id": rid, "conversation_id": fixture.saved.conversation_id}),
                               capture=fixture.saved.capture)
        value = _changed(sample, receipt=_changed(
            sample.receipt, request_id=rid, command=command.command, submit_digest=command.sha256,
            status=history_state, result=None if history_state == "unknown" else sample.receipt.result))
        receipts["rid:" + rid] = SimpleNamespace(
            key="rid:" + rid, id=f"opaque-{index:04d}", etag="fixed-" + rid, value=_json(value))
    original = fixture.hosted.portal.call(fixture.runtime._rid, "original-rid")
    receipts["rid:original-rid"] = SimpleNamespace(
        key="rid:original-rid", id="opaque-0299", etag=original.etag, value=_json(original.value))
    keys = list(receipts)
    before = {key: canonical_bytes(item.value) for key, item in receipts.items()}
    reads, pages, history_reads, failures = [], [], [], []
    current_rid, elapsed = [None], [0.0]

    async def history_page(store, *, limit=None, after=None, **kwargs):
        if store._name != TURNS_STORE:
            return await old_keys(store, limit=limit, after=after, **kwargs)
        start = 0 if after is None else next(i for i, key in enumerate(keys) if receipts[key].id == after) + 1
        selected = [receipts[key] for key in keys[start:start + limit]]
        pages.append(after)
        return SimpleNamespace(keys=selected, first_id=selected[0].id if selected else None,
                               last_id=selected[-1].id if selected else None, has_more=start + limit < len(keys))

    async def bounded_read(store, key, **kwargs):
        if store._name == conversation_store_name(fixture.saved.conversation_id):
            reads.append((current_rid[0], key))
            if fail_once and current_rid[0] == "rid:original-rid" and key == "meta" and not failures:
                failures.append(key)
                raise ServiceResponseError("OFFLINE one unconfirmed metadata read")
            await asyncio.sleep(0.08 if current_rid[0] == "rid:original-rid" else 0.001)
        if store._name == TURNS_STORE and key in receipts:
            history_reads.append(key)
            elapsed[0] += 0.005
            current_rid[0] = key
            return receipts[key]
        return await old_get(store, key, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "get_item", bounded_read)
    monkeypatch.setattr(FoundryStateStore, "list_keys", history_page)
    monkeypatch.setattr(native_environment, "_SCAN_SECONDS", 0.2)
    monkeypatch.setattr(native_environment, "monotonic", lambda: elapsed[0])
    responses = []
    for _ in range(10):
        prior_reads, prior_pages = len(history_reads), len(pages)
        response = omit(fixture, preview=True)
        assert len(history_reads) - prior_reads <= 41, "The outer drain must return when the slice budget is spent"
        assert len(pages) - prior_pages <= 3, "A spent slice must not turn into one list request per remaining key"
        if response.status_code == 503:
            assert fail_once and len(failures) == 1
            assert response.json()["error"]["files_check"]["key_category"] == "conversation"
            continue
        assert response.status_code == 200, response.text
        responses.append(response.json())
        if response.json()["reason"] is None:
            break
    assert responses[-1]["reason"] is None, "A checkpoint longer than one slice must still make progress"
    assert 1 < len(responses) <= 10
    assert len(set(history_reads)) == 300
    assert set(history_reads) == set(keys)
    assert reads.count(("rid:original-rid", "meta")) <= 2, "The checkpoint prefix must not restart every slice"
    assert all(history_reads.count(key) == 1 for key in keys if key != "rid:original-rid")
    assert {key: canonical_bytes(item.value) for key, item in receipts.items()} == before
    assert not fixture.runtime.executor.calls
    monkeypatch.setattr(native_environment, "_SCAN_SECONDS", 10)
    ready = omit(fixture)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["files_migrated"] is False
    assert {key: canonical_bytes(item.value) for key, item in receipts.items()} == before
