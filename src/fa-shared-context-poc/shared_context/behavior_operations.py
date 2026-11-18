"""Operation records, canonical operation material and the broker's read side.

The embedding server supplies an authenticated ExecutionContext, a trusted UTC clock and one version-pinned
RegisteredAdapter; none comes from model arguments. Owner records contain server-assigned stable operation IDs.
prepare/authorize/execute and outcome recording were retired with the legacy job lane (2026-11-17,
DECISIONS §12): no route or runtime caller remains. The record models stay so stored rows still parse, and the
GET operation preview and decision state read them. actual_environment=NOT_RUN.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated, Literal, TypeVar

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from .behavior_store import BehaviorStore, ControlWrite, Digest, InlineMaterial
from .errors import Problem
from .models import Identifier, StrictModel
from .records import Record

if TYPE_CHECKING:
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


def conversation_key(conversation_id):
    return control_key("assistant-conversation", conversation_id)


def _read_conflicts(read, *, phase):
    """Only explicitly selected read-only gates may repeat; never a tool/dispatch."""
    for attempt in range(3):
        try:
            return read()
        except Problem as error:
            if error.status != 412 or error.body["error"]["code"] != "BEHAVIOR_CONFLICT" or attempt == 2:
                raise
            logging.getLogger(__name__).warning("ASSISTANT_READ_CONFLICT phase=%s attempt=%s maximum=3", phase, attempt + 1)


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


_OBSERVED_EFFECT: dict[str, Effect] = {"applied": "applied", "partial": "partial"}
# The effects that may follow an attempt's recorded effect; known effects never move back.
_LATER_EFFECTS: dict[str, frozenset[str]] = {
    "unknown": frozenset({"unknown", "not_applied", "applied", "partial"}),
    "not_applied": frozenset({"not_applied"}), "applied": frozenset({"applied"}),
    "partial": frozenset({"partial", "applied"}),
}


def _settled(operation: _Operation, attempt_id: str, effect: Effect, evidence: VerifiedOutcome) -> _Operation:
    unsettled = operation.unsettled_attempt_ids
    if effect in {"applied", "not_applied"}:
        unsettled = tuple(item for item in unsettled if item != attempt_id)
    aggregate = operation.effect_state
    if effect in {"applied", "partial"}:
        aggregate = effect
    elif aggregate not in {"applied", "partial"}:
        aggregate = "unknown" if unsettled else "not_applied"
    successful_result = evidence.result if evidence.business_result == "succeeded" else operation.successful_result
    return _updated(
        operation, effect_state=aggregate,
        business_state=_business_state(operation, evidence, aggregate, unsettled, successful_result),
        unsettled_attempt_ids=unsettled, successful_result=successful_result,
    )


def _business_state(
    operation: _Operation, evidence: VerifiedOutcome, aggregate: Effect,
    unsettled: tuple[str, ...], successful_result: ReceiptResult | None,
) -> str:
    if unsettled or aggregate in {"unknown", "partial"}:
        return "pending"
    if aggregate == "applied" and successful_result is not None:
        return "succeeded"
    if evidence.business_result == "failed" or operation.business_state == "failed":
        return "failed"
    return "pending"


class ToolBrokerCore:
    def __init__(
        self, store: BehaviorStore, *, clock: Callable[[], datetime],
        adapter: RegisteredAdapter | None = None,
        execution_adapter: WorkStateEffectAdapter | None = None,
    ):
        self.store, self.clock = store, clock
        self._adapter = adapter
        self._contract = None if adapter is None else _checked(adapter.contract, AdapterContract)
        self.dispatcher = None
        if execution_adapter is not None:
            from .behavior_effects import WorkStateDispatcher

            # Any other adapter type is refused by BehaviorDispatcher.__init__ (403 OPERATION_EXECUTION_DISABLED).
            self.dispatcher = WorkStateDispatcher(self, execution_adapter)

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

