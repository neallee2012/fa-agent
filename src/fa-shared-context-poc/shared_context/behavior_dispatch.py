"""Dispatch record shapes and the dispatcher base that pins the concrete adapter.

The arm/consume/send lifecycle was retired with the legacy job lane (2026-11-17, DECISIONS §12). DispatchRecord
and DispatchObligation stay so stored rows still parse; "fa-context-one-send-1" (EXECUTION_CONTRACT) stays
accepted for the same reason. ArtifactMetrics is the numeric result schema the assistant reads and saves.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import field_validator, model_validator

from .behavior_operations import (
    AttemptBinding, Count, ExecutionContext, ToolBrokerCore, _Model, _digest, _utc, control_key,
)
from .behavior_store import Digest
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


class BehaviorDispatcher:
    def __init__(self, core: ToolBrokerCore, adapter: WorkStateEffectAdapter):
        from .behavior_effects import WorkStateEffectAdapter

        if type(adapter) is not WorkStateEffectAdapter or core._enabled()[0] is not adapter.registration:
            raise Problem(403, "OPERATION_EXECUTION_DISABLED", "the concrete adapter and registered contract must be identical")
        if core._contract_digest() != _digest(adapter.contract.model_dump(mode="json")):
            raise Problem(403, "OPERATION_EXECUTION_DISABLED", "execution registration differs")
        self.core, self.adapter, self.store = core, adapter, core.store

