"""The feed reader MCP server end to end over its ASGI app, with a fake fetch (synthetic feeds only)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from feed_fixtures import FEED_URL, TOKEN, keys, rss, signed_headers  # noqa: F401  (first: puts the app on sys.path)
from feed_mcp.fetch import FeedError
from feed_mcp.parse import READ_PAGE_CHARS, SUMMARY_CHARS, Scrubber, parse_feed, safe_link
from feed_mcp.server import create_app
from shared_context.feed_signature import generate_keypair, load_signing_key

APP_ROOT = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"


def synthetic_feed(count=50, words=40):
    """Shaped like the measured real feed (about 50 items, full text in content:encoded); content is synthetic."""
    items = []
    for n in range(count):
        paragraphs = "".join(f"<p>Paragraph {p} of item {n}: " + "lorem ipsum " * words + "</p>" for p in range(30))
        items.append({"title": f"Item {n} &amp; more", "link": f"https://news.example.test/2026/item-{n}/",
                      "guid": f"guid-{n}", "date": f"Mon, {1 + n % 28:02d} Sep 2026 {n % 24:02d}:00:00 GMT",
                      "author": "Synthetic Author", "description": "<p>Short summary " + "word " * 200 + "</p>",
                      "content": paragraphs + f"<p>Manage your feed at {FEED_URL}</p><script>alert(1)</script>"})
    return rss(items)


class Calls:
    def __init__(self, data):
        self.data, self.urls = data, []

    def __call__(self, url):
        self.urls.append(url)
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


def client(keys, data, clock=None):
    fetch = Calls(data)
    kwargs = {} if clock is None else {"clock": clock}
    return TestClient(create_app(keys[1], fetch=fetch, **kwargs)), fetch


def call(c, headers, name, arguments=None, request_id=1):
    response = c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                                                      "params": {"name": name, "arguments": arguments or {}}})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return result["isError"], result["content"][0]["text"]


def test_only_a_request_signed_for_its_exact_url_reaches_mcp(keys):
    other, _ = generate_keypair()
    c, fetch = client(keys, synthetic_feed(2))
    good = signed_headers(keys[0])
    refused = [
        {k: v for k, v in good.items() if k != "x-fa-feed-signature"},
        {k: v for k, v in good.items() if k != "x-feed-url"},
        signed_headers(load_signing_key(other)),
        {**good, "x-feed-url": FEED_URL + "x"},
        {**good, "x-feed-url": "http://feeds.example.test/rss"},
    ]
    with c:
        assert c.get("/healthz").status_code == 200 and c.get("/other").status_code == 404
        for headers in refused:
            response = c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert response.status_code == 401 and response.json()["error"] == "FEED_MCP_UNAUTHORIZED"
        duplicated = c.post("/mcp", headers=[*good.items(), ("x-feed-url", FEED_URL)],
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert duplicated.status_code == 401
        listed = c.post("/mcp", headers=good, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
        assert sorted(tool["name"] for tool in listed["result"]["tools"]) == ["list_items", "read_item"]
    assert fetch.urls == []


def test_both_tools_declare_the_official_read_only_annotation(keys):
    """Foundry toolbox lists upstream MCP annotations unchanged; FA tells the model a failed read may be retried."""
    c, fetch = client(keys, synthetic_feed(2))
    with c:
        listed = c.post("/mcp", headers=signed_headers(keys[0]),
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    assert {tool["name"]: tool["annotations"]["readOnlyHint"] for tool in listed["result"]["tools"]} == {
        "list_items": True, "read_item": True}
    assert fetch.urls == []


def test_list_items_is_newest_first_bounded_and_fetches_once_for_following_reads(keys, caplog):
    caplog.set_level(logging.DEBUG)
    c, fetch = client(keys, synthetic_feed())
    headers = signed_headers(keys[0])
    with c:
        error, text = call(c, headers, "list_items", {"limit": 10})
        assert not error
        first = json.loads(text)
        assert first["total"] == 50 and first["next_offset"] == 10 and len(first["items"]) == 10
        dates = [item["published"] for item in first["items"]]
        assert dates == sorted(dates, reverse=True)
        assert all(len(item["summary"]) <= SUMMARY_CHARS and item["chars"] > READ_PAGE_CHARS for item in first["items"])
        assert first["items"][0]["title"].startswith("Item ") and "&amp;" not in first["items"][0]["title"]
        error, text = call(c, headers, "list_items", {"limit": 50}, 2)
        everything = json.loads(text)
        assert not error and len(text) < 64 * 1024 and everything["next_offset"] is None
        assert [item["item_id"] for item in everything["items"][:10]] == [item["item_id"] for item in first["items"]]
        error, text = call(c, headers, "list_items", {"limit": 999}, 3)
        assert error
    assert fetch.urls == [FEED_URL], "the reads within the cache window fetch the feed once"
    assert TOKEN not in caplog.text and FEED_URL not in caplog.text


def test_read_item_pages_the_full_text_and_never_returns_the_feed_token(keys):
    c, _ = client(keys, synthetic_feed(3))
    headers = signed_headers(keys[0])
    with c:
        item = json.loads(call(c, headers, "list_items")[1])["items"][0]
        pages, page, texts = None, 1, []
        while pages is None or page <= pages:
            error, text = call(c, headers, "read_item", {"item_id": item["item_id"], "page": page}, page + 10)
            assert not error and len(text) < 64 * 1024
            value = json.loads(text)
            pages = value["pages"]
            texts.append(value["text"])
            assert len(value["text"]) <= READ_PAGE_CHARS
            page += 1
        assert pages > 1 and item["chars"] == sum(len(part) for part in texts)
        full = "".join(texts)
        assert full.startswith("Paragraph 0 of item") and "alert(1)" not in full and "<p>" not in full
        assert TOKEN not in full and "Manage your feed at [已移除]" in full
        error, text = call(c, headers, "read_item", {"item_id": item["item_id"], "page": pages + 1}, 99)
        assert error and "PAGE_OUT_OF_RANGE" in text
        error, text = call(c, headers, "read_item", {"item_id": "0" * 16}, 98)
        assert error and "ITEM_NOT_FOUND" in text


@pytest.mark.parametrize("code,status", [("FEED_HTTP_STATUS", 403), ("FEED_FETCH_FAILED", None),
                                         ("FEED_TOO_LARGE", None), ("FEED_URL_BLOCKED", None)])
def test_fetch_failures_are_fixed_codes_without_the_url(keys, caplog, code, status):
    caplog.set_level(logging.DEBUG)
    c, _ = client(keys, FeedError(code, status))
    with c:
        error, text = call(c, signed_headers(keys[0]), "list_items")
    assert error and code in text and TOKEN not in text and "feeds.example.test" not in text
    assert (str(status) in text) if status else True
    assert TOKEN not in caplog.text


def test_item_links_lose_tokens_and_query_strings():
    scrubber = Scrubber(FEED_URL + "?member=OFFLINEm3mb3rK3y99")
    assert safe_link("https://news.example.test/2026/annual-review/?utm=1#top", scrubber) == \
        "https://news.example.test/2026/annual-review/"
    assert safe_link(f"https://news.example.test/read/{TOKEN}/post", scrubber) == "https://news.example.test/"
    assert safe_link("https://news.example.test/p/Xy7Kq2Lm9Pz4Wc", scrubber) == "https://news.example.test/"
    assert safe_link("https://u:p@news.example.test/a", scrubber) == "https://news.example.test/a"
    assert safe_link(f"https://{TOKEN.lower()}.example.test/a", Scrubber(f"https://{TOKEN.lower()}.example.test/rss")) is None
    assert safe_link("javascript:alert(1)", scrubber) is None
    assert scrubber.text("member OFFLINEm3mb3rK3y99 and " + TOKEN) == "member [已移除] and [已移除]"


def test_atom_and_rss1_feeds_are_read():
    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Daily</title>
    <entry><id>urn:a:1</id><title type="html">A &lt;b&gt;bold&lt;/b&gt; title</title>
    <link rel="alternate" href="https://news.example.test/a/1"/><updated>2026-10-01T08:00:00Z</updated>
    <author><name>Writer</name></author><content type="html">&lt;p&gt;Atom body&lt;/p&gt;</content></entry>
    <entry><id>urn:a:2</id><title>Second</title><published>2026-10-02T08:00:00+08:00</published>
    <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>X one</p><p>X two</p></div></content></entry>
    </feed>"""
    feed = parse_feed(atom, Scrubber(FEED_URL))
    assert feed.title == "Atom Daily" and [item.title for item in feed.items] == ["Second", "A bold title"]
    assert feed.items[0].text == "X one\n\nX two" and feed.items[1].text == "Atom body"
    assert feed.items[1].author == "Writer" and feed.items[1].link == "https://news.example.test/a/1"
    rdf = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel rdf:about="x"><title>R1</title>
    </channel><item rdf:about="https://news.example.test/r/1"><title>R one</title><link>https://news.example.test/r/1</link>
    <description>Plain</description><dc:date>2026-10-03T00:00:00Z</dc:date></item></rdf:RDF>"""
    feed = parse_feed(rdf, Scrubber(FEED_URL))
    assert feed.title == "R1" and feed.items[0].title == "R one" and feed.items[0].text == "Plain"


@pytest.mark.parametrize("data,code", [
    (b"<html><body>not a feed</body></html>", "FEED_NOT_RSS"),
    (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel/></rss>', "FEED_FORMAT"),
    (b"<rss><channel><item>", "FEED_FORMAT"),
])
def test_non_feeds_and_entity_declarations_are_refused(data, code):
    with pytest.raises(FeedError) as caught:
        parse_feed(data, Scrubber(FEED_URL))
    assert caught.value.code == code


def test_every_returned_field_is_bounded_even_for_an_oversized_feed(keys):
    from feed_mcp.parse import AUTHOR_CHARS, LINK_CHARS, TITLE_CHARS
    huge = [{"title": "T" * 5000, "author": "A" * 5000, "guid": f"g{n}", "description": "d " * 5000,
             "link": "https://news.example.test/" + "/".join(["section"] * 400), "content": "<p>x</p>"}
            for n in range(60)]
    feed = parse_feed(rss(huge, title="F" * 5000), Scrubber(FEED_URL))
    assert len(feed.title) == TITLE_CHARS and feed.title.endswith("…")
    item = feed.items[0]
    assert len(item.title) == TITLE_CHARS and len(item.author) == AUTHOR_CHARS and len(item.summary) == SUMMARY_CHARS
    assert item.link == "https://news.example.test/" and LINK_CHARS < 400 * len("section/")
    c, _ = client(keys, rss(huge, title="F" * 5000))
    with c:
        error, text = call(c, signed_headers(keys[0]), "list_items", {"limit": 50})
    assert not error and len(text) < 64 * 1024


def test_list_items_stops_by_serialized_size_and_continues_with_next_offset(keys):
    """Review case: every field at its cap, titles of quotes (escaped twice) and links just under 512 kept as is."""
    from feed_mcp.server import LIST_RESULT_CHARS
    link = "https://news.example.test/" + "/".join(["section"] * 60)
    assert len(link) <= 512
    worst = [{"title": '"' * 400, "author": "A" * 400, "guid": f"g{n}", "description": "\\" * 400, "link": link,
              "content": "<p>x</p>"} for n in range(50)]
    c, _ = client(keys, rss(worst))
    seen, offset, request_id = [], 0, 1
    with c:
        while offset is not None:
            error, text = call(c, signed_headers(keys[0]), "list_items", {"limit": 50, "offset": offset}, request_id)
            page = json.loads(text)
            assert not error and len(text) <= LIST_RESULT_CHARS + 1024 and page["items"]
            assert all(item["link"] == link for item in page["items"])
            seen += [item["item_id"] for item in page["items"]]
            offset, request_id = page["next_offset"], request_id + 1
    assert request_id > 2, "the first page stopped early by size"
    assert len(seen) == len(set(seen)) == 50


def test_item_ids_are_stable_and_hide_the_guid():
    first = parse_feed(synthetic_feed(5), Scrubber(FEED_URL))
    again = parse_feed(synthetic_feed(5), Scrubber(FEED_URL))
    assert [item.item_id for item in first.items] == [item.item_id for item in again.items]
    assert all(len(item.item_id) == 16 and "guid" not in item.item_id for item in first.items)


def test_the_cache_expires(keys):
    now = [0.0]
    c, fetch = client(keys, synthetic_feed(2), clock=lambda: now[0])
    headers = signed_headers(keys[0])
    with c:
        call(c, headers, "list_items")
        now[0] = 121.0
        call(c, headers, "list_items", request_id=2)
    assert len(fetch.urls) == 2


def test_the_image_files_are_enough_to_import_the_server(tmp_path):
    """Dockerfile.feed-mcp copies only shared_context/__init__.py, shared_context/feed_signature.py and feed_mcp."""
    (tmp_path / "shared_context").mkdir()
    for name in ("__init__.py", "feed_signature.py"):
        (tmp_path / "shared_context" / name).write_bytes((APP_ROOT / "shared_context" / name).read_bytes())
    (tmp_path / "feed_mcp").mkdir()
    for path in (APP_ROOT / "feed_mcp").glob("*.py"):
        (tmp_path / "feed_mcp" / path.name).write_bytes(path.read_bytes())
    environ = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run([sys.executable, "-c", "import feed_mcp.server, feed_mcp.__main__; print('ok')"],
                            cwd=tmp_path, env={**environ, "PYTHONPATH": str(tmp_path)}, capture_output=True,
                            text=True, check=False)
    assert result.stdout.strip() == "ok", result.stderr[-2000:]
    dockerfile = (APP_ROOT / "Dockerfile.feed-mcp").read_text(encoding="utf-8")
    assert "COPY shared_context/__init__.py shared_context/feed_signature.py ./shared_context/" in dockerfile
