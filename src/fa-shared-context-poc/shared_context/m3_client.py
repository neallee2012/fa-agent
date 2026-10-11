from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable
from uuid import uuid4

import httpx
from pydantic import TypeAdapter

from .errors import Problem
from .m3_models import (
    MAX_ARTIFACT_BYTES, ApprovalInput, ArtifactDocument, ArtifactVersion, CreateArtifact, ETag,
    Filename, PendingVersion, SkillDeactivationInput, SkillResourceRead, SkillReviewEntry, SkillReviewList, VersionInput,
    validate_id,
)
from .records import Record


class ArtifactClient:
    def __init__(self, http: httpx.Client, team: str, token: Callable[[], str], *, api_url: str = ""):
        self.http, self.team, self.token = http, validate_id(team), token
        self.api_url = api_url.rstrip("/")

    def _path(self, artifact: str) -> str:
        return f"{self.api_url}/teams/{self.team}/artifacts/{validate_id(artifact)}"

    def _request(self, method: str, path: str, *, body: dict | None = None,
                 etag: str | None = None, binary: bool = False, content: bytes | None = None,
                 file: VersionInput | None = None) -> tuple[httpx.Headers, bytes]:
        limit = MAX_ARTIFACT_BYTES if binary else 64 * 1024
        headers = {"Authorization": "Bearer " + self.token()}
        if etag is not None:
            headers["If-Match"] = etag
        if file is not None:
            headers.update({
                "Content-Type": "application/octet-stream", "X-Artifact-Filename": file.filename,
                "X-Artifact-Media-Type": file.media_type, "X-Artifact-SHA256": file.sha256,
            })
            if file.source_work is not None:
                headers["X-Artifact-Source-Work"] = file.source_work
            if file.skill_purpose is not None:
                headers["X-Artifact-Skill-Purpose"] = file.skill_purpose.header()
            if file.skill_resources is not None:
                headers["X-Artifact-Skill-Resources"] = file.skill_resources.header()
            if file.skill_tools is not None:
                headers["X-Artifact-Skill-Tools"] = file.skill_tools.header()
        try:
            with self.http.stream(method, path, headers=headers, json=body, content=content, follow_redirects=False) as response:
                chunks, total = [], 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise Problem(502, "M3_RESPONSE_TOO_LARGE", "remote response exceeds its declared contract")
                    chunks.append(chunk)
                data = b"".join(chunks)
                if not response.is_success:
                    try:
                        failure = json.loads(data)["error"]
                        code, message = failure["code"], failure["message"]
                        if not isinstance(code, str) or not isinstance(message, str):
                            raise ValueError("invalid error fields")
                    except (ValueError, KeyError, TypeError) as exc:
                        raise Problem(502, "M3_HTTP_ERROR", "remote request failed without a valid error receipt",
                                      http_status=response.status_code) from exc
                    raise Problem(response.status_code, code, message)
                return response.headers, data
        except httpx.HTTPError as exc:
            raise Problem(502, "M3_REQUEST_UNKNOWN", "request failed; read the artifact before retrying a decision") from exc

    def _snapshot(self, artifact: str, headers: httpx.Headers, data: bytes) -> Record:
        try:
            payload = json.loads(data)
            etag = TypeAdapter(ETag).validate_python(payload.pop("etag"), strict=True)
            document = ArtifactDocument.model_validate(payload)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise Problem(502, "M3_RESPONSE_INVALID", "remote artifact metadata is invalid") from exc
        if document.artifact_id != artifact or document.team != self.team or not etag or headers.get("etag") != etag:
            raise Problem(502, "M3_RESPONSE_INVALID", "remote artifact identity or ETag differs")
        return Record(document.model_dump(), etag)

    def get(self, artifact: str) -> Record:
        return self._snapshot(artifact, *self._request("GET", self._path(artifact)))

    def skill_reviews(self, after: str | None = None) -> SkillReviewList:
        path = f"{self.api_url}/teams/{self.team}/skill-reviews"
        if after is not None:
            path += "?after=" + validate_id(after)
        headers, raw = self._request("GET", path)
        try:
            result = SkillReviewList.model_validate_json(raw)
        except ValueError:
            raise Problem(502, "M3_RESPONSE_INVALID", "skill review list response is invalid") from None
        if headers.get("cache-control") != "no-store" or result.team != self.team or result.after != after:
            raise Problem(502, "M3_RESPONSE_INVALID", "skill review page differs from its request or is cacheable")
        return result

    def all_skill_reviews(self) -> tuple[SkillReviewEntry, ...]:
        """Follow every page; each validated page cursor advances, so the loop always ends."""
        entries, after = [], None
        while True:
            page = self.skill_reviews(after)
            entries.extend(page.entries)
            if page.next_after is None:
                return tuple(entries)
            after = page.next_after

    def create(self, artifact: str, body: CreateArtifact) -> Record:
        result = self._snapshot(artifact, *self._request("POST", self._path(artifact), body=body.model_dump()))
        if any(result.payload[key] != value for key, value in body.model_dump().items()):
            raise Problem(502, "M3_RESPONSE_INVALID", "created artifact acknowledgement differs")
        return result

    def upload(self, artifact: str, version_id: str, etag: str, file: VersionInput, data: bytes) -> Record:
        validate_id(version_id, version=True)
        if not 1 <= len(data) <= MAX_ARTIFACT_BYTES or hashlib.sha256(data).hexdigest() != file.sha256:
            raise Problem(422, "M3_FILE_INTEGRITY", "upload bytes differ from their metadata")
        result = self._snapshot(artifact, *self._request(
            "PUT", self._path(artifact) + "/versions/" + version_id, etag=etag, content=data, file=file,
        ))
        version = self.version(result, version_id)
        if (version.sha256 != file.sha256 or version.blob.bytes != len(data)
                or version.filename != file.filename or version.media_type != file.media_type
                or version.skill_purpose != file.skill_purpose
                or version.skill_resources != file.skill_resources
                or version.skill_tools != file.skill_tools
                or (file.source_work is not None and version.source_work != file.source_work)):
            raise Problem(502, "M3_RESPONSE_INVALID", "uploaded version acknowledgement differs")
        return result

    def reconcile(self, artifact: str, snapshot: Record) -> Record:
        """Request original pending-version finalization without retransmitting bytes."""
        path = self._path(artifact)
        try:
            document = ArtifactDocument.model_validate(snapshot.payload)
            etag = TypeAdapter(ETag).validate_python(snapshot.etag, strict=True)
        except (ValueError, TypeError, AttributeError) as exc:
            raise Problem(422, "M3_RECONCILE_INVALID", "a valid original artifact snapshot is required") from exc
        if document.artifact_id != artifact or document.team != self.team:
            raise Problem(422, "M3_RECONCILE_INVALID", "the snapshot belongs to another artifact or team")
        pending = document.pending
        if pending is None:
            raise Problem(409, "M3_UPLOAD_NOT_PENDING", "read the original version; there is no pending upload to reconcile")
        result = self._snapshot(artifact, *self._request(
            "POST", path + "/versions/" + pending.version_id + "/reconcile", etag=etag, body={},
        ))
        completed = ArtifactDocument.model_validate(result.payload)
        version = completed.versions[-1] if completed.versions else None
        expected_fields = PendingVersion.model_fields.keys() - {"bytes", "status"}
        if (version is None or version.version_id != pending.version_id
                or version.number != len(document.versions) + 1
                or version.blob.bytes != pending.bytes or version.approval is not None
                or any(getattr(version, name) != getattr(pending, name) for name in expected_fields)):
            raise Problem(502, "M3_RESPONSE_INVALID", "reconciled version differs from the original pending upload")
        expected_document = document.model_dump(mode="json")
        expected_document["revision"] += 1
        expected_document["pending"] = None
        expected_document["versions"].append(version.model_dump(mode="json"))
        if completed.model_dump(mode="json") != expected_document:
            raise Problem(502, "M3_RESPONSE_INVALID", "reconciliation changed metadata outside the original pending version")
        return result

    @staticmethod
    def version(record: Record, version_id: str) -> ArtifactVersion:
        validate_id(version_id, version=True)
        document = ArtifactDocument.model_validate(record.payload)
        version = next((item for item in document.versions if item.version_id == version_id), None)
        if version is None:
            raise Problem(404, "M3_VERSION_NOT_FOUND", "the requested completed version is absent")
        return version

    def content(self, artifact: str, version: ArtifactVersion) -> bytes:
        headers, data = self._request(
            "GET", self._path(artifact) + "/versions/" + version.version_id + "/content",
            etag=version.blob.etag, binary=True,
        )
        if (headers.get("etag") != version.blob.etag or headers.get("x-artifact-sha256") != version.sha256
                or len(data) != version.blob.bytes or hashlib.sha256(data).hexdigest() != version.sha256):
            raise Problem(502, "M3_FILE_INTEGRITY", "downloaded bytes or headers differ from the selected version")
        return data

    def skill_resource(self, artifact: str, version_id: str, resource_name: str) -> SkillResourceRead:
        validate_id(version_id, version=True)
        TypeAdapter(Filename).validate_python(resource_name, strict=True)
        headers, raw = self._request(
            "GET", self._path(artifact) + "/versions/" + version_id + "/resources/" + resource_name,
        )
        try:
            result = SkillResourceRead.model_validate_json(raw)
        except ValueError:
            raise Problem(502, "M3_RESPONSE_INVALID", "skill resource response is invalid") from None
        if (headers.get("cache-control") != "no-store" or result.skill_artifact_id != artifact
                or result.skill_version_id != version_id or result.resource.name != resource_name):
            raise Problem(502, "M3_RESPONSE_INVALID", "skill resource response differs from its original selection")
        return result

    def download(self, artifact: str, version_id: str, output: Path) -> ArtifactVersion:
        if output.exists() or output.is_symlink():
            raise FileExistsError("download never replaces an existing path")
        version = self.version(self.get(artifact), version_id)
        data = self.content(artifact, version)
        temporary = output.parent / (".fa-m3-download-" + uuid4().hex)
        created = False
        try:
            with temporary.open("xb") as stream:
                created = True
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # A same-directory hard link publishes the complete file without replacing a racing destination.
            os.link(temporary, output)
        finally:
            if created:
                temporary.unlink()
        return version

    def decide(self, artifact: str, snapshot: Record, body: ApprovalInput) -> Record:
        result = self._snapshot(artifact, *self._request(
            "POST", self._path(artifact) + "/decisions", etag=snapshot.etag, body=body.model_dump(),
        ))
        version = self.version(result, body.version_id)
        if version.approval is None or version.approval.model_dump(include=set(ApprovalInput.model_fields)) != body.model_dump():
            raise Problem(502, "M3_DECISION_UNKNOWN", "decision acknowledgement differs; read before continuing")
        if body.decision == "approve" and result.payload["effective_version_id"] != body.version_id:
            raise Problem(502, "M3_DECISION_UNKNOWN", "approved version was not acknowledged as effective")
        return result

    def deactivate(self, artifact: str, snapshot: Record, body: SkillDeactivationInput) -> Record:
        original = self.version(snapshot, body.version_id)
        result = self._snapshot(artifact, *self._request(
            "POST", self._path(artifact) + "/deactivations", etag=snapshot.etag, body=body.model_dump(),
        ))
        document = ArtifactDocument.model_validate(result.payload)
        decision = next((item for item in document.deactivations if item.decision_id == body.decision_id), None)
        if (decision is None
                or decision.model_dump(include=set(SkillDeactivationInput.model_fields)) != body.model_dump()
                or self.version(result, body.version_id) != original):
            raise Problem(502, "M3_DEACTIVATION_UNKNOWN", "deactivation acknowledgement differs; read before continuing")
        return result
