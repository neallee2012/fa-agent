"""Shared screenshot-delivery DTOs; no storage, authority or SDK dependencies."""

import base64
import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_wire import Digest, Identifier, WireModel
from .m3_models import MAX_ARTIFACT_BYTES, PNG_SIGNATURE

ENTRY_ID = "fa-image-delivery-v1"
TOOL_NAME = "deliver_image"
MAX_IMAGE_BYTES = MAX_ARTIFACT_BYTES
MAX_TURN_IMAGES = 4
TITLE = "Screenshots delivered with one assistant reply"
IMAGE_DELIVERY_GUIDANCE = {
    "IMAGE_DELIVERY_NO_IMAGE": "No screenshot from this turn is available; take one first, then deliver it.",
    "IMAGE_DELIVERY_INVALID": "Only a PNG screenshot up to 512 KiB can be delivered.",
    "IMAGE_DELIVERY_LIMIT": "This reply already has its maximum of four screenshots.",
    "IMAGE_DELIVERY_DISABLED": "This turn no longer has the screenshot delivery capability.",
    "IMAGE_DELIVERY_CONFLICT": "The screenshot slot for this reply differs; do not claim a delivery.",
    "IMAGE_DELIVERY_UNKNOWN": "The delivery is not confirmed; do not claim it. Delivering the same screenshot "
                              "again is safe and never creates a duplicate.",
}
IMAGE_DELIVERY_ERRORS = frozenset(IMAGE_DELIVERY_GUIDANCE)


def png_bytes(data: bytes) -> bytes:
    """A complete PNG up to the artifact bound; nothing else is delivered as an image."""
    if (not isinstance(data, bytes) or not 24 <= len(data) <= MAX_IMAGE_BYTES
            or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR"):
        raise ValueError("a PNG screenshot within its byte bound is required")
    return data


class DeliverImageArguments(WireModel):
    pass


class DeliverImageCommand(WireModel):
    request_id: Literal["deliver-image"]
    sha256: Digest
    png_base64: Annotated[str, Field(min_length=32, max_length=(MAX_IMAGE_BYTES + 2) // 3 * 4)]

    @model_validator(mode="after")
    def exact_png(self):
        data = png_bytes(base64.b64decode(self.png_base64, validate=True))
        if base64.b64encode(data).decode("ascii") != self.png_base64 or hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError("the PNG bytes differ from their canonical encoding or digest")
        return self

    @property
    def data(self) -> bytes:
        return base64.b64decode(self.png_base64, validate=True)


class ImageEntry(WireModel):
    version_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    number: Annotated[int, Field(ge=1, le=MAX_TURN_IMAGES)]
    sha256: Digest
    byte_length: Annotated[int, Field(ge=1, le=MAX_IMAGE_BYTES)]
    media_type: Literal["image/png"] = "image/png"


class ImageReceipt(ImageEntry):
    schema_version: Literal["fa-image-delivery-receipt-v1"] = "fa-image-delivery-receipt-v1"
    artifact_id: Identifier
    visible_to: Literal["the-original-conversation-human"] = "the-original-conversation-human"


class TurnImages(WireModel):
    schema_version: Literal["fa-turn-images-v1"] = "fa-turn-images-v1"
    binding_id: Identifier
    job_id: Identifier
    entries: Annotated[tuple[ImageEntry, ...], Field(max_length=MAX_TURN_IMAGES)] = ()

    @model_validator(mode="after")
    def ordered(self):
        if [entry.number for entry in self.entries] != list(range(1, len(self.entries) + 1)):
            raise ValueError("delivered screenshots must be listed in their original order")
        return self
