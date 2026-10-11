"""Bounded signed-human text intake, not a general secret detector or tool gate.

The server explicitly enables this policy. Human consent binds exact received
bytes; it is permission to retain their own submitted text, NOT review of its
truth or approval to execute it. Known credential forms are rejected before
storage; arbitrary tool output still requires its own trusted sanitizer.
"""
from __future__ import annotations

import hashlib
import re

from .errors import Problem, safe_text

TEXT_SCHEMA = "fa-human-text-v1"
METRICS_SCHEMA = "fa-artifact-metrics-v1"
_CREDENTIAL = re.compile(
    r"(?i)(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}"
    r"|\b(?:access_token|refresh_token|client_secret|accountkey|password|api[_-]?key)"
    r"\s*[=:]\s*[\"']?[^\s\"']{3,}"
    r"|[?&]sig=[A-Za-z0-9%+/=]{16,})"
)


def checked_human_text(original: bytes) -> str:
    if not isinstance(original, bytes) or not 0 < len(original) <= 8192:
        raise Problem(422, "BEHAVIOR_UNSAFE_CONTENT", "text must be complete and within its byte bound")
    try:
        text = original.decode("utf-8")
    except UnicodeError:
        raise Problem(422, "BEHAVIOR_UNSAFE_CONTENT", "valid UTF-8 text is required") from None
    if (any(ord(char) < 32 and char not in "\r\n\t" for char in text)
            or safe_text(text, len(text)) != text or _CREDENTIAL.search(text)):
        raise Problem(422, "BEHAVIOR_UNSAFE_CONTENT", "submitted text contains a prohibited credential or control form")
    return text


def require_text_consent(original: bytes, consent_digest: str | None, *, human: bool) -> None:
    if not human or consent_digest != hashlib.sha256(original).hexdigest():
        raise Problem(403, "BEHAVIOR_SOURCE_CONSENT_REQUIRED", "signed human consent must bind these exact source bytes")
    checked_human_text(original)
