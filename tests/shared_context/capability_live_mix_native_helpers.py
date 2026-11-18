"""從 test_capability_live_mix_native.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import os
import sys
from pathlib import Path
from shared_context.behavior_capabilities import enabled_toolboxes
from shared_context.tool_catalog import StdioEntry, build_catalog, load_catalog
from test_capability_calls import WEBIQ


SOURCE = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc" / "tool_catalog"


FAKE_PLAYWRIGHT = Path(__file__).with_name("fake_playwright_stdio_server.py")


def live_like_catalog():
    """The committed catalog as deployed (N5 toolbox entries off); only the Playwright command is swapped.

    The committed catalog has no mcp-api entry since WebIQ-MCP was retired (2026-10-22), so the fictional
    WebSearch-MCP fixture stands in for the API-brokered kind."""
    committed = enabled_toolboxes(load_catalog(SOURCE), ())
    committed = build_catalog([*committed.entries, WEBIQ])
    env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH") if name in os.environ}
    entries = []
    for entry in committed.entries:
        if entry.entry_id == "playwright":
            entry = entry.model_copy(update={"config": StdioEntry(
                type="stdio", command=sys.executable, args=(str(FAKE_PLAYWRIGHT),),
                env=tuple(sorted({**dict(entry.config.env), **env}.items())))})
        entries.append(entry)
    return build_catalog(entries)
