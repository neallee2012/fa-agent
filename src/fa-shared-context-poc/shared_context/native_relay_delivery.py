"""Receipts for the original SDK delivery task, not another queue or timer."""

from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .behavior_native_agent_wire import NativeRelayAcceptance, NativeRelayQueued, NativeRelaySend, relay_task_id
from .behavior_native_turn_wire import material_digest
from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem


class _Delivery(WireModel):
    reference: NativeRelayQueued
    source_submit_digest: Digest
    source_run_token: Identifier
    send_attempted: bool = False
    send_token: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    accepted: NativeRelayAcceptance | None = None
    outcome: Literal["not_delivered", "unknown"] | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def original(self):
        if self.reference.status != "queued" or self.reference.reason is not None:
            raise ValueError("delivery input retains the original queued reference")
        if self.accepted is not None and (
                not self.send_attempted or self.accepted.recipient_agent_id != self.reference.recipient_agent_id):
            raise ValueError("delivery acceptance must belong to this attempted recipient")
        if self.outcome == "not_delivered" and self.send_attempted:
            raise ValueError("an attempted send cannot be declared undelivered")
        if self.send_token is not None and not self.send_attempted:
            raise ValueError("an in-flight attempt retains its uncertainty marker")
        return self


async def read_delivery(runtime, reference):
    from .native_turns import TURNS_STORE
    async with runtime._store(TURNS_STORE) as store:
        item = await store.get_item("relay:" + reference.task_id)
    return _checked_delivery(item, reference), item


def _checked_delivery(item, reference):
    from .native_turns import _decode
    if item is None:
        raise Problem(503, "NATIVE_RELAY_OUTCOME_UNKNOWN", "the original delivery receipt is unavailable")
    value = _decode(item.value, _Delivery)
    if (item.key != "relay:" + reference.task_id
            or value.reference != reference.model_copy(update={"status": "queued", "reason": None})):
        raise Problem(409, "NATIVE_STATE_SCOPE", "the original delivery reference differs")
    return value


async def _save(runtime, value, item):
    from .native_turns import TURNS_STORE, _UNCERTAIN, _decode, _json
    key = "relay:" + value.reference.task_id
    async with runtime._store(TURNS_STORE) as store:
        try:
            if item is None:
                await store.create_item(key, _json(value))
            else:
                await store.set_item(key, _json(value), if_match=item.etag)
        except _UNCERTAIN:
            found = await store.get_item(key)
            if found is None or _decode(found.value, _Delivery) != value:
                raise Problem(503, "NATIVE_RELAY_OUTCOME_UNKNOWN", "the original delivery update is unconfirmed") from None


def reference_for(receipt, command):
    if (receipt.conversation_id, receipt.request_id) != (command.conversation_id, command.request_id):
        raise Problem(409, "NATIVE_STATE_SCOPE", "the queued command must retain its original sender address")
    return NativeRelayQueued(
        agent_id=receipt.agent_id, conversation_id=receipt.conversation_id, request_id=receipt.request_id,
        recipient_agent_id=command.message.recipient_agent_id, tool_call_id=command.tool_call_id,
        task_id=relay_task_id(receipt.agent_id, command), command_sha256=material_digest(command))


async def reserve(runtime, receipt, command, *, uncertain=False):
    from .native_turns import TURNS_STORE, _decode
    current = await runtime._rid(receipt.request_id)
    if ((current.value.receipt.agent_id, current.value.receipt.conversation_id, current.value.receipt.submit_digest) != (
                receipt.agent_id, receipt.conversation_id, receipt.submit_digest) or current.value.receipt.status != "started"
            or current.value.receipt.cancel_requested or receipt.conversation_id != command.conversation_id):
        raise Problem(409, "NATIVE_RELAY_REFUSED", "only the original started sender may queue a delivery")
    reference = reference_for(receipt, command)
    value = _Delivery(reference=reference, source_submit_digest=receipt.submit_digest,
                      source_run_token=current.value.run_token, send_attempted=uncertain)
    async with runtime._store(TURNS_STORE) as store:
        item = await store.get_item("relay:" + reference.task_id)
    if item is None:
        await _save(runtime, value, None)
    else:
        saved = _decode(item.value, _Delivery)
        if (saved.reference, saved.source_submit_digest, saved.source_run_token) != (
                reference, receipt.submit_digest, current.value.run_token):
            raise Problem(409, "NATIVE_STATE_SCOPE", "the original queued message differs")
        if uncertain and not saved.send_attempted:
            await _save(runtime, saved.model_copy(update={"send_attempted": True}), item)
    return reference


async def attach(runtime, reference):
    from .native_turns import _changed
    current = await runtime._rid(reference.request_id)
    saved, _ = await read_delivery(runtime, reference)
    receipt = current.value.receipt
    if (receipt.submit_digest != saved.source_submit_digest or current.value.run_token != saved.source_run_token
            or receipt.status != "started" or receipt.cancel_requested):
        raise Problem(409, "NATIVE_RELAY_REFUSED", "the queued sender execution was fenced")
    if receipt.queued_relay is not None:
        if receipt.queued_relay != reference:
            raise Problem(409, "NATIVE_RID_CONFLICT", "the original sender already queued another message")
        return
    result, _ = await runtime._cas(current, _changed(current.value, receipt=_changed(receipt, queued_relay=reference)))
    if result.value.receipt.queued_relay != reference:
        raise Problem(503, "NATIVE_RELAY_OUTCOME_UNKNOWN", "the sender's original queue reference is unconfirmed")


async def for_command(runtime, command):
    from .native_turns import TURNS_STORE
    current = await runtime._rid(command.request_id)
    receipt = current.value.receipt
    if receipt.conversation_id != command.conversation_id:
        raise Problem(409, "NATIVE_STATE_SCOPE", "the delivery belongs to another conversation")
    reference = reference_for(receipt, command)
    async with runtime._store(TURNS_STORE) as store:
        item = await store.get_item("relay:" + reference.task_id)
    if item is None:
        return None
    value = _checked_delivery(item, reference)
    if (value.source_submit_digest != receipt.submit_digest or value.source_run_token != current.value.run_token):
        raise Problem(409, "NATIVE_STATE_SCOPE", "the delivery no longer matches its original sender")
    return value, item


async def before_send(runtime, command):
    found = await for_command(runtime, command)
    if found is None:
        return None
    value, item = found
    if value.outcome is not None:
        raise Problem(409, "NATIVE_RELAY_REFUSED", "the original delivery has already stopped")
    token = uuid4().hex
    await _save(runtime, value.model_copy(update={"send_attempted": True, "send_token": token}), item)
    return token, value.send_attempted


async def refused_before_delivery(runtime, command, attempt):
    if attempt is None:
        return
    value, item = await for_command(runtime, command)
    token, earlier_unknown = attempt
    if value.send_token != token:
        raise Problem(503, "NATIVE_RELAY_OUTCOME_UNKNOWN", "another original attempt owns the send observation")
    await _save(runtime, value.model_copy(update={"send_attempted": earlier_unknown, "send_token": None}), item)


async def accepted(runtime, command, receipt):
    from .behavior_native_agent_wire import relay_acceptance_scope
    found = await for_command(runtime, command)
    if found is None:
        return False
    value, item = found
    if value.accepted is not None:
        if relay_acceptance_scope(value.accepted) != relay_acceptance_scope(receipt):
            raise Problem(409, "NATIVE_STATE_SCOPE", "the original delivery has another acceptance")
    else:
        await _save(runtime, value.model_copy(update={
            "accepted": receipt, "send_attempted": True, "send_token": None}), item)
    return True


async def stopped(runtime, command, *, expired, reason):
    found = await for_command(runtime, command)
    if found is None:
        return
    value, item = found
    if value.accepted is not None or value.outcome is not None:
        return
    outcome = "not_delivered" if expired and not value.send_attempted else "unknown"
    await _save(runtime, value.model_copy(update={"outcome": outcome, "reason": reason}), item)


async def project(runtime, receipt):
    from .native_turns import _changed
    from .behavior_native_agent_wire import relay_acceptance_scope
    if receipt.queued_relay is None:
        return receipt
    value, _ = await read_delivery(runtime, receipt.queued_relay)
    if (value.source_submit_digest != receipt.submit_digest or receipt.delegation is not None
            and value.accepted is not None
            and relay_acceptance_scope(receipt.delegation) != relay_acceptance_scope(value.accepted)):
        raise Problem(409, "NATIVE_STATE_SCOPE", "the delivery observation differs from its original sender")
    refused = value.accepted is not None and value.accepted.status == "busy_refused"
    status = "not_delivered" if refused else "accepted" if value.accepted is not None else value.outcome or "queued"
    return _changed(receipt, queued_relay=_changed(
        value.reference, status=status, reason="NATIVE_RELAY_QUEUE_FULL" if refused else value.reason),
        delegation=receipt.delegation or (None if refused else value.accepted))
