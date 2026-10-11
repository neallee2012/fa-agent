"""Local append transport intents and readbacks, never a second turn authority."""

from shared_context.native_append_wire import NativeAppend, NativeAppendReceipt
from .adapter import ConsoleError, canonical, require, sha
from .instructions import check_content


PHASE = "native-append"
READ_PHASE = "native-append-read"


class AppendControl:
    def __init__(self, native):
        self.native = native
        self.journal = native.journal
        self.pages = {}
        self.errors = {}

    def local(self, rid):
        return [(identifier, entry) for identifier, entry in self.journal.entries(PHASE)
                if entry["intent"].get("parent_request_id") == rid]

    def _saved(self, identifier, entry):
        read = self.journal.get(identifier, READ_PHASE)
        saved = read if read is not None and read.get("receipt") is not None else entry
        if saved.get("receipt") is None:
            return None
        try:
            return NativeAppendReceipt.model_validate_json(canonical(saved["receipt"]))
        except (ValueError, TypeError):
            raise ConsoleError("NATIVE_STATE_INVALID", 502) from None

    def proposal(self, original):
        entries = self.local(original.request_id)
        if entries:
            identifier, entry = entries[0]
            if entry["state"] == "UNKNOWN" and self._saved(identifier, entry) is None:
                return {"request_id": original.request_id, "append_id": identifier, "pending": True}
        previous = entries[0][0] if entries else None
        identifier = "append-" + sha(canonical([
            self.native.console.config.sha256, original.agent_id, original.request_id,
            original.submit_digest, previous,
        ]))[:40]
        return {"request_id": original.request_id, "append_id": identifier, "pending": False}

    @staticmethod
    def _scope(value, original):
        require((value.agent_id, value.hosted_session_id, value.request_id, value.conversation_id, value.submit_digest)
                == (original.agent_id, original.hosted_session_id, original.request_id,
                    original.conversation_id, original.submit_digest), "NATIVE_STATE_SCOPE", 502)

    def _remember(self, original, value):
        self._scope(value, original)
        local = self.journal.get(value.append_id, PHASE)
        if local is not None:
            command = NativeAppend.model_validate_json(canonical(local["intent"]["body"]))
            require((command.request_id, command.conversation_id, command.expected_submit_digest, command.content)
                    == (value.request_id, value.conversation_id, value.submit_digest, value.content),
                    "NATIVE_RID_CONFLICT", 502)
        if self.journal.get(value.append_id, READ_PHASE) is None:
            self.journal.begin(value.append_id, READ_PHASE, {
                "parent_request_id": original.request_id, "method": "GET",
                "created_at": self.native.clock().isoformat(),
            })
        self.journal.finish(value.append_id, READ_PHASE, {
            "state": "VERIFIED", "receipt": value.model_dump(mode="json"),
            "observed_at": self.native.clock().isoformat(),
        })

    def submit(self, rid, identifier, content, token):
        content = check_content(content)
        thread = self.native.thread_for(rid)
        require(thread is not None, "INSTRUCTION_SCOPE")
        require(not self.native.received_thread(thread), "AGENT_CONVERSATION_READ_ONLY")
        original = self.native._remember(thread, self.native._receipt(rid, token))
        try:
            command = NativeAppend.model_validate_json(canonical({
                "request_id": rid, "conversation_id": original.conversation_id, "append_id": identifier,
                "expected_submit_digest": original.submit_digest, "content": content,
            }))
        except (ValueError, TypeError):
            raise ConsoleError("INSTRUCTION_INVALID", 400) from None
        prior = self.journal.get(identifier, PHASE)
        if prior is not None:
            require(prior["intent"].get("body") == command.model_dump(mode="json"), "INSTRUCTION_CHANGED")
            self.refresh(rid, token)
            result = self._saved(identifier, prior)
            require(result is not None, "RECONCILE_REQUIRED")
            return result
        require(self.proposal(original) == {"request_id": rid, "append_id": identifier, "pending": False},
                "INSTRUCTION_CHANGED")
        self.journal.begin(identifier, PHASE, {
            "parent_request_id": rid, "thread_id": thread, "state_profile": original.state_profile,
            "method": "POST", "body": command.model_dump(mode="json"),
            "body_sha256": sha(canonical(command.model_dump(mode="json"))),
            "created_at": self.native.clock().isoformat(),
        })
        try:
            value = self.native.adapter.append(command, token)
            self.native._current(token)
            self._scope(value, original)
            require(value.content == content and value.append_id == identifier, "NATIVE_RID_CONFLICT", 502)
            self.journal.finish(identifier, PHASE, {
                "state": "CONFIRMED", "receipt": value.model_dump(mode="json"),
                "observed_at": self.native.clock().isoformat(),
            })
            self.errors.pop(rid, None)
            return value
        except ConsoleError as error:
            self.errors[rid] = error.code
            self.journal.finish(identifier, PHASE, {
                "state": "UNKNOWN", "code": error.code, "automatic_retry": False,
                "observed_at": self.native.clock().isoformat(),
            })
            raise

    def refresh(self, rid, token):
        thread = self.native.thread_for(rid)
        require(thread is not None, "INSTRUCTION_SCOPE")
        original = self.native._remember(thread, self.native._receipt(rid, token))
        try:
            page = self.native.adapter.appends(original.conversation_id, rid, token)
            self.native._current(token)
            self._scope(page, original)
            for value in page.appends:
                self._remember(original, value)
            self.pages[rid] = page
            self.errors.pop(rid, None)
            return page
        except ConsoleError as error:
            self.errors[rid] = error.code
            raise

    def views(self, rid):
        entries = {entry.append_id: {**entry.model_dump(mode="json"), "receipt_observed": True}
                   for entry in (() if rid not in self.pages else self.pages[rid].appends)}
        for identifier, local in reversed(self.local(rid)):
            saved = self._saved(identifier, local)
            if saved is not None:
                entries[identifier] = {**saved.model_dump(mode="json"), "submission_state": local["state"],
                                       "receipt_observed": True}
            else:
                entries[identifier] = {
                    "request_id": rid, "append_id": identifier, "content": local["intent"]["body"]["content"],
                    "status": "unknown", "submission_state": local["state"], "delivery": None,
                    "receipt_observed": False,
                }
        return list(entries.values())
