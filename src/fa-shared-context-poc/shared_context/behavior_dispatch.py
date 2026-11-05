"""One-consume arm/consume lifecycle over BR-01 (WorkStateDispatcher builds on it).

The existing authorize receipt is only a reservation. A dispatcher additionally
arms once, then consumes that arm with a fresh guard-protected gate. Only the
caller that observed BOTH fresh commits may send. A replay, crash, or uncertain
consume never grants another send, even when read-back finds nothing. Reservations
and the dispatch-obligation survive every failure. No automatic retry/refund/GC.

The concrete adapter is behavior_effects.WorkStateEffectAdapter; this base only
accepts that type and registered contract. Its subclass sends, reconciles and
records outcomes. ArtifactMetrics is the numeric result schema the assistant
reads and saves; no adapter in this module reads it over HTTP any more.

FA admission window: a consume is the revocation linearization point; one
transport entry is allowed within TWO trusted UTC seconds (SEND_WINDOW_SECONDS,
also before grant expiry), rechecked by _consume with _within. Subsequent changes
are in-flight, NOT retroactive cancellation. This is a SEND window, not a bound
on remote completion, and not instantaneous Entra revocation or cross-service
atomic IAM. "fa-context-one-send-1" (EXECUTION_CONTRACT) stays accepted so a stored
DispatchRecord row carrying it still parses; production never constructed the old
HTTP adapter, so none is expected. New rows carry the adapter's execution_contract.

Data rows use control_key("dispatch", operation, attempt) and
control_key("dispatch-obligation", operation, attempt), under the SAME authority
as owner/policy/grant/budget. All their writers must use BehaviorStore's guard.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import field_validator, model_validator

from .behavior_operations import (
    AttemptBinding, AuthorizationRequest, BudgetRecord, Count, ExecutionContext,
    GrantRecord, GrantSubject, PolicyRecord, ToolBrokerCore, _Attempt, _Model,
    _checked, _digest, _row, _updated, _utc, _write, control_key,
)
from .behavior_store import ControlChanges, ControlRequest, Digest, InlineMaterial
from .errors import Problem
from .models import Identifier

if TYPE_CHECKING:
    from .behavior_effects import WorkStateEffectAdapter

EXECUTION_CONTRACT = "fa-context-one-send-1"
SEND_WINDOW_SECONDS = 2


class ArtifactMetrics(_Model):
    """The entire supported artifact output schema; no arbitrary string fields."""

    schema_version: Literal["fa-artifact-metrics-v1"]
    item_count: Count
    completed_count: Count

    @model_validator(mode="after")
    def counts(self):
        if self.completed_count > self.item_count:
            raise ValueError("completed count exceeds item count")
        return self


class DispatchRecord(_Model):
    kind: Literal["operation_dispatch"] = "operation_dispatch"
    binding: AttemptBinding
    context: ExecutionContext
    grant_id: Identifier
    consume_request_id: Digest
    execution_contract: Literal["fa-context-one-send-1", "fa-table-work-effect-1"] = EXECUTION_CONTRACT
    phase: Literal["armed", "consumed", "settled"] = "armed"
    responsibility: Literal["reconcile_original", "resolved"] = "reconcile_original"
    armed_at: datetime
    gated_at: datetime | None = None
    not_after: datetime | None = None
    policy_revision: Count | None = None
    grant_revocation_revision: Count | None = None
    effect_state: Literal["unknown", "not_applied", "applied", "partial"] = "unknown"
    result_digest: Digest | None = None

    @field_validator("armed_at", "gated_at", "not_after")
    @classmethod
    def utc_times(cls, value):
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def lifecycle(self):
        gates = (self.gated_at, self.not_after, self.policy_revision, self.grant_revocation_revision)
        if self.phase == "armed":
            if any(value is not None for value in gates):
                raise ValueError("arming is not a completed gate")
        else:
            if any(value is None for value in gates):
                raise ValueError("consume requires the entire saved gate")
            if not self.armed_at <= self.gated_at < self.not_after:
                raise ValueError("invalid gate interval")
            if self.not_after - self.gated_at > timedelta(seconds=SEND_WINDOW_SECONDS):
                raise ValueError("gate exceeds the supported send window")
        if self.phase == "settled":
            if self.effect_state != "applied" or self.responsibility != "resolved" or self.result_digest is None:
                raise ValueError("settled reads require a protected result")
        elif self.effect_state != "unknown" or self.responsibility != "reconcile_original" or self.result_digest is not None:
            raise ValueError("unsettled reads retain unknown responsibility")
        return self


class DispatchObligation(_Model):
    kind: Literal["operation_dispatch_obligation"] = "operation_dispatch_obligation"
    binding: AttemptBinding
    owner_record_id: Identifier
    root_budget_id: Identifier
    consume_request_id: Digest
    state: Literal["pending_reconciliation", "resolved"] = "pending_reconciliation"


@dataclass(frozen=True)
class _Invocation:
    binding: AttemptBinding
    context: ExecutionContext
    material: InlineMaterial
    gated_at: datetime
    not_after: datetime
    request_id: str


def _within(clock: Callable[[], datetime], start: datetime, end: datetime) -> None:
    try:
        current = _utc(clock())
    except Exception:
        raise Problem(503, "OPERATION_CLOCK_INVALID", "trusted UTC is unavailable") from None
    if not start <= current < end:
        raise Problem(
            409, "OPERATION_DISPATCH_WINDOW_CLOSED",
            "the one-send window closed; reservation and reconciliation responsibility remain",
        )


class BehaviorDispatcher:
    def __init__(self, core: ToolBrokerCore, adapter: WorkStateEffectAdapter):
        from .behavior_effects import WorkStateEffectAdapter

        if type(adapter) is not WorkStateEffectAdapter or core._enabled()[0] is not adapter.registration:
            raise Problem(403, "OPERATION_EXECUTION_DISABLED", "the concrete adapter and registered contract must be identical")
        if core._contract_digest() != _digest(adapter.contract.model_dump(mode="json")):
            raise Problem(403, "OPERATION_EXECUTION_DISABLED", "execution registration differs")
        self.core, self.adapter, self.store = core, adapter, core.store

    @staticmethod
    def _keys(command) -> tuple[str, str]:
        return (
            control_key("dispatch", command.operation_id, command.attempt_id),
            control_key("dispatch-obligation", command.operation_id, command.attempt_id),
        )

    def reconciliation_request(
        self, command: AuthorizationRequest, context: ExecutionContext, *,
        stage: Literal["arm", "consume"],
    ) -> ControlRequest:
        command, context = _checked(command, AuthorizationRequest), _checked(context, ExecutionContext)
        if stage not in {"arm", "consume"}:
            raise Problem(422, "OPERATION_INVALID_DATA", "unknown dispatch stage")
        base = self.core.reconciliation_request(command, context)
        return ControlRequest(
            request_id=control_key("dispatch-" + stage, command.operation_id, command.attempt_id),
            request_digest=_digest([
                self.adapter.execution_contract, stage, base.model_dump(mode="json"),
                self.adapter.settings.model_dump(mode="json"),
            ]),
            read_set=tuple(sorted(set((*base.read_set, *self._keys(command))))),
        )

    def _attempt(self, snapshot, command, context):
        self.core._owner(snapshot, context, command.operation_id)
        operation, material, description = self.core._operation(snapshot, command.operation_id, context)
        attempt = _row(snapshot, control_key("attempt", command.operation_id, command.attempt_id), _Attempt)
        self.adapter.validate(description, context)
        if (
            operation.current_attempt_id != command.attempt_id or attempt.context != context
            or attempt.grant_id != command.grant_id or attempt.observation_sequence
            or attempt.effect_state != "unknown" or operation.effect_state != "unknown"
            or attempt.binding.operation_id != command.operation_id or attempt.binding.attempt_id != command.attempt_id
            or attempt.binding.description_digest != material.sha256
            or attempt.binding.adapter_contract_digest != self.core._contract_digest()
            or attempt.binding.target != description.target
            or attempt.binding.idempotency_key != control_key("idempotency", command.operation_id, material.sha256)
            or operation.unsettled_attempt_ids != (command.attempt_id,)
        ):
            raise Problem(409, "OPERATION_DISPATCH_DENIED", "attempt is not the original unresolved reservation")
        return attempt, material

    def _arm(self, command, context) -> None:
        request = self.reconciliation_request(command, context, stage="arm")
        dispatch_key, obligation_key = self._keys(command)
        fresh = False

        def decide(snapshot):
            nonlocal fresh
            fresh = True
            attempt, _ = self._attempt(snapshot, command, context)
            if snapshot[dispatch_key] is not None or snapshot[obligation_key] is not None:
                raise Problem(409, "OPERATION_DISPATCH_CONSUMED", "the original dispatch is already owned")
            consume_id = self.reconciliation_request(command, context, stage="consume").request_id
            dispatch = DispatchRecord(
                binding=attempt.binding, context=context, grant_id=command.grant_id,
                consume_request_id=consume_id, armed_at=self.core._now(),
                execution_contract=self.adapter.execution_contract,
            )
            obligation = DispatchObligation(
                binding=attempt.binding, owner_record_id=context.owner_record_id,
                root_budget_id=context.root_budget_id, consume_request_id=consume_id,
            )
            return ControlChanges(writes=(
                _write(dispatch_key, dispatch, "create"),
                _write(obligation_key, obligation, "create"),
            ), result={"dispatch": dispatch.model_dump(mode="json"), "dispatch_permission": False})

        self.store.transact(request, decide)
        if not fresh:
            raise Problem(
                409, "OPERATION_DISPATCH_CONSUMED",
                "historical arming is not permission; reconcile the original dispatch without another send",
            )

    def _consume(self, command, context) -> _Invocation:
        request = self.reconciliation_request(command, context, stage="consume")
        dispatch_key, obligation_key = self._keys(command)
        invocation = None

        def decide(snapshot):
            nonlocal invocation
            attempt, material = self._attempt(snapshot, command, context)
            dispatch = _row(snapshot, dispatch_key, DispatchRecord)
            obligation = _row(snapshot, obligation_key, DispatchObligation)
            if (
                dispatch.phase != "armed" or dispatch.binding != attempt.binding
                or dispatch.context != context or dispatch.grant_id != command.grant_id
                or obligation.binding != attempt.binding or obligation.state != "pending_reconciliation"
                or obligation.owner_record_id != context.owner_record_id
                or obligation.root_budget_id != context.root_budget_id
                or dispatch.consume_request_id != request.request_id or obligation.consume_request_id != request.request_id
            ):
                raise Problem(409, "OPERATION_DISPATCH_CONSUMED", "dispatch ownership or responsibility differs")
            grant = _row(snapshot, control_key("grant", command.grant_id), GrantRecord)
            policy = _row(snapshot, control_key("policy", context.policy_id), PolicyRecord)
            budget = _row(snapshot, control_key("budget", context.root_budget_id), BudgetRecord)
            now = self.core._now()
            if (
                grant.grant_id != command.grant_id or grant.revoked
                or grant.revocation_revision != attempt.grant_revocation_revision
                or GrantSubject(principal_id=context.principal_id, conversation_id=context.conversation_id) not in grant.subjects
                or grant.root_budget_id != context.root_budget_id or grant.description_digest != material.sha256
                or grant.adapter_contract_digest != self.core._contract_digest()
                or grant.policy_id != context.policy_id or policy.policy_id != context.policy_id
                or not policy.active or policy.revision != grant.policy_revision or policy.revision != attempt.policy_revision
                or not grant.not_before <= now < min(grant.expires_at, attempt.not_after)
                or now < dispatch.armed_at or now < attempt.authorized_at
            ):
                raise Problem(403, "OPERATION_GRANT_DENIED", "current human grant, revision or execution window differs")
            # authorize already reserved this exact attempt; zero REMAINING uses
            # is valid, not a reason to charge twice or reuse another reservation.
            if (
                not budget.active or budget.root_budget_id != context.root_budget_id
                or attempt.reserved_units != self.adapter.contract.reservation_units
            ):
                raise Problem(403, "OPERATION_BUDGET_DENIED", "the reserved root budget is not available")
            deadline = min(attempt.not_after, grant.expires_at, now + timedelta(seconds=SEND_WINDOW_SECONDS))
            consumed = _updated(
                dispatch, phase="consumed", gated_at=now, not_after=deadline,
                policy_revision=policy.revision, grant_revocation_revision=grant.revocation_revision,
            )
            invocation = _Invocation(attempt.binding, context, material, now, deadline, request.request_id)
            changes = ControlChanges(writes=(
                _write(dispatch_key, consumed, "replace"),
                _write(obligation_key, obligation, "replace"),
            ), result={"dispatch": consumed.model_dump(mode="json"), "dispatch_permission": False})
            return self._consume_changes(invocation, attempt, changes)

        self.store.transact(request, decide)
        if invocation is None:
            raise Problem(409, "OPERATION_DISPATCH_CONSUMED", "a consumed receipt never permits another dispatch")
        _within(self.core.clock, invocation.gated_at, invocation.not_after)
        return invocation

    def _consume_changes(self, invocation: _Invocation, attempt: _Attempt, changes: ControlChanges) -> ControlChanges:
        return changes
