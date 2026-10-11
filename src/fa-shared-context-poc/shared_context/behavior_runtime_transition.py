"""One drained, same-authority transition from checksum to the assistant profile.

The operator supplies exact configurations, fresh protection observations and
retirement evidence. No bootstrap, model call, target launch or budget refill
exists here. Immutable pre/post images retain the old configuration lineage;
ordinary runtime writes can never modify the transition history.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from types import MappingProxyType, SimpleNamespace
from typing import Annotated, Literal

from azure.core.exceptions import ResourceNotFoundError
from pydantic import Field, field_validator

from . import behavior_migration as migration
from . import behavior_delivery as delivery, behavior_operations as operations, behavior_workers as workers
from .behavior_foundry import FoundryTarget, NativeExecutionGrant, _TargetReservation, native_key
from .behavior_auth import BehaviorPrincipal
from .behavior_runtime import RuntimePolicy, runtime_key
from .behavior_server_config import ProtectionObservation, ServerConfiguration
from .behavior_store import GUARD_KEY, ControlChanges, ControlRequest, ControlWrite, Digest, _Row
from .errors import Problem
from .models import Identifier
from .records import Record
from .source_materials import InventoryProtectionVerifier, canonical, digest, utc


def transition_key(kind, *ids):
    return operations.control_key("runtime-transition-" + kind, *ids)


def _deny(code, message):
    return Problem(409, code, message)


def _payload_hash(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class RetiredTarget(migration._Model):
    target: FoundryTarget
    provider_session_status: Literal["idle"]
    provider_observation_sha256: Digest


class RuntimeRetirement(migration._Model):
    schema_version: Literal["fa-runtime-retirement-v1"] = "fa-runtime-retirement-v1"
    operator_oid: Identifier
    authority_domain: Identifier
    old_configuration_sha256: Digest
    new_configuration_sha256: Digest
    old_code_sha256: Digest
    controller_resource_id: str
    api_resource_id: str
    no_active_controller_revision: Literal[True]
    no_running_controller_jobs: Literal[True]
    old_business_admission_disabled: Literal[True]
    compute_observation_sha256: Digest
    targets: tuple[RetiredTarget, ...]
    observed_at: datetime
    valid_until: datetime

    _times = field_validator("observed_at", "valid_until")(utc)


class RuntimeTransitionPlan(migration._Model):
    schema_version: Literal["fa-runtime-transition-plan-v1"] = "fa-runtime-transition-plan-v1"
    transition_id: Identifier
    old_configuration_sha256: Digest
    new_configuration_sha256: Digest
    authority_domain: Identifier
    control_binding: Digest
    code_sha256: Digest
    inventory_sha256: Digest
    untouched_sha256: Digest
    counter_sha256: Digest
    previous_history_key: Identifier
    previous_history_sha256: Digest
    observation_sha256: dict[str, Digest]
    retirement_sha256: Digest
    changes_sha256: Digest
    guard: migration.GuardAnchor
    valid_until: datetime

    _times = field_validator("valid_until")(utc)

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))


class RuntimeTransitionAuthorization(migration._Model):
    schema_version: Literal["fa-runtime-transition-authorization-v1"] = "fa-runtime-transition-authorization-v1"
    decision_id: Identifier
    operator_oid: Identifier
    plan_sha256: Digest
    not_before: datetime
    expires_at: datetime
    action: Literal["activate-drained-assistant-profile"] = "activate-drained-assistant-profile"

    _times = field_validator("not_before", "expires_at")(utc)


class _Image(migration._Model):
    row_id: Identifier
    before_payload: str | None
    after_payload: str
    before_etag: str | None

    @property
    def before(self):
        return None if self.before_payload is None else json.loads(self.before_payload)

    @property
    def after(self):
        return json.loads(self.after_payload)


class RuntimeTransitionHistory(migration._Model):
    schema_version: Literal["fa-runtime-transition-history-v1"] = "fa-runtime-transition-history-v1"
    plan: RuntimeTransitionPlan
    authorization: RuntimeTransitionAuthorization
    previous_configuration: dict
    before_guard: dict
    request: ControlRequest
    images: Annotated[tuple[_Image, ...], Field(max_length=80)]
    applied_at: datetime

    _time = field_validator("applied_at")(utc)


class RuntimeTransitionCurrent(migration._Model):
    schema_version: Literal["fa-runtime-transition-current-v1"] = "fa-runtime-transition-current-v1"
    configuration_sha256: Digest
    history_key: Identifier
    history_sha256: Digest
    authorization_key: Identifier


class _Entity(dict):
    def __init__(self, value, etag):
        super().__init__(value)
        self.metadata = {"etag": etag}


class _SnapshotClient:
    """Read-only replay of receipt-verified preimages, never a native witness."""
    def __init__(self, store, inventory, *, guard_epoch):
        self.url, self.table_name = store._client.url, store._scope.table_name
        self.scope, self.inventory, self.epoch = store._scope, inventory, guard_epoch

    def get_entity(self, *, partition_key, row_key, raw_request_hook, **_):
        if partition_key != self.scope.partition_key:
            raise _deny("TRANSITION_HISTORY_INVALID", "historical replay escaped its authority")
        raw_request_hook(SimpleNamespace(http_request=SimpleNamespace(method="GET", url=self.url, body=None)))
        if row_key == GUARD_KEY:
            payload = {**self.inventory.guard.payload, "schema_version": 2, "configuration_sha256": self.epoch}
            etag = self.inventory.guard.etag
        else:
            records = self.inventory.receipts if row_key.startswith("behavior:v1:receipt:") else self.inventory.rows
            prefix = "behavior:v1:receipt:" if row_key.startswith("behavior:v1:receipt:") else "behavior:v1:row:"
            record = records.get(row_key.removeprefix(prefix))
            if record is None:
                error = ResourceNotFoundError("historical record absent")
                error.error_code = "ResourceNotFound"
                raise error
            payload, etag = record.payload, record.etag
        return _Entity({"PartitionKey": partition_key, "RowKey": row_key,
                        "Payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()}, etag)

    def submit_transaction(self, *_args, **_kwargs):
        raise _deny("TRANSITION_HISTORY_READ_ONLY", "historical replay cannot write")


def _snapshot_store(store, inventory, config):
    return migration.MigrationStore(_SnapshotClient(store, inventory, guard_epoch=config.sha256), store._scope)


def _retain_capture_history(rows, receipts, config, original, pointer):
    """Validate setup-only records, then exclude them only from business quiescence."""
    from .teams_personal import _RouteWitness
    from .teams_capture_server import _RouteBinding

    for key, row in tuple(rows.items()):
        if row.payload["data"].get("schema_version") != "fa-teams-personal-route-v1":
            continue
        witness = RuntimeTransition._parse(row.payload["data"], _RouteWitness)
        route = witness.route
        personal = witness.configuration_digest
        route_key = operations.control_key("teams-personal-route", personal)
        binding_key = operations.control_key("teams-capture-route-binding", personal)
        binding_row = rows.get(binding_key)
        if binding_row is None:
            raise _deny("TRANSITION_ROUTE_HISTORY_INVALID", "capture witness lacks its atomic installation binding")
        binding = RuntimeTransition._parse(binding_row.payload["data"], _RouteBinding)
        expected = _RouteBinding(
            server_configuration_sha256=config.sha256, personal_configuration_sha256=personal,
            original_configuration_sha256=original.sha256, control_binding=config.workers.control_binding,
            installation_row_id=migration.installation_record(config)[0],
            migration_current_row_id=migration.migration_key(config.table.scope.authority_domain, "current"),
            history_row_id=pointer.history_key, history_sha256=pointer.history_sha256,
            authorization_row_id=pointer.authorization_key, route_row_id=key,
            route_material_sha256=delivery._material(witness.model_dump(mode="json")).sha256,
        )
        request = ControlRequest(
            request_id=operations.control_key("teams-personal-enroll", personal),
            request_digest=digest(witness.model_dump(mode="json")), read_set=(route_key,),
        )
        receipt = receipts.get(request.request_id)
        if (key != route_key or binding != expected or witness.evidence_mode != "connector_http"
                or route.tenant_id != config.identity.tenant_id or route.aad_object_id != config.identity.human_oid
                or route.bot_app_id != config.identity.managed_identity_client_id
                or route.bot_id != "28:" + route.bot_app_id or route.conversation_type != "personal"
                or route.thread_id is not None or receipt is None
                or receipt.payload["request"] != request.model_dump(mode="json")
                or dict(receipt.payload["writes"]) != {
                    key: _payload_hash(row.payload), binding_key: _payload_hash(binding_row.payload),
                }
                or row.payload["material"] != delivery._material(witness.model_dump(mode="json")).model_dump(mode="json")
                or binding_row.payload["material"] != delivery._material(binding.model_dump(mode="json")).model_dump(mode="json")
                or receipt.payload["result"] != {
                    "status": "route_recorded_not_admitted", "route_id": route.route_id,
                    "evidence_mode": "connector_http", "work_admitted": False,
                    "native_gate": "closed", "actual_environment": "NOT_RUN",
                }):
            raise _deny("TRANSITION_ROUTE_HISTORY_INVALID", "capture history has a different identity, material or receipt")
        rows.pop(key)
        rows.pop(binding_key)
        receipts.pop(request.request_id)


def _without_pin_migration(store, inventory, config):
    view = _snapshot_store(store, inventory, config)
    original = migration.installed_origin(view, config)
    if original.sha256 == config.sha256:
        raise _deny("TRANSITION_PROTECTION_MIGRATION_REQUIRED", "verified protection migration is required first")
    key = migration.migration_key(config.table.scope.authority_domain, "current")
    pointer = RuntimeTransition._parse(inventory.rows[key].payload["data"], migration.MigrationCurrent)
    history = RuntimeTransition._parse(inventory.rows[pointer.history_key].payload["data"], migration.MigrationHistory)
    rows, receipts = dict(inventory.rows), dict(inventory.receipts)
    _retain_capture_history(rows, receipts, config, original, pointer)
    for removed in (key, pointer.history_key, pointer.authorization_key):
        rows.pop(removed)
    receipts.pop(history.request.request_id)
    for row_id, data in (
        migration.installation_record(original),
        (migration.protection_keys(original)[0], original.sources.protection.model_dump(mode="json")),
        (migration.protection_keys(original)[1], original.backups.protection.model_dump(mode="json")),
    ):
        payload = _Row(binding=store._binding, row_id=row_id, data=data).model_dump(mode="json")
        rows[row_id] = Record(payload, rows[row_id].etag)
    hashes = {**{"behavior:v1:row:" + key: _payload_hash(row.payload) for key, row in rows.items()},
              **{"behavior:v1:receipt:" + key: _payload_hash(row.payload) for key, row in receipts.items()},
              GUARD_KEY: inventory.entity_sha256[GUARD_KEY]}
    # Match the verified predecessor images; the live installation/guard is not changed.
    selected = migration.AuthorityInventory(
        inventory.scope, inventory.guard, MappingProxyType(rows), MappingProxyType(receipts),
        MappingProxyType(hashes), original.sha256,
    )
    migration._quiescence(selected, original)
    return original, pointer


def _counter_hash(rows):
    values = {}
    for key, row in rows.items():
        data = row.payload["data"]
        if data.get("kind") in {"operation_budget", "delivery_quota", "worker_pool", "native_target_reservation"}:
            kept = copy.deepcopy(data)
            kept.pop("configuration_digest", None)
            if kept.get("kind") == "native_target_reservation":
                kept.pop("target", None)
            values[key] = kept
    return digest(values)


def _config_change(old, new):
    if (old.workload_version != "checksum-count-v1" or new.workload_version != "assistant-v1.2"
            or old.workers.capacity != new.workers.capacity or old.workers.capacity != 1):
        raise _deny("TRANSITION_CONFIG_CHANGE_DENIED", "only a drained C1 checksum-to-assistant transition is supported")
    if (old.evidence.observer_oid != new.evidence.observer_oid
            or old.evidence.inventory_scopes != new.evidence.inventory_scopes
            or old.evidence.max_age_seconds != new.evidence.max_age_seconds):
        raise _deny("TRANSITION_CONFIG_CHANGE_DENIED", "observer, retention coverage and evidence freshness cannot be changed")
    before, after = old.model_dump(mode="json"), new.model_dump(mode="json")
    for field in ("deployment_id", "workload_version", "assistant", "bindings", "policies", "operations", "evidence"):
        before.pop(field, None)
        after.pop(field, None)
    for field in ("executor_version", "protection_version"):
        before["workers"].pop(field)
        after["workers"].pop(field)
    for field in ("protection_version", "verifier_version"):
        before["delivery"].pop(field)
        after["delivery"].pop(field)
    for data in (before, after):
        data["targets"][0].pop("agent_version")
        data["targets"][0].pop("session_id")
    if before != after:
        raise _deny("TRANSITION_CONFIG_CHANGE_DENIED", "authority, budgets, retention, pool or identity changes are prohibited")
    if old.targets[0].session_id == new.targets[0].session_id:
        raise _deny("TRANSITION_CONFIG_CHANGE_DENIED", "a separately owned version-pinned assistant session is required")
    for field in ("binding_id", "job_id", "event_id", "operation_id", "attempt_id", "grant_id"):
        if {getattr(binding, field) for binding in old.bindings} & {getattr(binding, field) for binding in new.bindings}:
            raise _deny("TRANSITION_CONFIG_CHANGE_DENIED", "new profile must not reuse old work or operation identities")
    for field in ("owner_record_id", "policy_id", "conversation_id"):
        if {getattr(binding.owner, field) for binding in old.bindings} & {
            getattr(binding.owner, field) for binding in new.bindings
        }:
            raise _deny("TRANSITION_CONFIG_CHANGE_DENIED", "old owner, policy and conversation records must be retained")
    if {binding.work.work_id for binding in old.bindings} & {binding.work.work_id for binding in new.bindings}:
        raise _deny("TRANSITION_CONFIG_CHANGE_DENIED", "old work results cannot be reused")


class RuntimeTransition:
    def __init__(self, store, old, new, observations, *, retirement, clock, native_grants=()):
        _config_change(old, new)
        old = ServerConfiguration.model_validate_json(old.model_dump_json())
        new = ServerConfiguration.model_validate_json(new.model_dump_json())
        if not isinstance(store, migration.MigrationStore) or store._installation_fence is not None:
            raise _deny("TRANSITION_OPERATOR_REQUIRED", "an explicitly isolated operator store is required")
        if store._scope != old.table.scope or store._binding != old.workers.control_binding:
            raise _deny("TRANSITION_CONFIG_CHANGE_DENIED", "the actual authority differs")
        self.store, self.old, self.new, self.clock = store, old, new, clock
        self.observations = {lane: ProtectionObservation.model_validate_json(observations[lane])
                             for lane in ("sources", "backups")}
        self.observation_hashes = {lane: hashlib.sha256(observations[lane]).hexdigest()
                                   for lane in self.observations}
        self.retirement = RuntimeRetirement.model_validate_json(retirement)
        self.retirement_sha = hashlib.sha256(retirement).hexdigest()
        self.grants = tuple(NativeExecutionGrant.model_validate_json(grant.model_dump_json()) for grant in native_grants)

    @staticmethod
    def _parse(data, model):
        try:
            return model.model_validate_json(canonical(data))
        except (ValueError, TypeError):
            raise _deny("TRANSITION_HISTORY_INVALID", "retained transition schema differs") from None

    def _operator(self, principal):
        if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human" or principal.actor_id is not None
                or principal.oid != self.old.identity.human_oid or principal.tenant_id != self.old.identity.tenant_id):
            raise Problem(403, "TRANSITION_OPERATOR_REQUIRED", "configured delegated operator required")
        principal.require(self.old.identity.team_id, "read")
        principal.require(self.old.identity.team_id, "approve")

    def _fresh(self):
        now, retired = utc(self.clock()), self.retirement
        if (retired.operator_oid != self.old.identity.human_oid
                or retired.authority_domain != self.old.table.scope.authority_domain
                or retired.old_configuration_sha256 != self.old.sha256
                or retired.new_configuration_sha256 != self.new.sha256
                or retired.controller_resource_id != self.old.controller_resource_id
                or retired.api_resource_id != self.old.api_resource_id
                or not retired.observed_at <= now < retired.valid_until
                or retired.valid_until - retired.observed_at > timedelta(minutes=20)
                or tuple(item.target for item in retired.targets) != self.old.active_targets):
            raise _deny("TRANSITION_RETIREMENT_REQUIRED", "fresh exact controller retirement and target observations required")
        deadlines = [retired.valid_until]
        for lane, observation in self.observations.items():
            expected = getattr(self.new, lane)
            if (observation.configuration != expected.protection
                    or observation.inventory_coverage is None
                    or set(observation.inventory_coverage) != set(self.new.evidence.inventory_scopes)
                    or len(observation.inventory_coverage) != len(self.new.evidence.inventory_scopes)
                    or observation.inventory_observed_at is None or observation.inventory_valid_until is None
                    or observation.blob_service.account_resource_id != expected.account_resource_id
                    or observation.management_policy.account_resource_id != expected.account_resource_id
                    or not observation.inventory_observed_at <= now < observation.inventory_valid_until):
                raise _deny("TRANSITION_PROTECTION_REQUIRED", "exact current protection inventory required")
            InventoryProtectionVerifier(observation.state, clock=self.clock).verify(expected.protection)
            for instant in (observation.observed_at, observation.inventory_observed_at,
                            observation.blob_service.observed_at, observation.management_policy.observed_at):
                if now - instant > timedelta(seconds=self.new.evidence.max_age_seconds) or instant > now:
                    raise _deny("TRANSITION_PROTECTION_REQUIRED", "protection observation is stale")
            deadlines.extend((observation.valid_until, observation.inventory_valid_until,
                              observation.blob_service.valid_until, observation.management_policy.valid_until))
        for policy in self.new.policies:
            if not now < policy.policy.valid_until <= now + timedelta(hours=4):
                raise _deny("TRANSITION_POLICY_REQUIRED", "new profile requires a separately authorized finite policy window")
            deadlines.append(policy.policy.valid_until)
        return min(deadlines)

    def _changes(self, inventory):
        new = self.new
        worker_digest = digest({"contract": new.workers.model_dump(mode="json"), "mode": "native"})
        delivery_digest = digest({"contract": new.delivery.model_dump(mode="json"), "mode": "native"})
        changes = {}

        def changed(key, data):
            before = inventory.rows.get(key)
            if before is not None and before.payload["material"] is not None:
                raise _deny("TRANSITION_MATERIAL_MUTATION_DENIED", "retained materials cannot be rewritten")
            after = _Row(binding=self.store._binding, row_id=key, data=data).model_dump(mode="json")
            changes[key] = _Image(
                row_id=key, before_payload=None if before is None else json.dumps(
                    before.payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                before_etag=None if before is None else before.etag,
                after_payload=json.dumps(after, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            )

        changed(*migration.installation_record(new))
        pool_key = workers.worker_key("pool", new.workers.workspace_id)
        changed(pool_key, {**inventory.rows[pool_key].payload["data"], "configuration_digest": worker_digest})
        for key, row in inventory.rows.items():
            data = row.payload["data"]
            if data.get("kind") in {"delivery_quota", "delivery_pending_index"}:
                if data.get("pending") or data.get("entries"):
                    raise _deny("TRANSITION_NOT_DRAINED", "delivery quota or index is still occupied")
                changed(key, {**data, "configuration_digest": delivery_digest})
            elif data.get("kind") == "native_target_reservation":
                reservation = self._parse(data, _TargetReservation)
                target = next((item for item in new.active_targets if item.executor_id == reservation.target.executor_id), None)
                if reservation.active_launch_id is not None or reservation.target not in self.old.active_targets or target is None:
                    raise _deny("TRANSITION_NOT_DRAINED", "old target is active or unrecognized")
                changed(key, {**data, "configuration_digest": worker_digest, "target": target.model_dump(mode="json")})
        for binding in new.bindings:
            rows = (
                (operations.control_key("owner", binding.owner.owner_record_id), operations.OwnerRecord(
                    **binding.owner.model_dump(), active=True, operation_ids=(binding.operation_id,)).model_dump(mode="json")),
                (operations.control_key("policy", binding.owner.policy_id), operations.PolicyRecord(
                    policy_id=binding.owner.policy_id, revision=next(p.policy.operation_policy_revision for p in new.policies
                                                                  if p.binding_id == binding.binding_id), active=True).model_dump(mode="json")),
                (runtime_key("policy", binding.binding_id), next(p.policy.model_dump(mode="json") for p in new.policies
                                                                if p.binding_id == binding.binding_id)),
                (runtime_key("plan-index", binding.binding_id), {"keys": []}),
            )
            for key, data in rows:
                if key in inventory.rows:
                    raise _deny("TRANSITION_NEW_ID_COLLISION", "new profile records already exist")
                changed(key, data)
        if len(self.grants) != len(new.bindings):
            raise _deny("TRANSITION_NATIVE_GRANT_REQUIRED", "one exact new human-authorized launch grant per binding required")
        from .behavior_assistant_wire import Task
        for binding in new.bindings:
            grant = next((item for item in self.grants if item.job_id == binding.job_id), None)
            settings = new.assistant
            profile = next(item for item in settings.profiles if item.binding_id == binding.binding_id)
            task = Task(job_id=binding.job_id, work_id=binding.work.work_id,
                        definition_digest=digest(binding.work.model_dump(mode="json")),
                        profile_digest=digest(profile.model_dump(mode="json")), source=profile.source,
                        artifact_id=profile.artifact_id, artifact_version=profile.artifact_version,
                        operation_id=binding.operation_id).model_dump_json().encode()
            until = next(item.policy.valid_until for item in new.policies if item.binding_id == binding.binding_id)
            policy_revision = next(item.policy.operation_policy_revision for item in new.policies
                                   if item.binding_id == binding.binding_id)
            if (grant is None or grant.grant_id != native_key("bootstrap-grant", binding.job_id)
                    or grant.owner != binding.owner or grant.definition != binding.work or grant.controller_id != new.controller_id
                    or grant.workload_version != "assistant-v1.2" or grant.targets != new.active_targets
                    or grant.input_digest != hashlib.sha256(task).hexdigest() or grant.checkpoint_after_bytes is not None
                    or grant.policy_revision != policy_revision
                    or not 0 < grant.remaining_uses <= new.workers.max_generations
                    or not grant.not_before <= utc(self.clock()) < grant.expires_at <= until):
                raise _deny("TRANSITION_NATIVE_GRANT_REQUIRED", "launch grant does not bind the exact assistant task/owner/target")
            key = native_key("grant", grant.grant_id)
            if key in inventory.rows:
                raise _deny("TRANSITION_NEW_ID_COLLISION", "new launch authorization already exists")
            changed(key, grant.model_dump(mode="json"))
        if len(changes) > 80:
            raise _deny("TRANSITION_TOO_LARGE", "this atomic transition exceeds its bounded change set")
        return tuple(changes[key] for key in sorted(changes))

    def plan(self, principal, *, transition_id):
        self._operator(principal)
        until = self._fresh()
        inventory = self.store.read_inventory()
        if transition_key("current", self.old.table.scope.authority_domain) in inventory.rows:
            raise _deny("TRANSITION_ALREADY_APPLIED", "this transition never overwrites an earlier runtime transition")
        _, pointer = _without_pin_migration(self.store, inventory, self.old)
        pin_history = self._parse(inventory.rows[pointer.history_key].payload["data"], migration.MigrationHistory)
        if pin_history.plan.code_sha256 != self.retirement.old_code_sha256:
            raise _deny("TRANSITION_RETIREMENT_REQUIRED", "retirement does not identify the installed predecessor")
        images = self._changes(inventory)
        excluded = {GUARD_KEY, *("behavior:v1:row:" + image.row_id for image in images)}
        untouched = {key: value for key, value in inventory.entity_sha256.items() if key not in excluded}
        return RuntimeTransitionPlan(
            transition_id=transition_id, old_configuration_sha256=self.old.sha256, new_configuration_sha256=self.new.sha256,
            authority_domain=self.old.table.scope.authority_domain, control_binding=self.store._binding,
            code_sha256=migration.code_sha256(), inventory_sha256=digest(dict(inventory.entity_sha256)),
            untouched_sha256=digest(untouched), counter_sha256=_counter_hash(inventory.rows),
            previous_history_key=pointer.history_key, previous_history_sha256=pointer.history_sha256,
            observation_sha256=self.observation_hashes, retirement_sha256=self.retirement_sha,
            changes_sha256=digest([image.model_dump(mode="json") for image in images]),
            guard=migration.GuardAnchor(binding=inventory.guard.payload["binding"], revision=inventory.guard.payload["revision"],
                                        etag=inventory.guard.etag), valid_until=until,
        )

    def authorize(self, principal, plan, *, decision_id, expires_at):
        self._operator(principal)
        if plan != self.plan(principal, transition_id=plan.transition_id):
            raise _deny("TRANSITION_STALE_PLAN", "operator must approve the exact current transition")
        now = utc(self.clock())
        if not now < utc(expires_at) <= min(plan.valid_until, now + timedelta(minutes=20)):
            raise _deny("TRANSITION_AUTHORIZATION_EXPIRED", "bounded current operator authorization required")
        return RuntimeTransitionAuthorization(decision_id=decision_id, operator_oid=principal.oid,
                                              plan_sha256=plan.sha256, not_before=now, expires_at=expires_at)

    def _authorized(self, principal, plan, approval, *, historical=False):
        self._operator(principal)
        if (approval.operator_oid != principal.oid or approval.plan_sha256 != plan.sha256
                or plan.old_configuration_sha256 != self.old.sha256 or plan.new_configuration_sha256 != self.new.sha256
                or plan.code_sha256 != migration.code_sha256() or plan.observation_sha256 != self.observation_hashes
                or plan.retirement_sha256 != self.retirement_sha):
            raise _deny("TRANSITION_AUTHORIZATION_DENIED", "exact code/configuration/observation approval differs")
        if not historical and not approval.not_before <= utc(self.clock()) < min(approval.expires_at, plan.valid_until, self._fresh()):
            raise _deny("TRANSITION_AUTHORIZATION_EXPIRED", "operator transition authorization expired")

    def _request(self, plan, approval, images):
        return ControlRequest(request_id=transition_key("apply", plan.transition_id),
                              request_digest=digest([plan.sha256, approval.model_dump(mode="json")]),
                              read_set=tuple(image.row_id for image in images))

    def apply(self, principal, plan, approval):
        self._authorized(principal, plan, approval)
        if self.plan(principal, transition_id=plan.transition_id) != plan:
            raise _deny("TRANSITION_STALE_PLAN", "the approved inventory changed")
        inventory = self.store.read_inventory()
        if digest(dict(inventory.entity_sha256)) != plan.inventory_sha256:
            raise _deny("TRANSITION_STALE_PLAN", "the approved inventory changed")
        images = self._changes(inventory)
        request = self._request(plan, approval, images)
        history = RuntimeTransitionHistory(
            plan=plan, authorization=approval, previous_configuration=self.old.model_dump(mode="json"),
            before_guard={**inventory.guard.payload, "etag": inventory.guard.etag}, images=images,
            request=request, applied_at=utc(self.clock()),
        )
        hkey = transition_key("history", plan.transition_id)
        akey = transition_key("authorization", approval.decision_id)
        current_key = transition_key("current", plan.authority_domain)
        pointer = RuntimeTransitionCurrent(configuration_sha256=self.new.sha256, history_key=hkey,
                                           history_sha256=digest(history.model_dump(mode="json")), authorization_key=akey)
        def decide(rows):
            self._authorized(principal, plan, approval)
            for image in images:
                row = rows[image.row_id]
                if (row is None) != (image.before is None) or row is not None and (
                    row.etag != image.before_etag or row.payload != image.before
                ):
                    raise _deny("TRANSITION_STALE_PLAN", "an exact transition preimage changed")
            writes = [ControlWrite(row_id=image.row_id, mode="create" if image.before is None else "replace",
                                   data=image.after["data"]) for image in images]
            writes.extend(ControlWrite(row_id=key, mode="create", data=value.model_dump(mode="json"))
                          for key, value in ((hkey, history), (akey, approval), (current_key, pointer)))
            return ControlChanges(writes=tuple(writes), result={
                "transition_id": plan.transition_id, "new_configuration_sha256": self.new.sha256,
                "history_key": hkey, "history_sha256": pointer.history_sha256,
                "counter_sha256": plan.counter_sha256, "activation_performed": False,
            })
        return self.store.transact(request, decide, expected_guard=inventory.guard,
                                   before_send=lambda: self._authorized(principal, plan, approval),
                                   install_configuration_sha256=self.new.sha256)

    def reconcile(self, principal, plan, approval):
        self._authorized(principal, plan, approval, historical=True)
        key = transition_key("history", plan.transition_id)
        row = self.store.read_snapshot((key,))[key]
        if row is None:
            raise _deny("TRANSITION_OUTCOME_UNKNOWN", "no exact committed transition history; never reapply automatically")
        history = self._parse(row.payload["data"], RuntimeTransitionHistory)
        if history.plan != plan or history.authorization != approval:
            raise _deny("TRANSITION_HISTORY_INVALID", "retained transition approval differs")
        return self.store.reconcile(history.request)

    def verify(self, principal, plan, approval):
        receipt = self.reconcile(principal, plan, approval)
        inventory = self.store.read_inventory()
        written = {key: inventory.entity_sha256.get("behavior:v1:row:" + key) for key, _ in receipt.payload["writes"]}
        if dict(receipt.payload["writes"]) != written or inventory.installation_configuration_sha256 != self.new.sha256:
            raise _deny("TRANSITION_POSTSTATE_CHANGED", "transition writes or physical guard differ")
        excluded = {GUARD_KEY, "behavior:v1:receipt:" + receipt.payload["request"]["request_id"],
                    *("behavior:v1:row:" + key for key in written)}
        preserved = {key: value for key, value in inventory.entity_sha256.items() if key not in excluded}
        if digest(preserved) != plan.untouched_sha256 or _counter_hash(inventory.rows) != plan.counter_sha256:
            raise _deny("TRANSITION_POSTSTATE_CHANGED", "existing data or budget/slot counters changed")
        installed_transition_origin(self.store, self.new)
        return {"transition_id": plan.transition_id, "preserved_sha256": plan.untouched_sha256,
                "counter_sha256": plan.counter_sha256, "activation_performed": False}


def installed_transition_origin(store, config):
    """Validate current runtime lineage, then the retained preceding pin migration."""
    current_key = transition_key("current", config.table.scope.authority_domain)
    current = store.read_snapshot((current_key,))[current_key]
    if current is None:
        return None
    pointer = RuntimeTransition._parse(current.payload["data"], RuntimeTransitionCurrent)
    if pointer.configuration_sha256 != config.sha256 or store._guard_epoch != config.sha256:
        raise _deny("TRANSITION_HISTORY_INVALID", "runtime transition configuration/guard differs")
    rows = store.read_snapshot((pointer.history_key, pointer.authorization_key, migration.installation_record(config)[0]))
    if any(row is None for row in rows.values()):
        raise _deny("TRANSITION_HISTORY_INVALID", "runtime transition proof is incomplete")
    history = RuntimeTransition._parse(rows[pointer.history_key].payload["data"], RuntimeTransitionHistory)
    previous = ServerConfiguration.model_validate_json(canonical(history.previous_configuration))
    _config_change(previous, config)
    plan, approval = history.plan, history.authorization
    if (pointer.history_sha256 != digest(history.model_dump(mode="json"))
            or plan.new_configuration_sha256 != config.sha256 or plan.old_configuration_sha256 != previous.sha256
            or approval.plan_sha256 != plan.sha256 or approval.operator_oid != config.identity.human_oid
            or not approval.not_before <= history.applied_at < min(approval.expires_at, plan.valid_until)
            or pointer.history_key != transition_key("history", plan.transition_id)
            or pointer.authorization_key != transition_key("authorization", approval.decision_id)
            or rows[pointer.authorization_key].payload["data"] != approval.model_dump(mode="json")
            or rows[migration.installation_record(config)[0]].payload["data"] != migration.installation_record(config)[1]
            or history.request.request_id != transition_key("apply", plan.transition_id)
            or history.request.request_digest != digest([plan.sha256, approval.model_dump(mode="json")])
            or history.request.read_set != tuple(image.row_id for image in history.images)
            or digest([image.model_dump(mode="json") for image in history.images]) != plan.changes_sha256):
        raise _deny("TRANSITION_HISTORY_INVALID", "runtime transition is not an exact authorized state change")
    receipt = store.reconcile(history.request)
    expected = {image.row_id: _payload_hash(image.after) for image in history.images}
    expected.update({key: _payload_hash(row.payload) for key, row in rows.items()})
    expected[current_key] = _payload_hash(current.payload)
    if dict(receipt.payload["writes"]) != expected:
        raise _deny("TRANSITION_HISTORY_INVALID", "transition receipt does not prove all recorded writes")
    inventory = store.read_inventory()
    historical_rows, historical_receipts = dict(inventory.rows), dict(inventory.receipts)
    for image in history.images:
        if image.before is None:
            historical_rows.pop(image.row_id, None)
        else:
            historical_rows[image.row_id] = Record(image.before, image.before_etag)
    for key in (current_key, pointer.history_key, pointer.authorization_key):
        historical_rows.pop(key)
    historical_receipts.pop(history.request.request_id)
    guard_data = dict(history.before_guard)
    guard_etag = guard_data.pop("etag")
    historical = migration.AuthorityInventory(inventory.scope, Record(guard_data, guard_etag),
                                             historical_rows, historical_receipts, {}, previous.sha256)
    original = migration.installed_origin(_snapshot_store(store, historical, previous), previous)
    pin_key = migration.migration_key(config.table.scope.authority_domain, "current")
    pin = RuntimeTransition._parse(historical_rows[pin_key].payload["data"], migration.MigrationCurrent)
    if pin.history_key != plan.previous_history_key or pin.history_sha256 != plan.previous_history_sha256:
        raise _deny("TRANSITION_HISTORY_INVALID", "the original protection history anchor differs")
    store.bind_installation(migration.installation_record(config)[0], config.sha256, protected_rows=(
        *migration.protection_keys(config), pin_key, pin.history_key, pin.authorization_key,
        current_key, pointer.history_key, pointer.authorization_key,
    ))
    store._verified_migration_epoch = config.sha256
    return original
