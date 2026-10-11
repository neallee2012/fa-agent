"""Claimed SDK recovery on explicit offline transports; never native stop proof."""
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from conftest import A
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_foundry import (
    FoundryTarget, NativeExecutionGrant, NativeJobRoute, NativeLaunchAuthority, NativeRecoveryBinding,
    _NativeLaunch, _TargetReservation, native_key,
)
from shared_context.behavior_recovery import ClaimedSdkRecovery, PlatformStopObservation, RecoveryScope, stop_endpoint
from shared_context.behavior_operations import control_key
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from test_behavior_assistant import assistant, submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def recovery(assistant, request):
    h = assistant
    pre_sdk = getattr(request, "param", None) == "pre-sdk"
    provider = AssistantProvider(
        "https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="offline-provider")),
        transport=httpx.MockTransport(lambda _: httpx.Response(503, content=b"offline")),
    )
    h.app = create_assistant_app(h.assistant, h.verifier, provider=provider)
    from fastapi.testclient import TestClient
    h.client = TestClient(h.app)
    h.executor.http_client, h.executor.model_transport = h.client, None
    submit(h)
    if pre_sdk:
        def unacquired(start):
            assignment, ticket = h.assistant.issue(start)
            assert ticket is not None and not assignment.resume
            raise Problem(503, "SYNTHETIC_PRE_ACQUIRE_FAILURE", "No SDK or model was started")
        adapter = h.workers.adapter
        h.workers.adapter = type(adapter)(unacquired, adapter.query, adapter.stop, adapter.verify)
    with pytest.raises(Problem):
        h.runtime.controller.step("binding-one")
    h.mutate(worker_key("job", "job-one"), state="capacity_uncertain", control_remaining=0, queries_reserved=16)
    job = h.workers.inspect("job-one")
    target = FoundryTarget(
        executor_id="executor-one", project_endpoint="https://synthetic.services.ai.azure.com/api/projects/p",
        agent_id="11111111-1111-1111-1111-111111111111", agent_name="assistant",
        agent_version="22", session_id="original-owned-session", agent_oid=A.oid,
    )
    route = NativeJobRoute(job_id=job.spec.job_id, api_binding_id=h.binding.binding_id, grant_id="original-grant")
    native = NativeLaunchAuthority(
        h.workers, team_id=h.binding.team_id, controller_id="internal-controller", targets=(target,),
        jobs=(route,), verifier_version=h.config.verifier_version, clock=lambda: h.time,
        recovery_binding=NativeRecoveryBinding(configuration_sha256="d" * 64,
                                               operator_oid=h.binding.human_oid,
                                               credential_client_id="original-api-client"),
    )
    from shared_context.behavior_workers import ExecutorStart
    launch_id = native_key("launch-id", job.binding.start_request_id)
    start = ExecutorStart(binding=job.binding,
                          request=h.workers._request("start", (job.spec.job_id,), {}, ()), input=job.spec.input)
    launch = _NativeLaunch(
        launch_id=launch_id, configuration_digest=h.workers.configuration_digest(), api_binding_id=h.binding.binding_id,
        grant_id=route.grant_id, start=start, definition=h.binding.work, target=target,
        process_generation=1, permit_digest="a" * 64, post_digest="b" * 64,
        phase="claimed", native_invocation_id="inv-original", claim_id="original-claim",
        process_id="original-process", report_token_digest="c" * 64,
        evidence_sequence=16, probes_reserved=16, workload_version="assistant-v1.2",
    )
    grant = NativeExecutionGrant(
        grant_id=route.grant_id, controller_id=native.controller_id, owner=job.owner, policy_revision=1,
        job_id=job.spec.job_id, definition=h.binding.work, input_digest=hashlib.sha256(job.spec.input).hexdigest(),
        targets=(target,), active=True, remaining_uses=1, not_before=h.time - timedelta(minutes=10),
        expires_at=h.time - timedelta(seconds=1), workload_version="assistant-v1.2",
    )
    h.seed([
        (native_key("launch", launch_id), launch),
        (native_key("target", target.executor_id), _TargetReservation(
            target=target, generation=1, active_launch_id=launch_id, configuration_digest=h.workers.configuration_digest(),
        )),
        (native_key("grant", route.grant_id), grant),
    ])
    native.assistant = h.assistant
    h.workers.adapter = h.workers.adapter.__class__(
        h.workers.adapter.start, h.workers.adapter.query, h.workers.adapter.stop, native.verify,
    )
    config = SimpleNamespace(
        sha256="d" * 64, workers=h.workers.contract, delivery=h.config, bindings=(h.binding,), active_targets=(target,),
        identity=SimpleNamespace(human_oid=h.binding.human_oid, tenant_id=h.binding.tenant_id,
                                 team_id=h.binding.team_id, managed_identity_client_id="original-api-client"),
    )
    model_requests = [
        body["data"]["request_sha256"] for entity in h.transport.backing.rows.values()
        if "data" in (body := json.loads(entity["Payload"])) and "request_sha256" in body["data"]
    ]
    assert len(model_requests) == (0 if pre_sdk else 1)
    from shared_context import behavior_recovery as recovery_module
    scope_type = recovery_module.PreSdkRecoveryScope if pre_sdk else RecoveryScope
    scope = scope_type(
        recovery_id="original-sdk-recovery", configuration_sha256=config.sha256,
        job_id=job.spec.job_id, launch_id=launch_id, operator_oid=h.binding.human_oid,
        model_request_sha256=None if pre_sdk else model_requests[0],
        expires_at=h.time + timedelta(minutes=20),
        **({"recovery_phase": "before_sdk_acquire"} if pre_sdk else {}),
    )
    principal = BehaviorPrincipal(h.binding.tenant_id, h.binding.human_oid, h.binding.team_id,
                                  "human", None, frozenset({"read", "approve"}))
    calls = []

    def stop(observed_target, deadline):
        assert h.time < deadline
        assert h.workers.inspect(job.spec.job_id).cancel_requested
        assert h.workers.inspect(job.spec.job_id).stop_credit == 0
        calls.append(observed_target)
        return PlatformStopObservation(
            target=observed_target, endpoint=stop_endpoint(observed_target), credential_client_id="original-api-client",
            status_code=204, observed_at=h.time,
        )

    service = ClaimedSdkRecovery(config, native, scope, stop, delivery=h.dep.delivery, clock=lambda: h.time)
    return h, service, principal, calls


def approve(service, principal):
    plan = service.plan(principal)
    return service.authorize(principal, plan, "explicit-human-cancellation")


def data(h, kind, *ids):
    key = control_key(kind, *ids)
    return h.store.read_snapshot((key,))[key].payload["data"]


def test_claimed_failed_sdk_can_cancel_after_positive_platform_stop_without_refunding_or_fake_pid(recovery):
    h, service, principal, calls = recovery
    budget = data(h, "budget", "root")
    approval = approve(service, principal)
    result = service.execute(principal, approval)
    assert result["state"] == "cancelled" and result["delivered"] is False
    assert len(calls) == 1
    job = h.workers.inspect("job-one")
    assert job.result_forwarded
    assert data(h, "delivery-work-state", h.binding.obligation_id)["state"] == "cancelled"
    assert data(h, "delivery-obligation", h.binding.obligation_id)["fulfillment"] == "pending"
    assert job.control_remaining == 0 and job.queries_reserved == 16 and job.cancel_credit == job.stop_credit == 0
    assert data(h, "budget", "root") == budget
    assert data(h, "worker-pool", h.workers.contract.workspace_id)["slots"][0]["binding"] is None
    launch = service.native.launch(service.scope.launch_id)
    assert launch.child_pid is None and launch.phase == "sealed"
    proof = service.native.verify(result["platform_stop_evidence_id"])
    assert proof.result.state == "cancelled" and proof.result.unresolved_operations
    assert b'"model_outcome": "unknown"' in proof.result.content
    assert service.execute(principal, approval)["stop_resent"] is False
    assert len(calls) == 1


def test_unknown_stop_never_releases_capacity_or_repeats_the_provider_request(recovery):
    h, service, principal, calls = recovery
    approval = approve(service, principal)

    def unknown(target, deadline):
        calls.append(target)
        raise TimeoutError("untrusted provider text")

    service.stop = unknown
    with pytest.raises(Problem) as error:
        service.execute(principal, approval)
    assert error.value.body["error"]["code"] == "RECOVERY_STOP_UNKNOWN"
    assert data(h, "worker-pool", h.workers.contract.workspace_id)["slots"][0]["binding"] is not None
    assert service.execute(principal, approval)["stop-observation"] is False
    assert len(calls) == 1


@pytest.mark.parametrize("field", ["operator", "target", "endpoint", "credential", "time"])
def test_foreign_or_stale_stop_cannot_seal_original_execution(recovery, field):
    h, service, principal, calls = recovery
    approval = approve(service, principal)
    if field == "operator":
        principal = BehaviorPrincipal(h.binding.tenant_id, "not-the-human", h.binding.team_id,
                                      "human", None, frozenset({"read", "approve"}))
    else:
        original = service.stop

        def invalid(target, deadline):
            value = original(target, deadline)
            return value.model_copy(update={
                "target": {"target": target.model_copy(update={"session_id": "another-session"})},
                "endpoint": {"endpoint": "https://untrusted.invalid"},
                "credential": {"credential_client_id": "another-client"},
                "time": {"observed_at": h.time - timedelta(seconds=1)},
            }[field])

        service.stop = invalid
    with pytest.raises(Problem):
        service.execute(principal, approval)
    assert service.native.launch(service.scope.launch_id).phase == "claimed"
    assert h.workers.inspect("job-one").state == "capacity_uncertain"


@pytest.mark.parametrize("stage", ["stop-intent", "stop-observation"])
def test_committed_ack_loss_is_reconciled_without_another_stop(recovery, monkeypatch, stage):
    h, service, principal, calls = recovery
    from shared_context.behavior_recovery import recovery_key
    approval = approve(service, principal)
    transact = service.store.transact
    wanted = recovery_key(stage, service.scope.recovery_id)

    def lose_ack(request, decide):
        receipt = transact(request, decide)
        if request.request_id == wanted:
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "offline ACK loss after committed EGT")
        return receipt

    monkeypatch.setattr(service.store, "transact", lose_ack)
    with pytest.raises(Problem) as unknown:
        service.execute(principal, approval)
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    monkeypatch.setattr(service.store, "transact", transact)
    state = service.execute(principal, approval)
    assert state["stop-intent"]
    assert len(calls) == (0 if stage == "stop-intent" else 1)
    if stage == "stop-observation":
        assert service.finish(principal, approval)["state"] == "cancelled"
        assert len(calls) == 1
    else:
        assert not state["stop-observation"] and h.workers.inspect("job-one").state == "capacity_uncertain"


def test_changed_budget_after_human_approval_stops_before_cancellation_or_provider(recovery):
    h, service, principal, calls = recovery
    approval = approve(service, principal)
    before = data(h, "budget", "root")
    h.mutate(control_key("budget", "root"), remaining_units=before["remaining_units"] - 1)
    with pytest.raises(Problem) as stale:
        service.execute(principal, approval)
    assert stale.value.body["error"]["code"] == "RECOVERY_PLAN_STALE"
    assert calls == [] and not h.workers.inspect("job-one").cancel_requested


def test_expired_independent_permission_cannot_issue_a_stop_or_extend_runtime_policy(recovery):
    h, service, principal, calls = recovery
    approval = approve(service, principal)
    h.time = approval.expires_at
    with pytest.raises(Problem):
        service.execute(principal, approval)
    assert calls == [] and not h.workers.inspect("job-one").cancel_requested


def test_provider_explicit_already_stopped_is_not_confused_with_idle_metadata(recovery):
    h, service, principal, calls = recovery
    original = service.stop

    def already_stopped(target, deadline):
        return original(target, deadline).model_copy(update={"status_code": 409, "error_code": "session_already_stopped"})

    service.stop = already_stopped
    result = service.execute(principal, approve(service, principal))
    assert result["state"] == "cancelled" and len(calls) == 1


def test_reused_target_refuses_even_the_recovery_plan(recovery):
    h, service, principal, calls = recovery
    target = service.native.launch(service.scope.launch_id).target
    h.mutate(native_key("target", target.executor_id), active_launch_id="different-work")
    with pytest.raises(Problem):
        service.plan(principal)
    assert calls == [] and not h.workers.inspect("job-one").cancel_requested


def test_positive_platform_proof_cannot_be_relabelled_as_a_success_result(recovery):
    h, service, principal, calls = recovery
    service.execute(principal, approve(service, principal))
    launch = service.native.launch(service.scope.launch_id)
    eid = launch.sealed_evidence_id
    from shared_context.behavior_foundry import _NativeEvidence
    saved = data(h, "native-evidence", eid)
    changed = _NativeEvidence.model_validate_json(json.dumps(saved)).model_copy(update={
        "evidence": service.native.verify(eid).model_copy(update={
            "result": service.native.verify(eid).result.model_copy(update={"content": b"False success"}),
        }),
    })
    from shared_context.behavior_recovery import verify_platform_stop
    proof_key = native_key("platform-stop", launch.launch_id)
    proof_row = h.store.read_snapshot((proof_key,))[proof_key]
    with pytest.raises(Problem):
        verify_platform_stop(service.native, launch, changed, proof_row)
    assert len(calls) == 1


def test_signed_operator_http_has_no_business_entry_or_client_supplied_stop_proof(recovery):
    from fastapi.testclient import TestClient
    from shared_context.behavior_recovery_api import create_recovery_app
    h, service, _, calls = recovery
    composition = SimpleNamespace(
        config=service.config, verifier=h.verifier, installation=lambda: None, close=lambda: None,
        _peer_router=None, _peers={}, assistant=None,
        runtime=SimpleNamespace(dep=SimpleNamespace(native_launches=service.native, delivery=h.dep.delivery),
                                clock=lambda: h.time),
    )
    with TestClient(create_recovery_app(composition, service.scope, stop=service.stop)) as client:
        base = "/operator/v1/sdk-recovery"
        assert client.post(base + "/plan", json={}).status_code == 401
        assert client.post(base + "/plan", json={"clock": "override"}, headers=h.headers("human")).status_code == 422
        plan = client.post(base + "/plan", json={}, headers=h.headers("human"))
        assert plan.status_code == 200, plan.text
        auth = client.post(base + "/authorize", json={"plan": plan.json(), "decision_id": "signed-cancel"},
                           headers=h.headers("human"))
        assert auth.status_code == 200, auth.text
        assert client.post(base + "/execute", json={**auth.json(), "stop_receipt": {}},
                           headers=h.headers("human")).status_code == 422
        result = client.post(base + "/execute", json=auth.json(), headers=h.headers("human"))
        assert result.status_code == 200, result.text
        assert result.json()["state"] == "cancelled" and result.json()["delivered"] is False
        assert client.post("/behavior/v1/bindings/binding-one/assistant/submit", json={},
                           headers=h.headers("human")).status_code == 404
        assert client.get("/healthz").json()["models"] == "DISABLED"
    assert len(calls) == 1
