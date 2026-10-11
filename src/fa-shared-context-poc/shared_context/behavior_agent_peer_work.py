"""Unpaid receiver registration from one already-authorized peer assignment."""

from .behavior_assistant_wire import InstructionSubmit, PeerWorkReference
from .behavior_auth import BehaviorPrincipal
from .behavior_delegation import _current_parent, _parent_keys
from .behavior_operations import control_key
from .errors import Problem
from .source_materials import digest


def peer_reads(root):
    return tuple(dict.fromkeys((root.request.request_id, *_parent_keys(root.origin))))


def validate_reference(root, policy, reference, request_id, instruction):
    from .behavior_agent_messages import message_key
    if (type(reference) is not PeerWorkReference or type(instruction) is not InstructionSubmit
            or reference.root_id != root.request.request_id
            or reference.sender_agent_id != root.spec.origin_agent_id
            or reference.recipient_agent_id != policy.agent_id
            or reference.recipient_agent_id != root.directory.agents[1].agent_id
            or policy.sha256 != root.recipient_policy_sha256
            or reference.message_key != request_id or instruction.request_id != request_id
            or reference.message_key != message_key(root.spec.root_request_id, reference.sender_agent_id, reference.message_id)
            or reference.recipient_work_id != control_key("agent-message-contract-work", reference.message_key, policy.agent_id)
            or reference.conversation_id != root.directory.agents[1].conversation_id
            or reference.content_sha256 != digest(instruction.content)
            or instruction.conversation_id is not None or instruction.files or instruction.attachments):
        raise Problem(403, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "the receiver work differs from the original peer assignment")


def authorize_registration(control, principal, reference, request_id, instruction):
    from .behavior_agent_message_api import _read_root
    root = _read_root(control.store, reference.root_id)
    validate_reference(root, control.policy, reference, request_id, instruction)
    sender = root.directory.agents[0]
    if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "agent"
            or (principal.tenant_id, principal.team, principal.oid, principal.actor_id)
            != (sender.tenant_id, sender.team_id, sender.principal_oid, sender.actor_id)):
        raise Problem(403, "AGENT_MESSAGE_IDENTITY_DENIED", "only the original verified sender may register this peer work")
    principal.require(sender.team_id, "submit")
    _current_parent(control.store.read_snapshot(_parent_keys(root.origin)), root.origin)
    if control.runtime._now() >= root.spec.deadline:
        raise Problem(403, "AGENT_MESSAGE_ROOT_EXPIRED", "the original peer work allowance has expired")
    return root


def narrow_profile(profile):
    from .behavior_assistant import AssistantProfile
    if profile.sdk_start_profile == "fa-sdk-start-gate-v2":
        raise Problem(403, "AGENT_MESSAGE_PROFILE_UNSUPPORTED", "a memory-specific SDK gate cannot become a peer work profile")
    return AssistantProfile.model_validate({
        **profile.model_dump(), "prompt_profile": "fa-instruction-v1", "require_operation": False,
        "worker_profile": None,
        "memory_scope": None, "capabilities": (), "skill_artifacts": (), "skill_catalog_budget_profile": None,
        "sop_copy": None, "text_attachment_profile": None, "user_text_file_profile": None, "turn_origin_profile": None,
    })
