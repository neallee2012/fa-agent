"""Drive the retained peer work through the existing controller and C1 worker."""

from typing import Literal
from pydantic import Field
import logging

from .behavior_agent_message_api import _read_root, _receiver_busy, _receiver_record, collaboration_key
from .behavior_agent_messages import (
    AgentMessage, AgentMessageLedger, MessageEnvelope, _RootState, _QueueClaim, _MessageState,
    claim_key, message_key, message_state_key, root_key, root_state_key,
)
from .behavior_auth import BehaviorPrincipal
from .behavior_delegation import _current_parent, _parent_keys
from .behavior_operations import _Model, _row, _updated, _write, control_key
from .behavior_runtime import material
from .behavior_store import ControlChanges, ControlRequest
from .behavior_wire import Digest, Identifier, JobStatus
from .behavior_workers import ExecutorEvidence, JobRecord, JobSpec, _Pool, worker_key
from .errors import Problem
from .source_materials import canonical, digest
from .behavior_agent_message_wire import MAX_AGENT_MESSAGE_BYTES


SAVED_RESULT_LIMIT_NOTICE = "The complete peer result is saved, but exceeds the peer message limit; manual result access is required."
STOPPED_PEER_NOTICE = (
    "The original peer work is closed by verified stop or fencing evidence. "
    "No successful peer result is available; do not resend the work or infer its effects."
)
REFERENCE_REFUSALS = frozenset({
    "SOURCE_EXPIRED", "SOURCE_WITHDRAWN", "SOURCE_REFERENCE_MISMATCH", "SOURCE_SCOPE_DENIED",
    "AGENT_MESSAGE_REFS_CHANGED", "AGENT_MESSAGE_REFS_DENIED", "BEHAVIOR_SOURCE_DENIED",
})


def reference_refusal_notice(code):
    return f"The peer result is saved but its original references are unavailable: {code}."


class PeerResultForward(_Model):
    kind: Literal["agent_collaboration_result_forward"] = "agent_collaboration_result_forward"
    root_id: Identifier
    job_id: Identifier
    claim_id: Identifier
    result_message_key: Identifier
    result_sha256: Digest | None
    stop_evidence_sha256: Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    request: ControlRequest


def stopped_peer_digest(job, completion):
    if (job.state != "cancelled" or not job.cancel_requested or job.binding is None or job.generation < 1
            or completion.binding != job.binding or completion.evidence_id != job.last_evidence_id
            or completion.sequence != job.last_sequence or completion.native_execution_id != job.native_execution_id
            or completion.state != "stopped" or completion.result is not None
            or not completion.entire_tree_idle or not completion.all_inflight_starts_fenced):
        raise Problem(409, "AGENT_MESSAGE_RESULT_INVALID", "the original peer's complete stop evidence is required")
    return digest(completion.model_dump(mode="json"))


def _controller_principal(route):
    """Internal queue consumer for this server-resolved work; never a supplied credential."""
    return BehaviorPrincipal(route.tenant_id, route.principal_oid, route.team_id, "agent", route.actor_id,
                             frozenset({"read", "consume", "submit"}))


def _current(root, peer, receiver, message):
    from .behavior_agent_message_refs import validate_team_refs
    from .behavior_assistant import _Session, key
    owner = peer._agent_message_owner
    origin = root.origin
    _current_parent(peer.runtime.store.read_snapshot(_parent_keys(origin)), origin)
    if peer.runtime._now() >= root.spec.deadline:
        raise Problem(403, "AGENT_MESSAGE_ROOT_EXPIRED", "the original peer execution allowance expired")
    policy = peer.runtime.work_policy.installed()
    if not policy.active or not policy.read_enabled or policy.sha256 != root.recipient_policy_sha256:
        raise Problem(403, "AGENT_MESSAGE_ROUTE_DENIED", "the original receiver policy changed")
    session_key = key("session", origin.session_id)
    session = _row(owner.runtime.store.read_snapshot((session_key,)), session_key, _Session)
    from .behavior_agent_message_api import _captured
    _captured(owner.assistant.authority, session)
    if message.command.refs:
        validate_team_refs(
            sender_authority=owner.assistant.authority,
            sender_binding=owner.runtime.work_policy.records[origin.binding_id].binding,
            sender_principal=_controller_principal(root.directory.agents[0]),
            receiver_authority=peer.assistant.authority, receiver_binding=receiver.binding,
            receiver_principal=_controller_principal(root.directory.agents[1]),
            refs=message.command.refs, allowed_refs=root.spec.allowed_refs)


def _status(job):
    return JobStatus(job_id=job.spec.job_id, work_id=job.spec.work_id, state=job.state,
                     generation=job.generation, result_forwarded=job.result_forwarded,
                     delivery_id=None, obligation_fulfilled=False)


def check_model_time(authority, session, rows):
    task = session.assignment.task.instruction
    if task is not None and task.peer_work is not None:
        from .behavior_agent_message_api import AgentCollaborationRoot
        root = _row(rows, task.peer_work.root_id, AgentCollaborationRoot)
        if authority.runtime._now() >= root.spec.deadline:
            raise Problem(429, "AGENT_MESSAGE_ROOT_EXPIRED", "the original peer model allowance has expired")


def session_keys(task):
    reference = task.peer_work
    return (reference.root_id, reference.message_key, message_state_key(reference.message_key),
            claim_key(reference.message_key))


def check_session(session, job, rows):
    reference = session.assignment.task.instruction.peer_work
    message = _row(rows, reference.message_key, MessageEnvelope)
    claim = _row(rows, claim_key(reference.message_key), _QueueClaim)
    state = _row(rows, message_state_key(reference.message_key), _MessageState)
    if (message.to_work_id != session.execution.work_id or message.payload_sha256 != reference.message_sha256
            or message.sender_agent_id != reference.sender_agent_id
            or message.recipient_agent_id != reference.recipient_agent_id
            or message.message_kind not in {"request", "handoff"}
            or claim.assignment.root_request_id != message.root_request_id
            or claim.assignment.agent_id != reference.recipient_agent_id
            or claim.assignment.work_id != session.execution.work_id
            or claim.assignment.attempt_id != claim.claim_id
            or claim.epoch != session.execution.owner.epoch
            or state.claim_id != claim.claim_id or state.status not in {"claimed", "unknown"}
            or job.spec.obligation_id is not None or job.spec.parent_job_id is not None):
        raise Problem(403, "AGENT_MESSAGE_EXECUTION_FENCED", "the original peer claim and independent work are required")


def check_start(control, receiver):
    reference = receiver.profile.instruction.peer_work
    peer = control.runtime.agent_message_composition
    root = _read_root(control.store, reference.root_id)
    ledger = AgentMessageLedger(control.store, root.directory, clock=control.runtime._now)
    message = ledger.read(_controller_principal(root.directory.agents[1]), reference.message_key)
    claim, _ = ledger._verified(claim_key(reference.message_key), _QueueClaim)
    state, _ = ledger._verified(message_state_key(reference.message_key), _MessageState)
    if (claim.consumer_id != peer.config.controller_id or claim.epoch != receiver.binding.owner.epoch
            or claim.assignment.work_id != receiver.binding.work.work_id
            or claim.assignment.attempt_id != receiver.binding.attempt_id
            or state.claim_id != claim.claim_id or state.status not in {"claimed", "unknown"}):
        raise Problem(403, "AGENT_MESSAGE_CLAIM_DENIED", "only this receiver's original queue claim may start work")
    _current(root, peer, receiver, message)


def _result_current(root, peer, receiver, refs):
    from .behavior_agent_message_refs import validate_team_refs
    owner = peer._agent_message_owner
    _current_parent(peer.runtime.store.read_snapshot(_parent_keys(root.origin)), root.origin, executing=False)
    sender = _controller_principal(root.directory.agents[1])
    recipient = _controller_principal(root.directory.agents[0])
    peer.runtime.authorize(receiver.binding.binding_id, sender, "read")
    original, _, _ = owner.runtime.authorize(root.origin.binding_id, recipient, "read")
    if refs:
        validate_team_refs(sender_authority=peer.assistant.authority, sender_binding=receiver.binding,
            sender_principal=sender, receiver_authority=owner.assistant.authority, receiver_binding=original,
            receiver_principal=recipient, refs=refs, allowed_refs=root.spec.allowed_refs)


def forward_result(peer, receiver, root, ledger, message, job):
    from .behavior_assistant import key
    from .behavior_instruction import SavedReply, failure_notice, unpack_reply
    from .behavior_delivery import VerifiedWorkResult, _material
    store = peer.runtime.store
    binding = receiver.binding
    completion_key = worker_key("completion", job.spec.job_id, str(job.generation))
    result_key = key("result", binding.work.work_id)
    reply_key = key("reply", binding.job_id)
    rows = dict(store.read_snapshot((completion_key, result_key, reply_key)))
    stop_digest = None
    no_sdk = job.state == "cancelled" and job.generation == 0 and job.binding is None and job.cancel_requested
    if no_sdk:
        receipt = peer.runtime.dep.workers.read_cancellation_receipt(job.spec.job_id, job.owner, 0)
        if (receipt.payload["result"].get("executor_stopped") is not True
                or any(rows[key] is not None for key in (completion_key, result_key, reply_key))):
            raise Problem(409, "AGENT_MESSAGE_RESULT_INVALID", "the original pre-SDK cancellation receipt is required")
        completion, result = None, None
    else:
        completion = _row(rows, completion_key, ExecutorEvidence)
        if (completion.binding != job.binding or completion.evidence_id != job.last_evidence_id
                or not completion.entire_tree_idle or not completion.all_inflight_starts_fenced):
            raise Problem(409, "AGENT_MESSAGE_RESULT_INVALID", "the original peer completion and saved result must agree")
        if completion.state == "stopped" and completion.result is None:
            if (rows[result_key] is not None or rows[reply_key] is not None
                    or rows[completion_key].payload["material"] != _material(
                        completion.model_dump(mode="json")).model_dump(mode="json")):
                raise Problem(409, "AGENT_MESSAGE_RESULT_INVALID", "an intact terminal stop without a saved result is required")
            result = None
            stop_digest = stopped_peer_digest(job, completion)
        else:
            if rows[result_key] is None and completion.result is not None:
                result_key = worker_key("work-result", completion.result.evidence_id)
                rows.update(store.read_snapshot((result_key,)))
            result = _row(rows, result_key, VerifiedWorkResult)
            if completion.result != result or result.work_id != binding.work.work_id:
                raise Problem(409, "AGENT_MESSAGE_RESULT_INVALID", "the original peer completion and saved result must agree")
    saved = None if rows[reply_key] is None else _row(rows, reply_key, SavedReply)
    reply = None if saved is None else unpack_reply(saved)
    if result is not None and result.state == "succeeded" and reply is None:
        raise Problem(409, "AGENT_MESSAGE_RESULT_INVALID", "a successful peer result requires its complete saved SDK reply")
    text = (STOPPED_PEER_NOTICE if stop_digest is not None else
            "The peer work was cancelled before SDK execution." if no_sdk else
            f"The peer work ended as {job.state}; no successful result was produced."
            if reply is None else reply.text)
    if result is not None and reply is None and result.content == failure_notice("failed", "model_call_limit_reached"):
        text = "The peer reached MODEL_CALL_LIMIT_REACHED and stopped; no successful peer result was produced."
    principal = _controller_principal(root.directory.agents[1])
    reference = receiver.profile.instruction.peer_work
    claim, _ = ledger._verified(claim_key(reference.message_key), _QueueClaim)
    command = AgentMessage(
        message_id=control_key("agent-message-result", binding.job_id), recipient_agent_id=root.spec.origin_agent_id,
        kind="result" if result is not None and result.state == "succeeded" else "blocked", content=text,
        in_reply_to=reference.message_key, refs=() if no_sdk or reply is None else message.command.refs)
    if len(canonical(command.model_dump(mode="json"))) > MAX_AGENT_MESSAGE_BYTES:
        command = command.model_copy(update={"kind": "blocked", "content": SAVED_RESULT_LIMIT_NOTICE, "refs": ()})
    response_key = message_key(root.spec.root_request_id, reference.recipient_agent_id, command.message_id)
    if store.read_snapshot((response_key,))[response_key] is not None:
        response = ledger.read(principal, response_key)
        if (response.message.from_work_id != binding.work.work_id
                or response.message.to_work_id != root.spec.origin_work_id
                or response.command.in_reply_to != reference.message_key):
            raise Problem(409, "AGENT_MESSAGE_RESULT_CHANGED", "the retained response belongs to another original work")
    else:
        current = lambda: _result_current(root, peer, receiver, command.refs)
        try:
            current()
            response = store.validated_writes(lambda: ledger.send(principal, claim.assignment, command), current)
        except Problem as error:
            code = error.body["error"]["code"]
            if code not in REFERENCE_REFUSALS or not command.refs:
                raise
            if store.read_snapshot((response_key,))[response_key] is not None:
                response = ledger.read(principal, response_key)
            else:
                command = command.model_copy(update={"kind": "blocked", "content": reference_refusal_notice(code), "refs": ()})
                current()
                response = store.validated_writes(lambda: ledger.send(principal, claim.assignment, command), current)
    ledger.acknowledge(principal, claim.claim_id, consumer_id=claim.consumer_id, epoch=claim.epoch)
    job_key, pool_key = worker_key("job", binding.job_id), peer.runtime.dep.workers.pool_key(binding.job_id)
    forward_key = control_key("agent-message-result-forward", binding.job_id)
    result_digest = None if result is None else digest(result.model_dump(mode="json"))
    request = ControlRequest(request_id=forward_key, request_digest=digest([
        root.request.request_id, binding.job_id, claim.claim_id, response.message.message_key,
        result_digest, *((stop_digest,) if stop_digest is not None else ())]),
        read_set=(job_key, pool_key, completion_key, result_key, forward_key,
                  response.message.message_key, message_state_key(reference.message_key)))
    forwarded = PeerResultForward(root_id=root.request.request_id, job_id=binding.job_id, claim_id=claim.claim_id,
        result_message_key=response.message.message_key, result_sha256=result_digest,
        stop_evidence_sha256=stop_digest, request=request)
    def finish(rows):
        actual = _row(rows, job_key, JobRecord)
        pool = _row(rows, pool_key, _Pool)
        accepted = _row(rows, response.message.message_key, MessageEnvelope)
        consumed = _row(rows, message_state_key(reference.message_key), _MessageState)
        if (actual != job or actual.result_forwarded or binding.job_id in (*pool.pending, *pool.completions)
                or any(slot.binding is not None for slot in pool.slots)
                or (None if rows[completion_key] is None else _row(rows, completion_key, ExecutorEvidence)) != completion
                or (None if rows[result_key] is None else _row(rows, result_key, VerifiedWorkResult)) != result
                or accepted != response.message or consumed.status != "completed" or consumed.claim_id != claim.claim_id):
            raise Problem(409, "AGENT_MESSAGE_RESULT_CHANGED", "the original saved peer result or receipt changed")
        return ControlChanges(writes=(
            peer.runtime.dep.workers._write(job_key, _updated(actual, result_forwarded=True), rows),
            _write(forward_key, forwarded, "create", material(forwarded.model_dump(mode="json"))),
        ), result={"job_id": binding.job_id, "result_message_key": response.message.message_key,
                   "human_obligation_fulfilled": False})
    store.transact(request, finish)


def _stopped_reason(peer, root):
    key = worker_key("job", root.origin.execution.job_id)
    job = _row(peer.runtime.store.read_snapshot((key,)), key, JobRecord)
    if job.cancel_requested or job.state in {"succeeded", "failed", "cancelled"}:
        return "original_sender_stopped_before_dispatch"
    if peer.runtime._now() >= root.spec.deadline:
        return "original_allowance_expired_before_dispatch"
    return None


def _retire_unstarted(peer, receiver, root, ledger, principal, reason):
    reference = receiver.profile.instruction.peer_work
    store = peer.runtime.store
    job_key = worker_key("job", receiver.binding.job_id)
    def current():
        _current_parent(store.read_snapshot(_parent_keys(root.origin)), root.origin, executing=False)
        if store.read_snapshot((job_key,))[job_key] is not None or _stopped_reason(peer, root) is None:
            raise Problem(412, "BEHAVIOR_CONFLICT", "the original unstarted receiver changed before retirement")
    current()
    claim = store.validated_writes(lambda: ledger.claim(principal, reference.message_key,
        request_id=control_key("agent-message-consumer", reference.message_key),
        consumer_id=peer.config.controller_id, epoch=receiver.binding.owner.epoch), current)
    store.validated_writes(lambda: ledger.acknowledge(principal, claim.claim_id,
        consumer_id=peer.config.controller_id, epoch=receiver.binding.owner.epoch, reason=reason), current)


def _has_assistant_claim(peer, job):
    from .behavior_assistant import _Session, key
    if job.binding is None:
        return False
    session_key = key("session", key("session-id", job.binding.start_request_id))
    rows = peer.runtime.store.read_snapshot((session_key,))
    if rows[session_key] is None:
        return False
    session = _row(rows, session_key, _Session)
    if session.execution != job.binding:
        raise Problem(403, "AGENT_MESSAGE_EXECUTION_FENCED", "the peer claim belongs to another execution")
    return True


def step_peer(peer, receiver, root, ledger):
    lock=peer._agent_message_consumer_lock
    if not lock.acquire(blocking=False):
        return None
    try:
        try:
            return _step_peer(peer,receiver,root,ledger)
        except Problem as error:
            if error.body["error"]["code"] in {"WORKER_START_UNKNOWN", "WORKER_QUERY_UNKNOWN", "WORKER_STOP_UNKNOWN"}:
                reference=receiver.profile.instruction.peer_work
                ledger.mark_unknown(_controller_principal(root.directory.agents[1]), claim_key(reference.message_key),
                    consumer_id=peer.config.controller_id, epoch=receiver.binding.owner.epoch,
                    request_id=control_key("agent-message-dispatch-unknown", reference.message_key))
            raise
    finally:
        lock.release()


def _step_peer(peer, receiver, root, ledger):
    reference = receiver.profile.instruction.peer_work
    principal = _controller_principal(root.directory.agents[1])
    message = ledger.read(principal, reference.message_key)
    if (message.message.to_work_id != receiver.binding.work.work_id
            or message.message.payload_sha256 != reference.message_sha256):
        raise Problem(403, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "the queue item differs from its registered receiver work")
    workers, store = peer.runtime.dep.workers, peer.runtime.store
    job_key = worker_key("job", receiver.binding.job_id)
    row = store.read_snapshot((job_key,))[job_key]
    if row is None:
        pending = ledger.pending(principal)
        if not pending or pending[0] != reference.message_key:
            return None
        reason = _stopped_reason(peer, root)
        if reason is not None:
            _retire_unstarted(peer, receiver, root, ledger, principal, reason)
            return None
        if _receiver_busy(peer, receiver, for_dispatch=True):
            return None
        current = lambda: _current(root, peer, receiver, message)
        current()
        claim = store.validated_writes(lambda: ledger.claim(
            principal, reference.message_key, request_id=control_key("agent-message-consumer", reference.message_key),
            consumer_id=peer.config.controller_id, epoch=receiver.binding.owner.epoch), current)
        if (claim.assignment.agent_id != reference.recipient_agent_id
                or claim.assignment.work_id != receiver.binding.work.work_id
                or claim.assignment.attempt_id != receiver.binding.attempt_id):
            raise Problem(403, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "the original queue claim differs from the receiver work")
        spec = JobSpec(job_id=receiver.binding.job_id, work_id=receiver.binding.work.work_id,
                       input=peer.runtime.job_input(receiver.binding))
        store.validated_writes(lambda: workers.enqueue(spec, receiver.binding.owner), current)
        row = store.read_snapshot((job_key,))[job_key]
    job = _row({job_key: row}, job_key, JobRecord)
    if (job.state not in {"succeeded", "failed", "cancelled"} and not job.cancel_requested
            and _stopped_reason(peer, root) is not None):
        _current_parent(store.read_snapshot(_parent_keys(root.origin)), root.origin, executing=False)
        workers.cancel(job.spec.job_id, job.owner, expected_binding=job.binding)
        job = workers.inspect(job.spec.job_id)
    if job.state in {"queued", "waiting_dependency"}:
        workers.start(job.spec.job_id, job.owner)
    elif job.state in {"succeeded", "failed", "cancelled"} and not job.result_forwarded:
        forward_result(peer, receiver, root, ledger, message, job)
    elif job.state not in {"succeeded", "failed", "cancelled"} and job.cancel_requested and job.stop_credit:
        workers.stop(job.spec.job_id, job.owner)
    elif (job.state not in {"succeeded", "failed", "cancelled"}
          and (job.cancel_requested or _has_assistant_claim(peer, job))) and not (
            not job.cancel_requested and peer.runtime.dep.native_launches.instruction_recently_active(job)):
        workers.reconcile(job.spec.job_id, job.owner)
    observed = workers.inspect(receiver.binding.job_id)
    if observed.state == "capacity_uncertain":
        ledger.mark_unknown(principal, claim_key(reference.message_key), consumer_id=peer.config.controller_id,
            epoch=receiver.binding.owner.epoch,
            request_id=control_key("agent-message-execution-unknown", observed.spec.job_id, str(observed.generation)))
    return _status(observed)


def _advance_queue(peer, ledger):
    owner = peer._agent_message_owner
    for _ in range(32):
        principal = _controller_principal(ledger.directory.agents[1])
        pending = ledger.pending(principal)
        if not pending:
            return
        message, _ = ledger._verified(pending[0], MessageEnvelope)
        original = next((record for record in owner.runtime.work_policy.records.values()
                         if record.admission_id == message.root_request_id), None)
        if original is None:
            raise Problem(503, "AGENT_MESSAGE_UNKNOWN", "the pending peer message lost its original A work")
        root = _read_root(peer.runtime.store, collaboration_key(original.binding.binding_id))
        ledger = AgentMessageLedger(peer.runtime.store, root.directory, clock=peer.runtime._now)
        receiver = _receiver_record(root, ledger, peer, identifier=pending[0])
        if receiver is None:
            raise Problem(503, "AGENT_MESSAGE_UNKNOWN", "the accepted message lost its original receiver registration")
        step_peer(peer, receiver, root, ledger)
        after = ledger.pending(principal)
        if after and after[0] == pending[0]:
            return


def advance(runtime, binding):
    profile = getattr(runtime, "profiles", {}).get(binding.binding_id)
    task = None if profile is None else profile.instruction
    if task is None:
        return None
    composition = runtime.agent_message_composition
    if task.peer_work is not None:
        root = _read_root(runtime.store, task.peer_work.root_id)
        ledger = AgentMessageLedger(runtime.store, root.directory, clock=runtime._now)
        result = step_peer(composition, runtime.work_policy.records[binding.binding_id], root, ledger)
        if result is not None:
            return result
        key = worker_key("job", binding.job_id)
        rows = runtime.store.read_snapshot((key,))
        if rows[key] is None:
            raise Problem(409, "AGENT_MESSAGE_QUEUED", "the original peer message is queued, not dispatched")
        return _status(_row(rows, key, JobRecord))
    if task.capabilities is None or not any(grant.entry_id == "fa-agent-collaboration-v1"
                                         for grant in task.capabilities.grants):
        return None
    try:
        key = collaboration_key(binding.binding_id)
        if runtime.store.read_snapshot((key,))[key] is None:
            return None
        root = _read_root(runtime.store, key)
        if runtime.store.read_snapshot((root_key(root.spec.root_request_id),))[root_key(root.spec.root_request_id)] is None:
            return None
        ledger = AgentMessageLedger(runtime.store, root.directory, clock=runtime._now)
        state, _ = ledger._verified(root_state_key(root.spec.root_request_id), _RootState)
        if state.handoff_message_id is None:
            return None
        peer = composition.for_agent(root.directory.agents[1].agent_id)
        _advance_queue(peer, ledger)
    except Problem as error:
        logging.getLogger(__name__).warning("AGENT_MESSAGE_ADVANCE_UNRESOLVED code=%s original_work=true",
                                            error.body["error"]["code"])
    return None


def resume_peer_messages(composition):
    from .behavior_agent_messages import _ReceiverQueue, queue_key
    runtime = composition.runtime
    controls = runtime.work_policy.records
    origins = {record.admission_id: record for record in controls.values()
               if record.profile.instruction is not None and record.profile.instruction.peer_work is None}
    keys = tuple(queue_key(target.agent_id) for target in composition.config.targets[:2])
    queues = runtime.store.read_snapshot(keys)
    for key in keys:
        if queues[key] is None:
            continue
        queue = _row(queues, key, _ReceiverQueue)
        rows = runtime.store.read_snapshot(queue.pending)
        for identifier in queue.pending:
            message = _row(rows, identifier, MessageEnvelope)
            original = origins.get(message.root_request_id)
            if original is None:
                raise Problem(503, "AGENT_MESSAGE_UNKNOWN", "the pending message lost its original A work")
            root = _read_root(runtime.store, collaboration_key(original.binding.binding_id))
            ledger = AgentMessageLedger(runtime.store, root.directory, clock=runtime._now)
            verified = ledger.read(_controller_principal(root.directory.resolve(queue.agent_id)), identifier)
            if verified.message != message:
                raise Problem(409, "AGENT_MESSAGE_RECORD_INVALID", "the original queued message changed during restart")
            composition.launch_instruction(original.binding.binding_id)


def complete_origin(runtime, binding):
    """Consume internal results only after their original A work has forwarded its own final."""
    profile = getattr(runtime, "profiles", {}).get(binding.binding_id)
    task = None if profile is None else profile.instruction
    if (task is None or task.peer_work is not None or task.capabilities is None
            or not any(grant.entry_id == "fa-agent-collaboration-v1" for grant in task.capabilities.grants)):
        return
    key = collaboration_key(binding.binding_id)
    if runtime.store.read_snapshot((key,))[key] is None:
        return
    root = _read_root(runtime.store, key)
    ledger = AgentMessageLedger(runtime.store, root.directory, clock=runtime._now)
    principal = _controller_principal(root.directory.agents[0])
    for _ in range(32):
        pending = ledger.pending(principal)
        if not pending:
            return
        message, _ = ledger._verified(pending[0], MessageEnvelope)
        record = next((record for record in runtime.work_policy.records.values()
                       if record.admission_id == message.root_request_id), None)
        if record is None:
            raise Problem(503, "AGENT_MESSAGE_UNKNOWN", "the result queue lost its original A work")
        job_key = worker_key("job", record.binding.job_id)
        job = _row(runtime.store.read_snapshot((job_key,)), job_key, JobRecord)
        if job.state not in {"succeeded", "failed", "cancelled"} or not job.result_forwarded:
            return
        root = _read_root(runtime.store, collaboration_key(record.binding.binding_id))
        ledger = AgentMessageLedger(runtime.store, root.directory, clock=runtime._now)
        principal = _controller_principal(root.directory.agents[0])
        runtime.authorize(record.binding.binding_id, principal, "read")
        if (message.message_kind not in {"result", "blocked"} or message.to_work_id != record.binding.work.work_id
                or message.recipient_agent_id != root.spec.origin_agent_id):
            raise Problem(403, "AGENT_MESSAGE_REPLY_DENIED", "the completed A work may consume only its own peer result")
        claimed = ledger.claim(principal, pending[0],
            request_id=control_key("agent-message-result-consumer", pending[0]),
            consumer_id=runtime.controller.controller_id, epoch=record.binding.owner.epoch)
        ledger.acknowledge(principal, claimed.claim_id,
                           consumer_id=runtime.controller.controller_id, epoch=record.binding.owner.epoch)
