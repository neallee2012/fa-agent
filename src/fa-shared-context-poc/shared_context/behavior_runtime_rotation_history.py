"""Read-only retirement catalog for a bounded SDK-to-SDK lineage.

The checksum inventory is anchored to its original approved transition, not
reclassified as SDK work. Later SDK rows must be typed and receipt covered.
Only a fully acknowledged claimed-SDK cancellation may retain model uncertainty
or a verified pre-acquire result with no model request.
This module neither settles an effect nor treats a diagnostic receipt as a grant.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from typing import Literal, get_args, get_origin

from . import behavior_delivery as d, behavior_foundry as n, behavior_inbox as inbox
from . import behavior_migration as m, behavior_operations as op, behavior_workers as w
from . import memory_governance as memory
from . import source_materials as sources, source_reviews as reviews
from . import behavior_wire as wire
from .behavior_assistant import _Delegation, key as assistant_key
from .behavior_recovery_history import _Inventory, _checked_receipts, validate_recovery_history, validate_unclaimed_history
from .behavior_runtime import CommandPlan, RuntimePolicy, _PlanIndex, runtime_key
from .behavior_runtime_transition import (
    RuntimeTransitionCurrent, RuntimeTransitionHistory, _payload_hash, transition_key,
)
from .behavior_settlement import SettlementAuthorization, SettlementPlan, _Revocation, settlement_key
from .behavior_store import GUARD_KEY, ControlRequest, _Receipt, _Row
from .errors import Problem
from .records import Record
from .source_materials import AuthenticatedSourceContext, canonical, digest


def require(condition, message):
    if not condition:
        raise m._problem("ROTATION_HISTORY_INVALID", message)


def binding_scopes(config, binding, policy=None):
    if policy is None:
        policy = next(entry.policy for entry in config.policies if entry.binding_id == binding.binding_id)
    actors = ("human-" + binding.human_oid, *(agent.actor for agent in config.identity.agents if agent.oid == binding.agent_oid))
    return tuple(dict.fromkeys(AuthenticatedSourceContext(
        binding.tenant_id, binding.team_id, actor, binding.ingress_id, policy.allowed_purposes,
    ).scope(policy.source_visibility) for actor in actors))


def shared_keys(config):
    return {
        m.installation_record(config)[0], w.worker_key("pool", config.workers.workspace_id),
        w.worker_key("readiness", config.workers.workspace_id),
        op.control_key("inbox-readiness", digest({"contract": config.delivery.model_dump(mode="json"), "mode": "native"})),
        *(op.control_key("delivery-consumer-cursor", root.root_budget_id) for root in config.roots),
        *(op.control_key("memory-subject", scope.key, binding.subject)
          for binding in config.bindings for scope in binding_scopes(config, binding)),
        *(n.native_key("target", target.executor_id) for target in config.targets),
        *(op.control_key("budget", root.root_budget_id) for root in config.roots),
        *(op.control_key("delivery-index", root.root_budget_id) for root in config.roots),
        *(op.control_key("delivery-quota", "root", root.root_budget_id) for root in config.roots),
        op.control_key("delivery-quota", "team", config.identity.team_id),
        op.control_key("delivery-quota", "entry", config.delivery.entry_id),
    }


def receipt_catalog(inventory):
    """Prove complete current coverage and reject orphan/duplicate receipt writes."""
    latest, revisions = {}, set()
    try:
        # Shares the one parsed receipt copy every startup view of this inventory uses.
        receipts, _ = _checked_receipts(inventory, inventory.guard.payload["binding"],
                                        inventory.guard.payload["revision"])
    except Problem as error:
        if error.body["error"]["code"] != "MIGRATION_RECOVERY_HISTORY_INVALID":
            raise
        receipts = None
    require(receipts is not None, "receipt identity, revision, writes or hash differs")
    for key, value in receipts.items():
        require(value.guard_revision not in revisions, "receipt identity, revision, writes or hash differs")
        revisions.add(value.guard_revision)
        for row_id, fingerprint in value.writes:
            require(row_id in inventory.rows, "a retained receipt references a missing row")
            if row_id not in latest or latest[row_id][0] < value.guard_revision:
                latest[row_id] = (value.guard_revision, fingerprint)
    for key, record in inventory.rows.items():
        fingerprint = _payload_hash(record.payload)
        require(record.payload["row_id"] == key and record.payload["binding"] == inventory.guard.payload["binding"]
                and inventory.entity_sha256.get("behavior:v1:row:" + key) == fingerprint
                and key in latest and latest[key][1] == fingerprint,
                "current row is not the latest receipt-bound image: " + key)
    return receipts


def historical_shared_row(inventory, config, row_id, fingerprint, cutoff):
    """Recover exact preimages, never pair today's mutable data with old receipts."""
    current = m._row(inventory, row_id)
    if _payload_hash(current.payload) == fingerprint:
        return current
    receipts = {key: m._parsed(row.payload, _Receipt) for key, row in inventory.receipts.items()}
    writers = sorted((value for value in receipts.values() if row_id in dict(value.writes)),
                     key=lambda value: value.guard_revision)
    earlier = [value for value in writers if value.guard_revision <= cutoff]
    require(earlier and dict(earlier[-1].writes)[row_id] == fingerprint,
            "shared preimage has no exact cutoff receipt: " + row_id)
    anchor = earlier[-1]
    binding = inventory.guard.payload["binding"]

    def pointer(data):
        return _Row(binding=binding, row_id=row_id, data=data).model_dump(mode="json")

    archive_models = {}
    if row_id == w.worker_key("readiness", config.workers.workspace_id):
        archive_models["worker_readiness"] = w.WorkerReadiness
    if row_id == op.control_key("inbox-readiness", digest({
            "contract": config.delivery.model_dump(mode="json"), "mode": "native"})):
        archive_models["inbox_deployment_evidence"] = inbox.InboxDeploymentEvidence
    for key, row in inventory.rows.items():
        model = archive_models.get(row.payload["data"].get("kind"))
        if model is None:
            continue
        value = m._parsed(row.payload["data"], model)
        payload = pointer(value.model_dump(mode="json"))
        if _payload_hash(payload) != fingerprint:
            continue
        expected_key = (w.worker_key("readiness-evidence", value.evidence_id) if model is w.WorkerReadiness else
                        op.control_key("inbox-readiness-evidence", value.evidence_id))
        require(key == expected_key and row.payload["material"] == d._material(
                    value.model_dump(mode="json")).model_dump(mode="json")
                and dict(anchor.writes) == {row_id: fingerprint, key: _payload_hash(row.payload)},
                "readiness preimage lacks its immutable publisher evidence")
        return Record(payload, current.etag)

    for root in config.roots:
        if row_id != op.control_key("delivery-consumer-cursor", root.root_budget_id):
            continue
        for key, row in inventory.rows.items():
            if row.payload["data"].get("kind") != "delivery_consumer_page":
                continue
            page = m._parsed(row.payload["data"], d._ConsumerPage)
            if page.root_budget_id != root.root_budget_id or key != op.control_key(
                    "delivery-consumer-page", root.root_budget_id, str(page.sequence)):
                continue
            for active in (True, False):
                payload = pointer(page.model_copy(update={"active": active}).model_dump(mode="json"))
                if _payload_hash(payload) != fingerprint:
                    continue
                require(page.active and row.payload["material"] == d._material(
                    page.model_dump(mode="json")).model_dump(mode="json"), "cursor archive material differs")
                if active:
                    require(dict(anchor.writes).get(key) == _payload_hash(row.payload),
                            "active cursor lacks its original archived page")
                else:
                    request = ControlRequest(
                        request_id=op.control_key("delivery-consumer-page-done", root.root_budget_id, str(page.sequence)),
                        request_digest=digest(page.model_dump(mode="json")), read_set=(row_id,))
                    archived = [value for value in receipts.values() if value.guard_revision < anchor.guard_revision
                                and dict(value.writes).get(key) == _payload_hash(row.payload)
                                and dict(value.writes).get(row_id) == _payload_hash(pointer(page.model_dump(mode="json")))]
                    require(anchor.request == request and dict(anchor.writes) == {row_id: fingerprint}
                            and anchor.result == {"actual_environment": "NOT_RUN", "execution_enabled": False,
                                                  "page_completed": page.sequence} and len(archived) == 1,
                            "completed cursor lacks its original scan/done receipts")
                return Record(payload, current.etag)

    if row_id in {op.control_key("budget", root.root_budget_id) for root in config.roots}:
        root = next(root for root in config.roots if row_id == op.control_key("budget", root.root_budget_id))
        initial = pointer(op.BudgetRecord(root_budget_id=root.root_budget_id, active=True,
                                         remaining_attempts=root.attempts, remaining_units=root.units).model_dump(mode="json"))
        if _payload_hash(initial) == fingerprint:
            return Record(initial, current.etag)
        budget = m._parsed(current.payload["data"], op.BudgetRecord)
        for receipt in reversed([value for value in writers if value.guard_revision > cutoff]):
            payload = pointer(budget.model_dump(mode="json"))
            require(dict(receipt.writes)[row_id] == _payload_hash(payload),
                    "budget replay differs from the exact later write image")
            attempts, units = _historical_budget_debit(inventory, config, receipt, row_id)
            budget = budget.model_copy(update={
                "remaining_attempts": budget.remaining_attempts + attempts,
                "remaining_units": budget.remaining_units + units,
            })
        payload = pointer(budget.model_dump(mode="json"))
        require(_payload_hash(payload) == fingerprint, "budget debit replay cannot recover the catalogued preimage")
        return Record(payload, current.etag)
    raise m._problem("ROTATION_PREIMAGE_UNAVAILABLE",
                     "exact archived shared preimage is unavailable; historical state cannot be inferred", row_id=row_id)


def _historical_budget_debit(inventory, config, receipt, budget_key):
    """Only known producer debits may be reversed in a read-only historical view."""
    from . import behavior_assistant as assistant, behavior_assistant_wire as aw
    writes = dict(receipt.writes)
    for key in writes:
        row = m._row(inventory, key)
        data = row.payload["data"]
        if data.get("kind") == "worker_attempt":
            attempt = m._parsed(data, w._Attempt)
            execution = attempt.start.binding
            contract = digest({"contract": config.workers.model_dump(mode="json"), "mode": "native"})
            require(key == w.worker_key("attempt", execution.job_id, str(execution.generation))
                    and receipt.request == attempt.start.request
                    and receipt.request.request_id == w.worker_key("start", execution.job_id, str(execution.generation))
                    and receipt.request.request_digest == digest(["start", execution.model_dump(mode="json"), contract])
                    and budget_key == op.control_key("budget", execution.owner.root_budget_id)
                    and set(writes) == {budget_key, key, w.worker_key("job", execution.job_id),
                                        w.worker_key("pool", config.workers.workspace_id)}
                    and writes[key] == _payload_hash(row.payload)
                    and row.payload["material"] == d._material(attempt.start.model_dump(mode="json")).model_dump(mode="json"),
                    "budget start debit lacks its exact immutable attempt")
            return 1, config.workers.reservation_units
        if set(data) == {"request_sha256", "consumed"}:
            intent = m._parsed(data, assistant._ModelIntent)
            session_keys = [value for value in writes if value != key and value != budget_key]
            require(len(session_keys) == 1 and len(writes) == 3, "model debit write-set differs")
            session = m._parsed(m._row(inventory, session_keys[0]).payload["data"], assistant._Session)
            sid = session.assignment.session_id
            command = aw.Reservation(request_id=intent.request_sha256, request_sha256=intent.request_sha256)
            original = {**row.payload, "data": intent.model_copy(update={"consumed": False}).model_dump(mode="json")}
            require(key == assistant.key("model-intent", sid, intent.request_sha256)
                    and session_keys[0] == assistant.key("session", sid)
                    and receipt.request.request_id == assistant.key("model", sid, command.request_id)
                    and receipt.request.request_digest == digest(command.model_dump(mode="json"))
                    and budget_key == op.control_key("budget", session.execution.owner.root_budget_id)
                    and budget_key in receipt.request.read_set
                    and writes[key] == _payload_hash(original) and receipt.result == {"state": "accepted"},
                    "budget model debit lacks its original reservation")
            return 1, 1
        if {"description", "execution", "admission_plan_id", "stage"} <= data.keys():
            effect = m._parsed(data, assistant._ArtifactEffect)
            sid = assistant.key("session-id", effect.execution.start_request_id)
            answer = json.loads(effect.content)
            command = aw.Save(request_id="own-result", answer=aw.Answer(
                **{field: answer[field] for field in aw.Answer.model_fields}))
            original = {**row.payload, "data": effect.model_copy(update={
                "stage": "create_unknown", "artifact_sha256": None,
            }).model_dump(mode="json")}
            require(key == assistant.key("artifact", effect.execution.work_id)
                    and budget_key == op.control_key("budget", effect.execution.owner.root_budget_id)
                    and set(writes) == {budget_key, key} and writes[key] == _payload_hash(original)
                    and receipt.request.request_id == assistant.key("save", sid, command.request_id)
                    and receipt.request.request_digest == digest(command.model_dump(mode="json"))
                    and budget_key in receipt.request.read_set and key in receipt.request.read_set
                    and receipt.result == {"state": "create_unknown"},
                    "budget artifact debit lacks its original immutable effect")
            return 2, 2
    raise m._problem("ROTATION_PREIMAGE_UNAVAILABLE", "unrecognized budget writer cannot reconstruct a prior cutoff")


def predecessor_catalog(inventory, config):
    """Reconstruct HASHES, not invented payloads or receipts, at the old cutover."""
    from .behavior_runtime_rotation import RotationCurrent, RotationHistory, CatalogChunk, rotation_key
    current_key = rotation_key("current", config.table.scope.authority_domain)
    if current_key in inventory.rows:
        receipt_catalog(inventory)
        pointer = m._parsed(m._row(inventory, current_key).payload["data"], RotationCurrent)
        history = m._parsed(m._row(inventory, pointer.history_key).payload["data"], RotationHistory)
        require(pointer.configuration_sha256 == config.sha256
                and pointer.history_sha256 == digest(history.model_dump(mode="json")),
                "rotation predecessor differs from the installed configuration")
        anchored = {current_key, pointer.history_key, pointer.authorization_key}
        anchored.update(reference.row_id for reference in history.shared_preimage_chunks)
        mutable = shared_keys(config)
        for reference in history.catalog:
            chunk = m._parsed(m._row(inventory, reference.row_id).payload["data"], CatalogChunk)
            require(reference.sha256 == digest(chunk.model_dump(mode="json")), "retained predecessor catalog differs")
            anchored.add(reference.row_id)
            anchored.update(entry.key for entry in chunk.entries if entry.kind == "row" and entry.key not in mutable)
        return history, anchored
    pkey = transition_key("current", config.table.scope.authority_domain)
    pointer = m._parsed(m._row(inventory, pkey).payload["data"], RuntimeTransitionCurrent)
    history = m._parsed(m._row(inventory, pointer.history_key).payload["data"], RuntimeTransitionHistory)
    require(pointer.configuration_sha256 == config.sha256
            and digest(history.model_dump(mode="json")) == pointer.history_sha256,
            "the original checksum-to-SDK transition is not this configuration")
    receipts = receipt_catalog(inventory)
    cutoff, latest, hashes = history.plan.guard.revision, {}, {}
    for key, receipt in receipts.items():
        if receipt.guard_revision > cutoff:
            continue
        hashes["behavior:v1:receipt:" + key] = _payload_hash(inventory.receipts[key].payload)
        for row_id, fingerprint in receipt.writes:
            if row_id not in latest or latest[row_id][0] < receipt.guard_revision:
                latest[row_id] = receipt.guard_revision, fingerprint
    hashes.update({"behavior:v1:row:" + key: value[1] for key, value in latest.items()})
    guard = {key: value for key, value in history.before_guard.items() if key != "etag"}
    physical = m._MigratedGuard(binding=guard["binding"], revision=guard["revision"],
                                configuration_sha256=history.plan.old_configuration_sha256)
    hashes[GUARD_KEY] = _payload_hash(physical.model_dump(mode="json"))
    require(digest(hashes) == history.plan.inventory_sha256,
            "retained receipts do not reconstruct the exact approved checksum inventory")
    excluded = {GUARD_KEY, *("behavior:v1:row:" + image.row_id for image in history.images)}
    require(digest({key: value for key, value in hashes.items() if key not in excluded})
            == history.plan.untouched_sha256, "original unchanged-inventory anchor differs")
    mutable = shared_keys(config)
    anchored = {key for key, (_, fingerprint) in latest.items()
                if key not in mutable and key not in {image.row_id for image in history.images}}
    for key in anchored:
        require(_payload_hash(inventory.rows[key].payload) == latest[key][1],
                "a retired checksum row changed after its approved cutover")
    anchored.update((pkey, pointer.history_key, pointer.authorization_key))
    return history, anchored


class _RuntimePullProof(wire.WireModel):
    oid: wire.Identifier
    action: Literal["fetch", "acknowledge"]
    request_digest: wire.Digest


def _settled_receipts(view, config, binding, ob, ex, ack, proof, settlement_reads=()):
    did, eid = ob.final_delivery_id, ob.fulfillment_receipt_id
    ob_key = op.control_key("delivery-obligation", binding.obligation_id)
    exkey, ackkey = op.control_key("inbox-exchange", did), op.control_key("inbox-ack-access", eid)
    proofkey = op.control_key("inbox-receipt-evidence", eid)
    outkey, workkey = op.control_key("delivery-outbox", did), op.control_key("delivery-work-state", binding.obligation_id)
    finalkey = op.control_key("delivery-receipt", did, eid)
    inbox_reads = (
        op.control_key("owner", binding.owner.owner_record_id),
        op.control_key("delivery-conversation", binding.owner.conversation_id), ob_key, outkey,
        op.control_key("policy", binding.owner.policy_id), exkey,
    )
    fetch_command = inbox.InboxFetch(obligation_id=ob.obligation_id, delivery_id=did,
                                     access_evidence_id=ex.authorization_evidence.evidence_id)
    require(ex.fetch_request == ControlRequest(
        request_id=op.control_key("inbox-fetch", did, ex.authorization_evidence.evidence_id),
        request_digest=digest([fetch_command.model_dump(mode="json"), binding.owner.model_dump(mode="json"),
                               ex.authorization_evidence.model_dump(mode="json")]),
        read_set=tuple(dict.fromkeys((*inbox_reads, *settlement_reads)))),
        "fetch request does not bind the exact protected read set")
    require(ack.request.read_set == tuple(dict.fromkeys((*inbox_reads, proofkey, *settlement_reads))),
            "ACK protected read set differs")
    fetch = view.receipts.get(ex.fetch_request.request_id)
    acknowledgment = view.receipts.get(ack.request.request_id)
    require(fetch is not None and fetch.request == ex.fetch_request
            and fetch.result == {"exchange_id": ex.exchange_id}
            and exkey in dict(fetch.writes)
            and acknowledgment is not None and acknowledgment.request == ack.request
            and ack.request.request_id == op.control_key("inbox-acknowledge", did)
            and ack.request.request_digest == digest([did, ex.exchange_id, ex.acknowledgment_token,
                                                     ex.binding.model_dump(mode="json"), ex.principal_id]),
            "original fetch or ACK request/receipt differs")
    view.receipt(ack.request, (exkey, proofkey, ackkey), exact={
        proofkey: view.hash(proofkey), ackkey: view.hash(ackkey),
    }, result={"evidence_id": eid, "confirmation_level": "client_received"})
    command = d.ReceiptRequest(obligation_id=ob.obligation_id, delivery_id=did, evidence_id=eid)
    quota_keys = tuple(op.control_key("delivery-quota", kind, value) for kind, value in (
        ("team", config.delivery.team_id), ("entry", config.delivery.entry_id), ("root", binding.owner.root_budget_id)))
    request = ControlRequest(
        request_id=op.control_key("delivery-request", "receipt", ob.obligation_id, did, eid),
        request_digest=digest(["receipt", command.model_dump(mode="json"), binding.owner.model_dump(mode="json"),
                               ob.configuration_digest]),
        read_set=tuple(sorted((op.control_key("owner", binding.owner.owner_record_id),
                               op.control_key("delivery-conversation", binding.owner.conversation_id),
                               op.control_key("delivery-index", binding.owner.root_budget_id),
                               ob_key, outkey, workkey, *quota_keys))),
    )
    receipt = view.receipts.get(request.request_id)
    require(receipt is not None, "client ACK has no original fulfillment receipt")
    from .behavior_settlement_history import settled_root_quota_hash
    final_writes = {key: view.hash(key) for key in (
        *quota_keys, op.control_key("delivery-index", binding.owner.root_budget_id), ob_key, outkey, finalkey)}
    final_writes[op.control_key("delivery-quota", "root", binding.owner.root_budget_id)] = settled_root_quota_hash(
        view, receipt, binding.owner.root_budget_id)
    require(receipt.request == request and dict(receipt.writes) == final_writes
            and fetch.guard_revision < acknowledgment.guard_revision < receipt.guard_revision
            and receipt.result.get("fulfillment") == "fulfilled"
            and receipt.result.get("fulfillment_receipt_id") == eid,
            "client ACK did not settle the original obligation")
    return fetch, acknowledgment


def _settled_runtime_pull(view, config, binding, ob, ex, ack, proof, out):
    policy = view.row(runtime_key("policy", binding.binding_id), RuntimePolicy)
    configured = next(item.policy for item in config.policies if item.binding_id == binding.binding_id)
    owner = view.row(op.control_key("owner", binding.owner.owner_record_id), op.OwnerRecord)
    operation_policy = view.row(op.control_key("policy", binding.owner.policy_id), op.PolicyRecord)
    require(policy.model_dump(mode="json") == configured.model_dump(mode="json") and policy.active
            and policy.binding_digest == digest(["fa-behavior-api-v1", "native", binding.model_dump(mode="json")])
            and owner.active and all(getattr(owner, field) == value for field, value in binding.owner.model_dump().items())
            and binding.operation_id in owner.operation_ids
            and operation_policy.active and operation_policy.policy_id == binding.owner.policy_id
            and operation_policy.revision == policy.operation_policy_revision,
            "normal pull history is not bound to its original unchanged runtime policy/owner")
    require(ex.fetch_count == 1 and out.receipt_count == 1,
            "normal pull retirement requires one original fetch and confirmation")
    gate = (
        runtime_key("policy", binding.binding_id), op.control_key("owner", binding.owner.owner_record_id),
        op.control_key("policy", binding.owner.policy_id), op.control_key("budget", binding.owner.root_budget_id),
    )
    keys, access_receipts = set(), []
    for access, action in ((ex.authorization_evidence, "fetch"), (ack.access, "acknowledge")):
        try:
            origin = _RuntimePullProof.model_validate_json(access.proof)
        except (ValueError, TypeError) as error:
            raise m._problem("ROTATION_HISTORY_INVALID", "normal pull authorization proof is invalid") from error
        require(origin.oid == access.principal_id == config.identity.human_oid
                and origin.action == access.action == action and canonical(origin.model_dump(mode="json")) == access.proof
                and access.binding == ex.binding and access.authentication_version == config.authentication_version
                and access.policy_revision == policy.operation_policy_revision
                and access.observed_at < access.expires_at
                and access.expires_at == min(policy.valid_until, access.observed_at + timedelta(seconds=60)),
                "normal pull access does not retain its exact principal, scope or finite policy")
        key = runtime_key("inbox-access", access.evidence_id)
        require(view.row(key, inbox.VerifiedInboxAccess, material=True) == access,
                "normal pull authorization differs from its immutable runtime evidence")
        request = ControlRequest(
            request_id=runtime_key("evidence", "inbox-access", access.evidence_id),
            request_digest=digest(access.model_dump(mode="json")), read_set=gate,
        )
        access_receipts.append(view.receipt(
            request, (key,), exact={key: view.hash(key)}, result={"evidence_id": access.evidence_id},
        ))
        keys.add(key)
    fetch, acknowledged = _settled_receipts(view, config, binding, ob, ex, ack, proof)
    require(access_receipts[0].guard_revision < fetch.guard_revision
            and access_receipts[1].guard_revision < acknowledged.guard_revision,
            "normal pull access was not persisted before its exact operation")
    did, eid = ob.final_delivery_id, ob.fulfillment_receipt_id
    exkey = op.control_key("inbox-exchange", did)
    ob_key = op.control_key("delivery-obligation", binding.obligation_id)
    outkey = op.control_key("delivery-outbox", did)
    original_exchange = ex.model_copy(update={"acknowledged": False, "receipt_evidence_id": None})
    sent_unknown = out.model_copy(update={"send_outcome": "unknown", "confirmed_levels": (), "receipt_count": 0})
    require(set(dict(fetch.writes)) == {ob_key, exkey, outkey}
            and dict(fetch.writes)[exkey] == view.image_hash(exkey, original_exchange)
            and dict(fetch.writes)[outkey] == view.image_hash(outkey, sent_unknown)
            and dict(acknowledged.writes)[exkey] == view.hash(exkey),
            "normal pull fetch/ACK write images differ")
    # Fulfillment overwrites only these finite fields; match the exact prior
    # receipt hash rather than substituting today's fulfilled obligation.
    candidates = (
        ob.model_copy(update={
            "fulfillment": "pending", "delivery_state": "delivery_unknown", "fulfillment_credit": 1,
            "fulfillment_receipt_id": None, "confirmed_levels": (),
            "due_pending": due, "automation": automation,
        })
        for due in (False, True) for automation in ("active", "needs_attention", "stopped")
    )
    require(sum(view.image_hash(ob_key, value) == dict(fetch.writes)[ob_key] for value in candidates) == 1,
            "normal pull fetch obligation cannot be reconstructed from the fulfillment transition")
    keys.update((
        exkey, op.control_key("inbox-ack-access", eid), op.control_key("inbox-receipt-evidence", eid),
        op.control_key("delivery-receipt", did, eid), op.control_key("delivery-outbox", did),
    ))
    return keys, proof


def _finished_plans(view, config, binding):
    from .behavior_recovery import FinishedResultRecoveryPlan, RecoveryAuthorization, recovery_key

    plans = []
    for key, row in view.inventory.rows.items():
        candidate = row.payload["data"].get("plan")
        if (not isinstance(candidate, dict) or candidate.get("recovery_phase") != "after_durable_finish"
                or candidate.get("configuration_sha256") != config.sha256):
            continue
        authorization = view.row(key, RecoveryAuthorization, material=True)
        plan = authorization.plan
        require(isinstance(plan, FinishedResultRecoveryPlan)
                and key == recovery_key("authorization", plan.recovery_id)
                and plan.operator_oid == config.identity.human_oid
                and plan.binding.job_id == binding.job_id and plan.binding.work_id == binding.work.work_id
                and plan.binding.owner == binding.owner, "finished-result authorization binding differs")
        plans.append(plan)
    require(len(plans) <= 1, "duplicate finished-result closure")
    return tuple(plans)


def _settled(view, config, binding):
    """Verify either current-policy pull or independent retained settlement."""
    finished = bool(_finished_plans(view, config, binding))
    ob_key = op.control_key("delivery-obligation", binding.obligation_id)
    ob = view.row(ob_key, d._Obligation)
    require(ob.fulfillment == ob.delivery_state == "fulfilled" and not ob.due_pending
            and ob.automation == "stopped" and ob.confirmed_levels == ("client_received",)
            and ob.fulfillment_receipt_id is not None and ob.final_delivery_id is not None
            and ob.final_version == ob.last_version and ob.final_version > 0,
            "old delivery is not positively client_received/fulfilled")
    did, eid = ob.final_delivery_id, ob.fulfillment_receipt_id
    exkey = op.control_key("inbox-exchange", did)
    ackkey = op.control_key("inbox-ack-access", eid)
    proofkey = op.control_key("inbox-receipt-evidence", eid)
    ex = view.row(exkey, inbox._Exchange)
    ack = view.row(ackkey, inbox._Acknowledged, material=True)
    proof = view.row(proofkey, d.VerifiedDeliveryReceipt, material=True)
    outkey = op.control_key("delivery-outbox", did)
    out = view.row(outkey, d._Outbox)
    workkey = op.control_key("delivery-work-state", binding.obligation_id)
    work = view.row(workkey, d._Work)
    require(ex.binding == ack.access.binding == proof.binding == out.binding
            and ex.binding.obligation_id == ob.obligation_id and ex.binding.delivery_id == did
            and ex.binding.version == ob.final_version and ex.binding.route == binding.route
            and ex.binding.channel_version == config.delivery.channel.version
            and ex.principal_id == ack.access.principal_id == config.identity.human_oid
            and ex.acknowledged and ex.receipt_evidence_id == eid and ex.exchange_id == ack.exchange_id
            and proof.evidence_id == eid and proof.verifier_version == config.delivery.verifier_version
            and proof.confirmed_levels == ("client_received",) and proof.send_outcome == "emitted"
            and proof.dispatch_authorization_ref == ex.fetch_request.request_id
            and proof.proof == inbox._ack_proof(ex, ack) and out.send_outcome == "emitted"
            and "client_received" in out.confirmed_levels and out.message_kind == ("final" if finished else "failure"),
            "terminal receipt is not the exact authenticated final cancellation exchange")
    view.row(exkey, inbox._Exchange, material=ex.authorization_evidence.model_dump(mode="json"))
    finalkey = op.control_key("delivery-receipt", did, eid)
    final = view.row(finalkey, m._DeliveryReceipt, material=proof.model_dump(mode="json"))
    require(final.evidence == proof, "delivery confirmation differs from its inbox acknowledgment")
    payload = d._Payload(
        event_id=ob.event_id, root_request_id=ob.root_request_id, work_id=ob.work_id,
        message_kind="final" if finished else "failure", work_state="succeeded" if finished else "cancelled",
        result_evidence_id=work.result.evidence_id,
        content=work.result.content, artifacts=work.result.artifacts, known_effects=work.result.known_effects,
        unresolved_operations=work.result.unresolved_operations, responsibility_owner=ob.owner_record_id,
        notification_code="verified_result" if finished else "delivery_needs_attention",
    )
    material = d._material(payload.model_dump(mode="json"))
    require(view.inventory.rows[outkey].payload["material"] == material.model_dump(mode="json")
            and out.binding.payload_digest == material.sha256, "final bytes differ from the cancelled unknown-model result")

    try:
        authorization = json.loads(ex.authorization_evidence.proof)
    except (ValueError, TypeError, UnicodeError) as error:
        raise m._problem("ROTATION_HISTORY_INVALID", "pull authorization proof is invalid") from error
    require(isinstance(authorization, dict), "pull authorization proof must be an object")
    if set(authorization) == {"oid", "action", "request_digest"}:
        return _settled_runtime_pull(view, config, binding, ob, ex, ack, proof, out)
    require(set(authorization) == {"authorization_id", "authorization_sha256"},
            "pull authorization is not a supported runtime or retained grant")
    authorization_id = authorization["authorization_id"]
    auth = view.row(authorization_id, SettlementAuthorization, material=True)
    plan = auth.plan
    plan_key = settlement_key("plan", plan.settlement_id)
    require(view.row(plan_key, SettlementPlan, material=True) == plan
            and authorization_id == auth.authorization_id == settlement_key("authorization", plan.settlement_id)
            and plan.configuration_sha256 == config.sha256 and plan.control_binding == view.guard.binding
            and plan.binding_id == binding.binding_id and plan.job_id == binding.job_id
            and plan.work_id == binding.work.work_id and plan.context == binding.owner
            and plan.delivery == ex.binding and plan.terminal_state == ("succeeded" if finished else "cancelled")
            and plan.runtime_binding_digest == digest(["fa-behavior-api-v1", "native", binding.model_dump(mode="json")])
            and plan.worker_configuration_digest == digest({"contract": config.workers.model_dump(mode="json"), "mode": "native"})
            and plan.delivery_configuration_digest == ob.configuration_digest
            and auth.decision.plan_sha256 == plan.sha256 and auth.principal_id == config.identity.human_oid
            and auth.protection.configuration_sha256 == config.sha256
            and auth.protection.control_binding == view.guard.binding and auth.protection.plan_sha256 == plan.sha256
            and auth.protection.retained_materials == plan.retained_materials
            and auth.protection.evidence_mode == "native"
            and auth.protection.authentication_version == config.authentication_version
            and auth.protection.observed_at <= auth.approved_at < auth.expires_at <= auth.protection.expires_at,
            "independent immutable settlement authorization/configuration differs")
    require(auth.expires_at <= auth.approved_at + timedelta(minutes=15)
            and auth.expires_at <= auth.protection.observed_at + timedelta(minutes=5),
            "historical settlement authorization exceeds its independent bound")
    policy = view.row(runtime_key("policy", binding.binding_id), RuntimePolicy)
    job = view.row(w.worker_key("job", binding.job_id), w.JobRecord)
    launch = view.row(plan.launch_row_id, n._NativeLaunch)
    completion = view.row(plan.completion_row_id, w.ExecutorEvidence, material=True)
    require(plan.runtime_policy_sha256 == digest(policy.model_dump(mode="json"))
            and plan.operation_policy_revision == policy.operation_policy_revision
            and plan.job_sha256 == digest(job.model_dump(mode="json"))
            and plan.launch_row_id == n.native_key("launch", launch.launch_id)
            and plan.completion_row_id == w.worker_key("completion", binding.job_id, str(job.generation))
            and plan.completion_sha256 == digest(completion.model_dump(mode="json"))
            and completion.evidence_id == job.last_evidence_id and completion.result == work.result
            and plan.result_evidence_id == work.result.evidence_id
            and plan.result_sha256 == d._material(work.result.model_dump(mode="json")).sha256,
            "settlement does not retain the original policy/completion/result")
    # Later terminal revalidation may increase only the sealed launch sequence.
    sealed = launch.model_copy(update={"evidence_sequence": completion.sequence})
    require(plan.launch_sha256 == digest(sealed.model_dump(mode="json")), "settlement sealed launch hash differs")
    for item in plan.retained_materials:
        record = view.inventory.rows.get(item.row_id)
        require(record is not None and record.payload["material"] is not None
                and record.payload["material"]["sha256"] == item.sha256, "settled retained material disappeared")
    require({item.row_id for item in plan.retained_materials}
            == {workkey, w.worker_key("work-result", work.result.evidence_id), outkey},
            "settlement retained-material set differs")
    auth_proof = d._material({"authorization_id": auth.authorization_id,
                             "authorization_sha256": digest(auth.model_dump(mode="json"))}).data
    for access, action in ((ex.authorization_evidence, "fetch"), (ack.access, "acknowledge")):
        require(access.proof == auth_proof and access.action == action
                and access.principal_id == auth.principal_id and access.binding == plan.delivery
                and access.authentication_version == config.authentication_version
                and access.policy_revision == plan.operation_policy_revision
                and access.observed_at == auth.approved_at and access.expires_at == auth.expires_at,
                "retained access is not the exact independent settlement grant")
    settlement_reads = (
        runtime_key("policy", binding.binding_id), op.control_key("owner", binding.owner.owner_record_id),
        op.control_key("policy", binding.owner.policy_id), op.control_key("budget", binding.owner.root_budget_id),
        op.control_key("delivery-conversation", binding.owner.conversation_id), ob_key, workkey,
        w.worker_key("job", binding.job_id), w.worker_key("work-result", work.result.evidence_id), outkey,
        plan.launch_row_id, plan.completion_row_id,
        *(settlement_key(kind, plan.settlement_id) for kind in ("plan", "authorization", "revocation")),
    )
    fetch, _ = _settled_receipts(view, config, binding, ob, ex, ack, proof, settlement_reads)
    keys = {plan_key, authorization_id, exkey, ackkey, proofkey, finalkey, outkey}
    for key, record in view.inventory.rows.items():
        if record.payload["data"].get("kind") == "retained_result_settlement_revocation":
            rev = view.row(key, _Revocation, material=True)
            if rev.command.authorization_id != auth.authorization_id:
                continue
            require(key == settlement_key("revocation", plan.settlement_id)
                    and rev.command.authorization_id == auth.authorization_id
                    and rev.authorization_sha256 == digest(auth.model_dump(mode="json"))
                    and rev.principal_id == auth.principal_id and rev.revoked_at >= auth.approved_at,
                    "unrelated or malformed settlement revocation")
            view.receipt(ControlRequest(request_id=key, request_digest=digest(rev.command.model_dump(mode="json")),
                                        read_set=(authorization_id,)), (key,), exact={key: view.hash(key)},
                         result={"authorization_id": authorization_id, "revoked": True})
            keys.add(key)
    for key, request_digest, result in (
        (plan_key, plan.sha256, {"plan_sha256": plan.sha256, "delivered": False}),
        (authorization_id, digest(auth.decision.model_dump(mode="json")),
         {"authorization_id": authorization_id, "plan_sha256": plan.sha256, "delivered": False}),
    ):
        receipt = view.receipts.get(key)
        require(receipt is not None and receipt.request.request_digest == request_digest
                and receipt.request.read_set == settlement_reads
                and dict(receipt.writes) == {key: view.hash(key)} and receipt.result == result
                and receipt.guard_revision < fetch.guard_revision, "settlement setup lacks its exact receipt")
    return keys, proof


class _SourceHistory:
    """Pure retained inline history, never a current source/claim permission.

    Reconstruct the producers' requests and historical head/index write images.
    Expiry bytes participate in their original hashes; no current clock, Blob
    reader, active review verifier, renewal or source read API is used here.
    """
    def __init__(self, view, config, anchored):
        self.view, self.inventory, self.config = view, view.inventory, config
        self.binding, self.policy = config.bindings[0], config.policies[0].policy
        self.scopes = binding_scopes(config, self.binding)
        self.models, self.events, self.originals, self.decisions, self.claims = {}, {}, {}, {}, {}
        signatures = (
            (sources.SourceEvent, {"reference", "source_actor", "ingress_id", "schema_id"}),
            (sources.SourceHead, {"reference", "source_actor", "access_state", "access_revision"}),
            (memory.ReviewedClaim, {"candidate", "reference", "review", "request", "sequence"}),
            (reviews._DecisionRecord, {"decision_key", "candidate_sha256", "material_sha256", "scope"}),
        )
        for key, row in self.inventory.rows.items():
            if key in anchored:
                continue
            for model, fields in signatures:
                if fields <= row.payload["data"].keys():
                    self.models[key] = view.row(key, model)
                    break
        for key, value in self.models.items():
            if isinstance(value, sources.SourceEvent):
                self._event(value.reference)
        for value in tuple(self.events.values()):
            self._source_receipt(value)
        self._source_indexes()
        if self.events:
            pinned = config.assistant.profiles[0].source
            require(any(event.reference.source_id == pinned.source_id and event.reference.version == pinned.version
                        and event.reference.sha256 == pinned.sha256 and event.reference.byte_length == pinned.byte_length
                        for event in self.events.values()), "retained source does not contain the exact configured SDK input")
        for key, value in self.models.items():
            if isinstance(value, reviews._DecisionRecord):
                self._decision(key, value)
        self._claim_history()
        for record, material, _ in self.decisions.values():
            if record.decision == "revoke":
                candidate = material.command.candidate
                reference = memory.ClaimReference(scope=record.scope, subject=candidate.subject,
                    claim_id=candidate.claim_id, version=candidate.version, sha256=record.candidate_sha256)
                claim = self.claims.get(reference.key)
                require(claim is not None and claim.candidate == candidate
                        and self.view.receipts[claim.request.request_id].guard_revision
                        < self.view.receipts[record.request.request_id].guard_revision,
                        "human revocation lacks its previously committed exact claim")
        index_key = runtime_key("plan-index", self.binding.binding_id)
        index = self.view.row(index_key, _PlanIndex)
        original_keys = set()
        for key, row in self.inventory.rows.items():
            data = row.payload["data"]
            if data.get("binding_id") == self.binding.binding_id and {
                "input_digest", "principal_oid", "context", "data", "request", "request_id", "kind",
            } <= data.keys():
                plan = m._parsed(data, CommandPlan)
                if key == runtime_key("plan", plan.binding_id, plan.kind, plan.request_id):
                    original_keys.add(key)
        require(len(index.keys) == len(set(index.keys)) and set(index.keys) == original_keys,
                "command index does not exactly cover the retained original plans")

    def _plain(self, key):
        require(self.inventory.rows[key].payload["material"] is None, "unexpected material on a typed history index/claim")

    def _event(self, reference):
        if reference.key in self.events:
            require(self.events[reference.key].reference == reference, "conflicting historical source reference")
            return self.events[reference.key]
        value = self.view.row(reference.key, sources.SourceEvent)
        require(value.reference == reference and reference.scope in self.scopes
                and reference.source_id in self.binding.source_ids and value.codec == "identity"
                and value.purpose == self.policy.purpose and value.ingress_id == self.binding.ingress_id
                and value.blob is None and value.protection is None,
                "retired source must be inline and in the exact original registered source scope")
        actors = {"human-" + self.config.identity.human_oid,
                  *(actor.actor for actor in self.config.identity.agents if actor.oid == self.binding.agent_oid)}
        require(value.source_actor in actors
                and (reference.scope.visibility != "private" or reference.scope.owner_actor == value.source_actor),
                "retired source actor/owner differs")
        material = m._parsed(self.inventory.rows[reference.key].payload["material"], sources.InlineMaterial)
        require(material.sha256 == reference.sha256 and len(material.data) == reference.byte_length,
                "retained source bytes/hash/length differ")
        self.events[reference.key] = value
        self.originals[reference.key] = material.data
        return value

    def _graph(self, references):
        found, pending = {}, list(references)
        while pending:
            reference = pending.pop()
            if reference.key in found:
                require(found[reference.key].event.reference == reference, "conflicting retained necessary graph")
                continue
            require(len(found) < sources.MAX_GRAPH_NODES, "retained necessary graph exceeds its bound")
            event = self._event(reference)
            head = self.view.row(reference.head_key, sources.SourceHead)
            require(head.reference.scope == reference.scope and head.reference.source_id == reference.source_id
                    and head.source_actor == event.source_actor and head.access_state == "active"
                    and head.access_revision == 1, "unsupported withdrawn or rebound source head")
            receipt = m._receipt(self.inventory, event.request)
            require(receipt is not None, "necessary source has no original admission receipt")
            found[reference.key] = sources.ExactSource(event, self.originals[reference.key], receipt, head.access_revision)
            pending.extend(event.necessary)
        return found

    def _source_plan(self, value):
        graph = self._graph(value.necessary)
        require(value.reference.key not in graph, "cyclic retained source graph")
        source = sources.SourceInput(
            source_id=value.reference.source_id, version=value.reference.version, visibility=value.reference.scope.visibility,
            purpose=value.purpose, schema_id=value.schema_id, media_type=value.media_type,
            necessary=value.necessary, expires_at=value.expires_at,
        )
        dependencies = tuple(item.event.reference for _, item in sorted(graph.items()))
        metadata = {
            "source": source.model_dump(mode="json"), "reference": value.reference.model_dump(mode="json"),
            "source_actor": value.source_actor, "ingress_id": value.ingress_id, "protection": None,
            "composition": None, "dependency_versions": [item.model_dump(mode="json") for item in dependencies],
        }
        request = ControlRequest(
            request_id=op.control_key("source-admit", value.reference.key), request_digest=digest(metadata),
            read_set=tuple(dict.fromkeys((value.reference.key, value.reference.head_key,
                op.control_key("source-cursor", value.reference.scope.key),
                *(key for item in dependencies for key in (item.key, item.head_key))))),
        )
        return sources.SourcePlan(source=source, reference=value.reference, source_actor=value.source_actor,
            ingress_id=value.ingress_id, original=self.originals[value.reference.key],
            dependency_versions=dependencies, request=request)

    def _source_receipt(self, value):
        plan = self._source_plan(value)
        require(value.request == plan.request, "source history differs from the exact original admission request")
        reference = value.reference
        head_key, cursor_key = reference.head_key, op.control_key("source-cursor", reference.scope.key)
        head = sources.SourceHead(reference=reference, source_actor=value.source_actor, sequence=value.sequence)
        cursor = sources._Cursor(sequence=value.sequence)
        for key in (head_key, cursor_key):
            self._plain(key)
        receipt = self.view.receipt(plan.request, (reference.key, head_key, cursor_key), exact={
            reference.key: self.view.hash(reference.key), head_key: self.view.image_hash(head_key, head),
            cursor_key: self.view.image_hash(cursor_key, cursor),
        }, result={"reference": reference.model_dump(mode="json"), "sequence": value.sequence})
        require(all(self.view.receipts[self.events[ref.key].request.request_id].guard_revision < receipt.guard_revision
                    for ref in plan.dependency_versions), "necessary source was not committed before its dependent")

    def _source_indexes(self):
        for key, value in self.models.items():
            if isinstance(value, sources.SourceHead):
                self._event(value.reference)
                require(key == value.reference.head_key, "source head key is not bound to its reference")
        grouped = {}
        for value in self.events.values():
            grouped.setdefault(value.reference.scope.key, []).append(value)
        for scope_key, values in grouped.items():
            # Include receipt-anchored predecessor versions sharing this cursor.
            for key, row in self.inventory.rows.items():
                if key not in self.events and {"reference", "source_actor", "ingress_id", "schema_id"} <= row.payload["data"].keys():
                    event = self.view.row(key, sources.SourceEvent)
                    if event.reference.scope.key == scope_key:
                        values.append(event)
            ordered = sorted(values, key=lambda event: event.sequence)
            require([event.sequence for event in ordered] == list(range(1, len(ordered) + 1)),
                    "source cursor omits, duplicates or resets a retained source sequence")
            revisions = [self.view.receipts[event.request.request_id].guard_revision for event in ordered]
            require(revisions == sorted(set(revisions)), "source sequence/receipt causal order differs")
            cursor_key = op.control_key("source-cursor", scope_key)
            cursor = self.view.row(cursor_key, sources._Cursor)
            require(cursor.sequence == len(ordered), "source cursor is not the exact retained sequence")
            self._plain(cursor_key)
            self.models[cursor_key] = cursor
            by_source = {}
            for event in ordered:
                by_source.setdefault(event.reference.source_id, []).append(event)
            for versions in by_source.values():
                require([event.reference.version for event in versions] == list(range(1, len(versions) + 1))
                        and len({event.source_actor for event in versions}) == 1,
                        "source version/actor history is not contiguous")
                latest = versions[-1]
                head = self.view.row(latest.reference.head_key, sources.SourceHead)
                require(head == sources.SourceHead(reference=latest.reference, source_actor=latest.source_actor,
                                                   sequence=latest.sequence), "current source head differs from retained versions")
                self.models[latest.reference.head_key] = head

    def _candidate(self, candidate, scope):
        require(scope in self.scopes and candidate.visibility == scope.visibility
                and candidate.subject == self.binding.subject and candidate.claim_id in self.binding.claim_ids
                and candidate.purpose == self.policy.purpose, "claim/review escaped its original registered scope")
        graph = self._graph(candidate.supporting)
        require(all(exact.event.reference.scope == scope for exact in graph.values()),
                "claim cannot broaden a retained source scope")
        return graph

    def _decision(self, key, value):
        inline = m._parsed(self.inventory.rows[key].payload["material"], sources.InlineMaterial)
        try:
            material = reviews._DecisionMaterial.model_validate_json(inline.data)
            reviewed_at = sources.utc(material.reviewed_at)
        except (ValueError, TypeError):
            raise m._problem("ROTATION_HISTORY_INVALID", "invalid retained human review material or UTC time") from None
        require(len(inline.data) <= reviews.HumanReviewPolicy(
            policy_version=self.config.review_policy_version).max_material_bytes, "review material exceeds its producer bound")
        command, scope = material.command, material.scope
        graph = self._candidate(command.candidate, scope)
        fingerprint = memory.claim_digest(command.candidate, scope)
        approval_key = op.control_key("source-human-review", scope.key, fingerprint, self.config.review_policy_version)
        revoke_key = op.control_key("source-human-revocation", scope.key, fingerprint)
        reference = memory.ClaimReference(scope=scope, subject=command.candidate.subject,
            claim_id=command.candidate.claim_id, version=command.candidate.version, sha256=fingerprint)
        reads = (approval_key, revoke_key, *sources.SourceMaterials.source_read_keys(graph))
        if command.decision == "revoke":
            reads += (reference.key, op.control_key("memory-subject", scope.key, reference.subject))
        request = ControlRequest(
            request_id=op.control_key("source-human-review-request", scope.key, command.review_id),
            request_digest=digest(material.model_dump(mode="json")), read_set=tuple(dict.fromkeys(reads)),
        )
        require(key == (revoke_key if command.decision == "revoke" else approval_key)
                and material.human_oid == self.config.identity.human_oid
                and material.human_source_actor == "human-" + material.human_oid
                and command.candidate_sha256 == fingerprint and command.policy_version == self.config.review_policy_version
                and command.evidence_sha256 == sources.SourceMaterials.evidence_digest(graph)
                and inline.data == canonical(material.model_dump(mode="json")),
                "human decision scope, source graph, identity or original material differs")
        expected = reviews._DecisionRecord(decision_key=key, scope=scope, candidate_sha256=fingerprint,
            evidence_sha256=command.evidence_sha256, policy_version=command.policy_version, decision=command.decision,
            human_oid=material.human_oid, material_sha256=inline.sha256, request=request)
        require(value == expected, "review record does not bind its immutable material and exact request")
        receipt = self.view.receipt(request, (key,), exact={key: self.view.hash(key)},
            result={"decision_key": key, "decision": command.decision, "candidate_sha256": fingerprint})
        require(all(exact.receipt.payload["guard_revision"] < receipt.guard_revision
                    and (exact.event.expires_at is None or reviewed_at < exact.event.expires_at)
                    for exact in graph.values()), "review precedes its source or used an already expired source")
        self._current_at(graph, receipt.guard_revision)
        self.decisions[key] = (value, material, graph)

    def _current_at(self, graph, revision):
        for exact in graph.values():
            require(not any(other.reference.head_key == exact.event.reference.head_key
                            and other.reference.version > exact.event.reference.version
                            and self.view.receipts[other.request.request_id].guard_revision < revision
                            for other in self.events.values()),
                    "historical review/claim did not use the then-current exact source")

    def _claim_history(self):
        groups = {}
        for key, value in self.models.items():
            if not isinstance(value, memory.ReviewedClaim):
                continue
            reference, candidate = value.reference, value.candidate
            graph = self._candidate(candidate, reference.scope)
            require(key == reference.key and reference == memory.ClaimReference(
                scope=reference.scope, subject=candidate.subject, claim_id=candidate.claim_id,
                version=candidate.version, sha256=memory.claim_digest(candidate, reference.scope)),
                "reviewed claim key/content/expiry digest differs")
            decision = self.decisions.get(value.review.decision_id)
            require(decision is not None, "claim has no exact retained human decision")
            record, material, _ = decision
            expected = memory.ReviewAttestation(
                decision_id=record.decision_key, reviewer_id=record.human_oid, reviewer_kind="human",
                policy_version=record.policy_version, candidate_sha256=record.candidate_sha256,
                evidence_sha256=record.evidence_sha256, reviewed_checks=material.command.reviewed_checks,
                outcome="approved", authority_record_sha256=digest(self.inventory.rows[record.decision_key].payload),
            )
            require(record.decision == "approve" and material.command.candidate == candidate
                    and value.review == expected
                    and (candidate.expires_at is None or material.reviewed_at < candidate.expires_at),
                    "claim is not the original explicitly approved candidate")
            state_key = op.control_key("memory-subject", reference.scope.key, reference.subject)
            revoke_key = op.control_key("source-human-revocation", reference.scope.key, reference.sha256)
            request = ControlRequest(request_id=op.control_key("memory-review", reference.key),
                request_digest=digest([reference.model_dump(mode="json"), expected.model_dump(mode="json")]),
                read_set=tuple(dict.fromkeys((state_key, reference.key, *sources.SourceMaterials.source_read_keys(graph),
                                             record.decision_key, revoke_key))))
            require(value.request == request, "claim's original request/read set differs")
            self._plain(key)
            self.claims[key] = memory.ClaimPlan(candidate=candidate, reference=reference, review=expected, request=request)
            groups.setdefault(state_key, []).append(value)
        for state_key, claims in groups.items():
            claims.sort(key=lambda claim: claim.sequence)
            self._plain(state_key)
            heads, previous_revision = (), 0
            for sequence, claim in enumerate(claims, 1):
                require(claim.sequence == sequence, "claim index omits or resets a retained sequence")
                prior = next((head for head in heads if head.reference.claim_id == claim.reference.claim_id), None)
                require((prior is None and claim.reference.version == 1 and claim.candidate.supersedes is None)
                        or (prior is not None and claim.reference.version == prior.reference.version + 1
                            and claim.candidate.supersedes == prior.reference.sha256),
                        "claim version does not preserve its exact predecessor")
                heads = tuple(head for head in heads if head.reference.claim_id != claim.reference.claim_id) + (
                    memory.ClaimHead(reference=claim.reference, status="active", sequence=sequence),)
                state = memory.SubjectState(scope=claim.reference.scope, subject=claim.reference.subject,
                                            sequence=sequence, heads=heads)
                receipt = self.view.receipt(claim.request, (claim.reference.key, state_key), exact={
                    claim.reference.key: self.view.hash(claim.reference.key), state_key: self.view.image_hash(state_key, state),
                }, result={"claim": claim.reference.model_dump(mode="json"), "sequence": sequence})
                review = self.decisions[claim.review.decision_id][0]
                self._current_at(self.decisions[claim.review.decision_id][2], receipt.guard_revision)
                require(receipt.guard_revision > max(previous_revision, self.view.receipts[review.request.request_id].guard_revision),
                        "claim receipt precedes its review or predecessor")
                previous_revision = receipt.guard_revision
            require(self.view.row(state_key, memory.SubjectState) == state, "subject index differs from the original claim history")
            self.models[state_key] = state

    def command(self, key, plan):
        """Bind source/review/claim command metadata to the verified producers."""
        family = {"source_plan", "human_review", "claim_plan"} & plan.data.keys()
        if not family:
            require(plan.kind not in {
                *("source-" + source_id for source_id in self.binding.source_ids),
                *("human-review-" + claim_id for claim_id in self.binding.claim_ids),
                *("claim-" + claim_id for claim_id in self.binding.claim_ids),
            }, "typed source/review/claim command data is missing")
            return
        require(len(family) == 1 and len(plan.data) == 1, "ambiguous source/claim command data")
        self.view.row(key, CommandPlan, material=True)
        lane = next(iter(family))
        if lane == "source_plan":
            saved = m._parsed(plan.data[lane], sources.SourcePlan)
            event = self._event(saved.reference)
            require(saved == self._source_plan(event) and plan.kind == "source-" + saved.reference.source_id,
                    "source command does not retain its exact admission plan")
            principal = self.config.identity.human_oid if saved.source_actor == "human-" + self.config.identity.human_oid else self.binding.agent_oid
            expected = saved.request
            command = wire.SourcePrepare(
                request_id=plan.request_id, version=saved.source.version, original=saved.original,
                necessary=tuple(self._wire_source(ref) for ref in saved.source.necessary),
                source_schema=saved.source.schema_id, expires_at=saved.source.expires_at,
                retention_consent_sha256=saved.reference.sha256 if saved.source.schema_id == "fa-human-text-v1" else None,
            )
        elif lane == "human_review":
            saved = m._parsed(plan.data[lane], reviews.HumanReviewPlan)
            matches = [(value, material) for value, material, _ in self.decisions.values() if value.request == saved.request]
            require(len(matches) == 1 and matches[0][1] == saved.material
                    and plan.kind == "human-review-" + saved.material.command.candidate.claim_id,
                    "review command does not retain its exact human decision")
            principal, expected = self.config.identity.human_oid, saved.request
            command = self._review_command(plan.request_id, saved.material)
        else:
            saved = m._parsed(plan.data[lane], memory.ClaimPlan)
            require(self.claims.get(saved.reference.key) == saved and plan.kind == "claim-" + saved.reference.claim_id,
                    "claim command does not retain its exact reviewed version")
            principal, expected = self.config.identity.human_oid, saved.request
            command = self._review_command(plan.request_id, self.decisions[saved.review.decision_id][1])
        require(plan.principal_oid == principal and plan.request == expected
                and plan.input_digest == digest(command.model_dump(mode="json")),
                "command's exact input, principal or receiver request differs")
        original_key = runtime_key("plan", plan.binding_id, plan.kind, plan.request_id)
        original = self.view.row(original_key, CommandPlan, material=True)
        require(original == plan, "consumed command differs from its original")
        gate = (runtime_key("policy", self.binding.binding_id), op.control_key("owner", self.binding.owner.owner_record_id),
                op.control_key("policy", self.binding.owner.policy_id), op.control_key("budget", self.binding.owner.root_budget_id))
        reserve = ControlRequest(request_id=runtime_key("plan-reserve", original_key),
            request_digest=digest([plan.input_digest, principal, plan.binding_digest]),
            read_set=(*gate, runtime_key("plan-index", self.binding.binding_id)))
        self.view.receipt(reserve, (original_key, runtime_key("plan-index", self.binding.binding_id)),
                          exact={original_key: self.view.hash(original_key)}, result={"plan_key": original_key})
        if key != original_key:
            request = ControlRequest(request_id=key, request_digest=digest(plan.model_dump(mode="json")), read_set=gate)
            self.view.receipt(request, (key,), exact={key: self.view.hash(key)},
                              result={"responsibility": "reconcile_original", "dispatch_permission": False})

    @staticmethod
    def _wire_source(reference):
        return wire.ExactSourceRef(source_id=reference.source_id, version=reference.version,
                                   sha256=reference.sha256, byte_length=reference.byte_length)

    def _review_command(self, request_id, material):
        command, candidate = material.command, material.command.candidate
        return wire.ClaimReview(
            request_id=request_id, version=candidate.version, text=candidate.text, kind=candidate.kind,
            supporting=tuple(self._wire_source(ref) for ref in candidate.supporting),
            effective_from=candidate.effective_from, expires_at=candidate.expires_at, supersedes=candidate.supersedes,
            decision_id=command.review_id, candidate_sha256=command.candidate_sha256,
            evidence_sha256=command.evidence_sha256, policy_version=command.policy_version,
            reviewed_checks=command.reviewed_checks,
        )


def validate_retirement(inventory, config):
    """Fail closed on additions; no permissive unknown-operation or pending lane."""
    require(len(config.bindings) == 1, "this bounded rotation retires one SDK binding")
    history, anchored = predecessor_catalog(inventory, config)
    view = _Inventory(inventory, config)
    binding = config.bindings[0]
    from .behavior_recovery import FinishedResultRecoveryPlan, RecoveryAuthorization
    finished_plans = _finished_plans(view, config, binding)
    finished = bool(finished_plans)
    terminal = "succeeded" if finished else "cancelled"
    settled, proof = _settled(view, config, binding)
    # The original recovery recognizer intentionally predates settlement's
    # `approved_at` schema. Exclude ONLY independently verified settlement rows
    # from that recognizer; keep the real work/model/obligation and all receipts.
    current_history = replace(inventory, rows={
        key: value for key, value in inventory.rows.items() if key not in settled and key not in anchored
    })
    recovery = validate_recovery_history(current_history, config)
    unclaimed = validate_unclaimed_history(current_history, config, complete=True)
    require(bool(recovery) != bool(unclaimed), "one exact claimed recovery or unclaimed fence history is required")
    classified = set(recovery) | set(unclaimed) | anchored | settled
    classified.update(m._operator_history(replace(inventory, rows={
        key: row for key, row in inventory.rows.items() if key not in anchored
    }), config))
    source_history = _SourceHistory(view, config, anchored)
    classified.update(source_history.models)
    pool = view.row(w.worker_key("pool", config.workers.workspace_id), w._Pool)
    require(not pool.pending and not pool.completions and len(pool.slots) == 1
            and pool.slots[0].number == 0 and pool.slots[0].binding is None
            and pool.slots[0].lease_until is None, "global C1 has not drained")
    worker_digest = digest({"contract": config.workers.model_dump(mode="json"), "mode": "native"})
    delivery_digest = digest({"contract": config.delivery.model_dump(mode="json"), "mode": "native"})
    fixed = {
        op.control_key("owner", binding.owner.owner_record_id): op.OwnerRecord,
        op.control_key("policy", binding.owner.policy_id): op.PolicyRecord,
        runtime_key("policy", binding.binding_id): RuntimePolicy,
        runtime_key("plan-index", binding.binding_id): _PlanIndex,
        w.worker_key("pool", config.workers.workspace_id): w._Pool,
        w.worker_key("job", binding.job_id): w.JobRecord,
        op.control_key("delivery-obligation", binding.obligation_id): d._Obligation,
        op.control_key("delivery-work-state", binding.obligation_id): d._Work,
        op.control_key("delivery-conversation", binding.owner.conversation_id): d._Conversation,
        op.control_key("delivery-event", binding.event_id): m._DeliveryEvent,
        op.control_key("delivery-work", binding.work.work_id): m._WorkIdentity,
        assistant_key("delegation", binding.event_id): _Delegation,
    }
    for root in config.roots:
        fixed.update({
            op.control_key("budget", root.root_budget_id): op.BudgetRecord,
            op.control_key("delivery-index", root.root_budget_id): d.PendingIndexRecord,
            op.control_key("delivery-quota", "root", root.root_budget_id): d.QuotaRecord,
        })
    fixed.update({op.control_key("delivery-quota", kind, value): d.QuotaRecord for kind, value in (
        ("team", config.identity.team_id), ("entry", config.delivery.entry_id))})
    kinds = m._kind_models()
    no_kind = (w.ExecutorEvidence, d.VerifiedWorkResult, d.VerifiedIngress, inbox.VerifiedInboxAccess, memory.SubjectState,
               m._NativePostIntent, m._WorkerAdmission, m.HostedReportBody)
    for key, record in inventory.rows.items():
        if key in classified or key == m.installation_record(config)[0]:
            continue
        data = record.payload["data"]
        if key in fixed:
            value = view.row(key, fixed[key])
        elif data.get("kind") in kinds:
            value = view.row(key, kinds[data["kind"]])
        elif {"binding_id", "binding_digest", "input_digest", "request_id", "context", "request"} <= data.keys():
            value = view.row(key, CommandPlan)
        else:
            candidates = []
            for model in no_kind:
                try:
                    candidates.append(model.model_validate_json(canonical(data)))
                except (ValueError, TypeError):
                    continue
            require(len(candidates) == 1, "unclassified SDK/history row: " + key)
            value = candidates[0]
        contract = getattr(value, "configuration_digest", None)
        if contract is not None:
            require(contract == (worker_digest if getattr(value, "kind", "").startswith(("worker_", "native_"))
                                 else delivery_digest), "late row belongs to another runtime contract")
        require(getattr(value, "evidence_mode", "native") == "native", "offline evidence is not native retirement proof")
        if type(value) in fixed.values():
            require(key in fixed and fixed[key] is type(value), "typed row is stored under an unrelated key")
        if isinstance(value, (op._Operation, op._Attempt)) or data.get("kind", "").startswith(
                ("operation_dispatch", "work_effect_")):
            raise m._problem("ROTATION_OPERATION_UNKNOWN", "model-only retirement cannot carry business operations")
        if isinstance(value, d.QuotaRecord):
            require(value.pending == 0, "pending delivery quota")
        elif isinstance(value, d.PendingIndexRecord):
            require(not value.entries, "pending delivery index")
        elif isinstance(value, n._TargetReservation):
            require(key == n.native_key("target", value.target.executor_id)
                    and value.target in config.active_targets and value.active_launch_id is None,
                    "wrong or active native reservation")
        elif isinstance(value, n._NativeLaunch):
            require(value.api_binding_id == binding.binding_id and value.target in config.active_targets
                    and value.phase == "sealed"
                    and (value.pending_control == "cancel" or n.native_key("unclaimed-fence", value.launch_id) in unclaimed
                         and value.pending_control == "observe"),
                    "unsealed or unrelated launch")
        elif isinstance(value, w.JobRecord):
            require(key == w.worker_key("job", binding.job_id) and value.spec.job_id == binding.job_id
                    and value.state == terminal and value.result_forwarded, "unretired job")
        elif isinstance(value, n.NativeExecutionGrant):
            require(key == n.native_key("grant", value.grant_id) and value.job_id == binding.job_id
                    and value.owner == binding.owner and value.definition == binding.work
                    and value.targets == config.active_targets and value.controller_id == config.controller_id,
                    "unrelated native grant")
        elif isinstance(value, d._Obligation):
            require(key == op.control_key("delivery-obligation", binding.obligation_id)
                    and value.fulfillment == "fulfilled", "unsettled old obligation")
        elif isinstance(value, d._Work):
            require(key == op.control_key("delivery-work-state", binding.obligation_id)
                    and value.state == terminal and value.result is not None,
                    "unsettled old work")
        elif isinstance(value, d._Due):
            require(value.state == "done" and value.obligation_id == binding.obligation_id
                    and key == op.control_key("delivery-due", value.obligation_id, str(value.generation)),
                    "pending or unrelated due obligation")
        elif isinstance(value, CommandPlan):
            require(value.binding_id == binding.binding_id and value.context == binding.owner
                    and value.binding_digest == digest(["fa-behavior-api-v1", "native", binding.model_dump(mode="json")])
                    and value.request.request_id in inventory.receipts,
                    "late command plan is unbound or has no definitive receipt")
            original = runtime_key("plan", value.binding_id, value.kind, value.request_id)
            require(key in {original, runtime_key("consume", original)}, "command plan key differs")
            source_history.command(key, value)
        elif isinstance(value, _Delegation):
            require(value.human_oid == config.identity.human_oid and value.event_id == binding.event_id
                    and value.profile_digest == digest(config.assistant.profiles[0].model_dump(mode="json"))
                    and m._receipt(inventory, value.admission_request) is not None, "SDK delegation differs")
        elif isinstance(value, (w.ExecutorEvidence, d.VerifiedWorkResult)):
            result = value.result if isinstance(value, w.ExecutorEvidence) else value
            if result is not None:
                require(result.work_id == binding.work.work_id and result.state == terminal
                        and result == view.row(op.control_key("delivery-work-state", binding.obligation_id), d._Work).result,
                        "unrelated or unknown business result")
            if isinstance(value, w.ExecutorEvidence):
                require(value.binding.job_id == binding.job_id and key in {
                    w.worker_key("evidence", value.evidence_id),
                    w.worker_key("completion", binding.job_id, str(value.binding.generation))},
                    "terminal worker evidence key differs")
            else:
                require(key == w.worker_key("work-result", value.evidence_id), "worker result key differs")
        elif isinstance(value, w._Attempt):
            require(value.start.binding == view.row(w.worker_key("job", binding.job_id), w.JobRecord).binding
                    and key == w.worker_key("attempt", binding.job_id, str(value.start.binding.generation)),
                    "unrelated worker attempt")
        elif isinstance(value, w._ForwardPlan):
            require(key == w.worker_key("result-forward", binding.job_id) and value.job_id == binding.job_id
                    and value.context == binding.owner and m._receipt(inventory, value.request) is not None,
                    "unrelated or unsettled worker forwarding")
        elif isinstance(value, n._NativeProbe):
            launch = view.row(n.native_key("launch", value.launch_id), n._NativeLaunch)
            require(key == n.native_key("probe", value.probe_id) and launch.api_binding_id == binding.binding_id
                    and launch.phase == "sealed", "probe is not part of the sealed original execution")
        elif isinstance(value, m.HostedReportBody):
            probe = view.row(n.native_key("probe", value.probe_id), n._NativeProbe, material=True)
            launch = view.row(n.native_key("launch", probe.launch_id), n._NativeLaunch)
            evidence_id = n.native_key("evidence-id", launch.launch_id, value.probe_id)
            evidence = view.row(n.native_key("evidence", evidence_id), n._NativeEvidence, material=True)
            body = value.model_dump(mode="json")
            request = ControlRequest(
                request_id=n.native_key("report", launch.launch_id, value.probe_id),
                request_digest=digest(body),
                read_set=tuple(sorted({
                    n.native_key("launch", launch.launch_id), n.native_key("grant", launch.grant_id),
                    op.control_key("owner", binding.owner.owner_record_id),
                    op.control_key("policy", binding.owner.policy_id),
                    op.control_key("budget", binding.owner.root_budget_id),
                    w.worker_key("job", binding.job_id), n.native_key("target", launch.target.executor_id),
                    n.native_key("probe", value.probe_id),
                })),
            )
            require(key == n.native_key("report", evidence_id)
                    and launch.api_binding_id == binding.binding_id and launch.phase == "sealed"
                    and value.invocation_id == launch.native_invocation_id and value.claim_id == launch.claim_id
                    and value.process_id == value.supervisor_id == launch.process_id
                    and value.process_generation == launch.process_generation
                    and value.binding_digest == digest(launch.start.binding.model_dump(mode="json"))
                    and value.identity == n._identity(launch.target)
                    and value.sequence <= launch.last_report_sequence
                    and value.phase in {"spawn_unknown", "running", "result_saved"}
                    and value.result is None and value.checkpoint is None and value.outcome is None
                    and value.sdk_stop is None and not value.child_exited and value.exit_code is None
                    and evidence.probe_id == value.probe_id and evidence.launch_id == launch.launch_id
                    and evidence.report_digest == digest(body)
                    and evidence.evidence.binding == launch.start.binding
                    and evidence.evidence.state == ("running" if value.phase == "running" else "unknown")
                    and not evidence.evidence.entire_tree_idle and not evidence.evidence.all_inflight_starts_fenced
                    and probe.issued_at <= evidence.observed_at < probe.expires_at
                    and m._receipt(inventory, request) is not None,
                    "historical native report is not an exact nonterminal observation of the sealed original")
            view.row(key, m.HostedReportBody, material=True)
        elif isinstance(value, n._NativeEvidence):
            launch = view.row(n.native_key("launch", value.launch_id), n._NativeLaunch)
            require(key == n.native_key("evidence", value.evidence.evidence_id)
                    and value.evidence.binding == launch.start.binding and launch.api_binding_id == binding.binding_id
                    and launch.phase == "sealed", "native evidence is outside the sealed original execution")
        elif isinstance(value, w.WorkerReadiness):
            require(key in {w.worker_key("readiness", config.workers.workspace_id),
                            w.worker_key("readiness-evidence", value.evidence_id)}
                    and value.control_binding == config.workers.control_binding
                    and value.executor_id == config.workers.executor_id
                    and value.executor_version == config.workers.executor_version,
                    "worker readiness history scope differs")
        elif isinstance(value, inbox.InboxDeploymentEvidence):
            require(key in {op.control_key("inbox-readiness", delivery_digest),
                            op.control_key("inbox-readiness-evidence", value.evidence_id)}
                    and value.control_binding == config.workers.control_binding
                    and value.authentication_version == config.authentication_version,
                    "inbox readiness history scope differs")
        elif isinstance(value, d._Intent):
            require(key == op.control_key("delivery-intent", value.intent_id)
                    and value.obligation_id == binding.obligation_id
                    and value.state_row_id == op.control_key("delivery-work-state", binding.obligation_id)
                    and value.consumer_version == config.delivery.consumer_version, "unbound delivery intent")
        elif isinstance(value, d._ConsumerPlan):
            require(key == op.control_key("delivery-consumer-plan", binding.obligation_id)
                    and value.obligation_id == binding.obligation_id and value.context == binding.owner
                    and m._receipt(inventory, value.request) is not None, "unsettled delivery preparation")
        elif isinstance(value, d._ConsumerPage):
            cursor_key = op.control_key("delivery-consumer-cursor", value.root_budget_id)
            cursor = view.row(cursor_key, d._ConsumerPage)
            active = value.model_copy(update={"active": True})
            done = ControlRequest(
                request_id=op.control_key("delivery-consumer-page-done", value.root_budget_id, str(value.sequence)),
                request_digest=digest(active.model_dump(mode="json")), read_set=(cursor_key,))
            require(key in {op.control_key("delivery-consumer-page", value.root_budget_id, str(value.sequence)),
                            cursor_key}
                    and value.root_budget_id == binding.owner.root_budget_id and not cursor.active
                    and cursor.sequence >= value.sequence and m._receipt(inventory, done) is not None
                    and all(view.row(op.control_key("delivery-obligation", oid), d._Obligation).fulfillment == "fulfilled"
                            for oid in value.selected),
                    "active or unrelated consumer page")
        elif isinstance(value, (d._Outbox, inbox._Exchange)):
            require(value.binding.obligation_id == binding.obligation_id
                    and key == op.control_key("delivery-outbox" if isinstance(value, d._Outbox) else "inbox-exchange",
                                              value.binding.delivery_id),
                    "message/exchange is outside the settled obligation")
        elif isinstance(value, d.VerifiedIngress):
            require(key == runtime_key("ingress", value.evidence_id)
                    and value.on_behalf_of_principal_id == binding.owner.principal_id,
                    "ingress history identity differs")
        elif isinstance(value, inbox.VerifiedInboxAccess):
            require(key == runtime_key("inbox-access", value.evidence_id)
                    and value.binding.obligation_id == binding.obligation_id
                    and value.principal_id == config.identity.human_oid, "read history identity differs")
        elif isinstance(value, m._NativePostIntent):
            launch = view.row(n.native_key("launch", value.launch_id), n._NativeLaunch)
            require(key == n.native_key("post-intent", value.launch_id)
                    and value.request == m._native_post_request(config, launch)
                    and m._receipt(inventory, value.request) is not None, "unsettled native post intent")
        elif isinstance(value, m._WorkerAdmission):
            require(key == w.worker_key("admission", binding.obligation_id)
                    and value.job_id == binding.job_id and m._receipt(inventory, value.enqueue_request) is not None,
                    "unsettled worker admission")
        elif isinstance(value, memory.SubjectState):
            require(key == op.control_key("memory-subject", value.scope.key, binding.subject)
                    and value.scope in binding_scopes(config, binding) and value.subject == binding.subject,
                    "memory subject index escaped the original source namespace")
            for head in value.heads:
                view.row(head.reference.key, memory.ReviewedClaim)
        elif key not in fixed:
            raise m._problem("ROTATION_UNCLASSIFIED_RECORD", "unsupported late typed history", row_id=key)
        classified.add(key)
    require(set(inventory.rows) == classified | {m.installation_record(config)[0]}, "incomplete retirement catalog")
    work = view.row(op.control_key("delivery-work-state", binding.obligation_id), d._Work)
    from .behavior_recovery import MultiModelRecoveryPlan, RecoveryAuthorization
    multiple = [item.plan for item in recovery.values() if isinstance(item, RecoveryAuthorization)
                and isinstance(item.plan, MultiModelRecoveryPlan)]
    if finished:
        require(len(multiple) == 1 and isinstance(multiple[0], FinishedResultRecoveryPlan)
                and multiple[0] == finished_plans[0] and work.result.state == "succeeded"
                and not work.result.unresolved_operations and not work.result.known_effects,
                "verified finished-result closure must preserve the original successful result")
        return history, {
            "liability_kind": "finished_result_platform_stop", "business_result": "succeeded",
            "configuration_sha256": config.sha256, "job_id": binding.job_id, "work_id": binding.work.work_id,
            "root_budget_id": binding.owner.root_budget_id, "model_outcome": "unknown",
            "model_intents": tuple(item.model_dump(mode="json") for item in multiple[0].model_intents),
            "finished_result": multiple[0].finished_result.model_dump(mode="json"),
            "final_delivery_id": proof.binding.delivery_id, "receipt_evidence_id": proof.evidence_id,
        }
    if multiple:
        require(len(multiple) == 1 and not work.result.artifacts and not work.result.known_effects
                and work.result.unresolved_operations == tuple(item.request_sha256 for item in multiple[0].model_intents),
                "model-set result must preserve every original unresolved request")
        return history, {
            "liability_kind": "multiple_model_requests", "configuration_sha256": config.sha256,
            "job_id": binding.job_id, "work_id": binding.work.work_id, "root_budget_id": binding.owner.root_budget_id,
            "model_outcome": "unknown",
            "model_intents": tuple(item.model_dump(mode="json") for item in multiple[0].model_intents),
            "final_delivery_id": proof.binding.delivery_id, "receipt_evidence_id": proof.evidence_id,
        }
    require(work.result.artifacts == () and work.result.known_effects == ()
            and len(work.result.unresolved_operations) <= 1, "only a cancelled SDK result without business effects is supported")
    if not work.result.unresolved_operations:
        from .behavior_assistant import _Session, _ModelIntent
        from .behavior_recovery import PreAssignmentRecoveryPlan, RecoveryAuthorization
        sessions = [item for item in recovery.values() if isinstance(item, _Session)]
        unassigned = [item for item in recovery.values() if isinstance(item, RecoveryAuthorization)
                      and isinstance(item.plan, PreAssignmentRecoveryPlan)]
        require((len(sessions) == 1 and sessions[0].state == "accepted"
                and sessions[0].model_requests == 0 and not sessions[0].context_read
                 or not sessions and len(unassigned) == 1
                 or not sessions and not unassigned and len(unclaimed) == 1)
                and not any(isinstance(item, _ModelIntent) for item in recovery.values()),
                "zero-model retirement requires verified pre-SDK recovery")
    return history, {
        "configuration_sha256": config.sha256, "job_id": binding.job_id, "work_id": binding.work.work_id,
        "root_budget_id": binding.owner.root_budget_id,
        "model_request_sha256": work.result.unresolved_operations[0] if work.result.unresolved_operations else None,
        "model_outcome": "unknown" if work.result.unresolved_operations else "not_requested",
        "final_delivery_id": proof.binding.delivery_id, "receipt_evidence_id": proof.evidence_id,
    }


def _row_shape(model):
    """(required keys, allowed keys, kind Literal or None), or None when a validator or alias could rewrite input."""
    fields = model.model_fields
    try:  # private in pinned pydantic 2.12.5 (_internal/_decorators.py:415 DecoratorInfos): any other shape is slow path
        decorators = model.__pydantic_decorators__
        rewrites = bool(decorators.validators or decorators.root_validators
                        or any(item.info.mode != "after" for item in (*decorators.model_validators.values(),
                                                                       *decorators.field_validators.values())))
    except (AttributeError, TypeError):
        return None
    if (model.model_config.get("extra") != "forbid" or getattr(model, "__pydantic_root_model__", False) or rewrites
            or any(field.alias not in (None, name) or field.validation_alias is not None
                   for name, field in fields.items())):
        return None
    kind = fields.get("kind")
    kinds = (get_args(kind.annotation) if kind is not None and not kind.metadata
             and get_origin(kind.annotation) is Literal else None)
    return frozenset(name for name, field in fields.items() if field.is_required()), frozenset(fields), kinds


def _refused(shape, data):
    """True only where pydantic must refuse the row (a missing or extra key, or a kind outside the Literal)."""
    if shape is None:
        return False
    required, allowed, kinds = shape
    return (not required <= data.keys() <= allowed
            or kinds is not None and "kind" in data and data["kind"] not in kinds)


def validate_additions(inventory, config, previous, retained, metadata, *, bindings_override=None, extra_models=(),
                       max_inventory_entities=m.DEFAULT_INVENTORY_MAX_ENTITIES):
    """Startup admits typed current work, not a renewed predecessor capability."""
    from . import behavior_assistant as assistant, memory_governance as memory, source_reviews
    from . import behavior_assistant_wire as aw
    from . import source_backup
    from .behavior_recovery_history import validate_recovery_history
    from .source_materials import SourceEvent, SourceHead, SourceSnapshot, _Preparation, _SourceWithdrawal
    from .behavior_instruction_cost import AgentTurnUsageRecord, ModelCost, TurnUsageRecord
    from .behavior_instruction_progress import ProgressRecord
    from .memory_note_proposals import NoteProposalRecord
    from .memory_candidates import MemoryCandidateRecord
    from .memory_note_synthesis import NoteSynthesisRecord

    receipt_catalog(inventory)
    kinds = m._kind_models()
    models = (
        CommandPlan, RuntimePolicy, _PlanIndex, op.HumanOperationRejection, op.HumanOperationRejectionIntent,
        assistant._Delegation, assistant._Session,
        assistant._ModelIntent, assistant._ArtifactEffect,ModelCost,TurnUsageRecord,AgentTurnUsageRecord,ProgressRecord,n.InstructionSessionStop,
        SourceEvent, SourceHead, SourceSnapshot, _Preparation, _SourceWithdrawal,
        source_backup._UploadIntent, source_backup._Completion, source_reviews._DecisionRecord,
        memory.ReviewedClaim, memory.SubjectState, memory.ClaimRevocation, memory.ProjectionRecord,
        w.ExecutorEvidence, w.ExecutorStop, d.VerifiedWorkResult, d.VerifiedIngress, inbox.VerifiedInboxAccess,
        d.VerifiedDeliveryReceipt, m.HostedReportBody, m._NativePostIntent, m._WorkerAdmission,
        NoteProposalRecord, MemoryCandidateRecord, NoteSynthesisRecord,
    ) + tuple(extra_models)
    # Skipping a model only where it must fail leaves each row's matches, and so every message, unchanged.
    shapes = tuple((model, _row_shape(model)) for model in models)
    selected = config.bindings if bindings_override is None else bindings_override
    bindings = {binding.binding_id: binding for binding in selected}
    jobs = {binding.job_id for binding in selected}
    work_ids = {binding.work.work_id for binding in selected}
    mutable = shared_keys(config) - {m.installation_record(config)[0]}
    additions = {key: row for key, row in inventory.rows.items()
                 if key in mutable or key not in retained and key not in metadata}
    current_inventory = replace(inventory, rows=additions)
    observer_history = m._operator_history(current_inventory, config)
    from .behavior_settlement_history import validate_settlement_history
    late_revocations = {key for key, row in additions.items()
                        if row.payload["data"].get("kind") == "retained_result_settlement_revocation"
                        and row.payload["data"].get("command", {}).get("authorization_id") in retained}
    settlement_history = validate_settlement_history(replace(current_inventory, rows={
        key: row for key, row in additions.items() if key not in late_revocations
    }), config)
    recovery_history = validate_recovery_history(replace(current_inventory, rows={
        key: row for key, row in additions.items() if key not in settlement_history
    }), config,max_entities=max_inventory_entities)
    unclaimed_history = validate_unclaimed_history(current_inventory, config,
        max_entities=max_inventory_entities,bindings=selected)
    from .behavior_recovery_close import validate_close_history
    closed_history = validate_close_history(current_inventory, config, selected)
    from .behavior_result_delivery import validate_result_history
    result_history = validate_result_history(current_inventory, config, selected)
    from .behavior_work_target import validate_target_history, target_key
    target_selection = validate_target_history(inventory, config)
    target_history = set() if target_selection is None else {target_key(config.table.scope.authority_domain)}
    from .behavior_instruction import validate_instruction_history
    instruction_history = validate_instruction_history(inventory, config, selected)
    from .behavior_delegation import validate_delegation_history
    delegation_history = validate_delegation_history(inventory, config, selected, row_ids=set(additions))
    from .behavior_agent_message_history import validate_agent_message_history
    agent_message_history = validate_agent_message_history(inventory, config)
    from .behavior_agent_registry import AgentScopeRegistry, validate_agent_registry_history
    agent_registry_history = validate_agent_registry_history(inventory, config)
    from .behavior_team_registry import validate_team_registry_history
    team_registry_history = validate_team_registry_history(inventory, config)
    from .behavior_turn_receipts import validate_turn_receipt_history
    turn_receipt_history = validate_turn_receipt_history(inventory, config)
    from .behavior_team_authority import validate_team_authority_history
    team_authority_history = validate_team_authority_history(inventory, config)
    from .native_hitl import validate_native_hitl_history
    hitl_history = validate_native_hitl_history(inventory, config)
    from .team_mcp import validate_team_mcp_history
    team_mcp_history = validate_team_mcp_history(inventory, config)
    from .native_routines import validate_native_routine_history
    routine_history = validate_native_routine_history(inventory, config)
    subject_scopes = [(binding, binding_scopes(config, binding)) for binding in config.bindings]
    subject_scopes.extend((value.policy.template,
                           binding_scopes(config, value.policy.template, value.policy.legacy_template_policy))
                          for value in agent_registry_history.values() if isinstance(value, AgentScopeRegistry))
    current_targets = config.active_targets if target_selection is None else (target_selection.command.target,)
    for key, record in additions.items():
        if (key in observer_history or key in recovery_history or key in settlement_history
                or key in unclaimed_history or key in closed_history or key in result_history or key in target_history
                or key in instruction_history or key in delegation_history or key in agent_registry_history
                or key in agent_message_history or key in team_registry_history
                or key in turn_receipt_history or key in team_authority_history or key in hitl_history
                or key in team_mcp_history or key in routine_history):
            continue
        data = record.payload["data"]
        if data.get("kind") == "retained_result_settlement_revocation":
            rev = m._parsed(data, _Revocation)
            auth = m._parsed(m._row(inventory, rev.command.authorization_id).payload["data"], SettlementAuthorization)
            require(auth.plan.configuration_sha256 == previous.sha256
                    and key == settlement_key("revocation", auth.plan.settlement_id)
                    and rev.principal_id == previous.identity.human_oid
                    and rev.authorization_sha256 == digest(auth.model_dump(mode="json"))
                    and record.payload["material"] == d._material(data).model_dump(mode="json"),
                    "late settlement revocation is not the original immutable history")
            receipt = m._receipt(inventory, ControlRequest(request_id=key,
                request_digest=digest(rev.command.model_dump(mode="json")), read_set=(rev.command.authorization_id,)))
            require(receipt is not None and dict(receipt.payload["writes"]) == {key: _payload_hash(record.payload)}
                    and receipt.payload["result"] == {"authorization_id": rev.command.authorization_id, "revoked": True},
                    "late revocation has no original receipt")
            continue
        model = kinds.get(data.get("kind"))
        if model is not None:
            value = m._parsed(data, model)
        else:
            matches = []
            for model, shape in shapes:
                if _refused(shape, data):
                    continue
                try:
                    matches.append(model.model_validate_json(canonical(data)))
                except (ValueError, TypeError):
                    continue
            if not matches and set(data) == {"sequence"}:
                scopes = set()
                for row in additions.values():
                    try:
                        scopes.add(SourceHead.model_validate_json(canonical(row.payload["data"])).reference.scope.key)
                    except (ValueError, TypeError):
                        continue
                require(type(data["sequence"]) is int and data["sequence"] >= 0
                        and key in {op.control_key("source-cursor", scope) for scope in scopes}, "unbound late source cursor")
                continue
            require(len(matches) == 1, "unclassified late current-runtime row: " + key)
            value = matches[0]
        root = getattr(value, "root_budget_id", None)
        require(root is None or root in {root.root_budget_id for root in config.roots}, "late row escaped the original root")
        if isinstance(value, (n.NativeExecutionGrant, w.JobRecord, assistant._Session)):
            job_id = value.job_id if isinstance(value, n.NativeExecutionGrant) else (
                value.spec.job_id if isinstance(value, w.JobRecord) else value.execution.job_id)
            require(job_id in jobs, "old grants/jobs cannot become current execution authority")
        if isinstance(value,w._Pool) and value.job_id is not None:
            job=m._parsed(m._row(inventory,w.worker_key("job",value.job_id)).payload["data"],w.JobRecord)
            require(value.job_id in jobs and key==w.worker_key("pool",config.workers.workspace_id,value.job_id)
                    and aw.Task.model_validate_json(job.spec.input).instruction is not None
                    and len(value.slots)==1 and value.slots[0].number==0
                    and (value.slots[0].binding is None or value.slots[0].binding.job_id==value.job_id)
                    and set((*value.pending,*value.completions))<={value.job_id},
                    "instruction slot crossed another original work")
        if isinstance(value,n._TargetReservation) and value.job_id is not None:
            grant=m._parsed(m._row(inventory,n.native_key("grant",n.native_key("bootstrap-grant",value.job_id))).payload["data"],
                            n.NativeExecutionGrant)
            require(value.job_id in jobs and value.target in grant.targets
                    and key==n.native_key("target",value.target.executor_id,value.job_id),
                    "instruction target crossed another original session")
        if isinstance(value,ModelCost):
            from .behavior_instruction_cost import cost_key
            require(value.job_id in jobs and key==cost_key(value.session_id,value.request_sha256),
                    "instruction model cost escaped its original work")
        if isinstance(value,TurnUsageRecord):
            from .behavior_instruction_cost import turn_usage_key
            require(value.job_id in jobs and key==turn_usage_key(value.job_id,value.rid),
                    "instruction turn usage escaped its original work")
        if isinstance(value,AgentTurnUsageRecord):
            # Its Agent is checked against the original policies when the cost ledger is replayed.
            from .behavior_instruction_cost import agent_turn_usage_key
            require(key==agent_turn_usage_key(value.agent_oid,value.rid,value.source),
                    "agent turn usage escaped its original key")
        if isinstance(value,ProgressRecord):
            require(value.job_id in jobs and key==assistant.key("progress",value.session_id),
                    "public SDK progress escaped its original work")
        if isinstance(value,n.InstructionSessionStop):
            launch=m._parsed(m._row(inventory,n.native_key("launch",value.launch_id)).payload["data"],n._NativeLaunch)
            require(value.job_id in jobs and value.target==launch.target and launch.phase=="sealed"
                and launch.start.binding.job_id==value.job_id
                and aw.Task.model_validate_json(launch.start.input).instruction is not None
                and key==n.native_key("instruction-session-stop",value.launch_id),
                "session cleanup escaped its completed original instruction")
        if isinstance(value, w.ExecutorStop):
            binding = value.binding
            jobkey = w.worker_key("job", binding.job_id)
            ownerkeys = (op.control_key("owner", binding.owner.owner_record_id),
                         op.control_key("policy", binding.owner.policy_id))
            worker_digest = digest({"contract": config.workers.model_dump(mode="json"), "mode": "native"})
            expected = ControlRequest(request_id=w.worker_key("stop", binding.job_id, str(binding.generation)),
                request_digest=digest(["stop", binding.model_dump(mode="json"), worker_digest]),
                read_set=tuple(sorted((jobkey, *ownerkeys))))
            receipt = m._receipt(inventory, expected)
            require(binding.job_id in jobs and key == w.worker_key("stop-intent", binding.job_id, str(binding.generation))
                    and value.request == expected and record.payload["material"] == d._material(value.model_dump(mode="json")).model_dump(mode="json")
                    and receipt is not None and set(dict(receipt.payload["writes"])) == {jobkey, key}
                    and dict(receipt.payload["writes"])[key] == _payload_hash(record.payload),
                    "original stop reservation lacks its exact typed receipt")
        if isinstance(value, n._NativeLaunch):
            grant=m._parsed(m._row(inventory,n.native_key("grant",value.grant_id)).payload["data"],n.NativeExecutionGrant)
            free=(value.workload_version=="assistant-v1.2"
                  and aw.Task.model_validate_json(value.start.input).instruction is not None)
            require(value.api_binding_id in bindings and value.target in (
                    grant.targets if free else (*config.active_targets,*current_targets))
                    and value.start.binding.job_id in jobs, "late launch is not a configured successor")
        if isinstance(value, CommandPlan):
            require(value.binding_id in bindings and value.binding_digest == digest([
                "fa-behavior-api-v1", "native", bindings[value.binding_id].model_dump(mode="json")]),
                "late plan cannot use a retired binding")
        if isinstance(value, d._Work):
            require(value.definition.work_id in work_ids, "late work is not a configured successor")
        if isinstance(value, n._TargetReservation) and value.job_id is None:
            require(value.target in (config.targets if target_selection is None else current_targets)
                    and key == n.native_key("target", value.target.executor_id),
                    "current target reservation differs")
        if isinstance(value, w._Pool) and value.job_id is None:
            require(key == w.worker_key("pool", config.workers.workspace_id)
                    and value.workspace_id == config.workers.workspace_id
                    and len(value.slots) == config.workers.capacity
                    and {slot.number for slot in value.slots} == set(range(config.workers.capacity))
                    and all(slot.binding is None or slot.binding.job_id in jobs for slot in value.slots),
                    "current global pool contains an unregistered execution")
        if isinstance(value, memory.SubjectState):
            require(any(value.scope in scopes and value.subject == binding.subject
                        and key == op.control_key("memory-subject", value.scope.key, binding.subject)
                        for binding, scopes in subject_scopes), "current subject escaped the original namespace")
            for head in value.heads:
                claim = m._parsed(m._row(inventory, head.reference.key).payload["data"], memory.ReviewedClaim)
                require(claim.reference == head.reference, "current claim index has no exact immutable claim")
