"""C3: the Team owner's Connect cards, on H1 consent rows and the console's once journal.

A card names a toolbox source this Agent has no working connection for. Its consent links come only from the card's
private readback (``card``), never from the attention lists or this console's state view. One click per card and owner,
「我連好了，繼續」 or 「先不用」; a second click returns the saved original and is never sent again.
"""

from pydantic import TypeAdapter

from shared_context.behavior_wire import Digest
from shared_context.connector_registry import CONNECTORS
from shared_context.native_hitl_wire import (
    ConnectorConsentDecision, ConnectorConsentPage, ConnectorConsentReadback, ConnectorConsentReference,
    NativeHitlControlReceipt, NativeHitlPendingQuery, hitl_key,
)
from .adapter import ConsoleError, canonical, require, sha, strict_json

PHASE = "native-consent"
PATH = "/consent-decisions"
MAX_WRITE_BYTES = 8192
_DONE = {"completed", "busy_refused"}
MESSAGES = {}


def connector_view(connector_id):
    """What the card says about a registered connector; None for another toolbox source."""
    ref = CONNECTORS.get(connector_id)
    return None if ref is None else {
        "display_name": ref.display_name, "disclosure": ref.disclosure, "revoke_url": ref.revoke_url}


class NativeConsentControl:
    def __init__(self, native):
        self.native, self.hitl = native, native.hitl
        self.console, self.journal = native.console, native.journal
        self.pages, self.errors, self.observations = {}, {}, {}

    def refresh(self, thread, token, *, only=None):
        if not self.hitl.enabled:
            return
        try:
            for rid, original in tuple(self.native.receipts.items()):
                if self.native.request_threads.get(rid) != thread or only is not None and rid != only:
                    continue
                if original.status in _DONE:
                    self.pages.pop(rid, None)
                    continue
                turn = self.hitl._turn(original)
                attention, after, closed = [], 0, False
                while True:
                    page = self.hitl._read("/turn-consents", ConnectorConsentPage, token,
                                           NativeHitlPendingQuery(turn=turn, after=after))
                    require(page.turn == turn and all(self.hitl._turn(ref.call) == turn for ref in page.references)
                            and all(item.consent in page.references for item in page.attention),
                            "NATIVE_HITL_RECORD_INVALID", 502)
                    attention.extend(page.attention)
                    closed = closed or page.closed
                    if page.next_after is None:
                        break
                    require(page.next_after > after, "NATIVE_HITL_RECORD_INVALID", 502)
                    after = page.next_after
                self.pages[rid] = {"thread_id": thread, "attention": [] if closed else attention}
            self.errors.pop(thread, None)
        except ConsoleError as error:
            self.errors[thread] = error.code
            if error.status in {401, 403} or error.upstream_status in {401, 403}:
                raise

    def attention(self):
        if not self.hitl.enabled or self.console.vault.state() != "AVAILABLE":
            return []
        return [{"thread_id": page["thread_id"], "label": self.hitl.attention_label(page["thread_id"], item.consent.call),
                 "item": item.model_dump(mode="json")} for page in self.pages.values() for item in page["attention"]]

    def view(self, thread):
        try:
            commands = self.history() if self.console.vault.state() == "AVAILABLE" else []
            error = self.errors.get(thread)
        except ConsoleError as problem:
            commands, error = [], problem.code
        return {"commands": commands, "error": None if error is None else {"code": error}}

    def _request_id(self, reference):
        config = self.console.config
        return sha(canonical([config.tenant_id, self.hitl.team_id, config.human_oid, "native-consent-decision",
                              reference.model_dump(mode="json")]))

    def _card(self, thread, identifier, token):
        try:
            TypeAdapter(Digest).validate_python(identifier, strict=True)
        except ValueError:
            raise ConsoleError("NATIVE_HITL_INVALID_REQUEST", 400) from None
        value = self.hitl._read("/consents/" + identifier, ConnectorConsentReadback, token)
        consent, reference = value.consent, value.reference
        call = consent.command.consent.call
        require(consent.consent_id == reference.consent_id == identifier
                and consent.sha256 == reference.consent_digest and reference.call == call,
                "NATIVE_HITL_RECORD_INVALID", 502)
        self.hitl.call_scope(thread, call, token, read_original=True)
        require(consent.tenant_id == self.console.config.tenant_id and consent.team_id == self.hitl.team_id,
                "NATIVE_HITL_SCOPE_DENIED", 403)
        return value

    def card(self, thread, identifier):
        """The private readback for one card: the only console response that carries its consent links."""
        token = self.hitl.token(thread)
        value = self._card(thread, identifier, token)
        return {"readback": value.model_dump(mode="json"),
                "connector": connector_view(value.consent.command.consent.connector_id),
                "read_at": self.native.clock().isoformat(), "commands": self.history()}

    def original(self, identifier):
        entry = self.journal.get(identifier, PHASE)
        require(entry is not None, "NATIVE_HITL_ORIGINAL_REQUIRED")
        try:
            intent = entry["intent"]
            raw = canonical(intent["body"])
            command = ConnectorConsentDecision.model_validate_json(raw)
            reference = ConnectorConsentReference.model_validate_json(canonical(intent["selection"]))
            require(intent["method"] == "POST" and intent["path"] == PATH
                    and intent["config_sha256"] == self.console.config.sha256 and intent["team_id"] == self.hitl.team_id
                    and intent["body_sha256"] == sha(raw) and raw == canonical(command.model_dump(mode="json"))
                    and identifier == command.request_id == self._request_id(reference)
                    and (command.consent_id, command.consent_digest) == (reference.consent_id, reference.consent_digest),
                    "NATIVE_HITL_ORIGINAL_INVALID", 502)
        except (KeyError, ValueError, TypeError):
            raise ConsoleError("NATIVE_HITL_ORIGINAL_INVALID", 502) from None
        return entry, command, reference

    def history(self):
        values = []
        for identifier, _ in self.journal.entries(PHASE):
            entry, command, reference = self.original(identifier)
            values.append({
                "request_id": identifier, "kind": command.kind, "state": entry["state"],
                "thread_id": entry["intent"]["thread_id"], "command": command.model_dump(mode="json"),
                "selection": reference.model_dump(mode="json"), "code": entry.get("code"),
                "observation": self.observations.get(identifier),
                "display": entry["intent"].get("display"), "created_at": entry["intent"].get("created_at"),
            })
        return values

    def _result(self, identifier):
        return {"request_id": identifier, "commands": self.history(),
                "original_request_resent": False, "purpose": "readback-only-not-execution-permission"}

    def _receipt(self, command, team, token, *, reconcile=False):
        receipt = self.hitl._read(PATH + ("/reconcile" if reconcile else ""), NativeHitlControlReceipt, token, command)
        config = self.console.config
        key = hitl_key("intent", config.tenant_id, team, config.human_oid, command.request_id)
        result = receipt.result
        require(receipt.control_request_id == hitl_key("apply", config.tenant_id, team, key)
                and receipt.control_request_digest == sha(canonical([
                    config.tenant_id, team, config.human_oid, command.model_dump(mode="json")]))
                and result.get("request_id") == command.request_id and result.get("kind") == command.kind
                and result.get("purpose") == "readback-only-not-execution-permission"
                and result.get("consent_id") == command.consent_id and result.get("status") == command.choice,
                "NATIVE_HITL_RECORD_INVALID", 502)
        return receipt

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
            value = self._card(thread, reference.consent_id, token)
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

    def reconcile(self, thread, identifier):
        with self.console.message_lock(PHASE):
            token = self.hitl.token(thread)
            entry, command, reference = self.original(identifier)
            self.hitl.call_scope(thread, reference.call, token)
            self.observations.pop(identifier, None)
            receipt = self._receipt(command, entry["intent"]["team_id"], token, reconcile=True)
            self.observations[identifier] = receipt.model_dump(mode="json")
            self.journal.finish(identifier, PHASE, {
                "state": "CONFIRMED", "receipt": receipt.model_dump(mode="json"),
                "observed_at": self.native.clock().isoformat(), "automatic_retry": False}, keep_confirmed=True)
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
