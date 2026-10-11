"""Hosted receipt client and T2 consumer; the injected execution function owns the existing SDK turn."""

import json
import re

import httpx

from .behavior_agent_control import CONNECTION_HEADER, MAX_FRAME_BYTES, _decode
from .behavior_turn_wire import BeginTurn, StartedTurn, TurnEnd, TurnEndReceipt, TurnHandle, TurnObservation
from .errors import Problem

PREFIX = "/behavior/v1/turn-receipts"


class TurnReceiptClient:
    def __init__(self, api_url, *, token_provider, transport=None):
        url = httpx.URL(api_url)
        if (url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment
                or url.path not in ("", "/") or url.port not in (None, 443) or not callable(token_provider)):
            raise ValueError("the receipt client requires its fixed HTTPS API origin and original identity")
        self.token_provider = token_provider
        self.http = httpx.Client(base_url=api_url.rstrip("/"), transport=transport,
                                 timeout=30, follow_redirects=False, trust_env=False)

    def _call(self, action, context, command, model):
        headers = {
            "Authorization": "Bearer " + self.token_provider(),
            CONNECTION_HEADER: context.control_connection_id,
            "Content-Type": "application/json", "Accept-Encoding": "identity",
        }
        try:
            with self.http.stream("POST", PREFIX + "/" + action, headers=headers,
                                  content=command.model_dump_json().encode()) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_FRAME_BYTES:
                        raise Problem(502, "TURN_RECEIPT_INVALID", "the original response exceeds its bound")
                if response.status_code != 200:
                    code = "TURN_REQUEST_UNKNOWN"
                    try:
                        candidate = json.loads(bytes(raw)).get("error", {}).get("code")
                        if isinstance(candidate, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", candidate):
                            code = candidate
                    except (ValueError, AttributeError):
                        pass
                    raise Problem(response.status_code, code, "the original request did not grant execution; read its receipt")
                if (response.headers.get("cache-control") != "no-store"
                        or response.headers.get("content-encoding", "identity") != "identity"
                        or response.headers.get("content-type", "").split(";")[0] != "application/json"):
                    raise Problem(502, "TURN_RECEIPT_INVALID", "the original receipt response is not private JSON")
                return _decode(bytes(raw), model)
        except httpx.HTTPError:
            raise Problem(503, "TURN_REQUEST_UNKNOWN", "receipt transport failed; never resend the turn") from None

    def begin(self, context, request):
        handle = _decode(request.payload.get_secret_value(), TurnHandle)
        started = self._call("begin", context, BeginTurn(address=request.address, ticket=handle), StartedTurn)
        if (started.start.ticket != handle or started.start.address != request.address
                or started.start.worker != context):
            raise Problem(502, "TURN_RECEIPT_INVALID", "the start receipt belongs to another original turn or process")
        return started

    def read(self, context, address):
        result = self._call("read", context, address, TurnObservation)
        if result.address != address:
            raise Problem(502, "TURN_RECEIPT_INVALID", "the observation belongs to another original turn")
        return result

    def cancel(self, context, address):
        result = self._call("cancel", context, address, TurnObservation)
        if result.address != address:
            raise Problem(502, "TURN_RECEIPT_INVALID", "the cancellation belongs to another original turn")
        return result

    def complete(self, context, end):
        result = self._call("complete", context, end, TurnEndReceipt)
        if result.address != end.address or result.ticket != end.ticket:
            raise Problem(502, "TURN_RECEIPT_INVALID", "the saved end belongs to another original turn")
        return result

    def close(self):
        self.http.close()


class TurnReceiptConsumer:
    def __init__(self, client: TurnReceiptClient, *, execute, abort):
        if not callable(execute) or not callable(abort):
            raise ValueError("the existing SDK execution and CID/RID-specific abort functions are required")
        self.client, self.execute_turn, self.abort_turn = client, execute, abort

    def consume(self, context, request):
        return self.client.begin(context, request).model_dump_json().encode()

    def execute(self, context, request, start_receipt):
        started = _decode(start_receipt, StartedTurn)
        handle = _decode(request.payload.get_secret_value(), TurnHandle)
        if (started.start.worker != context or started.start.address != request.address
                or started.start.ticket != handle):
            raise Problem(403, "TURN_RECEIPT_INVALID", "only this freshly started original turn may execute")
        end = self.execute_turn(context, started)
        if not isinstance(end, TurnEnd):
            raise Problem(503, "TURN_END_UNKNOWN", "the original SDK turn did not produce a typed saved end")
        if end.ticket != handle or end.address != request.address or end.start_sha256 != started.start.sha256:
            raise Problem(409, "TURN_END_CONFLICT", "the SDK result belongs to another original started turn")
        return self.client.complete(context, end).model_dump_json().encode()

    def read(self, context, address):
        return self.client.read(context, address).model_dump_json().encode()

    def cancel(self, context, address):
        self.abort_turn(address)
        return self.client.cancel(context, address).model_dump_json().encode()
