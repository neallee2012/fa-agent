"""save_reply refusals, saved bytes and read set; native-mode startup fixture, no process or model."""

from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
import zlib

import pytest

from shared_context import behavior_assistant as assistant_module
from shared_context import behavior_assistant_checkpoint as checkpoint_module
from shared_context import behavior_assistant_wire as aw
from shared_context import behavior_instruction as instruction_module
from shared_context import behavior_sdk_checkpoint as sdk_checkpoint_module
from shared_context.behavior_assistant import key
from shared_context.behavior_instruction import (
    conversation_key, failure_notice, reply_proof, reply_reference, save_reply, turn_material, SavedReply, TurnArchive,
)
from shared_context.behavior_instruction_progress import save_progress
from shared_context.behavior_operations import control_key
from shared_context.behavior_sdk_checkpoint import save_state
from shared_context.behavior_sdk_state import SaveSdkState, SdkState
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite, InlineMaterial
from shared_context.behavior_delivery import VerifiedWorkResult, _material
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_conversation_transfer_startup import claimed_turn, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key

# The goldens below were written with runtime 1.0.83, still a known release; pinned here so they do not move with sdk_pin.
STOP = aw.AssistantSdkStop(runtime_version="1.0.83", pid=99, exit_code=0, graceful=True)
UNAVAILABLE = aw.ArchiveUnavailable(error="SDK_ARCHIVE_UNAVAILABLE")
TEXT = "Original saved answer. 原文"
AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def reply(opened, **changes):
    task = opened.assignment.task.instruction
    return aw.TextReply(**{"text": TEXT, "sdk_session_id": opened.assignment.sdk_session_id,
                           "conversation_id": task.conversation_id, "turn_number": task.turn_number, **changes})


def command(opened, **changes):
    return aw.SaveTextReply(**{"request_id": "save-text-reply", "sdk_stop": STOP, "archive": UNAVAILABLE,
                               "reply": reply(opened), **changes})


def save(c, opened, **changes):
    return c.authority.save_text_reply(opened.session, command(opened, **changes), opened.agent)


def row(c, row_id):
    return c.h.runtime.store.read_snapshot((row_id,))[row_id]


def tamper(c, row_id, **updates):
    """Replace fields of one retained row inside a real transaction, keeping its material."""
    request = ControlRequest(request_id=control_key("tamper", row_id, *updates),
                             request_digest=hashlib.sha256(row_id.encode()).hexdigest(), read_set=(row_id,))

    def change(rows):
        payload = rows[row_id].payload
        material = None if payload["material"] is None else InlineMaterial.model_validate_json(json.dumps(payload["material"]))
        return ControlChanges(writes=(ControlWrite(row_id=row_id, mode="replace", data={**payload["data"], **updates},
                                                   material=material),), result={"synthetic_only": True})

    c.h.runtime.store.transact(request, change)


def nothing_saved(c, opened):
    binding = opened.binding
    assert all(row(c, row_id) is None for row_id in (
        key("result", binding.work.work_id), key("reply", binding.job_id), key("text-archive", binding.job_id)))


def refused(c, opened, status, expected, **changes):
    before = c.h.runtime.store._guard()
    with pytest.raises(Problem) as error:
        save(c, opened, **changes)
    assert (error.value.status, error.value.body["error"]["code"]) == (status, expected)
    assert c.h.runtime.store._guard() == before
    nothing_saved(c, opened)


def test_only_an_original_instruction_save_is_accepted(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "other-command")
    refused(c, opened, 403, "INSTRUCTION_RESULT_DENIED", request_id="another-request")
    not_instruction = SimpleNamespace(assignment=SimpleNamespace(task=SimpleNamespace(instruction=None)))
    with pytest.raises(Problem) as error:
        save_reply(None, not_instruction, command(opened))
    assert error.value.body["error"]["code"] == "INSTRUCTION_RESULT_DENIED"


@pytest.mark.parametrize("changes", [
    {"reply": None},
    {"sdk_stop": aw.AssistantSdkStop(pid=99, release="fa-resident-worker-poc-v1")},
], ids=["no-reply-no-failure", "release-not-captured"])
def test_save_requires_the_original_stopped_sdk(synthetic_standing_v1, changes):
    c = synthetic_standing_v1
    refused(c, claimed_turn(c, "stopped-sdk"), 403, "INSTRUCTION_RESULT_DENIED", **changes)


@pytest.mark.parametrize("changes", [
    {"conversation_id": "another-conversation"}, {"turn_number": 2}, {"sdk_session_id": "another-sdk-session"},
], ids=["conversation", "turn", "sdk-session"])
def test_save_requires_a_reply_for_this_turn(synthetic_standing_v1, changes):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "bound-reply")
    refused(c, opened, 403, "INSTRUCTION_RESULT_DENIED", reply=reply(opened, **changes))


def test_model_limit_is_only_for_an_agent_turn_without_a_reply(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "human-model-limit")
    refused(c, opened, 403, "INSTRUCTION_RESULT_DENIED", failure="model_call_limit_reached", reply=None)
    refused(c, opened, 403, "INSTRUCTION_RESULT_DENIED", failure="model_call_limit_reached")


def stored_state(c, opened):
    sid = opened.assignment.sdk_session_id
    state = SdkState(sdk_session_id=sid, files={
        "events.jsonl": "".join(json.dumps(event) + "\n" for event in (
            {"type": "session.start", "data": {"sessionId": sid}},
            {"type": "user.message", "data": {"content": opened.assignment.task.instruction.content}},
            {"type": "assistant.message", "data": {"content": TEXT}},
        )),
        "workspace.yaml": "id: " + sid + "\n",
    })
    return save_state(c.authority, opened.session, SaveSdkState(request_id="sdk-state", state=state, sdk_stop=STOP))


def test_save_rejects_a_reply_that_cannot_fit_the_protected_envelope(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "too-large")
    noisy = hashlib.sha256(b"seed").hexdigest()
    while len(noisy) < 70_000:
        noisy += hashlib.sha256(noisy[-64:].encode()).hexdigest()
    refused(c, opened, 413, "INSTRUCTION_REPLY_STORAGE_LIMIT", reply=reply(opened, text=noisy))


def test_save_rejects_a_compressible_reply_over_the_raw_limit(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "too-long")
    text = "a" * (4 * 1024 * 1024 + 1)
    assert len(zlib.compress(text.encode())) <= 32768
    refused(c, opened, 413, "INSTRUCTION_REPLY_STORAGE_LIMIT", reply=reply(opened, text=text))


@pytest.mark.parametrize("target,updates", [
    ("job", {"state": "queued"}),
    ("session", {"state": "ready_to_finalize"}),
    ("conversation", {"active_job_id": None}),
    ("conversation", {"sdk_session_id": "another-sdk-session"}),
    ("conversation", {"jobs": ["another-job"]}),
], ids=["job-state", "session-state", "conversation-active", "conversation-sdk", "conversation-last-job"])
def test_save_is_fenced_when_the_turn_or_conversation_changed(synthetic_standing_v1, target, updates):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "fenced")
    keys = {"job": worker_key("job", opened.binding.job_id),
            "session": key("session", opened.assignment.session_id),
            "conversation": conversation_key(opened.assignment.task.instruction.conversation_id)}
    tamper(c, keys[target], **updates)
    with pytest.raises(Problem) as error:
        save(c, opened, failure="sdk_failed", reply=None)
    assert error.value.body["error"]["code"] == "INSTRUCTION_RESULT_FENCED"
    nothing_saved(c, opened)


def test_failure_named_cancelled_requires_an_accepted_cancel(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "unaccepted-cancel")
    with pytest.raises(Problem) as error:
        save(c, opened, failure="cancelled", reply=None)
    assert error.value.body["error"]["code"] == "INSTRUCTION_RESULT_FENCED"
    nothing_saved(c, opened)


def test_save_is_fenced_when_the_session_runs_another_execution(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "other-execution")
    session_key = key("session", opened.assignment.session_id)
    execution = row(c, session_key).payload["data"]["execution"]
    tamper(c, session_key, execution={**execution, "start_request_id": "f" * 64})
    with pytest.raises(Problem) as error:
        save(c, opened, failure="sdk_failed", reply=None)
    assert error.value.body["error"]["code"] == "INSTRUCTION_RESULT_FENCED"
    nothing_saved(c, opened)


@pytest.mark.parametrize("change", ["binding", "owner", "jobs", "turns"])
def test_save_is_fenced_on_each_retained_execution_term(synthetic_standing_v1, change):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "fenced-terms")
    job_key = worker_key("job", opened.binding.job_id)
    conversation = conversation_key(opened.assignment.task.instruction.conversation_id)
    job = row(c, job_key).payload["data"]
    if change == "binding":
        tamper(c, job_key, binding={**job["binding"], "start_request_id": "f" * 64})
    elif change == "owner":
        tamper(c, job_key, owner={**job["owner"], "epoch": job["owner"]["epoch"] + 1})
    elif change == "jobs":
        tamper(c, conversation, jobs=[opened.binding.job_id, "later-job"])
    else:
        tamper(c, conversation, jobs=["earlier-job", opened.binding.job_id])
    with pytest.raises(Problem) as error:
        save(c, opened, failure="sdk_failed", reply=None)
    assert error.value.body["error"]["code"] == "INSTRUCTION_RESULT_FENCED"
    nothing_saved(c, opened)


def test_saved_checkpoint_reference_must_still_match(synthetic_standing_v1):
    from shared_context.behavior_sdk_checkpoint import checkpoint_key
    c = synthetic_standing_v1
    opened = claimed_turn(c, "changed-reference")
    reference = stored_state(c, opened)
    saved = row(c, checkpoint_key(opened.binding.job_id)).payload["data"]
    tamper(c, checkpoint_key(opened.binding.job_id), reference={**saved["reference"], "sha256": "0" * 64})
    with pytest.raises(Problem) as error:
        save(c, opened, archive=reference)
    assert error.value.body["error"]["code"] == "SDK_STATE_INCOMPLETE"
    nothing_saved(c, opened)


def progress(*seqs):
    return aw.PublicProgress(events=tuple(aw.PublicProgressEvent(
        seq=seq, at=AT, kind="stage", status="started", summary="SDK turn started") for seq in seqs))


def saved_progress(c, opened):
    data = row(c, key("progress", opened.assignment.session_id)).payload["data"]
    return aw.PublicProgress.model_validate_json(json.dumps(data["progress"]))


def test_final_progress_is_saved_and_cannot_replace_newer_events(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "progress")
    progress_key = key("progress", opened.assignment.session_id)
    save_progress(c.authority, opened.session, aw.ProgressUpdate(request_id="progress-1", progress=progress(1, 2, 3)))
    assert saved_progress(c, opened) == progress(1, 2, 3)
    refused(c, opened, 409, "INSTRUCTION_PROGRESS_INVALID", progress=progress(1, 2))
    save(c, opened, progress=progress(1, 2, 3, 4))
    assert [event["seq"] for event in row(c, progress_key).payload["data"]["progress"]["events"]] == [1, 2, 3, 4]


def test_first_final_progress_creates_its_row(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "first-progress")
    save(c, opened, progress=progress(1))
    assert saved_progress(c, opened) == progress(1)


def rows_of(c, opened):
    binding = opened.binding
    names = {"result": key("result", binding.work.work_id), "reply": key("reply", binding.job_id),
             "archive": key("text-archive", binding.job_id)}
    return {name: row(c, row_id) for name, row_id in names.items()}


def test_saved_reply_rows_and_read_set_are_exact(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "exact-reply")
    binding, sid = opened.binding, opened.assignment.session_id
    sent = command(opened)
    receipt = c.authority.save_text_reply(opened.session, sent, opened.agent)
    saved = rows_of(c, opened)
    raw = sent.reply.model_dump_json().encode("utf-8")
    packed = SavedReply.model_validate_json(json.dumps(saved["reply"].payload["data"]))
    assert packed.job_id == binding.job_id and packed.data == zlib.compress(raw)
    assert packed.sha256 == hashlib.sha256(raw).hexdigest() and packed.byte_length == len(raw)
    result = VerifiedWorkResult.model_validate_json(json.dumps(saved["result"].payload["data"]))
    assert result.state == "succeeded" and result.evidence_id == key("result", binding.work.work_id)
    assert result.content == reply_reference(packed)
    assert result.proof == reply_proof(opened.assignment.sdk_session_id, UNAVAILABLE, STOP, None)
    assert result.definition_digest == digest(binding.work.model_dump(mode="json"))
    assert result.verifier_version == c.h.runtime.dep.delivery.contract.verifier_version
    assert result.passed_checks == aw.TEXT_CHECKS and result.known_effects == () and result.unresolved_operations == ()
    assert saved["result"].payload["material"] == _material(saved["result"].payload["data"]).model_dump(mode="json")
    archive = TurnArchive.model_validate_json(json.dumps(saved["archive"].payload["data"]))
    assert saved["archive"].payload["material"] == turn_material(archive).model_dump(mode="json")
    assert (archive.conversation_id, archive.job_id, archive.turn_number) == (
        opened.assignment.task.instruction.conversation_id, binding.job_id, 1)
    assert archive.archive == UNAVAILABLE and archive.sdk_stop == STOP and archive.failure is None
    assert receipt.state == "ready_to_finalize" and receipt.work_outcome == "succeeded"
    assert receipt.result_digest == digest(saved["result"].payload["data"])
    original = c.h.runtime.store.reconcile(ControlRequest(
        request_id=key("text-reply", sid), request_digest=digest(sent.model_dump(mode="json")),
        read_set=tuple(dict.fromkeys((
            *c.authority._keys(opened.session), conversation_key(archive.conversation_id),
            key("text-archive", binding.job_id), key("result", binding.work.work_id), key("reply", binding.job_id),
            control_key("operation", binding.operation_id),
        )))))
    assert original.payload["result"] == {"state": "ready_to_finalize", "result_digest": receipt.result_digest,
                                          "work_outcome": "succeeded"}
    session = row(c, key("session", sid)).payload["data"]
    conversation = row(c, conversation_key(archive.conversation_id)).payload["data"]
    assert session["state"] == "ready_to_finalize"
    assert conversation["active_job_id"] is None and conversation["archive_id"] == key("text-archive", binding.job_id)


def test_saved_failure_keeps_the_notice_and_no_reply(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "exact-failure")
    receipt = save(c, opened, failure="sdk_failed", reply=None)
    saved = rows_of(c, opened)
    assert saved["reply"] is None and receipt.work_outcome == "failed"
    result = VerifiedWorkResult.model_validate_json(json.dumps(saved["result"].payload["data"]))
    assert result.state == "failed" and result.passed_checks == ()
    assert result.content == failure_notice("failed", "sdk_failed")
    assert result.proof == reply_proof(opened.assignment.sdk_session_id, UNAVAILABLE, STOP, "sdk_failed")


def test_accepted_cancel_wins_over_a_saved_reply(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "exact-cancel")
    c.control.cancel(c.human, opened.binding.binding_id)
    receipt = save(c, opened)
    saved = rows_of(c, opened)
    assert receipt.work_outcome == "cancelled" and saved["reply"] is not None
    result = VerifiedWorkResult.model_validate_json(json.dumps(saved["result"].payload["data"]))
    assert result.state == "cancelled" and result.passed_checks == ()
    assert result.proof == reply_proof(opened.assignment.sdk_session_id, UNAVAILABLE, STOP, "cancelled")
    archive = TurnArchive.model_validate_json(json.dumps(saved["archive"].payload["data"]))
    assert archive.failure is None


def test_a_replay_of_the_same_save_returns_the_original_receipt(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "replay")
    first = save(c, opened)
    guard = c.h.runtime.store._guard()
    assert save(c, opened) == first
    assert c.h.runtime.store._guard() == guard


def sha(record):
    return hashlib.sha256(json.dumps(record.payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# Hashes captured from the composition before save_reply was split.
GOLDEN = {
    "cancelled": (
        "3d9fc7e717d4cc8096c62777c8b60a1b1abd29b48187f8804b49b2806082fa2b",
        "29ef2ed3f873d61d11e6d8500580c810a1c96a10eac2a6b1db28e89b38535741",
        {
            "result": "a6258a366c27aa1bee0bcae2685b45c7d64495f31373727aa581bb0bb8eb5a7f",
            "reply": "1eb7fe3770f6c0a05872f9164727f49994908dc55ff16d4d7efd922e50fd3300",
            "archive": "d7bde8a8fb8f57b578645c1e79da5b20cceb02f329848ee112c3db7195e2b122",
        },
    ),
    "failure": (
        "af808379f460b3b2efd56988cb3c45a3c8151e0d84cb478810e68a24ed0271c5",
        "7189302aeb2764fb390f9c3d1c2bb53df2633bbdc24c9a8024424ccb88f00802",
        {
            "result": "371f725559b75004be8275d6ca066662bc149b8ad977d07603c992cf9f6bb1f4",
            "reply": None,
            "archive": "4e705f09c4714765ad6dab16a00a2b80a86e7c0c11508c766c4f6d86274c2b2c",
        },
    ),
    "reply": (
        "4259b0fbebb677da8c9fd457bbb70f48145c22fad36cb5affba3a204d5ad7877",
        "8b11011e7206880f534327e71674557d004df63463a1a54dee7aea2dd01b7f09",
        {
            "result": "6484a112c6b4a78ba2ca379ca127a223d7a16cbbd01eb8a2aa96672167bc75ac",
            "reply": "bec2a107514331108c072e52b23b8b6d6b5f85aef968134cad9ebb97f64ff603",
            "archive": "10757004d62402db3cc89a3c7ba7f47f9ded61f5611cce4677f618074788e52e",
        },
    ),
}


@pytest.mark.parametrize("scenario", ["reply", "failure", "cancelled"])
def test_saved_bytes_are_exactly_the_original_composition(synthetic_standing_v1, scenario):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "golden-" + scenario)
    if scenario == "cancelled":
        c.control.cancel(c.human, opened.binding.binding_id)
    sent = command(opened, **({"failure": "sdk_failed", "reply": None} if scenario == "failure" else {}))
    receipt = c.authority.save_text_reply(opened.session, sent, opened.agent)
    saved = rows_of(c, opened)
    assert (receipt.result_digest, digest(sent.model_dump(mode="json")),
            {name: None if value is None else sha(value) for name, value in saved.items()}) == GOLDEN[scenario]


def past_the_limit_check(c, opened):
    """The synthetic agent marker stops the save later; only the limit refusal matters here."""
    with pytest.raises(Exception) as error:
        save_reply(c.authority, opened.session, command(opened, failure="model_call_limit_reached", reply=None))
    assert not (isinstance(error.value, Problem) and error.value.body["error"]["code"] == "INSTRUCTION_RESULT_DENIED")


def agent_turn(c, opened, monkeypatch):
    records = c.authority.runtime.work_policy.records
    monkeypatch.setitem(records, opened.session.binding_id,
                        records[opened.session.binding_id].model_copy(update={"agent_origin": object()}))


def test_model_limit_on_an_agent_turn_needs_no_reply_and_refuses_one(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "agent-model-limit")
    agent_turn(c, opened, monkeypatch)
    refused(c, opened, 403, "INSTRUCTION_RESULT_DENIED", failure="model_call_limit_reached")
    past_the_limit_check(c, opened)


def test_model_limit_is_refused_for_a_turn_without_a_registration(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "unregistered-limit")
    monkeypatch.delitem(c.authority.runtime.work_policy.records, opened.session.binding_id)
    refused(c, opened, 403, "INSTRUCTION_RESULT_DENIED", failure="model_call_limit_reached", reply=None)


def test_model_limit_is_also_allowed_on_a_peer_task(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "peer-model-limit")
    object.__setattr__(opened.session.assignment.task.instruction, "peer_work", object())
    past_the_limit_check(c, opened)


def test_save_refuses_an_archive_of_another_sdk_session(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "other-archive")
    reference = stored_state(c, opened)
    monkeypatch.setattr(sdk_checkpoint_module, "load_state", lambda *args, **kwargs: None)
    refused(c, opened, 403, "INSTRUCTION_RESULT_DENIED",
            archive=reference.model_copy(update={"sdk_session_id": "another-sdk-session"}))


def test_save_rereads_the_stored_checkpoint_first(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "stored-reread")
    reference, asked = stored_state(c, opened), []

    def load_state(*args, **kwargs):
        asked.append((args, kwargs))
        raise Problem(409, "SDK_STATE_INCOMPLETE", "synthetic unreadable state")

    monkeypatch.setattr(sdk_checkpoint_module, "load_state", load_state)
    refused(c, opened, 409, "SDK_STATE_INCOMPLETE", archive=reference)
    assert asked == [((c.authority, opened.session, reference), {"job_id": opened.session.execution.job_id})]


def test_save_opens_an_inline_archive_first(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "inline-archive")
    raw = b"{}"
    archive = aw.SdkArchive(sdk_session_id=opened.assignment.sdk_session_id, data=zlib.compress(raw),
                            sha256=hashlib.sha256(raw).hexdigest())

    def unpack(value):
        raise Problem(409, "SDK_STATE_INCOMPLETE", "synthetic unreadable archive")

    monkeypatch.setattr(checkpoint_module, "unpack", unpack)
    refused(c, opened, 409, "SDK_STATE_INCOMPLETE", archive=archive)


def refuse(code):
    def refusal(*args, **kwargs):
        raise Problem(409, code, "synthetic refusal")
    return refusal


def test_a_saved_reply_passes_the_work_gate(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "gated")
    monkeypatch.setattr(c.authority, "_gate", refuse("SYNTHETIC_GATE"))
    refused(c, opened, 409, "SYNTHETIC_GATE")


def refuse_reads(c, monkeypatch):
    runtime, state = c.authority.runtime, c.authority.runtime._state

    def read(binding, rows, **options):
        if options.get("use") == "read":
            raise Problem(409, "SYNTHETIC_READ", "synthetic refusal")
        return state(binding, rows, **options)

    monkeypatch.setattr(runtime, "_state", read)


def test_a_failure_save_still_needs_readable_state(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "failure-read")
    refuse_reads(c, monkeypatch)
    refused(c, opened, 409, "SYNTHETIC_READ", failure="sdk_failed", reply=None)


def test_a_copy_turn_needs_readable_state_before_its_gate(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "copy-read")
    object.__setattr__(opened.session.assignment.task.instruction, "sop_copy", object())
    refuse_reads(c, monkeypatch)
    with pytest.raises(Problem) as error:
        save_reply(c.authority, opened.session, command(opened))
    assert error.value.body["error"]["code"] == "SYNTHETIC_READ"


def test_an_unresolved_operation_fails_the_saved_work(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "unresolved")
    monkeypatch.setattr(c.authority.runtime.dep.workers, "_operation_effects",
                        lambda *args: (("applied",), ("still-open",)))
    assert save(c, opened).work_outcome == "failed"
    saved = rows_of(c, opened)
    result = VerifiedWorkResult.model_validate_json(json.dumps(saved["result"].payload["data"]))
    packed = SavedReply.model_validate_json(json.dumps(saved["reply"].payload["data"]))
    assert (result.state, result.known_effects, result.unresolved_operations) == ("failed", ("applied",), ("still-open",))
    assert result.passed_checks == () and result.content == reply_reference(packed)


def test_final_progress_may_repeat_the_saved_last_event(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "same-progress")
    save_progress(c.authority, opened.session, aw.ProgressUpdate(request_id="progress-1", progress=progress(1, 2)))
    assert save(c, opened, progress=progress(1, 2)).work_outcome == "succeeded"
    assert saved_progress(c, opened) == progress(1, 2)
    result = VerifiedWorkResult.model_validate_json(json.dumps(rows_of(c, opened)["result"].payload["data"]))
    assert result.state == "succeeded" and result.evidence_id == key("result", opened.binding.work.work_id)


def test_read_set_lists_each_row_once(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "unique-reads")
    keys, transact = c.authority._keys, c.h.runtime.store.transact
    conversation = conversation_key(opened.assignment.task.instruction.conversation_id)
    monkeypatch.setattr(c.authority, "_keys", lambda session: (*keys(session), conversation))
    asked = []
    monkeypatch.setattr(c.h.runtime.store, "transact", lambda request, decide: (asked.append(request), transact(request, decide))[1])
    save(c, opened)
    reads = next(request.read_set for request in asked if request.request_id == key("text-reply", opened.assignment.session_id))
    assert len(reads) == len(set(reads)) and conversation in reads


def test_native_save_keeps_the_ledger_conflict_retry(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "native-retry")
    real, calls = assistant_module._ledger_conflicts, []

    def retried(store, request, decide):
        calls.append(request.request_id)
        return real(store, request, decide)

    monkeypatch.setattr(assistant_module, "_ledger_conflicts", retried)
    assert c.authority.runtime.mode == "native"
    save(c, opened)
    assert key("text-reply", opened.assignment.session_id) in calls


def test_a_reply_exactly_at_the_compressed_limit_is_saved(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "at-limit")
    monkeypatch.setattr(instruction_module, "zlib", SimpleNamespace(compress=lambda raw: b"x" * 32768))
    save(c, opened)
    assert SavedReply.model_validate_json(json.dumps(rows_of(c, opened)["reply"].payload["data"])).data == b"x" * 32768
