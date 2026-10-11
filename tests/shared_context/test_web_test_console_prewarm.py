"""Typing prewarm in the bridge (README#typing-prewarm): off unless FA_CONSOLE_PREWARM=1, then one forwarded hint.

The bridge keeps nothing for it: no journal entry, message lock or state, and it adds no body of its own.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from web_test_console import __main__ as launcher
from web_test_console import prewarm as typing_prewarm
from web_test_console.agent_connections import AgentConnections
from web_test_console.bridge import create_app
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_web_test_console_agents import connection_file, directory_payload
from test_web_test_console_frozen_interface import real_interface  # noqa: F401

ORIGIN = "http://127.0.0.1:8875"
AGENT = {"X-Console-Agent": "agent-one"}
PATH = "/agents/agent-one/work-policy/v1/native-turns/prewarm"


@pytest.fixture
def console(real_interface, tmp_path):
    c = real_interface
    connections = AgentConnections.model_validate_json(connection_file(c, tmp_path).path.read_bytes())
    replies, seen = {"status": 200, "body": {"state": "sent", "toolboxes": 2}}, []

    def backend(request):
        if request.url.path == "/work-policy/v1/agents":
            return httpx.Response(200, json=directory_payload(), headers={"cache-control": "no-store"})
        if request.url.path == PATH:
            seen.append(request)
            return httpx.Response(replies["status"], json=replies["body"], headers={"cache-control": "no-store"})
        path = request.url.path.removeprefix("/agents/agent-one")
        return c.backend.send(httpx.Request(request.method, request.url.copy_with(path=path),
                                           headers=request.headers, content=request.content))

    token = c.h.headers()["Authorization"].removeprefix("Bearer ")
    upstream = httpx.Client(transport=httpx.MockTransport(backend))
    opened = []

    def start(prewarm):
        app = create_app(c.config, c.journal, backend=upstream, token_provider=lambda: token,
                         clock=lambda: c.h.time, port=8875, instruction_connection=c.connection,
                         agent_connections=connections, prewarm=prewarm)
        ui = TestClient(app, base_url=ORIGIN)
        ui.__enter__()
        opened.append(ui)
        page = ui.get("/")
        headers = {"Origin": ORIGIN, "X-Console-CSRF": ui.get("/api/state").json()["csrf"]}
        assert ui.post("/api/authenticate", json={}, headers=headers).status_code == 200
        return ui, page, {**headers, **AGENT}
    yield start, replies, seen, c
    for ui in opened:
        ui.__exit__(None, None, None)
    upstream.close()


def test_without_the_setting_the_page_never_asks_and_the_bridge_refuses(console):
    start, _, seen, _ = console
    ui, page, headers = start(False)
    assert "const PREWARM_CONFIGURED=false;" in page.text
    refused = ui.post("/api/instructions/prewarm", json={}, headers=headers)
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "PREWARM_DISABLED"
    assert seen == []


def test_the_bridge_forwards_one_empty_hint_for_the_selected_agent_and_keeps_nothing(console):
    start, replies, seen, c = console
    ui, page, headers = start(True)
    assert "const PREWARM_CONFIGURED=true;" in page.text and "const PREWARM_CONFIGURED=false;" not in page.text
    journal = c.journal.read_bytes()
    response = ui.post("/api/instructions/prewarm", json={}, headers=headers)
    assert response.status_code == 200 and response.json() == {"prewarm": "sent"}
    assert len(seen) == 1 and seen[0].method == "POST" and seen[0].content == b"{}"
    assert seen[0].headers["authorization"].startswith("Bearer ")
    assert c.journal.read_bytes() == journal
    # A refusal from the API is passed on by name; the page ignores it either way.
    replies["status"], replies["body"] = 503, {"error": {"code": "NATIVE_TURNS_DISABLED", "message": "off",
                                                         "automatic_retry": False}}
    refused = ui.post("/api/instructions/prewarm", json={}, headers=headers)
    assert refused.status_code == 503 and len(seen) == 2


def test_a_prewarm_carries_no_body_of_its_own(console):
    start, _, seen, _ = console
    ui, _, headers = start(True)
    assert ui.post("/api/instructions/prewarm", json={"toolboxes": []}, headers=headers).status_code == 403
    assert ui.post("/api/instructions/prewarm", json={}, headers={**headers, "X-Console-CSRF": "x"}).status_code == 403
    assert seen == []


@pytest.mark.parametrize("value,expected", [("1", True), (None, False), ("0", False), ("true", False)])
def test_only_exactly_one_turns_it_on(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(typing_prewarm.ENV, raising=False)
    else:
        monkeypatch.setenv(typing_prewarm.ENV, value)
    assert typing_prewarm.configured() is expected


def test_serve_reads_the_setting_from_the_environment(console, monkeypatch, tmp_path):
    _, _, _, c = console
    folder = tmp_path / "launch"
    folder.mkdir()
    manifest = connection_file(c, folder)
    created = []
    monkeypatch.setattr(launcher, "create_app", lambda *args, **kwargs: created.append(kwargs) or object())
    monkeypatch.setattr(launcher.uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.setenv(typing_prewarm.ENV, "1")
    assert launcher.main(manifest.args) == 0
    monkeypatch.delenv(typing_prewarm.ENV)
    assert launcher.main(manifest.args) == 0
    assert [kwargs["prewarm"] for kwargs in created] == [True, False]
