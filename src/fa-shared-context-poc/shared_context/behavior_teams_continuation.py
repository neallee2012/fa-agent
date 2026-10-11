"""Delivery-only continuation of one already stopped immutable failure result."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Annotated, Literal
import hashlib

from pydantic import Field, field_validator, model_validator
from .behavior_delivery import _Model, _utc, _material, _Work, _Obligation, _parse
from .behavior_store import Digest, ControlRequest, ControlChanges, ControlWrite, GUARD_KEY
from .models import Identifier
from .behavior_auth import BehaviorPrincipal
from .behavior_recovery import (
    ExpiredTeamsCleanupScope, ExpiredTeamsCleanupAuthorization, ExpiredTeamsCleanupHistory,
    recovery_key, denied, _cancelled_result,
)
from .behavior_recovery_history import _Inventory, _multi_anchor
from .behavior_teams_recovery import configured_work, original_teams_state
from .behavior_teams_cleanup import retained_native
from .behavior_migration import code_sha256
from .behavior_operations import control_key
from .behavior_channels_migration import channel_key
from .behavior_workers import worker_key, JobRecord
from .behavior_foundry import native_key, _NativeLaunch
from .teams_identity import TeamsRouteRecord
from .teams_delivery import TeamsDeliveryAdapter
from .source_materials import digest
from .errors import Problem


def continuation_key(delivery_id):
    return control_key("original-failure-delivery-continuation", delivery_id)


def continuation_grant(delivery_id):
    return channel_key("continuation-send-grant", delivery_id)


class DeliveryContinuationScope(_Model):
    schema_version: Literal["fa-original-failure-delivery-scope-v1"] = "fa-original-failure-delivery-scope-v1"
    purpose: Literal["deliver_original_confirmed_failure_only"]
    continuation_id: Identifier
    configuration_sha256: Digest
    operator_oid: Identifier
    event_id: Digest
    job_id: Identifier
    launch_id: Identifier
    original_recovery_id: Identifier
    original_scope_sha256: Digest
    original_authorization_sha256: Digest
    original_code_sha256: Digest
    delivery_code_sha256: Digest
    observation_sha256: Digest
    not_before: datetime
    expires_at: datetime
    _times = field_validator("not_before", "expires_at")(_utc)

    @model_validator(mode="after")
    def finite(self):
        if not self.not_before < self.expires_at:
            raise ValueError("delivery continuation requires a finite interval")
        return self


class DeliveryContinuationObservation(_Model):
    schema_version: Literal["fa-original-failure-delivery-observation-v1"] = "fa-original-failure-delivery-observation-v1"
    purpose: Literal["deliver_original_confirmed_failure_only"]
    evidence_id: Identifier
    observer_oid: Identifier
    configuration_sha256: Digest
    delivery_code_sha256: Digest
    original_authorization_sha256: Digest
    inventory_sha256: Digest
    guard_revision: Annotated[int, Field(ge=0)]
    original_native_evidence_sha256: Digest
    original_platform_proof_sha256: Digest
    original_result_sha256: Digest
    route_sha256: Digest
    payload_sha256: Digest
    body_sha256: Digest
    protection_sha256: Digest
    deployment_sha256: Digest
    observed_at: datetime
    expires_at: datetime
    proof: Annotated[bytes, Field(min_length=1, max_length=2048)]
    _times = field_validator("observed_at", "expires_at")(_utc)


class DeliveryContinuationPlan(_Model):
    schema_version: Literal["fa-original-failure-delivery-plan-v1"] = "fa-original-failure-delivery-plan-v1"
    purpose: Literal["deliver_original_confirmed_failure_only"]
    scope: DeliveryContinuationScope
    original_scope: ExpiredTeamsCleanupScope
    observation: DeliveryContinuationObservation
    delivery_id: Digest
    original_rows: Annotated[dict[Identifier, Digest], Field(min_length=1, max_length=48)]
    original_receipts: Annotated[dict[Identifier, Digest], Field(min_length=1, max_length=32)]
    inventory_sha256: Digest
    inventory_guard_revision: Annotated[int, Field(ge=0)]
    inventory_guard_sha256: Digest
    inventory_guard_etag: str
    original_job_sha256: Digest

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))


class DeliveryContinuationAuthorization(_Model):
    schema_version: Literal["fa-original-failure-delivery-authorization-v1"] = "fa-original-failure-delivery-authorization-v1"
    purpose: Literal["deliver_original_confirmed_failure_only"]
    plan: DeliveryContinuationPlan
    decision_id: Identifier
    approved_at: datetime
    expires_at: datetime
    _times = field_validator("approved_at", "expires_at")(_utc)


def original_authorization(view, scope):
    return view.row(recovery_key("authorization", scope.original_recovery_id),
                    ExpiredTeamsCleanupAuthorization, material=True)


def _original(view, scope, old_scope):
    config = view.config
    old = original_authorization(view, scope)
    plan = old.plan
    if (digest(old.model_dump(mode="json")) != scope.original_authorization_sha256
            or digest(old_scope.model_dump(mode="json")) != scope.original_scope_sha256
            or old_scope.recovery_id != scope.original_recovery_id
            or old_scope.configuration_sha256 != scope.configuration_sha256
            or plan.configuration_sha256 != config.sha256 or scope.configuration_sha256 != config.sha256
            or plan.code_sha256 != scope.original_code_sha256
            or old_scope.event_id != scope.event_id or plan.event_id != scope.event_id
            or old_scope.job_id != scope.job_id or plan.binding.job_id != scope.job_id
            or old_scope.launch_id != scope.launch_id or plan.launch_id != scope.launch_id
            or old_scope.operator_oid != scope.operator_oid or scope.operator_oid != config.identity.human_oid
            or old_scope.not_before != plan.not_before or old_scope.expires_at != plan.cleanup_expires_at
            or old_scope.observation_sha256 != digest(plan.observation.model_dump(mode="json"))
            or not old.approved_at <= scope.not_before < scope.expires_at <= old.expires_at):
        raise denied("DELIVERY_CONTINUATION_SCOPE_INVALID", "continuation is not the exact original approval and deadline")
    launch = view.row(native_key("launch", scope.launch_id), _NativeLaunch)
    registered, _, rows, receipts = original_teams_state(view.inventory, config, launch)
    proof_key = native_key("platform-stop", scope.launch_id)
    proof = view.row(proof_key, ExpiredTeamsCleanupHistory, material=True)
    job = view.row(worker_key("job", scope.job_id), JobRecord)
    b = registered.binding
    result = _cancelled_result(SimpleNamespace(verifier_version=config.delivery.verifier_version), launch, proof)
    workkey = control_key("delivery-work-state", b.obligation_id)
    work = view.row(workkey, _Work, material=result.model_dump(mode="json"))
    if (proof.intent.authorization != old or proof.observation.observed_at > scope.not_before or launch.phase != "sealed"
            or job.state != "cancelled" or not job.result_forwarded or job.completion_credit
            or work.result != result or result.state != "cancelled"
            or result.known_effects or result.unresolved_operations or result.artifacts):
        raise denied("DELIVERY_CONTINUATION_RESULT_INVALID", "original positive stop and forwarded exact failure required")
    for rid in (recovery_key("authorization", scope.original_recovery_id),
                recovery_key("stop-intent", scope.original_recovery_id),
                recovery_key("stop-observation", scope.original_recovery_id), proof_key,
                native_key("evidence", launch.sealed_evidence_id),
                worker_key("completion", scope.job_id, str(job.generation)),
                worker_key("work-result", result.evidence_id), worker_key("result-forward", scope.job_id), workkey):
        rows[rid] = view.hash(rid)
    for rid, receipt in view.receipts.items():
        if plan.inventory_guard_revision < receipt.guard_revision <= view.guard.revision:
            if any(key in rows for key, _ in receipt.writes):
                receipts[rid] = digest(view.inventory.receipts[rid].payload)
    route = view.row(control_key("teams-route", config.channels.route_id), TeamsRouteRecord, material=True)
    from .behavior_teams_recovery_history import failure_payload
    payload = failure_payload(b, result)
    body = TeamsDeliveryAdapter._body(SimpleNamespace(), payload, route)
    facts = {
        "original_platform_proof_sha256": digest(proof.model_dump(mode="json")),
        "original_result_sha256": _material(result.model_dump(mode="json")).sha256,
        "route_sha256": digest(route.model_dump(mode="json")),
        "payload_sha256": _material(payload.model_dump(mode="json")).sha256,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "original_native_evidence_sha256": digest(retained_native(view, config).model_dump(mode="json")),
    }
    return old, b, result, rows, receipts, facts


def _no_send(inventory, binding):
    did = control_key("delivery-final-id", binding.obligation_id)
    operation = TeamsDeliveryAdapter.operation_id(did)
    keys = (
        control_key("delivery-consumer-plan", binding.obligation_id),
        control_key("delivery-outbox", did), control_key("teams-send-plan", did),
        control_key("teams-send", did), control_key("teams-provider", did),
        control_key("operation", operation), control_key("attempt", operation, control_key("teams-attempt", did)),
        control_key("grant", continuation_grant(did)),
    )
    from .behavior_teams_cleanup import cleanup_grant
    keys = (*keys, control_key("grant", cleanup_grant(did)))
    ob = inventory.rows[control_key("delivery-obligation", binding.obligation_id)].payload["data"]
    if any(k in inventory.rows for k in keys) or ob["final_delivery_id"] is not None or ob["fulfillment"] != "pending":
        raise denied("DELIVERY_CONTINUATION_ALREADY_STARTED", "original delivery preparation/send exists; reconcile only")
    return did


def validate_continuation(view, original):
    """One explicit typed continuation addition; preserve the original history."""
    candidates = [(rid, row) for rid, row in view.inventory.rows.items()
                  if row.payload["data"].get("schema_version") == "fa-original-failure-delivery-authorization-v1"]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise denied("DELIVERY_CONTINUATION_HISTORY_INVALID", "only one continuation for this original delivery is supported")
    rid, _ = candidates[0]
    auth = view.row(rid, DeliveryContinuationAuthorization, material=True)
    p, scope = auth.plan, auth.plan.scope
    old, binding, _, rows, receipts, facts = _original(view, scope, p.original_scope)
    if (old != original or rid != continuation_key(p.delivery_id)
            or p.delivery_id != control_key("delivery-final-id", binding.obligation_id)
            or rows != p.original_rows
            or {k:v for k,v in receipts.items() if view.receipts[k].guard_revision <= p.inventory_guard_revision} != p.original_receipts
            or not scope.not_before <= auth.approved_at < auth.expires_at <= scope.expires_at
            or digest(p.observation.model_dump(mode="json")) != scope.observation_sha256):
        raise denied("DELIVERY_CONTINUATION_HISTORY_INVALID", "exact original continuation references differ")
    _validate_observation(view.config, p, facts)
    request = ControlRequest(request_id=rid, request_digest=digest(auth.model_dump(mode="json")))
    receipt = view.receipt(request, (rid,), exact={rid: view.hash(rid)}, result={"continuation_authorization": rid})
    if receipt.guard_revision != p.inventory_guard_revision+1:
        raise denied("DELIVERY_CONTINUATION_HISTORY_INVALID", "continuation approval did not commit at original snapshot")
    anchor = SimpleNamespace(**{k:getattr(p,k) for k in (
        "inventory_guard_revision","inventory_guard_sha256","inventory_sha256","original_job_sha256")},
        configuration_sha256=scope.configuration_sha256, binding=old.plan.binding)
    _multi_anchor(view, SimpleNamespace(plan=anchor))
    return auth


def _validate_observation(config, plan, facts):
    o, s = plan.observation, plan.scope
    if (o.observer_oid != config.evidence.observer_oid or o.configuration_sha256 != config.sha256
            or o.delivery_code_sha256 != s.delivery_code_sha256
            or o.original_authorization_sha256 != s.original_authorization_sha256
            or o.inventory_sha256 != plan.inventory_sha256 or o.guard_revision != plan.inventory_guard_revision
            or any(getattr(o,k) != value for k,value in facts.items())
            or not o.observed_at <= s.not_before < s.expires_at <= o.expires_at
            or s.not_before-o.observed_at > timedelta(minutes=5)):
        raise denied("DELIVERY_CONTINUATION_OBSERVATION_INVALID", "fresh original-result-only observation differs")


class OriginalFailureDeliveryContinuation:
    """No executor/stop entry or callback exists on this authority."""
    def __init__(self, composition, scope, *, original_scope, observation, current_observation):
        if (type(scope) is not DeliveryContinuationScope or type(original_scope) is not ExpiredTeamsCleanupScope
                or type(observation) is not DeliveryContinuationObservation or not callable(current_observation)):
            raise denied("DELIVERY_CONTINUATION_SCOPE_INVALID", "explicit typed original delivery scope required")
        if digest(observation.model_dump(mode="json")) != scope.observation_sha256:
            raise denied("DELIVERY_CONTINUATION_OBSERVATION_INVALID", "trusted observation does not match scope")
        self.composition, self.config, self.scope = composition, composition.config, scope
        self.original_scope, self.observation, self.current_observation = original_scope, observation, current_observation
        self.teams, self.store = composition.teams, composition.runtime.store
        self.workers, self.native = composition.runtime.dep.workers, composition.runtime.dep.native_launches
        self.clock = composition.runtime.clock

    def _time(self, authorization=None):
        upper = min(self.scope.expires_at, self.observation.expires_at,
                    self.scope.expires_at if authorization is None else authorization.expires_at)
        lower = self.scope.not_before if authorization is None else max(self.scope.not_before,authorization.approved_at)
        if not lower <= self.teams.runtime._now() < upper:
            raise Problem(403, "DELIVERY_CONTINUATION_EXPIRED", "original hard delivery deadline is not current")

    def _operator(self, principal, *, historical=False):
        if (not isinstance(principal,BehaviorPrincipal) or principal.kind!="human"
                or principal.oid!=self.config.identity.human_oid or principal.oid!=self.scope.operator_oid
                or principal.tenant_id!=self.config.identity.tenant_id):
            raise denied("DELIVERY_CONTINUATION_OPERATOR_DENIED", "original exact human required")
        principal.require(self.config.identity.team_id,"read")
        if not historical:
            principal.require(self.config.identity.team_id,"approve")
            if code_sha256()!=self.scope.delivery_code_sha256 or self.current_observation()!=self.observation:
                raise denied("DELIVERY_CONTINUATION_OBSERVATION_INVALID", "current delivery code/observer changed")
            self._time()

    def preview(self, principal):
        self._operator(principal)
        self.composition.installation()
        inventory = self.store.read_inventory()
        view = _Inventory(inventory,self.config)
        old,binding,_,rows,receipts,facts = _original(view,self.scope,self.original_scope)
        did = _no_send(inventory,binding)
        if continuation_key(did) in inventory.rows:
            raise denied("DELIVERY_CONTINUATION_ALREADY_STARTED", "original continuation exists; reconcile only")
        if self.teams.runtime._now()-self.observation.observed_at>timedelta(minutes=5):
            raise denied("DELIVERY_CONTINUATION_OBSERVATION_INVALID", "preview requires fresh current observation")
        plan=DeliveryContinuationPlan(scope=self.scope,original_scope=self.original_scope,observation=self.observation,
            delivery_id=did,original_rows=rows,original_receipts=receipts,
            inventory_sha256=digest(dict(inventory.entity_sha256)),inventory_guard_revision=view.guard.revision,
            inventory_guard_sha256=inventory.entity_sha256[GUARD_KEY],inventory_guard_etag=inventory.guard.etag,
            original_job_sha256=view.hash(worker_key("job",binding.job_id)),purpose=self.scope.purpose)
        _validate_observation(self.config,plan,facts)
        self._operator(principal)
        if self.store.read_inventory().entity_sha256!=inventory.entity_sha256:
            raise denied("DELIVERY_CONTINUATION_STALE", "original delivery preview changed")
        if self.teams.runtime._now()-self.observation.observed_at>timedelta(minutes=5):
            raise denied("DELIVERY_CONTINUATION_OBSERVATION_INVALID", "observation expired during preview verification")
        self._time()
        return plan

    def authorize(self, principal, plan, decision_id):
        self._operator(principal)
        rid=continuation_key(plan.delivery_id)
        prior=self.store.read_snapshot((rid,))[rid]
        if prior is not None:
            saved=_parse(prior.payload["data"],DeliveryContinuationAuthorization)
            if saved.plan!=plan or saved.decision_id!=decision_id:
                raise denied("DELIVERY_CONTINUATION_AUTHORIZATION_INVALID", "original decision differs")
            self._authorization(principal,saved)
            return saved
        if plan!=self.preview(principal):
            raise denied("DELIVERY_CONTINUATION_STALE", "exact original plan changed")
        value=DeliveryContinuationAuthorization(plan=plan,decision_id=decision_id,approved_at=self.teams.runtime._now(),
            expires_at=self.scope.expires_at,purpose=self.scope.purpose)
        inventory=self.store.read_inventory()
        if digest(dict(inventory.entity_sha256))!=plan.inventory_sha256:
            raise denied("DELIVERY_CONTINUATION_STALE", "authority changed before original continuation approval")
        def final():
            self._operator(principal)
            self.composition.installation()
            if self.store.read_inventory().entity_sha256!=inventory.entity_sha256:
                raise denied("DELIVERY_CONTINUATION_STALE", "authority changed at approval commit")
            if self.teams.runtime._now()-self.observation.observed_at>timedelta(minutes=5):
                raise denied("DELIVERY_CONTINUATION_OBSERVATION_INVALID", "initial approval observation is no longer fresh")
            self._time(value)
        request=ControlRequest(request_id=rid,request_digest=digest(value.model_dump(mode="json")))
        self.store.transact(request,lambda _:ControlChanges(writes=(ControlWrite(row_id=rid,mode="create",
            data=value.model_dump(mode="json"),material=_material(value.model_dump(mode="json"))),),
            result={"continuation_authorization":rid}),expected_guard=inventory.guard,before_send=final)
        return value

    def _authorization(self, principal, authorization, *, historical=False):
        self._operator(principal,historical=historical)
        if type(authorization) is not DeliveryContinuationAuthorization or authorization.plan.scope!=self.scope:
            raise denied("DELIVERY_CONTINUATION_AUTHORIZATION_INVALID", "exact delivery-only authorization required")
        self.composition.installation()
        view=_Inventory(self.store.read_inventory(),self.config)
        old=original_authorization(view,self.scope)
        if validate_continuation(view,old)!=authorization:
            raise denied("DELIVERY_CONTINUATION_AUTHORIZATION_INVALID", "stored continuation differs")
        if not historical:
            self._time(authorization)

    def original(self):
        return original_authorization(_Inventory(self.store.read_inventory(),self.config),self.scope)

    def _write_gate(self, principal, authorization):
        self._authorization(principal,authorization)
        self._operator(principal)
        self._time(authorization)

    def deliver(self, principal, authorization):
        from .behavior_teams_cleanup import CleanupDelivery
        self._authorization(principal,authorization)
        authority=CleanupDelivery(self,principal,authorization)
        return self.store.validated_writes(authority.run,lambda:self._write_gate(principal,authorization))

    def reconcile(self, principal, authorization):
        self._authorization(principal,authorization,historical=True)
        did=authorization.plan.delivery_id
        rows=self.store.read_snapshot(tuple(control_key(kind,did) for kind in (
            "teams-send","teams-provider","delivery-outbox")))
        obkey=control_key("delivery-obligation",configured_work(self.config,self.scope.event_id)[0].obligation_id)
        obligation=self.store.read_snapshot((obkey,))[obkey].payload["data"]
        return {"continuation_id":self.scope.continuation_id,"delivery_id":did,
            "send_present":rows[control_key("teams-send",did)] is not None,
            "provider_receipt_present":rows[control_key("teams-provider",did)] is not None,
            "fulfillment":obligation["fulfillment"],"confirmation":obligation["confirmed_levels"],"dispatch_performed":False}
