"""Team connector management on the official Foundry toolbox and ARM connection APIs.

Same contract as Team MCP (team_mcp.py): only the Team's original owner writes, every write stays in the Team's own
toolbox and connection, one request ID maps to one toolbox version through the version ``metadata``, and nothing is
deleted. The connection name is fixed per project, Team and connector, because Foundry keeps each caller's OAuth
consent per connection name (https://learn.microsoft.com/azure/foundry/agents/how-to/mcp-authentication).
"""

from __future__ import annotations

import logging
import re
import threading
from time import monotonic

from .behavior_auth import BehaviorJwtVerifier
from .connector_registry import CONNECTORS, ConnectorRef
from .errors import Problem
from .source_materials import digest
from .team_connector_wire import (
    TeamConnectorChange, TeamConnectorChangeResult, TeamConnectorState, TeamConnectorsView, connector_entry_id,
    connector_metadata, connector_servers, connector_toolbox_name, connector_tools,
)
from .team_mcp import (
    OAUTH2_CONNECTION_API_VERSION, _server, _status, _team_lock, _write_failure, authorize_team_owner,
    promote_request_version, read_failure, team_connection_prefix,
)
from .team_mcp_wire import toolbox_grant

_log = logging.getLogger(__name__)
GRANTS_SECONDS = 30.0
_CLIENT_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_AUTHORITY = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/"


def connector_connection_name(project_scope: str, team_id: str, connector_id: str) -> str:
    """The Team's own prefix plus the connector ID: re-enabling or a new secret never renames it."""
    return team_connection_prefix(project_scope, team_id) + connector_id


class TeamConnectorService:
    def __init__(self, verifier: BehaviorJwtVerifier, *, foundry_factory, project_scope: str | None, app_settings):
        self.verifier, self.foundry_factory = verifier, foundry_factory
        self.project_scope, self.app_settings = project_scope, app_settings
        self._grants: dict[tuple[str, str], tuple[float, tuple]] = {}
        self._grants_lock = threading.Lock()

    def _names(self, team: str, ref: ConnectorRef) -> tuple[str, str]:
        if self.project_scope is None:
            raise Problem(503, "TEAM_MCP_UNAVAILABLE", "this environment has no project scope for Team connectors")
        try:
            toolbox = connector_toolbox_name(team, ref.id)
        except ValueError:
            raise Problem(503, "TEAM_MCP_UNAVAILABLE", "the Team ID cannot name a connector toolbox") from None
        return toolbox, connector_connection_name(self.project_scope, team, ref.id)

    @staticmethod
    def _parse(team: str, ref: ConnectorRef, connection: str, default, tools: list[dict], metadata: dict):
        if default is None:
            return None
        servers = {server.server_label: server for server in (_server(tool, default) for tool in tools)
                   if server is not None}
        return connector_servers(team, ref, connection, metadata, servers, len(tools))

    def _state(self, foundry, team: str, ref: ConnectorRef) -> tuple[TeamConnectorState, tuple]:
        toolbox, connection = self._names(team, ref)
        default, tools, metadata = foundry.read_default_version(toolbox)
        parsed = self._parse(team, ref, connection, default, tools, metadata)
        state = TeamConnectorState(
            connector_id=ref.id, display_name=ref.display_name, disclosure=ref.disclosure, revoke_url=ref.revoke_url,
            toolbox=toolbox, connection=connection, default_version=default,
            settings=None if parsed is None else parsed[0])
        return state, () if parsed is None else parsed[1]

    def _read_state(self, foundry, team: str, ref: ConnectorRef) -> TeamConnectorState:
        try:
            return self._state(foundry, team, ref)[0]
        except Exception as error:
            raise read_failure(error) from None

    def view(self, principal) -> TeamConnectorsView:
        authorize_team_owner(self.verifier, principal)
        foundry = self.foundry_factory()
        return TeamConnectorsView(team_id=principal.team, connectors=tuple(
            self._read_state(foundry, principal.team, ref) for ref in CONNECTORS.values()))

    def _ensure_connection(self, foundry, principal, ref: ConnectorRef, name: str, command: TeamConnectorChange):
        try:
            existing = foundry.connection(name, api_version=OAUTH2_CONNECTION_API_VERSION)
        except Exception as error:
            raise read_failure(error) from None
        if existing is not None and (existing.get("fa_team"), existing.get("fa_connector")) != (principal.team, ref.id):
            raise Problem(409, "TEAM_MCP_COMMAND_CONFLICT", "another owner already holds this connection name")
        if command.client_secret is None:
            if existing is None:
                raise Problem(422, "TEAM_CONNECTOR_SECRET_REQUIRED", "the first enable needs the app's client secret")
            return
        if existing is not None and existing.get("fa_request_id") == command.request_id:
            return  # this request already wrote it; a lost reply is never a second write
        client_id = self.app_settings.get(ref.authentication.app_setting) or ""
        if _CLIENT_ID.fullmatch(client_id) is None:
            raise Problem(422, "TEAM_MCP_CONNECTION_CREATE_UNAVAILABLE",
                          "this environment has no Entra app for this connector")
        authority = _AUTHORITY.format(tenant=principal.tenant_id)
        try:
            foundry.put_oauth2_connection(
                name, target=ref.server_url, client_id=client_id,
                client_secret=command.client_secret.get_secret_value(), authorization_url=authority + "authorize",
                token_url=authority + "token", scopes=ref.authentication.scopes,
                metadata={"fa_team": principal.team, "fa_connector": ref.id, "fa_request_id": command.request_id})
        except Exception as error:
            _log.warning("TEAM_CONNECTOR_CONNECTION_WRITE_FAILED status=%s", _status(error))
            raise _write_failure(error, "connection") from None

    def _finish(self, foundry, principal, ref: ConnectorRef, command: TeamConnectorChange, version: str):
        toolbox, _ = self._names(principal.team, ref)
        promote_request_version(foundry, toolbox, version, command.expected_version)
        self.forget(principal.tenant_id, principal.team)
        return TeamConnectorChangeResult(request_id=command.request_id, state="applied", toolbox_version=version,
                                         connector=self._read_state(foundry, principal.team, ref))

    def submit(self, principal, command: TeamConnectorChange) -> TeamConnectorChangeResult:
        authorize_team_owner(self.verifier, principal, write=True)
        ref = command.ref
        toolbox, connection = self._names(principal.team, ref)
        material = digest([principal.tenant_id, principal.team, principal.oid, command.material()])
        foundry = self.foundry_factory()
        with _team_lock(principal.tenant_id, principal.team):
            try:
                found = foundry.request_version(toolbox, command.request_id)
                if found is None:
                    default, _, metadata = foundry.read_default_version(toolbox)
            except Exception as error:
                raise read_failure(error) from None
            if found is not None:
                if found["metadata"].get("fa_command") != material:
                    raise Problem(409, "TEAM_MCP_COMMAND_CONFLICT", "this request_id already names another change")
                return self._finish(foundry, principal, ref, command, found["version"])
            if default != command.expected_version:
                raise Problem(409, "TEAM_MCP_CHANGED", "read the current connector settings and change again")
            owner = (metadata.get("fa_team"), metadata.get("fa_connector"))
            if default is not None and owner != (principal.team, ref.id):
                # A default version FA did not write for this Team and connector; never write over its versions.
                raise Problem(409, "TEAM_CONNECTOR_TOOLBOX_TAKEN", "this connector toolbox holds another owner's tools")
            self._ensure_connection(foundry, principal, ref, connection, command)
            try:
                version = foundry.create_version(
                    toolbox, tools=connector_tools(ref, connection, command),
                    metadata=connector_metadata(principal.team, ref, connection, command, material))
            except Exception as error:
                raise _write_failure(error, "toolbox version") from None
            return self._finish(foundry, principal, ref, command, version)

    def forget(self, tenant: str, team: str) -> None:
        with self._grants_lock:
            self._grants.pop((tenant, team), None)

    def team_grants(self, tenant: str, team: str) -> tuple:
        """The Team's enabled connectors, as grants pinned to each connector toolbox's current default version."""
        if self.project_scope is None:
            return ()
        key, now = (tenant, team), monotonic()
        with self._grants_lock:
            cached = self._grants.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        foundry, grants = self.foundry_factory(), []
        try:
            for ref in CONNECTORS.values():
                state, servers = self._state(foundry, team, ref)
                if state.settings is not None and state.settings.enabled:
                    grants += [toolbox_grant(connector_entry_id(server.server_label), state.toolbox, server)
                               for server in servers]
        except Exception as error:
            _log.warning("TEAM_CONNECTOR_GRANTS_UNAVAILABLE status=%s cached=%s", _status(error), cached is not None)
            return () if cached is None else cached[1]
        value = tuple(grants)
        with self._grants_lock:
            self._grants[key] = (now + GRANTS_SECONDS, value)
        return value


def current_team_connector_grants(runtime) -> tuple:
    service = getattr(runtime, "team_connectors", None)
    raw = getattr(runtime, "bindings", None) or {}
    bindings = tuple(raw.values()) if hasattr(raw, "values") else tuple(raw)
    if service is None or not bindings:
        return ()
    return service.team_grants(bindings[0].tenant_id, bindings[0].team_id)
