"""Remote mode: the same thin console behind Container Apps built-in Entra authentication (ADR-0008 §3).

Container Apps signs the user in before any request reaches this process, so there is no anonymous path.
This module only
- admits the one configured human by the platform's principal header (the API still checks oid→Team owner),
- hands the token-store access token to the unchanged Console/TokenVault checks, and
- copies every committed journal write into one leased Blob before the write returns, so a work POST is only
  sent after its intent is durable and a replaced replica can no longer write.
"""
from __future__ import annotations

import contextvars
import io
import os
import re
import signal
import sqlite3
import sys
import tarfile
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .adapter import ConsoleError, require

# Container Apps injects these after sign-in; outside callers cannot set them (official auth docs).
PRINCIPAL_HEADER = "x-ms-client-principal-id"
NAME_HEADER = "x-ms-client-principal-name"
IDP_HEADER = "x-ms-client-principal-idp"
TOKEN_HEADER = "x-ms-token-aad-access-token"
LEASE_SECONDS = 60
RENEW_SECONDS = 20
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_PLAIN_FILE_BYTES = 65536
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")

_request_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("fa_console_request_token", default=None)


def public_origin(value):
    parsed = urlsplit(value)
    require(parsed.scheme == "https" and bool(parsed.hostname) and parsed.netloc == parsed.hostname
            and not (parsed.path or parsed.query or parsed.fragment), "REMOTE_CONFIGURATION_INVALID", 400)
    return value


@dataclass(frozen=True)
class RemoteBoundary:
    """Platform-authenticated single owner. Admission here is never authorization; the API still decides."""
    origin: str
    owner_oid: str

    def __post_init__(self):
        public_origin(self.origin)
        require(_UUID.fullmatch(self.owner_oid) is not None, "REMOTE_CONFIGURATION_INVALID", 400)

    @property
    def host(self):
        return urlsplit(self.origin).netloc

    def admit(self, headers):
        require(headers.get(PRINCIPAL_HEADER, "").lower() == self.owner_oid and headers.get(IDP_HEADER) == "aad",
                "REMOTE_PRINCIPAL_DENIED", 403)
        _request_token.set(headers.get(TOKEN_HEADER) or None)

    @staticmethod
    def display_name(headers):
        name = headers.get(NAME_HEADER, "")
        return name if 0 < len(name) <= 256 and name.isprintable() else "已指派的帳號"


class EasyAuthTokenProvider:
    """The current request's token-store access token; TokenVault still checks tenant, oid, audience, scope,
    client and expiry, and the API verifies the signature."""

    def __call__(self):
        token = _request_token.get()
        require(isinstance(token, str) and 0 < len(token) <= 16384, "LOGIN_REQUIRED", 401)
        return token


def _valid_name(name):
    parts = PurePosixPath(name).parts
    return (0 < len(parts) <= 4 and "/".join(parts) == name
            and all(_SEGMENT.fullmatch(part) and part not in {".", ".."} for part in parts))


def pack(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(files[name]), 0o600
            archive.addfile(info, io.BytesIO(files[name]))
    return buffer.getvalue()


def unpack(raw):
    require(len(raw) <= MAX_ARCHIVE_BYTES, "REMOTE_STATE_INVALID", 503)
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for member in archive:
                require(member.isreg() and _valid_name(member.name) and member.name not in files,
                        "REMOTE_STATE_INVALID", 503)
                files[member.name] = archive.extractfile(member).read()
    except tarfile.TarError:
        raise ConsoleError("REMOTE_STATE_INVALID", 503) from None
    return files


def _terminate():
    # Uvicorn shuts down gracefully; Container Apps restarts the replica, which then restores the newest state.
    os.kill(os.getpid(), signal.SIGTERM)


def _sqlite_bytes(target):
    with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        return db.serialize()


class LeasedArchive:
    """One Blob holds every journal under ``root``; only the replica holding its lease can write it.

    A starting replica breaks any older lease before it restores, so the older replica's next write fails
    (it cannot start another POST) and it exits; it never overwrites the newer state.
    """

    def __init__(self, blob, root, *, on_lost=_terminate, lease_factory=None):
        self.blob, self.root, self.on_lost = blob, Path(root).resolve(), on_lost
        self.lease_factory = lease_factory
        self.lock = threading.Lock()
        self.snapshots: dict[str, bytes] = {}
        self.lease = None
        self.lost = False
        self.warned = False
        self._stop = threading.Event()

    def start(self, *, renew=True):
        from azure.core.exceptions import HttpResponseError, ResourceExistsError
        from azure.storage.blob import BlobLeaseClient

        factory = self.lease_factory or BlobLeaseClient
        # A leased blob refuses every write without its lease ID (412), even a create-if-missing one; an existing
        # blob is therefore never written here, so a new replica always reaches the takeover below.
        if not self.blob.exists():
            try:
                self.blob.upload_blob(pack({}), overwrite=False)
            except ResourceExistsError:
                pass
            except HttpResponseError as error:
                if error.status_code != 412:
                    raise
        lease = factory(self.blob)
        try:
            lease.acquire(lease_duration=LEASE_SECONDS)
        except HttpResponseError as error:
            if error.status_code != 409:
                raise
            lease.break_lease(lease_break_period=0)
            lease = factory(self.blob)
            lease.acquire(lease_duration=LEASE_SECONDS)
        self.lease = lease
        files = unpack(self.blob.download_blob(lease=lease, max_concurrency=1).readall())
        self.root.mkdir(parents=True, exist_ok=True)
        for name, raw in files.items():
            target = self.root / name
            require(not target.exists(), "REMOTE_STATE_INVALID", 503)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        self.snapshots = files
        if renew:
            threading.Thread(target=self._renew, name="fa-console-lease", daemon=True).start()

    def contains(self, path):
        try:
            return _valid_name(Path(path).resolve().relative_to(self.root).as_posix())
        except ValueError:
            return False

    def persist(self, path):
        """Called after a journal commit; raising stops the caller before any further POST."""
        self._keep(path, lambda target: _sqlite_bytes(target))

    def persist_file(self, path):
        """A small plain file (a new Agent's connection) kept with the journals; same lease rule.

        The file is checked before the lease is used: a refused file is a local error and never stops the replica.
        """
        source = Path(path)
        try:
            raw = (source.read_bytes() if source.is_file() and not source.is_symlink()
                   and source.stat().st_size <= MAX_PLAIN_FILE_BYTES else None)
        except OSError:
            raw = None
        require(raw is not None and len(raw) <= MAX_PLAIN_FILE_BYTES, "REMOTE_STATE_UNAVAILABLE", 503)
        self._keep(path, lambda _target: raw)

    def _keep(self, path, read):
        require(self.contains(path), "REMOTE_STATE_UNAVAILABLE", 503)
        target = Path(path).resolve()
        name = target.relative_to(self.root).as_posix()
        with self.lock:
            require(not self.lost, "REMOTE_STATE_UNAVAILABLE", 503)
            try:
                snapshots = {**self.snapshots, name: read(target)}
                raw = pack(snapshots)
                # Restore refuses an archive over MAX_ARCHIVE_BYTES, so one is never written. The lease is still ours:
                # this is a local refusal (reads keep working), not a takeover, so the replica is not stopped.
                require(len(raw) <= MAX_ARCHIVE_BYTES, "REMOTE_STATE_FULL", 503)
                if len(raw) > MAX_ARCHIVE_BYTES // 2 and not self.warned:
                    self.warned = True
                    print(f"REMOTE_STATE_NEAR_LIMIT: archive {len(raw)} of {MAX_ARCHIVE_BYTES} bytes", file=sys.stderr)
                self.blob.upload_blob(raw, overwrite=True, lease=self.lease)
            except ConsoleError:
                raise
            except Exception:
                self._lose()
                raise ConsoleError("REMOTE_STATE_UNAVAILABLE", 503) from None
            self.snapshots = snapshots

    def _lose(self):
        if not self.lost:
            self.lost = True
            self.on_lost()

    def _renew(self):
        while not self._stop.wait(RENEW_SECONDS):
            try:
                self.lease.renew()
            except Exception:
                with self.lock:
                    self._lose()
                return

    def release(self):
        self._stop.set()
        with self.lock:
            if self.lease is not None and not self.lost:
                try:
                    self.lease.release()
                except Exception:
                    pass
            self.lost = True


def remote_bundle(agents, client_id, *, config_dir="/config", state_dir="/data/state"):
    """Offline rewrite of a reviewed local Agent manifest for ``serve-remote``.

    Only the delegated client (the remote Entra app, never the Azure CLI) and the container paths change; every
    hash is recomputed from the new bytes, so the remote process pins exactly what the operator deployed.
    """
    from .adapter import canonical, parse_config, sha
    from .agent_connections import read_connection
    from .instructions import InstructionConnection

    require(_UUID.fullmatch(client_id) is not None, "REMOTE_CONFIGURATION_INVALID", 400)
    files, entries = {}, []
    for entry in agents.agents:
        config, connection = read_connection(entry)
        default = entry.agent_id == agents.default_agent_id
        stem = "console" if default else "agent-" + sha(entry.agent_id.encode())[:16]
        remote = parse_config(canonical({**config.model_dump(mode="json"), "client_id": client_id}))
        config_raw = canonical(remote.model_dump(mode="json"))
        connection_raw = canonical(InstructionConnection.model_validate(
            {**connection.model_dump(mode="json"), "configuration_sha256": remote.sha256}).model_dump(mode="json"))
        instructions_name = ("instructions" if default else stem + "-instructions") + ".json"
        files[stem + ".json"], files[instructions_name] = config_raw, connection_raw
        entries.append({
            "agent_id": entry.agent_id, "config_path": f"{config_dir}/{stem}.json", "config_sha256": sha(config_raw),
            "journal_path": f"{state_dir}/console.sqlite3" if default else f"{state_dir}/agents/{stem}.sqlite3",
            "instructions_path": f"{config_dir}/{instructions_name}", "instructions_sha256": sha(connection_raw)})
    files["agents.json"] = canonical({"schema_version": "fa-console-agents-v1",
                                      "default_agent_id": agents.default_agent_id, "agents": entries})
    return files
