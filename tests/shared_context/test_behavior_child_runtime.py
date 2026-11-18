"""Controlled C1 runtime composition over local SDK/HTTP transports, not Azure proof."""

import json
import hashlib
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from shared_context import behavior_wire as wire
from shared_context.behavior_foundry import NativeExecutionGrant, native_key
from shared_context.behavior_hosted import BehaviorExecutorClient, ChecksumSupervisor, HostedInvocation, HostedRuntime
from shared_context.behavior_server import application, bootstrap
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
