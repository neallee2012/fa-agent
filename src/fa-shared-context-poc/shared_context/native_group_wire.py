"""SDK-independent group conversation records; group counts never replace native relay quota."""

import os
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .behavior_agent_message_wire import MAX_TEAM_AGENTS
from .behavior_assistant_wire import InstructionSubmit
from .behavior_native_agent_wire import NativeRelayResult
from .behavior_native_turn_wire import NativeReceipt, NativeResultReference, NativeTurnAddress
from .behavior_wire import Digest, Identifier, WireModel

MAX_GROUP_ROUNDS = 3
MAX_GROUP_POSTS = 10
MAX_MEMBER_ROUND_POSTS = 3
MAX_GROUP_MEMBERS = MAX_TEAM_AGENTS
GroupPostKind = Literal["request", "result", "handoff", "fyi", "blocked", "final"]
NATIVE_GROUP_PREFIX = "/native-groups/v1"
NATIVE_GROUP_ERRORS = frozenset("NATIVE_GROUP_" + suffix for suffix in (
    "UNKNOWN", "STATE_INVALID", "SCOPE_DENIED", "UNAVAILABLE", "DISABLED", "MEMBER_REQUIRED",
    "NOT_FOUND", "INVALID", "AGENT_REQUIRED", "ORIGINAL_RESULT_REQUIRED", "TURN_CLOSED",
    "ORCHESTRATOR_REQUIRED", "CONFLICT", "ALLOWANCE", "CLOSED", "ROUND_CHANGED", "PENDING",
    "POST_NOT_FOUND", "ROUTE_UNAVAILABLE", "REPLY_REQUIRED", "REQUEST_REJECTED",
))
NATIVE_GROUP_FAILURE_GUIDANCE = {
    "NATIVE_GROUP_ROUND_CHANGED": (
        "群組輪次不符，不是權限拒絕。請先用 read_group 讀取目前輪次並核對；"
        "已到原預期輪次就不要再換輪，仍需換輪才呼叫 next_group_round。"
        "expected_round 填目前輪次，不是要切換到的輪次。保留原群組，不要重開。"),
    "NATIVE_GROUP_REPLY_REQUIRED": (
        "群組成員不能把新工作交辦回主持者，以免循環等待。send_agent_message 是交辦工具，不是回覆工具；"
        "請完成目前工作，原結果會由系統保存並回給主持者。無法完成就如實說明；"
        "只補充資訊請用 share_group_fyi，不要換 ID 再交辦。"),
}


def groups_enabled(environ=None):
    flag = (os.environ if environ is None else environ).get("FA_NATIVE_GROUPS_ENABLED", "")
    if flag not in {"", "0", "1"}:
        raise ValueError("FA_NATIVE_GROUPS_ENABLED must be absent, 0 or 1")
    return flag == "1"


def _bounded_content(value):
    if not value.strip() or len(value.encode("utf-8")) > 8192:
        raise ValueError("one nonempty group text within 8192 UTF-8 bytes is required")
    return value


class NativeGroupOpen(WireModel):
    request_id: Identifier
    orchestrator_agent_id: Identifier
    member_agent_ids: Annotated[tuple[Identifier, ...], Field(min_length=2, max_length=MAX_GROUP_MEMBERS)]
    content: Annotated[str, Field(min_length=1)]

    @field_validator("content")
    @classmethod
    def original_native_text(cls, value):
        return InstructionSubmit.transport_text(_bounded_content(value))

    @model_validator(mode="after")
    def original_members(self):
        if (len(set(self.member_agent_ids)) != len(self.member_agent_ids)
                or self.orchestrator_agent_id not in self.member_agent_ids):
            raise ValueError("distinct original members must include the orchestrator")
        return self


class NativeGroupPostReference(WireModel):
    post_id: Digest
    speaker_agent_id: Identifier
    round: Annotated[int, Field(ge=1, le=MAX_GROUP_ROUNDS)]
    content_sha256: Digest
    kind: GroupPostKind
    recipient_agent_id: Identifier | None = None
    turn: NativeTurnAddress | None = None
    result: NativeResultReference | None = None


class NativeGroupPost(WireModel):
    request_id: Identifier
    round: Annotated[int, Field(ge=1, le=MAX_GROUP_ROUNDS)]
    kind: GroupPostKind
    content: Annotated[str, Field(min_length=1)]
    recipient_agent_id: Identifier | None = None
    turn: NativeTurnAddress | None = None
    result: NativeResultReference | None = None

    _content = field_validator("content")(_bounded_content)


class NativeGroupPostView(WireModel):
    reference: NativeGroupPostReference
    content: str


class NativeGroupAdvance(WireModel):
    request_id: Identifier
    expected_round: Annotated[int, Field(ge=1, le=MAX_GROUP_ROUNDS)]


class NativeGroupRoundReceipt(WireModel):
    group_id: Digest
    request_id: Identifier
    to_round: Annotated[int, Field(ge=2, le=MAX_GROUP_ROUNDS)]


class NativeGroupSignal(WireModel):
    event_id: Digest
    recipient_agent_id: Identifier
    kind: Literal["fyi", "receipt"]
    payload_sha256: Digest


class NativeGroupSignalReceipt(WireModel):
    group_id: Digest
    event_id: Digest
    recipient_agent_id: Identifier
    action: Literal["silence"] = "silence"
    reason: Literal["fyi_no_reply_requested", "transport_receipt"]
    start_work: Literal[False] = False


class NativeGroupDelivery(WireModel):
    message_id: Digest
    sender_agent_id: Identifier
    recipient_agent_id: Identifier
    round: Annotated[int, Field(ge=1, le=MAX_GROUP_ROUNDS)]
    status: Literal["awaiting", "completed", "blocked"] = "awaiting"
    refusal_code: Literal["NATIVE_TURNS_DISABLED", "NATIVE_SESSION_CHANGED"] | None = None

    @model_validator(mode="after")
    def original_refusal(self):
        if self.refusal_code is not None and self.status != "blocked":
            raise ValueError("a verified pre-admission refusal keeps its blocked state")
        return self


class NativeGroupView(WireModel):
    schema_version: Literal["fa-native-group-v1"] = "fa-native-group-v1"
    group_id: Digest
    opening_request_id: Identifier
    question: str
    orchestrator_agent_id: Identifier
    member_agent_ids: Annotated[tuple[Identifier, ...], Field(min_length=2, max_length=MAX_GROUP_MEMBERS)]
    round: Annotated[int, Field(ge=1, le=MAX_GROUP_ROUNDS)] = 1
    state: Literal["open", "closed"] = "open"
    posts: Annotated[tuple[NativeGroupPostReference, ...], Field(max_length=MAX_GROUP_POSTS)] = ()
    deliveries: Annotated[tuple[NativeGroupDelivery, ...], Field(max_length=MAX_GROUP_POSTS)] = ()
    awaiting_final: bool = True

    @model_validator(mode="after")
    def original_limits(self):
        if (len(set(self.member_agent_ids)) != len(self.member_agent_ids)
                or self.orchestrator_agent_id not in self.member_agent_ids
                or len({post.post_id for post in self.posts}) != len(self.posts)):
            raise ValueError("group members and published post IDs must remain distinct")
        counts = {}
        for post in self.posts:
            if post.speaker_agent_id not in self.member_agent_ids or post.round > self.round:
                raise ValueError("published posts retain their original group and round")
            key = (post.speaker_agent_id, post.round)
            counts[key] = counts.get(key, 0) + 1
            if counts[key] > MAX_MEMBER_ROUND_POSTS:
                raise ValueError("member publication count exceeds the round allowance")
        finals = [post for post in self.posts if post.kind == "final"]
        if (len({item.message_id for item in self.deliveries}) != len(self.deliveries)
                or any(item.round > self.round or item.sender_agent_id not in self.member_agent_ids
                       or item.recipient_agent_id not in self.member_agent_ids for item in self.deliveries)):
            raise ValueError("deliveries retain distinct original messages and members")
        pending = [item for item in self.deliveries if item.status == "awaiting"]
        for item in pending:
            key = (item.recipient_agent_id, item.round)
            counts[key] = counts.get(key, 0) + 1
            if counts[key] > MAX_MEMBER_ROUND_POSTS:
                raise ValueError("a pending result retains its member's publication slot")
        if self.state == "closed":
            if (pending or len(finals) != 1 or self.posts[-1] != finals[0] or self.awaiting_final
                    or finals[0].speaker_agent_id != self.orchestrator_agent_id):
                raise ValueError("a closed group retains its one saved orchestrator conclusion")
        elif (finals or not self.awaiting_final or len(self.posts) + len(pending) >= MAX_GROUP_POSTS
              or any(agent == self.orchestrator_agent_id and count >= MAX_MEMBER_ROUND_POSTS
                     for (agent, _round), count in counts.items())):
            raise ValueError("an open group must preserve its final publication allowance")
        return self


class NativeGroupStarted(WireModel):
    group_id: Digest
    receipt: NativeReceipt


class NativeGroupContext(WireModel):
    group: NativeGroupView | None = None
    root: NativeTurnAddress | None = None
    role: Literal["orchestrator", "member"] | None = None


class NativeGroupCompletion(WireModel):
    group_id: Digest | None = None
    post: NativeGroupPostReference | None = None
    reason: Literal["published", "no_group", "delegated", "awaiting_results", "publication_limit", "closed", "old_round"]


class NativeGroupTurnAction(NativeRelayResult):
    tool_call_id: Annotated[str, Field(min_length=1, max_length=256)]


class NativeGroupFyi(NativeGroupTurnAction):
    content: str
    recipient_agent_id: Identifier | None = None

    _content = field_validator("content")(_bounded_content)


class NativeGroupNextRound(NativeGroupTurnAction):
    expected_round: Annotated[int, Field(ge=1, le=MAX_GROUP_ROUNDS)]


class NativeGroupTranscript(WireModel):
    group: NativeGroupView
    posts: Annotated[tuple[NativeGroupPostView, ...], Field(max_length=MAX_GROUP_POSTS)]


class NativeGroupOptions(WireModel):
    enabled: bool
    max_rounds: Literal[3] = 3
    max_posts: Literal[10] = 10
    max_member_round_posts: Literal[3] = 3
    images: Literal[False] = False
    member_agent_ids: tuple[Identifier, ...] = ()
    default_agent_id: Identifier | None = None


class NativeGroupList(WireModel):
    groups: Annotated[tuple[NativeGroupView, ...], Field(max_length=20)]
    next_after: Digest | None = None


class NativeGroupListQuery(WireModel):
    after: Digest | None = None


class NativeGroupActivity(WireModel):
    group_id: Digest
    turns: tuple[NativeReceipt, ...]
    unconfirmed_request_ids: tuple[Identifier, ...] = ()
