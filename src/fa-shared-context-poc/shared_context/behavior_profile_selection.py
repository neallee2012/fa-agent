"""Human-selected new-conversation settings; original policies and conversations stay immutable."""

import hashlib
from pydantic import AwareDatetime, Field

from typing import Literal

from .behavior_agent_template import AgentTemplate, TemplateRole, TemplateSkill
from .behavior_assistant import AssistantProfile
from .behavior_operations import _Model, _parse, control_key
from .behavior_runtime import material
from .behavior_skill_prompt import assemble_prompt
from .behavior_store import ControlChanges, ControlRequest, ControlWrite
from .behavior_wire import Command, Digest, Identifier, WireModel
from .errors import Problem
from .source_materials import digest

READ_ONLY_PROFILES = {
    "fa-instruction-v1", "fa-instruction-v2", "fa-instruction-v3", "fa-instruction-v4", "fa-instruction-skills-v1",
    "fa-instruction-skill-purpose-v1", "fa-instruction-skill-resources-v1", "fa-instruction-user-files-v1",
    "fa-instruction-assistant-replies-v1", "fa-instruction-history-v1", "fa-instruction-agent-role-v1",
    "fa-instruction-agent-role-v2", "fa-instruction-agent-role-v3", "fa-instruction-agent-role-v4",
}
# The draft writer only creates an unapproved Team draft; it is still an explicit human selection.
SELECTABLE_PROFILES = frozenset({*READ_ONLY_PROFILES, "fa-instruction-history-skill-draft-v1"})


class SelectConversationProfile(Command):
    expected_selection_id: Identifier | None
    template: AgentTemplate


class UnselectedConversationProfile(WireModel):
    selection_id: None
    applies_to: Literal["new-conversations-only"]
    permissions_changed: Literal[False]


class ConversationProfileResult(WireModel):
    selection_id: Identifier
    applies_to: Literal["new-conversations-only"]
    template_sha256: Digest
    permissions_changed: Literal[False]
    existing_conversations_changed: Literal[False]


class SelectedConversationProfile(ConversationProfileResult):
    template: AgentTemplate


CurrentConversationProfile = UnselectedConversationProfile | SelectedConversationProfile


class ConversationProfileReceipt(WireModel):
    result: ConversationProfileResult
    control_request_id: Digest
    control_request_digest: Digest


class ConversationProfileSelection(_Model):
    kind: Literal["conversation_profile_selection"] = "conversation_profile_selection"
    selection_id: Identifier
    policy_sha256: Digest
    command: SelectConversationProfile
    selected_by: Identifier
    selected_at: AwareDatetime
    request: ControlRequest
    agent_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)


class ConversationProfileHead(_Model):
    kind: Literal["conversation_profile_head"] = "conversation_profile_head"
    selection_id: Identifier
    agent_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)


def head_key(domain, agent_id=None):
    return control_key("conversation-profile-head", domain, *((agent_id,) if agent_id is not None else ()))


def selection_key(domain, identifier, agent_id=None):
    return control_key("conversation-profile-selection", domain, *((agent_id,) if agent_id is not None else ()), identifier)


def profile_values_from_template(template):
    """Project approved settings without creating an execution profile or work identity."""
    if template.role.profile not in SELECTABLE_PROFILES or template.unavailable_skills:
        raise Problem(422, "PROFILE_SELECTION_UNSUPPORTED", "only compatible read-only instruction settings may be selected")
    try:
        role = assemble_prompt(template.role.profile).record
        expected = TemplateRole(profile=role.profile, prefix_sha256=role.prefix_sha256,
                                capability_catalog_version=role.capability_catalog_version)
        if template.role != expected:
            raise ValueError("versioned role differs")
        return {
            "prompt_profile": template.role.profile,
            "skill_artifacts": tuple(item.name for item in template.skills),
            "checkpoint_profile": template.settings.checkpoint_profile,
            "model_provider": template.settings.model_provider,
            "sdk_start_profile": template.settings.sdk_start_profile,
            "model_capacity_profile": template.settings.model_capacity_profile,
            "skill_catalog_budget_profile": template.settings.skill_catalog_budget_profile,
            "text_attachment_profile": template.settings.text_attachment_profile,
            "user_text_file_profile": template.settings.user_text_file_profile,
            "turn_origin_profile": template.settings.turn_origin_profile,
            "worker_profile": template.settings.worker_profile,
            "capabilities": template.settings.capabilities,
        }
    except (TypeError, ValueError):
        raise Problem(422, "PROFILE_SELECTION_UNSUPPORTED", "template settings do not form a compatible original-scope profile") from None


def profile_from_template(policy, template):
    values = profile_values_from_template(template)
    try:
        return AssistantProfile.model_validate_json(policy.profile.model_copy(update=values).model_dump_json())
    except (TypeError, ValueError):
        raise Problem(422, "PROFILE_SELECTION_UNSUPPORTED", "template settings do not form a compatible original-scope profile") from None


def read_selection(store, policy, identifier):
    from .behavior_work_policy import work_policy_key
    key = selection_key(policy.authority_domain, identifier, policy.agent_id)
    row = store.read_snapshot((key,))[key]
    if row is None:
        raise Problem(409, "PROFILE_SELECTION_MISSING", "the original approved profile selection is unavailable")
    value = _parse(row.payload["data"], ConversationProfileSelection)
    expected_request = ControlRequest(
        request_id=key, request_digest=digest(value.command.model_dump(mode="json")),
        read_set=(work_policy_key("policy", *policy.scope_ids), work_policy_key("revocation", *policy.scope_ids),
                  key, head_key(policy.authority_domain, policy.agent_id)),
    )
    if (value.selection_id != identifier or value.policy_sha256 != policy.sha256
            or value.selected_by != policy.template.human_oid or value.agent_id != policy.agent_id
            or value.selection_id != control_key("conversation-profile-id", *policy.scope_ids, value.command.request_id)
            or value.request != expected_request
            or row.payload["material"] != material(value.model_dump(mode="json")).model_dump(mode="json")):
        raise Problem(409, "PROFILE_SELECTION_INVALID", "stored profile selection differs from its original approved request")
    receipt = store.reconcile(value.request)
    from .source_materials import _payload_hash
    if dict(receipt.payload["writes"]).get(key) != _payload_hash(row.payload):
        raise Problem(409, "PROFILE_SELECTION_INVALID", "selection receipt does not cover the retained approval")
    profile_from_template(policy, value.command.template)
    return value


def current_selection(store, policy):
    key = head_key(policy.authority_domain, policy.agent_id)
    row = store.read_snapshot((key,))[key]
    if row is None:
        return None
    current = _parse(row.payload["data"], ConversationProfileHead)
    value = read_selection(store, policy, current.selection_id)
    if current.agent_id != policy.agent_id or row.payload["material"] is not None:
        raise Problem(409, "PROFILE_SELECTION_INVALID", "profile selection pointer differs")
    from .source_materials import _payload_hash
    receipt = store.reconcile(value.request)
    if dict(receipt.payload["writes"]).get(key) != _payload_hash(row.payload):
        raise Problem(409, "PROFILE_SELECTION_INVALID", "current selection is not the acknowledged head")
    return value


def choice_for_conversation(control, conversation):
    if conversation.jobs:
        previous = control._job_record(conversation.jobs[-1])
        if previous is None:
            control.refresh()
            previous = control._job_record(conversation.jobs[-1])
        if previous is None:
            raise Problem(409, "PROFILE_SELECTION_MISSING", "the original conversation profile is unavailable")
        if previous.profile_selection_id is None:
            return None
        return read_selection(control.store, control.policy, previous.profile_selection_id)
    return current_selection(control.store, control.policy)


def selection_result(value):
    return ConversationProfileResult(
        selection_id=value.selection_id, applies_to="new-conversations-only",
        template_sha256=hashlib.sha256(value.command.template.model_dump_json().encode("utf-8")).hexdigest(),
        permissions_changed=False, existing_conversations_changed=False,
    ).model_dump(mode="json")


def saved_selection_command(control, request_id):
    """The command this request ID already saved, or None; a resumed caller resends it instead of recomputing."""
    policy = control.installed()
    identifier = control_key("conversation-profile-id", *policy.scope_ids, request_id)
    key = selection_key(policy.authority_domain, identifier, policy.agent_id)
    if control.store.read_snapshot((key,))[key] is None:
        return None
    return read_selection(control.store, policy, identifier).command


def select_profile(control, authority, principal, command, *, reconcile=False):
    control._human(principal, "read")
    if not reconcile:
        control._human(principal, "approve")
    if authority is None or authority.runtime is not control.runtime:
        raise Problem(503, "PROFILE_SELECTION_DISABLED", "the original assistant authority is required")
    policy = control.installed()
    if not policy.active or not policy.read_enabled:
        raise Problem(403, "WORK_POLICY_REVOKED", "current standing read and work scope is required")
    identifier = control_key("conversation-profile-id", *policy.scope_ids, command.request_id)
    key, current_key = selection_key(policy.authority_domain, identifier, policy.agent_id), head_key(policy.authority_domain, policy.agent_id)
    rows = control.store.read_snapshot((key, current_key))
    if rows[key] is not None:
        original = read_selection(control.store, policy, identifier)
        if original.command != command:
            raise Problem(409, "PROFILE_SELECTION_CONFLICT", "the original selection request has different content")
        return control.store.reconcile(original.request)
    if reconcile:
        raise Problem(503, "PROFILE_SELECTION_UNKNOWN", "original profile selection is not confirmed; do not resend with a new ID")
    if rows[current_key] is not None:
        current_selection(control.store, policy)
    profile = profile_from_template(policy, command.template)
    from .behavior_capabilities import check_selectable, runtime_grants
    check_selectable(profile.capabilities, profile.prompt_profile, runtime_grants(control.runtime))
    if profile.skill_catalog_budget_profile is not None:
        from .behavior_skill_catalog import validate_catalog_budget
        validate_catalog_budget(control.models.select(None).capacity_snapshot(),
                                len(profile.skill_artifacts), control.runtime.dep.memory._count)
    def current_catalog():
        actor = authority.actors.get(policy.template.agent_oid)
        if (actor is None or actor.oid != policy.template.agent_oid or actor.team != policy.template.team_id
                or authority.artifacts.context.auth.tenant_id != policy.template.tenant_id):
            raise Problem(403, "PROFILE_SELECTION_DENIED", "the original Team agent identity is required")
        return authority.skills.catalog(actor, policy.template.team_id, profile.skill_artifacts,
                                         schema_version="fa-approved-skill-catalog-v2")
    catalog = current_catalog()
    references = tuple(TemplateSkill(name=item.name, version_id=item.version_id, sha256=item.sha256,
                                    metadata_sha256=item.metadata_sha256) for item in catalog.entries)
    if references != command.template.skills or catalog.unavailable_count:
        raise Problem(409, "PROFILE_SELECTION_SOURCES_CHANGED", "template references do not match current approved Team skills")
    request = ControlRequest(request_id=key, request_digest=digest(command.model_dump(mode="json")),
                             read_set=(control.policy_key, control.revocation_key, key, current_key))
    def decide(current):
        control._human(principal, "read")
        control._human(principal, "approve")
        active = control.installed(current)
        if not active.active or not active.read_enabled:
            raise Problem(403, "WORK_POLICY_REVOKED", "current standing scope was revoked")
        prior = None if current[current_key] is None else _parse(current[current_key].payload["data"], ConversationProfileHead)
        if (None if prior is None else prior.selection_id) != command.expected_selection_id:
            raise Problem(409, "PROFILE_SELECTION_CONFLICT", "the current profile selection changed; read before selecting")
        if current_catalog() != catalog:
            raise Problem(409, "PROFILE_SELECTION_SOURCES_CHANGED", "skill references changed during profile selection")
        value = ConversationProfileSelection(selection_id=identifier, policy_sha256=policy.sha256,
            command=command, selected_by=principal.oid, selected_at=control.runtime._now(), request=request,
            agent_id=policy.agent_id)
        head = ConversationProfileHead(selection_id=identifier, agent_id=policy.agent_id)
        return ControlChanges(writes=(
            ControlWrite(row_id=key, mode="create", data=value.model_dump(mode="json"), material=material(value.model_dump(mode="json"))),
            ControlWrite(row_id=current_key, mode="create" if prior is None else "replace",
                         data=head.model_dump(mode="json")),
        ), result=selection_result(value))
    return control.store.transact(request, decide)
