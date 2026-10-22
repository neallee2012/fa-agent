"""API-side capability routes: current schemas, calls for captured mcp-api grants, and a current-work check.

Only the FA API imports this module (it reaches the pinned ``mcp`` SDK through
``mcp_client``). Every call re-checks the original session's captured grant and
the current catalog digest, and resolves the secret on the server only. There is
no durable intent: a call is deduplicated in this process by (session, tool call
id) against a digest of its tool identity and arguments, and an unknown outcome
is returned as an explicit error for the model to decide on, never resent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Annotated, Literal

import anyio
from pydantic import Field, JsonValue

from .behavior_capabilities import (
    CAPABILITY_ROUTE_SECONDS, CapabilityGrant, McpToolList, McpToolResult, environment_secret, feature_tools,
    runtime_catalog_entry,
)
from .behavior_timing import phase_timing
from .behavior_wire import Command, Digest, WireModel
from .errors import Problem

MAX_CALL_ARGUMENT_BYTES = 16 * 1024
MAX_REMEMBERED_CALLS = 1024

logger = logging.getLogger(__name__)


class CapabilityUnavailable(WireModel):
    """A grant whose tools could not be listed this turn, so the native turn runs without it
    (README#mcp-api-list-unavailable). Its entry and a fixed reason class only; never the service's error text."""
    entry_id: str
    reason: Literal["upstream_auth", "service_error"]


class CapabilityCatalogResult(WireModel):
    schema_version: Literal["fa-capability-catalog-v1"] = "fa-capability-catalog-v1"
    entries: tuple[McpToolList, ...]
    unavailable: tuple[CapabilityUnavailable, ...] = Field(default=(), exclude_if=lambda value: not value)


class CapabilityCurrent(WireModel):
    """The route's session gate passed: ticket, binding, owner and job/attempt are still current."""
    schema_version: Literal["fa-capability-current-v1"] = "fa-capability-current-v1"
    current: Literal[True] = True


class CapabilityCall(Command):
    request_id: Literal["capability-call"]
    tool_call_id: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[\x21-\x7e]+$")]
    tool: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    arguments: dict[str, JsonValue]
    input_schema_sha256: Digest


def gateway_options(session) -> tuple[dict, bool]:
    """Model-gateway options from the original captured task only, never from the request body."""
    task = session.assignment.task.instruction
    capture = None if task is None else task.capabilities
    if capture is None or not capture.grants:
        return {}, False
    served = feature_tools(capture)
    tools = frozenset(name for grant in capture.grants for name in grant.tools if name not in served)
    options = {"capability_tools": tools} if tools else {}
    from .behavior_delegation_wire import CAPABILITY_ID
    if any(grant.entry_id == CAPABILITY_ID for grant in capture.grants):
        from .behavior_capability_prompt import session_needs
        try:
            session_needs(capture)
        except ValueError:
            raise Problem(403, "DELEGATION_DISABLED", "the captured fixed child configuration differs") from None
    if capture.vision:
        options["allow_images"] = True
    return options, capture.vision


def _api_grants(session) -> tuple[CapabilityGrant, ...]:
    task = session.assignment.task.instruction
    capture = None if task is None else task.capabilities
    return () if capture is None else tuple(grant for grant in capture.grants if grant.kind == "mcp-api")


def _entry(runtime, grant):
    entry = runtime_catalog_entry(runtime, grant)
    if entry is None:
        raise Problem(503, "CAPABILITY_UNAVAILABLE", "the captured capability is no longer in the current catalog",
                      capability=grant.entry_id)
    return entry


def _run(async_call):
    return anyio.run(async_call)


# A listing the service could not answer: its credential is missing or rejected, or it failed or timed out
# (mcp_client._problem). A missing allowlisted tool, an invalid schema or a denied execution still refuses.
_LIST_UNAVAILABLE = {"CAPABILITY_NEEDS_AUTH": "upstream_auth", "CAPABILITY_UNAVAILABLE": "service_error"}


def list_grant_tools(runtime, grants, *, seconds=CAPABILITY_ROUTE_SECONDS, degrade=False):
    """Current allowlisted schemas of already-scoped mcp-api grants; the caller owns the scope check.

    With ``degrade`` (the native route), a grant whose listing failed is named in ``unavailable`` and the others
    are still listed, so the turn runs without only that grant (README#mcp-api-list-unavailable).
    """
    from .mcp_client import list_tools
    deadline, lists, unavailable = time.monotonic() + seconds, [], []
    for grant in grants:
        entry = _entry(runtime, grant)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Problem(503, "CAPABILITY_UNAVAILABLE", "capability schemas were not read within the route bound",
                          capability=grant.entry_id)
        try:
            with phase_timing("capability_list_tools"):
                lists.append(_run(lambda: list_tools(entry, environment_secret, timeout=remaining)))
        except Problem as error:
            code = error.body["error"]["code"]
            if not degrade or code not in _LIST_UNAVAILABLE:
                raise
            # Entry and code only; never the service's answer or the secret.
            logger.warning("FA_CAPABILITY_LIST_UNAVAILABLE entry=%s code=%s", grant.entry_id, code)
            unavailable.append(CapabilityUnavailable(entry_id=grant.entry_id, reason=_LIST_UNAVAILABLE[code]))
    return CapabilityCatalogResult(entries=tuple(lists), unavailable=tuple(unavailable))


def capability_catalog(authority, session, command, principal, *, seconds=CAPABILITY_ROUTE_SECONDS):
    """Current allowlisted schemas for this turn's mcp-api grants (TOOL-02: read before calling)."""
    # The route's current_session just authorized this request; the recheck after list_tools stays.
    result = list_grant_tools(authority.runtime, _api_grants(session), seconds=seconds)
    authority.runtime.authorize(session.binding_id, principal, "execute")
    return result


class _Once:
    """Same-process deduplication of one (session, tool call id) to its first outcome.

    A repeat with the same content waits for or returns the original outcome; the same
    id with different content is refused. Only completed calls can be evicted at
    MAX_REMEMBERED_CALLS; a process restart still loses this non-durable cache.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.calls: OrderedDict = OrderedDict()

    def run(self, key, content, call):
        with self.lock:
            slot = self.calls.get(key)
            first = slot is None
            if first:
                while len(self.calls) >= MAX_REMEMBERED_CALLS:
                    completed = next((identity for identity, prior in self.calls.items()
                                      if prior["done"].is_set()), None)
                    if completed is None:
                        raise Problem(503, "CAPABILITY_UNAVAILABLE",
                                      "all retained calls are still in progress; the new call was not sent")
                    del self.calls[completed]
                slot = {"content": content, "done": threading.Event(), "result": None, "error": None}
                self.calls[key] = slot
            elif slot["content"] != content:
                raise Problem(409, "CAPABILITY_CALL_CONFLICT",
                              "this tool call id was already used with different content; it was not resent")
        if first:
            try:
                slot["result"] = call()
            except Problem as error:
                slot["error"] = error
            except Exception:
                slot["error"] = Problem(503, "CAPABILITY_UNAVAILABLE",
                                        "the capability call outcome is unknown; it was not resent")
            finally:
                slot["done"].set()
        else:
            slot["done"].wait()
        if slot["error"] is not None:
            raise slot["error"]
        return slot["result"]


_ONCE = _Once()


def capability_current(session, command, principal):
    """The route already passed the active session gate; nothing else is checked or written."""
    return CapabilityCurrent()


def call_grant_tool(runtime, grants, *, tool_call_id, tool, arguments, input_schema_sha256, key, log, once=_ONCE):
    """Call one tool of already-scoped mcp-api grants once per ``key``; the caller owns the scope check.

    Returns the unchecked result and its grant; the secret is resolved on the server only.
    """
    from .mcp_client import call_tool
    if len(json.dumps(arguments).encode("utf-8")) > MAX_CALL_ARGUMENT_BYTES:
        raise Problem(422, "CAPABILITY_ARGUMENTS", "tool arguments exceed their bound")
    grant = next((item for item in grants if tool in item.tools), None)
    if grant is None:
        raise Problem(403, "CAPABILITY_TOOL_DENIED", "the tool is not in this turn's captured capabilities")
    entry = _entry(runtime, grant)
    raw_tool = tool[len(grant.entry_id) + 1:]
    content = hashlib.sha256(json.dumps({
        "entry_id": grant.entry_id, "entry_sha256": grant.entry_sha256, "tool": tool,
        "arguments": arguments, "input_schema_sha256": input_schema_sha256,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()

    def call():
        outcome = "error"
        try:
            result = _run(lambda: call_tool(entry, environment_secret, raw_tool, arguments,
                                            expected_schema_sha256=input_schema_sha256,
                                            timeout=CAPABILITY_ROUTE_SECONDS))
            outcome = "tool_error" if result.is_error else "ok"
            return result
        except Problem as error:
            outcome = error.body["error"]["code"]
            raise
        finally:
            logger.warning("FA_CAPABILITY_CALL %s", json.dumps({
                **log, "tool_call_id": tool_call_id, "capability": grant.entry_id, "tool": raw_tool,
                "outcome": outcome, "counts_against_shared_budget": False,
            }, sort_keys=True))

    return once.run(key, content, call), grant


def checked_result(result, grant):
    if not isinstance(result, McpToolResult) or result.entry_sha256 != grant.entry_sha256:
        raise Problem(502, "CAPABILITY_RESULT_INVALID", "the capability result differs from its captured entry")
    return result


def capability_call(authority, session, command: CapabilityCall, principal, *, once=_ONCE):
    authority.runtime.authorize(session.binding_id, principal, "execute")
    result, grant = call_grant_tool(
        authority.runtime, _api_grants(session), tool_call_id=command.tool_call_id, tool=command.tool,
        arguments=command.arguments, input_schema_sha256=command.input_schema_sha256,
        key=(session.assignment.session_id, command.tool_call_id),
        log={"job_id": session.execution.job_id, "session_id": session.assignment.session_id}, once=once)
    authority.runtime.authorize(session.binding_id, principal, "execute")
    return checked_result(result, grant)
