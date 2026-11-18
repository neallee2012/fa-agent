"""Original-session delegation operations; the shared route verifies identity and ticket first."""

from .behavior_capabilities import BUILTIN_CAPABILITIES
from .behavior_capability_prompt import session_needs
from .behavior_delegation import DelegationLedger, capture_parent
from .behavior_delegation_wire import CAPABILITY_ID, DelegationReply
from .errors import Problem


def _scope(authority, session):
    task = session.assignment.task.instruction
    capture = None if task is None else task.capabilities
    grant = None if capture is None else next((item for item in capture.grants if item.entry_id == CAPABILITY_ID), None)
    if grant is None or grant.entry_sha256 != BUILTIN_CAPABILITIES[CAPABILITY_ID].entry_sha256:
        raise Problem(403, "DELEGATION_DISABLED", "this original turn did not capture the fixed delegation capability")
    try:
        session_needs(capture)
    except ValueError:
        raise Problem(403, "DELEGATION_DISABLED", "the captured fixed child configuration differs") from None
    return DelegationLedger(authority.store), capture_parent(authority, session, capability_sha256=grant.entry_sha256)


def _reply(view, *, fresh=False):
    return DelegationReply(
        parent_job_id=view.parent.execution.job_id, sdk_session_id=view.parent.sdk_session_id,
        delegation_id=view.delegation_id, tool_call_id=view.tool_call_id, input_sha256=view.input_sha256,
        child_agent_id=view.child_agent_id, status=view.status, result=view.result, failure_code=view.failure_code,
        fresh=fresh, cancel_requested=view.cancel_requested, child_cancelled=view.child_cancelled,
        parent_idle=view.parent_idle, stop_confirmed=view.stop_confirmed,
    )

