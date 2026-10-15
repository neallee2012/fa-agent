"""ADR-0009 §5 open-RID set: the Agent's non-terminal receipts, so a version follow reads those instead of every receipt.

Lifecycle (ADR-0009 lifecycle table): a RID is added before its admission is settled (``admitting`` cleared), removed
after its receipt is written terminal; a removal that fails leaves a stale entry, which the next follow reads, finds
terminal and removes. The set is complete for one hold of one Hosted session only: ``complete_for`` and
``complete_since`` name the session and the follow commit (a full scan, the one-time backfill, or a scan of a set
complete for its source) that wrote it. Only the current session admits new RIDs and its image adds every one, so the
set stays complete while that session holds the Agent; a follow whose source got the Agent any other way (from an
image before this module, by an abandon) scans every receipt again.

It is a separate ``fa/v1/turns`` item, not a field of the ``agent`` record, and stays one (ADR-0009, 2026-10-14
amendment): images before it decode that record strictly (``extra="forbid"``, M1), and older images skip this key, as
they skip every key that is not ``rid:``. The set lists RIDs only; a follow reads each listed receipt for its status
and ``run_token``. Phase 4a's start fence is one CAS on the ``agent`` record and does not write the set.
"""

import logging
from typing import NamedTuple

from pydantic import Field

from .behavior_wire import Identifier, WireModel
from .errors import Problem

OPEN_KEY = "open-rids"
_ATTEMPTS = 8
log = logging.getLogger(__name__)


class _OpenRids(WireModel):
    # The ADR's "已補齊" marker, scoped to the session it holds for (see the module docstring) and to the follow
    # (environment request ID) that made that session the Agent's: one hold, not every later one of the same SID.
    complete_for: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    complete_since: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    # Sticky: a Responses receipt was admitted at some point (a full scan sets it on any Responses receipt it reads).
    responses: bool = Field(default=False, exclude_if=lambda value: not value)
    # Sorted, each once.
    rids: tuple[Identifier, ...] = ()


class Fetched(NamedTuple):
    """The raw ``open-rids`` item as one read returned it (``item`` None: absent), for ``add`` to compare and swap
    against without reading it again."""
    item: object


async def fetch(runtime):
    from .native_turns import TURNS_STORE

    async with runtime._store(TURNS_STORE) as store:
        return Fetched(await store.get_item(OPEN_KEY))


async def read(runtime):
    from .native_turns import TURNS_STORE, _decode

    async with runtime._store(TURNS_STORE) as store:
        item = await store.get_item(OPEN_KEY)
    return None if item is None else _decode(item.value, _OpenRids)


async def _update(runtime, change, fetched=None):
    """Read, merge and CAS; ``change`` is idempotent, so a lost ACK or a conflict is read again and merged again.
    ``fetched``: an earlier read the first attempt compares against instead of reading (one write when nothing moved)."""
    from .native_turns import TURNS_STORE, _CONFLICT, _UNCERTAIN, _decode, _json

    for _ in range(_ATTEMPTS):
        async with runtime._store(TURNS_STORE) as store:
            try:
                item = await store.get_item(OPEN_KEY) if fetched is None else fetched.item
            except _UNCERTAIN as error:
                # Callers catch only ``Problem``: an unconfirmed read is retried, then becomes the 503 below.
                log.info("FA_NATIVE_OPEN_RIDS_READ_UNCONFIRMED exception_type=%s", type(error).__name__)
                continue
            fetched = None
            value = _OpenRids() if item is None else _decode(item.value, _OpenRids)
            replacement = change(value)
            if replacement == value:
                return value
            try:
                if item is None:
                    await store.create_item(OPEN_KEY, _json(replacement))
                else:
                    await store.set_item(OPEN_KEY, _json(replacement), if_match=item.etag)
                return replacement
            except _CONFLICT:
                continue
            except _UNCERTAIN as error:
                log.info("FA_NATIVE_OPEN_RIDS_REREAD exception_type=%s", type(error).__name__)
                continue
    raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "the open-RID set update is unconfirmed")


def _with(value, **updates):
    from .native_turns import _changed

    return _changed(value, **updates)


async def add(runtime, rids, *, responses=False, fetched=None):
    """Before an admission is settled (and for open receipts a full scan skipped): never hides an open receipt.
    ``fetched``: the item the admission reads already returned, so the common case is one CAS and no read."""
    rids = tuple(rids)

    def change(value):
        missing = {rid for rid in rids if rid not in value.rids}
        if not missing and (value.responses or not responses):
            return value
        return _with(value, rids=tuple(sorted({*value.rids, *missing})), responses=value.responses or responses)

    return await _update(runtime, change, fetched)


async def remove(runtime, rid):
    """After the receipt is terminal. Best effort: a failure keeps a stale entry, which a later follow re-checks."""
    def change(value):
        if rid not in value.rids:
            return value
        return _with(value, rids=tuple(key for key in value.rids if key != rid))

    try:
        await _update(runtime, change)
    except Problem as error:
        log.warning("FA_NATIVE_OPEN_RID_KEPT code=%s", error.body["error"]["code"])


async def mark_complete(runtime, session_id, since, *, responses=False):
    """After the follow ``since`` (its environment request ID) made ``session_id`` the Agent's: the final scan found
    every open receipt (they are in the set, added at admission or by the scan), and from here only ``session_id``
    admits. Best effort: without the marker the next follow scans every receipt again."""
    def change(value):
        if (value.complete_for, value.complete_since) == (session_id, since) and (value.responses or not responses):
            return value
        return _with(value, complete_for=session_id, complete_since=since, responses=value.responses or responses)

    try:
        await _update(runtime, change)
    except Problem as error:
        log.warning("FA_NATIVE_OPEN_RIDS_UNMARKED code=%s", error.body["error"]["code"])


async def unmark(runtime, session_id):
    """Never trust the marker for ``session_id`` again (its next follow scans every receipt). Raises when it can not
    be cleared."""
    def change(value):
        return value if value.complete_for != session_id else _with(value, complete_for=None, complete_since=None)

    await _update(runtime, change)


async def check_hold(runtime, session_id, since):
    """At a follow's start, while the Agent record still holds the source's own environment (``since``: the request
    that made ``session_id`` the Agent's, None without one): a marker an earlier hold of the same SID wrote is cleared.
    That SID can get the Agent back by an abandon (a generation-0 SID the binding row no longer lists as previous),
    which scans nothing, and an image without the set may have admitted work in between. Raises when unconfirmed;
    the follow has written nothing yet and is sent again."""
    def change(value):
        if value.complete_for != session_id or value.complete_since == since:
            return value
        log.info("FA_NATIVE_OPEN_RIDS_HOLD_CHANGED")
        return _with(value, complete_for=None, complete_since=None)

    await _update(runtime, change)
