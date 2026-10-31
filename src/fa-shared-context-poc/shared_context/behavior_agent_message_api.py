"""Trusted peer-message entry points; credentials and assignments stay server-side."""

import logging
from datetime import timedelta
from typing import Literal

from .behavior_agent_message_wire import (
    CAPABILITY_ID, AgentMessageList, AgentMessageProjection, AgentMessageRead, AgentMessageRecipient,
    PeerTeamReference, PublicAgentDirectory, PublicAgentEntry,
)
from .behavior_agent_messages import (
    AgentAssignment, AgentDirectory, AgentMessageLedger, AgentRoute, RootSpec, root_key, root_state_key, _RootState,
    message_key,
)
from .behavior_auth import BehaviorPrincipal
from .behavior_capabilities import BUILTIN_CAPABILITIES, runtime_grants
from .behavior_delegation import capture_parent, _current_parent, _parent_keys
from .behavior_operations import _Model, _row, _write, control_key
from .behavior_runtime import material
from .behavior_store import ControlChanges, ControlRequest, Digest
from .behavior_wire import Identifier
from .behavior_workers import ExecutorBinding
from .errors import Problem
from .source_materials import _payload_hash, digest


class AgentMessageOrigin(_Model):
    tenant_id: Identifier
    team_id: Identifier
    binding_id: Identifier
    parent_request_id: Identifier
    root_request_id: Identifier
    registration_id: Identifier
    registration_sha256: Digest
    session_id: Identifier
    sdk_session_id: Identifier
    profile_sha256: Digest
    capability_id: Literal["fa-agent-collaboration-v1"] = "fa-agent-collaboration-v1"
    capability_sha256: Digest
    execution: ExecutorBinding


class AgentCollaborationRoot(_Model):
    kind: Literal["agent_collaboration_root"] = "agent_collaboration_root"
    origin: AgentMessageOrigin
    directory: AgentDirectory
    spec: RootSpec
    recipient_policy_sha256: Digest
    recipient_hosted_version: str
    request: ControlRequest


def collaboration_key(binding_id):
    return control_key("agent-collaboration-root", binding_id)


def _composition(authority):
    composition = getattr(authority.runtime, "agent_message_composition", None)
    if composition is None:
        raise Problem(503, "AGENT_MESSAGE_UNAVAILABLE", "the original peer composition is unavailable")
    return getattr(composition, "_agent_message_owner", composition)


def assignment_recipient(authority, binding, task):
    capture = None if task.instruction is None else task.instruction.capabilities
    if capture is None or not any(grant.entry_id == CAPABILITY_ID for grant in capture.grants):
        return None
    owner = _composition(authority)
    if binding.agent_oid != owner.config.targets[0].agent_oid:
        raise Problem(403, "AGENT_MESSAGE_ROUTE_DENIED", "only the original A assignment may address B")
    target = owner.config.targets[1]
    return AgentMessageRecipient(agent_id=target.agent_id, display_name=target.agent_name)


def _captured(authority, session, *, current=True):
    task = session.assignment.task.instruction
    capture = None if task is None else task.capabilities
    grant = None if capture is None else next((item for item in capture.grants if item.entry_id == CAPABILITY_ID), None)
    if (grant is None or grant.kind != "fa-builtin" or grant.tools != ("send_agent_message",) or grant.vision
            or grant.entry_sha256 != BUILTIN_CAPABILITIES[CAPABILITY_ID].entry_sha256):
        raise Problem(403, "AGENT_MESSAGE_DISABLED", "this original assignment did not capture peer collaboration")
    if current:
        from .behavior_profile_selection import current_selection
        control = authority.runtime.work_policy
        selected = current_selection(authority.store, control.policy)
        if (runtime_grants(authority.runtime).get(CAPABILITY_ID) != grant or selected is None
                or grant.ref not in selected.command.template.settings.capabilities):
            raise Problem(403, "AGENT_MESSAGE_DISABLED", "the current approved collaboration capability is unavailable")
    return grant


def _read_root(store, key):
    rows = store.read_snapshot((key,))
    value = _row(rows, key, AgentCollaborationRoot)
    receipt = store.reconcile(value.request)
    if (value.request.request_id != key or rows[key].payload["material"] != material(value.model_dump(mode="json")).model_dump(mode="json")
            or dict(receipt.payload["writes"]) != {key: _payload_hash(rows[key].payload)}):
        raise Problem(409, "AGENT_MESSAGE_RECORD_INVALID", "the original collaboration root receipt differs")
    return value


def _reader(route):
    return BehaviorPrincipal(route.tenant_id, route.principal_oid, route.team_id, "agent", route.actor_id,
                             frozenset({"read"}))


def _route(config, policy, *, conversation_id, hosted_session_id, sdk_session_id, profile_sha256):
    target = next((item for item in config.targets if item.agent_oid == policy.template.agent_oid), None)
    if target is None:
        raise Problem(403, "AGENT_MESSAGE_ROUTE_DENIED", "the registered identity has no server target")
    scope = config.agent_scope(target.agent_id)
    return AgentRoute(
        agent_id=target.agent_id, tenant_id=scope.tenant_id, team_id=scope.team_id,
        principal_oid=scope.identity.oid, actor_id=scope.identity.actor,
        policy_id=policy.policy_id, template_id=policy.template.binding_id, template_sha256=profile_sha256,
        target_id=target.executor_id, conversation_id=conversation_id, hosted_session_id=hosted_session_id,
        sdk_session_id=sdk_session_id,
        memory_scope_id=control_key("agent-private-memory", scope.authority_domain, target.agent_id),
        cwd_scope_id=control_key("agent-private-cwd", target.agent_id, conversation_id),
        credential_binding_id=control_key("agent-credential-binding", target.agent_oid))


def _new_root(authority, session, principal, recipient_agent_id):
    from .behavior_instruction import hosted_session_id, sdk_session_id
    from .behavior_work_target import instruction_hosted_version
    owner = _composition(authority)
    grant = _captured(authority, session)
    binding, _, _ = authority.runtime.authorize(session.binding_id, principal, "execute")
    origin = AgentMessageOrigin(**{
        **capture_parent(authority, session, capability_sha256=grant.entry_sha256).model_dump(),
        "capability_id": CAPABILITY_ID,
    })
    if (binding.agent_oid != owner.config.targets[0].agent_oid
            or recipient_agent_id != owner.config.targets[1].agent_id):
        raise Problem(403, "AGENT_MESSAGE_ROUTE_DENIED", "this scope permits only the original A-to-B-to-A pair")
    control = authority.runtime.work_policy
    record = control.records[binding.binding_id]
    peer = owner.for_agent(recipient_agent_id)
    recipient = peer.runtime.work_policy
    recipient_policy = recipient.installed()
    if not recipient_policy.active or not recipient_policy.read_enabled:
        raise Problem(403, "AGENT_MESSAGE_ROUTE_DENIED", "the recipient has no current registered work/read authority")
    version = instruction_hosted_version(recipient, None, existing=False)
    if record.hosted_session_id is None:
        raise Problem(403, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "the original Hosted conversation is required")
    cid = control_key("agent-collaboration-conversation", record.admission_id, recipient_agent_id)
    directory = AgentDirectory(agents=(
        _route(owner.config, control.policy, conversation_id=record.profile.instruction.conversation_id,
               hosted_session_id=record.hosted_session_id, sdk_session_id=origin.sdk_session_id,
               profile_sha256=origin.profile_sha256),
        _route(owner.config, recipient_policy, conversation_id=cid, hosted_session_id=hosted_session_id(cid),
               sdk_session_id=sdk_session_id(cid), profile_sha256=digest(recipient_policy.profile.model_dump(mode="json"))),
    ))
    source = record.profile.source
    spec = RootSpec(
        root_request_id=record.admission_id, origin_agent_id=directory.agents[0].agent_id,
        origin_work_id=binding.work.work_id, origin_attempt_id=binding.attempt_id,
        root_budget_id=binding.owner.root_budget_id, human_obligation_id=binding.obligation_id,
        deadline=record.runtime_policy.accepted_at + timedelta(seconds=control.policy.queue_seconds),
        max_messages=8, max_hops=2,
        allowed_refs=(PeerTeamReference(resource_kind="source", tenant_id=binding.tenant_id, team_id=binding.team_id,
                                       resource_id=source.source_id, version=str(source.version), sha256=source.sha256),))
    if authority.runtime._now() >= spec.deadline:
        raise Problem(403, "AGENT_MESSAGE_ROOT_EXPIRED", "the original peer work allowance has expired")
    key = collaboration_key(binding.binding_id)
    request = ControlRequest(
        request_id=key, request_digest=digest([origin.model_dump(mode="json"), directory.model_dump(mode="json"),
                                             spec.model_dump(mode="json"), recipient_policy.sha256, version]),
        read_set=tuple(dict.fromkeys((*authority._keys(session), *_parent_keys(origin),
                                    recipient.policy_key, recipient.revocation_key, key))))
    value = AgentCollaborationRoot(origin=origin, directory=directory, spec=spec,
                                  recipient_policy_sha256=recipient_policy.sha256, recipient_hosted_version=version,
                                  request=request)

    def decide(rows):
        authority._gate(session, rows)
        _current_parent(rows, origin)
        if authority.runtime._now() >= spec.deadline:
            raise Problem(403, "AGENT_MESSAGE_ROOT_EXPIRED", "the original peer work allowance has expired")
        if recipient.installed(rows) != recipient_policy:
            raise Problem(409, "AGENT_MESSAGE_ROUTE_DENIED", "the original recipient policy changed")
        return ControlChanges(writes=(_write(key, value, "create", material(value.model_dump(mode="json"))),),
                              result={"root_request_id": spec.root_request_id, "model_started": False})
    authority.store.transact(request, decide)
    return _read_root(authority.store, key)


def _original_root(authority, session, principal, *, recipient_agent_id=None, new_message=None):
    _captured(authority, session, current=recipient_agent_id is not None)
    key = collaboration_key(session.binding_id)
    row = authority.store.read_snapshot((key,))[key]
    if row is None:
        if recipient_agent_id is None:
            raise Problem(503, "AGENT_MESSAGE_UNKNOWN", "the original message root has not been confirmed")
        if new_message is None or new_message.in_reply_to is not None or new_message.kind not in {"request", "handoff", "fyi"}:
            raise Problem(403, "AGENT_MESSAGE_REPLY_DENIED", "a reply requires this assignment's existing original root")
        root = _new_root(authority, session, principal, recipient_agent_id)
    else:
        root = _read_root(authority.store, key)
    if (root.origin.binding_id != session.binding_id or root.origin.session_id != session.assignment.session_id
            or root.origin.execution != session.execution):
        raise Problem(403, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "the message root belongs to another original assignment")
    peer = _composition(authority).for_agent(root.directory.agents[1].agent_id)
    policy = peer.runtime.work_policy.installed()
    if not policy.active or not policy.read_enabled or policy.sha256 != root.recipient_policy_sha256:
        raise Problem(403, "AGENT_MESSAGE_ROUTE_DENIED", "the original recipient's current read/work authority differs")
    ledger = AgentMessageLedger(authority.store, root.directory, clock=authority.runtime._now)
    ledger_key = root_key(root.spec.root_request_id)
    if authority.store.read_snapshot((ledger_key,))[ledger_key] is None:
        if recipient_agent_id is None:
            raise Problem(503, "AGENT_MESSAGE_UNKNOWN", "the original ledger root has not been confirmed")
        ledger.register_root(root.spec)
    return root, ledger, peer


def _receiver_record(root, ledger, peer, *, identifier=None):
    from .behavior_work_policy import WorkRegistration, derive_work_binding, work_policy_key
    if identifier is None:
        state, _ = ledger._verified(root_state_key(root.spec.root_request_id), _RootState)
        identifier = state.handoff_message_id
    if identifier is None:
        return None
    control = peer.runtime.work_policy
    admission, _ = derive_work_binding(control.policy, identifier)
    key = work_policy_key("registration", admission)
    rows = peer.runtime.store.read_snapshot((key,))
    return None if rows[key] is None else _row(rows, key, WorkRegistration)


def _prepare_receiver(authority, session, principal, root, ledger, peer, command):
    from .behavior_assistant_wire import InstructionSubmit, PeerWorkReference
    if command.kind not in {"request", "handoff"}:
        return _receiver_record(root, ledger, peer)
    identifier = message_key(root.spec.root_request_id, root.spec.origin_agent_id, command.message_id)
    state, _ = ledger._verified(root_state_key(root.spec.root_request_id), _RootState)
    if state.handoff_message_id is not None and state.handoff_message_id != identifier:
        raise Problem(409, "AGENT_MESSAGE_HANDOFF_EXISTS", "the original receiver work is already retained")
    existing = _receiver_record(root, ledger, peer, identifier=identifier)
    if existing is not None:
        if existing.submit_sha256 != digest(command.model_dump(mode="json")):
            raise Problem(409, "AGENT_MESSAGE_ORIGINAL_REQUIRED", "the original peer work has different input")
        return existing
    reference = PeerWorkReference(
        root_id=root.request.request_id, message_id=command.message_id, message_key=identifier,
        message_sha256=digest(command.model_dump(mode="json")), content_sha256=digest(command.content),
        sender_agent_id=root.spec.origin_agent_id, recipient_agent_id=command.recipient_agent_id,
        recipient_work_id=control_key("agent-message-contract-work", identifier, command.recipient_agent_id),
        conversation_id=root.directory.agents[1].conversation_id)
    instruction = InstructionSubmit(request_id=identifier, content=command.content,
                                    model=session.assignment.task.instruction.model)
    return peer.runtime.work_policy.register(principal, identifier, reference.message_sha256,
                                            instruction=instruction, peer_work=reference)


def _references(authority, session, principal, root, peer, refs, receiver):
    if not refs:
        return
    if receiver is None:
        raise Problem(409, "AGENT_MESSAGE_RECIPIENT_NOT_READY", "an original registered receiver scope is required for references")
    from .behavior_agent_message_refs import validate_team_refs
    validate_team_refs(
        sender_authority=authority, sender_binding=authority._binding(session), sender_principal=principal,
        receiver_authority=peer.assistant.authority, receiver_binding=receiver.binding,
        receiver_principal=_reader(root.directory.agents[1]), refs=refs, allowed_refs=root.spec.allowed_refs)


def _projection(root, view):
    message = view.message
    return AgentMessageProjection(
        message_key=message.message_key, message_id=message.message_id, root_request_id=message.root_request_id,
        sender_agent_id=message.sender_agent_id, recipient_agent_id=message.recipient_agent_id,
        from_work_id=message.from_work_id, to_work_id=message.to_work_id,
        from_conversation_id=root.directory.resolve(message.sender_agent_id).conversation_id,
        to_conversation_id=root.directory.resolve(message.recipient_agent_id).conversation_id,
        kind=message.message_kind, hop=message.hop, status=view.status, decision=view.decision)


def _result(root, ledger, view, *, fresh=False):
    state, _ = ledger._verified(root_state_key(root.spec.root_request_id), _RootState)
    return AgentMessageRead(
        message=_projection(root, view), content=view.command.content,
        refs=tuple(PeerTeamReference.model_validate(ref.model_dump(mode="json")) for ref in view.command.refs),
        payload_sha256=view.message.payload_sha256, response_message_key=state.response_message_id,
        fresh=fresh, root_deadline=root.spec.deadline)


def _receiver_busy(peer, receiver, *, for_dispatch=False):
    from .behavior_workers import JobRecord, worker_key
    control = peer.runtime.work_policy
    control.refresh()
    records = {worker_key("job", record.binding.job_id): record for record in control.records.values()
               if record.profile.instruction is not None
               and (receiver is None or record.binding.job_id != receiver.binding.job_id)}
    keys = tuple(records)
    if not keys:
        return False
    for offset in range(0, len(keys), 98):
        batch = keys[offset:offset + 98]
        rows = peer.runtime.store.read_snapshot(batch)
        unstarted = {key: reference for key in batch if rows[key] is None
                     and (reference := getattr(records[key].profile.instruction, "peer_work", None)) is not None}
        messages = ({} if not unstarted else peer.runtime.store.read_snapshot(
            tuple(dict.fromkeys(reference.message_key for reference in unstarted.values()))))
        from .behavior_agent_messages import message_state_key, _MessageState
        state_keys = tuple(dict.fromkeys(message_state_key(reference.message_key) for reference in unstarted.values()
                                       if messages[reference.message_key] is not None))
        states = {} if not state_keys else peer.runtime.store.read_snapshot(state_keys)
        for key in batch:
            if key in unstarted:
                message_id = unstarted[key].message_key
                state_id = message_state_key(message_id)
                if (for_dispatch or messages[message_id] is None or states[state_id] is not None
                        and _row(states, state_id, _MessageState).status in {"silenced", "completed"}):
                    continue
            if rows[key] is None or _row(rows, key, JobRecord).state not in {"succeeded", "failed", "cancelled"}:
                return True
    return False


def send(authority, session, command, principal):
    from .behavior_assistant import _read_conflicts
    root, ledger, peer = _original_root(authority, session, principal,
                                        recipient_agent_id=command.message.recipient_agent_id, new_message=command.message)
    assignment = AgentAssignment(
        root_request_id=root.spec.root_request_id, agent_id=root.spec.origin_agent_id,
        work_id=session.execution.work_id, attempt_id=root.spec.origin_attempt_id)
    if any(ref not in root.spec.allowed_refs for ref in command.message.refs):
        raise Problem(403, "AGENT_MESSAGE_SHARE_DENIED", "the message cannot add sources to the original approved root")
    if any(ref.resource_kind == "source" and ref.resource_id not in peer.runtime.work_policy.policy.template.source_ids
           for ref in command.message.refs):
        raise Problem(403, "AGENT_MESSAGE_SHARE_DENIED", "the original receiver policy does not include this source")
    if command.message.kind in {"request", "handoff"}:
        ledger.check_handoff(principal, assignment, command.message)
    if command.message.refs:
        from .behavior_agent_message_refs import preflight_team_refs
        _read_conflicts(lambda: preflight_team_refs(
            sender_authority=authority, sender_binding=authority._binding(session), sender_principal=principal,
            receiver_authority=peer.assistant.authority, receiver_policy=peer.runtime.work_policy.installed(),
            refs=command.message.refs, allowed_refs=root.spec.allowed_refs), phase="peer_reference_preflight")
    identifier = (message_key(root.spec.root_request_id, root.spec.origin_agent_id, command.message.message_id)
                  if command.message.kind in {"request", "handoff"} else None)
    existing = _receiver_record(root, ledger, peer, identifier=identifier)
    busy = _receiver_busy(peer, existing)
    receiver = _prepare_receiver(authority, session, principal, root, ledger, peer, command.message)

    def current():
        _captured(authority, session)
        authority._gate(session, authority.store.read_snapshot(authority._keys(session)))
        _references(authority, session, principal, root, peer, command.message.refs, receiver)
        if peer.runtime.work_policy.installed().sha256 != root.recipient_policy_sha256:
            raise Problem(403, "AGENT_MESSAGE_ROUTE_DENIED", "the recipient policy changed before saving")
    _read_conflicts(current, phase="peer_reference_current")
    result = authority.store.validated_writes(
        lambda: ledger.send_recorded(principal, assignment, command.message, receiver_busy=busy), current)
    return _result(root, ledger, result.view, fresh=result.fresh)


def read(authority, session, command, principal):
    root, ledger, peer = _original_root(authority, session, principal)
    result = ledger.read(principal, command.message_key)
    if result.message.root_request_id != root.spec.root_request_id:
        raise Problem(403, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "the requested message belongs to another root")
    _references(authority, session, principal, root, peer, result.command.refs, _receiver_record(root, ledger, peer))
    return _result(root, ledger, result)


def public_messages(composition, principal, binding_id):
    control = composition.runtime.work_policy
    control._human(principal)
    binding, _, _ = composition.runtime.authorize(binding_id, principal, "read")
    record = control.records.get(binding_id)
    if record is None or record.profile.instruction is None:
        raise Problem(403, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "an original registered conversation is required")
    owner = getattr(composition, "_agent_message_owner", composition)
    agent_id = next(target.agent_id for target in owner.config.targets if target.agent_oid == binding.agent_oid)
    peer_work = record.profile.instruction.peer_work
    key = collaboration_key(binding_id) if peer_work is None else peer_work.root_id
    rows = composition.runtime.store.read_snapshot((key,))
    messages = ()
    if rows[key] is not None:
        root = _read_root(composition.runtime.store, key)
        ledger_rows = composition.runtime.store.read_snapshot(
            (root_key(root.spec.root_request_id), root_state_key(root.spec.root_request_id)))
        if any(row is not None for row in ledger_rows.values()):
            if any(row is None for row in ledger_rows.values()):
                raise Problem(409, "AGENT_MESSAGE_RECORD_INVALID", "the original ledger initialization is incomplete")
            ledger = AgentMessageLedger(composition.runtime.store, root.directory, clock=composition.runtime._now)
            messages = tuple(_projection(root, value) for value in ledger.messages(
                _reader(root.directory.resolve(agent_id)), root.spec.root_request_id))
    return AgentMessageList(agent_id=agent_id, conversation_id=record.profile.instruction.conversation_id,
                            binding_id=binding_id, messages=messages)


def public_agents(composition, principal):
    owner = getattr(composition, "_agent_message_owner", composition)
    control = owner.runtime.work_policy
    if control is None:
        raise Problem(503, "WORK_POLICY_DISABLED", "the original standing policy is required")
    control._human(principal)
    policy = control.installed()
    if not policy.active or not policy.read_enabled:
        raise Problem(403, "WORK_POLICY_REVOKED", "current original read authority is required")
    entries = []
    from .behavior_team_registry import registered_agent_ids, resolve_agent_scope
    configured = tuple(target.agent_id for target in owner.config.targets)
    registered = tuple(agent_id for agent_id in registered_agent_ids(owner.runtime.store, owner.config)
                       if agent_id not in configured)
    for agent_id in configured + registered:
        reason, display_name = None, agent_id
        try:
            if agent_id in configured:
                scope = owner.config.agent_scope(agent_id)
            elif owner.config.identity.team_id != principal.team:
                continue
            else:
                scope = resolve_agent_scope(owner.runtime.store, owner.config, agent_id)
            display_name = scope.target.agent_name
            if scope.team_id != principal.team:
                continue
            if agent_id not in configured:
                from .behavior_agent_onboarding import agent_onboarding, onboarding_not_ready
                onboarding = agent_onboarding(owner.runtime.store, owner.config, agent_id)
                if onboarding is not None:
                    display_name = onboarding.display_name
                    if onboarding.state != "ready":
                        raise onboarding_not_ready()
            peer = owner.for_agent(agent_id)
            current = peer.runtime.work_policy
            current._human(principal)
            selected = current.installed()
            if not selected.active or not selected.read_enabled:
                raise Problem(403, "WORK_POLICY_REVOKED", "the selected peer is unavailable")
            from .behavior_work_target import instruction_hosted_version
            instruction_hosted_version(current, None, existing=False)
        except Problem as error:
            reason = error.body["error"]["code"]
            logging.getLogger(__name__).warning("AGENT_DIRECTORY_UNAVAILABLE agent_id=%s code=%s",
                                                agent_id, reason)
        entries.append(PublicAgentEntry(
            agent_id=agent_id, display_name=display_name, api_prefix="/agents/" + agent_id,
            state="ready" if reason is None else "unavailable", reason=reason))
    return PublicAgentDirectory(default_agent_id=owner.config.targets[0].agent_id, agents=tuple(entries))
