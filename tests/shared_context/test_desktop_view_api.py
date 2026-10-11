"""API broker for the live desktop viewer: owner check once, then a byte relay to ``invocations_ws``.

Real JWT verification with local keys; Foundry session reads and the upstream WebSocket are fakes."""

import asyncio
import queue
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import A
from shared_context import behavior_desktop_view_api as broker
from shared_context import behavior_team_registry
from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
from shared_context.errors import Problem
from test_m3_auth import CLIENT_ID, HUMAN_OID, jwk_client, signing_key, token  # noqa: F401
from test_team_registry import AGENT_OID, _target

TARGET = _target(1, AGENT_OID)
SESSION = "fa-chat-" + "a" * 48
PATH = f"/desktop/v1/agents/{TARGET.agent_id}/sessions/{SESSION}"


class FakeUpstream:
    """The Foundry ``invocations_ws`` connection: the test feeds Hosted frames and reads what was sent."""

    def __init__(self):
        self.output, self.sent = queue.Queue(), queue.Queue()
        self.close_code = self.close_reason = None
        self.closed, self.hosted_closed = False, None

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            try:
                item = self.output.get_nowait()
            except queue.Empty:
                if self.closed:
                    raise StopAsyncIteration from None
                await asyncio.sleep(0.005)
                continue
            if isinstance(item, tuple):
                self.close_code, self.close_reason = item
                raise StopAsyncIteration
            return item

    async def send(self, data):
        if self.hosted_closed is not None:
            from websockets.exceptions import ConnectionClosedOK
            from websockets.frames import Close
            self.close_code, self.close_reason = self.hosted_closed
            raise ConnectionClosedOK(Close(*self.hosted_closed), None)
        self.sent.put(data)

    async def close(self):
        self.closed = True


class FakeSessions:
    def __init__(self, status="active", upstream=None):
        self.value, self.upstream, self.calls = status, upstream or FakeUpstream(), []

    def status(self, target, session_id):
        self.calls.append(("status", target.agent_name, session_id))
        if isinstance(self.value, Problem):
            raise self.value
        return self.value

    async def connect(self, target, session_id):
        self.calls.append(("connect", target.agent_name, session_id))
        if isinstance(self.upstream, Exception):
            raise self.upstream
        return self.upstream


@pytest.fixture
def desktop(auth, jwk_client, signing_key, monkeypatch):
    scopes = {TARGET.agent_id: SimpleNamespace(team_id=A.team, target=TARGET)}

    def resolve(store, config, agent_id):
        if agent_id not in scopes:
            raise Problem(403, "AGENT_SCOPE_DENIED", "one configured or registered Agent ID is required")
        return scopes[agent_id]
    monkeypatch.setattr(behavior_team_registry, "resolve_agent_scope", resolve)
    verifier = BehaviorJwtVerifier(auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, A.team), jwk_client)
    composition = SimpleNamespace(verifier=verifier, runtime=SimpleNamespace(store=object()), config=object(),
                                  clients=())

    def client(sessions):
        app = FastAPI()
        broker.install_desktop_view_routes(app, composition, sessions=sessions)
        return TestClient(app)

    def headers(oid=None):
        claims = ({"scp": "Behavior.Read"} if oid is None else
                  {"oid": oid, "idtyp": "app", "roles": ["Behavior.Agent"]})
        return {"Authorization": "Bearer " + token(signing_key, claims, remove=() if oid is None else ("scp",))}

    return SimpleNamespace(client=client, headers=headers, scopes=scopes)


def closed(socket):
    with pytest.raises(WebSocketDisconnect) as error:
        socket.receive_bytes()
    return error.value.code, error.value.reason


def refused(desktop, sessions, path=PATH, headers=None):
    with desktop.client(sessions) as client, client.websocket_connect(path, headers=dict(headers or {})) as socket:
        return closed(socket)


def test_without_one_bearer_nothing_is_read_or_opened(desktop):
    sessions = FakeSessions()
    assert refused(desktop, sessions) == (4401, "BEHAVIOR_UNAUTHENTICATED")
    assert sessions.calls == []


def test_an_agent_identity_cannot_watch_or_drive_a_desktop(desktop):
    sessions = FakeSessions()
    assert refused(desktop, sessions, headers=desktop.headers(A.oid)) == (4403, "DESKTOP_VIEW_DENIED")
    assert sessions.calls == []


def test_another_teams_or_unknown_agent_is_refused_before_foundry(desktop):
    sessions = FakeSessions()
    desktop.scopes["other"] = SimpleNamespace(team_id="team-other", target=TARGET)
    assert refused(desktop, sessions, f"/desktop/v1/agents/other/sessions/{SESSION}",
                   desktop.headers()) == (4403, "BEHAVIOR_ACCESS_DENIED")
    assert refused(desktop, sessions, f"/desktop/v1/agents/missing/sessions/{SESSION}",
                   desktop.headers()) == (4403, "AGENT_SCOPE_DENIED")
    assert sessions.calls == []


@pytest.mark.parametrize("status", ["idle", "creating", "deleted"])
def test_a_session_that_is_not_running_is_never_woken(desktop, status):
    sessions = FakeSessions(status)
    assert refused(desktop, sessions, headers=desktop.headers()) == (4409, "DESKTOP_VM_NOT_RUNNING")
    assert sessions.calls == [("status", TARGET.agent_name, SESSION)]


def test_another_agents_or_unreadable_session_is_explained(desktop):
    for problem, expected in (
            (Problem(404, "DESKTOP_SESSION_UNKNOWN", "x"), (4404, "DESKTOP_SESSION_UNKNOWN")),
            (Problem(503, "DESKTOP_SESSION_UNAVAILABLE", "x"), (4503, "DESKTOP_SESSION_UNAVAILABLE"))):
        sessions = FakeSessions(problem)
        assert refused(desktop, sessions, headers=desktop.headers()) == expected
        assert [call[0] for call in sessions.calls] == ["status"]
    sessions = FakeSessions()
    assert refused(desktop, sessions, f"/desktop/v1/agents/{TARGET.agent_id}/sessions/-bad",
                   desktop.headers()) == (4422, "DESKTOP_SESSION_UNKNOWN")
    assert sessions.calls == []


def test_owner_gets_a_two_way_relay_and_the_hosted_close_reason(desktop):
    sessions = FakeSessions()
    upstream = sessions.upstream
    with desktop.client(sessions) as client, client.websocket_connect(PATH, headers=desktop.headers()) as socket:
        upstream.output.put(b"RFB 003.008\n")
        assert socket.receive_bytes() == b"RFB 003.008\n"
        socket.send_bytes(b"RFB 003.008\n")
        assert upstream.sent.get(timeout=5) == b"RFB 003.008\n"
        upstream.output.put((4404, "DESKTOP_NOT_RUNNING"))
        assert closed(socket) == (4404, "DESKTOP_NOT_RUNNING")
    assert sessions.calls == [("status", TARGET.agent_name, SESSION), ("connect", TARGET.agent_name, SESSION)]
    assert upstream.closed


def test_a_viewer_leaving_closes_the_upstream_and_text_is_refused(desktop):
    sessions = FakeSessions()
    with desktop.client(sessions) as client:
        with client.websocket_connect(PATH, headers=desktop.headers()) as socket:
            socket.send_bytes(b"x")
            assert sessions.upstream.sent.get(timeout=5) == b"x"
        for _ in range(200):
            if sessions.upstream.closed:
                break
            time.sleep(0.01)
    assert sessions.upstream.closed
    sessions = FakeSessions()
    with desktop.client(sessions) as client, client.websocket_connect(PATH, headers=desktop.headers()) as socket:
        socket.send_text("hello")
        assert closed(socket) == (1003, "RFB_BINARY_ONLY")
    assert sessions.upstream.closed and sessions.upstream.sent.empty()


def test_hosted_closing_while_the_viewer_sends_keeps_the_hosted_reason(desktop):
    sessions = FakeSessions()
    with desktop.client(sessions) as client, client.websocket_connect(PATH, headers=desktop.headers()) as socket:
        socket.send_bytes(b"before")
        assert sessions.upstream.sent.get(timeout=5) == b"before"
        sessions.upstream.hosted_closed = (1001, "going away")
        socket.send_bytes(b"framebuffer-update-request")
        assert closed(socket) == (1001, "going away")
    assert sessions.upstream.closed


def test_an_abnormal_upstream_close_is_not_forwarded_as_an_unsendable_code(desktop):
    sessions = FakeSessions()
    with desktop.client(sessions) as client, client.websocket_connect(PATH, headers=desktop.headers()) as socket:
        sessions.upstream.output.put((1006, ""))
        assert closed(socket) == (1011, "DESKTOP_VIEW_UPSTREAM_CLOSED")


def test_a_refused_upstream_upgrade_is_reported(desktop):
    sessions = FakeSessions(upstream=OSError("refused"))
    assert refused(desktop, sessions, headers=desktop.headers()) == (4502, "DESKTOP_VIEW_UPSTREAM_REFUSED")


def test_the_foundry_connection_uses_the_official_route_and_the_api_identity(monkeypatch):
    import websockets.asyncio.client as client
    calls = []

    async def connect(url, **options):
        calls.append((url, options))
        return "upstream"
    monkeypatch.setattr(client, "connect", connect)
    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="API-MI-TOKEN", scope=scope))
    assert asyncio.run(broker.FoundryDesktopSessions(credential).connect(TARGET, SESSION)) == "upstream"
    (url, options), = calls
    assert url == ("wss://synthetic.services.ai.azure.com/api/projects/project/agents/registered-1/endpoint/"
                   f"protocols/invocations_ws?api-version=v1&agent_session_id={SESSION}")
    assert options == {"additional_headers": {"Authorization": "Bearer API-MI-TOKEN"},
                       "max_size": broker.MAX_FRAME_BYTES, "open_timeout": broker.OPEN_TIMEOUT_SECONDS,
                       "compression": None, "proxy": None}


def test_the_production_api_serves_the_viewer_route():
    import inspect

    from shared_context import behavior_server
    assert "install_desktop_view_routes(app, composition)" in inspect.getsource(behavior_server.application)
