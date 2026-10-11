"""SDK-independent scope and receipts for one user-provided skill draft."""

from typing import Annotated, Literal

from pydantic import Field, field_validator

from .behavior_capabilities import CapabilityCapture, skillify_granted
from .behavior_wire import Command, Digest, Identifier, WireModel
from .m3_models import SkillPurpose, VersionId, policy_text_is_visible

SKILL_DRAFT_PROFILE = "fa-instruction-skill-draft-v1"
# Human-selected per new conversation; the older profile stays bound to its installed standing policy.
HISTORY_SKILL_DRAFT_PROFILE = "fa-instruction-history-skill-draft-v1"
HISTORY_SKILL_DRAFT_MEMORY_PROFILE = "fa-instruction-history-skill-draft-memory-v1"
SKILL_DRAFT_PROFILES = (SKILL_DRAFT_PROFILE, HISTORY_SKILL_DRAFT_PROFILE, HISTORY_SKILL_DRAFT_MEMORY_PROFILE)
SKILL_DRAFT_ERRORS = frozenset({
    "SKILL_DRAFT_DISABLED", "SKILL_DRAFT_SCOPE_DENIED", "SKILL_DRAFT_CONTENT_INVALID",
    "SKILL_DRAFT_TARGET_CONFLICT", "SKILL_DRAFT_OUTCOME_UNKNOWN", "SKILL_DRAFT_RECEIPT_INVALID",
})


def skill_draft_enabled(prompt_profile: str | None, capabilities: CapabilityCapture | None = None) -> bool:
    """Tool availability from the immutable task; the writer still checks its original human authorization."""
    return prompt_profile in SKILL_DRAFT_PROFILES or (
        prompt_profile in {"fa-instruction-v2", "fa-instruction-v3", "fa-instruction-v4", "fa-instruction-agent-role-v2",
                           "fa-instruction-agent-role-v3", "fa-instruction-agent-role-v4"}
        and skillify_granted(capabilities))


def skill_purpose(value: str) -> SkillPurpose:
    return SkillPurpose(schema_version="fa-skill-purpose-v1", description=value)


def skill_draft_bytes(value: str) -> bytes:
    try:
        raw = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Skill draft body must be UTF-8 text") from exc
    if not value.strip() or len(raw) > 8192 or not policy_text_is_visible(value, multiline=True):
        raise ValueError("Skill draft body must be visible nonblank UTF-8 text within 8192 bytes")
    return raw


class SkillDraftToolArgs(WireModel):
    purpose: Annotated[str, Field(min_length=1, max_length=512)]
    body: Annotated[str, Field(min_length=1, max_length=8192)]

    @field_validator("purpose")
    @classmethod
    def valid_purpose(cls, value):
        skill_purpose(value)
        return value

    @field_validator("body")
    @classmethod
    def valid_body(cls, value):
        skill_draft_bytes(value)
        return value


class SaveSkillDraftCommand(Command):
    request_id: Literal["save-skill-draft"]
    purpose: Annotated[str, Field(min_length=1, max_length=512)]
    body: Annotated[str, Field(min_length=1, max_length=8192)]

    @field_validator("purpose")
    @classmethod
    def valid_purpose(cls, value):
        skill_purpose(value)
        return value

    @field_validator("body")
    @classmethod
    def valid_body(cls, value):
        skill_draft_bytes(value)
        return value


class SkillDraftOutcome(WireModel):
    schema_version: Literal["fa-skill-draft-outcome-v1"]
    receipt_id: Identifier
    intent_sha256: Digest
    stage: Literal["create_unknown", "created", "upload_unknown", "saved"]

    @property
    def known_effects(self):
        return () if self.stage == "create_unknown" else (self.receipt_id,)

    @property
    def unresolved_operations(self):
        return () if self.stage == "saved" else (self.receipt_id,)


class SkillDraftReceipt(WireModel):
    schema_version: Literal["fa-skill-draft-receipt-v1"]
    state_scope: Literal["at-original-draft-write"]
    receipt_id: Identifier
    job_id: Identifier
    artifact_id: Identifier
    version_id: VersionId
    visibility: Literal["team"]
    source_turn_number: Annotated[int, Field(ge=1)]
    source_user_text_sha256: Digest
    sha256: Digest
    byte_length: Annotated[int, Field(ge=1, le=8192)]
    purpose: SkillPurpose
    metadata_sha256: Digest
    approved: Literal[False]
    effective_version_id: None

    @field_validator("approved", mode="before")
    @classmethod
    def literal_false(cls, value):
        if value is not False:
            raise ValueError("Draft status must be the literal boolean false, never an approval")
        return value
