"""Normal job observation must not change the identity of exact reviewed knowledge."""

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient

from shared_context import m2_copilot
from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_work_memory import WorkMemoryRead
from shared_context.behavior_operations import control_key
from shared_context.behavior_workers import worker_key
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from test_behavior_api import METRICS
from test_behavior_assistant import assistant
from test_instruction_flow import check_history, instruction_models, runtime_path, sdk_io
from test_instruction_prompt import saved_turn, submit
from test_m3_auth import jwk_client, signing_key
from test_memory_no_sdk import PROFILE_V2, configured_memory_gate, native_memory_no_start
from test_memory_no_sdk_conflicts import observe_job_during_context
from test_reviewed_work_memory import NOTE, request, reviewed_note
from test_sdk_compaction_product import open_turn


def save_observation(name, value):
    if os.environ.get("FA_MEMORY_OBSERVER_EVIDENCE_DIR"):
        destination = Path(os.environ["FA_MEMORY_OBSERVER_EVIDENCE_DIR"])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def test_signed_catalog_and_read_survive_original_job_query_update(assistant, monkeypatch):
    h, control, human = configured_memory_gate(assistant, PROFILE_V2)
    turn = open_turn(h, control, human, "memory-observer-live")
    observations, failures, constructors = observe_job_during_context(h, monkeypatch, local=True)
    try:
        response = request(turn, "work-memory-catalog")
        save_observation("catalog-query-update", {
            "scope": "Signed catalog/read and real WorkerCore.reconcile; not native SDK evidence",
            "runtime_mode": h.runtime.mode,
            "response_status": response.status_code,
            "refusal_code": response.headers.get("x-behavior-assistant-error-code"),
            "observations": observations, "sdk_constructor_calls": len(constructors),
            "offline_model_requests": len(h.endpoint.requests),
            "source_metadata_returned": response.status_code == 200 and "source" in response.json(),
        })
        assert len(observations) == 1
        assert observations[0]["runtime_mode"] == h.runtime.mode == "offline_contract"
        assert observations[0]["mutations"] == ["job-query-observation"]
        assert observations[0]["text_policy_transition"] is None
        assert observations[0]["knowledge_comparison_phase"] == "after-job-observation"
        assert "normal query/job observation only" in observations[0]["seam"]
        assert response.status_code == 200, response.text
        catalog = turn.client.work_memory_catalog()
        loaded = turn.client.read_work_memory("claim-one")
        assert loaded.source == catalog.source and loaded.text == NOTE
        assert loaded.source.text_sha256 == hashlib.sha256(NOTE.encode("utf-8")).hexdigest()
        assert [item.text.encode("utf-8") for item in loaded.originals] == [METRICS]
        assert constructors == [] and h.endpoint.requests == [] and failures == []
        session_key = key("session", turn.assignment.session_id)
        saved = h.store.read_snapshot((session_key,))[session_key]
        assert saved.payload["data"]["work_memory_source"] == catalog.source.model_dump(mode="json")
        assert turn.client.status().state == "running"
    finally:
        turn.client.close()


def sdk_reads_note(monkeypatch):
    calls = sdk_io(monkeypatch, expected_tools=("read_context", "execute_operation", "read_work_memory"))
    factory = m2_copilot.CopilotClient.side_effect
    reads = []

    def client(**connection):
        sdk = factory(**connection)
        original_open = sdk.create_session.side_effect

        async def opened(*args, **options):
            session = await original_open(*args, **options)
            original_send = session.send.side_effect
            reader = next(tool for tool in options["tools"] if tool.name == "read_work_memory")

            async def send(prompt, **kwargs):
                result = await reader.handler(SimpleNamespace(session_id=session.session_id,
                    tool_name="read_work_memory", tool_call_id="observer-memory-read",
                    arguments={"claim_id": "claim-one"}))
                assert result.result_type == "success"
                value = WorkMemoryRead.model_validate_json(result.text_result_for_llm)
                assert value.text == NOTE
                assert [original.text.encode("utf-8") for original in value.originals] == [METRICS]
                reads.append(value.source)
                return await original_send(prompt, **kwargs)

            session.send.side_effect = send
            session.send_and_wait.side_effect = send
            return session

        sdk.create_session.side_effect = opened
        sdk.resume_session.side_effect = opened
        return sdk

    m2_copilot.CopilotClient.side_effect = client
    return calls, reads


def test_local_original_turn_and_cold_followup_complete_after_job_observation(assistant, monkeypatch, tmp_path):
    h, control, _ = configured_memory_gate(assistant, PROFILE_V2)
    observations, failures, _ = observe_job_during_context(h, monkeypatch, local=True, allow_sdk=True)
    calls, reads = sdk_reads_note(monkeypatch)
    h.endpoint.replies = iter([
        {"content": "Original reviewed knowledge remained valid."},
        {"content": "The same exact note is still current after cold restore."},
    ])
    h.endpoint.unbounded = True
    conversation, old, sdk_id = None, {}, None
    originals = ("  Keep this original instruction.\r\n", "\tContinue this same reviewed note.\r\n")
    for number, text in enumerate(originals):
        h.executor.root = tmp_path / str(number)
        accepted, command = submit(h, f"observer-local-{number}", text, conversation)
        conversation = accepted["conversation_id"]
        binding = h.runtime.bindings[accepted["binding_id"]]
        done = h.runtime.controller.step(binding.binding_id)
        assert done.state == "succeeded" and done.result_forwarded and not done.obligation_fulfilled
        assert control.records[binding.binding_id].profile.instruction.content == command.content == text
        assert h.store.read_snapshot(tuple(old)) == old
        old.update(h.store.read_snapshot((key("text-archive", binding.job_id),)))
        turn = saved_turn(h, binding.job_id)
        assert turn.work_memory_source == reads[-1] and turn.failure is None
        sdk_id = sdk_id or turn.archive.sdk_session_id
        assert turn.archive.sdk_session_id == sdk_id
        check_history(h)
    assert len(observations) == len(reads) == len(calls) == 2 and not failures
    assert reads[0] == reads[1] and [call["prompt"] for call in calls] == list(originals)
    assert [call["resume"] for call in calls] == [False, True]
    view = h.client.get("/work-policy/v1/conversations/" + conversation, headers=h.headers())
    assert view.status_code == 200 and view.json()["can_follow_up"]
    save_observation("local-completed-cold-route", {
        "scope": "Real Local controller/API, SDK I/O/process doubles; not native stop proof",
        "observations": observations, "completed_turns": 2, "sdk_io_requests": len(calls),
        "same_knowledge_and_sdk_identity": True, "old_archive_retained": True, "raw_user_unchanged": True,
        "generic_refusals": failures, "can_follow_up": True,
    })


def test_hosted_restore_read_and_signed_result_continue_after_job_observation(native_memory_no_start, monkeypatch):
    c = native_memory_no_start
    old_key = key("text-archive", c.first.binding.job_id)
    before = c.h.store.read_snapshot((old_key,))[old_key]
    observations, failures, _ = observe_job_during_context(c.h, monkeypatch, local=False, allow_sdk=True)
    calls, reads = sdk_reads_note(monkeypatch)
    c.h.endpoint.replies = iter([{"content": "The original reviewed note is still readable."}])
    c.h.endpoint.unbounded = True
    c.supervisor.model_transport = c.h.endpoint.transport
    c.h.workers.start(c.binding.job_id, c.binding.owner)
    c.supervisor.join(timeout=10)
    state = c.supervisor.observe(c.starts[0].launch_id)
    assert state.phase == "sealed" and state.sdk_no_start is None and state.outcome == "succeeded"
    assert len(observations) == len(calls) == len(reads) == 1 and not failures
    c.h.workers.reconcile(c.binding.job_id, c.binding.owner)
    done = c.h.runtime.controller.step(c.binding.binding_id)
    assert done.state == "succeeded" and done.result_forwarded
    assert c.h.store.read_snapshot((old_key,))[old_key] == before
    archive = saved_turn(c.h, c.binding.job_id)
    assert archive.work_memory_source == reads[0] == saved_turn(c.h, c.first.binding.job_id).work_memory_source
    assert archive.archive.sdk_session_id == c.first.assignment.sdk_session_id
    assert calls[0]["resume"] and calls[0]["prompt"] == c.command.content
    check_history(c.h)
    view = c.h.client.get("/work-policy/v1/conversations/" + c.accepted["conversation_id"], headers=c.h.headers())
    assert view.status_code == 200 and view.json()["can_follow_up"]
    save_observation("hosted-completed-original-restore", {
        "scope": "Original Hosted journal/claim/probe/report with SDK I/O/process doubles; not Azure or native stop proof",
        "observations": observations, "completed_turns": 1, "sdk_io_requests": len(calls),
        "same_knowledge_and_sdk_identity": True, "old_archive_retained": True, "raw_user_unchanged": True,
        "generic_refusals": failures, "result_forwarded": done.result_forwarded,
        "no_sdk_disposition": state.sdk_no_start is not None,
    })


@pytest.mark.parametrize("change", [
    "cancel", "job-terminal", "execution", "owner-epoch", "owner-disabled",
    "standing-revoked", "claim-expired", "budget-predicate",
])
def test_current_authority_changes_during_read_are_not_observation_metadata(assistant, monkeypatch, change):
    h, control, human = configured_memory_gate(assistant, PROFILE_V2)
    turn = open_turn(h, control, human, "observer-authority-race")
    try:
        if change == "claim-expired":
            reviewed_note(h, version=3, request_id="finite-note", binding_id=turn.binding.binding_id,
                          expires_at=h.time + timedelta(minutes=1))
        captured = turn.client.work_memory_catalog().source
        context = h.runtime.context
        changed = []

        def after_read(binding_id, command, principal):
            result = context(binding_id, command, principal)
            if command.request_id == "work-memory-context" and not changed:
                changed.append(True)
                job_key = worker_key("job", turn.binding.job_id)
                owner_key = control_key("owner", turn.binding.owner.owner_record_id)
                if change == "cancel":
                    control.cancel(human, turn.binding.binding_id)
                elif change == "job-terminal":
                    h.mutate(job_key, state="failed")
                elif change == "execution":
                    job = h.workers.inspect(turn.binding.job_id)
                    h.mutate(job_key, binding=job.binding.model_copy(
                        update={"generation": job.binding.generation + 1}).model_dump(mode="json"))
                elif change == "owner-epoch":
                    h.mutate(owner_key, epoch=turn.binding.owner.epoch + 1)
                elif change == "owner-disabled":
                    h.mutate(owner_key, active=False)
                elif change == "standing-revoked":
                    control.revoke(human, "revoke-observer-race")
                elif change == "claim-expired":
                    h.time += timedelta(minutes=2)
                else:
                    budget_key = control_key("budget", turn.binding.owner.root_budget_id)
                    budget = h.store.read_snapshot((budget_key,))[budget_key]
                    h.mutate(budget_key, remaining_units=budget.payload["data"]["remaining_units"] - 1)
            return result

        monkeypatch.setattr(h.runtime, "context", after_read)
        denied = request(turn, claim_id="claim-one")
        assert changed == [True] and denied.status_code in {403, 409}, denied.text
        assert "TEAM_MEMORY_CANARY" not in denied.text and "originals" not in denied.json()
        session_key = key("session", turn.assignment.session_id)
        saved = h.store.read_snapshot((session_key,))[session_key]
        assert saved.payload["data"]["work_memory_source"] == captured.model_dump(mode="json")
        assert saved.payload["data"]["model_requests"] == 0 and h.endpoint.requests == []
        assert h.store.read_snapshot((key("result", turn.binding.work.work_id),))[key("result", turn.binding.work.work_id)] is None
        save_observation("current-authority-" + change, {
            "scope": "Signed memory read; current trusted-authority mutation at the post-assembly seam",
            "mutation": change, "status": denied.status_code,
            "refusal": denied.headers.get("x-behavior-assistant-error-code", denied.json().get("error", {}).get("code")),
            "old_capture_retained": True, "response_body_withheld": True, "model_requests": 0,
            "state_fault_injection": change in {"job-terminal", "execution", "owner-epoch", "owner-disabled", "budget-predicate"},
        })
    finally:
        turn.client.close()


@pytest.mark.parametrize("change", ["observer", "cancel", "late-observer"])
def test_catalog_capture_keeps_job_predicate_and_original_atomic_guard(assistant, monkeypatch, change):
    h, control, human = configured_memory_gate(assistant, PROFILE_V2)
    turn = open_turn(h, control, human, "observer-capture-cas")
    request_id = key("work-memory", turn.assignment.session_id, "current-work-memory")
    transitions, submissions, read_sets = [], [], []

    def alter():
        transitions.append(True)
        if change == "cancel":
            control.cancel(human, turn.binding.binding_id)
        else:
            with pytest.raises(Problem) as unknown:
                h.workers.reconcile(turn.binding.job_id, turn.binding.owner)
            assert unknown.value.body["error"]["code"] == "OPERATION_RECORD_MISSING"

    command = h.assistant._command
    transaction = h.store.transact
    send = h.store._submit

    def before_transaction(session, name, *args, **kwargs):
        if name == "work-memory" and change != "late-observer":
            alter()
        return command(session, name, *args, **kwargs)

    def capture_reads(original, decide):
        if original.request_id == request_id:
            read_sets.append(original.read_set)
        return transaction(original, decide)

    def before_submit(operations, original):
        if original is not None and original.request_id == request_id:
            submissions.append(True)
            if change == "late-observer":
                alter()
        return send(operations, original)

    monkeypatch.setattr(h.assistant, "_command", before_transaction)
    monkeypatch.setattr(h.store, "transact", capture_reads)
    monkeypatch.setattr(h.store, "_submit", before_submit)
    try:
        response = request(turn, "work-memory-catalog")
        assert transitions == [True] and len(read_sets) == 1
        assert worker_key("job", turn.binding.job_id) in read_sets[0]
        assert set(h.assistant._keys(turn.session)) <= set(read_sets[0])
        session_key = key("session", turn.assignment.session_id)
        saved = h.store.read_snapshot((session_key,))[session_key]
        if change == "observer":
            assert response.status_code == 200 and submissions == [True], response.text
            assert saved.payload["data"]["work_memory_source"] == response.json()["source"]
            assert turn.client.read_work_memory("claim-one").text == NOTE
        else:
            assert response.status_code == (403 if change == "cancel" else 412), response.text
            assert "work_memory_source" not in saved.payload["data"]
            assert submissions == ([] if change == "cancel" else [True])
            if change == "late-observer":
                assert response.headers["x-behavior-assistant-error-code"] == "BEHAVIOR_CONFLICT"
        assert h.endpoint.requests == []
        save_observation("capture-cas-" + change, {
            "scope": "Real catalog ControlRequest and guard/ETag/EGT; deterministic interleaving, no retry",
            "status": response.status_code, "capture_submissions": len(submissions),
            "all_original_gate_keys_in_transaction": True,
            "captured": "work_memory_source" in saved.payload["data"], "model_requests": 0,
        })
    finally:
        turn.client.close()


def test_model_gates_allow_job_observation_without_replaying_consumed_intent(assistant, monkeypatch):
    from sdk_checkpoint_local import sse
    from shared_context.behavior_instruction_cost import cost_key
    h, control, human = configured_memory_gate(assistant, PROFILE_V2)
    sent = []

    def provider(request):
        sent.append(True)
        return sse(request, {"content": "The same authorized note was used once."}, 1)

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_OBSERVER_CONTROL")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    turn = open_turn(h, control, human, "observer-model-once")
    context, observations = h.runtime.context, []
    try:
        turn.client.work_memory_catalog()
        loaded = turn.client.read_work_memory("claim-one")

        def observed(binding_id, command, principal):
            result = context(binding_id, command, principal)
            if command.request_id == "work-memory-context":
                before = h.workers.inspect(turn.binding.job_id)
                with pytest.raises(Problem) as unknown:
                    h.workers.reconcile(turn.binding.job_id, turn.binding.owner)
                assert unknown.value.body["error"]["code"] == "OPERATION_RECORD_MISSING"
                current = context(binding_id, command, principal)
                assert current.manifest == result.manifest and current.claims == result.claims
                assert current.originals == result.originals
                assert h.workers.inspect(turn.binding.job_id).queries_reserved == before.queries_reserved + 1
                observations.append(True)
            return result

        monkeypatch.setattr(h.runtime, "context", observed)
        body = {"model": "gpt-4.1-mini", "stream": True, "messages": [
            {"role": "user", "content": "Use the reviewed note, with no external operation."},
            {"role": "tool", "tool_call_id": "original-memory-read", "content": loaded.model_dump_json()},
        ]}
        digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        turn.client.reserve_model(digest)
        response = turn.client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
        assert response.status_code == 200 and sent == [True] and len(observations) == 1  # reserve only
        retained_keys = (key("model-intent", turn.assignment.session_id, digest),
                         cost_key(turn.assignment.session_id, digest))
        retained = h.store.read_snapshot(retained_keys)
        assert retained[retained_keys[0]].payload["data"]["consumed"] is True
        assert retained[retained_keys[1]] is not None
        monkeypatch.setattr(h.runtime, "context", context)
        with pytest.raises(Problem) as replay:
            turn.client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
        assert replay.value.body["error"]["upstream_error_code"] == "ASSISTANT_MODEL_UNKNOWN"
        assert h.store.read_snapshot(retained_keys) == retained and sent == [True]
        save_observation("reserve-consume-egress-once", {
            "scope": "Real signed API reserve/consume/final-provider-gate with scripted offline HTTP",
            "normal_query_updates": len(observations), "exact_knowledge_unchanged": True,
            "offline_provider_requests": len(sent), "consumed_intent_and_cost_retained": True,
            "replay_refused": True, "paid_requests": 0,
        })
    finally:
        turn.client.close()
