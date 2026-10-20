"""One original RID's ordered control receipts; only the SDK delivers their text."""

import hashlib
import json
import logging
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .native_append_wire import NativeAppend, NativeAppendReceipt, NativeAppends, check_append_status

# This process's own claims in ``NativeTurnControl.sent_appends``: append ID -> (send token, outcome). The outcome is
# UNSENT from before the claim write until the SDK send starts, ATTEMPTED from then on (also when the send raised), then
# the SDK message ID, or None when the channel took nothing. Only memory: a restart forgets them (the entry stays
# ``sending`` and ends ``unknown``; it is never sent again).
UNSENT, ATTEMPTED = object(), object()


class _Entry(WireModel):
    command: NativeAppend
    status: Literal["accepted", "sending", "applied", "not_applied", "unknown"] = "accepted"
    send_token: Identifier | None = None
    sdk_message_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    delivery: Literal["steering", "queued", "idle"] | None = None
    reason: Literal["turn_finished", "cancelled", "sdk_unconfirmed"] | None = None

    @model_validator(mode="after")
    def delivery_state(self):
        check_append_status(self.status, self.sdk_message_id, self.delivery, self.reason)
        return self


class _Appends(WireModel):
    agent_id: Identifier
    hosted_session_id: Identifier
    request_id: Identifier
    conversation_id: Identifier
    submit_digest: Digest
    entries: tuple[_Entry, ...] = ()

    @model_validator(mode="after")
    def original_scope(self):
        seen = set()
        for entry in self.entries:
            command = entry.command
            if (command.request_id, command.conversation_id, command.expected_submit_digest) != (
                    self.request_id, self.conversation_id, self.submit_digest) or command.append_id in seen:
                raise ValueError("Append controls retain one original turn and distinct IDs")
            seen.add(command.append_id)
        return self


def _key(rid):
    return "appends:" + hashlib.sha256(rid.encode("utf-8")).hexdigest()


def _empty(receipt):
    return _Appends(agent_id=receipt.agent_id, hosted_session_id=receipt.hosted_session_id,
                    request_id=receipt.request_id, conversation_id=receipt.conversation_id,
                    submit_digest=receipt.submit_digest)


async def _load(store, receipt):
    from .native_turns import _decode

    item = await store.get_item(_key(receipt.request_id))
    value = _empty(receipt) if item is None else _decode(item.value, _Appends)
    if value.model_dump(exclude={"entries"}) != _empty(receipt).model_dump(exclude={"entries"}):
        raise Problem(403, "NATIVE_STATE_SCOPE", "the append controls belong to another original turn")
    return value, None if item is None else item.etag


async def _update(runtime, receipt, change):
    from .native_turns import TURNS_STORE, _CONFLICT, _UNCERTAIN, _json

    async with runtime._store(TURNS_STORE) as store:
        for _ in range(3):
            original, etag = await _load(store, receipt)
            replacement = change(original)
            if replacement == original:
                return original
            try:
                if etag is None:
                    await store.create_item(_key(receipt.request_id), _json(replacement))
                else:
                    await store.set_item(_key(receipt.request_id), _json(replacement), if_match=etag)
                return replacement
            except _UNCERTAIN as error:
                observed, _ = await _load(store, receipt)
                if observed == replacement:
                    return observed
                if not isinstance(error, _CONFLICT):
                    logging.getLogger(__name__).warning("NATIVE_APPEND_UNKNOWN stage=state_write")
                    raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "read the original append ID; do not resend") from None
    raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "the original append update could not be confirmed")


def _receipt(state, entry, target):
    from .native_turns import _changed

    value = NativeAppendReceipt(
        agent_id=state.agent_id, hosted_session_id=state.hosted_session_id, request_id=state.request_id,
        conversation_id=state.conversation_id, append_id=entry.command.append_id, submit_digest=state.submit_digest,
        sequence=state.entries.index(entry) + 1, content=entry.command.content, status=entry.status,
        sdk_message_id=entry.sdk_message_id, delivery=entry.delivery, reason=entry.reason)
    if entry.status in {"accepted", "sending"} and target.status not in {"accepted", "started"}:
        unconfirmed = entry.status == "sending" or target.status == "unknown"
        value = _changed(value, status="unknown" if unconfirmed else "not_applied",
                         reason="sdk_unconfirmed" if unconfirmed else "turn_finished")
    return value


async def append(runtime, command):
    from .native_turns import MAX_ITEM_BYTES, _changed, _json

    original = await runtime._rid(command.request_id)
    target = original.value.receipt
    runtime._address(target, command.conversation_id)
    if command.expected_submit_digest != target.submit_digest:
        raise Problem(409, "NATIVE_RID_CONFLICT", "the append must name the original turn version")
    if target.status in {"accepted", "started"}:
        await runtime._require_original_session(receipt=target)
    # This process runs the turn and its SDK channel is closed (the last SDK round is over; it never reopens and the RID
    # never runs the SDK again), so the append is final now, not after the SDK stop and save (G plan, 2026-10-19).
    control = runtime.running_controls.get(command.request_id)
    closed = control is not None and control.steering is not None and control.steering.closed
    cancelled = target.cancel_requested or closed and control.cancelled

    def accept(state):
        for entry in state.entries:
            if entry.command.append_id == command.append_id:
                if entry.command != command:
                    raise Problem(409, "NATIVE_RID_CONFLICT", "this append ID already retains different content")
                return state
        ended = target.status not in {"accepted", "started"} or cancelled or closed
        reason = ("cancelled" if cancelled else "turn_finished") if ended else None
        entries = state.entries
        if closed:
            # Earlier appends the closed channel never took end in this same write, so they never settle after this
            # one (the owner's resend keeps append order). An attempted send is left to the turn's own settle.
            entries = tuple(_changed(item, status="not_applied", reason=reason)
                            if item.status == "accepted" or _unsent(item, control) else item for item in entries)
        entry = _Entry(command=command, status="not_applied" if ended else "accepted", reason=reason)
        replacement = _changed(state, entries=(*entries, entry))
        # Leave room for escaped SDK IDs and completion metadata before promising acceptance.
        reserved = 2048 * sum(item.status in {"accepted", "sending"} for item in replacement.entries)
        if len(json.dumps(_json(replacement), allow_nan=False).encode("utf-8")) + reserved > MAX_ITEM_BYTES:
            raise Problem(413, "NATIVE_REQUEST_TOO_LARGE", "the original turn's append receipts exceed the State store bound")
        return replacement

    state = await _update(runtime, target, accept)
    runtime._wake_control(command.request_id)  # a running turn here delivers it now, not at its next poll
    return _receipt(state, next(item for item in state.entries if item.command.append_id == command.append_id), target)


async def read_append(runtime, command):
    from .native_turns import TURNS_STORE

    target = (await runtime._rid(command.request_id)).value.receipt
    runtime._address(target, command.conversation_id)
    async with runtime._store(TURNS_STORE) as store:
        state, _ = await _load(store, target)
    if command.append_id is None:
        return NativeAppends(
            **state.model_dump(exclude={"entries"}),
            appends=tuple(_receipt(state, entry, target) for entry in state.entries))
    for entry in state.entries:
        if entry.command.append_id == command.append_id:
            return _receipt(state, entry, target)
    raise Problem(404, "NATIVE_APPEND_MISSING", "the original append receipt is not visible")


def _observed(entry, channel, *, finished=False, cancelled=False):
    from .native_turns import _changed

    if entry.status not in {"accepted", "sending"}:
        return entry
    delivery = None if channel is None else channel.delivered.get(entry.sdk_message_id)
    if delivery is not None:
        return _changed(entry, status="applied", delivery=delivery)
    if finished:
        return _changed(entry, status="not_applied" if entry.status == "accepted" else "unknown",
                        reason=("cancelled" if cancelled else "turn_finished") if entry.status == "accepted"
                        else "sdk_unconfirmed")
    return entry


def _remembered(entry, control, *, finished=False):
    """The outcome of this process's own claim of an entry: the SDK message ID, or ``not_applied`` when the channel took
    nothing or the turn ended before the send started. Applied again on every settle, so a ``remember`` write that was
    unconfirmed is retried."""
    from .native_turns import _changed

    claim = control.sent_appends.get(entry.command.append_id)
    if claim is None or entry.status != "sending" or entry.send_token != claim[0] or entry.sdk_message_id is not None:
        return entry
    outcome = claim[1]
    if outcome is None or (outcome is UNSENT and finished):
        return _changed(entry, status="not_applied", reason="cancelled" if control.cancelled else "turn_finished")
    if isinstance(outcome, str):
        return _changed(entry, sdk_message_id=outcome)
    return entry  # UNSENT while the turn runs (sent next); ATTEMPTED (never sent again)


def _unsent(entry, control):
    """Claimed by this process (its claim write may have been unconfirmed) and not handed to the SDK yet."""
    claim = control.sent_appends.get(entry.command.append_id)
    return entry.status == "sending" and claim is not None and claim[0] == entry.send_token and claim[1] is UNSENT


async def settle_appends(runtime, target, control, *, finished=False):
    from .native_turns import _changed

    def settle(state):
        return _changed(state, entries=tuple(_observed(
            _remembered(entry, control, finished=finished), control.steering, finished=finished,
            cancelled=control.cancelled) for entry in state.entries))

    return await _update(runtime, target, settle)


async def deliver_appends(runtime, target, control):
    """Hand the next append to the SDK, in order; True when one was handed over (look for the next at once).

    The next one is the first ``accepted`` entry or, before it, an entry this process claimed and has not sent: a claim
    write that landed although its ``_update`` was unconfirmed. A ``sending`` entry under another token (another
    process, or this one before a restart) or whose send has started is never sent again."""
    from .native_turns import _changed

    channel = control.steering
    if channel is None or not channel.ready or channel.closed or control.stop_requested():
        return
    state = await settle_appends(runtime, target, control)
    pending = next((entry for entry in state.entries if entry.status == "accepted" or _unsent(entry, control)), None)
    if pending is None:
        return
    append_id = pending.command.append_id
    if pending.status == "accepted":
        # Recorded before the claim write, so a write that lands unconfirmed is still sent (and only once); an
        # unconfirmed claim is written again under the same token.
        previous = control.sent_appends.get(append_id)
        token = previous[0] if previous is not None and previous[1] is UNSENT else uuid4().hex
        control.sent_appends[append_id] = (token, UNSENT)

        def claim(current):
            return _changed(current, entries=tuple(
                _changed(entry, status="sending", send_token=token)
                if entry.command.append_id == append_id and entry.status == "accepted" else entry
                for entry in current.entries))

        state = await _update(runtime, target, claim)
        pending = next(entry for entry in state.entries if entry.command.append_id == append_id)
        if pending.send_token != token:
            return True
    control.sent_appends[append_id] = (pending.send_token, ATTEMPTED)  # from here a resend could deliver it twice
    identifier = await channel.send(pending.command.content)
    control.sent_appends[append_id] = (pending.send_token, identifier)
    await settle_appends(runtime, target, control)  # writes it through ``_remembered``
    return True
