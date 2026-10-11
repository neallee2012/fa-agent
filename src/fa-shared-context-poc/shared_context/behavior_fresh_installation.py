"""One approved, create-only native installation on an entirely empty Table.

The operator file is the existing bootstrap trust model, not a verified JWT.
No legacy execution, source review, retirement or readiness is manufactured.
"""

import hashlib
from datetime import datetime, timedelta
from typing import Literal

from pydantic import field_validator

from .behavior_agent_registry import AgentScopeRegistry, _request as peer_request, registry_key
from .behavior_agent_registry_operator import _peer_policy
from .behavior_code_identity import code_sha256
from .behavior_migration import MigrationStore, installation_record, migration_key
from .behavior_operations import _Model, _parse, _utc, control_key
from .behavior_runtime import _PlanIndex, material, runtime_key
from .behavior_runtime_transition import _payload_hash
from .behavior_server_config import ServerConfiguration
from .behavior_store import ControlChanges, ControlRequest, ControlWrite, Digest, _Row
from .behavior_work_policy import (
    CostLedger, CostObservation, Money, StandingPolicyInstallation, SteadyWorkPolicy, WorkRegistry, work_policy_key,
)
from .behavior_workers import Slot, _Pool, worker_key
from .errors import Problem
from .models import Identifier
from .source_materials import digest


def fresh_key(domain):
    return control_key("fresh-installation", domain)


def _require(condition, message, *, status=409):
    if not condition:
        raise Problem(status, "FRESH_INITIALIZATION_INVALID", message)


class FreshInstallationInputs(_Model):
    schema_version: Literal["fa-fresh-installation-inputs-v1"] = "fa-fresh-installation-inputs-v1"
    policy: SteadyWorkPolicy
    peer_agent_id: Identifier
    peer_registration_id: Identifier
    initial_accounted_microusd: Money
    budget_enforced: bool


class FreshInstallationPlan(_Model):
    schema_version: Literal["fa-fresh-installation-plan-v1"] = "fa-fresh-installation-plan-v1"
    request_id: Identifier
    installation_id: Identifier
    configuration_sha256: Digest
    code_sha256: Digest
    inputs: FreshInstallationInputs
    input_sha256: Digest
    peer_policy: SteadyWorkPolicy
    job_input_sha256: Digest
    empty_table_observed_at: datetime
    _time = field_validator("empty_table_observed_at")(_utc)

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))


class FreshInstallationAuthorization(_Model):
    schema_version: Literal["fa-fresh-installation-authorization-v1"] = "fa-fresh-installation-authorization-v1"
    purpose: Literal["initialize-empty-native-authority"] = "initialize-empty-native-authority"
    approval_source: Literal["operator-approved-file"] = "operator-approved-file"
    decision_id: Identifier
    authorizing_human_oid: Identifier
    configuration_sha256: Digest
    plan_sha256: Digest
    not_before: datetime
    expires_at: datetime
    _times = field_validator("not_before", "expires_at")(_utc)


class FreshInstallation(_Model):
    kind: Literal["fresh_native_installation"] = "fresh_native_installation"
    schema_version: Literal["fa-fresh-installation-v1"] = "fa-fresh-installation-v1"
    plan: FreshInstallationPlan
    authorization: FreshInstallationAuthorization
    request: ControlRequest
    applied_at: datetime
    _time = field_validator("applied_at")(_utc)


def _settings(config, inputs):
    _require(type(config) is ServerConfiguration and config.workload_version == "assistant-v1.2"
             and config.assistant is not None and config.workers.capacity == 1
             and len(config.bindings) == len(config.roots) == 1 and not config.memory_projections,
             "fresh initialization supports one native v1 assistant template/root, without channels or legacy projections")
    policy = inputs.policy
    binding = config.bindings[0]
    _require(policy.configuration_sha256 == config.sha256 and policy.evidence_mode == "native"
             and policy.authority_domain == config.table.scope.authority_domain
             and policy.control_binding == config.workers.control_binding
             and policy.agent_scope is None and policy.template == binding
             and policy.profile == config.assistant.profiles[0]
             and policy.legacy_template_policy.model_dump(mode="json") == config.policies[0].policy.model_dump(mode="json")
             and binding.owner.principal_id == binding.agent_oid == config.targets[0].agent_oid
             and policy.cost_observer_oid == config.evidence.observer_oid
             and policy.queue_seconds <= config.delivery.max_queue_seconds
             and policy.minimum_observed_microusd <= inputs.initial_accounted_microusd,
             "fresh policy, accounting floor, owner and original configured scope must match", status=403)
    _require(inputs.peer_agent_id == config.targets[1].agent_id,
             "B must be the original configured second Agent, not a newly provisioned target", status=403)
    return _peer_policy(config, policy, inputs.peer_agent_id, inputs.peer_registration_id)


def _request(plan, authorization):
    return ControlRequest(request_id=plan.request_id,
                          request_digest=digest([plan.sha256, authorization.model_dump(mode="json")]))


def _check_plan(config, plan):
    _require(plan.configuration_sha256 == config.sha256
             and plan.input_sha256 == digest(plan.inputs.model_dump(mode="json"))
             and plan.peer_policy == _settings(config, plan.inputs),
             "retain the exact configuration, inputs and derived B policy", status=403)


def _check_authorization(config, plan, authorization):
    _require(authorization.authorizing_human_oid == config.identity.human_oid
             and authorization.configuration_sha256 == config.sha256
             and authorization.plan_sha256 == plan.sha256
             and plan.empty_table_observed_at <= authorization.not_before < authorization.expires_at
             and authorization.expires_at - authorization.not_before <= timedelta(minutes=15),
             "one exact configured operator-file approval and a finite installation window are required", status=403)


def _check_record(config, record):
    _check_plan(config, record.plan)
    _check_authorization(config, record.plan, record.authorization)
    _require(record.request == _request(record.plan, record.authorization)
             and record.authorization.not_before <= record.applied_at < record.authorization.expires_at,
             "the original installation must retain its approved request and commit time")


def initial_accounting(record):
    """Project the approved opening balance, not a provider billing observation."""
    plan, inputs = record.plan, record.plan.inputs
    policy = inputs.policy
    observation = CostObservation(
        evidence_id=control_key("fresh-accounting", plan.installation_id), evidence_mode="native",
        observer_oid=policy.cost_observer_oid, authority_domain=policy.authority_domain,
        control_binding=policy.control_binding, cost_scope_sha256=policy.cost_scope_sha256,
        observed_cumulative_microusd=inputs.initial_accounted_microusd,
        legacy_unresolved_reserve_microusd=0, legacy_liability_sha256=digest({}),
        evidence_sha256=digest(["empty-table-opening-balance", plan.sha256]),
        observed_at=record.applied_at,
    )
    installation = StandingPolicyInstallation(
        policy=policy, operator_oid=record.authorization.authorizing_human_oid,
        decision_id=record.authorization.decision_id, approved_at=record.applied_at,
        change_expires_at=record.authorization.expires_at, initial_observation=observation,
    )
    ledger = CostLedger(
        authority_domain=policy.authority_domain, cost_scope_sha256=policy.cost_scope_sha256,
        observed_high_water_microusd=inputs.initial_accounted_microusd,
        legacy_unresolved_reserve_microusd=0, legacy_liability_sha256=digest({}),
        latest_observed_at=record.applied_at, latest_observation_sha256=digest(observation.model_dump(mode="json")),
        budget_enforced=inputs.budget_enforced,
    )
    return installation, ledger


def _result(record):
    return {
        "state": "initialized", "installation_id": record.plan.installation_id,
        "request_id": record.request.request_id, "configuration_sha256": record.plan.configuration_sha256,
        "policy_sha256": record.plan.inputs.policy.sha256, "peer_agent_id": record.plan.inputs.peer_agent_id,
        "peer_policy_sha256": record.plan.peer_policy.sha256, "native_runtime_prepared": False,
        "legacy_readiness_published": False, "budget_reset": False,
    }


def _layout(config, record):
    from .behavior_server import bootstrap_records

    policy, peer = record.plan.inputs.policy, record.plan.peer_policy
    installation, ledger = initial_accounting(record)
    rows = [(key, value.model_dump(mode="json"), None) for key, value in bootstrap_records(config)]
    rows.append((*installation_record(config), None))
    for binding in config.bindings:
        runtime_policy = next(item.policy for item in config.policies if item.binding_id == binding.binding_id)
        rows.extend((
            (runtime_key("policy", binding.binding_id), runtime_policy.model_dump(mode="json"), None),
            (runtime_key("plan-index", binding.binding_id), _PlanIndex().model_dump(mode="json"), None),
        ))
    pool = _Pool(
        configuration_digest=digest({"contract": config.workers.model_dump(mode="json"), "mode": "native"}),
        workspace_id=config.workers.workspace_id, slots=(Slot(number=0),),
    )
    registry = AgentScopeRegistry(
        registry_id=peer.agent_scope.registry_id,
        scopes=(config.agent_scope(config.targets[0].agent_id), config.agent_scope(peer.agent_id)),
        legacy_policy_sha256=policy.sha256, policy=peer,
        operator_oid=record.authorization.authorizing_human_oid, registered_at=record.applied_at,
        request=peer_request(peer), initialization_request=record.request,
    )
    for key, value, retained in (
        (worker_key("pool", config.workers.workspace_id), pool, False),
        (work_policy_key("policy", policy.authority_domain), installation, True),
        (work_policy_key("cost", policy.authority_domain), ledger, False),
        (work_policy_key("registry", *policy.scope_ids), WorkRegistry(policy_id=policy.policy_id), False),
        (registry_key(policy.authority_domain, peer.agent_id), registry, True),
        (work_policy_key("registry", *peer.scope_ids), WorkRegistry(policy_id=peer.policy_id), False),
        (fresh_key(policy.authority_domain), record, False),
    ):
        data = value.model_dump(mode="json")
        rows.append((key, data, material(data) if retained else None))
    return ControlChanges(
        writes=tuple(ControlWrite(row_id=key, mode="create", data=data, material=retained)
                     for key, data, retained in rows),
        result=_result(record),
    )


def _fingerprints(config, record):
    return {
        write.row_id: _payload_hash(_Row(
            binding=config.workers.control_binding, row_id=write.row_id, data=write.data,
            material=write.material,
        ).model_dump(mode="json"))
        for write in _layout(config, record).writes
    }


def _verify_receipt(config, record, receipt):
    _check_record(config, record)
    _require(receipt.payload["request"] == record.request.model_dump(mode="json")
             and receipt.payload["guard_revision"] == 1
             and dict(receipt.payload["writes"]) == _fingerprints(config, record)
             and receipt.payload["result"] == _result(record),
             "the fresh installation is not one original complete create-only transaction")


def _read_record(store, config):
    key = fresh_key(config.table.scope.authority_domain)
    row = store.read_record(key)
    _require(row is not None, "the original fresh installation record is missing")
    return _parse(row.payload["data"], FreshInstallation)


def peer_initialization_receipt(store, config, registry, expected):
    record = _read_record(store, config)
    receipt = store.reconcile(record.request)
    _verify_receipt(config, record, receipt)
    _require(record.request == registry.initialization_request
             and record.plan.peer_policy == registry.policy
             and all(dict(receipt.payload["writes"]).get(key) == value for key, value in expected.items()),
             "B does not belong to this original atomic fresh installation")
    return receipt


def validate_fresh_inventory(inventory, config):
    key = fresh_key(config.table.scope.authority_domain)
    _require(key in inventory.rows, "the original fresh installation is missing from inventory")
    record = _parse(inventory.rows[key].payload["data"], FreshInstallation)
    receipt = inventory.receipts.get(record.request.request_id)
    _require(receipt is not None, "the original fresh installation receipt is missing")
    _verify_receipt(config, record, receipt)
    return record


def installed_fresh_origin(store, config):
    record = _read_record(store, config)
    receipt = store.reconcile(record.request)
    _verify_receipt(config, record, receipt)
    domain = config.table.scope.authority_domain
    immutable = {
        installation_record(config)[0], fresh_key(domain), work_policy_key("policy", domain),
        registry_key(domain, record.plan.peer_policy.agent_id),
        *(runtime_key("policy", binding.binding_id) for binding in config.bindings),
    }
    absent = (
        work_policy_key("current", domain), migration_key(domain, "current"),
        control_key("runtime-transition-current", domain), control_key("runtime-rotation-current", domain),
    )
    rows = store.read_snapshot((*sorted(immutable), *absent, work_policy_key("cost", domain)))
    expected = dict(receipt.payload["writes"])
    _require(all(rows[key] is not None and _payload_hash(rows[key].payload) == expected[key] for key in immutable)
             and all(rows[key] is None for key in absent),
             "fresh immutable settings changed or were mixed with a legacy migration")
    ledger_row = rows[work_policy_key("cost", domain)]
    _require(ledger_row is not None, "the one original accounting ledger is missing")
    ledger = _parse(ledger_row.payload["data"], CostLedger)
    _require(ledger.authority_domain == domain and ledger.cost_scope_sha256 == record.plan.inputs.policy.cost_scope_sha256
             and ledger.legacy_liability_sha256 == digest({})
             and ledger.observed_high_water_microusd >= record.plan.inputs.initial_accounted_microusd,
             "the original accounting scope or opening balance regressed")
    store.bind_installation(installation_record(config)[0], config.sha256, protected_rows=(*immutable, *absent))
    store._verified_fresh_initialization = config.sha256
    return config


class FreshInitializer:
    def __init__(self, composition):
        self.composition = composition
        self.config = composition.config
        self.clock = composition.runtime.clock
        self.store = MigrationStore(composition.clients[0], self.config.table.scope)

    def plan(self, inputs, *, request_id, installation_id):
        inputs = FreshInstallationInputs.model_validate_json(inputs.model_dump_json())
        peer = _settings(self.config, inputs)
        self.store.require_empty_table()
        return FreshInstallationPlan(
            request_id=request_id, installation_id=installation_id, configuration_sha256=self.config.sha256,
            code_sha256=code_sha256(), inputs=inputs, input_sha256=digest(inputs.model_dump(mode="json")),
            peer_policy=peer, job_input_sha256=hashlib.sha256(
                self.composition.runtime.job_input(self.config.bindings[0])).hexdigest(),
            empty_table_observed_at=self.clock(),
        )

    def execute(self, plan, authorization, *, reconcile=False):
        plan = FreshInstallationPlan.model_validate_json(plan.model_dump_json())
        authorization = FreshInstallationAuthorization.model_validate_json(authorization.model_dump_json())
        _check_plan(self.config, plan)
        _check_authorization(self.config, plan, authorization)
        request = _request(plan, authorization)
        prior = self.store._receipt(request)
        if prior is not None:
            record = _read_record(self.store, self.config)
            _require(record.plan == plan and record.authorization == authorization,
                     "the original request belongs to a different fresh initialization")
            _verify_receipt(self.config, record, prior)
            return prior
        if reconcile:
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "retain the original plan, approval and request ID",
                          request_id=request.request_id, request_digest=request.request_digest)

        def current():
            now = _utc(self.clock())
            _require(plan.code_sha256 == code_sha256()
                     and plan.job_input_sha256 == hashlib.sha256(
                         self.composition.runtime.job_input(self.config.bindings[0])).hexdigest(),
                     "the exact planned build or original template input changed", status=403)
            _require(authorization.not_before <= now < authorization.expires_at,
                     "the fresh installation approval is not currently valid", status=403)

        current()
        record = FreshInstallation(plan=plan, authorization=authorization, request=request, applied_at=self.clock())
        return self.store.initialize_empty(request, _layout(self.config, record), before_send=current)
