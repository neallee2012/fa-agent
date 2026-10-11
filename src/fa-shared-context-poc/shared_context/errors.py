from __future__ import annotations

import re
from typing import Any


def safe_text(value: Any, limit: int = 4096) -> str:
    text = str(value)
    text = re.sub(r"(?i)Bearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED_JWT]", text)
    return text[:limit]


def safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if str(k).lower() in {
                "authorization", "access_token", "refresh_token", "client_secret",
                "token", "accountkey", "cookie", "set-cookie",
            } else safe_json(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [safe_json(v) for v in value]
    return safe_text(value) if isinstance(value, str) else value


class Problem(Exception):
    def __init__(self, status: int, code: str, message: str, **details: Any):
        super().__init__(message)
        self.status = status
        self.body = {"error": {"code": code, "message": safe_text(message), **safe_json(details)}}


def upstream_problem(service: str, exc: Exception) -> Problem:
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    error = getattr(exc, "error", None)
    raw_error = {}
    if response is not None:
        try:
            payload = response.json()
            raw_error = payload.get("error", {}) if isinstance(payload, dict) else {}
            if not isinstance(raw_error, dict):
                raw_error = {}
        except (ValueError, AttributeError):
            pass
    code = getattr(error, "code", None) or raw_error.get("code") or type(exc).__name__
    message = getattr(error, "message", None) or raw_error.get("message") or str(exc)
    return Problem(502, "UPSTREAM_ERROR", safe_text(message), service=service,
                   upstream_status=status, upstream_code=code)
