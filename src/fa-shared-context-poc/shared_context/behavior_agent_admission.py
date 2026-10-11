"""Pure Agent-message admission writes; the existing WorkerCore owns the later start."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
from typing import Mapping

from . import behavior_assistant_wire as aw
from .behavior_agent_role_prompt import AGENT_ROLE_PROFILE, ROLE_PROFILES, RolePromptCapture
from .behavior_agent_role_prompt_versions import AgentRolePromptHead, AgentRolePromptVersion
from .behavior_agent_worker import AgentVmBinding
from .behavior_assistant import task_for_profile
from .behavior_auth import BehaviorPrincipal
from .behavior_capabilities import capture as capture_capabilities, runtime_grants
from .behavior_foundry import NativeExecutionGrant, instruction_target, native_key
from .behavior_instruction import Conversation, TurnArchive, conversation_id, conversation_key, registration_write, sdk_session_id
from .behavior_instruction_models import InstructionModel
from .behavior_operations import BudgetRecord, OwnerRecord, PolicyRecord, _checked, _row, _utc, _write, control_key
from .behavior_profile_selection import ConversationProfileHead, ConversationProfileSelection, head_key, profile_from_template, selection_key
from .behavior_runtime import _PlanIndex, material, runtime_key
from .behavior_store import ControlRequest, ControlWrite
from .behavior_turn_receipts import TurnStartRecord, TurnTicketRecord, start_key, ticket_handle
from .behavior_turn_ticket import TurnAuthorization
from .behavior_turn_wire import DEFAULT_RELAY_MODEL_REQUESTS, StartedTurn, TurnRelayContext, TurnTicket
from .behavior_vm_assignment import capture_agent_vm
from .behavior_workers import JobRecord, JobSpec, Slot, _Pool, worker_key
from .behavior_work_policy import (
    AgentWorkOrigin, CostLedger, CostObservation, WorkRegistration, check_dispatch_row,
    cumulative_decision, derive_work, derive_work_binding, registered_prompt_override, work_policy_key,
)
from .behavior_work_registry import append_read_keys, append_writes, head_key as registry_head_key
from .errors import Problem
from .records import Record
from .source_materials import digest


@dataclass(frozen=True)
class PreparedAgentAdmission:
    registration: WorkRegistration
    job: JobRecord
    read_set: tuple[str, ...]
    writes: tuple[ControlWrite, ...]


def agent_admission_anchors(control, command, sender):
    """Initial point-read keys; no I/O, registration, or cached binding changes."""
    command = _checked(command, aw.InstructionSubmit)
    p = control.policy
    handle = ticket_handle(sender.ticket)
    from .behavior_agent_role_prompt_versions import head_key as role_head
    return tuple(dict.fromkeys((
        control.policy_key, control.revocation_key, control.cost_key,
        control_key("budget", p.template.owner.root_budget_id),
        control.registry_key, registry_head_key(control.registry_key),
        conversation_key(conversation_id(p, command)), head_key(p.authority_domain, p.agent_id),
        role_head(p.scope_ids), handle.ticket_id, start_key(handle.ticket_id),
    )))


def _previous(control, conversation):
    record = None if not conversation.jobs else control._job_record(conversation.jobs[-1])
    if conversation.jobs and record is None:
        raise Problem(409, "AGENT_ADMISSION_NOT_READY", "load the original recipient registrations before admission")
    return record


def agent_admission_keys(control, command, sender, anchors):
    """Complete the bounded read set using the already observed registry/conversation heads."""
    p = control.policy
    admission, binding = derive_work_binding(p, command.request_id, instruction=command)
    ckey = conversation_key(conversation_id(p, command))
    required = list(agent_admission_anchors(control, command, sender))
    if any(key not in anchors for key in required):
        raise Problem(422, "AGENT_ADMISSION_READS_MISSING", "the original admission anchors must be supplied")
    required.extend((
        work_policy_key("registration", admission),
        control_key("owner", binding.owner.owner_record_id), control_key("policy", binding.owner.policy_id),
        runtime_key("policy", binding.binding_id), runtime_key("plan-index", binding.binding_id),
        worker_key("job", binding.job_id), worker_key("pool", control.runtime.dep.workers.contract.workspace_id, binding.job_id),
        *append_read_keys(p, anchors),
    ))
    if control.runtime.mode == "native":
        required.append(native_key("grant", native_key("bootstrap-grant", binding.job_id)))
    choice_id = None
    if anchors[ckey] is not None:
        conversation = _row(anchors, ckey, Conversation)
        previous = _previous(control, conversation)
        if previous is not None:
            required.extend((work_policy_key("registration", previous.admission_id),
                             worker_key("job", previous.binding.job_id)))
            if conversation.archive_id is not None:
                required.append(conversation.archive_id)
            choice_id = previous.profile_selection_id
    else:
        hkey = head_key(p.authority_domain, p.agent_id)
        if anchors[hkey] is not None:
            choice_id = _row(anchors, hkey, ConversationProfileHead).selection_id
    if choice_id is not None:
        required.append(selection_key(p.authority_domain, choice_id, p.agent_id))
    hkey = head_key(p.authority_domain, p.agent_id)
    if anchors[hkey] is not None:
        current_choice = _row(anchors, hkey, ConversationProfileHead).selection_id
        required.append(selection_key(p.authority_domain, current_choice, p.agent_id))
    from .behavior_agent_role_prompt_versions import head_key as role_head, version_key as role_version
    hkey = role_head(p.scope_ids)
    if anchors[hkey] is not None:
        required.append(role_version(p.scope_ids, _row(anchors, hkey, AgentRolePromptHead).version))
    keys = tuple(dict.fromkeys(required))
    if len(keys) > 98:
        raise Problem(413, "AGENT_ADMISSION_LIMIT", "the original admission exceeds the control read-set bound")
    return keys


def _sender(rows, principal, sender, recipient_vm, relay, now):
    if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "agent"
            or principal.tenant_id != sender.ticket.tenant_id
            or principal.team != sender.ticket.vm.team_id or principal.team != recipient_vm.team_id
            or principal.oid != sender.ticket.agent_oid or principal.oid != sender.start.worker.caller_oid
            or principal.actor_id != sender.start.actor_id or sender.ticket.vm.agent_id == recipient_vm.agent_id):
        raise Problem(403, "AGENT_ADMISSION_DENIED", "the original verified sender and same-Team recipient are required")
    principal.require(recipient_vm.team_id, "submit")
    handle = ticket_handle(sender.ticket)
    actual = _row(rows, handle.ticket_id, TurnTicketRecord)
    started = _row(rows, start_key(handle.ticket_id), TurnStartRecord)
    if (actual.ticket != sender.ticket or not actual.started or started.start != sender.start
            or sender.start.ticket != handle):
        raise Problem(403, "AGENT_ADMISSION_DENIED", "the message must retain its original committed sender")
    parent = sender.ticket.relay
    ancestors = () if parent is None else parent.chain
    expected_root = handle.ticket_id if parent is None else parent.root
    if (relay.root != expected_root or relay.hop != (1 if parent is None else parent.hop + 1)
            or relay.chain != (*ancestors, sender.ticket.vm.agent_id) or recipient_vm.agent_id in relay.chain
            or now >= relay.deadline or parent is not None and relay.deadline > parent.deadline):
        raise Problem(403, "AGENT_ADMISSION_DENIED", "the original relay depth, chain and deadline may not expand")


def _conversation(control, rows, command, vm):
    p = control.policy
    ckey = conversation_key(conversation_id(p, command))
    row = rows[ckey]
    b = p.template
    if row is None:
        if command.conversation_id is not None:
            raise Problem(404, "CONVERSATION_NOT_FOUND", "the requested recipient conversation is not recorded")
        conversation = Conversation(
            conversation_id=conversation_id(p, command), human_oid=b.human_oid, team_id=b.team_id,
            agent_oid=b.agent_oid, sdk_session_id=sdk_session_id(conversation_id(p, command)))
        return capture_agent_vm(control, conversation, None, vm), None
    conversation = _row(rows, ckey, Conversation)
    if ((conversation.human_oid, conversation.team_id, conversation.agent_oid) != (b.human_oid, b.team_id, b.agent_oid)
            or conversation.agent_vm != vm or conversation.reset_request_id is not None):
        raise Problem(403, "CONVERSATION_SCOPE_DENIED", "the original recipient conversation and VM must match")
    previous = _previous(control, conversation)
    if (conversation.active_job_id is not None or previous is None or conversation.archive_id is None):
        raise Problem(409, "AGENT_ADMISSION_NOT_READY", "retain the inbox message until the original conversation is ready")
    job = _row(rows, worker_key("job", previous.binding.job_id), JobRecord)
    archive = _row(rows, conversation.archive_id, TurnArchive)
    if (job.state not in {"succeeded", "failed", "cancelled"}
            or archive.job_id != previous.binding.job_id or archive.conversation_id != conversation.conversation_id
            or not isinstance(archive.archive, (aw.SdkArchive, aw.StoredSdkArchive))):
        raise Problem(409, "AGENT_ADMISSION_NOT_READY", "the prior recipient turn must have a completed saved checkpoint")
    if any(outcome is not None and outcome.unresolved_operations
           for outcome in (archive.sop_copy_outcome, archive.skill_draft_outcome)):
        raise Problem(409, "AGENT_ADMISSION_NOT_READY", "retain the inbox message while prior writes remain unresolved")
    if _row(rows, work_policy_key("registration", previous.admission_id), WorkRegistration) != previous:
        raise Problem(409, "CONVERSATION_CONTEXT_CHANGED", "the original recipient registration differs")
    return conversation, previous


def _profile_choice(control, rows, previous):
    p = control.policy
    if previous is None:
        hkey = head_key(p.authority_domain, p.agent_id)
        choice_id = None if rows[hkey] is None else _row(rows, hkey, ConversationProfileHead).selection_id
    else:
        choice_id = previous.profile_selection_id
    if choice_id is None:
        return None, p.profile
    key = selection_key(p.authority_domain, choice_id, p.agent_id)
    choice = _row(rows, key, ConversationProfileSelection)
    if (choice.policy_sha256 != p.sha256 or choice.agent_id != p.agent_id
            or choice.selected_by != p.template.human_oid
            or rows[key].payload["material"] != material(choice.model_dump(mode="json")).model_dump(mode="json")):
        raise Problem(403, "PROFILE_SELECTION_INVALID", "the original approved recipient profile is required")
    return choice, profile_from_template(p, choice.command.template)


def _role_prompt(policy, rows, profile):
    if profile.prompt_profile not in ROLE_PROFILES:
        return None
    from .behavior_agent_role_prompt_versions import head_key as role_head, version_key as role_version
    hkey = role_head(policy.scope_ids)
    if rows[hkey] is None and profile.prompt_profile != AGENT_ROLE_PROFILE:
        return None
    if rows[hkey] is None:
        raise Problem(409, "AGENT_ROLE_PROMPT_MISSING", "the recipient has no approved role prompt")
    head = _row(rows, hkey, AgentRolePromptHead)
    key = role_version(policy.scope_ids, head.version)
    value = _row(rows, key, AgentRolePromptVersion)
    if value.version != head.version or value.sha256 != head.sha256:
        raise Problem(409, "AGENT_ROLE_PROMPT_INVALID", "the original approved role prompt differs")
    return RolePromptCapture(version=value.version, sha256=value.sha256, text=value.text)


def prepare_agent_admission(control, rows: Mapping[str, Record | None], *, principal: BehaviorPrincipal,
                            sender: StartedTurn, command: aw.InstructionSubmit, recipient_vm: AgentVmBinding,
                            relay: TurnRelayContext, message_id: str, model: InstructionModel,
                            observation: CostObservation, request: ControlRequest, now: datetime,
                            refs=(), model_call_limit=DEFAULT_RELAY_MODEL_REQUESTS) -> PreparedAgentAdmission:
    """Run inside the caller's decision: no I/O, transaction, dispatch or mutation of control.records."""
    command = _checked(command, aw.InstructionSubmit)
    now = _utc(now)
    read_set = agent_admission_keys(control, command, sender, rows)
    if not set(read_set) <= set(request.read_set) or not set(read_set) <= set(rows):
        raise Problem(422, "AGENT_ADMISSION_READS_MISSING", "the outer CAS must contain every original admission predicate")
    p = control.installed(rows)
    if p != control.policy or not p.active or not p.read_enabled or principal.tenant_id != p.template.tenant_id:
        raise Problem(403, "AGENT_ADMISSION_DENIED", "the recipient policy must allow this new admission")
    _sender(rows, principal, sender, recipient_vm, relay, now)
    if any((ref.tenant_id, ref.team_id) != (principal.tenant_id, principal.team) for ref in refs):
        raise Problem(403, "AGENT_ADMISSION_DENIED", "a message reference cannot cross its original Team")
    conversation, previous = _conversation(control, rows, command, recipient_vm)
    choice, effective = _profile_choice(control, rows, previous)
    if effective.memory_scope is not None or previous is not None and previous.profile.memory_scope is not None:
        raise Problem(403, "AGENT_ADMISSION_PROFILE_UNSUPPORTED", "the recipient memory capture needs its ordinary admission reader")
    if command.model is not None and command.model != model.id:
        raise Problem(403, "INSTRUCTION_MODEL_UNAVAILABLE", "the supplied model must be the server-resolved deployment")
    capabilities = None
    if effective.capabilities:
        if choice is None:
            raise Problem(403, "CAPABILITY_SELECTION_REQUIRED", "capabilities require the recipient's approved profile")
        hkey = head_key(p.authority_domain, p.agent_id)
        current_id = None if rows[hkey] is None else _row(rows, hkey, ConversationProfileHead).selection_id
        current = None if current_id is None else _row(
            rows, selection_key(p.authority_domain, current_id, p.agent_id), ConversationProfileSelection)
        current_refs = () if current is None else profile_from_template(p, current.command.template).capabilities
        capabilities = capture_capabilities(effective.capabilities, current_refs, current_id, runtime_grants(control.runtime))
    if capabilities is not None and capabilities.vision and model.supports_image_input is not True:
        raise Problem(422, "CAPABILITY_MODEL_UNSUPPORTED", "the captured capability requires an image-capable model")
    source = AgentWorkOrigin(message_id=message_id, sender=sender.start, relay=relay, refs=refs,
                             model_call_limit=model_call_limit, request=request)
    admission, binding, profile = derive_work(
        p, command.request_id, control.runtime.mode, instruction=command, agent_origin=source,
        turn_number=len(conversation.jobs) + 1, previous_archive_id=conversation.archive_id, model=model.id,
        hosted_agent_version=recipient_vm.hosted_agent_version,
        model_capacity=None if effective.model_capacity_profile is None else model.capacity_snapshot(),
        reasoning_summary="detailed" if effective.model_provider == "openai-responses" and model.supports_reasoning_summary else None,
        profile_template=None if choice is None else choice.command.template, capability_capture=capabilities,
        role_prompt=_role_prompt(p, rows, effective),
        prompt_override=registered_prompt_override(p, previous.profile) if previous is not None and choice is None else None,
    )
    decision = cumulative_decision(p, _row(rows, control.cost_key, CostLedger), observation, now,
                                   read_id=command.request_id, mode=control.runtime.mode, instruction=True)
    root_key = control_key("budget", binding.owner.root_budget_id)
    if decision.decision != "allow" or not _row(rows, root_key, BudgetRecord).active:
        raise Problem(403, "AGENT_ADMISSION_BUDGET_DENIED", "the original recipient budget must allow new work")
    policy = control._per_work_policy(p, admission, binding, profile, now, True)
    check_dispatch_row(control, admission, binding, profile, policy, observation)
    record = WorkRegistration(
        admission_id=admission, request_id=command.request_id,
        submit_sha256=hashlib.sha256(command.model_dump_json().encode()).hexdigest(),
        policy_id=p.policy_id, policy_revision=p.revision, policy_sha256=p.sha256, principal_oid=principal.oid,
        binding=binding, profile=profile, runtime_policy=policy, hosted_session_id=recipient_vm.hosted_session_id,
        profile_selection_id=None if choice is None else choice.selection_id, agent_scope=p.agent_scope,
        turn_authorization=TurnAuthorization(), agent_vm=recipient_vm, agent_origin=source,
    )
    worker = control.runtime.dep.workers
    task = task_for_profile(binding, profile)
    spec = JobSpec(job_id=binding.job_id, work_id=binding.work.work_id, input=task.model_dump_json().encode())
    job = JobRecord(configuration_digest=worker.configuration_digest(), spec=spec, owner=binding.owner,
                    state="queued", control_remaining=None)
    pool = _Pool(configuration_digest=worker.configuration_digest(), workspace_id=worker.contract.workspace_id,
                 job_id=binding.job_id, slots=(Slot(number=0),), pending=(binding.job_id,))
    data = (
        (work_policy_key("registration", admission), record, material(record.model_dump(mode="json"))),
        (control_key("owner", binding.owner.owner_record_id),
         OwnerRecord(**binding.owner.model_dump(), active=True, operation_ids=(binding.operation_id,)), None),
        (control_key("policy", binding.owner.policy_id),
         PolicyRecord(policy_id=binding.owner.policy_id, revision=policy.operation_policy_revision, active=True), None),
        (runtime_key("policy", binding.binding_id), policy, None),
        (runtime_key("plan-index", binding.binding_id), _PlanIndex(), None),
        (worker_key("job", binding.job_id), job, material_bytes(spec.input)),
        (worker_key("pool", worker.contract.workspace_id, binding.job_id), pool, None),
    )
    if any(rows[key] is not None for key, _, _ in data):
        raise Problem(409, "AGENT_ADMISSION_CONFLICT", "the original Agent request is already admitted; read its receipt")
    writes = tuple(_write(key, value, "create", retained) for key, value, retained in data)
    if control.runtime.mode == "native":
        native = control.runtime.dep.native_launches
        targets = () if native is None else tuple(t for t in native.targets if t.agent_oid == binding.agent_oid)
        if len(targets) != 1 or targets[0].agent_id != recipient_vm.agent_id:
            raise Problem(403, "AGENT_ADMISSION_DENIED", "the recipient requires its original configured execution target")
        grant_id = native_key("bootstrap-grant", binding.job_id)
        gkey = native_key("grant", grant_id)
        if rows[gkey] is not None:
            raise Problem(409, "AGENT_ADMISSION_CONFLICT", "the original worker grant already exists")
        grant = NativeExecutionGrant(
            grant_id=grant_id, controller_id=native.controller_id, owner=binding.owner,
            policy_revision=policy.operation_policy_revision, job_id=binding.job_id, definition=binding.work,
            input_digest=material_bytes(spec.input).sha256,
            targets=(instruction_target(targets[0], binding.job_id, agent_version=recipient_vm.hosted_agent_version,
                                        session_id=recipient_vm.hosted_session_id),),
            active=True, remaining_uses=1, not_before=now, expires_at=None, workload_version="assistant-v1.2",
        )
        writes += (_write(gkey, grant, "create"),)
    writes += (*append_writes(p, rows, admission), registration_write(conversation, binding.job_id))
    from .behavior_store import _Row
    for write in writes:
        control.store._entity("behavior:v1:row:" + write.row_id, _Row(
            binding=control.store._binding, row_id=write.row_id, data=write.data, material=write.material))
    return PreparedAgentAdmission(record, job, read_set, writes)


def material_bytes(raw):
    from .behavior_store import InlineMaterial
    return InlineMaterial(data=raw, sha256=hashlib.sha256(raw).hexdigest())


def ticket_for_agent_admission(registration, assignment):
    """After the real WorkerCore start, retain the original message source in its final execution ticket."""
    source = registration.agent_origin
    if source is None or registration.agent_vm is None or assignment.task != task_for_profile(
            registration.binding, registration.profile):
        raise Problem(403, "AGENT_ADMISSION_DENIED", "the final ticket must use its original Agent admission")
    from .behavior_agent_worker import AgentTurnAddress
    return TurnTicket(
        tenant_id=registration.binding.tenant_id, vm=registration.agent_vm,
        address=AgentTurnAddress(conversation_id=assignment.task.instruction.conversation_id,
                                sdk_session_id=assignment.sdk_session_id, request_id=registration.request_id),
        assignment=assignment, owner_oid=registration.binding.human_oid, agent_oid=registration.binding.agent_oid,
        issuer_oid=registration.principal_oid, source="agent", request_sha256=registration.submit_sha256,
        relay=source.relay, model_call_limit=source.model_call_limit, refs=source.refs,
    )


def validate_agent_admission_history(view, config, policy, record, records):
    """Check the new admission's portion of its caller-owned message/root transaction."""
    from .behavior_instruction import registration_image
    from .behavior_work_registry import registration_index_images
    from .behavior_work_policy import registered_instruction

    origin = record.agent_origin
    if origin is None:
        raise Problem(409, "AGENT_ADMISSION_INVALID", "the original Agent source is required")
    receipt = view.receipts.get(origin.request.request_id)
    if receipt is None or receipt.request != origin.request:
        raise Problem(409, "AGENT_ADMISSION_INVALID", "the Agent admission has no original outer transaction")
    sender_ticket = view.row(origin.sender.ticket.ticket_id, TurnTicketRecord).ticket
    sender_start = view.row(start_key(origin.sender.ticket.ticket_id), TurnStartRecord)
    if (sender_start.start != origin.sender or origin.sender.ticket != ticket_handle(sender_ticket)
            or origin.sender.worker.caller_oid != record.principal_oid
            or sender_ticket.agent_oid != record.principal_oid
            or sender_ticket.vm.team_id != record.binding.team_id
            or sender_ticket.tenant_id != record.binding.tenant_id
            or record.agent_vm is None or record.agent_vm.agent_id == sender_ticket.vm.agent_id
            or view.receipts[sender_start.request.request_id].guard_revision >= receipt.guard_revision):
        raise Problem(409, "AGENT_ADMISSION_INVALID", "the original sender/start must precede the recipient admission")
    parent = sender_ticket.relay
    if (origin.relay.root != (origin.sender.ticket.ticket_id if parent is None else parent.root)
            or origin.relay.hop != (1 if parent is None else parent.hop + 1)
            or origin.relay.chain != (*(() if parent is None else parent.chain), sender_ticket.vm.agent_id)
            or record.agent_vm.agent_id in origin.relay.chain
            or record.runtime_policy.accepted_at >= origin.relay.deadline
            or parent is not None and origin.relay.deadline > parent.deadline):
        raise Problem(409, "AGENT_ADMISSION_INVALID", "the accepted relay scope differs from its original sender")
    if any((ref.tenant_id, ref.team_id) != (record.binding.tenant_id, record.binding.team_id) for ref in origin.refs):
        raise Problem(409, "AGENT_ADMISSION_INVALID", "an original reference crossed the recipient Team")
    revoked_key = work_policy_key("revocation", *policy.scope_ids)
    if revoked_key in view.inventory.rows and (
            revoked_key not in view.receipts
            or view.receipts[revoked_key].guard_revision <= receipt.guard_revision):
        raise Problem(409, "AGENT_ADMISSION_INVALID", "revoked recipient policy cannot accept a new Agent work")
    b = record.binding
    task = task_for_profile(b, record.profile)
    submitted = registered_instruction(record)
    if record.submit_sha256 != hashlib.sha256(submitted.model_dump_json().encode()).hexdigest():
        raise Problem(409, "AGENT_ADMISSION_INVALID", "the original accepted instruction bytes differ")
    choice = None
    if record.profile_selection_id is not None:
        choice = view.row(selection_key(policy.authority_domain, record.profile_selection_id, policy.agent_id),
                          ConversationProfileSelection)
        if view.receipts[choice.request.request_id].guard_revision >= receipt.guard_revision:
            raise Problem(409, "AGENT_ADMISSION_INVALID", "the recipient profile was not approved before admission")
    captured = record.profile.instruction.capabilities
    if captured is not None:
        from .behavior_capabilities import check_captured
        if choice is None:
            raise Problem(409, "AGENT_ADMISSION_INVALID", "the admitted capabilities have no original profile")
        current = None if captured.head_selection_id is None else view.row(
            selection_key(policy.authority_domain, captured.head_selection_id, policy.agent_id), ConversationProfileSelection)
        if current is not None and view.receipts[current.request.request_id].guard_revision >= receipt.guard_revision:
            raise Problem(409, "AGENT_ADMISSION_INVALID", "the capability head postdates the original admission")
        check_captured(profile_from_template(policy, choice.command.template).capabilities, captured,
                       () if current is None else profile_from_template(policy, current.command.template).capabilities)
    spec = JobSpec(job_id=b.job_id, work_id=b.work.work_id, input=task.model_dump_json().encode())
    worker_digest = digest({"contract": config.workers.model_dump(mode="json"), "mode": "native"})
    job = JobRecord(configuration_digest=worker_digest, spec=spec, owner=b.owner, state="queued", control_remaining=None)
    pool = _Pool(configuration_digest=worker_digest, workspace_id=config.workers.workspace_id,
                 job_id=b.job_id, slots=(Slot(number=0),), pending=(b.job_id,))
    rkey = work_policy_key("registration", record.admission_id)
    initial = {
        rkey: record,
        control_key("owner", b.owner.owner_record_id): OwnerRecord(
            **b.owner.model_dump(), active=True, operation_ids=(b.operation_id,)),
        control_key("policy", b.owner.policy_id): PolicyRecord(
            policy_id=b.owner.policy_id, revision=record.runtime_policy.operation_policy_revision, active=True),
        runtime_key("policy", b.binding_id): record.runtime_policy,
        runtime_key("plan-index", b.binding_id): _PlanIndex(),
        worker_key("job", b.job_id): job,
        worker_key("pool", config.workers.workspace_id, b.job_id): pool,
        conversation_key(task.instruction.conversation_id): registration_image(record, records),
    }
    grant_key = native_key("grant", native_key("bootstrap-grant", b.job_id))
    from .behavior_team_registry import inventory_agent_scope
    target = (inventory_agent_scope(view.inventory, config, policy.agent_id).target if policy.agent_id is not None
              else next(target for target in config.active_targets if target.agent_oid == b.agent_oid))
    initial[grant_key] = NativeExecutionGrant(
        grant_id=native_key("bootstrap-grant", b.job_id), controller_id=config.controller_id, owner=b.owner,
        policy_revision=record.runtime_policy.operation_policy_revision, job_id=b.job_id, definition=b.work,
        input_digest=hashlib.sha256(spec.input).hexdigest(), targets=(instruction_target(
            target, b.job_id, agent_version=record.agent_vm.hosted_agent_version,
            session_id=record.hosted_session_id),),
        active=True, remaining_uses=1, not_before=record.runtime_policy.accepted_at, expires_at=None,
        workload_version="assistant-v1.2",
    )
    layout = registration_index_images(view.inventory, policy, record.admission_id)
    writes = dict(receipt.writes)
    if (not set((*initial, *layout)) <= set(writes)
            or not {work_policy_key("policy", *policy.scope_ids), revoked_key,
                    work_policy_key("cost", policy.authority_domain),
                    control_key("budget", b.owner.root_budget_id),
                    origin.sender.ticket.ticket_id, start_key(origin.sender.ticket.ticket_id),
                    *initial, *layout} <= set(receipt.request.read_set)):
        raise Problem(409, "AGENT_ADMISSION_INVALID", "the original admission predicates/writes are incomplete")
    for key, value in {**initial, **{k: v for k, v in layout.items() if v is not None}}.items():
        if writes[key] != view.image_hash(key, value):
            raise Problem(409, "AGENT_ADMISSION_INVALID", "the original admission write image differs")
    current = view.row(worker_key("job", b.job_id), JobRecord)
    if (current.spec != spec or current.spec.obligation_id is not None or current.spec.parent_job_id is not None
            or view.inventory.rows[worker_key("job", b.job_id)].payload["material"]
                != material_bytes(spec.input).model_dump(mode="json")):
        raise Problem(409, "AGENT_ADMISSION_INVALID", "an Agent work cannot acquire a human obligation or new input")
