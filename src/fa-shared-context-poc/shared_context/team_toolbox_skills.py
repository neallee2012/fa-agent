"""Skills attached to the Team's own Foundry toolbox, offered to the Team owner as drafts to import (ADR-0014).

The API reads the default version's skill references and each skill's zip with the pinned azure-ai-projects SDK. This
module only parses those bytes and decides, with the owner draft's own validators, whether one can be imported; the
import itself is the existing owner draft, so approval still binds the sha256 of FA's own copy. SDK-independent.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
import zlib
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from .behavior_wire import Identifier, WireModel
from .team_mcp_wire import ToolboxVersion
from .team_skill_draft_wire import OwnerSkillDraftInput

# Eight bodies at their bound, escaped as JSON, stay inside the Team MCP route's 256 KiB response.
MAX_TOOLBOX_SKILLS = 8
MAX_SKILL_BODY_BYTES = 8192
# Bounds the zip read before parsing; one SKILL.md of at most 8192 bytes always fits.
MAX_SKILL_ZIP_BYTES = 64 * 1024
# Only importable skills carry a description, already held to the owner draft's 512-byte purpose.
MAX_DESCRIPTION_CHARS = 1024

SkillRefusal = Literal[
    "unsupported_type", "unavailable", "too_large", "multiple_files", "no_skill_md",
    "invalid_name", "invalid_purpose", "invalid_body",
]


class TeamToolboxSkill(WireModel):
    name: Annotated[str, Field(min_length=1, max_length=256)]
    skill_version: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    description: Annotated[str, Field(max_length=MAX_DESCRIPTION_CHARS)] | None = None
    body: Annotated[str, Field(min_length=1, max_length=MAX_SKILL_BODY_BYTES)] | None = None
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    refusal: SkillRefusal | None = None

    @model_validator(mode="after")
    def importable_has_content(self):
        if (self.refusal is None) != (self.body is not None and self.sha256 is not None):
            raise ValueError("an importable skill has its body and sha256; a refused one has neither")
        return self


class TeamToolboxSkills(WireModel):
    schema_version: Literal["fa-team-toolbox-skills-v1"] = "fa-team-toolbox-skills-v1"
    team_id: Identifier
    toolbox: str
    default_version: ToolboxVersion | None
    skills: Annotated[tuple[TeamToolboxSkill, ...], Field(max_length=MAX_TOOLBOX_SKILLS)]
    # References past the first MAX_TOOLBOX_SKILLS, not read.
    omitted: Annotated[int, Field(ge=0)] = 0


def refused(name: str, refusal: SkillRefusal, *, skill_version=None) -> TeamToolboxSkill:
    return TeamToolboxSkill(name=name[:256] or "?", skill_version=skill_version, refusal=refusal)


def _skill_md(name: str, archive: bytes) -> bytes | SkillRefusal:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            files = [info for info in bundle.infolist() if not info.is_dir()]
            if len(files) > 1:
                return "multiple_files"
            if not files or files[0].filename not in {"SKILL.md", f"{name}/SKILL.md"}:
                return "no_skill_md"
            with bundle.open(files[0]) as handle:
                data = handle.read(MAX_SKILL_BODY_BYTES + 1)
    # Corrupt (zlib.error, EOFError), encrypted (RuntimeError) or odd archives refuse this one skill, not the list.
    except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, RuntimeError, OSError, ValueError,
            NotImplementedError):
        return "unavailable"
    return "too_large" if len(data) > MAX_SKILL_BODY_BYTES else data


def toolbox_skill(name: str, skill_version: str, description: str | None, archive: bytes | None) -> TeamToolboxSkill:
    """One referenced skill as an import candidate; ``archive`` is None when its zip exceeded the read cap. It is
    importable only when the owner draft would accept it as it is: name as the title, description as the purpose."""
    def refuse(refusal):
        return refused(name, refusal, skill_version=skill_version)

    if archive is None:
        return refuse("too_large")
    data = _skill_md(name, archive)
    if isinstance(data, str):
        return refuse(data)
    try:
        body = data.decode("utf-8")
    except UnicodeDecodeError:
        return refuse("invalid_body")
    try:
        OwnerSkillDraftInput.model_validate({"schema_version": "fa-team-owner-skill-draft-v1", "title": name,
                                             "arguments": {"purpose": description or "", "body": body}})
    except ValidationError as error:
        location = error.errors()[0]["loc"]
        return refuse("invalid_name" if location[0] == "title"
                      else "invalid_purpose" if location[-1] == "purpose" else "invalid_body")
    return TeamToolboxSkill(name=name, skill_version=skill_version, description=description, body=body,
                            sha256=hashlib.sha256(data).hexdigest())
