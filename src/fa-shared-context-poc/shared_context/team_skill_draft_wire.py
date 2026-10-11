"""The Team owner's own skill draft: input shape and server-derived source; no authority or SDK imports.

The same draft content always derives the same source and artifact, so sending it again after a lost reply finds the
original draft instead of creating a second one; changed content is a new draft (Q3: edits are new drafts).
"""

from typing import Annotated, Literal

from pydantic import Field, field_validator

from .behavior_wire import WireModel
from .m3_models import OwnerArtifactSource, policy_text_is_visible
from .native_skill_draft_wire import RESOURCE_DRAFT_REQUEST_BYTES, NativeSkillDraftArgs, draft_material

OWNER_DRAFT_REQUEST_BYTES = RESOURCE_DRAFT_REQUEST_BYTES
# Short enough that four selected skills still fit the Agent's bounded catalog.
OWNER_TITLE_MAX = 64


class OwnerSkillDraftInput(WireModel):
    schema_version: Literal["fa-team-owner-skill-draft-v1"]
    title: Annotated[str, Field(min_length=1, max_length=OWNER_TITLE_MAX)]
    arguments: NativeSkillDraftArgs

    @field_validator("title")
    @classmethod
    def printable_title(cls, value):
        if not value.strip() or value != value.strip() or not policy_text_is_visible(value):
            raise ValueError("Skill names must be one printable line without surrounding spaces")
        return value


def owner_material(draft: OwnerSkillDraftInput) -> dict:
    return {"kind": "team-owner-skill-draft-v1", "title": draft.title, "draft": draft_material(draft.arguments)}


def owner_source(tenant_id: str, team_id: str, owner_oid: str, draft: OwnerSkillDraftInput) -> OwnerArtifactSource:
    from .behavior_native_turn_wire import material_digest

    return OwnerArtifactSource(kind="team-owner", tenant_id=tenant_id, team_id=team_id, owner_oid=owner_oid,
                               submit_sha256=material_digest(owner_material(draft)))


def owner_artifact_id(source: OwnerArtifactSource) -> str:
    from .behavior_native_turn_wire import material_digest

    return material_digest({"kind": "team-owner-skill-draft", "source": source.model_dump(mode="json")})


def owner_version_id(source: OwnerArtifactSource) -> str:
    return source.submit_sha256[:32]
