from __future__ import annotations

import base64
import re
from uuid import UUID

from .errors import Problem
from .models import ID_PATTERN


def native_memory_scope(scope: str) -> str:
    parts = scope.split(":")
    valid_shape = (parts[0] == "team" and len(parts) == 2) or (parts[0] == "private" and len(parts) == 3)
    if not valid_shape or not all(re.fullmatch(ID_PATTERN, part) for part in parts[1:]):
        raise Problem(502, "MEMORY_PROTOCOL_ERROR", "invalid internal memory scope")
    # The async extractor accepts fewer characters than the front-end validator.
    return "sc1-" + base64.urlsafe_b64encode(scope.encode("ascii")).decode("ascii").rstrip("=")


def validate_api_audience(audience: str) -> None:
    match = re.fullmatch(r"api://([a-f0-9-]{36})(?:/[A-Za-z0-9][A-Za-z0-9_-]{0,127})?", audience)
    if not match or str(UUID(match.group(1))) != match.group(1):
        raise ValueError("CONTEXT_API_AUDIENCE must be api://<canonical-UUID>[/<resource-name>]")
