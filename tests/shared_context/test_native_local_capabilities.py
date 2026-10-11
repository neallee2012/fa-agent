"""Native C1 on the C0 seam: Playwright (Hosted stdio MCP), shell and desktop reuse their existing handlers."""

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shared_context import native_sdk
from shared_context.behavior_capabilities import (
    BUILTIN_CAPABILITIES, CapabilityCapture, CapabilityGrant, available_grants,
)
from shared_context.behavior_desktop_capability import desktop_root
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure, ToolOutput, ToolSpec
from shared_context.native_capabilities import bind_capabilities
from shared_context.native_local_capabilities import (
    NativeOwner, accepts, bind_local, local_capability_provider, mcp_temp_path,
)
from shared_context.native_turns import NativeTurnRuntime
from shared_context.tool_catalog import StdioEntry
from test_native_capabilities import selected_capture
from test_native_turns import Harness, IDENTITY, local_env  # noqa: F401
from test_native_turn_wire import submit

MODEL_ROOT = "https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1"


def browser_grant(command=sys.executable):
    return CapabilityGrant(entry_id="playwright", entry_sha256="0" * 64, kind="mcp-hosted",
                           tools=("playwright-browser_navigate", "playwright-browser_take_screenshot"), vision=True,
                           launch=StdioEntry(type="stdio", command=command, args=("-c", "pass"), env=()))


def with_grants(*entries, browser=None):
    """The C0 v3 capture fixture with real builtin grants and, optionally, a Hosted MCP grant."""
    data = selected_capture().model_dump(mode="json")
    grants = tuple(sorted([*available_grants(None, entries).values(), *(() if browser is None else (browser,))],
                          key=lambda grant: grant.entry_id))
    data["model"]["supports_image_input"] = True
    data["template"]["settings"]["capabilities"] = [grant.ref.model_dump(mode="json") for grant in grants]
    data["capabilities"] = CapabilityCapture(head_selection_id="OFFLINE-selection", grants=grants).model_dump(mode="json")
    return NativeTurnCapture.model_validate_json(json.dumps(data))


class Handlers:
    """The existing factory contract: factory(owner, grant) -> (specs, result models); desktop has release."""

    def __init__(self):
        self.owners, self.ran, self.released, self.displayed = [], [], [], []
        outer = self

        class Desktop:
            def __call__(self, owner, grant):
                return outer.made(owner, grant)

            def release(self, owner):
                outer.released.append(owner)

            def display(self, owner, browser=None):
                outer.displayed.append((owner, browser))
                return {"DISPLAY": ":7"}

        self.handlers = {"fa-shell-v1": self.made, "fa-desktop-v1": Desktop()}

    def made(self, owner, grant):
        self.owners.append(owner)

        def spec(name):
            async def handle(packet):
                self.ran.append((name, packet["arguments"]))
                return ToolOutput(json.dumps({"ran": name}))
            return ToolSpec(name, "OFFLINE " + name, {"type": "object"}, handle)
        return tuple(spec(name) for name in grant.tools), {name: object for name in grant.tools}


def context(tmp_path, capture, stopped=lambda: False):
    return SimpleNamespace(
        root=tmp_path / "native-turns", turn_root=tmp_path / "native-turns" / "rid-digest",
        receipt=SimpleNamespace(capture=capture, request_id="rid-local"), scope=SimpleNamespace(sdk_session_id="sdk-1"),
        control=SimpleNamespace(stop_requested=stopped), current=lambda: _current(stopped), hitl=None, assembly=None)


async def _current(stopped):
    return not stopped()


@pytest.mark.parametrize("grant,ok", [
    (available_grants(None, ("fa-shell-v1",))["fa-shell-v1"], True),
    (available_grants(None, ("fa-desktop-v1",))["fa-desktop-v1"], True),
    (browser_grant(), True),
    *((available_grants(None, (entry,))[entry], False) for entry in (
        "fa-skills-v1", "fa-delegate-v1", "fa-image-delivery-v1", "fa-skillify-v1", "fa-memory-candidate-v1")),
])
def test_the_registered_consumer_accepts_only_c1_capabilities(grant, ok):
    assert accepts(grant) is ok
    assert local_capability_provider().accepts(grant) is ok
    if ok:
        # Other consumers (C2..C5) may be registered too; a C1 grant must still have exactly one consumer.
        providers = native_sdk.native_capability_providers("https://api.offline.invalid", None)
        assert sum(provider.accepts(grant) for provider in providers) == 1


def test_existing_handlers_get_a_native_owner_and_pass_the_c0_tool_checks(tmp_path):
    fake = Handlers()
    capture = with_grants("fa-shell-v1", "fa-desktop-v1")
    provider = local_capability_provider(fake.handlers)
    ctx = context(tmp_path, capture)

    async def bind():
        async with AsyncExitStack() as stack:
            return await bind_capabilities(ctx, (provider,), stack)
    binding, = asyncio.run(bind())
    # No job, ticket or binding: only the native state root beside the job lane's launch roots.
    assert fake.owners == [NativeOwner(tmp_path / "native-turns")] * 2 and fake.released == []
    assert {spec.name for spec in binding.tools} == {"run_command", *BUILTIN_CAPABILITIES["fa-desktop-v1"].tools}
    assert binding.mcp_temp_directory is None
    # The granted turn's desktop is started before the SDK; its display goes only to the MCP configuration.
    assert binding.mcp_display_env == {"DISPLAY": ":7"}
    assert fake.displayed == [(NativeOwner(tmp_path / "native-turns"), None)]  # no Playwright grant: no browser
    run = next(spec for spec in binding.tools if spec.name == "run_command")
    packet = {"session_id": "sdk-1", "tool_call_id": "call-1", "tool_name": "run_command", "arguments": {"command": "pwd"}}
    assert json.loads(asyncio.run(run.handler(packet)).text) == {"ran": "run_command"}
    assert fake.ran == [("run_command", {"command": "pwd"})]


def test_a_stopped_turn_never_runs_the_tool(tmp_path):
    fake, stopped = Handlers(), [False]
    ctx = context(tmp_path, with_grants("fa-shell-v1"), stopped=lambda: stopped[0])

    async def bind():
        async with AsyncExitStack() as stack:
            return await bind_capabilities(ctx, (local_capability_provider(fake.handlers),), stack)
    binding, = asyncio.run(bind())
    stopped[0] = True
    with pytest.raises(ToolFailure):
        asyncio.run(binding.tools[0].handler({"session_id": "sdk-1", "tool_call_id": "call-1", "arguments": {}}))
    assert fake.ran == []


def test_a_selected_turn_without_the_desktop_closes_the_earlier_desktop(tmp_path):
    fake = Handlers()
    binding = asyncio.run(bind_local(context(tmp_path, None), with_grants("fa-shell-v1").capabilities.grants,
                                     handlers=fake.handlers))
    assert fake.released == [NativeOwner(tmp_path / "native-turns")]
    assert [spec.name for spec in binding.tools] == ["run_command"]
    assert binding.mcp_display_env is None and fake.displayed == []


def test_the_browser_uses_the_pinned_stdio_server_with_a_private_temp(tmp_path):
    fake = Handlers()
    grants = with_grants("fa-desktop-v1", browser=browser_grant()).capabilities.grants
    binding = asyncio.run(bind_local(context(tmp_path, None), grants, handlers=fake.handlers))
    # MCP tools come from the SDK server; this consumer registers only the desktop handlers.
    assert {spec.name for spec in binding.tools} == set(BUILTIN_CAPABILITIES["fa-desktop-v1"].tools)
    temp = binding.mcp_temp_directory
    assert binding.mcp_display_env == {"DISPLAY": ":7"}
    assert fake.displayed[-1][1] is None  # this grant names no PLAYWRIGHT_BROWSERS_PATH: the MCP launches its own
    # Beside the job lane's MCP temps under the Agent's journal root, one per RID.
    assert temp.is_dir() and temp.parent == tmp_path / "tmp"
    other = mcp_temp_path(tmp_path / "native-turns", tmp_path / "native-turns" / "other-rid")
    assert other.parent == temp.parent and other != temp
    missing = with_grants(browser=browser_grant(str(tmp_path / "missing-playwright-mcp"))).capabilities.grants
    with pytest.raises(Problem) as refused:
        asyncio.run(bind_local(context(tmp_path, None), missing, handlers=fake.handlers))
    assert refused.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE"


def test_the_hosted_browser_temp_leaves_room_for_chromiums_singleton_socket():
    from pathlib import PurePosixPath
    from shared_context.m2_copilot import mcp_temp_dir
    root = PurePosixPath("/home/session/.fa-assistant-v12/native-turns")
    temp = mcp_temp_path(root, root / ("f" * 64))
    socket = len(str(temp) + "/org.chromium.Chromium.XXXXXX/SingletonSocket")
    job = mcp_temp_dir(root.parent / "launch-1" / "sdk", root.parent)
    assert socket <= 107 and len(str(temp)) == len(str(job))


def test_a_handler_that_differs_from_its_grant_is_refused(tmp_path):
    handlers = {"fa-shell-v1": lambda owner, grant: ((), {})}
    with pytest.raises(Problem) as refused:
        asyncio.run(bind_local(context(tmp_path, None), with_grants("fa-shell-v1").capabilities.grants,
                               handlers=handlers))
    assert refused.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE"


def test_native_and_job_lane_share_the_agents_private_desktop_root(tmp_path):
    journal = tmp_path / "private"
    assert desktop_root(NativeOwner(journal / "native-turns")) == desktop_root(
        SimpleNamespace(root=journal / "launch-1")) == journal / "desktop"


def test_native_turn_runs_local_tools_with_capture_vision_and_mcp_through_the_existing_runner(
        local_env, monkeypatch, tmp_path):
    fake, seen = Handlers(), []
    selected = with_grants("fa-desktop-v1", "fa-shell-v1", browser=browser_grant())

    async def sdk(**options):
        seen.append(options)
        needs = options["capability_session"]
        assert set(needs.mcp_servers) == {"playwright"} and needs.vision
        assert needs.custom_tools == (*BUILTIN_CAPABILITIES["fa-desktop-v1"].tools, "run_command")
        assert options["mcp_temp_directory"].is_dir() and await options["capability_current"]() is True
        assert options["mcp_display_env"] == {"DISPLAY": ":7"}
        assert "fa-shell-v1" in options["system_message"] and "playwright" in options["system_message"]
        spec = next(tool for tool in options["tools"] if tool.name == "run_command")
        await spec.handler({"session_id": options["sdk_session_id"], "tool_call_id": "call-1",
                            "tool_name": "run_command", "arguments": {"command": "pwd"}})
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "done", "sdk_stop": {"pid": 1, "exit_code": 0, "graceful": True,
                                                      "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native-turns", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid", model_url=MODEL_ROOT, token_provider=lambda: "OFFLINE-token",
        capability_providers=(local_capability_provider(fake.handlers),),
    )
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        h = Harness(runtime, SimpleNamespace(controls={}), client)
        assert h.invoke(NativeSubmit(command=submit().command, capture=selected)).status_code == 202
        h.wait("rid-1", "completed")
    assert len(seen) == 1 and fake.ran == [("run_command", {"command": "pwd"})]
    assert fake.owners == [NativeOwner(tmp_path / "native-turns")] * 2


def test_the_registered_c1_consumer_admits_its_grants_and_c1_alone_refuses_the_rest():
    def executor(providers):
        return native_sdk.NativeSdkExecutor(
            root="/OFFLINE", runtime_path="/OFFLINE", api_url="https://api.offline.invalid", model_url=MODEL_ROOT,
            token_provider=lambda: "OFFLINE-token", capability_providers=providers)
    registered = native_sdk.native_capability_providers("https://api.offline.invalid", None)
    executor(registered).preflight(with_grants("fa-shell-v1", "fa-desktop-v1", browser=browser_grant()))
    # Only the C1 consumer itself is checked here; other consumers may later accept these grants.
    with pytest.raises(Problem) as refused:
        executor((local_capability_provider(),)).preflight(with_grants("fa-shell-v1", "fa-image-delivery-v1"))
    assert refused.value.body["error"]["code"] == "NATIVE_PROFILE_UNSUPPORTED"


def test_a_desktop_turn_with_the_image_browser_asks_the_desktop_for_its_own_browser(tmp_path):
    chrome = tmp_path / "ms-playwright" / "chromium-1246" / "chrome-linux64" / "chrome"
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"")
    chrome.chmod(0o755)
    image = browser_grant().model_copy(update={"launch": StdioEntry(
        type="stdio", command=sys.executable, args=("-c", "pass"),
        env=(("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "ms-playwright")),))})
    fake = Handlers()
    asyncio.run(bind_local(context(tmp_path, None), with_grants("fa-desktop-v1", browser=image).capabilities.grants,
                           handlers=fake.handlers))
    assert fake.displayed == [(NativeOwner(tmp_path / "native-turns"), str(chrome))]
    fake.displayed.clear()
    asyncio.run(bind_local(context(tmp_path, None), with_grants(browser=image).capabilities.grants,
                           handlers=fake.handlers))
    assert fake.displayed == []  # no desktop grant: Playwright stays headless and launches its own
