"""CAP-06 PR5: 8765 onboarding is a thin client of the server record and writes only fixed local links."""

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from shared_context.behavior_agent_console_wire import AgentConsoleConnection
from web_test_console import __main__ as launcher
from web_test_console.adapter import canonical, sha
from web_test_console.agent_connections import AgentConnections
from web_test_console.agent_onboarding import PHASE
from web_test_console.bridge import Journal, create_app
from test_m3_auth import jwk_client, signing_key
from test_web_test_console_agents import connection_file, directory_payload
from test_web_test_console_frozen_interface import real_interface

NEW = "agent-new"


def new_policy(c):
    from test_agent_scope_registry import peer_policy

    server = SimpleNamespace(targets=(None, SimpleNamespace(
        agent_id=NEW, agent_oid="33333333-3333-3333-3333-333333333333")))
    return peer_policy(server, c.config.standing_policy)


class Server:
    """Server-side onboarding stand-in: one record per request ID, progress only on POST."""

    def __init__(self, c):
        self.c, self.calls, self.posts, self.records = c, [], [], {}
        self.lose_next_post, self.quota, self.link_fail = False, False, False
        self.directory_state = None
        # The new Agent's native environment: prepare is idempotent; preparation is 409 until prepared.
        self.prepares, self.prepared, self.prepare_refusal = [], None, None
        self.lose_prepare, self.preparation_fail = False, False
        self.prepare_errors, self.preparation_errors = [], []  # (status, code) per call, in order
        policy = new_policy(c)
        self.connection = AgentConsoleConnection(
            agent_id=NEW, display_name="Researcher", backend_configuration_sha256=policy.configuration_sha256,
            standing_policy=policy, standing_policy_sha256=policy.sha256)

    def directory(self):
        value = directory_payload()
        value["agents"] = value["agents"][:1]
        if self.directory_state is not None:
            state, reason = self.directory_state
            value["agents"].append({"agent_id": NEW, "display_name": "Researcher", "api_prefix": "/agents/" + NEW,
                                    "state": state, "reason": reason})
        return value

    def view(self, rid):
        record = self.records[rid]
        value = {"request_id": rid, "state": record["state"], "reason": None, "display_name": "Researcher",
                 "agent_name": "fa-agent-0123456789abcdef"}
        if record["state"] in {"registered", "ready"}:
            value["agent_id"] = NEW
        return value

    def __call__(self, request):
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        store = {"cache-control": "no-store"}
        if path == "/work-policy/v1/agents" and method == "GET":
            return httpx.Response(200, json=self.directory(), headers=store)
        if path == "/work-policy/v1/agents" and method == "POST":
            body = json.loads(request.content)
            self.posts.append(body)
            if self.quota:
                return httpx.Response(409, headers=store, json={"error": {
                    "code": "AGENT_QUOTA_REACHED", "message": "full", "continue_same_request": False,
                    "automatic_retry": False, "limit": 16}})
            rid = body["request_id"]
            record = self.records.setdefault(rid, {"body": body, "state": "admitted"})
            assert record["body"] == body
            record["state"] = {"admitted": "creating", "creating": "ready"}.get(record["state"], record["state"])
            self.directory_state = (("ready", None) if record["state"] == "ready"
                                    else ("unavailable", "AGENT_ONBOARDING_IN_PROGRESS"))
            if self.lose_next_post:
                self.lose_next_post = False
                raise httpx.ReadError("lost reply", request=request)
            return httpx.Response(200 if record["state"] == "ready" else 202, json=self.view(rid), headers=store)
        if path.startswith("/work-policy/v1/agents/onboarding/") and method == "GET":
            rid = path.rsplit("/", 1)[1]
            if rid not in self.records:
                return httpx.Response(404, headers=store, json={"error": {"code": "AGENT_ONBOARDING_NOT_FOUND"}})
            return httpx.Response(200, json=self.view(rid), headers=store)
        if path == "/work-policy/v1/agents/" + NEW + "/console-connection" and method == "GET":
            if self.link_fail:
                self.link_fail = False
                return httpx.Response(503, headers=store, json={"error": {"code": "BACKEND_UNAVAILABLE"}})
            return httpx.Response(200, content=self.connection.model_dump_json(), headers={
                **store, "content-type": "application/json"})
        native = "/agents/" + NEW + "/work-policy/v1/native-turns/"
        if path == native + "prepare" and method == "POST":
            body = json.loads(request.content)
            self.prepares.append(body)
            if self.prepare_errors:
                status, code = self.prepare_errors.pop(0)
                return httpx.Response(status, headers=store, json={"error": {"code": code}})
            if self.prepare_refusal is not None:
                code, self.prepare_refusal = self.prepare_refusal, None
                return httpx.Response(409, headers=store, json={"error": {"code": code}})
            if self.prepared is None:
                self.prepared = {"schema_version": "fa-native-session-preparation-v1", "agent_id": NEW,
                                 "request_id": body["request_id"], "previous_hosted_session_id": None,
                                 "hosted_session_id": "native-session-1", "hosted_agent_version": "7",
                                 "state": "ready", "model_started": False, "original_work_replayed": False}
            if self.lose_prepare:
                self.lose_prepare = False
                raise httpx.ReadError("lost prepare reply", request=request)
            return httpx.Response(200, json=self.prepared, headers=store)
        if path == native + "preparation" and method == "GET":
            return httpx.Response(200, headers=store, json={
                "schema_version": "fa-native-environment-metadata-v1", "agent_id": NEW,
                "hosted_session_id": "native-session-1", "hosted_agent_version": "7", "state": "unverified",
                "observed_sessions": [], "truncated": False, "state_scope": "platform-metadata-only",
                "preparation_verified": False, "model_started": False, "original_work_replayed": False,
                "scope": "entire-agent"})
        if path == native + "prepare/reconcile" and method == "POST":
            assert json.loads(request.content) == self.prepares[0]
            if self.preparation_errors:
                status, code = self.preparation_errors.pop(0)
                return httpx.Response(status, headers=store, json={"error": {"code": code}})
            if self.preparation_fail:
                self.preparation_fail = False
                return httpx.Response(503, headers=store, json={"error": {"code": "NATIVE_ENVIRONMENT_UNAVAILABLE"}})
            if self.prepared is None:
                return httpx.Response(409, headers=store, json={"error": {"code": "NATIVE_SESSION_CHANGED"}})
            return httpx.Response(200, headers=store, json={"agent_id": NEW, "hosted_session_id": "native-session-1",
                                                            "etag": "etag-1", "preparation": self.prepared})
        assert path.startswith("/agents/agent-one/"), path
        inner = path.removeprefix("/agents/agent-one")
        return self.c.backend.send(httpx.Request(method, request.url.copy_with(path=inner),
                                                headers=request.headers, content=request.content))


def open_ui(c, upstream, links):
    app = create_app(c.config, c.journal, backend=upstream,
        token_provider=lambda: c.h.headers()["Authorization"].removeprefix("Bearer "),
        clock=lambda: c.h.time, port=8873, instruction_connection=c.connection, agent_connections=links)
    ui = TestClient(app, base_url="http://127.0.0.1:8873")
    ui.get("/")
    headers = {"Origin": "http://127.0.0.1:8873", "X-Console-CSRF": ui.get("/api/state").json()["csrf"]}
    assert ui.post("/api/authenticate", json={}, headers=headers).status_code == 200
    return ui, headers


def one_agent_manifest(c, tmp_path):
    manifest = connection_file(c, tmp_path)
    manifest.raw["agents"] = manifest.raw["agents"][:1]
    manifest.path.write_bytes(canonical(manifest.raw))
    manifest.args[-1] = sha(manifest.path.read_bytes())
    return manifest


def test_lost_reply_and_restart_resume_the_same_request_then_a_new_manifest_enables_the_agent(
    real_interface, tmp_path, monkeypatch,
):
    c = real_interface
    manifest = one_agent_manifest(c, tmp_path)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    server = Server(c)
    old_manifest, old_journal = manifest.path.read_bytes(), c.journal.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            server.lose_next_post = True
            lost = ui.post("/api/agents/onboarding", json={"display_name": "Researcher", "role_prompt": "Finds sources."},
                           headers=headers)
            assert lost.status_code == 503 and lost.json()["error"]["code"] == "BACKEND_OFFLINE"
        # 8765 restarts: the same local record is still there and still names the same request ID.
        ui, headers = open_ui(c, upstream, links)
        with ui:
            listed = ui.get("/api/agents/onboarding").json()["onboardings"]
            assert len(listed) == 1 and listed[0]["state"] == "UNKNOWN"
            rid = listed[0]["request_id"]
            again = ui.post("/api/agents/onboarding", json={"display_name": "Other", "role_prompt": "x"},
                            headers=headers)
            assert again.status_code == 409 and again.json()["error"]["code"] == "AGENT_ONBOARDING_PENDING"
            status = ui.get("/api/agents/onboarding/" + rid).json()
            assert status["server_state"] == "creating" and status["state"] == "SERVER"
            directory = ui.get("/api/agents").json()["agents"]
            assert [(a["id"], a["enabled"], a["reason"]) for a in directory][1] == (
                NEW, False, "AGENT_ONBOARDING_IN_PROGRESS")
            done = ui.post("/api/agents/onboarding/" + rid + "/continue", json={}, headers=headers)
            assert done.status_code == 200, done.text
            value = done.json()
            assert value["state"] == "CONFIRMED" and not value["restart_required"] and value["agent_id"] == NEW
            # No restart: the new Agent joined the running manifest and is selectable at the next directory read.
            directory = ui.get("/api/agents").json()["agents"]
            assert directory[1]["id"] == NEW and directory[1]["enabled"], directory[1]
            assert ui.get("/api/state", headers={"X-Console-Agent": NEW}).status_code == 200
            assert ui.post("/api/agents/onboarding/" + rid + "/continue", json={}, headers=headers).json() == value
        # A restart with the original manifest restores the confirmed link from the onboarding record.
        ui, headers = open_ui(c, upstream, links)
        with ui:
            restored = ui.get("/api/agents").json()["agents"]
            assert [(a["id"], a["enabled"]) for a in restored] == [("agent-one", True), (NEW, True)]
    assert {post["request_id"] for post in server.posts} == {rid} and len(server.records) == 1
    assert all(post == server.posts[0] for post in server.posts)
    assert manifest.path.read_bytes() == old_manifest and c.journal.read_bytes() == old_journal
    root = tmp_path / "agents"
    new_manifest = root / ("manifest-" + value["manifest_sha256"][:16] + ".json")
    assert sha(new_manifest.read_bytes()) == value["manifest_sha256"]
    linked = AgentConnections.model_validate_json(new_manifest.read_bytes())
    assert linked.agents[0] == links.agents[0] and linked.agents[1].agent_id == NEW
    entry = linked.agents[1]
    folder = Path(entry.config_path).parent
    assert folder.parent == root and Path(entry.instructions_path).parent == folder
    assert Path(entry.journal_path).parent == folder and Path(entry.journal_path) != c.journal
    config = json.loads(Path(entry.config_path).read_bytes())
    assert config["standing_policy_sha256"] == server.connection.standing_policy_sha256
    # New Agents start new conversations on the native lane, like A and B.
    assert json.loads(Path(entry.instructions_path).read_bytes())["state_profile"] == "foundry-native-v1"
    assert config["standing_policy_sha256"] != c.config.standing_policy_sha256
    saved = Journal(root / "onboarding.sqlite3", c.config).get(rid, PHASE)
    assert saved["intent"]["command"] == server.posts[0]
    assert saved["intent"]["command_sha256"] == sha(canonical(server.posts[0]))
    # Restart with the new manifest and its sha: the new Agent is selectable; the old sha still starts without it.
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        ui, _ = open_ui(c, upstream, linked)
        with ui:
            listed = ui.get("/api/agents").json()["agents"]
            assert [(a["id"], a["enabled"]) for a in listed] == [("agent-one", True), (NEW, True)]
            state = ui.get("/api/state", headers={"X-Console-Agent": NEW})
            assert state.status_code == 200 and state.json()["selected_agent_id"] == NEW
            assert not Path(entry.journal_path).exists() or Path(entry.journal_path).is_file()
    assert c.journal.read_bytes() == old_journal
    calls = []
    monkeypatch.setattr(launcher, "create_app", lambda *args, **kwargs: calls.append(kwargs) or object())
    monkeypatch.setattr(launcher.uvicorn, "run", lambda *args, **kwargs: None)
    swapped = manifest.args[:-3] + [str(new_manifest), "--agents-sha256", value["manifest_sha256"]]
    assert launcher.main(swapped) == 0 and calls[-1]["agent_connections"] == linked
    assert launcher.main(manifest.args) == 0 and calls[-1]["agent_connections"] == links
    assert launcher.main(swapped[:-1] + [manifest.args[-1]]) == 2


def test_failed_connection_read_resumes_with_the_same_request(real_interface, tmp_path):
    c = real_interface
    manifest = one_agent_manifest(c, tmp_path)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    server = Server(c)
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            first = ui.post("/api/agents/onboarding", json={"display_name": "Researcher", "role_prompt": "x"},
                            headers=headers).json()
            rid = first["request_id"]
            assert first["server_state"] == "creating"
            server.link_fail = True
            ready = ui.post("/api/agents/onboarding/" + rid + "/continue", json={}, headers=headers)
            assert ready.status_code == 200, ready.text
            assert ready.json()["state"] == "SERVER" and ready.json()["server_state"] == "ready"
            done = ui.post("/api/agents/onboarding/" + rid + "/continue", json={}, headers=headers)
            assert done.status_code == 200, done.text
            assert done.json()["state"] == "CONFIRMED" and done.json()["agent_id"] == NEW
    assert {post["request_id"] for post in server.posts} == {rid} and len(server.posts) == 2


def test_quota_refusal_shows_the_limit_and_leaves_no_local_link(real_interface, tmp_path):
    c = real_interface
    manifest = one_agent_manifest(c, tmp_path)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    server = Server(c)
    server.quota = True
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            full = ui.post("/api/agents/onboarding", json={"display_name": "Researcher", "role_prompt": "x"},
                           headers=headers)
            assert full.status_code == 200, full.text
            value = full.json()
            assert (value["state"], value["code"], value["limit"]) == ("REFUSED", "AGENT_QUOTA_REACHED", 16)
            assert not value["restart_required"]
            # A refusal decided before any server row allows a new request ID; nothing local was linked.
            assert ui.post("/api/agents/onboarding", json={"display_name": "Researcher", "role_prompt": "x"},
                           headers=headers).json()["state"] == "REFUSED"
            # The panel falls back to the first listed record, so the newest must come first.
            listed = ui.get("/api/agents/onboarding").json()["onboardings"]
            assert [item["request_id"] for item in listed] == [post["request_id"] for post in server.posts[::-1]]
    root = tmp_path / "agents"
    assert sorted(path.name for path in root.iterdir()) == ["onboarding.sqlite3"]
    assert len({post["request_id"] for post in server.posts}) == 2


@pytest.mark.parametrize("body", [
    {"display_name": "Researcher", "role_prompt": "x", "config_path": "C:/elsewhere/config.json"},
    {"display_name": "Researcher", "role_prompt": "x", "standing_policy": {}},
    {"display_name": "Researcher", "role_prompt": "x", "request_id": "chosen-by-browser"},
    {"display_name": "Researcher", "role_prompt": "é" * 1025},
    {"display_name": "Researcher"},
])
def test_browser_cannot_supply_paths_policies_or_oversized_input(real_interface, tmp_path, body):
    c = real_interface
    manifest = one_agent_manifest(c, tmp_path)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    server = Server(c)
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            refused = ui.post("/api/agents/onboarding", json=body, headers=headers)
            assert refused.status_code == 400 and refused.json()["error"]["code"] == "AGENT_ONBOARDING_INPUT_INVALID"
            assert ui.post("/api/agents/onboarding/../x/continue", json={"config_path": "x"},
                           headers=headers).status_code in {403, 404}
            assert ui.post("/api/agents/onboarding/nope/continue", json={}, headers=headers).status_code == 404
    assert server.posts == []
    assert not (tmp_path / "agents").exists() or all(
        path.name == "onboarding.sqlite3" for path in (tmp_path / "agents").iterdir())


CHOICE = {"capabilities": [{"entry_id": "fa-skills-v1", "entry_sha256": "c" * 64}],
          "skills": [{"name": "receipt-guide", "version_id": "1" * 32, "sha256": "a" * 64,
                      "metadata_sha256": "b" * 64}]}


def test_chosen_tools_and_skills_are_sent_as_is_and_reused_on_resume(real_interface, tmp_path):
    c = real_interface
    manifest = one_agent_manifest(c, tmp_path)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    server = Server(c)
    body = {"display_name": "Researcher", "role_prompt": "x", **CHOICE}
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            first = ui.post("/api/agents/onboarding", json=body, headers=headers).json()
            rid = first["request_id"]
            done = ui.post("/api/agents/onboarding/" + rid + "/continue", json={}, headers=headers)
            assert done.status_code == 200 and done.json()["state"] == "CONFIRMED", done.text
    assert len(server.posts) == 2 and server.posts[0] == server.posts[1] == {"request_id": rid, **body}
    saved = Journal(tmp_path / "agents" / "onboarding.sqlite3", c.config).get(rid, PHASE)
    assert saved["intent"]["command"] == {"request_id": rid, **body}


@pytest.mark.parametrize("change", [
    {"capabilities": CHOICE["capabilities"]},
    {"skills": CHOICE["skills"]},
    {**CHOICE, "capabilities": [{"entry_id": "fa-skills-v1", "entry_sha256": "c" * 64, "kind": "fa-builtin"}]},
    {**CHOICE, "skills": CHOICE["skills"] * 5},
    {**CHOICE, "skills": [{**CHOICE["skills"][0], "path": "C:/skills"}]},
])
def test_browser_choice_must_be_exact_references(real_interface, tmp_path, change):
    c = real_interface
    manifest = one_agent_manifest(c, tmp_path)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    server = Server(c)
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            refused = ui.post("/api/agents/onboarding", json={"display_name": "R", "role_prompt": "x", **change},
                              headers=headers)
            assert refused.status_code == 400 and refused.json()["error"]["code"] == "AGENT_ONBOARDING_INPUT_INVALID"
    assert server.posts == []


def test_server_choice_refusal_is_final_and_links_nothing(real_interface, tmp_path):
    c = real_interface
    manifest = one_agent_manifest(c, tmp_path)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    server = Server(c)

    def refuse(request):
        if request.url.path == "/work-policy/v1/agents" and request.method == "POST":
            server.posts.append(json.loads(request.content))
            return httpx.Response(409, headers={"cache-control": "no-store"}, json={"error": {
                "code": "CAPABILITY_SOURCES_CHANGED", "message": "changed"}})
        return server(request)

    with httpx.Client(transport=httpx.MockTransport(refuse)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            value = ui.post("/api/agents/onboarding", json={"display_name": "R", "role_prompt": "x", **CHOICE},
                            headers=headers).json()
            assert (value["state"], value["code"]) == ("REFUSED", "CAPABILITY_SOURCES_CHANGED")
    assert sorted(path.name for path in (tmp_path / "agents").iterdir()) == ["onboarding.sqlite3"]


def test_server_choice_change_after_admission_keeps_the_original_request(real_interface, tmp_path):
    c = real_interface
    manifest = one_agent_manifest(c, tmp_path)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    server = Server(c)

    def stale(request):
        if request.url.path == "/work-policy/v1/agents" and request.method == "POST":
            server.posts.append(json.loads(request.content))
            return httpx.Response(409, headers={"cache-control": "no-store"}, json={"error": {
                "code": "AGENT_ONBOARDING_SELECTION_STALE", "message": "stale", "cause": "CAPABILITY_SOURCES_CHANGED"}})
        return server(request)

    with httpx.Client(transport=httpx.MockTransport(stale)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            body = {"display_name": "R", "role_prompt": "x", **CHOICE}
            value = ui.post("/api/agents/onboarding", json=body, headers=headers).json()
            assert (value["state"], value["code"]) == ("UNKNOWN", "AGENT_ONBOARDING_SELECTION_STALE")
            # Not final: a new request is refused while this one is pending, and resume resends the same request.
            again = ui.post("/api/agents/onboarding", json=body, headers=headers)
            assert again.status_code == 409 and again.json()["error"]["code"] == "AGENT_ONBOARDING_PENDING"
            again = ui.post(f"/api/agents/onboarding/{value['request_id']}/continue", json={}, headers=headers).json()
            assert (again["state"], again["request_id"]) == ("UNKNOWN", value["request_id"])
    assert len(server.posts) == 2 and server.posts[0] == server.posts[1]
    assert sorted(path.name for path in (tmp_path / "agents").iterdir()) == ["onboarding.sqlite3"]


def test_options_offer_the_team_catalog_with_the_default_team_choice_preselected(real_interface, tmp_path):
    c = real_interface
    manifest = one_agent_manifest(c, tmp_path)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    server = Server(c)
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            value = ui.get("/api/agents/onboarding/options")
            assert value.status_code == 200, value.text
            value = value.json()
            scoped = {"X-Console-Agent": "agent-one"}
            assert value["options"] == ui.get("/api/agent-template/options", headers=scoped).json()
            template = ui.get("/api/agent-template", headers=scoped).json()
            assert value["current"] == {"capabilities": template["settings"].get("capabilities", []),
                                        "skills": template.get("skills", [])}
            assert ui.get("/api/agents/onboarding/options?x=1").status_code == 403
    assert server.posts == []


def test_options_never_preselect_a_tool_the_agent_role_profile_cannot_compose(real_interface, tmp_path, monkeypatch):
    """A tool the new Agent's profile cannot compose is never the default.

    agent-role v2 composes every current builtin, so the server rule is simulated as refusing skillify.
    """
    from shared_context.behavior_agent_template import TemplateCapabilityOption, TemplateOptions
    from shared_context.behavior_capabilities import BUILTIN_CAPABILITIES
    from web_test_console import agent_onboarding, bridge

    monkeypatch.setattr(agent_onboarding, "base_supports", lambda entry_id, profile: entry_id != "fa-skillify-v1")

    builtins = {entry: BUILTIN_CAPABILITIES[entry] for entry in ("fa-skillify-v1", "fa-skills-v1")}
    offered = TemplateOptions(skills=(), more_skills=False, capabilities=tuple(
        TemplateCapabilityOption(entry_id=item.entry_id, entry_sha256=item.entry_sha256, kind="fa-builtin",
                                 tools=item.tools, vision=item.vision) for item in builtins.values()),
        composable_profiles=("fa-instruction-agent-role-v1",))
    refs = [{"entry_id": item.entry_id, "entry_sha256": item.entry_sha256}
            for item in sorted(builtins.values(), key=lambda item: item.entry_id)]
    monkeypatch.setattr(bridge.Console, "template_options", lambda self: offered)
    monkeypatch.setattr(bridge.Console, "download_template", lambda self: json.dumps(
        {"settings": {"capabilities": refs}, "skills": []}).encode())
    c = real_interface
    manifest = one_agent_manifest(c, tmp_path)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    server = Server(c)
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            value = ui.get("/api/agents/onboarding/options")
            assert value.status_code == 200, value.text
            value = value.json()
    assert value["unsupported"] == ["fa-skillify-v1"]
    assert [item["entry_id"] for item in value["options"]["capabilities"]] == ["fa-skillify-v1", "fa-skills-v1"]
    assert value["current"]["capabilities"] == [ref for ref in refs if ref["entry_id"] == "fa-skills-v1"]
    page = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc" / "web_test_console" / "index.html"
    text = page.read_text(encoding="utf-8")
    assert "input.disabled=input.faBlocked||" in text and "!Array.isArray(value.unsupported)" in text
    assert server.posts == []


def test_onboarding_body_limit_fits_a_full_choice(real_interface, tmp_path):
    c = real_interface
    manifest = one_agent_manifest(c, tmp_path)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    server = Server(c)
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            big = ui.post("/api/agents/onboarding", content=b" " * 16385,
                          headers={**headers, "Content-Type": "application/json"})
            assert big.status_code == 413
    assert server.posts == []


def test_a_link_written_before_the_native_default_keeps_its_job_bound_file(real_interface, tmp_path, monkeypatch):
    """Files written by an older 8765 whose journal entry was lost are reused, not a permanent local conflict."""
    from web_test_console import agent_onboarding

    c = real_interface
    manifest = one_agent_manifest(c, tmp_path)
    links = AgentConnections.model_validate_json(manifest.path.read_bytes())
    server = Server(c)
    real_finish, lost = Journal.finish, []

    def lose_link(self, rid, phase, observed):
        if phase == PHASE and observed.get("state") == "CONFIRMED" and not lost:
            lost.append(True)
            raise OSError("OFFLINE journal write lost")
        return real_finish(self, rid, phase, observed)

    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            first = ui.post("/api/agents/onboarding", json={"display_name": "Researcher", "role_prompt": "x"},
                            headers=headers).json()
            rid = first["request_id"]
            monkeypatch.setattr(agent_onboarding, "NEW_AGENT_STATE_PROFILE", None)
            monkeypatch.setattr(Journal, "finish", lose_link)
            with pytest.raises(OSError):
                ui.post("/api/agents/onboarding/" + rid + "/continue", json={}, headers=headers)
            assert lost == [True]
            monkeypatch.setattr(agent_onboarding, "NEW_AGENT_STATE_PROFILE", "foundry-native-v1")
            monkeypatch.setattr(Journal, "finish", real_finish)
            done = ui.post("/api/agents/onboarding/" + rid + "/continue", json={}, headers=headers)
            assert done.status_code == 200, done.text
            assert done.json()["state"] == "CONFIRMED" and done.json()["agent_id"] == NEW
    value = done.json()
    linked = AgentConnections.model_validate_json(
        (tmp_path / "agents" / ("manifest-" + value["manifest_sha256"][:16] + ".json")).read_bytes())
    entry = next(item for item in linked.agents if item.agent_id == NEW)
    saved = json.loads(Path(entry.instructions_path).read_bytes())
    assert "state_profile" not in saved and sha(Path(entry.instructions_path).read_bytes()) == entry.instructions_sha256


@pytest.mark.parametrize("case", ["ready", "refused-then-retry", "reply-lost", "unknown-then-read"])
def test_a_new_agent_is_prepared_once_with_one_request_id(real_interface, tmp_path, case):
    """After the link, 8765 prepares the new Agent's native environment; only failures resend the same ID."""
    c = real_interface
    links = AgentConnections.model_validate_json(one_agent_manifest(c, tmp_path).path.read_bytes())
    server = Server(c)
    if case == "refused-then-retry":
        server.prepare_refusal = "NATIVE_BOOTSTRAP_SOURCE_ACTIVE"
    elif case == "reply-lost":
        server.lose_prepare = True
    elif case == "unknown-then-read":
        server.lose_prepare, server.preparation_fail = True, True
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            rid = ui.post("/api/agents/onboarding", json={"display_name": "Researcher", "role_prompt": "x"},
                          headers=headers).json()["request_id"]
            done = ui.post("/api/agents/onboarding/" + rid + "/continue", json={}, headers=headers).json()
            assert done["state"] == "CONFIRMED"
            first = done["prepare"]
            expected = {"ready": "READY", "refused-then-retry": "FAILED", "reply-lost": "READY",
                        "unknown-then-read": "UNKNOWN"}[case]
            assert first["state"] == expected and done["ready_to_chat"] is (expected == "READY")
            if case == "refused-then-retry":
                assert first["code"] == "NATIVE_BOOTSTRAP_SOURCE_ACTIVE"
            if expected != "READY":
                again = ui.post("/api/agents/onboarding/" + rid + "/prepare", json={}, headers=headers)
                assert again.status_code == 200, again.text
                done = again.json()
            assert done["prepare"]["state"] == "READY" and done["ready_to_chat"] is True
            assert done["prepare"]["hosted_agent_version"] == "7"
            # Ready is final: the button and a restart never prepare again.
            assert ui.post("/api/agents/onboarding/" + rid + "/prepare", json={}, headers=headers).json() == done
    ids = {body["request_id"] for body in server.prepares}
    assert len(ids) == 1 and ids == {first["request_id"]}
    # An unknown outcome is only read back; only a confirmed refusal sends the same request again.
    assert len(server.prepares) == (2 if case == "refused-then-retry" else 1)


@pytest.mark.parametrize("case", ["version-building", "missing-roles", "five-minutes-pass"])
def test_the_page_retries_prepare_only_for_not_ready_or_unknown_and_for_five_minutes(real_interface, tmp_path, case):
    """A version still being built is retried with the same ID; 403 is never retried; after 5 minutes it stops."""
    from datetime import timedelta

    c = real_interface
    links = AgentConnections.model_validate_json(one_agent_manifest(c, tmp_path).path.read_bytes())
    server = Server(c)
    if case == "version-building":
        server.prepare_errors = [(503, "AGENT_VERSION_FOLLOWING")] * 2
        server.preparation_errors = [(503, "AGENT_VERSION_FOLLOWING")] * 2
    elif case == "missing-roles":
        server.prepare_errors = [(403, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE")]
        server.preparation_errors = [(403, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE")]
    else:
        server.lose_prepare, server.preparation_fail = True, True
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        ui, headers = open_ui(c, upstream, links)
        with ui:
            rid = ui.post("/api/agents/onboarding", json={"display_name": "Researcher", "role_prompt": "x"},
                          headers=headers).json()["request_id"]
            first = ui.post("/api/agents/onboarding/" + rid + "/continue", json={}, headers=headers).json()["prepare"]
            if case == "version-building":
                assert (first["state"], first["code"], first["auto_retry"]) == ("FAILED", "AGENT_VERSION_FOLLOWING", True)
                second = ui.post("/api/agents/onboarding/" + rid + "/prepare", json={}, headers=headers).json()
                assert second["prepare"]["state"] == "FAILED" and second["prepare"]["auto_retry"] is True
                third = ui.post("/api/agents/onboarding/" + rid + "/prepare", json={}, headers=headers).json()
                assert third["prepare"]["state"] == "READY" and third["prepare"]["auto_retry"] is False
                assert third["ready_to_chat"] is True
            elif case == "missing-roles":
                assert first["state"] == "FAILED" and first["upstream_status"] == 403
                assert first["code"] == "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE" and first["auto_retry"] is False
            else:
                assert first["state"] == "UNKNOWN" and first["auto_retry"] is True
                c.h.time += timedelta(seconds=301)
                listed = next(item for item in ui.get("/api/agents/onboarding").json()["onboardings"]
                              if item["request_id"] == rid)
                assert listed["prepare"]["state"] == "UNKNOWN" and listed["prepare"]["auto_retry"] is False
    assert len({body["request_id"] for body in server.prepares}) == 1
    assert len(server.prepares) == {"version-building": 3, "missing-roles": 1, "five-minutes-pass": 1}[case]


def test_remote_waits_for_new_roles_and_resends_the_same_prepare_every_ten_minutes(real_interface, tmp_path,
                                                                                 monkeypatch):
    """Stage assigns the roles before ready (#470); a later 403 means they are still taking effect (Neal 00:33)."""
    from datetime import timedelta
    from web_test_console.bridge import Journal
    from web_test_console.remote import RemoteBoundary

    c = real_interface
    links = AgentConnections.model_validate_json(one_agent_manifest(c, tmp_path).path.read_bytes())
    server = Server(c)
    server.prepare_errors = [(403, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE")] * 2
    server.preparation_errors = [(403, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE")] * 2
    kept = []
    monkeypatch.setattr(Journal, "after_commit", lambda path: kept.append(("db", path)))
    monkeypatch.setattr(Journal, "after_file", lambda path: kept.append(("file", path)))
    origin = "https://ca-fa-console-stage.example.io"
    signed = {"x-ms-client-principal-id": c.config.human_oid, "x-ms-client-principal-idp": "aad",
              "x-ms-token-aad-access-token": c.h.headers()["Authorization"].removeprefix("Bearer ")}
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        from web_test_console.remote import EasyAuthTokenProvider
        app = create_app(c.config, c.journal, backend=upstream, token_provider=EasyAuthTokenProvider(),
                         clock=lambda: c.h.time, instruction_connection=c.connection, agent_connections=links,
                         remote=RemoteBoundary(origin, c.config.human_oid))
        ui = TestClient(app, base_url=origin)
        with ui:
            ui.get("/", headers=signed)
            headers = {**signed, "Origin": origin, "X-Console-CSRF": ui.get("/api/state", headers=signed).json()["csrf"]}
            assert ui.post("/api/authenticate", json={}, headers=headers).status_code == 200
            rid = ui.post("/api/agents/onboarding", json={"display_name": "Researcher", "role_prompt": "x"},
                          headers=headers).json()["request_id"]
            done = ui.post("/api/agents/onboarding/" + rid + "/continue", json={}, headers=headers).json()
            # The new Agent's files were kept with the journals before the link was recorded.
            assert [kind for kind, _ in kept if kind == "file"] == ["file", "file", "file"]
            first = done["prepare"]
            assert (first["state"], first["upstream_status"], first["permission_wait"]) == ("FAILED", 403, True)
            assert first["auto_retry"] is True and first["retry_seconds"] == 600
            c.h.time += timedelta(minutes=10)
            second = ui.post("/api/agents/onboarding/" + rid + "/prepare", json={}, headers=headers).json()["prepare"]
            assert second["permission_wait"] and second["auto_retry"]
            c.h.time += timedelta(minutes=10)
            third = ui.post("/api/agents/onboarding/" + rid + "/prepare", json={}, headers=headers).json()
            assert third["prepare"]["state"] == "READY" and third["ready_to_chat"] is True
    assert len(server.prepares) == 3 and len({body["request_id"] for body in server.prepares}) == 1


def test_remote_permission_wait_stops_after_two_hours_and_a_failed_upload_leaves_the_link_unconfirmed(
        real_interface, tmp_path, monkeypatch):
    from datetime import timedelta
    from web_test_console.adapter import ConsoleError
    from web_test_console.bridge import Journal
    from web_test_console.remote import EasyAuthTokenProvider, RemoteBoundary

    c = real_interface
    links = AgentConnections.model_validate_json(one_agent_manifest(c, tmp_path).path.read_bytes())
    server = Server(c)
    server.prepare_errors = [(403, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE")] * 20
    server.preparation_errors = [(403, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE")] * 20
    refuse = {"on": True}

    def keep_file(path):
        if refuse["on"]:
            raise ConsoleError("REMOTE_STATE_UNAVAILABLE", 503)

    monkeypatch.setattr(Journal, "after_commit", lambda path: None)
    monkeypatch.setattr(Journal, "after_file", keep_file)
    origin = "https://ca-fa-console-stage.example.io"
    signed = {"x-ms-client-principal-id": c.config.human_oid, "x-ms-client-principal-idp": "aad",
              "x-ms-token-aad-access-token": c.h.headers()["Authorization"].removeprefix("Bearer ")}
    with httpx.Client(transport=httpx.MockTransport(server)) as upstream:
        app = create_app(c.config, c.journal, backend=upstream, token_provider=EasyAuthTokenProvider(),
                         clock=lambda: c.h.time, instruction_connection=c.connection, agent_connections=links,
                         remote=RemoteBoundary(origin, c.config.human_oid))
        ui = TestClient(app, base_url=origin)
        with ui:
            ui.get("/", headers=signed)
            headers = {**signed, "Origin": origin, "X-Console-CSRF": ui.get("/api/state", headers=signed).json()["csrf"]}
            assert ui.post("/api/authenticate", json={}, headers=headers).status_code == 200
            rid = ui.post("/api/agents/onboarding", json={"display_name": "Researcher", "role_prompt": "x"},
                          headers=headers).json()["request_id"]
            failed = ui.post("/api/agents/onboarding/" + rid + "/continue", json={}, headers=headers)
            assert failed.status_code == 503 and failed.json()["error"]["code"] == "REMOTE_STATE_UNAVAILABLE"
            listed = ui.get("/api/agents/onboarding", headers=signed).json()["onboardings"][0]
            assert listed["state"] != "CONFIRMED" and server.prepares == []
            refuse["on"] = False
            done = ui.post("/api/agents/onboarding/" + rid + "/continue", json={}, headers=headers).json()
            assert done["state"] == "CONFIRMED" and done["prepare"]["permission_wait"]
            c.h.time += timedelta(hours=2, seconds=1)
            listed = ui.get("/api/agents/onboarding", headers=signed).json()["onboardings"][0]
            assert listed["prepare"]["permission_wait"] and listed["prepare"]["auto_retry"] is False
    assert {body["request_id"] for body in server.posts} == {rid}
    assert len({body["request_id"] for body in server.prepares}) == 1
