"""ADR-0009 phase 5a: workspace snapshots for an idle upgrade, kept in the Team's Blob (ADR-0002, ADR-0009 §6 ②).

API-only; ``upgrade_snapshot_flow`` (5b) wires it into U1/U4. Layout:
``fa-upgrade-snapshots/<tenant>/<team>/<agent>/<upgrade_id>/files/NNNN`` per regular file, then ``manifest.json``
written LAST as the publish marker; tenant and Team are also in every blob's metadata and both must match on read. A
prefix without a manifest is incomplete: it is never restored and never resumed (a retry uses a new ``upgrade_id``).
Every blob is create-only; FA never deletes them (a Blob lifecycle rule does, phase 5c). Limits are ``fa-workspace-files-v1``; over a limit the whole snapshot is
④ with a reason and nothing is written.

The caller owns the Team's ContainerClient (credential, close); its single-put/single-get sizes must cover
``MAX_FILE_BYTES``. Not named ``native_*``: the Hosted profile snapshot sweeps those modules.

The container is ``FA_UPGRADE_SNAPSHOT_CONTAINER_URL`` (``snapshot_container_url``), on its own storage account
(Neal 2026-10-17, ``infra/shared-context/upgrade-snapshots.bicep``); unset means phase 5 stays off. There is no
fallback: the no-auto-gc sources/backup account refuses the lifecycle rule that deletes snapshots.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from typing import TYPE_CHECKING, Annotated, Callable, Literal, Sequence

from pydantic import Field, TypeAdapter, ValidationError, model_validator
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from .backup_store import blob_problem
from .behavior_wire import Identifier, WireModel
from .errors import Problem
from .m3_store import _BLOB_REQUEST, _tenant
from .native_files_wire import (FILES_PROFILE, MAX_FILE_ENTRIES, MAX_MANIFEST_BYTES, MAX_TOTAL_BYTES, WORKSPACE_ROOT,
                                NativeFileEntry, check_tree)

if TYPE_CHECKING:
    from azure.storage.blob import ContainerClient

SNAPSHOT_ROOT = "fa-upgrade-snapshots"
SNAPSHOT_FORMAT = "fa-upgrade-snapshot-v1"
MANIFEST_NAME = "manifest.json"
_identifier = TypeAdapter(Identifier)
CONTAINER_URL_ENV = "FA_UPGRADE_SNAPSHOT_CONTAINER_URL"
_CONTAINER_URL = re.compile(r"https://([a-z0-9]{3,24})\.blob\.core\.windows\.net/[a-z0-9](?:[a-z0-9]|-(?!-)){1,61}[a-z0-9]")


def snapshot_container_url(config, environ=None) -> str | None:
    """The dedicated snapshot container URL, or None when unset (phase 5 off). A malformed URL, or one on the
    configured sources or backup account, stops startup: snapshots never fall back to the no-auto-gc account."""
    environ = os.environ if environ is None else environ
    value = environ.get(CONTAINER_URL_ENV) or ""
    if not value:
        return None
    match = _CONTAINER_URL.fullmatch(value)
    if match is None:
        raise ValueError(f"{CONTAINER_URL_ENV} must be https://<account>.blob.core.windows.net/<container>")
    protected = {name for storage in (config.sources, config.backups) for name in (
        storage.account_resource_id.rsplit("/", 1)[-1], urlsplit(storage.protection.container_url).hostname.split(".")[0])}
    if match.group(1) in protected:
        raise ValueError(f"{CONTAINER_URL_ENV} must be its own storage account, never the sources or backup account")
    return value


class UpgradeSnapshotManifest(WireModel):
    format: Literal["fa-upgrade-snapshot-v1"] = SNAPSHOT_FORMAT
    profile: Literal["fa-workspace-files-v1"] = FILES_PROFILE
    root: Literal[".fa-assistant-v12/desktop/files"] = WORKSPACE_ROOT
    team: Identifier
    agent_id: Identifier
    upgrade_id: Identifier
    root_mode: Annotated[int, Field(ge=0, le=0o777)]
    entries: Annotated[tuple[NativeFileEntry, ...], Field(max_length=MAX_FILE_ENTRIES)]

    @model_validator(mode="after")
    def bounded_tree(self):
        check_tree(self.entries)  # the manifest byte bound is save()'s ④ reason and _get's read limit
        return self

    def encoded(self) -> bytes:
        return self.model_dump_json().encode("utf-8")


@dataclass(frozen=True)
class SnapshotResult:
    """``published`` only when ``manifest.json`` was written; otherwise ``reason`` is the ④ reason (a FILES_ERRORS code)."""
    published: bool
    reason: str | None = None
    manifest_sha256: str | None = None


def _refused(reason: str) -> SnapshotResult:
    return SnapshotResult(published=False, reason=reason)


def _not_found() -> Problem:
    return Problem(404, "UPGRADE_SNAPSHOT_NOT_FOUND", "upgrade snapshot file not found")


def _integrity() -> Problem:
    return Problem(502, "UPGRADE_SNAPSHOT_INTEGRITY_ERROR", "stored snapshot content does not match its manifest")


class UpgradeSnapshotStore:
    def __init__(self, container: ContainerClient, tenant_id: str):
        self.container = container
        self.tenant_id = _tenant(tenant_id)

    def _prefix(self, team: str, agent_id: str, upgrade_id: str) -> str:
        try:
            for value in (team, agent_id, upgrade_id):
                _identifier.validate_python(value, strict=True)
        except ValidationError as exc:
            raise Problem(422, "INVALID_UPGRADE_SNAPSHOT", "invalid team, agent ID, or upgrade ID") from exc
        # Tenant and Team are in the name as well as in metadata, like m3/v1/{tenant}/{team}/ (defence in depth).
        return f"{SNAPSHOT_ROOT}/{self.tenant_id}/{team}/{agent_id}/{upgrade_id}/"

    def _put(self, name: str, team: str, data: bytes) -> str:
        from azure.storage.blob import ContentSettings

        sha256 = hashlib.sha256(data).hexdigest()
        try:
            self.container.get_blob_client(name).upload_blob(
                data, length=len(data), overwrite=False, max_concurrency=1, **_BLOB_REQUEST,
                content_settings=ContentSettings(content_type="application/octet-stream"),
                metadata={"tenant_id": self.tenant_id, "team": team, "sha256": sha256, "bytes": str(len(data))},
            )
        except (ResourceExistsError, ResourceModifiedError) as exc:
            raise Problem(409, "UPGRADE_SNAPSHOT_EXISTS", "snapshot prefixes are never resumed; use a new upgrade ID") from exc
        except AzureError as exc:
            raise blob_problem(exc) from exc
        return sha256

    def _get(self, name: str, team: str, limit: int) -> bytes | None:
        """None when the blob is absent or belongs to another tenant/Team; content is checked against its own digest."""
        blob = self.container.get_blob_client(name)
        try:
            stream = blob.download_blob(max_concurrency=1, **_BLOB_REQUEST)
            metadata = stream.properties.metadata or {}
            if metadata.get("tenant_id") != self.tenant_id or metadata.get("team") != team:
                return None
            if stream.properties.size > limit:
                raise _integrity()
            data = stream.readall()
        except ResourceNotFoundError as exc:
            if getattr(exc, "error_code", None) == "BlobNotFound":
                return None
            raise blob_problem(exc) from exc
        except AzureError as exc:
            raise blob_problem(exc) from exc
        if metadata.get("bytes") != str(len(data)) or metadata.get("sha256") != hashlib.sha256(data).hexdigest():
            raise _integrity()
        return data

    def save(self, team: str, agent_id: str, upgrade_id: str, root_mode: int, entries: Sequence[NativeFileEntry],
             read: Callable[[NativeFileEntry], bytes | None]) -> SnapshotResult:
        """Write every file, then the manifest. ``read`` returns an entry's bytes, or None when the source is no longer
        usable (5b: the old session went ``idle``); any exception leaves the prefix without a manifest."""
        prefix = self._prefix(team, agent_id, upgrade_id)
        entries = tuple(entries)
        if len(entries) > MAX_FILE_ENTRIES or sum(entry.byte_length for entry in entries) > MAX_TOTAL_BYTES:
            return _refused("NATIVE_FILES_LIMIT")
        try:
            manifest = UpgradeSnapshotManifest(team=team, agent_id=agent_id, upgrade_id=upgrade_id,
                                               root_mode=root_mode, entries=entries)
        except ValidationError:
            return _refused("NATIVE_FILES_UNSAFE")
        encoded = manifest.encoded()
        if len(encoded) > MAX_MANIFEST_BYTES:
            return _refused("NATIVE_FILES_LIMIT")
        for index, entry in enumerate(manifest.entries):
            if entry.kind != "file":
                continue
            data = read(entry)
            if data is None:
                return _refused("NATIVE_FILES_SOURCE_UNAVAILABLE")
            if len(data) != entry.byte_length or hashlib.sha256(data).hexdigest() != entry.sha256:
                return _refused("NATIVE_FILES_SOURCE_CHANGED")
            self._put(f"{prefix}files/{index:04d}", team, data)
        return SnapshotResult(published=True, manifest_sha256=self._put(prefix + MANIFEST_NAME, team, encoded))

    def manifest(self, team: str, agent_id: str, upgrade_id: str) -> UpgradeSnapshotManifest | None:
        """The published manifest, or None: a prefix without ``manifest.json`` is never restorable."""
        data = self._get(self._prefix(team, agent_id, upgrade_id) + MANIFEST_NAME, team, MAX_MANIFEST_BYTES)
        if data is None:
            return None
        try:
            manifest = UpgradeSnapshotManifest.model_validate_json(data)
        except ValidationError as exc:
            raise _integrity() from exc
        if (manifest.team, manifest.agent_id, manifest.upgrade_id) != (team, agent_id, upgrade_id):
            raise _integrity()
        return manifest

    def read_file(self, manifest: UpgradeSnapshotManifest, index: int) -> bytes:
        """One file of a published manifest, verified against the manifest's digest."""
        if not isinstance(manifest, UpgradeSnapshotManifest) or not 0 <= index < len(manifest.entries):
            raise Problem(422, "INVALID_UPGRADE_SNAPSHOT", "restore must name a file of a published manifest")
        entry = manifest.entries[index]
        if entry.kind != "file":
            raise Problem(422, "INVALID_UPGRADE_SNAPSHOT", "restore must name a file of a published manifest")
        prefix = self._prefix(manifest.team, manifest.agent_id, manifest.upgrade_id)
        data = self._get(f"{prefix}files/{index:04d}", manifest.team, entry.byte_length)
        if data is None:
            raise _not_found()
        if len(data) != entry.byte_length or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise _integrity()
        return data
