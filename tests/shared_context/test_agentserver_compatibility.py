"""Pinned transport decisions, not a replacement retry policy or cloud evidence."""

from types import SimpleNamespace

import pytest
from azure.ai.agentserver.core import AgentConfig, tasks
from azure.ai.agentserver.core.tasks._client import TransportClassifiedError, _build_default_policies, _raise_classified
from azure.core.exceptions import AzureError
from azure.core.pipeline import PipelineContext, PipelineResponse
from azure.core.pipeline.policies import AsyncRetryPolicy
from azure.core.rest import HttpRequest


@pytest.mark.parametrize("status,body,classification", [
    (502, b"OFFLINE gateway", "transient"),
    (403, b"OFFLINE refused", "permanent"),
    (409, b'{"error":{"code":"binding_mismatch"}}', "evicted"),
    (412, b"", "conflict"),
])
def test_real_core_classification_still_escapes_azure_error(status, body, classification):
    response = SimpleNamespace(status_code=status, headers={"x-ms-request-id": "offline-request"}, body=lambda: body)
    assert not hasattr(tasks, "TransportClassifiedError")
    assert not issubclass(TransportClassifiedError, AzureError)
    with pytest.raises(TransportClassifiedError) as error:
        _raise_classified(response, method="PATCH", url="https://offline.invalid/tasks/original")
    assert (error.value.status, error.value.classification, error.value.request_id) == (
        status, classification, "offline-request")


@pytest.mark.parametrize("method,status,retry_after,expected", [
    ("PATCH", 502, False, False), ("PATCH", 502, True, True),
    ("GET", 502, False, True), ("PATCH", 500, False, True),
    ("PATCH", 503, False, True), ("PATCH", 504, False, True),
    ("PATCH", 409, False, False), ("PATCH", 409, True, True),
    ("PATCH", 429, False, False), ("PATCH", 429, True, True),
])
def test_task_retry_requires_method_status_and_retry_after_intersection(method, status, retry_after, expected):
    policy = next(policy for policy in _build_default_policies(object()) if isinstance(policy, AsyncRetryPolicy))
    settings = policy.configure_retries({})
    assert settings["total"] == 3
    response = PipelineResponse(
        HttpRequest(method, "https://offline.invalid/tasks/original"),
        SimpleNamespace(status_code=status, headers={"Retry-After": "1"} if retry_after else {}),
        PipelineContext(None),
    )
    assert bool(policy.is_retry(settings, response)) is expected


def test_core_session_guid_is_platform_incarnation_metadata(monkeypatch):
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_GUID", "OFFLINE-incarnation")
    assert AgentConfig.from_env().session_guid == "OFFLINE-incarnation"
