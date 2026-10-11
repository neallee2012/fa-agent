"""Trusted relay material for native turns; N1 owns durable acceptance and its official queue."""

from __future__ import annotations

import hashlib
import json
from .behavior_auth import BehaviorPrincipal
from .behavior_native_agent_wire import NativePeerRequest, NativeRelaySend, relay_task_id
from .behavior_native_turn_wire import (
    NativeAgentCommand, NativeAgentOrigin, NativeAgentReceive, NativeOutcomeReference, NativeReceipt, NativeResultReference,
    NativeTurnAddress, NativeTurnCapture, material_digest, native_turn_root,
)
from .behavior_server_config import ServerAgentScope
from .behavior_team_registry import same_team_recipient
from .errors import Problem
from .native_responses_wire import NATIVE_RESPONSES_PROFILE, NATIVE_STATE_PROFILE

def _sender(principal: BehaviorPrincipal, scope: ServerAgentScope, receipt: NativeReceipt, *, status: str | None):
    principal.require(scope.team_id, "submit")
    if (principal.kind != "agent" or principal.tenant_id != str(scope.tenant_id)
            or principal.oid != str(scope.identity.oid) or principal.actor_id != scope.identity.actor
            or receipt.agent_id != scope.target.agent_id
            or status is not None and (receipt.status != status or receipt.cancel_requested)):
        raise Problem(403, "NATIVE_AGENT_SOURCE_INVALID", "the original turn must belong to the verified sending Agent")


def _recipient(principal: BehaviorPrincipal, scope: ServerAgentScope):
    if str(scope.tenant_id) != principal.tenant_id:
        raise Problem(403, "AGENT_MESSAGE_ROUTE_DENIED", "only a different Agent in this Team can receive the message")
    same_team_recipient(scope, sender_oid=principal.oid, team_id=principal.team)


def _address(receipt: NativeReceipt) -> NativeTurnAddress:
    return NativeTurnAddress(agent_id=receipt.agent_id, conversation_id=receipt.conversation_id,
                             request_id=receipt.request_id)


def message_identifier(principal: BehaviorPrincipal, sender: NativeReceipt, tool_call_id: str) -> str:
    if not isinstance(tool_call_id, str) or not tool_call_id or len(tool_call_id) > 256:
        raise Problem(422, "AGENT_MESSAGE_ARGUMENTS", "the original SDK callback identifier is required")
    return material_digest({
        "kind": "fa-native-peer-message-v1", "tenant": principal.tenant_id, "team": principal.team,
        "sender": _address(sender).model_dump(mode="json"), "tool_call_id": tool_call_id,
    })


def message_material_digest(message: NativeAgentReceive | NativeReceipt) -> str:
    if message.source is None:
        raise Problem(403, "NATIVE_AGENT_SOURCE_INVALID", "original Agent provenance is required")
    return material_digest({
        "command": message.command.model_dump(mode="json", exclude={"model"}),
        "source": message.source.model_dump(mode="json"),
    })


def queued_command_matches(sender: NativeReceipt, command: NativeRelaySend) -> bool:
    reference = sender.queued_relay
    return (reference is not None
            and (reference.agent_id, reference.conversation_id, reference.request_id,
                 reference.recipient_agent_id, reference.tool_call_id, reference.command_sha256, reference.task_id) == (
                sender.agent_id, command.conversation_id, command.request_id, command.message.recipient_agent_id,
                command.tool_call_id, material_digest(command), relay_task_id(sender.agent_id, command)))


def queued_request_matches(sender: NativeReceipt, command: NativeRelaySend) -> bool:
    return (queued_command_matches(sender, command) and sender.queued_relay.status == "queued"
            and not sender.cancel_requested)


def prepare_request(
    principal: BehaviorPrincipal, sender_scope: ServerAgentScope, sender: NativeReceipt,
    recipient: ServerAgentScope, capture: NativeTurnCapture, command: NativePeerRequest, *, tool_call_id: str,
    original: NativeReceipt | None = None,
) -> NativeAgentReceive:
    """Only the brief/recipient/refs come from the model; IDs and lineage come from verified original material."""
    sender = NativeReceipt.model_validate_json(sender.model_dump_json())
    command = NativePeerRequest.model_validate_json(command.model_dump_json())
    queued = queued_request_matches(sender, NativeRelaySend(
        conversation_id=sender.conversation_id, request_id=sender.request_id, tool_call_id=tool_call_id, message=command))
    _sender(principal, sender_scope, sender, status="started" if original is None and not queued else None)
    _recipient(principal, recipient)
    if command.recipient_agent_id != recipient.target.agent_id:
        raise Problem(403, "AGENT_MESSAGE_ROUTE_DENIED", "the message must use the server-resolved recipient")
    if any(ref.tenant_id != principal.tenant_id or ref.team_id != principal.team for ref in command.refs):
        raise Problem(403, "AGENT_MESSAGE_REFS_DENIED", "message references cannot select another Team")
    address = _address(sender)
    root = native_turn_root(sender)
    root_id = (material_digest({"kind": "fa-native-peer-root-v1", "tenant": principal.tenant_id,
                               "team": principal.team, "origin": root.model_dump(mode="json")})
               if sender.source is None else sender.source.root_id)
    hop = 1 if sender.source is None else sender.source.hop + 1
    identifier = message_identifier(principal, sender, tool_call_id)
    source = NativeAgentOrigin(
        kind="agent_request", message_kind=command.kind, message_id=identifier,
        sender=address, recipient_agent_id=recipient.target.agent_id, root_id=root_id, root=root,
        hop=hop, refs=command.refs,
    )
    request = NativeAgentReceive(
        command=NativeAgentCommand(request_id=identifier, content=command.content, model=capture.model.id),
        capture=capture, source=source,
        state_profile=NATIVE_STATE_PROFILE if original is None else original.state_profile,
        new_conversation_id=(original.conversation_id if original is not None
                             and original.state_profile == NATIVE_RESPONSES_PROFILE
                             and original.command.conversation_id is None else None),
    )
    if original is not None and (
            original.agent_id != recipient.target.agent_id or original.submit_digest != request.sha256):
        raise Problem(409, "NATIVE_RID_CONFLICT", "the original accepted message has different material")
    return request


def prepare_result(
    principal: BehaviorPrincipal, sender_scope: ServerAgentScope, sender: NativeReceipt,
    recipient: ServerAgentScope, capture: NativeTurnCapture, reference: NativeResultReference, text: str | None,
    *, request: NativeReceipt | None = None,
) -> NativeAgentReceive:
    """The original request message ID is also its sole result/wake RID in the recipient's own store."""
    sender = NativeReceipt.model_validate_json(sender.model_dump_json())
    _sender(principal, sender_scope, sender, status="completed")
    _recipient(principal, recipient)
    original = sender if request is None else request
    origin = original.source
    if (origin is None or origin.kind != "agent_request" or sender.result != reference
            or original.agent_id != sender.agent_id or original.conversation_id != sender.conversation_id
            or origin.sender.agent_id != recipient.target.agent_id or sender.delegation is not None):
        raise Problem(403, "AGENT_MESSAGE_REPLY_DENIED", "only the original completed recipient can reply")
    if text is not None:
        raw = text.encode("utf-8")
        if len(raw) != reference.text_bytes or hashlib.sha256(raw).hexdigest() != reference.text_sha256:
            raise Problem(409, "NATIVE_RESULT_INCOMPLETE", "the complete original result length or hash differs")
    content = text
    if text is None or not text.strip() or len(json.dumps(text, ensure_ascii=False).encode("utf-8")) > 8192:
        content = ("The peer's original result is saved. Read the complete attached result reference; "
                   "this notification is not the result text.")
    source = NativeAgentOrigin(
        kind="agent_result", message_kind="result", message_id=origin.message_id,
        sender=_address(sender), recipient_agent_id=recipient.target.agent_id,
        root_id=origin.root_id, root=origin.root, hop=origin.hop - 1,
        in_reply_to=origin.message_id, result=reference, continuation=origin.sender, refs=origin.refs,
    )
    return NativeAgentReceive(
        command=NativeAgentCommand(
            request_id=origin.message_id, conversation_id=origin.sender.conversation_id,
            content=content, model=capture.model.id),
        capture=capture, source=source,
    )


def outcome_reference(receipt: NativeReceipt) -> NativeOutcomeReference:
    if receipt.status not in {"unknown", "busy_refused"}:
        raise Problem(403, "AGENT_MESSAGE_REPLY_DENIED", "a stored unconfirmed outcome is required")
    code = receipt.failure_code or "NATIVE_TURN_UNKNOWN"
    return NativeOutcomeReference(status=receipt.status, failure_code=code, receipt_digest=material_digest({
        "address": _address(receipt).model_dump(mode="json"), "submit": receipt.submit_digest,
        "status": receipt.status, "failure_code": code,
    }))


def prepare_outcome(
    principal: BehaviorPrincipal, sender_scope: ServerAgentScope, sender: NativeReceipt,
    recipient: ServerAgentScope, capture: NativeTurnCapture, *, request: NativeReceipt | None = None,
) -> NativeAgentReceive:
    sender = NativeReceipt.model_validate_json(sender.model_dump_json())
    _sender(principal, sender_scope, sender, status=None)
    _recipient(principal, recipient)
    original = sender if request is None else request
    origin = original.source
    if (origin is None or origin.kind != "agent_request" or original.agent_id != sender.agent_id
            or original.conversation_id != sender.conversation_id or origin.sender.agent_id != recipient.target.agent_id):
        raise Problem(403, "AGENT_MESSAGE_REPLY_DENIED", "the original peer request is required")
    outcome = outcome_reference(sender)
    source = NativeAgentOrigin(
        kind="agent_notice", message_kind="blocked", message_id=origin.message_id,
        sender=_address(sender), recipient_agent_id=recipient.target.agent_id, root_id=origin.root_id,
        root=origin.root, hop=max(0, origin.hop - 1), in_reply_to=origin.message_id,
        outcome=outcome, continuation=origin.sender)
    return NativeAgentReceive(
        command=NativeAgentCommand(
            request_id=origin.message_id, conversation_id=origin.sender.conversation_id, model=capture.model.id,
            content=f"對方的原回合結果未確認（{outcome.failure_code}）。這不表示對方已停止或沒有外部效果；"
                    "請勿重新交辦原工作。"),
        capture=capture, source=source)
