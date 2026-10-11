"""MEM-01 (2026-10-03): fixed rules applied before any memory write; no model judgment.

The official extractor stored a synthetic key and the user's "do not store" turn (13d9 probe 2-c). Foundry offers no
pre-extraction filter for an existing store: ``user_profile_details`` is set only at creation and covers only user
profiles, and direct remember/forget commands exist only through the memory search tool
(https://learn.microsoft.com/azure/foundry/agents/how-to/memory-usage#customize-memory,
#apply-direct-remember-or-forget-behavior). Its best practices leave redaction to the caller (#best-practices), so FA:

- ``redact_secrets`` replaces values that clearly look like credentials (a labeled value, or a well-known key/token
  format) with ``REDACTED``. Plain words such as "token count" or "password policy" stay; anything without a label or a
  known format is not recognized (a stated limit, not a guarantee).
- ``opted_out`` finds an explicit "do not remember/store" in the human's own words; such a turn sends no memory write.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED-SECRET]"

_LABEL = (r"api[ _-]?key|apikey|access[ _-]?key|client[ _-]?secret|secret|token|password|passwd|pwd"
          r"|密碼|金鑰|密鑰|令牌|憑證")
# A label, then a separator, then a value of at least six non-CJK characters up to whitespace, a quote or punctuation
# (a value another rule already replaced is not counted twice). Chinese has no spaces, so stopping at CJK keeps
# sentences such as 「密碼是用來登入系統的」 intact.
_VALUE_CHAR = r"(?![\u3000-\u9fff\uff00-\uffef])[^\s\"'「」『』，。、；;,()（）]"
_LABELED = re.compile(
    r"((?:" + _LABEL + r")\s*(?:(?:是|為)(?:\s*[:：=])?|[:：=])\s*[\"'「『]?)"
    r"(?!\[REDACTED-SECRET\])((?:" + _VALUE_CHAR + r"){6,})", re.IGNORECASE)
# re.ASCII: a token written right next to Chinese text (no space) still has a word boundary.
_FORMATS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}", re.ASCII),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}", re.ASCII),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}", re.ASCII),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b", re.ASCII),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}", re.ASCII),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", re.ASCII),
)
_VALUED = (
    re.compile(r"(\bBearer\s+)([A-Za-z0-9._~+/=-]{20,})", re.ASCII),
    re.compile(r"(\b(?:AccountKey|SharedAccessKey|SharedAccessSignature|Password|Pwd)=)"
               r"((?:(?![\u3000-\u9fff\uff00-\uffef])[^;\s])+)", re.IGNORECASE | re.ASCII),
)
_OPT_OUT = re.compile(
    r"(?:不要|別|不用|勿)(?:幫我)?(?:記住|記下|記錄|記(?!錯)|存下|存|保存|儲存|寫進記憶|放進記憶)"
    r"|\b(?:don['’]?t|do not)\s+(?:remember|store|save)\b", re.IGNORECASE | re.ASCII)


def redact_secrets(text: str) -> tuple[str, int]:
    """``text`` with clearly credential-like values replaced, and how many were replaced."""
    count = 0
    for pattern in _FORMATS:
        text, found = pattern.subn(REDACTED, text)
        count += found
    for pattern in (*_VALUED, _LABELED):
        text, found = pattern.subn(lambda match: match.group(1) + REDACTED, text)
        count += found
    return text, count


def opted_out(text: str) -> bool:
    """True when the human's own words explicitly ask not to remember or store this turn."""
    return _OPT_OUT.search(text) is not None
