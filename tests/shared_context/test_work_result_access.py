"""Every refusal of the saved-result reader, on the rows of a real synthetic native turn.

The turn runs the real submit, claim, save, forward and fetch path. Each case then edits what the
reader sees, nothing is written, and a forged row must still parse through its real model. A branch
a model already refuses (`work_revision`, a terminal evidence that is not idle) has no case here.
"""
import copy
from types import SimpleNamespace

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_delivery import ArtifactVersion, VerifiedWorkResult, _Payload
from shared_context.behavior_foundry import native_key
from shared_context.behavior_operations import _parse, control_key
from shared_context.behavior_runtime import material
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from test_conversation_transfer_startup import saved_turn, synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401

DIGEST = "0" * 64
TERMINAL = "only the exact saved terminal result is readable"
SAVED = "original worker result material differs"
RETAINED = "exact retained payload/route differs"
NATIVE = "saved result lacks original terminal native proof"
NEVER_STARTED = "only an original fenced never-started cancellation is eligible"


class Reads:
    """Real rows with named edits, for this reader only."""

    def __init__(self, store, monkeypatch):
        self.edits = {}
        real = store.read_snapshot

        def read(keys):
            rows = dict(real(keys))
            for key, edit in self.edits.items():
                if key in rows:
                    rows[key] = edit(rows[key])
            return rows
        monkeypatch.setattr(store, "read_snapshot", read)

    def data(self, key, change):
        previous = self.edits.get(key, lambda row: row)

        def edit(row):
            payload = copy.deepcopy(previous(row).payload)
            change(payload["data"])
            return SimpleNamespace(payload=payload)
        self.edits[key] = edit

    def fields(self, key, **values):
        self.data(key, lambda data: data.update(values))

    def nested(self, key, field, **values):
        self.data(key, lambda data: data[field].update(values))

    def material(self, key, value):
        previous = self.edits.get(key, lambda row: row)
        self.edits[key] = lambda row: SimpleNamespace(
            payload={**previous(row).payload, "material": value.model_dump(mode="json")})

    def other_material(self, key):
        self.material(key, material({"other": True}))

    def resealed(self, key):
        """Refresh the material of an edited row, so its separate material check cannot hide the edit."""
        previous = self.edits.get(key, lambda row: row)

        def edit(row):
            data = previous(row).payload["data"]
            return SimpleNamespace(payload={"data": data, "material": material(data).model_dump(mode="json")})
        self.edits[key] = edit

    def row(self, key, value):
        self.edits[key] = lambda _: value


def reading(c, monkeypatch, binding, request_id):
    """Fetch through the real route once and keep the access it verified."""
    seen = []
    verify = c.control.verify_result_access

    def recorded(access):
        seen.append(access)
        return verify(access)
    with monkeypatch.context() as patch:
        patch.setattr(c.control, "verify_result_access", recorded)
        c.h.runtime.fetch(binding.binding_id, wire.Command(request_id=request_id), c.human)
    b, access = binding, seen[-1]
    result = c.h.runtime.store.read_snapshot((control_key("delivery-work-state", b.obligation_id),))[
        control_key("delivery-work-state", b.obligation_id)].payload["data"]["result"]
    return SimpleNamespace(
        c=c, binding=b, access=access, reads=Reads(c.h.runtime.store, monkeypatch), result=result,
        job=worker_key("job", b.job_id), work=control_key("delivery-work-state", b.obligation_id),
        ob=control_key("delivery-obligation", b.obligation_id),
        out=control_key("delivery-outbox", access.binding.delivery_id),
        saved=worker_key("work-result", result["evidence_id"]),
    )


@pytest.fixture
def finished(synthetic_standing_v1, monkeypatch):  # noqa: F811
    c = synthetic_standing_v1
    turn = saved_turn(c, "result-access-finished")
    c.h.runtime.controller.step(turn.binding.binding_id)
    t = reading(c, monkeypatch, turn.binding, "result-access-fetch")
    row = c.h.runtime.store.read_snapshot((t.job,))[t.job].payload["data"]
    t.launch = native_key("launch", native_key("launch-id", row["binding"]["start_request_id"]))
    t.completion = worker_key("completion", t.binding.job_id, str(row["generation"]))
    return t


@pytest.fixture
def cancelled(synthetic_standing_v1, monkeypatch):  # noqa: F811
    c = synthetic_standing_v1
    binding, _ = c.control.submit(c.human, InstructionSubmit(request_id="result-access-cancelled", content="Never started."))
    c.control.cancel(c.human, binding.binding_id)
    assert c.h.runtime.dep.workers.inspect(binding.job_id).binding is None
    t = reading(c, monkeypatch, binding, "result-access-cancelled-fetch")
    t.completion = worker_key("completion", binding.job_id, "0")
    return t


def refused(t, message):
    with pytest.raises(Problem, match=message) as denied:
        t.c.control.verify_result_access(t.access)
    assert denied.value.status == 403 and denied.value.body["error"]["code"] == "WORK_RESULT_ACCESS_DENIED"


def access_with(t, **values):
    t.access = t.access.model_copy(update=values)


def out_binding(t, **values):
    """Change the retained binding and the access together, so the reader gets past `access.binding`."""
    forged = t.access.binding.model_copy(update=values)
    t.reads.fields(t.out, binding=forged.model_dump(mode="json"))
    access_with(t, binding=forged)


def test_untouched_finished_rows_pass(finished):
    assert finished.c.control.verify_result_access(finished.access) is None


def test_untouched_never_started_rows_pass(cancelled):
    assert cancelled.c.control.verify_result_access(cancelled.access) is None


def test_only_a_current_work_access_is_accepted(finished):
    from shared_context.behavior_inbox import InboxFetch
    forged = InboxFetch(obligation_id="o", delivery_id="d", access_evidence_id="e")
    with pytest.raises(Problem, match="explicit current result access required") as denied:
        finished.c.control.verify_result_access(forged)
    assert denied.value.status == 403 and denied.value.body["error"]["code"] == "WORK_RESULT_ACCESS_DENIED"


def test_an_unregistered_reader_is_refused(finished):
    access_with(finished, runtime_binding_id="not-registered")
    refused(finished, "registered original result reader required")


def test_the_job_owner_epoch_alone_does_not_block_the_read(finished):
    finished.reads.nested(finished.job, "owner", epoch=99)
    assert finished.c.control.verify_result_access(finished.access) is None


TERMINAL_CASES = {
    "access_policy": lambda t: access_with(t, work_policy_id="another-policy"),
    "access_principal": lambda t: access_with(t, principal_id="another-person"),
    "access_binding": lambda t: access_with(t, binding=t.access.binding.model_copy(update={"payload_digest": DIGEST})),
    "access_authentication": lambda t: access_with(t, authentication_version="older-version"),
    "access_revision": lambda t: access_with(t, policy_revision=t.access.policy_revision + 1),
    "job_not_terminal": lambda t: t.reads.fields(t.job, state="running"),
    "job_not_forwarded": lambda t: t.reads.fields(t.job, result_forwarded=False),
    "job_keeps_credit": lambda t: t.reads.fields(t.job, completion_credit=1),
    "work_definition": lambda t: t.reads.nested(t.work, "definition", goal="Another goal."),
    "work_state": lambda t: t.reads.fields(t.work, state="failed"),
    "work_result_missing": lambda t: t.reads.fields(t.work, result=None),
    "result_work": lambda t: t.reads.nested(t.work, "result", work_id="another-work"),
    "result_state": lambda t: t.reads.nested(t.work, "result", state="failed"),
    "result_definition": lambda t: t.reads.nested(t.work, "result", definition_digest=DIGEST),
    "result_verifier": lambda t: t.reads.nested(t.work, "result", verifier_version="another-verifier"),
    "spec_job": lambda t: t.reads.nested(t.job, "spec", job_id="another-job"),
    "spec_work": lambda t: t.reads.nested(t.job, "spec", work_id="another-work"),
    "spec_obligation": lambda t: t.reads.nested(t.job, "spec", obligation_id="another-obligation"),
    "spec_input": lambda t: t.reads.nested(t.job, "spec", input="b3RoZXI="),
    "obligation_route": lambda t: t.reads.nested(t.ob, "route", recipient_id="another-person"),
    "obligation_delivery": lambda t: t.reads.fields(t.ob, final_delivery_id="another-delivery"),
    "outbox_route": lambda t: out_binding(t, route=t.access.binding.route.model_copy(update={"recipient_id": "another-person"})),
    "outbox_obligation": lambda t: out_binding(t, obligation_id="another-obligation"),
    "job_owner": lambda t: t.reads.nested(t.job, "owner", conversation_id="another-conversation"),
}


@pytest.mark.parametrize("case", TERMINAL_CASES)
def test_a_result_that_is_not_the_exact_terminal_one_is_refused(finished, case):
    TERMINAL_CASES[case](finished)
    refused(finished, TERMINAL)


SAVED_CASES = {
    "missing": lambda t: t.reads.row(t.saved, None),
    "different": lambda t: t.reads.fields(t.saved, verifier_version="another-verifier"),
    "material": lambda t: t.reads.other_material(t.saved),
}


@pytest.mark.parametrize("case", SAVED_CASES)
def test_the_worker_result_row_must_equal_the_delivered_result(finished, case):
    SAVED_CASES[case](finished)
    if case == "different":
        finished.reads.resealed(finished.saved)
    refused(finished, SAVED)


RETAINED_CASES = {
    "kind": lambda t: t.reads.fields(t.out, message_kind="failure"),
    "digest": lambda t: out_binding(t, payload_digest=DIGEST),
    "artifacts": lambda t: out_binding(t, artifacts=(ArtifactVersion(artifact_id="artifact", version="1", sha256=DIGEST),)),
    "material": lambda t: t.reads.other_material(t.out),
}


@pytest.mark.parametrize("case", RETAINED_CASES)
def test_the_retained_outbox_must_be_this_results_payload(finished, case):
    RETAINED_CASES[case](finished)
    refused(finished, RETAINED)


NATIVE_CASES = {
    "launch_phase": lambda t: t.reads.fields(t.launch, phase="claimed"),
    "completed_binding": lambda t: t.reads.nested(t.completion, "binding", executor_version="another-version"),
    "completed_result": lambda t: t.reads.fields(t.completion, result=None),
    "launch_binding": lambda t: t.reads.data(t.launch, lambda d: d["start"]["binding"].update(executor_version="another-version")),
    "launch_api_binding": lambda t: t.reads.fields(t.launch, api_binding_id="another-binding"),
    "launch_definition": lambda t: t.reads.nested(t.launch, "definition", goal="Another goal."),
    "launch_configuration": lambda t: t.reads.fields(t.launch, configuration_digest=DIGEST),
    "completed_state": lambda t: t.reads.fields(t.completion, state="failed"),
    "completed_evidence": lambda t: t.reads.fields(t.completion, evidence_id="another-evidence"),
    "completed_execution": lambda t: t.reads.fields(t.completion, native_execution_id="another-invocation"),
    "completed_material": lambda t: t.reads.other_material(t.completion),
}


@pytest.mark.parametrize("case", NATIVE_CASES)
def test_a_native_result_needs_its_sealed_launch_and_completion(finished, case):
    NATIVE_CASES[case](finished)
    if case != "completed_material":
        finished.reads.resealed(finished.completion)
    refused(finished, NATIVE)


def stopped_execution(t, state):
    t.reads.fields(t.completion, state=state, result=ended_as(t, "cancelled"))
    t.reads.resealed(t.completion)


def test_a_started_job_that_was_cancelled_is_proved_by_a_stopped_execution(finished):
    stopped_execution(finished, "stopped")
    assert finished.c.control.verify_result_access(finished.access) is None


def test_a_cancelled_job_is_not_proved_by_a_failed_execution(finished):
    stopped_execution(finished, "failed")
    refused(finished, NATIVE)


@pytest.mark.parametrize("name", ["launch", "completion"])
def test_a_missing_native_proof_row_stays_a_conflict(finished, name):
    finished.reads.row(getattr(finished, name), None)
    with pytest.raises(Problem) as denied:
        finished.c.control.verify_result_access(finished.access)
    assert denied.value.status == 409 and denied.value.body["error"]["code"] == "OPERATION_RECORD_MISSING"


def ended_as(t, state):
    """Keep every row consistent up to the last gate, as if this job had ended in `state`."""
    b = t.binding
    result = _parse({**t.result, "state": state}, VerifiedWorkResult)
    dumped = result.model_dump(mode="json")
    payload = material(_Payload(
        event_id=b.event_id, root_request_id=b.root_request_id, work_id=b.work.work_id, message_kind="failure",
        work_state=state, result_evidence_id=result.evidence_id, content=result.content, artifacts=result.artifacts,
        known_effects=result.known_effects, unresolved_operations=result.unresolved_operations,
        responsibility_owner=b.owner.owner_record_id, notification_code="delivery_needs_attention",
    ).model_dump(mode="json"))
    t.reads.fields(t.job, state=state)
    t.reads.fields(t.work, state=state, result=dumped)
    t.reads.row(t.saved, SimpleNamespace(payload={"data": dumped, "material": material(dumped).model_dump(mode="json")}))
    t.reads.fields(t.out, message_kind="failure")
    out_binding(t, payload_digest=payload.sha256)
    t.reads.material(t.out, payload)
    return dumped


NEVER_STARTED_CASES = {
    "job_generation": lambda t: t.reads.fields(t.job, generation=1),
    "job_not_cancel_requested": lambda t: t.reads.fields(t.job, cancel_requested=False),
    "evidence_spec": lambda t: t.reads.nested(t.completion, "spec", parent_job_id="another-job"),
    "evidence_owner": lambda t: t.reads.nested(t.completion, "owner", conversation_id="another-conversation"),
    "evidence_generation": lambda t: t.reads.fields(t.completion, generation=1),
    "evidence_prior": lambda t: t.reads.fields(t.completion, prior_evidence_id="an-earlier-evidence"),
    "evidence_children": lambda t: t.reads.fields(t.completion, children=["a-child"]),
    "evidence_id": lambda t: t.reads.fields(t.completion, evidence_id="another-evidence"),
    "evidence_material": lambda t: t.reads.other_material(t.completion),
    "attempt_exists": lambda t: t.reads.row(worker_key("attempt", t.binding.job_id, "1"), SimpleNamespace(payload={})),
    "launch_exists": lambda t: t.reads.row(
        native_key("launch", native_key("launch-id", worker_key("start", t.binding.job_id, "1"))),
        SimpleNamespace(payload={})),
}


@pytest.mark.parametrize("case", NEVER_STARTED_CASES)
def test_a_cancellation_without_a_launch_must_prove_nothing_ever_started(cancelled, case):
    NEVER_STARTED_CASES[case](cancelled)
    if case != "evidence_material":
        cancelled.reads.resealed(cancelled.completion)
    refused(cancelled, NEVER_STARTED)


def test_a_never_started_job_that_is_not_cancelled_is_refused(cancelled):
    ended_as(cancelled, "failed")
    refused(cancelled, NEVER_STARTED)


def test_a_never_started_cancellation_cannot_name_an_execution(cancelled):
    t = cancelled
    binding = {"job_id": t.binding.job_id, "work_id": t.binding.work.work_id, "generation": 1,
               "owner": t.binding.owner.model_dump(mode="json"), "executor_id": "executor", "executor_version": "1",
               "input_digest": DIGEST, "start_request_id": "another-start"}
    t.reads.fields(t.completion, binding=binding)
    t.reads.resealed(t.completion)
    refused(t, NEVER_STARTED)
