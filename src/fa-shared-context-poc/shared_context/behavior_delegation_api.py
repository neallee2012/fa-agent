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


def prepare(authority, session, command, principal):
    ledger, parent = _scope(authority, session)
    result = ledger.prepare(parent, command.tool_call_id, command.arguments.model_dump(mode="json"))
    return _reply(result.view, fresh=result.fresh)


def read(authority, session, command, principal):
    ledger, parent = _scope(authority, session)
    return _reply(ledger.read(parent))


def observe(authority, session, command, principal):
    ledger, parent = _scope(authority, session)
    original = ledger.read(parent)
    if command.tool_call_id != original.tool_call_id:
        raise Problem(409, "DELEGATION_SCOPE_CHANGED", "only the original task observation is accepted")
    if command.kind == "started":
        value = ledger.record_started(parent, command.tool_call_id, command.child_agent_id, event_id=command.event_id)
    elif command.kind == "result":
        value = ledger.record_result(parent, command.tool_call_id, command.child_agent_id,
            event_id=command.event_id, outcome=command.outcome, text=command.text,
            ended_in_tool_call=command.ended_in_tool_call, failure_code=command.failure_code)
    elif command.kind == "cancel":
        value = ledger.request_cancel(parent, request_id=command.event_id)
    elif command.kind == "parent_idle":
        value = ledger.record_parent_idle(parent, command.tool_call_id, command.child_agent_id, event_id=command.event_id)
    else:
        value = ledger.record_unknown(parent, command.tool_call_id, event_id=command.event_id,
                                      reason_code=command.failure_code, child_agent_id=command.child_agent_id)
    return _reply(value)


def validate_model(authority, session, reference):
    ledger, parent = _scope(authority, session)
    view = ledger.read(parent)
    if (view.status != "running" or view.cancel_requested or view.tool_call_id != reference.tool_call_id
            or view.child_agent_id != reference.child_agent_id):
        raise Problem(403, "DELEGATION_MODEL_DENIED", "the original active child and task must match before model dispatch")


def link_model(authority, session, reference, request_sha256):
    ledger, parent = _scope(authority, session)
    return ledger.link_model_request(parent, reference.tool_call_id, reference.child_agent_id, request_sha256)
