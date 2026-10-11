"""OFFLINE native-path composition: signed JWT, real HTTP clients/local transport.

The final lifecycle test runs the actual constrained local child/supervisor.
It does not provision Entra grants, invoke Azure, or establish Hosted PASS.
"""

import hashlib
import json
import time
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from conftest import A, B
from shared_context import behavior_wire as wire
from shared_context.behavior_api import create_behavior_app
from shared_context.behavior_foundry import (
    FoundryExecutor, FoundryHttpClient, FoundryTarget, NativeExecutionGrant,
    NativeJobRoute, NativeLaunchAuthority, native_key,
)
from shared_context.behavior_hosted import BehaviorExecutorClient, ChecksumSupervisor, LocalAssignment
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime import BehaviorRuntime, RuntimeController
from shared_context.errors import Problem
from test_behavior_api import BASE, CTX, RuntimeHarness
from test_behavior_delivery import NOW
from test_m3_auth import jwk_client, signing_key, token

CHECKS = ("checksum_matches", "byte_count_matches", "newline_count_matches",
          "native_binding_verified", "executor_idle_verified")


class NativeHarness(RuntimeHarness):
    def __init__(self, auth, key_client, signing):
        super().__init__(auth, key_client, signing, work_checks=CHECKS)
        self.actual_invocation_id = "inv_provider_generated_0648c3c2"
        self.native_calls = []
        self.target = FoundryTarget(
            executor_id=self.workers.contract.executor_id,
            project_endpoint="https://native.example/api/projects/test",
            agent_id="11111111-1111-1111-1111-111111111111", agent_name="checksum",
            agent_version="7", session_id="assigned-session", agent_oid=A.oid,
        )
        self.native = NativeLaunchAuthority(
            self.workers, team_id=A.team, controller_id="internal-controller", targets=(self.target,),
            jobs=(NativeJobRoute(job_id="job-one", api_binding_id="binding-one", grant_id="native-grant"),),
            verifier_version=self.config.verifier_version, clock=lambda: self.time,
        )
        self.native_handler = self.accept
        credential = SimpleNamespace(get_token=lambda _: SimpleNamespace(token="SYNTHETIC_FOUNDRY_CREDENTIAL"))
        self.native_http = FoundryHttpClient(self.target, credential, transport=httpx.MockTransport(self.transport_native))
        self.native_executor = FoundryExecutor(self.native, (self.native_http,))
        self.workers.adapter = self.native_executor.adapter()
        self.dep = replace(self.dep, native_launches=self.native)
        self.runtime = BehaviorRuntime(self.dep, (self.binding,), clock=lambda: self.time,
                                       mode="offline_contract", controller_id="internal-controller")
        self.app = create_behavior_app(self.runtime, self.verifier)
        self.client = TestClient(self.app)
        self.submit()
        job = self.workers.inspect("job-one")
        self.seed([(native_key("grant", "native-grant"), NativeExecutionGrant(
            grant_id="native-grant", controller_id="internal-controller", owner=CTX,
            policy_revision=1, job_id="job-one", definition=self.binding.work,
            input_digest=hashlib.sha256(job.spec.input).hexdigest(),
            targets=(self.target,), active=True, remaining_uses=4,
            not_before=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=5),
        ))])

    def transport_native(self, request):
        self.native_calls.append(request)
        try:
            return self.native_handler(request)
        except Exception as exc:
            self.native_error = exc
            raise

    def accept(self, request):
        return httpx.Response(202, headers={
            "x-agent-invocation-id": self.actual_invocation_id,
            "x-agent-session-id": self.target.session_id,
        }, json={"status": "pending", "invocation_id": self.actual_invocation_id,
                 "launch_id": json.loads(request.content)["launch_id"]})

    def launch(self):
        self.runtime.controller.step("binding-one")
        sent = json.loads(self.native_calls[0].content)
        return sent["launch_id"], self.native_calls[0].headers[wire.NATIVE_LAUNCH_PERMIT_HEADER]

    def identity(self):
        return wire.HostedIdentity(
            agent_id=self.target.agent_id, agent_name=self.target.agent_name,
            agent_version=self.target.agent_version, session_id=self.target.session_id,
        )

    def claim_body(self, *, process_id="supervisor", request_id="claim"):
        return wire.HostedClaimBody(
            request_id=request_id, invocation_id=self.actual_invocation_id,
            identity=self.identity(), process_id=process_id,
        )

    def callback(self, suffix, body, *, launch_id, capability=None, report=False, identity="agent"):
        headers = self.headers(identity)
        if capability is not None:
            headers["X-Behavior-Report-Token" if report else "X-Behavior-Launch-Permit"] = capability
        return self.client.post(
            BASE + "/hosted/launches/" + launch_id + suffix,
            content=body.model_dump_json() if isinstance(body, wire.WireModel) else json.dumps(body),
            headers=headers,
        )


@pytest.fixture
def h(auth, jwk_client, signing_key):
    value = NativeHarness(auth, jwk_client, signing_key)
    yield value
    value.native_http.close()


@pytest.mark.parametrize("path", [
    "/jobs/job-one/consume", "/jobs/job-one/start", "/jobs/job-one/adopt",
    "/supervisor/claim", "/supervisor/drain", "/executor/prepare", "/executor/probe",
])
def test_generic_agent_consume_role_has_no_controller_route(h, path):
    for principal in ("agent", "human"):
        assert h.post(path, {"request_id": "fake", "controller_id": "internal-controller"}, principal).status_code == 404
    assert h.native_calls == []
    assert h.workers.inspect("job-one").state == "queued"


def test_controller_is_explicit_internal_handle_not_a_role_or_known_id(h):
    with pytest.raises(Problem):
        h.runtime._controller_step("binding-one", "internal-controller")
    with pytest.raises(Problem):
        h.runtime._controller_step("binding-one", RuntimeController(h.runtime, "internal-controller"))
    with pytest.raises(Problem):
        BehaviorRuntime(h.dep, (h.binding,), clock=lambda: h.time, mode="offline_contract")
    assert h.runtime.controller.step("binding-one").state == "starting"


def test_provider_generated_invocation_id_is_bound_by_first_permit_claim(h):
    launch_id, permit = h.launch()
    logical = h.workers.inspect("job-one").binding.start_request_id
    actual = "inv_provider_generated_0648c3c2"
    assert actual != logical
    body = h.claim_body().model_copy(update={"invocation_id": actual})
    response = h.callback("/claim", body, launch_id=launch_id, capability=permit)
    assert response.status_code == 200, response.text
    assert response.json()["assignment"]["invocation_id"] == actual
    assert h.native.launch(launch_id).native_invocation_id == actual


@pytest.mark.parametrize("request_id", ["claim", "second-post-claim"])
def test_same_permit_cannot_claim_second_provider_invocation_or_obtain_report_token(h, request_id):
    launch_id, permit = h.launch()
    first = h.callback("/claim", h.claim_body(), launch_id=launch_id, capability=permit)
    assert first.status_code == 200 and "x-behavior-report-token" in first.headers
    second = h.callback("/claim", h.claim_body(request_id=request_id).model_copy(
        update={"invocation_id": "inv_a_second_post"}), launch_id=launch_id, capability=permit)
    assert second.status_code == 403 and "x-behavior-report-token" not in second.headers
    assert h.native.launch(launch_id).native_invocation_id == h.actual_invocation_id
    assert h.native.launch(launch_id).start.binding.start_request_id != h.actual_invocation_id


@pytest.mark.parametrize("change", [
    None, {"session_id": "another-work-session"}, {"agent_version": "999"},
    {"agent_oid": B.oid}, {"agent_name": "another-agent"},
])
def test_instruction_callback_requires_exact_per_work_target_not_template_session(h, monkeypatch, change):
    from shared_context.behavior_foundry import instruction_target

    launch_id, _ = h.launch()
    expected = instruction_target(h.target, "job-one")
    actual = expected if change is None else expected.model_copy(update=change)
    h.mutate(native_key("launch", launch_id), target=actual.model_dump(mode="json"))
    monkeypatch.setattr(h.workers, "instruction", lambda job_id: job_id == "job-one")
    registered = SimpleNamespace(
        binding=h.binding,
        profile=SimpleNamespace(instruction=SimpleNamespace(hosted_agent_version=h.target.agent_version)),
        hosted_session_id=expected.session_id,
    )
    monkeypatch.setattr(h.workers, "work_policy", SimpleNamespace(
        _job_record=lambda job_id: registered if job_id == "job-one" else None))
    principal = h.verifier(h.client.build_request("GET", "/", headers=h.headers("agent")))
    if change is None:
        assert expected not in h.native.targets
        assert h.native.targets_for("job-one") == (expected,)
        assert h.runtime.executor_callback("binding-one", launch_id, principal, claim=True) is h.native
    else:
        with pytest.raises(Problem) as denied:
            h.runtime.executor_callback("binding-one", launch_id, principal, claim=True)
        assert denied.value.body["error"]["code"] == "BEHAVIOR_EXECUTOR_DENIED"


def test_unclaimed_202_never_infers_native_id_for_query_or_cancel(h):
    launch_id, _permit = h.launch()
    binding = h.workers.inspect("job-one").binding
    assert h.native.launch(launch_id).native_invocation_id is None
    assert h.native.launch(launch_id).phase == "post_unknown"
    calls = len(h.native_calls)
    with pytest.raises(Problem) as query:
        h.native_executor.query(binding)
    assert query.value.body["error"]["code"] == "NATIVE_INVOCATION_UNKNOWN"
    h.workers.cancel("job-one", CTX)
    with pytest.raises(Problem):
        h.workers.stop("job-one", CTX)
    assert len(h.native_calls) == calls
    assert h.native.launch(launch_id).native_invocation_id is None


def test_report_capability_is_bound_to_claimed_actual_id_not_logical_id(h):
    launch_id, permit = h.launch()
    claim = h.callback("/claim", h.claim_body(), launch_id=launch_id, capability=permit)
    assignment = claim.json()["assignment"]
    binding = h.workers.inspect("job-one").binding
    probe = h.native.probe(binding)
    body = wire.HostedProbeBody(
        **h.claim_body(request_id="resolve").model_copy(update={"invocation_id": binding.start_request_id}).model_dump(),
        claim_id=assignment["claim_id"],
    )
    response = h.callback("/probes/" + probe.probe_id + "/resolve", body, launch_id=launch_id,
                          capability=claim.headers["x-behavior-report-token"], report=True)
    assert response.status_code == 403


def test_mi_or_session_assertion_without_native_delivered_permit_is_not_a_fence(h):
    launch_id, permit = h.launch()
    body = h.claim_body()
    assert h.callback("/claim", body, launch_id=launch_id).status_code == 403
    assert h.callback("/claim", body, launch_id=launch_id, capability="0" * 64).status_code == 403
    assert h.callback("/claim", body.model_copy(update={"identity": body.identity.model_copy(
        update={"session_id": "caller-session"})}), launch_id=launch_id, capability=permit).status_code == 403
    assert h.callback("/claim", body, launch_id=launch_id, capability=permit, identity="human").status_code == 403
    forged = {**body.model_dump(mode="json"), "launch_permit": permit, "epoch": 999}
    assert h.callback("/claim", forged, launch_id=launch_id, capability=permit).status_code == 422
    assert h.native.launch(launch_id).phase == "post_unknown"


def test_another_allowlisted_agent_cannot_use_this_jobs_private_permit(h):
    launch_id, permit = h.launch()
    headers = {
        "Authorization": "Bearer " + token(h.signing, {"oid": B.oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, remove=("scp",)),
        "Content-Type": "application/json", "X-Behavior-Launch-Permit": permit,
    }
    result = h.client.post(BASE + "/hosted/launches/" + launch_id + "/claim",
                           content=h.claim_body().model_dump_json(), headers=headers)
    assert result.status_code == 403 and h.native.launch(launch_id).phase == "post_unknown"


def test_permit_is_hash_only_durable_bound_one_use_and_not_in_ordinary_output(h, caplog):
    launch_id, permit = h.launch()
    native = h.native.launch(launch_id)
    assert native.permit_digest == hashlib.sha256(permit.encode()).hexdigest()
    assert native.target.session_id == h.target.session_id and native.target.agent_version == "7"
    body = h.claim_body()
    first = h.callback("/claim", body, launch_id=launch_id, capability=permit)
    assert first.status_code == 200, first.text
    assert first.json()["claim_state"] == "fresh"
    secret = first.headers["x-behavior-report-token"]
    assert "report_token" not in first.json() and permit not in first.text and secret not in first.text
    second = h.callback("/claim", body, launch_id=launch_id, capability=permit)
    assert second.status_code == 200 and second.json()["claim_state"] == "replay"
    assert "x-behavior-report-token" not in second.headers
    competing = h.callback("/claim", h.claim_body(process_id="other", request_id="other"),
                           launch_id=launch_id, capability=permit)
    assert competing.status_code == 403 and h.native.launch(launch_id).process_id == body.process_id
    for row in h.transport.backing.rows.values():
        assert permit.encode() not in row["Payload"] and secret.encode() not in row["Payload"]
    assert permit not in caplog.text and secret not in caplog.text
    assert permit.encode() not in h.workers.inspect("job-one").spec.input


def test_claim_current_epoch_and_policy_are_rechecked_after_native_post(h):
    launch_id, permit = h.launch()
    h.mutate(control_key("owner", "owner"), epoch=2)
    assert h.callback("/claim", h.claim_body(), launch_id=launch_id, capability=permit).status_code == 403
    assert h.native.launch(launch_id).phase == "post_unknown"
    assert h.native.launch(launch_id).target.session_id == "assigned-session"


def test_claim_lost_commit_ack_cannot_reissue_private_report_capability(h):
    launch_id, permit = h.launch()
    body = h.claim_body()
    h.transport.after_apply = "lost"
    result = h.callback("/claim", body, launch_id=launch_id, capability=permit)
    assert result.status_code == 503 and "x-behavior-report-token" not in result.headers
    h.transport.after_apply = None
    replay = h.callback("/claim", body, launch_id=launch_id, capability=permit)
    assert replay.status_code == 200 and replay.json()["claim_state"] == "replay"
    assert "x-behavior-report-token" not in replay.headers
    assert h.native.launch(launch_id).phase == "claimed"
    assert h.workers.inspect("job-one").state == "starting"


def test_cancel_unclaimed_permit_fences_every_late_start_without_provider_stop(h):
    launch_id, permit = h.launch()
    binding = h.workers.inspect("job-one").binding
    h.workers.cancel("job-one", CTX)
    evidence_id = h.native.fence_unclaimed(binding)
    assert h.native.verify(evidence_id).all_inflight_starts_fenced
    assert h.native.verify(evidence_id).state == "stopped"
    late = h.callback("/claim", h.claim_body(), launch_id=launch_id, capability=permit)
    assert late.status_code == 403
    h.workers.observe("job-one", evidence_id, CTX)
    assert h.workers.inspect("job-one").state == "cancelled"
    assert len(h.native_calls) == 1


def test_expired_unclaimed_fence_is_freshly_revalidated_without_provider_or_child_claim(h):
    launch_id, permit = h.launch()
    binding = h.workers.inspect("job-one").binding
    h.workers.cancel("job-one", CTX)
    original = h.native.fence_unclaimed(binding)
    original_key = native_key("evidence", original)
    original_record = h.store.read_snapshot((original_key,))[original_key]
    h.workers.observe("job-one", original, CTX)
    h.time = NOW + timedelta(minutes=2)
    with pytest.raises(Problem):
        h.native.verify(original)
    refreshed = h.native_executor.query(binding)
    assert refreshed != original
    proof = h.native.verify(refreshed)
    assert proof.state == "stopped" and proof.sequence > h.native.launch(launch_id).start.binding.generation
    assert json.loads(proof.proof)["no_provider_stop_claim"] is True
    assert h.native.launch(launch_id).native_invocation_id is None
    assert h.native.launch(launch_id).claim_id is None
    assert h.native.launch(launch_id).process_id is None
    assert h.native.launch(launch_id).report_token_digest is None
    assert h.native.launch(launch_id).child_pid is None
    assert proof.result is None and proof.checkpoint is None
    assert h.native.launch(launch_id).sealed_evidence_id == original
    assert h.store.read_snapshot((original_key,))[original_key] == original_record
    h.time += timedelta(minutes=2)
    another = h.native.reconcile_sealed(binding)
    assert another not in {original, refreshed}
    assert h.native.verify(another).sequence == proof.sequence + 1
    assert h.store.read_snapshot((original_key,))[original_key] == original_record
    assert len(h.native_calls) == 1
    assert h.callback("/claim", h.claim_body(), launch_id=launch_id, capability=permit).status_code == 403


@pytest.mark.parametrize("change", [
    {"claim_id": "unexpected-claim"}, {"process_id": "unexpected-process"},
    {"report_token_digest": "f" * 64}, {"child_pid": 999},
    {"native_invocation_id": "inv_unverified"}, {"phase": "post_unknown"},
])
def test_unclaimed_fence_revalidation_refuses_claim_child_or_reopened_gate(h, change):
    launch_id, _permit = h.launch()
    binding = h.workers.inspect("job-one").binding
    h.workers.cancel("job-one", CTX)
    original = h.native.fence_unclaimed(binding)
    h.mutate(native_key("launch", launch_id), **change)
    h.time = NOW + timedelta(minutes=2)
    with pytest.raises(Problem):
        h.native.reconcile_sealed(binding)
    assert h.native.launch(launch_id).sealed_evidence_id == original
    assert h.native.launch(launch_id).evidence_sequence == 1
    assert len(h.native_calls) == 1


def test_unclaimed_fence_revalidation_requires_unchanged_retained_gate_record(h):
    launch_id, _permit = h.launch()
    binding = h.workers.inspect("job-one").binding
    h.workers.cancel("job-one", CTX)
    h.native.fence_unclaimed(binding)
    key = native_key("unclaimed-fence", launch_id)
    entity = h.transport.backing.rows[(h.store._scope.table_name, h.store._scope.partition_key,
                                       "behavior:v1:row:" + key)]
    body = json.loads(entity["Payload"])
    body["data"]["claim_never_issued"] = False
    entity["Payload"] = json.dumps(body).encode()
    h.time = NOW + timedelta(minutes=2)
    with pytest.raises(Problem):
        h.native_executor.query(binding)
    assert len(h.native_calls) == 1


def test_fence_cannot_invent_child_exit_after_claim_wins(h):
    launch_id, permit = h.launch()
    assert h.callback("/claim", h.claim_body(), launch_id=launch_id, capability=permit).status_code == 200
    h.workers.cancel("job-one", CTX)
    with pytest.raises(Problem):
        h.native.fence_unclaimed(h.workers.inspect("job-one").binding)
    assert h.native.launch(launch_id).phase == "claimed"
    assert h.workers.inspect("job-one").state == "starting"

def test_concurrent_claimants_can_consume_only_one_process_bound_capability(h):
    launch_id, permit = h.launch()
    submit = h.store._submit
    race_id = native_key("claim", launch_id, "first")
    winners = []

    def racing(operations, request):
        if request is not None and request.request_id == race_id and not winners:
            winners.append(h.callback("/claim", h.claim_body(process_id="winner", request_id="second"),
                                      launch_id=launch_id, capability=permit))
        return submit(operations, request)

    h.store._submit = racing
    loser = h.callback("/claim", h.claim_body(process_id="loser", request_id="first"),
                       launch_id=launch_id, capability=permit)
    assert loser.status_code in (409, 412) and loser.json()["error"]["code"] == "BEHAVIOR_CONFLICT"
    assert "x-behavior-report-token" not in loser.headers
    assert winners[0].status_code == 200 and "x-behavior-report-token" in winners[0].headers
    assert h.native.launch(launch_id).process_id == "winner"


def test_unconfirmed_native_post_reservation_never_recreates_or_resends_permit(h):
    from shared_context.behavior_workers import worker_key

    start_id = worker_key("start", "job-one", "1")
    launch_id = native_key("launch-id", start_id)
    reserve_id = native_key("prepare", launch_id)
    submit = h.store._submit

    def lose_reservation(operations, request):
        if request is not None and request.request_id == reserve_id:
            h.transport.after_apply = "lost"
        try:
            return submit(operations, request)
        finally:
            h.transport.after_apply = None

    h.store._submit = lose_reservation
    with pytest.raises(Problem):
        h.runtime.controller.step("binding-one")
    launch = h.native.launch(launch_id)
    assert launch.phase == "post_unknown" and h.native_calls == []
    _, permit = h.native.prepare(launch.start)
    assert permit is None
    with pytest.raises(Problem):
        h.runtime.controller.step("binding-one")
    assert all(request.method == "GET" for request in h.native_calls)
    assert h.native.launch(launch_id).phase == "post_unknown"


@pytest.mark.parametrize("case", ["missing-probe", "expired-probe", "wrong-generation", "no-owned-exit", "cancel-accepted"])
def test_no_predecessor_release_without_fresh_bound_terminal_native_evidence(h, case):
    launch_id, permit = h.launch()
    claimed = h.callback("/claim", h.claim_body(), launch_id=launch_id, capability=permit)
    assignment = wire.HostedAssignment.model_validate_json(json.dumps(claimed.json()["assignment"]))
    report_token = claimed.headers["x-behavior-report-token"]
    probe = h.native.probe(h.workers.inspect("job-one").binding)
    body = wire.HostedReportBody(
        request_id="report", invocation_id=assignment.invocation_id, identity=assignment.identity,
        process_id=assignment.process_id, claim_id=assignment.claim_id,
        probe_id=probe.probe_id, process_generation=assignment.process_generation,
        binding_digest=assignment.binding_digest, supervisor_id=assignment.process_id,
        sequence=1, phase="sealed", outcome="stopped", child_exited=False,
    )
    probe_id = probe.probe_id
    if case == "missing-probe":
        probe_id = "0" * 64
    elif case == "expired-probe":
        h.time = NOW + timedelta(seconds=61)
    elif case == "wrong-generation":
        body = body.model_copy(update={"process_generation": assignment.process_generation + 1})
    elif case == "cancel-accepted":
        body = body.model_copy(update={"phase": "running"})
    result = h.callback("/probes/" + probe_id + "/report", body, launch_id=launch_id,
                        capability=report_token, report=True)
    assert result.status_code == (200 if case == "cancel-accepted" else 403 if case != "missing-probe" else 409)
    if case == "cancel-accepted":
        assert result.json()["state"] == "running"
    target_key = native_key("target", h.target.executor_id)
    assert h.store.read_snapshot((target_key,))[target_key].payload["data"]["active_launch_id"] == launch_id
    assert h.native.launch(launch_id).phase == "claimed"


def test_actual_native_path_local_child_and_fresh_probe_complete_without_exposing_controller(h, tmp_path):
    supervisor = ChecksumSupervisor(tmp_path.resolve())
    local = {}

    def api_transport(request):
        response = h.client.post(request.url.path, content=request.content, headers=dict(request.headers))
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    api = BehaviorExecutorClient(
        "https://api.example", h.verifier.auth.audience,
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token=h.headers("agent")["Authorization"].removeprefix("Bearer "))),
        transport=httpx.MockTransport(api_transport),
    )

    def native_transport(request):
        invocation = h.actual_invocation_id
        response_headers = {"x-agent-invocation-id": invocation, "x-agent-session-id": h.target.session_id}
        if request.method == "POST" and request.url.path.endswith("/invocations"):
            posted = json.loads(request.content)
            claim = api.claim("binding-one", posted["launch_id"], wire.HostedClaimRequest(
                **h.claim_body(process_id=supervisor.supervisor_id).model_dump(),
                launch_permit=SecretStr(request.headers[wire.NATIVE_LAUNCH_PERMIT_HEADER]),
            ))
            assert claim.claim_state == "fresh" and claim.report_token is not None
            assignment = claim.assignment
            local.update(launch_id=assignment.launch_id, claim_id=assignment.claim_id,
                         report_token=claim.report_token.get_secret_value(), assignment=assignment)
            supervisor.start(LocalAssignment.model_validate(assignment.model_dump()))
            return httpx.Response(202, headers=response_headers, json={
                "status": "accepted", "launch_id": assignment.launch_id, "invocation_id": assignment.invocation_id,
            })
        assert request.headers["x-agent-invocation-id"] == invocation
        assert "/invocations/" + invocation in request.url.path
        probe_id = request.headers[wire.NATIVE_PROBE_HEADER]
        assigned = local["assignment"]
        probe_body = wire.HostedProbeBody(
            **h.claim_body(process_id=supervisor.supervisor_id, request_id="probe").model_dump(),
            claim_id=assigned.claim_id,
        )
        resolved = api.resolve_probe("binding-one", assigned.launch_id, probe_id, wire.HostedProbeRequest(
            **probe_body.model_dump(), report_token=SecretStr(local["report_token"]),
        ))
        assert resolved.probe_id == probe_id
        current = supervisor.observe(assigned.launch_id)
        report = wire.HostedReportBody(
            **probe_body.model_dump(), probe_id=probe_id, process_generation=assigned.process_generation,
            binding_digest=assigned.binding_digest, supervisor_id=supervisor.supervisor_id,
            sequence=current.sequence, phase=current.phase, child_pid=current.child_pid,
            outcome=current.outcome, checkpoint=current.checkpoint, exit_code=current.exit_code,
            child_exited=current.child_exited,
            result=None if current.result is None else wire.ChecksumResult(**current.result.model_dump()),
        )
        reported = api.report("binding-one", assigned.launch_id, probe_id, wire.HostedReport(
            **report.model_dump(), report_token=SecretStr(local["report_token"]),
        ))
        return httpx.Response(200, headers=response_headers, content=reported.model_dump_json())

    h.native_handler = native_transport
    try:
        try:
            started = h.runtime.controller.step("binding-one")
        except Problem:
            error = getattr(h, "native_error", None)
            if isinstance(error, Problem):
                pytest.fail(error.body["error"]["code"])
            if hasattr(error, "errors"):
                pytest.fail(str(error.errors(include_input=False)))
            pytest.fail(type(error).__name__)
        assert started.state == "starting"
        launch_id = local["launch_id"]
        assert h.native.launch(launch_id).phase == "claimed"
        target_key = native_key("target", h.target.executor_id)
        assert h.store.read_snapshot((target_key,))[target_key].payload["data"]["active_launch_id"] == launch_id
        deadline = time.monotonic() + 10
        while supervisor.observe(launch_id).phase != "sealed" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert supervisor.observe(launch_id).phase == "sealed"
        result = h.runtime.controller.step("binding-one")
        assert result.state == "succeeded" and result.result_forwarded
        assert h.native.launch(launch_id).phase == "sealed"
        pull = h.post("/delivery/pull", wire.Command(request_id="native-pull"))
        assert pull.status_code == 200
        assert local["report_token"] not in pull.text
        assert sum(request.method == "POST" and request.url.path.endswith("/invocations") for request in h.native_calls) == 1
    finally:
        api.close()
        for child in supervisor.children.values():
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
