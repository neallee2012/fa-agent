"""Native-mode offline producers; no real stop, model or delivery endpoint."""

import copy
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from shared_context import behavior_delivery as d, behavior_foundry as n, behavior_workers as w
from shared_context.behavior_assistant import _Session, key as assistant_key
from shared_context.behavior_assistant_wire import Assignment, Task
from shared_context.behavior_inbox import InboxRegistration, PullInbox
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_operations import control_key
from shared_context.behavior_recovery import ClaimedSdkRecovery, PlatformStopObservation, PreSdkRecoveryScope, stop_endpoint
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_runtime_rotation import (
    RuntimeRotation, RotationPlan, RotationHistory, ModelLiability, _snapshot_store, rotation_key,
)
from shared_context.behavior_server_config import ServerConfiguration
from shared_context.behavior_settlement import (
    AuthorizeSettlement, RetainedResultSettlement, SettlementRegistration, SettlementUse,
    settlement_key,
)
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.source_materials import digest
from shared_context.errors import Problem
from shared_context.records import Record
from shared_context.behavior_runtime_transition import _payload_hash
from test_behavior_recovery_history import forbidden
from test_behavior_runtime_rotation import approved, rotated
from test_behavior_settlement import OfflineProtection, ack_command, fetch_command, write


def page_inventory(table):
    def query(*, query_filter, parameters, raw_request_hook, results_per_page, **kwargs):
        assert query_filter == "PartitionKey eq @partition" and kwargs["retry_total"] == 0
        values = [copy.deepcopy(value) for (name, partition, _), value in sorted(table.backing.rows.items())
                  if name == table.table_name and partition == parameters["partition"]]

        def pages():
            for offset in range(0, len(values), results_per_page):
                raw_request_hook(SimpleNamespace(http_request=SimpleNamespace(
                    method="GET", url=table.url + "/Behavior()", body=None)))
                yield iter(values[offset:offset + results_per_page])
        return SimpleNamespace(by_page=pages)
    table.query_entities = query


@pytest.fixture
def current_cancelled(rotated, request):
    """Seed only the claimed boundary; real recovery and settlement emit history."""
    h = rotated
    suffix = getattr(request, "id_suffix", "")
    if suffix:
        first = h.service.plan(h.principal, rotation_id="next-sdk" + suffix)
        approval = h.service.authorize(h.principal, first, decision_id="new-human-approval" + suffix,
                                       expires_at=h.h.time + timedelta(minutes=5))
    else:
        first, approval = approved(h)
    h.service.apply(h.principal, first, approval)
    h.service.verify(h.principal, first, approval)
    page_inventory(h.table)
    config, b = h.new, h.new.bindings[0]
    store = MigrationStore(h.table, h.store._scope)
    installed_origin(store, config)
    clock = [h.h.time]
    worker = w.WorkerCore(store, config.workers, adapter=None, clock=lambda: clock[0])
    authority = n.NativeLaunchAuthority(
        worker, team_id=config.identity.team_id, controller_id=config.controller_id,
        targets=config.active_targets,
        jobs=(n.NativeJobRoute(job_id=b.job_id, api_binding_id=b.binding_id,
                              grant_id=n.native_key("bootstrap-grant", b.job_id)),),
        verifier_version=config.delivery.verifier_version, clock=lambda: clock[0],
        recovery_binding=n.NativeRecoveryBinding(configuration_sha256=config.sha256,
            operator_oid=config.identity.human_oid, credential_client_id=config.identity.managed_identity_client_id),
    )
    worker.adapter = w.ExecutorAdapter(forbidden, forbidden, forbidden, authority.verify)
    inbox = PullInbox(store, InboxRegistration(config.authentication_version, forbidden, forbidden))
    delivery = d.DeliveryCore(store, clock=lambda: clock[0], registration=d.RuntimeRegistration(
        config.delivery, verify_work_result=worker.verify_work_result, channel_adapter=inbox))
    inbox.attach(delivery)
    profile = config.assistant.profiles[0]
    task = Task(job_id=b.job_id, work_id=b.work.work_id, definition_digest=digest(b.work.model_dump(mode="json")),
                profile_digest=digest(profile.model_dump(mode="json")), source=profile.source,
                artifact_id=profile.artifact_id, artifact_version=profile.artifact_version, operation_id=b.operation_id)
    body = task.model_dump_json().encode()
    execution = w.ExecutorBinding(
        job_id=b.job_id, work_id=b.work.work_id, generation=1, owner=b.owner,
        executor_id=config.workers.executor_id, executor_version=config.workers.executor_version,
        input_digest=hashlib.sha256(body).hexdigest(), start_request_id="current-pre-sdk-start" + suffix)
    start = w.ExecutorStart(binding=execution, input=body,
                            request=worker._request("start", (b.job_id,), {}, ()))
    launch_id = n.native_key("launch-id", execution.start_request_id)
    target = config.active_targets[0]
    launch = n._NativeLaunch(
        launch_id=launch_id, configuration_digest=worker.configuration_digest(), api_binding_id=b.binding_id,
        grant_id=n.native_key("bootstrap-grant", b.job_id), start=start, definition=b.work, target=target,
        process_generation=8, permit_digest="a" * 64, post_digest="b" * 64, phase="claimed",
        native_invocation_id="current-invocation", claim_id="current-claim", process_id="current-process",
        report_token_digest="c" * 64, evidence_sequence=16, probes_reserved=16, workload_version="assistant-v1.2")
    sid = assistant_key("session-id", execution.start_request_id)
    rows = {
        n.native_key("launch", launch_id): launch,
        n.native_key("target", target.executor_id): n._TargetReservation(
            target=target, generation=8, active_launch_id=launch_id, configuration_digest=worker.configuration_digest()),
        w.worker_key("job", b.job_id): w.JobRecord(
            configuration_digest=worker.configuration_digest(),
            spec=w.JobSpec(job_id=b.job_id, work_id=b.work.work_id, input=body, obligation_id=b.obligation_id),
            owner=b.owner, binding=execution, generation=1, state="capacity_uncertain",
            control_remaining=0, queries_reserved=16),
        w.worker_key("attempt", b.job_id, "1"): w._Attempt(start=start),
        w.worker_key("pool", config.workers.workspace_id): w._Pool(
            configuration_digest=worker.configuration_digest(), workspace_id=config.workers.workspace_id,
            slots=(w.Slot(number=0, binding=execution, lease_until=clock[0]),), pending=(b.job_id,)),
        assistant_key("session", sid): _Session(
            binding_id=b.binding_id, assignment=Assignment(binding_id=b.binding_id, session_id=sid,
                sdk_session_id=sid, task=task), execution=execution, ticket_digest="d" * 64),
        control_key("delivery-obligation", b.obligation_id): d._Obligation(
            configuration_digest=delivery.configuration_digest(), evidence_mode="native", obligation_id=b.obligation_id,
            event_id=b.event_id, root_request_id=b.root_request_id, work_id=b.work.work_id,
            owner_record_id=b.owner.owner_record_id, conversation_id=b.owner.conversation_id,
            root_budget_id=b.owner.root_budget_id, policy_revision=1, route=b.route,
            required_confirmation_level="client_received", channel_version=config.delivery.channel.version,
            review_at=clock[0], deadline=clock[0] + timedelta(minutes=10), normal_control_remaining=1),
        control_key("delivery-work-state", b.obligation_id): d._Work(
            configuration_digest=delivery.configuration_digest(), evidence_mode="native",
            event_id=b.event_id, definition=b.work, definition_digest=digest(b.work.model_dump(mode="json"))),
        control_key("delivery-conversation", b.owner.conversation_id): d._Conversation(
            configuration_digest=delivery.configuration_digest(), evidence_mode="native",
            conversation_id=b.owner.conversation_id, owner_record_id=b.owner.owner_record_id, epoch=1, sequence=1),
        control_key("delivery-intent", control_key("delivery-intent-id", b.event_id)): d._Intent(
            configuration_digest=delivery.configuration_digest(), evidence_mode="native",
            intent_id=control_key("delivery-intent-id", b.event_id), obligation_id=b.obligation_id,
            action="process_event", consumer_version=config.delivery.consumer_version, generation=0,
            state_row_id=control_key("delivery-work-state", b.obligation_id)),
    }
    for key, value in rows.items():
        write(store, key, value.model_dump(mode="json"),
              material=d._material(start.model_dump(mode="json")) if isinstance(value, w._Attempt) else None)
    for kind, value in (("root", b.owner.root_budget_id), ("team", config.identity.team_id),
                        ("entry", config.delivery.entry_id)):
        key = control_key("delivery-quota", kind, value)
        data = store.read_snapshot((key,))[key].payload["data"]
        write(store, key, {**data, "pending": 1})
    key = control_key("delivery-index", b.owner.root_budget_id)
    data = store.read_snapshot((key,))[key].payload["data"]
    entry = d.PendingEntry(obligation_id=b.obligation_id, owner_record_id=b.owner.owner_record_id,
                           conversation_id=b.owner.conversation_id, review_at=clock[0], generation=0,
                           automation="active", due_pending=False)
    write(store, key, {**data, "entries": [entry.model_dump(mode="json")]})
    clock[0] += getattr(request, "param", timedelta(hours=2))
    stops = []

    def stop(target, deadline):
        stops.append(target)
        return PlatformStopObservation(target=target, endpoint=stop_endpoint(target),
            credential_client_id=config.identity.managed_identity_client_id, status_code=204, observed_at=clock[0])

    recovery = ClaimedSdkRecovery(config, authority, PreSdkRecoveryScope(
        recovery_id="current-pre-sdk-recovery" + suffix, configuration_sha256=config.sha256, job_id=b.job_id,
        launch_id=launch_id, model_request_sha256=None, operator_oid=config.identity.human_oid,
        expires_at=clock[0] + timedelta(minutes=10), recovery_phase="before_sdk_acquire"),
        stop, delivery=delivery, clock=lambda: clock[0])
    decision = recovery.authorize(h.principal, recovery.plan(h.principal), "current-explicit-cancellation")
    recovery.execute(h.principal, decision)
    runtime = SimpleNamespace(store=store, dep=SimpleNamespace(delivery=delivery, inbox=inbox, workers=worker),
        mode="native", bindings={b.binding_id: b}, _now=lambda: clock[0],
        binding_digest=lambda binding: digest(["fa-behavior-api-v1", "native", binding.model_dump(mode="json")]),
        job_input=lambda _: body, _gate_keys=lambda binding: (
            runtime_key("policy", binding.binding_id), control_key("owner", binding.owner.owner_record_id),
            control_key("policy", binding.owner.policy_id), control_key("budget", binding.owner.root_budget_id)))
    registration = SettlementRegistration(
        settlement_id="current-retained-settlement" + suffix, binding_id=b.binding_id, configuration_sha256=config.sha256,
        expires_at=clock[0] + timedelta(minutes=10), protection_evidence_id="current-retention-observation",
        protection_verifier_version="offline-retention-v1", protection_verifier=OfflineProtection(clock[0], "native"))
    service = RetainedResultSettlement(config, runtime, registration)
    inbox.register_retained_results(service)
    return SimpleNamespace(h=h, config=config, store=store, runtime=runtime, service=service,
                           principal=h.principal, clock=clock, stops=stops, first_plan=first)


def reread(case):
    before = case.store.read_inventory().entity_sha256
    count = len(case.h.table.submissions)
    assert installed_origin(MigrationStore(case.h.table, case.store._scope), case.config) == case.h.original
    assert case.store.read_inventory().entity_sha256 == before
    assert len(case.h.table.submissions) == count


def test_current_settlement_persistence_points_allow_read_only_installation(current_cancelled):
    c = current_cancelled
    reread(c)
    original = c.store.read_inventory()
    plan = c.service.preflight(c.principal)
    reread(c)
    assert c.service.prepare(c.principal) == plan
    reread(c)
    grant = c.service.authorize(c.principal, AuthorizeSettlement(
        plan_sha256=plan.sha256, decision_id="current-delivery-approved", decision="settle_retained_result"))
    reread(c)
    received = c.service.fetch(c.principal, fetch_command(grant))
    reread(c)
    receipt = c.service.acknowledge(c.principal, ack_command(grant, received))
    reread(c)
    assert c.service.reconcile(c.principal, SettlementUse(authorization_id=grant.authorization_id)) == receipt
    reread(c)
    assert receipt.payload["result"]["fulfillment"] == "fulfilled" and len(c.stops) == 1
    after = c.store.read_inventory()
    b = c.config.bindings[0]
    for key in (runtime_key("policy", b.binding_id), control_key("budget", b.owner.root_budget_id),
                n.native_key("grant", n.native_key("bootstrap-grant", b.job_id))):
        assert after.rows[key] == original.rows[key]


@pytest.mark.parametrize("rotated", ["pre-sdk"], indirect=True)
def test_zero_model_cancellation_rotates_without_inventing_a_model_digest(rotated):
    plan, auth = approved(rotated)
    assert plan.liability.model_outcome == "not_requested"
    assert plan.liability.model_request_sha256 is None
    assert auth.action == "rotate-sdk-retain-cancelled-pre-sdk-result"
    rotated.service.apply(rotated.principal, plan, auth)
    rotated.service.verify(rotated.principal, plan, auth)


def next_rotation(c, *, suffix=""):
    raw = c.config.model_dump(mode="json")
    raw["deployment_id"] = "third-sdk-installation" + suffix
    b = raw["bindings"][0]
    for field in ("binding_id", "event_id", "job_id", "operation_id", "attempt_id", "grant_id"):
        b[field] = "third-" + b[field]
    for field in ("owner_record_id", "conversation_id", "policy_id"):
        b["owner"][field] = "third-" + b["owner"][field]
    b["work"]["work_id"] = "third-" + b["work"]["work_id"]
    b["claim_ids"] = ["third-claim" + suffix]
    raw["policies"][0]["binding_id"] = b["binding_id"]
    raw["policies"][0]["policy"]["binding_digest"] = digest(["fa-behavior-api-v1", "native", b])
    raw["policies"][0]["policy"]["valid_until"] = (c.clock[0] + timedelta(hours=1)).isoformat()
    raw["operations"][0]["binding_id"] = b["binding_id"]
    raw["assistant"]["profiles"][0].update(binding_id=b["binding_id"], claim_id="third-claim" + suffix,
                                          artifact_id="third-artifact" + suffix)
    raw["targets"][0].update(agent_version=str(int(c.config.targets[0].agent_version) + 1),
                             session_id="third-sdk-session" + suffix)
    new = ServerConfiguration.model_validate_json(json.dumps(raw))
    b, p = new.bindings[0], new.assistant.profiles[0]
    task = Task(job_id=b.job_id, work_id=b.work.work_id, definition_digest=digest(b.work.model_dump(mode="json")),
                profile_digest=digest(p.model_dump(mode="json")), source=p.source, artifact_id=p.artifact_id,
                artifact_version=p.artifact_version, operation_id=b.operation_id)
    grant = n.NativeExecutionGrant(
        grant_id=n.native_key("bootstrap-grant", b.job_id), controller_id=new.controller_id,
        owner=b.owner, policy_revision=1, job_id=b.job_id, definition=b.work,
        input_digest=hashlib.sha256(task.model_dump_json().encode()).hexdigest(),
        targets=new.active_targets, active=True, remaining_uses=1, not_before=c.clock[0],
        expires_at=c.clock[0] + timedelta(minutes=15), workload_version="assistant-v1.2")
    paths = c.h.paths
    for path in (paths.sources, paths.backups, paths.retirement):
        data = json.loads(path.read_bytes())
        def fresh(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"observed_at", "inventory_observed_at"}:
                        value[key] = c.clock[0].isoformat()
                    elif key in {"valid_until", "inventory_valid_until"}:
                        value[key] = (c.clock[0] + timedelta(minutes=15)).isoformat()
                    else:
                        fresh(item)
            elif isinstance(value, list):
                for item in value:
                    fresh(item)
        fresh(data)
        if path == paths.retirement:
            data.update(old_configuration_sha256=c.config.sha256, new_configuration_sha256=new.sha256,
                        old_code_sha256=c.first_plan.code_sha256)
            data["targets"][0]["target"] = c.config.active_targets[0].model_dump(mode="json")
            data["current_targets"][0]["target"] = new.active_targets[0].model_dump(mode="json")
        path.write_text(json.dumps(data), encoding="utf-8")
    return RuntimeRotation(MigrationStore(c.h.table, c.store._scope), c.config, new, paths,
                           native_grants=(grant,), clock=lambda: c.clock[0], allow_apply=True)


def settle(c):
    plan = c.service.prepare(c.principal)
    grant = c.service.authorize(c.principal, AuthorizeSettlement(
        plan_sha256=plan.sha256, decision_id="current-delivery-approved", decision="settle_retained_result"))
    received = c.service.fetch(c.principal, fetch_command(grant))
    c.service.acknowledge(c.principal, ack_command(grant, received))
    return grant, received


@pytest.mark.parametrize("lost_response", [False, True])
def test_second_rotation_preserves_both_old_unknown_and_current_zero_model(current_cancelled, lost_response):
    c = current_cancelled
    settle(c)
    reread(c)
    service = next_rotation(c)
    before = c.store.read_inventory()
    plan = service.plan(c.principal, rotation_id="third-sdk")
    approval = service.authorize(c.principal, plan, decision_id="third-approved",
                                 expires_at=c.clock[0] + timedelta(minutes=5))
    assert plan.liability.model_outcome == "not_requested" and plan.liability.model_request_sha256 is None
    assert plan.schema_version == "fa-runtime-rotation-plan-v2"
    count = len(c.h.table.submissions)
    if lost_response:
        c.h.table.failure = "after"
        with pytest.raises(Problem):
            service.apply(c.principal, plan, approval)
        c.h.table.failure = None
        service.reconcile(c.principal, plan, approval)
    else:
        service.apply(c.principal, plan, approval)
    assert len(c.h.table.submissions) == count + 1
    service.verify(c.principal, plan, approval)
    assert installed_origin(MigrationStore(c.h.table, c.store._scope), service.new) == c.h.original
    after = service.store.read_inventory()
    changes = {image.row_id for image in plan.images}
    for key, row in before.rows.items():
        if key not in changes and key != control_key("runtime-rotation-current", c.config.table.scope.authority_domain):
            assert after.rows[key] == row
    assert all(after.receipts[key] == row for key, row in before.receipts.items())
    assert c.first_plan.liability.model_outcome == "unknown"
    assert c.first_plan.liability.model_request_sha256 == "e" * 64


@pytest.mark.parametrize("stage", ["plan", "authorization", "fetch", "ack", "confirmation"])
def test_lost_response_at_each_durable_stage_is_readable_without_resend(current_cancelled, monkeypatch, stage):
    c = current_cancelled
    s = c.service
    plan = s.preflight(c.principal)
    grant = received = None
    if stage != "plan":
        s.prepare(c.principal)
    if stage not in {"plan", "authorization"}:
        grant = s.authorize(c.principal, AuthorizeSettlement(
            plan_sha256=plan.sha256, decision_id="lost-response-decision", decision="settle_retained_result"))
    if stage in {"ack", "confirmation"}:
        received = s.fetch(c.principal, fetch_command(grant))
    original = c.store.transact
    lost = []
    def transact(request, decide, **kwargs):
        result = original(request, decide, **kwargs)
        selected = {
            "plan": settlement_key("plan", plan.settlement_id),
            "authorization": settlement_key("authorization", plan.settlement_id),
            "fetch": None,
            "ack": control_key("inbox-acknowledge", plan.delivery.delivery_id),
            "confirmation": control_key("delivery-request", "receipt", plan.delivery.obligation_id,
                                        plan.delivery.delivery_id,
                                        control_key("inbox-ack-evidence", plan.delivery.delivery_id)),
        }[stage]
        if not lost and (request.request_id == selected or stage == "fetch"):
            lost.append(request)
            raise Problem(503, "SYNTHETIC_RESPONSE_LOST", "offline commit succeeded but response was lost")
        return result
    monkeypatch.setattr(c.store, "transact", transact)
    with pytest.raises(Problem) as unknown:
        if stage == "plan":
            s.prepare(c.principal)
        elif stage == "authorization":
            s.authorize(c.principal, AuthorizeSettlement(
                plan_sha256=plan.sha256, decision_id="lost-response-decision", decision="settle_retained_result"))
        elif stage == "fetch":
            s.fetch(c.principal, fetch_command(grant))
        else:
            s.acknowledge(c.principal, ack_command(grant, received))
    assert unknown.value.body["error"]["code"] == "SYNTHETIC_RESPONSE_LOST"
    assert len(lost) == 1
    monkeypatch.setattr(c.store, "transact", original)
    count = len(c.h.table.submissions)
    reread(c)
    c.store.reconcile(lost[0])
    assert len(c.h.table.submissions) == count
    obligation = c.store.read_snapshot((control_key("delivery-obligation", plan.delivery.obligation_id),))[
        control_key("delivery-obligation", plan.delivery.obligation_id)].payload["data"]
    assert obligation["fulfillment"] == ("fulfilled" if stage == "confirmation" else "pending")
    if stage == "confirmation":
        receipt = s.reconcile(c.principal, SettlementUse(authorization_id=grant.authorization_id))
        assert receipt.payload["result"]["fulfillment"] == "fulfilled"
        assert len(c.h.table.submissions) == count
    else:
        with pytest.raises(Problem):
            next_rotation(c).plan(c.principal, rotation_id="must-not-ignore-pending")
    if stage == "ack":
        saved_ack = c.store.reconcile(lost[0])
        receipt = s.reconcile(c.principal, SettlementUse(authorization_id=grant.authorization_id))
        assert receipt.payload["result"]["fulfillment"] == "fulfilled"
        assert c.store.reconcile(lost[0]) == saved_ack
        assert len(c.h.table.submissions) == count + 1
        reread(c)


@pytest.mark.parametrize("stage", ["consumer-plan", "outbox"])
def test_preflight_response_loss_does_not_require_a_settlement_plan_to_read_installation(
    current_cancelled, monkeypatch, stage,
):
    c = current_cancelled
    b = c.config.bindings[0]
    selected = (control_key("delivery-consumer-claim", b.obligation_id) if stage == "consumer-plan" else
                control_key("delivery-request", "message", b.obligation_id,
                            control_key("delivery-final-id", b.obligation_id)))
    original = c.store.transact
    lost = []
    def transact(request, decide, **kwargs):
        result = original(request, decide, **kwargs)
        if request.request_id == selected:
            lost.append(request)
            raise Problem(503, "SYNTHETIC_RESPONSE_LOST", "offline preflight response lost")
        return result
    monkeypatch.setattr(c.store, "transact", transact)
    with pytest.raises(Problem, match="offline preflight response lost"):
        c.service.preflight(c.principal)
    monkeypatch.setattr(c.store, "transact", original)
    assert len(lost) == 1
    reread(c)
    count = len(c.h.table.submissions)
    c.store.reconcile(lost[0])
    assert len(c.h.table.submissions) == count


@pytest.mark.parametrize("mutation", [
    "missing-plan", "missing-authorization", "missing-fetch-receipt", "missing-ack-receipt",
    "missing-confirmation", "wrong-plan-receipt", "wrong-read-set", "wrong-write-set", "wrong-material",
    "wrong-route", "wrong-config", "unknown-row", "wrong-ack-proof",
])
def test_settlement_history_rejects_missing_or_mismatched_evidence(current_cancelled, mutation):
    c = current_cancelled
    grant, received = settle(c)
    inventory = c.store.read_inventory()
    rows = copy.deepcopy(dict(inventory.rows))
    receipts = copy.deepcopy(dict(inventory.receipts))
    hashes = dict(inventory.entity_sha256)
    plan_key = settlement_key("plan", grant.plan.settlement_id)
    exkey = control_key("inbox-exchange", grant.plan.delivery.delivery_id)
    ex = rows[exkey].payload["data"]
    proofkey = control_key("inbox-receipt-evidence", ex["receipt_evidence_id"])
    if mutation in {"missing-plan", "missing-authorization", "unknown-row"}:
        if mutation == "unknown-row":
            payload = {**rows[plan_key].payload, "row_id": "unknown-current-history", "data": {"unrecognized": True}}
            rows["unknown-current-history"] = Record(payload, 'W/"unknown"')
        else:
            rows.pop(plan_key if mutation == "missing-plan" else grant.authorization_id)
    elif mutation.startswith("missing-") or mutation in {"wrong-plan-receipt", "wrong-read-set", "wrong-write-set"}:
        key = {
            "missing-fetch-receipt": ex["fetch_request"]["request_id"],
            "missing-ack-receipt": control_key("inbox-acknowledge", grant.plan.delivery.delivery_id),
            "missing-confirmation": control_key("delivery-request", "receipt", grant.plan.delivery.obligation_id,
                grant.plan.delivery.delivery_id, ex["receipt_evidence_id"]),
        }.get(mutation, plan_key)
        if mutation.startswith("missing-"):
            receipts.pop(key)
        else:
            row = receipts[key]
            payload = copy.deepcopy(row.payload)
            if mutation == "wrong-plan-receipt":
                payload["request"]["request_digest"] = "f" * 64
            elif mutation == "wrong-read-set":
                payload["request"]["read_set"] = []
            else:
                payload["writes"] = []
            receipts[key] = Record(payload, row.etag)
            hashes["behavior:v1:receipt:" + key] = _payload_hash(payload)
    else:
        key = proofkey if mutation == "wrong-ack-proof" else plan_key
        row = rows[key]
        payload = copy.deepcopy(row.payload)
        if mutation == "wrong-material":
            payload["material"] = d._material({"wrong": True}).model_dump(mode="json")
        elif mutation == "wrong-route":
            payload["data"]["delivery"]["route"]["recipient_id"] = "foreign-recipient"
        elif mutation == "wrong-config":
            payload["data"]["configuration_sha256"] = "f" * 64
        else:
            payload["data"]["proof"] = "dGFtcGVyZWQ="
        rows[key] = Record(payload, row.etag)
        hashes["behavior:v1:row:" + key] = _payload_hash(payload)
    broken = replace(inventory, rows=rows, receipts=receipts, entity_sha256=hashes)
    before = c.store.read_inventory().entity_sha256
    with pytest.raises(Problem):
        installed_origin(_snapshot_store(c.store, broken, c.config), c.config)
    assert c.store.read_inventory().entity_sha256 == before


def test_legacy_liability_and_first_rotation_json_keep_the_original_shape(rotated):
    plan, auth = approved(rotated)
    expected = {
        "configuration_sha256": plan.old_configuration_sha256,
        "job_id": rotated.old.bindings[0].job_id, "work_id": rotated.old.bindings[0].work.work_id,
        "root_budget_id": rotated.old.bindings[0].owner.root_budget_id,
        "model_request_sha256": "e" * 64, "model_outcome": "unknown",
        "final_delivery_id": plan.liability.final_delivery_id, "receipt_evidence_id": plan.liability.receipt_evidence_id,
    }
    assert plan.liability.model_dump_json() == json.dumps(expected, separators=(",", ":"))
    assert isinstance(plan.liability, ModelLiability) and plan.schema_version == "fa-runtime-rotation-plan-v1"
    assert RotationPlan.model_validate_json(plan.model_dump_json()).model_dump_json() == plan.model_dump_json()
    rotated.service.apply(rotated.principal, plan, auth)
    key = rotation_key("history", plan.rotation_id)
    raw = rotated.store.read_snapshot((key,))[key].payload["data"]
    assert "previous_current" not in raw
    assert RotationHistory.model_validate_json(json.dumps(raw)).model_dump(mode="json") == raw


@pytest.mark.parametrize("mutation", ["fake-model-digest", "relabel-unknown", "unknown-outcome"])
def test_liability_discriminator_cannot_convert_absence_into_unknown(rotated, mutation):
    from pydantic import ValidationError
    plan, _ = approved(rotated)
    data = plan.model_dump(mode="json")
    if mutation == "fake-model-digest":
        data["liability"]["model_outcome"] = "not_requested"
    elif mutation == "relabel-unknown":
        data["liability"]["model_request_sha256"] = None
    else:
        data["liability"]["model_outcome"] = "succeeded"
    with pytest.raises(ValidationError):
        RotationPlan.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("mutation", ["old-pointer", "old-history", "old-receipt", "new-history"])
def test_second_rotation_rejects_broken_lineage_without_writes(current_cancelled, mutation):
    c = current_cancelled
    settle(c)
    service = next_rotation(c)
    plan = service.plan(c.principal, rotation_id="third-sdk-corruption")
    auth = service.authorize(c.principal, plan, decision_id="third-approved",
                             expires_at=c.clock[0] + timedelta(minutes=5))
    service.apply(c.principal, plan, auth)
    inventory = service.store.read_inventory()
    rows, receipts, hashes = copy.deepcopy(dict(inventory.rows)), dict(inventory.receipts), dict(inventory.entity_sha256)
    if mutation == "old-receipt":
        receipts.pop(rotation_key("apply", c.first_plan.rotation_id))
    elif mutation == "old-history":
        rows.pop(rotation_key("history", c.first_plan.rotation_id))
    else:
        key = rotation_key("history", plan.rotation_id)
        row = rows[key]
        payload = copy.deepcopy(row.payload)
        if mutation == "old-pointer":
            payload["data"]["previous_current"]["configuration_sha256"] = "f" * 64
        else:
            payload["data"]["plan"]["liability"]["model_request_sha256"] = "f" * 64
        rows[key] = Record(payload, row.etag)
        hashes["behavior:v1:row:" + key] = _payload_hash(payload)
    broken = replace(inventory, rows=rows, receipts=receipts, entity_sha256=hashes)
    count = len(c.h.table.submissions)
    with pytest.raises(Problem):
        installed_origin(_snapshot_store(service.store, broken, service.new), service.new)
    assert len(c.h.table.submissions) == count


@pytest.mark.parametrize("scans", [1, 3])
def test_current_settlement_does_not_freeze_later_receipt_bound_empty_scans(current_cancelled, scans):
    c = current_cancelled
    settle(c)
    b = c.config.bindings[0]
    consumer = d.DeliveryConsumer(c.runtime.dep.delivery)
    for _ in range(scans):
        consumer.drain(b.owner.root_budget_id, lambda _: b.owner, "post-settlement-offline-scan", limit=1)
    reread(c)
    plan = next_rotation(c).plan(c.principal, rotation_id="after-empty-scan")
    assert plan.liability.model_outcome == "not_requested"


@pytest.mark.parametrize("mutation", ["missing-done", "wrong-scan-limit", "refund"])
def test_late_quota_changes_need_exact_original_empty_scan_receipts(current_cancelled, mutation):
    c = current_cancelled
    settle(c)
    b = c.config.bindings[0]
    d.DeliveryConsumer(c.runtime.dep.delivery).drain(
        b.owner.root_budget_id, lambda _: b.owner, "offline-late-scan", limit=1)
    inventory = c.store.read_inventory()
    receipts, hashes = dict(inventory.receipts), dict(inventory.entity_sha256)
    if mutation == "missing-done":
        receipts.pop(control_key("delivery-consumer-page-done", b.owner.root_budget_id, "1"))
    elif mutation == "wrong-scan-limit":
        key = control_key("delivery-consumer-scan", b.owner.root_budget_id, "0")
        row = receipts[key]
        payload = copy.deepcopy(row.payload)
        payload["request"]["request_digest"] = "f" * 64
        receipts[key] = Record(payload, row.etag)
        hashes["behavior:v1:receipt:" + key] = _payload_hash(payload)
    else:
        key = control_key("delivery-quota", "root", b.owner.root_budget_id)
        data = inventory.rows[key].payload["data"]
        write(c.store, key, {**data, "scans_remaining": data["scans_remaining"] + 1})
        inventory = c.store.read_inventory()
        receipts, hashes = dict(inventory.receipts), dict(inventory.entity_sha256)
    broken = replace(inventory, receipts=receipts, entity_sha256=hashes)
    before = c.store.read_inventory().entity_sha256
    with pytest.raises(Problem):
        installed_origin(_snapshot_store(c.store, broken, c.config), c.config)
    assert c.store.read_inventory().entity_sha256 == before
