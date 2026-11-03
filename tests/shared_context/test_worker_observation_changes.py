"""What one verified executor report may change on a job, its slot and its pool; offline contract fixtures only."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import timedelta

import pytest

from shared_context.behavior_assistant_wire import Checkpoint, Task
from shared_context.behavior_wire import ExactSourceRef
from shared_context.behavior_operations import control_key
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite, InlineMaterial
from shared_context.behavior_workers import JobSpec, _digest, worker_key
from shared_context.errors import Problem
from test_behavior_store import code
from test_behavior_workers import CONTEXT, WorkerHarness


def rewrite(h, key, change):
    """Replace one worker row's data after `change(data)`, keeping its retained material."""
    h.serial += 1
    name = f"rewrite-{h.serial}"
    request = ControlRequest(request_id=name, request_digest=hashlib.sha256(name.encode()).hexdigest(), read_set=(key,))

    def decide(snapshot):
        row = snapshot[key].payload
        data = copy.deepcopy(row["data"])
        change(data)
        material = None if row["material"] is None else InlineMaterial.model_validate_json(json.dumps(row["material"]))
        return ControlChanges(writes=(ControlWrite(row_id=key, mode="replace", data=data, material=material),),
                              result={"synthetic_only": True})

    return h.store.transact(request, decide)


def started(h, name="job-one", **changes):
    h.job(name, **changes)
    h.workers.start(name, CONTEXT)
    return h.workers.inspect(name)


def later(h, job, state, **values):
    """Evidence for the job's own binding that is newer than everything seen so far."""
    return h.executor.attest(job.binding, state, sequence=h.executor.serial + 10, **values)


def status(receipt):
    return receipt.payload["result"]["status"]


def retained(h, evidence_id):
    key = worker_key("evidence", evidence_id)
    return h.store.read_snapshot((key,))[key]


def pool_row(h):
    key = worker_key("pool", h.worker_contract.workspace_id)
    return h.store.read_snapshot((key,))[key].payload["data"]


def test_evidence_for_a_parked_parent_is_retained_without_moving_the_job():
    h = WorkerHarness()
    parent = started(h, "parent")
    h.job("child", parent="parent")
    h.workers.observe("parent", h.proof("parent", "parked", checkpoint=b"checkpoint"), CONTEXT)
    parked = h.workers.inspect("parent")
    assert parked.state == "waiting_dependency"
    evidence_id = later(h, parent, "running", native_execution_id=parked.native_execution_id)
    receipt = h.workers.observe("parent", evidence_id, CONTEXT)
    assert status(receipt) == "late_evidence_retained" and h.workers.inspect("parent") == parked
    assert retained(h, evidence_id) is not None


def test_evidence_after_a_terminal_report_is_retained_without_moving_the_job():
    h = WorkerHarness()
    job = started(h)
    h.workers.observe("job-one", h.proof("job-one", "succeeded"), CONTEXT)
    done = h.workers.inspect("job-one")
    evidence_id = later(h, job, "running", native_execution_id=done.native_execution_id)
    receipt = h.workers.observe("job-one", evidence_id, CONTEXT)
    assert done.state == "succeeded" and status(receipt) == "late_evidence_retained"
    assert retained(h, evidence_id) is not None
    assert h.workers.inspect("job-one") == done and h.occupied() == []


def test_only_a_newer_sequence_moves_the_job():
    h = WorkerHarness()
    job = started(h)
    h.workers.observe("job-one", later(h, job, "running", native_execution_id=job.native_execution_id), CONTEXT)
    seen = h.workers.inspect("job-one")
    for sequence in (seen.last_sequence, seen.last_sequence - 1):
        stale = h.executor.attest(job.binding, "unknown", sequence=sequence)
        assert status(h.workers.observe("job-one", stale, CONTEXT)) == "late_evidence_retained"
    assert h.workers.inspect("job-one") == seen
    newer = h.executor.attest(job.binding, "unknown", sequence=seen.last_sequence + 1)
    assert status(h.workers.observe("job-one", newer, CONTEXT)) == "capacity_uncertain"
    assert h.workers.inspect("job-one").last_sequence == seen.last_sequence + 1


def test_the_native_execution_id_may_be_learned_or_repeated_but_never_changed():
    h = WorkerHarness()
    job = started(h)
    first = h.workers.inspect("job-one").native_execution_id
    assert first is not None
    h.workers.observe("job-one", later(h, job, "unknown", native_execution_id=None), CONTEXT)
    assert h.workers.inspect("job-one").native_execution_id == first
    h.workers.observe("job-one", later(h, job, "running", native_execution_id=first), CONTEXT)
    with pytest.raises(Problem) as changed:
        h.workers.observe("job-one", later(h, job, "running", native_execution_id="another-executor"), CONTEXT)
    assert code(changed) == "WORKER_NATIVE_BINDING_DENIED" and h.workers.inspect("job-one").native_execution_id == first


@pytest.mark.parametrize("held", ["nowhere", "twice"])
def test_a_report_for_a_permit_that_is_not_uniquely_held_is_refused(held):
    h = WorkerHarness(capacity=4)
    job = started(h)
    if held == "nowhere":
        rewrite(h, worker_key("pool", h.worker_contract.workspace_id),
                lambda data: data["slots"][0].update(binding=None, lease_until=None))
    else:
        rewrite(h, worker_key("pool", h.worker_contract.workspace_id),
                lambda data: data["slots"][1].update(binding=data["slots"][0]["binding"], lease_until=data["slots"][0]["lease_until"]))
    with pytest.raises(Problem) as lost:
        h.workers.observe("job-one", later(h, job, "running", native_execution_id=job.native_execution_id), CONTEXT)
    assert code(lost) == "WORKER_SLOT_INVALID" and h.workers.inspect("job-one").state == "running"


def test_a_report_does_not_need_the_execution_policy_to_still_be_active():
    h = WorkerHarness()
    started(h)
    h.job("job-two")
    h.mutate("policy", CONTEXT.policy_id, active=False)
    with pytest.raises(Problem) as refused:
        h.workers.start("job-two", CONTEXT)
    assert code(refused) == "WORKER_POLICY_DENIED"
    receipt = h.workers.observe("job-one", h.proof("job-one", "succeeded"), CONTEXT)
    assert status(receipt) == "succeeded"


def test_a_report_from_another_scope_is_refused_even_when_the_owner_record_matches_it():
    h = WorkerHarness()
    started(h)
    h.mutate("owner", CONTEXT.owner_record_id, conversation_id="other-conversation")
    elsewhere = CONTEXT.model_copy(update={"conversation_id": "other-conversation"})
    with pytest.raises(Problem) as fenced:
        h.workers.observe("job-one", h.proof("job-one", "succeeded"), elsewhere)
    assert code(fenced) == "WORKER_JOB_FENCED"


def assistant_task(job_id, work_id):
    return Task(job_id=job_id, work_id=work_id, definition_digest="a" * 64, profile_digest="b" * 64,
                source=ExactSourceRef(source_id="source-one", version=1, sha256="c" * 64, byte_length=12),
                artifact_id="result-one", artifact_version="d" * 32, operation_id="operation-one")


def checkpoint_for(task, **changes):
    values = dict(session_id="e" * 64, task_digest=_digest(task.model_dump(mode="json")), archive_digest="f" * 64)
    return Checkpoint(**{**values, **changes}).model_dump_json().encode()


def assistant_job(h, task, name="solo"):
    spec = JobSpec(job_id=name, work_id="work-" + name, input=task.model_dump_json().encode())
    h.workers.enqueue(spec, CONTEXT)
    h.workers.start(name, CONTEXT)
    return h.workers.inspect(name)


def test_a_childless_job_may_park_only_on_its_own_assistant_checkpoint():
    h = WorkerHarness()
    task = assistant_task("solo", "work-solo")
    job = assistant_job(h, task)
    units = job.control_remaining
    assert units == h.worker_contract.control_units - 1
    h.workers.observe("solo", h.proof("solo", "parked", checkpoint=checkpoint_for(task)), CONTEXT)
    parked = h.workers.inspect("solo")
    assert parked.state == "waiting_dependency" and parked.checkpoint == checkpoint_for(task)
    assert parked.control_remaining == units - 1 and h.occupied() == []


@pytest.mark.parametrize("case", ["other_job", "other_work", "other_task_digest", "not_a_checkpoint", "not_a_task"])
def test_a_childless_job_cannot_park_on_anything_else(case):
    h = WorkerHarness()
    task = assistant_task("solo", "work-solo")
    if case == "other_job":
        spec_task = assistant_task("someone-else", "work-solo")
    elif case == "other_work":
        spec_task = assistant_task("solo", "someone-elses-work")
    else:
        spec_task = task
    if case == "not_a_task":
        h.workers.enqueue(JobSpec(job_id="solo", work_id="work-solo", input=b"not a task"), CONTEXT)
        h.workers.start("solo", CONTEXT)
    else:
        h.workers.enqueue(JobSpec(job_id="solo", work_id="work-solo", input=spec_task.model_dump_json().encode()), CONTEXT)
        h.workers.start("solo", CONTEXT)
    proof_checkpoint = {
        "other_task_digest": checkpoint_for(task, task_digest="0" * 64), "not_a_checkpoint": b"not json",
    }.get(case, checkpoint_for(spec_task))
    with pytest.raises(Problem) as refused:
        h.workers.observe("solo", h.proof("solo", "parked", checkpoint=proof_checkpoint), CONTEXT)
    assert code(refused) == "WORKER_PARK_DENIED"
    assert h.workers.inspect("solo").state in {"starting", "running"} and len(h.occupied()) == 1


def test_parking_and_unfinished_reports_spend_the_control_budget_and_stop_when_it_is_gone():
    h = WorkerHarness()
    parent = started(h, "parent")
    h.job("child", parent="parent")
    units = parent.control_remaining
    h.workers.observe("parent", later(h, parent, "unknown"), CONTEXT)
    after_unknown = h.workers.inspect("parent")
    assert after_unknown.state == "capacity_uncertain" and after_unknown.control_remaining == units - 1
    h.workers.observe("parent", later(h, parent, "running", native_execution_id=after_unknown.native_execution_id), CONTEXT)
    after_running = h.workers.inspect("parent")
    assert after_running.state == "running" and after_running.control_remaining == units - 2
    rewrite(h, worker_key("job", "parent"), lambda data: data.update(control_remaining=0))
    with pytest.raises(Problem) as spent:
        h.workers.observe("parent", later(h, parent, "unknown"), CONTEXT)
    assert code(spent) == "WORKER_CONTROL_EXHAUSTED"
    with pytest.raises(Problem) as no_park:
        h.workers.observe("parent", later(h, parent, "parked", checkpoint=b"checkpoint"), CONTEXT)
    assert code(no_park) == "WORKER_PARK_DENIED"
    assert h.workers.inspect("parent").state == "running" and len(h.occupied()) == 1


def test_a_finished_job_spends_its_one_completion_credit_and_never_a_second():
    h = WorkerHarness()
    job = started(h)
    assert job.completion_credit == 1
    h.workers.observe("job-one", h.proof("job-one", "stopped"), CONTEXT)
    done = h.workers.inspect("job-one")
    assert done.state == "cancelled" and done.completion_credit == 0
    other = WorkerHarness()
    started(other)
    rewrite(other, worker_key("job", "job-one"), lambda data: data.update(completion_credit=0))
    with pytest.raises(Problem) as spent:
        other.workers.observe("job-one", other.proof("job-one", "failed"), CONTEXT)
    assert code(spent) == "WORKER_COMPLETION_EXHAUSTED" and len(other.occupied()) == 1


@pytest.mark.parametrize("state", ["succeeded", "failed", "stopped"])
def test_a_finished_report_releases_only_its_permit_and_leaves_pending(state):
    h = WorkerHarness(capacity=4)
    started(h, "first")
    started(h, "second")
    h.job("waiting")
    assert len(h.occupied()) == 2
    receipt = h.workers.observe("first", h.proof("first", state), CONTEXT)
    assert status(receipt) == ("cancelled" if state == "stopped" else state)
    held = [slot["binding"]["job_id"] for slot in h.occupied()]
    assert held == ["second"] and receipt.payload["result"]["completion_evidence_id"] is not None
    assert "first" not in h.workers.pending() and "second" in h.workers.pending()
    assert h.workers.completions() == () and h.workers.inspect("first").state == (
        "cancelled" if state == "stopped" else state)


def test_only_a_job_with_an_obligation_enters_the_completion_inbox():
    h = WorkerHarness()
    ob, command, _ = h.admit()
    h.workers.enqueue(JobSpec(job_id="owed", work_id=command.work.work_id, input=b"saved", obligation_id=ob), CONTEXT)
    h.workers.start("owed", CONTEXT)
    h.workers.observe("owed", h.proof("owed", "succeeded"), CONTEXT)
    assert h.workers.completions() == ("owed",) and h.workers.pending() == ()
    plain = WorkerHarness()
    started(plain)
    plain.workers.observe("job-one", plain.proof("job-one", "succeeded"), CONTEXT)
    assert plain.workers.completions() == () and plain.workers.pending() == ()


def test_parking_keeps_the_job_pending_and_frees_the_permit():
    h = WorkerHarness()
    started(h, "parent")
    h.job("child", parent="parent")
    h.workers.observe("parent", h.proof("parent", "parked", checkpoint=b"checkpoint"), CONTEXT)
    assert set(h.workers.pending()) == {"parent", "child"}
    assert h.occupied() == [] and h.workers.completions() == ()


@pytest.mark.parametrize("name, change", [
    ("cancel_requested", lambda data: data.update(cancel_requested=True)),
    ("no_longer_queued", lambda data: data.update(state="running")),
    ("generation_moved", lambda data: data.update(generation=1)),
    ("children_changed", lambda data: data.update(children=["intruder"])),
    ("checkpoint_changed", lambda data: data.update(checkpoint="Y2hhbmdlZA==")),
    ("spec_changed", lambda data: data["spec"].update(work_id="someone-elses-work")),
], ids=lambda value: value if isinstance(value, str) else None)
def test_a_start_is_fenced_when_the_job_moved_after_it_was_planned(name, change):
    h = WorkerHarness()
    h.job()
    real = h.workers.inspect

    def racing(job_id):
        job = real(job_id)
        rewrite(h, worker_key("job", job_id), change)
        return job

    h.workers.inspect = racing
    with pytest.raises(Problem) as fenced:
        h.workers.start("job-one", CONTEXT)
    assert code(fenced) == "WORKER_START_FENCED" and h.occupied() == [] and h.executor.starts == []


def test_a_start_beyond_the_generation_limit_is_fenced():
    h = WorkerHarness()
    h.job()
    rewrite(h, worker_key("job", "job-one"), lambda data: data.update(generation=h.worker_contract.max_generations))
    with pytest.raises(Problem) as fenced:
        h.workers.start("job-one", CONTEXT)
    assert code(fenced) == "WORKER_START_FENCED" and h.executor.starts == []


def native_core(h, **contract_changes):
    """A native-mode core over the harness store whose independent verifier answers from `proofs`."""
    from shared_context.behavior_workers import ExecutorAdapter, WorkerCore
    proofs = {}
    contract = h.worker_contract.model_copy(update=contract_changes)
    adapter = ExecutorAdapter(h.executor.start, h.executor.query, h.executor.stop, lambda key: h.executor.evidence[key],
                              verify_readiness=lambda evidence_id: proofs[evidence_id])
    return WorkerCore(h.store, contract, adapter=adapter, mode="native", clock=lambda: h.time), proofs


def readiness(core, proofs, key, minutes=0, ttl=30, **changes):
    from datetime import timedelta
    from shared_context.behavior_workers import WorkerReadiness
    observed = core.clock() + timedelta(minutes=minutes)
    values = dict(
        evidence_id=key, configuration_digest=core.configuration_digest(),
        control_binding=core.contract.control_binding, protection_version=core.contract.protection_version,
        executor_id=core.contract.executor_id, executor_version=core.contract.executor_version,
        native_binding_reference="binding-ref", supervisor_reference="supervisor-ref",
        protection_reference="protection-ref", observed_at=observed, expires_at=observed + timedelta(minutes=ttl),
        proof=b"offline readiness fixture, NOT native evidence",
    )
    proofs[key] = WorkerReadiness(**{**values, **changes})
    return key


def row_data(h, key):
    row = h.store.read_snapshot((key,))[key]
    return None if row is None else row.payload["data"]


POINTER = worker_key("readiness", "whole-workspace")
POOL = worker_key("pool", "whole-workspace")


def test_a_verified_readiness_is_retained_and_becomes_the_pointer():
    h = WorkerHarness()
    core, proofs = native_core(h)
    core.publish_readiness(readiness(core, proofs, "ready-1", minutes=-5))
    assert row_data(h, POINTER)["evidence_id"] == "ready-1"
    assert row_data(h, worker_key("readiness-evidence", "ready-1"))["evidence_id"] == "ready-1"
    core.publish_readiness(readiness(core, proofs, "ready-2", minutes=-1))
    assert row_data(h, POINTER)["evidence_id"] == "ready-2"
    assert row_data(h, worker_key("readiness-evidence", "ready-1")) is not None


def test_only_the_native_mode_with_a_verifier_may_publish_readiness():
    import dataclasses
    h = WorkerHarness()
    with pytest.raises(Problem) as offline:
        h.workers.publish_readiness("ready-1")
    assert code(offline) == "WORKER_NATIVE_DISABLED"
    core, proofs = native_core(h)
    core.adapter = dataclasses.replace(core.adapter, verify_readiness=None)
    with pytest.raises(Problem) as unverified:
        core.publish_readiness("ready-1")
    assert code(unverified) == "WORKER_NATIVE_DISABLED"


@pytest.mark.parametrize("field, value", [
    ("evidence_id", "someone-elses-evidence"), ("configuration_digest", "0" * 64), ("control_binding", "1" * 64),
    ("protection_version", "other-protection"), ("executor_id", "other-executor"), ("executor_version", "other-version")])
def test_a_readiness_that_does_not_match_the_contract_is_refused(field, value):
    h = WorkerHarness()
    core, proofs = native_core(h)
    readiness(core, proofs, "ready-1", **{field: value})
    with pytest.raises(Problem) as refused:
        core.publish_readiness("ready-1")
    assert code(refused) == "WORKER_NATIVE_DISABLED" and row_data(h, POINTER) is None


@pytest.mark.parametrize("minutes, ttl", [(1, 30), (-60, 30), (0, 1)])
def test_a_readiness_outside_its_validity_window_is_refused(minutes, ttl):
    h = WorkerHarness()
    core, proofs = native_core(h)
    readiness(core, proofs, "ready-1", minutes=minutes, ttl=ttl)
    if minutes == 0:
        h.time = h.time + timedelta(minutes=ttl)  # exactly at expiry: the window is half open
    with pytest.raises(Problem) as refused:
        core.publish_readiness("ready-1")
    assert code(refused) == "WORKER_NATIVE_DISABLED" and "expired" in refused.value.body["error"]["message"]


@pytest.mark.parametrize("minutes, accepted", [(-6, False), (-5, False), (-4, True)])
def test_readiness_cannot_move_backward_or_stand_still(minutes, accepted):
    h = WorkerHarness()
    core, proofs = native_core(h)
    core.publish_readiness(readiness(core, proofs, "ready-1", minutes=-5, ttl=60))
    readiness(core, proofs, "ready-2", minutes=minutes, ttl=60)
    if accepted:
        core.publish_readiness("ready-2")
        assert row_data(h, POINTER)["evidence_id"] == "ready-2"
    else:
        with pytest.raises(Problem) as stale:
            core.publish_readiness("ready-2")
        assert code(stale) == "WORKER_READINESS_STALE" and row_data(h, POINTER)["evidence_id"] == "ready-1"


def migrated(h, *, busy=False):
    """ready-1 was published for the first contract; the pool already belongs to the replacing contract."""
    first, first_proofs = native_core(h)
    first.publish_readiness(readiness(first, first_proofs, "ready-1", minutes=-5, ttl=60))
    if busy:
        h.job("busy")
        h.workers.start("busy", CONTEXT)
    second, proofs = native_core(h, queries_per_generation=9)
    rewrite(h, POOL, lambda data: data.update(configuration_digest=second.configuration_digest()))
    return first, first_proofs, second, proofs


def test_a_drained_migrated_pool_may_replace_exactly_the_retained_predecessor():
    h = WorkerHarness()
    first, first_proofs, second, proofs = migrated(h)
    second.publish_readiness(readiness(second, proofs, "ready-2"), supersedes="ready-1")
    assert row_data(h, POINTER)["evidence_id"] == "ready-2"
    assert row_data(h, worker_key("readiness-evidence", "ready-1")) is not None


@pytest.mark.parametrize("case, expected", [
    ("pending", "WORKER_MIGRATION_NOT_DRAINED"), ("completions", "WORKER_MIGRATION_NOT_DRAINED"),
    ("busy", "WORKER_MIGRATION_NOT_DRAINED"), ("binding_only", "WORKER_MIGRATION_NOT_DRAINED"),
    ("lease_only", "WORKER_MIGRATION_NOT_DRAINED"),
    ("unknown_predecessor", "WORKER_READINESS_PREDECESSOR_INVALID"),
    ("not_the_pointer", "WORKER_READINESS_PREDECESSOR_INVALID"),
    ("same_contract", "WORKER_READINESS_PREDECESSOR_INVALID"),
    ("retained_data_differs", "WORKER_READINESS_PREDECESSOR_INVALID"),
    ("retained_material_differs", "WORKER_READINESS_PREDECESSOR_INVALID"),
    ("not_newer", "WORKER_READINESS_STALE"), ("older", "WORKER_READINESS_STALE"),
    ("expired", "WORKER_NATIVE_DISABLED")])
def test_a_readiness_replacement_is_refused_unless_every_predecessor_check_holds(case, expected):
    h = WorkerHarness()
    first, first_proofs, second, proofs = migrated(h, busy=case in {"busy", "binding_only", "lease_only"})
    core, supersedes, minutes = second, "ready-1", 0
    if case == "pending":
        rewrite(h, POOL, lambda data: data.update(pending=["queued-job"]))
    elif case == "completions":
        rewrite(h, POOL, lambda data: data.update(completions=["done-job"]))
    elif case == "binding_only":
        rewrite(h, POOL, lambda data: (data.update(pending=[]), [slot.update(lease_until=None) for slot in data["slots"]]))
    elif case == "lease_only":
        rewrite(h, POOL, lambda data: (data.update(pending=[]), [slot.update(binding=None) for slot in data["slots"]]))
    elif case == "unknown_predecessor":
        supersedes = "never-published"
    elif case == "not_the_pointer":
        first.publish_readiness(readiness(first, first_proofs, "ready-1b", minutes=-4, ttl=60))
    elif case == "same_contract":
        core = first
        rewrite(h, POOL, lambda data: data.update(configuration_digest=first.configuration_digest()))
        proofs = first_proofs
    elif case == "retained_data_differs":
        rewrite(h, worker_key("readiness-evidence", "ready-1"), lambda data: data.update(proof="ZGlmZmVyZW50IHJldGFpbmVkIHByb29m"))
    elif case == "retained_material_differs":
        for key in (POINTER, worker_key("readiness-evidence", "ready-1")):
            rewrite(h, key, lambda data: data.update(proof="c2FtZSBkcmlmdCBvbiBib3RoIHJvd3M="))
    elif case == "not_newer":
        minutes = -5
    elif case == "older":
        minutes = -6
    elif case == "expired":
        minutes = -90
    readiness(core, proofs, "ready-2", minutes=minutes, ttl=30)
    with pytest.raises(Problem) as refused:
        core.publish_readiness("ready-2", supersedes=supersedes)
    assert code(refused) == expected
    assert row_data(h, POINTER)["evidence_id"] in {"ready-1", "ready-1b"}


def test_a_parked_parent_stays_parked_until_its_children_are_finished():
    h = WorkerHarness()
    started(h, "parent")
    h.job("child", parent="parent")
    h.workers.observe("parent", h.proof("parent", "parked", checkpoint=b"checkpoint"), CONTEXT)
    with pytest.raises(Problem) as pending:
        h.workers.start("parent", CONTEXT)
    assert code(pending) == "WORKER_CHILDREN_PENDING" and h.occupied() == []
    h.workers.cancel("child", CONTEXT)
    h.workers.start("parent", CONTEXT)
    assert h.workers.inspect("parent").generation == 2 and len(h.occupied()) == 1


def test_a_parked_parent_that_lost_its_checkpoint_cannot_restart():
    h = WorkerHarness()
    started(h, "parent")
    h.job("child", parent="parent")
    h.workers.observe("parent", h.proof("parent", "parked", checkpoint=b"checkpoint"), CONTEXT)
    h.workers.cancel("child", CONTEXT)
    rewrite(h, worker_key("job", "parent"), lambda data: data.update(checkpoint=None))
    with pytest.raises(Problem) as lost:
        h.workers.start("parent", CONTEXT)
    assert code(lost) == "WORKER_CHILDREN_PENDING" and h.occupied() == []


@pytest.mark.parametrize("change", [
    {"remaining_attempts": 0}, {"remaining_units": 0}, {"active": False}, {"root_budget_id": "another-root"}])
def test_a_start_needs_an_active_budget_of_its_own_with_an_attempt_and_units_left(change):
    h = WorkerHarness()
    h.job()
    h.mutate("budget", "root", **change)
    with pytest.raises(Problem) as spent:
        h.workers.start("job-one", CONTEXT)
    assert code(spent) == "WORKER_BUDGET_EXHAUSTED" and h.occupied() == [] and h.executor.starts == []


def test_a_start_spends_exactly_one_attempt_and_the_reservation_units():
    h = WorkerHarness(reservation_units=5)
    h.job()
    budget = h.data("budget", "root")
    h.mutate("budget", "root", remaining_units=4)
    with pytest.raises(Problem) as short:
        h.workers.start("job-one", CONTEXT)
    assert code(short) == "WORKER_BUDGET_EXHAUSTED"
    h.mutate("budget", "root", remaining_units=5, remaining_attempts=budget["remaining_attempts"])
    h.workers.start("job-one", CONTEXT)
    spent = h.data("budget", "root")
    assert (spent["remaining_attempts"], spent["remaining_units"]) == (budget["remaining_attempts"] - 1, 0)


def test_a_newer_owner_epoch_can_start_a_queued_job_only_after_adopting_it():
    h = WorkerHarness()
    h.job()
    h.mutate("owner", CONTEXT.owner_record_id, epoch=2)
    newer = CONTEXT.model_copy(update={"epoch": 2})
    with pytest.raises(Problem) as fenced:
        h.workers.start("job-one", newer)
    assert code(fenced) == "WORKER_JOB_FENCED" and h.executor.starts == []
    h.workers.adopt("job-one", newer)
    h.workers.start("job-one", newer)
    assert h.workers.inspect("job-one").owner.epoch == 2 and len(h.occupied()) == 1


def retain(h, name, *, material=None, **fields):
    """Save one operation record directly, with any field combination, together with its description material."""
    from shared_context.behavior_operations import OperationDescription, ResolvedResource, _Operation, control_key
    description = OperationDescription(
        adapter_id="configured-adapter", adapter_version="1", kind="request", method="PATCH",
        target="test-work://original", content=b"exact approved bytes",
        resources=(ResolvedResource(resource_id="test-work://original", version="1"),),
    )
    saved = description.model_dump_json().encode() if material is None else material
    digest = hashlib.sha256(saved).hexdigest()
    values = dict(
        operation_id=name, owner_record_id=CONTEXT.owner_record_id, conversation_id=CONTEXT.conversation_id,
        root_budget_id=CONTEXT.root_budget_id, description_digest=digest, adapter_contract_digest="1" * 64,
    )
    operation = _Operation(**{**values, **fields})
    h.store.transact(
        ControlRequest(request_id="retained-" + name, request_digest=hashlib.sha256(name.encode()).hexdigest()),
        lambda _: ControlChanges(writes=(ControlWrite(
            row_id=control_key("operation", name), mode="create", data=operation.model_dump(mode="json"),
            material=InlineMaterial(data=saved, sha256=digest),
        ),), result={"synthetic_only": True}),
    )


def completed():
    from shared_context.behavior_operations import ReceiptResult, ResolvedResource
    return ReceiptResult(receipt_id="receipt", resource=ResolvedResource(resource_id="test-work://original", version="v2"),
                         status="completed")


def effects(h, *operation_ids):
    from shared_context.behavior_delivery import authority_snapshot
    from shared_context.behavior_operations import control_key
    keys = tuple(control_key("operation", identifier) for identifier in operation_ids)
    return h.workers._operation_effects(authority_snapshot(h.store, keys), operation_ids, CONTEXT)


SETTLED = dict(effect_state="applied", business_state="succeeded", current_attempt_id="attempt")


@pytest.mark.parametrize("fields, known, unresolved", [
    ({}, False, False),
    (dict(SETTLED, successful_result=completed()), True, False),
    (dict(SETTLED, successful_result=completed(), business_state="pending"), True, True),
    (dict(SETTLED, successful_result=completed(), business_state="failed"), True, True),
    (dict(SETTLED, successful_result=completed(), current_attempt_id=None), True, True),
    (dict(SETTLED, successful_result=completed(), unsettled_attempt_ids=("other",)), True, True),
    (dict(SETTLED), True, True),
    (dict(SETTLED, successful_result=completed().model_copy(update={"status": "failed"})), True, True),
    (dict(effect_state="partial", business_state="succeeded", current_attempt_id="attempt",
          successful_result=completed()), False, True),
    (dict(effect_state="not_applied", current_attempt_id="attempt"), False, True),
    (dict(effect_state="not_applied"), False, True),
    (dict(effect_state="partial"), False, True),
    (dict(effect_state="unknown", current_attempt_id="attempt"), False, True),
    (dict(unsettled_attempt_ids=("pending-attempt",)), False, True),
    (dict(business_state="failed"), False, True),
    (dict(successful_result=completed()), False, True),
], ids=lambda value: None)
def test_a_retained_operation_is_known_when_applied_and_unresolved_unless_fully_settled(fields, known, unresolved):
    h = WorkerHarness()
    retain(h, "op-a", **fields)
    assert effects(h, "op-a") == (("op-a",) if known else (), ("op-a",) if unresolved else ())


def test_missing_records_are_skipped_and_records_of_another_shape_are_unresolved():
    h = WorkerHarness()
    h.store.transact(
        ControlRequest(request_id="opaque-operation", request_digest=hashlib.sha256(b"opaque").hexdigest()),
        lambda _: ControlChanges(writes=(ControlWrite(
            row_id=control_key("operation", "opaque"), mode="create", data={"operation_id": "opaque", "state": "future"}),),
            result={"synthetic_only": True}),
    )
    retain(h, "op-a", **dict(SETTLED, successful_result=completed()))
    assert effects(h, "absent", "opaque", "op-a") == (("op-a",), ("opaque",))


@pytest.mark.parametrize("fields, expected, status", [
    ({"owner_record_id": "another-owner"}, "WORKER_OPERATION_FENCED", 403),
    ({"conversation_id": "another-conversation"}, "WORKER_OPERATION_FENCED", 403),
    ({"root_budget_id": "another-root"}, "WORKER_OPERATION_FENCED", 403),
    ({"operation_id": "op-other"}, "WORKER_OPERATION_FENCED", 403),
    ({"description_digest": "2" * 64}, "WORKER_OPERATION_FENCED", 409),
])
def test_a_retained_operation_outside_this_scope_or_with_a_different_description_is_fenced(fields, expected, status):
    h = WorkerHarness()
    retain(h, "op-a", **fields)
    with pytest.raises(Problem) as fenced:
        effects(h, "op-a")
    assert code(fenced) == expected and fenced.value.status == status


def test_a_retained_description_that_is_not_an_operation_description_is_fenced():
    h = WorkerHarness()
    retain(h, "op-a", material=b"{ not an operation description")
    with pytest.raises(Problem) as fenced:
        effects(h, "op-a")
    assert code(fenced) == "WORKER_OPERATION_FENCED" and fenced.value.status == 409
