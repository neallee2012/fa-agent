"""Team connectors: one registered connector (``connector_registry.py``) per Team, in its own Foundry toolbox
``fa-connector-{team}-{connector}`` with a read and a write entry on one stable OAuth project connection.

The toolbox's default version is the only record. Its metadata says whether the connector is enabled; disabling
writes a new version with the same tools and ``fa_connector_enabled=false``, so no toolbox is emptied or deleted.
SDK-independent. The client secret is a ``SecretStr``: no dump, digest, log or response carries it.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from .behavior_capabilities import CapabilityGrant
from .behavior_wire import Command, Identifier, WireModel
from .connector_registry import CONNECTORS, ConnectorId, ConnectorRef
from .team_mcp_wire import (
    MAX_SECRET_CHARS, ApprovalSetting, ConnectionName, TeamMcpServer, ToolboxVersion, _flag,
)

TEAM_CONNECTORS_ENABLED_ENV = "FA_TEAM_CONNECTORS_ENABLED"
CONNECTOR_ENTRY_PREFIX = "connector-"
_TOOLBOX_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
_ENTRY_CONNECTORS = {CONNECTOR_ENTRY_PREFIX + entry.server_label: ref.id
                     for ref in CONNECTORS.values() for entry in ref.entries}


def team_connectors_enabled(environ) -> bool:
    return _flag(environ, TEAM_CONNECTORS_ENABLED_ENV)


def connector_toolbox_name(team_id: str, connector_id: str) -> str:
    """Its own toolbox: an unconsented OAuth source makes the whole toolbox's tools/list fail (P0a). The prefix
    differs from Team MCP's ``fa-team-`` so no Team ID can name another Team's connector toolbox."""
    name = f"fa-connector-{team_id}-{connector_id}"
    if _TOOLBOX_NAME.fullmatch(name) is None:
        raise ValueError("Team ID and connector cannot be represented as one Foundry toolbox name")
    return name


def connector_entry_id(server_label: str) -> str:
    return CONNECTOR_ENTRY_PREFIX + server_label


class TeamConnectorChange(Command):
    """The connector's whole desired state. Add, approval change, disable and re-enable are all this one command,
    and each applied command is one new toolbox version."""

    schema_version: Literal["fa-team-connector-change-v1"]
    connector_id: str
    # The connector toolbox's default_version the owner read; None only when it has no toolbox yet.
    expected_version: ToolboxVersion | None
    enabled: bool
    read_approval: ApprovalSetting
    write_approval: ApprovalSetting
    # Needed only when the Team's connection does not exist yet; a later secret replaces it under the same name.
    client_secret: Annotated[SecretStr, Field(min_length=1, max_length=MAX_SECRET_CHARS)] | None = Field(
        default=None, exclude_if=lambda value: value is None)

    @field_validator("connector_id")
    @classmethod
    def registered(cls, value):
        if value not in CONNECTORS:
            raise ValueError("unknown connector")
        return value

    @field_validator("client_secret")
    @classmethod
    def one_line(cls, value):
        if value is not None and any(char in value.get_secret_value() for char in "\r\n\0"):
            raise ValueError("the client secret is one line")
        return value

    @property
    def ref(self) -> ConnectorRef:
        return CONNECTORS[self.connector_id]

    def material(self) -> dict:
        """The command without its secret: the same request ID names this exact change."""
        return {**self.model_dump(mode="json", exclude={"client_secret"}),
                "client_secret": self.client_secret is not None}


class TeamConnectorSettings(WireModel):
    """What the default version, as FA wrote it, says."""

    enabled: bool
    read_approval: ApprovalSetting
    write_approval: ApprovalSetting


class TeamConnectorState(WireModel):
    connector_id: Identifier
    display_name: str
    disclosure: str
    revoke_url: str
    toolbox: str
    connection: ConnectionName
    # None: never added. A default version with no settings is one FA did not write; it offers no tools.
    default_version: ToolboxVersion | None
    settings: TeamConnectorSettings | None


class TeamConnectorsView(WireModel):
    schema_version: Literal["fa-team-connectors-v1"] = "fa-team-connectors-v1"
    team_id: Identifier
    connectors: tuple[TeamConnectorState, ...]


class TeamConnectorChangeResult(WireModel):
    schema_version: Literal["fa-team-connector-change-result-v1"] = "fa-team-connector-change-result-v1"
    request_id: Identifier
    state: Literal["applied"]
    toolbox_version: ToolboxVersion
    connector: TeamConnectorState
    original_request_resent: Literal[False] = False


def connector_tools(ref: ConnectorRef, connection: str, command: TeamConnectorChange) -> list[dict]:
    """The version's two entries: only the registry's URL, connection and allowlisted tools."""
    approvals = {ref.read.server_label: command.read_approval, ref.write.server_label: command.write_approval}
    return [{"type": "mcp", "server_label": entry.server_label, "server_url": ref.server_url,
             "require_approval": approvals[entry.server_label], "allowed_tools": list(entry.allowed_tools),
             "project_connection_id": connection} for entry in ref.entries]


def connector_metadata(team_id: str, ref: ConnectorRef, connection: str, command: TeamConnectorChange,
                       command_digest: str) -> dict[str, str]:
    return {"fa_request_id": command.request_id, "fa_team": team_id, "fa_connector": ref.id,
            "fa_connector_enabled": "true" if command.enabled else "false", "fa_connection": connection,
            "fa_command": command_digest}


def connector_servers(team_id: str, ref: ConnectorRef, connection: str, metadata: dict,
                      servers: dict[str, TeamMcpServer], tool_count: int):
    """The settings and the two servers when the default version is exactly one FA wrote for this Team and
    connector; otherwise None, and the version offers no tools."""
    enabled = {"true": True, "false": False}.get(metadata.get("fa_connector_enabled"))
    if (enabled is None or metadata.get("fa_team") != team_id or metadata.get("fa_connector") != ref.id
            or tool_count != len(ref.entries) or set(servers) != {entry.server_label for entry in ref.entries}):
        return None
    for entry in ref.entries:
        server = servers[entry.server_label]
        if (server.server_url != ref.server_url or server.project_connection_id != connection
                or server.require_approval is None or not set(server.allowed_tools) <= set(entry.allowed_tools)):
            return None
    settings = TeamConnectorSettings(enabled=enabled, read_approval=servers[ref.read.server_label].require_approval,
                                     write_approval=servers[ref.write.server_label].require_approval)
    return settings, tuple(servers[entry.server_label] for entry in ref.entries)


MAX_CONNECTOR_GRANTS = len(_ENTRY_CONNECTORS)


def connector_of(entry_id: str) -> str | None:
    """The registered connector a toolbox grant's entry belongs to; None for any other grant."""
    return _ENTRY_CONNECTORS.get(entry_id)


class ConnectorCapture(WireModel):
    """The Team connectors one turn may call, kept beside the human-selected CapabilityCapture: connectors are on by
    default and take no selection slot. A conversation's first turn fixes its connectors; a later turn keeps only
    those the Team still enables (team_connector_narrowing.resolve_connectors)."""

    schema_version: Literal["fa-connector-capture-v1"] = "fa-connector-capture-v1"
    grants: Annotated[tuple[CapabilityGrant, ...], Field(min_length=1, max_length=MAX_CONNECTOR_GRANTS)]

    @model_validator(mode="after")
    def registered_entries(self):
        names = [grant.entry_id for grant in self.grants]
        if names != sorted(set(names)):
            raise ValueError("Connector grants must be distinct and ordered")
        if any(grant.kind != "mcp-toolbox" or connector_of(grant.entry_id) is None for grant in self.grants):
            raise ValueError("A connector grant must be a registered connector's toolbox entry")
        return self

    @property
    def connector_ids(self) -> frozenset[str]:
        return frozenset(connector_of(grant.entry_id) for grant in self.grants)


class SelectAgentConnectors(Command):
    """The owner's whole per-Agent connector choice for new conversations; a connector not listed stays on."""

    schema_version: Literal["fa-agent-connectors-command-v1"]
    # The revision the owner read; 0 while this Agent has never changed it.
    expected_revision: Annotated[int, Field(ge=0)]
    disabled_connector_ids: Annotated[tuple[ConnectorId, ...], Field(max_length=16)]

    @field_validator("disabled_connector_ids")
    @classmethod
    def ordered(cls, value):
        if list(value) != sorted(set(value)):
            raise ValueError("disabled connectors must be distinct and ordered")
        return value


class AgentConnectorsView(WireModel):
    schema_version: Literal["fa-agent-connectors-v1"] = "fa-agent-connectors-v1"
    revision: Annotated[int, Field(ge=0)]
    disabled_connector_ids: tuple[ConnectorId, ...]
    applies_to: Literal["new-conversations-only"] = "new-conversations-only"
