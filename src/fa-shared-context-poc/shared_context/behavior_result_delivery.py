"""Append the saved cancellation result after an unexchanged queued failure notice."""
from typing import Literal

from . import behavior_delivery as d
from .behavior_auth import BehaviorPrincipal
from .behavior_foundry import _NativeLaunch, native_key
from .behavior_inbox import CurrentWorkInboxAccess, INBOX_CHANNEL_ID
from .behavior_operations import control_key
from .behavior_runtime import RuntimeBinding, runtime_key
from .behavior_store import ControlChanges, ControlRequest
from .behavior_workers import ExecutorEvidence, JobRecord, worker_key
from .errors import Problem
from .models import Identifier
from .source_materials import digest


class OriginalResultDelivery(d._Model):
    kind: Literal["original_result_delivery"] = "original_result_delivery"
    runtime_binding_id: Identifier
    job_id: Identifier
    original_fetch_request_id: Identifier
    original_access_evidence_id: Identifier
    previous: d.DeliveryBinding
    current: d.DeliveryBinding
    original_obligation: d._Obligation
    request: ControlRequest


def continuation_key(binding_id):
    return runtime_key("result-continuation", binding_id)


def require(condition, message):
    if not condition:
        raise Problem(409, "RESULT_CONTINUATION_DENIED", message)


def _payload(binding, result):
    return d._Payload(
        event_id=binding.event_id, root_request_id=binding.root_request_id, work_id=binding.work.work_id,
        message_kind="failure", work_state="cancelled", result_evidence_id=result.evidence_id,
        content=result.content, artifacts=result.artifacts, known_effects=result.known_effects,
        unresolved_operations=result.unresolved_operations, responsibility_owner=binding.owner.owner_record_id,
        notification_code="delivery_needs_attention",
    )


def _terminal(core, rows, binding):
    job = core._row(rows, worker_key("job", binding.job_id), JobRecord)
    work = core._row(rows, control_key("delivery-work-state", binding.obligation_id), d._Work)
    result = work.result
    require(job.spec.job_id == binding.job_id and job.spec.work_id == binding.work.work_id
            and job.spec.obligation_id == binding.obligation_id and job.owner == binding.owner
            and job.state == work.state == "cancelled" and job.result_forwarded and not job.completion_credit
            and job.cancel_requested and not job.stop_credit and job.binding is not None
            and job.generation == 1 and result is not None
            and work.definition == binding.work and result.work_id == binding.work.work_id
            and result.state == "cancelled" and result.definition_digest == digest(binding.work.model_dump(mode="json"))
            and result.work_revision == 1 and result.verifier_version == core.contract.verifier_version,
            "original saved terminal work differs")
    savedkey = worker_key("work-result", result.evidence_id)
    saved = core._row(rows, savedkey, d.VerifiedWorkResult)
    completionkey = worker_key("completion", binding.job_id, "1")
    completed = core._row(rows, completionkey, ExecutorEvidence)
    launch = core._row(rows, native_key("launch", native_key("launch-id", job.binding.start_request_id)), _NativeLaunch)
    require(saved == result and rows[savedkey].payload["material"] == d._material(result.model_dump(mode="json")).model_dump(mode="json")
            and completed.binding == job.binding and completed.result == result and completed.state == "stopped"
            and completed.evidence_id == job.last_evidence_id and completed.entire_tree_idle
            and completed.all_inflight_starts_fenced and launch.phase == "sealed"
            and launch.start.binding == job.binding and launch.api_binding_id == binding.binding_id
            and launch.definition == binding.work and launch.start.input == job.spec.input
            and launch.configuration_digest == job.configuration_digest
            and completed.native_execution_id == launch.native_invocation_id
            and rows[completionkey].payload["material"] == d._material(completed.model_dump(mode="json")).model_dump(mode="json"),
            "original cancellation completion is not retained")
    return result


def _stale(core, rows, binding, previous):
    ob = core._row(rows, control_key("delivery-obligation", binding.obligation_id), d._Obligation)
    key = control_key("delivery-outbox", previous.delivery_id)
    old = core._row(rows, key, d._Outbox)
    material = d._parse(rows[key].payload["material"], d.InlineMaterial)
    expected = d._Payload(
        event_id=binding.event_id, root_request_id=binding.root_request_id, work_id=binding.work.work_id,
        message_kind="failure", work_state="queued", result_evidence_id=None, content=None,
        artifacts=(), known_effects=None, unresolved_operations=None,
        responsibility_owner=binding.owner.owner_record_id, notification_code="delivery_needs_attention",
    )
    require(previous == old.binding and old.message_kind == "failure" and old.send_outcome is None
            and old.receipt_count == 0 and not old.confirmed_levels
            and previous.obligation_id == binding.obligation_id and previous.route == binding.route
            and previous.channel_version == ob.channel_version
            and material == d._material(expected.model_dump(mode="json")) and material.sha256 == previous.payload_digest,
            "only the original unsent queued failure may be continued")
    require(rows[control_key("inbox-exchange", previous.delivery_id)] is None,
            "an original exchange exists; do not replace or repeat its delivery")
    return ob, old


def append_saved_result(runtime, binding_id, command, principal, previous, *, reconcile=False):
    """Server-owned Table-only operator; never invoked automatically by fetch."""
    binding, _, context = runtime.authorize(binding_id, principal, "read")
    principal.require(binding.team_id, "approve")
    require(isinstance(principal, BehaviorPrincipal) and principal.kind == "human"
            and principal.oid == binding.human_oid and context == binding.owner
            and runtime.work_policy is not None and binding_id in runtime.work_policy.records
            and binding.route.channel_id == INBOX_CHANNEL_ID, "original standing human result scope required")
    previous = d._check(previous, d.DeliveryBinding)
    store, core = runtime.store, runtime.dep.delivery
    key = continuation_key(binding_id)
    body = [binding_id, command.model_dump(mode="json"), previous.model_dump(mode="json")]
    prior = store.read_snapshot((key,))[key]
    if prior is not None:
        saved = d._parse(prior.payload["data"], OriginalResultDelivery)
        require(saved.runtime_binding_id == binding_id and saved.original_fetch_request_id == command.request_id
                and saved.previous == previous and saved.request.request_id == key
                and saved.request.request_digest == digest(body)
                and prior.payload["material"] == d._material(saved.model_dump(mode="json")).model_dump(mode="json"),
                "original continuation ID is bound to different input")
        return store.reconcile(saved.request)
    if reconcile:
        raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "original result continuation receipt is absent",
                      request_id=key)
    jobkey, workkey = worker_key("job", binding.job_id), control_key("delivery-work-state", binding.obligation_id)
    first = store.read_snapshot((jobkey, workkey))
    job = core._row(first, jobkey, JobRecord)
    work = core._row(first, workkey, d._Work)
    require(job.binding is not None and work.result is not None, "original terminal result required")
    did = control_key("delivery-result-version", binding.obligation_id, str(previous.version + 1))
    accessid = runtime_key("current-pull-access", binding_id, "fetch")
    accesskey = runtime_key("inbox-access", accessid)
    newkey = control_key("delivery-outbox", did)
    reads = tuple(dict.fromkeys((
        *runtime._gate_keys(binding), jobkey, workkey,
        worker_key("work-result", work.result.evidence_id), worker_key("completion", binding.job_id, "1"),
        native_key("launch", native_key("launch-id", job.binding.start_request_id)),
        control_key("delivery-obligation", binding.obligation_id),
        control_key("delivery-outbox", previous.delivery_id), control_key("inbox-exchange", previous.delivery_id),
        control_key("inbox-exchange", did), accesskey, key, newkey,
    )))
    request = ControlRequest(request_id=key, request_digest=digest(body), read_set=reads)

    def decide(rows):
        _, current_context = runtime._state(binding, rows, use="read")
        require(current_context == context == binding.owner, "original owner epoch changed")
        result = _terminal(core, rows, binding)
        ob, _ = _stale(core, rows, binding, previous)
        access = core._row(rows, accesskey, CurrentWorkInboxAccess)
        require(ob.final_delivery_id == previous.delivery_id and ob.final_version == ob.last_version == previous.version
                and ob.fulfillment == "pending" and not ob.confirmed_levels
                and ob.route == binding.route and previous.version < core.contract.max_delivery_versions
                and access.binding == previous and access.runtime_binding_id == binding_id
                and access.principal_id == principal.oid and access.action == "fetch"
                and rows[accesskey].payload["material"] == d._material(access.model_dump(mode="json")).model_dump(mode="json")
                and rows[newkey] is None and rows[key] is None and rows[control_key("inbox-exchange", did)] is None,
                "original failed access/version changed or delivery already started")
        material = d._material(_payload(binding, result).model_dump(mode="json"))
        current = previous.model_copy(update={"delivery_id": did, "version": previous.version + 1,
                                              "payload_digest": material.sha256, "artifacts": result.artifacts})
        record = OriginalResultDelivery(runtime_binding_id=binding_id, job_id=binding.job_id,
            original_fetch_request_id=command.request_id, original_access_evidence_id=accessid,
            previous=previous, current=current, original_obligation=ob, request=request)
        return ControlChanges(writes=(
            core._write(key, record, "create", d._material(record.model_dump(mode="json"))),
            core._write(newkey, d._Outbox(configuration_digest=core.configuration_digest(),
                                         binding=current, message_kind="failure"), "create", material),
            core._write(control_key("delivery-obligation", ob.obligation_id),
                d._update(ob, final_delivery_id=did, final_version=current.version, last_version=current.version,
                          delivery_state="final_prepared")),
        ), result={"binding_id": binding_id, "delivery_id": did, "version": current.version,
                   "original_fetch_request_id": command.request_id, "previous_delivery_id": previous.delivery_id,
                   "exchange_created": False, "acknowledged": False})
    return store.transact(request, decide)


def require_continuation(runtime, binding_id, command, principal):
    binding, _, context = runtime.authorize(binding_id, principal, "read")
    row = runtime.store.read_snapshot((continuation_key(binding_id),))[continuation_key(binding_id)]
    if row is None:
        raise Problem(409, "RESULT_CONTINUATION_NOT_READY", "original retained result continuation is not prepared")
    value = d._parse(row.payload["data"], OriginalResultDelivery)
    require(value.runtime_binding_id == binding_id and value.job_id == binding.job_id
            and value.original_fetch_request_id == command.request_id
            and value.current.route == binding.route and value.current.obligation_id == binding.obligation_id,
            "continue only the original failed fetch ID and recipient")
    require(row.payload["material"] == d._material(value.model_dump(mode="json")).model_dump(mode="json"),
            "original continuation material differs")
    runtime.store.reconcile(value.request)
    require(runtime.store.read_snapshot((control_key("inbox-exchange", value.previous.delivery_id),))[
        control_key("inbox-exchange", value.previous.delivery_id)] is None,
        "original delivery exchange exists; continuation denied")
    return value, binding, context


def validate_result_history(inventory, config, bindings):
    """Recognize this producer without accepting arbitrary appended final versions."""
    candidates = {key: row for key, row in inventory.rows.items()
                  if row.payload["data"].get("kind") == "original_result_delivery"}
    if not candidates:
        return {}
    from .behavior_recovery_history import _Inventory
    view = _Inventory(inventory, config)
    for key in candidates:
        value = view.row(key, OriginalResultDelivery, material=True)
        binding = next((b for b in bindings if b.binding_id == value.runtime_binding_id), None)
        require(binding is not None and key == continuation_key(binding.binding_id)
                and value.job_id == binding.job_id and value.previous.obligation_id == value.current.obligation_id == binding.obligation_id
                and value.previous.route == value.current.route == binding.route
                and value.current.version == value.previous.version + 1 <= config.delivery.max_delivery_versions
                and value.current.delivery_id == control_key("delivery-result-version", binding.obligation_id, str(value.current.version)),
                "result continuation escaped original binding/version")
        oldkey = control_key("delivery-outbox", value.previous.delivery_id)
        newkey = control_key("delivery-outbox", value.current.delivery_id)
        obkey = control_key("delivery-obligation", binding.obligation_id)
        old = view.row(oldkey, d._Outbox)
        new = view.row(newkey, d._Outbox)
        ob = view.row(obkey, d._Obligation)
        work = view.row(control_key("delivery-work-state", binding.obligation_id), d._Work)
        require(work.result is not None and work.result.state == "cancelled", "saved cancellation result is missing")
        result = view.row(worker_key("work-result", work.result.evidence_id), d.VerifiedWorkResult, material=True)
        material = d._material(_payload(binding, result).model_dump(mode="json"))
        oldmaterial = d._parse(inventory.rows[oldkey].payload["material"], d.InlineMaterial)
        expected_old = d._Payload(event_id=binding.event_id, root_request_id=binding.root_request_id,
            work_id=binding.work.work_id, message_kind="failure", work_state="queued",
            result_evidence_id=None, content=None, artifacts=(), known_effects=None, unresolved_operations=None,
            responsibility_owner=binding.owner.owner_record_id, notification_code="delivery_needs_attention")
        require(old.binding == value.previous and old.message_kind == new.message_kind == "failure"
                and old.send_outcome is None and old.receipt_count == 0 and not old.confirmed_levels
                and oldmaterial == d._material(expected_old.model_dump(mode="json"))
                and oldmaterial.sha256 == value.previous.payload_digest
                and inventory.rows.get(control_key("inbox-exchange", value.previous.delivery_id)) is None
                and result == work.result and new.binding == value.current
                and value.current.payload_digest == material.sha256 and value.current.artifacts == result.artifacts
                and inventory.rows[newkey].payload["material"] == material.model_dump(mode="json")
                and ob.final_delivery_id == value.current.delivery_id and ob.final_version == ob.last_version == value.current.version,
                "result continuation did not retain exact old/new payload")
        original = value.original_obligation
        require(original.obligation_id == binding.obligation_id and original.final_delivery_id == value.previous.delivery_id
                and original.final_version == original.last_version == value.previous.version
                and original.fulfillment == "pending" and not original.confirmed_levels
                and original.route == binding.route,
                "result continuation original obligation differs")
        oldaccesskey = runtime_key("inbox-access", value.original_access_evidence_id)
        access = view.row(oldaccesskey, CurrentWorkInboxAccess, material=True)
        require(access.binding == value.previous and access.runtime_binding_id == binding.binding_id
                and access.principal_id == binding.human_oid and access.action == "fetch",
                "original failed read is not retained")
        body = [binding.binding_id, {"request_id": value.original_fetch_request_id}, value.previous.model_dump(mode="json")]
        require(value.request.request_id == key and value.request.request_digest == digest(body),
                "original request ID continuation differs")
        freshout = d._Outbox(configuration_digest=new.configuration_digest, evidence_mode=new.evidence_mode,
                            binding=value.current, message_kind="failure")
        after = d._update(original, final_delivery_id=value.current.delivery_id, final_version=value.current.version,
                          last_version=value.current.version, delivery_state="final_prepared")
        # _Outbox's defaults are the same initial image emitted by prepare_message.
        view.receipt(value.request, (key, newkey, obkey), exact={
            key: view.hash(key), newkey: view.image_hash(newkey, freshout), obkey: view.image_hash(obkey, after),
        }, result={"binding_id": binding.binding_id, "delivery_id": value.current.delivery_id,
                   "version": value.current.version, "original_fetch_request_id": value.original_fetch_request_id,
                   "previous_delivery_id": value.previous.delivery_id, "exchange_created": False, "acknowledged": False})
    return candidates
