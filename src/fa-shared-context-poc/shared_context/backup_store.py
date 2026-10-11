from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from azure.core import MatchConditions
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from .auth import Actor
from .backup_models import BackupETag, BackupId, BackupReceipt, BackupSHA256, BackupVersion, MAX_BACKUP_BYTES, canonical_oid
from .errors import Problem, safe_text, upstream_problem

if TYPE_CHECKING:
    from azure.storage.blob import BlobClient, BlobProperties, ContainerClient

_backup_id = TypeAdapter(BackupId)
_sha256 = TypeAdapter(BackupSHA256)
_version = TypeAdapter(BackupVersion)
_etag = TypeAdapter(BackupETag)


def validate_lookup(backup_id: str, version_id: str | None = None) -> None:
    try:
        _backup_id.validate_python(backup_id, strict=True)
        if version_id is not None:
            _version.validate_python(version_id, strict=True)
    except ValidationError as exc:
        raise Problem(422, "INVALID_BACKUP", "invalid backup ID or version ID") from exc


def not_found() -> Problem:
    return Problem(404, "BACKUP_NOT_FOUND", "backup not found")


def blob_problem(exc: AzureError) -> Problem:
    problem = upstream_problem("blob", exc)
    code = getattr(exc, "error_code", None)
    if code is not None:
        problem.body["error"]["upstream_code"] = safe_text(getattr(code, "value", code))
    return problem


def missing_blob_problem(exc: ResourceNotFoundError) -> Problem:
    if getattr(exc, "error_code", None) == "BlobNotFound":
        return not_found()
    return blob_problem(exc)


def require_etag(receipt: BackupReceipt, etag: str | None) -> None:
    if etag is None:
        raise Problem(428, "ETAG_REQUIRED", "If-Match is required")
    try:
        _etag.validate_python(etag, strict=True)
    except ValidationError as exc:
        raise Problem(412, "ETAG_CONFLICT", "supply one non-wildcard ETag without control characters") from exc
    if etag != receipt.etag:
        raise Problem(412, "ETAG_CONFLICT", "supply the exact ETag from the backup receipt")


def verify_content(receipt: BackupReceipt, data: bytes) -> None:
    if len(data) != receipt.bytes or hashlib.sha256(data).hexdigest() != receipt.sha256:
        raise Problem(502, "BACKUP_INTEGRITY_ERROR", "stored backup content does not match its receipt")


def validate_upload(backup_id: str, data: bytes, sha256: str) -> None:
    try:
        _backup_id.validate_python(backup_id, strict=True)
        _sha256.validate_python(sha256, strict=True)
    except ValidationError as exc:
        raise Problem(422, "INVALID_BACKUP", "invalid backup ID or SHA-256") from exc
    if len(data) > MAX_BACKUP_BYTES:
        raise Problem(413, "BACKUP_TOO_LARGE", "backup exceeds 8 MiB")
    if not data:
        raise Problem(422, "EMPTY_BACKUP", "backup must not be empty")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise Problem(422, "BACKUP_HASH_MISMATCH", "backup SHA-256 does not match the body")


@dataclass(frozen=True)
class BackupDownload:
    receipt: BackupReceipt
    data: bytes


class BackupStore(Protocol):
    def create(self, actor: Actor, backup_id: str, data: bytes, sha256: str) -> BackupReceipt: ...
    def receipt(self, actor: Actor, backup_id: str, version_id: str | None = None) -> BackupReceipt: ...
    def download(self, actor: Actor, backup_id: str, version_id: str, etag: str | None) -> BackupDownload: ...


class InMemoryBackupStore:
    """Local adapter for the same contract, not evidence of Azure persistence."""

    def __init__(self, tenant_id: str):
        self.tenant_id = canonical_oid(tenant_id)
        self._lock = threading.Lock()
        self._backups: dict[tuple[str, str], tuple[BackupReceipt, bytes]] = {}

    def create(self, actor: Actor, backup_id: str, data: bytes, sha256: str) -> BackupReceipt:
        validate_upload(backup_id, data, sha256)
        owner = canonical_oid(actor.oid)
        with self._lock:
            key = (owner, backup_id)
            if key in self._backups:
                raise Problem(409, "BACKUP_EXISTS", "backup already exists")
            receipt = BackupReceipt(
                backup_id=backup_id, owner_oid=owner, sha256=sha256, bytes=len(data),
                version_id="memory-" + uuid4().hex, etag=f'"memory-{uuid4().hex}"',
            )
            self._backups[key] = (receipt, data)
            return receipt.model_copy()

    def receipt(self, actor: Actor, backup_id: str, version_id: str | None = None) -> BackupReceipt:
        validate_lookup(backup_id, version_id)
        with self._lock:
            result = self._backups.get((canonical_oid(actor.oid), backup_id))
            if result is None or (version_id is not None and result[0].version_id != version_id):
                raise not_found()
            return result[0].model_copy()

    def download(self, actor: Actor, backup_id: str, version_id: str, etag: str | None) -> BackupDownload:
        if version_id is None:
            raise Problem(422, "BACKUP_VERSION_REQUIRED", "version_id is required for download")
        receipt = self.receipt(actor, backup_id, version_id)
        require_etag(receipt, etag)
        with self._lock:
            data = self._backups[(actor.oid, backup_id)][1]
        verify_content(receipt, data)
        return BackupDownload(receipt, data)


class BlobBackupStore:
    def __init__(self, container: ContainerClient, tenant_id: str):
        self.container = container
        self.tenant_id = canonical_oid(tenant_id)

    def _blob(self, actor: Actor, backup_id: str, version_id: str | None = None) -> BlobClient:
        validate_lookup(backup_id, version_id)
        owner = canonical_oid(actor.oid)
        return self.container.get_blob_client(
            f"v1/{self.tenant_id}/{owner}/{backup_id}", version_id=version_id,
        )

    def _receipt(self, actor: Actor, backup_id: str, properties: BlobProperties,
                 version_id: str | None = None) -> BackupReceipt:
        metadata = properties.metadata
        if not isinstance(metadata, dict):
            raise Problem(502, "BACKUP_PROTOCOL_ERROR", "stored backup metadata is missing")
        if metadata.get("owner_oid") != actor.oid or metadata.get("tenant_id") != self.tenant_id:
            raise not_found()
        if (metadata.get("backup_id") != backup_id or properties.blob_type != "BlockBlob"
                or properties.content_settings.content_type != "application/octet-stream"
                or metadata.get("bytes") != str(properties.size)):
            raise Problem(502, "BACKUP_PROTOCOL_ERROR", "stored backup properties are inconsistent")
        try:
            receipt = BackupReceipt(
                backup_id=backup_id, owner_oid=actor.oid, sha256=metadata.get("sha256"),
                bytes=properties.size, version_id=properties.version_id, etag=properties.etag,
            )
        except ValidationError as exc:
            raise Problem(502, "BACKUP_PROTOCOL_ERROR", "stored backup receipt is invalid or unversioned") from exc
        if version_id is not None and receipt.version_id != version_id:
            raise Problem(502, "BACKUP_PROTOCOL_ERROR", "storage returned a different backup version")
        return receipt

    def create(self, actor: Actor, backup_id: str, data: bytes, sha256: str) -> BackupReceipt:
        from azure.storage.blob import ContentSettings

        validate_upload(backup_id, data, sha256)
        blob = self._blob(actor, backup_id)
        try:
            result = blob.upload_blob(
                data, length=len(data), overwrite=False, max_concurrency=1, timeout=20,
                retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
                content_settings=ContentSettings(content_type="application/octet-stream"),
                metadata={
                    "tenant_id": self.tenant_id, "owner_oid": actor.oid, "backup_id": backup_id,
                    "sha256": sha256, "bytes": str(len(data)),
                },
            )
        except (ResourceExistsError, ResourceModifiedError) as exc:
            raise Problem(409, "BACKUP_EXISTS", "backup already exists or create condition failed") from exc
        except AzureError as exc:
            raise blob_problem(exc) from exc
        try:
            written = BackupReceipt(
                backup_id=backup_id, owner_oid=actor.oid, sha256=sha256, bytes=len(data),
                version_id=result.get("version_id"), etag=result.get("etag"),
            )
        except ValidationError as exc:
            raise Problem(502, "BACKUP_PROTOCOL_ERROR", "upload did not return a native version and ETag") from exc
        confirmed = self.receipt(actor, backup_id, written.version_id)
        if confirmed != written:
            raise Problem(502, "BACKUP_INTEGRITY_ERROR", "stored backup differs from the upload acknowledgement")
        return confirmed

    def receipt(self, actor: Actor, backup_id: str, version_id: str | None = None) -> BackupReceipt:
        blob = self._blob(actor, backup_id, version_id)
        try:
            properties = blob.get_blob_properties(timeout=20, retry_total=0)
        except ResourceNotFoundError as exc:
            raise missing_blob_problem(exc) from exc
        except AzureError as exc:
            raise blob_problem(exc) from exc
        return self._receipt(actor, backup_id, properties, version_id)

    def download(self, actor: Actor, backup_id: str, version_id: str, etag: str | None) -> BackupDownload:
        if version_id is None:
            raise Problem(422, "BACKUP_VERSION_REQUIRED", "version_id is required for download")
        receipt = self.receipt(actor, backup_id, version_id)
        require_etag(receipt, etag)
        blob = self._blob(actor, backup_id, version_id)
        try:
            stream = blob.download_blob(
                offset=0, length=receipt.bytes, etag=receipt.etag,
                match_condition=MatchConditions.IfNotModified, max_concurrency=1,
                timeout=20, retry_total=0,
            )
            # The SDK replaces properties.size with the requested range length.
            if stream.properties.content_range != f"bytes 0-{receipt.bytes - 1}/{receipt.bytes}":
                raise Problem(502, "BACKUP_INTEGRITY_ERROR", "download range does not cover the entire backup")
            downloaded_receipt = self._receipt(actor, backup_id, stream.properties, version_id)
            if downloaded_receipt != receipt:
                raise Problem(502, "BACKUP_INTEGRITY_ERROR", "download properties differ from the backup receipt")
            parts = []
            total = 0
            for chunk in stream.chunks():
                total += len(chunk)
                if total > receipt.bytes:
                    raise Problem(502, "BACKUP_INTEGRITY_ERROR", "download exceeds the stored backup size")
                parts.append(chunk)
        except ResourceNotFoundError as exc:
            raise missing_blob_problem(exc) from exc
        except ResourceModifiedError as exc:
            raise Problem(412, "ETAG_CONFLICT", "stored backup ETag changed") from exc
        except AzureError as exc:
            raise blob_problem(exc) from exc
        except ValueError as exc:
            raise Problem(502, "BACKUP_PROTOCOL_ERROR", "storage returned a malformed download response") from exc
        data = b"".join(parts)
        verify_content(receipt, data)
        return BackupDownload(receipt, data)
