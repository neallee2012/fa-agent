"""Prepare only the POC directory, then exec the agent without root credentials."""
from __future__ import annotations

import json
import os
import platform
import stat
import sys
from pathlib import Path

PLATFORM_HOME = Path("/home/session")
APP_UID = 10001
RECEIPT_ENV = "FA_BOOTSTRAP_RECEIPT"


def process_identity() -> dict:
    fields = {}
    for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
        name, _, value = line.partition(":")
        if name in ("Uid", "Gid", "Groups"):
            fields[name] = [int(item) for item in value.split()]
        elif name in ("CapEff", "CapPrm", "CapAmb"):
            fields[name] = value.strip()
    if set(fields) != {"Uid", "Gid", "Groups", "CapEff", "CapPrm", "CapAmb"}:
        raise RuntimeError("process identity/capability fields are unavailable")
    return fields


def _account() -> tuple[int, int]:
    import pwd

    account = pwd.getpwnam("app")
    if account.pw_uid != APP_UID or account.pw_gid <= 0:
        raise RuntimeError("app must have UID10001 and a non-root primary group")
    return account.pw_uid, account.pw_gid


def _attributes(info) -> dict:
    return {"uid": info.st_uid, "gid": info.st_gid, "mode": oct(stat.S_IMODE(info.st_mode))}


def prepare_private_directory(home: Path, uid: int, gid: int) -> dict:
    if home != PLATFORM_HOME or any(node.is_symlink() for node in (home, *home.parents)):
        raise RuntimeError("bootstrap only accepts the observed /home/session without symlinks")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    home_fd = os.open(home, flags)
    try:
        before = _attributes(os.fstat(home_fd))
        created = False
        try:
            os.mkdir(".fa-m1", mode=0o700, dir_fd=home_fd)
            created = True
        except FileExistsError:
            pass
        private_fd = os.open(".fa-m1", flags, dir_fd=home_fd)
        try:
            if created:
                os.fchown(private_fd, uid, gid)
                os.fchmod(private_fd, 0o700)
            info = os.fstat(private_fd)
            if not stat.S_ISDIR(info.st_mode) or _attributes(info) != {
                "uid": uid, "gid": gid, "mode": "0o700",
            }:
                raise RuntimeError(f"existing private directory has unexpected ownership/mode: {_attributes(info)}; no repair")
            if _attributes(os.fstat(home_fd)) != before:
                raise RuntimeError("HOME metadata changed during initialization")
            return {
                "home": str(home), "home_attributes": before,
                "private_directory": str(home / ".fa-m1"), "private_attributes": _attributes(info),
                "created": created, "existing_data_modified": False,
            }
        finally:
            os.close(private_fd)
    finally:
        os.close(home_fd)


def drop_privileges(uid: int, gid: int) -> dict:
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    identity = process_identity()
    if (identity["Uid"] != [uid] * 4 or identity["Gid"] != [gid] * 4 or identity["Groups"]
            or any(int(identity[name], 16) for name in ("CapEff", "CapPrm", "CapAmb"))):
        raise RuntimeError("privilege drop did not clear root credentials/capabilities")
    return identity


def bootstrap() -> None:
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise RuntimeError("bootstrap requires initial Linux UID0, not a root agent runtime")
    if os.environ.get("HOME") != str(PLATFORM_HOME):
        raise RuntimeError("unexpected platform HOME; no alternate-directory fallback")
    uid, gid = _account()
    receipt = {"initial_uid": os.geteuid(), **prepare_private_directory(PLATFORM_HOME, uid, gid)}
    receipt["runtime_identity"] = drop_privileges(uid, gid)
    os.environ[RECEIPT_ENV] = json.dumps(receipt, separators=(",", ":"))
    os.environ["USER"] = os.environ["LOGNAME"] = "app"
    print(json.dumps({"event": "private_home_bootstrap", **receipt}), flush=True)
    entry = str(Path(__file__).resolve().with_name("hosted_main.py"))
    os.execv(sys.executable, [sys.executable, entry])


if __name__ == "__main__":
    try:
        bootstrap()
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(json.dumps({
            "error": "BOOTSTRAP_FAILED", "type": type(exc).__name__,
            "errno": getattr(exc, "errno", None), "message": str(exc),
            "effective_uid": getattr(os, "geteuid", lambda: None)(),
            "home": os.environ.get("HOME") if os.environ.get("HOME") in ("/home/session", "/home/app", None) else "[unexpected]",
        }), file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
