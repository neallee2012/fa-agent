"""Images a human attaches to one original instruction (B1 PA-08); wire DTOs only, no storage or SDK imports.

The browser converts png/jpg/webp to one PNG within the screenshot bound, so every model, gateway and tool path
keeps its PNG-only rule. Bytes travel once in ``NativeUserImageUpload`` and stay in their own content-addressed
item (a State store item holds at most 1 MB); the instruction keeps only name, digest and length, which its
submit digest covers.
"""

import base64
import hashlib
import struct
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .assistant_image_wire import MAX_IMAGE_BYTES, MAX_TURN_IMAGES, png_bytes
from .m3_models import Filename
from .behavior_wire import Digest, Identifier, WireModel

USER_IMAGE_ERRORS = frozenset({
    "NATIVE_IMAGE_INVALID", "NATIVE_IMAGE_MISSING", "NATIVE_IMAGE_STATE_FULL", "NATIVE_IMAGE_UNSUPPORTED"})
EncodedPng = Annotated[str, Field(min_length=32, max_length=(MAX_IMAGE_BYTES + 2) // 3 * 4)]
# Pinned runtime 1.0.83 re-encodes an image with a side over 2000 px, and the result can exceed the model gateway's
# PNG bound, which would replace it with a placeholder; within this side the exact uploaded bytes reach the model.
MAX_USER_IMAGE_SIDE = 2000


class UserImage(WireModel):
    name: Filename
    sha256: Digest
    byte_length: Annotated[int, Field(ge=24, le=MAX_IMAGE_BYTES)]
    media_type: Literal["image/png"] = "image/png"


UserImages = Annotated[tuple[UserImage, ...], Field(max_length=MAX_TURN_IMAGES)]


def validate_user_images(images):
    if len({image.name for image in images}) != len(images):
        raise ValueError("Attached images need distinct names")
    return images


def canonical_png(png_base64: str) -> bytes:
    """The exact PNG bytes, refusing a non-canonical encoding so one image has one digest."""
    data = png_bytes(base64.b64decode(png_base64, validate=True))
    if base64.b64encode(data).decode("ascii") != png_base64:
        raise ValueError("the PNG differs from its canonical encoding")
    width, height = struct.unpack(">II", data[16:24])
    if not 1 <= width <= MAX_USER_IMAGE_SIDE or not 1 <= height <= MAX_USER_IMAGE_SIDE:
        raise ValueError("an attached image side must be at most 2000 px")
    return data


class NativeUserImageUpload(WireModel):
    """Store one PNG for this Agent; content-addressed, so repeating it is the same item."""

    operation: Literal["user-image-upload"] = "user-image-upload"
    png_base64: EncodedPng

    @model_validator(mode="after")
    def exact_png(self):
        canonical_png(self.png_base64)
        return self


class NativeUserImageStored(WireModel):
    schema_version: Literal["fa-native-user-image-v1"] = "fa-native-user-image-v1"
    agent_id: Identifier
    sha256: Digest
    byte_length: Annotated[int, Field(ge=24, le=MAX_IMAGE_BYTES)]
    media_type: Literal["image/png"] = "image/png"


def stored(agent_id: str, png_base64: str) -> NativeUserImageStored:
    data = canonical_png(png_base64)
    return NativeUserImageStored(agent_id=agent_id, sha256=hashlib.sha256(data).hexdigest(), byte_length=len(data))
