"""SDK-independent exact reviewed-work metadata and read result; not semantic memory."""

import hashlib
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from .behavior_wire import Command, Digest, ExactSourceRef, Identifier, MemoryKind, WireModel

WORK_MEMORY_RESPONSE_BYTES = 24576
WORK_MEMORY_ERRORS = frozenset({
    "WORK_MEMORY_READ_DISABLED", "WORK_MEMORY_SCOPE_DENIED", "WORK_MEMORY_NOT_AVAILABLE",
    "WORK_MEMORY_SOURCE_CHANGED", "WORK_MEMORY_SOURCE_INVALID", "WORK_MEMORY_SOURCE_LIMIT",
    "MEMORY_REQUIRED_FACT_MISSING", "MEMORY_CLAIM_REVOKED", "MEMORY_CLAIM_STALE",
    "MEMORY_CLAIM_NOT_EFFECTIVE", "MEMORY_CLAIM_EXPIRED", "MEMORY_CLAIM_CORRUPT",
    "MEMORY_TOKEN_COUNT_UNKNOWN", "MEMORY_REQUIRED_CONTEXT_TOO_LARGE",
    "HUMAN_REVIEW_REVOKED", "HUMAN_REVIEW_NOT_APPROVED", "HUMAN_REVIEW_BINDING_CHANGED",
    "HUMAN_REVIEW_MISSING", "HUMAN_REVIEW_CORRUPT", "MEMORY_REVIEW_REJECTED", "MEMORY_REVIEW_UNAVAILABLE",
    "SOURCE_EXPIRED", "SOURCE_WITHDRAWN", "SOURCE_REQUIRED_MISSING", "SOURCE_RECORD_INVALID",
    "SOURCE_STALE", "SOURCE_SCOPE_DENIED", "ASSISTANT_SOURCE_DENIED", "ASSISTANT_CONTEXT_CHANGED",
    "ASSISTANT_SOURCE_CHANGED", "ASSISTANT_CLAIM_EXPIRED", "SOURCE_ACCESS_CHANGED",
    "SOURCE_RECEIPT_MISMATCH", "SOURCE_INTEGRITY_ERROR",
})
REVIEWED_NOTE_ERRORS = WORK_MEMORY_ERRORS | frozenset({
    "MEMORY_RECALL_DISABLED", "MEMORY_RECALL_SCOPE_DENIED", "MEMORY_RECALL_UNAVAILABLE",
    "MEMORY_RECALL_CHANGED", "MEMORY_SETTINGS_MISSING", "MEMORY_SETTINGS_INVALID",
})


class WorkMemoryName(WireModel):
    claim_id: Identifier


class ReadWorkMemory(Command):
    claim_id: Identifier


class WorkMemorySource(WireModel):
    schema_version: Literal["fa-work-memory-source-v1"]
    kind: Literal["reviewed-work-note"] = "reviewed-work-note"
    visibility: Literal["team"] = "team"
    team_id: Identifier
    subject: Identifier
    purpose: Identifier
    claim_id: Identifier
    claim_version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    claim_sha256: Digest
    text_sha256: Digest
    text_utf8_bytes: Annotated[int, Field(ge=1)]
    review_sha256: Digest
    review_policy_version: Identifier
    manifest_sha256: Digest
    sources: Annotated[tuple[ExactSourceRef, ...], Field(min_length=1, max_length=24)]
    tokens: None = None

    @model_validator(mode="after")
    def bounded_provenance(self):
        identities = tuple((source.source_id, source.version) for source in self.sources)
        if (identities != tuple(sorted(set(identities)))
                or len(self.model_dump_json().encode("utf-8")) > 8192):
            raise ValueError("Complete reviewed-memory provenance must fit its storage contract")
        return self


class WorkMemoryCatalog(WireModel):
    schema_version: Literal["fa-work-memory-catalog-v1"]
    usage: Literal["knowledge-data-not-permission"] = "knowledge-data-not-permission"
    source: WorkMemorySource


class WorkMemoryOriginal(WireModel):
    source: ExactSourceRef
    text: str

    @model_validator(mode="after")
    def exact_original(self):
        data = self.text.encode("utf-8")
        if len(data) != self.source.byte_length or hashlib.sha256(data).hexdigest() != self.source.sha256:
            raise ValueError("Necessary original differs from its exact source reference")
        return self


class WorkMemoryApplicability(WireModel):
    kind: MemoryKind
    effective_from: AwareDatetime
    expires_at: AwareDatetime | None

    @model_validator(mode="after")
    def interval(self):
        if self.expires_at is not None and self.expires_at <= self.effective_from:
            raise ValueError("Reviewed memory must retain its valid interval")
        return self


class WorkMemoryRead(WireModel):
    schema_version: Literal["fa-work-memory-read-v1"]
    usage: Literal["knowledge-data-not-permission"] = "knowledge-data-not-permission"
    source: WorkMemorySource
    text: str
    originals: Annotated[tuple[WorkMemoryOriginal, ...], Field(min_length=1, max_length=24)]
    applicability: WorkMemoryApplicability | None = Field(default=None, exclude_if=lambda value:value is None)

    @model_validator(mode="after")
    def exact_material(self):
        data = self.text.encode("utf-8")
        if (not self.text.strip() or len(data) != self.source.text_utf8_bytes
                or hashlib.sha256(data).hexdigest() != self.source.text_sha256
                or tuple(original.source for original in self.originals) != self.source.sources
                or len(self.model_dump_json().encode("utf-8")) > WORK_MEMORY_RESPONSE_BYTES):
            raise ValueError("Complete reviewed claim and necessary originals must match their bounded provenance")
        return self
