"""Responses transport over the original native receipt, executor and checkpoint authority."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import aclosing
from dataclasses import dataclass
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

    def __init__(self, first_preview):
        self.parts, self.ready, self.size, self.full = [], asyncio.Event(), 0, False
        self.first_preview, self.previewed = first_preview, False

    def observe(self, event):
        """The SDK turn's public event observer: tool starts and live preview text become display text."""
        text = _tool_progress_text(event)
        preview = event.get("live_text")
        if isinstance(preview, str) and preview:
            if not self.previewed:
                self.previewed = True
                self.first_preview()
            text = preview if text is None else text + preview
        if text is not None:
            self.add(text)

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


@dataclass
class _Turn:
    """One response over its original RID; ``original`` and ``receipt`` move forward as the handler rereads or writes."""

    stream: ResponseEventStream
    context: ResponseContext
    cancellation_signal: asyncio.Event
    entered: float
    rid: str
    original: object
    receipt: object
    ended: bool = False  # a failure event already ended this response

    def fenced(self):
        return self.cancellation_signal.is_set() and not self.context.client_cancelled

    def signals(self, value):
        value.shutdown = self.context.shutdown.is_set()
        # The cause flag /cancel stamps on this context: a recovered handler's signal is not the one /cancel sets.
        value.cancelled = value.cancelled or self.context.client_cancelled
        value.fenced = value.fenced or (self.fenced() and not value.shutdown)

    def fail(self, code, message):
        self.ended = True
        return self.stream.emit_failed(code=code, message=message)


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
            turn = _Turn(stream, context, cancellation_signal, entered, original.value.receipt.request_id,
                         original, original.value.receipt)
            await self._admit(turn)
            if turn.rid in self.runtime.responses_queued:
                yield stream.emit_failed(code="NATIVE_TURN_QUEUED", message="This original RID is already queued.")
                return
            if await self._takes_queue_place(turn):
                self.runtime.responses_queued.add(turn.rid)
                reserved = turn.rid
                async with aclosing(self._slot_events(turn, NativeTurnControl())) as events:
                    async for event in events:
                        yield event
                if turn.ended:
                    return
                self.runtime.responses_queued.discard(reserved)
                reserved = None
            async with aclosing(self._terminal_events(turn)) as events:
                async for event in events:
                    yield event
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

    async def _admit(self, turn):
        receipt, context = turn.receipt, turn.context
        if receipt.status in {"accepted", "started"}:
            if receipt.status == "accepted" or not context.is_recovery:
                await self.runtime._require_original_session(receipt=receipt)
            if context.shutdown.is_set():
                await context.exit_for_recovery()
        elapsed_timing("native_response_admit", turn.entered, job_id=timing_id(turn.rid))

    async def _takes_queue_place(self, turn):
        """Close a cancelled intent, or refuse a signalled or full queue; True when this response waits for the slot."""
        if turn.receipt.status == "accepted" and (turn.receipt.cancel_requested or turn.context.client_cancelled):
            turn.receipt = await self.runtime._unknown(turn.original, "NATIVE_TURN_CANCELLED")
        if turn.receipt.status != "accepted":
            return turn.receipt.status == "started"
        if turn.fenced():
            return False
        if not self.runtime.response_queue_full():
            return True
        if turn.receipt.human_origin:
            refused = _changed(turn.receipt, status="busy_refused", failure_code="NATIVE_TURN_BUSY")
            current, _ = await self.runtime._cas(turn.original, _changed(turn.original.value, receipt=refused))
            turn.receipt = current.value.receipt
        return False

    async def _slot_events(self, turn, control):
        context = turn.context
        async with self.runtime.execution_slot(turn.original, control, turn.signals) as acquired:
            # As the Invocations chain: before "started", while an earlier turn's CLI still runs this one stays
            # accepted (no second SDK) until that CLI exits, the recovery shutdown defers it, or the bound ends
            # it resendable (below).
            exited = not acquired or await self.runtime._await_runtime_exit(
                lambda: context.shutdown.is_set() or turn.cancellation_signal.is_set() or context.client_cancelled,
                turn.receipt.conversation_id)
            turn.signals(control)
            starting = perf_counter()
            if context.shutdown.is_set():
                await context.exit_for_recovery()
            turn.original = await self.runtime._rid(turn.rid)
            turn.receipt = turn.original.value.receipt
            if turn.receipt.hosted_session_id != self.runtime.identity.session_id:
                # ADR-0009 M9: the session the Agent switched to adopted it; it runs there, untouched here.
                yield turn.fail("NATIVE_SESSION_CHANGED", "The original RID now belongs to another Hosted session.")
                return
            if await self._settled_unstarted(turn, control, acquired, exited):
                return
            async with aclosing(self._start_events(turn, control, starting)) as events:
                async for event in events:
                    yield event

    async def _settled_unstarted(self, turn, control, acquired, exited):
        """Re-enter, close or keep the reread receipt; False only when this response should start its SDK turn."""
        context = turn.context
        if turn.receipt.status != "accepted":
            turn.receipt = await self.runtime._reenter(
                turn.original, failure_code="NATIVE_TURN_UNKNOWN" if turn.fenced() else None,
                recovered=context.is_recovery and not context.client_cancelled)
        elif not exited:
            turn.receipt = await self.runtime._close_unstarted(turn.original)
        elif turn.fenced():
            # The public signal does not identify its cause. Keep this unstarted intent retryable.
            pass
        elif control.cancelled or turn.receipt.cancel_requested:
            turn.receipt = await self.runtime._unknown(turn.original, "NATIVE_TURN_CANCELLED")
        elif acquired:
            return False
        return True

    async def _start_events(self, turn, control, starting):
        runtime, context = self.runtime, turn.context
        meta, _ = await runtime._meta(turn.receipt.conversation_id)
        if meta.inflight_rid != turn.rid:
            yield turn.fail("NATIVE_TURN_NOT_TAKEN", "The original RID does not own the CID slot.")
            return
        started = _changed(turn.original.value, run_token=uuid4().hex, receipt=_changed(
            turn.receipt, status="started", response_id=context.response_id))
        try:
            turn.original, ours = await runtime._cas(turn.original, started)
        except _UnchangedRidWrite:
            turn.receipt = await runtime._unknown(turn.original, "NATIVE_STATE_STORE_UNKNOWN")
        else:
            elapsed_timing("native_start", starting, job_id=timing_id(turn.rid))
            if ours:
                pending = _DisplayBuffer(
                    lambda: elapsed_timing("native_first_preview", turn.entered, job_id=timing_id(turn.rid)))
                control.on_event = pending.observe
                running = asyncio.create_task(runtime._execute_started(turn.original, control, turn.signals))
                try:
                    async for event in _progress_events(turn.stream, pending, running, context.response_id):
                        yield event
                    turn.receipt = await running
                finally:
                    control.on_event = None
                    if not running.done():
                        running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
            elif turn.original.value.receipt.hosted_session_id != runtime.identity.session_id:
                turn.receipt = turn.original.value.receipt
            else:
                turn.receipt = await runtime._reenter(turn.original)
        turn.signals(control)
        if context.shutdown.is_set():
            await context.exit_for_recovery()

    async def _terminal_events(self, turn):
        receipt, context, stream = turn.receipt, turn.context, turn.stream
        if context.is_recovery and not context.client_cancelled:
            from .native_continuations import schedule_continuation
            try:
                await schedule_continuation(self.runtime, receipt)
            except Problem:
                await self.runtime._notify_terminal(receipt)
                raise
        receipt = await self.runtime._notify_terminal(receipt)
        if receipt.status != "completed":
            yield stream.emit_failed(
                code=receipt.failure_code or "NATIVE_TURN_NOT_TAKEN",
                message="The original native receipt did not complete; retain its RID.")
            return
        original_text = await self._result_text(turn.rid, receipt.conversation_id)
        message, item = _message(stream, context.response_id, "final_answer")
        yield message.emit_added(item)
        text = TextContentBuilder(stream, output_index=message.output_index, item_id=message.item_id, content_index=0)
        yield text.emit_added()
        yield text.emit_delta(original_text)
        yield text.emit_text_done(original_text)
        yield text.emit_done()
        yield _message_done(message, item, original_text)
        yield stream.emit_completed()

    async def _result_text(self, rid, conversation_id):
        parts, part = [], 0
        while True:
            result = await self.runtime.result(NativeResult(request_id=rid, conversation_id=conversation_id, part=part))
            parts.append(result.text)
            if result.next_part is None:
                return "".join(parts)
            part = result.next_part


def native_responses_handler_factory(hosted, native_runtime: NativeTurnRuntime | None):
    """N6-0 owns host registration; no alternate host, task, session or executor is created here."""
    if native_runtime is None:
        raise Problem(503, "NATIVE_TURNS_DISABLED",
                      "Responses require FA_NATIVE_TURNS and this process's original native runtime")
    if native_runtime.identity != hosted.identity:
        raise Problem(403, "NATIVE_STATE_SCOPE", "Responses require this process's original native runtime")
    return NativeResponsesHandler(native_runtime).handle
