"""Real A/B policy producers behind the console; local transport, no Hosted execution.

Second-hop projection fixtures do not execute the B consumer or claim an A SDK continuation.
"""

import asyncio
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from shared_context.behavior_channels_pin_refresh import core_code_sha256
from shared_context.behavior_assistant import _LocalHttp
from shared_context.behavior_profile_selection import SelectConversationProfile
from shared_context.behavior_peer_runtime import install_peer_routing
from web_test_console.adapter import canonical, parse_config, sha
from web_test_console.agent_connections import AgentConnections
from web_test_console.bridge import create_app
from web_test_console.instructions import InstructionConnection
from test_agent_runtime import peer_runtime
from test_agent_message_api import application as peer_application
from test_conversation_profile_selection import template
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_web_test_console_standing import configuration


@pytest.fixture
def peer_console(request, tmp_path, signing_key):
    api_loop, ui_loop, restart_loop = (asyncio.new_event_loop() for _ in range(3))
    try:
        peer = request.getfixturevalue("peer_runtime")
        c, a, b = peer.c, peer.a, peer.b
        aid, bid = a.config.targets[0].agent_id, b.agent_id
        entries, configs, connections = [], [], []
        for number, (identity, composition) in enumerate(((aid, a), (bid, b))):
            source = SimpleNamespace(runtime=composition.runtime, verifier=composition.verifier,
                                     config=composition.runtime.dep.delivery.contract)
            raw = configuration(source, SimpleNamespace(policy=composition.runtime.work_policy.policy))
            raw["console_id"] = "real-peer-ui-" + str(number)
            config = parse_config(canonical(raw))
            link = InstructionConnection(schema_version="fa-console-instructions-connection-v1",
                configuration_sha256=config.sha256, backend_core_sha256=core_code_sha256(),
                state_profile=getattr(request, "param", None))
            config_path, link_path = tmp_path / f"config-{number}.json", tmp_path / f"link-{number}.json"
            config_path.write_bytes(canonical(config.model_dump(mode="json")))
            link_path.write_bytes(canonical(link.model_dump(mode="json")))
            entries.append({
                "agent_id": identity, "config_path": str(config_path), "config_sha256": sha(config_path.read_bytes()),
                "instructions_path": str(link_path), "instructions_sha256": sha(link_path.read_bytes()),
                "journal_path": str(tmp_path / f"journal-{number}.sqlite3"),
            })
            configs.append(config)
            connections.append(link)
        manifest = AgentConnections.model_validate_json(canonical({
            "schema_version": "fa-console-agents-v1", "default_agent_id": aid, "agents": entries,
        }))
        calls = []
        messages = SimpleNamespace(body=None)
        faults = SimpleNamespace(lose_submit=False, template=None)
        with ExitStack() as stack:
            api_app = peer_application(a)
            install_peer_routing(api_app, a, peer_application)
            api = stack.enter_context(TestClient(api_app, backend_options={"loop_factory": lambda: api_loop}))
            local = _LocalHttp(api)

            def send(message):
                calls.append((message.method, message.url.path, message.content))
                assert message.url.path == "/work-policy/v1/agents" or any(
                    message.url.path.startswith("/agents/" + identity + "/") for identity in (aid, bid))
                if (faults.template is not None and message.method == "GET"
                        and message.url.path == "/agents/" + bid + "/work-policy/v1/agent-template"):
                    return httpx.Response(faults.template[0], json={"error": {"code": faults.template[1]}},
                                          headers={"cache-control": "no-store"})
                if message.url.path.endswith("/agent-messages") and messages.body is not None:
                    return httpx.Response(200, json=messages.body, headers={"cache-control": "no-store"})
                response = local.handle_request(message)
                if faults.lose_submit and message.url.path == "/agents/" + bid + "/work-policy/v1/submit":
                    raise httpx.ReadTimeout("synthetic peer admission response loss")
                return response

            backend = stack.enter_context(httpx.Client(transport=httpx.MockTransport(send)))
            app = create_app(configs[0], entries[0]["journal_path"], agent_connections=manifest,
                instruction_connection=connections[0], port=8874, backend=backend, clock=lambda: c.h.time,
                token_provider=lambda: c.h.headers(signing_key, "Behavior.Read Behavior.Approve")["Authorization"][7:])
            ui = stack.enter_context(TestClient(app, base_url="http://127.0.0.1:8874",
                                                backend_options={"loop_factory": lambda: ui_loop}))
            ui.get("/")
            headers = {"Origin": "http://127.0.0.1:8874",
                       "X-Console-CSRF": ui.get("/api/state").json()["csrf"], "X-Console-Agent": bid}
            logged_in = ui.post("/api/authenticate", json={}, headers=headers)
            assert logged_in.status_code == 200, logged_in.text
            yield SimpleNamespace(ui=ui, app=app, headers=headers, calls=calls, c=c, a=a, b=b, aid=aid, bid=bid,
                                  configs=configs, entries=entries, messages=messages, faults=faults,
                                  backend=backend, manifest=manifest, connections=connections,
                                  restart_loop=restart_loop, signing_key=signing_key, api=api)
    finally:
        for loop in (api_loop, ui_loop, restart_loop):
            if not loop.is_closed():
                loop.close()


def test_peer_template_selection_uses_its_original_scope_and_journal_once(peer_console):
    c = peer_console
    before = Path(c.entries[0]["journal_path"]).read_bytes()
    current = c.ui.get("/api/agent-template/selection", headers=c.headers)
    assert current.status_code == 200, current.text
    command = SelectConversationProfile(request_id="one-human-selection", expected_selection_id=None, template=template())
    selected = c.ui.post("/api/agent-template/select", json=command.model_dump(mode="json"), headers=c.headers)
    assert selected.status_code == 200, selected.text
    again = c.ui.post("/api/agent-template/select", json=command.model_dump(mode="json"), headers=c.headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "TEMPLATE_SELECTION_ALREADY_ATTEMPTED"
    assert len([call for call in c.calls if call[0] == "POST"]) == 1
    assert Path(c.entries[0]["journal_path"]).read_bytes() == before
    assert c.a.runtime.work_policy is c.c.control and c.c.sessions.calls == []


def test_published_directory_and_both_prefixes_reach_only_the_original_agent(peer_console):
    c = peer_console
    listed = c.ui.get("/api/agents").json()
    assert listed["default_agent_id"] == c.aid
    assert {entry["id"] for entry in listed["agents"] if entry["enabled"]} == {c.aid, c.bid}
    a_headers = {**c.headers, "X-Console-Agent": c.aid}
    state = c.ui.get("/api/instructions/models", headers=a_headers)
    assert state.status_code == 200, state.text
    request_id = state.json()["composer"]["request_id"]
    submitted = c.ui.post("/api/instructions/" + request_id + "/submit",
        json={"content": "Only this original A work.", "model": "gpt-4.1-mini"}, headers=a_headers)
    assert submitted.status_code == 200, submitted.text
    task = submitted.json()["task"]
    messages = c.ui.get("/api/instructions/" + request_id + "/agent-messages", headers=a_headers)
    assert messages.status_code == 200, messages.text
    assert messages.json() == {
        "schema_version": "fa-agent-message-list-v1", "agent_id": c.aid,
        "conversation_id": task["admission"]["conversation_id"], "binding_id": task["binding_id"], "messages": [],
    }
    foreign = c.api.get(
        f"/agents/{c.bid}/behavior/v1/bindings/{task['binding_id']}/jobs/{task['job_id']}",
        headers=c.c.h.headers(c.signing_key, "Behavior.Read"))
    assert foreign.status_code == 403
    count = len(c.calls)
    wrong = c.ui.get("/api/instructions/" + request_id + "/agent-messages", headers=c.headers)
    assert wrong.status_code == 403 and len(c.calls) == count
    assert task["binding_id"] in c.a.runtime.bindings and task["binding_id"] not in c.b.runtime.bindings
    assert c.c.sessions.calls == []


@pytest.fixture
def connectors_on(monkeypatch):
    from shared_context.team_connector_wire import TEAM_CONNECTORS_ENABLED_ENV
    monkeypatch.setenv(TEAM_CONNECTORS_ENABLED_ENV, "1")


def test_each_agent_connector_switch_reads_and_writes_only_that_agent(connectors_on, peer_console):
    c = peer_console
    a_headers = {**c.headers, "X-Console-Agent": c.aid}

    def read(headers):
        response = c.ui.get("/api/agent-connectors", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    def post(request_id, expected, disabled):
        return c.ui.post("/api/agent-connectors", headers=c.headers, json={
            "schema_version": "fa-agent-connectors-command-v1", "request_id": request_id,
            "expected_revision": expected, "disabled_connector_ids": disabled})

    def shown(revision, on):
        return {"schema_version": "fa-console-agent-connectors-v1", "revision": revision,
                "applies_to": "new-conversations-only", "can_manage": True,
                "connectors": [{"connector_id": "teams", "display_name": "Microsoft Teams", "on": on}]}

    assert read(c.headers) == shown(0, True) and read(a_headers) == shown(0, True)
    off = post("b" * 32, 0, ["teams"])
    assert (off.status_code, off.json()) == (200, shown(1, False))
    assert read(a_headers) == shown(0, True), "the other Agent keeps its own choice"
    again = post("b" * 32, 0, ["teams"])
    assert (again.status_code, again.json()) == (200, shown(1, False)), "the same ID returns its original result"
    stale = post("c" * 32, 0, [])
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "AGENT_CONNECTORS_CHANGED")
    on = post("d" * 32, 1, [])
    assert (on.status_code, on.json()) == (200, shown(2, True))
    paths = {path for method, path, _ in c.calls if method == "POST"}
    assert paths == {"/agents/" + c.bid + "/work-policy/v1/agent-connectors"}


@pytest.mark.parametrize("status,code", [(409, "AGENT_TEMPLATE_SOURCE_CHANGED"), (404, "AGENT_TEMPLATE_PROFILE_UNSUPPORTED")])
def test_only_a_peer_without_a_versioned_template_keeps_its_lane(peer_console, status, code):
    """B has no prompt profile or selection: its turns keep the original lane, while any other template failure refuses."""
    c = peer_console
    panel = c.ui.get("/api/agent-template", headers=c.headers)
    assert panel.status_code == 409 and panel.json()["error"]["code"] == "TEMPLATE_UNAVAILABLE"
    assert c.ui.get("/api/instructions/models", headers=c.headers).status_code == 200
    c.faults.template = (status, code)
    refused = c.ui.get("/api/instructions/models", headers=c.headers)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "TEMPLATE_UNAVAILABLE"
    assert refused.json()["error"]["upstream_status"] == status
    assert not [call for call in c.calls if call[0] == "POST"]


@pytest.mark.parametrize("peer_console", ["foundry-native-v1"], indirect=True)
def test_native_connection_without_a_versioned_template_still_refuses_before_any_write(peer_console):
    """Native capture rejects an Agent without a versioned template, so only job-bound connections keep their lane."""
    c = peer_console
    refused = c.ui.get("/api/instructions/models", headers=c.headers)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "TEMPLATE_UNAVAILABLE"
    assert refused.json()["error"]["upstream_status"] == 409
    assert not [call for call in c.calls if call[0] == "POST"]


@pytest.mark.parametrize("memory", [False, True], ids=["ordinary", "own-memory"])
def test_peer_submission_keeps_its_declared_preflight_scope_and_original_request(peer_console, memory):
    c = peer_console
    if memory:
        from shared_context.memory_bridge_settings import MemoryBridgeSettingsService, SelectMemoryBridgeSettings
        MemoryBridgeSettingsService(c.b.runtime).select(c.c.human, SelectMemoryBridgeSettings(
            schema_version="fa-memory-bridge-settings-command-v1", request_id="peer-memory-choice",
            expected_settings_id=None, enabled=True))
    state = c.ui.get("/api/instructions/models", headers=c.headers)
    assert state.status_code == 200, state.text
    request_id = state.json()["composer"]["request_id"]
    before = Path(c.entries[0]["journal_path"]).read_bytes()
    body = {"content": "Keep this original peer request.", "model": "gpt-4.1-mini"}
    path = "/api/instructions/" + request_id + "/submit"
    accepted = c.ui.post(path, json=body, headers=c.headers)
    assert accepted.status_code == 200, accepted.text
    task = accepted.json()["task"]
    assert task["task_id"] == request_id
    assert task["binding_id"] in c.b.runtime.bindings and task["binding_id"] not in c.a.runtime.bindings
    inspected = c.ui.post("/api/tasks/" + request_id + "/status", json={}, headers=c.headers)
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["task"]["execution"]["observation"] is not None, inspected.text
    if memory:
        assert c.b.runtime.work_policy.records[task["binding_id"]].profile.memory_scope.enabled is True
    repeated = c.ui.post(path, json=body, headers=c.headers)
    assert repeated.status_code == 200, repeated.text
    assert len([call for call in c.calls if call[0] == "POST"
                and call[1] == "/agents/" + c.bid + "/work-policy/v1/submit"]) == 1
    assert Path(c.entries[0]["journal_path"]).read_bytes() == before
    assert c.c.sessions.calls == []


def test_peer_unknown_submission_survives_a_new_bridge_and_reconciles_only_its_original_command(peer_console):
    c = peer_console
    state = c.ui.get("/api/instructions/models", headers=c.headers).json()
    request_id = state["composer"]["request_id"]
    path = "/api/instructions/" + request_id + "/submit"
    body = {"content": "Preserve the original B intent.", "model": "gpt-4.1-mini"}
    a_before = Path(c.entries[0]["journal_path"]).read_bytes()
    c.faults.lose_submit = True
    failed = c.ui.post(path, json=body, headers=c.headers)
    assert failed.status_code == 503
    original = c.app.state.agent_consoles.resolve(c.bid).journal.get(request_id, "submit")
    assert original["state"] == "UNKNOWN"
    c.faults.lose_submit = False
    app = create_app(c.configs[0], c.entries[0]["journal_path"], agent_connections=c.manifest,
        instruction_connection=c.connections[0], port=8875, backend=c.backend, clock=lambda: c.c.h.time,
        token_provider=lambda: c.c.h.headers(c.signing_key, "Behavior.Read Behavior.Approve")["Authorization"][7:])
    with TestClient(app, base_url="http://127.0.0.1:8875",
                    backend_options={"loop_factory": lambda: c.restart_loop}) as ui:
        ui.get("/")
        headers = {"Origin": "http://127.0.0.1:8875", "X-Console-CSRF": ui.get("/api/state").json()["csrf"],
                   "X-Console-Agent": c.bid}
        assert ui.post("/api/authenticate", json={}, headers=headers).status_code == 200
        assert ui.get("/api/instructions/models", headers=headers).status_code == 200
        repeated = ui.post(path, json=body, headers=headers)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["task"]["submission"]["state"] == "UNKNOWN"
        reconciled = ui.post("/api/tasks/" + request_id + "/submit-reconcile", json={}, headers=headers)
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["task"]["admission"] is not None
        assert app.state.agent_consoles.resolve(c.bid).journal.get(request_id, "submit") == original
    assert len([call for call in c.calls if call[0] == "POST"
                and call[1] == "/agents/" + c.bid + "/work-policy/v1/submit"]) == 1
    assert Path(c.entries[0]["journal_path"]).read_bytes() == a_before
    assert c.c.sessions.calls == []


def test_agent_message_projection_is_bound_to_original_work_and_never_a_human_receipt(peer_console):
    c = peer_console
    state = c.ui.get("/api/instructions/models", headers=c.headers).json()
    request_id = state["composer"]["request_id"]
    submitted = c.ui.post("/api/instructions/" + request_id + "/submit",
        json={"content": "Original peer work.", "model": "gpt-4.1-mini"}, headers=c.headers)
    assert submitted.status_code == 200, submitted.text
    task = submitted.json()["task"]
    cid = task["admission"]["conversation_id"]
    c.messages.body = {
        "schema_version": "fa-agent-message-list-v1", "agent_id": c.bid,
        "conversation_id": cid, "binding_id": task["binding_id"],
        "messages": [{
            "message_key": "original-handoff", "message_id": "original-message", "root_request_id": "original-root",
            "sender_agent_id": c.aid, "recipient_agent_id": c.bid,
            "from_work_id": "parent-work", "to_work_id": task["work_id"],
            "from_conversation_id": "parent-conversation", "to_conversation_id": cid,
            "kind": "result", "hop": 1, "status": "completed",
            "decision": {"action": "silence", "reason": "original_result_saved",
                         "settles_human_obligation": False, "model_dispatch_authorized": False},
            "human_obligation_fulfilled": False,
        }],
    }
    before = [Path(entry["journal_path"]).read_bytes() for entry in c.entries]
    path = "/api/instructions/" + request_id + "/agent-messages"
    response = c.ui.get(path, headers=c.headers)
    assert response.status_code == 200, response.text
    assert response.json()["agent_id"] == c.bid
    assert response.json()["messages"][0]["human_obligation_fulfilled"] is False
    c.messages.body["messages"][0]["content"] = "PRIVATE_PEER_CONTENT"
    invalid = c.ui.get(path, headers=c.headers)
    assert invalid.status_code == 502 and "PRIVATE_PEER_CONTENT" not in invalid.text
    del c.messages.body["messages"][0]["content"]
    c.messages.body["agent_id"] = c.aid
    wrong = c.ui.get(path, headers=c.headers)
    assert wrong.status_code == 502 and wrong.json()["error"]["code"] == "AGENT_MESSAGES_INVALID"
    assert [Path(entry["journal_path"]).read_bytes() for entry in c.entries] == before
    assert all(method == "GET" for method, route, _ in c.calls if route.endswith("/agent-messages"))
    assert c.c.sessions.calls == []


@pytest.mark.parametrize("kind,status", [
    ("result", "completed"), ("blocked", "needs_attention"), ("blocked", "unknown"),
], ids=["completed-return", "blocked-return", "unknown-return"])
def test_second_hop_projection_keeps_original_a_pending_and_never_creates_b_submission(peer_console, kind, status):
    c = peer_console
    a_headers = {**c.headers, "X-Console-Agent": c.aid}
    draft = c.ui.get("/api/instructions/models", headers=a_headers).json()["composer"]
    request_id = draft["request_id"]
    admitted = c.ui.post("/api/instructions/" + request_id + "/submit", headers=a_headers,
        json={"content": "Keep the original A result and delivery obligation.", "model": "gpt-4.1-mini"})
    assert admitted.status_code == 200, admitted.text
    task = admitted.json()["task"]
    cid = task["admission"]["conversation_id"]
    b_state = c.ui.get("/api/state", headers=c.headers).json()
    assert all(item["instruction"] is None for item in b_state["tasks"])
    before = [Path(entry["journal_path"]).read_bytes() for entry in c.entries]
    retained = {field: task[field] for field in ("submission", "reply", "acknowledgment", "admission")}
    handoff = {
        "message_key": "original-handoff", "message_id": "handoff-one", "root_request_id": "original-root",
        "sender_agent_id": c.aid, "recipient_agent_id": c.bid,
        "from_work_id": task["work_id"], "to_work_id": "peer-work-without-local-submit",
        "from_conversation_id": cid, "to_conversation_id": "peer-conversation",
        "kind": "handoff", "hop": 1, "status": "completed",
        "decision": {"action": "queued", "reason": "original_handoff_consumed",
                     "settles_human_obligation": False, "model_dispatch_authorized": False},
        "human_obligation_fulfilled": False,
    }
    returned = {
        **handoff, "message_key": "original-return", "message_id": "return-two",
        "sender_agent_id": c.bid, "recipient_agent_id": c.aid,
        "from_work_id": handoff["to_work_id"], "to_work_id": task["work_id"],
        "from_conversation_id": handoff["to_conversation_id"], "to_conversation_id": cid,
        "kind": kind, "hop": 2, "status": status,
        "decision": {"action": "silence" if kind == "result" else "needs_attention",
                     "reason": "original_result_saved" if kind == "result" else "original_peer_blocked",
                     "settles_human_obligation": False, "model_dispatch_authorized": False},
    }
    c.messages.body = {
        "schema_version": "fa-agent-message-list-v1", "agent_id": c.aid,
        "binding_id": task["binding_id"], "conversation_id": cid, "messages": [handoff, returned],
    }
    count = len(c.calls)
    path = "/api/instructions/" + request_id + "/agent-messages"
    for _ in range(2):
        response = c.ui.get(path, headers=a_headers)
        assert response.status_code == 200, response.text
        assert response.json() == c.messages.body
    current = next(item for item in c.ui.get("/api/state", headers=a_headers).json()["tasks"]
                   if item["task_id"] == request_id)
    assert {field: current[field] for field in retained} == retained
    assert current["reply"] is None and current["acknowledgment"]["state"] == "NOT_SENT"
    assert current["admission"]["status"]["state"] == "queued"
    assert c.ui.get("/api/state", headers=c.headers).json()["composer"] == b_state["composer"]
    assert [Path(entry["journal_path"]).read_bytes() for entry in c.entries] == before
    assert [(method, route) for method, route, _ in c.calls[count:]] == [
        ("GET", f"/agents/{c.aid}/work-policy/v1/bindings/{task['binding_id']}/agent-messages"),
        ("GET", f"/agents/{c.aid}/work-policy/v1/bindings/{task['binding_id']}/agent-messages"),
    ]
    c.messages.body["messages"][1]["to_conversation_id"] = "not-the-original-a-conversation"
    wrong = c.ui.get(path, headers=a_headers)
    assert wrong.status_code == 502 and wrong.json()["error"]["code"] == "AGENT_MESSAGES_INVALID"
    assert [Path(entry["journal_path"]).read_bytes() for entry in c.entries] == before
    assert c.c.sessions.calls == []
