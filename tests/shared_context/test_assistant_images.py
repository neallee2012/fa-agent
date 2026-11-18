"""Screenshots delivered with a reply: immutable PNG versions read only through the original work's authority."""

import base64
import hashlib
import struct
import zlib

import pytest

from shared_context.behavior_history_prompt import HISTORY_PROFILE
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from assistant_images_helpers import png


def command(data):
    from shared_context.assistant_image_wire import DeliverImageCommand
    return DeliverImageCommand(request_id="deliver-image", sha256=hashlib.sha256(data).hexdigest(),
                               png_base64=base64.b64encode(data).decode("ascii"))


def test_image_capability_adds_only_its_tool_and_guidance():
    from shared_context.behavior_capabilities import CapabilityCapture, available_grants
    from shared_context.behavior_capability_prompt import assemble_capabilities, session_needs
    from shared_context.behavior_skill_prompt import assemble_prompt

    grant = available_grants(None, ("fa-image-delivery-v1",))["fa-image-delivery-v1"]
    assert grant.tools == ("deliver_image",) and grant.vision is False
    capture = CapabilityCapture(head_selection_id="selected-images", grants=(grant,))
    prompt = assemble_capabilities(HISTORY_PROFILE, capture)
    assert prompt.system_message.startswith(assemble_prompt(HISTORY_PROFILE).system_message)
    assert "call deliver_image" in prompt.system_message and "Never paste image data or links" in prompt.system_message
    assert session_needs(capture).custom_tools == ("deliver_image",)


def test_screenshots_the_model_gateway_refuses_are_replaced_not_sent():
    from shared_context.behavior_assistant_gateway import MAX_IMAGE_DATA_URL_CHARS
    from shared_context.m2_copilot import MODEL_IMAGE_URL_CHARS, _omit_unsendable_images

    assert MODEL_IMAGE_URL_CHARS == MAX_IMAGE_DATA_URL_CHARS
    small = "data:image/png;base64," + base64.b64encode(png()).decode()
    large = "data:image/png;base64," + "A" * MODEL_IMAGE_URL_CHARS
    body = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": small}},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ"}},
        {"type": "image_url", "image_url": {"url": large}},
    ]}]}
    assert _omit_unsendable_images(body) == 2
    parts = body["messages"][0]["content"]
    assert parts[0]["image_url"]["url"] == small
    assert [part["type"] for part in parts] == ["image_url", "text", "text"]


def test_only_a_complete_png_within_the_bound_is_a_delivery_command():
    from shared_context.assistant_image_wire import DeliverImageCommand, MAX_IMAGE_BYTES

    data = png()
    assert command(data).data == data
    for bad in (b"GIF89a" + data[6:], data[:8] + b"\x00" * 20):
        with pytest.raises(ValueError):
            command(bad)
    with pytest.raises(ValueError):
        DeliverImageCommand(request_id="deliver-image", sha256="0" * 64, png_base64=base64.b64encode(data).decode())
    with pytest.raises(ValueError):
        command(data + b"\x00" * (MAX_IMAGE_BYTES - len(data) + 1))
