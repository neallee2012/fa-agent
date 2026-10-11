"""Bounded pending members over an unchanged legacy index and original receipts."""

import os
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_delivery import PendingEntry, PendingIndexRecord, _Bound, _update
from .behavior_operations import control_key
from .errors import Problem
from .models import Identifier

PAGE_SIZE = 64
WRITER_ENV = "FA_DELIVERY_PENDING_INDEX_WRITER"


class PendingHead(_Bound):
    kind: Literal["delivery_pending_head_v1"] = "delivery_pending_head_v1"
    root_budget_id: Identifier
    revision: Annotated[int, Field(ge=0)]
    page_count: Annotated[int, Field(ge=0)] = 0
    pending_count: Annotated[int, Field(ge=0)]


class PendingPage(_Bound):
    kind: Literal["delivery_pending_page_v1"] = "delivery_pending_page_v1"
    root_budget_id: Identifier
    number: Annotated[int, Field(ge=1)]
    obligation_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=PAGE_SIZE)]

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.obligation_ids)) != len(self.obligation_ids):
            raise ValueError("pending page contains duplicate obligations")
        return self


class PendingMember(_Bound):
    kind: Literal["delivery_pending_member_v1"] = "delivery_pending_member_v1"
    root_budget_id: Identifier
    obligation_id: Identifier
    page_number: Annotated[int, Field(ge=1)]
    entry: PendingEntry | None

    @model_validator(mode="after")
    def original(self):
        if self.entry is not None and (
                self.entry.obligation_id != self.obligation_id or not self.entry.instruction):
            raise ValueError("pending member requires its original instruction obligation")
        return self


def head_key(root):
    return control_key("delivery-pending-head-v1", root)


def page_key(root, number):
    return control_key("delivery-pending-page-v1", root, str(number))


def member_key(root, obligation):
    return control_key("delivery-pending-member-v1", root, obligation)


def writer_enabled():
    value = os.environ.get(WRITER_ENV, "false")
    if value not in {"false", "true"}:
        raise Problem(503, "DELIVERY_PENDING_CONFIGURATION", "pending writer must be explicitly true or false")
    return value == "true"


def _require(value):
    if not value:
        raise Problem(409, "DELIVERY_PENDING_INVALID", "original pending membership or page is incomplete")


def _head(core, rows, root):
    key = head_key(root)
    if key not in rows:
        rows = core.store.read_snapshot((key,))
    if rows[key] is None:
        return None
    value = core._row(rows, key, PendingHead)
    _require(value.root_budget_id == root)
    return value


def _legacy(core, rows, root):
    key = control_key("delivery-index", root)
    value = core._row(rows, key, PendingIndexRecord)
    if value.root_budget_id != root:
        raise Problem(409, "DELIVERY_SCOPE_DENIED", "pending index scope differs")
    return value


def counts(core, rows, root):
    head = _head(core, rows, root)
    legacy = _legacy(core, rows, root)
    return (len(legacy.entries) + (0 if head is None else head.pending_count),
            sum(not item.instruction for item in legacy.entries))


def read(core, rows, root):
    head = _head(core, rows, root)
    legacy = _legacy(core, rows, root)
    if head is None:
        return legacy
    result = {item.obligation_id: item for item in legacy.entries}
    locations = {}
    for number in range(1, head.page_count + 1):
        key = page_key(root, number)
        page = core._row(core.store.read_snapshot((key,)), key, PendingPage)
        _require(page.root_budget_id == root and page.number == number
                 and (number == head.page_count or len(page.obligation_ids) == PAGE_SIZE))
        for identifier in page.obligation_ids:
            _require(identifier not in locations and identifier not in result)
            locations[identifier] = number
    identifiers = tuple(locations)
    for offset in range(0, len(identifiers), PAGE_SIZE):
        batch = identifiers[offset:offset + PAGE_SIZE]
        members = core.store.read_snapshot(tuple(member_key(root, identifier) for identifier in batch))
        for identifier in batch:
            key = member_key(root, identifier)
            value = core._row(members, key, PendingMember)
            _require(value.root_budget_id == root and value.obligation_id == identifier
                     and value.page_number == locations[identifier])
            if value.entry is None:
                result.pop(identifier, None)
            else:
                result[identifier] = value.entry
    _require(len(result) == len(legacy.entries) + head.pending_count)
    return _update(legacy, revision=legacy.revision + head.revision, entries=tuple(result.values()))


def transact(core, request, decide, root, *, obligation_id=None, admission=False):
    hkey = head_key(root)
    observed = core.store.read_snapshot((hkey,))
    head = _head(core, observed, root)
    writing = head is not None or writer_enabled()
    extra = [hkey]
    if writing and obligation_id is not None:
        extra.append(member_key(root, obligation_id))
        if admission:
            number = 1 if head is None or head.page_count == 0 else head.page_count
            # Read both candidate pages so a full tail can advance atomically.
            extra.extend((page_key(root, number), page_key(root, number + 1)))

    def checked(rows):
        if rows[hkey] != observed[hkey]:
            raise Problem(412, "BEHAVIOR_CONFLICT", "pending index head changed before the original transaction")
        return decide(rows)

    return core.store.transact(request, checked, extra_read_set=tuple(extra))


def writes(core, rows, context, ob, *, admission=False):
    root = context.root_budget_id
    hkey, mkey = head_key(root), member_key(root, ob.obligation_id)
    head = _head(core, rows, root)
    legacy = _legacy(core, rows, root)
    previous = next((item for item in legacy.entries if item.obligation_id == ob.obligation_id), None)
    if previous is not None or ob.deadline is not None or head is None and not writer_enabled():
        present = previous is not None
        if present == admission:
            raise Problem(409, "DELIVERY_INDEX_INVALID", "pending index membership differs")
        entries = tuple(item for item in legacy.entries if item.obligation_id != ob.obligation_id)
        if ob.fulfillment != "fulfilled":
            entries += (_entry(ob),)
        return (core._write(control_key("delivery-index", root),
                            _update(legacy, entries=entries, revision=legacy.revision + 1)),)
    if admission and not writer_enabled():
        raise Problem(503, "DELIVERY_PENDING_WRITER_DISABLED",
                      "the reader-compatible image retains paged pending work; enable its writer for new admission")
    _require(mkey in rows)
    if head is None:
        head = PendingHead(configuration_digest=legacy.configuration_digest, evidence_mode=legacy.evidence_mode,
            root_budget_id=root, revision=0, pending_count=0)
    member = None if rows[mkey] is None else core._row(rows, mkey, PendingMember)
    if member is not None:
        _require(member.root_budget_id == root and member.obligation_id == ob.obligation_id)
        previous = member.entry
        _require(previous is None or (previous.owner_record_id, previous.conversation_id)
                 == (ob.owner_record_id, ob.conversation_id))
    if (previous is not None) == admission or admission and member is not None:
        raise Problem(409, "DELIVERY_INDEX_INVALID", "pending index membership differs")
    entry = None if ob.fulfillment == "fulfilled" else _entry(ob)
    result = []
    number = None if member is None else member.page_number
    if admission:
        number = max(1, head.page_count)
        key = page_key(root, number)
        page = None if rows[key] is None else core._row(rows, key, PendingPage)
        if page is not None:
            _require(page.root_budget_id == root and page.number == number)
            if len(page.obligation_ids) == PAGE_SIZE:
                number += 1
                key = page_key(root, number)
                _require(rows[key] is None)
                page = None
        elif head.page_count:
            _require(False)
        identifiers = () if page is None else page.obligation_ids
        page = PendingPage(configuration_digest=head.configuration_digest, evidence_mode=head.evidence_mode,
            root_budget_id=root, number=number, obligation_ids=(*identifiers, ob.obligation_id))
        result.append(core._write(key, page, "create" if rows[key] is None else "replace"))
    member = PendingMember(configuration_digest=head.configuration_digest, evidence_mode=head.evidence_mode,
        root_budget_id=root, obligation_id=ob.obligation_id, page_number=number, entry=entry)
    result.append(core._write(mkey, member, "create" if rows[mkey] is None else "replace"))
    result.append(core._write(hkey, _update(head, revision=head.revision + 1,
        page_count=max(head.page_count, number or 0),
        pending_count=head.pending_count + int(entry is not None) - int(previous is not None)),
        "create" if rows[hkey] is None else "replace"))
    return tuple(result)


def _entry(ob):
    return PendingEntry(obligation_id=ob.obligation_id, owner_record_id=ob.owner_record_id,
        conversation_id=ob.conversation_id, review_at=ob.review_at, generation=ob.generation,
        automation=ob.automation, due_pending=ob.due_pending, instruction=ob.deadline is None)
