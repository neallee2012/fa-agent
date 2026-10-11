"""Native HITL application contracts; no Copilot or Hosted SDK, waiter or executor."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from .behavior_native_turn_wire import MAX_HITL_REVIEW_PAGE, NativeTurnAddress, StateStoreCursor, material_digest
from .behavior_delegation_wire import DELEGATION_PREPARE_BYTES
from .behavior_wire import Command, Digest, Identifier, WireModel
from .connector_registry import ConnectorId
from .tool_catalog import _TOOLBOX_NAME

MAX_RULES = 20
NATIVE_HITL_PREFIX = "/native-hitl/v1"
ENABLED_ENV = "FA_NATIVE_HITL_ENABLED"
TTL_ENV = "FA_NATIVE_HITL_TTL_SECONDS"
INTERACTIVE_TTL_ENV = "FA_NATIVE_HITL_INTERACTIVE_TTL_SECONDS"
NATIVE_HITL_BODY_BYTES = DELEGATION_PREPARE_BYTES + 24 * 1024
NATIVE_HITL_HTTP_BYTES = 512 * 1024
NATIVE_HITL_ERRORS = frozenset({
    "NATIVE_HITL_SCOPE_DENIED", "NATIVE_HITL_OWNER_ONLY", "NATIVE_HITL_AGENT_ONLY",
    "NATIVE_HITL_NOT_FOUND", "NATIVE_HITL_RECORD_INVALID", "NATIVE_HITL_COMMAND_CONFLICT",
    "NATIVE_HITL_RULES_CHANGED", "NATIVE_HITL_RULE_LIMIT", "NATIVE_HITL_RULE_MISSING",
    "NATIVE_HITL_CALL_EXISTS", "NATIVE_HITL_EXPIRED", "NATIVE_HITL_ACTION_CHANGED",
    "NATIVE_HITL_STATE_CONFLICT", "NATIVE_HITL_INVALID_REQUEST",
    "NATIVE_HITL_TURN_CLOSED", "NATIVE_HITL_REVIEW_DENIED", "NATIVE_HITL_PERMISSION_REQUIRED",
    "NATIVE_HITL_TAKEOVER", "NATIVE_HITL_ALREADY_AUTHORIZED",
    "NATIVE_HITL_MATERIAL_UNAVAILABLE", "NATIVE_HITL_QUESTION_CHANGED", "NATIVE_HITL_ANSWER_INVALID",
    "NATIVE_HITL_HUMAN_DENIED", "NATIVE_HITL_INTERRUPTED", "NATIVE_HITL_EFFECT_UNKNOWN",
})
NATIVE_HITL_FAILURE_GUIDANCE = {
    "NATIVE_HITL_HUMAN_DENIED": "真人已拒絕這個原操作。",
    "NATIVE_HITL_INTERRUPTED": "系統中斷了原批准等待；不是人類否決。",
    "NATIVE_HITL_EXPIRED": "原批准等待期限已到；不是人類否決。",
    "NATIVE_HITL_EFFECT_UNKNOWN": "原操作效果未確認；不是人類否決，也不能推定尚未執行。",
    "NATIVE_HITL_TURN_CLOSED": "原回合已結束或停止，不能恢復批准等待；不是人類否決。",
    "NATIVE_HITL_REVIEW_DENIED": "獨立審查未允許這個原操作；不是人類否決。",
    "NATIVE_HITL_TAKEOVER": "這一步須由真人在 VM viewer 接手；不可代填登入、密碼、2FA、CAPTCHA 或付款確認。",
    "NATIVE_HITL_PERMISSION_REQUIRED": "原操作沒有可用的批准；不能當成人類已否決或效果已完成。",
    "NATIVE_HITL_ALREADY_AUTHORIZED": "原呼叫已有放行或停止紀錄；這不是新的執行許可，也不是人類否決。",
    "NATIVE_HITL_AUTHORITY_UNAVAILABLE": "原批准狀態無法核清；不是人類否決。",
    "NATIVE_HITL_RECORD_INVALID": "原批准資料未通過完整性核對；不是人類否決。",
    "NATIVE_HITL_ACTION_CHANGED": "原操作的內容或呼叫範圍不同，已停止；不是人類否決。",
    "NATIVE_HITL_SCOPE_DENIED": "原操作的身分或 Team 範圍不符，已停止；不是人類否決。",
    "NATIVE_HITL_NOT_STARTED": (
        "批准步驟沒有完成（例如批准紀錄寫入時與其他寫入衝突），這次呼叫在執行前就停止，工具沒有執行，也沒有任何效果；"
        "不是人類否決，也不是權限拒絕。若仍需要，可以用新的呼叫再做一次（會重新經過批准）；使用者要求重試時照做。"),
    "NATIVE_HITL_ROUTINE_HUMAN_ONLY": (
        "排程只能由真人在對話裡請求後提出；這次是排程觸發或其他助理的回合，沒有出卡，也沒有建立排程。"
        "不要在這個回合再呼叫 propose_routine；需要時請在回覆裡說明，等使用者自己提出。"),
}


def native_hitl_failure_feedback(code: str | None) -> str:
    if not isinstance(code, str) or code not in NATIVE_HITL_FAILURE_GUIDANCE:
        code = "NATIVE_HITL_AUTHORITY_UNAVAILABLE"
    if code in {"NATIVE_HITL_NOT_STARTED", "NATIVE_HITL_ROUTINE_HUMAN_ONLY"}:
        # Nothing ran, so a new call is not a resend of an unknown effect.
        return f"[{code}] {NATIVE_HITL_FAILURE_GUIDANCE[code]}"
    if code == "NATIVE_HITL_AUTHORITY_UNAVAILABLE":
        return (
            f"[{code}] 這一次呼叫的批准狀態暫時無法確認；不是人類否決。"
            "請核對這一次呼叫的原紀錄，不要重送它或宣稱效果已完成。"
            "使用者之後的新請求可以依當時權限重新嘗試；若涉及原操作的未知效果，"
            "仍須保留 UNKNOWN 並先核對，不能把同一操作當成新請求重做。")
    return f"[{code}] {NATIVE_HITL_FAILURE_GUIDANCE[code]} 請查原紀錄；不要重送或宣稱效果已完成。"


Revision = Annotated[int, Field(ge=0, le=2**31 - 1)]
CallId = Annotated[str, Field(min_length=1, max_length=256)]
RuleText = Annotated[str, Field(min_length=1, max_length=512)]
ETag = Annotated[str, Field(min_length=1, max_length=1024)]
HitlStatus = Literal["pending", "allow", "deny", "expired", "interrupted", "effect_unknown"]
# B1 PA-11: bounded so one ask_user question and its answer stay one inline row.
QuestionText = Annotated[str, Field(min_length=1, max_length=2000)]
ChoiceText = Annotated[str, Field(min_length=1, max_length=200)]
MAX_CHOICES = 10
# Neal 2026-10-10, as Grok Bot: a wait raised in a person's own turn waits for that person; a scheduled or
# Agent-sent turn keeps the short expiry. Older records have no field; background is left out of dumps so their
# stored bytes and digests stay exactly as written.
WaitKind = Literal["interactive", "background"]


def _wait_field():
    return Field(default="background", exclude_if=lambda value: value == "background")


def hitl_settings(environ=None):
    """(enabled, background wait seconds, interactive wait seconds); the interactive bound is cleanup, never shown."""
    environ = os.environ if environ is None else environ
    enabled = environ.get(ENABLED_ENV, "")
    if enabled in {"", "0"}:
        return False, 600, 86400
    if enabled != "1":
        raise ValueError("FA_NATIVE_HITL_ENABLED must be absent, 0 or 1")

    def seconds(name, default):
        value = environ.get(name, default)
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 2**31 - 1:
            raise ValueError(name + " must be a positive integer")
        return int(value)
    return True, seconds(TTL_ENV, "600"), seconds(INTERACTIVE_TTL_ENV, "86400")


def hitl_key(kind, tenant, team, *parts):
    # Keep H1 control_key bytes exactly, without importing the API's Table SDK into Hosted.
    raw = json.dumps(["fa-operation-control-v1", "native-hitl-" + kind, tenant, team, *parts],
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def pending_id(tenant, team, call):
    return hitl_key("pending", tenant, team, material_digest(call))


def question_id(tenant, team, call):
    # Its own key namespace: an ask_user wait never shares a row with a real tool call's approval.
    return hitl_key("question", tenant, team, material_digest(call))


class NativeHitlTurn(WireModel):
    agent_id: Identifier
    conversation_id: Identifier
    request_id: Identifier


class NativeHitlControlReceipt(WireModel):
    result: dict
    control_request_id: Identifier
    control_request_digest: Digest


class NativeHitlRuleInput(WireModel):
    text: RuleText
    behavior: Literal["allow_automatically", "ask_first"]

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("a rule needs its confirmed natural-language scope")
        return value


class NativeHitlRule(NativeHitlRuleInput):
    rule_id: Identifier


class NativeHitlRules(WireModel):
    schema_version: Literal["fa-native-hitl-rules-v1"] = "fa-native-hitl-rules-v1"
    team_id: Identifier
    revision: Revision
    etag: ETag | None
    rules: Annotated[tuple[NativeHitlRule, ...], Field(max_length=MAX_RULES)] = ()


class _RulesVersion(WireModel):
    expected_rules_revision: Revision
    expected_rules_etag: ETag | None

    @model_validator(mode="after")
    def exact_version(self):
        if (self.expected_rules_revision == 0) != (self.expected_rules_etag is None):
            raise ValueError("only the initial absent rule set has no ETag")
        if self.expected_rules_etag is not None and (
                not self.expected_rules_etag.strip() or self.expected_rules_etag == "*"
                or any(char in self.expected_rules_etag for char in "\r\n")):
            raise ValueError("use the original exact rules ETag")
        return self


class NativeHitlRuleChange(Command, _RulesVersion):
    kind: Literal["rules_change"] = "rules_change"
    operation: Literal["add", "update", "delete"]
    rule_id: Identifier | None = None
    rule: NativeHitlRuleInput | None = None

    @model_validator(mode="after")
    def exact_change(self):
        if (self.operation == "add") != (self.rule_id is None):
            raise ValueError("only add allocates a new stable rule ID")
        if (self.operation == "delete") != (self.rule is None):
            raise ValueError("add/update needs the complete confirmed rule; delete keeps no replacement")
        return self


class NativeHitlCall(WireModel):
    agent_id: Identifier
    conversation_id: Identifier
    request_id: Identifier
    sdk_session_id: CallId
    tool_call_id: CallId


class NativeHitlAction(WireModel):
    call: NativeHitlCall
    category: Literal["shell", "mcp", "desktop", "automation_write", "delegation", "agent_call", "routine"]
    tool: CallId
    target: Annotated[str, Field(min_length=1, max_length=2048)]
    arguments: dict[str, JsonValue]

    @property
    def sha256(self) -> str:
        return material_digest(self)


def native_hitl_effect_material(receipt, action):
    return {"kind": "native-hitl-action-v1", "submit_digest": receipt.submit_digest,
            "capture_digest": receipt.capture_digest, "action": action.model_dump(mode="json")}


class NativeHitlReview(WireModel):
    """H2's independent review result, never a human identity or an execution grant.

    ``source="toolbox"`` is not a review: a Foundry toolbox tool's own ``require_approval`` (``always`` asks for
    every call, ``never`` allows) decides, as the official hosted-agent contract requires; no Team rule applies.
    """

    rules_revision: Revision
    matched_rule_ids: Annotated[tuple[Identifier, ...], Field(max_length=MAX_RULES)] = ()
    source: Literal["rules", "default", "unavailable", "uncertain", "toolbox"]
    outcome: Literal["allow", "ask", "deny"]
    reason: Literal[
        "reviewed", "ask_rule", "review_unavailable", "review_invalid", "review_uncertain",
        "human_request_unavailable", "human_takeover", "toolbox_always", "toolbox_never",
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    takeover_required: bool = Field(default=False, exclude_if=lambda value: not value)
    reviewed_at: datetime | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def exact_source(self):
        if len(set(self.matched_rule_ids)) != len(self.matched_rule_ids):
            raise ValueError("matched rule IDs must be unique")
        if (self.source == "rules") != bool(self.matched_rule_ids):
            raise ValueError("retain matched rules or the explicit default/fallback source")
        if self.source in {"unavailable", "uncertain"} and self.outcome != "ask":
            raise ValueError("an unavailable or uncertain review must ask")
        if self.takeover_required and self.outcome != "deny":
            raise ValueError("human takeover cannot be replaced with an approval")
        toolbox = {"toolbox_always": "ask", "toolbox_never": "allow"}
        if (self.source == "toolbox") != (self.reason in toolbox) or (
                self.source == "toolbox" and (self.outcome != toolbox[self.reason] or self.takeover_required)):
            raise ValueError("a toolbox setting only asks for always or allows for never")
        return self


class NativeHitlPendingCreate(Command):
    kind: Literal["pending_create"] = "pending_create"
    action: NativeHitlAction
    action_digest: Digest
    review: NativeHitlReview
    wait: WaitKind = _wait_field()

    @model_validator(mode="after")
    def exact_action(self):
        if self.action_digest != self.action.sha256:
            raise ValueError("the digest must cover the complete original action and call")
        if self.review.outcome != "ask":
            raise ValueError("only an ask result creates a pending human approval")
        return self


class NativeHitlDecision(Command, _RulesVersion):
    kind: Literal["decision"] = "decision"
    pending_id: Digest
    pending_digest: Digest
    choice: Literal["allow_once", "allow_rule", "deny"]
    rule: NativeHitlRuleInput | None = None

    @model_validator(mode="after")
    def confirmed_rule(self):
        if (self.choice == "allow_rule") != (self.rule is not None):
            raise ValueError("only allow_rule includes the exact human-confirmed rule text")
        if self.rule is not None and self.rule.behavior != "allow_automatically":
            raise ValueError("allow_rule can only add the confirmed automatic-allow rule")
        return self


class NativeHitlClose(Command):
    kind: Literal["close"] = "close"
    pending_id: Digest
    pending_digest: Digest
    status: Literal["expired", "interrupted", "effect_unknown"]


class NativeHitlPending(WireModel):
    kind: Literal["native_hitl_pending"] = "native_hitl_pending"
    tenant_id: Identifier
    team_id: Identifier
    pending_id: Digest
    agent_oid: Identifier
    command: NativeHitlPendingCreate
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def window(self):
        for value in (self.created_at, self.expires_at):
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError("pending timestamps must be explicit UTC")
        if self.expires_at <= self.created_at:
            raise ValueError("the pending call needs a positive wait window")
        return self

    @property
    def sha256(self) -> str:
        return material_digest(self)


class NativeHitlApprovalReference(WireModel):
    pending_id: Digest
    pending_digest: Digest
    call: NativeHitlCall


class NativeAttentionItem(WireModel):
    """One read-only projection; question text/history cannot be parsed as approval."""

    schema_version: Literal["fa-native-attention-v1"] = "fa-native-attention-v1"
    type: Literal["approval_required"] = "approval_required"
    item_id: Digest
    approval: NativeHitlApprovalReference
    created_at: datetime
    expires_at: datetime
    purpose: Literal["attention-only-not-approval"] = "attention-only-not-approval"

    @model_validator(mode="after")
    def stable_reference(self):
        if self.item_id != self.approval.pending_id:
            raise ValueError("an approval item retains the original pending ID")
        return self


class NativeHitlOnceGrant(WireModel):
    """The recorded choice only. H2 must check the live original call and consume once."""

    kind: Literal["native_hitl_once_grant"] = "native_hitl_once_grant"
    tenant_id: Identifier
    team_id: Identifier
    grant_id: Digest
    approval: NativeHitlApprovalReference
    action_digest: Digest
    decision_id: Digest
    rules_revision: Revision
    expires_at: datetime
    uses: Literal[1] = 1
    purpose: Literal["recorded-choice-not-execution-permission"] = "recorded-choice-not-execution-permission"


class NativeHitlAuthorize(Command):
    kind: Literal["authorize"] = "authorize"
    action: NativeHitlAction
    action_digest: Digest
    review: NativeHitlReview
    expected_rules_etag: ETag | None
    effect_token: Identifier
    approval: NativeHitlApprovalReference | None = None

    @model_validator(mode="after")
    def exact_action(self):
        if self.action_digest != self.action.sha256:
            raise ValueError("authorization must name the complete original action")
        if self.approval is not None and self.approval.call != self.action.call:
            raise ValueError("human approval must name this exact original call")
        return self


class NativeHitlAuthorization(WireModel):
    authorization_id: Digest
    call: NativeHitlCall
    action_digest: Digest
    review: NativeHitlReview
    effect_token: Identifier
    approval: NativeHitlApprovalReference | None
    grant_id: Digest | None
    expires_at: datetime
    purpose: Literal["original-live-claim-only"] = "original-live-claim-only"


class NativeHitlEnd(Command):
    kind: Literal["end"] = "end"
    turn: NativeHitlTurn


class NativeHitlPendingQuery(WireModel):
    turn: NativeHitlTurn
    after: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(ge=1, le=20)] = 20


class NativeHitlPendingPage(WireModel):
    schema_version: Literal["fa-native-hitl-pending-page-v1"] = "fa-native-hitl-pending-page-v1"
    turn: NativeHitlTurn
    references: tuple[NativeHitlApprovalReference, ...]
    attention: tuple[NativeAttentionItem, ...]
    next_after: Annotated[int, Field(ge=0)] | None
    closed: bool
    purpose: Literal["read-only-turn-projection"] = "read-only-turn-projection"


class NativeHitlEffectState(WireModel):
    action: NativeHitlAction
    reviews: tuple[NativeHitlReview, ...] = ()
    pending: NativeHitlApprovalReference | None = None
    authorization: NativeHitlAuthorization | None = None
    phase: Literal["reviewing", "waiting", "ready", "dispatched", "stopped"] = "reviewing"


class NativeHitlReviewEntry(WireModel):
    tool_call_id: CallId
    action: NativeHitlAction | None
    action_digest: Digest | None
    reviews: tuple[NativeHitlReview, ...] = ()
    status: Literal["recorded", "not_recorded"]
    time_status: Literal["recorded", "unknown"]

    @model_validator(mode="after")
    def original_material(self):
        if self.action is None:
            if self.action_digest is not None or self.reviews:
                raise ValueError("unrecorded action cannot claim a digest or review")
        elif self.action.sha256 != self.action_digest or self.action.call.tool_call_id != self.tool_call_id:
            raise ValueError("review history must retain the complete original action")
        if self.status != ("recorded" if self.reviews else "not_recorded"):
            raise ValueError("missing reviews must remain explicitly unrecorded")
        timed = bool(self.reviews) and all(review.reviewed_at is not None for review in self.reviews)
        if self.time_status != ("recorded" if timed else "unknown"):
            raise ValueError("missing review times must remain unknown")
        return self


class NativeHitlReviewHistory(NativeHitlTurn):
    schema_version: Literal["fa-native-hitl-review-history-v1"] = "fa-native-hitl-review-history-v1"
    entries: Annotated[tuple[NativeHitlReviewEntry, ...], Field(max_length=MAX_HITL_REVIEW_PAGE)]
    next_after: StateStoreCursor | None
    purpose: Literal["readback-only-not-execution-permission"] = "readback-only-not-execution-permission"

    @model_validator(mode="after")
    def original_turn(self):
        if len({entry.tool_call_id for entry in self.entries}) != len(self.entries):
            raise ValueError("a review page cannot repeat an original tool call")
        for entry in self.entries:
            if entry.action is not None:
                call = entry.action.call
                if (call.agent_id, call.conversation_id, call.request_id) != (
                        self.agent_id, self.conversation_id, self.request_id):
                    raise ValueError("review history belongs to one original Agent and turn")
        return self


class NativeHitlReadback(WireModel):
    schema_version: Literal["fa-native-hitl-readback-v1"] = "fa-native-hitl-readback-v1"
    pending: NativeHitlPending
    approval: NativeHitlApprovalReference
    status: HitlStatus
    decision: NativeHitlDecision | None
    grant: NativeHitlOnceGrant | None
    attention: NativeAttentionItem | None
    observed_at: datetime
    expired: bool
    current_rules_revision: Revision
    authorization: NativeHitlAuthorization | None = Field(default=None, exclude_if=lambda value: value is None)
    turn_closed: bool = Field(default=False, exclude_if=lambda value: not value)
    purpose: Literal["readback-only-not-execution-permission"] = "readback-only-not-execution-permission"


class NativeHitlQuestion(WireModel):
    """B1 PA-11: one SDK ask_user request in the original live call. A question, never an action or approval.

    ``call.tool_call_id`` is the waiting runtime's own local ID (fresh per runtime and per question), not an SDK
    tool call, so a restarted turn can never reopen an old wait or reuse its answer.
    """

    call: NativeHitlCall
    question: QuestionText
    choices: Annotated[tuple[ChoiceText, ...], Field(max_length=MAX_CHOICES)] = ()
    allow_freeform: bool = True

    @property
    def sha256(self) -> str:
        return material_digest(self)


class NativeHitlQuestionCreate(Command):
    kind: Literal["question_create"] = "question_create"
    question: NativeHitlQuestion
    question_digest: Digest
    wait: WaitKind = _wait_field()

    @model_validator(mode="after")
    def exact_question(self):
        if self.question_digest != self.question.sha256:
            raise ValueError("the digest must cover the complete original question and call")
        return self


class NativeHitlQuestionPending(WireModel):
    kind: Literal["native_hitl_question"] = "native_hitl_question"
    tenant_id: Identifier
    team_id: Identifier
    question_id: Digest
    agent_oid: Identifier
    command: NativeHitlQuestionCreate
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def window(self):
        for value in (self.created_at, self.expires_at):
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError("question timestamps must be explicit UTC")
        if self.expires_at <= self.created_at:
            raise ValueError("the question needs a positive wait window")
        return self

    @property
    def sha256(self) -> str:
        return material_digest(self)


class NativeHitlQuestionReference(WireModel):
    question_id: Digest
    question_digest: Digest
    call: NativeHitlCall


class NativeHitlAnswer(Command):
    """The Team owner's typed reply to one original question; never an approval, grant or rule."""

    kind: Literal["answer"] = "answer"
    question_id: Digest
    question_digest: Digest
    answer: QuestionText
    was_freeform: bool


class NativeQuestionRequiredItem(WireModel):
    """B1 PA-11: an open ask_user question. Read-only attention: answering it approves nothing."""

    schema_version: Literal["fa-native-attention-v1"] = "fa-native-attention-v1"
    type: Literal["question_required"] = "question_required"
    item_id: Digest
    question: NativeHitlQuestionReference
    text: QuestionText
    choices: Annotated[tuple[ChoiceText, ...], Field(max_length=MAX_CHOICES)]
    allow_freeform: bool
    created_at: datetime
    expires_at: datetime
    purpose: Literal["attention-only-not-approval"] = "attention-only-not-approval"
    wait: WaitKind = _wait_field()

    @model_validator(mode="after")
    def stable_reference(self):
        if self.item_id != self.question.question_id:
            raise ValueError("a question item retains the original question ID")
        return self


class NativeHitlQuestionReadback(WireModel):
    schema_version: Literal["fa-native-hitl-question-readback-v1"] = "fa-native-hitl-question-readback-v1"
    question: NativeHitlQuestionPending
    reference: NativeHitlQuestionReference
    status: Literal["pending", "answered"]
    answer: NativeHitlAnswer | None
    attention: NativeQuestionRequiredItem | None
    observed_at: datetime
    expired: bool
    turn_closed: bool
    purpose: Literal["readback-only-not-execution-permission"] = "readback-only-not-execution-permission"


class NativeHitlQuestionPage(WireModel):
    schema_version: Literal["fa-native-hitl-question-page-v1"] = "fa-native-hitl-question-page-v1"
    turn: NativeHitlTurn
    references: tuple[NativeHitlQuestionReference, ...]
    attention: tuple[NativeQuestionRequiredItem, ...]
    next_after: Annotated[int, Field(ge=0)] | None
    closed: bool
    purpose: Literal["read-only-turn-projection"] = "read-only-turn-projection"


def consent_id(tenant, team, call):
    return hitl_key("consent", tenant, team, material_digest(call))


MAX_CONSENT_URLS = 4
# A real consent link is ~2,300 characters (P0a); the bound keeps four links and the record within one inline row.
ConsentUrl = Annotated[str, Field(min_length=9, max_length=4096, pattern=r"^https://\S+$")]


class ConnectorConsent(WireModel):
    """C3: one toolbox connection this Agent lacks, in one original turn. Neither an approval nor a tool result.

    ``consent`` is the toolbox's JSON-RPC -32006 with its own links; ``reconnect`` is a connection the provider
    revoked, which has no link. The owner fixes it outside FA, then resumes the original step once: a ``tools/list``
    lists the toolbox again, a ``tools/call`` sends the same call (``call.tool_call_id``) with the same arguments.
    The links are private to the owner's card: never in model context, progress, logs or attention projections.
    """

    call: NativeHitlCall
    need: Literal["consent", "reconnect"]
    # A registered Team connector; None for another toolbox source (a Team MCP server with OAuth).
    connector_id: ConnectorId | None
    toolbox_name: Annotated[str, Field(pattern=_TOOLBOX_NAME.pattern)]
    server_label: Annotated[str, Field(pattern=_TOOLBOX_NAME.pattern)]
    step: Literal["tools/list", "tools/call"]
    original_call_digest: Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    consent_urls: Annotated[tuple[ConsentUrl, ...], Field(max_length=MAX_CONSENT_URLS)] = ()
    attempt: Literal[1] = 1

    @model_validator(mode="after")
    def one_step(self):
        if (self.step == "tools/call") != (self.original_call_digest is not None):
            raise ValueError("only a tool call keeps its original call digest")
        if self.need == "reconnect" and self.consent_urls:
            raise ValueError("a revoked connection has no consent link")
        return self

    @property
    def sha256(self) -> str:
        return material_digest(self)


class ConnectorConsentCreate(Command):
    kind: Literal["consent_create"] = "consent_create"
    consent: ConnectorConsent
    consent_digest: Digest
    wait: WaitKind = _wait_field()

    @model_validator(mode="after")
    def exact_consent(self):
        if self.consent_digest != self.consent.sha256:
            raise ValueError("the digest must cover the complete original consent and call")
        return self


class ConnectorConsentPending(WireModel):
    kind: Literal["native_hitl_consent"] = "native_hitl_consent"
    tenant_id: Identifier
    team_id: Identifier
    consent_id: Digest
    agent_oid: Identifier
    command: ConnectorConsentCreate
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def window(self):
        for value in (self.created_at, self.expires_at):
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError("consent timestamps must be explicit UTC")
        if self.expires_at <= self.created_at:
            raise ValueError("the consent needs a positive wait window")
        return self

    @property
    def sha256(self) -> str:
        return material_digest(self)


class ConnectorConsentReference(WireModel):
    consent_id: Digest
    consent_digest: Digest
    call: NativeHitlCall


class ConnectorConsentDecision(Command):
    """The Team owner's one click: ``resume`` (「我連好了，繼續」) or ``decline`` (「先不用」). Never an approval."""

    kind: Literal["consent_decision"] = "consent_decision"
    consent_id: Digest
    consent_digest: Digest
    choice: Literal["resume", "decline"]


class NativeConnectRequiredItem(WireModel):
    """An open Connect card for the attention lists; without the links, which only the private readback carries."""

    schema_version: Literal["fa-native-attention-v1"] = "fa-native-attention-v1"
    type: Literal["connect_required"] = "connect_required"
    item_id: Digest
    consent: ConnectorConsentReference
    need: Literal["consent", "reconnect"]
    connector_id: ConnectorId | None
    server_label: Annotated[str, Field(pattern=_TOOLBOX_NAME.pattern)]
    step: Literal["tools/list", "tools/call"]
    created_at: datetime
    expires_at: datetime
    purpose: Literal["attention-only-not-approval"] = "attention-only-not-approval"
    wait: WaitKind = _wait_field()

    @model_validator(mode="after")
    def stable_reference(self):
        if self.item_id != self.consent.consent_id:
            raise ValueError("a Connect item retains the original consent ID")
        return self


class ConnectorConsentReadback(WireModel):
    schema_version: Literal["fa-native-hitl-consent-readback-v1"] = "fa-native-hitl-consent-readback-v1"
    consent: ConnectorConsentPending
    reference: ConnectorConsentReference
    status: Literal["pending", "resume", "decline"]
    decision: ConnectorConsentDecision | None
    attention: NativeConnectRequiredItem | None
    observed_at: datetime
    expired: bool
    turn_closed: bool
    purpose: Literal["readback-only-not-execution-permission"] = "readback-only-not-execution-permission"


class ConnectorConsentPage(WireModel):
    schema_version: Literal["fa-native-hitl-consent-page-v1"] = "fa-native-hitl-consent-page-v1"
    turn: NativeHitlTurn
    references: tuple[ConnectorConsentReference, ...]
    attention: tuple[NativeConnectRequiredItem, ...]
    next_after: Annotated[int, Field(ge=0)] | None
    closed: bool
    purpose: Literal["read-only-turn-projection"] = "read-only-turn-projection"


NativeAttention = Annotated[NativeAttentionItem | NativeQuestionRequiredItem | NativeConnectRequiredItem,
                            Field(discriminator="type")]

AttentionCursor = Annotated[str, Field(min_length=1, max_length=4096, pattern=r"^[A-Za-z0-9_-]+$")]


class NativeHitlAttentionOrigin(WireModel):
    state: Literal["verified", "unavailable"] = "unavailable"
    root: NativeTurnAddress | None = None
    sender: NativeTurnAddress | None = None

    @model_validator(mode="after")
    def known_origin(self):
        if (self.state == "verified") != (self.root is not None):
            raise ValueError("a verified origin needs its original root address")
        if self.state == "unavailable" and self.sender is not None:
            raise ValueError("an unverified origin cannot name a sender")
        return self


class NativeHitlTeamAttentionEntry(WireModel):
    item: NativeAttention
    origin: NativeHitlAttentionOrigin


class NativeHitlTeamAttentionPage(WireModel):
    schema_version: Literal["fa-native-hitl-team-attention-v1"] = "fa-native-hitl-team-attention-v1"
    team_id: Identifier
    items: Annotated[tuple[NativeHitlTeamAttentionEntry, ...], Field(max_length=20)]
    next_cursor: AttentionCursor | None
    observed_at: datetime


NativeHitlCommand = Annotated[
    NativeHitlRuleChange | NativeHitlPendingCreate | NativeHitlDecision | NativeHitlClose
    | NativeHitlAuthorize | NativeHitlEnd | NativeHitlQuestionCreate | NativeHitlAnswer
    | ConnectorConsentCreate | ConnectorConsentDecision,
    Field(discriminator="kind"),
]
