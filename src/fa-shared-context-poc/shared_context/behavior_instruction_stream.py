"""Transient answer previews, never saved-result or execution-completion evidence."""
from collections import OrderedDict
from datetime import datetime, timezone
import json
import re
from threading import RLock
from time import monotonic

import httpx
import asyncio

from .errors import Problem


class BoundedStream(httpx.AsyncByteStream):
    def __init__(self, response, *, close=None, check=None, on_chunk=None):
        self.response, self.close, self.check, self.on_chunk = response, close, check, on_chunk
        self.closed = False

    async def __aiter__(self):
        size = 0
        try:
            async for chunk in self.response.aiter_bytes():
                if self.check is not None:
                    self.check()
                size += len(chunk)
                if size > 4 * 1024 * 1024:
                    raise Problem(413, "ASSISTANT_MODEL_STREAM_LIMIT", "original model stream exceeds its byte limit")
                if self.on_chunk is not None:
                    self.on_chunk(chunk)
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self):
        if not self.closed:
            self.closed = True
            try:
                await self.response.aclose()
            finally:
                if self.close is not None:
                    await self.close()


class AsyncFromSyncTransport(httpx.AsyncBaseTransport):
    """Use the caller-supplied local transport without opening a real socket."""
    def __init__(self, transport):
        self.transport = transport

    async def handle_async_request(self, request):
        response = await asyncio.to_thread(self.transport.handle_request, request)
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                iterator = response.iter_bytes()
                while True:
                    chunk = await asyncio.to_thread(next, iterator, None)
                    if chunk is None:break
                    yield chunk
            async def aclose(self):
                await asyncio.to_thread(response.close)
        return httpx.Response(response.status_code,headers=response.headers,request=request,stream=Body())


class LiveReplies:
    """Optional process-local preview: missing data never substitutes for a reply."""
    def __init__(self):
        self.values = OrderedDict()
        self.lock = RLock()

    def begin(self, job_id, request_id):
        with self.lock:
            self.values[job_id] = {"request": request_id, "text": "", "seq": 0, "seen": monotonic()}
            self.values.move_to_end(job_id)
            while len(self.values) > 64:
                self.values.popitem(last=False)

    def append(self, job_id, request_id, text):
        with self.lock:
            value = self.values.get(job_id)
            if value is None or value["request"] != request_id:
                return
            # Only the preview is bounded. The original result remains complete.
            value["text"] = (value["text"] + text)[:32768]
            value["seq"] += 1
            value["seen"] = monotonic()
            value["at"] = datetime.now(timezone.utc).isoformat()

    def read(self, job_id):
        with self.lock:
            value = self.values.get(job_id)
            if value is None or not value["text"]:
                return None
            if monotonic() - value["seen"] > 600:
                del self.values[job_id]
                return None
            return {"text": value["text"], "sequence": value["seq"], "observed_at": value["at"],
                    "durable": False, "complete": False}


class SseDataEvents:
    """Only complete SSE data events; never reconnect, rewrite, or dispatch work."""

    _line_end = re.compile(br"\r\n|\r|\n")

    def __init__(self):
        self.pending = b""
        self.data = []
        self.first_line = True
        self.after_cr = False

    def feed(self, chunk):
        if not chunk:
            return
        if self.after_cr:
            chunk = chunk.removeprefix(b"\n")
            self.after_cr = False
        raw = self.pending + chunk
        start = 0
        for end in self._line_end.finditer(raw):
            line = raw[start:end.start()]
            start = end.end()
            self.after_cr = end.group() == b"\r" and start == len(raw)
            if self.first_line:
                line = line.removeprefix(b"\xef\xbb\xbf")
                self.first_line = False
            if not line:
                if self.data:
                    value = b"\n".join(self.data)
                    self.data.clear()
                    yield value
                continue
            field, _, value = line.partition(b":")
            if field == b"data":
                self.data.append(value.removeprefix(b" "))
        self.pending = raw[start:]


class AnswerEvents:
    def __init__(self, append, *, wire_api="completions"):
        self.events = SseDataEvents()
        self.append = append
        self.wire_api = wire_api

    def feed(self, chunk):
        for data in self.events.feed(chunk):
            if data.strip() == b"[DONE]":
                continue
            try:
                packet = json.loads(data)
            except (ValueError, UnicodeError) as error:
                raise Problem(502, "ASSISTANT_MODEL_STREAM_INVALID", "original SSE event is invalid") from error
            if not isinstance(packet, dict) or packet.get("error"):
                raise Problem(502, "ASSISTANT_MODEL_STREAM_INVALID", "original SSE stream reported an error")
            if self.wire_api == "responses":
                if packet.get("type") == "response.failed":
                    raise Problem(502, "ASSISTANT_MODEL_STREAM_INVALID", "original Responses stream failed")
                if packet.get("type") == "response.output_text.delta" and isinstance(packet.get("delta"), str):
                    self.append(packet["delta"])
                continue
            for choice in packet.get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                text = (choice.get("delta") or {}).get("content")
                if isinstance(text, str) and text:
                    self.append(text)
