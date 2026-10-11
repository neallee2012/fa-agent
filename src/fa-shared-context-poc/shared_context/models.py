from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
Identifier = Annotated[str, Field(pattern=ID_PATTERN, min_length=1, max_length=64)]
Goal = Annotated[str, Field(min_length=1, max_length=4096)]
Progress = Annotated[str, Field(max_length=8192)]
ArtifactText = Annotated[str, Field(max_length=24576)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmptyBody(StrictModel):
    pass


class CreateWork(StrictModel):
    goal: Goal
    progress: Progress = ""
    artifact_text: ArtifactText = ""


class UpdateWork(StrictModel):
    goal: Goal | None = None
    progress: Progress | None = None
    artifact_text: ArtifactText | None = None

    @model_validator(mode="after")
    def nonempty_changes(self):
        if not self.model_fields_set or any(getattr(self, k) is None for k in self.model_fields_set):
            raise ValueError("at least one non-null editable field is required")
        return self


class Handoff(StrictModel):
    to_actor: Identifier


class SyntheticMemory(StrictModel):
    marker: Identifier
    value: Identifier

    def chat(self) -> list[dict]:
        return [{
            "role": "user", "type": "message",
            "content": [{"type": "input_text", "text": (
                f"My preferred color theme for the dashboard named {self.marker} is {self.value}. "
                f"This is a long-term appearance preference specific to that dashboard."
            )}],
        }]


class SearchMemory(StrictModel):
    query: Annotated[str, Field(min_length=1, max_length=2048)]
