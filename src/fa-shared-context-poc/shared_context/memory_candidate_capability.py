"""Hosted staging adapter; effective memory still belongs to the human review path."""

from .behavior_assistant_deadlines import hosted_sync
from .behavior_capabilities import BUILTIN_CAPABILITIES, CAPABILITY_TOOL_GUIDANCE
from .errors import Problem
from .m2_copilot import ToolFailure, ToolOutput, ToolSpec
from .memory_candidate_wire import (
    CandidateArguments, CandidateCommand, CandidateReceipt, MEMORY_CANDIDATE_GUIDANCE,
)

ENTRY_ID = "fa-memory-candidate-v1"
CAPABILITY_TOOL_GUIDANCE.update(MEMORY_CANDIDATE_GUIDANCE)


def memory_candidate_capability(session, grant):
    builtin = BUILTIN_CAPABILITIES[ENTRY_ID]
    if (grant.entry_id != ENTRY_ID or grant.kind != "fa-builtin"
            or grant.entry_sha256 != builtin.entry_sha256 or grant.tools != builtin.tools or grant.vision):
        raise Problem(503, "CAPABILITY_UNAVAILABLE", "the original candidate capability differs")
    original, confirmed = None, None

    async def handle(packet):
        nonlocal original, confirmed
        try:
            arguments = CandidateArguments.model_validate(packet["arguments"])
            command = CandidateCommand(request_id="stage-memory-candidate", **arguments.model_dump())
        except (KeyError, ValueError, TypeError, Problem):
            raise ToolFailure("Use one bounded exact quote or an explicit no_candidate decision.",
                              public_code="CAPABILITY_ARGUMENTS") from None
        async with session._authority_lock:
            if original is not None and original != command:
                raise ToolFailure("Keep the original turn decision; no replacement was sent.",
                                  public_code="MEMORY_CANDIDATE_CONFLICT")
            if confirmed is None:
                first = original is None
                original = command
                call = (session.client.stage_memory_candidate if first
                        else session.client.reconcile_memory_candidate)
                try:
                    result = await hosted_sync(call, command)
                except Problem as error:
                    code = error.body["error"]["code"]
                    raise ToolFailure(
                        "No effective memory was created; keep answering and do not resend an unknown candidate.",
                        public_code=code if code in MEMORY_CANDIDATE_GUIDANCE else "CAPABILITY_UNAVAILABLE",
                    ) from None
                if (result.binding_id != session.client.assignment.binding_id
                        or result.decision != command.decision):
                    raise ToolFailure("The original candidate receipt did not match this turn.",
                                      public_code="CAPABILITY_RESULT_INVALID")
                confirmed = result
            return ToolOutput(confirmed.model_dump_json())

    tool = ToolSpec(
        "stage_memory_candidate",
        "Suggest one exact, unique quote from the current user's message, or explicitly no_candidate. "
        "Never quote secrets, approvals, greetings or one-turn formatting as lasting knowledge. "
        "This is an inactive suggestion: the original answer must first be saved, then the human must share, "
        "review and commit it. Do not claim to have saved effective memory.",
        CandidateArguments.model_json_schema(), handle,
    )
    return (tool,), {tool.name: CandidateReceipt}
