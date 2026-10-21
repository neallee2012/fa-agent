"""Offline SDK-shaped and real-SDK/fake-transport tests, never Azure evidence."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import threading
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit
from unittest.mock import Mock

import pytest
from azure.core import MatchConditions
from azure.core.credentials import AccessToken
from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError
from azure.core.pipeline.transport import HttpResponse, HttpTransport
from azure.core.utils import CaseInsensitiveDict
from azure.data.tables import TableClient, TableTransactionError, UpdateMode
from pydantic import ValidationError

from shared_context.behavior_store import (
    GUARD_KEY, MAX_INLINE_BYTES, AuthorityScope, BehaviorStore, ControlChanges,
    ControlRequest, ControlWrite, InlineMaterial,
)
from shared_context.errors import Problem

URL = "https://synthetic.table.core.windows.net"
SCOPE = AuthorityScope(authority_domain="team-one", table_name="Behavior", partition_key="control-one")


def request(name="request-one", reads=()):
    return ControlRequest(
        request_id=name, request_digest=hashlib.sha256(name.encode()).hexdigest(),
        read_set=reads,
    )


def changes(*writes, result=None):
    return ControlChanges(writes=tuple(writes), result={"accepted": True} if result is None else result)


def write(row, data, mode="create", material=None):
    return ControlWrite(row_id=row, mode=mode, data=data, material=material)


def code(error):
    return error.value.body["error"]["code"]


class NativeEntity(dict):
    def __init__(self, entity, etag):
        super().__init__(entity)
        self.metadata = {"etag": etag}


def conflict(status):
    error = TableTransactionError(message="0:synthetic conflict")
    error.status_code = status
    return error


class Backing:
    """One fake EGT step; apply holds a lock, so a batch lands atomically like a Table transaction."""

    def __init__(self):
        self.rows = {}
        self.sequence = 0
        self.lock = threading.Lock()

    # SQLiteBacking (test_behavior_workers) pickles the rows between processes; a lock is per process, so it is rebuilt.
    def __getstate__(self):
        return {key: value for key, value in self.__dict__.items() if key != "lock"}

    def __setstate__(self, state):
        self.__dict__.update(state, lock=threading.Lock())

    def get(self, table, partition, row):
        value = self.rows.get((table, partition, row))
        if value is None:
            error = ResourceNotFoundError(message="synthetic missing")
            error.error_code = "ResourceNotFound"
            raise error
        return copy.deepcopy(value)

    def apply(self, table, operations):
        with self.lock:
            return self._apply(table, operations)

    def _apply(self, table, operations):
        candidate = copy.deepcopy(self.rows)
        sequence, results, seen, partition = self.sequence, [], set(), None
        for operation in operations:
            mode, entity, *options = operation
            key = table, entity["PartitionKey"], entity["RowKey"]
            if key in seen or partition is not None and key[1] != partition:
                raise conflict(400)
            seen.add(key)
            partition = key[1]
            if mode == "create":
                if key in candidate:
                    raise conflict(409)
            else:
                assert mode == "update"
                assert options[0]["mode"] == UpdateMode.REPLACE
                assert options[0]["match_condition"] == MatchConditions.IfNotModified
                if key not in candidate or candidate[key].metadata["etag"] != options[0]["etag"]:
                    raise conflict(412)
            sequence += 1
            etag = f'W/"fake-{sequence}"'
            candidate[key] = NativeEntity(entity, etag)
            results.append({"etag": etag})
        self.rows, self.sequence = candidate, sequence
        return results


def before_closing_guard(monkeypatch, store, act):
    """Call act(keys) in a reading thread just before its read's closing guard check.

    keys are the predicate keys that read re-checks after an unrelated guard advance (since #197); inside a
    consistent_read view the check runs when the view closes, so a rewrite here is still a real conflict.
    """
    import threading
    local = threading.local()
    read, guard = store._read_predicates, store._guard
    def tracked(keys, *args, **kwargs):
        stack = local.__dict__.setdefault("stack", [])
        stack.append(tuple(keys))
        try:
            return read(keys, *args, **kwargs)
        finally:
            stack.pop()
    def closing(*args, **kwargs):
        stack = getattr(local, "stack", None)
        if stack:
            act(stack[-1])
        return guard(*args, **kwargs)
    monkeypatch.setattr(store, "_read_predicates", tracked)
    monkeypatch.setattr(store, "_guard", closing)


def stored_row(backing, keys):
    """The first stored row among the keys a read just fetched, if any (absent keys cannot be rewritten)."""
    rows = {"behavior:v1:row:" + item for item in keys or ()}
    return next((key for key in backing.rows if key[2] in rows), None)


def rewrite_key(backing, keys):
    """A concurrent writer that rewrote one row the read just fetched: same content, a new ETag."""
    key = stored_row(backing, keys)
    backing.sequence += 1
    backing.rows[key] = NativeEntity(dict(backing.rows[key]), f'W/"fake-{backing.sequence}"')


def rewrite_read_rows(backing):
    """A concurrent writer that rewrote the data rows a read used: same content, new ETags.

    Since #197 a read accepts an unrelated guard advance; only a changed key it read still returns 412.
    """
    for key, entity in list(backing.rows.items()):
        if key[2].startswith("behavior:v1:row:"):
            backing.sequence += 1
            backing.rows[key] = NativeEntity(dict(entity), f'W/"fake-{backing.sequence}"')


class FakeTable:
    """TableClient-shaped fake that invokes the real public request hook."""

    url = URL
    table_name = "Behavior"

    def __init__(self, backing=None):
        self.backing = backing or Backing()
        self.submissions, self.reads = [], []
        self.before_submit = self.after_get = None
        self.failure = None
        self.repeat_request = False
        self.body_size = 1024

    def get_entity(self, *, partition_key, row_key, raw_request_hook, **kwargs):
        assert kwargs["retry_total"] == kwargs["retry_read"] == kwargs["retry_connect"] == 0
        self.reads.append((partition_key, row_key))
        raw_request_hook(SimpleNamespace(http_request=SimpleNamespace(
            method="GET", url=self.url + "/Behavior", body=None,
        )))
        entity = self.backing.get(self.table_name, partition_key, row_key)
        if self.after_get:
            callback, self.after_get = self.after_get, None
            callback(row_key)
        return entity

    def submit_transaction(self, operations, *, raw_request_hook, **kwargs):
        assert kwargs["retry_total"] == kwargs["retry_status"] == 0
        assert kwargs["retry_read"] == kwargs["retry_connect"] == 0
        assert kwargs["retry_to_secondary"] is False and kwargs["logging_enable"] is False
        self.submissions.append(copy.deepcopy(operations))
        wire = SimpleNamespace(http_request=SimpleNamespace(
            method="POST", url=self.url + "/$batch", body=b"x" * self.body_size,
        ))
        raw_request_hook(wire)
        if self.before_submit:
            callback, self.before_submit = self.before_submit, None
            callback()
        if self.failure == "before":
            raise ServiceRequestError("synthetic pre-ack failure")
        result = self.backing.apply(self.table_name, operations)
        if self.repeat_request:
            raw_request_hook(wire)
        if self.failure == "after":
            raise ServiceRequestError("synthetic lost ack")
        if self.failure == "malformed":
            return [{"etag": "*"}]
        return result


@pytest.fixture
def store():
    client = FakeTable()
    adapter = BehaviorStore(client, SCOPE)
    adapter.initialize()
    return adapter, client


def seed(adapter):
    return adapter.transact(request("seed"), lambda _: changes(
        write("grant", {"allowed": True}), write("budget", {"remaining": 1}),
        write("owner", {"epoch": 1}),
    ))


def spend(snapshot):
    assert snapshot["grant"].payload["data"]["allowed"]
    assert snapshot["owner"].payload["data"]["epoch"] == 1
    remaining = snapshot["budget"].payload["data"]["remaining"]
    if remaining != 1:
        raise Problem(403, "BUDGET_DENIED", "synthetic budget exhausted")
    return changes(
        write("budget", {"remaining": remaining - 1}, "replace"),
        write("work", {"state": "accepted"}),
        write("obligation", {"state": "pending"}),
        write("wake", {"state": "due"}),
    )


def test_public_snapshot_is_bounded_guarded_read_only_and_returns_copies(store):
    adapter, client = store
    seed(adapter)
    writes_before = len(client.submissions)
    client.reads.clear()
    snapshot = adapter.read_snapshot(("grant", "budget", "missing"))
    assert snapshot["grant"].payload["data"] == {"allowed": True}
    assert snapshot["budget"].etag.startswith('W/"fake-')
    assert snapshot["missing"] is None
    assert client.reads == [
        (SCOPE.partition_key, GUARD_KEY), (SCOPE.partition_key, "behavior:v1:row:grant"),
        (SCOPE.partition_key, "behavior:v1:row:budget"), (SCOPE.partition_key, "behavior:v1:row:missing"),
        (SCOPE.partition_key, GUARD_KEY),
    ]
    assert len(client.submissions) == writes_before
    snapshot["budget"].payload["data"]["remaining"] = 999
    assert adapter.read_snapshot(("budget",))["budget"].payload["data"]["remaining"] == 1
    assert len(client.submissions) == writes_before


@pytest.mark.parametrize("keys", [
    ("grant", "grant"), ("",), ("../outside",), (1,), "grant", ["grant"],
    tuple(f"row-{index}" for index in range(99)),
])
def test_public_snapshot_rejects_invalid_keys_before_any_storage_read(store, keys):
    adapter, client = store
    before = len(client.reads), len(client.submissions)
    with pytest.raises(Problem) as invalid:
        adapter.read_snapshot(keys)
    assert code(invalid) == "BEHAVIOR_INVALID_DATA"
    assert (len(client.reads), len(client.submissions)) == before


def test_public_snapshot_rejects_mixed_guard_generation_without_retry(store):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)

    def concurrent_write(key):
        if key == GUARD_KEY:
            client.after_get = concurrent_write
            return
        assert key == "behavior:v1:row:grant"
        other.transact(request("revoke", ("grant",)), lambda _: changes(
            write("grant", {"allowed": False}, "replace"),
        ))

    client.reads.clear()
    before = len(client.submissions)
    client.after_get = concurrent_write
    with pytest.raises(Problem) as changed:
        adapter.read_snapshot(("grant",))
    assert code(changed) == "BEHAVIOR_CONFLICT"
    assert len(client.reads) == 4 and len(client.submissions) == before


def test_public_snapshot_requires_initialized_authority():
    client = FakeTable()
    adapter = BehaviorStore(client, SCOPE)
    with pytest.raises(Problem) as missing:
        adapter.read_snapshot(())
    assert code(missing) == "BEHAVIOR_NOT_INITIALIZED"
    assert not client.submissions


def test_public_snapshot_accepts_exactly_98_unique_keys_without_writes(store):
    adapter, client = store
    client.reads.clear()
    before = len(client.submissions)
    keys = tuple(f"row-{index}" for index in range(98))
    snapshot = adapter.read_snapshot(keys)
    assert tuple(snapshot) == keys and all(value is None for value in snapshot.values())
    assert len(client.reads) == 100 and len(client.submissions) == before


def test_public_snapshot_rejects_mismatched_logical_row_identity(store):
    adapter, client = store
    seed(adapter)
    entity = client.backing.rows[(SCOPE.table_name, SCOPE.partition_key, "behavior:v1:row:grant")]
    payload = json.loads(entity["Payload"])
    payload["row_id"] = "different"
    entity["Payload"] = json.dumps(payload).encode()
    with pytest.raises(Problem) as invalid:
        adapter.read_snapshot(("grant",))
    assert code(invalid) == "BEHAVIOR_PROTOCOL_ERROR"


def test_initialize_create_only_never_creates_table_and_reuses_bound_guard(store):
    adapter, client = store
    assert [op[0] for op in client.submissions[0]] == ["create"]
    first = adapter.initialize()
    assert first.payload["revision"] == 0 and first.etag
    assert len(client.submissions) == 1


def test_authority_configuration_and_persisted_binding_cannot_be_mixed(store):
    adapter, client = store
    with pytest.raises(Problem) as wrong_table:
        BehaviorStore(client, SCOPE.model_copy(update={"table_name": "Other"}))
    assert code(wrong_table) == "BEHAVIOR_AUTHORITY_MISMATCH"
    for update in ({"authority_domain": "other"},):
        other = BehaviorStore(FakeTable(client.backing), SCOPE.model_copy(update=update))
        with pytest.raises(Problem) as wrong_domain:
            other.initialize()
        assert code(wrong_domain) == "BEHAVIOR_AUTHORITY_MISMATCH"
    client.table_name = "Changed"
    with pytest.raises(Problem):
        adapter.transact(request(), lambda _: changes())


@pytest.mark.parametrize("change", [
    {"max_operations": 101}, {"max_batch_bytes": 4 * 1024 * 1024 + 1},
    {"max_payload_bytes": 48 * 1024 + 1}, {"partition_key": "../other"},
])
def test_scope_limits_reject_invalid_or_constructed_models(change):
    with pytest.raises(ValidationError):
        AuthorityScope(**{**SCOPE.model_dump(), **change})
    with pytest.raises(Problem):
        BehaviorStore(FakeTable(), SCOPE.model_copy(update=change))


def test_guard_predicates_receipt_and_all_mutations_share_one_atomic_batch(store):
    adapter, client = store
    seed(adapter)
    before = client.backing.get("Behavior", SCOPE.partition_key, GUARD_KEY)
    result = adapter.transact(request(reads=("grant", "budget", "owner")), spend)
    operations = client.submissions[-1]
    assert len(operations) == 6
    assert operations[0][2] == {
        "mode": UpdateMode.REPLACE, "etag": before.metadata["etag"],
        "match_condition": MatchConditions.IfNotModified,
    }
    assert [op[0] for op in operations] == ["update", "create", "update", "create", "create", "create"]
    assert {op[1]["PartitionKey"] for op in operations} == {SCOPE.partition_key}
    assert result.payload["guard_revision"] == 2
    assert result.payload["result"] == {"accepted": True}
    assert result.etag != before.metadata["etag"]


def test_concurrent_authority_change_between_reads_rejects_inconsistent_snapshot(store):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)

    def concurrent(key):
        if key != "behavior:v1:row:budget":
            client.after_get = concurrent
            return
        other.transact(request("concurrent", ("budget",)), lambda _: changes(
            write("budget", {"remaining": 0}, "replace")))

    client.after_get = concurrent
    decide = Mock()
    count = len(client.submissions)
    with pytest.raises(Problem) as stale:
        adapter.transact(request(reads=("grant", "budget", "owner")), decide)
    assert code(stale) == "BEHAVIOR_CONFLICT"
    assert len(client.submissions) == count
    decide.assert_not_called()


def test_unrelated_writer_between_reads_rebases_only_unchanged_predicates(store):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    client.after_get = lambda _: other.transact(request("unrelated"), lambda _: changes(write("unrelated", {})))
    receipt = adapter.transact(request(reads=("grant", "budget", "owner")), spend)
    assert receipt.payload["guard_revision"] == 3
    assert adapter.read_snapshot(("budget",))["budget"].payload["data"]["remaining"] == 0


def _row_data(adapter, key):
    value = adapter.read_snapshot((key,))[key]
    return None if value is None else value.payload["data"]


def test_rebased_write_never_overwrites_a_predicate_changed_before_its_egt(store):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    client.after_get = lambda _: other.transact(request("unrelated"), lambda _: changes(write("unrelated", {})))
    client.before_submit = lambda: other.transact(request("competing", ("budget",)), lambda _: changes(
        write("budget", {"remaining": 7}, "replace")))
    with pytest.raises(Problem) as lost:
        adapter.transact(request(reads=("grant", "budget", "owner")), spend)
    assert code(lost) == "BEHAVIOR_CONFLICT"
    assert _row_data(adapter, "budget") == {"remaining": 7} and _row_data(adapter, "work") is None


@pytest.mark.parametrize("moment", ["during-reads", "before-egt"])
def test_rebased_write_is_refused_when_an_absent_predicate_is_inserted(store, moment):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    revoke = lambda: other.transact(request("revoke"), lambda _: changes(write("revocation", {"revoked": True})))

    def after(key):
        if key != "behavior:v1:row:grant":
            client.after_get = after
        elif moment == "during-reads":
            revoke()
        else:
            other.transact(request("unrelated"), lambda _: changes(write("unrelated", {})))
            client.before_submit = revoke

    def decide(rows):
        assert rows["revocation"] is None
        return changes(write("granted-work", {"state": "accepted"}))

    client.after_get = after
    with pytest.raises(Problem) as phantom:
        adapter.transact(request("needs-no-revocation", ("revocation", "grant")), decide)
    assert code(phantom) == "BEHAVIOR_CONFLICT"
    assert _row_data(adapter, "revocation") == {"revoked": True} and _row_data(adapter, "granted-work") is None


def test_same_request_committed_during_reads_is_not_committed_twice(store):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    original = request("same-request", ("grant",))

    def after(key):
        if key != "behavior:v1:row:grant":
            client.after_get = after
            return
        other.transact(original, lambda _: changes(write("first-commit", {})))

    client.after_get = after
    decide = Mock()
    with pytest.raises(Problem) as duplicate:
        adapter.transact(original, decide)
    assert code(duplicate) == "BEHAVIOR_CONFLICT"
    decide.assert_not_called()
    assert adapter.reconcile(original).payload["writes"][0][0] == "first-commit"


def test_competing_spend_after_snapshot_fails_guard_without_partial_writes(store):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    client.before_submit = lambda: other.transact(
        request("other", ("grant", "budget", "owner")), spend,
    )
    with pytest.raises(Problem) as stale:
        adapter.transact(request(reads=("grant", "budget", "owner")), spend)
    assert code(stale) == "BEHAVIOR_CONFLICT"
    assert len(client.submissions) == 3
    with pytest.raises(Problem) as missing:
        adapter.reconcile(request(reads=("grant", "budget", "owner")))
    assert code(missing) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert other.reconcile(request("other", ("grant", "budget", "owner"))).payload["result"]["accepted"]
    budget = client.backing.get("Behavior", SCOPE.partition_key, "behavior:v1:row:budget")
    assert json.loads(budget["Payload"])["data"]["remaining"] == 0


def test_same_id_digest_reads_receipt_without_evaluating_or_submitting_again(store):
    adapter, client = store
    original = adapter.transact(request(), lambda _: changes(write("work", {})))
    count, decide = len(client.submissions), Mock()
    assert adapter.transact(request(), decide) == original
    assert len(client.submissions) == count
    decide.assert_not_called()
    for altered in (
        request().model_copy(update={"request_digest": "f" * 64}),
        request(reads=("different",)),
    ):
        with pytest.raises(Problem) as collision:
            adapter.transact(altered, decide)
        assert code(collision) == "BEHAVIOR_REQUEST_CONFLICT"


@pytest.mark.parametrize("failure", ["before", "after", "malformed"])
def test_unknown_commit_never_resends_or_returns_success_and_reconcile_is_explicit(store, failure):
    adapter, client = store
    client.failure = failure
    count = len(client.submissions)
    with pytest.raises(Problem) as unknown:
        adapter.transact(request(), lambda _: changes(write("work", {})))
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert unknown.value.body["error"]["request_id"] == request().request_id
    assert len(client.submissions) == count + 1
    restarted = BehaviorStore(FakeTable(client.backing), SCOPE)
    if failure == "before":
        with pytest.raises(Problem) as absent:
            restarted.reconcile(request())
        assert code(absent) == "BEHAVIOR_COMMIT_UNKNOWN"
    else:
        assert restarted.reconcile(request()).payload["result"]["accepted"]
    assert len(client.submissions) == count + 1


def test_additional_sdk_dispatch_is_blocked_and_no_automatic_reconciliation(store):
    adapter, client = store
    client.repeat_request = True
    with pytest.raises(Problem) as unknown:
        adapter.transact(request(), lambda _: changes())
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert unknown.value.body["error"]["request_id"] == request().request_id
    assert len(client.submissions) == 2


def test_create_conflict_rolls_back_guard_and_receipt_in_fake_egt(store):
    adapter, client = store
    adapter.transact(request("seed"), lambda _: changes(write("existing", {})))
    before = copy.deepcopy(client.backing.rows)
    with pytest.raises(Problem) as exists:
        adapter.transact(request(), lambda _: changes(write("existing", {})))
    assert code(exists) == "BEHAVIOR_CONFLICT"
    assert client.backing.rows == before


def test_replace_requires_predicate_and_callback_cannot_modify_internal_read_snapshot(store):
    adapter, client = store
    seed(adapter)
    count = len(client.submissions)
    with pytest.raises(Problem) as missing:
        adapter.transact(request(), lambda _: changes(write("budget", {}, "replace")))
    assert code(missing) == "BEHAVIOR_MISSING_PREDICATE"
    assert len(client.submissions) == count

    def mutate(snapshot):
        snapshot["budget"].payload["data"]["remaining"] = 99
        return changes(write("budget", {"remaining": 0}, "replace"))

    adapter.transact(request("replace", ("budget",)), mutate)
    assert client.submissions[-1][2][2]["etag"] != "*"


def test_duplicate_rows_nonfinite_data_and_operation_overflow_are_pre_dispatch(store):
    adapter, client = store
    count = len(client.submissions)
    invalid = ControlChanges.model_construct(
        writes=(write("same", {}), write("same", {})), result={},
    )
    with pytest.raises(Problem) as duplicate:
        adapter.transact(request(), lambda _: invalid)
    assert code(duplicate) == "BEHAVIOR_INVALID_DATA"
    with pytest.raises((Problem, ValidationError)):
        adapter.transact(request(), lambda _: changes(write("work", {"bad": float("nan")})))
    assert len(client.submissions) == count
    limited = BehaviorStore(client, SCOPE.model_copy(update={"max_operations": 3}))
    with pytest.raises(Problem) as large:
        limited.transact(request(), lambda _: changes(write("a", {}), write("b", {})))
    assert code(large) == "BEHAVIOR_BATCH_TOO_LARGE"
    assert len(client.submissions) == count


def test_exact_100_operations_include_guard_receipt_and_98_business_rows(store):
    adapter, client = store
    adapter.transact(request(), lambda _: changes(*(
        write(f"row-{index}", {}) for index in range(98)
    )))
    assert len(client.submissions[-1]) == 100
    count = len(client.submissions)
    invalid = ControlChanges.model_construct(
        writes=tuple(write(f"row-{index}", {}) for index in range(99)), result={},
    )
    with pytest.raises(Problem) as large:
        adapter.transact(request("too-many"), lambda _: invalid)
    assert code(large) == "BEHAVIOR_INVALID_DATA"
    assert len(client.submissions) == count


def test_exact_entity_payload_limit_includes_envelope_and_one_extra_byte_is_rejected(store):
    _, client = store
    adapter = BehaviorStore(client, SCOPE.model_copy(update={"max_payload_bytes": 2048}))
    adapter.transact(request("measure"), lambda _: changes(write("a", {"text": ""})))
    overhead = len(client.submissions[-1][2][1]["Payload"])
    adapter.transact(request("fits"), lambda _: changes(write("b", {"text": "x" * (2048 - overhead)})))
    assert len(client.submissions[-1][2][1]["Payload"]) == 2048
    count = len(client.submissions)
    with pytest.raises(Problem) as large:
        adapter.transact(request("over"), lambda _: changes(write("c", {"text": "x" * (2049 - overhead)})))
    assert code(large) == "BEHAVIOR_ENTITY_TOO_LARGE"
    assert len(client.submissions) == count


def test_entity_payload_limit_counts_utf8_bytes_not_characters(store):
    _, client = store
    adapter = BehaviorStore(client, SCOPE.model_copy(update={"max_payload_bytes": 2048}))
    with pytest.raises(Problem) as large:
        adapter.transact(request(), lambda _: changes(write("work", {"text": "\u754c" * 700})))
    assert code(large) == "BEHAVIOR_ENTITY_TOO_LARGE"
    assert len(client.submissions) == 1


def test_inline_material_is_atomic_hash_checked_retained_and_read_back(store):
    adapter, client = store
    data = b"synthetic-input"
    material = InlineMaterial(data=data, sha256=hashlib.sha256(data).hexdigest())
    adapter.transact(request("seed"), lambda _: changes(write("work", {"state": "queued"}, material=material)))
    with pytest.raises(Problem) as retained:
        adapter.transact(request("drop", ("work",)), lambda _: changes(write("work", {}, "replace")))
    assert code(retained) == "BEHAVIOR_MATERIAL_RETAINED"
    observed = []

    def retain(snapshot):
        observed.append(base64.urlsafe_b64decode(snapshot["work"].payload["material"]["data"]))
        return changes(write("work", {"state": "running"}, "replace", material))

    adapter.transact(request("retain", ("work",)), retain)
    assert observed == [data]
    assert not hasattr(adapter, "delete") and not hasattr(adapter, "gc")
    with pytest.raises(ValidationError):
        InlineMaterial(data=data, sha256="0" * 64)
    with pytest.raises(ValidationError):
        InlineMaterial(data=b"x" * (MAX_INLINE_BYTES + 1), sha256="0" * 64)


def test_corrupt_guard_or_missing_table_is_not_empty_state(store):
    adapter, client = store
    guard = client.backing.rows[("Behavior", SCOPE.partition_key, GUARD_KEY)]
    guard["Payload"] = b'{"schema_version":999}'
    with pytest.raises(Problem) as corrupt:
        adapter.transact(request(), Mock())
    assert code(corrupt) == "BEHAVIOR_PROTOCOL_ERROR"
    client.get_entity = Mock(side_effect=ResourceNotFoundError(message="table missing"))
    with pytest.raises(Problem) as missing:
        adapter.initialize()
    assert code(missing) == "BEHAVIOR_READ_FAILED"


def test_saved_nonfinite_json_is_rejected_before_decision(store):
    adapter, client = store
    adapter.transact(request("seed"), lambda _: changes(write("work", {"value": 1})))
    row = client.backing.rows[("Behavior", SCOPE.partition_key, "behavior:v1:row:work")]
    payload = json.loads(row["Payload"])
    payload["data"]["value"] = float("nan")
    row["Payload"] = json.dumps(payload).encode()
    decide = Mock()
    with pytest.raises(Problem) as invalid:
        adapter.transact(request(reads=("work",)), decide)
    assert code(invalid) == "BEHAVIOR_PROTOCOL_ERROR"
    decide.assert_not_called()


@pytest.mark.parametrize("etag", ["", "*", "bad\r\netag", None])
def test_invalid_native_guard_etag_does_not_dispatch(store, etag):
    adapter, client = store
    client.backing.rows[("Behavior", SCOPE.partition_key, GUARD_KEY)].metadata["etag"] = etag
    with pytest.raises(Problem) as invalid:
        adapter.transact(request(), Mock())
    assert code(invalid) == "BEHAVIOR_PROTOCOL_ERROR"
    assert len(client.submissions) == 1


class WireResponse(HttpResponse):
    def __init__(self, request, status, body, headers):
        super().__init__(request, None)
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers)
        self.content_type = self.headers["Content-Type"]
        self.reason = "synthetic response"
        self._body = body

    def body(self):
        return self._body

    def read(self):
        return self._body

    def stream_download(self, pipeline, **kwargs):
        return iter((self._body,))


def json_response(request, status, payload, headers=None):
    return WireResponse(request, status, json.dumps(payload).encode(), {
        "Content-Type": "application/json", **(headers or {}),
    })


def parse_batch(request):
    body = request.body.encode() if isinstance(request.body, str) else request.body
    message = BytesParser(policy=policy.default).parsebytes(
        ("Content-Type: " + request.headers["Content-Type"] + "\r\n\r\n").encode() + body,
    )
    operations, wire_parts = [], []
    for part in message.walk():
        if part.get_content_type() != "application/http":
            continue
        raw = part.get_payload(decode=True)
        first, rest = raw.split(b"\r\n", 1)
        headers, data = rest.split(b"\r\n\r\n", 1)
        method, url, _ = first.decode().split(" ", 2)
        header_values = BytesParser(policy=policy.default).parsebytes(headers + b"\r\n\r\n")
        wire_parts.append((method, url, header_values, data))
        entity = json.loads(data)
        assert entity.pop("Payload@odata.type") == "Edm.Binary"
        entity["Payload"] = base64.b64decode(entity["Payload"])
        if method == "POST":
            operations.append(("create", entity))
        else:
            assert method == "PUT"
            operations.append(("update", entity, {
                "mode": UpdateMode.REPLACE, "etag": header_values["If-Match"],
                "match_condition": MatchConditions.IfNotModified,
            }))
    return operations, wire_parts


def batch_response(request, results):
    body = "--batchreply\r\nContent-Type: multipart/mixed; boundary=changesetreply\r\n\r\n"
    for index, result in enumerate(results):
        body += (
            "--changesetreply\r\nContent-Type: application/http\r\n"
            f"Content-Transfer-Encoding: binary\r\nContent-ID: {index}\r\n\r\n"
            f'HTTP/1.1 204 No Content\r\nETag: {result["etag"]}\r\n\r\n\r\n'
        )
    body += "--changesetreply--\r\n--batchreply--\r\n"
    return WireResponse(request, 202, body.encode(), {
        "Content-Type": "multipart/mixed; boundary=batchreply",
    })


def batch_conflict_response(request, status):
    error = json.dumps({"odata.error": {
        "code": "UpdateConditionNotSatisfied" if status == 412 else "EntityAlreadyExists",
        "message": {"lang": "en-US", "value": "0:synthetic conflict"},
    }})
    body = (
        "--batchreply\r\nContent-Type: multipart/mixed; boundary=changesetreply\r\n\r\n"
        "--changesetreply\r\nContent-Type: application/http\r\n"
        "Content-Transfer-Encoding: binary\r\nContent-ID: 0\r\n\r\n"
        f"HTTP/1.1 {status} Conflict\r\nContent-Type: application/json\r\n\r\n"
        f"{error}\r\n--changesetreply--\r\n--batchreply--\r\n"
    )
    return WireResponse(request, 202, body.encode(), {
        "Content-Type": "multipart/mixed; boundary=batchreply",
    })


class WireTransport(HttpTransport):
    """Real Table SDK, local HTTP transport only; never opens a socket."""

    def __init__(self):
        self.backing, self.requests, self.parts = Backing(), [], []
        self.fail_post = False
        self.conflict_status = None
        self.after_apply = None

    def open(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def send(self, request, **kwargs):
        self.requests.append(request)
        if request.method == "GET":
            match = re.search(r"PartitionKey='([^']+)',RowKey='([^']+)'", unquote(request.url))
            assert match
            try:
                entity = self.backing.get("Behavior", *match.groups())
            except ResourceNotFoundError:
                return json_response(request, 404, {"odata.error": {
                    "code": "ResourceNotFound", "message": {"value": "synthetic missing"},
                }}, {"x-ms-error-code": "ResourceNotFound"})
            payload = {**entity, "Payload": base64.b64encode(entity["Payload"]).decode(),
                       "Payload@odata.type": "Edm.Binary", "odata.etag": entity.metadata["etag"]}
            return json_response(request, 200, payload, {"etag": entity.metadata["etag"]})
        assert request.method == "POST" and urlsplit(request.url).path == "/$batch"
        if self.fail_post:
            return json_response(request, 503, {"odata.error": {
                "code": "ServerBusy", "message": {"value": "synthetic unavailable"},
            }}, {"x-ms-error-code": "ServerBusy"})
        if self.conflict_status is not None:
            return batch_conflict_response(request, self.conflict_status)
        operations, parts = parse_batch(request)
        self.parts.append(parts)
        result = self.backing.apply("Behavior", operations)
        if self.after_apply == "lost":
            raise ServiceRequestError("synthetic ack lost after commit")
        if self.after_apply == "incomplete":
            result = result[:1]
        return batch_response(request, result)


class Credential:
    def get_token(self, *scopes, **kwargs):
        return AccessToken("synthetic-not-a-real-token", 9999999999)


def wire_store(scope=SCOPE):
    transport = WireTransport()
    client = TableClient(URL, scope.table_name, credential=Credential(), transport=transport)
    return BehaviorStore(client, scope), transport


def test_real_sdk_serializes_one_batch_with_guard_and_row_if_match():
    adapter, transport = wire_store()
    adapter.initialize()
    seed(adapter)
    guard_etag = transport.backing.get("Behavior", SCOPE.partition_key, GUARD_KEY).metadata["etag"]
    budget_etag = transport.backing.get(
        "Behavior", SCOPE.partition_key, "behavior:v1:row:budget",
    ).metadata["etag"]
    result = adapter.transact(request(reads=("grant", "budget", "owner")), spend)
    assert result.payload["guard_revision"] == 2
    parts = transport.parts[-1]
    assert len(parts) == 6
    assert parts[0][0] == "PUT" and parts[0][2]["If-Match"] == guard_etag
    assert parts[1][0] == "POST"
    assert parts[2][0] == "PUT" and parts[2][2]["If-Match"] == budget_etag
    assert all("/Behavior" in unquote(part[1]) for part in parts)
    assert len([r for r in transport.requests if r.method == "POST"]) == 3


def test_real_sdk_exact_multipart_limit_stops_before_transport():
    measure, first = wire_store()
    measure.initialize()
    measure.transact(request(), lambda _: changes(write("work", {})))
    body = first.requests[-1].body
    size = len(body.encode() if isinstance(body, str) else body)
    for limit, succeeds in ((size, True), (size - 1, False)):
        adapter, transport = wire_store(SCOPE.model_copy(update={"max_batch_bytes": limit}))
        adapter.initialize()
        if succeeds:
            adapter.transact(request(), lambda _: changes(write("work", {})))
            assert len([r for r in transport.requests if r.method == "POST"]) == 2
        else:
            with pytest.raises(Problem) as large:
                adapter.transact(request(), lambda _: changes(write("work", {})))
            assert code(large) == "BEHAVIOR_BATCH_TOO_LARGE"
            assert len([r for r in transport.requests if r.method == "POST"]) == 1


def test_real_sdk_does_not_retry_503_even_with_default_client_retry_policy():
    adapter, transport = wire_store()
    adapter.initialize()
    transport.fail_post = True
    with pytest.raises(Problem) as unknown:
        adapter.transact(request(), lambda _: changes(write("work", {})))
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert len([r for r in transport.requests if r.method == "POST"]) == 2


@pytest.mark.parametrize("status", [409, 412])
def test_real_sdk_parses_batch_conflict_and_core_does_not_retry_or_read_back(status):
    adapter, transport = wire_store()
    adapter.initialize()
    transport.conflict_status = status
    with pytest.raises(Problem) as rejected:
        adapter.transact(request(), lambda _: changes(write("work", {})))
    assert code(rejected) == "BEHAVIOR_CONFLICT"
    assert rejected.value.status == status
    assert transport.requests[-1].method == "POST"
    assert len([r for r in transport.requests if r.method == "POST"]) == 2


@pytest.mark.parametrize("failure", ["lost", "incomplete"])
def test_real_sdk_unknown_ack_reconciles_original_receipt_without_resending(failure):
    adapter, transport = wire_store()
    adapter.initialize()
    transport.after_apply = failure
    material = InlineMaterial(data=b"input", sha256=hashlib.sha256(b"input").hexdigest())
    with pytest.raises(Problem) as unknown:
        adapter.transact(request(), lambda _: changes(write("work", {}, material=material)))
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert transport.requests[-1].method == "POST"
    assert len([r for r in transport.requests if r.method == "POST"]) == 2
    assert adapter.reconcile(request()).payload["result"]["accepted"]
    assert len([r for r in transport.requests if r.method == "POST"]) == 2
