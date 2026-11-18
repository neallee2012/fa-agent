"""Trusted peer-message entry points; credentials and assignments stay server-side."""

import logging
from typing import Literal

from .behavior_agent_message_wire import CAPABILITY_ID, AgentMessageList, AgentMessageProjection, AgentMessageRead, PeerTeamReference, PublicAgentDirectory, PublicAgentEntry
from .behavior_agent_messages import AgentDirectory, AgentMessageLedger, AgentRoute, RootSpec, root_key, root_state_key, _RootState
from .behavior_auth import BehaviorPrincipal
from .behavior_capabilities import BUILTIN_CAPABILITIES, runtime_grants
from .behavior_operations import _Model, _row, control_key
from .behavior_runtime import material
from .behavior_store import ControlRequest, Digest
from .behavior_wire import Identifier
from .behavior_workers import ExecutorBinding
from .errors import Problem
from .source_materials import _payload_hash


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
