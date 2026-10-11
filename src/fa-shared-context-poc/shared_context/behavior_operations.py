"""Bounded operation preparation/accounting core; execution is opt-in.

The embedding server supplies an authenticated ExecutionContext, a trusted UTC
clock and one version-pinned RegisteredAdapter. None comes from model arguments.
Only a trusted approval/control writer may provision Owner/Policy/Grant/Budget
records, using control_key() and the SAME BehaviorStore authority/guard. This
module provides no human-grant creation endpoint or out-of-band predicate writer.
Owner records contain server-assigned stable operation IDs for logical actions;
models cannot mint a replacement ID to retry an unresolved action. The embedding
server must preserve that identity across resumes, rather than assign a new ID.
Units are integer reservation units, NOT a provider billing cap.

Accounting adapter callbacks resolve compliant content or VERIFY previously obtained
destination evidence. They must return the narrow models below or a safe Problem;
no accounting callback executes a tool, shell or arbitrary HTTP request.
Concrete execution and its credential boundary live in behavior_dispatch.
No adapter is installed by default. Evidence-contract options default to disabled;
registering a boolean in ToolIntent cannot enable them or supply approval.

prepare() resolves aliases once and returns the retained canonical bytes for an
approval preview; the caller must not resolve the alias again. authorize() reads
those bytes and all current predicates in one guard-protected transaction. Its
receipt is an accounting fact, NOT a reusable dispatch ticket. Replaying it
returns the original receipt without another reservation. execute() requires an
explicit concrete adapter; its dispatcher consumes saved bytes and enforces a
fresh last gate, one-attempt delivery, clock window and destination conditions.

After conflict/unknown, do not blindly resubmit: use reconciliation_request()
with BehaviorStore.reconcile(). A missing receipt remains unknown. A successful
reconciliation is historical read-back, never permission to dispatch again.
The optional behavior_dispatch adapter adds a durable one-consume lifecycle.
Without it execute() remains disabled. Authorization receipts remain accounting
only; they never confer permission to send, including after reconciliation.
Callbacks inside transact are side-effect-free. There are no locks, refunds,
deletions, Blob pointers or GC. Inline operation/evidence material is retained,
including when commit/outcome is unknown; deployment retention protection still
requires independent verification. actual_environment=NOT_RUN.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated, Literal, TypeVar

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from .behavior_store import (
    BehaviorStore, ControlChanges, ControlRequest, ControlWrite, Digest, InlineMaterial,
)
from .errors import Problem
from .models import Identifier, StrictModel
from .records import Record

if TYPE_CHECKING:
    from .behavior_dispatch import ContextArtifactAdapter
    from .behavior_effects import WorkStateEffectAdapter

CANONICALIZATION_VERSION = "fa-operation-v1"
Text = Annotated[str, Field(min_length=1, max_length=1024)]
Version = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
Count = Annotated[int, Field(ge=0, le=2**63 - 1)]
Effect = Literal["unknown", "not_applied", "applied", "partial"]
Observation = Literal[
    "unknown", "negative_query", "cancel_accepted", "local_lease_expired",
    "connection_closed", "caller_stopped", "applied", "partial",
    "terminal_rejection", "terminal_withdrawal", "effect_fenced",
]
TERMINAL = frozenset({"terminal_rejection", "terminal_withdrawal", "effect_fenced"})


class _Model(StrictModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False,
        ser_json_bytes="base64", val_json_bytes="base64",
    )


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("trusted, timezone-aware UTC is required")
    return value


class ResolvedResource(_Model):
    resource_id: Text
    version: Version

    @field_validator("version")
    @classmethod
    def immutable_version(cls, value):
        if value.lower() in {"unknown", "current", "latest", "head"}:
            raise ValueError("an immutable resolved version is required")
        return value


class ContentDependency(ResolvedResource):
    content: Annotated[bytes, Field(max_length=16384)]


class OperationDescription(_Model):
    canonicalization_version: Literal["fa-operation-v1"] = CANONICALIZATION_VERSION
    adapter_id: Identifier
    adapter_version: Version
    kind: Literal["request", "script", "plan"]
    method: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")]
    target: Text
    content: Annotated[bytes, Field(max_length=24576)]
    resources: Annotated[tuple[ResolvedResource, ...], Field(min_length=1, max_length=16)]
    dependencies: Annotated[tuple[ContentDependency, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def unique_resources(self):
        for items in (self.resources, self.dependencies):
            if len({item.resource_id for item in items}) != len(items):
                raise ValueError("duplicate resolved resource")
        return self


class ToolIntent(_Model):
    operation_id: Identifier
    input_ref: Identifier


class ExecutionContext(_Model):
    owner_record_id: Identifier
    principal_id: Identifier
    epoch: Count
    conversation_id: Identifier
    policy_id: Identifier
    root_budget_id: Identifier


class OwnerRecord(ExecutionContext):
    kind: Literal["operation_owner"] = "operation_owner"
    active: bool
    operation_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=64)]


class PolicyRecord(_Model):
    kind: Literal["operation_policy"] = "operation_policy"
    policy_id: Identifier
    revision: Count
    active: bool


class GrantSubject(_Model):
    principal_id: Identifier
    conversation_id: Identifier


class GrantRecord(_Model):
    kind: Literal["operation_grant"] = "operation_grant"
    grant_id: Identifier
    approval_source: Literal["human"]
    approval_id: Identifier
    subjects: Annotated[tuple[GrantSubject, ...], Field(min_length=1, max_length=32)]
    root_budget_id: Identifier
    description_digest: Digest
    adapter_contract_digest: Digest
    policy_id: Identifier
    policy_revision: Count
    revocation_revision: Count
    revoked: bool
    not_before: datetime
    expires_at: datetime
    remaining_uses: Count
    remaining_units: Count

    _times = field_validator("not_before", "expires_at")(_utc)

    @model_validator(mode="after")
    def interval(self):
        if self.not_before >= self.expires_at:
            raise ValueError("empty grant interval")
        return self


class BudgetRecord(_Model):
    kind: Literal["operation_budget"] = "operation_budget"
    root_budget_id: Identifier
    active: bool
    remaining_attempts: Count
    remaining_units: Count


class AdapterContract(_Model):
    adapter_id: Identifier
    adapter_version: Version
    allowed_kinds: Annotated[tuple[Literal["request", "script", "plan"], ...], Field(min_length=1)]
    allowed_methods: Annotated[tuple[str, ...], Field(min_length=1, max_length=16)]
    allowed_targets: Annotated[tuple[Text, ...], Field(min_length=1, max_length=16)]
    reservation_units: Annotated[int, Field(ge=1, le=2**63 - 1)]
    evidence_source: Identifier
    outcome_contract_version: Version
    terminality_contract_version: Version | None = None
    idempotency_contract_version: Version | None = None

    @model_validator(mode="after")
    def exact_allowlists(self):
        if any(not item or "*" in item for item in (*self.allowed_methods, *self.allowed_targets)):
            raise ValueError("only exact, nonempty allowlists are supported")
        for version in (
            self.adapter_version, self.outcome_contract_version,
            self.terminality_contract_version, self.idempotency_contract_version,
        ):
            if version is not None and version.lower() in {"unknown", "current", "latest", "head"}:
                raise ValueError("a known pinned adapter/verification version is required")
        return self


class AttemptBinding(_Model):
    operation_id: Identifier
    attempt_id: Identifier
    description_digest: Digest
    adapter_contract_digest: Digest
    target: Text
    idempotency_key: Digest


class ReceiptResult(_Model):
    receipt_id: Identifier
    resource: ResolvedResource
    status: Literal["completed", "failed"]


class EffectPart(_Model):
    unit_id: Identifier
    effect: Literal["applied", "not_applied", "unknown"]


class VerifiedOutcome(_Model):
    evidence_id: Identifier
    source: Identifier
    contract_version: Version
    binding: AttemptBinding
    observation: Observation
    sequence: Annotated[int, Field(ge=1, le=2**63 - 1)]
    observed_at: datetime
    proof: Annotated[bytes, Field(min_length=1, max_length=8192)]
    terminality_contract_version: Version | None = None
    terminality_scope: Literal["entire_attempt_all_in_flight_deliveries"] | None = None
    terminality_reference: Identifier | None = None
    business_result: Literal["unknown", "succeeded", "failed"] = "unknown"
    result: ReceiptResult | None = None
    parts: Annotated[tuple[EffectPart, ...], Field(max_length=32)] = ()

    _time = field_validator("observed_at")(_utc)

    @model_validator(mode="after")
    def result_contract(self):
        terminal_fields = (
            self.terminality_contract_version, self.terminality_scope, self.terminality_reference,
        )
        if self.observation in TERMINAL:
            if any(value is None for value in terminal_fields):
                raise ValueError("terminality requires a verified full-attempt scope and destination reference")
        elif any(value is not None for value in terminal_fields):
            raise ValueError("a nonterminal observation cannot assert terminality")
        if self.business_result == "succeeded" and (
            self.observation != "applied" or self.result is None or self.result.status != "completed"
        ):
            raise ValueError("success requires applied effects and a completed result receipt")
        if self.observation == "partial" and (
            not any(part.effect == "applied" for part in self.parts)
            or not any(part.effect != "applied" for part in self.parts)
        ):
            raise ValueError("partial requires known effects and unfinished units")
        if self.observation == "applied" and any(part.effect != "applied" for part in self.parts):
            raise ValueError("applied cannot hide incomplete effect units")
        if self.observation in TERMINAL and any(part.effect != "not_applied" for part in self.parts):
            raise ValueError("terminal non-application cannot hide known or unresolved effects")
        if len({part.unit_id for part in self.parts}) != len(self.parts):
            raise ValueError("duplicate effect unit")
        return self


class IdempotencyEvidence(_Model):
    evidence_id: Identifier
    source: Identifier
    contract_version: Version
    binding: AttemptBinding
    scope: Literal["whole_operation_all_overlapping_deliveries"]
    not_before: datetime
    expires_at: datetime
    proof: Annotated[bytes, Field(min_length=1, max_length=8192)]

    _times = field_validator("not_before", "expires_at")(_utc)

    @model_validator(mode="after")
    def interval(self):
        if self.not_before >= self.expires_at:
            raise ValueError("empty destination idempotency interval")
        return self


@dataclass(frozen=True)
class RegisteredAdapter:
    """Server-only registration, NOT a provider implementation or approval."""

    contract: AdapterContract
    resolve: Callable[[str], OperationDescription]
    verify_outcome: Callable[[str], VerifiedOutcome] | None = None
    verify_idempotency: Callable[[str], IdempotencyEvidence] | None = None


class AuthorizationRequest(_Model):
    operation_id: Identifier
    attempt_id: Identifier
    grant_id: Identifier
    previous_attempt_id: Identifier | None = None
    idempotency_evidence_id: Identifier | None = None


class OutcomeRequest(_Model):
    operation_id: Identifier
    attempt_id: Identifier
    evidence_id: Identifier


class HumanOperationRejectionIntent(_Model):
    schema_version: Literal["fa-operation-rejection-intent-v1"] = "fa-operation-rejection-intent-v1"
    binding_id: Identifier
    binding_digest: Digest
    operation_id: Identifier
    decision_id: Identifier
    request_id: Identifier
    principal_oid: Identifier
    input_digest: Digest


class HumanOperationRejection(_Model):
    schema_version: Literal["fa-operation-rejection-v1"]
    operation_id: Identifier
    decision_id: Identifier
    description_digest: Digest
    adapter_contract_digest: Digest
    rejected_by_oid: Identifier
    rejected_at: datetime

    _time = field_validator("rejected_at")(_utc)


class _Operation(_Model):
    kind: Literal["operation"] = "operation"
    operation_id: Identifier
    owner_record_id: Identifier
    conversation_id: Identifier
    root_budget_id: Identifier
    description_digest: Digest
    adapter_contract_digest: Digest
    current_attempt_id: Identifier | None = None
    unsettled_attempt_ids: Annotated[tuple[Identifier, ...], Field(max_length=16)] = ()
    effect_state: Effect = "unknown"
    business_state: Literal["pending", "succeeded", "failed"] = "pending"
    successful_result: ReceiptResult | None = None
    human_rejection: HumanOperationRejection | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def exact_rejection(self):
        rejected = self.human_rejection
        if rejected is not None and (
            rejected.operation_id != self.operation_id
            or rejected.description_digest != self.description_digest
            or rejected.adapter_contract_digest != self.adapter_contract_digest
            or self.current_attempt_id is not None or self.unsettled_attempt_ids
            or self.effect_state != "unknown" or self.business_state != "pending"
            or self.successful_result is not None
        ):
            raise ValueError("Human rejection must retain its exact unattempted operation")
        return self


class _Attempt(_Model):
    kind: Literal["operation_attempt"] = "operation_attempt"
    binding: AttemptBinding
    context: ExecutionContext
    grant_id: Identifier
    grant_revocation_revision: Count
    policy_revision: Count
    reserved_units: Count
    authorized_at: datetime
    not_after: datetime
    basis: Literal["first_attempt", "terminal_not_applied", "whole_unit_idempotency"]
    effect_state: Effect = "unknown"
    observation_sequence: Count = 0
    terminality_contract_version: Version | None = None

    _times = field_validator("authorized_at", "not_after")(_utc)


M = TypeVar("M", bound=StrictModel)
Snapshot = Mapping[str, Record | None]


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _checked(value: M, model: type[M]) -> M:
    if not isinstance(value, model):
        raise Problem(422, "OPERATION_INVALID_DATA", "a typed operation contract is required")
    try:
        validated = model.model_validate(value.model_dump())
        _json(validated.model_dump(mode="json"))
        return validated
    except (ValidationError, TypeError, ValueError, UnicodeError):
        raise Problem(422, "OPERATION_INVALID_DATA", "operation contract validation failed") from None


def _parse(value: object, model: type[M]) -> M:
    try:
        return _checked(model.model_validate_json(_json(value)), model)
    except (ValidationError, TypeError, ValueError, UnicodeError):
        raise Problem(502, "OPERATION_RECORD_INVALID", "stored or adapter contract validation failed") from None


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def control_key(kind: str, *identifiers: str) -> str:
    """Deterministic 64-character row IDs; never a raw Table key or partition."""
    return _digest(["fa-operation-control-v1", kind, *identifiers])


def canonical_material(description: OperationDescription) -> InlineMaterial:
    description = _checked(description, OperationDescription)
    value = description.model_dump(mode="json")
    for field in ("resources", "dependencies"):
        value[field] = sorted(value[field], key=lambda item: item["resource_id"])
    try:
        data = _json(value)
        return InlineMaterial(data=data, sha256=hashlib.sha256(data).hexdigest())
    except (ValidationError, UnicodeError):
        raise Problem(413, "OPERATION_CONTENT_LIMIT", "canonical inline content exceeds the supported contract") from None


def _row(snapshot: Snapshot, key: str, model: type[M]) -> M:
    record = snapshot[key]
    if record is None:
        raise Problem(409, "OPERATION_RECORD_MISSING", "a required trusted control record is missing")
    return _parse(record.payload["data"], model)


def _write(key: str, value: _Model, mode: Literal["create", "replace"], material=None) -> ControlWrite:
    return ControlWrite(row_id=key, mode=mode, data=value.model_dump(mode="json"), material=material)


def _updated(value: M, **updates: object) -> M:
    return type(value).model_validate({**value.model_dump(), **updates})


class ToolBrokerCore:
    def __init__(
        self, store: BehaviorStore, *, clock: Callable[[], datetime],
        adapter: RegisteredAdapter | None = None,
        execution_adapter: ContextArtifactAdapter | WorkStateEffectAdapter | None = None,
    ):
        self.store, self.clock = store, clock
        self._adapter = adapter
        self._contract = None if adapter is None else _checked(adapter.contract, AdapterContract)
        self.dispatcher = None
        if execution_adapter is not None:
            from .behavior_dispatch import BehaviorDispatcher
            from .behavior_effects import WorkStateDispatcher, WorkStateEffectAdapter

            dispatcher = WorkStateDispatcher if type(execution_adapter) is WorkStateEffectAdapter else BehaviorDispatcher
            self.dispatcher = dispatcher(self, execution_adapter)

    def _enabled(self) -> tuple[RegisteredAdapter, AdapterContract]:
        if self._adapter is None or self._contract is None:
            raise Problem(403, "OPERATION_CAPABILITY_DISABLED", "no trusted versioned adapter is registered")
        return self._adapter, self._contract

    def _now(self) -> datetime:
        try:
            return _utc(self.clock())
        except ValueError:
            raise Problem(503, "OPERATION_CLOCK_INVALID", "trusted UTC is unavailable") from None

    def _contract_digest(self) -> str:
        return _digest(self._enabled()[1].model_dump(mode="json"))

    def _description(self, material: InlineMaterial) -> OperationDescription:
        try:
            description = _checked(OperationDescription.model_validate_json(material.data), OperationDescription)
        except ValidationError:
            raise Problem(502, "OPERATION_CONTENT_INVALID", "saved execution content is invalid") from None
        contract = self._enabled()[1]
        if (
            description.adapter_id != contract.adapter_id
            or description.adapter_version != contract.adapter_version
            or description.kind not in contract.allowed_kinds
            or description.method not in contract.allowed_methods
            or description.target not in contract.allowed_targets
            or canonical_material(description) != material
        ):
            raise Problem(403, "OPERATION_CONTENT_DENIED", "saved content differs from the pinned adapter contract")
        return description

    def _owner(self, snapshot: Snapshot, context: ExecutionContext, operation_id: str) -> None:
        owner = _row(snapshot, control_key("owner", context.owner_record_id), OwnerRecord)
        if (
            not owner.active or operation_id not in owner.operation_ids
            or any(getattr(owner, field) != getattr(context, field) for field in ExecutionContext.model_fields)
        ):
            raise Problem(403, "OPERATION_OWNER_DENIED", "current owner, epoch or authenticated scope differs")

    def _operation(self, snapshot: Snapshot, operation_id: str, context: ExecutionContext):
        key = control_key("operation", operation_id)
        operation = _row(snapshot, key, _Operation)
        if (
            operation.operation_id != operation_id
            or operation.owner_record_id != context.owner_record_id
            or operation.conversation_id != context.conversation_id
            or operation.root_budget_id != context.root_budget_id
            or operation.adapter_contract_digest != self._contract_digest()
        ):
            raise Problem(403, "OPERATION_BINDING_DENIED", "operation ownership or adapter binding differs")
        record = snapshot[key]
        assert record is not None
        material = _parse(record.payload["material"], InlineMaterial)
        if operation.description_digest != material.sha256:
            raise Problem(502, "OPERATION_CONTENT_INVALID", "saved description digest differs")
        return operation, material, self._description(material)

    def reconciliation_request(
        self, request: ToolIntent | AuthorizationRequest | OutcomeRequest, context: ExecutionContext,
    ) -> ControlRequest:
        """Persist this stable request BEFORE calling a transition; read-back only."""
        context = _checked(context, ExecutionContext)
        self._enabled()
        if isinstance(request, ToolIntent):
            request = _checked(request, ToolIntent)
            action, identity = "prepare", (request.operation_id,)
            reads = [control_key("operation", request.operation_id), control_key("owner", context.owner_record_id)]
        elif isinstance(request, AuthorizationRequest):
            request = _checked(request, AuthorizationRequest)
            action, identity = "authorize", (request.operation_id, request.attempt_id)
            reads = [
                control_key("operation", request.operation_id),
                control_key("attempt", request.operation_id, request.attempt_id),
                control_key("owner", context.owner_record_id), control_key("grant", request.grant_id),
                control_key("policy", context.policy_id), control_key("budget", context.root_budget_id),
            ]
            if request.previous_attempt_id is not None:
                reads.append(control_key("attempt", request.operation_id, request.previous_attempt_id))
        elif isinstance(request, OutcomeRequest):
            request = _checked(request, OutcomeRequest)
            action, identity = "outcome", (request.operation_id, request.attempt_id, request.evidence_id)
            reads = [
                control_key("operation", request.operation_id),
                control_key("attempt", request.operation_id, request.attempt_id),
                control_key("owner", context.owner_record_id),
            ]
        else:
            raise Problem(422, "OPERATION_INVALID_DATA", "unknown operation transition")
        return ControlRequest(
            request_id=control_key(action, *identity),
            request_digest=_digest([action, request.model_dump(mode="json"), context.model_dump(mode="json"), self._contract_digest()]),
            read_set=tuple(sorted(set(reads))),
        )

    def prepare(self, intent: ToolIntent, context: ExecutionContext) -> Record:
        request = self.reconciliation_request(intent, context)
        intent, context = _checked(intent, ToolIntent), _checked(context, ExecutionContext)
        material = canonical_material(self._enabled()[0].resolve(intent.input_ref))
        self._description(material)
        key = control_key("operation", intent.operation_id)

        def decide(snapshot: Snapshot) -> ControlChanges:
            self._owner(snapshot, context, intent.operation_id)
            if snapshot[key] is not None:
                raise Problem(409, "OPERATION_ID_CONFLICT", "operation ID is already bound")
            operation = _Operation(
                operation_id=intent.operation_id, owner_record_id=context.owner_record_id,
                conversation_id=context.conversation_id, root_budget_id=context.root_budget_id,
                description_digest=material.sha256, adapter_contract_digest=self._contract_digest(),
            )
            return ControlChanges(writes=(_write(key, operation, "create", material),), result={
                "operation_id": intent.operation_id, "description_digest": material.sha256,
                "adapter_contract_digest": operation.adapter_contract_digest,
                "content": material.model_dump(mode="json"),
                "canonicalization_version": CANONICALIZATION_VERSION, "execution_enabled": False,
            })

        receipt = self.store.transact(request, decide)
        if receipt.payload["result"]["description_digest"] != material.sha256:
            raise Problem(409, "OPERATION_ID_CONFLICT", "operation ID is bound to different resolved content")
        return receipt

    def _idempotency(self, evidence_id: str | None) -> IdempotencyEvidence | None:
        if evidence_id is None:
            return None
        adapter, contract = self._enabled()
        if contract.idempotency_contract_version is None or adapter.verify_idempotency is None:
            raise Problem(403, "OPERATION_RETRY_DISABLED", "destination idempotency verification is not installed")
        evidence = _checked(adapter.verify_idempotency(evidence_id), IdempotencyEvidence)
        if (
            evidence.evidence_id != evidence_id or evidence.source != contract.evidence_source
            or evidence.contract_version != contract.idempotency_contract_version
        ):
            raise Problem(403, "OPERATION_EVIDENCE_DENIED", "destination idempotency contract differs")
        return evidence

    def _retry(
        self, previous: _Attempt | None, operation: _Operation,
        evidence: IdempotencyEvidence | None, now: datetime,
    ) -> Literal["first_attempt", "terminal_not_applied", "whole_unit_idempotency"]:
        if previous is None:
            if operation.current_attempt_id is not None or evidence is not None:
                raise Problem(409, "OPERATION_ATTEMPT_CONFLICT", "original attempt must be identified")
            return "first_attempt"
        if previous.effect_state in {"applied", "partial"} or operation.effect_state in {"applied", "partial"}:
            raise Problem(409, "OPERATION_RETRY_UNSAFE", "known effects cannot be replayed as a whole operation")
        contract = self._enabled()[1]
        if previous.effect_state == "not_applied" and not operation.unsettled_attempt_ids:
            if contract.terminality_contract_version is not None and previous.terminality_contract_version == contract.terminality_contract_version:
                return "terminal_not_applied"
            raise Problem(403, "OPERATION_RETRY_DISABLED", "terminality contract is unavailable")
        if evidence is None or evidence.binding != previous.binding:
            raise Problem(409, "OPERATION_RETRY_UNSAFE", "original delivery may still apply")
        if not evidence.not_before <= now < evidence.expires_at:
            raise Problem(409, "OPERATION_RETRY_UNSAFE", "destination idempotency window is not valid")
        return "whole_unit_idempotency"

    def _grant_budget(self, snapshot: Snapshot, command: AuthorizationRequest, context: ExecutionContext,
                      description_digest: Digest, now: datetime, units: int) -> tuple[GrantRecord, PolicyRecord, BudgetRecord]:
        grant = _row(snapshot, control_key("grant", command.grant_id), GrantRecord)
        policy = _row(snapshot, control_key("policy", context.policy_id), PolicyRecord)
        budget = _row(snapshot, control_key("budget", context.root_budget_id), BudgetRecord)
        if (
            grant.grant_id != command.grant_id or grant.revoked
            or GrantSubject(principal_id=context.principal_id, conversation_id=context.conversation_id) not in grant.subjects
            or grant.root_budget_id != context.root_budget_id or grant.description_digest != description_digest
            or grant.adapter_contract_digest != self._contract_digest()
            or grant.policy_id != context.policy_id or policy.policy_id != context.policy_id
            or not policy.active or grant.policy_revision != policy.revision
            or not grant.not_before <= now < grant.expires_at
        ):
            raise Problem(403, "OPERATION_GRANT_DENIED", "current human grant or policy does not permit this operation")
        if budget.root_budget_id != context.root_budget_id or not budget.active or (
            min(grant.remaining_uses, budget.remaining_attempts) < 1
            or min(grant.remaining_units, budget.remaining_units) < units
        ):
            raise Problem(403, "OPERATION_BUDGET_DENIED", "shared grant or root budget is exhausted")
        return grant, policy, budget

    def authorize(self, command: AuthorizationRequest, context: ExecutionContext) -> Record:
        request = self.reconciliation_request(command, context)
        command, context = _checked(command, AuthorizationRequest), _checked(context, ExecutionContext)
        evidence = self._idempotency(command.idempotency_evidence_id)
        contract = self._enabled()[1]
        fresh = False

        def decide(snapshot: Snapshot) -> ControlChanges:
            nonlocal fresh
            fresh = True  # Response classification only, NOT a concurrency authority.
            self._owner(snapshot, context, command.operation_id)
            operation, material, description = self._operation(snapshot, command.operation_id, context)
            if operation.human_rejection is not None:
                raise Problem(409, "OPERATION_HUMAN_REJECTED", "the original operation was declined by its human")
            if operation.current_attempt_id != command.previous_attempt_id or command.attempt_id == command.previous_attempt_id:
                raise Problem(409, "OPERATION_ATTEMPT_CONFLICT", "attempt chain differs")
            attempt_key = control_key("attempt", command.operation_id, command.attempt_id)
            if snapshot[attempt_key] is not None:
                raise Problem(409, "OPERATION_ATTEMPT_CONFLICT", "attempt ID is already bound")
            previous = None
            if command.previous_attempt_id is not None:
                previous = _row(snapshot, control_key("attempt", command.operation_id, command.previous_attempt_id), _Attempt)
                if (
                    previous.binding.operation_id != command.operation_id
                    or previous.binding.attempt_id != command.previous_attempt_id
                    or previous.binding.description_digest != material.sha256
                    or previous.binding.adapter_contract_digest != self._contract_digest()
                    or previous.binding.target != description.target
                ):
                    raise Problem(409, "OPERATION_ATTEMPT_CONFLICT", "previous attempt binding differs")
            now = self._now()
            basis = self._retry(previous, operation, evidence, now)
            if len(operation.unsettled_attempt_ids) >= 16:
                raise Problem(409, "OPERATION_ATTEMPT_LIMIT", "unsettled attempt capacity is exhausted")
            grant_key, budget_key = control_key("grant", command.grant_id), control_key("budget", context.root_budget_id)
            units = contract.reservation_units
            grant, policy, budget = self._grant_budget(snapshot, command, context, material.sha256, now, units)
            not_after = grant.expires_at
            if basis == "whole_unit_idempotency":
                assert evidence is not None
                not_after = min(not_after, evidence.expires_at)
            binding = AttemptBinding(
                operation_id=command.operation_id, attempt_id=command.attempt_id,
                description_digest=material.sha256, adapter_contract_digest=self._contract_digest(),
                target=description.target, idempotency_key=control_key("idempotency", command.operation_id, material.sha256),
            )
            if previous is not None and binding.idempotency_key != previous.binding.idempotency_key:
                raise Problem(409, "OPERATION_ATTEMPT_CONFLICT", "retry key differs")
            attempt = _Attempt(
                binding=binding, context=context, grant_id=command.grant_id,
                grant_revocation_revision=grant.revocation_revision, policy_revision=policy.revision,
                reserved_units=units, authorized_at=now, not_after=not_after, basis=basis,
            )
            evidence_material = None
            if evidence is not None:
                data = _json(evidence.model_dump(mode="json"))
                evidence_material = InlineMaterial(data=data, sha256=hashlib.sha256(data).hexdigest())
            return ControlChanges(writes=(
                _write(control_key("operation", command.operation_id), _updated(
                    operation, current_attempt_id=command.attempt_id, effect_state="unknown", business_state="pending",
                    unsettled_attempt_ids=(*operation.unsettled_attempt_ids, command.attempt_id),
                ), "replace", material),
                _write(attempt_key, attempt, "create", evidence_material),
                _write(grant_key, _updated(grant, remaining_uses=grant.remaining_uses - 1, remaining_units=grant.remaining_units - units), "replace"),
                _write(budget_key, _updated(budget, remaining_attempts=budget.remaining_attempts - 1, remaining_units=budget.remaining_units - units), "replace"),
            ), result={
                "attempt": attempt.model_dump(mode="json"), "content": material.model_dump(mode="json"),
                "execution_enabled": False,
            })

        receipt = self.store.transact(request, decide)
        if fresh:
            attempt = _parse(receipt.payload["result"]["attempt"], _Attempt)
            after_commit = self._now()
            if not attempt.authorized_at <= after_commit < attempt.not_after:
                raise Problem(
                    409, "OPERATION_AUTHORIZATION_WINDOW_CLOSED",
                    "reservation remains held; no dispatch permitted after an uncertain authorization window",
                    request_id=request.request_id, request_digest=request.request_digest,
                )
        return receipt

    def record_outcome(self, command: OutcomeRequest, context: ExecutionContext) -> Record:
        if self.dispatcher is not None:
            return self.dispatcher.record_outcome(command, context)
        self.reconciliation_request(command, context)
        command, context = _checked(command, OutcomeRequest), _checked(context, ExecutionContext)
        adapter, _ = self._enabled()
        if adapter.verify_outcome is None:
            raise Problem(403, "OPERATION_EVIDENCE_DISABLED", "destination outcome verification is not installed")
        evidence = _checked(adapter.verify_outcome(command.evidence_id), VerifiedOutcome)
        return self._record_verified_outcome(command, context, evidence)

    def _record_verified_outcome(
        self, command: OutcomeRequest, context: ExecutionContext, evidence: VerifiedOutcome,
        *, extra_reads: tuple[str, ...] = (),
        extend: Callable[[Snapshot, ControlChanges], ControlChanges] | None = None,
    ) -> Record:
        """Internal credential-boundary handoff; callers cannot submit raw evidence."""
        request = self.reconciliation_request(command, context)
        command, context = _checked(command, OutcomeRequest), _checked(context, ExecutionContext)
        evidence = _checked(evidence, VerifiedOutcome)
        contract = self._enabled()[1]
        if extra_reads:
            request = _updated(request, read_set=tuple(sorted(set((*request.read_set, *extra_reads)))))
        if (
            evidence.evidence_id != command.evidence_id or evidence.source != contract.evidence_source
            or evidence.contract_version != contract.outcome_contract_version
        ):
            raise Problem(403, "OPERATION_EVIDENCE_DENIED", "outcome source or contract differs")
        terminal = evidence.observation in TERMINAL
        if terminal and (
            contract.terminality_contract_version is None
            or evidence.terminality_contract_version != contract.terminality_contract_version
        ):
            raise Problem(403, "OPERATION_EVIDENCE_DENIED", "destination terminality has not been established")
        effect: Effect = "not_applied" if terminal else (
            "applied" if evidence.observation == "applied" else "partial" if evidence.observation == "partial" else "unknown"
        )
        data = _json(evidence.model_dump(mode="json"))
        proof_material = InlineMaterial(data=data, sha256=hashlib.sha256(data).hexdigest())

        def decide(snapshot: Snapshot) -> ControlChanges:
            self._owner(snapshot, context, command.operation_id)
            operation, material, description = self._operation(snapshot, command.operation_id, context)
            attempt_key = control_key("attempt", command.operation_id, command.attempt_id)
            attempt = _row(snapshot, attempt_key, _Attempt)
            if (
                evidence.binding != attempt.binding
                or attempt.binding.operation_id != command.operation_id
                or attempt.binding.attempt_id != command.attempt_id
                or attempt.binding.description_digest != material.sha256
                or attempt.binding.adapter_contract_digest != self._contract_digest()
                or attempt.binding.target != description.target
                or attempt.binding.idempotency_key != control_key("idempotency", command.operation_id, material.sha256)
                or evidence.sequence <= attempt.observation_sequence or evidence.observed_at > self._now()
                or evidence.result is not None and evidence.result.resource not in description.resources
            ):
                raise Problem(409, "OPERATION_EVIDENCE_DENIED", "outcome is stale or bound to a different attempt")
            if (
                attempt.effect_state == "applied" and effect != "applied"
                or attempt.effect_state == "not_applied" and effect != "not_applied"
                or attempt.effect_state == "partial" and effect not in {"partial", "applied"}
            ):
                raise Problem(409, "OPERATION_EVIDENCE_CONFLICT", "outcome contradicts already recorded effects")
            attempt_record = snapshot[attempt_key]
            assert attempt_record is not None
            old_material = attempt_record.payload["material"]
            retained = None if old_material is None else _parse(old_material, InlineMaterial)
            unsettled = operation.unsettled_attempt_ids
            if effect in {"applied", "not_applied"}:
                unsettled = tuple(item for item in unsettled if item != command.attempt_id)
            aggregate = operation.effect_state
            if effect in {"applied", "partial"}:
                aggregate = effect
            elif aggregate not in {"applied", "partial"}:
                aggregate = "unknown" if unsettled else "not_applied"
            successful_result = operation.successful_result
            if evidence.business_result == "succeeded":
                successful_result = evidence.result
            business = "pending"
            if not unsettled and aggregate not in {"unknown", "partial"}:
                if aggregate == "applied" and successful_result is not None:
                    business = "succeeded"
                elif evidence.business_result == "failed" or operation.business_state == "failed":
                    business = "failed"
            changes = ControlChanges(writes=(
                _write(control_key("operation", command.operation_id), _updated(
                    operation, effect_state=aggregate, business_state=business,
                    unsettled_attempt_ids=unsettled, successful_result=successful_result,
                ), "replace", material),
                _write(attempt_key, _updated(
                    attempt, effect_state=effect, observation_sequence=evidence.sequence,
                    terminality_contract_version=evidence.terminality_contract_version if terminal else None,
                ), "replace", retained),
                _write(control_key("outcome", command.operation_id, command.attempt_id, command.evidence_id), evidence, "create", proof_material),
            ), result={
                "operation_id": command.operation_id, "attempt_id": command.attempt_id,
                "effect_state": effect, "business_result": evidence.business_result,
                "operation_effect_state": aggregate, "operation_business_state": business,
                "evidence_digest": proof_material.sha256, "execution_enabled": False,
            })
            return changes if extend is None else extend(snapshot, changes)

        receipt = self.store.transact(request, decide)
        if receipt.payload["result"]["evidence_digest"] != proof_material.sha256:
            raise Problem(409, "OPERATION_EVIDENCE_CONFLICT", "evidence ID is bound to different verified content")
        return receipt

    def execute(
        self, operation_id: str, attempt_id: str, *,
        context: ExecutionContext | None = None, grant_id: str | None = None,
    ) -> Record:
        if self.dispatcher is None:
            raise Problem(403, "OPERATION_EXECUTION_DISABLED", "no concrete execution adapter is configured")
        if context is None or grant_id is None:
            raise Problem(403, "OPERATION_EXECUTION_CONTEXT_REQUIRED", "server identity and a grant reference are required")
        return self.dispatcher.execute(AuthorizationRequest(
            operation_id=operation_id, attempt_id=attempt_id, grant_id=grant_id,
        ), context)
