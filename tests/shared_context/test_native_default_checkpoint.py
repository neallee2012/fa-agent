"""B1 TC: auto-no-custom-v1 is the default for new native conversations and new Agents; explicit choices stay."""

import json

import pytest

from shared_context import behavior_sdk_state
from shared_context.behavior_agent_template import (
    AgentTemplate, TemplateSettings, export_template, with_native_default_checkpoint,
)
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_native_capture import resolve_capture
from shared_context.behavior_native_turn_wire import NativeConversation
from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
from shared_context.behavior_sdk_state import (
    AUTO_PROFILE, CheckpointProfile, local_platform, native_default_checkpoint,
)
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401

AUTO = {"name": AUTO_PROFILE, "platform": local_platform(), "provider": "openai-completions"}


def _submit(rid, cid=None):
    return InstructionSubmit(request_id=rid, content="Hello.", conversation_id=cid)


def _select(c, request_id, **settings):
    current = export_template(c.control, c.authority, c.human)
    values = current.model_dump(mode="json")
    values["settings"].update(settings)
    select_profile(c.control, c.authority, c.human, SelectConversationProfile(
        request_id=request_id, expected_selection_id=None,
        template=AgentTemplate.model_validate_json(json.dumps(values))))


def test_the_old_default_profile_is_unchanged():
    # Existing captures, selections and checkpoint references keep their serialized bytes and digests.
    assert CheckpointProfile().model_dump(mode="json") == {
        "name": "manual-no-custom-v1", "platform": "win32-x64", "provider": "openai-completions"}
    assert TemplateSettings().checkpoint_profile is None


def test_the_default_follows_the_provider_and_is_never_set_where_none_is_allowed(monkeypatch):
    assert native_default_checkpoint(None).model_dump(mode="json") == AUTO
    assert native_default_checkpoint("openai-responses").provider == "openai-responses"
    # The resident worker PoC keeps ordinary checkpoints only; an unsupported platform keeps the old behavior.
    assert native_default_checkpoint(None, "fa-resident-worker-v1") is None
    monkeypatch.setattr(behavior_sdk_state, "local_platform", lambda: None)
    assert native_default_checkpoint(None) is None


def test_a_new_native_conversation_without_a_selection_compacts_automatically(synthetic_standing_v1):
    c = synthetic_standing_v1
    assert c.control.policy.profile.checkpoint_profile is None
    first = resolve_capture(c.control, c.authority, c.human, _submit("native-new"))
    assert first.profile_selection_id is None
    assert first.template.settings.checkpoint_profile.model_dump(mode="json") == AUTO
    # The export (template panel, selection source) still shows the Agent's own settings, unchanged.
    assert export_template(c.control, c.authority, c.human).settings.checkpoint_profile is None
    # A follow-up turn keeps the conversation's original capture.
    original = NativeConversation(agent_id=c.native.targets[0].agent_id,
                                  hosted_session_id=c.native.targets[0].session_id, conversation_id="native-cid",
                                  last_turn=1, last_request_id="native-new", capture=first, capture_digest=first.sha256)
    later = resolve_capture(c.control, c.authority, c.human, _submit("native-next", "native-cid"), conversation=original)
    assert later.template == first.template


@pytest.mark.parametrize("chosen", [None, {"name": "manual-no-custom-v1", "platform": "win32-x64",
                                           "provider": "openai-completions"}])
def test_an_existing_selection_is_used_exactly_as_chosen(synthetic_standing_v1, chosen):
    c = synthetic_standing_v1
    _select(c, "chosen-checkpoint", checkpoint_profile=chosen)
    captured = resolve_capture(c.control, c.authority, c.human, _submit("native-selected"))
    assert captured.profile_selection_id is not None
    assert captured.template.settings.checkpoint_profile == (
        None if chosen is None else CheckpointProfile.model_validate(chosen))


def test_only_a_missing_choice_is_filled():
    from test_native_turn_wire import capture
    template = capture().template
    assert template.settings.checkpoint_profile is None
    filled = with_native_default_checkpoint(template)
    # The checkpoint provider follows the template's own provider (this fixture is a Responses template).
    assert template.settings.model_provider == "openai-responses"
    assert filled.settings.checkpoint_profile.model_dump(mode="json") == {**AUTO, "provider": "openai-responses"}
    assert filled.model_copy(update={"settings": filled.settings.model_copy(
        update={"checkpoint_profile": None})}) == template
    manual = template.model_copy(update={"settings": template.settings.model_copy(
        update={"checkpoint_profile": CheckpointProfile(platform=local_platform())})})
    assert with_native_default_checkpoint(manual) == manual
    worker = template.model_copy(update={"settings": template.settings.model_copy(
        update={"worker_profile": "fa-resident-worker-v1"})})
    assert with_native_default_checkpoint(worker) == worker
