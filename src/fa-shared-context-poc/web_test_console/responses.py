"""Thin Responses transport; admission, execution and history authority remain in the API."""

import re
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import AwareDatetime, Field, TypeAdapter

from shared_context.behavior_foundry import MAX_NATIVE_TURN_HTTP_BYTES
from shared_context.behavior_native_responses_api import PREFIX, PlatformHistoryPage
from shared_context.behavior_wire import Identifier, WireModel
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE, NativeResponseId
from .adapter import STATUS_ERRORS, ConsoleError, error_code, require
from .native import NativeAdapter, _decode

RECONCILE_PHASE = "responses-reconcile"
MAX_RECONCILE_FAILURES = 10
# RTT B live text: the API ends each stream within 230 s; the page resumes with its last sequence number.
LIVE_SECONDS = 240
# The API's longest forwarded line is about 1 MiB (one UTF-8 event); a longer unfinished line ends the stream.
MAX_LIVE_LINE = 2 * 1024 * 1024
# The API answers NATIVE_RID_MISSING (404) or NATIVE_STREAM_NOT_STARTED (409) until the turn starts (dev #708, 10-07).
LIVE_WAIT_ERRORS = frozenset({"NATIVE_RID_MISSING", "NATIVE_STREAM_NOT_STARTED"})
LIVE_ERRORS = LIVE_WAIT_ERRORS | {"NATIVE_STREAM_UNAVAILABLE"}
_LIVE_PATH = re.compile(re.escape(PREFIX) + r"/conversations/[A-Za-z0-9_-]{1,128}/requests/[A-Za-z0-9_-]{1,128}/stream")


class ResponseReconciliation(WireModel):
    state: Literal["waiting", "in_flight", "stopped", "owned"]
    failures: Annotated[int, Field(ge=0, le=MAX_RECONCILE_FAILURES)] = 0
    wait_step: Annotated[int, Field(ge=0, le=6)] = 0
    due_at: AwareDatetime | None = None
    code: str | None = None


class ResponsesAdapter(NativeAdapter):
    submit_path = PREFIX + "/submit"
    cancel_path = PREFIX + "/cancel"

    def _submit_to(self, path, command, token):
        value = super()._submit_to(path, command, token)
        require(value.state_profile == NATIVE_RESPONSES_PROFILE, "NATIVE_STATE_SCOPE", 502)
        return value

    def reconcile(self, command, token):
        return self._submit_to(PREFIX + "/reconcile", command, token)

    def change(self, command, token):
        value = super().change(command, token)
        require(value.state_profile == NATIVE_RESPONSES_PROFILE, "NATIVE_STATE_SCOPE", 502)
        return value

    def history_page(self, kind, identifier, token, *, after=None, limit=20):
        require(kind in {"conversation", "response"}, "NATIVE_REQUEST_INVALID", 400)
        try:
            TypeAdapter(Identifier if kind == "conversation" else NativeResponseId).validate_python(identifier)
        except ValueError:
            raise ConsoleError("NATIVE_REQUEST_INVALID", 400) from None
        params = {"limit": limit, **({} if after is None else {"after": after})}
        path = PREFIX + "/" + ("conversations" if kind == "conversation" else "responses") + "/" + identifier + "/history"
        raw, _ = self.send("GET", path, None, token, params=params, limit=MAX_NATIVE_TURN_HTTP_BYTES)
        page = _decode(PlatformHistoryPage, raw)
        require((page.source_kind, page.source_id) == (kind, identifier), "NATIVE_STATE_SCOPE", 502)
        require(page.next_after is None or page.next_after != after, "NATIVE_STATE_INVALID", 502)
        require(len({message.item_id for message in page.messages}) == len(page.messages), "NATIVE_STATE_INVALID", 502)
        return page

    def live_text(self, cid, rid, token, *, starting_after=None):
        """RTT B: open one read-only live-text stream of an owner turn; its bytes are relayed as they come.

        Display only: the API forwards whitelisted official Responses events and the page replaces them with the
        saved reply from ``/status``. The stream holds one of the backend pool's connections (100, #710) while it
        runs; the API's 15-second keep-alive stays well inside the 90-second read timeout. Nothing is sent, retried
        or stored here."""
        path = PREFIX + "/conversations/" + cid + "/requests/" + rid + "/stream"
        url = self.config.backend_origin + self.api_prefix + path
        require(_LIVE_PATH.fullmatch(path) is not None
                and urlsplit(url).netloc == urlsplit(self.config.backend_origin).netloc
                and (starting_after is None or type(starting_after) is int and starting_after >= 0),
                "ORIGIN_DENIED", 403)
        try:
            response = self.client.send(self.client.build_request(
                "GET", url, params=None if starting_after is None else {"starting_after": starting_after},
                headers={"Authorization": "Bearer " + token, "Accept": "text/event-stream",
                         "Accept-Encoding": "identity"}), stream=True, follow_redirects=False)
        except (httpx.HTTPError, OSError):
            raise ConsoleError("BACKEND_OFFLINE", 503) from None
        media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if (response.status_code != 200 or media != "text/event-stream"
                or response.headers.get("content-encoding", "identity").lower() != "identity"):
            status, data = response.status_code, bytearray()
            try:
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 8192:
                        break
            except (httpx.HTTPError, OSError):
                pass
            finally:
                response.close()
            code = error_code(data)
            if code in LIVE_ERRORS:
                refused = ConsoleError(code, 409, status)
                refused.automatic_retry = code in LIVE_WAIT_ERRORS
                raise refused
            raise ConsoleError(STATUS_ERRORS.get(status, "BACKEND_UNAVAILABLE"), 409, status)
        secret = token.encode()

        def chunks():
            # Only whole lines go on, so the login token (which has no newline) is checked whole before any of it
            # could reach the page, however the bytes were split.
            started, pending = time.monotonic(), b""
            try:
                for chunk in response.iter_bytes():
                    pending += chunk
                    end = pending.rfind(b"\n") + 1
                    ready, pending = pending[:end], pending[end:]
                    if (secret in ready or secret in pending or len(pending) > MAX_LIVE_LINE
                            or time.monotonic() - started > LIVE_SECONDS):
                        return
                    if ready:
                        yield ready
            except (httpx.HTTPError, OSError):
                return
            finally:
                response.close()

        return chunks()
