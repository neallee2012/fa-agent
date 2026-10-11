"""Web PubSub hints (v16): API-only, ID-only, best effort; a fake service client, no Azure call."""

import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import TENANT
from shared_context import realtime_push
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.errors import Problem
from shared_context.native_hitl_api import ENABLED_ENV, install_native_hitl_routes
from shared_context.native_hitl_wire import NativeHitlClose, NativeHitlEnd, NativeHitlTurn
from shared_context.realtime_push import ENDPOINT_ENV, HUB_ENV, hint, realtime_publisher, realtime_settings
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_hitl import create_pending, decision, hitl, post  # noqa: F401
from test_native_hitl_questions import answer, create_question
from test_behavior_assistant import assistant  # noqa: F401
from test_instruction_flow import instruction_models, runtime_path  # noqa: F401

ENDPOINT = "https://wps-fa-test.webpubsub.azure.com"
URL = "wss://wps-fa-test.webpubsub.azure.com/client/hubs/fa?access_token=synthetic"


class FakeService:
    def __init__(self):
        self.sent, self.tokens, self.fail, self.url = [], [], False, URL

    def send_to_group(self, group, message, *, content_type):
        if self.fail:
            raise RuntimeError("synthetic outage")
        self.sent.append((group, message, content_type))

    def get_client_access_token(self, **kwargs):
        if self.fail:
            raise RuntimeError("synthetic outage")
        self.tokens.append(kwargs)
        return {"baseUrl": self.url.split("?")[0], "token": "synthetic", "url": self.url}

    def messages(self):
        for publisher in list(realtime_push._publishers.values()):
            publisher.drain()
        return [{key: value for key, value in message.items() if key != "at"} for _, message, _ in self.sent]


@pytest.fixture
def realtime(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(realtime_push, "_publishers", {})
    monkeypatch.setattr(realtime_push, "_client_factory", lambda endpoint, hub, client_id: service)
    monkeypatch.setenv(ENDPOINT_ENV, ENDPOINT)
    monkeypatch.delenv(HUB_ENV, raising=False)
    yield service
    for publisher in realtime_push._publishers.values():
        publisher._executor.shutdown(wait=True)


def test_a_hint_carries_only_its_declared_ids():
    value = hint("team-one", "turn.finished", {"agent_id": "agent-a", "request_id": "rid-1", "conversation_id": None})
    assert set(value) == {"schema_version", "type", "team_id", "at", "agent_id", "request_id"}
    assert value["schema_version"] == "fa-realtime-event-v1" and value["at"].endswith("Z")
    for event_type, fields in (
            ("turn.finished", {"request_id": "rid-1", "text": "hello"}),
            ("turn.finished", {"request_id": "hello world"}),
            ("turn.finished", {"request_id": 7}),
            ("turn.started", {"request_id": "rid-1"})):
        with pytest.raises(ValueError):
            hint("team-one", event_type, fields)
    with pytest.raises(ValueError):
        hint("team/one", "turn.finished", {"request_id": "rid-1"})


def test_settings_are_off_when_unset_and_stop_startup_when_malformed():
    assert realtime_settings({}) is None and realtime_publisher(environ={}) is None
    assert realtime_settings({ENDPOINT_ENV: ENDPOINT}) == (ENDPOINT, "fa")
    assert realtime_settings({ENDPOINT_ENV: ENDPOINT, HUB_ENV: "fa_dev"}) == (ENDPOINT, "fa_dev")
    for environ in ({ENDPOINT_ENV: "http://wps-fa-test.webpubsub.azure.com"},
                    {ENDPOINT_ENV: ENDPOINT + "/"}, {ENDPOINT_ENV: "https://elsewhere.example.com"},
                    {ENDPOINT_ENV: ENDPOINT, HUB_ENV: "1fa"}):
        with pytest.raises(ValueError):
            realtime_settings(environ)


def test_publishing_is_queued_json_to_the_team_group_and_a_failure_is_one_log_line(realtime, caplog, monkeypatch):
    publisher = realtime_publisher()
    assert realtime_publisher() is publisher
    publisher.notify("team-one", "turn.finished", agent_id="agent-a", request_id="rid-1")
    assert realtime.messages() == [{"schema_version": "fa-realtime-event-v1", "type": "turn.finished",
                                    "team_id": "team-one", "agent_id": "agent-a", "request_id": "rid-1"}]
    assert realtime.sent[0][0] == "team.team-one" and realtime.sent[0][2] == "application/json"

    def unreadable():
        raise Problem(404, "NATIVE_HITL_NOT_FOUND", "synthetic")
    publisher.notify("team-one", "hitl.approval", build=unreadable, pending_id="pending-1", status="allow")
    assert realtime.messages()[-1] == {"schema_version": "fa-realtime-event-v1", "type": "hitl.approval",
                                       "team_id": "team-one", "pending_id": "pending-1", "status": "allow"}

    realtime.fail = True
    with caplog.at_level(logging.WARNING, logger="shared_context.realtime_push"):
        assert publisher.notify("team-one", "turn.finished", request_id="rid-2") is None
        publisher.drain()
        monkeypatch.setattr(realtime_push, "MAX_BACKLOG", 0)
        publisher.notify("team-one", "turn.finished", request_id="rid-3")
    assert [record.getMessage() for record in caplog.records if record.name == "shared_context.realtime_push"] == [
        "FA_REALTIME_PUBLISH_FAILED type=turn.finished exception_type=RuntimeError",
        "FA_REALTIME_PUBLISH_DROPPED type=turn.finished reason=backlog"]
    assert len(realtime.sent) == 2


def test_the_client_url_is_for_this_endpoint_hub_and_team_only(realtime):
    publisher = realtime_publisher()
    value = publisher.client_access("owner-oid", "team-one")
    assert value["schema_version"] == "fa-realtime-negotiate-v1" and value["url"] == URL
    assert value["team_id"] == "team-one" and value["expires_at"].endswith("Z")
    assert realtime.tokens == [{"user_id": "owner-oid", "groups": ["team.team-one"], "minutes_to_expire": 60}]
    for url in ("wss://other.webpubsub.azure.com/client/hubs/fa?access_token=x",
                "wss://wps-fa-test.webpubsub.azure.com:444/client/hubs/fa?access_token=x",
                "wss://wps-fa-test.webpubsub.azure.com/client/hubs/other?access_token=x",
                "wss://wps-fa-test.webpubsub.azure.com/client/hubs/fa?access_token=x&role=y",
                "ws://wps-fa-test.webpubsub.azure.com/client/hubs/fa?access_token=x"):
        realtime.url = url
        with pytest.raises(ValueError):
            publisher.client_access("owner-oid", "team-one")


@pytest.fixture
def pushed_hitl(hitl, realtime):
    app = FastAPI()
    _install_error_handlers(app)
    install_native_hitl_routes(app, hitl.composition, environ={ENABLED_ENV: "1", ENDPOINT_ENV: ENDPOINT})
    with TestClient(app) as client:
        hitl.client = client
        yield hitl


def test_hitl_writes_hint_the_original_turn_ids_and_reconcile_or_refusal_does_not(pushed_hitl, realtime):
    c = pushed_hitl
    team = c.owner.team
    ids = {"agent_id": "native-a", "conversation_id": "cid-one", "request_id": "rid-one"}
    _, pending = create_pending(c)
    choice = decision(c, pending)
    assert post(c, "/decisions", choice).status_code == 200
    assert post(c, "/decisions/reconcile", choice).status_code == 200
    assert post(c, "/decisions", decision(c, pending, request_id="decision-two")).status_code == 409
    _, other = create_pending(c, call_id="tool-two", request_id="pending-two")
    closed = NativeHitlClose(request_id="close-two", pending_id=other["pending_id"],
                             pending_digest=other["pending_digest"], status="interrupted")
    assert post(c, "/observations", closed, c.agent).status_code == 200
    _, question = create_question(c)
    assert post(c, "/answers", answer(question)).status_code == 200
    end = NativeHitlEnd(request_id="end-one", turn=NativeHitlTurn(**ids))
    assert post(c, "/end", end, c.agent).status_code == 200
    quiet = NativeHitlEnd(request_id="end-quiet", turn=NativeHitlTurn(**{**ids, "request_id": "rid-quiet"}))
    assert post(c, "/end", quiet, c.agent).status_code == 200
    common = {"schema_version": "fa-realtime-event-v1", "team_id": team}
    assert realtime.messages() == [
        {**common, "type": "hitl.approval", **ids, "pending_id": pending["pending_id"], "status": "pending"},
        {**common, "type": "hitl.approval", **ids, "pending_id": pending["pending_id"], "status": "allow"},
        {**common, "type": "hitl.approval", **ids, "pending_id": other["pending_id"], "status": "pending"},
        {**common, "type": "hitl.approval", **ids, "pending_id": other["pending_id"], "status": "interrupted"},
        {**common, "type": "hitl.question", **ids, "question_id": question["question_id"], "status": "pending"},
        {**common, "type": "hitl.question", **ids, "question_id": question["question_id"], "status": "answered"},
        {**common, "type": "hitl.closed", **ids},
    ]
    assert all(group == "team." + team for group, _, _ in realtime.sent)
    text = json.dumps([message for _, message, _ in realtime.sent])
    assert "colour" not in text and "echo" not in text and "blue" not in text


def test_hitl_writes_still_succeed_when_publishing_fails(pushed_hitl, realtime):
    realtime.fail = True
    _, pending = create_pending(pushed_hitl)
    assert post(pushed_hitl, "/decisions", decision(pushed_hitl, pending)).status_code == 200
    assert realtime.messages() == []


def _relay_app(monkeypatch, principal, **results):
    from shared_context import native_agent_api
    from shared_context.native_agent_api import NativeAgentRelayGateway, install_native_agent_routes

    async def body(request, model, **_):
        return SimpleNamespace(conversation_id="cid-a", request_id="rid-a")
    monkeypatch.setattr(native_agent_api, "_body", body)
    for name, value in results.items():
        def call(self, principal, command, value=value):
            if isinstance(value, Problem):
                raise value
            return value
        monkeypatch.setattr(NativeAgentRelayGateway, name, call)
    composition = SimpleNamespace(
        verifier=lambda request: principal,
        config=SimpleNamespace(targets=(SimpleNamespace(agent_id="agent-a-id", agent_oid=principal.oid),)))
    app = FastAPI()
    _install_error_handlers(app)
    install_native_agent_routes(app, composition, enabled=True)
    return TestClient(app)


def test_relay_hints_name_the_recipient_turn_and_the_verified_sender(realtime, monkeypatch):
    from shared_context.behavior_native_agent_wire import NATIVE_AGENT_PREFIX, NativeRelayAcceptance
    principal = BehaviorPrincipal(TENANT, "a-oid", "team-one", "agent", "agent-a", frozenset({"read", "submit"}))
    received = NativeRelayAcceptance(message_id="rid-b", recipient_agent_id="agent-b-id", conversation_id="cid-b",
                                     submit_digest="0" * 64, status="accepted")
    replied = NativeRelayAcceptance(message_id="rid-wake", recipient_agent_id="agent-c-id", conversation_id="cid-c",
                                    submit_digest="1" * 64, status="accepted",
                                    source_conversation_id="cid-a", source_request_id="rid-a")
    refused = Problem(409, "NATIVE_RELAY_RECIPIENT_UNAVAILABLE", "synthetic")
    with _relay_app(monkeypatch, principal, send=received, result=replied, outcome=replied) as client:
        headers = {"Authorization": "Bearer synthetic"}
        assert client.post(NATIVE_AGENT_PREFIX + "/send", json={}, headers=headers).status_code == 202
        assert client.post(NATIVE_AGENT_PREFIX + "/result", json={}, headers=headers).status_code == 202
    with _relay_app(monkeypatch, principal, send=refused) as client:
        assert client.post(NATIVE_AGENT_PREFIX + "/send", json={}, headers=headers).status_code == 409
    common = {"schema_version": "fa-realtime-event-v1", "team_id": "team-one", "sender_agent_id": "agent-a-id"}
    assert realtime.messages() == [
        {**common, "type": "agent.received", "agent_id": "agent-b-id", "conversation_id": "cid-b",
         "request_id": "rid-b", "status": "accepted"},
        {**common, "type": "agent.replied", "agent_id": "agent-c-id", "conversation_id": "cid-c",
         "request_id": "rid-wake", "source_request_id": "rid-a"},
    ]

def test_turn_end_hint_follows_the_recorded_usage_and_negotiate_is_owner_only(realtime, assistant):
    from shared_context.behavior_turn_usage import AGENT_TURN_USAGE_PATH, AgentTurnUsageReport
    from test_sdk_compaction_product import configured
    from test_turn_usage_ledger import group, report
    h, control, human = configured(assistant)
    team = control.policy.template.team_id
    agent = h.verifier(h.client.build_request("GET", "/", headers=h.headers("agent")))
    body = AgentTurnUsageReport(request_id="vm-turn", usage=report(group())).model_dump(mode="json")
    assert h.client.post(AGENT_TURN_USAGE_PATH, json=body, headers=h.headers("agent")).status_code == 200
    different = dict(body, usage=report(group(requests=4)).model_dump(mode="json"))
    assert h.client.post(AGENT_TURN_USAGE_PATH, json=different, headers=h.headers("agent")).status_code == 409
    assert h.client.post(AGENT_TURN_USAGE_PATH, json=body, headers=h.headers()).status_code == 403
    assert realtime.messages() == [{"schema_version": "fa-realtime-event-v1", "type": "turn.finished",
                                    "team_id": team, "agent_id": agent.actor_id, "request_id": "vm-turn"}]

    path = "/work-policy/v1/realtime/negotiate"
    owner = h.client.get(path, headers=h.headers())
    assert owner.status_code == 200, owner.text
    assert owner.json() == {"schema_version": "fa-realtime-negotiate-v1", "url": URL,
                            "expires_at": owner.json()["expires_at"], "team_id": team}
    assert realtime.tokens == [{"user_id": human.oid, "groups": ["team." + team], "minutes_to_expire": 60}]
    assert h.client.get(path, headers=h.headers("agent")).status_code == 403
    assert h.client.get(path + "?team=other", headers=h.headers()).status_code == 422
    realtime.fail = True
    unavailable = h.client.get(path, headers=h.headers())
    assert unavailable.status_code == 503 and unavailable.json()["error"]["code"] == "REALTIME_UNAVAILABLE"
    assert len(realtime.tokens) == 1


def test_negotiate_says_disabled_when_realtime_is_not_configured(assistant, monkeypatch):
    from test_sdk_compaction_product import configured
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    h, _, _ = configured(assistant)
    refused = h.client.get("/work-policy/v1/realtime/negotiate", headers=h.headers())
    assert refused.status_code == 503 and refused.json()["error"] == {
        "code": "REALTIME_DISABLED", "message": "Realtime hints are not configured; keep polling.",
        "automatic_retry": False}
