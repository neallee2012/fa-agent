"""Pinned SDK 1.0.13 / runtime 1.0.83: the model's run_command reaches the Hosted shell and its result returns.

Linux only (the desktop image as UID10001); scripted provider, no network or model.
"""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

from shared_context import behavior_shell_capability as shell
from shared_context.behavior_capabilities import CapabilityCapture, available_grants
from shared_context.behavior_capability_prompt import session_needs
from shared_context.m2_copilot import run_copilot
from test_m2_copilot import ScriptedEndpoint, runtime_path, tool_call, workspace

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or os.geteuid() == 0,
                                reason="the shell runs only in a non-root Linux Hosted runtime")


def test_model_command_runs_in_the_private_sandbox_and_returns_its_output(workspace, runtime_path, monkeypatch):
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "fa-native-canary")
    monkeypatch.setattr(shell, "is_dumpable", lambda: False)
    grant = available_grants(None, ("fa-shell-v1",))["fa-shell-v1"]
    journal = workspace / "journal"
    journal.mkdir(mode=0o700)
    specs, _ = shell.shell_capability(SimpleNamespace(root=journal / "launch"), grant)
    endpoint = ScriptedEndpoint([
        tool_call("run_command", {"command": "pwd; nproc; grep MemTotal /proc/meminfo; env | grep -c IDENTITY || true"},
                  "call-shell"),
        {"content": "DONE"},
    ])
    completed = []

    def on_event(event):
        if event.get("type") == "tool.execution_complete":
            completed.append(event["data"])

    async def current():
        return True
    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work", sdk_session_id="shell-native",
        resume=False, prompt="How many CPUs and how much memory?", tools=list(specs),
        api_url="https://context.invalid", token_provider=endpoint.token, on_event=on_event,
        timeout_seconds=90, runtime_path=runtime_path, http_transport=endpoint.transport,
        capability_session=session_needs(CapabilityCapture(head_selection_id=None, grants=(grant,))),
        capability_current=current,
    ))
    assert result["final_message"] == "DONE"
    [done] = completed
    assert done["tool_call_id"] == "call-shell" and done["success"] is True
    output = json.loads(done["result"]["content"])
    lines = output["output"].splitlines()
    assert output["exit_code"] == 0 and lines[0] == str(journal / "desktop" / "files")
    assert int(lines[1]) >= 1 and lines[2].startswith("MemTotal:") and lines[3] == "0"
