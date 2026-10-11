from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .errors import Problem
from .m3_client import ArtifactClient
from .m3_models import ArtifactKind, CreateArtifact, VersionId, VersionInput
from .models import Identifier, StrictModel
from .records import Record


class ArtifactTask(StrictModel):
    artifact_id: Identifier
    kind: ArtifactKind
    title: Annotated[str, Field(min_length=1, max_length=256)]
    version_id: VersionId
    mode: Literal["create", "revise"]
    base_version_id: VersionId | None = None

    @model_validator(mode="after")
    def revision_source(self):
        if (self.mode == "revise") != (self.base_version_id is not None):
            raise ValueError("only a revision requires an exact source version")
        if self.base_version_id == self.version_id:
            raise ValueError("a revision must create a new immutable version")
        return self


class ArtifactTools:
    def __init__(self, client: ArtifactClient, task: ArtifactTask, work: str):
        self.client, self.task, self.work = client, task, work

    def existing(self) -> Record:
        record = self.client.get(self.task.artifact_id)
        if record.payload["kind"] != self.task.kind:
            raise Problem(409, "M3_TASK_CONFLICT", "the existing artifact kind differs from the assigned task")
        return record

    def read_source(self) -> dict:
        if self.task.mode != "revise" or self.task.base_version_id is None:
            raise Problem(403, "M3_TASK_CONFLICT", "this task has no assigned revision source")
        record = self.existing()
        version = self.client.version(record, self.task.base_version_id)
        if version.media_type not in ("text/plain", "text/markdown") or version.blob.bytes > 8192:
            raise Problem(422, "M3_TASK_FORMAT", "this assistant slice accepts source text up to 8 KiB")
        data = self.client.content(self.task.artifact_id, version)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Problem(422, "M3_TASK_FORMAT", "revision source must be UTF-8 text") from exc
        return {"artifact_id": self.task.artifact_id, "version": version.model_dump(), "text": text}

    def create(self) -> Record:
        if self.task.mode != "create":
            raise Problem(403, "M3_TASK_CONFLICT", "a revision cannot create another artifact")
        return self.client.create(self.task.artifact_id, CreateArtifact(
            kind=self.task.kind, title=self.task.title, source_work=self.work,
        ))

    def submit(self, metadata: VersionInput, data: bytes) -> Record:
        record = self.existing()
        if self.task.mode == "revise":
            self.client.version(record, self.task.base_version_id)
        elif record.payload["source_work"] != self.work or record.payload["versions"]:
            raise Problem(409, "M3_TASK_CONFLICT", "a create task cannot reuse an existing artifact history")
        return self.client.upload(self.task.artifact_id, self.task.version_id, record.etag, metadata, data)
