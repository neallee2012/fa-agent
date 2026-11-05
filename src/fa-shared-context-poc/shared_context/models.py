from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
Identifier = Annotated[str, Field(pattern=ID_PATTERN, min_length=1, max_length=64)]
Goal = Annotated[str, Field(min_length=1, max_length=4096)]
Progress = Annotated[str, Field(max_length=8192)]
ArtifactText = Annotated[str, Field(max_length=24576)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateWork(StrictModel):
    goal: Goal
    progress: Progress = ""
    artifact_text: ArtifactText = ""


class Handoff(StrictModel):
    to_actor: Identifier
