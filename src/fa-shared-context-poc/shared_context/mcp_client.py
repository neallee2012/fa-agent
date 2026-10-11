"""API-side MCP client for catalog entries that carry a server-side secret.

Only the FA API imports this module (it needs the ``mcp`` SDK pinned in
``requirements-behavior.txt``); Hosted and SDK-independent DTOs never do. The
secret is resolved per call into the httpx client headers and never appears in
results, errors or logs produced here. Transport and protocol handling stay in
the official SDK; this module adds only the FA boundary: allowlisted tools,
bounded results, and distinct needs-auth / unavailable / tool errors.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
from collections.abc import Callable
from typing import Any

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import TextContent

from .behavior_capabilities import (
    MAX_TOOL_DESCRIPTION_CHARS, MAX_TOOL_RESULT_CHARS, McpToolList, McpToolResult, McpToolSchema,
)
from .errors import Problem
from .tool_catalog import CatalogEntry, resolve_headers

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_RESULT_CHARS = MAX_TOOL_RESULT_CHARS
MAX_DESCRIPTION_CHARS = MAX_TOOL_DESCRIPTION_CHARS
MAX_SCHEMA_BYTES = 16 * 1024


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _client(entry: CatalogEntry, headers: dict[str, str], timeout: float,
            transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    options: dict[str, Any] = {}
    if transport is not None:
        options["transport"] = transport
    else:
        options["verify"] = ssl.create_default_context(
            cafile=os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"))
    return httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout), trust_env=False,
                             follow_redirects=False, **options)


def _flatten(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [item for inner in error.exceptions for item in _flatten(inner)]
    return [error]


def _problem(entry: CatalogEntry, error: BaseException) -> Problem:
    for item in _flatten(error):
        if isinstance(item, Problem):
            return item
        if isinstance(item, httpx.HTTPStatusError) and item.response.status_code in (401, 403):
            return Problem(503, "CAPABILITY_NEEDS_AUTH", "the capability service rejected the configured credential",
                           capability=entry.entry_id)
    return Problem(503, "CAPABILITY_UNAVAILABLE", "the capability service is unavailable; the outcome is unknown",
                   capability=entry.entry_id)


async def _with_session(entry: CatalogEntry, lookup: Callable[[str], str | None],
                        action: Callable[[ClientSession], Any], *, timeout: float,
                        transport: httpx.AsyncBaseTransport | None):
    if entry.execution != "api-client":
        raise Problem(403, "CAPABILITY_EXECUTION_DENIED", "only API-client catalog entries are called by the API")
    headers = resolve_headers(entry, lookup)
    try:
        with anyio.fail_after(timeout):
            async with _client(entry, headers, timeout, transport) as client:
                async with streamable_http_client(entry.config.url, http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await action(session)
    except Problem:
        raise
    except Exception as error:
        raise _problem(entry, error) from None


def _schemas(entry: CatalogEntry, listed) -> tuple[McpToolSchema, ...]:
    offered = {tool.name: tool for tool in listed.tools}
    missing = [name for name in entry.tools if name not in offered]
    if missing:
        raise Problem(503, "CAPABILITY_TOOL_UNAVAILABLE", "the capability service no longer offers an allowlisted tool",
                      capability=entry.entry_id, tools=missing)
    result = []
    for name in entry.tools:
        schema = offered[name].inputSchema
        if not isinstance(schema, dict) or len(json.dumps(schema).encode("utf-8")) > MAX_SCHEMA_BYTES:
            raise Problem(502, "CAPABILITY_SCHEMA_INVALID", "an allowlisted tool schema is missing or exceeds its bound",
                          capability=entry.entry_id, tool=name)
        result.append(McpToolSchema(name=name, description=(offered[name].description or "")[:MAX_DESCRIPTION_CHARS],
                                    input_schema=schema, input_schema_sha256=_canonical_sha256(schema)))
    return tuple(result)


async def list_tools(entry: CatalogEntry, lookup: Callable[[str], str | None], *,
                     timeout: float = DEFAULT_TIMEOUT_SECONDS,
                     transport: httpx.AsyncBaseTransport | None = None) -> McpToolList:
    """Read the current schemas of the entry's allowlisted tools only (TOOL-02)."""
    async def action(session):
        return _schemas(entry, await session.list_tools())
    tools = await _with_session(entry, lookup, action, timeout=timeout, transport=transport)
    return McpToolList(entry_id=entry.entry_id, entry_sha256=entry.entry_sha256, tools=tools)


async def call_tool(entry: CatalogEntry, lookup: Callable[[str], str | None], tool: str,
                    arguments: dict[str, Any], *, expected_schema_sha256: str | None = None,
                    timeout: float = DEFAULT_TIMEOUT_SECONDS,
                    transport: httpx.AsyncBaseTransport | None = None) -> McpToolResult:
    """Call one allowlisted tool; an error result is returned as data so the model can correct its arguments."""
    if tool not in entry.tools:
        raise Problem(403, "CAPABILITY_TOOL_DENIED", "the tool is not in the catalog allowlist",
                      capability=entry.entry_id, tool=tool)
    if not isinstance(arguments, dict):
        raise Problem(422, "CAPABILITY_ARGUMENTS", "tool arguments must be a JSON object")

    async def action(session):
        if expected_schema_sha256 is not None:
            current = {item.name: item for item in _schemas(entry, await session.list_tools())}
            if current[tool].input_schema_sha256 != expected_schema_sha256:
                raise Problem(409, "CAPABILITY_SCHEMA_CHANGED", "the tool schema changed since this work captured it",
                              capability=entry.entry_id, tool=tool)
        try:
            return await session.call_tool(tool, arguments)
        except McpError:
            raise Problem(502, "CAPABILITY_TOOL_FAILED", "the capability service refused the tool call",
                          capability=entry.entry_id, tool=tool) from None

    outcome = await _with_session(entry, lookup, action, timeout=timeout, transport=transport)
    parts = [item.text for item in outcome.content if isinstance(item, TextContent)]
    if not parts and outcome.structuredContent is not None:
        parts = [json.dumps(outcome.structuredContent, ensure_ascii=False)]
    text = "\n".join(parts)
    return McpToolResult(entry_id=entry.entry_id, entry_sha256=entry.entry_sha256, tool=tool,
                         is_error=bool(outcome.isError), text=text[:MAX_RESULT_CHARS],
                         truncated=len(text) > MAX_RESULT_CHARS)
