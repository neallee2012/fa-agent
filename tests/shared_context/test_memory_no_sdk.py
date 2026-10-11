"""Memory refusals before the original SDK gate; real signed authority, no model."""

import json
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event
from types import MappingProxyType, SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient

from conftest import A
from shared_context import behavior_assistant_hosted as hosted
from shared_context import behavior_wire as wire
from shared_context import m2_copilot
from shared_context.behavior_assistant import AssistantAuthority, AssistantProfile, _LocalHttp, key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_foundry import FoundryTarget, NativeExecutionGrant, NativeJobRoute, NativeLaunchAuthority, native_key
from shared_context.behavior_hosted import BehaviorExecutorClient, HostedInvocation
from shared_context.behavior_wire import HostedIdentity
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import check_history, instruction_models, runtime_path
from test_instruction_prompt import submit
from test_m3_auth import jwk_client, signing_key
from test_reviewed_work_memory import configured_memory, reviewed_note, revoke_review, withdraw_source
from test_sdk_compaction_product import open_turn, ordinary, settle
from test_sdk_no_start import assert_failure_available, report_packet, post_native_report

PROFILE_V2 = "fa-sdk-start-gate-v2"


def configured_memory_gate(assistant, profile="fa-sdk-start-gate-v1"):
    reviewed_note(assistant)
    assistant.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({
            **value.model_dump(), "prompt_profile": "fa-instruction-work-memory-v1", "sdk_start_profile": profile,
        })
        for name, value in assistant.runtime.profiles.items()
    })
    return configured_memory(assistant)


def save_proof(name, h, binding, *, constructors, source_code, observer=None):
    if not os.environ.get("FA_MEMORY_NO_SDK_EVIDENCE_DIR"):
        return
    row = h.store.read_snapshot((key("no-sdk-turn", binding.job_id),))[key("no-sdk-turn", binding.job_id)]
    proof = None if row is None else row.payload["data"]
    target = Path(os.environ["FA_MEMORY_NO_SDK_EVIDENCE_DIR"])
    target.mkdir(parents=True, exist_ok=True)
    (target / (name + ".json")).write_text(json.dumps({
        "source_refusal": source_code,
        "sdk_constructor_calls": len(constructors), "offline_model_requests": len(h.endpoint.requests),
        "job_state": h.workers.inspect(binding.job_id).state,
        "result_forwarded": h.workers.inspect(binding.job_id).result_forwarded,
        "no_sdk_schema": None if proof is None else proof["schema_version"],
        "disposition": None if proof is None else proof["disposition"],
        "no_sdk_turn_sha256": None if proof is None else hashlib.sha256(
            json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "new_turn_archive": h.store.read_snapshot((key("text-archive", binding.job_id),))[key("text-archive", binding.job_id)] is not None,
        "observer_phase": None if observer is None else observer.phase,
        "observer_sdk_stop": None if observer is None else observer.sdk_stop,
        "observer_child_pid": None if observer is None else observer.child_pid,
        "cloud_acceptance": "NOT_RUN", "paid_model_requests": 0,
    }, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("profile", ["fa-sdk-start-gate-v1", PROFILE_V2])
def test_memory_refusal_before_sdk_entry_reaches_original_failed_result(assistant, monkeypatch, profile):
    h, control, human = configured_memory_gate(assistant, profile)
    first = open_turn(h, control, human, "memory-no-sdk-first")
    constructors, failures = [], []
    try:
        first.client.work_memory_catalog()
        state = ordinary(first.assignment.sdk_session_id)
        settle(first, state)
        prior_keys = (key("text-archive", first.binding.job_id), key("result", first.binding.work.work_id))
        prior = h.store.read_snapshot(prior_keys)
        h.workers.adapter = h.executor.adapter()
        acquire = hosted.AssistantClient.acquire
        restore = hosted.AssistantClient.restore

        def acquire_then_withdraw(client):
            result = acquire(client)
            withdraw_source(h, client.assignment.binding_id)
            return result

        def observed_restore(client):
            try:
                return restore(client)
            except Problem as error:
                failures.append(error.body["error"]["code"])
                raise

        def forbidden(*args, **kwargs):
            constructors.append(True)
            raise AssertionError("A verified pre-entry source refusal must not construct SDK.")

        monkeypatch.setattr(hosted.AssistantClient, "acquire", acquire_then_withdraw)
        monkeypatch.setattr(hosted.AssistantClient, "restore", observed_restore)
        monkeypatch.setattr(m2_copilot, "CopilotClient", forbidden)
        accepted, command = submit(h, "memory-no-sdk-second", "  Retain the refused original user task.\r\n",
                                   first.assignment.task.instruction.conversation_id)
        binding = h.runtime.bindings[accepted["binding_id"]]
        refusal = None
        try:
            result = h.runtime.controller.step(binding.binding_id)
        except Problem as error:
            refusal = error.body["error"]["code"]
            result = h.workers.inspect(binding.job_id)
        job = h.workers.inspect(binding.job_id)
        sid = key("session-id", job.binding.start_request_id)
        from shared_context.behavior_hosted import LocalJournal
        journal = LocalJournal(h.executor.root / sid)
        with journal.connect() as db:
            gate = json.loads(db.execute("SELECT body FROM assistant_sdk_start_gates").fetchone()[0])
        assert constructors == [] and h.endpoint.requests == [] and failures == ["SOURCE_WITHDRAWN"]
        assert control.records[binding.binding_id].profile.instruction.content == command.content
        assert h.store.read_snapshot(prior_keys) == prior
        if os.environ.get("FA_MEMORY_NO_SDK_EVIDENCE_DIR"):
            target = Path(os.environ["FA_MEMORY_NO_SDK_EVIDENCE_DIR"])
            target.mkdir(parents=True, exist_ok=True)
            (target / f"local-preentry-{profile}.json").write_text(json.dumps({
                "state": result.state, "controller_error": refusal,
                "sdk_constructor_calls": 0, "offline_model_requests": 0,
                "original_source_refusal": failures[0], "gate_state": gate["state"],
                "gate_profile": gate["binding"]["profile"],
                "result_forwarded": job.result_forwarded,
                "prior_archive_and_result_retained": True,
                "user_task_retained": True,
                "proof": "Counts alone do not establish terminality; the positive gate/observer proof is separate.",
            }, indent=2) + "\n", encoding="utf-8")
        if profile == "fa-sdk-start-gate-v1":
            assert result.state == "starting" and refusal == "WORKER_START_UNKNOWN"
            assert gate["state"] == "open" and not job.result_forwarded
            return
        assert result.state == "failed" and result.result_forwarded, {
            "controller_error": refusal, "state": result.state, "gate_state": gate["state"],
        }
        assert_failure_available(h, binding)
        assert gate["schema_version"] == "fa-sdk-start-gate-record-v2"
        assert gate["state"] == "closed_before_entry" and gate["refusal_code"] == "SOURCE_WITHDRAWN"
        saved = h.store.read_snapshot((key("no-sdk-turn", binding.job_id),))[key("no-sdk-turn", binding.job_id)]
        assert saved.payload["data"]["schema_version"] == "fa-no-sdk-turn-v2"
        assert saved.payload["data"]["disposition"]["schema_version"] == "fa-sdk-no-start-v2"
        assert saved.payload["data"]["disposition"]["owned_thread_completed"] is True
        assert h.store.read_snapshot((key("text-archive", binding.job_id),))[key("text-archive", binding.job_id)] is None
        proof = json.loads(h.assistant.result(job.binding).proof)
        assert proof["schema_version"] == "fa-sdk-no-start-result-v2" and "sdk_stop" not in proof
        projection = h.client.get("/work-policy/v1/conversations/" + accepted["conversation_id"], headers=h.headers())
        assert projection.status_code == 200 and projection.json()["can_reset"]
        assert not projection.json()["can_follow_up"]
        assert projection.json()["follow_up_blocker"] == "SDK_NOT_STARTED_RESET_REQUIRED"
        from web_test_console.freeform import ConversationProjection
        bridge_view = ConversationProjection.model_validate_json(projection.content)
        assert bridge_view.can_reset and not bridge_view.can_follow_up
        reset = h.client.post("/work-policy/v1/conversations/" + accepted["conversation_id"] + "/reset",
                             json={"request_id": "explicit-memory-reset"}, headers=h.headers())
        assert reset.status_code == 200 and reset.json()["archived"]
        assert h.store.read_snapshot(prior_keys) == prior
        check_history(h)
        save_proof("local-v2-closed-before-restore", h, binding, constructors=constructors, source_code=failures[0])
    finally:
        first.client.close()


@pytest.fixture
def native_memory_no_start(assistant, tmp_path, request, monkeypatch):
    profile = getattr(request, "param", PROFILE_V2)
    h, control, human = configured_memory_gate(assistant, profile)
    def no_runtime(*args, **kwargs):
        raise AssertionError("Synthetic Hosted journal tests do not launch a runtime.")
    monkeypatch.setattr(m2_copilot, "CopilotClient", no_runtime)
    first = open_turn(h, control, human, "memory-hosted-first")
    api = supervisor = None
    try:
        first.client.work_memory_catalog()
        settle(first, ordinary(first.assignment.sdk_session_id))
        template = FoundryTarget(
            executor_id=h.workers.contract.executor_id, project_endpoint="https://native.invalid/api/projects/test",
            agent_id="11111111-1111-1111-1111-111111111111", agent_name="assistant",
            agent_version="8", session_id="memory-no-sdk-template", agent_oid=A.oid,
        )
        native = NativeLaunchAuthority(
            h.workers, team_id=A.team, controller_id="internal-controller", targets=(template,),
            jobs=(NativeJobRoute(job_id="job-one", api_binding_id="binding-one", grant_id="fixture-grant"),),
            verifier_version=h.config.verifier_version, clock=lambda: h.time,
        )
        h.runtime.dep = replace(h.runtime.dep, native_launches=native)
        h.assistant = AssistantAuthority(h.runtime, h.artifacts, (A,))
        provider_hooks, provider_requests = [], []
        def provider_token(_):
            for hook in provider_hooks:
                hook()
            return SimpleNamespace(token="OFFLINE_MEMORY_NO_SDK_PROVIDER")
        def forbidden_provider(request):
            provider_requests.append(True)
            raise AssertionError("This no-SDK contract test must not send to a model.")
        gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
            SimpleNamespace(get_token=provider_token), transport=httpx.MockTransport(forbidden_provider))
        h.client = TestClient(create_assistant_app(h.assistant, h.verifier, provider=gateway))
        install_work_policy_routes(h.client.app, SimpleNamespace(
            runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
            require_current_native_evidence=lambda: None,
        ))
        accepted, command = submit(h, "memory-hosted-next", "  Retain this original failed memory task.\r\n",
                                   first.assignment.task.instruction.conversation_id)
        binding = h.runtime.bindings[accepted["binding_id"]]
        control.refresh()
        target = native.targets_for(binding.job_id)[0]
        job = h.workers.inspect(binding.job_id)
        grant = native.jobs[binding.job_id].grant_id
        h.seed([(native_key("grant", grant), NativeExecutionGrant(
            grant_id=grant, controller_id="internal-controller", owner=job.owner,
            policy_revision=1, job_id=binding.job_id, definition=binding.work,
            input_digest=hashlib.sha256(job.spec.input).hexdigest(), targets=(target,),
            active=True, remaining_uses=1, not_before=h.time - timedelta(minutes=1),
            expires_at=h.time + timedelta(minutes=5), workload_version="assistant-v1.2",
        ))])
        api = BehaviorExecutorClient("https://context.invalid", h.verifier.auth.audience,
            SimpleNamespace(get_token=lambda _: SimpleNamespace(
                token=h.headers("agent")["Authorization"].removeprefix("Bearer "))),
            transport=_LocalHttp(h.client))
        supervisor = hosted.AssistantSupervisor(tmp_path / "owned-memory-host", runtime_path=Path(__file__))
        identity = HostedIdentity(**{field: getattr(target, field)
                                     for field in ("agent_id", "agent_name", "agent_version", "session_id")})
        host = hosted.AssistantHostedRuntime(identity, supervisor, api, clock=lambda: h.time)
        invocation, starts = "synthetic-memory-no-sdk-invocation", []
        def start_assigned(start):
            launch, permit = native.prepare(start)
            value = HostedInvocation(binding_id=binding.binding_id, launch_id=launch.launch_id, launch_permit=permit)
            starts.append(value)
            host.start(value, invocation, identity.session_id)
        def probe_original(execution):
            probe = native.probe(execution)
            return host.observe(invocation, identity.session_id, probe.probe_id).evidence_id
        h.workers.adapter = replace(h.workers.adapter, start=start_assigned, query=probe_original, verify=native.verify)
        yield SimpleNamespace(h=h, control=control, human=human, first=first, binding=binding, accepted=accepted,
            command=command, native=native, supervisor=supervisor, host=host, identity=identity,
            invocation=invocation, starts=starts, api=api, provider_hooks=provider_hooks, provider_requests=provider_requests)
    finally:
        if supervisor is not None:
            supervisor.join(timeout=5)
        if api is not None:
            api.close()
        first.client.close()


def close_memory_entry(c, monkeypatch, *, change="withdrawn", join=True):
    acquire = hosted.AssistantClient.acquire
    constructors = []
    def after_ack(client):
        result = acquire(client)
        if change == "withdrawn":
            withdraw_source(c.h, client.assignment.binding_id)
        elif change == "version":
            reviewed_note(c.h, version=3, request_id="post-acquire-version", binding_id=client.assignment.binding_id)
        elif change == "text-policy":
            from shared_context.behavior_runtime import runtime_key
            c.h.mutate(runtime_key("policy", client.assignment.binding_id),
                       human_text_enabled=False, text_policy_version=None)
        elif change == "scope":
            from shared_context.behavior_runtime import runtime_key
            c.h.mutate(runtime_key("policy", client.assignment.binding_id), source_visibility="private")
        elif change in {"review-revoked", "claim-revoked"}:
            context, claim = revoke_review(c.h, client.assignment.binding_id)
            if change == "claim-revoked":
                c.h.memory.revoke(context, claim.reference, reason="Synthetic withdrawal of this reviewed work note.")
        elif change == "expired":
            reviewed_note(c.h, version=3, request_id="post-acquire-expiry", binding_id=client.assignment.binding_id,
                          expires_at=c.h.time + timedelta(minutes=1))
            c.h.time += timedelta(minutes=2)
        elif change == "source-version":
            from test_behavior_api import METRICS
            path = "/behavior/v1/bindings/" + client.assignment.binding_id + "/sources/metrics/"
            prepared = c.h.client.post(path + "prepare", headers=c.h.headers(), content=wire.SourcePrepare(
                request_id="new-source-same-bytes", version=2, original=METRICS).model_dump_json())
            assert prepared.status_code == 200, prepared.text
            committed = c.h.client.post(path + "commit", headers=c.h.headers(),
                content=wire.Command(request_id="new-source-same-bytes").model_dump_json())
            assert committed.status_code == 200, committed.text
        else:
            raise AssertionError("unknown fixture change")
        return result
    def forbidden(*args, **kwargs):
        constructors.append(True)
        raise AssertionError("Memory refusal must precede SDK construction.")
    monkeypatch.setattr(hosted.AssistantClient, "acquire", after_ack)
    monkeypatch.setattr(m2_copilot, "CopilotClient", forbidden)
    c.h.workers.start(c.binding.job_id, c.binding.owner)
    if join:
        c.supervisor.join(timeout=5)
    return c.supervisor.observe(c.starts[0].launch_id), constructors


@pytest.mark.parametrize("change,code", [
    ("withdrawn", "SOURCE_WITHDRAWN"), ("version", "WORK_MEMORY_SOURCE_CHANGED"),
    ("text-policy", "BEHAVIOR_UNSAFE_CONTENT"),
    ("scope", "WORK_MEMORY_SCOPE_DENIED"), ("review-revoked", "HUMAN_REVIEW_REVOKED"),
    ("claim-revoked", "MEMORY_CLAIM_REVOKED"), ("expired", "MEMORY_CLAIM_EXPIRED"),
    ("source-version", "SOURCE_STALE"),
])
def test_hosted_memory_disposition_reaches_signed_report_and_original_failure(native_memory_no_start, monkeypatch, change, code):
    c = native_memory_no_start
    prior_keys = (key("text-archive", c.first.binding.job_id), key("result", c.first.binding.work.work_id))
    old = c.h.store.read_snapshot(prior_keys)
    observed, constructors = close_memory_entry(c, monkeypatch, change=change)
    assert observed.phase == "sdk_not_started", observed.model_dump(mode="json")
    assert observed.sdk_stop is None and observed.child_pid is None and not observed.child_exited
    assert observed.sdk_no_start.schema_version == "fa-sdk-no-start-v2"
    assert observed.sdk_no_start.gate.refusal_code == code
    assert constructors == [] and c.h.endpoint.requests == []
    with c.supervisor.journal.connect() as db:
        failure = json.loads(db.execute("SELECT body FROM assistant_failures").fetchone()[0])
    assert failure["code"] == code
    c.h.workers.reconcile(c.binding.job_id, c.binding.owner)
    done = c.h.runtime.controller.step(c.binding.binding_id)
    assert done.state == "failed" and done.result_forwarded and not done.obligation_fulfilled
    assert c.h.store.read_snapshot(prior_keys) == old
    assert c.control.records[c.binding.binding_id].profile.instruction.content == c.command.content
    assert_failure_available(c.h, c.binding)
    check_history(c.h)
    projection = c.h.client.get("/work-policy/v1/conversations/" + c.accepted["conversation_id"], headers=c.h.headers())
    assert projection.status_code == 200 and projection.json()["can_reset"]
    assert not projection.json()["can_follow_up"]
    assert projection.json()["follow_up_blocker"] == "SDK_NOT_STARTED_RESET_REQUIRED"
    assert c.host.start(c.starts[0], c.invocation, c.identity.session_id)["status"] == "pending"
    assert constructors == []
    save_proof("hosted-v2-" + change, c.h, c.binding, constructors=constructors, source_code=code, observer=observed)


@pytest.mark.parametrize("native_memory_no_start,change", [
    (None, "withdrawn"), ("fa-sdk-start-gate-v1", "withdrawn"),
    (None, "text-policy"), ("fa-sdk-start-gate-v1", "text-policy"),
], indirect=["native_memory_no_start"])
def test_old_or_disabled_profile_does_not_gain_memory_terminal_permission(native_memory_no_start, monkeypatch, change):
    c = native_memory_no_start
    observed, constructors = close_memory_entry(c, monkeypatch, change=change)
    assert observed.phase == "spawn_unknown" and observed.sdk_no_start is None and observed.sdk_stop is None
    c.h.workers.reconcile(c.binding.job_id, c.binding.owner)
    assert c.h.workers.inspect(c.binding.job_id).state == "capacity_uncertain"
    assert constructors == [] and c.h.endpoint.requests == []
    assert c.h.store.read_snapshot((key("no-sdk-turn", c.binding.job_id),))[key("no-sdk-turn", c.binding.job_id)] is None
    if change == "text-policy":
        with c.supervisor.journal.connect() as db:
            failure = json.loads(db.execute("SELECT body FROM assistant_failures").fetchone()[0])
        assert failure["code"] == "ASSISTANT_API_UNKNOWN"
        assert failure.get("upstream_error_code") in {None, "UNCLASSIFIED"}


@pytest.mark.parametrize("fault", ["withdraw-after-capture", "capture-ack-lost"])
def test_metadata_capture_is_not_sdk_state_but_unknown_capture_ack_is_not_terminal(assistant, monkeypatch, fault):
    h, control, _ = configured_memory_gate(assistant, PROFILE_V2)
    command = h.assistant._command
    captured, constructors = [], []
    def capture_then_refuse(session, name, *args, **kwargs):
        result = command(session, name, *args, **kwargs)
        if name == "work-memory":
            captured.append(session.assignment.session_id)
            if fault == "capture-ack-lost":
                raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "Synthetic metadata capture acknowledgement lost.")
            withdraw_source(h, session.binding_id)
        return result
    def forbidden(*args, **kwargs):
        constructors.append(True)
        raise AssertionError("No pre-entry memory response is available to this SDK.")
    monkeypatch.setattr(h.assistant, "_command", capture_then_refuse)
    monkeypatch.setattr(m2_copilot, "CopilotClient", forbidden)
    h.workers.adapter = h.executor.adapter()
    accepted, user = submit(h, "memory-capture-gate", "  Save my original refused request.\r\n")
    binding = h.runtime.bindings[accepted["binding_id"]]
    if fault == "capture-ack-lost":
        with pytest.raises(Problem) as denied:
            h.runtime.controller.step(binding.binding_id)
        assert denied.value.body["error"]["code"] == "WORKER_START_UNKNOWN"
    else:
        result = h.runtime.controller.step(binding.binding_id)
        assert result.state == "failed" and result.result_forwarded
        assert_failure_available(h, binding)
        check_history(h)
    assert len(captured) == 1 and constructors == [] and h.endpoint.requests == []
    saved = h.store.read_snapshot((key("session", captured[0]),))[key("session", captured[0])]
    assert saved.payload["data"]["work_memory_source"]["claim_version"] == 2
    assert saved.payload["data"]["model_requests"] == 0
    assert control.records[binding.binding_id].profile.instruction.content == user.content
    nkey = key("no-sdk-turn", binding.job_id)
    no_sdk = h.store.read_snapshot((nkey,))[nkey]
    assert (no_sdk is None) == (fault == "capture-ack-lost")
    if fault == "capture-ack-lost":
        with pytest.raises(Problem) as unresolved:
            h.workers.reconcile(binding.job_id, binding.owner)
        assert unresolved.value.body["error"]["code"] == "OPERATION_RECORD_MISSING"
        assert h.workers.inspect(binding.job_id).state == "starting" and len(captured) == 1
        assert saved.payload["data"]["state"] == "running"
    save_proof(fault, h, binding, constructors=constructors,
               source_code="BEHAVIOR_COMMIT_UNKNOWN" if no_sdk is None else "SOURCE_WITHDRAWN")


def test_complete_signed_memory_source_over_response_bound_closes_only_v2_entry(assistant, monkeypatch):
    import tiktoken
    encoder = tiktoken.get_encoding("cl100k_base")
    h, _, _ = configured_memory_gate(assistant, PROFILE_V2)
    monkeypatch.setattr(h.memory, "_count", lambda data: len(encoder.encode(data.decode("utf-8"))))
    original_source, reviewed_text = b"\\" * 8192, "\\" * 4096
    source_digest = hashlib.sha256(original_source).hexdigest()
    acquire, constructors, created = hosted.AssistantClient.acquire, [], []
    def after_ack(client):
        result = acquire(client)
        path = "/behavior/v1/bindings/" + client.assignment.binding_id
        prepared = h.client.post(path + "/sources/dependency/prepare", headers=h.headers(), content=wire.SourcePrepare(
            request_id="full-memory-original", version=1, original=original_source, source_schema="fa-human-text-v1",
            retention_consent_sha256=source_digest,
        ).model_dump_json())
        assert prepared.status_code == 200, prepared.text
        committed = h.client.post(path + "/sources/dependency/commit", headers=h.headers(),
            content=wire.Command(request_id="full-memory-original").model_dump_json())
        assert committed.status_code == 200, committed.text
        reference = wire.ExactSourceRef(source_id="dependency", version=1,
            sha256=source_digest, byte_length=len(original_source))
        reviewed_note(h, text=reviewed_text, version=3, request_id="full-memory-claim",
                      supporting=(h.runtime.profiles[client.assignment.binding_id].source, reference),
                      binding_id=client.assignment.binding_id)
        created.append(reference)
        return result
    def forbidden(*args, **kwargs):
        constructors.append(True)
        raise AssertionError("The complete source cannot fit; no SDK should be constructed.")
    monkeypatch.setattr(hosted.AssistantClient, "acquire", after_ack)
    monkeypatch.setattr(m2_copilot, "CopilotClient", forbidden)
    h.workers.adapter = h.executor.adapter()
    accepted, _ = submit(h, "memory-full-response", "Read the exact reviewed note only if permitted.")
    binding = h.runtime.bindings[accepted["binding_id"]]
    done = h.runtime.controller.step(binding.binding_id)
    assert done.state == "failed" and done.result_forwarded
    assert constructors == [] and h.endpoint.requests == [] and len(created) == 1
    path = "/behavior/v1/bindings/" + binding.binding_id
    context = h.client.post(path + "/context", headers=h.headers(),
        content=wire.ContextRequest(request_id="original-memory-limit", required_claim_ids=("claim-one",)).model_dump_json())
    assert context.status_code == 200, context.text
    assert context.json()["claims"][0]["candidate"]["text"] == reviewed_text
    assert h.client.get(path + "/sources/dependency/versions/1", headers=h.headers()).content == original_source
    row = h.store.read_snapshot((key("no-sdk-turn", binding.job_id),))[key("no-sdk-turn", binding.job_id)]
    assert row.payload["data"]["disposition"]["gate"]["refusal_code"] == "WORK_MEMORY_SOURCE_LIMIT"
    from shared_context.behavior_work_memory import WORK_MEMORY_RESPONSE_BYTES
    assert WORK_MEMORY_RESPONSE_BYTES == 24576
    assert_failure_available(h, binding)
    check_history(h)
    save_proof("complete-source-bound", h, binding, constructors=constructors, source_code="WORK_MEMORY_SOURCE_LIMIT")


def test_closed_memory_gate_does_not_override_revoked_report_policy(native_memory_no_start, monkeypatch):
    from shared_context.behavior_runtime import runtime_key
    c = native_memory_no_start
    observed, constructors = close_memory_entry(c, monkeypatch)
    assert observed.phase == "sdk_not_started"
    probe, body = report_packet(c, observed)
    c.h.mutate(runtime_key("policy", c.binding.binding_id), active=False)
    before = c.h.store._guard()
    response = post_native_report(c, probe, body)
    assert response.status_code == 403, response.text
    assert c.h.store._guard() == before and not c.h.workers.inspect(c.binding.job_id).result_forwarded
    assert c.h.store.read_snapshot((key("no-sdk-turn", c.binding.job_id),))[key("no-sdk-turn", c.binding.job_id)] is None
    assert constructors == [] and c.h.endpoint.requests == []
    save_proof("current-report-policy-blocked", c.h, c.binding, constructors=constructors,
               source_code="SOURCE_WITHDRAWN", observer=observed)


@pytest.mark.parametrize("tamper", [
    "wrong-job", "wrong-sdk", "wrong-task", "wrong-config", "wrong-code", "wrong-supervisor",
    "wrong-claim", "wrong-generation", "entry-attempted", "thread-live", "thread-number",
    "missing-version", "future-version", "mixed-binding", "mixed-record", "mixed-disposition",
    "v1-skill-code", "unknown-code",
])
def test_v2_native_report_refuses_unbound_or_mixed_version_proof(native_memory_no_start, monkeypatch, tamper):
    c = native_memory_no_start
    observed, _ = close_memory_entry(c, monkeypatch)
    probe, body = report_packet(c, observed)
    value = body["sdk_no_start"]
    if tamper.startswith("wrong-"):
        field = {
            "wrong-job": "job_id", "wrong-sdk": "sdk_session_id", "wrong-task": "task_digest",
            "wrong-config": "worker_configuration_digest", "wrong-code": "code_sha256",
            "wrong-supervisor": "supervisor_id", "wrong-claim": "claim_id", "wrong-generation": "process_generation",
        }[tamper]
        target = value["gate"]["owner" if field in {"supervisor_id", "claim_id", "process_generation"} else "binding"]
        target[field] = 42 if field == "process_generation" else ("f" * 64 if field.endswith(("digest", "sha256")) else "different")
    elif tamper == "entry-attempted":
        value["gate"].update(state="entry_attempted", refusal_code=None)
    elif tamper in {"thread-live", "thread-number"}:
        value["owned_thread_completed"] = False if tamper == "thread-live" else 1
    elif tamper == "missing-version":
        value.pop("schema_version")
    elif tamper == "future-version":
        value["schema_version"] = "fa-sdk-no-start-future"
    elif tamper == "mixed-binding":
        value["gate"]["binding"]["schema_version"] = "fa-sdk-start-binding-v1"
    elif tamper == "mixed-record":
        value["gate"]["schema_version"] = "fa-sdk-start-gate-record-v1"
    elif tamper == "mixed-disposition":
        value["schema_version"] = "fa-sdk-no-start-v1"
    else:
        value["gate"]["refusal_code"] = "SKILL_SOURCE_CHANGED" if tamper == "v1-skill-code" else "SOURCE_REQUIRED_MISSING"
    guard = c.h.store._guard()
    response = post_native_report(c, probe, body)
    assert response.status_code in {403, 422}, response.text
    assert c.h.store._guard() == guard
    assert not c.h.workers.inspect(c.binding.job_id).result_forwarded
    assert c.h.store.read_snapshot((key("no-sdk-turn", c.binding.job_id),))[key("no-sdk-turn", c.binding.job_id)] is None


@pytest.mark.parametrize("attempt", ["construction", "start", "controlled-read-after-entry"])
def test_memory_refusal_after_sdk_entry_attempt_is_not_no_sdk_even_without_pid(native_memory_no_start, monkeypatch, attempt):
    c = native_memory_no_start
    calls = []
    async def start():
        calls.append("start")
        if attempt == "controlled-read-after-entry":
            withdraw_source(c.h, c.binding.binding_id)
            client = c.supervisor.clients[c.starts[0].launch_id]
            with pytest.raises(Problem) as refused:
                client.read_work_memory("claim-one")
            assert refused.value.body["error"]["code"] == "SOURCE_WITHDRAWN"
            calls.append("read-denied")
            raise refused.value
        raise Problem(403, "SOURCE_WITHDRAWN", "Synthetic refusal after the actual SDK entry point.")
    async def no_process_cleanup():
        return None
    def construct(**options):
        calls.append("construction")
        if attempt == "construction":
            raise Problem(403, "SOURCE_WITHDRAWN", "Synthetic construction refusal after entry intent.")
        return SimpleNamespace(start=start, stop=no_process_cleanup, force_stop=no_process_cleanup, _cli_process=None)
    monkeypatch.setattr(m2_copilot, "CopilotClient", construct)
    c.h.workers.start(c.binding.job_id, c.binding.owner)
    c.supervisor.join(timeout=5)
    observed = c.supervisor.observe(c.starts[0].launch_id)
    assert observed.phase == "spawn_unknown" and observed.sdk_no_start is None and observed.sdk_stop is None
    assert observed.child_pid is None and c.provider_requests == []
    assert calls == (["construction"] if attempt == "construction" else ["construction", "start"]
                     + (["read-denied"] if attempt == "controlled-read-after-entry" else []))
    with c.supervisor.journal.connect() as db:
        gate = json.loads(db.execute("SELECT body FROM assistant_sdk_start_gates").fetchone()[0])
    assert gate["state"] == "entry_attempted" and gate["refusal_code"] is None
    c.h.workers.reconcile(c.binding.job_id, c.binding.owner)
    assert c.h.workers.inspect(c.binding.job_id).state == "capacity_uncertain"
    before = list(calls)
    assert c.host.start(c.starts[0], c.invocation, c.identity.session_id)["status"] == "pending"
    assert calls == before


def test_closed_memory_gate_still_requires_its_original_thread_to_join(native_memory_no_start, monkeypatch):
    from shared_context.behavior_sdk_start import SdkStartGate
    c = native_memory_no_start
    closed, release = Event(), Event()
    original_close = SdkStartGate.close
    def close_and_wait(gate, code):
        original_close(gate, code)
        closed.set()
        assert release.wait(10)
    monkeypatch.setattr(SdkStartGate, "close", close_and_wait)
    try:
        close_memory_entry(c, monkeypatch, join=False)
        assert closed.wait(5)
        observed = c.supervisor.observe(c.starts[0].launch_id)
        assert observed.phase == "spawn_unknown" and observed.sdk_no_start is None
        release.set()
        c.supervisor.join(timeout=5)
        observed = c.supervisor.observe(c.starts[0].launch_id)
        assert observed.phase == "sdk_not_started" and observed.sdk_no_start.owned_thread_completed is True
    finally:
        release.set()
        c.supervisor.join(timeout=5)


@pytest.mark.parametrize("transition", ["reserve", "close", "observer"])
@pytest.mark.parametrize("lost_at", ["before-commit", "after-commit"])
def test_memory_gate_or_observer_unacknowledged_journal_is_not_terminal(native_memory_no_start, monkeypatch, transition, lost_at):
    c = native_memory_no_start
    original_connect, original_save = c.supervisor.journal.connect, c.supervisor.journal._save
    observer_write, losses = [], []
    def save(db, state):
        original_save(db, state)
        if transition == "observer" and state.phase == "sdk_not_started":
            observer_write.append(True)
    @contextmanager
    def connect():
        writes = []
        def traced(statement):
            if (transition == "reserve" and statement.startswith("INSERT INTO assistant_sdk_start_gates")
                    or transition == "close" and statement.startswith("UPDATE assistant_sdk_start_gates")
                    and "closed_before_entry" in statement):
                writes.append(True)
        with original_connect() as db:
            db.set_trace_callback(traced)
            yield db
            if writes or observer_write:
                observer_write.clear()
                losses.append(True)
                if lost_at == "before-commit":
                    raise sqlite3.OperationalError("Synthetic original journal commit lost.")
            else:
                return
        raise sqlite3.OperationalError("Synthetic original journal acknowledgement lost.")
    monkeypatch.setattr(c.supervisor.journal, "_save", save)
    monkeypatch.setattr(c.supervisor.journal, "connect", connect)
    observed, constructors = close_memory_entry(c, monkeypatch)
    monkeypatch.setattr(c.supervisor.journal, "connect", original_connect)
    monkeypatch.setattr(c.supervisor.journal, "_save", original_save)
    assert losses == [True]
    assert observed.phase == "spawn_unknown" and observed.sdk_no_start is None
    assert c.supervisor.observe(c.starts[0].launch_id).phase == "spawn_unknown"
    c.h.workers.reconcile(c.binding.job_id, c.binding.owner)
    assert c.h.workers.inspect(c.binding.job_id).state == "capacity_uncertain"
    assert constructors == [] and c.provider_requests == []
    assert c.h.store.read_snapshot((key("no-sdk-turn", c.binding.job_id),))[key("no-sdk-turn", c.binding.job_id)] is None


@pytest.mark.parametrize("lost_at", ["assistant-claim", "acquire"])
def test_unacknowledged_original_claim_or_acquire_does_not_authorize_memory_closure(native_memory_no_start, monkeypatch, lost_at):
    c = native_memory_no_start
    completed, constructors = [], []
    if lost_at == "assistant-claim":
        original = c.h.assistant.native_claim
        def lost_claim(*args, **kwargs):
            original(*args, **kwargs)
            completed.append(True)
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "Synthetic original assignment acknowledgement lost.")
        monkeypatch.setattr(c.h.assistant, "native_claim", lost_claim)
    else:
        original = hosted.AssistantClient.acquire
        def lost_acquire(client):
            original(client)
            completed.append(True)
            withdraw_source(c.h, client.assignment.binding_id)
            raise Problem(503, "ASSISTANT_API_UNKNOWN", "Synthetic acquire acknowledgement lost.")
        monkeypatch.setattr(hosted.AssistantClient, "acquire", lost_acquire)
    def forbidden(*args, **kwargs):
        constructors.append(True)
        raise AssertionError("Unacknowledged original acquisition cannot enter SDK.")
    monkeypatch.setattr(m2_copilot, "CopilotClient", forbidden)
    if lost_at == "assistant-claim":
        with pytest.raises(Problem) as start:
            c.h.workers.start(c.binding.job_id, c.binding.owner)
        assert start.value.body["error"]["code"] == "WORKER_START_UNKNOWN"
    else:
        c.h.workers.start(c.binding.job_id, c.binding.owner)
    c.supervisor.join(timeout=5)
    if lost_at == "assistant-claim":
        with pytest.raises(Problem) as observation:
            c.supervisor.observe(c.starts[0].launch_id)
        assert observation.value.body["error"]["code"] == "HOSTED_MATERIAL_MISSING"
    else:
        observed = c.supervisor.observe(c.starts[0].launch_id)
        assert observed.phase == "spawn_unknown" and observed.sdk_no_start is None
    assert completed == [True] and constructors == []
    if lost_at == "assistant-claim":
        with pytest.raises(Problem) as query:
            c.h.workers.reconcile(c.binding.job_id, c.binding.owner)
        assert query.value.body["error"]["code"] == "WORKER_QUERY_UNKNOWN"
        assert c.h.workers.inspect(c.binding.job_id).state == "starting"
    else:
        c.h.workers.reconcile(c.binding.job_id, c.binding.owner)
        assert c.h.workers.inspect(c.binding.job_id).state == "capacity_uncertain"
    assert c.host.start(c.starts[0], c.invocation, c.identity.session_id)["status"] == "pending"
    assert completed == [True] and c.provider_requests == []
    assert c.h.store.read_snapshot((key("no-sdk-turn", c.binding.job_id),))[key("no-sdk-turn", c.binding.job_id)] is None


@pytest.mark.parametrize("missing", ["tokens", "claim-record"])
def test_missing_or_unknown_memory_evidence_does_not_become_a_definite_no_sdk_refusal(native_memory_no_start, monkeypatch, missing):
    c = native_memory_no_start
    acquire = hosted.AssistantClient.acquire
    def after_ack(client):
        result = acquire(client)
        if missing == "tokens":
            monkeypatch.setattr(c.h.memory, "_count", lambda _: None)
        else:
            from shared_context.behavior_operations import control_key
            from shared_context.source_materials import SourceScope
            scope = SourceScope(tenant_id=c.binding.tenant_id, team_id=c.binding.team_id, visibility="team")
            missing_key = control_key("memory-claim", scope.key, c.binding.subject, "claim-one", "2")
            snapshot = c.h.memory.read_snapshot
            def absent(keys):
                rows = dict(snapshot(keys))
                if missing_key in rows:
                    rows[missing_key] = None
                return rows
            monkeypatch.setattr(c.h.memory, "read_snapshot", absent)
        return result
    constructors = []
    def forbidden(*args, **kwargs):
        constructors.append(True)
        raise AssertionError("Unavailable required memory must not reach SDK.")
    monkeypatch.setattr(hosted.AssistantClient, "acquire", after_ack)
    monkeypatch.setattr(m2_copilot, "CopilotClient", forbidden)
    c.h.workers.start(c.binding.job_id, c.binding.owner)
    c.supervisor.join(timeout=5)
    observed = c.supervisor.observe(c.starts[0].launch_id)
    assert observed.phase == "spawn_unknown" and observed.sdk_no_start is None
    with c.supervisor.journal.connect() as db:
        gate = json.loads(db.execute("SELECT body FROM assistant_sdk_start_gates").fetchone()[0])
        failure = json.loads(db.execute("SELECT body FROM assistant_failures").fetchone()[0])
    assert gate["state"] == "open"
    assert failure["code"] == ("MEMORY_TOKEN_COUNT_UNKNOWN" if missing == "tokens" else "SOURCE_REQUIRED_MISSING")
    c.h.workers.reconcile(c.binding.job_id, c.binding.owner)
    assert c.h.workers.inspect(c.binding.job_id).state == "capacity_uncertain"
    assert constructors == [] and c.provider_requests == []


@pytest.mark.parametrize("consumed", [False, True])
def test_closed_memory_gate_cannot_erase_reserved_or_consumed_model_intent(native_memory_no_start, monkeypatch, consumed):
    c = native_memory_no_start
    catalog = hosted.AssistantClient.work_memory_catalog
    body = {"model": "gpt-4.1-mini", "stream": True, "messages": [
        {"role": "user", "content": "Use only the assigned reviewed knowledge."},
    ]}
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if consumed:
        # consume is the last authority check; withdraw after it and fail the provider token, so the intent is
        # consumed with an unknown outcome and nothing is sent.
        def withdraw_and_fail():
            withdraw_source(c.h, c.binding.binding_id)
            raise RuntimeError("OFFLINE provider token unavailable")
        c.provider_hooks.append(withdraw_and_fail)
    def reserved_then_revoked(client):
        catalog(client)
        client.reserve_model(digest)
        if consumed:
            with pytest.raises(Problem) as denied:
                client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
            assert denied.value.body["error"]["upstream_status"] == 503
        else:
            withdraw_source(c.h, c.binding.binding_id)
        return catalog(client)
    monkeypatch.setattr(hosted.AssistantClient, "work_memory_catalog", reserved_then_revoked)
    c.h.workers.start(c.binding.job_id, c.binding.owner)
    c.supervisor.join(timeout=5)
    observed = c.supervisor.observe(c.starts[0].launch_id)
    assert observed.phase == "sdk_not_started"
    sid = observed.sdk_no_start.gate.binding.session_id
    ikey = key("model-intent", sid, digest)
    intent = c.h.store.read_snapshot((ikey,))[ikey]
    assert intent is not None and intent.payload["data"]["consumed"] is consumed
    probe, report = report_packet(c, observed)
    guard = c.h.store._guard()
    refused = post_native_report(c, probe, report)
    assert refused.status_code == 409, refused.text
    assert c.h.store._guard() == guard and c.h.store.read_snapshot((ikey,))[ikey] == intent
    assert not c.h.workers.inspect(c.binding.job_id).result_forwarded and c.provider_requests == []


@pytest.mark.parametrize("activity", [
    "checkpoint", "reply", "artifact", "unknown-operation", "applied-operation", "child", "later-generation",
])
def test_closed_memory_gate_does_not_discard_existing_state_or_effects(native_memory_no_start, monkeypatch, activity):
    from shared_context.behavior_sdk_checkpoint import checkpoint_key
    from shared_context.behavior_operations import control_key
    c = native_memory_no_start
    observed, _ = close_memory_entry(c, monkeypatch)
    if activity in {"child", "later-generation"}:
        from shared_context.behavior_workers import worker_key
        row_key = worker_key("job", c.binding.job_id)
        c.h.mutate(row_key, **({"children": ["unfinished-child"]} if activity == "child" else {"generation": 2}))
    elif activity == "applied-operation":
        from shared_context.behavior_operations import _Operation, OperationDescription, ResolvedResource
        from shared_context.behavior_delivery import _material
        from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
        row_key = control_key("operation", c.binding.operation_id)
        description = OperationDescription(adapter_id="offline-retained-effect", adapter_version="v1", kind="request",
            method="POST", target="local-test-only", content=b"Existing effect fixture; never execute.",
            resources=(ResolvedResource(resource_id="retained-resource", version="immutable-v1"),))
        material = _material(description.model_dump(mode="json"))
        operation = _Operation(operation_id=c.binding.operation_id, owner_record_id=c.binding.owner.owner_record_id,
            conversation_id=c.binding.owner.conversation_id, root_budget_id=c.binding.owner.root_budget_id,
            description_digest=material.sha256, adapter_contract_digest="1" * 64,
            current_attempt_id="retained-unknown-attempt", effect_state="applied")
        c.h.store.transact(ControlRequest(request_id="inject-retained-effect", request_digest="1" * 64),
            lambda _: ControlChanges(writes=(ControlWrite(row_id=row_key, mode="create",
                data=operation.model_dump(mode="json"), material=material),), result={"fixture": "retained-effect-only"}))
    else:
        row_key = (checkpoint_key(c.binding.job_id) if activity == "checkpoint" else
                   control_key("operation", c.binding.operation_id) if activity == "unknown-operation" else
                   key("artifact", c.binding.work.work_id) if activity == "artifact" else
                   key("reply", c.binding.job_id))
        # Fault injection proves even unreadable retained state blocks closure; it is not SDK or effect evidence.
        c.h.seed([(row_key, wire.Command(request_id="retained-unresolved-record"))])
    retained = c.h.store.read_snapshot((row_key,))[row_key]
    probe, body = report_packet(c, observed)
    before = c.h.store._guard()
    response = post_native_report(c, probe, body)
    assert response.status_code == 409, response.text
    assert c.h.store._guard() == before and c.h.store.read_snapshot((row_key,))[row_key] == retained
    assert not c.h.workers.inspect(c.binding.job_id).result_forwarded and c.provider_requests == []


@pytest.mark.parametrize("race", ["owner-epoch", "cancel", "expired-probe", "changed-config", "replacement-supervisor"])
def test_memory_proof_remains_bound_to_current_owner_probe_config_and_owned_thread(native_memory_no_start, monkeypatch, race):
    from shared_context.behavior_operations import control_key
    c = native_memory_no_start
    observed, _ = close_memory_entry(c, monkeypatch)
    if race == "replacement-supervisor":
        replacement = hosted.AssistantSupervisor(c.supervisor.journal.root, runtime_path=Path(__file__))
        state = replacement.observe(c.starts[0].launch_id)
        assert state.phase == "spawn_unknown" and state.sdk_no_start is None
        return
    probe, body = report_packet(c, observed)
    if race == "owner-epoch":
        c.h.mutate(control_key("owner", c.binding.owner.owner_record_id), epoch=c.binding.owner.epoch + 1)
    elif race == "cancel":
        c.control.cancel(c.human, c.binding.binding_id)
    elif race == "expired-probe":
        c.h.time = probe.expires_at
    else:
        c.control.policy = c.control.policy.model_copy(update={
            "profile": c.control.policy.profile.model_copy(update={"sdk_start_profile": None}),
        })
    before = c.h.store._guard()
    response = post_native_report(c, probe, body)
    assert response.status_code in {403, 409, 503}, response.text
    assert c.h.store._guard() == before and not c.h.workers.inspect(c.binding.job_id).result_forwarded


def test_memory_report_ack_loss_and_replay_use_only_original_sealed_receipt(native_memory_no_start, monkeypatch):
    c = native_memory_no_start
    observed, constructors = close_memory_entry(c, monkeypatch)
    probe, body = report_packet(c, observed)
    c.h.transport.after_apply = "lost"
    response = post_native_report(c, probe, body)
    c.h.transport.after_apply = None
    assert response.status_code == 503 and not c.h.workers.inspect(c.binding.job_id).result_forwarded
    assert c.native.launch(c.starts[0].launch_id).phase == "sealed"
    rkey, nkey = key("result", c.binding.work.work_id), key("no-sdk-turn", c.binding.job_id)
    original = c.h.store.read_snapshot((rkey, nkey))
    before = c.h.store._guard()
    replay = post_native_report(c, probe, body)
    assert replay.status_code == 200 and c.h.store._guard() == before
    late_probe, late_body = report_packet(c, observed)
    before = c.h.store._guard()
    assert post_native_report(c, late_probe, late_body).status_code == 409
    assert c.h.store._guard() == before
    evidence = c.native.reconcile_sealed(c.h.workers.inspect(c.binding.job_id).binding)
    c.h.workers.observe(c.binding.job_id, evidence, c.binding.owner)
    result = c.h.runtime.controller.step(c.binding.binding_id)
    assert result.state == "failed" and result.result_forwarded
    assert c.h.store.read_snapshot((rkey, nkey)) == original and constructors == [] and c.provider_requests == []
    assert_failure_available(c.h, c.binding)
    check_history(c.h)
    save_proof("report-ack-reconciled-original-only", c.h, c.binding, constructors=constructors,
               source_code="SOURCE_WITHDRAWN", observer=observed)
