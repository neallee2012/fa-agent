"""Hosted ``propose_routine``: the assistant drafts a daily routine; only the owner's click on its card schedules it.

Registered only when native routines, routine proposals and native HITL are on. The handler runs only after the
owner allowed this exact draft on the approval card, when the API has already saved it; it writes nothing itself.
"""

from datetime import datetime, timezone

from .m2_copilot import ToolFailure, ToolOutput, ToolSpec
from .native_routine_wire import (
    ROUTINE_PROPOSAL_TOOL, NativeDailySchedule, NativeRoutineProposal, NativeRoutineProposalResult, next_daily_run,
)

DESCRIPTION = (
    "Propose a daily scheduled routine when the user asks you to do something every day at a set time "
    "(for example: check a site for new articles every morning at 9). Use this instead of cron, shell timers or "
    "sleeping in the sandbox: this environment sleeps when idle and cannot keep its own timers. The owner must "
    "approve the exact draft on an approval card; until then nothing is scheduled, and neither your words nor a "
    "chat reply count as approval. Once approved it runs every day, each time in a new conversation that has none "
    "of this or earlier runs' history, and each run's tool calls still follow the normal approval rules. One call proposes one routine; to change one, the owner disables it "
    "in settings and you propose a new one.")


def propose_routine_tool(clock=None, *, default_time_zone=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    description = DESCRIPTION
    if default_time_zone is not None:
        try:
            NativeDailySchedule.known_zone(default_time_zone)
        except ValueError:
            default_time_zone = None
    if default_time_zone is not None:
        # The plan's default: the Agent's own deployed zone (Hosted TZ), not a value written into the code.
        description += f" If the user does not name a time zone, use this Agent's time zone {default_time_zone}."
    else:
        description += " If the user does not name a time zone, ask them."

    async def handle(packet):
        try:
            draft = NativeRoutineProposal.model_validate(packet["arguments"])
        except (ValueError, KeyError, TypeError):
            raise ToolFailure("The routine draft is invalid; nothing was scheduled.",
                              public_code="NATIVE_ROUTINE_INVALID") from None
        schedule = draft.schedule
        return ToolOutput(NativeRoutineProposalResult(
            schedule=schedule, content=draft.content, next_run_at=next_daily_run(schedule, clock()),
        ).model_dump_json())

    return ToolSpec(ROUTINE_PROPOSAL_TOOL, description, NativeRoutineProposal.model_json_schema(), handle)
