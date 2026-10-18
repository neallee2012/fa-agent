"""ADR-0009 §7a "(r)(s) 的競爭" and M9: a receipt an earlier SID accepted and left unfinished when the Agent switched.

The new session only numbers a turn of the conversation (r) or gives it the Responses slot (s) once the left receipt
can no longer complete, so the conversation never forks, repeats work or runs two turns at once:

- ``accepted`` (never started) on any earlier SID, retired or not: adopted (M9, Neal 2026-10-13; on a retired SID too,
  review round 3: the platform can not run it there, so adopting it only gains). One CAS on its ``rid:`` item makes it
  this session's (``hosted_session_id``), keeps its RID and request bytes and records ``adopted_from``; it then runs
  here exactly once, before newer messages of its conversation. The changed ETag rejects the source's own late writes
  (v28 ``_cas`` sends its ``started`` / ``_save_turn`` write with the accepted ETag first; the store refuses it).
  ``adopted_from`` is a field images before it do not know (``extra="forbid"``), so every *read* of the item there fails
  (``NATIVE_STATE_INVALID``): the 0.1 s ``_observe_control`` read, the re-read after the slot and the read-back after a
  refused write. A v28 turn parked in its execution slot ends at its next 0.1 s receipt read without touching it. This
  image checks the SID.
- terminal, or ``started`` on a retired SID (the platform can not run it): go on now;
- ``started`` on an idle source: wait behind it. Only what changes in the shared store counts as progress: the left
  receipt's ETag and its public progress snapshot's ETag (``progress:<sha256(RID)>``). The observation is persisted in
  ``stall:<sha256(RID)>`` with the Hosted wall clock, so a VM restart does not start the wait again. Unchanged for
  ``STALL_SECONDS`` (N, default 10 minutes): the receipt is first written ``unknown`` by CAS, then the new work goes on.
  A failed or unconfirmed CAS is never passed to the owner: the receipt is read again; terminal goes on, still
  ``started`` counts as a change (observe again, keep waiting).

Its cards wait only in the source's memory, so once a left turn can no longer complete (started on a retired source, or
marked here) its cards close as at a normal turn end (``close_cards``, ADR-0009 gate 6).

A message that has to wait never holds the Agent's chain (other conversations go on) and is never refused: it joins
its conversation's ``left-queue`` (conversation store) and a ``fa-native-left-wait-v1`` task of its own looks again
every ``POLL_SECONDS``, driving the stall rule above without any poll from outside; that task's own SDK timeout hands it
over to a successor task and never ends the message (review round 5). Only the queue's first RID goes on,
so older messages are served first whoever sent them. A Responses message then holds the slot (``needs_response``);
Hosted can not send a Responses request to itself, so its wait task asks the API to send it (``on_slot_taken``, the
Agent's own identity, review round 3) and keeps looking until it started, at most once per ``RESEND_SECONDS``: a
caller that stopped polling (the Teams relay stops at 110 s) still gets it sent, once. An adopted Responses slot
holder gets the same wait task.

A late completion of a receipt marked here is fenced by the receipt itself (``run_token``/ETag): the source reads its
receipt every second (v28: every 0.1 s) and saves only by CAS on the original ETag (``_observe_control``,
``_execute_started``, ``_save_turn``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import logging
import time
from typing import Annotated, Literal

from pydantic import Field

from .behavior_wire import Identifier, WireModel
from .errors import Problem
from .native_responses_wire import NATIVE_RESPONSES_PROFILE

log = logging.getLogger(__name__)
# N: how long a left ``started`` receipt and its progress snapshot may stay unchanged before it counts as lost.
STALL_SECONDS = 600
# How often a waiting message looks at its conversation again (in its own wait task, never in the Agent's chain).
POLL_SECONDS = 5
# How long a waiting Responses message that holds its slot waits for its request to arrive before asking the API again.
RESEND_SECONDS = 30
# The SDK timeout of one wait task (the SDK default is 1 day). It is not a limit on how long a message may wait (review
# round 5): a holder that keeps progressing is waited for however long its turn takes, so when the timeout fires (or
# fires at once on a recovered task whose budget already passed) the task starts its successor and ends, and the
# message stays ``accepted`` in its queue. Only the stall rule (N) decides that a holder is lost.
WAIT_TIMEOUT_SECONDS = 3600
# Review round 5: how many requests to send a waiting Responses message one wait task lets fail (404 from an older API,
# 5xx, transport error), at least ``RESEND_SECONDS`` apart, before it ends the message ``unknown``
# (``NATIVE_TURN_TIMEOUT``) with its terminal notice: the owner sees it fail, never a silent ``needs_response``. The
# count is per task run (review round 6): a successor, or the same task recovered after a VM restart, starts again from
# zero. Failing without a break takes about 30 minutes, under one task's hour, so a lasting outage ends the message in
# the task where it began or, when it began late in that task, in its successor: without a VM restart, at most about
# twice ``SEND_ATTEMPTS`` requests in all.
SEND_ATTEMPTS = 60
# Review round 6: the longest backoff between requests a 409 ``NATIVE_SESSION_CHANGED`` answers (``early_delay``).
EARLY_MAX_SECONDS = 300
FAILURE_CODE = "NATIVE_SOURCE_STALLED"
# The API refused to send a waiting Responses message because the owner's work policy is revoked or inactive; the
# message keeps waiting (review round 5), like an owner send under that policy.
REVOKED_CODE = "WORK_POLICY_REVOKED"
_RETIRED = frozenset({"failed", "deleted", "expired", "missing"})
_TERMINAL = frozenset({"completed", "unknown", "busy_refused"})
QUEUE_KEY = "left-queue"
MAX_QUEUED = 64
# The commit-time adoption read stops here; what it misses is adopted when it is read (``observe_left``).
_ADOPT_SCAN_SECONDS = 3
# ``turn_order``: the caller did not read the conversation meta.
_UNREAD = object()


def clock() -> float:
    """The Hosted wall clock (seconds since the epoch); persisted, so it must survive a VM restart."""
    return time.time()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stall_key(rid: str) -> str:
    return "stall:" + _sha(rid)


def retired_key(session_id: str) -> str:
    return "retired:" + _sha(session_id)


class _Stall(WireModel):
    schema_version: Literal["fa-native-left-stall-v1"] = "fa-native-left-stall-v1"
    request_id: Identifier
    receipt_etag: Annotated[str, Field(min_length=1, max_length=256)]
    # None: the left turn wrote no public progress snapshot (best effort); then only the receipt ETag counts.
    progress_etag: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    first_seen: Annotated[float, Field(ge=0)]


class _Retired(WireModel):
    schema_version: Literal["fa-native-retired-source-v1"] = "fa-native-retired-source-v1"
    hosted_session_id: Identifier
    source_status: Literal["failed", "deleted", "expired", "missing"]


class _Queue(WireModel):
    """A conversation's messages waiting behind a left receipt, oldest first (key ``left-queue`` of its store)."""
    schema_version: Literal["fa-native-left-queue-v1"] = "fa-native-left-queue-v1"
    conversation_id: Identifier
    request_ids: Annotated[tuple[Identifier, ...], Field(max_length=MAX_QUEUED)] = ()


@dataclass(frozen=True)
class Order:
    go: bool  # this RID may be numbered / take the slot now
    queued: bool  # it is in its conversation's queue (left there until it starts; later readers drop it)
    behind: str = ""  # what it waits for (part of its wait task's ID)


async def record_retired(runtime, store, session_id: str, source_status: str | None):
    """Written by the abandon switch, before its commit, when the API saw the source retired."""
    if source_status not in _RETIRED:
        return
    await runtime._immutable(store, retired_key(session_id),
                             _Retired(hosted_session_id=session_id, source_status=source_status))


def left(runtime, receipt) -> bool:
    """An unfinished receipt another SID accepted (the left receipt this module is about)."""
    return receipt.status in {"accepted", "started"} and receipt.hosted_session_id != runtime.identity.session_id


async def can_complete(runtime, original) -> bool:
    """Side-effect free: True only for a left ``started`` receipt whose source was not retired."""
    from .native_turns import TURNS_STORE

    receipt = original.value.receipt
    if receipt.status != "started" or original.value.run_token is None:
        return False
    async with runtime._store(TURNS_STORE) as store:
        return await store.get_item(retired_key(receipt.hosted_session_id)) is None


async def close_cards(runtime, receipt):
    """ADR-0009 gate 6: a left turn that can no longer complete closes its cards as a normal turn end does
    (``finish_turn``: ``/end``, then each open approval ``interrupted`` / ``effect_unknown``), so the owner never sees a
    live card whose wait is gone and a routine allow can not write a schedule for it. The IDs are fixed per turn and
    card, so a repeat replays; a recorded decision or answer stays. Best effort, and only in the background
    (``_close_cards_later``, once per RID per process): never delays or changes what the caller decides."""
    from .native_hitl_runtime import NativeHitlRuntime
    from .native_turns import _TASK_TRANSPORT_ERRORS

    client = getattr(runtime.executor, "hitl_client", None)
    if client is None:
        return
    try:
        await NativeHitlRuntime.finish_turn(client, runtime, receipt)
    except (Problem, *_TASK_TRANSPORT_ERRORS) as error:
        log.info("NATIVE_LEFT_RECEIPT_CARDS_UNCONFIRMED rid_prefix=%s exception_type=%s", receipt.request_id[:8],
                 type(error).__name__)


async def settled(runtime, original) -> bool:
    """True when ``original`` (a ``_Versioned`` receipt) can no longer complete; False: wait behind it.

    One pass, never sleeping (the Responses slot runs it on the request path). It may write the ``stall:`` observation
    and, after N unchanged minutes, the receipt's terminal ``unknown``; a left turn found unable to complete then has
    its cards closed (``close_cards``)."""
    from .native_progress import _key as progress_key
    from .native_turns import TURNS_STORE, _UNCERTAIN, _decode, _json

    if not left(runtime, original.value.receipt):
        return True
    if not await can_complete(runtime, original):
        if original.value.receipt.status == "started":  # on a retired source: its wait is gone with the source
            runtime._close_cards_later(original.value.receipt)
        return True
    rid = original.value.receipt.request_id
    async with runtime._store(TURNS_STORE) as store:
        progress = await store.get_item(progress_key(rid))
        found = await store.get_item(stall_key(rid))
        seen = None if found is None else _decode(found.value, _Stall)
        progress_etag = None if progress is None else progress.etag or None
        if seen is None or seen.request_id != rid or (seen.receipt_etag, seen.progress_etag) != (
                original.etag, progress_etag):
            await _observe(store, found, _Stall(request_id=rid, receipt_etag=original.etag,
                                                progress_etag=progress_etag, first_seen=clock()), _json, _UNCERTAIN)
            return False
        if clock() - seen.first_seen < STALL_SECONDS:
            return False
    try:
        await runtime._unknown(original, FAILURE_CODE)
    except Problem as error:
        # Never the owner's error: a changed ETag (e.g. a delegation retained while still started) or an unconfirmed
        # write reads the receipt again below.
        log.info("NATIVE_LEFT_RECEIPT_MARK_UNCONFIRMED code=%s", error.body["error"]["code"])
    current = await runtime._rid(rid)
    if current.value.receipt.status not in {"accepted", "started"}:
        log.warning("NATIVE_LEFT_RECEIPT_SETTLED rid_prefix=%s status=%s failure_code=%s", rid[:8],
                    current.value.receipt.status, current.value.receipt.failure_code)
        # Only our own stall mark: a turn the source completed or ended ``unknown`` itself (a recovery handoff) keeps
        # its recorded allows for the continuation to read.
        if current.value.receipt.status == "unknown" and current.value.receipt.failure_code == FAILURE_CODE:
            # shortcut: a crash between the mark and this close (or a close cut at shutdown, or a transient HITL failure:
            # no retry, the receipt is terminal so no later pass comes back here) leaves the cards open to their TTL, and
            # until then a routine-proposal allow can still write a schedule; upgrade to the durable terminal
            # notification if that gap is ever seen.
            runtime._close_cards_later(current.value.receipt)
        return True
    async with runtime._store(TURNS_STORE) as store:
        found = await store.get_item(stall_key(rid))
        progress = await store.get_item(progress_key(rid))
        await _observe(store, found, _Stall(
            request_id=rid, receipt_etag=current.etag, progress_etag=None if progress is None else progress.etag or None,
            first_seen=clock()), _json, _UNCERTAIN)
    return False


async def _observe(store, found, value, _json, uncertain):
    """Record (or restart) the observation; a lost race means another reader just recorded one: keep waiting."""
    try:
        if found is None:
            await store.create_item(stall_key(value.request_id), _json(value))
        else:
            await store.set_item(stall_key(value.request_id), _json(value), if_match=found.etag)
    except uncertain as error:
        log.info("NATIVE_LEFT_RECEIPT_OBSERVATION_UNCONFIRMED exception_type=%s", type(error).__name__)


async def adopt(runtime, original):
    """M9: make a never-started receipt another SID accepted (retired or not) this session's; returns it adopted
    (still ``accepted``, a new ETag) or None when it is not one, or the source started it first (then it is left
    ``started``). Only while this session holds the Agent; the RID, request and capture stay as they were."""
    from .native_turns import _UnchangedRidWrite, _changed

    receipt = original.value.receipt
    me = runtime.identity.session_id
    if receipt.status != "accepted" or original.value.run_token is not None or receipt.hosted_session_id == me:
        return None
    current, _ = await runtime._agent_record()
    if current.hosted_session_id != me:
        return None
    if await runtime._refused(receipt.request_id, fresh=True):
        # Refused by version (ADR-0009 §9): its owner was told to send it again as a new message; running it here too
        # would do the work twice. It stays as it is and reads final (``_refused_view``).
        return None
    replacement = _changed(original.value, receipt=_changed(receipt, hosted_session_id=me),
                           adopted_from=receipt.hosted_session_id)
    for attempt in range(3):
        try:
            saved, _ = await runtime._cas(original, replacement)
            break
        except _UnchangedRidWrite:
            # Read back unchanged (same ETag and value): the same CAS is safe to send again.
            if attempt == 2:
                raise
    if saved.value.receipt.hosted_session_id != me or saved.value.receipt.status != "accepted":
        return None
    log.warning("NATIVE_LEFT_RECEIPT_ADOPTED rid_prefix=%s source_session_id=%s", receipt.request_id[:8],
                receipt.hosted_session_id)
    return saved


async def _queue(runtime, cid, *, create=False):
    from .native_checkpoint import conversation_store_name
    from .native_turns import _decode

    async with runtime._store(conversation_store_name(cid), create=create) as store:
        item = await store.get_item(QUEUE_KEY)
    return (None, None) if item is None else (_decode(item.value, _Queue), item.etag)


async def _write(runtime, cid, etag, request_ids):
    """One CAS of the queue; False when it lost a race or is unconfirmed (the caller reads it again)."""
    from .native_checkpoint import conversation_store_name
    from .native_turns import _UNCERTAIN, _json

    value = _json(_Queue(conversation_id=cid, request_ids=tuple(request_ids)))
    async with runtime._store(conversation_store_name(cid), create=etag is None) as store:
        try:
            if etag is None:
                await store.create_item(QUEUE_KEY, value)
            else:
                await store.set_item(QUEUE_KEY, value, if_match=etag)
            return True
        except _UNCERTAIN as error:
            log.info("NATIVE_LEFT_QUEUE_WRITE_UNCONFIRMED exception_type=%s", type(error).__name__)
            return False


async def _head(runtime, cid, queue, etag):
    """The queue's first RID still waiting here; RIDs before it that started, ended or belong to another session are
    dropped (one CAS; a lost race leaves them to the next reader)."""
    if queue is None:
        return None
    ids = list(queue.request_ids)
    dropped = False
    while ids:
        try:
            receipt = (await runtime._rid(ids[0])).value.receipt
        except Problem as error:
            if error.body["error"]["code"] != "NATIVE_RID_MISSING":
                raise
            receipt = None
        if (receipt is not None and receipt.status == "accepted"
                and receipt.hosted_session_id == runtime.identity.session_id):
            break
        ids.pop(0)
        dropped = True
    if dropped:
        await _write(runtime, cid, etag, ids)
    return ids[0] if ids else None


async def turn_order(runtime, original, *, meta=_UNREAD, held=None):
    """One pass, never sleeping: may this accepted receipt of this session be numbered (Invocations) or take its
    conversation's slot (Responses) now? Older queued messages go first; a holder another SID left goes first until
    it can no longer complete (adopted when it never started); a Responses holder of this session until it ends
    (``slot:``, the one-at-a-time rule; an adopted one not sent yet is ``adopted:`` and the message queues instead).
    ``meta`` / ``held``: the conversation meta (None: none) and its holder's receipt the caller just read; only the
    queue is read again then, so an admission pays one extra read and no second meta read."""
    from .native_checkpoint import conversation_store_name
    from .native_turns import _Meta, _TASK_TRANSPORT_ERRORS, _decode

    receipt = original.value.receipt
    rid, cid = receipt.request_id, receipt.conversation_id
    responses = receipt.state_profile == NATIVE_RESPONSES_PROFILE
    try:
        async with runtime._store(conversation_store_name(cid)) as store:
            if meta is _UNREAD:
                meta_item, queue_item = await asyncio.gather(store.get_item("meta"), store.get_item(QUEUE_KEY))
                meta = None if meta_item is None else _decode(meta_item.value, _Meta)
            else:
                queue_item = await store.get_item(QUEUE_KEY)
    except Problem as error:
        if error.body["error"]["code"] == "NATIVE_STATE_STORE_MISSING":
            return Order(True, False)
        raise
    queue = None if queue_item is None else _decode(queue_item.value, _Queue)
    queued = queue is not None and rid in queue.request_ids
    first = await _head(runtime, cid, queue, None if queue_item is None else queue_item.etag)
    if first is not None and first != rid:
        return Order(False, queued, "queue:" + first)
    holder = None if meta is None else meta.inflight_rid if responses else meta.rid
    if holder is None or holder == rid:
        return Order(True, queued)
    if held is None or held.value.receipt.request_id != holder:
        held = await runtime._rid(holder)
    if left(runtime, held.value.receipt):
        adopted = await adopt(runtime, held)
        if adopted is None:
            return Order(await settled(runtime, held), queued, "left:" + holder)
        try:
            await _dispatch_adopted(runtime, adopted)
        except (Problem, *_TASK_TRANSPORT_ERRORS) as error:
            # The adoption is committed; this caller's own send still advances the holder (``_advance_inflight``).
            if isinstance(error, Problem) and error.status < 500:
                raise
            log.warning("NATIVE_LEFT_RECEIPT_ADOPTION_WAIT_UNCONFIRMED exception_type=%s", type(error).__name__)
        held = adopted
    if responses and held.value.receipt.status == "accepted" and held.value.adopted_from is not None:
        # Review round 3: an adopted holder not sent yet is waited for like a left one (queued, never refused);
        # one at a time still holds, since only the queue's first takes the slot once the holder ends.
        return Order(False, queued, "adopted:" + holder)
    if responses and held.value.receipt.status not in _TERMINAL:
        return Order(False, queued, "slot:" + holder)
    # Invocations: the order among this session's own turns is the chain's.
    return Order(True, queued)


async def enqueue(runtime, original):
    receipt = original.value.receipt
    rid, cid = receipt.request_id, receipt.conversation_id
    for _ in range(5):
        # A new conversation's store is created by its first turn (``_allocate``); one that waits creates it first.
        queue, etag = await _queue(runtime, cid, create=receipt.command.conversation_id is None)
        ids = () if queue is None else queue.request_ids
        if rid in ids:
            return
        if len(ids) >= MAX_QUEUED:
            break
        if await _write(runtime, cid, etag, (*ids, rid)):
            return
    raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "the conversation's waiting order is unconfirmed; retain the RID")


async def dequeue(runtime, receipt):
    """Best effort once it started: a RID left behind is dropped by the next reader of the queue (``_head``)."""
    try:
        for _ in range(3):
            queue, etag = await _queue(runtime, receipt.conversation_id)
            if queue is None or receipt.request_id not in queue.request_ids:
                return
            ids = [value for value in queue.request_ids if value != receipt.request_id]
            if await _write(runtime, receipt.conversation_id, etag, ids):
                return
    except Problem as error:
        log.info("NATIVE_LEFT_QUEUE_DEQUEUE_UNCONFIRMED code=%s", error.body["error"]["code"])


def _wait_input(runtime, original):
    from .native_turns import NativeTaskInput

    return NativeTaskInput(request_id=original.value.receipt.request_id, accepted_etag=original.etag,
                           call_id=original.value.call_id, execution_version=runtime.identity.agent_version)


async def defer(runtime, original, behind, *, after=None):
    """Queue this accepted receipt in its conversation and start its own wait task (never the Agent's chain).

    ``after``: the ID of the wait task this one succeeds (its timeout fired, review round 5); part of the new task's ID,
    so the successor differs from it and a recovered copy of the same task finds the same successor (no second one)."""
    from azure.ai.agentserver.core.tasks import TaskConflictError

    from .behavior_native_turn_wire import material_digest

    receipt = original.value.receipt
    await enqueue(runtime, original)
    material = {"agent": runtime.identity.agent_id, "rid": receipt.request_id, "behind": behind}
    if after is not None:
        material["after"] = after
    try:
        await runtime.left_wait.start(task_id="fa-left-wait-" + material_digest(material)[:48],
                                      input=_wait_input(runtime, original))
    except TaskConflictError:
        log.info("NATIVE_LEFT_WAIT_RETAINED rid_prefix=%s", receipt.request_id[:8])
        return
    log.warning("NATIVE_QUEUED_BEHIND_LEFT_RECEIPT rid_prefix=%s behind=%s", receipt.request_id[:8],
                behind.split(":", 1)[0])


def early_delay(count: int) -> float:
    """How long after its ``count``-th 409 ``NATIVE_SESSION_CHANGED`` in a row a waiting message is asked for again
    (review round 6): the next look (``POLL_SECONDS``), then ``RESEND_SECONDS``, doubling up to ``EARLY_MAX_SECONDS``;
    never a failure, so it is never ended for it."""
    if count <= 1:
        return POLL_SECONDS
    return min(RESEND_SECONDS * 2 ** (count - 2), EARLY_MAX_SECONDS)


async def wait_turn(runtime, ctx):
    """The wait task of one queued message: looks again every ``POLL_SECONDS`` (each pass may advance the stall rule)
    until it may go on, then puts it on the Agent's chain (Invocations) or gives it the slot (Responses) and has the
    API send it, until its Responses handler started it.

    Its SDK timeout never ends the message (review round 5): a holder that keeps progressing is waited for however long
    its turn takes (only the stall rule decides it is lost), so on ``timeout_exceeded`` (also set at once on a task
    recovered after a VM restart whose budget passed) the task starts its successor and ends, the message still
    ``accepted`` in its queue. Failed sends are bounded by ``SEND_ATTEMPTS`` instead; a cancel still ends it."""
    from time import monotonic

    from azure.ai.agentserver.core.tasks import SteeringQueueFull

    from .native_turns import NativeTaskInput, _TASK_TRANSPORT_ERRORS, _decode

    supplied = ctx.input if isinstance(ctx.input, NativeTaskInput) else _decode(ctx.input, NativeTaskInput)
    rid = supplied.request_id
    due_at = None  # monotonic: no request to send it before this
    failed = early = 0
    while True:
        if ctx.shutdown.is_set():
            return await ctx.exit_for_recovery()
        # Review round 6: the read, the cancel and the hand-over to a successor retry a transient error (5xx, transport)
        # like a look does; ``cancel_requested`` / ``timeout_exceeded`` stay set, so the next pass does it again.
        # Failing the task there would leave the message ``accepted`` at its queue's head with no task looking at it.
        try:
            original = await runtime._rid(rid)
            receipt = original.value.receipt
            if receipt.status != "accepted" or receipt.hosted_session_id != runtime.identity.session_id:
                await dequeue(runtime, receipt)
                return rid
            if ctx.cancel_requested or receipt.cancel_requested:
                return await _end(runtime, original, "NATIVE_TURN_CANCELLED")
            if ctx.timeout_exceeded:
                await defer(runtime, original, "renewed", after=ctx.task_id)
                log.warning("NATIVE_LEFT_WAIT_RENEWED rid_prefix=%s", rid[:8])
                return rid
            if (await turn_order(runtime, original)).go:
                if receipt.state_profile != NATIVE_RESPONSES_PROFILE:
                    binding = await runtime._require_original_session(receipt=receipt)
                    await runtime.chain.start(task_id=binding.task_id, input_id=rid,
                                              input=_wait_input(runtime, original))
                    return rid
                taken = await runtime._response_slot(original, wait=False)
                if taken.status != "accepted" or taken.dispatch is None:
                    return rid
                if taken.dispatch.state == "needs_response":
                    if runtime.on_slot_taken is None:
                        return rid
                    if due_at is None or monotonic() >= due_at:
                        outcome = await _send(runtime, taken)
                        if outcome in {"refused", "revoked"}:
                            # Review round 5: like an owner send under a revoked policy, nothing was sent and the
                            # receipt is left as it is (``accepted``, holding its slot); it is sent once the policy is
                            # restored, at the API's next touch of the conversation (``_advance_inflight``) or the
                            # owner's reconcile of the same RID. Messages queued behind it keep looking every
                            # ``POLL_SECONDS`` until then (review round 6): the owner's own action holds them.
                            return rid
                        if outcome in {"later", "failed"}:
                            failed += 1
                            if failed >= SEND_ATTEMPTS:
                                return await _end(runtime, await runtime._rid(rid), "NATIVE_TURN_TIMEOUT")
                        # "early": refused before anything was sent, never a failure; asked again after a backoff
                        # (``early_delay``, review round 6). "sent", "failed" and "later" (an older API's 404) are
                        # asked again after ``RESEND_SECONDS``.
                        early = early + 1 if outcome == "early" else 0
                        due_at = monotonic() + (early_delay(early) if early else RESEND_SECONDS)
                # ``not_taken``, ``already_queued`` or just sent: look again; the top of the loop ends this task once
                # the Responses handler started it (or it ended).
        except (SteeringQueueFull, *_TASK_TRANSPORT_ERRORS) as error:
            log.info("NATIVE_LEFT_WAIT_RETRY exception_type=%s", type(error).__name__)
        except Problem as error:
            if error.status < 500:
                raise
            log.info("NATIVE_LEFT_WAIT_RETRY code=%s", error.body["error"]["code"])
        # ``cancel`` is already set when a cancel or the timeout is being retried (the SDK sets it with
        # ``timeout_exceeded``): then only shutdown cuts the sleep short, so the retry still waits ``POLL_SECONDS``.
        signals = (asyncio.create_task(ctx.shutdown.wait()),
                   *(() if ctx.cancel.is_set() else (asyncio.create_task(ctx.cancel.wait()),)))
        try:
            await asyncio.wait(signals, timeout=POLL_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for signal in signals:
                signal.cancel()
            await asyncio.gather(*signals, return_exceptions=True)


async def _end(runtime, original, code):
    """Ends this message ``unknown`` with ``code`` while it is still this session's ``accepted`` one (out of its queue,
    with its terminal notice); one that started or ended meanwhile is left as it is."""
    receipt = original.value.receipt
    if receipt.status != "accepted" or receipt.hosted_session_id != runtime.identity.session_id:
        await dequeue(runtime, receipt)
        return receipt.request_id
    ended = await runtime._unknown(original, code)
    await dequeue(runtime, ended)
    await runtime._notify_terminal(ended)
    return receipt.request_id


async def _send(runtime, taken):
    """One request to the API to send this waiting Responses message (``on_slot_taken``):

    - ``"sent"``: answered; looked at again no sooner than ``RESEND_SECONDS``, so a request still on its way is not
      sent twice (and Hosted's Responses handler admits one RID once: ``responses_queued`` and the ``started`` CAS);
    - ``"failed"``: a 5xx / transport error, after which the request may have been sent; spaced like ``"sent"`` and
      counted against ``SEND_ATTEMPTS`` (review round 5);
    - ``"early"``: 409 ``NATIVE_SESSION_CHANGED``, refused before anything was sent (the API has not caught up with the
      switch that made this session the Agent's); asked again after a backoff (``early_delay``: the next look, then
      ``RESEND_SECONDS``, doubling up to ``EARLY_MAX_SECONDS``), never counted as failed. A session that is really no
      longer the Agent's sees its message adopted by the next one and its task ends at the top of the loop;
    - ``"revoked"``: 403 ``WORK_POLICY_REVOKED``, the owner's work policy is revoked or inactive and nothing was sent
      (review round 4): handled like ``"refused"`` (review round 5), as an owner send under that policy leaves its
      receipt ``accepted`` to be sent once the policy is restored;
    - ``"later"``: 404, an API older than this route (review round 4; deploy order is API before Hosted): nothing was
      sent, asked again no sooner than ``RESEND_SECONDS``, counted against ``SEND_ATTEMPTS``;
    - ``"refused"``: any other 4xx: the message keeps its slot and is sent at the API's next touch of the
      conversation, as before."""
    from .native_turns import _TASK_TRANSPORT_ERRORS

    try:
        await runtime.on_slot_taken(taken)
    except _TASK_TRANSPORT_ERRORS as error:
        log.info("NATIVE_LEFT_SLOT_DISPATCH_RETRY exception_type=%s", type(error).__name__)
        return "failed"
    except Problem as error:
        code = error.body["error"]["code"]
        if error.status == 409 and code == "NATIVE_SESSION_CHANGED":
            log.info("NATIVE_LEFT_SLOT_DISPATCH_RETRY code=%s", code)
            return "early"
        if error.status == 403 and code == REVOKED_CODE:
            log.warning("NATIVE_LEFT_SLOT_DISPATCH_REVOKED rid_prefix=%s", taken.request_id[:8])
            return "revoked"
        if error.status == 404:
            # Review round 4: an API older than this route (deploy order is API first); asked again after
            # ``RESEND_SECONDS``, at most ``SEND_ATTEMPTS`` failed requests in all (review round 5).
            log.info("NATIVE_LEFT_SLOT_DISPATCH_RETRY status=404 code=%s", code)
            return "later"
        if error.status < 500:
            log.warning("NATIVE_LEFT_SLOT_DISPATCH_REFUSED rid_prefix=%s status=%s code=%s", taken.request_id[:8],
                        error.status, code)
            return "refused"
        log.info("NATIVE_LEFT_SLOT_DISPATCH_RETRY code=%s", code)
        return "failed"
    else:
        log.warning("NATIVE_LEFT_SLOT_DISPATCH_REQUESTED rid_prefix=%s", taken.request_id[:8])
    return "sent"


async def _dispatch_adopted(runtime, adopted):
    """An adopted receipt runs through the ordinary path of this session: it waits its turn in its conversation's
    queue with a wait task of its own. A Responses slot holder keeps its slot (it is the queue's first) and its wait
    task has the API send it (review round 3: before, it waited for the API's next touch)."""
    await defer(runtime, adopted, "adopted")


async def observe_left(runtime, original):
    """An outside read of a RID (the API's status reads): a never-started receipt an earlier SID left is adopted and
    queued here; a left ``started`` one gets one pass of the stall rule. Returns False, without any I/O, for every
    other receipt. A read still answers when this fails (5xx)."""
    from .native_turns import _TASK_TRANSPORT_ERRORS

    if not left(runtime, original.value.receipt):
        return False
    try:
        if original.value.receipt.status == "accepted":
            adopted = await adopt(runtime, original)
            if adopted is not None:
                await _dispatch_adopted(runtime, adopted)
        else:
            await settled(runtime, original)
    except _TASK_TRANSPORT_ERRORS as error:
        log.info("NATIVE_LEFT_RECEIPT_READ_UNCONFIRMED exception_type=%s", type(error).__name__)
    except Problem as error:
        if error.status < 500:
            raise
        log.info("NATIVE_LEFT_RECEIPT_READ_UNCONFIRMED code=%s", error.body["error"]["code"])
    return True


async def adopt_left(runtime, source=None, rids=None):
    """Right after an abandon commit, under the admission lock: adopt every never-started receipt other SIDs left,
    oldest first, before this session admits anything, then close the cards of every turn that started on ``source``
    (the SID just abandoned) when it is retired: such a turn can never complete (ADR-0009 gate 6). Bounded like the
    backfill read; what it misses is adopted or closed when it is read (``observe_left``). ``rids``: the open-RID set
    when it was complete for ``source`` (every open receipt is listed), read instead of every receipt."""
    from time import monotonic

    from .native_turns import TURNS_STORE, _TASK_TRANSPORT_ERRORS

    found, started, complete, after, deadline = [], [], True, None, monotonic() + _ADOPT_SCAN_SECONDS
    try:
        async with runtime._store(TURNS_STORE) as store:
            while complete:
                if rids is not None:
                    keys, page = ["rid:" + rid for rid in sorted(rids)], None
                else:
                    page = await store.list_keys(limit=100, after=after)
                    keys = [key.key for key in page.keys]
                for key in keys:
                    if not key.startswith("rid:"):
                        continue
                    if monotonic() > deadline:
                        complete = False
                        break
                    item = await store.get_item(key)
                    try:
                        original = runtime._check_rid(item, key[4:])
                    except Problem:
                        continue
                    if original.value.receipt.status == "accepted" and left(runtime, original.value.receipt):
                        created = getattr(item, "created_at", None)
                        found.append((created if isinstance(created, int) and not isinstance(created, bool) else 0,
                                      key, original))
                    elif left(runtime, original.value.receipt) and original.value.receipt.hosted_session_id == source:
                        started.append(original)
                if page is None or not page.has_more:
                    break
                if not page.last_id or page.last_id == after:
                    complete = False
                    break
                after = page.last_id
    except (Problem, *_TASK_TRANSPORT_ERRORS) as error:
        # Never fails the committed switch: what is not adopted here is adopted when it is read.
        log.warning("NATIVE_LEFT_RECEIPTS_ADOPTION_READ_UNCONFIRMED exception_type=%s", type(error).__name__)
        complete = False
    adopted = 0
    for _, _, original in sorted(found, key=lambda entry: entry[:2]):
        try:
            current = await adopt(runtime, original)
            if current is not None:
                adopted += 1
                await _dispatch_adopted(runtime, current)
        except (Problem, *_TASK_TRANSPORT_ERRORS) as error:
            log.warning("NATIVE_LEFT_RECEIPT_ADOPTION_UNCONFIRMED exception_type=%s code=%s", type(error).__name__,
                        error.body["error"]["code"] if isinstance(error, Problem) else None)
    log.warning("NATIVE_LEFT_RECEIPTS_ADOPTED count=%d complete=%s", adopted, complete)
    for original in started:
        try:
            if not await can_complete(runtime, original):
                runtime._close_cards_later(original.value.receipt)
        except (Problem, *_TASK_TRANSPORT_ERRORS) as error:
            log.info("NATIVE_LEFT_RECEIPT_CARDS_UNCONFIRMED exception_type=%s", type(error).__name__)
