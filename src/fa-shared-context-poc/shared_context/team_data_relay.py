"""Team data relay for Hosted agents (ADR-0006 option C); no new IAM or network.

The verified agent token selects the team; callers never name a team, partition,
container path or artifact namespace. Records use the existing team-partitioned
TableRepository (create-only, or replace with the original ETag); objects use the
existing create-only BlobArtifactStore and its receipts. There is no delete, list
or published-content write. Existing rows and blobs are not moved or rewritten.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import JsonValue, TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .behavior_api import _body, _principal_dependency, _response
from .behavior_auth import BehaviorPrincipal
from .errors import Problem
from .m3_models import BlobReceipt, Digest, MAX_ARTIFACT_BYTES, VersionId
from .m3_store import ArtifactStore
from .models import Identifier, StrictModel
from .storage import MAX_PAYLOAD_BYTES, Repository, serialize

PREFIX = "/team-data/v1"
SCHEMA = "fa-team-data-record-v1"
# kind -> whether an existing record may be replaced with its ETag.
RECORD_KINDS = {"turn-complete": False, "checkpoint": True, "result": True, "memory": True}
OBJECT_KINDS = frozenset({"checkpoint", "result", "memory"})
MAX_RECORD_BODY_BYTES = MAX_PAYLOAD_BYTES
_identifier = TypeAdapter(Identifier)
_version = TypeAdapter(VersionId)
_digest = TypeAdapter(Digest)


class RecordWrite(StrictModel):
    data: dict[str, JsonValue]


class ObjectRead(StrictModel):
    receipt: BlobReceipt


def _invalid(message):
    return Problem(422, "TEAM_DATA_INVALID_REQUEST", message)


class TeamDataRelay:
    def __init__(self, records: Repository, objects: ArtifactStore, tenant_id: str):
        self.records, self.objects, self.tenant_id = records, objects, str(tenant_id)

    @staticmethod
    def team(principal: BehaviorPrincipal) -> str:
        if not isinstance(principal, BehaviorPrincipal) or principal.kind != "agent" or not principal.actor_id:
            raise Problem(403, "TEAM_DATA_AGENT_ONLY", "only a verified team agent may use team data")
        return principal.team

    def _partition(self, team):
        return f"team-data-v1-{self.tenant_id}-{team}"

    @staticmethod
    def _record_key(kind, key):
        if kind not in RECORD_KINDS:
            raise Problem(404, "TEAM_DATA_KIND_UNKNOWN", "unsupported team record kind")
        try:
            _identifier.validate_python(key, strict=True)
        except ValidationError:
            raise _invalid("invalid record key") from None
        return f"{kind}:{key}"

    @staticmethod
    def _artifact(kind, object_id, version_id):
        if kind not in OBJECT_KINDS:
            raise Problem(404, "TEAM_DATA_KIND_UNKNOWN", "unsupported team object kind")
        artifact = f"td-{kind}-{object_id}"
        try:
            _identifier.validate_python(object_id, strict=True)
            _identifier.validate_python(artifact, strict=True)
            _version.validate_python(version_id, strict=True)
        except ValidationError:
            raise _invalid("invalid object ID or 32-hex version ID") from None
        return artifact

    def read_record(self, principal, kind, key):
        team = self.team(principal)
        row = self._record_key(kind, key)
        record = self.records.get(self._partition(team), row)
        stored = record.payload
        if (not isinstance(stored, dict) or set(stored) != {"schema", "kind", "key", "writer", "data"}
                or (stored["schema"], stored["kind"], stored["key"]) != (SCHEMA, kind, key)):
            raise Problem(502, "TEAM_DATA_STATE_INVALID", "stored team record does not match its key")
        return {"kind": kind, "key": key, "writer": stored["writer"], "data": stored["data"], "etag": record.etag}

    def write_record(self, principal, kind, key, data, *, etag=None):
        team = self.team(principal)
        row = self._record_key(kind, key)
        payload = {"schema": SCHEMA, "kind": kind, "key": key, "writer": principal.actor_id, "data": data}
        try:
            serialize(payload)
        except Problem:
            raise Problem(413, "TEAM_DATA_BODY_LIMIT", "the stored record would exceed 48 KiB") from None
        if etag is None:
            return self.records.create(self._partition(team), row, payload)
        if not RECORD_KINDS[kind]:
            raise Problem(409, "TEAM_DATA_IMMUTABLE", "this record kind can only be created once")
        return self.records.replace(self._partition(team), row, payload, etag)

    def put_object(self, principal, kind, object_id, version_id, data, sha256):
        team = self.team(principal)
        return self.objects.put(team, self._artifact(kind, object_id, version_id), version_id, data, sha256)

    def object_receipt(self, principal, kind, object_id, version_id):
        team = self.team(principal)
        return self.objects.stat(team, self._artifact(kind, object_id, version_id), version_id)

    def read_object(self, principal, kind, object_id, version_id, receipt):
        team = self.team(principal)
        return self.objects.read(team, self._artifact(kind, object_id, version_id), version_id, receipt)


def _single(request, name):
    values = request.headers.getlist(name)
    if len(values) > 1:
        raise _invalid("duplicate request headers")
    return values[0] if values else None


def _no_body(request):
    if request.query_params:
        raise _invalid("query parameters are unsupported")
    if request.headers.get("content-length", "0") != "0" or "transfer-encoding" in request.headers:
        raise _invalid("this route takes no body")


async def _octets(request):
    if request.query_params:
        raise _invalid("query parameters are unsupported")
    for name in ("content-length", "content-type", "content-encoding"):
        _single(request, name)
    if request.headers.get("content-type") != "application/octet-stream":
        raise Problem(415, "TEAM_DATA_INVALID_REQUEST", "application/octet-stream is required")
    if request.headers.get("content-encoding", "identity") != "identity":
        raise Problem(415, "TEAM_DATA_INVALID_REQUEST", "encoded bodies are unsupported")
    length = request.headers.get("content-length")
    if length is not None and (not re.fullmatch(r"[0-9]{1,8}", length) or int(length) > MAX_ARTIFACT_BYTES):
        raise Problem(413, "TEAM_DATA_BODY_LIMIT", "object exceeds 512 KiB")
    chunks, total = [], 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_ARTIFACT_BYTES:
                raise Problem(413, "TEAM_DATA_BODY_LIMIT", "object exceeds 512 KiB")
            chunks.append(chunk)
    except ClientDisconnect:
        raise _invalid("incomplete request") from None
    if length is not None and total != int(length):
        raise _invalid("request length differs")
    sha256 = _single(request, "x-fa-content-sha256")
    try:
        _digest.validate_python(sha256, strict=True)
    except ValidationError:
        raise _invalid("one lowercase x-fa-content-sha256 header is required") from None
    return b"".join(chunks), sha256


def install_team_data_routes(app, relay: TeamDataRelay, verifier):
    router = APIRouter()
    identity = _principal_dependency(verifier)
    record = PREFIX + "/records/{kind}/{key}"
    version = PREFIX + "/objects/{kind}/{object_id}/versions/{version_id}"

    @router.get(record)
    async def read_record(request: Request, kind: str, key: str,
                          principal: BehaviorPrincipal = Depends(identity)):
        relay.team(principal)
        _no_body(request)
        return _response(await run_in_threadpool(relay.read_record, principal, kind, key))

    @router.put(record)
    async def write_record(request: Request, kind: str, key: str,
                           principal: BehaviorPrincipal = Depends(identity)):
        relay.team(principal)
        create, match = _single(request, "if-none-match"), _single(request, "if-match")
        if (create is None) == (match is None) or create not in (None, "*") or match is not None and (
                not match.strip() or match == "*"):
            raise Problem(422, "TEAM_DATA_PRECONDITION_REQUIRED",
                          "send exactly If-None-Match: * to create or If-Match: <etag> to replace")
        command = await _body(request, RecordWrite, max_body_bytes=MAX_RECORD_BODY_BYTES)
        saved = await run_in_threadpool(relay.write_record, principal, kind, key, command.data, etag=match)
        response = _response({"etag": saved.etag})
        response.status_code = 201 if create else 200
        return response

    @router.put(version)
    async def put_object(request: Request, kind: str, object_id: str, version_id: str,
                         principal: BehaviorPrincipal = Depends(identity)):
        relay.team(principal)
        data, sha256 = await _octets(request)
        receipt = await run_in_threadpool(relay.put_object, principal, kind, object_id, version_id, data, sha256)
        response = _response(receipt)
        response.status_code = 201
        return response

    @router.get(version + "/receipt")
    async def object_receipt(request: Request, kind: str, object_id: str, version_id: str,
                             principal: BehaviorPrincipal = Depends(identity)):
        relay.team(principal)
        _no_body(request)
        return _response(await run_in_threadpool(relay.object_receipt, principal, kind, object_id, version_id))

    @router.post(version + "/read")
    async def read_object(request: Request, kind: str, object_id: str, version_id: str,
                          principal: BehaviorPrincipal = Depends(identity)):
        relay.team(principal)
        command = await _body(request, ObjectRead)
        data = await run_in_threadpool(relay.read_object, principal, kind, object_id, version_id, command.receipt)
        return Response(data, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})

    app.include_router(router)
