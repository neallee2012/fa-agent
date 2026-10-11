"""Read-only catalog of the one original cancellation delivery, never a sender."""
from types import SimpleNamespace
import hashlib

from . import behavior_delivery as d, behavior_operations as op
from .behavior_recovery_history import _require
from .behavior_recovery import ExpiredTeamsCleanupPlan, recovery_key
from .behavior_runtime import runtime_key
from .behavior_store import ControlRequest
from .behavior_channels_migration import channel_key
from .behavior_workers import worker_key
from .behavior_migration import installation_record
from .source_materials import digest
from .teams_delivery import TeamsSendPlan, TeamsSendContext, TeamsDeliveryAdapter, _Send, _ProviderReceipt
from .teams_identity import TEAMS_VERSION, TeamsSettings
from .teams_native import TeamsNativeEvidence, TeamsNativeRegistration, TeamsNativeGate
from .errors import Problem


def failure_payload(binding, original_result):
    return d._Payload(event_id=binding.event_id, root_request_id=binding.root_request_id,
        work_id=binding.work.work_id, message_kind="failure", work_state="cancelled",
        result_evidence_id=original_result.evidence_id, content=original_result.content,
        artifacts=(), known_effects=(), unresolved_operations=(), responsibility_owner=binding.owner.owner_record_id,
        notification_code="delivery_needs_attention")


def delivery_history(view, config, binding, *, original_result, authorization, continuation=None):
    """Resolve exact producer requests/typed writes for each durable send stage."""
    owner, obid = binding.owner, binding.obligation_id
    cleanup = isinstance(authorization.plan, ExpiredTeamsCleanupPlan)
    until = authorization.expires_at if cleanup else config.channels.valid_until
    authkey = recovery_key("authorization", authorization.plan.recovery_id)
    if continuation is not None:
        from .behavior_teams_continuation import DeliveryContinuationAuthorization, continuation_key
        _require(type(continuation) is DeliveryContinuationAuthorization and cleanup,
                 "explicit original delivery continuation required")
        until = continuation.expires_at
        authkey = continuation_key(continuation.plan.delivery_id)
    obkey = op.control_key("delivery-obligation", obid)
    workkey = op.control_key("delivery-work-state", obid)
    work = view.row(workkey, d._Work)
    ob = view.row(obkey, d._Obligation)
    did = op.control_key("delivery-final-id", obid)
    pkey = op.control_key("delivery-consumer-plan", obid)
    if pkey not in view.inventory.rows:
        return set()
    _require(work.result == original_result and work.state == "cancelled", "Teams recovery delivery is not the original cancellation")
    plan = view.row(pkey, d._ConsumerPlan, material=True)
    core_digest = digest({"contract": config.delivery.model_dump(mode="json"), "mode": "native"})
    quotas = tuple(op.control_key("delivery-quota", kind, value) for kind, value in (
        ("team", config.identity.team_id), ("entry", config.delivery.entry_id), ("root", owner.root_budget_id)))
    builder = SimpleNamespace(_enabled=lambda: (None, config.delivery), configuration_digest=lambda: core_digest,
                              _quota_keys=lambda _: quotas)
    def delivery_request(command):
        return d.DeliveryCore.request_for(builder, command, owner)
    intent_id = op.control_key("delivery-intent-id", binding.event_id)
    common = (op.control_key("owner", owner.owner_record_id), op.control_key("delivery-conversation", owner.conversation_id),
              obkey, workkey, op.control_key("budget", owner.root_budget_id), pkey,
              op.control_key("delivery-intent", intent_id))
    claim = ControlRequest(request_id=op.control_key("delivery-consumer-claim", obid),
                           request_digest=digest([obid, core_digest]), read_set=tuple(sorted(common)))
    _require(plan.obligation_id == obid and plan.context == owner and plan.intent_id == intent_id
             and plan.configuration_digest == core_digest and plan.evidence_mode == "native"
             and plan.consumer_version == config.delivery.consumer_version
             and plan.command.delivery_id == did and plan.command.message_kind == "failure"
             and plan.request == delivery_request(plan.command), "original cancellation consumer plan differs")
    view.receipt(claim, (pkey,), exact={pkey: view.hash(pkey)})
    selected = {pkey}
    outkey = op.control_key("delivery-outbox", did)
    if outkey not in view.inventory.rows:
        return selected
    out = view.row(outkey, d._Outbox)
    payload = failure_payload(binding, original_result)
    material = d._material(payload.model_dump(mode="json"))
    _require(out.message_kind == "failure" and out.binding.delivery_id == did
             and out.binding.obligation_id == obid and out.binding.route == binding.route
             and out.binding.channel_version == TEAMS_VERSION and not out.binding.artifacts
             and out.binding.payload_digest == material.sha256
             and view.inventory.rows[outkey].payload["material"] == material.model_dump(mode="json")
             and ob.final_delivery_id == did and ob.route == binding.route
             and ob.required_confirmation_level == "provider_accepted", "original Teams result/outbox bytes differ")
    view.receipt(plan.request, (outkey, obkey))
    selected.add(outkey)
    acl = op.control_key("teams-route-access", config.channels.route_id)
    ownerkey = op.control_key("owner", owner.owner_record_id)
    gates = (runtime_key("policy", binding.binding_id), ownerkey, op.control_key("policy", owner.policy_id),
             op.control_key("budget", owner.root_budget_id), channel_key("current", config.table.scope.authority_domain),
             channel_key("revocation", config.sha256), channel_key("work", binding.event_id), acl)
    if cleanup:
        gates = (*gates, authkey)
    scope = ControlRequest(request_id=channel_key("result-scope", did),
        request_digest=digest([binding.binding_id, did]),
        read_set=tuple(dict.fromkeys((*gates, acl, ownerkey, outkey, worker_key("job", binding.job_id), workkey))))
    if scope.request_id in view.receipts:
        view.receipt(scope, (acl, ownerkey), result={"result_scope_bound": True})
    send_plan_key = op.control_key("teams-send-plan", did)
    if send_plan_key not in view.inventory.rows:
        return selected
    send_plan = view.row(send_plan_key, TeamsSendPlan, material=True)
    _require(send_plan.binding == out.binding and send_plan.evidence_mode == "native"
             and send_plan.operation_id == TeamsDeliveryAdapter.operation_id(did)
             and send_plan.route.route_id == binding.route.thread_id
             and send_plan.description.target == send_plan.route.target()
             and send_plan.description.content == TeamsDeliveryAdapter._body(SimpleNamespace(), payload, send_plan.route),
             "Teams send plan does not describe original exact cancellation")
    native_keys = (
        op.control_key("teams-native-current", config.channels.personal.configuration_digest),
        op.control_key("teams-native-evidence", config.channels.native_evidence_id),
        installation_record(config)[0],
        op.control_key("teams-personal-route", config.channels.personal.configuration_digest),
        op.control_key("teams-capture-route-binding", config.channels.personal.configuration_digest),
        channel_key("current", config.table.scope.authority_domain), channel_key("revocation", config.sha256),
    )
    if cleanup:
        native_keys = (*native_keys, authkey)
    prepare_id = op.control_key("teams-prepare", did)
    prepare = view.receipts.get(prepare_id)
    send_keys = (outkey, obkey, ownerkey, op.control_key("delivery-conversation", owner.conversation_id),
                 op.control_key("teams-route", config.channels.route_id), acl)
    _require(prepare is not None and prepare.request == ControlRequest(
        request_id=prepare_id, request_digest=digest(send_plan.model_dump(mode="json")),
        read_set=(*send_keys, send_plan_key, *native_keys)),
             "Teams original prepare request/read set differs")
    view.receipt(prepare.request, (send_plan_key,), exact={send_plan_key: view.hash(send_plan_key)})
    selected.add(send_plan_key)
    contract = op.AdapterContract(adapter_id="teams-connector", adapter_version=TEAMS_VERSION,
        allowed_kinds=("request",), allowed_methods=("POST",), allowed_targets=(send_plan.route.target(),),
        reservation_units=1, evidence_source="teams-http", outcome_contract_version=TEAMS_VERSION)
    operations = SimpleNamespace(_enabled=lambda: None, _contract_digest=lambda: digest(contract.model_dump(mode="json")))
    def operation_request(command):
        return op.ToolBrokerCore.reconciliation_request(operations, command, owner)
    opkey = op.control_key("operation", send_plan.operation_id)
    if opkey in view.inventory.rows:
        operation = view.row(opkey, op._Operation, material=send_plan.description.model_dump(mode="json"))
        _require(operation.operation_id == send_plan.operation_id and operation.owner_record_id == owner.owner_record_id
                 and operation.description_digest == send_plan.description_digest
                 and operation.adapter_contract_digest == digest(contract.model_dump(mode="json")),
                 "Teams sender operation changed")
        req = operation_request(op.ToolIntent(operation_id=send_plan.operation_id, input_ref=did))
        view.receipt(req, (opkey,))
        selected.add(opkey)
    grantid = channel_key("continuation-send-grant" if continuation is not None else "cleanup-send-grant" if cleanup else "send-grant", did)
    send_authorization = continuation if continuation is not None else authorization
    grantkey = op.control_key("grant", grantid)
    if grantkey in view.inventory.rows:
        grant = view.row(grantkey, op.GrantRecord)
        _require(grant.grant_id == grantid and grant.policy_id == owner.policy_id
                 and grant.root_budget_id == owner.root_budget_id
                 and grant.description_digest == send_plan.description_digest
                 and grant.expires_at == until and grant.remaining_uses in {0, 1}
                 and (not cleanup or grant.approval_id == send_authorization.decision_id),
                 "original finite send grant differs")
        req = ControlRequest(request_id=channel_key("cleanup-authorize-send" if cleanup else "authorize-send", did),
            request_digest=digest([did, send_plan.description_digest,
                digest(send_authorization.model_dump(mode="json")) if cleanup else digest(config.channels.model_dump(mode="json"))]),
            read_set=(*gates, grantkey))
        view.receipt(req, (grantkey,))
        selected.add(grantkey)
    sendkey = op.control_key("teams-send", did)
    if sendkey not in view.inventory.rows:
        return selected
    send = view.row(sendkey, _Send)
    expected_owner = TeamsSendContext(context=owner, grant_id=grantid)
    req = ControlRequest(request_id=op.control_key("teams-claim", did),
        request_digest=digest([send_plan.description_digest, expected_owner.model_dump(mode="json")]), read_set=(sendkey,))
    _require(send.delivery_id == did and send.operation_id == send_plan.operation_id and send.owner == expected_owner
             and send.description_digest == send_plan.description_digest
             and send.attempt_id == op.control_key("teams-attempt", did) and send.claim_request == req,
             "original send claim differs")
    view.receipt(req, (sendkey,), result={"status": "claimed_unknown", "send_permission": False})
    selected.add(sendkey)
    auth = op.AuthorizationRequest(operation_id=send.operation_id, attempt_id=send.attempt_id, grant_id=grantid)
    authreq = operation_request(auth)
    attemptkey = op.control_key("attempt", send.operation_id, send.attempt_id)
    if attemptkey in view.inventory.rows:
        attempt = view.row(attemptkey, op._Attempt)
        _require(attempt.context == owner and attempt.binding.operation_id == send.operation_id
                 and attempt.binding.attempt_id == send.attempt_id
                 and attempt.binding.description_digest == send_plan.description_digest
                 and attempt.binding.target == send_plan.description.target and attempt.reserved_units == 1,
                 "original Teams attempt differs")
        view.receipt(authreq, (opkey, attemptkey, grantkey, op.control_key("budget", owner.root_budget_id)))
        selected.add(attemptkey)
    if send.phase == "consumed":
        req = send.gate_request
        _require(req is not None and req.request_id == op.control_key("teams-consume", did)
                 and req.request_digest == digest([authreq.model_dump(mode="json"), send_plan.model_dump(mode="json")])
                 and req.read_set == tuple(sorted(set((*authreq.read_set, *send_keys, sendkey, send_plan_key, *native_keys))))
                 and send.gated_at is not None and send.gated_at < send.not_after <= until,
                 "original finite Teams consumed gate differs")
        _require(send_authorization.approved_at <= send.gated_at < send_authorization.expires_at,
                 "original Teams send is outside the independent recovery approval")
        view.receipt(req, (sendkey,), exact={sendkey: view.hash(sendkey)},
            result={"status": "delivery_unknown", "send_permission": False, "send_digest": digest(send.model_dump(mode="json"))})
        evidence = view.row(native_keys[1], TeamsNativeEvidence, material=True)
        def unavailable(*_):
            raise Problem(403, "TEAMS_RECOVERY_HISTORY_INVALID", "historical verifier cannot fetch or dispatch")
        registration = TeamsNativeRegistration(
            personal=config.channels.personal, server_configuration_sha256=config.sha256,
            installation_row_id=native_keys[2], evidence_id=config.channels.native_evidence_id,
            verify_current=unavailable, require_current=unavailable)
        reader = SimpleNamespace(
            core=SimpleNamespace(contract=config.delivery.model_copy(update={"channel": config.channels.channel}),
                                 configuration_digest=lambda: core_digest),
            settings=TeamsSettings(tenant_id=config.channels.personal.tenant_id,
                bot_app_id=config.channels.personal.uami_client_id, team_id=config.identity.team_id,
                routes=(send_plan.route,)), clock=lambda: evidence.observed_at if cleanup else send.gated_at)
        TeamsNativeGate(reader, registration)._validate(evidence, view.inventory.rows)
        _require(send.native_evidence_id == evidence.evidence_id
                 and send.native_evidence_digest == digest(evidence.model_dump(mode="json"))
                 and send.native_installation_data_sha256 == evidence.installation_data_sha256,
                 "consumed original Teams gate lost its exact native provenance")
    providerkey = op.control_key("teams-provider", did)
    if providerkey not in view.inventory.rows:
        return selected
    proof = view.row(providerkey, _ProviderReceipt, material=True)
    _require(send.phase == "consumed" and proof.binding == out.binding and proof.operation_id == send.operation_id
             and proof.attempt_id == send.attempt_id and proof.request_id == send.gate_request.request_id
             and proof.description_digest == send_plan.description_digest and proof.target == send_plan.description.target
             and proof.body_digest == hashlib.sha256(send_plan.description.content).hexdigest()
             and send.gated_at <= proof.observed_at < until
             and proof.observed_at < send_authorization.expires_at
             and proof.evidence_mode == "connector_http"
             and proof.native_evidence_id == send.native_evidence_id
             and proof.native_evidence_digest == send.native_evidence_digest,
             "original provider receipt differs from consumed send")
    req = ControlRequest(request_id=op.control_key("teams-provider-receipt", did),
                         request_digest=digest(proof.model_dump(mode="json")), read_set=(providerkey,))
    view.receipt(req, (providerkey,), exact={providerkey: view.hash(providerkey)})
    selected.add(providerkey)
    outcome = operation_request(op.OutcomeRequest(operation_id=send.operation_id, attempt_id=send.attempt_id, evidence_id=did))
    if outcome.request_id in view.receipts:
        view.receipt(outcome, (opkey, attemptkey, outcome.request_id), exact={outcome.request_id: view.hash(outcome.request_id)})
        selected.add(outcome.request_id)
    confirmation = delivery_request(d.ReceiptRequest(obligation_id=obid, delivery_id=did, evidence_id=did))
    if confirmation.request_id in view.receipts:
        receiptkey = op.control_key("delivery-receipt", did, did)
        unknown = continuation is not None and proof.provider_activity_id is None
        writes = ((obkey, outkey, receiptkey) if unknown else
                  (*quotas, op.control_key("delivery-index", owner.root_budget_id), obkey, outkey, receiptkey))
        view.receipt(confirmation, writes)
        selected.add(receiptkey)
        from .behavior_migration import _DeliveryReceipt
        expected = TeamsDeliveryAdapter.verify_receipt(SimpleNamespace(
            _provider=lambda _: (send_plan, send, proof), _core=lambda:SimpleNamespace(contract=config.delivery)), did)
        actual = view.row(receiptkey, _DeliveryReceipt, material=expected.model_dump(mode="json"))
        _require(actual.evidence == expected, "original delivery receipt differs from provider evidence")
        if unknown:
            _require(proof.problem_code is not None and ob.fulfillment == "pending"
                     and ob.delivery_state == "delivery_unknown" and ob.fulfillment_receipt_id is None
                     and not ob.confirmed_levels and out.send_outcome == "unknown" and not out.confirmed_levels,
                     "unknown original continuation send cannot become positive receipt")
        else:
            _require(proof.http_status in (200, 201) and proof.provider_activity_id is not None and proof.problem_code is None
                     and ob.fulfillment == "fulfilled" and ob.confirmed_levels == ("provider_accepted",),
                     "positive original Teams receipt is required for fulfillment")
    return selected
