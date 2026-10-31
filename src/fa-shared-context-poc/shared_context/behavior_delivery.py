"""Bounded admission/obligation/outbox contract core. actual_environment=NOT_RUN.

Model invocation is CLOSED. The supported native channel is
the authenticated PullInbox, with a durable, non-model DeliveryConsumer below.
Native admission additionally requires that concrete adapter and independently
verified, persisted deployment readiness. Offline registrations remain explicit;
none of the local tests constitutes native environment evidence.

The server supplies ExecutionContext, UTC, immutable verifier handles and the
registration. IncomingEvent has no actor/source/role/approval fields. Verifiers
must authenticate, scope-check and sanitize before returning the narrow evidence
models; their functions are never tool callbacks or arbitrary network executors.
Source provenance does not grant permission to execute an operation.

Provision finite QuotaRecord and PendingIndexRecord rows through the SAME
BehaviorStore authority as operations.OwnerRecord/PolicyRecord/BudgetRecord.
All predicate writers must advance that guard. request_for() is side-effect-free:
persist its original ControlRequest before a transition. On conflict/unknown use
store.reconcile(original_request), not a new ID or an automatic resubmit loop.
Historical receipts are not new work, dispatch permission, or proof of delivery.

One admission creates one new work and obligation. Completion uses verified work
evidence, not job status from a model. Ack/progress never fulfill. One immutable
final/failure is supported per obligation; rerouting, final replacement and
unknown-send retry are deliberately unsupported. Receipts for older ack/progress
versions can be retained without fulfilling the current final.

Normal control, attention, deterministic notification and fulfillment credits
are reserved separately.
No reservation is refunded on failure/unknown. These are finite logical credits,
not a storage-capacity, cost, fairness or F18-F20 guarantee.

Root indexes hold at most 32 pending obligations. Scans have persisted page
budgets, revision-bound cursors and bounded pages. Due IDs are obligation +
processing generation; completion does not fulfill or reset model attempts.
Processing intents are immutable references, not duplicate mutable states.
Consumers must recheck their state row AND the obligation's current owner,
generation and fulfillment; a fulfilled obligation makes old intents inert.
All materials/receipts are inline and never deleted. There is no Blob pin/GC
protocol. behavior_workers provides bounded executor scheduling; the embedding
server owns authentication, supervision, and native adapter installation.

New public Interfaces:
  authority_snapshot(store, keys) -> Mapping[str, Record | None]
  DeliveryConsumer(core).consume(obligation_id, context, worker_id) -> dict
  DeliveryConsumer(core).drain(root_budget_id, resolve_context, worker_id,
                               limit=4) -> tuple[dict, ...]
  The server may pass its exact pending_binding for the native assistant C1
  pull profile. A complete current eligibility read can defer a new nonready
  page without writing or consuming scan quota. It never caches that decision,
  suppresses errors, fulfills an obligation, or changes an existing page.
New keys (all passed through control_key, inside the existing authority):
  delivery-consumer-plan(obligation_id): immutable original final command/request
  delivery-consumer-cursor(root_budget_id): resumable, rotating active page
  delivery-consumer-page(root_budget_id, sequence): retained scan intent
Existing admission intent, result, obligation, outbox and receipt rows remain the
authorities. A page consumes one existing root scans_remaining reservation.
Native and offline configurations have distinct configuration digests; synthetic
rows cannot be relabelled native by changing RuntimeRegistration.mode.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Annotated, Literal, TypeVar

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from .behavior_operations import BudgetRecord, ExecutionContext, OwnerRecord, PolicyRecord, control_key
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest, ControlWrite, Digest, InlineMaterial
from .errors import Problem
from .models import Identifier, StrictModel
from .records import Record

if TYPE_CHECKING:
    from .behavior_runtime import RuntimeBinding

RUNTIME_VERSION = "fa-delivery-contract-v1"
Count = Annotated[int, Field(ge=0, le=2**63 - 1)]
ShortText = Annotated[str, Field(min_length=1, max_length=1024)]
Content = Annotated[bytes, Field(min_length=1, max_length=8192)]
Level = Literal["persisted", "provider_accepted", "client_received", "ui_rendered", "human_read"]
MessageKind = Literal["ack", "progress", "final", "failure"]
SourceKind = Literal["human", "routine", "agent", "tool_result", "system"]
Snapshot = Mapping[str, Record | None]


class _Model(StrictModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False,
        ser_json_bytes="base64", val_json_bytes="base64",
    )


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timezone-aware server UTC is required")
    return value


class Route(_Model):
    tenant_id: Identifier
    account_id: Identifier
    recipient_id: Identifier
    channel_id: Identifier
    thread_id: Identifier


class ChannelContract(_Model):
    channel_id: Identifier
    version: Identifier
    supported_levels: Annotated[tuple[Level, ...], Field(min_length=1, max_length=5)]
    max_confirmation_level: Level

    @model_validator(mode="after")
    def declared_levels(self):
        if len(set(self.supported_levels)) != len(self.supported_levels) or self.max_confirmation_level not in self.supported_levels:
            raise ValueError("channel levels must be explicit and unique")
        return self


class RuntimeContract(_Model):
    runtime_version: Literal["fa-delivery-contract-v1"] = RUNTIME_VERSION
    control_binding: Digest
    team_id: Identifier
    entry_id: Identifier
    protection_version: Identifier
    consumer_version: Identifier
    timer_version: Identifier
    verifier_version: Identifier
    channel: ChannelContract
    max_queue_seconds: Annotated[int, Field(ge=1, le=3600)] = 300
    review_interval_seconds: Annotated[int, Field(ge=1, le=60)] = 10
    normal_control_units: Annotated[int, Field(ge=4, le=32)] = 8
    max_delivery_versions: Annotated[int, Field(ge=1, le=8)] = 4
    max_receipts_per_delivery: Annotated[int, Field(ge=1, le=8)] = 4
    max_due_generations: Annotated[int, Field(ge=1, le=8)] = 4
    scan_page_size: Annotated[int, Field(ge=1, le=16)] = 4
    model_repair_contract: Literal["offline-model-budget-v1"] | None = None
    model_repair_units: Annotated[int, Field(ge=1, le=1024)] = 1


class ReadinessEvidence(_Model):
    evidence_id: Identifier
    configuration_digest: Digest
    control_binding: Digest
    protection_version: Identifier
    consumer_version: Identifier
    timer_version: Identifier
    coverage: tuple[Literal[
        "atomic_control", "inline_retention", "durable_consumer",
        "timer_redrive", "bounded_scan", "ownership_recovery",
    ], ...]
    observed_at: datetime
    expires_at: datetime
    proof: Content

    _times = field_validator("observed_at", "expires_at")(_utc)


class IncomingEvent(_Model):
    schema_version: Literal[1] = 1
    runtime_version: Literal["fa-delivery-contract-v1"] = RUNTIME_VERSION
    event_id: Identifier
    root_request_id: Identifier
    conversation_id: Identifier
    causation_id: Identifier | None = None
    content: Content


class WorkDefinition(_Model):
    work_id: Identifier
    goal: ShortText
    criteria_version: Identifier
    result_kind: Literal["answer", "artifact", "operation", "mixed"]
    required_checks: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=16)]
    human_checks: Annotated[tuple[Identifier, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def checks(self):
        if len(set(self.required_checks)) != len(self.required_checks) or not set(self.human_checks) <= set(self.required_checks):
            raise ValueError("work checks must be unique and human checks must be required")
        return self


class AdmissionRequest(_Model):
    event: IncomingEvent
    work: WorkDefinition
    route: Route
    required_confirmation_level: Level
    deadline: datetime | None
    expected_sequence: Count
    ingress_evidence_id: Identifier

    _time = field_validator("deadline")(lambda value:None if value is None else _utc(value))


class VerifiedIngress(_Model):
    evidence_id: Identifier
    admission_digest: Digest
    verifier_version: Identifier
    actor_id: Identifier
    on_behalf_of_principal_id: Identifier
    source_kind: SourceKind
    policy_revision: Count
    observed_at: datetime
    expires_at: datetime
    proof: Content

    _times = field_validator("observed_at", "expires_at")(_utc)


class ArtifactVersion(_Model):
    artifact_id: Identifier
    version: Identifier
    sha256: Digest


class VerifiedWorkResult(_Model):
    evidence_id: Identifier
    verifier_version: Identifier
    work_id: Identifier
    work_revision: Literal[1] = 1
    definition_digest: Digest
    state: Literal["succeeded", "failed", "cancelled"]
    passed_checks: tuple[Identifier, ...]
    human_approved_checks: tuple[Identifier, ...] = ()
    known_effects: Annotated[tuple[Identifier, ...], Field(max_length=16)] = ()
    unresolved_operations: Annotated[tuple[Identifier, ...], Field(max_length=16)] = ()
    artifacts: Annotated[tuple[ArtifactVersion, ...], Field(max_length=16)] = ()
    content: Content
    proof: Content


class WorkResultRequest(_Model):
    obligation_id: Identifier
    evidence_id: Identifier


class MessageRequest(_Model):
    obligation_id: Identifier
    delivery_id: Identifier
    expected_version: Count
    message_kind: MessageKind


class DeliveryBinding(_Model):
    obligation_id: Identifier
    delivery_id: Identifier
    version: Annotated[int, Field(ge=1)]
    payload_digest: Digest
    route: Route
    channel_version: Identifier
    artifacts: tuple[ArtifactVersion, ...]


class VerifiedDeliveryReceipt(_Model):
    evidence_id: Identifier
    verifier_version: Identifier
    binding: DeliveryBinding
    send_outcome: Literal["emitted", "suppressed", "failed", "unknown"]
    confirmed_levels: Annotated[tuple[Level, ...], Field(max_length=5)]
    dispatch_authorization_ref: Identifier
    proof: Content

    @model_validator(mode="after")
    def levels(self):
        if len(set(self.confirmed_levels)) != len(self.confirmed_levels):
            raise ValueError("duplicate confirmation level")
        return self


class ReceiptRequest(_Model):
    obligation_id: Identifier
    delivery_id: Identifier
    evidence_id: Identifier


class DueRequest(_Model):
    obligation_id: Identifier
    generation: Count


class DueCompletion(DueRequest):
    pass


class ScanCursor(_Model):
    root_budget_id: Identifier
    owner_record_id: Identifier
    principal_id: Identifier
    conversation_id: Identifier
    epoch: Count
    index_revision: Count
    offset: Annotated[int, Field(ge=0, le=32)]


class ScanRequest(_Model):
    scan_id: Identifier
    cursor: ScanCursor | None = None


@dataclass(frozen=True)
class RuntimeRegistration:
    """Server-owned registration; callbacks alone cannot enable native delivery."""

    contract: RuntimeContract
    mode: Literal["native", "offline_contract"] = "native"
    verify_readiness: Callable[[], ReadinessEvidence] | None = None
    verify_ingress: Callable[[str], VerifiedIngress] | None = None
    verify_work_result: Callable[[str], VerifiedWorkResult] | None = None
    verify_receipt: Callable[[str], VerifiedDeliveryReceipt] | None = None
    channel_adapter: object | None = None


class _Bound(_Model):
    schema_version: Literal[1] = 1
    runtime_version: Literal["fa-delivery-contract-v1"] = RUNTIME_VERSION
    evidence_mode: Literal["offline_contract", "native"] = "offline_contract"
    configuration_digest: Digest


class QuotaRecord(_Bound):
    kind: Literal["delivery_quota"] = "delivery_quota"
    scope_kind: Literal["team", "entry", "root"]
    scope_id: Identifier
    pending_limit: Annotated[int, Field(ge=1, le=32)]
    pending: Annotated[int, Field(ge=0, le=32)] = 0
    root_request_id: Identifier | None = None
    available_control_units: Count = 0
    scans_remaining: Annotated[int, Field(ge=0, le=1000)] = 0

    @model_validator(mode="after")
    def capacity(self):
        if self.pending > self.pending_limit or (self.scope_kind == "root") != (self.root_request_id is not None):
            raise ValueError("invalid pending/root quota configuration")
        return self


class PendingEntry(_Model):
    obligation_id: Identifier
    owner_record_id: Identifier
    conversation_id: Identifier
    review_at: datetime
    generation: Count
    automation: Literal["active", "needs_attention", "stopped"]
    due_pending: bool
    instruction: bool = Field(default=False,exclude_if=lambda value:not value)

    _time = field_validator("review_at")(_utc)


class PendingIndexRecord(_Bound):
    kind: Literal["delivery_pending_index"] = "delivery_pending_index"
    root_budget_id: Identifier
    revision: Count = 0
    entries: tuple[PendingEntry, ...] = ()

    @model_validator(mode="after")
    def unique(self):
        if len({item.obligation_id for item in self.entries}) != len(self.entries):
            raise ValueError("duplicate obligation in index")
        return self


class _Conversation(_Bound):
    kind: Literal["delivery_conversation"] = "delivery_conversation"
    conversation_id: Identifier
    owner_record_id: Identifier
    epoch: Count
    sequence: Count


class _Work(_Bound):
    kind: Literal["delivery_work"] = "delivery_work"
    event_id: Identifier
    definition: WorkDefinition
    definition_digest: Digest
    state: Literal["queued", "succeeded", "failed", "cancelled"] = "queued"
    result: VerifiedWorkResult | None = None


class _Obligation(_Bound):
    kind: Literal["delivery_obligation"] = "delivery_obligation"
    obligation_id: Identifier
    event_id: Identifier
    root_request_id: Identifier
    work_id: Identifier
    owner_record_id: Identifier
    conversation_id: Identifier
    root_budget_id: Identifier
    policy_revision: Count
    route: Route
    required_confirmation_level: Level
    channel_version: Identifier
    fulfillment: Literal["pending", "fulfilled"] = "pending"
    delivery_state: Literal["pending", "acked", "final_prepared", "delivery_unknown", "delivery_failed", "fulfilled"] = "pending"
    last_version: Count = 0
    final_delivery_id: Identifier | None = None
    final_version: Count = 0
    fulfillment_receipt_id: Identifier | None = None
    confirmed_levels: tuple[Level, ...] = ()
    review_at: datetime
    deadline: datetime | None
    generation: Count = 0
    due_pending: bool = False
    automation: Literal["active", "needs_attention", "stopped"] = "active"
    attention_reason: Identifier | None = None
    normal_control_remaining: Count | None
    attention_credit: Annotated[int, Field(ge=0, le=1)] = 1
    notification_credit: Annotated[int, Field(ge=0, le=1)] = 1
    fulfillment_credit: Annotated[int, Field(ge=0, le=1)] = 1
    model_attempts_reserved: Annotated[int, Field(ge=0, le=2)] = 0

    _times = field_validator("review_at", "deadline")(lambda value:None if value is None else _utc(value))


class _Payload(_Model):
    schema_version: Literal[1] = 1
    event_id: Identifier
    root_request_id: Identifier
    work_id: Identifier
    message_kind: MessageKind
    work_state: Literal["queued", "succeeded", "failed", "cancelled"]
    result_evidence_id: Identifier | None
    content: bytes | None
    artifacts: tuple[ArtifactVersion, ...]
    known_effects: tuple[Identifier, ...] | None
    unresolved_operations: tuple[Identifier, ...] | None
    responsibility_owner: Identifier
    notification_code: Literal["request_admitted", "work_status", "verified_result", "delivery_needs_attention"]


class _Outbox(_Bound):
    kind: Literal["delivery_outbox"] = "delivery_outbox"
    binding: DeliveryBinding
    message_kind: MessageKind
    send_outcome: Literal["emitted", "suppressed", "failed", "unknown"] | None = None
    confirmed_levels: tuple[Level, ...] = ()
    receipt_count: Count = 0


class _Intent(_Bound):
    kind: Literal["delivery_processing_intent"] = "delivery_processing_intent"
    intent_id: Identifier
    obligation_id: Identifier
    action: Literal["process_event", "review", "use_saved_result", "reconcile_delivery", "needs_attention"]
    consumer_version: Identifier
    generation: Count
    state_row_id: Identifier


class _Due(_Bound):
    kind: Literal["delivery_due"] = "delivery_due"
    obligation_id: Identifier
    generation: Count
    intent_id: Identifier
    action: Literal["review", "use_saved_result", "reconcile_delivery", "needs_attention"]
    state: Literal["pending", "done"] = "pending"


M = TypeVar("M", bound=StrictModel)
Command = AdmissionRequest | WorkResultRequest | MessageRequest | ReceiptRequest | DueRequest | ScanRequest


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _check(value: M, model: type[M]) -> M:
    if not isinstance(value, model):
        raise Problem(422, "DELIVERY_INVALID_DATA", "a typed delivery contract is required")
    try:
        checked = model.model_validate(value.model_dump())
        _json(checked.model_dump(mode="json"))
        return checked
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise Problem(422, "DELIVERY_INVALID_DATA", "delivery contract validation failed") from None


def _command(value: M, model: type[M]) -> M:
    if type(value) is not model:
        raise Problem(422, "DELIVERY_INVALID_DATA", "command type does not match the transition")
    return _check(value, model)


def _parse(value: object, model: type[M]) -> M:
    try:
        return _check(model.model_validate_json(_json(value)), model)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise Problem(502, "DELIVERY_RECORD_INVALID", "stored delivery contract is invalid") from None


def _update(value: M, **updates: object) -> M:
    return type(value).model_validate({**value.model_dump(), **updates})


def _material(value: object) -> InlineMaterial:
    data = _json(value)
    try:
        return InlineMaterial(data=data, sha256=hashlib.sha256(data).hexdigest())
    except ValidationError:
        raise Problem(413, "DELIVERY_MATERIAL_LIMIT", "compliant inline material exceeds the supported limit") from None


def obligation_id_for(event_id: str) -> str:
    return control_key("delivery-obligation-id", event_id)


def admission_digest(command: AdmissionRequest) -> str:
    """Content/plan binding, independent of a writer's current sequence."""
    command = _check(command, AdmissionRequest)
    return _digest(command.model_dump(mode="json", exclude={"expected_sequence"}))


class DeliveryCore:
    def __init__(
        self, store: BehaviorStore, *, clock: Callable[[], datetime],
        registration: RuntimeRegistration | None = None,
    ):
        self.store, self.clock, self.registration = store, clock, registration
        self.work_policy = None
        self.contract = None if registration is None else _check(registration.contract, RuntimeContract)

    def _enabled(self) -> tuple[RuntimeRegistration, RuntimeContract]:
        if self.registration is None or self.contract is None:
            raise Problem(403, "DELIVERY_NATIVE_DISABLED", "native storage/protection/consumer/timer integration is not installed")
        if self.registration.mode == "native":
            from .behavior_inbox import PullInbox
            adapter = self.registration.channel_adapter
            if not isinstance(adapter, PullInbox):
                raise Problem(403, "DELIVERY_NATIVE_DISABLED", "the concrete authenticated pull channel is required")
            adapter.check_structure(self)
        elif self.registration.mode != "offline_contract":
            raise Problem(403, "DELIVERY_NATIVE_DISABLED", "unknown delivery runtime mode")
        return self.registration, self.contract

    def _now(self) -> datetime:
        try:
            return _utc(self.clock())
        except ValueError:
            raise Problem(503, "DELIVERY_CLOCK_INVALID", "trusted UTC is unavailable") from None

    def configuration_digest(self) -> str:
        registration, contract = self._enabled()
        body = contract.model_dump(mode="json")
        return _digest(body if registration.mode == "offline_contract" else {"contract": body, "mode": "native"})

    def _row(self, snapshot: Snapshot, key: str, model: type[M]) -> M:
        record = snapshot[key]
        if record is None:
            raise Problem(409, "DELIVERY_CONFIGURATION_OR_RECORD_MISSING", "a required control record is missing")
        if record.payload["binding"] != self._enabled()[1].control_binding:
            raise Problem(409, "DELIVERY_AUTHORITY_MISMATCH", "control authority binding differs")
        value = _parse(record.payload["data"], model)
        if isinstance(value, _Bound) and (
            value.configuration_digest != self.configuration_digest()
            or value.evidence_mode != self.registration.mode
        ):
            raise Problem(409, "DELIVERY_CONFIGURATION_MISMATCH", "runtime configuration differs")
        return value

    def _write(self, key: str, value: StrictModel | dict, mode="replace", material=None):
        if isinstance(value, _Bound):
            value = _update(value, evidence_mode=self.registration.mode)
        data = value.model_dump(mode="json") if isinstance(value, StrictModel) else value
        return ControlWrite(row_id=key, mode=mode, data=data, material=material)

    def _owner(self, snapshot: Snapshot, context: ExecutionContext):
        owner = self._row(snapshot, control_key("owner", context.owner_record_id), OwnerRecord)
        if not owner.active or any(getattr(owner, name) != getattr(context, name) for name in ExecutionContext.model_fields):
            raise Problem(403, "DELIVERY_OWNER_DENIED", "current owner, epoch or scope differs")
        conversation = snapshot[control_key("delivery-conversation", context.conversation_id)]
        if conversation is not None:
            value = self._row(snapshot, control_key("delivery-conversation", context.conversation_id), _Conversation)
            if (value.conversation_id, value.owner_record_id, value.epoch) != (
                context.conversation_id, context.owner_record_id, context.epoch,
            ):
                raise Problem(403, "DELIVERY_WRITER_DENIED", "conversation writer binding differs")

    def _obligation(self, snapshot: Snapshot, obligation_id: str, context: ExecutionContext):
        self._owner(snapshot, context)
        ob = self._row(snapshot, control_key("delivery-obligation", obligation_id), _Obligation)
        if (ob.obligation_id, ob.owner_record_id, ob.conversation_id, ob.root_budget_id) != (
            obligation_id, context.owner_record_id, context.conversation_id, context.root_budget_id,
        ):
            raise Problem(403, "DELIVERY_SCOPE_DENIED", "obligation scope differs")
        if (ob.route.channel_id != self.contract.channel.channel_id
                or ob.channel_version != self.contract.channel.version):
            raise Problem(403, "DELIVERY_CHANNEL_MISMATCH", "an obligation must use its original registered channel")
        return ob

    def _index(self, snapshot: Snapshot, context: ExecutionContext):
        from .behavior_delivery_pending import read
        return read(self, snapshot, context.root_budget_id)

    def _work(self, snapshot: Snapshot, ob: _Obligation) -> _Work:
        key = control_key("delivery-work-state", ob.obligation_id)
        work = self._row(snapshot, key, _Work)
        if (
            work.event_id != ob.event_id or work.definition.work_id != ob.work_id
            or work.definition_digest != _digest(work.definition.model_dump(mode="json"))
            or (work.state == "queued") != (work.result is None)
            or work.result is not None and (
                work.result.work_id != ob.work_id or work.result.definition_digest != work.definition_digest
                or work.result.state != work.state
            )
        ):
            raise Problem(409, "DELIVERY_WORK_BINDING_INVALID", "saved work/result association differs")
        stored_material = snapshot[key].payload["material"]
        if work.result is not None:
            if _parse(stored_material, InlineMaterial) != _material(work.result.model_dump(mode="json")):
                raise Problem(409, "DELIVERY_WORK_BINDING_INVALID", "saved result and retained evidence differ")
        elif stored_material is not None:
            raise Problem(409, "DELIVERY_WORK_BINDING_INVALID", "queued work has unexpected result material")
        return work

    def _index_writes(self, snapshot: Snapshot, context: ExecutionContext, ob: _Obligation, *, admission=False):
        from .behavior_delivery_pending import writes
        return writes(self, snapshot, context, ob, admission=admission)

    def _index_transact(self, request, decide, context, *, obligation_id=None, admission=False):
        from .behavior_delivery_pending import transact
        return transact(self, request, decide, context.root_budget_id,
                        obligation_id=obligation_id, admission=admission)

    def _quota_keys(self, context: ExecutionContext):
        config = self._enabled()[1]
        return tuple(control_key("delivery-quota", kind, value) for kind, value in (
            ("team", config.team_id), ("entry", config.entry_id), ("root", context.root_budget_id),
        ))

    def _quotas(self, snapshot: Snapshot, context: ExecutionContext):
        config = self._enabled()[1]
        quotas = tuple(self._row(snapshot, key, QuotaRecord) for key in self._quota_keys(context))
        if tuple((q.scope_kind, q.scope_id) for q in quotas) != (
            ("team", config.team_id), ("entry", config.entry_id), ("root", context.root_budget_id),
        ):
            raise Problem(409, "DELIVERY_SCOPE_DENIED", "quota scope differs")
        return quotas

    def _credit(self, ob: _Obligation, fallback: Literal["notification_credit", "attention_credit"] | None = None):
        if ob.deadline is None and ob.normal_control_remaining is None:
            return ob
        if ob.normal_control_remaining:
            return _update(ob, normal_control_remaining=ob.normal_control_remaining - 1)
        if fallback is not None and getattr(ob, fallback):
            return _update(ob, **{fallback: 0})
        raise Problem(429, "DELIVERY_CONTROL_EXHAUSTED", "reserved control capacity is exhausted; obligation remains unfulfilled")

    def _readiness(self) -> ReadinessEvidence:
        registration, config = self._enabled()
        if registration.mode == "native":
            registration.channel_adapter.check_installation(self)
        if registration.verify_readiness is None:
            raise Problem(403, "DELIVERY_PREREQUISITES_MISSING", "readiness verification is not configured")
        proof = _check(registration.verify_readiness(), ReadinessEvidence)
        if (
            proof.configuration_digest != self.configuration_digest() or proof.control_binding != config.control_binding
            or (proof.protection_version, proof.consumer_version, proof.timer_version) != (
                config.protection_version, config.consumer_version, config.timer_version,
            )
            or set(proof.coverage) != {
                "atomic_control", "inline_retention", "durable_consumer", "timer_redrive", "bounded_scan", "ownership_recovery",
            }
        ):
            raise Problem(403, "DELIVERY_PREREQUISITES_MISSING", "required readiness coverage or binding differs")
        return proof

    def request_for(self, command: Command, context: ExecutionContext) -> ControlRequest:
        """Persist before invocation. This method does not call a verifier/store."""
        self._enabled()
        context = _check(context, ExecutionContext)
        if type(command) not in (
            AdmissionRequest, WorkResultRequest, MessageRequest, ReceiptRequest,
            DueRequest, DueCompletion, ScanRequest,
        ):
            raise Problem(422, "DELIVERY_INVALID_DATA", "unknown delivery transition")
        command = _check(command, type(command))
        common = [
            control_key("owner", context.owner_record_id),
            control_key("delivery-conversation", context.conversation_id),
            control_key("delivery-index", context.root_budget_id),
        ]
        body = command.model_dump(mode="json")
        if isinstance(command, AdmissionRequest):
            action, ids = "admit", (command.event.event_id,)
            body.pop("expected_sequence")
            common += [
                control_key("policy", context.policy_id), control_key("budget", context.root_budget_id),
                control_key("delivery-event", command.event.event_id),
                control_key("delivery-work", command.work.work_id),
                control_key("delivery-obligation", obligation_id_for(command.event.event_id)),
                *self._quota_keys(context),
            ]
            if getattr(self,"work_policy",None) is not None:
                common.extend(self.work_policy.admission_keys(command))
        elif isinstance(command, ScanRequest):
            action, ids = "scan", (command.scan_id,)
            common += [self._quota_keys(context)[2]]
        else:
            common += [control_key("delivery-obligation", command.obligation_id)]
            if isinstance(command, WorkResultRequest):
                action, ids = "work-result", (command.obligation_id, command.evidence_id)
            elif isinstance(command, MessageRequest):
                action, ids = "message", (command.obligation_id, command.delivery_id)
                common += [control_key("delivery-outbox", command.delivery_id)]
            elif isinstance(command, ReceiptRequest):
                action, ids = "receipt", (command.obligation_id, command.delivery_id, command.evidence_id)
                common += [control_key("delivery-outbox", command.delivery_id), *self._quota_keys(context)]
            elif isinstance(command, DueCompletion):
                action, ids = "complete-due", (command.obligation_id, str(command.generation))
                common += [control_key("delivery-due", command.obligation_id, str(command.generation))]
            elif isinstance(command, DueRequest):
                action, ids = "due", (command.obligation_id, str(command.generation))
                common += [control_key("delivery-due", command.obligation_id, str(command.generation))]
            else:
                raise Problem(422, "DELIVERY_INVALID_DATA", "unknown delivery transition")
            common += [control_key("delivery-work-state", command.obligation_id)]
        return ControlRequest(
            request_id=control_key("delivery-request", action, *ids),
            request_digest=_digest([action, body, context.model_dump(mode="json"), self.configuration_digest()]),
            read_set=tuple(sorted(set(common))),
        )

    def _result(self, **fields: object):
        return {"actual_environment": "NOT_RUN", "execution_enabled": False, **fields}

    def _bound_data(self):
        return {
            "schema_version": 1, "runtime_version": RUNTIME_VERSION,
            "configuration_digest": self.configuration_digest(), "evidence_mode": self._enabled()[0].mode,
        }

    def admit(self, command: AdmissionRequest, context: ExecutionContext) -> Record:
        command,context=_command(command,AdmissionRequest),_check(context,ExecutionContext)
        record=None if self.work_policy is None else self.work_policy._admission_record(command)
        attempts=3 if record is not None and record.profile.instruction is not None else 1
        for attempt in range(attempts):
            try:
                return self._admit_once(command,context)
            except Problem as error:
                if (attempt==attempts-1 or error.status!=412
                        or error.body["error"]["code"] not in {"BEHAVIOR_CONFLICT","WORK_POLICY_COST_CHANGED"}):
                    raise
                # Only the rejected admission EGT repeats, with fresh predicates/cost.
                # A lost commit acknowledgment is not a 412 and is never retried here.

    def _admit_once(self, command: AdmissionRequest, context: ExecutionContext) -> Record:
        request = self.request_for(command, context)
        record=None if self.work_policy is None else self.work_policy._admission_record(command)
        instruction=record is not None and record.profile.instruction is not None
        readiness = None if instruction else self._readiness()
        registration, config = self._enabled()
        if registration.verify_ingress is None:
            raise Problem(403, "DELIVERY_INGRESS_DISABLED", "authenticated ingress verification is missing")
        actor = _check(registration.verify_ingress(command.ingress_evidence_id), VerifiedIngress)
        if (
            actor.evidence_id != command.ingress_evidence_id or actor.admission_digest != admission_digest(command)
            or actor.verifier_version != config.verifier_version
            or actor.on_behalf_of_principal_id != context.principal_id
            or command.event.conversation_id != context.conversation_id
            or command.route.channel_id != config.channel.channel_id
            or command.required_confirmation_level not in config.channel.supported_levels
        ):
            raise Problem(403, "DELIVERY_INGRESS_DENIED", "authenticated ingress, route or confirmation requirement differs")
        actor_material = _material(actor.model_dump(mode="json"))
        cost_observation = None if self.work_policy is None else self.work_policy.admission_observation(command)
        event_material = InlineMaterial(data=command.event.content, sha256=hashlib.sha256(command.event.content).hexdigest())
        ob_id = obligation_id_for(command.event.event_id)
        fresh = False

        def decide(snapshot: Snapshot):
            nonlocal fresh
            fresh = True
            self._owner(snapshot, context)
            policy = self._row(snapshot, control_key("policy", context.policy_id), PolicyRecord)
            budget = self._row(snapshot, control_key("budget", context.root_budget_id), BudgetRecord)
            now = self._now()
            if (
                policy.policy_id != context.policy_id or not policy.active or policy.revision != actor.policy_revision
                or budget.root_budget_id != context.root_budget_id or not budget.active
                or not actor.observed_at <= now < actor.expires_at
                or readiness is not None and not readiness.observed_at <= now < readiness.expires_at
                or (command.deadline is not None if instruction else
                    command.deadline is None or not now < command.deadline <= now + timedelta(seconds=config.max_queue_seconds))
            ):
                raise Problem(403, "DELIVERY_ADMISSION_DENIED", "current policy, budget, readiness or admission time window is invalid")
            conv_key = control_key("delivery-conversation", context.conversation_id)
            previous = snapshot[conv_key]
            sequence = 0 if previous is None else self._row(snapshot, conv_key, _Conversation).sequence
            if sequence != command.expected_sequence:
                raise Problem(409, "DELIVERY_SEQUENCE_CONFLICT", "conversation sequence differs")
            if snapshot[control_key("delivery-event", command.event.event_id)] is not None:
                raise Problem(409, "DELIVERY_EVENT_WITHOUT_RECEIPT", "event alone does not prove a complete admission")
            from .behavior_delivery_pending import counts
            pending_count, non_instruction_count = counts(self, snapshot, context.root_budget_id)
            quotas = self._quotas(snapshot, context)
            if quotas[2].pending != non_instruction_count:
                raise Problem(409, "DELIVERY_INDEX_INVALID", "root pending count and index differ")
            credits = config.normal_control_units + 3
            if not instruction and (
                any(q.pending >= q.pending_limit for q in quotas) or pending_count >= 32
                or quotas[2].available_control_units < credits
            ):
                raise Problem(429, "DELIVERY_ADMISSION_FULL", "pending or reserved control capacity is exhausted")
            if quotas[2].root_request_id != command.event.root_request_id:
                raise Problem(403, "DELIVERY_SCOPE_DENIED", "root request/budget association differs")
            bound = {"configuration_digest": self.configuration_digest(), "evidence_mode": registration.mode}
            work = _Work(**bound, event_id=command.event.event_id, definition=command.work, definition_digest=_digest(command.work.model_dump(mode="json")))
            ob = _Obligation(
                **bound, obligation_id=ob_id, event_id=command.event.event_id,
                root_request_id=command.event.root_request_id, work_id=command.work.work_id,
                owner_record_id=context.owner_record_id, conversation_id=context.conversation_id,
                root_budget_id=context.root_budget_id, route=command.route,
                policy_revision=policy.revision,
                required_confirmation_level=command.required_confirmation_level, channel_version=config.channel.version,
                review_at=(now+timedelta(seconds=config.review_interval_seconds) if instruction else
                           min(command.deadline,now+timedelta(seconds=config.review_interval_seconds))),
                deadline=command.deadline, normal_control_remaining=None if instruction else config.normal_control_units,
            )
            intent_id = control_key("delivery-intent-id", command.event.event_id)
            writes = [
                self._write(conv_key, _Conversation(
                    **bound, conversation_id=context.conversation_id, owner_record_id=context.owner_record_id,
                    epoch=context.epoch, sequence=sequence + 1,
                ), "create" if previous is None else "replace"),
                self._write(control_key("delivery-event", command.event.event_id), {
                    **self._bound_data(), "kind": "delivery_event",
                    **command.event.model_dump(mode="json", exclude={"content"}),
                    "sequence": sequence + 1, "work_id": command.work.work_id,
                    "source": actor.model_dump(mode="json"), "content_digest": event_material.sha256,
                    "admission_request_id": request.request_id,
                    "execution_context": context.model_dump(mode="json"),
                }, "create", event_material),
                self._write(control_key("delivery-work", command.work.work_id), {
                    **self._bound_data(), "kind": "delivery_work_identity", "work_id": command.work.work_id,
                    "event_id": command.event.event_id, "obligation_id": ob_id,
                    "definition_digest": work.definition_digest,
                    "state_row_id": control_key("delivery-work-state", ob_id),
                }, "create"),
                self._write(control_key("delivery-work-state", ob_id), work, "create"),
                self._write(control_key("delivery-obligation", ob_id), ob, "create"),
                self._write(control_key("delivery-intent", intent_id), _Intent(
                    **bound, intent_id=intent_id, obligation_id=ob_id, action="process_event",
                    consumer_version=config.consumer_version, generation=0,
                    state_row_id=control_key("delivery-work-state", ob_id),
                ), "create"),
                *self._index_writes(snapshot, context, ob, admission=True),
            ]
            for key, quota in (() if instruction else zip(self._quota_keys(context), quotas)):
                writes.append(self._write(key, _update(
                    quota, pending=quota.pending + 1,
                    available_control_units=quota.available_control_units - credits if quota.scope_kind == "root" else quota.available_control_units,
                )))
            if self.work_policy is not None:
                writes.extend(self.work_policy.admission_writes(snapshot,command,context,actor,cost_observation))
            return ControlChanges(writes=tuple(writes), result=self._result(
                status="queued", event_id=command.event.event_id, work_id=command.work.work_id,
                obligation_id=ob_id, sequence=sequence + 1, processing_intent_id=intent_id,
                actor_digest=actor_material.sha256,
                valid_until=min(actor.expires_at if readiness is None else readiness.expires_at, actor.expires_at,
                    actor.expires_at if instruction else command.deadline).isoformat(),
                admitted_at=now.isoformat(),
            ))

        commit = lambda: self._index_transact(request, decide, context,
                                             obligation_id=ob_id, admission=True)
        receipt = (commit() if self.work_policy is None else
                   self.work_policy.admission_commit(command,cost_observation,
                       commit))
        if receipt.payload["result"]["actor_digest"] != actor_material.sha256:
            raise Problem(409, "DELIVERY_SOURCE_CONFLICT", "ingress evidence ID has changed")
        if fresh:
            now = self._now()
            result = receipt.payload["result"]
            if not datetime.fromisoformat(result["admitted_at"]) <= now < datetime.fromisoformat(result["valid_until"]):
                raise Problem(409, "DELIVERY_ADMISSION_UNKNOWN", "admission remains reserved; reconcile the original request",
                              request_id=request.request_id, request_digest=request.request_digest)
        return receipt

    def record_work_result(self, command: WorkResultRequest, context: ExecutionContext) -> Record:
        command, context = _command(command, WorkResultRequest), _check(context, ExecutionContext)
        request = self.request_for(command, context)
        registration, config = self._enabled()
        if registration.verify_work_result is None:
            raise Problem(403, "DELIVERY_WORK_VERIFIER_DISABLED", "work verification is not installed")
        evidence = _check(registration.verify_work_result(command.evidence_id), VerifiedWorkResult)
        if evidence.evidence_id != command.evidence_id or evidence.verifier_version != config.verifier_version:
            raise Problem(403, "DELIVERY_WORK_EVIDENCE_DENIED", "work evidence identity differs")
        material = _material(evidence.model_dump(mode="json"))

        def decide(snapshot: Snapshot):
            ob = self._obligation(snapshot, command.obligation_id, context)
            key = control_key("delivery-work-state", ob.obligation_id)
            work = self._work(snapshot, ob)
            if (
                work.definition.work_id != ob.work_id or evidence.work_id != ob.work_id
                or evidence.definition_digest != work.definition_digest or work.state != "queued"
            ):
                raise Problem(409, "DELIVERY_WORK_EVIDENCE_DENIED", "work revision or terminal state differs")
            if evidence.state == "succeeded" and (
                not set(work.definition.required_checks) <= set(evidence.passed_checks)
                or not set(work.definition.human_checks) <= set(evidence.human_approved_checks)
                or evidence.unresolved_operations
                or work.definition.result_kind in {"artifact", "mixed"} and not evidence.artifacts
            ):
                raise Problem(409, "DELIVERY_WORK_INCOMPLETE", "necessary results or operations are not verified")
            return ControlChanges(writes=(
                self._write(key, _update(work, state=evidence.state, result=evidence), material=material),
            ), result=self._result(work_state=evidence.state, obligation_fulfilled=False, evidence_digest=material.sha256))

        receipt = self.store.transact(request, decide)
        if receipt.payload["result"]["evidence_digest"] != material.sha256:
            raise Problem(409, "DELIVERY_EVIDENCE_CONFLICT", "work evidence ID has changed")
        return receipt

    def prepare_message(self, command: MessageRequest, context: ExecutionContext) -> Record:
        command, context = _command(command, MessageRequest), _check(context, ExecutionContext)
        request = self.request_for(command, context)
        config = self._enabled()[1]

        def decide(snapshot: Snapshot):
            ob = self._obligation(snapshot, command.obligation_id, context)
            work = self._work(snapshot, ob)
            final = command.message_kind in {"final", "failure"}
            if (ob.fulfillment == "fulfilled" or ob.last_version != command.expected_version
                    or ob.deadline is not None and ob.last_version >= config.max_delivery_versions):
                raise Problem(409, "DELIVERY_VERSION_CONFLICT", "delivery version or obligation state differs")
            if not final and ob.deadline is not None and ob.last_version >= config.max_delivery_versions - 1:
                raise Problem(429, "DELIVERY_FINAL_SLOT_RESERVED", "the remaining outbox version is reserved for final/failure")
            if final and ob.final_delivery_id is not None:
                raise Problem(409, "DELIVERY_FINAL_ALREADY_PREPARED", "reconcile the original final; replacement/rerouting is not implemented")
            if command.message_kind == "final" and (work.state != "succeeded" or work.result is None):
                raise Problem(409, "DELIVERY_WORK_INCOMPLETE", "a final requires saved verified results")
            ob = self._credit(ob, "notification_credit" if final else None)
            result = work.result
            payload = _Payload(
                event_id=ob.event_id, root_request_id=ob.root_request_id, work_id=ob.work_id,
                message_kind=command.message_kind, work_state=work.state,
                result_evidence_id=None if result is None else result.evidence_id,
                content=None if result is None else result.content,
                artifacts=() if result is None else result.artifacts,
                known_effects=None if result is None else result.known_effects,
                unresolved_operations=None if result is None else result.unresolved_operations,
                responsibility_owner=ob.owner_record_id,
                notification_code=(
                    "request_admitted" if command.message_kind == "ack" else
                    "work_status" if command.message_kind == "progress" else
                    "verified_result" if command.message_kind == "final" else "delivery_needs_attention"
                ),
            )
            material = _material(payload.model_dump(mode="json"))
            binding = DeliveryBinding(
                obligation_id=ob.obligation_id, delivery_id=command.delivery_id, version=ob.last_version + 1,
                payload_digest=material.sha256, route=ob.route, channel_version=ob.channel_version,
                artifacts=payload.artifacts,
            )
            ob = _update(ob, last_version=binding.version, **({
                "final_delivery_id": command.delivery_id, "final_version": binding.version, "delivery_state": "final_prepared",
            } if final else {}))
            return ControlChanges(writes=(
                self._write(control_key("delivery-outbox", command.delivery_id), _Outbox(
                    configuration_digest=self.configuration_digest(), binding=binding, message_kind=command.message_kind,
                ), "create", material),
                self._write(control_key("delivery-obligation", ob.obligation_id), ob),
            ), result=self._result(binding=binding.model_dump(mode="json"), message_kind=command.message_kind, obligation_fulfilled=False))

        return self.store.transact(request, decide)

    def record_receipt(self, command: ReceiptRequest, context: ExecutionContext) -> Record:
        command, context = _command(command, ReceiptRequest), _check(context, ExecutionContext)
        request = self.request_for(command, context)
        registration, config = self._enabled()
        verifier = (registration.channel_adapter.verify_receipt
                    if registration.mode == "native" else registration.verify_receipt)
        if verifier is None:
            raise Problem(403, "DELIVERY_RECEIPT_VERIFIER_DISABLED", "receipt verification is not installed")
        proof = _check(verifier(command.evidence_id), VerifiedDeliveryReceipt)
        if (
            proof.evidence_id != command.evidence_id or proof.verifier_version != config.verifier_version
            or not set(proof.confirmed_levels) <= set(config.channel.supported_levels)
        ):
            raise Problem(403, "DELIVERY_RECEIPT_DENIED", "receipt identity or channel evidence level differs")
        proof_material = _material(proof.model_dump(mode="json"))

        def decide(snapshot: Snapshot):
            ob = self._obligation(snapshot, command.obligation_id, context)
            outbox_key = control_key("delivery-outbox", command.delivery_id)
            outbox = self._row(snapshot, outbox_key, _Outbox)
            if (
                proof.binding != outbox.binding or outbox.binding.obligation_id != ob.obligation_id
                or outbox.binding.delivery_id != command.delivery_id or outbox.binding.route != ob.route
                or outbox.binding.channel_version != ob.channel_version or ob.channel_version != config.channel.version
            ):
                raise Problem(409, "DELIVERY_RECEIPT_DENIED", "receipt digest, route, version or association differs")
            material = _parse(snapshot[outbox_key].payload["material"], InlineMaterial)
            if material.sha256 != proof.binding.payload_digest:
                raise Problem(409, "DELIVERY_RECEIPT_DENIED", "saved payload differs")
            current = ob.final_delivery_id == command.delivery_id and ob.final_version == proof.binding.version
            fulfills = (
                ob.fulfillment == "pending" and current and outbox.message_kind in {"final", "failure"}
                and proof.send_outcome == "emitted" and ob.required_confirmation_level in proof.confirmed_levels
            )
            limit = config.max_receipts_per_delivery
            if ob.fulfillment == "pending" and outbox.message_kind in {"final", "failure"} and not fulfills:
                limit -= 1
            if ob.deadline is not None and outbox.receipt_count >= limit:
                raise Problem(429, "DELIVERY_RECEIPT_LIMIT", "bounded non-fulfilling receipt capacity is exhausted")
            writes = []
            if fulfills:
                if not ob.fulfillment_credit:
                    raise Problem(429, "DELIVERY_CONTROL_EXHAUSTED", "fulfillment reservation is missing")
                ob = _update(
                    ob, fulfillment="fulfilled", delivery_state="fulfilled", fulfillment_credit=0,
                    fulfillment_receipt_id=proof.evidence_id, confirmed_levels=proof.confirmed_levels,
                    due_pending=False, automation="stopped",
                )
                for key, quota in (() if ob.deadline is None else zip(self._quota_keys(context), self._quotas(snapshot, context))):
                    if quota.pending < 1:
                        raise Problem(409, "DELIVERY_QUOTA_INVALID", "pending reservation is missing")
                    if quota.scope_kind == "root":
                        from .behavior_delivery_pending import counts
                        if quota.pending != counts(self, snapshot, context.root_budget_id)[1]:
                            raise Problem(409, "DELIVERY_INDEX_INVALID", "root pending count and index differ")
                    writes.append(self._write(key, _update(quota, pending=quota.pending - 1)))
                writes.extend(self._index_writes(snapshot, context, ob))
            else:
                ob = self._credit(ob)
                if ob.fulfillment == "pending":
                    if current and proof.send_outcome in {"unknown", "failed"}:
                        ob = _update(ob, delivery_state="delivery_unknown" if proof.send_outcome == "unknown" else "delivery_failed")
                    elif not ob.final_delivery_id and outbox.message_kind == "ack" and proof.send_outcome == "emitted":
                        ob = _update(ob, delivery_state="acked")
            writes.extend((
                self._write(control_key("delivery-obligation", ob.obligation_id), ob),
                self._write(outbox_key, _update(
                    outbox,
                    send_outcome="emitted" if "emitted" in (outbox.send_outcome, proof.send_outcome) else proof.send_outcome,
                    confirmed_levels=tuple(level for level in config.channel.supported_levels if (
                        level in outbox.confirmed_levels or proof.send_outcome == "emitted" and level in proof.confirmed_levels
                    )),
                    receipt_count=outbox.receipt_count + 1,
                ), material=material),
                self._write(control_key("delivery-receipt", command.delivery_id, proof.evidence_id),
                            {**self._bound_data(), "kind": "delivery_verified_receipt", "evidence": proof.model_dump(mode="json")},
                            "create", proof_material),
            ))
            return ControlChanges(writes=tuple(writes), result=self._result(
                obligation_id=ob.obligation_id, fulfillment=ob.fulfillment, delivery_state=ob.delivery_state,
                required_confirmation_level=ob.required_confirmation_level,
                confirmed_levels=list(ob.confirmed_levels), channel_version=ob.channel_version,
                fulfillment_receipt_id=ob.fulfillment_receipt_id, evidence_digest=proof_material.sha256,
            ))

        receipt = self._index_transact(request, decide, context, obligation_id=command.obligation_id)
        if receipt.payload["result"]["evidence_digest"] != proof_material.sha256:
            raise Problem(409, "DELIVERY_EVIDENCE_CONFLICT", "receipt evidence ID has changed")
        return receipt

    def scan_due(self, command: ScanRequest, context: ExecutionContext) -> Record:
        command, context = _command(command, ScanRequest), _check(context, ExecutionContext)
        request = self.request_for(command, context)
        config = self._enabled()[1]

        def decide(snapshot: Snapshot):
            self._owner(snapshot, context)
            index = self._index(snapshot, context)
            key = self._quota_keys(context)[2]
            quota = self._row(snapshot, key, QuotaRecord)
            if quota.scope_kind != "root" or quota.scope_id != context.root_budget_id or quota.scans_remaining < 1:
                raise Problem(429, "DELIVERY_SCAN_LIMIT", "root scan budget is unavailable")
            cursor = command.cursor
            offset = 0
            if cursor is not None:
                expected = ScanCursor(
                    **{name: getattr(context, name) for name in ("root_budget_id", "owner_record_id", "principal_id", "conversation_id", "epoch")},
                    index_revision=index.revision, offset=cursor.offset,
                )
                if cursor != expected:
                    raise Problem(409, "DELIVERY_CURSOR_STALE", "cursor revision or authorized reader differs")
                offset = cursor.offset
            entries = sorted(
                (entry for entry in index.entries if entry.owner_record_id == context.owner_record_id and entry.conversation_id == context.conversation_id),
                key=lambda entry: entry.obligation_id,
            )
            if offset > len(entries):
                raise Problem(409, "DELIVERY_CURSOR_STALE", "cursor offset differs")
            page = entries[offset:offset + config.scan_page_size]
            now = self._now()
            due = [entry.model_dump(mode="json") for entry in page if entry.automation == "active" and entry.review_at <= now]
            next_offset = offset + len(page)
            next_cursor = None if next_offset >= len(entries) else ScanCursor(
                **{name: getattr(context, name) for name in ("root_budget_id", "owner_record_id", "principal_id", "conversation_id", "epoch")},
                index_revision=index.revision, offset=next_offset,
            ).model_dump(mode="json")
            return ControlChanges(writes=(self._write(key, _update(quota, scans_remaining=quota.scans_remaining - 1)),),
                                  result=self._result(due=due, cursor=next_cursor, inspected=len(page)))

        return self._index_transact(request, decide, context)

    def mark_due(self, command: DueRequest, context: ExecutionContext) -> Record:
        command, context = _command(command, DueRequest), _check(context, ExecutionContext)
        request = self.request_for(command, context)
        config = self._enabled()[1]

        def decide(snapshot: Snapshot):
            ob = self._obligation(snapshot, command.obligation_id, context)
            now = self._now()
            if ob.fulfillment != "pending" or ob.automation != "active" or command.generation != ob.generation:
                raise Problem(409, "DELIVERY_DUE_STALE", "obligation processing generation is not active")
            if now < ob.review_at or ob.due_pending or ob.deadline is not None and ob.generation >= config.max_due_generations:
                raise Problem(409, "DELIVERY_DUE_NOT_READY", "due is early, already pending or exhausted")
            work = self._work(snapshot, ob)
            attention = ob.deadline is not None and (now >= ob.deadline or not ob.normal_control_remaining)
            ob = self._credit(ob, "attention_credit")
            if attention:
                ob = _update(ob, automation="needs_attention", attention_reason="deadline_or_control_exhausted")
            ob = _update(ob, due_pending=True)
            intent_id = control_key("delivery-due-intent-id", ob.obligation_id, str(ob.generation))
            action = "needs_attention" if attention else (
                "reconcile_delivery" if ob.final_delivery_id else "use_saved_result" if work.result is not None else "review"
            )
            intent = _Intent(
                configuration_digest=self.configuration_digest(), intent_id=intent_id, obligation_id=ob.obligation_id,
                action=action, consumer_version=config.consumer_version, generation=ob.generation,
                state_row_id=control_key("delivery-due", ob.obligation_id, str(ob.generation)),
            )
            return ControlChanges(writes=(
                self._write(intent.state_row_id, _Due(
                    configuration_digest=self.configuration_digest(), obligation_id=ob.obligation_id,
                    generation=ob.generation, intent_id=intent_id, action=action,
                ), "create"),
                self._write(control_key("delivery-intent", intent_id), intent, "create"),
                self._write(control_key("delivery-obligation", ob.obligation_id), ob),
                *self._index_writes(snapshot, context, ob),
            ), result=self._result(intent=intent.model_dump(mode="json"), obligation_fulfilled=False))

        return self._index_transact(request, decide, context, obligation_id=command.obligation_id)

    def complete_due(self, command: DueCompletion, context: ExecutionContext) -> Record:
        command, context = _command(command, DueCompletion), _check(context, ExecutionContext)
        request = self.request_for(command, context)
        config = self._enabled()[1]

        def decide(snapshot: Snapshot):
            ob = self._obligation(snapshot, command.obligation_id, context)
            key = control_key("delivery-due", ob.obligation_id, str(command.generation))
            due = self._row(snapshot, key, _Due)
            if command.generation != ob.generation or not ob.due_pending or due.state != "pending":
                raise Problem(409, "DELIVERY_DUE_STALE", "due processing generation differs")
            now = self._now()
            stop = ob.automation != "active" or ob.deadline is not None and (
                now >= ob.deadline or ob.generation + 1 >= config.max_due_generations)
            ob = self._credit(ob, "attention_credit" if stop else None)
            ob = _update(
                ob, generation=ob.generation + 1, due_pending=False,
                review_at=(now+timedelta(seconds=config.review_interval_seconds) if ob.deadline is None
                           else min(ob.deadline,now+timedelta(seconds=config.review_interval_seconds))),
                automation="stopped" if stop else "active",
                attention_reason="review_limit_or_deadline" if stop and ob.attention_reason is None else ob.attention_reason,
            )
            return ControlChanges(writes=(
                self._write(key, _update(due, state="done")),
                self._write(control_key("delivery-obligation", ob.obligation_id), ob),
                *self._index_writes(snapshot, context, ob),
            ), result=self._result(generation=ob.generation, automation=ob.automation, obligation_fulfilled=False))

        return self._index_transact(request, decide, context, obligation_id=command.obligation_id)

    def send(self, delivery_id: str) -> Record:
        raise Problem(403, "DELIVERY_SEND_DISABLED", "outbound sends are disabled; use authenticated PullInbox.fetch")


def authority_snapshot(store: BehaviorStore, keys: tuple[str, ...]) -> Snapshot:
    """Compatibility name for the store's public, bounded live authority read."""
    return store.read_snapshot(keys)


class _ConsumerPlan(_Bound):
    kind: Literal["delivery_consumer_plan"] = "delivery_consumer_plan"
    obligation_id: Identifier
    intent_id: Identifier
    consumer_version: Identifier
    worker_id: Identifier
    context: ExecutionContext
    generation: Count
    command: MessageRequest
    request: ControlRequest


class _ConsumerPage(_Bound):
    kind: Literal["delivery_consumer_page"] = "delivery_consumer_page"
    root_budget_id: Identifier
    sequence: Count = 0
    offset: Annotated[int, Field(ge=0, le=32)] = 0
    selected: Annotated[tuple[Identifier, ...], Field(max_length=16)] = ()
    active: bool = False


class DeliveryConsumer:
    """A bounded restartable consumer; it NEVER invokes a business/model executor.

    Call drain(root_budget_id, resolve_context, worker_id) from a supervised
    process/timer, or consume for one known obligation. The bounded durable index,
    original admission intent, result, and immutable plan are the restart inputs.
    resolve_context is a server-only current-owner lookup, not a model callback.
    A prepared outbox is merely available to PullInbox, not delivered.
    pending_binding is trusted opt-in only; the default durable scan is unchanged.
    """

    def __init__(self, core: DeliveryCore):
        self.core = core

    def _original_work(self, rows, obligation_id, context, intent_id):
        core = self.core
        current = core._obligation(rows, obligation_id, context)
        intent = core._row(rows, control_key("delivery-intent", intent_id), _Intent)
        if (
            intent.obligation_id != obligation_id or intent.action != "process_event"
            or intent.state_row_id != control_key("delivery-work-state", obligation_id)
            or intent.consumer_version != core.contract.consumer_version
            or current.fulfillment != "pending" or current.final_delivery_id is not None
        ):
            raise Problem(409, "DELIVERY_INTENT_STALE", "original admission intent is not consumable")
        work = core._work(rows, current)
        budget = core._row(rows, control_key("budget", context.root_budget_id), BudgetRecord)
        if budget.root_budget_id != current.root_budget_id:
            raise Problem(403, "DELIVERY_SCOPE_DENIED", "root budget binding differs")
        return current, work, budget

    def _business_pending(self, current, work, budget, *, now=None):
        return work.result is None and (
            (self.core._now() if now is None else now) < current.deadline and budget.active and budget.remaining_attempts
            and budget.remaining_units and current.automation == "active"
        )

    def _defer_pending(self, root_budget_id: str, resolve_context: Callable[[str], ExecutionContext],
                       binding: RuntimeBinding) -> bool:
        """Read-only, exact native C1 eligibility; no remembered scheduling decision."""
        from .behavior_assistant_wire import CHECKS
        from .behavior_inbox import INBOX_CHANNEL_ID, InboxDeploymentEvidence
        from .behavior_runtime import RUNTIME_VERSION, RuntimeBinding, RuntimePolicy, runtime_key
        from .behavior_workers import JobRecord, _Pool, worker_key

        core = self.core
        binding = _check(binding, RuntimeBinding)
        registration, contract = core._enabled()
        if (registration.mode != "native" or binding.route.channel_id != INBOX_CHANNEL_ID
                or contract.channel.channel_id != INBOX_CHANNEL_ID or set(binding.work.required_checks) != set(CHECKS)
                or binding.child_jobs or binding.owner.root_budget_id != root_budget_id
                or binding.team_id != contract.team_id):
            raise Problem(403, "DELIVERY_DEFER_SCOPE_DENIED", "one exact native assistant pull binding is required")
        before = core.store._guard()
        readiness = core._readiness()
        inbox_key = control_key("inbox-readiness", core.configuration_digest())
        published = core._row(authority_snapshot(core.store, (inbox_key,)), inbox_key, InboxDeploymentEvidence)
        context = _check(resolve_context(binding.owner.owner_record_id), ExecutionContext)
        if context != binding.owner:
            raise Problem(403, "DELIVERY_OWNER_DENIED", "deferral requires the exact configured owner")
        index_key = control_key("delivery-index", root_budget_id)
        page_key = control_key("delivery-consumer-cursor", root_budget_id)
        quota_key = control_key("delivery-quota", "root", root_budget_id)
        pool_key = worker_key("pool", published.worker_workspace_id)
        job_key = worker_key("job", binding.job_id)
        policy_key = runtime_key("policy", binding.binding_id)
        intent_id = control_key("delivery-intent-id", binding.event_id)
        plan_key = control_key("delivery-consumer-plan", binding.obligation_id)
        keys = (
            index_key, page_key, quota_key, inbox_key, pool_key, job_key, policy_key,
            control_key("owner", context.owner_record_id), control_key("policy", context.policy_id),
            control_key("delivery-conversation", context.conversation_id),
            control_key("delivery-obligation", binding.obligation_id),
            control_key("delivery-work-state", binding.obligation_id),
            control_key("budget", root_budget_id), control_key("delivery-intent", intent_id), plan_key,
        )
        rows = authority_snapshot(core.store, keys)
        core._owner(rows, context)
        core._row(rows, control_key("delivery-conversation", context.conversation_id), _Conversation)
        owner = core._row(rows, control_key("owner", context.owner_record_id), OwnerRecord)
        current_policy = core._row(rows, control_key("policy", context.policy_id), PolicyRecord)
        policy = core._row(rows, policy_key, RuntimePolicy)
        now = core._now()
        if (policy.binding_digest != _digest([RUNTIME_VERSION, "native", binding.model_dump(mode="json")])
                or not policy.active or not current_policy.active or current_policy.policy_id != context.policy_id
                or policy.operation_policy_revision != current_policy.revision or now >= policy.valid_until
                or binding.operation_id not in owner.operation_ids):
            raise Problem(403, "BEHAVIOR_CURRENT_POLICY_DENIED", "current owner, policy or binding is unavailable")
        if (core._row(rows, inbox_key, InboxDeploymentEvidence) != published
                or not readiness.observed_at <= now < readiness.expires_at):
            raise Problem(403, "DELIVERY_PREREQUISITES_MISSING", "current readiness is unavailable")
        index = core._index(rows, context)
        quota = core._row(rows, quota_key, QuotaRecord)
        if (quota.scope_kind != "root" or quota.scope_id != root_budget_id
                or quota.root_request_id != binding.root_request_id
                or quota.pending != sum(not entry.instruction for entry in index.entries)):
            raise Problem(403, "DELIVERY_SCOPE_DENIED", "root index/quota binding differs")
        page = None if rows[page_key] is None else core._row(rows, page_key, _ConsumerPage)
        if page is not None and page.root_budget_id != root_budget_id:
            raise Problem(403, "DELIVERY_SCOPE_DENIED", "consumer page belongs to another root")
        pool = core._row(rows, pool_key, _Pool)
        job = core._row(rows, job_key, JobRecord)
        if (pool.workspace_id != published.worker_workspace_id
                or pool.configuration_digest != published.worker_configuration_digest
                or job.configuration_digest != pool.configuration_digest or job.owner != context
                or job.spec.job_id != binding.job_id or job.spec.work_id != binding.work.work_id
                or job.spec.obligation_id != binding.obligation_id):
            raise Problem(403, "DELIVERY_SCOPE_DENIED", "original worker/job binding differs")
        if len(pool.slots) == 1 and (pool.slots[0].number != 0 or
                pool.slots[0].binding != job.binding and job.state in {"queued", "starting", "running", "capacity_uncertain"}):
            raise Problem(403, "DELIVERY_SCOPE_DENIED", "original C1 reservation differs from its job")
        obligation = core._obligation(rows, binding.obligation_id, context)
        work = core._work(rows, obligation)
        if (obligation.route != binding.route or obligation.event_id != binding.event_id
                or obligation.work_id != binding.work.work_id or obligation.root_request_id != binding.root_request_id
                or work.definition != binding.work):
            raise Problem(403, "DELIVERY_SCOPE_DENIED", "exact original delivery binding differs")
        expected_entry = PendingEntry(
            obligation_id=obligation.obligation_id, owner_record_id=obligation.owner_record_id,
            conversation_id=obligation.conversation_id, review_at=obligation.review_at,
            generation=obligation.generation, automation=obligation.automation, due_pending=obligation.due_pending)
        if (len(index.entries) == 1 and index.entries[0].obligation_id == binding.obligation_id
                and index.entries[0] != expected_entry):
            raise Problem(409, "DELIVERY_INDEX_INVALID", "original pending entry differs from its obligation")
        if (index.entries != (expected_entry,) or page is not None and page.active
                or len(pool.slots) != 1 or pool.pending != (binding.job_id,) or pool.completions
                or job.state not in {"queued", "starting", "running", "capacity_uncertain"} or job.cancel_requested
                or obligation.fulfillment != "pending" or obligation.final_delivery_id is not None
                or rows[plan_key] is not None or work.state != "queued" or work.result is not None):
            return False
        current, work, budget = self._original_work(rows, binding.obligation_id, context, intent_id)
        if core.store._guard() != before:
            raise Problem(412, "BEHAVIOR_CONFLICT", "authority changed during delivery eligibility")
        now = core._now()
        if now >= policy.valid_until or not readiness.observed_at <= now < readiness.expires_at:
            raise Problem(403, "BEHAVIOR_CURRENT_POLICY_DENIED", "delivery eligibility expired during validation")
        return bool(self._business_pending(current, work, budget, now=now))

    def consume(self, obligation_id: str, context: ExecutionContext, worker_id: str) -> dict:
        core = self.core
        context = _check(context, ExecutionContext)
        common = (
            control_key("owner", context.owner_record_id),
            control_key("delivery-conversation", context.conversation_id),
            control_key("delivery-obligation", obligation_id),
            control_key("delivery-work-state", obligation_id),
            control_key("budget", context.root_budget_id),
            control_key("delivery-consumer-plan", obligation_id),
        )
        snapshot = authority_snapshot(core.store, common)
        ob = core._obligation(snapshot, obligation_id, context)
        if ob.fulfillment == "fulfilled":
            return core._result(status="fulfilled", obligation_id=obligation_id)
        if ob.final_delivery_id is not None:
            return core._result(status="awaiting_client_ack", delivery_id=ob.final_delivery_id)
        intent_id = control_key("delivery-intent-id", ob.event_id)
        reads = (*common, control_key("delivery-intent", intent_id))
        request = ControlRequest(
            request_id=control_key("delivery-consumer-claim", obligation_id),
            request_digest=_digest([obligation_id, core.configuration_digest()]),
            read_set=tuple(sorted(reads)),
        )

        def decide(rows):
            current, work, budget = self._original_work(rows, obligation_id, context, intent_id)
            if self._business_pending(current, work, budget):
                raise Problem(409, "DELIVERY_RESULT_PENDING", "business work remains queued; consumer will not rerun it")
            command = MessageRequest(
                obligation_id=obligation_id, delivery_id=control_key("delivery-final-id", obligation_id),
                expected_version=current.last_version,
                message_kind="final" if work.state == "succeeded" else "failure",
            )
            plan = _ConsumerPlan(
                configuration_digest=core.configuration_digest(), evidence_mode=core.registration.mode,
                obligation_id=obligation_id, intent_id=intent_id, worker_id=worker_id,
                consumer_version=core.contract.consumer_version, context=context,
                generation=current.generation, command=command, request=core.request_for(command, context),
            )
            return ControlChanges(writes=(core._write(
                control_key("delivery-consumer-plan", obligation_id), plan, "create",
                _material(plan.model_dump(mode="json")),
            ),), result=core._result(plan_row=control_key("delivery-consumer-plan", obligation_id)))

        core.store.transact(request, decide)
        rows = authority_snapshot(core.store, reads)
        core._obligation(rows, obligation_id, context)
        plan = core._row(rows, control_key("delivery-consumer-plan", obligation_id), _ConsumerPlan)
        if (
            plan.context != context or plan.intent_id != intent_id
            or plan.consumer_version != core.contract.consumer_version
            or _parse(rows[control_key("delivery-consumer-plan", obligation_id)].payload["material"], InlineMaterial)
            != _material(plan.model_dump(mode="json"))
        ):
            raise Problem(409, "DELIVERY_PLAN_FENCED", "the retained consumer plan or current owner differs")
        # Pure deterministic storage transition: replay cannot send or run work.
        receipt = core.prepare_message(plan.command, plan.context)
        return core._result(status="awaiting_client_ack", **receipt.payload["result"])

    def drain(
        self, root_budget_id: str, resolve_context: Callable[[str], ExecutionContext],
        worker_id: str, *, limit: int = 4,
        pending_binding: RuntimeBinding | None = None,
    ) -> tuple[dict, ...]:
        if type(limit) is not int or not 1 <= limit <= 16:
            raise Problem(422, "DELIVERY_SCAN_LIMIT", "drain limit must be 1..16")
        core = self.core
        if pending_binding is not None:
            if self._defer_pending(root_budget_id, resolve_context, pending_binding):
                return ()
        index_key = control_key("delivery-index", root_budget_id)
        page_key = control_key("delivery-consumer-cursor", root_budget_id)
        quota_key = control_key("delivery-quota", "root", root_budget_id)
        keys = (index_key, page_key, quota_key)
        rows = authority_snapshot(core.store, keys)
        previous = None if rows[page_key] is None else core._row(rows, page_key, _ConsumerPage)
        sequence = 0 if previous is None else previous.sequence
        if previous is None or not previous.active:
            request = ControlRequest(
                request_id=control_key("delivery-consumer-scan", root_budget_id, str(sequence)),
                request_digest=_digest([root_budget_id, sequence, limit, core.configuration_digest()]),
                read_set=keys,
            )

            def select(snapshot):
                from .behavior_delivery_pending import read
                index = read(core, snapshot, root_budget_id)
                quota = core._row(snapshot, quota_key, QuotaRecord)
                current = None if snapshot[page_key] is None else core._row(snapshot, page_key, _ConsumerPage)
                if (
                    index.root_budget_id != root_budget_id or quota.scope_id != root_budget_id
                    or quota.scope_kind != "root"
                    or quota.pending != sum(not entry.instruction for entry in index.entries)
                ):
                    raise Problem(403, "DELIVERY_SCOPE_DENIED", "root index/quota binding differs")
                if current != previous:
                    raise Problem(409, "DELIVERY_CURSOR_STALE", "another consumer advanced the durable page")
                if quota.scans_remaining < 1:
                    raise Problem(429, "DELIVERY_SCAN_LIMIT", "finite consumer scan budget is exhausted")
                entries = sorted(index.entries, key=lambda entry: entry.obligation_id)
                offset = 0 if current is None or current.offset >= len(entries) else current.offset
                selected = tuple(entry.obligation_id for entry in entries[offset:offset + limit])
                page = _ConsumerPage(
                    configuration_digest=core.configuration_digest(), evidence_mode=core.registration.mode,
                    root_budget_id=root_budget_id, sequence=sequence + 1,
                    offset=offset + len(selected), selected=selected, active=True,
                )
                return ControlChanges(writes=(
                    core._write(page_key, page, "create" if current is None else "replace"),
                    core._write(control_key("delivery-consumer-page", root_budget_id, str(page.sequence)),
                                page, "create", _material(page.model_dump(mode="json"))),
                    core._write(quota_key, _update(quota, scans_remaining=quota.scans_remaining - 1)),
                ), result=core._result(page=page.model_dump(mode="json")))

            from .behavior_delivery_pending import transact
            transact(core, request, select, root_budget_id)
        page = core._row(authority_snapshot(core.store, (page_key,)), page_key, _ConsumerPage)
        results = []
        for obligation_id in page.selected:
            try:
                ob_key = control_key("delivery-obligation", obligation_id)
                ob = core._row(authority_snapshot(core.store, (ob_key,)), ob_key, _Obligation)
                results.append(DeliveryConsumer(core).consume(
                    obligation_id, resolve_context(ob.owner_record_id), worker_id))
            except Problem as exc:
                results.append(core._result(obligation_id=obligation_id, problem=exc.body["error"]["code"]))
        done = ControlRequest(
            request_id=control_key("delivery-consumer-page-done", root_budget_id, str(page.sequence)),
            request_digest=_digest(page.model_dump(mode="json")), read_set=(page_key,),
        )

        def complete(snapshot):
            current = core._row(snapshot, page_key, _ConsumerPage)
            if current != page:
                raise Problem(409, "DELIVERY_CURSOR_STALE", "the active consumer page differs")
            return ControlChanges(writes=(core._write(page_key, _update(page, active=False)),),
                                  result=core._result(page_completed=page.sequence))

        core.store.transact(done, complete)
        return tuple(results)
