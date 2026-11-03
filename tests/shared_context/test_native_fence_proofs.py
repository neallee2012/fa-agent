"""Every clause of the retained fence/lost-session proof checks, on saved models only.

The proofs below come from the real fence path (or the shape seal_lost_session writes). Each case breaks
exactly one clause, so a dropped or reordered clause fails here. No process, SDK, model or cloud call runs.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from shared_context.behavior_delivery import _digest, _json, _material, _parse
from shared_context.behavior_foundry import (
    SESSION_LOST, SESSION_VERSION_CHANGED, NativeLaunchAuthority, _lost_session_result, _NativeEvidence,
    _version_refusal_result, native_key,
)
from shared_context.behavior_workers import ExecutorEvidence, worker_key
from shared_context.errors import Problem
from test_behavior_delivery import CONTEXT
from test_behavior_foundry import NativeHarness
from test_behavior_settlement import write


FENCE = "permanent unclaimed gate-fence proof is inconsistent"
REFUSAL = "version refusal lacks its exact original observation"
LOST = "lost-session proof is inconsistent"
SAVED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def fenced_launch(root, observed):
    """A real unclaimed fence: the version refusal when observed is set, else the human's cancel."""
    h = NativeHarness(root)
    h.hold_start = True
    job_id = h.specs[0].job_id
    h.workers.start(job_id, CONTEXT)
    binding = h.workers.inspect(job_id).binding
    if observed is None:
        key = worker_key("job", job_id)
        write(h.store, key, {**h.store.read_snapshot((key,))[key].payload["data"], "cancel_requested": True})
    evidence_id = h.native.fence_unclaimed(binding, observed_agent_version=observed)
    launch_id = native_key("launch-id", binding.start_request_id)
    keys = (native_key("evidence", evidence_id), native_key("unclaimed-fence", launch_id))
    rows = h.store.read_snapshot(keys)
    saved = _parse(rows[keys[0]].payload["data"], _NativeEvidence)
    return h, h.native.launch(launch_id), saved, SimpleNamespace(payload=dict(rows[keys[1]].payload))


@pytest.fixture(scope="module")
def cancel_case(tmp_path_factory):
    h, *case = fenced_launch(tmp_path_factory.mktemp("cancel"), None)
    yield h.native, *case
    h.close()


@pytest.fixture(scope="module")
def version_case(tmp_path_factory):
    h, *case = fenced_launch(tmp_path_factory.mktemp("version"), "999")
    yield h.native, *case
    h.close()


@pytest.fixture(params=["cancel", "version"])
def either_case(request):
    return request.getfixturevalue(request.param + "_case")


def refused(call, *args, message):
    with pytest.raises(Problem) as denied:
        call(*args)
    assert (denied.value.status, denied.value.body["error"]["code"]) == (403, "NATIVE_EVIDENCE_INVALID")
    assert denied.value.body["error"]["message"] == message


def changed_evidence(saved, **updates):
    return saved.model_copy(update={"evidence": saved.evidence.model_copy(update=updates)})


def fence_row(fence, data):
    return SimpleNamespace(payload={**fence.payload, "data": data, "material": _material(data).model_dump(mode="json")})


def test_the_real_fences_pass_with_and_without_an_authority(either_case):
    native, launch, saved, fence = either_case
    assert native._verify_unclaimed_fence(launch, saved, fence) is None
    assert NativeLaunchAuthority._verify_unclaimed_fence(None, launch, saved, fence) is None


@pytest.mark.parametrize("field,value", [
    ("phase", "claimed"), ("native_invocation_id", "claimed-invocation"), ("claim_id", "claimed"),
    ("process_id", "claimed-process"), ("report_token_digest", "a" * 64), ("child_pid", 7),
    ("checkpoint", b"checkpoint"), ("sealed_evidence_id", "another-evidence"),
])
def test_a_launch_that_was_not_fenced_unclaimed_is_refused(either_case, field, value):
    native, launch, saved, fence = either_case
    refused(native._verify_unclaimed_fence, launch.model_copy(update={field: value}), saved, fence, message=FENCE)


def test_a_fence_read_for_another_launch_is_refused(either_case):
    native, launch, saved, fence = either_case
    refused(native._verify_unclaimed_fence, launch.model_copy(update={"launch_id": "another"}), saved, fence, message=FENCE)


def test_a_missing_fence_row_is_refused_as_a_proof_failure(either_case):
    native, launch, saved, _ = either_case
    refused(native._verify_unclaimed_fence, launch, saved, None, message=FENCE)


def test_fence_data_or_material_that_differs_is_refused(either_case):
    native, launch, saved, fence = either_case
    refused(native._verify_unclaimed_fence, launch, saved,
            fence_row(fence, {**fence.payload["data"], "extra": True}), message=FENCE)
    other = SimpleNamespace(payload={**fence.payload, "material": _material({"other": True}).model_dump(mode="json")})
    refused(native._verify_unclaimed_fence, launch, saved, other, message=FENCE)


def test_a_bad_fence_material_is_only_parsed_after_the_launch_and_data_match(either_case):
    native, launch, saved, fence = either_case
    broken = SimpleNamespace(payload={**fence.payload, "material": {"not": "material"}})
    with pytest.raises(Problem) as invalid:
        native._verify_unclaimed_fence(launch, saved, broken)
    assert invalid.value.status == 502 and invalid.value.body["error"]["code"] == "DELIVERY_RECORD_INVALID"
    refused(native._verify_unclaimed_fence, launch.model_copy(update={"phase": "claimed"}), saved, broken, message=FENCE)
    other = {**broken.payload, "data": {**fence.payload["data"], "extra": True}}
    refused(native._verify_unclaimed_fence, launch, saved, SimpleNamespace(payload=other), message=FENCE)


@pytest.mark.parametrize("field,value", [
    ("launch_id", "another"), ("probe_id", "another"), ("report_digest", "a" * 64),
])
def test_saved_evidence_of_another_fence_is_refused(either_case, field, value):
    native, launch, saved, fence = either_case
    refused(native._verify_unclaimed_fence, launch, saved.model_copy(update={field: value}), fence, message=FENCE)


@pytest.mark.parametrize("field,value", [
    ("proof", b"{}"), ("native_execution_id", "another"), ("entire_tree_idle", False),
    ("all_inflight_starts_fenced", False), ("checkpoint", b"checkpoint"), ("state", "succeeded"),
])
def test_saved_execution_evidence_that_differs_is_refused(either_case, field, value):
    native, launch, saved, fence = either_case
    refused(native._verify_unclaimed_fence, launch, changed_evidence(saved, **{field: value}), fence, message=FENCE)


def test_evidence_for_another_binding_is_refused(either_case):
    native, launch, saved, fence = either_case
    other = saved.evidence.binding.model_copy(update={"job_id": "another-job"})
    refused(native._verify_unclaimed_fence, launch, changed_evidence(saved, binding=other), fence, message=FENCE)


def test_cancel_and_refusal_keep_their_own_state_and_result(cancel_case, version_case):
    native, launch, saved, fence = cancel_case
    assert saved.evidence.state == "stopped" and saved.evidence.result is None
    refused(native._verify_unclaimed_fence, launch, changed_evidence(saved, state="failed"), fence, message=FENCE)
    refused(native._verify_unclaimed_fence, launch, changed_evidence(saved, result=version_case[2].evidence.result),
            fence, message=FENCE)
    native, launch, saved, fence = version_case
    assert saved.evidence.state == "failed" and saved.evidence.result is not None
    refused(native._verify_unclaimed_fence, launch, changed_evidence(saved, state="stopped"), fence, message=FENCE)
    other = saved.evidence.result.model_copy(update={"content": b"another notice"})
    refused(native._verify_unclaimed_fence, launch, changed_evidence(saved, result=other), fence, message=FENCE)


@pytest.mark.parametrize("change", [
    {"failure_code": "ANOTHER_CODE"}, {"observed_agent_version": 999}, {"observed_agent_version": None},
    {"observed_agent_version": "0"}, {"observed_agent_version": "1" * 17}, {"observed_agent_version": "9\n"},
])
def test_a_version_refusal_without_its_exact_observation_is_refused_first(version_case, change):
    native, launch, saved, fence = version_case
    # The launch clause below would also fail, so this pins the refusal check running before it.
    refused(native._verify_unclaimed_fence, launch.model_copy(update={"phase": "claimed"}), saved,
            fence_row(fence, {**fence.payload["data"], **change}), message=REFUSAL)


def test_a_refusal_observing_the_launch_own_version_is_refused(version_case):
    native, launch, saved, fence = version_case
    same = fence_row(fence, {**fence.payload["data"], "observed_agent_version": launch.target.agent_version})
    refused(native._verify_unclaimed_fence, launch, saved, same, message=REFUSAL)


def test_a_refusal_without_a_result_is_refused_before_reading_it(version_case):
    native, launch, saved, fence = version_case
    refused(native._verify_unclaimed_fence, launch, changed_evidence(saved, result=None), fence, message=REFUSAL)


def test_only_an_authority_pins_the_verifier_version_of_a_refusal(version_case):
    native, launch, saved, fence = version_case
    foreign = _version_refusal_result(launch, json.loads(saved.evidence.proof), saved.evidence.result.verifier_version + "-other")
    other = changed_evidence(saved, result=foreign)
    refused(native._verify_unclaimed_fence, launch, other, fence, message=REFUSAL)
    assert NativeLaunchAuthority._verify_unclaimed_fence(None, launch, other, fence) is None


def lost_saved(launch, *, session_status="failed", stop_requested=False, cancelled=False, verifier="verifier-v1", **proof):
    """The shape seal_lost_session writes; proof overrides stay consistent with the digest and material."""
    body = {
        "kind": "terminal_lost_session", "launch_id": launch.launch_id,
        "original_start_request_id": launch.start.binding.start_request_id,
        "native_invocation_id": launch.native_invocation_id, "session_id": launch.target.session_id,
        "agent_version": launch.target.agent_version, "session_status": session_status,
        "stop_requested": stop_requested, "cancel_requested": cancelled,
        "failure_code": SESSION_LOST, "no_provider_effect_claim": True, **proof,
    }
    evidence = ExecutorEvidence(
        evidence_id=native_key("lost-session-evidence", launch.launch_id), binding=launch.start.binding, sequence=1,
        native_execution_id=launch.native_invocation_id or "unclaimed", state="stopped" if cancelled else "failed",
        entire_tree_idle=True, all_inflight_starts_fenced=True, proof=_json(body),
        result=None if cancelled else _lost_session_result(launch, body, verifier),
    )
    return _NativeEvidence(
        launch_id=launch.launch_id, configuration_digest="a" * 64,
        probe_id=native_key("lost-session", launch.launch_id), evidence=evidence,
        observed_at=SAVED_AT, expires_at=SAVED_AT, report_digest=_digest(body),
    )


@pytest.fixture
def lost_case(cancel_case):
    _, launch, _, _ = cancel_case
    authority = SimpleNamespace(verifier_version="verifier-v1")
    return authority, launch.model_copy(update={
        "phase": "sealed", "native_invocation_id": "claimed-invocation", "claim_id": "claimed", "process_id": "process",
        "sealed_evidence_id": native_key("lost-session-evidence", launch.launch_id),
    })


def verify_lost(authority, launch, saved):
    return NativeLaunchAuthority._verify_lost_session(authority, launch, saved)


@pytest.mark.parametrize("status,stopped,cancelled", [
    ("failed", False, False), ("deleted", False, False), ("expired", False, True),
    ("idle", True, False), ("stop_accepted", True, True),
])
def test_a_proven_stopped_session_passes(lost_case, status, stopped, cancelled):
    authority, launch = lost_case
    saved = lost_saved(launch, session_status=status, stop_requested=stopped, cancelled=cancelled)
    assert verify_lost(authority, launch, saved) is None
    assert verify_lost(None, launch, saved) is None


@pytest.mark.parametrize("proof", [b"not json", b"\xff", b"[]", b"null", b'"text"', b"7"])
def test_a_proof_that_is_not_a_json_object_is_refused_not_raised(lost_case, proof):
    authority, launch = lost_case
    saved = lost_saved(launch)
    refused(verify_lost, authority, launch, changed_evidence(saved, proof=proof), message=LOST)
    refused(verify_lost, None, launch, changed_evidence(saved, proof=proof, result=None), message=LOST)


@pytest.mark.parametrize("change", [
    {"kind": "another"}, {"launch_id": "another"}, {"original_start_request_id": "another"},
    {"native_invocation_id": "another"}, {"session_id": "another"}, {"agent_version": "another"},
    {"failure_code": SESSION_VERSION_CHANGED}, {"no_provider_effect_claim": False}, {"extra": True},
    {"cancel_requested": "yes"},
])
def test_a_proof_naming_anything_else_is_refused(lost_case, change):
    authority, launch = lost_case
    refused(verify_lost, authority, launch, lost_saved(launch, **change), message=LOST)


def test_a_proof_missing_a_key_is_refused(lost_case):
    authority, launch = lost_case
    saved = lost_saved(launch)
    body = json.loads(saved.evidence.proof)
    del body["session_status"]
    refused(verify_lost, authority, launch, changed_evidence(saved, proof=_json(body)), message=LOST)


@pytest.mark.parametrize("status,stopped", [
    ("active", False), ("idle", False), ("stop_accepted", False), ("failed", "yes"), (None, True),
])
def test_a_session_without_a_proven_stop_is_refused(lost_case, status, stopped):
    authority, launch = lost_case
    refused(verify_lost, authority, launch, lost_saved(launch, session_status=status, stop_requested=stopped), message=LOST)


def test_only_an_authority_pins_the_verifier_version(lost_case):
    authority, launch = lost_case
    saved = lost_saved(launch, verifier="verifier-v2")
    refused(verify_lost, authority, launch, saved, message=LOST)
    assert verify_lost(None, launch, saved) is None


@pytest.mark.parametrize("field,value", [("phase", "claimed"), ("claim_id", None), ("sealed_evidence_id", "another")])
def test_a_launch_that_was_not_sealed_by_the_lost_session_is_refused(lost_case, field, value):
    authority, launch = lost_case
    saved = lost_saved(launch)
    refused(verify_lost, authority, launch.model_copy(update={field: value}), saved, message=LOST)


def test_an_unclaimed_launch_cannot_be_a_lost_session(lost_case):
    authority, launch = lost_case
    unclaimed = launch.model_copy(update={"native_invocation_id": None})
    # Proof and evidence name the same empty invocation, so only the launch clause can refuse it.
    saved = changed_evidence(lost_saved(unclaimed), native_execution_id=None)
    refused(verify_lost, authority, unclaimed, saved, message=LOST)


@pytest.mark.parametrize("field,value", [
    ("launch_id", "another"), ("probe_id", "another"), ("report_digest", "a" * 64),
])
def test_saved_evidence_of_another_lost_session_is_refused(lost_case, field, value):
    authority, launch = lost_case
    refused(verify_lost, authority, launch, lost_saved(launch).model_copy(update={field: value}), message=LOST)


@pytest.mark.parametrize("field,value", [
    ("proof", b'{"kind":"terminal_lost_session"}'), ("native_execution_id", "another"), ("entire_tree_idle", False),
    ("all_inflight_starts_fenced", False), ("checkpoint", b"checkpoint"), ("state", "stopped"),
])
def test_saved_lost_session_evidence_that_differs_is_refused(lost_case, field, value):
    authority, launch = lost_case
    refused(verify_lost, authority, launch, changed_evidence(lost_saved(launch), **{field: value}), message=LOST)


def test_a_differently_ordered_proof_is_refused(lost_case):
    authority, launch = lost_case
    saved = lost_saved(launch)
    spaced = json.dumps(json.loads(saved.evidence.proof)).encode()
    refused(verify_lost, authority, launch, changed_evidence(saved, proof=spaced), message=LOST)


def test_evidence_of_another_lost_binding_is_refused(lost_case):
    authority, launch = lost_case
    saved = lost_saved(launch)
    other = saved.evidence.binding.model_copy(update={"job_id": "another-job"})
    refused(verify_lost, authority, launch, changed_evidence(saved, binding=other), message=LOST)


def test_a_failed_closure_requires_its_result_and_a_cancelled_one_refuses_any(lost_case):
    authority, launch = lost_case
    failed = lost_saved(launch)
    refused(verify_lost, authority, launch, changed_evidence(failed, result=None), message=LOST)
    refused(verify_lost, None, launch, changed_evidence(failed, result=None), message=LOST)
    cancelled = lost_saved(launch, cancelled=True, stop_requested=True, session_status="stop_accepted")
    refused(verify_lost, authority, launch, changed_evidence(cancelled, result=failed.evidence.result), message=LOST)
    other = failed.evidence.result.model_copy(update={"content": b"another notice"})
    refused(verify_lost, authority, launch, changed_evidence(failed, result=other), message=LOST)


def test_a_cancelled_proof_keeps_stopped_state(lost_case):
    authority, launch = lost_case
    cancelled = lost_saved(launch, cancelled=True, stop_requested=True, session_status="stop_accepted")
    refused(verify_lost, authority, launch, changed_evidence(cancelled, state="failed"), message=LOST)
