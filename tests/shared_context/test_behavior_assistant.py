"""Actual SDK/runtime + HTTP/authority/M3 boundaries; no Azure or model calls."""

import base64
import hashlib
import json
import asyncio
from types import SimpleNamespace
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import A, require_cold_resume_paths
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
from behavior_assistant_helpers import call


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
    engine = AssistantEngine()
    with pytest.raises(Problem) as disabled:
        engine.dispatch({"operation": "assistant", "profile": "assistant-v1.2", "action": "start"})
    assert disabled.value.status == 403
    with pytest.raises(Problem) as invalid:
        engine.dispatch(["SYNTHETIC_SECRET_CANARY"])
    assert invalid.value.status == 422
    assert "SYNTHETIC_SECRET_CANARY" not in str(invalid.value)


@pytest.mark.parametrize("payload", [
    {"operation": "assistant", "action": "start", "target_agent_id": "00000000-0000-4000-8000-000000000001",
     "run_id": "run-1", "team": "team-one", "work": "task-1", "prompt": "SYNTHETIC_SECRET_CANARY",
     "input_text": "SYNTHETIC_SECRET_CANARY"},
    {"operation": "assistant", "profile": "assistant-v1.1", "action": "start"},
    {"operation": "assistant", "action": "status", "target_agent_id": "00000000-0000-4000-8000-000000000001",
     "run_id": "run-1"},
])
def test_retired_command_shapes_are_rejected_even_with_a_typed_session(payload):
    from shared_context.behavior_assistant_hosted import AssistantSession
    from shared_context.m2_assistant import AssistantEngine
    session = AssistantSession.__new__(AssistantSession)
    session.dispatch = lambda _payload: pytest.fail("a retired command shape must not reach the v1.2 session")
    with pytest.raises(Problem) as rejected:
        AssistantEngine(behavior=session).dispatch(payload)
    assert rejected.value.status == 422
    assert rejected.value.body["error"]["code"] == "INVALID_REQUEST"
    assert "SYNTHETIC_SECRET_CANARY" not in str(rejected.value.body)
