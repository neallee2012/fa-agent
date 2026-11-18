"""Bridge a fresh VM begin to the original SDK session and data helpers, without a bearer ticket."""


from .behavior_assistant import _Session, key
from .behavior_auth import BehaviorPrincipal
from .behavior_operations import ExecutionContext, OwnerRecord, _row, control_key
from .behavior_turn_receipts import TurnStartRecord, TurnTicketRecord, start_key, ticket_handle, turn_key
from .behavior_workers import JobRecord, worker_key
from .errors import Problem
from .source_materials import digest


def vm_session_keys(authority, session):
    binding = authority._binding(session)
    return (
        session.vm_turn_ticket_id, start_key(session.vm_turn_ticket_id),
        key("session", session.assignment.session_id), worker_key("job", binding.job_id),
        control_key("owner", binding.owner.owner_record_id), authority.runtime.dep.workers.pool_key(binding.job_id),
    )


def _execution_state(authority, session, rows, *, executing):
    binding = authority._binding(session)
    control = authority.runtime.work_policy
    record = None if control is None else control.records.get(binding.binding_id)
    job = _row(rows, worker_key("job", binding.job_id), JobRecord)
    owner = _row(rows, control_key("owner", binding.owner.owner_record_id), OwnerRecord)
    context = ExecutionContext(**{field: getattr(owner, field) for field in ExecutionContext.model_fields})
    if (record is None or record.binding != binding or context != session.execution.owner
            or job.binding != session.execution or job.owner != context
            or authority.runtime.job_input(binding) != job.spec.input
            or job.spec.input != session.assignment.task.model_dump_json().encode()):
        raise Problem(403, "ASSISTANT_EXECUTION_FENCED", "the original current job, input and execution are required")
    if executing:
        pool = authority.runtime.dep.workers._pool(rows)
        if (job.cancel_requested or job.state not in {"starting", "running", "capacity_uncertain"}
                or sum(slot.binding == session.execution for slot in pool.slots) != 1):
            raise Problem(403, "ASSISTANT_EXECUTION_FENCED", "the original running job or reservation is no longer current")
    return record.runtime_policy, context


def vm_session_state(authority, session, rows, *, executing):
    handle_id = session.vm_turn_ticket_id
    ticket = _row(rows, handle_id, TurnTicketRecord)
    start = _row(rows, start_key(handle_id), TurnStartRecord).start
    current = _row(rows, key("session", session.assignment.session_id), _Session)
    if (session.ticket_digest is not None or not ticket.started
            or ticket_handle(ticket.ticket).ticket_id != handle_id
            or start.ticket != ticket_handle(ticket.ticket)
            or start.worker.vm != ticket.ticket.vm or start.worker.caller_oid != ticket.ticket.agent_oid
            or start.job_id != session.execution.job_id or start.address != ticket.ticket.address
            or ticket.ticket.assignment != session.assignment
            or current.assignment != session.assignment or current.execution != session.execution
            or current.vm_turn_ticket_id != handle_id or current.ticket_digest is not None):
        raise Problem(403, "ASSISTANT_EXECUTION_FENCED", "the saved start and original VM SDK session must agree")
    if executing and ticket.cancel_requested:
        raise Problem(403, "ASSISTANT_EXECUTION_FENCED", "the original admitted turn has been cancelled")
    if executing and ticket.ticket.relay is not None and authority.runtime._now() >= ticket.ticket.relay.deadline:
        raise Problem(409, "TURN_DEADLINE_EXPIRED", "the original relay turn deadline has passed")
    return _execution_state(authority, session, rows, executing=executing)


def read_vm_session(authority, principal, context, handle):
    """The route owns T2's connection lease; this reader verifies the original start and SDK data."""
    if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "agent"
            or principal.oid != context.caller_oid or principal.team != context.vm.team_id):
        raise Problem(403, "TURN_WORKER_DENIED", "the current signed worker handshake is required")
    principal.require(context.vm.team_id, "execute")
    captured = authority._vm_starts.get(handle.ticket_id)
    if captured is None:
        records = authority.store.read_snapshot((handle.ticket_id, start_key(handle.ticket_id)))
        original = _row(records, handle.ticket_id, TurnTicketRecord)
        ticket = original.ticket
        started = _row(records, start_key(handle.ticket_id), TurnStartRecord).start
        if not original.started:
            raise Problem(403, "TURN_WORKER_DENIED", "the original start has not committed")
    else:
        ticket, started = captured
    expected_key = turn_key(principal.tenant_id, principal.team, context.vm.agent_id, ticket.address.request_id)
    if (handle.ticket_id != expected_key or handle != ticket_handle(ticket)
            or ticket.tenant_id != principal.tenant_id or ticket.agent_oid != principal.oid
            or ticket.vm != context.vm or started.worker.vm != context.vm
            or started.worker.process_id != context.process_id or started.worker.caller_oid != principal.oid
            or started.actor_id != principal.actor_id or started.ticket != handle):
        raise Problem(403, "TURN_WORKER_DENIED", "the current worker does not own the original started turn")
    skey = key("session", ticket.assignment.session_id)
    session = _row({skey: authority.store.read_record(skey)}, skey, _Session)
    if (session.vm_turn_ticket_id != handle.ticket_id or session.ticket_digest is not None
            or session.assignment != ticket.assignment or session.execution.job_id != started.job_id
            or ticket.assignment.sdk_start_binding is not None and
            ticket.assignment.sdk_start_binding.execution_digest != digest(session.execution.model_dump(mode="json"))):
        raise Problem(403, "ASSISTANT_EXECUTION_FENCED", "the original started SDK session differs")
    if captured is None:
        authority._vm_starts[handle.ticket_id] = ticket, started
    return session
