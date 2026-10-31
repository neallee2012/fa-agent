"""Real genesis validation and signed peer claims; later standing rows are synthetic setup."""

from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_hosted import BehaviorExecutorClient
from shared_context.behavior_peer_runtime import install_peer_routing
from shared_context.behavior_server import application
from shared_context.errors import Problem
from test_agent_runtime import peer_runtime
from test_agent_runtime_routing import agent_headers, routing_peer, scoped_app
from test_behavior_assistant_server import AssistantHarness
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def origin_peer(request, monkeypatch):
    activate = AssistantHarness.activate

    def verify_root(harness):
        activate(harness)
        # Validate the real genesis before this fixture adds its synthetic standing-history layout.
        harness.composition.installation()

    monkeypatch.setattr(AssistantHarness, "activate", verify_root)
    return request.getfixturevalue("routing_peer")


def test_first_signed_peer_claim_uses_the_validated_origin_without_inventory(
    origin_peer, signing_key, monkeypatch,
):
    peers, loop = origin_peer
    c, owner, peer = peers.c, peers.a, peers.b
    binding, _ = peer.runtime.work_policy.submit(c.human, InstructionSubmit(
        request_id="first-peer-claim", content="Synthetic claim without a model run."))
    native, workers = peer.runtime.dep.native_launches, peer.runtime.dep.workers
    prepared = []
    adapter = workers.adapter
    workers.adapter = replace(adapter, start=lambda start: prepared.append(native.prepare(start)),
                              verify_readiness=lambda _: c.h.bundle.worker)
    try:
        workers.start(binding.job_id, binding.owner)
    finally:
        workers.adapter = adapter
    launch, permit = prepared[0]
    app = scoped_app(owner)
    def receiver_application(composition):
        mounted = application(composition)
        mounted.state.instruction_runner = False
        return mounted
    install_peer_routing(app, owner, receiver_application)
    scans = []
    inventory = peer.runtime.store.read_inventory

    def counted_inventory(*args, **kwargs):
        scans.append(True)
        return inventory(*args, **kwargs)

    monkeypatch.setattr(peer.runtime.store, "read_inventory", counted_inventory)
    encoded = agent_headers(c, signing_key, 1)["Authorization"].removeprefix("Bearer ")
    credential = SimpleNamespace(get_token=lambda *_args, **_kwargs: SimpleNamespace(token=encoded))
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as server:
        def send(request):
            response = server.request(request.method, request.url.path,
                                      content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))

        client = BehaviorExecutorClient("https://offline.example", owner.config.identity.audience, credential,
                                       transport=httpx.MockTransport(send))
        receipt, error = None, None
        try:
            receipt = client.claim(binding.binding_id, launch.launch_id, wire.HostedClaimRequest(
                request_id="original-first-peer-claim", invocation_id="original-first-peer-invocation",
                identity=wire.HostedIdentity(
                    agent_id=launch.target.agent_id, agent_name=launch.target.agent_name,
                    agent_version=launch.target.agent_version, session_id=launch.target.session_id),
                process_id="original-peer-process", launch_permit=permit))
        except Problem as failure:
            error = failure
        finally:
            client.close()
    assert scans == [], "the first peer claim must not replay the complete authority inventory"
    assert error is None and receipt.claim_state == "fresh"
    assert c.sessions.calls == []


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
