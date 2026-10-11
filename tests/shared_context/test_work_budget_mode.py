"""Budget enforcement changes preserve accounting and require the original human."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared_context.behavior_operations import _row
from shared_context.behavior_work_policy import CostLedger, cumulative_decision
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_m3_auth import jwk_client, signing_key
from test_work_policy_lifecycle import enabled


@pytest.fixture
def runtime_path():
    return Path(__file__)  # These authority tests never start an SDK process.


def test_disable_budget_is_authorized_idempotent_and_preserves_every_counter(assistant):
    h, control, human, facts = enabled(assistant)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier))
    before = _row(h.store.read_snapshot((control.cost_key,)), control.cost_key, CostLedger)
    command = {"request_id": "explicit-accounting-only", "policy_sha256": control.policy.sha256,
               "enforced": False}
    path = "/operator/v1/standing-work-policy/budget-enforcement"
    denied = h.client.post(path, json=command, headers=h.headers("agent"))
    assert denied.status_code == 403
    accepted = h.client.post(path, json=command, headers=h.headers())
    assert accepted.status_code == 200, accepted.text
    after = _row(h.store.read_snapshot((control.cost_key,)), control.cost_key, CostLedger)
    assert after.budget_enforced is False
    assert after.model_copy(update={"budget_enforced": True}) == before
    assert "budget_enforced" not in before.model_dump(mode="json")
    guard = h.store._guard()
    assert h.client.post(path, json=command, headers=h.headers()).status_code == 200
    assert h.store._guard() == guard
    changed = h.client.post(path, json={**command, "enforced": True}, headers=h.headers())
    assert changed.status_code == 409
    assert h.store._guard() == guard


def test_accounting_only_does_not_block_on_ceiling_or_staleness_but_keeps_revocation(assistant):
    from shared_context.behavior_work_budget import BudgetEnforcementCommand, set_budget_enforcement
    h, control, human, facts = enabled(assistant)
    set_budget_enforcement(control, human, BudgetEnforcementCommand(
        request_id="disable-before-cost-growth", policy_sha256=control.policy.sha256, enforced=False))
    control.observe_cost(facts[0].model_copy(update={"observed_cumulative_microusd": 210_000_000}))
    ledger = _row(h.store.read_snapshot((control.cost_key,)), control.cost_key, CostLedger)
    observation = facts[0].model_copy(update={"observed_cumulative_microusd": 210_000_000})
    decision = cumulative_decision(control.policy, ledger, observation, h.time + timedelta(days=1),
                                   read_id="past-old-ceiling", mode=h.runtime.mode)
    assert decision.decision == "allow"
    assert decision.budget_enforced is False
    assert decision.remaining_microusd is None and decision.cumulative_ceiling_microusd is None
    assert decision.observed_high_water_microusd == 210_000_000
    revoked = cumulative_decision(control.policy.model_copy(update={"active": False}), ledger,
                                 observation, h.time, read_id="revoked", mode=h.runtime.mode)
    assert revoked.decision == "deny" and revoked.reason == "policy_revoked"
    with pytest.raises(Problem) as denied:
        set_budget_enforcement(control, replace(human, actions=frozenset({"read"})),
                               BudgetEnforcementCommand(request_id="not-approved",
                                   policy_sha256=control.policy.sha256, enforced=True))
    assert denied.value.body["error"]["code"]
