"""Shared candidate tool DTOs; no storage, authority or SDK dependencies."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_ingress import checked_human_text
from .behavior_wire import Digest, Identifier, WireModel
from .memory_source import ProposalSource

CandidateReason = Literal[
    "stable_preference", "durable_fact", "no_durable_information",
    "single_turn_instruction", "sensitive", "approval_or_receipt",
]
MEMORY_CANDIDATE_GUIDANCE = {
    "MEMORY_CANDIDATE_DISABLED": "This turn no longer has the original candidate capability.",
    "MEMORY_CANDIDATE_SOURCE_INVALID": "No exact unique quote from this original user message was confirmed.",
    "MEMORY_CANDIDATE_CONFLICT": "This turn already has its original decision; do not replace it.",
    "MEMORY_CANDIDATE_INVALID": "The original candidate could not be verified; do not use substituted content.",
    "BEHAVIOR_COMMIT_UNKNOWN": "The candidate outcome is unknown; only read its original receipt.",
    "BEHAVIOR_UNSAFE_CONTENT": "Do not retain secrets or unsafe control text in a candidate.",
}
MEMORY_CANDIDATE_ERRORS = frozenset(MEMORY_CANDIDATE_GUIDANCE)


class CandidateArguments(WireModel):
    decision: Literal["candidate", "no_candidate"]
    reason: CandidateReason
    quote: Annotated[str, Field(min_length=1, max_length=4096)] | None = None

    @model_validator(mode="after")
    def exact_or_explicitly_empty(self):
        suggested = self.decision == "candidate"
        if (suggested != (self.quote is not None)
                or suggested != (self.reason in {"stable_preference", "durable_fact"})):
            raise ValueError("a suggestion needs an exact quote; a skip must not retain text")
        if self.quote is not None:
            checked_human_text(self.quote.encode("utf-8"))
            if not self.quote.strip() or len(self.quote.encode("utf-8")) > 4096:
                raise ValueError("the exact quote must contain at most 4096 UTF-8 bytes")
        return self


class CandidateCommand(CandidateArguments):
    request_id: Literal["stage-memory-candidate"]


class CandidateReceipt(WireModel):
    schema_version: Literal["fa-memory-candidate-receipt-v1"] = "fa-memory-candidate-receipt-v1"
    candidate_id: Identifier
    binding_id: Identifier
    decision: Literal["candidate", "no_candidate"]
    state: Literal["staged"] = "staged"
    effective: Literal[False] = False
    control_request_id: Identifier
    control_request_digest: Digest


# ADR-0007 C5: the same one-decision-per-turn suggestion, addressed by the original native Agent/CID/RID.
class NativeCandidateReceipt(WireModel):
    schema_version: Literal["fa-native-memory-candidate-receipt-v1"] = "fa-native-memory-candidate-receipt-v1"
    agent_id: Identifier
    conversation_id: Identifier
    request_id: Identifier
    decision: Literal["candidate", "no_candidate"]
    state: Literal["staged"] = "staged"
    effective: Literal[False] = False


class NativeCandidateView(WireModel):
    schema_version: Literal["fa-native-memory-candidate-view-v1"] = "fa-native-memory-candidate-view-v1"
    agent_id: Identifier
    conversation_id: Identifier
    request_id: Identifier
    turn_number: Annotated[int, Field(ge=1)] | None = None
    state: Literal["not_recorded", "awaiting_saved_turn", "pending_review", "skipped", "ineligible"]
    effective: Literal[False] = False
    reason: CandidateReason | None = None
    text: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    source: ProposalSource | None = None

    @model_validator(mode="after")
    def saved_selection_only(self):
        if self.state == "pending_review":
            if self.text is None or self.source is None:
                raise ValueError("a reviewable suggestion requires its exact source")
            raw = self.text.encode("utf-8")
            if (self.source.speaker != "user" or self.source.conversation_id != self.conversation_id
                    or self.source.original_request_id != self.request_id
                    or self.source.turn_number != self.turn_number
                    or self.source.end_byte - self.source.start_byte != len(raw)
                    or self.source.end_byte > self.source.source_bytes
                    or hashlib.sha256(raw).hexdigest() != self.source.selected_sha256):
                raise ValueError("the suggestion differs from its selected source bytes")
        elif self.text is not None or self.source is not None:
            raise ValueError("an unsaved or skipped suggestion must not expose a quote")
        return self
