"""Signed catalog presentation and Hosted wiring; real BPE, no SDK process/model."""

import copy
import json
import os
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from conftest import A
from shared_context import m2_copilot
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_model_capacity import DeclaredModelLimits, ModelCapacitySnapshot
from shared_context.behavior_server import ServerTokenizer
from shared_context.behavior_server_config import TokenizerConfiguration
from shared_context.behavior_skill_catalog import DESCRIPTION_PREFIX, LOOKUP, present_catalog
from shared_context.behavior_skill_prompt import SKILL_CAPABILITIES
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from test_approved_skill_api import change_artifact, post_skill
from test_behavior_assistant import assistant
from test_instruction_flow import check_history, instruction_models, runtime_path, sdk_io
from test_instruction_capacity import change_loaded_capacity
from test_instruction_prompt import submit
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn
from test_skill_purpose import BODY, PROFILE
from test_skill_purpose_api import add_purpose_skill
from test_work_policy_lifecycle import enabled


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


def test_real_bpe_budget_omits_metadata_not_authorized_skill_access(
    assistant, signing_key, jwk_client, tokenizer,
):
    h, control, human, budget = configure(assistant, signing_key, jwk_client, tokenizer)
    turn = open_turn(h, control, human, "budget-read")
    try:
        catalog = turn.client.skill_catalog()
        original = catalog.model_dump_json()
        before = h.store._guard()
        artifacts = copy.deepcopy(h.artifacts.repository.data)
        view = turn.client.skill_catalog_presentation(catalog)
        assert view.stage == "omitted" and view.omitted_count > 0
        assert view.description_tokens == tokenizer.count(view.description.encode()) <= budget
        assert view.budget_tokens == budget
        assert "PURPOSE_BODY_CANARY" not in view.description
        shown = {entry["name"] for entry in json.loads(view.description.split("\n", 1)[1])["entries"]}
        hidden = next(name for name in NAMES if name not in shown)
        result = turn.client.read_skill(hidden)
        assert result.text.encode() == BODY and result.skill.name == hidden
        with pytest.raises(Problem) as denied:
            turn.client.read_skill("not-configured")
        assert denied.value.body["error"]["code"] == "SKILL_SOURCE_DENIED"
        assert turn.client.skill_catalog().model_dump_json() == original
        assert h.store._guard() == before and h.artifacts.repository.data == artifacts
        assert not h.endpoint.requests
    finally:
        turn.client.close()


@pytest.mark.parametrize("failure,expected", [
    ("unknown-counter", "SKILL_CATALOG_TOKEN_COUNT_UNKNOWN"),
    ("changed-source", "SKILL_SOURCE_CHANGED"),
    ("changed-capacity", "MODEL_CAPACITY_CHANGED"),
])
def test_presentation_refusals_are_current_typed_and_do_not_mutate_work(
    assistant, signing_key, jwk_client, tokenizer, monkeypatch, failure, expected,
):
    h, control, human, _ = configure(assistant, signing_key, jwk_client, tokenizer)
    turn = open_turn(h, control, human, "budget-denial")
    try:
        catalog = turn.client.skill_catalog()
        if failure == "unknown-counter":
            h.runtime.dep.memory._count = lambda _: None
        elif failure == "changed-source":
            change_artifact(h, lambda data: data.update(effective_version_id=None), NAMES[0])
        else:
            original = control.models.models["gpt-4.1-mini"]
            change_loaded_capacity(control, monkeypatch, max_prompt_tokens=original.max_prompt_tokens + 50)
        guard = h.store._guard()
        with pytest.raises(Problem) as denied:
            turn.client.skill_catalog_presentation(catalog)
        assert denied.value.body["error"]["code"] == expected
        assert h.store._guard() == guard and not h.endpoint.requests
    finally:
        turn.client.close()


def test_source_change_during_presentation_is_refused_at_the_next_reserve(
    assistant, signing_key, jwk_client, tokenizer,
):
    """Presentation is read-only metadata; the change is refused before any model dispatch, at reserve."""
    h, control, human, _ = configure(assistant, signing_key, jwk_client, tokenizer)
    turn = open_turn(h, control, human, "budget-late-change")
    try:
        catalog = turn.client.skill_catalog()
        def changed(data):
            change_artifact(h, lambda document: document.update(effective_version_id=None), NAMES[0])
            return tokenizer.count(data)
        h.runtime.dep.memory._count = changed
        guard = h.store._guard()
        turn.client.skill_catalog_presentation(catalog)
        assert h.store._guard() == guard
        with pytest.raises(Problem) as denied:
            turn.client.reserve_model("a" * 64)
        assert denied.value.body["error"]["upstream_error_code"] == "SKILL_SOURCE_CHANGED"
        assert h.store._guard() == guard and not h.endpoint.requests
    finally:
        turn.client.close()


def test_unconfigured_budget_is_not_enabled_by_calling_the_new_endpoint(
    assistant, signing_key, jwk_client, tokenizer,
):
    h, control, human, _ = configure(assistant, signing_key, jwk_client, tokenizer, budgeted=False)
    turn = open_turn(h, control, human, "budget-not-enabled")
    try:
        assert "skill_catalog_budget_profile" not in turn.assignment.task.model_dump_json()
        guard = h.store._guard()
        response = post_skill(turn, {"request_id": "skill-catalog-presentation"}, action="skill-catalog-presentation")
        assert response.status_code == 403
        assert response.headers["x-behavior-assistant-error-code"] == "SKILL_CATALOG_BUDGET_DISABLED"
        assert turn.client.read_skill(NAMES[0]).text.encode() == BODY
        assert h.store._guard() == guard
    finally:
        turn.client.close()


@pytest.mark.parametrize("field,value", [
    ("catalog_sha256", "0" * 64), ("capacity_sha256", "0" * 64),
    ("input_limit_tokens", 64000), ("unavailable_count", 4), ("omitted_count", 4),
])
def test_hosted_rejects_presentation_for_another_catalog_or_capacity(
    assistant, signing_key, jwk_client, tokenizer, monkeypatch, field, value,
):
    h, control, human, _ = configure(assistant, signing_key, jwk_client, tokenizer)
    turn = open_turn(h, control, human, "budget-presentation-binding")
    try:
        catalog = turn.client.skill_catalog()
        original = turn.client.skill_catalog_presentation(catalog)
        monkeypatch.setattr(turn.client, "_post", lambda *args, **kwargs: original.model_copy(update={field: value}))
        with pytest.raises(Problem) as denied:
            turn.client.skill_catalog_presentation(catalog)
        assert denied.value.body["error"]["code"] == "SKILL_CATALOG_VIEW_CHANGED"
        assert not h.endpoint.requests
    finally:
        turn.client.close()


def test_missing_declared_capacity_is_refused_before_admission_or_reservation(
    assistant, signing_key, jwk_client, tokenizer,
):
    h, control, human, _ = configure(assistant, signing_key, jwk_client, tokenizer)
    model = control.models.models["gpt-4.1-mini"]
    control.models.models[model.id] = model.model_copy(update={
        "max_prompt_tokens": None, "context_window_tokens": None,
    })
    before = h.store._guard()
    response = h.client.post("/work-policy/v1/submit", json={
        "request_id": "budget-unknown-capacity", "content": "Read an approved skill.",
    }, headers=h.headers())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SKILL_CATALOG_CAPACITY_UNKNOWN"
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


def test_known_capacity_that_cannot_fit_lookup_is_refused_before_admission(
    assistant, signing_key, jwk_client, tokenizer,
):
    h, control, _, _ = configure(assistant, signing_key, jwk_client, tokenizer, budget=1)
    before = h.store._guard()
    response = h.client.post("/work-policy/v1/submit", json={
        "request_id": "budget-too-small-at-entry", "content": "Read an approved skill.",
    }, headers=h.headers())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "SKILL_CATALOG_BUDGET_TOO_SMALL"
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


def test_unknown_configured_counter_is_refused_before_admission(
    assistant, signing_key, jwk_client, tokenizer,
):
    h, control, _, _ = configure(assistant, signing_key, jwk_client, tokenizer)
    h.runtime.dep.memory._count = lambda _: None
    before = h.store._guard()
    response = h.client.post("/work-policy/v1/submit", json={
        "request_id": "budget-counter-unknown-at-entry", "content": "Read an approved skill.",
    }, headers=h.headers())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SKILL_CATALOG_TOKEN_COUNT_UNKNOWN"
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


def test_hosted_sends_the_bounded_description_without_hiding_names_in_an_enum(
    assistant, signing_key, jwk_client, tokenizer, monkeypatch, tmp_path,
):
    h, control, _, budget = configure(assistant, signing_key, jwk_client, tokenizer)
    calls = sdk_io(monkeypatch, expected_tools=SKILL_CAPABILITIES)
    factory = m2_copilot.CopilotClient.side_effect
    options = []

    def capture(**connection):
        sdk = factory(**connection)
        original = sdk.create_session.side_effect
        async def opened(*args, **configuration):
            options.append(configuration)
            return await original(*args, **configuration)
        sdk.create_session.side_effect = opened
        sdk.resume_session.side_effect = opened
        return sdk

    m2_copilot.CopilotClient.side_effect = capture
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Budget fixture reply."}, {"content": "Budget follow-up."}])
    accepted, _ = submit(h, "budget-hosted", "Read " + NAMES[-1] + ".")
    profile = control.records[accepted["binding_id"]].profile
    assert profile.instruction.skill_catalog_budget_profile == "fa-skill-catalog-budget-v1"
    assert h.runtime.controller.step(accepted["binding_id"]).state == "succeeded"
    reader = next(tool for tool in options[0]["tools"] if tool.name == "read_skill")
    assert tokenizer.count(reader.description.encode()) <= budget
    assert "enum" not in reader.parameters["properties"]["name"]
    assert "omission does not mean unavailable" in reader.description
    assert calls[0]["prompt"] == "Read " + NAMES[-1] + "."
    first = control.records[accepted["binding_id"]].model_dump_json()
    h.executor.root = tmp_path / "fresh-catalog-sdk-files"
    second, _ = submit(h, "budget-followup", "Read " + NAMES[0] + ".", accepted["conversation_id"])
    assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
    next_reader = next(tool for tool in options[1]["tools"] if tool.name == "read_skill")
    assert tokenizer.count(next_reader.description.encode()) <= budget
    assert calls[1]["resume"] and calls[1]["sdk_session_id"] == calls[0]["sdk_session_id"]
    assert control.records[accepted["binding_id"]].model_dump_json() == first
    check_history(h)


def test_export_retains_budget_profile_as_a_reference_not_new_authority(
    assistant, signing_key, jwk_client, tokenizer,
):
    from shared_context.behavior_agent_template import export_template, inspect_template

    h, control, human, _ = configure(assistant, signing_key, jwk_client, tokenizer)
    before = h.store._guard()
    exported = export_template(control, h.assistant, human)
    assert exported.settings.skill_catalog_budget_profile == "fa-skill-catalog-budget-v1"
    assert exported.settings.model_capacity_profile == "fa-model-capacity-v1"
    inspected = inspect_template(exported, exported)
    assert inspected.references_match and not inspected.authorization and not inspected.activated
    assert h.store._guard() == before and control.records == {} and not h.endpoint.requests


def test_template_thin_client_preserves_the_optional_budget_reference(
    assistant, signing_key, jwk_client, tokenizer,
):
    from web_test_console.agent_template import download, inspect, parse_template

    h, control, _, _ = configure(assistant, signing_key, jwk_client, tokenizer)
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
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


@pytest.mark.parametrize("field,value", [
    ("prompt_profile", "fa-instruction-v1"), ("model_capacity_profile", None),
])
def test_budget_configuration_requires_original_skill_and_capacity_profiles(assistant, field, value):
    original = next(iter(assistant.runtime.profiles.values())).model_dump()
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
