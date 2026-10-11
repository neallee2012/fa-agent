"""Pinned SDK 1.0.13 / runtime 1.0.83: a Hosted MCP server gets a writable, FA-private HOME and a short TMPDIR.

The runtime passes MCP children no HOME, so tools fell back to the passwd home on the read-only Hosted rootfs
(Playwright: mkdir '/home/app/.cache' ENOENT). Its TMPDIR under the long FA state dir made Chromium's
SingletonSocket path too long. The short TMPDIR lives under the private state root, because the native HOME is
not writable after the Hosted privilege drop (live: PermissionError). Offline stdio stand-in and scripted
provider; no network or model.
"""

import asyncio
import json
import os
import sys
from pathlib import Path, PurePosixPath

import pytest

from shared_context.behavior_assistant_hosted import hosted_mcp_temp
from shared_context.behavior_capabilities import CapabilityCapture, CapabilityGrant
from shared_context.behavior_capability_prompt import session_needs
from shared_context.errors import Problem
from shared_context.m2_copilot import mcp_temp_dir, run_copilot, show_on_desktop
from shared_context.tool_catalog import StdioEntry
from test_m2_copilot import ScriptedEndpoint, runtime_path, tool_call, workspace

SERVER = Path(__file__).with_name("fake_mcp_env_server.py")
CHROMIUM_SOCKET = "org.chromium.Chromium.wbTbjq/SingletonSocket"


def test_hosted_mcp_temp_dir_fits_the_chromium_socket_path_limit():
    # Live v48 C1: Chromium FATAL "Socket path too long" for its SingletonSocket under the FA state dir.
    private = PurePosixPath("/home/session/.fa-assistant-v12")
    state = private / ("5f4bc6ee" * 8) / "sdk"
    assert len(str(state / "runtime-work" / CHROMIUM_SOCKET)) > 107
    temp = mcp_temp_dir(state, private)
    assert temp.parent == private / "tmp" and len(str(temp / CHROMIUM_SOCKET)) <= 107
    assert mcp_temp_dir(state.parent.parent / ("0" * 64) / "sdk", private) != temp


def test_hosted_mcp_temp_is_made_under_the_private_root_or_refused_before_the_sdk(tmp_path):
    root = tmp_path / "private" / ("5f4bc6ee" * 8)
    temp = hosted_mcp_temp(root, "playwright")
    assert temp == mcp_temp_dir(root / "sdk", root.parent) and temp.is_dir() and os.access(temp, os.W_OK)
    (tmp_path / "blocked").write_text("not a directory")
    with pytest.raises(Problem) as refused:
        hosted_mcp_temp(tmp_path / "blocked" / "launch", "playwright")
    assert refused.value.status == 503 and refused.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE"


@pytest.mark.parametrize("catalog_home", [None, "catalog-home"], ids=["fa-home", "catalog-home-kept"])
def test_hosted_mcp_server_gets_a_writable_private_home(workspace, runtime_path, catalog_home):
    env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH") if name in os.environ}
    if catalog_home is not None:
        env["HOME"] = str(workspace / catalog_home)
    grant = CapabilityGrant(entry_id="fake", entry_sha256="0" * 64, kind="mcp-hosted", tools=("fake-report",),
                            vision=False, launch=StdioEntry(type="stdio", command=sys.executable, args=(str(SERVER),),
                                                            env=tuple(sorted(env.items()))))
    session = session_needs(CapabilityCapture(head_selection_id=None, grants=(grant,)))
    endpoint = ScriptedEndpoint([tool_call("fake-report", {}, "call-report"), {"content": "DONE"}])

    async def current():
        return True
    state = workspace / "state"
    temp = hosted_mcp_temp(workspace / "launch", "fake")
    result = asyncio.run(run_copilot(
        state_dir=state, working_directory=workspace / "work", sdk_session_id="mcp-home", resume=False,
        prompt="Report the environment.", tools=[], api_url="https://context.invalid", token_provider=endpoint.token,
        on_event=lambda _: None, timeout_seconds=90, runtime_path=runtime_path,
        http_transport=endpoint.transport, capability_session=session, capability_current=current,
        mcp_temp_directory=temp,
    ))
    assert result["final_message"] == "DONE"
    tool = [m for m in endpoint.requests[1]["messages"] if m["role"] == "tool"][-1]
    text = tool["content"] if isinstance(tool["content"], str) else "".join(p.get("text", "") for p in tool["content"])
    reported, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
    home = state.resolve() / "mcp-home" / "fake"
    expected_home = str(workspace / catalog_home) if catalog_home is not None else str(home)
    assert reported["HOME"] == expected_home
    assert reported["XDG_CACHE_HOME"] == str(home / ".cache")
    assert home.is_dir() and os.access(home, os.W_OK)
    assert reported["TMPDIR"] == str(temp) and reported["tempdir"] == str(temp)


def _reported(endpoint):
    tool = [m for m in endpoint.requests[1]["messages"] if m["role"] == "tool"][-1]
    text = tool["content"] if isinstance(tool["content"], str) else "".join(p.get("text", "") for p in tool["content"])
    return json.JSONDecoder().raw_decode(text[text.index("{"):])[0]


ATTACHED = {"DISPLAY": ":7", "CDP_ENDPOINT": "http://127.0.0.1:9222"}


@pytest.mark.parametrize("display", [None, {"DISPLAY": ":7"}, ATTACHED],
                         ids=["no-desktop-headless", "on-the-turns-desktop", "attached-to-the-desktop-browser"])
def test_playwright_runs_headed_only_on_the_turns_own_desktop(workspace, runtime_path, display, caplog):
    # Same pinned SDK/runtime path as the real Playwright MCP; the stand-in reports the argv and DISPLAY it got.
    env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH") if name in os.environ}
    grant = CapabilityGrant(entry_id="playwright", entry_sha256="0" * 64, kind="mcp-hosted",
                            tools=("playwright-report",), vision=False,
                            launch=StdioEntry(type="stdio", command=sys.executable,
                                              args=(str(SERVER), "--headless", "--isolated"),
                                              env=tuple(sorted(env.items()))))
    session = session_needs(CapabilityCapture(head_selection_id=None, grants=(grant,)))
    endpoint = ScriptedEndpoint([tool_call("playwright-report", {}, "call-report"), {"content": "DONE"}])

    async def current():
        return True
    caplog.set_level("WARNING", logger="shared_context.m2_copilot")
    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work", sdk_session_id="mcp-display",
        resume=False, prompt="Report the environment.", tools=[], api_url="https://context.invalid",
        token_provider=endpoint.token, on_event=lambda _: None, timeout_seconds=90, runtime_path=runtime_path,
        http_transport=endpoint.transport, capability_session=session, capability_current=current,
        mcp_display_env=display,
    ))
    assert result["final_message"] == "DONE"
    reported = _reported(endpoint)
    if display is None:
        assert reported["argv"] == ["--headless", "--isolated"] and reported["DISPLAY"] is None
    elif display == ATTACHED:
        # No --isolated: the MCP uses the desktop browser's default context, so tabs and logins stay.
        assert reported["argv"] == ["--cdp-endpoint", "http://127.0.0.1:9222"] and reported["DISPLAY"] == ":7"
    else:
        assert reported["argv"] == ["--isolated"] and reported["DISPLAY"] == ":7"
    # One readable Hosted log line states what the MCP child got, so a live turn can tell headed from headless.
    logged = [record.getMessage() for record in caplog.records if record.getMessage().startswith("FA_MCP_DESKTOP ")]
    assert logged == ["FA_MCP_DESKTOP " + json.dumps(
        {"display": None if display is None else ":7", "headed": display is not None, "server": "playwright",
         "browser": "attached" if display == ATTACHED else "launched"}, sort_keys=True)]
    # The captured server configuration itself is never changed.
    assert list(session.mcp_servers["playwright"]["args"]) == [str(SERVER), "--headless", "--isolated"]


def test_only_the_playwright_server_and_a_local_x_display_are_accepted():
    servers = {"playwright": {"args": ["--headless", "--isolated"], "env": {"HOME": "/h"}},
               "other": {"args": ["--headless"], "env": {}}}
    show_on_desktop(servers, {"DISPLAY": ":12"})
    assert servers == {"playwright": {"args": ["--isolated"], "env": {"HOME": "/h", "DISPLAY": ":12"}},
                       "other": {"args": ["--headless"], "env": {}}}
    for bad in ({"DISPLAY": "remote:0"}, {"DISPLAY": ":0.0"}, {"DISPLAY": ":1", "XAUTHORITY": "/x"}, {},
                {"DISPLAY": ":1", "CDP_ENDPOINT": "http://10.0.0.5:9222"},
                {"DISPLAY": ":1", "CDP_ENDPOINT": "ws://127.0.0.1:9222/devtools/browser/x"},
                {"DISPLAY": ":1", "CDP_ENDPOINT": "http://127.0.0.1:9222/json"}):
        with pytest.raises(ValueError):
            show_on_desktop({"playwright": {"args": [], "env": {}}}, bad)


def test_a_catalog_that_already_chooses_its_browser_keeps_it():
    attached = {"DISPLAY": ":3", "CDP_ENDPOINT": "http://127.0.0.1:4000"}
    servers = {"playwright": {"args": ["--headless", "--isolated", "--browser", "chromium"], "env": {}}}
    show_on_desktop(servers, attached)
    assert servers["playwright"]["args"] == ["--browser", "chromium", "--cdp-endpoint", "http://127.0.0.1:4000"]
    for own in (["--user-data-dir", "/p"], ["--user-data-dir=/p"], ["--extension"], ["--cdp-endpoint=http://x"]):
        servers = {"playwright": {"args": ["--headless", "--isolated", *own], "env": {}}}
        show_on_desktop(servers, attached)
        assert servers["playwright"]["args"] == ["--isolated", *own] and servers["playwright"]["env"]["DISPLAY"] == ":3"
