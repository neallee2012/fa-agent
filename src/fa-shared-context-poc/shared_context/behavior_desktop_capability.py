"""Hosted handler for the FA builtin capability ``fa-desktop-v1`` (CAP-05).

``behavior_assistant_hosted`` registers ``DESKTOP_CAPABILITY`` in ``CAPABILITY_HANDLERS``. For a turn whose
captured grants include the desktop, the factory checks this Hosted environment first (desktop image and a
non-dumpable main process); otherwise the turn is refused before the model sees any desktop tool (E-10).
For a turn without the grant, ``release`` closes a desktop left running by an earlier turn.

One desktop per Hosted process, rooted at the Agent's private assistant directory. Like Grokbot's per-Agent
desktop it stays across turns while the process lives, so the live viewer keeps it: ``display`` starts it at the
beginning of each granted turn and hands its DISPLAY to the turn's Playwright MCP. With the image's Chromium it
also starts the desktop's own browser (persistent profile) and hands its local CDP endpoint, so the MCP attaches
to the same browser every turn and its tabs and logins stay. Each granted turn renews the desktop's budgets, halt
and observation; windows and files stay. Outbound network
is not restricted here: the current Hosted account has no network injection, and a network-layer allowlist waits
for a VNet-injected Foundry redeployment.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

from .behavior_capabilities import CAPABILITY_TOOL_GUIDANCE
from .behavior_desktop import prerequisites
from .behavior_desktop_tools import DESKTOP_TOOLS, DESKTOP_GUIDANCE, DesktopToolbox, tool_schema
from .behavior_process_hardening import is_dumpable
from .errors import Problem
from .m2_copilot import HEADED_ON_DESKTOP, ToolFailure, ToolOutput, ToolSpec

ENTRY_ID = "fa-desktop-v1"

# The boundary renders capability refusals from this registry; desktop codes never come from the API.
CAPABILITY_TOOL_GUIDANCE.update(DESKTOP_GUIDANCE)


def desktop_root(session) -> Path:
    """The Agent-private assistant root shared by this Hosted process's turns, not one launch directory."""
    return Path(session.root).parent / "desktop"


def browser_executable(grants) -> str | None:
    """The image's pinned Chromium for this turn's Playwright MCP grant, or None (the MCP then launches its own).

    Found under the catalog's ``PLAYWRIGHT_BROWSERS_PATH`` as Playwright installs Chrome for Testing
    (``chromium-<revision>/chrome-linux64/chrome``); anything but exactly one executable is not used."""
    for grant in grants:
        if grant.kind != "mcp-hosted" or grant.entry_id not in HEADED_ON_DESKTOP or grant.launch is None:
            continue
        base = dict(grant.launch.env).get("PLAYWRIGHT_BROWSERS_PATH")
        found = [] if not base else sorted(Path(base).glob("chromium-*/chrome-linux64/chrome"))
        if len(found) == 1 and os.access(found[0], os.X_OK):
            return str(found[0])
    return None


class DesktopCapability:
    def __init__(self):
        self._toolboxes = {}
        self._lock = threading.Lock()

    def __call__(self, session, grant):
        try:
            prerequisites()
            if is_dumpable() is not False:
                raise Problem(503, "DESKTOP_UNAVAILABLE", "the Hosted main process is still dumpable")
        except Problem:
            raise Problem(503, "CAPABILITY_UNAVAILABLE", "this Hosted environment cannot run the desktop",
                          capability=grant.entry_id) from None
        root = desktop_root(session)
        # The desktop stays across granted turns; only its budgets, halt and observation are per turn.
        with self._lock:
            toolbox = self._toolboxes.get(root)
            if toolbox is None:
                toolbox = self._toolboxes[root] = DesktopToolbox(root)
        toolbox.renew()
        return (tuple(_spec(toolbox, tool) for tool in DESKTOP_TOOLS),
                {tool.name: tool.result for tool in DESKTOP_TOOLS})

    def display(self, session, browser: str | None = None) -> dict[str, str] | None:
        """Start a granted turn's desktop now (call it in a worker thread) and return its X display.

        With ``browser`` (the image's Chromium) the desktop's own browser is started too and its local
        ``CDP_ENDPOINT`` added; if it cannot start, only the display is returned and the MCP launches its own.
        None when this turn has no desktop or it cannot start; the tools then report why, as before."""
        with self._lock:
            toolbox = self._toolboxes.get(desktop_root(session))
        env = None if toolbox is None else toolbox.start()
        if env is None:
            return None
        found = {"DISPLAY": env["DISPLAY"]}
        if browser is not None:
            endpoint = toolbox.browser(browser)
            if endpoint is None:
                logging.getLogger(__name__).warning("FA_DESKTOP_BROWSER_UNAVAILABLE")
            else:
                found["CDP_ENDPOINT"] = endpoint
        return found

    def release(self, session) -> dict | None:
        """Close the desktop when a turn without the grant starts; nothing is replayed later."""
        with self._lock:
            toolbox = self._toolboxes.pop(desktop_root(session), None)
        return None if toolbox is None else toolbox.close()

    def current_env(self) -> dict | None:
        """The running desktop's display environment for a live viewer; never starts or replaces one."""
        with self._lock:
            toolboxes = tuple(self._toolboxes.values())
        for toolbox in toolboxes:
            env = toolbox.running_env()
            if env is not None:
                return env
        return None


def _spec(toolbox, tool):
    async def handle(packet):
        try:
            result = await asyncio.to_thread(toolbox.call, tool.name, packet["arguments"])
        except Problem as error:
            code = error.body["error"]["code"]
            raise ToolFailure("Desktop refused the action; do not claim completion or resend it.",
                              public_code=code) from None
        return ToolOutput(result.text, png_base64=result.png_base64)
    return ToolSpec(tool.name, tool.description, tool_schema(tool.name), handle)


DESKTOP_CAPABILITY = DesktopCapability()
