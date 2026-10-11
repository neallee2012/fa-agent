"""Native C1 consumer: Playwright (Hosted stdio MCP), shell and desktop through their existing handlers.

This is a `NativeCapabilityProvider` on the C0 seam, not another tool engine. C0 builds the SDK needs from the full
capture (`session_needs`: MCP servers, custom tools, vision, image tools), guards every tool with the original turn
and SDK session, and passes images and the MCP temp directory to `run_copilot`. This consumer supplies only what the
job lane used to take from its session:

* The existing `CAPABILITY_HANDLERS` factories (`run_command`, the four desktop tools), called with a native
  owner. They read only `owner.root`. The native root is `journal.root/native-turns`, which sits beside the job
  lane's launch roots, so `desktop/` and `shell/` are the same Agent-private directories in both lanes.
* A private TMPDIR for the image-pinned Hosted MCP child: one per RID, under the Agent's journal root like the
  job lane's (Chromium's SingletonSocket path must fit in 107 bytes). The launch command must exist in this image,
  or the turn is refused before the SDK starts.
* When this consumer is selected without the desktop grant, a desktop left running by an earlier turn is closed,
  as in the job lane. With the grant, the Agent's desktop is started before the SDK and its X display is handed
  to the image's Playwright MCP, which then runs headed on it (visible in the live viewer). With the image's
  Chromium the desktop also keeps its own browser, which the per-RID MCP attaches to over CDP, so the browser,
  its tabs and logins stay across RIDs like the desktop.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import Problem
from .native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider

DESKTOP = "fa-desktop-v1"
LOCAL_BUILTINS = frozenset({"fa-shell-v1", DESKTOP})


@dataclass(frozen=True)
class NativeOwner:
    """What the existing handlers read from a session: its root only, never a job, ticket or binding."""

    root: Path


def mcp_temp_path(root, turn_root):
    """This RID's short MCP TMPDIR beside the job lane's: `<journal root>/tmp/<hash of the RID's sdk dir>`."""
    from .m2_copilot import mcp_temp_dir
    return mcp_temp_dir(turn_root / "sdk", root.parent)


def accepts(grant) -> bool:
    return grant.kind == "mcp-hosted" or grant.kind == "fa-builtin" and grant.entry_id in LOCAL_BUILTINS


async def bind_local(context, grants, *, handlers=None) -> NativeCapabilityBinding:
    if handlers is None:
        from .behavior_assistant_hosted import CAPABILITY_HANDLERS as handlers
    owner = NativeOwner(Path(context.root))
    desktop = handlers.get(DESKTOP)
    if desktop is not None and hasattr(desktop, "release") and all(grant.entry_id != DESKTOP for grant in grants):
        await asyncio.to_thread(desktop.release, owner)
    tools, results, temp = [], {}, None
    for grant in grants:
        if grant.kind == "mcp-hosted":
            command = None if grant.launch is None else grant.launch.command
            if command is None or not os.path.isfile(command) or not os.access(command, os.X_OK):
                raise Problem(503, "CAPABILITY_UNAVAILABLE", "this Hosted image does not provide a captured tool",
                              capability=grant.entry_id)
            if temp is None:
                temp = mcp_temp_path(Path(context.root), Path(context.turn_root))
                try:
                    temp.mkdir(mode=0o700, parents=True, exist_ok=True)
                except OSError:
                    raise Problem(503, "CAPABILITY_UNAVAILABLE", "this Hosted environment has no private temp "
                                  "directory for a tool", capability=grant.entry_id) from None
            continue
        factory = handlers.get(grant.entry_id)
        if factory is None:
            raise Problem(503, "CAPABILITY_UNAVAILABLE", "this Hosted image has no handler for a captured capability",
                          capability=grant.entry_id)
        specs, models = factory(owner, grant)
        if tuple(spec.name for spec in specs) != grant.tools or set(models) != set(grant.tools):
            raise Problem(503, "CAPABILITY_UNAVAILABLE", "a capability handler differs from its captured tools",
                          capability=grant.entry_id)
        tools.extend(specs)
        results.update(models)
    display = None
    if desktop is not None and hasattr(desktop, "display") and any(grant.entry_id == DESKTOP for grant in grants):
        from .behavior_desktop_capability import browser_executable
        display = await asyncio.to_thread(desktop.display, owner, browser_executable(grants))
    return NativeCapabilityBinding(tools=tuple(tools), result_schemas=results, mcp_temp_directory=temp,
                                   mcp_display_env=display)


def local_capability_provider(handlers=None) -> NativeCapabilityProvider:
    async def bind(context, grants):
        return await bind_local(context, grants, handlers=handlers)
    return NativeCapabilityProvider(accepts=accepts, bind=bind, name="local")
