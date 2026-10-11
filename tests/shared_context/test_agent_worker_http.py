"""Signed API -> HTTP control client -> original receipt consumer; no live/model calls."""

from threading import Event, Thread
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import A, B
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_auth import AGENT_ROLE, BehaviorAuthSettings, BehaviorJwtVerifier
from shared_context.behavior_agent_control import (
    CONNECTION_HEADER, PREFIX, AgentControlClient, AgentControlRelay, install_control_routes,
)
from shared_context import behavior_wire as wire
from shared_context.behavior_agent_worker import AgentVmBinding
from shared_context.behavior_assistant_hosted import AssistantHostedRuntime, AssistantSupervisor
from shared_context.errors import Problem
from test_agent_worker_control import OriginalConsumer, delivery, vm
from test_m3_auth import CLIENT_ID, HUMAN_OID, jwk_client, signing_key, token


@pytest.fixture
def channel(auth, jwk_client, signing_key, tmp_path):
    binding = AgentVmBinding(
        team_id=A.team, agent_id="11111111-1111-1111-1111-111111111111", hosted_session_id="agent-session-one",
        hosted_agent_version="3", vm_generation="api-logical-generation",
    )
    relay = AgentControlRelay(wait_seconds=0)
    verifier = BehaviorJwtVerifier(auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, A.team), jwk_client)

    def expected(principal):
        if principal.oid != A.oid:
            raise Problem(403, "AGENT_WORKER_BINDING_CHANGED", "the configured Agent differs")
        return binding

    app = FastAPI()
    _install_error_handlers(app)
    install_control_routes(app, relay, verifier, expected)
    encoded = token(signing_key, {"oid": A.oid, "idtyp": "app", "roles": [AGENT_ROLE]}, remove=("scp",))
    consumer = OriginalConsumer()
    supervisor = AssistantSupervisor(tmp_path / "worker", runtime_path=tmp_path / "unused-runtime")
    runtime = AssistantHostedRuntime(wire.HostedIdentity(
        agent_id=binding.agent_id, agent_name="offline-agent",
        agent_version=binding.hosted_agent_version, session_id=binding.hosted_session_id,
    ), supervisor, SimpleNamespace(api_url="https://api.example.test", audience="api://offline",
        credential=SimpleNamespace(get_token=lambda _: SimpleNamespace(token=encoded))))
    calls = []
    with TestClient(app) as server:
        def send(request):
            calls.append((request.method, request.url.path))
            reply = server.request(request.method, request.url.path, content=request.content,
                                   headers=dict(request.headers))
            return httpx.Response(reply.status_code, headers=dict(reply.headers), content=reply.content)
        client = runtime.agent_control_client(
            binding, consumer, caller_oid=A.oid,
            transport=httpx.MockTransport(send),
        )
        try:
            yield binding, relay, consumer, client, server, calls
        finally:
            consumer.release.set()
            client.close()
            client.worker.close()
            relay.close()


def test_signed_control_delivers_original_bytes_and_receipt_without_a_foundry_invocation(channel):
    binding, relay, consumer, client, server, calls = channel
    client.connect()
    request = delivery("cid-two", "rid-two")
    waiting = relay.deliver(binding, request)
    client.receive_once()
    assert waiting.result(timeout=2) == b"original-receipt-rid-two"
    assert consumer.calls == [(binding, request)]
    assert consumer.starts[0][0].caller_oid == A.oid
    assert all("invocation" not in path for _, path in calls)
    assert calls[0] == ("POST", "/behavior/v1/agent-worker/connect")
    assert ("GET", "/behavior/v1/agent-worker/next") in calls


def test_cancel_and_receipt_reads_take_priority_over_pending_turn_delivery(channel):
    binding, relay, consumer, client, server, calls = channel
    client.connect()
    first, second = delivery("cid-one", "rid-one"), delivery("cid-two", "rid-two")
    first_reply = relay.deliver(binding, first)
    client.receive_once()
    assert consumer.entered.wait(2)
    second_reply = relay.deliver(binding, second)
    cancelled = relay.cancel(binding, second.address)
    client.receive_once()
    assert cancelled.result(timeout=2) == b"original-cancel-rid-two"
    assert not first_reply.done()
    assert consumer.calls == [(binding, first)]
    original = relay.read(binding, second.address)
    client.receive_once()
    assert original.result(timeout=2) == b"original-cancel-rid-two"
    client.receive_once()
    consumer.release.set()
    assert first_reply.result(timeout=2) == b"original-receipt-rid-one"
    assert second_reply.result(timeout=2) == b"original-cancel-rid-two"


def test_stale_channel_replacement_keeps_old_dispatch_unknown_instead_of_resending():
    binding = vm()
    now = [0.0]
    relay = AgentControlRelay(wait_seconds=0, clock=lambda: now[0])
    old_context = relay.connect(binding, "original-process", "verified-agent-oid")
    waiting = relay.deliver(binding, delivery("cid-two", "rid-two"))
    now[0] = 61.0
    replacement = relay.connect(binding, "replacement-process", old_context.caller_oid)
    with pytest.raises(Problem) as unknown:
        waiting.result(timeout=2)
    assert unknown.value.body["error"]["code"] == "AGENT_WORKER_EXECUTION_UNKNOWN"
    assert replacement.control_connection_id != old_context.control_connection_id
    with pytest.raises(Problem):
        relay.peer(binding, connection_id=old_context.control_connection_id, caller_oid=old_context.caller_oid)
    assert relay.peer(binding).next(0) is None
    relay.close()


@pytest.mark.parametrize("case", ["human", "other-agent", "team", "session", "version", "generation", "duplicate-json"])
def test_signed_control_rejects_wrong_identity_or_vm_before_creating_a_channel(channel, signing_key, case):
    binding, relay, consumer, client, server, calls = channel
    actor = HUMAN_OID if case == "human" else B.oid if case == "other-agent" else A.oid
    changes = {"oid": actor, "scp": "Behavior.Read"} if case == "human" else {
        "oid": actor, "idtyp": "app", "roles": [AGENT_ROLE],
    }
    encoded = token(signing_key, changes, remove=() if case == "human" else ("scp",))
    body = {"vm": binding.model_dump(mode="json"), "process_id": "original-process"}
    field = {"team": "team_id", "session": "hosted_session_id",
             "version": "hosted_agent_version", "generation": "vm_generation"}.get(case)
    if field:
        body["vm"][field] = "99" if case == "version" else "another"
    headers = {"Authorization": "Bearer " + encoded, "Content-Type": "application/json"}
    if case == "duplicate-json":
        response = server.post(PREFIX + "/connect", content=b'{"process_id":"one","process_id":"two"}', headers=headers)
    else:
        response = server.post(PREFIX + "/connect", json=body, headers=headers)
    assert response.status_code in {401, 403, 422}
    with pytest.raises(Problem):
        relay.peer(binding)
    assert consumer.calls == []


def test_explicit_disconnect_then_read_keeps_the_finished_rid_and_does_not_execute_it_again(channel):
    binding, relay, consumer, client, server, calls = channel
    client.connect()
    request = delivery("cid-two", "rid-two")
    reply = relay.deliver(binding, request)
    client.receive_once()
    assert reply.result(timeout=2) == b"original-receipt-rid-two"
    original_context = relay.peer(binding).context
    client.disconnect()
    client.connect()
    original = relay.read(binding, request.address)
    client.receive_once()
    assert original.result(timeout=2) == b"original-receipt-rid-two"
    assert len(consumer.calls) == 1
    assert relay.peer(binding).context.control_connection_id != original_context.control_connection_id


def test_wait_loop_keeps_receiving_controls_while_the_sdk_turn_is_running(channel):
    binding, relay, consumer, client, server, calls = channel
    relay.wait_seconds = 0.1
    client.connect()
    stopping = Event()
    loop = Thread(target=client.run, args=(stopping,), daemon=True)
    loop.start()
    try:
        first = relay.deliver(binding, delivery("cid-one", "rid-one"))
        assert consumer.entered.wait(2)
        address = delivery("cid-two", "rid-two").address
        assert relay.cancel(binding, address).result(timeout=2) == b"original-cancel-rid-two"
        assert not first.done()
        consumer.release.set()
        assert first.result(timeout=2) == b"original-receipt-rid-one"
    finally:
        consumer.release.set()
        stopping.set()
        loop.join(timeout=2)
    assert not loop.is_alive()
    with pytest.raises(Problem):
        relay.peer(binding)


def test_full_turn_buffer_still_accepts_priority_cancel_without_starting_another_turn(channel):
    binding, relay, consumer, client, server, calls = channel
    client.connect()
    turns = [delivery("cid-two", "rid-" + str(index)) for index in range(32)]
    for request in turns:
        relay.deliver(binding, request)
    cancellation = relay.cancel(binding, turns[-1].address)
    client.receive_once()
    assert cancellation.result(timeout=2) == b"original-cancel-rid-31"
    assert consumer.calls == []


def test_a_late_old_turn_result_is_not_sent_on_the_replacement_connection(channel):
    binding, relay, consumer, client, server, calls = channel
    client.connect()
    original = relay.deliver(binding, delivery("cid-one", "rid-one"))
    client.receive_once()
    assert consumer.entered.wait(2)
    client.disconnect()
    client.connect()
    replies = len([call for call in calls if call == ("POST", PREFIX + "/reply")])
    consumer.release.set()
    client.worker.close()
    assert len([call for call in calls if call == ("POST", PREFIX + "/reply")]) == replies
    with pytest.raises(Problem) as unknown:
        original.result(timeout=2)
    assert unknown.value.body["error"]["code"] == "AGENT_WORKER_EXECUTION_UNKNOWN"
    assert consumer.receipts[delivery("cid-one", "rid-one").address] == b"original-receipt-rid-one"
