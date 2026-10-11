"""Operator migration contract; all storage/native observations are offline fixtures."""

import json
import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest

from shared_context.behavior_auth import BehaviorPrincipal
from shared_context import behavior_delivery as d, behavior_workers as w
from shared_context import behavior_operations as op, source_backup as sb
from shared_context.behavior_delivery import DeliveryCore, PendingIndexRecord, QuotaRecord, RuntimeRegistration
from shared_context.behavior_inbox import InboxRegistration, PullInbox
from shared_context.behavior_migration import MigrationStore, ProtectionMigration, installation_record, installed_origin
from shared_context.behavior_operations import BudgetRecord, OwnerRecord, PolicyRecord, control_key
from shared_context.behavior_runtime import CommandPlan, RuntimeBinding, runtime_key, executor_job_input
from shared_context.behavior_server_config import ServerConfiguration, synthetic_configuration
from shared_context.behavior_store import BehaviorStore as LegacyStore, ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_workers import Slot, WorkerCore, _Pool, worker_key
from shared_context.errors import Problem
from shared_context.source_materials import (
    AuthenticatedSourceContext, SourceInput, SourceMaterials, _Preparation,
    InventoryProtectionVerifier, configuration_evidence_fingerprint, digest,
)
from shared_context.source_backup import RecoveryAuthorization, SourceBackupReference, SourceBackupService, SourceBackupSettings
from shared_context.backup_store import InMemoryBackupStore
from test_behavior_migration_store import InventoryTable
from test_behavior_server import NOW, observation
from test_behavior_store import SCOPE


def fixture(*, with_child=False):
    table = InventoryTable()
    store = MigrationStore(table, SCOPE)
    store.initialize()
    raw = synthetic_configuration().model_dump(mode="json")
    if with_child:
        raw["bindings"][0]["child_jobs"] = [{
            "job_id": "registered-child", "work": {**raw["bindings"][0]["work"], "work_id": "registered-child-work"},
        }]
        bound = RuntimeBinding.model_validate_json(json.dumps(raw["bindings"][0]))
        raw["policies"][0]["policy"]["binding_digest"] = digest(["fa-behavior-api-v1", "native", bound.model_dump(mode="json")])
    raw["table"]["scope"] = SCOPE.model_dump(mode="json")
    raw["workers"]["control_binding"] = raw["delivery"]["control_binding"] = store._binding
    for lane in ("sources", "backups"):
        raw[lane]["protection"]["authority_domain"] = SCOPE.authority_domain
    preliminary = ServerConfiguration.model_validate_json(json.dumps(raw))
    previous = {lane: observation(getattr(preliminary, lane), preliminary.evidence.inventory_scopes)
                for lane in ("sources", "backups")}
    for lane in previous:
        raw[lane]["protection"] = previous[lane].configuration.model_dump(mode="json")
    old = ServerConfiguration.model_validate_json(json.dumps(raw))
    fresh = {}
    for lane, old_observation in previous.items():
        fingerprint = configuration_evidence_fingerprint(
            blob_service_etag=old_observation.blob_service.etag,
            management_policy_etag=old_observation.management_policy.etag,
            blob_service_evidence=old_observation.blob_service, management_policy_evidence=old_observation.management_policy,
            automatic_delete_consumers=(), inventory_version="offline-inventory-v2",
        )
        fresh[lane] = old_observation.model_copy(update={
            "configuration": old_observation.configuration.model_copy(update={"evidence_sha256": fingerprint}),
            "inventory_version": "offline-inventory-v2",
        })
    proposed = old.model_dump(mode="json")
    for lane in fresh:
        proposed[lane]["protection"] = fresh[lane].configuration.model_dump(mode="json")
    new = ServerConfiguration.model_validate_json(json.dumps(proposed))
    worker = WorkerCore(store, old.workers, adapter=None, clock=lambda: NOW, mode="native")
    def no_native(*_):
        raise AssertionError("migration fixture must not invoke native delivery")
    inbox = PullInbox(store, InboxRegistration(old.authentication_version, no_native, no_native))
    delivery = DeliveryCore(store, clock=lambda: NOW,
                            registration=RuntimeRegistration(old.delivery, "native", channel_adapter=inbox))
    inbox.attach(delivery)
    delivery_digest = delivery.configuration_digest()
    key, installed = installation_record(old)
    rows = {
        key: installed,
        old.sources.protection.key: old.sources.protection.model_dump(mode="json"),
        control_key("source-backup-protection", SCOPE.authority_domain, old.backups.protection.container_url):
            old.backups.protection.model_dump(mode="json"),
        worker_key("pool", old.workers.workspace_id): _Pool(
            configuration_digest=worker.configuration_digest(), workspace_id=old.workers.workspace_id,
            slots=tuple(Slot(number=i) for i in range(old.workers.capacity)),
        ).model_dump(mode="json"),
    }
    for binding in old.bindings:
        rows[control_key("owner", binding.owner.owner_record_id)] = OwnerRecord(
            **binding.owner.model_dump(), active=True, operation_ids=(binding.operation_id,),
        ).model_dump(mode="json")
        rows[control_key("policy", binding.owner.policy_id)] = PolicyRecord(
            policy_id=binding.owner.policy_id, revision=1, active=True,
        ).model_dump(mode="json")
        rows[runtime_key("plan-index", binding.binding_id)] = {"keys": []}
    for policy in old.policies:
        rows[runtime_key("policy", policy.binding_id)] = policy.policy.model_dump(mode="json")
    for root in old.roots:
        rows[control_key("budget", root.root_budget_id)] = BudgetRecord(
            root_budget_id=root.root_budget_id, active=True, remaining_attempts=7, remaining_units=11,
        ).model_dump(mode="json")
        rows[control_key("delivery-index", root.root_budget_id)] = PendingIndexRecord(
            configuration_digest=delivery_digest, evidence_mode="native", root_budget_id=root.root_budget_id,
        ).model_dump(mode="json")
        rows[control_key("delivery-quota", "root", root.root_budget_id)] = QuotaRecord(
            configuration_digest=delivery_digest, evidence_mode="native", scope_kind="root",
            scope_id=root.root_budget_id, root_request_id=root.root_request_id,
            pending_limit=root.pending_limit, available_control_units=13, scans_remaining=3,
        ).model_dump(mode="json")
    for kind, value, limit in (("team", old.identity.team_id, old.team_pending_limit),
                               ("entry", old.delivery.entry_id, old.entry_pending_limit)):
        rows[control_key("delivery-quota", kind, value)] = QuotaRecord(
            configuration_digest=delivery_digest, evidence_mode="native", scope_kind=kind, scope_id=value,
            pending_limit=limit,
        ).model_dump(mode="json")
    budget, winner, proof = (control_key("server-authority-probe", old.sha256, kind)
                             for kind in ("budget", "winner", "proof"))
    rows[budget] = {"remaining": 1, "configuration_sha256": old.sha256}
    store.transact(ControlRequest(request_id="seed-migration", request_digest=digest(rows)),
                   lambda _: ControlChanges(writes=tuple(ControlWrite(row_id=k, mode="create", data=v)
                                                         for k, v in rows.items()), result={}))
    winning = ControlRequest(request_id=control_key("server-authority-probe-winner", old.sha256),
                             request_digest=digest(["authority-probe-winner", old.sha256]), read_set=(budget,))
    won = store.transact(winning, lambda _: ControlChanges(writes=(
        ControlWrite(row_id=budget, mode="replace", data={"remaining": 0, "configuration_sha256": old.sha256}),
        ControlWrite(row_id=winner, mode="create", data={"winner": True, "control_binding": store._binding}),
    ), result={}))
    proof_data = {
        "configuration_sha256": old.sha256, "control_binding": store._binding, "stale_batch_status": 412,
        "winner_request_id": winning.request_id,
        "loser_request_id": control_key("server-authority-probe-loser", old.sha256),
        "winner_receipt_sha256": digest(won.payload), "business_payload_sha256": dict(won.payload["writes"]),
    }
    store.transact(ControlRequest(request_id="seed-probe-proof", request_digest=digest(proof_data)),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id=proof, mode="create", data=proof_data),), result={}))
    principal = BehaviorPrincipal(old.identity.tenant_id, old.identity.human_oid, old.identity.team_id, "human", None,
                                  frozenset({"read", "approve"}))
    service = ProtectionMigration(
        store, old, new, {lane: item.model_dump_json().encode() for lane, item in fresh.items()},
        previous_observations={lane: item.model_dump_json().encode() for lane, item in previous.items()}, clock=lambda: NOW,
    )
    return service, store, table, old, new, principal


def terminal_root_and_child(store, config):
    binding = config.bindings[0]
    root = binding.registered_jobs[0]
    child = binding.child_jobs[0]
    worker_digest = digest({"contract": config.workers.model_dump(mode="json"), "mode": "native"})
    delivery_digest = digest({"contract": config.delivery.model_dump(mode="json"), "mode": "native"})
    root_result = None
    rows = {}
    for job in (root, child):
        data = executor_job_input(job.job_id, job.work)
        executor = w.ExecutorBinding(
            job_id=job.job_id, work_id=job.work.work_id, generation=1, owner=binding.owner,
            executor_id=config.workers.executor_id, executor_version=config.workers.executor_version,
            input_digest=hashlib.sha256(data).hexdigest(), start_request_id="start-" + job.job_id,
        )
        result = d.VerifiedWorkResult(
            evidence_id="result-" + job.job_id, verifier_version=config.delivery.verifier_version,
            work_id=job.work.work_id, definition_digest=digest(job.work.model_dump(mode="json")), state="succeeded",
            passed_checks=job.work.required_checks, content=b"offline terminal result", proof=b"offline trusted fixture",
        )
        evidence = w.ExecutorEvidence(
            evidence_id="terminal-" + job.job_id, binding=executor, sequence=1,
            native_execution_id="native-" + job.job_id, state="succeeded", entire_tree_idle=True,
            all_inflight_starts_fenced=True, result=result, proof=b"offline terminal/fence fixture",
        )
        spec = w.JobSpec(job_id=job.job_id, work_id=job.work.work_id, input=data,
                         parent_job_id=None if job == root else root.job_id,
                         obligation_id=binding.obligation_id if job == root else None)
        record = w.JobRecord(
            configuration_digest=worker_digest, spec=spec, owner=binding.owner, state="succeeded", generation=1,
            binding=executor, native_execution_id=evidence.native_execution_id, last_evidence_id=evidence.evidence_id,
            last_sequence=1, children=(child.job_id,) if job == root else (),
            completion_credit=0, control_remaining=7, result_forwarded=job == root,
        )
        rows[worker_key("job", job.job_id)] = record.model_dump(mode="json")
        rows[worker_key("completion", job.job_id, "1")] = evidence.model_dump(mode="json")
        if job == root:
            root_result = result
    work = d._Work(
        configuration_digest=delivery_digest, evidence_mode="native", event_id=binding.event_id,
        definition=binding.work, definition_digest=root_result.definition_digest, state="succeeded", result=root_result,
    )
    ob = d._Obligation(
        configuration_digest=delivery_digest, evidence_mode="native", obligation_id=binding.obligation_id,
        event_id=binding.event_id, root_request_id=binding.root_request_id, work_id=binding.work.work_id,
        owner_record_id=binding.owner.owner_record_id, conversation_id=binding.owner.conversation_id,
        root_budget_id=binding.owner.root_budget_id, policy_revision=1, route=binding.route,
        required_confirmation_level="client_received", channel_version=config.delivery.channel.version,
        fulfillment="fulfilled", delivery_state="fulfilled", final_delivery_id="final-delivery", final_version=1,
        last_version=1, fulfillment_receipt_id="final-receipt", confirmed_levels=("client_received",),
        review_at=NOW, deadline=NOW + timedelta(minutes=1), automation="stopped", normal_control_remaining=5,
    )
    proof = d.VerifiedDeliveryReceipt(
        evidence_id="final-receipt", verifier_version=config.delivery.verifier_version,
        binding=d.DeliveryBinding(obligation_id=ob.obligation_id, delivery_id="final-delivery", version=1,
                                  payload_digest="b" * 64, route=binding.route,
                                  channel_version=config.delivery.channel.version, artifacts=()),
        send_outcome="emitted", confirmed_levels=("client_received",),
        dispatch_authorization_ref="read-only-fixture", proof=b"offline acknowledged fixture",
    )
    rows[control_key("delivery-work-state", binding.obligation_id)] = work.model_dump(mode="json")
    rows[control_key("delivery-obligation", binding.obligation_id)] = ob.model_dump(mode="json")
    rows[control_key("delivery-receipt", "final-delivery", "final-receipt")] = {
        "schema_version": 1, "runtime_version": "fa-delivery-contract-v1", "evidence_mode": "native",
        "configuration_digest": delivery_digest, "kind": "delivery_verified_receipt", "evidence": proof.model_dump(mode="json"),
    }
    store.transact(ControlRequest(request_id="closed-root-child", request_digest=digest(rows)),
                   lambda _: ControlChanges(writes=tuple(ControlWrite(row_id=key, mode="create", data=data)
                                                         for key, data in rows.items()), result={}))


def test_operator_pin_migration_is_one_EGT_and_preserves_every_existing_nonpointer_entity():
    service, store, table, old, new, principal = fixture()
    before = store.read_inventory()
    plan = service.plan(principal, migration_id="closeout-pins")
    approval = service.authorize(principal, plan, decision_id="operator-approved", expires_at=NOW + timedelta(minutes=5))
    count = len(table.submissions)
    result = service.apply(principal, plan, approval)
    assert len(table.submissions) == count + 1
    assert result.payload["result"]["new_configuration_sha256"] == new.sha256
    after = store.read_inventory()
    pointers = {
        "behavior:v1:row:" + installation_record(old)[0],
        "behavior:v1:row:" + old.sources.protection.key,
        "behavior:v1:row:" + control_key("source-backup-protection", old.table.scope.authority_domain,
                                       old.backups.protection.container_url),
        "behavior:v1:guard",
    }
    for key, fingerprint in before.entity_sha256.items():
        if key not in pointers:
            assert after.entity_sha256[key] == fingerprint
    assert service.reconcile(principal, plan, approval) == result


@pytest.mark.parametrize("change", ["agent", "other-human", "read-only"])
def test_migration_requires_the_exact_operator_before_state_access(change):
    service, _, table, _, _, principal = fixture()
    if change == "agent":
        principal = replace(principal, kind="agent", actor_id="agent-a")
    elif change == "other-human":
        principal = replace(principal, oid="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    else:
        principal = replace(principal, actions=frozenset({"read"}))
    reads, writes = len(table.reads), len(table.submissions)
    with pytest.raises(Problem) as denied:
        service.plan(principal, migration_id="forbidden")
    assert denied.value.status == 403
    assert (len(table.reads), len(table.submissions)) == (reads, writes)


@pytest.mark.parametrize("state", ["pending", "completions", "lease"])
def test_pool_must_be_completely_quiescent(state):
    service, store, _, old, _, principal = fixture()
    key = worker_key("pool", old.workers.workspace_id)
    data = store.read_snapshot((key,))[key].payload["data"]
    if state == "lease":
        data["slots"][0]["lease_until"] = (NOW + timedelta(minutes=1)).isoformat()
    else:
        data[state] = ["binding-job"]
    store.transact(ControlRequest(request_id="busy-pool", request_digest=digest(data), read_set=(key,)),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="replace", data=data),), result={}))
    with pytest.raises(Problem) as busy:
        service.plan(principal, migration_id="busy")
    assert busy.value.body["error"]["code"] == "MIGRATION_NOT_QUIESCENT"


def test_empty_pool_with_a_different_contract_is_not_the_same_installed_pool():
    service, store, _, old, _, principal = fixture()
    key = worker_key("pool", old.workers.workspace_id)
    data = store.read_snapshot((key,))[key].payload["data"]
    data["configuration_digest"] = "f" * 64
    store.transact(ControlRequest(request_id="wrong-pool", request_digest=digest(data), read_set=(key,)),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="replace", data=data),), result={}))
    with pytest.raises(Problem) as different:
        service.plan(principal, migration_id="different-pool")
    assert different.value.body["error"]["code"] == "MIGRATION_RECORD_INVALID"


def test_unknown_record_cannot_be_omitted_from_migration_snapshot():
    service, store, _, _, _, principal = fixture()
    store.transact(ControlRequest(request_id="unknown-row", request_digest="d" * 64),
                   lambda _: ControlChanges(writes=(ControlWrite(
                       row_id="unknown-obligation", mode="create", data={"state": "pending_unknown", "unknown": True},
                   ),), result={}))
    with pytest.raises(Problem) as unknown:
        service.plan(principal, migration_id="unknown")
    assert unknown.value.body["error"]["code"] == "MIGRATION_UNCLASSIFIED_RECORD"


def test_approved_plan_rejects_state_change_without_resetting_counters():
    service, store, table, old, _, principal = fixture()
    plan = service.plan(principal, migration_id="stale")
    approval = service.authorize(principal, plan, decision_id="approve-stale", expires_at=NOW + timedelta(minutes=5))
    key = control_key("budget", old.roots[0].root_budget_id)
    data = store.read_snapshot((key,))[key].payload["data"]
    data["remaining_units"] -= 1
    store.transact(ControlRequest(request_id="legitimate-spend", request_digest=digest(data), read_set=(key,)),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="replace", data=data),), result={}))
    before = len(table.submissions)
    with pytest.raises(Problem) as stale:
        service.apply(principal, plan, approval)
    assert stale.value.body["error"]["code"] == "MIGRATION_STATE_CHANGED"
    assert len(table.submissions) == before
    assert store.read_snapshot((key,))[key].payload["data"]["remaining_units"] == 10


def test_unknown_apply_ack_only_reconciles_original_atomic_result():
    service, store, table, _, new, principal = fixture()
    plan = service.plan(principal, migration_id="unknown-ack")
    approval = service.authorize(principal, plan, decision_id="approve-unknown", expires_at=NOW + timedelta(minutes=5))
    table.failure = "after"
    with pytest.raises(Problem) as unknown:
        service.apply(principal, plan, approval)
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    before = len(table.submissions)
    table.failure = None
    result = service.reconcile(principal, plan, approval)
    assert result.payload["result"]["new_configuration_sha256"] == new.sha256
    assert len(table.submissions) == before


def test_new_installation_uses_exact_historical_probe_without_bootstrap_or_counter_reset():
    service, store, table, old, new, principal = fixture()
    plan = service.plan(principal, migration_id="lineage")
    approval = service.authorize(principal, plan, decision_id="approve-lineage", expires_at=NOW + timedelta(minutes=5))
    service.apply(principal, plan, approval)
    count = len(table.submissions)
    assert service.verify_applied(principal, plan, approval)["before_preserved_sha256"] == plan.preserved_sha256
    rebuilt = MigrationStore(table, SCOPE)
    assert installed_origin(rebuilt, new).sha256 == old.sha256
    assert len(table.submissions) == count
    with pytest.raises(Problem):
        installed_origin(MigrationStore(table, SCOPE), old)


@pytest.mark.parametrize("field", ["operator_oid", "code_sha256", "plan_sha256"])
def test_approval_cannot_be_rebound_to_other_operator_code_or_plan(field):
    service, _, table, _, _, principal = fixture()
    plan = service.plan(principal, migration_id="approval-binding")
    approval = service.authorize(principal, plan, decision_id="approve-binding", expires_at=NOW + timedelta(minutes=5))
    changed = approval.model_copy(update={field: "e" * 64 if field != "operator_oid" else "other-operator"})
    count = len(table.submissions)
    with pytest.raises(Problem) as denied:
        service.apply(principal, plan, changed)
    assert denied.value.body["error"]["code"] == "MIGRATION_AUTHORIZATION_DENIED"
    assert len(table.submissions) == count


@pytest.mark.parametrize("change", ["container", "authority", "ACL", "policy-expiry", "capacity"])
def test_config_pin_migration_cannot_loosen_or_change_other_configuration(change):
    service, store, _, old, new, _ = fixture()
    raw = new.model_dump(mode="json")
    if change == "container":
        raw["sources"]["protection"]["container_url"] += "-other"
    elif change == "authority":
        raw["table"]["scope"]["partition_key"] = "other-authority"
    elif change == "ACL":
        raw["policies"][0]["policy"]["source_visibility"] = "private"
    elif change == "policy-expiry":
        raw["policies"][0]["policy"]["valid_until"] = (NOW + timedelta(days=30)).isoformat()
    else:
        raw["workers"]["capacity"] = 3
    with pytest.raises((Problem, ValueError)):
        candidate = ServerConfiguration.model_validate_json(json.dumps(raw))
        ProtectionMigration(
            store, old, candidate,
            {lane: observation.model_dump_json().encode() for lane, observation in service.observations.items()},
            previous_observations={lane: observation.model_dump_json().encode()
                                   for lane, observation in service.previous_observations.items()},
            clock=lambda: NOW,
        )


def test_terminal_registered_child_without_human_delivery_is_not_mistaken_for_missing_work():
    service, store, _, old, _, principal = fixture(with_child=True)
    terminal_root_and_child(store, old)
    plan = service.plan(principal, migration_id="closed-child")
    assert plan.quiescence_counts["JobRecord"] == 2
    assert "registered-child" not in plan.unsubmitted_jobs
    assert old.bindings[0].job_id not in plan.unsubmitted_jobs


def test_expired_inline_originals_and_completed_backup_remain_unchanged_and_independently_recoverable():
    service, store, table, old, new, principal = fixture()
    clock = [NOW]
    service.clock = lambda: clock[0]
    context = AuthenticatedSourceContext(old.identity.tenant_id, old.identity.team_id,
                                         principal.source_actor_id, "operator-fixture", ("metrics", "profile"))
    sources = SourceMaterials(store, clock=lambda: clock[0])
    source = sources.plan(context, SourceInput(
        source_id=old.bindings[0].source_ids[0], version=1, visibility="team", purpose="metrics",
        schema_id="fa-human-text-v1", media_type="text/plain", expires_at=NOW + timedelta(seconds=1),
    ), b"retained original after applicability expiry")
    source_receipt = sources.commit(context, source)
    snapshot = sources.capture_snapshot(
        context, snapshot_id="migration-snapshot", scope=source.reference.scope, conversation_id="migration-fixture",
        runtime_version="migration-fixture", roots=(source.reference,),
    )
    settings = SourceBackupSettings(tenant_id=old.identity.tenant_id, authority_domain=old.table.scope.authority_domain,
                                    container_url=old.backups.protection.container_url)
    backup_store = InMemoryBackupStore(old.identity.tenant_id)
    privileged = replace(principal, actions=principal.actions | frozenset({"backup", "restore"}))
    backup = SourceBackupService(
        backup_store, settings, sources=sources, protection=old.backups.protection,
        protection_verifier=InventoryProtectionVerifier(service.previous_observations["backups"].state,
                                                        clock=lambda: clock[0]), clock=lambda: clock[0],
    )
    plan = backup.plan(privileged, context, backup_id="a" * 32, scope=source.reference.scope,
                       snapshot_id="migration-snapshot", snapshot_receipt=snapshot)
    uploaded = backup.upload(privileged, context, plan)
    reference = SourceBackupReference.model_validate_json(json.dumps(uploaded.payload["result"]["reference"]))
    before = store.read_inventory()
    clock[0] += timedelta(seconds=2)
    migration = service.plan(principal, migration_id="retain-expired")
    approval = service.authorize(principal, migration, decision_id="retain-approved",
                                 expires_at=NOW + timedelta(minutes=5))
    service.apply(principal, migration, approval)
    after = store.read_inventory()
    assert after.entity_sha256["behavior:v1:row:" + source.reference.key] == before.entity_sha256["behavior:v1:row:" + source.reference.key]
    restored_sources = SourceMaterials(MigrationStore(table, SCOPE), clock=lambda: clock[0])
    with pytest.raises(Problem) as expired:
        restored_sources.read_exact(context, source.reference)
    assert expired.value.body["error"]["code"] == "SOURCE_EXPIRED"
    assert restored_sources.reconcile(context, source) == source_receipt
    class Catalog:
        allowed = False

        def authorize(self, operator, exact_reference):
            if not self.allowed:
                raise Problem(403, "RECOVERY_NOT_AUTHORIZED", "independent approval required")
            return RecoveryAuthorization(
                decision_id="explicit-recovery", policy_version="recovery-v1", principal_oid=operator.oid,
                reference_sha256=exact_reference.sha256, scope=exact_reference.description.scope,
                current_access_revision=1, expires_at=NOW + timedelta(hours=1),
            )
    catalog = Catalog()
    recovery = SourceBackupService(backup_store, settings, recovery_authorizer=catalog, clock=lambda: clock[0])
    with pytest.raises(Problem):
        recovery.restore_quarantined(privileged, reference)
    catalog.allowed = True
    result = recovery.restore_quarantined(privileged, reference)
    assert result.status == "quarantined" and result.originals[source.reference.key] == source.original
    assert reference.description.protection == old.backups.protection
    assert new.backups.protection != reference.description.protection


def test_any_retained_large_source_preparation_is_explicitly_rejected():
    service, store, _, old, _, principal = fixture()
    context = AuthenticatedSourceContext(old.identity.tenant_id, old.identity.team_id,
                                         principal.source_actor_id, "operator-fixture", ("metrics",))
    sources = SourceMaterials(store)
    source = sources.plan(context, SourceInput(source_id=old.bindings[0].source_ids[0], version=1,
                         purpose="metrics", schema_id="fa-human-text-v1", media_type="text/plain"), b"fixture")
    preparation = _Preparation(request=source.request, reference=source.reference, plan_sha256=source.request.request_digest,
                               protection=old.sources.protection, state="pending_or_unknown")
    key = control_key("source-preparation", source.reference.key)
    store.transact(ControlRequest(request_id="old-large", request_digest=digest(preparation.model_dump(mode="json"))),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="create",
                                                                  data=preparation.model_dump(mode="json")),), result={}))
    with pytest.raises(Problem) as blocked:
        service.plan(principal, migration_id="not-large")
    assert blocked.value.body["error"]["code"] == "MIGRATION_LEGACY_PROTECTED_SOURCE"


def test_pending_backup_upload_cannot_be_skipped_as_historical_metadata():
    service, store, _, old, _, principal = fixture()
    context = AuthenticatedSourceContext(old.identity.tenant_id, old.identity.team_id,
                                         principal.source_actor_id, "fixture", ("metrics",))
    description = sb._BackupDescription(
        backup_id="b" * 32, owner_oid=principal.oid, scope=context.scope("team"), snapshot_id="pending",
        archive_sha256="a" * 64, byte_length=1, header_sha256="b" * 64, manifest_sha256="c" * 64,
        snapshot_receipt_sha256="d" * 64, protection=old.backups.protection,
        storage=SourceBackupSettings(tenant_id=old.identity.tenant_id, authority_domain=old.table.scope.authority_domain,
                                     container_url=old.backups.protection.container_url),
    )
    intent = sb._UploadIntent(description=description, request=ControlRequest(request_id="pending-backup", request_digest="e" * 64))
    store.transact(ControlRequest(request_id="save-pending-backup", request_digest="a" * 64),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id="pending-backup-row", mode="create",
                                                                  data=intent.model_dump(mode="json")),), result={}))
    with pytest.raises(Problem) as pending:
        service.plan(principal, migration_id="cannot-skip-backup")
    assert pending.value.body["error"]["code"] == "MIGRATION_BACKUP_UNKNOWN"


def test_prepared_source_without_receipt_remains_a_write_obligation():
    service, store, _, old, _, principal = fixture()
    binding = old.bindings[0]
    context = AuthenticatedSourceContext(old.identity.tenant_id, old.identity.team_id,
                                         principal.source_actor_id, binding.ingress_id, ("metrics",))
    source = SourceMaterials(store).plan(context, SourceInput(
        source_id=binding.source_ids[0], version=1, purpose="metrics", schema_id="fa-human-text-v1", media_type="text/plain",
    ), b"not admitted")
    command = CommandPlan(
        binding_id=binding.binding_id, binding_digest=old.policies[0].policy.binding_digest,
        kind="source-" + binding.source_ids[0], request_id="pending-source", principal_oid=principal.oid,
        input_digest="a" * 64, context=binding.owner, data={"source_plan": source.model_dump(mode="json")},
        request=source.request,
    )
    key = runtime_key("plan", binding.binding_id, command.kind, command.request_id)
    index = runtime_key("plan-index", binding.binding_id)
    store.transact(ControlRequest(request_id="save-pending-source", request_digest="b" * 64, read_set=(index,)),
                   lambda _: ControlChanges(writes=(
                       ControlWrite(row_id=key, mode="create", data=command.model_dump(mode="json")),
                       ControlWrite(row_id=index, mode="replace", data={"keys": [key]}),
                   ), result={}))
    with pytest.raises(Problem) as pending:
        service.plan(principal, migration_id="cannot-skip-source")
    assert pending.value.body["error"]["code"] == "MIGRATION_WRITE_OBLIGATION_UNKNOWN"


def test_unresolved_operation_cannot_be_cleared_by_pin_migration():
    service, store, _, old, _, principal = fixture()
    binding = old.bindings[0]
    operation = op._Operation(
        operation_id=binding.operation_id, owner_record_id=binding.owner.owner_record_id,
        conversation_id=binding.owner.conversation_id, root_budget_id=binding.owner.root_budget_id,
        description_digest="a" * 64, adapter_contract_digest="b" * 64, unsettled_attempt_ids=("unknown-attempt",),
    )
    store.transact(ControlRequest(request_id="unknown-operation", request_digest="c" * 64),
                   lambda _: ControlChanges(writes=(ControlWrite(row_id=control_key("operation", operation.operation_id),
                          mode="create", data=operation.model_dump(mode="json")),), result={}))
    with pytest.raises(Problem) as pending:
        service.plan(principal, migration_id="cannot-hide-effect")
    assert pending.value.body["error"]["code"] == "MIGRATION_OPERATION_UNKNOWN"


def test_apply_with_expired_authorization_does_not_write():
    service, _, table, _, _, principal = fixture()
    plan = service.plan(principal, migration_id="expired-approval")
    approval = service.authorize(principal, plan, decision_id="expires", expires_at=NOW + timedelta(seconds=1))
    service.clock = lambda: NOW + timedelta(seconds=1)
    count = len(table.submissions)
    with pytest.raises(Problem) as expired:
        service.apply(principal, plan, approval)
    assert expired.value.body["error"]["code"] == "MIGRATION_AUTHORIZATION_EXPIRED"
    assert len(table.submissions) == count


def test_guard_race_at_EGT_does_not_partially_update_pins_or_create_history():
    service, store, table, old, _, principal = fixture()
    plan = service.plan(principal, migration_id="egt-race")
    approval = service.authorize(principal, plan, decision_id="approve-race", expires_at=NOW + timedelta(minutes=5))
    competitor = MigrationStore(table, SCOPE)
    budget = control_key("budget", old.roots[0].root_budget_id)
    data = store.read_snapshot((budget,))[budget].payload["data"]
    data["remaining_units"] -= 1
    table.before_submit = lambda: competitor.transact(
        ControlRequest(request_id="racing-spend", request_digest=digest(data), read_set=(budget,)),
        lambda _: ControlChanges(writes=(ControlWrite(row_id=budget, mode="replace", data=data),), result={}),
    )
    with pytest.raises(Problem) as raced:
        service.apply(principal, plan, approval)
    assert raced.value.status == 412
    current = store.read_snapshot((installation_record(old)[0], old.sources.protection.key))
    assert current[installation_record(old)[0]].payload["data"]["configuration_sha256"] == old.sha256
    assert current[old.sources.protection.key].payload["data"] == old.sources.protection.model_dump(mode="json")
    with pytest.raises(Problem) as missing:
        service.reconcile(principal, plan, approval)
    assert missing.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"


def test_post_apply_verification_rejects_a_tampered_pin_even_when_installation_is_unchanged():
    service, store, table, old, _, principal = fixture()
    plan = service.plan(principal, migration_id="tampered-pin")
    approval = service.authorize(principal, plan, decision_id="approve-pin", expires_at=NOW + timedelta(minutes=5))
    service.apply(principal, plan, approval)
    entity = table.backing.rows[(table.table_name, SCOPE.partition_key, "behavior:v1:row:" + old.sources.protection.key)]
    body = json.loads(entity["Payload"])
    body["data"]["evidence_sha256"] = "f" * 64
    entity["Payload"] = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    with pytest.raises(Problem) as changed:
        service.verify_applied(principal, plan, approval)
    assert changed.value.body["error"]["code"] == "MIGRATION_POSTSTATE_CHANGED"


def test_migration_permanently_fences_an_unmodified_legacy_store_writer():
    service, store, table, _, _, principal = fixture()
    legacy = LegacyStore(table, SCOPE)
    plan = service.plan(principal, migration_id="legacy-fence")
    approval = service.authorize(principal, plan, decision_id="approve-legacy-fence", expires_at=NOW + timedelta(minutes=5))
    service.apply(principal, plan, approval)
    before = dict(store.read_inventory().entity_sha256)
    with pytest.raises(Problem):
        legacy.transact(ControlRequest(request_id="legacy-after-migration", request_digest="c" * 64),
                        lambda _: ControlChanges(writes=(), result={}))
    assert dict(store.read_inventory().entity_sha256) == before


def test_migration_aware_receiver_keeps_guard_v2_for_subsequent_guarded_activity():
    service, store, table, _, new, principal = fixture()
    plan = service.plan(principal, migration_id="new-writer")
    approval = service.authorize(principal, plan, decision_id="approve-new-writer", expires_at=NOW + timedelta(minutes=5))
    service.apply(principal, plan, approval)
    writer = MigrationStore(table, SCOPE)
    installed_origin(writer, new)
    receiver = LegacyStore(writer._client, SCOPE)
    receiver.transact(ControlRequest(request_id="new-generation-readback", request_digest="a" * 64),
                      lambda _: ControlChanges(writes=(), result={"new_generation": True}))
    inventory = writer.read_inventory()
    assert inventory.installation_configuration_sha256 == new.sha256
    assert inventory.guard.payload["revision"] == plan.guard.revision + 2
    with pytest.raises(Problem):
        LegacyStore(table, SCOPE).read_snapshot(())
