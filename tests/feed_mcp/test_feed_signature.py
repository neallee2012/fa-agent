"""The API signs exactly one feed URL; only that URL verifies against the public key."""

from __future__ import annotations

import pytest

from feed_fixtures import FEED_URL, keys  # noqa: F401  (first: puts the app on sys.path)
from shared_context.feed_signature import (
    check_feed_url, generate_keypair, load_public_key, load_signing_key, sign_feed_url, verify_feed_url,
)


def test_a_signature_verifies_only_for_its_url_and_key(keys):
    private, public = keys
    signature = sign_feed_url(private, FEED_URL)
    assert len(signature) == 86 and verify_feed_url(public, FEED_URL, signature)
    _, other = generate_keypair()
    tampered = ("A" if signature[0] != "A" else "B") + signature[1:]
    for url, sig, key in ((FEED_URL + "/", signature, public), (FEED_URL, tampered, public),
                          (FEED_URL, signature, load_public_key(other)), (FEED_URL, "", public),
                          (FEED_URL, None, public), (FEED_URL, signature + "=", public),
                          ("http://feeds.example.test/rss", sign_feed_url(private, FEED_URL), public)):
        assert not verify_feed_url(key, url, sig)


@pytest.mark.parametrize("url", ["", "http://a.example.test/f", "https:///f", "https://u:p@a.example.test/f",
                                 "https://a.example.test:444/f", "https://a.example.test/f#x",
                                 "https://a.example.test/f g", "https://a.example.test/\u00e9", "ftp://a.example.test/f",
                                 "https://a.example.test/" + "x" * 2048, None, 7])
def test_only_one_https_url_on_port_443_is_a_feed_url(url):
    with pytest.raises(ValueError) as caught:
        check_feed_url(url)
    assert "example.test" not in str(caught.value)


def test_explicit_port_443_and_query_strings_are_feed_urls():
    for url in ("https://a.example.test:443/f", "https://a.example.test/f?token=OFFLINEabc123XYZ"):
        assert check_feed_url(url) == url


@pytest.mark.parametrize("text", ["", "not base64!", "QUJD", None])
def test_keys_must_be_32_bytes_of_standard_base64(text):
    with pytest.raises(ValueError):
        load_signing_key(text)
    with pytest.raises(ValueError):
        load_public_key(text)
