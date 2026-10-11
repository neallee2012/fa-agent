"""Owner skill review routes for the behavior API, backed by the existing M3 service."""

from __future__ import annotations

import hashlib
import logging

from azure.core.exceptions import AzureError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .behavior_api import _body, _principal_dependency, _response
from .behavior_skill_draft import skill_draft_bytes, skill_purpose
from .errors import Problem
from .m3_auth import HumanApprover
from .m3_models import (
    OWNER_AUTHOR, ApprovalInput, ArtifactDocument, SkillDeactivationInput, SkillReviewEntry, VersionInput,
    skill_resource_text, validate_id,
)
from .native_skill_draft_api import NativeSkillDraftGateway
from .native_skill_draft_wire import resource_manifest, resource_reference
from .team_skill_draft_wire import (
    OWNER_DRAFT_REQUEST_BYTES, OwnerSkillDraftInput, owner_artifact_id, owner_source, owner_version_id,
)

PREFIX = "/team-skills/v1"
DECISION_BODY_BYTES = 4096
_log = logging.getLogger(__name__)


def _problem(error: Problem) -> JSONResponse:
    return JSONResponse(error.body, status_code=error.status, headers={"Cache-Control": "no-store"})


async def _reply(call):
    try:
        return await call()
    except Problem as error:
        return _problem(error)


def _service(composition):
    service = getattr(getattr(getattr(composition, "assistant", None), "authority", None), "artifacts", None)
    if service is None:
        raise Problem(503, "TEAM_SKILLS_UNAVAILABLE", "the Team skill review authority is unavailable")
    return service


def _owner_principal(service, principal, action: str) -> HumanApprover:
    if (
        getattr(principal, "kind", None) != "human"
        or principal.oid != service.approval.approver_oid
        or principal.team != service.approval.team
        or action not in principal.actions
    ):
        raise Problem(403, "TEAM_SKILLS_OWNER_ONLY", "only the configured Team owner may review Team skills")
    return HumanApprover(service.approval.approver_oid, service.approval.team)


async def _no_body_or_query(request: Request, *, allow_after: bool = False) -> str | None:
    params = request.query_params
    if allow_after:
        if set(params) - {"after"} or len(params.getlist("after")) > 1:
            raise Problem(422, "TEAM_SKILLS_INVALID_REQUEST", "the review list accepts only one after cursor")
        after = params.get("after")
        if after is not None:
            validate_id(after)
    elif params:
        raise Problem(422, "TEAM_SKILLS_INVALID_REQUEST", "this read takes no query parameters")
    try:
        async for chunk in request.stream():
            if chunk:
                raise Problem(422, "TEAM_SKILLS_INVALID_REQUEST", "read endpoints do not accept a body")
    except ClientDisconnect as exc:
        raise Problem(400, "TEAM_SKILLS_INVALID_REQUEST", "request body was disconnected") from exc
    return params.get("after") if allow_after else None


def _exact_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if len(values) > 1:
        raise Problem(422, "TEAM_SKILLS_INVALID_REQUEST", "a single " + name + " header is required")
    return values[0] if values else None


def _record_response(record):
    response = _response(record.response())
    response.headers["ETag"] = record.etag
    return response


def _skill_record(service, principal, artifact: str):
    record = service.get(principal, service.approval.team, artifact)
    try:
        document = ArtifactDocument.model_validate(record.payload)
    except (ValidationError, ValueError, TypeError) as exc:
        raise Problem(502, "M3_STATE_INVALID", "artifact state could not be verified") from exc
    if document.kind != "skill":
        raise Problem(409, "TEAM_SKILL_NOT_SKILL", "only skill artifacts can be reviewed here")
    return record, document


def _version(service, principal, artifact: str, version_id: str):
    _skill_record(service, principal, artifact)
    return service.version(principal, service.approval.team, artifact, version_id)


def _content(service, principal, artifact: str, version_id: str):
    version = _version(service, principal, artifact, version_id)
    return service.download(principal, service.approval.team, artifact, version_id, version.blob.etag)


def _resource(service, principal, artifact: str, version_id: str, name: str) -> dict:
    _, document = _skill_record(service, principal, artifact)
    version = next((item for item in document.versions if item.version_id == version_id), None)
    if version is None:
        raise Problem(404, "M3_VERSION_NOT_FOUND", "no completed version has that ID")
    reference = None if version.skill_resources is None else next(
        (item for item in version.skill_resources.entries if item.name == name), None)
    if reference is None:
        raise Problem(404, "TEAM_SKILL_RESOURCE_NOT_FOUND", "the skill version has no resource with that name")
    resource = service._resource_version(
        principal, service.approval.team, version.source_work, reference, native_source=version.native_source)
    verified, raw = service.download(
        principal, service.approval.team, reference.artifact_id, reference.version_id, resource.blob.etag)
    if verified != resource:
        raise Problem(409, "M3_SKILL_RESOURCE_CHANGED", "resource version changed during the read")
    try:
        text = skill_resource_text(raw, reference.sha256)
    except (UnicodeError, ValueError):
        raise Problem(422, "M3_SKILL_RESOURCE_FORMAT", "resource must be complete valid UTF-8 data") from None
    if service._resource_version(
        principal, service.approval.team, version.source_work, reference, native_source=version.native_source,
    ) != resource:
        raise Problem(409, "M3_SKILL_RESOURCE_CHANGED", "resource version changed during the read")
    return {
        "schema_version": "fa-team-skill-resource-v1",
        "artifact_id": artifact,
        "version_id": version_id,
        "name": reference.name,
        "media_type": resource.media_type,
        "sha256": reference.sha256,
        "text": text,
    }


def _draft_unknown():
    return Problem(503, "SKILL_DRAFT_OUTCOME_UNKNOWN",
                   "read the review list; the same content sent again finds the same draft and is never saved twice")


def _draft_target(service, human, source, identifier, title):
    try:
        record = service.get(human, source.team_id, identifier)
    except Problem as error:
        if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
            return None, None
        raise
    document = ArtifactDocument.model_validate(record.payload)
    if (document.kind != "skill" or document.title != title or document.native_source != source
            or document.owner != OWNER_AUTHOR or document.owner_oid != human.oid or len(document.versions) > 1):
        raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the derived owner draft slot holds other content")
    return record, document


def _tolerated(error) -> bool:
    """A lost or conflicting write is read back instead of failing; a definite refusal stops here."""
    return not (isinstance(error, Problem) and error.status < 500 and error.status not in {409, 412})


def _finish_pending(service, human, team, identifier, version_id, etag, data, sha256):
    try:
        service.reconcile(human, team, identifier, version_id, etag)
    except Problem as error:
        if error.status != 404 or error.body["error"]["code"] != "ARTIFACT_BLOB_NOT_FOUND":
            raise
        # The reservation exists but its bytes never landed. The version ID and SHA-256 come from this same content and
        # the store only creates, so writing these bytes once can finish only this exact pending version.
        try:
            service.store.put(team, identifier, version_id, data, sha256)
        except Problem as exists:
            if exists.status != 409:
                raise
        service.reconcile(human, team, identifier, version_id, etag)


def _finish_owner_resources(service, human, source, arguments):
    """Finish an owner resource whose reservation is recorded but whose exact bytes never landed."""
    for resource in arguments.resources:
        ref, raw = resource_reference(source, resource), resource.text.encode("utf-8")
        try:
            record = service.get(human, source.team_id, ref.artifact_id)
        except Problem as error:
            if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
                continue
            raise
        document = ArtifactDocument.model_validate(record.payload)
        pending = document.pending
        if (document.versions or pending is None or document.native_source != source
                or (pending.version_id, pending.sha256, pending.bytes, pending.filename, pending.media_type) != (
                    ref.version_id, ref.sha256, len(raw), resource.name, resource.media_type)):
            continue
        _finish_pending(service, human, source.team_id, ref.artifact_id, ref.version_id, record.etag, raw, ref.sha256)


def _save_owner_draft(service, human, draft: OwnerSkillDraftInput) -> SkillReviewEntry:
    team = service.approval.team
    source = owner_source(service.context.auth.tenant_id, team, human.oid, draft)
    identifier, version_id = owner_artifact_id(source), owner_version_id(source)
    data = skill_draft_bytes(draft.arguments.body)
    metadata = VersionInput(filename="SKILL.md", media_type="text/markdown", sha256=hashlib.sha256(data).hexdigest(),
                            skill_purpose=skill_purpose(draft.arguments.purpose),
                            skill_resources=resource_manifest(source, draft.arguments))
    record, document = _draft_target(service, human, source, identifier, draft.title)
    if record is None:
        try:
            service.create_native_skill(human, team, identifier, title=draft.title, native_source=source)
        except (Problem, AzureError, OSError) as error:
            if not _tolerated(error):
                raise
            _log.warning("TEAM_OWNER_SKILL_DRAFT_UNCONFIRMED stage=create exception_type=%s", type(error).__name__)
        record, document = _draft_target(service, human, source, identifier, draft.title)
        if record is None:
            raise _draft_unknown()
    if not document.versions:
        pending = document.pending
        if pending is not None and (
                pending.native_source != source or pending.version_id != version_id or pending.sha256 != metadata.sha256
                or pending.bytes != len(data) or pending.skill_purpose != metadata.skill_purpose
                or pending.skill_resources != metadata.skill_resources):
            raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the pending owner draft has different content")
        try:
            if pending is not None:
                _finish_pending(service, human, team, identifier, version_id, record.etag, data, metadata.sha256)
            else:
                _finish_owner_resources(service, human, source, draft.arguments)
                NativeSkillDraftGateway._resources(service, human, source, draft.arguments, save=True)
                service.upload(human, team, identifier, version_id, record.etag, metadata, data, native_source=source)
        except (Problem, AzureError, OSError) as error:
            if not _tolerated(error):
                raise
            _log.warning("TEAM_OWNER_SKILL_DRAFT_UNCONFIRMED stage=content exception_type=%s", type(error).__name__)
        _, document = _draft_target(service, human, source, identifier, draft.title)
        if document is None or not document.versions:
            raise _draft_unknown()
    version = document.versions[0]
    if (version.native_source != source or version.version_id != version_id or version.sha256 != metadata.sha256
            or version.skill_purpose != metadata.skill_purpose or version.skill_resources != metadata.skill_resources):
        raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the derived owner draft holds other content")
    _, saved = service.download(human, team, identifier, version.version_id, version.blob.etag)
    if saved != data:
        raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the saved owner draft bytes differ")
    if version.skill_resources is not None:
        service._validate_resources(human, team, version, verify_content=True, native_source=source)
    return SkillReviewEntry.from_document(document)


def install_team_skills_routes(app, composition) -> None:
    router = APIRouter()
    identity = _principal_dependency(composition.verifier)

    @router.get(PREFIX + "/reviews")
    async def reviews(request: Request, principal=Depends(identity)):
        async def operation():
            service = _service(composition)
            human = _owner_principal(service, principal, "read")
            after = await _no_body_or_query(request, allow_after=True)
            value = await run_in_threadpool(service.skill_reviews, human, service.approval.team, after)
            return _response(value)
        return await _reply(operation)

    @router.get(PREFIX + "/skills/{artifact}")
    async def get_skill(artifact: str, request: Request, principal=Depends(identity)):
        async def operation():
            service = _service(composition)
            human = _owner_principal(service, principal, "read")
            await _no_body_or_query(request)
            record, _ = await run_in_threadpool(_skill_record, service, human, artifact)
            return _record_response(record)
        return await _reply(operation)

    @router.get(PREFIX + "/skills/{artifact}/versions/{version_id}")
    async def get_version(artifact: str, version_id: str, request: Request, principal=Depends(identity)):
        async def operation():
            service = _service(composition)
            human = _owner_principal(service, principal, "read")
            await _no_body_or_query(request)
            value = await run_in_threadpool(_version, service, human, artifact, version_id)
            return _response(value)
        return await _reply(operation)

    @router.get(PREFIX + "/skills/{artifact}/versions/{version_id}/content")
    async def content(artifact: str, version_id: str, request: Request, principal=Depends(identity)):
        async def operation():
            service = _service(composition)
            human = _owner_principal(service, principal, "read")
            await _no_body_or_query(request)
            version, data = await run_in_threadpool(_content, service, human, artifact, version_id)
            return Response(data, media_type="application/octet-stream", headers={
                "Cache-Control": "no-store", "X-Artifact-SHA256": version.sha256,
            })
        return await _reply(operation)

    @router.get(PREFIX + "/skills/{artifact}/versions/{version_id}/resources/{name}")
    async def resource(artifact: str, version_id: str, name: str, request: Request, principal=Depends(identity)):
        async def operation():
            service = _service(composition)
            human = _owner_principal(service, principal, "read")
            await _no_body_or_query(request)
            value = await run_in_threadpool(_resource, service, human, artifact, version_id, name)
            return _response(value)
        return await _reply(operation)

    @router.post(PREFIX + "/drafts")
    async def draft(request: Request, principal=Depends(identity)):
        async def operation():
            service = _service(composition)
            human = _owner_principal(service, principal, "approve")
            body = await _body(request, OwnerSkillDraftInput, max_body_bytes=OWNER_DRAFT_REQUEST_BYTES)
            entry = await run_in_threadpool(_save_owner_draft, service, human, body)
            return _response({"schema_version": "fa-team-owner-skill-draft-result-v1",
                              "entry": entry.model_dump(mode="json")})
        return await _reply(operation)

    @router.post(PREFIX + "/skills/{artifact}/decisions")
    async def decide(artifact: str, request: Request, principal=Depends(identity)):
        async def operation():
            service = _service(composition)
            human = _owner_principal(service, principal, "approve")
            body = await _body(request, ApprovalInput, max_body_bytes=DECISION_BODY_BYTES)
            etag = _exact_header(request, "if-match")

            def apply():
                _skill_record(service, human, artifact)
                return service.decide(human, service.approval.team, artifact, etag, body)

            return _record_response(await run_in_threadpool(apply))
        return await _reply(operation)

    @router.post(PREFIX + "/skills/{artifact}/deactivations")
    async def deactivate(artifact: str, request: Request, principal=Depends(identity)):
        async def operation():
            service = _service(composition)
            human = _owner_principal(service, principal, "approve")
            body = await _body(request, SkillDeactivationInput, max_body_bytes=DECISION_BODY_BYTES)
            etag = _exact_header(request, "if-match")

            def apply():
                _skill_record(service, human, artifact)
                return service.deactivate_skill(human, service.approval.team, artifact, etag, body)

            return _record_response(await run_in_threadpool(apply))
        return await _reply(operation)

    app.include_router(router)
