"""Bounded delivery storage contracts with explicit synthetic ingress/result/receipt verifiers."""

from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from shared_context.behavior_delivery import DeliveryConsumer, ScanCursor, ScanRequest
from shared_context.behavior_delivery_pending import (
    PAGE_SIZE, WRITER_ENV, PendingHead, PendingMember, PendingPage, head_key, member_key, page_key,
)
from shared_context.behavior_operations import control_key
from shared_context.errors import Problem
from test_behavior_delivery import CONTEXT, OTHER, Harness
from test_behavior_store import FakeTable, SCOPE
from shared_context.behavior_store import BehaviorStore
from test_behavior_store import changes, request, store, write


def instruction_harness():
    h = Harness(scan_page_size=4)
    # The admission mode/cost observer is this test's seam; delivery and storage
    # transitions, exact receipts, CAS, and scan all remain their real producers.
    h.core.work_policy = SimpleNamespace(
        _admission_record=lambda _: SimpleNamespace(profile=SimpleNamespace(instruction=True)),
        admission_keys=lambda _: (),
        admission_observation=lambda _: None,
        admission_writes=lambda *args: (),
        admission_commit=lambda command, observation, commit: commit(),
    )
    return h


def admit_instruction(h, number, *, context=CONTEXT, sequence=None):
    command = h.command(f"instruction-{number}", context=context, sequence=number if sequence is None else sequence)
    command = command.model_copy(update={"deadline": None})
    h.attest(command, context)
    receipt = h.core.admit(command, context)
    return receipt.payload["result"]["obligation_id"], command, receipt


def read_row(h, key, model):
    return h.core._row(h.store.read_snapshot((key,)), key, model)


def test_reader_stage_keeps_legacy_writes_and_activation_does_not_rewrite_old_members(monkeypatch):
    monkeypatch.setenv(WRITER_ENV, "false")
    h = instruction_harness()
    old, command, receipt = admit_instruction(h, 0)
    index = control_key("delivery-index", "root")
    before = h.store.read_snapshot((index,))[index]
    assert h.store.read_snapshot((head_key("root"),))[head_key("root")] is None
    monkeypatch.setenv(WRITER_ENV, "true")
    new, _, _ = admit_instruction(h, 1)
    assert h.store.read_snapshot((index,))[index] == before
    assert h.core.admit(command, CONTEXT) == receipt
    head = read_row(h, head_key("root"), PendingHead)
    assert (head.pending_count, head.page_count, head.revision) == (1, 1, 1)
    h.work_result(old)
    h.message(old)
    h.receipt(old)
    assert h.data("delivery-index", "root")["entries"] == []
    assert read_row(h, head_key("root"), PendingHead) == head
    assert read_row(h, member_key("root", new), PendingMember).entry is not None
    assert h.store.read_snapshot((member_key("root", old),))[member_key("root", old)] is None


def test_non_instruction_admission_keeps_its_original_fixed_write_set(monkeypatch):
    monkeypatch.setenv(WRITER_ENV, "true")
    h = Harness()
    _, _, receipt = h.admit()
    keys = {key for key, _ in receipt.payload["writes"]}
    assert control_key("delivery-index", "root") in keys
    assert not any(row.payload["data"]["kind"].startswith("delivery_pending_") and
                   row.payload["data"]["kind"] != "delivery_pending_index"
                   for row in h.store.read_snapshot(tuple(keys)).values())
    assert h.store.read_snapshot((head_key("root"),))[head_key("root")] is None


def test_multiple_pages_and_exact_ack_use_bounded_transaction_predicates(monkeypatch):
    monkeypatch.setenv(WRITER_ENV, "true")
    h = instruction_harness()
    original = h.store._read_predicates
    sizes = []

    def predicates(keys, *args):
        sizes.append(len(keys))
        return original(keys, *args)

    monkeypatch.setattr(h.store, "_read_predicates", predicates)
    admitted = [admit_instruction(h, number)[0] for number in range(PAGE_SIZE * 2 + 3)]
    head = read_row(h, head_key("root"), PendingHead)
    assert (head.page_count, head.pending_count) == (3, len(admitted))
    assert len(read_row(h, page_key("root", 1), PendingPage).obligation_ids) == PAGE_SIZE
    assert len(read_row(h, page_key("root", 3), PendingPage).obligation_ids) == 3
    assert max(sizes) < 25
    sizes.clear()
    first = admitted[0]
    h.work_result(first)
    h.message(first)
    h.receipt(first)
    assert max(sizes) < 25
    assert read_row(h, head_key("root"), PendingHead).pending_count == len(admitted) - 1
    assert read_row(h, member_key("root", first), PendingMember).entry is None
    assert h.data("delivery-index", "root")["entries"] == []
    assert all(len(row["Payload"]) <= 48 * 1024 for row in h.client.backing.rows.values())


def test_reader_rollback_reads_pages_and_acknowledges_but_never_writes_new_legacy_admission(monkeypatch):
    monkeypatch.setenv(WRITER_ENV, "true")
    h = instruction_harness()
    ob, _, _ = admit_instruction(h, 0)
    h.work_result(ob)
    h.message(ob)
    monkeypatch.setenv(WRITER_ENV, "false")
    store = BehaviorStore(FakeTable(h.client.backing), SCOPE)
    cold = h.broker(store)
    h.core = cold
    h.time += timedelta(seconds=h.config.review_interval_seconds)
    result = cold.scan_due(ScanRequest(scan_id="reader-observes-current"), CONTEXT)
    assert [entry["obligation_id"] for entry in result.payload["result"]["due"]] == [ob]
    h.receipt(ob)
    assert read_row(h, head_key("root"), PendingHead).pending_count == 0
    h.core.work_policy = instruction_harness().core.work_policy
    with pytest.raises(Problem) as denied:
        admit_instruction(h, 1)
    assert denied.value.body["error"]["code"] == "DELIVERY_PENDING_WRITER_DISABLED"
    assert h.data("delivery-index", "root")["entries"] == []


def test_scan_cursor_and_consumer_pages_keep_original_pending_responsibility(monkeypatch):
    monkeypatch.setenv(WRITER_ENV, "true")
    h = instruction_harness()
    obligations = [admit_instruction(h, number)[0] for number in range(6)]
    for number, ob in enumerate(obligations):
        h.work_result(ob, evidence_id=f"saved-{number}")
    h.time += timedelta(seconds=h.config.review_interval_seconds)
    first = h.core.scan_due(ScanRequest(scan_id="first-page"), CONTEXT)
    assert first.payload["result"]["inspected"] == 4
    cursor = ScanCursor.model_validate_json(json.dumps(first.payload["result"]["cursor"]))
    second = h.core.scan_due(ScanRequest(scan_id="next-page", cursor=cursor), CONTEXT)
    found = [entry["obligation_id"] for page in (first, second) for entry in page.payload["result"]["due"]]
    assert set(found) == set(obligations)
    consumer = DeliveryConsumer(h.core)
    results = [result for _ in range(3) for result in consumer.drain("root", lambda _: CONTEXT, "offline-consumer", limit=2)]
    assert len(results) == 6
    assert len({result["binding"]["obligation_id"] for result in results}) == 6
    assert all(h.data("delivery-obligation", ob)["fulfillment"] == "pending" for ob in obligations)
    admit_instruction(h, 6)
    with pytest.raises(Problem) as stale:
        h.core.scan_due(ScanRequest(scan_id="stale-original-page", cursor=cursor), CONTEXT)
    assert stale.value.body["error"]["code"] == "DELIVERY_CURSOR_STALE"


def test_lost_member_commit_uses_original_receipt_without_duplicating_head(monkeypatch):
    monkeypatch.setenv(WRITER_ENV, "true")
    h = instruction_harness()
    command = h.command("lost-member").model_copy(update={"deadline": None})
    h.attest(command)
    original = h.core.request_for(command, CONTEXT)
    h.client.failure = "after"
    with pytest.raises(Problem) as lost:
        h.core.admit(command, CONTEXT)
    assert lost.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    receipt = h.store.reconcile(original)
    guard = h.store._guard()
    assert h.core.admit(command, CONTEXT) == receipt
    assert h.store._guard() == guard
    assert read_row(h, head_key("root"), PendingHead).pending_count == 1
    assert read_row(h, head_key("root"), PendingHead).revision == 1


def test_tail_cas_conflict_retries_only_original_instruction_admission(monkeypatch):
    monkeypatch.setenv(WRITER_ENV, "true")
    h = instruction_harness()
    for number in range(PAGE_SIZE):
        admit_instruction(h, number)
    h.client.before_submit = lambda: admit_instruction(h, PAGE_SIZE)
    ob, _, _ = admit_instruction(h, 2000, context=OTHER, sequence=0)
    head = read_row(h, head_key("root"), PendingHead)
    assert head.pending_count == PAGE_SIZE + 2 and head.page_count == 2
    page = read_row(h, page_key("root", 2), PendingPage)
    assert len(page.obligation_ids) == 2 and ob in page.obligation_ids


def test_layout_predicates_keep_original_request_receipt_and_exact_etag(store):
    adapter, client = store
    adapter.transact(request("seed-layout"), lambda _: changes(write("head", {"revision": 0})))
    original = request("unchanged-command", ("owner",))
    before = adapter.read_snapshot(("head",))["head"]
    receipt = adapter.transact(original, lambda rows: changes(
        write("head", {"revision": rows["head"].payload["data"]["revision"] + 1}, mode="replace")),
        extra_read_set=("head", "owner"))
    assert receipt.payload["request"] == original.model_dump(mode="json")
    update = next(operation for operation in client.submissions[-1] if operation[1]["RowKey"] == "behavior:v1:row:head")
    assert update[2]["etag"] == before.etag
    assert adapter.reconcile(original) == receipt
    guard = adapter._guard()
    assert adapter.transact(original, lambda _: pytest.fail("retained receipt must not reapply layout"),
                            extra_read_set=("head",)) == receipt
    assert adapter._guard() == guard
    for extra in (None, "head", tuple(f"extra-{i}" for i in range(98))):
        with pytest.raises(Problem) as refused:
            adapter.transact(request("bad-layout", ("owner",)), lambda _: changes(), extra_read_set=extra)
        assert refused.value.status == 422
    assert adapter._guard() == guard


@pytest.mark.parametrize("missing", ["page", "member"])
def test_missing_pending_page_or_member_is_not_an_empty_success(monkeypatch, missing):
    monkeypatch.setenv(WRITER_ENV, "true")
    h = instruction_harness()
    ob, _, _ = admit_instruction(h, 0)
    key = page_key("root", 1) if missing == "page" else member_key("root", ob)
    read = h.store.read_snapshot
    before = h.store._guard()

    def hidden(keys):
        return {name: None if name == key else value for name, value in read(keys).items()}

    monkeypatch.setattr(h.store, "read_snapshot", hidden)
    with pytest.raises(Problem) as incomplete:
        h.core.scan_due(ScanRequest(scan_id="incomplete-page"), CONTEXT)
    assert incomplete.value.status == 409
    assert h.store._guard() == before


def test_same_admission_id_cannot_replace_original_content_or_create_second_member(monkeypatch):
    monkeypatch.setenv(WRITER_ENV, "true")
    h = instruction_harness()
    _, command, _ = admit_instruction(h, 0)
    altered = command.model_copy(update={"event": command.event.model_copy(update={"content": b"Different."})})
    h.attest(altered)
    guard = h.store._guard()
    with pytest.raises(Problem) as conflict:
        h.core.admit(altered, CONTEXT)
    assert conflict.value.body["error"]["code"] == "BEHAVIOR_REQUEST_CONFLICT"
    assert h.store._guard() == guard
    assert read_row(h, head_key("root"), PendingHead).pending_count == 1
