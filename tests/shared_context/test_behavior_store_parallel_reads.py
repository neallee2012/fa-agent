"""Independent point reads of one guard-bracketed batch run in parallel; the bracket and first error do not change."""
import threading
import time

import pytest
from azure.core.exceptions import ServiceRequestError

from shared_context import behavior_store
from shared_context.behavior_store import GUARD_KEY, BehaviorStore
from shared_context.errors import Problem
from test_behavior_store import FakeTable, NativeEntity, SCOPE, changes, code, request, write


class SlowTable(FakeTable):
    """Each GET waits like a network round trip; records how many were in flight together."""

    def __init__(self, backing=None, delay=0.03):
        super().__init__(backing)
        self.delay, self.lock, self.inflight, self.peak = delay, threading.Lock(), 0, 0
        self.failures = {}

    def get_entity(self, *, partition_key, row_key, raw_request_hook, **kwargs):
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            time.sleep(self.failures.get(row_key, (self.delay, None))[0])
            failure = self.failures.get(row_key, (None, None))[1]
            if failure == "malformed":
                raw_request_hook(type("W", (), {"http_request": type("R", (), {
                    "method": "GET", "url": self.url + "/Behavior", "body": None})()})())
                return NativeEntity({"PartitionKey": partition_key, "RowKey": row_key, "Payload": b"{"}, 'W/"bad"')
            if failure is not None:
                raw_request_hook(type("W", (), {"http_request": type("R", (), {
                    "method": "GET", "url": self.url + "/Behavior", "body": None})()})())
                raise failure
            return super().get_entity(partition_key=partition_key, row_key=row_key,
                                      raw_request_hook=raw_request_hook, **kwargs)
        finally:
            with self.lock:
                self.inflight -= 1


@pytest.fixture
def slow():
    client = SlowTable()
    adapter = BehaviorStore(client, SCOPE)
    adapter.initialize()
    keys = tuple(f"row-{number:02}" for number in range(20))
    adapter.transact(request("seed-many"), lambda _: changes(*(write(key, {"n": n}) for n, key in enumerate(keys))))
    return adapter, client, keys


def test_one_batch_reads_its_keys_in_parallel_within_the_bound(slow):
    adapter, client, keys = slow
    client.peak = 0
    started = time.perf_counter()
    rows = adapter.read_snapshot(keys)
    elapsed = time.perf_counter() - started
    assert [rows[key].payload["data"]["n"] for key in keys] == list(range(20))
    assert 1 < client.peak <= behavior_store.MAX_PARALLEL_READS
    # 20 sequential GETs plus two guard reads would take at least 22 round trips.
    assert elapsed < 22 * client.delay


def test_same_batch_inside_a_read_view_is_parallel_and_still_cached(slow):
    adapter, client, keys = slow
    client.peak = 0

    def read():
        first = adapter.read_snapshot(keys)
        reads = len(client.reads)
        assert adapter.read_snapshot(keys) == first and len(client.reads) == reads
        return first

    assert adapter.consistent_read(read)[keys[0]].payload["data"]["n"] == 0
    assert client.peak > 1


@pytest.mark.parametrize("path", ["snapshot", "transact"])
def test_a_row_changed_during_parallel_reads_is_still_a_conflict(slow, path):
    adapter, client, keys = slow
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    physical = adapter._read_physical
    changed = []

    def racing(key, model):
        value = physical(key, model)
        if key == "behavior:v1:row:" + keys[3] and not changed:
            changed.append(True)
            other.transact(request("competing", (keys[3],)), lambda _: changes(write(keys[3], {"n": -1}, "replace")))
        return value

    adapter._read_physical = racing
    decided = []
    with pytest.raises(Problem) as stale:
        if path == "snapshot":
            adapter.read_snapshot(keys)
        else:
            adapter.transact(request("needs-current", keys), lambda rows: decided.append(rows) or changes())
    assert code(stale) == "BEHAVIOR_CONFLICT" and changed and not decided
    assert adapter.read_snapshot((keys[3],))[keys[3]].payload["data"]["n"] == -1


def test_first_error_in_key_order_is_reported_even_if_a_later_key_fails_first(slow):
    adapter, client, keys = slow
    client.failures["behavior:v1:row:" + keys[2]] = (0.1, ServiceRequestError("slow earlier failure"))
    client.failures["behavior:v1:row:" + keys[9]] = (0.0, "malformed")
    with pytest.raises(Problem) as first:
        adapter.read_snapshot(keys)
    assert first.value.status == 503 and code(first) == "BEHAVIOR_READ_FAILED"
    client.failures.pop("behavior:v1:row:" + keys[2])
    with pytest.raises(Problem) as later:
        adapter.read_snapshot(keys)
    assert code(later) == "BEHAVIOR_PROTOCOL_ERROR"


def test_guard_is_never_read_in_parallel_with_the_rows(slow):
    adapter, client, keys = slow
    order, physical = [], adapter._read_physical

    def tracked(key, model):
        order.append(("start", key))
        value = physical(key, model)
        order.append(("end", key))
        return value

    adapter._read_physical = tracked
    adapter.read_snapshot(keys)
    guards = [index for index, (kind, key) in enumerate(order) if key == GUARD_KEY]
    rows = [index for index, (kind, key) in enumerate(order) if key != GUARD_KEY]
    assert order[guards[0]] == ("start", GUARD_KEY) and order[guards[0] + 1] == ("end", GUARD_KEY)
    assert guards[1] < min(rows) and max(rows) < guards[2]
