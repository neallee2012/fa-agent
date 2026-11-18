"""從 test_agent_message_continuation.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

from dataclasses import replace
from types import SimpleNamespace
from pydantic import SecretStr
from shared_context import behavior_wire as wire
from shared_context import behavior_assistant_wire as aw
from agent_runtime_routing_helpers import agent_headers


def start_peer(m):
    response = m.client.post(m.path, json=m.command, headers=m.headers)
    assert response.status_code == 200, response.text
    receiver = next(iter(m.b.runtime.work_policy.records.values()))
    m.c.authority.record_instruction_activity(m.turn.session)
    starts = []
    workers = m.b.runtime.dep.workers
    original = workers.adapter
    def start(command):
        starts.append(m.b.runtime.dep.native_launches.prepare(command))
    workers.adapter = replace(original, start=start)
    try:
        status = m.a.runtime.controller.step(m.turn.binding.binding_id)
    finally:
        workers.adapter = original
    assert len(starts) == 1
    return SimpleNamespace(receiver=receiver, launch=starts[0][0], permit=starts[0][1], status=status)


def claim_peer(m, started, signing_key, *, acquire=True):
    native = m.b.runtime.dep.native_launches
    receiver, launch = started.receiver, started.launch
    actor = m.c.h.config.identity.auth().actors[receiver.binding.agent_oid]
    from shared_context.behavior_auth import BehaviorPrincipal
    principal = BehaviorPrincipal(receiver.binding.tenant_id, actor.oid, actor.team, "agent", actor.actor,
                                  frozenset({"read", "consume", "execute"}))
    identity = wire.HostedIdentity(**{name: getattr(launch.target, name)
                                    for name in ("agent_id", "agent_name", "agent_version", "session_id")})
    claimed = native.claim(receiver.binding.binding_id, launch.launch_id, wire.HostedClaimRequest(
        request_id="original-b-native-claim", invocation_id="original-b-invocation",
        identity=identity, process_id="synthetic-b-process", launch_permit=started.permit), principal)
    headers = {**agent_headers(m.c, signing_key, 1),
               "x-behavior-report-token": claimed.report_token.get_secret_value()}
    command = wire.HostedProbeBody(request_id="assistant-native-claim", invocation_id=claimed.assignment.invocation_id,
        identity=identity, process_id=claimed.assignment.process_id, claim_id=claimed.assignment.claim_id)
    prefix = f"/behavior/v1/bindings/{receiver.binding.binding_id}/assistant"
    response = m.client.post(prefix + f"/launches/{launch.launch_id}/claim",
                             content=command.model_dump_json(), headers=headers)
    assert response.status_code == 200, response.text
    assignment = aw.NativeClaimReceipt.model_validate_json(response.content).assignment
    headers = {**agent_headers(m.c, signing_key, 1),
               "x-behavior-assistant-ticket": response.headers["x-behavior-assistant-ticket"]}
    if acquire:
        acquired = m.client.post(prefix + f"/sessions/{assignment.session_id}/acquire",
                                 json={"request_id": "sdk-start"}, headers=headers)
        assert acquired.status_code == 200, acquired.text
    session = m.b.assistant.authority.session(
        assignment.session_id, SecretStr(headers["x-behavior-assistant-ticket"]), principal)
    return SimpleNamespace(assignment=assignment, headers=headers, principal=principal, claimed=claimed,
                           session=session,
                           claim=wire.HostedProbeRequest(**command.model_dump(), report_token=claimed.report_token))
