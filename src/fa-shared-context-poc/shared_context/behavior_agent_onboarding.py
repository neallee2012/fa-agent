"""CAP-06 onboarding: an active Team owner's confirmation creates one Hosted agent and registers it.

Every step is keyed by the original request ID and resumable by POSTing the same body again; no background
thread. Admission reserves one Team slot in one CAS; only an explicit Foundry failure confirmed by a read-back
releases it. An unknown outcome keeps the slot and is continued with the same request ID.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import time
import unicodedata
import uuid
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .behavior_agent_message_wire import MAX_TEAM_AGENTS
from .behavior_agent_role_prompt import AGENT_ROLE_V4_PROFILE, ROLE_PROFILES
from .behavior_agent_role_prompt_versions import role_prompt_command, save_role_prompt
from .behavior_agent_template import TemplateSkill
from .behavior_capabilities import CapabilityRefs, SKILLS_ENTRY_ID, check_selectable, runtime_grants, skills_selected
from .behavior_foundry import PLATFORM_NO_RETRY, PLATFORM_READ_RETRY, FoundryTarget, _upstream_status
from .behavior_operations import _Model, _parse, _row, _write, control_key
from .behavior_skill_catalog import CatalogBudgetProfile
from .behavior_store import ControlChanges, ControlRequest
from .behavior_team_authority import TeamAuthority, team_authority_key
from .behavior_team_registry import (
    IdentityRegistration, Oid, TeamAgent, TeamAgentRetired, TeamAgents, _release_request, _release_result,
    identity_key, roster_key,
)
from .behavior_turn_origin import TurnOriginProfile
from .behavior_wire import Command, Digest
from .errors import Problem
from .models import Identifier
from .source_materials import digest

# B1 SP series: Agents created from now on use the shared native prompt; existing Agents keep their selection.
NEW_AGENT_PROFILE = AGENT_ROLE_V4_PROFILE


States = Literal["admitted", "creating", "registered", "ready", "failed"]
_CHAIN = ("admitted", "creating", "registered", "ready")
_PROBE_ENV = (
    "FA_MODEL_DIRECT_PROBE_URL", "FA_MODEL_DIRECT_PROBE_MODEL", "FA_N0_STATE_PROBE",
    "FA_N6_RESPONSES_PROBE", "FA_N6_RESPONSES_INTERRUPT_SESSION_ID",
    # Nothing reads it since C3; listed so a dev Agent that ran the P0 consent probe drops it on its next sync.
    "FA_TOOLBOX_CONSENT_PROBE",
)
# Selection refusals that S0 returns as-is (nothing written) but S5 must wrap (the agent already exists).
_SELECTION_CODES = frozenset({
    "AGENT_TEMPLATE_DISABLED", "CAPABILITY_SELECTION_INVALID", "CAPABILITY_BASE_UNSUPPORTED",
    "CAPABILITY_SOURCES_CHANGED", "PROFILE_SELECTION_SOURCES_CHANGED", "PROFILE_SELECTION_DENIED",
    "PROFILE_SELECTION_UNSUPPORTED", "PROFILE_SELECTION_DISABLED", "SKILL_CATALOG_BUDGET_TOO_SMALL",
    "AGENT_ONBOARDING_PROFILE_UNSUPPORTED"})
_METADATA_RID, _METADATA_TEAM = "fa_onboarding_request_id", "fa_team_id"
_REASONS = Literal["FOUNDRY_CREATE_REJECTED", "FOUNDRY_VERSION_FAILED"]
# Reply-only (never stored): the version is active but its endpoint protocols or project roles are not confirmed yet.
SETUP_PENDING = "AGENT_SETUP_PENDING"


SelectedSkills = Annotated[tuple[TemplateSkill, ...], Field(max_length=4)]


class AgentSelection(_Model):
    """The owner's exact Team-approved choice; every continuation uses this original, never a re-read."""

    capabilities: CapabilityRefs
    skills: SelectedSkills


class CreateTeamAgent(Command):
    display_name: Annotated[str, Field(min_length=1, max_length=64)]
    # Bounded by the route body limit; role_prompt_command refuses oversized text with 422 and max_bytes.
    role_prompt: str
    # Both omitted (an earlier client) keeps the original copy-A behavior and the same request digest.
    capabilities: CapabilityRefs | None = Field(default=None, exclude_if=lambda value: value is None)
    skills: SelectedSkills | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("display_name")
    @classmethod
    def visible_name(cls, value):
        if not value.strip() or value != value.strip() or any(unicodedata.category(c).startswith("C") for c in value):
            raise ValueError("a visible display name without control characters is required")
        return value

    @model_validator(mode="after")
    def whole_selection(self):
        if (self.capabilities is None) != (self.skills is None):
            raise ValueError("capabilities and skills are selected together")
        return self

    @property
    def selection(self):
        if self.capabilities is None:
            return None
        return AgentSelection(capabilities=self.capabilities, skills=self.skills)


class TeamAgentOnboarding(_Model):
    kind: Literal["team_agent_onboarding"] = "team_agent_onboarding"
    tenant_id: Oid
    team_id: Identifier
    request_id: Identifier
    owner_oid: Oid
    request_sha256: Digest
    display_name: Annotated[str, Field(min_length=1, max_length=64)]
    role_prompt_sha256: Digest
    agent_name: Identifier
    state: States
    target: FoundryTarget | None = Field(default=None, exclude_if=lambda value: value is None)
    reason: _REASONS | None = Field(default=None, exclude_if=lambda value: value is None)
    # Absent on onboardings admitted before owners could choose; those keep copying A.
    selection: AgentSelection | None = Field(default=None, exclude_if=lambda value: value is None)
    # A's turn origin as its new conversations used it at admission; absent keeps the copied standing value.
    turn_origin_profile: TurnOriginProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    # A's new-conversation lane at admission when it is not the native default (Responses); absent stays native.
    state_profile: Literal["foundry-responses-v1"] | None = Field(default=None, exclude_if=lambda value: value is None)
    # A's new-conversation catalog budget at admission when the owner chose the skills tool (SK-03/SK-04).
    skill_catalog_budget_profile: CatalogBudgetProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    # When it entered 'registered' (same CAS); absent on onboardings registered before it was kept.
    registered_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def exact_state(self):
        if (self.target is not None) != (self.state in {"registered", "ready"}):
            raise ValueError("only a registered onboarding retains its original target")
        if self.registered_at is not None and self.target is None:
            raise ValueError("only a registered onboarding retains when it was registered")
        if (self.reason is not None) != (self.state == "failed"):
            raise ValueError("only a failed onboarding retains its reason")
        return self


class TeamAgentName(_Model):
    kind: Literal["team_agent_name"] = "team_agent_name"
    tenant_id: Oid
    team_id: Identifier
    display_name: Annotated[str, Field(min_length=1, max_length=64)]
    request_id: Identifier
    active: bool


class TeamAgentPending(_Model):
    """Admitted onboardings not yet ready or failed; counted against the Team's Agent limit."""

    kind: Literal["team_agent_pending"] = "team_agent_pending"
    tenant_id: Oid
    team_id: Identifier
    request_ids: Annotated[tuple[Identifier, ...], Field(max_length=MAX_TEAM_AGENTS)]

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.request_ids)) != len(self.request_ids):
            raise ValueError("each onboarding is pending once")
        return self


def onboarding_key(tenant_id, team_id, request_id):
    return control_key("team-agent-onboarding", tenant_id, team_id, request_id)


def name_key(tenant_id, team_id, display_name):
    return control_key("team-agent-name", tenant_id, team_id, display_name.casefold())


def pending_key(tenant_id, team_id):
    return control_key("team-agent-pending", tenant_id, team_id)


def _seed(tenant_id, team_id, request_id):
    return hashlib.sha256("|".join(("fa-agent-onboarding-v1", tenant_id, team_id, request_id)).encode()).hexdigest()


def derived_names(tenant_id, team_id, request_id):
    h = _seed(tenant_id, team_id, request_id)
    return {"agent_name": "fa-agent-" + h[:20], "session_id": "fa-session-" + h[:32],
            "executor_id": "fa-executor-" + h[:32]}


def _request_sha256(body):
    return digest(["fa-agent-onboarding-request-v1", body.model_dump(mode="json")])


def _admit_request(value, domain):
    t, team, rid = value.tenant_id, value.team_id, value.request_id
    return ControlRequest(
        request_id=control_key("team-agent-onboarding-step", t, team, rid, "admitted"),
        request_digest=digest([value.request_sha256, "admitted", value.model_dump(mode="json")]),
        read_set=(onboarding_key(t, team, rid), name_key(t, team, value.display_name), pending_key(t, team),
                  roster_key(t, team), identity_key(t, value.owner_oid), team_authority_key(domain, team)),
    )


def _step_request(value, state):
    """One step transition; reason is never in the digest so the same step always replays."""
    t, team, rid = value.tenant_id, value.team_id, value.request_id
    material = [value.request_sha256, state]
    if state in {"registered", "ready"}:
        material.append(value.target.model_dump(mode="json"))
    reads = (onboarding_key(t, team, rid),)
    if state in {"ready", "failed"}:
        reads += (pending_key(t, team),)
    if state == "failed":
        reads += (name_key(t, team, value.display_name),)
    return ControlRequest(request_id=control_key("team-agent-onboarding-step", t, team, rid, state),
                          request_digest=digest(material), read_set=reads)


MAX_CREATE_ATTEMPTS = 8
# One Foundry call is bounded well inside the read-back-only window, so an in-flight POST is never re-sent.
FOUNDRY_CONNECT_SECONDS = 30
FOUNDRY_READ_SECONDS = 60
# A claimed POST whose outcome is still unread blocks a new POST this long (covers an in-flight concurrent call).
CREATE_RETRY_SECONDS = 300
assert FOUNDRY_CONNECT_SECONDS + FOUNDRY_READ_SECONDS < CREATE_RETRY_SECONDS


class TeamAgentCreateAttempt(_Model):
    """One create-only claim to POST create_version; only the CAS winner of attempt N may POST."""

    kind: Literal["team_agent_create_attempt"] = "team_agent_create_attempt"
    tenant_id: Oid
    team_id: Identifier
    request_id: Identifier
    attempt: Annotated[int, Field(ge=1, le=MAX_CREATE_ATTEMPTS)]
    claim: Identifier
    claimed_at: AwareDatetime


def attempt_key(tenant_id, team_id, request_id, attempt):
    return control_key("team-agent-create-attempt", tenant_id, team_id, request_id, str(attempt))


def _attempt_request(value, claim):
    t, team, rid = value.tenant_id, value.team_id, value.request_id
    return ControlRequest(
        request_id=control_key("team-agent-create-attempt-step", t, team, rid, str(claim.attempt)),
        request_digest=digest([value.request_sha256, claim.model_dump(mode="json")]),
        read_set=(onboarding_key(t, team, rid), attempt_key(t, team, rid, claim.attempt)),
    )


def _attempt_result(claim):
    return {"request_id": claim.request_id, "attempt": claim.attempt}


def _result(value):
    return {"request_id": value.request_id, "state": value.state}


def view(value):
    result = {"request_id": value.request_id, "state": value.state, "reason": value.reason,
              "display_name": value.display_name, "agent_name": value.agent_name}
    if value.target is not None:
        result["agent_id"] = value.target.agent_id
    return result


# ----- Foundry (agent version) ------------------------------------------------------------------------------


class FoundryCallError(Exception):
    """status None means the outcome is unknown (transport or unexpected failure)."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


class ProjectAgentFoundry:
    """Hosted agent versions in A's project with the API's managed identity; a write is never retried."""

    def __init__(self, endpoint, credential):
        self.endpoint, self.credential = endpoint, credential

    def _call(self, operation, *, read=False):
        from azure.ai.projects import AIProjectClient
        from azure.core.exceptions import AzureError
        retry = PLATFORM_READ_RETRY if read else PLATFORM_NO_RETRY
        try:
            # allow_preview: SDK 2.3.0 sends the Foundry-Features header for agent versions only when enabled.
            with AIProjectClient(endpoint=self.endpoint, credential=self.credential, allow_preview=True, **retry,
                                 connection_timeout=FOUNDRY_CONNECT_SECONDS,
                                 read_timeout=FOUNDRY_READ_SECONDS) as project:
                return operation(project.agents)
        except AzureError as error:
            raise FoundryCallError(_upstream_status(error)) from None
        except Exception:
            raise FoundryCallError(None) from None

    def get_version(self, agent_name, version):
        return self._call(lambda agents: agents.get_version(agent_name, version).as_dict(), read=True)

    def list_versions(self, agent_name):
        # Every page: a deployment's version of this build may be anywhere in the history.
        return self._call(lambda agents: [item.as_dict() for item in agents.list_versions(agent_name, limit=100)],
                          read=True)

    def create_version(self, agent_name, *, definition, metadata):
        return self._call(lambda agents: agents.create_version(
            agent_name, body={"definition": definition, "metadata": metadata}).as_dict())

    def get_agent(self, agent_name):
        return self._call(lambda agents: agents.get(agent_name).as_dict(), read=True)

    def update_route(self, agent_name, agent_endpoint):
        return self._call(lambda agents: agents.update_details(
            agent_name, {"agent_endpoint": agent_endpoint}).as_dict())


def default_foundry(composition):
    return ProjectAgentFoundry(composition.config.targets[0].project_endpoint, composition.clients[0].credential)


def default_setup(composition):
    import os
    from .behavior_agent_setup import ROLE_SCOPE_ENV, NewAgentSetup
    credential = composition.clients[0].credential if os.environ.get(ROLE_SCOPE_ENV) else None
    return NewAgentSetup.from_environment(composition.config.targets[0].project_endpoint, credential)


# ----- context ----------------------------------------------------------------------------------------------


def _owner_context(composition, principal, *, write):
    from .behavior_auth import BehaviorPrincipal
    teams = getattr(composition.verifier, "teams", None)
    if teams is None:
        raise Problem(503, "TEAM_REGISTRY_DISABLED", "the Team registry is not configured")
    if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human"
            or principal.actor_id is not None or principal.tenant_id != teams.tenant_id):
        raise Problem(403, "TEAM_OWNER_DENIED", "the verified original Team owner is required")
    principal.require(principal.team, "approve" if write else "read")
    owner = teams.owner(principal.oid)
    if owner is None or not owner.active or owner.kind != "owner" or owner.team_id != principal.team:
        raise Problem(403, "TEAM_OWNER_DENIED", "an active registered owner of this Team is required")
    if principal.team != composition.config.identity.team_id:
        raise Problem(409, "TEAM_RUNTIME_NOT_SERVED", "this runtime serves only its configured Team")
    return teams


def _read(store, key):
    row = store.read_record(key)
    return None if row is None else _parse(row.payload["data"], TeamAgentOnboarding)


def _role_profile(profile, selection=None):
    """A's settings on the agent-role profile.

    With an owner selection the standing profile carries no tools or skills: capabilities apply only through a
    human profile selection, so S5 records the chosen ones as the new agent's own selection (``_selection_template``).
    """
    from .behavior_assistant import AssistantProfile
    values = {**profile.model_dump(), "prompt_profile": NEW_AGENT_PROFILE,
              "user_text_file_profile": profile.user_text_file_profile or "fa-user-text-files-v1", "sop_copy": None}
    if selection is not None:
        values.update(capabilities=(), skill_artifacts=(), skill_catalog_budget_profile=None)
    try:
        return AssistantProfile.model_validate(values)
    except ValueError:
        raise Problem(409, "AGENT_ONBOARDING_PROFILE_UNSUPPORTED",
                      "the original Team template cannot use the agent-role profile") from None


def _with_origin(profile, turn_origin_profile):
    return profile if turn_origin_profile is None else profile.model_copy(
        update={"turn_origin_profile": turn_origin_profile})


def _effective_turn_origin(composition, original):
    """A's turn origin for new conversations: its current human selection, else its standing profile."""
    from .behavior_profile_selection import current_selection
    head = current_selection(composition.runtime.store, original)
    return original.profile.turn_origin_profile if head is None else head.command.template.settings.turn_origin_profile


def _effective_state_profile(composition, original):
    """A's lane for new conversations when it differs from the native default (its current human selection)."""
    from .behavior_profile_selection import current_selection
    from .native_responses_wire import UNLABELED_STATE_PROFILE
    head = current_selection(composition.runtime.store, original)
    value = UNLABELED_STATE_PROFILE if head is None else head.command.template.settings.state_profile
    return None if value == UNLABELED_STATE_PROFILE else value


def _effective_catalog_budget(composition, original, selection):
    """A's catalog budget for new conversations, kept only when the owner's choice has a skills catalog to present."""
    from .behavior_profile_selection import current_selection
    if not skills_selected(selection.capabilities):
        return None
    head = current_selection(composition.runtime.store, original)
    return (original.profile.skill_catalog_budget_profile if head is None
            else head.command.template.settings.skill_catalog_budget_profile)


def _selection_template(base, selection, state_profile=None, budget=None):
    """The new agent's first new-conversation template: its own base settings plus exactly the owner's choice."""
    from .behavior_agent_template import AgentTemplate, TemplateRole, TemplateSettings
    from .behavior_sdk_state import native_default_checkpoint
    from .behavior_skill_prompt import assemble_prompt
    # The registered role profile (agent-role v1 for an onboarding registered before v2) keeps S5 repeatable.
    role = assemble_prompt(base.prompt_profile if base.prompt_profile in ROLE_PROFILES else NEW_AGENT_PROFILE).record
    try:
        return AgentTemplate(
            role=TemplateRole(profile=role.profile, prefix_sha256=role.prefix_sha256,
                              capability_catalog_version=role.capability_catalog_version),
            settings=TemplateSettings(
                # A new Agent's native conversations compact automatically unless A's base already chose a profile.
                checkpoint_profile=base.checkpoint_profile or native_default_checkpoint(
                    base.model_provider, base.worker_profile),
                model_provider=base.model_provider,
                # Like A and B's selections (stage conversation_defaults): the server-declared facts, e.g. gpt-5.4-mini's
                # documented 272000 input, instead of the pinned runtime's 128000 default (TC-04).
                sdk_start_profile=base.sdk_start_profile,
                model_capacity_profile=base.model_capacity_profile or "fa-model-capacity-v1",
                skill_catalog_budget_profile=budget, text_attachment_profile=base.text_attachment_profile,
                user_text_file_profile=base.user_text_file_profile, turn_origin_profile=base.turn_origin_profile,
                worker_profile=base.worker_profile, capabilities=selection.capabilities,
                **({} if state_profile is None else {"state_profile": state_profile}),
            ),
            skills=selection.skills,
        )
    except ValueError:
        raise Problem(409, "AGENT_ONBOARDING_PROFILE_UNSUPPORTED",
                      "the chosen tools and skills do not form an agent-role template") from None


def _check_selection(composition, original, selection):
    """S0 gate before any write: only the current Team-approved catalog, on the agent-role base profile."""
    from types import SimpleNamespace
    from .behavior_profile_selection import profile_from_template
    from .behavior_skill_catalog import validate_catalog_budget
    from .m3_skills import validate_selection
    check_selectable(selection.capabilities, NEW_AGENT_PROFILE, runtime_grants(composition.runtime))
    names = tuple(item.name for item in selection.skills)
    try:
        validate_selection(names)
    except ValueError:
        names = None
    if names is None or names != tuple(sorted(names)):
        raise Problem(422, "AGENT_ONBOARDING_INVALID", "skill references must be distinct and ordered by name")
    if names and not skills_selected(selection.capabilities):
        raise Problem(422, "AGENT_ONBOARDING_SKILLS_TOOL_REQUIRED",
                      f"approved skills need the {SKILLS_ENTRY_ID} capability on the agent-role profile")
    base = _with_origin(_role_profile(original.profile, selection), _effective_turn_origin(composition, original))
    profile = profile_from_template(SimpleNamespace(profile=base), _selection_template(
        base, selection, _effective_state_profile(composition, original),
        _effective_catalog_budget(composition, original, selection)))
    control = composition.runtime.work_policy
    if profile.skill_catalog_budget_profile is not None:
        validate_catalog_budget(control.models.select(None).capacity_snapshot(), len(names),
                                control.runtime.dep.memory._count)
    if not names:
        return
    authority = getattr(getattr(composition, "assistant", None), "authority", None)
    if authority is None or authority.runtime is not composition.runtime:
        raise Problem(503, "AGENT_TEMPLATE_DISABLED", "the configured assistant authority is required")
    binding = original.template
    actor = authority.actors.get(binding.agent_oid)
    if (actor is None or actor.oid != binding.agent_oid or actor.team != binding.team_id
            or authority.artifacts.context.auth.tenant_id != binding.tenant_id):
        raise Problem(403, "PROFILE_SELECTION_DENIED", "the original Team agent identity is required")
    catalog = authority.skills.catalog(actor, binding.team_id, names, schema_version="fa-approved-skill-catalog-v2")
    current = tuple(TemplateSkill(name=item.name, version_id=item.version_id, sha256=item.sha256,
                                  metadata_sha256=item.metadata_sha256) for item in catalog.entries)
    if current != selection.skills or catalog.unavailable_count:
        raise Problem(409, "PROFILE_SELECTION_SOURCES_CHANGED", "skill references do not match current approved Team skills")


def _original_policy(composition, selection=None, *, check=False):
    from .behavior_agent_registry import resolve_agent_policy
    if composition.runtime.work_policy is None:
        composition.installation()
    config = composition.config
    original = resolve_agent_policy(composition.runtime.store, config, config.targets[0].agent_id)
    if (original.agent_scope is not None or original.legacy_template_policy.source_visibility != "team"
            or original.profile.memory_scope is not None or original.profile.instruction is not None):
        raise Problem(409, "AGENT_ONBOARDING_PROFILE_UNSUPPORTED",
                      "only the original shared Team template can seed a new agent")
    if check and selection is not None:
        _check_selection(composition, original, selection)
    else:
        _role_profile(original.profile, selection)
    return original


# ----- S0 admission -----------------------------------------------------------------------------------------


def _admit(composition, principal, value):
    store, config = composition.runtime.store, composition.config
    domain = config.table.scope.authority_domain
    request = _admit_request(value, domain)
    okey, nkey, pkey, rkey, ikey, akey = request.read_set
    legacy_owner = config.identity.human_oid

    def decide(rows):
        stored_owner = None if rows[ikey] is None else _row(rows, ikey, IdentityRegistration)
        if (stored_owner is None and value.owner_oid != legacy_owner
                or stored_owner is not None and (stored_owner.kind != "owner" or not stored_owner.active
                                                 or stored_owner.team_id != value.team_id)):
            raise Problem(403, "TEAM_OWNER_DENIED", "an active registered owner of this Team is required")
        if rows[okey] is not None:
            raise Problem(409, "AGENT_ONBOARDING_CONFLICT", "this request ID already has an original onboarding")
        name = None if rows[nkey] is None else _row(rows, nkey, TeamAgentName)
        if name is not None and name.active:
            raise Problem(409, "AGENT_NAME_TAKEN", "this Team already has an agent with that name")
        authority = None if rows[akey] is None else _row(rows, akey, TeamAuthority)
        # T6b: the value in force after the owner's lowering steps, not the operator's original grant.
        limit = MAX_TEAM_AGENTS if authority is None else authority.limits.max_agents
        pending = None if rows[pkey] is None else _row(rows, pkey, TeamAgentPending)
        roster = None if rows[rkey] is None else _row(rows, rkey, TeamAgents)
        waiting = () if pending is None else pending.request_ids
        used = len(config.targets) + (0 if roster is None else len(roster.agent_ids)) + len(waiting)
        if used >= limit:
            raise Problem(409, "AGENT_QUOTA_REACHED", "this Team has no remaining agent capacity", limit=limit)
        updated = TeamAgentPending(tenant_id=value.tenant_id, team_id=value.team_id,
                                   request_ids=(*waiting, value.request_id))
        label = TeamAgentName(tenant_id=value.tenant_id, team_id=value.team_id, display_name=value.display_name,
                              request_id=value.request_id, active=True)
        return ControlChanges(writes=(
            _write(okey, value, "create"),
            _write(nkey, label, "create" if name is None else "replace"),
            _write(pkey, updated, "create" if pending is None else "replace"),
        ), result=_result(value))

    _transact(store, request, decide)


def _transact(store, request, decide, attempts=3):
    for attempt in range(attempts):
        try:
            return store.transact(request, decide)
        except Problem as error:
            code = error.body["error"]["code"]
            if code == "BEHAVIOR_REQUEST_CONFLICT":
                raise Problem(409, "AGENT_ONBOARDING_CONFLICT",
                              "this request ID is bound to a different original onboarding") from None
            if code == "BEHAVIOR_COMMIT_UNKNOWN":
                raise Problem(503, "AGENT_ONBOARDING_UNKNOWN",
                              "the step outcome is unknown; POST the same request ID and body again to continue") from None
            if error.status != 412 or code != "BEHAVIOR_CONFLICT" or attempt == attempts - 1:
                raise


def _advance(store, current, state, *, target=None, reason=None, registered_at=None):
    # registered_at is kept out of the step digest (like reason), so a retry with a later clock still replays.
    updated = current.model_copy(update={"state": state, "target": target if state in {"registered", "ready"}
                                         else None, "reason": reason,
                                         "registered_at": registered_at if state == "registered"
                                         else current.registered_at})
    updated = TeamAgentOnboarding.model_validate_json(updated.model_dump_json())
    request = _step_request(updated, state)
    okey = request.read_set[0]
    t, team = current.tenant_id, current.team_id

    def decide(rows):
        stored = _row(rows, okey, TeamAgentOnboarding)
        if stored != current:
            raise Problem(412, "BEHAVIOR_CONFLICT", "the onboarding changed; read it again")
        writes = [_write(okey, updated, "replace")]
        if state in {"ready", "failed"}:
            pkey = pending_key(t, team)
            pending = _row(rows, pkey, TeamAgentPending)
            writes.append(_write(pkey, pending.model_copy(update={"request_ids": tuple(
                rid for rid in pending.request_ids if rid != current.request_id)}), "replace"))
        if state == "failed":
            nkey = name_key(t, team, current.display_name)
            name = _row(rows, nkey, TeamAgentName)
            if name.request_id != current.request_id or not name.active:
                raise Problem(409, "TEAM_REGISTRY_INVALID", "the onboarding no longer holds its original name")
            writes.append(_write(nkey, name.model_copy(update={"active": False}), "replace"))
        return ControlChanges(writes=tuple(writes), result=_result(updated))

    _transact(store, request, decide)


# ----- S1/S2 Foundry ----------------------------------------------------------------------------------------


def _number(version):
    value = version.get("version")
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _mine(versions, request_id):
    mine, others = [], False
    for item in versions:
        metadata = item.get("metadata") or {}
        if metadata.get(_METADATA_RID) == request_id and _number(item) is not None:
            mine.append(item)
        else:
            others = True
    return sorted(mine, key=_number), others


def _list(foundry, name):
    try:
        return foundry.list_versions(name)
    except FoundryCallError as error:
        if error.status == 404:
            return []
        raise


def _a_definition(composition, foundry):
    """(definition, label) of A's version of the deployed build; 503 AGENT_VERSION_FOLLOWING before it is synced."""
    from .behavior_agent_versions import original_source
    return original_source(composition, foundry)


def _target(composition, value, details):
    names = derived_names(value.tenant_id, value.team_id, value.request_id)
    guid = details.get("agent_guid")
    principal_id = (details.get("instance_identity") or {}).get("principal_id")
    if not isinstance(guid, str) or not isinstance(principal_id, str):
        return None
    try:
        return FoundryTarget(executor_id=names["executor_id"], project_endpoint=composition.config.targets[0].project_endpoint,
                             agent_id=guid.lower(), agent_name=value.agent_name, agent_version=details["version"],
                             session_id=names["session_id"], agent_oid=principal_id.lower())
    except ValueError:
        raise Problem(502, "AGENT_ONBOARDING_FOUNDRY_INVALID", "the created agent version has an invalid identity") from None


def _latest_attempt(store, value):
    keys = tuple(attempt_key(value.tenant_id, value.team_id, value.request_id, n)
                 for n in range(1, MAX_CREATE_ATTEMPTS + 1))
    rows = store.read_snapshot(keys)
    latest = None
    for key in keys:
        if rows[key] is None:
            break
        latest = _parse(rows[key].payload["data"], TeamAgentCreateAttempt)
    return latest


def _claim(store, value, attempt, now):
    """True only for the caller whose create-only attempt row committed; any other outcome must not POST."""
    claim = TeamAgentCreateAttempt(tenant_id=value.tenant_id, team_id=value.team_id, request_id=value.request_id,
                                   attempt=attempt, claim=uuid.uuid4().hex, claimed_at=now())
    request = _attempt_request(value, claim)
    okey, akey = request.read_set

    def decide(rows):
        if _row(rows, okey, TeamAgentOnboarding) != value:
            raise Problem(412, "BEHAVIOR_CONFLICT", "the onboarding changed; read it again")
        return ControlChanges(writes=(_write(akey, claim, "create"),), result=_attempt_result(claim))

    try:
        store.transact(request, decide)
    except Problem as error:
        if error.status in {409, 412, 503} or error.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN":
            return False
        raise
    return True


def _create(composition, foundry, value, *, deadline, sleep, clock, now):
    """Return ("target", FoundryTarget), ("failed", reason) or ("pending", None) when unresolved."""
    store = composition.runtime.store
    rejected = False
    while True:
        try:
            mine, others = _mine(_list(foundry, value.agent_name), value.request_id)
        except FoundryCallError:
            return "pending", None
        if not mine:
            if others:
                raise Problem(409, "AGENT_ONBOARDING_NAME_CONFLICT",
                              "the derived agent name belongs to another request; nothing was created")
            if rejected:
                return "failed", "FOUNDRY_CREATE_REJECTED"
            latest = _latest_attempt(store, value)
            if latest is not None and (now() - latest.claimed_at).total_seconds() < CREATE_RETRY_SECONDS:
                # An earlier POST may still be in flight or committed without being visible yet: read back only.
                if clock() >= deadline:
                    return "pending", None
                sleep(1)
                continue
            attempt = 1 if latest is None else latest.attempt + 1
            if attempt > MAX_CREATE_ATTEMPTS:
                raise Problem(409, "AGENT_ONBOARDING_ATTEMPTS_EXHAUSTED",
                              "every create attempt for this request ID is used; its slot is retained")
            try:
                definition, source = _a_definition(composition, foundry)
            except FoundryCallError:
                return "pending", None
            if not _claim(store, value, attempt, now):
                return "pending", None
            try:
                from .behavior_agent_versions import METADATA_BUILD, METADATA_SOURCE, deployed_build
                # fa_build lets every API process of this build pin the new agent's first version directly.
                foundry.create_version(value.agent_name, definition=definition, metadata={
                    _METADATA_RID: value.request_id, _METADATA_TEAM: value.team_id,
                    METADATA_BUILD: deployed_build(composition.runtime), METADATA_SOURCE: source})
            except FoundryCallError as error:
                if error.status not in {400, 422}:
                    return "pending", None
                # Explicit rejection: confirm by read-back that no version of this request exists.
                rejected = True
                continue
            continue
        statuses = [item.get("status") for item in mine]
        if "creating" not in statuses:
            for item in mine:
                if item.get("status") == "active":
                    target = _target(composition, value, item)
                    if target is not None:
                        return "target", target
                    break
            else:
                if all(status in {"failed", "deleting", "deleted"} for status in statuses):
                    return "failed", "FOUNDRY_VERSION_FAILED"
        if clock() >= deadline:
            return "pending", None
        sleep(1)


# ----- S4/S5 runtime registration and role --------------------------------------------------------------------


def _register_runtime(composition, principal, value):
    from .behavior_agent_registry import _check_policy, read_agent_registry, register_agent_scope
    from .behavior_agent_registry_operator import _owned_result_work, _peer_policy
    from .behavior_team_registry import resolve_agent_scope
    from .behavior_work_policy import SteadyWorkPolicy
    store, config = composition.runtime.store, composition.config
    original = _original_policy(composition, value.selection)
    agent_id = value.target.agent_id
    scope = resolve_agent_scope(store, config, agent_id)
    policy = _peer_policy(config, original, agent_id, value.request_id, scope)
    policy = SteadyWorkPolicy.model_validate_json(policy.model_copy(update={
        "profile": _with_origin(_role_profile(policy.profile, value.selection), value.turn_origin_profile)}).model_dump_json())
    _check_policy(config, original, policy, scope)
    try:
        existing = read_agent_registry(store, config, agent_id)
    except Problem as error:
        if error.body["error"]["code"] != "AGENT_SCOPE_MISSING":
            raise
        existing = None
    if existing is not None and existing.policy != policy:
        # Registered before NEW_AGENT_PROFILE changed: resuming keeps that registration's role profile.
        kept = SteadyWorkPolicy.model_validate_json(policy.model_copy(update={"profile": policy.profile.model_copy(
            update={"prompt_profile": existing.policy.profile.prompt_profile})}).model_dump_json())
        if existing.policy.profile.prompt_profile not in ROLE_PROFILES or existing.policy != kept:
            raise Problem(409, "AGENT_ONBOARDING_CONFLICT", "the agent already has a different runtime registration")
        policy = kept
    if existing is None:
        _owned_result_work(composition, policy)
    register_agent_scope(store, config, policy, principal, now=composition.runtime.clock())


def _save_role(composition, principal, value, text):
    control = composition.for_agent(value.target.agent_id).runtime.work_policy
    try:
        save_role_prompt(control, principal, role_prompt_command(value.request_id, text))
    except Problem as error:
        if error.body["error"]["code"] != "AGENT_ROLE_PROMPT_POLICY_CHANGED":
            raise


def _select_tools(composition, principal, value):
    """S5: the owner's original choice becomes the new agent's own human profile selection (same rid = same row)."""
    if value.selection is None:
        return
    from .behavior_profile_selection import SelectConversationProfile, saved_selection_command, select_profile
    peer = composition.for_agent(value.target.agent_id)
    control = peer.runtime.work_policy
    try:
        # A resumed S5 resends exactly what it saved, even if this build would build the template differently.
        command = saved_selection_command(control, value.request_id) or SelectConversationProfile(
            request_id=value.request_id, expected_selection_id=None,
            template=_selection_template(control.policy.profile, value.selection, value.state_profile,
                                         value.skill_catalog_budget_profile))
        select_profile(control, getattr(getattr(peer, "assistant", None), "authority", None), principal, command)
    except Problem as error:
        # After S0 the agent already exists: a selection refusal here must never read as "nothing was written".
        code = error.body["error"]["code"]
        if code not in _SELECTION_CODES:
            raise
        logging.getLogger(__name__).warning(
            "AGENT_ONBOARDING_SELECTION_STALE request_id=%s cause=%s", value.request_id, code)
        raise Problem(409, "AGENT_ONBOARDING_SELECTION_STALE",
                      "the original tool and skill choice no longer matches the Team catalog; the agent is registered "
                      "and this request ID stays the only way forward", cause=code) from None


# ----- entry points -----------------------------------------------------------------------------------------


def onboard(composition, principal, body, *, foundry=None, setup=None, deadline_seconds=20, sleep=time.sleep,
            clock=time.monotonic, now=lambda: datetime.now(timezone.utc)):
    """Return (finished, view): finished is True for ready/failed; otherwise continue with the same body."""
    teams = _owner_context(composition, principal, write=True)
    if type(body) is not CreateTeamAgent:
        raise Problem(422, "AGENT_ONBOARDING_INVALID", "a typed onboarding body is required")
    role_prompt_command(body.request_id, body.role_prompt)
    deadline = clock() + deadline_seconds
    store, config = composition.runtime.store, composition.config
    tenant, team = teams.tenant_id, principal.team
    request_sha = _request_sha256(body)
    okey = onboarding_key(tenant, team, body.request_id)
    current = _read(store, okey)
    if current is not None and (current.request_sha256 != request_sha or current.owner_oid != principal.oid):
        raise Problem(409, "AGENT_ONBOARDING_CONFLICT", "this request ID is bound to a different original onboarding")
    if current is None:
        original = _original_policy(composition, body.selection, check=True)
        _admit(composition, principal, TeamAgentOnboarding(
            tenant_id=tenant, team_id=team, request_id=body.request_id, owner_oid=principal.oid,
            request_sha256=request_sha, display_name=body.display_name,
            role_prompt_sha256=hashlib.sha256(body.role_prompt.encode("utf-8")).hexdigest(),
            agent_name=derived_names(tenant, team, body.request_id)["agent_name"], state="admitted",
            selection=body.selection, turn_origin_profile=_effective_turn_origin(composition, original),
            state_profile=_effective_state_profile(composition, original),
            skill_catalog_budget_profile=None if body.selection is None else _effective_catalog_budget(
                composition, original, body.selection),
        ))
    foundry = default_foundry(composition) if foundry is None else foundry
    setup = default_setup(composition) if setup is None else setup
    setup_pending = False
    for _ in range(16):
        current = _read(store, okey)
        try:
            if current.state == "admitted":
                _advance(store, current, "creating")
            elif current.state == "creating":
                outcome, detail = _create(composition, foundry, current, deadline=deadline, sleep=sleep, clock=clock,
                                          now=now)
                if outcome == "pending":
                    break
                if outcome == "failed":
                    _advance(store, current, "failed", reason=detail)
                else:
                    # Before registration, so no deploy-time endpoint writer sees this agent yet (#468).
                    pending = setup.pending(foundry, current.agent_name, detail.agent_oid)
                    if pending is not None:
                        logging.getLogger(__name__).warning(
                            "FA_AGENT_SETUP_PENDING request_id=%s %s", current.request_id, pending)
                        setup_pending = True
                        break
                    teams.register_agent(principal, current.request_id, detail.agent_id, detail)
                    _advance(store, current, "registered", target=detail, registered_at=now())
            elif current.state == "registered":
                _register_runtime(composition, principal, current)
                _save_role(composition, principal, current, body.role_prompt)
                _select_tools(composition, principal, current)
                _advance(store, current, "ready", target=current.target)
            else:
                break
        except Problem as error:
            if error.status != 412 or error.body["error"]["code"] != "BEHAVIOR_CONFLICT":
                raise
        if clock() >= deadline:
            break
    current = _read(store, okey)
    result = view(current)
    if setup_pending and current.state == "creating":
        result["reason"] = SETUP_PENDING
    return current.state in {"ready", "failed"}, result


def onboarding_status(composition, principal, request_id):
    from pydantic import TypeAdapter
    teams = _owner_context(composition, principal, write=False)
    try:
        TypeAdapter(Identifier).validate_python(request_id, strict=True)
    except ValueError:
        current = None
    else:
        current = _read(composition.runtime.store, onboarding_key(teams.tenant_id, principal.team, request_id))
    if current is None:
        raise Problem(404, "AGENT_ONBOARDING_NOT_FOUND", "no onboarding has this request ID")
    return view(current)


def agent_onboarding(store, config, agent_id):
    """The original onboarding of a registered Agent, or None for one registered without onboarding."""
    from .behavior_team_registry import TeamAgent, agent_key
    tenant = config.identity.tenant_id
    row = store.read_record(agent_key(tenant, agent_id))
    if row is None:
        return None
    agent = _parse(row.payload["data"], TeamAgent)
    current = _read(store, onboarding_key(tenant, agent.team_id, agent.request_id))
    if current is None or current.target is None or current.target.agent_id != agent_id:
        return None
    return current


def onboarding_not_ready():
    return Problem(409, "AGENT_ONBOARDING_IN_PROGRESS", "the agent's onboarding has not reached ready")


def console_connection(composition, principal, agent_id):
    """Owner-only read of one ready onboarded Agent's installed policy; never writes."""
    from pydantic import TypeAdapter
    from .behavior_agent_console_wire import AgentConsoleConnection
    from .behavior_team_registry import TeamAgent, agent_key
    teams = _owner_context(composition, principal, write=False)
    try:
        TypeAdapter(Identifier).validate_python(agent_id, strict=True)
    except ValueError:
        raise Problem(404, "AGENT_CONSOLE_NOT_FOUND", "no onboarded agent has this ID") from None
    store, config = composition.runtime.store, composition.config
    configured = next((target for target in config.targets if target.agent_id == agent_id), None)
    if configured is not None:
        if config.agent_scope(agent_id).team_id != principal.team:
            raise Problem(403, "AGENT_CONSOLE_DENIED", "the agent belongs to another Team")
        raise Problem(404, "AGENT_CONSOLE_NOT_FOUND", "no onboarded agent has this ID")
    row = store.read_record(agent_key(teams.tenant_id, agent_id))
    if row is None:
        raise Problem(404, "AGENT_CONSOLE_NOT_FOUND", "no onboarded agent has this ID")
    if _parse(row.payload["data"], TeamAgent).team_id != principal.team:
        raise Problem(403, "AGENT_CONSOLE_DENIED", "the agent belongs to another Team")
    current = agent_onboarding(store, config, agent_id)
    if current is None:
        raise Problem(404, "AGENT_CONSOLE_NOT_FOUND", "no onboarded agent has this ID")
    if current.state != "ready":
        raise onboarding_not_ready()
    control = composition.for_agent(agent_id).runtime.work_policy
    control._human(principal)
    policy = control.installed()
    if not policy.active or not policy.read_enabled or policy.agent_id != agent_id:
        raise Problem(403, "WORK_POLICY_REVOKED", "the agent's current read authority is required")
    return AgentConsoleConnection(agent_id=agent_id, display_name=current.display_name,
                                  backend_configuration_sha256=policy.configuration_sha256,
                                  standing_policy=policy, standing_policy_sha256=policy.sha256)


# ----- startup history --------------------------------------------------------------------------------------


def validate_onboarding_history(view_, inventory, config, classified):
    """Recognize onboarding/name/pending rows and every original step receipt of each onboarding."""
    from .behavior_team_registry import agent_key
    identity = config.identity
    domain = config.table.scope.authority_domain
    tenant, team = str(identity.tenant_id), identity.team_id
    onboardings, names, pendings, attempts = {}, {}, {}, {}
    for key, row in inventory.rows.items():
        kind = row.payload["data"].get("kind")
        if kind == "team_agent_onboarding":
            onboardings[key] = view_.row(key, TeamAgentOnboarding)
        elif kind == "team_agent_name":
            names[key] = view_.row(key, TeamAgentName)
        elif kind == "team_agent_pending":
            pendings[key] = view_.row(key, TeamAgentPending)
        elif kind == "team_agent_create_attempt":
            attempts[key] = view_.row(key, TeamAgentCreateAttempt)

    def invalid(message):
        raise Problem(409, "TEAM_REGISTRY_INVALID", message)

    by_rid = {}
    for key, value in onboardings.items():
        if (key != onboarding_key(value.tenant_id, value.team_id, value.request_id)
                or (value.tenant_id, value.team_id) != (tenant, team)
                or value.agent_name != derived_names(tenant, team, value.request_id)["agent_name"]):
            invalid("a retained onboarding differs from its original scope")
        chain = (*_CHAIN[:_CHAIN.index(value.state) + 1],) if value.state != "failed" else (
            "admitted", "creating", "failed")
        for state in chain:
            image = TeamAgentOnboarding.model_validate_json(json.dumps({
                **value.model_dump(mode="json"), "state": state,
                "target": value.model_dump(mode="json").get("target") if state in {"registered", "ready"} else None,
                "registered_at": value.model_dump(mode="json").get("registered_at")
                if state in {"registered", "ready"} else None,
                "reason": value.reason if state == "failed" else None,
            }))
            request = (_admit_request(image, domain) if state == "admitted" else _step_request(image, state))
            writes = request.read_set[:3] if state == "admitted" else request.read_set
            view_.receipt(request, writes, exact={key: view_.image_hash(key, image)}, result=_result(image))
        if value.state in {"registered", "ready"}:
            akey = agent_key(tenant, value.target.agent_id)
            agent = inventory.rows.get(akey)
            data = None if agent is None else agent.payload["data"]
            if (data is None or data.get("request_id") != value.request_id
                    or data.get("target") != value.target.model_dump(mode="json")):
                invalid("a registered onboarding lacks its original Team agent registration")
        by_rid[value.request_id] = value
    pkey = pending_key(tenant, team)
    for key, value in pendings.items():
        if key != pkey or (value.tenant_id, value.team_id) != (tenant, team):
            invalid("a retained pending list differs from its original Team")
    waiting = set() if pkey not in pendings else set(pendings[pkey].request_ids)
    # A stuck 'registered' onboarding left the list in its Agent's retire CAS (checked by the registry history), or,
    # under a tombstone written without it (also one left admitted or creating), in its own release CAS.
    retired = {value.agent_id: value for value in classified.values() if isinstance(value, TeamAgentRetired)}
    agents = {value.request_id: value.agent_id for value in classified.values() if isinstance(value, TeamAgent)}
    released = {value.onboarding_request_id for value in retired.values()}
    for rid, value in by_rid.items():
        tombstone = retired.get(agents.get(rid)) if value.state not in {"ready", "failed"} else None
        if tombstone is not None and tombstone.onboarding_request_id is None and rid not in waiting:
            view_.receipt(_release_request(tombstone, rid), (pkey,), result=_release_result(tombstone, rid))
            released.add(rid)
    open_ = {rid for rid, value in by_rid.items() if value.state not in {"ready", "failed"} and rid not in released}
    if waiting != open_:
        invalid("the pending onboarding list differs from the open onboardings")
    for key, value in names.items():
        owner = by_rid.get(value.request_id)
        if (key != name_key(tenant, team, value.display_name) or (value.tenant_id, value.team_id) != (tenant, team)
                or owner is None or owner.display_name != value.display_name
                or value.active == (owner.state == "failed")):
            invalid("a retained agent name differs from its original onboarding")
    for value in by_rid.values():
        nkey = name_key(tenant, team, value.display_name)
        if value.state != "failed" and (nkey not in names or names[nkey].request_id != value.request_id):
            invalid("an onboarding lacks its original agent name")
    claimed = {}
    for key, claim in attempts.items():
        owner = by_rid.get(claim.request_id)
        if (key != attempt_key(tenant, team, claim.request_id, claim.attempt)
                or (claim.tenant_id, claim.team_id) != (tenant, team) or owner is None or owner.state == "admitted"):
            invalid("a retained create attempt differs from its original onboarding")
        view_.receipt(_attempt_request(owner, claim), (key,), exact={key: view_.image_hash(key, claim)},
                      result=_attempt_result(claim))
        claimed.setdefault(claim.request_id, set()).add(claim.attempt)
    if any(numbers != set(range(1, len(numbers) + 1)) for numbers in claimed.values()):
        invalid("retained create attempts are not contiguous")
    classified.update(onboardings)
    classified.update(names)
    classified.update(pendings)
    classified.update(attempts)
    return classified
