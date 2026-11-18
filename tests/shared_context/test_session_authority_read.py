"""Row-scoped pure reads: unrelated writers may move the shared guard, read rows may not."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from shared_context.behavior_store import GUARD_KEY, BehaviorStore
from shared_context.errors import Problem
from test_behavior_store import FakeTable, SCOPE, changes, code, request, seed, store, write
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from session_authority_read_helpers import runtime_path


def _in_other_thread(action):
    def run():
        with ThreadPoolExecutor(max_workers=1) as writer:
            return writer.submit(action).result()
    return run


def _read_with_writer(adapter, keys, commit, *, row_scoped):
    def read():
        value = adapter.read_snapshot(keys)
        _in_other_thread(commit)()
        assert adapter.read_snapshot(keys) == value
        return value
    return adapter.consistent_read(read, row_scoped=row_scoped)


@pytest.mark.parametrize("row_scoped", [False, True])
def test_unrelated_writer_only_passes_row_scoped_read_after_exact_reverification(store, row_scoped):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    commit = lambda: other.transact(request("unrelated", ("owner",)), lambda _: changes(
        write("owner", {"epoch": 2}, "replace")))
    client.reads.clear()
    if not row_scoped:
        with pytest.raises(Problem) as failure:
            _read_with_writer(adapter, ("budget", "missing"), commit, row_scoped=False)
        assert code(failure) == "BEHAVIOR_CONFLICT"
        return
    result = _read_with_writer(adapter, ("budget", "missing"), commit, row_scoped=True)
    assert result["budget"].payload["data"]["remaining"] == 1 and result["missing"] is None
    assert sorted(key for _, key in client.reads) == sorted([
        GUARD_KEY, GUARD_KEY, "behavior:v1:row:budget", "behavior:v1:row:budget",
        "behavior:v1:row:missing", "behavior:v1:row:missing"])


@pytest.mark.parametrize("change", ["same-row", "missing-row-created", "receipt-created"])
def test_row_scoped_read_rejects_any_change_to_what_it_read(store, change):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    if change == "same-row":
        keys, commit = ("budget",), lambda: other.transact(request("spend", ("budget",)), lambda _: changes(
            write("budget", {"remaining": 0}, "replace")))
    elif change == "missing-row-created":
        keys, commit = ("missing",), lambda: other.transact(request("create"), lambda _: changes(
            write("missing", {"created": True})))
    else:
        keys, commit = (), lambda: other.transact(request("later"), lambda _: changes(write("later", {})))

    def read():
        value = adapter.read_snapshot(keys)
        if change == "receipt-created":
            with pytest.raises(Problem):
                adapter.reconcile(request("later"))
        _in_other_thread(commit)()
        return value
    with pytest.raises(Problem) as failure:
        adapter.consistent_read(read, row_scoped=True)
    assert code(failure) == "BEHAVIOR_CONFLICT"


def test_row_scoped_read_still_cannot_borrow_another_store_or_accept_nonboolean(store):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    with pytest.raises(Problem) as crossed:
        adapter.consistent_read(lambda: other.read_snapshot(("budget",)), row_scoped=True)
    assert code(crossed) == "BEHAVIOR_READ_SCOPE_INVALID"
    with pytest.raises(Problem) as flag:
        adapter.consistent_read(lambda: None, row_scoped=1)
    assert code(flag) == "BEHAVIOR_READ_SCOPE_INVALID"


@pytest.mark.parametrize("fault", ["epoch", "installation", "same-config-image", "guard-etag", "guard-missing"])
def test_row_scoped_migration_view_keeps_guard_epoch_and_installation_checks(fault):
    from shared_context.behavior_migration import MigrationStore
    client = FakeTable()
    adapter = MigrationStore(client, SCOPE)
    adapter.initialize()
    adapter.transact(request("install"), lambda _: changes(write("install", {"configuration_sha256": "a" * 64})))
    adapter.bind_installation("install", "a" * 64)

    def read():
        adapter.read_snapshot(("install",))
        k = GUARD_KEY if fault in {"epoch", "guard-etag", "guard-missing"} else "behavior:v1:row:install"
        entity = client.backing.rows[(SCOPE.table_name, SCOPE.partition_key, k)]
        if fault == "guard-missing":
            del client.backing.rows[(SCOPE.table_name, SCOPE.partition_key, k)]
        elif fault == "guard-etag":
            entity.metadata["etag"] = 'W/"replacement"'
        else:
            payload = json.loads(entity["Payload"])
            if fault == "epoch":
                payload.update(schema_version=2, configuration_sha256="b" * 64)
            elif fault == "installation":
                payload["data"]["configuration_sha256"] = "b" * 64
            else:
                payload["data"]["changed_metadata"] = True
            entity["Payload"] = json.dumps(payload).encode()
        return "not an authorized result"
    with pytest.raises(Problem):
        adapter.consistent_read(read, row_scoped=True)


def test_plain_snapshot_accepts_unrelated_guard_writer_only_after_rereading_its_keys(store):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)

    writes = []

    def unrelated(key):
        if key == GUARD_KEY:
            client.after_get = unrelated
            return
        writes.append(key)
        other.transact(request(f"unrelated-{len(writes)}", ("owner",)), lambda _: changes(
            write("owner", {"epoch": len(writes) + 1}, "replace")))

    client.reads.clear()
    client.after_get = unrelated
    assert adapter.read_snapshot(("grant",))["grant"].payload["data"]["allowed"] is True
    assert [key for _, key in client.reads] == [GUARD_KEY, "behavior:v1:row:grant", GUARD_KEY, "behavior:v1:row:grant"]
    client.after_get = unrelated
    committed = adapter.transact(request("rebased-writer", ("grant",)), lambda _: changes(write("after", {})))
    assert committed.payload["guard_revision"] == adapter._guard()[0].revision

