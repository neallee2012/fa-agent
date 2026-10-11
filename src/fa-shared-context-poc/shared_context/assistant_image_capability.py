"""Hosted adapter: deliver this turn's latest screenshot to the original human as an immutable PNG."""

import asyncio
import base64
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from .assistant_image_wire import (
    ENTRY_ID, IMAGE_DELIVERY_GUIDANCE, MAX_IMAGE_BYTES, TOOL_NAME, DeliverImageArguments, DeliverImageCommand,
    ImageReceipt, png_bytes,
)
from .behavior_assistant_deadlines import hosted_sync
from .behavior_capabilities import BUILTIN_CAPABILITIES, CAPABILITY_TOOL_GUIDANCE
from .errors import Problem
from .m2_copilot import ToolFailure, ToolOutput, ToolSpec

CAPABILITY_TOOL_GUIDANCE.update(IMAGE_DELIVERY_GUIDANCE)


def _convert_environment(temp) -> dict[str, str]:
    """Only what ImageMagick needs: an exec'd child is dumpable again, so same-UID commands could read its environ."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(temp),
            "TMPDIR": str(temp), "MAGICK_TEMPORARY_PATH": str(temp)}


def _fit(data: bytes, temp) -> bytes:
    """A browser viewport PNG can exceed the artifact bound; halve it with the preinstalled ImageMagick."""
    convert = shutil.which("convert") if os.name == "posix" else None
    for _ in range(3):
        if len(data) <= MAX_IMAGE_BYTES or convert is None:
            break
        # The Hosted rootfs is read-only; ImageMagick needs a private writable temp directory.
        temp.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = subprocess.run([convert, "png:-", "-resize", "50%", "-strip", "png:-"], input=data,
                              capture_output=True, timeout=30, check=True, env=_convert_environment(temp)).stdout
    return png_bytes(data)


def image_delivery_capability(session, grant):
    builtin = BUILTIN_CAPABILITIES[ENTRY_ID]
    if (grant.entry_id != ENTRY_ID or grant.kind != "fa-builtin"
            or grant.entry_sha256 != builtin.entry_sha256 or grant.tools != builtin.tools or grant.vision):
        raise Problem(503, "CAPABILITY_UNAVAILABLE", "the original screenshot delivery capability differs")

    async def handle(packet):
        try:
            DeliverImageArguments.model_validate(packet["arguments"])
        except (KeyError, ValueError, TypeError):
            raise ToolFailure("deliver_image takes no arguments.", public_code="CAPABILITY_ARGUMENTS") from None
        encoded = session.latest_image
        if encoded is None:
            raise ToolFailure(IMAGE_DELIVERY_GUIDANCE["IMAGE_DELIVERY_NO_IMAGE"], public_code="IMAGE_DELIVERY_NO_IMAGE")
        try:
            data = await asyncio.to_thread(_fit, base64.b64decode(encoded, validate=True),
                                           Path(getattr(session, "root", ".")) / "image-tmp")
        except (ValueError, OSError, subprocess.SubprocessError):
            raise ToolFailure(IMAGE_DELIVERY_GUIDANCE["IMAGE_DELIVERY_INVALID"], public_code="IMAGE_DELIVERY_INVALID") from None
        sha = hashlib.sha256(data).hexdigest()
        command = DeliverImageCommand(request_id="deliver-image", sha256=sha,
                                      png_base64=base64.b64encode(data).decode("ascii"))
        async with session._authority_lock:
            try:
                result = await hosted_sync(session.client.deliver_image, command)
            except Problem as error:
                code = error.body["error"]["code"]
                raise ToolFailure(
                    "The screenshot was not confirmed as delivered; do not claim it is attached.",
                    public_code=code if code in IMAGE_DELIVERY_GUIDANCE else "CAPABILITY_UNAVAILABLE",
                ) from None
        if result.sha256 != sha or result.byte_length != len(data) or result.version_id != sha[:32]:
            raise ToolFailure("The delivery receipt did not match this screenshot.", public_code="CAPABILITY_RESULT_INVALID")
        return ToolOutput(result.model_dump_json())

    tool = ToolSpec(
        TOOL_NAME,
        "Attach the latest screenshot taken in this turn (by a browser or desktop tool) to your reply as a PNG. "
        "No arguments and no link are returned; only this conversation's human can open it. Delivering the same "
        "screenshot again does not add a copy. Say it is attached only after this receipt.",
        DeliverImageArguments.model_json_schema(), handle,
    )
    return (tool,), {tool.name: ImageReceipt}
