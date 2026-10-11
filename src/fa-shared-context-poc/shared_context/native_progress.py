"""Best-effort bounded public SDK snapshots in the original Agent's N1 store."""

import asyncio
import hashlib
import json
import logging

from .behavior_assistant_wire import PublicProgress, PublicProgressEvent, PublicReasoningSummary
from .errors import Problem
from .native_progress_wire import NativeProgressSnapshot, progress_snapshot


log = logging.getLogger(__name__)

# SDK 1.0.13 stop() can join two threads for one second each on this event loop.
_SAVE_TIMEOUT_SECONDS = 5
_FINISH_WAIT_SECONDS = 1


def _key(rid):
    return "progress:" + hashlib.sha256(rid.encode("utf-8")).hexdigest()


# A preview is shown only while it fits one reply page; a longer reply waits for its durable result.
PREVIEW_MAX_BYTES = 128 * 1024
PREVIEW_KIND = "fa-native-reply-preview-v1"


def _preview_key(rid):
    return "preview:" + hashlib.sha256(rid.encode("utf-8")).hexdigest()


async def save_preview(runtime, receipt, text):
    """Best effort, written once before the SDK stops: the reply this turn is about to save.

    A preview is display data only. It is not the result, not delivered, and never read by memory, delivery,
    recovery or the checkpoint; the durable answer stays the completed receipt. Never raises.
    """
    from .native_turns import TURNS_STORE, _UNCERTAIN

    raw = text.encode("utf-8")
    if not text or len(raw) > PREVIEW_MAX_BYTES:
        return
    value = {"kind": PREVIEW_KIND, "request_id": receipt.request_id, "submit_digest": receipt.submit_digest,
             "text": text, "text_sha256": hashlib.sha256(raw).hexdigest()}
    try:
        async with runtime._store(TURNS_STORE) as store:
            await store.create_item(_preview_key(receipt.request_id), value)
    except (Problem, *_UNCERTAIN) as error:
        log.warning("FA_NATIVE_PREVIEW_UNSAVED exception_type=%s", type(error).__name__)


async def read_preview(runtime, receipt):
    """The saved preview of a still-started turn, or None; an invalid or foreign item is ignored."""
    from .native_turns import TURNS_STORE

    if receipt.status != "started":
        return None
    async with runtime._store(TURNS_STORE) as store:
        item = await store.get_item(_preview_key(receipt.request_id))
    value = None if item is None else item.value
    if (not isinstance(value, dict) or value.get("kind") != PREVIEW_KIND
            or value.get("request_id") != receipt.request_id or value.get("submit_digest") != receipt.submit_digest
            or not isinstance(value.get("text"), str)
            or hashlib.sha256(value["text"].encode("utf-8")).hexdigest() != value.get("text_sha256")):
        return None
    return value["text"]


def _checked(item, receipt):
    from .native_turns import _decode

    value = _decode(item.value, NativeProgressSnapshot)
    if (value.agent_id, value.hosted_session_id, value.conversation_id, value.request_id, value.submit_digest) != (
            receipt.agent_id, receipt.hosted_session_id, receipt.conversation_id,
            receipt.request_id, receipt.submit_digest):
        raise Problem(403, "NATIVE_STATE_SCOPE", "the public activity belongs to another original turn")
    if value.progress.reasoning_summary.availability == "provided" and (
            receipt.capture.template.settings.model_provider != "openai-responses"
            or receipt.capture.reasoning_summary is None):
        raise Problem(502, "NATIVE_STATE_INVALID", "the original turn did not enable public reasoning summaries")
    return value


async def read_progress(runtime, command):
    from .native_turns import TURNS_STORE

    receipt = (await runtime._rid(command.request_id)).value.receipt
    runtime._address(receipt, command.conversation_id)
    async with runtime._store(TURNS_STORE) as store:
        item = await store.get_item(_key(receipt.request_id))
    value = progress_snapshot(receipt, PublicProgress(), recorded=False) if item is None else _checked(item, receipt)
    recorder = runtime.progress_recorder
    failed = recorder is not None and recorder.receipt.request_id == receipt.request_id and recorder.failed
    if not value.finished and (failed or receipt.turn_number is not None
                              and receipt.status not in {"accepted", "started"}):
        return progress_snapshot(
            receipt, value.progress, recorded=value.recorded, finished=value.finished,
            truncated=value.truncated, unconfirmed=True)
    return value


class NativeProgressRecorder:
    def __init__(self, runtime, receipt):
        self.runtime, self.receipt = runtime, receipt
        self.snapshot = progress_snapshot(receipt, PublicProgress())
        self.pending = None
        self.task = None
        self.failed = False
        self.invalid = False
        self.unconfirmed_write = None
        self.closed = False

    def observe(self, event):
        if self.closed or self.invalid or "public_progress" not in event:
            return
        try:
            value = PublicProgressEvent.model_validate_json(json.dumps(event["public_progress"]))
            previous = self.snapshot.progress
            if previous.events and value.seq <= previous.events[-1].seq:
                raise ValueError("The original public sequence did not advance")
            summary = previous.reasoning_summary
            if value.reasoning_summary is not None:
                if self.receipt.capture.template.settings.model_provider != "openai-responses" or (
                        self.receipt.capture.reasoning_summary is None):
                    raise ValueError("This original turn did not enable public reasoning summaries")
                summary = PublicReasoningSummary(
                    availability="provided", source="openai-responses", text=value.reasoning_summary)
            self.snapshot = progress_snapshot(
                self.receipt, PublicProgress(events=(*previous.events, value), reasoning_summary=summary),
                truncated=self.snapshot.truncated)
        except (ValueError, TypeError):
            self.failed = True
            self.invalid = True
            log.warning("FA_NATIVE_PROGRESS_UNCONFIRMED reason=invalid_public_event")
            return
        self.pending = self.snapshot
        if not self.failed and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._flush())

    async def _save(self, snapshot):
        from .native_turns import TURNS_STORE, _UNCERTAIN, _json

        async with self.runtime._store(TURNS_STORE) as store:
            key = _key(self.receipt.request_id)
            item = await store.get_item(key)
            previous = None if item is None else _checked(item, self.receipt)
            if self.unconfirmed_write is not None:
                if previous != self.unconfirmed_write:
                    raise Problem(503, "NATIVE_PROGRESS_UNCONFIRMED", "the original public activity write is unconfirmed")
                self.unconfirmed_write = None
            if previous is not None:
                if previous.finished:
                    return
                old_sequence = previous.progress.events[-1].seq if previous.progress.events else 0
                new_sequence = snapshot.progress.events[-1].seq if snapshot.progress.events else 0
                if old_sequence > new_sequence or previous == snapshot:
                    return
            # Retain the exact intent across a lost ACK, failed readback or cancellation.
            self.unconfirmed_write = snapshot
            try:
                if item is None:
                    await store.create_item(key, _json(snapshot))
                else:
                    await store.set_item(key, _json(snapshot), if_match=item.etag)
            except _UNCERTAIN:
                pass
            observed = await store.get_item(key)
            if observed is None or _checked(observed, self.receipt) != snapshot:
                raise Problem(503, "NATIVE_PROGRESS_UNCONFIRMED", "the original public activity write is unconfirmed")
            self.unconfirmed_write = None

    async def _flush(self):
        from .native_turns import _UNCERTAIN

        while self.pending is not None and not self.invalid:
            snapshot, self.pending = self.pending, None
            try:
                async with asyncio.timeout(_SAVE_TIMEOUT_SECONDS):
                    await self._save(snapshot)
            except (Problem, ValueError, *_UNCERTAIN) as error:
                self.failed = True
                log.warning("FA_NATIVE_PROGRESS_UNCONFIRMED exception_type=%s", type(error).__name__)
                if not self.closed or snapshot.finished:
                    return
            else:
                self.failed = self.invalid

    async def finish(self):
        self.closed = True
        if not self.invalid:
            self.pending = progress_snapshot(
                self.receipt, self.snapshot.progress, finished=True, truncated=self.snapshot.truncated)
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self._flush())
        if self.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=_FINISH_WAIT_SECONDS)
            except TimeoutError:
                self.failed = True
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
                log.warning("FA_NATIVE_PROGRESS_UNCONFIRMED reason=finish_timeout")
