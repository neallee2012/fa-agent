"""Exact existing source attachments through admission, signed reads and SDK I/O."""

from datetime import timedelta
from types import MappingProxyType

import pytest

from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_attachments import TextAttachment
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_wire import Command, ExactSourceRef, SourcePrepare
from shared_context.source_materials import SourceWithdrawalInput
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from work_policy_lifecycle_helpers import enabled
from instruction_attachments_helpers import configured_attachments, add_source

def post(h, command, route="submit"):
    return h.client.post("/work-policy/v1/" + route, content=command.model_dump_json(), headers=h.headers())


def test_duplicate_aliases_are_refused_without_guessing_which_source(assistant_api):
    _, _, _, attachment = configured_attachments(assistant_api)
    with pytest.raises(ValueError):
        InstructionSubmit(request_id="duplicate-aliases", content="Read sources.", attachments=(attachment, attachment))


def test_template_exports_only_the_attachment_profile_not_source_content(assistant_api):
    from shared_context.behavior_agent_template import export_template, inspect_template
    h, control, human, _ = configured_attachments(assistant_api)
    before = h.store._guard()
    template = export_template(control, h.assistant, human)
    assert template.settings.text_attachment_profile == "fa-source-text-attachments-v1"
    assert "ATTACHED_SOURCE_CANARY" not in template.model_dump_json()
    assert not template.authorization and not inspect_template(template, template).activated
    assert h.store._guard() == before and not control.records


@pytest.mark.parametrize("name", ["../source", "/source.txt", "C:\\source.txt", "https://example.invalid/source"])
def test_attachment_names_cannot_be_file_paths_or_urls(name):
    with pytest.raises(ValueError):
        TextAttachment(name=name, source=ExactSourceRef(source_id="allowed", version=1, sha256="a" * 64, byte_length=1))
