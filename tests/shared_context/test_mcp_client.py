"""API-side MCP client: allowlisted tools, secret handling and error classes against an in-process MCP server."""

import json
import subprocess
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse

from shared_context.errors import Problem
from shared_context.mcp_client import MAX_RESULT_CHARS, call_tool, list_tools
from shared_context.tool_catalog import parse_entry_file

KEY = "Zx9Qm2Lp7Rt4Vw8Ky3Nb6Hc1Jd5Fg0Sa"
ENTRY = parse_entry_file("websearch.json", json.dumps({"mcpServers": {"WebSearch-MCP": {
    "url": "https://webiq.test/mcp", "type": "http",
    "headers": {"x-apikey": "${secret:websearch-api-key}"}, "tools": ["web", "browse"],
}}}).encode("utf-8"))
SECRETS = {"websearch-api-key": KEY}


class Recording(httpx.AsyncBaseTransport):
    def __init__(self, inner):
        self.inner, self.requests = inner, []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


def fake_server(*, tools=("web", "browse", "images"), huge=False, failing=False, describe="Search the web."):
    server = FastMCP("fake-webiq", stateless_http=True, json_response=True,
                     transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    if "web" in tools:
        @server.tool(description=describe)
        def web(query: str) -> str:
            if failing:
                raise ValueError("query rejected by the fake service")
            if huge:
                return "x" * (MAX_RESULT_CHARS + 10)
            return json.dumps({"webResults": [{"url": "https://example.test/a", "content": query}]})
    if "browse" in tools:
        @server.tool(description="Read one URL.")
        def browse(url: str) -> str:
            return "page"
    if "images" in tools:
        @server.tool(description="Not allowlisted.")
        def images(query: str) -> str:
            return "image"
    app = server.streamable_http_app()

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and dict(scope["headers"]).get(b"x-apikey") != KEY.encode():
            await PlainTextResponse("denied", status_code=401)(scope, receive, send)
            return
        await app(scope, receive, send)
    return server, Recording(httpx.ASGITransport(app=guarded))


def run(server, operation):
    async def main():
        async with server.session_manager.run():
            return await operation()
    try:
        return anyio.run(main)
    except BaseExceptionGroup as group:
        # The fake server's own task group wraps what the client raised.
        problems = group.subgroup(lambda error: isinstance(error, Problem))
        if problems is None or len(problems.exceptions) != 1 or isinstance(problems.exceptions[0], BaseExceptionGroup):
            raise
        raise problems.exceptions[0] from None


def denied(code, operation, server=None):
    with pytest.raises(Problem) as error:
        if server is None:
            anyio.run(operation)
        else:
            run(server, operation)
    body = json.dumps(error.value.body)
    assert error.value.body["error"]["code"] == code and KEY not in body
    return error.value


def test_lists_only_allowlisted_tools_with_schema_digests_and_sends_the_resolved_header():
    server, transport = fake_server()
    listed = run(server, lambda: list_tools(ENTRY, SECRETS.get, transport=transport))
    assert [tool.name for tool in listed.tools] == ["web", "browse"]
    assert listed.entry_sha256 == ENTRY.entry_sha256
    assert listed.tools[0].input_schema["properties"]["query"]["type"] == "string"
    assert len(listed.tools[0].input_schema_sha256) == 64
    assert transport.requests and all(r.headers["x-apikey"] == KEY for r in transport.requests)
    assert KEY not in listed.model_dump_json()


def test_call_returns_bounded_text_and_error_results_as_data():
    server, transport = fake_server()
    result = run(server, lambda: call_tool(ENTRY, SECRETS.get, "web", {"query": "hello"}, transport=transport))
    assert not result.is_error and "https://example.test/a" in result.text and not result.truncated
    assert KEY not in result.model_dump_json()

    server, transport = fake_server(failing=True)
    failed = run(server, lambda: call_tool(ENTRY, SECRETS.get, "web", {"query": "x"}, transport=transport))
    assert failed.is_error and "rejected" in failed.text

    server, transport = fake_server(huge=True)
    huge = run(server, lambda: call_tool(ENTRY, SECRETS.get, "web", {"query": "x"}, transport=transport))
    assert huge.truncated and len(huge.text) == MAX_RESULT_CHARS


def test_missing_secret_or_rejected_key_is_needs_auth_without_leaking_the_key():
    server, transport = fake_server()
    denied("CAPABILITY_NEEDS_AUTH", lambda: list_tools(ENTRY, lambda name: None, transport=transport))
    assert transport.requests == []
    wrong = {"websearch-api-key":"Wr0ngKeyValue9876543210abcdefXYZ"}
    error = denied("CAPABILITY_NEEDS_AUTH", lambda: list_tools(ENTRY, wrong.get, transport=transport), server)
    assert "Wr0ngKeyValue" not in json.dumps(error.body)


@pytest.mark.parametrize("respond", [
    lambda request: httpx.Response(500, text="boom"),
    lambda request: httpx.Response(302, headers={"location": "https://elsewhere.test/mcp"}),
])
def test_unavailable_service_and_cross_origin_redirects_are_unavailable(respond):
    seen = []

    def handler(request):
        seen.append(request)
        return respond(request)
    denied("CAPABILITY_UNAVAILABLE", lambda: list_tools(ENTRY, SECRETS.get, transport=httpx.MockTransport(handler)))
    assert seen and all(request.url.host == "webiq.test" for request in seen)


def test_connection_failure_and_timeout_are_unavailable():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    denied("CAPABILITY_UNAVAILABLE", lambda: list_tools(ENTRY, SECRETS.get, transport=httpx.MockTransport(refuse)))

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await anyio.sleep(5)
    denied("CAPABILITY_UNAVAILABLE", lambda: list_tools(ENTRY, SECRETS.get, timeout=0.2, transport=Slow()))


def test_tools_outside_the_allowlist_or_missing_on_the_server_are_refused():
    server, transport = fake_server()
    denied("CAPABILITY_TOOL_DENIED", lambda: call_tool(ENTRY, SECRETS.get, "images", {"query": "x"}, transport=transport))
    assert transport.requests == []
    server, transport = fake_server(tools=("web",))
    denied("CAPABILITY_TOOL_UNAVAILABLE", lambda: list_tools(ENTRY, SECRETS.get, transport=transport), server)


def test_changed_schema_is_refused_before_the_call():
    server, transport = fake_server()
    listed = run(server, lambda: list_tools(ENTRY, SECRETS.get, transport=transport))
    digest = listed.tools[0].input_schema_sha256
    server, transport = fake_server()
    ok = run(server, lambda: call_tool(ENTRY, SECRETS.get, "web", {"query": "a"}, expected_schema_sha256=digest,
                                       transport=transport))
    assert not ok.is_error
    server, transport = fake_server()
    denied("CAPABILITY_SCHEMA_CHANGED", lambda: call_tool(ENTRY, SECRETS.get, "web", {"query": "a"},
                                                          expected_schema_sha256="0" * 64, transport=transport), server)


def test_hosted_entries_are_not_called_by_the_api():
    stdio = parse_entry_file("p.json", json.dumps({"mcpServers": {"playwright": {
        "type": "stdio", "command": "/opt/fa-tools/bin/playwright-mcp", "tools": ["browser_navigate"]}}}).encode())
    denied("CAPABILITY_EXECUTION_DENIED", lambda: list_tools(stdio, SECRETS.get))


def test_catalog_module_stays_free_of_the_mcp_sdk():
    root = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    code = "import sys, shared_context.tool_catalog; print('mcp' in sys.modules)"
    output = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True)
    assert output.stdout.strip() == "False"
