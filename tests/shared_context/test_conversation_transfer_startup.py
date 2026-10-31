"""New synthetic native-mode startup fixture, not the missing Window2 bundle.

The predecessor goes through the real inventory/quiescence reader. Standing
materials use initial_records and the layout the retired operator installer
wrote (its plan/approval/history rows live on only here), then the real
standing/current-runtime validators run. The historical Teams cleanup/WebHandoff/ACK outer chain is not
reconstructed here. SDK states/exits are synthetic, Table/Blob/platform I/O is
local, and no SDK process, model or Azure execution is claimed.
"""

import base64
from dataclasses import replace
from datetime import timedelta
import json
import socket
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant import key as assistant_key
from shared_context.behavior_assistant_wire import InstructionSubmit, SaveTextReply, TextReply
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_conversation_transfer import TransferCommand, head_key, prepare, transfer_key
from shared_context.behavior_instruction import conversation_key
from shared_context.behavior_foundry import FoundryExecutor
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_runtime import material
from shared_context.behavior_sdk_checkpoint import save_state
from shared_context.behavior_sdk_state import SaveSdkState, SdkState
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_work_policy import CostObservation, SteadyWorkPolicy, WorkPolicyControl, work_policy_key
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.source_materials import digest
from test_behavior_assistant_server import AssistantHarness
from assistant_sdk_fixture import review_source
from test_behavior_store import WireTransport, json_response
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from hosted_versions_stub import deployed_versions


class StartupTableTransport(WireTransport):
    def send(self, request, **kwargs):
        if request.method == "GET" and urlsplit(request.url).path.rstrip("()") == "/Behavior":
            self.requests.append(request)
            assert parse_qs(urlsplit(request.url).query)["$filter"] == ["PartitionKey eq 'authority'"]
            items = [{**entity, "Payload": base64.b64encode(entity["Payload"]).decode(),
                      "Payload@odata.type": "Edm.Binary", "odata.etag": entity.metadata["etag"]}
                     for (table, partition, _), entity in sorted(self.backing.rows.items())
                     if table == "Behavior" and partition == "authority"]
            return json_response(request, 200, {"value": items}, {})
        return super().send(request, **kwargs)


class SyntheticSessionClient:
    credential = object()

    def __init__(self, target, observations):
        self.target, self.observations = target, observations

    def for_target(self, target):
        return SyntheticSessionClient(target, self.observations)

    def create_session(self, *, before_send):
        before_send()
        self.observations.calls.append(("create", self.target))
        if self.observations.lose_create:
            raise Problem(503, "FOUNDRY_SESSION_UNKNOWN", "Synthetic create response loss")

    def verify_session(self, *, before_send):
        before_send()
        self.observations.calls.append(("get", self.target))
        # An existing conversation continues only on an active SID (ADR-0009 §10); idle is refused at admission.
        return SimpleNamespace(agent_session_id=self.target.session_id, status="active")


@pytest.fixture
def synthetic_standing_v1(tmp_path, jwk_client, monkeypatch, request):
    def forbidden(*args, **kwargs):
        pytest.fail("this startup fixture must not perform external I/O or launch a process")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("test_behavior_server.WireTransport", StartupTableTransport)
    monkeypatch.delenv("FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE", raising=False)
    h = AssistantHarness(tmp_path, jwk_client)
    try:
        h.legacy()
        h.activate()
        config, runtime = h.config, h.runtime
        deployed_versions(monkeypatch, {config.targets[0].agent_id: "41"}, others=True)
        store = runtime.store
        assert runtime.mode == runtime.dep.workers.mode == "native"
        review_source(store, config)
        human = BehaviorPrincipal(config.identity.tenant_id, config.identity.human_oid,
                                  config.identity.team_id, "human", None,
                                  frozenset({"read", "approve", "submit", "acknowledge"}))
        policy = SteadyWorkPolicy(
            policy_id="synthetic-transfer-startup-v1", revision=1, configuration_sha256=config.sha256,
            evidence_mode="native", authority_domain=config.table.scope.authority_domain,
            control_binding=config.workers.control_binding, active=True, read_enabled=True,
            template=config.bindings[0],
            profile=config.assistant.profiles[0].model_copy(update={"claim_id": "synthetic-standing-reviewed"}),
            legacy_template_policy=config.policies[0].policy, cost_scope_sha256=digest(config.resource_bindings()),
            cost_observer_oid=config.evidence.observer_oid, minimum_observed_microusd=1_000_000,
            work_estimate_microusd=1_000_000, cost_max_age_seconds=300,
            queue_seconds=min(600, config.delivery.max_queue_seconds), max_model_requests=1,
        )
        if getattr(request, "param", None) == "routine":
            from shared_context.behavior_routine_wire import DailyRoutineDefinition
            policy = policy.model_copy(update={"routines": (DailyRoutineDefinition(
                routine_id="native-daily", owner_oid=human.oid, approved_version="native-daily-v1",
                time_zone="UTC", local_time=h.time.strftime("%H:%M"), enabled=True,
                content="Read the currently approved source.",
            ),)})
        predecessor = MigrationStore(h.composition.clients[0], config.table.scope)
        before = predecessor.read_inventory()
        assert installed_origin(predecessor, config) == config
        observation = CostObservation(
            evidence_id="synthetic-transfer-cost", evidence_mode="native",
            observer_oid=policy.cost_observer_oid, authority_domain=policy.authority_domain,
            control_binding=policy.control_binding, cost_scope_sha256=policy.cost_scope_sha256,
            observed_cumulative_microusd=1_000_000, legacy_unresolved_reserve_microusd=0,
            legacy_liability_sha256=digest(dict(before.entity_sha256)), evidence_sha256="a" * 64,
            observed_at=h.time,
        )
        control = WorkPolicyControl(runtime, policy, lambda: observation)
        values = control.initial_records(human, "synthetic-transfer-approved", h.time + timedelta(minutes=5))
        writes = [ControlWrite(row_id=k, mode="create", data=v.model_dump(mode="json"),
                               material=material(v.model_dump(mode="json")) if k == control.policy_key else None)
                  for k, v in zip((control.policy_key, control.cost_key, control.registry_key), values)]
        writes.append(ControlWrite(row_id=work_policy_key("current", policy.authority_domain), mode="create",
                                   data={"schema_version": "fa-standing-policy-current-v1"}))
        store.transact(ControlRequest(request_id=work_policy_key("install", "synthetic-transfer-install-v1"),
                                      request_digest=digest(policy.model_dump(mode="json"))),
                       lambda _: ControlChanges(writes=tuple(writes), result={}), expected_guard=before.guard)
        control.attach()
        authority = h.composition.assistant.authority
        authority.artifacts.store = InMemoryArtifactStore(config.identity.tenant_id)
        sessions = SimpleNamespace(calls=[], lose_create=False)
        native = runtime.dep.native_launches
        runtime.dep.workers.adapter = FoundryExecutor(
            native, tuple(SyntheticSessionClient(target, sessions) for target in native.targets)
        ).adapter(verify_readiness=lambda _: h.bundle.worker)
        runtime.dep.delivery.registration = replace(runtime.dep.delivery.registration,
                                                   verify_readiness=lambda: h.bundle.delivery)
        runtime.dep.inbox.registration = replace(runtime.dep.inbox.registration,
                                                 verify_deployment=lambda _: h.bundle.inbox)
        case = SimpleNamespace(h=h, control=control, authority=authority, human=human,
                               before=before, native=native, sessions=sessions)
        read_startup(case)
        yield case
    finally:
        h.composition.close()


def read_startup(c):
    return MigrationStore(c.h.composition.clients[0], c.h.config.table.scope).read_inventory()


def claimed_turn(c, request_id, conversation_id=None, *, acquire=True):
    runtime, workers = c.h.runtime, c.h.runtime.dep.workers
    binding, _ = c.control.submit(c.human, InstructionSubmit(
        request_id=request_id, content="Synthetic startup-reader conversation.",
        conversation_id=conversation_id))
    reservations = []
    original = workers.adapter
    workers.adapter = replace(workers.adapter, start=lambda start: reservations.append(c.native.prepare(start)),
                              verify=c.native.verify)
    try:
        workers.start(binding.job_id, binding.owner)
    finally:
        workers.adapter = original
    launch, permit = reservations[0]
    actor = c.h.config.identity.auth().actors[binding.agent_oid]
    agent = BehaviorPrincipal(binding.tenant_id, binding.agent_oid, binding.team_id, "agent", actor.actor,
                              frozenset({"read", "consume", "execute"}))
    identity = wire.HostedIdentity(**{name: getattr(launch.target, name)
                                    for name in ("agent_id", "agent_name", "agent_version", "session_id")})
    claimed = c.native.claim(binding.binding_id, launch.launch_id, wire.HostedClaimRequest(
        request_id="synthetic-native-claim", invocation_id="synthetic-invocation-" + request_id,
        identity=identity, process_id="synthetic-no-process", launch_permit=permit), agent)
    claim = wire.HostedProbeRequest(
        request_id="synthetic-assistant-claim", invocation_id=claimed.assignment.invocation_id,
        identity=identity, process_id=claimed.assignment.process_id, claim_id=claimed.assignment.claim_id,
        report_token=claimed.report_token)
    assignment, ticket = c.authority.native_claim(binding.binding_id, launch.launch_id, claim, agent)
    session = c.authority.session(assignment.session_id, ticket, agent)
    if acquire:
        c.authority.acquire(session, wire.Command(request_id="sdk-start"))
        session = c.authority.session(assignment.session_id, ticket, agent)
    return SimpleNamespace(binding=binding, launch=launch, claimed=claimed, claim=claim,
                           agent=agent, assignment=assignment, ticket=ticket, session=session)


def saved_turn(c, request_id, conversation_id=None, previous=None, *, stop=None):
    opened = claimed_turn(c, request_id, conversation_id)
    binding, launch, claimed, claim, agent, assignment, ticket, session = (
        getattr(opened, name) for name in ("binding", "launch", "claimed", "claim", "agent", "assignment", "ticket", "session"))
    runtime, workers = c.h.runtime, c.h.runtime.dep.workers
    if previous is not None:
        assert c.authority.restore(session, agent) == previous
    sid = assignment.sdk_session_id
    events = ([{"type": "session.start", "data": {"sessionId": sid}}] if previous is None else
              [json.loads(line) for line in previous.files["events.jsonl"].splitlines()])
    events.extend(({"type": "user.message", "data": {"content": assignment.task.instruction.content}},
                   {"type": "assistant.message", "data": {"content": "Synthetic saved reply."}}))
    state = SdkState(sdk_session_id=sid, files={
        "events.jsonl": "".join(json.dumps(event) + "\n" for event in events),
        "workspace.yaml": "id: " + sid + "\n",
    })
    stop = stop or wire.AssistantSdkStop(pid=99, exit_code=0, graceful=True)
    exited = stop.release is None
    reference = save_state(c.authority, session, SaveSdkState(request_id="sdk-state", state=state, sdk_stop=stop))
    c.authority.save_text_reply(session, SaveTextReply(request_id="save-text-reply", sdk_stop=stop,
        archive=reference, reply=TextReply(text="Synthetic saved reply.", sdk_session_id=sid,
            conversation_id=assignment.task.instruction.conversation_id,
            turn_number=assignment.task.instruction.turn_number)), agent)
    result = c.authority.result(launch.start.binding)
    probe = c.native.probe(launch.start.binding)
    reported = c.native.report(binding.binding_id, launch.launch_id, probe.probe_id, wire.HostedReport(
        **claim.model_dump(), probe_id=probe.probe_id, process_generation=launch.process_generation,
        binding_digest=digest(launch.start.binding.model_dump(mode="json")),
        supervisor_id=claimed.assignment.process_id, sequence=1, phase="sealed", child_pid=stop.pid,
        outcome="succeeded", sdk_stop=stop, exit_code=stop.exit_code, child_exited=exited,
        result=wire.AssistantExecutionResult(session_id=assignment.session_id,
                                            result_digest=digest(result.model_dump(mode="json")))), agent)
    workers.observe(binding.job_id, reported.evidence_id, binding.owner)
    workers.forward_result(binding.job_id, binding.owner, runtime.dep.delivery)
    assert workers.inspect(binding.job_id).state == "succeeded"
    assert workers.inspect(binding.job_id).result_forwarded
    return SimpleNamespace(binding=binding, assignment=assignment, state=state, archive=reference)


def failed_turn(c, conversation_id):
    runtime, workers = c.h.runtime, c.h.runtime.dep.workers
    binding, _ = c.control.submit(c.human, InstructionSubmit(
        request_id="synthetic-version-failed", conversation_id=conversation_id,
        content="This failed message must not be automatically resent."))
    def refuse(start):
        c.native.prepare(start)
        return c.native.fence_unclaimed(start.binding, observed_agent_version="999")
    original = workers.adapter
    workers.adapter = replace(workers.adapter, start=refuse, verify=c.native.verify)
    try:
        workers.start(binding.job_id, binding.owner)
    finally:
        workers.adapter = original
    workers.forward_result(binding.job_id, binding.owner, runtime.dep.delivery)
    assert workers.inspect(binding.job_id).state == "failed"
    assert workers.inspect(binding.job_id).result_forwarded
    return binding


def test_transfer_pending_ready_and_post_turn_are_readable_at_startup(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    first = saved_turn(c, "synthetic-first")
    cid = first.assignment.task.instruction.conversation_id
    failed = failed_turn(c, cid)
    original = read_startup(c)
    c.sessions.calls.clear()
    c.sessions.lose_create = True
    deployed_versions(monkeypatch, "42")
    command = TransferCommand(request_id="synthetic-environment", failed_job_id=failed.job_id,
                              expected_agent_version="42")
    with pytest.raises(Problem) as lost:
        prepare(c.control, c.authority, c.human, cid, command)
    assert lost.value.body["error"]["code"] == "CONVERSATION_TRANSFER_UNKNOWN"
    pending = read_startup(c)
    transfer = transfer_key(cid, failed.job_id)
    assert sum(row.payload["data"].get("kind") == "conversation_environment_transfer"
               for row in pending.rows.values()) == 1
    assert not any(row.payload["data"].get("kind") == "conversation_environment_head"
                   for row in pending.rows.values())
    assert pending.rows[conversation_key(cid)] == original.rows[conversation_key(cid)]
    assert len(c.sessions.calls) == 1 and c.sessions.calls[0][0] == "create"
    target = c.sessions.calls[0][1]

    prepared = prepare(c.control, c.authority, c.human, cid, command, reconcile=True)
    assert prepared.state == "ready" and prepared.transfer_id == transfer
    assert prepared.sdk_restored is False and prepared.failed_message_resent is False
    ready = read_startup(c)
    assert ready.rows[transfer] == pending.rows[transfer]
    assert ready.rows[head_key(cid)].payload["data"] == {
        "kind": "conversation_environment_head", "transfer_id": transfer}
    assert ready.rows[conversation_key(cid)].payload["data"]["hosted_session_id"] == target.session_id
    assert c.sessions.calls == [("create", target), ("get", target)]

    third = saved_turn(c, "synthetic-human-followup", cid, first.state)
    assert third.assignment.sdk_session_id == first.assignment.sdk_session_id
    assert third.assignment.task.instruction.turn_number == 3
    assert third.assignment.task.instruction.previous_archive_id == assistant_key("text-archive", first.binding.job_id)
    assert c.control.records[third.binding.binding_id].environment_transfer_id == transfer
    after = read_startup(c)
    assert after.rows[transfer] == pending.rows[transfer]
    assert after.rows[head_key(cid)] == ready.rows[head_key(cid)]
    assert after.rows[worker_key("job", failed.job_id)] == original.rows[worker_key("job", failed.job_id)]
    assert after.rows[assistant_key("text-archive", first.binding.job_id)] == original.rows[
        assistant_key("text-archive", first.binding.job_id)]
    assert len(c.control.records) == 3
    assert [method for method, _ in c.sessions.calls].count("create") == 1
