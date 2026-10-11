"""fa-shell-v1: one run_command in the Agent's own Hosted sandbox (TOOL-04, SP-09, SP-29, E-10).

Pure checks run everywhere; process checks need Linux (the desktop image, UID10001) and are skipped elsewhere.
"""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared_context import behavior_assistant_hosted as hosted
from shared_context import behavior_shell_capability as shell
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_capabilities import (
    BUILTIN_CAPABILITIES, MAX_CAPABILITIES, CapabilityCapture, CapabilityRef, available_grants, validate_refs,
)
from shared_context.behavior_capability_prompt import addendum, session_needs
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure

LINUX = sys.platform.startswith("linux")
linux_only = pytest.mark.skipif(not LINUX or os.geteuid() == 0, reason="needs a non-root Linux Hosted-like process")
CANARY = "fa-sp29-canary-7f3c1e"
PROTECTED = {"IDENTITY_ENDPOINT": "http://localhost:42356/msi/token", "IDENTITY_HEADER": CANARY}


def grant():
    return available_grants(None, ("fa-shell-v1",))["fa-shell-v1"]


def test_builtin_is_one_run_command_tool_and_capabilities_allow_twelve():
    assert hosted.CAPABILITY_HANDLERS[shell.ENTRY_ID] is shell.shell_capability
    assert BUILTIN_CAPABILITIES[shell.ENTRY_ID].tools == ("run_command",) and not grant().vision
    assert MAX_CAPABILITIES == 12
    refs = tuple(CapabilityRef(entry_id=f"entry-{index:02d}", entry_sha256="0" * 64) for index in range(12))
    assert validate_refs(refs) == refs
    with pytest.raises(ValueError):
        validate_refs(refs + (CapabilityRef(entry_id="entry-12", entry_sha256="0" * 64),))
    needs = session_needs(CapabilityCapture(head_selection_id=None, grants=(grant(),)))
    assert needs.custom_tools == ("run_command",) and needs.builtin_tools == ()
    text = addendum((grant(),))
    assert "fa-shell-v1" in text and "not the user's device" in text and "Tools: run_command." in text


def test_output_keeps_first_and_last_bytes_and_counts_the_rest():
    small = shell._Output()
    small.add(b"a" * 9000)
    small.add(b"b" * 7000)
    exact = small.result(0)
    assert exact.output == "a" * 9000 + "b" * 7000 and not exact.truncated and exact.output_bytes == 16000
    big = shell._Output()
    for _ in range(10):
        big.add(b"x" * 10000)
    big.add(b"END")
    result = big.result(3)
    assert result.truncated and result.output_bytes == 100003 and result.exit_code == 3
    assert result.output.startswith("x" * 8192) and result.output.endswith("x" * 8189 + "END")
    assert "[... 83619 bytes omitted ...]" in result.output


@pytest.mark.parametrize("environ, protected", [
    (PROTECTED, True),
    ({"MSI_ENDPOINT": "http://localhost/msi", "MSI_SECRET": CANARY}, True),
    ({}, False),  # IMDS: any same-VM process can ask for the Agent's token
    ({"MSI_ENDPOINT": "http://localhost/msi"}, False),
    ({"AZURE_CLIENT_ID": "x", "AZURE_TENANT_ID": "y", "AZURE_FEDERATED_TOKEN_FILE": "/var/run/token"}, False),
    ({**PROTECTED, "AZURE_CLIENT_SECRET": "secret"}, False),
    ({**PROTECTED, "AZURE_CLIENT_CERTIFICATE_PATH": "/cert.pem"}, False),
])
def test_identity_must_need_a_secret_only_the_hosted_process_holds(environ, protected):
    assert shell._identity_protected(environ) is protected


def test_turn_is_refused_before_any_tool_when_the_sandbox_cannot_keep_secrets(monkeypatch, tmp_path):
    session = SimpleNamespace(root=tmp_path / "journal" / "launch")

    def refuse(environ=None):
        raise Problem(503, "SHELL_UNAVAILABLE", "the Hosted main process is still dumpable")
    monkeypatch.setattr(shell, "prerequisites", refuse)
    with pytest.raises(Problem) as refused:
        shell.shell_capability(session, grant())
    assert refused.value.status == 503 and refused.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert refused.value.body["error"]["capability"] == "fa-shell-v1"
    changed = grant().model_copy(update={"entry_sha256": "1" * 64})
    with pytest.raises(Problem):
        shell.shell_capability(session, changed)
    if not LINUX:
        monkeypatch.undo()
        with pytest.raises(Problem) as windows:
            shell.shell_capability(session, grant())
        assert windows.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE"


def factory(tmp_path, monkeypatch, keep_active=None):
    """The real factory for a Hosted-like session whose process holds a protected identity secret."""
    for name, value in PROTECTED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(shell, "is_dumpable", lambda: False)
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    session = SimpleNamespace(root=root / "launch", **({"keep_active": keep_active} if keep_active else {}))
    specs, models = shell.shell_capability(session, grant())
    boundary = AssistantModelBoundary(None, text_reply=True, capability_results=models)
    return specs[0], boundary, root


def run(spec, boundary, command):
    output = asyncio.run(spec.handler({"arguments": {"command": command}, "tool_call_id": "call-1"}))
    return shell.CommandResult.model_validate_json(boundary.tool_result(spec.name, output.text))


@linux_only
def test_command_runs_in_private_files_with_only_allowlisted_environment(tmp_path, monkeypatch):
    spec, boundary, root = factory(tmp_path, monkeypatch)
    monkeypatch.setenv("FA_CAPABILITY_SECRET_EXAMPLE", CANARY)
    result = run(spec, boundary, "pwd; env | sort; echo done > note.txt; exit 3")
    lines = result.output.splitlines()
    assert result.exit_code == 3 and lines[0] == str(root / "desktop" / "files")
    names = {line.split("=", 1)[0] for line in lines[1:]}
    assert names <= {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM", "PAGER", "SHELL", "PWD", "SHLVL", "_"}
    assert CANARY not in result.output
    assert (root / "desktop" / "files" / "note.txt").read_text() == "done\n"
    for path in (root / "desktop", root / "desktop" / "files", root / "shell" / "home", root / "shell" / "tmp"):
        assert os.stat(path).st_mode & 0o777 == 0o700
    with pytest.raises(ToolFailure) as bad:
        asyncio.run(spec.handler({"arguments": {"command": "id", "cwd": "/"}}))
    assert bad.value.public_code == "CAPABILITY_ARGUMENTS"


@linux_only
def test_signal_exit_and_large_output_are_reported_not_hidden(tmp_path, monkeypatch):
    spec, boundary, _ = factory(tmp_path, monkeypatch)
    assert run(spec, boundary, "kill -TERM $$").exit_code == -15
    big = run(spec, boundary, "head -c 100000 /dev/zero | tr '\\0' a")
    assert big.truncated and big.output_bytes == 100000 and len(big.output) < 17000


@linux_only
def test_background_processes_stop_when_the_command_exits(tmp_path, monkeypatch):
    spec, boundary, _ = factory(tmp_path, monkeypatch)
    started = time.monotonic()
    result = run(spec, boundary, "sleep 300 & echo $!")
    assert time.monotonic() - started < shell.DRAIN_SECONDS + 3
    pid = int(result.output.strip())
    time.sleep(0.2)
    assert not Path(f"/proc/{pid}").exists() or "Z" in Path(f"/proc/{pid}/stat").read_text().split()[2]


@linux_only
def test_cancelled_command_stops_its_whole_group_and_never_reruns(tmp_path, monkeypatch):
    beats = []

    async def keep_active():
        beats.append(time.monotonic())
    monkeypatch.setattr(shell, "KEEPALIVE_SECONDS", 0.2)
    spec, _, root = factory(tmp_path, monkeypatch, keep_active)
    marker = root / "desktop" / "files" / "runs.txt"

    async def main():
        task = asyncio.create_task(spec.handler({"arguments": {
            "command": "echo run >> runs.txt; sleep 300 & echo $! > child.pid; sleep 300"}}))
        await asyncio.sleep(1.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(main())
    child = int((root / "desktop" / "files" / "child.pid").read_text())
    time.sleep(0.2)
    assert not Path(f"/proc/{child}").exists() or "Z" in Path(f"/proc/{child}/stat").read_text().split()[2]
    assert marker.read_text() == "run\n" and len(beats) >= 3
    count = len(beats)
    time.sleep(0.5)
    assert len(beats) == count


HOSTED_CHILD = textwrap.dedent("""
    import asyncio, json, os, sys
    from pathlib import Path
    from types import SimpleNamespace
    from shared_context import behavior_shell_capability as shell
    from shared_context.behavior_capabilities import available_grants
    from shared_context.behavior_process_hardening import disable_dumpable
    root = Path(sys.argv[1])
    command = "env; tr '\\\\0' '\\\\n' < /proc/$PPID/environ 2>&1; ls /proc/$PPID/fd 2>&1 | head -3"
    if sys.argv[2] == "hardened":
        disable_dumpable()
        spec = shell.shell_capability(SimpleNamespace(root=root / "launch"),
                                      available_grants(None, ("fa-shell-v1",))["fa-shell-v1"])[0][0]
        text = asyncio.run(spec.handler({"arguments": {"command": command}})).text
    else:
        try:
            shell.shell_capability(SimpleNamespace(root=root / "launch"),
                                   available_grants(None, ("fa-shell-v1",))["fa-shell-v1"])
            text = json.dumps({"refused": False})
        except Exception as error:
            refused = {"refused": getattr(error, "status", None) == 503}
            (root / "files").mkdir(mode=0o700)
            result = asyncio.run(shell.run_command(shell.prerequisites.__globals__["shutil"].which("bash"), command,
                cwd=root / "files", env=shell.command_environment(root, root)))
            text = json.dumps({**refused, "output": result.output})
    print(text)
""")


@linux_only
@pytest.mark.parametrize("mode", ["hardened", "dumpable"])
def test_sp29_canary_in_the_hosted_process_never_reaches_a_command(tmp_path, mode):
    """Counterexample first: a dumpable Hosted leaks the canary to /proc/$PPID/environ, and the factory refuses it."""
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    source = Path(shell.__file__).resolve().parents[1]
    completed = subprocess.run([sys.executable, "-c", HOSTED_CHILD, str(root), mode], cwd=source,
                               # Like the platform's, the canary is in the Hosted process's initial environment.
                               env={"PATH": os.environ["PATH"], "PYTHONPATH": str(source), "HOME": str(tmp_path),
                                    **PROTECTED},
                               capture_output=True, text=True, timeout=60, check=True)
    result = json.loads(completed.stdout)
    if mode == "hardened":
        assert CANARY not in result["output"] and "Permission denied" in result["output"]
    else:
        assert result["refused"] is True and CANARY in result["output"]
