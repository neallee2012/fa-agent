"""Instruction attribution from existing server records, never action authority."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, model_validator

from .behavior_wire import Identifier, WireModel

if TYPE_CHECKING:
    from .behavior_assistant_wire import Task
    from .behavior_runtime import RuntimeBinding
    from .behavior_work_policy import WorkRegistration


TurnOriginProfile = Literal["fa-instruction-context-origin-v1"]


class InstructionTurnOrigin(WireModel):
    availability: Literal["recorded", "not-recorded"]
    source_kind: Literal["human", "routine", "agent"] | None
    actor_role: Literal["original-requester", "routine-owner", "sending-agent"] | None
    request_id: Identifier | None
    binding_id: Identifier
    conversation_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]
    previous_turn_number: Annotated[int, Field(ge=1)] | None
    job_id: Identifier
    work_id: Identifier
    reply_target: Literal["original-conversation"] | None
    authorization: Literal[False]

    @model_validator(mode="after")
    def attribution_is_explicit(self):
        facts = (self.source_kind, self.actor_role, self.request_id, self.reply_target)
        if self.availability == "recorded":
            if any(value is None for value in facts):
                raise ValueError("Recorded origin requires the original request attribution")
            if (self.source_kind, self.actor_role) not in {
                ("human", "original-requester"), ("routine", "routine-owner"), ("agent", "sending-agent"),
            }:
                raise ValueError("The source kind and original actor role differ")
        elif any(value is not None for value in facts):
            raise ValueError("Missing origin cannot acquire inferred attribution")
        if self.previous_turn_number is not None and self.previous_turn_number != self.turn_number - 1:
            raise ValueError("Only the original linear previous turn is described")
        return self

