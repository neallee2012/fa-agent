"""Opt-in assistant DTOs. No storage, JWT, owner, clock or endpoint selection."""

from typing import Annotated, Literal

import json
from datetime import datetime
from pydantic import Field, JsonValue, TypeAdapter, field_validator, model_validator

from .behavior_wire import (
    AssistantExecutionResult, AssistantSdkStop, Command, Digest, ExactSourceRef, Identifier, ModelDeployment,
    SdkStartBinding, SdkStartProfile, WireModel, WorkerProfile,
)
from .behavior_sdk_state import StoredSdkArchive, CheckpointProfile, ModelProvider, validate_provider_checkpoint
from .behavior_skill_prompt import InstructionPromptProfile as PromptProfile, SKILL_PROFILES
from .behavior_skill_catalog import CatalogBudgetProfile
from .behavior_model_capacity import ModelCapacitySnapshot
from .behavior_work_memory_prompt import WORK_MEMORY_PROFILE
from .behavior_skill_copy import SOP_COPY_PROFILE, TeamSopCopy
from .m3_models import Filename
from .behavior_attachments import TextAttachmentProfile, TextAttachments, validate_attachments
from .behavior_turn_origin import InstructionTurnOrigin, TurnOriginProfile
from .behavior_text_files import UserTextFileProfile, UserTextFiles, validate_user_files
from .behavior_user_images import UserImages, validate_user_images
from .behavior_user_file_prompt import FILE_READ_PROFILES
from .behavior_capabilities import CAPABILITY_BASE_PROFILES, CapabilityCapture, TurnGrants, skills_granted
from .behavior_agent_role_prompt import RolePromptCapture, role_text_mismatch
from .behavior_delegation_wire import DelegationModelRef
from .behavior_agent_message_wire import AgentMessageRecipient

PROFILE = "assistant-v1.2"
CHECKS = ("answer_verified", "artifact_verified", "finish_verified")
TEXT_CHECKS = ("sdk_response_saved", "sdk_turn_completed")


class AssistantSubmit(Command):
    content: Literal[
        "Use the reviewed metrics to compute remaining items and save my result."
    ] = "Use the reviewed metrics to compute remaining items and save my result."
    authorize_own_result: Literal[True] = True


class InstructionSubmit(Command):
    content: Annotated[str, Field(min_length=1)]
    authorize_own_result: Literal[True] = True
    conversation_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    model: ModelDeployment | None = Field(default=None, exclude_if=lambda value: value is None)
    attachments: TextAttachments = Field(default=(), exclude_if=lambda value: not value)
    files: UserTextFiles = Field(default=(), exclude_if=lambda value: not value)
    # Native turns only (B1 PA-08); omitted when empty, so earlier commands keep their digest.
    images: UserImages = Field(default=(), exclude_if=lambda value: not value)

    _attachments = field_validator("attachments")(validate_attachments)
    _images = field_validator("images")(validate_user_images)

    @model_validator(mode="after")
    def original_file_input(self):
        validate_user_files(self.content, self.files, reference_names=tuple(item.name for item in self.attachments))
        return self

    @field_validator("content")
    @classmethod
    def transport_text(cls, value):
        if not value.strip() or len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 8192:
            raise ValueError("nonblank task text must fit the original task transport")
        return value


class PeerWorkReference(WireModel):
    schema_version: Literal["fa-agent-peer-work-v1"] = "fa-agent-peer-work-v1"
    root_id: Identifier
    message_id: Identifier
    message_key: Identifier
    message_sha256: Digest
    content_sha256: Digest
    sender_agent_id: Identifier
    recipient_agent_id: Identifier
    recipient_work_id: Identifier
    conversation_id: Identifier
    source_kind: Literal["agent"] = "agent"
    authorization: Literal[False] = False


class InstructionTask(WireModel):
    content: str
    conversation_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]
    previous_archive_id: Identifier | None = None
    model: ModelDeployment | None = None
    requested_model: ModelDeployment | None = None
    hosted_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    checkpoint_profile: CheckpointProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    model_provider: ModelProvider | None = Field(default=None, exclude_if=lambda value: value is None)
    reasoning_summary: Literal["detailed"] | None = Field(default=None, exclude_if=lambda value: value is None)
    prompt_profile: PromptProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    sdk_start_profile: SdkStartProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    model_capacity: ModelCapacitySnapshot | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_catalog_budget_profile: CatalogBudgetProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    sop_copy: TeamSopCopy | None = Field(default=None, exclude_if=lambda value: value is None)
    text_attachment_profile: TextAttachmentProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    turn_origin_profile: TurnOriginProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    worker_profile: WorkerProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    user_text_file_profile: UserTextFileProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    # A native turn passes its TurnGrants (selected capabilities plus Team connectors); only the selected part counts
    # toward the 12-capability limit. Pydantic's smart union keeps an in-memory TurnGrants (exact type) and, for JSON
    # both members accept, takes the first: a stored capture of at most 12 reads back as the CapabilityCapture it was.
    capabilities: CapabilityCapture | TurnGrants | None = Field(default=None, exclude_if=lambda value: value is None)
    peer_work: PeerWorkReference | None = Field(default=None, exclude_if=lambda value: value is None)
    role_prompt: RolePromptCapture | None = Field(default=None, exclude_if=lambda value: value is None)
    attachments: TextAttachments = Field(default=(), exclude_if=lambda value: not value)
    files: UserTextFiles = Field(default=(), exclude_if=lambda value: not value)

    _attachments = field_validator("attachments")(validate_attachments)

    @model_validator(mode="after")
    def attachment_profile(self):
        validate_provider_checkpoint(self.model_provider, self.checkpoint_profile)
        if self.reasoning_summary is not None and self.model_provider != "openai-responses":
            raise ValueError("Public reasoning summaries require the captured Responses provider")
        if self.attachments and self.text_attachment_profile is None:
            raise ValueError("Source attachments require the explicit original server profile")
        if self.turn_origin_profile is not None and self.prompt_profile is None:
            raise ValueError("Turn origin requires an explicit versioned instruction profile")
        if self.files and self.user_text_file_profile is None:
            raise ValueError("Literal input files require the original explicit user-text-file profile")
        if self.user_text_file_profile is not None and self.prompt_profile is None:
            raise ValueError("Literal input files require a versioned instruction profile")
        if self.prompt_profile in FILE_READ_PROFILES and self.user_text_file_profile is None:
            raise ValueError("The original-user-file profile requires explicit literal file input")
        validate_user_files(self.content, self.files, reference_names=tuple(item.name for item in self.attachments))
        return self

    @model_validator(mode="after")
    def capacity_model(self):
        if self.model_capacity is not None and self.model_capacity.model != self.model:
            raise ValueError("The immutable model capacity belongs to a different selected deployment")
        if (self.skill_catalog_budget_profile is not None and self.prompt_profile not in SKILL_PROFILES
                and not skills_granted(self.capabilities)):
            raise ValueError("Catalog budgeting requires an explicit approved-skill profile")
        return self

    @model_validator(mode="after")
    def start_profile(self):
        if self.sdk_start_profile == "fa-sdk-start-gate-v2" and self.prompt_profile != WORK_MEMORY_PROFILE:
            raise ValueError("SDK gate v2 belongs only to the reviewed-work-memory instruction profile")
        if self.worker_profile is not None and self.checkpoint_profile is not None:
            raise ValueError("The resident worker PoC keeps ordinary SDK checkpoints only")
        return self

    @model_validator(mode="after")
    def capability_base(self):
        if self.capabilities is not None and self.prompt_profile not in CAPABILITY_BASE_PROFILES:
            raise ValueError("Captured capabilities require a composable versioned instruction profile")
        if role_text_mismatch(self.prompt_profile, self.role_prompt) is not None:
            raise ValueError("The agent-role profile and its captured role text require each other")
        return self

    @model_validator(mode="after")
    def copy_scope(self):
        if (self.prompt_profile == SOP_COPY_PROFILE) != (self.sop_copy is not None):
            raise ValueError("A copy-only instruction requires its explicit immutable Team copy authorization")
        return self


class InstructionContext(WireModel):
    source: ExactSourceRef
    content: Annotated[str, Field(max_length=16384)]


class InstructionContextOrigin(InstructionContext):
    schema_version: TurnOriginProfile
    turn_origin: InstructionTurnOrigin


_INSTRUCTION_CONTEXT_RESULT = TypeAdapter(InstructionContext | InstructionContextOrigin)


def parse_instruction_context(raw: str) -> InstructionContext | InstructionContextOrigin:
    return _INSTRUCTION_CONTEXT_RESULT.validate_json(raw)


class PublicProgressEvent(WireModel):
    seq: Annotated[int,Field(ge=1)]
    at: datetime
    kind: Literal["stage","tool_started","tool_completed","error"]
    tool_name: Annotated[str,Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")] | None = None
    status: Literal["started","succeeded","failed","unknown"] | None = None
    summary: Annotated[str,Field(max_length=256)] | None = None
    source: ExactSourceRef | None = None
    tool_start_seq: Annotated[int, Field(strict=True, ge=1, le=2**53-1)] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    reasoning_summary: Annotated[str, Field(min_length=1, max_length=256)] | None = Field(
        default=None, exclude_if=lambda value:value is None)

    @model_validator(mode="after")
    def tool_correlation(self):
        if self.reasoning_summary is not None and self.kind != "stage":
            raise ValueError("A public reasoning summary belongs to an assistant stage")
        if self.tool_start_seq is not None and (
            self.tool_name is None
            or self.seq > 2**53-1
            or self.kind not in {"tool_started", "tool_completed"}
            or self.kind == "tool_started" and self.tool_start_seq != self.seq
            or self.kind == "tool_completed" and self.tool_start_seq >= self.seq
        ):
            raise ValueError("Tool correlation must refer to its earlier public start")
        return self


class PublicReasoningSummary(WireModel):
    availability: Literal["not_provided", "provided"] = "not_provided"
    source: Literal["copilot-sdk-1.0.13", "openai-responses"] = "copilot-sdk-1.0.13"
    text: Annotated[str, Field(min_length=1, max_length=256)] | None = None

    @model_validator(mode="after")
    def public_source(self):
        if (self.availability=="provided" and (self.text is None or self.source!="openai-responses")
                or self.availability=="not_provided" and (self.text is not None or self.source!="copilot-sdk-1.0.13")):
            raise ValueError("A provided summary requires its explicit public provider source")
        return self


class PublicProgress(WireModel):
    events: tuple[PublicProgressEvent,...] = ()
    reasoning_summary: PublicReasoningSummary = Field(default_factory=PublicReasoningSummary)

    @model_validator(mode="after")
    def visible_tool_origins(self):
        if any(event.tool_start_seq is not None for event in self.events):
            if [event.seq for event in self.events] != sorted({event.seq for event in self.events}):
                raise ValueError("Correlated public events must have distinct increasing sequences")
            visible = {event.seq: event for event in self.events}
            completed = set()
            for event in self.events:
                origin = visible.get(event.tool_start_seq)
                if origin is not None and (
                    origin.kind != "tool_started" or origin.tool_name != event.tool_name
                    or origin.tool_start_seq != event.tool_start_seq
                ):
                    raise ValueError("Visible tool correlation origin differs")
                if event.kind == "tool_completed" and event.tool_start_seq is not None:
                    if event.tool_start_seq in completed:
                        raise ValueError("A public tool call cannot complete twice in one snapshot")
                    completed.add(event.tool_start_seq)
        return self


class ProgressUpdate(Command):
    progress: PublicProgress


class Answer(WireModel):
    item_count: Annotated[int, Field(ge=0, le=1000000)]
    completed_count: Annotated[int, Field(ge=0, le=1000000)]
    remaining_count: Annotated[int, Field(ge=0, le=1000000)]


class Task(WireModel):
    profile: Literal["assistant-v1.2"] = PROFILE
    job_id: Identifier
    work_id: Identifier
    definition_digest: Digest
    profile_digest: Digest
    source: ExactSourceRef
    artifact_id: Identifier
    artifact_version: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    operation_id: Identifier
    instruction: InstructionTask | None = Field(default=None, exclude_if=lambda value: value is None)


class Assignment(WireModel):
    binding_id: Identifier
    session_id: Digest
    sdk_session_id: Identifier
    task: Task
    resume: bool = False
    sdk_start_binding: SdkStartBinding | None = Field(default=None, exclude_if=lambda value: value is None)
    agent_message_recipient: AgentMessageRecipient | None = Field(default=None, exclude_if=lambda value: value is None)


class Empty(WireModel):
    pass


class SkillName(WireModel):
    name: Identifier


class ReadSkill(Command):
    name: Identifier


class SkillResourceName(SkillName):
    resource: Filename


class ReadSkillResource(ReadSkill):
    resource: Filename


class Start(WireModel):
    operation: Literal["assistant"] = "assistant"
    profile: Literal["assistant-v1.2"] = PROFILE
    action: Literal["start", "resume", "status"]


class Save(Command):
    answer: Answer


class Reservation(Command):
    request_sha256: Digest


class ToolReceipt(WireModel):
    state: Literal["accepted", "running", "saved", "ready_to_finalize", "waiting", "unknown"]
    receipt_id: Digest | None = None
    artifact_sha256: Digest | None = None
    result_digest: Digest | None = None
    reason: Literal["finish_missing", "operation_unknown", "sdk_failed"] | None = None
    work_outcome: Literal["succeeded","failed","cancelled"] | None = Field(default=None,exclude_if=lambda v:v is None)


class ContextResult(WireModel):
    source: ExactSourceRef
    item_count: Annotated[int, Field(ge=0, le=1000000)]
    completed_count: Annotated[int, Field(ge=0, le=1000000)]


ExecutionResult = AssistantExecutionResult


class Checkpoint(WireModel):
    workload_version: Literal["assistant-v1.2"] = PROFILE
    session_id: Digest
    task_digest: Digest
    archive_digest: Digest
    state: Literal["waiting"] = "waiting"


class TurnStopped(Command):
    reason: Literal["finish_missing", "operation_unknown", "sdk_failed"]


class EffectResult(WireModel):
    operation_id: Identifier
    effect_state: Literal["unknown", "not_applied", "applied", "partial"]
    business_state: Literal["pending", "succeeded", "failed"]


class SdkArchive(WireModel):
    sdk_version: Literal["1.0.13"] = "1.0.13"
    runtime_version: Literal["1.0.83"] = "1.0.83"
    sdk_session_id: Identifier
    codec: Literal["zlib-v1"] = "zlib-v1"
    data: Annotated[bytes, Field(min_length=1, max_length=16000)]
    sha256: Digest


class SaveCheckpoint(Command):
    archive: SdkArchive


class TextReply(WireModel):
    schema_version: Literal["fa-assistant-text-result-v1"] = "fa-assistant-text-result-v1"
    text: Annotated[str, Field(min_length=1)]
    sdk_session_id: Identifier
    conversation_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def bounded_reply(self):
        if not self.text.strip():
            raise ValueError("a complete nonempty SDK reply is required")
        return self


class ArchiveUnavailable(WireModel):
    error: Literal["ASSISTANT_CHECKPOINT_INVALID", "ASSISTANT_CHECKPOINT_LIMIT", "SDK_ARCHIVE_UNAVAILABLE",
                   "SDK_STATE_INCOMPLETE", "SDK_STATE_UPLOAD_UNKNOWN", "SDK_STATE_PRIVATE_MATERIAL",
                   "SDK_STATE_UNSUPPORTED_FORMAT", "SDK_STATE_SOURCE_UNVERIFIED"]


InstructionFailure = Literal["cancelled", "sdk_failed", "model_call_limit_reached"]


class SaveTextReply(Command):
    reply: TextReply | None
    archive: SdkArchive | ArchiveUnavailable | StoredSdkArchive
    sdk_stop: AssistantSdkStop
    failure: InstructionFailure | None = Field(default=None,exclude_if=lambda v:v is None)
    progress: PublicProgress | None = Field(default=None,exclude_if=lambda v:v is None)


class CheckpointReceipt(WireModel):
    checkpoint: Checkpoint


class ResidentRestore(Command):
    """The resident SDK still holds the previous turn's released state; no archive bytes are transferred."""
    archive_sha256: Digest
    pid: Annotated[int, Field(ge=1)]


SdkStop = AssistantSdkStop


class NativeClaimReceipt(WireModel):
    assignment: Assignment


class ModelRequest(Command):
    body: dict[str, JsonValue]
    checkpoint_compaction: bool = Field(default=False, exclude_if=lambda value: not value)
    delegation: DelegationModelRef | None = Field(default=None, exclude_if=lambda value: value is None)
