"""A stopped original peer must release its queue without restarting that work."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant import key
from shared_context.behavior_foundry import FoundryExecutor
from shared_context.errors import Problem
from test_agent_message_api import message_turn
from test_agent_message_continuation import save_peer
from test_agent_runtime import peer_runtime
from test_agent_runtime_routing import agent_headers, routing_peer
from test_conversation_transfer_startup import claimed_turn, read_startup, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_lost_session_closure import LostHostedClient
from test_m3_auth import jwk_client, signing_key


@pytest.mark.parametrize("claimed", [False, True], ids=["unclaimed-fence", "platform-stop"])
@pytest.mark.parametrize("later_active", [True, False], ids=["later-active", "later-ended"])
def test_proven_stopped_peer_without_sdk_session_releases_the_original_receiver(
    message_turn, signing_key, claimed, later_active,
):
    m = message_turn
    accepted = m.client.post(m.path, json=m.command, headers=m.headers)
    assert accepted.status_code == 200, accepted.text
    control, workers, native = m.b.runtime.work_policy, m.b.runtime.dep.workers, m.b.runtime.dep.native_launches
    receiver = next(iter(control.records.values()))
    principal = m.b.verifier(m.client.build_request("POST", "/", headers=agent_headers(m.c, signing_key, 1)))
    starts = []
    original = workers.adapter

    def lost_start(command):
        launch, permit = native.prepare(command)
        starts.append(launch)
        request = wire.HostedClaimRequest(
            request_id="original-native-claim", invocation_id="original-lost-invocation",
            identity=wire.HostedIdentity(
                agent_id=launch.target.agent_id, agent_name=launch.target.agent_name,
                agent_version=launch.target.agent_version, session_id=launch.target.session_id),
            process_id="original-lost-process", launch_permit=permit)
        if claimed:
            native.claim(receiver.binding.binding_id, launch.launch_id, request, principal)
        else:
            starts[-1] = SimpleNamespace(launch=launch, request=request)
        raise Problem(503, "SYNTHETIC_LOST_START", "The original invocation response is unknown")

    workers.adapter = replace(original, start=lost_start)
    try:
        m.a.runtime.controller.step(m.turn.binding.binding_id)
    finally:
        workers.adapter = original
    assert len(starts) == 1
    old = workers.inspect(receiver.binding.job_id)
    assert old.state in {"starting", "capacity_uncertain"} and old.generation == 1
    pending = m.client.post(m.path.removesuffix("send") + "read", json={
        "request_id": "agent-message-read", "message_key": receiver.request_id}, headers=m.headers)
    assert pending.status_code == 200 and pending.json()["message"]["status"] == "unknown"
    session_key = key("session", key("session-id", old.binding.start_request_id))
    assert m.b.runtime.store.read_snapshot((session_key,))[session_key] is None

    observations = SimpleNamespace(calls=[], status="active", session_id=None, after_stop="active")
    executor = FoundryExecutor(native, tuple(LostHostedClient(target, observations) for target in native.targets))
    next_starts = []

    def next_start(command):
        next_starts.append(native.prepare(command)[0])

    workers.adapter = replace(original, start=next_start, query=executor.query, stop=executor.stop,
                              stopped_session=executor.stopped_session)
    try:
        origin = SimpleNamespace(receiver=m.a.runtime.work_policy.records[m.turn.binding.binding_id],
                                 launch=m.turn.launch)
        original_a = SimpleNamespace(assignment=m.turn.assignment, session=m.turn.session, headers=m.headers,
                                     principal=m.turn.agent, claim=m.turn.claim, claimed=m.turn.claimed)
        save_peer(m, origin, original_a, "The original peer result is still unknown.", composition=m.a)
        assert m.a.runtime.controller.step(m.turn.binding.binding_id).result_forwarded
        assert workers.inspect(receiver.binding.job_id).state == "capacity_uncertain"

        later = claimed_turn(m.c, "new-root-after-unknown-peer")
        headers = {**agent_headers(m.c, signing_key, 0),
                   "x-behavior-assistant-ticket": later.ticket.get_secret_value()}
        path = (f"/behavior/v1/bindings/{later.binding.binding_id}/assistant"
                f"/sessions/{later.assignment.session_id}/agent-message-send")
        request = {"request_id": "agent-message-send", "message": {
            "message_id": "new-original-handoff", "recipient_agent_id": m.b.agent_id,
            "kind": "handoff", "content": "A different new task; do not replay the older task.", "refs": []}}
        queued = m.client.post(path, json=request, headers=headers)
        assert queued.status_code == 200, queued.text
        assert queued.json()["message"]["decision"]["reason"] == "original_receiver_busy"
        if not later_active:
            later_origin = SimpleNamespace(receiver=m.a.runtime.work_policy.records[later.binding.binding_id],
                                           launch=later.launch)
            later_a = SimpleNamespace(assignment=later.assignment, session=later.session, headers=headers,
                                      principal=later.agent, claim=later.claim, claimed=later.claimed)
            save_peer(m, later_origin, later_a, "The newer A turn has ended.", composition=m.a)
        m.a.runtime.controller.step(later.binding.binding_id)
        assert next_starts == []

        # Only a fresh, matching terminal platform status permits the original stop path to close.
        if claimed:
            observations.status = "expired"
        else:
            before = workers.inspect(receiver.binding.job_id)
            provider_calls = list(observations.calls)
            evidence_id = native.fence_unclaimed(before.binding)
            workers.observe(receiver.binding.job_id, evidence_id, before.owner)
            with pytest.raises(Problem):
                native.claim(receiver.binding.binding_id, starts[0].launch.launch_id, starts[0].request, principal)
            assert observations.calls == provider_calls
        m.a.runtime.controller.step(later.binding.binding_id)
        m.a.runtime.controller.step(later.binding.binding_id)
        stopped = workers.inspect(receiver.binding.job_id)
        assert stopped.state in {"failed", "cancelled"} and stopped.result_forwarded
        assert len(next_starts) == int(later_active)
        assert all(launch.start.binding.job_id != receiver.binding.job_id for launch in next_starts)
        assert len(starts) == 1
        assert m.b.runtime.store.read_snapshot((session_key,))[session_key] is None
        assert m.a.runtime.work_policy.read_reply(
            m.c.human, m.turn.assignment.task.instruction.conversation_id,
            m.turn.assignment.task.instruction.turn_number,
        ).text == "The original peer result is still unknown."
        m.a.runtime.controller.step(later.binding.binding_id)
        assert len(starts) == 1 and len(next_starts) == int(later_active)
        read_startup(m.c)
    finally:
        workers.adapter = original
