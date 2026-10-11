"""One HTTPS origin routes verified Agent credentials to their original scoped runtime."""

from types import SimpleNamespace
import asyncio

from fastapi.testclient import TestClient
import pytest

from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_peer_runtime import install_peer_routing
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.behavior_agent_registry import register_agent_scope
from shared_context.behavior_server import _lifespan, application
from shared_context.errors import Problem
from test_agent_scope_registry import peer_policy
from test_agent_runtime import peer_runtime
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token


@pytest.fixture
def routing_peer(request):
    # Windows creates its internal loop wakeup pipe with a local socket pair.
    # Create only that loop before the startup fixture forbids every network connect.
    loop = asyncio.new_event_loop()
    try:
        yield request.getfixturevalue("peer_runtime"), loop
    finally:
        if not loop.is_closed():
            loop.close()


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
    app = create_assistant_app(composition.assistant.authority, composition.verifier,
                               provider=composition.assistant.provider)
    install_work_policy_routes(app, SimpleNamespace(
        runtime=composition.runtime, verifier=composition.verifier, assistant=composition.assistant,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    return app


def agent_headers(c, key, agent):
    return {"Authorization": "Bearer " + token(key, {
        "oid": c.h.config.targets[agent].agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",)), "Content-Type": "application/json"}


def test_flat_binding_callbacks_route_by_verified_identity_and_refuse_other_agent_path(routing_peer, signing_key):
    peer_runtime, loop = routing_peer
    c, a, b = peer_runtime.c, peer_runtime.a, peer_runtime.b
    binding, _ = b.runtime.work_policy.submit(c.human, InstructionSubmit(
        request_id="b-api-route", content="Synthetic B callback routing."))
    app = scoped_app(a)
    install_peer_routing(app, a, scoped_app)
    path = f"/behavior/v1/bindings/{binding.binding_id}/jobs/{binding.job_id}"
    before = a.runtime.store._guard()
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        response = client.get(path, headers=agent_headers(c, signing_key, 1))
        assert response.status_code == 200, response.text
        assert response.json()["job_id"] == binding.job_id
        wrong = client.get(path, headers=agent_headers(c, signing_key, 0))
        assert wrong.status_code == 403
        unauthenticated = client.get(path)
        assert unauthenticated.status_code == 401
    assert a.runtime.store._guard() == before


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


def test_peer_app_read_failure_rebuilds_closed_peer_without_replacing_original_work(
        routing_peer, signing_key, monkeypatch):
    peer_runtime, loop = routing_peer
    c, a, b = peer_runtime.c, peer_runtime.a, peer_runtime.b
    binding, _ = b.runtime.work_policy.submit(c.human, InstructionSubmit(
        request_id="b-recover-app", content="Keep the original B work after a transient startup read."))
    attempts = []

    def factory(peer):
        attempts.append(peer)
        if len(attempts) == 1:
            def unavailable(_keys):
                raise Problem(503, "BEHAVIOR_READ_FAILED", "Synthetic transient read failure")
            with monkeypatch.context() as fault:
                fault.setattr(peer.runtime.store, "read_snapshot", unavailable)
                return application(peer)
        if peer.closed:
            return application(peer)
        peer.runtime.validate_startup()
        return scoped_app(peer)

    app = scoped_app(a)
    install_peer_routing(app, a, factory)
    path = f"/behavior/v1/bindings/{binding.binding_id}/jobs/{binding.job_id}"
    before = a.runtime.store._guard()
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        assert client.get(path, headers=agent_headers(c, signing_key, 1)).status_code == 503
        assert b.closed and not a.closed
        assert client.get("/work-policy/v1/accounting",
                          headers=c.h.headers(signing_key, "Behavior.Read")).status_code == 200
        recovered = client.get(path, headers=agent_headers(c, signing_key, 1))
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["job_id"] == binding.job_id
        current = a.for_agent(b.agent_id)
        assert current is not b and not current.closed
        assert current.runtime.store is a.runtime.store
        assert current.runtime.dep.workers.contract.capacity == 1
        assert current.runtime.dep.native_launches.targets == b.runtime.dep.native_launches.targets
        assert current.runtime.work_policy.records[binding.binding_id].binding == binding
        assert client.get(path, headers=agent_headers(c, signing_key, 0)).status_code == 403
        assert client.get(path).status_code == 401
        assert client.get(path, headers=agent_headers(c, signing_key, 1)).status_code == 200
        assert attempts == [b, current]
    assert a.runtime.store._guard() == before
    assert a.runtime.work_policy is c.control and c.sessions.calls == []
