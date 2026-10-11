from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess

from smb_oauth import NativeLibrary, cache_state, validate_config


def main() -> None:
    assert os.geteuid() == 0
    assert importlib.metadata.version("azure-identity") == "1.25.3"
    assert importlib.metadata.version("PyYAML") == "6.0.3"
    account = pwd.getpwnam("azfilesuser")
    assert account.pw_uid == account.pw_gid == 10002
    validate_config()
    assert cache_state() == {"present": False}
    assert Path("/etc/request-key.d/cifs.spnego.conf").read_text().split() == [
        "create", "cifs.spnego", "*", "*", "/usr/local/sbin/cifs.upcall", "%k",
    ]
    native = NativeLibrary()
    assert native.version == "1.0.12"
    commands = [
        ["azfilesauthmanager", "--version"],
        ["dpkg-query", "--show", "--showformat=${Package} ${Version}\n", "cifs-utils"],
        ["/usr/local/sbin/cifs.upcall", "--version"],
        ["klist", "-V"],
        ["dmesg", "--kernel", "--syslog", "--buffer-size=65536", "--color=never", "--nopager", "--help"],
    ]
    versions = {}
    for command in commands:
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Image CLI check failed: {command!r}; exit={exc.returncode}; "
                f"stdout={exc.stdout[-1000:]!r}; stderr={exc.stderr[-1000:]!r}"
            ) from exc
        versions[command[0]] = (result.stdout + result.stderr).strip()[:500]
    assert "1.0.12" in versions["azfilesauthmanager"]
    assert versions["/usr/local/sbin/cifs.upcall"].endswith("7.4")
    assert shutil.which("mount.cifs")
    for executable in ("/usr/sbin/request-key", "/usr/bin/keyctl"):
        assert os.access(executable, os.X_OK), executable
    print(json.dumps({
        "scope": "image_abi_cli_only_not_hosted_mount",
        "library_version": native.version,
        "native_log_sink_verified": True,
        "upcall_source_commit": "edac7178bec9520fb57d14946e67f5dd33b82d43",
        "credential_cache_absent": True,
        "versions": versions,
    }, indent=2))


if __name__ == "__main__":
    main()
