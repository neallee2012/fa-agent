"""Pins for toolbox_provider paths the other toolbox tests leave unexercised (bind's call guard and warm's outcomes).

Offline: the toolbox is test_native_toolbox.Toolbox over an httpx transport.
"""

import asyncio
import dataclasses
import json
import logging

import httpx
import pytest

from shared_context import native_toolbox
from shared_context.behavior_capabilities import TOOLBOX_OUTCOME_UNKNOWN, available_grants
from shared_context.behavior_native_turn_wire import NativePrewarmToolbox
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure
from shared_context.native_capability_tools import REFUSED
from shared_context.tool_catalog import build_catalog, parse_entry_file
from test_connector_consent import not_connected
from test_native_prewarm import Gated, bind as bind_names
from test_native_toolbox import ENDPOINT, GRANT, SCHEMA, Toolbox, bind, bind_on, call, context, methods
from test_native_toolbox_unavailable import prewarm_waits

ORG = NativePrewarmToolbox(toolbox="fa-org-tools")
# Another source in the same project toolbox (sorted after WebIQ), so both grants share one toolbox version.
XTRA = available_grants(build_catalog([parse_entry_file("xtra-toolbox.json", json.dumps({"mcpServers": {"Xtra": {
    "type": "toolbox", "toolbox": "fa-org-tools", "server_label": "Xtra", "tools": ["web"]}}}).encode())]))["Xtra"]
META = {"tool_configuration": {"type": "mcp", "server_label": "WebIQ", "require_approval": "never"}}
DENIED = Problem(409, "NATIVE_HITL_HUMAN_DENIED", "the owner declined")


@pytest.fixture(autouse=True)
def platform(monkeypatch):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(native_toolbox, "_CALLS", native_toolbox._Calls())
    monkeypatch.setattr(native_toolbox, "_DECLINED", set())
    # The card's own wire validation is test_connector_consent's; here only its fields are read.
    monkeypatch.setattr(native_toolbox, "ConnectorConsent", lambda **fields: fields)


class Answering(Toolbox):
    """The toolbox, with each ``method`` request answered by ``answer(body)`` while ``answer`` is set."""

    def __init__(self, method, answer=None, tools=None):
        self.method, self.answer = method, answer
        super().__init__(tools)

    def handle(self, request):
        if request.method == "POST" and self.answer is not None:
            body = json.loads(request.content)
            if body["method"] == self.method:
                self.requests.append((request, body))
                return self.answer(body)
        return super().handle(request)


class Hitl:
    """Native HITL as the toolbox sees it: each Connect card takes the next answer (None is a resume), then the last."""

    def __init__(self, *answers):
        self.cards, self.answers, self.approvals = [], list(answers), {}

    def consent_call(self, tool_call_id):
        return tool_call_id

    async def connect(self, consent):
        self.cards.append(consent)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if answer is not None:
            raise answer

    def set_toolbox_approval(self, name, approval):
        self.approvals[name] = approval

    async def invoke(self, packet, handler):
        return await handler(packet)


def owner_turn(hitl, grants=(GRANT,), rid="rid-1"):
    ctx = dataclasses.replace(context(grants=grants, rid=rid), hitl=hitl)
    ctx.receipt.human_origin = True
    return ctx


def agent_token():
    return "agent-token"


def test_a_call_whose_arguments_are_not_an_object_is_refused_and_sends_nothing():
    toolbox = Toolbox()
    web = bind(toolbox, context())["WebIQ-web"][0]
    for arguments in ("fa", ["fa"], None):
        with pytest.raises(ToolFailure) as refused:
            call(web, arguments, "call-bad")
        assert str(refused.value) == "Tool arguments must be a JSON object." and refused.value.public_code == REFUSED
    assert toolbox.calls == []
    # The refusal claimed nothing: the same call id with an object still runs once.
    call(web, {"search_query": "fa"}, "call-bad")
    assert len(toolbox.calls) == 1


def test_a_second_prewarm_of_a_version_still_listing_counts_listing_and_lists_nothing(caplog):
    caplog.set_level(logging.WARNING)
    toolbox = Toolbox()
    gated = Gated(toolbox)
    provider = native_toolbox.toolbox_provider(transport=gated)

    async def run():
        gated.listing, gated.release = asyncio.Event(), asyncio.Event()
        first = asyncio.ensure_future(provider.warm(agent_token, (ORG,)))
        await gated.listing.wait()
        await provider.warm(agent_token, (NativePrewarmToolbox(toolbox="fa-org-tools", version="1"),))
        gated.release.set()
        await first
    asyncio.run(run())
    assert methods(toolbox).count("tools/list") == 1
    assert "FA_NATIVE_PREWARM toolboxes=1 listed=0 kept=0 listing=1 failed=0" in caplog.text
    assert "FA_NATIVE_PREWARM toolboxes=1 listed=1 kept=0 listing=0 failed=0" in caplog.text


def test_a_prewarm_that_raises_an_unexpected_error_logs_its_type_and_counts_a_failure(caplog):
    caplog.set_level(logging.WARNING)
    toolbox = Toolbox()

    def broken():
        raise RuntimeError("token source broke")
    asyncio.run(native_toolbox.toolbox_provider(transport=toolbox.transport).warm(broken, (ORG,)))
    assert "FA_NATIVE_PREWARM_FAILED exception_type=RuntimeError" in caplog.text
    assert "FA_NATIVE_PREWARM toolboxes=1 listed=0 kept=0 listing=0 failed=1" in caplog.text
    assert "token source broke" not in caplog.text and toolbox.gets == []


def test_a_prewarm_failure_that_is_a_problem_is_counted_without_an_exception_log(caplog):
    caplog.set_level(logging.WARNING)
    toolbox = Toolbox()
    toolbox.get_reply = lambda request: httpx.Response(503)
    asyncio.run(native_toolbox.toolbox_provider(transport=toolbox.transport).warm(agent_token, (ORG,)))
    assert "FA_NATIVE_PREWARM_FAILED" not in caplog.text
    assert "FA_NATIVE_PREWARM toolboxes=1 listed=0 kept=0 listing=0 failed=1" in caplog.text


def test_a_declined_card_names_the_first_grant_and_is_not_shown_again_in_that_conversation():
    toolbox = Answering("tools/list", not_connected)
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport)
    hitl = Hitl(DENIED)
    for rid in ("rid-1", "rid-2"):
        tools = bind_on(provider, owner_turn(hitl, (GRANT, XTRA), rid))
        assert all(spec.description.startswith("Not connected for this assistant") for spec in tools.values())
    assert len(hitl.cards) == 1
    card = hitl.cards[0]
    assert (card["toolbox_name"], card["server_label"], card["step"], card["need"]) == (
        "fa-org-tools", "WebIQ", "tools/list", "consent")
    assert hitl.approvals == {"WebIQ-web": "never", "WebIQ-browse": "never", "Xtra-web": "never"}


def test_a_call_still_not_connected_after_its_one_resume_is_not_asked_or_sent_again():
    toolbox = Answering("tools/call", not_connected)
    hitl = Hitl(None, DENIED)
    web = bind_on(native_toolbox.toolbox_provider(transport=toolbox.transport), owner_turn(hitl))["WebIQ-web"]
    with pytest.raises(ToolFailure) as failed:
        call(web, {"search_query": "fa"}, "call-1")
    assert failed.value.public_code == native_toolbox.CONNECTOR_NOT_CONNECTED
    assert [card["step"] for card in hitl.cards] == ["tools/call"]
    assert methods(toolbox).count("tools/call") == 2


def test_a_listed_tool_without_an_approval_setting_binds_as_never_with_or_without_hitl():
    tools = [{"name": "WebIQ___web", "inputSchema": SCHEMA}, {"name": "WebIQ.browse", "inputSchema": SCHEMA, "_meta": {}}]
    hitl = Hitl(None)
    assert set(bind_on(native_toolbox.toolbox_provider(transport=Toolbox(tools).transport), owner_turn(hitl))) == {
        "WebIQ-web", "WebIQ-browse"}
    assert hitl.approvals == {"WebIQ-web": "never", "WebIQ-browse": "never"}
    assert set(bind(Toolbox(tools), context())) == {"WebIQ-web", "WebIQ-browse"}


def test_a_call_failure_with_an_unexpected_code_is_an_unknown_outcome(caplog):
    caplog.set_level(logging.WARNING)
    broken = []

    def token():
        if broken:
            raise Problem(503, "SOMETHING_ELSE", "not a toolbox code")
        return "agent-token"
    toolbox = Toolbox()
    web = bind(toolbox, dataclasses.replace(context(), model_token_provider=token))["WebIQ-web"][0]
    broken.append(True)
    with pytest.raises(ToolFailure) as failed:
        call(web, {"search_query": "fa"}, "call-1")
    assert failed.value.public_code == TOOLBOX_OUTCOME_UNKNOWN and toolbox.calls == []
    assert f"code={native_toolbox.UNKNOWN} read_only=false" in caplog.text


def test_a_grant_timeout_bounds_each_call_and_the_default_is_two_minutes():
    def timed(body):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": []}})
    timed_grant = GRANT.model_copy(update={"toolbox": GRANT.toolbox.model_copy(update={"timeout_ms": 2500})})
    for grant, expected in ((timed_grant, 2.5), (GRANT, 120.0)):
        toolbox = Answering("tools/call", timed)
        call(bind(toolbox, context(grants=(grant,)))["WebIQ-web"][0], {"search_query": "fa"}, f"call-{expected}")
        sent = [request for request, body in toolbox.requests if body["method"] == "tools/call"]
        assert len(sent) == 1 and sent[0].extensions["timeout"]["read"] == expected


def test_a_failed_second_listing_leaves_out_every_grant_on_that_version():
    listed = [{"name": "WebIQ___web", "inputSchema": SCHEMA, "_meta": META},
              {"name": "Xtra___web", "inputSchema": SCHEMA, "_meta": META}]
    toolbox = Answering("tools/list", tools=listed)
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport)
    assert set(bind_on(provider, context(grants=(XTRA,)))) == {"Xtra-web"}
    # The kept list lacks WebIQ.browse, so the next turn lists once more, and that listing fails.
    toolbox.answer = lambda body: httpx.Response(503)
    tools = bind_on(provider, context(grants=(GRANT, XTRA), rid="rid-2"))
    assert set(tools) == {"WebIQ-web", "WebIQ-browse", "Xtra-web"}
    assert all(spec.description.startswith("Unavailable this turn") for spec in tools.values())
    assert methods(toolbox).count("tools/list") == 2



class Held(httpx.AsyncBaseTransport):
    """The toolbox, with each ``tools/list`` held until the test releases that one listing."""

    def __init__(self, toolbox):
        self.toolbox, self.held, self.arrived = toolbox, [], None

    async def handle_async_request(self, request):
        await request.aread()
        if request.method == "POST" and json.loads(request.content)["method"] == "tools/list":
            release = asyncio.Event()
            self.held.append(release)
            self.arrived.set()
            await release.wait()
        return self.toolbox.handle(request)


def test_a_turn_after_another_turn_listed_binds_from_the_kept_list_without_waiting_for_a_later_prewarm(monkeypatch):
    toolbox = Toolbox()
    held = Held(toolbox)
    provider = native_toolbox.toolbox_provider(transport=held)
    waiting = prewarm_waits(monkeypatch)

    async def run():
        held.arrived = asyncio.Event()
        first = asyncio.ensure_future(bind_names(provider, context()))
        await held.arrived.wait()
        held.arrived.clear()
        # A prewarm starts while the first turn is still listing: the version is unlisted, so it lists too.
        warming = asyncio.ensure_future(provider.warm(agent_token, (ORG,)))
        await held.arrived.wait()
        held.held[0].set()
        assert await first == {"WebIQ-web", "WebIQ-browse"}
        # The first turn's list is kept while the prewarm still lists: the next turn binds from it at once.
        later = asyncio.ensure_future(bind_names(provider, context(rid="rid-2")))
        await asyncio.wait((later, asyncio.ensure_future(waiting.wait())), return_when=asyncio.FIRST_COMPLETED)
        assert later.done() and not waiting.is_set()
        held.held[1].set()
        await warming
        return await later
    assert asyncio.run(run()) == {"WebIQ-web", "WebIQ-browse"}
    assert methods(toolbox).count("tools/list") == 2
