"""The extra recovery fence can deny, never replace original writer permission."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from shared_context.behavior_migration import MigrationStore
from shared_context.behavior_store import ControlChanges
from shared_context.errors import Problem
from test_behavior_migration_store import store_with_row
from test_behavior_store import request, FakeTable, SCOPE


def test_composes_original_hook_and_checks_time_after_last_wait():
    store, table = store_with_row()
    clock, order = [0], []
    def original():
        order.append("original")
        store.read_snapshot(("budget",))
        clock[0] = 10
    def last():
        order.append("extra-read")
        store.read_snapshot(("budget",))
        order.append("extra-time")
        if clock[0] >= 10:
            raise Problem(403, "ORIGINAL_WINDOW_EXPIRED", "synthetic last wire boundary")
    before = store.read_inventory()
    with pytest.raises(Problem) as denied:
        store.validated_writes(
            lambda: store.transact(request("original-only"), lambda _: ControlChanges(result={}), before_send=original), last)
    assert denied.value.body["error"]["code"] == "ORIGINAL_WINDOW_EXPIRED"
    assert order == ["original", "extra-read", "extra-time"]
    assert store.read_inventory() == before
    store.transact(request("ordinary"), lambda _: ControlChanges(result={}))
    assert order == ["original", "extra-read", "extra-time"]


@pytest.mark.parametrize("kind", ["thread", "task", "expired", "other-store"])
def test_copied_context_or_different_store_cannot_borrow_validation(kind):
    store, table = store_with_row()
    before = store.read_inventory()
    checks, captured = [], []
    def write():
        return store.transact(request("must-not-send"), lambda _: ControlChanges(result={}))
    if kind == "thread":
        with ThreadPoolExecutor(max_workers=1) as pool:
            def operation():
                return pool.submit(copy_context().run, write).result()
            with pytest.raises(Problem):
                store.validated_writes(operation, lambda: checks.append(True))
    elif kind == "task":
        async def main():
            async def escaped():
                write()
            def operation():
                captured.append(asyncio.Task(escaped(), loop=asyncio.get_running_loop(), eager_start=True))
            store.validated_writes(operation, lambda: checks.append(True))
            with pytest.raises(Problem):
                await captured[0]
        asyncio.run(main())
    elif kind == "expired":
        store.validated_writes(lambda: captured.append(copy_context()), lambda: checks.append(True))
        with pytest.raises(Problem):
            captured[0].run(write)
    else:
        another = MigrationStore(FakeTable(table.backing), SCOPE)
        with pytest.raises(Problem):
            store.validated_writes(
                lambda: another.transact(request("another-store"), lambda _: ControlChanges(result={})),
                lambda: checks.append(True))
    assert checks == [] and store.read_inventory() == before


def test_exception_nested_and_lazy_results_leave_no_validator_behind():
    store, _ = store_with_row()
    checks = []
    def check():
        checks.append(True)
    def failed():
        raise ValueError("synthetic synchronous failure")
    with pytest.raises(ValueError):
        store.validated_writes(failed, check)
    with pytest.raises(Problem):
        store.validated_writes(lambda: store.validated_writes(lambda: None, check), check)
    async def later():
        store.transact(request("lazy"), lambda _: ControlChanges(result={}))
    with pytest.raises(Problem):
        store.validated_writes(later, check)
    store.transact(request("ordinary-after-errors"), lambda _: ControlChanges(result={}))
    assert checks == []
