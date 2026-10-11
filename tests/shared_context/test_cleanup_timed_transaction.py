"""Same EGT, moved full validators, no renewable send permission."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context, Context

import pytest

from shared_context.behavior_migration import MigrationStore
from shared_context.behavior_store import ControlChanges, ControlWrite
from shared_context.errors import Problem
from test_behavior_migration_store import store_with_row
from test_behavior_store import request, FakeTable, SCOPE


def test_full_original_hooks_run_before_time_sensitive_decision_once():
    store, _ = store_with_row()
    now, order = [0], []
    def before_send():
        now[0] += 3
        order.append("original-before-send")
    def inherited():
        now[0] += 13
        order.append("full-inherited")
    def decide(rows):
        assert rows["budget"].payload["data"]["remaining"] == 7
        order.append("decide")
        return ControlChanges(writes=(), result={"gated_at": now[0], "not_after": now[0]+2})
    def final():
        order.append("last-clock")
        assert now[0] == 16
    command = request("original-once", ("budget",))
    def operation():
        return store.transact(command, decide, before_send=before_send, decision_final_check=final)
    receipt = store.validated_writes(operation, inherited)
    assert receipt.payload["result"] == {"gated_at": 16, "not_after": 18}
    assert order == ["original-before-send", "full-inherited", "decide", "last-clock"]
    assert store.validated_writes(operation, inherited) == receipt
    assert order == ["original-before-send", "full-inherited", "decide", "last-clock"]


@pytest.mark.parametrize("stage", ["full-validator", "after-decision", "actual-cas"])
def test_guard_change_never_promotes_a_stale_timed_decision(stage):
    store, table = store_with_row()
    competitor = MigrationStore(FakeTable(table.backing), SCOPE)
    def write():
        return Context().run(competitor.transact, request("other-write", ("budget",)),
            lambda _: ControlChanges(writes=(ControlWrite(row_id="budget", mode="replace", data={"remaining": 6}),), result={}))
    def full():
        if stage == "full-validator":
            write()
    def decide(_):
        if stage == "after-decision":
            write()
        return ControlChanges(result={"timed": True})
    if stage == "actual-cas":
        table.before_submit = write
    command = request("timed-original", ("budget",))
    with pytest.raises(Problem) as rejected:
        store.validated_writes(lambda: store.transact(command, decide, decision_final_check=lambda: None), full)
    assert rejected.value.body["error"]["code"] == "BEHAVIOR_CONFLICT"
    assert store._receipt(command) is None


def test_deadline_check_is_after_last_physical_read_and_ordinary_hook_is_not_moved(monkeypatch):
    store, _ = store_with_row()
    order, now = [], [0]
    guard = store._guard
    def read():
        import sys
        value = guard()
        if sys._getframe(1).f_code.co_name == "final":
            order.append("physical-guard")
            now[0] = 3
        return value
    monkeypatch.setattr(store, "_guard", read)
    def final():
        order.append("deadline")
        if now[0] >= 2:
            raise Problem(403, "TIMED_EXPIRED", "synthetic final expiry")
    with pytest.raises(Problem):
        store.validated_writes(lambda: store.transact(request("late"),
            lambda _: ControlChanges(result={}), decision_final_check=final), lambda: None)
    assert order == ["physical-guard", "deadline"]
    order.clear()
    def decision(_):
        order.append("normal-decision")
        return ControlChanges(result={})
    store.validated_writes(lambda: store.transact(request("normal"), decision,
        before_send=lambda: order.append("normal-before-send")), lambda: order.append("normal-inherited"))
    assert order == ["normal-decision", "normal-before-send", "normal-inherited"]


@pytest.mark.parametrize("escape", ["thread", "task", "closed", "another-store"])
def test_timed_callbacks_cannot_borrow_another_execution_scope(escape):
    store, table = store_with_row()
    before = store.read_inventory()
    def transact():
        return store.transact(request("escape"), lambda _: ControlChanges(result={}),
                              decision_final_check=lambda: None)
    if escape == "thread":
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(Problem):
                store.validated_writes(lambda: pool.submit(copy_context().run, transact).result(), lambda: None)
    elif escape == "task":
        async def main():
            children = []
            async def child():
                transact()
            store.validated_writes(lambda: children.append(asyncio.Task(
                child(), loop=asyncio.get_running_loop(), eager_start=True)), lambda: None)
            with pytest.raises(Problem):
                await children[0]
        asyncio.run(main())
    elif escape == "closed":
        contexts = []
        store.validated_writes(lambda: contexts.append(copy_context()), lambda: None)
        with pytest.raises(Problem):
            contexts[0].run(transact)
    else:
        another = MigrationStore(FakeTable(table.backing), SCOPE)
        with pytest.raises(Problem):
            store.validated_writes(lambda: another.transact(request("other-store"),
                lambda _: ControlChanges(result={}), decision_final_check=lambda: None), lambda: None)
    assert store.read_inventory() == before


def test_timed_callback_requires_owned_validation_and_cleans_exceptions():
    store, _ = store_with_row()
    command = request("unsupported")
    with pytest.raises(Problem):
        store.transact(command, lambda _: ControlChanges(result={}), decision_final_check=lambda: None)
    def denied():
        raise Problem(403, "VALIDATOR_DENIED", "synthetic check")
    with pytest.raises(Problem):
        store.validated_writes(lambda: store.transact(command, lambda _: ControlChanges(result={}),
            decision_final_check=lambda: None), denied)
    assert store._receipt(command) is None
    store.transact(request("normal-after-error"), lambda _: ControlChanges(result={}))
