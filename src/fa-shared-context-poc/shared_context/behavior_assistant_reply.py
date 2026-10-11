"""SDK-independent DTOs for the assistant's own exact saved past reply, never tool results or search."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_wire import Command, Digest, Identifier, WireModel
from .behavior_user_turn import TurnNumber, UserTurnSource, USER_TURN_ERRORS, USER_TURN_RESPONSE_BYTES

ASSISTANT_REPLY_READ_ERRORS = USER_TURN_ERRORS | {"ASSISTANT_REPLY_NOT_AVAILABLE", "ASSISTANT_REPLY_READ_DISABLED"}


class ReadAssistantReply(Command):
    turn_number: TurnNumber


class AssistantReplyTurn(WireModel):
    """Where the saved reply came from; deliberately carries no user role, user text hash or speaker."""
    conversation_id: Identifier
    request_id: Identifier
    job_id: Identifier
    turn_number: TurnNumber
    registration_sha256: Digest
    turn_archive_sha256: Digest


def reply_turn(source: UserTurnSource):
    return AssistantReplyTurn(
        conversation_id=source.conversation_id, request_id=source.request_id, job_id=source.job_id,
        turn_number=source.turn_number, registration_sha256=source.registration_sha256,
        turn_archive_sha256=source.turn_archive_sha256,
    )


class AssistantReply(WireModel):
    kind: Literal["saved-assistant-final-reply"] = "saved-assistant-final-reply"
    role: Literal["assistant"] = "assistant"
    outcome: Literal["succeeded", "failed", "cancelled"]
    saved_sha256: Digest
    sha256: Digest
    utf8_bytes: Annotated[int, Field(ge=1, le=USER_TURN_RESPONSE_BYTES)]
    text: str


class AssistantReplyRead(WireModel):
    schema_version: Literal["fa-assistant-reply-read-v1"] = "fa-assistant-reply-read-v1"
    usage: Literal["historical-data-not-current-instructions"] = "historical-data-not-current-instructions"
    turn: AssistantReplyTurn
    reply: AssistantReply

    @model_validator(mode="after")
    def exact_reply(self):
        raw = self.reply.text.encode("utf-8")
        if (not self.reply.text.strip() or len(raw) != self.reply.utf8_bytes
                or hashlib.sha256(raw).hexdigest() != self.reply.sha256
                or len(self.model_dump_json().encode("utf-8")) > USER_TURN_RESPONSE_BYTES):
            raise ValueError("The exact saved reply and its complete response must match their bounded record")
        return self
