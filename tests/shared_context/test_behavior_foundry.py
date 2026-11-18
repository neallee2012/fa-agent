"""Offline protocol serialization/local transport tests; native NOT_RUN."""

import json
import hashlib
import time
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from shared_context.behavior_foundry import FoundryHttpClient, FoundryTarget, FOUNDRY_TOKEN_SCOPE
from shared_context.errors import Problem
from shared_context import behavior_wire as wire
from shared_context.behavior_delivery import VerifiedWorkResult
from shared_context.behavior_foundry import (
    FoundryExecutor, NativeExecutionGrant, NativeJobRoute, NativeLaunchAuthority, native_key,
)
from shared_context.behavior_workers import JobSpec
from test_behavior_delivery import CONTEXT, NOW
from test_behavior_workers import WorkerHarness
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


class NativeHarness(WorkerHarness):
    """Concrete clients and server authority over OFFLINE HTTP/Table transports."""

    def __init__(self, capacity=1, count=1, park_first=False, enqueue_all=True):
        super().__init__(capacity=capacity)
        self.native_calls = []
        self.hold_start = False
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
        self.clients = [FoundryHttpClient(
            target, Credential(), transport=httpx.MockTransport(lambda request, t=target: self.invoke(request, t)),
        ) for target in self.targets]
        self.foundry = FoundryExecutor(self.native, tuple(self.clients))
        self.workers.adapter = self.foundry.adapter()

    def invoke(self, request, target):
        """The Hosted side as it is now: a held start stays pending; any other legacy request is the retired lane."""
        self.native_calls.append(request)
        invocation_id = ("inv_" + hashlib.sha256(request.content).hexdigest()[:48]
                         if request.url.path.endswith("/invocations") else request.headers["x-agent-invocation-id"])
        headers = {"x-agent-invocation-id": invocation_id, "x-agent-session-id": target.session_id}
        if request.url.path.endswith("/invocations") and self.hold_start:
            return httpx.Response(202, json={"status": "pending", "invocation_id": invocation_id,
                                           "launch_id": json.loads(request.content)["launch_id"]},
                                  headers=headers, request=request)
        retired = Problem(410, "JOB_LANE_RETIRED", "the legacy job lane is retired")
        return httpx.Response(retired.status, json=retired.body, headers=headers, request=request)

    def close(self):
        for client in self.clients:
            client.close()

