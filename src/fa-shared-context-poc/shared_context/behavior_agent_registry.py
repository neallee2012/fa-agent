"""Additive A/B scope metadata in the original authority; never assemble a runtime."""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .behavior_auth import BehaviorPrincipal
from .behavior_operations import BudgetRecord, _Model, _row, _utc, _write, control_key
from .behavior_runtime import material
from .behavior_server_config import ServerAgentScope
from .behavior_store import ControlChanges, ControlRequest, Digest, _Row
from .behavior_team_registry import resolve_agent_scope
from .behavior_work_policy import CostLedger, StandingPolicyInstallation, SteadyWorkPolicy, WorkRegistry, work_policy_key
from .errors import Problem
from .models import Identifier
from .source_materials import _payload_hash, digest


def _require(condition, message):
    if not condition:
        raise Problem(409, "AGENT_SCOPE_INVALID", message)


def registry_key(domain, agent_id):
    return work_policy_key("policy", domain, agent_id)


def _request(policy):
    return ControlRequest(
        request_id=registry_key(policy.authority_domain, policy.agent_id),
        request_digest=digest(["fa-agent-scope-registry-v1", policy.model_dump(mode="json")]),
        read_set=(
            work_policy_key("policy", policy.authority_domain),
            registry_key(policy.authority_domain, policy.agent_id),
            work_policy_key("registry", *policy.scope_ids),
            work_policy_key("cost", policy.authority_domain),
            control_key("budget", policy.template.owner.root_budget_id),
        ),
    )


class AgentScopeRegistry(_Model):
    kind: Literal["agent_scope_registry"] = "agent_scope_registry"
    schema_version: Literal["fa-agent-scope-registry-v1"] = "fa-agent-scope-registry-v1"
    registry_id: Identifier
    scopes: tuple[ServerAgentScope, ServerAgentScope]
    legacy_policy_sha256: Digest
    policy: SteadyWorkPolicy
    operator_oid: Identifier
    registered_at: datetime
    request: ControlRequest
    initialization_request: ControlRequest | None = Field(default=None, exclude_if=lambda value: value is None)

    _time = field_validator("registered_at")(_utc)

    @model_validator(mode="after")
    def exact_scope(self):
        a, b = self.scopes
        p, binding = self.policy, self.policy.template
        common = ("configuration_sha256", "authority_domain", "control_binding", "tenant_id", "team_id", "human_oid")
        if (not a.legacy_keys or b.legacy_keys
                or any(getattr(a, field) != getattr(b, field) for field in common)
                or a.identity.oid == b.identity.oid or a.identity.actor == b.identity.actor
                or p.agent_scope is None or p.agent_scope.registry_id != self.registry_id
                or p.agent_id != b.target.agent_id or binding.agent_oid != b.identity.oid
                or (binding.tenant_id, binding.team_id, binding.human_oid) != (b.tenant_id, b.team_id, b.human_oid)
                or (p.configuration_sha256, p.authority_domain, p.control_binding) != (
                    b.configuration_sha256, b.authority_domain, b.control_binding)
                or self.operator_oid != b.human_oid or self.request != _request(p)):
            raise ValueError("registry must retain the exact original authority and distinct server-resolved agent")
        return self


def _check_policy(config, legacy, policy, b):
    _require(policy.agent_scope is not None, "a new agent scope must be explicit; legacy A cannot be reinstalled")
    a = config.agent_scope(config.targets[0].agent_id)
    _require(not b.legacy_keys and b.target.agent_id == policy.agent_id and legacy.agent_scope is None
             and legacy.configuration_sha256 == config.sha256 and legacy.template == config.bindings[0],
             "the original A installation and configured peer must remain unchanged")
    shared = (
        "configuration_sha256", "evidence_mode", "authority_domain", "control_binding", "cost_scope_sha256",
        "cost_observer_oid", "cumulative_ceiling_microusd", "safety_reserve_microusd", "minimum_observed_microusd",
    )
    _require(all(getattr(policy, name) == getattr(legacy, name) for name in shared)
             and policy.template.root_request_id == legacy.template.root_request_id
             and policy.template.owner.root_budget_id == legacy.template.owner.root_budget_id,
             "both agents must use the original authority, root and cumulative cost scope")
    _require(policy.policy_id != legacy.policy_id
             and all(getattr(policy.template, name) != getattr(legacy.template, name)
                     for name in ("binding_id", "agent_oid", "event_id", "job_id", "operation_id", "attempt_id", "grant_id"))
             and all(getattr(policy.template.owner, name) != getattr(legacy.template.owner, name)
                     for name in ("owner_record_id", "conversation_id", "policy_id"))
             and policy.template.work.work_id != legacy.template.work.work_id
             and policy.profile.artifact_id != legacy.profile.artifact_id
             and policy.profile.source.source_id in policy.template.source_ids
             and policy.profile.claim_id in policy.template.claim_ids
             and policy.profile.instruction is None and policy.profile.memory_scope is None,
             "the peer requires its own policy/template/result identities, not A's work or private memory capture")
    return a, b


def _result(value):
    return {
        "registry_id": value.registry_id, "agent_id": value.policy.agent_id,
        "cost_ledger_id": work_policy_key("cost", value.policy.authority_domain),
        "runtime_activated": False, "budget_reset": False,
    }


def _check_record(value, config, legacy, peer):
    _require(value.scopes == _check_policy(config, legacy, value.policy, peer)
             and value.legacy_policy_sha256 == legacy.sha256,
             "stored registry differs from the original server-resolved scope")

def registration_request(value):
    return value.initialization_request or value.request


def _initial_receipt(store, config, value, expected):
    if value.initialization_request is not None:
        from .behavior_fresh_installation import peer_initialization_receipt
        return peer_initialization_receipt(store, config, value, expected)
    receipt = store.reconcile(value.request)
    _require(dict(receipt.payload["writes"]) == expected
             and receipt.payload["result"] == _result(value), "registry lacks its original exact write receipt")
    return receipt


def read_agent_registry(store, config, agent_id):
    key = registry_key(config.table.scope.authority_domain, agent_id)
    legacy_key = work_policy_key("policy", config.table.scope.authority_domain)
    rows = store.read_snapshot((key, legacy_key))
    if rows[key] is None:
        raise Problem(409, "AGENT_SCOPE_MISSING", "the configured peer has no registered policy")
    value = _row(rows, key, AgentScopeRegistry)
    legacy = _row(rows, legacy_key, StandingPolicyInstallation).policy
    _check_record(value, config, legacy, resolve_agent_scope(store, config, agent_id))
    _require(value.policy.agent_id == agent_id
             and rows[key].payload["material"] == material(value.model_dump(mode="json")).model_dump(mode="json"),
             "registry identity or retained bytes differ")
    index_key = work_policy_key("registry", *value.policy.scope_ids)
    initial_index = _Row(binding=store._binding, row_id=index_key,
                         data=WorkRegistry(policy_id=value.policy.policy_id).model_dump(mode="json"))
    _initial_receipt(store, config, value, {
        key: _payload_hash(rows[key].payload), index_key: _payload_hash(initial_index.model_dump(mode="json")),
    })
    return value


def resolve_agent_policy(store, config, agent_id):
    scope = resolve_agent_scope(store, config, agent_id)
    if not scope.legacy_keys:
        return read_agent_registry(store, config, agent_id).policy
    key = work_policy_key("policy", scope.authority_domain)
    policy = _row(store.read_snapshot((key,)), key, StandingPolicyInstallation).policy
    _require(policy.agent_scope is None and policy.configuration_sha256 == config.sha256
             and policy.template == config.bindings[0], "original A policy/configuration differs")
    return policy


def registered_agent_scope(store, policy):
    """Resolve a captured peer policy's ownership; callers still verify live identity/ACLs."""
    _require(policy.agent_scope is not None, "legacy A has no new agent-scope registry")
    key = registry_key(policy.authority_domain, policy.agent_id)
    value = _row(store.read_snapshot((key,)), key, AgentScopeRegistry)
    _require(value.policy == policy, "the original registered peer policy differs")
    return value.scopes[1]


def register_agent_scope(store, config, policy, principal, *, now, reconcile=False):
    """Register only metadata and an empty work index; do not create cost or native rows."""
    policy = SteadyWorkPolicy.model_validate_json(policy.model_dump_json())
    if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human" or principal.actor_id is not None
            or principal.tenant_id != config.identity.tenant_id or principal.oid != config.identity.human_oid):
        raise Problem(403, "AGENT_SCOPE_DENIED", "the original server-verified human is required")
    principal.require(config.identity.team_id, "read" if reconcile else "approve")
    _require(policy.agent_scope is not None and store._scope == config.table.scope
             and store._binding == config.workers.control_binding, "explicit peer and original authority are required")
    peer = resolve_agent_scope(store, config, policy.agent_id)
    request = _request(policy)
    rows = store.read_snapshot(request.read_set)
    key = request.request_id
    if rows[key] is not None:
        original = read_agent_registry(store, config, policy.agent_id)
        if original.policy != policy:
            raise Problem(409, "AGENT_SCOPE_CONFLICT", "the original registry ID cannot bind a different policy")
        return store.reconcile(registration_request(original))
    if reconcile:
        raise Problem(503, "AGENT_SCOPE_UNKNOWN", "original registry is not confirmed; do not change its ID and resend")

    def changes(current):
        legacy = _row(current, work_policy_key("policy", policy.authority_domain), StandingPolicyInstallation)
        scopes = _check_policy(config, legacy.policy, policy, peer)
        ledger = _row(current, work_policy_key("cost", policy.authority_domain), CostLedger)
        root = _row(current, control_key("budget", policy.template.owner.root_budget_id), BudgetRecord)
        _require(ledger.authority_domain == policy.authority_domain
                 and ledger.cost_scope_sha256 == policy.cost_scope_sha256
                 and root.root_budget_id == legacy.policy.template.owner.root_budget_id,
                 "the original shared ledger and root must already exist")
        index_key = work_policy_key("registry", *policy.scope_ids)
        _require(current[key] is None and current[index_key] is None, "a registered peer cannot reset its original work")
        value = AgentScopeRegistry(
            registry_id=policy.agent_scope.registry_id, scopes=scopes, legacy_policy_sha256=legacy.policy.sha256,
            policy=policy, operator_oid=principal.oid, registered_at=now, request=request,
        )
        return ControlChanges(writes=(
            _write(key, value, "create", material(value.model_dump(mode="json"))),
            _write(index_key, WorkRegistry(policy_id=policy.policy_id), "create"),
        ), result=_result(value))

    return store.transact(request, changes)
