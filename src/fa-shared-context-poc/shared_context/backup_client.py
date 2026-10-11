from __future__ import annotations

import hashlib
import json

import httpx
from azure.core.exceptions import AzureError
from pydantic import ValidationError

from .backup_models import MAX_BACKUP_BYTES, BackupReceipt
from .errors import Problem


_PUBLIC_BACKUP_ERRORS = frozenset({
    "ACTOR_NOT_ALLOWED", "AUTH_KEYS_UNAVAILABLE", "CROSS_TEAM_DENIED", "UNAUTHENTICATED",
    "BACKUP_CONTENT_ENCODING", "BACKUP_CONTENT_TYPE", "BACKUP_EXISTS", "BACKUP_HASH_MISMATCH",
    "BACKUP_INCOMPLETE", "BACKUP_INTEGRITY_ERROR", "BACKUP_NOT_FOUND", "BACKUP_PROTOCOL_ERROR",
    "BACKUP_TOO_LARGE", "BACKUP_VERSION_REQUIRED", "EMPTY_BACKUP", "ETAG_CONFLICT", "ETAG_REQUIRED",
    "INVALID_BACKUP", "INVALID_REQUEST", "REQUEST_TOO_LARGE", "UPSTREAM_ERROR",
})


class BackupClient:
    def __init__(self, api_url, api_audience, credential, http):
        self.api_url, self.scope = api_url, api_audience + "/.default"
        self.credential, self.http = credential, http

    def _request(self, method, backup_id, *, data=None, receipt=None):
        try:
            token = self.credential.get_token(self.scope).token
        except AzureError:
            raise Problem(503, "BACKUP_AUTHENTICATION_FAILED",
                          "backup authentication is unavailable; retain the original backup reference") from None
        headers = {"Authorization": f"Bearer {token}"}
        params = {}
        if data is not None:
            headers.update({"Content-Type": "application/octet-stream", "X-Backup-SHA256": hashlib.sha256(data).hexdigest()})
        if receipt is not None:
            params["version_id"] = receipt.version_id
            headers["If-Match"] = receipt.etag
        try:
            with self.http.stream(
                method, self.api_url + "/private-environments/backups/" + backup_id,
                params=params, headers=headers, content=data, timeout=httpx.Timeout(70, connect=10),
            ) as response:
                success = response.status_code == (201 if method == "PUT" else 200)
                limit = receipt.bytes if success and receipt is not None else 8192
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > limit:
                        raise Problem(502, "BACKUP_RESPONSE_TOO_LARGE", "backup response exceeds its declared bound")
                    content.extend(chunk)
                if not success:
                    try:
                        body = json.loads(content)
                    except (ValueError, UnicodeDecodeError):
                        body = {}
                    error = body.get("error", {}) if isinstance(body, dict) else {}
                    code = error.get("code") if isinstance(error, dict) else None
                    status = response.status_code
                    raise Problem(
                        status if status in (400, 401, 403, 404, 409, 412, 413, 422, 429, 503) else 502,
                        "ENVIRONMENT_BACKUP_FAILED", "cloud backup request failed; no local fallback",
                        upstream_status=status,
                        upstream_code=code if isinstance(code, str) and code in _PUBLIC_BACKUP_ERRORS else None,
                    )
                return bytes(content), response.headers
        except httpx.HTTPError:
            raise Problem(502, "BACKUP_TRANSPORT_FAILED",
                          "backup transport outcome is unresolved; retain the original backup reference") from None

    def put(self, backup_id: str, data: bytes) -> BackupReceipt:
        if not 0 < len(data) <= MAX_BACKUP_BYTES:
            raise Problem(413, "BACKUP_TOO_LARGE", "compressed backup exceeds its size bound")
        raw, _ = self._request("PUT", backup_id, data=data)
        try:
            receipt = BackupReceipt.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            raise Problem(502, "BACKUP_RECEIPT_INVALID", "cloud backup returned an invalid receipt") from exc
        if (receipt.backup_id != backup_id or receipt.bytes != len(data)
                or receipt.sha256 != hashlib.sha256(data).hexdigest()):
            raise Problem(502, "BACKUP_RECEIPT_MISMATCH", "cloud receipt differs from the independently hashed upload")
        return receipt

    def get(self, receipt: BackupReceipt) -> bytes:
        raw, headers = self._request("GET", receipt.backup_id, receipt=receipt)
        if (len(raw) != receipt.bytes or hashlib.sha256(raw).hexdigest() != receipt.sha256
                or headers.get("X-Backup-SHA256") != receipt.sha256
                or headers.get("X-Backup-Version-Id") != receipt.version_id
                or headers.get("ETag") != receipt.etag):
            raise Problem(409, "BACKUP_CONTENT_CHANGED", "cloud version, ETag, bytes or hash differs from the retained receipt")
        return raw
