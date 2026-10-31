from __future__ import annotations

import asyncio
import copy

import httpx
import pytest
from azure.ai.projects.models import MemoryStoreDefaultDefinition

from legacy_m2_m4 import m4_config
from shared_context.m4_fixtures import ALIASES
from legacy_m2_m4.m2_gateway import M2Gateway
from legacy_m2_m4.m4_context import M4ContextService, SeedFixture
from shared_context.errors import Problem
from conftest import A, FakeMemory
from test_m2_gateway import Credential, PROJECT
from test_m4_api import M4_ENV, SEED, m3, m4


BASE_DEFINITION = {
    "kind": "default",
    "chat_model": "gpt-4.1-mini",
    "embedding_model": "text-embedding-3-small",
    "options": {
        "user_profile_enabled": True,
        "chat_summary_enabled": True,
        "default_ttl_seconds": 0,
    },
}


def test_recovery_customizes_only_profile_extraction_with_no_oracle_values():
    original = copy.deepcopy(BASE_DEFINITION)
    result = m4_config.memory_definition_for_recovery(original)
    assert original == BASE_DEFINITION
    details = result["options"].pop("user_profile_details")
    assert result == original
    assert "third-person" in details
    assert "concrete" in details and "named subject" in details
    assert all(value not in details.casefold() for aliases in ALIASES.values() for value in aliases)


def test_recovery_definition_round_trips_through_real_sdk_model():
    expected = m4_config.memory_definition_for_recovery(BASE_DEFINITION)
    assert MemoryStoreDefaultDefinition(expected).as_dict() == expected


@pytest.mark.parametrize("path,value", [
    (("kind",), "unknown"),
    (("chat_model",), "different-model"),
    (("embedding_model",), "different-embedding"),
    (("options", "user_profile_enabled"), False),
    (("options", "chat_summary_enabled"), False),
    (("options", "default_ttl_seconds"), 60),
])
def test_recovery_refuses_changed_baseline(path, value):
    changed = copy.deepcopy(BASE_DEFINITION)
    target = changed
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match="baseline"):
        m4_config.memory_definition_for_recovery(changed)


def amended_settings(memory_store="poc-m4-context-02"):
    return m4_config.M4Settings.from_env({
        **M4_ENV,
        "CONTEXT_M4_MEMORY_STORE": memory_store,
        "CONTEXT_M4_ADDITIONAL_REQUESTS": "120",
        "CONTEXT_M4_AMENDMENT_ID": "m4-threefold-20260915",
    })


def test_cumulative_amendment_preserves_original_attempts_and_stops_at_180(m4):
    service, repository, _, _, _ = m4
    original = M2Gateway(service.context, service.settings.model_settings, PROJECT, Credential(), milestone="m4")
    for _ in range(4):
        original.reserve(A)
    old_ids = [entry["attempt_id"] for entry in original.budget(A)["records"]]
    old_key = original.partition, original.row
    asyncio.run(original.aclose())
    updated = M2Gateway(service.context, amended_settings().model_settings, PROJECT, Credential(), milestone="m4")
    try:
        budget = updated.budget(A)
        assert (updated.partition, updated.row) == old_key
        assert budget["original_max_requests"] == 60 and budget["max_requests"] == 180
        assert budget["attempts"] == 4 and budget["remaining"] == 176
        assert [entry["attempt_id"] for entry in budget["records"]] == old_ids
        for _ in range(176):
            updated.reserve(A)
        with pytest.raises(Problem) as exc:
            updated.reserve(A)
        assert exc.value.status == 429 and updated.budget(A)["attempts"] == 180
        assert len(repository.get(*old_key).payload["entries"]) == 180
    finally:
        asyncio.run(updated.aclose())


def test_amended_gateway_cannot_create_a_new_counter(m4):
    updated = M2Gateway(
        m4[0].context, amended_settings().model_settings, PROJECT, Credential(),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected model call"))),
        milestone="m4",
    )
    try:
        with pytest.raises(Problem) as exc:
            updated.budget(A)
        assert exc.value.body["error"]["code"] == "M2_COUNTER_UNCERTAIN"
    finally:
        asyncio.run(updated.aclose())


def test_final_clean_store_does_not_grant_another_model_amendment(m4):
    service, _, _, _, _ = m4
    original = M2Gateway(service.context, service.settings.model_settings, PROJECT, Credential(), milestone="m4")
    for _ in range(4):
        original.reserve(A)
    asyncio.run(original.aclose())
    recovery = M2Gateway(service.context, amended_settings().model_settings, PROJECT, Credential(), milestone="m4")
    for _ in range(16):
        recovery.reserve(A)
    previous = recovery.budget(A)
    asyncio.run(recovery.aclose())
    final = M2Gateway(
        service.context, amended_settings("poc-m4-context-03").model_settings, PROJECT, Credential(), milestone="m4",
    )
    try:
        assert final.budget(A) == previous
        assert final.budget(A)["attempts"] == 20
        assert final.budget(A)["max_requests"] == 180
        assert final.budget(A)["remaining"] == 160
    finally:
        asyncio.run(final.aclose())


def test_final_clean_store_separates_receipts_without_changing_fixture(m4):
    service, repository, _, _, artifacts = m4
    recovery_memory, final_memory = FakeMemory(), FakeMemory()
    recovery = M4ContextService(artifacts.context, recovery_memory, amended_settings(), artifacts)
    existing = recovery.seed(A, A.team, SeedFixture(**SEED))
    before = copy.deepcopy(repository.data)
    final = M4ContextService(artifacts.context, final_memory, amended_settings("poc-m4-context-03"), artifacts)
    created = final.seed(A, A.team, SeedFixture(**SEED))
    assert existing["operation_id"] != created["operation_id"]
    assert recovery_memory.calls[0][2] == final_memory.calls[0][2]
    assert all(repository.data[key] == value for key, value in before.items())
    with pytest.raises(Problem) as exc:
        final.poll_memory(A, A.team, existing["operation_id"])
    assert exc.value.status == 404


def test_recovery_seed_uses_new_store_receipts_without_replaying_original(m4):
    service, repository, old_memory, _, artifacts = m4
    old_result = service.seed(A, A.team, SeedFixture(**SEED))
    preserved = copy.deepcopy(repository.data)
    new_memory = FakeMemory()
    recovered = M4ContextService(artifacts.context, new_memory, amended_settings(), artifacts)
    result = recovered.seed(A, A.team, SeedFixture(**SEED))
    assert result["operation_id"] != old_result["operation_id"]
    assert len(new_memory.calls) == len(old_memory.calls) == 1
    assert new_memory.calls[0][2] == old_memory.calls[0][2]
    for key, value in preserved.items():
        assert repository.data[key] == value
    with pytest.raises(Problem) as exc:
        recovered.poll_memory(A, A.team, old_result["operation_id"])
    assert exc.value.status == 404
    with pytest.raises(Problem) as exc:
        recovered.seed(A, A.team, SeedFixture(**SEED))
    assert exc.value.status == 409 and len(new_memory.calls) == 1


@pytest.mark.parametrize("changes", [
    {"CONTEXT_M4_ADDITIONAL_REQUESTS": "121"},
    {"CONTEXT_M4_ADDITIONAL_REQUESTS": "0120"},
    {"CONTEXT_M4_AMENDMENT_ID": "different-amendment"},
    {"CONTEXT_M4_MAX_REQUESTS": "180"},
    {"CONTEXT_M4_RUN_ID": "m4-context-02"},
    {"CONTEXT_M4_MEMORY_STORE": "poc-m4-context-01"},
    {"CONTEXT_M4_MEMORY_STORE": "poc-m4-context-04"},
])
def test_recovery_rejects_unapproved_allowance_combinations(changes):
    env = {
        **M4_ENV, "CONTEXT_M4_MEMORY_STORE": "poc-m4-context-02",
        "CONTEXT_M4_ADDITIONAL_REQUESTS": "120",
        "CONTEXT_M4_AMENDMENT_ID": "m4-threefold-20260915", **changes,
    }
    with pytest.raises(ValueError):
        m4_config.M4Settings.from_env(env)
