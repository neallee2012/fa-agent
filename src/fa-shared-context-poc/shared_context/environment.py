from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from hosted_bootstrap import RECEIPT_ENV, process_identity

from .errors import Problem
from .gui import GuiProbe, run_gui_probe
from .models import Identifier, StrictModel
from .backup_models import BackupReceipt
from .private_files import guard as _guard, read as _read, write as _write, load_json as _json

ENVIRONMENT_LOCK = threading.Lock()
SETTINGS = {"python_utf8": "1"}
LANGUAGE = "zh-TW"
FILES = ("work.txt", "tool-settings.json")


class EnvironmentProbe(StrictModel):
    operation: Literal["environment"]
    action: Literal["inspect", "seed", "read", "seed_file", "read_file", "backup", "restore"]
    target_agent_id: Annotated[str, Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
    run_id: Identifier
    nonce: Identifier | None = None
    checkpoint_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    include_screenshot: bool = False
    backup_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None
    backup: BackupReceipt | None = None

    @model_validator(mode="after")
    def action_fields(self):
        if self.action == "backup":
            if (self.backup_id is None or self.checkpoint_sha256 is None or self.nonce is not None
                    or self.backup is not None or self.include_screenshot):
                raise ValueError("backup requires an ID and current checkpoint, not new content or screenshots")
            return self
        if self.action == "restore":
            if (self.backup is None or self.backup_id is not None or self.checkpoint_sha256 is not None
                    or self.nonce is not None or self.include_screenshot):
                raise ValueError("restore requires the independently retained backup receipt only")
            return self
        if self.backup is not None or self.backup_id is not None:
            raise ValueError("backup fields are only accepted by backup/restore")
        if self.action in ("seed", "seed_file"):
            if self.nonce is None or self.checkpoint_sha256 is not None:
                raise ValueError("seed requires a nonce and no checkpoint")
        elif self.action in ("read", "read_file"):
            if self.checkpoint_sha256 is None or self.nonce is not None:
                raise ValueError("read requires the independently retained checkpoint hash, not a nonce")
        elif self.nonce is not None or self.checkpoint_sha256 is not None:
            raise ValueError("inspect accepts no synthetic content or checkpoint")
        if self.action in ("seed_file", "read_file") and self.include_screenshot:
            raise ValueError("file-only probes do not take screenshots")
        return self


def _binding(command: EnvironmentProbe) -> dict:
    fields = {
        "agent_id": "FOUNDRY_AGENT_ID", "agent_name": "FOUNDRY_AGENT_NAME",
        "agent_version": "FOUNDRY_AGENT_VERSION", "session_id": "FOUNDRY_AGENT_SESSION_ID",
    }
    result = {key: os.environ.get(variable, "") for key, variable in fields.items()}
    try:
        if (not all(result.values()) or any(len(value) > 256 for value in result.values())
                or str(UUID(result["agent_id"])) != result["agent_id"]):
            raise ValueError("missing platform binding")
    except ValueError as exc:
        raise Problem(503, "ENVIRONMENT_BINDING_UNAVAILABLE", "trusted Hosted identity and session are required") from exc
    if command.target_agent_id != result["agent_id"]:
        raise Problem(403, "PRIVATE_ENVIRONMENT_FORBIDDEN", "a different agent's private environment is not accessible")
    if platform.system() != "Linux" or os.geteuid() == 0:
        raise Problem(503, "ENVIRONMENT_RUNTIME_UNAVAILABLE", "M1 requires a non-root Linux Hosted runtime")
    return result


def _fingerprints(home: Path, root: Path, names: tuple[str, ...] = FILES) -> dict:
    result = {}
    for name in names:
        data = _read(home, root / name)
        result[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    return result


def _checkpoint(home: Path, root: Path, command: EnvironmentProbe, binding: dict, scope: str) -> tuple[bytes, dict]:
    raw = _read(home, root / "checkpoint.json")
    if hashlib.sha256(raw).hexdigest() != command.checkpoint_sha256:
        raise Problem(409, "ENVIRONMENT_CHECKPOINT_CHANGED", "checkpoint differs from the independently retained receipt")
    manifest = _json(raw)
    saved_binding = manifest.get("binding")
    if (manifest.get("schema_version") != 1 or manifest.get("run_id") != command.run_id
            or not isinstance(saved_binding, dict) or saved_binding.get("agent_id") != binding["agent_id"]):
        raise Problem(409, "ENVIRONMENT_BINDING_CHANGED", "checkpoint identity or run does not match")
    if manifest["binding"] != binding:
        raise Problem(409, "ENVIRONMENT_RESTORE_REQUIRED", "replacement sessions and versions need an approved restore contract")
    if manifest.get("probe_scope", "environment") != scope:
        raise Problem(409, "ENVIRONMENT_SCOPE_CHANGED", "checkpoint belongs to a different environment probe")
    return raw, manifest


def _file_only(home: Path, root: Path, command: EnvironmentProbe, binding: dict) -> dict:
    if command.action == "seed_file":
        root.parent.mkdir(mode=0o700, exist_ok=True)
        try:
            root.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise Problem(409, "ENVIRONMENT_ALREADY_SEEDED", "this run already exists; do not retry an uncertain seed") from exc
        _write(home, root / "work.txt", f"m1 synthetic work: {command.nonce}\n".encode())
        manifest = {
            "schema_version": 1, "probe_scope": "file-only", "binding": binding,
            "run_id": command.run_id, "files": _fingerprints(home, root, ("work.txt",)),
        }
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        _write(home, root / "checkpoint.json", raw)
    else:
        raw, manifest = _checkpoint(home, root, command, binding, "file-only")
        if manifest.get("files") != _fingerprints(home, root, ("work.txt",)):
            raise Problem(409, "ENVIRONMENT_FILES_CHANGED", "private file differs from the saved checkpoint")
    receipt = os.environ.get(RECEIPT_ENV)
    if receipt is not None and len(receipt.encode()) > 4096:
        raise Problem(503, "ENVIRONMENT_BOOTSTRAP_INVALID", "bootstrap receipt exceeds its bounded size")
    try:
        identity = process_identity()
    except (OSError, RuntimeError, ValueError) as exc:
        raise Problem(503, "ENVIRONMENT_IDENTITY_UNAVAILABLE", "process credentials/capabilities could not be observed") from exc
    info = (root / "work.txt").stat()
    return {
        "binding": binding, "probe_scope": "file-only", "private_root": str(root),
        "checkpoint_sha256": hashlib.sha256(raw).hexdigest(), "files": manifest["files"],
        "file_attributes": {"uid": info.st_uid, "gid": info.st_gid, "mode": oct(stat.S_IMODE(info.st_mode))},
        "process_identity": identity, "bootstrap": _json(receipt.encode()) if receipt is not None else None,
        "gui_invoked": False, "hosted_continuity_validated": False,
        "scope_note": "Private file only; no browser, profile, tool settings, replacement-session or SDK-work restore.",
    }


def _exercise(home: Path, root: Path, command: EnvironmentProbe, seed_marker: str | None = None) -> tuple[dict, dict]:
    settings = _json(_read(home, root / "tool-settings.json"))
    if settings != SETTINGS:
        raise Problem(409, "ENVIRONMENT_SETTINGS_CHANGED", "tool settings no longer match the M1 contract")
    profile = root / "profile"
    preferences = _json(_read(home, profile / "Default" / "Preferences", 512 * 1024))
    international = preferences.get("intl")
    if not isinstance(international, dict) or international.get("accept_languages") != LANGUAGE:
        raise Problem(409, "ENVIRONMENT_PROFILE_CHANGED", "the saved browser language preference changed")
    applied = subprocess.run(
        [sys.executable, "-c", "import sys; print(sys.flags.utf8_mode)"],
        env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "PYTHONUTF8": settings["python_utf8"]},
        capture_output=True, text=True, timeout=10, check=True,
    )
    if applied.stdout.strip() != "1":
        raise Problem(502, "ENVIRONMENT_SETTINGS_NOT_APPLIED", "the real tool process did not apply UTF-8 mode")
    gui = run_gui_probe(GuiProbe(
        operation="gui", run_id=command.run_id, nonce=uuid4().hex,
        include_screenshot=command.include_screenshot,
    ), persistent_profile=profile, seed_profile_marker=seed_marker)
    if (gui.get("probe_status") != "passed" or not gui.get("cleanup", {}).get("success")
            or gui.get("profile_closed_cleanly") is not True):
        raise Problem(
            502, "ENVIRONMENT_BROWSER_FAILED", "private-profile browser validation failed; private files retained",
            gui_error=gui.get("error"), diagnostics_summary=gui.get("diagnostics"), cleanup=gui.get("cleanup"),
        )
    language = gui.get("accept_language", "").split(",", 1)[0].strip()
    if language != LANGUAGE:
        raise Problem(502, "ENVIRONMENT_PROFILE_NOT_APPLIED", "Chromium did not send the saved language preference")
    versions = gui.get("versions")
    if (not isinstance(versions, dict) or set(versions) != {"chromium", "openbox", "xvfb"}
            or any(not isinstance(value, str) or not value.strip() or len(value) > 2048 for value in versions.values())):
        raise Problem(502, "ENVIRONMENT_VERSIONS_UNAVAILABLE", "the browser toolchain versions were not recorded")
    return gui, {"python": platform.python_version(), **versions}


def _run(command: EnvironmentProbe, backup_client=None) -> dict:
    binding = _binding(command)
    configured_home = os.environ.get("HOME", "")
    home = Path(configured_home)
    if not configured_home or not home.is_absolute() or ".." in home.parts or not home.is_dir():
        raise Problem(503, "ENVIRONMENT_HOME_UNAVAILABLE", "platform HOME must be an existing absolute directory")
    _guard(home, home)
    key = hashlib.sha256(f"{binding['agent_id']}:{command.run_id}".encode()).hexdigest()[:20]
    base, root = home / ".fa-m1", home / ".fa-m1" / key
    _guard(home, root)
    if command.action == "inspect":
        info = home.stat()
        return {
            "binding": binding, "home": str(home), "effective_uid": os.geteuid(),
            "home_owner_uid": info.st_uid, "home_mode": oct(stat.S_IMODE(info.st_mode)),
            "candidate_root": str(root), "seeded": (root / "checkpoint.json").is_file(),
            "home_write_validated": False, "hosted_continuity_validated": False,
        }
    if command.action in ("seed_file", "read_file"):
        return _file_only(home, root, command, binding)
    if command.action in ("backup", "restore"):
        if backup_client is None:
            raise Problem(503, "BACKUP_UNAVAILABLE", "cloud backup transport is required")
        from .environment_backup import create_archive, restore_archive

        if command.action == "restore":
            if root.exists():
                raise Problem(409, "ENVIRONMENT_ALREADY_SEEDED", "restore never overwrites an existing run")
            content = backup_client.get(command.backup)
            return restore_archive(home, root, command.run_id, binding, command.backup, content)
        verified = _run(command.model_copy(update={"action": "read", "backup_id": None}))
        raw, manifest = _checkpoint(home, root, command, binding, "environment")
        archive, archive_manifest = create_archive(home, root, command.run_id, binding, raw, manifest)
        receipt = backup_client.put(command.backup_id, archive)
        return {
            "binding": binding, "backup": receipt.model_dump(), "checkpoint_sha256": verified["checkpoint_sha256"],
            "files": verified["files"], "profile": verified["profile"], "tool_versions": verified["tool_versions"],
            "gui": verified["gui"], "settings_applied": verified["settings_applied"],
            "archive_files": len(archive_manifest["files"]), "archive_uncompressed_bytes": archive_manifest["file_bytes"],
            "profile_closed_cleanly": True, "hosted_continuity_validated": False,
        }
    if command.action == "seed":
        base.mkdir(mode=0o700, exist_ok=True)
        try:
            root.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise Problem(409, "ENVIRONMENT_ALREADY_SEEDED", "this run already exists; do not retry an uncertain seed") from exc
        _write(home, root / "work.txt", f"m1 synthetic work: {command.nonce}\n".encode())
        _write(home, root / "tool-settings.json", json.dumps(SETTINGS, sort_keys=True).encode())
        (root / "profile" / "Default").mkdir(parents=True, mode=0o700)
        _write(home, root / "profile" / "Default" / "Preferences", json.dumps({
            "intl": {"accept_languages": LANGUAGE},
        }).encode())
        marker = uuid4().hex
        gui, versions = _exercise(home, root, command, marker)
        marker_hash = hashlib.sha256(marker.encode()).hexdigest()
        if gui.get("profile_marker_sha256") != marker_hash:
            raise Problem(502, "ENVIRONMENT_COOKIE_NOT_APPLIED", "Chromium did not return the synthetic persistent cookie")
        manifest = {
            "schema_version": 1, "binding": binding, "run_id": command.run_id,
            "files": _fingerprints(home, root), "tool_versions": versions, "profile_language": LANGUAGE,
            "profile_marker_sha256": marker_hash,
        }
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        _write(home, root / "checkpoint.json", raw)
    else:
        raw, manifest = _checkpoint(home, root, command, binding, "environment")
        if manifest.get("files") != _fingerprints(home, root):
            raise Problem(409, "ENVIRONMENT_FILES_CHANGED", "private files differ from the saved checkpoint")
        gui, versions = _exercise(home, root, command)
        if gui.get("profile_marker_sha256") != manifest.get("profile_marker_sha256"):
            raise Problem(409, "ENVIRONMENT_COOKIE_MISSING", "Chromium did not restore the browser-owned cookie")
        if versions != manifest.get("tool_versions"):
            raise Problem(409, "ENVIRONMENT_TOOLS_CHANGED", "toolchain differs from the recorded image baseline")
    return {
        "binding": binding, "private_root": str(root), "checkpoint_sha256": hashlib.sha256(raw).hexdigest(),
        "files": manifest["files"], "tool_versions": versions, "settings_applied": {"python_utf8": 1},
        "profile": {
            "path": str(root / "profile"), "accept_language": gui["accept_language"],
            "marker_sha256": gui["profile_marker_sha256"],
        },
        "gui": gui, "hosted_continuity_validated": False,
        "scope_note": "A single invocation is not stop/resume, replacement-session, or autonomous OS UI proof.",
    }


def run_environment_probe(command: EnvironmentProbe, *, backup_client=None) -> dict:
    if not ENVIRONMENT_LOCK.acquire(blocking=False):
        raise Problem(409, "ENVIRONMENT_BUSY", "one private-environment operation is already running")
    try:
        return _run(command, backup_client)
    except FileNotFoundError:
        raise Problem(404, "ENVIRONMENT_NOT_FOUND", "private fixture or checkpoint does not exist") from None
    except PermissionError as exc:
        raise Problem(
            503, "ENVIRONMENT_HOME_NOT_WRITABLE", "private HOME access failed; no privilege or temporary-directory fallback",
            operation=command.action, error_type=type(exc).__name__,
            errno=exc.errno if type(exc.errno) is int else None,
        ) from None
    except (OSError, subprocess.SubprocessError) as exc:
        error_number, return_code = getattr(exc, "errno", None), getattr(exc, "returncode", None)
        raise Problem(
            503, "ENVIRONMENT_IO_FAILED", f"private environment operation failed: {type(exc).__name__}",
            operation=command.action, errno=error_number if type(error_number) is int else None,
            returncode=return_code if type(return_code) is int else None,
        ) from None
    finally:
        ENVIRONMENT_LOCK.release()
