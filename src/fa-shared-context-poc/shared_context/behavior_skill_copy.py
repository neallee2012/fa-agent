"""SDK-independent scope and receipts for one explicitly authorized Team SOP copy."""

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .behavior_wire import Digest, Identifier, WireModel
from .m3_models import SkillPurpose, VersionId

SOP_COPY_PROFILE = "fa-instruction-sop-copy-v1"
SOP_COPY_ERRORS = frozenset({
    "SOP_COPY_DISABLED", "SOP_COPY_SCOPE_DENIED", "SOP_COPY_SOURCE_CHANGED", "SOP_COPY_SOURCE_DENIED",
    "SOP_COPY_TARGET_CONFLICT", "SOP_COPY_OUTCOME_UNKNOWN", "SOP_COPY_RECEIPT_INVALID",
})


class TeamSopCopy(WireModel):
    schema_version: Literal["fa-team-sop-skill-copy-v1"]
    authorization: Literal["one-approved-sop-byte-copy-per-work"]
    visibility: Literal["team"]
    source_visibility: Literal["team"]
    team_id: Identifier
    source_artifact_id: Identifier
    source_version_id: VersionId
    source_sha256: Digest
    source_approval_sha256: Digest
    purpose: SkillPurpose


class SopCopyOutcome(WireModel):
    schema_version: Literal["fa-sop-copy-outcome-v1"]
    receipt_id: Identifier
    intent_sha256: Digest
    stage: Literal["create_unknown", "created", "upload_unknown", "saved"]

    @property
    def known_effects(self):
        return () if self.stage == "create_unknown" else (self.receipt_id,)

    @property
    def unresolved_operations(self):
        return () if self.stage == "saved" else (self.receipt_id,)


class SopCopyReceipt(WireModel):
    schema_version: Literal["fa-sop-skill-draft-receipt-v1"]
    state_scope: Literal["at-original-copy-write"]
    receipt_id: Identifier
    job_id: Identifier
    artifact_id: Identifier
    version_id: VersionId
    visibility: Literal["team"]
    source_artifact_id: Identifier
    source_version_id: VersionId
    source_sha256: Digest
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

    @model_validator(mode="after")
    def exact_copy(self):
        if self.sha256 != self.source_sha256 or self.artifact_id == self.source_artifact_id:
            raise ValueError("A draft copy must retain the exact source bytes under its separate assigned identity")
        return self
