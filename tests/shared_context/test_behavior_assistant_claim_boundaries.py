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
from test_behavior_assistant_acquire_conflicts import native_acquire, current_rotation
from test_behavior_rotation_cutoff import two_rotations
from test_behavior_rotation_settlement_history import current_cancelled
from test_behavior_runtime_rotation import rotated
from test_behavior_assistant import call
from test_behavior_api import METRICS
from test_m2_copilot import ScriptedEndpoint
from test_m3_auth import jwk_client, signing_key

pytestmark = pytest.mark.parametrize("native_acquire", ["before-native"], indirect=True)


def reviewed(c):
    runtime, b = c.composition.runtime, c.binding
    profile = c.config.assistant.profiles[0]
    source = wire.SourcePrepare(request_id="startup-source", version=1, original=METRICS)
    runtime.prepare_source(b.binding_id, profile.source.source_id, source, c.principal)
    runtime.commit_source(b.binding_id, profile.source.source_id, wire.Command(request_id=source.request_id), c.principal)
    draft = wire.ClaimDraft(request_id="startup-claim", version=1, text="The accepted metrics source has been reviewed.",
                           supporting=(profile.source,), effective_from=c.clock[0])
    preview = runtime.preview_claim(b.binding_id, profile.claim_id, draft, c.principal)
    command = wire.ClaimReview(**draft.model_dump(), decision_id="startup-reviewed",
        candidate_sha256=preview.candidate_sha256, evidence_sha256=preview.evidence_sha256,
        policy_version=preview.policy_version,
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"))
    runtime.review_claim(b.binding_id, profile.claim_id, command, c.principal)
    runtime.commit_claim(b.binding_id, profile.claim_id, wire.Command(request_id=command.request_id), c.principal)


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


@pytest.mark.parametrize("phase", ["native-commit", "native-post-read", "assignment"])
def test_full_http_startup_preserves_original_capability_across_known_conflicts(hosted_flow, monkeypatch, phase):
    h, c = hosted_flow, hosted_flow.c
    original = c.transport.send
    sid = key("session-id", c.starts[0].binding.start_request_id)
    injected = []
    claim_posts = []
    postcommitted = [False]
    def send(request, **kwargs):
        native_request = next((r for r in c.requests if "/hosted/launches/" in r.url.path and r.url.path.endswith("/claim")), None)
        native_id = (None if native_request is None else native_key(
            "claim", c.launch_id, json.loads(native_request.content)["request_id"]))
        native_post = request.method == "POST" and native_id and native_id.encode() in request.body
        if native_post:
            claim_posts.append(request)
        match = (native_post if phase == "native-commit" else
                 request.method == "POST" and key("issue", sid).encode() in request.body if phase == "assignment" else
                 postcommitted[0] and request.method == "GET"
                 and worker_key("job", c.binding.job_id) in request.url)
        if match and not injected:
            injected.append(phase)
            c.tick()
        result = original(request, **kwargs)
        if native_post:
            postcommitted[0] = True
        return result
    monkeypatch.setattr(c.transport, "send", send)
    run(h)
    assert injected == [phase]
    assert len(claim_posts) == (2 if phase == "native-commit" else 1)


@pytest.mark.parametrize("phase", [
    "all-authority-ledgers", "context-native-evidence", "provider-evidence",
])
def test_complete_sdk_flow_survives_controller_conflicts_without_repeating_effects(hosted_flow, monkeypatch, phase):
    from shared_context.behavior_operations import control_key
    h, c = hosted_flow, hosted_flow.c
    sid = key("session-id", c.starts[0].binding.start_request_id)
    ids = {key(name, sid, command): name for name, command in (
        ("acquire", "sdk-start"), ("context", "current-context"), ("save", "own-result"), ("finish", "finish-task"))}
    ids.update({key("artifact-stage", sid, stage): stage for stage in ("created", "upload_unknown", "saved")})
    original = c.transport.send
    injected = set()
    consumed = [False]
    uploads, creates = [], []
    original_upload, original_create = c.authority.artifacts.upload, c.authority.artifacts.create
    budget_key = control_key("budget", c.binding.owner.root_budget_id)
    before_budget = c.store.read_snapshot((budget_key,))[budget_key].payload["data"]
    def upload(*args, **kwargs):
        uploads.append(1)
        return original_upload(*args, **kwargs)
    def create(*args, **kwargs):
        creates.append(1)
        return original_create(*args, **kwargs)
    monkeypatch.setattr(c.authority.artifacts, "upload", upload)
    monkeypatch.setattr(c.authority.artifacts, "create", create)
    def send(request, **kwargs):
        last = c.requests[-1] if c.requests else None
        if last is not None and last.url.path.endswith(("/reserve-model", "/model")):
            command = json.loads(last.content)
            kind = "model" if last.url.path.endswith("/reserve-model") else "model-send"
            ids[key(kind, sid, command["request_id"])] = kind
        ledger = next((label for rid, label in ids.items() if request.method == "POST" and rid.encode() in request.body), None)
        selected = (ledger if phase == "all-authority-ledgers" else
                    phase if request.method == "GET" and request.url.split("?")[0].rstrip("()").endswith("/Behavior")
                    and last is not None and (last.url.path.endswith("/context") if phase == "context-native-evidence"
                                             else consumed[0]) else None)
        if selected is not None and selected not in injected:
            injected.add(selected)
            c.tick()
        result = original(request, **kwargs)
        if ledger == "model-send":
            consumed[0] = True
        return result
    monkeypatch.setattr(c.transport, "send", send)
    run(h)
    assert uploads == creates == [1]
    assert injected == ({"acquire", "context", "model", "model-send", "save", "created", "upload_unknown", "saved", "finish"}
                        if phase == "all-authority-ledgers" else {phase})
    assert len(h.supervisor.threads) == 1
    assert len([r for r in c.requests if r.url.path.endswith("/model")]) == 3
    after_budget = c.store.read_snapshot((budget_key,))[budget_key].payload["data"]
    assert after_budget["remaining_attempts"] == before_budget["remaining_attempts"] - 5
    assert after_budget["remaining_units"] == before_budget["remaining_units"] - 5


@pytest.mark.parametrize("boundary", ["native-http-lost", "assignment-http-lost", "native-commit-lost", "assignment-commit-lost"])
def test_lost_claim_response_never_reissues_a_capability_or_spawns_sdk(hosted_flow, monkeypatch, boundary):
    h, c = hosted_flow, hosted_flow.c
    original_http, original_table = c.api_transport.handle_request, c.transport.send
    sid = key("session-id", c.starts[0].binding.start_request_id)
    observations = []
    def http(request):
        response = original_http(request)
        hit = (request.url.path.endswith("/hosted/launches/" + c.launch_id + "/claim") if boundary == "native-http-lost"
               else request.url.path.endswith("/assistant/launches/" + c.launch_id + "/claim"))
        if boundary.endswith("http-lost") and hit:
            assert response.status_code == 200
            observations.append(boundary)
            response.close()
            raise httpx.ReadTimeout("SYNTHETIC_PRIVATE_RESPONSE_LOST", request=request)
        return response
    def table(request, **kwargs):
        last = c.requests[-1] if c.requests else None
        native_id = (native_key("claim", c.launch_id, json.loads(last.content)["request_id"])
                     if last is not None and "/hosted/" in last.url.path and last.url.path.endswith("/claim") else None)
        hit = request.method == "POST" and (
            native_id is not None and native_id.encode() in request.body if boundary == "native-commit-lost" else
            key("issue", sid).encode() in request.body)
        if boundary.endswith("commit-lost") and hit:
            observations.append(boundary)
            c.transport.after_apply = "lost"
            try:
                return original_table(request, **kwargs)
            finally:
                c.transport.after_apply = None
        return original_table(request, **kwargs)
    h.api.http._transport = httpx.MockTransport(http)
    monkeypatch.setattr(c.transport, "send", table)
    with pytest.raises(Problem):
        h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)
    assert observations == [boundary]
    assert not h.supervisor.threads and h.endpoint.requests == []
    requests = len(c.requests)
    assert h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)["status"] == "pending"
    assert len(c.requests) == requests
    rows = c.store.read_snapshot((key("session", sid),))
    assert (rows[key("session", sid)] is not None) == boundary.startswith("assignment")


@pytest.mark.parametrize("failure", ["500", "network", "other-412"])
def test_unknown_assignment_failures_are_one_http_attempt_and_safely_classified(hosted_flow, failure):
    h, c = hosted_flow, hosted_flow.c
    original = c.api_transport.handle_request
    observed = []
    def send(request):
        if request.url.path.endswith("/assistant/launches/" + c.launch_id + "/claim"):
            observed.append(request)
            if failure == "network":
                raise httpx.ReadTimeout("SYNTHETIC_PRIVATE_CANARY", request=request)
            return httpx.Response(500 if failure == "500" else 412, text="SYNTHETIC_PRIVATE_CANARY",
                headers={"x-behavior-assistant-error-code": "MIGRATION_HISTORY_INVALID",
                         "x-behavior-assistant-error-phase": "native_claim"})
        return original(request)
    h.api.http._transport = httpx.MockTransport(send)
    with pytest.raises(Problem):
        h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)
    assert len(observed) == 1 and not h.supervisor.threads and h.endpoint.requests == []
    with h.supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id=?", (c.launch_id,)).fetchone()
    assert "SYNTHETIC_PRIVATE_CANARY" not in raw
    if failure != "network":
        assert json.loads(raw)["upstream_error_phase"] == "native_claim"


@pytest.mark.parametrize("stage", ["native", "assignment"])
def test_two_simultaneous_http_claims_return_only_one_fresh_capability(hosted_flow, monkeypatch, stage):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    h, c = hosted_flow, hosted_flow.c
    request_id = "parallel-original-claim"
    native_command = wire.HostedClaimRequest(
        request_id=request_id, invocation_id="offline-concurrent", identity=c.identity,
        process_id="original-process", launch_permit=c.permit)
    native_body = native_command.model_dump(mode="json", exclude={"launch_permit"})
    url = f"/behavior/v1/bindings/{c.binding.binding_id}/hosted/launches/{c.launch_id}/claim"
    headers = {"Authorization": "Bearer " + c.token, "x-behavior-launch-permit": c.permit.get_secret_value()}
    body = native_body
    sid = key("session-id", c.starts[0].binding.start_request_id)
    if stage == "assignment":
        response = c.client.post(url, json=body, headers=headers)
        assert response.status_code == 200
        claim = wire.HostedClaimReceipt.model_validate_json(response.content)
        url = f"/behavior/v1/bindings/{c.binding.binding_id}/assistant/launches/{c.launch_id}/claim"
        headers = {"Authorization": "Bearer " + c.token,
                   "x-behavior-report-token": response.headers["x-behavior-report-token"]}
        body = wire.HostedProbeBody(request_id="assistant-native-claim", invocation_id=claim.assignment.invocation_id,
            identity=c.identity, process_id=claim.assignment.process_id, claim_id=claim.assignment.claim_id).model_dump(mode="json")
    barrier = Barrier(2)
    rid = native_key("claim", c.launch_id, request_id) if stage == "native" else key("issue", sid)
    original = c.transport.send
    posts = []
    def send(request, **kwargs):
        if request.method == "POST" and rid.encode() in request.body:
            posts.append(1)
            barrier.wait(timeout=10)
        return original(request, **kwargs)
    monkeypatch.setattr(c.transport, "send", send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(c.client.post, url, json=body, headers=headers) for _ in range(2)]
        responses = [future.result(timeout=20) for future in futures]
    header = "x-behavior-report-token" if stage == "native" else "x-behavior-assistant-ticket"
    assert sum(header in response.headers for response in responses) == 1
    assert sum(response.status_code == 200 for response in responses) == 1
    assert len(posts) == 2 and not h.supervisor.threads and h.endpoint.requests == []


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


@pytest.mark.parametrize("failure", ["http500", "timeout"])
def test_consumed_model_intent_never_retries_an_unknown_provider_send(hosted_flow, failure):
    h, c = hosted_flow, hosted_flow.c
    sends = []
    async def unknown(request):
        sends.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("SYNTHETIC_PRIVATE_PROVIDER", request=request)
        return httpx.Response(500, text="SYNTHETIC_PRIVATE_PROVIDER", request=request)
    c.composition.assistant.provider.transport = httpx.MockTransport(unknown)
    assert h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)["status"] == "accepted"
    h.supervisor.join(timeout=70)
    assert len(sends) == 1
    assert len([r for r in c.requests if r.url.path.endswith("/model")]) == 1
    assert h.supervisor.observe(c.launch_id).phase == "spawn_unknown"
    assert len(h.supervisor.threads) == 1
    with h.supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id=?", (c.launch_id,)).fetchone()
    assert "SYNTHETIC_PRIVATE_PROVIDER" not in raw


def test_post_commit_assignment_exception_does_not_inherit_a_stale_fresh_flag(hosted_flow, monkeypatch):
    h, c = hosted_flow, hosted_flow.c
    sid = key("session-id", c.starts[0].binding.start_request_id)
    original = c.store.transact
    committed = []
    def transact(request, decide, **kwargs):
        result = original(request, decide, **kwargs)
        if request.request_id == key("issue", sid):
            committed.append(result)
            raise Problem(412, "BEHAVIOR_CONFLICT", "synthetic observer after a retained commit")
        return result
    monkeypatch.setattr(c.store, "transact", transact)
    with pytest.raises(Problem) as error:
        h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)
    assert error.value.body["error"]["upstream_error_code"] == "ASSISTANT_LEDGER_ALREADY_COMMITTED"
    assert len(committed) == 1 and not h.supervisor.threads and h.endpoint.requests == []
    assert c.store.read_snapshot((key("session", sid),))[key("session", sid)] is not None
