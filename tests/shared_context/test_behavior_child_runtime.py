"""Controlled C1 runtime composition over local SDK/HTTP transports, not Azure proof."""

import json
import base64
import hashlib
import time
from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from shared_context import behavior_wire as wire
from shared_context.behavior_foundry import NativeExecutionGrant, native_key
from shared_context.behavior_hosted import BehaviorExecutorClient, ChecksumSupervisor, HostedInvocation, HostedRuntime
from shared_context.behavior_server import application, bootstrap, run_controller
from shared_context.behavior_server_config import BootstrapAuthorization
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite, InlineMaterial
from shared_context.behavior_workers import worker_key
from shared_context.behavior_runtime import RuntimeController
from shared_context.errors import Problem
from test_behavior_server import Harness, NOW, workspace
from test_m3_auth import jwk_client, signing_key, token

from shared_context.behavior_server_config import ServerConfiguration, synthetic_configuration
from shared_context.source_materials import digest


def c1_config_data():
    value = synthetic_configuration().model_dump(mode="json")
    value["workers"]["capacity"] = 1
    value["bindings"] = value["bindings"][:1]
    value["policies"] = value["policies"][:1]
    value["operations"] = value["operations"][:1]
    binding = value["bindings"][0]
    binding["child_jobs"] = [{
        "job_id": "child-job-1",
        "work": {**binding["work"], "work_id": "child-work-1", "goal": "Complete the separately authorized child checksum."},
    }]
    value["policies"][0]["policy"]["binding_digest"] = digest(["fa-behavior-api-v1", "native", binding])
    return value


def test_c1_configuration_registers_separate_child_work_without_child_authority_input():
    config = ServerConfiguration.model_validate_json(json.dumps(c1_config_data()))
    assert config.workers.capacity == 1
    assert len(config.targets) == 4 and len(config.bindings) == 1
    child = config.bindings[0].child_jobs[0]
    assert child.job_id == "child-job-1"
    assert child.work.work_id != config.bindings[0].work.work_id
    assert set(child.model_dump()) == {"job_id", "work"}


def test_deployed_header_configuration_digest_and_empty_child_serialization_remain_unchanged():
    path = Path(__file__).resolve().parents[2] / ".azure" / "behavior-native-config-header" / "config.json"
    if not path.is_file():
        pytest.skip("operator-owned deployed configuration is not present in this checkout")
    original = path.read_bytes()
    config = ServerConfiguration.model_validate_json(original)
    assert config.sha256 == "97604ac577b08601885c1d867c8619b9305f5c9de5ed35c879b9af608733b8e1"
    for binding in config.bindings:
        assert "child_jobs" not in binding.model_dump()
        assert "child_jobs" not in binding.model_dump(mode="json")
    explicit = config.model_dump(mode="json")
    for binding in explicit["bindings"]:
        binding["child_jobs"] = []
    same = ServerConfiguration.model_validate_json(json.dumps(explicit))
    assert same.model_dump() == config.model_dump()
    assert same.sha256 == config.sha256
    assert path.read_bytes() == original


class C1Harness(Harness):
    def __init__(self, workspace, key_client, signing_key):
        super().__init__(workspace, key_client, configuration_data=c1_config_data())
        self.signing_key, self.native_error = signing_key, None
        self.activate()
        self.client = TestClient(application(self.composition))
        self.probe_directives = []
        self.before_claim, self.fail_observe = None, False
        target = self.config.targets[0]
        agent_token = token(signing_key, {"oid": target.agent_oid, "roles": ["Behavior.Agent"], "idtyp": "app"}, remove=("scp",))

        def api_transport(request):
            if request.url.path.endswith("/claim") and self.before_claim is not None:
                callback, self.before_claim = self.before_claim, None
                callback()
            result = self.client.post(request.url.path, content=request.content, headers=dict(request.headers))
            if request.url.path.endswith("/resolve") and result.status_code == 200:
                self.probe_directives.append(result.json()["directive"])
            return httpx.Response(result.status_code, headers=result.headers, content=result.content)

        self.api = BehaviorExecutorClient(
            "https://api.example", self.config.identity.audience,
            SimpleNamespace(get_token=lambda _: SimpleNamespace(token=agent_token)),
            transport=httpx.MockTransport(api_transport),
        )
        self.supervisor = ChecksumSupervisor(workspace / "hosted")
        self.hosted = HostedRuntime(wire.HostedIdentity(**{
            key: getattr(target, key) for key in ("agent_id", "agent_name", "agent_version", "session_id")
        }), self.supervisor, self.api, clock=lambda: self.time)

    def authorization(self):
        binding = self.config.bindings[0]
        jobs = ((binding.job_id, binding.work), *((child.job_id, child.work) for child in binding.child_jobs))
        grants = []
        for job_id, work in jobs:
            body = wire.ExecutorJobInput(
                job_id=job_id, work_id=work.work_id,
                definition_digest=digest(work.model_dump(mode="json")), required_checks=work.required_checks,
            ).model_dump_json().encode()
            grants.append(NativeExecutionGrant(
                grant_id=native_key("bootstrap-grant", job_id), controller_id=self.config.controller_id,
                owner=binding.owner, policy_revision=1, job_id=job_id, definition=work,
                input_digest=hashlib.sha256(body).hexdigest(), targets=self.config.targets[:1],
                active=True, remaining_uses=4, not_before=NOW, expires_at=NOW + timedelta(hours=1),
                checkpoint_after_bytes=64 if job_id == binding.job_id else None,
            ))
        return BootstrapAuthorization(
            decision_id="c1-exact-approval", authorizing_human_oid=self.config.identity.human_oid,
            configuration_sha256=self.config.sha256, purpose="authorized-six-gap-validation",
            not_before=NOW, expires_at=NOW + timedelta(hours=1), native_grants=tuple(grants),
        )

    def native(self, request):
        self.calls.append(request)
        target = self.config.targets[0]
        start = request.method == "POST" and request.url.path.endswith("/invocations")
        invocation = "inv_" + hashlib.sha256(request.content).hexdigest()[:48] if start else request.headers["x-agent-invocation-id"]
        headers = {"x-agent-invocation-id": invocation, "x-agent-session-id": target.session_id}
        try:
            if not start and self.fail_observe:
                raise httpx.ReadTimeout("OFFLINE unknown native park; keep the slot", request=request)
            if start:
                result = self.hosted.start(HostedInvocation(
                    **json.loads(request.content), launch_permit=SecretStr(request.headers[wire.NATIVE_LAUNCH_PERMIT_HEADER]),
                ), invocation, target.session_id)
            else:
                result = self.hosted.observe(invocation, target.session_id, request.headers[wire.NATIVE_PROBE_HEADER],
                                             cancellation=request.url.path.endswith("/cancel")).model_dump(mode="json")
            return httpx.Response(202 if start else 200, json=result, headers=headers)
        except Exception as error:
            self.native_error = error
            raise

    def post(self, path, body):
        return self.client.post("/behavior/v1/bindings/binding-1" + path, json=body,
                                headers=self.headers(self.signing_key))

    def status(self, path="/jobs/job-1"):
        return self.client.get("/behavior/v1/bindings/binding-1" + path, headers=self.headers(self.signing_key))

    def wait_child_exit(self, job_id):
        job = self.runtime.dep.workers.inspect(job_id)
        launch_id = native_key("launch-id", job.binding.start_request_id)
        self.supervisor.children[launch_id].wait(timeout=15)
        return launch_id

    def mutate_job(self, job_id, **updates):
        key = worker_key("job", job_id)
        row = self.runtime.store.read_snapshot((key,))[key].payload
        request = ControlRequest(request_id="test-mutate-" + job_id, request_digest=digest(updates), read_set=(key,))
        self.runtime.store.transact(request, lambda _: ControlChanges(writes=(ControlWrite(
            row_id=key, mode="replace", data={**row["data"], **updates},
            material=InlineMaterial.model_validate_json(json.dumps(row["material"])),
        ),), result={"offline_test_mutation": True}))

    def close(self):
        for child in self.supervisor.children.values():
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
        self.api.close()
        self.client.close()
        self.composition.close()


@pytest.fixture
def c1(workspace, jwk_client, signing_key):
    h = C1Harness(workspace, jwk_client, signing_key)
    try:
        yield h
    finally:
        h.close()


def test_authenticated_child_enqueue_requires_started_parent_and_replays_one_registered_job(c1):
    assert c1.post("/work/submit", {"request_id": "root-submit"}).status_code == 200
    assert c1.post("/children/child-job-1/enqueue", {"request_id": "child-enqueue"}).status_code == 409
    assert c1.runtime.controller.step("binding-1").state == "starting"
    first = c1.post("/children/child-job-1/enqueue", {"request_id": "child-enqueue"})
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "queued"
    assert first.json()["parent_job_id"] == "job-1"
    assert first.json()["has_delivery_obligation"] is False
    assert c1.post("/children/child-job-1/enqueue", {"request_id": "child-enqueue"}).json() == first.json()
    assert c1.status("/children/child-job-1").status_code == 200
    assert c1.runtime.dep.workers.inspect("job-1").children == ("child-job-1",)
    assert len(c1.calls) == 1


def test_native_style_c1_parent_park_child_completion_exact_checkpoint_resume_and_delivery(c1):
    workers = c1.runtime.dep.workers
    assert c1.post("/work/submit", {"request_id": "root-submit"}).status_code == 200
    assert c1.runtime.controller.step("binding-1").state == "starting"
    assert c1.post("/children/child-job-1/enqueue", {"request_id": "child-enqueue"}).status_code == 200
    first_launch = c1.wait_child_exit("job-1")
    with pytest.raises(Problem) as full:
        workers.start("child-job-1", c1.config.bindings[0].owner)
    assert full.value.body["error"]["code"] == "WORKER_CAPACITY_FULL"
    parked = c1.runtime.controller.park("binding-1")
    assert parked.state == "waiting_dependency"
    checkpoint = workers.inspect("job-1").checkpoint
    assert checkpoint is not None
    assert c1.status().json()["obligation_fulfilled"] is False
    c1.runtime.controller.drain(limit=4)
    assert c1.status("/children/child-job-1").json()["state"] == "starting"
    child_launch = c1.wait_child_exit("child-job-1")
    c1.runtime.controller.drain(limit=4)
    assert c1.status("/children/child-job-1").json()["state"] == "succeeded"
    assert c1.status().json()["state"] == "waiting_dependency"
    assert c1.status().json()["obligation_fulfilled"] is False
    assert workers.inspect("child-job-1").spec.obligation_id is None
    assert "child-job-1" not in workers.completions()
    with pytest.raises(Problem):
        workers.observe("job-1", workers.inspect("child-job-1").last_evidence_id, c1.config.bindings[0].owner)
    with pytest.raises(Problem):
        workers.forward_result("child-job-1", c1.config.bindings[0].owner, c1.runtime.dep.delivery)
    c1.runtime.controller.drain(limit=4)
    parent = workers.inspect("job-1")
    assert parent.state == "starting" and parent.generation == 2
    resumed_launch = c1.runtime.dep.native_launches.launch(native_key("launch-id", parent.binding.start_request_id))
    assert resumed_launch.start.checkpoint == checkpoint
    assert parent.binding.checkpoint_digest == hashlib.sha256(checkpoint).hexdigest()
    assert c1.supervisor.journal.assignment(resumed_launch.launch_id).previous_launch_id == first_launch
    assert resumed_launch.launch_id not in {first_launch, child_launch}
    c1.wait_child_exit("job-1")
    assert c1.runtime.controller.step("binding-1").state == "succeeded"
    pull = c1.post("/delivery/pull", {"request_id": "root-pull"})
    assert pull.status_code == 200, pull.text
    delivery = wire.DeliveryBinding.model_validate_json(base64.urlsafe_b64decode(pull.headers["x-behavior-binding"]))
    acknowledged = c1.post("/delivery/acknowledge", {
        "request_id": "root-ack", "exchange_id": pull.headers["x-behavior-exchange"],
        "acknowledgment_token": pull.headers["x-behavior-ack-token"], "binding": delivery.model_dump(mode="json"),
    })
    assert acknowledged.status_code == 200
    assert c1.status().json()["obligation_fulfilled"] is True
    starts = [call for call in c1.calls if call.method == "POST" and call.url.path.endswith("/invocations")]
    assert len(starts) == 3


def test_controller_tick_requests_registered_parent_park_through_bounded_query_lane(c1):
    c1.post("/work/submit", {"request_id": "root-submit"})
    c1.runtime.controller.step("binding-1")
    c1.post("/children/child-job-1/enqueue", {"request_id": "child-enqueue"})
    c1.wait_child_exit("job-1")
    result = c1.runtime.controller.drain(limit=1)
    assert result[0]["status"] == "waiting_dependency"
    assert c1.probe_directives == ["park"]
    assert c1.runtime.dep.workers.inspect("job-1").queries_reserved == 1
    assert c1.status("/children/child-job-1").json()["state"] == "queued"


@pytest.mark.parametrize("extra", [
    {"owner": {}}, {"parent_job_id": "child-job-1"}, {"script": "run"},
    {"grant_id": "caller-grant"}, {"clock": "2026-09-16T00:00:00Z"}, {"input": "arbitrary"},
])
def test_child_http_cannot_supply_execution_authority_or_raw_work(c1, extra):
    assert c1.post("/children/child-job-1/enqueue", {"request_id": "bad", **extra}).status_code == 422
    assert not c1.calls


def test_unknown_child_root_selector_and_foreign_actor_are_denied(c1):
    assert c1.post("/children/unknown-child/enqueue", {"request_id": "bad"}).status_code == 403
    assert c1.client.post("/behavior/v1/bindings/binding-1/children/unknown-child/enqueue",
                         content=b"x" * 100000, headers=c1.headers(c1.signing_key)).status_code == 403
    assert c1.status("/jobs/child-job-1").status_code == 403
    agent = c1.config.identity.agents[1]
    headers = {"Authorization": "Bearer " + token(c1.signing_key, {
        "oid": agent.oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",))}
    assert c1.client.post("/behavior/v1/bindings/binding-1/children/child-job-1/enqueue",
                         json={"request_id": "bad"}, headers=headers).status_code == 403
    with pytest.raises(Problem):
        c1.runtime._controller_park("binding-1", RuntimeController(c1.runtime, c1.config.controller_id))
    assert not c1.calls


@pytest.mark.parametrize("failure", ["lost-ack", "no-commit"])
def test_child_enqueue_unknown_reconciles_exact_core_egt_without_resending(c1, failure):
    c1.post("/work/submit", {"request_id": "root-submit"})
    c1.runtime.controller.step("binding-1")
    original_submit = c1.runtime.store._submit
    request_id = worker_key("enqueue", "child-job-1")
    observed = []

    def fault(operations, request):
        if request is not None and request.request_id == request_id:
            observed.append(request)
            if failure == "lost-ack":
                c1.table.after_apply = "lost"
            else:
                c1.table.fail_post = True
            try:
                return original_submit(operations, request)
            finally:
                c1.table.after_apply = None
                c1.table.fail_post = False
        return original_submit(operations, request)

    c1.runtime.store._submit = fault
    first = c1.post("/children/child-job-1/enqueue", {"request_id": "one-child"})
    assert first.status_code == 503 and first.json()["error"]["automatic_retry"] is False
    assert len(observed) == 1
    owner = c1.config.bindings[0].owner
    assert set(observed[0].read_set) == {
        worker_key("pool", c1.config.workers.workspace_id), worker_key("job", "child-job-1"),
        worker_key("job", "job-1"), *c1.runtime.dep.workers._owner_keys(owner),
    }
    count = len(c1.table.parts)
    repeated = c1.post("/children/child-job-1/enqueue", {"request_id": "one-child"})
    reconciled = c1.post("/children/child-job-1/reconcile", {"request_id": "one-child"})
    assert repeated.status_code == reconciled.status_code == (200 if failure == "lost-ack" else 503)
    assert len(observed) == 1 and len(c1.table.parts) == count
    assert c1.runtime.dep.workers.inspect("job-1").children == (("child-job-1",) if failure == "lost-ack" else ())
    assert len(c1.calls) == 1


def test_lost_native_park_and_expired_lease_never_start_child_or_release_parent_slot(c1):
    c1.post("/work/submit", {"request_id": "root-submit"})
    c1.runtime.controller.step("binding-1")
    c1.post("/children/child-job-1/enqueue", {"request_id": "child-enqueue"})
    c1.wait_child_exit("job-1")
    c1.time += timedelta(seconds=c1.config.workers.lease_seconds + 1)
    c1.fail_observe = True
    result = c1.runtime.controller.drain(limit=1)
    assert result[0]["problem"] == "WORKER_QUERY_UNKNOWN"
    with pytest.raises(Problem) as full:
        c1.runtime.dep.workers.start("child-job-1", c1.config.bindings[0].owner)
    assert full.value.body["error"]["code"] == "WORKER_CAPACITY_FULL"
    assert c1.status("/children/child-job-1").json()["generation"] == 0
    assert len([call for call in c1.calls if call.method == "POST"]) == 1
    c1.fail_observe = False
    assert c1.runtime.controller.drain(limit=1)[0]["status"] == "waiting_dependency"
    c1.runtime.controller.drain(limit=1)
    assert c1.status("/children/child-job-1").json()["state"] == "starting"


@pytest.mark.parametrize("tamper", ["parent", "work", "owner"])
def test_native_callback_rejects_registered_child_with_mismatched_saved_job(c1, tamper):
    c1.post("/work/submit", {"request_id": "root-submit"})
    c1.runtime.controller.step("binding-1")
    c1.post("/children/child-job-1/enqueue", {"request_id": "child-enqueue"})
    c1.wait_child_exit("job-1")
    c1.runtime.controller.park("binding-1")

    def corrupt():
        job = c1.runtime.dep.workers.inspect("child-job-1")
        if tamper == "owner":
            c1.mutate_job("child-job-1", owner={**job.owner.model_dump(mode="json"),
                                               "principal_id": c1.config.identity.agents[1].oid})
        else:
            field, value = ("parent_job_id", "unregistered-root") if tamper == "parent" else ("work_id", "work-1")
            c1.mutate_job("child-job-1", spec={**job.spec.model_dump(mode="json"), field: value})

    c1.before_claim = corrupt
    result = c1.runtime.controller.drain(limit=1)
    assert result[0]["problem"] == "WORKER_START_UNKNOWN"
    job = c1.runtime.dep.workers.inspect("child-job-1")
    launch = c1.runtime.dep.native_launches.launch(native_key("launch-id", job.binding.start_request_id))
    assert launch.claim_id is None and launch.native_invocation_id is None
    assert launch.launch_id not in c1.supervisor.children


@pytest.mark.parametrize("change", [
    {"job_id": "job-1"}, {"work": None}, {"parent_job_id": "child-job-1"},
    {"owner": {}}, {"child_jobs": []},
])
def test_child_configuration_rejects_cycles_and_authority_fields(change):
    raw = c1_config_data()
    raw["bindings"][0]["child_jobs"][0].update(change)
    with pytest.raises(ValueError):
        ServerConfiguration.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("change", [
    {"input_digest": "f" * 64},
    {"targets": ()},
    {"remaining_uses": 8},
])
def test_child_bootstrap_grant_is_exact_and_not_a_root_or_unlimited_authorization(c1, change):
    original = c1.approval.native_grants[1]
    grant = original.model_copy(update=change)
    approval = c1.approval.model_copy(update={"native_grants": (c1.approval.native_grants[0], grant)})
    count = len(c1.table.parts)
    with pytest.raises((Problem, ValueError)):
        bootstrap(c1.composition, approval, stage="records")
    assert len(c1.table.parts) == count
    assert c1.runtime.dep.native_launches.jobs["child-job-1"].api_binding_id == "binding-1"
    key = native_key("grant", original.grant_id)
    saved = NativeExecutionGrant.model_validate_json(json.dumps(c1.runtime.store.read_snapshot((key,))[key].payload["data"]))
    assert saved == original and saved.definition.work_id == "child-work-1"
    assert saved.owner == c1.config.bindings[0].owner and len(saved.targets) == 1
    assert c1.approval.native_grants[0].checkpoint_after_bytes == 64


@pytest.mark.parametrize("index,change", [
    (0, {"checkpoint_after_bytes": None}), (0, {"remaining_uses": 1}),
    (1, {"checkpoint_after_bytes": 64}),
])
def test_c1_grants_require_parent_checkpoint_resume_and_nonparking_leaf(c1, index, change):
    grants = list(c1.approval.native_grants)
    grants[index] = grants[index].model_copy(update=change)
    approval = c1.approval.model_copy(update={"native_grants": tuple(grants)})
    with pytest.raises(Problem):
        bootstrap(c1.composition, approval, stage="records")

def test_c1_manual_foreground_controller_uses_same_runtime_tick_without_request_daemon(c1):
    c1.post("/work/submit", {"request_id": "root-submit"})
    c1.runtime.controller.step("binding-1")
    c1.post("/children/child-job-1/enqueue", {"request_id": "child-enqueue"})
    c1.wait_child_exit("job-1")
    result = run_controller(c1.composition)
    assert result["ticks"] == 1 and c1.composition.closed
    assert c1.probe_directives == ["park"]
    assert c1.runtime.dep.workers.inspect("job-1").state == "waiting_dependency"
    assert c1.runtime.dep.workers.inspect("child-job-1").state == "queued"


def test_parent_park_respects_finite_worker_query_budget_without_native_dispatch(c1):
    c1.post("/work/submit", {"request_id": "root-submit"})
    c1.runtime.controller.step("binding-1")
    c1.post("/children/child-job-1/enqueue", {"request_id": "child-enqueue"})
    c1.wait_child_exit("job-1")
    c1.mutate_job("job-1", queries_reserved=c1.config.workers.queries_per_generation)
    before = len(c1.calls)
    result = c1.runtime.controller.drain(limit=1)
    assert result[0]["problem"] == "WORKER_QUERY_EXHAUSTED"
    assert len(c1.calls) == before
    assert c1.runtime.dep.workers.inspect("child-job-1").generation == 0


def test_c4_minimum_and_cross_binding_child_identity_remain_strict():
    c1 = c1_config_data()
    c1["workers"]["capacity"] = 4
    with pytest.raises(ValueError):
        ServerConfiguration.model_validate_json(json.dumps(c1))
    duplicate = synthetic_configuration().model_dump(mode="json")
    duplicate["bindings"][0]["child_jobs"] = [{
        "job_id": duplicate["bindings"][1]["job_id"],
        "work": {**duplicate["bindings"][0]["work"], "work_id": "separate-work"},
    }]
    with pytest.raises(ValueError):
        ServerConfiguration.model_validate_json(json.dumps(duplicate))
