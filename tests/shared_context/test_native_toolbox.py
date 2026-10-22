"""N5: organization connectors through the Foundry project toolbox (catalog, default-off, legacy refusal, VM provider).

Offline only: the toolbox is an httpx MockTransport that speaks the JSON-RPC shape of the official BYO sample.
"""

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from shared_context import native_toolbox
from shared_context.behavior_agent_template import TemplateCapabilityOption
from shared_context.behavior_capabilities import (
    TOOLBOX_NOT_RUN, TOOLBOX_OUTCOME_UNKNOWN, CapabilityCapture, CapabilityGrant, McpToolResult, available_grants,
    enabled_toolboxes, load_runtime_grants,
)
from shared_context.behavior_capability_prompt import addendum, session_needs
from shared_context.behavior_sdk_state import NativeCheckpointScope
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure
from shared_context.native_capabilities import NativeCapabilityContext, bind_capabilities, selected_providers
from shared_context.native_capability_tools import REFUSED, UNKNOWN
from shared_context.native_sdk import native_capability_providers
from shared_context.native_turns import NativeTurnControl
from shared_context.tool_catalog import CatalogError, build_catalog, load_catalog, parse_entry_file
from test_behavior_assistant import assistant  # noqa: F401
from test_capability_calls import WEBIQ
from test_instruction_flow import instruction_models, runtime_path  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401

CATALOG_DIR = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc" / "tool_catalog"
ENDPOINT = "https://acct.services.ai.azure.com/api/projects/proj"
TOOLBOX = parse_entry_file("webiq-toolbox.json", json.dumps({"mcpServers": {"WebIQ": {
    "type": "toolbox", "toolbox": "fa-org-tools", "server_label": "WebIQ", "tools": ["web", "browse"]}}}).encode())
GRANT = available_grants(build_catalog([TOOLBOX]))["WebIQ"]
SCHEMA = {"type": "object", "properties": {"search_query": {"type": "string"}}}


def test_a_toolbox_entry_names_its_project_toolbox_and_label_but_no_credential():
    assert TOOLBOX.execution == "foundry-toolbox" and TOOLBOX.tool_names == ("WebIQ-web", "WebIQ-browse")
    assert GRANT.kind == "mcp-toolbox" and GRANT.toolbox == TOOLBOX.config and GRANT.launch is None
    assert (GRANT.toolbox.toolbox, GRANT.toolbox.server_label) == ("fa-org-tools", "WebIQ")
    shipped = load_catalog(CATALOG_DIR)
    assert shipped.entry("WebIQ") == TOOLBOX and shipped.entry("WebIQ-MCP") is None
    for bad in ({"toolbox": "../x"}, {"server_label": "a.b"}, {"headers": {"x-apikey": "${secret:k}"}},
                {"toolbox": None}):
        config = {"type": "toolbox", "toolbox": "fa-org-tools", "server_label": "WebIQ", "tools": ["web"], **bad}
        config = {key: value for key, value in config.items() if value is not None}
        with pytest.raises(CatalogError):
            parse_entry_file("bad.json", json.dumps({"mcpServers": {"WebIQ": config}}).encode())
    for kind, extra in (("mcp-toolbox", {}), ("mcp-api", {"toolbox": TOOLBOX.config})):
        with pytest.raises(ValueError):
            CapabilityGrant(entry_id="WebIQ", entry_sha256=GRANT.entry_sha256, kind=kind, tools=GRANT.tools,
                            vision=False, **extra)
    assert TemplateCapabilityOption(entry_id="WebIQ", entry_sha256=GRANT.entry_sha256, kind="mcp-toolbox",
                                    tools=GRANT.tools, vision=False).kind == "mcp-toolbox"


def test_toolbox_entries_stay_unloaded_until_the_deployment_names_them(monkeypatch):
    monkeypatch.setenv("FA_TOOL_CATALOG_DIR", str(CATALOG_DIR))
    monkeypatch.delenv("FA_CAPABILITY_BUILTINS", raising=False)
    monkeypatch.delenv("FA_CAPABILITY_TOOLBOX_ENTRIES", raising=False)
    off = load_runtime_grants()
    assert sorted(off) == ["playwright"]
    monkeypatch.setenv("FA_CAPABILITY_TOOLBOX_ENTRIES", "WebIQ")
    on = load_runtime_grants()
    assert on["WebIQ"] == GRANT and on["playwright"] == off["playwright"] and "WebIQ-MCP" not in on
    for names in ("WebIQ-MCP", "playwright", "Missing", "WebIQ,WebIQ"):
        monkeypatch.setenv("FA_CAPABILITY_TOOLBOX_ENTRIES", names)
        with pytest.raises(Problem) as error:
            load_runtime_grants()
        assert error.value.body["error"]["code"] == "CAPABILITY_CONFIGURATION"
    with pytest.raises(Problem):
        enabled_toolboxes(None, ("WebIQ",))


def test_a_toolbox_grant_is_a_custom_tool_set_with_its_own_guidance():
    capture = CapabilityCapture(head_selection_id=None, grants=(GRANT,))
    needs = session_needs(capture)
    assert needs.custom_tools == GRANT.tools and needs.mcp_servers == {} and needs.mcp_tools == ()
    text = addendum(capture.grants)
    assert "Foundry project toolbox" in text and "WebIQ-web, WebIQ-browse" in text


def test_the_job_path_refuses_a_toolbox_grant_before_the_sdk_starts(assistant, monkeypatch):
    from shared_context import behavior_assistant_hosted as hosted
    from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES
    from test_capability_capture import CATALOG, FILES, enabled
    from test_conversation_profile_selection import select, template
    from test_instruction_prompt import submit
    from shared_context.behavior_history_prompt import HISTORY_PROFILE
    catalog = build_catalog([*CATALOG.entries, TOOLBOX])
    h, _ = enabled(assistant, monkeypatch, grants=available_grants(catalog, ("fa-desktop-v1",)), catalog=catalog,
                   tools=HISTORY_CAPABILITIES, image_tools=False)
    choice, _ = select(h, request_id="choose-toolbox", previous=None, value=template(
        HISTORY_PROFILE, user_text_file_profile=FILES, capabilities=(GRANT.ref,)))
    assert choice.status_code == 200, choice.text
    started, refusals = [], []

    async def never(**options):
        started.append(options)
        raise AssertionError("the job path must not start the SDK for a toolbox grant")
    monkeypatch.setattr(hosted, "run_copilot", never)
    original = hosted.AssistantSession._run_turn

    async def observed(self, work):
        try:
            return await original(self, work)
        except Problem as error:
            refusals.append(error.body["error"])
            raise
    monkeypatch.setattr(hosted.AssistantSession, "_run_turn", observed)
    accepted, _ = submit(h, "toolbox-on-job-path", "Search the web.")
    try:
        h.runtime.controller.step(accepted["binding_id"])
    except Problem:
        pass
    assert started == [] and [(item["code"], item["capability"]) for item in refusals] == [
        ("CAPABILITY_UNAVAILABLE", "WebIQ")]


class Toolbox:
    """The toolbox: the official toolbox GET and its MCP proxy (initialize, initialized, tools/list, tools/call)."""

    def __init__(self, tools=None, *, approval="never"):
        meta = {"tool_configuration": {"type": "mcp", "server_label": "WebIQ", "require_approval": approval}}
        self.tools = tools if tools is not None else [
            {"name": "WebIQ___web", "description": "Search the web", "inputSchema": SCHEMA, "_meta": meta},
            {"name": "WebIQ.browse", "description": "Browse a page", "inputSchema": {"type": "object"}, "_meta": meta},
            {"name": "WebIQ___news", "description": "Not captured", "inputSchema": SCHEMA, "_meta": meta},
            {"name": "Other___web", "description": "Another source", "inputSchema": SCHEMA, "_meta": meta},
        ]
        self.requests, self.calls, self.reply = [], [], None
        # Sessions the server ended: any later request on them gets 404 before anything runs (MCP Streamable HTTP).
        self.ended, self.opened, self.live = set(), 0, set()
        # Toolbox versions: ``default`` is what the toolbox GET and the consumer endpoint serve.
        self.default, self.versions, self.gets, self.get_reply = "1", {}, [], None
        self.transport = httpx.MockTransport(self.handle)

    def listed(self, request):
        path = request.url.path
        version = path.split("/versions/")[1].split("/")[0] if "/versions/" in path else self.default
        return self.versions.get(version, self.tools)

    def handle(self, request):
        if request.method == "GET":
            self.gets.append(request)
            if self.get_reply is not None:
                return self.get_reply(request)
            return httpx.Response(200, json={"id": "tb-1", "name": "fa-org-tools", "default_version": self.default})
        body = json.loads(request.content)
        self.requests.append((request, body))
        method, session = body["method"], request.headers.get("mcp-session-id")
        if method == "initialize":
            if session is not None:
                return httpx.Response(400)
            self.opened += 1
            session = "toolbox-session" if self.opened == 1 else f"toolbox-session-{self.opened}"
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"serverInfo": {}}},
                                  headers={"mcp-session-id": session})
        if method == "notifications/initialized":
            self.live.add(session)
            return httpx.Response(202)
        if session in self.ended:
            return httpx.Response(404)
        if session not in self.live:
            # MCP lifecycle: no request before the session's initialized notification.
            return httpx.Response(400)
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"tools": self.listed(request)}})
        self.calls.append(body["params"])
        if self.reply is not None:
            return self.reply(body)
        result = {"content": [{"type": "text", "text": "found"},
                              {"type": "resource", "resource": {"uri": "https://e.test", "text": "cited"}}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def context(grants=(GRANT,), tokens=None, rid="rid-1"):
    tokens = tokens if tokens is not None else []

    def token():
        tokens.append(f"agent-token-{len(tokens)}")
        return tokens[-1]
    selected = CapabilityCapture(head_selection_id=None, grants=tuple(grants))
    capture = SimpleNamespace(capabilities=selected, tools=selected, template=SimpleNamespace(skills=()))
    receipt = SimpleNamespace(capture=capture, request_id=rid, agent_id="fa-agent-1", conversation_id="cid-1")
    return NativeCapabilityContext(
        receipt=receipt, control=NativeTurnControl(), root=Path("unused"), turn_root=Path("unused"),
        scope=NativeCheckpointScope(agent_id="fa-agent-1", conversation_id="cid-1", sdk_session_id="sdk-1"),
        runtime=None, model=None, api_url="https://api.example.test", api_token_provider=None,
        model_token_provider=token)


def bind(toolbox, ctx):
    async def run():
        async with AsyncExitStack() as stack:
            return await bind_capabilities(ctx, (native_toolbox.toolbox_provider(transport=toolbox.transport),), stack)
    return {spec.name: (spec, binding.result_schemas[spec.name]) for binding in asyncio.run(run()) for spec in binding.tools}


def call(spec, arguments, call_id, *, session="sdk-1"):
    return asyncio.run(spec.handler({"session_id": session, "tool_call_id": call_id, "tool_name": spec.name,
                                     "arguments": arguments}))


@pytest.fixture(autouse=True)
def platform(monkeypatch):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(native_toolbox, "_CALLS", native_toolbox._Calls())


def test_only_captured_tools_bind_and_every_request_uses_a_fresh_agent_token():
    toolbox, tokens = Toolbox(), []
    tools = bind(toolbox, context(tokens=tokens))
    assert {name: schema for name, (_, schema) in tools.items()} == {
        "WebIQ-web": McpToolResult, "WebIQ-browse": McpToolResult}
    assert tools["WebIQ-browse"][0].parameters == {"type": "object", "properties": {}}
    web = tools["WebIQ-web"][0]
    assert web.parameters == SCHEMA and web.description == "Search the web"
    output = McpToolResult.model_validate_json(call(web, {"search_query": "fa"}, "call-1").text)
    assert (output.entry_id, output.tool, output.text, output.is_error) == ("WebIQ", "web", "found\ncited", False)
    assert toolbox.calls == [{"name": "WebIQ___web", "arguments": {"search_query": "fa"}}]
    requests = [request for request, _ in toolbox.requests]
    # The official toolbox GET reads the default version; MCP then lists and calls exactly that version.
    assert [str(request.url) for request in toolbox.gets] == [ENDPOINT + "/toolboxes/fa-org-tools?api-version=v1"]
    assert "foundry-features" not in toolbox.gets[0].headers
    assert {str(request.url) for request in requests} == {
        ENDPOINT + "/toolboxes/fa-org-tools/versions/1/mcp?api-version=v1"}
    assert [request.headers["authorization"] for request in (*toolbox.gets, *requests)] == [f"Bearer {item}" for item in tokens]
    assert len(set(tokens)) == len(requests) + len(toolbox.gets) == 5
    assert all(request.headers["foundry-features"] == "Toolboxes=V1Preview" for request in requests)
    assert "mcp-session-id" not in requests[0].headers
    assert all(request.headers["mcp-session-id"] == "toolbox-session" for request in requests[1:])
    assert "agent-token" not in output.text


def test_version_pinned_toolbox_entry_uses_immutable_version_url():
    toolbox = Toolbox()
    versioned = GRANT.model_copy(update={"toolbox": GRANT.toolbox.model_copy(update={"version": "ver_1"})})
    bind(toolbox, context(grants=(versioned,)))
    assert {str(request.url) for request, _ in toolbox.requests} == {
        ENDPOINT + "/toolboxes/fa-org-tools/versions/ver_1/mcp?api-version=v1"}
    # A pinned version is immutable: no default-version read.
    assert toolbox.gets == []


def test_one_sdk_call_is_sent_once_and_an_unknown_or_refused_outcome_is_never_resent():
    toolbox = Toolbox()
    web = bind(toolbox, context())["WebIQ-web"][0]
    first = call(web, {"search_query": "fa"}, "call-1")
    assert call(web, {"search_query": "fa"}, "call-1").text == first.text and len(toolbox.calls) == 1
    with pytest.raises(ToolFailure) as conflict:
        call(web, {"search_query": "other"}, "call-1")
    assert conflict.value.public_code == REFUSED and len(toolbox.calls) == 1
    cases = [(lambda body: httpx.Response(503), TOOLBOX_OUTCOME_UNKNOWN),
             (lambda body: httpx.Response(403), TOOLBOX_NOT_RUN),
             (lambda body: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                                     "error": {"code": -32602, "message": "bad"}}), TOOLBOX_NOT_RUN),
             (lambda body: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                                     "error": {"code": -32603, "message": "x"}}), TOOLBOX_OUTCOME_UNKNOWN)]
    for index, (reply, code) in enumerate(cases):
        toolbox.reply = reply
        sent = len(toolbox.calls)
        for _ in range(2):
            with pytest.raises(ToolFailure) as failed:
                call(web, {"search_query": "fa"}, f"failed-{index}")
            assert failed.value.public_code == code
        assert len(toolbox.calls) == sent + 1
    toolbox.reply = lambda body: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
        "isError": True, "content": [{"type": "text", "text": "bad query"}]}})
    error = McpToolResult.model_validate_json(call(web, {"search_query": ""}, "tool-error").text)
    assert error.is_error and error.text == "bad query"


def test_a_stream_reply_is_read_and_a_stopped_turn_sends_nothing():
    toolbox = Toolbox()
    ctx = context()
    web = bind(toolbox, ctx)["WebIQ-web"][0]
    toolbox.reply = lambda body: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
        'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        f'event: message\ndata: {{"jsonrpc":"2.0","id":{body["id"]},"result":'
        '{"content":[{"type":"text","text":"streamed"}]}}\n\n').encode())
    assert McpToolResult.model_validate_json(call(web, {"search_query": "fa"}, "sse").text).text == "streamed"
    sent = len(toolbox.calls)
    with pytest.raises(ToolFailure):
        call(web, {"search_query": "fa"}, "other-session", session="old-sdk")
    ctx.control.cancelled = True
    with pytest.raises(ToolFailure):
        call(web, {"search_query": "fa"}, "after-stop")
    assert len(toolbox.calls) == sent


@pytest.mark.parametrize("broken", ["missing", "approval", "endpoint", "refused"])
def test_binding_refuses_before_the_sdk_when_the_toolbox_differs_from_the_capture(monkeypatch, broken):
    toolbox = Toolbox()
    if broken == "missing":
        toolbox.tools = [tool for tool in toolbox.tools if tool["name"] != "WebIQ.browse"]
    elif broken == "approval":
        toolbox = Toolbox(approval="always")
    elif broken == "endpoint":
        monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "http://acct.example.test/api/projects/proj")
    else:
        # The toolbox's own definite answer about this Agent (403) is not a listing that merely failed.
        toolbox.transport = httpx.MockTransport(lambda request: httpx.Response(403))
    with pytest.raises(Problem) as refused:
        bind(toolbox, context())
    assert refused.value.body["error"]["code"] in {"CAPABILITY_UNAVAILABLE", "NATIVE_EXECUTION_UNAVAILABLE", REFUSED}
    assert toolbox.calls == []


def test_the_registered_toolbox_provider_is_the_single_consumer_of_a_toolbox_grant():
    providers = native_capability_providers("https://api.example.test", None)
    selected = CapabilityCapture(head_selection_id=None, grants=(GRANT, WEBIQ_GRANT))
    capture = SimpleNamespace(capabilities=selected, tools=selected, template=SimpleNamespace(skills=()))
    chosen = {grant.entry_id: provider for provider, grants in selected_providers(capture, providers) for grant in grants}
    assert chosen["WebIQ"] is not chosen["WebSearch-MCP"]
    assert [provider.accepts(GRANT) for provider in providers].count(True) == 1


WEBIQ_GRANT = available_grants(build_catalog([WEBIQ]))["WebSearch-MCP"]


CANARY = "CANARY-BODY-7f3e"


def reply_for(method, response):
    """A toolbox that answers ``method`` with ``response`` and behaves normally otherwise."""
    toolbox = Toolbox()
    original = toolbox.handle

    def handle(request):
        if request.method == "GET":
            return original(request)
        body = json.loads(request.content)
        if body["method"] == method:
            toolbox.requests.append((request, body))
            return response(body)
        return original(request)
    toolbox.transport = httpx.MockTransport(handle)
    return toolbox


@pytest.mark.parametrize("method,response,stage,reason,unavailable", [
    ("initialize", lambda body: httpx.Response(200, headers={"content-type": "text/html"},
                                               content=f"<html>{CANARY}</html>".encode()),
     "response_decode", "http_status=200;content=html;bytes=", "response_invalid"),
    ("initialize", lambda body: httpx.Response(200, json={"jsonrpc": "2.0", "id": 99, "result": {"x": CANARY}}),
     "response_decode", "jsonrpc_invalid", "response_invalid"),
    ("tools/list", lambda body: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}}),
     "tool_list", "tools_not_list", "response_invalid"),
    ("tools/list", lambda body: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": [CANARY]}),
     "response_decode", "result_missing", "response_invalid"),
    ("initialize", lambda body: httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"x"),
     "response_headers", "encoding_unsupported", "response_invalid"),
    ("tools/list", lambda body: httpx.Response(502, text=CANARY), "response_status", "http_status=502", "service_error"),
    ("initialize", lambda body: (_ for _ in ()).throw(httpx.ConnectError(CANARY)),
     "request", "exception_type=ConnectError", "network"),
    ("tools/list", lambda body: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                                          "error": {"code": -32603, "message": CANARY}}),
     "rpc_error", "rpc_code=-32603", "source_error"),
])
def test_every_failed_listing_logs_a_fixed_reason_and_never_content(caplog, method, response, stage, reason,
                                                                   unavailable):
    caplog.set_level(logging.WARNING)
    tokens = []
    # A listing that failed leaves only this toolbox out of the turn (Neal 2026-10-10); the bind itself succeeds.
    tools = bind(reply_for(method, response), context(tokens=tokens))
    assert set(tools) == {"WebIQ-web", "WebIQ-browse"}
    lines = [record.getMessage() for record in caplog.records]
    assert any(line.startswith(f"NATIVE_TOOLBOX_FAILED method={method} stage={stage} code={UNKNOWN}")
               and reason in line for line in lines), lines
    assert (f"FA_NATIVE_TOOL_SOURCE_UNAVAILABLE agent_id=fa-agent-1 request_id=rid-1 entry=WebIQ "
            f"code=TOOLBOX_LIST_FAILED reason={unavailable}") in lines
    assert not any(line.startswith("FA_NATIVE_CAPABILITY_BIND_FAILED") for line in lines), lines
    assert CANARY not in caplog.text and not any(token in caplog.text for token in tokens)


def test_bind_refusals_and_token_failures_log_their_reason(caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    cases = [
        (Toolbox([]), context(), "CAPABILITY_UNAVAILABLE", "reason=captured_tool_missing;listed=0;label_matches=0"),
        (Toolbox(approval="always"), context(), "CAPABILITY_UNAVAILABLE", "reason=approval_required"),
    ]
    for toolbox, ctx, code, reason in cases:
        caplog.clear()
        with pytest.raises(Problem) as failed:
            bind(toolbox, ctx)
        assert failed.value.body["error"]["code"] == code
        assert reason in caplog.text and f"entries=WebIQ code={code}" in caplog.text, caplog.text
    # No identity token for the toolbox: it cannot be listed, so only this toolbox is left out of the turn.
    caplog.clear()
    blank = context()
    blank.model_token_provider = lambda: ""
    assert set(bind(Toolbox(), blank)) == {"WebIQ-web", "WebIQ-browse"}
    assert "stage=token code=" + UNKNOWN + " reason=token_invalid" in caplog.text
    assert "code=TOOLBOX_LIST_FAILED reason=identity_unavailable" in caplog.text
    caplog.clear()
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT")
    with pytest.raises(Problem):
        bind(Toolbox(), context())
    assert "stage=endpoint code=NATIVE_EXECUTION_UNAVAILABLE reason=endpoint_missing" in caplog.text


def test_default_transport_matches_the_model_call_and_the_turn_call_id_is_forwarded(monkeypatch, caplog):
    import azure.ai.agentserver.core as core
    from azure.ai.agentserver.core import FoundryAgentRequestContext
    caplog.set_level(logging.WARNING)
    toolbox, built = Toolbox(), []

    def transport(**options):
        built.append(options)
        return toolbox.transport
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    monkeypatch.setattr(core, "get_request_context", lambda: FoundryAgentRequestContext(call_id="turn-call-1"))

    async def run():
        async with AsyncExitStack() as stack:
            return await bind_capabilities(context(), (native_toolbox.toolbox_provider(),), stack)
    asyncio.run(run())
    # Like behavior_model_direct: an explicit retries=0 transport keeps httpx's default TLS trust settings.
    assert built and all(options == {"retries": 0} for options in built)
    assert all(request.headers["x-agent-foundry-call-id"] == "turn-call-1"
               and request.headers["authorization"].startswith("Bearer agent-token-")
               for request, _ in toolbox.requests)
    toolbox.tools = []
    with pytest.raises(Problem):
        asyncio.run(run())
    broken = reply_for("initialize", lambda body: httpx.Response(502))
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **options: broken.transport)
    asyncio.run(run())
    assert "reason=http_status=502;call_id=present" in caplog.text and "turn-call-1" not in caplog.text


def bind_on(provider, ctx):
    """One turn's bind against a provider kept across turns, as the Hosted process keeps it."""
    async def run():
        async with AsyncExitStack() as stack:
            return await bind_capabilities(ctx, (provider,), stack)
    return {spec.name: spec for binding in asyncio.run(run()) for spec in binding.tools}


def methods(toolbox, start=0):
    return [body["method"] for _, body in toolbox.requests[start:]]


def test_a_later_turn_binds_from_the_kept_list_and_calls_the_kept_session_with_its_own_call_id(monkeypatch):
    import azure.ai.agentserver.core as core
    from azure.ai.agentserver.core import FoundryAgentRequestContext
    toolbox = Toolbox()
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport)
    monkeypatch.setattr(core, "get_request_context", lambda: FoundryAgentRequestContext(call_id="turn-call-1"))
    first = bind_on(provider, context())
    assert methods(toolbox) == ["initialize", "notifications/initialized", "tools/list"]
    call(first["WebIQ-web"], {"search_query": "fa"}, "call-1")
    monkeypatch.setattr(core, "get_request_context", lambda: FoundryAgentRequestContext(call_id="turn-call-2"))
    sent = len(toolbox.requests)
    second = bind_on(provider, context(rid="rid-2"))
    # The second turn's bind only reads the default version; its call reuses the session with its own call id.
    assert len(toolbox.requests) == sent and len(toolbox.gets) == 2 and second.keys() == first.keys()
    call(second["WebIQ-browse"], {}, "call-2")
    assert methods(toolbox, sent) == ["tools/call"]
    request = toolbox.requests[-1][0]
    assert (request.headers["mcp-session-id"], request.headers["x-agent-foundry-call-id"]) == (
        "toolbox-session", "turn-call-2")
    ids = [body["id"] for _, body in toolbox.requests if "id" in body]
    assert len(ids) == len(set(ids)) and toolbox.opened == 1


def approving(tools, approval):
    return [{**tool, "_meta": {"tool_configuration": {**tool["_meta"]["tool_configuration"],
                                                      "require_approval": approval}}} for tool in tools]


def test_a_list_is_kept_per_exact_version_and_a_missing_tool_is_listed_once_more():
    toolbox = Toolbox()
    browse = next(tool for tool in toolbox.tools if tool["name"] == "WebIQ.browse")
    toolbox.tools.remove(browse)
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport)

    def lists():
        return methods(toolbox).count("tools/list")
    bind_on(provider, context(grants=(GRANT.model_copy(update={"tools": ("WebIQ-web",)}),)))
    toolbox.tools.append(browse)
    # The kept list lacks browse (a version can list nothing briefly): listed once more, then kept again.
    assert set(bind_on(provider, context())) == {"WebIQ-web", "WebIQ-browse"} and lists() == 2
    bind_on(provider, context())
    assert lists() == 2 and toolbox.opened == 2
    gone = GRANT.model_copy(update={"tools": ("WebIQ-web", "WebIQ-gone")})
    with pytest.raises(Problem) as missing:
        bind_on(provider, context(grants=(gone,)))
    assert missing.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE" and lists() == 3
    # A promoted default version is a different immutable version: listed at once; going back reuses its list.
    toolbox.default = "2"
    bind_on(provider, context())
    assert lists() == 4 and {request.url.path.split("/versions/")[1].split("/")[0]
                             for request, _ in toolbox.requests[-3:]} == {"2"}
    toolbox.default = "1"
    bind_on(provider, context())
    assert lists() == 4
    versioned = GRANT.model_copy(update={"toolbox": GRANT.toolbox.model_copy(update={"version": "ver_1"})})
    gets = len(toolbox.gets)
    bind_on(provider, context(grants=(versioned,)))
    bind_on(provider, context(grants=(versioned,)))
    # A pinned version is never read or listed again.
    assert lists() == 5 and len(toolbox.gets) == gets


def test_a_promoted_default_that_requires_approval_is_refused_on_the_next_turn(caplog):
    caplog.set_level(logging.WARNING)
    toolbox = Toolbox()
    toolbox.versions = {"1": toolbox.tools, "2": approving(toolbox.tools, "always")}
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport)
    web = bind_on(provider, context())["WebIQ-web"]
    toolbox.default = "2"
    # Without native HITL an always tool is not offered at all, from the very next turn.
    with pytest.raises(Problem) as refused:
        bind_on(provider, context(rid="rid-2"))
    assert refused.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE" and "reason=approval_required" in caplog.text
    # The earlier turn's tools keep calling the version that turn checked, never the newly promoted one.
    call(web, {"search_query": "fa"}, "earlier-turn")
    assert toolbox.requests[-1][0].url.path.endswith("/toolboxes/fa-org-tools/versions/1/mcp")


def test_an_unread_default_version_never_reuses_a_kept_list(caplog):
    caplog.set_level(logging.WARNING)
    toolbox = Toolbox()
    toolbox.versions = {"1": toolbox.tools, "2": approving(toolbox.tools, "always")}
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport)
    bind_on(provider, context())
    toolbox.default = "2"
    toolbox.get_reply = lambda request: httpx.Response(503)
    sent = len(toolbox.requests)
    # The read failed: this turn lists the consumer endpoint itself (now version 2) instead of the kept version 1.
    with pytest.raises(Problem) as refused:
        bind_on(provider, context(rid="rid-2"))
    assert refused.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert methods(toolbox, sent) == ["initialize", "notifications/initialized", "tools/list"]
    assert toolbox.requests[-1][0].url.path.endswith("/toolboxes/fa-org-tools/mcp")
    assert "method=toolbox_get stage=response_status code=NATIVE_CAPABILITY_UNKNOWN reason=http_status=503" in caplog.text
    # Still unread: the turn lists the consumer endpoint and keeps nothing, and fails closed. Without native HITL the
    # tools are not offered, even though the listed setting says never (the serving version is unknown).
    toolbox.default = "1"
    with pytest.raises(Problem) as closed:
        bind_on(provider, context(rid="rid-3"))
    assert closed.value.body["error"]["reason"] == "default_version_unread"
    assert "reason=default_version_unread" in caplog.text
    assert toolbox.requests[-1][0].url.path.endswith("/toolboxes/fa-org-tools/mcp") and toolbox.calls == []
    lists = methods(toolbox).count("tools/list")
    with pytest.raises(Problem):
        bind_on(provider, context(rid="rid-4"))
    assert methods(toolbox).count("tools/list") == lists + 1
    # Unread and unlistable: only this toolbox is left out of the turn, and its tools send nothing.
    broken = native_toolbox.toolbox_provider(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    left_out = bind_on(broken, context(rid="rid-5"))
    with pytest.raises(ToolFailure) as failed:
        call(left_out["WebIQ-web"], {"search_query": "fa"}, "unlistable")
    assert failed.value.public_code == "NATIVE_TOOLBOX_UNAVAILABLE"


def test_an_ended_session_is_reopened_once_and_a_call_still_runs_once(caplog):
    caplog.set_level(logging.WARNING)
    toolbox = Toolbox()
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport)
    web = bind_on(provider, context())["WebIQ-web"]
    toolbox.ended.add("toolbox-session")
    output = McpToolResult.model_validate_json(call(web, {"search_query": "fa"}, "after-expiry").text)
    assert output.text == "found\ncited" and len(toolbox.calls) == 1
    assert methods(toolbox, 3) == ["tools/call", "initialize", "notifications/initialized", "tools/call"]
    assert ("method=tools/call stage=response_status code=NATIVE_CAPABILITY_REFUSED "
            "reason=http_status=404;session_ended;call_id=absent") in caplog.text
    web = bind_on(provider, context(rid="rid-2"))["WebIQ-web"]
    call(web, {"search_query": "fa"}, "next-turn")
    assert toolbox.requests[-1][0].headers["mcp-session-id"] == "toolbox-session-2" and toolbox.opened == 2
    # Ended again on the reopened session: a definite refusal, never a third send of that call.
    toolbox.ended.update({"toolbox-session-2", "toolbox-session-3"})
    for _ in range(2):
        with pytest.raises(ToolFailure) as refused:
            call(web, {"search_query": "fa"}, "ended-twice")
        assert refused.value.public_code == TOOLBOX_NOT_RUN
    assert len(toolbox.calls) == 2 and toolbox.opened == 3
    # Any other failure leaves its outcome unknown (not resent) and the next call opens a new session.
    toolbox.ended.clear()
    toolbox.reply = lambda body: httpx.Response(503)
    with pytest.raises(ToolFailure) as unknown:
        call(web, {"search_query": "fa"}, "unknown-call")
    assert unknown.value.public_code == TOOLBOX_OUTCOME_UNKNOWN and len(toolbox.calls) == 3
    toolbox.reply = None
    call(web, {"search_query": "fa"}, "after-unknown")
    assert len(toolbox.calls) == 4 and toolbox.opened == 5
    assert toolbox.requests[-1][0].headers["mcp-session-id"] == "toolbox-session-5"


def test_kept_toolbox_versions_are_bounded():
    toolboxes = native_toolbox._Toolboxes()
    oldest = toolboxes.get((ENDPOINT, "fa-org-tools", "0"))
    for index in range(1, native_toolbox.MAX_TOOLBOXES + 1):
        toolboxes.get((ENDPOINT, "fa-org-tools", str(index)))
    assert len(toolboxes.items) == native_toolbox.MAX_TOOLBOXES
    assert (ENDPOINT, "fa-org-tools", "0") not in toolboxes.items
    assert toolboxes.get((ENDPOINT, "fa-org-tools", "0")) is not oldest


def test_each_registered_provider_times_its_own_bind_under_a_fixed_name(caplog):
    from shared_context.native_turns import timing_id
    caplog.set_level(logging.WARNING)
    bind(Toolbox(), context())
    with pytest.raises(Problem):
        bind(Toolbox([]), context(rid="rid-2"))
    timings = [json.loads(record.getMessage().split(" ", 1)[1]) for record in caplog.records
               if record.getMessage().startswith("FA_TIMING ")]
    assert [(item["phase"], item["job_id"], item["outcome"]) for item in timings] == [
        ("native_bind_toolbox", timing_id("rid-1"), "returned"), ("native_bind_toolbox", timing_id("rid-2"), "failed")]
    names = [provider.name for provider in native_capability_providers("https://api.example.test", None)]
    assert len(set(names)) == len(names) and "other" not in names


def test_parallel_calls_after_a_session_end_share_one_reopened_session():
    toolbox = Toolbox()
    provider = native_toolbox.toolbox_provider(transport=toolbox.transport)
    ctx = context()
    delays = iter([0.03, 0.0, 0.02, 0.01, 0.0, 0.03, 0.01, 0.02] * 4)

    async def slow_token():
        await asyncio.sleep(next(delays))
        return "agent-token"
    ctx.model_token_provider = slow_token
    tools = bind_on(provider, ctx)
    toolbox.ended.add("toolbox-session")
    work = [(tools["WebIQ-web"], {"search_query": "a"}), (tools["WebIQ-browse"], {}),
            (tools["WebIQ-web"], {"search_query": "b"})]

    async def together():
        return await asyncio.gather(*(spec.handler({
            "session_id": "sdk-1", "tool_call_id": f"parallel-{index}", "tool_name": spec.name, "arguments": arguments,
        }) for index, (spec, arguments) in enumerate(work)))
    outputs = asyncio.run(together())
    # One reopened session serves every call; none is sent without a session or before its initialized notification.
    assert all(McpToolResult.model_validate_json(output.text).text == "found\ncited" for output in outputs)
    assert len(toolbox.calls) == 3 and toolbox.opened == 2
    assert {request.headers.get("mcp-session-id") for request, body in toolbox.requests
            if body["method"] == "tools/call"} == {"toolbox-session", "toolbox-session-2"}


def test_a_call_whose_session_cannot_be_opened_is_refused_without_sending_it(caplog):
    caplog.set_level(logging.WARNING)
    toolbox, refuse_open = Toolbox(), []

    def handle(request):
        if refuse_open and json.loads(request.content)["method"] == "initialize":
            return httpx.Response(503)
        return toolbox.handle(request)
    web = bind_on(native_toolbox.toolbox_provider(transport=httpx.MockTransport(handle)), context())["WebIQ-web"]
    toolbox.ended.add("toolbox-session")
    refuse_open.append(True)
    with pytest.raises(ToolFailure) as refused:
        call(web, {"search_query": "fa"}, "no-session")
    assert refused.value.public_code == TOOLBOX_NOT_RUN and toolbox.calls == []
    assert "method=tools/call stage=session code=NATIVE_CAPABILITY_REFUSED reason=session_unavailable" in caplog.text


CONSENT_URL = "https://consent.example.test/login?data=PROBE-7f3e"
REVOKED = "Failed to extract exchanged token from ConnectorGateway response"
OTHER = parse_entry_file("other-toolbox.json", json.dumps({"mcpServers": {"Other": {
    "type": "toolbox", "toolbox": "fa-other-tools", "server_label": "Other", "tools": ["web"]}}}).encode())
OTHER_GRANT = available_grants(build_catalog([OTHER]))["Other"]


def consent_error(body):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {
        "code": -32006, "message": f"User consent is required: {CONSENT_URL}"}})


def rpc_error(number, message):
    return lambda body: httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {
        "code": number, "message": message}})


def unconnected(toolbox, method, response, name="fa-org-tools"):
    """Only toolbox ``name`` answers ``method`` with ``response``; every other toolbox behaves normally."""
    original = toolbox.handle

    def handle(request):
        if request.method != "GET" and f"/toolboxes/{name}/" in request.url.path:
            body = json.loads(request.content)
            if body["method"] == method:
                toolbox.requests.append((request, body))
                return response(body)
        return original(request)
    toolbox.transport = httpx.MockTransport(handle)
    return toolbox


@pytest.mark.parametrize("method", ["initialize", "tools/list"])
def test_without_hitl_an_unconnected_toolbox_only_says_so_and_the_other_toolbox_still_binds(caplog, method):
    toolbox = unconnected(Toolbox(), method, consent_error)
    with caplog.at_level(logging.WARNING):
        tools = bind(toolbox, context(grants=(OTHER_GRANT, GRANT)))
        assert set(tools) == {"Other-web", "WebIQ-web", "WebIQ-browse"}
        assert tools["WebIQ-web"][0].description == (
            "Not connected for this assistant: WebIQ. Calling it only reports that. Do not use the VM browser, "
            "Playwright or the desktop to sign in to this service or to reach it another way; the owner connects it "
            "only from the Connect card in the workbench, in a conversation with this Agent.")
        found = json.loads(call(tools["Other-web"][0], {"search_query": "fa"}, "other-call").text)
        with pytest.raises(ToolFailure) as failed:
            call(tools["WebIQ-web"][0], {}, "unconnected-call")
    assert found["text"] == "found\ncited" and [item["name"] for item in toolbox.calls] == ["Other___web"]
    assert failed.value.public_code == "NATIVE_CONNECTOR_NOT_CONNECTED"
    assert failed.value.public_message.startswith("This service is not connected for this assistant yet")
    assert failed.value.public_message.endswith("Do not use the VM browser, Playwright or the desktop to sign in to "
                                                "this service or to reach it another way; the owner connects it only "
                                                "from the Connect card in the workbench, in a conversation with this Agent.")
    assert CONSENT_URL not in failed.value.public_message and CONSENT_URL not in caplog.text
    assert f"method={method} stage=rpc_error code=NATIVE_CAPABILITY_REFUSED reason=rpc_code=-32006;need=consent" in (
        caplog.text)


def test_without_hitl_a_call_without_consent_is_not_connected_once_and_never_resent(caplog):
    toolbox = Toolbox()
    web = bind(toolbox, context())["WebIQ-web"][0]
    toolbox.reply = consent_error
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(ToolFailure) as failed:
                call(web, {"search_query": "fa"}, "consent-call")
            assert failed.value.public_code == "NATIVE_CONNECTOR_NOT_CONNECTED"
    assert len(toolbox.calls) == 1
    assert CONSENT_URL not in caplog.text and CONSENT_URL not in failed.value.public_message
    assert "code=NATIVE_CONNECTOR_NOT_CONNECTED" in caplog.text


def test_a_revoked_connection_is_told_apart_from_a_bad_request_by_its_text_and_never_retried():
    toolbox = Toolbox()
    web = bind(toolbox, context())["WebIQ-web"][0]
    toolbox.reply = rpc_error(-32602, REVOKED)
    with pytest.raises(ToolFailure) as revoked:
        call(web, {"search_query": "fa"}, "revoked-call")
    toolbox.reply = rpc_error(-32602, "Invalid params")
    with pytest.raises(ToolFailure) as invalid:
        call(web, {"search_query": "fa"}, "invalid-call")
    assert (revoked.value.public_code, invalid.value.public_code) == (
        "NATIVE_CONNECTOR_RECONNECT_REQUIRED", "NATIVE_TOOLBOX_NOT_RUN")
    assert revoked.value.public_message.startswith("This assistant's connection to this service no longer works")
    assert "Do not use the VM browser, Playwright or the desktop to sign in" in revoked.value.public_message
    assert len(toolbox.calls) == 2
    listed = bind(unconnected(Toolbox(), "tools/list", rpc_error(-32007, "INVALID_CONFIGURATION: " + REVOKED)),
                  context())
    with pytest.raises(ToolFailure) as stub:
        call(listed["WebIQ-browse"][0], {}, "revoked-list")
    assert stub.value.public_code == "NATIVE_CONNECTOR_RECONNECT_REQUIRED"
    # Without that text, INVALID_CONFIGURATION is the toolbox's definite answer about its configuration: it refuses.
    with pytest.raises(Problem):
        bind(unconnected(Toolbox(), "tools/list", rpc_error(-32007, "INVALID_CONFIGURATION")), context())


def test_the_retired_probe_flag_is_dropped_on_the_next_sync():
    from shared_context.behavior_agent_versions import _ENVIRONMENT_FLAGS, _carried_environment
    # P0's dev flag: not a sync/stage key, read by nothing since C3, and a definition that still has it loses it.
    assert "FA_TOOLBOX_CONSENT_PROBE" not in _ENVIRONMENT_FLAGS
    assert _carried_environment({"FA_TOOLBOX_CONSENT_PROBE": "1", "TZ": "Asia/Taipei"}) == {"TZ": "Asia/Taipei"}


def test_a_connector_entry_not_the_server_hint_decides_whether_a_failed_call_may_be_made_again():
    from types import SimpleNamespace

    from shared_context.team_connector_wire import connector_entry_id

    hinted = {"annotations": {"readOnlyHint": True}}
    # Work IQ lists call_function as read-only, but it sits in the write entry: a failure is never "call again".
    assert native_toolbox._read_only(SimpleNamespace(entry_id=connector_entry_id("teams_write")), hinted) is False
    assert native_toolbox._read_only(SimpleNamespace(entry_id=connector_entry_id("teams_read")), {}) is True
    assert native_toolbox._read_only(SimpleNamespace(entry_id="WebIQ-web"), hinted) is True
    assert native_toolbox._read_only(SimpleNamespace(entry_id="WebIQ-web"), {}) is False


def test_a_teams_write_create_entity_carries_the_message_body_note_and_other_toolboxes_do_not():
    from shared_context.connector_registry import ConnectorEntry
    from shared_context.team_connector_wire import connector_entry_id
    from shared_context.team_mcp_wire import TeamMcpServer, team_mcp_grant, toolbox_grant

    def server(tools):
        return TeamMcpServer(server_label="teams_write", server_url="https://workiq.svc.cloud.microsoft/mcp",
                             allowed_tools=tools, version="v1")
    connector = toolbox_grant(connector_entry_id("teams_write"), "fa-connector-team-one-teams",
                              server(("create_entity", "delete_entity")))
    team = team_mcp_grant("team-one", server(("create_entity",)))
    meta = {"tool_configuration": {"type": "mcp", "server_label": "teams_write", "require_approval": "never"}}
    listed = "Create an entity. " + "x" * 2000
    tools = bind(Toolbox([
        {"name": "teams_write___create_entity", "description": listed, "inputSchema": SCHEMA, "_meta": meta},
        {"name": "teams_write___delete_entity", "description": "Delete an entity", "inputSchema": SCHEMA, "_meta": meta},
    ]), context(grants=(connector, team)))
    note = ('\nTo post a Teams chat or channel message: parentUrl ends with /messages; jsonBody is '
            '{"body":{"contentType":"text" or "html","content":"..."}}. Do not put @odata.type inside body '
            '(Teams rejects it with 400).')
    create = tools["connector-teams_write-create_entity"][0].description
    assert create == listed[:1024 - len(note)] + note and len(create) == 1024
    assert tools["connector-teams_write-delete_entity"][0].description == "Delete an entity"
    assert tools[team.tools[0]][0].description == listed[:1024]
    with pytest.raises(ValueError, match="outside allowed_tools"):
        ConnectorEntry(server_label="teams_write", allowed_tools=("create_entity",), default_approval="always",
                       tool_notes={"fetch": "Use the full path."})


def test_a_relay_turn_and_its_automatic_continuation_both_count_as_relay_turns():
    from types import SimpleNamespace

    relay = SimpleNamespace(request_id="tr-" + "a" * 40, continuation=None)
    resumed = SimpleNamespace(request_id="resume-" + "b" * 56,
                              continuation=SimpleNamespace(root_request_id="tr-" + "a" * 40))
    workbench = SimpleNamespace(request_id="text-" + "c" * 32, continuation=None)
    resumed_workbench = SimpleNamespace(request_id="resume-" + "d" * 56,
                                        continuation=SimpleNamespace(root_request_id="text-" + "c" * 32))
    assert [native_toolbox._relay_turn(r) for r in (relay, resumed, workbench, resumed_workbench)] == [
        True, True, False, False]
