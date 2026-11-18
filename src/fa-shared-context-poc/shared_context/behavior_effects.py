"""Table-backed v1.2 work-goal adapter and the record shapes of its retired execution.

The approve/execute path (receiver, dispatcher send/acknowledge/reconcile) was retired with the legacy job lane
(2026-11-17, DECISIONS §12). What stays: the configured WorkStateEffectAdapter the server registers on its
ToolBrokerCore (aliases, validation, inspect), the goal shapes in configuration, and the dispatcher shell the
operation preview reads its execution contract from. No Azure setup, credentials or HTTP mutation here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator

from .behavior_delivery import DeliveryCore
from .behavior_dispatch import BehaviorDispatcher, DispatchObligation, DispatchRecord
from .behavior_operations import (
    AdapterContract, ExecutionContext, GrantRecord, OperationDescription, PolicyRecord, RegisteredAdapter,
    ResolvedResource, ToolBrokerCore, _Attempt, _Model, _Operation, _checked, _digest, _json, _row, _utc, control_key,
)
from .behavior_store import BehaviorStore, ControlRequest, Digest, InlineMaterial
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


@dataclass(frozen=True)
class _Consumed:
    """The rows of one receiver decision, parsed once from its guarded snapshot."""

    command: WorkEffectDelivery
    context: ExecutionContext
    envelope: _Envelope
    operation: _Operation
    material: InlineMaterial
    description: OperationDescription
    attempt: _Attempt
    dispatch: DispatchRecord
    responsibility: DispatchObligation
    retained: _Envelope


def _policy_differs(seen: _Consumed, grant: GrantRecord, policy: PolicyRecord) -> bool:
    policy_id = seen.context.policy_id
    return (
        grant.policy_id != policy_id or policy.policy_id != policy_id or not policy.active
        or policy.revision != grant.policy_revision or policy.revision != seen.attempt.policy_revision
        or policy.revision != seen.dispatch.policy_revision
    )


class WorkStateDispatcher(BehaviorDispatcher):
    def __init__(self, core: ToolBrokerCore, adapter: WorkStateEffectAdapter):
        super().__init__(core, adapter)


