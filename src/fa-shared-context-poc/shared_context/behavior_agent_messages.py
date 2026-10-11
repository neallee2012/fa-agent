"""Contract only: peer routes, durable messages and wake decisions.

No endpoint, capability, startup classification or SDK is registered here.
The trusted caller must supply server-resolved assignments and approved root
scope. A persisted receipt or queue claim is not authority to launch a model,
send externally, or fulfill the original human delivery obligation.

The root stores only a reference to the authority's existing CostLedger; this
contract does not create or debit money. Team references are pinned inputs, not
fresh ACL evidence. JWT/ticket verification, live assignment checks, current
share/read permission, token/time spending and SDK dispatch remain integration
requirements. Queue ``completed`` means consumed, never human-delivered.
"""

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .behavior_auth import BehaviorPrincipal
from .behavior_agent_message_wire import AgentMessage, MessageKind, TeamReference, WakeDecision
from .behavior_operations import _Model, _checked, _parse, _row, _updated, _utc, _write, control_key
from .behavior_runtime import material
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest, Digest, InlineMaterial, MAX_INLINE_BYTES
from .errors import Problem
from .models import Identifier
from .source_materials import canonical, digest


class AgentRoute(_Model):
    agent_id: Identifier
    tenant_id: Identifier
    team_id: Identifier
    principal_oid: Identifier
    actor_id: Identifier
    policy_id: Identifier
    template_id: Identifier
    template_sha256: Digest
    target_id: Identifier
    conversation_id: Identifier
    hosted_session_id: Identifier
    sdk_session_id: Identifier
    memory_scope_id: Identifier
    cwd_scope_id: Identifier
    credential_binding_id: Identifier


class AgentDirectory(_Model):
    agents: Annotated[tuple[AgentRoute, ...], Field(min_length=2, max_length=2)]

    @model_validator(mode="after")
    def separate_peers(self):
        first, second = self.agents
        if (first.tenant_id, first.team_id) != (second.tenant_id, second.team_id):
            raise ValueError("the two peers must belong to the same tenant and Team")
        private = ("agent_id", "principal_oid", "actor_id", "policy_id", "template_id", "target_id",
                   "conversation_id", "hosted_session_id", "sdk_session_id", "memory_scope_id",
                   "cwd_scope_id", "credential_binding_id")
        if any(getattr(first, name) == getattr(second, name) for name in private):
            raise ValueError("peer identity, route and private scopes must remain separate")
        return self

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))

    def resolve(self, agent_id):
        route = next((route for route in self.agents if route.agent_id == agent_id), None)
        if route is None:
            raise Problem(403, "AGENT_MESSAGE_ROUTE_DENIED", "the recipient is not in the server directory")
        return route

    def authenticate(self, principal, action):
        if not isinstance(principal, BehaviorPrincipal) or principal.kind != "agent":
            raise Problem(403, "AGENT_MESSAGE_IDENTITY_DENIED", "a server-verified agent principal is required")
        route = next((route for route in self.agents if route.principal_oid == principal.oid), None)
        if route is None or (route.tenant_id, route.team_id, route.actor_id) != (
                principal.tenant_id, principal.team, principal.actor_id):
            raise Problem(403, "AGENT_MESSAGE_IDENTITY_DENIED", "credential and configured agent identity differ")
        principal.require(route.team_id, action)
        return route


class AgentAssignment(_Model):
    root_request_id: Identifier
    agent_id: Identifier
    work_id: Identifier
    attempt_id: Identifier


class RootSpec(_Model):
    root_request_id: Identifier
    origin_agent_id: Identifier
    origin_work_id: Identifier
    origin_attempt_id: Identifier
    root_budget_id: Identifier
    human_obligation_id: Identifier
    deadline: datetime
    max_messages: Annotated[int, Field(ge=2, le=32)]
    max_hops: Annotated[int, Field(ge=2, le=32)]
    allowed_refs: Annotated[tuple[TeamReference, ...], Field(max_length=8)] = ()
    _time = field_validator("deadline")(_utc)


class RootEnvelope(_Model):
    kind: Literal["agent_message_contract_root"] = "agent_message_contract_root"
    spec: RootSpec
    authority_binding: Digest
    directory_sha256: Digest
    cost_ledger_id: Identifier
    request: ControlRequest


def decide_wake(kind: MessageKind, *, receiver_busy=False, duplicate=False, allowance_available=True):
    """Classify already authenticated/correlated facts; never execute their wake."""
    if kind not in {"request", "handoff", "result", "blocked", "fyi", "receipt"}:
        raise Problem(422, "AGENT_MESSAGE_KIND_INVALID", "one supported peer message kind is required")
    if any(type(value) is not bool for value in (receiver_busy, duplicate, allowance_available)):
        raise Problem(422, "AGENT_MESSAGE_WAKE_INVALID", "trusted wake facts must be explicit booleans")
    if duplicate:
        return WakeDecision(action="silence", reason="original_message_already_recorded")
    if kind in {"fyi", "receipt"}:
        return WakeDecision(action="silence", reason="no_new_work" if kind == "fyi" else "transport_receipt_only")
    if not allowance_available:
        return WakeDecision(action="needs_attention", reason="root_allowance_unavailable")
    if receiver_busy:
        return WakeDecision(action="queued", reason="original_receiver_busy")
    return WakeDecision(action="wake", reason="original_dependency_blocked" if kind == "blocked" else "new_work_or_result")


class _RootState(_Model):
    kind: Literal["agent_message_contract_root_state"] = "agent_message_contract_root_state"
    root_request_id: Identifier
    message_count: Annotated[int, Field(ge=0)] = 0
    receipt_count: Annotated[int, Field(ge=0)] = 0
    handoff_message_id: Identifier | None = None
    response_message_id: Identifier | None = None
    message_keys: Annotated[tuple[Identifier, ...], Field(max_length=64)] = Field(
        default=(), exclude_if=lambda value: not value)
    request: ControlRequest


class MessageEnvelope(_Model):
    kind: Literal["agent_message_contract_envelope"] = "agent_message_contract_envelope"
    message_key: Identifier
    message_id: Identifier
    message_kind: MessageKind
    source_kind: Literal["agent"] = "agent"
    authorization: Literal[False] = False
    root_request_id: Identifier
    root_budget_id: Identifier
    sender_agent_id: Identifier
    sender_oid: Identifier
    recipient_agent_id: Identifier
    from_work_id: Identifier
    from_attempt_id: Identifier
    to_work_id: Identifier
    hop: Annotated[int, Field(ge=1)]
    causation_id: Identifier
    payload_sha256: Digest
    request: ControlRequest


class _MessageState(_Model):
    kind: Literal["agent_message_contract_state"] = "agent_message_contract_state"
    message_key: Identifier
    status: Literal["queued", "silenced", "needs_attention", "claimed", "completed", "unknown"]
    decision: WakeDecision
    claim_id: Identifier | None = None
    request: ControlRequest


class _ReceiverQueue(_Model):
    kind: Literal["agent_message_contract_queue"] = "agent_message_contract_queue"
    agent_id: Identifier
    pending: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()
    request: ControlRequest


class MessageView(_Model):
    message: MessageEnvelope
    command: AgentMessage
    status: Literal["queued", "silenced", "needs_attention", "claimed", "completed", "unknown"]
    decision: WakeDecision


@dataclass(frozen=True)
class MessageSubmission:
    view: MessageView
    fresh: bool


class _QueueClaim(_Model):
    kind: Literal["agent_message_contract_claim"] = "agent_message_contract_claim"
    claim_id: Identifier
    message_key: Identifier
    receiver_agent_id: Identifier
    consumer_id: Identifier
    epoch: Annotated[int, Field(ge=1)]
    assignment: AgentAssignment
    request: ControlRequest


@dataclass(frozen=True)
class QueueClaim:
    fresh: bool
    claim_id: str
    assignment: AgentAssignment
    decision: WakeDecision
    model_dispatch_authorized: Literal[False] = False


def root_key(root_request_id):
    return control_key("agent-message-contract-root", root_request_id)


def root_state_key(root_request_id):
    return control_key("agent-message-contract-root-state", root_request_id)


def message_key(root_request_id, sender, message_id):
    return control_key("agent-message-contract-message", root_request_id, sender, message_id)


def message_state_key(identifier):
    return control_key("agent-message-contract-state", identifier)


def queue_key(agent_id):
    return control_key("agent-message-contract-queue", agent_id)


def claim_key(identifier):
    return control_key("agent-message-contract-claim", identifier)


def _require_allowance(root, counter, command, hop, now):
    closing, receipt_only = command.kind in {"result", "blocked"}, command.kind == "receipt"
    if not (closing or receipt_only) and now >= root.spec.deadline:
        raise Problem(403, "AGENT_MESSAGE_ROOT_EXPIRED", "original root deadline has expired")
    held_reply = counter.handoff_message_id is not None and counter.response_message_id is None
    reserve_reply = command.kind in {"request", "handoff"} or held_reply and not closing
    if (not receipt_only and counter.message_count + 1 + int(reserve_reply) > root.spec.max_messages
            or hop > root.spec.max_hops):
        raise Problem(429, "AGENT_MESSAGE_ALLOWANCE", "the original root message allowance is exhausted")
    if command.kind in {"request", "handoff"} and counter.handoff_message_id is not None:
        raise Problem(409, "AGENT_MESSAGE_HANDOFF_EXISTS", "one original A-to-B work is already retained")


class AgentMessageLedger:
    def __init__(self, store: BehaviorStore, directory: AgentDirectory, *, clock):
        self.store, self.directory, self.clock = store, _checked(directory, AgentDirectory), clock

    def _verified(self, key, model):
        from .behavior_runtime_transition import _payload_hash
        row = self.store.read_snapshot((key,))[key]
        if row is None:
            raise Problem(503, "AGENT_MESSAGE_UNKNOWN", "only the original message receipt may be reconciled")
        value = _parse(row.payload["data"], model)
        receipt = self.store.reconcile(value.request)
        if dict(receipt.payload["writes"]).get(key) != _payload_hash(row.payload):
            raise Problem(409, "AGENT_MESSAGE_RECORD_INVALID", "the exact original write receipt is required")
        return value, row

    def register_root(self, spec: RootSpec):
        """Record a server-approved root contract, not a new budget or approval."""
        from .behavior_work_policy import CostLedger, work_policy_key
        spec = _checked(spec, RootSpec)
        origin = self.directory.resolve(spec.origin_agent_id)
        if any((ref.tenant_id, ref.team_id) != (origin.tenant_id, origin.team_id) for ref in spec.allowed_refs):
            raise Problem(403, "AGENT_MESSAGE_SHARE_DENIED", "a root contract cannot approve another Team's sources")
        _utc(spec.deadline)
        if _utc(self.clock()) >= spec.deadline:
            raise Problem(403, "AGENT_MESSAGE_ROOT_EXPIRED", "a new root contract needs its current allowance")
        key = root_key(spec.root_request_id)
        state_id = root_state_key(spec.root_request_id)
        cost_id = work_policy_key("cost", self.store._scope.authority_domain)
        request = ControlRequest(request_id=key,
            request_digest=digest([spec.model_dump(mode="json"), self.directory.sha256]),
            read_set=(key, state_id, cost_id))
        root = RootEnvelope(spec=spec, authority_binding=self.store._binding,
                            directory_sha256=self.directory.sha256, cost_ledger_id=cost_id, request=request)

        def decide(rows):
            cost = _row(rows, cost_id, CostLedger)
            if cost.authority_domain != self.store._scope.authority_domain:
                raise Problem(403, "AGENT_MESSAGE_ROOT_DENIED", "the existing single-authority ledger is required")
            if rows[key] is not None or rows[state_id] is not None:
                raise Problem(409, "AGENT_MESSAGE_ROOT_EXISTS", "retain the original root instead of replacing its allowance")
            return ControlChanges(writes=(
                _write(key, root, "create", material(root.model_dump(mode="json"))),
                _write(state_id, _RootState(root_request_id=spec.root_request_id, request=request), "create"),
            ), result={"root_request_id": spec.root_request_id, "cost_ledger_id": cost_id, "new_budget": False})
        self.store.transact(request, decide)
        return self._root(spec.root_request_id)

    def _root(self, root_request_id):
        root, _ = self._verified(root_key(root_request_id), RootEnvelope)
        if root.authority_binding != self.store._binding or root.directory_sha256 != self.directory.sha256:
            raise Problem(409, "AGENT_MESSAGE_ROOT_DENIED", "original routes and the single control authority must match")
        return root

    def check_handoff(self, principal, assignment: AgentAssignment, command: AgentMessage):
        """Pure preflight before creating the receiver; send still rechecks its original transaction."""
        assignment, command = _checked(assignment, AgentAssignment), _checked(command, AgentMessage)
        sender = self.directory.authenticate(principal, "submit")
        receiver = self.directory.resolve(command.recipient_agent_id)
        root = self._root(assignment.root_request_id)
        if (command.kind not in {"request", "handoff"} or command.in_reply_to is not None
                or (assignment.agent_id, assignment.work_id, assignment.attempt_id) != (
                    root.spec.origin_agent_id, root.spec.origin_work_id, root.spec.origin_attempt_id)
                or sender.agent_id != assignment.agent_id or receiver.agent_id == sender.agent_id):
            raise Problem(403, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "only the original root may reserve its peer work")
        identifier = message_key(assignment.root_request_id, sender.agent_id, command.message_id)
        if self.store.read_snapshot((identifier,))[identifier] is not None:
            prior = self.read(principal, identifier)
            if (prior.command != command or prior.message.from_work_id != assignment.work_id
                    or prior.message.from_attempt_id != assignment.attempt_id):
                raise Problem(409, "AGENT_MESSAGE_ORIGINAL_REQUIRED", "the original message has different input")
            return
        counter, _ = self._verified(root_state_key(assignment.root_request_id), _RootState)
        _require_allowance(root, counter, command, 1, _utc(self.clock()))
        queue_id = queue_key(receiver.agent_id)
        if self.store.read_snapshot((queue_id,))[queue_id] is not None:
            queue, _ = self._verified(queue_id, _ReceiverQueue)
            if len(queue.pending) >= 32:
                raise Problem(429, "AGENT_MESSAGE_QUEUE_FULL", "the original receiver queue is full")

    def send(self, principal, assignment: AgentAssignment, command: AgentMessage, *, receiver_busy=False):
        return self.send_recorded(principal, assignment, command, receiver_busy=receiver_busy).view

    def send_recorded(self, principal, assignment: AgentAssignment, command: AgentMessage, *, receiver_busy=False):
        assignment = _checked(assignment, AgentAssignment)
        command = _checked(command, AgentMessage)
        sender = self.directory.authenticate(principal, "submit")
        receiver = self.directory.resolve(command.recipient_agent_id)
        root = self._root(assignment.root_request_id)
        if any(ref not in root.spec.allowed_refs or (ref.tenant_id, ref.team_id) != (
                sender.tenant_id, sender.team_id) for ref in command.refs):
            raise Problem(403, "AGENT_MESSAGE_SHARE_DENIED", "only the root's exact approved Team references may be carried")
        if sender.agent_id == receiver.agent_id or sender.agent_id != assignment.agent_id:
            raise Problem(403, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "sender identity must come from its original assignment")
        origin = (assignment.agent_id, assignment.work_id, assignment.attempt_id) == (
            root.spec.origin_agent_id, root.spec.origin_work_id, root.spec.origin_attempt_id)
        previous = None if command.in_reply_to is None else self.read(principal, command.in_reply_to)
        closing = command.kind in {"result", "blocked"}
        reply_claim_id = None
        if previous is not None:
            prior = previous.message
            if (prior.root_request_id != assignment.root_request_id or prior.recipient_agent_id != sender.agent_id
                    or prior.sender_agent_id != receiver.agent_id or prior.to_work_id != assignment.work_id
                    or command.kind not in {"result", "blocked", "receipt", "fyi"}
                    or command.kind == "receipt" and prior.message_kind == "receipt"):
                raise Problem(403, "AGENT_MESSAGE_REPLY_DENIED", "reply must match the original root, peers and work")
            if not origin:
                reply_claim_id = claim_key(prior.message_key)
                claim, _ = self._verified(reply_claim_id, _QueueClaim)
                if claim.assignment != assignment:
                    raise Problem(403, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "only the original claimed peer work may reply")
            if closing and (prior.message_kind not in {"request", "handoff"} or origin):
                raise Problem(403, "AGENT_MESSAGE_REPLY_DENIED", "only the assigned peer returns the original result")
        elif not origin or command.kind not in {"request", "handoff", "fyi"}:
            raise Problem(403, "AGENT_MESSAGE_REPLY_DENIED", "a reply or receipt needs its original message")
        body = canonical(command.model_dump(mode="json"))
        if len(body) > MAX_INLINE_BYTES:
            raise Problem(413, "AGENT_MESSAGE_LIMIT", "complete message exceeds the original inline envelope")
        payload = InlineMaterial(data=body, sha256=digest(command.model_dump(mode="json")))
        receipt_only = command.kind == "receipt"
        identifier = (control_key("agent-message-contract-receipt", command.in_reply_to, sender.agent_id)
                      if receipt_only else message_key(assignment.root_request_id, sender.agent_id, command.message_id))
        state_id = message_state_key(identifier)
        root_id, counter_id = root_key(assignment.root_request_id), root_state_key(assignment.root_request_id)
        queue_id = queue_key(receiver.agent_id)
        request = ControlRequest(request_id=identifier,
            request_digest=digest([assignment.model_dump(mode="json"), command.model_dump(mode="json"),
                                   self.directory.sha256]),
            read_set=(root_id, counter_id, identifier, state_id, queue_id,
                      *((previous.message.message_key,) if previous is not None else ()),
                      *((reply_claim_id,) if reply_claim_id is not None else ())))
        decision = decide_wake(command.kind, receiver_busy=receiver_busy,
                               allowance_available=_utc(self.clock()) < root.spec.deadline)
        queued = decision.action in {"wake", "queued", "needs_attention"}
        hop = 1 if previous is None else previous.message.hop + (0 if receipt_only else 1)
        envelope = MessageEnvelope(
            message_key=identifier, message_id=command.message_id, message_kind=command.kind,
            root_request_id=assignment.root_request_id, root_budget_id=root.spec.root_budget_id,
            sender_agent_id=sender.agent_id, sender_oid=sender.principal_oid,
            recipient_agent_id=receiver.agent_id, from_work_id=assignment.work_id, from_attempt_id=assignment.attempt_id,
            to_work_id=(control_key("agent-message-contract-work", identifier, receiver.agent_id)
                        if previous is None else previous.message.from_work_id),
            hop=hop, causation_id=root.spec.root_request_id if previous is None else previous.message.message_key,
            payload_sha256=payload.sha256, request=request)
        fresh = False

        def decide(rows):
            nonlocal fresh
            current = _row(rows, root_id, RootEnvelope)
            counter = _row(rows, counter_id, _RootState)
            if current != root:
                raise Problem(403, "AGENT_MESSAGE_ROOT_EXPIRED", "original root authority or deadline changed")
            _require_allowance(root, counter, command, hop, _utc(self.clock()))
            if rows[identifier] is not None or rows[state_id] is not None:
                raise Problem(409, "AGENT_MESSAGE_ORIGINAL_REQUIRED", "read the original message and receipt")
            if closing and (previous is None or counter.handoff_message_id != previous.message.message_key
                            or counter.response_message_id is not None):
                raise Problem(409, "AGENT_MESSAGE_RESULT_EXISTS", "the original peer result cannot be replaced")
            if reply_claim_id is not None and _row(rows, reply_claim_id, _QueueClaim).assignment != assignment:
                raise Problem(409, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "original peer assignment changed")
            state = _MessageState(message_key=identifier,
                                  status="needs_attention" if decision.action == "needs_attention" else
                                  "queued" if queued else "silenced",
                                  decision=decision, request=request)
            fresh = True
            writes = [_write(identifier, envelope, "create", payload), _write(state_id, state, "create"),
                      _write(counter_id, _updated(counter, message_count=counter.message_count + int(not receipt_only),
                          receipt_count=counter.receipt_count + int(receipt_only),
                          handoff_message_id=identifier if command.kind in {"request", "handoff"} else counter.handoff_message_id,
                          response_message_id=identifier if closing else counter.response_message_id,
                          message_keys=(*counter.message_keys, identifier),
                          request=request), "replace")]
            if queued:
                prior = rows[queue_id]
                queue = (_ReceiverQueue(agent_id=receiver.agent_id, request=request)
                         if prior is None else _row(rows, queue_id, _ReceiverQueue))
                if len(queue.pending) >= 32:
                    raise Problem(429, "AGENT_MESSAGE_QUEUE_FULL", "the bounded original receiver queue is full")
                writes.append(_write(queue_id, _updated(queue, pending=(*queue.pending, identifier), request=request),
                                     "create" if prior is None else "replace"))
            return ControlChanges(writes=tuple(writes),
                                  result={"message_key": identifier, "status": state.status, "model_started": False})
        for attempt in range(3):
            fresh = False
            try:
                self.store.transact(request, decide)
                break
            except Problem as error:
                if error.status != 412 or error.body["error"]["code"] != "BEHAVIOR_CONFLICT":
                    raise
                if self.store._receipt(request) is not None:
                    fresh = False
                    break
                if attempt == 2:
                    raise
                logging.getLogger(__name__).warning("AGENT_MESSAGE_COMMIT_CONFLICT attempt=%s maximum=3", attempt + 1)
        return MessageSubmission(self.read(principal, identifier), fresh)

    def messages(self, principal, root_request_id):
        root = self._root(root_request_id)
        reader = self.directory.authenticate(principal, "read")
        self.directory.resolve(reader.agent_id)
        state, _ = self._verified(root_state_key(root.spec.root_request_id), _RootState)
        return tuple(self.read(principal, identifier) for identifier in state.message_keys)

    def read(self, principal, identifier):
        reader = self.directory.authenticate(principal, "read")
        def inspect():
            message, row = self._verified(identifier, MessageEnvelope)
            self._root(message.root_request_id)
            if reader.agent_id not in {message.sender_agent_id, message.recipient_agent_id}:
                raise Problem(403, "AGENT_MESSAGE_READ_DENIED", "only this message's original peers may read it")
            payload = _parse(row.payload["material"], InlineMaterial)
            command = AgentMessage.model_validate_json(payload.data)
            state, _ = self._verified(message_state_key(identifier), _MessageState)
            if (payload.sha256 != message.payload_sha256 or command.message_id != message.message_id
                    or command.kind != message.message_kind or command.recipient_agent_id != message.recipient_agent_id
                    or state.message_key != identifier):
                raise Problem(409, "AGENT_MESSAGE_RECORD_INVALID", "original message content or state differs")
            return MessageView(message=message, command=command, status=state.status, decision=state.decision)
        return inspect() if self.store._read_view() is not None else self.store.consistent_read(inspect)

    def pending(self, principal):
        receiver = self.directory.authenticate(principal, "consume")
        key = queue_key(receiver.agent_id)
        if self.store.read_snapshot((key,))[key] is None:
            return ()
        queue, _ = self._verified(key, _ReceiverQueue)
        if queue.agent_id != receiver.agent_id:
            raise Problem(409, "AGENT_MESSAGE_QUEUE_INVALID", "receiver queue identity differs")
        return queue.pending

    def claim(self, principal, identifier, *, request_id, consumer_id, epoch, receiver_busy=False):
        if type(receiver_busy) is not bool:
            raise Problem(422, "AGENT_MESSAGE_WAKE_INVALID", "receiver busy state must be a trusted boolean")
        receiver = self.directory.authenticate(principal, "consume")
        message = self.read(principal, identifier)
        if receiver.agent_id != message.message.recipient_agent_id:
            raise Problem(403, "AGENT_MESSAGE_CLAIM_DENIED", "only the original recipient may claim its queue item")
        root = self._root(message.message.root_request_id)
        key, state_id, queue_id = claim_key(identifier), message_state_key(identifier), queue_key(receiver.agent_id)
        request = ControlRequest(request_id=key,
            request_digest=digest([identifier, receiver.agent_id, request_id, consumer_id, epoch]),
            read_set=(identifier, state_id, queue_id, key))
        assignment = AgentAssignment(
            root_request_id=message.message.root_request_id, agent_id=receiver.agent_id,
            work_id=message.message.to_work_id,
            attempt_id=root.spec.origin_attempt_id if receiver.agent_id == root.spec.origin_agent_id else key)
        claim = _QueueClaim(claim_id=key, message_key=identifier, receiver_agent_id=receiver.agent_id,
                            consumer_id=consumer_id, epoch=epoch, assignment=assignment, request=request)
        fresh = False

        def decide(rows):
            nonlocal fresh
            state = _row(rows, state_id, _MessageState)
            queue = _row(rows, queue_id, _ReceiverQueue)
            if receiver_busy:
                raise Problem(409, "AGENT_MESSAGE_RECEIVER_BUSY", "retain the original queued work until its safe point")
            if (not queue.pending or queue.pending[0] != identifier or queue.agent_id != receiver.agent_id
                    or state.status not in {"queued", "needs_attention"} or state.claim_id is not None
                    or rows[key] is not None):
                raise Problem(409, "AGENT_MESSAGE_CLAIM_CONFLICT", "the original queue item already has an owner or is not next")
            fresh = True
            return ControlChanges(writes=(
                _write(key, claim, "create", material(claim.model_dump(mode="json"))),
                _write(state_id, _updated(state, status="claimed", claim_id=key, request=request), "replace"),
            ), result={"claim_id": key, "message_key": identifier, "model_started": False})
        self.store.transact(request, decide)
        stored, _ = self._verified(key, _QueueClaim)
        decision = decide_wake(message.message.message_kind,
                               allowance_available=_utc(self.clock()) < root.spec.deadline)
        return QueueClaim(fresh=fresh, claim_id=key, assignment=stored.assignment, decision=decision)

    def acknowledge(self, principal, identifier, *, consumer_id, epoch, reason="original_message_consumed"):
        if reason not in {"original_message_consumed", "original_sender_stopped_before_dispatch",
                          "original_allowance_expired_before_dispatch"}:
            raise Problem(422, "AGENT_MESSAGE_ACK_INVALID", "one supported original consumption reason is required")
        receiver = self.directory.authenticate(principal, "consume")
        claim, _ = self._verified(identifier, _QueueClaim)
        if (receiver.agent_id != claim.receiver_agent_id or claim.consumer_id != consumer_id or claim.epoch != epoch):
            raise Problem(403, "AGENT_MESSAGE_CLAIM_DENIED", "only the original queue owner may confirm consumption")
        state_id, queue_id = message_state_key(claim.message_key), queue_key(receiver.agent_id)
        request = ControlRequest(request_id=control_key("agent-message-contract-consumed", identifier),
            request_digest=digest([identifier, consumer_id, epoch,
                                   *(() if reason == "original_message_consumed" else (reason,))]),
            read_set=(identifier, state_id, queue_id))

        def decide(rows):
            current = _row(rows, identifier, _QueueClaim)
            state = _row(rows, state_id, _MessageState)
            queue = _row(rows, queue_id, _ReceiverQueue)
            if (current != claim or state.claim_id != identifier or state.status not in {"claimed", "unknown"}
                    or not queue.pending or queue.pending[0] != claim.message_key):
                raise Problem(409, "AGENT_MESSAGE_CLAIM_CONFLICT", "read the original consumption receipt")
            return ControlChanges(writes=(
                _write(state_id, _updated(state, status="completed", decision=WakeDecision(
                    action="silence", reason=reason), request=request), "replace"),
                _write(queue_id, _updated(queue, pending=queue.pending[1:], request=request), "replace"),
            ), result={"message_key": claim.message_key, "consumed": True, "human_obligation_fulfilled": False})
        self.store.transact(request, decide)
        return self.read(principal, claim.message_key)

    def mark_unknown(self, principal, identifier, *, consumer_id, epoch, request_id):
        receiver = self.directory.authenticate(principal, "consume")
        claim, _ = self._verified(identifier, _QueueClaim)
        if receiver.agent_id != claim.receiver_agent_id or (consumer_id, epoch) != (claim.consumer_id, claim.epoch):
            raise Problem(403, "AGENT_MESSAGE_CLAIM_DENIED", "only the original receiver observation may become unknown")
        state_id = message_state_key(claim.message_key)
        request = ControlRequest(request_id=control_key("agent-message-contract-unknown", identifier, request_id),
            request_digest=digest([identifier, consumer_id, epoch]),
            read_set=(identifier, state_id))

        def decide(rows):
            current = _row(rows, identifier, _QueueClaim)
            state = _row(rows, state_id, _MessageState)
            if current != claim or state.claim_id != identifier or state.status not in {"claimed", "unknown"}:
                raise Problem(409, "AGENT_MESSAGE_CLAIM_CONFLICT", "unknown cannot replace a confirmed original outcome")
            return ControlChanges(writes=(_write(state_id, _updated(
                state, status="unknown", decision=WakeDecision(action="needs_attention",
                    reason="original_receiver_outcome_unknown"), request=request), "replace"),),
                result={"message_key": claim.message_key, "status": "unknown", "queue_released": False})
        self.store.transact(request, decide)
        return self.read(principal, claim.message_key)
