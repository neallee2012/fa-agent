"""8765 live desktop viewer: local boundary on the WebSocket, owner bearer to the API broker, vendored noVNC."""

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from web_test_console import desktop
from web_test_console.agent_connections import AgentConnections
from web_test_console.bridge import create_app
from test_desktop_view_api import FakeUpstream
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_web_test_console_agents import connection_file, directory_payload
from test_web_test_console_frozen_interface import real_interface  # noqa: F401

ORIGIN = "http://127.0.0.1:8873"
SESSION = "fa-chat-" + "b" * 48
PATH = f"/api/agents/agent-one/desktop?session={SESSION}"


@pytest.fixture
def console(real_interface, tmp_path, monkeypatch):
    c = real_interface
    manifest = connection_file(c, tmp_path)
    connections = AgentConnections.model_validate_json(manifest.path.read_bytes())
    opened = []

    def backend(request):
        if request.url.path == "/work-policy/v1/agents":
            return httpx.Response(200, json=directory_payload(), headers={"cache-control": "no-store"})
        path = request.url.path.removeprefix("/agents/agent-one")
        return c.backend.send(httpx.Request(request.method, request.url.copy_with(path=path),
                                           headers=request.headers, content=request.content))

    async def connect(url, token):
        opened.append((url, token))
        upstream = FakeUpstream()
        opened.append(upstream)
        return upstream
    monkeypatch.setattr(desktop, "connect", connect)
    token = c.h.headers()["Authorization"].removeprefix("Bearer ")
    upstream = httpx.Client(transport=httpx.MockTransport(backend))
    app = create_app(c.config, c.journal, backend=upstream, token_provider=lambda: token,
                     clock=lambda: c.h.time, port=8873, instruction_connection=c.connection,
                     agent_connections=connections)
    with TestClient(app, base_url=ORIGIN) as ui:
        page = ui.get("/")
        csrf = ui.get("/api/state").json()["csrf"]

        def login():
            response = ui.post("/api/authenticate", json={}, headers={"Origin": ORIGIN, "X-Console-CSRF": csrf})
            assert response.status_code == 200, response.text
            assert ui.get("/api/agents").status_code == 200
        yield ui, page, login, opened, token, c
    upstream.close()


def closed(socket):
    with pytest.raises(WebSocketDisconnect) as error:
        socket.receive_bytes()
    return error.value.code, error.value.reason


def browser(ui, *, origin=ORIGIN, cookie=True):
    """A same-origin browser socket: the absolute URL carries Host and the jar's cookie; Origin is explicit."""
    headers = {} if origin is None else {"Origin": origin}
    if not cookie:
        assert "fa_console" not in ui.cookies
    return headers


def socket_for(ui, path=PATH, **options):
    return ui.websocket_connect("ws://127.0.0.1:8873" + path, headers=browser(ui, **options))


def refused(ui, path=PATH, **options):
    with socket_for(ui, path, **options) as socket:
        return closed(socket)


def test_csp_allows_only_this_consoles_socket_and_the_cursor_image(console):
    _, page, *_ = console
    policy = dict(part.strip().split(" ", 1) for part in page.headers["content-security-policy"].split(";"))
    assert policy["connect-src"] == "'self' ws://127.0.0.1:8873"
    assert policy["img-src"] == "'self' blob: data:"


def test_vendored_novnc_modules_are_served_as_javascript_and_nothing_else(console):
    ui, *_ = console
    module = ui.get("/novnc/core/rfb.js")
    assert module.status_code == 200 and module.headers["content-type"] == "text/javascript; charset=utf-8"
    assert b"export default class RFB" in module.content
    assert ui.get("/novnc/vendor/pako/lib/zlib/inflate.js").status_code == 200
    for path in ("/novnc/LICENSE.txt", "/novnc/core/missing.js", "/novnc/%2e%2e/desktop.py",
                 "/novnc/..%2F..%2Fweb_test_console%2Fbridge.py"):
        assert ui.get(path).status_code in {403, 404}, path
    assert (desktop.NOVNC / "LICENSE.txt").is_file() and (desktop.NOVNC / "docs" / "LICENSE.MPL-2.0").is_file()


def test_another_origin_or_a_missing_cookie_never_reaches_the_api(console):
    ui, _, login, opened, *_ = console
    login()
    assert refused(ui, origin="http://evil.example") == (4403, "LOCAL_REQUEST_DENIED")
    assert refused(ui, origin=None) == (4403, "LOCAL_REQUEST_DENIED")
    with ui.websocket_connect(PATH, headers=browser(ui)) as socket:
        assert closed(socket) == (4403, "LOCAL_REQUEST_DENIED")
    ui.cookies.clear()
    assert refused(ui, cookie=False) == (4403, "LOCAL_REQUEST_DENIED")
    assert opened == []


def test_selectors_login_and_agent_are_checked_before_connecting(console):
    ui, _, login, opened, *_ = console
    assert refused(ui) == (4409, "AGENT_DIRECTORY_UNAVAILABLE")
    login()
    for path in ("/api/agents/agent-one/desktop", f"/api/agents/agent-one/desktop?session=-bad",
                 f"{PATH}&session={SESSION}", f"{PATH}&extra=1"):
        assert refused(ui, path) == (4422, "DESKTOP_SESSION_REQUIRED"), path
    assert refused(ui, f"/api/agents/unlisted/desktop?session={SESSION}") == (4409, "AGENT_NOT_AVAILABLE")
    assert opened == []


def test_the_owner_bearer_reaches_the_api_broker_and_bytes_flow_both_ways(console):
    ui, _, login, opened, token, c = console
    login()
    with socket_for(ui) as socket:
        socket.send_bytes(b"RFB 003.008\n")
        upstream = opened[1]
        assert upstream.sent.get(timeout=5) == b"RFB 003.008\n"
        upstream.output.put(b"RFB 003.008\n")
        assert socket.receive_bytes() == b"RFB 003.008\n"
        upstream.output.put((4409, "DESKTOP_VM_NOT_RUNNING"))
        assert closed(socket) == (4409, "DESKTOP_VM_NOT_RUNNING")
    host = c.config.backend_origin.removeprefix("https://")
    assert opened[0] == (f"wss://{host}/desktop/v1/agents/agent-one/sessions/{SESSION}", token)
    assert upstream.closed


def test_a_refused_api_upgrade_is_reported(console, monkeypatch):
    ui, _, login, *_ = console
    login()

    async def fail(url, token):
        raise OSError("refused")
    monkeypatch.setattr(desktop, "connect", fail)
    assert refused(ui) == (4502, "DESKTOP_VIEW_UPSTREAM_REFUSED")


def test_the_page_offers_a_viewer_that_explains_every_close_reason():
    text = desktop.NOVNC.with_name("index.html").read_text(encoding="utf-8")
    assert 'import("/novnc/core/rfb.js")' in text and "{shared:true}" in text
    assert "viewOnly" not in text and "scaleViewport=true" in text and "requestFullscreen" in text
    # The Hosted x11vnc draws the desktop pointer into the picture; the dot marks the viewer's own mouse.
    assert "showDotCursor=true" in text
    for reason in ("DESKTOP_NOT_RUNNING", "DESKTOP_VM_NOT_RUNNING", "DESKTOP_SESSION_UNKNOWN", "DESKTOP_CLOSED",
                   "DESKTOP_VIEW_DENIED", "DESKTOP_VIEW_UPSTREAM_REFUSED", "LOGIN_REQUIRED"):
        assert reason + ":" in text, reason
