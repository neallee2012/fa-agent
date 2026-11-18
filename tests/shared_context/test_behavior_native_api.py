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
    FoundryExecutor, FoundryHttpClient, FoundryTarget, NativeJobRoute, NativeLaunchAuthority, native_key,
)
from shared_context.behavior_hosted import BehaviorExecutorClient, ChecksumSupervisor, LocalAssignment
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime import BehaviorRuntime, RuntimeController
from shared_context.errors import Problem
from legacy_submit_rows_helpers import load_store
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
        # The stored rows are what /work/submit and then the native-grant seed left; the seed took serial 2.
        load_store(self.transport.backing, "runtime-native-submitted")
        self.serial += 1

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
