"""Offline protocol serialization/local transport tests; native NOT_RUN."""

import json
import hashlib
import time
import multiprocessing
import os
from datetime import timedelta
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from shared_context.behavior_foundry import FoundryHttpClient, FoundryTarget, FOUNDRY_TOKEN_SCOPE
from shared_context.errors import Problem
from shared_context import behavior_wire as wire
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_delivery import VerifiedWorkResult
from shared_context.behavior_foundry import (
    FoundryExecutor, NativeExecutionGrant, NativeJobRoute, NativeLaunchAuthority, controller_process_entry, native_key,
)
from shared_context.behavior_hosted import BehaviorExecutorClient, ChecksumSupervisor, HostedInvocation, HostedRuntime
from shared_context.behavior_workers import JobSpec, WorkerContract, WorkerCore
from shared_context.behavior_store import BehaviorStore
from test_behavior_delivery import CONTEXT, NOW
from test_behavior_workers import SQLiteBacking, WorkerHarness, join
from test_behavior_store import FakeTable, SCOPE
from test_behavior_store import code

TARGET = FoundryTarget(
    executor_id="configured-executor-one", project_endpoint="https://synthetic.services.ai.azure.com/api/projects/project",
    agent_id="11111111-1111-1111-1111-111111111111", agent_name="checksum-agent", agent_version="7",
    session_id="existing-session-no-create",
    agent_oid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
)
INVOCATION = "a" * 64
ACTUAL_INVOCATION = "inv_provider_generated_0648c3c2"
PROBE = "b" * 64
PERMIT = SecretStr("c" * 64)


class Credential:
    def get_token(self, scope):
        assert scope == FOUNDRY_TOKEN_SCOPE
        return SimpleNamespace(token="synthetic-test-token-never-persist")


def response(request, status=202, **changes):
    invocation = ACTUAL_INVOCATION if request.url.path.endswith("/invocations") else INVOCATION
    headers = {"x-agent-invocation-id": invocation, "x-agent-session-id": TARGET.session_id, **changes}
    body = {"status": "pending"}
    if request.url.path.endswith("/invocations"):
        body.update(launch_id=json.loads(request.content)["launch_id"], invocation_id=invocation)
    return httpx.Response(status, headers=headers, json=body, request=request)


def test_native_client_sends_only_original_launch_id_and_fixed_existing_session_protocol():
    calls = []

    def transport(request):
        calls.append(request)
        return response(request)

    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(transport))
    result = client.start(INVOCATION, "server-launch", "binding-one", PERMIT)
    request = calls[0]
    assert request.method == "POST"
    assert request.url.path == "/api/projects/project/agents/checksum-agent/endpoint/protocols/invocations"
    assert dict(request.url.params) == {"api-version": "v1", "agent_session_id": TARGET.session_id}
    assert request.headers["x-agent-invocation-id"] == INVOCATION
    assert json.loads(request.content) == {
        "binding_id": "binding-one", "launch_id": "server-launch",
    }
    assert request.headers[wire.NATIVE_LAUNCH_PERMIT_HEADER] == PERMIT.get_secret_value()
    assert "X-Behavior-Launch-Permit" not in request.headers
    assert PERMIT.get_secret_value().encode() not in request.content
    assert result.status_code == 202 and "terminal" not in result.body.decode()
    assert result.invocation_id == ACTUAL_INVOCATION != INVOCATION
    assert len(calls) == 1
    client.close()


def test_observe_and_cancel_use_original_invocation_and_server_probe():
    calls = []
    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(
        lambda request: calls.append(request) or response(request),
    ))
    client.observe(INVOCATION, PROBE)
    client.cancel(INVOCATION, PROBE)
    assert calls[0].method == "GET" and calls[0].url.path.endswith("/invocations/" + INVOCATION)
    assert calls[1].method == "POST" and calls[1].url.path.endswith("/invocations/" + INVOCATION + "/cancel")
    assert all(dict(call.url.params) == {"api-version": "v1"} for call in calls)
    assert all(call.headers[wire.NATIVE_PROBE_HEADER] == PROBE for call in calls)
    assert calls[1].content == b"{}"
    client.close()


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 404, 429, 500, 503])
def test_errors_and_redirects_are_not_retried_and_never_expose_body(status):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://untrusted.example/"},
                              content=b"synthetic-secret-private-trace", request=request)

    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(transport))
    with pytest.raises(Problem) as error:
        client.start(INVOCATION, "launch", "binding-one", PERMIT)
    assert code(error) == "FOUNDRY_INVOCATION_UNKNOWN"
    assert "synthetic-secret" not in str(error.value.body) and len(calls) == 1
    client.close()


def test_lost_start_ack_does_not_authorize_an_automatic_second_post():
    calls = []

    def transport(request):
        calls.append(request)
        raise httpx.ReadTimeout("must not expose private response context", request=request)

    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(transport))
    with pytest.raises(Problem) as unknown:
        client.start(INVOCATION, "launch", "binding-one", PERMIT)
    assert code(unknown) == "FOUNDRY_INVOCATION_UNKNOWN" and len(calls) == 1
    assert "private response" not in str(unknown.value.body)
    client.close()


@pytest.mark.parametrize("header", ["x-agent-invocation-id", "x-agent-session-id"])
def test_wrong_echoed_native_binding_is_not_a_receipt(header):
    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(
        lambda request: response(request, **{header: "wrong"}),
    ))
    with pytest.raises(Problem) as error:
        client.start(INVOCATION, "launch", "binding-one", PERMIT)
    assert code(error) == "FOUNDRY_NATIVE_BINDING_MISMATCH"
    client.close()


@pytest.mark.parametrize("case", ["missing-id", "empty-id", "oversized-id", "bad-id", "missing-launch", "wrong-launch", "body-id"])
def test_start_requires_bounded_provider_id_and_exact_returned_launch(case):
    calls = []

    def transport(request):
        calls.append(request)
        header_id = ACTUAL_INVOCATION
        body = {"launch_id": "launch", "invocation_id": ACTUAL_INVOCATION, "status": "accepted"}
        if case in {"empty-id", "oversized-id", "bad-id"}:
            header_id = {"empty-id": "", "oversized-id": "x" * 65, "bad-id": "inv/other"}[case]
            body["invocation_id"] = header_id
        if case == "missing-launch":
            body.pop("launch_id")
        if case == "wrong-launch":
            body["launch_id"] = "another-launch"
        if case == "body-id":
            body["invocation_id"] = "inv_other"
        headers = {"x-agent-session-id": TARGET.session_id}
        if case != "missing-id":
            headers["x-agent-invocation-id"] = header_id
        return httpx.Response(202, headers=headers, json=body)

    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(Problem) as failure:
            client.start(INVOCATION, "launch", "binding-one", PERMIT)
        assert code(failure) == "FOUNDRY_NATIVE_BINDING_MISMATCH" and len(calls) == 1
    finally:
        client.close()


def test_default_has_no_credentials_or_implicit_native_activation():
    client = FoundryHttpClient(TARGET, transport=httpx.MockTransport(lambda _: pytest.fail("must not send")))
    with pytest.raises(Problem) as error:
        client.start(INVOCATION, "launch", "binding-one", PERMIT)
    assert code(error) == "FOUNDRY_CREDENTIAL_DISABLED"
    client.close()


@pytest.mark.parametrize("change", [
    {"project_endpoint": "http://synthetic/api/projects/project"},
    {"project_endpoint": "https://user:secret@synthetic/api/projects/project"},
    {"project_endpoint": "https://synthetic/api/projects/project?session=implicit"},
    {"agent_version": "latest"}, {"session_id": "../create"}, {"agent_id": "not-a-guid"},
])
def test_native_configuration_requires_exact_preprovisioned_bindings(change):
    with pytest.raises(ValidationError):
        FoundryTarget(**{**TARGET.model_dump(), **change})


class ApiCredential:
    def get_token(self, scope):
        assert scope == "api://22222222-2222-2222-2222-222222222222/.default"
        return SimpleNamespace(token="synthetic-mi-token")


class NativeHarness(WorkerHarness):
    """Concrete clients, server authority and real children over OFFLINE HTTP/Table transports."""

    def __init__(self, root, capacity=1, count=1, park_first=False, enqueue_all=True):
        super().__init__(capacity=capacity)
        self.native_calls, self.api_calls, self.claim_requests, self.report_requests = [], [], [], []
        self.drop_start_response = False
        self.hold_start = False
        self.drop_claim_response = False
        self.drop_report_response = False
        self.transform_report = None
        self.targets = tuple(TARGET.model_copy(update={
            "executor_id": f"executor-{index}", "agent_name": f"checksum-{index}",
            "agent_id": f"{index + 1:08x}-1111-1111-1111-111111111111",
            "agent_oid": f"{index + 1:08x}-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "session_id": f"existing-session-{index}",
        }) for index in range(capacity))
        routes, grants = [], []
        self.specs, self.obligations = [], []
        for index in range(count):
            command = self.command(f"native-{index}", sequence=index)
            command = command.model_copy(update={"work": command.work.model_copy(update={
                "required_checks": ("checksum_matches", "byte_count_matches", "native_binding_verified", "executor_idle_verified"),
                "criteria_version": "checksum-count-v1",
            })})
            self.attest(command)
            receipt = self.core.admit(command, CONTEXT)
            ob = receipt.payload["result"]["obligation_id"]
            content = b"original\nbytes\x00\xff" * 30
            spec = JobSpec(job_id=f"native-job-{index}", work_id=command.work.work_id, input=content, obligation_id=ob)
            self.specs.append(spec)
            self.obligations.append(ob)
            if enqueue_all or index == 0:
                self.workers.enqueue(spec, CONTEXT)
            route = NativeJobRoute(job_id=spec.job_id, api_binding_id=f"binding-{index}", grant_id=f"native-grant-{index}")
            routes.append(route)
            grants.append((native_key("grant", route.grant_id), NativeExecutionGrant(
                grant_id=route.grant_id, controller_id="internal-controller", owner=CONTEXT, policy_revision=1,
                job_id=spec.job_id, definition=command.work, input_digest=hashlib.sha256(content).hexdigest(),
                targets=self.targets, active=True, remaining_uses=4,
                not_before=NOW, expires_at=NOW + timedelta(hours=1),
                checkpoint_after_bytes=64 if park_first and index == 0 else None,
            )))
        self.put(grants)
        self.native = NativeLaunchAuthority(
            self.workers, team_id="team", controller_id="internal-controller", targets=self.targets,
            jobs=tuple(routes), verifier_version=self.config.verifier_version, clock=lambda: self.time,
        )
        self.runtimes, self.clients = {}, []
        for target in self.targets:
            principal = BehaviorPrincipal("tenant", target.agent_oid, "team", "agent", target.agent_name, frozenset({"consume"}))
            api = BehaviorExecutorClient(
                "https://behavior.example", "api://22222222-2222-2222-2222-222222222222", ApiCredential(),
                transport=httpx.MockTransport(lambda request, actor=principal: self.api(request, actor)),
            )
            runtime = HostedRuntime(
                wire.HostedIdentity(**{field: getattr(target, field) for field in ("agent_id", "agent_name", "agent_version", "session_id")}),
                ChecksumSupervisor(root / target.executor_id), api, clock=lambda: self.time,
            )
            self.runtimes[target.executor_id] = runtime
            self.clients.append(FoundryHttpClient(
                target, Credential(), transport=httpx.MockTransport(lambda request, t=target: self.invoke(request, t)),
            ))
        self.foundry = FoundryExecutor(self.native, tuple(self.clients))
        self.workers.adapter = self.foundry.adapter()

    def api(self, request, principal):
        self.api_calls.append((request.method, request.url.path, request.content))
        parts = request.url.path.split("/")
        binding_id, launch_id = parts[4], parts[7]
        body = json.loads(request.content)
        try:
            if parts[-1] == "claim":
                command = wire.HostedClaimRequest(**wire.HostedClaimBody.model_validate_json(request.content).model_dump(),
                                                launch_permit=SecretStr(request.headers["X-Behavior-Launch-Permit"]))
                self.claim_requests.append((binding_id, launch_id, command, principal))
                result = self.native.claim(binding_id, launch_id, command, principal)
                if self.drop_claim_response:
                    self.drop_claim_response = False
                    raise httpx.ReadTimeout("synthetic lost claim ack", request=request)
                headers = {}
                if result.report_token is not None:
                    headers["X-Behavior-Report-Token"] = result.report_token.get_secret_value()
                return httpx.Response(200, json=result.model_dump(mode="json", exclude={"report_token"}), headers=headers, request=request)
            probe_id = parts[-2]
            if parts[-1] == "resolve":
                command = wire.HostedProbeRequest(**wire.HostedProbeBody.model_validate_json(request.content).model_dump(),
                                                 report_token=SecretStr(request.headers["X-Behavior-Report-Token"]))
                result = self.native.resolve_probe(binding_id, launch_id, probe_id, command, principal)
            else:
                command = wire.HostedReport(**wire.HostedReportBody.model_validate_json(request.content).model_dump(),
                                           report_token=SecretStr(request.headers["X-Behavior-Report-Token"]))
                if self.transform_report is not None:
                    command = self.transform_report(command)
                self.report_requests.append((binding_id, launch_id, probe_id, command, principal))
                result = self.native.report(binding_id, launch_id, probe_id, command, principal)
                if self.drop_report_response:
                    self.drop_report_response = False
                    raise httpx.ReadTimeout("synthetic lost report ack", request=request)
            return httpx.Response(200, json=result.model_dump(mode="json"), request=request)
        except Problem as exc:
            return httpx.Response(exc.status, json=exc.body, request=request)

    def invoke(self, request, target):
        self.native_calls.append(request)
        runtime = self.runtimes[target.executor_id]
        invocation_id = ("inv_" + hashlib.sha256(request.content).hexdigest()[:48]
                         if request.url.path.endswith("/invocations") else request.headers["x-agent-invocation-id"])
        headers = {"x-agent-invocation-id": invocation_id, "x-agent-session-id": target.session_id}
        try:
            if request.url.path.endswith("/invocations"):
                if self.hold_start:
                    return httpx.Response(202, json={"status": "pending", "invocation_id": invocation_id,
                                                   "launch_id": json.loads(request.content)["launch_id"]},
                                          headers=headers, request=request)
                payload = HostedInvocation(**json.loads(request.content),
                                           launch_permit=SecretStr(request.headers[wire.NATIVE_LAUNCH_PERMIT_HEADER]))
                result = runtime.start(payload, invocation_id, target.session_id)
                if self.drop_start_response:
                    self.drop_start_response = False
                    raise httpx.ReadTimeout("synthetic lost native ack", request=request)
                return httpx.Response(202, json=result, headers=headers, request=request)
            result = runtime.observe(
                invocation_id, target.session_id, request.headers[wire.NATIVE_PROBE_HEADER],
                cancellation=request.url.path.endswith("/cancel"),
            )
            return httpx.Response(200, json=result.model_dump(mode="json"), headers=headers, request=request)
        except Problem as exc:
            return httpx.Response(exc.status, json=exc.body, headers=headers, request=request)

    def finish_job(self, index=0):
        job_id = self.specs[index].job_id
        launch_id = native_key("launch-id", self.workers.inspect(job_id).binding.start_request_id)
        launch = self.native.launch(launch_id)
        runtime = self.runtimes[launch.target.executor_id]
        runtime.supervisor.children[launch_id].wait(timeout=15)
        receipt = self.workers.reconcile(job_id, CONTEXT)
        return receipt

    def close(self):
        for runtime in self.runtimes.values():
            for child in runtime.supervisor.children.values():
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
            runtime.api.close()
        for client in self.clients:
            client.close()


def test_concrete_native_protocol_authority_and_real_child_complete_exact_original_bytes(tmp_path):
    h = NativeHarness(tmp_path)
    try:
        first = h.workers.start(h.specs[0].job_id, CONTEXT)
        assert first.payload["result"]["status"] == "starting" and len(h.occupied()) == 1
        result = h.finish_job()
        assert result.payload["result"]["status"] == "succeeded" and h.occupied() == []
        evidence_id = h.workers.inspect(h.specs[0].job_id).last_evidence_id
        verified = h.native.verify(evidence_id)
        logical = h.workers.inspect(h.specs[0].job_id).binding.start_request_id
        launch = h.native.launch(native_key("launch-id", logical))
        assert verified.native_execution_id == launch.native_invocation_id != logical
        assert h.claim_requests[0][2].invocation_id == launch.native_invocation_id
        assert any(request.method == "GET" and request.url.path.endswith("/" + launch.native_invocation_id)
                   for request in h.native_calls)
        body = json.loads(verified.result.content)
        assert body["input_digest"] == hashlib.sha256(h.specs[0].input).hexdigest()
        assert body["byte_count"] == len(h.specs[0].input)
        assert verified.entire_tree_idle and verified.all_inflight_starts_fenced
        assert result.payload["result"]["actual_environment"] == "NOT_RUN"
    finally:
        h.close()


def test_native_lost_start_ack_reconciles_same_child_without_second_post(tmp_path):
    h = NativeHarness(tmp_path)
    try:
        h.drop_start_response = True
        with pytest.raises(Problem) as unknown:
            h.workers.start(h.specs[0].job_id, CONTEXT)
        assert code(unknown) == "WORKER_START_UNKNOWN"
        assert len(h.occupied()) == 1
        logical = h.workers.inspect(h.specs[0].job_id).binding.start_request_id
        actual = h.native.launch(native_key("launch-id", logical)).native_invocation_id
        assert actual is not None and actual != logical
        h.finish_job()
        assert sum(call.method == "POST" and call.url.path.endswith("/invocations") for call in h.native_calls) == 1
        assert h.workers.inspect(h.specs[0].job_id).state == "succeeded"
        assert all(request.url.path.endswith("/" + actual) for request in h.native_calls if request.method == "GET")
        h.workers.start(h.specs[0].job_id, CONTEXT)
        assert sum(call.method == "POST" and call.url.path.endswith("/invocations") for call in h.native_calls) == 1
    finally:
        h.close()


@pytest.mark.parametrize("case", ["claimed-id-mismatch", "wrong-launch"])
def test_native_post_response_cannot_replace_persisted_permit_claim_binding(tmp_path, case):
    h = NativeHarness(tmp_path)
    original = h.invoke

    def spoof(request, target):
        actual = original(request, target)
        if not request.url.path.endswith("/invocations"):
            return actual
        body, headers = actual.json(), dict(actual.headers)
        if case == "claimed-id-mismatch":
            body["invocation_id"] = headers["x-agent-invocation-id"] = "inv_spoofed"
        else:
            body["launch_id"] = "different-original-launch"
        headers.pop("content-length", None)
        return httpx.Response(202, headers=headers, json=body, request=request)

    h.invoke = spoof
    try:
        with pytest.raises(Problem) as unknown:
            h.workers.start(h.specs[0].job_id, CONTEXT)
        assert code(unknown) == "WORKER_START_UNKNOWN"
        logical = h.workers.inspect(h.specs[0].job_id).binding.start_request_id
        actual = h.native.launch(native_key("launch-id", logical)).native_invocation_id
        assert actual not in {logical, "inv_spoofed", None}
        h.finish_job()
        assert sum(call.method == "POST" and call.url.path.endswith("/invocations") for call in h.native_calls) == 1
        assert h.workers.inspect(h.specs[0].job_id).state == "succeeded"
    finally:
        h.close()


def test_same_agent_mi_and_forged_session_assertion_cannot_claim_without_native_permit(tmp_path):
    h = NativeHarness(tmp_path)
    try:
        h.hold_start = True
        h.workers.start(h.specs[0].job_id, CONTEXT)
        target = h.targets[0]
        launch_id = native_key("launch-id", h.workers.inspect(h.specs[0].job_id).binding.start_request_id)
        launch = h.native.launch(launch_id)
        principal = BehaviorPrincipal("tenant", target.agent_oid, "team", "agent", target.agent_name, frozenset({"consume"}))
        command = wire.HostedClaimRequest(
            request_id="attacker-claim", invocation_id=launch.start.binding.start_request_id,
            identity=wire.HostedIdentity(**{field: getattr(target, field) for field in ("agent_id", "agent_name", "agent_version", "session_id")}),
            process_id="unassigned-process", launch_permit=SecretStr("0" * 64),
        )
        with pytest.raises(Problem) as denied:
            h.native.claim("binding-0", launch_id, command, principal)
        assert code(denied) == "NATIVE_CLAIM_DENIED" and len(h.occupied()) == 1
        assert h.native.launch(launch_id).phase == "post_unknown"
    finally:
        h.close()


def test_lost_fresh_claim_response_does_not_turn_replay_into_launch_permission(tmp_path):
    h = NativeHarness(tmp_path)
    try:
        h.drop_claim_response = True
        with pytest.raises(Problem):
            h.workers.start(h.specs[0].job_id, CONTEXT)
        args = h.claim_requests[0]
        replay = h.native.claim(*args)
        assert replay.claim_state == "replay" and replay.report_token is None
        assert all(not runtime.supervisor.children for runtime in h.runtimes.values())
        assert len(h.occupied()) == 1
    finally:
        h.close()


def test_four_distinct_configured_local_executors_fifth_queue_not_four_foundry_agents(tmp_path):
    h = NativeHarness(tmp_path, capacity=4, count=5)
    try:
        for spec in h.specs[:4]:
            h.workers.start(spec.job_id, CONTEXT)
        assert len(h.occupied()) == 4
        assert len({runtime.supervisor.children[next(iter(runtime.supervisor.children))].pid
                    for runtime in h.runtimes.values()}) == 4
        with pytest.raises(Problem) as full:
            h.workers.start(h.specs[4].job_id, CONTEXT)
        assert code(full) == "WORKER_CAPACITY_FULL"
        assert h.workers.inspect(h.specs[4].job_id).state == "queued"
        assert len(h.native_calls) == 4
        h.finish_job(0)
        h.workers.start(h.specs[4].job_id, CONTEXT)
        assert len(h.occupied()) == 4
        assert h.workers.inspect(h.specs[4].job_id).state == "starting"
        for index in range(1, 5):
            assert h.finish_job(index).payload["result"]["actual_environment"] == "NOT_RUN"
    finally:
        h.close()


def test_concrete_c1_parent_parks_child_progresses_then_resumes_and_late_predecessor_cannot_release(tmp_path):
    h = NativeHarness(tmp_path, count=2, park_first=True, enqueue_all=False)
    try:
        parent = h.specs[0].job_id
        h.workers.start(parent, CONTEXT)
        first_binding = h.workers.inspect(parent).binding
        h.specs[1] = h.specs[1].model_copy(update={"parent_job_id": parent})
        h.workers.enqueue(h.specs[1], CONTEXT)
        first_launch_id = native_key("launch-id", first_binding.start_request_id)
        h.runtimes[h.targets[0].executor_id].supervisor.children[first_launch_id].wait(timeout=15)
        parked_id = h.foundry.park(first_binding)
        h.workers.observe(parent, parked_id, CONTEXT)
        assert h.workers.inspect(parent).state == "waiting_dependency" and not h.occupied()
        checkpoint = h.workers.inspect(parent).checkpoint
        old_runtime = h.runtimes[h.targets[0].executor_id]
        # A new Hosted process gets a new capability for the next launch, not the
        # old reporter token. The original native park is already sealed server-side.
        h.runtimes[h.targets[0].executor_id] = HostedRuntime(
            old_runtime.identity, ChecksumSupervisor(old_runtime.supervisor.journal.root),
            old_runtime.api, clock=lambda: h.time,
        )
        assert not h.runtimes[h.targets[0].executor_id].report_tokens
        h.workers.start(h.specs[1].job_id, CONTEXT)
        h.finish_job(1)
        h.workers.start(parent, CONTEXT)
        assert h.workers.inspect(parent).generation == 2 and len(h.occupied()) == 1
        late = h.native.reconcile_sealed(first_binding)
        assert h.workers.observe(parent, late, CONTEXT).payload["result"]["status"] == "late_evidence_retained"
        assert h.workers.inspect(parent).generation == 2 and len(h.occupied()) == 1
        h.finish_job()
        launch_id = native_key("launch-id", h.workers.inspect(parent).binding.start_request_id)
        task = h.runtimes[h.targets[0].executor_id].supervisor.journal.assignment(launch_id)
        assert task.checkpoint == checkpoint and task.process_generation == 3
        assert h.workers.inspect(parent).state == "succeeded"
    finally:
        h.close()


def test_concrete_cancel_waits_for_owned_exit_and_keeps_original_stop_identity(tmp_path):
    h = NativeHarness(tmp_path, park_first=True)
    try:
        job_id = h.specs[0].job_id
        h.workers.start(job_id, CONTEXT)
        accepted = h.workers.cancel(job_id, CONTEXT)
        assert accepted.payload["result"]["executor_stopped"] is False and len(h.occupied()) == 1
        result = h.workers.stop(job_id, CONTEXT)
        assert result.payload["result"]["status"] == "cancelled" and not h.occupied()
        assert sum(call.url.path.endswith("/cancel") for call in h.native_calls) == 1
        h.workers.reconcile(job_id, CONTEXT)
        assert sum(call.url.path.endswith("/cancel") for call in h.native_calls) == 1
    finally:
        h.close()


@pytest.mark.parametrize("part", ["session", "version", "process", "generation", "binding", "exit", "result"])
def test_wrong_native_report_cannot_release_slot_or_publish_a_result(tmp_path, part):
    h = NativeHarness(tmp_path)
    try:
        job_id = h.specs[0].job_id
        h.workers.start(job_id, CONTEXT)

        def corrupt(report):
            if part in {"session", "version"}:
                return report.model_copy(update={"identity": report.identity.model_copy(update={
                    "session_id" if part == "session" else "agent_version": "foreign" if part == "session" else "9",
                })})
            if part == "result":
                return report.model_copy(update={"result": report.result.model_copy(update={"byte_count": 1})})
            return report.model_copy(update={
                {"process": "process_id", "generation": "process_generation", "binding": "binding_digest", "exit": "child_exited"}[part]:
                {"process": "foreign", "generation": 99, "binding": "0" * 64, "exit": False}[part],
            })

        h.transform_report = corrupt
        with pytest.raises(Problem):
            h.finish_job()
        assert len(h.occupied()) == 1
        assert h.workers.inspect(job_id).state == "starting"
        h.transform_report = None
        h.finish_job()
        assert h.workers.inspect(job_id).state == "succeeded"
    finally:
        h.close()


def test_report_ack_loss_recovers_retained_verified_result_without_second_native_execution(tmp_path):
    h = NativeHarness(tmp_path)
    try:
        job_id = h.specs[0].job_id
        h.workers.start(job_id, CONTEXT)
        h.drop_report_response = True
        with pytest.raises(Problem):
            h.finish_job()
        assert len(h.occupied()) == 1
        calls = len(h.native_calls)
        h.workers.reconcile(job_id, CONTEXT)
        assert len(h.native_calls) == calls and h.workers.inspect(job_id).state == "succeeded"
        assert len(h.occupied()) == 0
    finally:
        h.close()


def test_fresh_terminal_verification_survives_expired_probe_and_restarted_controller(tmp_path):
    h = NativeHarness(tmp_path)
    try:
        job_id = h.specs[0].job_id
        h.workers.start(job_id, CONTEXT)
        h.finish_job()
        original = h.workers.inspect(job_id).last_evidence_id
        h.time += timedelta(minutes=2)
        with pytest.raises(Problem) as stale:
            h.native.verify(original)
        assert code(stale) == "NATIVE_EVIDENCE_INVALID"
        before = len(h.native_calls)
        rebuilt = FoundryExecutor(h.native, tuple(h.clients))
        fresh = rebuilt.query(h.workers.inspect(job_id).binding)
        assert h.native.verify(fresh).state == "succeeded" and fresh != original
        assert len(h.native_calls) == before
    finally:
        h.close()


def test_capabilities_never_enter_api_json_authority_rows_or_private_child_state(tmp_path):
    h = NativeHarness(tmp_path)
    try:
        h.workers.start(h.specs[0].job_id, CONTEXT)
        h.finish_job()
        permit = h.native_calls[0].headers[wire.NATIVE_LAUNCH_PERMIT_HEADER]
        runtime = next(iter(h.runtimes.values()))
        report_token = next(iter(runtime.report_tokens.values())).get_secret_value()
        assert permit.encode() not in h.native_calls[0].content
        payloads = b"".join(value["Payload"] for value in h.client.backing.rows.values())
        requests = b"".join(body for _, _, body in h.api_calls)
        private_state = runtime.supervisor.journal.path.read_bytes()
        for capability in (permit, report_token):
            assert capability.encode() not in payloads + requests + private_state
        assert h.data("budget", "root")["remaining_attempts"] == 19
    finally:
        h.close()


def test_generic_agent_consume_and_human_cannot_supply_a_server_controller():
    agent = BehaviorPrincipal("tenant", TARGET.agent_oid, "team", "agent", "agent", frozenset({"consume"}))
    human = BehaviorPrincipal("tenant", TARGET.agent_oid, "team", "human", None, frozenset({"read", "submit", "acknowledge"}))
    for principal in (agent, human):
        with pytest.raises(Problem) as denied:
            controller_process_entry(lambda: principal)
        assert code(denied) == "NATIVE_CONTROLLER_REQUIRED"


def test_native_adoption_refuses_running_predecessor_even_with_a_new_epoch(tmp_path):
    h = NativeHarness(tmp_path, park_first=True)
    try:
        job_id = h.specs[0].job_id
        h.workers.start(job_id, CONTEXT)
        h.mutate("owner", CONTEXT.owner_record_id, epoch=2)
        with pytest.raises(Problem) as uncertain:
            h.foundry.adopt(job_id, CONTEXT.model_copy(update={"epoch": 2}))
        assert code(uncertain) == "NATIVE_ADOPTION_UNCERTAIN" and len(h.occupied()) == 1
    finally:
        h.close()


def test_native_adoption_requires_verified_park_and_current_owner_and_renewed_exact_grant(tmp_path):
    h = NativeHarness(tmp_path, count=2, park_first=True, enqueue_all=False)
    try:
        parent = h.specs[0].job_id
        h.workers.start(parent, CONTEXT)
        h.specs[1] = h.specs[1].model_copy(update={"parent_job_id": parent})
        h.workers.enqueue(h.specs[1], CONTEXT)
        parked = h.foundry.park(h.workers.inspect(parent).binding)
        h.workers.observe(parent, parked, CONTEXT)
        h.mutate("owner", CONTEXT.owner_record_id, epoch=2)
        new_owner = CONTEXT.model_copy(update={"epoch": 2})
        h.foundry.adopt(parent, new_owner)
        assert h.workers.inspect(parent).owner.epoch == 2
        h.workers.cancel(h.specs[1].job_id, new_owner)
        h.mutate("native-grant", "native-grant-0", owner=new_owner.model_dump(mode="json"))
        h.workers.start(parent, new_owner)
        job = h.workers.inspect(parent)
        launch_id = native_key("launch-id", job.binding.start_request_id)
        runtime = h.runtimes[h.native.launch(launch_id).target.executor_id]
        runtime.supervisor.children[launch_id].wait(timeout=15)
        h.workers.reconcile(parent, new_owner)
        assert h.workers.inspect(parent).state == "succeeded"
        assert h.workers.inspect(parent).binding.owner.epoch == 2
    finally:
        h.close()


def test_actual_sdk_host_and_loopback_http_serialize_native_session_capability_and_receipt(tmp_path, monkeypatch):
    """Real socket + installed SDK host + real child; API/Table remain OFFLINE."""
    from fastapi.testclient import TestClient
    from shared_context.behavior_hosted import create_host

    h = NativeHarness(tmp_path)
    target = h.targets[0]
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", target.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_ID", target.agent_id)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", target.agent_name)
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", target.agent_version)
    runtime = h.runtimes[target.executor_id]
    seen = []

    with TestClient(create_host(runtime)) as sdk:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_control(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                parsed = urlsplit(self.path)
                path = parsed.path.split("/endpoint/protocols", 1)[1]
                if parsed.query:
                    path += "?" + parsed.query
                seen.append((self.command, parsed.path))
                forwarded = {name: value for name, value in self.headers.items()
                             if name.lower().startswith("x-client-") or name.lower() in {
                                 "accept", "accept-encoding", "content-type", "content-length", "user-agent"}}
                # These are platform-injected metadata, not forwarded caller headers.
                forwarded["x-agent-invocation-id"] = (
                    "inv_" + hashlib.sha256(raw).hexdigest()[:48] if parsed.path.endswith("/invocations")
                    else self.headers["x-agent-invocation-id"])
                forwarded["x-agent-session-id"] = target.session_id
                result = sdk.request(self.command, path, content=raw, headers=forwarded)
                self.send_response(result.status_code)
                for name in ("content-type", "x-agent-invocation-id", "x-agent-session-id"):
                    if name in result.headers:
                        self.send_header(name, result.headers[name])
                self.send_header("Content-Length", str(len(result.content)))
                self.end_headers()
                self.wfile.write(result.content)

            do_GET = handle_control
            do_POST = handle_control

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever)
        thread.start()

        class LocalTransport(httpx.BaseTransport):
            def __init__(self):
                self.transport = httpx.HTTPTransport(retries=0)

            def handle_request(self, request):
                local = httpx.Request(
                    request.method, request.url.copy_with(scheme="http", host="127.0.0.1", port=server.server_port),
                    headers=request.headers, content=request.content, extensions=request.extensions,
                )
                return self.transport.handle_request(local)

            def close(self):
                self.transport.close()

        client = FoundryHttpClient(target, Credential(), transport=LocalTransport())
        try:
            h.foundry = FoundryExecutor(h.native, (client,))
            h.workers.adapter = h.foundry.adapter()
            h.workers.start(h.specs[0].job_id, CONTEXT)
            h.finish_job()
            assert h.workers.inspect(h.specs[0].job_id).state == "succeeded"
            assert [method for method, _ in seen] == ["POST", "GET"]
            assert h.native.launch(native_key("launch-id", h.workers.inspect(h.specs[0].job_id).binding.start_request_id)).phase == "sealed"
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            h.close()


class SocketNativeTransport(httpx.BaseTransport):
    """Test-only exact-route proxy to a LOCAL socket, never Azure."""

    def __init__(self, port):
        self.port, self.transport = port, httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        return self.transport.handle_request(httpx.Request(
            request.method, request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port),
            headers=request.headers, content=request.content, extensions=request.extensions,
        ))

    def close(self):
        self.transport.close()


def native_controller_child(path, contract_json, target_json, port, action):
    """Real controller process, with independent clients and shared OFFLINE authority."""
    store = BehaviorStore(FakeTable(SQLiteBacking(path)), SCOPE)
    workers = WorkerCore(
        store, WorkerContract.model_validate_json(contract_json), adapter=None,
        mode="offline_contract", clock=lambda: NOW,
    )
    target = FoundryTarget.model_validate_json(target_json)
    authority = NativeLaunchAuthority(
        workers, team_id="team", controller_id="internal-controller", targets=(target,),
        jobs=(NativeJobRoute(job_id="native-job-0", api_binding_id="binding-0", grant_id="native-grant-0"),),
        verifier_version="offline-verifier-v1", clock=lambda: NOW,
    )
    client = FoundryHttpClient(target, Credential(), transport=SocketNativeTransport(port))
    executor = FoundryExecutor(authority, (client,))
    workers.adapter = executor.adapter()
    if action == "start-and-die":
        workers.start("native-job-0", CONTEXT)
        os._exit(23)
    workers.reconcile("native-job-0", CONTEXT)
    client.close()


def test_real_controller_process_restart_uses_authority_and_native_probe_not_a_second_start(tmp_path):
    h = NativeHarness(tmp_path / "host")
    database = tmp_path / "authority.db"
    backing = SQLiteBacking(database)
    backing.seed(h.client.backing)
    h.client.backing = backing

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            response = h.invoke(httpx.Request(
                self.command, "https://synthetic.services.ai.azure.com" + self.path,
                headers=dict(self.headers), content=body,
            ), h.targets[0])
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                if name.lower() in {"content-type", "x-agent-session-id", "x-agent-invocation-id"}:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = request
        do_POST = request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    ctx = multiprocessing.get_context("spawn")
    args = (str(database), h.worker_contract.model_dump_json(), h.targets[0].model_dump_json(), server.server_port)
    try:
        first = ctx.Process(target=native_controller_child, args=(*args, "start-and-die"))
        first.start()
        join(first)
        assert first.exitcode == 23 and h.workers.inspect("native-job-0").state == "starting"
        binding = h.workers.inspect("native-job-0").binding
        launch_id = native_key("launch-id", binding.start_request_id)
        h.runtimes[h.targets[0].executor_id].supervisor.children[launch_id].wait(timeout=15)
        restarted = ctx.Process(target=native_controller_child, args=(*args, "reconcile"))
        restarted.start()
        join(restarted)
        assert restarted.exitcode == 0 and h.workers.inspect("native-job-0").state == "succeeded"
        assert h.workers.inspect("native-job-0").binding == binding
        assert sum(request.method == "POST" and request.url.path.endswith("/invocations") for request in h.native_calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        h.close()
