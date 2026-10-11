"""Independent, bounded retained terminal-result settlement. OFFLINE-first.

Server-only composition:
  RetainedResultSettlement(original_configuration, existing_runtime, registration)
  inbox.register_retained_results(service)
  service.prepare(principal) -> SettlementPlan (metadata only)
  service.authorize(principal, AuthorizeSettlement) -> SettlementAuthorization
  service.fetch / acknowledge / reconcile / revoke

No default registration, evidence acquisition, bootstrap, supervisor, source
read, model call, native stop, or outbound sender exists here. The optional
standalone API is NOT installed into the ordinary application. A verifier must
independently observe the exact receiver and retained inline material in the
original authority. Worker/pool readiness, Blob REST access and local filesystem
persistence are not such evidence. Native observation acquisition is NOT
implemented. An offline witness cannot authorize native use.

One server-installed settlement_id permits one immutable plan/authorization and
one irreversible revocation; no renewal/reset endpoint. The installation window
is at most one hour and each explicit HUMAN grant at most 15 minutes. Fetches
share PullInbox's eight-attempt bound and ACK lane. Reconciliation never fetches
bytes or invents ACK. Expired/revoked grants cannot be used even for ACK replay;
a subsequent human-authorized installation can reconcile the same saved IDs.

prepare only uses DeliveryConsumer's deterministic final preparation and returns
no payload. It may leave an outbox if verification/commit subsequently fails;
that is not delivery or a read grant. All payload release and ACK persistence
recheck the grant, revocation, current owner/epoch, operation PolicyRecord,
original RuntimePolicy bytes, terminal job/result and exact payload in the inbox
EGT. Only RuntimePolicy.valid_until is NOT a settlement read grant or deadline.
It is never changed. Old policies, budgets, grants and receipts are retained.

Protection is deliberately limited to the two existing inline result materials,
the exact final outbox, and this authority's durable exchange/receipt lane. No
artifact Blob dereference, source read, GC, UI, human-read or Teams claim follows.
UTC effectiveness is the existing server-clock contract, not Table commit time.
An already committed authenticated ACK remains a historical fact if revocation
wins later receipt reconciliation; it is never erased or downgraded. Public
reconciliation still checks current authorization before and after read-back.

New create-only keys use settlement_key("plan" | "authorization" | "revocation",
settlement_id). Their data kinds are retained_result_settlement_plan,
retained_result_settlement_authorization and retained_result_settlement_revocation.
Access handles create no separate row; the existing inbox retains their evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal, Protocol

from pydantic import Field, field_validator

from .behavior_auth import BehaviorPrincipal
from .behavior_delivery import (
    Content, Count, DeliveryBinding, DeliveryConsumer, VerifiedWorkResult,
    _Model, _Outbox, _Payload, _check, _digest, _material, _parse, _utc,
)
from .behavior_foundry import _NativeLaunch, native_key
from .behavior_inbox import InboxAcknowledgment, InboxFetch, VerifiedInboxAccess
from .behavior_operations import BudgetRecord, ExecutionContext, OwnerRecord, PolicyRecord, control_key
from .behavior_runtime import BehaviorRuntime, RuntimePolicy, runtime_key
from .behavior_server_config import ServerConfiguration
from .behavior_store import ControlChanges, ControlRequest, Digest, InlineMaterial
from .behavior_workers import ExecutorEvidence, JobRecord, worker_key
from .errors import Problem
from .models import Identifier


def settlement_key(kind: str, settlement_id: str) -> str:
    return control_key("retained-settlement-" + kind, settlement_id)


class RetainedMaterial(_Model):
    row_id: Identifier
    sha256: Digest


class SettlementPlan(_Model):
    kind: Literal["retained_result_settlement_plan"] = "retained_result_settlement_plan"
    settlement_id: Identifier
    configuration_sha256: Digest
    control_binding: Digest
    runtime_binding_digest: Digest
    delivery_configuration_digest: Digest
    worker_configuration_digest: Digest
    binding_id: Identifier
    job_id: Identifier
    job_sha256: Digest
    work_id: Identifier
    work_revision: Literal[1] = 1
    terminal_state: Literal["succeeded", "failed", "cancelled"]
    context: ExecutionContext
    operation_policy_revision: Count
    runtime_policy_sha256: Digest
    result_evidence_id: Identifier
    result_sha256: Digest
    delivery: DeliveryBinding
    retained_materials: Annotated[tuple[RetainedMaterial, ...], Field(min_length=3, max_length=3)]
    launch_row_id: Identifier | None = None
    launch_sha256: Digest | None = None
    completion_row_id: Identifier | None = None
    completion_sha256: Digest | None = None

    @property
    def sha256(self):
        return _digest(self.model_dump(mode="json"))


class RetainedPayloadProtection(_Model):
    """Returned only by the installed verifier, never accepted from HTTP."""

    evidence_id: Identifier
    verifier_version: Identifier
    evidence_mode: Literal["offline_contract", "native"]
    configuration_sha256: Digest
    control_binding: Digest
    plan_sha256: Digest
    authentication_version: Identifier
    retained_materials: Annotated[tuple[RetainedMaterial, ...], Field(min_length=3, max_length=3)]
    coverage: Literal["exact-inline-result-and-inbox-exchange-receipts"]
    receiver_scope: Literal["terminal-result-only"]
    observed_at: datetime
    expires_at: datetime
    proof: Content

    _times = field_validator("observed_at", "expires_at")(_utc)


class RetainedProtectionVerifier(Protocol):
    def verify(self, evidence_id: str, plan: SettlementPlan) -> RetainedPayloadProtection:
        """Verify pre-acquired independent protection/receiver evidence."""
        ...


@dataclass(frozen=True)
class SettlementRegistration:
    """Trusted installer inputs, NOT capabilities selected by a requesting user."""

    settlement_id: str
    binding_id: str
    configuration_sha256: str
    expires_at: datetime
    protection_evidence_id: str
    protection_verifier_version: str
    protection_verifier: RetainedProtectionVerifier


class AuthorizeSettlement(_Model):
    plan_sha256: Digest
    decision_id: Identifier
    decision: Literal["settle_retained_result"]


class SettlementAuthorization(_Model):
    kind: Literal["retained_result_settlement_authorization"] = "retained_result_settlement_authorization"
    authorization_id: Identifier
    plan: SettlementPlan
    decision: AuthorizeSettlement
    principal_id: Identifier
    approved_at: datetime
    expires_at: datetime
    protection: RetainedPayloadProtection

    _times = field_validator("approved_at", "expires_at")(_utc)


class SettlementUse(_Model):
    authorization_id: Identifier


class SettlementFetch(SettlementUse):
    request_id: Identifier


class SettlementAcknowledgment(SettlementFetch):
    exchange_id: Identifier
    acknowledgment_token: Identifier
    binding: DeliveryBinding
    confirmation_level: Literal["client_received"] = "client_received"


class RevokeSettlement(SettlementUse):
    decision_id: Identifier


class _Revocation(_Model):
    kind: Literal["retained_result_settlement_revocation"] = "retained_result_settlement_revocation"
    command: RevokeSettlement
    principal_id: Identifier
    authorization_sha256: Digest
    revoked_at: datetime

    _time = field_validator("revoked_at")(_utc)


@dataclass(frozen=True)
class _RetainedAccess:
    service: RetainedResultSettlement
    principal: BehaviorPrincipal
    authorization: SettlementAuthorization
    access: VerifiedInboxAccess
    request_id: str
    read_set: tuple[str, ...]


class RetainedResultSettlement:
    def __init__(self, configuration: ServerConfiguration, runtime: BehaviorRuntime,
                 registration: SettlementRegistration | None = None):
        self.config, self.runtime, self.registration = configuration, runtime, registration
        self.store, self.core, self.inbox = runtime.store, runtime.dep.delivery, runtime.dep.inbox
        self.workers = runtime.dep.workers
        self.configuration_sha256 = configuration.sha256
        self.binding = None if registration is None else runtime.bindings.get(registration.binding_id)
        if registration is not None:
            self._installed()

    def _installed(self):
        r, b = self.registration, self.binding
        if (r is None or b is None or r.protection_verifier is None
                or r.binding_id != b.binding_id
                or self.config.sha256 != self.configuration_sha256
                or r.configuration_sha256 != self.configuration_sha256
                or self.config.delivery != self.core.contract
                or self.config.workers != self.workers.contract
                or self.config.table.scope != self.store._scope
                or self.core.contract.control_binding != self.store._binding
                or self.workers.contract.control_binding != self.store._binding
                or tuple(self.runtime.bindings.values()) != self.config.bindings
                or self.config.identity.human_oid != b.human_oid
                or self.config.identity.tenant_id != b.tenant_id or self.config.identity.team_id != b.team_id
                or self.config.authentication_version != self.inbox.registration.authentication_version
                or self.inbox.core is not self.core or self.inbox.store is not self.store
                or self.core.registration.channel_adapter is not self.inbox
                or self.workers.store is not self.store
                or self.runtime.mode != self.core.registration.mode or self.workers.mode != self.runtime.mode):
            raise Problem(403, "SETTLEMENT_DISABLED", "explicit original configuration and retained-result verifier are required")
        # Validate even server registrations; no malformed ID can select another namespace.
        from pydantic import TypeAdapter
        for value in (r.settlement_id, r.binding_id, r.protection_evidence_id, r.protection_verifier_version):
            TypeAdapter(Identifier).validate_python(value, strict=True)
        TypeAdapter(Digest).validate_python(r.configuration_sha256, strict=True)
        _utc(r.expires_at)

    def _operator(self, principal, action="read", *, current=True):
        self._installed()
        if self.inbox._retained_results is not self:
            raise Problem(403, "SETTLEMENT_DISABLED", "this retained-result verifier is not explicitly registered")
        b = self.binding
        if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human"
                or principal.actor_id is not None or principal.oid != b.human_oid
                or principal.tenant_id != b.tenant_id):
            raise Problem(403, "SETTLEMENT_HUMAN_REQUIRED", "the configured authenticated HUMAN is required")
        principal.require(b.team_id, "read")
        principal.require(b.team_id, action)
        now = self.runtime._now()
        if current and not now < self.registration.expires_at <= now + timedelta(hours=1):
            raise Problem(403, "SETTLEMENT_PERMISSION_EXPIRED", "the independently installed settlement window is closed")

    def _base_keys(self):
        b = self.binding
        return (*self.runtime._gate_keys(b),
                control_key("delivery-conversation", b.owner.conversation_id),
                control_key("delivery-obligation", b.obligation_id),
                control_key("delivery-work-state", b.obligation_id), worker_key("job", b.job_id))

    def _keys(self, plan=None):
        if plan is None:
            rows = self.store.read_snapshot(self._base_keys())
            _, _, work, job, _ = self._state(rows)
            delivery_id = self.core._obligation(rows, self.binding.obligation_id, self._context(rows)).final_delivery_id
            delivery_id = delivery_id or control_key("delivery-final-id", self.binding.obligation_id)
            result_id = work.result.evidence_id
            launch = (native_key("launch", native_key("launch-id", job.binding.start_request_id))
                      if self.runtime.mode == "native" and job.binding is not None else None)
            completion = worker_key("completion", self.binding.job_id, str(job.generation)) if launch is not None else None
        else:
            delivery_id, result_id, launch = plan.delivery.delivery_id, plan.result_evidence_id, plan.launch_row_id
            completion = plan.completion_row_id
        return tuple(dict.fromkeys((
            *self._base_keys(), worker_key("work-result", result_id),
            control_key("delivery-outbox", delivery_id),
            *(() if launch is None else (launch,)),
            *(() if completion is None else (completion,)),
            *(settlement_key(kind, self.registration.settlement_id) for kind in ("plan", "authorization", "revocation")),
        )))

    def _context(self, rows):
        b = self.binding
        owner = self.core._row(rows, control_key("owner", b.owner.owner_record_id), OwnerRecord)
        if (not owner.active or b.operation_id not in owner.operation_ids or any(
                getattr(owner, key) != getattr(b.owner, key)
                for key in ExecutionContext.model_fields if key != "epoch")):
            raise Problem(403, "SETTLEMENT_OWNER_DENIED", "current owner or configured scope differs")
        return ExecutionContext(**{key: getattr(owner, key) for key in ExecutionContext.model_fields})

    def _state(self, rows):
        b, core = self.binding, self.core
        context = self._context(rows)
        policy = core._row(rows, control_key("policy", context.policy_id), PolicyRecord)
        old = core._row(rows, runtime_key("policy", b.binding_id), RuntimePolicy)
        budget = core._row(rows, control_key("budget", context.root_budget_id), BudgetRecord)
        if (not policy.active or policy.policy_id != context.policy_id or not old.active
                or old.binding_digest != self.runtime.binding_digest(b)
                or old.operation_policy_revision != policy.revision
                or budget.root_budget_id != context.root_budget_id):
            raise Problem(403, "SETTLEMENT_POLICY_DENIED", "current operation policy or original runtime scope differs")
        ob = core._obligation(rows, b.obligation_id, context)
        work = core._work(rows, ob)
        job = self.workers._row(rows, worker_key("job", b.job_id), JobRecord)
        if (work.result is None or work.state not in {"succeeded", "failed", "cancelled"}
                or job.state != work.state or not job.result_forwarded or job.completion_credit != 0
                or job.children or job.spec.parent_job_id is not None
                or job.spec.job_id != b.job_id or job.spec.work_id != b.work.work_id
                or job.spec.obligation_id != b.obligation_id or job.spec.input != self.runtime.job_input(b)
                or work.definition != b.work or work.result.work_revision != 1
                or work.result.verifier_version != core.contract.verifier_version
                or ob.event_id != b.event_id or ob.root_request_id != b.root_request_id
                or ob.route != b.route or ob.required_confirmation_level != "client_received"
                or any(getattr(job.owner, key) != getattr(context, key)
                       for key in ExecutionContext.model_fields if key != "epoch")):
            raise Problem(403, "SETTLEMENT_RESULT_DENIED", "only the exact forwarded immutable terminal job/result is eligible")
        return context, ob, work, job, old

    def _describe(self, rows):
        context, ob, work, job, old = self._state(rows)
        result = work.result
        result_key = worker_key("work-result", result.evidence_id)
        if result_key not in rows:
            raise Problem(403, "SETTLEMENT_RESULT_DENIED", "the original result reference changed")
        retained = self.workers._row(rows, result_key, VerifiedWorkResult)
        result_material = _material(result.model_dump(mode="json"))
        if retained != result or _parse(rows[result_key].payload["material"], InlineMaterial) != result_material:
            raise Problem(403, "SETTLEMENT_RESULT_DENIED", "worker and delivery retained results differ")
        if ob.final_delivery_id is None:
            raise Problem(409, "SETTLEMENT_NOT_PREPARED", "no immutable terminal outbox is prepared")
        key = control_key("delivery-outbox", ob.final_delivery_id)
        if key not in rows:
            raise Problem(403, "SETTLEMENT_PAYLOAD_DENIED", "the original outbox reference changed")
        outbox = self.core._row(rows, key, _Outbox)
        material = _parse(rows[key].payload["material"], InlineMaterial)
        kind = "final" if work.state == "succeeded" else "failure"
        expected = _Payload(
            event_id=ob.event_id, root_request_id=ob.root_request_id, work_id=ob.work_id,
            message_kind=kind, work_state=work.state, result_evidence_id=result.evidence_id,
            content=result.content, artifacts=result.artifacts, known_effects=result.known_effects,
            unresolved_operations=result.unresolved_operations, responsibility_owner=ob.owner_record_id,
            notification_code="verified_result" if kind == "final" else "delivery_needs_attention",
        )
        if (outbox.message_kind != kind or outbox.binding.obligation_id != ob.obligation_id
                or outbox.binding.delivery_id != ob.final_delivery_id or outbox.binding.route != ob.route
                or outbox.binding.version != ob.final_version or ob.last_version != ob.final_version
                or outbox.binding.channel_version != ob.channel_version
                or outbox.binding.artifacts != result.artifacts
                or material != _material(expected.model_dump(mode="json"))
                or material.sha256 != outbox.binding.payload_digest):
            raise Problem(403, "SETTLEMENT_PAYLOAD_DENIED", "only the original exact terminal payload/version may be settled")
        launch_key, launch_digest, completion_key, completion_digest = None, None, None, None
        if self.runtime.mode == "native":
            if job.binding is None:
                raise Problem(403, "SETTLEMENT_RESULT_DENIED", "a sealed original native execution is required")
            launch_key = native_key("launch", native_key("launch-id", job.binding.start_request_id))
            if launch_key not in rows:
                raise Problem(403, "SETTLEMENT_RESULT_DENIED", "the original native execution changed")
            launch = self.workers._row(rows, launch_key, _NativeLaunch)
            completion_key = worker_key("completion", self.binding.job_id, str(job.generation))
            if completion_key not in rows:
                raise Problem(403, "SETTLEMENT_RESULT_DENIED", "the original completion reference changed")
            completion = self.workers._row(rows, completion_key, ExecutorEvidence)
            if (completion.binding != job.binding or completion.evidence_id != job.last_evidence_id
                    or completion.sequence != job.last_sequence or completion.sequence > launch.evidence_sequence
                    or completion.native_execution_id != launch.native_invocation_id
                    or completion.state != ("stopped" if job.state == "cancelled" else job.state)
                    or not completion.entire_tree_idle or not completion.all_inflight_starts_fenced
                    or completion.result != result
                    or _parse(rows[completion_key].payload["material"], InlineMaterial)
                    != _material(completion.model_dump(mode="json"))):
                raise Problem(403, "SETTLEMENT_RESULT_DENIED", "the retained completion does not prove this terminal result")
            if (launch.phase != "sealed" or launch.sealed_evidence_id is None
                    or launch.start.binding != job.binding
                    or job.last_evidence_id != launch.sealed_evidence_id
                    and job.last_evidence_id != native_key("terminal-verification-id", launch.launch_id, str(completion.sequence))
                    or launch.definition != work.definition or launch.api_binding_id != self.binding.binding_id
                    or launch.configuration_digest != self.workers.configuration_digest()):
                raise Problem(403, "SETTLEMENT_RESULT_DENIED", "native execution is not the sealed terminal original")
            launch_digest = _digest(launch.model_dump(mode="json"))
            completion_digest = _digest(completion.model_dump(mode="json"))
        return SettlementPlan(
            settlement_id=self.registration.settlement_id, configuration_sha256=self.configuration_sha256,
            control_binding=self.store._binding, runtime_binding_digest=self.runtime.binding_digest(self.binding),
            delivery_configuration_digest=self.core.configuration_digest(),
            worker_configuration_digest=self.workers.configuration_digest(), binding_id=self.binding.binding_id,
            job_id=self.binding.job_id, job_sha256=_digest(job.model_dump(mode="json")),
            work_id=work.definition.work_id, terminal_state=work.state, context=context,
            operation_policy_revision=old.operation_policy_revision,
            runtime_policy_sha256=_digest(old.model_dump(mode="json")), result_evidence_id=result.evidence_id,
            result_sha256=result_material.sha256, delivery=outbox.binding,
            retained_materials=(
                RetainedMaterial(row_id=control_key("delivery-work-state", ob.obligation_id), sha256=result_material.sha256),
                RetainedMaterial(row_id=result_key, sha256=result_material.sha256),
                RetainedMaterial(row_id=key, sha256=material.sha256),
            ), launch_row_id=launch_key, launch_sha256=launch_digest,
            completion_row_id=completion_key, completion_sha256=completion_digest,
        )

    def _saved(self, rows, kind, model):
        key = settlement_key(kind, self.registration.settlement_id)
        value = self.core._row(rows, key, model)
        if _parse(rows[key].payload["material"], InlineMaterial) != _material(value.model_dump(mode="json")):
            raise Problem(403, "SETTLEMENT_RECORD_CHANGED", "retained settlement record differs")
        return value

    def _protection(self, plan):
        r = self.registration
        try:
            proof = _check(r.protection_verifier.verify(r.protection_evidence_id, plan), RetainedPayloadProtection)
        except Exception:
            raise Problem(403, "SETTLEMENT_PROTECTION_DENIED", "independent retained-payload/receiver verification failed") from None
        now = self.runtime._now()
        if (proof.evidence_id != r.protection_evidence_id or proof.verifier_version != r.protection_verifier_version
                or proof.evidence_mode != self.runtime.mode or proof.configuration_sha256 != self.configuration_sha256
                or proof.control_binding != self.store._binding or proof.plan_sha256 != plan.sha256
                or proof.authentication_version != self.inbox.registration.authentication_version
                or proof.retained_materials != plan.retained_materials
                or not proof.observed_at <= now < proof.expires_at
                or now - proof.observed_at > timedelta(minutes=5)):
            raise Problem(403, "SETTLEMENT_PROTECTION_DENIED", "fresh exact inline retention and receiver observation is required")
        return proof

    def preflight(self, principal):
        """Prepare only the retained outbox and expose metadata to an observer."""
        self._operator(principal, "approve")
        keys = self._keys()
        rows = self.store.read_snapshot(keys)
        context, _, _, _, _ = self._state(rows)
        DeliveryConsumer(self.core).consume(self.binding.obligation_id, context, "retained-result-settlement")
        rows = self.store.read_snapshot(keys)
        return self._describe(rows)

    def prepare(self, principal):
        plan = self.preflight(principal)
        keys = self._keys(plan)
        self._protection(plan)
        key = settlement_key("plan", self.registration.settlement_id)
        request = ControlRequest(request_id=key, request_digest=plan.sha256, read_set=keys)

        def decide(current):
            self._operator(principal, "approve")
            if self._describe(current) != plan:
                raise Problem(409, "SETTLEMENT_PLAN_STALE", "original terminal result or recipient changed")
            return ControlChanges(writes=(self.core._write(key, plan, "create", _material(plan.model_dump(mode="json"))),),
                                  result={"plan_sha256": plan.sha256, "delivered": False})

        self.store.transact(request, decide)
        saved = self._saved(self.store.read_snapshot(keys), "plan", SettlementPlan)
        if saved != self._describe(self.store.read_snapshot(keys)):
            raise Problem(409, "SETTLEMENT_PLAN_STALE", "the retained plan is no longer current")
        return saved

    def authorize(self, principal, command: AuthorizeSettlement):
        command = _check(command, AuthorizeSettlement)
        self._operator(principal, "approve")
        keys = self._keys()
        rows = self.store.read_snapshot(keys)
        plan = self._saved(rows, "plan", SettlementPlan)
        if command.plan_sha256 != plan.sha256 or self._describe(rows) != plan:
            raise Problem(409, "SETTLEMENT_PLAN_STALE", "explicit decision must bind the exact saved plan")
        proof = self._protection(plan)
        key = settlement_key("authorization", self.registration.settlement_id)
        if rows[key] is not None:
            saved = self._saved(rows, "authorization", SettlementAuthorization)
            if saved.decision != command:
                raise Problem(409, "SETTLEMENT_DECISION_CONFLICT", "the installed settlement already has its immutable decision")
            self._authorized(rows, principal, saved)
            if proof != saved.protection:
                raise Problem(403, "SETTLEMENT_PROTECTION_DENIED", "original protection observation changed")
            return saved
        now = self.runtime._now()
        authorization = SettlementAuthorization(
            authorization_id=key, plan=plan, decision=command, principal_id=principal.oid, approved_at=now,
            expires_at=min(self.registration.expires_at, proof.expires_at,
                           proof.observed_at + timedelta(minutes=5), now + timedelta(minutes=15)),
            protection=proof,
        )
        request = ControlRequest(request_id=key, request_digest=_digest(command.model_dump(mode="json")), read_set=keys)

        def decide(current):
            self._operator(principal, "approve")
            self._authorized(current, principal, authorization, saved=False)
            return ControlChanges(writes=(self.core._write(
                key, authorization, "create", _material(authorization.model_dump(mode="json")),
            ),), result={"authorization_id": key, "plan_sha256": plan.sha256, "delivered": False})

        self.store.transact(request, decide)
        rows = self.store.read_snapshot(keys)
        saved = self._saved(rows, "authorization", SettlementAuthorization)
        self._authorized(rows, principal, saved)
        if saved.decision != command:
            raise Problem(409, "SETTLEMENT_DECISION_CONFLICT", "the installed settlement decision differs")
        return saved

    def _authorized(self, rows, principal, authorization, *, saved=True):
        self._operator(principal)
        now = self.runtime._now()
        key = settlement_key("authorization", self.registration.settlement_id)
        if (authorization.authorization_id != key or authorization.principal_id != principal.oid
                or authorization.decision.plan_sha256 != authorization.plan.sha256
                or not authorization.approved_at <= now < authorization.expires_at
                or authorization.expires_at > self.registration.expires_at
                or not authorization.protection.observed_at <= now < authorization.protection.expires_at
                or now - authorization.protection.observed_at > timedelta(minutes=5)
                or rows[settlement_key("revocation", self.registration.settlement_id)] is not None):
            raise Problem(403, "SETTLEMENT_AUTHORIZATION_DENIED", "independent HUMAN settlement authorization expired or was revoked")
        if (self._describe(rows) != authorization.plan
                or self._saved(rows, "plan", SettlementPlan) != authorization.plan
                or saved and self._saved(rows, "authorization", SettlementAuthorization) != authorization):
            raise Problem(403, "SETTLEMENT_SCOPE_CHANGED", "immutable terminal result, policy, owner or exact destination changed")

    def _access(self, authorization, request_id, action):
        return VerifiedInboxAccess(
            evidence_id=control_key("retained-settlement-access", authorization.authorization_id, action, request_id),
            principal_id=authorization.principal_id, authentication_version=self.inbox.registration.authentication_version,
            binding=authorization.plan.delivery, action=action,
            policy_revision=authorization.plan.operation_policy_revision,
            observed_at=authorization.approved_at, expires_at=authorization.expires_at,
            proof=_material({"authorization_id": authorization.authorization_id,
                             "authorization_sha256": _digest(authorization.model_dump(mode="json"))}).data,
        )

    def _permit(self, principal, command, action):
        self._operator(principal, "acknowledge" if action == "acknowledge" else "read")
        if self.inbox._retained_results is not self:
            raise Problem(403, "SETTLEMENT_DISABLED", "this retained-result verifier is not registered")
        keys = self._keys()
        rows = self.store.read_snapshot(keys)
        authorization = self._saved(rows, "authorization", SettlementAuthorization)
        if command.authorization_id != authorization.authorization_id:
            raise Problem(403, "SETTLEMENT_AUTHORIZATION_DENIED", "authorization belongs to another settlement")
        self._authorized(rows, principal, authorization)
        if self._protection(authorization.plan) != authorization.protection:
            raise Problem(403, "SETTLEMENT_PROTECTION_DENIED", "original protection observation changed")
        request_id = getattr(command, "request_id", "reconcile")
        return _RetainedAccess(self, principal, authorization, self._access(authorization, request_id, action),
                               request_id, keys)

    def _check_permit(self, permit, command, context, action):
        self._operator(permit.principal, "acknowledge" if action == "acknowledge" else "read")
        expected = self._access(permit.authorization, permit.request_id, action)
        if (permit.access != expected or context != permit.authorization.plan.context
                or command.access_evidence_id != expected.evidence_id
                or command.obligation_id != expected.binding.obligation_id
                or command.delivery_id != expected.binding.delivery_id
                or permit.read_set != self._keys(permit.authorization.plan)):
            raise Problem(403, "SETTLEMENT_SCOPE_CHANGED", "opaque retained-result access belongs to another scope")
        return expected

    def _gate(self, rows, permit):
        self._authorized(rows, permit.principal, permit.authorization)

    def fetch(self, principal, command: SettlementFetch):
        command = _check(command, SettlementFetch)
        permit = self._permit(principal, command, "fetch")
        binding = permit.access.binding
        return self.inbox.fetch_retained(InboxFetch(
            obligation_id=binding.obligation_id, delivery_id=binding.delivery_id,
            access_evidence_id=permit.access.evidence_id,
        ), permit.authorization.plan.context, permit)

    def acknowledge(self, principal, command: SettlementAcknowledgment):
        command = _check(command, SettlementAcknowledgment)
        permit = self._permit(principal, command, "acknowledge")
        binding = permit.access.binding
        return self.inbox.acknowledge_retained(InboxAcknowledgment(
            obligation_id=binding.obligation_id, delivery_id=binding.delivery_id,
            access_evidence_id=permit.access.evidence_id, exchange_id=command.exchange_id,
            acknowledgment_token=command.acknowledgment_token, binding=command.binding,
            confirmation_level=command.confirmation_level,
        ), permit.authorization.plan.context, permit)

    def reconcile(self, principal, command: SettlementUse):
        command = _check(command, SettlementUse)
        permit = self._permit(principal, command, "acknowledge")
        receipt = self.inbox.reconcile(permit.access.binding.delivery_id, permit.authorization.plan.context)
        self._gate(self.store.read_snapshot(permit.read_set), permit)
        return receipt

    def revoke(self, principal, command: RevokeSettlement):
        command = _check(command, RevokeSettlement)
        self._operator(principal, "approve", current=False)
        key = settlement_key("revocation", self.registration.settlement_id)
        auth_key = settlement_key("authorization", self.registration.settlement_id)
        request = ControlRequest(request_id=key, request_digest=_digest(command.model_dump(mode="json")),
                                 read_set=(auth_key,))

        def decide(rows):
            self._operator(principal, "approve", current=False)
            authorization = self._saved(rows, "authorization", SettlementAuthorization)
            if command.authorization_id != authorization.authorization_id or authorization.principal_id != principal.oid:
                raise Problem(403, "SETTLEMENT_AUTHORIZATION_DENIED", "revocation belongs to another authorization")
            value = _Revocation(
                command=command, principal_id=principal.oid, revoked_at=self.runtime._now(),
                authorization_sha256=_digest(authorization.model_dump(mode="json")),
            )
            return ControlChanges(writes=(self.core._write(
                key, value, "create", _material(value.model_dump(mode="json")),
            ),), result={"authorization_id": authorization.authorization_id, "revoked": True})

        return self.store.transact(request, decide)
