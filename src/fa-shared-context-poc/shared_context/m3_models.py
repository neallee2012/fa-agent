from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

from .backup_models import BackupETag, BackupId, BackupSHA256, BackupVersion, OwnerOid
from .models import Identifier, StrictModel
from .errors import Problem

MAX_ARTIFACT_BYTES = 512 * 1024
MAX_ARTIFACT_VERSIONS = 12
MAX_SKILL_RESOURCE_BYTES = 8192
VersionId = BackupId
Digest = BackupSHA256
ETag = BackupETag
Filename = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", max_length=128)]
MediaType = Literal["text/plain", "text/markdown", "application/json", "application/octet-stream", "image/png"]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ArtifactKind = Literal["deliverable", "sop", "skill"]
Timestamp = Annotated[str, Field(min_length=1, max_length=64)]
_identifier, _version = TypeAdapter(Identifier), TypeAdapter(VersionId)


def validate_id(value: str, *, version: bool = False) -> str:
    try:
        return (_version if version else _identifier).validate_python(value, strict=True)
    except ValidationError as exc:
        raise Problem(422, "M3_INVALID_ID", "invalid artifact, team or version ID") from exc


class NativeArtifactSource(StrictModel):
    """Immutable reference produced from a verified native human turn, not caller authority."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["native-turn"] = "native-turn"
    tenant_id: OwnerOid
    team_id: Identifier
    agent_id: Identifier
    agent_oid: OwnerOid
    conversation_id: Identifier
    request_id: Identifier
    submit_sha256: Digest

    @property
    def author_oid(self) -> str:
        return self.agent_oid

    @property
    def author(self) -> tuple[str, str, str]:
        return self.kind, self.tenant_id, self.agent_id


OWNER_AUTHOR = "team-owner"


class OwnerArtifactSource(StrictModel):
    """Immutable reference for text the configured Team owner typed in; the API derives it from the verified login."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["team-owner"]
    tenant_id: OwnerOid
    team_id: Identifier
    owner_oid: OwnerOid
    submit_sha256: Digest

    @property
    def author_oid(self) -> str:
        return self.owner_oid

    @property
    def author(self) -> tuple[str, str, str]:
        return self.kind, self.tenant_id, self.owner_oid


# A draft's server-derived source: an Agent's native turn or the Team owner's own input.
ArtifactSource = NativeArtifactSource | OwnerArtifactSource


def check_artifact_source(source_work, native_source):
    if (source_work is None) == (native_source is None):
        raise ValueError("exactly one legacy work or native turn source is required")


class CreateArtifact(StrictModel):
    kind: ArtifactKind
    title: Annotated[str, Field(min_length=1, max_length=256)]
    source_work: Identifier


def _metadata_header(value: StrictModel) -> str:
    return base64.urlsafe_b64encode(value.model_dump_json().encode("utf-8")).decode("ascii")


def _metadata_from_header(value: str, model, maximum: int):
    if len(value) > maximum:
        raise ValueError("Skill metadata header exceeds its complete byte bound")
    raw = base64.b64decode(value, altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(raw).decode("ascii") != value:
        raise ValueError("Skill metadata header must use canonical base64url")
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Skill metadata must not contain duplicate JSON fields")
        return result
    json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    return model.model_validate_json(raw)


def policy_text_is_visible(value: str, *, multiline: bool = False) -> bool:
    return all(char.isprintable() or unicodedata.category(char) == "Zs"
               or multiline and char in "\r\n\t" for char in value)


class SkillPurpose(StrictModel):
    schema_version: Literal["fa-skill-purpose-v1"]
    description: Annotated[str, Field(min_length=1, max_length=512)]

    @field_validator("description")
    @classmethod
    def bounded_literal(cls, value):
        if not value.strip() or not policy_text_is_visible(value) or len(value.encode("utf-8")) > 512:
            raise ValueError("Skill purpose must be printable text within 512 UTF-8 bytes")
        return value

    def header(self) -> str:
        return _metadata_header(self)

    @classmethod
    def from_header(cls, value: str):
        return _metadata_from_header(value, cls, 2048)


class SkillResourceReference(StrictModel):
    name: Filename
    artifact_id: Identifier
    version_id: VersionId
    sha256: Digest


class SkillResources(StrictModel):
    schema_version: Literal["fa-skill-resources-v1"] = "fa-skill-resources-v1"
    entries: Annotated[list[SkillResourceReference], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def distinct_names(self):
        names = tuple(entry.name for entry in self.entries)
        if names != tuple(sorted(set(names))):
            raise ValueError("Skill resource names must be distinct and ordered")
        return self

    def header(self) -> str:
        return _metadata_header(self)

    @classmethod
    def from_header(cls, value: str):
        return _metadata_from_header(value, cls, 4096)


class SkillResourceRead(StrictModel):
    schema_version: Literal["fa-skill-resource-read-v1"] = "fa-skill-resource-read-v1"
    skill_artifact_id: Identifier
    skill_version_id: VersionId
    skill_metadata_sha256: Digest
    resource: SkillResourceReference
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    text: str

    @model_validator(mode="after")
    def exact_content(self):
        skill_resource_text(self.text.encode("utf-8"), self.resource.sha256)
        return self


class SkillToolRequirement(StrictModel):
    entry_id: Literal["fa-delegate-v1"]
    entry_sha256: Digest
    tool: Literal["task"]


class SkillToolRequirements(StrictModel):
    schema_version: Literal["fa-skill-tool-requirements-v1"] = "fa-skill-tool-requirements-v1"
    entries: Annotated[list[SkillToolRequirement], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def distinct_tools(self):
        names = [(item.entry_id, item.tool) for item in self.entries]
        if names != sorted(set(names)):
            raise ValueError("Skill tool requirements must be distinct and ordered")
        return self

    def header(self) -> str:
        return _metadata_header(self)

    @classmethod
    def from_header(cls, value: str):
        return _metadata_from_header(value, cls, 4096)


def skill_resource_text(raw: bytes, expected_sha256: str) -> str:
    if (not 0 < len(raw) <= MAX_SKILL_RESOURCE_BYTES
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise ValueError("Skill resource bytes differ from their complete immutable reference")
    text = raw.decode("utf-8")
    if any(unicodedata.category(char) == "Cc" and char not in "\r\n\t" for char in text):
        raise ValueError("Skill resource contains unsupported control characters")
    return text


class VersionInput(StrictModel):
    filename: Filename
    media_type: MediaType
    sha256: Digest
    source_work: Identifier | None = None
    skill_purpose: SkillPurpose | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_resources: SkillResources | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_tools: SkillToolRequirements | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def resource_metadata(self):
        if (self.skill_resources is not None or self.skill_tools is not None) and self.skill_purpose is None:
            raise ValueError("Skill manifest requires version-bound purpose metadata and its approval digest")
        return self


class BlobReceipt(StrictModel):
    native_version: BackupVersion
    etag: ETag
    sha256: Digest
    bytes: Annotated[int, Field(ge=1, le=MAX_ARTIFACT_BYTES)]


class ApprovalInput(StrictModel):
    decision_id: VersionId
    version_id: VersionId
    sha256: Digest
    decision: Literal["approve", "reject"]
    metadata_sha256: Digest | None = Field(default=None, exclude_if=lambda value: value is None)


class ApprovalRecord(ApprovalInput):
    approver_oid: OwnerOid
    decided_at: Timestamp


class SkillDeactivationInput(StrictModel):
    schema_version: Literal["fa-skill-deactivation-v1"]
    decision_id: VersionId
    version_id: VersionId
    sha256: Digest
    metadata_sha256: Digest | None = Field(default=None, exclude_if=lambda value: value is None)


class SkillDeactivationRecord(SkillDeactivationInput):
    deactivated_by_oid: OwnerOid
    deactivated_at: Timestamp
    applied_revision: Annotated[int, Field(ge=1)]


class ArtifactVersion(VersionInput):
    source_work: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    native_source: ArtifactSource | None = Field(default=None, exclude_if=lambda value: value is None)
    version_id: VersionId
    number: Annotated[int, Field(ge=1, le=MAX_ARTIFACT_VERSIONS)]
    blob: BlobReceipt
    created_by_oid: OwnerOid
    source_work_revision: Annotated[int, Field(ge=1)] | None = Field(default=None, exclude_if=lambda value: value is None)
    created_at: Timestamp
    approval: ApprovalRecord | None = None
    metadata_sha256: Digest | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def matching_receipts(self):
        check_artifact_source(self.source_work, self.native_source)
        if (self.source_work is None) != (self.source_work_revision is None):
            raise ValueError("only a legacy source has a work revision")
        if self.native_source is not None and self.native_source.author_oid != self.created_by_oid:
            raise ValueError("the native source must belong to the creating author")
        if self.sha256 != self.blob.sha256:
            raise ValueError("artifact and blob digests differ")
        if (self.skill_purpose is None) != (self.metadata_sha256 is None):
            raise ValueError("Versioned purpose requires its complete metadata digest")
        if self.approval and (self.approval.version_id != self.version_id or self.approval.sha256 != self.sha256):
            raise ValueError("approval must name the exact immutable version")
        if self.approval and self.approval.metadata_sha256 != self.metadata_sha256:
            raise ValueError("approval must pin the exact version metadata")
        return self


class PendingVersion(VersionInput):
    source_work: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    native_source: ArtifactSource | None = Field(default=None, exclude_if=lambda value: value is None)
    version_id: VersionId
    bytes: Annotated[int, Field(ge=1, le=MAX_ARTIFACT_BYTES)]
    created_by_oid: OwnerOid
    source_work_revision: Annotated[int, Field(ge=1)] | None = Field(default=None, exclude_if=lambda value: value is None)
    created_at: Timestamp
    status: Literal["pending_or_unknown"] = "pending_or_unknown"

    @model_validator(mode="after")
    def original_source(self):
        check_artifact_source(self.source_work, self.native_source)
        if (self.source_work is None) != (self.source_work_revision is None):
            raise ValueError("only a legacy source has a work revision")
        if self.native_source is not None and self.native_source.author_oid != self.created_by_oid:
            raise ValueError("the native source must belong to the creating author")
        return self


def skill_metadata_sha256(team: str, artifact: str, version: dict) -> str | None:
    if version.get("skill_purpose") is None:
        return None
    material = {
        "schema_version": "fa-skill-version-metadata-v1", "team": team, "artifact_id": artifact,
        "version": {key: value for key, value in version.items() if key not in {"approval", "metadata_sha256"}},
    }
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ArtifactDocument(CreateArtifact):
    schema_version: Literal[1] = 1
    source_work: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    artifact_id: Identifier
    team: Identifier
    owner: Identifier
    owner_oid: OwnerOid
    native_source: ArtifactSource | None = Field(default=None, exclude_if=lambda value: value is None)
    revision: Annotated[int, Field(ge=1)]
    versions: Annotated[list[ArtifactVersion], Field(max_length=MAX_ARTIFACT_VERSIONS)] = Field(default_factory=list)
    pending: PendingVersion | None = None
    effective_version_id: VersionId | None = None
    deactivations: Annotated[list[SkillDeactivationRecord], Field(max_length=MAX_ARTIFACT_VERSIONS)] = Field(
        default_factory=list, exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def valid_history(self):
        check_artifact_source(self.source_work, self.native_source)
        if self.native_source is not None and (self.native_source.team_id, self.native_source.author_oid) != (
                self.team, self.owner_oid):
            raise ValueError("the native source must belong to the artifact's Team and owner")
        if isinstance(self.native_source, OwnerArtifactSource) != (self.owner == OWNER_AUTHOR):
            raise ValueError("only a Team owner source uses the Team owner author name")
        for version in self.versions:
            if (version.native_source is None) != (self.native_source is None):
                raise ValueError("native and legacy artifact provenance cannot be mixed")
            if version.native_source is not None and version.native_source.team_id != self.team:
                raise ValueError("a native artifact version cannot cross Teams")
            if version.native_source is not None and version.native_source.author != self.native_source.author:
                raise ValueError("the native artifact source must retain the original author")
            if version.skill_purpose is not None and self.kind != "skill":
                raise ValueError("Only skill versions may carry an approved purpose")
            if version.metadata_sha256 != skill_metadata_sha256(
                self.team, self.artifact_id, version.model_dump(mode="json"),
            ):
                raise ValueError("Skill version metadata differs from its immutable digest")
        if self.pending and self.pending.skill_purpose is not None and self.kind != "skill":
            raise ValueError("Only skill versions may propose a purpose")
        if self.pending and self.pending.native_source is not None and self.pending.native_source.team_id != self.team:
            raise ValueError("a native pending version cannot cross Teams")
        if self.pending and (self.pending.native_source is None) != (self.native_source is None):
            raise ValueError("native and legacy pending provenance cannot be mixed")
        ids = [item.version_id for item in self.versions]
        if len(ids) != len(set(ids)) or [item.number for item in self.versions] != list(range(1, len(ids) + 1)):
            raise ValueError("version history must be unique and sequential")
        if self.pending and (self.pending.version_id in ids or len(ids) >= MAX_ARTIFACT_VERSIONS):
            raise ValueError("invalid pending version")
        if any(item.created_by_oid != self.owner_oid for item in self.versions) or (
            self.pending and self.pending.created_by_oid != self.owner_oid
        ):
            raise ValueError("version provenance must match the artifact owner")
        if self.kind == "deliverable" and (
            self.effective_version_id is not None or any(item.approval for item in self.versions)
        ):
            raise ValueError("ordinary deliverables do not activate policies")
        if self.deactivations and self.kind != "skill":
            raise ValueError("Only skills support explicit deactivation")
        deactivated, revision = set(), 0
        for decision in self.deactivations:
            version = next((item for item in self.versions if item.version_id == decision.version_id), None)
            if (version is None or version.approval is None or version.approval.decision != "approve"
                    or decision.sha256 != version.sha256 or decision.metadata_sha256 != version.metadata_sha256):
                raise ValueError("Deactivation must retain an exact approved skill version")
            if decision.version_id in deactivated or not revision < decision.applied_revision <= self.revision:
                raise ValueError("Deactivation history must be unique and ordered")
            deactivated.add(decision.version_id)
            revision = decision.applied_revision
        decisions = [item.approval.decision_id for item in self.versions if item.approval]
        decisions.extend(item.decision_id for item in self.deactivations)
        if len(decisions) != len(set(decisions)):
            raise ValueError("decision IDs cannot be reused")
        if self.effective_version_id is not None:
            if self.effective_version_id in deactivated:
                raise ValueError("A deactivated version cannot silently become effective again")
            effective = next((item for item in self.versions if item.version_id == self.effective_version_id), None)
            if effective is None or effective.approval is None or effective.approval.decision != "approve":
                raise ValueError("effective version must have an exact human approval")
        return self


MAX_SKILL_REVIEW_ENTRIES = 24
SkillReviewStatus = Literal["no_version", "upload_pending", "awaiting_decision", "rejected", "effective", "deactivated"]


class SkillReviewVersion(StrictModel):
    version_id: VersionId
    number: Annotated[int, Field(ge=1, le=MAX_ARTIFACT_VERSIONS)]
    sha256: Digest
    metadata_sha256: Digest | None = None
    created_at: Timestamp
    decision: Literal["approve", "reject"] | None = None


class SkillReviewEntry(StrictModel):
    """Review metadata only; skill bodies stay behind the existing versioned download."""

    artifact_id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=256)]
    owner: Identifier
    source_work: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    native_source: ArtifactSource | None = Field(default=None, exclude_if=lambda value: value is None)
    revision: Annotated[int, Field(ge=1)]
    status: SkillReviewStatus
    latest_version: SkillReviewVersion | None = None
    effective_version_id: VersionId | None = None
    deactivated_version_ids: Annotated[tuple[VersionId, ...], Field(max_length=MAX_ARTIFACT_VERSIONS)] = ()

    @classmethod
    def from_document(cls, document: ArtifactDocument) -> "SkillReviewEntry":
        latest = document.versions[-1] if document.versions else None
        deactivated = {item.version_id for item in document.deactivations}
        if document.pending is not None:
            status = "upload_pending"
        elif latest is None:
            status = "no_version"
        elif latest.approval is None:
            status = "awaiting_decision"
        elif latest.approval.decision == "reject":
            status = "rejected"
        elif document.effective_version_id == latest.version_id:
            status = "effective"
        elif latest.version_id in deactivated:
            status = "deactivated"
        else:
            raise ValueError("An approved latest skill version must be effective or deactivated")
        return cls(
            artifact_id=document.artifact_id, title=document.title, owner=document.owner,
            source_work=document.source_work, native_source=document.native_source, revision=document.revision, status=status,
            effective_version_id=document.effective_version_id,
            deactivated_version_ids=tuple(item.version_id for item in document.deactivations),
            latest_version=None if latest is None else SkillReviewVersion(
                version_id=latest.version_id, number=latest.number, sha256=latest.sha256,
                metadata_sha256=latest.metadata_sha256, created_at=latest.created_at,
                decision=None if latest.approval is None else latest.approval.decision,
            ),
        )


class SkillReviewList(StrictModel):
    """One page in artifact-ID order; ``next_after`` continues it, and deliverables only advance the cursor."""

    schema_version: Literal["fa-skill-review-list-v1"] = "fa-skill-review-list-v1"
    team: Identifier
    after: Identifier | None = None
    entries: Annotated[tuple[SkillReviewEntry, ...], Field(max_length=MAX_SKILL_REVIEW_ENTRIES)] = ()
    next_after: Identifier | None = None

    @model_validator(mode="after")
    def ordered_page(self):
        names = [entry.artifact_id for entry in self.entries]
        if names != sorted(set(names)) or (self.after is not None and names and names[0] <= self.after):
            raise ValueError("Skill review entries must be unique and follow the page cursor")
        if self.next_after is not None and (
            (self.after is not None and self.next_after <= self.after) or (names and self.next_after < names[-1])
        ):
            raise ValueError("The next page cursor must advance past this page")
        return self
