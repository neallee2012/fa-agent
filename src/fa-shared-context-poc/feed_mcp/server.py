"""Read-only feed reader MCP server (stateless streamable HTTP) for FA Team toolboxes.

Foundry injects the project connection's ``x-feed-url`` and ``x-fa-feed-signature`` headers into every call to this
server; the agent and model never see them. A request without a signature from the FA API's key is refused before MCP
handles it, and each tool verifies it again before fetching. Tools return JSON text bounded well under FA's 64K
tool-result limit; neither results, errors nor logs carry the feed URL.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Annotated

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.datastructures import Headers

from shared_context.feed_signature import FEED_SIGNATURE_HEADER, FEED_URL_HEADER, verify_feed_url

from .fetch import FeedError, fetch_feed
from .parse import Feed, Scrubber, pages, parse_feed

MCP_PATH = "/mcp"
LIST_DEFAULT, LIST_MAX = 10, 50
# Items are added while the serialized list stays within this many characters (FA keeps 64K per tool result).
LIST_RESULT_CHARS = 48_000
CACHE_SECONDS = 120.0
CACHE_ENTRIES = 8
# Both tools only read the feed (MCP ToolAnnotations); FA tells the model a failed read may be called again.
READ_ONLY = ToolAnnotations(readOnlyHint=True)
MESSAGES = {
    "FEED_MCP_UNAUTHORIZED": "這個連線沒有 FA 簽過的 feed 網址；請在設定頁用「建立新的 connection」、標頭填 x-feed-url 重新建立。",
    "FEED_FETCH_FAILED": "連不到 feed 網站或逾時；可以稍後再試一次。",
    "FEED_HTTP_STATUS": "feed 網站回了錯誤狀態；401／403／404 通常表示訂閱過期或網址已失效。",
    "FEED_URL_BLOCKED": "feed 網址或轉址目標不是 443 埠的公開 https 位址，已拒絕。",
    "FEED_REDIRECTS": "feed 網址轉址太多次，已停止。",
    "FEED_TOO_LARGE": "feed 超過 8 MB 上限，沒有讀取。",
    "FEED_NOT_RSS": "這個網址回的不是 RSS／Atom feed。",
    "FEED_FORMAT": "feed 內容格式無法解析。",
    "ITEM_NOT_FOUND": "feed 裡找不到這個 item_id；文章可能已從 feed 移除，請重新列出。",
    "PAGE_OUT_OF_RANGE": "沒有這一頁；請看 pages 欄位。",
}
_log = logging.getLogger(__name__)


def _fail(code: str, status: int | None = None):
    body = {"error": code, "message": MESSAGES[code]}
    if status is not None:
        body["status"] = status
    raise ToolError(json.dumps(body, ensure_ascii=False))


def signed_url(headers: Headers, public_key) -> str | None:
    """The one signed feed URL of a request, or None."""
    urls, signatures = headers.getlist(FEED_URL_HEADER), headers.getlist(FEED_SIGNATURE_HEADER)
    if len(urls) != 1 or len(signatures) != 1 or not verify_feed_url(public_key, urls[0], signatures[0]):
        return None
    return urls[0]


class _SignatureGate:
    """ASGI middleware: /healthz answers; /mcp needs a valid signature; nothing else is served."""

    def __init__(self, app, public_key):
        self.app, self.public_key = app, public_key

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path")
            if path == "/healthz":
                return await _reply(send, 200, b'{"status":"ok"}')
            if path != MCP_PATH:
                return await _reply(send, 404, b'{"error":"NOT_FOUND"}')
            if signed_url(Headers(scope=scope), self.public_key) is None:
                _log.info("FEED_MCP_REFUSED reason=signature")
                return await _reply(send, 401, json.dumps({"error": "FEED_MCP_UNAUTHORIZED",
                                                           "message": MESSAGES["FEED_MCP_UNAUTHORIZED"]},
                                                          ensure_ascii=False).encode("utf-8"))
        await self.app(scope, receive, send)


async def _reply(send, status: int, body: bytes):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


class FeedCache:
    """A short in-process cache so a list and the following page reads fetch the feed once (one replica)."""

    def __init__(self, fetch, clock):
        self.fetch, self.clock = fetch, clock
        self._items: dict[bytes, tuple[float, Feed]] = {}
        self._lock = threading.Lock()

    def load(self, url: str) -> Feed:
        key, now = hashlib.sha256(url.encode("ascii")).digest(), self.clock()
        with self._lock:
            cached = self._items.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
        feed = parse_feed(self.fetch(url), Scrubber(url))
        with self._lock:
            for stale in [name for name, (expires, _) in self._items.items() if expires <= now]:
                del self._items[stale]
            while len(self._items) >= CACHE_ENTRIES:
                del self._items[min(self._items, key=lambda name: self._items[name][0])]
            self._items[key] = (now + CACHE_SECONDS, feed)
        return feed


def create_app(public_key, *, fetch=fetch_feed, clock=time.monotonic):
    cache = FeedCache(fetch, clock)
    # Called by Foundry over the public ingress: the caller check is the signature, not the Host header.
    server = FastMCP("fa-feed-reader", stateless_http=True, json_response=True, log_level="WARNING",
                     streamable_http_path=MCP_PATH,
                     transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    async def load(ctx: Context, tool: str) -> Feed:
        request = ctx.request_context.request
        url = None if request is None else signed_url(request.headers, public_key)
        if url is None:
            _fail("FEED_MCP_UNAUTHORIZED")
        started = time.monotonic()
        try:
            feed = await anyio.to_thread.run_sync(cache.load, url)
        except FeedError as error:
            _log.info("FEED_MCP_CALL tool=%s outcome=%s ms=%d", tool, error.code, (time.monotonic() - started) * 1000)
            _fail(error.code, error.status)
        _log.info("FEED_MCP_CALL tool=%s outcome=ok items=%d ms=%d", tool, len(feed.items),
                  (time.monotonic() - started) * 1000)
        return feed

    @server.tool(name="list_items", structured_output=False, annotations=READ_ONLY,
                 description="List the newest items of the connected private feed (newest first): item_id, title, "
                             "published, author, link, a short summary and the full-text length. Use read_item with "
                             "an item_id to read the full text; when next_offset is set, call again with that offset.")
    async def list_items(ctx: Context, limit: Annotated[int, Field(ge=1, le=LIST_MAX)] = LIST_DEFAULT,
                         offset: Annotated[int, Field(ge=0, le=10_000)] = 0) -> str:
        feed = await load(ctx, "list_items")
        entries, used = [], 0
        for item in feed.items[offset:offset + limit]:
            entry = {"item_id": item.item_id, "title": item.title, "published": item.published,
                     "author": item.author, "link": item.link, "summary": item.summary, "chars": len(item.text)}
            size = len(json.dumps(entry, ensure_ascii=False)) + 2
            # The serialized reply is what FA bounds; stop before it, and next_offset continues from here.
            if entries and used + size > LIST_RESULT_CHARS:
                break
            entries.append(entry)
            used += size
        end = offset + len(entries)
        result = {"feed_title": feed.title, "total": len(feed.items), "offset": offset,
                  "next_offset": end if end < len(feed.items) else None, "items": entries}
        return json.dumps(result, ensure_ascii=False)

    @server.tool(name="read_item", structured_output=False, annotations=READ_ONLY,
                 description="Read the full text of one feed item as clean plain text, one page at a time "
                             "(page 1 first; the reply says how many pages there are).")
    async def read_item(ctx: Context, item_id: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")],
                        page: Annotated[int, Field(ge=1, le=1000)] = 1) -> str:
        feed = await load(ctx, "read_item")
        item = next((entry for entry in feed.items if entry.item_id == item_id), None)
        if item is None:
            _fail("ITEM_NOT_FOUND")
        parts = pages(item.text)
        if page > len(parts):
            _fail("PAGE_OUT_OF_RANGE")
        return json.dumps({"item_id": item.item_id, "title": item.title, "published": item.published,
                           "author": item.author, "link": item.link, "page": page, "pages": len(parts),
                           "text": parts[page - 1]}, ensure_ascii=False)

    return _SignatureGate(server.streamable_http_app(), public_key)
