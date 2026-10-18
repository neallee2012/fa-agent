"""Shared admission and Responses dispatch; owner and T5 callers retain their own authorization."""

import logging
from functools import partial
from typing import Literal
from uuid import uuid4

from pydantic import TypeAdapter

from .behavior_agent_versions import current_hosted_version, hosted_model_switch
from .behavior_foundry_responses import ConversationMode, FoundryResponsesClient, HistoryKind
from .behavior_native_turn_wire import (
    NativeAgentReceive, NativeRoutineSubmit, NativeContinuationSubmit, NativeConversationRead, NativeRead, NativeSubmit,
)
from .behavior_wire import WireModel
from .errors import Problem
from .native_responses_wire import (
    NATIVE_RESPONSES_PROFILE, NATIVE_STATE_PROFILE, NativeResponseHeaders, NativeResponseId, channel,
)

PREFIX = "/work-policy/v1/responses-turns"
# D1/D2 select Conversations; each Agent still needs the original build and template eligibility.
RESPONSES_CONVERSATION_MODE: ConversationMode | None = "conversation"


class PlatformHistoryMessage(WireModel):
    item_id: NativeResponseId
    platform_role: Literal["user", "assistant"]
    text: str | None
    state: Literal["available", "unconfirmed"]


class PlatformHistoryPage(WireModel):
    schema_version: Literal["fa-platform-history-v1"] = "fa-platform-history-v1"
    source_kind: HistoryKind
    source_id: NativeResponseId
    messages: tuple[PlatformHistoryMessage, ...]
    next_after: NativeResponseId | None
    previous_response_id: NativeResponseId | None = None
    fallback: Literal["native-result"] | None = None


def turn_profile(capture, *, original=None, conversation=None):
    """The profile a turn is sent with. An original keeps its own (it is inside ``submit_digest``); a conversation
    gives only its channel: ADR-0009 2d-i sends v1 until 2d-ii, also into a conversation an H2d image labelled v2."""
    if original is not None:
        return original.state_profile
    if conversation is not None:
        return NATIVE_RESPONSES_PROFILE if channel(conversation.state_profile) == "responses" else NATIVE_STATE_PROFILE
    return capture.template.settings.state_profile


def platform_client(composition, client):
    """``client`` was resolved for this request by the caller's admission; Hosted checks Responses and version."""
    if RESPONSES_CONVERSATION_MODE is None:
        raise Problem(503, "NATIVE_TURNS_DISABLED", "Responses routing has not been selected for this deployment")
    return FoundryResponsesClient(
        client, team_id=composition.runtime.work_policy.policy.template.team_id,
        conversation_mode=RESPONSES_CONVERSATION_MODE)


def template_responses_options(composition, principal):
    from .behavior_agent_template import TemplateResponsesOptions
    from .behavior_native_turn_api import NativeTurnGateway, configured_native_turns

    mode = RESPONSES_CONVERSATION_MODE
    if mode is None:
        return TemplateResponsesOptions()
    options = TemplateResponsesOptions(history_source={
        "conversation": "conversation", "previous_response_id": "response",
    }[mode])
    if not configured_native_turns():
        return options
    _, client = NativeTurnGateway(composition, enabled=True)._context(principal, "read", prepared=False)
    try:
        current_hosted_version(composition.runtime, client.target, require_responses=True)
    except Problem as error:
        if error.status != 409 or error.body["error"]["code"] != "NATIVE_TURNS_DISABLED":
            raise
        return options
    return TemplateResponsesOptions(enabled=True, history_source=options.history_source,
                                    model_switch=hosted_model_switch(composition.runtime, client.target))


def _original_after_dispatch(client, expected):
    from .behavior_native_turn_api import NativeTurnGateway

    value, response = NativeTurnGateway._invoke(client, NativeRead(request_id=expected.request_id))
    if (value.submit_digest, value.conversation_id, value.state_profile) != (
            expected.submit_digest, expected.conversation_id, NATIVE_RESPONSES_PROFILE):
        raise Problem(409, "NATIVE_RID_CONFLICT", "the response dispatch must retain its original receipt")
    NativeTurnGateway._same_session(client, value)
    return value, response


def _send_response(client, platform, receipt, *, before_send, confirm=True, stream=False):
    if receipt.status != "accepted" or receipt.response_id is not None:
        return receipt, None
    if receipt.dispatch is None:
        raise Problem(502, "NATIVE_STATE_INVALID", "an accepted Responses receipt requires its dispatch state")
    if receipt.dispatch.state != "needs_response":
        return receipt, None
    # A same-CID POST already running is refused by the SDK with 409 conversation_locked (pending, not an error).
    acknowledged = None
    try:
        acknowledged = platform.start(
            NativeResponseHeaders(request_id=receipt.request_id, conversation_id=receipt.conversation_id,
                                  submit_digest=receipt.submit_digest),
            receipt.command.content,
            previous_response_id=(receipt.dispatch.previous_response_id
                                  if platform.mode == "previous_response_id" else None),
            before_send=before_send, stream=stream,
        )
    except Problem as error:
        if (error.body["error"]["code"] != "NATIVE_TURN_BUSY"
                or error.body["error"].get("upstream_error_code") != "conversation_locked"):
            raise
        logging.getLogger(__name__).warning("NATIVE_RESPONSE_PENDING code=%s status=%s",
                                           error.body["error"]["code"], error.status)
    if not confirm:
        return receipt, None
    # The platform POST acknowledgment is not the FA started CAS, nor authority to bind its response ID.
    observed, upstream = _original_after_dispatch(client, receipt)
    if (acknowledged is not None and observed.status == "accepted"
            and (observed.dispatch is None or observed.dispatch.state == "needs_response")):
        logging.getLogger(__name__).warning("NATIVE_RESPONSE_PENDING code=NATIVE_RESPONSE_UNCONFIRMED response_status=%s",
                                           acknowledged.status)
        raise Problem(503, "NATIVE_RESPONSE_UNCONFIRMED", "the original Responses handoff is not confirmed")
    return observed, upstream


def _advance_inflight(client, platform, receipt, *, before_send, confirm=True):
    from .behavior_native_turn_api import NativeTurnGateway

    if receipt.status == "busy_refused" and receipt.failure_code == "NATIVE_TURN_BUSY":
        conversation, _ = NativeTurnGateway._invoke(client, NativeConversationRead(
            conversation_id=receipt.conversation_id))
        if conversation.state_profile != NATIVE_RESPONSES_PROFILE:
            raise Problem(409, "NATIVE_STATE_SCOPE", "the busy conversation must retain its Responses lane")
        holder_id = conversation.inflight_request_id
    else:
        holder_id = None if receipt.dispatch is None else receipt.dispatch.inflight_request_id
    if holder_id is None or holder_id == receipt.request_id:
        return
    holder, _ = NativeTurnGateway._invoke(client, NativeRead(request_id=holder_id))
    if holder.conversation_id != receipt.conversation_id or holder.state_profile != NATIVE_RESPONSES_PROFILE:
        raise Problem(409, "NATIVE_STATE_SCOPE", "the in-flight receipt belongs to another conversation")
    if holder.hosted_session_id != client.target.session_id:
        # ADR-0009 §7a (s): ``client`` is the session that just admitted this message, so a holder of another SID was
        # left by an earlier one. It is never sent anywhere (that session is no longer called); this message waits
        # behind it in Hosted, which releases the slot. Not an error to the owner (no NATIVE_SESSION_CHANGED resend).
        logging.getLogger(__name__).info("NATIVE_RESPONSE_HOLDER_LEFT conversation_turn_waits=true")
        return
    NativeTurnGateway._same_session(client, holder)
    _send_response(client, platform, holder, before_send=before_send, confirm=confirm)


def _send_after_reply(client, platform, admitted, *, advance, before_send, refused=None):
    """The owner already has Hosted's accepted receipt; the same RID/body is POSTed once after the reply.

    ``client`` is the one this admission confirmed. The workbench reads that receipt and reconciles the same RID if
    it still needs its Responses POST. ``refused`` sees a refusal as the synchronous send's caller would.
    Only the owner's own admitted turn is created as a replayable stream (RTT B live text); a lock inside that stream
    reads as unconfirmed, which this path only logs while the receipt keeps the same RID for reconcile. Advancing
    another holder (possibly a T5, routine or continuation turn) keeps ``stream: false``."""
    try:
        if advance:
            _advance_inflight(client, platform, admitted, before_send=before_send, confirm=False)
        else:
            _send_response(client, platform, admitted, before_send=before_send, confirm=False, stream=True)
    except Problem as error:
        logging.getLogger(__name__).warning("NATIVE_RESPONSE_PENDING code=%s status=%s after_reply=true",
                                           error.body["error"]["code"], error.status)
        if refused is not None:
            refused(error)


def submit_native_turn(composition, client, command, capture, *, before_send=None, source=None, original=None,
                       conversation=None, continuation=None, routine=None, defer=None):
    """One admitted original command, one CID slot, at most one Responses POST; no wait or new queue.

    ``before_send`` is an optional cheap producer check; the owner's entry check is not repeated per send.
    ``defer`` (owner ``/submit`` only) takes the Responses POST and runs it after the accepted receipt is returned."""
    from .behavior_native_turn_api import NativeTurnGateway

    if (original is not None and original.agent_id != client.target.agent_id
            or conversation is not None and (
                conversation.agent_id != client.target.agent_id or conversation.conversation_id != command.conversation_id)):
        raise Problem(403, "NATIVE_STATE_SCOPE", "the original material belongs to another Agent or conversation")
    if command.conversation_id is not None and original is None and conversation is None:
        raise Problem(409, "NATIVE_CONVERSATION_MISSING", "the original conversation is required before admitting a follow-up")
    if original is None:
        from .behavior_native_capture import start_pinned
        # ADR-0009 2d-i: no receipt holds this capture yet, so it names the image of the SID it goes to now.
        capture = start_pinned(capture, composition.runtime, client.target)
    profile = turn_profile(capture, original=original, conversation=conversation)
    read_only = original is not None and original.status != "accepted"
    platform = (platform_client(composition, client)
                if profile == NATIVE_RESPONSES_PROFILE and not read_only else None)
    new_cid = None
    if profile == NATIVE_RESPONSES_PROFILE and command.conversation_id is None:
        new_cid = (original.conversation_id if original is not None else
                   platform.create_conversation(before_send=before_send) if platform.mode == "conversation"
                   else "native-rsp-" + uuid4().hex)
    values = {"command": command, "capture": capture, "state_profile": profile, "new_conversation_id": new_cid}
    request = (NativeContinuationSubmit(
        command=command, capture=capture, state_profile=profile, source=source, routine=routine, continuation=continuation)
        if continuation is not None else
        NativeRoutineSubmit(command=command, capture=capture, state_profile=profile, routine=routine)
        if routine is not None else
        NativeSubmit(**values) if source is None else NativeAgentReceive(**values, source=source))
    if original is not None and request.sha256 != original.submit_digest:
        raise Problem(409, "NATIVE_RID_CONFLICT", "the original request retains different material or a different lane")
    if read_only:
        return original, None
    if before_send is not None:
        before_send()
    admitted, upstream = NativeTurnGateway._invoke(client, request)
    if profile == NATIVE_STATE_PROFILE:
        return admitted, upstream
    if source is not None and original is None and continuation is None:
        # The existing durable T5 follow owns subsequent attempts, including their finite error budget.
        return admitted, upstream
    advance = admitted.status == "busy_refused" or admitted.dispatch is not None and admitted.dispatch.state == "not_taken"
    if defer is not None:
        defer(partial(_send_after_reply, client, platform, admitted, advance=advance, before_send=before_send))
        return admitted, upstream
    if advance:
        _advance_inflight(client, platform, admitted, before_send=before_send)
        return admitted, upstream
    observed, _ = _send_response(client, platform, admitted, before_send=before_send)
    return observed, upstream


def reconcile(gateway, principal, command):
    original, response = gateway.read(principal, NativeRead(request_id=command.request_id))
    if original.state_profile != NATIVE_RESPONSES_PROFILE:
        raise Problem(409, "NATIVE_STATE_SCOPE", "the original RID is not a Responses turn")
    if original.command != command:
        raise Problem(409, "NATIVE_RID_CONFLICT", "reconcile must retain the original command")
    if original.status != "accepted" or original.dispatch is not None and original.dispatch.state == "already_queued":
        return original, response
    return gateway.submit(principal, command, state_profile=NATIVE_RESPONSES_PROFILE, original=original,
                          response=response)


def cancel(gateway, principal, command):
    control, client = gateway._turn_context(principal, "read", command.request_id)
    gateway._legacy(control, command.conversation_id)
    original, response = gateway._invoke(client, NativeRead(request_id=command.request_id))
    if original.state_profile != NATIVE_RESPONSES_PROFILE or original.conversation_id != command.conversation_id:
        raise Problem(409, "NATIVE_STATE_SCOPE", "cancellation must name the original Responses turn")
    if original.status not in {"accepted", "started"}:
        if original.status == "unknown" and original.continuation_available:
            return gateway._accounted(control, gateway._invoke(client, command))
        return gateway._accounted(control, (original, response))
    gateway._same_session(client, original)
    cancelled, response = gateway._invoke(client, command)
    if cancelled.response_id is not None and cancelled.status == "started":
        platform = platform_client(gateway.composition, client)
        platform.cancel(cancelled.response_id, before_send=lambda: gateway._context(principal, "read", prepared=False))
        cancelled, response = _original_after_dispatch(client, cancelled)
    return gateway._accounted(control, (cancelled, response))


def check_history_scope(gateway, principal, kind: HistoryKind, identifier):
    """One T1-authorized scope check; replacing metadata verification never changes the routes."""
    _, client = gateway._context(principal, "read", prepared=False)
    platform = platform_client(gateway.composition, client)
    return platform, platform.history_record(kind, identifier)


def _history_messages(items, *, response_complete=True):
    messages = []
    for item in items:
        if not isinstance(item, dict):
            raise Problem(502, "NATIVE_STATE_INVALID", "the platform history contains an invalid item")
        if item.get("type") != "message" or item.get("role") not in {"user", "assistant"}:
            continue
        role = item["role"]
        if role == "assistant" and item.get("phase") == "commentary":
            continue
        content = item.get("content")
        complete = (isinstance(content, list) and all(
            isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}
            and isinstance(part.get("text"), str) for part in content))
        if role == "assistant":
            complete = (complete and response_complete and item.get("phase") == "final_answer"
                        and item.get("status") in {None, "completed"})
        try:
            messages.append(PlatformHistoryMessage(
                item_id=item["id"], platform_role=role,
                text="".join(part["text"] for part in content) if complete else None,
                state="available" if complete else "unconfirmed"))
        except (KeyError, ValueError, TypeError):
            raise Problem(502, "NATIVE_STATE_INVALID", "the platform message does not have a usable original identity") from None
    return messages


def history(gateway, principal, kind: HistoryKind, identifier, *, after=None, limit=20):
    platform, original = check_history_scope(gateway, principal, kind, identifier)
    page = platform.history_items(kind, identifier, after=after, limit=limit)
    messages = _history_messages(page["data"])
    previous = None
    if kind == "response" and not page["has_more"]:
        output = original.get("output")
        if not isinstance(output, list):
            raise Problem(502, "NATIVE_STATE_INVALID", "the original response has no complete output list")
        messages.extend(_history_messages(output, response_complete=original["status"] == "completed"))
        try:
            if original.get("previous_response_id") is not None:
                previous = TypeAdapter(NativeResponseId).validate_python(original["previous_response_id"])
                if previous == identifier:
                    raise ValueError()
        except ValueError:
            raise Problem(502, "NATIVE_STATE_INVALID", "the response chain did not advance") from None
    if len({message.item_id for message in messages}) != len(messages):
        raise Problem(502, "NATIVE_STATE_INVALID", "the platform history repeats an item identity")
    return PlatformHistoryPage(
        source_kind=kind, source_id=identifier, messages=tuple(messages),
        next_after=page["last_id"] if page["has_more"] else None, previous_response_id=previous,
        fallback="native-result" if any(message.state == "unconfirmed" for message in messages) else None,
    ), None
