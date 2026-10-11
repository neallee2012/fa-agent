"""Actual SDK/runtime + HTTP/authority/M3 boundaries; no Azure or model calls."""

import base64
import hashlib
import json
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import A
from shared_context import behavior_wire as wire
from shared_context.behavior_assistant import (
    AssistantAuthority, AssistantProfile, AssistantRuntime, LocalAssistantExecutor,
)
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_wire import AssistantSubmit
from shared_context.m3_artifacts import ArtifactService
from shared_context.m3_auth import ApprovalSettings
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.models import CreateWork
from shared_context.errors import Problem
from test_behavior_api import BASE, METRICS, RuntimeHarness
from test_m2_copilot import ScriptedEndpoint, runtime_path
from test_m3_auth import HUMAN_OID, CLIENT_ID, jwk_client, signing_key


def require_cold_resume_paths(root):
    """The offline SDK cold-resume checkpoint nests about 200 characters of session-state below the test root."""
    import os
    if os.name != "nt" or len(str(root)) + 200 < 260:
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            if winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1:
                return
    except OSError:
        pass
    pytest.skip("needs Windows LongPathsEnabled=1 or a short --basetemp (e.g. C:\\t): the offline SDK checkpoint "
                "path below the test root exceeds MAX_PATH 260")


def call(name, args, number):
    return {"tool_calls": [{"index": 0, "id": f"call_{number}", "type": "function",
                           "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture
def assistant(auth, jwk_client, signing_key, system, tmp_path, runtime_path):
    h = RuntimeHarness(auth, jwk_client, signing_key, work_checks=(
        "answer_verified", "artifact_verified", "finish_verified",
    ))
    ref = h.source()
    candidate = wire.ClaimDraft(
        request_id="review", version=1, text="The accepted metrics source has been reviewed.",
        supporting=(ref,), effective_from=h.time,
    )
    preview = h.post("/claims/claim-one/preview", candidate).json()
    reviewed = h.post("/claims/claim-one/review", wire.ClaimReview(
        **candidate.model_dump(), decision_id="decision",
        candidate_sha256=preview["candidate_sha256"], evidence_sha256=preview["evidence_sha256"],
        policy_version=preview["policy_version"],
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
    ))
    assert reviewed.status_code == 200, reviewed.text
    assert h.post("/claims/claim-one/commit", wire.Command(request_id="review")).status_code == 200
    context = system[0]
    context.create(A, A.team, "legacy-source", CreateWork(goal="Assistant result source"))
    h.artifacts = ArtifactService(context, InMemoryArtifactStore(auth.tenant_id),
                                 ApprovalSettings(HUMAN_OID, CLIENT_ID, A.team))
    profile = AssistantProfile(
        binding_id="binding-one", source=ref, claim_id="claim-one",
        legacy_source_work="legacy-source", artifact_id="assistant-report",
        artifact_version="a" * 32, policy_version="assistant-own-result-v1",
    )
    h.runtime = AssistantRuntime(h.dep, (h.binding,), profiles=(profile,),
                                 clock=lambda: h.time, mode="offline_contract",
                                 controller_id="internal-controller")
    h.assistant = AssistantAuthority(h.runtime, h.artifacts, (A,))
    h.app = create_assistant_app(h.assistant, h.verifier)
    h.client = TestClient(h.app)
    h.endpoint = ScriptedEndpoint([
        call("read_context", {}, 1),
        call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 1}, 2),
        call("finish_task", {}, 3),
    ])
    h.executor = LocalAssistantExecutor(
        h.assistant, h.client, agent_headers=lambda: h.headers("agent"),
        root=tmp_path, runtime_path=runtime_path, model_transport=h.endpoint.transport,
    )
    h.workers.adapter = h.executor.adapter()
    yield h
    h.executor.close()


def submit(h):
    response = h.post("/assistant/submit", AssistantSubmit(request_id="assistant-submit"))
    assert response.status_code == 200, response.text
    return response.json()


def test_user_receives_verified_artifact_only_after_finish_and_separate_receipt(assistant):
    h = assistant
    assert submit(h)["state"] == "queued"
    assert h.endpoint.requests == []
    result = h.runtime.controller.step("binding-one")
    assert result.state == "succeeded"
    assert result.result_forwarded and not result.obligation_fulfilled
    version = h.artifacts.version(A, A.team, "assistant-report", "a" * 32)
    _, content = h.artifacts.download(A, A.team, "assistant-report", "a" * 32, version.blob.etag)
    assert json.loads(content)["remaining_count"] == 1
    assert json.loads(content)["source_sha256"] == hashlib.sha256(METRICS).hexdigest()
    assert h.artifacts.context.get(A, A.team, "legacy-source").payload["revision"] == 1
    pull = h.post("/delivery/pull", wire.Command(request_id="pull"))
    assert pull.status_code == 200, pull.text
    delivery = wire.DeliveryBinding.model_validate_json(base64.urlsafe_b64decode(pull.headers["x-behavior-binding"]))
    assert delivery.artifacts[0].sha256 == version.sha256
    acknowledgment = wire.PullAcknowledgment(
        request_id="ack", exchange_id=pull.headers["x-behavior-exchange"],
        acknowledgment_token=pull.headers["x-behavior-ack-token"], binding=delivery,
    )
    assert h.post("/delivery/acknowledge", acknowledgment).json()["result"]["fulfillment"] == "fulfilled"
    assert submit(h)["obligation_fulfilled"]
    assert len(h.endpoint.requests) == 3


def test_sdk_turn_end_does_not_finish_and_cold_resume_does_not_repeat_saved_artifact(assistant, tmp_path):
    require_cold_resume_paths(tmp_path)
    h = assistant
    h.endpoint.replies = iter([
        call("read_context", {}, 1),
        call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 1}, 2),
        {},
        call("read_context", {}, 4),
        call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 1}, 5),
        call("finish_task", {}, 6),
    ])
    submit(h)
    parked = h.runtime.controller.step("binding-one")
    assert parked.state == "waiting_dependency" and not parked.obligation_fulfilled
    original = h.artifacts.get(A, A.team, "assistant-report")
    h.executor.root = tmp_path / "replacement-host"
    done = h.runtime.controller.step("binding-one")
    assert done.state == "succeeded" and done.generation == 2
    assert h.artifacts.get(A, A.team, "assistant-report") == original
    assert len(h.endpoint.requests) == 6


def test_unknown_upload_is_not_resent_after_authority_restart(assistant, monkeypatch):
    h = assistant
    original_put = h.artifacts.store.put
    uploads = []
    def lost_receipt(*args):
        uploads.append(args)
        original_put(*args)
        raise TimeoutError("SYNTHETIC_SECRET_MUST_NEVER_LEAVE")
    monkeypatch.setattr(h.artifacts.store, "put", lost_receipt)
    h.endpoint.replies = iter([
        call("read_context", {}, 1),
        call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 1}, 2),
        {},
        call("read_context", {}, 4),
        call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 1}, 5),
        call("finish_task", {}, 6),
    ])
    submit(h)
    assert h.runtime.controller.step("binding-one").state == "waiting_dependency"
    h.assistant = AssistantAuthority(h.runtime, h.artifacts, (A,))
    h.app = create_assistant_app(h.assistant, h.verifier)
    h.client = TestClient(h.app)
    h.executor.authority, h.executor.http_client = h.assistant, h.client
    assert h.runtime.controller.step("binding-one").state == "succeeded"
    assert len(uploads) == 1
    assert "SYNTHETIC_SECRET_MUST_NEVER_LEAVE" not in json.dumps(h.endpoint.requests)


def test_unapproved_operation_cannot_change_work_even_when_model_asks(assistant):
    h = assistant
    h.endpoint.replies = iter([call("execute_operation", {}, 1), {}])
    submit(h)
    assert h.runtime.controller.step("binding-one").state == "waiting_dependency"
    assert h.client.get(BASE + "/operation", headers=h.headers("agent")).status_code != 200
    assert not h.runtime.job_status("binding-one", h.verifier(h.client.build_request(
        "GET", BASE, headers=h.headers("agent")))).obligation_fulfilled


@pytest.mark.parametrize("identity", ["agent", "old-m3"])
def test_only_explicit_human_admission_can_delegate_own_result(assistant, identity):
    response = assistant.post("/assistant/submit", AssistantSubmit(request_id="bad-submit"), identity)
    assert response.status_code in {401, 403}
    assert assistant.endpoint.requests == []


@pytest.mark.parametrize("pause", [False, True])
def test_native_protocol_reaches_actual_assistant_sdk_not_checksum(assistant, tmp_path, runtime_path, pause):
    if pause:
        require_cold_resume_paths(tmp_path)
    from shared_context.behavior_assistant_hosted import AssistantHostedRuntime, AssistantSupervisor
    from shared_context.behavior_foundry import (
        FoundryExecutor, FoundryHttpClient, FoundryTarget, NativeExecutionGrant, NativeJobRoute,
        NativeLaunchAuthority, native_key,
    )
    from shared_context.behavior_hosted import BehaviorExecutorClient, HostedInvocation
    from shared_context.behavior_assistant import _LocalHttp
    from pydantic import SecretStr

    h = assistant
    target = FoundryTarget(
        executor_id=h.workers.contract.executor_id, project_endpoint="https://native.example/api/projects/test",
        agent_id="11111111-1111-1111-1111-111111111111", agent_name="assistant",
        agent_version="8", session_id="assistant-native-session", agent_oid=A.oid,
    )
    native = NativeLaunchAuthority(
        h.workers, team_id=A.team, controller_id="internal-controller", targets=(target,),
        jobs=(NativeJobRoute(job_id="job-one", api_binding_id="binding-one", grant_id="native-grant"),),
        verifier_version=h.config.verifier_version, clock=lambda: h.time,
    )
    provider_id = None
    native_starts = 0
    host = None
    if pause:
        h.endpoint.replies = iter([
            call("read_context", {}, 1),
            call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 1}, 2), {},
            call("read_context", {}, 4),
            call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 1}, 5),
            call("finish_task", {}, 6),
        ])
    def native_http(request):
        nonlocal provider_id, native_starts
        if request.method == "POST" and "x-client-behavior-launch-permit" in request.headers:
            native_starts += 1
            provider_id = f"inv_assistant_sdk_local_{native_starts}"
            body = json.loads(request.content)
            result = host.start(HostedInvocation(**body, launch_permit=SecretStr(
                request.headers["x-client-behavior-launch-permit"])), provider_id, target.session_id)
            return httpx.Response(202, json=result, headers={
                "x-agent-invocation-id": provider_id, "x-agent-session-id": target.session_id,
            })
        report = host.observe(provider_id, target.session_id, request.headers["x-client-behavior-probe-id"])
        return httpx.Response(200, json=report.model_dump(mode="json"), headers={
            "x-agent-invocation-id": provider_id, "x-agent-session-id": target.session_id,
        })
    native_client = FoundryHttpClient(target, SimpleNamespace(get_token=lambda _: SimpleNamespace(token="SYNTHETIC_NATIVE")),
                                     transport=httpx.MockTransport(native_http))
    executor = FoundryExecutor(native, (native_client,))
    h.workers.adapter = executor.adapter()
    h.dep = replace(h.dep, native_launches=native)
    h.runtime = AssistantRuntime(h.dep, (h.binding,), profiles=tuple(h.runtime.profiles.values()),
                                 clock=lambda: h.time, mode="offline_contract", controller_id="internal-controller")
    h.assistant = AssistantAuthority(h.runtime, h.artifacts, (A,))
    h.app = create_assistant_app(h.assistant, h.verifier)
    h.client = TestClient(h.app)
    api = BehaviorExecutorClient(
        "https://context.invalid", h.verifier.auth.audience,
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token=h.headers("agent")["Authorization"].removeprefix("Bearer "))),
        transport=_LocalHttp(h.client),
    )
    supervisor = AssistantSupervisor(tmp_path / "native-host", runtime_path=runtime_path, model_transport=h.endpoint.transport)
    host = AssistantHostedRuntime(wire.HostedIdentity(
        agent_id=target.agent_id, agent_name=target.agent_name, agent_version=target.agent_version,
        session_id=target.session_id,
    ), supervisor, api, clock=lambda: h.time)
    submit(h)
    job = h.workers.inspect("job-one")
    h.seed([(native_key("grant", "native-grant"), NativeExecutionGrant(
        grant_id="native-grant", controller_id="internal-controller", owner=job.owner,
        policy_revision=1, job_id="job-one", definition=h.binding.work,
        input_digest=hashlib.sha256(job.spec.input).hexdigest(), targets=(target,),
        active=True, remaining_uses=4, not_before=h.time - timedelta(minutes=1),
        expires_at=h.time + timedelta(minutes=5), workload_version="assistant-v1.2",
    ))])
    try:
        first = h.runtime.controller.step("binding-one")
        assert first.state in {"starting", "running"}
        supervisor.join(timeout=45)
        complete = h.runtime.controller.step("binding-one")
        if pause:
            assert complete.state == "waiting_dependency"
            original = h.artifacts.get(A, A.team, "assistant-report")
            # A replacement supervisor has no private SDK files or old ticket.
            supervisor = AssistantSupervisor(tmp_path / "replacement-native-host", runtime_path=runtime_path,
                                             model_transport=h.endpoint.transport)
            host = AssistantHostedRuntime(host.identity, supervisor, api, clock=lambda: h.time)
            assert h.runtime.controller.step("binding-one").state == "starting"
            supervisor.join(timeout=45)
            complete = h.runtime.controller.step("binding-one")
            assert h.artifacts.get(A, A.team, "assistant-report") == original
        assert complete.state == "succeeded" and not complete.obligation_fulfilled
        assert len(h.endpoint.requests) == (6 if pause else 3)
    finally:
        api.close()
        native_client.close()


def test_real_sdk_uses_ticket_bound_server_model_proxy_and_shared_root_budget(assistant):
    from shared_context.behavior_assistant_gateway import AssistantProvider
    h = assistant
    h.endpoint.expected_url = "https://provider.invalid/openai/v1/chat/completions"
    provider = AssistantProvider(
        "https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="SYNTHETIC_UPSTREAM_PRIVATE")),
        transport=h.endpoint.transport,
    )
    h.app = create_assistant_app(h.assistant, h.verifier, provider=provider)
    h.client = TestClient(h.app)
    h.executor.http_client, h.executor.model_transport = h.client, None
    submit(h)
    assert h.runtime.controller.step("binding-one").state == "succeeded"
    assert len(h.endpoint.requests) == 3
    assert "SYNTHETIC_UPSTREAM_PRIVATE" not in json.dumps(h.endpoint.requests)


@pytest.mark.parametrize("reply", [
    {"content": "SYNTHETIC_SECRET_CANARY"},
    call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 1,
                         "owner": "SYNTHETIC_SECRET_CANARY"}, 1),
    call("shell", {"command": "SYNTHETIC_SECRET_CANARY"}, 1),
])
def test_unclassified_model_success_is_refused_before_sdk_files_and_restart(assistant, reply, caplog):
    h = assistant
    h.endpoint.replies = iter([reply])
    submit(h)
    with pytest.raises(Problem):
        h.runtime.controller.step("binding-one")
    assert len(h.endpoint.requests) == 1
    diagnostics = [record.getMessage() for record in caplog.records if "ASSISTANT_MODEL_FAILURE" in record.getMessage()]
    assert diagnostics == ["ASSISTANT_MODEL_FAILURE phase=output_boundary status=unknown"]
    assert "SYNTHETIC_SECRET_CANARY" not in caplog.text
    for path in h.executor.root.rglob("*"):
        if path.is_file():
            assert b"SYNTHETIC_SECRET_CANARY" not in path.read_bytes()
            assert h.headers("agent")["Authorization"].encode() not in path.read_bytes()
    with pytest.raises(Problem):
        h.runtime.controller.step("binding-one")
    assert len(h.endpoint.requests) == 1
    assert h.workers.inspect("job-one").state == "starting"


def test_upstream_error_records_status_without_response_body_or_credentials(caplog):
    from shared_context.behavior_assistant_gateway import AssistantProvider
    provider = AssistantProvider(
        "https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="SYNTHETIC_UPSTREAM_PRIVATE")),
        transport=httpx.MockTransport(lambda _: httpx.Response(503, text="SYNTHETIC_SECRET_CANARY")),
    )
    body = {"model": "gpt-4.1-mini", "max_tokens": 1024, "stream": True,
            "messages": [{"role": "user", "content": "Synthetic request"}]}
    with pytest.raises(Problem):
        asyncio.run(provider.send(body))
    assert "ASSISTANT_PROVIDER_HTTP_FAILURE status=503" in caplog.text
    assert "SYNTHETIC_SECRET_CANARY" not in caplog.text
    assert "SYNTHETIC_UPSTREAM_PRIVATE" not in caplog.text


def test_native_sdk_exception_is_observable_without_fabricating_exit(tmp_path, monkeypatch, caplog):
    from shared_context import behavior_assistant_hosted as hosted
    from shared_context import m2_assistant

    def fail(_):
        raise RuntimeError("SYNTHETIC_SECRET_CANARY")

    monkeypatch.setattr(hosted, "AssistantSession", lambda *args, **kwargs: object())
    monkeypatch.setattr(m2_assistant, "AssistantEngine",
                        lambda *args, **kwargs: SimpleNamespace(dispatch=fail))
    supervisor = hosted.AssistantSupervisor(tmp_path, runtime_path=tmp_path / "not-invoked")
    supervisor._execute(SimpleNamespace(launch_id="original-unknown"), None)
    assert "ASSISTANT_EXECUTION_UNRESOLVED" in caplog.text
    assert "SYNTHETIC_SECRET_CANARY" not in caplog.text
    with supervisor.journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["http-error", "stream-error", "tool-success", "tool-error"])
def test_secret_boundary_refuses_unknown_output_before_any_general_channel(kind):
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    boundary = AssistantModelBoundary(None)
    if kind.startswith("tool"):
        value = '{"state":"saved","secret":"SYNTHETIC_SECRET_CANARY"}' if kind == "tool-success" else "SYNTHETIC_SECRET_CANARY"
        with pytest.raises((ValueError, KeyError)):
            boundary.tool_result("save_result", value)
        return
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"SYNTHETIC_'
            yield b'SECRET_CANARY"}}]}\n\n'
            raise RuntimeError("SYNTHETIC_SECRET_CANARY")
    response = (httpx.Response(500, content=b"SYNTHETIC_SECRET_CANARY") if kind == "http-error"
                else httpx.Response(200, stream=Stream(), headers={"content-type": "text/event-stream"}))
    with pytest.raises(RuntimeError) as error:
        asyncio.run(boundary.response(response))
    assert "SYNTHETIC_SECRET_CANARY" not in str(error.value)


def test_owner_epoch_revocation_wins_before_first_tool_or_model(assistant, monkeypatch):
    from shared_context.behavior_operations import control_key
    h = assistant
    issue = h.assistant.issue
    def rebind(start):
        result = issue(start)
        h.mutate(control_key("owner", "owner"), epoch=2)
        return result
    monkeypatch.setattr(h.assistant, "issue", rebind)
    submit(h)
    with pytest.raises(Problem):
        h.runtime.controller.step("binding-one")
    assert h.endpoint.requests == []
    assert h.workers.inspect("job-one").state == "starting"


def test_changed_result_cannot_replace_saved_artifact_on_resume(assistant, tmp_path):
    require_cold_resume_paths(tmp_path)
    h = assistant
    h.endpoint.replies = iter([
        call("read_context", {}, 1),
        call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 1}, 2), {},
        call("read_context", {}, 4),
        call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 99}, 5), {},
    ])
    submit(h)
    assert h.runtime.controller.step("binding-one").state == "waiting_dependency"
    original = h.artifacts.get(A, A.team, "assistant-report")
    h.executor.root = tmp_path / "replacement"
    assert h.runtime.controller.step("binding-one").state == "waiting_dependency"
    assert h.artifacts.get(A, A.team, "assistant-report") == original


def test_real_sdk_rejects_self_goal_edits_and_can_finish_the_original_work(assistant):
    from shared_context.behavior_effects import GOAL_REVIEW, WorkGoalUpdate
    from test_behavior_api import CTX
    h = assistant
    submit(h)
    before = h.adapter.inspect("work-one", CTX)
    h.references["goal"] = WorkGoalUpdate(
        work_id="work-one", obligation_id=h.binding.obligation_id, expected_etag=before.etag,
        expected_definition_digest=before.definition_digest, goal=GOAL_REVIEW,
    )
    prepared = h.post("/operation/prepare", wire.OperationPrepare(request_id="prepare", input_ref="goal"), "agent")
    assert prepared.status_code == 200, prepared.text
    descriptor = prepared.json()["result"]
    approval = wire.OperationApproval(
        request_id="human-approval", decision_id="human-operation", description_digest=descriptor["description_digest"],
        adapter_contract_digest=descriptor["adapter_contract_digest"], expires_at=h.time + timedelta(minutes=2),
    )
    assert h.post("/operation/approve", approval, "agent").status_code == 403
    assert h.post("/operation/approve", approval).status_code == 200
    h.endpoint.replies = iter([
        call("execute_operation", {}, 1), call("execute_operation", {}, 2),
        call("read_context", {}, 3),
        call("save_result", {"item_count": 3, "completed_count": 2, "remaining_count": 1}, 4),
        call("finish_task", {}, 5),
    ])
    assert h.runtime.controller.step("binding-one").state == "succeeded"
    after = h.adapter.inspect("work-one", CTX)
    assert after.definition_digest == before.definition_digest
    assert len(h.endpoint.requests) == 5


def test_revoked_source_cannot_reenter_model_through_a_cold_sdk_checkpoint(assistant, tmp_path):
    h = assistant
    h.endpoint.replies = iter([call("read_context", {}, 1), {}])
    submit(h)
    assert h.runtime.controller.step("binding-one").state == "waiting_dependency"
    ref = next(iter(h.runtime.profiles.values())).source
    revoke = wire.SourceWithdrawal(
        request_id="withdraw", withdrawal_id="withdraw-source", reference=ref, expected_access_revision=1,
    )
    assert h.post("/sources/metrics/withdraw", revoke).status_code == 200
    h.executor.root = tmp_path / "cold-no-source-access"
    with pytest.raises(Problem):
        h.runtime.controller.step("binding-one")
    assert len(h.endpoint.requests) == 2
    assert not list(h.executor.root.rglob("events.jsonl"))


def test_profile_cannot_be_enabled_by_model_payload_or_invalid_legacy_input():
    from shared_context.m2_assistant import AssistantEngine
    engine = AssistantEngine(None, process_marker="profile-denied")
    with pytest.raises(Problem) as disabled:
        engine.dispatch({"operation": "assistant", "profile": "assistant-v1.2", "action": "start"})
    assert disabled.value.status == 403
    with pytest.raises(Problem) as invalid:
        engine.dispatch(["SYNTHETIC_SECRET_CANARY"])
    assert invalid.value.status == 422
    assert "SYNTHETIC_SECRET_CANARY" not in str(invalid.value)
