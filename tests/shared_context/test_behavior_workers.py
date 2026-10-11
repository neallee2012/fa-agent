"""OFFLINE authority/executor contract tests, including real local processes.

SQLiteBacking shares the existing explicitly fake Table transaction semantics
across processes. LocalProcessExecutor performs a durable synthetic effect in a
real worker process. Neither is Azure Table/Foundry/native-capacity evidence.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import pickle
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from shared_context.behavior_delivery import (
    DeliveryConsumer, DeliveryCore, RuntimeContract, RuntimeRegistration, VerifiedWorkResult, authority_snapshot,
)
from shared_context.behavior_operations import (
    OperationDescription, ReceiptResult, ResolvedResource, _Operation, control_key,
)
from shared_context.behavior_store import GUARD_KEY, BehaviorStore, ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_workers import (
    ExecutorAdapter, ExecutorEvidence, JobSpec, WorkerConsumer, WorkerContract, WorkerCore,
    worker_key,
)
from shared_context.errors import Problem
from test_behavior_delivery import CONTEXT, NOW, Harness
from test_behavior_store import Backing, FakeTable, SCOPE, code


class OfflineExecutor:
    def __init__(self):
        self.starts, self.stops, self.queries = [], [], []
        self.evidence, self.latest = {}, {}
        self.serial = 0
        self.fail_start = False

    def adapter(self):
        return ExecutorAdapter(self.start, self.query, self.stop, lambda key: self.evidence[key])

    def attest(self, binding, state, **updates):
        self.serial += 1
        evidence_id = f"offline-evidence-{self.serial}"
        idle = state in {"parked", "succeeded", "failed", "stopped"}
        values = {
            "evidence_id": evidence_id, "binding": binding, "sequence": self.serial,
            "native_execution_id": control_key("offline-executor", binding.job_id, str(binding.generation)),
            "state": state, "entire_tree_idle": idle, "all_inflight_starts_fenced": idle,
            "proof": b"offline executor contract fixture, NOT native activity evidence", **updates,
        }
        proof = ExecutorEvidence(**values)
        self.evidence[evidence_id] = proof
        self.latest[binding.start_request_id] = evidence_id
        return evidence_id

    def start(self, command):
        self.starts.append(command)
        evidence_id = self.attest(command.binding, "running")
        if self.fail_start:
            raise TimeoutError("untrusted executor error must not escape")
        return evidence_id

    def query(self, binding):
        self.queries.append(binding)
        return self.latest.get(binding.start_request_id) or self.attest(binding, "unknown")

    def stop(self, command):
        self.stops.append(command)
        return self.attest(command.binding, "unknown")


class WorkerHarness(Harness):
    def __init__(self, capacity=1, **options):
        super().__init__()
        self.executor = OfflineExecutor()
        self.worker_contract = WorkerContract(
            workspace_id="whole-workspace", control_binding=self.config.control_binding,
            executor_id="offline-executor", executor_version="v1", protection_version="inline-no-gc-v1",
            capacity=capacity, **options,
        )
        self.workers = self.worker(self.store)
        self.workers.initialize()

    def worker(self, store):
        return WorkerCore(
            store, self.worker_contract, adapter=self.executor.adapter(),
            mode="offline_contract", clock=lambda: self.time,
        )

    def job(self, name="job-one", parent=None, **changes):
        spec = JobSpec(job_id=name, work_id="work-" + name, input=b"exact saved business input", parent_job_id=parent, **changes)
        self.workers.enqueue(spec, CONTEXT)
        return spec

    def proof(self, job, state, **values):
        binding = self.workers.inspect(job).binding
        return self.executor.attest(binding, state, **values)

    def occupied(self):
        key = worker_key("pool", self.worker_contract.workspace_id)
        row = authority_snapshot(self.store, (key,))[key]
        return [slot for slot in row.payload["data"]["slots"] if slot["binding"] is not None]


def test_scheduler_native_is_closed_without_persisted_native_evidence():
    h = WorkerHarness()
    closed = WorkerCore(h.store, h.worker_contract, adapter=h.executor.adapter())
    with pytest.raises(Problem) as error:
        closed.enqueue(JobSpec(job_id="not-enabled", work_id="w", input=b"x"), CONTEXT)
    assert code(error) == "WORKER_NATIVE_DISABLED"
    assert h.executor.starts == []


def test_c4_fifth_is_queued_until_verified_completion_not_four_permits_as_native_proof():
    h = WorkerHarness(capacity=4)
    for number in range(5):
        h.job(f"job-{number}")
    for number in range(4):
        h.workers.start(f"job-{number}", CONTEXT)
    with pytest.raises(Problem) as full:
        h.workers.start("job-4", CONTEXT)
    assert code(full) == "WORKER_CAPACITY_FULL"
    assert len(h.occupied()) == len(h.executor.starts) == 4
    assert h.workers.inspect("job-4").state == "queued"
    h.workers.observe("job-0", h.proof("job-0", "succeeded"), CONTEXT)
    receipt = h.workers.start("job-4", CONTEXT)
    assert receipt.payload["result"]["actual_environment"] == "NOT_RUN"
    assert len(h.occupied()) == 4 and len(h.executor.starts) == 5


def test_two_worker_claim_race_cannot_start_the_same_business_twice():
    h = WorkerHarness()
    h.job()
    other = h.worker(BehaviorStore(FakeTable(h.client.backing), SCOPE))
    h.client.before_submit = lambda: other.start("job-one", CONTEXT)
    with pytest.raises(Problem) as lost:
        h.workers.start("job-one", CONTEXT)
    assert code(lost) == "BEHAVIOR_CONFLICT"
    assert len(h.executor.starts) == 1
    h.workers.start("job-one", CONTEXT)
    assert len(h.executor.starts) == 1 and len(h.executor.queries) == 1


def test_start_claim_commit_ack_loss_queries_original_attempt_without_starting():
    h = WorkerHarness()
    h.job()
    h.client.failure = "after"
    with pytest.raises(Problem) as lost:
        h.workers.start("job-one", CONTEXT)
    assert code(lost) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert h.executor.starts == []
    original = h.workers.inspect("job-one").binding
    rebuilt = h.worker(BehaviorStore(FakeTable(h.client.backing), SCOPE))
    receipt = rebuilt.start("job-one", CONTEXT)
    assert receipt.payload["result"]["status"] == "capacity_uncertain"
    assert h.executor.starts == [] and h.executor.queries == [original]
    assert len(h.occupied()) == 1
    assert h.data("budget", "root")["remaining_attempts"] == 19


def test_cancel_winning_the_last_guard_gate_prevents_any_business_start():
    h = WorkerHarness()
    h.job()
    h.client.before_submit = lambda: h.workers.cancel("job-one", CONTEXT)
    with pytest.raises(Problem) as fenced:
        h.workers.start("job-one", CONTEXT)
    assert code(fenced) == "BEHAVIOR_CONFLICT"
    assert h.executor.starts == []
    assert h.workers.inspect("job-one").state == "cancelled"
    assert h.data("budget", "root")["remaining_attempts"] == 20


def test_executor_start_response_loss_never_reruns_business_work():
    h = WorkerHarness()
    h.job()
    h.executor.fail_start = True
    with pytest.raises(Problem) as error:
        h.workers.start("job-one", CONTEXT)
    assert code(error) == "WORKER_START_UNKNOWN"
    rebuilt = h.worker(BehaviorStore(FakeTable(h.client.backing), SCOPE))
    rebuilt.start("job-one", CONTEXT)
    assert len(h.executor.starts) == 1 and rebuilt.inspect("job-one").state == "running"
    assert len(h.occupied()) == 1


def test_expired_lease_unknown_and_cancel_acceptance_never_release_an_executor():
    h = WorkerHarness()
    h.job()
    h.job("second")
    h.workers.start("job-one", CONTEXT)
    h.time += timedelta(days=2)
    h.workers.observe("job-one", h.proof("job-one", "unknown", native_execution_id=None), CONTEXT)
    receipt = h.workers.cancel("job-one", CONTEXT)
    assert receipt.payload["result"]["cancel_accepted"] is True
    assert receipt.payload["result"]["executor_stopped"] is False
    h.workers.stop("job-one", CONTEXT)
    h.workers.stop("job-one", CONTEXT)
    assert len(h.executor.stops) == 1
    assert len(h.occupied()) == 1
    with pytest.raises(Problem) as full:
        h.workers.start("second", CONTEXT)
    assert code(full) == "WORKER_CAPACITY_FULL"
    h.workers.observe("job-one", h.proof("job-one", "stopped"), CONTEXT)
    h.workers.start("second", CONTEXT)
    assert h.workers.inspect("job-one").state == "cancelled"


def test_full_admission_and_zero_model_budget_leave_completion_cancel_and_stop_lanes():
    h = WorkerHarness(pending_limit=1, control_units=1)
    h.job()
    h.workers.start("job-one", CONTEXT)
    assert h.workers.inspect("job-one").control_remaining == 0
    h.mutate("budget", "root", remaining_attempts=0, remaining_units=0)
    with pytest.raises(Problem) as full:
        h.job("overflow")
    assert code(full) == "WORKER_ADMISSION_FULL"
    h.workers.cancel("job-one", CONTEXT)
    with pytest.raises(Problem) as exhausted:
        h.workers.stop("job-one", CONTEXT)
    assert code(exhausted) == "WORKER_CONTROL_EXHAUSTED"
    # Stop itself was dispatched once on its independent reservation.
    assert len(h.executor.stops) == 1 and len(h.occupied()) == 1
    receipt = h.workers.observe("job-one", h.proof("job-one", "stopped"), CONTEXT)
    assert receipt.payload["result"]["status"] == "cancelled"
    assert h.occupied() == []


def test_cancelled_admission_is_durably_forwardable_without_an_executor_or_second_delivery():
    h = WorkerHarness()
    ob, command, _ = h.admit()
    h.workers.enqueue(JobSpec(job_id="never-started", work_id=command.work.work_id,
                             input=b"retained input", obligation_id=ob), CONTEXT)
    h.message(ob, "failure")
    h.receipt(ob)
    old_obligation = h.data("delivery-obligation", ob)
    h.workers.cancel("never-started", CONTEXT)
    assert h.workers.completions() == ("never-started",)
    rebuilt = h.worker(BehaviorStore(FakeTable(h.client.backing), SCOPE))
    delivery = h.broker(rebuilt.store, replace(h.registration, verify_work_result=rebuilt.verify_work_result))
    rebuilt.forward_result("never-started", CONTEXT, delivery)
    rebuilt.forward_result("never-started", CONTEXT, delivery)
    assert rebuilt.completions() == ()
    assert rebuilt.inspect("never-started").result_forwarded
    assert h.data("delivery-work-state", ob)["state"] == "cancelled"
    assert h.data("delivery-obligation", ob) == old_obligation
    assert not h.executor.starts and not h.executor.stops and not h.occupied()


def test_inactive_cancel_commit_ack_loss_keeps_completion_and_does_not_spend_again():
    h = WorkerHarness()
    ob, command, _ = h.admit()
    h.workers.enqueue(JobSpec(job_id="never-started", work_id=command.work.work_id, input=b"saved",
                             obligation_id=ob), CONTEXT)
    h.client.failure = "after"
    with pytest.raises(Problem) as lost:
        h.workers.cancel("never-started", CONTEXT)
    assert code(lost) == "BEHAVIOR_COMMIT_UNKNOWN"
    rebuilt = h.worker(BehaviorStore(FakeTable(h.client.backing), SCOPE))
    rebuilt.cancel("never-started", CONTEXT)
    assert rebuilt.completions() == ("never-started",)
    assert rebuilt.inspect("never-started").completion_credit == 0
    assert not h.executor.starts


def test_start_winning_cancel_race_cannot_create_an_inactive_completion():
    h = WorkerHarness()
    ob, command, _ = h.admit()
    h.workers.enqueue(JobSpec(job_id="racing", work_id=command.work.work_id, input=b"saved",
                             obligation_id=ob), CONTEXT)
    h.client.before_submit = lambda: h.workers.start("racing", CONTEXT)
    with pytest.raises(Problem) as conflict:
        h.workers.cancel("racing", CONTEXT)
    assert code(conflict) == "BEHAVIOR_CONFLICT"
    h.workers.cancel("racing", CONTEXT)
    with pytest.raises(Problem) as denied:
        h.workers.reconcile_inactive_cancellation("racing", CONTEXT)
    assert code(denied) == "WORKER_CANCELLATION_UNVERIFIED"
    assert h.workers.completions() == () and len(h.occupied()) == 1


def test_legacy_inactive_cancel_reconciles_existing_reservation_without_execution():
    h = WorkerHarness()
    ob, command, _ = h.admit()
    h.workers.enqueue(JobSpec(job_id="legacy", work_id=command.work.work_id, input=b"saved",
                             obligation_id=ob), CONTEXT)
    h.mutate("worker-job", "legacy", state="cancelled", cancel_requested=True, cancel_credit=0)
    h.mutate("worker-pool", h.worker_contract.workspace_id, pending=[])
    receipt = h.workers.reconcile_inactive_cancellation("legacy", CONTEXT)
    assert h.workers.reconcile_inactive_cancellation("legacy", CONTEXT) == receipt
    delivery = h.broker(h.store, replace(h.registration, verify_work_result=h.workers.verify_work_result))
    h.workers.forward_result("legacy", CONTEXT, delivery)
    assert h.data("delivery-work-state", ob)["state"] == "cancelled"
    assert h.workers.completions() == () and h.executor.starts == []


def test_saved_stop_without_business_result_can_forward_explicit_cancellation_only():
    h = WorkerHarness()
    ob, command, _ = h.admit()
    h.workers.enqueue(JobSpec(job_id="stopped", work_id=command.work.work_id, input=b"saved",
                             obligation_id=ob), CONTEXT)
    h.workers.start("stopped", CONTEXT)
    h.workers.cancel("stopped", CONTEXT)
    h.workers.observe("stopped", h.proof("stopped", "stopped"), CONTEXT)
    original = h.data("worker-completion", "stopped", "1")
    assert original["result"] is None
    delivery = h.broker(h.store, replace(h.registration, verify_work_result=h.workers.verify_work_result))
    h.workers.forward_result("stopped", CONTEXT, delivery)
    result = h.data("delivery-work-state", ob)["result"]
    assert result["state"] == "cancelled" and result["passed_checks"] == []
    assert result["unresolved_operations"] == []
    assert h.data("worker-completion", "stopped", "1") == original
    assert h.workers.completions() == () and len(h.executor.starts) == 1


@pytest.mark.parametrize("count", [1, 17])
def test_cancellation_preserves_opaque_operation_records_or_keeps_the_completion_pending(count):
    h = WorkerHarness()
    ob, command, _ = h.admit()
    identifiers = tuple(f"unreconciled-{number}" for number in range(count))
    h.mutate("owner", CONTEXT.owner_record_id, operation_ids=list(identifiers))
    h.store.transact(
        ControlRequest(request_id="opaque-operations", request_digest=hashlib.sha256(b"opaque").hexdigest()),
        lambda _: ControlChanges(writes=tuple(
            ControlWrite(row_id=control_key("operation", identifier), mode="create",
                         data={"operation_id": identifier, "state": "future-unrecognized-state"})
            for identifier in identifiers
        ), result={"synthetic_only": True}),
    )
    h.workers.enqueue(JobSpec(job_id="cancel", work_id=command.work.work_id, input=b"saved",
                             obligation_id=ob), CONTEXT)
    h.workers.cancel("cancel", CONTEXT)
    delivery = h.broker(h.store, replace(h.registration, verify_work_result=h.workers.verify_work_result))
    if count <= 16:
        h.workers.forward_result("cancel", CONTEXT, delivery)
        assert h.data("delivery-work-state", ob)["result"]["unresolved_operations"] == list(identifiers)
    else:
        with pytest.raises(Problem) as denied:
            h.workers.forward_result("cancel", CONTEXT, delivery)
        assert code(denied) == "WORKER_CANCELLATION_RECONCILIATION_REQUIRED"
        assert h.data("delivery-work-state", ob)["state"] == "queued"
        assert h.workers.completions() == ("cancel",)
    assert not h.executor.starts


@pytest.mark.parametrize("applied", [False, True])
def test_cancellation_classifies_prepared_and_verified_applied_operations(applied):
    h = WorkerHarness()
    ob, command, _ = h.admit()
    description = OperationDescription(
        adapter_id="configured-adapter", adapter_version="1", kind="request", method="PATCH",
        target="test-work://original", content=b"exact approved bytes",
        resources=(ResolvedResource(resource_id="test-work://original", version="1"),),
    )
    material = description.model_dump_json().encode()
    digest = hashlib.sha256(material).hexdigest()
    operation = _Operation(
        operation_id="op-a", owner_record_id=CONTEXT.owner_record_id, conversation_id=CONTEXT.conversation_id,
        root_budget_id=CONTEXT.root_budget_id, description_digest=digest, adapter_contract_digest="1" * 64,
        current_attempt_id="original-attempt" if applied else None,
        effect_state="applied" if applied else "unknown", business_state="succeeded" if applied else "pending",
        successful_result=ReceiptResult(receipt_id="original-receipt",
            resource=ResolvedResource(resource_id=description.target, version="applied-version"), status="completed") if applied else None,
    )
    from shared_context.behavior_store import InlineMaterial
    h.store.transact(
        ControlRequest(request_id="retained-applied", request_digest=hashlib.sha256(b"retained").hexdigest()),
        lambda _: ControlChanges(writes=(ControlWrite(
            row_id=control_key("operation", "op-a"), mode="create", data=operation.model_dump(mode="json"),
            material=InlineMaterial(data=material, sha256=digest),
        ),), result={"synthetic_only": True}),
    )
    h.workers.enqueue(JobSpec(job_id="cancel", work_id=command.work.work_id, input=b"saved", obligation_id=ob), CONTEXT)
    h.workers.cancel("cancel", CONTEXT)
    delivery = h.broker(h.store, replace(h.registration, verify_work_result=h.workers.verify_work_result))
    h.workers.forward_result("cancel", CONTEXT, delivery)
    result = h.data("delivery-work-state", ob)["result"]
    assert result["known_effects"] == (["op-a"] if applied else [])
    assert result["unresolved_operations"] == []
    assert result["state"] == "cancelled"


def test_parked_cancel_retains_child_responsibilities_without_claiming_the_child_stopped():
    h = WorkerHarness()
    ob, command, _ = h.admit()
    h.workers.enqueue(JobSpec(job_id="parent", work_id=command.work.work_id, input=b"saved",
                             obligation_id=ob), CONTEXT)
    h.workers.start("parent", CONTEXT)
    h.job("child", parent="parent")
    h.workers.observe("parent", h.proof("parent", "parked", checkpoint=b"saved checkpoint"), CONTEXT)
    h.workers.cancel("parent", CONTEXT)
    delivery = h.broker(h.store, replace(h.registration, verify_work_result=h.workers.verify_work_result))
    h.workers.forward_result("parent", CONTEXT, delivery)
    assert h.data("delivery-work-state", ob)["result"]["unresolved_operations"] == ["child"]
    assert h.workers.inspect("child").state == "queued"
    assert h.workers.pending() == ("child",) and h.workers.completions() == ()


def test_cancel_queued_job_needs_no_slot_or_model():
    h = WorkerHarness()
    h.job()
    h.mutate("budget", "root", remaining_attempts=0, remaining_units=0)
    receipt = h.workers.cancel("job-one", CONTEXT)
    assert receipt.payload["result"]["executor_stopped"] is True
    assert h.executor.starts == [] and h.workers.pending() == ()


def test_c1_parent_checkpoint_yields_child_progress_then_atomic_reacquire():
    h = WorkerHarness()
    h.job("parent")
    h.workers.start("parent", CONTEXT)
    h.job("child", parent="parent")
    with pytest.raises(Problem) as full:
        h.workers.start("child", CONTEXT)
    assert code(full) == "WORKER_CAPACITY_FULL"
    h.workers.observe("parent", h.proof("parent", "parked", checkpoint=b"verified parked checkpoint"), CONTEXT)
    assert h.occupied() == [] and h.workers.inspect("parent").state == "waiting_dependency"
    consumer = WorkerConsumer(h.workers)
    consumer.drain(lambda _: CONTEXT, limit=1)
    assert h.workers.inspect("child").state == "running"
    assert h.workers.inspect("parent").state == "waiting_dependency"
    h.workers.observe("child", h.proof("child", "succeeded"), CONTEXT)
    consumer.drain(lambda _: CONTEXT, limit=1)
    parent = h.workers.inspect("parent")
    assert parent.state == "running" and parent.generation == 2
    assert h.executor.starts[-1].checkpoint == b"verified parked checkpoint"
    assert len(h.occupied()) == 1 and len(h.executor.starts) == 3


def test_parent_reacquire_competes_for_same_slot_and_retains_checkpoint():
    h = WorkerHarness()
    h.job("parent")
    h.workers.start("parent", CONTEXT)
    h.job("child", parent="parent")
    h.workers.observe("parent", h.proof("parent", "parked", checkpoint=b"checkpoint"), CONTEXT)
    h.workers.cancel("child", CONTEXT)
    h.job("unrelated")
    h.workers.start("unrelated", CONTEXT)
    with pytest.raises(Problem) as full:
        h.workers.start("parent", CONTEXT)
    assert code(full) == "WORKER_CAPACITY_FULL"
    assert h.workers.inspect("parent").checkpoint == b"checkpoint"
    assert h.workers.inspect("parent").generation == 1


def test_late_old_generation_completion_does_not_release_resumed_parent_slot():
    h = WorkerHarness()
    h.job("parent")
    h.workers.start("parent", CONTEXT)
    old = h.workers.inspect("parent").binding
    h.job("child", parent="parent")
    h.workers.observe("parent", h.proof("parent", "parked", checkpoint=b"checkpoint"), CONTEXT)
    h.workers.cancel("child", CONTEXT)
    h.workers.start("parent", CONTEXT)
    receipt = h.workers.observe("parent", h.executor.attest(old, "succeeded"), CONTEXT)
    assert receipt.payload["result"]["status"] == "late_evidence_retained"
    assert h.workers.inspect("parent").generation == 2 and len(h.occupied()) == 1


@pytest.mark.parametrize("field,value", [("input_digest", "0" * 64), ("generation", 2)])
def test_wrong_attempt_receipt_cannot_release_slot(field, value):
    h = WorkerHarness()
    h.job()
    h.workers.start("job-one", CONTEXT)
    wrong = h.workers.inspect("job-one").binding.model_copy(update={field: value})
    with pytest.raises(Problem):
        h.workers.observe("job-one", h.executor.attest(wrong, "succeeded"), CONTEXT)
    assert len(h.occupied()) == 1 and h.workers.inspect("job-one").state == "running"


def test_new_owner_cannot_take_over_unknown_executor_but_can_adopt_verified_park():
    h = WorkerHarness()
    h.job("parent")
    h.workers.start("parent", CONTEXT)
    h.job("child", parent="parent")
    h.mutate("owner", CONTEXT.owner_record_id, epoch=2)
    new = CONTEXT.model_copy(update={"epoch": 2})
    with pytest.raises(Problem) as denied:
        h.workers.adopt("parent", new)
    assert code(denied) == "WORKER_TAKEOVER_UNCERTAIN" and len(h.occupied()) == 1
    with pytest.raises(Problem) as old:
        h.workers.cancel("parent", CONTEXT)
    assert code(old) == "WORKER_OWNER_FENCED"
    h.workers.observe("parent", h.proof("parent", "parked", checkpoint=b"checkpoint"), new)
    h.workers.adopt("parent", new)
    h.workers.cancel("child", new)
    h.workers.start("parent", new)
    assert h.workers.inspect("parent").owner.epoch == 2 and len(h.occupied()) == 1


def test_admission_intent_is_claimed_once_even_if_a_caller_changes_job_id():
    h = WorkerHarness()
    ob, command, _ = h.admit()
    spec = JobSpec(job_id="original", work_id=command.work.work_id, input=b"saved input", obligation_id=ob)
    h.workers.enqueue(spec, CONTEXT)
    with pytest.raises(Problem):
        h.workers.enqueue(spec.model_copy(update={"job_id": "replacement"}), CONTEXT)
    h.workers.start("original", CONTEXT)
    assert len(h.executor.starts) == 1


@pytest.mark.parametrize("winner_outcome", ["observed", "commit_ack_lost", "query_unknown"])
def test_two_reconcilers_replaying_one_query_reservation_dispatch_only_once(winner_outcome):
    h = WorkerHarness(queries_per_generation=1)
    h.job()
    h.workers.start("job-one", CONTEXT)
    binding = h.workers.inspect("job-one").binding
    other_client = FakeTable(h.client.backing)
    other = h.worker(BehaviorStore(other_client, SCOPE))
    if winner_outcome == "commit_ack_lost":
        other_client.failure = "after"
    elif winner_outcome == "query_unknown":
        def uncertain_query(binding):
            h.executor.query(binding)
            raise TimeoutError("untrusted query details")
        other.adapter = replace(other.adapter, query=uncertain_query)
    receipts, failures, guard_reads = [], [], 0

    def complete_competing_query(row_key):
        nonlocal guard_reads
        if row_key == GUARD_KEY:
            guard_reads += 1
        if guard_reads == 2:
            # Both callers have read reservation zero; this one commits first.
            try:
                receipts.append(other.reconcile("job-one", CONTEXT))
            except Problem as error:
                failures.append(error)
        else:
            h.client.after_get = complete_competing_query

    submissions = len(h.client.submissions)
    h.client.after_get = complete_competing_query
    refused = None
    try:
        h.workers.reconcile("job-one", CONTEXT)
    except Problem as error:
        refused = error
    assert h.executor.queries == ([] if winner_outcome == "commit_ack_lost" else [binding])
    if winner_outcome == "observed":
        assert len(receipts) == 1 and receipts[0].payload["result"]["status"] == "running"
        assert failures == []
    else:
        expected = "BEHAVIOR_COMMIT_UNKNOWN" if winner_outcome == "commit_ack_lost" else "WORKER_QUERY_UNKNOWN"
        assert receipts == [] and len(failures) == 1
        assert failures[0].body["error"]["code"] == expected
    assert refused is not None and refused.body["error"]["code"] == "WORKER_QUERY_REPLAY"
    assert h.client.reads[-1] == (
        SCOPE.partition_key, "behavior:v1:receipt:" + refused.body["error"]["request_id"],
    )
    if winner_outcome == "commit_ack_lost":
        assert refused.body["error"]["request_id"] == failures[0].body["error"]["request_id"]
        assert refused.body["error"]["request_digest"] == failures[0].body["error"]["request_digest"]
    assert len(h.client.submissions) == submissions
    assert h.workers.inspect("job-one").queries_reserved == 1
    assert len(h.occupied()) == 1 and len(h.executor.starts) == 1


def test_query_reservation_decision_loser_does_not_dispatch_after_competing_commit():
    h = WorkerHarness(queries_per_generation=1)
    h.job()
    h.workers.start("job-one", CONTEXT)
    binding = h.workers.inspect("job-one").binding
    other = h.worker(BehaviorStore(FakeTable(h.client.backing), SCOPE))
    receipts = []
    h.client.before_submit = lambda: receipts.append(other.reconcile("job-one", CONTEXT))
    with pytest.raises(Problem) as conflict:
        h.workers.reconcile("job-one", CONTEXT)
    assert code(conflict) == "BEHAVIOR_CONFLICT"
    assert len(receipts) == 1 and receipts[0].payload["result"]["status"] == "running"
    assert h.executor.queries == [binding]
    assert h.workers.inspect("job-one").queries_reserved == 1
    assert len(h.occupied()) == 1 and len(h.executor.starts) == 1


@pytest.mark.parametrize("failure", ["before", "after", "malformed"])
def test_query_reservation_unknown_commit_never_dispatches_or_releases_capacity(failure):
    h = WorkerHarness(queries_per_generation=1)
    h.job()
    h.workers.start("job-one", CONTEXT)
    before = h.workers.inspect("job-one")
    h.client.failure = failure
    with pytest.raises(Problem) as unknown:
        h.workers.reconcile("job-one", CONTEXT)
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert h.executor.queries == []
    after = h.workers.inspect("job-one")
    assert after.queries_reserved == (0 if failure == "before" else 1)
    assert (after.binding, after.last_evidence_id, after.state) == (
        before.binding, before.last_evidence_id, before.state,
    )
    assert len(h.occupied()) == 1 and len(h.executor.starts) == 1


def test_serial_reconciliation_uses_a_new_bounded_reservation_for_each_query():
    h = WorkerHarness(queries_per_generation=2)
    h.job()
    h.workers.start("job-one", CONTEXT)
    binding = h.workers.inspect("job-one").binding
    first = h.workers.reconcile("job-one", CONTEXT)
    assert first.payload["result"]["status"] == "running"
    rebuilt = h.worker(BehaviorStore(FakeTable(h.client.backing), SCOPE))
    h.executor.attest(binding, "unknown")
    second = rebuilt.reconcile("job-one", CONTEXT)
    assert second.payload["result"]["status"] == "capacity_uncertain"
    assert h.executor.queries == [binding, binding]
    assert rebuilt.inspect("job-one").queries_reserved == 2
    with pytest.raises(Problem) as exhausted:
        rebuilt.reconcile("job-one", CONTEXT)
    assert code(exhausted) == "WORKER_QUERY_EXHAUSTED"
    assert h.executor.queries == [binding, binding]
    assert len(h.occupied()) == 1 and len(h.executor.starts) == 1


def test_query_reservations_are_bounded_but_final_callback_and_cancel_still_progress():
    h = WorkerHarness(queries_per_generation=1)
    h.job()
    h.workers.start("job-one", CONTEXT)
    h.workers.reconcile("job-one", CONTEXT)
    queries = len(h.executor.queries)
    with pytest.raises(Problem) as full:
        h.workers.reconcile("job-one", CONTEXT)
    assert code(full) == "WORKER_QUERY_EXHAUSTED" and len(h.executor.queries) == queries
    h.workers.cancel("job-one", CONTEXT)
    h.workers.observe("job-one", h.proof("job-one", "stopped"), CONTEXT)
    assert h.workers.inspect("job-one").state == "cancelled"


def test_stale_owner_is_denied_before_any_executor_query():
    h = WorkerHarness()
    h.job()
    h.workers.start("job-one", CONTEXT)
    h.mutate("owner", CONTEXT.owner_record_id, epoch=2)
    with pytest.raises(Problem) as stale:
        h.workers.reconcile("job-one", CONTEXT)
    assert code(stale) == "WORKER_OWNER_FENCED" and h.executor.queries == []


def test_completion_consumer_survives_forward_claim_loss_and_never_reexecutes_business():
    h = WorkerHarness(pending_limit=1)
    ob, command, _ = h.admit()
    spec = JobSpec(job_id="original", work_id=command.work.work_id, input=b"business input", obligation_id=ob)
    h.workers.enqueue(spec, CONTEXT)
    h.workers.start("original", CONTEXT)
    result = VerifiedWorkResult(
        evidence_id="saved-worker-result", verifier_version=h.config.verifier_version,
        work_id=command.work.work_id,
        definition_digest=h.data("delivery-work-state", ob)["definition_digest"],
        state="succeeded", passed_checks=("answer-checked",),
        content=b"verified result from original executor", proof=b"offline checked business result",
    )
    h.workers.observe("original", h.proof("original", "succeeded", result=result), CONTEXT)
    assert h.data("delivery-work-state", ob)["state"] == "queued"
    assert h.workers.pending() == () and h.workers.completions() == ("original",)
    with pytest.raises(Problem) as reserved:
        h.job("blocked-admission")
    assert code(reserved) == "WORKER_ADMISSION_FULL"
    delivery = h.broker(h.store, replace(h.registration, verify_work_result=h.workers.verify_work_result))
    h.client.failure = "after"
    with pytest.raises(Problem) as lost:
        h.workers.forward_result("original", CONTEXT, delivery)
    assert code(lost) == "BEHAVIOR_COMMIT_UNKNOWN"
    store = BehaviorStore(FakeTable(h.client.backing), SCOPE)
    workers = h.worker(store)
    rebuilt = h.broker(store, replace(h.registration, verify_work_result=workers.verify_work_result))
    WorkerConsumer(workers, rebuilt).drain(lambda _: CONTEXT)
    assert workers.completions() == ()
    assert h.data("delivery-work-state", ob)["state"] == "succeeded"
    assert h.data("delivery-work-state", ob)["result"]["content"] == result.model_dump(mode="json")["content"]
    DeliveryConsumer(rebuilt).consume(ob, CONTEXT, "restarted-delivery-worker")
    assert h.data("delivery-obligation", ob)["final_delivery_id"]
    assert len(h.executor.starts) == 1


def test_unverified_completed_result_stays_in_completion_inventory_without_rerun():
    h = WorkerHarness()
    ob, command, _ = h.admit()
    h.workers.enqueue(JobSpec(job_id="original", work_id=command.work.work_id, input=b"x", obligation_id=ob), CONTEXT)
    h.workers.start("original", CONTEXT)
    h.workers.observe("original", h.proof("original", "succeeded"), CONTEXT)
    delivery = h.broker(h.store, replace(h.registration, verify_work_result=h.workers.verify_work_result))
    result = WorkerConsumer(h.workers, delivery).drain(lambda _: CONTEXT)
    assert result[0]["problem"] == "WORKER_RESULT_PENDING"
    assert h.workers.completions() == ("original",)
    assert h.data("delivery-work-state", ob)["state"] == "queued"
    assert len(h.executor.starts) == 1


class SQLiteBacking:
    """Persistent OFFLINE fake EGT authority, never a production store adapter."""

    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("CREATE TABLE IF NOT EXISTS authority (id INTEGER PRIMARY KEY, data BLOB)")
            db.execute("INSERT OR IGNORE INTO authority VALUES (1, ?)", (pickle.dumps(Backing()),))

    def get(self, table, partition, row):
        with sqlite3.connect(self.path, timeout=10) as db:
            backing = pickle.loads(db.execute("SELECT data FROM authority WHERE id=1").fetchone()[0])
        return backing.get(table, partition, row)

    def apply(self, table, operations):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            backing = pickle.loads(db.execute("SELECT data FROM authority WHERE id=1").fetchone()[0])
            result = backing.apply(table, operations)
            db.execute("UPDATE authority SET data=? WHERE id=1", (pickle.dumps(backing),))
            return result

    def seed(self, backing):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("UPDATE authority SET data=? WHERE id=1", (pickle.dumps(backing),))


class LocalProcessExecutor:
    """A synthetic durable business effect run in a real local worker process."""

    def __init__(self, path, crash=False):
        self.path, self.crash = str(path), crash
        self.evidence = {}
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("CREATE TABLE IF NOT EXISTS business (binding TEXT PRIMARY KEY, input BLOB)")

    def adapter(self):
        return ExecutorAdapter(self.start, self.query, self.stop, lambda key: self.evidence[key])

    def start(self, command):
        with sqlite3.connect(self.path, timeout=10) as db:
            # A duplicate invocation raises instead of hiding it with INSERT OR IGNORE.
            db.execute("INSERT INTO business VALUES (?, ?)", (command.binding.start_request_id, command.input))
        if self.crash:
            os._exit(23)
        return self.query(command.binding)

    def query(self, binding):
        with sqlite3.connect(self.path, timeout=10) as db:
            found = db.execute("SELECT input FROM business WHERE binding=?", (binding.start_request_id,)).fetchone()
        evidence = ExecutorEvidence(
            evidence_id=control_key("local-process-evidence", binding.start_request_id, str(bool(found))),
            binding=binding, sequence=2 if found else 1,
            native_execution_id=control_key("local-process", binding.start_request_id) if found else None,
            state="succeeded" if found else "unknown", entire_tree_idle=bool(found),
            all_inflight_starts_fenced=bool(found),
            proof=b"synthetic local durable effect and process completion; NOT Foundry proof",
        )
        self.evidence[evidence.evidence_id] = evidence
        return evidence.evidence_id

    def stop(self, command):
        return self.query(command.binding)


def process_worker(path, config_json, crash, start_event):
    store = BehaviorStore(FakeTable(SQLiteBacking(path)), SCOPE)
    executor = LocalProcessExecutor(path, crash)
    core = WorkerCore(
        store, WorkerContract.model_validate_json(config_json), adapter=executor.adapter(),
        mode="offline_contract", clock=lambda: NOW,
    )
    start_event.wait(15)
    try:
        core.start("job-one", CONTEXT)
    except Problem as exc:
        if exc.body["error"]["code"] in {"BEHAVIOR_CONFLICT", "BEHAVIOR_COMMIT_UNKNOWN"}:
            raise SystemExit(2)
        raise


def process_delivery(path, config_json, obligation):
    store = BehaviorStore(FakeTable(SQLiteBacking(path)), SCOPE)
    core = DeliveryCore(store, clock=lambda: NOW, registration=RuntimeRegistration(
        RuntimeContract.model_validate_json(config_json), mode="offline_contract",
    ))
    DeliveryConsumer(core).consume(obligation, CONTEXT, "fresh-process")


def persistent(h, path):
    backing = SQLiteBacking(path)
    backing.seed(h.client.backing)
    store = BehaviorStore(FakeTable(backing), SCOPE)
    executor = LocalProcessExecutor(path)
    core = WorkerCore(store, h.worker_contract, adapter=executor.adapter(), mode="offline_contract", clock=lambda: NOW)
    return core


def join(process):
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail("owned local test process failed to exit")


def test_real_process_loss_after_business_effect_is_reconciled_without_rerun(tmp_path):
    h = WorkerHarness()
    h.job()
    path = tmp_path / "authority.db"
    core = persistent(h, path)
    ctx = multiprocessing.get_context("spawn")
    event = ctx.Event()
    event.set()
    process = ctx.Process(target=process_worker, args=(str(path), h.worker_contract.model_dump_json(), True, event))
    process.start()
    join(process)
    assert process.exitcode == 23
    assert core.inspect("job-one").state == "starting"
    original = core.inspect("job-one").binding
    core.start("job-one", CONTEXT)
    assert core.inspect("job-one").state == "succeeded"
    assert core.inspect("job-one").binding == original
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM business").fetchone()[0] == 1


def test_two_real_local_worker_processes_share_one_start_reservation(tmp_path):
    h = WorkerHarness()
    h.job()
    path = tmp_path / "authority.db"
    core = persistent(h, path)
    ctx = multiprocessing.get_context("spawn")
    event = ctx.Event()
    processes = [ctx.Process(target=process_worker, args=(
        str(path), h.worker_contract.model_dump_json(), False, event,
    )) for _ in range(2)]
    for process in processes:
        process.start()
    event.set()
    for process in processes:
        join(process)
        assert process.exitcode in {0, 2}
    core.reconcile("job-one", CONTEXT)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM business").fetchone()[0] == 1
    assert core.inspect("job-one").state == "succeeded"


def test_real_restarted_delivery_process_reconstructs_saved_result_without_business_callback(tmp_path):
    h = WorkerHarness()
    ob, _, _ = h.admit()
    h.work_result(ob)
    path = tmp_path / "authority.db"
    core = persistent(h, path)
    ctx = multiprocessing.get_context("spawn")
    for _ in range(2):
        process = ctx.Process(target=process_delivery, args=(str(path), h.config.model_dump_json(), ob))
        process.start()
        join(process)
        assert process.exitcode == 0
    key = control_key("delivery-obligation", ob)
    row = authority_snapshot(core.store, (key,))[key].payload["data"]
    assert row["last_version"] == 1 and row["fulfillment"] == "pending"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM business").fetchone()[0] == 0
