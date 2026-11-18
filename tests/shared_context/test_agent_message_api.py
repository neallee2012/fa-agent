"""Signed peer-message seams; no model, Hosted process, or network."""

from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from shared_context.behavior_api import create_behavior_app
from shared_context.behavior_peer_runtime import install_peer_routing
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_agent_runtime import peer_runtime
from test_conversation_transfer_startup import synthetic_standing_v1
from agent_runtime_routing_helpers import agent_headers, routing_peer
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_behavior_store import store
from hosted_versions_stub import deployed_versions
from legacy_replay_helpers import legacy_tokens, pinned_configuration, replayed


def directory_application(composition):
    """The work-policy routes over the bounded behavior app."""
    app = create_behavior_app(composition.runtime, composition.verifier)
    install_work_policy_routes(app, composition)
    return app


def test_signed_directory_lists_registered_agents_without_exposing_credentials_or_starting_work(
        routing_peer, signing_key):
    peers, loop = routing_peer
    c, a, b = peers.c, peers.a, peers.b
    app = directory_application(a)
    install_peer_routing(app, a, directory_application)
    before = a.runtime.store._guard()
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        response = client.get("/work-policy/v1/agents", headers=c.h.headers(signing_key, "Behavior.Read"))
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["schema_version"] == "fa-agent-directory-v1"
        assert value["default_agent_id"] == c.h.config.targets[0].agent_id
        assert [item["agent_id"] for item in value["agents"]] == [
            target.agent_id for target in c.h.config.targets
        ]
        assert value["agents"][1]["agent_id"] == b.agent_id
        for item in value["agents"]:
            assert set(item) == {"agent_id", "display_name", "api_prefix", "state", "reason"}
            assert item["api_prefix"] == "/agents/" + item["agent_id"]
        for item in value["agents"][:2]:
            assert item["state"] == "ready" and item["reason"] is None
        for item in value["agents"][2:]:
            assert item["state"] == "unavailable" and item["reason"] == "AGENT_SCOPE_MISSING"
        assert client.get("/work-policy/v1/agents").status_code == 401
        assert client.get("/work-policy/v1/agents",
                          headers=agent_headers(c, signing_key, 0)).status_code == 403
    assert a.runtime.store._guard() == before
    assert c.sessions.calls == []


def test_unavailable_b_stays_explicit_without_becoming_the_default_agent(routing_peer, signing_key, monkeypatch):
    peers, loop = routing_peer
    original = peers.c.h.config.targets[0]
    deployed_versions(monkeypatch, {original.agent_id: original.agent_version})
    with TestClient(directory_application(peers.a), backend_options={"loop_factory": lambda: loop}) as client:
        result = client.get("/work-policy/v1/agents", headers=peers.c.h.headers(signing_key, "Behavior.Read"))
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["default_agent_id"] == peers.c.h.config.targets[0].agent_id
    assert value["agents"][0]["state"] == "ready"
    assert value["agents"][1]["agent_id"] == peers.b.agent_id
    assert value["agents"][1]["state"] == "unavailable"
    assert value["agents"][1]["reason"] == "AGENT_VERSION_FOLLOWING"


@pytest.mark.parametrize("retained", [98, 99, 197])
def test_receiver_busy_respects_the_real_store_predicate_bound(store, retained, monkeypatch):
    from shared_context.behavior_agent_message_api import _receiver_busy
    adapter, _ = store
    records = {str(index): SimpleNamespace(profile=SimpleNamespace(instruction=True),
               binding=SimpleNamespace(job_id=f"original-job-{index}")) for index in range(retained)}
    control = SimpleNamespace(refresh=lambda: None, records=records)
    peer = SimpleNamespace(runtime=SimpleNamespace(work_policy=control, store=adapter))
    calls = []
    original = adapter.read_snapshot
    def read(keys):
        calls.append(len(keys))
        return original(keys)
    monkeypatch.setattr(adapter, "read_snapshot", read)
    assert _receiver_busy(peer, None) is True
    assert calls and max(calls) <= 98


def test_message_listing_does_not_read_every_other_registered_turn(
        legacy_tokens, pinned_configuration, routing_peer, signing_key, monkeypatch):
    from shared_context.behavior_capabilities import available_grants
    from assistant_images_helpers import _read_bytes
    peers, loop = routing_peer
    c, a = peers.c, peers.a
    a.runtime.capability_grants = available_grants(None, ("fa-agent-collaboration-v1",))
    turn = replayed(c.h)  # select_profile + claimed_turn: one claimed human turn of A with peer collaboration
    app = directory_application(a)
    install_peer_routing(app, a, directory_application)
    path = f"/work-policy/v1/bindings/{turn.binding_id}/agent-messages"
    headers = c.h.headers(signing_key, "Behavior.Read")
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        first, used = _read_bytes(monkeypatch, lambda: client.get(path, headers=headers))
        assert first.status_code == 200, first.text
        control = a.runtime.work_policy
        for other in replayed(c.h):  # three more A instructions, registered through the retired WorkPolicyControl.register
            control.records[other.binding.binding_id] = other
            control.refresh(other.binding.binding_id)
        store = a.runtime.store
        store._scope = store._scope.model_copy(update={"max_batch_bytes": used + 16384})
        listing = client.get(path, headers=headers)
        assert listing.status_code == 200, listing.text


def test_hosted_capability_registration_uses_the_bound_peer_tool_factory():
    from shared_context.behavior_assistant_hosted import CAPABILITY_HANDLERS
    from shared_context.behavior_agent_message_tools import agent_message_capability
    assert CAPABILITY_HANDLERS["fa-agent-collaboration-v1"] is agent_message_capability

