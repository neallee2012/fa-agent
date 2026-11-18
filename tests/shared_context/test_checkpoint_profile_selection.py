"""Existing human selection captures checkpoint/start presets without changing old work."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from shared_context.behavior_assistant_wire import ModelRequest
from shared_context.behavior_profile_selection import SelectConversationProfile
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.behavior_sdk_state import CheckpointProfile
from shared_context.behavior_work_policy import WorkPolicyControl
from shared_context.source_materials import digest
from assistant_api_helpers import assistant_api
from test_conversation_profile_selection import PATH, configured, select, template
from instruction_flow_helpers import instruction_inventory, instruction_models, sdk_io
from test_m3_auth import signing_key, jwk_client


def reconstruct(control):
    from shared_context.behavior_inbox import PullInbox
    inbox = PullInbox(control.store, control.runtime.dep.inbox.registration)
    inbox.attach(control.runtime.dep.delivery)
    control.runtime.dep = replace(control.runtime.dep, inbox=inbox)
    rebuilt = WorkPolicyControl(control.runtime, control.policy, control.cost_reader)
    rebuilt.base_bindings, rebuilt.base_profiles = control.base_bindings, control.base_profiles
    rebuilt.attach()
    return rebuilt


def selection_history(h, control):
    config = SimpleNamespace(table=SimpleNamespace(scope=h.store._scope), sha256="f" * 64,
        workers=h.workers.contract, delivery=h.delivery.contract,
        identity=SimpleNamespace(human_oid=h.binding.human_oid))


@pytest.mark.parametrize("settings", [
    {"checkpoint_profile": CheckpointProfile(platform="linux-x64")},
    {"sdk_start_profile": "fa-sdk-start-gate-v1"},
])
@pytest.mark.parametrize("identity", ["agent", "read-only"])
def test_nonapprovers_cannot_select_checkpoint_or_start_presets(assistant_api, settings, identity):
    h, control, _ = configured(assistant_api)
    before = h.store._guard()
    response, _ = select(h, value=template(**settings), identity=identity)
    assert response.status_code == 403
    assert h.store._guard() == before and not control.records


def test_start_v2_is_not_enabled_without_its_distinct_memory_profile(assistant_api):
    h, control, _ = configured(assistant_api)
    before = h.store._guard()
    response, _ = select(h, value=template(sdk_start_profile="fa-sdk-start-gate-v2"))
    assert response.status_code == 422
    assert h.store._guard() == before and not control.records


@pytest.mark.parametrize("setting,value", [
    ("checkpoint_profile", {"name": "manual-no-custom-v1", "platform": "linux-arm64", "provider": "openai-completions"}),
    ("checkpoint_profile", {"name": "future-format", "platform": "linux-x64", "provider": "openai-completions"}),
    ("checkpoint_profile", {"name": "manual-no-custom-v1", "platform": "linux-x64", "provider": "unapproved-provider"}),
    ("checkpoint_profile", {"name": "manual-no-custom-v1", "platform": "linux-x64", "provider": "openai-completions", "safe": True}),
    ("sdk_start_profile", "fa-sdk-start-gate-future"),
])
def test_selection_accepts_only_the_fixed_typed_presets(assistant_api, setting, value):
    h, control, _ = configured(assistant_api)
    command = SelectConversationProfile(request_id="unsupported-preset", expected_selection_id=None, template=template())
    packet = command.model_dump(mode="json")
    packet["template"]["settings"][setting] = value
    before = h.store._guard()
    response = h.client.post(PATH + "/select", json=packet, headers=h.headers())
    assert response.status_code == 422
    assert h.store._guard() == before and not control.records
