"""Versioned no-SDK disposition through original controller and signed routes."""

import json
import hashlib
import base64
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import A
from shared_context import m2_copilot
from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant import AssistantAuthority, AssistantProfile, _LocalHttp, key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_foundry import FoundryTarget, NativeExecutionGrant, NativeJobRoute, NativeLaunchAuthority, native_key
from shared_context.behavior_hosted import BehaviorExecutorClient, HostedInvocation
from shared_context.behavior_wire import HostedIdentity
from shared_context.errors import Problem
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_approved_skill_api import add_skill, approve_skill_version, configured
from test_behavior_assistant import assistant
from test_instruction_flow import check_history, instruction_models, runtime_path
from test_instruction_prompt import submit
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn, ordinary, settle


def configured_no_start(assistant):
    assistant.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({
            **profile.model_dump(), "sdk_start_profile": "fa-sdk-start-gate-v1",
        }) for name, profile in assistant.runtime.profiles.items()
    })
    return configured(assistant)


def assert_failure_available(h, binding):
    response = h.client.post(f"/behavior/v1/bindings/{binding.binding_id}/delivery/pull",
                             json={"request_id": "read-original-failure"}, headers=h.headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["message_kind"] == "failure" and payload["work_state"] == "failed"
    assert payload["work_id"] == binding.work.work_id
    assert json.loads(base64.urlsafe_b64decode(payload["content"])) == {
        "schema_version": "fa-assistant-notice-v1", "state": "failed", "sdk_reply_available": False}


def save_evidence(name, h, binding, *, constructors):
    if not os.environ.get("FA_NO_SDK_EVIDENCE_DIR"):
        return
    row = h.store.read_snapshot((key("no-sdk-turn", binding.job_id),))[key("no-sdk-turn", binding.job_id)]
    saved = row.payload["data"]
    gate = saved["disposition"]["gate"]
    destination = Path(os.environ["FA_NO_SDK_EVIDENCE_DIR"])
    destination.mkdir(parents=True, exist_ok=True)
    (destination / (name + ".json")).write_text(json.dumps({
        "evidence": name, "profile": gate["binding"]["profile"], "gate_state": gate["state"],
        "code_sha256": gate["binding"]["code_sha256"], "owner_kind": gate["owner"]["kind"],
        "refusal_code": gate["refusal_code"], "owned_thread_completed": saved["disposition"]["owned_thread_completed"],
        "sdk_constructor_calls": constructors, "model_requests": len(h.endpoint.requests),
        "failed_result_forwarded": h.workers.inspect(binding.job_id).result_forwarded,
        "new_sdk_archive_created": False, "sdk_stop_fabricated": False,
        "prior_archive_and_user_task_retained": True, "failure_pull_verified": True,
        "failure_confirmation": "pull payload only; no ACK or human-read claim",
        "followup_requires_explicit_reset": True,
        "no_sdk_turn_sha256": hashlib.sha256(json.dumps(saved, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "first_archive": "synthetic SDK state published through the real API/controller",
        "cloud_acceptance": "NOT_RUN", "paid_model_requests": 0,
    }, indent=2) + "\n", encoding="utf-8")


def test_closed_sdk_entry_delivers_failure_and_allows_only_explicit_reset(assistant, monkeypatch):
    add_skill(assistant)
    h, control, human = configured_no_start(assistant)
    first = open_turn(h, control, human, "no-sdk-first")
    sdk_constructions = []
    try:
        settle(first, ordinary(first.assignment.sdk_session_id))
        previous_keys = (key("text-archive", first.binding.job_id), key("result", first.binding.work.work_id))
        previous = h.store.read_snapshot(previous_keys)
        h.workers.adapter = h.executor.adapter()
        issue = h.assistant.issue

        def change_after_issue(start):
            result = issue(start)
            approve_skill_version(h, "receipt-guide", "Current guidance after the original assignment.")
            return result

        def sdk_forbidden(*args, **kwargs):
            sdk_constructions.append(True)
            raise AssertionError("The source refusal precedes SDK construction.")

        monkeypatch.setattr(h.assistant, "issue", change_after_issue)
        monkeypatch.setattr(m2_copilot, "CopilotClient", sdk_forbidden)
        accepted, command = submit(h, "no-sdk-second", "  Preserve this new user task.\r\n",
                                   first.assignment.task.instruction.conversation_id)
        binding = h.runtime.bindings[accepted["binding_id"]]
        outcome = h.runtime.controller.step(binding.binding_id)
        assert outcome.state == "failed" and outcome.result_forwarded
        assert sdk_constructions == [] and h.endpoint.requests == []
        assert h.store.read_snapshot(previous_keys) == previous
        assert control.records[binding.binding_id].profile.instruction.content == command.content
        archive_key = key("text-archive", binding.job_id)
        assert h.store.read_snapshot((archive_key,))[archive_key] is None
        result = h.assistant.result(h.workers.inspect(binding.job_id).binding)
        assert result.state == "failed" and result.passed_checks == ()
        assert "sdk_stop" not in json.loads(result.proof)
        assert_failure_available(h, binding)
        projection = h.client.get("/work-policy/v1/conversations/" + accepted["conversation_id"],
                                  headers=h.headers())
        assert projection.status_code == 200
        assert projection.json()["can_follow_up"] is False and projection.json()["can_reset"] is True
        assert projection.json()["follow_up_blocker"] == "SDK_NOT_STARTED_RESET_REQUIRED"
        assert projection.json()["turns"][-1]["reply_available"] is False
        check_history(h)
        reset = h.client.post("/work-policy/v1/conversations/" + accepted["conversation_id"] + "/reset",
            json={"request_id": "explicit-reset"}, headers=h.headers())
        assert reset.status_code == 200 and reset.json()["archived"]
        check_history(h)
        assert h.store.read_snapshot(previous_keys) == previous
        save_evidence("local-closed-entry", h, binding, constructors=len(sdk_constructions))
    finally:
        first.client.close()


@pytest.fixture
def native_no_start(assistant, tmp_path, request):
    add_skill(assistant)
    h, control, human = configured_no_start(assistant) if getattr(request, "param", True) else configured(assistant)
    first = open_turn(h, control, human, "native-no-sdk-first")
    api = supervisor = None
    try:
        settle(first, ordinary(first.assignment.sdk_session_id))
        template = FoundryTarget(executor_id=h.workers.contract.executor_id,
            project_endpoint="https://native.invalid/api/projects/test",
            agent_id="11111111-1111-1111-1111-111111111111",
            agent_name="assistant", agent_version="8", session_id="no-sdk-template", agent_oid=A.oid)
        native = NativeLaunchAuthority(h.workers, team_id=A.team, controller_id="internal-controller",
            targets=(template,), jobs=(NativeJobRoute(job_id="job-one", api_binding_id="binding-one", grant_id="fixture-grant"),),
            verifier_version=h.config.verifier_version, clock=lambda: h.time)
        h.runtime.dep = replace(h.runtime.dep, native_launches=native)
        h.assistant = AssistantAuthority(h.runtime, h.artifacts, (A,))
        h.client = TestClient(create_assistant_app(h.assistant, h.verifier))
        install_work_policy_routes(h.client.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
            installation=lambda: None, require_current_native_evidence=lambda: None))
        accepted, command = submit(h, "native-no-sdk-second", "  Preserve the refused task.\r\n",
                                   first.assignment.task.instruction.conversation_id)
        binding = h.runtime.bindings[accepted["binding_id"]]
        control.refresh()
        target = native.targets_for(binding.job_id)[0]
        job = h.workers.inspect(binding.job_id)
        h.seed([(native_key("grant", native.jobs[binding.job_id].grant_id), NativeExecutionGrant(
            grant_id=native.jobs[binding.job_id].grant_id, controller_id="internal-controller", owner=job.owner,
            policy_revision=1, job_id=binding.job_id, definition=binding.work,
            input_digest=hashlib.sha256(job.spec.input).hexdigest(), targets=(target,),
            active=True, remaining_uses=1, not_before=h.time - timedelta(minutes=1),
            expires_at=h.time + timedelta(minutes=5), workload_version="assistant-v1.2",
        ))])
        api = BehaviorExecutorClient("https://context.invalid", h.verifier.auth.audience,
            SimpleNamespace(get_token=lambda _: SimpleNamespace(
                token=h.headers("agent")["Authorization"].removeprefix("Bearer "))), transport=_LocalHttp(h.client))
        supervisor = hosted.AssistantSupervisor(tmp_path / "owned-host", runtime_path=Path(__file__))
        identity = HostedIdentity(**{field: getattr(target, field)
                                     for field in ("agent_id", "agent_name", "agent_version", "session_id")})
        host = hosted.AssistantHostedRuntime(identity, supervisor, api, clock=lambda: h.time)
        invocation, starts = "synthetic-no-sdk-invocation", []

        def start_assigned(start):
            launch, permit = native.prepare(start)
            payload = HostedInvocation(binding_id=binding.binding_id, launch_id=launch.launch_id, launch_permit=permit)
            starts.append(payload)
            host.start(payload, invocation, identity.session_id)

        def probe_original(execution):
            probe = native.probe(execution)
            return host.observe(invocation, identity.session_id, probe.probe_id).evidence_id

        h.workers.adapter = replace(h.workers.adapter, start=start_assigned, query=probe_original, verify=native.verify)
        yield SimpleNamespace(h=h, control=control, human=human, first=first, binding=binding,
            accepted=accepted, command=command, native=native, supervisor=supervisor,
            host=host, identity=identity, invocation=invocation, starts=starts, api=api)
    finally:
        if supervisor is not None:
            supervisor.join(timeout=5)
        if api is not None:
            api.close()
        first.client.close()


def test_hosted_closed_gate_reports_original_failure_through_signed_probe(native_no_start, monkeypatch):
    c = native_no_start
    h = c.h
    previous_keys = (key("text-archive", c.first.binding.job_id), key("result", c.first.binding.work.work_id))
    previous = h.store.read_snapshot(previous_keys)
    issue = h.assistant.issue
    constructions = []

    def issue_and_change(start):
        result = issue(start)
        approve_skill_version(h, "receipt-guide", "Approved after this native assignment.")
        return result

    def forbidden(*args, **kwargs):
        constructions.append(True)
        raise AssertionError("A refused pre-SDK source must not construct the client.")

    monkeypatch.setattr(h.assistant, "issue", issue_and_change)
    monkeypatch.setattr(m2_copilot, "CopilotClient", forbidden)
    h.runtime.controller.step(c.binding.binding_id)
    c.supervisor.join(timeout=5)
    launch_id = c.starts[0].launch_id
    observed = c.supervisor.observe(launch_id)
    assert observed.phase == "sdk_not_started" and observed.sdk_stop is None and observed.child_pid is None
    h.workers.reconcile(c.binding.job_id, c.binding.owner)
    done = h.runtime.controller.step(c.binding.binding_id)
    assert done.state == "failed" and done.result_forwarded
    assert constructions == [] and h.endpoint.requests == []
    assert h.store.read_snapshot(previous_keys) == previous
    assert c.control.records[c.binding.binding_id].profile.instruction.content == c.command.content
    assert_failure_available(h, c.binding)
    check_history(h)
    projection = h.client.get("/work-policy/v1/conversations/" + c.accepted["conversation_id"], headers=h.headers())
    assert projection.status_code == 200 and projection.json()["can_reset"]
    assert not projection.json()["can_follow_up"]
    assert projection.json()["follow_up_blocker"] == "SDK_NOT_STARTED_RESET_REQUIRED"
    assert c.host.start(c.starts[0], c.invocation, c.identity.session_id)["status"] == "pending"
    assert constructions == []
    save_evidence("hosted-synthetic-closed-entry", h, c.binding, constructors=len(constructions))


def close_native_entry(c, monkeypatch):
    issue = c.h.assistant.issue

    def changed(start):
        result = issue(start)
        approve_skill_version(c.h, "receipt-guide", "Approved after assignment before SDK entry.")
        return result

    monkeypatch.setattr(c.h.assistant, "issue", changed)
    c.h.workers.start(c.binding.job_id, c.binding.owner)
    c.supervisor.join(timeout=5)
    return c.supervisor.observe(c.starts[0].launch_id)


def report_packet(c, observed):
    launch_id = c.starts[0].launch_id
    assigned = c.supervisor.journal.assignment(launch_id)
    job = c.h.workers.inspect(c.binding.job_id)
    probe = c.native.probe(job.binding)
    body = {
        "request_id": "original-report", "probe_id": probe.probe_id,
        "invocation_id": c.invocation, "identity": c.identity.model_dump(mode="json"),
        "process_id": assigned.process_id, "claim_id": assigned.claim_id,
        "process_generation": assigned.process_generation, "binding_digest": assigned.binding_digest,
        "supervisor_id": observed.supervisor_id, "sequence": observed.sequence,
        "phase": observed.phase, "outcome": observed.outcome, "child_pid": observed.child_pid,
        "child_exited": observed.child_exited, "exit_code": observed.exit_code,
    }
    if observed.sdk_no_start is not None:
        body["sdk_no_start"] = observed.sdk_no_start.model_dump(mode="json")
    return probe, body


def post_native_report(c, probe, body):
    launch_id = c.starts[0].launch_id
    return c.h.client.post(
        f"/behavior/v1/bindings/{c.binding.binding_id}/hosted/launches/{launch_id}/probes/{probe.probe_id}/report",
        json=body, headers={**c.h.headers("agent"),
            "X-Behavior-Report-Token": c.host.report_tokens[launch_id].get_secret_value()})


@pytest.mark.parametrize("tamper", [
    "missing-version", "future-version", "wrong-job", "wrong-execution", "wrong-config", "wrong-code",
    "wrong-supervisor", "wrong-claim", "wrong-generation", "entry-attempted", "thread-live",
    "thread-number", "missing-gate-version", "missing-binding-version",
])
def test_no_sdk_native_report_rejects_unbound_or_unverified_dispositions(native_no_start, monkeypatch, tamper):
    c = native_no_start
    observed = close_native_entry(c, monkeypatch)
    assert observed.phase == "sdk_not_started"
    probe, body = report_packet(c, observed)
    value = body["sdk_no_start"]
    if tamper == "missing-version":
        value.pop("schema_version")
    elif tamper == "missing-gate-version":
        value["gate"].pop("schema_version")
    elif tamper == "missing-binding-version":
        value["gate"]["binding"].pop("schema_version")
    elif tamper == "future-version":
        value["schema_version"] = "fa-sdk-no-start-future"
    elif tamper == "wrong-job":
        value["gate"]["binding"]["job_id"] = "different-job"
    elif tamper in {"wrong-execution", "wrong-config", "wrong-code"}:
        field = {"wrong-execution": "execution_digest", "wrong-config": "worker_configuration_digest",
                 "wrong-code": "code_sha256"}[tamper]
        value["gate"]["binding"][field] = "f" * 64
    elif tamper in {"wrong-supervisor", "wrong-claim", "wrong-generation"}:
        field = {"wrong-supervisor": "supervisor_id", "wrong-claim": "claim_id",
                 "wrong-generation": "process_generation"}[tamper]
        value["gate"]["owner"][field] = 42 if field == "process_generation" else "different-owner"
    elif tamper == "entry-attempted":
        value["gate"].update(state="entry_attempted", refusal_code=None)
    elif tamper == "thread-number":
        value["owned_thread_completed"] = 1
    else:
        value["owned_thread_completed"] = False
    guard = c.h.store._guard()
    response = post_native_report(c, probe, body)
    assert response.status_code in {403, 422}
    assert c.h.store._guard() == guard
    nkey = key("no-sdk-turn", c.binding.job_id)
    assert c.h.store.read_snapshot((nkey,))[nkey] is None
    assert not c.h.workers.inspect(c.binding.job_id).result_forwarded
    assert c.h.endpoint.requests == []


@pytest.mark.parametrize("attempt", ["construction", "start"])
def test_sdk_entry_attempt_without_a_pid_still_cannot_use_no_sdk_disposition(native_no_start, monkeypatch, attempt):
    c = native_no_start
    calls = []

    async def failed_start():
        calls.append("start")
        raise TimeoutError("synthetic original start outcome unknown")

    async def no_process_cleanup():
        return None

    def construct(**options):
        calls.append("construction")
        if attempt == "construction":
            raise RuntimeError("synthetic construction outcome unknown")
        return SimpleNamespace(start=failed_start, stop=no_process_cleanup, force_stop=no_process_cleanup,
                               _cli_process=None)

    monkeypatch.setattr(m2_copilot, "CopilotClient", construct)
    c.h.workers.start(c.binding.job_id, c.binding.owner)
    c.supervisor.join(timeout=5)
    launch_id = c.starts[0].launch_id
    observed = c.supervisor.observe(launch_id)
    assert calls == (["construction"] if attempt == "construction" else ["construction", "start"])
    assert observed.phase == "spawn_unknown" and observed.sdk_no_start is None and observed.sdk_stop is None
    with c.supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_sdk_start_gates").fetchone()
    assert json.loads(raw)["state"] == "entry_attempted"
    c.h.workers.reconcile(c.binding.job_id, c.binding.owner)
    assert c.h.workers.inspect(c.binding.job_id).state == "capacity_uncertain"
    assert c.h.endpoint.requests == []
    assert c.host.start(c.starts[0], c.invocation, c.identity.session_id)["status"] == "pending"
    assert len(calls) == (1 if attempt == "construction" else 2)


@pytest.mark.parametrize("fault", ["before-commit", "after-commit"])
def test_no_sdk_observer_journal_commit_unknown_cannot_be_reported_terminal(native_no_start, monkeypatch, fault):
    c = native_no_start
    original_save = c.supervisor.journal._save
    original_connect = c.supervisor.journal.connect
    pending = []

    def save(db, state):
        original_save(db, state)
        if state.phase == "sdk_not_started":
            pending.append(True)
            if fault == "before-commit":
                raise sqlite3.OperationalError("synthetic observer commit unavailable")

    @contextmanager
    def connect():
        with original_connect() as db:
            yield db
        if pending and fault == "after-commit":
            pending.clear()
            raise sqlite3.OperationalError("synthetic observer commit acknowledgement lost")

    monkeypatch.setattr(c.supervisor.journal, "_save", save)
    monkeypatch.setattr(c.supervisor.journal, "connect", connect)
    observed = close_native_entry(c, monkeypatch)
    assert observed.phase == "spawn_unknown" and observed.sdk_no_start is None
    monkeypatch.setattr(c.supervisor.journal, "connect", original_connect)
    monkeypatch.setattr(c.supervisor.journal, "_save", original_save)
    assert c.supervisor.observe(c.starts[0].launch_id).phase == "spawn_unknown"
    c.h.workers.reconcile(c.binding.job_id, c.binding.owner)
    assert c.h.workers.inspect(c.binding.job_id).state == "capacity_uncertain"
    nkey = key("no-sdk-turn", c.binding.job_id)
    assert c.h.store.read_snapshot((nkey,))[nkey] is None


@pytest.mark.parametrize("race", ["owner-epoch", "cancel"])
def test_current_owner_change_or_cancellation_prevents_no_sdk_failure_commit(native_no_start, monkeypatch, race):
    c = native_no_start
    observed = close_native_entry(c, monkeypatch)
    probe, body = report_packet(c, observed)
    if race == "owner-epoch":
        from shared_context.behavior_operations import control_key
        c.h.mutate(control_key("owner", c.binding.owner.owner_record_id), epoch=c.binding.owner.epoch + 1)
    else:
        c.control.cancel(c.human, c.binding.binding_id)
    guard = c.h.store._guard()
    response = post_native_report(c, probe, body)
    assert response.status_code == 403
    assert c.h.store._guard() == guard
    assert not c.h.workers.inspect(c.binding.job_id).result_forwarded


def test_no_sdk_report_replay_and_late_probe_do_not_rewrite_original_failure(native_no_start, monkeypatch):
    c = native_no_start
    observed = close_native_entry(c, monkeypatch)
    probe, body = report_packet(c, observed)
    response = post_native_report(c, probe, body)
    assert response.status_code == 200
    guard = c.h.store._guard()
    replay = post_native_report(c, probe, body)
    assert replay.status_code == 200 and replay.json() == response.json() and c.h.store._guard() == guard
    result_key = key("result", c.binding.work.work_id)
    original = c.h.store.read_snapshot((result_key,))[result_key]
    late_probe, late_body = report_packet(c, observed)
    guard = c.h.store._guard()
    assert post_native_report(c, late_probe, late_body).status_code == 409
    assert c.h.store._guard() == guard and c.h.store.read_snapshot((result_key,))[result_key] == original
    evidence_id = response.json()["evidence_id"]
    execution = c.h.workers.inspect(c.binding.job_id).binding
    c.h.workers.observe(c.binding.job_id, evidence_id, c.binding.owner)
    c.native.reconcile_sealed(execution)
    done = c.h.runtime.controller.step(c.binding.binding_id)
    assert done.state == "failed" and done.result_forwarded
    assert c.h.store.read_snapshot((result_key,))[result_key] == original
    check_history(c.h)


@pytest.mark.parametrize("native_no_start", [False], indirect=True)
def test_unconfigured_server_cannot_be_enabled_by_a_no_sdk_report(native_no_start, monkeypatch):
    from shared_context.behavior_code_identity import code_sha256
    from shared_context.source_materials import digest
    c = native_no_start
    observed = close_native_entry(c, monkeypatch)
    assert observed.phase == "spawn_unknown" and observed.sdk_no_start is None
    probe, body = report_packet(c, observed)
    client = c.supervisor.clients[c.starts[0].launch_id]
    assignment = client.assignment
    execution = c.h.workers.inspect(c.binding.job_id).binding
    assigned = c.supervisor.journal.assignment(c.starts[0].launch_id)
    assert assignment.sdk_start_binding is None
    body.update(phase="sdk_not_started", outcome="failed", sdk_no_start={
        "schema_version": "fa-sdk-no-start-v1", "owned_thread_completed": True,
        "gate": {
            "schema_version": "fa-sdk-start-gate-record-v1", "state": "closed_before_entry",
            "refusal_code": "SKILL_SOURCE_CHANGED",
            "binding": {
                "schema_version": "fa-sdk-start-binding-v1", "profile": "fa-sdk-start-gate-v1",
                "session_id": assignment.session_id, "sdk_session_id": assignment.sdk_session_id,
                "job_id": c.binding.job_id, "task_digest": digest(assignment.task.model_dump(mode="json")),
                "profile_digest": assignment.task.profile_digest,
                "execution_digest": digest(execution.model_dump(mode="json")),
                "worker_configuration_digest": c.h.workers.configuration_digest(), "code_sha256": code_sha256(),
            },
            "owner": {
                "kind": "hosted", "supervisor_id": c.supervisor.supervisor_id,
                "launch_id": assigned.launch_id, "claim_id": assigned.claim_id,
                "process_generation": assigned.process_generation,
            },
        },
    })
    guard = c.h.store._guard()
    assert post_native_report(c, probe, body).status_code == 403
    assert c.h.store._guard() == guard
    assert c.h.workers.inspect(c.binding.job_id).state == "starting"


def test_changed_server_configuration_rejects_a_previous_gate_disposition(native_no_start, monkeypatch):
    c = native_no_start
    observed = close_native_entry(c, monkeypatch)
    probe, body = report_packet(c, observed)
    c.control.policy = c.control.policy.model_copy(update={
        "profile": c.control.policy.profile.model_copy(update={"sdk_start_profile": None})})
    guard = c.h.store._guard()
    assert post_native_report(c, probe, body).status_code in {403, 409, 503}
    assert c.h.store._guard() == guard
    assert not c.h.workers.inspect(c.binding.job_id).result_forwarded


def test_no_sdk_result_cannot_erase_an_existing_model_reservation(native_no_start, monkeypatch):
    c = native_no_start
    original = hosted.AssistantClient.skill_catalog

    def reserve_then_change(client):
        original(client)
        client.reserve_model("b" * 64)
        approve_skill_version(c.h, "receipt-guide", "Changed after the reservation.")
        return original(client)

    monkeypatch.setattr(hosted.AssistantClient, "skill_catalog", reserve_then_change)
    c.h.workers.start(c.binding.job_id, c.binding.owner)
    c.supervisor.join(timeout=5)
    observed = c.supervisor.observe(c.starts[0].launch_id)
    assert observed.phase == "sdk_not_started"
    sid = observed.sdk_no_start.gate.binding.session_id
    intent_key = key("model-intent", sid, "b" * 64)
    original_intent = c.h.store.read_snapshot((intent_key,))[intent_key]
    assert original_intent is not None
    probe, body = report_packet(c, observed)
    guard = c.h.store._guard()
    assert post_native_report(c, probe, body).status_code == 409
    assert c.h.store._guard() == guard and c.h.store.read_snapshot((intent_key,))[intent_key] == original_intent
    assert not c.h.workers.inspect(c.binding.job_id).result_forwarded and c.h.endpoint.requests == []


@pytest.mark.parametrize("field", ["sdk_start_profile", "sdk_start_binding", "sdk_no_start"])
def test_user_submit_cannot_choose_a_no_sdk_capability(assistant, field):
    h, _, _ = configured(assistant)
    guard = h.store._guard()
    response = h.client.post("/work-policy/v1/submit", json={
        "request_id": "user-cannot-enable-gate", "content": "Report current guidance.",
        field: "fa-sdk-start-gate-v1",
    }, headers=h.headers())
    assert response.status_code == 422 and h.store._guard() == guard


@pytest.mark.parametrize("lost_at", ["before-commit", "after-commit"])
def test_unacknowledged_initial_gate_reservation_stays_unknown(native_no_start, monkeypatch, lost_at):
    c = native_no_start
    connect = c.supervisor.journal.connect

    @contextmanager
    def lose_reservation():
        inserted = []
        with connect() as db:
            db.set_trace_callback(lambda statement: inserted.append(True)
                if statement.startswith("INSERT INTO assistant_sdk_start_gates") else None)
            yield db
            if inserted and lost_at == "before-commit":
                raise sqlite3.OperationalError("synthetic gate reservation commit lost")
        if inserted:
            raise sqlite3.OperationalError("synthetic gate reservation acknowledgement lost")

    monkeypatch.setattr(c.supervisor.journal, "connect", lose_reservation)
    c.h.workers.start(c.binding.job_id, c.binding.owner)
    c.supervisor.join(timeout=5)
    monkeypatch.setattr(c.supervisor.journal, "connect", connect)
    state = c.supervisor.observe(c.starts[0].launch_id)
    assert state.phase == "spawn_unknown" and state.sdk_no_start is None and state.sdk_stop is None
    c.h.workers.reconcile(c.binding.job_id, c.binding.owner)
    assert c.h.workers.inspect(c.binding.job_id).state == "capacity_uncertain"
    assert c.host.start(c.starts[0], c.invocation, c.identity.session_id)["status"] == "pending"
    assert c.h.endpoint.requests == []


def test_lost_no_sdk_report_ack_reconciles_original_evidence_without_reexecuting(native_no_start, monkeypatch):
    c = native_no_start
    observed = close_native_entry(c, monkeypatch)
    probe, body = report_packet(c, observed)
    c.h.transport.after_apply = "lost"
    response = post_native_report(c, probe, body)
    c.h.transport.after_apply = None
    assert response.status_code == 503
    launch = c.native.launch(c.starts[0].launch_id)
    assert launch.phase == "sealed"
    nkey, rkey = key("no-sdk-turn", c.binding.job_id), key("result", c.binding.work.work_id)
    original = c.h.store.read_snapshot((nkey, rkey))
    assert all(row is not None for row in original.values())
    execution = c.h.workers.inspect(c.binding.job_id).binding
    eid = c.native.reconcile_sealed(execution)
    c.h.workers.observe(c.binding.job_id, eid, c.binding.owner)
    done = c.h.runtime.controller.step(c.binding.binding_id)
    assert done.state == "failed" and done.result_forwarded
    assert c.h.store.read_snapshot((nkey, rkey)) == original
    assert c.h.endpoint.requests == []
    assert c.host.start(c.starts[0], c.invocation, c.identity.session_id)["status"] == "pending"
    check_history(c.h)


def test_server_selection_is_versioned_and_pinned_without_new_identity_scope():
    from shared_context.behavior_server_config import ServerConfiguration
    from test_behavior_assistant_server import assistant_configuration
    raw = assistant_configuration()
    before = ServerConfiguration.model_validate_json(json.dumps(raw))
    assert "sdk_start_profile" not in before.assistant.profiles[0].model_dump(mode="json")
    raw["assistant"]["profiles"][0]["sdk_start_profile"] = "fa-sdk-start-gate-v1"
    after = ServerConfiguration.model_validate_json(json.dumps(raw))
    assert before.sha256 != after.sha256
    assert before.resource_bindings()["assistant_sha256"] != after.resource_bindings()["assistant_sha256"]
    assert before.identity == after.identity and before.table == after.table
    raw["assistant"]["profiles"][0]["sdk_start_profile"] = "fa-sdk-start-gate-future"
    with pytest.raises(ValueError):
        ServerConfiguration.model_validate_json(json.dumps(raw))


def test_legacy_execution_and_report_bytes_do_not_gain_no_sdk_fields():
    from shared_context.behavior_hosted import LocalExecution
    from shared_context.behavior_wire import HostedReportBody
    execution = {
        "launch_id": "launch", "assignment_digest": "1" * 64, "supervisor_id": "supervisor",
        "child_nonce": "nonce", "phase": "spawn_unknown", "child_pid": None, "control": "continue",
        "control_request_id": None, "outcome": None, "checkpoint": None, "result": None,
        "exit_code": None, "child_exited": False, "sequence": 1,
    }
    report = {
        "request_id": "report", "invocation_id": "invocation",
        "identity": {"agent_id": "11111111-1111-1111-1111-111111111111", "agent_name": "agent",
                     "agent_version": "8", "session_id": "hosted"},
        "process_id": "supervisor", "claim_id": "claim", "probe_id": "probe", "process_generation": 1,
        "binding_digest": "2" * 64, "supervisor_id": "supervisor", "sequence": 1, "phase": "spawn_unknown",
        "child_pid": None, "outcome": None, "checkpoint": None, "result": None, "exit_code": None,
        "child_exited": False,
    }
    for model, document in ((LocalExecution, execution), (HostedReportBody, report)):
        original = json.dumps(document, separators=(",", ":"))
        assert model.model_validate_json(original).model_dump_json() == original
