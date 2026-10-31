"""Real, explicitly selected AssistantEngine vertical slice.

The legacy profile computes a typed metrics answer. Registered instructions
instead preserve human text, SDK conversation state and actual replies. Neither
profile is an autonomous publisher. Human admission + a pinned
server policy delegate exactly one own-result artifact. That delegation is NOT
a GrantRecord and cannot approve any other operation. M3 retains its real
legacy source-work ownership checks through an explicit preprovisioned mapping.

All decisions/results/materials live in the existing BehaviorStore authority.
LocalAssistantExecutor is a real SDK integration fixture, hard-disabled in
native mode. Native deployment uses the separately registered Hosted adapter;
offline SDK evidence never supplies a native readiness witness.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from pathlib import Path
from types import MappingProxyType
from time import monotonic
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_serializer, model_validator

from . import behavior_assistant_wire as aw
from . import behavior_wire as wire
from .auth import Actor
from .behavior_delivery import ArtifactVersion, VerifiedWorkResult, _Obligation, _Work
from .behavior_dispatch import ArtifactMetrics
from .behavior_operations import (
    BudgetRecord, OperationDescription, ResolvedResource,
    _Model, _Operation, _digest, _json, _parse, _row, _updated, _write, control_key,
)
from .behavior_runtime import BehaviorRuntime, material
from .behavior_store import ControlChanges, ControlRequest, InlineMaterial
from .behavior_workers import ExecutorAdapter, ExecutorBinding, ExecutorEvidence, ExecutorStart, JobRecord, worker_key
from .errors import Problem
from .m3_artifacts import ArtifactService
from .m3_models import ArtifactDocument, CreateArtifact, VersionInput
from .m3_skills import ApprovedSkillSource, ApprovedSkillCatalog, SkillSelection, validate_selection
from .behavior_skill_prompt import SKILL_PROFILES
from .behavior_skill_resources_prompt import SKILL_RESOURCES_PROFILE
from .behavior_skill_catalog import CatalogBudgetProfile, present_catalog
from .behavior_attachments import TextAttachmentProfile
from .behavior_text_files import UserTextFileProfile
from .behavior_user_file_prompt import FILE_READ_PROFILES, USER_INPUT_READ_PROFILES
from .behavior_user_turn import UserTurnSources, ordered_user_sources
from .behavior_model_capacity import ModelCapacityProfile
from .behavior_work_memory import WorkMemorySource
from .behavior_work_memory_prompt import WORK_MEMORY_PROFILE
from .behavior_reviewed_note_prompt import REVIEWED_NOTE_PROFILE
from .memory_bridge_settings import ConversationMemoryCapture
from .behavior_skill_copy import SOP_COPY_PROFILE, TeamSopCopy
from .behavior_skill_draft import skill_draft_enabled
from .behavior_capabilities import CAPABILITY_BASE_PROFILES, CapabilityRef, skills_selected, validate_refs
from .behavior_capability_prompt import skill_access, skill_purpose_access
from .source_materials import SourceEvent

EXECUTOR_VERSION = "assistant-sdk-1-0-13-runtime-1-0-83"


def task_for_profile(binding, profile):
    return aw.Task(
        job_id=binding.job_id, work_id=binding.work.work_id,
        definition_digest=_digest(binding.work.model_dump(mode="json")),
        profile_digest=_digest(profile.model_dump(mode="json")),
        source=profile.source, artifact_id=profile.artifact_id, artifact_version=profile.artifact_version,
        operation_id=binding.operation_id, instruction=profile.instruction,
    )


def _read_conflicts(read, *, phase):
    """Only explicitly selected read-only gates may repeat; never a tool/dispatch."""
    for attempt in range(3):
        try:
            return read()
        except Problem as error:
            if error.status != 412 or error.body["error"]["code"] != "BEHAVIOR_CONFLICT" or attempt == 2:
                raise
            logging.getLogger(__name__).warning("ASSISTANT_READ_CONFLICT phase=%s attempt=%s maximum=3", phase, attempt + 1)


def _ledger_conflicts(store, request, decide):
    """Known rejected local ledger EGTs only; a replay never inherits a stale fresh flag."""
    for attempt in range(3):
        entered = False
        def current(rows):
            nonlocal entered
            entered = True
            return decide(rows)
        try:
            receipt = store.transact(request, current)
            if attempt and not entered:
                raise Problem(409, "ASSISTANT_LEDGER_ALREADY_COMMITTED",
                              "original ledger receipt cannot grant another dispatch")
            return receipt
        except Problem as error:
            if error.status != 412 or error.body["error"]["code"] != "BEHAVIOR_CONFLICT":
                raise
            if _read_conflicts(lambda: store._receipt(request), phase="ledger_receipt") is not None:
                raise Problem(409, "ASSISTANT_LEDGER_ALREADY_COMMITTED",
                              "original ledger commit is retained; no new dispatch permission") from None
            if attempt == 2:
                raise
            logging.getLogger(__name__).warning("ASSISTANT_LEDGER_CONFLICT attempt=%s maximum=3", attempt + 1)
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

    def submit(self, binding_id, command, principal, *, reconcile=False):
        if binding_id in self.profiles:
            task = self.profiles[binding_id].instruction
            if task is not None and task.peer_work is not None:
                raise Problem(403, "AGENT_MESSAGE_CONSUMER_REQUIRED",
                              "the registered peer work can only be admitted by its original queue consumer")
            if (not isinstance(command, (aw.AssistantSubmit, aw.InstructionSubmit)) or principal.kind != "human"
                    or not command.authorize_own_result):
                raise Problem(403, "ASSISTANT_HUMAN_ADMISSION_REQUIRED", "explicit human own-result admission is required")
        return super().submit(binding_id, command, principal, reconcile=reconcile)

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


class _ArtifactEffect(_Model):
    description: OperationDescription
    description_digest: wire.Digest
    content: bytes
    authority_source: Literal["human_admission"] = "human_admission"
    admission_plan_id: wire.Identifier
    policy_version: Literal["assistant-own-result-v1"]
    policy_revision: int
    execution: ExecutorBinding
    stage: Literal["create_unknown", "created", "upload_unknown", "saved"]
    artifact_sha256: wire.Digest | None = None


class _ModelIntent(_Model):
    request_sha256: wire.Digest
    consumed: bool = False


class AssistantAuthority:
    def __init__(self, runtime: AssistantRuntime, artifacts: ArtifactService, actors: tuple[Actor, ...], *,
                 require_legacy_sources=True):
        if not isinstance(runtime, AssistantRuntime) or not isinstance(artifacts, ArtifactService):
            raise Problem(503, "ASSISTANT_CONFIGURATION", "real runtime and ArtifactService are required")
        self.runtime, self.store, self.artifacts = runtime, runtime.store, artifacts
        self.actors = {actor.oid: actor for actor in actors}
        from .behavior_turn_ticket import ticket_index
        self.turn_tickets = ticket_index(self.store)
        self._instruction_activity = {}
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

    def record_instruction_activity(self, session):
        if session.assignment.task.instruction is not None:
            self._instruction_activity[session.execution.job_id] = (session.execution, monotonic())

    def instruction_recently_active(self, execution):
        observed = self._instruction_activity.get(execution.job_id)
        # Process-local scheduling hint, never authority or proof of process liveness.
        return (observed is not None and observed[0] == execution and monotonic() - observed[1] < 300)

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

    def prepare_session(self, start: ExecutorStart, *, ticket_digest, vm_turn_ticket_id=None):
        """Prepare the original assignment/session; the caller owns the one atomic issuance."""
        candidates = [b for b in self.runtime.bindings.values() if b.job_id == start.binding.job_id]
        if len(candidates) != 1:
            raise Problem(403, "ASSISTANT_ASSIGNMENT_DENIED", "job has no assistant binding")
        binding = candidates[0]
        if binding.binding_id not in self.runtime.profiles or start.input != self.runtime.job_input(binding):
            raise Problem(403, "ASSISTANT_ASSIGNMENT_DENIED", "immutable task differs")
        sid = key("session-id", start.binding.start_request_id)
        task = aw.Task.model_validate_json(start.input)
        from .behavior_agent_message_api import assignment_recipient
        assignment = aw.Assignment(binding_id=binding.binding_id, session_id=sid, sdk_session_id=sid,
                                   task=task, resume=start.checkpoint is not None,
                                   agent_message_recipient=assignment_recipient(self, binding, task))
        prior_turn = None
        if task.instruction is not None:
            from .behavior_instruction import sdk_session_id, TurnArchive
            assignment = assignment.model_copy(update={
                "sdk_session_id": sdk_session_id(task.instruction.conversation_id),
                "resume": start.checkpoint is not None or task.instruction.previous_archive_id is not None})
            if task.instruction.previous_archive_id is not None:
                archive_key = task.instruction.previous_archive_id
                prior_turn = _row(self.store.read_snapshot((archive_key,)), archive_key, TurnArchive)
        previous = None
        if start.checkpoint is not None:
            checkpoint = aw.Checkpoint.model_validate_json(start.checkpoint)
            previous = _row(_read_conflicts(lambda: self.store.read_snapshot((key("session", checkpoint.session_id),)),
                                           phase="resume_session"),
                            key("session", checkpoint.session_id), _Session)
            archive = self.archive(previous)
            if (previous.state != "waiting" or previous.assignment.task != task
                    or checkpoint.task_digest != _digest(task.model_dump(mode="json"))
                    or checkpoint.archive_digest != archive.sha256):
                raise Problem(403, "ASSISTANT_CHECKPOINT_INVALID", "resume checkpoint differs from its original task")
            assignment = assignment.model_copy(update={"sdk_session_id": previous.assignment.sdk_session_id})
        if task.instruction is not None and task.instruction.sdk_start_profile is not None:
            from .behavior_code_identity import code_sha256
            assignment = assignment.model_copy(update={"sdk_start_binding": wire.SdkStartBinding(
                schema_version="fa-sdk-start-binding-" + wire.sdk_start_version(task.instruction.sdk_start_profile),
                profile=task.instruction.sdk_start_profile,
                session_id=sid, sdk_session_id=assignment.sdk_session_id, job_id=start.binding.job_id,
                task_digest=_digest(task.model_dump(mode="json")), profile_digest=task.profile_digest,
                execution_digest=_digest(start.binding.model_dump(mode="json")),
                worker_configuration_digest=self.runtime.dep.workers.configuration_digest(),
                code_sha256=code_sha256(),
            )})
        control = self.runtime.work_policy
        admitted = None if control is None else control.records.get(binding.binding_id)
        admitted = admitted if admitted is not None and admitted.turn_authorization is not None else None
        session = _Session(binding_id=binding.binding_id, assignment=assignment, execution=start.binding,
                           ticket_digest=ticket_digest, vm_turn_ticket_id=vm_turn_ticket_id,
                           state="running" if vm_turn_ticket_id is not None else "accepted",
                           turn_authorization_digest=None if admitted is None else _digest(admitted.model_dump(mode="json")),
                           context_read=(previous.context_read if previous is not None else
                                         prior_turn.context_read if prior_turn is not None else False),
                           model_requests=0 if previous is None else previous.model_requests,
                           model_intent_ids=() if previous is None else previous.model_intent_ids,
                           user_turn_sources=(previous.user_turn_sources if previous is not None else
                                              prior_turn.user_turn_sources if prior_turn is not None else ()),
                           work_memory_source=(previous.work_memory_source if previous is not None else
                                               prior_turn.work_memory_source if prior_turn is not None else None))
        self.current_model_capacity(session)
        if task.instruction is not None and skill_access(task.instruction.prompt_profile, task.instruction.capabilities):
            session = _updated(session, skill_catalog=self._configured_skills(session))
        if previous is not None and previous.skill_catalog is not None and previous.skill_catalog != session.skill_catalog:
            raise Problem(403, "SKILL_SOURCE_CHANGED", "retained skill catalog changed; original history is not rewritten")
        if previous is None and prior_turn is not None and prior_turn.skill_catalog is not None:
            from .behavior_instruction import previous_turn, validate_turn_skills
            prior_turn, _ = previous_turn(self, session)
            validate_turn_skills(self, session, prior_turn)
        return session, archive if previous is not None else None

    def issue(self, start: ExecutorStart):
        """Controller-internal only; a replay cannot mint another execution ticket."""
        ticket = SecretStr(secrets.token_hex(32))
        session, restore_archive = self.prepare_session(
            start, ticket_digest=hashlib.sha256(ticket.get_secret_value().encode()).hexdigest())
        assignment = session.assignment
        binding = self._binding(session)
        sid = assignment.session_id
        fresh = False
        request = ControlRequest(request_id=key("issue", sid), request_digest=_digest(start.model_dump(mode="json")),
                                 read_set=self._keys(session))

        def decide(rows):
            nonlocal fresh
            self._gate(session, rows)
            fresh = True
            writes = [_write(key("session", sid), session, "create")]
            if restore_archive is not None:
                writes.append(_write(key("restore", sid), restore_archive, "create",
                                     material(restore_archive.model_dump(mode="json"))))
            return ControlChanges(writes=tuple(writes),
                                  result={"session_id": sid})

        if self.runtime.mode == "native":
            _ledger_conflicts(self.store, request, decide)
        else:
            self.store.transact(request, decide)
        admitted = None if self.runtime.work_policy is None else self.runtime.work_policy.records.get(binding.binding_id)
        if fresh and admitted is not None and admitted.turn_authorization is not None:
            self.turn_tickets.publish(admitted, session)
        return assignment, ticket if fresh else None

    def session(self, sid, ticket, principal, *, active=True):
        session = _row(self.store.read_snapshot((key("session", sid),)), key("session", sid), _Session)
        task = session.assignment.task.instruction
        peer_read = not active and task is not None and task.peer_work is not None
        self.runtime.authorize(session.binding_id, principal, "read" if peer_read else "execute")
        if (principal.kind != "agent" or session.ticket_digest is None or not isinstance(ticket, SecretStr)
                or not secrets.compare_digest(session.ticket_digest,
                    hashlib.sha256(ticket.get_secret_value().encode()).hexdigest())):
            raise Problem(403, "ASSISTANT_TICKET_DENIED", "server-issued binding ticket is required")
        if active:
            self._gate(session, self.store.read_snapshot(self._keys(session)))
        return session

    def _command(self, session, name, command, decide, *, extra=()):
        request = ControlRequest(
            request_id=key(name, session.assignment.session_id, command.request_id),
            request_digest=_digest(command.model_dump(mode="json")),
            read_set=tuple(dict.fromkeys((*self._keys(session), *extra))),
        )
        def guarded(rows):
            self._gate(session, rows)
            if name in {"model", "model-send"}:
                from .behavior_agent_message_runtime import check_model_time
                check_model_time(self, session, rows)
            native_model = name in {"model", "model-send"} and self.runtime.mode == "native"
            if session.turn_authorization_digest is not None or native_model:
                current = _row(rows, key("session", session.assignment.session_id), _Session)
                if (native_model and current.state != "running" or current.assignment != session.assignment
                        or current.execution != session.execution or current.binding_id != session.binding_id
                        or current.ticket_digest != session.ticket_digest
                        or current.turn_authorization_digest != session.turn_authorization_digest):
                    raise Problem(403, "ASSISTANT_EXECUTION_FENCED",
                                  "current session, ticket and execution are required")
            return decide(rows)
        if (self.runtime.mode == "native"
                and name in {"context", "model", "model-send", "save", "artifact-stage", "finish", "stopped", "checkpoint","progress"}):
            return _ledger_conflicts(self.store, request, guarded)
        return self.store.transact(request, guarded)

    def current_session(self, binding_id, sid, ticket, principal, *, active=True):
        if self._has_turn_ticket(binding_id, sid):
            session = self.turn_session(binding_id, sid, ticket, principal)
            if active:
                self._gate(session, self.store.read_snapshot(self._keys(session)))
            return session
        def read():
            session = self.session(sid, ticket, principal, active=active)
            if session.binding_id != binding_id:
                raise Problem(403, "ASSISTANT_BINDING_DENIED", "ticket is bound to another assignment")
            task = session.assignment.task.instruction
            peer_read = not active and task is not None and task.peer_work is not None
            self.runtime.authorize(binding_id, principal, "read" if peer_read else "execute")
            return session
        return _read_conflicts(lambda: self.store.consistent_read(read, row_scoped=True), phase="session_authority")

    def _has_turn_ticket(self, binding_id, sid):
        control = self.runtime.work_policy
        record = None if control is None else control.records.get(binding_id)
        return (record is not None and record.turn_authorization is not None
                or self.turn_tickets.contains(sid))

    def turn_session(self, binding_id, sid, ticket, principal):
        """Authenticate without storage; read mutable session state once after authentication."""
        captured = self.turn_tickets.authenticate(binding_id, sid, ticket, principal)
        session_key = key("session", sid)
        session = _row({session_key: self.store.read_record(session_key)}, session_key, _Session)
        captured.matches(session)
        return session

    def model_session(self, binding_id, sid, ticket, principal):
        """Admitted turns verify their ticket locally and read mutable session state once.

        Legacy instructions keep the principal/ticket/registry read below. Effect handlers still use
        _command and its current CAS predicates; this first slice does not replace those checks.
        """
        if self._has_turn_ticket(binding_id, sid):
            return self.turn_session(binding_id, sid, ticket, principal)
        from .behavior_auth import BehaviorPrincipal
        from .behavior_work_registry import head_key
        control = self.runtime.work_policy
        record = None if control is None else control.records.get(binding_id)
        binding = self.runtime.bindings.get(binding_id)
        if (record is None or record.profile.instruction is None or binding is None or binding != record.binding
                or binding_id in self.runtime.historical_binding_ids):
            return self.current_session(binding_id, sid, ticket, principal)
        if not isinstance(principal, BehaviorPrincipal):
            raise Problem(403, "BEHAVIOR_BINDING_DENIED", "unknown authorized binding")
        principal.require(binding.team_id, "execute")
        if (principal.tenant_id != binding.tenant_id
                or principal.oid != (binding.human_oid if principal.kind == "human" else binding.agent_oid)):
            raise Problem(403, "BEHAVIOR_BINDING_DENIED", "principal is not assigned to this binding")
        registry, session_key = control.registry_key, key("session", sid)

        def read():
            rows = self.store.read_snapshot((session_key, registry, head_key(registry)))
            return rows, control.listed(binding_id, rows)
        rows, listed = _read_conflicts(lambda: self.store.consistent_read(read, row_scoped=True),
                                       phase="session_authority")
        if not listed:
            raise Problem(403, "BEHAVIOR_BINDING_DENIED", "unknown authorized binding")
        session = _row(rows, session_key, _Session)
        if (principal.kind != "agent" or session.ticket_digest is None or not isinstance(ticket, SecretStr)
                or not secrets.compare_digest(session.ticket_digest,
                    hashlib.sha256(ticket.get_secret_value().encode()).hexdigest())):
            raise Problem(403, "ASSISTANT_TICKET_DENIED", "server-issued binding ticket is required")
        if session.binding_id != binding_id:
            raise Problem(403, "ASSISTANT_BINDING_DENIED", "ticket is bound to another assignment")
        return session

    def acquire(self, session, command):
        self.current_model_capacity(session)
        sid = session.assignment.session_id
        fresh = False
        def decide(rows):
            nonlocal fresh
            current = _row(rows, key("session", sid), _Session)
            if current.state != "accepted":
                raise Problem(409, "ASSISTANT_START_UNKNOWN", "previous SDK start must not be replayed")
            fresh = True
            return ControlChanges(writes=(_write(key("session", sid), _updated(current, state="running"), "replace"),),
                                  result={"state": "running"})
        # Only this first commit may start an SDK. Lost acknowledgments observe.
        receipt = self._command(session, "acquire", command, decide)
        if not fresh:
            raise Problem(409, "ASSISTANT_START_UNKNOWN", "original SDK start was already consumed")
        self.record_instruction_activity(session)
        return aw.ToolReceipt(**receipt.payload["result"])

    def acquire_current(self, binding_id, session_id, ticket, command, principal):
        """At most three attempts, all for one immutable acquisition.

        A failed read or rejected atomic EGT may be re-entered only for the
        store's exact 412 conflict. Unknown commits and retained receipts are
        never converted into permission to start another SDK.
        """
        if command.request_id != "sdk-start":
            raise Problem(422, "ASSISTANT_COMMAND_ID", "the fixed bounded command ID is required")
        original = None
        for attempt in range(3):
            try:
                def inspect():
                    self.runtime.authorize(binding_id, principal, "execute")
                    session = self.session(session_id, ticket, principal)
                    if session.binding_id != binding_id:
                        raise Problem(403, "ASSISTANT_BINDING_DENIED", "ticket is bound to another assignment")
                    request = ControlRequest(
                        request_id=key("acquire", session.assignment.session_id, command.request_id),
                        request_digest=_digest(command.model_dump(mode="json")),
                        read_set=tuple(dict.fromkeys(self._keys(session))),
                    )
                    if self.store._receipt(request) is not None:
                        self._gate(session, self.store.read_snapshot(self._keys(session)))
                        raise Problem(409, "ASSISTANT_START_UNKNOWN", "original SDK start was already consumed")
                    return session,request
                session,request=self.store.consistent_read(inspect)
                scope = (session.assignment, session.execution, session.ticket_digest, request)
                if original is not None and scope != original:
                    raise Problem(409, "ASSISTANT_ACQUIRE_CHANGED", "original acquisition scope changed")
                original = scope
                return self.acquire(session, command)
            except Problem as error:
                if error.status != 412 or error.body["error"]["code"] != "BEHAVIOR_CONFLICT":
                    raise
                logging.getLogger(__name__).warning(
                    "ASSISTANT_ACQUIRE_CONFLICT phase=acquire_authority attempt=%s maximum=3", attempt + 1)
                if attempt == 2:
                    raise

    def _reviewed(self, session, principal):
        return _read_conflicts(lambda: self._reviewed_once(session, principal), phase="reviewed_sources")

    def _reviewed_once(self, session, principal):
        def read():
            profile, binding = self.runtime.profiles[session.binding_id], self._binding(session)
            command = wire.ContextRequest(request_id="assistant-current-context", required_claim_ids=(profile.claim_id,))
            context = self.runtime.context(session.binding_id, command, principal)
            exact = self.runtime.read_source(session.binding_id, profile.source.source_id, profile.source.version, principal)
            ref = exact.event.reference
            if (ref.sha256 != profile.source.sha256 or ref.byte_length != profile.source.byte_length
                    or not any(item == ref for item in context.manifest.sources)
                    or not all(claim.human_reviewed for claim in context.claims)):
                raise Problem(403, "ASSISTANT_SOURCE_DENIED", "exact human-reviewed source is required")
            metrics = (None if session.assignment.task.instruction is not None
                       else ArtifactMetrics.model_validate_json(exact.original))
            source_context = self.runtime._source_context(
                binding, self.runtime.authorize(session.binding_id, principal, "read")[1], principal,
            )
            keys = tuple(dict.fromkeys((
                control_key("memory-subject", context.manifest.scope.key, context.manifest.subject),
                *(claim.reference.key for claim in context.claims),
                *(value for source in context.manifest.sources for value in (source.key, source.head_key)),
                *(value for claim in context.claims for value in self.runtime.dep.memory._review_keys(
                    source_context, claim.candidate, claim.review)),
            )))
            snapshot = self.store.read_snapshot(keys)
            again = self.runtime.context(session.binding_id, command, principal)
            if again.manifest != context.manifest:
                raise Problem(409, "ASSISTANT_CONTEXT_CHANGED", "reviewed context changed")
            return metrics, exact, snapshot
        return self.store.consistent_read(read)

    def _current_sources(self, rows, snapshot):
        if any(rows[k] != value for k, value in snapshot.items()):
            raise Problem(409, "ASSISTANT_SOURCE_CHANGED", "source/review authorization changed at the effect gate")
        for record in snapshot.values():
            if record and record.payload["data"].get("reference") and "schema_id" in record.payload["data"]:
                event = _parse(record.payload["data"], SourceEvent)
                if event.expires_at is not None and event.expires_at <= self.runtime._now():
                    raise Problem(403, "SOURCE_EXPIRED", "source expired before assistant dispatch")
            if record and record.payload["data"].get("candidate"):
                from .memory_governance import ReviewedClaim
                claim = _parse(record.payload["data"], ReviewedClaim)
                if (claim.candidate.effective_from > self.runtime._now()
                        or claim.candidate.expires_at is not None and claim.candidate.expires_at <= self.runtime._now()):
                    raise Problem(403, "ASSISTANT_CLAIM_EXPIRED", "reviewed claim is not currently applicable")

    def context(self, session, command, principal):
        metrics, exact, snapshot = self._reviewed(session, principal)
        sid = session.assignment.session_id
        def decide(rows):
            self._current_sources(rows, snapshot)
            current = _row(rows, key("session", sid), _Session)
            return ControlChanges(writes=(_write(key("session", sid), _updated(current, context_read=True), "replace"),),
                                  result={"state": "accepted"})
        self._command(session, "context", command, decide, extra=tuple(snapshot))
        if session.assignment.task.instruction is not None:
            return aw.InstructionContext(source=session.assignment.task.source, content=exact.original.decode("utf-8"))
        return aw.ContextResult(source=session.assignment.task.source, item_count=metrics.item_count,
                                completed_count=metrics.completed_count)

    def context_origin(self, session, command, principal):
        from .behavior_turn_origin import instruction_turn_origin
        control = self.runtime.work_policy
        registration = None if control is None else control.records.get(session.binding_id)
        origin = instruction_turn_origin(session.assignment.task, self._binding(session), registration)
        context = self.context(session, command, principal)
        return aw.InstructionContextOrigin(
            source=context.source, content=context.content,
            schema_version="fa-instruction-context-origin-v1", turn_origin=origin,
        )

    def reserve_model(self, session, command, principal):
        source_snapshot = self.model_sources(session, principal)
        sid, budget_key = session.assignment.session_id, control_key("budget", session.execution.owner.root_budget_id)
        fresh = False
        instruction=session.assignment.task.instruction is not None
        if command.request_id != command.request_sha256:
            raise Problem(422, "ASSISTANT_MODEL_ID", "model request ID must be its exact immutable digest")
        def decide(rows):
            nonlocal fresh
            self._current_sources(rows, source_snapshot)
            current = _row(rows, key("session", sid), _Session)
            budget = _row(rows, budget_key, BudgetRecord)
            if (current.state != "running" or not budget.active
                    or not instruction and (
                    current.model_requests >= self.runtime.profiles[session.binding_id].max_model_requests
                    or budget.remaining_attempts < 1 or budget.remaining_units < 1)):
                raise Problem(429, "ASSISTANT_MODEL_BUDGET", "finite model/root allowance is unavailable")
            fresh = True
            return ControlChanges(writes=(
                _write(key("model-intent", sid, command.request_sha256),
                       _ModelIntent(request_sha256=command.request_sha256), "create"),
                _write(key("session", sid), _updated(
                    current, model_requests=current.model_requests + 1,
                ), "replace"),
                *(() if instruction else (_write(budget_key, _updated(budget, remaining_attempts=budget.remaining_attempts - 1,
                                          remaining_units=budget.remaining_units - 1), "replace"),)),
            ), result={"state": "accepted"})
        receipt = self._command(session, "model", command, decide, extra=(budget_key, *source_snapshot))
        if not fresh:
            raise Problem(409, "ASSISTANT_MODEL_UNKNOWN", "reserved model request is not a replay permission")
        return aw.ToolReceipt(**receipt.payload["result"])

    def consume_model(self, session, command, principal):
        # Instruction work: reserve already checked every source under CAS; revocation, cancellation and execution
        # changes are still refused by the model-send _gate below. Legacy work keeps its source snapshot: legacy
        # multi-model recovery verifies consume's exact read set against the original reservation.
        source_snapshot = ({} if session.assignment.task.instruction is not None
                           else self.model_sources(session, principal))
        if command.checkpoint_compaction:
            from .behavior_instruction import previous_turn
            from .behavior_sdk_state import DIAG_ARCHIVE_CODECS
            task = session.assignment.task.instruction
            if task is None or task.checkpoint_profile is None or task.previous_archive_id is None:
                raise Problem(403, "SDK_STATE_SOURCE_UNVERIFIED", "compaction requires the original configured job profile")
            prior, row = previous_turn(self, session)
            if isinstance(prior.archive, aw.StoredSdkArchive) and prior.archive.codec in DIAG_ARCHIVE_CODECS:
                raise Problem(403, "SDK_STATE_SOURCE_UNVERIFIED", "an inherited checkpoint does not authorize another compact")
            source_snapshot = {task.previous_archive_id: row}
        digest = _digest(command.body)
        if command.request_id != digest:
            raise Problem(403, "ASSISTANT_MODEL_MISMATCH", "model request differs from its reservation")
        intent_key = key("model-intent", session.assignment.session_id, digest)
        fresh = False
        quoted=None
        cost_reads=()
        if session.assignment.task.instruction is not None:
            from .behavior_instruction_cost import quote,cost_key
            quoted=quote(self,session,command.body)
            cost_reads=(self.runtime.work_policy.cost_key,cost_key(session.assignment.session_id,digest))
        def decide(rows):
            nonlocal fresh
            self._current_sources(rows, source_snapshot)
            intent = _row(rows, intent_key, _ModelIntent)
            if intent.consumed or intent.request_sha256 != digest:
                raise Problem(409, "ASSISTANT_MODEL_UNKNOWN", "original model dispatch may already have been sent")
            fresh = True
            from .behavior_instruction_cost import reserve_writes
            options=({"admitted_policy":self.runtime.work_policy.policy}
                     if session.vm_turn_ticket_id is not None else {})
            priced=() if quoted is None else reserve_writes(self.runtime.work_policy,rows,quoted,**options)
            return ControlChanges(writes=(_write(intent_key, _updated(intent, consumed=True), "replace"),*priced),
                                  result={"state": "accepted"})
        self._command(session, "model-send", command, decide, extra=(intent_key, *source_snapshot,*cost_reads))
        if not fresh:
            raise Problem(409, "ASSISTANT_MODEL_UNKNOWN", "a consumed reservation cannot dispatch again")

    def save(self, session, command, principal):
        if session.assignment.task.instruction is not None and session.assignment.task.instruction.sop_copy is not None:
            raise Problem(403, "SOP_COPY_SCOPE_DENIED", "a copy-only slot cannot become a metrics deliverable")
        if (session.assignment.task.instruction is not None
                and skill_draft_enabled(session.assignment.task.instruction.prompt_profile,
                                        session.assignment.task.instruction.capabilities)):
            raise Problem(403, "SKILL_DRAFT_SCOPE_DENIED", "a skill-draft slot cannot become a metrics deliverable")
        metrics, exact, snapshot = self._reviewed(session, principal)
        answer = command.answer
        if (answer.item_count, answer.completed_count, answer.remaining_count) != (
                metrics.item_count, metrics.completed_count, metrics.item_count - metrics.completed_count):
            raise Problem(409, "ASSISTANT_ANSWER_INVALID", "required counts and remaining-items calculation differ")
        binding, profile = self._binding(session), self.runtime.profiles[session.binding_id]
        actor = self.actors[binding.agent_oid]
        content = _json({**answer.model_dump(), "source_id": profile.source.source_id,
                         "source_version": profile.source.version, "source_sha256": profile.source.sha256})
        digest = hashlib.sha256(content).hexdigest()
        effect_key = key("artifact", binding.work.work_id)
        description = OperationDescription(
            adapter_id="assistant-m3", adapter_version="v1", kind="request", method="CREATE_UPLOAD",
            target=f"m3:{binding.team_id}:{profile.artifact_id}:{profile.artifact_version}",
            content=_json({
                "create": {"kind": "deliverable", "title": "Reviewed metrics result",
                           "source_work": profile.legacy_source_work},
                "upload": {"version_id": profile.artifact_version, "filename": "result.json",
                           "media_type": "application/json", "source_work": profile.legacy_source_work,
                           "sha256": digest},
                "file_utf8": content.decode("utf-8"),
            }), resources=(
                ResolvedResource(resource_id=profile.legacy_source_work, version="legacy-owner-checked"),
                ResolvedResource(resource_id=profile.artifact_id, version=profile.artifact_version),
            ),
        )
        ob_key = control_key("delivery-obligation", binding.obligation_id)
        work_key = control_key("delivery-work-state", binding.obligation_id)
        fresh = False
        def reserve(rows):
            nonlocal fresh
            self._current_sources(rows, snapshot)
            current = _row(rows, key("session", session.assignment.session_id), _Session)
            if not current.context_read or current.state != "running":
                raise Problem(409, "ASSISTANT_CONTEXT_REQUIRED", "read current reviewed context before saving")
            ob = _row(rows, ob_key, _Obligation)
            work = _row(rows, work_key, _Work)
            if work.definition != binding.work or ob.event_id != binding.event_id:
                raise Problem(409, "ASSISTANT_WORK_CHANGED", "fixed admission criteria changed")
            existing = rows[effect_key]
            if existing is not None:
                effect = _parse(existing.payload["data"], _ArtifactEffect)
                if effect.description != description:
                    raise Problem(409, "ASSISTANT_RESULT_CONFLICT", "the own-result content is immutable")
                return ControlChanges(result={"state": effect.stage})
            budget_key = control_key("budget", session.execution.owner.root_budget_id)
            budget = _row(rows, budget_key, BudgetRecord)
            if not budget.active or budget.remaining_attempts < 2 or budget.remaining_units < 2:
                raise Problem(429, "ASSISTANT_EFFECT_BUDGET", "both artifact effects need a root reservation")
            fresh = True
            effect = _ArtifactEffect(
                description=description, description_digest=_digest(description.model_dump(mode="json")),
                content=content, admission_plan_id=binding.event_id,
                policy_version=profile.policy_version, policy_revision=self.runtime._state(binding, rows)[0].operation_policy_revision,
                execution=session.execution, stage="create_unknown",
            )
            return ControlChanges(writes=(
                _write(effect_key, effect, "create", InlineMaterial(data=content, sha256=digest)),
                _write(budget_key, _updated(budget, remaining_attempts=budget.remaining_attempts - 2,
                                          remaining_units=budget.remaining_units - 2), "replace"),
            ), result={"state": "create_unknown"})
        self._command(session, "save", command, reserve, extra=(*snapshot, effect_key, ob_key, work_key))
        if not fresh:
            return self.reconcile_artifact(session, principal)
        try:
            self.artifacts.create(actor, binding.team_id, profile.artifact_id, CreateArtifact(
                kind="deliverable", title="Reviewed metrics result", source_work=profile.legacy_source_work,
            ))
        except Exception:
            raise Problem(503, "ASSISTANT_EFFECT_UNKNOWN", "artifact create outcome is unknown; reconcile without resending") from None
        self._stage(session, "created", snapshot)
        return self._upload(session, snapshot)

    def _stage(self, session, stage, snapshot, *, digest=None):
        effect_key = key("artifact", self._binding(session).work.work_id)
        command = wire.Command(request_id=stage)
        fresh = False
        def decide(rows):
            nonlocal fresh
            self._current_sources(rows, snapshot)
            effect = _row(rows, effect_key, _ArtifactEffect)
            if stage == "upload_unknown":
                budget = _row(rows, control_key("budget", session.execution.owner.root_budget_id), BudgetRecord)
                if (effect.stage != "created"
                        or not budget.active
                        or effect.policy_revision != self.runtime._state(self._binding(session), rows)[0].operation_policy_revision):
                    raise Problem(409, "ASSISTANT_EFFECT_UNKNOWN", "original upload is not a new dispatch permission")
                fresh = True
            return ControlChanges(writes=(_write(effect_key, _updated(effect, stage=stage, artifact_sha256=digest),
                                                "replace", _parse(rows[effect_key].payload["material"], InlineMaterial)),),
                                  result={"state": stage})
        result = self._command(session, "artifact-stage", command, decide, extra=(*snapshot, effect_key))
        if stage == "upload_unknown" and not fresh:
            raise Problem(409, "ASSISTANT_EFFECT_UNKNOWN", "upload intent was already consumed; reconcile it")
        return result

    def _upload(self, session, snapshot):
        binding, profile = self._binding(session), self.runtime.profiles[session.binding_id]
        actor = self.actors[binding.agent_oid]
        effect_key = key("artifact", binding.work.work_id)
        effect = _row(_read_conflicts(lambda: self.store.read_snapshot((effect_key,)), phase="artifact_state"),
                      effect_key, _ArtifactEffect)
        before = self.artifacts.get(actor, binding.team_id, profile.artifact_id)
        self._stage(session, "upload_unknown", snapshot)
        try:
            self.artifacts.upload(actor, binding.team_id, profile.artifact_id, profile.artifact_version, before.etag,
                VersionInput(filename="result.json", media_type="application/json",
                             source_work=profile.legacy_source_work, sha256=hashlib.sha256(effect.content).hexdigest()),
                effect.content)
        except Exception:
            raise Problem(503, "ASSISTANT_EFFECT_UNKNOWN", "artifact upload outcome is unknown; never resend bytes") from None
        return self.reconcile_artifact(session, None, snapshot=snapshot)

    def reconcile_artifact(self, session, principal, *, snapshot=None):
        if (session.assignment.task.instruction is not None
                and skill_draft_enabled(session.assignment.task.instruction.prompt_profile,
                                        session.assignment.task.instruction.capabilities)):
            raise Problem(403, "SKILL_DRAFT_SCOPE_DENIED", "a skill-draft slot cannot become a metrics deliverable")
        if snapshot is None:
            _, _, snapshot = self._reviewed(session, principal)
        binding, profile = self._binding(session), self.runtime.profiles[session.binding_id]
        actor = self.actors[binding.agent_oid]
        effect_key = key("artifact", binding.work.work_id)
        effect = _row(_read_conflicts(lambda: self.store.read_snapshot((effect_key,)), phase="artifact_state"),
                      effect_key, _ArtifactEffect)
        try:
            current = self.artifacts.get(actor, binding.team_id, profile.artifact_id)
            document = ArtifactDocument.model_validate(current.payload)
            if (document.owner_oid != actor.oid or document.source_work != profile.legacy_source_work
                    or document.kind != "deliverable" or document.title != "Reviewed metrics result"):
                raise ValueError()
            if effect.stage == "create_unknown":
                self._stage(session, "created", snapshot)
                # The missing create acknowledgment is reconciled; upload is a
                # distinct, not-yet-dispatched effect with its own saved intent.
                return self._upload(session, snapshot)
            if effect.stage == "created":
                return self._upload(session, snapshot)
            if document.pending is not None:
                self.artifacts.reconcile(actor, binding.team_id, profile.artifact_id,
                                         profile.artifact_version, current.etag)
            version = self.artifacts.version(actor, binding.team_id, profile.artifact_id, profile.artifact_version)
            _, content = self.artifacts.download(actor, binding.team_id, profile.artifact_id,
                                                profile.artifact_version, version.blob.etag)
            if content != effect.content or version.source_work != profile.legacy_source_work:
                raise ValueError()
        except Exception:
            raise Problem(503, "ASSISTANT_EFFECT_UNKNOWN", "original artifact receipt is unresolved; do not resend") from None
        self._stage(session, "saved", snapshot, digest=version.sha256)
        return aw.ToolReceipt(state="saved", receipt_id=effect_key, artifact_sha256=version.sha256)

    def finish(self, session, command, principal):
        receipt = self.reconcile_artifact(session, principal)
        _, _, snapshot = self._reviewed(session, principal)
        binding, profile = self._binding(session), self.runtime.profiles[session.binding_id]
        effect_key, result_key = key("artifact", binding.work.work_id), key("result", binding.work.work_id)
        op_key = control_key("operation", binding.operation_id)
        work_key = control_key("delivery-work-state", binding.obligation_id)
        def decide(rows):
            self._current_sources(rows, snapshot)
            current = _row(rows, key("session", session.assignment.session_id), _Session)
            effect = _row(rows, effect_key, _ArtifactEffect)
            work = _row(rows, work_key, _Work)
            if work.definition != binding.work or work.definition_digest != session.assignment.task.definition_digest:
                raise Problem(409, "ASSISTANT_WORK_CHANGED", "finish must verify the same admitted work revision")
            if effect.stage != "saved" or not current.context_read or receipt.artifact_sha256 != effect.artifact_sha256:
                raise Problem(409, "ASSISTANT_FINISH_MISSING", "verified result and exact artifact are required")
            known = ()
            if rows[op_key] is not None:
                operation = _parse(rows[op_key].payload["data"], _Operation)
                if (operation.owner_record_id != session.execution.owner.owner_record_id
                        or operation.conversation_id != session.execution.owner.conversation_id
                        or operation.root_budget_id != session.execution.owner.root_budget_id):
                    raise Problem(403, "ASSISTANT_OPERATION_SCOPE", "operation belongs to a different authority scope")
                if operation.current_attempt_id is not None:
                    if (operation.effect_state != "applied" or operation.business_state != "succeeded"
                            or operation.unsettled_attempt_ids or operation.successful_result is None):
                        raise Problem(409, "ASSISTANT_EFFECT_UNKNOWN", "necessary operation remains unresolved")
                    known = (binding.operation_id,)
            if profile.require_operation and not known:
                raise Problem(409, "ASSISTANT_OPERATION_REQUIRED", "the assigned operation needs a confirmed receipt")
            result = VerifiedWorkResult(
                evidence_id=result_key, verifier_version=self.runtime.dep.delivery.contract.verifier_version,
                work_id=binding.work.work_id, definition_digest=session.assignment.task.definition_digest,
                state="succeeded", passed_checks=aw.CHECKS, known_effects=known,
                artifacts=(ArtifactVersion(artifact_id=profile.artifact_id, version=profile.artifact_version,
                                           sha256=effect.artifact_sha256),),
                content=effect.content, proof=_json({
                    "effect": effect_key, "description_digest": effect.description_digest,
                    "finish_request_id": command.request_id, "criteria_version": binding.work.criteria_version,
                }),
            )
            return ControlChanges(writes=(
                _write(result_key, result, "create", material(result.model_dump(mode="json"))),
                _write(key("session", session.assignment.session_id), _updated(current, state="ready_to_finalize"), "replace"),
            ), result={"state": "ready_to_finalize", "receipt_id": result_key, "artifact_sha256": effect.artifact_sha256})
        record = self._command(session, "finish", command, decide, extra=(*snapshot, effect_key, op_key, result_key, work_key))
        return aw.ToolReceipt(**record.payload["result"])

    def result(self, execution):
        result_key = key("result", execution.work_id)
        rows = _read_conflicts(lambda: self.store.read_snapshot((result_key,)), phase="result_state")
        result = _row(rows, result_key, VerifiedWorkResult)
        if (result.work_id != execution.work_id or _parse(rows[result_key].payload["material"], InlineMaterial)
                != material(result.model_dump(mode="json"))):
            raise Problem(403, "ASSISTANT_RESULT_INVALID", "durable finish evidence differs")
        return result

    def stopped(self, session, command):
        sid = session.assignment.session_id
        def decide(rows):
            current = _row(rows, key("session", sid), _Session)
            if current.state == "ready_to_finalize":
                return ControlChanges(result={"state": "ready_to_finalize"})
            return ControlChanges(writes=(_write(key("session", sid), _updated(
                current, state="waiting", reason=command.reason), "replace"),),
                result={"state": "waiting", "reason": command.reason})
        return aw.ToolReceipt(**self._command(session, "stopped", command, decide).payload["result"])

    def checkpoint(self, session, command):
        from .behavior_assistant_checkpoint import unpack
        unpack(command.archive)
        sid = session.assignment.session_id
        if command.archive.sdk_session_id != session.assignment.sdk_session_id:
            raise Problem(403, "ASSISTANT_CHECKPOINT_INVALID", "SDK session changed")
        checkpoint = aw.Checkpoint(session_id=sid, task_digest=_digest(session.assignment.task.model_dump(mode="json")),
                                   archive_digest=command.archive.sha256)
        def decide(rows):
            current = _row(rows, key("session", sid), _Session)
            if current.state != "waiting":
                raise Problem(409, "ASSISTANT_CHECKPOINT_INVALID", "only explicitly stopped unfinished work may checkpoint")
            return ControlChanges(writes=(
                _write(key("archive", sid), command.archive, "create", material(command.archive.model_dump(mode="json"))),
            ), result={"checkpoint": checkpoint.model_dump(mode="json")})
        record = self._command(session, "checkpoint", command, decide)
        return aw.CheckpointReceipt.model_validate(record.payload["result"])

    def archive(self, session, *, restored=False):
        from .behavior_assistant_checkpoint import unpack
        archive_key = key("restore" if restored else "archive", session.assignment.session_id)
        rows = _read_conflicts(lambda: self.store.read_snapshot((archive_key,)), phase="checkpoint_archive")
        archive = _row(rows, archive_key, aw.SdkArchive)
        if _parse(rows[archive_key].payload["material"], InlineMaterial) != material(archive.model_dump(mode="json")):
            raise Problem(403, "ASSISTANT_CHECKPOINT_INVALID", "retained SDK bytes differ")
        unpack(archive)
        return archive

    def restore(self, session, principal):
        # History can contain prior tool results. Revalidate applicability/ACL
        # before allowing those bytes back into a replacement SDK process.
        self.current_model_capacity(session)
        if (session.assignment.task.instruction is not None
                and session.assignment.task.instruction.prompt_profile == REVIEWED_NOTE_PROFILE):
            from .behavior_reviewed_note_source import current_sources
            current_sources(self, session, principal)
        if session.assignment.task.instruction is not None and session.assignment.task.instruction.previous_archive_id:
            from .behavior_instruction import restore_turn
            return restore_turn(self, session, principal)
        self.model_sources(session, principal)
        return self.archive(session, restored=True)

    def resident_restore(self, session, command, principal):
        """Same prior-turn revalidation as restore; the resident SDK keeps its bytes (ADR-0005 PoC)."""
        task = session.assignment.task.instruction
        if task is None or task.worker_profile is None or task.previous_archive_id is None:
            raise Problem(403, "RESIDENT_WORKER_DISABLED", "only a resident-profile follow-up may keep its SDK")
        self.current_model_capacity(session)
        if task.prompt_profile == REVIEWED_NOTE_PROFILE:
            from .behavior_reviewed_note_source import current_sources
            current_sources(self, session, principal)
        from .behavior_instruction import restore_turn
        return restore_turn(self, session, principal, resident=command)

    def model_sources(self, session, principal):
        from .behavior_work_policy import refresh_once
        # Read-only: every source still authorizes and reads its rows; each binding's registry refresh runs once.
        with refresh_once():
            return self._model_sources(session, principal)

    def _model_sources(self, session, principal):
        self.current_model_capacity(session)
        historical = {}
        if session.assignment.task.instruction is not None and session.assignment.task.instruction.text_attachment_profile is not None:
            from .behavior_attachment_source import current_sources
            historical.update(current_sources(self, session, principal)[1])
        if session.assignment.task.instruction is not None and session.assignment.task.instruction.sop_copy is not None:
            from .behavior_skill_copy_source import current
            historical.update(current(self, session, principal))
        if (session.assignment.task.instruction is not None
                and skill_draft_enabled(session.assignment.task.instruction.prompt_profile,
                                        session.assignment.task.instruction.capabilities)):
            from .behavior_skill_draft_source import current
            historical.update(current(self, session, principal))
        if session.work_memory_source is not None or (session.assignment.task.instruction is not None
                and session.assignment.task.instruction.prompt_profile == WORK_MEMORY_PROFILE):
            from .behavior_work_memory_source import current_sources
            historical.update(current_sources(self, session, principal))
        if (session.assignment.task.instruction is not None
                and session.assignment.task.instruction.prompt_profile == REVIEWED_NOTE_PROFILE):
            from .behavior_reviewed_note_source import current_sources
            historical.update(current_sources(self, session, principal))
        if session.user_turn_sources or (session.assignment.task.instruction is not None
                                        and session.assignment.task.instruction.prompt_profile in USER_INPUT_READ_PROFILES):
            from .behavior_user_turn_source import current_sources
            historical.update(current_sources(self, session, principal))
        if session.skill_catalog is not None or (
                session.assignment.task.instruction is not None
                and skill_access(session.assignment.task.instruction.prompt_profile,
                                 session.assignment.task.instruction.capabilities)):
            self.current_skills(session)
        snapshot = (historical if session.assignment.task.instruction is not None and not session.context_read
                    else {**historical, **self._reviewed(session, principal)[2]})
        if len(set((*self._keys(session), *snapshot))) > 96:
            raise Problem(413, "USER_TURN_SOURCE_LIMIT", "complete source authorization exceeds the atomic read-set bound")
        return snapshot

    def attached_text(self, session, command, principal):
        from .behavior_attachment_source import current_sources
        # The route's current_session already authorized this principal for this request.
        task = session.assignment.task.instruction
        if task is None or task.text_attachment_profile is None:
            raise Problem(403, "ATTACHMENTS_DISABLED", "the original task does not enable source attachments")
        return current_sources(self, session, principal)[0]

    def current_model_capacity(self, session):
        task = session.assignment.task
        if task.instruction is None:
            return None
        profile = self.runtime.profiles[session.binding_id]
        capacity = task.instruction.model_capacity
        if profile.model_capacity_profile is None and capacity is None:
            return None
        if (capacity is None or profile.model_capacity_profile != capacity.profile
                or task.profile_digest != _digest(profile.model_dump(mode="json"))):
            raise Problem(403, "MODEL_CAPACITY_DISABLED", "only the original server profile may select model capacity")
        if self.runtime.work_policy is None:
            raise Problem(403, "MODEL_CAPACITY_DISABLED", "model capacity requires its original registered instruction")
        current = self.runtime.work_policy.models.capacity_snapshot(task.instruction.model)
        if current != capacity:
            raise Problem(403, "MODEL_CAPACITY_CHANGED", "declared model capacity changed; the original task is not rewritten")
        return capacity

    def read_user_turn(self, session, command, principal):
        from .behavior_user_turn_source import read_user_turn
        return read_user_turn(self, session, command, principal)

    def read_user_file(self, session, command, principal):
        from .behavior_user_turn_source import read_user_file
        return read_user_file(self, session, command, principal)

    def read_assistant_reply(self, session, command, principal):
        from .behavior_user_turn_source import read_assistant_reply
        return read_assistant_reply(self, session, command, principal)

    def work_memory_catalog(self, session, command, principal):
        from .behavior_work_memory_source import catalog
        return catalog(self, session, command, principal)

    def copy_sop_draft(self, session, command, principal):
        from .behavior_skill_copy_source import copy_draft
        return copy_draft(self, session, command, principal)

    def save_skill_draft(self, session, command, principal):
        from .behavior_skill_draft_source import save_draft
        return save_draft(self, session, command, principal)

    def read_work_memory(self, session, command, principal):
        from .behavior_work_memory_source import read
        return read(self, session, command, principal)

    def reviewed_note_catalog(self, session, command, principal):
        from .behavior_reviewed_note_source import catalog
        return catalog(self, session, command, principal)

    def read_reviewed_note(self, session, command, principal):
        from .behavior_reviewed_note_source import read
        return read(self, session, command, principal)

    def _skill_scope(self, session):
        binding = self._binding(session)
        profile = self.runtime.profiles[session.binding_id]
        task = session.assignment.task
        if (task.instruction is None
                or not skill_access(task.instruction.prompt_profile, task.instruction.capabilities)
                or profile.prompt_profile != task.instruction.prompt_profile
                or task.profile_digest != _digest(profile.model_dump(mode="json"))):
            raise Problem(403, "SKILL_ACCESS_DISABLED", "the original server profile does not enable skill sources")
        actor = self.actors.get(binding.agent_oid)
        if (actor is None or actor.oid != binding.agent_oid or actor.team != binding.team_id
                or self.artifacts.context.auth.tenant_id != binding.tenant_id):
            raise Problem(403, "SKILL_IDENTITY_DENIED", "the original assignment needs its existing artifact Actor")
        try:
            self.artifacts.authorize(actor, binding.team_id)
        except Problem as error:
            if error.status != 403 or error.body["error"]["code"] not in {
                "ACTOR_NOT_ALLOWED", "CROSS_TEAM_DENIED", "M3_TEAM_DENIED", "M3_IDENTITY_DENIED",
            }:
                raise
            raise Problem(403, "SKILL_IDENTITY_DENIED",
                          "the original artifact Actor no longer has current Team access") from None
        return actor, binding.team_id, profile.skill_artifacts

    def _configured_skills(self, session):
        task = session.assignment.task.instruction
        # A captured catalog keeps its schema, so a session started before this rule still matches its reads.
        schema = (session.skill_catalog.schema_version if session.skill_catalog is not None
                  else "fa-approved-skill-catalog-v2" if skill_purpose_access(task.prompt_profile, task.capabilities)
                  else "fa-approved-skill-catalog-v1")
        return self.skills.catalog(*self._skill_scope(session), schema_version=schema)

    def current_skills(self, session):
        # Every caller's request already passed _gate (route) or passes it under CAS before any write.
        if session.state != "running" or session.skill_catalog is None:
            raise Problem(403, "SKILL_ACCESS_DISABLED", "only an active skill-enabled assignment may read guidance")
        catalog = self._configured_skills(session)
        if catalog != session.skill_catalog:
            raise Problem(403, "SKILL_SOURCE_CHANGED", "original approved catalog changed; no cached guidance is authorized")
        return catalog

    def skill_catalog(self, session, command, principal):
        # The route's current_session already authorized this principal and gated the session.
        return self.current_skills(session)

    def skill_catalog_presentation(self, session, command, principal):
        catalog = self.current_skills(session)
        task = session.assignment.task.instruction
        profile = self.runtime.profiles[session.binding_id]
        if (task is None or task.skill_catalog_budget_profile != "fa-skill-catalog-budget-v1"
                or profile.skill_catalog_budget_profile != task.skill_catalog_budget_profile):
            raise Problem(403, "SKILL_CATALOG_BUDGET_DISABLED", "only the original server profile enables catalog budgeting")
        capacity = self.current_model_capacity(session)
        if capacity is None:
            raise Problem(503, "SKILL_CATALOG_CAPACITY_UNKNOWN", "the original declared capacity is required")
        return present_catalog(catalog, capacity, task.content, self.runtime.dep.memory._count)

    def read_skill(self, session, command, principal):
        self.runtime.authorize(session.binding_id, principal, "execute")
        catalog = self.current_skills(session)
        selected = next((entry for entry in catalog.entries if entry.name == command.name), None)
        if selected is None:
            raise Problem(403, "SKILL_SOURCE_DENIED", "only an entry in the original approved catalog can be read")
        result = self.skills.read(*self._skill_scope(session), selected,
            include_resources=session.assignment.task.instruction.prompt_profile == SKILL_RESOURCES_PROFILE)
        self.runtime.authorize(session.binding_id, principal, "execute")
        self.current_skills(session)
        return result

    def read_skill_resource(self, session, command, principal):
        self.runtime.authorize(session.binding_id, principal, "execute")
        task = session.assignment.task.instruction
        if task is None or task.prompt_profile != SKILL_RESOURCES_PROFILE:
            raise Problem(403, "SKILL_ACCESS_DISABLED", "the original profile does not enable manifest resources")
        catalog = self.current_skills(session)
        selected = next((entry for entry in catalog.entries if entry.name == command.name), None)
        if selected is None:
            raise Problem(403, "SKILL_SOURCE_DENIED", "only an original approved skill can supply resource references")
        actor, team, _ = self._skill_scope(session)
        result = self.artifacts.skill_resource(actor, team, selected.name, selected.version_id, command.resource)
        if result.skill_metadata_sha256 != selected.metadata_sha256:
            raise Problem(403, "SKILL_SOURCE_CHANGED", "resource manifest differs from the original approved catalog")
        self.runtime.authorize(session.binding_id, principal, "execute")
        self.current_skills(session)
        return result

    def save_text_reply(self, session, command, principal):
        from .behavior_instruction import save_reply
        if session.assignment.task.instruction is not None and (
            session.assignment.task.instruction.sop_copy is not None
            or skill_draft_enabled(session.assignment.task.instruction.prompt_profile,
                                   session.assignment.task.instruction.capabilities)
        ):
            self.runtime.authorize(session.binding_id, principal, "read")
        return save_reply(self, session, command)

    def validate_native_start(self, start, grant, binding_id):
        binding = self.runtime.bindings.get(binding_id)
        if (binding_id not in self.runtime.profiles or binding is None
                or self.runtime.job_input(binding) != start.input or grant.definition != binding.work
                or grant.checkpoint_after_bytes is not None):
            raise Problem(403, "ASSISTANT_NATIVE_GRANT", "exact assistant input, definition and native grant are required")

    def native_claim(self, binding_id, launch_id, command, principal):
        native = self.runtime.dep.native_launches
        if native is None or native.assistant is not self:
            raise Problem(403, "ASSISTANT_NATIVE_DISABLED", "native execution must use its exact registered authority")
        def validate():
            self.runtime.authorize(binding_id, principal, "execute")
            launch = native.launch(launch_id)
            native._reporter(launch, command, principal)
            native._current(self.store.read_snapshot(native._keys(launch)), launch, dispatch=True)
            if launch.api_binding_id != binding_id or launch.workload_version != aw.PROFILE:
                raise Problem(403, "ASSISTANT_NATIVE_DENIED", "native claim is not assigned this assistant profile")
            return launch
        launch = _read_conflicts(lambda: self.store.consistent_read(validate, max_keys=None), phase="native_claim")
        assignment, ticket = self.issue(launch.start)
        if ticket is None:
            raise Problem(409, "ASSISTANT_NATIVE_UNKNOWN", "original assignment ticket was already issued")
        return assignment, ticket

    def native_report_plan(self, launch, command, principal):
        sid = key("session-id", launch.start.binding.start_request_id)
        session_key = key("session", sid)
        session = _row(self.store.read_snapshot((session_key,)), session_key, _Session)
        stop = command.sdk_stop
        instruction_task = session.assignment.task.instruction
        released = (stop is not None and stop.release is not None and instruction_task is not None
                    and stop.release == instruction_task.worker_profile)
        # A resident release keeps the runtime alive: no exit, but the same owned pid and a released turn.
        if (session.execution != launch.start.binding or stop is None
                or stop.pid != command.child_pid or stop.exit_code != command.exit_code
                or command.child_exited == released or stop.release is not None and not released
                or released and command.outcome == "parked"
                or not stop.graceful):
            raise Problem(403, "ASSISTANT_NATIVE_IDLE", "gracefully stopped exact owned SDK runtime is required")
        instruction=session.assignment.task.instruction is not None
        if command.outcome == "succeeded" or instruction and command.outcome in {"failed","stopped"}:
            result = self.result(launch.start.binding)
            if instruction and result.state!={"succeeded":"succeeded","failed":"failed","stopped":"cancelled"}[command.outcome]:
                raise Problem(403,"ASSISTANT_NATIVE_RESULT","reported SDK exit and saved work outcome differ")
            expected = aw.ExecutionResult(session_id=sid, result_digest=_digest(result.model_dump(mode="json")))
            if command.result != expected or command.checkpoint is not None or session.state != "ready_to_finalize":
                raise Problem(403, "ASSISTANT_NATIVE_RESULT", "SDK turn end is not a durable finish receipt")
            snapshot = {} if instruction else self.model_sources(session,principal)
            result_key, effect_key = key("result", launch.start.binding.work_id), key("artifact", launch.start.binding.work_id)
            if session.assignment.task.instruction is not None:
                from .behavior_instruction import TurnArchive
                effect_key = key("text-archive",launch.start.binding.job_id)
                saved = _row(self.store.read_snapshot((effect_key,)),effect_key,TurnArchive)
                if (saved.sdk_stop != stop or isinstance(saved.archive,(aw.SdkArchive,aw.StoredSdkArchive))
                        and saved.archive.sdk_session_id != session.assignment.sdk_session_id):
                    raise Problem(403,"ASSISTANT_NATIVE_RESULT","original reply and archive must bind the same stopped SDK")
            binding = self._binding(session)
            peer_work = instruction and session.assignment.task.instruction.peer_work is not None
            if peer_work:
                from .behavior_work_policy import WorkRegistration, work_policy_key
                registered = self.runtime.work_policy.records[binding.binding_id]
                work_key = work_policy_key("registration", registered.admission_id)
            else:
                work_key = control_key("delivery-work-state", binding.obligation_id)
            snapshot = {**snapshot, **self.store.read_snapshot((session_key, result_key, effect_key, work_key))}
            if _row(snapshot, session_key, _Session) != session:
                raise Problem(409, "ASSISTANT_RESULT_CHANGED", "result session changed during verification")
            definition = (_digest(_row(snapshot, work_key, WorkRegistration).binding.work.model_dump(mode="json"))
                          if peer_work else _row(snapshot, work_key, _Work).definition_digest)
            if definition != result.definition_digest:
                raise Problem(409, "ASSISTANT_WORK_CHANGED", "native completion no longer matches current work")
            return result, None, snapshot
        if command.outcome == "parked":
            checkpoint = aw.Checkpoint.model_validate_json(command.checkpoint)
            archive = self.archive(session)
            if (session.state != "waiting" or command.result is not None
                    or checkpoint.session_id != sid or checkpoint.archive_digest != archive.sha256
                    or checkpoint.task_digest != _digest(session.assignment.task.model_dump(mode="json"))):
                raise Problem(403, "ASSISTANT_NATIVE_CHECKPOINT", "exact stopped SDK checkpoint is required")
            snapshot = self.store.read_snapshot((session_key, key("archive", sid)))
            return None, command.checkpoint, snapshot
        raise Problem(403, "ASSISTANT_NATIVE_UNKNOWN", "unfinished or unverified SDK exit retains execution responsibility")

    def no_sdk_report_context(self, launch, command):
        from .behavior_instruction_no_sdk import keys
        sid = key("session-id", launch.start.binding.start_request_id)
        skey = key("session", sid)
        session = _row(self.store.read_snapshot((skey,)), skey, _Session)
        disposition = command.sdk_no_start
        owner = None if disposition is None else disposition.gate.owner
        if (session.execution != launch.start.binding or session.assignment.sdk_start_binding is None
                or disposition is None or disposition.gate.binding != session.assignment.sdk_start_binding
                or owner.kind != "hosted" or owner.launch_id != launch.launch_id
                or owner.claim_id != launch.claim_id or owner.supervisor_id != launch.process_id
                or owner.process_generation != launch.process_generation):
            raise Problem(403, "SDK_NO_START_SCOPE", "only the original enabled native observer may report this closed gate")
        return session, keys(self, session)

    def verify_native_report_snapshot(self, rows, snapshot):
        self._current_sources(rows, snapshot)


class LocalAssistantExecutor:
    """OFFLINE ONLY: real pinned SDK, typed HTTP client and durable authority.

    Its process stop evidence is confined to this explicitly local fixture;
    NativeLaunchAuthority must be used for a real Hosted deployment.
    """

    def __init__(self, authority, http_client, *, agent_headers, root: Path, runtime_path: Path, model_transport):
        if authority.runtime.mode != "offline_contract":
            raise Problem(403, "ASSISTANT_OFFLINE_ONLY", "local SDK execution cannot provide native evidence")
        self.authority, self.http_client, self.agent_headers = authority, http_client, agent_headers
        self.root, self.runtime_path, self.model_transport = root, runtime_path, model_transport
        # Optional hosted.ResidentPool for resident-profile tasks; tests replace it to model a lost worker.
        self.resident = None

    def adapter(self):
        return ExecutorAdapter(self.start, self.query, self.stop, self.verify)

    def start(self, start):
        from .behavior_assistant_hosted import AssistantClient, AssistantSession
        from .m2_assistant import AssistantEngine
        assignment, ticket = self.authority.issue(start)
        if ticket is None:
            return self.query(start.binding)
        transport = _LocalHttp(self.http_client)
        client = AssistantClient(
            "https://context.invalid", assignment, ticket,
            token_provider=lambda: self.agent_headers()["Authorization"].removeprefix("Bearer "),
            transport=transport,
        )
        try:
            if assignment.sdk_start_binding is not None:
                return self._start_with_gate(start, client)
            bridge = AssistantSession(client, root=self.root / assignment.session_id,
                                      runtime_path=self.runtime_path, model_transport=self.model_transport,
                                      resident=self.resident)
            engine = AssistantEngine(None, process_marker=assignment.session_id, behavior=bridge)
            outcome = engine.dispatch({"operation": "assistant", "profile": aw.PROFILE, "action": "start"})
            return self._complete(start, outcome)
        finally:
            client.close()

    def _start_with_gate(self, start, client):
        from threading import Thread
        from uuid import uuid4
        from .behavior_assistant_hosted import AssistantSession
        from .behavior_hosted import LocalJournal
        from .behavior_instruction_no_sdk import finish_local
        root = self.root / client.assignment.session_id
        journal = LocalJournal(root)
        owner = wire.SdkStartOwner(kind="local-offline", supervisor_id=uuid4().hex)
        observed = {}

        def execute():
            try:
                bridge = AssistantSession(client, root=root, runtime_path=self.runtime_path,
                    model_transport=self.model_transport, sdk_journal=journal, sdk_owner=owner,
                    resident=self.resident)
                observed["bridge"] = bridge
                observed["outcome"] = bridge.dispatch({"operation": "assistant", "profile": aw.PROFILE, "action": "start"})
            except Exception as error:
                observed["error"] = error

        thread = Thread(target=execute, daemon=False, name="fa-local-sdk-" + client.assignment.session_id[:12])
        thread.start()
        thread.join()
        if "outcome" in observed:
            return self._complete(start, observed["outcome"])
        bridge = observed.get("bridge")
        disposition = None if bridge is None else bridge._sdk_start_gate.observe_closed()
        if disposition is not None:
            skey = key("session", client.assignment.session_id)
            session = _row(self.authority.store.read_snapshot((skey,)), skey, _Session)
            outcome = finish_local(self.authority, session, disposition)
            return self._complete(start, outcome, no_sdk=disposition)
        if "error" in observed:
            raise observed["error"]
        raise Problem(503, "ASSISTANT_START_UNKNOWN", "owned local dispatch has no terminal observation")

    def _complete(self, start, outcome, *, no_sdk=None):
        eid = key("local-evidence", start.binding.start_request_id)
        if outcome["state"] != "ready_to_finalize":
            evidence = ExecutorEvidence(
                evidence_id=eid, binding=start.binding, sequence=1, native_execution_id=start.binding.start_request_id,
                state="parked", entire_tree_idle=True, all_inflight_starts_fenced=True,
                checkpoint=_json(outcome["checkpoint"]),
                proof=b"OFFLINE SDK graceful stop and protected checkpoint; unfinished work is not success",
            )
        else:
            result=self.authority.result(start.binding)
            evidence = ExecutorEvidence(
                evidence_id=eid, binding=start.binding, sequence=1, native_execution_id=start.binding.start_request_id,
                state="stopped" if result.state=="cancelled" else result.state, entire_tree_idle=True, all_inflight_starts_fenced=True,
                result=result,
                proof=(material({"schema_version": "fa-local-no-sdk-evidence-" + no_sdk.gate.binding.version,
                                 "disposition": no_sdk.model_dump(mode="json")}).data if no_sdk is not None else
                       b"OFFLINE real SDK graceful stop; builtins/MCP/child agents disabled; not Hosted evidence"),
            )
        request = ControlRequest(request_id=key("local-complete", eid), request_digest=_digest(evidence.model_dump(mode="json")))
        self.authority.store.transact(request, lambda _: ControlChanges(
            writes=(_write(eid, evidence, "create", material(evidence.model_dump(mode="json"))),), result={"evidence_id": eid}))
        return eid

    def query(self, binding):
        return key("local-evidence", binding.start_request_id)

    def verify(self, evidence_id):
        return _row(self.authority.store.read_snapshot((evidence_id,)), evidence_id, ExecutorEvidence)

    def stop(self, command):
        return self.query(command.binding)

    def close(self):
        pass


class _LocalHttp:
    def __init__(self, client):
        self.client = client

    def handle_request(self, request):
        import httpx
        response = self.client.request(request.method, request.url.raw_path.decode(),
                                       content=request.content, headers=dict(request.headers))
        return httpx.Response(response.status_code, headers=response.headers, content=response.content, request=request)

    def close(self):
        pass
