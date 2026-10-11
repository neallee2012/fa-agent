"""Team owner MCP management routes."""

from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .behavior_api import _nonfinite, _principal_dependency, _response, _unique
from .errors import Problem
from .feed_signature import SIGNING_KEY_ENV, load_signing_key
from .team_mcp import PROJECT_SCOPE_ENV, TeamMcpService, default_foundry_factory
from .team_mcp_wire import TeamMcpChange, team_mcp_settings

PREFIX = "/team-mcp/v1"
TEAM_MCP_BODY_BYTES = 16 * 1024
TEAM_MCP_HTTP_BYTES = 256 * 1024
TEAM_MCP_ERRORS = frozenset({
    "TEAM_MCP_OWNER_ONLY", "TEAM_MCP_UNAVAILABLE", "TEAM_MCP_CHANGED", "TEAM_MCP_INVALID", "TEAM_MCP_LIMIT",
    "TEAM_MCP_EXISTS", "TEAM_MCP_MISSING", "TEAM_MCP_LAST_SERVER", "TEAM_MCP_CONNECTION_DENIED",
    "TEAM_MCP_CONNECTION_CREATE_UNAVAILABLE", "TEAM_MCP_COMMAND_CONFLICT", "TEAM_MCP_FORBIDDEN",
    "TEAM_MCP_REFUSED", "TEAM_MCP_OUTCOME_UNKNOWN", "TEAM_MCP_PLAINTEXT_SECRET", "TEAM_MCP_FEED_URL_INVALID",
    "TEAM_MCP_FEED_UNAVAILABLE",
})
_log = logging.getLogger(__name__)


def _problem(error: Problem) -> JSONResponse:
    return JSONResponse(error.body, status_code=error.status, headers={"Cache-Control": "no-store"})


async def _reply(request: Request, call, errors=TEAM_MCP_ERRORS):
    try:
        value = await run_in_threadpool(call)
    except Problem as error:
        code = error.body["error"]["code"]
        if code not in errors:
            raise
        return _problem(error)
    response = _response(value, max_bytes=TEAM_MCP_HTTP_BYTES)
    if getattr(value, "etag", None):
        response.headers["ETag"] = value.etag
    return response


async def _no_body_or_query(request: Request):
    if request.query_params:
        raise Problem(422, "TEAM_MCP_INVALID", "Team MCP reads take no query parameters")
    try:
        async for chunk in request.stream():
            if chunk:
                raise Problem(422, "TEAM_MCP_INVALID", "Team MCP reads take no body")
    except ClientDisconnect as exc:
        raise Problem(400, "TEAM_MCP_INVALID", "request body was disconnected") from exc


async def _json_body(request: Request, model, *, max_body_bytes: int):
    if request.query_params:
        raise Problem(422, "TEAM_MCP_INVALID", "Team MCP writes take no query parameters")
    for name in ("content-length", "content-type", "content-encoding"):
        if len(request.headers.getlist(name)) > 1:
            raise Problem(422, "TEAM_MCP_INVALID", "duplicate request headers")
    if request.headers.get("content-type") not in {"application/json", "application/json; charset=utf-8"}:
        raise Problem(415, "TEAM_MCP_INVALID", "JSON content type is required")
    if request.headers.get("content-encoding", "identity") != "identity":
        raise Problem(415, "TEAM_MCP_INVALID", "encoded bodies are unsupported")
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isdecimal() or int(length) > max_body_bytes:
            raise Problem(422, "TEAM_MCP_INVALID", "request exceeds the Team MCP body limit")
    total, chunks = 0, []
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_body_bytes:
                raise Problem(422, "TEAM_MCP_INVALID", "request exceeds the Team MCP body limit")
            chunks.append(chunk)
    except ClientDisconnect as exc:
        raise Problem(400, "TEAM_MCP_INVALID", "request body was disconnected") from exc
    raw = b"".join(chunks)
    if length is not None and total != int(length):
        raise Problem(400, "TEAM_MCP_INVALID", "request length differs")
    try:
        json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite)
        return model.model_validate_json(raw)
    except (ValueError, TypeError, UnicodeError, ValidationError) as error:
        # Fixed messages only: a validation error can quote the input, which may hold a connection key.
        if "TEAM_MCP_PLAINTEXT_SECRET" in str(error):
            raise Problem(422, "TEAM_MCP_PLAINTEXT_SECRET", "server_url must not contain credential-looking material") from None
        if "TEAM_MCP_FEED_URL_INVALID" in str(error):
            raise Problem(422, "TEAM_MCP_FEED_URL_INVALID", "x-feed-url needs one https feed URL") from None
        raise Problem(422, "TEAM_MCP_INVALID", "invalid Team MCP change body") from None


async def _change_body(request: Request) -> TeamMcpChange:
    return await _json_body(request, TeamMcpChange, max_body_bytes=TEAM_MCP_BODY_BYTES)


def project_scope_setting(composition, environ) -> str | None:
    """The project's ARM ID for the Team's own connections; only the project this API already serves."""
    from .behavior_agent_setup import _ENDPOINT, _SCOPE

    scope = environ.get(PROJECT_SCOPE_ENV) or None
    if scope is None:
        return None
    try:
        endpoint = composition.config.targets[0].project_endpoint
    except (AttributeError, IndexError, TypeError):
        endpoint = None
    matched, project = _SCOPE.fullmatch(scope), _ENDPOINT.fullmatch(endpoint or "")
    if (matched is None or project is None or matched.group(2).lower() != project.group(1)
            or matched.group(3).lower() != project.group(2).lower()):
        _log.warning("TEAM_MCP_CONNECTIONS_INACTIVE reason=config_invalid")
        return None
    return scope


def feed_signing_key_setting(environ):
    """The FA feed reader's signing key (a Container Apps secret), or None: then x-feed-url connections are refused."""
    value = environ.get(SIGNING_KEY_ENV) or None
    if value is None:
        return None
    try:
        return load_signing_key(value)
    except ValueError:
        _log.warning("TEAM_MCP_FEED_SIGNING_INACTIVE reason=config_invalid")
        return None


def install_team_mcp_routes(app, composition, *, environ=None, foundry_factory=None) -> None:
    environ = os.environ if environ is None else environ
    enabled, single_team = team_mcp_settings(environ)
    if not enabled:
        return
    scope = project_scope_setting(composition, environ)
    factory = foundry_factory or default_foundry_factory(composition, scope)
    service = TeamMcpService(composition.verifier, foundry_factory=factory, single_team=single_team,
                             project_scope=scope, feed_signing_key=feed_signing_key_setting(environ))
    app.state.team_mcp = service
    # Turn captures read the Team's current toolbox servers through the same service (runtime_grants).
    composition.runtime.team_mcp = service
    router = APIRouter()
    identity = _principal_dependency(composition.verifier)

    @router.get(PREFIX + "/servers")
    async def servers(request: Request, principal=Depends(identity)):
        try:
            service._authorize(principal)
            await _no_body_or_query(request)
        except Problem as error:
            if error.body["error"]["code"] in TEAM_MCP_ERRORS:
                return _problem(error)
            raise
        return await _reply(request, lambda: service.view(principal))

    @router.get(PREFIX + "/connections")
    async def connections(request: Request, principal=Depends(identity)):
        try:
            service._authorize(principal)
            await _no_body_or_query(request)
        except Problem as error:
            if error.body["error"]["code"] in TEAM_MCP_ERRORS:
                return _problem(error)
            raise
        return await _reply(request, lambda: service.connections(principal))

    @router.post(PREFIX + "/changes")
    async def changes(request: Request, principal=Depends(identity)):
        try:
            service._authorize(principal, write=True)
            command = await _change_body(request)
        except Problem as error:
            if error.body["error"]["code"] in TEAM_MCP_ERRORS:
                return _problem(error)
            raise
        return await _reply(request, lambda: service.submit(principal, command))

    app.include_router(router)
