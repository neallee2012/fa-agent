from __future__ import annotations

import base64
import ctypes
import errno
import json
import logging
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from itertools import islice
from pathlib import Path
from typing import Any, Callable

STORAGE_AUDIENCE = "https://storage.azure.com"
STORAGE_SCOPE = STORAGE_AUDIENCE + "/.default"
CREDENTIAL_UID = 10002
CACHE_PATH = Path("/tmp/krb5cc_10002")
CACHE_NAME = "FILE:/tmp/krb5cc_10002"
CONFIG_PATH = Path("/etc/azfilesauth/config.yaml")
LIBRARY_PATH = Path("/usr/lib/libazfilesauth.so")
LIBRARY_VERSION = "1.0.12"
REFRESH_MARGIN_SECONDS = 300
MIN_LIFETIME_SECONDS = 60

# The native helper changes effective UID and uses process-global libcurl state.
AUTH_LOCK = threading.RLock()


class SmbOAuthError(Exception):
    def __init__(self, reason_code: str, **details: Any) -> None:
        self.reason_code = reason_code
        self.message = "Managed identity SMB authentication failed; see safe stage evidence"
        self.details = {"auth_mode": "managed_identity", **details}
        super().__init__(self.message)


def parse_config(text: str) -> dict[str, Any]:
    expected = {
        "USER_UID": CREDENTIAL_UID, "KRB5_CC_NAME": CACHE_NAME,
        "LOG_DESTINATION": "file", "LOG_FILE_PATH": "/dev/null",
        "CURL_CONNECT_TIMEOUT": 10, "CURL_TOTAL_TIMEOUT": 30,
    }
    try:
        import yaml

        if len(text.encode("utf-8")) > 8192:
            raise ValueError()
        if any(isinstance(t, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken))
               for t in yaml.scan(text)):
            raise ValueError()
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        if not isinstance(node, yaml.MappingNode):
            raise ValueError()
        config: dict[str, Any] = {}
        for key, value in node.value:
            if (
                not isinstance(key, yaml.ScalarNode) or not isinstance(value, yaml.ScalarNode)
                or key.value not in expected or key.value in config
            ):
                raise ValueError()
            if type(expected[key.value]) is int:
                if value.tag != "tag:yaml.org,2002:int" or not re.fullmatch(r"[1-9][0-9]*", value.value):
                    raise ValueError()
                config[key.value] = int(value.value)
            else:
                if value.tag != "tag:yaml.org,2002:str":
                    raise ValueError()
                config[key.value] = value.value
        if config != expected:
            raise ValueError()
        return config
    except ImportError:
        raise SmbOAuthError("SMB_AUTH_DEPENDENCY_MISSING", stage="yaml") from None
    except Exception:
        # YAML errors can quote the original document. Never include them.
        raise SmbOAuthError("SMB_AUTH_CONFIG_INVALID", stage="config_schema") from None


def _trusted_parents(path: Path, *, cache: bool = False) -> None:
    for parent in path.parents:
        info = parent.lstat()
        writable = stat.S_IMODE(info.st_mode) & 0o022
        private_tmp = cache and parent == Path("/tmp") and stat.S_IMODE(info.st_mode) == 0o1777
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or (writable and not private_tmp):
            raise SmbOAuthError("SMB_AUTH_CONFIG_INVALID", stage="parent_permissions")


def _root_file(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
        and not stat.S_IMODE(info.st_mode) & 0o022
    )


def validate_config() -> None:
    try:
        import pwd

        if os.geteuid() != 0:
            raise SmbOAuthError("SMB_AUTH_ROOT_REQUIRED", stage="process_identity")
        if pwd.getpwuid(CREDENTIAL_UID).pw_name != "azfilesuser":
            raise SmbOAuthError("SMB_AUTH_CONFIG_INVALID", stage="credential_identity")
        _trusted_parents(CONFIG_PATH)
        fd = os.open(CONFIG_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            # Native seteuid(10002) rereads this NONSECRET config without changing groups.
            if not _root_file(info) or stat.S_IMODE(info.st_mode) not in {0o444, 0o644}:
                raise SmbOAuthError("SMB_AUTH_CONFIG_INVALID", stage="config_permissions")
            parse_config(handle.read(8193))
        sink = Path("/dev/null").lstat()
        if not stat.S_ISCHR(sink.st_mode) or sink.st_rdev != os.makedev(1, 3) or sink.st_uid != 0:
            raise SmbOAuthError("SMB_AUTH_CONFIG_INVALID", stage="native_log_sink")
    except SmbOAuthError:
        raise
    except Exception:
        raise SmbOAuthError("SMB_AUTH_CONFIG_INVALID", stage="config_read") from None


def validate_library() -> None:
    try:
        _trusted_parents(LIBRARY_PATH)
        resolved = LIBRARY_PATH.resolve(strict=True)
        if (
            LIBRARY_PATH.lstat().st_uid != 0
            or resolved != Path("/usr/lib/libazfilesauth.so.0.0.0")
            or not _root_file(resolved.lstat())
        ):
            raise SmbOAuthError("SMB_NATIVE_LIBRARY_UNSAFE", stage="library_permissions")
    except SmbOAuthError:
        raise
    except Exception:
        raise SmbOAuthError("SMB_NATIVE_LIBRARY_UNAVAILABLE", stage="library_path") from None


def cache_state() -> dict[str, Any]:
    try:
        _trusted_parents(CACHE_PATH, cache=True)
        try:
            info = CACHE_PATH.lstat()
        except FileNotFoundError:
            return {"present": False}
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != CREDENTIAL_UID
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
        ):
            raise SmbOAuthError("SMB_CACHE_UNSAFE", stage="cache_permissions")
        return {
            "present": True, "owner_uid": info.st_uid, "mode": "0600",
            "nonempty": info.st_size > 0, "device": info.st_dev, "inode": info.st_ino,
            "mtime_ns": info.st_mtime_ns, "size": info.st_size,
        }
    except SmbOAuthError:
        raise
    except Exception:
        raise SmbOAuthError("SMB_CACHE_UNAVAILABLE", stage="cache_metadata") from None


def remove_cache_file() -> None:
    try:
        if os.geteuid() != 0:
            raise SmbOAuthError("SMB_NATIVE_IDENTITY_NOT_RESTORED", stage="cache_cleanup")
        if cache_state()["present"]:
            CACHE_PATH.unlink()
    except SmbOAuthError:
        raise
    except Exception:
        raise SmbOAuthError("SMB_CACHE_CLEANUP_FAILED", stage="cache_unlink") from None


def native_log_fds() -> set[int]:
    """Inspect only this process's null-sink FD metadata, never descriptor contents."""
    try:
        import fcntl

        paths = list(islice(Path("/proc/self/fd").iterdir(), 513))
        if len(paths) > 512:
            raise ValueError()
        found = set()
        for path in paths:
            try:
                if os.readlink(path) != "/dev/null":
                    continue
                fd = int(path.name)
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                if (
                    flags & os.O_ACCMODE == os.O_WRONLY and flags & os.O_APPEND
                    and fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
                ):
                    found.add(fd)
            except OSError as exc:
                if exc.errno not in {errno.ENOENT, errno.EBADF}:
                    raise
        return found
    except Exception:
        raise SmbOAuthError("SMB_NATIVE_LOG_SINK_UNVERIFIED", stage="native_log_sink") from None


@contextmanager
def sdk_log_guard():
    # SDK decorators log exception text independently of HTTP logging_enable.
    # Scrub before handlers, only on the calling thread; other request logs survive.
    previous = logging.getLogRecordFactory()
    owner = threading.get_ident()

    def safe_record(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if threading.get_ident() == owner:
            record.msg, record.args = "Managed identity SDK diagnostic suppressed", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return record

    logging.setLogRecordFactory(safe_record)
    try:
        yield
    finally:
        if logging.getLogRecordFactory() is not safe_record:
            raise SmbOAuthError("SMB_SDK_LOG_GUARD_CHANGED", stage="acquire_token")
        logging.setLogRecordFactory(previous)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def token_claims(
    token: str, expires_on: int, expected_oid: str, expected_tid: str, now: float,
) -> tuple[dict[str, str], int]:
    try:
        if not isinstance(token, str) or len(token) > 32768:
            raise ValueError()
        segments = token.split(".")
        if len(segments) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_-]+", s) for s in segments):
            raise ValueError()
        decode = lambda s: json.loads(
            base64.b64decode(s + "=" * (-len(s) % 4), altchars=b"-_", validate=True),
            object_pairs_hook=_unique_object,
        )
        header, claims = decode(segments[0]), decode(segments[1])
        if not isinstance(header, dict) or header.get("alg") != "RS256" or not isinstance(claims, dict):
            raise ValueError()
        if claims.get("aud") != STORAGE_AUDIENCE:
            raise SmbOAuthError("SMB_TOKEN_AUDIENCE_MISMATCH", stage="token_claims")
        exp, nbf = claims.get("exp"), claims.get("nbf", 0)
        if (
            type(exp) is not int or type(nbf) is not int or type(expires_on) is not int
            or min(exp, expires_on) <= now + MIN_LIFETIME_SECONDS or nbf > now
        ):
            raise SmbOAuthError("SMB_TOKEN_LIFETIME_INVALID", stage="token_claims")
        minimal = {"aud": STORAGE_AUDIENCE, "oid": str(uuid.UUID(claims["oid"])),
                   "tid": str(uuid.UUID(claims["tid"]))}
        if minimal["oid"] != expected_oid or minimal["tid"] != expected_tid:
            raise SmbOAuthError("SMB_MI_IDENTITY_MISMATCH", stage="token_claims", claims=minimal)
        # This is a payload check of an SDK token, NOT local signature verification.
        return minimal, min(exp, expires_on)
    except SmbOAuthError:
        raise
    except Exception:
        raise SmbOAuthError("SMB_TOKEN_INVALID", stage="token_claims") from None


class NativeLibrary:
    def __init__(self, *, loader: Callable[..., Any] = ctypes.CDLL) -> None:
        with AUTH_LOCK:
            validate_config()  # The library constructor initializes its native logger.
            validate_library()
            before = native_log_fds()
            try:
                self.library = loader(str(LIBRARY_PATH), use_errno=True)
                self.set_function = self.library.extern_smb_set_credential_oauth_token
                self.set_function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint)]
                self.set_function.restype = ctypes.c_int
                self.clear_function = self.library.extern_smb_clear_credential
                self.clear_function.argtypes = [ctypes.c_char_p]
                self.clear_function.restype = ctypes.c_int
                self.version_function = self.library.extern_smb_version
                self.version_function.argtypes = []
                self.version_function.restype = ctypes.c_char_p
            except Exception:
                raise SmbOAuthError("SMB_NATIVE_LIBRARY_UNAVAILABLE", stage="library_load") from None
            added = native_log_fds() - before
            if len(added) != 1:
                raise SmbOAuthError("SMB_NATIVE_LOG_SINK_UNVERIFIED", stage="library_load")
            self.log_fd = added.pop()
            if self._call("version", self.version_function) != LIBRARY_VERSION.encode("ascii"):
                raise SmbOAuthError("SMB_NATIVE_VERSION_MISMATCH", stage="library_version")
            self.version = LIBRARY_VERSION

    @staticmethod
    def _call(stage: str, function: Callable[..., Any], *args: Any) -> Any:
        if os.geteuid() != 0:
            raise SmbOAuthError("SMB_AUTH_ROOT_REQUIRED", stage=stage)
        try:
            return function(*args)
        except Exception:
            raise SmbOAuthError("SMB_NATIVE_CALL_FAILED", stage=stage) from None
        finally:
            if os.geteuid() != 0:
                raise SmbOAuthError("SMB_NATIVE_IDENTITY_NOT_RESTORED", stage=stage) from None

    def set(self, endpoint: str, token: str) -> int:
        with AUTH_LOCK:
            if self.log_fd not in native_log_fds():
                raise SmbOAuthError("SMB_NATIVE_LOG_SINK_UNVERIFIED", stage="native_set")
            expires = ctypes.c_uint(0)
            buffer = ctypes.create_string_buffer(token.encode("ascii"))
            try:
                code = self._call(
                    "native_set", self.set_function, endpoint.encode("ascii"),
                    ctypes.cast(buffer, ctypes.c_char_p), ctypes.byref(expires),
                )
            finally:
                ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
            if code != 0:
                raise SmbOAuthError("SMB_NATIVE_SET_FAILED", stage="native_set", return_code=code)
            return expires.value

    def clear(self, endpoint: str) -> None:
        with AUTH_LOCK:
            if self.log_fd not in native_log_fds():
                raise SmbOAuthError("SMB_NATIVE_LOG_SINK_UNVERIFIED", stage="native_clear")
            code = self._call("native_clear", self.clear_function, endpoint.encode("ascii"))
            if code != 0:
                raise SmbOAuthError("SMB_NATIVE_CLEAR_FAILED", stage="native_clear", return_code=code)


def managed_identity_credential() -> Any:
    try:
        from azure.identity import ManagedIdentityCredential

        return ManagedIdentityCredential(
            connection_timeout=5, read_timeout=10, retry_total=0, logging_enable=False,
        )
    except Exception:
        raise SmbOAuthError("SMB_MI_CREDENTIAL_UNAVAILABLE", stage="credential_creation") from None


class SmbOAuthSession:
    def __init__(
        self, account: str, expected_oid: str, expected_tid: str, *,
        credential_factory: Callable[[], Any] = managed_identity_credential,
        native_factory: Callable[[], Any] = NativeLibrary,
        clock: Callable[[], float] = time.time,
    ) -> None:
        try:
            if not re.fullmatch(r"[a-z0-9]{3,24}", account):
                raise ValueError()
            self.expected_oid = str(uuid.UUID(expected_oid))
            self.expected_tid = str(uuid.UUID(expected_tid))
        except Exception:
            raise SmbOAuthError("SMB_AUTH_CONFIG_INVALID", stage="expected_identity") from None
        self.endpoint = f"https://{account}.file.core.windows.net"
        self.credential_factory, self.native_factory, self.clock = credential_factory, native_factory, clock
        self.credential: Any = None
        self.native: Any = None
        self.expires_at = 0.0
        self.claims: dict[str, str] = {}
        self.cache: dict[str, Any] = {}

    def _native(self) -> Any:
        if self.native is None:
            self.native = self.native_factory()
        return self.native

    def invalidate(self) -> None:
        with AUTH_LOCK:
            self.expires_at, self.claims, self.cache = 0.0, {}, {}
            validate_config()
            if not cache_state()["present"]:
                return
            native_error = None
            native_return_code = None
            try:
                self._native().clear(self.endpoint)
            except SmbOAuthError as exc:
                native_error = exc.reason_code
                native_return_code = exc.details.get("return_code")
            except Exception:
                native_error = "SMB_NATIVE_CALL_FAILED"
            try:
                remove_cache_file()
            except SmbOAuthError as exc:
                raise SmbOAuthError(
                    "SMB_CACHE_CLEANUP_FAILED", stage="invalidate",
                    native_reason_code=native_error, native_return_code=native_return_code,
                    file_reason_code=exc.reason_code, cache_removed=False,
                ) from None
            if native_error:
                raise SmbOAuthError(
                    "SMB_CACHE_CLEANUP_FAILED", stage="invalidate",
                    native_reason_code=native_error, native_return_code=native_return_code, cache_removed=True,
                ) from None

    def cleanup_failure(self, error: SmbOAuthError) -> None:
        if error.reason_code == "SMB_CACHE_CLEANUP_FAILED":
            error.details["cleanup"] = {"status": "failed", "not_retried": True}
            return
        try:
            self.invalidate()
            error.details["cleanup"] = {"status": "success"}
        except SmbOAuthError as cleanup:
            error.details["cleanup"] = {
                "status": "failed", "reason_code": cleanup.reason_code, **cleanup.details,
            }

    def ensure(self, *, force: bool = False) -> dict[str, Any]:
        with AUTH_LOCK:
            try:
                validate_config()
                current = cache_state()
                if (
                    not force and current.get("nonempty") and current == self.cache
                    and self.expires_at > self.clock() + REFRESH_MARGIN_SECONDS
                ):
                    return self._evidence("reused")
                self.invalidate()
                native = self._native()
                try:
                    with sdk_log_guard():
                        if self.credential is None:
                            self.credential = self.credential_factory()
                        access = self.credential.get_token(STORAGE_SCOPE, logging_enable=False)
                        value, expires_on = access.token, access.expires_on
                except SmbOAuthError:
                    raise
                except Exception:
                    raise SmbOAuthError("SMB_MI_TOKEN_ACQUISITION_FAILED", stage="acquire_token") from None
                claims, token_expiry = token_claims(
                    value, expires_on, self.expected_oid, self.expected_tid, self.clock(),
                )
                started = self.clock()
                lifetime = native.set(self.endpoint, value)
                if type(lifetime) is not int or lifetime <= MIN_LIFETIME_SECONDS:
                    raise SmbOAuthError("SMB_NATIVE_LIFETIME_INVALID", stage="native_set")
                expiry = min(token_expiry, started + lifetime)
                cache = cache_state()
                if not cache.get("nonempty"):
                    raise SmbOAuthError("SMB_CACHE_MISSING", stage="after_native_set")
                if expiry <= self.clock() + MIN_LIFETIME_SECONDS:
                    raise SmbOAuthError("SMB_NATIVE_LIFETIME_INVALID", stage="after_native_set")
                self.claims, self.expires_at, self.cache = claims, expiry, cache
                return self._evidence("refreshed")
            except SmbOAuthError as exc:
                self.cleanup_failure(exc)
                raise
            except Exception:
                error = SmbOAuthError("SMB_AUTH_INTERNAL_FAILURE", stage="prepare")
                self.cleanup_failure(error)
                raise error from None

    def _evidence(self, action: str) -> dict[str, Any]:
        return {
            "credential_source": "hosted_managed_identity", "claims": dict(self.claims),
            "claims_validation": "sdk_payload_checks_not_signature_verification",
            "native_version": self.native.version, "credential_uid": CREDENTIAL_UID,
            "cache": {"present": True, "owner_uid": CREDENTIAL_UID, "mode": "0600"},
            "refresh_action": action, "credential_expires_at": self.expires_at,
        }
