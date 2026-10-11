"""M3 檔案版本儲存；只建立不可覆寫的版本，不處理批准或補寫 pending 狀態。

InMemoryArtifactStore 僅供本機測試，不代表 Azure 持久性。
BlobArtifactStore 使用呼叫端持有的 MI ContainerClient，不建立容器、憑證，也不關閉該 client。
put 只允許一次 create-only PUT，再以 native_version HEAD 核對；未知結果只由明確的 stat 核對。
read 固定 native_version 與 ETag，驗證完整範圍、metadata、實際長度及 SHA-256。
每筆最多 512 KiB；client 的 single-put／single-get 門檻須足以容納整筆資料。
公開 request hook 阻止 SDK 補讀、重送或改用分段寫入；不以成功結果掩蓋不完整操作。
"""

from __future__ import annotations

import hashlib
import threading
from typing import TYPE_CHECKING, Callable, Protocol
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from azure.core import MatchConditions
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from .backup_models import canonical_oid
from .backup_store import blob_problem
from .errors import Problem
from .m3_models import BlobReceipt, Digest, MAX_ARTIFACT_BYTES, VersionId
from .models import Identifier

if TYPE_CHECKING:
    from azure.core.pipeline import PipelineRequest
    from azure.storage.blob import BlobClient, BlobProperties, ContainerClient

_identifier = TypeAdapter(Identifier)
_version_id = TypeAdapter(VersionId)
_digest = TypeAdapter(Digest)
_BLOB_REQUEST = {
    "timeout": 20, "connection_timeout": 10, "read_timeout": 20,
    "retry_total": 0, "retry_connect": 0, "retry_read": 0, "retry_status": 0,
}


def _tenant(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("tenant_id must be a canonical UUID")
    try:
        return canonical_oid(value)
    except ValueError as exc:
        raise ValueError("tenant_id must be a canonical UUID") from exc


def _key(team: str, artifact_id: str, version_id: str) -> tuple[str, str, str]:
    try:
        _identifier.validate_python(team, strict=True)
        _identifier.validate_python(artifact_id, strict=True)
        _version_id.validate_python(version_id, strict=True)
    except ValidationError as exc:
        raise Problem(422, "INVALID_ARTIFACT", "invalid team, artifact ID, or logical version ID") from exc
    return team, artifact_id, version_id


def _validate_upload(team: str, artifact_id: str, version_id: str, data: bytes, sha256: str) -> None:
    _key(team, artifact_id, version_id)
    try:
        _digest.validate_python(sha256, strict=True)
    except ValidationError as exc:
        raise Problem(422, "INVALID_ARTIFACT", "invalid artifact SHA-256") from exc
    if not isinstance(data, bytes):
        raise Problem(422, "INVALID_ARTIFACT", "artifact data must be bytes")
    if len(data) > MAX_ARTIFACT_BYTES:
        raise Problem(413, "ARTIFACT_TOO_LARGE", "artifact exceeds 512 KiB")
    if not data:
        raise Problem(422, "EMPTY_ARTIFACT", "artifact must not be empty")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise Problem(422, "ARTIFACT_HASH_MISMATCH", "artifact SHA-256 does not match the data")


def _expected_receipt(receipt: BlobReceipt) -> BlobReceipt:
    if not isinstance(receipt, BlobReceipt):
        raise Problem(422, "INVALID_ARTIFACT_RECEIPT", "a validated blob receipt is required")
    try:
        return BlobReceipt.model_validate(receipt.model_dump())
    except ValidationError as exc:
        raise Problem(422, "INVALID_ARTIFACT_RECEIPT", "invalid native version, ETag, size, or digest") from exc


def _not_found() -> Problem:
    return Problem(404, "ARTIFACT_BLOB_NOT_FOUND", "artifact version not found")


def _missing_blob(exc: ResourceNotFoundError) -> Problem:
    return _not_found() if getattr(exc, "error_code", None) == "BlobNotFound" else blob_problem(exc)


def _protocol_error() -> Problem:
    return Problem(502, "ARTIFACT_BLOB_PROTOCOL_ERROR", "stored artifact properties or native receipt are invalid")


def _single_request(method: str) -> Callable[[PipelineRequest], None]:
    sent = False

    def check(request: PipelineRequest) -> None:
        nonlocal sent
        if sent or request.http_request.method != method:
            raise Problem(502, "ARTIFACT_BLOB_PROTOCOL_ERROR", "additional storage requests are not allowed")
        if method == "PUT" and request.http_request.headers.get("If-None-Match") != "*":
            raise Problem(502, "ARTIFACT_BLOB_PROTOCOL_ERROR", "artifact requires one create-only Put Blob request")
        sent = True

    return check


def _match_receipt(actual: BlobReceipt, expected: BlobReceipt) -> None:
    if actual.etag != expected.etag:
        raise Problem(412, "ETAG_CONFLICT", "supply the exact ETag from the artifact receipt")
    if actual != expected:
        raise Problem(502, "ARTIFACT_INTEGRITY_ERROR", "stored artifact differs from its accepted receipt")


def _verify_content(receipt: BlobReceipt, data: bytes) -> None:
    if len(data) != receipt.bytes or hashlib.sha256(data).hexdigest() != receipt.sha256:
        raise Problem(502, "ARTIFACT_INTEGRITY_ERROR", "artifact content does not match its receipt")


class ArtifactStore(Protocol):
    def put(self, team: str, artifact_id: str, version_id: str, data: bytes, sha256: str) -> BlobReceipt: ...
    def stat(self, team: str, artifact_id: str, version_id: str) -> BlobReceipt: ...
    def read(self, team: str, artifact_id: str, version_id: str, receipt: BlobReceipt) -> bytes: ...


class InMemoryArtifactStore:
    def __init__(self, tenant_id: str):
        self.tenant_id = _tenant(tenant_id)
        self._lock = threading.Lock()
        self._versions: dict[tuple[str, str, str], tuple[BlobReceipt, bytes]] = {}

    def put(self, team: str, artifact_id: str, version_id: str, data: bytes, sha256: str) -> BlobReceipt:
        _validate_upload(team, artifact_id, version_id, data, sha256)
        with self._lock:
            key = (team, artifact_id, version_id)
            if key in self._versions:
                raise Problem(409, "ARTIFACT_VERSION_EXISTS", "artifact version already exists")
            receipt = BlobReceipt(native_version="memory-" + uuid4().hex, etag=f'"memory-{uuid4().hex}"',
                                  sha256=sha256, bytes=len(data))
            self._versions[key] = (receipt, data)
            return receipt.model_copy()

    def _get(self, team: str, artifact_id: str, version_id: str) -> tuple[BlobReceipt, bytes]:
        key = _key(team, artifact_id, version_id)
        with self._lock:
            value = self._versions.get(key)
            if value is None:
                raise _not_found()
            return value

    def stat(self, team: str, artifact_id: str, version_id: str) -> BlobReceipt:
        return self._get(team, artifact_id, version_id)[0].model_copy()

    def read(self, team: str, artifact_id: str, version_id: str, receipt: BlobReceipt) -> bytes:
        expected = _expected_receipt(receipt)
        actual, data = self._get(team, artifact_id, version_id)
        if actual.native_version != expected.native_version:
            raise _not_found()
        _match_receipt(actual, expected)
        _verify_content(expected, data)
        return data


class BlobArtifactStore:
    def __init__(self, container: ContainerClient, tenant_id: str):
        self.container = container
        self.tenant_id = _tenant(tenant_id)

    def _blob(self, team: str, artifact_id: str, version_id: str, native_version: str | None = None) -> BlobClient:
        _key(team, artifact_id, version_id)
        return self.container.get_blob_client(
            f"m3/v1/{self.tenant_id}/{team}/{artifact_id}/{version_id}", version_id=native_version,
        )

    def _receipt(self, team: str, artifact_id: str, version_id: str, properties: BlobProperties,
                 native_version: str | None = None) -> BlobReceipt:
        metadata = properties.metadata
        if not isinstance(metadata, dict):
            raise _protocol_error()
        if metadata.get("tenant_id") != self.tenant_id or metadata.get("team") != team:
            raise _not_found()
        settings = properties.content_settings
        if (metadata.get("artifact_id") != artifact_id or metadata.get("logical_version_id") != version_id
                or metadata.get("bytes") != str(properties.size) or properties.blob_type != "BlockBlob"
                or settings is None or settings.content_type != "application/octet-stream"):
            raise _protocol_error()
        try:
            receipt = BlobReceipt(
                native_version=properties.version_id, etag=properties.etag,
                sha256=metadata.get("sha256"), bytes=properties.size,
            )
        except ValidationError as exc:
            raise _protocol_error() from exc
        if native_version is not None and receipt.native_version != native_version:
            raise _protocol_error()
        return receipt

    def put(self, team: str, artifact_id: str, version_id: str, data: bytes, sha256: str) -> BlobReceipt:
        from azure.storage.blob import ContentSettings

        _validate_upload(team, artifact_id, version_id, data, sha256)
        blob = self._blob(team, artifact_id, version_id)
        try:
            result = blob.upload_blob(
                data, length=len(data), overwrite=False, max_concurrency=1, **_BLOB_REQUEST,
                raw_request_hook=_single_request("PUT"),
                content_settings=ContentSettings(content_type="application/octet-stream"),
                metadata={
                    "tenant_id": self.tenant_id, "team": team, "artifact_id": artifact_id,
                    "logical_version_id": version_id, "sha256": sha256, "bytes": str(len(data)),
                },
            )
        except (ResourceExistsError, ResourceModifiedError) as exc:
            raise Problem(409, "ARTIFACT_VERSION_EXISTS", "artifact version already exists or create condition failed") from exc
        except AzureError as exc:
            raise blob_problem(exc) from exc
        except ValueError as exc:
            raise _protocol_error() from exc
        if not isinstance(result, dict):
            raise _protocol_error()
        try:
            written = BlobReceipt(
                native_version=result.get("version_id"), etag=result.get("etag"), sha256=sha256, bytes=len(data),
            )
        except ValidationError as exc:
            raise _protocol_error() from exc
        confirmed = self._stat(team, artifact_id, version_id, written.native_version)
        if confirmed != written:
            raise Problem(502, "ARTIFACT_INTEGRITY_ERROR", "stored artifact differs from its upload acknowledgement")
        return confirmed

    def _stat(self, team: str, artifact_id: str, version_id: str, native_version: str | None = None) -> BlobReceipt:
        blob = self._blob(team, artifact_id, version_id, native_version)
        try:
            properties = blob.get_blob_properties(raw_request_hook=_single_request("HEAD"), **_BLOB_REQUEST)
        except ResourceNotFoundError as exc:
            raise _missing_blob(exc) from exc
        except AzureError as exc:
            raise blob_problem(exc) from exc
        except ValueError as exc:
            raise _protocol_error() from exc
        return self._receipt(team, artifact_id, version_id, properties, native_version)

    def stat(self, team: str, artifact_id: str, version_id: str) -> BlobReceipt:
        return self._stat(team, artifact_id, version_id)

    def read(self, team: str, artifact_id: str, version_id: str, receipt: BlobReceipt) -> bytes:
        expected = _expected_receipt(receipt)
        actual = self._stat(team, artifact_id, version_id, expected.native_version)
        _match_receipt(actual, expected)
        blob = self._blob(team, artifact_id, version_id, expected.native_version)
        try:
            stream = blob.download_blob(
                offset=0, length=expected.bytes, etag=expected.etag,
                match_condition=MatchConditions.IfNotModified, max_concurrency=1, **_BLOB_REQUEST,
                raw_request_hook=_single_request("GET"),
            )
            # The SDK replaces properties.size with the requested range length.
            if stream.properties.content_range != f"bytes 0-{expected.bytes - 1}/{expected.bytes}":
                raise Problem(502, "ARTIFACT_INTEGRITY_ERROR", "download range does not cover the entire artifact")
            downloaded = self._receipt(team, artifact_id, version_id, stream.properties, expected.native_version)
            _match_receipt(downloaded, expected)
            parts, total = [], 0
            for chunk in stream.chunks():
                total += len(chunk)
                if total > expected.bytes:
                    raise Problem(502, "ARTIFACT_INTEGRITY_ERROR", "download exceeds the accepted artifact size")
                parts.append(chunk)
        except ResourceNotFoundError as exc:
            raise _missing_blob(exc) from exc
        except ResourceModifiedError as exc:
            raise Problem(412, "ETAG_CONFLICT", "stored artifact ETag changed") from exc
        except AzureError as exc:
            raise blob_problem(exc) from exc
        except ValueError as exc:
            raise _protocol_error() from exc
        data = b"".join(parts)
        _verify_content(expected, data)
        return data
