from __future__ import annotations

import ctypes
import errno
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
import uuid
from pathlib import Path

LOG_ROOT = Path("/run/fa-smb-diagnostics")
UPCALL = "/usr/local/sbin/cifs.upcall"
CACHE = Path("/tmp/krb5cc_10002")
RULE = "create cifs.spnego * * /usr/local/bin/python /app/smb_diagnostics.py %k"
LOG_LIMIT = 8192
CURL = "/usr/bin/curl"
KEY_RULE = "create user fa-poc-callout:* * |/bin/echo fa-poc-probe"
KEY_RULE_PATH = Path("/etc/request-key.d/fa-poc-callout.conf")


def path_metadata(path: Path) -> dict:
    try:
        info = path.stat()
        return {
            "present": True, "regular_file": stat.S_ISREG(info.st_mode),
            "directory": stat.S_ISDIR(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
            "mode": stat.S_IMODE(info.st_mode), "size": info.st_size,
            "executable": os.access(path, os.X_OK), "device": info.st_dev, "inode": info.st_ino,
        }
    except OSError as exc:
        return {"present": False, "errno": exc.errno}


def process_metadata() -> dict:
    result = {"uid": os.getuid(), "euid": os.geteuid(), "gid": os.getgid(),
              "cache": path_metadata(CACHE)}
    try:
        namespace = os.readlink("/proc/self/ns/mnt")
        result["mount_namespace"] = namespace if re.fullmatch(r"mnt:\[\d+\]", namespace) else "unrecognized"
    except OSError as exc:
        result["namespace_errno"] = exc.errno
    return result


def _open_log(flags: int) -> int:
    info = LOG_ROOT.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError(errno.EACCES, "Untrusted diagnostic directory")
    fd = os.open(LOG_ROOT / "upcall.jsonl", flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        os.close(fd)
        raise PermissionError(errno.EACCES, "Untrusted diagnostic file")
    return fd


def begin(shared_root: Path, *, probe_key_service: bool = True) -> dict:
    result = {"process": process_metadata(), "paths": {
        name: path_metadata(Path(name)) for name in (
            str(shared_root), "/sbin/request-key", UPCALL, "/usr/local/bin/python",
            "/etc/krb5.conf", "/etc/request-key.d/cifs.spnego.conf",
        )
    }}
    if probe_key_service and os.environ.get("POC_MI_KEY_DIAGNOSTICS") == "1":
        result["key_service"] = _child_probe("--key-service-probe", 4)
    try:
        LOG_ROOT.mkdir(mode=0o700, exist_ok=True)
        fd = _open_log(os.O_CREAT | os.O_WRONLY)
        try:
            os.ftruncate(fd, 0)
        finally:
            os.close(fd)
        rule = Path("/etc/request-key.d/cifs.spnego.conf").read_text(encoding="utf-8")
        result.update(status="ready", request_key_rule_matches=rule.strip() == RULE)
    except OSError as exc:
        result.update(status="unavailable", errno=exc.errno)
    return result


def _append(record: dict) -> None:
    fd = _open_log(os.O_WRONLY | os.O_APPEND)
    with os.fdopen(fd, "wb") as stream:
        value = (json.dumps(record, separators=(",", ":")) + "\n").encode()
        if os.fstat(stream.fileno()).st_size + len(value) > LOG_LIMIT:
            raise OSError(errno.EFBIG, "Diagnostic limit")
        stream.write(value)


def finish() -> dict:
    try:
        fd = _open_log(os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            data = stream.read(LOG_LIMIT + 1)
        if len(data) > LOG_LIMIT:
            return {"status": "unavailable", "reason": "size_limit"}
        events = [json.loads(line) for line in data.splitlines()]
        # The root-only file is produced by this wrapper, never by the native helper.
        for event in events:
            if event.get("event") not in ("entry", "exit", "timeout", "launch_error"):
                return {"status": "unavailable", "reason": "invalid_event"}
        return {"status": "available", "events": events, "invoked": bool(events)}
    except OSError as exc:
        return {"status": "unavailable", "errno": exc.errno}
    except (ValueError, TypeError, AttributeError):
        return {"status": "unavailable", "reason": "invalid_json"}


def mount_message(text: str) -> dict:
    match = re.search(r"mount error\((\d{1,4})\)", text or "", re.IGNORECASE)
    if match:
        number = int(match[1])
        return {"errno": number, "reason": errno.errorcode.get(number, "unknown_errno")}
    return {"reason": "unclassified"}


def curl_metadata(completed: subprocess.CompletedProcess) -> dict:
    result = {"return_code": completed.returncode}
    fields = completed.stdout.strip().split("|")
    if len(fields) != 6 or not re.fullmatch(r"\d{3}", fields[0]):
        return {**result, "status": "invalid_metrics"}
    try:
        address = str(ipaddress.ip_address(fields[1])) if fields[1] else None
        timings = {}
        for name, value in zip(("dns_seconds", "connect_seconds", "tls_seconds", "total_seconds"), fields[2:]):
            if not re.fullmatch(r"\d{1,6}\.\d{1,6}", value):
                raise ValueError()
            timings[name] = float(value)
        return {**result, "status": "available", "http_status": int(fields[0]),
                "remote_ip": address, **timings}
    except ValueError:
        return {**result, "status": "invalid_metrics"}


def network_sample(host: str) -> dict:
    result = {"uid": os.getuid(), "euid": os.geteuid()}
    try:
        addresses = sorted({
            str(ipaddress.ip_address(item[4][0]))
            for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        })
        result["dns"] = {"status": "available", "addresses": addresses[:8]}
    except OSError as exc:
        result["dns"] = {"status": "failed", "errno": exc.errno}
    try:
        with socket.create_connection((host, 445), timeout=2):
            pass
        result["tcp_445"] = {"status": "connected"}
    except OSError as exc:
        result["tcp_445"] = {"status": "failed", "errno": exc.errno,
                             "timeout": isinstance(exc, TimeoutError)}
    try:
        completed = subprocess.run(
            [CURL, "--disable", "--silent", "--output", "/dev/null", "--head",
             "--proto", "=https", "--connect-timeout", "3", "--max-time", "5",
             "--write-out",
             "%{http_code}|%{remote_ip}|%{time_namelookup}|%{time_connect}|%{time_appconnect}|%{time_total}",
             f"https://{host}/"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False, timeout=6,
        )
        result["https_no_auth"] = curl_metadata(completed)
    except subprocess.TimeoutExpired:
        result["https_no_auth"] = {"status": "process_timeout"}
    except OSError as exc:
        result["https_no_auth"] = {"status": "launch_failed", "errno": exc.errno}
    return result


def network_samples() -> dict:
    account = os.environ.get("POC_STORAGE_ACCOUNT_NAME", "")
    if not re.fullmatch(r"[a-z0-9]{3,24}", account):
        return {"status": "invalid_storage_account"}
    host = f"{account}.file.core.windows.net"
    result = {
        "status": "available", "scope": "unauthenticated_connectivity_only", "host": host,
        "proxy_environment_present": {
            name: bool(os.environ.get(name))
            for name in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY")
        },
        "probes": [],
    }
    # Mirror the native library's effective UID change in this isolated child only.
    previous = os.geteuid()
    try:
        for uid in (0, 10002):
            os.seteuid(uid)
            result["probes"].append(network_sample(host))
    finally:
        os.seteuid(previous)
    return result


def _child_probe(argument: str, timeout: int) -> dict:
    try:
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), argument],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=timeout, check=False,
        )
        if completed.returncode != 0 or len(completed.stdout) > 4096:
            return {"status": "probe_failed", "return_code": completed.returncode}
        return json.loads(completed.stdout)
    except subprocess.TimeoutExpired:
        return {"status": "probe_timeout"}
    except OSError as exc:
        return {"status": "probe_launch_failed", "errno": exc.errno}
    except ValueError:
        return {"status": "invalid_probe_json"}


def network_probe() -> dict:
    return _child_probe("--network-probe", 26)


def key_service_sample() -> dict:
    library = ctypes.CDLL("libkeyutils.so.1", use_errno=True)
    serial, pointer, size = ctypes.c_int32, ctypes.c_char_p, ctypes.c_size_t
    signatures = {
        "keyctl_join_session_keyring": ([pointer], serial),
        "add_key": ([pointer, pointer, pointer, size, serial], serial),
        "request_key": ([pointer, pointer, pointer, serial], serial),
        "keyctl_read": ([serial, ctypes.c_void_p, size], ctypes.c_long),
        "keyctl_unlink": ([serial, serial], ctypes.c_long),
    }
    for name, (arguments, returns) in signatures.items():
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, returns
    ring = library.keyctl_join_session_keyring(None)
    if ring < 0:
        return {"status": "anonymous_keyring_failed", "errno": ctypes.get_errno()}
    result = {"status": "available", "scope": "isolated_child_dummy_keys_only"}
    nonce = uuid.uuid4().hex.encode("ascii")
    local = library.add_key(b"user", b"fa-poc-local:" + nonce, b"probe", 5, ring)
    if local < 0:
        result["local_add"] = {"status": "failed", "errno": ctypes.get_errno()}
    else:
        result["local_add"] = {"status": "success", "unlink_return_code": library.keyctl_unlink(local, ring)}
    requested = library.request_key(b"user", b"fa-poc-callout:" + nonce, b"probe", ring)
    if requested < 0:
        result["request_key"] = {"status": "failed", "errno": ctypes.get_errno()}
        return result
    try:
        buffer = ctypes.create_string_buffer(64)
        length = library.keyctl_read(requested, buffer, len(buffer))
        result["request_key"] = {
            "status": "success", "payload_matches": length == 13 and buffer.raw[:length] == b"fa-poc-probe\n",
        }
        if length < 0:
            result["request_key"].update(status="read_failed", errno=ctypes.get_errno())
    finally:
        result["request_key"]["unlink_return_code"] = library.keyctl_unlink(requested, ring)
    return result


def kernel_message(text: str) -> dict | None:
    patterns = (
        ("mount_failed", r"cifs_mount failed w/return code\s*=\s*(-?\d{1,5})"),
        ("session_setup_failed", r"Send error in SessSetup\s*=\s*(-?\d{1,5})"),
        ("spnego_key_failed", r"(?:get|getting) (?:cifs )?spnego key.*?(?:=|:)\s*(-?\d{1,5})"),
        ("tree_connect_failed", r"(?:tree connect|TCon).*?(?:failed|error).*?(?:=|:)\s*(-?\d{1,5})"),
    )
    for category, pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return {"category": category, "code": int(match[1])}
    match = re.search(r"Status code returned (0x[0-9a-f]{8})", text, re.IGNORECASE)
    if match:
        return {"category": "server_status", "code": match[1].lower()}
    if "Verify user has a krb5 ticket and keyutils is installed" in text:
        return {"category": "check_ticket_and_keyutils"}
    return None


def upcall(key: str) -> int:
    if not re.fullmatch(r"[0-9]{1,10}", key):
        return 64
    try:
        _append({"event": "entry", "process": process_metadata()})
    except OSError as exc:
        print(f"POC_SMB_DIAGNOSTIC_ERROR entry errno={exc.errno}", file=sys.stderr)
    try:
        result = subprocess.run(
            [UPCALL, key], check=False, timeout=25,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        code, record = result.returncode, {"event": "exit", "return_code": result.returncode}
    except subprocess.TimeoutExpired:
        code, record = 124, {"event": "timeout"}
    except OSError as exc:
        code, record = 126, {"event": "launch_error", "errno": exc.errno}
    try:
        _append(record)
    except OSError as exc:
        print(f"POC_SMB_DIAGNOSTIC_ERROR exit errno={exc.errno}", file=sys.stderr)
    return code if code >= 0 else 128 - code


def self_check() -> None:
    proof = begin(Path("/team"), probe_key_service=False)
    assert proof["status"] == "ready" and proof["request_key_rule_matches"], proof
    for path in ("/sbin/request-key", UPCALL, "/usr/local/bin/python"):
        assert proof["paths"][path]["executable"], path
    assert path_metadata(Path(CURL))["executable"], CURL
    if os.environ.get("POC_MI_KEY_DIAGNOSTICS") == "1":
        assert KEY_RULE_PATH.read_text().strip() == KEY_RULE
        assert path_metadata(Path("/bin/echo"))["executable"]
        ctypes.CDLL("libkeyutils.so.1")
    _append({"event": "entry", "process": process_metadata()})
    _append({"event": "exit", "return_code": 0})
    assert len(finish()["events"]) == 2
    begin(Path("/team"), probe_key_service=False)
    assert finish()["events"] == []
    print(json.dumps({"scope": "diagnostic_image_only_not_kernel_upcall", "status": "PASS"}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        self_check()
    elif sys.argv[1:] == ["--network-probe"]:
        print(json.dumps(network_samples()))
    elif sys.argv[1:] == ["--key-service-probe"]:
        print(json.dumps(key_service_sample()))
    elif len(sys.argv) == 2:
        raise SystemExit(upcall(sys.argv[1]))
    else:
        raise SystemExit(64)
