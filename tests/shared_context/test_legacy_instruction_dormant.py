"""Unfinished old instruction work stays as stored: the served API neither resumes nor observes it (2026-11-17).

The legacy job lane is retired, so a restart or a read of an old conversation must not send its work back to
Hosted. Offline: a fresh installation under the real server factory with its startup lifespan; the old work is
admitted in-process by the retiring WorkPolicyControl.submit (its route already answers 410).
"""
from dataclasses import replace

from fastapi.testclient import TestClient

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_operations import _row
from shared_context.behavior_server import application
from shared_context.behavior_work_policy import CostLedger, CostObservation
from instruction_flow_helpers import instruction_models
from test_fresh_installation import fresh_case, initialize, writes
from test_m3_auth import jwk_client, signing_key


def old_work(h, monkeypatch, *, started=False):
    """The served app over one unfinished old instruction job and every Hosted start its job sends.

    The server has no instruction observer to launch any more (2026-11-17); a start would show up in `starts`."""
    initialize(h)
    starts = []
    app = application(h.composition)
    control = h.runtime.work_policy
    ledger = _row(h.runtime.store.read_snapshot((control.cost_key,)), control.cost_key, CostLedger)
    policy = control.policy
    observation = CostObservation(
        evidence_id="old-work-cost", evidence_mode=policy.evidence_mode, observer_oid=policy.cost_observer_oid,
        authority_domain=policy.authority_domain, control_binding=policy.control_binding,
        cost_scope_sha256=policy.cost_scope_sha256, observed_cumulative_microusd=ledger.observed_high_water_microusd,
        legacy_unresolved_reserve_microusd=ledger.legacy_unresolved_reserve_microusd,
        legacy_liability_sha256=ledger.legacy_liability_sha256, evidence_sha256="e" * 64, observed_at=h.time)
    control.cost_reader = lambda: observation
    identity = h.config.identity
    human = BehaviorPrincipal(identity.tenant_id, identity.human_oid, identity.team_id, "human", None,
                              frozenset({"read", "submit"}))
    binding, _ = control.submit(human, InstructionSubmit(request_id="old-work", content="Old work."))
    h.runtime.dep.workers.adapter = replace(h.runtime.dep.workers.adapter, start=starts.append)
    if started:
        h.runtime.dep.workers.start(binding.job_id, binding.owner)
        starts.clear()
    return app, binding, starts


def unchanged(h):
    return h.runtime.store._guard(), writes(h)


def test_startup_does_not_resume_unfinished_old_instruction_work(fresh_case, monkeypatch):
    h = fresh_case
    app, binding, starts = old_work(h, monkeypatch)
    assert h.runtime.dep.workers.inspect(binding.job_id).state == "queued"
    before = unchanged(h)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert starts == []
    assert unchanged(h) == before and h.calls == []
    assert h.runtime.dep.workers.inspect(binding.job_id).state == "queued"


def test_reading_an_old_conversation_does_not_observe_its_unfinished_work(fresh_case, signing_key, monkeypatch):
    h = fresh_case
    app, binding, starts = old_work(h, monkeypatch, started=True)
    job = h.runtime.dep.workers.inspect(binding.job_id)
    assert job.state == "starting"
    cid = h.runtime.work_policy.records[binding.binding_id].profile.instruction.conversation_id
    with TestClient(app) as client:
        before = unchanged(h)
        response = client.get("/work-policy/v1/conversations/" + cid, headers=h.headers(signing_key))
        assert response.status_code == 200, response.text
        assert [turn["state"] for turn in response.json()["turns"]] == ["starting"]
        assert starts == []
        assert unchanged(h) == before
    assert h.calls == [] and h.runtime.dep.workers.inspect(binding.job_id) == job
