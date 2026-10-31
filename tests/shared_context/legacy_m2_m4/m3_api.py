from __future__ import annotations

import re
from typing import Callable

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from shared_context.auth import Actor
from shared_context.errors import Problem
from shared_context.m3_artifacts import ArtifactService, Principal, validate_id
from shared_context.m3_models import (
    MAX_ARTIFACT_BYTES, ApprovalInput, CreateArtifact, SkillDeactivationInput, SkillPurpose, SkillResources,
    SkillToolRequirements, VersionInput,
)
from shared_context.models import EmptyBody
from shared_context.storage import Record

ARTIFACT_PREFIX = "/teams/{team}/artifacts/{artifact}"


def is_artifact_upload(scope: dict) -> bool:
    return scope["method"] == "PUT" and re.fullmatch(
        r"/teams/[^/]+/artifacts/[^/]+/versions/[^/]+", scope["path"],
    ) is not None


def exact_header(request: Request, name: str, *, required: bool = False) -> str | None:
    values = request.headers.getlist(name)
    if len(values) > 1 or (required and not values):
        raise Problem(422, "M3_INVALID_HEADERS", "a single " + name + " header is required")
    return values[0] if values else None


async def read_artifact_body(request: Request) -> bytes:
    declared = exact_header(request, "content-length")
    if declared is not None:
        if re.fullmatch(r"[0-9]{1,10}", declared) is None:
            raise Problem(422, "M3_INVALID_LENGTH", "invalid Content-Length")
        if int(declared) > MAX_ARTIFACT_BYTES:
            raise Problem(413, "M3_FILE_TOO_LARGE", "artifact exceeds 512 KiB")
    parts, total = [], 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_ARTIFACT_BYTES:
                raise Problem(413, "M3_FILE_TOO_LARGE", "artifact exceeds 512 KiB")
            parts.append(chunk)
    except ClientDisconnect as exc:
        raise Problem(400, "M3_UPLOAD_INCOMPLETE", "artifact upload was disconnected") from exc
    if declared is not None and total != int(declared):
        raise Problem(400, "M3_UPLOAD_INCOMPLETE", "body length differs from Content-Length")
    return b"".join(parts)


def add_artifact_routes(app: FastAPI, service: ArtifactService, verifier: Callable) -> None:
    identity = Depends(verifier)

    def result(record: Record):
        return JSONResponse(record.response(), headers={"ETag": record.etag})

    @app.post(ARTIFACT_PREFIX, status_code=201)
    def create(team: str, artifact: str, body: CreateArtifact, principal: Principal = identity):
        response = result(service.create(principal, team, artifact, body))
        response.status_code = 201
        return response

    @app.get(ARTIFACT_PREFIX)
    def get(team: str, artifact: str, principal: Principal = identity):
        return result(service.get(principal, team, artifact))

    @app.get("/teams/{team}/skill-reviews")
    def skill_reviews(team: str, request: Request, principal: Principal = identity):
        params = request.query_params
        if set(params) - {"after"} or len(params.getlist("after")) > 1:
            raise Problem(422, "INVALID_REQUEST", "the skill review list accepts only one after cursor")
        value = service.skill_reviews(principal, team, params.get("after"))
        return JSONResponse(value.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.put(ARTIFACT_PREFIX + "/versions/{version_id}")
    async def upload(team: str, artifact: str, version_id: str, request: Request, principal: Principal = identity):
        service.authorize(principal, team)
        if not isinstance(principal, Actor):
            raise Problem(403, "M3_AGENT_REQUIRED", "only an agent may upload artifact bytes")
        validate_id(artifact)
        validate_id(version_id, version=True)
        if request.query_params:
            raise Problem(422, "INVALID_REQUEST", "artifact upload does not accept query parameters")
        if exact_header(request, "content-type") != "application/octet-stream":
            raise Problem(415, "M3_CONTENT_TYPE", "Content-Type must be application/octet-stream")
        if exact_header(request, "content-encoding") not in (None, "identity"):
            raise Problem(415, "M3_CONTENT_ENCODING", "encoded bodies are not accepted")
        try:
            purpose = exact_header(request, "x-artifact-skill-purpose")
            resources = exact_header(request, "x-artifact-skill-resources")
            tools = exact_header(request, "x-artifact-skill-tools")
            metadata = VersionInput.model_validate({
                "filename": exact_header(request, "x-artifact-filename", required=True),
                "media_type": exact_header(request, "x-artifact-media-type", required=True),
                "sha256": exact_header(request, "x-artifact-sha256", required=True),
                "source_work": exact_header(request, "x-artifact-source-work"),
                "skill_purpose": None if purpose is None else SkillPurpose.from_header(purpose),
                "skill_resources": None if resources is None else SkillResources.from_header(resources),
                "skill_tools": None if tools is None else SkillToolRequirements.from_header(tools),
            })
        except ValueError as exc:
            raise Problem(422, "M3_INVALID_HEADERS", "invalid artifact metadata or versioned skill purpose") from exc
        etag = exact_header(request, "if-match")
        data = await read_artifact_body(request)
        record = await run_in_threadpool(service.upload, principal, team, artifact, version_id, etag, metadata, data)
        return result(record)

    @app.post(ARTIFACT_PREFIX + "/versions/{version_id}/reconcile")
    def reconcile(team: str, artifact: str, version_id: str, request: Request,
                  body: EmptyBody, principal: Principal = identity):
        return result(service.reconcile(principal, team, artifact, version_id, exact_header(request, "if-match")))

    @app.get(ARTIFACT_PREFIX + "/versions/{version_id}")
    def version(team: str, artifact: str, version_id: str, principal: Principal = identity):
        return service.version(principal, team, artifact, version_id).model_dump()

    @app.get(ARTIFACT_PREFIX + "/versions/{version_id}/resources/{resource_name}")
    def skill_resource(team: str, artifact: str, version_id: str, resource_name: str,
                       principal: Principal = identity):
        value = service.skill_resource(principal, team, artifact, version_id, resource_name)
        return JSONResponse(value.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.get(ARTIFACT_PREFIX + "/versions/{version_id}/content")
    def download(team: str, artifact: str, version_id: str, request: Request, principal: Principal = identity):
        version, data = service.download(principal, team, artifact, version_id, exact_header(request, "if-match"))
        return Response(data, media_type="application/octet-stream", headers={
            "ETag": version.blob.etag, "X-Artifact-SHA256": version.sha256,
            "Content-Disposition": 'attachment; filename="' + version.filename + '"',
        })

    @app.post(ARTIFACT_PREFIX + "/decisions")
    def decide(team: str, artifact: str, body: ApprovalInput, request: Request, principal: Principal = identity):
        return result(service.decide(principal, team, artifact, exact_header(request, "if-match"), body))

    @app.post(ARTIFACT_PREFIX + "/deactivations")
    def deactivate(team: str, artifact: str, body: SkillDeactivationInput,
                   request: Request, principal: Principal = identity):
        if request.query_params:
            raise Problem(422, "INVALID_REQUEST", "skill deactivation does not accept query parameters")
        return result(service.deactivate_skill(principal, team, artifact, exact_header(request, "if-match"), body))

    @app.get(ARTIFACT_PREFIX + "/effective")
    def effective(team: str, artifact: str, principal: Principal = identity):
        return service.effective(principal, team, artifact).model_dump()
