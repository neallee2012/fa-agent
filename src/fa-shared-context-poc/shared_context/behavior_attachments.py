"""Exact retained text references, not filesystem paths or new access grants."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_wire import ExactSourceRef, WireModel
from .m3_models import Filename

TextAttachmentProfile = Literal["fa-source-text-attachments-v1"]
MAX_ATTACHED_TEXT_BYTES = 32768
ATTACHMENT_RESPONSE_BYTES = 98304
ATTACHMENT_ERRORS = frozenset({
    "ATTACHMENTS_DISABLED", "ATTACHMENT_SOURCE_DENIED", "ATTACHMENT_SOURCE_CHANGED",
    "ATTACHMENT_FORMAT", "ATTACHMENT_HISTORY_INVALID", "ATTACHMENT_SOURCE_LIMIT",
    "SOURCE_WITHDRAWN", "SOURCE_EXPIRED", "SOURCE_ACCESS_CHANGED", "SOURCE_REFERENCE_MISMATCH",
    "SOURCE_REQUIRED_MISSING", "SOURCE_INTEGRITY_ERROR", "SOURCE_RECEIPT_MISMATCH",
    "SOURCE_SCOPE_DENIED", "SOURCE_GRAPH_LIMIT", "BEHAVIOR_SOURCE_DENIED",
})


class TextAttachment(WireModel):
    name: Filename
    source: ExactSourceRef


TextAttachments = Annotated[tuple[TextAttachment, ...], Field(max_length=4)]


def validate_attachments(attachments: tuple[TextAttachment, ...]) -> tuple[TextAttachment, ...]:
    if (len({item.name for item in attachments}) != len(attachments)
            or sum(item.source.byte_length for item in attachments) > MAX_ATTACHED_TEXT_BYTES
            or any(item.source.byte_length == 0 for item in attachments)):
        raise ValueError("Attachments require distinct names and bounded complete nonempty sources")
    return attachments


class AttachedText(WireModel):
    attachment: TextAttachment
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    text: str

    @model_validator(mode="after")
    def exact(self):
        raw = self.text.encode("utf-8")
        if (len(raw) != self.attachment.source.byte_length
                or hashlib.sha256(raw).hexdigest() != self.attachment.source.sha256):
            raise ValueError("Attachment text differs from its complete original reference")
        return self


class AttachedTexts(WireModel):
    schema_version: Literal["fa-source-text-attachments-read-v1"] = "fa-source-text-attachments-read-v1"
    entries: Annotated[tuple[AttachedText, ...], Field(max_length=4)] = ()

    @model_validator(mode="after")
    def bounded(self):
        validate_attachments(tuple(item.attachment for item in self.entries))
        if len(self.model_dump_json().encode("utf-8")) > ATTACHMENT_RESPONSE_BYTES:
            raise ValueError("Complete attachment response exceeds the transport bound")
        return self
