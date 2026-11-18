"""Model-specific declared limits through real admission and Hosted SDK options."""

import json
import os
import asyncio
from pathlib import Path
from types import MappingProxyType

import pytest

from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_model_capacity import DeclaredModelLimits, ModelCapacitySnapshot
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_models, runtime_path
from instruction_prompt_helpers import configured as configured_prompt
from test_m3_auth import signing_key, jwk_client


def declared_facts(monkeypatch):
    path = Path(os.environ["FA_INSTRUCTION_MODEL_FACTS_FILE"])
    facts = json.loads(path.read_text(encoding="utf-8"))
    facts["gpt-4.1-mini"].update(context_window_tokens=64000, max_output_tokens=2048)
    facts["configured-second-model"].update(context_window_tokens=16000, max_output_tokens=512)
    path.write_text(json.dumps(facts), encoding="utf-8")
    return facts


def configured_capacity(assistant_api, prompt="fa-instruction-v1"):
    assistant_api.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({**profile.model_dump(), "model_capacity_profile": "fa-model-capacity-v1"})
        for name, profile in assistant_api.runtime.profiles.items()
    })
    return configured_prompt(assistant_api, prompt)


def test_independent_input_and_output_maxima_are_not_forced_into_an_invented_sum():
    limits = DeclaredModelLimits(context_window_tokens=1000, max_prompt_tokens=1000, max_output_tokens=500)
    assert limits.sdk_values() == {"max_context_window_tokens": 1000, "max_prompt_tokens": 1000, "max_output_tokens": 500}
    snapshot = ModelCapacitySnapshot.from_declared("gpt-4.1-mini", DeclaredModelLimits(
        context_window_tokens=64000, max_prompt_tokens=60000, max_output_tokens=2048))
    assert snapshot.source_sha256 == "cfc1c638425c53a970f3746eeddaac4033590a164d47e08e541739895d0c24db"


@pytest.mark.parametrize("field,value", [
    ("schema_version", "future"), ("profile", "future"), ("source", "runtime-attested"),
    ("source_sha256", "0" * 64), ("model", "different-model"),
])
def test_capacity_snapshot_rejects_forged_digest_versions_and_sources(field, value):
    snapshot = ModelCapacitySnapshot.from_declared("declared-model", DeclaredModelLimits(context_window_tokens=1000))
    data = snapshot.model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValueError):
        ModelCapacitySnapshot.model_validate_json(json.dumps(data))


def test_capacity_snapshot_requires_explicit_version_and_selected_model_match():
    snapshot = ModelCapacitySnapshot.from_declared("one", DeclaredModelLimits(max_output_tokens=100))
    data = snapshot.model_dump(mode="json")
    data.pop("schema_version")
    with pytest.raises(ValueError):
        ModelCapacitySnapshot.model_validate_json(json.dumps(data))
    from shared_context.behavior_assistant_wire import InstructionTask
    with pytest.raises(ValueError):
        InstructionTask(content="Hello", conversation_id="original", turn_number=1, model="two", model_capacity=snapshot)


def test_server_owned_capacity_profile_is_default_off_and_pinned_in_configuration():
    from shared_context.behavior_server_config import ServerConfiguration
    from test_behavior_assistant_server import assistant_configuration
    raw = assistant_configuration()
    before = ServerConfiguration.model_validate_json(json.dumps(raw))
    raw["assistant"]["profiles"][0]["model_capacity_profile"] = "fa-model-capacity-v1"
    selected = ServerConfiguration.model_validate_json(json.dumps(raw))
    assert before.sha256 != selected.sha256
    assert before.resource_bindings()["assistant_sha256"] != selected.resource_bindings()["assistant_sha256"]
    assert before.identity == selected.identity and before.table == selected.table and before.sources == selected.sources
    assert "model_capacity_profile" not in before.assistant.profiles[0].model_dump(mode="json")
    raw["assistant"]["profiles"][0]["model_capacity_profile"] = "fa-model-capacity-future"
    with pytest.raises(ValueError):
        ServerConfiguration.model_validate_json(json.dumps(raw))


def test_hosted_sdk_entry_refuses_a_capacity_snapshot_for_another_model(tmp_path, monkeypatch):
    from shared_context import m2_copilot
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    snapshot = ModelCapacitySnapshot.from_declared("first", DeclaredModelLimits(max_output_tokens=1024))
    monkeypatch.setattr(m2_copilot, "CopilotClient", lambda **kwargs: pytest.fail("mismatched capacity must not create SDK"))
    with pytest.raises(ValueError, match="different selected deployment"):
        asyncio.run(m2_copilot.run_copilot(
            state_dir=tmp_path / "sdk", working_directory=tmp_path, sdk_session_id="original-sdk", resume=False,
            prompt="Original exact user instruction.", tools=[], api_url="https://context.invalid",
            token_provider=lambda: pytest.fail("mismatched model needs no token"), on_event=lambda _: None,
            model="second", instruction=True, behavior_boundary=AssistantModelBoundary(None, text_reply=True),
            model_capacity=snapshot, runtime_path=tmp_path / "not-started",
        ))
