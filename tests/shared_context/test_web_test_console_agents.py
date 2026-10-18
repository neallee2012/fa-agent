"""Agent routing through the existing local HTTP boundary and separate synthetic journals."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from web_test_console import __main__ as launcher
from web_test_console.agent_connections import AgentConnections, AgentConsoles
from web_test_console.adapter import ConsoleError, canonical, parse_config, sha
from web_test_console.bridge import create_app
from web_test_console.instructions import InstructionConnection
from test_m3_auth import jwk_client, signing_key
from test_web_test_console_frozen_interface import real_interface
from test_web_test_console_standing import configuration


def directory_payload():
    return {
        "schema_version": "fa-agent-directory-v1", "default_agent_id": "agent-one",
        "agents": [
            {"agent_id": "agent-one", "display_name": "First assistant", "api_prefix": "/agents/agent-one",
             "state": "ready", "reason": None},
            {"agent_id": "agent-two", "display_name": "Second assistant", "api_prefix": "/agents/agent-two",
             "state": "ready", "reason": None},
        ],
    }


def connection_file(c, tmp_path):
    config = tmp_path / "original-config.json"
    config.write_bytes(canonical(c.config.model_dump(mode="json")))
    instructions = tmp_path / "original-instructions.json"
    instructions.write_bytes(canonical(c.connection.model_dump(mode="json")))
    first = {
        "agent_id": "agent-one", "config_path": str(config), "config_sha256": sha(config.read_bytes()),
        "journal_path": str(c.journal), "instructions_path": str(instructions),
        "instructions_sha256": sha(instructions.read_bytes()),
    }
    second = {
        "agent_id": "agent-two", "config_path": str(tmp_path / "missing-peer-config.json"),
        "config_sha256": "f" * 64, "journal_path": str(tmp_path / "peer.sqlite3"),
        "instructions_path": str(tmp_path / "missing-peer-instructions.json"), "instructions_sha256": "e" * 64,
    }
    raw = {"schema_version": "fa-console-agents-v1", "default_agent_id": "agent-one", "agents": [first, second]}
    path = tmp_path / "agents.json"
    path.write_bytes(canonical(raw))
    args = ["serve", "--config", str(config), "--config-sha256", first["config_sha256"],
            "--journal", str(c.journal), "--instructions", str(instructions),
            "--instructions-sha256", first["instructions_sha256"],
            "--agents", str(path), "--agents-sha256", sha(path.read_bytes())]
    return SimpleNamespace(raw=raw, path=path, args=args, first=first, second=second)


def supplied_peer(c, manifest):
    """Use the server test fixture to supply a complete B policy; the client never derives it."""
    from test_agent_scope_registry import peer_policy

    server = SimpleNamespace(targets=(None, SimpleNamespace(
        agent_id="agent-two", agent_oid="22222222-2222-2222-2222-222222222222")))
    policy = peer_policy(server, c.config.standing_policy)
    value = configuration(c.h, SimpleNamespace(policy=policy))
    value["console_id"] = "offline-peer-ui"
    config = parse_config(canonical(value))
    connection = InstructionConnection(
        schema_version="fa-console-instructions-connection-v1",
        configuration_sha256=config.sha256, backend_core_sha256=c.connection.backend_core_sha256)
    entry = manifest.second
    Path(entry["config_path"]).write_bytes(canonical(config.model_dump(mode="json")))
    Path(entry["instructions_path"]).write_bytes(canonical(connection.model_dump(mode="json")))
    entry["config_sha256"] = sha(Path(entry["config_path"]).read_bytes())
    entry["instructions_sha256"] = sha(Path(entry["instructions_path"]).read_bytes())
    manifest.path.write_bytes(canonical(manifest.raw))
    return config


def test_launcher_accepts_only_the_pinned_agent_connections_without_touching_original_journal(
    real_interface, tmp_path, monkeypatch,
):
    c = real_interface
    manifest = connection_file(c, tmp_path)
    calls = []
    monkeypatch.setattr(launcher, "create_app", lambda *args, **kwargs: calls.append((args, kwargs)) or object())
    monkeypatch.setattr(launcher.uvicorn, "run", lambda *args, **kwargs: None)
    before = c.journal.read_bytes()
    assert launcher.main(manifest.args) == 0
    supplied = calls[0][1]["agent_connections"]
    assert supplied.default_agent_id == "agent-one"
    assert [entry.agent_id for entry in supplied.agents] == ["agent-one", "agent-two"]
    assert calls[0][0][0] == c.config and str(calls[0][0][1]) == str(c.journal)
    assert calls[0][1]["instruction_connection"] == c.connection
    assert c.journal.read_bytes() == before
    assert launcher.main(manifest.args[:-1] + ["0" * 64]) == 2
    assert launcher.main(manifest.args[:-2]) == 2
    assert len(calls) == 1


def test_server_directory_selects_ids_without_falling_back_for_a_missing_peer_config(real_interface, tmp_path):
    c = real_interface
    manifest = connection_file(c, tmp_path)
    connections = AgentConnections.model_validate_json(manifest.path.read_bytes())
    calls = []
    directory = directory_payload()

    def backend(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/work-policy/v1/agents":
            assert request.method == "GET"
            return httpx.Response(200, json=directory, headers={"cache-control": "no-store"})
        assert request.url.path.startswith("/agents/agent-one/")
        path = request.url.path.removeprefix("/agents/agent-one")
        return c.backend.send(httpx.Request(request.method, request.url.copy_with(path=path),
                                           headers=request.headers, content=request.content))

    before = c.journal.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(backend)) as upstream:
        app = create_app(c.config, c.journal, backend=upstream,
            token_provider=lambda: c.h.headers()["Authorization"].removeprefix("Bearer "),
            clock=lambda: c.h.time, port=8873, instruction_connection=c.connection, agent_connections=connections)
        with TestClient(app, base_url="http://127.0.0.1:8873") as ui:
            ui.get("/")
            initial = ui.get("/api/state").json()
            headers = {"Origin": "http://127.0.0.1:8873", "X-Console-CSRF": initial["csrf"]}
            logged_in = ui.post("/api/authenticate", json={}, headers=headers)
            assert logged_in.status_code == 200, logged_in.text
            listed = ui.get("/api/agents")
            assert listed.status_code == 200, listed.text
            assert listed.json()["default_agent_id"] == "agent-one"
            first, second = listed.json()["agents"]
            assert first["id"] == "agent-one" and first["enabled"]
            assert second["id"] == "agent-two" and not second["enabled"]
            assert second["reason"] == "AGENT_CONFIGURATION_MISSING"
            state = ui.get("/api/state", headers={"X-Console-Agent": "agent-one"})
            assert state.status_code == 200, state.text
            assert state.json()["selected_agent_id"] == "agent-one"
            assert state.json()["composer"]["request_id"] == c.browser.ui.get("/api/state").json()["composer"]["request_id"]
            for identity, expected in (("agent-two", "AGENT_CONFIGURATION_MISSING"),
                                       ("unlisted", "AGENT_NOT_AVAILABLE")):
                denied = ui.get("/api/instructions/progress", headers={"X-Console-Agent": identity})
                assert denied.status_code == 409 and denied.json()["error"]["code"] == expected
            missing = ui.get("/api/instructions/progress")
            assert missing.status_code == 409 and missing.json()["error"]["code"] == "AGENT_SELECTION_REQUIRED"
    assert c.journal.read_bytes() == before
    assert not (tmp_path / "peer.sqlite3").exists()
    assert all(method == "GET" for method, _ in calls)


def test_directory_beyond_two_agents_lists_every_usable_entry_without_local_fallback(real_interface, tmp_path):
    from shared_context.behavior_agent_message_wire import MAX_TEAM_AGENTS

    page = Path(create_app.__code__.co_filename).with_name("index.html").read_text(encoding="utf-8")
    assert f"value.agents.length>{MAX_TEAM_AGENTS}" in page
    c = real_interface
    manifest = connection_file(c, tmp_path)
    connections = AgentConnections.model_validate_json(manifest.path.read_bytes())
    directory = directory_payload()
    directory["agents"] += [
        {"agent_id": "agent-three", "display_name": "Third assistant", "api_prefix": "/agents/agent-three",
         "state": "ready", "reason": None},
        {"agent_id": "agent-four", "display_name": "Fourth assistant", "api_prefix": "/agents/agent-four",
         "state": "unavailable", "reason": "AGENT_SCOPE_MISSING"},
    ]
    oversized = directory_payload()
    oversized["agents"] += [
        {"agent_id": f"agent-extra-{index}", "display_name": f"Extra {index}",
         "api_prefix": f"/agents/agent-extra-{index}", "state": "unavailable", "reason": "AGENT_SCOPE_MISSING"}
        for index in range(MAX_TEAM_AGENTS - 1)
    ]
    served = [directory]

    def backend(request):
        if request.url.path == "/work-policy/v1/agents":
            assert request.method == "GET"
            return httpx.Response(200, json=served[0], headers={"cache-control": "no-store"})
        assert request.url.path.startswith("/agents/agent-one/")
        path = request.url.path.removeprefix("/agents/agent-one")
        return c.backend.send(httpx.Request(request.method, request.url.copy_with(path=path),
                                           headers=request.headers, content=request.content))

    before = c.journal.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(backend)) as upstream:
        app = create_app(c.config, c.journal, backend=upstream,
            token_provider=lambda: c.h.headers()["Authorization"].removeprefix("Bearer "),
            clock=lambda: c.h.time, port=8874, instruction_connection=c.connection, agent_connections=connections)
        with TestClient(app, base_url="http://127.0.0.1:8874") as ui:
            ui.get("/")
            initial = ui.get("/api/state").json()
            headers = {"Origin": "http://127.0.0.1:8874", "X-Console-CSRF": initial["csrf"]}
            assert ui.post("/api/authenticate", json={}, headers=headers).status_code == 200
            listed = ui.get("/api/agents")
            assert listed.status_code == 200, listed.text
            agents = {agent["id"]: agent for agent in listed.json()["agents"]}
            # P12: a target the server marks unavailable (for example a never-invoked executor) is not a chat
            # partner; it is listed only for support, and still cannot be selected.
            assert list(agents) == ["agent-one", "agent-two", "agent-three"]
            assert listed.json()["hidden_agents"] == [
                {"id": "agent-four", "name": "Fourth assistant", "reason": "AGENT_SCOPE_MISSING"}]
            assert agents["agent-one"]["enabled"]
            assert agents["agent-one"]["description"] == ""
            assert not agents["agent-three"]["enabled"]
            assert agents["agent-three"]["reason"] == "AGENT_CONFIGURATION_MISSING"
            for identity, expected in (("agent-three", "AGENT_CONFIGURATION_MISSING"),
                                       ("agent-four", "AGENT_NOT_AVAILABLE")):
                denied = ui.get("/api/instructions/progress", headers={"X-Console-Agent": identity})
                assert denied.status_code == 409 and denied.json()["error"]["code"] == expected
            unavailable_default = copy.deepcopy(directory)
            unavailable_default["agents"][0].update(state="unavailable", reason="WORK_POLICY_REVOKED")
            served[0] = unavailable_default
            kept = ui.get("/api/agents").json()
            assert [agent["id"] for agent in kept["agents"]] == ["agent-one", "agent-two", "agent-three"]
            assert kept["agents"][0]["enabled"] is False and kept["agents"][0]["reason"] == "WORK_POLICY_REVOKED"
            assert [agent["id"] for agent in kept["hidden_agents"]] == ["agent-four"]
            served[0] = oversized
            refreshed = ui.get("/api/agents")
            assert refreshed.status_code == 502
            assert refreshed.json()["error"]["code"] == "AGENT_DIRECTORY_INVALID"
    assert c.journal.read_bytes() == before
    assert not (tmp_path / "peer.sqlite3").exists()


def test_page_load_rereads_a_directory_older_than_a_minute_so_a_retired_agent_disappears(real_interface, tmp_path):
    from datetime import timedelta

    c = real_interface
    connections = AgentConnections.model_validate_json(connection_file(c, tmp_path).path.read_bytes())
    listed = directory_payload()
    listed["agents"][1].update(state="unavailable", reason="AGENT_SCOPE_MISSING")
    retired = directory_payload()
    retired["agents"].pop()
    served, reads = [listed], []

    def backend(request):
        if request.url.path == "/work-policy/v1/agents":
            reads.append(request.method)
            if served[0] is None:
                return httpx.Response(503, json={"error": {"code": "BACKEND_UNAVAILABLE"}})
            return httpx.Response(200, json=served[0], headers={"cache-control": "no-store"})
        path = request.url.path.removeprefix("/agents/agent-one")
        return c.backend.send(httpx.Request(request.method, request.url.copy_with(path=path),
                                           headers=request.headers, content=request.content))

    with httpx.Client(transport=httpx.MockTransport(backend)) as upstream:
        app = create_app(c.config, c.journal, backend=upstream,
            token_provider=lambda: c.h.headers()["Authorization"].removeprefix("Bearer "),
            clock=lambda: c.h.time, port=8873, instruction_connection=c.connection, agent_connections=connections)
        with TestClient(app, base_url="http://127.0.0.1:8873") as ui:
            ui.get("/")
            initial = ui.get("/api/state").json()
            assert reads == []  # not signed in: nothing is read
            headers = {"Origin": "http://127.0.0.1:8873", "X-Console-CSRF": initial["csrf"]}
            assert ui.post("/api/authenticate", json={}, headers=headers).status_code == 200
            assert reads == ["GET"]
            assert [agent["id"] for agent in ui.get("/api/state").json()["hidden_agents"]] == ["agent-two"]
            served[0] = retired
            assert [agent["id"] for agent in ui.get("/api/state").json()["hidden_agents"]] == ["agent-two"]
            assert reads == ["GET"]  # read within the last minute: reused
            c.h.time += timedelta(seconds=61)
            state = ui.get("/api/state").json()
            assert reads == ["GET", "GET"]
            assert state["hidden_agents"] == [] and [agent["id"] for agent in state["agents"]] == ["agent-one"]
            served[0] = None
            c.h.time += timedelta(seconds=61)
            failed = ui.get("/api/state")
            assert failed.status_code == 200 and failed.json()["agent_directory_error"] == "AGENT_DIRECTORY_UNAVAILABLE"
            assert reads == ["GET", "GET", "GET"]


def test_two_clients_keep_agent_models_drafts_and_fixed_task_ids_in_separate_journals(
    real_interface, tmp_path,
):
    c = real_interface
    manifest = connection_file(c, tmp_path)
    peer = supplied_peer(c, manifest)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    calls = []

    def backend(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/work-policy/v1/agents":
            return httpx.Response(200, json=directory_payload(), headers={"cache-control": "no-store"})
        prefix = next((prefix for prefix in ("/agents/agent-one", "/agents/agent-two")
                       if request.url.path.startswith(prefix + "/")), None)
        assert prefix is not None
        path = request.url.path.removeprefix(prefix)
        response = c.backend.send(httpx.Request(request.method, request.url.copy_with(path=path),
                                                headers=request.headers, content=request.content))
        if prefix == "/agents/agent-two" and path == "/work-policy/v1/models":
            value = response.json()
            value["default_model"] = "peer-only-model"
            value["models"] = [{**value["models"][0], "id": "peer-only-model", "display_name": "Peer model"}]
            return httpx.Response(200, json=value, headers={"cache-control": "no-store"})
        return response

    before = c.journal.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(backend)) as upstream:
        app = create_app(c.config, c.journal, backend=upstream,
            token_provider=lambda: c.h.headers()["Authorization"].removeprefix("Bearer "),
            clock=lambda: c.h.time, port=8873, instruction_connection=c.connection, agent_connections=links)
        with TestClient(app, base_url="http://127.0.0.1:8873") as first, TestClient(
            app, base_url="http://127.0.0.1:8873",
        ) as second:
            for ui in (first, second):
                ui.get("/")
            csrf = first.get("/api/state").json()["csrf"]
            headers = {"Origin": "http://127.0.0.1:8873", "X-Console-CSRF": csrf}
            assert first.post("/api/authenticate", json={}, headers=headers).status_code == 200
            a, b = {"X-Console-Agent": "agent-one"}, {"X-Console-Agent": "agent-two"}
            original = first.get("/api/instructions/models", headers=a)
            other = second.get("/api/instructions/models", headers=b)
            assert original.status_code == other.status_code == 200, (original.text, other.text)
            assert original.json()["composer"]["models"]["default_model"] == "gpt-4.1-mini"
            assert other.json()["composer"]["models"]["default_model"] == "peer-only-model"
            assert original.json()["selected_agent_id"] == "agent-one"
            assert other.json()["selected_agent_id"] == "agent-two"
            assert other.json()["console_id"] == peer.console_id
            request_a = original.json()["composer"]["request_id"]
            request_b = other.json()["composer"]["request_id"]
            assert request_a != request_b
            left = first.get("/api/tasks/A/summary", headers=a)
            right = second.get("/api/tasks/A/summary", headers=b)
            assert left.status_code == right.status_code == 403
            assert left.json()["error"]["code"] == right.json()["error"]["code"] == "ACTION_NOT_ALLOWED"
            assert first.get("/api/state", headers=a).json()["tasks"] == []
            assert second.get("/api/state", headers=b).json()["tasks"] == []
            denied = second.post("/api/instructions/" + request_a + "/submit",
                json={"content": "Do not send A from B.", "model": "peer-only-model"}, headers={**headers, **b})
            assert denied.status_code == 409 and denied.json()["error"]["code"] == "INSTRUCTION_SCOPE"
            assert first.get("/api/state", headers=a).json()["composer"]["request_id"] == request_a
            assert second.get("/api/state", headers=b).json()["composer"]["request_id"] == request_b
    assert c.journal.read_bytes() == before
    assert Path(manifest.second["journal_path"]).is_file()
    assert ("GET", "/agents/agent-one/work-policy/v1/models") in calls
    assert ("GET", "/agents/agent-two/work-policy/v1/models") in calls
    assert all(method == "GET" for method, _ in calls)


def test_directory_failures_and_changed_peer_config_never_use_cached_routes_or_rewrite_journals(
    real_interface, tmp_path,
):
    c = real_interface
    manifest = connection_file(c, tmp_path)
    supplied_peer(c, manifest)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    source = SimpleNamespace(status=200, body=directory_payload())
    calls = []

    def backend(request):
        calls.append((request.method, request.url.path))
        assert request.method == "GET" and request.url.path == "/work-policy/v1/agents"
        return httpx.Response(source.status, json=source.body, headers={"cache-control": "no-store"})

    with httpx.Client(transport=httpx.MockTransport(backend)) as upstream:
        app = create_app(c.config, c.journal, backend=upstream,
            token_provider=lambda: c.h.headers()["Authorization"].removeprefix("Bearer "),
            clock=lambda: c.h.time, port=8873, instruction_connection=c.connection, agent_connections=links)
        with TestClient(app, base_url="http://127.0.0.1:8873") as ui:
            ui.get("/")
            csrf = ui.get("/api/state").json()["csrf"]
            login = ui.post("/api/authenticate", json={}, headers={
                "Origin": "http://127.0.0.1:8873", "X-Console-CSRF": csrf})
            assert login.status_code == 200
            selected = {"X-Console-Agent": "agent-two"}
            assert ui.get("/api/state", headers=selected).status_code == 200
            before = [Path(entry["journal_path"]).read_bytes() for entry in manifest.raw["agents"]]
            for fault in ("404", "403", "default", "duplicate", "prefix"):
                source.status, source.body = 200, directory_payload()
                if fault.isdigit():
                    source.status = int(fault)
                    source.body = {"error": {"code": "UNAVAILABLE"}}
                elif fault == "default":
                    source.body["default_agent_id"] = "agent-two"
                elif fault == "duplicate":
                    source.body["agents"][1] = source.body["agents"][0]
                else:
                    source.body["agents"][1]["api_prefix"] = "/agents/agent-one"
                response = ui.get("/api/agents")
                assert response.status_code in {409, 502, 503}, (fault, response.text)
                count = len(calls)
                refused = ui.get("/api/instructions/progress", headers=selected)
                assert refused.status_code == 409
                assert len(calls) == count
                assert [Path(entry["journal_path"]).read_bytes() for entry in manifest.raw["agents"]] == before
            source.status, source.body = 200, directory_payload()
            Path(manifest.second["config_path"]).write_bytes(b"{}")
            directory = ui.get("/api/agents")
            assert directory.status_code == 200
            first, second = directory.json()["agents"]
            assert first["enabled"] and not second["enabled"]
            assert second["reason"] == "AGENT_CONFIGURATION_INVALID"
            assert ui.get("/api/state", headers=selected).status_code == 409
            assert ui.get("/api/state", headers={"X-Console-Agent": "agent-one"}).status_code == 200
            count = len(calls)
            denied = ui.get("/api/agents", headers={"Origin": "https://untrusted.invalid"})
            assert denied.status_code == 403 and len(calls) == count
            assert [Path(entry["journal_path"]).read_bytes() for entry in manifest.raw["agents"]] == before


def test_a_peer_pointing_at_another_configurations_journal_is_disabled_without_opening_it_for_write(
    real_interface, tmp_path,
):
    c = real_interface
    manifest = connection_file(c, tmp_path)
    supplied_peer(c, manifest)
    peer_journal = Path(manifest.second["journal_path"])
    original = c.journal.read_bytes()
    peer_journal.write_bytes(original)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json=directory_payload(), headers={"cache-control": "no-store"},
    ))) as backend:
        app = create_app(c.config, c.journal, backend=backend,
            token_provider=lambda: c.h.headers()["Authorization"].removeprefix("Bearer "),
            clock=lambda: c.h.time, port=8873, instruction_connection=c.connection, agent_connections=links)
        with TestClient(app, base_url="http://127.0.0.1:8873") as ui:
            ui.get("/")
            csrf = ui.get("/api/state").json()["csrf"]
            assert ui.post("/api/authenticate", json={}, headers={
                "Origin": "http://127.0.0.1:8873", "X-Console-CSRF": csrf,
            }).status_code == 200
            result = ui.get("/api/agents").json()
            assert result["agents"][0]["enabled"] is True
            assert result["agents"][1]["enabled"] is False
            assert result["agents"][1]["reason"] == "AGENT_CONFIGURATION_INVALID"
    assert c.journal.read_bytes() == peer_journal.read_bytes() == original


def _offline_agents(tmp_path, send):
    """AgentConsoles around a stand-in default console; only the directory read and sign-in are exercised."""
    from datetime import datetime, timezone

    first = {"agent_id": "agent-one", "config_path": str(tmp_path / "c.json"), "config_sha256": "a" * 64,
             "journal_path": str(tmp_path / "j.sqlite3"), "instructions_path": str(tmp_path / "i.json"),
             "instructions_sha256": "b" * 64}
    connections = AgentConnections.model_validate_json(json.dumps({"default_agent_id": "agent-one", "agents": [first]}))
    default = SimpleNamespace(config=None, backend=None, vault=SimpleNamespace(get=lambda: "token"),
                              clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))

    def authenticate(*, automatic, load_models):
        default.vault = SimpleNamespace(get=lambda: "renewed")
        return {"authentication": "AVAILABLE", "authentication_refresh_at": None}

    default.authenticate = authenticate
    agents = AgentConsoles(connections, default, None)
    agents.reader = SimpleNamespace(send=send)
    return agents


def test_a_silent_sign_in_refresh_answers_ok_when_only_the_directory_read_fails(tmp_path):
    def send(*args, **kwargs):
        raise ConsoleError("BACKEND_UNAVAILABLE", 503)

    agents = _offline_agents(tmp_path, send)
    result = agents.authenticate(automatic=True)
    assert result["authentication"] == "AVAILABLE"
    assert result["agent_directory_error"] == "AGENT_DIRECTORY_UNAVAILABLE"
    with pytest.raises(ConsoleError) as manual:  # a sign-in the user pressed still reports the failed read
        agents.authenticate(automatic=False)
    assert (manual.value.code, manual.value.status) == ("AGENT_DIRECTORY_UNAVAILABLE", 503)

def test_a_slow_directory_read_does_not_block_resolving_an_agent(tmp_path):
    import threading

    from shared_context.behavior_agent_message_wire import PublicAgentDirectory

    listed = directory_payload()
    listed["agents"].pop()
    entered, release = threading.Event(), threading.Event()

    def send(*args, **kwargs):
        entered.set()
        release.wait(10)
        return json.dumps(listed).encode(), {"cache-control": "no-store"}

    agents = _offline_agents(tmp_path, send)
    agents.directory, agents.error = PublicAgentDirectory.model_validate_json(json.dumps(listed)), None
    agents.loaded = {"agent-one": None}
    reading = threading.Thread(target=agents.refresh_directory)
    reading.start()
    try:
        assert entered.wait(5)
        resolved = []
        resolving = threading.Thread(target=lambda: resolved.append(agents.resolve("agent-one")))
        resolving.start()
        resolving.join(2)
        assert resolved == [agents.default], "resolve() waited for the directory HTTP read"
    finally:
        release.set()
        reading.join(5)
    assert agents.directory_read_at is not None and agents.error is None