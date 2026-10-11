"""RTT cold turn: captured capability consumers, and distinct toolboxes, bind together (README "rtt-cold-bind").

dev v124 OK-0 bound seven consumers one after another (skills 0.6 s, catalog 1.3 s, desktop 2.3 s, toolbox 23.7 s,
the toolbox itself listing fa-org-tools and then the Team toolbox). Offline only: fake consumers and the toolbox
MockTransport; no SDK, model, network or MCP server runs. Each "together" case waits for the other side to start, so
binding one after another times out instead of passing.
"""

import asyncio
import hashlib
import json
import logging
from contextlib import AsyncExitStack

import httpx
import pytest

from shared_context import native_toolbox
from shared_context.behavior_capabilities import CapabilityGrant, McpToolResult
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolSpec
from shared_context.native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider, bind_capabilities
from shared_context.tool_catalog import ToolboxEntry
from test_native_toolbox import ENDPOINT, GRANT, Toolbox, context

WAIT = 2.0


@pytest.fixture(autouse=True)
def platform(monkeypatch):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(native_toolbox, "_CALLS", native_toolbox._Calls())


def grant(entry):
    return CapabilityGrant(entry_id=entry, entry_sha256=hashlib.sha256(entry.encode()).hexdigest(), kind="mcp-api",
                           tools=(entry + "-run",), vision=False)


def made(grants, close=None):
    name = grants[0].tools[0]

    async def handle(packet):
        raise AssertionError("not called")
    return NativeCapabilityBinding(tools=(ToolSpec(name, name, {"type": "object", "properties": {}}, handle),),
                                   result_schemas={name: McpToolResult}, close=close)


def consumer(entry, bind):
    return NativeCapabilityProvider(accepts=lambda item: item.entry_id == entry, bind=bind, name=entry)


def run_bind(ctx, providers):
    async def run():
        async with AsyncExitStack() as stack:
            return await bind_capabilities(ctx, providers, stack)
    return asyncio.run(run())


def test_consumers_bind_together_and_keep_their_order():
    started = {"alpha": asyncio.Event(), "beta": asyncio.Event()}

    def waiting(entry, other):
        async def bind(ctx, grants):
            started[entry].set()
            await asyncio.wait_for(started[other].wait(), WAIT)
            return made(grants)
        return consumer(entry, bind)

    ctx = context(grants=(grant("alpha"), grant("beta")))
    bindings = run_bind(ctx, (waiting("alpha", "beta"), waiting("beta", "alpha")))
    assert [tool.name for binding in bindings for tool in binding.tools] == ["alpha-run", "beta-run"]
    assert ctx.bound_tools == {"alpha-run", "beta-run"}


def test_the_first_failure_in_consumer_order_is_raised_and_every_made_binding_is_closed(caplog):
    caplog.set_level(logging.WARNING)
    closed, beta_failed = [], asyncio.Event()

    async def alpha(ctx, grants):
        # Fails after beta: the reported failure is still the first consumer's, as when they bound in order.
        await asyncio.wait_for(beta_failed.wait(), WAIT)
        raise Problem(503, "ALPHA_FAILED", "alpha")

    async def beta(ctx, grants):
        beta_failed.set()
        raise Problem(409, "BETA_FAILED", "beta")

    async def gamma(ctx, grants):
        async def close():
            closed.append("gamma")
        return made(grants, close)

    with pytest.raises(Problem) as failed:
        run_bind(context(grants=(grant("alpha"), grant("beta"), grant("gamma"))),
                 (consumer("alpha", alpha), consumer("beta", beta), consumer("gamma", gamma)))
    assert failed.value.body["error"]["code"] == "ALPHA_FAILED"
    assert closed == ["gamma"]
    for entry, code in (("alpha", "ALPHA_FAILED"), ("beta", "BETA_FAILED")):
        assert f"FA_NATIVE_CAPABILITY_BIND_FAILED entries={entry} code={code} exception_type=Problem" in caplog.text


def test_a_cancelled_turn_still_closes_the_bindings_already_made():
    closed, gamma_done, never = [], asyncio.Event(), asyncio.Event()

    async def alpha(ctx, grants):
        await never.wait()

    async def gamma(ctx, grants):
        async def close():
            closed.append("gamma")
        gamma_done.set()
        return made(grants, close)

    async def run():
        async with AsyncExitStack() as stack:
            task = asyncio.ensure_future(bind_capabilities(
                context(grants=(grant("alpha"), grant("gamma"))),
                (consumer("alpha", alpha), consumer("gamma", gamma)), stack))
            await asyncio.wait_for(gamma_done.wait(), WAIT)
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return closed
    assert asyncio.run(run()) == ["gamma"]


def test_an_earlier_consumers_failure_ends_the_bind_without_waiting_for_a_slower_one():
    cancelled, never = [], asyncio.Event()

    async def alpha(ctx, grants):
        raise Problem(409, "ALPHA_FAILED", "alpha")

    async def slow(ctx, grants):
        try:
            await never.wait()
        except asyncio.CancelledError:
            cancelled.append("slow")
            raise

    async def run():
        async with AsyncExitStack() as stack:
            return await asyncio.wait_for(bind_capabilities(
                context(grants=(grant("alpha"), grant("slow"))),
                (consumer("alpha", alpha), consumer("slow", slow)), stack), WAIT)
    with pytest.raises(Problem) as failed:
        asyncio.run(run())
    # The slower consumer is stopped, not left running after the turn's bind.
    assert failed.value.body["error"]["code"] == "ALPHA_FAILED" and cancelled == ["slow"]


def test_a_second_cancel_while_waiting_for_cleanup_still_closes_the_bindings_already_made():
    closed, gamma_done, cleaning, release = [], asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def alpha(ctx, grants):
        try:
            await release.wait()
        finally:
            # A consumer that takes a while to stop after the bind cancels it.
            cleaning.set()
            await release.wait()

    async def gamma(ctx, grants):
        async def close():
            closed.append("gamma")
        gamma_done.set()
        return made(grants, close)

    async def run():
        async with AsyncExitStack() as stack:
            task = asyncio.ensure_future(bind_capabilities(
                context(grants=(grant("alpha"), grant("gamma"))),
                (consumer("alpha", alpha), consumer("gamma", gamma)), stack))
            await asyncio.wait_for(gamma_done.wait(), WAIT)
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), WAIT)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return closed
    assert asyncio.run(run()) == ["gamma"]


def test_a_stopped_turn_binds_no_consumer():
    calls = []

    async def bind(ctx, grants):
        calls.append(grants[0].entry_id)
        return made(grants)

    ctx = context(grants=(grant("alpha"), grant("beta")))
    ctx.control.cancelled = True
    with pytest.raises(Problem) as stopped:
        run_bind(ctx, (consumer("alpha", bind), consumer("beta", bind)))
    assert stopped.value.body["error"]["code"] == "NATIVE_TURN_UNKNOWN" and calls == []


TEAM = CapabilityGrant(
    entry_id="TeamFeed", entry_sha256="7" * 64, kind="mcp-toolbox", tools=("TeamFeed-web",), vision=False,
    toolbox=ToolboxEntry(type="toolbox", toolbox="fa-team-x", server_label="WebIQ", version="2"))


class Gated:
    """The offline toolbox, but one toolbox's ``tools/list`` answers only after the other toolbox's has arrived."""

    def __init__(self, toolbox):
        self.toolbox, self.arrived = toolbox, {"fa-org-tools": asyncio.Event(), "fa-team-x": asyncio.Event()}
        self.transport = httpx.MockTransport(self.handle)

    async def handle(self, request):
        if request.method == "POST" and json.loads(request.content)["method"] == "tools/list":
            name = request.url.path.split("/toolboxes/")[1].split("/")[0]
            self.arrived[name].set()
            other, = set(self.arrived) - {name}
            await asyncio.wait_for(self.arrived[other].wait(), WAIT)
        return self.toolbox.handle(request)


def toolbox_bind(transport, grants):
    return run_bind(context(grants=grants), (native_toolbox.toolbox_provider(transport=transport),))


def test_distinct_toolboxes_are_listed_together():
    toolbox = Toolbox()
    gated = Gated(toolbox)
    binding, = toolbox_bind(gated.transport, (TEAM, GRANT))
    assert [tool.name for tool in binding.tools] == ["TeamFeed-web", "WebIQ-web", "WebIQ-browse"]
    # Only the unpinned organization toolbox reads its default version; each version is listed once.
    assert [str(request.url) for request in toolbox.gets] == [ENDPOINT + "/toolboxes/fa-org-tools?api-version=v1"]
    assert sorted(str(request.url) for request, body in toolbox.requests if body["method"] == "tools/list") == [
        ENDPOINT + "/toolboxes/fa-org-tools/versions/1/mcp?api-version=v1",
        ENDPOINT + "/toolboxes/fa-team-x/versions/2/mcp?api-version=v1"]


def test_a_toolbox_failure_is_still_reported_at_its_first_grant():
    toolbox = Toolbox()
    team_missing = [{"name": "Other___web", "description": "x", "inputSchema": {"type": "object"}}]
    toolbox.versions["2"] = team_missing
    original = toolbox.handle

    def failing_org(request):
        if request.method == "POST" and "/toolboxes/fa-org-tools/" in request.url.path:
            if json.loads(request.content)["method"] == "tools/list":
                return httpx.Response(500)
        return original(request)
    toolbox.transport = httpx.MockTransport(failing_org)
    # TeamFeed comes first: its missing tool is reported, not the organization toolbox's later list failure.
    with pytest.raises(Problem) as first:
        toolbox_bind(toolbox.transport, (TEAM, GRANT))
    assert (first.value.body["error"]["code"], first.value.body["error"]["capability"]) == (
        "CAPABILITY_UNAVAILABLE", "TeamFeed")
    toolbox.versions["2"] = toolbox.tools
    # Alone, the organization toolbox's failed list only leaves that toolbox out; TeamFeed binds as listed.
    binding, = toolbox_bind(toolbox.transport, (TEAM, GRANT))
    assert {tool.name: tool.description for tool in binding.tools} == {
        "TeamFeed-web": "Search the web",
        "WebIQ-web": "Unavailable this turn: WebIQ could not list its tools. Calling it only reports that.",
        "WebIQ-browse": "Unavailable this turn: WebIQ could not list its tools. Calling it only reports that."}


def test_every_grant_on_a_version_sees_the_list_an_earlier_grant_refreshed():
    """A Team toolbox has one grant per server on the same version; a kept empty list is listed once more."""
    toolbox = Toolbox()
    other = CapabilityGrant(
        entry_id="Other", entry_sha256="8" * 64, kind="mcp-toolbox", tools=("Other-web",), vision=False,
        toolbox=ToolboxEntry(type="toolbox", toolbox="fa-org-tools", server_label="Other"))
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport)
    # Just after creation the version lists nothing (official troubleshooting); this turn fails and keeps that list.
    toolbox.versions["1"] = []
    with pytest.raises(Problem) as empty:
        run_bind(context(grants=(other, GRANT)), (provider,))
    assert empty.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    del toolbox.versions["1"]
    binding, = run_bind(context(grants=(other, GRANT), rid="rid-2"), (provider,))
    assert [tool.name for tool in binding.tools] == ["Other-web", "WebIQ-web", "WebIQ-browse"]
    # Turn 1 listed once; turn 2 listed once more for the first grant, and the second grant used that list.
    assert sum(body["method"] == "tools/list" for _, body in toolbox.requests) == 2
