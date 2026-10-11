"""CAP-03／04 deploy content: the committed organization catalog files load and keep their approved scope."""

import json
from pathlib import Path

from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_capability_prompt import session_needs
from shared_context.behavior_capabilities import CapabilityCapture
from shared_context.tool_catalog import HttpEntry, StdioEntry, ToolboxEntry, load_catalog

SOURCE = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
PLAYWRIGHT_TOOLS = ("browser_navigate", "browser_snapshot", "browser_take_screenshot", "browser_click", "browser_type",
                    "browser_fill_form", "browser_select_option", "browser_wait_for", "browser_navigate_back")


def test_committed_catalog_loads_with_the_approved_tools_only():
    catalog = load_catalog(SOURCE / "tool_catalog")
    assert sorted(entry.entry_id for entry in catalog.entries) == ["WebIQ", "WebIQ-MCP", "playwright"]
    toolbox = catalog.entry("WebIQ")
    assert toolbox.execution == "foundry-toolbox" and isinstance(toolbox.config, ToolboxEntry)
    assert (toolbox.config.toolbox, toolbox.config.server_label, toolbox.tools) == (
        "fa-org-tools", "WebIQ", ("web", "browse"))
    catalog = catalog.model_copy(update={"entries": tuple(item for item in catalog.entries if item.entry_id != "WebIQ")})
    playwright, webiq = catalog.entry("playwright"), catalog.entry("WebIQ-MCP")
    assert playwright.execution == "hosted-stdio" and isinstance(playwright.config, StdioEntry)
    assert playwright.tools == PLAYWRIGHT_TOOLS and "browser_file_upload" not in playwright.tools
    assert playwright.config.command == "/opt/fa-tools/bin/playwright-mcp"
    assert "--allow-unrestricted-file-access" not in playwright.config.args
    assert dict(playwright.config.env) == {"PLAYWRIGHT_BROWSERS_PATH": "/opt/fa-tools/ms-playwright"}
    assert webiq.execution == "api-client" and isinstance(webiq.config, HttpEntry)
    assert webiq.tools == ("web", "browse") and dict(webiq.config.headers) == {"x-apikey": "${secret:webiq-api-key}"}
    grants = available_grants(catalog)
    needs = session_needs(CapabilityCapture(head_selection_id=None, grants=tuple(grants[name] for name in sorted(grants))))
    assert needs.mcp_tools == tuple("playwright-" + name for name in PLAYWRIGHT_TOOLS)
    assert needs.custom_tools == ("WebIQ-MCP-web", "WebIQ-MCP-browse") and not needs.vision


def test_image_pins_the_playwright_mcp_package_and_its_launcher_path():
    tools = SOURCE / "tools" / "playwright-mcp"
    package = json.loads((tools / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((tools / "package-lock.json").read_text(encoding="utf-8"))
    assert package["dependencies"] == {"@playwright/mcp": "0.0.82"}
    installed = {name: item for name, item in lock["packages"].items() if name}
    assert installed["node_modules/@playwright/mcp"]["version"] == "0.0.82"
    assert all(item["integrity"].startswith("sha512-") and item["resolved"].startswith("https://registry.npmjs.org/")
               for item in installed.values())
    dockerfile = (SOURCE / "Dockerfile.behavior-assistant-desktop").read_text(encoding="utf-8")
    assert "npm ci --omit=dev --ignore-scripts" in dockerfile and "install --no-shell chromium" in dockerfile
    assert "/opt/fa-tools/bin/playwright-mcp" in dockerfile and "npx" not in dockerfile


def test_desktop_image_has_git_checked_at_build_and_no_git_credentials():
    """SP-16: run_command can use git on the Agent's own computer; nothing configures a credential or remote."""
    dockerfile = (SOURCE / "Dockerfile.behavior-assistant-desktop").read_text(encoding="utf-8")
    install = dockerfile[dockerfile.index("apt-get install"):dockerfile.index("rm -rf /var/lib/apt/lists/*")]
    assert " git " in install.replace("\\\n", " ").replace("\n", " ") + " "
    assert "git --version" in dockerfile
    lowered = dockerfile.lower()
    assert "credential" not in lowered and "git config" not in lowered and "remote add" not in lowered
