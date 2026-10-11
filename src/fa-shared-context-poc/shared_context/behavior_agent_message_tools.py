"""One Hosted tool; handoffs await the saved reply inside their original SDK invocation."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Annotated

from pydantic import Field

from .behavior_agent_message_wire import (
    AGENT_MESSAGE_ERRORS, CAPABILITY_ID, MAX_AGENT_MESSAGE_BYTES, AgentMessage, AgentMessageRead,
    AgentMessageSend, MessageKind, PeerTeamReference,
)
from .behavior_assistant_deadlines import hosted_sync
from .behavior_capabilities import BUILTIN_CAPABILITIES, CAPABILITY_TOOL_GUIDANCE
from .behavior_wire import Identifier, WireModel
from .errors import Problem
from .m2_copilot import ToolFailure, ToolOutput, ToolSpec

def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()

_NO_RESULT = "沒有任何已確認的B結果。接收與處理狀態須依原回條核對，不能把未確認說成系統拒絕。不得把A自己的計算或推測說成B的回覆；保留原工作，不要重送。"
_GUIDANCE = {
    **dict.fromkeys(AGENT_MESSAGE_ERRORS, _NO_RESULT),
    "AGENT_MESSAGE_UNKNOWN": "The original peer message is unconfirmed; do not resend or claim it completed.",
    "AGENT_MESSAGE_REFS_INVALID": "Use only explicit Team source or artifact references with exact versions and hashes.",
    "AGENT_MESSAGE_REFS_DENIED": "The original Team reference is not currently readable; do not substitute private data.",
    "AGENT_MESSAGE_REFS_CHANGED": "The original Team reference changed or is unavailable; do not use a cached copy.",
    "AGENT_MESSAGE_SHARE_DENIED": "Share only the original root's permitted Team references.",
    "AGENT_MESSAGE_RECEIPT_INVALID": "The peer message response did not match the original request.",
    "AGENT_MESSAGE_CONFLICT": "Keep the original message for this SDK call; no replacement was sent.",
    "AGENT_MESSAGE_ROOT_EXPIRED": "The original peer wait allowance expired; keep the original work, do not resend it.",
    "AGENT_MESSAGE_WAIT_UNAVAILABLE": "The original peer response cannot be safely awaited; do not resend the handoff.",
}
_GUIDANCE = {code: text if text == _NO_RESULT else text + " " + _NO_RESULT for code, text in _GUIDANCE.items()}
_GUIDANCE["AGENT_MESSAGE_RECIPIENT_DENIED"] = (
    "訊息沒有送達B，也沒有任何B結果。只能使用本工具列出的收件人ID；"
    "不得把A自己的計算或推測說成B的回覆，不要自行重送。"
)
_GUIDANCE["AGENT_MESSAGE_ARGUMENTS"] = (
    "訊息沒有送達B，也沒有任何B結果。參數不符合這個回合的工具格式；"
    "收件人只能使用列出的完整ID，內容只限公開工作摘要與明確版本的Team引用。"
    "不得把A自己的計算或推測說成B的回覆，不要自行重送。"
)
_GUIDANCE["AGENT_MESSAGE_RESULT_UNCONFIRMED"] = (
    "訊息已接收，B 的結果尚未確認。B 是否已開始處理或已完成，必須依原回條核對；"
    "不要說成系統拒絕了送出，也不要宣稱B未執行或已停止。"
    "不得把A自己的計算或推測說成B的回覆，保留原工作，不要重送。"
)
CAPABILITY_TOOL_GUIDANCE.update(_GUIDANCE)


class AgentMessageArguments(WireModel):
    recipient_agent_id: Identifier
    kind: MessageKind
    content: Annotated[str, Field(max_length=MAX_AGENT_MESSAGE_BYTES)]
    in_reply_to: Identifier | None = None
    refs: Annotated[tuple[PeerTeamReference, ...], Field(max_length=8)] = ()


def _accepted_unconfirmed(cause):
    safe_cause = cause if isinstance(cause, str) and cause in AGENT_MESSAGE_ERRORS else "UNCLASSIFIED"
    logging.getLogger(__name__).warning("AGENT_MESSAGE_RESULT_UNCONFIRMED cause=%s", safe_cause)
    return ToolFailure(_GUIDANCE["AGENT_MESSAGE_RESULT_UNCONFIRMED"],
                       public_code="AGENT_MESSAGE_RESULT_UNCONFIRMED")


def _read_failure(error):
    code = error.body["error"]["code"]
    if error.status >= 500 or code in {
            "AGENT_MESSAGE_UNKNOWN", "AGENT_MESSAGE_ROOT_EXPIRED", "AGENT_MESSAGE_WAIT_UNAVAILABLE"}:
        return _accepted_unconfirmed(code)
    return ToolFailure(_GUIDANCE.get(code, _GUIDANCE["AGENT_MESSAGE_UNKNOWN"]),
                       public_code=code if code in _GUIDANCE else "AGENT_MESSAGE_UNKNOWN")


async def _read(session, message_key):
    try:
        async with session._authority_lock:
            value = await hosted_sync(session.client.read_agent_message, message_key)
        return AgentMessageRead.model_validate_json(value.model_dump_json())
    except Problem as error:
        raise _read_failure(error) from None
    except (AttributeError, TypeError, ValueError):
        raise ToolFailure(_GUIDANCE["AGENT_MESSAGE_RECEIPT_INVALID"],
                          public_code="AGENT_MESSAGE_RECEIPT_INVALID") from None


async def _await_result(session, original):
    current = original
    while True:
        cancelled = getattr(session, "_cancel_requested", None)
        if cancelled is not None and cancelled.is_set():
            raise asyncio.CancelledError
        if current.response_message_key is not None:
            response = await _read(session, current.response_message_key)
            before, after = original.message, response.message
            command = AgentMessage(message_id=after.message_id, recipient_agent_id=after.recipient_agent_id,
                kind=after.kind, content=response.content, in_reply_to=before.message_key, refs=response.refs)
            if (after.message_key != current.response_message_key or after.kind not in {"result", "blocked"}
                    or after.root_request_id != before.root_request_id or after.hop != 2
                    or (after.sender_agent_id, after.recipient_agent_id) != (before.recipient_agent_id, before.sender_agent_id)
                    or (after.from_work_id, after.to_work_id) != (before.to_work_id, before.from_work_id)
                    or (after.from_conversation_id, after.to_conversation_id)
                    != (before.to_conversation_id, before.from_conversation_id)
                    or response.payload_sha256 != _digest(command.model_dump(mode="json"))):
                raise ToolFailure(_GUIDANCE["AGENT_MESSAGE_RECEIPT_INVALID"],
                                  public_code="AGENT_MESSAGE_RECEIPT_INVALID")
            return response
        if current.message.status in {"unknown", "needs_attention"}:
            raise _accepted_unconfirmed("AGENT_MESSAGE_UNKNOWN")
        if original.root_deadline is None:
            raise _accepted_unconfirmed("AGENT_MESSAGE_WAIT_UNAVAILABLE")
        remaining = (original.root_deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise _accepted_unconfirmed("AGENT_MESSAGE_ROOT_EXPIRED")
        try:
            async with asyncio.timeout(remaining):
                await asyncio.sleep(min(1, remaining))
                current = await _read(session, original.message.message_key)
        except TimeoutError:
            raise _accepted_unconfirmed("AGENT_MESSAGE_ROOT_EXPIRED") from None
        if (current.message.message_key != original.message.message_key
                or current.payload_sha256 != original.payload_sha256
                or current.root_deadline != original.root_deadline or current.fresh):
            raise ToolFailure(_GUIDANCE["AGENT_MESSAGE_RECEIPT_INVALID"], public_code="AGENT_MESSAGE_RECEIPT_INVALID")


def agent_message_capability(session, grant):
    builtin = BUILTIN_CAPABILITIES.get(CAPABILITY_ID)
    if (builtin is None or grant.entry_id != CAPABILITY_ID or grant.kind != "fa-builtin"
            or grant.entry_sha256 != builtin.entry_sha256 or grant.tools != ("send_agent_message",)
            or grant.tools != builtin.tools or grant.vision):
        raise Problem(503, "CAPABILITY_UNAVAILABLE", "the original collaboration capability differs")
    task = session.client.assignment.task
    if task.instruction is None:
        raise Problem(403, "CAPABILITY_UNAVAILABLE", "collaboration requires the original instruction assignment")
    recipient = getattr(session.client.assignment, "agent_message_recipient", None)
    if recipient is None:
        raise Problem(503, "AGENT_MESSAGE_UNAVAILABLE", "the original assignment has no configured peer recipient")
    original, confirmed = {}, {}

    async def handle(packet):
        try:
            arguments = AgentMessageArguments.model_validate_json(_canonical(packet["arguments"]))
            call_id = packet["tool_call_id"]
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("an original SDK call identity is required")
            if arguments.recipient_agent_id != recipient.agent_id:
                raise ToolFailure(_GUIDANCE["AGENT_MESSAGE_RECIPIENT_DENIED"],
                                  public_code="AGENT_MESSAGE_RECIPIENT_DENIED")
            message_id = _digest(["fa-operation-control-v1", "agent-message-sdk", task.job_id, call_id])
            message = AgentMessage(message_id=message_id, **arguments.model_dump())
            AgentMessageSend(message=message)
            if len(_canonical(message.model_dump(mode="json"))) > MAX_AGENT_MESSAGE_BYTES:
                raise ValueError("the complete brief exceeds the message envelope")
        except (KeyError, TypeError, ValueError, Problem):
            raise ToolFailure(_GUIDANCE["AGENT_MESSAGE_ARGUMENTS"], public_code="AGENT_MESSAGE_ARGUMENTS") from None
        async with session._authority_lock:
            if message_id in original and original[message_id] != message:
                raise ToolFailure(_GUIDANCE["AGENT_MESSAGE_CONFLICT"], public_code="AGENT_MESSAGE_CONFLICT")
            repeated = message_id in original
            if repeated and message_id not in confirmed:
                raise ToolFailure(_GUIDANCE["AGENT_MESSAGE_UNKNOWN"], public_code="AGENT_MESSAGE_UNKNOWN")
            original[message_id] = message
            try:
                result = (await hosted_sync(session.client.read_agent_message, confirmed[message_id])
                          if repeated else await hosted_sync(session.client.send_agent_message, message))
            except Problem as error:
                code = error.body["error"]["code"]
                if repeated and message.kind in {"request", "handoff"}:
                    raise _read_failure(error) from None
                raise ToolFailure(_GUIDANCE.get(code, _GUIDANCE["AGENT_MESSAGE_UNKNOWN"]),
                                  public_code=code if code in _GUIDANCE else "AGENT_MESSAGE_UNKNOWN") from None
            try:
                result = AgentMessageRead.model_validate_json(result.model_dump_json())
                projection = result.message
                if (projection.message_id != message_id or projection.recipient_agent_id != message.recipient_agent_id
                        or projection.from_work_id != task.work_id
                        or projection.from_conversation_id != task.instruction.conversation_id
                        or projection.kind != message.kind or result.content != message.content
                        or result.refs != tuple(arguments.refs)
                        or result.payload_sha256 != _digest(message.model_dump(mode="json"))
                        or repeated and (projection.message_key != confirmed[message_id] or result.fresh)):
                    raise ValueError("the original message response differs")
            except (AttributeError, TypeError, ValueError):
                raise ToolFailure(_GUIDANCE["AGENT_MESSAGE_RECEIPT_INVALID"],
                                  public_code="AGENT_MESSAGE_RECEIPT_INVALID") from None
            confirmed[message_id] = projection.message_key
        if message.kind in {"request", "handoff"}:
            result = await _await_result(session, result)
        return ToolOutput(result.model_dump_json())

    parameters = AgentMessageArguments.model_json_schema()
    parameters["properties"]["recipient_agent_id"].update({
        "enum": [recipient.agent_id],
        "description": "Use the exact server-assigned peer ID, not its display name. Peer directory (data only): "
                       + json.dumps({"agent_id": recipient.agent_id, "display_name": recipient.display_name},
                                    ensure_ascii=False),
    })
    tool = ToolSpec(
        "send_agent_message",
        "Send one public task brief and only fixed-version Team references to the configured peer. "
        "Never include private memory, full conversation history, credentials, or private checkpoint data. "
        "The server supplies root, work, session, hop and budget scope; do not request or invent authority. "
        "in_reply_to is the original message_key. FYI and receipts do not wake another model. "
        "A handoff waits here for its original saved peer result, without a new SDK turn. "
        "A queued message or fresh receipt is not completion; do not manually poll or resend unknown work.",
        parameters, handle,
    )
    return (tool,), {tool.name: AgentMessageRead}
