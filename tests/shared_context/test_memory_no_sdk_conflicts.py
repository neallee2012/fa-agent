"""A scheduled public observer update is not evidence that reviewed knowledge changed."""

import json
import os
from pathlib import Path

import pytest

from shared_context import behavior_assistant_hosted as hosted
from shared_context import m2_copilot
from shared_context.behavior_assistant import key
from shared_context.behavior_hosted import LocalJournal
from shared_context.behavior_wire import SdkNoStart, sdk_refusal_allowed
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_instruction_prompt import submit
from test_m3_auth import jwk_client, signing_key
from test_memory_no_sdk import PROFILE_V2, configured_memory_gate, native_memory_no_start
from test_memory_no_sdk_wire import literal_disposition


def observe_job_during_context(h, monkeypatch, *, local, allow_sdk=False, revoke_text_policy=False):
    original = h.runtime.context
    observations, observed_bindings = [], set()

    def read_then_observe(binding_id, command, principal):
        result = original(binding_id, command, principal)
        if command.request_id == "work-memory-context" and binding_id not in observed_bindings:
            binding = h.runtime.bindings[binding_id]
            before = h.workers.inspect(binding.job_id)
            if local:
                with pytest.raises(Problem) as unknown:
                    h.workers.reconcile(binding.job_id, binding.owner)
                assert unknown.value.body["error"]["code"] == "OPERATION_RECORD_MISSING"
            else:
                h.workers.reconcile(binding.job_id, binding.owner)
            after = h.workers.inspect(binding.job_id)
            current = original(binding_id, command, principal)
            assert before.binding == after.binding and before.spec == after.spec
            assert after.queries_reserved == before.queries_reserved + 1
            assert current.manifest == result.manifest and current.claims == result.claims
            assert current.originals == result.originals and current.summary == result.summary
            mutations = ["job-query-observation"]
            text_policy_transition = None
            observed_bindings.add(binding_id)
            if revoke_text_policy:
                from shared_context.behavior_runtime import runtime_key
                policy_key = runtime_key("policy", binding_id)
                policy_before = h.store.read_snapshot((policy_key,))[policy_key].payload["data"]
                h.mutate(policy_key, human_text_enabled=False, text_policy_version=None)
                policy_after = h.store.read_snapshot((policy_key,))[policy_key].payload["data"]
                text_policy_transition = {
                    "human_text_enabled_before": policy_before["human_text_enabled"],
                    "human_text_enabled_after": policy_after["human_text_enabled"],
                    "text_policy_version_present_before": policy_before["text_policy_version"] is not None,
                    "text_policy_version_present_after": policy_after["text_policy_version"] is not None,
                }
                assert text_policy_transition == {
                    "human_text_enabled_before": True, "human_text_enabled_after": False,
                    "text_policy_version_present_before": True, "text_policy_version_present_after": False,
                }
                mutations.append("text-policy-revocation")
            observations.append({
                "seam": ("WorkerCore.reconcile followed by trusted per-work text-policy revocation"
                         if revoke_text_policy else "WorkerCore.reconcile; normal query/job observation only"),
                "runtime_mode": h.runtime.mode, "mutations": mutations,
                "text_policy_transition": text_policy_transition,
                "knowledge_comparison_phase": ("after-job-observation-before-text-policy-revocation"
                                               if revoke_text_policy else "after-job-observation"),
                "previous_queries": before.queries_reserved, "current_queries": after.queries_reserved,
                "job_state_after_observation": after.state,
                "same_execution_and_task": True, "same_exact_claim_review_manifest_and_originals": True,
            })
        return result

    failures, constructors = [], []
    catalog = hosted.AssistantClient.work_memory_catalog
    restore = hosted.AssistantClient.restore

    def observed(method):
        def call(client):
            try:
                return method(client)
            except Problem as error:
                failures.append(error.body["error"]["code"])
                raise
        return call

    def forbidden(*args, **kwargs):
        constructors.append(True)
        raise AssertionError("A conflicting current-state read must not enter the SDK.")

    monkeypatch.setattr(h.runtime, "context", read_then_observe)
    monkeypatch.setattr(hosted.AssistantClient, "work_memory_catalog", observed(catalog))
    monkeypatch.setattr(hosted.AssistantClient, "restore", observed(restore))
    if not allow_sdk:
        monkeypatch.setattr(m2_copilot, "CopilotClient", forbidden)
    return observations, failures, constructors


def conflict_evidence(name, h, binding, journal, observations, failures, constructors, *, observer=None):
    with journal.connect() as db:
        gate = json.loads(db.execute("SELECT body FROM assistant_sdk_start_gates").fetchone()[0])
    job = h.workers.inspect(binding.job_id)
    keys = (key("no-sdk-turn", binding.job_id), key("result", binding.work.work_id),
            key("text-archive", binding.job_id))
    retained = h.store.read_snapshot(keys)
    value = {
        "scope": "Normal query/job observation followed by actual text-policy revocation; not an observer-only refusal",
        "runtime_mode": h.runtime.mode,
        "mutations": observations[0]["mutations"],
        "observations": observations, "client_refusals": failures,
        "sdk_constructor_calls": len(constructors), "offline_model_requests": len(h.endpoint.requests),
        "gate_state": gate["state"], "gate_refusal": gate["refusal_code"],
        "observer_phase": None if observer is None else observer.phase,
        "observer_no_sdk": None if observer is None else observer.sdk_no_start is not None,
        "job_state": job.state, "result_forwarded": job.result_forwarded,
        "no_sdk_turn": retained[keys[0]] is not None, "saved_result": retained[keys[1]] is not None,
        "new_archive": retained[keys[2]] is not None,
    }
    if os.environ.get("FA_MEMORY_NO_SDK_EVIDENCE_DIR"):
        destination = Path(os.environ["FA_MEMORY_NO_SDK_EVIDENCE_DIR"])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return value


def test_generic_source_comparison_code_cannot_assert_a_v2_no_sdk_disposition():
    assert not sdk_refusal_allowed(PROFILE_V2, "ASSISTANT_SOURCE_CHANGED")
    value = literal_disposition("v2")
    value["gate"]["refusal_code"] = "ASSISTANT_SOURCE_CHANGED"
    with pytest.raises(ValueError):
        SdkNoStart.model_validate_json(json.dumps(value))
    assert sdk_refusal_allowed(PROFILE_V2, "WORK_MEMORY_SOURCE_CHANGED")
    assert sdk_refusal_allowed("fa-sdk-start-gate-v1", "SKILL_SOURCE_CHANGED")


def test_local_text_policy_race_keeps_generic_refusal_unresolved(assistant, monkeypatch):
    h, control, _ = configured_memory_gate(assistant, PROFILE_V2)
    observations, failures, constructors = observe_job_during_context(
        h, monkeypatch, local=True, revoke_text_policy=True)
    accepted, command = submit(h, "local-generic-source-conflict", "  Keep my original task.\r\n")
    binding = h.runtime.bindings[accepted["binding_id"]]
    error_code = None
    try:
        h.runtime.controller.step(binding.binding_id)
    except Problem as error:
        error_code = error.body["error"]["code"]
    job = h.workers.inspect(binding.job_id)
    journal = LocalJournal(h.executor.root / key("session-id", job.binding.start_request_id))
    evidence = conflict_evidence("local-query-update-text-policy-revocation", h, binding, journal,
                                 observations, failures, constructors)
    assert len(observations) == 1 and failures == ["ASSISTANT_SOURCE_CHANGED"]
    assert evidence["runtime_mode"] == h.runtime.mode == "offline_contract"
    assert evidence["mutations"] == ["job-query-observation", "text-policy-revocation"]
    assert "text-policy revocation" in evidence["scope"] and "text-policy revocation" in observations[0]["seam"]
    assert observations[0]["text_policy_transition"]["human_text_enabled_after"] is False
    assert observations[0]["knowledge_comparison_phase"] == "after-job-observation-before-text-policy-revocation"
    assert constructors == [] and h.endpoint.requests == []
    assert evidence["gate_state"] == "open" and evidence["gate_refusal"] is None, evidence
    assert error_code == "WORKER_START_UNKNOWN" and job.state == "starting"
    assert not any(evidence[name] for name in ("no_sdk_turn", "saved_result", "new_archive", "result_forwarded"))
    assert control.records[binding.binding_id].profile.instruction.content == command.content
    view = h.client.get("/work-policy/v1/conversations/" + accepted["conversation_id"], headers=h.headers())
    assert view.status_code == 200 and not view.json()["can_reset"] and not view.json()["can_follow_up"]


def test_hosted_text_policy_race_cannot_report_a_generic_no_sdk_failure(native_memory_no_start, monkeypatch):
    c = native_memory_no_start
    old_key = key("text-archive", c.first.binding.job_id)
    old_archive = c.h.store.read_snapshot((old_key,))[old_key]
    observations, failures, constructors = observe_job_during_context(
        c.h, monkeypatch, local=False, revoke_text_policy=True)
    c.h.workers.start(c.binding.job_id, c.binding.owner)
    c.supervisor.join(timeout=5)
    state = c.supervisor.observe(c.starts[0].launch_id)
    c.h.workers.reconcile(c.binding.job_id, c.binding.owner)
    evidence = conflict_evidence("hosted-query-update-text-policy-revocation", c.h, c.binding, c.supervisor.journal,
                                 observations, failures, constructors, observer=state)
    assert len(observations) == 1 and failures == ["ASSISTANT_SOURCE_CHANGED"]
    assert evidence["runtime_mode"] == c.h.runtime.mode == "offline_contract"
    assert evidence["mutations"] == ["job-query-observation", "text-policy-revocation"]
    assert "text-policy revocation" in evidence["scope"] and "text-policy revocation" in observations[0]["seam"]
    assert observations[0]["text_policy_transition"]["human_text_enabled_after"] is False
    assert observations[0]["knowledge_comparison_phase"] == "after-job-observation-before-text-policy-revocation"
    assert constructors == [] and c.provider_requests == [] and c.h.endpoint.requests == []
    assert evidence["gate_state"] == "open" and state.phase == "spawn_unknown", evidence
    assert state.sdk_no_start is None and state.sdk_stop is None and not state.child_exited
    assert evidence["job_state"] == "capacity_uncertain"
    assert not any(evidence[name] for name in ("no_sdk_turn", "saved_result", "new_archive", "result_forwarded"))
    with c.supervisor.journal.connect() as db:
        failure = json.loads(db.execute("SELECT body FROM assistant_failures").fetchone()[0])
    assert failure["code"] == failure["upstream_error_code"] == "ASSISTANT_SOURCE_CHANGED"
    assert c.h.store.read_snapshot((old_key,))[old_key] == old_archive
    view = c.h.client.get("/work-policy/v1/conversations/" + c.accepted["conversation_id"], headers=c.h.headers())
    assert view.status_code == 200 and not view.json()["can_reset"] and not view.json()["can_follow_up"]
    assert c.host.start(c.starts[0], c.invocation, c.identity.session_id)["status"] == "pending"
    assert len(observations) == 1 and constructors == []
