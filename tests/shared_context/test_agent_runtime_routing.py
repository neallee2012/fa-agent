"""One HTTPS origin routes verified Agent credentials to their original scoped runtime."""

from types import SimpleNamespace
import asyncio

from fastapi.testclient import TestClient
import pytest

from shared_context.behavior_api import create_behavior_app
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_peer_runtime import install_peer_routing
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.behavior_agent_registry import register_agent_scope
from shared_context.behavior_server import _lifespan, application
from shared_context.errors import Problem
from test_agent_scope_registry import peer_policy
from test_agent_runtime import peer_runtime
from test_conversation_transfer_startup import synthetic_standing_v1
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key, token
from agent_runtime_routing_helpers import routing_peer, agent_headers


@pytest.fixture
def broken_peer(request):
    loop = asyncio.new_event_loop()
    try:
        c = request.getfixturevalue("synthetic_standing_v1")
        policy = peer_policy(c.h.config, c.control.policy)
        register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time)
        yield c, policy, loop
    finally:
        if not loop.is_closed():
            loop.close()


def scoped_app(composition):
    app = create_behavior_app(composition.runtime, composition.verifier)
    install_work_policy_routes(app, SimpleNamespace(
        runtime=composition.runtime, verifier=composition.verifier, assistant=composition.assistant,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    return app


def test_human_peer_prefix_uses_the_bound_control_without_changing_legacy_a(routing_peer, signing_key):
    peer_runtime, loop = routing_peer
    c, a, b = peer_runtime.c, peer_runtime.a, peer_runtime.b
    app = scoped_app(a)
    install_peer_routing(app, a, scoped_app)
    headers = c.h.headers(signing_key, "Behavior.Read Behavior.Approve")
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        path = "/work-policy/v1/accounting"
        a_result = client.get(path, headers=headers)
        b_result = client.get("/agents/" + b.agent_id + path, headers=headers)
        assert a_result.status_code == b_result.status_code == 200, (a_result.text, b_result.text)
        assert a_result.json()["version"]["ledger_etag"] == b_result.json()["version"]["ledger_etag"]
        assert a_result.json()["version"]["policy_sha256"] == a.runtime.work_policy.policy.sha256
        assert b_result.json()["version"]["policy_sha256"] == b.runtime.work_policy.policy.sha256
        assert a.runtime.work_policy is c.control
        denied = client.get("/agents/" + b.agent_id + path, headers=agent_headers(c, signing_key, 0))
        assert denied.status_code == 403


def test_missing_peer_runtime_dependency_is_explicit_and_does_not_break_a_startup(broken_peer, signing_key, caplog):
    c, policy, loop = broken_peer
    a = c.h.composition
    app = scoped_app(a)
    install_peer_routing(app, a, scoped_app)
    _lifespan(app, a, a.runtime.validate_startup)
    headers = c.h.headers(signing_key, "Behavior.Read")
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        assert client.get("/work-policy/v1/accounting", headers=headers).status_code == 200
        result = client.get("/agents/" + policy.agent_id + "/work-policy/v1/accounting", headers=headers)
        assert result.status_code == 503
        assert client.get("/work-policy/v1/accounting", headers=headers).status_code == 200
    assert "AGENT_RUNTIME_UNAVAILABLE" in caplog.text
    assert a.runtime.work_policy is c.control
