"""Web PubSub push hints for the page (Neal 2026-10-06 08:28: fewer portal and API calls).

The bridge only relays the API's negotiate for the selected Agent. The returned client URL carries a short-lived
Web PubSub access token for the browser's own WebSocket; it is never stored, logged or journaled here. Events only
name what changed; the page then reads that one item through the ordinary routes, so a missed event is only slower.
"""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import AwareDatetime

from shared_context.behavior_wire import Identifier, WireModel

from .adapter import ConsoleError, require, strict_json

NEGOTIATE_PATH = "/work-policy/v1/realtime/negotiate"
NEGOTIATE_BYTES = 8192
# The page script ships with this line; the bridge flips it only when an endpoint is configured.
PAGE_MARKER = "const REALTIME_CONFIGURED=false;"
_HUB = re.compile(r"/client/hubs/[A-Za-z][A-Za-z0-9_]{0,127}")

# Same names as the API's negotiate refusals: DISABLED = push is not configured (stay on polling), UNAVAILABLE = no
# connection now (retry later). Both keep the page polling.
MESSAGES = {
    "REALTIME_DISABLED": "這個工作台沒有設定即時推送；照常定時更新。",
    "REALTIME_UNAVAILABLE": "目前無法建立即時推送連線；照常定時更新，稍後再試。",
    "REALTIME_INVALID": "即時推送的連線資訊無法核對；照常定時更新。",
}


class RealtimeNegotiate(WireModel):
    schema_version: Literal["fa-realtime-negotiate-v1"]
    url: str
    expires_at: AwareDatetime
    team_id: Identifier


def endpoint_host(value):
    """``FA_REALTIME_ENDPOINT``: ``https://<name>.webpubsub.azure.com`` only. Unset or anything else turns push off
    (the page keeps polling); an optional hint never stops the workbench from starting."""
    if not value:
        return None
    parsed = urlsplit(value)
    host = parsed.hostname
    if (parsed.scheme == "https" and host and parsed.netloc == host and parsed.path in {"", "/"}
            and not (parsed.query or parsed.fragment) and host.endswith(".webpubsub.azure.com")):
        return host
    return None


def page(html, host):
    require(html.count(PAGE_MARKER) == 1, "REALTIME_INVALID", 500)
    return html if host is None else html.replace(PAGE_MARKER, "const REALTIME_CONFIGURED=true;")


def negotiate(console, host):
    require(host is not None, "REALTIME_DISABLED", 404)
    console.active()
    raw, _ = console.adapter.send("GET", NEGOTIATE_PATH, None, console.vault.get(), limit=NEGOTIATE_BYTES)
    try:
        strict_json(raw)
        value = RealtimeNegotiate.model_validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError("REALTIME_INVALID", 502) from None
    url = urlsplit(value.url)
    query = parse_qs(url.query, keep_blank_values=True)
    # Only the configured service may receive the token; the page's CSP allows exactly that host.
    require(url.scheme == "wss" and url.hostname == host and url.netloc == host and _HUB.fullmatch(url.path)
            and not url.fragment and set(query) == {"access_token"} and len(query["access_token"]) == 1
            and query["access_token"][0], "REALTIME_INVALID", 502)
    return {"url": value.url, "expires_at": value.expires_at.isoformat(), "team_id": value.team_id}
