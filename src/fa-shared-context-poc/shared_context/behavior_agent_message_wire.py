"""SDK-independent, private-free peer-message transport contracts."""

from typing import Annotated, Literal
import json
import re
from datetime import datetime, timezone

from pydantic import Field, field_validator, model_validator

from .behavior_wire import Digest, Identifier, WireModel


CAPABILITY_ID = "fa-agent-collaboration-v1"
# PA-03: added where the roster would be when this turn did not select collaboration, so an earlier roster from history
# or memory is not presented as current. It names only same-Team teammates, not fa-delegate-v1's child (task).
NOT_SELECTED_REASON = (
    "本回合沒有選用同 Team 協作能力，所以沒有同事名冊，也沒有 send_agent_message，現在不能把工作交給同 Team 的其他助理。"
    "之前回合或記憶裡的同事名單是當時的設定，不代表現在可以交給他們。"
    "被問到可以交給哪些同事時，說明這回合沒有開啟同 Team 協作、目前無法交給同事，不要再列出舊名單。")
MAX_AGENT_MESSAGE_BYTES = 32 * 1024
AGENT_MESSAGE_HTTP_BYTES = MAX_AGENT_MESSAGE_BYTES + 4096
AGENT_MESSAGE_ERRORS = frozenset({
    "AGENT_MESSAGE_ALLOWANCE", "AGENT_MESSAGE_ARGUMENTS", "AGENT_MESSAGE_ASSIGNMENT_DENIED", "AGENT_MESSAGE_CONFLICT",
    "AGENT_MESSAGE_DISABLED", "AGENT_MESSAGE_HANDOFF_EXISTS", "AGENT_MESSAGE_IDENTITY_DENIED",
    "AGENT_MESSAGE_LIMIT", "AGENT_MESSAGE_ORIGINAL_REQUIRED", "AGENT_MESSAGE_PROFILE_UNSUPPORTED",
    "AGENT_MESSAGE_QUEUE_FULL", "AGENT_MESSAGE_READ_DENIED", "AGENT_MESSAGE_RECEIPT_INVALID",
    "AGENT_MESSAGE_RECIPIENT_DENIED", "AGENT_MESSAGE_RECIPIENT_NOT_READY", "AGENT_MESSAGE_RECORD_INVALID",
    "AGENT_MESSAGE_REFS_CHANGED", "AGENT_MESSAGE_REFS_DENIED", "AGENT_MESSAGE_REFS_INVALID",
    "AGENT_MESSAGE_REPLY_DENIED", "AGENT_MESSAGE_RESULT_EXISTS", "AGENT_MESSAGE_RESULT_UNCONFIRMED", "AGENT_MESSAGE_ROOT_DENIED",
    "AGENT_MESSAGE_ROOT_EXISTS", "AGENT_MESSAGE_ROOT_EXPIRED", "AGENT_MESSAGE_ROUTE_DENIED",
    "AGENT_MESSAGE_SHARE_DENIED", "AGENT_MESSAGE_UNAVAILABLE", "AGENT_MESSAGE_UNKNOWN",
    "AGENT_MESSAGE_WAIT_UNAVAILABLE",
    "BEHAVIOR_BINDING_DENIED", "BEHAVIOR_CURRENT_POLICY_DENIED", "BEHAVIOR_SOURCE_DENIED",
    "WORK_POLICY_CURRENT_STATE_MISSING", "WORK_POLICY_DISABLED", "WORK_POLICY_REVOKED",
    "WORK_POLICY_SCOPE_CHANGED", "INSTRUCTION_HOSTED_CONFIGURATION", "AGENT_VERSION_FOLLOWING",
    "SOURCE_EXPIRED", "SOURCE_REFERENCE_MISMATCH", "SOURCE_WITHDRAWN",
})
MessageKind = Literal["request", "handoff", "result", "blocked", "fyi", "receipt"]
MessageStatus = Literal["queued", "silenced", "needs_attention", "claimed", "completed", "unknown"]


class AgentMessageRecipient(WireModel):
    agent_id: Identifier
    display_name: Annotated[str, Field(min_length=1, max_length=128)]


class TeamReference(WireModel):
    visibility: Literal["team"] = "team"
    resource_kind: Literal["source", "artifact"] | None = Field(default=None, exclude_if=lambda value: value is None)
    tenant_id: Identifier
    team_id: Identifier
    resource_id: Identifier
    version: Identifier
    sha256: Digest


class AgentMessage(WireModel):
    message_id: Identifier
    recipient_agent_id: Identifier
    kind: MessageKind
    content: str
    in_reply_to: Identifier | None = None
    refs: Annotated[tuple[TeamReference, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def data_only(self):
        if self.kind == "receipt":
            if self.content or self.refs:
                raise ValueError("transport receipts cannot carry another request or payload")
        elif not self.content.strip() and not self.refs:
            raise ValueError("a complete public brief or Team reference is required")
        if len(set(self.refs)) != len(self.refs):
            raise ValueError("message references must be unique")
        return self


class WakeDecision(WireModel):
    action: Literal["wake", "queued", "silence", "needs_attention"]
    reason: str
    settles_human_obligation: Literal[False] = False
    model_dispatch_authorized: Literal[False] = False


class PeerTeamReference(TeamReference):
    resource_kind: Literal["source", "artifact"]

    @model_validator(mode="after")
    def fixed_version(self):
        pattern = r"[1-9][0-9]*" if self.resource_kind == "source" else r"[a-f0-9]{32}"
        if re.fullmatch(pattern, self.version) is None:
            raise ValueError("the declared resource kind requires its exact canonical version")
        return self


class AgentMessageSend(WireModel):
    request_id: Literal["agent-message-send"] = "agent-message-send"
    message: AgentMessage

    @model_validator(mode="after")
    def explicit_resources(self):
        if self.message.kind in {"request", "handoff"} and len(
                json.dumps(self.message.content, ensure_ascii=False).encode("utf-8")) > 8192:
            raise ValueError("the complete peer brief must fit the existing instruction transport")
        for ref in self.message.refs:
            PeerTeamReference.model_validate(ref.model_dump(mode="json"))
        return self


class AgentMessageReadCommand(WireModel):
    request_id: Literal["agent-message-read"] = "agent-message-read"
    message_key: Identifier


class AgentMessageProjection(WireModel):
    message_key: Identifier
    message_id: Identifier
    root_request_id: Identifier
    sender_agent_id: Identifier
    recipient_agent_id: Identifier
    from_work_id: Identifier
    to_work_id: Identifier
    from_conversation_id: Identifier
    to_conversation_id: Identifier
    kind: MessageKind
    hop: Annotated[int, Field(ge=1, le=2)]
    status: MessageStatus
    decision: WakeDecision
    human_obligation_fulfilled: Literal[False] = False


class AgentMessageRead(WireModel):
    schema_version: Literal["fa-agent-message-read-v1"] = "fa-agent-message-read-v1"
    message: AgentMessageProjection
    content: str
    refs: Annotated[tuple[PeerTeamReference, ...], Field(max_length=8)] = ()
    payload_sha256: Digest
    response_message_key: Identifier | None = None
    fresh: bool = False
    root_deadline: datetime | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("root_deadline")
    @classmethod
    def trusted_time(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value)):
            raise ValueError("the original peer deadline must use explicit UTC")
        return value


class AgentMessageList(WireModel):
    schema_version: Literal["fa-agent-message-list-v1"] = "fa-agent-message-list-v1"
    agent_id: Identifier
    conversation_id: Identifier
    binding_id: Identifier
    messages: Annotated[tuple[AgentMessageProjection, ...], Field(max_length=64)] = ()


class PublicAgentEntry(WireModel):
    agent_id: Identifier
    display_name: Annotated[str, Field(min_length=1, max_length=128)]
    api_prefix: str
    state: Literal["ready", "unavailable"]
    reason: Identifier | None = None

    @model_validator(mode="after")
    def server_route(self):
        if self.api_prefix != "/agents/" + self.agent_id or (self.state == "ready") != (self.reason is None):
            raise ValueError("directory entries require their exact server route and explicit availability")
        return self


MAX_TEAM_AGENTS = 16


class PublicAgentDirectory(WireModel):
    schema_version: Literal["fa-agent-directory-v1"] = "fa-agent-directory-v1"
    default_agent_id: Identifier
    agents: Annotated[tuple[PublicAgentEntry, ...], Field(min_length=1, max_length=MAX_TEAM_AGENTS)]

    @model_validator(mode="after")
    def explicit_default(self):
        if self.default_agent_id not in {entry.agent_id for entry in self.agents}:
            raise ValueError("the legacy route must identify one server directory entry")
        return self
