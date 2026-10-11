"""Native screenshot scope and read DTOs; no Job, storage client or SDK imports."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .assistant_image_wire import MAX_IMAGE_BYTES, MAX_TURN_IMAGES, DeliverImageCommand, ImageEntry
from .behavior_native_capability_wire import NativeTurnScope
from .behavior_wire import Digest, Identifier, WireModel

ImageVersion = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
EncodedImage = Annotated[str, Field(min_length=32, max_length=(MAX_IMAGE_BYTES + 2) // 3 * 4)]


class NativeImagesRead(NativeTurnScope):
    operation: Literal["images"] = "images"


class NativeImageRead(NativeTurnScope):
    operation: Literal["image"] = "image"
    version_id: ImageVersion


class NativeImageScope(WireModel):
    agent_id: Identifier
    conversation_id: Identifier
    request_id: Identifier
    submit_sha256: Digest


class NativeImageReceipt(NativeImageScope, ImageEntry):
    schema_version: Literal["fa-native-image-receipt-v1"] = "fa-native-image-receipt-v1"
    visible_to: Literal["the-original-conversation-human"] = "the-original-conversation-human"

    @model_validator(mode="after")
    def exact_version(self):
        if self.version_id != self.sha256[:32]:
            raise ValueError("the screenshot version must name its original bytes")
        return self


class NativeTurnImages(NativeImageScope):
    schema_version: Literal["fa-native-turn-images-v1"] = "fa-native-turn-images-v1"
    entries: Annotated[tuple[ImageEntry, ...], Field(max_length=MAX_TURN_IMAGES)] = ()

    @model_validator(mode="after")
    def ordered(self):
        if ([entry.number for entry in self.entries] != list(range(1, len(self.entries) + 1))
                or len({entry.version_id for entry in self.entries}) != len(self.entries)
                or any(entry.version_id != entry.sha256[:32] for entry in self.entries)):
            raise ValueError("screenshots must keep their unique original versions and order")
        return self


class NativeImageContent(NativeImageScope):
    schema_version: Literal["fa-native-image-content-v1"] = "fa-native-image-content-v1"
    entry: ImageEntry
    png_base64: EncodedImage

    @property
    def data(self):
        return DeliverImageCommand(
            request_id="deliver-image", sha256=self.entry.sha256, png_base64=self.png_base64).data

    @model_validator(mode="after")
    def complete_png(self):
        if len(self.data) != self.entry.byte_length or self.entry.version_id != self.entry.sha256[:32]:
            raise ValueError("the screenshot bytes differ from their original version")
        return self
