"""BR-01 atomic control storage; no API, Hosted, queue, or Blob integration.

Bind one trusted authority_domain to one existing TableClient/table/partition.
Every writer of its predicates MUST use this core and advance the same guard.
Changing that mapping or permitting out-of-band writes requires a migration;
this core is not a global configuration registry or an authorization evaluator.

initialize() explicitly creates only a guard entity, never a table. transact()
reads a guard-bracketed snapshot, calls a trusted decision function, then submits
guard + create-only receipt + all writes in ONE EGT. A conflict is returned, not
retried. After an uncertain response, call reconcile(); absence remains unknown.
Returning an existing receipt is historical read-back, not permission to dispatch.
read_snapshot() exposes the same bounded, guard-bracketed predicate read without
creating a receipt or advancing the guard. Missing rows are explicit None.
If an unrelated writer advances the guard while predicates are read, the keys are
re-read and accepted only if all are unchanged (rows are never deleted and every
write replaces the ETag); transact() then CASes its EGT on that later guard.
consistent_read(callback) reuses typed reads only inside one synchronous pure
callback, bracketed by physical guard reads. All validators and clocks still run.
A moved guard is accepted only when the guard binding is unchanged and every
row/receipt/missing key the callback point-read is re-read with the same ETag
and value; views cannot enumerate, so no unread row can matter.
It is not dispatch permission or a transaction; later writers read/CAS normally.
Callbacks must not dispatch, await, spawn work, or write through any adapter.
The decision function MUST be side-effect-free: it only computes control changes.
Callers retain the original request ID/digest before calling this core.

Application ceilings: 48 KiB binary Payload/entity, 32 KiB inline material.
Azure ceilings: 100 operations/EGT, 4 MiB actual serialized multipart body.
The public request hook checks the final body before transport, and rejects a
second SDK request. Inline material and receipts have no delete/GC path; material
cannot be removed or replaced by a control-row update. This does not protect
against Azure lifecycle/admin deletion and is not Blob pin/GC evidence.

Sources: Azure Table entity group transactions; azure-data-tables 12.7.0
TableClient.submit_transaction, raw_request_hook, and retry configuration.
The decision function owns grant/budget/owner logic and compliance of inputs.
No cloud calls are made at import or construction time.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock, get_ident
from types import MappingProxyType
from typing import Annotated, Literal, TypeVar
from urllib.parse import urlsplit

from azure.core import MatchConditions
from azure.core.exceptions import AzureError, HttpResponseError, ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode
from pydantic import ConfigDict, Field, JsonValue, TypeAdapter, ValidationError, model_validator

from .behavior_timing import count_table_reads
from .errors import Problem
from .models import Identifier, StrictModel
from .records import Record
from .storage import MAX_PAYLOAD_BYTES

MAX_BATCH_OPERATIONS = 100
MAX_BATCH_BYTES = 4 * 1024 * 1024
MAX_INLINE_BYTES = 32 * 1024
GUARD_KEY = "behavior:v1:guard"
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
TableName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9]{2,62}$")]
_REQUEST = {
    "retry_total": 0, "retry_connect": 0, "retry_read": 0, "retry_status": 0,
    "retry_to_secondary": False, "logging_enable": False,
}


class _Frozen(StrictModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False,
        ser_json_bytes="base64", val_json_bytes="base64",
    )


class AuthorityScope(_Frozen):
    authority_domain: Identifier
    table_name: TableName
    partition_key: Identifier
    max_operations: Annotated[int, Field(ge=2, le=MAX_BATCH_OPERATIONS)] = MAX_BATCH_OPERATIONS
    max_batch_bytes: Annotated[int, Field(ge=1, le=MAX_BATCH_BYTES)] = MAX_BATCH_BYTES
    max_payload_bytes: Annotated[int, Field(ge=1, le=MAX_PAYLOAD_BYTES)] = MAX_PAYLOAD_BYTES


class InlineMaterial(_Frozen):
    data: Annotated[bytes, Field(max_length=MAX_INLINE_BYTES)]
    sha256: Digest

    @model_validator(mode="after")
    def matches(self):
        if hashlib.sha256(self.data).hexdigest() != self.sha256:
            raise ValueError("inline material digest does not match")
        return self


class ControlRequest(_Frozen):
    request_id: Identifier
    request_digest: Digest
    read_set: Annotated[tuple[Identifier, ...], Field(max_length=98)] = ()

    @model_validator(mode="after")
    def unique_reads(self):
        if len(set(self.read_set)) != len(self.read_set):
            raise ValueError("duplicate predicate row")
        return self


class ControlWrite(_Frozen):
    row_id: Identifier
    mode: Literal["create", "replace"]
    data: dict[str, JsonValue]
    material: InlineMaterial | None = None


class ControlChanges(_Frozen):
    writes: Annotated[tuple[ControlWrite, ...], Field(max_length=98)] = ()
    result: dict[str, JsonValue]

    @model_validator(mode="after")
    def unique_writes(self):
        if len({write.row_id for write in self.writes}) != len(self.writes):
            raise ValueError("duplicate mutation row")
        return self


class _Bound(_Frozen):
    binding: Digest


class _Guard(_Bound):
    schema_version: Literal[1] = 1
    kind: Literal["guard"] = "guard"
    revision: Annotated[int, Field(ge=0, le=2**63 - 1)]


class _Row(_Bound):
    schema_version: Literal[1] = 1
    kind: Literal["row"] = "row"
    row_id: Identifier
    data: dict[str, JsonValue]
    material: InlineMaterial | None = None


class _Receipt(_Bound):
    schema_version: Literal[1] = 1
    kind: Literal["receipt"] = "receipt"
    request: ControlRequest
    writes: tuple[tuple[Identifier, Digest], ...]
    result: dict[str, JsonValue]
    guard_revision: Annotated[int, Field(ge=1, le=2**63 - 1)]


Model = TypeVar("Model", bound=_Frozen)
StoredModel = TypeVar("StoredModel", bound=_Bound)
ReadResult = TypeVar("ReadResult")
Decision = Callable[[Mapping[str, Record | None]], ControlChanges]
_READ_KEYS: TypeAdapter[tuple[str, ...]] = TypeAdapter(Annotated[tuple[Identifier, ...], Field(max_length=98)])


def _execution_owner() -> tuple[int, asyncio.Task | None]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return get_ident(), task


@dataclass
class _ReadView:
    store: BehaviorStore
    owner: tuple[int, asyncio.Task | None]
    max_keys: int | None = 98
    values: dict[str, tuple[type[_Bound], tuple[_Bound, str] | None]] = field(default_factory=dict)
    bytes: int = 0
    verifying: bool = False
    active: bool = True
    failed: bool = False
    row_scoped: bool = False


_READ_VIEW: ContextVar[_ReadView | None] = ContextVar("behavior_consistent_read", default=None)
# Independent point GETs of one bracketed batch; bounded to stay well under Table throttling.
MAX_PARALLEL_READS = 8
_NOT_FETCHED: Mapping[str, Future] = MappingProxyType({})
_READ_POOL: ThreadPoolExecutor | None = None
_READ_POOL_LOCK = Lock()


def _read_pool() -> ThreadPoolExecutor:
    global _READ_POOL
    with _READ_POOL_LOCK:
        if _READ_POOL is None:
            _READ_POOL = ThreadPoolExecutor(max_workers=MAX_PARALLEL_READS, thread_name_prefix="fa-table-read")
        return _READ_POOL


def _json(model: _Frozen) -> bytes:
    try:
        return json.dumps(
            model.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise Problem(422, "BEHAVIOR_INVALID_DATA", "control data must be finite UTF-8 JSON") from exc


def _validated(value: Model, expected: type[Model]) -> Model:
    if not isinstance(value, expected):
        raise Problem(422, "BEHAVIOR_INVALID_DATA", "a validated control model is required")
    try:
        result = expected.model_validate(value.model_dump())
        _json(result)
        return result
    except ValidationError as exc:
        raise Problem(422, "BEHAVIOR_INVALID_DATA", "invalid control model") from exc


def _etag(value: object) -> str:
    if not isinstance(value, str) or not value or value == "*" or len(value) > 1024 or any(
        char in value for char in "\r\n"
    ):
        raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "missing or invalid native ETag")
    return value


def _unknown(request: ControlRequest | None) -> Problem:
    details = {} if request is None else {
        "request_id": request.request_id, "request_digest": request.request_digest,
    }
    return Problem(
        503, "BEHAVIOR_COMMIT_UNKNOWN",
        "commit outcome is unknown; reconcile the original ID without automatic resubmission",
        **details,
    )


class _RequestGate:
    def __init__(self, method: str, origin: tuple[str, str], byte_limit: int | None = None):
        self.method, self.origin, self.byte_limit = method, origin, byte_limit
        self.sent = 0

    def __call__(self, request) -> None:
        if self.sent:
            if self.method == "GET":
                raise Problem(503, "BEHAVIOR_READ_FAILED", "additional storage reads are not allowed")
            raise _unknown(None)
        wire = request.http_request
        url = urlsplit(wire.url)
        if wire.method != self.method or (url.scheme, url.netloc) != self.origin:
            raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "unexpected storage request target")
        if self.byte_limit is not None:
            if not url.path.endswith("/$batch") or not isinstance(wire.body, (str, bytes)):
                raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "expected a serialized Table batch")
            body = wire.body.encode("utf-8") if isinstance(wire.body, str) else wire.body
            if len(body) > self.byte_limit:
                raise Problem(413, "BEHAVIOR_BATCH_TOO_LARGE", "serialized batch exceeds its byte limit")
        self.sent += 1


class BehaviorStore:
    def __init__(self, client: TableClient, scope: AuthorityScope):
        self._scope = _validated(scope, AuthorityScope)
        self._client = client
        endpoint = urlsplit(client.url)
        if endpoint.scheme != "https" or not endpoint.netloc or endpoint.username or endpoint.password:
            raise Problem(422, "BEHAVIOR_INVALID_AUTHORITY", "an HTTPS Table client is required")
        self._origin = (endpoint.scheme, endpoint.netloc)
        self._client_url = (endpoint.scheme, endpoint.netloc, endpoint.path.rstrip("/"))
        self._binding = hashlib.sha256(json.dumps({
            "layout": 1, "endpoint": self._client_url,
            "domain": self._scope.authority_domain,
            "table": self._scope.table_name, "partition": self._scope.partition_key,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self._check_client()

    def _check_client(self) -> None:
        endpoint = urlsplit(self._client.url)
        if self._client.table_name != self._scope.table_name or (
            endpoint.scheme, endpoint.netloc, endpoint.path.rstrip("/")
        ) != self._client_url:
            raise Problem(409, "BEHAVIOR_AUTHORITY_MISMATCH", "Table client does not match its binding")

    def _entity(self, key: str, model: _Frozen) -> dict:
        raw = _json(model)
        if len(raw) > self._scope.max_payload_bytes:
            raise Problem(413, "BEHAVIOR_ENTITY_TOO_LARGE", "control Payload exceeds its byte limit")
        return {"PartitionKey": self._scope.partition_key, "RowKey": key, "Payload": raw}

    def _read_view(self) -> _ReadView | None:
        view = _READ_VIEW.get()
        if view is not None and (not view.active or view.store is not self or view.owner != _execution_owner()):
            view.failed = True
            raise Problem(409, "BEHAVIOR_READ_SCOPE_INVALID", "pure read scope cannot cross store, task or thread")
        return view

    @staticmethod
    def _reject_read_view():
        view = _READ_VIEW.get()
        if view is not None:
            view.failed = True
            raise Problem(409, "BEHAVIOR_READ_SCOPE_INVALID", "pure read callback cannot write, dispatch or nest scopes")

    def _consistent_read_identity(self):
        self._check_client()
        return self._scope, self._binding, self._client_url

    def consistent_read(self, callback: Callable[[], ReadResult], *, max_keys: int | None = 98,
                        row_scoped: bool = True) -> ReadResult:
        """One bounded pure computation, never a cached authorization or retry.

        By default unrelated writers may advance the shared guard; the result is
        returned only if every row/receipt the callback read is still unchanged.
        row_scoped=False keeps the whole-partition guard bracket.
        """
        self._reject_read_view()
        if max_keys is not None and (type(max_keys) is not int or max_keys < 1):
            raise Problem(422, "BEHAVIOR_READ_SCOPE_INVALID", "a positive key bound or byte-bounded collection read is required")
        if type(row_scoped) is not bool:
            raise Problem(422, "BEHAVIOR_READ_SCOPE_INVALID", "row-scoped verification must be explicit")
        view = _ReadView(self, _execution_owner(), max_keys=max_keys, row_scoped=row_scoped)
        token = _READ_VIEW.set(view)
        try:
            before = self._guard()
            identity = self._consistent_read_identity()
            result = callback()
            if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
                if inspect.iscoroutine(result) or inspect.isgenerator(result):
                    result.close()
                raise Problem(422, "BEHAVIOR_READ_SCOPE_INVALID", "pure read callback must finish synchronously")
            result = copy.deepcopy(result)
            view.verifying = True
            after = self._guard()
            if row_scoped and after != before:
                after = self._unchanged_reads(view, before, after)
            if before != after or identity != self._consistent_read_identity():
                raise Problem(412, "BEHAVIOR_CONFLICT", "authority changed during pure read callback")
            if view.failed:
                raise Problem(409, "BEHAVIOR_READ_SCOPE_INVALID", "pure read scope contains a failed operation")
            return result
        finally:
            view.active = False
            view.values.clear()
            _READ_VIEW.reset(token)

    def _unchanged_reads(self, view: _ReadView, before: tuple[_Guard, str],
                         after: tuple[_Guard, str]) -> tuple[_Guard, str]:
        # Views allow point reads only (no enumeration). Rows are never deleted and
        # every write replaces the ETag, so equal re-reads prove every observed row,
        # including absent keys, held together between the callback and this check.
        if after[0].binding != before[0].binding or after[0].revision <= before[0].revision:
            raise Problem(412, "BEHAVIOR_CONFLICT", "authority guard changed during pure read callback")
        rows = [(key, model, prior) for key, (model, prior) in tuple(view.values.items()) if key != GUARD_KEY]
        fetched = self._fetch_many([(key, model) for key, model, _ in rows])
        for key, model, prior in rows:
            if self._physical(key, model, fetched) != prior:
                raise Problem(412, "BEHAVIOR_CONFLICT", "a row read by the pure callback changed")
        return before

    def _fetch_many(self, keys: list[tuple[str, type[StoredModel]]]) -> dict[str, Future]:
        """Start independent point GETs together; each result keeps its own error."""
        if len(keys) < 2:
            return {}
        pool = _read_pool()
        count_table_reads(len(keys))
        return {key: pool.submit(self._read_physical, key, model) for key, model in keys}

    def _physical(self, key: str, model: type[StoredModel], fetched: Mapping[str, Future]):
        # Results are consumed in the original key order, so the first error reported is unchanged.
        started = fetched.get(key)
        if started is None:
            count_table_reads(1)
            return self._read_physical(key, model)
        return started.result()

    def _read(self, key: str, model: type[StoredModel],
              fetched: Mapping[str, Future] = _NOT_FETCHED) -> tuple[StoredModel, str] | None:
        view = self._read_view()
        if view is None:
            return self._physical(key, model, fetched)
        try:
            entry = view.values.get(key)
            if entry is not None:
                prior_model, prior = entry
                if prior_model is not model:
                    raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "pure read key changed its stored type")
                if not view.verifying:
                    return copy.deepcopy(prior)
            elif (key != GUARD_KEY and view.max_keys is not None
                  and sum(k != GUARD_KEY for k in view.values) >= view.max_keys):
                raise Problem(413, "BEHAVIOR_READ_SCOPE_LIMIT", "pure read exceeds its distinct predicate/receipt key bound")
            value = self._physical(key, model, fetched)
            if view.verifying:
                # A row-scoped view receives a moved guard and re-checks its rows instead.
                if entry is None or value != prior and not (view.row_scoped and key == GUARD_KEY):
                    raise Problem(412, "BEHAVIOR_CONFLICT", "physical guard or installation changed during pure read")
            else:
                size = len(key.encode()) + (0 if value is None else len(_json(value[0])))
                if view.bytes + size > self._scope.max_batch_bytes:
                    raise Problem(413, "BEHAVIOR_READ_SCOPE_LIMIT", "pure read exceeds its authority byte bound")
                view.bytes += size
                view.values[key] = model, copy.deepcopy(value)
            return copy.deepcopy(value)
        except BaseException:
            view.failed = True
            raise

    def _read_physical(self, key: str, model: type[StoredModel]) -> tuple[StoredModel, str] | None:
        gate = _RequestGate("GET", self._origin)
        try:
            entity = self._client.get_entity(
                partition_key=self._scope.partition_key, row_key=key,
                raw_request_hook=gate, **_REQUEST,
            )
        except ResourceNotFoundError as exc:
            if getattr(exc, "error_code", None) in {"ResourceNotFound", "EntityNotFound"}:
                return None
            raise Problem(503, "BEHAVIOR_READ_FAILED", "authority storage is unavailable") from exc
        except AzureError as exc:
            raise Problem(503, "BEHAVIOR_READ_FAILED", "authority read failed") from exc
        try:
            if (
                gate.sent != 1 or entity["PartitionKey"] != self._scope.partition_key
                or entity["RowKey"] != key or not isinstance(entity["Payload"], bytes)
                or len(entity["Payload"]) > self._scope.max_payload_bytes
            ):
                raise ValueError("invalid entity envelope")
            value = _validated(model.model_validate_json(entity["Payload"]), model)
            etag = _etag(entity.metadata["etag"])
        except (Problem, ValidationError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "invalid stored control record") from exc
        if value.binding != self._binding:
            raise Problem(409, "BEHAVIOR_AUTHORITY_MISMATCH", "stored authority binding differs")
        return value, etag

    def _guard(self) -> tuple[_Guard, str]:
        guard = self._read(GUARD_KEY, _Guard)
        if guard is None:
            raise Problem(409, "BEHAVIOR_NOT_INITIALIZED", "authority guard is not initialized")
        return guard

    def _submit(self, operations: list[tuple], request: ControlRequest | None,
                *, before_send: Callable[[], None] | None = None) -> list[str]:
        self._reject_read_view()
        if not operations or len(operations) > self._scope.max_operations:
            raise Problem(413, "BEHAVIOR_BATCH_TOO_LARGE", "batch exceeds its operation limit")
        gate = _RequestGate("POST", self._origin, self._scope.max_batch_bytes)
        def checked(request):
            gate(request)
            if before_send is not None:
                before_send()
        try:
            result = self._client.submit_transaction(operations, raw_request_hook=checked, **_REQUEST)
        except Problem as exc:
            if exc.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN":
                raise _unknown(request) from exc
            raise
        except HttpResponseError as exc:
            status = getattr(exc, "status_code", None)
            if status in {409, 412}:
                raise Problem(status, "BEHAVIOR_CONFLICT", "atomic control conflict; reconcile first") from exc
            if status in {400, 401, 403, 404, 413}:
                raise Problem(502, "BEHAVIOR_COMMIT_REJECTED", "Table rejected the atomic batch",
                              upstream_status=status) from exc
            raise _unknown(request) from exc
        except (AzureError, TimeoutError, OSError) as exc:
            raise _unknown(request) from exc
        except (ValueError, TypeError, KeyError) as exc:
            if gate.sent:
                raise _unknown(request) from exc
            raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "batch serialization failed before dispatch") from exc
        if gate.sent != 1 or not isinstance(result, list) or len(result) != len(operations):
            raise _unknown(request)
        try:
            return [_etag(item["etag"]) for item in result]
        except (Problem, TypeError, KeyError) as exc:
            raise _unknown(request) from exc

    def initialize(self) -> Record:
        self._reject_read_view()
        self._check_client()
        prior = self._read(GUARD_KEY, _Guard)
        if prior is not None:
            return Record(prior[0].model_dump(mode="json"), prior[1])
        guard = _Guard(binding=self._binding, revision=0)
        etags = self._submit([("create", self._entity(GUARD_KEY, guard))], None)
        return Record(guard.model_dump(mode="json"), etags[0])

    def require_empty_table(self) -> None:
        """Observe the entire Table, not one partition; this is not an atomic predicate."""
        self._reject_read_view()
        self._check_client()
        pages = 0
        path = self._client_url[2] or "/" + self._scope.table_name

        def gate(request):
            nonlocal pages
            wire = request.http_request
            target = urlsplit(wire.url)
            if (wire.method != "GET" or (target.scheme, target.netloc) != self._origin
                    or target.path.rstrip("()").rstrip("/") != path.rstrip("/")):
                raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "empty-table read escaped its exact Table")
            pages += 1
            if pages > 1024:
                raise Problem(503, "FRESH_TABLE_READ_FAILED", "empty-table read did not finish")

        try:
            entities = self._client.list_entities(
                select=["PartitionKey", "RowKey"], results_per_page=1,
                raw_request_hook=gate, **_REQUEST,
            )
            for _ in entities:
                raise Problem(409, "FRESH_TABLE_NOT_EMPTY", "fresh initialization requires an entirely empty Table")
        except (AzureError, TimeoutError, OSError) as exc:
            raise Problem(503, "FRESH_TABLE_READ_FAILED", "the complete empty-table observation failed") from exc
        if not pages:
            raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "empty-table read did not reach storage")

    def initialize_empty(self, request: ControlRequest, changes: ControlChanges, *,
                         before_send: Callable[[], None]) -> Record:
        """Create guard, receipt and all initial rows in one EGT; never create a Table.

        Every writer must retain the same authority/partition mapping and guard.
        Azure cannot atomically combine a table-wide query with this single-partition
        transaction, so unrelated/out-of-band writers must be excluded by the operator.
        """
        self._reject_read_view()
        self._check_client()
        request = _validated(request, ControlRequest)
        changes = _validated(changes, ControlChanges)
        prior = self._receipt(request)
        if prior is not None:
            return prior
        if request.read_set or any(write.mode != "create" for write in changes.writes):
            raise Problem(422, "FRESH_INITIALIZATION_INVALID", "fresh initialization only creates new rows")
        self.require_empty_table()
        writes, fingerprints = [], []
        for write in changes.writes:
            row = _Row(binding=self._binding, row_id=write.row_id, data=write.data, material=write.material)
            entity = self._entity("behavior:v1:row:" + write.row_id, row)
            writes.append(("create", entity))
            fingerprints.append((write.row_id, hashlib.sha256(entity["Payload"]).hexdigest()))
        receipt = _Receipt(binding=self._binding, request=request, writes=tuple(fingerprints),
                           result=changes.result, guard_revision=1)
        operations = [
            ("create", self._entity(GUARD_KEY, _Guard(binding=self._binding, revision=1))),
            ("create", self._entity("behavior:v1:receipt:" + request.request_id, receipt)),
            *writes,
        ]

        def final():
            self.require_empty_table()
            before_send()

        etags = self._submit(operations, request, before_send=final)
        return Record(receipt.model_dump(mode="json"), etags[1])

    def _receipt(self, request: ControlRequest) -> Record | None:
        prior = self._read("behavior:v1:receipt:" + request.request_id, _Receipt)
        if prior is None:
            return None
        receipt, etag = prior
        if receipt.request != request:
            raise Problem(409, "BEHAVIOR_REQUEST_CONFLICT", "request ID is bound to different input")
        return Record(receipt.model_dump(mode="json"), etag)

    def reconcile(self, request: ControlRequest) -> Record:
        self._check_client()
        request = _validated(request, ControlRequest)
        self._guard()
        receipt = self._receipt(request)
        if receipt is None:
            raise _unknown(request)
        return receipt

    def _read_predicates(
        self, keys: tuple[str, ...], guard: _Guard, guard_etag: str, row_scoped: bool = False,
    ) -> tuple[dict[str, tuple[_Row, str] | None], tuple[_Guard, str]]:
        view = _READ_VIEW.get()
        cached = view.values if view is not None and view.store is self else {}
        rows = {key: "behavior:v1:row:" + key for key in keys}
        # All GETs complete before the closing guard read below, so the bracket is unchanged.
        fetched = self._fetch_many([(row, _Row) for row in rows.values() if row not in cached])
        stored = {key: self._read(row, _Row, fetched) for key, row in rows.items()}
        if any(value is not None and value[0].row_id != key for key, value in stored.items()):
            raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "predicate row identity differs")
        after_guard, after_etag = self._guard()
        # An unrelated writer may advance the guard; accept only if every key read is unchanged.
        moved = guard != after_guard or guard_etag != after_etag
        if moved and row_scoped and after_guard.binding == guard.binding and after_guard.revision > guard.revision:
            again = {} if view is not None else self._fetch_many([(row, _Row) for row in rows.values()])
            moved = not all(self._read(rows[key], _Row, again) == value for key, value in stored.items())
        if moved:
            raise Problem(412, "BEHAVIOR_CONFLICT", "authority changed while reading predicates")
        return stored, (after_guard, after_etag)

    @staticmethod
    def _snapshot_records(stored: Mapping[str, tuple[_Row, str] | None]) -> dict[str, Record | None]:
        return {
            key: None if value is None else Record(value[0].model_dump(mode="json"), value[1])
            for key, value in stored.items()
        }

    def read_record(self, key: str) -> Record | None:
        """One atomic entity read, not a multi-row snapshot or permission to write."""
        try:
            _READ_KEYS.validate_python((key,), strict=True)
        except (ValidationError, ValueError, TypeError) as exc:
            raise Problem(422, "BEHAVIOR_INVALID_DATA", "one validated predicate key is required") from exc
        self._check_client()
        value = self._read("behavior:v1:row:" + key, _Row)
        if value is not None and value[0].row_id != key:
            raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "predicate row identity differs")
        return self._snapshot_records({key: value})[key]

    def read_record_page(self, *, limit=20, continuation=None, modified_since=None):
        """One physical data-row page for discovery; callers must re-read selected records before acting.

        modified_since keeps only rows the Table server last wrote at or after that UTC time (its system
        Timestamp; a partition scan, not a key range). Pass the same value on every continuation page.
        """
        self._reject_read_view()
        if type(limit) is not int or not 1 <= limit <= 20:
            raise Problem(422, "BEHAVIOR_INVALID_DATA", "record discovery requires a bounded page")
        if modified_since is not None and (
                type(modified_since) is not datetime or modified_since.utcoffset() != timedelta(0)):
            raise Problem(422, "BEHAVIOR_INVALID_DATA", "record discovery takes one UTC modification bound")
        self._check_client()
        self._guard()
        request_gate = _RequestGate("GET", self._origin)
        query_path = self._client_url[2] or "/" + self._scope.table_name

        def gate(request):
            request_gate(request)
            if urlsplit(request.http_request.url).path.rstrip("()").rstrip("/") != query_path.rstrip("/"):
                raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "record discovery escaped its original Table")

        query_filter = "PartitionKey eq @partition and RowKey ge @start and RowKey lt @end"
        parameters = {"partition": self._scope.partition_key, "start": "behavior:v1:row:", "end": "behavior:v1:row;"}
        if modified_since is not None:
            query_filter += " and Timestamp ge @since"
            parameters["since"] = modified_since
        rows = []
        try:
            pages = self._client.query_entities(
                query_filter=query_filter, parameters=parameters,
                select=["PartitionKey", "RowKey", "Payload"], results_per_page=limit,
                raw_request_hook=gate, **_REQUEST,
            ).by_page(continuation_token=continuation)
            for entity in next(pages, ()):
                key, raw = entity["RowKey"], entity["Payload"]
                if (len(rows) >= limit or entity["PartitionKey"] != self._scope.partition_key
                        or not isinstance(key, str) or not key.startswith("behavior:v1:row:")
                        or not isinstance(raw, bytes) or len(raw) > self._scope.max_payload_bytes):
                    raise ValueError("invalid discovery row")
                value = _validated(_Row.model_validate_json(raw), _Row)
                identifier = key.removeprefix("behavior:v1:row:")
                if value.row_id != identifier or value.binding != self._binding:
                    raise ValueError("discovery row belongs to another binding")
                rows.append((identifier, Record(value.model_dump(mode="json"), _etag(entity.metadata["etag"]))))
            token = pages.continuation_token
        except AzureError:
            raise Problem(503, "BEHAVIOR_READ_FAILED", "record discovery is unavailable") from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise Problem(502, "BEHAVIOR_PROTOCOL_ERROR", "record discovery returned invalid data") from None
        if request_gate.sent != 1 or token is not None and token == continuation:
            raise Problem(503, "BEHAVIOR_READ_FAILED", "record discovery did not confirm an advancing page")
        self._check_client()
        return tuple(rows), token

    def read_snapshot(self, keys: tuple[str, ...]) -> Mapping[str, Record | None]:
        try:
            keys = _READ_KEYS.validate_python(keys, strict=True)
            if len(set(keys)) != len(keys):
                raise ValueError("duplicate predicate")
        except (ValidationError, ValueError, TypeError) as exc:
            raise Problem(422, "BEHAVIOR_INVALID_DATA", "at most 98 unique validated predicate keys are required") from exc
        self._check_client()
        guard, guard_etag = self._guard()
        return self._snapshot_records(self._read_predicates(keys, guard, guard_etag, True)[0])

    def transact(self, request: ControlRequest, decide: Decision, *, extra_read_set: tuple[str, ...] = ()) -> Record:
        self._reject_read_view()
        self._check_client()
        request = _validated(request, ControlRequest)
        try:
            extra = _READ_KEYS.validate_python(extra_read_set, strict=True)
            reads = _READ_KEYS.validate_python(tuple(dict.fromkeys((*request.read_set, *extra))), strict=True)
        except (ValidationError, ValueError, TypeError):
            raise Problem(422, "BEHAVIOR_INVALID_DATA", "at most 98 unique transaction predicates are required") from None
        guard, guard_etag = self._guard()
        prior = self._receipt(request)
        if prior is not None:
            return prior
        # Layout predicates share the same guard/ETag transaction; the original
        # logical request stays byte-compatible with already saved command plans.
        stored, current = self._read_predicates(reads, guard, guard_etag, True)
        if current != (guard, guard_etag):
            # Unchanged predicates under a later guard: the EGT below CASes on that guard.
            if self._receipt(request) is not None:
                raise Problem(412, "BEHAVIOR_CONFLICT", "authority changed while reading predicates")
            guard, guard_etag = current
        snapshot = self._snapshot_records(stored)
        changes = _validated(decide(copy.deepcopy(snapshot)), ControlChanges)
        if guard.revision == 2**63 - 1:
            raise Problem(409, "BEHAVIOR_REVISION_EXHAUSTED", "authority revision is exhausted")
        updated_guard = _Guard(binding=self._binding, revision=guard.revision + 1)
        writes, fingerprints = [], []
        for write in changes.writes:
            old = stored.get(write.row_id)
            if write.mode == "replace":
                if old is None:
                    raise Problem(409, "BEHAVIOR_MISSING_PREDICATE", "replace requires a read-set record")
                if old[0].material is not None and old[0].material != write.material:
                    raise Problem(409, "BEHAVIOR_MATERIAL_RETAINED", "inline material cannot be removed or changed")
            elif old is not None:
                raise Problem(409, "BEHAVIOR_ROW_EXISTS", "create-only row already exists")
            row = _Row(binding=self._binding, row_id=write.row_id, data=write.data, material=write.material)
            entity = self._entity("behavior:v1:row:" + write.row_id, row)
            fingerprints.append((write.row_id, hashlib.sha256(entity["Payload"]).hexdigest()))
            if write.mode == "create":
                writes.append(("create", entity))
            else:
                writes.append(("update", entity, {
                    "mode": UpdateMode.REPLACE, "etag": old[1],
                    "match_condition": MatchConditions.IfNotModified,
                }))
        receipt = _Receipt(
            binding=self._binding, request=request, writes=tuple(fingerprints),
            result=changes.result, guard_revision=updated_guard.revision,
        )
        operations = [
            ("update", self._entity(GUARD_KEY, updated_guard), {
                "mode": UpdateMode.REPLACE, "etag": guard_etag,
                "match_condition": MatchConditions.IfNotModified,
            }),
            ("create", self._entity("behavior:v1:receipt:" + request.request_id, receipt)),
            *writes,
        ]
        etags = self._submit(operations, request)
        return Record(receipt.model_dump(mode="json"), etags[1])
