"""Native relay DTOs shared by the API and Hosted tool adapter, without runtime SDK imports."""

from typing import Annotated, Literal

from pydantic import Field, RootModel, field_validator, model_validator

from .behavior_agent_message_wire import AgentMessageRecipient, MAX_TEAM_AGENTS, PeerTeamReference
from .behavior_assistant_wire import InstructionSubmit
from .behavior_wire import Digest, Identifier, WireModel
from .native_responses_wire import (
    NATIVE_STATE_PROFILE, NativeResponseDispatch, NativeResponseId, NativeStateProfile, check_response_state,
    unlabeled_profile,
)

NATIVE_AGENT_PREFIX = "/work-policy/v1/native-agent-messages"


class NativePeerRequest(WireModel):
    recipient_agent_id: Identifier
    kind: Literal["request", "handoff"]
    content: Annotated[str, Field(min_length=1)]
    refs: Annotated[tuple[PeerTeamReference, ...], Field(max_length=8)] = ()

    @field_validator("content")
    @classmethod
    def bounded_brief(cls, value):
        return InstructionSubmit.transport_text(value)

    @field_validator("refs")
    @classmethod
    def distinct_refs(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Message references cannot be repeated")
        return value


class NativeRelaySend(WireModel):
    conversation_id: Identifier
    request_id: Identifier
    tool_call_id: Annotated[str, Field(min_length=1, max_length=256)]
    message: NativePeerRequest


class NativeRelayAcceptance(WireModel):
    message_id: Identifier
    recipient_agent_id: Identifier
    conversation_id: Identifier
    submit_digest: Digest
    status: Literal["accepted", "started", "completed", "unknown", "busy_refused"]
    source_conversation_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    source_request_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    state_profile: NativeStateProfile = Field(
        default=NATIVE_STATE_PROFILE, exclude_if=unlabeled_profile)
    response_id: NativeResponseId | None = Field(default=None, exclude_if=lambda value: value is None)
    dispatch: NativeResponseDispatch | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def original_dispatch(self):
        check_response_state(self.state_profile, self.status, self.message_id, self.response_id, self.dispatch)
        return self


def relay_acceptance_scope(accepted: NativeRelayAcceptance) -> dict:
    return accepted.model_dump(mode="json", include={
        "message_id", "recipient_agent_id", "conversation_id", "submit_digest",
        "source_conversation_id", "source_request_id", "state_profile",
    })


class NativeRelayQueued(WireModel):
    status: Literal["queued", "accepted", "not_delivered", "unknown"] = "queued"
    agent_id: Identifier
    conversation_id: Identifier
    request_id: Identifier
    recipient_agent_id: Identifier
    tool_call_id: Annotated[str, Field(min_length=1, max_length=256)]
    task_id: Digest
    command_sha256: Digest
    reason: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,95}$")] | None = Field(
        default=None, exclude_if=lambda value: value is None)


class NativeRelaySendResult(RootModel[NativeRelayAcceptance | NativeRelayQueued]):
    pass


def relay_task_id(agent_id: str, command: NativeRelaySend) -> str:
    from .behavior_native_turn_wire import material_digest
    return material_digest({
        "kind": "fa-native-version-message-v1", "agent": agent_id, "command": command.model_dump(mode="json")})


class NativeRelayResult(WireModel):
    conversation_id: Identifier
    request_id: Identifier


class NativeRelayReadResult(NativeRelayResult):
    part: Annotated[int, Field(ge=0)]


class NativePeerDirectory(WireModel):
    agents: Annotated[tuple[AgentMessageRecipient, ...], Field(max_length=MAX_TEAM_AGENTS)]
