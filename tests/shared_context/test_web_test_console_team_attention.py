"""Cross-Agent cards keep the target console's journal and the signed original H1 writers."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context.behavior_assistant import _LocalHttp
from shared_context.behavior_inbox import INBOX_VERSION
from web_test_console.adapter import canonical, parse_config, sha
from web_test_console.agent_connections import AgentConnections
from web_test_console.bridge import create_app
from web_test_console.instructions import InstructionConnection
from test_agent_scope_registry import peer_policy
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_native_turn_api import native_api
from test_web_test_console_hitl import hitl_console, pending_call, decision_input
from test_web_test_console_native import native_console
from test_web_test_console_questions import question_call
from test_web_test_console_responses import responses_console, responses_api
from test_web_test_console_standing import configuration


@pytest.fixture
def team_console(request, tmp_path, monkeypatch):
    loop = asyncio.new_event_loop()
    c = request.getfixturevalue("hitl_console")
    draft, pid = pending_call(c)
    _, question = question_call(c)
    target = c.api.remote.target.agent_id
    other = "agent-two"
    policy = peer_policy(SimpleNamespace(targets=(None, SimpleNamespace(
        agent_id=other, agent_oid="22222222-2222-2222-2222-222222222222"))), c.config.standing_policy)
    harness = SimpleNamespace(config=SimpleNamespace(channel=SimpleNamespace(version=INBOX_VERSION)),
                              runtime=c.c.h.runtime, verifier=c.api.composition.verifier)
    other_config = parse_config(canonical({
        **configuration(harness, SimpleNamespace(policy=policy)), "console_id": "offline-other-agent"}))
    links = []
    for identity, config in ((target, c.config), (other, other_config)):
        config_path = tmp_path / (identity + ".json")
        instructions_path = tmp_path / (identity + "-instructions.json")
        connection = InstructionConnection(
            schema_version="fa-console-instructions-connection-v1", configuration_sha256=config.sha256,
            backend_core_sha256=c.connection.backend_core_sha256, state_profile="foundry-native-v1")
        config_path.write_bytes(canonical(config.model_dump(mode="json")))
        instructions_path.write_bytes(canonical(connection.model_dump(mode="json")))
        links.append({
            "agent_id": identity, "config_path": str(config_path), "config_sha256": sha(config_path.read_bytes()),
            "instructions_path": str(instructions_path), "instructions_sha256": sha(instructions_path.read_bytes()),
            "journal_path": str(tmp_path / (identity + ".sqlite3")),
        })
    connections = AgentConnections.model_validate_json(canonical({
        "schema_version": "fa-console-agents-v1", "default_agent_id": target, "agents": links}))
    directory = {"schema_version": "fa-agent-directory-v1", "default_agent_id": target, "agents": [
        {"agent_id": identity, "display_name": "Target B" if identity == target else "Viewing A",
         "api_prefix": "/agents/" + identity, "state": "ready", "reason": None} for identity in (target, other)]}
    transport = _LocalHttp(c.api.client)
    calls, pages = [], []

    def page(*, limit=20, continuation=None, modified_since=None):
        assert modified_since is not None and modified_since.utcoffset().total_seconds() == 0
        pages.append((limit, continuation))
        return [(identifier, c.hitl.service.store.read_record(identifier))
                for identifier in (pid, question["question_id"])], None

    monkeypatch.setattr(c.hitl.service.store, "read_record_page", page)

    def forward(request):
        calls.append((request.method, request.url.path, request.content))
        if request.url.path == "/work-policy/v1/agents":
            return httpx.Response(200, json=directory, headers={"cache-control": "no-store"})
        path = request.url.path
        for identity in (target, other):
            if path.startswith("/agents/" + identity + "/"):
                path = path.removeprefix("/agents/" + identity)
                break
        return transport.handle_request(httpx.Request(
            request.method, request.url.copy_with(path=path), headers=request.headers, content=request.content))

    with httpx.Client(transport=httpx.MockTransport(forward)) as backend:
        app = create_app(c.config, links[0]["journal_path"], backend=backend, port=8883,
            instruction_connection=c.connection, agent_connections=connections,
            token_provider=lambda: c.api.headers["Authorization"].removeprefix("Bearer "),
            clock=lambda: c.c.h.time, environment="OFFLINE_TEST")
        with TestClient(app, base_url="http://127.0.0.1:8883",
                        backend_options={"loop_factory": lambda: loop}) as ui:
            ui.get("/")
            csrf = ui.get("/api/state").json()["csrf"]
            headers = {"Origin": "http://127.0.0.1:8883", "X-Console-CSRF": csrf, "X-Console-Agent": target}
            assert ui.post("/api/authenticate", headers=headers, json={}).status_code == 200
            assert ui.get("/api/agents").status_code == 200
            yield SimpleNamespace(c=c, ui=ui, app=app, headers=headers, target=target, other=other,
                                  draft=draft, pid=pid, question=question, calls=calls, pages=pages)
    if not loop.is_running() and not loop.is_closed():
        loop.close()


def test_team_pending_without_a_local_thread_uses_original_target_and_once(team_console):
    h = team_console
    target_console = h.app.state.agent_consoles.resolve(h.target)
    assert target_console.instructions.store.command(h.draft["request_id"]) is None
    read = h.ui.get("/api/native-hitl/team/pending/" + h.pid, headers=h.headers)
    assert read.status_code == 200, read.text
    value = read.json()
    before = len(h.calls)
    command = decision_input(value, "allow_once")
    decided = h.ui.post("/api/native-hitl/team/decisions", headers=h.headers, json=command)
    assert decided.status_code == 200, decided.text
    assert h.ui.post("/api/native-hitl/team/decisions", headers=h.headers, json=command).status_code == 200
    writes = [call for call in h.calls[before:] if call[:2] == ("POST", "/native-hitl/v1/decisions")]
    assert len(writes) == 1
    saved = target_console.journal.get(decided.json()["request_id"], "native-hitl")
    assert saved["intent"]["selection"] == value["readback"]["approval"]
    assert saved["intent"]["selection"]["call"]["agent_id"] == h.target
    assert saved["intent"]["thread_id"] is None
    assert target_console.instructions.store.command(h.draft["request_id"]) is None
    checked = h.ui.post("/api/native-hitl/team/reconcile", headers=h.headers,
                        json={"request_id": decided.json()["request_id"]})
    assert checked.status_code == 200, checked.text
    assert len([call for call in h.calls if call[:2] == ("POST", "/native-hitl/v1/decisions")]) == 1


def test_other_agent_header_cannot_claim_the_original_pending_or_question(team_console):
    h = team_console
    wrong = {**h.headers, "X-Console-Agent": h.other}
    read = h.ui.get("/api/native-hitl/team/pending/" + h.pid, headers=h.headers).json()
    for path, body in (
        ("decisions", decision_input(read, "allow_once")),
        ("answers", {"question": h.c.hitl.service.read_question(h.c.hitl.owner, h.question["question_id"]).reference.model_dump(mode="json"),
                     "answer": "Ann", "was_freeform": False}),
    ):
        before = len(h.calls)
        refused = h.ui.post("/api/native-hitl/team/" + path, headers=wrong, json=body)
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "NATIVE_HITL_SCOPE_DENIED"
        assert not any(method == "POST" for method, _path, _body in h.calls[before:])


def test_team_question_without_local_thread_keeps_the_original_answer_id(team_console):
    h = team_console
    question = h.c.hitl.service.read_question(h.c.hitl.owner, h.question["question_id"])
    body = {"question": question.reference.model_dump(mode="json"), "answer": "Ann", "was_freeform": False}
    result = h.ui.post("/api/native-hitl/team/answers", headers=h.headers, json=body)
    assert result.status_code == 200, result.text
    again = h.ui.post("/api/native-hitl/team/answers", headers=h.headers, json={**body, "answer": "Bo"})
    assert again.status_code == 200
    assert again.json()["commands"][0]["command"]["answer"] == "Ann"
    assert len([call for call in h.calls if call[:2] == ("POST", "/native-hitl/v1/answers")]) == 1
    assert not any(path == "/native-hitl/v1/decisions" for _method, path, _body in h.calls)
    checked = h.ui.post("/api/native-hitl/team/answer-reconcile", headers=h.headers,
                        json={"request_id": result.json()["request_id"]})
    assert checked.status_code == 200, checked.text
    assert len([call for call in h.calls if call[:2] == ("POST", "/native-hitl/v1/answers")]) == 1


def test_changed_team_pending_reference_is_refused_without_a_decision(team_console):
    h = team_console
    value = h.ui.get("/api/native-hitl/team/pending/" + h.pid, headers=h.headers).json()
    body = decision_input(value, "allow_once")
    body["approval"]["pending_digest"] = "f" * 64
    before = len(h.calls)
    refused = h.ui.post("/api/native-hitl/team/decisions", headers=h.headers, json=body)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "NATIVE_HITL_ACTION_CHANGED"
    assert not any(method == "POST" for method, _path, _body in h.calls[before:])


def test_lost_team_decision_ack_only_reconciles_the_original_target(team_console):
    h = team_console
    value = h.ui.get("/api/native-hitl/team/pending/" + h.pid, headers=h.headers).json()
    original_handle = h.app.state.agent_consoles.resolve(h.target).backend._transport.handle_request

    def lose(request):
        response = original_handle(request)
        if request.method == "POST" and request.url.path == "/native-hitl/v1/decisions":
            raise httpx.ReadTimeout("OFFLINE lost original Team decision acknowledgment", request=request)
        return response

    target_console = h.app.state.agent_consoles.resolve(h.target)
    with httpx.Client(transport=httpx.MockTransport(lose)) as lost:
        adapter = target_console.native_instructions.hitl.adapter
        original_client = adapter.client
        adapter.client = lost
        try:
            failed = h.ui.post("/api/native-hitl/team/decisions", headers=h.headers,
                               json=decision_input(value, "allow_once"))
        finally:
            adapter.client = original_client
    assert failed.status_code == 503
    rid, original = target_console.journal.entries("native-hitl")[0]
    assert original["state"] == "UNKNOWN"
    assert h.ui.post("/api/native-hitl/team/decisions", headers=h.headers,
                     json=decision_input(value, "allow_once")).status_code == 200
    checked = h.ui.post("/api/native-hitl/team/reconcile", headers=h.headers, json={"request_id": rid})
    assert checked.status_code == 200, checked.text
    assert target_console.journal.get(rid, "native-hitl") == original
    assert len([call for call in h.calls if call[:2] == ("POST", "/native-hitl/v1/decisions")]) == 1


def test_team_discovery_reads_both_kinds_without_creating_local_turns(team_console):
    h = team_console
    before = len(h.calls)
    response = h.ui.get("/api/native-hitl/team/attention", headers={**h.headers, "X-Console-Agent": h.other})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["team_id"] == h.c.hitl.service.rules(h.c.hitl.owner).team_id
    assert {entry["item"]["item_id"] for entry in value["items"]} == {h.pid, h.question["question_id"]}
    assert h.pages == [(20, None)]
    assert all(method == "GET" for method, _path, _body in h.calls[before:])
    assert h.app.state.agent_consoles.resolve(h.other).instructions.store.command(h.draft["request_id"]) is None


@pytest.mark.parametrize("suffix", ["?team_id=other", "?limit=99", "?cursor=x&cursor=y"])
def test_team_discovery_rejects_extra_local_selectors(team_console, suffix):
    h = team_console
    before = len(h.calls)
    result = h.ui.get("/api/native-hitl/team/attention" + suffix, headers=h.headers)
    assert result.status_code == 403
    assert len(h.calls) == before
