"""Pure read scopes on the same public store seam; never Azure evidence."""
from collections import Counter
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import copy
import json
import threading

import pytest

from shared_context.behavior_store import BehaviorStore, GUARD_KEY
from shared_context.errors import Problem
from test_behavior_store import FakeTable, SCOPE, store, seed, request, changes, write, code


def test_operation_reuses_typed_rows_and_receipts_but_returns_independent_copies(store):
    adapter, client = store
    seed(adapter)
    client.reads.clear()
    posts = len(client.submissions)
    def read():
        first = adapter.read_snapshot(("budget", "missing"))
        first["budget"].payload["data"]["remaining"] = 999
        second = adapter.read_snapshot(("budget", "missing"))
        assert second["budget"].payload["data"]["remaining"] == 1
        receipt = adapter.reconcile(request("seed"))
        receipt.payload["result"]["accepted"] = False
        assert adapter.reconcile(request("seed")).payload["result"]["accepted"] is True
        return second
    result = adapter.consistent_read(read)
    counts = Counter(key for _, key in client.reads)
    assert counts == {GUARD_KEY: 2, "behavior:v1:row:budget": 1,
                      "behavior:v1:row:missing": 1, "behavior:v1:receipt:seed": 1}
    assert len(client.submissions) == posts
    result["budget"].payload["data"]["remaining"] = 999
    assert adapter.consistent_read(lambda: adapter.read_snapshot(("budget",)))["budget"].payload["data"]["remaining"] == 1
    assert Counter(key for _, key in client.reads)[GUARD_KEY] == 4


def test_final_guard_catches_real_other_writer_without_retry(store):
    from concurrent.futures import ThreadPoolExecutor
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    client.reads.clear()
    with ThreadPoolExecutor(max_workers=1) as writer:
        def read():
            value = adapter.read_snapshot(("budget",))
            writer.submit(other.transact, request("spend", ("budget",)), lambda _: changes(
                write("budget", {"remaining": 0}, "replace"))).result()
            assert adapter.read_snapshot(("budget",)) == value
            return value
        with pytest.raises(Problem) as caught:
            adapter.consistent_read(read)
    assert code(caught) == "BEHAVIOR_CONFLICT"
    # The moved guard re-checks the row the callback read; the changed budget is refused.
    assert Counter(key for _, key in client.reads) == {GUARD_KEY: 2, "behavior:v1:row:budget": 2}
    assert adapter.read_snapshot(("budget",))["budget"].payload["data"]["remaining"] == 0


@pytest.mark.parametrize("action", ["transact", "initialize", "submit", "nested"])
def test_callback_cannot_write_dispatch_or_hide_violation(store, action):
    adapter, client = store
    seed(adapter)
    before = copy.deepcopy(client.backing.rows)
    callbacks = {
        "transact": lambda: adapter.transact(request("illegal"), lambda _: changes()),
        "initialize": adapter.initialize,
        "submit": lambda: adapter._submit([], None),
        "nested": lambda: adapter.consistent_read(lambda: adapter.read_snapshot(())),
    }
    def read():
        with pytest.raises(Problem):
            callbacks[action]()
        return "must not escape a failed scope"
    with pytest.raises(Problem):
        adapter.consistent_read(read)
    assert client.backing.rows == before
    assert adapter.read_snapshot(("budget",))["budget"] is not None


@pytest.mark.parametrize("fault", ["unavailable", "material", "receipt-missing", "receipt-tampered"])
def test_failed_read_cannot_be_hidden_or_leak_to_next_call(store, fault):
    from azure.core.exceptions import ServiceRequestError
    adapter, client = store
    seed(adapter)
    if fault in {"material", "receipt-tampered", "receipt-missing"}:
        k = "behavior:v1:row:budget" if fault == "material" else "behavior:v1:receipt:seed"
        entity = client.backing.rows[(SCOPE.table_name, SCOPE.partition_key, k)]
        if fault == "receipt-missing":
            del client.backing.rows[(SCOPE.table_name, SCOPE.partition_key, k)]
        else:
            payload = json.loads(entity["Payload"])
            if fault == "material":
                payload["material"] = {"data": "eA==", "sha256": "a" * 64}
            else:
                payload["request"]["request_digest"] = "0" * 64
            entity["Payload"] = json.dumps(payload).encode()
    original = client.get_entity
    if fault == "unavailable":
        def unavailable(**kwargs):
            if kwargs["row_key"] == "behavior:v1:row:budget":
                raise ServiceRequestError("synthetic read failure")
            return original(**kwargs)
        client.get_entity = unavailable
    with pytest.raises(Problem):
        adapter.consistent_read(lambda: adapter.reconcile(request("seed")) if fault.startswith("receipt") else
                                adapter.read_snapshot(("budget",)))
    client.get_entity = original
    # No callback state or poisoned cache survives either a read exception or
    # user code raising after it has successfully fetched a row.
    with pytest.raises(ValueError):
        def raises():
            adapter.read_snapshot(("grant",))
            raise ValueError("synthetic callback failure")
        adapter.consistent_read(raises)
    assert adapter.consistent_read(lambda: adapter.read_snapshot(("grant",)))["grant"] is not None


def test_caught_transport_failure_poison_still_rejects_callback_result(store):
    from azure.core.exceptions import ServiceRequestError
    adapter, client = store
    original = client.get_entity
    def unavailable(**kwargs):
        if kwargs["row_key"] != GUARD_KEY:
            raise ServiceRequestError("synthetic unavailable")
        return original(**kwargs)
    client.get_entity = unavailable
    def read():
        with pytest.raises(Problem):
            adapter.read_snapshot(("missing",))
        return None
    with pytest.raises(Problem) as failure:
        adapter.consistent_read(read)
    assert code(failure) == "BEHAVIOR_READ_SCOPE_INVALID"


def test_aggregate_scope_bound_includes_absence_and_receipts(store):
    adapter, client = store
    def bounded():
        for index in range(98):
            assert adapter.read_snapshot((f"key-{index}",))[f"key-{index}"] is None
    adapter.consistent_read(bounded)
    client.reads.clear()
    def excessive():
        bounded()
        adapter._receipt(request("one-more"))
    with pytest.raises(Problem) as failure:
        adapter.consistent_read(excessive)
    assert code(failure) == "BEHAVIOR_READ_SCOPE_LIMIT"
    assert len(client.reads) == 99


def test_collection_read_can_exceed_transaction_key_count_without_writes(store):
    adapter, client = store
    before = len(client.submissions)
    def read():
        return [adapter.read_snapshot((f"key-{index}",))[f"key-{index}"] for index in range(120)]
    assert adapter.consistent_read(read, max_keys=None) == [None] * 120
    assert len(client.submissions) == before
    with pytest.raises(Problem):
        adapter.consistent_read(read)


def test_collection_absence_keys_still_consume_bounded_cache_memory():
    adapter = BehaviorStore(FakeTable(), SCOPE.model_copy(update={"max_batch_bytes": 1024}))
    adapter.initialize()
    def read():
        for index in range(120):
            adapter.read_snapshot((f"missing-{index:03}-" + "x"*45,))
    with pytest.raises(Problem) as error:
        adapter.consistent_read(read, max_keys=None)
    assert code(error) == "BEHAVIOR_READ_SCOPE_LIMIT"


def test_aggregate_typed_payload_bytes_use_existing_authority_ceiling():
    client = FakeTable()
    adapter = BehaviorStore(client, SCOPE.model_copy(update={"max_batch_bytes": 1024}))
    adapter.initialize()
    adapter.transact(request("seed-large"), lambda _: changes(
        write("first", {"text": "a" * 550}), write("second", {"text": "b" * 550})))
    assert adapter.read_snapshot(("first", "second"))["second"] is not None
    with pytest.raises(Problem) as failure:
        adapter.consistent_read(lambda: adapter.read_snapshot(("first", "second")))
    assert code(failure) == "BEHAVIOR_READ_SCOPE_LIMIT"


@pytest.mark.parametrize("kind", ["coroutine", "generator"])
def test_async_or_lazy_callback_is_never_treated_as_finished(store, kind):
    adapter, _ = store
    async def coroutine():
        return adapter.read_snapshot(())
    def generator():
        yield adapter.read_snapshot(())
    with pytest.raises(Problem) as failure:
        adapter.consistent_read(coroutine if kind == "coroutine" else generator)
    assert code(failure) == "BEHAVIOR_READ_SCOPE_INVALID"


def test_same_store_threads_get_separate_scopes_and_copied_context_is_rejected(store):
    adapter, client = store
    seed(adapter)
    barrier = threading.Barrier(2)
    client.reads.clear()
    def callback():
        first = adapter.read_snapshot(("budget",))
        barrier.wait(timeout=5)
        assert adapter.read_snapshot(("budget",)) == first
        return first
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(adapter.consistent_read, callback) for _ in range(2)]
        assert futures[0].result() == futures[1].result()
        assert Counter(key for _, key in client.reads) == {GUARD_KEY: 4, "behavior:v1:row:budget": 2}
        def illegal():
            ctx = copy_context()
            with pytest.raises(Problem):
                pool.submit(ctx.run, adapter.read_snapshot, ("budget",)).result()
        with pytest.raises(Problem):
            adapter.consistent_read(illegal)


def test_copied_context_cannot_resurrect_a_closed_scope(store):
    adapter, _ = store
    saved = []
    adapter.consistent_read(lambda: saved.append(copy_context()))
    with pytest.raises(Problem):
        saved[0].run(adapter.read_snapshot, ())
    assert adapter.consistent_read(lambda: adapter.read_snapshot(())) == {}


def test_task_contexts_do_not_share_view_and_child_task_cannot_inherit_it(store):
    adapter, client = store
    seed(adapter)
    client.reads.clear()
    async def independent():
        return adapter.consistent_read(lambda: adapter.read_snapshot(("budget",)))
    async def main():
        values = await asyncio.gather(independent(), independent())
        assert values[0] == values[1]
        child = []
        async def escaped():
            adapter.read_snapshot(("budget",))
        def callback():
            child.append(asyncio.create_task(escaped()))
        adapter.consistent_read(callback)
        with pytest.raises(Problem):
            await child[0]
        return await independent()
    assert asyncio.run(main())["budget"] is not None
    assert Counter(key for _, key in client.reads) == {GUARD_KEY: 8, "behavior:v1:row:budget": 3}


def test_eager_child_task_cannot_use_parent_active_scope(store):
    adapter, _ = store
    child = []
    async def escaped():
        adapter.read_snapshot(())
    async def main():
        def callback():
            child.append(asyncio.Task(escaped(), loop=asyncio.get_running_loop(), eager_start=True))
        with pytest.raises(Problem):
            adapter.consistent_read(callback)
        with pytest.raises(Problem):
            await child[0]
        assert adapter.consistent_read(lambda: adapter.read_snapshot(())) == {}
    asyncio.run(main())


def test_other_store_dispatch_inside_callback_is_blocked_before_storage(store):
    adapter, client = store
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    before = copy.deepcopy(client.backing.rows)
    with pytest.raises(Problem):
        adapter.consistent_read(lambda: other.transact(request("forbidden"), lambda _: changes()))
    assert client.backing.rows == before


def test_late_commit_cas_is_still_physical_and_view_never_memoizes_ledger(store):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    adapter.consistent_read(lambda: adapter.read_snapshot(("budget",)))
    client.before_submit = lambda: other.transact(request("concurrent", ("budget",)), lambda _: changes(
        write("budget", {"remaining": 0}, "replace")))
    with pytest.raises(Problem) as failure:
        adapter.transact(request("original", ("budget",)), lambda _: changes(
            write("budget", {"remaining": 0}, "replace")))
    assert code(failure) == "BEHAVIOR_CONFLICT"
    assert adapter._receipt(request("original", ("budget",))) is None


@pytest.mark.parametrize("fault", ["epoch", "installation", "same-config-image", "guard-etag", "guard-missing"])
def test_migration_view_rechecks_physical_guard_epoch_and_full_installation(fault):
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
        adapter.consistent_read(read)


def test_migration_inventory_is_never_a_cached_view_or_scope_side_effect():
    from shared_context.behavior_migration import MigrationStore
    adapter = MigrationStore(FakeTable(), SCOPE)
    adapter.initialize()
    with pytest.raises(Problem) as failure:
        adapter.consistent_read(adapter.read_inventory)
    assert code(failure) == "BEHAVIOR_READ_SCOPE_INVALID"
