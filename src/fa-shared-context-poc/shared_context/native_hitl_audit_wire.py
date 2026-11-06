"""Owner audit readback DTOs (API -> bridge only); Hosted never reads them, so they stay out of the pinned wire."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_wire import Digest, Identifier, WireModel
from .native_hitl_wire import (
    AttentionCursor, NativeHitlAction, NativeHitlApprovalReference, NativeHitlCall, NativeHitlReview,
    NativeHitlRuleInput, Revision,
)

# Each revision reads its snapshot, intent and (allow_rule) card row: a page of 20 stays within consistent_read's 98 keys.
MAX_RULE_HISTORY_PAGE = 20

RuleChangeKind = Literal["add", "update", "delete", "allow_rule"]


class NativeHitlRuleRevision(WireModel):
    """One saved rule revision, read from its create-only snapshot and the intent that wrote it."""

    schema_version: Literal["fa-native-hitl-rule-revision-v1"] = "fa-native-hitl-rule-revision-v1"
    revision: Annotated[int, Field(ge=1, le=2**31 - 1)]
    intent_id: Digest
    changed_at: datetime
    changed_by_oid: Identifier
    change: RuleChangeKind
    rule_id: Identifier
    rule: NativeHitlRuleInput | None
    previous_rule: NativeHitlRuleInput | None
    source_pending_id: Digest | None = None
    source_call: NativeHitlCall | None = None
    purpose: Literal["readback-only-not-execution-permission"] = "readback-only-not-execution-permission"

    @model_validator(mode="after")
    def exact_change(self):
        if ((self.change == "delete") != (self.rule is None)
                or (self.change in {"add", "allow_rule"}) != (self.previous_rule is None)
                or (self.change == "allow_rule") != (self.source_pending_id is not None)
                or (self.source_pending_id is None) != (self.source_call is None)):
            raise ValueError("a rule revision keeps exactly the texts and source card of its change")
        return self


class NativeHitlRuleHistoryPage(WireModel):
    """Newest first. Deliberately not NativeHitlRules: it has no ETag and grants no write base."""

    schema_version: Literal["fa-native-hitl-rule-history-v1"] = "fa-native-hitl-rule-history-v1"
    team_id: Identifier
    head_revision: Revision
    entries: Annotated[tuple[NativeHitlRuleRevision, ...], Field(max_length=MAX_RULE_HISTORY_PAGE)]
    next_before: Annotated[int, Field(ge=1, le=2**31 - 1)] | None
    purpose: Literal["readback-only-not-execution-permission"] = "readback-only-not-execution-permission"

    @model_validator(mode="after")
    def newest_first(self):
        revisions = [entry.revision for entry in self.entries]
        if (revisions != sorted(revisions, reverse=True) or len(set(revisions)) != len(revisions)
                or any(revision > self.head_revision for revision in revisions)
                or self.next_before is not None and (not revisions or self.next_before != revisions[-1]
                                                     or self.next_before == 1)):
            raise ValueError("rule history pages run newest first down to the next older revision")
        return self


AllowedBy = Literal["rule", "default", "toolbox", "human_once"]


def allowed_by(review, approval):
    """How one executed call was allowed: a grouping of the saved review and approval, never a replacement for them."""
    if approval is not None:
        return "human_once"
    return {"toolbox": "toolbox", "rules": "rule"}.get(review.source, "default")


class NativeHitlAllowedEntry(WireModel):
    """One executed managed call, read from its create-only authorization and the Agent intent that claimed it."""

    authorization_id: Digest
    intent_id: Digest
    authorized_at: datetime
    action: NativeHitlAction
    review: NativeHitlReview
    approval: NativeHitlApprovalReference | None
    by: AllowedBy

    @model_validator(mode="after")
    def derived(self):
        if self.by != allowed_by(self.review, self.approval):
            raise ValueError("by only groups the saved review and approval")
        return self


class NativeHitlAllowedPage(WireModel):
    """One physical scan page (or the rest of one); an empty page with next_cursor is not the end."""

    schema_version: Literal["fa-native-hitl-allowed-v1"] = "fa-native-hitl-allowed-v1"
    team_id: Identifier
    since: datetime
    entries: Annotated[tuple[NativeHitlAllowedEntry, ...], Field(max_length=20)]
    next_cursor: AttentionCursor | None
    observed_at: datetime
    purpose: Literal["readback-only-not-execution-permission"] = "readback-only-not-execution-permission"
