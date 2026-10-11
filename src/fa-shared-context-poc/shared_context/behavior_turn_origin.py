"""Instruction attribution from existing server records, never action authority."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, model_validator

from .behavior_wire import Identifier, WireModel
from .errors import Problem

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


def instruction_turn_origin(
    task: Task, binding: RuntimeBinding, registration: WorkRegistration | None,
) -> InstructionTurnOrigin:
    instruction = task.instruction
    if instruction is None:
        raise Problem(409, "ASSISTANT_ORIGIN_UNAVAILABLE", "only an instruction turn has this origin view")
    if (task.job_id, task.work_id) != (binding.job_id, binding.work.work_id):
        raise Problem(403, "ASSISTANT_ORIGIN_DENIED", "turn origin differs from the assigned work")
    source_kind, actor_role, request_id, reply_target = None, None, None, None
    if registration is not None:
        recorded = registration.profile.instruction
        source = registration.agent_origin
        expected_oid = binding.human_oid if source is None else source.sender.worker.caller_oid
        if (registration.binding != binding or registration.principal_oid != expected_oid
                or recorded is None
                or (recorded.content, recorded.conversation_id, recorded.turn_number, recorded.files, recorded.attachments)
                != (instruction.content, instruction.conversation_id, instruction.turn_number, instruction.files, instruction.attachments)):
            raise Problem(403, "ASSISTANT_ORIGIN_DENIED", "recorded turn origin differs from the assignment")
        source_kind, actor_role = (("agent", "sending-agent") if source is not None else
                                  ("human", "original-requester") if registration.routine is None
                                  else ("routine", "routine-owner"))
        request_id, reply_target = registration.request_id, "original-conversation"
    return InstructionTurnOrigin(
        availability="recorded" if registration is not None else "not-recorded",
        source_kind=source_kind, actor_role=actor_role, request_id=request_id,
        binding_id=binding.binding_id, conversation_id=instruction.conversation_id,
        turn_number=instruction.turn_number,
        previous_turn_number=(instruction.turn_number - 1
                              if instruction.previous_archive_id is not None and instruction.turn_number > 1 else None),
        job_id=task.job_id, work_id=task.work_id, reply_target=reply_target, authorization=False,
    )
