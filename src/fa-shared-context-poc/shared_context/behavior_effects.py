"""Controlled Table-backed v1.2 work mutation; actual_environment=NOT_RUN.

This receiver replaces the goal of ONE queued, not-yet-enqueued DeliveryCore
work with one of two bounded, non-secret goals. It updates the EXISTING
delivery-work-state row and its identity row's definition digest. It creates no alternative work authority,
does not touch legacy ContextService/M2/M3 rows, and does not change completion
checks, delivery obligations, executor state, or claim that work succeeded.
Existing result verifiers reject evidence pinned to the old definition digest.
The revision precondition is the work row's exact native ETag, not a second
revision counter. Immutable receipts retain the before/after definition digests.

Server setup:
  adapter = WorkStateEffectAdapter(delivery_core, allowed_work_ids=(...,),
                                   references={"prepared": WorkGoalUpdate(...)})
  broker = ToolBrokerCore(store, clock=trusted_utc,
                         adapter=adapter.registration, execution_adapter=adapter)
  broker.prepare(ToolIntent(...), server_context)
  # Existing trusted human grant provisioner binds the prepared digest.
  broker.execute(operation_id, attempt_id, context=server_context, grant_id=...)

Endpoint-facing trusted-server interfaces:
  adapter.inspect(work_id, context) -> WorkRevision
  broker.dispatcher.receiver.receive(WorkEffectDelivery, context) -> Record
  broker.dispatcher.receiver.reconcile(operation_id, attempt_id, context) -> Record
  broker.dispatcher.reconcile(operation_id, attempt_id, context) -> Record
Never deserialize ExecutionContext or register an adapter/transport from JSON.
The delivery command contains only original IDs and exact protected material;
grant, actor, policy and clock come exclusively from server/control records.

The sender persists the original receiver request in the consume EGT. The
receiver rechecks owner/epoch, grant/revocation/policy, reserved attempt, consumed
dispatch, exact prepared content, v1.2 work/obligation and ETag in its own
guard-protected decision. Work change + immutable receiver receipt share ONE
EGT. A competing revocation/owner/work writer advances the same guard and fences
the effect. Replays return that original receipt, not another mutation. Sender
timeouts retain unknown responsibility until read-only original-ID reconciliation
and a separate acknowledgement EGT. Neither receive nor acknowledge reserves
another grant use/unit. No automatic resend, refund or terminal-no-effect claim.

UTC is checked in the receiver decision AND at the final native batch request
hook (including the dispatch/grant deadlines). Table has no server-side NOW
predicate: this does NOT claim that an arbitrarily delayed network batch cannot
commit after physical wall-clock expiry. Strict service-commit-time expiry needs
a deadline-capable native backend/contract; local tests are not that evidence.
The supported UTC boundary here is receiver admission plus the final send hook,
while mutable revocation/epoch/version predicates are fenced at the actual EGT.
No Azure setup, credentials, HTTP mutation, resource creation or cleanup here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator

from .behavior_delivery import DeliveryCore
from .behavior_dispatch import (
    BehaviorDispatcher, DispatchObligation, DispatchRecord, _Invocation, _within,
)
from .behavior_operations import (
    AdapterContract, AttemptBinding, AuthorizationRequest, BudgetRecord, ExecutionContext,
    GrantRecord, GrantSubject, OperationDescription, OutcomeRequest, PolicyRecord,
    ReceiptResult, RegisteredAdapter, ResolvedResource, ToolBrokerCore, _Attempt,
    _Model, _checked, _digest, _json, _parse, _row, _updated, _utc, _write,
    canonical_material, control_key,
)
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest, Digest, InlineMaterial
from .behavior_workers import worker_key
from .errors import Problem
from .models import Identifier
from .records import Record

EFFECT_CONTRACT = "fa-table-work-effect-1"
GOAL_REVIEW = "Review the prepared result against the pinned work requirements."
GOAL_VERIFY = "Verify the pinned work requirements before preparing delivery."
Goal = Literal[
    "Review the prepared result against the pinned work requirements.",
    "Verify the pinned work requirements before preparing delivery.",
]
NativeRevision = Annotated[str, Field(min_length=1, max_length=256)]


def _material(value: object) -> InlineMaterial:
    data = _json(value)
    return InlineMaterial(data=data, sha256=hashlib.sha256(data).hexdigest())


def effect_key(kind: str, operation_id: str, attempt_id: str) -> str:
    return control_key("work-effect-" + kind, operation_id, attempt_id)


class WorkGoalUpdate(_Model):
    work_id: Identifier
    obligation_id: Identifier
    expected_etag: NativeRevision
    expected_definition_digest: Digest
    goal: Goal

    @field_validator("expected_etag")
    @classmethod
    def native_revision(cls, value):
        # Azure Table OData ETags and the existing SDK-shaped offline fixture.
        if re.fullmatch(r"""W/"(?:datetime'[0-9A-Fa-fTtZz:.%+-]+'|fake-[0-9]+)\"""", value) is None:
            raise ValueError("an exact native Table work-row ETag is required")
        return value


class WorkRevision(_Model):
    work_id: Identifier
    obligation_id: Identifier
    etag: NativeRevision
    definition_digest: Digest
    state: Literal["queued", "succeeded", "failed", "cancelled"]


class WorkEffectSettings(_Model):
    control_binding: Digest
    delivery_configuration_digest: Digest
    team_id: Identifier
    allowed_work_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=16)]
    execution_contract: Literal["fa-table-work-effect-1"] = EFFECT_CONTRACT


class WorkEffectDelivery(_Model):
    operation_id: Identifier
    attempt_id: Identifier
    consume_request_id: Digest
    material: InlineMaterial


class _Envelope(_Model):
    kind: Literal["work_effect_delivery"] = "work_effect_delivery"
    context: ExecutionContext
    command: WorkEffectDelivery
    receiver_request: ControlRequest


class WorkEffectReceipt(_Model):
    kind: Literal["work_effect_receipt"] = "work_effect_receipt"
    receiver_contract: Literal["fa-table-work-effect-1"] = EFFECT_CONTRACT
    receipt_id: Digest
    request_id: Digest
    consume_request_id: Digest
    binding: AttemptBinding
    work_id: Identifier
    obligation_id: Identifier
    prior_work_revision: Digest
    previous_definition_digest: Digest
    applied_definition_digest: Digest
    applied_state_digest: Digest
    owner_epoch: Annotated[int, Field(ge=0)]
    approval_id: Identifier
    grant_revocation_revision: Annotated[int, Field(ge=0)]
    policy_revision: Annotated[int, Field(ge=0)]
    receiver_checked_at: datetime
    receiver_not_after: datetime
    effect_state: Literal["applied"] = "applied"
    work_state: Literal["queued"] = "queued"
    obligation_fulfilled: Literal[False] = False

    _times = field_validator("receiver_checked_at", "receiver_not_after")(_utc)


class _DeadlineClient:
    """Same Table authority/client; adds a final, bounded request hook, no retries."""

    def __init__(self, client, clock, interval):
        self._client, self._clock, self._interval = client, clock, interval
        self.url, self.table_name = client.url, client.table_name

    def get_entity(self, **kwargs):
        return self._client.get_entity(**kwargs)

    def submit_transaction(self, operations, *, raw_request_hook, **kwargs):
        def check(request):
            if len(self._interval) != 1:
                raise Problem(403, "WORK_EFFECT_GATE_MISSING", "receiver authorization is missing")
            _within(self._clock, *self._interval[0])
            raw_request_hook(request)

        return self._client.submit_transaction(operations, raw_request_hook=check, **kwargs)


class WorkStateEffectAdapter:
    execution_contract = EFFECT_CONTRACT

    def __init__(
        self, delivery: DeliveryCore, *, allowed_work_ids: tuple[str, ...],
        references: Mapping[str, WorkGoalUpdate],
        transport: Callable[[WorkEffectDelivery, ExecutionContext], Record] | None = None,
    ):
        self.delivery, self.references, self.transport = delivery, references, transport
        config = delivery._enabled()[1]
        self.settings = WorkEffectSettings(
            control_binding=config.control_binding, team_id=config.team_id,
            delivery_configuration_digest=delivery.configuration_digest(), allowed_work_ids=allowed_work_ids,
        )
        if len(set(allowed_work_ids)) != len(allowed_work_ids):
            raise Problem(422, "WORK_EFFECT_CONFIGURATION", "duplicate work allowlist entry")
        self.contract = AdapterContract(
            adapter_id="fa-v12-work-state", adapter_version="1",
            allowed_kinds=("request",), allowed_methods=("PATCH",),
            allowed_targets=tuple(self.target(work) for work in allowed_work_ids),
            reservation_units=1, evidence_source="fa-table-work-receiver",
            outcome_contract_version=EFFECT_CONTRACT,
        )
        self.registration = RegisteredAdapter(contract=self.contract, resolve=self.resolve)

    def target(self, work_id: str) -> str:
        return f"fa-work-state://{self.settings.control_binding}/{work_id}"

    def _configuration(self, store: BehaviorStore) -> None:
        if (
            store._binding != self.settings.control_binding
            or self.delivery.store._binding != self.settings.control_binding
            or self.delivery.configuration_digest() != self.settings.delivery_configuration_digest
            or self.delivery._enabled()[1].team_id != self.settings.team_id
        ):
            raise Problem(403, "WORK_EFFECT_AUTHORITY_MISMATCH", "receiver and work must share one authority/configuration")

    def resolve(self, reference: str) -> OperationDescription:
        try:
            spec = _checked(self.references[reference], WorkGoalUpdate)
        except Exception:
            raise Problem(422, "WORK_EFFECT_REFERENCE_DENIED", "no compliant server-resolved work update") from None
        target = self.target(spec.work_id)
        description = OperationDescription(
            adapter_id=self.contract.adapter_id, adapter_version=self.contract.adapter_version,
            kind="request", method="PATCH", target=target, content=_json(spec.model_dump(mode="json")),
            resources=(ResolvedResource(resource_id=target, version=hashlib.sha256(spec.expected_etag.encode()).hexdigest()),),
        )
        self.validate(description)
        return description

    def validate(self, description: OperationDescription, context: ExecutionContext | None = None) -> WorkGoalUpdate:
        description = _checked(description, OperationDescription)
        try:
            spec = WorkGoalUpdate.model_validate_json(description.content)
        except (ValidationError, ValueError):
            raise Problem(422, "WORK_EFFECT_CONTENT_DENIED", "unsupported saved work update") from None
        target = self.target(spec.work_id)
        if (
            description.adapter_id != self.contract.adapter_id or description.adapter_version != self.contract.adapter_version
            or description.kind != "request" or description.method != "PATCH" or description.target != target
            or spec.work_id not in self.settings.allowed_work_ids or target not in self.contract.allowed_targets
            or description.content != _json(spec.model_dump(mode="json")) or description.dependencies
            or description.resources != (ResolvedResource(
                resource_id=target, version=hashlib.sha256(spec.expected_etag.encode()).hexdigest(),
            ),)
        ):
            raise Problem(403, "WORK_EFFECT_CONTENT_DENIED", "method, work target, body or revision differs")
        return spec

    def inspect(self, work_id: str, context: ExecutionContext) -> WorkRevision:
        context = _checked(context, ExecutionContext)
        self._configuration(self.delivery.store)
        if work_id not in self.settings.allowed_work_ids:
            raise Problem(403, "WORK_EFFECT_TARGET_DENIED", "work is not in the server allowlist")
        identity_key = control_key("delivery-work", work_id)
        identity = self.delivery.store.read_snapshot((identity_key,))[identity_key]
        if identity is None:
            raise Problem(404, "WORK_EFFECT_WORK_MISSING", "only an admitted v1.2 work can be updated")
        try:
            obligation_id = identity.payload["data"]["obligation_id"]
            keys = self._work_keys(work_id, obligation_id, context)
        except (KeyError, TypeError):
            raise Problem(502, "WORK_EFFECT_WORK_INVALID", "invalid v1.2 work identity") from None
        rows = self.delivery.store.read_snapshot(keys)
        ob = self.delivery._obligation(rows, obligation_id, context)
        work = self.delivery._work(rows, ob)
        self._identity(rows, work_id, obligation_id, work)
        return WorkRevision(
            work_id=work_id, obligation_id=obligation_id,
            etag=rows[control_key("delivery-work-state", obligation_id)].etag,
            definition_digest=work.definition_digest, state=work.state,
        )

    @staticmethod
    def _work_keys(work_id, obligation_id, context):
        return (
            control_key("delivery-work", work_id), control_key("delivery-work-state", obligation_id),
            control_key("delivery-obligation", obligation_id),
            control_key("delivery-conversation", context.conversation_id),
            control_key("owner", context.owner_record_id),
        )

    def _identity(self, rows, work_id, obligation_id, work):
        record = rows[control_key("delivery-work", work_id)]
        if record is None:
            raise Problem(404, "WORK_EFFECT_WORK_MISSING", "v1.2 work identity is missing")
        data = record.payload["data"]
        if (
            record.payload["binding"] != self.settings.control_binding
            or data.get("kind") != "delivery_work_identity" or data.get("work_id") != work_id
            or data.get("obligation_id") != obligation_id or data.get("event_id") != work.event_id
            or data.get("state_row_id") != control_key("delivery-work-state", obligation_id)
            or data.get("definition_digest") != work.definition_digest
            or data.get("configuration_digest") != self.settings.delivery_configuration_digest
            or data.get("evidence_mode") != self.delivery.registration.mode
        ):
            raise Problem(409, "WORK_EFFECT_WORK_INVALID", "work identity does not reference this authoritative state")
        return record


class WorkStateEffectReceiver:
    def __init__(self, core: ToolBrokerCore, adapter: WorkStateEffectAdapter):
        self.core, self.adapter, self.store = core, adapter, core.store
        adapter._configuration(self.store)

    def request_for(self, command: WorkEffectDelivery, context: ExecutionContext, grant_id: str) -> ControlRequest:
        """Server-only request construction during consume, never a grant argument on receive."""
        description = OperationDescription.model_validate_json(command.material.data)
        spec = self.adapter.validate(description)
        keys = (
            *self.adapter._work_keys(spec.work_id, spec.obligation_id, context),
            worker_key("admission", spec.obligation_id),
            control_key("operation", command.operation_id),
            control_key("attempt", command.operation_id, command.attempt_id),
            control_key("grant", grant_id), control_key("policy", context.policy_id),
            control_key("budget", context.root_budget_id),
            control_key("dispatch", command.operation_id, command.attempt_id),
            control_key("dispatch-obligation", command.operation_id, command.attempt_id),
            effect_key("delivery", command.operation_id, command.attempt_id),
            effect_key("receipt", command.operation_id, command.attempt_id),
        )
        return ControlRequest(
            request_id=effect_key("receive", command.operation_id, command.attempt_id),
            request_digest=_digest([
                EFFECT_CONTRACT, command.model_dump(mode="json"), context.model_dump(mode="json"),
                self.adapter.settings.model_dump(mode="json"),
            ]),
            read_set=tuple(sorted(set(keys))),
        )

    def _envelope(self, operation_id: str, attempt_id: str) -> _Envelope:
        key = effect_key("delivery", operation_id, attempt_id)
        rows = self.store.read_snapshot((key,))
        envelope = _row(rows, key, _Envelope)
        saved = _parse(rows[key].payload["material"], InlineMaterial)
        if (
            envelope.command.operation_id != operation_id or envelope.command.attempt_id != attempt_id
            or saved != _material(envelope.model_dump(mode="json"))
        ):
            raise Problem(409, "WORK_EFFECT_DELIVERY_DENIED", "protected receiver delivery differs")
        return envelope

    def receive(self, command: WorkEffectDelivery, context: ExecutionContext) -> Record:
        command, context = _checked(command, WorkEffectDelivery), _checked(context, ExecutionContext)
        self.adapter._configuration(self.store)
        envelope = self._envelope(command.operation_id, command.attempt_id)
        if command != envelope.command or context != envelope.context:
            raise Problem(403, "WORK_EFFECT_DELIVERY_DENIED", "delivery bytes or authenticated execution context differ")
        interval = []
        store = BehaviorStore(_DeadlineClient(self.store._client, self.core.clock, interval), self.store._scope)

        def decide(rows):
            self.core._owner(rows, context, command.operation_id)
            operation, material, description = self.core._operation(rows, command.operation_id, context)
            spec = self.adapter.validate(description, context)
            attempt = _row(rows, control_key("attempt", command.operation_id, command.attempt_id), _Attempt)
            dispatch = _row(rows, control_key("dispatch", command.operation_id, command.attempt_id), DispatchRecord)
            responsibility = _row(rows, control_key("dispatch-obligation", command.operation_id, command.attempt_id), DispatchObligation)
            retained = _row(rows, effect_key("delivery", command.operation_id, command.attempt_id), _Envelope)
            if (
                retained != envelope or command.material != material or canonical_material(description) != material
                or envelope.receiver_request != self.request_for(command, context, attempt.grant_id)
                or dispatch.context != context or attempt.context != context
                or dispatch.execution_contract != EFFECT_CONTRACT or dispatch.phase != "consumed"
                or dispatch.grant_id != attempt.grant_id or dispatch.binding != attempt.binding
                or dispatch.consume_request_id != command.consume_request_id
                or responsibility.binding != attempt.binding or responsibility.consume_request_id != command.consume_request_id
                or responsibility.state != "pending_reconciliation"
                or responsibility.owner_record_id != context.owner_record_id or responsibility.root_budget_id != context.root_budget_id
                or operation.current_attempt_id != command.attempt_id or operation.unsettled_attempt_ids != (command.attempt_id,)
                or operation.effect_state != "unknown" or attempt.effect_state != "unknown" or attempt.observation_sequence
                or attempt.binding.description_digest != material.sha256
                or attempt.binding.adapter_contract_digest != self.core._contract_digest()
                or attempt.binding.target != description.target
                or attempt.binding.operation_id != command.operation_id or attempt.binding.attempt_id != command.attempt_id
                or attempt.binding.idempotency_key != control_key("idempotency", command.operation_id, material.sha256)
            ):
                raise Problem(403, "WORK_EFFECT_BINDING_DENIED", "receiver requires the exact existing consumed attempt")
            grant = _row(rows, control_key("grant", attempt.grant_id), GrantRecord)
            policy = _row(rows, control_key("policy", context.policy_id), PolicyRecord)
            budget = _row(rows, control_key("budget", context.root_budget_id), BudgetRecord)
            if (
                grant.grant_id != attempt.grant_id or grant.revoked
                or grant.revocation_revision != attempt.grant_revocation_revision
                or grant.revocation_revision != dispatch.grant_revocation_revision
                or grant.root_budget_id != context.root_budget_id or grant.description_digest != material.sha256
                or grant.adapter_contract_digest != self.core._contract_digest()
                or GrantSubject(principal_id=context.principal_id, conversation_id=context.conversation_id) not in grant.subjects
                or grant.policy_id != context.policy_id or policy.policy_id != context.policy_id or not policy.active
                or policy.revision != grant.policy_revision or policy.revision != attempt.policy_revision
                or policy.revision != dispatch.policy_revision
                or budget.root_budget_id != context.root_budget_id or not budget.active
                or attempt.reserved_units != self.adapter.contract.reservation_units
            ):
                raise Problem(403, "WORK_EFFECT_GRANT_DENIED", "current grant, revocation, policy or reservation differs")
            ob = self.adapter.delivery._obligation(rows, spec.obligation_id, context)
            work = self.adapter.delivery._work(rows, ob)
            identity = self.adapter._identity(rows, spec.work_id, spec.obligation_id, work)
            if rows[worker_key("admission", spec.obligation_id)] is not None:
                raise Problem(409, "WORK_EFFECT_EXECUTION_BOUND", "enqueued work requires a new revision and execution binding")
            state_key = control_key("delivery-work-state", spec.obligation_id)
            state = rows[state_key]
            if (
                ob.work_id != spec.work_id or ob.fulfillment != "pending" or ob.final_delivery_id is not None
                or work.state != "queued" or work.result is not None or state.payload["material"] is not None
                or state.etag != spec.expected_etag or work.definition_digest != spec.expected_definition_digest
                or work.definition.goal == spec.goal
            ):
                raise Problem(409, "WORK_EFFECT_REVISION_CONFLICT", "queued work revision or definition differs")
            checked_at = self.core._now()
            deadline = min(grant.expires_at, attempt.not_after, dispatch.not_after, ob.deadline)
            start = max(grant.not_before, attempt.authorized_at, dispatch.gated_at, dispatch.armed_at)
            _within(self.core.clock, start, deadline)
            interval.append((checked_at, deadline))
            definition = _updated(work.definition, goal=spec.goal)
            changed = _updated(work, definition=definition, definition_digest=_digest(definition.model_dump(mode="json")))
            proof = WorkEffectReceipt(
                receipt_id=effect_key("receipt", command.operation_id, command.attempt_id),
                request_id=envelope.receiver_request.request_id, consume_request_id=command.consume_request_id,
                binding=attempt.binding, work_id=spec.work_id, obligation_id=spec.obligation_id,
                prior_work_revision=hashlib.sha256(spec.expected_etag.encode()).hexdigest(),
                previous_definition_digest=work.definition_digest, applied_definition_digest=changed.definition_digest,
                applied_state_digest=_digest(changed.model_dump(mode="json")), owner_epoch=context.epoch,
                approval_id=grant.approval_id, grant_revocation_revision=grant.revocation_revision,
                policy_revision=policy.revision, receiver_checked_at=checked_at, receiver_not_after=deadline,
            )
            proof_material = _material(proof.model_dump(mode="json"))
            return ControlChanges(writes=(
                _write(state_key, changed, "replace"),
                self.adapter.delivery._write(
                    control_key("delivery-work", spec.work_id),
                    {**identity.payload["data"], "definition_digest": changed.definition_digest},
                ),
                _write(proof.receipt_id, proof, "create", proof_material),
            ), result={
                "receipt": proof.model_dump(mode="json"),
                "receiver_receipt_digest": proof_material.sha256, "effect_state": "applied",
            })

        try:
            return store.transact(envelope.receiver_request, decide)
        except Problem as exc:
            if exc.body["error"]["code"] == "BEHAVIOR_CONFLICT":
                # A racing duplicate can read the winner; never resubmit a batch.
                try:
                    return self.store.reconcile(envelope.receiver_request)
                except Problem:
                    pass
            raise

    def reconcile(self, operation_id: str, attempt_id: str, context: ExecutionContext) -> Record:
        context = _checked(context, ExecutionContext)
        self.adapter._configuration(self.store)
        envelope = self._envelope(operation_id, attempt_id)
        rows = self.store.read_snapshot((
            control_key("owner", context.owner_record_id), control_key("operation", operation_id),
        ))
        self.core._owner(rows, context, operation_id)
        self.core._operation(rows, operation_id, context)
        receipt = self.store.reconcile(envelope.receiver_request)
        proof = _parse(receipt.payload["result"]["receipt"], WorkEffectReceipt)
        if (
            proof.request_id != envelope.receiver_request.request_id
            or proof.binding.operation_id != operation_id or proof.binding.attempt_id != attempt_id
            or proof.consume_request_id != envelope.command.consume_request_id
            or proof.binding.description_digest != envelope.command.material.sha256
            or receipt.payload["result"]["receiver_receipt_digest"] != _material(proof.model_dump(mode="json")).sha256
        ):
            raise Problem(502, "WORK_EFFECT_RECEIPT_INVALID", "original receiver receipt does not match the retained delivery")
        return receipt


class WorkStateDispatcher(BehaviorDispatcher):
    def __init__(self, core: ToolBrokerCore, adapter: WorkStateEffectAdapter):
        super().__init__(core, adapter)
        self.receiver = WorkStateEffectReceiver(core, adapter)

    def check_mutable_target(self, command: AuthorizationRequest, context: ExecutionContext,
                             *, fixed_work_ids: tuple[str, ...] = ()) -> None:
        self.adapter._configuration(self.store)
        operation_key = control_key("operation", command.operation_id)
        rows = self.store.read_snapshot((
            operation_key, control_key("owner", context.owner_record_id),
            control_key("grant", command.grant_id), control_key("policy", context.policy_id),
            control_key("budget", context.root_budget_id),
        ))
        self.core._owner(rows, context, command.operation_id)
        operation, material, description = self.core._operation(
            rows, command.operation_id, context,
        )
        if operation.current_attempt_id is not None:
            return
        self.core._grant_budget(rows, command, context, material.sha256, self.core._now(),
                                self.adapter.contract.reservation_units)
        spec = self.adapter.validate(description, context)
        admission_key = worker_key("admission", spec.obligation_id)
        if spec.work_id in fixed_work_ids or self.store.read_snapshot((admission_key,))[admission_key] is not None:
            raise Problem(409, "WORK_EFFECT_EXECUTION_BOUND", "fixed work requires a new revision and execution binding")

    def _consume_changes(self, invocation: _Invocation, attempt: _Attempt, changes: ControlChanges) -> ControlChanges:
        command = WorkEffectDelivery(
            operation_id=invocation.binding.operation_id, attempt_id=invocation.binding.attempt_id,
            consume_request_id=invocation.request_id, material=invocation.material,
        )
        envelope = _Envelope(
            context=invocation.context, command=command,
            receiver_request=self.receiver.request_for(command, invocation.context, attempt.grant_id),
        )
        return ControlChanges(writes=(*changes.writes, _write(
            effect_key("delivery", command.operation_id, command.attempt_id), envelope, "create",
            _material(envelope.model_dump(mode="json")),
        )), result=changes.result)

    def execute(self, command: AuthorizationRequest, context: ExecutionContext) -> Record:
        command, context = _checked(command, AuthorizationRequest), _checked(context, ExecutionContext)
        if command.previous_attempt_id is not None or command.idempotency_evidence_id is not None:
            raise Problem(403, "OPERATION_RETRY_DISABLED", "only original-ID receiver reconciliation is supported")
        self.adapter._configuration(self.store)
        self.check_mutable_target(command, context)
        self.core.authorize(command, context)
        self._arm(command, context)
        invocation = self._consume(command, context)
        delivery = WorkEffectDelivery(
            operation_id=command.operation_id, attempt_id=command.attempt_id,
            consume_request_id=invocation.request_id, material=invocation.material,
        )
        try:
            result = (self.adapter.transport or self.receiver.receive)(delivery, context)
        except Exception:
            raise Problem(
                503, "WORK_EFFECT_SEND_UNKNOWN",
                "receiver response is unknown; reconcile only the original delivery without another send",
            ) from None
        original = self.receiver.reconcile(command.operation_id, command.attempt_id, context)
        if not isinstance(result, Record) or result != original:
            raise Problem(502, "WORK_EFFECT_RECEIPT_INVALID", "receiver acknowledgement differs from original persisted evidence")
        return self._acknowledge(original, context)

    def _acknowledge(self, receipt: Record, context: ExecutionContext) -> Record:
        proof = _parse(receipt.payload["result"]["receipt"], WorkEffectReceipt)
        binding = proof.binding
        dispatch_key = control_key("dispatch", binding.operation_id, binding.attempt_id)
        obligation_key = control_key("dispatch-obligation", binding.operation_id, binding.attempt_id)
        operation_key = control_key("operation", binding.operation_id)
        attempt_key = control_key("attempt", binding.operation_id, binding.attempt_id)
        material = _material(proof.model_dump(mode="json"))
        request = ControlRequest(
            request_id=effect_key("acknowledge", binding.operation_id, binding.attempt_id),
            request_digest=_digest([EFFECT_CONTRACT, "acknowledge", material.sha256]),
            read_set=tuple(sorted((
                control_key("owner", context.owner_record_id), operation_key, attempt_key,
                dispatch_key, obligation_key, proof.receipt_id,
            ))),
        )

        def decide(rows):
            self.core._owner(rows, context, binding.operation_id)
            operation, prepared, _ = self.core._operation(rows, binding.operation_id, context)
            attempt = _row(rows, attempt_key, _Attempt)
            dispatch = _row(rows, dispatch_key, DispatchRecord)
            obligation = _row(rows, obligation_key, DispatchObligation)
            saved = _row(rows, proof.receipt_id, WorkEffectReceipt)
            if (
                saved != proof or _parse(rows[proof.receipt_id].payload["material"], InlineMaterial) != material
                or attempt.binding != binding or dispatch.binding != binding or obligation.binding != binding
                or binding.description_digest != prepared.sha256 or dispatch.phase != "consumed"
                or dispatch.consume_request_id != proof.consume_request_id
                or operation.current_attempt_id != binding.attempt_id or operation.unsettled_attempt_ids != (binding.attempt_id,)
                or attempt.effect_state != "unknown" or operation.effect_state != "unknown"
            ):
                raise Problem(409, "WORK_EFFECT_RECEIPT_INVALID", "receipt is not for this unresolved consumed attempt")
            attempt_material = rows[attempt_key].payload["material"]
            result = ReceiptResult(
                receipt_id=proof.receipt_id,
                resource=ResolvedResource(resource_id=binding.target, version=proof.applied_state_digest),
                status="completed",
            )
            return ControlChanges(writes=(
                _write(operation_key, _updated(
                    operation, effect_state="applied", business_state="succeeded",
                    unsettled_attempt_ids=(), successful_result=result,
                ), "replace", prepared),
                _write(attempt_key, _updated(attempt, effect_state="applied", observation_sequence=1), "replace",
                       None if attempt_material is None else _parse(attempt_material, InlineMaterial)),
                _write(dispatch_key, _updated(
                    dispatch, phase="settled", responsibility="resolved", effect_state="applied", result_digest=material.sha256,
                ), "replace", material),
                _write(obligation_key, _updated(obligation, state="resolved"), "replace"),
            ), result={
                "effect_state": "applied", "operation_business_state": "succeeded",
                "receiver_receipt": proof.model_dump(mode="json"), "result_digest": material.sha256,
                "work_state": "queued", "obligation_fulfilled": False, "execution_enabled": True,
            })

        return self.store.transact(request, decide)

    def reconcile(self, operation_id: str, attempt_id: str, context: ExecutionContext) -> Record:
        original = self.receiver.reconcile(operation_id, attempt_id, context)
        return self._acknowledge(original, context)

    def record_outcome(self, command: OutcomeRequest, context: ExecutionContext) -> Record:
        command = _checked(command, OutcomeRequest)
        if command.evidence_id != effect_key("receipt", command.operation_id, command.attempt_id):
            raise Problem(403, "OPERATION_EVIDENCE_DENIED", "only the original immutable receiver receipt is supported")
        return self.reconcile(command.operation_id, command.attempt_id, context)
