"""Offline helpers for the feed reader MCP server: synthetic feeds and keys only, never a real private feed.

A uniquely named module, not a conftest: tests/shared_context imports names from its own conftest, so a second
conftest anywhere under tests/ would shadow it when both directories are collected together. Test modules import
this module first (it puts the app on sys.path) and import the `keys` fixture from it."""

from __future__ import annotations

import sys
from pathlib import Path

import httpcore
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"))

from shared_context.feed_signature import (  # noqa: E402
    generate_keypair, load_public_key, load_signing_key, sign_feed_url,
)

TOKEN = "OFFLINEa1B2c3D4e5F6g7H8"
FEED_URL = f"https://feeds.example.test/rss/{TOKEN}"
PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def keys():
    private, public = generate_keypair()
    return load_signing_key(private), load_public_key(public)


def signed_headers(private_key, url=FEED_URL):
    return {"accept": "application/json, text/event-stream", "content-type": "application/json",
            "x-feed-url": url, "x-fa-feed-signature": sign_feed_url(private_key, url)}


def rss(items, *, title="Synthetic Daily") -> bytes:
    """items: dicts with title, link, guid, date, author, description, content (HTML)."""
    body = []
    for item in items:
        body.append(
            "<item>"
            f"<title>{item.get('title', '')}</title><link>{item.get('link', '')}</link>"
            f"<guid isPermaLink=\"false\">{item.get('guid', '')}</guid><pubDate>{item.get('date', '')}</pubDate>"
            f"<dc:creator>{item.get('author', '')}</dc:creator>"
            f"<description><![CDATA[{item.get('description', '')}]]></description>"
            f"<content:encoded><![CDATA[{item.get('content', '')}]]></content:encoded>"
            "</item>")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
            f"<title>{title}</title>{''.join(body)}</channel></rss>").encode("utf-8")


def http_response(status=200, body=b"", headers=None) -> bytes:
    headers = {"Content-Type": "application/rss+xml", "Content-Length": str(len(body)), "Connection": "close",
               **(headers or {})}
    lines = [f"HTTP/1.1 {status} X"] + [f"{name}: {value}" for name, value in headers.items() if value is not None]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


class _Stream(httpcore.MockStream):
    def __init__(self, buffer, sent):
        super().__init__(buffer)
        self.sent = sent

    def write(self, buffer, timeout=None):
        self.sent.append(buffer)


class FakeNetwork(httpcore.NetworkBackend):
    """address -> raw HTTP response (or a list, one per connection); records connections and request bytes."""

    def __init__(self, responses):
        self.responses, self.connected, self.sent = responses, [], []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append((host, port))
        response = self.responses.get(host)
        if isinstance(response, list):
            response = response.pop(0) if response else None
        if response is None:
            raise httpcore.ConnectError(f"refused {host}")
        return _Stream([response], self.sent)
