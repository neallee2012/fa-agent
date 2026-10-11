"""Team owner MCP management on the official Foundry toolbox API.

The toolbox is project-wide and has no per-Team access (ADR-0007 §5), so the API does only three things: it checks
the original Team owner, keeps every write inside the Team's own toolbox ``fa-team-{team}``, and maps one request ID
to one toolbox version. Foundry's versions and ``default_version`` are the only record of which servers exist; no FA
head or intent row is written. Create version always makes a new version and has no idempotency key
(https://learn.microsoft.com/azure/foundry/agents/how-to/tools/toolbox#create-a-new-version), so the request ID rides
in the official version ``metadata`` and a resend reuses that version.

A key for a new MCP server goes once from the page to this API and once to Foundry, in the ARM PUT of the Team's own
project connection (the downstream credential lives on the connection, never in the agent or toolbox:
https://learn.microsoft.com/azure/foundry/agents/how-to/tools/use-toolbox-hosted-agent). The key is never logged,
stored by FA or returned. The connection name is derived from the project and Team, so only the Team's own
connections are ever written; nothing here deletes. A new connection whose header is ``x-feed-url`` also gets the
API's ``x-fa-feed-signature`` of that URL (feed_signature.py), so the FA feed reader fetches only URLs signed here.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import threading
from datetime import datetime
from time import monotonic
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import Field

from .behavior_auth import BehaviorJwtVerifier, BehaviorPrincipal
from .behavior_operations import _Model, _parse
from .behavior_wire import Command, WireModel
from .errors import Problem
from .feed_signature import FEED_SIGNATURE_HEADER, FEED_URL_HEADER, sign_feed_url
from .source_materials import digest
from .team_mcp_wire import (
    MAX_TEAM_MCP_SERVERS, ConnectionName, ServerLabel, TeamMcpChange, TeamMcpChangeResult, TeamMcpConnection,
    TeamMcpConnections, TeamMcpServer, TeamMcpServerInput, TeamMcpView, TeamToolName, ToolboxVersion,
    team_mcp_grant, team_toolbox_name,
)

_log = logging.getLogger(__name__)
_SAFE_CREDENTIAL_TYPES = {"none", "aad", "managedidentity", "agenticidentity", "agenticidentitytoken_preview",
                          "userentratoken", "projectmanagedidentity"}
_MCP_CONNECTION_TYPES = {"remotetool", "remotetool_preview", "customkeys"}
# The keys FA writes for one MCP server; a tool entry with anything else is kept as it is, never edited here.
_MANAGED_KEYS = {"type", "server_label", "server_url", "allowed_tools", "require_approval", "project_connection_id"}
_TEAM_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_TEAM_LOCKS_GUARD = threading.Lock()
# A lost-ACK resend happens right after the original; the newest versions are enough to find its version.
_REQUEST_LOOKUP_VERSIONS = 100
# Turn captures read the Team's servers often; Foundry is read again after this, or at once after a change here.
GRANTS_SECONDS = 30.0
ARM = "https://management.azure.com"
ARM_TOKEN_SCOPE = "https://management.azure.com/.default"
# The version the stage connection script already uses (deploy/stage/new_webiq_connection.py).
CONNECTION_API_VERSION = "2025-06-01"
# The version the P0b dev OAuth2 connection was written and read back with (the azd connections extension's version).
OAUTH2_CONNECTION_API_VERSION = "2025-04-01-preview"
PROJECT_SCOPE_ENV = "FA_TEAM_MCP_PROJECT_RESOURCE_ID"


def _team_lock(tenant: str, team: str) -> threading.Lock:
    with _TEAM_LOCKS_GUARD:
        return _TEAM_LOCKS.setdefault((tenant, team), threading.Lock())


def team_mcp_key(kind, tenant, team, *parts) -> str:
    raw = json.dumps(["fa-operation-control-v1", "team-mcp-" + kind, tenant, team, *parts],
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def team_connection_prefix(project_scope: str, team_id: str) -> str:
    """``t`` + 10 hex + ``-``: an official connection name is at most 33 characters and unique in the account, so the
    Team's part is a fixed-length token of this project and Team."""
    return "t" + hashlib.sha256(f"{project_scope.lower()}|{team_id}".encode("utf-8")).hexdigest()[:10] + "-"


def team_connection_name(project_scope: str, team_id: str, request_id: str) -> str:
    """One connection per change request: a new key never overwrites a connection a live version may still use."""
    return team_connection_prefix(project_scope, team_id) + hashlib.sha256(
        f"{project_scope.lower()}|{team_id}|{request_id}".encode("utf-8")).hexdigest()[:12]


class TeamMcpFoundryError(Exception):
    """status None means the outcome is unknown."""

    def __init__(self, status: int | None):
        super().__init__(status)
        self.status = status


def _field(value, *names, default=None):
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _plain(value) -> dict:
    if hasattr(value, "as_dict"):
        value = value.as_dict()
    return json.loads(json.dumps(value)) if isinstance(value, dict) else {}


def _status(error: Exception) -> int | None:
    if isinstance(error, TeamMcpFoundryError):
        return error.status
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None) or getattr(response, "status_code", None)
    return status if type(status) is int and 100 <= status <= 599 else None


def _secret_connection(connection: TeamMcpConnection) -> bool:
    return connection.credential_type.casefold() not in _SAFE_CREDENTIAL_TYPES


def _write_failure(error: Exception, what: str) -> Problem:
    """One Foundry write's failure: 401/403 forbidden, another 4xx a definite refusal, anything else unknown."""
    status = _status(error)
    if status in {401, 403}:
        return Problem(403, "TEAM_MCP_FORBIDDEN", f"Foundry refused the {what} write", upstream_status=status)
    if status is not None and 400 <= status < 500:
        return Problem(422, "TEAM_MCP_REFUSED", f"Foundry refused the {what}", upstream_status=status)
    return Problem(503, "TEAM_MCP_OUTCOME_UNKNOWN", "resend the same Team MCP command request_id",
                   upstream_status=status)


class TeamMcpFoundryClient:
    """Thin lazy wrapper over the pinned azure-ai-projects toolboxes API and the ARM connection PUT."""

    def __init__(self, endpoint: str, credential, *, project_scope: str | None = None, transport=None):
        self.endpoint, self.credential = endpoint, credential
        self.project_scope, self.transport = project_scope, transport

    def _call(self, operation):
        try:
            from azure.ai.projects import AIProjectClient
            from azure.core.exceptions import AzureError
            with AIProjectClient(endpoint=self.endpoint, credential=self.credential, allow_preview=True,
                                 retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
                                 connection_timeout=10, read_timeout=30) as project:
                return operation(project)
        except AzureError as error:
            from .behavior_foundry import _upstream_status
            raise TeamMcpFoundryError(_upstream_status(error)) from None
        except (TimeoutError, OSError):
            raise TeamMcpFoundryError(None) from None

    def list_connections(self) -> tuple[TeamMcpConnection, ...]:
        def op(project):
            return tuple(_connection(item) for item in project.connections.list())
        return self._call(op)

    def read_toolbox(self, name: str) -> tuple[str | None, list[dict]]:
        """The default version and its tools; (None, []) when the Team has no toolbox yet."""
        return self.read_default_version(name)[:2]

    def read_default_version(self, name: str) -> tuple[str | None, list[dict], dict[str, str]]:
        """The default version, its tools and its metadata; (None, [], {}) when the toolbox does not exist yet."""
        def op(project):
            try:
                default = _field(project.toolboxes.get(name), "default_version")
            except Exception as error:
                if _status(error) == 404:
                    return None, [], {}
                raise
            if not default:
                return None, [], {}
            version = project.toolboxes.get_version(name, default)
            return (str(default), [_plain(tool) for tool in (_field(version, "tools") or ())],
                    dict(_field(version, "metadata") or {}))
        return self._call(op)

    def default_version(self, name: str) -> str | None:
        def op(project):
            try:
                return _field(project.toolboxes.get(name), "default_version") or None
            except Exception as error:
                if _status(error) == 404:
                    return None
                raise
        return self._call(op)

    def request_version(self, name: str, request_id: str) -> dict | None:
        """The version an earlier send of this request created, among the newest versions."""
        def op(project):
            try:
                versions = itertools.islice(project.toolboxes.list_versions(name, limit=_REQUEST_LOOKUP_VERSIONS,
                                                                            order="desc"), _REQUEST_LOOKUP_VERSIONS)
                for item in versions:
                    metadata = _field(item, "metadata") or {}
                    if metadata.get("fa_request_id") == request_id:
                        return {"version": str(_field(item, "version")), "metadata": dict(metadata)}
            except Exception as error:
                if _status(error) != 404:
                    raise
            return None
        return self._call(op)

    def create_version(self, name: str, *, tools: list[dict], metadata: dict[str, str]) -> str:
        def op(project):
            created = project.toolboxes.create_version(
                name, body={"description": "FA Team MCP", "metadata": metadata, "tools": tools})
            version = _field(created, "version")
            if not isinstance(version, str) or not version:
                raise TeamMcpFoundryError(None)
            return version
        return self._call(op)

    def promote(self, name: str, version: str) -> None:
        self._call(lambda project: project.toolboxes.update(name, default_version=version))

    def _arm(self, method: str, name: str, body: dict | None = None, *,
             api_version: str = CONNECTION_API_VERSION) -> dict | None:
        import httpx

        if self.project_scope is None:
            raise TeamMcpFoundryError(409)
        try:
            token = self.credential.get_token(ARM_TOKEN_SCOPE).token
        except Exception:
            raise TeamMcpFoundryError(None) from None
        url = f"{ARM}{self.project_scope}/connections/{quote(name, safe='')}"
        try:
            with httpx.Client(transport=self.transport or httpx.HTTPTransport(retries=0), trust_env=False,
                              follow_redirects=False, timeout=httpx.Timeout(30, connect=5)) as client:
                response = client.request(method, url, params={"api-version": api_version}, json=body,
                                          headers={"Authorization": "Bearer " + token})
        except httpx.HTTPError:
            raise TeamMcpFoundryError(None) from None
        if method == "GET" and response.status_code == 404:
            return None
        if response.status_code not in {200, 201}:
            raise TeamMcpFoundryError(response.status_code)
        try:
            value = response.json()
        except ValueError:
            raise TeamMcpFoundryError(None) from None
        return value if isinstance(value, dict) else {}

    def connection(self, name: str, *, api_version: str = CONNECTION_API_VERSION) -> dict | None:
        """The connection's non-secret metadata, or None when it does not exist."""
        value = self._arm("GET", name, api_version=api_version)
        return None if value is None else dict((value.get("properties") or {}).get("metadata") or {})

    def put_connection(self, name: str, *, target: str, keys: dict[str, str], metadata: dict[str, str]) -> None:
        self._arm("PUT", name, {"properties": {
            "category": "RemoteTool", "authType": "CustomKeys", "target": target, "isSharedToAll": False,
            "credentials": {"keys": keys}, "metadata": metadata}})

    def put_oauth2_connection(self, name: str, *, target: str, client_id: str, client_secret: str,
                              authorization_url: str, token_url: str, scopes: tuple[str, ...],
                              metadata: dict[str, str]) -> None:
        """A custom OAuth2 connection in the shape the P0b dev connection reads back; Foundry assigns the redirect
        URL and keeps each caller's token, so FA never holds one."""
        self._arm("PUT", name, {"properties": {
            "category": "RemoteTool", "authType": "OAuth2", "target": target, "isSharedToAll": False,
            "authorizationUrl": authorization_url, "tokenUrl": token_url, "refreshUrl": token_url,
            "scopes": list(scopes), "credentials": {"clientId": client_id, "clientSecret": client_secret},
            "metadata": metadata}}, api_version=OAUTH2_CONNECTION_API_VERSION)


def default_foundry_factory(composition, project_scope=None):
    def factory():
        try:
            endpoint = composition.config.targets[0].project_endpoint
            credential = composition.clients[0].credential
        except (AttributeError, IndexError, TypeError):
            raise Problem(503, "TEAM_MCP_UNAVAILABLE", "Foundry project configuration is unavailable") from None
        return TeamMcpFoundryClient(endpoint, credential, project_scope=project_scope)
    return factory


def _connection(item) -> TeamMcpConnection:
    name = _field(item, "name", "id", "connection_name")
    ctype = _field(item, "connection_type", "connectionType", "type", default="")
    credential = _field(item, "credential_type", "credentialType", "authentication_type", "authType", default=None)
    if credential is None:
        # azure-ai-projects 2.3.0 Connection: the auth type is `credentials.type` (ApiKey, AAD, SAS, CustomKeys, None, ...).
        credentials = _field(item, "credentials", default=None)
        credential = "" if credentials is None else _field(credentials, "type", default="")
    if isinstance(credential, dict):
        credential = credential.get("type", "")
    if credential is None:
        credential = "None"
    ctype, credential = getattr(ctype, "value", ctype), getattr(credential, "value", credential)
    return TeamMcpConnection(name=name, connection_type=str(ctype), credential_type=str(credential))


def _server(tool: dict, version: str) -> TeamMcpServer | None:
    """A plain MCP entry this page can show and edit; any other tool entry stays untouched."""
    data = {key: value for key, value in tool.items() if key != "type"}
    if tool.get("type") != "mcp" or not set(tool) <= _MANAGED_KEYS or not isinstance(data.get("allowed_tools"), list):
        return None
    try:
        return TeamMcpServer.model_validate_json(json.dumps({**data, "version": version}))
    except ValueError:
        return None


def _tool(server: TeamMcpServerInput, connection: str | None) -> dict:
    tool = {"type": "mcp", "server_label": server.server_label, "server_url": server.server_url,
            "require_approval": server.require_approval, "allowed_tools": list(server.allowed_tools)}
    connection = connection or server.project_connection_id
    if connection is not None:
        tool["project_connection_id"] = connection
    return tool


def authorize_team_owner(verifier: BehaviorJwtVerifier, principal, *, write=False) -> None:
    """Only the Team's current original human owner, reading or (with ``Behavior.Approve``) writing its own Team."""
    if not isinstance(principal, BehaviorPrincipal) or principal.tenant_id != verifier.auth.tenant_id:
        raise Problem(403, "TEAM_MCP_OWNER_ONLY", "only the delegated Team owner may manage Team MCP servers")
    try:
        principal.require(principal.team, "approve" if write else "read")
    except Problem:
        raise Problem(403, "TEAM_MCP_OWNER_ONLY", "only the delegated Team owner may manage Team MCP servers") from None
    if principal.kind != "human" or principal.actor_id is not None:
        raise Problem(403, "TEAM_MCP_OWNER_ONLY", "only the delegated Team owner may manage Team MCP servers")
    teams = verifier.teams
    if teams is None:
        valid = principal.oid == verifier.settings.human_oid and principal.team == verifier.settings.team
    else:
        value = teams.owner(principal.oid)
        valid = (value is not None and value.active and value.kind == "owner"
                 and (value.tenant_id, value.oid, value.team_id)
                 == (principal.tenant_id, principal.oid, principal.team))
    if not valid:
        raise Problem(403, "TEAM_MCP_OWNER_ONLY", "the current original Team owner is required")
    try:
        team_toolbox_name(principal.team)
    except ValueError:
        raise Problem(503, "TEAM_MCP_UNAVAILABLE", "the Team ID cannot be represented as a Foundry toolbox name") from None


def read_failure(error) -> Problem:
    if isinstance(error, Problem):
        return error
    status = _status(error)
    if status in {401, 403}:
        return Problem(403, "TEAM_MCP_FORBIDDEN", "Foundry refused Team MCP access", upstream_status=status)
    # A read: nothing changed; reading again is safe.
    return Problem(503, "TEAM_MCP_OUTCOME_UNKNOWN", "Foundry could not be read; read again", upstream_status=status)


def promote_request_version(foundry, toolbox: str, version: str, expected_version: str | None) -> None:
    """Make this request's version the default unless it already is."""
    try:
        default = foundry.default_version(toolbox)
        if default != version:
            if default != expected_version:
                # Another change became the default first; this request's version stays unused, not promoted.
                raise Problem(409, "TEAM_MCP_CHANGED", "read the current Team MCP settings and change again")
            foundry.promote(toolbox, version)
    except Problem:
        raise
    except Exception as error:
        # A refused promote may still have been applied by an earlier send: resend the same request to read it.
        failure = _write_failure(error, "toolbox default version")
        if failure.body["error"]["code"] == "TEAM_MCP_REFUSED":
            failure = Problem(503, "TEAM_MCP_OUTCOME_UNKNOWN", "resend the same Team MCP command request_id",
                              upstream_status=_status(error))
        raise failure from None


class TeamMcpService:
    def __init__(self, verifier: BehaviorJwtVerifier, *, foundry_factory, single_team: bool,
                 project_scope: str | None = None, feed_signing_key=None):
        self.verifier, self.foundry_factory = verifier, foundry_factory
        self.single_team, self.project_scope = single_team, project_scope
        # Ed25519 private key for the FA feed reader (feed_signature.py); None until the environment sets it.
        self.feed_signing_key = feed_signing_key
        self._grants: dict[tuple[str, str], tuple[float, tuple]] = {}
        self._grants_lock = threading.Lock()

    def _authorize(self, principal, *, write=False):
        authorize_team_owner(self.verifier, principal, write=write)

    _read_failure = staticmethod(read_failure)

    def _read(self, foundry, team: str) -> tuple[str | None, list[dict]]:
        try:
            return foundry.read_toolbox(team_toolbox_name(team))
        except Exception as error:
            raise self._read_failure(error) from None

    def _owned(self, team: str, name: str) -> bool:
        return self.project_scope is not None and name.startswith(team_connection_prefix(self.project_scope, team))

    def _view(self, principal, default: str | None, tools: list[dict]) -> TeamMcpView:
        servers = [server for server in (_server(tool, default) for tool in tools) if server is not None]
        return TeamMcpView(team_id=principal.team, toolbox=team_toolbox_name(principal.team), default_version=default,
                           single_team=self.single_team, servers=tuple(sorted(servers, key=lambda item: item.server_label)),
                           other_tools=len(tools) - len(servers), can_create_connection=self.project_scope is not None)

    def view(self, principal) -> TeamMcpView:
        self._authorize(principal)
        return self._view(principal, *self._read(self.foundry_factory(), principal.team))

    def _connections_raw(self) -> tuple[TeamMcpConnection, ...]:
        try:
            return tuple(sorted(self.foundry_factory().list_connections(), key=lambda item: item.name))
        except Exception as error:
            raise self._read_failure(error) from None

    def connections(self, principal) -> TeamMcpConnections:
        self._authorize(principal)
        listed, hidden = self._filter_connections(principal.team, self._connections_raw())
        return TeamMcpConnections(single_team=self.single_team, connections=listed, hidden_secret_connections=hidden)

    def _filter_connections(self, team: str, connections: tuple[TeamMcpConnection, ...]):
        connections = tuple(item.model_copy(update={"team_owned": self._owned(team, item.name)})
                            for item in connections if item.connection_type.casefold() in _MCP_CONNECTION_TYPES)
        if self.single_team:
            return connections, 0
        shown = tuple(item for item in connections if item.team_owned or not _secret_connection(item))
        return shown, len(connections) - len(shown)

    def _validate_connection(self, team: str, server: TeamMcpServerInput) -> None:
        if server.project_connection_id is None:
            return
        found = next((item for item in self._connections_raw() if item.name == server.project_connection_id), None)
        if found is None:
            raise Problem(422, "TEAM_MCP_CONNECTION_DENIED", "the referenced project connection is unavailable")
        if found.connection_type.casefold() not in _MCP_CONNECTION_TYPES:
            raise Problem(422, "TEAM_MCP_CONNECTION_DENIED", "the connection type is not supported for MCP",
                          reason="unsupported_connection_type")
        if not self.single_team and _secret_connection(found) and not self._owned(team, found.name):
            raise Problem(422, "TEAM_MCP_CONNECTION_DENIED", "secret_in_shared_project",
                          reason="secret_in_shared_project")

    @staticmethod
    def _changed_tools(command: TeamMcpChange, tools: list[dict], default: str | None, connection: str | None):
        managed = {server.server_label for server in (_server(tool, default) for tool in tools) if server is not None}
        labels = {tool.get("server_label") for tool in tools}
        label = command.server_label
        if command.operation == "add":
            if label in labels:
                raise Problem(409, "TEAM_MCP_EXISTS", "that Team MCP server already exists")
            if len(managed) >= MAX_TEAM_MCP_SERVERS:
                raise Problem(409, "TEAM_MCP_LIMIT", "the Team already has eight MCP servers")
            return [*tools, _tool(command.server, connection)]
        if label not in managed:
            raise Problem(409, "TEAM_MCP_MISSING", "that Team MCP server is not in the current version")
        index = next(index for index, tool in enumerate(tools) if tool.get("server_label") == label)
        if command.operation == "modify":
            return [*tools[:index], _tool(command.server, connection), *tools[index + 1:]]
        remaining = [*tools[:index], *tools[index + 1:]]
        if not remaining:
            # A toolbox version needs at least one tool, and the Team toolbox is never deleted (2026-10-03 decision).
            raise Problem(409, "TEAM_MCP_LAST_SERVER", "the Team's last MCP server cannot be disabled")
        return remaining

    def _connection_keys(self, new) -> dict[str, str]:
        """The connection's header keys. A feed URL also gets the API's signature of exactly that URL, so the FA feed
        reader fetches only URLs signed here; any other header is written as typed."""
        value = new.secret.get_secret_value()
        if not new.is_feed_url:
            return {new.header_name: value}
        if self.feed_signing_key is None:
            raise Problem(422, "TEAM_MCP_FEED_UNAVAILABLE", "this environment has no feed reader signing key")
        return {FEED_URL_HEADER: value, FEED_SIGNATURE_HEADER: sign_feed_url(self.feed_signing_key, value)}

    def _put_connection(self, foundry, principal, command: TeamMcpChange) -> str:
        if self.project_scope is None:
            raise Problem(422, "TEAM_MCP_CONNECTION_CREATE_UNAVAILABLE",
                          "this environment cannot create project connections")
        keys = self._connection_keys(command.new_connection)
        name = team_connection_name(self.project_scope, principal.team, command.request_id)
        try:
            existing = foundry.connection(name)
        except Exception as error:
            raise self._read_failure(error) from None
        if existing is not None:
            if existing.get("fa_request_id") == command.request_id:
                return name
            # Never overwrite a connection: a live version may still send its key to its own server.
            raise Problem(409, "TEAM_MCP_COMMAND_CONFLICT", "another change already owns this connection name")
        try:
            foundry.put_connection(name, target=command.server.server_url, keys=keys, metadata={
                "fa_team": principal.team, "fa_request_id": command.request_id,
                "fa_server_label": command.server_label})
        except Exception as error:
            _log.warning("TEAM_MCP_CONNECTION_WRITE_FAILED status=%s", _status(error))
            raise _write_failure(error, "connection") from None
        return name

    def _finish(self, foundry, principal, command: TeamMcpChange, version: str, connection: str | None):
        promote_request_version(foundry, team_toolbox_name(principal.team), version, command.expected_version)
        self.forget(principal.tenant_id, principal.team)
        return TeamMcpChangeResult(request_id=command.request_id, state="applied", toolbox_version=version,
                                   connection=connection, view=self._view(principal, *self._read(foundry, principal.team)))

    def submit(self, principal, command: TeamMcpChange) -> TeamMcpChangeResult:
        self._authorize(principal, write=True)
        toolbox = team_toolbox_name(principal.team)
        material = digest([principal.tenant_id, principal.team, principal.oid, command.material()])
        foundry = self.foundry_factory()
        # One API replica (maxReplicas 1): the lock keeps a resend from racing its original between lookup and create.
        with _team_lock(principal.tenant_id, principal.team):
            try:
                found = foundry.request_version(toolbox, command.request_id)
            except Exception as error:
                raise self._read_failure(error) from None
            if found is not None:
                if found["metadata"].get("fa_command") != material:
                    raise Problem(409, "TEAM_MCP_COMMAND_CONFLICT", "this request_id already names another change")
                return self._finish(foundry, principal, command, found["version"],
                                    found["metadata"].get("fa_connection") or None)
            default, tools = self._read(foundry, principal.team)
            if default != command.expected_version:
                raise Problem(409, "TEAM_MCP_CHANGED", "read the current Team MCP settings and change again")
            if command.server is not None and command.new_connection is None:
                self._validate_connection(principal.team, command.server)
            # Every refusal here comes before the connection write. A new key always gets a new connection, so even
            # if Foundry later refuses the version, no live version's connection or key has changed.
            self._changed_tools(command, tools, default, None)
            connection = (self._put_connection(foundry, principal, command)
                          if command.new_connection is not None else None)
            changed = self._changed_tools(command, tools, default, connection)
            metadata = {"fa_request_id": command.request_id, "fa_team": principal.team, "fa_command": material}
            if connection is not None:
                metadata["fa_connection"] = connection
            try:
                version = foundry.create_version(toolbox, tools=changed, metadata=metadata)
            except Exception as error:
                raise _write_failure(error, "toolbox version") from None
            return self._finish(foundry, principal, command, version, connection)

    def forget(self, tenant: str, team: str) -> None:
        with self._grants_lock:
            self._grants.pop((tenant, team), None)

    def team_grants(self, tenant: str, team: str) -> tuple:
        """The Team's servers in its current default version, as grants pinned to that version."""
        key, now = (tenant, team), monotonic()
        with self._grants_lock:
            cached = self._grants.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            default, tools = self.foundry_factory().read_toolbox(team_toolbox_name(team))
        except Exception as error:
            # Not a refusal: the last read servers stay offered; without one, this turn has no Team MCP tools.
            _log.warning("TEAM_MCP_GRANTS_UNAVAILABLE status=%s cached=%s", _status(error), cached is not None)
            return () if cached is None else cached[1]
        servers = sorted((server for server in (_server(tool, default) for tool in tools) if server is not None),
                         key=lambda item: item.server_label)
        value = tuple(team_mcp_grant(team, server) for server in servers)
        with self._grants_lock:
            self._grants[key] = (now + GRANTS_SECONDS, value)
        return value


# Retired head/intent rows (written until 2026-10-06): kept unchanged and only classified for migration and rotation.
class _LegacyServerInput(WireModel):
    server_label: ServerLabel
    server_url: Annotated[str, Field(min_length=1, max_length=2048)]
    project_connection_id: ConnectionName | None = Field(default=None, exclude_if=lambda value: value is None)
    allowed_tools: Annotated[tuple[TeamToolName, ...], Field(min_length=1, max_length=32)]


class _LegacyServer(_LegacyServerInput):
    version: ToolboxVersion


class _LegacyDesired(_LegacyServerInput):
    version: ToolboxVersion | None = Field(default=None, exclude_if=lambda value: value is None)


class _LegacyChange(Command):
    schema_version: Literal["fa-team-mcp-change-v1"]
    expected_revision: Annotated[int, Field(ge=0, le=2**31 - 1)]
    expected_etag: Annotated[str, Field(min_length=1, max_length=1024)] | None = None
    operation: Literal["add", "modify", "disable"]
    server_label: ServerLabel
    server: _LegacyServerInput | None = None


class _LegacyPending(WireModel):
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
    operation: Literal["add", "modify", "disable"]
    server_label: ServerLabel
    created_at: datetime


class _Head(_Model):
    kind: Literal["team_mcp_head"] = "team_mcp_head"
    tenant_id: str
    team_id: str
    toolbox: str
    revision: Annotated[int, Field(ge=0, le=2**31 - 1)]
    servers: Annotated[tuple[_LegacyServer, ...], Field(max_length=MAX_TEAM_MCP_SERVERS)] = ()
    pending: _LegacyPending | None = None


class _Intent(_Model):
    kind: Literal["team_mcp_intent"] = "team_mcp_intent"
    tenant_id: str
    team_id: str
    request_id: str
    principal_oid: str
    command: _LegacyChange
    base_revision: Annotated[int, Field(ge=0, le=2**31 - 1)]
    desired_servers: Annotated[tuple[_LegacyDesired, ...], Field(max_length=MAX_TEAM_MCP_SERVERS)]
    created_at: datetime
    state: Literal["pending", "applied", "refused"]
    toolbox_version: ToolboxVersion | None = Field(default=None, exclude_if=lambda value: value is None)
    refused_code: str | None = Field(default=None, exclude_if=lambda value: value is None)


def _head_key(tenant: str, team: str) -> str:
    return team_mcp_key("head", tenant, team)


def _intent_key(tenant: str, team: str, request_id: str) -> str:
    return team_mcp_key("intent", tenant, team, request_id)


def _record(rows, key, model):
    row = rows.get(key)
    if row is None:
        return None
    return _parse(row.payload["data"], model)


def validate_team_mcp_history(inventory, config):
    """Classify the retired Team MCP head and intent rows for migration and rotation; nothing reads or resumes them."""
    from .behavior_team_registry import TeamOwnership, team_key

    models = {model.model_fields["kind"].default: model for model in (_Head, _Intent)}
    values = {}
    for key, row in inventory.rows.items():
        data = row.payload["data"]
        kind = data.get("kind")
        if not isinstance(kind, str) or not kind.startswith("team_mcp_"):
            continue
        if kind not in models:
            raise Problem(409, "TEAM_MCP_INVALID", "unknown retained Team MCP schema")
        value = _parse(data, models[kind])
        tenant, team = value.tenant_id, value.team_id
        if tenant != config.identity.tenant_id:
            raise Problem(409, "TEAM_MCP_INVALID", "retained Team MCP data belongs to another tenant")
        if team == config.identity.team_id:
            owner_oid = config.identity.human_oid
        else:
            ownership = _record(inventory.rows, team_key(tenant, team), TeamOwnership)
            if ownership is None or (ownership.tenant_id, ownership.team_id) != (tenant, team):
                raise Problem(409, "TEAM_MCP_INVALID", "retained Team MCP data has no original Team owner")
            owner_oid = ownership.owner_oid
        if isinstance(value, _Head):
            expected = _head_key(tenant, team)
        else:
            expected = _intent_key(tenant, team, value.request_id)
            if value.command.request_id != value.request_id or value.principal_oid != owner_oid:
                raise Problem(409, "TEAM_MCP_INVALID", "the retained Team MCP command has no matching Team owner")
        if key != expected:
            raise Problem(409, "TEAM_MCP_INVALID", "retained Team MCP key differs from its original identity")
        values[key] = value
    return values


def current_team_mcp_grants(runtime) -> tuple:
    # AssistantRuntime.bindings maps binding_id -> RuntimeBinding; every binding of one runtime is in one Team.
    service = getattr(runtime, "team_mcp", None)
    raw = getattr(runtime, "bindings", None) or {}
    bindings = tuple(raw.values()) if hasattr(raw, "values") else tuple(raw)
    if service is None or not bindings:
        return ()
    try:
        team_toolbox_name(bindings[0].team_id)
    except ValueError:
        return ()
    return service.team_grants(bindings[0].tenant_id, bindings[0].team_id)
