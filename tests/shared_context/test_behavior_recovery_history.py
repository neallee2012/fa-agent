"""Typed recovery history only: synthetic claimed rows, offline Table and stop.

No SDK/model, Azure, native approval, deployment or delivery call is performed.
Cancellation/seal/observe/forward receipts are emitted by the actual producers.
"""
import copy
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from shared_context import behavior_delivery as d, behavior_foundry as n, behavior_workers as w
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context import behavior_wire as wire
from shared_context.behavior_assistant import _ModelIntent, _Session, key as assistant_key
from shared_context.behavior_assistant_wire import Assignment, Task
from shared_context.behavior_inbox import InboxRegistration, PullInbox
from shared_context.behavior_migration import _quiescence
from shared_context.behavior_operations import control_key
from shared_context.behavior_recovery import (
    ClaimedSdkRecovery, PlatformStopObservation, PreSdkRecoveryScope, RecoveryAuthorization, RecoveryScope,
    recovery_key, stop_endpoint,
)
from shared_context.behavior_recovery_history import validate_recovery_history
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_server_config import ServerConfiguration
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from shared_context.records import Record
from shared_context.source_materials import digest
from test_behavior_assistant_server import assistant_configuration
from test_behavior_migration import fixture
from test_behavior_server import NOW


def forbidden(*_args, **_kwargs):
    raise AssertionError("history tests cannot dispatch model/native/delivery calls")


@pytest.fixture
def history(request, monkeypatch):
    # The offline reviewed code image must not race concurrent worktree edits.
    monkeypatch.setattr("shared_context.behavior_recovery.code_sha256", lambda: "f" * 64)
    pre_sdk = getattr(request, "param", None) == "pre-sdk"
    raw = assistant_configuration()
    raw["policies"][0]["policy"]["valid_until"] = (NOW - timedelta(minutes=5)).isoformat()
    with patch("test_behavior_migration.synthetic_configuration",
               return_value=ServerConfiguration.model_validate_json(json.dumps(raw))):
        _, store, table, config, _, principal = fixture()
    h = SimpleNamespace(store=store, table=table, config=config, principal=principal, time=NOW, calls=[])
    b, target = config.bindings[0], config.active_targets[0]
    worker = w.WorkerCore(store, config.workers, adapter=None, clock=lambda: h.time)
    authority = n.NativeLaunchAuthority(
        worker, team_id=config.identity.team_id, controller_id=config.controller_id,
        targets=config.active_targets, jobs=(n.NativeJobRoute(job_id=b.job_id, api_binding_id=b.binding_id,
                                                            grant_id="original-native-grant"),),
        verifier_version=config.delivery.verifier_version, clock=lambda: h.time,
        recovery_binding=n.NativeRecoveryBinding(configuration_sha256=config.sha256,
            operator_oid=config.identity.human_oid, credential_client_id=config.identity.managed_identity_client_id),
    )
    worker.adapter = w.ExecutorAdapter(forbidden, forbidden, forbidden, authority.verify)
    channel = PullInbox(store, InboxRegistration(config.authentication_version, forbidden, forbidden))
    delivery = d.DeliveryCore(store, clock=lambda: h.time, registration=d.RuntimeRegistration(
        config.delivery, verify_work_result=worker.verify_work_result, channel_adapter=channel,
    ))
    channel.attach(delivery)
    task = Task(
        job_id=b.job_id, work_id=b.work.work_id, definition_digest=digest(b.work.model_dump(mode="json")),
        profile_digest=digest(config.assistant.profiles[0].model_dump(mode="json")),
        source=config.assistant.profiles[0].source, artifact_id=config.assistant.profiles[0].artifact_id,
        artifact_version=config.assistant.profiles[0].artifact_version, operation_id=b.operation_id,
    )
    body = task.model_dump_json().encode()
    execution = w.ExecutorBinding(
        job_id=b.job_id, work_id=b.work.work_id, generation=1, owner=b.owner,
        executor_id=config.workers.executor_id, executor_version=config.workers.executor_version,
        input_digest=hashlib.sha256(body).hexdigest(), start_request_id="original-worker-start",
    )
    start = w.ExecutorStart(binding=execution, input=body,
                            request=worker._request("start", (b.job_id,), {}, ()))
    launch_id = n.native_key("launch-id", execution.start_request_id)
    launch = n._NativeLaunch(
        launch_id=launch_id, configuration_digest=worker.configuration_digest(), api_binding_id=b.binding_id,
        grant_id="original-native-grant", start=start, definition=b.work, target=target, process_generation=7,
        permit_digest="a" * 64, post_digest="b" * 64, phase="claimed", native_invocation_id="original-invocation",
        claim_id="original-claim", process_id="original-process",
        report_token_digest=hashlib.sha256(("c" * 64).encode()).hexdigest(),
        evidence_sequence=16, probes_reserved=16, workload_version="assistant-v1.2",
    )
    job = w.JobRecord(
        configuration_digest=worker.configuration_digest(),
        spec=w.JobSpec(job_id=b.job_id, work_id=b.work.work_id, input=body, obligation_id=b.obligation_id),
        owner=b.owner, binding=execution, generation=1, state="capacity_uncertain",
        control_remaining=0, queries_reserved=16,
    )
    sid = assistant_key("session-id", execution.start_request_id)
    session = _Session(
        binding_id=b.binding_id, assignment=Assignment(binding_id=b.binding_id, session_id=sid,
            sdk_session_id=sid, task=task), execution=execution, ticket_digest="d" * 64,
        state="accepted" if pre_sdk else "waiting", reason=None if pre_sdk else "sdk_failed",
        model_requests=0 if pre_sdk else 1,
    )
    model = _ModelIntent(request_sha256="e" * 64, consumed=True)
    grant = n.NativeExecutionGrant(
        grant_id=launch.grant_id, controller_id=config.controller_id, owner=b.owner, policy_revision=1,
        job_id=b.job_id, definition=b.work, input_digest=execution.input_digest, targets=(target,),
        active=True, remaining_uses=1, not_before=NOW - timedelta(hours=1), expires_at=NOW - timedelta(minutes=5),
        workload_version="assistant-v1.2",
    )
    obligation = d._Obligation(
        configuration_digest=delivery.configuration_digest(), evidence_mode="native", obligation_id=b.obligation_id,
        event_id=b.event_id, root_request_id=b.root_request_id, work_id=b.work.work_id,
        owner_record_id=b.owner.owner_record_id, conversation_id=b.owner.conversation_id,
        root_budget_id=b.owner.root_budget_id, policy_revision=1, route=b.route,
        required_confirmation_level="client_received", channel_version=config.delivery.channel.version,
        review_at=NOW, deadline=NOW + timedelta(minutes=10), normal_control_remaining=1,
    )
    work = d._Work(configuration_digest=delivery.configuration_digest(), evidence_mode="native",
                   event_id=b.event_id, definition=b.work, definition_digest=digest(b.work.model_dump(mode="json")))
    rows = {
        n.native_key("launch", launch_id): launch,
        n.native_key("target", target.executor_id): n._TargetReservation(
            target=target, generation=7, active_launch_id=launch_id, configuration_digest=worker.configuration_digest()),
        n.native_key("grant", grant.grant_id): grant,
        w.worker_key("job", b.job_id): job,
        w.worker_key("attempt", b.job_id, "1"): w._Attempt(start=start),
        w.worker_key("pool", config.workers.workspace_id): w._Pool(
            configuration_digest=worker.configuration_digest(), workspace_id=config.workers.workspace_id,
            slots=(w.Slot(number=0, binding=execution, lease_until=NOW - timedelta(minutes=5)),), pending=(b.job_id,)),
        assistant_key("session", sid): session,
        control_key("delivery-obligation", b.obligation_id): obligation,
        control_key("delivery-work-state", b.obligation_id): work,
    }
    if not pre_sdk:
        rows[assistant_key("model-intent", sid, model.request_sha256)] = model
    pool_key = w.worker_key("pool", config.workers.workspace_id)
    store.transact(ControlRequest(request_id="offline-claimed-sdk", request_digest=digest(list(rows)), read_set=(pool_key,)),
        lambda _: ControlChanges(writes=tuple(ControlWrite(
            row_id=key, mode="replace" if key == pool_key else "create", data=value.model_dump(mode="json"),
            material=d._material(start.model_dump(mode="json")) if isinstance(value, w._Attempt) else None,
        ) for key, value in rows.items()), result={"offline_claim_fixture": True}))
    if getattr(request, "include_reports", False):
        probe = authority.probe(execution)
        actor = BehaviorPrincipal(config.identity.tenant_id, target.agent_oid, config.identity.team_id,
                                  "agent", config.identity.agents[0].actor, frozenset({"consume"}))
        authority.report(b.binding_id, launch_id, probe.probe_id, wire.HostedReport(
            request_id="original-observer-report", invocation_id=launch.native_invocation_id,
            identity=wire.HostedIdentity(**{name: getattr(target, name) for name in (
                "agent_id", "agent_name", "agent_version", "session_id")}),
            process_id=launch.process_id, claim_id=launch.claim_id, probe_id=probe.probe_id,
            process_generation=launch.process_generation, binding_digest=digest(execution.model_dump(mode="json")),
            supervisor_id=launch.process_id, sequence=1, phase="spawn_unknown",
            report_token=SecretStr("c" * 64),
        ), actor)
    scope_type = PreSdkRecoveryScope if pre_sdk else RecoveryScope
    scope = scope_type(
        recovery_id="original-recovery", configuration_sha256=config.sha256, job_id=b.job_id, launch_id=launch_id,
        model_request_sha256=None if pre_sdk else model.request_sha256, operator_oid=config.identity.human_oid,
        expires_at=NOW + timedelta(minutes=20 if getattr(request, "param", 204) in {"legacy-clock-15", "legacy-late-stop"} else 10),
        **({"recovery_phase": "before_sdk_acquire"} if pre_sdk else {}),
    )

    def stop(target, deadline):
        if getattr(request, "param", 204) == "legacy-late-stop":
            h.time = h.approval.approved_at + timedelta(minutes=15, microseconds=1)
        assert h.time < deadline
        h.calls.append(target)
        status = 409 if getattr(request, "param", 204) == 409 else 204
        return PlatformStopObservation(target=target, endpoint=stop_endpoint(target),
            credential_client_id=config.identity.managed_identity_client_id, status_code=status,
            error_code="session_already_stopped" if status == 409 else None, observed_at=h.time)

    h.worker, h.authority = worker, authority
    def clock():
        if getattr(request, "param", 204) in {"ticking-clock", "legacy-clock-15"}:
            h.time += timedelta(microseconds=1)
        return h.time
    h.service = ClaimedSdkRecovery(config, authority, scope, stop, delivery=delivery, clock=clock)
    plan = h.service.plan(principal)
    if getattr(request, "param", 204) in {"legacy-clock-15", "legacy-late-stop"}:
        h.approval = RecoveryAuthorization(
            plan=plan, decision_id="offline-explicit-cancellation", approved_at=h.time,
            expires_at=h.time + timedelta(minutes=15, microseconds=4),
        )
        key = recovery_key("authorization", plan.recovery_id)
        data = h.approval.model_dump(mode="json")
        store.transact(ControlRequest(request_id=key, request_digest=digest(data)),
            lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="create", data=data,
                                                          material=d._material(data)),),
                                     result={"authorization_key": key}))
    else:
        h.approval = h.service.authorize(principal, plan, "offline-explicit-cancellation")
    h.before_cancel = store.read_inventory()
    h.service.execute(principal, h.approval)
    h.inventory = store.read_inventory()
    return h


def inventory_copy(h):
    return replace(h.inventory, rows=copy.deepcopy(dict(h.inventory.rows)),
                   receipts=copy.deepcopy(dict(h.inventory.receipts)), entity_sha256=dict(h.inventory.entity_sha256))


@pytest.mark.parametrize("history", ["legacy-clock-15"], indirect=True)
def test_two_publisher_clock_reads_do_not_reject_an_action_within_the_strict_window(history):
    h = history
    assert h.approval.expires_at - h.approval.approved_at >= timedelta(minutes=15)
    before = copy.deepcopy(h.table.backing.rows)
    assert validate_recovery_history(h.inventory, h.config)
    assert h.table.backing.rows == before


@pytest.mark.parametrize("history", ["legacy-late-stop"], indirect=True)
def test_legacy_expiry_drift_never_extends_the_allowed_stop_action_window(history):
    with pytest.raises(Problem) as denied:
        validate_recovery_history(history.inventory, history.config)
    assert denied.value.body["error"]["code"] == "MIGRATION_RECOVERY_HISTORY_INVALID"


def change_row(inventory, key, mutate):
    """Corrupt the offline snapshot while retaining its old immutable receipts."""
    record = inventory.rows[key]
    payload = copy.deepcopy(record.payload)
    mutate(payload)
    inventory.rows[key] = Record(payload, record.etag)


def test_complete_history_is_pure_and_preserves_unknown_effects_pending_delivery_and_expired_policy(history):
    h = history
    before = copy.deepcopy(h.table.backing.rows)
    input_before = (copy.deepcopy(dict(h.inventory.rows)), copy.deepcopy(dict(h.inventory.receipts)),
                    dict(h.inventory.entity_sha256))
    submissions = len(h.table.submissions)
    h.time += timedelta(days=1)
    with patch.object(h.store, "transact", forbidden), patch.object(h.store, "read_snapshot", forbidden):
        classified = validate_recovery_history(h.inventory, h.config)
    assert len(classified) == 6
    assert h.table.backing.rows == before and len(h.table.submissions) == submissions
    assert (h.inventory.rows, h.inventory.receipts, h.inventory.entity_sha256) == input_before
    with pytest.raises(TypeError):
        classified["invented"] = None
    b = h.config.bindings[0]
    for key in (control_key("budget", b.owner.root_budget_id), control_key("owner", b.owner.owner_record_id),
                runtime_key("policy", b.binding_id), n.native_key("grant", "original-native-grant")):
        assert h.inventory.rows[key] == h.before_cancel.rows[key]
    for key, receipt in h.before_cancel.receipts.items():
        assert h.inventory.receipts[key] == receipt
    launch = h.authority.launch(h.service.scope.launch_id)
    job = h.worker.inspect(b.job_id)
    assert launch.sealed_evidence_id != job.last_evidence_id
    assert job.last_evidence_id == n.native_key("terminal-verification-id", launch.launch_id, str(job.last_sequence))
    assert launch.child_pid is None and len(h.calls) == 1
    work = h.inventory.rows[control_key("delivery-work-state", b.obligation_id)].payload["data"]
    assert work["result"]["unresolved_operations"] == [h.service.scope.model_request_sha256]
    assert h.inventory.rows[control_key("delivery-obligation", b.obligation_id)].payload["data"]["fulfillment"] == "pending"


@pytest.mark.parametrize("history", [204, 409, "ticking-clock"], indirect=True)
def test_exact_provider_outcomes_and_later_unused_terminal_refresh_are_valid(history):
    h = history
    h.time += timedelta(hours=1)
    extra = h.authority.reconcile_sealed(h.approval.plan.binding)
    assert extra != h.worker.inspect(h.approval.plan.binding.job_id).last_evidence_id
    assert len(validate_recovery_history(h.store.read_inventory(), h.config)) == 6


@pytest.mark.parametrize("first,code", [
    ("delivery-obligation", "MIGRATION_DELIVERY_PENDING"),
    ("delivery-work-state", "MIGRATION_NOT_QUIESCENT"),
])
def test_classification_never_turns_cancellation_responsibilities_into_migration_pass(history, first, code):
    h = history
    key = control_key(first, h.config.bindings[0].obligation_id)
    inventory = replace(h.inventory, rows={key: h.inventory.rows[key],
                        **{k: v for k, v in h.inventory.rows.items() if k != key}})
    assert len(validate_recovery_history(inventory, h.config)) == 6
    with pytest.raises(Problem) as denied:
        _quiescence(inventory, h.config)
    assert denied.value.body["error"]["code"] == code


@pytest.mark.parametrize("kind", ["authorization", "stop-intent", "stop-observation", "platform-stop"])
def test_missing_or_unprotected_recovery_rows_are_errors(history, kind):
    key = (n.native_key(kind, history.service.scope.launch_id) if kind == "platform-stop"
           else recovery_key(kind, history.service.scope.recovery_id))
    for missing in (True, False):
        inventory = inventory_copy(history)
        if missing:
            inventory.rows.pop(key)
        else:
            change_row(inventory, key, lambda row: row.update(material=None))
        with pytest.raises(Problem):
            validate_recovery_history(inventory, history.config)


@pytest.mark.parametrize("lane", ["authorization", "cancel-bound", "stop-intent", "stop-observation",
                                  "sealed", "reconcile-sealed", "observe", "prepare-forward", "forwarded", "work-result"])
def test_missing_original_receipts_are_not_reconstructed(history, lane):
    h = history
    plan = h.approval.plan
    job = h.worker.inspect(plan.binding.job_id)
    request_ids = {
        "authorization": recovery_key("authorization", plan.recovery_id),
        "cancel-bound": w.worker_key("cancel-bound", plan.binding.job_id, plan.binding.start_request_id),
        "stop-intent": recovery_key("stop-intent", plan.recovery_id),
        "stop-observation": recovery_key("stop-observation", plan.recovery_id),
        "sealed": recovery_key("sealed", plan.recovery_id),
        "reconcile-sealed": n.native_key("reconcile-sealed", plan.launch_id, str(job.last_sequence)),
        "observe": w.worker_key("observe", plan.binding.job_id, job.last_evidence_id),
        "prepare-forward": w.worker_key("prepare-forward", plan.binding.job_id),
        "forwarded": w.worker_key("forwarded", plan.binding.job_id),
        "work-result": control_key("delivery-request", "work-result", h.config.bindings[0].obligation_id,
                                  n.native_key("platform-stop-result", plan.launch_id)),
    }
    inventory = inventory_copy(h)
    inventory.receipts.pop(request_ids[lane])
    with pytest.raises(Problem):
        validate_recovery_history(inventory, h.config)


@pytest.mark.parametrize("field,value", [
    ("operator_oid", "another-human"), ("configuration_sha256", "0" * 64),
    ("model_request_sha256", "0" * 64), ("launch_id", "foreign-launch"),
    ("native_invocation_id", "foreign-invocation"), ("code_sha256", "bad-digest"),
    ("snapshot_sha256", None),
])
def test_corrupt_approval_plan_never_classifies(history, field, value):
    inventory = inventory_copy(history)
    key = recovery_key("authorization", history.service.scope.recovery_id)
    change_row(inventory, key, lambda row: row["data"]["plan"].update({field: value}))
    with pytest.raises(Problem):
        validate_recovery_history(inventory, history.config)


@pytest.mark.parametrize("field,value", [
    ("approved_at", (NOW + timedelta(seconds=1)).isoformat()),
    ("expires_at", NOW.isoformat()),
    ("expires_at", (NOW + timedelta(minutes=16)).isoformat()),
    ("approved_at", "2026-09-17T00:00:00"),
])
def test_invalid_human_approval_window_is_not_extended(history, field, value):
    inventory = inventory_copy(history)
    key = recovery_key("authorization", history.service.scope.recovery_id)
    change_row(inventory, key, lambda row: row["data"].update({field: value}))
    with pytest.raises(Problem):
        validate_recovery_history(inventory, history.config)


@pytest.mark.parametrize("field,value", [
    ("endpoint", "https://foreign.invalid/stop"), ("credential_client_id", "foreign-client"),
    ("status_code", 202), ("status_code", True), ("status_code", "204"),
    ("error_code", "unknown"), ("observed_at", "2026-09-17T00:00:00"),
    ("observed_at", (NOW - timedelta(seconds=1)).isoformat()),
    ("observed_at", (NOW + timedelta(minutes=10)).isoformat()),
])
def test_corrupt_stop_observation_is_not_terminal_proof(history, field, value):
    inventory = inventory_copy(history)
    key = recovery_key("stop-observation", history.service.scope.recovery_id)
    change_row(inventory, key, lambda row: row["data"].update({field: value}))
    with pytest.raises(Problem):
        validate_recovery_history(inventory, history.config)


@pytest.mark.parametrize("case", ["target", "generation", "terminal-id", "result", "model", "extra-field",
                                  "receipt-material", "orphan", "all-recovery-missing"])
def test_foreign_partial_or_unmatched_history_is_not_excluded(history, case):
    h = history
    inventory = inventory_copy(h)
    plan = h.approval.plan
    job = h.worker.inspect(plan.binding.job_id)
    if case == "target":
        key = n.native_key("launch", plan.launch_id)
        change_row(inventory, key, lambda row: row["data"]["target"].update(agent_version="999"))
    elif case == "generation":
        key = w.worker_key("job", plan.binding.job_id)
        change_row(inventory, key, lambda row: row["data"]["binding"].update(generation=2))
    elif case == "terminal-id":
        key = n.native_key("evidence", job.last_evidence_id)
        change_row(inventory, key, lambda row: row["data"]["evidence"].update(evidence_id="invented-terminal-id"))
    elif case == "result":
        key = w.worker_key("completion", plan.binding.job_id, "1")
        change_row(inventory, key, lambda row: row["data"]["result"].update(unresolved_operations=[]))
    elif case == "model":
        sid = assistant_key("session-id", plan.binding.start_request_id)
        key = assistant_key("model-intent", sid, plan.model_request_sha256)
        change_row(inventory, key, lambda row: row["data"].update(consumed=False))
    elif case == "extra-field":
        key = recovery_key("authorization", plan.recovery_id)
        change_row(inventory, key, lambda row: row["data"].update(approved_by_model=True))
    elif case == "receipt-material":
        key = recovery_key("stop-intent", plan.recovery_id)
        payload = copy.deepcopy(inventory.receipts[key].payload)
        payload["request"]["read_set"] = []
        inventory.receipts[key] = Record(payload, inventory.receipts[key].etag)
    elif case == "orphan":
        key = recovery_key("stop-observation", plan.recovery_id)
        inventory.rows["unmatched-observation"] = inventory.rows[key]
    else:
        for kind in ("authorization", "stop-intent", "stop-observation"):
            inventory.rows.pop(recovery_key(kind, plan.recovery_id))
        inventory.rows.pop(n.native_key("platform-stop", plan.launch_id))
    with pytest.raises(Problem):
        validate_recovery_history(inventory, h.config)


@pytest.mark.parametrize("bound", [0, 33, True, "1"])
def test_validator_limits_are_strict(history, bound):
    with pytest.raises(Problem):
        validate_recovery_history(history.inventory, history.config, max_histories=bound)


def test_unrecognized_rows_are_preserved_and_remain_inventory_errors(history):
    h = history
    key = control_key("sdk-recovery-unsupported", "unmatched")
    h.store.transact(ControlRequest(request_id="offline-unknown-row", request_digest="0" * 64),
        lambda _: ControlChanges(writes=(ControlWrite(row_id=key, mode="create",
            data={"unrecognized_recovery_state": "unknown"}),), result={}))
    inventory = h.store.read_inventory()
    assert key not in validate_recovery_history(inventory, h.config)
    with pytest.raises(Problem) as denied:
        _quiescence(inventory, h.config)
    assert denied.value.body["error"]["code"] == "MIGRATION_UNCLASSIFIED_RECORD"
    assert h.store.read_inventory().rows[key] == inventory.rows[key]


def test_different_configuration_cannot_reinterpret_recovery_or_enable_assistant_upgrade(history):
    raw = history.config.model_dump(mode="json")
    raw["deployment_id"] = "unapproved-runtime-upgrade"
    changed = ServerConfiguration.model_validate_json(json.dumps(raw))
    with pytest.raises(Problem):
        validate_recovery_history(history.inventory, changed)
    from shared_context.behavior_runtime_transition import _config_change
    with pytest.raises(Problem) as denied:
        _config_change(history.config, changed)
    assert denied.value.body["error"]["code"] == "TRANSITION_CONFIG_CHANGE_DENIED"
