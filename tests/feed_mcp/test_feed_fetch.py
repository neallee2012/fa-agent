"""The guarded fetch: https/443 to public addresses only (each redirect too), bounded, RSS/Atom media types."""

from __future__ import annotations

import logging

import httpcore
import pytest

from feed_fixtures import FEED_URL, PUBLIC_IP, TOKEN, FakeNetwork, http_response, rss  # first: puts the app on sys.path

import feed_mcp.fetch as fetch  # noqa: E402
from feed_mcp.fetch import FeedError, GuardedBackend, fetch_feed, public_address  # noqa: E402

FEED = rss([{"title": "One", "guid": "1"}])


def backend(responses, names=None):
    names = names or {"feeds.example.test": [PUBLIC_IP]}
    network = FakeNetwork(responses)
    return GuardedBackend(inner=network, resolve=lambda host, port: names[host]), network


def failure(url=FEED_URL, **kwargs):
    with pytest.raises(FeedError) as caught:
        fetch_feed(url, **kwargs)
    return caught.value


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254",
                                     "100.64.0.1", "168.63.129.16", "0.0.0.0", "224.0.0.1", "::1", "fe80::1",
                                     "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "64:ff9b::a9fe:a9fe",
                                     "2002:7f00:1::", "not-an-ip"])
def test_private_loopback_link_local_metadata_and_platform_addresses_are_refused(address):
    assert public_address(address) is False


@pytest.mark.parametrize("address", [PUBLIC_IP, "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address):
    assert public_address(address) is True


def test_a_name_with_any_non_public_address_or_another_port_is_never_connected():
    guarded, network = backend({PUBLIC_IP: http_response(body=FEED)},
                               {"feeds.example.test": [PUBLIC_IP, "10.0.0.5"]})
    with pytest.raises(FeedError) as mixed:
        guarded.connect_tcp("feeds.example.test", 443)
    assert mixed.value.code == "FEED_URL_BLOCKED"
    with pytest.raises(FeedError) as port:
        guarded.connect_tcp("feeds.example.test", 8443)
    assert port.value.code == "FEED_URL_BLOCKED" and network.connected == []


def test_a_feed_is_read_from_the_checked_address_without_credentials():
    guarded, network = backend({PUBLIC_IP: http_response(body=FEED)})
    assert fetch_feed(FEED_URL, backend=guarded) == FEED
    assert network.connected == [(PUBLIC_IP, 443)]
    request = b"".join(network.sent).lower()
    assert b"host: feeds.example.test" in request and b"cookie" not in request and b"authorization" not in request
    assert b"x-feed-url" not in request and b"x-fa-feed-signature" not in request


def test_each_redirect_is_checked_again():
    to_private = backend({PUBLIC_IP: http_response(302, headers={"Location": "https://inside.example.test/rss"})},
                         {"feeds.example.test": [PUBLIC_IP], "inside.example.test": ["10.1.2.3"]})
    assert failure(backend=to_private[0]).code == "FEED_URL_BLOCKED" and to_private[1].connected == [(PUBLIC_IP, 443)]
    to_http = backend({PUBLIC_IP: http_response(301, headers={"Location": "http://feeds.example.test/rss"})})
    assert failure(backend=to_http[0]).code == "FEED_URL_BLOCKED"
    to_port = backend({PUBLIC_IP: http_response(307, headers={"Location": "https://feeds.example.test:8443/rss"})})
    assert failure(backend=to_port[0]).code == "FEED_URL_BLOCKED"
    loop = backend({PUBLIC_IP: [http_response(302, headers={"Location": "/again"}) for _ in range(5)]})
    assert failure(backend=loop[0]).code == "FEED_REDIRECTS" and len(loop[1].connected) == 4
    moved = backend({PUBLIC_IP: [http_response(301, headers={"Location": "/moved"}), http_response(body=FEED)]})
    assert fetch_feed(FEED_URL, backend=moved[0]) == FEED


@pytest.mark.parametrize("response,code", [
    (http_response(404), "FEED_HTTP_STATUS"),
    (http_response(body=b"<html></html>", headers={"Content-Type": "text/html"}), "FEED_NOT_RSS"),
    (http_response(body=FEED, headers={"Content-Encoding": "gzip"}), "FEED_FORMAT"),
    (http_response(body=b"x", headers={"Content-Length": str(fetch.MAX_FEED_BYTES + 1)}), "FEED_TOO_LARGE"),
])
def test_status_media_type_encoding_and_declared_size_are_checked(response, code):
    assert failure(backend=backend({PUBLIC_IP: response})[0]).code == code


def test_a_body_larger_than_the_limit_stops_while_streaming(monkeypatch):
    monkeypatch.setattr(fetch, "MAX_FEED_BYTES", 1024)
    chunked = (b"HTTP/1.1 200 OK\r\nContent-Type: application/rss+xml\r\nTransfer-Encoding: chunked\r\n"
               b"Connection: close\r\n\r\n" + b"".join(b"400\r\n" + b"x" * 1024 + b"\r\n" for _ in range(3)) + b"0\r\n\r\n")
    assert failure(backend=backend({PUBLIC_IP: chunked})[0]).code == "FEED_TOO_LARGE"


def test_the_total_deadline_stops_a_slow_feed():
    ticks = iter([0.0] + [fetch.TOTAL_SECONDS + 1] * 10)
    assert failure(backend=backend({PUBLIC_IP: http_response(body=FEED)})[0],
                   clock=lambda: next(ticks)).code == "FEED_FETCH_FAILED"


def test_network_errors_are_one_fixed_code_and_never_log_the_url(caplog):
    caplog.set_level(logging.DEBUG)
    guarded, _ = backend({})
    error = failure(backend=guarded)
    assert error.code == "FEED_FETCH_FAILED" and TOKEN not in str(error)
    assert TOKEN not in caplog.text and "feeds.example.test" not in caplog.text


def test_only_https_feed_urls_are_fetched():
    for url in ("http://feeds.example.test/rss", "https://feeds.example.test:8443/rss", "file:///etc/passwd"):
        assert failure(url, backend=backend({PUBLIC_IP: http_response(body=FEED)})[0]).code == "FEED_URL_BLOCKED"


def test_the_real_backend_refuses_loopback_before_connecting():
    with pytest.raises(FeedError) as caught:
        GuardedBackend(resolve=lambda host, port: ["127.0.0.1"]).connect_tcp("localhost", 443)
    assert caught.value.code == "FEED_URL_BLOCKED"
    assert isinstance(GuardedBackend().inner, httpcore.SyncBackend)
