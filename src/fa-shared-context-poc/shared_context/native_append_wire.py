"""Owner append controls; the original turn and its authority remain unchanged."""

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .behavior_assistant_wire import InstructionSubmit
from .behavior_wire import Digest, Identifier, WireModel


def check_append_status(status, message_id, delivery, reason):
    if status == "applied":
        if not message_id or delivery is None or reason is not None:
            raise ValueError("Applied append controls require the original SDK delivery event")
    elif delivery is not None:
        raise ValueError("An unconfirmed append cannot claim SDK delivery")
    if status == "accepted" and (message_id is not None or reason is not None):
        raise ValueError("An accepted append has not attempted SDK delivery")


class NativeAppend(WireModel):
    operation: Literal["append"] = "append"
    request_id: Identifier
    conversation_id: Identifier
    append_id: Identifier
    expected_submit_digest: Digest
    content: str

    @field_validator("content")
    @classmethod
    def bounded_text(cls, value):
        return InstructionSubmit.transport_text(value)


class NativeAppendRead(WireModel):
    operation: Literal["append_read"] = "append_read"
    request_id: Identifier
    conversation_id: Identifier
    append_id: Identifier | None = None


class NativeAppendReceipt(WireModel):
    schema_version: Literal["fa-native-append-v1"] = "fa-native-append-v1"
    agent_id: Identifier
    hosted_session_id: Identifier
    request_id: Identifier
    conversation_id: Identifier
    append_id: Identifier
    submit_digest: Digest
    sequence: Annotated[int, Field(ge=1)]
    content: str
    status: Literal["accepted", "sending", "applied", "not_applied", "unknown"]
    sdk_message_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    delivery: Literal["steering", "queued", "idle"] | None = None
    reason: Literal["turn_finished", "cancelled", "sdk_unconfirmed"] | None = None
    authorization: Literal[False] = False

    @model_validator(mode="after")
    def delivery_state(self):
        check_append_status(self.status, self.sdk_message_id, self.delivery, self.reason)
        return self


class NativeAppends(WireModel):
    schema_version: Literal["fa-native-appends-v1"] = "fa-native-appends-v1"
    agent_id: Identifier
    hosted_session_id: Identifier
    request_id: Identifier
    conversation_id: Identifier
    submit_digest: Digest
    appends: tuple[NativeAppendReceipt, ...]

    @model_validator(mode="after")
    def original_scope(self):
        scope = (self.agent_id, self.hosted_session_id, self.request_id, self.conversation_id, self.submit_digest)
        if (len({entry.append_id for entry in self.appends}) != len(self.appends)
                or any((entry.agent_id, entry.hosted_session_id, entry.request_id, entry.conversation_id,
                        entry.submit_digest) != scope or entry.sequence != index
                       for index, entry in enumerate(self.appends, 1))):
            raise ValueError("Append history must retain its original scope, IDs and order")
        return self
