from __future__ import annotations

import json
import re
from typing import Callable, TypeVar

import anyio
from fastapi import Depends, FastAPI, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from shared_context.auth import Actor
from shared_context.errors import Problem
from .m4_context import M4ContextService, SeedFixture
from shared_context.m4_fixtures import encoded
from shared_context.models import SearchMemory, StrictModel

MAX_M4_BODY_BYTES = 16384
_Body = TypeVar("_Body", bound=StrictModel)


def is_m4_upload(scope: dict) -> bool:
    return scope["method"] == "POST" and re.fullmatch(
        r"/m4/teams/[^/]+/memory/(?:seed|private/[^/]+/search)", scope["path"],
    ) is not None


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value: str):
    raise ValueError("nonfinite JSON value")


def _no_query(request: Request) -> None:
    if request.query_params:
        raise Problem(422, "INVALID_REQUEST", "M4 routes do not accept query parameters")


async def read_m4_body(request: Request, model: type[_Body]) -> _Body:
    _no_query(request)
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise Problem(415, "M4_CONTENT_TYPE", "Content-Type must be application/json")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise Problem(415, "M4_CONTENT_ENCODING", "encoded requests are not supported")
    declared = request.headers.get("content-length")
    if declared is not None:
        if re.fullmatch(r"[0-9]{1,10}", declared) is None:
            raise Problem(422, "INVALID_REQUEST", "invalid Content-Length")
        if int(declared) > MAX_M4_BODY_BYTES:
            raise Problem(413, "REQUEST_TOO_LARGE", "M4 request exceeds 16 KiB")
    parts, total = [], 0
    try:
        with anyio.fail_after(30):
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_M4_BODY_BYTES:
                    raise Problem(413, "REQUEST_TOO_LARGE", "M4 request exceeds 16 KiB")
                parts.append(chunk)
    except TimeoutError as exc:
        raise Problem(408, "REQUEST_TIMEOUT", "M4 request body timed out") from exc
    except ClientDisconnect as exc:
        raise Problem(400, "REQUEST_INCOMPLETE", "M4 request body was disconnected") from exc
    if declared is not None and int(declared) != total:
        raise Problem(400, "REQUEST_INCOMPLETE", "body length differs from Content-Length")
    try:
        return model.model_validate(json.loads(
            b"".join(parts), object_pairs_hook=_unique, parse_constant=_constant,
        ))
    except (ValueError, ValidationError, RecursionError) as exc:
        raise Problem(422, "INVALID_REQUEST", "invalid M4 JSON fields, types or fixture SHA-256") from exc


def add_m4_routes(app: FastAPI, service: M4ContextService, verifier: Callable) -> None:
    identity = Depends(verifier)

    @app.post("/m4/teams/{team}/memory/seed", status_code=202)
    async def seed(team: str, request: Request, actor: Actor = identity):
        service.authorize(actor, team)
        return await run_in_threadpool(service.seed, actor, team, await read_m4_body(request, SeedFixture))

    @app.get("/m4/teams/{team}/memory/operations/{operation}")
    def poll(team: str, operation: str, request: Request, actor: Actor = identity):
        service.authorize(actor, team)
        _no_query(request)
        return service.poll_memory(actor, team, operation)

    @app.post("/m4/teams/{team}/memory/private/{target}/search")
    async def search(team: str, target: str, request: Request, actor: Actor = identity):
        service.scope(actor, team, target)
        body = await read_m4_body(request, SearchMemory)
        return await run_in_threadpool(service.search, actor, team, target, body.query)

    @app.get("/m4/teams/{team}/works/{work}/context")
    def context(team: str, work: str, request: Request, actor: Actor = identity):
        service.authorize(actor, team)
        _no_query(request)
        return Response(encoded(service.assemble(actor, team, work)), media_type="application/json")
