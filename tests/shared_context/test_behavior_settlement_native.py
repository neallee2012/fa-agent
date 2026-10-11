"""Native-mode BRANCH coverage on OFFLINE SDK transports, NOT native evidence.

The original namespace is provisioned in native mode by the existing synthetic
server fixture; no offline rows are relabelled. Completed job/launch evidence is
seeded explicitly. No Foundry start/query/stop/model request is performed.
"""

import hashlib
from datetime import timedelta

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_delivery import _digest, _material
from shared_context.behavior_foundry import _NativeLaunch, native_key
from shared_context.behavior_inbox import InboxFetch, VerifiedInboxAccess
from shared_context.behavior_operations import control_key
from shared_context.behavior_settlement import (
    RetainedResultSettlement, SettlementRegistration, SettlementUse,
)
from shared_context.behavior_store import ControlRequest
from shared_context.behavior_workers import ExecutorBinding, ExecutorEvidence, ExecutorStart, worker_key
from shared_context.errors import Problem
from test_behavior_server import Harness as NativeHarness
from test_behavior_settlement import OfflineProtection, ack_command, approve, fetch_command, human, terminal, write
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def native_retained(tmp_path, jwk_client, request):
    h = NativeHarness(tmp_path, jwk_client)
    try:
        h.activate()
        runtime, binding = h.runtime, h.config.bindings[0]
        principal = human(binding)
        from dataclasses import replace
        runtime.submit(binding.binding_id, wire.SubmitWork(request_id="existing-job"),
                       replace(principal, actions=principal.actions | {"submit"}))
        result = terminal(runtime, binding)
        job_key = worker_key("job", binding.job_id)
        job = runtime.store.read_snapshot((job_key,))[job_key].payload["data"]
        execution = ExecutorBinding(
            job_id=binding.job_id, work_id=binding.work.work_id, generation=1, owner=binding.owner,
            executor_id=h.config.workers.executor_id, executor_version=h.config.workers.executor_version,
            input_digest=hashlib.sha256(runtime.job_input(binding)).hexdigest(), start_request_id="original-start",
        )
        launch_id = native_key("launch-id", execution.start_request_id)
        sequence = 2 if getattr(request, "param", False) else 1
        latest = native_key("terminal-verification-id", launch_id, "2") if sequence == 2 else "sealed-evidence"
        write(runtime.store, job_key, {**job, "binding": execution.model_dump(mode="json"),
                                      "generation": 1, "last_evidence_id": latest, "last_sequence": sequence})
        completion = ExecutorEvidence(
            evidence_id=latest, binding=execution, sequence=sequence,
            native_execution_id="original-invocation", state="stopped",
            entire_tree_idle=True, all_inflight_starts_fenced=True, result=result,
            proof=b"OFFLINE verified terminal completion fixture",
        )
        write(runtime.store, worker_key("completion", binding.job_id, "1"), completion.model_dump(mode="json"),
              material=_material(completion.model_dump(mode="json")))
        launch = _NativeLaunch(
            launch_id=launch_id, configuration_digest=runtime.dep.workers.configuration_digest(),
            api_binding_id=binding.binding_id, grant_id="old-native-grant",
            start=ExecutorStart(binding=execution, request=ControlRequest(
                request_id="original-start", request_digest=_digest(execution.model_dump(mode="json")),
            ), input=runtime.job_input(binding)), definition=binding.work, target=h.config.targets[0],
            process_generation=1, permit_digest="a" * 64, post_digest="b" * 64,
            phase="sealed", sealed_evidence_id="sealed-evidence", native_invocation_id="original-invocation",
            evidence_sequence=sequence,
        )
        write(runtime.store, native_key("launch", launch_id), launch.model_dump(mode="json"))
        policy = next(item.policy for item in h.config.policies if item.binding_id == binding.binding_id)
        h.time = max(h.time + timedelta(hours=2), policy.valid_until + timedelta(seconds=1))
        observer = OfflineProtection(h.time, mode="native")
        original_verify = observer.verify

        def verify(evidence_id, plan):
            proof = original_verify(evidence_id, plan)
            observer.proof = proof.model_copy(update={"authentication_version": h.config.authentication_version})
            return observer.proof

        observer.verify = verify
        registration = SettlementRegistration(
            settlement_id="exact-native-mode-fixture", binding_id=binding.binding_id,
            configuration_sha256=h.config.sha256, expires_at=h.time + timedelta(minutes=10),
            protection_evidence_id="offline-shaped-observation", protection_verifier_version="offline-retention-v1",
            protection_verifier=observer,
        )
        service = RetainedResultSettlement(h.config, runtime, registration)
        runtime.dep.inbox.register_retained_results(service)
        yield h, service, observer, principal
    finally:
        h.composition.close()


def test_native_terminal_receiver_does_not_borrow_worker_readiness(native_retained, monkeypatch):
    h, service, _, principal = native_retained
    with pytest.raises(Problem):
        h.runtime.dep.inbox.check_installation(h.runtime.dep.delivery)
    with pytest.raises(Problem):
        h.runtime.dep.workers._ready()
    with pytest.raises(Problem):
        h.runtime.authorize(service.binding.binding_id, principal, "read")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("worker-or-general-installation")
        pytest.fail("settlement must not acquire/renew worker or general inbox readiness")

    monkeypatch.setattr(h.runtime.dep.workers, "_ready", forbidden)
    monkeypatch.setattr(h.runtime.dep.inbox, "check_installation", forbidden)
    monkeypatch.setattr(h.runtime, "validate_startup", forbidden)
    monkeypatch.setattr(h.runtime, "resolve_owner", forbidden)
    monkeypatch.setattr(h.runtime.dep.sources, "read_exact", forbidden)
    grant = approve(service, principal)
    received = service.fetch(principal, fetch_command(grant))
    receipt = service.acknowledge(principal, ack_command(grant, received))
    assert receipt.payload["result"]["confirmed_levels"] == ["client_received"]
    assert receipt.payload["result"]["fulfillment"] == "fulfilled"
    assert service.reconcile(principal, SettlementUse(authorization_id=grant.authorization_id)) == receipt
    assert calls == [] and h.calls == []
    # The ordinary inbox still takes its full native gate; it cannot consume a settlement permit.
    access = VerifiedInboxAccess(
        evidence_id="ordinary-read", principal_id=principal.oid,
        authentication_version=h.config.authentication_version, binding=grant.plan.delivery,
        action="fetch", policy_revision=1, observed_at=h.time, expires_at=h.time + timedelta(seconds=60),
        proof=b"OFFLINE ordinary access fixture",
    )
    from dataclasses import replace
    h.runtime.dep.inbox.registration = replace(h.runtime.dep.inbox.registration, verify_access=lambda _: access)
    monkeypatch.setattr(h.runtime.dep.inbox, "check_installation", lambda _: (_ for _ in ()).throw(
        Problem(403, "GENERAL_INBOX_CLOSED", "full readiness remains expired"),
    ))
    with pytest.raises(Problem) as ordinary:
        h.runtime.dep.inbox.fetch(InboxFetch(
            obligation_id=grant.plan.delivery.obligation_id, delivery_id=grant.plan.delivery.delivery_id,
            access_evidence_id=access.evidence_id,
        ), grant.plan.context)
    assert ordinary.value.body["error"]["code"] == "GENERAL_INBOX_CLOSED"


@pytest.mark.parametrize("change", ["offline-evidence", "no-observation", "unsealed-launch", "wrong-material"])
def test_native_mode_cannot_manufacture_retention_or_terminality(native_retained, change):
    h, service, observer, principal = native_retained
    if change == "offline-evidence":
        observer.mode = "offline_contract"
    elif change == "no-observation":
        observer.deny = True
    elif change == "unsealed-launch":
        job = h.runtime.dep.workers.inspect(service.binding.job_id)
        key = native_key("launch", native_key("launch-id", job.binding.start_request_id))
        row = h.runtime.store.read_snapshot((key,))[key]
        write(h.runtime.store, key, {**row.payload["data"], "phase": "claimed"})
    else:
        plan = service.prepare(principal)
        observer.proof = observer.proof.model_copy(update={
            "retained_materials": tuple(item.model_copy(update={"sha256": "f" * 64}) for item in plan.retained_materials),
        })
    with pytest.raises(Problem):
        approve(service, principal)
    assert h.calls == []


@pytest.mark.parametrize("native_retained", [True], indirect=True)
def test_fresh_terminal_reverification_id_is_not_mistaken_for_an_unsealed_job(native_retained):
    h, service, _, principal = native_retained
    b = service.binding
    job = h.runtime.dep.workers.inspect(b.job_id)
    launch_id = native_key("launch-id", job.binding.start_request_id)
    launch_key = native_key("launch", launch_id)
    launch = h.runtime.store.read_snapshot((launch_key,))[launch_key].payload["data"]
    assert job.last_evidence_id != launch["sealed_evidence_id"]
    grant = approve(service, principal)
    received = service.fetch(principal, fetch_command(grant))
    assert service.acknowledge(principal, ack_command(grant, received)).payload["result"]["fulfillment"] == "fulfilled"
    assert h.calls == []


@pytest.mark.parametrize("field,value", [("last_evidence_id", "unrelated-evidence"), ("last_sequence", 99)])
def test_current_job_cannot_substitute_an_unretained_completion(native_retained, field, value):
    h, service, _, principal = native_retained
    key = worker_key("job", service.binding.job_id)
    row = h.runtime.store.read_snapshot((key,))[key]
    write(h.runtime.store, key, {**row.payload["data"], field: value})
    with pytest.raises(Problem) as denied:
        approve(service, principal)
    assert denied.value.body["error"]["code"] == "SETTLEMENT_RESULT_DENIED"
    assert h.calls == []
