"""The installation fence around BehaviorStore, and the startup origin check.

The operator migration is gone; this keeps what normal writers still need: pin the
exact installation and physical guard epoch for writes and pure read scopes, read
the complete inventory, and refuse tables holding retired pointers at startup.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import copy
import logging
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

from azure.core.exceptions import AzureError
from pydantic import ValidationError

from . import behavior_operations as operations
from .behavior_server_config import ServerConfiguration
from .behavior_store import (
    GUARD_KEY, AuthorityScope, BehaviorStore, Digest, _Guard, _Receipt, _Row, _REQUEST, _etag, _validated, _execution_owner,
)
from .errors import Problem
from .records import Record
from .source_materials import canonical, control_key

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
        epoch = self._owner._guard_epoch
        if epoch is not None:
            for operation in physical:
                if operation[1]["RowKey"] == GUARD_KEY:
                    logical = _Guard.model_validate_json(operation[1]["Payload"])
                    guard = _MigratedGuard(binding=logical.binding, revision=logical.revision, configuration_sha256=epoch)
                    operation[1]["Payload"] = json.dumps(guard.model_dump(mode="json"), ensure_ascii=False,
                                                         separators=(",", ":")).encode()
        def gate(request):
            self._owner._check_installation()
            if self._owner._installation_fence is not None and self._owner._guard_epoch is not None:
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
        self._verified_fresh_initialization = None
        self._expected_guard = ContextVar("migration_expected_guard", default=None)
        self._before_send = ContextVar("migration_before_send", default=None)
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
                self._protected_rows)

    def transact(self, request, decide, *, expected_guard: Record | None = None,
                 before_send: Callable[[], None] | None = None, extra_read_set=()):
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
        send_token = self._before_send.set(validate if inherited is not None else before_send)
        try:
            return super().transact(request, decide, extra_read_set=extra_read_set)
        finally:
            self._before_send.reset(send_token)
            self._expected_guard.reset(guard_token)

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


def _parsed(value, model):
    try:
        return model.model_validate_json(canonical(value))
    except (ValueError, TypeError, ValidationError) as exc:
        raise _problem("MIGRATION_RECORD_INVALID", "authoritative record does not match its typed schema",
                       record_model=model.__name__) from exc


def _problem(code, message, **details):
    return Problem(409, code, message, **details)


def origin_pointer_keys(domain: str) -> tuple[str, str, str, str, str]:
    """The pointers installed_origin dispatches on, in its order; a fresh installation has none after the first."""
    from .behavior_fresh_installation import fresh_key
    from .behavior_work_policy import work_policy_key
    return (fresh_key(domain), work_policy_key("current", domain), migration_key(domain, "current"),
            control_key("runtime-transition-current", domain), control_key("runtime-rotation-current", domain))


def installed_origin(store: MigrationStore, config: ServerConfiguration) -> ServerConfiguration:
    """A fresh row selects fresh verification; retired pointers refuse; else the approved installation row binds."""
    from .behavior_fresh_installation import installed_fresh_origin
    store._verified_fresh_initialization = None
    initial, current_work_policy, current_key, transition_key, rotation_key = origin_pointer_keys(
        config.table.scope.authority_domain)
    if store.read_snapshot((initial,))[initial] is not None:
        return installed_fresh_origin(store, config)
    if store.read_snapshot((current_work_policy,))[current_work_policy] is not None:
        raise Problem(503, "BEHAVIOR_CONFIGURATION",
                      "this table holds a retired standing-policy installation; only a fresh installation can start")
    key, expected = installation_record(config)
    rows = store.read_snapshot((key, current_key, transition_key, rotation_key))
    if rows[key] is None or rows[key].payload["data"] != expected:
        raise Problem(503, "BEHAVIOR_CONFIGURATION", "operator-approved installation differs")
    if any(rows[retired] is not None for retired in (current_key, transition_key, rotation_key)):
        raise Problem(503, "BEHAVIOR_CONFIGURATION",
                      "this table holds a retired migration or runtime transition; only a fresh installation can start")
    if store._guard_epoch is not None:
        raise Problem(503, "BEHAVIOR_CONFIGURATION", "guarded table has no installation history")
    store.bind_installation(key, config.sha256, protected_rows=(*protection_keys(config), current_key))
    return config
