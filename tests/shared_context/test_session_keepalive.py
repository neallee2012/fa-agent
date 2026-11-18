"""Original-session status reads use the platform session resource; no SDK process or live service."""

from contextlib import contextmanager
from types import SimpleNamespace

from azure.ai.projects.models import AgentSessionResource, AgentVersionDetails
from azure.core.exceptions import HttpResponseError
import httpx
import pytest

from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.errors import Problem
from test_behavior_foundry import TARGET, INVOCATION, Credential


def platform_session(status="active", **changes):
    return AgentSessionResource({
        "agent_session_id": TARGET.session_id,
        "version_indicator": {"type": "version_ref", "agent_version": TARGET.agent_version},
        "status": status, "created_at": 1, "last_accessed_at": 2, "expires_at": 2_000_000_000,
        **changes,
    })


@pytest.mark.parametrize("status", ["active", "idle", "failed", "deleting", "deleted", "expired"])
def test_session_status_keeps_original_platform_state_without_create_stop_or_invocation(monkeypatch, status):
    seen = []
    value = platform_session(status)

    def get_session(agent_name, session_id):
        seen.append((agent_name, session_id))
        return value

    @contextmanager
    def project(**options):
        assert options["endpoint"] == TARGET.project_endpoint
        assert all(options[name] == 0 for name in ("retry_total", "retry_connect", "retry_read", "retry_status"))
        yield SimpleNamespace(agents=SimpleNamespace(get_session=get_session))

    monkeypatch.setattr("azure.ai.projects.AIProjectClient", project)
    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(
        lambda _: pytest.fail("session metadata must not invoke the Hosted process")))
    try:
        assert client.session_status() is value
        assert seen == [(TARGET.agent_name, TARGET.session_id)]
    finally:
        client.close()


@pytest.mark.parametrize("seconds,expected", [(120, 120), (900, 900), (3600, 3600), (None, 900)])
def test_idle_timeout_comes_from_the_original_version_not_the_latest(monkeypatch, seconds, expected):
    calls = []
    definition = {"kind": "hosted", "cpu": "1", "memory": "2Gi"}
    if seconds is not None:
        definition["session_configuration"] = {"idle_timeout_seconds": seconds}
    value = AgentVersionDetails({
        "id": "original-version", "name": TARGET.agent_name, "version": TARGET.agent_version,
        "created_at": 1, "metadata": {}, "definition": definition,
    })

    def get_version(name, version):
        calls.append((name, version))
        return value

    @contextmanager
    def project(**options):
        assert options["retry_total"] == 0
        yield SimpleNamespace(agents=SimpleNamespace(get_version=get_version))

    monkeypatch.setattr("azure.ai.projects.AIProjectClient", project)
    client = FoundryHttpClient(TARGET, Credential())
    try:
        assert client.session_idle_timeout() == expected
        assert calls == [(TARGET.agent_name, TARGET.agent_version)]
    finally:
        client.close()


@pytest.mark.parametrize("failure,expected", [
    ("read", (503, "CONVERSATION_SESSION_UNAVAILABLE")),
    ("identity", (409, "CONVERSATION_SESSION_CHANGED")),
    ("version", (409, "CONVERSATION_SESSION_CHANGED")),
])
def test_session_read_failure_and_mismatch_are_not_stopping_proof(monkeypatch, failure, expected):
    seen = []

    def get_session(*args):
        seen.append(args)
        if failure == "read":
            raise HttpResponseError("PRIVATE_PLATFORM_MESSAGE")
        return platform_session("idle", **(
            {"agent_session_id": "another-session"} if failure == "identity" else
            {"version_indicator": {"type": "version_ref", "agent_version": "99"}}))

    @contextmanager
    def project(**_options):
        yield SimpleNamespace(agents=SimpleNamespace(get_session=get_session))

    monkeypatch.setattr("azure.ai.projects.AIProjectClient", project)
    client = FoundryHttpClient(TARGET, Credential())
    try:
        with pytest.raises(Problem) as error:
            client.session_status()
        assert (error.value.status, error.value.body["error"]["code"]) == expected
        assert "PRIVATE" not in str(error.value.body) and len(seen) == 1
    finally:
        client.close()

