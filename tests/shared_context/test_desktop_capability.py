"""fa-desktop-v1 Hosted wiring: the registered factory refuses unusable environments (E-10), its tools
reach the pinned runtime and API gateway with screenshots, and a turn without the grant closes the desktop."""

import asyncio
from types import SimpleNamespace

import pytest

from shared_context import behavior_assistant_hosted as hosted
from shared_context import behavior_desktop_capability as capability
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_capabilities import BUILTIN_CAPABILITIES
from shared_context.behavior_desktop import DESKTOP_LOCK, OwnedDesktop
from shared_context.behavior_desktop_tools import DesktopFileRead, DesktopObservation, DesktopToolbox
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure
from assistant_api_helpers import assistant_api
from test_behavior_desktop import FakeRunner
from capability_capture_helpers import GRANTS
from instruction_flow_helpers import instruction_models
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key

GRANT = GRANTS["fa-desktop-v1"]
DESKTOP_TOOLS = BUILTIN_CAPABILITIES["fa-desktop-v1"].tools


@pytest.fixture
def fake_desktop(monkeypatch):
    """A scripted X display behind the real OwnedDesktop/DesktopToolbox; no Linux display is needed."""
    runner = FakeRunner()
    monkeypatch.setattr(capability, "prerequisites", lambda: 10001)
    monkeypatch.setattr(capability, "is_dumpable", lambda: False)
    monkeypatch.setattr(capability, "DesktopToolbox", lambda root: DesktopToolbox(
        root, desktop_factory=lambda path: OwnedDesktop(path, runner=runner), hardened=lambda: True))
    yield runner
    for root in list(capability.DESKTOP_CAPABILITY._toolboxes):
        capability.DESKTOP_CAPABILITY._toolboxes.pop(root).close()


def session(tmp_path):
    launch = tmp_path / "assistant" / "launch-1"
    launch.mkdir(parents=True, mode=0o700, exist_ok=True)
    return SimpleNamespace(root=launch)


def test_the_desktop_handler_is_registered_for_its_captured_tools():
    assert hosted.CAPABILITY_HANDLERS["fa-desktop-v1"] is capability.DESKTOP_CAPABILITY
    assert DESKTOP_TOOLS == ("desktop_open_file", "desktop_observe", "desktop_act", "desktop_read_file")


@pytest.mark.parametrize("broken", ["prerequisites", "dumpable"])
def test_unusable_environment_refuses_the_turn_before_any_tool(tmp_path, monkeypatch, broken):
    def missing():
        raise Problem(503, "DESKTOP_UNAVAILABLE", "desktop commands missing: Xvfb")
    monkeypatch.setattr(capability, "prerequisites", missing if broken == "prerequisites" else lambda: 10001)
    monkeypatch.setattr(capability, "is_dumpable", lambda: broken == "dumpable")
    with pytest.raises(Problem) as error:
        capability.DESKTOP_CAPABILITY(session(tmp_path), GRANT)
    assert error.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert error.value.body["error"]["capability"] == "fa-desktop-v1"
    assert capability.DESKTOP_CAPABILITY._toolboxes == {} and not DESKTOP_LOCK.locked()


def test_tools_return_screenshots_and_refusals_carry_desktop_guidance(tmp_path, fake_desktop):
    specs, models = capability.DESKTOP_CAPABILITY(session(tmp_path), GRANT)
    assert tuple(spec.name for spec in specs) == DESKTOP_TOOLS and set(models) == set(DESKTOP_TOOLS)
    handlers = {spec.name: spec.handler for spec in specs}
    boundary = AssistantModelBoundary(None, text_reply=True, capability_results=models)

    def call(name, arguments):
        return asyncio.run(handlers[name]({"session_id": "s", "tool_call_id": "c", "tool_name": name,
                                           "arguments": arguments}))

    opened = call("desktop_open_file", {"name": "notes.txt", "create": True})
    observation = DesktopObservation.model_validate_json(boundary.tool_result("desktop_open_file", opened.text))
    assert opened.png_base64 and (tmp_path / "assistant" / "desktop" / "files" / "notes.txt").exists()
    call("desktop_act", {"observation_id": observation.observation_id, "action": {"type": "wait"}})
    with pytest.raises(ToolFailure) as refused:
        call("desktop_act", {"observation_id": observation.observation_id, "action": {"type": "wait"}})
    assert refused.value.public_code == "DESKTOP_OBSERVATION_STALE"
    guidance = boundary.public_tool_failure("desktop_act", refused.value.public_code)
    assert "[DESKTOP_OBSERVATION_STALE]" in guidance and "desktop_observe" in guidance
    read = call("desktop_read_file", {"name": "notes.txt"})
    assert read.png_base64 is None and DesktopFileRead.model_validate_json(read.text).bytes == 0


def test_release_closes_the_desktop_and_is_idempotent(tmp_path, fake_desktop):
    specs, _ = capability.DESKTOP_CAPABILITY(session(tmp_path), GRANT)
    observe = next(spec.handler for spec in specs if spec.name == "desktop_observe")
    asyncio.run(observe({"arguments": {}}))
    assert DESKTOP_LOCK.locked()
    assert capability.DESKTOP_CAPABILITY.release(session(tmp_path))["closed"] is True
    assert not DESKTOP_LOCK.locked() and capability.DESKTOP_CAPABILITY.release(session(tmp_path)) is None


def test_a_release_during_another_desktop_turn_never_leaves_an_untracked_desktop(tmp_path, fake_desktop):
    specs, _ = capability.DESKTOP_CAPABILITY(session(tmp_path), GRANT)
    observe = next(spec.handler for spec in specs if spec.name == "desktop_observe")
    asyncio.run(observe({"arguments": {}}))
    assert capability.DESKTOP_CAPABILITY.release(session(tmp_path))["closed"] is True
    with pytest.raises(ToolFailure) as refused:
        asyncio.run(observe({"arguments": {}}))
    assert refused.value.public_code == "DESKTOP_CLOSED" and not DESKTOP_LOCK.locked()
    later, _ = capability.DESKTOP_CAPABILITY(session(tmp_path), GRANT)
    fresh = next(spec.handler for spec in later if spec.name == "desktop_observe")
    assert DesktopObservation.model_validate_json(asyncio.run(fresh({"arguments": {}})).text).halted is None
    assert capability.DESKTOP_CAPABILITY.release(session(tmp_path))["closed"] is True and not DESKTOP_LOCK.locked()

def _observed(handler, arguments=None):
    return DesktopObservation.model_validate_json(asyncio.run(handler({"arguments": arguments or {}})).text)


def test_the_desktop_stays_across_granted_turns_with_fresh_turn_budgets(tmp_path, fake_desktop):
    owner = session(tmp_path)
    specs, _ = capability.DESKTOP_CAPABILITY(owner, GRANT)
    # Started at the beginning of the granted turn, before any desktop tool, so the viewer and Playwright see it.
    assert capability.DESKTOP_CAPABILITY.display(owner) == {"DISPLAY": ":7"}
    assert capability.DESKTOP_CAPABILITY.current_env()["DISPLAY"] == ":7"
    handlers = {spec.name: spec.handler for spec in specs}
    opened = _observed(handlers["desktop_open_file"], {"name": "a.txt", "create": True})
    warning = fake_desktop.add_window("Security Warning", 0, "DIALOG")
    assert _observed(handlers["desktop_observe"]).halted.reason == "security_warning"
    del fake_desktop.windows[warning]

    later, _ = capability.DESKTOP_CAPABILITY(owner, GRANT)  # the next granted turn (e.g. only Playwright)
    assert capability.DESKTOP_CAPABILITY.display(owner) == {"DISPLAY": ":7"}
    assert capability.DESKTOP_CAPABILITY.current_env() is not None
    handlers = {spec.name: spec.handler for spec in later}
    with pytest.raises(ToolFailure) as stale:
        asyncio.run(handlers["desktop_act"]({"arguments": {"observation_id": opened.observation_id,
                                                           "action": {"type": "wait"}}}))
    assert stale.value.public_code == "DESKTOP_OBSERVATION_STALE"
    seen = _observed(handlers["desktop_observe"])
    assert seen.halted is None and [window.title for window in seen.windows] == ["a.txt - L3afpad"]
    assert sum(1 for args, _ in fake_desktop.spawned if args[0] == "Xvfb") == 1 and DESKTOP_LOCK.locked()
    assert (tmp_path / "assistant" / "desktop" / "files" / "a.txt").exists()


def test_a_desktop_whose_display_exited_is_started_again_on_the_next_granted_turn(tmp_path, fake_desktop):
    owner = session(tmp_path)
    capability.DESKTOP_CAPABILITY(owner, GRANT)
    assert capability.DESKTOP_CAPABILITY.display(owner) == {"DISPLAY": ":7"}
    toolbox = capability.DESKTOP_CAPABILITY._toolboxes[capability.desktop_root(owner)]
    toolbox._desktop._core[0].alive = False
    capability.DESKTOP_CAPABILITY(owner, GRANT)
    assert toolbox._desktop is None and not DESKTOP_LOCK.locked()
    assert capability.DESKTOP_CAPABILITY.display(owner) == {"DISPLAY": ":7"} and DESKTOP_LOCK.locked()
    assert sum(1 for args, _ in fake_desktop.spawned if args[0] == "Xvfb") == 2


def test_no_display_without_a_granted_turn_or_when_the_desktop_cannot_start(tmp_path, monkeypatch, fake_desktop):
    owner = session(tmp_path)
    assert capability.DESKTOP_CAPABILITY.display(owner) is None and not DESKTOP_LOCK.locked()
    monkeypatch.setattr(capability, "DesktopToolbox", lambda root: DesktopToolbox(root, hardened=lambda: False))
    specs, _ = capability.DESKTOP_CAPABILITY(owner, GRANT)
    assert capability.DESKTOP_CAPABILITY.display(owner) is None and not DESKTOP_LOCK.locked()
    with pytest.raises(ToolFailure) as refused:
        asyncio.run(next(spec.handler for spec in specs if spec.name == "desktop_observe")({"arguments": {}}))
    assert refused.value.public_code == "DESKTOP_UNAVAILABLE"


def test_a_granted_turn_with_the_image_browser_attaches_to_the_desktops_own_browser(tmp_path, fake_desktop):
    owner = session(tmp_path)
    capability.DESKTOP_CAPABILITY(owner, GRANT)
    chrome = "/opt/fa-tools/ms-playwright/chromium-1246/chrome-linux64/chrome"
    assert capability.DESKTOP_CAPABILITY.display(owner, chrome) == {
        "DISPLAY": ":7", "CDP_ENDPOINT": "http://127.0.0.1:9222"}
    capability.DESKTOP_CAPABILITY(owner, GRANT)  # the next granted turn
    assert capability.DESKTOP_CAPABILITY.display(owner, chrome)["CDP_ENDPOINT"] == "http://127.0.0.1:9222"
    assert sum(1 for args, _ in fake_desktop.spawned if args[0] == chrome) == 1
    fake_desktop.browser_port = None
    desktop = capability.DESKTOP_CAPABILITY._toolboxes[capability.desktop_root(owner)]._desktop
    next(p for p in desktop._processes if desktop._owners.get(p.pid) == "adopted").alive = False
    # A browser that cannot start again leaves only the display: the MCP then launches its own on it.
    assert capability.DESKTOP_CAPABILITY.display(owner, chrome) == {"DISPLAY": ":7"}
    assert capability.DESKTOP_CAPABILITY.display(owner) == {"DISPLAY": ":7"}


def test_the_image_browser_is_found_only_from_the_playwright_grant_layout(tmp_path):
    from shared_context.behavior_capabilities import CapabilityGrant
    from shared_context.tool_catalog import StdioEntry
    chrome = tmp_path / "ms-playwright" / "chromium-1246" / "chrome-linux64" / "chrome"
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"")
    chrome.chmod(0o755)

    def grant(entry_id="playwright", browsers=str(tmp_path / "ms-playwright")):
        return CapabilityGrant(entry_id=entry_id, entry_sha256="0" * 64, kind="mcp-hosted", tools=(f"{entry_id}-x",),
                               vision=False, launch=StdioEntry(type="stdio", command="/opt/fa-tools/bin/playwright-mcp",
                                                               args=(), env=(("PLAYWRIGHT_BROWSERS_PATH", browsers),)))
    assert capability.browser_executable((GRANT, grant())) == str(chrome)
    assert capability.browser_executable((grant(entry_id="other"),)) is None
    assert capability.browser_executable((grant(browsers=str(tmp_path / "missing")),)) is None
    (tmp_path / "ms-playwright" / "chromium-1300" / "chrome-linux64").mkdir(parents=True)
    (tmp_path / "ms-playwright" / "chromium-1300" / "chrome-linux64" / "chrome").write_bytes(b"")
    assert capability.browser_executable((grant(),)) is None
