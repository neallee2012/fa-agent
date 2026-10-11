from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import os
import platform
import re
import socket
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from desktop_smoke import run_desktop_smoke

if TYPE_CHECKING:
    from smb_oauth import SmbOAuthSession

SCHEMA_VERSION = "1.0"
DISPATCH_LOCK = threading.RLock()
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
KERNEL_READ_BYTES = 65536
KERNEL_MAX_MESSAGES = 16
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
OPERATIONS = {
    "probe_environment",
    "mount_shared",
    "resume_recover",
    "shared_io_step",
    "desktop_smoke",
    "status",
}


class PocError(Exception):
    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "reason_code": self.reason_code,
            "message": self.message,
            "details": self.details,
        }


def credential_format(key: str) -> dict[str, bool]:
    checks = {
        "has_quotes": '"' in key or "'" in key,
        "has_crlf": "\r" in key or "\n" in key,
        "has_nul": "\x00" in key,
        "has_whitespace": any(character.isspace() for character in key),
        "valid_base64": False,
        "valid_64_byte_base64": False,
    }
    try:
        decoded = base64.b64decode(key, validate=True)
        checks["valid_base64"] = True
        checks["valid_64_byte_base64"] = len(decoded) == 64
    except (binascii.Error, ValueError):
        pass
    return checks


def diagnostic_text(text: str | bytes | None, key: str, limit: int = 4096, *, tail: bool = False) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = (text or "").replace(key, "<REDACTED>") if key else (text or "")
    encoded = text.encode("utf-8")
    return (encoded[-limit:] if tail else encoded[:limit]).decode("utf-8", errors="ignore")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise PocError("INVALID_REQUEST", "relative_path must be a non-empty string")

    normalized = relative_path.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise PocError("PATH_OUTSIDE_RUN", "absolute paths are not allowed")

    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PocError("PATH_OUTSIDE_RUN", "relative_path contains an unsafe segment")

    resolved_root = root.resolve()
    candidate = root.joinpath(*parts)
    resolved_candidate = candidate.resolve(strict=False)
    if os.path.commonpath([str(resolved_root), str(resolved_candidate)]) != str(
        resolved_root
    ):
        raise PocError("PATH_OUTSIDE_RUN", "path resolves outside the run directory")

    current = resolved_root
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise PocError("PATH_OUTSIDE_RUN", "symlink traversal is not allowed")

    if candidate.is_symlink():
        raise PocError("PATH_OUTSIDE_RUN", "symlink targets are not allowed")
    return candidate


def mount_details(target: Path) -> dict[str, Any]:
    target_text = str(target.resolve())
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return {"mounted": False, "target": target_text, "reason": "mountinfo_missing"}

    for line in mountinfo.read_text(encoding="utf-8", errors="replace").splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields = left.split()
        right_fields = right.split()
        if len(fields) < 5 or len(right_fields) < 2:
            continue
        if fields[4] == target_text:
            return {
                "mounted": True,
                "target": target_text,
                "filesystem_type": right_fields[0],
                "source": right_fields[1],
                "mount_options": fields[5].split(","),
                "super_options": right_fields[2].split(",")
                if len(right_fields) > 2
                else [],
            }
    return {"mounted": False, "target": target_text}


class PocDispatcher:
    def __init__(
        self,
        *,
        home: Path | None = None,
        shared_root: Path | None = None,
        runtime_root: Path | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        oauth_factory: Callable[..., SmbOAuthSession] | None = None,
    ) -> None:
        self.home = (home or Path(os.environ.get("HOME", Path.cwd()))).resolve()
        self.shared_root = (shared_root or Path("/team")).resolve()
        self.runtime_root = (
            runtime_root or Path(os.environ.get("POC_RUNTIME_DIR", "/tmp/fa-poc"))
        ).resolve()
        self.command_runner = command_runner
        self.oauth_factory = oauth_factory
        self._oauth: SmbOAuthSession | None = None
        self._oauth_context: tuple[str, str, str] | None = None
        self.state_root = self.home / "fa-poc" / "state"
        self.private_root = self.home / "fa-poc" / "private"
        self.evidence_root = self.home / "fa-poc" / "evidence"

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        with DISPATCH_LOCK:
            if self._auth_mode() == "managed_identity":
                return self._dispatch_oauth(payload)
            return self._dispatch_locked(payload)

    def _dispatch_oauth(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            from smb_oauth import AUTH_LOCK, SmbOAuthError
        except ImportError:
            raise PocError("IMAGE_OR_DEPENDENCY", "managed identity SMB module is unavailable") from None
        with AUTH_LOCK:
            try:
                return self._dispatch_locked(payload)
            except SmbOAuthError as exc:
                raise PocError(exc.reason_code, exc.message, details=exc.details) from None

    @staticmethod
    def _auth_mode() -> str:
        mode = os.environ.get("POC_SMB_AUTH_MODE", "account_key")
        if mode not in {"account_key", "managed_identity"}:
            raise PocError("INVALID_CONFIGURATION", "POC_SMB_AUTH_MODE must be account_key or managed_identity")
        return mode

    def _dispatch_locked(self, payload: dict[str, Any]) -> dict[str, Any]:
        common = self._validate_common(payload)
        operation = common["operation"]

        if operation == "probe_environment":
            result = self._probe_environment()
        elif operation == "status":
            result = self._status(common["poc_id"], common["run_id"])
        else:
            step_id = self._identifier(payload.get("step_id"), "step_id")
            result = self._idempotent(
                common["poc_id"],
                common["run_id"],
                step_id,
                payload,
                lambda: self._dispatch_mutating(operation, common, payload),
            )

        response = {
            "schema_version": SCHEMA_VERSION,
            "poc_id": common["poc_id"],
            "run_id": common["run_id"],
            "operation": operation,
            "status": "success",
            "result": result,
        }
        if operation in {"mount_shared", "resume_recover"} and self._auth_mode() == "managed_identity":
            if result.get("auth_mode") != "managed_identity":
                raise PocError(
                    "SMB_AUTH_MODE_UNVERIFIED",
                    "mount result did not verify managed identity authentication",
                )
            response["auth_mode"] = "managed_identity"
        return response

    def _validate_common(self, payload: dict[str, Any]) -> dict[str, str]:
        if not isinstance(payload, dict):
            raise PocError("INVALID_REQUEST", "request body must be a JSON object")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise PocError(
                "INVALID_REQUEST",
                f"schema_version must be {SCHEMA_VERSION}",
            )
        operation = payload.get("operation")
        if operation not in OPERATIONS:
            raise PocError(
                "INVALID_REQUEST",
                "operation is not allowed",
                details={"allowed_operations": sorted(OPERATIONS)},
            )
        return {
            "poc_id": self._identifier(payload.get("poc_id"), "poc_id"),
            "run_id": self._identifier(payload.get("run_id"), "run_id"),
            "operation": operation,
        }

    @staticmethod
    def _identifier(value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
            raise PocError(
                "INVALID_REQUEST",
                f"{field_name} must match {IDENTIFIER_RE.pattern}",
            )
        return value

    def _dispatch_mutating(
        self,
        operation: str,
        common: dict[str, str],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if operation == "mount_shared":
            return self._mount_shared()
        if operation == "resume_recover":
            before = mount_details(self.shared_root)
            result = self._mount_shared(force_credentials=True)
            result["mount_before_recovery"] = before
            result["platform_resume_evidence"] = "not_observed_by_this_operation"
            return result
        if operation == "shared_io_step":
            return self._shared_io_step(common["poc_id"], common["run_id"], payload)
        if operation == "desktop_smoke":
            publish_path = payload.get("publish_relative_path")
            if publish_path is not None:
                self._require_shared_mount()
                safe_path(self._shared_run_root(common["poc_id"], common["run_id"]), publish_path)
            try:
                result = run_desktop_smoke(
                    poc_id=common["poc_id"],
                    run_id=common["run_id"],
                    nonce=self._identifier(payload.get("nonce"), "nonce"),
                    evidence_root=self.evidence_root,
                    shared_run_root=self._shared_run_root(
                        common["poc_id"], common["run_id"]
                    ),
                    publish_relative_path=payload.get("publish_relative_path"),
                )
                for name in ("before", "after", "browser_download"):
                    artifact = result["artifacts"][name]
                    artifact["size"] = Path(artifact["path"]).stat().st_size
                return result
            except RuntimeError as exc:
                reason, separator, message = str(exc).partition(": ")
                if separator and reason in {"GUI_OR_BROWSER_RUNTIME", "INVALID_REQUEST"}:
                    raise PocError(reason, message) from exc
                raise
        raise PocError("INVALID_REQUEST", "operation is not implemented")

    def _idempotent(
        self,
        poc_id: str,
        run_id: str,
        step_id: str,
        payload: dict[str, Any],
        execute: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        state_dir = self.state_root / poc_id / run_id
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / f"{step_id}.json"
        lock_file = state_dir / f"{step_id}.lock"
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        if state_file.exists():
            return self._load_existing_step(state_file, fingerprint)

        lock_fd: int | None = None
        deadline = time.monotonic() + 5
        while lock_fd is None:
            try:
                lock_fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if state_file.exists():
                    return self._load_existing_step(state_file, fingerprint)
                if time.monotonic() >= deadline:
                    raise PocError(
                        "STEP_IN_PROGRESS",
                        "another invocation is processing this step",
                        status_code=409,
                    )
                time.sleep(0.05)

        try:
            if state_file.exists():
                return self._load_existing_step(state_file, fingerprint)
            result = execute()
            record = {
                "fingerprint": fingerprint,
                "completed_at_utc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
                "result": result,
            }
            temporary = state_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(record, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, state_file)
            return result
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            lock_file.unlink(missing_ok=True)

    @staticmethod
    def _load_existing_step(state_file: Path, fingerprint: str) -> dict[str, Any]:
        record = json.loads(state_file.read_text(encoding="utf-8"))
        if record.get("fingerprint") != fingerprint:
            raise PocError(
                "STEP_CONFLICT",
                "step_id was already used with a different request",
                status_code=409,
            )
        result = dict(record["result"])
        result["idempotent_replay"] = True
        return result

    def _probe_environment(self) -> dict[str, Any]:
        status = Path("/proc/self/status")
        capability_lines: dict[str, str] = {}
        if status.exists():
            for line in status.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                key, separator, value = line.partition(":")
                if separator and key in {"CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"}:
                    capability_lines[key] = value.strip()

        filesystems = []
        filesystem_file = Path("/proc/filesystems")
        if filesystem_file.exists():
            filesystems = filesystem_file.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()

        return {
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "gid": os.getgid() if hasattr(os, "getgid") else None,
            "user": os.environ.get("USER"),
            "home": str(self.home),
            "kernel": platform.release(),
            "platform": platform.platform(),
            "capabilities": capability_lines,
            "filesystems": filesystems,
            "dev_fuse": {
                "exists": Path("/dev/fuse").exists(),
                "readable": os.access("/dev/fuse", os.R_OK),
                "writable": os.access("/dev/fuse", os.W_OK),
            },
            "team_mount": mount_details(self.shared_root),
            "agent": {
                "id": os.environ.get("FOUNDRY_AGENT_ID"),
                "name": os.environ.get("FOUNDRY_AGENT_NAME"),
                "version": os.environ.get("FOUNDRY_AGENT_VERSION"),
                "session_id": os.environ.get("FOUNDRY_AGENT_SESSION_ID"),
            },
        }

    def _mount_shared(self, *, force_credentials: bool = False) -> dict[str, Any]:
        if self._auth_mode() == "managed_identity":
            return self._mount_shared_managed_identity(force_credentials=force_credentials)
        diagnostics = (
            {
                "enabled": True, "credential_source": "not_used", "secret_version": None,
                "secret_version_status": "not_used",
                "credential_file": {"status": "not_run"}, "kernel": {"status": "not_run"},
            }
            if os.environ.get("POC_MOUNT_DIAGNOSTICS") == "1" else None
        )
        try:
            result = self._mount_shared_attempt(diagnostics)
        except PocError as exc:
            if diagnostics is not None:
                exc.details.update(diagnostics=diagnostics)
                exc.details.setdefault("mount_errno", None)
                exc.details.setdefault("denial_origin", "unknown")
            raise
        except OSError as exc:
            if diagnostics is None:
                raise
            raise PocError(
                "MOUNT_EXECUTION_FAILED", "mount preparation or cleanup failed",
                details={
                    "diagnostics": diagnostics, "mount_errno": None, "denial_origin": "unknown",
                    "error_type": type(exc).__name__, "execution_errno": exc.errno,
                },
            ) from exc
        if diagnostics is not None:
            result["diagnostics"] = diagnostics
        return result

    def _oauth_session(self) -> SmbOAuthSession:
        from smb_oauth import SmbOAuthSession

        context = (
            os.environ.get("POC_STORAGE_ACCOUNT_NAME", ""),
            os.environ.get("POC_EXPECTED_MI_OBJECT_ID", ""),
            os.environ.get("POC_EXPECTED_MI_TENANT_ID", ""),
        )
        if self._oauth is None:
            factory = self.oauth_factory if self.oauth_factory is not None else SmbOAuthSession
            self._oauth = factory(*context)
            self._oauth_context = context
        elif self._oauth_context != context:
            raise PocError("INVALID_CONFIGURATION", "managed identity SMB configuration changed during the session")
        return self._oauth

    @staticmethod
    def _require_oauth_mount_options(mount: dict[str, Any]) -> None:
        from smb_oauth import CREDENTIAL_UID

        options = set(mount.get("mount_options", [])) | set(mount.get("super_options", []))
        required = {"sec=krb5", f"cruid={CREDENTIAL_UID}", "vers=3.1.1", "seal"}
        if not required.issubset(options):
            raise PocError(
                "SMB_MOUNT_OPTIONS_UNVERIFIED",
                "CIFS mountinfo must confirm Kerberos, credential UID, SMB 3.1.1 and encryption",
                details={"missing_options": sorted(required - options), "auth_mode": "managed_identity"},
            )

    def _mount_shared_managed_identity(self, *, force_credentials: bool) -> dict[str, Any]:
        from smb_oauth import CACHE_NAME, CREDENTIAL_UID, SmbOAuthError

        account = os.environ.get("POC_STORAGE_ACCOUNT_NAME", "")
        share = os.environ.get("POC_FILE_SHARE_NAME", "")
        if not re.fullmatch(r"[a-z0-9]{3,24}", account):
            raise PocError("INVALID_CONFIGURATION", "invalid storage account name")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", share):
            raise PocError("INVALID_CONFIGURATION", "invalid Azure Files share name")
        existing = mount_details(self.shared_root)
        if existing["mounted"]:
            existing = self._require_shared_mount(refresh=False)
        oauth = self._oauth_session()
        try:
            auth = oauth.ensure(force=force_credentials or not existing["mounted"])
        except SmbOAuthError as exc:
            exc.details.update(mount_attempted=False, mount_errno=None, denial_origin="unknown")
            if os.environ.get("POC_MI_NETWORK_DIAGNOSTICS") == "1":
                from smb_diagnostics import network_probe

                exc.details["diagnostics"] = {"network": network_probe()}
            raise
        if existing["mounted"]:
            return {**existing, "mount_mode": "already_present", "auth_mode": "managed_identity", "auth": auth}

        host = f"{account}.file.core.windows.net"
        diagnostics = None
        try:
            try:
                dns_addresses = sorted({
                    result[4][0] for result in socket.getaddrinfo(host, 445, type=socket.SOCK_STREAM)
                })
                with socket.create_connection((host, 445), timeout=5):
                    pass
            except OSError:
                raise PocError("NETWORK_OR_DNS", "Azure Files DNS or TCP 445 connectivity failed") from None
            self.shared_root.mkdir(parents=True, exist_ok=True)
            options = [
                "sec=krb5", f"cruid={CREDENTIAL_UID}", "vers=3.1.1", "seal",
                "serverino", "nosharesock", "actimeo=1", "mfsymlinks",
                f"uid={os.getuid()}", f"gid={os.getgid()}", "file_mode=0660", "dir_mode=0770",
            ]
            if os.environ.get("POC_MI_MOUNT_DIAGNOSTICS") == "1":
                from smb_diagnostics import begin, finish, mount_message

                diagnostics = {"preflight": begin(self.shared_root)}
                before = self._kernel_snapshot("", categories_only=True)
            try:
                completed = self.command_runner(
                    ["mount", "-t", "cifs", f"//{host}/{share}", str(self.shared_root), "-o", ",".join(options)],
                    capture_output=True, text=True, timeout=30, check=False,
                    env={
                        "PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
                        "LC_ALL": "C", "KRB5CCNAME": CACHE_NAME,
                    },
                )
            except (OSError, subprocess.TimeoutExpired):
                raise PocError("MOUNT_EXECUTION_FAILED", "managed identity mount command failed or timed out") from None
            finally:
                if diagnostics is not None:
                    diagnostics["kernel"] = self._kernel_delta(
                        before, self._kernel_snapshot("", categories_only=True),
                    )
                    diagnostics["upcall"] = finish()
            if diagnostics is not None:
                diagnostics["mount_error"] = mount_message(completed.stderr)
            if completed.returncode != 0:
                reason, mount_errno = self._mount_failure_reason(completed.stderr)
                raise PocError(
                    reason, "Azure Files mount failed",
                    details={
                        "return_code": completed.returncode, "mount_errno": mount_errno,
                        "denial_origin": "unknown", "host": host, "dns_addresses": dns_addresses,
                        "tcp_445": True, "mount_output": "suppressed_for_managed_identity",
                    },
                )
            details = self._require_shared_mount(refresh=False)
            return {
                **details, "mount_mode": "application_mount", "auth_mode": "managed_identity",
                "auth": auth, "host": host, "dns_addresses": dns_addresses, "tcp_445": True,
                **({"diagnostics": diagnostics} if diagnostics is not None else {}),
            }
        except Exception as exc:
            # Neither command output nor arbitrary exception messages enter MI evidence.
            error = exc if isinstance(exc, PocError) else PocError(
                "MOUNT_EXECUTION_FAILED", "managed identity mount preparation or verification failed",
            )
            error.details.update(auth_mode="managed_identity", auth=auth)
            if diagnostics is not None:
                error.details["diagnostics"] = diagnostics
            error.details.setdefault("mount_errno", None)
            error.details.setdefault("denial_origin", "unknown")
            try:
                oauth.invalidate()
                error.details["credential_cleanup"] = {"status": "success"}
            except SmbOAuthError as cleanup:
                error.details["credential_cleanup"] = {
                    "status": "failed", "reason_code": cleanup.reason_code, **cleanup.details,
                }
            raise error from None

    @staticmethod
    def _mount_failure_reason(stderr: str) -> tuple[str, int | None]:
        match = re.search(r"mount error\((\d+)\)", stderr, re.IGNORECASE)
        mount_errno = int(match.group(1)) if match else None
        if mount_errno == 13:
            reason = "MOUNT_ACCESS_DENIED_UNCLASSIFIED"
        elif mount_errno == 1:
            reason = "MOUNT_PRIVILEGE_OR_KERNEL"
        elif mount_errno == 2:
            reason = "MOUNT_ENOENT_UNCLASSIFIED"
        elif "permission denied" in stderr.lower():
            reason = "MOUNT_ACCESS_DENIED_UNCLASSIFIED"
        elif "operation not permitted" in stderr.lower():
            reason = "MOUNT_PRIVILEGE_OR_KERNEL"
        else:
            reason = "AUTHENTICATION_OR_SHARE_ACL"
        return reason, mount_errno

    @staticmethod
    def _credential_file_diagnostics(path: Path, account: str, key: str) -> dict:
        result: dict[str, Any] = {
            "status": "unavailable", "readable_by_process": False,
            "content_matches_expected": False, "errno": None,
        }
        try:
            metadata = path.stat(follow_symlinks=False)
            result.update(
                owner_uid=metadata.st_uid, owner_gid=metadata.st_gid,
                process_euid=os.geteuid(), owner_matches_process=metadata.st_uid == os.geteuid(),
                mode=f"{stat.S_IMODE(metadata.st_mode):04o}",
                regular_file=stat.S_ISREG(metadata.st_mode),
            )
            if not result["regular_file"]:
                result["error_type"] = "NotRegularFile"
                return result
            with path.open("rb") as handle:
                content = handle.read(512)
            result.update(
                status="available", readable_by_process=True,
                content_matches_expected=content == f"username={account}\npassword={key}\n".encode("utf-8"),
            )
        except OSError as exc:
            result.update(error_type=type(exc).__name__, errno=exc.errno)
        return result

    def _kernel_snapshot(self, key: str, *, categories_only: bool = False) -> dict:
        record: dict[str, Any] = {"status": "unavailable", "errno": None}
        try:
            # Bookworm defaults to boot-relative timestamps; --time-format=raw is unsupported.
            completed = self.command_runner(
                [
                    "dmesg", "--kernel", "--syslog", f"--buffer-size={KERNEL_READ_BYTES}",
                    "--color=never", "--nopager",
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3, check=False,
                env={"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            record.update(error_type=type(exc).__name__, errno=getattr(exc, "errno", None))
            return record
        record["return_code"] = completed.returncode
        if completed.returncode != 0:
            record["stderr"] = diagnostic_text(completed.stderr, key, 512)
            for text, number in (("Operation not permitted", errno.EPERM), ("Permission denied", errno.EACCES)):
                if f"read kernel buffer failed: {text}" in record["stderr"]:
                    record.update(errno=number, errno_source="dmesg_stderr")
            if categories_only:
                record["stderr"] = "suppressed_for_managed_identity"
            return record
        output = diagnostic_text(completed.stdout, key, KERNEL_READ_BYTES, tail=True)
        record.update(
            status="available", cursor_us=None, records=[],
            read_limit_reached=len((completed.stdout or "").encode("utf-8")) >= KERNEL_READ_BYTES,
        )
        if categories_only:
            record["unclassified_related_messages"] = 0
        for line in output.splitlines():
            match = re.match(r"^(?:<\d+>)?\s*\[\s*(\d+)\.(\d+)\]\s*(.*)$", line)
            if not match:
                continue
            timestamp = int(match[1]) * 1000000 + int(match[2][:6].ljust(6, "0"))
            record["cursor_us"] = max(record["cursor_us"] or 0, timestamp)
            message = match[3]
            related = re.search(r"\b(?:cifs|smb\d*)(?:\b|_)", message, re.IGNORECASE) or (
                re.search(r"\b(?:apparmor|selinux|avc|seccomp|lsm|audit)\b", message, re.IGNORECASE)
                and re.search(r"\bmount\b", message, re.IGNORECASE)
            )
            # Do not retain credential/mount-data dumps, even inside a relevant kernel message.
            sensitive_dump = re.search(
                r"\b(?:password|passwd|pass|accountkey|secret|authorization|credentials|mountdata|mount_data)"
                r"\s*[:=]|\bmount data\b",
                message, re.IGNORECASE,
            )
            if related and not sensitive_dump:
                if categories_only:
                    from smb_diagnostics import kernel_message

                    category = kernel_message(message)
                    if category is None:
                        record["unclassified_related_messages"] += 1
                    else:
                        record["records"].append((timestamp, category))
                else:
                    record["records"].append((timestamp, diagnostic_text(line, key, 512)))
        return record

    @staticmethod
    def _kernel_delta(before: dict, after: dict) -> dict:
        result = {
            "status": "unavailable", "messages": [],
            "before": {key: value for key, value in before.items() if key != "records"},
            "after": {key: value for key, value in after.items() if key != "records"},
            "read_limit_bytes": KERNEL_READ_BYTES, "max_messages": KERNEL_MAX_MESSAGES,
            "attribution": "invocation_time_window_only",
        }
        if before["status"] != "available" or after["status"] != "available":
            result["reason"] = "kernel_read_unavailable"
        elif before["cursor_us"] is None or after["cursor_us"] is None:
            result["reason"] = "kernel_timestamp_unavailable"
        elif after["cursor_us"] < before["cursor_us"]:
            result["reason"] = "kernel_cursor_regressed"
        else:
            messages = [line for timestamp, line in after["records"] if timestamp > before["cursor_us"]]
            truncated = after["read_limit_reached"] or len(messages) > KERNEL_MAX_MESSAGES
            result.update(
                status="partial" if truncated else "available", truncated=truncated,
                messages=messages[-KERNEL_MAX_MESSAGES:],
            )
        return result

    def _mount_shared_attempt(self, diagnostics: dict | None) -> dict[str, Any]:
        existing = mount_details(self.shared_root)
        if existing["mounted"]:
            existing = self._require_shared_mount()
            existing["mount_mode"] = "already_present"
            return existing

        account = os.environ.get("POC_STORAGE_ACCOUNT_NAME", "")
        share = os.environ.get("POC_FILE_SHARE_NAME", "")
        key = os.environ.get("POC_STORAGE_ACCOUNT_KEY", "")
        vault_url = os.environ.get("POC_KEY_VAULT_URL", "")
        secret_name = os.environ.get(
            "POC_STORAGE_KEY_SECRET_NAME", "azure-files-account-key"
        )
        if not account or not share or (not key and not vault_url):
            raise PocError(
                "ENVIRONMENT_OR_APPROVAL",
                "approved Azure Files configuration is not available",
                status_code=424,
                details={
                    "required_environment": [
                        "POC_STORAGE_ACCOUNT_NAME",
                        "POC_FILE_SHARE_NAME",
                        "POC_KEY_VAULT_URL",
                        "POC_STORAGE_KEY_SECRET_NAME",
                    ]
                },
            )
        if not re.fullmatch(r"[a-z0-9]{3,24}", account):
            raise PocError("INVALID_CONFIGURATION", "invalid storage account name")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", share):
            raise PocError("INVALID_CONFIGURATION", "invalid Azure Files share name")
        if diagnostics is not None:
            diagnostics["credential_source"] = "environment" if key else "key_vault"
        if not key:
            key = (
                self._read_storage_key(vault_url, secret_name, diagnostics)
                if diagnostics is not None else self._read_storage_key(vault_url, secret_name)
            )
        if diagnostics is not None:
            diagnostics["credential_format"] = credential_format(key)
            if not diagnostics["credential_format"]["valid_64_byte_base64"]:
                raise PocError("INVALID_CONFIGURATION", "storage key must be unmodified 64-byte base64")

        host = f"{account}.file.core.windows.net"
        dns_addresses = sorted(
            {
                result[4][0]
                for result in socket.getaddrinfo(host, 445, type=socket.SOCK_STREAM)
            }
        )
        try:
            with socket.create_connection((host, 445), timeout=5):
                tcp_445 = True
        except OSError as exc:
            raise PocError(
                "NETWORK_OR_DNS",
                "Azure Files TCP 445 connectivity failed",
                details={"host": host, "dns_addresses": dns_addresses, "error": str(exc)},
            ) from exc

        self.shared_root.mkdir(parents=True, exist_ok=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        credential_path: Path | None = None
        try:
            fd, credential_name = tempfile.mkstemp(
                prefix="smb-", suffix=".cred", dir=self.runtime_root
            )
            credential_path = Path(credential_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(f"username={account}\npassword={key}\n")
            if diagnostics is not None:
                file = self._credential_file_diagnostics(credential_path, account, key)
                diagnostics["credential_file"] = file
                if (
                    not file["readable_by_process"] or not file["content_matches_expected"]
                    or not file.get("owner_matches_process") or file.get("mode") != "0600"
                    or not file.get("regular_file")
                ):
                    raise PocError("INVALID_CONFIGURATION", "credentials file did not meet the read/owner/mode/content checks")

            options = [
                f"credentials={credential_path}",
                "vers=3.1.1",
                "seal",
                "serverino",
                "nosharesock",
                "actimeo=1",
                "mfsymlinks",
                f"uid={os.getuid()}",
                f"gid={os.getgid()}",
                "file_mode=0660",
                "dir_mode=0770",
            ]
            before = self._kernel_snapshot(key) if diagnostics is not None else None
            try:
                completed = self.command_runner(
                    [
                        "mount", "-t", "cifs", f"//{host}/{share}",
                        str(self.shared_root), "-o", ",".join(options),
                    ],
                    capture_output=True, text=True, timeout=30, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                if diagnostics is None:
                    raise
                raise PocError(
                    "MOUNT_EXECUTION_FAILED", "mount command timed out",
                    details={"error_type": type(exc).__name__, "timeout_seconds": 30},
                ) from exc
            finally:
                if diagnostics is not None and before is not None:
                    diagnostics["kernel"] = self._kernel_delta(before, self._kernel_snapshot(key))
            stderr = completed.stderr.replace(str(credential_path), "<credentials-file>")
            stdout = completed.stdout.replace(str(credential_path), "<credentials-file>")
            reason, mount_errno = self._mount_failure_reason(stderr)
            if diagnostics is not None:
                diagnostics["mount_output_truncated"] = {
                    "stderr": len(stderr.encode("utf-8")) > 4096,
                    "stdout": len(stdout.encode("utf-8")) > 4096,
                }
                stderr = diagnostic_text(stderr, key)
                stdout = diagnostic_text(stdout, key)
            if completed.returncode != 0:
                raise PocError(
                    reason,
                    "Azure Files mount failed",
                    details={
                        "return_code": completed.returncode,
                        "mount_errno": mount_errno,
                        "denial_origin": "unknown",
                        "stdout": stdout,
                        "stderr": stderr,
                        "host": host,
                        "dns_addresses": dns_addresses,
                        "tcp_445": tcp_445,
                    },
                )
        finally:
            if credential_path is not None:
                if diagnostics is not None:
                    diagnostics["credential_file"]["removed"] = False
                credential_path.unlink(missing_ok=True)
                if diagnostics is not None:
                    diagnostics["credential_file"]["removed"] = True

        details = mount_details(self.shared_root)
        if not details["mounted"] or details.get("filesystem_type") != "cifs":
            raise PocError(
                "MOUNT_PRIVILEGE_OR_KERNEL",
                "mount command returned success but /team is not a CIFS mount",
                details=details,
            )
        details.update(
            {
                "mount_mode": "application_mount",
                "host": host,
                "dns_addresses": dns_addresses,
                "tcp_445": tcp_445,
            }
        )
        return details

    @staticmethod
    def _read_storage_key(vault_url: str, secret_name: str, diagnostics: dict | None = None) -> str:
        if not vault_url.startswith("https://") or not vault_url.endswith(
            ".vault.azure.net"
        ):
            raise PocError("INVALID_CONFIGURATION", "invalid Key Vault URL")
        if not IDENTIFIER_RE.fullmatch(secret_name):
            raise PocError("INVALID_CONFIGURATION", "invalid Key Vault secret name")
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            credential = DefaultAzureCredential()
            secret = SecretClient(vault_url=vault_url, credential=credential).get_secret(
                secret_name
            )
            if diagnostics is not None:
                version = secret.properties.version
                diagnostics["secret_version"] = (
                    version if isinstance(version, str) and IDENTIFIER_RE.fullmatch(version) else None
                )
                diagnostics["secret_version_status"] = (
                    "available" if diagnostics["secret_version"] is not None else "unavailable"
                )
                if not secret.value:
                    diagnostics["credential_format"] = credential_format("")
                    raise PocError("INVALID_CONFIGURATION", "Key Vault secret is empty")
            if not secret.value:
                raise ValueError("secret value is empty")
            return secret.value
        except PocError:
            raise
        except Exception as exc:
            raise PocError(
                "AUTHENTICATION_OR_SHARE_ACL",
                "could not retrieve the Azure Files key from Key Vault",
                details={
                    "error_type": type(exc).__name__,
                    "error": str(exc) if diagnostics is None else "Key Vault retrieval failed; raw exception suppressed",
                },
            ) from (None if diagnostics is not None else exc)

    def _shared_run_root(self, poc_id: str, run_id: str) -> Path:
        return safe_path(self.shared_root, f"poc/{poc_id}/{run_id}")

    def _require_shared_mount(self, *, refresh: bool = True) -> dict[str, Any]:
        mount = mount_details(self.shared_root)
        expected = (
            f"//{os.environ.get('POC_STORAGE_ACCOUNT_NAME', '')}.file.core.windows.net"
            f"/{os.environ.get('POC_FILE_SHARE_NAME', '')}"
        )
        if (
            not mount["mounted"] or mount.get("filesystem_type") != "cifs"
            or mount.get("source") != expected
        ):
            raise PocError(
                "MOUNT_PRIVILEGE_OR_KERNEL",
                "shared I/O requires the approved CIFS source at /team",
                status_code=424,
                details=mount,
            )
        if self._auth_mode() == "managed_identity":
            self._require_oauth_mount_options(mount)
            if refresh:
                mount["auth"] = self._oauth_session().ensure()
        return mount

    def _shared_io_step(
        self, poc_id: str, run_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        mount = self._require_shared_mount()

        action = payload.get("action")
        allowed = {
            "write_text",
            "write_bytes",
            "read_text",
            "read_bytes",
            "hash",
            "inspect",
            "rename",
            "write_private_marker",
            "check_private_marker_absent",
        }
        if action not in allowed:
            raise PocError(
                "INVALID_REQUEST",
                "shared_io_step action is not allowed",
                details={"allowed_actions": sorted(allowed)},
            )

        if action in {"write_private_marker", "check_private_marker_absent"}:
            return self._private_marker(poc_id, run_id, action, payload)

        root = self._shared_run_root(poc_id, run_id)
        if action != "inspect":
            root.mkdir(parents=True, exist_ok=True)
        path = safe_path(root, payload.get("relative_path"))

        if action == "inspect":
            try:
                metadata = path.stat()
            except FileNotFoundError:
                return {
                    "action": action, "relative_path": payload["relative_path"],
                    "exists": False, "is_file": False, "team_mount": mount,
                }
            if not path.is_file():
                raise PocError("SHARED_IO_CONSISTENCY", "artifact is not a regular file")
            return {
                "action": action, "relative_path": payload["relative_path"],
                "exists": True, "is_file": True, "size": metadata.st_size,
                "sha256": sha256_file(path), "team_mount": mount,
            }
        if action == "write_text":
            content = payload.get("content")
            if not isinstance(content, str):
                raise PocError("INVALID_REQUEST", "content must be a string")
            encoded = content.encode("utf-8")
            self._validate_size(encoded)
            self._atomic_write(path, encoded)
        elif action == "write_bytes":
            encoded_content = payload.get("content_base64")
            if not isinstance(encoded_content, str):
                raise PocError("INVALID_REQUEST", "content_base64 must be a string")
            try:
                decoded = base64.b64decode(encoded_content, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise PocError("INVALID_REQUEST", "content_base64 is invalid") from exc
            self._validate_size(decoded)
            self._atomic_write(path, decoded)
        elif action == "rename":
            destination = safe_path(root, payload.get("destination_relative_path"))
            if not path.exists():
                raise PocError("SHARED_IO_CONSISTENCY", "rename source does not exist")
            if destination.exists():
                raise PocError(
                    "SHARED_IO_CONSISTENCY",
                    "rename destination already exists",
                    status_code=409,
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.rename(path, destination)
            return {
                "action": action,
                "source_relative_path": payload["relative_path"],
                "destination_relative_path": payload["destination_relative_path"],
                "destination_sha256": sha256_file(destination),
                "destination_size": destination.stat().st_size,
                "source_exists": path.exists(),
            }
        else:
            if not path.is_file():
                raise PocError("SHARED_IO_CONSISTENCY", "artifact does not exist")

        response: dict[str, Any] = {
            "action": action,
            "relative_path": payload.get("relative_path"),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        if action == "read_text":
            response["content"] = path.read_text(encoding="utf-8")
        elif action == "read_bytes":
            response["content_base64"] = base64.b64encode(path.read_bytes()).decode(
                "ascii"
            )
        return response

    @staticmethod
    def _validate_size(content: bytes) -> None:
        if len(content) > MAX_ARTIFACT_BYTES:
            raise PocError(
                "INVALID_REQUEST",
                f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes",
            )

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _private_marker(
        self,
        poc_id: str,
        run_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        marker = self.private_root / poc_id / run_id / "session-marker.txt"
        if action == "write_private_marker":
            content = payload.get("content")
            if not isinstance(content, str) or not content:
                raise PocError("INVALID_REQUEST", "content must be a non-empty string")
            marker.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(marker, content.encode("utf-8"))
            return {
                "action": action,
                "path": str(marker),
                "sha256": sha256_file(marker),
            }
        return {
            "action": action,
            "path": str(marker),
            "absent": not marker.exists(),
        }

    def _status(self, poc_id: str, run_id: str) -> dict[str, Any]:
        state_dir = self.state_root / poc_id / run_id
        completed_steps = (
            sorted(path.stem for path in state_dir.glob("*.json"))
            if state_dir.exists()
            else []
        )
        return {
            "completed_steps": completed_steps,
            "team_mount": mount_details(self.shared_root),
            "private_marker_exists": (
                self.private_root / poc_id / run_id / "session-marker.txt"
            ).exists(),
        }
