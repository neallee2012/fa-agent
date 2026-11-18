"""Work indexes and original SDK archives; the SDK remains the conversation authority."""
from typing import Annotated, Literal
from datetime import datetime
import hashlib
import zlib

from pydantic import Field, field_validator, model_validator

from . import behavior_assistant_wire as aw
from .behavior_delivery import _Model, _parse, _material, _utc
from .behavior_operations import control_key, conversation_key
from .errors import Problem
from .models import Identifier
from .behavior_skill_prompt import AssemblyRecord
from .behavior_capability_prompt import CapabilityAssemblyRecord
from .m3_skills import ApprovedSkillCatalog
from .behavior_user_turn import UserTurnSources, ordered_user_sources
from .behavior_work_memory import WorkMemorySource
from .behavior_skill_copy import SopCopyOutcome, SopCopyReceipt
from .behavior_skill_draft import SkillDraftOutcome, SkillDraftReceipt
from .behavior_agent_worker import AgentVmBinding


def conversation_id(policy, command):
    return command.conversation_id or control_key(
        "assistant-conversation-id", *policy.scope_ids, policy.template.human_oid, command.request_id)


def sdk_session_id(identifier):
    return control_key("assistant-sdk-conversation", identifier)


def hosted_session_id(identifier):
    return "fa-chat-" + control_key("assistant-hosted-conversation", identifier)[:48]


class Conversation(_Model):
    kind: Literal["assistant_sdk_conversation"] = "assistant_sdk_conversation"
    conversation_id: Identifier
    human_oid: Identifier
    team_id: Identifier
    agent_oid: Identifier
    sdk_session_id: Identifier
    jobs: tuple[Identifier, ...] = ()
    active_job_id: Identifier | None = None
    archive_id: Identifier | None = None
    hosted_session_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    hosted_agent_version: str | None = Field(default=None, exclude_if=lambda value: value is None)
    reset_request_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    archived_at: datetime | None = Field(default=None, exclude_if=lambda value: value is None)
    agent_vm: AgentVmBinding | None = Field(default=None, exclude_if=lambda value: value is None)

    _time = field_validator("archived_at")(lambda value: None if value is None else _utc(value))

    @model_validator(mode="after")
    def archive_pair(self):
        if (self.reset_request_id is None) != (self.archived_at is None):
            raise ValueError("an archived conversation requires its exact reset request and time")
        if self.agent_vm is not None and (
                self.agent_vm.team_id != self.team_id or self.agent_vm.hosted_session_id != self.hosted_session_id
                or self.agent_vm.hosted_agent_version != self.hosted_agent_version):
            raise ValueError("a conversation must retain its originally captured Agent VM")
        return self


class TurnArchive(_Model):
    kind: Literal["assistant_sdk_turn_archive"] = "assistant_sdk_turn_archive"
    conversation_id: Identifier
    job_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]
    archive: aw.SdkArchive | aw.ArchiveUnavailable | aw.StoredSdkArchive
    sdk_stop: aw.AssistantSdkStop
    context_read: bool = False
    failure: aw.InstructionFailure | None = None
    progress: aw.PublicProgress | None = Field(default=None,exclude_if=lambda v:v is None)
    prompt_assembly: AssemblyRecord | None = Field(default=None,exclude_if=lambda v:v is None)
    capability_assembly: CapabilityAssemblyRecord | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_catalog: ApprovedSkillCatalog | None = Field(default=None, exclude_if=lambda value: value is None)
    user_turn_sources: UserTurnSources = Field(default=(), exclude_if=lambda value: not value)
    work_memory_source: WorkMemorySource | None = Field(default=None, exclude_if=lambda value: value is None)
    sop_copy_receipt: SopCopyReceipt | None = Field(default=None, exclude_if=lambda value: value is None)
    sop_copy_outcome: SopCopyOutcome | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_draft_receipt: SkillDraftReceipt | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_draft_outcome: SkillDraftOutcome | None = Field(default=None, exclude_if=lambda value: value is None)

    _user_sources = field_validator("user_turn_sources")(ordered_user_sources)


def turn_material(value):
    metadata=value.model_dump(mode="json")
    metadata["archive"].pop("data",None)
    return _material(metadata)


class SavedReply(_Model):
    kind:Literal["assistant_text_reply"]="assistant_text_reply"
    job_id:Identifier
    data:Annotated[bytes,Field(max_length=32768)]
    sha256:Annotated[str,Field(pattern=r"^[a-f0-9]{64}$")]
    byte_length:Annotated[int,Field(ge=1,le=4*1024*1024)]


def unpack_reply(value):
    decoder=zlib.decompressobj()
    raw=decoder.decompress(value.data,4*1024*1024+1)
    if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or len(raw)!=value.byte_length
            or hashlib.sha256(raw).hexdigest()!=value.sha256):
        raise Problem(409,"INSTRUCTION_REPLY_INVALID","complete saved response bytes differ")
    return aw.TextReply.model_validate_json(raw)


def reply_reference(value):
    from .behavior_assistant import key
    return _material({"schema_version":"fa-assistant-text-reference-v1","reply_id":key("reply",value.job_id),
                      "sha256":value.sha256,"byte_length":value.byte_length}).data


def failure_notice(state, failure=None):
    notice = {"schema_version": "fa-assistant-notice-v1", "state": state, "sdk_reply_available": False}
    if failure == "model_call_limit_reached":
        notice["code"] = "MODEL_CALL_LIMIT_REACHED"
    return _material(notice).data


def previous_turn(authority, session):
    from .behavior_assistant import key
    task = session.assignment.task.instruction
    if task is None or task.previous_archive_id is None:
        raise Problem(409, "CONVERSATION_CONTEXT_MISSING", "original prior SDK archive is required")
    row = authority.store.read_snapshot((task.previous_archive_id,))[task.previous_archive_id]
    if row is None:
        raise Problem(409, "CONVERSATION_CONTEXT_MISSING", "original SDK archive is unavailable; do not start empty")
    saved = _parse(row.payload["data"], TurnArchive)
    if not isinstance(saved.archive,(aw.SdkArchive,aw.StoredSdkArchive)):
        raise Problem(409,"CONVERSATION_CONTEXT_MISSING","saved SDK context is unavailable; do not start empty")
    from .behavior_conversation_transfer import permits_archive_gap
    control = authority.runtime.work_policy
    record = None if control is None else control._job_record(session.assignment.task.job_id)
    gap_allowed = control is not None and permits_archive_gap(control, record, saved)
    if (saved.conversation_id != task.conversation_id or saved.turn_number != task.turn_number - 1 and not gap_allowed
            or task.previous_archive_id != key("text-archive", saved.job_id)
            or saved.archive.sdk_session_id != session.assignment.sdk_session_id
            or row.payload["material"] != turn_material(saved).model_dump(mode="json")):
        raise Problem(409, "CONVERSATION_CONTEXT_CHANGED", "original SDK context differs")
    return saved, row

