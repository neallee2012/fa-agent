from __future__ import annotations

import unicodedata
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, Field

from .models import StrictModel

MAX_BACKUP_BYTES = 8 * 1024 * 1024
BackupId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)]
BackupSHA256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)]
BackupVersion = Annotated[str, Field(pattern=r"^[\x20-\x7e]+$", min_length=1, max_length=256)]


def canonical_oid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("owner_oid must be a canonical UUID")
    return value


def single_etag(value: str) -> str:
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("etag must not contain control characters")
    if value.strip() == "*" or "," in value:
        raise ValueError("etag must be a single non-wildcard value")
    return value


OwnerOid = Annotated[str, AfterValidator(canonical_oid)]
BackupETag = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(single_etag)]


class BackupReceipt(StrictModel):
    backup_id: BackupId
    owner_oid: OwnerOid
    sha256: BackupSHA256
    bytes: Annotated[int, Field(ge=1, le=MAX_BACKUP_BYTES)]
    version_id: BackupVersion
    etag: BackupETag
