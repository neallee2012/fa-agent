"""Explicit human budget-mode changes; accounting and old approvals stay intact."""
from datetime import datetime
from typing import Literal

from .behavior_operations import _Model, _row, _write, _updated
from .behavior_runtime import material
from .behavior_store import ControlChanges, ControlRequest, Digest
from .behavior_work_policy import CostLedger, work_policy_key
from .errors import Problem
from .models import Identifier
from .source_materials import digest


class BudgetEnforcementCommand(_Model):
    request_id: Identifier
    policy_sha256: Digest
    enforced: bool


class BudgetEnforcementChange(_Model):
    kind: Literal["steady_budget_enforcement_change"] = "steady_budget_enforcement_change"
    policy_id: Identifier
    command: BudgetEnforcementCommand
    previous_enforced: bool
    principal_oid: Identifier
    changed_at: datetime


def change_request(policy, command):
    key = work_policy_key("budget-enforcement", policy.authority_domain, command.request_id)
    request = ControlRequest(request_id=key, request_digest=digest(command.model_dump(mode="json")),
        read_set=(work_policy_key("policy", policy.authority_domain),
                  work_policy_key("revocation", policy.authority_domain),
                  work_policy_key("cost", policy.authority_domain), key))
    return key, request


def set_budget_enforcement(control, principal, command):
    control._human(principal, "approve")
    if command.policy_sha256 != control.policy.sha256:
        raise Problem(409, "WORK_BUDGET_POLICY_CHANGED", "the original standing policy differs")
    key, request = change_request(control.policy, command)
    def decide(rows):
        policy = control.installed(rows)
        if not policy.active or not policy.read_enabled:
            raise Problem(403, "WORK_POLICY_REVOKED", "a budget change cannot revive revoked work authority")
        ledger = _row(rows, control.cost_key, CostLedger)
        change = BudgetEnforcementChange(policy_id=policy.policy_id, command=command,
            previous_enforced=ledger.budget_enforced, principal_oid=principal.oid, changed_at=control.runtime._now())
        updated = _updated(ledger, budget_enforced=command.enforced)
        return ControlChanges(writes=(
            _write(control.cost_key, updated, "replace"),
            _write(key, change, "create", material(change.model_dump(mode="json"))),
        ), result={"budget_enforced": command.enforced, "accounting_preserved": True})
    return control.store.transact(request, decide)

