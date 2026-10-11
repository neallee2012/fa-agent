"""Read-only retained settlement history, including incomplete durable stages.

Uses the producer's terminal-payload description, never its approval, clock,
readiness or mutation paths. Classification does not mean delivered.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from . import behavior_delivery as d, behavior_inbox as inbox, behavior_workers as w
from . import behavior_migration as m, behavior_operations as op
from .behavior_recovery_history import _Inventory
from .behavior_runtime import runtime_key
from .behavior_settlement import (
    RetainedResultSettlement, SettlementAuthorization, SettlementPlan, _Revocation, settlement_key,
)
from .behavior_store import ControlRequest
from .source_materials import digest

KINDS = {
    "retained_result_settlement_plan",
    "retained_result_settlement_authorization",
    "retained_result_settlement_revocation",
}


def require(condition, message):
    if not condition:
        raise m._problem("ROTATION_SETTLEMENT_HISTORY_INVALID", message)


def _no_clock():
    raise m._problem("ROTATION_SETTLEMENT_HISTORY_INVALID", "historical classification cannot read a live clock")


def _no_dispatch(*args):
    raise m._problem("ROTATION_SETTLEMENT_HISTORY_INVALID", "historical classification cannot acquire live evidence")


def _description(view, binding, settlement_id):
    """Compose only the existing deterministic producer checks over a snapshot."""
    config = view.config
    store = SimpleNamespace(_scope=config.table.scope, _binding=view.guard.binding)
    worker = w.WorkerCore(store, config.workers, adapter=None, clock=_no_clock)
    channel = inbox.PullInbox(store, inbox.InboxRegistration(config.authentication_version, _no_dispatch, _no_dispatch))
    core = d.DeliveryCore(store, clock=_no_clock, registration=d.RuntimeRegistration(
        config.delivery, verify_work_result=_no_dispatch, channel_adapter=channel))
    channel.attach(core)
    job = view.row(w.worker_key("job", binding.job_id), w.JobRecord)
    from .behavior_assistant_wire import Task
    profile = next(item for item in config.assistant.profiles if item.binding_id == binding.binding_id)
    task = Task(
        job_id=binding.job_id, work_id=binding.work.work_id,
        definition_digest=digest(binding.work.model_dump(mode="json")),
        profile_digest=digest(profile.model_dump(mode="json")), source=profile.source,
        artifact_id=profile.artifact_id, artifact_version=profile.artifact_version,
        operation_id=binding.operation_id,
    )
    require(job.spec.input == task.model_dump_json().encode(), "settlement task differs from configured input")
    runtime = SimpleNamespace(
        store=store, dep=SimpleNamespace(delivery=core, inbox=channel, workers=worker), mode="native",
        bindings={binding.binding_id: binding},
        binding_digest=lambda b: digest(["fa-behavior-api-v1", "native", b.model_dump(mode="json")]),
        job_input=lambda _: task.model_dump_json().encode(),
        _gate_keys=lambda b: (
            runtime_key("policy", b.binding_id), op.control_key("owner", b.owner.owner_record_id),
            op.control_key("policy", b.owner.policy_id), op.control_key("budget", b.owner.root_budget_id)),
    )
    service = RetainedResultSettlement(config, runtime)
    service.binding = binding
    service.registration = SimpleNamespace(settlement_id=settlement_id)
    plan = service._describe(view.inventory.rows)
    require(plan.context == binding.owner, "settlement does not retain the configured execution owner")
    for item in plan.retained_materials:
        row = view.inventory.rows[item.row_id]
        require(row.payload["material"] is not None and row.payload["material"]["sha256"] == item.sha256,
                "settlement retained material is missing or differs")
    return service, plan


def _preflight(view):
    classified = set()
    for binding in view.config.bindings:
        key = op.control_key("delivery-consumer-plan", binding.obligation_id)
        row = view.inventory.rows.get(key)
        if row is None or row.payload["data"].get("worker_id") != "retained-result-settlement":
            continue
        value = view.row(key, d._ConsumerPlan, material=True)
        context = binding.owner
        obkey = op.control_key("delivery-obligation", binding.obligation_id)
        workkey = op.control_key("delivery-work-state", binding.obligation_id)
        intentkey = op.control_key("delivery-intent", op.control_key("delivery-intent-id", binding.event_id))
        intent = view.row(intentkey, d._Intent)
        ob = view.row(obkey, d._Obligation)
        work = view.row(workkey, d._Work)
        contract = digest({"contract": view.config.delivery.model_dump(mode="json"), "mode": "native"})
        require(value.configuration_digest == intent.configuration_digest == ob.configuration_digest == contract
                and value.evidence_mode == intent.evidence_mode == "native"
                and value.context == context and value.obligation_id == binding.obligation_id
                and value.intent_id == intent.intent_id == op.control_key("delivery-intent-id", binding.event_id)
                and intent.obligation_id == binding.obligation_id and intent.state_row_id == workkey
                and intent.action == "process_event"
                and value.consumer_version == intent.consumer_version == view.config.delivery.consumer_version
                and value.command.obligation_id == binding.obligation_id
                and value.command.delivery_id == op.control_key("delivery-final-id", binding.obligation_id)
                and value.command.message_kind == ("final" if work.state == "succeeded" else "failure")
                and work.result is not None, "retained preflight is not the exact terminal consumer plan")
        common = (op.control_key("owner", context.owner_record_id),
                  op.control_key("delivery-conversation", context.conversation_id),
                  obkey, workkey, op.control_key("budget", context.root_budget_id), key, intentkey)
        claim = view.receipt(ControlRequest(
            request_id=op.control_key("delivery-consumer-claim", binding.obligation_id),
            request_digest=digest([binding.obligation_id, contract]), read_set=tuple(sorted(common))),
            (key,), exact={key: view.hash(key)}, result={
                "actual_environment": "NOT_RUN", "execution_enabled": False, "plan_row": key})
        outkey = op.control_key("delivery-outbox", value.command.delivery_id)
        request = ControlRequest(
            request_id=op.control_key("delivery-request", "message", binding.obligation_id, value.command.delivery_id),
            request_digest=digest(["message", value.command.model_dump(mode="json"), context.model_dump(mode="json"), contract]),
            read_set=tuple(sorted((common[0], common[1],
                op.control_key("delivery-index", context.root_budget_id), obkey, workkey, outkey))))
        require(value.request == request, "preflight message request differs")
        if outkey in view.inventory.rows:
            service, plan = _description(view, binding, "history-preflight")
            require(plan.delivery.version == value.command.expected_version + 1, "preflight version differs")
            out = view.row(outkey, d._Outbox)
            original = out.model_copy(update={"send_outcome": None, "confirmed_levels": (), "receipt_count": 0})
            message = view.receipt(request, (obkey, outkey), exact={outkey: view.image_hash(outkey, original)},
                                  result=service.core._result(binding=plan.delivery.model_dump(mode="json"),
                                      message_kind=out.message_kind, obligation_fulfilled=False))
            require(claim.guard_revision < message.guard_revision, "outbox precedes its consumer claim")
        else:
            require(ob.final_delivery_id is None and request.request_id not in view.receipts,
                    "preflight outbox disappeared after commit")
        classified.add(key)
    return classified


def _authorization(view, key, plan, reads):
    auth = view.row(key, SettlementAuthorization, material=True)
    proof = auth.protection
    require(key == auth.authorization_id == settlement_key("authorization", plan.settlement_id)
            and auth.plan == plan and auth.principal_id == view.config.identity.human_oid
            and auth.decision.plan_sha256 == plan.sha256
            and proof.configuration_sha256 == plan.configuration_sha256
            and proof.control_binding == plan.control_binding and proof.plan_sha256 == plan.sha256
            and proof.retained_materials == plan.retained_materials and proof.evidence_mode == "native"
            and proof.authentication_version == view.config.authentication_version
            and proof.observed_at <= auth.approved_at < auth.expires_at <= proof.expires_at
            and auth.expires_at <= proof.observed_at + timedelta(minutes=5)
            and auth.expires_at <= auth.approved_at + timedelta(minutes=15),
            "settlement authorization/protection/configuration differs")
    view.receipt(ControlRequest(request_id=key, request_digest=digest(auth.decision.model_dump(mode="json")),
                                read_set=reads),
                 (key,), exact={key: view.hash(key)}, result={
                     "authorization_id": key, "plan_sha256": plan.sha256, "delivered": False})
    return auth


def _access(view, access, auth, action):
    require(access.action == action and access.binding == auth.plan.delivery
            and access.principal_id == auth.principal_id
            and access.authentication_version == view.config.authentication_version
            and access.policy_revision == auth.plan.operation_policy_revision
            and access.observed_at == auth.approved_at and access.expires_at == auth.expires_at
            and access.proof == d._material({
                "authorization_id": auth.authorization_id,
                "authorization_sha256": digest(auth.model_dump(mode="json")),
            }).data, "settlement access differs from its independent grant")


def settled_root_quota_hash(view, confirmation, root_id):
    """Replay only exact completed empty scans after the final receipt."""
    key = op.control_key("delivery-quota", "root", root_id)
    later = sorted((receipt for receipt in view.receipts.values()
                    if receipt.guard_revision > confirmation.guard_revision and key in dict(receipt.writes)),
                   key=lambda receipt: receipt.guard_revision)
    if not later:
        require(dict(confirmation.writes).get(key) == view.hash(key), "settled quota lost its original write image")
        return view.hash(key)
    require(0 < len(later) <= 1000, "unrecognized quota mutation after settlement")
    quota = view.row(key, d.QuotaRecord)
    require(quota.scope_id == root_id and quota.scope_kind == "root" and quota.pending == 0
            and quota.scans_remaining + len(later) <= 1000, "settled root quota changed outside empty scans")
    cursor_key = op.control_key("delivery-consumer-cursor", root_id)
    index_key = op.control_key("delivery-index", root_id)
    cursor = view.row(cursor_key, d._ConsumerPage)
    require(not cursor.active and not view.row(index_key, d.PendingIndexRecord).entries,
            "post-settlement scan is not drained")
    previous_sequence = None
    for index, receipt in enumerate(later):
        page = m._parsed(receipt.result.get("page"), d._ConsumerPage)
        page_key = op.control_key("delivery-consumer-page", root_id, str(page.sequence))
        require(page.root_budget_id == root_id and page.active and not page.selected and page.offset == 0
                and page.configuration_digest == quota.configuration_digest and page.evidence_mode == "native"
                and (previous_sequence is None or page.sequence == previous_sequence + 1)
                and view.row(page_key, d._ConsumerPage, material=True) == page
                and receipt.result == {"actual_environment": "NOT_RUN", "execution_enabled": False,
                                      "page": page.model_dump(mode="json")}
                and receipt.request.request_id == op.control_key("delivery-consumer-scan", root_id, str(page.sequence - 1))
                and receipt.request.read_set == (index_key, cursor_key, key)
                and receipt.request.request_digest in {
                    digest([root_id, page.sequence - 1, limit, quota.configuration_digest]) for limit in range(1, 17)
                }, "post-settlement quota writer is not a bounded original empty scan")
        after = quota.model_copy(update={"scans_remaining": quota.scans_remaining + len(later) - index - 1})
        require(dict(receipt.writes) == {
            cursor_key: view.image_hash(cursor_key, page), page_key: view.hash(page_key), key: view.image_hash(key, after),
        }, "empty scan write-set or exact quota decrement differs")
        done = view.receipt(ControlRequest(
            request_id=op.control_key("delivery-consumer-page-done", root_id, str(page.sequence)),
            request_digest=digest(page.model_dump(mode="json")), read_set=(cursor_key,)),
            (cursor_key,), exact={cursor_key: view.image_hash(cursor_key, page.model_copy(update={"active": False}))},
            result={"actual_environment": "NOT_RUN", "execution_enabled": False, "page_completed": page.sequence})
        require(receipt.guard_revision < done.guard_revision
                and (index + 1 == len(later) or done.guard_revision < later[index + 1].guard_revision),
                "empty scan completion order differs")
        previous_sequence = page.sequence
    require(cursor.sequence == previous_sequence, "current cursor differs from final retained empty scan")
    return view.image_hash(key, quota.model_copy(update={"scans_remaining": quota.scans_remaining + len(later)}))


def _exchange(view, plan, auth, reads):
    binding, context = plan.delivery, plan.context
    exkey = op.control_key("inbox-exchange", binding.delivery_id)
    if exkey not in view.inventory.rows:
        return set()
    ex = view.row(exkey, inbox._Exchange)
    view.row(exkey, inbox._Exchange, material=ex.authorization_evidence.model_dump(mode="json"))
    _access(view, ex.authorization_evidence, auth, "fetch")
    require(ex.binding == binding and ex.principal_id == auth.principal_id, "settlement exchange scope differs")
    base = (
        op.control_key("owner", context.owner_record_id),
        op.control_key("delivery-conversation", context.conversation_id),
        op.control_key("delivery-obligation", binding.obligation_id),
        op.control_key("delivery-outbox", binding.delivery_id),
        op.control_key("policy", context.policy_id), exkey,
    )
    command = inbox.InboxFetch(obligation_id=binding.obligation_id, delivery_id=binding.delivery_id,
                               access_evidence_id=ex.authorization_evidence.evidence_id)
    request = ControlRequest(
        request_id=op.control_key("inbox-fetch", binding.delivery_id, command.access_evidence_id),
        request_digest=digest([command.model_dump(mode="json"), context.model_dump(mode="json"),
                               ex.authorization_evidence.model_dump(mode="json")]),
        read_set=tuple(dict.fromkeys((*base, *reads))),
    )
    require(ex.fetch_request == request and ex.fetch_count == 1, "unrecognized repeated settlement exchange")
    fetch = view.receipts.get(request.request_id)
    require(fetch is not None and fetch.request == request and fetch.result == {"exchange_id": ex.exchange_id}
            and set(dict(fetch.writes)) == {exkey, base[2], base[3]}, "settlement fetch receipt differs")
    first = ex.model_copy(update={"acknowledged": False, "receipt_evidence_id": None})
    require(dict(fetch.writes)[exkey] == view.image_hash(exkey, first), "original exchange image differs")
    keys = {exkey}
    if not ex.acknowledged:
        require(ex.receipt_evidence_id is None, "unacknowledged exchange has receipt evidence")
        return keys
    eid = op.control_key("inbox-ack-evidence", binding.delivery_id)
    ackkey, proofkey = op.control_key("inbox-ack-access", eid), op.control_key("inbox-receipt-evidence", eid)
    ack = view.row(ackkey, inbox._Acknowledged, material=True)
    proof = view.row(proofkey, d.VerifiedDeliveryReceipt, material=True)
    _access(view, ack.access, auth, "acknowledge")
    require(ex.receipt_evidence_id == eid and ack.exchange_id == ex.exchange_id
            and proof == d.VerifiedDeliveryReceipt(
                evidence_id=eid, verifier_version=view.config.delivery.verifier_version, binding=binding,
                send_outcome="emitted", confirmed_levels=("client_received",),
                dispatch_authorization_ref=ex.fetch_request.request_id, proof=inbox._ack_proof(ex, ack)),
            "settlement acknowledgment evidence differs")
    request = ControlRequest(
        request_id=op.control_key("inbox-acknowledge", binding.delivery_id),
        request_digest=digest([binding.delivery_id, ex.exchange_id, ex.acknowledgment_token,
                               binding.model_dump(mode="json"), ex.principal_id]),
        read_set=tuple(dict.fromkeys((*base, proofkey, *reads))),
    )
    require(ack.request == request, "settlement ACK request differs")
    receipt = view.receipt(request, (exkey, proofkey, ackkey),
                          exact={key: view.hash(key) for key in (exkey, proofkey, ackkey)},
                          result={"evidence_id": eid, "confirmation_level": "client_received"})
    require(fetch.guard_revision < receipt.guard_revision, "settlement receipt order differs")
    return keys | {ackkey, proofkey}


def validate_settlement_history(inventory, config):
    """Classify each persisted stage, without requiring or claiming fulfillment."""
    candidates = {key: row for key, row in inventory.rows.items() if row.payload["data"].get("kind") in KINDS}
    for row in inventory.rows.values():
        data = row.payload["data"]
        if data.get("kind") == "inbox_exchange":
            ex = m._parsed(data, inbox._Exchange)
            try:
                authority = json.loads(ex.authorization_evidence.proof)
            except (ValueError, TypeError):
                continue
            if isinstance(authority, dict) and "authorization_id" in authority:
                require(authority["authorization_id"] in candidates, "orphan retained settlement exchange")
    view = _Inventory(inventory, config)
    classified = _preflight(view)
    if not candidates:
        return classified
    plans = {key: row for key, row in candidates.items()
             if row.payload["data"]["kind"] == "retained_result_settlement_plan"}
    require(0 < len(plans) <= 32, "orphan or excessive settlement history")
    for key in plans:
        plan = view.row(key, SettlementPlan, material=True)
        binding = next((b for b in config.bindings if b.binding_id == plan.binding_id), None)
        require(binding is not None and key == settlement_key("plan", plan.settlement_id)
                and plan.configuration_sha256 == config.sha256, "settlement plan belongs to another installation")
        service, current = _description(view, binding, plan.settlement_id)
        require(current == plan, "settlement plan differs from retained terminal payload")
        reads = service._keys(plan)
        prepared = view.receipt(ControlRequest(request_id=key, request_digest=plan.sha256, read_set=reads),
                               (key,), exact={key: view.hash(key)},
                               result={"plan_sha256": plan.sha256, "delivered": False})
        classified.add(key)
        authkey = settlement_key("authorization", plan.settlement_id)
        if authkey in candidates:
            auth = _authorization(view, authkey, plan, reads)
            require(prepared.guard_revision < view.receipts[authkey].guard_revision, "authorization precedes plan")
            classified.add(authkey)
            classified.update(_exchange(view, plan, auth, reads))
            revkey = settlement_key("revocation", plan.settlement_id)
            if revkey in candidates:
                rev = view.row(revkey, _Revocation, material=True)
                require(rev.command.authorization_id == authkey and rev.principal_id == auth.principal_id
                        and rev.authorization_sha256 == digest(auth.model_dump(mode="json"))
                        and rev.revoked_at >= auth.approved_at, "settlement revocation differs")
                view.receipt(ControlRequest(request_id=revkey,
                    request_digest=digest(rev.command.model_dump(mode="json")), read_set=(authkey,)),
                    (revkey,), exact={revkey: view.hash(revkey)}, result={"authorization_id": authkey, "revoked": True})
                classified.add(revkey)
            obligation = view.row(op.control_key("delivery-obligation", binding.obligation_id), d._Obligation)
            if obligation.fulfillment == "fulfilled":
                from .behavior_runtime_rotation_history import _settled
                complete, _ = _settled(view, config, binding)
                classified.update(complete)
        else:
            require(op.control_key("inbox-exchange", plan.delivery.delivery_id) not in inventory.rows,
                    "settlement exchange has no immutable authorization")
    require(set(candidates) <= classified, "unmatched settlement history")
    return classified
