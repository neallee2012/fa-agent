"""Full HTTP native claim -> assignment -> actual local SDK; no external service."""
from pathlib import Path
from types import SimpleNamespace
import json
import os

import httpx
import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_hosted import AssistantHostedRuntime, AssistantSupervisor
from shared_context.behavior_hosted import BehaviorExecutorClient, HostedInvocation
from shared_context.behavior_foundry import native_key
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from shared_context.source_materials import digest
from pydantic import SecretStr
from test_behavior_assistant_acquire_conflicts import native_acquire
from behavior_assistant_acquire_conflicts_helpers import installed_assistant
from behavior_assistant_helpers import call
from test_behavior_api import METRICS
from test_m2_copilot import ScriptedEndpoint
from test_m3_auth import jwk_client, signing_key
from behavior_assistant_claim_boundaries_helpers import reviewed

pytestmark = pytest.mark.parametrize("native_acquire", ["before-native"], indirect=True)


@pytest.fixture
def hosted_flow(native_acquire, tmp_path):
    c = native_acquire
    reviewed(c)
    endpoint = ScriptedEndpoint([call("read_context", {}, 1),
        call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 1}, 2),
        call("finish_task", {}, 3)])
    endpoint.expected_url = c.config.assistant.provider_endpoint
    c.composition.assistant.provider.transport = endpoint.transport
    c.composition.assistant.provider.credential = SimpleNamespace(get_token=lambda _: SimpleNamespace(token="SYNTHETIC_PROVIDER"))
    runtime = Path(os.environ["M2_TEST_RUNTIME"])
    assert runtime.is_file(), "explicit pinned runtime is required; no auto-download"
    supervisor = AssistantSupervisor(tmp_path / "hosted", runtime_path=runtime)
    api = BehaviorExecutorClient("https://context.invalid", c.config.identity.audience,
                                SimpleNamespace(get_token=lambda _: SimpleNamespace(token=c.token)),
                                transport=c.api_transport)
    host = AssistantHostedRuntime(c.identity, supervisor, api, clock=lambda: c.clock[0])
    payload = HostedInvocation(binding_id=c.binding.binding_id, launch_id=c.launch_id, launch_permit=c.permit)
    try:
        yield SimpleNamespace(c=c, host=host, supervisor=supervisor, api=api, payload=payload, endpoint=endpoint)
    finally:
        supervisor.join(timeout=70)
        api.close()


def run(h):
    result = h.host.start(h.payload, "offline-full-invocation", h.c.identity.session_id)
    assert result["status"] == "accepted"
    h.supervisor.join(timeout=70)
    state = h.supervisor.observe(h.c.launch_id)
    assert state.phase == "sealed" and state.sdk_stop.graceful and state.outcome == "succeeded"
    assert len(h.endpoint.requests) == 3
    assert len([r for r in h.c.requests if r.url.path.endswith("/hosted/launches/" + h.c.launch_id + "/claim")]) == 1
    assert len([r for r in h.c.requests if r.url.path.endswith("/assistant/launches/" + h.c.launch_id + "/claim")]) == 1
    assert h.host.start(h.payload, "offline-full-invocation", h.c.identity.session_id)["status"] == "pending"


@pytest.mark.parametrize("fault", ["revoked", "expired", "wrong-report", "changed-request"])
def test_assignment_conflict_or_claim_replay_cannot_bypass_current_scope(hosted_flow, monkeypatch, fault):
    from test_behavior_settlement import write
    from shared_context.behavior_operations import control_key
    h, c = hosted_flow, hosted_flow.c
    sid = key("session-id", c.starts[0].binding.start_request_id)
    original = c.transport.send
    injected = []
    def send(request, **kwargs):
        if request.method == "POST" and key("issue", sid).encode() in request.body and not injected:
            injected.append(fault)
            c.tick()
            if fault == "revoked":
                ownerkey = control_key("owner", c.binding.owner.owner_record_id)
                row = c.competitor.read_snapshot((ownerkey,))[ownerkey]
                write(c.competitor, ownerkey, {**row.payload["data"], "active": False})
            elif fault == "expired":
                c.clock[0] = c.config.policies[0].policy.valid_until
            elif fault == "changed-request":
                # A conflicting request ID cannot be treated as the same immutable issue.
                from shared_context.behavior_store import ControlRequest, ControlChanges
                c.competitor.transact(ControlRequest(request_id=key("issue", sid), request_digest="f" * 64),
                    lambda _: ControlChanges(result={"unrelated": True}))
        return original(request, **kwargs)
    if fault == "wrong-report":
        original_http = c.api_transport.handle_request
        def http(request):
            if "/assistant/launches/" in request.url.path:
                request.headers["x-behavior-report-token"] = "f" * 64
            return original_http(request)
        h.api.http._transport = httpx.MockTransport(http)
    else:
        monkeypatch.setattr(c.transport, "send", send)
    with pytest.raises(Problem):
        h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)
    assert not h.supervisor.threads and h.endpoint.requests == []
    assert c.store.read_snapshot((key("session", sid),))[key("session", sid)] is None


@pytest.mark.parametrize("fault", ["wrong-permit", "wrong-identity", "unknown500"])
def test_first_http_claim_does_not_relax_identity_or_retry_transport_failure(hosted_flow, fault):
    h, c = hosted_flow, hosted_flow.c
    if fault == "wrong-permit":
        h.payload = h.payload.model_copy(update={"launch_permit": SecretStr("f" * 64)})
    elif fault == "wrong-identity":
        h.host.identity = h.host.identity.model_copy(update={"agent_version": "999"})
    else:
        def failed(request):
            c.requests.append(request)
            return httpx.Response(500, headers={"x-behavior-assistant-error-code": "BEHAVIOR_CONFLICT"},
                                  text="SYNTHETIC_PRIVATE_FAILURE")
        h.api.http._transport = httpx.MockTransport(failed)
    with pytest.raises(Problem):
        h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)
    assert len(c.requests) == 1 and not h.supervisor.threads and not h.endpoint.requests
    assert c.native.launch(c.launch_id).phase == "post_unknown"
