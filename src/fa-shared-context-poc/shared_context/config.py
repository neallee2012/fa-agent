from __future__ import annotations

import re
from uuid import UUID

from .errors import Problem
from .models import ID_PATTERN


def validate_api_audience(audience: str) -> None:
    match = re.fullmatch(r"api://([a-f0-9-]{36})(?:/[A-Za-z0-9][A-Za-z0-9_-]{0,127})?", audience)
    if not match or str(UUID(match.group(1))) != match.group(1):
        raise ValueError("CONTEXT_API_AUDIENCE must be api://<canonical-UUID>[/<resource-name>]")
