from __future__ import annotations

import re
from typing import Callable

from fastapi import Depends, FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .auth import Actor
from .backup_models import MAX_BACKUP_BYTES
from .backup_store import BackupStore
from .errors import Problem
from .service import ContextService

BACKUP_PREFIX = "/private-environments/backups"


def is_backup_upload(scope: dict) -> bool:
    return scope["method"] == "PUT" and re.fullmatch(
        BACKUP_PREFIX + r"/[^/]+", scope["path"],
    ) is not None


async def read_backup_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        if not re.fullmatch(r"[0-9]{1,10}", declared):
            raise Problem(422, "INVALID_REQUEST", "invalid Content-Length")
        if int(declared) > MAX_BACKUP_BYTES:
            raise Problem(413, "BACKUP_TOO_LARGE", "backup exceeds 8 MiB")
    parts = []
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BACKUP_BYTES:
                raise Problem(413, "BACKUP_TOO_LARGE", "backup exceeds 8 MiB")
            parts.append(chunk)
    except ClientDisconnect as exc:
        raise Problem(400, "BACKUP_INCOMPLETE", "backup upload was disconnected") from exc
    if declared is not None and total != int(declared):
        raise Problem(400, "BACKUP_INCOMPLETE", "body length differs from Content-Length")
    return b"".join(parts)


def add_backup_routes(app: FastAPI, service: ContextService, verifier: Callable, store: BackupStore) -> None:
    identity = Depends(verifier)

    @app.put(BACKUP_PREFIX + "/{backup_id}", status_code=201)
    async def upload(backup_id: str, request: Request, actor: Actor = identity):
        service.authorize(actor, actor.team)
        if request.headers.get("content-type", "").lower() != "application/octet-stream":
            raise Problem(415, "BACKUP_CONTENT_TYPE", "Content-Type must be application/octet-stream")
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise Problem(415, "BACKUP_CONTENT_ENCODING", "encoded request bodies are not supported")
        if request.query_params:
            raise Problem(422, "INVALID_REQUEST", "upload does not accept query parameters")
        hashes = request.headers.getlist("x-backup-sha256")
        if len(hashes) != 1 or not re.fullmatch(r"[a-f0-9]{64}", hashes[0]):
            raise Problem(422, "INVALID_BACKUP", "one lowercase X-Backup-SHA256 header is required")
        if not re.fullmatch(r"[a-f0-9]{32}", backup_id):
            raise Problem(422, "INVALID_BACKUP", "backup ID must be 32 lowercase hex characters")
        data = await read_backup_body(request)
        receipt = await run_in_threadpool(store.create, actor, backup_id, data, hashes[0])
        return receipt.model_dump()

    @app.get(BACKUP_PREFIX + "/{backup_id}/receipt")
    def get_receipt(backup_id: str, request: Request, actor: Actor = identity):
        service.authorize(actor, actor.team)
        if set(request.query_params) - {"version_id"} or len(request.query_params.getlist("version_id")) > 1:
            raise Problem(422, "INVALID_REQUEST", "only one version_id query parameter is allowed")
        return store.receipt(actor, backup_id, request.query_params.get("version_id")).model_dump()

    @app.get(BACKUP_PREFIX + "/{backup_id}")
    def download(backup_id: str, request: Request, actor: Actor = identity):
        service.authorize(actor, actor.team)
        versions = request.query_params.getlist("version_id")
        if set(request.query_params) != {"version_id"} or len(versions) != 1:
            raise Problem(422, "BACKUP_VERSION_REQUIRED", "exactly one version_id is required")
        etags = request.headers.getlist("if-match")
        if len(etags) > 1:
            raise Problem(412, "ETAG_CONFLICT", "one exact If-Match header is required")
        result = store.download(actor, backup_id, versions[0], etags[0] if etags else None)
        return Response(result.data, media_type="application/octet-stream", headers={
            "X-Backup-SHA256": result.receipt.sha256,
            "X-Backup-Version-Id": result.receipt.version_id,
            "ETag": result.receipt.etag,
        })
