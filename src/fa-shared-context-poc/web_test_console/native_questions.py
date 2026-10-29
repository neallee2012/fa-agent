"""B1 PA-11: the Team owner's answers to the SDK's ask_user, on H1 question rows and the console's once journal.

Questions share H3's adapter, Team scope and the one attention list; answers never use the approval writer.
"""

from shared_context.native_hitl_wire import (
    NativeHitlAnswer, NativeHitlQuestionPage, NativeHitlQuestionReadback, NativeHitlQuestionReference,
)
from .adapter import ConsoleError, canonical, require, sha, strict_json
from .native_hitl_rows import NativeHitlRowControl

PHASE = "native-question"
PATH = "/answers"
MAX_WRITE_BYTES = 16384
MESSAGES = {
    "NATIVE_HITL_QUESTION_CHANGED": "助理的原問題已不同；請重新讀取，不會把回答套到別的問題。",
    "NATIVE_HITL_ANSWER_INVALID": "請選原問題的選項，或在允許時輸入自己的回答。",
}


class NativeQuestionControl(NativeHitlRowControl):
    NOUN, PHASE, PATH, REQUEST_TAG = "question", PHASE, PATH, "native-question-answer"
    PAGE_PATH, PAGE = "/turn-questions", NativeHitlQuestionPage
    READBACK_PATH, READBACK = "/questions/", NativeHitlQuestionReadback
    COMMAND, REFERENCE = NativeHitlAnswer, NativeHitlQuestionReference

    @staticmethod
    def _status(command):
        return "answered"

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
            value = self._readback(thread, reference.question_id, token)
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
