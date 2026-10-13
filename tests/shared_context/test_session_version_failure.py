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
from test_instruction_flow import instruction_models, runtime_path, sdk_io
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


@pytest.mark.parametrize("lost_start,observation", [
    (False, "already_changed"), (False, "changed"), (True, "changed"), (True, "late_changed"),
    (True, "same"), (True, "unavailable"), (False, "forward_pending"),
])
def test_instruction_version_failure_preserves_prior_reply_and_original_identity(
        assistant, monkeypatch, lost_start, observation):
    from types import SimpleNamespace
    from shared_context.behavior_assistant_wire import InstructionSubmit, PROFILE
    from shared_context.behavior_foundry import (
        FoundryExecutor, NativeExecutionGrant, NativeJobRoute, NativeLaunchAuthority,
    )
    from shared_context.behavior_instruction import conversation_key
    from test_behavior_foundry import TARGET
    from test_work_policy_lifecycle import enabled

    deployed_versions(monkeypatch, TARGET.agent_version)
    h, control, human, _ = enabled(assistant)
    io_calls = sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Original saved reply remains readable."}])
    first, _ = control.submit(human, InstructionSubmit(request_id="retained-first", content="Keep this reply."))
    assert h.runtime.controller.step(first.binding_id).state == "succeeded"
    cid = control.records[first.binding_id].profile.instruction.conversation_id
    prior_reply = control.read_reply(human, cid, 1)
    prior_model_calls = len(h.endpoint.requests)
    target = TARGET.model_copy(update={
        "executor_id": h.workers.contract.executor_id, "agent_oid": first.agent_oid,
    })
    native = NativeLaunchAuthority(
        h.workers, team_id=first.team_id, controller_id="internal-controller", targets=(target,),
        jobs=(NativeJobRoute(job_id=h.binding.job_id, api_binding_id=h.binding.binding_id, grant_id="unused-native"),),
        verifier_version=h.config.verifier_version, clock=lambda: h.time,
    )
    native.assistant = h.assistant
    h.runtime.dep = replace(h.runtime.dep, native_launches=native)
    second, _ = control.submit(human, InstructionSubmit(
        request_id="original-version-refusal", conversation_id=cid, content="Continue in this same environment."))
    key = conversation_key(cid)
    before = h.store.read_snapshot((key,))[key]
    record = control.records[second.binding_id]
    grant_id = native_key("bootstrap-grant", second.job_id)
    grant = NativeExecutionGrant(
        grant_id=grant_id, controller_id=native.controller_id, owner=second.owner, policy_revision=1,
        job_id=second.job_id, definition=second.work,
        input_digest=hashlib.sha256(h.runtime.job_input(second)).hexdigest(),
        targets=native.targets_for(second.job_id), active=True, remaining_uses=1,
        not_before=h.time, expires_at=None, workload_version=PROFILE,
    )
    h.seed([(native_key("grant", grant_id), grant)])
    calls, platform = [], {"changed": observation == "already_changed"}

    class Client:
        credential = object()

        def __init__(self, selected):
            self.target = selected

        def for_target(self, selected):
            return Client(selected)

        def create_session(self, *, before_send):
            pytest.fail("an existing conversation cannot create a replacement session")

        def verify_session(self, *, before_send):
            before_send()
            calls.append("verify")
            if platform["changed"] and observation == "unavailable":
                raise Problem(409, "CONVERSATION_SESSION_UNAVAILABLE", "OFFLINE metadata read is unavailable")
            if platform["changed"] and observation in {"changed", "late_changed", "already_changed", "forward_pending"}:
                raise Problem(409, SESSION_VERSION_CHANGED, "OFFLINE version changed",
                              expected_agent_version=self.target.agent_version, observed_agent_version="999")
            return SimpleNamespace(status="idle", agent_session_id=self.target.session_id)

        def start(self, *args, before_send=None):
            if before_send is not None:
                before_send()
            calls.append("invoke")
            platform["changed"] = observation != "late_changed"
            if lost_start:
                raise Problem(503, "FOUNDRY_INVOCATION_UNKNOWN", "OFFLINE original response unknown")
            return SimpleNamespace(invocation_id="original-invocation")

        def stop_session(self):
            pytest.fail("a version refusal cannot stop another platform version")

    executor = FoundryExecutor(native, (Client(target),))
    h.workers.adapter = replace(h.workers.adapter, start=executor.start, query=executor.query,
                                stop=executor.stop, verify=native.verify)
    if lost_start and observation != "changed":
        with pytest.raises(Problem) as unknown:
            h.workers.start(second.job_id, second.owner)
        assert unknown.value.body["error"]["code"] == "WORKER_START_UNKNOWN"
        assert h.workers.inspect(second.job_id).state == "starting"
        if observation not in {"changed", "late_changed"}:
            original = h.workers.inspect(second.job_id).binding
            with pytest.raises(Problem) as unresolved:
                h.workers.reconcile(second.job_id, second.owner)
            assert unresolved.value.body["error"]["code"] == "WORKER_QUERY_UNKNOWN"
            assert h.workers.inspect(second.job_id).state == "starting"
            assert h.workers.inspect(second.job_id).binding == original
            assert session_version_failure(h.store, h.workers.inspect(second.job_id)) is None
            assert h.store.read_snapshot((key,))[key] == before
            assert control.read_reply(human, cid, 1) == prior_reply
            assert calls.count("invoke") == 1 and len(io_calls) == 1
            assert len(h.endpoint.requests) == prior_model_calls
            return
        platform["changed"] = True
        from shared_context.behavior_work_policy_api import install_work_policy_routes
        scheduled = []
        def observe_original(binding_id):
            scheduled.append(binding_id)
            h.runtime.controller.step(binding_id)
        install_work_policy_routes(
            h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier, installation=lambda: None),
            start_work=observe_original,
        )
        response = h.client.get(f"/work-policy/v1/conversations/{cid}", headers=h.headers())
        assert response.status_code == 200, response.text
        assert response.json()["turns"][-1]["state"] == "starting"
        assert scheduled == [second.binding_id]
    else:
        h.workers.start(second.job_id, second.owner)
    job = h.workers.inspect(second.job_id)
    assert job.state == "failed" and not job.cancel_requested and job.generation == 1
    if observation == "forward_pending":
        import asyncio
        from contextlib import ExitStack
        from threading import Event
        import httpx
        from shared_context.behavior_server import ServerComposition
        from shared_context.behavior_work_policy_api import install_work_policy_routes

        original_result = native.verify(job.last_evidence_id).result
        original_forward = h.workers.forward_result
        original_record = h.runtime.dep.delivery.record_work_result
        forwards, scheduled, receipts = [], [], []
        entered, release = Event(), Event()

        def forward_once(job_id, owner, delivery):
            forwards.append(job_id)
            return original_forward(job_id, owner, delivery)

        def lose_first_record_response(command, owner):
            receipt = original_record(command, owner)
            receipts.append(receipt)
            if len(receipts) == 1:
                entered.set()
                assert release.wait(10)
                raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "OFFLINE failure forwarding is unconfirmed")
            return receipt

        monkeypatch.setattr(h.workers, "forward_result", forward_once)
        monkeypatch.setattr(h.runtime.dep.delivery, "record_work_result", lose_first_record_response)
        composition = ServerComposition(None, h.runtime, None, None, (), ExitStack())

        def observe_original(binding_id):
            composition.launch_instruction(binding_id)
            scheduled.append(composition._instruction_tasks[binding_id])

        install_work_policy_routes(
            h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier, installation=lambda: None),
            start_work=observe_original,
        )

        async def query_until_forwarded():
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://offline") as browser:
                    path = f"/work-policy/v1/conversations/{cid}"
                    first_read = await browser.get(path, headers=h.headers())
                    assert first_read.status_code == 200
                    assert first_read.json()["follow_up_blocker"] == "CONVERSATION_FAILURE_DELIVERY_PENDING"
                    assert len(scheduled) == 1, "failed original work must be reattached to its existing observer"
                    assert await asyncio.to_thread(entered.wait, 10)
                    repeat = await browser.get(path, headers=h.headers())
                    assert repeat.status_code == 200
                    assert scheduled[0] is scheduled[1] and forwards == [second.job_id]
                    release.set()
                    await asyncio.wait_for(asyncio.shield(scheduled[0]), 10)
                    assert not h.workers.inspect(second.job_id).result_forwarded
                    retry = await browser.get(path, headers=h.headers())
                    assert retry.status_code == 200
                    assert retry.json()["follow_up_blocker"] == "CONVERSATION_FAILURE_DELIVERY_PENDING"
                    await asyncio.wait_for(asyncio.shield(scheduled[-1]), 10)
                    assert h.workers.inspect(second.job_id).result_forwarded
                    scheduled_count = len(scheduled)
                    for _ in range(2):
                        completed = await browser.get(path, headers=h.headers())
                        assert completed.status_code == 200
                        assert completed.json()["follow_up_blocker"] == SESSION_VERSION_CHANGED
                    assert len(scheduled) == scheduled_count and forwards == [second.job_id, second.job_id]
                    assert len(receipts) == 2 and receipts[0] == receipts[1]
            finally:
                release.set()
                await composition.stop_instruction_observers()
                composition.close()

        asyncio.run(query_until_forwarded())
        assert native.verify(job.last_evidence_id).result == original_result
    if not h.workers.inspect(second.job_id).result_forwarded:
        h.workers.forward_result(second.job_id, second.owner, h.runtime.dep.delivery)
    assert h.workers.inspect(second.job_id).result_forwarded
    assert h.store.read_snapshot((key,))[key] == before
    assert control.read_reply(human, cid, 1) == prior_reply
    projection = control.conversation(human, cid)
    assert projection["follow_up_blocker"] == SESSION_VERSION_CHANGED and not projection["can_follow_up"]
    assert projection["turns"][-1]["request_id"] == "original-version-refusal"
    assert projection["turns"][-1]["state"] == "failed"
    preserved = h.store.read_snapshot((control.registry_key, control.cost_key))
    with pytest.raises(Problem) as denied:
        control.submit(human, InstructionSubmit(
            request_id="do-not-change-session", conversation_id=cid, content="No automatic version migration."))
    assert denied.value.body["error"]["code"] == SESSION_VERSION_CHANGED
    assert h.store.read_snapshot((control.registry_key, control.cost_key)) == preserved
    assert calls.count("invoke") == (0 if observation == "already_changed" else 1) and len(io_calls) == 1
    assert len(h.endpoint.requests) == prior_model_calls
    assert control.records[second.binding_id] == record


def test_registered_but_not_enqueued_instruction_keeps_its_original_pending_error(assistant):
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from test_work_policy_lifecycle import enabled
    h, control, human, _ = enabled(assistant)
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
    assert h.endpoint.requests == []


def test_native_admission_branch_rejects_drift_before_creating_any_new_record(assistant, monkeypatch):
    import httpx
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.behavior_foundry import FoundryExecutor, FoundryHttpClient, NativeJobRoute, NativeLaunchAuthority
    from test_behavior_foundry import TARGET
    from test_foundry_start_diagnostics import Credential, SessionTransport, inject_sdk
    from test_work_policy_lifecycle import enabled

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
    from test_work_policy_lifecycle import enabled

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


def test_routine_version_failures_do_not_consume_the_manual_cancellation_limit():
    from shared_context.behavior_migration import _UnclaimedFence
    from shared_context.behavior_recovery_history import _check_unclaimed_fence_limit
    manual = _UnclaimedFence(
        launch_id="manual-original", original_start_request_id="manual-start",
        cancel_requested=True, claim_never_issued=True, no_provider_stop_claim=True,
    )
    failures = tuple(_UnclaimedFence(
        launch_id=f"version-{number}", original_start_request_id=f"start-{number}",
        cancel_requested=False, claim_never_issued=True, no_provider_stop_claim=True,
        failure_code=SESSION_VERSION_CHANGED, observed_agent_version="999",
    ) for number in range(33))
    _check_unclaimed_fence_limit(failures)
    _check_unclaimed_fence_limit((*failures, *(manual for _ in range(32))))
    with pytest.raises(Problem) as limited:
        _check_unclaimed_fence_limit((*failures, *(manual for _ in range(33))))
    assert limited.value.body["error"]["code"] == "MIGRATION_RECOVERY_HISTORY_INVALID"
