"""Real genesis validation and signed peer claims; later standing rows are synthetic setup."""


import pytest

from shared_context.errors import Problem
from test_agent_runtime import peer_runtime
from agent_runtime_routing_helpers import routing_peer
from test_behavior_assistant_server import AssistantHarness
from test_conversation_transfer_startup import synthetic_standing_v1
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client
from test_m3_auth import signing_key


@pytest.fixture
def origin_peer(request, monkeypatch):
    activate = AssistantHarness.activate

    def verify_root(harness):
        activate(harness)
        # Validate the real genesis before this fixture adds its synthetic standing-history layout.
        harness.composition.installation()

    monkeypatch.setattr(AssistantHarness, "activate", verify_root)
    return request.getfixturevalue("routing_peer")


@pytest.mark.parametrize("mismatch", ["missing", "truthy_flag", "owner", "config", "store", "process", "fence"])
def test_peer_refuses_an_unverified_or_different_origin_without_falling_back_to_inventory(
    origin_peer, monkeypatch, mismatch,
):
    from shared_context import behavior_server as server
    peers, _loop = origin_peer
    owner, peer = peers.a, peers.b

    def unexpected(*_args, **_kwargs):
        pytest.fail("a mismatched origin must fail before inventory validation")

    with monkeypatch.context() as fault:
        fault.setattr(peer.runtime.store, "read_inventory", unexpected)
        if mismatch == "missing":
            fault.setattr(owner, "_verified_origin", None)
        elif mismatch == "truthy_flag":
            fault.setattr(owner, "_verified_origin", True)
        elif mismatch == "owner":
            fault.setattr(peer, "_agent_message_owner", object())
        elif mismatch == "config":
            fault.setattr(peer, "config", owner.config.model_copy())
        elif mismatch == "store":
            fault.setattr(peer.runtime, "store", object())
        elif mismatch == "process":
            process_id = server.getpid()
            fault.setattr(server, "getpid", lambda: process_id + 1)
        else:
            fault.setattr(peer.runtime.store, "_installation_fence", None)
        with pytest.raises(Problem) as refused:
            peer.installation()
        assert refused.value.status == 503
        assert refused.value.body["error"]["code"] == "AGENT_ORIGIN_UNAVAILABLE"


def test_a_refused_root_leaves_its_peer_no_origin_to_reuse(origin_peer):
    peers, _loop = origin_peer
    owner, peer = peers.a, peers.b
    # This fixture's table carries a retired standing-policy pointer, so the root's origin check refuses it.
    with pytest.raises(Problem):
        owner.installation()
    with pytest.raises(Problem) as refused:
        peer.installation()
    assert refused.value.body["error"]["code"] == "AGENT_ORIGIN_UNAVAILABLE"


def test_peer_keeps_the_real_installation_fence_and_atomic_probe_reads(origin_peer, monkeypatch):
    from shared_context import behavior_server as server
    peers, _loop = origin_peer
    store = peers.b.runtime.store
    checked, probes = [], []
    check, probe = store._check_installation, server._authority_probe_readback

    def checked_fence():
        checked.append(True)
        return check()

    def checked_probe(actual_store, origin, **kwargs):
        probes.append((actual_store, origin))
        return probe(actual_store, origin, **kwargs)

    monkeypatch.setattr(store, "_check_installation", checked_fence)
    monkeypatch.setattr(server, "_authority_probe_readback", checked_probe)
    peers.b.installation()
    assert checked and probes == [(store, peers.a.config)]
    assert peers.b.runtime.work_policy.installed() == peers.policy
    assert peers.b.runtime.dep.native_launches.targets[0].agent_id == peers.policy.agent_id


@pytest.mark.parametrize("changed, code", [
    ("fence", "MIGRATION_INSTALLATION_CHANGED"),
    ("probe", "BEHAVIOR_AUTHORITY_PROBE_REQUIRED"),
])
def test_retained_origin_never_hides_changed_installation_or_probe_rows(origin_peer, monkeypatch, changed, code):
    from shared_context.behavior_server import _probe_layout
    peers, _loop = origin_peer
    store = peers.b.runtime.store
    target = (store._installation_fence[0] if changed == "fence"
              else _probe_layout(peers.a.config)[0]["proof"])
    read = store._read

    def changed_row(key, model, *args, **kwargs):
        result = read(key, model, *args, **kwargs)
        if key == "behavior:v1:row:" + target and result is not None:
            row, etag = result
            data = ({**row.data, "configuration_sha256": "f" * 64} if changed == "fence"
                    else {"invalid_probe": True})
            return row.model_copy(update={"data": data}), etag
        return result

    monkeypatch.setattr(store, "_read", changed_row)
    with pytest.raises(Problem) as refused:
        peers.b.installation()
    assert refused.value.body["error"]["code"] == code
