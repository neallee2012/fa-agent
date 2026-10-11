"""Catalog rendering contracts; sources and skill bodies are never truncated."""

import hashlib
import json

import pytest

from shared_context.behavior_model_capacity import DeclaredModelLimits, ModelCapacitySnapshot
from shared_context.behavior_skill_catalog import catalog_input_limit, present_catalog
from shared_context.errors import Problem
from shared_context.m3_skills import PurposeSkillCatalog, PurposeSkillEntry


def capacity(budget):
    return ModelCapacitySnapshot.from_declared(
        "gpt-4.1-mini", DeclaredModelLimits(max_prompt_tokens=budget * 50),
    )


def catalog():
    entries = tuple(PurposeSkillEntry(
        name=f"skill-{index}-" + chr(97 + index) * 48,
        title="Guide title " * 15, description="Approved purpose " * 15,
        metadata_sha256="c" * 64, version_id=str(index + 1) * 32,
        sha256=hashlib.sha256(str(index).encode()).hexdigest(), byte_length=100,
        team="team-one", source_work="source-work", source_work_revision=1,
    ) for index in range(3))
    return PurposeSkillCatalog(schema_version="fa-approved-skill-catalog-v2", entries=entries)


def test_full_description_exact_token_threshold_and_original_bytes():
    original = catalog()
    before = original.model_dump_json()
    roomy = present_catalog(original, capacity(5000), "", len)
    assert roomy.stage == "full"
    exact = present_catalog(original, capacity(roomy.description_tokens), "", len)
    assert exact.description == roomy.description
    assert exact.description_tokens == exact.budget_tokens == len(exact.description.encode())
    assert original.model_dump_json() == before
    assert exact.capacity_sha256 != roomy.capacity_sha256
    assert exact.catalog_sha256 == roomy.catalog_sha256


@pytest.mark.parametrize("budget,stage", [(2700, "shortened"), (650, "names"), (440, "omitted")])
def test_ordered_degradation_keeps_lookup_and_prioritizes_exact_requested_names(budget, stage):
    original = catalog()
    before = original.model_dump_json()
    requested = original.entries[-1].name
    result = present_catalog(original, capacity(budget), "Please use " + requested + ".", len)
    assert result.stage == stage
    assert result.description_tokens == len(result.description.encode()) <= budget
    packet = json.loads(result.description.split("\n", 1)[1])
    assert "read_skill(name)" in packet["lookup"] and "does not mean unavailable" in packet["lookup"]
    assert result.omitted_count == len(original.entries) - len(packet["entries"])
    if stage == "omitted":
        assert result.omitted_count > 0 and packet["entries"][0]["name"] == requested
    if stage == "names":
        assert all(set(entry) == {"name"} for entry in packet["entries"])
    if stage == "shortened":
        assert "description" not in packet["entries"][0]
        assert packet["entries"][0]["description_excerpt"] == original.entries[0].description[:96]
        assert packet["entries"][0]["description_omitted_characters"] == len(original.entries[0].description) - 96
    assert original.model_dump_json() == before
    assert present_catalog(original, capacity(budget), "Please use " + requested + ".", len) == result


def test_cjk_adjacent_exact_skill_name_is_prioritized_during_omission():
    original = catalog()
    requested = original.entries[-1].name
    result = present_catalog(original, capacity(440), "請使用" + requested + "技能", len)
    assert result.stage == "omitted"
    packet = json.loads(result.description.split("\n", 1)[1])
    assert packet["entries"][0]["name"] == requested


@pytest.mark.parametrize("count", [None, 0, -1, True, 1.5])
def test_unknown_or_invalid_counts_fail_instead_of_claiming_zero(count):
    with pytest.raises(Problem) as error:
        present_catalog(catalog(), capacity(5000), "", lambda _: count)
    assert error.value.body["error"]["code"] == "SKILL_CATALOG_TOKEN_COUNT_UNKNOWN"


@pytest.mark.parametrize("limits,expected", [
    ({"max_prompt_tokens": 10000}, 10000),
    ({"context_window_tokens": 10000, "max_output_tokens": 1000}, 9000),
    ({"context_window_tokens": 10000, "max_prompt_tokens": 8000, "max_output_tokens": 1000}, 8000),
])
def test_declared_input_budget_uses_known_conservative_capacity(limits, expected):
    snapshot = ModelCapacitySnapshot.from_declared("gpt-4.1-mini", DeclaredModelLimits(**limits))
    original = snapshot.model_dump_json()
    assert catalog_input_limit(snapshot) == expected
    assert snapshot.model_dump_json() == original


@pytest.mark.parametrize("limits", [{}, {"context_window_tokens": 10000}, {"max_output_tokens": 1000}])
def test_missing_input_capacity_is_not_invented(limits):
    snapshot = ModelCapacitySnapshot.from_declared("gpt-4.1-mini", DeclaredModelLimits(**limits))
    with pytest.raises(Problem) as error:
        catalog_input_limit(snapshot)
    assert error.value.body["error"]["code"] == "SKILL_CATALOG_CAPACITY_UNKNOWN"


def test_too_small_budget_is_explicit_not_an_empty_success():
    with pytest.raises(Problem) as error:
        present_catalog(catalog(), capacity(1), "", len)
    assert error.value.body["error"]["code"] == "SKILL_CATALOG_BUDGET_TOO_SMALL"


def test_empty_authorized_catalog_is_distinct_from_omitted_entries():
    empty = PurposeSkillCatalog(schema_version="fa-approved-skill-catalog-v2", unavailable_count=2)
    result = present_catalog(empty, capacity(100), "", len)
    assert result.stage == "full" and result.omitted_count == 0 and result.unavailable_count == 2
    assert result.description_tokens > 0
