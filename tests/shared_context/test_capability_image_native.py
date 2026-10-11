"""Pinned SDK 1.0.13 / runtime 1.0.83: a screenshot PNG from a Hosted MCP tool or an FA tool reaches on_tool_image.

The SDK tool-completion event is the only place Hosted sees an MCP tool's image bytes. Offline stdio stand-in
and scripted provider; no browser, network or model.
"""

import asyncio
import base64
import os
import sys
from pathlib import Path

from shared_context.behavior_capabilities import CapabilityCapture, CapabilityGrant
from shared_context.behavior_capability_prompt import session_needs
from shared_context.m2_copilot import ToolOutput, ToolSpec, run_copilot
from shared_context.tool_catalog import StdioEntry
from test_assistant_images import png
from test_m2_copilot import ScriptedEndpoint, runtime_path, tool_call, workspace

SERVER = Path(__file__).with_name("fake_playwright_stdio_server.py")


def test_mcp_and_fa_tool_screenshots_reach_the_image_hook_in_order(workspace, runtime_path):
    env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP") if name in os.environ}
    grant = CapabilityGrant(entry_id="playwright", entry_sha256="0" * 64, kind="mcp-hosted",
                            tools=("playwright-browser_take_screenshot",), vision=False,
                            launch=StdioEntry(type="stdio", command=sys.executable, args=(str(SERVER),),
                                              env=tuple(sorted(env.items()))))
    session = session_needs(CapabilityCapture(head_selection_id=None, grants=(grant,)))
    desktop = png(6)

    async def observe(_packet):
        return ToolOutput("observed", png_base64=base64.b64encode(desktop).decode())
    endpoint = ScriptedEndpoint([
        tool_call("playwright-browser_take_screenshot", {}, "call-shot"),
        tool_call("fake_observe", {}, "call-observe"),
        {"content": "DONE"},
    ])
    seen = []

    async def current():
        return True
    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work", sdk_session_id="tool-images",
        resume=False, prompt="Take both screenshots.",
        tools=[ToolSpec("fake_observe", "Observe.", {"type": "object", "properties": {}}, observe)],
        api_url="https://context.invalid", token_provider=endpoint.token, on_event=lambda _: None,
        timeout_seconds=90, runtime_path=runtime_path, http_transport=endpoint.transport,
        capability_session=session, capability_current=current, on_tool_image=seen.append,
    ))
    assert result["final_message"] == "DONE"
    decoded = [base64.b64decode(value) for value in seen]
    assert len(decoded) == 2 and decoded[0].startswith(b"\x89PNG\r\n\x1a\n") and decoded[1] == desktop
