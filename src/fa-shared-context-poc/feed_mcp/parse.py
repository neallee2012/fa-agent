"""RSS 2.0, RSS 1.0 and Atom into items with plain text. Nothing here fetches or follows a link.

Every string that leaves this module passes the feed URL's scrubber, so the subscriber's token never reaches a tool
result even when a feed repeats its own URL in an item. Item links lose their query, fragment and user info, and
become the bare origin when a path segment looks like a token.
"""

from __future__ import annotations

import hashlib
import pyexpat
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from .fetch import FeedError

# Expat 2.4.1+ refuses billion-laughs and quadratic-blowup entity expansion; ElementTree never loads an external DTD
# or entity (https://docs.python.org/3/library/xml.html#xml-vulnerabilities).
if pyexpat.version_info < (2, 4, 1):
    raise RuntimeError("expat 2.4.1 or newer is required")

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
DC = "{http://purl.org/dc/elements/1.1/}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
RSS1 = "{http://purl.org/rss/1.0/}"
REDACTED = "[已移除]"
# Every returned field is bounded; list_items also stops by serialized size (server.LIST_RESULT_CHARS).
SUMMARY_CHARS = 300
TITLE_CHARS = 300
AUTHOR_CHARS = 120
LINK_CHARS = 512
READ_PAGE_CHARS = 12_000
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9._~+=-]+")
_SENSITIVE_NAME = re.compile(r"(?i)token|key|secret|auth|sig|pass|session|code|hash|uid|user|id$")
_BLOCK = frozenset({"address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
                    "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol",
                    "p", "pre", "section", "table", "tr", "ul"})
_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "head", "iframe", "object", "video", "audio",
                   "canvas", "form", "button", "select", "textarea"})


def _wordish(part: str) -> bool:
    if part.isdigit():
        return len(part) <= 4
    return part.isalpha() and (part.islower() or part.istitle() or (part.isupper() and len(part) <= 6))


def token_like(value: str) -> bool:
    """An opaque credential rather than a readable slug: "2026-annual-review" is a slug, "a1B2c3D4e5F6" is not."""
    if len(value) < 12 or _TOKEN_CHARS.fullmatch(value) is None:
        return False
    parts = [part for part in re.split(r"[-_.~+=]", value) if part]
    return not parts or not all(_wordish(part) for part in parts)


class Scrubber:
    """Pieces of the feed URL that must never appear in a tool result."""

    def __init__(self, feed_url: str):
        parts = urlsplit(feed_url)
        found = {feed_url, feed_url.split("://", 1)[-1], parts.path + ("?" + parts.query if parts.query else "")}
        for piece in (*(parts.hostname or "").split("."), *parts.path.split("/")):
            if token_like(piece) or len(piece) >= 16:
                found.add(piece)
            found.update(part for part in re.split(r"[-_.~]", piece) if token_like(part))
        if parts.query:
            found.add(parts.query)
        for name, value in parse_qsl(parts.query, keep_blank_values=True):
            if token_like(value) or len(value) >= 16 or (len(value) >= 6 and _SENSITIVE_NAME.search(name)):
                found.add(value)
        found |= {unquote(item) for item in found}
        # Longest first: a whole URL is replaced before one of its own segments.
        self.pieces = tuple(sorted((item for item in found if len(item) >= 6 and item != "/"), key=len, reverse=True))

    def text(self, value: str) -> str:
        for piece in self.pieces:
            value = value.replace(piece, REDACTED)
        return value

    def leaks(self, value: str) -> bool:
        return any(piece in value or piece in unquote(value) for piece in self.pieces)


def safe_link(link: str | None, scrubber: Scrubber) -> str | None:
    if not link:
        return None
    try:
        parts = urlsplit(link.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    if scrubber.leaks(parts.hostname):
        return None
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    origin = f"{parts.scheme}://{host}" + (f":{port}" if port else "")
    path = parts.path or "/"
    if scrubber.leaks(link) or any(token_like(unquote(segment)) for segment in path.split("/")):
        return origin + "/"
    safe = urlunsplit((parts.scheme, origin.split("://", 1)[1], path, "", ""))
    return safe if len(safe) <= LINK_CHARS else origin + "/"


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def _open(self, tag, attrs, *, closed):
        tag = tag.rsplit(":", 1)[-1]
        if tag in _SKIP:
            if not closed:
                self.skip += 1
            return
        if self.skip:
            return
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK:
            self.parts.append("\n")
        elif tag == "img":
            alt = (dict(attrs).get("alt") or "").strip()
            if alt:
                self.parts.append(f"[圖片：{alt}]")

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs, closed=True)

    def handle_endtag(self, tag):
        tag = tag.rsplit(":", 1)[-1]
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _tidy(text: str) -> str:
    lines, blank = [], False
    for line in text.split("\n"):
        line = re.sub(r"[ \t\r\f\v\u00a0]+", " ", line).strip()
        if line:
            lines.append(line)
            blank = False
        elif lines and not blank:
            lines.append("")
            blank = True
    return "\n".join(lines).strip()


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    parser = _Text()
    parser.feed(value)
    parser.close()
    return _tidy("".join(parser.parts))


def _plain(element) -> str:
    return "" if element is None else "".join(element.itertext()).strip()


def _inline(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _date(raw: str) -> tuple[str | None, float | None]:
    raw = (raw or "").strip()
    if not raw:
        return None, None
    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            return _inline(raw)[:64], None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(), value.timestamp()


@dataclass(frozen=True)
class Item:
    item_id: str
    title: str
    published: str | None
    author: str | None
    link: str | None
    summary: str
    text: str
    order: float | None


@dataclass(frozen=True)
class Feed:
    title: str
    items: tuple[Item, ...]


def _atom_content(element) -> str:
    """Atom text constructs: html is escaped markup, xhtml is inline markup, text (the default) is plain."""
    if element is None:
        return ""
    kind = (element.get("type") or "text").lower()
    if kind == "html" or kind.endswith("/html"):
        return html_to_text(element.text)
    if kind == "xhtml" or kind.endswith("xml"):
        return html_to_text("".join(ET.tostring(child, encoding="unicode") for child in element))
    return _tidy("".join(element.itertext()))


class _Row(NamedTuple):
    """One entry as read, before scrubbing and capping."""

    key: str
    title: str
    date: str
    author: str
    link: str | None
    summary: str
    text: str


def _rss_row(key, title, date, author, link, description, content) -> _Row:
    """RSS 2.0 and 1.0: description is the summary and content:encoded the full text; each stands in for the other."""
    return _Row(key or "", title or "", date or "", author or "", link,
                html_to_text(description or content), html_to_text(content or description))


def _entries(root) -> tuple[str, list[_Row]]:
    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise FeedError("FEED_FORMAT")
        return channel.findtext("title") or "", [
            _rss_row(item.findtext("guid") or item.findtext("link"), item.findtext("title"),
                     item.findtext("pubDate") or item.findtext(DC + "date"),
                     item.findtext(DC + "creator") or item.findtext("author"), item.findtext("link"),
                     item.findtext("description"), item.findtext(CONTENT + "encoded"))
            for item in channel.findall("item")]
    if root.tag == RDF + "RDF":
        channel = root.find(RSS1 + "channel")
        return ("" if channel is None else channel.findtext(RSS1 + "title") or ""), [
            _rss_row(item.get(RDF + "about") or item.findtext(RSS1 + "link"), item.findtext(RSS1 + "title"),
                     item.findtext(DC + "date"), item.findtext(DC + "creator"), item.findtext(RSS1 + "link"),
                     item.findtext(RSS1 + "description"), item.findtext(CONTENT + "encoded"))
            for item in root.findall(RSS1 + "item")]
    if root.tag == ATOM + "feed":
        rows = []
        for entry in root.findall(ATOM + "entry"):
            links = entry.findall(ATOM + "link")
            link = next((item.get("href") for item in links if item.get("rel", "alternate") == "alternate"), None)
            summary, content = _atom_content(entry.find(ATOM + "summary")), _atom_content(entry.find(ATOM + "content"))
            rows.append(_Row(entry.findtext(ATOM + "id") or link or "", _plain(entry.find(ATOM + "title")),
                             entry.findtext(ATOM + "published") or entry.findtext(ATOM + "updated") or "",
                             _plain(entry.find(ATOM + "author/" + ATOM + "name")), link, summary or content,
                             content or summary))
        return _plain(root.find(ATOM + "title")), rows
    raise FeedError("FEED_NOT_RSS")


def _cap(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def parse_feed(data: bytes, scrubber: Scrubber) -> Feed:
    # Entity declarations are never needed by a feed; refuse them instead of relying on expansion limits alone.
    if re.search(rb"<!ENTITY", data, re.IGNORECASE):
        raise FeedError("FEED_FORMAT")
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError):
        raise FeedError("FEED_FORMAT") from None
    title, rows = _entries(root)
    items, seen = [], set()
    for index, row in enumerate(rows):
        key = row.key.strip() or f"{row.title}\n{row.date}"
        item_id = hashlib.sha256(f"fa-feed-item-v1\n{key}".encode("utf-8")).hexdigest()[:16]
        if item_id in seen:
            item_id = hashlib.sha256(f"fa-feed-item-v1\n{key}\n#{index}".encode("utf-8")).hexdigest()[:16]
        seen.add(item_id)
        published, order = _date(row.date)
        items.append(Item(item_id=item_id, title=_cap(scrubber.text(_inline(html_to_text(row.title))), TITLE_CHARS),
                          published=published, author=_cap(scrubber.text(_inline(row.author)), AUTHOR_CHARS) or None,
                          link=safe_link(row.link, scrubber),
                          summary=_cap(scrubber.text(_inline(row.summary)), SUMMARY_CHARS),
                          text=scrubber.text(row.text), order=order))
    # Newest first; items without a readable date keep the feed's order after the dated ones.
    items.sort(key=lambda item: (item.order is None, -(item.order or 0.0)))
    return Feed(title=_cap(scrubber.text(_inline(html_to_text(title))), TITLE_CHARS), items=tuple(items))


def pages(text: str) -> list[str]:
    """Pages of at most READ_PAGE_CHARS, cut at a line break in the last quarter when there is one."""
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + READ_PAGE_CHARS)
        if end < len(text):
            cut = text.rfind("\n", start + READ_PAGE_CHARS * 3 // 4, end)
            if cut > start:
                end = cut + 1
        out.append(text[start:end])
        start = end
    return out or [""]
