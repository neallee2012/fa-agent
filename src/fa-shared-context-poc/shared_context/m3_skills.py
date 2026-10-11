"""Bounded, approved M3 text sources; not a filesystem or native skill loader."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .m3_models import (
    ArtifactDocument, ArtifactVersion, SkillPurpose, SkillResources, SkillToolRequirement, SkillToolRequirements,
    VersionId, check_artifact_source, policy_text_is_visible,
)

if TYPE_CHECKING:
    from .auth import Actor
    from .m3_artifacts import ArtifactService

MAX_SKILL_CATALOG_BYTES = 4096
SKILL_ERRORS = frozenset({
    "SKILL_ACCESS_DISABLED", "SKILL_IDENTITY_DENIED", "SKILL_APPROVAL_DENIED",
    "SKILL_SOURCE_DENIED", "SKILL_SOURCE_CHANGED", "SKILL_CATALOG_LIMIT",
    "M3_SKILL_RESOURCE_DENIED", "M3_SKILL_RESOURCE_CHANGED", "M3_SKILL_RESOURCE_FORMAT",
    "M3_NO_EFFECTIVE_VERSION", "M3_FILE_INTEGRITY", "M3_VERSION_NOT_FOUND",
})
SkillSelection = Annotated[tuple[Identifier, ...], Field(max_length=4)]
_selection = TypeAdapter(SkillSelection)


def validate_selection(value: tuple[str, ...]) -> tuple[str, ...]:
    selected = _selection.validate_python(value, strict=True)
    if len(set(selected)) != len(selected):
        raise ValueError("Approved skill selectors must be distinct")
    return selected


class SkillEntry(WireModel):
    name: Identifier
    title: Annotated[str, Field(min_length=1, max_length=256)]
    description: None = None
    kind: Literal["skill"] = "skill"
    version_id: VersionId
    sha256: Digest
    byte_length: Annotated[int, Field(ge=1, le=8192)]
    team: Identifier
    source: Literal["m3-human-approved-artifact"] = "m3-human-approved-artifact"
    source_work: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    source_work_revision: Annotated[int, Field(ge=1)] | None = Field(default=None, exclude_if=lambda value: value is None)
    native_submit_sha256: Digest | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def original_source(self):
        check_artifact_source(self.source_work, self.native_submit_sha256)
        if (self.source_work is None) != (self.source_work_revision is None):
            raise ValueError("only a legacy skill source has a work revision")
        return self

    @field_validator("title")
    @classmethod
    def printable_title(cls, value):
        if not value.strip() or not policy_text_is_visible(value):
            raise ValueError("Skill titles must be printable metadata")
        return value


class SkillCatalog(WireModel):
    schema_version: Literal["fa-approved-skill-catalog-v1"] = "fa-approved-skill-catalog-v1"
    purpose: Literal["instruction-guidance-only"] = "instruction-guidance-only"
    entries: Annotated[tuple[SkillEntry, ...], Field(max_length=4)] = ()
    unavailable_count: Annotated[int, Field(ge=0, le=4)] = 0
    tokens: None = None

    @model_validator(mode="after")
    def bounded(self):
        names = tuple(entry.name for entry in self.entries)
        if names != tuple(sorted(set(names))) or len(names) + self.unavailable_count > 4:
            raise ValueError("Skill catalog must be bounded, distinct and ordered")
        if len(self.model_dump_json().encode("utf-8")) > MAX_SKILL_CATALOG_BYTES:
            raise ValueError("Complete skill catalog exceeds its byte bound")
        return self


class SkillReadResult(WireModel):
    skill: SkillEntry
    text: str

    @model_validator(mode="after")
    def exact_body(self):
        data = self.text.encode("utf-8")
        if (len(data) != self.skill.byte_length or hashlib.sha256(data).hexdigest() != self.skill.sha256
                or not self.text.strip()
                or not policy_text_is_visible(self.text, multiline=True)):
            raise ValueError("Skill body differs from the approved immutable receipt")
        return self


class PurposeSkillEntry(SkillEntry):
    description: str | None = None
    metadata_sha256: Digest | None = None

    @model_validator(mode="after")
    def approved_purpose(self):
        if (self.description is None) != (self.metadata_sha256 is None):
            raise ValueError("Skill purpose requires its approved metadata digest")
        if self.description is not None:
            SkillPurpose(schema_version="fa-skill-purpose-v1", description=self.description)
        return self


class PurposeSkillCatalog(SkillCatalog):
    schema_version: Literal["fa-approved-skill-catalog-v2"]
    entries: Annotated[tuple[PurposeSkillEntry, ...], Field(max_length=4)] = ()


class PurposeSkillReadResult(SkillReadResult):
    schema_version: Literal["fa-approved-skill-read-v2"]
    skill: PurposeSkillEntry


class SkillToolAvailability(SkillToolRequirement):
    available: bool
    reason: Literal["not_captured", "not_bound"] | None = None
    authorization: Literal[False] = False

    @model_validator(mode="after")
    def explained(self):
        if self.available != (self.reason is None):
            raise ValueError("Tool availability needs a reason when it is unavailable")
        return self


class ResourceSkillReadResult(PurposeSkillReadResult):
    schema_version: Literal["fa-approved-skill-read-v3", "fa-approved-skill-read-v4"]
    resources: SkillResources | None = None
    tool_requirements: SkillToolRequirements | None = Field(default=None, exclude_if=lambda value: value is None)
    tool_availability: Annotated[tuple[SkillToolAvailability, ...], Field(max_length=4)] | None = Field(
        default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def declared_tools(self):
        if (self.schema_version == "fa-approved-skill-read-v4") != (self.tool_requirements is not None):
            raise ValueError("Only v4 carries declared tool requirements")
        if self.tool_availability is not None:
            if self.tool_requirements is None or [
                (item.entry_id, item.entry_sha256, item.tool) for item in self.tool_availability
            ] != [(item.entry_id, item.entry_sha256, item.tool) for item in self.tool_requirements.entries]:
                raise ValueError("Availability must describe exactly the declared tool requirements")
        return self


ApprovedSkillCatalog = Annotated[SkillCatalog | PurposeSkillCatalog, Field(discriminator="schema_version")]


class ApprovedSkillSource:
    """Uses the existing verified Actor and human approval/store authority."""

    def __init__(self, artifacts: ArtifactService):
        self.artifacts = artifacts

    def catalog(self, actor: Actor, team: str, selected: tuple[str, ...], *,
                schema_version: str = "fa-approved-skill-catalog-v1") -> SkillCatalog | PurposeSkillCatalog:
        # Hosted imports the DTOs, not the API-only auth dependencies.
        from .auth import Actor

        if not isinstance(actor, Actor):
            raise Problem(403, "SKILL_IDENTITY_DENIED", "an existing server-bound artifact Actor is required")
        self.artifacts.authorize(actor, team)
        if schema_version not in {"fa-approved-skill-catalog-v1", "fa-approved-skill-catalog-v2"}:
            raise Problem(403, "SKILL_ACCESS_DISABLED", "the skill catalog version is not supported")
        purpose_enabled = schema_version == "fa-approved-skill-catalog-v2"
        selected = validate_selection(selected)
        entries = []
        for name in sorted(selected):
            try:
                record = self.artifacts.get(actor, team, name)
            except Problem as error:
                if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
                    continue
                raise
            document = ArtifactDocument.model_validate(record.payload)
            if document.kind != "skill" or document.effective_version_id is None:
                continue
            version = next(value for value in document.versions if value.version_id == document.effective_version_id)
            if (version.approval is None or version.approval.decision != "approve"
                    or version.approval.approver_oid != self.artifacts.approval.approver_oid):
                raise Problem(403, "SKILL_APPROVAL_DENIED", "the configured human must approve this exact version")
            if version.media_type not in {"text/plain", "text/markdown"}:
                raise Problem(403, "SKILL_SOURCE_DENIED", "only approved policy text is eligible")
            entry_type = PurposeSkillEntry if purpose_enabled else SkillEntry
            purpose = ({"description": None if version.skill_purpose is None else version.skill_purpose.description,
                        "metadata_sha256": version.metadata_sha256} if purpose_enabled else {})
            entries.append(entry_type(
                name=name, title=document.title, version_id=version.version_id, sha256=version.sha256,
                byte_length=version.blob.bytes, team=team, source_work=version.source_work,
                source_work_revision=version.source_work_revision,
                native_submit_sha256=None if version.native_source is None else version.native_source.submit_sha256, **purpose,
            ))
        try:
            catalog_type = PurposeSkillCatalog if purpose_enabled else SkillCatalog
            return catalog_type(schema_version=schema_version, entries=tuple(entries),
                                unavailable_count=len(selected) - len(entries))
        except ValueError:
            raise Problem(413, "SKILL_CATALOG_LIMIT", "complete optional catalog does not fit; nothing was truncated") from None

    def read(self, actor: Actor, team: str, selected: tuple[str, ...], expected: SkillEntry | PurposeSkillEntry,
             *, include_resources: bool = False,
             include_tool_requirements: bool = False) -> SkillReadResult | PurposeSkillReadResult | ResourceSkillReadResult:
        schema = ("fa-approved-skill-catalog-v2" if isinstance(expected, PurposeSkillEntry)
                  else "fa-approved-skill-catalog-v1")
        if expected not in self.catalog(actor, team, selected, schema_version=schema).entries:
            raise Problem(403, "SKILL_SOURCE_CHANGED", "original configured approved skill is no longer current")
        policies = self.artifacts.policy_context(actor, team, (expected.name,))
        policy = policies[0]
        if policy["status"] != "approved" or policy["kind"] != "skill":
            raise Problem(403, "SKILL_SOURCE_CHANGED", "original skill approval is unavailable")
        version = ArtifactVersion.model_validate(policy["version"])
        if (version.version_id != expected.version_id or version.sha256 != expected.sha256
                or version.blob.bytes != expected.byte_length
                or version.source_work != expected.source_work
                or version.source_work_revision != expected.source_work_revision
                or (None if version.native_source is None else version.native_source.submit_sha256)
                != expected.native_submit_sha256):
            raise Problem(403, "SKILL_SOURCE_CHANGED", "skill version or source changed during the read")
        if isinstance(expected, PurposeSkillEntry) and (
            expected.metadata_sha256 != version.metadata_sha256
            or expected.description != (None if version.skill_purpose is None else version.skill_purpose.description)
        ):
            raise Problem(403, "SKILL_SOURCE_CHANGED", "approved skill purpose changed during the read")
        if version.skill_tools is not None and not (include_resources and include_tool_requirements):
            raise Problem(403, "SKILL_ACCESS_DISABLED", "this skill needs the versioned native manifest reader")
        if include_resources:
            if not isinstance(expected, PurposeSkillEntry):
                raise Problem(403, "SKILL_ACCESS_DISABLED", "resource reads require approved purpose metadata")
            result = ResourceSkillReadResult(
                schema_version="fa-approved-skill-read-v3" if version.skill_tools is None else "fa-approved-skill-read-v4",
                skill=expected, text=policy["text"], resources=version.skill_resources,
                tool_requirements=version.skill_tools)
        else:
            result_type = PurposeSkillReadResult if isinstance(expected, PurposeSkillEntry) else SkillReadResult
            result = result_type(skill=expected, text=policy["text"],
                                 **({"schema_version": "fa-approved-skill-read-v2"}
                                    if isinstance(expected, PurposeSkillEntry) else {}))
        if expected not in self.catalog(actor, team, selected, schema_version=schema).entries:
            raise Problem(403, "SKILL_SOURCE_CHANGED", "skill approval changed during the read")
        return result
