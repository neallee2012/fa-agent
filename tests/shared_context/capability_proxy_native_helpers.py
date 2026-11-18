"""從 test_capability_proxy_native.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import json
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse
from shared_context.errors import Problem


KEY = "OfflineWebIqKey9Zx2Lp7Rt4Vw8Ky3Nb6"


def fake_webiq():
    # Served at the captured entry's URL path; the in-process transport never reaches the network.
    server = FastMCP("fake-webiq", stateless_http=True, json_response=True, streamable_http_path="/v3/mcp",
                     transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @server.tool(description="Search the web.")
    def web(query: str) -> str:
        return json.dumps({"webResults": [{"url": "https://example.test/fa", "content": "FA result for " + query}]})

    @server.tool(description="Read one URL.")
    def browse(url: str) -> str:
        return "page"
    app = server.streamable_http_app()

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and dict(scope["headers"]).get(b"x-apikey") != KEY.encode():
            await PlainTextResponse("denied", status_code=401)(scope, receive, send)
            return
        await app(scope, receive, send)
    return server, httpx.ASGITransport(app=guarded)


def served(operation, calls):
    """Each API call runs its own fake server inside the event loop the real client uses."""
    async def wrapped(*args, **kwargs):
        server, transport = fake_webiq()
        try:
            async with server.session_manager.run():
                result = await operation(*args, transport=transport, **kwargs)
        except BaseExceptionGroup as group:
            # The fake server's own task group wraps what the client raised.
            problems = group.subgroup(lambda error: isinstance(error, Problem))
            if problems is None or len(problems.exceptions) != 1:
                raise
            raise problems.exceptions[0] from None
        calls.append(operation.__name__)
        return result
    return wrapped
