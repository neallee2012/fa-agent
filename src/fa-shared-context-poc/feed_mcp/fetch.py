"""One bounded HTTPS GET of a signed feed URL; nothing else leaves this server.

Only https on port 443 to public addresses. Every connection, including each redirect hop, resolves the host name and
refuses it unless every address is public, then connects to one of those checked addresses (TLS still verifies the
host name), so a name that resolves elsewhere a moment later cannot reach a private service. No proxy, cookie,
credential, retry or decompression. Errors carry a fixed code and never the URL.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import ssl
import time
from urllib.parse import urljoin

import certifi
import httpcore

from shared_context.feed_signature import check_feed_url

MAX_FEED_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 3
CONNECT_SECONDS = 5.0
READ_SECONDS = 10.0
TOTAL_SECONDS = 20.0
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
# RSS/Atom media types, plus the generic ones some feed hosts send; the parser still requires an RSS/Atom root.
_FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml", "application/rdf+xml", "application/xml",
                         "text/xml", "application/x-rss+xml", "text/plain", "application/octet-stream", ""})
# Public-looking addresses that still reach the platform or a private network: Azure WireServer (168.63.129.16,
# https://learn.microsoft.com/azure/virtual-network/what-is-ip-address-168-63-129-16), NAT64 and 6to4 prefixes.
_BLOCKED = tuple(ipaddress.ip_network(net) for net in ("168.63.129.16/32", "64:ff9b::/96", "64:ff9b:1::/48",
                                                       "2002::/16"))
_HEADERS = [(b"User-Agent", b"FA-feed-reader/1"), (b"Accept-Encoding", b"identity"),
            (b"Accept", b"application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.1")]
_log = logging.getLogger(__name__)
# httpcore's debug trace names the host and request target; this process never lets it reach a log.
logging.getLogger("httpcore").setLevel(logging.WARNING)


class FeedError(Exception):
    def __init__(self, code: str, status: int | None = None):
        super().__init__(code)
        self.code, self.status = code, status


def public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not any(ip in net for net in _BLOCKED if net.version == ip.version)


def _resolve(host: str, port: int) -> list[str]:
    try:
        return list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except (OSError, UnicodeError):
        raise FeedError("FEED_FETCH_FAILED") from None


class GuardedBackend(httpcore.NetworkBackend):
    """Connects only to checked public addresses on port 443."""

    def __init__(self, inner: httpcore.NetworkBackend | None = None, resolve=_resolve):
        self.inner, self.resolve = inner or httpcore.SyncBackend(), resolve

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if port != 443:
            raise FeedError("FEED_URL_BLOCKED")
        addresses = self.resolve(host, port)
        if not addresses or not all(public_address(address) for address in addresses):
            raise FeedError("FEED_URL_BLOCKED")
        failure = None
        for address in addresses:
            try:
                return self.inner.connect_tcp(address, port, timeout=timeout, local_address=local_address,
                                              socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                failure = error
        raise failure

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise FeedError("FEED_URL_BLOCKED")

    def sleep(self, seconds):
        self.inner.sleep(seconds)


_SSL = None


def _ssl_context() -> ssl.SSLContext:
    global _SSL
    if _SSL is None:
        _SSL = ssl.create_default_context(cafile=certifi.where())
    return _SSL


def _header(headers, name: bytes) -> str | None:
    values = [value for key, value in headers if key.lower() == name]
    if len(values) > 1:
        raise FeedError("FEED_FORMAT")
    return values[0].decode("latin-1").strip() if values else None


def fetch_feed(url: str, *, backend: httpcore.NetworkBackend | None = None, clock=time.monotonic) -> bytes:
    deadline = clock() + TOTAL_SECONDS
    current = url
    with httpcore.ConnectionPool(ssl_context=_ssl_context(), network_backend=backend or GuardedBackend(), retries=0,
                                 http1=True, http2=False, max_connections=2) as pool:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                check_feed_url(current)
            except ValueError:
                raise FeedError("FEED_URL_BLOCKED") from None
            remaining = deadline - clock()
            if remaining <= 0:
                raise FeedError("FEED_FETCH_FAILED")
            timeout = {"connect": min(CONNECT_SECONDS, remaining), "read": min(READ_SECONDS, remaining),
                       "write": min(READ_SECONDS, remaining), "pool": min(CONNECT_SECONDS, remaining)}
            try:
                with pool.stream("GET", current, headers=_HEADERS, extensions={"timeout": timeout}) as response:
                    if response.status in _REDIRECTS:
                        location = _header(response.headers, b"location")
                        if not location:
                            raise FeedError("FEED_HTTP_STATUS", response.status)
                        current = urljoin(current, location)
                        continue
                    if not 200 <= response.status < 300:
                        raise FeedError("FEED_HTTP_STATUS", response.status)
                    media = (_header(response.headers, b"content-type") or "").split(";", 1)[0].strip().lower()
                    if media not in _FEED_TYPES:
                        raise FeedError("FEED_NOT_RSS")
                    if (_header(response.headers, b"content-encoding") or "identity").lower() != "identity":
                        raise FeedError("FEED_FORMAT")
                    length = _header(response.headers, b"content-length")
                    if length is not None and length.isdecimal() and int(length) > MAX_FEED_BYTES:
                        raise FeedError("FEED_TOO_LARGE")
                    chunks, total = [], 0
                    for chunk in response.iter_stream():
                        total += len(chunk)
                        if total > MAX_FEED_BYTES:
                            raise FeedError("FEED_TOO_LARGE")
                        if clock() > deadline:
                            raise FeedError("FEED_FETCH_FAILED")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except FeedError:
                raise
            except Exception as error:
                # The type only: httpcore and ssl messages can name the host or the request target.
                _log.warning("FEED_FETCH_ERROR type=%s", type(error).__name__)
                raise FeedError("FEED_FETCH_FAILED") from None
    raise FeedError("FEED_REDIRECTS")
