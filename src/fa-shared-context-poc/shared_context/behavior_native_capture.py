"""N2: the original server settings and source readers, without registering a work."""

import os

from .behavior_agent_role_prompt_versions import captured_role_prompt
from .behavior_agent_template import AgentTemplate, TemplateSkill, read_profile_template, with_native_default_checkpoint
from .behavior_assistant_wire import InstructionSubmit
from .behavior_attachment_source import read_scoped_sources
from .behavior_attachments import AttachedTexts
from .behavior_capabilities import capture, runtime_grants, turn_grants
from .behavior_capability_prompt import skill_access
from .behavior_code_identity import code_sha256
from .behavior_native_turn_wire import (
    NativeAgentCommand, NativeConversation, NativeSubmit, NativeTurnCapture, TeamMemoryCapture, team_memory_scope,
)
from .behavior_profile_selection import (
    current_selection, profile_values_from_template, read_selection,
)
from .behavior_skill_catalog import validate_catalog_budget
from .errors import Problem
from .source_materials import AuthenticatedSourceContext
from .team_connector_narrowing import resolve_connectors

TEAM_MEMORY_ENV = "FA_TEAM_MEMORY_ENABLED"


def team_memory_capture(tenant_id, team_id, *, environ=None):
    """TN-1, off unless exactly "1": the server-bound Team's memory scope; never from the model, body or UI."""
    environ = os.environ if environ is None else environ
    if environ.get(TEAM_MEMORY_ENV) != "1":
        return None
    return TeamMemoryCapture(scope=team_memory_scope(str(tenant_id), str(team_id)))


def project_capture(template, selection_id, model, *, head_refs=(), head_selection_id=None,
                    available, role_prompt=None, attached_text=None, count_tokens, team_memory=None, connectors=None):
    """Pure projection of already-read inputs; the full original template is never narrowed."""
    template = AgentTemplate.model_validate_json(template.model_dump_json())
    profile_values_from_template(template)
    settings = template.settings
    grants = capture(settings.capabilities, head_refs, head_selection_id, available)
    capacity = None if settings.model_capacity_profile is None else model.capacity_snapshot()
    if settings.skill_catalog_budget_profile is not None and skill_access(template.role.profile, grants):
        if capacity is None:
            raise Problem(503, "SKILL_CATALOG_CAPACITY_UNKNOWN", "catalog budgeting requires declared model capacity")
        validate_catalog_budget(capacity, len(template.skills), count_tokens)
    if grants is not None and grants.vision and model.supports_image_input is not True:
        raise Problem(422, "CAPABILITY_MODEL_UNSUPPORTED", "a selected capability needs a model that accepts images")
    try:
        return NativeTurnCapture(
            template=template, profile_selection_id=selection_id, model=model, model_capacity=capacity,
            reasoning_summary=("detailed" if settings.model_provider == "openai-responses"
                               and model.supports_reasoning_summary is True else None),
            role_prompt=role_prompt, capabilities=grants, attached_text=attached_text,
            sdk_start_code_sha256=code_sha256() if settings.sdk_start_profile is not None else None,
            team_memory=team_memory, connectors=connectors,
        )
    except ValueError:
        raise Problem(422, "NATIVE_PROFILE_UNSUPPORTED", "the original settings do not form a supported native capture") from None


def start_pinned(capture, runtime, target):
    """ADR-0009 2d-i: the SDK start pin names the image that will run ``capture`` (``target``'s version's
    ``fa_build``), not this API's build. Every Hosted image refuses a pin other than its own ``code_sha256()``
    (``check_start_capture``), and G serves older images until their turn ends. Only ``submit_native_turn`` calls
    it, for an RID no receipt holds: a saved intent never names the pin, a receipt keeps its own. An unknown build
    keeps this API's pin."""
    if capture.sdk_start_code_sha256 is None:
        return capture
    from .behavior_agent_versions import hosted_build
    return pinned_to(capture, hosted_build(runtime, target))


def pinned_to(capture, build):
    """``capture`` with the SDK start pin ``build``; unchanged without a selected gate or a known build."""
    if capture.sdk_start_code_sha256 is None or build is None or build == capture.sdk_start_code_sha256:
        return capture
    return capture.model_copy(update={"sdk_start_code_sha256": build})


def _skills(authority, policy, template):
    if not template.skills:
        return
    binding = policy.template
    actor = authority.actors.get(binding.agent_oid)
    if (actor is None or actor.team != binding.team_id or actor.oid != binding.agent_oid
            or authority.artifacts.context.auth.tenant_id != binding.tenant_id):
        raise Problem(403, "SKILL_IDENTITY_DENIED", "the original server-bound Team identity is required")
    catalog = authority.skills.catalog(
        actor, binding.team_id, tuple(item.name for item in template.skills),
        schema_version="fa-approved-skill-catalog-v2",
    )
    refs = tuple(TemplateSkill(name=item.name, version_id=item.version_id, sha256=item.sha256,
                              metadata_sha256=item.metadata_sha256) for item in catalog.entries)
    if refs != template.skills:
        raise Problem(409, "PROFILE_SELECTION_SOURCES_CHANGED", "the original selected Team skills changed")


def resolve_capture(control, authority, principal, command, *, conversation=None):
    """Read current owner/settings/sources once. No conversation, job, ledger or receipt writes.

    Of ``conversation`` only its ID and capture are read; the API submit prefetch relies on that to reuse a result."""
    command = InstructionSubmit.model_validate_json(command.model_dump_json())
    return _resolve_capture(control, authority, principal, command, conversation=conversation,
                            require_submit=lambda: control._human(principal, "submit"))


def resolve_agent_capture(control, authority, principal, recipient, command, *, conversation=None):
    """A verified Agent resolves the receiver's settings, without gaining the owner's approval identity."""
    from .behavior_team_registry import same_team_recipient

    command = NativeAgentCommand.model_validate_json(command.model_dump_json())

    def require_submit():
        principal.require(recipient.team_id, "submit")
        if (principal.kind != "agent" or principal.tenant_id != str(recipient.tenant_id)
                or (recipient.target.agent_id != control.policy.agent_id if control.policy.agent_id is not None
                    else not recipient.legacy_keys)
                or str(recipient.identity.oid) != control.policy.template.agent_oid
                or recipient.team_id != control.policy.template.team_id):
            raise Problem(403, "NATIVE_AGENT_SOURCE_INVALID", "the verified Agent and recipient settings must match")
        same_team_recipient(recipient, sender_oid=principal.oid, team_id=principal.team)

    return _resolve_capture(control, authority, principal, command, conversation=conversation,
                            require_submit=require_submit)


def resolve_routine_capture(control, authority, command, *, conversation, authorize):
    """The checked producer/definition callback replaces owner identity, not the original capture rules."""
    command = NativeAgentCommand.model_validate_json(command.model_dump_json())
    if command.attachments or command.files:
        raise Problem(422, "NATIVE_ROUTINE_INVALID", "routine input uses its approved text and trigger data")
    return _resolve_capture(control, authority, None, command, conversation=conversation,
                            require_submit=authorize)


def resolve_continuation_capture(control, authority, principal, original, command, conversation):
    """Recheck this Agent's original capture; recovery is not a new owner instruction."""
    def require_submit():
        principal.require(control.policy.template.team_id, "submit")
        if (principal.kind != "agent" or principal.oid != control.policy.template.agent_oid
                or principal.tenant_id != control.policy.template.tenant_id
                or original.agent_id != control.policy.agent_id and control.policy.agent_id is not None):
            raise Problem(403, "NATIVE_AGENT_SOURCE_INVALID", "only the original Agent may continue its request")

    # Already-resolved immutable attachment text remains original data, not a new Agent source request.
    reading = command.model_copy(update={"attachments": (), "model": original.capture.model.id})
    resolved = _resolve_capture(control, authority, principal, reading, conversation=conversation,
                                require_submit=require_submit)
    old = () if original.capture.capabilities is None else original.capture.capabilities.grants
    current = () if resolved.capabilities is None else resolved.capabilities.grants
    if any(grant not in current for grant in old):
        raise Problem(403, "NATIVE_CONTINUATION_CAPTURE_REVOKED", "an original capability is no longer available")
    capabilities = resolved.capabilities
    if capabilities is not None:
        capabilities = capabilities.model_copy(update={"grants": tuple(grant for grant in current if grant in old)})
    old = () if original.capture.connectors is None else original.capture.connectors.grants
    kept = tuple(grant for grant in (() if resolved.connectors is None else resolved.connectors.grants) if grant in old)
    if len(kept) != len(old):
        raise Problem(403, "NATIVE_CONTINUATION_CAPTURE_REVOKED", "an original Team connector is no longer available")
    return NativeTurnCapture.model_validate_json(resolved.model_copy(update={
        "capabilities": capabilities, "attached_text": original.capture.attached_text,
        "connectors": resolved.connectors.model_copy(update={"grants": kept}) if kept else None,
    }).model_dump_json())


def new_conversation_template(authority, policy, head):
    """The template a new native conversation captures: the human selection, else the server profile."""
    if head is not None:
        return head.command.template
    from .behavior_work_policy import configured_prompt_override
    prompt = policy.profile.prompt_profile or configured_prompt_override(policy)
    if prompt is None:
        raise Problem(409, "AGENT_TEMPLATE_PROFILE_UNSUPPORTED", "native capture requires a versioned profile")
    # No human selection: the native default fills only a missing checkpoint choice.
    return with_native_default_checkpoint(read_profile_template(authority, policy, policy.profile, prompt))


def system_prompt_inputs(control, authority, principal):
    """Template, capability grants and role text a new native conversation would capture now; reads only."""
    control._human(principal)
    if authority is None or authority.runtime is not control.runtime:
        raise Problem(503, "AGENT_TEMPLATE_DISABLED", "the original settings and source readers are required")

    def read():
        policy = control.installed()
        if policy != control.policy or not policy.active or not policy.read_enabled:
            raise Problem(403, "WORK_POLICY_REVOKED", "current standing read authority is required")
        head = current_selection(control.store, policy)
        template = new_conversation_template(authority, policy, head)
        grants = capture(
            template.settings.capabilities, () if head is None else head.command.template.settings.capabilities,
            None if head is None else head.selection_id, runtime_grants(control.runtime))
        connectors = resolve_connectors(control, policy, template, None)
        grants = turn_grants(grants, () if connectors is None else connectors.grants)
        role = captured_role_prompt(control.store, policy, template.role.profile)
        return template, None if head is None else head.selection_id, grants, role

    from .behavior_assistant import _read_conflicts
    return _read_conflicts(lambda: control.store.consistent_read(read), phase="system_prompt")


def _resolve_capture(control, authority, principal, command, *, conversation, require_submit):
    require_submit()
    if authority is None or authority.runtime is not control.runtime:
        raise Problem(503, "AGENT_TEMPLATE_DISABLED", "the original settings and source readers are required")
    if conversation is not None:
        conversation = NativeConversation.model_validate_json(conversation.model_dump_json())
        if command.conversation_id != conversation.conversation_id:
            raise Problem(403, "NATIVE_STATE_SCOPE", "the original native conversation must match")
    elif command.conversation_id is not None:
        raise Problem(409, "NATIVE_CONVERSATION_MISSING", "read the original native conversation before resolving settings")

    def read():
        require_submit()
        policy = control.installed()
        if policy != control.policy or not policy.active or not policy.read_enabled:
            raise Problem(403, "WORK_POLICY_REVOKED", "current original read and submission rights are required")
        head = current_selection(control.store, policy)
        if conversation is None:
            selection_id = None if head is None else head.selection_id
            template = new_conversation_template(authority, policy, head)
        else:
            selection_id, template = conversation.capture.profile_selection_id, conversation.capture.template
            if selection_id is not None:
                original = read_selection(control.store, policy, selection_id)
                if original.command.template != template:
                    raise Problem(409, "NATIVE_CAPTURE_CHANGED", "the original conversation selection differs")
        _skills(authority, policy, template)
        settings = template.settings
        if command.attachments and settings.text_attachment_profile is None:
            raise Problem(403, "ATTACHMENTS_DISABLED", "the original settings do not enable source attachments")
        if command.files and settings.user_text_file_profile is None:
            raise Problem(403, "USER_TEXT_FILES_DISABLED", "the original settings do not enable literal files")
        attached = None
        if command.attachments:
            binding, source_policy = policy.template, policy.legacy_template_policy
            context = AuthenticatedSourceContext(
                binding.tenant_id, binding.team_id, principal.source_actor_id,
                binding.ingress_id, source_policy.allowed_purposes,
            )
            entries, _ = read_scoped_sources(
                control.runtime.dep.sources, control.store, context, command.attachments,
                source_visibility=source_policy.source_visibility, source_ids=binding.source_ids,
                validate_output=lambda exact: control.runtime._validate_source_output(exact, source_policy),
            )
            attached = AttachedTexts(entries=tuple(entries))
        value = project_capture(
            template, selection_id, control.models.select(command.model),
            head_refs=() if head is None else head.command.template.settings.capabilities,
            head_selection_id=None if head is None else head.selection_id,
            available=runtime_grants(control.runtime),
            role_prompt=captured_role_prompt(control.store, policy, template.role.profile),
            attached_text=attached, count_tokens=control.runtime.dep.memory._count,
            team_memory=team_memory_capture(policy.template.tenant_id, policy.template.team_id),
            connectors=resolve_connectors(control, policy, template, None if conversation is None else conversation.capture),
        )
        require_submit()
        return NativeSubmit(command=command, capture=value).capture if isinstance(command, InstructionSubmit) else value

    return control.store.consistent_read(read)
