"""Offline post-transition readiness regression; not native readiness evidence."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from shared_context.behavior_operations import control_key
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite, InlineMaterial
from shared_context.behavior_workers import ExecutorAdapter, ExecutorBinding, WorkerCore, WorkerReadiness, worker_key
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_runtime_transition import transition_fixture
from test_behavior_server import NOW


def worker(store, config, evidence, *, now=NOW):
    def no_execution(*_):
        raise AssertionError("readiness publication must never invoke an executor")

    adapter = ExecutorAdapter(no_execution, no_execution, no_execution, no_execution, evidence.__getitem__)
    return WorkerCore(store, config.workers, adapter=adapter, clock=lambda: now)


def witness(core, config, identifier, observed_at):
    return WorkerReadiness(
        evidence_id=identifier, configuration_digest=core.configuration_digest(),
        control_binding=config.workers.control_binding, protection_version=config.workers.protection_version,
        executor_id=config.workers.executor_id, executor_version=config.workers.executor_version,
        native_binding_reference=config.deployment_id, supervisor_reference=config.controller_id,
        protection_reference=config.sources.protection.generation,
        observed_at=observed_at, expires_at=NOW + timedelta(minutes=25),
        proof=b"offline verified-readiness fixture, not a native observation",
    )


def prepared(*, apply_transition=True):
    transition, store, table, original, old, new, principal = transition_fixture()
    evidence = {}
    previous = worker(store, old, evidence)
    old_proof = witness(previous, old, "old-worker-readiness", NOW - timedelta(minutes=1))
    evidence[old_proof.evidence_id] = old_proof
    previous.publish_readiness(old_proof.evidence_id)
    before = store.read_inventory()

    plan = transition.plan(principal, transition_id="readiness-transition")
    authorization = transition.authorize(
        principal, plan, decision_id="readiness-transition-approved", expires_at=NOW + timedelta(minutes=10),
    )
    if apply_transition:
        transition.apply(principal, plan, authorization)
    writer = MigrationStore(table, store._scope)
    assert installed_origin(writer, new if apply_transition else old) == original
    current = worker(writer, new, evidence, now=NOW + timedelta(seconds=1))
    proof = witness(current, new, "new-worker-readiness", NOW + timedelta(seconds=1))
    evidence[proof.evidence_id] = proof
    return store, writer, table, original, new, current, old_proof, proof, before


def test_post_transition_readiness_advances_the_same_pointer_without_changing_old_evidence_or_budget():
    store, writer, table, original, new, current, old_proof, proof, before = prepared()
    pointer = worker_key("readiness", new.workers.workspace_id)
    prior = store.read_inventory()
    with pytest.raises(Problem) as stale:
        current._ready()
    assert stale.value.body["error"]["code"] == "WORKER_CONFIGURATION_MISMATCH"

    receipt = current.publish_readiness(proof.evidence_id, supersedes=old_proof.evidence_id)

    assert receipt.payload["result"]["readiness_evidence_id"] == proof.evidence_id
    after = writer.read_inventory()
    assert after.rows[pointer].payload["data"] == proof.model_dump(mode="json")
    for key, row in before.rows.items():
        if row.payload["data"].get("kind") == "operation_budget" or key == worker_key("readiness-evidence", old_proof.evidence_id):
            assert after.rows[key] == row
    for key, row in prior.rows.items():
        if key != pointer:
            assert after.rows[key] == row
    assert all(after.receipts[key] == row for key, row in prior.receipts.items())
    current._ready()
    assert installed_origin(MigrationStore(table, store._scope), new) == original


def test_new_readiness_without_the_approved_pool_transition_remains_fenced():
    store, _, _, _, _, current, old_proof, proof, _ = prepared(apply_transition=False)
    before = store.read_inventory().entity_sha256
    with pytest.raises(Problem) as denied:
        current.publish_readiness(proof.evidence_id, supersedes=old_proof.evidence_id)
    assert denied.value.body["error"]["code"] == "WORKER_CONFIGURATION_MISMATCH"
    assert store.read_inventory().entity_sha256 == before


@pytest.mark.parametrize("mutation", ["pending", "completions", "occupied", "lease", "predecessor", "retained-data", "wrong-id", "stale"])
def test_supersession_rejects_unfinished_work_or_nonexact_history_without_any_write(mutation):
    store, writer, _, _, new, current, old_proof, proof, _ = prepared()
    predecessor_id = old_proof.evidence_id
    if mutation in {"pending", "completions", "occupied", "lease"}:
        key = worker_key("pool", new.workers.workspace_id)
        row = writer.read_snapshot((key,))[key]
        data = dict(row.payload["data"])
        if mutation == "occupied":
            binding = ExecutorBinding(
                job_id="unfinished-work", work_id="unfinished-result", generation=1,
                owner=new.bindings[0].owner, executor_id=new.workers.executor_id,
                executor_version=new.workers.executor_version, input_digest="a" * 64,
                start_request_id="unfinished-start",
            )
            data["slots"] = [{"number": 0, "binding": binding.model_dump(mode="json"), "lease_until": None}]
        elif mutation == "lease":
            data["slots"] = [{"number": 0, "binding": None, "lease_until": NOW.isoformat()}]
        else:
            data[mutation] = ["unfinished-work"]
    elif mutation in {"predecessor", "retained-data"}:
        key = worker_key("readiness", new.workers.workspace_id) if mutation == "predecessor" else worker_key(
            "readiness-evidence", old_proof.evidence_id,
        )
        row = writer.read_snapshot((key,))[key]
        data = {**row.payload["data"], "supervisor_reference": "different-supervisor"}
    elif mutation == "wrong-id":
        predecessor_id = "not-the-retained-predecessor"
    else:
        current.adapter = replace(
            current.adapter, verify_readiness=lambda _: proof.model_copy(update={"observed_at": old_proof.observed_at}),
        )
    if mutation not in {"wrong-id", "stale"}:
        writer.transact(ControlRequest(request_id=control_key("readiness-test-mutation", mutation),
                                       request_digest=digest(data), read_set=(key,)),
                        lambda _: ControlChanges(writes=(ControlWrite(
                            row_id=key, mode="replace", data=data,
                            material=None if row.payload["material"] is None else InlineMaterial.model_validate_json(
                                json.dumps(row.payload["material"]),
                            ),
                        ),), result={}))
    before = store.read_inventory().entity_sha256
    with pytest.raises(Problem):
        current.publish_readiness(proof.evidence_id, supersedes=predecessor_id)
    assert store.read_inventory().entity_sha256 == before


def test_supersession_ack_loss_reconciles_original_read_set_without_replacing_again():
    store, writer, table, _, _, current, old_proof, proof, _ = prepared()
    table.failure = "after"
    with pytest.raises(Problem) as unknown:
        current.publish_readiness(proof.evidence_id, supersedes=old_proof.evidence_id)
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    table.failure = None
    before = store.read_inventory().entity_sha256
    receipt = current.publish_readiness(proof.evidence_id, supersedes=old_proof.evidence_id)
    assert receipt.payload["result"]["readiness_evidence_id"] == proof.evidence_id
    assert writer.read_inventory().entity_sha256 == before


def test_same_contract_renewal_keeps_legacy_request_shape_and_idempotency():
    _, writer, _, _, new, current, old_proof, proof, _ = prepared()
    current.publish_readiness(proof.evidence_id, supersedes=old_proof.evidence_id)
    renewal = proof.model_copy(update={"evidence_id": "renewed-worker-readiness",
                                       "observed_at": NOW + timedelta(seconds=2)})
    current.clock = lambda: NOW + timedelta(seconds=2)
    current.adapter = replace(current.adapter, verify_readiness=lambda _: renewal)
    receipt = current.publish_readiness(renewal.evidence_id)
    assert receipt.payload["request"]["read_set"] == [worker_key("readiness", new.workers.workspace_id)]
    before = writer.read_inventory().entity_sha256
    assert current.publish_readiness(renewal.evidence_id) == receipt
    assert writer.read_inventory().entity_sha256 == before


def test_late_pending_work_wins_atomically_over_readiness_supersession():
    _, writer, table, _, new, current, old_proof, proof, _ = prepared()
    pointer = worker_key("readiness", new.workers.workspace_id)
    before = writer.read_snapshot((pointer,))[pointer]
    pool_key = worker_key("pool", new.workers.workspace_id)

    def late_work():
        row = writer.read_snapshot((pool_key,))[pool_key]
        data = {**row.payload["data"], "pending": ["concurrent-work"]}
        writer.transact(
            ControlRequest(request_id="late-work", request_digest=digest(data), read_set=(pool_key,)),
            lambda _: ControlChanges(writes=(ControlWrite(row_id=pool_key, mode="replace", data=data),), result={}),
        )

    table.before_submit = late_work
    with pytest.raises(Problem) as conflict:
        current.publish_readiness(proof.evidence_id, supersedes=old_proof.evidence_id)
    assert conflict.value.body["error"]["code"] == "BEHAVIOR_CONFLICT"
    evidence_key = worker_key("readiness-evidence", proof.evidence_id)
    assert writer.read_snapshot((pointer,))[pointer] == before
    assert writer.read_snapshot((evidence_key,))[evidence_key] is None
