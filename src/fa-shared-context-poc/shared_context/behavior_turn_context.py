"""Read-only, SDK-independent metadata; never a checkpoint or permission export."""

import hashlib
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from .backup_models import MAX_BACKUP_BYTES
from .behavior_agent_template import TemplateSkill
from .behavior_assistant_wire import ArchiveUnavailable, ModelDeployment, SdkArchive, StoredSdkArchive
from .behavior_model_capacity import ModelCapacitySnapshot
from .behavior_sdk_state import DIAG_ARCHIVE_CODECS
from .behavior_attachments import TextAttachments, validate_attachments
from .behavior_text_files import UserTextFileReference, file_reference
from .behavior_skill_prompt import AssemblyRecord, assemble_prompt
from .behavior_capability_prompt import CapabilityAssemblyRecord, capability_record
from .behavior_user_turn import MAX_USER_TURN_SOURCES
from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .m3_skills import PurposeSkillEntry

MAX_TURN_CONTEXT_BYTES = 16384
PositiveBytes = Annotated[int, Field(ge=1, le=MAX_BACKUP_BYTES)]


class ArchiveMetrics(WireModel):
    codec: Literal["zlib-v1", "artifact-session-fs-v1", "artifact-session-fs-diag-empty-v1",
                   "artifact-session-fs-responses-v1", "artifact-session-fs-responses-diag-v1"] | None
    sdk_version: Literal["1.0.13"] | None
    runtime_version: Literal["1.0.83"] | None
    sha256: Digest | None
    encoded_state_bytes: PositiveBytes | None
    inline_zlib_bytes: Annotated[int, Field(ge=1, le=16000)] | None
    part_count: Annotated[int, Field(ge=1, le=16)] | None
    unavailable: ArchiveUnavailable | None
    content_reverified: Literal[False]

    @model_validator(mode="after")
    def measurement_basis(self):
        if self.codec is None:
            valid = self.unavailable is not None and all(value is None for value in (
                self.sdk_version, self.runtime_version, self.sha256,
                self.encoded_state_bytes, self.inline_zlib_bytes, self.part_count))
        elif self.codec == "zlib-v1":
            valid = (self.unavailable is None and self.sha256 is not None and self.inline_zlib_bytes is not None
                     and self.encoded_state_bytes is None and self.part_count is None)
        else:
            valid = (self.unavailable is None and self.sha256 is not None and self.encoded_state_bytes is not None
                     and self.part_count is not None and self.inline_zlib_bytes is None)
        if not valid:
            raise ValueError("Archive byte measurements must retain their exact storage basis")
        if self.codec is not None and (self.sdk_version is None or self.runtime_version is None):
            raise ValueError("Available archive metadata must retain its recorded SDK/runtime versions")
        return self


class ReplyMetrics(WireModel):
    json_utf8_bytes: Annotated[int, Field(ge=1, le=4 * 1024 * 1024)]
    text_utf8_bytes: Annotated[int, Field(ge=1, le=4 * 1024 * 1024)]
    stored_zlib_bytes: Annotated[int, Field(ge=1, le=32768)]
    sha256: Digest

    @model_validator(mode="after")
    def text_is_part_of_reply(self):
        if self.text_utf8_bytes >= self.json_utf8_bytes:
            raise ValueError("Reply text is only part of its complete JSON payload")
        return self


class TurnTokenUsage(WireModel):
    full_request_tokens: None
    provider_input_tokens: None
    provider_output_tokens: None
    reason: Literal["complete-turn-usage-not-recorded"]


class HistoricalSkillCatalog(WireModel):
    catalog_schema: Literal["fa-approved-skill-catalog-v1", "fa-approved-skill-catalog-v2"]
    entries: Annotated[tuple[TemplateSkill, ...], Field(max_length=4)]
    unavailable_count: Annotated[int, Field(ge=0, le=4)]
    usage: Literal["historical-catalog-not-load-or-permission-proof"]

    @model_validator(mode="after")
    def bounded_names(self):
        names = tuple(entry.name for entry in self.entries)
        if names != tuple(sorted(set(names))) or len(names) + self.unavailable_count > 4:
            raise ValueError("Historical catalog must keep its bounded ordered references")
        return self


class HistoricalUserSource(WireModel):
    turn_number: Annotated[int, Field(ge=1)]
    sha256: Digest
    utf8_bytes: Annotated[int, Field(ge=1, le=8192)]


class HistoricalWorkMemory(WireModel):
    claim_id: Identifier
    claim_version: Annotated[int, Field(ge=1)]
    claim_sha256: Digest
    text_sha256: Digest
    text_utf8_bytes: Annotated[int, Field(ge=1)]
    manifest_sha256: Digest
    usage: Literal["historical-reference-not-current-authority"]


class RecordedCompaction(WireModel):
    source: Literal["stored-checkpoint-observation", "inherited-checkpoint-observation"]
    observation_job_id: Identifier
    trigger: Literal["manual"]
    checkpoint_number: Annotated[int, Field(ge=1)]
    summary_sha256: Digest
    messages_removed: Annotated[int, Field(ge=0)]
    tokens_removed: int
    previous_state_sha256: Digest


class TurnContext(WireModel):
    schema_version: Literal["fa-turn-context-v1"]
    request_id: Identifier
    job_id: Identifier
    conversation_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]
    model: ModelDeployment
    model_capacity: ModelCapacitySnapshot | None = Field(default=None, exclude_if=lambda value: value is None)
    attachments: TextAttachments = Field(default=(), exclude_if=lambda value: not value)
    user_files: Annotated[tuple[UserTextFileReference, ...], Field(max_length=4)] = Field(
        default=(), exclude_if=lambda value: not value)
    read_at: AwareDatetime
    source: Literal["saved-turn-metadata"]
    turn_material_sha256: Digest
    prompt_assembly: AssemblyRecord | None
    capability_assembly: CapabilityAssemblyRecord | None = Field(default=None, exclude_if=lambda value: value is None)
    context_read: bool | None
    skill_catalog: HistoricalSkillCatalog | None
    user_turn_sources: Annotated[tuple[HistoricalUserSource, ...], Field(max_length=MAX_USER_TURN_SOURCES)] | None
    work_memory_source: HistoricalWorkMemory | None
    archive: ArchiveMetrics
    reply: ReplyMetrics | None
    compaction: RecordedCompaction | None
    token_usage: TurnTokenUsage
    wall_clock_seconds: None
    ui_rendered_bytes: None
    authorization: Literal[False]
    read_is_acknowledgment: Literal[False]

    @model_validator(mode="after")
    def bounded_sources(self):
        validate_attachments(self.attachments)
        file_names = tuple(item.name for item in (*self.attachments, *self.user_files))
        if len(file_names) > 4 or len(set(file_names)) != len(file_names):
            raise ValueError("Recorded file metadata must retain its distinct original input names")
        if self.model_capacity is not None and self.model_capacity.model != self.model:
            raise ValueError("Recorded model capacity belongs to another turn model")
        if self.user_turn_sources is not None:
            turns = tuple(source.turn_number for source in self.user_turn_sources)
            if turns != tuple(sorted(set(turns))) or any(number >= self.turn_number for number in turns):
                raise ValueError("User-source metadata must reference earlier turns in order")
        if (self.compaction is not None) != (self.archive.codec in DIAG_ARCHIVE_CODECS):
            raise ValueError("Compaction metadata requires its recorded checkpoint codec")
        if self.compaction is not None and (
            (self.compaction.source == "stored-checkpoint-observation")
            != (self.compaction.observation_job_id == self.job_id)
        ):
            raise ValueError("Current and inherited compaction observations must retain their original job")
        if len(self.model_dump_json().encode("utf-8")) > MAX_TURN_CONTEXT_BYTES:
            raise ValueError("Complete turn metadata exceeds its public byte bound")
        return self


def read_saved_turn_context(control, principal, identifier, turn_number):
    from .behavior_assistant import key
    from .behavior_delivery import _parse
    from .behavior_instruction import SavedReply, TurnArchive, sdk_session_id, turn_material

    record = control._instruction_record(principal, identifier, turn_number)
    binding = record.binding
    akey, rkey = key("text-archive", binding.job_id), key("reply", binding.job_id)
    rows = control.store.read_snapshot((akey, rkey))
    if rows[akey] is None:
        raise Problem(404, "TURN_CONTEXT_NOT_READY", "no original saved turn metadata is available")
    raw = rows[akey].payload["data"]
    saved = _parse(raw, TurnArchive)
    retained = turn_material(saved)
    if (saved.conversation_id != identifier or saved.job_id != binding.job_id or saved.turn_number != turn_number
            or saved.prompt_assembly != assemble_prompt(record.profile.instruction.prompt_profile).record
            or saved.capability_assembly != capability_record(record.profile.instruction.prompt_profile,
                                                              record.profile.instruction.capabilities)
            or rows[akey].payload["material"] != retained.model_dump(mode="json")
            or any(source.conversation_id != identifier for source in saved.user_turn_sources)):
        raise Problem(409, "CONVERSATION_CONTEXT_CHANGED", "original saved turn metadata differs")
    archive = saved.archive
    if isinstance(archive, (SdkArchive, StoredSdkArchive)) and archive.sdk_session_id != sdk_session_id(identifier):
        raise Problem(409, "CONVERSATION_CONTEXT_CHANGED", "original saved SDK identity differs")
    if isinstance(archive, StoredSdkArchive) and (
        archive.conversation_id, archive.job_id, archive.tenant_id, archive.team_id, archive.agent_oid,
    ) != (identifier, binding.job_id, binding.tenant_id, binding.team_id, binding.agent_oid):
        raise Problem(409, "CONVERSATION_CONTEXT_CHANGED", "recorded checkpoint belongs to another scope")
    metrics = ArchiveMetrics(
        codec=None if isinstance(archive, ArchiveUnavailable) else archive.codec,
        sdk_version=None if isinstance(archive, ArchiveUnavailable) else archive.sdk_version,
        runtime_version=None if isinstance(archive, ArchiveUnavailable) else archive.runtime_version,
        sha256=None if isinstance(archive, ArchiveUnavailable) else archive.sha256,
        encoded_state_bytes=archive.byte_length if isinstance(archive, StoredSdkArchive) else None,
        inline_zlib_bytes=len(archive.data) if isinstance(archive, SdkArchive) else None,
        part_count=len(archive.parts) if isinstance(archive, StoredSdkArchive) else None,
        unavailable=archive if isinstance(archive, ArchiveUnavailable) else None, content_reverified=False,
    )
    reply = None
    if rows[rkey] is not None:
        original = _parse(rows[rkey].payload["data"], SavedReply)
        verified = control.read_reply(principal, identifier, turn_number)
        encoded = verified.model_dump_json().encode("utf-8")
        reply = ReplyMetrics(json_utf8_bytes=len(encoded), text_utf8_bytes=len(verified.text.encode("utf-8")),
                             stored_zlib_bytes=len(original.data), sha256=hashlib.sha256(encoded).hexdigest())
    catalog = None if saved.skill_catalog is None else HistoricalSkillCatalog(
        catalog_schema=saved.skill_catalog.schema_version,
        entries=tuple(TemplateSkill(name=entry.name, version_id=entry.version_id, sha256=entry.sha256,
                                   metadata_sha256=entry.metadata_sha256 if isinstance(entry, PurposeSkillEntry) else None)
                      for entry in saved.skill_catalog.entries),
        unavailable_count=saved.skill_catalog.unavailable_count,
        usage="historical-catalog-not-load-or-permission-proof",
    )
    memory = saved.work_memory_source
    compaction = None
    if isinstance(archive, StoredSdkArchive) and archive.source is not None:
        observed = archive.source.execution
        if (observed.conversation_id, observed.sdk_session_id) != (identifier, archive.sdk_session_id):
            raise Problem(409, "CONVERSATION_CONTEXT_CHANGED", "recorded compaction belongs to another conversation")
        observation_source = "stored-checkpoint-observation"
        if observed.job_id != binding.job_id:
            previous_key = record.profile.instruction.previous_archive_id
            if previous_key != key("text-archive", archive.source.previous_job_id):
                raise Problem(409, "CONVERSATION_CONTEXT_CHANGED", "inherited compaction has a different registered source")
            previous_row = control.store.read_snapshot((previous_key,))[previous_key]
            if previous_row is None:
                raise Problem(409, "CONVERSATION_CONTEXT_CHANGED", "inherited compaction source is unavailable")
            previous = _parse(previous_row.payload["data"], TurnArchive)
            prior = previous.archive
            if (previous.conversation_id != identifier or previous.turn_number != turn_number - 1
                    or previous.job_id != archive.source.previous_job_id
                    or previous_row.payload["material"] != turn_material(previous).model_dump(mode="json")
                    or not isinstance(prior, StoredSdkArchive) or prior.source is None
                    or prior.sha256 != archive.source.previous_sha256
                    or prior.source.execution != observed
                    or prior.source.profile != archive.source.profile
                    or archive.source.profile != record.profile.instruction.checkpoint_profile
                    or (prior.conversation_id, prior.job_id, prior.sdk_session_id,
                        prior.tenant_id, prior.team_id, prior.agent_oid) != (
                        identifier, previous.job_id, archive.sdk_session_id,
                        binding.tenant_id, binding.team_id, binding.agent_oid)):
                raise Problem(409, "CONVERSATION_CONTEXT_CHANGED", "inherited compaction source metadata differs")
            observation_source = "inherited-checkpoint-observation"
        compaction = RecordedCompaction(
            source=observation_source, observation_job_id=observed.job_id, trigger=observed.trigger,
            checkpoint_number=observed.checkpoint_number, summary_sha256=observed.summary_sha256,
            messages_removed=observed.messages_removed, tokens_removed=observed.tokens_removed,
            previous_state_sha256=archive.source.previous_sha256,
        )
    return TurnContext(
        schema_version="fa-turn-context-v1", request_id=record.request_id, job_id=binding.job_id,
        conversation_id=identifier, turn_number=turn_number, model=record.profile.instruction.model,
        model_capacity=record.profile.instruction.model_capacity,
        attachments=record.profile.instruction.attachments,
        user_files=tuple(file_reference(item) for item in record.profile.instruction.files),
        read_at=control.runtime._now(), source="saved-turn-metadata", turn_material_sha256=retained.sha256,
        prompt_assembly=saved.prompt_assembly, capability_assembly=saved.capability_assembly,
        context_read=raw.get("context_read"), skill_catalog=catalog,
        user_turn_sources=None if "user_turn_sources" not in raw else tuple(HistoricalUserSource(
            turn_number=source.turn_number, sha256=source.sha256, utf8_bytes=source.utf8_bytes,
        ) for source in saved.user_turn_sources),
        work_memory_source=None if memory is None else HistoricalWorkMemory(
            claim_id=memory.claim_id, claim_version=memory.claim_version, claim_sha256=memory.claim_sha256,
            text_sha256=memory.text_sha256, text_utf8_bytes=memory.text_utf8_bytes,
            manifest_sha256=memory.manifest_sha256, usage="historical-reference-not-current-authority",
        ),
        archive=metrics, reply=reply, compaction=compaction,
        token_usage=TurnTokenUsage(full_request_tokens=None, provider_input_tokens=None, provider_output_tokens=None,
                                  reason="complete-turn-usage-not-recorded"),
        wall_clock_seconds=None, ui_rendered_bytes=None, authorization=False, read_is_acknowledgment=False,
    )
