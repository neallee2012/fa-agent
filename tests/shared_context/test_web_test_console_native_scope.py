"""Native control replies retain the same selected-Agent envelope as GET state."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from azure.ai.projects.models import AgentSessionResource

from shared_context.behavior_assistant import _LocalHttp
from web_test_console.agent_connections import AgentConnections
from web_test_console.bridge import create_app
from test_web_test_console_agents import connection_file, directory_payload
from test_web_test_console_native import native_console
from test_native_turn_api import native_api
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_native_session_prepare_api import project_client


@pytest.fixture
def scope_loop():
    loop = asyncio.new_event_loop()
    yield loop
    if not loop.is_closed():
        loop.close()


@pytest.fixture
def routed_native_console(scope_loop, request, tmp_path, monkeypatch):
    case = request.getfixturevalue("native_console")
    target = case.api.remote.target
    session = AgentSessionResource({
        "agent_session_id": target.session_id, "status": "active",
        "version_indicator": {"type": "version_ref", "agent_version": target.agent_version}})
    project_client(monkeypatch, get_session=lambda *_args: session,
                   list_sessions=lambda *_args, **_kwargs: iter((session,)))
    manifest = connection_file(SimpleNamespace(
        config=case.config, connection=case.connection, journal=tmp_path / "agent-one.sqlite3"), tmp_path)
    connections = AgentConnections.model_validate_json(manifest.path.read_bytes())
    transport, calls = _LocalHttp(case.api.client), []

    def forward(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/work-policy/v1/agents":
            return httpx.Response(200, json=directory_payload(), headers={"cache-control": "no-store"})
        assert request.url.path.startswith("/agents/agent-one/")
        path = request.url.path.removeprefix("/agents/agent-one")
        return transport.handle_request(httpx.Request(
            request.method, request.url.copy_with(path=path), headers=request.headers, content=request.content))

    with httpx.Client(transport=httpx.MockTransport(forward)) as backend:
        app = create_app(
            case.config, tmp_path / "agent-one.sqlite3", backend=backend, port=8873,
            instruction_connection=case.connection, agent_connections=connections,
            token_provider=lambda: case.api.headers["Authorization"].removeprefix("Bearer "),
            clock=lambda: case.c.h.time, environment="OFFLINE_TEST")
        with TestClient(app, base_url="http://127.0.0.1:8873",
                        backend_options={"loop_factory": lambda: scope_loop}) as ui:
            ui.get("/")
            initial = ui.get("/api/state").json()
            headers = {"Origin": "http://127.0.0.1:8873", "X-Console-CSRF": initial["csrf"],
                       "X-Console-Agent": "agent-one"}
            authenticated = ui.post("/api/authenticate", headers=headers, json={})
            assert authenticated.status_code == 200, authenticated.text
            listed = ui.get("/api/agents")
            assert listed.status_code == 200, listed.text
            models = ui.get("/api/instructions/models", headers=headers)
            assert models.status_code == 200, models.text
            baseline = ui.get("/api/state", headers=headers).json()
            decorate = app.state.agent_consoles.decorate
            decoration_loops = []

            def observe_decorate(value, identity):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    on_event_loop = False
                else:
                    on_event_loop = True
                decoration_loops.append(on_event_loop)
                return decorate(value, identity)

            monkeypatch.setattr(app.state.agent_consoles, "decorate", observe_decorate)
            yield SimpleNamespace(
                case=case, app=app, ui=ui, headers=headers, baseline=baseline, calls=calls,
                decoration_loops=decoration_loops)



@pytest.mark.parametrize("suffix", [
    "environment-prepare", "environment-reconcile", "environment-auto",
    "environment-without-files-preview", "environment-without-files", "environment-without-files-continue",
])
def test_removed_environment_routes_keep_selected_agent_scope_and_send_nothing(routed_native_console, suffix):
    h = routed_native_console
    root = "/api/instructions/" + h.baseline["composer"]["thread_id"]
    result = h.ui.post(root + "/" + suffix, headers=h.headers, json={})
    assert result.status_code == 404
    assert not any("/environment" in path or path.endswith("/submit") for _, path in h.calls)


def test_agent_mode_first_submit_reaches_the_original_api_once_without_idle_check(routed_native_console):
    h = routed_native_console
    draft = h.baseline["composer"]
    sent = h.ui.post("/api/instructions/" + draft["request_id"] + "/submit", headers=h.headers, json={
        "content": "Send this new message once without a portal environment check.",
        "model": draft["models"]["default_model"]})
    assert sent.status_code == 200, sent.text
    assert sent.json()["task"]["submission"]["state"] == "CONFIRMED"
    submitted = [command for _, command in h.case.api.remote.calls if command.operation == "submit"]
    assert len(submitted) == 1 and submitted[0].command.request_id == draft["request_id"]
    assert not any("/environment" in path for _, path in h.calls)
