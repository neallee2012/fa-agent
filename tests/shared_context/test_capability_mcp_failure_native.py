"""Pinned SDK 1.0.13 / runtime 1.0.83: a failed Hosted MCP tool call leaves one bounded, redacted log line.

The SDK error text otherwise exists only in the archived tool result. Offline stdio stand-in with the Playwright
tool name and a scripted provider; no browser, network or model.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from shared_context.behavior_capabilities import CapabilityCapture, CapabilityGrant
from shared_context.behavior_capability_prompt import session_needs
from shared_context.m2_copilot import run_copilot
from shared_context.tool_catalog import StdioEntry
from test_m2_copilot import ScriptedEndpoint, runtime_path, tool_call, workspace

SERVER = Path(__file__).with_name("fake_playwright_stdio_server.py")


def test_failed_hosted_mcp_call_logs_server_tool_code_and_redacted_excerpt(workspace, runtime_path, caplog):
    env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP") if name in os.environ}
    grant = CapabilityGrant(entry_id="playwright", entry_sha256="0" * 64, kind="mcp-hosted",
                            tools=("playwright-browser_navigate",), vision=False,
                            launch=StdioEntry(type="stdio", command=sys.executable, args=(str(SERVER),),
                                              env=tuple(sorted(env.items()))))
    session = session_needs(CapabilityCapture(head_selection_id=None, grants=(grant,)))
    endpoint = ScriptedEndpoint([
        tool_call("playwright-browser_navigate",
                  {"url": "https://unreachable.invalid/synthetic-offline-1/login?token=SYNTHETIC-QUERY-SECRET"}, "call-fail"),
        tool_call("playwright-browser_navigate", {"url": "http://127.0.0.1:9/launch-crash"}, "call-crash"),
        tool_call("playwright-browser_navigate", {"url": "http://127.0.0.1:9/"}, "call-ok"),
        {"content": "DONE"},
    ])

    async def current():
        return True
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(run_copilot(
            state_dir=workspace / "state", working_directory=workspace / "work", sdk_session_id="mcp-failure",
            resume=False, prompt="Open both pages.", tools=[], api_url="https://context.invalid",
            token_provider=endpoint.token, on_event=lambda _: None, timeout_seconds=90, runtime_path=runtime_path,
            http_transport=endpoint.transport, capability_session=session, capability_current=current,
        ))
    assert result["final_message"] == "DONE"
    tools = [m for m in endpoint.requests[-1]["messages"] if m["role"] == "tool"]
    assert "navigated:http://127.0.0.1:9/" in json.dumps(tools[-1])
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("FA_MCP_TOOL_FAILED ")]
    assert len(lines) == 2, lines
    logged = json.loads(lines[0].split(" ", 1)[1])
    assert {key: logged[key] for key in ("server", "tool", "code")} == {
        "server": "playwright", "tool": "browser_navigate", "code": "failure"}
    assert "ERR_NAME_NOT_RESOLVED at https://unreachable.invalid/[REDACTED]/login?[redacted]" in logged["error"]
    assert len(logged["error"]) == 200 and "SYNTHETIC-QUERY-SECRET" not in lines[0] and "browser_log" not in logged
    assert not any(token in lines[0] for token in (f"synthetic-offline-{n}" for n in range(1, 4)))
    crash = json.loads(lines[1].split(" ", 1)[1])
    assert "Target page, context or browser has been closed" in crash["error"] and "<launched> pid=4242" in crash["error"]
    assert crash["browser_log"].startswith("[pid=4242][err] ") and "SYNTHETIC-CHROME-FATAL" in crash["browser_log"]
    assert "signal=SIGTRAP" in crash["browser_log"] and "--synthetic-flag" not in lines[1]
