"""The FA API signs one private feed URL; the feed reader MCP server fetches only a URL it can verify.

A Team owner creates a project connection whose header ``x-feed-url`` holds a private feed URL (the URL itself is
the subscriber's credential). Foundry injects a connection's headers into every call to the MCP server, so the agent
and model never see the URL ("The agent never sees the secret",
https://learn.microsoft.com/azure/foundry/agents/how-to/tools/tool-authentication). The settings page writes one
header per new connection; custom-keys connections carry more than one (same page: "repeatable when a service needs
more than one header"), so the API adds ``x-fa-feed-signature``, an Ed25519 signature of exactly that URL.

The feed server holds only the public key. Knowing its address is not enough to make it fetch anything, and a
leaked signature only fetches the one URL it was made for, which its holder already has.

Standard library only at import time; ``cryptography`` is imported where a key is used. The feed server image copies
this one module (and the package ``__init__``).
"""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import urlsplit

FEED_URL_HEADER = "x-feed-url"
FEED_SIGNATURE_HEADER = "x-fa-feed-signature"
SIGNING_KEY_ENV = "FA_FEED_MCP_SIGNING_KEY"
PUBLIC_KEY_ENV = "FEED_MCP_PUBLIC_KEY"
MAX_FEED_URL_CHARS = 2048
_MESSAGE_PREFIX = b"fa-feed-mcp-signature-v1\n"
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{86}")


def check_feed_url(value) -> str:
    """https, a host name, port 443, visible ASCII, no user info or fragment. Errors never quote the URL."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_FEED_URL_CHARS:
        raise ValueError("feed URL length")
    if any(not 0x21 <= ord(char) <= 0x7E for char in value):
        raise ValueError("feed URL characters")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("feed URL form") from None
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None
            or "#" in value or port not in (None, 443)):
        raise ValueError("feed URL form")
    return value


def _key_bytes(text) -> bytes:
    try:
        raw = base64.b64decode(text.strip(), validate=True) if isinstance(text, str) else b""
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 32:
        raise ValueError("an Ed25519 key is 32 bytes in standard base64")
    return raw


def load_signing_key(text):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(_key_bytes(text))


def load_public_key(text):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey.from_public_bytes(_key_bytes(text))


def sign_feed_url(private_key, url: str) -> str:
    message = _MESSAGE_PREFIX + check_feed_url(url).encode("ascii")
    return base64.urlsafe_b64encode(private_key.sign(message)).decode("ascii").rstrip("=")


def verify_feed_url(public_key, url, signature) -> bool:
    from cryptography.exceptions import InvalidSignature

    if not isinstance(signature, str) or _SIGNATURE.fullmatch(signature) is None:
        return False
    try:
        message = _MESSAGE_PREFIX + check_feed_url(url).encode("ascii")
        public_key.verify(base64.urlsafe_b64decode(signature + "=="), message)
    except (ValueError, InvalidSignature, binascii.Error):
        return False
    return True


def generate_keypair() -> tuple[str, str]:
    """(private, public) in standard base64; the private key goes only into the API's secret."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(private).decode("ascii"), base64.b64encode(public).decode("ascii")
