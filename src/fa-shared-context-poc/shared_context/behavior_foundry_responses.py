"""One-request Responses transport over the original API credential and HTTP pool."""

import json
import logging
from time import monotonic
from typing import Literal, NamedTuple
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter

from .behavior_api import _nonfinite, _unique
from .behavior_foundry import FOUNDRY_TOKEN_SCOPE, MAX_NATIVE_TURN_HTTP_BYTES, FoundryHttpClient
from .behavior_http_diagnostics import _exception_type, native_response_diagnostics
from .behavior_wire import Identifier, WireModel
from .errors import Problem
from .native_responses_wire import (
    NATIVE_RESPONSE_CID_HEADER, NATIVE_RESPONSE_DIGEST_HEADER, NATIVE_RESPONSE_RID_HEADER,
    NativeResponseHeaders, NativeResponseId,
)

ConversationMode = Literal["conversation", "previous_response_id"]
HistoryKind = Literal["conversation", "response"]
ResponseStatus = Literal["queued", "in_progress", "completed", "failed", "cancelled", "incomplete"]


class StoredResponse(WireModel):
    id: NativeResponseId
    status: ResponseStatus


class StreamSource(NamedTuple):
    """One read-only replay of an original stored background stream, addressed with the API's own identity."""
    url: str
    params: dict
    headers: dict


def sse_values(events, chunk):
    """Each complete SSE data event in ``chunk`` as strict JSON, or None when it is not JSON."""
    for data in events.feed(chunk):
        try:
            yield json.loads(data, object_pairs_hook=_unique, parse_constant=_nonfinite)
        except (ValueError, TypeError):
            yield None


def _identifier(value, model=Identifier):
    try:
        return TypeAdapter(model).validate_python(value)
    except ValueError:
        raise Problem(422, "NATIVE_REQUEST_INVALID", "one bounded original platform identifier is required") from None


class FoundryResponsesClient:
    def __init__(self, native: FoundryHttpClient, *, team_id: str, conversation_mode: ConversationMode):
        try:
            self.mode = TypeAdapter(ConversationMode).validate_python(conversation_mode)
        except ValueError:
            raise Problem(503, "NATIVE_TURNS_DISABLED", "the server has not selected a Responses conversation mode") from None
        self.native = native
        self.metadata = {"fa_agent_id": native.target.agent_id, "fa_team_id": _identifier(team_id)}
        self.root = (native.target.project_endpoint + "/agents/" + quote(native.target.agent_name, safe="")
                     + "/endpoint/protocols/openai")

    def _scope(self, value, *, kind, identifier=None):
        try:
            observed = TypeAdapter(Identifier if kind == "conversation" else NativeResponseId).validate_python(value["id"])
            if value.get("object") != kind or identifier is not None and observed != identifier:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise Problem(502, "NATIVE_STATE_INVALID", "the platform record differs from its original identifier") from None
        metadata = value.get("metadata")
        if not isinstance(metadata, dict) or any(metadata.get(key) != expected for key, expected in self.metadata.items()):
            raise Problem(403, "NATIVE_STATE_SCOPE", "the platform history belongs to another Agent or Team")
        return observed

    def _token(self):
        credential = self.native.credential
        if credential is None:
            raise Problem(403, "FOUNDRY_CREDENTIAL_DISABLED", "the original API managed identity is required")
        try:
            token = credential.get_token(FOUNDRY_TOKEN_SCOPE).token
        except Exception:
            raise Problem(503, "FOUNDRY_CREDENTIAL_UNAVAILABLE", "the original credential is unavailable") from None
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise Problem(503, "FOUNDRY_CREDENTIAL_UNAVAILABLE", "the original credential is invalid")
        return token

    @staticmethod
    def _first_event(response, started):
        """The acknowledgment of a background stream is its first event: ``response.created``. Anything else is
        unconfirmed; the stream is closed right after it, which never cancels a background response."""
        from .behavior_instruction_stream import SseDataEvents

        events, size = SseDataEvents(), 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if monotonic() - started > 120 or size > MAX_NATIVE_TURN_HTTP_BYTES:
                raise Problem(503, "NATIVE_RESPONSE_UNCONFIRMED", "the Responses stream did not acknowledge in bounds")
            for value in sse_values(events, chunk):
                if (isinstance(value, dict) and value.get("type") == "response.created"
                        and isinstance(value.get("response"), dict)):
                    return value["response"]
                raise Problem(503, "NATIVE_RESPONSE_UNCONFIRMED", "the Responses stream did not start with its ACK")
        raise Problem(503, "NATIVE_RESPONSE_UNCONFIRMED", "the Responses stream ended before its acknowledgment")

    def _request(self, method, suffix, *, body=None, params=None, headers=None, before_send=None, sse_ack=False):
        unknown_code = ("NATIVE_RESPONSE_UNCONFIRMED" if method == "POST" and suffix == "/responses"
                        else "NATIVE_TURN_UNKNOWN")
        token = self._token()
        encoded = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if encoded is not None and len(encoded) > MAX_NATIVE_TURN_HTTP_BYTES:
            raise Problem(413, "NATIVE_REQUEST_TOO_LARGE", "the complete Responses request exceeds its transport bound")
        if before_send is not None:
            before_send()
        response, phase, started = None, "request", monotonic()
        try:
            with self.native.http.stream(
                method, self.root + suffix, params={"api-version": "v1", **(params or {})}, content=encoded,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                         "Accept": "application/json", "Accept-Encoding": "identity", **(headers or {})},
            ) as response:
                phase = "response_headers"
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise Problem(502, "NATIVE_STATE_INVALID", "compressed control responses are not accepted")
                if (sse_ack and response.status_code == 200 and response.headers.get(
                        "content-type", "").split(";", 1)[0].strip().lower() == "text/event-stream"):
                    phase = "response_event"
                    return self._first_event(response, started)
                raw = bytearray()
                phase = "response_body"
                for chunk in response.iter_bytes():
                    if monotonic() - started > 120 or len(raw) + len(chunk) > MAX_NATIVE_TURN_HTTP_BYTES:
                        raise Problem(502, "NATIVE_STATE_INVALID", "the complete platform response exceeds its transport bound")
                    raw.extend(chunk)
                phase = "response_status"
                try:
                    value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite)
                except (ValueError, TypeError):
                    value = None
                if response.status_code in {401, 403}:
                    raise Problem(response.status_code, "NATIVE_TURNS_DISABLED",
                                  "the original API identity cannot access this Responses operation")
                if response.status_code not in {200, 201, 202}:
                    error = value.get("error") if isinstance(value, dict) else None
                    if (response.status_code == 409 and isinstance(error, dict)
                            and error.get("code") == "conversation_locked"):
                        raise Problem(409, "NATIVE_TURN_BUSY", "retain this RID and SID while the conversation is locked",
                                      upstream_status=409, upstream_error_code="conversation_locked")
                    # A bare 400/404 or a missing ACK does not prove that the handler never received this RID.
                    raise Problem(503, unknown_code, "the original Responses operation is unconfirmed",
                                  upstream_status=response.status_code)
                phase = "response_decode"
                if (not isinstance(value, dict)
                        or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"):
                    raise Problem(502, "NATIVE_STATE_INVALID", "a complete platform JSON object is required")
                return value
        except (Problem, httpx.HTTPError) as error:
            details = {"phase": phase, "method": method, "status": None if response is None else response.status_code,
                       "exception_type": None if isinstance(error, Problem) else _exception_type(error),
                       "elapsed_ms": round((monotonic() - started) * 1000)}
            if response is not None:
                details.update(native_response_diagnostics(response.headers))
            logging.getLogger(__name__).warning("NATIVE_RESPONSES_UNCONFIRMED %s", json.dumps(details, sort_keys=True))
            if isinstance(error, Problem):
                raise
            raise Problem(503, unknown_code, "the original Responses operation is unconfirmed") from None

    def _thread(self, conversation_id, previous_response_id):
        if self.mode == "conversation":
            if previous_response_id is not None:
                raise Problem(422, "NATIVE_REQUEST_INVALID", "conversation and response-chain modes cannot be mixed")
            return {"conversation": _identifier(conversation_id)}
        return {"metadata": self.metadata, **(
            {} if previous_response_id is None
            else {"previous_response_id": _identifier(previous_response_id, NativeResponseId)})}

    @staticmethod
    def _stored_response(value, *, expected_id=None, expected_session=None):
        try:
            stored = StoredResponse.model_validate({"id": value["id"], "status": value["status"]})
            if (value.get("object") != "response" or expected_id is not None and stored.id != expected_id
                    or expected_session is not None and value.get("agent_session_id", expected_session) != expected_session):
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise Problem(502, "NATIVE_STATE_INVALID", "the response acknowledgment is not from the original scope") from None
        return stored

    def start(self, original: NativeResponseHeaders, input_text: str, *, previous_response_id=None, before_send=None,
              stream=False):
        """``stream`` (owner sends only) makes the response replayable for the preview relay. A same-CID lock is
        then raised inside the 200 stream and reads as unconfirmed, so callers that rely on 409 keep ``False``."""
        from .behavior_native_turn_wire import NATIVE_EXPECTED_VERSION_HEADER
        original = NativeResponseHeaders.model_validate_json(original.model_dump_json())
        if not isinstance(input_text, str) or type(stream) is not bool:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "the original text input is required")
        value = self._request("POST", "/responses", body={
            "input": input_text, "background": True, "store": True, "stream": stream,
            "agent_session_id": self.native.target.session_id,
            **self._thread(original.conversation_id, previous_response_id),
        }, headers={
            NATIVE_RESPONSE_RID_HEADER: original.request_id, NATIVE_RESPONSE_CID_HEADER: original.conversation_id,
            NATIVE_RESPONSE_DIGEST_HEADER: original.submit_digest,
            NATIVE_EXPECTED_VERSION_HEADER: self.native.target.agent_version,
            **({"Accept": "text/event-stream"} if stream else {}),
        }, before_send=before_send, sse_ack=stream)
        return self._stored_response(value, expected_session=self.native.target.session_id)

    def stream_source(self, response_id, starting_after=None):
        """URL, query and headers for one read-only replay of an original stored background stream."""
        identifier = _identifier(response_id, NativeResponseId)
        if starting_after is not None and (type(starting_after) is not int or starting_after < 0):
            raise Problem(422, "NATIVE_REQUEST_INVALID", "starting_after is one non-negative sequence number")
        params = {"api-version": "v1", "stream": "true",
                  **({} if starting_after is None else {"starting_after": str(starting_after)})}
        return StreamSource(self.root + "/responses/" + quote(identifier, safe=""), params, {
            "Authorization": "Bearer " + self._token(), "Accept": "text/event-stream", "Accept-Encoding": "identity"})

    def create_conversation(self, *, before_send):
        if self.mode != "conversation":
            raise Problem(409, "NATIVE_STATE_SCOPE", "response-chain mode does not create a conversation object")
        value = self._request("POST", "/conversations", body={"metadata": self.metadata}, before_send=before_send)
        return self._scope(value, kind="conversation")

    def _items(self, path, *, after, limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "platform history pages contain 1 to 100 items")
        params = {"limit": limit, "order": "asc"}
        if after is not None:
            params["after"] = _identifier(after, NativeResponseId)
        page = self._request("GET", path, params=params)
        try:
            data = page["data"]
            if page.get("object") != "list" or not isinstance(data, list) or len(data) > limit:
                raise ValueError()
            identifiers = [TypeAdapter(NativeResponseId).validate_python(item["id"]) for item in data]
            if (len(identifiers) != len(set(identifiers)) or type(page.get("has_more")) is not bool
                    or page.get("has_more") and not identifiers
                    or identifiers and (page.get("first_id") != identifiers[0] or page.get("last_id") != identifiers[-1])
                    or page.get("has_more") and page["last_id"] == after):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise Problem(502, "NATIVE_STATE_INVALID", "the platform history page or cursor is incomplete") from None
        return page

    def conversation_items(self, conversation_id, *, after=None, limit=20):
        self.history_record("conversation", conversation_id)
        return self.history_items("conversation", conversation_id, after=after, limit=limit)

    def response_items(self, response_id, *, after=None, limit=20):
        value = self.history_record("response", response_id)
        return value, self.history_items("response", response_id, after=after, limit=limit)

    @staticmethod
    def _history_path(kind: HistoryKind, identifier):
        if kind not in {"conversation", "response"}:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original history source is required")
        checked = _identifier(identifier, Identifier if kind == "conversation" else NativeResponseId)
        return "/" + ("conversations" if kind == "conversation" else "responses") + "/" + quote(checked, safe="")

    def history_record(self, kind: HistoryKind, identifier):
        value = self._request("GET", self._history_path(kind, identifier))
        self._scope(value, kind=kind, identifier=identifier)
        if kind == "response":
            self._stored_response(value, expected_id=identifier)
        return value

    def history_items(self, kind: HistoryKind, identifier, *, after=None, limit=20):
        path = self._history_path(kind, identifier) + ("/items" if kind == "conversation" else "/input_items")
        return self._items(path, after=after, limit=limit)

    def cancel(self, response_id, *, before_send):
        identifier = _identifier(response_id, NativeResponseId)
        value = self._request("POST", "/responses/" + quote(identifier, safe="") + "/cancel",
                              before_send=before_send)
        return self._stored_response(value, expected_id=identifier)
