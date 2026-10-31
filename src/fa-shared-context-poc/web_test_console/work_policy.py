"""Local normalized read seam, NOT a backend HTTP schema or an authority grant.

The remaining DTOs are shared by the job-bound instruction lane's preflight
reader. The workbench no longer uses them to gate legacy fixed tasks.
"""
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, field_validator

from shared_context import behavior_wire as wire
from .adapter import require


class CumulativeBudget(wire.WireModel):
    budget_id: wire.Identifier
    unit: wire.Identifier
    remaining: Annotated[int, Field(ge=0)] | None
    required: Annotated[int, Field(ge=0)]
    enforced: bool = Field(default=True, exclude_if=lambda v:v)


class WorkPolicyQuery(wire.WireModel):
    read_id: wire.Identifier
    backend_origin: str
    backend_configuration_sha256: wire.Digest
    backend_core_sha256: wire.Digest
    tenant_id: wire.Identifier
    human_oid: wire.Identifier
    client_id: wire.Identifier
    binding_id: wire.Identifier
    job_id: wire.Identifier
    work_id: wire.Identifier
    root_request_id: wire.Identifier
    route: wire.DeliveryRoute
    submit_request_id: wire.Identifier
    submit_body_sha256: wire.Digest


class WorkPolicyObservation(wire.WireModel):
    query: WorkPolicyQuery
    decision: Literal["allow", "deny", "needs_approval"]
    reason: Literal["within_budget", "accounting_only", "expired", "budget_exhausted", "revoked", "not_ready", "approval_required"]
    revision: wire.Identifier
    observed_at: datetime
    execution_deadline: datetime | None
    budget: CumulativeBudget

    @field_validator("observed_at", "execution_deadline")
    @classmethod
    def utc(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
            raise ValueError("explicit UTC observation/work deadline required")
        return value


def checked_observation(observation, query, now, *, instruction=False):
    from .standing import StandingWorkObservation
    require(type(observation) in {WorkPolicyObservation, StandingWorkObservation}, "WORK_POLICY_INVALID", 502)
    observation = type(observation).model_validate_json(observation.model_dump_json())
    require(not instruction or type(observation) is StandingWorkObservation, "WORK_POLICY_INVALID", 502)
    require(observation.query == query
            and (instruction or observation.observed_at <= now), "WORK_POLICY_INVALID", 502)
    return observation
