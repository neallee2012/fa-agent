"""Exact user-text source ranges shared by memory candidates and note proposals; no storage or SDK dependency."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field

from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem


class ProposalSource(WireModel):
    conversation_id: Identifier
    turn_number: Annotated[int, Field(ge=1)]
    speaker: Literal["user", "assistant"]
    original_request_id: Identifier
    source_sha256: Digest
    source_bytes: Annotated[int, Field(ge=1)]
    start_byte: Annotated[int, Field(ge=0)]
    end_byte: Annotated[int, Field(ge=1)]
    selected_sha256: Digest


def quote_source(*, content, conversation_id, turn_number, request_id, quote) -> ProposalSource:
    """The byte range of ``quote`` in this original user message, which must contain it exactly once."""
    position = content.find(quote)
    if position < 0 or content.find(quote, position + 1) >= 0:
        raise Problem(409, "MEMORY_CANDIDATE_SOURCE_INVALID",
                      "the quote must occur exactly once in this original user message")
    raw = content.encode("utf-8")
    selected = quote.encode("utf-8")
    start = len(content[:position].encode("utf-8"))
    return ProposalSource(
        conversation_id=conversation_id, turn_number=turn_number, speaker="user",
        original_request_id=request_id, source_sha256=hashlib.sha256(raw).hexdigest(),
        source_bytes=len(raw), start_byte=start, end_byte=start + len(selected),
        selected_sha256=hashlib.sha256(selected).hexdigest(),
    )
