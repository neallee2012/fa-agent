"""The once-journaled Team owner rows that share H1's adapter and Team scope: questions (B1 PA-11) and Connect cards (C3).

A subclass names its row once (``NOUN``, its paths and wire models); the wire spells every field after that noun.
"""

from pydantic import TypeAdapter

from shared_context.behavior_wire import Digest
from shared_context.native_hitl_wire import NativeHitlControlReceipt, NativeHitlPendingQuery, hitl_key
from .adapter import ConsoleError, canonical, require, sha

# A finished or refused turn has no live SDK wait left to answer.
_DONE = {"completed", "busy_refused"}


class NativeHitlRowControl:
    NOUN = PHASE = PATH = PAGE_PATH = PAGE = READBACK_PATH = READBACK = COMMAND = REFERENCE = REQUEST_TAG = None

    def __init__(self, native):
        self.native, self.hitl = native, native.hitl
        self.console, self.journal = native.console, native.journal
        self.pages, self.errors, self.observations = {}, {}, {}

    def _row(self, value, field):
        return getattr(value, f"{self.NOUN}_{field}")

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
                    page = self.hitl._read(self.PAGE_PATH, self.PAGE, token,
                                           NativeHitlPendingQuery(turn=turn, after=after))
                    require(page.turn == turn and all(self.hitl._turn(ref.call) == turn for ref in page.references)
                            and all(getattr(item, self.NOUN) in page.references for item in page.attention),
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
        return [{"thread_id": page["thread_id"],
                 "label": self.hitl.attention_label(page["thread_id"], getattr(item, self.NOUN).call),
                 "item": item.model_dump(mode="json")} for page in self.pages.values() for item in page["attention"]]

    def view(self, thread):
        try:
            commands = self.history() if self.console.vault.state() == "AVAILABLE" else []
            error = self.errors.get(thread)
        except ConsoleError as problem:
            commands, error = [], problem.code
        return {"commands": commands, "error": None if error is None else {"code": error}}

    def _request_id(self, reference):
        # One command per original row and owner: a reload or a second click returns the saved original.
        config = self.console.config
        return sha(canonical([config.tenant_id, self.hitl.team_id, config.human_oid, self.REQUEST_TAG,
                              reference.model_dump(mode="json")]))

    def _readback(self, thread, identifier, token):
        try:
            TypeAdapter(Digest).validate_python(identifier, strict=True)
        except ValueError:
            raise ConsoleError("NATIVE_HITL_INVALID_REQUEST", 400) from None
        value = self.hitl._read(self.READBACK_PATH + identifier, self.READBACK, token)
        record, reference = getattr(value, self.NOUN), value.reference
        call = getattr(record.command, self.NOUN).call
        require(self._row(record, "id") == self._row(reference, "id") == identifier
                and record.sha256 == self._row(reference, "digest") and reference.call == call,
                "NATIVE_HITL_RECORD_INVALID", 502)
        self.hitl.call_scope(thread, call, token, read_original=True)
        require(record.tenant_id == self.console.config.tenant_id and record.team_id == self.hitl.team_id,
                "NATIVE_HITL_SCOPE_DENIED", 403)
        return value

    def original(self, identifier):
        entry = self.journal.get(identifier, self.PHASE)
        require(entry is not None, "NATIVE_HITL_ORIGINAL_REQUIRED")
        try:
            intent = entry["intent"]
            raw = canonical(intent["body"])
            command = self.COMMAND.model_validate_json(raw)
            reference = self.REFERENCE.model_validate_json(canonical(intent["selection"]))
            require(intent["method"] == "POST" and intent["path"] == self.PATH
                    and intent["config_sha256"] == self.console.config.sha256 and intent["team_id"] == self.hitl.team_id
                    and intent["body_sha256"] == sha(raw) and raw == canonical(command.model_dump(mode="json"))
                    and identifier == command.request_id == self._request_id(reference)
                    and (self._row(command, "id"), self._row(command, "digest"))
                    == (self._row(reference, "id"), self._row(reference, "digest")),
                    "NATIVE_HITL_ORIGINAL_INVALID", 502)
        except (KeyError, ValueError, TypeError):
            raise ConsoleError("NATIVE_HITL_ORIGINAL_INVALID", 502) from None
        return entry, command, reference

    def history(self):
        values = []
        for identifier, _ in self.journal.entries(self.PHASE):
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
        receipt = self.hitl._read(self.PATH + ("/reconcile" if reconcile else ""), NativeHitlControlReceipt, token,
                                  command)
        config = self.console.config
        key = hitl_key("intent", config.tenant_id, team, config.human_oid, command.request_id)
        result = receipt.result
        require(receipt.control_request_id == hitl_key("apply", config.tenant_id, team, key)
                and receipt.control_request_digest == sha(canonical([
                    config.tenant_id, team, config.human_oid, command.model_dump(mode="json")]))
                and result.get("request_id") == command.request_id and result.get("kind") == command.kind
                and result.get("purpose") == "readback-only-not-execution-permission"
                and result.get(f"{self.NOUN}_id") == self._row(command, "id")
                and result.get("status") == self._status(command),
                "NATIVE_HITL_RECORD_INVALID", 502)
        return receipt

    def reconcile(self, thread, identifier):
        with self.console.message_lock(self.PHASE):
            token = self.hitl.token(thread)
            entry, command, reference = self.original(identifier)
            self.hitl.call_scope(thread, reference.call, token)
            self.observations.pop(identifier, None)
            receipt = self._receipt(command, entry["intent"]["team_id"], token, reconcile=True)
            self.observations[identifier] = receipt.model_dump(mode="json")
            self.journal.finish(identifier, self.PHASE, {
                "state": "CONFIRMED", "receipt": receipt.model_dump(mode="json"),
                "observed_at": self.native.clock().isoformat(), "automatic_retry": False}, keep_confirmed=True)
            return self._result(identifier)
