"""Per-turn model usage a Hosted worker reports once, in its end receipt (ADR-0006 §5 A2).

The VM calls the model directly, so there is no per-request reserve or consume. Usage is summed per
deployment in memory and reported once when the turn ends. Only standard provider usage counts are
kept, never prompts, model text or reasoning content. ``reasoning_tokens`` is already part of
``output_tokens``; price only input and output.

- A request counts once its send has started. Requests refused before sending are not counted.
- A counted request without valid provider usage (missing, invalid, broken stream) is unknown:
  it adds to ``requests`` but not to ``measured_requests`` and contributes no token counts.
  Missing counts are never 0.
- A turn with no end receipt has no ``TurnModelUsage`` at all. Its usage stays unknown; nothing is
  refunded, zeroed or resent.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_wire import Identifier, ModelDeployment, WireModel

TURN_USAGE_SCHEMA = "fa-turn-model-usage-v1"
MAX_USAGE_GROUPS = 4
# Far above any per-turn model-call limit; only bounds a malformed report.
MAX_TURN_REQUESTS = 10_000
Count = Annotated[int, Field(ge=0)]


class ModelUsage(WireModel):
    """One deployment and wire API in one turn; token sums cover only the measured requests."""
    model: ModelDeployment
    # N1 meters either direct protocol. Legacy API-routed Completions never reports it here.
    wire_api: Literal["responses", "completions"]
    requests: Annotated[int, Field(ge=1, le=MAX_TURN_REQUESTS)]
    measured_requests: Annotated[int, Field(ge=0, le=MAX_TURN_REQUESTS)]
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    # Responses only; present only when every measured request reported it.
    reasoning_tokens: Count | None = None

    @model_validator(mode="after")
    def _consistent(self):
        measured = self.measured_requests > 0
        if (self.measured_requests > self.requests
                or (self.input_tokens is None) == measured or (self.output_tokens is None) == measured
                or self.reasoning_tokens is not None and (
                    not measured or self.output_tokens is None or self.reasoning_tokens > self.output_tokens)):
            raise ValueError("measured usage must be complete, bounded and never inferred as 0")
        return self


class TurnModelUsage(WireModel):
    # Both fields are required: a report missing them must not read as "no request was sent".
    schema_version: Literal["fa-turn-model-usage-v1"]
    usage: Annotated[tuple[ModelUsage, ...], Field(max_length=MAX_USAGE_GROUPS)]

    @model_validator(mode="after")
    def _distinct(self):
        groups = [(item.model, item.wire_api) for item in self.usage]
        if len(set(groups)) != len(groups):
            raise ValueError("each deployment and wire API is reported once")
        if sum(item.requests for item in self.usage) > MAX_TURN_REQUESTS:
            raise ValueError("turn usage exceeds its bound")
        return self

    @property
    def state(self) -> Literal["none", "complete", "partial"]:
        """``none``: no model request was sent; ``partial``: some sent request's usage is unknown."""
        if not self.usage:
            return "none"
        return "complete" if all(item.measured_requests == item.requests for item in self.usage) else "partial"


AGENT_TURN_USAGE_PATH = "/work-policy/v1/turn-usage"
# ADR-0011: "memory" is the Hosted private-memory extraction's own model call, reported once with the turn's RID.
AgentUsageSource = Literal["model", "memory"]


class AgentTurnUsageReport(WireModel):
    """ADR-0007: the VM's report of one final native turn, sent once when the turn ends.

    ``usage`` is required; ``None`` explicitly means the turn ended without trusted usage (unknown).
    The Agent is the verified caller, never a body field. Resending the same report for the same
    ``request_id`` returns the original result; a different report is refused with 409.
    """
    request_id: Identifier
    usage: TurnModelUsage | None
    # Not serialized when "model", so a turn report keeps its original bytes and an older API still accepts it.
    source: AgentUsageSource = Field(default="model", exclude_if=lambda value: value == "model")
