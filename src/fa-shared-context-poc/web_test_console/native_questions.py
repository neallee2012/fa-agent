"""B1 PA-11: the Team owner's answers to the SDK's ask_user, on H1 question rows and the console's once journal.

Questions share H3's adapter, Team scope and the one attention list; answers never use the approval writer.
"""

from pydantic import TypeAdapter

from shared_context.behavior_wire import Digest
from shared_context.native_hitl_wire import (
    NativeHitlAnswer, NativeHitlControlReceipt, NativeHitlPendingQuery, NativeHitlQuestionPage,
    NativeHitlQuestionReadback, NativeHitlQuestionReference, hitl_key,
)
from .adapter import ConsoleError, canonical, require, sha, strict_json

PHASE = "native-question"
PATH = "/answers"
MAX_WRITE_BYTES = 16384
# A finished or refused turn has no live SDK wait left to answer.
_DONE = {"completed", "busy_refused"}
MESSAGES = {
    "NATIVE_HITL_QUESTION_CHANGED": "助理的原問題已不同；請重新讀取，不會把回答套到別的問題。",
    "NATIVE_HITL_ANSWER_INVALID": "請選原問題的選項，或在允許時輸入自己的回答。",
}


class NativeQuestionControl:
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
                    page = self.hitl._read("/turn-questions", NativeHitlQuestionPage, token,
                                           NativeHitlPendingQuery(turn=turn, after=after))
                    require(page.turn == turn and all(self.hitl._turn(ref.call) == turn for ref in page.references)
                            and all(item.question in page.references for item in page.attention),
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
        return [{"thread_id": page["thread_id"], "label": self.hitl.attention_label(page["thread_id"], item.question.call),
                 "item": item.model_dump(mode="json")} for page in self.pages.values() for item in page["attention"]]

    def view(self, thread):
        try:
            commands = self.history() if self.console.vault.state() == "AVAILABLE" else []
            error = self.errors.get(thread)
        except ConsoleError as problem:
            commands, error = [], problem.code
        return {"commands": commands, "error": None if error is None else {"code": error}}

    def _request_id(self, reference):
        # One answer command per original question and owner: a reload or a second click returns the saved original.
        config = self.console.config
        return sha(canonical([config.tenant_id, self.hitl.team_id, config.human_oid, "native-question-answer",
                              reference.model_dump(mode="json")]))

    def _question(self, thread, identifier, token):
        try:
            TypeAdapter(Digest).validate_python(identifier, strict=True)
        except ValueError:
            raise ConsoleError("NATIVE_HITL_INVALID_REQUEST", 400) from None
        value = self.hitl._read("/questions/" + identifier, NativeHitlQuestionReadback, token)
        question, reference = value.question, value.reference
        call = question.command.question.call
        require(question.question_id == reference.question_id == identifier
                and question.sha256 == reference.question_digest and reference.call == call,
                "NATIVE_HITL_RECORD_INVALID", 502)
        self.hitl.call_scope(thread, call, token, read_original=True)
        require(question.tenant_id == self.console.config.tenant_id and question.team_id == self.hitl.team_id,
                "NATIVE_HITL_SCOPE_DENIED", 403)
        return value

    def original(self, identifier):
        entry = self.journal.get(identifier, PHASE)
        require(entry is not None, "NATIVE_HITL_ORIGINAL_REQUIRED")
        try:
            intent = entry["intent"]
            raw = canonical(intent["body"])
            command = NativeHitlAnswer.model_validate_json(raw)
            reference = NativeHitlQuestionReference.model_validate_json(canonical(intent["selection"]))
            require(intent["method"] == "POST" and intent["path"] == PATH
                    and intent["config_sha256"] == self.console.config.sha256 and intent["team_id"] == self.hitl.team_id
                    and intent["body_sha256"] == sha(raw) and raw == canonical(command.model_dump(mode="json"))
                    and identifier == command.request_id == self._request_id(reference)
                    and (command.question_id, command.question_digest)
                    == (reference.question_id, reference.question_digest),
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
                and result.get("question_id") == command.question_id and result.get("status") == "answered",
                "NATIVE_HITL_RECORD_INVALID", 502)
        return receipt

    def answer(self, thread, data):
        with self.console.message_lock(PHASE):
            token = self.hitl.token(thread)
            require(type(data) is dict and set(data) == {"question", "answer", "was_freeform"},
                    "NATIVE_HITL_INVALID_REQUEST", 400)
            try:
                reference = NativeHitlQuestionReference.model_validate_json(canonical(data["question"]))
            except (ValueError, TypeError):
                raise ConsoleError("NATIVE_HITL_INVALID_REQUEST", 400) from None
            self.hitl.call_scope(thread, reference.call, token)
            identifier = self._request_id(reference)
            if self.journal.get(identifier, PHASE) is not None:
                # Saved before: only its original is shown; reconcile it, never POST another answer.
                self.original(identifier)
                return self._result(identifier)
            try:
                command = NativeHitlAnswer.model_validate_json(canonical({
                    "request_id": identifier, "question_id": reference.question_id,
                    "question_digest": reference.question_digest, "answer": data["answer"],
                    "was_freeform": data["was_freeform"]}))
            except (ValueError, TypeError):
                raise ConsoleError("NATIVE_HITL_INVALID_REQUEST", 400) from None
            require(type(data["was_freeform"]) is bool, "NATIVE_HITL_INVALID_REQUEST", 400)
            value = self._question(thread, reference.question_id, token)
            require(value.reference == reference, "NATIVE_HITL_QUESTION_CHANGED")
            require(not value.expired and self.native.clock() < value.question.expires_at, "NATIVE_HITL_EXPIRED")
            require(not value.turn_closed, "NATIVE_HITL_TURN_CLOSED")
            require(value.status == "pending" and value.answer is None, "NATIVE_HITL_STATE_CONFLICT")
            asked = value.question.command.question
            require(asked.allow_freeform if command.was_freeform else command.answer in asked.choices,
                    "NATIVE_HITL_ANSWER_INVALID", 400)
            body = command.model_dump(mode="json")
            self.native._current(token)
            self.hitl.token(thread)
            self.journal.begin(identifier, PHASE, {
                "method": "POST", "path": PATH, "body": body, "body_sha256": sha(canonical(body)),
                "selection": reference.model_dump(mode="json"), "team_id": self.hitl.team_id, "thread_id": thread,
                "config_sha256": self.console.config.sha256, "created_at": self.native.clock().isoformat(),
                # Only to show "助理問：…" again beside the answer after a reload; never sent or compared.
                "display": {"question": asked.question},
            })
            # A failed remote after_commit above leaves UNKNOWN and never reaches the writer.
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


def install_question_routes(app, requested_console):
    from fastapi import Depends, Request
    from starlette.concurrency import run_in_threadpool

    def control(console):
        require(console.native_instructions is not None and console.native_instructions.hitl.enabled,
                "NATIVE_HITL_UNAVAILABLE")
        return console.native_instructions.questions

    async def body(request):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            require(len(raw) <= MAX_WRITE_BYTES, "NATIVE_HITL_INVALID_REQUEST", 413)
        try:
            value = strict_json(bytes(raw))
        except (ValueError, TypeError):
            raise ConsoleError("NATIVE_HITL_INVALID_REQUEST", 400) from None
        require(type(value) is dict, "NATIVE_HITL_INVALID_REQUEST", 400)
        return value

    @app.post("/api/native-hitl/team/answers")
    async def team_answer(request: Request, console=Depends(requested_console)):
        return await run_in_threadpool(control(console).answer, None, await body(request))

    @app.post("/api/native-hitl/team/answer-reconcile")
    async def team_reconcile(request: Request, console=Depends(requested_console)):
        value = await body(request)
        require(set(value) == {"request_id"} and isinstance(value["request_id"], str),
                "NATIVE_HITL_INVALID_REQUEST", 400)
        return await run_in_threadpool(control(console).reconcile, None, value["request_id"])

    @app.post("/api/native-hitl/{thread_id}/answers")
    async def answer(thread_id: str, request: Request, console=Depends(requested_console)):
        return await run_in_threadpool(control(console).answer, thread_id, await body(request))

    @app.post("/api/native-hitl/{thread_id}/answer-reconcile")
    async def reconcile(thread_id: str, request: Request, console=Depends(requested_console)):
        value = await body(request)
        require(set(value) == {"request_id"} and isinstance(value["request_id"], str), "NATIVE_HITL_INVALID_REQUEST", 400)
        return await run_in_threadpool(control(console).reconcile, thread_id, value["request_id"])
