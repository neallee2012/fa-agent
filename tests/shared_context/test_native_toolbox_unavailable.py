"""A toolbox that cannot list its tools leaves only itself out of the turn (Neal 2026-10-10).

「工具列不出來時，這一回合少那個工具照常跑，並在畫面上講明」: the turn runs, that toolbox's captured tools only report
``NATIVE_TOOLBOX_UNAVAILABLE`` and send nothing, the turn's assembly record names the source for the workbench, and a
turn within the backoff reuses that failure (README#toolbox-list-backoff) before one lists it again. A toolbox's own definite refusal and the capture checks after a list still refuse the turn.

Offline: the real native executor, Hosted routes and State store doubles from the existing harness; the toolbox is an
httpx MockTransport and the SDK an offline double. ``STAGE_WEBIQ_401`` is the -32006 message a read-only stage
toolbox tools/list returned on 2026-10-10 (ids redacted); ``UPSTREAM_401`` is a synthetic one of the same shape.
"""

import asyncio
import json
import logging
from contextlib import AsyncExitStack

import httpx
import pytest

from shared_context import native_toolbox
from shared_context.behavior_capabilities import CapabilityCapture, available_grants
from shared_context.errors import Problem
from shared_context.native_assembly import NativeAssemblyCollector
from shared_context.native_capabilities import bind_capabilities
from shared_context.behavior_native_turn_wire import NativePrewarmToolbox, NativeSubmit
from shared_context.behavior_timing import timing_id
from shared_context.m2_copilot import ToolFailure
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_assembly import install_executor, offline_sdk, run_turn
from test_native_hitl import hitl  # noqa: F401
from test_native_hitl_attention import attention  # noqa: F401
from test_native_responses import response_submit, responses  # noqa: F401
from shared_context.tool_catalog import build_catalog, parse_entry_file
from test_native_toolbox import (CONSENT_URL, ENDPOINT, GRANT, OTHER_GRANT, TOOLBOX, Toolbox, bind_on, call,
                                 consent_error, context, methods, unconnected)
from test_native_toolbox_approval import packet, permit, setup
from test_native_toolbox_parallel import boundary
from test_native_turns import local_env  # noqa: F401
from test_connector_consent import card, click, not_connected, page
from test_native_prewarm import Gated

UPSTREAM_TEXT = "auth_invalid_api_key UPSTREAM-CANARY-5d1"
# One failed source, an upstream 401, and a link that is not a consent link.
UPSTREAM_401 = "tools/list failed for 1 tool source(s), succeeded for 0 tool source(s) " + json.dumps({"errors": [
    {"name": "WebIQ", "type": "mcp", "error": {
        "code": "TOOL_SOURCE_ERROR",
        "message": f"https://upstream.example.test/mcp returned 401 Unauthorized: {UPSTREAM_TEXT}"}}]})
# Stage, 2026-10-10: the WebIQ source's API key was rejected upstream. Its URL is not a consent link: the per-source
# list has no CONSENT_REQUIRED entry.
STAGE_WEBIQ_401 = "tools/list failed for 1 tool source(s), succeeded for 0 tool source(s) " + json.dumps(
    {"errors": [{"name": "WebIQ", "type": "mcp", "error": {"code": "HTTP_401", "message": (
        "Authentication failed when connecting to the MCP server: https://api.microsoft.ai:443/v3/mcp: Response "
        "status code does not indicate success: 401 (Unauthorized). Response body: {\n  \"jsonrpc\": \"2.0\",\n  "
        "\"id\": \"0000\",\n  \"error\": {\n    \"code\": \"auth_invalid_api_key\",\n    \"message\": "
        "\"Invalid API key provided. (Details: Invalid API key)\"\n  }\n}")}}]}, separators=(",", ":"))
STAGE_TEXT = ("auth_invalid_api_key", "Invalid API key", "api.microsoft.ai", "Authentication failed")
UNAVAILABLE_DESCRIPTION = "Unavailable this turn: WebIQ could not list its tools. Calling it only reports that."


@pytest.fixture(autouse=True)
def platform(monkeypatch):
    # The toolbox's project endpoint only: the harness's local State store must not see a project endpoint.
    monkeypatch.setattr(native_toolbox, "project_endpoint", lambda: ENDPOINT)
    monkeypatch.setattr(native_toolbox, "_CALLS", native_toolbox._Calls())


def answering(toolbox, method, response):
    """``toolbox`` answers ``method`` with ``response`` while ``toolbox.broken``; otherwise as usual."""
    original, toolbox.broken = toolbox.handle, True

    def handle(request):
        if request.method != "GET" and toolbox.broken:
            body = json.loads(request.content)
            if body["method"] == method:
                toolbox.requests.append((request, body))
                return response(body)
        return original(request)
    toolbox.transport = httpx.MockTransport(handle)
    return toolbox


def rpc(number, message):
    return lambda body: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {
        "code": number, "message": message}})


upstream_401 = rpc(-32006, UPSTREAM_401)
SOURCE_FAILURES = {"synthetic": (upstream_401, (UPSTREAM_TEXT, "upstream.example.test")),
                   "stage": (rpc(-32006, STAGE_WEBIQ_401), STAGE_TEXT)}


def toolbox_request():
    data = response_submit().model_dump(mode="json")
    data["capture"]["template"]["settings"]["capabilities"] = [GRANT.ref.model_dump(mode="json")]
    data["capture"]["capabilities"] = CapabilityCapture(head_selection_id="toolbox-head", grants=(GRANT,)).model_dump(
        mode="json")
    return NativeSubmit.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("failure", sorted(SOURCE_FAILURES))
def test_a_turn_whose_toolbox_cannot_list_its_tools_completes_without_it_and_names_the_source(
        responses, monkeypatch, tmp_path, caplog, failure):
    caplog.set_level(logging.WARNING)
    reply, secrets = SOURCE_FAILURES[failure]
    toolbox = answering(Toolbox(), "tools/list", reply)
    seen, failures = {}, []

    async def sdk(**options):
        for spec in options["tools"]:
            if spec.name.startswith("WebIQ-"):
                seen[spec.name] = spec.description
                try:
                    await spec.handler({"session_id": options["sdk_session_id"], "tool_call_id": "call-" + spec.name,
                                        "tool_name": spec.name, "arguments": {"search_query": "fa"}})
                except ToolFailure as error:
                    failures.append(error.public_code)
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk, api_token_provider=lambda: "OFFLINE-api-token",
                     capability_providers=(native_toolbox.toolbox_provider(transport=toolbox.transport),))
    completed = run_turn(responses, toolbox_request())
    assert completed.status == "completed" and completed.failure_code is None
    # The captured tools stay bound, only say they are unavailable, and nothing reaches the toolbox.
    assert seen == {"WebIQ-web": UNAVAILABLE_DESCRIPTION, "WebIQ-browse": UNAVAILABLE_DESCRIPTION}
    assert failures == ["NATIVE_TOOLBOX_UNAVAILABLE", "NATIVE_TOOLBOX_UNAVAILABLE"] and toolbox.calls == []
    assert [item.model_dump(mode="json") for item in completed.assembly.unavailable_sources] == [
        {"entry_id": "WebIQ", "source": "WebIQ", "code": "TOOLBOX_LIST_FAILED", "reason": "upstream_auth"}]
    lines = [record.getMessage() for record in caplog.records]
    assert (f"FA_NATIVE_TOOL_SOURCE_UNAVAILABLE agent_id={completed.agent_id} request_id=rid-1 entry=WebIQ "
            "code=TOOLBOX_LIST_FAILED reason=upstream_auth") in lines
    assert not any("NATIVE_TURN" in line and "REFUSED" in line for line in lines)
    assert not any("NATIVE_CONNECTOR_NOT_CONNECTED" in line or "need=consent" in line for line in lines)
    # The upstream's own text and links stay out of the receipt and the logs.
    saved = completed.model_dump_json()
    assert not [text for text in secrets if text in saved or text in caplog.text]


@pytest.mark.parametrize("broken,code", [("refused", "NATIVE_CAPABILITY_REFUSED"),
                                         ("missing", "CAPABILITY_UNAVAILABLE")])
def test_a_toolbox_refusal_or_a_capture_mismatch_still_refuses_the_turn(responses, monkeypatch, tmp_path, broken, code):
    toolbox, started = Toolbox(), []
    if broken == "refused":
        # The toolbox's own definite answer about this Agent: an authorization decision, not a list that failed.
        toolbox.transport = httpx.MockTransport(lambda request: httpx.Response(403))
    else:
        # Listed, but a captured tool is gone: the capture no longer matches the toolbox.
        toolbox.tools = [tool for tool in toolbox.tools if tool["name"] != "WebIQ.browse"]

    async def sdk(**options):
        started.append(options)
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk, api_token_provider=lambda: "OFFLINE-api-token",
                     capability_providers=(native_toolbox.toolbox_provider(transport=toolbox.transport),))
    request = toolbox_request()
    accepted = responses.invoke(request)
    assert accepted.status_code == 202, accepted.text
    assert responses.respond(responses.call(responses.runtime.read, request.command.request_id)).status_code == 200
    refused = responses.wait(request.command.request_id, "unknown")
    assert refused.failure_code == code and started == [] and toolbox.calls == []
    assert refused.assembly.unavailable_sources == ()


class Clock:
    """A fake monotonic clock for the listing backoff (README#toolbox-list-backoff); no sleeps."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def recorded(rid):
    ctx = context(rid=rid)
    ctx.receipt.capture_digest = "0" * 64
    ctx.assembly = NativeAssemblyCollector(ctx.receipt)
    return ctx


def backoff_skips(caplog):
    return [json.loads(record.getMessage().removeprefix("FA_TIMING ")) for record in caplog.records
            if record.getMessage().startswith("FA_TIMING ") and "native_bind_toolbox_list_backoff" in record.getMessage()]


def test_a_failed_listing_is_remembered_for_the_backoff_and_listed_again_after_it(caplog):
    caplog.set_level(logging.WARNING)
    clock, backoff = Clock(), native_toolbox.LIST_FAILURE_BACKOFF_SECONDS
    assert backoff == 60.0
    toolbox = answering(Toolbox(), "tools/list", lambda body: httpx.Response(503))
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport, clock=clock)
    turns = [recorded("rid-1")]
    first = bind_on(provider, turns[0])
    with pytest.raises(ToolFailure) as left_out:
        call(first["WebIQ-web"], {"search_query": "fa"}, "first-turn")
    # The first failing turn is unchanged: one listing, left out, named, and nothing skipped.
    assert left_out.value.public_code == "NATIVE_TOOLBOX_UNAVAILABLE"
    assert methods(toolbox).count("tools/list") == 1 and backoff_skips(caplog) == []
    clock.now += backoff - 1
    turns.append(recorded("rid-2"))
    second = bind_on(provider, turns[1])
    # Inside the backoff: no listing, the same degradation, the same reason, and an exported FA_TIMING marker.
    assert methods(toolbox).count("tools/list") == 1
    assert {name: spec.description for name, spec in second.items()} == {
        "WebIQ-web": UNAVAILABLE_DESCRIPTION, "WebIQ-browse": UNAVAILABLE_DESCRIPTION}
    with pytest.raises(ToolFailure) as left_out:
        call(second["WebIQ-web"], {"search_query": "fa"}, "second-turn")
    assert left_out.value.public_code == "NATIVE_TOOLBOX_UNAVAILABLE" and toolbox.calls == []
    assert [[item.model_dump(mode="json") for item in ctx.assembly.snapshot().unavailable_sources] for ctx in turns] \
        == [[{"entry_id": "WebIQ", "source": "WebIQ", "code": "TOOLBOX_LIST_FAILED", "reason": "service_error"}]] * 2
    assert left_out_lines(caplog) == [
        f"FA_NATIVE_TOOL_SOURCE_UNAVAILABLE agent_id=fa-agent-1 request_id={rid} entry=WebIQ "
        "code=TOOLBOX_LIST_FAILED reason=service_error" for rid in ("rid-1", "rid-2")]
    skips = backoff_skips(caplog)
    assert len(skips) == 1 and skips[0]["outcome"] == "returned" and skips[0]["job_id"] == timing_id("rid-2")
    # Once the backoff ends, the next turn lists again and the toolbox is back without a restart.
    clock.now += 1
    toolbox.broken = False
    third = bind_on(provider, context(rid="rid-3"))
    assert third["WebIQ-web"].description == "Search the web"
    assert json.loads(call(third["WebIQ-web"], {"search_query": "fa"}, "third-turn").text)["text"] == "found\ncited"
    assert methods(toolbox).count("tools/list") == 2
    # Listed again, the version binds from its kept list: a later broken toolbox is not consulted.
    toolbox.broken = True
    assert bind_on(provider, context(rid="rid-4"))["WebIQ-web"].description == "Search the web"
    assert methods(toolbox).count("tools/list") == 2 and len(backoff_skips(caplog)) == 1


def test_a_prewarm_inside_the_backoff_still_lists_and_its_success_ends_the_backoff():
    clock = Clock()
    toolbox = answering(Toolbox(), "tools/list", lambda body: httpx.Response(503))
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport, clock=clock)
    bind_on(provider, context())
    toolbox.broken = False
    clock.now += 1
    # The owner types: the prewarm lists off the turn's path even inside the backoff, and recovers the toolbox.
    asyncio.run(provider.warm(lambda: "agent-token", (NativePrewarmToolbox(toolbox="fa-org-tools"),)))
    assert methods(toolbox).count("tools/list") == 2
    tools = bind_on(provider, context(rid="rid-2"))
    assert tools["WebIQ-web"].description == "Search the web" and methods(toolbox).count("tools/list") == 2


def test_a_failed_prewarm_does_not_start_a_backoff():
    clock = Clock()
    toolbox = answering(Toolbox(), "tools/list", lambda body: httpx.Response(503))
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport, clock=clock)
    asyncio.run(provider.warm(lambda: "agent-token", (NativePrewarmToolbox(toolbox="fa-org-tools"),)))
    toolbox.broken = False
    # Only a turn's own failed listing starts one: the first turn after a failed prewarm lists as before.
    assert bind_on(provider, context())["WebIQ-web"].description == "Search the web"
    assert methods(toolbox).count("tools/list") == 2


def test_a_connection_need_is_remembered_for_the_backoff_too(caplog):
    caplog.set_level(logging.WARNING)
    clock = Clock()
    toolbox = unconnected(Toolbox(), "tools/list", consent_error)
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport, clock=clock)
    for rid in ("rid-1", "rid-2"):
        tools = bind_on(provider, context(rid=rid))
        with pytest.raises(ToolFailure) as failed:
            call(tools["WebIQ-web"], {}, "call-" + rid)
        assert failed.value.public_code == "NATIVE_CONNECTOR_NOT_CONNECTED"
        assert tools["WebIQ-web"].description.startswith("Not connected for this assistant: WebIQ.")
        clock.now += 1
    assert methods(toolbox).count("tools/list") == 1 and len(backoff_skips(caplog)) == 1
    assert CONSENT_URL not in caplog.text and toolbox.calls == []


def test_the_backoff_never_reaches_another_toolbox_or_another_agent():
    clock = Clock()
    toolbox = unconnected(Toolbox(), "tools/list", lambda body: httpx.Response(503))
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport, clock=clock)
    assert bind_on(provider, context())["WebIQ-web"].description == UNAVAILABLE_DESCRIPTION
    # Another toolbox in the same process (the same Agent) lists as usual inside the window.
    assert bind_on(provider, context(grants=(OTHER_GRANT,), rid="rid-2"))["Other-web"].description == "Another source"
    assert methods(toolbox).count("tools/list") == 2
    # Another Agent is another Hosted process, so another provider: it lists the toolbox itself.
    healthy = Toolbox()
    other_agent = native_toolbox.toolbox_provider(transport=healthy.transport, clock=clock)
    assert bind_on(other_agent, context(rid="rid-3"))["WebIQ-web"].description == "Search the web"
    assert methods(healthy).count("tools/list") == 1


def switchable(reply):
    """``answering`` with a reply the test can swap between turns."""
    replies = [reply]
    return answering(Toolbox(), "tools/list", lambda body: replies[0](body)), replies


def connect_turn(hitl, tmp_path, provider, toolbox, before_click):
    """One owner turn on ``fa-org-tools`` whose Connect card is answered 「我連好了，繼續」 once; returns its tools or its
    refusal."""
    case = setup(hitl, tmp_path, "never", toolbox=toolbox, provider=provider)

    async def check():
        binding = asyncio.create_task(case.bind())
        item = (await card(case)).json()["attention"][0]
        before_click()
        assert click(hitl, item).status_code == 200
        try:
            return await binding
        except Problem as refused:
            return refused
        finally:
            await case.gate.close()
    return asyncio.run(check())


def test_a_connect_success_clears_every_remembered_connection_need_in_the_provider(hitl, tmp_path):
    clock = Clock()
    toolbox, _ = switchable(not_connected)
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport, clock=clock)
    # A turn nobody can answer (relay, Agent, routine) remembers both toolboxes' connection need.
    both = bind_on(provider, context(grants=(OTHER_GRANT, GRANT)))
    assert both["WebIQ-web"].description.startswith("Not connected")
    assert both["Other-web"].description.startswith("Not connected") and methods(toolbox).count("tools/list") == 2
    clock.now += 1
    # The owner connects in another conversation's card for fa-org-tools; its re-list never reads the backoff.
    tools = connect_turn(hitl, tmp_path, provider, toolbox, lambda: setattr(toolbox, "broken", False))
    assert tools["WebIQ-web"].description == "Search the web" and methods(toolbox).count("tools/list") == 3
    clock.now += 1
    # Inside the window the next turn binds both: fa-org-tools from its kept list, fa-other-tools listed again.
    both = bind_on(provider, context(grants=(OTHER_GRANT, GRANT), rid="rid-3"))
    assert both["WebIQ-web"].description == "Search the web" and both["Other-web"].description == "Another source"
    assert methods(toolbox).count("tools/list") == 4


def test_a_cleared_need_on_a_toolbox_still_unconnected_is_listed_once_and_remembered_again(hitl, tmp_path):
    clock = Clock()
    # fa-other-tools stays unconnected; fa-org-tools is unconnected only until the owner's click.
    toolbox = unconnected(Toolbox(), "tools/list", not_connected, name="fa-other-tools")
    toolbox.handle = toolbox.transport.handler
    toolbox = answering(toolbox, "tools/list", not_connected)
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport, clock=clock)

    def other_lists():
        return sum(body["method"] == "tools/list" and "/fa-other-tools/" in request.url.path
                   for request, body in toolbox.requests)
    bind_on(provider, context(grants=(OTHER_GRANT, GRANT)))
    assert other_lists() == 1
    clock.now += 1
    # The owner connects fa-org-tools only; its success also clears fa-other-tools' remembered need.
    tools = connect_turn(hitl, tmp_path, provider, toolbox, lambda: setattr(toolbox, "broken", False))
    assert tools["WebIQ-web"].description == "Search the web" and other_lists() == 1
    for rid in ("rid-3", "rid-4"):
        clock.now += 1
        # rid-3 lists fa-other-tools once and remembers its need again; rid-4, inside that window, does not list it.
        both = bind_on(provider, context(grants=(OTHER_GRANT, GRANT), rid=rid))
        assert both["Other-web"].description.startswith("Not connected for this assistant: Other.")
        assert both["WebIQ-web"].description == "Search the web" and other_lists() == 2


def test_an_explicit_refusal_of_the_connect_relist_drops_the_remembered_need(hitl, tmp_path):
    clock = Clock()
    toolbox, replies = switchable(not_connected)
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport, clock=clock)
    bind_on(provider, context())
    clock.now += 1
    # The card turn reuses the remembered need, then its re-list gets the toolbox's own 403: the turn is refused.
    refused = connect_turn(hitl, tmp_path, provider, toolbox,
                           lambda: replies.__setitem__(0, lambda body: httpx.Response(403)))
    assert isinstance(refused, Problem) and methods(toolbox).count("tools/list") == 2
    clock.now += 1
    toolbox.broken = False
    # Nothing is remembered from a refusal: the next turn inside the window lists again.
    assert bind_on(provider, context(rid="rid-3"))["WebIQ-web"].description == "Search the web"
    assert methods(toolbox).count("tools/list") == 3


def test_a_connect_relist_that_cannot_list_is_remembered_like_a_turn_listing(hitl, tmp_path):
    clock = Clock()
    toolbox, replies = switchable(not_connected)
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport, clock=clock)
    bind_on(provider, context())
    clock.now += 1
    tools = connect_turn(hitl, tmp_path, provider, toolbox,
                         lambda: replies.__setitem__(0, lambda body: httpx.Response(503)))
    assert tools["WebIQ-web"].description == UNAVAILABLE_DESCRIPTION and methods(toolbox).count("tools/list") == 2
    clock.now += 1
    ctx = recorded("rid-3")
    # The next turn reuses the re-list's service_error without listing, so without another card.
    assert bind_on(provider, ctx)["WebIQ-web"].description == UNAVAILABLE_DESCRIPTION
    assert methods(toolbox).count("tools/list") == 2
    assert [item.reason for item in ctx.assembly.snapshot().unavailable_sources] == ["service_error"]


def test_an_identity_token_failure_is_not_remembered(caplog):
    caplog.set_level(logging.WARNING)
    clock = Clock()
    toolbox = Toolbox()
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport, clock=clock)
    # The default version is read with a token; only the listing's token is invalid, as in a blip.
    tokens = iter(["agent-token-0"])
    ctx = recorded("rid-1")
    ctx.model_token_provider = lambda: next(tokens, "")
    assert bind_on(provider, ctx)["WebIQ-web"].description == UNAVAILABLE_DESCRIPTION
    assert [item.reason for item in ctx.assembly.snapshot().unavailable_sources] == ["identity_unavailable"]
    assert methods(toolbox).count("tools/list") == 0
    clock.now += 1
    # This VM's token, not the toolbox: the next turn inside the window lists as usual.
    assert bind_on(provider, context(rid="rid-2"))["WebIQ-web"].description == "Search the web"
    assert methods(toolbox).count("tools/list") == 1 and backoff_skips(caplog) == []


def prewarm_waits(monkeypatch):
    """Set when a turn starts waiting for the prewarm in flight: a deterministic point for the test, no sleep."""
    waiting = asyncio.Event()

    class Asyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        @staticmethod
        async def wait(fs, **kwargs):
            waiting.set()
            return await asyncio.wait(fs, **kwargs)
    monkeypatch.setattr(native_toolbox, "asyncio", Asyncio())
    return waiting


def warm_org(provider):
    return asyncio.ensure_future(provider.warm(lambda: "agent-token", (NativePrewarmToolbox(toolbox="fa-org-tools"),)))


def test_a_turn_waits_for_an_inflight_prewarm_before_the_backoff_and_its_success_wins(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    clock = Clock()
    toolbox = Toolbox()
    gated = Gated(toolbox)
    gated.refuse = 1
    provider = native_toolbox.toolbox_provider(transport=gated, clock=clock)
    waiting = prewarm_waits(monkeypatch)

    async def run():
        gated.listing, gated.release = asyncio.Event(), asyncio.Event()
        gated.release.set()
        async with AsyncExitStack() as stack:
            # The turn's own listing gets a 503: remembered for the backoff.
            await bind_capabilities(context(), (provider,), stack)
        clock.now += 1
        gated.listing.clear()
        gated.release.clear()
        warming = warm_org(provider)
        await gated.listing.wait()
        async with AsyncExitStack() as stack:
            binding = asyncio.ensure_future(bind_capabilities(context(rid="rid-2"), (provider,), stack))
            await waiting.wait()
            # Inside the backoff, the turn still waits for the prewarm in flight.
            assert not binding.done()
            gated.release.set()
            bindings = await binding
        await warming
        return {spec.name: spec.description for item in bindings for spec in item.tools}
    assert asyncio.run(run()) == {"WebIQ-web": "Search the web", "WebIQ-browse": "Browse a page"}
    assert methods(toolbox).count("tools/list") == 2 and backoff_skips(caplog) == []


class TimingOut(Gated):
    """``Gated``, whose every ``tools/list`` times out once released: the slow failure a timeout leaves."""

    async def handle_async_request(self, request):
        await request.aread()
        if request.method == "POST" and json.loads(request.content)["method"] == "tools/list":
            self.toolbox.requests.append((request, json.loads(request.content)))
            self.listing.set()
            await self.release.wait()
            raise httpx.ReadTimeout("tools/list timed out", request=request)
        return self.toolbox.handle(request)


def test_a_turn_started_inside_the_backoff_reuses_it_when_the_window_ends_during_a_failed_prewarm(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    clock, toolbox = Clock(), Toolbox()
    slow = TimingOut(toolbox)
    provider = native_toolbox.toolbox_provider(transport=slow, clock=clock)
    waiting = prewarm_waits(monkeypatch)
    turns = [recorded("rid-1"), recorded("rid-2")]

    async def run():
        slow.listing, slow.release = asyncio.Event(), asyncio.Event()
        slow.release.set()
        async with AsyncExitStack() as stack:
            # The turn's own listing times out: remembered as network for the backoff.
            await bind_capabilities(turns[0], (provider,), stack)
        clock.now += 1
        slow.listing.clear()
        slow.release.clear()
        warming = warm_org(provider)
        await slow.listing.wait()
        async with AsyncExitStack() as stack:
            binding = asyncio.ensure_future(bind_capabilities(turns[1], (provider,), stack))
            await waiting.wait()
            # The window ends while the turn waits for the slow prewarm, which then times out too.
            clock.now += native_toolbox.LIST_FAILURE_BACKOFF_SECONDS
            slow.release.set()
            bindings = await binding
        await warming
        return {spec.name: spec.description for item in bindings for spec in item.tools}
    assert asyncio.run(run()) == {"WebIQ-web": UNAVAILABLE_DESCRIPTION, "WebIQ-browse": UNAVAILABLE_DESCRIPTION}
    # The first turn's listing and the prewarm's only: after the wait the turn reused the failure instead of listing.
    assert methods(toolbox).count("tools/list") == 2 and len(backoff_skips(caplog)) == 1
    assert [[item.reason for item in ctx.assembly.snapshot().unavailable_sources] for ctx in turns] == [["network"]] * 2


@pytest.mark.parametrize("failure", sorted(SOURCE_FAILURES))
def test_with_native_hitl_an_upstream_failure_is_left_out_without_a_connect_card(attention, tmp_path, failure):
    case = setup(attention, tmp_path, "never", toolbox=answering(Toolbox(), "tools/list", SOURCE_FAILURES[failure][0]))

    async def check():
        # No owner card to wait for: the bind returns on its own.
        tools = await asyncio.wait_for(case.bind(), timeout=5)
        # Through the HITL gate the call needs no approval and only reports that the source is unavailable.
        assert await permit(case, "left-out") == (True, None)
        with pytest.raises(ToolFailure) as left_out:
            await tools["WebIQ-web"].handler(packet("left-out"))
        await case.gate.close()
        return tools, left_out.value
    tools, left_out = asyncio.run(check())
    assert tools["WebIQ-web"].description == UNAVAILABLE_DESCRIPTION
    assert left_out.public_code == "NATIVE_TOOLBOX_UNAVAILABLE"
    assert page(case.c, case.gate.turn).json()["references"] == []
    assert case.toolbox.calls == []


def test_the_model_is_told_the_source_is_unavailable_this_turn_and_not_refused():
    text = boundary().public_tool_failure("WebIQ-web", "NATIVE_TOOLBOX_UNAVAILABLE", "ignored detail")
    assert text == (
        "Tool call failed [NATIVE_TOOLBOX_UNAVAILABLE]. This tool source could not list its tools when this turn "
        "started, so it is unavailable for this turn and nothing was sent to it. This is not a permission refusal. "
        "Answer with what you can do without it and tell the user plainly that this source was not available this "
        "time; do not call its tools again in this turn and do not claim a result from it.")


@pytest.mark.parametrize("number,message,reason", [
    # A source failed upstream, without a consent link: the toolbox itself answered.
    (-32006, "tools/list failed for 1 tool source(s), succeeded for 0 tool source(s) " + json.dumps(
        {"errors": [{"name": "WebIQ", "type": "mcp", "error": {"code": "HTTP_500", "message": "Internal"}}]}),
     "source_error"),
    (-32603, "Internal error", "source_error"),
    (-32000, "Server error", "source_error"),
    (-32603, "upstream answered 401 (Unauthorized)", "upstream_auth"),
    ("x", "Internal error", "source_error"),
])
def test_a_source_or_server_error_to_the_listing_leaves_only_this_toolbox_out(caplog, number, message, reason):
    caplog.set_level(logging.WARNING)
    toolbox = answering(Toolbox(), "tools/list", rpc(number, message))
    tools = bind_on(native_toolbox.toolbox_provider(transport=toolbox.transport), context())
    with pytest.raises(ToolFailure) as left_out:
        call(tools["WebIQ-web"], {"search_query": "fa"}, "left-out")
    assert left_out.value.public_code == "NATIVE_TOOLBOX_UNAVAILABLE" and toolbox.calls == []
    assert ("FA_NATIVE_TOOL_SOURCE_UNAVAILABLE agent_id=fa-agent-1 request_id=rid-1 entry=WebIQ "
            f"code=TOOLBOX_LIST_FAILED reason={reason}") in [record.getMessage() for record in caplog.records]


@pytest.mark.parametrize("number,message", [
    (-32700, "Parse error"),
    (-32600, "Invalid Request"),
    (-32601, "Method not found"),
    (-32602, "Invalid params"),
    (-32007, "INVALID_CONFIGURATION"),
    # Upstream-auth words do not turn a definite answer about the request or configuration into a failed source.
    (-32007, "INVALID_CONFIGURATION: 401 (Unauthorized)"),
    (-32602, "Invalid params: auth_invalid_api_key"),
])
def test_a_definite_answer_about_the_request_or_configuration_still_refuses_the_turn(caplog, number, message):
    caplog.set_level(logging.WARNING)
    toolbox = answering(Toolbox(), "tools/list", rpc(number, message))
    with pytest.raises(Problem):
        bind_on(native_toolbox.toolbox_provider(transport=toolbox.transport), context())
    assert "stage=rpc_error code=" in caplog.text and f"rpc_code={number}" in caplog.text
    assert "FA_NATIVE_TOOL_SOURCE_UNAVAILABLE" not in caplog.text and toolbox.calls == []


def test_a_source_several_grants_share_is_named_once_per_turn(caplog):
    caplog.set_level(logging.WARNING)
    news = parse_entry_file("webiq-news.json", json.dumps({"mcpServers": {"WebIQNews": {
        "type": "toolbox", "toolbox": "fa-org-tools", "server_label": "WebIQ", "tools": ["news"]}}}).encode())
    grants = available_grants(build_catalog([TOOLBOX, news]))
    ctx = context(grants=(grants["WebIQ"], grants["WebIQNews"]))
    ctx.receipt.capture_digest = "0" * 64
    ctx.assembly = NativeAssemblyCollector(ctx.receipt)
    toolbox = answering(Toolbox(), "tools/list", upstream_401)
    tools = bind_on(native_toolbox.toolbox_provider(transport=toolbox.transport), ctx)
    # Every captured tool still reports the source is unavailable; the source is listed and named once.
    assert set(tools) == {"WebIQ-web", "WebIQ-browse", "WebIQNews-news"}
    assert methods(toolbox).count("tools/list") == 1
    lines = [record.getMessage() for record in caplog.records
             if record.getMessage().startswith("FA_NATIVE_TOOL_SOURCE_UNAVAILABLE")]
    assert lines == ["FA_NATIVE_TOOL_SOURCE_UNAVAILABLE agent_id=fa-agent-1 request_id=rid-1 entry=WebIQ "
                     "code=TOOLBOX_LIST_FAILED reason=upstream_auth"]
    assert [item.model_dump(mode="json") for item in ctx.assembly.snapshot().unavailable_sources] == [
        {"entry_id": "WebIQ", "source": "WebIQ", "code": "TOOLBOX_LIST_FAILED", "reason": "upstream_auth"}]


def two_sources(reply):
    """One toolbox serving two captured sources, Bing and WebIQ, whose listing ``reply`` answers; with the turn's
    record."""
    bing = parse_entry_file("bing.json", json.dumps({"mcpServers": {"Bing": {
        "type": "toolbox", "toolbox": "fa-org-tools", "server_label": "Bing", "tools": ["search"]}}}).encode())
    grants = available_grants(build_catalog([TOOLBOX, bing]))
    ctx = context(grants=(grants["Bing"], grants["WebIQ"]))
    ctx.receipt.capture_digest = "0" * 64
    ctx.assembly = NativeAssemblyCollector(ctx.receipt)
    toolbox = answering(Toolbox(), "tools/list", reply)
    return toolbox, ctx, bind_on(native_toolbox.toolbox_provider(transport=toolbox.transport), ctx)


def left_out_lines(caplog):
    return [record.getMessage() for record in caplog.records
            if record.getMessage().startswith("FA_NATIVE_TOOL_SOURCE_UNAVAILABLE")]


def test_a_toolbox_that_names_its_failed_source_gives_only_that_source_its_reason(caplog):
    caplog.set_level(logging.WARNING)
    # The stage message, as if the toolbox's other source had listed: only WebIQ is in the per-source list.
    toolbox, ctx, tools = two_sources(rpc(-32006, STAGE_WEBIQ_401.replace("succeeded for 0", "succeeded for 1")))
    assert {name: spec.description for name, spec in tools.items()} == {
        "WebIQ-web": UNAVAILABLE_DESCRIPTION, "WebIQ-browse": UNAVAILABLE_DESCRIPTION,
        "Bing-search": "Unavailable this turn: Bing could not list its tools. Calling it only reports that."}
    assert methods(toolbox).count("tools/list") == 1 and toolbox.calls == []
    assert left_out_lines(caplog) == [
        "FA_NATIVE_TOOL_SOURCE_UNAVAILABLE agent_id=fa-agent-1 request_id=rid-1 entry=Bing "
        "code=TOOLBOX_LIST_FAILED reason=source_error",
        "FA_NATIVE_TOOL_SOURCE_UNAVAILABLE agent_id=fa-agent-1 request_id=rid-1 entry=WebIQ "
        "code=TOOLBOX_LIST_FAILED reason=upstream_auth"]
    assert [item.model_dump(mode="json") for item in ctx.assembly.snapshot().unavailable_sources] == [
        {"entry_id": "Bing", "source": "Bing", "code": "TOOLBOX_LIST_FAILED", "reason": "source_error"},
        {"entry_id": "WebIQ", "source": "WebIQ", "code": "TOOLBOX_LIST_FAILED", "reason": "upstream_auth"}]
    assert not [text for text in STAGE_TEXT if text in caplog.text]


def test_without_a_per_source_list_every_source_of_the_toolbox_keeps_the_toolbox_reason(caplog):
    caplog.set_level(logging.WARNING)
    _, ctx, _ = two_sources(rpc(-32603, "upstream answered 401 (Unauthorized)"))
    assert left_out_lines(caplog) == [
        f"FA_NATIVE_TOOL_SOURCE_UNAVAILABLE agent_id=fa-agent-1 request_id=rid-1 entry={entry} "
        "code=TOOLBOX_LIST_FAILED reason=upstream_auth" for entry in ("Bing", "WebIQ")]
    assert [item.reason for item in ctx.assembly.snapshot().unavailable_sources] == ["upstream_auth", "upstream_auth"]


def test_sources_are_told_apart_by_their_full_name_and_a_bounded_one_is_still_named_once():
    receipt = context().receipt
    receipt.capture_digest = "0" * 64
    collector = NativeAssemblyCollector(receipt)
    first, second = "S" * 64 + "-one", "S" * 64 + "-two"
    # The wire field holds 64 characters; two sources sharing them are still two sources.
    assert [collector.source_unavailable("A", first, "source_error"), collector.source_unavailable("B", second, "network"),
            collector.source_unavailable("C", first, "upstream_auth")] == [True, True, False]
    assert [item.model_dump(mode="json") for item in collector.snapshot().unavailable_sources] == [
        {"entry_id": "A", "source": "S" * 64, "code": "TOOLBOX_LIST_FAILED", "reason": "source_error"},
        {"entry_id": "B", "source": "S" * 64, "code": "TOOLBOX_LIST_FAILED", "reason": "network"}]
    named = [collector.source_unavailable("A", f"src{index}", "network") for index in range(14)]
    # Past the record's 16, a new source is still named once (so logged once) and counted once.
    assert named == [True] * 14
    assert [collector.source_unavailable("A", "late", "network"), collector.source_unavailable("A", "late", "network")] \
        == [True, False]
    snapshot = collector.snapshot()
    assert len(snapshot.unavailable_sources) == 16 and snapshot.omitted_items == 1
    assert (snapshot.state, snapshot.reason) == ("partial", "summary_bound")


def kept_without_browse(reply):
    """A provider whose kept list for the default version lacks ``WebIQ.browse``; ``reply`` answers later listings
    once ``toolbox.broken`` is set."""
    toolbox = answering(Toolbox(), "tools/list", reply)
    toolbox.broken = False
    browse = next(tool for tool in toolbox.tools if tool["name"] == "WebIQ.browse")
    toolbox.tools.remove(browse)
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport)
    bind_on(provider, context(grants=(GRANT.model_copy(update={"tools": ("WebIQ-web",)}),)))
    return toolbox, provider


@pytest.mark.parametrize("reply,reason", [(lambda body: httpx.Response(503), "service_error"),
                                          (upstream_401, "upstream_auth")])
def test_a_failed_relist_for_a_missing_captured_tool_leaves_only_this_toolbox_out(caplog, reply, reason):
    caplog.set_level(logging.WARNING)
    toolbox, provider = kept_without_browse(reply)
    toolbox.broken = True
    tools = bind_on(provider, context(rid="rid-2"))
    # The kept list lacks browse, so the toolbox is listed once more; that listing fails like a first one would.
    assert methods(toolbox).count("tools/list") == 2
    assert {name: spec.description for name, spec in tools.items()} == {
        "WebIQ-web": UNAVAILABLE_DESCRIPTION, "WebIQ-browse": UNAVAILABLE_DESCRIPTION}
    with pytest.raises(ToolFailure) as left_out:
        call(tools["WebIQ-browse"], {}, "left-out")
    assert left_out.value.public_code == "NATIVE_TOOLBOX_UNAVAILABLE" and toolbox.calls == []
    assert [record.getMessage() for record in caplog.records
            if record.getMessage().startswith("FA_NATIVE_TOOL_SOURCE_UNAVAILABLE")] == [
        "FA_NATIVE_TOOL_SOURCE_UNAVAILABLE agent_id=fa-agent-1 request_id=rid-2 entry=WebIQ "
        f"code=TOOLBOX_LIST_FAILED reason={reason}"]


def test_a_relist_that_still_lacks_a_captured_tool_refuses_the_turn(caplog):
    caplog.set_level(logging.WARNING)
    toolbox, provider = kept_without_browse(lambda body: httpx.Response(503))
    with pytest.raises(Problem) as missing:
        bind_on(provider, context(rid="rid-2"))
    assert missing.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert methods(toolbox).count("tools/list") == 2 and toolbox.calls == []
    assert ("NATIVE_TOOLBOX_FAILED method=bind stage=tool_list code=CAPABILITY_UNAVAILABLE "
            "reason=captured_tool_missing;listed=3;label_matches=2") in [record.getMessage() for record in caplog.records]
    assert "FA_NATIVE_TOOL_SOURCE_UNAVAILABLE" not in caplog.text
