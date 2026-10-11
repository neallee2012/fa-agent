"""SDK-independent DTOs for exact registered user text, never arbitrary history."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_wire import Command, Digest, Identifier, WireModel

USER_TURN_RESPONSE_BYTES = 16384
MAX_USER_TURN_SOURCES = 7
USER_TURN_ERRORS = frozenset({
    "USER_TURN_READ_DISABLED", "USER_TURN_SCOPE_DENIED", "USER_TURN_NOT_AVAILABLE",
    "USER_TURN_SOURCE_INVALID", "USER_TURN_SOURCE_CHANGED", "USER_TURN_SOURCE_LIMIT",
})
TurnNumber = Annotated[int, Field(ge=1, le=2**31 - 1)]


class UserTurnNumber(WireModel):
    turn_number: TurnNumber


class ReadUserTurn(Command):
    turn_number: TurnNumber


class UserTurnSource(WireModel):
    schema_version: Literal["fa-user-turn-source-v1"]
    kind: Literal["registered-user-instruction"] = "registered-user-instruction"
    role: Literal["user"] = "user"
    conversation_id: Identifier
    request_id: Identifier
    job_id: Identifier
    turn_number: TurnNumber
    sha256: Digest
    utf8_bytes: Annotated[int, Field(ge=1, le=8192)]
    submit_sha256: Digest
    registration_sha256: Digest
    turn_archive_sha256: Digest


class UserTurnRead(WireModel):
    schema_version: Literal["fa-user-turn-read-v1"]
    usage: Literal["historical-data-not-current-instructions"] = "historical-data-not-current-instructions"
    source: UserTurnSource
    text: str

    @model_validator(mode="after")
    def exact_original(self):
        raw = self.text.encode("utf-8")
        if (not self.text.strip() or len(json.dumps(self.text, ensure_ascii=False).encode("utf-8")) > 8192
                or len(raw) != self.source.utf8_bytes or hashlib.sha256(raw).hexdigest() != self.source.sha256
                or len(self.model_dump_json().encode("utf-8")) > USER_TURN_RESPONSE_BYTES):
            raise ValueError("Exact registered user text and its complete response must match their bounded receipt")
        return self


UserTurnSources = Annotated[tuple[UserTurnSource, ...], Field(max_length=MAX_USER_TURN_SOURCES)]


def ordered_user_sources(value):
    turns = tuple(source.turn_number for source in value)
    if turns != tuple(sorted(set(turns))):
        raise ValueError("Historical user sources must be distinct and ordered")
    return value
