from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
import zlib
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .backup_models import MAX_BACKUP_BYTES, BackupReceipt
from .errors import Problem
from .private_files import guard, load_json, read, write

MAX_FILES = 2000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_TAR_BYTES = MAX_FILE_BYTES + MAX_MANIFEST_BYTES + MAX_FILES * 2048 + 10240
MANIFEST_NAME = "archive-manifest.json"
BASE_FILES = {"work.txt", "tool-settings.json", "checkpoint.json"}
PROFILE_LOCKS = {"SingletonLock", "SingletonCookie", "SingletonSocket"}


def _valid_name(name: str) -> bool:
    if not isinstance(name, str) or not 0 < len(name) <= 1024 or "\\" in name or ":" in name:
        return False
    path = PurePosixPath(name)
    return (
        not path.is_absolute() and path.as_posix() == name and ".." not in path.parts
        and not any(ord(char) < 32 or ord(char) == 127 for char in name)
        and (name in BASE_FILES or (len(path.parts) > 1 and path.parts[0] == "profile"))
    )


def _fingerprint(data: bytes) -> dict:
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def create_archive(home: Path, root: Path, run_id: str, binding: dict, checkpoint: bytes, saved: dict):
    files = {}
    total = 0
    for lock in PROFILE_LOCKS:
        candidate = root / "profile" / lock
        if candidate.exists() or candidate.is_symlink():
            raise Problem(409, "ENVIRONMENT_PROFILE_BUSY", "browser ownership files remain; no live-profile backup")
    for path in sorted(root.rglob("*")):
        guard(home, path)
        if path.is_dir():
            continue
        name = path.relative_to(root).as_posix()
        if not _valid_name(name):
            raise Problem(409, "UNSAFE_BACKUP_PATH", "private run contains a file outside the approved backup layout")
        if len(files) >= MAX_FILES:
            raise Problem(413, "BACKUP_TOO_MANY_FILES", "private backup exceeds its file-count bound")
        data = read(home, path, MAX_FILE_BYTES - total)
        total += len(data)
        files[name] = data
    if not BASE_FILES <= files.keys() or "profile/Default/Preferences" not in files:
        raise Problem(409, "BACKUP_INCOMPLETE", "private backup is missing required environment files")
    if files["checkpoint.json"] != checkpoint:
        raise Problem(409, "ENVIRONMENT_CHECKPOINT_CHANGED", "checkpoint changed while preparing the backup")
    if any(_fingerprint(files[name]) != saved["files"].get(name) for name in ("work.txt", "tool-settings.json")):
        raise Problem(409, "ENVIRONMENT_FILES_CHANGED", "private work or settings changed while preparing the backup")
    manifest = {
        "schema_version": 1, "run_id": run_id, "binding": binding,
        "checkpoint_sha256": hashlib.sha256(checkpoint).hexdigest(),
        "file_bytes": total, "files": {name: _fingerprint(data) for name, data in files.items()},
    }
    metadata = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if len(metadata) > MAX_MANIFEST_BYTES:
        raise Problem(413, "BACKUP_MANIFEST_TOO_LARGE", "backup manifest exceeds its bounded size")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in [(MANIFEST_NAME, metadata), *files.items()]:
            item = tarfile.TarInfo(name)
            item.size, item.mode, item.mtime = len(data), 0o600, 0
            archive.addfile(item, io.BytesIO(data))
    if output.tell() > MAX_TAR_BYTES:
        raise Problem(413, "BACKUP_TOO_LARGE", "uncompressed backup exceeds its bounded size")
    content = gzip.compress(output.getvalue(), mtime=0)
    if len(content) > MAX_BACKUP_BYTES:
        raise Problem(413, "BACKUP_TOO_LARGE", "compressed backup exceeds 8 MiB")
    return content, manifest


def _decode(content: bytes) -> tuple[dict, dict[str, bytes]]:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as compressed:
            raw = compressed.read(MAX_TAR_BYTES + 1)
        if len(raw) > MAX_TAR_BYTES:
            raise Problem(413, "BACKUP_TOO_LARGE", "expanded archive exceeds its bounded size")
        files = {}
        total = 0
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for item in archive:
                if (not item.isfile() or item.mode & 0o7111 or item.size < 0
                        or (item.name != MANIFEST_NAME and not _valid_name(item.name))
                        or item.name in files):
                    raise Problem(409, "UNSAFE_BACKUP_ARCHIVE", "archive has duplicate or unsafe names, types or modes")
                if len(files) > MAX_FILES:
                    raise Problem(413, "BACKUP_TOO_MANY_FILES", "archive exceeds its file-count bound")
                limit = MAX_MANIFEST_BYTES if item.name == MANIFEST_NAME else MAX_FILE_BYTES - total
                if item.size > limit:
                    raise Problem(413, "BACKUP_TOO_LARGE", "archive member exceeds its bounded size")
                stream = archive.extractfile(item)
                if stream is None:
                    raise Problem(409, "INVALID_BACKUP_ARCHIVE", "archive member cannot be read")
                with stream:
                    data = stream.read(item.size + 1)
                if len(data) != item.size:
                    raise Problem(409, "INVALID_BACKUP_ARCHIVE", "archive member length differs")
                files[item.name] = data
                if item.name != MANIFEST_NAME:
                    total += len(data)
    except (OSError, EOFError, tarfile.TarError, ValueError, zlib.error) as exc:
        raise Problem(409, "INVALID_BACKUP_ARCHIVE", f"archive decoding failed: {type(exc).__name__}") from exc
    if MANIFEST_NAME not in files:
        raise Problem(409, "BACKUP_INCOMPLETE", "archive manifest is missing")
    manifest = load_json(files.pop(MANIFEST_NAME))
    if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
            or not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != set(files)
            or manifest.get("file_bytes") != total or not BASE_FILES <= files.keys()
            or "profile/Default/Preferences" not in files):
        raise Problem(409, "INVALID_BACKUP_MANIFEST", "archive inventory or schema differs from its manifest")
    if any(_fingerprint(data) != manifest["files"][name] for name, data in files.items()):
        raise Problem(409, "BACKUP_CONTENT_CHANGED", "archive file differs from its recorded bytes/hash")
    return manifest, files


def restore_archive(home: Path, root: Path, run_id: str, binding: dict, receipt: BackupReceipt, content: bytes) -> dict:
    if len(content) != receipt.bytes or hashlib.sha256(content).hexdigest() != receipt.sha256:
        raise Problem(409, "BACKUP_CONTENT_CHANGED", "archive differs from its independently retained receipt")
    manifest, files = _decode(content)
    source = manifest.get("binding")
    if (not isinstance(source, dict) or set(source) != set(binding)
            or any(not isinstance(value, str) or not 0 < len(value) <= 256 for value in source.values())):
        raise Problem(409, "INVALID_BACKUP_MANIFEST", "source binding is invalid")
    if source["agent_id"] != binding["agent_id"] or source["agent_name"] != binding["agent_name"]:
        raise Problem(403, "PRIVATE_ENVIRONMENT_FORBIDDEN", "backup belongs to a different agent")
    if manifest.get("run_id") != run_id:
        raise Problem(409, "ENVIRONMENT_BINDING_CHANGED", "backup belongs to a different run")
    checkpoint = load_json(files["checkpoint.json"])
    source_hash = hashlib.sha256(files["checkpoint.json"]).hexdigest()
    if (source_hash != manifest.get("checkpoint_sha256") or checkpoint.get("binding") != source
            or checkpoint.get("run_id") != run_id or checkpoint.get("schema_version") != 1
            or checkpoint.get("probe_scope", "environment") != "environment"
            or not isinstance(checkpoint.get("files"), dict)
            or any(checkpoint["files"].get(name) != _fingerprint(files[name]) for name in ("work.txt", "tool-settings.json"))):
        raise Problem(409, "INVALID_BACKUP_MANIFEST", "source checkpoint does not match the archive")
    if load_json(files["tool-settings.json"]) != {"python_utf8": "1"}:
        raise Problem(409, "ENVIRONMENT_SETTINGS_CHANGED", "backup has unexpected tool settings")
    preferences = load_json(files["profile/Default/Preferences"])
    if not isinstance(preferences.get("intl"), dict) or preferences["intl"].get("accept_languages") != "zh-TW":
        raise Problem(409, "ENVIRONMENT_PROFILE_CHANGED", "backup has unexpected browser language settings")
    restored_from = {
        "backup_id": receipt.backup_id, "backup_sha256": receipt.sha256, "backup_version_id": receipt.version_id,
        "source_binding": source, "source_checkpoint_sha256": source_hash,
    }
    checkpoint = {**checkpoint, "binding": binding, "restored_from": restored_from}
    new_checkpoint = json.dumps(checkpoint, sort_keys=True, separators=(",", ":")).encode()
    if len(new_checkpoint) > 8192:
        raise Problem(413, "ENVIRONMENT_STATE_TOO_LARGE", "restored checkpoint exceeds the normal reader bound")
    guard(home, root)
    root.parent.mkdir(mode=0o700, exist_ok=True)
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise Problem(409, "ENVIRONMENT_ALREADY_SEEDED", "restore never overwrites an existing run") from exc
    for name, data in files.items():
        if name == "checkpoint.json":
            continue
        destination = root
        parts = PurePosixPath(name).parts
        for part in parts[:-1]:
            destination /= part
            guard(home, destination)
            destination.mkdir(mode=0o700, exist_ok=True)
        write(home, destination / parts[-1], data)
    temporary = root / (".checkpoint-" + uuid4().hex + ".tmp")
    write(home, temporary, new_checkpoint)
    # Atomic no-clobber publication. A failed restore retains its new partial run.
    os.link(temporary, root / "checkpoint.json", follow_symlinks=False)
    temporary.unlink()
    return {
        "binding": binding, "source_binding": source, "private_root": str(root),
        "checkpoint_sha256": hashlib.sha256(new_checkpoint).hexdigest(), "files": checkpoint["files"],
        "restored_from": restored_from, "restored_files": len(files),
        "gui_invoked": False, "hosted_continuity_validated": False,
        "scope_note": "Files restored from a pinned cloud version; a separate real browser/tool read is still required.",
    }
