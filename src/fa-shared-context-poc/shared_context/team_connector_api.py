"""Team owner connector routes; installed only with ``FA_TEAM_CONNECTORS_ENABLED=1``."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request

from .behavior_api import _principal_dependency
from .errors import Problem
from .team_connector import TeamConnectorService
from .team_connector_wire import TeamConnectorChange, team_connectors_enabled
from .team_mcp import authorize_team_owner, default_foundry_factory
from .team_mcp_api import (
    TEAM_MCP_BODY_BYTES, TEAM_MCP_ERRORS, _json_body, _no_body_or_query, _problem, _reply, project_scope_setting,
)

PREFIX = "/team-connectors/v1"
TEAM_CONNECTOR_ERRORS = TEAM_MCP_ERRORS | {"TEAM_CONNECTOR_SECRET_REQUIRED", "TEAM_CONNECTOR_TOOLBOX_TAKEN",
                                           "TEAM_CONNECTOR_PRIVATE_NETWORK"}


def install_team_connector_routes(app, composition, *, environ=None, foundry_factory=None) -> None:
    environ = os.environ if environ is None else environ
    if not team_connectors_enabled(environ):
        return
    scope = project_scope_setting(composition, environ)
    service = TeamConnectorService(composition.verifier, foundry_factory=foundry_factory or default_foundry_factory(
        composition, scope), project_scope=scope, app_settings=environ)
    app.state.team_connectors = service
    # Turn captures read the enabled connectors through the same service (team_connector_narrowing).
    composition.runtime.team_connectors = service
    router = APIRouter()
    identity = _principal_dependency(composition.verifier)

    @router.get(PREFIX + "/connectors")
    async def connectors(request: Request, principal=Depends(identity)):
        try:
            authorize_team_owner(composition.verifier, principal)
            await _no_body_or_query(request)
        except Problem as error:
            if error.body["error"]["code"] in TEAM_CONNECTOR_ERRORS:
                return _problem(error)
            raise
        return await _reply(request, lambda: service.view(principal), TEAM_CONNECTOR_ERRORS)

    @router.post(PREFIX + "/changes")
    async def changes(request: Request, principal=Depends(identity)):
        try:
            authorize_team_owner(composition.verifier, principal, write=True)
            command = await _json_body(request, TeamConnectorChange, max_body_bytes=TEAM_MCP_BODY_BYTES)
        except Problem as error:
            if error.body["error"]["code"] in TEAM_CONNECTOR_ERRORS:
                return _problem(error)
            raise
        return await _reply(request, lambda: service.submit(principal, command), TEAM_CONNECTOR_ERRORS)

    app.include_router(router)
