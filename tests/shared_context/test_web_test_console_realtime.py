"""Web PubSub push hints: the bridge only relays negotiate for the configured host and never keeps the token."""

import httpx
import pytest
from fastapi.testclient import TestClient

from web_test_console.agent_connections import AgentConnections
from web_test_console.bridge import create_app
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_web_test_console_agents import connection_file, directory_payload
from test_web_test_console_frozen_interface import real_interface  # noqa: F401

ORIGIN = "http://127.0.0.1:8874"
ENDPOINT = "https://wps-fa-test.webpubsub.azure.com"
ACCESS = "offline-web-pubsub-access-token"
NEGOTIATE = {"schema_version": "fa-realtime-negotiate-v1",
             "url": "wss://wps-fa-test.webpubsub.azure.com/client/hubs/fa?access_token=" + ACCESS,
             "expires_at": "2026-01-01T01:00:00Z", "team_id": "team-one"}


@pytest.fixture
def console(real_interface, tmp_path):
    c = real_interface
    manifest = connection_file(c, tmp_path)
    connections = AgentConnections.model_validate_json(manifest.path.read_bytes())
    replies, seen = {"negotiate": NEGOTIATE, "status": 200}, []

    def backend(request):
        if request.url.path == "/work-policy/v1/agents":
            return httpx.Response(200, json=directory_payload(), headers={"cache-control": "no-store"})
        if request.url.path == "/agents/agent-one/work-policy/v1/realtime/negotiate":
            seen.append(request)
            reply = replies["negotiate"]
            body = {"content": reply} if type(reply) is bytes else {"json": reply}
            return httpx.Response(replies["status"], **body, headers={"cache-control": "no-store"})
        path = request.url.path.removeprefix("/agents/agent-one")
        return c.backend.send(httpx.Request(request.method, request.url.copy_with(path=path),
                                           headers=request.headers, content=request.content))

    token = c.h.headers()["Authorization"].removeprefix("Bearer ")
    upstream = httpx.Client(transport=httpx.MockTransport(backend))
    opened = []

    def start(endpoint=ENDPOINT):
        app = create_app(c.config, c.journal, backend=upstream, token_provider=lambda: token,
                         clock=lambda: c.h.time, port=8874, instruction_connection=c.connection,
                         agent_connections=connections, realtime_endpoint=endpoint)
        ui = TestClient(app, base_url=ORIGIN)
        ui.__enter__()
        opened.append(ui)
        page = ui.get("/")
        csrf = ui.get("/api/state").json()["csrf"]
        response = ui.post("/api/authenticate", json={}, headers={"Origin": ORIGIN, "X-Console-CSRF": csrf})
        assert response.status_code == 200, response.text
        return ui, page
    yield start, replies, seen, c
    for ui in opened:
        ui.__exit__(None, None, None)
    upstream.close()


def connect_src(page):
    policy = dict(part.strip().split(" ", 1) for part in page.headers["content-security-policy"].split(";"))
    return policy["connect-src"]


AGENT = {"X-Console-Agent": "agent-one"}


@pytest.mark.parametrize("endpoint", [None, "", "https://evil.example.com", "http://wps-fa-test.webpubsub.azure.com",
                                      "https://wps-fa-test.webpubsub.azure.com:8443", ENDPOINT + "/client"])
def test_push_stays_off_without_one_exact_web_pubsub_endpoint(console, endpoint):
    start, _, seen, _ = console
    ui, page = start(endpoint)
    assert "const REALTIME_CONFIGURED=false;" in page.text
    assert connect_src(page) == "'self' ws://127.0.0.1:8874"
    refused = ui.get("/api/realtime/negotiate", headers=AGENT)
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "REALTIME_DISABLED"
    assert seen == []


def test_page_and_csp_allow_exactly_the_configured_endpoint(console):
    start, *_ = console
    _, page = start()
    assert "const REALTIME_CONFIGURED=true;" in page.text and "const REALTIME_CONFIGURED=false;" not in page.text
    assert connect_src(page) == "'self' ws://127.0.0.1:8874 wss://wps-fa-test.webpubsub.azure.com"


def test_negotiate_relays_the_url_for_the_selected_agent_and_keeps_no_token(console):
    start, _, seen, c = console
    ui, _ = start()
    response = ui.get("/api/realtime/negotiate", headers=AGENT)
    assert response.status_code == 200, response.text
    assert response.json() == {"url": NEGOTIATE["url"], "expires_at": "2026-01-01T01:00:00+00:00", "team_id": "team-one"}
    assert response.headers["cache-control"] == "no-store"
    assert len(seen) == 1 and seen[0].method == "GET" and not seen[0].url.query and not seen[0].content
    assert seen[0].headers["authorization"].startswith("Bearer ")
    assert ACCESS.encode() not in c.journal.read_bytes()
    assert ui.get("/api/realtime/negotiate?x=1", headers=AGENT).status_code == 403


@pytest.mark.parametrize("url", [
    "wss://evil.example.com/client/hubs/fa?access_token=t",
    "ws://wps-fa-test.webpubsub.azure.com/client/hubs/fa?access_token=t",
    "wss://wps-fa-test.webpubsub.azure.com:444/client/hubs/fa?access_token=t",
    "wss://wps-fa-test.webpubsub.azure.com/server/hubs/fa?access_token=t",
    "wss://wps-fa-test.webpubsub.azure.com/client/hubs/fa",
    "wss://wps-fa-test.webpubsub.azure.com/client/hubs/fa?access_token=t&redirect=x",
    "wss://wps-fa-test.webpubsub.azure.com/client/hubs/fa?access_token=t#x",
])
def test_negotiate_refuses_a_url_that_would_send_the_token_elsewhere(console, url):
    start, replies, _, _ = console
    ui, _ = start()
    replies["negotiate"] = {**NEGOTIATE, "url": url}
    refused = ui.get("/api/realtime/negotiate", headers=AGENT)
    assert refused.status_code == 502 and refused.json()["error"]["code"] == "REALTIME_INVALID"
    assert url not in refused.text


def test_negotiate_refuses_a_malformed_reply(console):
    start, replies, _, _ = console
    ui, _ = start()
    for reply in ({**NEGOTIATE, "expires_at": "2026-01-01T01:00:00"}, {**NEGOTIATE, "extra": 1},
                  {**NEGOTIATE, "schema_version": "v0"}, {k: v for k, v in NEGOTIATE.items() if k != "team_id"}):
        replies["negotiate"] = reply
        refused = ui.get("/api/realtime/negotiate", headers=AGENT)
        assert refused.status_code == 502 and refused.json()["error"]["code"] == "REALTIME_INVALID", reply


def test_negotiate_refuses_a_reply_too_deep_to_parse(console):
    start, replies, _, _ = console
    ui, _ = start()
    replies["negotiate"] = b"[" * 8192  # within NEGOTIATE_BYTES, deeper than the JSON parser's recursion limit
    refused = ui.get("/api/realtime/negotiate", headers=AGENT)
    assert refused.status_code == 502 and refused.json()["error"]["code"] == "REALTIME_INVALID"


def test_received_import_takes_one_rid_and_never_switches_the_open_conversation(console):
    start, *_ = console
    ui, _ = start()
    assert ui.get("/api/instructions/received", headers=AGENT).status_code == 403
    assert ui.get("/api/instructions/received?request_id=a&request_id=b", headers=AGENT).status_code == 403
    assert ui.get("/api/instructions/received?request_id=a&x=1", headers=AGENT).status_code == 403
    bad = ui.get("/api/instructions/received?request_id=../x", headers=AGENT)
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "INSTRUCTION_SCOPE"


@pytest.mark.parametrize("code", ["REALTIME_DISABLED", "REALTIME_UNAVAILABLE"])
def test_the_apis_negotiate_refusals_reach_the_page_by_name(console, code):
    start, replies, _, _ = console
    ui, _ = start()
    replies["status"], replies["negotiate"] = 503, {"error": {"code": code, "message": "keep polling",
                                                               "automatic_retry": False}}
    refused = ui.get("/api/realtime/negotiate", headers=AGENT)
    assert refused.status_code == 503 and refused.json()["error"]["code"] == code
