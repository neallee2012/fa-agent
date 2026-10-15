"""Responses transport over the original native receipt, executor and checkpoint authority."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from uuid import uuid4

from azure.ai.agentserver.core import get_request_context
from azure.ai.agentserver.core.tasks import TaskDeferred
from azure.ai.agentserver.responses import CreateResponse, ResponseContext, ResponseEventStream
from azure.ai.agentserver.responses.models import ItemFieldMessage, MessagePhase
from azure.ai.agentserver.responses.streaming import TextContentBuilder
# 2.2.0's public generic builder validates these IDs, but does not re-export their generator.
from azure.ai.agentserver.responses._id_generator import IdGenerator
from pydantic import ValidationError

from .behavior_native_turn_wire import NativeResult
from .errors import Problem
from .native_responses_wire import (
    NATIVE_RESPONSES_PROFILE, NATIVE_RESPONSE_CID_HEADER, NATIVE_RESPONSE_DIGEST_HEADER,
    NATIVE_RESPONSE_RID_HEADER, NativeResponseHeaders,
)
from .native_turns import NativeTurnControl, NativeTurnRuntime, _UnchangedRidWrite, _changed, timing_id
from .behavior_timing import elapsed_timing
from time import perf_counter

log = logging.getLogger(__name__)


def _tool_progress_text(event):
    progress = event.get("public_progress")
    if (event.get("type") == "tool.execution_start" and isinstance(progress, dict)
            and progress.get("kind") == "tool_started" and isinstance(progress.get("tool_name"), str)):
        return "\n正在使用工具：" + progress["tool_name"][:64] + "\n"
    return None


# Display text in the commentary item is bounded; a longer reply still arrives whole as the saved final answer.
MAX_DISPLAY_BYTES = 128 * 1024


class _DisplayBuffer:
    """Display text waiting for the SSE stream: merged without dropping, until a 128 KiB total limit."""

    def __init__(self):
        self.parts, self.ready, self.size, self.full = [], asyncio.Event(), 0, False

    def add(self, text):
        if self.full:
            return
        size = len(text.encode("utf-8"))
        if self.size + size > MAX_DISPLAY_BYTES:
            self.full = True
            log.warning("FA_NATIVE_PROGRESS_DROPPED reason=display_limit")
            return
        self.size += size
        self.parts.append(text)
        self.ready.set()

    def take(self):
        value, self.parts = "".join(self.parts), []
        self.ready.clear()
        return value


def _message(stream, response_id, phase: MessagePhase):
    identifier = IdGenerator.new_message_item_id(response_id)
    return stream.add_output_item(identifier), ItemFieldMessage(
        type="message", id=identifier, role="assistant", phase=phase, content=[], status="in_progress")


def _message_done(message, item, text):
    return message.emit_done({
        **item, "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}],
    })


async def _progress_events(stream, pending, running, response_id):
    message = text = None
    while True:
        if not pending.parts:
            if running.done():
                break
            waiter = asyncio.create_task(pending.ready.wait())
            try:
                await asyncio.wait((running, waiter), return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            continue
        value = pending.take()
        if message is None:
            message, item = _message(stream, response_id, "commentary")
            yield message.emit_added(item)
            text = TextContentBuilder(
                stream, output_index=message.output_index, item_id=message.item_id, content_index=0)
            yield text.emit_added()
        yield text.emit_delta(value)
    if text is not None:
        yield text.emit_text_done()
        yield text.emit_done()
        yield _message_done(message, item, text.final_text)


class NativeResponsesHandler:
    def __init__(self, runtime: NativeTurnRuntime):
        self.runtime = runtime
        runtime.responses_enabled = True

    async def _original(self, request, context):
        from .behavior_native_turn_wire import NATIVE_EXPECTED_VERSION_HEADER, require_expected_version
        if request.get("background") is not True or request.get("store") is not True:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "native Responses require background=true and store=true")
        headers = context.client_headers
        if not context.is_recovery:
            require_expected_version(headers.get(NATIVE_EXPECTED_VERSION_HEADER), self.runtime.identity.agent_version)
        try:
            address = NativeResponseHeaders(
                request_id=headers.get(NATIVE_RESPONSE_RID_HEADER),
                conversation_id=headers.get(NATIVE_RESPONSE_CID_HEADER),
                submit_digest=headers.get(NATIVE_RESPONSE_DIGEST_HEADER),
            )
        except ValidationError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "the three original native headers are required") from None
        if get_request_context().session_id != self.runtime.identity.session_id:
            raise Problem(403, "NATIVE_STATE_SCOPE", "the platform session differs from this Hosted process")
        original = await self.runtime._rid(address.request_id)
        receipt = original.value.receipt
        if context.is_recovery and receipt.status == "accepted":
            require_expected_version(headers.get(NATIVE_EXPECTED_VERSION_HEADER), self.runtime.identity.agent_version)
        if ((receipt.state_profile, receipt.conversation_id, receipt.submit_digest) != (
                NATIVE_RESPONSES_PROFILE, address.conversation_id, address.submit_digest)):
            raise Problem(409, "NATIVE_RID_CONFLICT", "the response differs from the original native submission")
        text = await context.get_input_text()
        if hashlib.sha256(text.encode("utf-8")).digest() != hashlib.sha256(
                receipt.command.content.encode("utf-8")).digest():
            raise Problem(409, "NATIVE_RID_CONFLICT", "the response input differs from the original text")
        return original

    async def handle(self, request: CreateResponse, context: ResponseContext, cancellation_signal: asyncio.Event):
        stream = ResponseEventStream(response_id=context.response_id, request=request)
        yield stream.emit_created()
        yield stream.emit_in_progress()
        reserved = None
        entered = perf_counter()
        try:
            original = await self._original(request, context)
            rid = original.value.receipt.request_id
            receipt = original.value.receipt
            if receipt.status in {"accepted", "started"}:
                if receipt.status == "accepted" or not context.is_recovery:
                    await self.runtime._require_original_session(receipt=receipt)
                if context.shutdown.is_set():
                    await context.exit_for_recovery()
            elapsed_timing("native_response_admit", entered, job_id=timing_id(rid))
            if rid in self.runtime.responses_queued:
                yield stream.emit_failed(code="NATIVE_TURN_QUEUED", message="This original RID is already queued.")
                return
            if receipt.status == "accepted" and (
                    receipt.cancel_requested or cancellation_signal.is_set() and context.client_cancelled):
                receipt = await self.runtime._unknown(original, "NATIVE_TURN_CANCELLED")
            queue_refused = receipt.status == "accepted" and cancellation_signal.is_set() and not context.client_cancelled
            if receipt.status == "accepted" and not queue_refused and self.runtime.response_queue_full():
                if receipt.human_origin:
                    refused = _changed(receipt, status="busy_refused", failure_code="NATIVE_TURN_BUSY")
                    current, _ = await self.runtime._cas(original, _changed(original.value, receipt=refused))
                    receipt = current.value.receipt
                queue_refused = True
            if receipt.status in {"accepted", "started"} and not queue_refused:
                self.runtime.responses_queued.add(rid)
                reserved = rid
            control = NativeTurnControl()

            def signals(value):
                value.shutdown = context.shutdown.is_set()
                value.cancelled = value.cancelled or (
                    cancellation_signal.is_set() and context.client_cancelled)
                value.fenced = value.fenced or (
                    cancellation_signal.is_set() and not context.client_cancelled and not value.shutdown)

            if reserved is not None:
                async with self.runtime.execution_slot(original, control, signals) as acquired:
                    starting = perf_counter()
                    if context.shutdown.is_set():
                        await context.exit_for_recovery()
                    original = await self.runtime._rid(rid)
                    receipt = original.value.receipt
                    if receipt.hosted_session_id != self.runtime.identity.session_id:
                        # ADR-0009 M9: the session the Agent switched to adopted it; it runs there, untouched here.
                        yield stream.emit_failed(code="NATIVE_SESSION_CHANGED",
                                                 message="The original RID now belongs to another Hosted session.")
                        return
                    if receipt.status != "accepted":
                        receipt = await self.runtime._reenter(
                            original, failure_code="NATIVE_TURN_UNKNOWN" if (
                                cancellation_signal.is_set() and not context.client_cancelled) else None,
                            recovered=context.is_recovery and not context.client_cancelled)
                    elif cancellation_signal.is_set() and not context.client_cancelled:
                        # The public signal does not identify its cause. Keep this unstarted intent retryable.
                        pass
                    elif control.cancelled or receipt.cancel_requested:
                        receipt = await self.runtime._unknown(original, "NATIVE_TURN_CANCELLED")
                    elif acquired:
                        meta, _ = await self.runtime._meta(receipt.conversation_id)
                        if meta.inflight_rid != rid:
                            yield stream.emit_failed(
                                code="NATIVE_TURN_NOT_TAKEN", message="The original RID does not own the CID slot.")
                            return
                        started = _changed(original.value, run_token=uuid4().hex, receipt=_changed(
                            receipt, status="started", response_id=context.response_id))
                        try:
                            original, ours = await self.runtime._cas(original, started)
                        except _UnchangedRidWrite:
                            receipt = await self.runtime._unknown(original, "NATIVE_STATE_STORE_UNKNOWN")
                        else:
                            elapsed_timing("native_start", starting, job_id=timing_id(rid))
                            if ours:
                                pending = _DisplayBuffer()
                                previewed = False

                                def public_event(event):
                                    nonlocal previewed
                                    text = _tool_progress_text(event)
                                    preview = event.get("live_text")
                                    if isinstance(preview, str) and preview:
                                        if not previewed:
                                            previewed = True
                                            elapsed_timing("native_first_preview", entered, job_id=timing_id(rid))
                                        text = preview if text is None else text + preview
                                    if text is not None:
                                        pending.add(text)

                                control.on_event = public_event
                                running = asyncio.create_task(
                                    self.runtime._execute_started(original, control, signals))
                                try:
                                    async for event in _progress_events(stream, pending, running, context.response_id):
                                        yield event
                                    receipt = await running
                                finally:
                                    control.on_event = None
                                    if not running.done():
                                        running.cancel()
                                    await asyncio.gather(running, return_exceptions=True)
                            elif original.value.receipt.hosted_session_id != self.runtime.identity.session_id:
                                receipt = original.value.receipt
                            else:
                                receipt = await self.runtime._reenter(original)
                        signals(control)
                        if context.shutdown.is_set():
                            await context.exit_for_recovery()
            if reserved is not None:
                self.runtime.responses_queued.discard(reserved)
                reserved = None
            if context.is_recovery and not context.client_cancelled:
                from .native_continuations import schedule_continuation
                try:
                    await schedule_continuation(self.runtime, receipt)
                except Problem:
                    await self.runtime._notify_terminal(receipt)
                    raise
            receipt = await self.runtime._notify_terminal(receipt)
            if receipt.status == "completed":
                parts, part = [], 0
                while True:
                    result = await self.runtime.result(NativeResult(
                        request_id=rid, conversation_id=receipt.conversation_id, part=part))
                    parts.append(result.text)
                    if result.next_part is None:
                        break
                    part = result.next_part
                message, item = _message(stream, context.response_id, "final_answer")
                yield message.emit_added(item)
                text = TextContentBuilder(
                    stream, output_index=message.output_index, item_id=message.item_id, content_index=0)
                yield text.emit_added()
                original_text = "".join(parts)
                yield text.emit_delta(original_text)
                yield text.emit_text_done(original_text)
                yield text.emit_done()
                yield _message_done(message, item, original_text)
                yield stream.emit_completed()
            else:
                yield stream.emit_failed(
                    code=receipt.failure_code or "NATIVE_TURN_NOT_TAKEN",
                    message="The original native receipt did not complete; retain its RID.")
        except TaskDeferred:
            if not context.shutdown.is_set():
                raise
            await context.exit_for_recovery()
        except Problem as error:
            code = error.body["error"]["code"]
            log.warning("FA_NATIVE_RESPONSE_REFUSED code=%s", code)
            yield stream.emit_failed(code=code, message=error.body["error"]["message"])
        finally:
            if reserved is not None:
                self.runtime.responses_queued.discard(reserved)


def native_responses_handler_factory(hosted, native_runtime: NativeTurnRuntime | None):
    """N6-0 owns host registration; no alternate host, task, session or executor is created here."""
    if native_runtime is None:
        raise Problem(503, "NATIVE_TURNS_DISABLED",
                      "Responses require FA_NATIVE_TURNS=foundry-native-v1 and this process's original native runtime")
    if native_runtime.identity != hosted.identity:
        raise Problem(403, "NATIVE_STATE_SCOPE", "Responses require this process's original native runtime")
    return NativeResponsesHandler(native_runtime).handle
