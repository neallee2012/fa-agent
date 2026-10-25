"""Code-owned references for the Team connectors FA can add; not a connector catalog and never a token store.

Each ``ConnectorRef`` keeps only what FA must write and Foundry does not provide: the server URL, how its project
connection authenticates, and which tools are reads or writes. Tool descriptions and schemas come from the
toolbox's ``tools/list``, plus an entry's fixed ``tool_notes``; a tool in neither list stays closed until a PR
classifies it. SDK-independent.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_wire import WireModel
from .team_mcp_wire import ApprovalSetting, ServerLabel, TeamToolName, _check_server_url


class ConnectorEntry(WireModel):
    """One toolbox MCP entry: ``require_approval`` is per server, so reads and writes are separate entries.
    ``tool_notes`` adds a fixed usage note to a tool's listed description where the server's own schema misleads."""

    server_label: ServerLabel
    allowed_tools: Annotated[tuple[TeamToolName, ...], Field(min_length=1, max_length=32)]
    default_approval: ApprovalSetting
    tool_notes: Mapping[TeamToolName, Annotated[str, Field(min_length=1, max_length=400)]] = {}

    @model_validator(mode="after")
    def notes_for_allowed_tools(self):
        if not set(self.tool_notes) <= set(self.allowed_tools):
            raise ValueError("a tool note names a tool outside allowed_tools")
        return self


class CustomOAuth2(WireModel):
    """A Foundry ``OAuth2`` project connection with the deployment's own Entra app. ``app_setting`` names the API
    environment variable holding the app's client ID; the client secret is never configured or stored by FA."""

    kind: Literal["custom_oauth2"] = "custom_oauth2"
    app_setting: Annotated[str, Field(pattern=r"^FA_CONNECTOR_[A-Z0-9_]{1,40}$")]
    scopes: Annotated[tuple[Annotated[str, Field(min_length=1, max_length=256)], ...], Field(min_length=1)]


ConnectorId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9]{0,15}$")]


class ConnectorRef(WireModel):
    id: ConnectorId
    display_name: str
    server_url: str
    authentication: CustomOAuth2
    read: ConnectorEntry
    write: ConnectorEntry
    revoke_url: str
    disclosure: str
    # False: FA_NETWORK_MODE=private refuses adding or re-enabling it (ADR-0010).
    private_network: bool = True

    @model_validator(mode="after")
    def distinct_entries(self):
        _check_server_url(self.server_url)
        if self.read.server_label == self.write.server_label:
            raise ValueError("read and write are separate toolbox entries")
        if set(self.read.allowed_tools) & set(self.write.allowed_tools):
            raise ValueError("a tool is either a read or a write")
        return self

    @property
    def entries(self) -> tuple[ConnectorEntry, ConnectorEntry]:
        return self.read, self.write


TEAMS = ConnectorRef(
    id="teams",
    display_name="Microsoft Teams",
    server_url="https://workiq.svc.cloud.microsoft/mcp",
    authentication=CustomOAuth2(
        app_setting="FA_CONNECTOR_TEAMS_CLIENT_ID",
        scopes=("api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask", "offline_access")),
    read=ConnectorEntry(
        server_label="teams_read",
        allowed_tools=("fetch", "fetch_blob", "get_schema", "list_agents", "search_paths"),
        default_approval="never"),
    # The server marks call_function readOnlyHint=true, but it calls an arbitrary function URL; ask hands work to
    # another Work IQ agent that may act. Both stay writes.
    write=ConnectorEntry(
        server_label="teams_write",
        allowed_tools=("ask", "call_function", "create_entity", "delete_entity", "do_action", "update_entity"),
        default_approval="always",
        # get_schema lists the message body as itemBody with "@odata.type"; Teams answers that with 400 (dev 2026-10-10).
        tool_notes={"create_entity": (
            'To post a Teams chat or channel message: parentUrl ends with /messages; jsonBody is '
            '{"body":{"contentType":"text" or "html","content":"..."}}. Do not put @odata.type inside body '
            '(Teams rejects it with 400).')}),
    revoke_url="https://entra.microsoft.com",
    disclosure=("Microsoft Teams 經 Work IQ 連線。Agent 能讀取的不只 Teams，也可能包含你的 Microsoft 365 "
                "郵件與檔案。"),
    # Work IQ is not supported in a network-isolated project (ADR-0010, toolbox network isolation docs).
    private_network=False,
)

CONNECTORS = MappingProxyType({ref.id: ref for ref in (TEAMS,)})
