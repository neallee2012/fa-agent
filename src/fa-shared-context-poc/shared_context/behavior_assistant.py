"""Assistant profiles, their runtime, and the authority that checks a native launch against its grant.

The legacy assistant session (AssistantEngine and its local and Hosted executors) is retired.
The stored session and delegation row models stay so existing BehaviorStore rows still read.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_serializer, model_validator

from . import behavior_assistant_wire as aw
from . import behavior_wire as wire
from .auth import Actor
from .behavior_delivery import _Obligation
from .behavior_operations import _Model, _digest, _parse, _read_conflicts, _row, _write, control_key
from .behavior_runtime import BehaviorRuntime, material
from .behavior_store import ControlRequest, InlineMaterial
from .behavior_workers import ExecutorBinding, JobRecord, worker_key
from .errors import Problem
from .m3_artifacts import ArtifactService
from .m3_skills import ApprovedSkillSource, ApprovedSkillCatalog, SkillSelection, validate_selection
from .behavior_skill_prompt import SKILL_PROFILES
from .behavior_skill_catalog import CatalogBudgetProfile
from .behavior_attachments import TextAttachmentProfile
from .behavior_text_files import UserTextFileProfile
from .behavior_user_file_prompt import FILE_READ_PROFILES
from .behavior_user_turn import UserTurnSources, ordered_user_sources
from .behavior_model_capacity import ModelCapacityProfile
from .behavior_work_memory import WorkMemorySource
from .behavior_work_memory_prompt import WORK_MEMORY_PROFILE
from .behavior_reviewed_note_prompt import REVIEWED_NOTE_PROFILE
from .memory_bridge_settings import ConversationMemoryCapture
from .behavior_skill_copy import SOP_COPY_PROFILE, TeamSopCopy
from .behavior_capabilities import CAPABILITY_BASE_PROFILES, CapabilityRef, skills_selected, validate_refs

EXECUTOR_VERSION = "assistant-sdk-1-0-13-runtime-1-0-83"


def task_for_profile(binding, profile):
    return aw.Task(
        job_id=binding.job_id, work_id=binding.work.work_id,
        definition_digest=_digest(binding.work.model_dump(mode="json")),
        profile_digest=_digest(profile.model_dump(mode="json")),
        source=profile.source, artifact_id=profile.artifact_id, artifact_version=profile.artifact_version,
        operation_id=binding.operation_id, instruction=profile.instruction,
    )


PROTECTION_VERSION = "assistant-inline-and-m3-retention-v1"


def key(kind, *values):
    return control_key("assistant-" + kind, *values)


class AssistantProfile(_Model):
    binding_id: wire.Identifier
    source: wire.ExactSourceRef
    claim_id: wire.Identifier
    legacy_source_work: wire.Identifier
    artifact_id: wire.Identifier
    artifact_version: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    policy_version: Literal["assistant-own-result-v1"]
    max_model_requests: Annotated[int, Field(ge=1, le=8)] = 6
    require_operation: bool = False
    instruction: aw.InstructionTask | None = Field(default=None, exclude_if=lambda value: value is None)
    checkpoint_profile: aw.CheckpointProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    model_provider: aw.ModelProvider | None = Field(default=None, exclude_if=lambda value: value is None)
    prompt_profile: aw.PromptProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    sdk_start_profile: aw.SdkStartProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    model_capacity_profile: ModelCapacityProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_catalog_budget_profile: CatalogBudgetProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_artifacts: SkillSelection = Field(default=(), exclude_if=lambda value: not value)
    sop_copy: TeamSopCopy | None = Field(default=None, exclude_if=lambda value: value is None)
    text_attachment_profile: TextAttachmentProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    user_text_file_profile: UserTextFileProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    turn_origin_profile: aw.TurnOriginProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    worker_profile: wire.WorkerProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    memory_scope: ConversationMemoryCapture | None = Field(default=None, exclude_if=lambda value: value is None)
    capabilities: tuple[CapabilityRef, ...] = Field(default=(), exclude_if=lambda value: not value)

    _skills = field_validator("skill_artifacts")(validate_selection)
    _capabilities = field_validator("capabilities")(validate_refs)

    @model_validator(mode="after")
    def skill_profile(self):
        aw.validate_provider_checkpoint(self.model_provider, self.checkpoint_profile)
        if (self.prompt_profile == REVIEWED_NOTE_PROFILE) != (
                self.memory_scope is not None and self.memory_scope.enabled and self.memory_scope.recall is not None):
            raise ValueError("Reviewed-note recall requires its captured memory scope and exact superset profile")
        if self.text_attachment_profile is not None and self.prompt_profile is None:
            raise ValueError("Text attachments require an explicit versioned instruction profile")
        if self.user_text_file_profile is not None and self.prompt_profile is None:
            raise ValueError("Literal input files require an explicit versioned instruction profile")
        if self.prompt_profile in FILE_READ_PROFILES and self.user_text_file_profile is None:
            raise ValueError("The original-user-file profile requires explicit literal file input")
        if self.turn_origin_profile is not None and self.prompt_profile is None:
            raise ValueError("Turn origin requires an explicit versioned instruction profile")
        if self.skill_artifacts and self.prompt_profile not in SKILL_PROFILES and not skills_selected(self.capabilities):
            raise ValueError("Skill selectors require the explicit approved-skill prompt profile")
        if self.skill_catalog_budget_profile is not None and (
            (self.prompt_profile not in SKILL_PROFILES and not skills_selected(self.capabilities))
            or self.model_capacity_profile is None
        ):
            raise ValueError("Catalog budgeting requires both approved-skill and declared-capacity profiles")
        if self.sdk_start_profile == "fa-sdk-start-gate-v2" and self.prompt_profile != WORK_MEMORY_PROFILE:
            raise ValueError("SDK gate v2 requires the reviewed-work-memory instruction profile")
        if self.worker_profile is not None and self.checkpoint_profile is not None:
            raise ValueError("The resident worker PoC keeps ordinary SDK checkpoints only")
        if (self.prompt_profile == SOP_COPY_PROFILE) != (self.sop_copy is not None):
            raise ValueError("SOP copy requires both the explicit profile and its exact Team-only authorization")
        if self.capabilities and self.prompt_profile not in CAPABILITY_BASE_PROFILES:
            raise ValueError("Capabilities require a composable versioned instruction profile")
        return self


class AssistantRuntime(BehaviorRuntime):
    def __init__(self, *args, profiles: tuple[AssistantProfile, ...], **kwargs):
        super().__init__(*args, **kwargs)
        if self.mode == "native" and (
            self.dep.workers.contract.capacity != 1
            or self.dep.workers.contract.executor_version != EXECUTOR_VERSION
            or self.dep.workers.contract.protection_version != PROTECTION_VERSION
            or self.dep.delivery.contract.protection_version != PROTECTION_VERSION
        ):
            raise Problem(503, "ASSISTANT_NATIVE_READINESS_REQUIRED",
                          "checksum readiness does not cover SDK execution or M3 artifact retention")
        self.profiles = MappingProxyType({p.binding_id: AssistantProfile.model_validate(p.model_dump()) for p in profiles})
        if not profiles or len(self.profiles) != len(profiles):
            raise Problem(503, "ASSISTANT_CONFIGURATION", "distinct explicit profiles are required")
        for profile in profiles:
            binding = self.bindings.get(profile.binding_id)
            if (binding is None or set(binding.work.required_checks) != set(aw.CHECKS)
                    or binding.work.human_checks or binding.child_jobs
                    or profile.claim_id not in binding.claim_ids or profile.source.source_id not in binding.source_ids
                    or profile.instruction is not None
                    and (profile.instruction.checkpoint_profile != profile.checkpoint_profile
                         or profile.instruction.model_provider != profile.model_provider
                         or profile.instruction.prompt_profile != profile.prompt_profile
                         or profile.instruction.skill_catalog_budget_profile != profile.skill_catalog_budget_profile
                         or profile.instruction.text_attachment_profile != profile.text_attachment_profile
                         or profile.instruction.user_text_file_profile != profile.user_text_file_profile
                         or profile.instruction.turn_origin_profile != profile.turn_origin_profile
                         or profile.instruction.sdk_start_profile != profile.sdk_start_profile
                         or profile.instruction.sop_copy != profile.sop_copy
                         or (None if profile.instruction.model_capacity is None else profile.instruction.model_capacity.profile)
                         != profile.model_capacity_profile)):
                raise Problem(503, "ASSISTANT_CONFIGURATION", "fixed work checks and reviewed-source binding are required")
            if self.mode == "native":
                try:
                    self.dep.native_launches.target_for_owner(binding.agent_oid)
                except Problem:
                    raise Problem(503, "ASSISTANT_CONFIGURATION", "one C1 target for the assigned owning agent is required") from None

    def job_input(self, binding):
        profile = self.profiles.get(binding.binding_id)
        if profile is None:
            return super().job_input(binding)
        return task_for_profile(binding, profile).model_dump_json().encode()

    def _save_plan(self, binding, kind, principal, command, context, data, request, *,
                   extra_writes=(), expected_index=None):
        if kind == "admission" and binding.binding_id in self.profiles:
            task = self.profiles[binding.binding_id].instruction
            if task is not None and task.peer_work is not None:
                raise Problem(403, "AGENT_MESSAGE_CONSUMER_REQUIRED",
                              "a human admission cannot replace the original peer queue claim")
            if not isinstance(command, (aw.AssistantSubmit, aw.InstructionSubmit)) or principal.kind != "human":
                raise Problem(403, "ASSISTANT_HUMAN_ADMISSION_REQUIRED", "only explicit human admission delegates own-result saving")
            consent = _Delegation(
                human_oid=principal.oid, event_id=binding.event_id, profile_digest=_digest(
                    self.profiles[binding.binding_id].model_dump(mode="json")),
                command=command, admission_request=request,
            )
            extra_writes = (*extra_writes, _write(key("delegation", binding.event_id), consent, "create",
                                                 material(consent.model_dump(mode="json"))))
        return super()._save_plan(binding, kind, principal, command, context, data, request,
                                  extra_writes=extra_writes, expected_index=expected_index)


class _Delegation(_Model):
    human_oid: wire.Identifier
    event_id: wire.Identifier
    profile_digest: wire.Digest
    command: aw.AssistantSubmit | aw.InstructionSubmit
    admission_request: ControlRequest
    operator_rule_sha256: wire.Digest | None = None
    source_evidence_id: wire.Identifier | None = None

    @model_serializer(mode="wrap")
    def original_shape(self, serialize):
        data = serialize(self)
        if self.operator_rule_sha256 is None and self.source_evidence_id is None:
            data.pop("operator_rule_sha256", None)
            data.pop("source_evidence_id", None)
        return data


class _Session(_Model):
    binding_id: wire.Identifier
    assignment: aw.Assignment
    execution: ExecutorBinding
    ticket_digest: wire.Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    vm_turn_ticket_id: wire.Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    turn_authorization_digest: wire.Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    state: Literal["accepted", "running", "waiting", "ready_to_finalize"] = "accepted"
    context_read: bool = False
    model_requests: Annotated[int, Field(ge=0)] = 0
    reason: Literal["finish_missing", "operation_unknown", "sdk_failed"] | None = None
    model_intent_ids: Annotated[tuple[wire.Identifier, ...], Field(max_length=8)] = ()
    skill_catalog: ApprovedSkillCatalog | None = Field(default=None, exclude_if=lambda value: value is None)
    user_turn_sources: UserTurnSources = Field(default=(), exclude_if=lambda value: not value)
    work_memory_source: WorkMemorySource | None = Field(default=None, exclude_if=lambda value: value is None)

    _user_sources = field_validator("user_turn_sources")(ordered_user_sources)

    @model_validator(mode="after")
    def one_entry(self):
        if (self.ticket_digest is None) == (self.vm_turn_ticket_id is None):
            raise ValueError("a session requires exactly one legacy ticket or admitted VM turn")
        return self

    @model_serializer(mode="wrap")
    def original_shape(self, serialize):
        data = serialize(self)
        if not self.model_intent_ids:
            data.pop("model_intent_ids", None)
        return data


class _ModelIntent(_Model):
    request_sha256: wire.Digest
    consumed: bool = False


def _carried(previous, prior_turn, name, default):
    """A turn-to-turn value: the resumed session's if any, else the previous instruction turn's, else the default."""
    if previous is not None:
        return getattr(previous, name)
    return default if prior_turn is None else getattr(prior_turn, name)


class AssistantAuthority:
    def __init__(self, runtime: AssistantRuntime, artifacts: ArtifactService, actors: tuple[Actor, ...], *,
                 require_legacy_sources=True):
        if not isinstance(runtime, AssistantRuntime) or not isinstance(artifacts, ArtifactService):
            raise Problem(503, "ASSISTANT_CONFIGURATION", "real runtime and ArtifactService are required")
        self.runtime, self.store, self.artifacts = runtime, runtime.store, artifacts
        self.actors = {actor.oid: actor for actor in actors}
        from .behavior_turn_ticket import ticket_index
        self.turn_tickets = ticket_index(self.store)
        self._vm_starts = {}
        self.skills = ApprovedSkillSource(artifacts)
        if not require_legacy_sources:
            fence = getattr(self.store, "_installation_fence", None)
            if (runtime.mode != "native" or fence is None
                    or getattr(self.store, "_verified_fresh_initialization", None) != fence[1]):
                raise Problem(503, "ASSISTANT_CONFIGURATION", "only the verified fresh native origin has no legacy sources")
        for profile in runtime.profiles.values():
            binding = runtime.bindings[profile.binding_id]
            actor = self.actors.get(binding.agent_oid)
            if actor is None or actor.team != binding.team_id:
                raise Problem(503, "ASSISTANT_CONFIGURATION", "the artifact actor must be server-bound")
            if not require_legacy_sources:
                # Native turns have no legacy result work. M3 checks ownership at actual use.
                continue
            # A new runtime work ID must never masquerade as a legacy Work ID.
            source = artifacts.context.get(actor, binding.team_id, profile.legacy_source_work)
            if source.payload["owner"] != actor.actor:
                raise Problem(403, "ASSISTANT_LEGACY_OWNER", "configured legacy artifact source is not owned")
        if runtime.dep.native_launches is not None:
            runtime.dep.native_launches.assistant = self

    def _binding(self, session):
        control=self.runtime.work_policy
        registered=(control is not None and session.assignment.task.instruction is not None
                    and session.binding_id in control.records and session.binding_id in self.runtime.bindings)
        if not registered:
            self.runtime.refresh_bindings(session.binding_id)
        return self.runtime.bindings[session.binding_id]

    def _keys(self, session):
        if session.vm_turn_ticket_id is not None:
            from .behavior_vm_session import vm_session_keys
            return vm_session_keys(self, session)
        binding = self._binding(session)
        task = session.assignment.task.instruction
        if task is not None and task.peer_work is not None:
            from .behavior_agent_message_runtime import session_keys
            delivery_keys = session_keys(task)
        else:
            delivery_keys = (key("delegation", binding.event_id), control_key("delivery-obligation", binding.obligation_id))
        return tuple(dict.fromkeys((
            *self.runtime._gate_keys(binding), worker_key("job", binding.job_id),
            self.runtime.dep.workers.pool_key(binding.job_id),
            key("session", session.assignment.session_id),
            *delivery_keys,
        )))

    def _gate(self, session, rows):
        if session.vm_turn_ticket_id is not None:
            from .behavior_vm_session import vm_session_state
            return vm_session_state(self, session, rows, executing=True)
        binding = self._binding(session)
        policy, context = self.runtime._state(binding, rows)
        job = _row(rows, worker_key("job", binding.job_id), JobRecord)
        pool = self.runtime.dep.workers._pool(rows)
        task = session.assignment.task.instruction
        if task is not None and task.peer_work is not None:
            from .behavior_agent_message_runtime import check_session
            check_session(session, job, rows)
            consent_matches = True
        else:
            delegation = _row(rows, key("delegation", binding.event_id), _Delegation)
            obligation = _row(rows, control_key("delivery-obligation", binding.obligation_id), _Obligation)
            consent_matches = (delegation.human_oid == binding.human_oid and delegation.event_id == binding.event_id
                and delegation.profile_digest == session.assignment.task.profile_digest
                and _parse(rows[key("delegation", binding.event_id)].payload["material"], InlineMaterial)
                == material(delegation.model_dump(mode="json"))
                and obligation.work_id == binding.work.work_id and obligation.event_id == binding.event_id)
        if (context != session.execution.owner or job.binding != session.execution
                or job.cancel_requested or job.state not in {"starting", "running", "capacity_uncertain"}
                or sum(slot.binding == session.execution for slot in pool.slots) != 1
                or self.runtime.job_input(binding) != job.spec.input
                or not consent_matches):
            raise Problem(403, "ASSISTANT_EXECUTION_FENCED", "current owner, epoch, job and capacity are required")
        return policy, context

    def validate_native_start(self, start, grant, binding_id):
        binding = self.runtime.bindings.get(binding_id)
        if (binding_id not in self.runtime.profiles or binding is None
                or self.runtime.job_input(binding) != start.input or grant.definition != binding.work
                or grant.checkpoint_after_bytes is not None):
            raise Problem(403, "ASSISTANT_NATIVE_GRANT", "exact assistant input, definition and native grant are required")

