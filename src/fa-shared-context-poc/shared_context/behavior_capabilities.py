"""Human-selected capability references and their per-turn capture; SDK-independent.

A template (and so the profile) names catalog entries by ``CapabilityRef``. Each
new turn captures the effective set as the original selection's references
that are still in the current selection head and in the current catalog, with
the same digest. The set can only shrink within a conversation. Captured grants
are then read from the original record; nothing is recomputed from current
configuration when a work is rebuilt or history is validated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .tool_catalog import CatalogEntry, StdioEntry, ToolboxEntry, ToolCatalog, build_catalog, load_catalog

MAX_CAPABILITIES = 12
_log = logging.getLogger(__name__)
_TEAM_MCP_COLLISIONS_LOGGED: set[str] = set()
# The memory variant is derived from its independent capture, not selectable as a template role.
CAPABILITY_BASE_PROFILES = frozenset({
    "fa-instruction-v2", "fa-instruction-v3", "fa-instruction-v4",
    "fa-instruction-history-v1", "fa-instruction-history-skill-draft-v1",
    "fa-instruction-history-skill-draft-memory-v1",
    "fa-instruction-agent-role-v1", "fa-instruction-agent-role-v2", "fa-instruction-agent-role-v3",
    "fa-instruction-agent-role-v4",
})

EntryId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")]
ToolName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
CapabilityKind = Literal["mcp-api", "mcp-hosted", "mcp-toolbox", "fa-builtin"]


class CapabilityRef(WireModel):
    entry_id: EntryId
    entry_sha256: Digest


def validate_refs(refs: tuple[CapabilityRef, ...]) -> tuple[CapabilityRef, ...]:
    names = [item.entry_id for item in refs]
    if len(refs) > MAX_CAPABILITIES or names != sorted(set(names)):
        raise ValueError(f"Capability references must be distinct, ordered and at most {MAX_CAPABILITIES}")
    return refs


CapabilityRefs = Annotated[tuple[CapabilityRef, ...], Field(max_length=MAX_CAPABILITIES)]


class CapabilityGrant(WireModel):
    entry_id: EntryId
    entry_sha256: Digest
    kind: CapabilityKind
    tools: Annotated[tuple[ToolName, ...], Field(min_length=1, max_length=32)]
    vision: bool
    launch: StdioEntry | None = Field(default=None, exclude_if=lambda value: value is None)
    toolbox: ToolboxEntry | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def launch_only_in_hosted(self):
        if (self.kind == "mcp-hosted") != (self.launch is not None):
            raise ValueError("Only a Hosted MCP grant carries its pinned stdio launch")
        if (self.kind == "mcp-toolbox") != (self.toolbox is not None):
            raise ValueError("Only a toolbox grant carries its project toolbox and server label")
        return self

    @property
    def ref(self) -> CapabilityRef:
        return CapabilityRef(entry_id=self.entry_id, entry_sha256=self.entry_sha256)


class CapabilityCapture(WireModel):
    schema_version: Literal["fa-capability-capture-v1"] = "fa-capability-capture-v1"
    head_selection_id: Identifier | None
    grants: Annotated[tuple[CapabilityGrant, ...], Field(max_length=MAX_CAPABILITIES)]

    @model_validator(mode="after")
    def ordered(self):
        validate_refs(tuple(item.ref for item in self.grants))
        names = [name for grant in self.grants for name in grant.tools]
        if len(names) != len(set(names)):
            raise ValueError("Captured capabilities must not expose the same tool name twice")
        return self

    @property
    def vision(self) -> bool:
        return any(item.vision for item in self.grants)


class TurnGrants(CapabilityCapture):
    """One native turn's tools: the human-selected capture plus the Team connectors captured beside it. Built in
    memory from those two validated captures and never stored, so only the selected part counts toward the limit."""

    grants: tuple[CapabilityGrant, ...]

    @model_validator(mode="after")
    def ordered(self):
        names = [item.entry_id for item in self.grants]
        tools = [name for grant in self.grants for name in grant.tools]
        if names != sorted(set(names)) or len(tools) != len(set(tools)):
            raise ValueError("A turn's grants must be distinct, ordered and expose each tool name once")
        return self


def turn_grants(selected: CapabilityCapture | None, connectors: tuple[CapabilityGrant, ...]) -> CapabilityCapture | None:
    if not connectors:
        return selected
    grants = (*(() if selected is None else selected.grants), *connectors)
    return TurnGrants(head_selection_id=None if selected is None else selected.head_selection_id,
                      grants=tuple(sorted(grants, key=lambda item: item.entry_id)))


@dataclass(frozen=True)
class BuiltinCapability:
    """An FA-owned capability; its tools and needs are code, not organization JSON."""

    entry_id: str
    tools: tuple[str, ...]
    vision: bool

    @property
    def entry_sha256(self) -> str:
        material = {"entry_id": self.entry_id, "kind": "fa-builtin", "tools": list(self.tools), "vision": self.vision}
        return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


SKILLS_ENTRY_ID = "fa-skills-v1"
SKILL_RESOURCES_ENTRY_ID = "fa-skill-resources-v1"
SKILLIFY_ENTRY_ID = "fa-skillify-v1"
DELEGATE_READER_ENTRY_ID = "fa-delegate-reader-v1"
BUILTIN_CAPABILITIES = {
    item.entry_id: item for item in (
        BuiltinCapability("fa-desktop-v1",
                          ("desktop_open_file", "desktop_observe", "desktop_act", "desktop_read_file"), vision=True),
        BuiltinCapability("fa-delegate-v1", ("task",), vision=False),
        BuiltinCapability(DELEGATE_READER_ENTRY_ID, ("read_user_file",), vision=False),
        BuiltinCapability("fa-agent-collaboration-v1", ("send_agent_message",), vision=False),
        BuiltinCapability(SKILLS_ENTRY_ID, ("read_skill",), vision=False),
        BuiltinCapability(SKILL_RESOURCES_ENTRY_ID, ("read_skill_resource",), vision=False),
        BuiltinCapability("fa-memory-candidate-v1", ("stage_memory_candidate",), vision=False),
        BuiltinCapability(SKILLIFY_ENTRY_ID, ("save_skill_draft",), vision=False),
        BuiltinCapability("fa-image-delivery-v1", ("deliver_image",), vision=False),
        BuiltinCapability("fa-shell-v1", ("run_command",), vision=False),
    )
}
# Builtins that switch on an existing FA tool path instead of a Hosted handler; their tools stay base tools
# for the gateway, boundary and Hosted registration.
FEATURE_TOOLS = {SKILLS_ENTRY_ID: frozenset({"read_skill"}), SKILLIFY_ENTRY_ID: frozenset({"save_skill_draft"}),
                 DELEGATE_READER_ENTRY_ID: frozenset({"read_user_file"})}
# Legacy guidance-only builtins; v2/v3/v4 and agent-role v2/v3/v4 can separately select the existing skill-draft writer.
BASE_TOOL_GUIDANCE = frozenset({SKILLIFY_ENTRY_ID, DELEGATE_READER_ENTRY_ID})


def requires_base_tool(entry_id: str, prompt_profile: str | None) -> bool:
    return entry_id in BASE_TOOL_GUIDANCE and not (
        entry_id == SKILLIFY_ENTRY_ID
        and prompt_profile in {"fa-instruction-v2", "fa-instruction-v3", "fa-instruction-v4",
                               "fa-instruction-agent-role-v2", "fa-instruction-agent-role-v3",
                               "fa-instruction-agent-role-v4"})


def base_supports(entry_id: str, prompt_profile: str | None) -> bool:
    """Whether a composable base profile declares the tools this capability needs (the selection rule below)."""
    if not requires_base_tool(entry_id, prompt_profile):
        return True
    from .behavior_skill_prompt import assemble_prompt
    tools = BUILTIN_CAPABILITIES[entry_id].tools
    return bool(tools) and set(tools) <= set(assemble_prompt(prompt_profile).record.declared_capabilities)


def skills_selected(refs: tuple[CapabilityRef, ...]) -> bool:
    return any(ref.entry_id == SKILLS_ENTRY_ID for ref in refs)


def skills_granted(capture: CapabilityCapture | None) -> bool:
    return capture is not None and any(grant.entry_id == SKILLS_ENTRY_ID for grant in capture.grants)


def skill_resources_granted(capture: CapabilityCapture | None) -> bool:
    return capture is not None and all(
        _grant(BUILTIN_CAPABILITIES[name]) in capture.grants
        for name in (SKILLS_ENTRY_ID, SKILL_RESOURCES_ENTRY_ID))


def skillify_granted(capture: CapabilityCapture | None) -> bool:
    return capture is not None and any(
        grant.entry_id == SKILLIFY_ENTRY_ID and grant == _grant(BUILTIN_CAPABILITIES[SKILLIFY_ENTRY_ID])
        for grant in capture.grants)


def feature_tools(capture: CapabilityCapture | None) -> frozenset:
    if capture is None:
        return frozenset()
    return frozenset(name for grant in capture.grants for name in FEATURE_TOOLS.get(grant.entry_id, ()))


def _grant(entry: CatalogEntry | BuiltinCapability) -> CapabilityGrant:
    if isinstance(entry, BuiltinCapability):
        return CapabilityGrant(entry_id=entry.entry_id, entry_sha256=entry.entry_sha256, kind="fa-builtin",
                               tools=entry.tools, vision=entry.vision)
    hosted = entry.execution == "hosted-stdio"
    if entry.execution == "foundry-toolbox":
        return CapabilityGrant(entry_id=entry.entry_id, entry_sha256=entry.entry_sha256, kind="mcp-toolbox",
                               tools=entry.tool_names, vision=False, toolbox=entry.config)
    return CapabilityGrant(entry_id=entry.entry_id, entry_sha256=entry.entry_sha256,
                           kind="mcp-hosted" if hosted else "mcp-api", tools=entry.tool_names, vision=False,
                           launch=entry.config if hosted else None)


def available_grants(catalog: ToolCatalog | None, builtins: tuple[str, ...] = ()) -> dict[str, CapabilityGrant]:
    """Current organization catalog plus the FA builtins the deployment enables."""
    result = {}
    for entry in () if catalog is None else catalog.entries:
        result[entry.entry_id] = _grant(entry)
    for name in builtins:
        if name not in BUILTIN_CAPABILITIES or name in result:
            raise Problem(503, "CAPABILITY_CONFIGURATION", "a configured builtin capability is unknown or duplicated")
        result[name] = _grant(BUILTIN_CAPABILITIES[name])
    tools = [tool for grant in result.values() for tool in grant.tools]
    if len(tools) != len(set(tools)):
        raise Problem(503, "CAPABILITY_CONFIGURATION", "two configured capabilities expose the same tool name")
    return result


def enabled_toolboxes(catalog: ToolCatalog | None, names: tuple[str, ...]) -> ToolCatalog | None:
    """Toolbox entries load only when the deployment names them; other entries are unchanged."""
    if catalog is None:
        if names:
            raise Problem(503, "CAPABILITY_CONFIGURATION", "a configured toolbox entry needs the organization catalog")
        return None
    toolboxes = {item.entry_id for item in catalog.entries if item.execution == "foundry-toolbox"}
    if len(set(names)) != len(names) or not set(names) <= toolboxes:
        raise Problem(503, "CAPABILITY_CONFIGURATION", "a configured toolbox entry is unknown or duplicated")
    if toolboxes <= set(names):
        return catalog
    return build_catalog([item for item in catalog.entries if item.entry_id not in toolboxes - set(names)])


def load_runtime_capabilities():
    """API startup: grants from the organization catalog and enabled FA builtins, plus the catalog itself."""
    directory = os.environ.get("FA_TOOL_CATALOG_DIR")
    builtins = tuple(item for item in os.environ.get("FA_CAPABILITY_BUILTINS", "").split(",") if item)
    toolboxes = tuple(item for item in os.environ.get("FA_CAPABILITY_TOOLBOX_ENTRIES", "").split(",") if item)
    catalog = enabled_toolboxes(None if not directory else load_catalog(Path(directory)), toolboxes)
    return MappingProxyType(available_grants(catalog, builtins)), catalog


def load_runtime_grants():
    """API startup: the organization catalog directory and enabled FA builtins; unset means none."""
    return load_runtime_capabilities()[0]


def runtime_grants(runtime) -> dict[str, CapabilityGrant]:
    result = dict(getattr(runtime, "capability_grants", None) or {})
    from .team_mcp_wire import team_mcp_settings
    enabled, _ = team_mcp_settings()
    team = ()
    if enabled:
        from .team_mcp import current_team_mcp_grants
        team = current_team_mcp_grants(runtime)
    # Team connectors are not selectable: each conversation captures them separately (team_connector_narrowing).
    for grant in team:
        if grant.entry_id in result:
            if grant.entry_id not in _TEAM_MCP_COLLISIONS_LOGGED:
                _TEAM_MCP_COLLISIONS_LOGGED.add(grant.entry_id)
                _log.warning("TEAM_MCP_GRANT_SKIPPED reason=entry_id_collision entry_id=%s", grant.entry_id)
            continue
        result[grant.entry_id] = grant
    return result


def runtime_catalog_entry(runtime, grant: CapabilityGrant) -> CatalogEntry | None:
    """The current catalog entry for a captured grant, only if its digest is still the captured one."""
    catalog = getattr(runtime, "capability_catalog", None)
    entry = None if catalog is None else catalog.entry(grant.entry_id)
    return entry if entry is not None and entry.entry_sha256 == grant.entry_sha256 else None


def secret_environment_name(name: str) -> str:
    return "FA_CAPABILITY_SECRET_" + name.upper().replace("-", "_")


def environment_secret(name: str) -> str | None:
    """Deploy-line Container App secrets are mapped to these variables; values never leave the API."""
    value = os.environ.get(secret_environment_name(name))
    return value if value else None


CAPABILITY_TOOL_GUIDANCE = {
    "CAPABILITY_NEEDS_AUTH": "The organization service credential is missing or rejected; tell the user it needs setup.",
    "CAPABILITY_UNAVAILABLE": "The service is unavailable or the outcome is unknown; do not claim a result.",
    "CAPABILITY_TOOL_UNAVAILABLE": "The service no longer offers this allowlisted tool; do not substitute another.",
    "CAPABILITY_SCHEMA_CHANGED": "The tool schema changed since this turn started; do not guess new arguments.",
    "CAPABILITY_SCHEMA_INVALID": "The tool schema is unusable; do not guess arguments.",
    "CAPABILITY_TOOL_DENIED": "This tool is not enabled for this turn.",
    "CAPABILITY_TOOL_FAILED": "The service refused the call; check the arguments against the schema.",
    "CAPABILITY_ARGUMENTS": "The arguments exceed their bound; send a smaller request.",
    "CAPABILITY_RESULT_INVALID": "The service result could not be verified; do not use it.",
    "CAPABILITY_CALL_CONFLICT": "This tool call was already made with different arguments; it was not resent.",
}
CAPABILITY_ERRORS = frozenset({*CAPABILITY_TOOL_GUIDANCE, "CAPABILITY_EXECUTION_DENIED"})
# A failed Foundry toolbox call whose text says plainly whether another call is safe (native_toolbox raises them,
# native HITL passes them on for a bound toolbox tool). A read-only tool is one whose MCP ``annotations`` declare
# ``readOnlyHint`` in the toolbox's tools/list; the hint only changes this text, never what is allowed.
TOOLBOX_NOT_RUN = "NATIVE_TOOLBOX_NOT_RUN"
TOOLBOX_READ_FAILED = "NATIVE_TOOLBOX_READ_FAILED"
# C5: a tool not marked read-only failed after its request may have reached the service (timeout, 5xx, dropped
# connection, unreadable reply). FA never sends it again; only the user decides after checking the service.
TOOLBOX_OUTCOME_UNKNOWN = "NATIVE_TOOLBOX_OUTCOME_UNKNOWN"
# C3: this Agent has no working connection for the toolbox source, so the toolbox ran nothing. The owner's Connect
# card carries any consent link; the model never does.
CONNECTOR_NOT_CONNECTED = "NATIVE_CONNECTOR_NOT_CONNECTED"
CONNECTOR_RECONNECT = "NATIVE_CONNECTOR_RECONNECT_REQUIRED"
# The toolbox could not list its tools when this turn started, so the turn runs without it (Neal 2026-10-10).
TOOLBOX_UNAVAILABLE = "NATIVE_TOOLBOX_UNAVAILABLE"
TOOLBOX_NOT_RAN = frozenset({TOOLBOX_NOT_RUN, CONNECTOR_NOT_CONNECTED, CONNECTOR_RECONNECT, TOOLBOX_UNAVAILABLE})
# C5: a connection is made only on the owner's Connect card, never around it.
CONNECT_ONLY_ON_THE_CARD = (
    "Do not use the VM browser, Playwright or the desktop to sign in to this service or to reach it another way; "
    "the owner connects it only from the Connect card in the workbench, in a conversation with this Agent.")
TOOLBOX_FAILURE_GUIDANCE = {
    CONNECTOR_NOT_CONNECTED: (
        "This service is not connected for this assistant yet, so nothing was sent and nothing changed. Tell the "
        "user it still needs to be connected; the owner connects it from the Connect card in the workbench. Do not "
        "call its tools again in this turn, and never ask for, invent or repeat a sign-in link. "
        + CONNECT_ONLY_ON_THE_CARD),
    CONNECTOR_RECONNECT: (
        "This assistant's connection to this service no longer works (its authorization was probably revoked at "
        "the service), so nothing was sent and nothing changed. Tell the user the connection has to be set up "
        "again; the owner sees where to check on the card in the workbench. Do not call its tools again in this "
        "turn, and never ask for, invent or repeat a sign-in link. " + CONNECT_ONLY_ON_THE_CARD),
    TOOLBOX_OUTCOME_UNKNOWN: (
        "The result of this call is unknown: it may or may not have taken effect, and it was not sent again. Do not "
        "call this tool again for the same action in this turn, and do not say it succeeded or failed. Tell the "
        "user the result is uncertain and was not resent, and ask them to check the service before deciding "
        "whether to do it again."),
    TOOLBOX_UNAVAILABLE: (
        "This tool source could not list its tools when this turn started, so it is unavailable for this turn and "
        "nothing was sent to it. This is not a permission refusal. Answer with what you can do without it and tell "
        "the user plainly that this source was not available this time; do not call its tools again in this turn "
        "and do not claim a result from it."),
    TOOLBOX_NOT_RUN: (
        "The toolbox rejected this call before running it, so nothing was executed and nothing changed. This is "
        "not a permission refusal. Check the arguments; you may make a new call. If it fails again, tell the user "
        "the tool is not working right now."),
    TOOLBOX_READ_FAILED: (
        "This call failed. The tool only reads data, so calling it again cannot change or duplicate anything; "
        "this is not a permission refusal. You may call it again, and do so when the user asks to retry. If it "
        "keeps failing, tell the user what could not be read."),
}
CAPABILITY_TOOL_GUIDANCE.update(TOOLBOX_FAILURE_GUIDANCE)
MAX_TOOL_DESCRIPTION_CHARS = 1024
MAX_TOOL_RESULT_CHARS = 64 * 1024
# One API capability route's external work is bounded in total; Hosted waits strictly longer, so a
# completed call is never reported to the model as unknown only because the client gave up first.
CAPABILITY_ROUTE_SECONDS = 30.0
CAPABILITY_CLIENT_SECONDS = 45.0


class McpToolSchema(WireModel):
    name: str
    description: Annotated[str, Field(max_length=MAX_TOOL_DESCRIPTION_CHARS)]
    input_schema: dict[str, JsonValue]
    input_schema_sha256: Digest


class McpToolList(WireModel):
    schema_version: Literal["fa-mcp-tool-list-v1"] = "fa-mcp-tool-list-v1"
    entry_id: str
    entry_sha256: Digest
    tools: tuple[McpToolSchema, ...]


class McpToolResult(WireModel):
    schema_version: Literal["fa-mcp-tool-result-v1"] = "fa-mcp-tool-result-v1"
    entry_id: str
    entry_sha256: Digest
    tool: str
    is_error: bool
    text: Annotated[str, Field(max_length=MAX_TOOL_RESULT_CHARS)]
    truncated: bool


def check_selectable(refs: tuple[CapabilityRef, ...], prompt_profile: str | None,
                     available: dict[str, CapabilityGrant]) -> tuple[CapabilityRef, ...]:
    """A human selection may only name current entries on a composable base profile."""
    try:
        validate_refs(refs)
    except ValueError:
        raise Problem(422, "CAPABILITY_SELECTION_INVALID", "capability references must be distinct and ordered") from None
    if refs and prompt_profile not in CAPABILITY_BASE_PROFILES:
        raise Problem(422, "CAPABILITY_BASE_UNSUPPORTED", "this instruction profile does not compose capabilities")
    if (any(ref.entry_id == DELEGATE_READER_ENTRY_ID for ref in refs)
            and not any(ref.entry_id == "fa-delegate-v1" for ref in refs)):
        raise Problem(422, "CAPABILITY_SELECTION_INVALID", "the child reader requires the original delegation capability")
    if (any(ref.entry_id == SKILL_RESOURCES_ENTRY_ID for ref in refs)
            and not skills_selected(refs)):
        raise Problem(422, "CAPABILITY_SELECTION_INVALID", "skill resources require the approved skill reader")
    if any(not base_supports(ref.entry_id, prompt_profile) for ref in refs):
        raise Problem(422, "CAPABILITY_BASE_UNSUPPORTED", "this capability needs a base profile that declares its tool")
    for ref in refs:
        current = available.get(ref.entry_id)
        if current is None or current.entry_sha256 != ref.entry_sha256:
            raise Problem(409, "CAPABILITY_SOURCES_CHANGED", "a selected capability is not in the current catalog",
                          capability=ref.entry_id)
    return refs


def capture(original: tuple[CapabilityRef, ...], head: tuple[CapabilityRef, ...], head_selection_id: str | None,
            available: dict[str, CapabilityGrant]) -> CapabilityCapture | None:
    """Effective set = original selection ∩ current head ∩ current catalog; it never grows."""
    if not original:
        return None
    kept = tuple(available[ref.entry_id] for ref in original
                 if ref in head and ref.entry_id in available and available[ref.entry_id].entry_sha256 == ref.entry_sha256)
    try:
        return CapabilityCapture(head_selection_id=head_selection_id, grants=kept)
    except ValueError:
        raise Problem(409, "CAPABILITY_SOURCES_CHANGED", "the selected capabilities no longer form a valid tool set") from None


def check_captured(original: tuple[CapabilityRef, ...], captured: CapabilityCapture | None,
                   head: tuple[CapabilityRef, ...] | None = None) -> CapabilityCapture | None:
    """Rebuild/history check from records only: grants stay within the original (and head) selections."""
    if captured is None:
        if original:
            raise Problem(409, "CAPABILITY_CAPTURE_INVALID", "a selection with capabilities needs its original capture")
        return None
    if not original:
        raise Problem(409, "CAPABILITY_CAPTURE_INVALID", "capabilities were captured without a selection naming them")
    if any(grant.ref not in original or head is not None and grant.ref not in head for grant in captured.grants):
        raise Problem(409, "CAPABILITY_CAPTURE_INVALID", "captured capabilities exceed the original human selection")
    return captured
