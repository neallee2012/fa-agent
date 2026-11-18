"""Signed catalog presentation and Hosted wiring; real BPE, no SDK process/model."""

import json
import os
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from conftest import A
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_model_capacity import DeclaredModelLimits, ModelCapacitySnapshot
from shared_context.behavior_server import ServerTokenizer
from shared_context.behavior_server_config import TokenizerConfiguration
from shared_context.behavior_skill_catalog import DESCRIPTION_PREFIX, LOOKUP, present_catalog
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from direct_submit_helpers import submit_direct
from test_skill_purpose import PROFILE
from skill_purpose_api_helpers import add_purpose_skill
from work_policy_lifecycle_helpers import enabled


NAMES = ("alpha-receipts-0123456789abcdef0123456789abcdef",
         "beta-receipts-fedcba9876543210fedcba9876543210",
         "gamma-receipts-9876543210abcdef9876543210abcdef")


@pytest.fixture
def tokenizer():
    cache = Path(os.environ.get(
        "TIKTOKEN_CACHE_DIR", str(Path(__file__).resolve().parents[2] / ".venv" / "fa-tokenizer-cache"),
    ))
    return ServerTokenizer(TokenizerConfiguration(version="0.11.0", encoding="o200k_base", cache_directory=str(cache)))


def configure(assistant, signing_key, jwk_client, tokenizer, *, budgeted=True, budget=None):
    for name in NAMES:
        add_purpose_skill(assistant, signing_key, jwk_client, name=name,
                          description="Use current approved receipts to explain the actual result. " * 4)
    if budget is None:
        text = DESCRIPTION_PREFIX + json.dumps({
            "entries": [{"name": NAMES[0]}], "omitted_count": 2, "unavailable_count": 0, "lookup": LOOKUP,
        }, ensure_ascii=False, separators=(",", ":"))
        budget = tokenizer.count(text.encode())
    facts_path = Path(os.environ["FA_INSTRUCTION_MODEL_FACTS_FILE"])
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    facts["gpt-4.1-mini"].update(context_window_tokens=64000, max_prompt_tokens=budget * 50, max_output_tokens=1024)
    facts_path.write_text(json.dumps(facts), encoding="utf-8")
    assistant.runtime.profiles = MappingProxyType({
        key: AssistantProfile.model_validate({
            **value.model_dump(), "prompt_profile": PROFILE, "skill_artifacts": NAMES,
            "model_capacity_profile": "fa-model-capacity-v1",
            **({"skill_catalog_budget_profile": "fa-skill-catalog-budget-v1"} if budgeted else {}),
        }) for key, value in assistant.runtime.profiles.items()
    })
    h, control, human, _ = enabled(assistant)
    h.runtime.dep.memory._count = tokenizer.count
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        assistant=SimpleNamespace(authority=h.assistant),
        require_current_native_evidence=lambda: None,
    ))
    return h, control, human, budget


def test_missing_declared_capacity_is_refused_before_admission_or_reservation(
    assistant_api, signing_key, jwk_client, tokenizer,
):
    h, control, human, _ = configure(assistant_api, signing_key, jwk_client, tokenizer)
    model = control.models.models["gpt-4.1-mini"]
    control.models.models[model.id] = model.model_copy(update={
        "max_prompt_tokens": None, "context_window_tokens": None,
    })
    before = h.store._guard()
    with pytest.raises(Problem) as refused:
        submit_direct(h, "budget-unknown-capacity", "Read an approved skill.")
    assert refused.value.status == 503
    assert refused.value.body["error"]["code"] == "SKILL_CATALOG_CAPACITY_UNKNOWN"
    assert h.store._guard() == before and not control.records


def test_known_capacity_that_cannot_fit_lookup_is_refused_before_admission(
    assistant_api, signing_key, jwk_client, tokenizer,
):
    h, control, _, _ = configure(assistant_api, signing_key, jwk_client, tokenizer, budget=1)
    before = h.store._guard()
    with pytest.raises(Problem) as refused:
        submit_direct(h, "budget-too-small-at-entry", "Read an approved skill.")
    assert refused.value.status == 413
    assert refused.value.body["error"]["code"] == "SKILL_CATALOG_BUDGET_TOO_SMALL"
    assert h.store._guard() == before and not control.records


def test_unknown_configured_counter_is_refused_before_admission(
    assistant_api, signing_key, jwk_client, tokenizer,
):
    h, control, _, _ = configure(assistant_api, signing_key, jwk_client, tokenizer)
    h.runtime.dep.memory._count = lambda _: None
    before = h.store._guard()
    with pytest.raises(Problem) as refused:
        submit_direct(h, "budget-counter-unknown-at-entry", "Read an approved skill.")
    assert refused.value.status == 503
    assert refused.value.body["error"]["code"] == "SKILL_CATALOG_TOKEN_COUNT_UNKNOWN"
    assert h.store._guard() == before and not control.records


def test_export_retains_budget_profile_as_a_reference_not_new_authority(
    assistant_api, signing_key, jwk_client, tokenizer,
):
    from shared_context.behavior_agent_template import export_template, inspect_template

    h, control, human, _ = configure(assistant_api, signing_key, jwk_client, tokenizer)
    before = h.store._guard()
    exported = export_template(control, h.assistant, human)
    assert exported.settings.skill_catalog_budget_profile == "fa-skill-catalog-budget-v1"
    assert exported.settings.model_capacity_profile == "fa-model-capacity-v1"
    inspected = inspect_template(exported, exported)
    assert inspected.references_match and not inspected.authorization and not inspected.activated
    assert h.store._guard() == before and control.records == {}


def test_template_thin_client_preserves_the_optional_budget_reference(
    assistant_api, signing_key, jwk_client, tokenizer,
):
    from web_test_console.agent_template import download, inspect, parse_template

    h, control, _, _ = configure(assistant_api, signing_key, jwk_client, tokenizer)
    before = h.store._guard()
    calls = []
    def send(method, path, body, token, *, limit):
        calls.append((method, path, body))
        response = h.client.request(method, path, json=body, headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200, response.text
        assert len(response.content) <= limit
        return response.content, response.headers
    adapter = SimpleNamespace(send=send)
    token = h.headers("read-only")["Authorization"].removeprefix("Bearer ")
    raw = download(adapter, token)
    assert parse_template(raw, code="INVALID", status=400).settings.skill_catalog_budget_profile == "fa-skill-catalog-budget-v1"
    result = inspect(adapter, token, raw)
    assert result.references_match and not result.authorization and not result.activated
    assert calls[1][2]["settings"]["skill_catalog_budget_profile"] == "fa-skill-catalog-budget-v1"
    assert h.store._guard() == before and not control.records


@pytest.mark.parametrize("field,value", [
    ("prompt_profile", "fa-instruction-v1"), ("model_capacity_profile", None),
])
def test_budget_configuration_requires_original_skill_and_capacity_profiles(assistant_api, field, value):
    original = next(iter(assistant_api.runtime.profiles.values())).model_dump()
    with pytest.raises(ValueError):
        AssistantProfile.model_validate({
            **original, "prompt_profile": PROFILE, "model_capacity_profile": "fa-model-capacity-v1",
            "skill_catalog_budget_profile": "fa-skill-catalog-budget-v1", field: value,
        })


def test_pinned_bpe_rendering_is_deterministic_and_counted_on_exact_description(tokenizer):
    from test_skill_catalog_budget import catalog

    source = catalog()
    capacity = ModelCapacitySnapshot.from_declared(
        "gpt-4.1-mini", DeclaredModelLimits(context_window_tokens=7000, max_output_tokens=1000),
    )
    first = present_catalog(source, capacity, source.entries[-1].name, tokenizer.count)
    assert first == present_catalog(source, capacity, source.entries[-1].name, tokenizer.count)
    assert first.budget_tokens == 120
    assert first.description_tokens == tokenizer.count(first.description.encode()) <= 120
