from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from typing import Mapping
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


@dataclass(frozen=True)
class BackupSettings:
    endpoint: str
    container: str

    def __post_init__(self):
        if not re.fullmatch(r"https://[a-z0-9]{3,24}\.blob\.core\.windows\.net", self.endpoint):
            raise ValueError("CONTEXT_BACKUP_BLOB_ENDPOINT must be https://<account>.blob.core.windows.net")
        if not 3 <= len(self.container) <= 63 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.container):
            raise ValueError("CONTEXT_BACKUP_CONTAINER must be a valid Azure container name")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> BackupSettings | None:
        values = os.environ if environ is None else environ
        endpoint, container = "CONTEXT_BACKUP_BLOB_ENDPOINT", "CONTEXT_BACKUP_CONTAINER"
        if endpoint not in values and container not in values:
            return None
        if endpoint not in values or container not in values:
            raise ValueError("CONTEXT_BACKUP_BLOB_ENDPOINT and CONTEXT_BACKUP_CONTAINER must be configured together")
        return cls(values[endpoint].removesuffix("/"), values[container])
