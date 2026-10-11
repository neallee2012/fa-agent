"""ADR-0007 C5: a native human turn's inactive memory suggestion, kept with that turn for the human to review.

- ``stage_memory_candidate`` binds only the exact ``fa-memory-candidate-v1`` grant and only on a human turn
  (``receipt.human_origin``): Agent messages and scheduled inputs are never treated as the user. The quote must occur
  exactly once in that original message (``quote_source``, shared with the old Table lane).
- One decision per original RID: ``create_item("cand:<RID>")`` in the conversation's own ``fa/v1/conv/<CID>`` store.
  The same decision returns its receipt; another one is a conflict; an unconfirmed write is read back by the same key
  and is never replaced or sent under a new ID.
- Review: ``NativeCandidateRead`` through the API's owner-checked native gateway shows the quote only after the
  original RID completed with its saved result. Nothing here shares, activates or approves a suggestion; that stays
  with the human review path, and private memory (C3) is not a substitute for it.
- No job, binding or WorkRegistration: the Agent's own receipt and store are the original material.
- MEM-02 (2026-10-03): a suggestion may name the Team note it corrects with ``supersedes: "note:<ref>"``. Only a ref
  this turn's Team recall showed is accepted and kept as that note's exact ``memory_id``; any other ref saves nothing.
  The field is written only when present, so a record without it keeps its earlier bytes.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Annotated, Literal

from pydantic import Field, model_serializer, model_validator

from .behavior_capabilities import BUILTIN_CAPABILITIES, CAPABILITY_TOOL_GUIDANCE
from .behavior_native_turn_wire import NativeCandidateRead
from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .m2_copilot import ToolFailure, ToolOutput, ToolSpec
from .memory_candidate_wire import (
    CandidateArguments, CandidateCommand, MEMORY_CANDIDATE_GUIDANCE, NativeCandidateReceipt, NativeCandidateView,
)
from .memory_source import ProposalSource, quote_source
from .native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider

ENTRY_ID = "fa-memory-candidate-v1"
TOOL = "stage_memory_candidate"
CAPABILITY_TOOL_GUIDANCE.update(MEMORY_CANDIDATE_GUIDANCE)
# The official memory_id grammar (behavior_private_memory._OFFICIAL_ID).
MemoryId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
log = logging.getLogger(__name__)


def candidate_key(request_id: str) -> str:
    return f"cand:{request_id}"


class NativeCandidateArguments(CandidateArguments):
    """MEM-02: native only, so the shared tool schema and the old Table lane stay unchanged."""

    supersedes: Annotated[str, Field(pattern=r"^note:[A-Za-z0-9_.:-]{1,8}$")] | None = None

    @model_validator(mode="after")
    def only_a_suggestion_corrects(self):
        if self.supersedes is not None and self.decision != "candidate":
            raise ValueError("only a suggestion can correct a Team note")
        return self


class NativeCandidateRecord(WireModel):
    kind: Literal["fa-native-memory-candidate-v1"] = "fa-native-memory-candidate-v1"
    agent_id: Identifier
    conversation_id: Identifier
    request_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]
    content_sha256: Digest
    command: CandidateCommand
    source: ProposalSource | None
    effective: Literal[False] = False
    # MEM-02: the exact Team note (official memory_id) this suggestion corrects, as this turn's recall showed it.
    supersedes: MemoryId | None = None

    @model_serializer(mode="wrap")
    def _without_absent_correction(self, handler):
        # A record without a correction keeps its earlier bytes, so it still reads where the field is unknown.
        data = handler(self)
        if data.get("supersedes") is None:
            data.pop("supersedes", None)
        return data

    @model_validator(mode="after")
    def exact_source(self):
        if (self.source is not None) != (self.command.decision == "candidate"):
            raise ValueError("only a suggestion keeps its exact source")
        if self.supersedes is not None and self.command.decision != "candidate":
            raise ValueError("only a suggestion can correct a Team note")
        if self.source is not None:
            raw = self.command.quote.encode("utf-8")
            if (self.source.speaker != "user" or self.source.conversation_id != self.conversation_id
                    or self.source.turn_number != self.turn_number
                    or self.source.original_request_id != self.request_id
                    or self.source.source_sha256 != self.content_sha256
                    or self.source.end_byte - self.source.start_byte != len(raw)
                    or hashlib.sha256(raw).hexdigest() != self.source.selected_sha256):
                raise ValueError("the suggestion differs from its exact original source range")
        return self


def _record(receipt, command: CandidateCommand, supersedes: str | None = None) -> NativeCandidateRecord:
    if not receipt.human_origin or receipt.turn_number is None:
        raise Problem(403, "MEMORY_CANDIDATE_DISABLED", "only an original human turn may suggest memory")
    content = receipt.command.content
    source = None if command.decision == "no_candidate" else quote_source(
        content=content, conversation_id=receipt.conversation_id, turn_number=receipt.turn_number,
        request_id=receipt.request_id, quote=command.quote)
    return NativeCandidateRecord(
        agent_id=receipt.agent_id, conversation_id=receipt.conversation_id, request_id=receipt.request_id,
        turn_number=receipt.turn_number, content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        command=command, source=source, supersedes=supersedes)


def _verified(found, receipt) -> NativeCandidateRecord:
    from .native_turns import _decode

    try:
        record = _decode(found.value, NativeCandidateRecord)
        if record != _record(receipt, record.command, record.supersedes):
            raise ValueError()
    except (Problem, ValueError, TypeError, KeyError):
        raise Problem(409, "MEMORY_CANDIDATE_INVALID", "the original candidate cannot be verified") from None
    return record


async def stage(runtime, receipt, command: CandidateCommand, supersedes: str | None = None) -> NativeCandidateReceipt:
    from .native_checkpoint import conversation_store_name
    from .native_turns import _UNCERTAIN, _json

    record = _record(receipt, command, supersedes)
    key = candidate_key(record.request_id)
    async with runtime._store(conversation_store_name(record.conversation_id)) as store:
        try:
            await store.create_item(key, _json(record))
        except _UNCERTAIN:
            pass  # a duplicate or a lost acknowledgment: only the same key decides, never a new one
        found = await store.get_item(key)
    if found is None:
        raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "the original candidate receipt is not confirmed; do not resend")
    existing = _verified(found, receipt)
    if existing.command != command or existing.supersedes != supersedes:
        raise Problem(409, "MEMORY_CANDIDATE_CONFLICT", "this turn already has its original suggestion decision")
    return NativeCandidateReceipt(agent_id=existing.agent_id, conversation_id=existing.conversation_id,
                                  request_id=existing.request_id, decision=existing.command.decision)


async def read_candidate(runtime, command: NativeCandidateRead) -> NativeCandidateView:
    from .native_checkpoint import conversation_store_name

    receipt = await runtime.read(command.request_id)
    if receipt.conversation_id != command.conversation_id or receipt.agent_id != runtime.identity.agent_id:
        raise Problem(403, "NATIVE_STATE_SCOPE", "the suggestion belongs to another Agent or conversation")
    base = dict(agent_id=receipt.agent_id, conversation_id=receipt.conversation_id,
                request_id=receipt.request_id, turn_number=receipt.turn_number)
    async with runtime._store(conversation_store_name(receipt.conversation_id)) as store:
        found = await store.get_item(candidate_key(receipt.request_id))
    if found is None:
        return NativeCandidateView(**base, state="not_recorded")
    record = _verified(found, receipt)
    reason = record.command.reason
    if receipt.status in {"accepted", "started"}:
        return NativeCandidateView(**base, state="awaiting_saved_turn", reason=reason)
    if receipt.status != "completed":
        return NativeCandidateView(**base, state="ineligible", reason=reason)
    if record.command.decision == "no_candidate":
        return NativeCandidateView(**base, state="skipped", reason=reason)
    return NativeCandidateView(**base, state="pending_review", reason=reason,
                               text=record.command.quote, source=record.source)


def accepts(grant) -> bool:
    builtin = BUILTIN_CAPABILITIES[ENTRY_ID]
    return (grant.entry_id == ENTRY_ID and grant.kind == "fa-builtin" and grant.entry_sha256 == builtin.entry_sha256
            and grant.tools == builtin.tools and not grant.vision)


async def bind(context, grants) -> NativeCapabilityBinding:
    async def handle(packet):
        try:
            arguments = NativeCandidateArguments.model_validate(packet["arguments"])
            command = CandidateCommand(request_id="stage-memory-candidate",
                                       **arguments.model_dump(exclude={"supersedes"}))
        except (KeyError, ValueError, TypeError, Problem):
            raise ToolFailure("Use one bounded exact quote or an explicit no_candidate decision.",
                              public_code="CAPABILITY_ARGUMENTS") from None
        if context.runtime is None:
            raise ToolFailure("No effective memory was created; this turn cannot keep a suggestion.",
                              public_code="CAPABILITY_UNAVAILABLE")
        supersedes = None
        if arguments.supersedes is not None:
            from .native_team_memory import recalled_note

            supersedes = recalled_note(context.receipt.request_id, arguments.supersedes)
            if supersedes is None:
                log.warning("FA_TEAM_MEMORY_CREATE status=not_sent reason=supersedes_unknown")
                raise ToolFailure("No Team note with that reference was shown this turn; nothing was saved. Tell the "
                                  "user it was not found and do not retry with another reference.",
                                  public_code="CAPABILITY_ARGUMENTS")
        try:
            receipt = await stage(context.runtime, context.receipt, command, supersedes)
        except Problem as error:
            code = error.body["error"]["code"]
            code = "BEHAVIOR_COMMIT_UNKNOWN" if code == "NATIVE_STATE_STORE_UNKNOWN" else code
            raise ToolFailure(
                "No effective memory was created; keep answering and do not resend an unknown candidate.",
                public_code=code if code in MEMORY_CANDIDATE_GUIDANCE else "CAPABILITY_UNAVAILABLE",
            ) from None
        return ToolOutput(receipt.model_dump_json())

    tool = ToolSpec(
        TOOL,
        "Suggest one exact, unique quote from the current user's message, or explicitly no_candidate. "
        "Use reason stable_preference for how the user wants work done from now on, and durable_fact for a lasting "
        "fact about the user's work. When the user changes or corrects how they want work done, that is also a "
        "stable_preference. "
        "Never quote secrets, approvals, greetings or one-turn formatting as lasting knowledge. "
        "When the user corrects something they said earlier, suggest the corrected statement: a correction is "
        "lasting knowledge. "
        "When it corrects a Team note shown this turn, set supersedes to that note's exact note: reference; "
        "never invent a reference. "
        "The suggestion is kept only after the original answer is saved; when Team sharing is on it is then shared "
        "with your teammates as data, otherwise it stays an inactive suggestion. "
        "Do not claim it is already remembered.",
        NativeCandidateArguments.model_json_schema(), handle,
    )
    return NativeCapabilityBinding(tools=(tool,), result_schemas={TOOL: NativeCandidateReceipt})


PROVIDER = NativeCapabilityProvider(accepts=accepts, bind=bind, name="memory_candidate")
