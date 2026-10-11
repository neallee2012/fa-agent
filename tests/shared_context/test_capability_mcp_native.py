"""One pinned SDK 1.0.13 / runtime 1.0.83 run with an offline stdio MCP server and a scripted provider."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from shared_context.behavior_capabilities import CapabilityCapture, CapabilityGrant
from shared_context.behavior_capability_prompt import session_needs
from shared_context.tool_catalog import StdioEntry
from shared_context.m2_copilot import run_copilot
from test_m2_copilot import ScriptedEndpoint, runtime_path, tool_call, workspace

SERVER = Path(__file__).with_name("fake_mcp_stdio_server.py")


@pytest.mark.parametrize("work_current", [True, False], ids=["current-work", "cancelled-work"])
def test_pinned_runtime_runs_only_the_captured_stdio_mcp_tool(workspace, runtime_path, work_current):
    env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH") if name in os.environ}
    # The catalog loader pins /opt/fa-tools in images; this offline test builds the grant directly.
    grant = CapabilityGrant(entry_id="fake", entry_sha256="0" * 64, kind="mcp-hosted", tools=("fake-echo",),
                            vision=False, launch=StdioEntry(type="stdio", command=sys.executable, args=(str(SERVER),),
                                                            env=tuple(sorted(env.items()))))
    session = session_needs(CapabilityCapture(head_selection_id=None, grants=(grant,)))
    assert session.permitted_mcp == {("fake", "fake-echo")}
    checked = []

    async def current():
        checked.append(True)
        return work_current
    endpoint = ScriptedEndpoint([
        tool_call("fake-echo", {"text": "hi"}, "call-echo"),
        tool_call("fake-hidden", {}, "call-hidden"),
        {"content": "CAPABILITY_DONE"},
    ])
    events = []
    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work",
        sdk_session_id="offline-capability-mcp", resume=False, prompt="Use the organization echo tool.",
        tools=[], api_url="https://context.invalid", token_provider=endpoint.token,
        on_event=events.append, timeout_seconds=90, runtime_path=runtime_path,
        http_transport=endpoint.transport, capability_session=session, capability_current=current,
    ))
    approvals = [e["data"] for e in events if e["type"] == "m2.permission" and e["data"].get("kind") == "mcp"]
    print("MCP_PERMISSION_EVENTS", approvals)
    assert result["final_message"] == "CAPABILITY_DONE"
    declared = [tool["function"]["name"] for tool in endpoint.requests[0]["tools"]]
    assert declared == ["fake-echo"]
    fake = [item for item in approvals if item["server_name"] == "fake"]
    assert [item["approved"] for item in fake] == [work_current] and len(checked) == 1
    assert [item["checks"]["work_current"] for item in fake] == [work_current]
    tool_messages = [m for m in endpoint.requests[1]["messages"] if m["role"] == "tool"]
    text = "".join(m["content"] if isinstance(m["content"], str) else "".join(p.get("text", "") for p in m["content"])
                   for m in tool_messages)
    assert ("echo:hi" in text) is work_current
    later = "".join(str(m.get("content")) for m in endpoint.requests[2]["messages"] if m["role"] == "tool")
    assert "hidden-called" not in later
