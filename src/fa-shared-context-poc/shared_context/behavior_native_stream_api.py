"""Read-only relay of one owner turn's official Responses stream (RTT B live text).

The browser shows ``commentary`` text while the model is still writing; the saved reply (``/status``) always
replaces it. This route only checks the caller (T1), reads the original Hosted receipt for its own
``response_id`` and replays ``GET /responses/{id}?stream=true&starting_after=N`` with the API's managed identity
(the platform serves a session only to the identity that created it). It forwards a fixed field whitelist,
never full response objects, keeps no state and never sends or cancels anything. A dropped stream resumes with
the last ``sequence_number``; a response not created as a stream, or past the official 10-minute replay window,
is refused so the caller keeps reading ``/status``.
https://learn.microsoft.com/azure/foundry/agents/how-to/stream-with-reconnect
"""

import asyncio
import json
import logging
import re
from functools import partial
from time import monotonic

import httpx
from fastapi import Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .behavior_api import _nonfinite, _unique
from .behavior_native_turn_wire import NativeRead
from .errors import Problem
from .native_progress_wire import NativeTurnStatusRead
from .native_responses_wire import NATIVE_RESPONSES_PROFILE

# Under the Azure Container Apps 240-second request limit; the caller resumes with its last sequence number.
STREAM_SECONDS = 230
KEEP_ALIVE_SECONDS = 15
# Bounds what is forwarded and one upstream event. Live text arrives in small merged deltas; larger events are
# skipped unread. The saved reply's own copies come after ``final_answer`` is added, where the relay stops.
MAX_FORWARDED_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
MAX_DELTA_CHARS = 128 * 1024
TERMINAL = frozenset({"response.completed", "response.failed", "response.incomplete", "response.cancelled"})
STREAM_ERRORS = {
    "NATIVE_STREAM_NOT_STARTED": "The turn has not started yet; open the preview again shortly.",
    "NATIVE_STREAM_UNAVAILABLE": "No live preview is available for this turn; keep reading its status.",
}
_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_ITEM = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_PHASES = (None, "commentary", "final_answer")
# Tests replace the network with an httpx mock transport; production uses the default.
_transport = None
log = logging.getLogger(__name__)


def forwarded(value):
    """The whitelisted fields of one official stream event, or None."""
    if not isinstance(value, dict):
        return None
    kind, sequence = value.get("type"), value.get("sequence_number")
    if type(sequence) is not int or sequence < 0:
        return None
    if kind in {"response.created", "response.in_progress"}:
        return {"type": kind, "sequence_number": sequence}
    if kind in TERMINAL:
        response = value.get("response") if isinstance(value.get("response"), dict) else {}
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        status, code = response.get("status"), error.get("code")
        return {"type": kind, "sequence_number": sequence,
                **({"status": status} if isinstance(status, str) and _CODE.fullmatch(status) else {}),
                **({"code": code} if isinstance(code, str) and _CODE.fullmatch(code) else {})}
    if kind == "response.output_item.added":
        item = value.get("item")
        if (not isinstance(item, dict) or item.get("type") != "message" or not isinstance(item.get("id"), str)
                or _ITEM.fullmatch(item["id"]) is None or item.get("phase") not in _PHASES):
            return None
        return {"type": kind, "sequence_number": sequence, "item_id": item["id"], "phase": item.get("phase")}
    if kind == "response.output_text.delta":
        item, delta = value.get("item_id"), value.get("delta")
        # Live text arrives in small merged deltas; a reply-sized one is the saved answer, read from /status.
        if (not isinstance(item, str) or _ITEM.fullmatch(item) is None or not isinstance(delta, str)
                or len(delta) > MAX_DELTA_CHARS):
            return None
        return {"type": kind, "sequence_number": sequence, "item_id": item, "delta": delta}
    return None


def stream_source(gateway, principal, conversation_id, request_id, starting_after):
    """T1, then the original receipt: only its own response is replayed, never a caller-chosen one."""
    from .behavior_native_responses_api import platform_client

    # A read: the selector window and a recently confirmed reader apply, as for the other reads and polls.
    control, client = gateway._read_context(principal)
    gateway._legacy(control, conversation_id)
    original, _ = gateway._invoke(client, NativeRead(request_id=request_id))
    if original.state_profile != NATIVE_RESPONSES_PROFILE or original.conversation_id != conversation_id:
        raise Problem(409, "NATIVE_STATE_SCOPE", "the preview must name the original Responses turn")
    gateway._same_session(client, original)
    if original.response_id is None:
        if original.status == "accepted":
            raise Problem(409, "NATIVE_STREAM_NOT_STARTED", "the original turn has not started")
        raise Problem(409, "NATIVE_STREAM_UNAVAILABLE", "the original turn has no Responses stream")
    return platform_client(gateway.composition, client).stream_source(original.response_id, starting_after)


async def open_stream(source):
    # One long-lived read per watched turn; the relay's own keep-alive covers idle reasoning upstream.
    client = httpx.AsyncClient(timeout=httpx.Timeout(STREAM_SECONDS, connect=10), follow_redirects=False,
                               transport=_transport)
    try:
        response = await client.send(
            client.build_request("GET", source.url, params=source.params, headers=source.headers), stream=True)
    except httpx.HTTPError as error:
        await client.aclose()
        log.warning("NATIVE_STREAM_UNAVAILABLE phase=request exception_type=%s", type(error).__name__)
        raise Problem(503, "NATIVE_STREAM_UNAVAILABLE", "the original stream could not be opened") from None
    media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if (response.status_code != 200 or media != "text/event-stream"
            or response.headers.get("content-encoding", "identity").lower() != "identity"):
        status = response.status_code
        await response.aclose()
        await client.aclose()
        log.warning("NATIVE_STREAM_UNAVAILABLE phase=response status=%s", status)
        # 400: not created as a stream or past its replay window; 404: no retained events.
        raise Problem(409 if status in {400, 404} else 503, "NATIVE_STREAM_UNAVAILABLE",
                      "the original stream is not available")
    return client, response


async def relay(client, response):
    chunks = asyncio.Queue(maxsize=16)

    async def pump():
        try:
            async for chunk in response.aiter_bytes():
                await chunks.put(chunk)
        except httpx.HTTPError as error:
            log.warning("NATIVE_STREAM_ENDED exception_type=%s", type(error).__name__)
        # Reached only when the upstream ended by itself; a cancelled pump leaves without waiting on the queue.
        await chunks.put(None)

    reader = asyncio.create_task(pump())
    frames, sent, started = _Frames(MAX_EVENT_BYTES), 0, monotonic()
    written = started
    try:
        while (left := STREAM_SECONDS - (monotonic() - started)) > 0:
            idle = KEEP_ALIVE_SECONDS - (monotonic() - written)
            if idle <= 0:
                written = monotonic()
                yield ": keep-alive\n\n"
                continue
            try:
                chunk = await asyncio.wait_for(chunks.get(), min(idle, left))
            except TimeoutError:
                continue
            if chunk is None:
                return
            for data in frames.feed(chunk):
                try:
                    value = forwarded(json.loads(data, object_pairs_hook=_unique, parse_constant=_nonfinite))
                except (ValueError, TypeError):
                    value = None
                if value is None:
                    continue
                frame = (f"id: {value['sequence_number']}\n"
                         f"data: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}\n\n")
                sent += len(frame.encode("utf-8"))
                if sent > MAX_FORWARDED_BYTES:
                    return
                written = monotonic()
                yield frame
                # After final_answer only the saved reply's own (reply-sized) copies follow: read /status instead.
                if value["type"] in TERMINAL or value.get("phase") == "final_answer":
                    return
    finally:
        reader.cancel()
        try:
            await asyncio.gather(reader, return_exceptions=True)
        finally:
            try:
                await response.aclose()
            finally:
                await client.aclose()


class _Frames:
    """The data of each complete SSE frame, in linear time; a frame over ``limit`` is skipped, never buffered.

    The official encoder ends frames with a blank line (``\\n\\n``), azure-ai-agentserver-responses 2.2.0
    ``streaming/_sse.py``."""

    def __init__(self, limit):
        self.buffer, self.scan, self.skipping, self.limit = bytearray(), 0, False, limit

    def feed(self, chunk):
        self.buffer += chunk
        while (end := self.buffer.find(b"\n\n", self.scan)) >= 0:
            frame = bytes(self.buffer[:end])
            del self.buffer[:end + 2]
            self.scan = 0
            if self.skipping:
                self.skipping = False
                continue
            data = b"\n".join(line[5:].removeprefix(b" ") for line in frame.split(b"\n") if line.startswith(b"data:"))
            if data:
                yield data
        self.scan = max(0, len(self.buffer) - 1)
        if len(self.buffer) > self.limit:
            self.skipping = True
            del self.buffer[:-1]
            self.scan = 0


def _raise(error):
    raise error


def install_native_stream_routes(app, gateway, identity, read_request, reply_read, prefix):
    @app.get(prefix + "/conversations/{conversation_id}/requests/{request_id}/stream")
    async def stream_turn(conversation_id: str, request_id: str, request: Request,
                          starting_after: int | None = Query(None, ge=0), principal=Depends(identity)):
        await read_request(request, queries=("starting_after",))
        try:
            NativeTurnStatusRead(conversation_id=conversation_id, request_id=request_id)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original CID and RID are required") from None
        try:
            source = await run_in_threadpool(partial(
                stream_source, gateway, principal, conversation_id, request_id, starting_after))
            client, response = await open_stream(source)
        except Problem as error:
            code = error.body["error"]["code"]
            if code not in STREAM_ERRORS:
                # The shared native read reply keeps each original refusal code and retry hint.
                return await reply_read(partial(_raise, error))
            log.warning("NATIVE_TURN_REFUSED code=%s", code)
            return JSONResponse(status_code=error.status, headers={"Cache-Control": "no-store"}, content={"error": {
                "code": code, "message": STREAM_ERRORS[code],
                "automatic_retry": code == "NATIVE_STREAM_NOT_STARTED", "reconcile_original": False}})
        return StreamingResponse(relay(client, response), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
