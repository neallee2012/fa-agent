"""New synthetic native-mode startup fixture, not the missing Window2 bundle.

The predecessor goes through the real inventory/quiescence reader. Standing
materials use initial_records and the layout the retired operator installer
wrote (its plan/approval/history rows live on only here), then the real
standing/current-runtime validators run. The historical Teams cleanup/WebHandoff/ACK outer chain is not
reconstructed here. SDK states/exits are synthetic, Table/Blob/platform I/O is
local, and no SDK process, model or Azure execution is claimed.
"""

from dataclasses import replace
from datetime import timedelta
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest

from shared_context.behavior_assistant import key as assistant_key
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_conversation_transfer import head_key, transfer_key
from shared_context.behavior_instruction import conversation_key
from shared_context.behavior_foundry import FoundryExecutor
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_runtime import material
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_work_policy import CostObservation, SteadyWorkPolicy, WorkPolicyControl, work_policy_key
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.source_materials import digest
from test_behavior_assistant_server import AssistantHarness
from assistant_sdk_fixture import review_source
from legacy_turn_rows_helpers import StartupTableTransport, read_startup
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from hosted_versions_stub import deployed_versions
from legacy_replay_helpers import legacy_tokens, pinned_configuration, replayed


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


def test_transfer_pending_ready_and_post_turn_are_readable_at_startup(
        legacy_tokens, pinned_configuration, synthetic_standing_v1):
    """Each stage is the state the retired job lane and transfer prepare left on 80ec0865 (legacy_replay_helpers)."""
    c = synthetic_standing_v1
    # A saved first turn, then a second turn of the same conversation that failed on its agent version.
    turns = replayed(c.h, c.authority.artifacts)
    cid = turns.conversation_id
    original = read_startup(c)
    # The environment transfer whose Hosted session create was lost (CONVERSATION_TRANSFER_UNKNOWN).
    target_session_id = replayed(c.h, c.authority.artifacts)
    pending = read_startup(c)
    transfer = transfer_key(cid, turns.failed_job_id)
    assert sum(row.payload["data"].get("kind") == "conversation_environment_transfer"
               for row in pending.rows.values()) == 1
    assert not any(row.payload["data"].get("kind") == "conversation_environment_head"
                   for row in pending.rows.values())
    assert pending.rows[conversation_key(cid)] == original.rows[conversation_key(cid)]

    # The same transfer reconciled to ready.
    replayed(c.h, c.authority.artifacts)
    ready = read_startup(c)
    assert ready.rows[transfer] == pending.rows[transfer]
    assert ready.rows[head_key(cid)].payload["data"] == {
        "kind": "conversation_environment_head", "transfer_id": transfer}
    assert ready.rows[conversation_key(cid)].payload["data"]["hosted_session_id"] == target_session_id

    # A human follow-up turn saved in the transferred environment.
    third = replayed(c.h, c.authority.artifacts)
    c.control.refresh()
    assert c.control.records[third].environment_transfer_id == transfer
    after = read_startup(c)
    assert after.rows[transfer] == pending.rows[transfer]
    assert after.rows[head_key(cid)] == ready.rows[head_key(cid)]
    assert after.rows[worker_key("job", turns.failed_job_id)] == original.rows[worker_key("job", turns.failed_job_id)]
    assert after.rows[assistant_key("text-archive", turns.first_job_id)] == original.rows[
        assistant_key("text-archive", turns.first_job_id)]
    assert len(c.control.records) == 3
