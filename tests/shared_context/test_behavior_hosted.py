"""Hosted runtime configuration checks; NOT Foundry/native evidence."""

import pytest

from shared_context.behavior_hosted import BehaviorExecutorClient, HostedRuntime
from shared_context.errors import Problem
from test_behavior_store import code


@pytest.mark.parametrize("audience", [
    "api://11111111-1111-1111-1111-111111111111",
    "api://11111111-1111-1111-1111-111111111111/fa-context-poc-api",
])
def test_hosted_api_preserves_existing_exact_resource_audience_contract(audience):
    client = BehaviorExecutorClient("https://api.example", audience)
    assert client.audience == audience


@pytest.mark.parametrize("audience", [
    "api://11111111-1111-1111-1111-111111111111/resource/.default",
    "api://11111111-1111-1111-1111-111111111111/resource?extra=1",
    "api://11111111-1111-1111-1111-111111111111/resource/other",
    "https://api.example", "api://invalid", None,
])
def test_hosted_api_rejects_relaxed_or_malformed_audiences(audience):
    with pytest.raises(Problem) as error:
        BehaviorExecutorClient("https://api.example", audience)
    assert code(error) == "HOSTED_API_CONFIGURATION"


def test_native_hosted_entry_is_disabled_without_explicit_configuration(monkeypatch):
    monkeypatch.delenv("BEHAVIOR_HOSTED_ENABLED", raising=False)
    with pytest.raises(Problem) as disabled:
        HostedRuntime.from_environment()
    assert code(disabled) == "HOSTED_RUNTIME_DISABLED"
