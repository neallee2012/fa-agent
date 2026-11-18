"""Original launch rejection over local authority/HTTP doubles; no SDK or cloud."""

from datetime import timedelta
from dataclasses import replace
import hashlib
import json
import socket
import subprocess

from pydantic import SecretStr
import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant import key as assistant_key
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_foundry import SESSION_VERSION_CHANGED, native_key, session_version_failure
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from test_behavior_delivery import CONTEXT
from test_behavior_foundry import NativeHarness
from test_behavior_assistant import assistant
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_models, runtime_path, sdk_io
from test_m3_auth import jwk_client, signing_key
from hosted_versions_stub import deployed_versions


@pytest.fixture
def unclaimed(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("no process or real network may start")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    h = NativeHarness(tmp_path)
    h.hold_start = True
    job_id = h.specs[0].job_id
    receipt = h.workers.start(job_id, CONTEXT)
    binding = h.workers.inspect(job_id).binding
    launch = h.native.launch(native_key("launch-id", binding.start_request_id))
    try:
        yield h, job_id, receipt, binding, launch
    finally:
        h.close()


def claim(h, launch, request_id):
    target = launch.target
    principal = BehaviorPrincipal("tenant", target.agent_oid, "team", "agent", target.agent_name,
                                  frozenset({"consume"}))
    command = wire.HostedClaimRequest(
        request_id=request_id, invocation_id="original-native-invocation",
        identity=wire.HostedIdentity(**{field: getattr(target, field) for field in (
            "agent_id", "agent_name", "agent_version", "session_id")}),
        process_id="original-supervisor",
        launch_permit=SecretStr(h.native_calls[0].headers[wire.NATIVE_LAUNCH_PERMIT_HEADER]),
    )
    return h.native.claim(launch.api_binding_id, launch.launch_id, command, principal)


def test_version_refusal_fences_the_original_launch_and_records_failed_not_cancelled(unclaimed):
    h, job_id, receipt, binding, launch = unclaimed
    assert h.native.pending_version_observation(h.workers.inspect(job_id))
    evidence_id = h.native.fence_unclaimed(binding, observed_agent_version="999")
    assert h.native.pending_version_observation(h.workers.inspect(job_id))
    evidence = h.native.verify(evidence_id)
    assert evidence.state == "failed" and evidence.result.state == "failed"
    assert json.loads(evidence.result.content)["code"] == SESSION_VERSION_CHANGED
    assert json.loads(evidence.proof)["no_provider_stop_claim"] is True
    assert json.loads(evidence.proof)["cancel_requested"] is False
    h.workers.observe(job_id, evidence_id, CONTEXT)
    job = h.workers.inspect(job_id)
    assert job.state == "failed" and job.binding == binding and not job.cancel_requested
    assert not job.result_forwarded and h.native.pending_version_observation(job)
    assert not h.occupied()
    assert session_version_failure(h.store, job) == SESSION_VERSION_CHANGED
    assert h.store.reconcile(launch.start.request) == receipt
    with pytest.raises(Problem):
        claim(h, launch, "late-claim")
    h.time += timedelta(seconds=61)
    refreshed = h.foundry.query(binding)
    assert h.native.verify(refreshed).result == evidence.result
    assert len(h.native_calls) == 1
    assert all(not runtime.supervisor.children for runtime in h.runtimes.values())


def test_claimed_launch_is_not_reclassified_as_a_pre_sdk_version_failure(unclaimed):
    h, job_id, _, binding, launch = unclaimed
    claim(h, launch, "owned-claim")
    assert not h.native.pending_version_observation(h.workers.inspect(job_id))
    with pytest.raises(Problem) as refused:
        h.native.fence_unclaimed(binding, observed_agent_version="999")
    assert refused.value.body["error"]["code"] == "NATIVE_CLAIM_ALREADY_OWNED"
    assert h.workers.inspect(job_id).state == "starting" and len(h.occupied()) == 1
    assert h.native.launch(launch.launch_id).phase == "claimed"
    key = native_key("unclaimed-fence", launch.launch_id)
    assert h.store.read_snapshot((key,))[key] is None


def test_retained_assignment_material_prevents_pre_sdk_failure(unclaimed):
    h, job_id, _, binding, launch = unclaimed
    sid = assistant_key("session-id", binding.start_request_id)
    key = assistant_key("session", sid)
    request = ControlRequest(request_id="retain-unknown-assignment", request_digest="a" * 64, read_set=(key,))
    h.store.transact(request, lambda _: ControlChanges(
        writes=(ControlWrite(row_id=key, mode="create", data={"kind": "retained-opaque-assignment"}),),
        result={"retained": True},
    ))
    with pytest.raises(Problem) as refused:
        h.native.fence_unclaimed(binding, observed_agent_version="999")
    assert refused.value.body["error"]["code"] == "NATIVE_VERSION_REFUSAL_UNRESOLVED"
    assert h.workers.inspect(job_id).state == "starting" and len(h.occupied()) == 1
    assert h.native.launch(launch.launch_id).phase == "post_unknown"
    assert h.store.read_snapshot((key,))[key] is not None


def test_registered_but_not_enqueued_instruction_keeps_its_original_pending_error(assistant_api):
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from work_policy_lifecycle_helpers import enabled
    h, control, human, _ = enabled(assistant_api)
    command = InstructionSubmit(request_id="registered-not-enqueued", content="Keep the original registration.")
    record = control.register(human, command.request_id,
                              hashlib.sha256(command.model_dump_json().encode()).hexdigest(), instruction=command)
    cid = record.profile.instruction.conversation_id
    preserved = h.store.read_snapshot((control.registry_key, control.cost_key))
    with pytest.raises(Problem) as pending:
        control.submit(human, InstructionSubmit(
            request_id="no-replacement-for-pending", conversation_id=cid, content="Do not replace the pending work."))
    assert pending.value.status == 409
    assert pending.value.body["error"]["code"] == "CONVERSATION_TURN_UNRESOLVED"
    assert h.store.read_snapshot((control.registry_key, control.cost_key)) == preserved


def test_native_admission_branch_rejects_drift_before_creating_any_new_record(assistant, monkeypatch):
    import httpx
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.behavior_foundry import FoundryExecutor, FoundryHttpClient, NativeJobRoute, NativeLaunchAuthority
    from test_behavior_foundry import TARGET
    from test_foundry_start_diagnostics import Credential, SessionTransport, inject_sdk
    from work_policy_lifecycle_helpers import enabled

    deployed_versions(monkeypatch, TARGET.agent_version)
    h, control, human, _ = enabled(assistant)
    sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Retained reply before platform drift."}])
    first, _ = control.submit(human, InstructionSubmit(request_id="admission-first", content="Keep this history."))
    assert h.runtime.controller.step(first.binding_id).state == "succeeded"
    record = control.records[first.binding_id]
    target = TARGET.model_copy(update={"executor_id": h.workers.contract.executor_id, "agent_oid": first.agent_oid})
    native = NativeLaunchAuthority(
        h.workers, team_id=first.team_id, controller_id="internal-controller", targets=(target,),
        jobs=(NativeJobRoute(job_id=first.job_id, api_binding_id=first.binding_id, grant_id="unused"),),
        verifier_version=h.config.verifier_version, clock=lambda: h.time,
    )
    credential = Credential()
    transport = SessionTransport(session_change={
        "agent_session_id": record.hosted_session_id,
        "version_indicator": {"type": "version_ref", "agent_version": "999"},
    })
    inject_sdk(monkeypatch, credential, transport)
    client = FoundryHttpClient(target, credential, transport=httpx.MockTransport(
        lambda _: pytest.fail("admission refusal cannot invoke")))
    executor = FoundryExecutor(native, (client,))
    before = h.transport.backing.rows.copy()
    try:
        # Exercise the native pre-admission branch only; never relabel or write persisted offline records.
        with monkeypatch.context() as branch:
            branch.setattr(h.runtime, "mode", "native")
            branch.setattr(h.runtime, "dep", replace(h.runtime.dep, native_launches=native))
            branch.setattr(h.workers, "adapter", replace(h.workers.adapter, start=executor.start))
            with pytest.raises(Problem) as denied:
                control.submit(human, InstructionSubmit(
                    request_id="refused-before-registration",
                    conversation_id=record.profile.instruction.conversation_id, content="Do not change version."))
            assert denied.value.status == 409
            assert denied.value.body["error"]["code"] == SESSION_VERSION_CHANGED
        assert h.transport.backing.rows == before
        assert len(transport.calls) == 1 and transport.calls[0]["method"] == "GET"
    finally:
        client.close()


def test_native_admission_never_wakes_an_idle_legacy_conversation_session(assistant, monkeypatch):
    """ADR-0009 §10: an idle fa-chat SID is not invoked; reset or a new conversation stays explicit."""
    import httpx
    from shared_context.behavior_assistant_wire import InstructionSubmit, PROFILE
    from shared_context.behavior_foundry import (
        FoundryExecutor, FoundryHttpClient, NativeExecutionGrant, NativeJobRoute, NativeLaunchAuthority,
    )
    from test_behavior_foundry import TARGET
    from test_foundry_start_diagnostics import Credential, SessionTransport, inject_sdk
    from work_policy_lifecycle_helpers import enabled

    deployed_versions(monkeypatch, TARGET.agent_version)
    h, control, human, _ = enabled(assistant)
    sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Retained reply before the session went idle."}])
    first, _ = control.submit(human, InstructionSubmit(request_id="idle-first", content="Keep this history."))
    assert h.runtime.controller.step(first.binding_id).state == "succeeded"
    record = control.records[first.binding_id]
    assert record.hosted_session_id.startswith("fa-chat-")
    target = TARGET.model_copy(update={"executor_id": h.workers.contract.executor_id, "agent_oid": first.agent_oid})
    native = NativeLaunchAuthority(
        h.workers, team_id=first.team_id, controller_id="internal-controller", targets=(target,),
        jobs=(NativeJobRoute(job_id=first.job_id, api_binding_id=first.binding_id, grant_id="unused"),),
        verifier_version=h.config.verifier_version, clock=lambda: h.time,
    )
    native.assistant = h.assistant
    credential = Credential()
    # Same SID and version as recorded; only the platform status says the compute was reclaimed.
    transport = SessionTransport(session_change={"agent_session_id": record.hosted_session_id, "status": "idle"})
    inject_sdk(monkeypatch, credential, transport)
    invoked = []

    def invoke(request):
        invoked.append((request.method, request.url.path))
        return httpx.Response(503, json={"error": {"code": "OFFLINE_INVOKE", "message": "offline"}})

    client = FoundryHttpClient(target, credential, transport=httpx.MockTransport(invoke))
    executor = FoundryExecutor(native, (client,))
    verify_session, offline = FoundryHttpClient.verify_session, h.runtime.mode

    def admission_branch_only(self, *, before_send):
        # Only the native admission branch runs as native: the offline harness has no native final store guard,
        # so the rest of admission is offline and, without the metadata gate, the turn reaches the native start.
        try:
            return verify_session(self, before_send=before_send)
        finally:
            h.runtime.mode = offline

    before = h.transport.backing.rows.copy()
    try:
        with monkeypatch.context() as branch:
            branch.setattr(h.runtime, "mode", "native")
            branch.setattr(h.runtime, "dep", replace(h.runtime.dep, native_launches=native))
            branch.setattr(h.workers, "adapter", replace(h.workers.adapter, start=executor.start))
            branch.setattr(FoundryHttpClient, "verify_session", admission_branch_only)
            try:
                second, _ = control.submit(human, InstructionSubmit(
                    request_id="refused-idle-session",
                    conversation_id=record.profile.instruction.conversation_id, content="Continue."))
            except Problem as error:
                denied = error
            else:
                denied, grant_id = None, native_key("bootstrap-grant", second.job_id)
                h.seed([(native_key("grant", grant_id), NativeExecutionGrant(
                    grant_id=grant_id, controller_id=native.controller_id, owner=second.owner, policy_revision=1,
                    job_id=second.job_id, definition=second.work,
                    input_digest=hashlib.sha256(h.runtime.job_input(second)).hexdigest(),
                    targets=native.targets_for(second.job_id), active=True, remaining_uses=1,
                    not_before=h.time, expires_at=None, workload_version=PROFILE))])
                try:
                    h.workers.start(second.job_id, second.owner)
                except Problem:
                    pass
        assert invoked == [], "an idle legacy session cannot be invoked"
        assert denied is not None and denied.status == 409
        assert denied.body["error"]["code"] == "CONVERSATION_SESSION_UNAVAILABLE", denied.body
        assert h.transport.backing.rows == before
        assert [call["method"] for call in transport.calls] == ["GET"]
    finally:
        client.close()

