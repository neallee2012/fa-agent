"""Native skill-draft data and original effect proof; no authority or SDK imports."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .behavior_native_capability_wire import NativeTurnScope
from .behavior_skill_draft import SkillDraftToolArgs, skill_draft_bytes
from .behavior_wire import Digest, Identifier, WireModel
from .m3_models import (
    Filename, NativeArtifactSource, SkillPurpose, SkillResourceReference, SkillResources, SkillReviewStatus,
    VersionId, skill_resource_text,
)

SLOT = "native-skill-draft"
LEGACY_DRAFT_REQUEST_BYTES = 32 * 1024
RESOURCE_DRAFT_REQUEST_BYTES = 128 * 1024


class NativeDraftResource(WireModel):
    name: Filename
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    text: Annotated[str, Field(min_length=1, max_length=8192)]

    @field_validator("text")
    @classmethod
    def valid_text(cls, value):
        raw = value.encode("utf-8")
        skill_resource_text(raw, hashlib.sha256(raw).hexdigest())
        return value

    def material(self):
        raw = self.text.encode("utf-8")
        return {"name": self.name, "media_type": self.media_type,
                "byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


class NativeSkillDraftArgs(SkillDraftToolArgs):
    resources: Annotated[list[NativeDraftResource], Field(max_length=4)] = Field(
        default_factory=list, exclude_if=lambda value: not value)

    @field_validator("resources")
    @classmethod
    def unique_names(cls, value):
        if len({item.name for item in value}) != len(value):
            raise ValueError("Draft resource aliases must be unique")
        return sorted(value, key=lambda item: item.name)


def artifact_id(tenant, team, agent, cid, rid):
    from .behavior_native_turn_wire import material_digest

    return material_digest({"kind": "native-skill-draft", "tenant": tenant, "team": team,
                            "agent": agent, "cid": cid, "rid": rid})


def draft_material(arguments: SkillDraftToolArgs):
    material = {"kind": "native-skill-draft-v1", "purpose": arguments.purpose,
                "body_sha256": hashlib.sha256(skill_draft_bytes(arguments.body)).hexdigest()}
    resources = getattr(arguments, "resources", ())
    if resources:
        material.update(kind="native-skill-draft-resources-v1",
                        resources=[item.material() for item in resources])
    return material


def resource_reference(source: NativeArtifactSource, resource: NativeDraftResource) -> SkillResourceReference:
    from .behavior_native_turn_wire import material_digest

    identity = {"kind": "native-skill-resource-v1", "source": source.model_dump(mode="json"), "name": resource.name}
    return SkillResourceReference(
        name=resource.name, artifact_id=material_digest(identity),
        version_id=material_digest({**identity, "resource": resource.material()})[:32],
        sha256=resource.material()["sha256"])


def resource_manifest(source, arguments):
    resources = getattr(arguments, "resources", ())
    return None if not resources else SkillResources(entries=[resource_reference(source, item) for item in resources])


class NativeSkillDraftWrite(NativeTurnScope):
    arguments: NativeSkillDraftArgs

    @field_validator("arguments", mode="before")
    @classmethod
    def compatible_arguments(cls, value):
        return value.model_dump(mode="json") if isinstance(value, SkillDraftToolArgs) else value


class NativeSkillDraftIntentRead(NativeSkillDraftWrite):
    operation: Literal["skill_draft_intent"] = "skill_draft_intent"


class NativeSkillDraftProof(WireModel):
    agent_id: Identifier
    conversation_id: Identifier
    request_id: Identifier
    submit_sha256: Digest
    input_digest: Digest
    effect_status: Literal["pending", "done"]


class NativeSkillDraftReceipt(WireModel):
    schema_version: Literal["fa-native-skill-draft-receipt-v1", "fa-native-skill-draft-receipt-v2"] = (
        "fa-native-skill-draft-receipt-v1")
    state_scope: Literal["at-original-draft-write"] = "at-original-draft-write"
    native_source: NativeArtifactSource
    artifact_id: Identifier
    version_id: VersionId
    sha256: Digest
    byte_length: Annotated[int, Field(ge=1, le=8192)]
    purpose: SkillPurpose
    metadata_sha256: Digest
    approved: Literal[False] = False
    effective_version_id: None = None
    resources: SkillResources | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def resource_version(self):
        if (self.schema_version == "fa-native-skill-draft-receipt-v2") != (self.resources is not None):
            raise ValueError("Only the attached draft receipt carries a resource manifest")
        return self

    @field_validator("approved", mode="before")
    @classmethod
    def unapproved_only(cls, value):
        if value is not False:
            raise ValueError("draft intake never claims human approval")
        return value


class NativeSkillDraftView(WireModel):
    agent_id: Identifier
    conversation_id: Identifier
    request_id: Identifier
    state: Literal["not_recorded"] | SkillReviewStatus
    receipt: NativeSkillDraftReceipt | None = None
    body: str | None = None

    @model_validator(mode="after")
    def original_body(self):
        if (self.receipt is None) != (self.body is None):
            raise ValueError("a draft body requires its complete saved receipt")
        if self.receipt is not None:
            source = self.receipt.native_source
            data = skill_draft_bytes(self.body)
            if ((source.agent_id, source.conversation_id, source.request_id) != (
                    self.agent_id, self.conversation_id, self.request_id)
                    or len(data) != self.receipt.byte_length
                    or hashlib.sha256(data).hexdigest() != self.receipt.sha256):
                raise ValueError("the draft body differs from the original receipt")
        return self
