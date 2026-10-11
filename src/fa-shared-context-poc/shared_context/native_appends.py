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

    def accept(state):
        for entry in state.entries:
            if entry.command.append_id == command.append_id:
                if entry.command != command:
                    raise Problem(409, "NATIVE_RID_CONFLICT", "this append ID already retains different content")
                return state
        ended = target.status not in {"accepted", "started"} or target.cancel_requested
        entry = _Entry(command=command, status="not_applied" if ended else "accepted",
                       reason=("cancelled" if target.cancel_requested else "turn_finished") if ended else None)
        replacement = _changed(state, entries=(*state.entries, entry))
        # Leave room for escaped SDK IDs and completion metadata before promising acceptance.
        reserved = 2048 * sum(item.status in {"accepted", "sending"} for item in replacement.entries)
        if len(json.dumps(_json(replacement), allow_nan=False).encode("utf-8")) + reserved > MAX_ITEM_BYTES:
            raise Problem(413, "NATIVE_REQUEST_TOO_LARGE", "the original turn's append receipts exceed the State store bound")
        return replacement

    state = await _update(runtime, target, accept)
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


async def settle_appends(runtime, target, control, *, finished=False):
    from .native_turns import _changed

    def settle(state):
        return _changed(state, entries=tuple(_observed(
            entry, control.steering, finished=finished, cancelled=control.cancelled) for entry in state.entries))

    return await _update(runtime, target, settle)


async def deliver_appends(runtime, target, control):
    from .native_turns import _changed

    channel = control.steering
    if channel is None or not channel.ready or channel.closed or control.stop_requested():
        return
    state = await settle_appends(runtime, target, control)
    pending = next((entry for entry in state.entries if entry.status == "accepted"), None)
    if pending is None:
        return
    token = uuid4().hex

    def claim(current):
        return _changed(current, entries=tuple(
            _changed(entry, status="sending", send_token=token)
            if entry.command.append_id == pending.command.append_id and entry.status == "accepted" else entry
            for entry in current.entries))

    state = await _update(runtime, target, claim)
    claimed = next(entry for entry in state.entries if entry.command.append_id == pending.command.append_id)
    if claimed.send_token != token:
        return
    identifier = await channel.send(claimed.command.content)

    def remember(current):
        entries = []
        for entry in current.entries:
            if entry.command.append_id == claimed.command.append_id and entry.send_token == token:
                entry = (_changed(entry, status="not_applied",
                                  reason="cancelled" if control.cancelled else "turn_finished")
                         if identifier is None else _observed(_changed(entry, sdk_message_id=identifier), channel))
            entries.append(entry)
        return _changed(current, entries=tuple(entries))

    await _update(runtime, target, remember)
