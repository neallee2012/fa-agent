"""Root-to-app identity helpers; behavior_server_main.hosted_bootstrap is the only caller."""
from __future__ import annotations

import os
import stat
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


def drop_privileges(uid: int, gid: int) -> dict:
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    identity = process_identity()
    if (identity["Uid"] != [uid] * 4 or identity["Gid"] != [gid] * 4 or identity["Groups"]
            or any(int(identity[name], 16) for name in ("CapEff", "CapPrm", "CapAmb"))):
        raise RuntimeError("privilege drop did not clear root credentials/capabilities")
    return identity
