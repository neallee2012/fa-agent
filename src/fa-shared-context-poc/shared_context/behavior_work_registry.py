"""Bounded per-policy registration IDs; original registry rows and request bytes stay intact."""

import os
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_operations import _Model, _row, _write, control_key
from .behavior_store import Digest
from .errors import Problem
from .models import Identifier
from .source_materials import _payload_hash

PAGE_SIZE = 64
WRITER_ENV = "FA_WORK_REGISTRY_WRITER"


class WorkRegistryHead(_Model):
    kind: Literal["work_registry_head_v1"] = "work_registry_head_v1"
    index_key: Identifier
    policy_id: Identifier
    policy_sha256: Digest
    legacy_sha256: Digest
    entry_count: Annotated[int, Field(ge=1)]
    page_count: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def count(self):
        if self.page_count != (self.entry_count + PAGE_SIZE - 1) // PAGE_SIZE:
            raise ValueError("registry page count must cover exactly its retained entries")
        return self


class WorkRegistryPage(_Model):
    kind: Literal["work_registry_page_v1"] = "work_registry_page_v1"
    index_key: Identifier
    number: Annotated[int, Field(ge=1)]
    entries: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=PAGE_SIZE)]

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.entries)) != len(self.entries):
            raise ValueError("registry page must retain distinct original admission IDs")
        return self


def index_key(policy):
    from .behavior_work_policy import work_policy_key
    return work_policy_key("registry", *policy.scope_ids)


def head_key(key):
    return control_key("work-registry-head-v1", key)


def page_key(key, number):
    return control_key("work-registry-page-v1", key, str(number))


def writer_enabled():
    value = os.environ.get(WRITER_ENV, "false")
    if value not in {"false", "true"}:
        raise Problem(503, "WORK_REGISTRY_CONFIGURATION", "registry writer must be explicitly true or false")
    return value == "true"


def _require(value):
    if not value:
        raise Problem(409, "WORK_POLICY_REGISTRY_INVALID", "original scoped registry pages or retained order differ")


def _value(rows, key, model, policy):
    _require(rows.get(key) is not None and rows[key].payload["binding"] == policy.control_binding
             and rows[key].payload["row_id"] == key and rows[key].payload["material"] is None)
    return _row(rows, key, model)


def _parts(policy, rows):
    from .behavior_work_policy import WorkRegistry
    key, hkey = index_key(policy), head_key(index_key(policy))
    legacy = _value(rows, key, WorkRegistry, policy)
    _require(legacy.policy_id == policy.policy_id)
    head = None if rows[hkey] is None else _value(rows, hkey, WorkRegistryHead, policy)
    if head is not None:
        _require(head.index_key == key and head.policy_id == policy.policy_id
                 and head.policy_sha256 == policy.sha256
                 and head.legacy_sha256 == _payload_hash(rows[key].payload))
    return legacy, head


def _basis(get_rows, policy, rows=None):
    key, hkey = index_key(policy), head_key(index_key(policy))
    rows = {} if rows is None else dict(rows)
    missing = tuple(name for name in (key, hkey) if name not in rows)
    if missing:
        rows.update(get_rows(missing))
    return _parts(policy, rows)


def count_registry(store, policy, rows=None):
    legacy, head = _basis(store.read_snapshot, policy, rows)
    return len(legacy.entries) + (0 if head is None else head.entry_count)


def _read(get_rows, policy, rows=None):
    from .behavior_work_policy import WorkRegistry
    key = index_key(policy)
    legacy, head = _basis(get_rows, policy, rows)
    if head is None:
        return legacy
    entries = list(legacy.entries)
    # Pages are independent point reads: fetch them together (98 keys per batch), check in page order.
    keys = [page_key(key, number) for number in range(1, head.page_count + 1)]
    pages = {}
    for start in range(0, len(keys), 98):
        pages.update(get_rows(tuple(keys[start:start + 98])))
    for number, pkey in enumerate(keys, 1):
        page = _value(pages, pkey, WorkRegistryPage, policy)
        expected = min(PAGE_SIZE, head.entry_count - (number - 1) * PAGE_SIZE)
        _require(page.index_key == key and page.number == number and len(page.entries) >= expected)
        # A concurrent append may extend the tail after this head was observed.
        # Its immutable prefix still represents this snapshot; the caller's CAS
        # or consistent-read guard fences using it as current authority.
        entries.extend(page.entries[:expected])
    _require(len(set(entries)) == len(entries))
    return WorkRegistry(policy_id=legacy.policy_id, entries=tuple(entries))


def read_registry(store, policy, rows=None):
    return _read(store.read_snapshot, policy, rows)



def transact(store, policy, request, decide, *, append=False):
    key = index_key(policy)
    hkey = head_key(key)
    observed = store.read_snapshot((hkey,))
    head = None if observed[hkey] is None else _value(observed, hkey, WorkRegistryHead, policy)
    extra = append_read_keys(policy, observed) if append else (hkey,)

    def checked(rows):
        if rows[hkey] != observed[hkey]:
            raise Problem(412, "BEHAVIOR_CONFLICT", "scoped registry head changed before the original transaction")
        return decide(rows)

    return store.transact(request, checked, extra_read_set=tuple(extra))


def append_read_keys(policy, rows):
    """Pure keys for one append, derived from the caller's observed head."""
    key = index_key(policy)
    hkey = head_key(key)
    if hkey not in rows:
        raise Problem(422, "WORK_POLICY_REGISTRY_INVALID", "the original registry head must be observed first")
    head = None if rows[hkey] is None else _value(rows, hkey, WorkRegistryHead, policy)
    if head is None and not writer_enabled():
        return (hkey,)
    number = 1 if head is None else head.page_count
    return (hkey, page_key(key, number), page_key(key, number + 1))


def append_writes(policy, rows, admission):
    key, hkey = index_key(policy), head_key(index_key(policy))
    legacy, head = _parts(policy, rows)
    if head is None and not writer_enabled():
        return (_write(key, legacy.model_copy(update={"entries": (*legacy.entries, admission)}), "replace"),)
    if not writer_enabled():
        raise Problem(503, "WORK_REGISTRY_WRITER_DISABLED",
                      "registry pages are retained; enable the compatible writer for new registration")
    number = 1 if head is None else head.page_count
    pkey = page_key(key, number)
    page = None if rows[pkey] is None else _value(rows, pkey, WorkRegistryPage, policy)
    if page is not None:
        _require(head is not None and page.index_key == key and page.number == number
                 and len(page.entries) == (head.entry_count - 1) % PAGE_SIZE + 1)
        if len(page.entries) == PAGE_SIZE:
            number += 1
            pkey = page_key(key, number)
            _require(rows[pkey] is None)
            page = None
    else:
        _require(head is None)
    entries = () if page is None else page.entries
    _require(admission not in legacy.entries and admission not in entries)
    page = WorkRegistryPage(index_key=key, number=number, entries=(*entries, admission))
    head = WorkRegistryHead(index_key=key, policy_id=policy.policy_id, policy_sha256=policy.sha256,
        legacy_sha256=_payload_hash(rows[key].payload), entry_count=1 if head is None else head.entry_count + 1,
        page_count=number)
    return (_write(pkey, page, "create" if rows[pkey] is None else "replace"),
            _write(hkey, head, "create" if rows[hkey] is None else "replace"))

