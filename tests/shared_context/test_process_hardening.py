"""C3 counterexample: a desktop child cannot read the hardened Hosted main process's
/proc/<pid>/environ, while the same read works before hardening."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from shared_context.behavior_desktop import EDITOR
from shared_context.behavior_process_hardening import disable_dumpable, is_dumpable

SOURCE = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
CANARY = "fa-hardening-canary-7f3e"
LINUX = sys.platform.startswith("linux")

# Runs as the "Hosted main" in its own interpreter so the canary is in its exec-time environment.
MAIN = r"""
import json, os, subprocess, sys
from pathlib import Path
from shared_context.behavior_process_hardening import disable_dumpable, is_dumpable

def child_read(env):
    result = subprocess.run(["cat", f"/proc/{os.getpid()}/environ"], env=env, capture_output=True)
    return {"returncode": result.returncode, "leaked": sys.argv[1].encode() in result.stdout}

report = {"before": child_read({"PATH": "/usr/bin:/bin"})}
report["hardened"] = disable_dumpable()
report["dumpable"] = is_dumpable()
report["after"] = child_read({"PATH": "/usr/bin:/bin"})
report["own_environ_still_usable"] = os.environ.get("FA_CANARY") == sys.argv[1]
check = [sys.executable, "-c", "from shared_context.behavior_process_hardening import is_dumpable; print(is_dumpable())"]
report["exec_child_dumpable"] = subprocess.run(check, capture_output=True, text=True,
                                               env={**os.environ, "FA_CANARY": ""}).stdout.strip()
if len(sys.argv) > 2:
    from shared_context.behavior_desktop_tools import DesktopToolbox
    root = Path(sys.argv[2])
    tools = DesktopToolbox(root)
    try:
        tools.call("desktop_observe", {})
        report["desktop_child"] = child_read(tools._desktop.display_env())
    finally:
        report["desktop_closed"] = tools.close()
print(json.dumps(report))
"""


def run_main(*extra):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(SOURCE), "FA_CANARY": CANARY,
           "HOME": os.environ.get("HOME", "/tmp"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-c", MAIN, CANARY, *extra], env=env, capture_output=True,
                            text=True, timeout=120, check=True)
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(LINUX, reason="non-Linux behaviour")
def test_non_linux_has_no_flag_to_set():
    assert is_dumpable() is None and disable_dumpable() is False


@pytest.mark.skipif(not LINUX, reason="PR_SET_DUMPABLE is Linux-only")
def test_hardened_main_environ_is_unreadable_by_same_uid_children():
    report = run_main()
    assert report["before"] == {"returncode": 0, "leaked": True}
    assert report["hardened"] is True and report["dumpable"] is False
    assert report["after"]["returncode"] != 0 and report["after"]["leaked"] is False
    assert report["own_environ_still_usable"] is True
    # Exec resets the flag, so every executed child must keep receiving an allowlisted environment.
    assert report["exec_child_dumpable"] == "True"


@pytest.mark.skipif(not LINUX or shutil.which(EDITOR) is None or shutil.which("Xvfb") is None,
                    reason="desktop child case needs the Linux desktop image")
def test_desktop_child_cannot_read_hardened_main_environ(tmp_path):
    root = tmp_path / "agent-a"
    root.mkdir(mode=0o700)
    report = run_main(str(root))
    assert report["desktop_child"]["returncode"] != 0 and report["desktop_child"]["leaked"] is False
    assert report["desktop_closed"]["processes_stopped"] and report["desktop_closed"]["runtime_removed"]
