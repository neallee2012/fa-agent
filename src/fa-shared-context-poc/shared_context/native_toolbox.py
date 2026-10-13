"""N5: captured ``mcp-toolbox`` grants as C0 tools against the Foundry project toolbox.

The toolbox holds the credential on its project connection; the native turn reaches it with this Agent's own
managed identity (scope ``https://ai.azure.com/.default``), so no secret reaches the VM or the FA API. The
pinned SDK 1.0.13 HTTP MCP config only takes fixed headers, while the toolbox wants a fresh token on every
request, so this thin bridge follows the official Bring-Your-Own Responses toolbox sample: JSON-RPC POSTs with
``httpx`` (``initialize``, ``tools/list``, ``tools/call``) and the ``mcp-session-id`` it returns.

Like that sample (and the .NET ``AddFoundryToolboxes`` startup listing), one Hosted process lists a toolbox
version once and reuses its MCP session across turns; a bind only matches the captured grants against that list.
Toolbox versions are immutable, so the list is kept per exact version. A grant without a pinned version reads the
toolbox's current ``default_version`` (official toolbox GET) every turn and lists and calls that exact version; if
that read fails, the turn lists the consumer endpoint itself and keeps nothing, and its tools fail closed: each call
asks for the owner's approval (not offered without native HITL), since the version serving the turn is unknown. A
captured tool the list lacks is
listed once more before refusing, and a tool call that gets an MCP 404 on the session reopens it once (MCP
Streamable HTTP session management). The cache never holds a turn's token or call context.

Because FA's Hosted process learns the Team toolbox only from a turn's capture, it cannot list at startup; a prewarm
(README#typing-prewarm) names the Agent's toolbox versions while the owner types and the same listing runs then, with
no call id. A bind that finds that listing still running waits for it instead of listing the version again.

The original turn's platform call context (call id) is forwarded when the hosted task has one, as the official
sample does. The expected connections are custom keys or project/agent identity; per-user OAuth/Entra passthrough
is not exercised here.
FA keeps only what the toolbox does not do: the captured grant decides which tools exist, a tool whose
``require_approval`` is ``always`` waits for the Team owner's per-call approval in native HITL (or is refused when
HITL is off), and one SDK tool call runs at most once per process (only a tool call the server refused with a
session 404 is sent again, once, on a new session). A failed call tells the model whether a new call is safe: a tool
whose listed MCP ``annotations`` declare ``readOnlyHint`` may be called again, a call the toolbox refused before
running anything did not run, and an unknown outcome of any other tool (timeout, 5xx, dropped connection, unreadable
reply) is never resent: the model is told to say so and leave the decision to the user.

C3: a toolbox source this Agent has no working OAuth connection for (JSON-RPC -32006 with the toolbox's consent links,
or a revoked connection) is not a failed turn. With native HITL the owner gets a Connect card and the original step is
repeated once after its 「我連好了，繼續」: a listing lists that toolbox again before the model starts, a call sends the
same call id and arguments again. Otherwise, or when it still is not connected, only that toolbox's tools say so; the
other toolboxes and tools are bound as usual. The links go to the card's private record only, never to the model or a
log.

A toolbox whose tool list cannot be read at turn start (its source failed upstream, a 5xx, a timeout, an unreadable
answer) is left out of that turn only (Neal 2026-10-10): its tools report ``NATIVE_TOOLBOX_UNAVAILABLE``, the turn's
assembly record names the source for the workbench, and the next turn lists it again. The toolbox's own definite
HTTP or JSON-RPC answer about the request or its configuration (-32700, -32600, -32601, -32602, -32007) and every
capture check after a list (a missing tool, an approval setting, a schema) still refuse.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import itertools
import json
import logging
import os
import re
from collections import OrderedDict
from urllib.parse import quote, urlsplit

import httpx
from azure.core.exceptions import AzureError

from .behavior_capabilities import (
    CONNECT_ONLY_ON_THE_CARD, CONNECTOR_NOT_CONNECTED, CONNECTOR_RECONNECT, MAX_TOOL_DESCRIPTION_CHARS,
    MAX_TOOL_RESULT_CHARS, TOOLBOX_FAILURE_GUIDANCE, TOOLBOX_NOT_RUN, TOOLBOX_OUTCOME_UNKNOWN, TOOLBOX_READ_FAILED,
    TOOLBOX_UNAVAILABLE, McpToolResult,
)
from .behavior_timing import phase_timing, timing_id
from .connector_registry import CONNECTORS
from .behavior_native_turn_wire import RELAY_PREFIX
from .errors import Problem
from .m2_copilot import ToolFailure, ToolOutput, ToolSpec
from .native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider
from .native_capability_tools import REFUSED, UNKNOWN
from .native_hitl_wire import MAX_CONSENT_URLS, ConnectorConsent
from .team_connector_wire import connector_entry_id, connector_of

# The official sample sends this preview feature header on every toolbox request.
TOOLBOX_FEATURES = "Toolboxes=V1Preview"
MAX_REMEMBERED_CALLS = 1024
# Listed toolbox versions kept by one provider (one per Hosted process); the least recently bound is dropped.
MAX_TOOLBOXES = 32
_MAX_RESPONSE_BYTES = 512 * 1024
_MAX_SCHEMA_BYTES = 16 * 1024
_MAX_LIST_PAGES = 8
_LIST_SECONDS = 60.0
_CALL_SECONDS = 120.0
# A request the proxy rejected before running anything; any other error leaves the outcome unknown.
_DEFINITE_HTTP = frozenset({400, 401, 403, 404, 405, 406, 409, 413, 415, 422})
_DEFINITE_RPC = frozenset({-32700, -32600, -32601, -32602, -32006})
_log = logging.getLogger(__name__)


def project_endpoint() -> str:
    """The platform-injected project endpoint; never a request or model value."""
    value = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    address = urlsplit(value)
    if (address.scheme != "https" or not address.hostname or address.username or address.password
            or address.query or address.fragment or "/api/projects/" not in address.path):
        _log.warning("NATIVE_TOOLBOX_FAILED method=bind stage=endpoint code=NATIVE_EXECUTION_UNAVAILABLE reason=%s",
                     "endpoint_missing" if not value else "endpoint_invalid")
        raise Problem(503, "NATIVE_EXECUTION_UNAVAILABLE", "the platform project endpoint is required for a toolbox")
    return value


def _content_kind(content_type: str) -> str:
    """A fixed category of the response media type; the raw header value is never logged."""
    media = content_type.split(";")[0].strip().lower()
    return {"application/json": "json", "text/event-stream": "event_stream", "text/html": "html",
            "text/plain": "text", "": "none"}.get(media, "other")


# Neal 2026-10-10: a toolbox that cannot list its tools at turn start leaves only that toolbox out of the turn. Which
# failures do so, by stage: the identity token, the connection, an unreadable answer. A non-definite HTTP status (5xx,
# 429) and some JSON-RPC errors do too (_rpc_unavailable); the toolbox's own definite HTTP answer (401/403/404 ...)
# about this Agent or its configuration still refuses the turn.
_UNAVAILABLE_STAGES = {
    "token": "identity_unavailable", "request": "network", "response_headers": "response_invalid",
    "response_body": "response_invalid", "response_decode": "response_invalid", "tool_list": "response_invalid",
}
# An upstream authentication failure the toolbox reports in its JSON-RPC error text (stage 10-10: the WebIQ source's
# 401 auth_invalid_api_key came back as -32006). Only this class is kept; the text is never logged or stored.
_UPSTREAM_AUTH = re.compile(r"\b40[13]\b|unauthori[sz]ed|forbidden|invalid[_ ]api[_ ]key|auth_invalid", re.IGNORECASE)


def _fail(method, stage, code, reason, message, status=502, kind=Problem, unavailable=None) -> Problem:
    # Fixed method, stage and reason only; never the token, arguments, tool output or response content.
    _log.warning("NATIVE_TOOLBOX_FAILED method=%s stage=%s code=%s reason=%s", method, stage, code, reason)
    problem = kind(status, code, message)
    if unavailable is None:
        unavailable = (("service_error" if code == UNKNOWN else None) if stage == "response_status"
                       else _UNAVAILABLE_STAGES.get(stage))
    # Read only by a turn's listing (toolbox_provider.bind); a tool call keeps its own failure rules.
    problem.unavailable = unavailable
    return problem


class _SessionEnded(Problem):
    """MCP 404 to a request that carried the session: the server ended it before running the request."""


_CONSENT_RPC = -32006
# P0a (dev, 2026-10-10): once the owner revoked the provider's grant, the toolbox answered tools/list with -32007 and
# tools/call with -32602, both with this text and no consent link. Only the text tells it from a bad request.
_REVOKED_RPC = frozenset({-32007, -32602})
_REVOKED_TEXT = "Failed to extract exchanged token"
# INVALID_CONFIGURATION: without the revoked text it is the toolbox's definite answer about its configuration.
_INVALID_CONFIGURATION_RPC = -32007
_LINK = re.compile(r"https://[^\s\"'<>\\]+")


class _ConnectionNeeded(Problem):
    """The toolbox ran nothing: this Agent has no OAuth connection for one of its sources (``consent``, with the
    toolbox's own links) or the provider revoked it (``reconnect``). The links are only for the owner's card."""

    def __init__(self, need, links=()):
        super().__init__(403, REFUSED, "the toolbox needs this Agent's connection")
        self.need, self.links = need, links


def _texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


def _consent_links(message):
    """The consent links in a -32006 message, one per unconsented source. Foundry puts them in an ``errors`` JSON after
    a sentence (#746); the JSON is decoded first, so an escaped character never cuts a link short."""
    texts = list(_texts(message))
    if isinstance(message, str) and (start := message.find("{")) >= 0:
        try:
            decoded, end = json.JSONDecoder().raw_decode(message, start)
            texts = [message[:start], *_texts(decoded), message[end:]]
        except ValueError:
            pass
    links = []
    for text in texts:
        for link in _LINK.findall(text):
            link = link.rstrip(".,;)")
            if len(link) <= 4096 and link not in links:
                links.append(link)
    return tuple(links[:MAX_CONSENT_URLS])


def _source_errors(message):
    """The per-source ``errors`` list a -32006 carries after its sentence (#746); None when it has none."""
    decoded = message
    if isinstance(message, str) and (start := message.find("{")) >= 0:
        try:
            decoded, _ = json.JSONDecoder().raw_decode(message, start)
        except ValueError:
            return None
    errors = decoded.get("errors") if isinstance(decoded, dict) else None
    return errors if isinstance(errors, list) else None


def _rpc_unavailable(number, message):
    """Why a JSON-RPC error to a listing leaves only this toolbox out, or None when it refuses the turn. -32006 that is
    not a connection need means a source failed upstream; a non-definite server error (-32603 ...) does too. A definite
    answer about the request or the configuration (-32700, -32600, -32601, -32602, -32007) refuses."""
    if number != _CONSENT_RPC and (number in _DEFINITE_RPC or number == _INVALID_CONFIGURATION_RPC):
        return None
    return "upstream_auth" if any(_UPSTREAM_AUTH.search(text) for text in _texts(message)) else "source_error"


def _source_reasons(message):
    """{source name: reason} from the per-source ``errors`` list of a failed listing; None without a usable list. Only
    the reason class is kept, never the source's error text."""
    reasons = {}
    for item in _source_errors(message) or ():
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            auth = any(_UPSTREAM_AUTH.search(text) for text in _texts(item.get("error")))
            if auth or item["name"] not in reasons:
                reasons[item["name"]] = "upstream_auth" if auth else "source_error"
    return reasons or None


def _connection_needed(number, message):
    if number == _CONSENT_RPC:
        # -32006 also reports a source that failed for another reason (stage 10-10: an upstream 401). Only a consent
        # link, and in a per-source list a CONSENT_REQUIRED entry, makes it a connection this Agent can still make.
        links, errors = _consent_links(message), _source_errors(message)
        if links and (errors is None or any(
                isinstance(item, dict) and isinstance(item.get("error"), dict)
                and item["error"].get("code") == "CONSENT_REQUIRED" for item in errors)):
            return _ConnectionNeeded("consent", links)
        return None
    if number in _REVOKED_RPC and any(_REVOKED_TEXT in text for text in _texts(message)):
        return _ConnectionNeeded("reconnect")
    return None


def _read_only(grant, tool) -> bool:
    """Whether a failed call may be made again. A connector's registry entry decides, not the server's hint: Work IQ
    lists call_function as readOnlyHint, yet it runs an arbitrary function. Other toolboxes use MCP readOnlyHint."""
    connector = CONNECTORS.get(connector_of(grant.entry_id))
    if connector is not None:
        return grant.entry_id == connector_entry_id(connector.read.server_label)
    annotations = tool.get("annotations")
    return isinstance(annotations, dict) and annotations.get("readOnlyHint") is True


def _tool_note(grant, raw):
    """A connector entry's fixed usage note for one tool; other toolboxes have none."""
    connector = CONNECTORS.get(connector_of(grant.entry_id))
    entries = () if connector is None else connector.entries
    return next((entry.tool_notes.get(raw) for entry in entries
                 if entry.server_label == grant.toolbox.server_label), None)


def _source_name(grant):
    connector = CONNECTORS.get(connector_of(grant.entry_id))
    return grant.toolbox.server_label if connector is None else connector.display_name


class _Toolbox:
    """One toolbox URL's listed tools and MCP session in this process; never a turn's token or call context."""

    def __init__(self):
        self.listed = None
        self.session_id, self.ready = None, False
        # JSON-RPC ids stay unique within the shared session.
        self.ids = itertools.count(1)
        self._lock, self._loop = None, None
        # A prewarm's listing of this version while it runs; a bind waits for it instead of listing again.
        self.warming: asyncio.Task | None = None

    def opening(self) -> asyncio.Lock:
        """Serializes opening the shared session, so parallel tool calls never overwrite each other's session."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._lock, self._loop = asyncio.Lock(), loop
        return self._lock


class _Toolboxes:
    def __init__(self):
        self.items: OrderedDict = OrderedDict()

    def get(self, key) -> _Toolbox:
        item = self.items.get(key)
        if item is None:
            while len(self.items) >= MAX_TOOLBOXES:
                self.items.popitem(last=False)
            item = self.items[key] = _Toolbox()
        self.items.move_to_end(key)
        return item


class ToolboxClient:
    """One turn's view of a toolbox: this turn's token provider and call context over the shared session."""

    def __init__(self, url, token_provider, *, transport=None, platform_headers=None, toolbox=None):
        self.url, self.token_provider, self.transport = url, token_provider, transport
        # The original turn's platform context (call id), forwarded as the official BYO sample does.
        self.platform_headers = dict(platform_headers or {})
        self.call_context = "present" if self.platform_headers else "absent"
        self.toolbox = toolbox if toolbox is not None else _Toolbox()
        self.fresh = False

    def _fail(self, method, stage, code, reason, message, status=502, kind=Problem, unavailable=None) -> Problem:
        return _fail(method, stage, code, f"{reason};call_id={self.call_context}", message, status, kind, unavailable)

    async def _token(self, method):
        try:
            token = self.token_provider()
            if inspect.isawaitable(token):
                token = await token
        except (AzureError, OSError) as error:
            raise self._fail(method, "token", UNKNOWN, f"exception_type={type(error).__name__}",
                        "the Agent identity token is unavailable", 503) from None
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise self._fail(method, "token", UNKNOWN, "token_invalid", "the Agent identity token is unavailable", 503)
        return token

    async def _exchange(self, method, verb, url, headers, *, timeout, body=None):
        """One bounded HTTP exchange; returns (status, content type, raw body, response session id)."""
        try:
            # Same construction as the model call to this host (behavior_model_direct): the transport keeps httpx's
            # default trust_env for TLS trust (SSL_CERT_FILE/SSL_CERT_DIR); the client still ignores proxy settings.
            transport = self.transport or httpx.AsyncHTTPTransport(retries=0)
            async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout, connect=5),
                                         follow_redirects=False, trust_env=False) as client:
                async with client.stream(verb, url, json=body, headers=headers) as response:
                    status, content_type = response.status_code, response.headers.get("content-type", "")
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise self._fail(method, "response_headers", UNKNOWN, f"http_status={status};encoding_unsupported",
                                    "the toolbox response encoding is unsupported")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > _MAX_RESPONSE_BYTES:
                            raise self._fail(method, "response_body", UNKNOWN, f"http_status={status};body_over_bound",
                                        "the toolbox response exceeds its bound")
                        raw.extend(chunk)
                    return status, content_type, bytes(raw), response.headers.get("mcp-session-id")
        except httpx.HTTPError as error:
            raise self._fail(method, "request", UNKNOWN, f"exception_type={type(error).__name__}",
                        "the toolbox outcome is unconfirmed", 503) from None

    async def default_version(self, toolbox_url):
        """The toolbox's current ``default_version`` from the official toolbox read; never kept across turns."""
        method = "toolbox_get"
        token = await self._token(method)
        status, content_type, raw, _ = await self._exchange(
            method, "GET", toolbox_url, {"Authorization": "Bearer " + token, "Accept": "application/json",
                                         "Accept-Encoding": "identity"}, timeout=_LIST_SECONDS)
        if status != 200:
            code = REFUSED if status in _DEFINITE_HTTP else UNKNOWN
            raise self._fail(method, "response_status", code, f"http_status={status}",
                             "the toolbox was not read", 503 if code == UNKNOWN else 403)
        try:
            version = json.loads(raw).get("default_version")
        except (ValueError, AttributeError):
            version = None
        if not isinstance(version, str) or not version:
            raise self._fail(method, "response_decode", UNKNOWN,
                             f"http_status={status};content={_content_kind(content_type)};default_version_missing",
                             "the toolbox default version is unreadable")
        return version

    async def _post(self, method, params=None, *, timeout, session=None, notification=False):
        """One JSON-RPC POST on ``session`` (if any); returns its result and the session id the response names."""
        token = await self._token(method)
        headers = {**self.platform_headers, "Authorization": "Bearer " + token, "Content-Type": "application/json",
                   "Foundry-Features": TOOLBOX_FEATURES, "Accept-Encoding": "identity"}
        if session:
            headers["mcp-session-id"] = session
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            body["id"] = request_id = next(self.toolbox.ids)
        status, content_type, raw, opened = await self._exchange(method, "POST", self.url, headers, timeout=timeout,
                                                                 body=body)
        if status not in (200, 202, 204):
            if status == 404 and session and method not in ("initialize", "notifications/initialized"):
                # MCP Streamable HTTP: the server ended this session and ran nothing; the caller may reopen it.
                self._end(session)
                raise self._fail(method, "response_status", REFUSED, "http_status=404;session_ended",
                                 "the toolbox session ended", 403, _SessionEnded)
            code = REFUSED if status in _DEFINITE_HTTP else UNKNOWN
            raise self._fail(method, "response_status", code, f"http_status={status}",
                        "the toolbox did not serve the request", 503 if code == UNKNOWN else 403)
        if notification:
            return None, opened
        shape = f"http_status={status};content={_content_kind(content_type)};bytes={len(raw)}"
        message = _message(raw, content_type, request_id)
        if message is None:
            raise self._fail(method, "response_decode", UNKNOWN, shape + ";jsonrpc_invalid",
                        "the toolbox response is invalid")
        error = message.get("error")
        if error is not None:
            number = error.get("code") if isinstance(error, dict) else None
            code = REFUSED if number in _DEFINITE_RPC else UNKNOWN
            reason = f"rpc_code={number}" if type(number) is int else "rpc_code=invalid"
            needed = _connection_needed(number, error.get("message")) if isinstance(error, dict) else None
            if needed is not None:
                # Logged by code and need only: the message carries the consent links.
                self._fail(method, "rpc_error", REFUSED, f"{reason};need={needed.need}", "the toolbox needs a "
                           "connection", 403)
                raise needed
            text = error.get("message") if isinstance(error, dict) else None
            unavailable = _rpc_unavailable(number, text)
            problem = self._fail(method, "rpc_error", code, reason, "the toolbox returned an error",
                                 503 if code == UNKNOWN else 403, unavailable=unavailable)
            problem.unavailable_by_source = None if unavailable is None else _source_reasons(text)
            raise problem
        result = message.get("result")
        if not isinstance(result, dict):
            raise self._fail(method, "response_decode", UNKNOWN, shape + ";result_missing", "the toolbox returned no result")
        return result, opened

    def _end(self, sent):
        """The next request opens a new session, unless another call already replaced this one."""
        if self.toolbox.session_id == sent:
            self.toolbox.ready = False

    async def initialize(self):
        """Open a new MCP session; it is shared with later requests only after the initialized notification."""
        _, session = await self._post("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                     "clientInfo": {"name": "fa-native-toolbox", "version": "1"}},
                                      timeout=_LIST_SECONDS)
        await self._post("notifications/initialized", timeout=_LIST_SECONDS, session=session, notification=True)
        self.toolbox.session_id, self.toolbox.ready = session, True
        return session

    async def _session_post(self, method, params, *, timeout):
        """One request on the shared session: opened when needed, reopened once if the server ended it (404)."""
        box = self.toolbox
        for reopened in (False, True):
            async with box.opening():
                if not box.ready:
                    try:
                        await self.initialize()
                    except _ConnectionNeeded:
                        raise
                    except Problem:
                        # Nothing of this request was sent yet: a definite refusal, not an unknown outcome.
                        raise self._fail(method, "session", REFUSED, "session_unavailable",
                                         "the toolbox session could not be opened", 403) from None
                session = box.session_id
            try:
                result, _ = await self._post(method, params, timeout=timeout, session=session)
                return result
            except _SessionEnded:
                # Rejected before running anything, so the same request on a new session is not a resend.
                if reopened:
                    raise
            except Problem:
                self._end(session)
                raise

    async def tools(self, *, refresh=False):
        """This toolbox version's listed tools, listed on a new session when absent or refreshed."""
        box = self.toolbox
        if refresh or box.listed is None:
            async with box.opening():
                session = await self.initialize()
            box.listed, self.fresh = await self.list_tools(session), True
        return box.listed

    async def list_tools(self, session=None):
        tools, cursor = [], None
        for _ in range(_MAX_LIST_PAGES):
            result, _ = await self._post("tools/list", {} if cursor is None else {"cursor": cursor},
                                         timeout=_LIST_SECONDS, session=session)
            page = result.get("tools")
            if not isinstance(page, list):
                raise self._fail("tools/list", "tool_list", UNKNOWN, "tools_not_list", "the toolbox tool list is invalid")
            tools.extend(page)
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise self._fail("tools/list", "tool_list", UNKNOWN, "page_bound", "the toolbox tool list exceeds its page bound")

    async def call_tool(self, name, arguments, *, timeout):
        return await self._session_post("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)


def _message(raw: bytes, content_type: str, request_id: int) -> dict | None:
    """A plain JSON reply, or the matching JSON-RPC message of a streamable-HTTP event stream; None if neither."""
    try:
        if content_type.split(";")[0].strip().lower() == "text/event-stream":
            for event in raw.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
                data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
                if data:
                    value = json.loads(data)
                    if isinstance(value, dict) and value.get("id") == request_id:
                        return value
            return None
        value = json.loads(raw)
    except (UnicodeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("id") != request_id:
        return None
    return value

class _Calls:
    """Process-level once-per-call memory (non-durable, like the API broker's); never a resend of unknown."""

    def __init__(self):
        self.items: OrderedDict = OrderedDict()

    def claim(self, key, digest):
        remembered = self.items.get(key)
        if remembered is None:
            while len(self.items) >= MAX_REMEMBERED_CALLS:
                self.items.popitem(last=False)
            self.items[key] = (digest, None)
            return None
        if remembered[0] != digest:
            raise ToolFailure("This tool call id was already used with other arguments; do not reuse it.",
                              public_code=REFUSED)
        if isinstance(remembered[1], str) and remembered[1] not in _FAILED:
            return remembered[1]
        raise ToolFailure(TOOLBOX_FAILURE_GUIDANCE.get(
            remembered[1], "This tool call already ended without a confirmed result; do not resend it under another "
            "call."), public_code=remembered[1] or UNKNOWN)

    def settle(self, key, digest, outcome):
        self.items[key] = (digest, outcome)


_FAILED = frozenset({UNKNOWN, TOOLBOX_NOT_RUN, TOOLBOX_READ_FAILED, TOOLBOX_OUTCOME_UNKNOWN, CONNECTOR_NOT_CONNECTED,
                     CONNECTOR_RECONNECT})
_CALLS = _Calls()
# C3: (agent, conversation, toolbox) whose Connect card the owner declined. The durable record is the API's
# "consent-declined" marker (a new card is refused with NATIVE_HITL_HUMAN_DENIED); this process cache only saves a round trip.
_DECLINED: set[tuple[str, str, str]] = set()


def _relay_turn(receipt) -> bool:
    """A Teams relay turn, or the automatic continuation of one (its own RID is "resume-…")."""
    continuation = getattr(receipt, "continuation", None)
    return receipt.request_id.startswith(RELAY_PREFIX) or (
        continuation is not None and continuation.root_request_id.startswith(RELAY_PREFIX))


def _text(result: dict) -> str:
    parts = []
    for item in result.get("content") or ():
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif isinstance(item, dict) and item.get("type") == "resource":
            resource = item.get("resource")
            if isinstance(resource, dict) and isinstance(resource.get("text"), str):
                parts.append(resource["text"])
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(parts)


def _offered(listed, label):
    """Toolbox names are ``{label}___{tool}`` or ``{label}.{tool}``; other sources in the toolbox are ignored."""
    offered = {}
    for tool in listed:
        name = tool.get("name") if isinstance(tool, dict) else None
        for separator in ("___", "."):
            if isinstance(name, str) and name.startswith(label + separator):
                raw = name[len(label) + len(separator):]
                if raw in offered:
                    _log.warning("NATIVE_TOOLBOX_FAILED method=bind stage=tool_list code=CAPABILITY_UNAVAILABLE "
                                 "reason=duplicate_tool")
                    raise Problem(503, "CAPABILITY_UNAVAILABLE", "the toolbox offers one tool name twice",
                                  capability=label)
                offered[raw] = tool
                break
    return offered


def _schema(tool, grant):
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or len(json.dumps(schema).encode("utf-8")) > _MAX_SCHEMA_BYTES:
        _log.warning("NATIVE_TOOLBOX_FAILED method=bind stage=tool_schema code=CAPABILITY_SCHEMA_INVALID reason=%s",
                     "schema_missing" if not isinstance(schema, dict) else "schema_over_bound")
        raise Problem(502, "CAPABILITY_SCHEMA_INVALID", "a toolbox tool schema is missing or exceeds its bound",
                      capability=grant.entry_id)
    if schema.get("type", "object") == "object" and "properties" not in schema:
        schema = {**schema, "properties": {}}
    return schema


def _platform_headers() -> dict:
    """The original native turn's platform call context; empty outside a hosted request or task."""
    try:
        from azure.ai.agentserver.core import get_request_context
        context = get_request_context()
        return dict(context.platform_headers()) if context.call_id else {}
    except (ImportError, LookupError, RuntimeError, AttributeError):
        return {}


def _mcp_url(endpoint, name, version):
    base = f"{endpoint}/toolboxes/{quote(name, safe='')}"
    return base + ("" if version is None else f"/versions/{quote(version, safe='')}") + "/mcp?api-version=v1"


def _toolbox_url(endpoint, name):
    return f"{endpoint}/toolboxes/{quote(name, safe='')}?api-version=v1"


def toolbox_provider(*, transport=None) -> NativeCapabilityProvider:
    """The single native consumer of captured ``mcp-toolbox`` grants; one instance per Hosted process."""
    toolboxes = _Toolboxes()

    async def bind(context, grants):
        endpoint = project_endpoint()
        platform = _platform_headers()
        clients, specs, schemas = {}, [], {}
        receipt = context.receipt

        def client_for(name, version, toolbox=None):
            return ToolboxClient(_mcp_url(endpoint, name, version), context.model_token_provider, transport=transport,
                                 platform_headers=platform, toolbox=toolbox)

        async def read_default(name):
            try:
                return await client_for(name, None).default_version(_toolbox_url(endpoint, name))
            except Problem:
                return None

        async def connected(grant, needed, tool_call_id=None, call_digest=None):
            """C3: the owner's Connect card for this Agent's connection; True only on its one 「我連好了，繼續」."""
            entry = grant.toolbox
            asked = (receipt.agent_id, receipt.conversation_id, entry.toolbox)
            # No card where nobody can press it: no HITL, a non-human origin (Agent or schedule), a Teams relay turn
            # (request ids "tr-", see native_teams_relay), or a connector the owner declined in this conversation.
            if context.hitl is None or not receipt.human_origin or _relay_turn(receipt) or asked in _DECLINED:
                return False
            try:
                await context.hitl.connect(ConnectorConsent(
                    call=context.hitl.consent_call(tool_call_id), need=needed.need,
                    connector_id=connector_of(grant.entry_id), toolbox_name=entry.toolbox,
                    server_label=entry.server_label, step="tools/list" if tool_call_id is None else "tools/call",
                    original_call_digest=call_digest, consent_urls=needed.links))
            except Problem as error:
                if error.body["error"]["code"] == "NATIVE_HITL_HUMAN_DENIED":
                    _DECLINED.add(asked)
                return False
            return True

        async def list_failure(client):
            warming = client.toolbox.warming
            if (client.toolbox.listed is None and warming is not None and not warming.done()
                    and warming.get_loop() is asyncio.get_running_loop()):
                # A prewarm is listing this exact version now: wait for its list rather than listing it a second time.
                # Waiting never cancels it; if it fails, this turn lists the version itself below.
                with phase_timing("native_bind_toolbox_prewarm_wait", job_id=timing_id(receipt.request_id)):
                    await asyncio.wait((warming,))
            try:
                await client.tools()
            except Problem as error:
                return error
            return None

        # The default version can be promoted at any time and decides require_approval: read it every turn. Distinct
        # toolboxes are read, then listed, together.
        unpinned = list(dict.fromkeys(grant.toolbox.toolbox for grant in grants if grant.toolbox.version is None))
        current = dict(zip(unpinned, await asyncio.gather(*(read_default(name) for name in unpinned))))
        placed = []
        for grant in grants:
            entry = grant.toolbox
            version = entry.version if entry.version is not None else current[entry.toolbox]
            # Unread default: the version serving this turn's calls is unknown, so no listed setting is trusted.
            cache_key = (endpoint, entry.toolbox, version)
            if cache_key not in clients:
                # An immutable version (official toolbox versioning) keeps its list and require_approval; an unread
                # default lists the consumer endpoint for this turn only and keeps nothing for later turns.
                kept = None if version is None else toolboxes.get(cache_key)
                clients[cache_key] = client_for(entry.toolbox, version, kept)
            placed.append((grant, version is None, cache_key))
        failures = dict(zip(clients, await asyncio.gather(*(list_failure(client) for client in clients.values()))))
        unconnected = {key: failure for key, failure in failures.items() if isinstance(failure, _ConnectionNeeded)}
        if unconnected:
            first = {}
            for grant, _, cache_key in placed:
                first.setdefault(cache_key, grant)

            async def relist(cache_key, needed):
                # Before the model starts: after the owner's one click the same toolbox is listed once more.
                if not await connected(first[cache_key], needed):
                    return needed
                try:
                    await clients[cache_key].tools(refresh=True)
                except Problem as error:
                    return error
                return None
            failures.update(zip(unconnected, await asyncio.gather(
                *(relist(cache_key, needed) for cache_key, needed in unconnected.items()))))
        for grant, unread, cache_key in placed:
            entry, client = grant.toolbox, clients[cache_key]
            prefix = grant.entry_id + "-"
            raws = {name: name[len(prefix):] if name.startswith(prefix) else None for name in grant.tools}
            if isinstance(failures[cache_key], _ConnectionNeeded):
                # Still not connected: its tools only say so and send nothing; every other tool binds as usual.
                code = CONNECTOR_RECONNECT if failures[cache_key].need == "reconnect" else CONNECTOR_NOT_CONNECTED

                async def unconnected_tool(packet, code=code):
                    raise ToolFailure(TOOLBOX_FAILURE_GUIDANCE[code], public_code=code)
                for name in grant.tools:
                    if context.hitl is not None:
                        context.hitl.set_toolbox_approval(name, "never")
                    specs.append(ToolSpec(
                        name, f"Not connected for this assistant: {_source_name(grant)}. Calling it only reports that. "
                        + CONNECT_ONLY_ON_THE_CARD,
                        {"type": "object", "properties": {}}, unconnected_tool))
                    schemas[name] = McpToolResult
                continue
            if failures[cache_key] is None:
                # The version's shared list, including a refresh an earlier grant on it made; no request when listed.
                listed = await client.tools()
                offered = _offered(listed, entry.server_label)
                if not client.fresh and not set(raws.values()) <= offered.keys():
                    # A version can list nothing briefly after creation (official troubleshooting); list once more.
                    try:
                        listed = await client.tools(refresh=True)
                    except Problem as error:
                        # Fails like a first listing would: left out, or the turn refused, by the same rule below.
                        failures[cache_key] = error
                    else:
                        offered = _offered(listed, entry.server_label)
            if failures[cache_key] is not None:
                reason = getattr(failures[cache_key], "unavailable", None)
                if reason is None:
                    # A definite refusal is reported at its first grant, as when the grants were listed one by one.
                    raise failures[cache_key]
                # Its tool list could not be read: this turn runs without this toolbox (Neal 2026-10-10). Its tools stay
                # bound, as the capture and the resident SDK expect, and only report that; nothing is kept for the next
                # turn, which lists it again.
                by_source = getattr(failures[cache_key], "unavailable_by_source", None)
                if by_source is not None:
                    # The toolbox named which sources failed: a source it did not name is left out only alongside them,
                    # so it gets the neutral reason rather than theirs (stage 10-10: WebIQ's 401).
                    reason = by_source.get(entry.server_label, "source_error")
                source = _source_name(grant)
                # Several grants can share one source: the turn's record decides that it is named, and logged, once.
                if context.assembly is None or context.assembly.source_unavailable(grant.entry_id, source, reason):
                    _log.warning("FA_NATIVE_TOOL_SOURCE_UNAVAILABLE agent_id=%s request_id=%s entry=%s "
                                 "code=TOOLBOX_LIST_FAILED reason=%s", receipt.agent_id, receipt.request_id,
                                 grant.entry_id, reason)

                async def unavailable_tool(packet):
                    raise ToolFailure(TOOLBOX_FAILURE_GUIDANCE[TOOLBOX_UNAVAILABLE], public_code=TOOLBOX_UNAVAILABLE)
                for name in grant.tools:
                    if context.hitl is not None:
                        context.hitl.set_toolbox_approval(name, "never")
                    specs.append(ToolSpec(
                        name, f"Unavailable this turn: {source} could not list its tools. "
                        "Calling it only reports that.", {"type": "object", "properties": {}}, unavailable_tool))
                    schemas[name] = McpToolResult
                continue
            for name in grant.tools:
                raw = raws[name]
                tool = offered.get(raw)
                if tool is None:
                    # Counts only: which listed names were seen is configuration, but stays out of the log.
                    _log.warning("NATIVE_TOOLBOX_FAILED method=bind stage=tool_list code=CAPABILITY_UNAVAILABLE "
                                 "reason=captured_tool_missing;listed=%s;label_matches=%s",
                                 len(listed), len(offered))
                    raise Problem(503, "CAPABILITY_UNAVAILABLE", "the toolbox no longer offers a captured tool",
                                  capability=grant.entry_id)
                configuration = (tool.get("_meta") or {}).get("tool_configuration") or {}
                approval = configuration.get("require_approval")
                approval = "never" if approval is None else approval
                if unread and approval in ("always", "never"):
                    # Fail closed: another version may be promoted mid-turn behind the consumer endpoint, so every
                    # call asks the owner first; without native HITL the tool is not offered this turn.
                    approval = "always"
                if approval not in ("always", "never") or approval == "always" and context.hitl is None:
                    # The toolbox never blocks tools/call: an `always` tool needs this runtime's own per-call human
                    # approval (native HITL); without it, or with an unknown setting, the tool is not offered at all.
                    reason = ("approval_unknown" if approval not in ("always", "never") else
                              "default_version_unread" if unread else "approval_required")
                    _log.warning("NATIVE_TOOLBOX_FAILED method=bind stage=tool_list code=CAPABILITY_UNAVAILABLE "
                                 "reason=%s", reason)
                    raise Problem(503, "CAPABILITY_UNAVAILABLE",
                                  "the toolbox default version could not be read, so its tools need per-call approval"
                                  if reason == "default_version_unread" else "a captured toolbox tool requires approval",
                                  capability=grant.entry_id, reason=reason)
                if context.hitl is not None:
                    context.hitl.set_toolbox_approval(name, approval)
                timeout = _CALL_SECONDS if entry.timeout_ms is None else entry.timeout_ms / 1000
                # Only decides the failure text, never what is allowed.
                read_only = _read_only(grant, tool)

                async def handle(packet, client=client, listed=tool["name"], raw=raw, grant=grant, timeout=timeout,
                                 read_only=read_only):
                    arguments = packet["arguments"]
                    if not isinstance(arguments, dict):
                        raise ToolFailure("Tool arguments must be a JSON object.", public_code=REFUSED)
                    key = (context.scope.sdk_session_id, receipt.request_id, packet["tool_call_id"])
                    digest = hashlib.sha256(json.dumps([grant.entry_id, raw, arguments], sort_keys=True,
                                                       ensure_ascii=False).encode("utf-8")).hexdigest()
                    remembered = _CALLS.claim(key, digest)
                    if remembered is not None:
                        return ToolOutput(remembered)
                    resent = False
                    while True:
                        try:
                            result = await client.call_tool(listed, arguments, timeout=timeout)
                            break
                        except _ConnectionNeeded as needed:
                            if resent or not await connected(grant, needed, packet["tool_call_id"], digest):
                                code = CONNECTOR_RECONNECT if needed.need == "reconnect" else CONNECTOR_NOT_CONNECTED
                                _CALLS.settle(key, digest, code)
                                _log.warning("NATIVE_TOOLBOX_CALL_FAILED agent_id=%s request_id=%s tool_call_id=%s "
                                             "code=%s read_only=%s", receipt.agent_id, receipt.request_id,
                                             packet["tool_call_id"], code, str(read_only).lower())
                                raise ToolFailure(TOOLBOX_FAILURE_GUIDANCE[code], public_code=code) from None
                            # The owner's one 「我連好了，繼續」: the same call id and arguments, sent once more.
                            resent = True
                        except Problem as error:
                            code = error.body["error"]["code"]
                            code = code if code in (REFUSED, UNKNOWN) else UNKNOWN
                            # A read-only tool can be called again whatever happened; otherwise only a call that ran
                            # nothing (REFUSED) says so, and an unknown outcome is never resent: the user decides.
                            public_code = (TOOLBOX_READ_FAILED if read_only else TOOLBOX_NOT_RUN if code == REFUSED
                                           else TOOLBOX_OUTCOME_UNKNOWN)
                            _CALLS.settle(key, digest, public_code)
                            _log.warning("NATIVE_TOOLBOX_CALL_FAILED agent_id=%s request_id=%s tool_call_id=%s code=%s "
                                         "read_only=%s", receipt.agent_id, receipt.request_id, packet["tool_call_id"],
                                         code, str(read_only).lower())
                            raise ToolFailure(TOOLBOX_FAILURE_GUIDANCE[public_code], public_code=public_code) from None
                    text = _text(result)
                    output = McpToolResult(entry_id=grant.entry_id, entry_sha256=grant.entry_sha256, tool=raw,
                                           is_error=bool(result.get("isError")), text=text[:MAX_TOOL_RESULT_CHARS],
                                           truncated=len(text) > MAX_TOOL_RESULT_CHARS).model_dump_json()
                    _CALLS.settle(key, digest, output)
                    return ToolOutput(output)

                description = tool.get("description") if isinstance(tool.get("description"), str) else name
                note = _tool_note(grant, raw)
                note = "" if note is None else "\n" + note
                specs.append(ToolSpec(name, description[:MAX_TOOL_DESCRIPTION_CHARS - len(note)] + note,
                                      _schema(tool, grant), handle))
                schemas[name] = McpToolResult
        return NativeCapabilityBinding(tools=tuple(specs), result_schemas=schemas)
    async def warm(token_provider, entries):
        """Prewarm while the owner types (README#typing-prewarm): list each named version into the kept lists, as the
        official startup listing does (.NET ``AddFoundryToolboxes`` lists ``tools/list`` once at startup). It runs
        outside any turn, so it sends no call id (official: never reuse a call id across requests), registers no
        approval and calls no tool. A failure is only logged: the next turn's bind lists that version itself."""
        counts = dict.fromkeys(("listed", "kept", "listing", "failed"), 0)

        async def one(entry, endpoint):
            version = entry.version
            if version is None:
                # Like a turn: the current default version, read now; its list is kept under that exact version.
                version = await ToolboxClient(_mcp_url(endpoint, entry.toolbox, None), token_provider,
                                              transport=transport).default_version(_toolbox_url(endpoint, entry.toolbox))
            box = toolboxes.get((endpoint, entry.toolbox, version))
            if box.listed is not None:
                return "kept"
            if box.warming is not None and not box.warming.done():
                return "listing"
            client = ToolboxClient(_mcp_url(endpoint, entry.toolbox, version), token_provider, transport=transport,
                                   toolbox=box)
            box.warming = asyncio.ensure_future(client.tools())
            await box.warming
            return "listed"

        with phase_timing("native_prewarm_toolbox"):
            try:
                endpoint = project_endpoint()
                outcomes = await asyncio.gather(*(one(entry, endpoint) for entry in entries), return_exceptions=True)
            except Problem:
                outcomes = ["failed"] * len(entries)
            for outcome in outcomes:
                counts[outcome if isinstance(outcome, str) else "failed"] += 1
                if isinstance(outcome, BaseException) and not isinstance(outcome, (Problem, asyncio.CancelledError)):
                    _log.warning("FA_NATIVE_PREWARM_FAILED exception_type=%s", type(outcome).__name__)
        # Counts only; which toolboxes were listed is configuration and stays out of the log.
        _log.warning("FA_NATIVE_PREWARM toolboxes=%s listed=%s kept=%s listing=%s failed=%s", len(entries),
                     counts["listed"], counts["kept"], counts["listing"], counts["failed"])

    return NativeCapabilityProvider(accepts=lambda grant: grant.kind == "mcp-toolbox", bind=bind, name="toolbox",
                                    warm=warm)
