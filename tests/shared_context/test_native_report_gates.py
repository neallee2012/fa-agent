"""NativeLaunchAuthority.report and reconcile_sealed, offline: each gate alone, its order, and what a pass records.

Every case starts from one genuine report held back from the authority (real child, real probe), so the single
change named by the case is the only reason it can be refused; ``test_the_held_report_is_accepted`` is the control.
"""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant_wire import Task
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_delivery import _digest, _material
from shared_context.behavior_foundry import native_key
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from test_behavior_delivery import CONTEXT
from test_behavior_foundry import NativeHarness
from test_behavior_store import SCOPE

ASSISTANT = "assistant-v1.2"
POC = "fa-resident-worker-poc-v1"


class Held:
    def __init__(self, h):
        self.h = h
        self.job_id = h.specs[0].job_id
        h.workers.start(self.job_id, CONTEXT)
        captured = []

        def hold(report):
            captured.append(report)
            raise Problem(409, "HELD_BY_TEST", "held back from the authority")

        h.transform_report = hold
        with pytest.raises(Problem):
            h.finish_job()
        h.transform_report = None
        self.report = captured[-1]
        assert self.report.phase == "sealed" and self.report.outcome == "succeeded"
        self.binding = h.workers.inspect(self.job_id).binding
        self.launch_id = native_key("launch-id", self.binding.start_request_id)
        target = h.native.launch(self.launch_id).target
        self.principal = BehaviorPrincipal("tenant", target.agent_oid, "team", "agent", target.agent_name, frozenset({"consume"}))
        self.evidence_id = native_key("evidence-id", self.launch_id, self.report.probe_id)

    def launch(self):
        return self.h.native.launch(self.launch_id)

    def send(self, *, binding_id="binding-0", probe_id=None, **changes):
        command = self.report.model_copy(update=changes)
        return self.h.native.report(binding_id, self.launch_id, probe_id or command.probe_id, command, self.principal)

    def refused(self, status, code, message=None, **options):
        before = self.launch()
        with pytest.raises(Problem) as caught:
            self.send(**options)
        assert (caught.value.status, caught.value.body["error"]["code"]) == (status, code)
        if message is not None:
            assert caught.value.body["error"]["message"] == message
        assert self.launch() == before
        key = native_key("evidence", self.evidence_id)
        assert self.h.store.read_snapshot((key,))[key] is None

    def row(self, key):
        return self.h.store.read_snapshot((key,))[key]

    def edit(self, key, *, in_step=False, change=None, **updates):
        """Rewrite one saved row as storage damage would; ``in_step`` keeps its inline material matching."""
        entity = self.h.client.backing.rows[("Behavior", SCOPE.partition_key, "behavior:v1:row:" + key)]
        payload = json.loads(entity["Payload"])
        payload["data"].update(updates)
        if change is not None:
            change(payload["data"])
        if in_step:
            payload["material"] = _material(payload["data"]).model_dump(mode="json")
        entity["Payload"] = json.dumps(payload).encode()

    def set_launch(self, **updates):
        self.edit(native_key("launch", self.launch_id), **updates)

    def set_workload(self, version):
        self.set_launch(workload_version=version)
        self.h.mutate("native-grant", "native-grant-0", workload_version=version)

    def job(self, **updates):
        self.edit(worker_key("job", self.job_id), **updates)

    def checkpoint(self, **changes):
        content = self.h.specs[0].input[:64]
        return json.dumps({
            "workload_version": "checksum-count-v1", "input_digest": self.binding.input_digest, "processed_bytes": 64,
            "prefix_digest": hashlib.sha256(content).hexdigest(), "newline_count": content.count(b"\n"), **changes,
        }).encode()

    def parking(self, **changes):
        return {"outcome": "parked", "result": None, "checkpoint": self.checkpoint(), **changes}

    def stopped(self):
        return {"outcome": "stopped", "result": None, "exit_code": 1}


@pytest.fixture
def held(tmp_path):
    h = NativeHarness(tmp_path)
    try:
        yield Held(h)
    finally:
        h.close()


NO_SDK = {
    "schema_version": "fa-sdk-no-start-v1", "owned_thread_completed": True,
    "gate": {
        "schema_version": "fa-sdk-start-gate-record-v1", "state": "closed_before_entry",
        "refusal_code": "SKILL_SOURCE_CHANGED",
        "binding": {
            "schema_version": "fa-sdk-start-binding-v1", "profile": "fa-sdk-start-gate-v1",
            "session_id": "1" * 64, "sdk_session_id": "2" * 64, "job_id": "native-job-0",
            "task_digest": "a" * 64, "profile_digest": "b" * 64, "execution_digest": "c" * 64,
            "worker_configuration_digest": "d" * 64, "code_sha256": "e" * 64,
        },
        "owner": {"kind": "hosted", "supervisor_id": "supervisor", "launch_id": "launch", "claim_id": "claim",
                  "process_generation": 1},
    },
}


def no_sdk_changes():
    return {"phase": "sdk_not_started", "outcome": "failed", "child_pid": None, "child_exited": False, "exit_code": None,
            "result": None, "sdk_no_start": wire.SdkNoStart.model_validate(NO_SDK)}


# The control, and what a sealed report records.

def test_the_held_report_is_accepted(held):
    receipt = held.send()
    assert (receipt.launch_id, receipt.probe_id, receipt.evidence_id, receipt.state) == (
        held.launch_id, held.report.probe_id, held.evidence_id, "succeeded")
    launch = held.launch()
    assert (launch.phase, launch.sealed_evidence_id, launch.last_report_sequence, launch.evidence_sequence) == (
        "sealed", held.evidence_id, held.report.sequence, 1)
    assert launch.child_pid == held.report.child_pid and launch.checkpoint is None
    evidence = held.h.native.verify(held.evidence_id)
    assert evidence.entire_tree_idle and evidence.all_inflight_starts_fenced
    assert evidence.native_execution_id == launch.native_invocation_id and evidence.state == "succeeded"
    assert evidence.sequence == 1 and evidence.binding == held.binding and evidence.checkpoint is None
    body = held.report.model_dump(mode="json", exclude={"report_token"})
    assert json.loads(evidence.proof) == {
        "launch_id": held.launch_id, "probe_id": held.report.probe_id, "report_digest": _digest(body),
        "native_identity": wire.HostedIdentity(**{name: getattr(launch.target, name) for name in (
            "agent_id", "agent_name", "agent_version", "session_id")}).model_dump(mode="json"),
    }
    result = evidence.result
    assert (result.evidence_id, result.state, result.passed_checks) == (
        native_key("result-id", held.launch_id), "succeeded", launch.definition.required_checks)
    assert json.loads(result.content) == {
        "workload_version": "checksum-count-v1", "input_digest": held.binding.input_digest,
        "byte_count": len(held.h.specs[0].input), "newline_count": held.h.specs[0].input.count(b"\n")}
    assert json.loads(result.proof) == {"native_evidence_id": held.evidence_id, "report_digest": _digest(body)}
    assert held.row(native_key("report", held.evidence_id)).payload["data"] == body
    target_key = held.h.native.target_key(launch.target, held.job_id)
    assert held.row(target_key).payload["data"]["active_launch_id"] is None


@pytest.mark.parametrize("phase,state", [("running", "running"), ("result_saved", "unknown"), ("spawn_unknown", "unknown")])
def test_a_progress_report_records_evidence_without_sealing_anything(held, phase, state):
    held.set_launch(child_pid=held.report.child_pid)
    receipt = held.send(phase=phase, outcome=None, result=None, exit_code=None, child_exited=False)
    launch = held.launch()
    assert receipt.state == state and launch.phase == "claimed" and launch.sealed_evidence_id is None
    assert launch.evidence_sequence == 1 and launch.child_pid == held.report.child_pid
    evidence = held.h.native.verify(held.evidence_id)
    assert not evidence.entire_tree_idle and not evidence.all_inflight_starts_fenced and evidence.result is None
    assert held.row(held.h.native.target_key(launch.target, held.job_id)).payload["data"]["active_launch_id"] == held.launch_id


def test_a_later_report_teaches_the_child_and_every_report_after_it_must_name_it(held):
    running = dict(phase="running", outcome=None, result=None, exit_code=None, child_exited=False)
    held.send(child_pid=None, **running)
    assert held.launch().child_pid is None
    second = held.h.native.probe(held.binding)
    command = held.report.model_copy(update=dict(
        running, probe_id=second.probe_id, request_id="second-report", sequence=held.report.sequence + 1))
    held.h.native.report("binding-0", held.launch_id, second.probe_id, command, held.principal)
    assert held.launch().child_pid == held.report.child_pid
    third = held.h.native.probe(held.binding)
    command = command.model_copy(update=dict(probe_id=third.probe_id, request_id="third-report", child_pid=None,
                                             sequence=held.report.sequence + 2))
    with pytest.raises(Problem) as caught:
        held.h.native.report("binding-0", held.launch_id, third.probe_id, command, held.principal)
    assert caught.value.body["error"]["code"] == "NATIVE_REPORT_DENIED"


# NATIVE_REPORT_DENIED: each binding alone.

DENIED = (403, "NATIVE_REPORT_DENIED", "fresh native process/generation/probe binding differs")


def test_a_report_needs_its_own_binding_route(held):
    held.refused(*DENIED, binding_id="binding-9")


def test_a_report_needs_a_probe_of_its_own_launch(held):
    held.edit(native_key("probe", held.report.probe_id), launch_id="another-launch")
    held.refused(*DENIED)


def test_a_report_must_name_the_probe_it_was_sent_for(held):
    other = held.h.native.probe(held.binding)
    held.refused(*DENIED, probe_id=other.probe_id)


@pytest.mark.parametrize("change", [
    {"process_generation": 99}, {"binding_digest": "0" * 64}, {"supervisor_id": "foreign-supervisor"},
])
def test_a_report_needs_the_original_process_binding(held, change):
    held.refused(*DENIED, **change)


@pytest.mark.parametrize("offset,accepted", [
    (timedelta(0), True), (timedelta(seconds=60) - timedelta(microseconds=1), True),
    (timedelta(seconds=60), False), (-timedelta(microseconds=1), False),
])
def test_a_report_must_arrive_inside_its_probe_window(held, offset, accepted):
    held.h.time += offset  # the harness clock has not moved since the probe was issued
    if accepted:
        assert held.send().state == "succeeded"
    else:
        held.refused(*DENIED)


def test_an_older_sequence_is_refused_and_a_newer_one_is_not(held):
    held.set_launch(last_report_sequence=held.report.sequence + 1)
    held.refused(*DENIED)
    held.set_launch(last_report_sequence=held.report.sequence - 1)
    assert held.send().state == "succeeded"


def test_the_last_sequence_may_only_repeat_the_same_report(held):
    held.set_launch(last_report_sequence=held.report.sequence, last_report_fingerprint="0" * 64)
    held.refused(*DENIED)
    held.set_launch(last_report_fingerprint=None)
    assert held.send().state == "succeeded"


def test_a_repeat_on_a_new_probe_keeps_its_fingerprint(held):
    running = dict(phase="running", outcome=None, result=None, exit_code=None, child_exited=False)
    held.send(**running)
    again = held.h.native.probe(held.binding)
    command = held.report.model_copy(update=dict(running, probe_id=again.probe_id, request_id="repeated-report"))
    assert held.h.native.report("binding-0", held.launch_id, again.probe_id, command, held.principal).state == "running"
    third = held.h.native.probe(held.binding)
    changed = command.model_copy(update={"phase": "spawn_unknown", "probe_id": third.probe_id, "request_id": "changed-report"})
    with pytest.raises(Problem) as caught:
        held.h.native.report("binding-0", held.launch_id, third.probe_id, changed, held.principal)
    assert caught.value.body["error"]["code"] == "NATIVE_REPORT_DENIED"


def test_a_known_child_must_match_and_an_unknown_child_may_be_learned(held):
    held.set_launch(child_pid=held.report.child_pid + 1)
    held.refused(*DENIED)
    held.set_launch(child_pid=held.report.child_pid)
    assert held.send().state == "succeeded"


# NATIVE_IDLE_UNVERIFIED and NATIVE_ALREADY_SEALED, after the binding and in this order.

IDLE = (403, "NATIVE_IDLE_UNVERIFIED", "owned child exit evidence is missing")


@pytest.mark.parametrize("change", [
    {"child_exited": False}, {"exit_code": None}, {"child_pid": None}, {"outcome": None, "result": None},
])
def test_a_sealed_report_needs_its_owned_exit(held, change):
    held.refused(*IDLE, **change)


def test_a_resident_release_does_not_replace_an_exit_without_an_assistant_plan(held):
    stop = wire.AssistantSdkStop(pid=held.report.child_pid, release=POC)
    held.refused(*IDLE, child_exited=False, exit_code=None, sdk_stop=stop)


def test_the_binding_is_checked_before_the_exit(held):
    held.refused(*DENIED, process_generation=99, child_exited=False)


def test_a_sealed_launch_refuses_another_report_after_the_exit_check(held):
    late = held.h.native.probe(held.binding)
    held.send()
    command = held.report.model_copy(update=dict(
        probe_id=late.probe_id, request_id="late-report", sequence=held.report.sequence + 1))
    with pytest.raises(Problem) as caught:
        held.h.native.report("binding-0", held.launch_id, late.probe_id, command, held.principal)
    assert (caught.value.status, caught.value.body["error"]["code"]) == (409, "NATIVE_ALREADY_SEALED")
    assert caught.value.body["error"]["message"] == "reconcile the retained terminal evidence; never execute again"
    with pytest.raises(Problem) as caught:
        held.h.native.report("binding-0", held.launch_id, late.probe_id,
                             command.model_copy(update={"child_exited": False}), held.principal)
    assert caught.value.body["error"]["code"] == "NATIVE_IDLE_UNVERIFIED"


# A checksum result, a failed or stopped child.

INVALID_RESULT = (403, "NATIVE_RESULT_INVALID", "saved checksum/count does not match the original assigned bytes")


def test_a_checksum_success_needs_a_clean_exit_and_the_exact_counts(held):
    held.refused(*INVALID_RESULT, exit_code=1)
    held.refused(*INVALID_RESULT, result=None)
    held.refused(*INVALID_RESULT, result=held.report.result.model_copy(update={"byte_count": 1}))


@pytest.mark.parametrize("change", [{"checkpoint": b"{}"}, {}])
def test_a_failed_child_cannot_claim_material(held, change):
    held.refused(403, "NATIVE_RESULT_INVALID", "failed/stopped execution cannot claim success material",
                 outcome="failed", **({"result": None, **change} if change else {}))


def test_a_failed_child_is_a_failed_result(held):
    assert held.send(outcome="failed", result=None, exit_code=1).state == "failed"
    result = held.h.native.verify(held.evidence_id).result
    assert (result.evidence_id, result.state, result.passed_checks, result.content) == (
        native_key("result-id", held.launch_id), "failed", (),
        b"Constrained checksum execution stopped without a completed result.")
    assert held.launch().phase == "sealed"


def test_a_stop_needs_a_real_cancel(held):
    held.refused(403, "NATIVE_CONTROL_DENIED", "an executor cannot invent cancellation authority", **held.stopped())


def test_a_cancelled_stop_is_a_cancelled_result(held):
    held.job(cancel_requested=True)
    assert held.send(**held.stopped()).state == "stopped"
    evidence = held.h.native.verify(held.evidence_id)
    assert evidence.state == "stopped" and evidence.result.state == "cancelled" and evidence.result.passed_checks == ()
    assert evidence.result.content == b"Constrained checksum execution stopped without a completed result."


# A parked child.

PARK = (403, "NATIVE_CHECKPOINT_INVALID", "parking requires an assigned control and owned exit")
BAD_CHECKPOINT = (403, "NATIVE_CHECKPOINT_INVALID", "checkpoint does not match the assigned original bytes")


def test_parking_needs_a_waiting_child_or_a_cancel(held):
    held.set_launch(checkpoint_after_bytes=64)
    held.refused(*PARK, **held.parking())


def test_parking_needs_an_assigned_control(held):
    held.job(children=["waiting-child"])
    held.refused(*PARK, **held.parking())


def test_parking_needs_a_clean_exit_and_a_checkpoint(held):
    held.job(children=["waiting-child"])
    held.set_launch(checkpoint_after_bytes=64)
    held.refused(*PARK, **held.parking(exit_code=1))
    held.refused(*PARK, **held.parking(checkpoint=None))


@pytest.mark.parametrize("control", ["bytes", "directive"])
def test_a_parked_child_saves_its_checkpoint_and_stays_open_for_the_next_generation(held, control):
    from shared_context.behavior_hosted import NativeCheckpoint
    held.job(children=["waiting-child"])
    if control == "bytes":
        held.set_launch(checkpoint_after_bytes=64)
    else:
        held.edit(native_key("probe", held.report.probe_id), directive="park")
    assert held.send(**held.parking()).state == "parked"
    evidence = held.h.native.verify(held.evidence_id)
    assert evidence.state == "parked" and evidence.result is None
    saved = NativeCheckpoint.model_validate_json(evidence.checkpoint)
    launch = held.launch()
    assert launch.checkpoint == evidence.checkpoint and launch.phase == "sealed"
    assert (saved.predecessor_launch_id, saved.predecessor_evidence_id, saved.child_pid) == (
        held.launch_id, held.evidence_id, held.report.child_pid)
    assert saved.predecessor_binding_digest == _digest(held.binding.model_dump(mode="json"))
    assert (saved.predecessor_process_generation, saved.predecessor_process_id) == (
        launch.process_generation, launch.process_id)
    assert saved.predecessor_identity.session_id == launch.target.session_id
    assert saved.checkpoint.processed_bytes == 64


def test_a_cancel_turns_a_verified_park_into_a_stop_without_a_checkpoint(held):
    held.job(cancel_requested=True)
    assert held.send(**held.parking()).state == "stopped"
    evidence = held.h.native.verify(held.evidence_id)
    assert evidence.state == "stopped" and evidence.checkpoint is None and held.launch().checkpoint is None
    result = evidence.result
    assert (result.evidence_id, result.state, result.passed_checks, result.content) == (
        native_key("result-id", held.launch_id), "cancelled", (),
        b"Constrained execution cancelled after its verified parked checkpoint.")
    assert json.loads(result.proof)["native_evidence_id"] == held.evidence_id


def test_even_a_cancelled_park_must_prove_its_checkpoint(held):
    held.job(cancel_requested=True)
    held.refused(*BAD_CHECKPOINT, **held.parking(checkpoint=held.checkpoint(newline_count=99)))


@pytest.mark.parametrize("changes", [
    {"input_digest": "0" * 64}, {"prefix_digest": "0" * 64}, {"newline_count": 99},
])
def test_a_checkpoint_must_match_the_assigned_bytes(held, changes):
    held.job(children=["waiting-child"])
    held.set_launch(checkpoint_after_bytes=64)
    held.refused(*BAD_CHECKPOINT, **held.parking(checkpoint=held.checkpoint(**changes)))


def test_a_checkpoint_cannot_claim_more_bytes_than_were_assigned(held):
    content = held.h.specs[0].input
    held.job(children=["waiting-child"])
    held.set_launch(checkpoint_after_bytes=64)
    assert len(content) < 8192
    held.refused(*BAD_CHECKPOINT, **held.parking(checkpoint=held.checkpoint(
        processed_bytes=len(content) + 1, prefix_digest=hashlib.sha256(content).hexdigest(),
        newline_count=content.count(b"\n"))))


def test_an_unreadable_checkpoint_is_refused(held):
    held.job(children=["waiting-child"])
    held.set_launch(checkpoint_after_bytes=64)
    held.refused(403, "NATIVE_CHECKPOINT_INVALID", "required checkpoint is invalid", **held.parking(checkpoint=b"{"))


# Assistant workloads, and the slot.

def test_a_sealed_report_needs_the_registered_assistant_verifier(held):
    held.set_workload(ASSISTANT)
    held.refused(403, "NATIVE_ASSISTANT_DISABLED", "assistant verifier is not registered")


def test_a_row_that_became_an_assistant_launch_needs_its_durable_plan(held, monkeypatch):
    snapshot = held.launch()
    held.set_workload(ASSISTANT)
    monkeypatch.setattr(held.h.native, "launch", lambda _launch_id: snapshot)
    with pytest.raises(Problem) as caught:
        held.send()
    assert (caught.value.status, caught.value.body["error"]["code"]) == (403, "NATIVE_ASSISTANT_DISABLED")
    assert caught.value.body["error"]["message"] == "durable assistant result/checkpoint is required"


@pytest.mark.parametrize("workload,assistant", [(None, True), (ASSISTANT, False), (None, False)])
def test_a_no_sdk_report_needs_an_assistant_workload_and_verifier(held, workload, assistant):
    if workload is not None:
        held.set_workload(workload)
    if assistant:
        held.h.native.assistant = SimpleNamespace()
    held.refused(403, "SDK_NO_START_SCOPE", "no-SDK disposition requires its enabled assistant verifier",
                 **no_sdk_changes())


def test_an_assistant_report_stops_when_its_sources_moved_inside_the_transaction(held):
    held.set_workload(ASSISTANT)
    seen = []

    def moved(_rows, snapshot):
        seen.append(snapshot)
        raise Problem(409, "ASSISTANT_SOURCE_CHANGED", "source/review authorization changed at the effect gate")

    held.h.native.assistant = SimpleNamespace(native_report_plan=lambda *_args: (None, None, {}),
                                              verify_native_report_snapshot=moved)
    held.refused(409, "ASSISTANT_SOURCE_CHANGED")
    assert seen == [{}]


def test_a_verifier_refusal_stops_before_anything_is_read_in_the_transaction(held, monkeypatch):
    held.set_workload(ASSISTANT)

    def refuse(_launch, _command, _principal):
        raise Problem(403, "ASSISTANT_NATIVE_IDLE", "the verifier refused")

    held.h.native.assistant = SimpleNamespace(native_report_plan=refuse)
    monkeypatch.setattr(held.h.store, "transact", lambda *_args: pytest.fail("no transaction may start"))
    with pytest.raises(Problem) as caught:
        held.send()
    assert caught.value.body["error"]["code"] == "ASSISTANT_NATIVE_IDLE"


@pytest.mark.parametrize("change", [{"active_launch_id": "another-launch"}, {"generation": 99}])
def test_sealing_refuses_a_slot_held_by_another_execution(held, change):
    target = held.launch().target
    held.edit(held.h.native.target_key(target, held.job_id), **change)
    held.refused(403, "NATIVE_PREDECESSOR_UNSETTLED", "the native target belongs to another execution")


# reconcile_sealed

TERMINAL = (403, "NATIVE_TERMINAL_INVALID")
RETAINED = "retained terminal evidence or generation is inconsistent"
FENCE = "retained native exit/claim fence is inconsistent"


def rejected(held, status, code, message):
    before = held.launch()
    with pytest.raises(Problem) as caught:
        held.h.native.reconcile_sealed(held.binding)
    assert (caught.value.status, caught.value.body["error"]["code"]) == (status, code)
    assert caught.value.body["error"]["message"] == message
    assert held.launch().evidence_sequence == before.evidence_sequence


def reported(held):
    held.send()
    held.evidence_key = native_key("evidence", held.evidence_id)
    held.report_key = native_key("report", held.evidence_id)
    return held


def tamper_report(held, **updates):
    """Change the saved report and keep its row and the evidence's digest matching, so only ``updates`` differ."""
    held.edit(held.report_key, in_step=True, **updates)
    data = held.row(held.report_key).payload["data"]
    held.edit(held.evidence_key, in_step=True, report_digest=_digest(data))


def tamper_evidence(held, **updates):
    def change(data):
        data["evidence"].update(updates)

    held.edit(held.evidence_key, in_step=True, change=change)


def test_a_retained_report_is_verified_again_under_a_new_evidence_id(held):
    reported(held)
    fresh = held.h.native.reconcile_sealed(held.binding)
    assert fresh == native_key("terminal-verification-id", held.launch_id, "2")
    evidence = held.h.native.verify(fresh)
    assert evidence.sequence == 2 and evidence.state == "succeeded"
    assert held.launch().evidence_sequence == 2 and held.launch().sealed_evidence_id == held.evidence_id


def test_an_unsealed_launch_has_no_terminal_proof(held):
    rejected(held, 409, "NATIVE_TERMINAL_UNKNOWN", "no verified permanently fenced native completion exists")


def test_a_foreign_binding_has_no_terminal_proof(held):
    reported(held)
    held.binding = held.binding.model_copy(update={"work_id": "another-work"})
    rejected(held, 409, "NATIVE_TERMINAL_UNKNOWN", "no verified permanently fenced native completion exists")


def test_a_sealed_launch_without_its_evidence_id_has_no_terminal_proof(held):
    reported(held)
    held.set_launch(sealed_evidence_id=None)
    rejected(held, 409, "NATIVE_TERMINAL_UNKNOWN", "no verified permanently fenced native completion exists")


@pytest.mark.parametrize("change", [{"phase": "claimed"}, {"sealed_evidence_id": "another-evidence"},
                                    {"evidence_sequence": 7}])
def test_a_launch_that_moved_under_the_read_is_refused(held, monkeypatch, change):
    reported(held)
    snapshot = held.launch()
    held.set_launch(**change)
    monkeypatch.setattr(held.h.native, "launch", lambda _launch_id: snapshot)
    rejected(held, *TERMINAL, RETAINED)


def test_a_launch_resealed_under_the_read_is_refused_even_with_matching_evidence(held, monkeypatch):
    reported(held)
    snapshot = held.launch()
    tamper_evidence(held, evidence_id="another-evidence")
    held.set_launch(sealed_evidence_id="another-evidence")
    monkeypatch.setattr(held.h.native, "launch", lambda _launch_id: snapshot)
    rejected(held, *TERMINAL, RETAINED)


@pytest.mark.parametrize("updates", [
    {"evidence_id": "another-evidence"},
    {"state": "unknown", "result": None, "entire_tree_idle": False},
    {"state": "unknown", "result": None, "all_inflight_starts_fenced": False},
])
def test_retained_evidence_must_still_prove_the_terminal_fence(held, updates):
    reported(held)
    tamper_evidence(held, **updates)
    rejected(held, *TERMINAL, RETAINED)


def test_retained_evidence_must_keep_its_binding(held):
    reported(held)
    tamper_evidence(held, binding=held.binding.model_copy(update={"generation": 2}).model_dump(mode="json"))
    rejected(held, *TERMINAL, RETAINED)


def test_retained_evidence_must_match_its_material(held):
    reported(held)
    held.edit(held.evidence_key, change=lambda data: data["evidence"].update(state="failed"))
    rejected(held, *TERMINAL, RETAINED)


@pytest.mark.parametrize("updates", [
    {"invocation_id": "another-invocation"}, {"process_id": "another-process"}, {"process_generation": 99},
    {"child_exited": False}, {"child_pid": 99999}, {"phase": "running"},
])
def test_a_retained_report_must_still_be_the_original_owned_exit(held, updates):
    reported(held)
    tamper_report(held, **updates)
    rejected(held, *TERMINAL, FENCE)


def test_retained_evidence_must_name_the_claimed_invocation(held):
    reported(held)
    tamper_evidence(held, native_execution_id="another-invocation")
    rejected(held, *TERMINAL, FENCE)


def test_a_retained_report_must_match_the_digest_the_evidence_saved(held):
    reported(held)
    held.edit(held.report_key, in_step=True, supervisor_id="another-supervisor")
    rejected(held, *TERMINAL, FENCE)


def test_a_retained_report_must_match_its_material(held):
    reported(held)
    held.edit(held.report_key, supervisor_id="another-supervisor")
    data = held.row(held.report_key).payload["data"]
    held.edit(held.evidence_key, in_step=True, report_digest=_digest(data))
    rejected(held, *TERMINAL, FENCE)


def test_a_claimed_launch_that_lost_its_invocation_is_refused(held, monkeypatch):
    reported(held)
    snapshot = held.launch()
    held.set_launch(native_invocation_id=None)
    monkeypatch.setattr(held.h.native, "launch", lambda _launch_id: snapshot)
    rejected(held, *TERMINAL, FENCE)


def retired_or_platform(held, name):
    """Move the sealed evidence to the id a closure of this kind writes."""
    sealed = native_key(name, held.launch_id)
    copy = held.row(held.evidence_key).payload["data"]
    copy = {**copy, "evidence": {**copy["evidence"], "evidence_id": sealed}}
    key = native_key("evidence", sealed)
    held.h.serial += 1
    name = f"copy-{held.h.serial}"
    held.h.store.transact(
        ControlRequest(request_id=name, request_digest=hashlib.sha256(name.encode()).hexdigest(), read_set=(key,)),
        lambda _rows: ControlChanges(writes=(ControlWrite(row_id=key, mode="create", data=copy, material=_material(copy)),),
                                     result={"synthetic_only": True}))
    held.set_launch(sealed_evidence_id=sealed)


def test_a_platform_stop_needs_its_provenance_row(held):
    reported(held)
    retired_or_platform(held, "platform-stop-evidence")
    rejected(held, *TERMINAL, "platform stop provenance is missing")


def test_a_retired_stop_closure_is_never_reconciled(held):
    reported(held)
    retired_or_platform(held, "original-stop-close-evidence")
    rejected(held, *TERMINAL, "original stop closures are retired and cannot be reconciled")


def test_the_closure_id_outranks_a_missing_invocation(held):
    reported(held)
    retired_or_platform(held, "original-stop-close-evidence")
    held.set_launch(native_invocation_id=None)
    rejected(held, *TERMINAL, "original stop closures are retired and cannot be reconciled")


def released_report(held, monkeypatch, *, profile=POC, instruction=True):
    """A retained resident release: no exit, the same pid, and a task captured with ``profile``."""
    reported(held)
    pid = held.report.child_pid
    tamper_report(held, child_exited=False, exit_code=None,
                  sdk_stop=wire.AssistantSdkStop(pid=pid, release=POC).model_dump(mode="json"))
    held.parsed = []

    def parse(raw):
        held.parsed.append(raw)
        return SimpleNamespace(instruction=SimpleNamespace(worker_profile=profile) if instruction else None)

    monkeypatch.setattr(Task, "model_validate_json", parse)


def test_a_resident_release_counts_as_an_owned_exit_for_its_captured_profile(held, monkeypatch):
    released_report(held, monkeypatch)
    held.set_workload(ASSISTANT)
    assert held.h.native.reconcile_sealed(held.binding)
    assert held.parsed == [held.h.specs[0].input]


@pytest.mark.parametrize("options", [{"profile": "fa-sdk-default"}, {"instruction": False}])
def test_a_resident_release_needs_the_task_to_have_chosen_it(held, monkeypatch, options):
    released_report(held, monkeypatch, **options)
    held.set_workload(ASSISTANT)
    rejected(held, *TERMINAL, FENCE)


def test_a_resident_release_is_only_an_assistant_exit(held, monkeypatch):
    released_report(held, monkeypatch)
    rejected(held, *TERMINAL, FENCE)
    assert held.parsed == []


def test_a_resident_release_must_be_the_saved_child(held, monkeypatch):
    released_report(held, monkeypatch)
    held.set_workload(ASSISTANT)
    held.set_launch(child_pid=held.report.child_pid + 1)
    rejected(held, *TERMINAL, FENCE)
    assert held.parsed == []


def test_a_resident_shaped_report_needs_every_part_of_the_release(held, monkeypatch):
    released_report(held, monkeypatch)
    held.set_workload(ASSISTANT)
    tamper_report(held, exit_code=0)
    rejected(held, *TERMINAL, FENCE)
    assert held.parsed == []
