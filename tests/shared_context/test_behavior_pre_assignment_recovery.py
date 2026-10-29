"""Real native claim producer stopped before assignment; offline transports."""
from datetime import timedelta
import json
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from shared_context import behavior_recovery as recovery
from shared_context.behavior_assistant import key as assistant_key
from shared_context.behavior_workers import worker_key
from shared_context.behavior_operations import control_key
from shared_context.behavior_recovery_history import validate_recovery_history
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.errors import Problem
from test_behavior_assistant_acquire_conflicts import native_acquire, current_rotation
from test_behavior_rotation_cutoff import two_rotations
from test_behavior_rotation_settlement_history import current_cancelled
from test_behavior_runtime_rotation import rotated
from test_m3_auth import jwk_client, signing_key


pytestmark = pytest.mark.parametrize("native_acquire", ["before-assignment"], indirect=True)


def service_for(c):
    scope = recovery.PreAssignmentRecoveryScope(
        recovery_id="missing-assignment-recovery", configuration_sha256=c.config.sha256,
        job_id=c.binding.job_id, launch_id=c.launch_id, model_request_sha256=None,
        operator_oid=c.config.identity.human_oid, expires_at=c.clock[0] + timedelta(minutes=10),
        recovery_phase="before_sdk_assignment")
    stops = []
    def stop(target, deadline):
        assert c.clock[0] < deadline
        assert c.native.workers.inspect(c.binding.job_id).cancel_requested
        stops.append(target)
        return recovery.PlatformStopObservation(
            target=target, endpoint=recovery.stop_endpoint(target),
            credential_client_id=c.config.identity.managed_identity_client_id,
            status_code=204, observed_at=c.clock[0])
    return recovery.ClaimedSdkRecovery(c.config, c.native, scope, stop,
        delivery=c.composition.runtime.dep.delivery, clock=lambda: c.clock[0]), stops


def test_real_claim_without_assignment_has_exact_no_model_recovery(native_acquire):
    c = native_acquire
    sid = assistant_key("session-id", c.starts[0].binding.start_request_id)
    assert c.native.launch(c.launch_id).phase == "claimed"
    assert c.store.read_snapshot((assistant_key("session", sid),))[assistant_key("session", sid)] is None
    service, stops = service_for(c)
    before = c.store.read_inventory()
    plan = service.plan(c.principal)
    approval = service.authorize(c.principal, plan, "explicit-no-assignment-cancel")
    result = service.execute(c.principal, approval)
    assert result["state"] == "cancelled" and result["model_outcome"] == "not_requested"
    assert plan.recovery_phase == "before_sdk_assignment"
    after = c.store.read_inventory()
    assert assistant_key("session", sid) not in after.rows
    assert not any(row_id == assistant_key("model-intent", sid, row.payload["data"]["request_sha256"])
                   for row_id, row in after.rows.items() if "request_sha256" in row.payload["data"])
    assert len(stops) == 1
    budget = control_key("budget", c.binding.owner.root_budget_id)
    assert after.rows[budget] == before.rows[budget]
    job = c.native.workers.inspect(c.binding.job_id)
    assert job.result_forwarded and job.cancel_requested
    pool = c.native.workers._pool(c.store.read_snapshot((worker_key("pool", c.config.workers.workspace_id),)))
    assert not pool.pending and not pool.completions and all(slot.binding is None for slot in pool.slots)
    evidence = c.native.verify(result["platform_stop_evidence_id"])
    assert not evidence.result.unresolved_operations
    assert json.loads(evidence.result.content)["model_outcome"] == "not_requested"
    assert service.execute(c.principal, approval)["stop_resent"] is False and len(stops) == 1


def classified_current(c):
    """Use the actual installed-origin caller to select current history."""
    count = len(c.h.c.h.table.submissions)
    original = installed_origin(MigrationStore(c.h.c.h.table, c.store._scope), c.config)
    assert original == c.h.c.h.original
    assert len(c.h.c.h.table.submissions) == count


def test_missing_assignment_cancellation_delivers_and_becomes_retirable(native_acquire):
    from shared_context.behavior_settlement import RetainedResultSettlement, SettlementRegistration, SettlementUse
    from test_behavior_settlement import OfflineProtection, approve, fetch_command, ack_command
    from test_behavior_rotation_settlement_history import next_rotation
    c = native_acquire
    service, calls = service_for(c)
    plan = service.plan(c.principal)
    approval = service.authorize(c.principal, plan, "cancel-before-assignment")
    service.execute(c.principal, approval)
    classified_current(c)
    observer = OfflineProtection(c.clock[0], "native")
    registration = SettlementRegistration(
        settlement_id="missing-assignment-report", binding_id=c.binding.binding_id, configuration_sha256=c.config.sha256,
        expires_at=c.clock[0] + timedelta(minutes=10), protection_evidence_id="no-assignment-retention",
        protection_verifier_version="offline-retention-v1", protection_verifier=observer)
    settlement = RetainedResultSettlement(c.config, c.composition.runtime, registration)
    c.composition.runtime.dep.inbox.register_retained_results(settlement)
    grant = approve(settlement, c.principal)
    classified_current(c)
    fetched = settlement.fetch(c.principal, fetch_command(grant))
    assert b"before SDK assignment" in __import__("base64").urlsafe_b64decode(json.loads(fetched.payload)["content"])
    classified_current(c)
    receipt = settlement.acknowledge(c.principal, ack_command(grant, fetched))
    assert receipt.payload["result"]["fulfillment"] == "fulfilled"
    assert settlement.reconcile(c.principal, SettlementUse(authorization_id=grant.authorization_id)) == receipt
    classified_current(c)
    retired = SimpleNamespace(config=c.config, clock=c.clock, store=c.store,
                              first_plan=c.h.plan, h=c.h.c.h)
    rotation = next_rotation(retired, suffix="-after-unassigned")
    next_plan = rotation.plan(c.principal, rotation_id="after-unassigned-cancel")
    assert next_plan.liability.model_outcome == "not_requested" and next_plan.liability.model_request_sha256 is None
    assert len(calls) == 1


@pytest.mark.parametrize("present", ["assignment", "acquire-receipt", "issue-receipt", "model", "archive", "restore", "artifact", "result"])
def test_new_scope_rejects_any_existing_assignment_or_sdk_effect(native_acquire, present):
    from shared_context.behavior_store import ControlRequest, ControlChanges, ControlWrite
    from test_behavior_settlement import write
    c = native_acquire
    sid = assistant_key("session-id", c.starts[0].binding.start_request_id)
    if present == "assignment":
        c.authority.native_claim(c.binding.binding_id, c.launch_id, c.claim_command, c.agent)
    elif present.endswith("receipt"):
        key = assistant_key("acquire", sid, "sdk-start") if present == "acquire-receipt" else assistant_key("issue", sid)
        c.competitor.transact(ControlRequest(request_id=key, request_digest="f" * 64),
                             lambda _: ControlChanges(result={"synthetic-present": True}))
    else:
        digest = "e" * 64
        key = (assistant_key("model-intent", sid, digest) if present == "model" else
               assistant_key(present, c.binding.work.work_id if present in {"artifact", "result"} else sid))
        write(c.competitor, key, {"request_sha256": digest, "consumed": False} if present == "model" else {"present": True})
    service, stops = service_for(c)
    with pytest.raises(Problem):
        service.plan(c.principal)
    assert stops == [] and not c.native.workers.inspect(c.binding.job_id).cancel_requested


@pytest.mark.parametrize("phase", ["fence", "reserve", "before-stop", "seal"])
def test_assignment_race_is_fenced_at_each_recovery_boundary(native_acquire, monkeypatch, phase):
    c = native_acquire
    service, stops = service_for(c)
    plan = service.plan(c.principal)
    approval = service.authorize(c.principal, plan, "race-approved")
    original = c.store.transact
    sid = assistant_key("session-id", c.starts[0].binding.start_request_id)
    selected = {
        "fence": recovery.recovery_key("cancel-before-assignment", plan.recovery_id),
        "reserve": recovery.recovery_key("stop-intent", plan.recovery_id),
        "before-stop": recovery.recovery_key("stop-intent", plan.recovery_id),
        "seal": recovery.recovery_key("sealed", plan.recovery_id),
    }[phase]
    injected = []
    def inject():
        from test_behavior_settlement import write
        if injected:
            return
        injected.append(phase)
        if phase == "fence":
            c.authority.native_claim(c.binding.binding_id, c.launch_id, c.claim_command, c.agent)
        else:
            # Once fenced, the real assignment producer must reject. A corrupt
            # external state write still cannot be treated as preserved absence.
            with pytest.raises(Problem):
                c.authority.native_claim(c.binding.binding_id, c.launch_id, c.claim_command, c.agent)
            write(c.competitor, assistant_key("session", sid), {"unexpected": True})
    def transact(request, decide, **kwargs):
        if request.request_id == selected and phase != "before-stop":
            inject()
        result = original(request, decide, **kwargs)
        if request.request_id == selected and phase == "before-stop":
            inject()
        return result
    monkeypatch.setattr(c.store, "transact", transact)
    with pytest.raises(Problem):
        service.execute(c.principal, approval)
    assert injected == [phase] and len(stops) == (1 if phase == "seal" else 0)
    assert c.native.launch(c.launch_id).phase != "sealed"


@pytest.mark.parametrize("stage", ["stop-intent", "stop-observation"])
def test_missing_assignment_unknown_commit_never_resends_platform_stop(native_acquire, monkeypatch, stage):
    c = native_acquire
    service, calls = service_for(c)
    approval = service.authorize(c.principal, service.plan(c.principal), "unknown-stop-approved")
    original = c.store.transact
    def transact(request, decide, **kwargs):
        result = original(request, decide, **kwargs)
        if request.request_id == recovery.recovery_key(stage, service.scope.recovery_id):
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "synthetic response loss")
        return result
    monkeypatch.setattr(c.store, "transact", transact)
    with pytest.raises(Problem):
        service.execute(c.principal, approval)
    monkeypatch.setattr(c.store, "transact", original)
    assert service.execute(c.principal, approval)["stop_resent"] is False
    assert len(calls) == (0 if stage == "stop-intent" else 1)
    if stage == "stop-observation":
        assert service.finish(c.principal, approval)["state"] == "cancelled"
        classified_current(c)
        assert len(calls) == 1


@pytest.mark.parametrize("fault", ["read-unavailable", "missing-claim", "wrong-claim-hash"])
def test_exact_read_and_original_claim_receipt_are_mandatory(native_acquire, monkeypatch, fault):
    c = native_acquire
    service, calls = service_for(c)
    original = c.store.read_inventory
    def inventory():
        if fault == "read-unavailable":
            raise Problem(503, "BEHAVIOR_READ_FAILED", "synthetic unavailable inventory")
        value = original()
        receipts = dict(value.receipts)
        key = next(key for key, row in receipts.items() if row.payload["result"] == {"claim_id": c.native_claim.assignment.claim_id})
        if fault == "missing-claim":
            receipts.pop(key)
        else:
            row = receipts[key]
            payload = copy.deepcopy(row.payload)
            payload["request"]["request_digest"] = "f" * 64
            receipts[key] = replace(row, payload=payload)
        return replace(value, receipts=receipts)
    monkeypatch.setattr(c.store, "read_inventory", inventory)
    with pytest.raises(Problem):
        service.plan(c.principal)
    assert not calls


@pytest.mark.parametrize("change", ["principal", "expired", "target", "claim", "scope"])
def test_missing_assignment_recovery_never_waives_original_identity_or_approval(native_acquire, change):
    from test_behavior_settlement import write
    from shared_context.behavior_foundry import native_key
    c = native_acquire
    service, calls = service_for(c)
    principal = c.principal
    if change == "principal":
        principal = replace(principal, oid=c.binding.agent_oid, kind="agent")
    elif change == "expired":
        c.clock[0] = service.scope.expires_at
    elif change in {"target", "claim"}:
        row_id = native_key("launch", c.launch_id)
        row = c.competitor.read_snapshot((row_id,))[row_id]
        data = copy.deepcopy(row.payload["data"])
        if change == "target":
            data["target"]["session_id"] = "wrong-target"
        else:
            data["claim_id"] = "different-claim"
        write(c.competitor, row_id, data)
    else:
        with pytest.raises(Problem):
            recovery.ClaimedSdkRecovery(c.config, c.native,
                service.scope.model_copy(update={"job_id": "wrong-job"}), service.stop,
                delivery=service.delivery, clock=service.clock)
        assert not calls
        return
    with pytest.raises(Problem):
        service.plan(principal)
    assert not calls
