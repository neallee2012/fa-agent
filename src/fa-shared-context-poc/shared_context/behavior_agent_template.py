"""Portable configuration references, with no identity, grants or private content."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .behavior_model_capacity import ModelCapacityProfile
from .behavior_sdk_state import CheckpointProfile, ModelProvider, native_default_checkpoint, validate_provider_checkpoint
from .behavior_skill_prompt import InstructionPromptProfile, assemble_prompt
from .behavior_skill_catalog import CatalogBudgetProfile
from .behavior_attachments import TextAttachmentProfile
from .behavior_capabilities import CapabilityRef, validate_refs
from .behavior_text_files import UserTextFileProfile
from .behavior_turn_origin import TurnOriginProfile
from .behavior_wire import Digest, Identifier, SdkStartProfile, WireModel, WorkerProfile
from .errors import Problem
from .m3_models import VersionId
from .m3_skills import PurposeSkillCatalog, SkillSelection, validate_selection
from .native_responses_wire import NATIVE_STATE_PROFILE, NativeStateProfile

if TYPE_CHECKING:
    from .behavior_assistant import AssistantAuthority
    from .behavior_auth import BehaviorPrincipal
    from .behavior_work_policy import WorkPolicyControl

MAX_TEMPLATE_BYTES = 8192
DestinationRequirements = tuple[
    Literal["current-human-agent-team-binding"],
    Literal["current-standing-read-and-work-policy"],
    Literal["reviewed-work-sources-and-private-memory"],
    Literal["model-deployment-and-provider-connection"],
    Literal["connector-accounts-and-tool-grants"],
    Literal["compatible-sdk-runtime-and-checkpoint"],
]
DESTINATION_REQUIREMENTS: DestinationRequirements = (
    "current-human-agent-team-binding",
    "current-standing-read-and-work-policy",
    "reviewed-work-sources-and-private-memory",
    "model-deployment-and-provider-connection",
    "connector-accounts-and-tool-grants",
    "compatible-sdk-runtime-and-checkpoint",
)


class TemplateRole(WireModel):
    profile: InstructionPromptProfile
    prefix_sha256: Digest
    capability_catalog_version: Identifier


class TemplateSettings(WireModel):
    sdk_version: Literal["1.0.13"] = "1.0.13"
    runtime_version: Literal["1.0.83"] = "1.0.83"
    mode: Literal["empty"] = "empty"
    checkpoint_profile: CheckpointProfile | None = None
    model_provider: ModelProvider | None = Field(default=None, exclude_if=lambda value: value is None)
    sdk_start_profile: SdkStartProfile | None = None
    model_capacity_profile: ModelCapacityProfile | None = None
    skill_catalog_budget_profile: CatalogBudgetProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    text_attachment_profile: TextAttachmentProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    user_text_file_profile: UserTextFileProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    turn_origin_profile: TurnOriginProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    worker_profile: WorkerProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    capabilities: tuple[CapabilityRef, ...] = Field(default=(), exclude_if=lambda value: not value)
    state_profile: NativeStateProfile = Field(
        default=NATIVE_STATE_PROFILE, exclude_if=lambda value: value == NATIVE_STATE_PROFILE)

    _capabilities = field_validator("capabilities")(validate_refs)

    @model_validator(mode="after")
    def original_provider(self):
        validate_provider_checkpoint(self.model_provider, self.checkpoint_profile)
        return self


class TemplateSkill(WireModel):
    name: Identifier
    version_id: VersionId
    sha256: Digest
    metadata_sha256: Digest | None = None


class AgentTemplate(WireModel):
    schema_version: Literal["fa-agent-template-v1"] = "fa-agent-template-v1"
    role: TemplateRole
    settings: TemplateSettings
    skills: Annotated[tuple[TemplateSkill, ...], Field(max_length=4)] = ()
    unavailable_skills: SkillSelection = ()
    required_at_destination: DestinationRequirements = DESTINATION_REQUIREMENTS
    authorization: Literal[False] = False

    @model_validator(mode="after")
    def bounded_references(self):
        names = tuple(item.name for item in self.skills)
        validate_selection((*names, *self.unavailable_skills))
        if names != tuple(sorted(names)) or self.unavailable_skills != tuple(sorted(self.unavailable_skills)):
            raise ValueError("Template skill references must be ordered and distinct")
        if len(self.model_dump_json().encode("utf-8")) > MAX_TEMPLATE_BYTES:
            raise ValueError("Complete template exceeds its byte bound")
        return self


class TemplateSkillInspection(WireModel):
    name: Identifier
    status: Literal["matches", "unavailable", "version_changed", "unresolved_reference"]


class TemplateInspection(WireModel):
    schema_version: Literal["fa-agent-template-inspection-v1"] = "fa-agent-template-inspection-v1"
    template_sha256: Digest
    role_matches: bool
    settings_match: bool
    selection_matches: bool
    skills: Annotated[tuple[TemplateSkillInspection, ...], Field(max_length=4)]
    references_match: bool
    required_at_destination: DestinationRequirements = DESTINATION_REQUIREMENTS
    authorization: Literal[False] = False
    activated: Literal[False] = False


def export_template(
    control: WorkPolicyControl, authority: AssistantAuthority, principal: BehaviorPrincipal,
) -> AgentTemplate:
    control._human(principal)
    if authority.runtime is not control.runtime:
        raise Problem(503, "AGENT_TEMPLATE_DISABLED", "the configured assistant authority is required")

    def read():
        policy = control.installed()
        if not policy.active or not policy.read_enabled:
            raise Problem(403, "WORK_POLICY_REVOKED", "current standing read authority is required")
        if policy.sha256 != control.policy.sha256:
            raise Problem(403, "WORK_POLICY_SCOPE_CHANGED", "the configured standing policy differs")
        profile = policy.profile
        from .behavior_profile_selection import current_selection, profile_from_template
        selection = current_selection(control.store, policy)
        if selection is not None:
            profile = profile_from_template(policy, selection.command.template)
        prompt_profile = profile.prompt_profile
        if prompt_profile is None:
            from .behavior_work_policy import configured_prompt_override
            prompt_profile = configured_prompt_override(policy)
        if prompt_profile is None:
            raise Problem(409, "AGENT_TEMPLATE_PROFILE_UNSUPPORTED", "export requires an explicit versioned instruction profile")
        return read_profile_template(
            authority, policy, profile, prompt_profile,
            state_profile=NATIVE_STATE_PROFILE if selection is None else selection.command.template.settings.state_profile)

    from .behavior_assistant import _read_conflicts
    return _read_conflicts(
        lambda: control.store.consistent_read(read), phase="agent_template",
    )


def with_native_default_checkpoint(template):
    """A new native conversation's template: auto-no-custom-v1 only when no checkpoint profile was chosen."""
    settings = template.settings
    default = (None if settings.checkpoint_profile is not None
               else native_default_checkpoint(settings.model_provider, settings.worker_profile))
    if default is None:
        return template
    values = template.model_dump(mode="json")
    values["settings"]["checkpoint_profile"] = default.model_dump(mode="json")
    return AgentTemplate.model_validate_json(json.dumps(values))


def read_profile_template(authority, policy, profile, prompt_profile, *, state_profile=NATIVE_STATE_PROFILE):
    """The caller owns the read scope; this reader neither opens one nor derives work."""
    binding = policy.template
    actor = authority.actors.get(binding.agent_oid)
    if (actor is None or actor.oid != binding.agent_oid or actor.team != binding.team_id
            or authority.artifacts.context.auth.tenant_id != binding.tenant_id):
        raise Problem(403, "SKILL_IDENTITY_DENIED", "the original server-bound artifact identity is required")
    catalog = authority.skills.catalog(
        actor, binding.team_id, profile.skill_artifacts, schema_version="fa-approved-skill-catalog-v2",
    )
    role = assemble_prompt(prompt_profile).record
    if role is None or not isinstance(catalog, PurposeSkillCatalog):
        raise Problem(503, "AGENT_TEMPLATE_DISABLED", "versioned role and skill references are required")
    skills = tuple(TemplateSkill(name=item.name, version_id=item.version_id, sha256=item.sha256,
                                metadata_sha256=item.metadata_sha256) for item in catalog.entries)
    exported = AgentTemplate(
        role=TemplateRole(profile=role.profile, prefix_sha256=role.prefix_sha256,
                          capability_catalog_version=role.capability_catalog_version),
        settings=TemplateSettings(
            checkpoint_profile=profile.checkpoint_profile, model_provider=profile.model_provider,
            sdk_start_profile=profile.sdk_start_profile, model_capacity_profile=profile.model_capacity_profile,
            skill_catalog_budget_profile=profile.skill_catalog_budget_profile,
            text_attachment_profile=profile.text_attachment_profile,
            user_text_file_profile=profile.user_text_file_profile, turn_origin_profile=profile.turn_origin_profile,
            worker_profile=profile.worker_profile, capabilities=profile.capabilities,
            state_profile=state_profile,
        ),
        skills=skills,
        unavailable_skills=tuple(sorted(set(profile.skill_artifacts) - {item.name for item in skills})),
    )
    # M3 rows are outside the control-store transaction; only references are exported.
    if catalog != authority.skills.catalog(
        actor, binding.team_id, profile.skill_artifacts, schema_version="fa-approved-skill-catalog-v2",
    ):
        raise Problem(409, "AGENT_TEMPLATE_SOURCE_CHANGED", "skill sources changed while preparing the export")
    return exported


def inspect_template(imported: AgentTemplate, current: AgentTemplate) -> TemplateInspection:
    available = {item.name: item for item in current.skills}
    results = []
    for item in imported.skills:
        selected = available.get(item.name)
        status = "unavailable" if selected is None else "matches" if selected == item else "version_changed"
        results.append(TemplateSkillInspection(name=item.name, status=status))
    results.extend(TemplateSkillInspection(name=name, status="unresolved_reference") for name in imported.unavailable_skills)
    selected_names = {item.name for item in imported.skills} | set(imported.unavailable_skills)
    current_names = set(available) | set(current.unavailable_skills)
    role_matches, settings_match = imported.role == current.role, imported.settings == current.settings
    selection_matches = selected_names == current_names
    return TemplateInspection(
        template_sha256=hashlib.sha256(imported.model_dump_json().encode("utf-8")).hexdigest(),
        role_matches=role_matches, settings_match=settings_match, selection_matches=selection_matches,
        skills=tuple(sorted(results, key=lambda item: item.name)),
        references_match=(role_matches and settings_match and selection_matches
                          and all(item.status == "matches" for item in results)),
    )


MAX_TEMPLATE_OPTION_SKILLS = 24
MAX_TEMPLATE_OPTION_PAGES = 16
# Up to 24 skill refs plus every current grant (a full catalog is ~70 KB); the API response cap.
MAX_TEMPLATE_OPTIONS_BYTES = 128 * 1024


class TemplateCapabilityOption(WireModel):
    entry_id: Identifier
    entry_sha256: Digest
    kind: Literal["mcp-api", "mcp-hosted", "mcp-toolbox", "fa-builtin"]
    tools: tuple[str, ...]
    vision: bool


class TemplateResponsesOptions(WireModel):
    enabled: bool = False
    history_source: Literal["conversation", "response"] | None = None


class TemplateOptions(WireModel):
    """What a human may add to a template now; references only, never a selection or permission."""

    schema_version: Literal["fa-agent-template-options-v1"] = "fa-agent-template-options-v1"
    skills: Annotated[tuple[TemplateSkill, ...], Field(max_length=MAX_TEMPLATE_OPTION_SKILLS)]
    more_skills: bool
    capabilities: tuple[TemplateCapabilityOption, ...]
    composable_profiles: tuple[str, ...]
    responses: TemplateResponsesOptions = Field(default_factory=TemplateResponsesOptions)
    authorization: Literal[False] = False
    activated: Literal[False] = False


def template_options(
    control: WorkPolicyControl, authority: AssistantAuthority, principal: BehaviorPrincipal,
) -> TemplateOptions:
    """Currently effective Team skills (the exact references selection verifies) and available capabilities."""
    from .behavior_capabilities import CAPABILITY_BASE_PROFILES, runtime_grants
    control._human(principal)
    if authority.runtime is not control.runtime:
        raise Problem(503, "AGENT_TEMPLATE_DISABLED", "the configured assistant authority is required")
    policy = control.installed()
    if not policy.active or not policy.read_enabled:
        raise Problem(403, "WORK_POLICY_REVOKED", "current standing read authority is required")
    binding = policy.template
    actor = authority.actors.get(binding.agent_oid)
    if (actor is None or actor.oid != binding.agent_oid or actor.team != binding.team_id
            or authority.artifacts.context.auth.tenant_id != binding.tenant_id):
        raise Problem(403, "SKILL_IDENTITY_DENIED", "the original server-bound artifact identity is required")
    names, after, more = [], None, False
    for _ in range(MAX_TEMPLATE_OPTION_PAGES):
        page = authority.artifacts.skill_reviews(actor, binding.team_id, after)
        names.extend(entry.artifact_id for entry in page.entries if entry.status == "effective")
        if len(names) > MAX_TEMPLATE_OPTION_SKILLS or page.next_after is None:
            break
        after = page.next_after
    else:
        more = True
    if len(names) > MAX_TEMPLATE_OPTION_SKILLS:
        names, more = names[:MAX_TEMPLATE_OPTION_SKILLS], True
    skills = []
    # Listing references must not combine unrelated purposes into one selection's 4 KiB budget.
    for name in names:
        catalog = authority.skills.catalog(actor, binding.team_id, (name,),
                                           schema_version="fa-approved-skill-catalog-v2")
        skills.extend(TemplateSkill(name=item.name, version_id=item.version_id, sha256=item.sha256,
                                    metadata_sha256=item.metadata_sha256) for item in catalog.entries)
    capabilities = tuple(TemplateCapabilityOption(
        entry_id=grant.entry_id, entry_sha256=grant.entry_sha256, kind=grant.kind, tools=grant.tools,
        vision=grant.vision) for grant in sorted(runtime_grants(control.runtime).values(), key=lambda g: g.entry_id))
    return TemplateOptions(skills=tuple(sorted(skills, key=lambda item: item.name)), more_skills=more,
                           capabilities=capabilities, composable_profiles=tuple(sorted(CAPABILITY_BASE_PROFILES)))
