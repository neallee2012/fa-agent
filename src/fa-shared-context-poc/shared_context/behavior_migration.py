"""Operator-only, quiescent protection-pin migration in the SAME authority.

No import/constructor I/O, bootstrap reuse, equivalence waiver, resource creation,
data deletion, work execution, or source applicability/withdrawal bypass exists.
The complete inventory and EGT use BehaviorStore's existing binding/guard format.
Migration capabilities live in this restricted adapter. Pure read scopes also
pin this adapter's exact installation and physical migration epoch.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import copy
import logging
import re
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal
from urllib.parse import urlsplit

from azure.core.exceptions import AzureError
from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from . import behavior_delivery as delivery, behavior_dispatch as dispatch, behavior_effects as effects
from . import behavior_foundry as foundry, behavior_inbox as inbox, behavior_operations as operations
from . import behavior_workers as workers, memory_governance as memory, source_backup, source_reviews
from .behavior_auth import BehaviorPrincipal
from .behavior_code_identity import code_sha256
from .behavior_runtime import CommandPlan, RuntimePolicy, _PlanIndex, runtime_key
from .behavior_wire import HostedReportBody, JobStatus
from .behavior_server_config import ProtectionObservation, ServerConfiguration
from .behavior_store import (
    GUARD_KEY, AuthorityScope, BehaviorStore, ControlChanges, ControlRequest, ControlWrite, InlineMaterial,
    Digest, _Guard, _Receipt, _Row, _REQUEST, _etag, _validated,
    _execution_owner,
)
from .errors import Problem
from .models import ID_PATTERN, Identifier, StrictModel
from .records import Record
from .source_materials import (
    InventoryProtectionVerifier, NoAutoGCConfiguration, SourceEvent, SourceHead, SourcePlan,
    SourceSnapshot, _Preparation, _SourceWithdrawal, canonical, control_key, digest, utc,
)

DEFAULT_INVENTORY_MAX_ENTITIES = 131_072
DEFAULT_INVENTORY_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_INVENTORY_MAX_PAGES = 512


@dataclass(frozen=True)
class AuthorityInventory:
    scope: AuthorityScope
    guard: Record
    rows: Mapping[str, Record] = field(repr=False)
    receipts: Mapping[str, Record] = field(repr=False)
    entity_sha256: Mapping[str, str] = field(repr=False)
    installation_configuration_sha256: str | None = None


@dataclass
class _WriteValidation:
    store: MigrationStore
    owner: tuple[int, object | None]
    check: Callable[[], None]
    active: bool = True


_WRITE_VALIDATION: ContextVar[_WriteValidation | None] = ContextVar("migration_write_validation", default=None)


class _MigratedGuard(_Guard):
    schema_version: Literal[2] = 2
    configuration_sha256: Digest


def _guard_value(raw):
    data = json.loads(raw)
    if data.get("schema_version") == 2:
        physical = _MigratedGuard.model_validate_json(raw)
        return _Guard(binding=physical.binding, revision=physical.revision), physical.configuration_sha256
    return _Guard.model_validate_json(raw), None


class _FencedClient:
    """Final-send fence also covers existing receiver adapters wrapping this client."""

    def __init__(self, client, owner):
        self._client, self._owner = client, owner

    @property
    def url(self):
        return self._client.url

    @property
    def table_name(self):
        return self._client.table_name

    def get_entity(self, **kwargs):
        entity = self._client.get_entity(**kwargs)
        if kwargs.get("row_key") == GUARD_KEY:
            try:
                logical, epoch = _guard_value(entity["Payload"])
            except (ValueError, TypeError, KeyError, ValidationError) as exc:
                raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "invalid migrated authority guard") from exc
            self._owner._observe_guard_epoch(epoch)
            if epoch is not None:
                entity = copy.deepcopy(entity)
                entity["Payload"] = json.dumps(logical.model_dump(mode="json"), ensure_ascii=False,
                                               separators=(",", ":")).encode()
        return entity

    def query_entities(self, **kwargs):
        return self._client.query_entities(**kwargs)

    def list_entities(self, **kwargs):
        return self._client.list_entities(**kwargs)

    def submit_transaction(self, operations, *, raw_request_hook, **kwargs):
        self._owner._reject_read_view()
        physical = copy.deepcopy(operations)
        epoch = self._owner._install_epoch.get() or self._owner._guard_epoch
        if epoch is not None:
            for operation in physical:
                if operation[1]["RowKey"] == GUARD_KEY:
                    logical = _Guard.model_validate_json(operation[1]["Payload"])
                    guard = _MigratedGuard(binding=logical.binding, revision=logical.revision, configuration_sha256=epoch)
                    operation[1]["Payload"] = json.dumps(guard.model_dump(mode="json"), ensure_ascii=False,
                                                         separators=(",", ":")).encode()
        def gate(request):
            self._owner._check_installation()
            if (self._owner._installation_fence is not None and self._owner._guard_epoch is not None
                    and self._owner._verified_migration_epoch != self._owner._guard_epoch):
                raise Problem(403, "MIGRATION_HISTORY_UNVERIFIED", "normal writers must verify the installed migration lineage")
            protected = self._owner._protected_rows
            if any(operation[1]["RowKey"] in protected for operation in operations):
                raise Problem(403, "MIGRATION_OPERATOR_REQUIRED", "normal writers cannot update installation pins")
            validate = self._owner._before_send.get()
            if validate is not None:
                validate()
            raw_request_hook(request)
        return self._client.submit_transaction(physical, raw_request_hook=gate, **kwargs)


class MigrationStore(BehaviorStore):
    """Additional operator inventory/fence seam without changing legacy hashes."""

    def __init__(self, client, scope, *, expected_installation: tuple[str, str] | None = None):
        self._installation_fence = None
        self._protected_rows = frozenset()
        self._guard_epoch = None
        self._verified_migration_epoch = None
        self._verified_fresh_initialization = None
        self._expected_guard = ContextVar("migration_expected_guard", default=None)
        self._before_send = ContextVar("migration_before_send", default=None)
        self._install_epoch = ContextVar("migration_install_epoch", default=None)
        super().__init__(_FencedClient(client, self), scope)
        if expected_installation is not None:
            self._set_installation(*expected_installation)

    def _set_installation(self, row_id: str, configuration_sha256: str, *, protected_rows=()):
        fence = row_id, configuration_sha256
        if self._installation_fence is not None and self._installation_fence != fence:
            raise Problem(409, "MIGRATION_INSTALLATION_CHANGED", "a running writer cannot rebind its installation")
        self._installation_fence = fence
        self._protected_rows = self._protected_rows | frozenset(
            "behavior:v1:row:" + key for key in (row_id, *protected_rows)
        )

    def bind_installation(self, row_id: str, configuration_sha256: str, *, protected_rows=()):
        self._set_installation(row_id, configuration_sha256, protected_rows=protected_rows)
        self._check_installation()

    def _observe_guard_epoch(self, epoch):
        if self._guard_epoch is not None and epoch is None:
            raise Problem(409, "MIGRATION_GUARD_DOWNGRADED", "the physical migrated guard reverted to legacy format")
        self._guard_epoch = epoch

    def _check_installation(self):
        if self._installation_fence is None:
            return
        key, expected = self._installation_fence
        row = self._read("behavior:v1:row:" + key, _Row)
        if (row is None or row[0].row_id != key or row[0].data.get("configuration_sha256") != expected
                or self._guard_epoch is not None and self._guard_epoch != expected):
            raise Problem(409, "MIGRATION_INSTALLATION_CHANGED", "writer configuration is no longer installed")

    def _guard(self):
        guard, etag = super()._guard()
        expected = self._expected_guard.get()
        if expected is not None and (expected.payload != guard.model_dump(mode="json") or expected.etag != etag):
            raise Problem(412, "BEHAVIOR_CONFLICT", "exact migration inventory guard is stale")
        self._check_installation()
        return guard, etag

    def _consistent_read_identity(self):
        return (super()._consistent_read_identity(), self._installation_fence, self._guard_epoch,
                self._verified_migration_epoch, self._protected_rows)

    def transact(self, request, decide, *, expected_guard: Record | None = None,
                 before_send: Callable[[], None] | None = None, install_configuration_sha256: str | None = None,
                 decision_final_check: Callable[[], None] | None = None, extra_read_set=()):
        if install_configuration_sha256 is not None and self._installation_fence is not None:
            raise Problem(403, "MIGRATION_OPERATOR_REQUIRED", "only an unbound operator may advance the guard format")
        guard_token = self._expected_guard.set(expected_guard)
        inherited = _WRITE_VALIDATION.get()
        def owner():
            if inherited is not None:
                if not inherited.active or inherited.store is not self or inherited.owner != _execution_owner():
                    raise Problem(403, "MIGRATION_VALIDATION_EXPIRED", "original synchronous store/thread/task validation ended")
        def validate():
            owner()
            if before_send is not None:
                before_send()
            if inherited is not None:
                inherited.check()
        anchor = None
        identity = None
        def timed_decide(rows):
            nonlocal anchor, identity
            owner()
            anchor, identity = self._guard(), self._consistent_read_identity()
            validate()
            return decide(rows)
        def final():
            owner()
            if (anchor is None or self._guard() != anchor
                    or self._consistent_read_identity() != identity):
                raise Problem(412, "BEHAVIOR_CONFLICT", "authority changed after original timed decision validation")
            decision_final_check()
        if decision_final_check is not None and (inherited is None or not callable(decision_final_check)):
            self._expected_guard.reset(guard_token)
            raise Problem(403, "MIGRATION_VALIDATION_REQUIRED", "timed decision requires an owned validated operation")
        # A time-sensitive decision runs AFTER all full validators. Its exact
        # snapshot remains fenced by physical guard/ETag and original EGT CAS;
        # the final callback must only recheck finite dispatch deadlines.
        send_token = self._before_send.set(
            final if decision_final_check is not None else validate if inherited is not None else before_send)
        epoch_token = self._install_epoch.set(install_configuration_sha256)
        try:
            return super().transact(request, timed_decide if decision_final_check is not None else decide,
                                    extra_read_set=extra_read_set)
        finally:
            self._before_send.reset(send_token)
            self._expected_guard.reset(guard_token)
            self._install_epoch.reset(epoch_token)

    def validated_writes(self, callback, check):
        """Additional deny-only final checks around existing synchronous producers."""
        if not callable(callback) or not callable(check):
            raise Problem(422, "MIGRATION_VALIDATION_INVALID", "synchronous operation and validator are required")
        if _WRITE_VALIDATION.get() is not None:
            raise Problem(409, "MIGRATION_VALIDATION_NESTED", "original write validation cannot be replaced")
        state = _WriteValidation(self, _execution_owner(), check)
        token = _WRITE_VALIDATION.set(state)
        try:
            result = callback()
            if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
                if inspect.iscoroutine(result) or inspect.isgenerator(result):
                    result.close()
                raise Problem(422, "MIGRATION_VALIDATION_INVALID", "validated operation must finish synchronously")
            return result
        finally:
            state.active = False
            _WRITE_VALIDATION.reset(token)

    def read_inventory(
        self, *, max_entities=DEFAULT_INVENTORY_MAX_ENTITIES,
        max_bytes=DEFAULT_INVENTORY_MAX_BYTES, max_pages=DEFAULT_INVENTORY_MAX_PAGES,
        log_capacity: bool = False,
    ) -> AuthorityInventory:
        self._reject_read_view()
        if (max_entities is not None and (type(max_entities) is not int or max_entities < 1)
                or any(type(value) is not int or value < 1 for value in (max_bytes, max_pages))):
            raise Problem(422, "BEHAVIOR_INVENTORY_LIMIT", "positive explicit inventory bounds required")
        self._check_client()
        guard, guard_etag = self._guard()
        rows, receipts, hashes = {}, {}, {}
        pages, size = 0, 0
        query_path = self._client_url[2] or "/" + self._scope.table_name

        def gate(request):
            nonlocal pages
            wire = request.http_request
            target = urlsplit(wire.url)
            if (wire.method != "GET" or (target.scheme, target.netloc) != self._origin
                    or target.path.rstrip("()").rstrip("/") != query_path.rstrip("/")):
                raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "inventory request escaped its exact Table")
            pages += 1
            if pages > max_pages:
                raise Problem(413, "BEHAVIOR_INVENTORY_LIMIT", "complete inventory exceeds page bound")

        try:
            query = self._client.query_entities(
                query_filter="PartitionKey eq @partition", parameters={"partition": self._scope.partition_key},
                select=["PartitionKey", "RowKey", "Payload"], results_per_page=1000,
                raw_request_hook=gate, **_REQUEST,
            )
            for page in query.by_page():
                for entity in page:
                    key, raw = entity["RowKey"], entity["Payload"]
                    if (entity["PartitionKey"] != self._scope.partition_key or not isinstance(key, str)
                            or not isinstance(raw, bytes) or len(raw) > self._scope.max_payload_bytes or key in hashes):
                        raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "invalid or duplicate inventory entity")
                    size += len(raw)
                    if (max_entities is not None and len(hashes) >= max_entities) or size > max_bytes:
                        raise Problem(413, "BEHAVIOR_INVENTORY_LIMIT", "complete inventory exceeds its bound")
                    etag = _etag(entity.metadata["etag"])
                    if key == GUARD_KEY:
                        value, epoch = _guard_value(raw)
                        self._observe_guard_epoch(epoch)
                        if value != guard or etag != guard_etag:
                            raise Problem(412, "BEHAVIOR_CONFLICT", "guard changed during inventory")
                    elif key.startswith("behavior:v1:row:"):
                        value = _validated(_Row.model_validate_json(raw), _Row)
                        if value.row_id != key.removeprefix("behavior:v1:row:"):
                            raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "inventory row identity differs")
                        rows[value.row_id] = Record(value.model_dump(mode="json"), etag)
                    elif key.startswith("behavior:v1:receipt:"):
                        value = _validated(_Receipt.model_validate_json(raw), _Receipt)
                        if value.request.request_id != key.removeprefix("behavior:v1:receipt:"):
                            raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "inventory receipt identity differs")
                        receipts[value.request.request_id] = Record(value.model_dump(mode="json"), etag)
                    else:
                        raise Problem(409, "BEHAVIOR_INVENTORY_UNRECOGNIZED", "unrecognized authority entity",
                                      entity_key=key)
                    if value.binding != self._binding:
                        raise Problem(409, "BEHAVIOR_AUTHORITY_MISMATCH", "inventory entity binding differs")
                    hashes[key] = hashlib.sha256(raw).hexdigest()
        except AzureError as exc:
            raise Problem(503, "BEHAVIOR_READ_FAILED", "complete authority inventory unavailable") from exc
        except (ValidationError, KeyError, TypeError, ValueError, AttributeError) as exc:
            raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "invalid inventory response") from exc
        after, after_etag = self._guard()
        if after != guard or after_etag != guard_etag:
            raise Problem(412, "BEHAVIOR_CONFLICT", "authority changed during complete inventory")
        if GUARD_KEY not in hashes or not pages:
            raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "inventory omitted its authority guard")
        if log_capacity:
            # Use the API's configured INFO sink without enabling other SDK loggers.
            # bytes_pct is of the bound this read enforced; the field name stays for existing log searches.
            logging.getLogger("uvicorn.error").info(
                "BEHAVIOR_INVENTORY_READ bytes=%d entities=%d bytes_pct_of_default=%.2f entities_pct_of_default=%.2f",
                size, len(hashes), 100 * size / max_bytes,
                100 * len(hashes) / DEFAULT_INVENTORY_MAX_ENTITIES,
            )
        return AuthorityInventory(self._scope, Record(guard.model_dump(mode="json"), guard_etag),
                                  MappingProxyType(rows), MappingProxyType(receipts), MappingProxyType(hashes), self._guard_epoch)


def installation_record(config: ServerConfiguration):
    return control_key("server-installation", config.table.scope.authority_domain), {
        "schema_version": "fa-behavior-installation-v1", "configuration_sha256": config.sha256,
        "deployment_id": config.deployment_id, "workspace_id": config.workers.workspace_id,
        "control_binding": config.workers.control_binding, "capacity": config.workers.capacity,
    }


def migration_key(authority, kind, identity="current"):
    return control_key("protection-migration", authority, kind, identity)


def protection_keys(config):
    return (
        config.sources.protection.key,
        control_key("source-backup-protection", config.table.scope.authority_domain,
                    config.backups.protection.container_url),
    )


class _Model(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class GuardAnchor(_Model):
    binding: Digest
    revision: Annotated[int, Field(ge=0)]
    etag: Annotated[str, Field(min_length=1, max_length=1024)]

    @field_validator("etag")
    @classmethod
    def exact_etag(cls, value):
        return _etag(value)


class MigrationPlan(_Model):
    schema_version: Literal["fa-protection-pin-plan-v1"] = "fa-protection-pin-plan-v1"
    migration_id: Identifier
    authority: AuthorityScope
    control_binding: Digest
    operator_oid: Identifier
    old_configuration_sha256: Digest
    new_configuration_sha256: Digest
    old_sources: NoAutoGCConfiguration
    old_backups: NoAutoGCConfiguration
    new_sources: NoAutoGCConfiguration
    new_backups: NoAutoGCConfiguration
    observation_sha256: dict[str, Digest]
    previous_observation_sha256: dict[str, Digest]
    code_sha256: Digest
    guard: GuardAnchor
    old_guard_schema: Literal[1] = 1
    new_guard_schema: Literal[2] = 2
    pointer_etags: dict[str, str]
    inventory_sha256: Digest
    preserved_sha256: Digest
    preserved_groups: dict[str, Digest]
    quiescence_counts: dict[str, int]
    unsubmitted_jobs: tuple[Identifier, ...]
    valid_until: datetime

    @field_validator("valid_until")
    @classmethod
    def utc_validity(cls, value):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("migration plan validity must use UTC")
        return value

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))


class MigrationAuthorization(_Model):
    schema_version: Literal["fa-protection-pin-authorization-v1"] = "fa-protection-pin-authorization-v1"
    decision_id: Identifier
    action: Literal["apply-quiescent-protection-pin-update"] = "apply-quiescent-protection-pin-update"
    operator_oid: Identifier
    plan_sha256: Digest
    old_configuration_sha256: Digest
    new_configuration_sha256: Digest
    code_sha256: Digest
    observation_sha256: dict[str, Digest]
    previous_observation_sha256: dict[str, Digest]
    not_before: datetime
    expires_at: datetime

    @field_validator("not_before", "expires_at")
    @classmethod
    def times(cls, value):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("operator authorization must use UTC")
        return value

    @model_validator(mode="after")
    def interval(self):
        if self.expires_at <= self.not_before:
            raise ValueError("operator authorization interval must be positive")
        return self


class MigrationHistory(_Model):
    schema_version: Literal["fa-protection-pin-history-v1"]
    plan: MigrationPlan
    authorization_sha256: Digest
    applied_at: datetime
    request: ControlRequest


class MigrationCurrent(_Model):
    schema_version: Literal["fa-protection-pin-current-v1"]
    configuration_sha256: Digest
    history_key: Identifier
    history_sha256: Digest
    authorization_key: Identifier


class _DeliveryEvent(delivery._Bound):
    kind: Literal["delivery_event"] = "delivery_event"
    event_id: Identifier
    root_request_id: Identifier
    conversation_id: Identifier
    causation_id: Identifier | None = None
    sequence: Annotated[int, Field(ge=1)]
    work_id: Identifier
    source: delivery.VerifiedIngress
    content_digest: Digest
    admission_request_id: Identifier
    execution_context: operations.ExecutionContext


class _WorkIdentity(delivery._Bound):
    kind: Literal["delivery_work_identity"] = "delivery_work_identity"
    work_id: Identifier
    event_id: Identifier
    obligation_id: Identifier
    definition_digest: Digest
    state_row_id: Identifier


class _DeliveryReceipt(delivery._Bound):
    kind: Literal["delivery_verified_receipt"] = "delivery_verified_receipt"
    evidence: delivery.VerifiedDeliveryReceipt


class _UnclaimedFence(_Model):
    kind: Literal["terminal_unclaimed_launch_fence"] = "terminal_unclaimed_launch_fence"
    launch_id: Identifier
    original_start_request_id: Identifier
    cancel_requested: bool
    claim_never_issued: Literal[True]
    no_provider_stop_claim: Literal[True]
    failure_code: Literal["CONVERSATION_SESSION_VERSION_CHANGED"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    observed_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")] | None = Field(
        default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def exact_disposition(self):
        if (self.failure_code is None and (not self.cancel_requested or self.observed_agent_version is not None)
                or self.failure_code is not None and (self.cancel_requested or self.observed_agent_version is None)):
            raise ValueError("an unclaimed fence needs its exact cancellation or version-refusal disposition")
        return self


def _problem(code, message, **details):
    return Problem(409, code, message, **details)


def _parsed(value, model):
    try:
        return model.model_validate_json(canonical(value))
    except (ValueError, TypeError, ValidationError) as exc:
        raise _problem("MIGRATION_RECORD_INVALID", "authoritative record does not match its typed schema",
                       record_model=model.__name__) from exc


def _row(inventory, key):
    result = inventory.rows.get(key)
    if result is None:
        raise _problem("MIGRATION_REQUIRED_RECORD_MISSING", "required authority record is absent", row_id=key)
    return result


def _receipt(inventory, request):
    record = inventory.receipts.get(request.request_id)
    if record is None:
        return None
    if _parsed(record.payload["request"], ControlRequest) != request:
        raise _problem("MIGRATION_REQUEST_CONFLICT", "retained request is bound to different input",
                       request_id=request.request_id)
    return record


def origin_pointer_keys(domain: str) -> tuple[str, str, str, str, str]:
    """The pointers installed_origin dispatches on, in its order; a fresh installation has none after the first."""
    from .behavior_fresh_installation import fresh_key
    from .behavior_work_policy import work_policy_key
    return (fresh_key(domain), work_policy_key("current", domain), migration_key(domain, "current"),
            control_key("runtime-transition-current", domain), control_key("runtime-rotation-current", domain))


def installed_origin_pins(store: MigrationStore, config: ServerConfiguration) -> dict[str, str | None] | None:
    """Key reads only: None unless installed_origin dispatches to the standing-policy origin, the one kind that reads
    the whole partition (read_inventory(max_entities=None)); else the hash of each pointer and named history row."""
    from .behavior_channels_migration import channel_key
    from .behavior_runtime_transition import _payload_hash
    domain = config.table.scope.authority_domain
    pointers = origin_pointer_keys(domain)
    rows = store.read_snapshot((*pointers, channel_key("current", domain)))
    if rows[pointers[0]] is not None or rows[pointers[1]] is None:
        return None
    # A name that is not a row key is hashed only in its pointer, so the full check refuses it with its own code.
    named = {value for row in rows.values() if row is not None
             for value in (row.payload["data"].get("history_key"), row.payload["data"].get("history_id"))
             if isinstance(value, str) and re.fullmatch(ID_PATTERN, value)}
    if named:
        rows.update(store.read_snapshot(tuple(sorted(named))))
    return {key: None if row is None else _payload_hash(row.payload) for key, row in rows.items()}


def installed_origin(store: MigrationStore, config: ServerConfiguration) -> ServerConfiguration:
    """Verify exact immutable lineage; the original atomic probe is not rerun."""
    from .behavior_fresh_installation import installed_fresh_origin
    store._verified_fresh_initialization = None
    initial, current_work_policy, current_key, transition_key, rotation_key = origin_pointer_keys(
        config.table.scope.authority_domain)
    if store.read_snapshot((initial,))[initial] is not None:
        return installed_fresh_origin(store, config)
    if store.read_snapshot((current_work_policy,))[current_work_policy] is not None:
        from .behavior_work_policy_installation import installed_work_policy_origin
        return installed_work_policy_origin(store,config)
    if config.schema_version == "fa-behavior-server-v2":
        from .behavior_channels_migration import installed_channels_origin
        return installed_channels_origin(store, config)
    key, expected = installation_record(config)
    rows = store.read_snapshot((key, current_key, transition_key, rotation_key, *protection_keys(config)))
    if rows[key] is None or rows[key].payload["data"] != expected:
        raise Problem(503, "BEHAVIOR_CONFIGURATION", "operator-approved installation differs")
    if rows[rotation_key] is not None:
        from .behavior_runtime_rotation import installed_rotation_origin
        return installed_rotation_origin(store, config)
    rows.pop(rotation_key)
    if rows[transition_key] is not None:
        from .behavior_runtime_transition import installed_transition_origin
        return installed_transition_origin(store, config)
    rows = {row_id: row for row_id, row in rows.items() if row_id != transition_key}
    if rows[current_key] is None:
        if store._guard_epoch is not None:
            raise _problem("MIGRATION_HISTORY_INVALID", "migrated guard has no exact installation history")
        store.bind_installation(key, config.sha256, protected_rows=(*protection_keys(config), current_key))
        return config
    pointer = _parsed(rows[current_key].payload["data"], MigrationCurrent)
    if pointer.configuration_sha256 != config.sha256 or store._guard_epoch != config.sha256:
        raise _problem("MIGRATION_HISTORY_INVALID", "migration pointer configuration differs")
    details = store.read_snapshot((pointer.history_key, pointer.authorization_key))
    if any(value is None for value in details.values()):
        raise _problem("MIGRATION_HISTORY_INVALID", "immutable migration history/authorization is missing")
    history_data = details[pointer.history_key].payload["data"]
    history = _parsed(history_data, MigrationHistory)
    approval_data = details[pointer.authorization_key].payload["data"]
    approval = _parsed(approval_data, MigrationAuthorization)
    plan = history.plan
    domain = config.table.scope.authority_domain
    if (digest(history_data) != pointer.history_sha256 or digest(approval_data) != history.authorization_sha256
            or plan.new_configuration_sha256 != config.sha256 or plan.authority != config.table.scope
            or plan.control_binding != config.workers.control_binding
            or plan.operator_oid != config.identity.human_oid or approval.operator_oid != plan.operator_oid
            or approval.plan_sha256 != plan.sha256 or approval.code_sha256 != plan.code_sha256
            or approval.old_configuration_sha256 != plan.old_configuration_sha256
            or approval.new_configuration_sha256 != plan.new_configuration_sha256
            or approval.observation_sha256 != plan.observation_sha256
            or approval.previous_observation_sha256 != plan.previous_observation_sha256
            or not approval.not_before <= utc(history.applied_at) < min(approval.expires_at, plan.valid_until)
            or pointer.history_key != migration_key(domain, "history", plan.migration_id)
            or pointer.authorization_key != migration_key(domain, "authorization", approval.decision_id)):
        raise _problem("MIGRATION_HISTORY_INVALID", "migration history is not an exact approved transition")
    original = config.model_dump(mode="json")
    original["sources"]["protection"] = plan.old_sources.model_dump(mode="json")
    original["backups"]["protection"] = plan.old_backups.model_dump(mode="json")
    original = ServerConfiguration.model_validate_json(json.dumps(original))
    if original.sha256 != plan.old_configuration_sha256:
        raise _problem("MIGRATION_HISTORY_INVALID", "historical configuration cannot be reconstructed exactly")
    constrained = original.model_dump(mode="json")
    current = config.model_dump(mode="json")
    for lane in ("sources", "backups"):
        constrained[lane]["protection"]["evidence_sha256"] = current[lane]["protection"]["evidence_sha256"]
    if constrained != current:
        raise _problem("MIGRATION_HISTORY_INVALID", "history is not an evidence-pin-only configuration change")
    if plan.new_sources != config.sources.protection or plan.new_backups != config.backups.protection:
        raise _problem("MIGRATION_HISTORY_INVALID", "installed protection differs from approved history")
    expected_request = ControlRequest(
        request_id=migration_key(domain, "apply", plan.migration_id),
        request_digest=digest([plan.sha256, approval.model_dump(mode="json")]),
        read_set=(key, *protection_keys(config), pointer.authorization_key, pointer.history_key, current_key),
    )
    if history.request != expected_request:
        raise _problem("MIGRATION_HISTORY_INVALID", "migration request identity differs")
    receipt = store.reconcile(expected_request)
    proof_rows = {**rows, **details}
    if any(record is None for record in proof_rows.values()):
        raise _problem("MIGRATION_HISTORY_INVALID", "atomic migration write set is incomplete")
    actual = {
        record_id: hashlib.sha256(json.dumps(record.payload, ensure_ascii=False, separators=(",", ":"),
                                             allow_nan=False).encode()).hexdigest()
        for record_id, record in proof_rows.items()
    }
    if dict(receipt.payload["writes"]) != actual:
        raise _problem("MIGRATION_HISTORY_INVALID", "current pins/history are not the atomic migration writes")
    for lane, pin_key in zip(("sources", "backups"), protection_keys(config)):
        if rows[pin_key].payload["data"] != getattr(config, lane).protection.model_dump(mode="json"):
            raise _problem("MIGRATION_HISTORY_INVALID", "current source/backup pin differs")
    store.bind_installation(key, config.sha256, protected_rows=(
        *protection_keys(config), current_key, pointer.history_key, pointer.authorization_key,
    ))
    store._verified_migration_epoch = config.sha256
    return original


def _origin_probe(inventory, config):
    keys = {kind: control_key("server-authority-probe", config.sha256, kind)
            for kind in ("intent", "budget", "winner", "loser", "proof")}
    winner = ControlRequest(request_id=control_key("server-authority-probe-winner", config.sha256),
                            request_digest=digest(["authority-probe-winner", config.sha256]), read_set=(keys["budget"],))
    receipt = _receipt(inventory, winner)
    if (receipt is None or keys["loser"] in inventory.rows
            or control_key("server-authority-probe-loser", config.sha256) in inventory.receipts
            or _row(inventory, keys["budget"]).payload["data"] != {"remaining": 0, "configuration_sha256": config.sha256}
            or _row(inventory, keys["winner"]).payload["data"] != {"winner": True, "control_binding": config.workers.control_binding}):
        raise _problem("MIGRATION_ATOMIC_PROOF_MISSING", "original authority capability proof is incomplete")
    hashes = {key: inventory.entity_sha256["behavior:v1:row:" + key] for key in (keys["budget"], keys["winner"])}
    expected = {
        "configuration_sha256": config.sha256, "control_binding": config.workers.control_binding, "stale_batch_status": 412,
        "winner_request_id": winner.request_id,
        "loser_request_id": control_key("server-authority-probe-loser", config.sha256),
        "winner_receipt_sha256": digest(receipt.payload), "business_payload_sha256": hashes,
    }
    if dict(receipt.payload["writes"]) != hashes or _row(inventory, keys["proof"]).payload["data"] != expected:
        raise _problem("MIGRATION_ATOMIC_PROOF_MISSING", "original immutable capability evidence differs")
    return set(keys.values())


_KIND_NAMES = frozenset({
    "operation_owner", "operation_policy", "operation_grant", "operation_budget", "operation", "operation_attempt",
    "operation_dispatch", "operation_dispatch_obligation", "work_effect_delivery", "work_effect_receipt",
    "delivery_quota", "delivery_pending_index", "delivery_conversation", "delivery_work", "delivery_obligation",
    "delivery_outbox", "delivery_processing_intent", "delivery_due", "delivery_consumer_plan", "delivery_consumer_page",
    "worker_pool", "worker_readiness", "worker_job", "worker_attempt", "worker_inactive_cancel", "worker_result_forward",
    "native_execution_grant", "native_target_reservation", "native_launch", "native_probe", "native_executor_evidence",
    "inbox_deployment_evidence", "inbox_exchange", "inbox_client_acknowledgment",
})


def _kind_models():
    from .behavior_fresh_installation import FreshInstallation
    from .behavior_conversation_transfer import EnvironmentTransfer, EnvironmentHead
    from .behavior_delegation import DelegationIntent, DelegationState, DelegationResult, DelegationModelUsage
    from .behavior_agent_message_history import MESSAGE_MODELS
    result = {}
    from .memory_bridge_settings import MemoryBridgeSettingsRecord, MemoryBridgeSettingsHead
    from .behavior_delivery_pending import PendingHead, PendingMember, PendingPage
    from .behavior_work_registry import WorkRegistryHead, WorkRegistryPage
    from .behavior_agent_role_prompt_versions import AgentRolePromptHead, AgentRolePromptVersion
    from .team_connector_narrowing import AgentConnectorNarrowing
    for module in (delivery, dispatch, effects, workers, foundry, inbox, operations):
        for value in vars(module).values():
            if not inspect.isclass(value) or not hasattr(value, "model_fields") or "kind" not in value.model_fields:
                continue
            kind = value.model_fields["kind"].default
            if isinstance(kind, str) and kind in _KIND_NAMES:
                result[kind] = value
    for model in (FreshInstallation, _DeliveryEvent, _WorkIdentity, _DeliveryReceipt, _UnclaimedFence, EnvironmentTransfer, EnvironmentHead,
                  MemoryBridgeSettingsRecord, MemoryBridgeSettingsHead, PendingHead, PendingPage, PendingMember,
                  WorkRegistryHead, WorkRegistryPage, AgentRolePromptVersion, AgentRolePromptHead, AgentConnectorNarrowing,
                  DelegationIntent, DelegationState, DelegationResult, DelegationModelUsage, *MESSAGE_MODELS):
        result[model.model_fields["kind"].default] = model
    return result


class _SessionStopAttempt(StrictModel):
    session_id: str = Field(min_length=1, max_length=512)
    agent_name: str = Field(min_length=1, max_length=512)
    state_before: Literal["idle"]
    stop_only_no_delete: Literal[True]


class _SessionVersion(StrictModel):
    type: Literal["version_ref"]
    agent_version: str = Field(min_length=1, max_length=128)


class _SessionMetadata(StrictModel):
    agent_session_id: str = Field(min_length=1, max_length=512)
    version_indicator: _SessionVersion
    status: Literal["idle"]
    created_at: int = Field(ge=0)
    last_accessed_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)


class _SessionStopResponse(StrictModel):
    status: Literal[200, 202, 204, 409]
    request_id: Annotated[str, Field(min_length=1, max_length=512)] | None
    error_code: Literal["session_already_stopped"] | None = None

    @model_validator(mode="after")
    def known_response(self):
        if self.status == 409 and self.error_code != "session_already_stopped":
            raise ValueError("the retained conflict must be session_already_stopped")
        return self


class _RetainedSessionStopResponse(StrictModel):
    session_id: str = Field(min_length=1, max_length=512)
    error_code: Literal["session_already_stopped"]
    request_id: str = Field(min_length=1, max_length=512)
    source_log_sha256: Digest
    stop_resent: Literal[False]


class _SessionStopObservation(StrictModel):
    agent: str = Field(min_length=1, max_length=512)
    session: str = Field(min_length=1, max_length=512)
    before: Literal["idle"]
    after: Literal["idle"]
    metadata: _SessionMetadata
    stop_only_no_delete: Literal[True]
    provider_stop_response: _SessionStopResponse | _RetainedSessionStopResponse
    no_cpu_state_claim_from_idle_metadata: Literal[True]


class _CloseoutJob(StrictModel):
    job_id: Identifier
    state: Literal["succeeded", "failed", "cancelled"]
    generation: int = Field(ge=0)
    known_effects: tuple[str, ...]
    fulfillment: Literal["fulfilled"]


class _ServiceCloseout(StrictModel):
    kind: Literal["c1_final_service_closeout"]
    observed_at: datetime
    configuration_sha256: Digest
    jobs: tuple[_CloseoutJob, ...]
    budget: operations.BudgetRecord
    pool_empty: Literal[True]
    delivery_index_empty: Literal[True]
    root5_work_absent: Literal[True]
    sessions: tuple[_SessionStopObservation, ...]
    data_or_session_deletes: Literal[0]


class _HistoricalSnapshot(StrictModel):
    pool: workers._Pool
    budget: operations.BudgetRecord
    jobs: dict[Identifier, workers.JobRecord | None]


class _BootstrapResult(StrictModel):
    stage: Literal["records", "worker", "activate"]
    configuration_sha256: Digest
    own_authority_probe: Literal["VERIFIED"]
    prerequisite_verification: Literal["RECORDED", "INCOMPLETE"]
    actual_environment: Literal["NOT_RUN"]
    full_scenario_pass: Literal[False]


class _ControllerResult(StrictModel):
    ticks: int = Field(ge=0)
    worker_items: int = Field(ge=0)
    delivery_items: int = Field(ge=0)
    problem_counts: dict[Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,96}$")], Annotated[int, Field(ge=0)]]
    preactivation: Literal[True]
    actual_environment: Literal["NOT_RUN"]
    full_scenario_pass: Literal[False]


class _OperatorProof(StrictModel):
    at: datetime
    configuration_sha256: Digest


class _BootstrapProof(_OperatorProof):
    kind: Literal["c1_native_bootstrap"]
    result: _BootstrapResult


class _ControllerProof(_OperatorProof):
    kind: Literal["c1_independent_controller_cycle"]
    result: _ControllerResult


class _RootStartProof(_OperatorProof):
    kind: Literal["c1_root_started"]
    tick: JobStatus
    root: workers.JobRecord
    launch: foundry._NativeLaunch


class _CapacityProof(_OperatorProof):
    kind: Literal["c1_capacity_and_real_lease_verified"]
    lease_until: datetime
    checked_at: datetime
    adopt_error: Literal["WORKER_TAKEOVER_UNCERTAIN"]
    before: _HistoricalSnapshot
    after: _HistoricalSnapshot
    child_start_not_sent: Literal[True]
    root_budget_unchanged: Literal[True]


class _HistoricalStart(StrictModel):
    actual_environment: Literal["NOT_RUN"]
    status: Literal["starting"]
    binding: workers.ExecutorBinding


class _HistoricalCompletion(StrictModel):
    actual_environment: Literal["NOT_RUN"]
    status: Literal["succeeded", "failed", "stopped"]
    job_id: Identifier
    cancel_requested: bool
    completion_evidence_id: Identifier


_HistoricalJobState = Literal[
    "queued", "starting", "running", "capacity_uncertain", "waiting_dependency", "succeeded", "failed", "cancelled",
]


class _HistoricalTick(StrictModel):
    number: int = Field(ge=0, lt=12)
    result: JobStatus | _HistoricalStart | _HistoricalCompletion
    parent_state: _HistoricalJobState
    parent_generation: int = Field(ge=0)
    child_state: _HistoricalJobState
    child_work_id: Identifier
    parent_work_id: Identifier


class _TickProof(_OperatorProof, _HistoricalTick):
    kind: Literal["c1_tick"]


class _PreparedDelivery(StrictModel):
    actual_environment: Literal["NOT_RUN"]
    execution_enabled: Literal[False]
    status: Literal["awaiting_client_ack"]
    delivery_id: Identifier


class _WorkCompleteProof(_OperatorProof):
    kind: Literal["c1_native_parent_child_work_verified"]
    ticks: Annotated[tuple[_HistoricalTick, ...], Field(min_length=1, max_length=12)]
    checkpoint_sha256: Digest
    prepared_delivery: _PreparedDelivery
    client_ack_pending: Literal[True]
    no_cpu_overlap_or_shared_filesystem_claim: Literal[True]
    final: _HistoricalSnapshot


class _OperatorAttempt(StrictModel):
    action: Literal["root-start", "capacity-lease", "progress", "lifecycle"]
    configuration_sha256: Digest
    consumed: Literal[True]


class _ClosedBinding(StrictModel):
    binding: Identifier
    job: Literal["succeeded", "cancelled"]
    generation: int = Field(ge=0)
    known_effects: Annotated[tuple[Identifier, ...], Field(max_length=16)]
    fulfillment: Literal["fulfilled"]
    row_hashes: dict[Digest, Digest]


class _ObservedTarget(StrictModel):
    target: foundry.FoundryTarget
    session: _SessionMetadata
    version: str
    image: str
    native_metadata_sha256: Digest


class _RuntimeObservation(StrictModel):
    kind: Literal["c1_fresh_native_runtime_observation"]
    observed_at: datetime
    configuration_sha256: Digest
    closed: tuple[_ClosedBinding, ...]
    budget: operations.BudgetRecord
    pool: workers._Pool
    sessions: tuple[_ObservedTarget, ...]
    source_root_unsubmitted: Literal[True]
    business_or_configuration_changes: Literal[False]
    operator_evidence_record_added: Literal[True]


class _WorkerRenewal(StrictModel):
    kind: Literal["c1_renewed_worker_and_actual_cycle"]
    worker_evidence_id: Identifier
    worker_receipt_sha256: Digest
    configuration_sha256: Digest
    result: _ControllerResult
    business_execution: Literal[False]


class _InboxRenewal(StrictModel):
    kind: Literal["c1_renewed_inbox"]
    configuration_sha256: Digest
    evidence_id: Identifier
    receipt_sha256: Digest
    business_state_or_counter_reset: Literal[False]


class _OperationDeliveryPreparation(StrictModel):
    actual_environment: Literal["NOT_RUN"]
    execution_enabled: Literal[False]
    status: Literal["awaiting_client_ack"]
    binding: delivery.DeliveryBinding
    message_kind: Literal["failure"]
    obligation_fulfilled: Literal[False]


class _OperationCaseProof(StrictModel):
    kind: Literal["native_operation_case_closed"]
    case: Literal["revoked", "lost-receipt", "mutable-alias"]
    binding_id: Identifier
    operation_id: Identifier
    job_state: Literal["cancelled"]
    generation: Literal[0]
    known_effects: Annotated[tuple[Identifier, ...], Field(max_length=1)]
    unresolved_operations: Annotated[tuple[Identifier, ...], Field(max_length=0)]
    obsolete_input_not_executed: Literal[True]
    fault_injection: Literal[
        "caller-side after actual Table receiver commit", "controller-side valid alias substitution",
    ] | None
    delivery: _OperationDeliveryPreparation
    client_ack_pending: Literal[True]
    before_sha256: dict[Digest, Digest]
    after_sha256: dict[Digest, Digest]


class _NativePostIntent(StrictModel):
    request: ControlRequest
    post_digest: Digest
    target: foundry.FoundryTarget
    launch_id: Identifier


class _WorkerAdmission(StrictModel):
    job_id: Identifier
    intent_id: Identifier
    enqueue_request: ControlRequest


def _native_post_request(config, launch, *, targets=None, instruction=False):
    binding = launch.start.binding
    reads = {
        foundry.native_key("launch", launch.launch_id),
        workers.worker_key("attempt", binding.job_id, str(binding.generation)),
        workers.worker_key("pool", config.workers.workspace_id, *((binding.job_id,) if instruction else ())),
        workers.worker_key("job", binding.job_id),
        foundry.native_key("grant", launch.grant_id),
        control_key("owner", binding.owner.owner_record_id), control_key("policy", binding.owner.policy_id),
        control_key("budget", binding.owner.root_budget_id),
        *(foundry.native_key("target", target.executor_id, *((binding.job_id,) if instruction else ()))
          for target in (config.active_targets if targets is None else targets)),
    }
    return ControlRequest(request_id=foundry.native_key("prepare", launch.launch_id),
                          request_digest=digest(launch.start.model_dump(mode="json")), read_set=tuple(sorted(reads)))


def _operator_proofs(inventory, config):
    models = {
        **{"bootstrap-" + stage: _BootstrapProof for stage in ("records", "worker", "activate")},
        "controller-preactivation": _ControllerProof, "root-start": _RootStartProof,
        "capacity-lease": _CapacityProof, "work-complete": _WorkCompleteProof,
        **{"progress-" + str(i): _TickProof for i in range(12)},
    }
    history = {}
    job_specs = {job.job_id: job.work.work_id for _, job in config.registered_jobs}
    root_ids = {binding.job_id for binding in config.bindings}
    child_works = {job.work.work_id for binding in config.bindings for job in binding.child_jobs}

    def snapshot(value):
        if (value.pool.workspace_id != config.workers.workspace_id
                or value.budget.root_budget_id not in {root.root_budget_id for root in config.roots}
                or any(job_id not in job_specs or job is not None and
                       (job.spec.job_id != job_id or job.spec.work_id != job_specs[job_id])
                       for job_id, job in value.jobs.items())):
            raise _problem("MIGRATION_RECORD_INVALID", "historical snapshot is outside the installed work scope")

    def tick(value):
        if (value.parent_work_id not in {job_specs[job_id] for job_id in root_ids}
                or value.child_work_id not in child_works):
            raise _problem("MIGRATION_RECORD_INVALID", "historical tick work differs")
        result = value.result
        bound = result.binding if isinstance(result, _HistoricalStart) else result
        if bound.job_id not in job_specs:
            raise _problem("MIGRATION_RECORD_INVALID", "historical tick job differs")

    for name, model in models.items():
        key = control_key("c1-operator-proof", name)
        if key not in inventory.rows:
            continue
        data = _row(inventory, key).payload["data"]
        value = _parsed(data, model)
        request = ControlRequest(request_id=control_key("c1-operator-proof-write", name), request_digest=digest(data))
        if value.configuration_sha256 != config.sha256 or _receipt(inventory, request) is None:
            raise _problem("MIGRATION_RECORD_INVALID", "historical operator proof binding differs", row_id=key)
        utc(value.at)
        if isinstance(value, _BootstrapProof):
            if name != "bootstrap-" + value.result.stage or value.result.configuration_sha256 != config.sha256:
                raise _problem("MIGRATION_RECORD_INVALID", "historical bootstrap stage differs", row_id=key)
        elif isinstance(value, _RootStartProof):
            if (value.root.spec.job_id not in root_ids or value.tick.job_id != value.root.spec.job_id
                    or value.launch.target not in config.targets or value.launch.start.binding != value.root.binding
                    or value.root.spec.work_id != job_specs[value.root.spec.job_id]):
                raise _problem("MIGRATION_RECORD_INVALID", "historical root start binding differs", row_id=key)
        elif isinstance(value, _CapacityProof):
            snapshot(value.before)
            snapshot(value.after)
            if (value.before.budget != value.after.budget or value.before.pool.slots != value.after.pool.slots
                    or utc(value.checked_at) <= utc(value.lease_until)):
                raise _problem("MIGRATION_RECORD_INVALID", "historical capacity observation differs", row_id=key)
        elif isinstance(value, _TickProof):
            tick(value)
            if name != "progress-" + str(value.number):
                raise _problem("MIGRATION_RECORD_INVALID", "historical tick number differs", row_id=key)
        elif isinstance(value, _WorkCompleteProof):
            snapshot(value.final)
            for observed in value.ticks:
                tick(observed)
        history[key] = value
    for name, proof_name in (("root-start", "root-start"), ("capacity-lease", "capacity-lease"), ("progress", "work-complete")):
        key = control_key("c1-operator-attempt", name)
        if key not in inventory.rows:
            continue
        value = _parsed(_row(inventory, key).payload["data"], _OperatorAttempt)
        request = ControlRequest(request_id=key, request_digest=digest([config.sha256, value.action, name]))
        if (value.configuration_sha256 != config.sha256 or value.action not in {name, "lifecycle"}
                or _receipt(inventory, request) is None
                or control_key("c1-operator-proof", proof_name) not in history):
            raise _problem("MIGRATION_RECORD_INVALID", "historical operator action is unresolved", row_id=key)
        history[key] = value
    for domain, model in (("c1-renewal-worker-cycle", _WorkerRenewal), ("c1-renewal-inbox", _InboxRenewal),
                          ("c1-renewal-observation", _RuntimeObservation)):
        key = control_key(domain, "r1")
        if key not in inventory.rows:
            continue
        data = _row(inventory, key).payload["data"]
        value = _parsed(data, model)
        if (value.configuration_sha256 != config.sha256
                or _receipt(inventory, ControlRequest(request_id=key, request_digest=digest(data))) is None):
            raise _problem("MIGRATION_RECORD_INVALID", "historical renewal identity differs", row_id=key)
        if isinstance(value, _RuntimeObservation):
            utc(value.observed_at)
            if (value.pool.workspace_id != config.workers.workspace_id
                    or len(value.sessions) != len(config.targets)
                    or {digest(entry.target.model_dump(mode="json")) for entry in value.sessions}
                    != {digest(target.model_dump(mode="json")) for target in config.targets}
                    or any(entry.binding not in {binding.binding_id for binding in config.bindings}
                           for entry in value.closed)
                    or any(entry.session.agent_session_id != entry.target.session_id
                           or entry.session.version_indicator.agent_version != entry.target.agent_version
                           or entry.version != entry.target.agent_version for entry in value.sessions)):
                raise _problem("MIGRATION_RECORD_INVALID", "historical runtime observation target differs", row_id=key)
        history[key] = value
    for binding in config.bindings:
        key = control_key("c1-operation-case-proof", binding.binding_id)
        if key not in inventory.rows:
            continue
        data = _row(inventory, key).payload["data"]
        value = _parsed(data, _OperationCaseProof)
        fault = {"revoked": None, "lost-receipt": "caller-side after actual Table receiver commit",
                 "mutable-alias": "controller-side valid alias substitution"}[value.case]
        expected_keys = {control_key("delivery-work-state", binding.obligation_id),
                         control_key("budget", binding.owner.root_budget_id), control_key("grant", binding.grant_id)}
        if (value.binding_id != binding.binding_id or value.operation_id != binding.operation_id
                or value.known_effects != (() if value.case == "revoked" else (binding.operation_id,))
                or value.fault_injection != fault or value.delivery.binding.obligation_id != binding.obligation_id
                or value.delivery.binding.route != binding.route
                or set(value.before_sha256) != expected_keys or set(value.after_sha256) != expected_keys
                or _receipt(inventory, ControlRequest(request_id=key, request_digest=digest(data))) is None):
            raise _problem("MIGRATION_RECORD_INVALID", "historical operation closeout binding differs", row_id=key)
        history[key] = value
    return history


def _operator_history(inventory, config):
    """Retain exact historical audits; none is evidence of current executor quiescence."""
    history = _operator_proofs(inventory, config)
    for target in config.targets:
        attempt_key = control_key("c1-session-stop-attempt", target.session_id)
        observed_key = control_key("c1-session-stop-observation", target.session_id)
        if attempt_key not in inventory.rows and observed_key not in inventory.rows:
            continue
        attempt_data = _row(inventory, attempt_key).payload["data"]
        observed_data = _row(inventory, observed_key).payload["data"]
        attempt = _parsed(attempt_data, _SessionStopAttempt)
        observed = _parsed(observed_data, _SessionStopObservation)
        if (attempt.session_id != target.session_id or attempt.agent_name != target.agent_name
                or observed.session != target.session_id or observed.agent != target.agent_name
                or observed.metadata.agent_session_id != target.session_id
                or observed.metadata.version_indicator.agent_version != target.agent_version
                or isinstance(observed.provider_stop_response, _RetainedSessionStopResponse)
                and observed.provider_stop_response.session_id != target.session_id):
            raise _problem("MIGRATION_RECORD_INVALID", "historical session stop target differs", row_id=observed_key)
        for key, request_digest in (
            (attempt_key, digest(target.model_dump(mode="json"))), (observed_key, digest(observed_data)),
        ):
            if _receipt(inventory, ControlRequest(request_id=key, request_digest=request_digest)) is None:
                raise _problem("MIGRATION_RECORD_INVALID", "historical session stop request differs", row_id=key)
        history[attempt_key], history[observed_key] = attempt, observed
    closeout_key = control_key("c1-final-service-closeout", "r1")
    if closeout_key in inventory.rows:
        data = _row(inventory, closeout_key).payload["data"]
        value = _parsed(data, _ServiceCloseout)
        root_jobs = {binding.job_id for binding in config.bindings}
        session_ids = {target.session_id for target in config.targets}
        if (value.configuration_sha256 != config.sha256
                or len({job.job_id for job in value.jobs}) != len(value.jobs)
                or not {job.job_id for job in value.jobs} <= root_jobs
                or value.budget.root_budget_id not in {root.root_budget_id for root in config.roots}
                or len(value.sessions) != len(session_ids)
                or {session.session for session in value.sessions} != session_ids
                or any(history.get(control_key("c1-session-stop-observation", session.session)) != session
                       for session in value.sessions)
                or _receipt(inventory, ControlRequest(request_id=closeout_key, request_digest=digest(data))) is None):
            raise _problem("MIGRATION_RECORD_INVALID", "historical service closeout binding differs", row_id=closeout_key)
        utc(value.observed_at)
        history[closeout_key] = value
    return history


def _quiescence(inventory, config):
    """Every row is typed/classified; unknown schemas and unresolved obligations deny."""
    from .behavior_recovery_history import validate_recovery_history
    from .memory_note_proposals import NoteProposalRecord
    from .memory_candidates import MemoryCandidateRecord
    from .memory_note_synthesis import NoteSynthesisRecord
    from .behavior_delegation import DelegationState, validate_delegation_history

    static = _origin_probe(inventory, config)
    static.update((installation_record(config)[0], *protection_keys(config)))
    coverage = set()
    for receipt in inventory.receipts.values():
        if receipt.payload["guard_revision"] > inventory.guard.payload["revision"]:
            raise _problem("MIGRATION_RECORD_INVALID", "receipt is newer than the authority guard")
        coverage.update(tuple(item) for item in receipt.payload["writes"])
        if any(key not in inventory.rows for key, _ in receipt.payload["writes"]):
            raise _problem("MIGRATION_UNVERIFIED_ROW", "a retained receipt refers to a missing authority row")
    for key in inventory.rows:
        if (key, inventory.entity_sha256["behavior:v1:row:" + key]) not in coverage:
            raise _problem("MIGRATION_UNVERIFIED_ROW", "current row lacks its exact immutable write receipt", row_id=key)
    worker_digest = digest({"contract": config.workers.model_dump(mode="json"), "mode": "native"})
    delivery_digest = digest({"contract": config.delivery.model_dump(mode="json"), "mode": "native"})
    kinds = _kind_models()
    classified, counts = _operator_history(inventory, config), {}
    classified.update(validate_recovery_history(inventory, config))
    delegations = validate_delegation_history(inventory, config, config.bindings)
    if any(isinstance(value, DelegationState) and value.status in {"unknown", "running"}
           for value in delegations.values()):
        raise _problem("MIGRATION_NOT_QUIESCENT", "an original SDK child is still unresolved")
    classified.update(delegations)
    from .behavior_agent_message_history import validate_agent_message_history
    messages = validate_agent_message_history(inventory, config)
    from .behavior_agent_messages import _MessageState
    if any(isinstance(value, _MessageState) and value.status not in {"silenced", "completed"}
           for value in messages.values()):
        raise _problem("MIGRATION_NOT_QUIESCENT", "an original peer message is still pending or unresolved")
    classified.update(messages)
    from .behavior_agent_registry import validate_agent_registry_history
    classified.update(validate_agent_registry_history(inventory, config))
    from .behavior_team_registry import validate_team_registry_history
    classified.update(validate_team_registry_history(inventory, config))
    from .behavior_turn_receipts import validate_turn_receipt_history
    classified.update(validate_turn_receipt_history(inventory, config))
    from .behavior_team_authority import validate_team_authority_history
    classified.update(validate_team_authority_history(inventory, config))
    from .native_hitl import validate_native_hitl_history
    classified.update(validate_native_hitl_history(inventory, config))
    from .team_mcp import validate_team_mcp_history
    classified.update(validate_team_mcp_history(inventory, config))
    from .native_routines import validate_native_routine_history
    classified.update(validate_native_routine_history(inventory, config))
    jobs = {job.job_id: (binding, job) for binding, job in config.registered_jobs}
    works = {job.work.work_id: job.job_id for _, job in config.registered_jobs}
    for binding in config.bindings:
        for key, model in (
            (control_key("owner", binding.owner.owner_record_id), operations.OwnerRecord),
            (control_key("policy", binding.owner.policy_id), operations.PolicyRecord),
            (runtime_key("policy", binding.binding_id), RuntimePolicy),
            (runtime_key("plan-index", binding.binding_id), _PlanIndex),
        ):
            classified[key] = _parsed(_row(inventory, key).payload["data"], model)
        if classified[runtime_key("policy", binding.binding_id)].model_dump(mode="json") != next(
                entry.policy.model_dump(mode="json") for entry in config.policies if entry.binding_id == binding.binding_id):
            raise _problem("MIGRATION_POLICY_CHANGED", "current runtime policy differs from the approved config")
    for root in config.roots:
        for key, model in (
            (control_key("budget", root.root_budget_id), operations.BudgetRecord),
            (control_key("delivery-index", root.root_budget_id), delivery.PendingIndexRecord),
            (control_key("delivery-quota", "root", root.root_budget_id), delivery.QuotaRecord),
        ):
            classified[key] = _parsed(_row(inventory, key).payload["data"], model)
    for kind, identity in (("team", config.identity.team_id), ("entry", config.delivery.entry_id)):
        key = control_key("delivery-quota", kind, identity)
        classified[key] = _parsed(_row(inventory, key).payload["data"], delivery.QuotaRecord)
    pool_key = workers.worker_key("pool", config.workers.workspace_id)
    classified[pool_key] = _parsed(_row(inventory, pool_key).payload["data"], workers._Pool)
    no_kind = (
        operations.HumanOperationRejection, operations.HumanOperationRejectionIntent,
        SourceEvent, SourceHead, SourceSnapshot, _Preparation, _SourceWithdrawal,
        source_backup._UploadIntent, source_backup._Completion,
        memory.ReviewedClaim, memory.SubjectState, memory.ClaimRevocation, memory.ProjectionRecord,
        source_reviews._DecisionRecord, delivery.VerifiedIngress, delivery.VerifiedWorkResult, inbox.VerifiedInboxAccess,
        delivery.VerifiedDeliveryReceipt, workers.ExecutorEvidence,
        operations.VerifiedOutcome, dispatch.SafeArtifactResult, HostedReportBody,
        _NativePostIntent, _WorkerAdmission,
        NoteProposalRecord, MemoryCandidateRecord, NoteSynthesisRecord,
    )
    cursor_keys = set()
    for key, row in inventory.rows.items():
        if key in static or key in classified:
            continue
        data = row.payload["data"]
        kind = data.get("kind")
        if isinstance(kind, str) and kind in kinds:
            value = _parsed(data, kinds[kind])
        elif {"binding_id", "binding_digest", "input_digest", "request_id", "context", "data", "request", "kind"} <= data.keys():
            value = _parsed(data, CommandPlan)
        elif data.get("schema_version") == "fa-bootstrap-authorization-v1":
            if (data.get("configuration_sha256") != config.sha256
                    or data.get("authorizing_human_oid") != config.identity.human_oid
                    or key != control_key("server-bootstrap-authorization", data.get("decision_id", ""))):
                raise _problem("MIGRATION_RECORD_INVALID", "bootstrap history identity differs")
            static.add(key)
            continue
        elif set(data) == {"sequence"} and type(data["sequence"]) is int and data["sequence"] >= 0:
            continue
        else:
            matches = []
            for model in no_kind:
                try:
                    matches.append(model.model_validate_json(canonical(data)))
                except (ValueError, TypeError, ValidationError):
                    continue
            if len(matches) != 1:
                raise _problem("MIGRATION_UNCLASSIFIED_RECORD", "no unique safe record classification",
                               row_id=key, payload_sha256=inventory.entity_sha256["behavior:v1:row:" + key])
            value = matches[0]
        classified[key] = value
        if isinstance(value, (SourceEvent, SourceHead)):
            cursor_keys.add(control_key("source-cursor", value.reference.scope.key))
    for key, row in inventory.rows.items():
        if key not in static and key not in classified and key not in cursor_keys:
            raise _problem("MIGRATION_UNCLASSIFIED_RECORD", "unbound cursor or metadata row", row_id=key)

    obligations = {value.obligation_id: value for value in classified.values() if isinstance(value, delivery._Obligation)}
    native_launches = {value.launch_id: value for value in classified.values() if isinstance(value, foundry._NativeLaunch)}
    actual_jobs = {value.spec.job_id: value for value in classified.values() if isinstance(value, workers.JobRecord)}
    actual_works = {value.definition.work_id: value for value in classified.values() if isinstance(value, delivery._Work)}
    operations_by_id = {value.operation_id: value for value in classified.values() if isinstance(value, operations._Operation)}
    attempts = [value for value in classified.values() if isinstance(value, operations._Attempt)]
    backup_completions = {value.reference.description.backup_id: value for value in classified.values()
                          if isinstance(value, source_backup._Completion)}
    allowed_actors = {"human-" + config.identity.human_oid, *(agent.actor for agent in config.identity.agents)}
    for binding in config.bindings:
        index = classified[runtime_key("plan-index", binding.binding_id)]
        actual = {key for key, value in classified.items() if isinstance(value, CommandPlan)
                  and value.binding_id == binding.binding_id
                  and key == runtime_key("plan", value.binding_id, value.kind, value.request_id)}
        if set(index.keys) != actual:
            raise _problem("MIGRATION_RECORD_INVALID", "command index does not exactly cover retained plans",
                           binding_id=binding.binding_id)
    for key, value in classified.items():
        name = value.__class__.__name__
        counts[name] = counts.get(name, 0) + 1
        kind = getattr(value, "kind", "")
        configuration_digest = getattr(value, "configuration_digest", None)
        if configuration_digest is not None:
            expected = worker_digest if kind.startswith(("worker_", "native_")) else delivery_digest
            if configuration_digest != expected:
                raise _problem("MIGRATION_RECORD_INVALID", "record is bound to a different installed contract", row_id=key)
        if isinstance(value, workers._Pool):
            if (key != pool_key or value.workspace_id != config.workers.workspace_id
                    or {slot.number for slot in value.slots} != set(range(config.workers.capacity))
                    or len(value.slots) != config.workers.capacity or value.pending or value.completions
                    or any(slot.binding is not None or slot.lease_until is not None for slot in value.slots)):
                raise _problem("MIGRATION_NOT_QUIESCENT", "worker pool still has pending/completion/active slots")
        elif isinstance(value, workers.JobRecord):
            if (value.spec.job_id not in jobs or value.state not in {"succeeded", "failed", "cancelled"}
                    or value.completion_credit != 0):
                raise _problem("MIGRATION_NOT_QUIESCENT", "root/child job is not completely terminal", row_id=key)
            if set(value.children) - jobs.keys():
                raise _problem("MIGRATION_UNCLASSIFIED_RECORD", "job references an unregistered child", row_id=key)
            binding, registered = jobs[value.spec.job_id]
            if value.spec.work_id != registered.work.work_id:
                raise _problem("MIGRATION_RECORD_INVALID", "job/work identity differs", row_id=key)
            if value.spec.job_id == binding.job_id:
                if value.spec.parent_job_id is not None or value.spec.obligation_id != binding.obligation_id or not value.result_forwarded:
                    raise _problem("MIGRATION_NOT_QUIESCENT", "root job delivery has not settled", row_id=key)
            else:
                parent = actual_jobs.get(binding.job_id)
                if (value.spec.parent_job_id != binding.job_id or value.spec.obligation_id is not None
                        or parent is None or value.spec.job_id not in parent.children):
                    raise _problem("MIGRATION_RECORD_INVALID", "registered child/parent linkage differs", row_id=key)
            completion_key = workers.worker_key("completion", value.spec.job_id, str(value.generation))
            completion = classified.get(completion_key)
            if isinstance(completion, workers.ExecutorEvidence):
                if (completion.binding != value.binding or completion.evidence_id != value.last_evidence_id
                        or not completion.entire_tree_idle or not completion.all_inflight_starts_fenced
                        or completion.state not in {"succeeded", "failed", "stopped"}):
                    raise _problem("MIGRATION_NOT_QUIESCENT", "terminal job lacks exact completed execution evidence", row_id=key)
            elif isinstance(completion, workers._InactiveCancellation):
                if (value.state != "cancelled" or not value.cancel_requested
                        or completion.spec != value.spec or completion.owner != value.owner
                        or completion.generation != value.generation or completion.evidence_id != value.last_evidence_id):
                    raise _problem("MIGRATION_NOT_QUIESCENT", "inactive cancellation evidence differs", row_id=key)
            else:
                raise _problem("MIGRATION_NOT_QUIESCENT", "job terminal receipt/evidence is absent", row_id=key)
        elif isinstance(value, delivery._Work):
            if (value.definition.work_id not in works or value.state not in {"succeeded", "failed", "cancelled"}
                    or value.result is None or value.result.unresolved_operations):
                raise _problem("MIGRATION_NOT_QUIESCENT", "work is nonterminal or has unresolved effects", row_id=key)
            if (value.definition_digest != digest(value.definition.model_dump(mode="json"))
                    or value.result.definition_digest != value.definition_digest or value.result.state != value.state):
                raise _problem("MIGRATION_RECORD_INVALID", "terminal work evidence/definition differs", row_id=key)
        elif isinstance(value, delivery._Obligation):
            binding = next((entry for entry in config.bindings if entry.obligation_id == value.obligation_id), None)
            if (binding is None or binding.job_id not in actual_jobs or value.work_id not in actual_works
                    or value.work_id != binding.work.work_id or value.event_id != binding.event_id
                    or actual_works[value.work_id].event_id != value.event_id
                    or value.owner_record_id != binding.owner.owner_record_id or value.route != binding.route):
                raise _problem("MIGRATION_RECORD_INVALID", "obligation is not linked to its registered terminal root", row_id=key)
            if (value.fulfillment != "fulfilled" or value.due_pending or value.automation != "stopped"
                    or "client_received" not in value.confirmed_levels or value.fulfillment_receipt_id is None
                    or value.final_delivery_id is None or value.final_version < 1):
                raise _problem("MIGRATION_DELIVERY_PENDING", "delivery responsibility is not fulfilled", row_id=key)
            proof_key = control_key("delivery-receipt", value.final_delivery_id, value.fulfillment_receipt_id)
            proof = classified.get(proof_key)
            if (not isinstance(proof, _DeliveryReceipt) or proof.evidence.send_outcome != "emitted"
                    or "client_received" not in proof.evidence.confirmed_levels
                    or proof.evidence.binding.obligation_id != value.obligation_id
                    or proof.evidence.binding.delivery_id != value.final_delivery_id
                    or proof.evidence.binding.version != value.final_version
                    or proof.evidence.binding.route != value.route):
                raise _problem("MIGRATION_DELIVERY_PENDING", "final client-received receipt is not exact", row_id=key)
        elif isinstance(value, delivery.PendingIndexRecord):
            if value.entries:
                raise _problem("MIGRATION_DELIVERY_PENDING", "delivery index is not empty", row_id=key)
        elif isinstance(value, delivery.QuotaRecord):
            if value.pending:
                raise _problem("MIGRATION_DELIVERY_PENDING", "pending quota has not been released", row_id=key)
        elif isinstance(value, foundry._TargetReservation):
            if value.target not in config.targets or key != foundry.native_key("target", value.target.executor_id):
                raise _problem("MIGRATION_RECORD_INVALID", "native reservation target is not the installed target", row_id=key)
            if value.active_launch_id is not None:
                raise _problem("MIGRATION_LAUNCH_PENDING", "a native target is still reserved", row_id=key)
        elif isinstance(value, foundry.NativeExecutionGrant):
            registered = jobs.get(value.job_id)
            if (registered is None or value.controller_id != config.controller_id
                    or value.definition != registered[1].work
                    or value.owner.owner_record_id != registered[0].owner.owner_record_id
                    or any(target not in config.targets for target in value.targets)):
                raise _problem("MIGRATION_RECORD_INVALID", "native grant is outside the unchanged job/owner/target configuration", row_id=key)
        elif isinstance(value, foundry._NativeLaunch):
            if value.target not in config.targets:
                raise _problem("MIGRATION_RECORD_INVALID", "native launch target is outside installed configuration", row_id=key)
            if value.phase != "sealed" or value.sealed_evidence_id is None:
                raise _problem("MIGRATION_LAUNCH_PENDING", "unclaimed/claimed native launch is not sealed", row_id=key)
            evidence_key = foundry.native_key("evidence", value.sealed_evidence_id)
            evidence = _parsed(_row(inventory, evidence_key).payload["data"], foundry._NativeEvidence)
            if (value.start.binding.job_id not in actual_jobs
                    or value.api_binding_id != jobs[value.start.binding.job_id][0].binding_id
                    or evidence.launch_id != value.launch_id or evidence.evidence.binding != value.start.binding
                    or evidence.evidence.evidence_id != value.sealed_evidence_id or not evidence.evidence.entire_tree_idle
                    or not evidence.evidence.all_inflight_starts_fenced):
                raise _problem("MIGRATION_LAUNCH_PENDING", "native terminal/fence evidence is incomplete", row_id=key)
        elif isinstance(value, foundry._NativeProbe):
            if value.launch_id not in native_launches or native_launches[value.launch_id].phase != "sealed":
                raise _problem("MIGRATION_LAUNCH_PENDING", "probe belongs to an unclosed launch", row_id=key)
        elif isinstance(value, _NativePostIntent):
            launch = native_launches.get(value.launch_id)
            if (launch is None or launch.phase != "sealed" or value.target != launch.target
                    or value.post_digest != launch.post_digest
                    or key != foundry.native_key("post-intent", value.launch_id)
                    or value.request != _native_post_request(config, launch)
                    or _receipt(inventory, value.request) is None):
                raise _problem("MIGRATION_LAUNCH_PENDING", "post intent lacks its original settled launch/receipt", row_id=key)
        elif isinstance(value, _WorkerAdmission):
            job = actual_jobs.get(value.job_id)
            binding = jobs[value.job_id][0] if value.job_id in jobs else None
            expected_reads = () if binding is None else tuple(sorted({
                pool_key, workers.worker_key("job", value.job_id),
                control_key("owner", binding.owner.owner_record_id), control_key("policy", binding.owner.policy_id),
                control_key("delivery-obligation", binding.obligation_id), control_key("delivery-intent", value.intent_id),
            }))
            if (job is None or binding is None or value.job_id != binding.job_id
                    or job.spec.obligation_id != binding.obligation_id
                    or key != workers.worker_key("admission", binding.obligation_id)
                    or value.intent_id != control_key("delivery-intent-id", binding.event_id)
                    or value.enqueue_request.request_id != workers.worker_key("enqueue", value.job_id)
                    or value.enqueue_request.request_digest != digest([
                        "enqueue", [job.spec.model_dump(mode="json"), job.owner.model_dump(mode="json")], worker_digest,
                    ])
                    or value.enqueue_request.read_set != expected_reads
                    or _receipt(inventory, value.enqueue_request) is None):
                raise _problem("MIGRATION_RECORD_INVALID", "worker admission lacks its original job/intent/receipt", row_id=key)
        elif isinstance(value, _UnclaimedFence):
            launch = native_launches.get(value.launch_id)
            if (launch is None or launch.phase != "sealed" or launch.claim_id is not None
                    or key != foundry.native_key("unclaimed-fence", value.launch_id)
                    or value.original_start_request_id != launch.start.binding.start_request_id):
                raise _problem("MIGRATION_LAUNCH_PENDING", "unclaimed-launch fence is not exact/permanent", row_id=key)
        elif isinstance(value, HostedReportBody):
            probe = classified.get(foundry.native_key("probe", value.probe_id))
            launch = native_launches.get(probe.launch_id) if isinstance(probe, foundry._NativeProbe) else None
            if (launch is None or launch.phase != "sealed" or value.claim_id != launch.claim_id
                    or value.invocation_id != launch.native_invocation_id):
                raise _problem("MIGRATION_LAUNCH_PENDING", "report has no exact sealed launch", row_id=key)
        elif isinstance(value, operations._Operation):
            if value.operation_id not in {binding.operation_id for binding in config.bindings}:
                raise _problem("MIGRATION_UNCLASSIFIED_RECORD", "operation is not registered in this configuration", row_id=key)
            related = [attempt for attempt in attempts if attempt.binding.operation_id == value.operation_id]
            if value.unsettled_attempt_ids or any(attempt.effect_state in {"unknown", "partial"} for attempt in related):
                raise _problem("MIGRATION_OPERATION_UNKNOWN", "operation has unresolved attempts", row_id=key)
            if value.current_attempt_id is not None and value.effect_state in {"unknown", "partial"}:
                raise _problem("MIGRATION_OPERATION_UNKNOWN", "operation outcome remains unresolved", row_id=key)
        elif isinstance(value, operations._Attempt):
            if value.binding.operation_id not in operations_by_id or value.effect_state in {"unknown", "partial"}:
                raise _problem("MIGRATION_OPERATION_UNKNOWN", "orphan/unresolved operation attempt", row_id=key)
        elif isinstance(value, dispatch.DispatchRecord):
            operation = operations_by_id.get(value.binding.operation_id)
            if (operation is None or value.phase != "settled" or value.responsibility != "resolved"
                    or value.binding.attempt_id not in {attempt.binding.attempt_id for attempt in attempts
                                                       if attempt.binding.operation_id == operation.operation_id}):
                raise _problem("MIGRATION_OPERATION_UNKNOWN", "dispatch remains armed/consumed/unknown", row_id=key)
        elif isinstance(value, dispatch.DispatchObligation):
            if value.state != "resolved":
                raise _problem("MIGRATION_OPERATION_UNKNOWN", "dispatch reconciliation remains pending", row_id=key)
        elif isinstance(value, effects._Envelope):
            if _receipt(inventory, value.receiver_request) is None:
                raise _problem("MIGRATION_OPERATION_UNKNOWN", "receiver delivery lacks its exact terminal receipt", row_id=key)
        elif isinstance(value, (SourceEvent, _Preparation)):
            if isinstance(value, _Preparation) or value.protection is not None or value.blob is not None:
                raise _problem("MIGRATION_LEGACY_PROTECTED_SOURCE", "protected large source/preparation requires a separate migration", row_id=key)
            if (key != value.reference.key or value.reference.scope.tenant_id != config.identity.tenant_id
                    or value.reference.scope.team_id != config.identity.team_id
                    or value.reference.source_id not in {source for binding in config.bindings for source in binding.source_ids}
                    or value.source_actor not in allowed_actors
                    or value.reference.scope.visibility == "private" and value.reference.scope.owner_actor != value.source_actor
                    or inventory.rows[key].payload["material"] is None):
                raise _problem("MIGRATION_RECORD_INVALID", "inline source binding/material differs", row_id=key)
            original = _parsed(inventory.rows[key].payload["material"], InlineMaterial)
            if original.sha256 != value.reference.sha256 or len(original.data) != value.reference.byte_length:
                raise _problem("MIGRATION_RECORD_INVALID", "retained inline source bytes differ", row_id=key)
        elif isinstance(value, SourceHead):
            event = classified.get(value.reference.key)
            if (not isinstance(event, SourceEvent) or event.reference != value.reference
                    or event.source_actor != value.source_actor or key != value.reference.head_key):
                raise _problem("MIGRATION_RECORD_INVALID", "source head has no exact retained version", row_id=key)
        elif isinstance(value, source_backup._UploadIntent):
            completion = backup_completions.get(value.description.backup_id)
            if completion is None or completion.reference.description != value.description or _receipt(inventory, completion.request) is None:
                raise _problem("MIGRATION_BACKUP_UNKNOWN", "backup upload has no exact completed reference/receipt", row_id=key)
        elif isinstance(value, source_backup._Completion):
            reference = value.reference
            if (_receipt(inventory, value.request) is None
                    or reference.description.protection != config.backups.protection
                    or reference.description.owner_oid != config.identity.human_oid
                    or reference.description.scope.tenant_id != config.identity.tenant_id
                    or reference.description.scope.team_id != config.identity.team_id
                    or reference.description.storage.tenant_id != config.identity.tenant_id
                    or reference.description.storage.authority_domain != config.table.scope.authority_domain
                    or reference.description.storage.container_url != config.backups.protection.container_url):
                raise _problem("MIGRATION_BACKUP_UNKNOWN", "backup completion is unverified", row_id=key)
        elif isinstance(value, delivery._Due):
            if value.state != "done":
                raise _problem("MIGRATION_DELIVERY_PENDING", "delivery due work remains pending", row_id=key)
        elif isinstance(value, (delivery._Outbox, inbox._Exchange)):
            if value.binding.obligation_id not in obligations or obligations[value.binding.obligation_id].fulfillment != "fulfilled":
                raise _problem("MIGRATION_DELIVERY_PENDING", "message/exchange belongs to unresolved delivery", row_id=key)
        elif isinstance(value, _WorkIdentity):
            work = actual_works.get(value.work_id)
            obligation = obligations.get(value.obligation_id)
            if (work is None or obligation is None or obligation.work_id != value.work_id
                    or value.definition_digest != work.definition_digest
                    or value.state_row_id != control_key("delivery-work-state", value.obligation_id)):
                raise _problem("MIGRATION_RECORD_INVALID", "work identity/obligation/state linkage differs", row_id=key)
        elif isinstance(value, _DeliveryEvent):
            if value.work_id not in actual_works or actual_works[value.work_id].event_id != value.event_id:
                raise _problem("MIGRATION_RECORD_INVALID", "event has no terminal admitted work", row_id=key)
        elif isinstance(value, CommandPlan):
            binding = next((item for item in config.bindings if item.binding_id == value.binding_id), None)
            if binding is None:
                raise _problem("MIGRATION_UNCLASSIFIED_RECORD", "command plan uses unknown binding", row_id=key)
            if (value.principal_oid not in {binding.human_oid, binding.agent_oid}
                    or value.context.owner_record_id != binding.owner.owner_record_id
                    or value.binding_digest != next(entry.policy.binding_digest for entry in config.policies
                                                    if entry.binding_id == binding.binding_id)):
                raise _problem("MIGRATION_RECORD_INVALID", "command principal/owner is outside its binding", row_id=key)
            original_key = runtime_key("plan", value.binding_id, value.kind, value.request_id)
            consume_key = runtime_key("consume", original_key)
            if key not in {original_key, consume_key}:
                raise _problem("MIGRATION_RECORD_INVALID", "command plan identity differs", row_id=key)
            if key == consume_key and classified.get(original_key) != value:
                raise _problem("MIGRATION_RECORD_INVALID", "consumed command has no identical original plan", row_id=key)
            if "source_plan" in value.data:
                plan = _parsed(value.data["source_plan"], SourcePlan)
                if plan.request != value.request or plan.reference.source_id not in binding.source_ids:
                    raise _problem("MIGRATION_RECORD_INVALID", "source command plan request/binding differs", row_id=key)
                if plan.protection is not None:
                    raise _problem("MIGRATION_LEGACY_PROTECTED_SOURCE", "retained source plan references old large-source protection", row_id=key)
            record = inventory.receipts.get(value.request.request_id)
            if record is None:
                raise _problem("MIGRATION_WRITE_OBLIGATION_UNKNOWN", "prepared/consumed command lacks a definitive receipt",
                               row_id=key, request_id=value.request.request_id)
            # An existing DIFFERENT immutable receipt proves this plan can never
            # write its version. Retain it as a conflict; never rewrite/retry it.
            if record.payload["request"] != value.request.model_dump(mode="json"):
                counts["conflicted_command_history"] = counts.get("conflicted_command_history", 0) + 1
    unsubmitted = []
    for job_id, (_, registered) in jobs.items():
        job, work = actual_jobs.get(job_id), actual_works.get(registered.work.work_id)
        if job is None and work is None:
            unsubmitted.append(job_id)
        elif job is None or (job.spec.parent_job_id is None and work is None):
            raise _problem("MIGRATION_REQUIRED_RECORD_MISSING", "partial root/child admission", job_id=job_id)
    return counts, tuple(sorted(unsubmitted))


class ProtectionMigration:
    def __init__(self, store: MigrationStore, old: ServerConfiguration, new: ServerConfiguration,
                 observations: Mapping[str, bytes], *, previous_observations: Mapping[str, bytes],
                 clock: Callable[[], datetime]):
        if not isinstance(store, MigrationStore):
            raise ValueError("the guarded migration adapter is required")
        self.store, self.old, self.new, self.clock = store, old, new, clock
        self.observations = {lane: ProtectionObservation.model_validate_json(observations[lane])
                             for lane in ("sources", "backups")}
        self.observation_hashes = {lane: hashlib.sha256(observations[lane]).hexdigest() for lane in self.observations}
        self.previous_observations = {
            lane: ProtectionObservation.model_validate_json(previous_observations[lane])
            for lane in ("sources", "backups")
        }
        self.previous_observation_hashes = {
            lane: hashlib.sha256(previous_observations[lane]).hexdigest() for lane in self.previous_observations
        }
        before, after = old.model_dump(mode="json"), new.model_dump(mode="json")
        for lane in self.observations:
            before[lane]["protection"]["evidence_sha256"] = after[lane]["protection"]["evidence_sha256"]
        if before != after or old.sha256 == new.sha256 or store._scope != old.table.scope or store._binding != old.workers.control_binding:
            raise _problem("MIGRATION_CONFIG_CHANGE_DENIED", "only exact protection evidence pins may change in the same authority")
        endpoint = urlsplit(old.table.endpoint)
        if (endpoint.scheme, endpoint.netloc, endpoint.path.rstrip("/")) != store._client_url:
            raise _problem("MIGRATION_CONFIG_CHANGE_DENIED", "actual Table endpoint differs from the approved config")
        for lane, previous in self.previous_observations.items():
            if previous.configuration != getattr(old, lane).protection:
                raise _problem("MIGRATION_OBSERVATION_MISMATCH", "historical observation is not the installed old pin")
            # Historical validation checks the original witness, not freshness
            # or permission to execute now; new evidence is checked separately.
            InventoryProtectionVerifier(previous.state, clock=lambda p=previous: p.observed_at).verify(previous.configuration)
            current = self.observations[lane]
            if (previous.blob_service.canonical_payload() != current.blob_service.canonical_payload()
                    or previous.management_policy.canonical_payload() != current.management_policy.canonical_payload()):
                raise _problem("MIGRATION_RETENTION_CHANGED", "this bounded pin update cannot alter native retention/TTL policy")

    def _operator(self, principal):
        if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human"
                or principal.oid != self.old.identity.human_oid or principal.tenant_id != self.old.identity.tenant_id
                or principal.actor_id is not None):
            raise Problem(403, "MIGRATION_OPERATOR_REQUIRED", "configured delegated human operator required")
        principal.require(self.old.identity.team_id, "approve")
        principal.require(self.old.identity.team_id, "read")

    def _fresh(self):
        instant = utc(self.clock())
        until = []
        for lane, observation in self.observations.items():
            if observation.configuration != getattr(self.new, lane).protection:
                raise _problem("MIGRATION_OBSERVATION_MISMATCH", "fresh observation must bind the exact new pin/container")
            account = getattr(self.new, lane).account_resource_id
            if (observation.blob_service.resource_id.lower() != (account + "/blobServices/default").lower()
                    or observation.management_policy.resource_id.lower() != (account + "/managementPolicies/default").lower()):
                raise _problem("MIGRATION_OBSERVATION_MISMATCH", "native observation ARM resource differs")
            if (observation.inventory_coverage is None
                    or set(self.new.evidence.inventory_scopes) != set(observation.inventory_coverage)
                    or len(self.new.evidence.inventory_scopes) != len(observation.inventory_coverage)
                    or observation.inventory_observed_at is None or observation.inventory_valid_until is None
                    or not utc(observation.inventory_observed_at) <= instant < utc(observation.inventory_valid_until)):
                raise _problem("MIGRATION_OBSERVATION_STALE", "complete fresh deletion-consumer inventory required")
            InventoryProtectionVerifier(observation.state, clock=self.clock).verify(observation.configuration)
            observed_times = (observation.observed_at, observation.inventory_observed_at,
                              observation.blob_service.observed_at, observation.management_policy.observed_at)
            if any(instant - utc(value) > timedelta(seconds=self.new.evidence.max_age_seconds) for value in observed_times):
                raise _problem("MIGRATION_OBSERVATION_STALE", "native observation exceeds the installed freshness policy")
            until.extend((observation.valid_until, observation.inventory_valid_until,
                          observation.blob_service.valid_until, observation.management_policy.valid_until))
        return min(utc(value) for value in until)

    def _pointers(self):
        return (installation_record(self.old)[0], *protection_keys(self.old))

    def plan(self, principal, *, migration_id: str) -> MigrationPlan:
        self._operator(principal)
        until = self._fresh()
        inventory = self.store.read_inventory()
        key, expected = installation_record(self.old)
        if _row(inventory, key).payload["data"] != expected:
            raise _problem("MIGRATION_INSTALLATION_CHANGED", "expected old installation is not current")
        if migration_key(self.old.table.scope.authority_domain, "current") in inventory.rows:
            raise _problem("MIGRATION_HISTORY_EXISTS", "this bounded migration does not overwrite prior migration history")
        if inventory.installation_configuration_sha256 is not None:
            raise _problem("MIGRATION_HISTORY_EXISTS", "the authority guard has already advanced to a migration epoch")
        for lane, row_key in zip(("sources", "backups"), protection_keys(self.old)):
            if _row(inventory, row_key).payload["data"] != getattr(self.old, lane).protection.model_dump(mode="json"):
                raise _problem("MIGRATION_INSTALLATION_CHANGED", "expected old protection pin differs")
        counts, unsubmitted = _quiescence(inventory, self.old)
        excluded = {GUARD_KEY, *("behavior:v1:row:" + key for key in self._pointers())}
        preserved = {key: value for key, value in inventory.entity_sha256.items() if key not in excluded}
        groups = {}
        for name, kinds in {
            "budgets": {"operation_budget", "delivery_quota"},
            "owners": {"operation_owner"},
            "revocations": {"operation_grant"},
        }.items():
            groups[name] = digest({key: inventory.entity_sha256["behavior:v1:row:" + key]
                                   for key, row in inventory.rows.items() if row.payload["data"].get("kind") in kinds})
        groups["receipts"] = digest({key: value for key, value in preserved.items() if key.startswith("behavior:v1:receipt:")})
        return MigrationPlan(
            migration_id=migration_id, authority=self.old.table.scope, control_binding=self.store._binding,
            operator_oid=principal.oid, old_configuration_sha256=self.old.sha256, new_configuration_sha256=self.new.sha256,
            old_sources=self.old.sources.protection, old_backups=self.old.backups.protection,
            new_sources=self.new.sources.protection, new_backups=self.new.backups.protection,
            observation_sha256=self.observation_hashes, code_sha256=code_sha256(),
            previous_observation_sha256=self.previous_observation_hashes,
            guard=GuardAnchor(binding=inventory.guard.payload["binding"], revision=inventory.guard.payload["revision"],
                              etag=inventory.guard.etag),
            pointer_etags={key: _row(inventory, key).etag for key in self._pointers()},
            inventory_sha256=digest(dict(inventory.entity_sha256)), preserved_sha256=digest(preserved),
            preserved_groups=groups, quiescence_counts=counts, unsubmitted_jobs=unsubmitted, valid_until=until,
        )

    def authorize(self, principal, plan: MigrationPlan, *, decision_id: str, expires_at: datetime):
        self._operator(principal)
        if (plan.old_configuration_sha256 != self.old.sha256 or plan.new_configuration_sha256 != self.new.sha256
                or plan.code_sha256 != code_sha256() or plan.observation_sha256 != self.observation_hashes
                or plan.previous_observation_sha256 != self.previous_observation_hashes
                or not utc(self.clock()) < utc(expires_at) <= min(plan.valid_until, self._fresh())):
            raise _problem("MIGRATION_AUTHORIZATION_DENIED", "approval must bind the current exact plan/code/fresh evidence window")
        return MigrationAuthorization(
            decision_id=decision_id, operator_oid=principal.oid, plan_sha256=plan.sha256,
            old_configuration_sha256=self.old.sha256, new_configuration_sha256=self.new.sha256,
            code_sha256=plan.code_sha256, observation_sha256=plan.observation_sha256,
            previous_observation_sha256=plan.previous_observation_sha256,
            not_before=utc(self.clock()), expires_at=expires_at,
        )

    def _authorize_apply(self, principal, plan, approval, *, historical=False):
        self._operator(principal)
        if (approval.operator_oid != principal.oid or approval.plan_sha256 != plan.sha256
                or approval.old_configuration_sha256 != self.old.sha256 or approval.new_configuration_sha256 != self.new.sha256
                or approval.observation_sha256 != self.observation_hashes or plan.observation_sha256 != self.observation_hashes
                or approval.previous_observation_sha256 != self.previous_observation_hashes
                or plan.previous_observation_sha256 != self.previous_observation_hashes
                or approval.code_sha256 != plan.code_sha256 or plan.code_sha256 != code_sha256()):
            raise _problem("MIGRATION_AUTHORIZATION_DENIED", "exact operator approval/code/config binding differs")
        if not historical and not approval.not_before <= utc(self.clock()) < min(approval.expires_at, self._fresh(), plan.valid_until):
            raise _problem("MIGRATION_AUTHORIZATION_EXPIRED", "migration approval/evidence window is closed")

    def _request(self, plan, approval):
        domain = self.old.table.scope.authority_domain
        keys = (*self._pointers(), migration_key(domain, "authorization", approval.decision_id),
                migration_key(domain, "history", plan.migration_id), migration_key(domain, "current"))
        return ControlRequest(
            request_id=migration_key(domain, "apply", plan.migration_id),
            request_digest=digest([plan.sha256, approval.model_dump(mode="json")]), read_set=keys,
        )

    def apply(self, principal, plan: MigrationPlan, approval: MigrationAuthorization) -> Record:
        self._authorize_apply(principal, plan, approval)
        fresh_plan = self.plan(principal, migration_id=plan.migration_id)
        if fresh_plan != plan:
            raise _problem("MIGRATION_STATE_CHANGED", "inventory/guard/ETags changed; no update is permitted")
        inventory = self.store.read_inventory()
        if digest(dict(inventory.entity_sha256)) != plan.inventory_sha256 or inventory.guard.etag != plan.guard.etag:
            raise _problem("MIGRATION_STATE_CHANGED", "complete authority changed after approval")
        request = self._request(plan, approval)
        installation, source, backup, auth_key, history_key, current_key = request.read_set
        history = {
            "schema_version": "fa-protection-pin-history-v1", "plan": plan.model_dump(mode="json"),
            "authorization_sha256": digest(approval.model_dump(mode="json")), "applied_at": utc(self.clock()).isoformat(),
            "request": request.model_dump(mode="json"),
        }
        pointer = {
            "schema_version": "fa-protection-pin-current-v1", "configuration_sha256": self.new.sha256,
            "history_key": history_key, "history_sha256": digest(history), "authorization_key": auth_key,
        }

        def decide(rows):
            self._authorize_apply(principal, plan, approval)
            _quiescence(inventory, self.old)
            for key in self._pointers():
                if rows[key] is None or rows[key].etag != plan.pointer_etags[key] or rows[key] != inventory.rows[key]:
                    raise _problem("MIGRATION_STATE_CHANGED", "exact old pointer state/ETag differs")
            if any(rows[key] is not None for key in (auth_key, history_key, current_key)):
                raise _problem("MIGRATION_HISTORY_EXISTS", "migration authorization/history is immutable")
            return ControlChanges(writes=(
                ControlWrite(row_id=installation, mode="replace", data=installation_record(self.new)[1]),
                ControlWrite(row_id=source, mode="replace", data=self.new.sources.protection.model_dump(mode="json")),
                ControlWrite(row_id=backup, mode="replace", data=self.new.backups.protection.model_dump(mode="json")),
                ControlWrite(row_id=auth_key, mode="create", data=approval.model_dump(mode="json")),
                ControlWrite(row_id=history_key, mode="create", data=history),
                ControlWrite(row_id=current_key, mode="create", data=pointer),
            ), result={
                "migration_id": plan.migration_id, "old_configuration_sha256": self.old.sha256,
                "new_configuration_sha256": self.new.sha256, "preserved_sha256": plan.preserved_sha256,
                "preserved_groups": plan.preserved_groups, "history_key": history_key, "activation_performed": False,
            })

        return self.store.transact(
            request, decide, expected_guard=inventory.guard,
            before_send=lambda: self._authorize_apply(principal, plan, approval),
            install_configuration_sha256=self.new.sha256,
        )

    def reconcile(self, principal, plan, approval):
        self._authorize_apply(principal, plan, approval, historical=True)
        return self.store.reconcile(self._request(plan, approval))

    def verify_applied(self, principal, plan, approval):
        """Read-only verification before any subsequent authorized activity."""
        receipt = self.reconcile(principal, plan, approval)
        inventory = self.store.read_inventory()
        if inventory.installation_configuration_sha256 != self.new.sha256:
            raise _problem("MIGRATION_POSTSTATE_CHANGED", "the permanent legacy-writer fence is not installed")
        request = self._request(plan, approval)
        written = {
            key: inventory.entity_sha256.get("behavior:v1:row:" + key)
            for key in request.read_set
        }
        if any(value is None for value in written.values()) or dict(receipt.payload["writes"]) != written:
            raise _problem("MIGRATION_POSTSTATE_CHANGED", "current pins/authorization/history differ from the exact migration receipt")
        excluded = {GUARD_KEY, "behavior:v1:receipt:" + request.request_id,
                    *("behavior:v1:row:" + key for key in request.read_set)}
        preserved = {key: value for key, value in inventory.entity_sha256.items() if key not in excluded}
        if digest(preserved) != plan.preserved_sha256:
            raise _problem("MIGRATION_POSTSTATE_CHANGED", "a pre-existing nonpointer entity changed after migration")
        current = inventory.rows[installation_record(self.new)[0]]
        if current.payload["data"] != installation_record(self.new)[1]:
            raise _problem("MIGRATION_POSTSTATE_CHANGED", "the approved installation is not current")
        pointer = _parsed(inventory.rows[migration_key(self.new.table.scope.authority_domain, "current")].payload["data"],
                          MigrationCurrent)
        return {
            "migration_id": plan.migration_id, "new_configuration_sha256": self.new.sha256,
            "before_preserved_sha256": plan.preserved_sha256, "after_preserved_sha256": digest(preserved),
            "receipt_sha256": digest(receipt.payload), "activation_performed": False,
            "migration_history_key": pointer.history_key, "migration_history_sha256": pointer.history_sha256,
            "code_sha256": plan.code_sha256,
        }


if __name__ == "__main__":
    from .behavior_migration_cli import run

    run()
