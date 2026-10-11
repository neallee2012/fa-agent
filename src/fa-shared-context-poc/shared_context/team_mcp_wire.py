"""Team-owned MCP servers in the Team's own Foundry toolbox; the official versions and ``default_version`` are the
only record of which servers exist.

SDK-independent: the console imports these DTOs and helpers without Azure SDKs. A secret typed for a new project
connection is a ``SecretStr``: no dump, digest, log or response carries it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from .behavior_capabilities import CapabilityGrant
from .behavior_wire import Command, Identifier, WireModel
from .feed_signature import FEED_URL_HEADER, check_feed_url
from .tool_catalog import CatalogError, ToolboxEntry, _check_plain_value, _looks_secret

TEAM_MCP_ENABLED_ENV = "FA_TEAM_MCP_ENABLED"
TEAM_MCP_SINGLE_TEAM_ENV = "FA_TEAM_MCP_SINGLE_TEAM"
MAX_TEAM_MCP_SERVERS = 8
TEAM_ENTRY_PREFIX = "team-"
MAX_SECRET_CHARS = 4096

_SERVER_LABEL = r"^[A-Za-z][A-Za-z0-9_]{0,20}$"
_TEAM_TOOL_NAME = r"^[A-Za-z0-9_-]{1,32}$"
_CONNECTION_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_TOOLBOX_VERSION = r"^[A-Za-z0-9._-]{1,64}$"
_HEADER_NAME = r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$"
_TOOLBOX_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
_SERVER_LABEL_RE = re.compile(_SERVER_LABEL)

ServerLabel = Annotated[str, Field(pattern=_SERVER_LABEL)]
TeamToolName = Annotated[str, Field(pattern=_TEAM_TOOL_NAME)]
ConnectionName = Annotated[str, Field(pattern=_CONNECTION_NAME)]
ToolboxVersion = Annotated[str, Field(pattern=_TOOLBOX_VERSION)]
# The official toolbox setting: ``always`` waits for the owner's approval of every call, ``never`` runs directly.
ApprovalSetting = Literal["always", "never"]

_SECRET_MARKER = "${secret:"


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _flag(environ, name: str) -> bool:
    value = environ.get(name, "")
    if value in {"", "0"}:
        return False
    if value == "1":
        return True
    raise ValueError(f"{name} must be absent, 0 or 1")


def team_mcp_settings(environ=None) -> tuple[bool, bool]:
    environ = os.environ if environ is None else environ
    return _flag(environ, TEAM_MCP_ENABLED_ENV), _flag(environ, TEAM_MCP_SINGLE_TEAM_ENV)


def team_toolbox_name(team_id) -> str:
    name = "fa-team-" + str(team_id)
    if _TOOLBOX_NAME.fullmatch(name) is None:
        raise ValueError("Team ID cannot be represented as one Foundry toolbox name")
    return name


def team_entry_id(label) -> str:
    if not isinstance(label, str) or _SERVER_LABEL_RE.fullmatch(label) is None:
        raise ValueError("invalid Team MCP server label")
    return TEAM_ENTRY_PREFIX + label


def _check_server_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("server_url must be an HTTPS URL of at most 2048 characters")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError("server_url must be an https URL without credentials or fragment")
    if _SECRET_MARKER in value:
        raise ValueError("TEAM_MCP_PLAINTEXT_SECRET")
    for segment in parts.path.split("/"):
        if _looks_secret(segment):
            raise ValueError("TEAM_MCP_PLAINTEXT_SECRET")
    for name, item in parse_qsl(parts.query, keep_blank_values=True):
        try:
            _check_plain_value("server_url query parameter", name, item)
        except CatalogError as error:
            if error.code == "CATALOG_PLAINTEXT_SECRET":
                raise ValueError("TEAM_MCP_PLAINTEXT_SECRET") from None
            raise ValueError("invalid server_url") from None
    return value


class _ServerFields(WireModel):
    server_label: ServerLabel
    server_url: Annotated[str, Field(min_length=1, max_length=2048)]
    project_connection_id: ConnectionName | None = Field(default=None, exclude_if=lambda value: value is None)
    allowed_tools: Annotated[tuple[TeamToolName, ...], Field(min_length=1, max_length=32)]

    _url = field_validator("server_url")(_check_server_url)

    @model_validator(mode="after")
    def exact_tools(self):
        tools = tuple(sorted(self.allowed_tools))
        if len(set(tools)) != len(tools):
            raise ValueError("allowed_tools must be distinct")
        if any(len(f"{team_entry_id(self.server_label)}-{tool}") > 64 for tool in tools):
            raise ValueError("server label and tool name exceed the model function-name bound")
        object.__setattr__(self, "allowed_tools", tools)
        return self


class TeamMcpServerInput(_ServerFields):
    require_approval: ApprovalSetting


class TeamMcpServer(_ServerFields):
    """One MCP server of the Team toolbox's default version, as Foundry returns it."""

    # None: the version itself does not set it; the runtime then follows what the toolbox's tools/list reports.
    require_approval: ApprovalSetting | None = None
    version: ToolboxVersion

    @property
    def entry_id(self) -> str:
        return team_entry_id(self.server_label)


def toolbox_entry_sha256(entry_id: str, toolbox: str, server: _ServerFields) -> str:
    """The server's definition only: not its toolbox version or approval setting, so adding another server or
    changing an approval keeps an Agent's selection; a new URL, connection or tool list is a new selection."""
    config = ToolboxEntry(type="toolbox", toolbox=toolbox, server_label=server.server_label)
    material = {
        "entry_id": entry_id,
        "execution": "foundry-toolbox",
        "tools": list(server.allowed_tools),
        "config": config.model_dump(mode="json"),
        "server_url": server.server_url,
        "project_connection_id": server.project_connection_id,
    }
    return hashlib.sha256(_canonical(material)).hexdigest()


def team_mcp_entry_sha256(team_id: str, server: _ServerFields) -> str:
    return toolbox_entry_sha256(team_entry_id(server.server_label), team_toolbox_name(team_id), server)


def toolbox_grant(entry_id: str, toolbox: str, server: TeamMcpServer) -> CapabilityGrant:
    """Pinned to the default version read at capture: an immutable definition and approval setting for the turn."""
    return CapabilityGrant(
        kind="mcp-toolbox",
        entry_id=entry_id,
        entry_sha256=toolbox_entry_sha256(entry_id, toolbox, server),
        tools=tuple(f"{entry_id}-{tool}" for tool in server.allowed_tools),
        vision=False,
        toolbox=ToolboxEntry(type="toolbox", toolbox=toolbox, server_label=server.server_label, version=server.version),
    )


def team_mcp_grant(team_id: str, server: TeamMcpServer) -> CapabilityGrant:
    return toolbox_grant(team_entry_id(server.server_label), team_toolbox_name(team_id), server)


class TeamMcpNewConnection(WireModel):
    """A key header for a new project connection the API creates; the value goes only to Foundry.

    Header ``x-feed-url`` names the FA feed reader: the value must be the private https feed URL, and the API adds
    its signature header (shared_context/feed_signature.py)."""

    header_name: Annotated[str, Field(pattern=_HEADER_NAME)]
    secret: Annotated[SecretStr, Field(min_length=1, max_length=MAX_SECRET_CHARS)]

    @field_validator("secret")
    @classmethod
    def one_line(cls, value):
        if any(char in value.get_secret_value() for char in "\r\n\0"):
            raise ValueError("the key is one line")
        return value

    @model_validator(mode="after")
    def feed_url(self):
        if self.is_feed_url:
            try:
                check_feed_url(self.secret.get_secret_value())
            except ValueError:
                raise ValueError("TEAM_MCP_FEED_URL_INVALID") from None
        return self

    @property
    def is_feed_url(self) -> bool:
        return self.header_name.casefold() == FEED_URL_HEADER


class TeamMcpChange(Command):
    schema_version: Literal["fa-team-mcp-change-v2"]
    # The toolbox's default_version the owner read; None only when the Team has no toolbox yet.
    expected_version: ToolboxVersion | None
    operation: Literal["add", "modify", "disable"]
    server_label: ServerLabel
    server: TeamMcpServerInput | None = None
    new_connection: TeamMcpNewConnection | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def exact_change(self):
        if self.operation in {"add", "modify"}:
            if self.server is None or self.server.server_label != self.server_label:
                raise ValueError("add/modify needs a matching complete server")
        elif self.server is not None or self.new_connection is not None:
            raise ValueError("disable does not include a replacement server or connection")
        if self.new_connection is not None and self.server.project_connection_id is not None:
            raise ValueError("a new connection is named by the API, not chosen")
        return self

    def material(self) -> dict:
        """The command without its secret: the same request ID names this exact change."""
        return self.model_dump(mode="json")


class TeamMcpConnection(WireModel):
    name: ConnectionName
    connection_type: str
    credential_type: str
    team_owned: bool = False


class TeamMcpView(WireModel):
    schema_version: Literal["fa-team-mcp-v2"] = "fa-team-mcp-v2"
    team_id: Identifier
    toolbox: str
    default_version: ToolboxVersion | None
    single_team: bool
    servers: Annotated[tuple[TeamMcpServer, ...], Field(max_length=MAX_TEAM_MCP_SERVERS)]
    # Other tools in the toolbox that this page does not manage; kept as they are by every change.
    other_tools: Annotated[int, Field(ge=0)] = 0
    hidden_connections: Annotated[int, Field(ge=0)] = 0
    can_create_connection: bool = False

    @model_validator(mode="after")
    def ordered(self):
        labels = [item.server_label for item in self.servers]
        if labels != sorted(set(labels)):
            raise ValueError("servers must be sorted by label and distinct")
        if any(item.version != self.default_version for item in self.servers):
            raise ValueError("every listed server is read from the default version")
        return self


class TeamMcpConnections(WireModel):
    schema_version: Literal["fa-team-mcp-connections-v2"] = "fa-team-mcp-connections-v2"
    single_team: bool
    connections: tuple[TeamMcpConnection, ...]
    hidden_secret_connections: Annotated[int, Field(ge=0)]


class TeamMcpChangeResult(WireModel):
    schema_version: Literal["fa-team-mcp-change-result-v2"] = "fa-team-mcp-change-result-v2"
    request_id: Identifier
    state: Literal["applied"]
    toolbox_version: ToolboxVersion
    connection: ConnectionName | None = Field(default=None, exclude_if=lambda value: value is None)
    view: TeamMcpView
    original_request_resent: Literal[False] = False
