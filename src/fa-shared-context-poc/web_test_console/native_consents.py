"""C3: the Team owner's Connect cards, on H1 consent rows and the console's once journal.

A card names a toolbox source this Agent has no working connection for. Its consent links come only from the card's
private readback (``card``), never from the attention lists or this console's state view. One click per card and owner,
「我連好了，繼續」 or 「先不用」; a second click returns the saved original and is never sent again.
"""

from shared_context.connector_registry import CONNECTORS
from shared_context.native_hitl_wire import (
    ConnectorConsentDecision, ConnectorConsentPage, ConnectorConsentReadback, ConnectorConsentReference,
)
from .adapter import ConsoleError, canonical, require, sha, strict_json
from .native_hitl_rows import NativeHitlRowControl

PHASE = "native-consent"
PATH = "/consent-decisions"
MAX_WRITE_BYTES = 8192
MESSAGES = {}


def connector_view(connector_id):
    """What the card says about a registered connector; None for another toolbox source."""
    ref = CONNECTORS.get(connector_id)
    return None if ref is None else {
        "display_name": ref.display_name, "disclosure": ref.disclosure, "revoke_url": ref.revoke_url}


class NativeConsentControl(NativeHitlRowControl):
    NOUN, PHASE, PATH, REQUEST_TAG = "consent", PHASE, PATH, "native-consent-decision"
    PAGE_PATH, PAGE = "/turn-consents", ConnectorConsentPage
    READBACK_PATH, READBACK = "/consents/", ConnectorConsentReadback
    COMMAND, REFERENCE = ConnectorConsentDecision, ConnectorConsentReference

    @staticmethod
    def _status(command):
        return command.choice

    def card(self, thread, identifier):
        """The private readback for one card: the only console response that carries its consent links."""
        token = self.hitl.token(thread)
        value = self._readback(thread, identifier, token)
        return {"readback": value.model_dump(mode="json"),
                "connector": connector_view(value.consent.command.consent.connector_id),
                "read_at": self.native.clock().isoformat(), "commands": self.history()}

    def decide(self, thread, data):
        with self.console.message_lock(PHASE):
            token = self.hitl.token(thread)
            require(type(data) is dict and set(data) == {"consent", "choice"}
                    and data["choice"] in {"resume", "decline"}, "NATIVE_HITL_INVALID_REQUEST", 400)
            try:
                reference = ConnectorConsentReference.model_validate_json(canonical(data["consent"]))
            except (ValueError, TypeError):
                raise ConsoleError("NATIVE_HITL_INVALID_REQUEST", 400) from None
            self.hitl.call_scope(thread, reference.call, token)
            identifier = self._request_id(reference)
            if self.journal.get(identifier, PHASE) is not None:
                # One click per card: a reload or a second click only shows the saved original.
                self.original(identifier)
                return self._result(identifier)
            command = ConnectorConsentDecision(request_id=identifier, consent_id=reference.consent_id,
                                               consent_digest=reference.consent_digest, choice=data["choice"])
            value = self._readback(thread, reference.consent_id, token)
            require(value.reference == reference, "NATIVE_HITL_ACTION_CHANGED")
            require(not value.expired and self.native.clock() < value.consent.expires_at, "NATIVE_HITL_EXPIRED")
            require(not value.turn_closed, "NATIVE_HITL_TURN_CLOSED")
            require(value.status == "pending" and value.decision is None, "NATIVE_HITL_STATE_CONFLICT")
            body = command.model_dump(mode="json")
            consent = value.consent.command.consent
            self.native._current(token)
            self.hitl.token(thread)
            self.journal.begin(identifier, PHASE, {
                "method": "POST", "path": PATH, "body": body, "body_sha256": sha(canonical(body)),
                "selection": reference.model_dump(mode="json"), "team_id": self.hitl.team_id, "thread_id": thread,
                "config_sha256": self.console.config.sha256, "created_at": self.native.clock().isoformat(),
                # Only to name the source again beside the click after a reload; never the links, never sent.
                "display": {"connector_id": consent.connector_id, "need": consent.need,
                            "name": (connector_view(consent.connector_id) or {}).get("display_name", consent.server_label)},
            })
            try:
                self.native._current(token)
                self.hitl.token(thread)
                receipt = self._receipt(command, self.hitl.team_id, token)
            except ConsoleError as error:
                self.journal.finish(identifier, PHASE, {
                    "state": "UNKNOWN", "code": error.code, "observed_at": self.native.clock().isoformat(),
                    "automatic_retry": False})
                raise
            self.journal.finish(identifier, PHASE, {
                "state": "CONFIRMED", "receipt": receipt.model_dump(mode="json"),
                "observed_at": self.native.clock().isoformat(), "automatic_retry": False})
            return self._result(identifier)


def install_consent_routes(app, requested_console):
    from fastapi import Depends, Request
    from starlette.concurrency import run_in_threadpool

    def control(console):
        require(console.native_instructions is not None and console.native_instructions.hitl.enabled,
                "NATIVE_HITL_UNAVAILABLE")
        return console.native_instructions.consents

    async def body(request, *, read=False):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            require(len(raw) <= (0 if read else MAX_WRITE_BYTES), "NATIVE_HITL_INVALID_REQUEST", 413)
        if read:
            return None
        try:
            value = strict_json(bytes(raw))
        except (ValueError, TypeError):
            raise ConsoleError("NATIVE_HITL_INVALID_REQUEST", 400) from None
        require(type(value) is dict, "NATIVE_HITL_INVALID_REQUEST", 400)
        return value

    def reconcile_body(value):
        require(set(value) == {"request_id"} and isinstance(value["request_id"], str), "NATIVE_HITL_INVALID_REQUEST", 400)
        return value["request_id"]

    @app.get("/api/native-hitl/team/consents/{consent_id}")
    async def team_card(consent_id: str, request: Request, console=Depends(requested_console)):
        await body(request, read=True)
        return await run_in_threadpool(control(console).card, None, consent_id)

    @app.post("/api/native-hitl/team/consents")
    async def team_decide(request: Request, console=Depends(requested_console)):
        return await run_in_threadpool(control(console).decide, None, await body(request))

    @app.post("/api/native-hitl/team/consent-reconcile")
    async def team_reconcile(request: Request, console=Depends(requested_console)):
        identifier = reconcile_body(await body(request))
        return await run_in_threadpool(control(console).reconcile, None, identifier)

    @app.get("/api/native-hitl/{thread_id}/consents/{consent_id}")
    async def card(thread_id: str, consent_id: str, request: Request, console=Depends(requested_console)):
        await body(request, read=True)
        return await run_in_threadpool(control(console).card, thread_id, consent_id)

    @app.post("/api/native-hitl/{thread_id}/consents")
    async def decide(thread_id: str, request: Request, console=Depends(requested_console)):
        return await run_in_threadpool(control(console).decide, thread_id, await body(request))

    @app.post("/api/native-hitl/{thread_id}/consent-reconcile")
    async def reconcile(thread_id: str, request: Request, console=Depends(requested_console)):
        identifier = reconcile_body(await body(request))
        return await run_in_threadpool(control(console).reconcile, thread_id, identifier)
