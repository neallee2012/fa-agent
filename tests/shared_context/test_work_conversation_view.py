"""What `WorkPolicyControl.conversation` shows, and which gate decides `follow_up_blocker`.

One real synthetic native turn is saved first. Each case then edits what the reader sees and
nothing is written. Later gates replace earlier reasons, so the order cases matter here.
"""
from types import SimpleNamespace

import pytest

from shared_context.behavior_assistant import key as assistant_key
from shared_context.behavior_assistant_wire import ArchiveUnavailable
from shared_context.behavior_instruction import TurnArchive, conversation_key, turn_material
from shared_context.behavior_instruction_progress import ProgressRecord
from shared_context.behavior_operations import _parse, control_key
from shared_context.behavior_sdk_checkpoint import checkpoint_key
from shared_context.behavior_skill_copy import SopCopyOutcome
from shared_context.behavior_skill_draft import SkillDraftOutcome
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from test_conversation_transfer_startup import saved_turn, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_work_result_access import DIGEST, Reads

RESET = {"reset_request_id": "a-reset", "archived_at": "2026-10-31T00:00:00+00:00"}


@pytest.fixture
def turn(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    saved = saved_turn(c, "conversation-view")
    store = c.h.runtime.store
    cid = saved.assignment.task.instruction.conversation_id
    job = saved.binding.job_id
    return SimpleNamespace(
        c=c, cid=cid, binding=saved.binding, reads=Reads(store, monkeypatch), store=store, patch=monkeypatch,
        conversation=conversation_key(cid), archive=assistant_key("text-archive", job), job=worker_key("job", job),
        session_id=saved.assignment.session_id, job_id=job,
    )


def view(t):
    return t.c.control.conversation(t.c.human, t.cid)


def stored_archive(t):
    return _parse(t.c.h.runtime.store.read_snapshot((t.archive,))[t.archive].payload["data"], TurnArchive)


def archive_as(t, key=None, **updates):
    """Forge the archive with a matching material, so only the named field differs."""
    saved = stored_archive(t).model_copy(update=updates)
    t.reads.row(key or t.archive, SimpleNamespace(payload={
        "data": saved.model_dump(mode="json"), "material": turn_material(saved).model_dump(mode="json")}))


def blocked_by(t, code):
    projection = view(t)
    assert projection["follow_up_blocker"] == code and projection["can_follow_up"] is False
    return projection


def refused(t, status, code, message):
    with pytest.raises(Problem, match=message) as denied:
        view(t)
    assert denied.value.status == status and denied.value.body["error"]["code"] == code


def outcome(model, schema, stage):
    return model(schema_version=schema, receipt_id="a-receipt", intent_sha256=DIGEST, stage=stage)


def sop(stage):
    return outcome(SopCopyOutcome, "fa-sop-copy-outcome-v1", stage)


def draft(stage):
    return outcome(SkillDraftOutcome, "fa-skill-draft-outcome-v1", stage)


def test_a_saved_turn_keeps_its_exact_projection_shape(turn):
    projection = view(turn)
    assert list(projection) == ["conversation_id", "sdk_session_id", "can_follow_up", "follow_up_blocker", "turns",
                                "hosted_session_id", "hosted_agent_version", "archived", "can_reset"]
    assert projection["conversation_id"] == turn.cid and projection["hosted_agent_version"] == "41"
    assert projection["can_follow_up"] is True and projection["follow_up_blocker"] is None
    assert projection["archived"] is False and projection["can_reset"] is True
    only, = projection["turns"]
    assert list(only) == ["request_id", "binding_id", "job_id", "work_id", "turn_number", "state", "model_requests",
                          "reply_available", "model", "delivery_id", "progress", "prompt_assembly"]
    assert only["request_id"] == "conversation-view" and only["turn_number"] == 1 and only["state"] == "succeeded"
    assert only["binding_id"] == turn.binding.binding_id and only["job_id"] == turn.job_id
    assert only["work_id"] == turn.binding.work.work_id and only["reply_available"] is True
    assert only["model"] == "gpt-4.1-mini" and only["model_requests"] == 0 and only["delivery_id"] is None
    assert only["progress"]["events"] == []


def test_an_unknown_conversation_is_not_found(turn):
    turn.cid = "not-a-saved-conversation"
    refused(turn, 404, "CONVERSATION_NOT_FOUND", "original conversation is unavailable")


@pytest.mark.parametrize("field", ["human_oid", "team_id", "agent_oid"])
def test_a_conversation_outside_this_scope_is_refused(turn, field):
    turn.reads.fields(turn.conversation, **{field: "someone-else"})
    refused(turn, 403, "CONVERSATION_SCOPE_DENIED", "outside this human's scope")


def test_a_turn_without_its_job_row_is_still_admitting(turn):
    turn.reads.row(turn.job, None)
    projection = blocked_by(turn, "turn_unresolved")
    only, = projection["turns"]
    assert only["state"] == "admitting" and only["reply_available"] is False and only["model_requests"] == 0
    assert projection["can_reset"] is False


def test_the_turn_reads_its_session_obligation_and_reply(turn):
    sid = turn.session_id
    turn.reads.fields(assistant_key("session", sid), model_requests=3)
    turn.reads.fields(control_key("delivery-obligation", turn.binding.obligation_id), final_delivery_id="a-delivery")
    only, = view(turn)["turns"]
    assert only["model_requests"] == 3 and only["delivery_id"] == "a-delivery"


def test_a_missing_reply_is_not_available(turn):
    turn.reads.row(assistant_key("reply", turn.job_id), None)
    assert view(turn)["turns"][0]["reply_available"] is False


def test_an_unforwarded_job_has_no_available_reply(turn):
    turn.reads.fields(turn.job, result_forwarded=False)
    projection = blocked_by(turn, "turn_unresolved")
    assert projection["turns"][0]["reply_available"] is False and projection["can_reset"] is False


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
def test_every_terminal_state_can_reset(turn, state):
    turn.reads.fields(turn.job, state=state)
    assert view(turn)["can_reset"] is True


def test_a_running_job_cannot_reset(turn):
    turn.reads.fields(turn.job, state="running")
    assert blocked_by(turn, "turn_unresolved")["can_reset"] is False


def progress_row(t, **changes):
    t.reads.row(assistant_key("progress", t.session_id), SimpleNamespace(payload={"data": ProgressRecord.model_validate({
        "session_id": t.session_id, "job_id": t.job_id, "conversation_id": t.cid, "turn_number": 1,
        "progress": view_progress(t), **changes}).model_dump(mode="json")}))


def view_progress(t):
    return {"reasoning_summary": {"availability": "provided", "source": "openai-responses", "text": "Saved."}}


def test_saved_public_progress_is_shown(turn):
    progress_row(turn)
    assert view(turn)["turns"][0]["progress"]["reasoning_summary"]["text"] == "Saved."


@pytest.mark.parametrize("changes", [{"job_id": "another-job"}, {"conversation_id": "another-conversation"}, {"turn_number": 2}])
def test_progress_of_another_turn_is_refused(turn, changes):
    progress_row(turn, **changes)
    refused(turn, 409, "CONVERSATION_CONTEXT_CHANGED", "public progress belongs to another turn")


@pytest.mark.parametrize("changes", [
    {"job_id": "another-job"}, {"conversation_id": "another-conversation"}, {"turn_number": 2},
])
def test_an_archive_of_another_turn_is_refused_on_the_turn_read(turn, changes):
    archive_as(turn, **changes)
    refused(turn, 409, "CONVERSATION_CONTEXT_CHANGED", "saved instruction assembly differs")


def test_an_archive_with_another_prompt_is_refused(turn):
    archive_as(turn, prompt_assembly=None)
    refused(turn, 409, "CONVERSATION_CONTEXT_CHANGED", "saved instruction assembly differs")


def test_an_archive_with_other_material_is_refused(turn):
    turn.reads.other_material(turn.archive)
    refused(turn, 409, "CONVERSATION_CONTEXT_CHANGED", "saved instruction assembly differs")


@pytest.mark.parametrize("changes", [{"conversation_id": "another-conversation"}, {"job_id": "another-job"}])
def test_the_follow_up_archive_must_be_this_conversations_last_turn(turn, changes):
    # A second row, because the turn read above checks the row at this job's own archive key.
    other = "another-archive"
    turn.reads.fields(turn.conversation, archive_id=other)
    archive_as(turn, key=other, **changes)
    refused(turn, 409, "CONVERSATION_CONTEXT_CHANGED", "saved SDK context differs")


def restored(t):
    from shared_context import behavior_conversation_transfer as transfer
    t.patch.setattr(transfer, "transferred_failure", lambda control, conversation, job: True)
    t.patch.setattr(transfer, "_archive_matches", lambda current, archived: True)


def test_a_restored_failure_allows_the_archive_of_the_failed_turn(turn):
    restored(turn)
    key = checkpoint_key(stored_archive(turn).job_id)
    turn.reads.row(checkpoint_key("the-job-it-replaced"), turn.store.read_snapshot((key,))[key])
    turn.reads.fields(turn.conversation, archive_id="another-archive")
    archive_as(turn, key="another-archive", job_id="the-job-it-replaced")
    assert view(turn)["can_follow_up"] is True


def test_a_restored_failure_never_allows_another_conversations_archive(turn):
    restored(turn)
    turn.reads.fields(turn.conversation, archive_id="another-archive")
    archive_as(turn, key="another-archive", conversation_id="another-conversation")
    refused(turn, 409, "CONVERSATION_CONTEXT_CHANGED", "saved SDK context differs")


def test_an_archive_of_another_sdk_session_is_refused(turn):
    saved = stored_archive(turn)
    archive_as(turn, archive=saved.archive.model_copy(update={"sdk_session_id": "another-session"}))
    refused(turn, 409, "CONVERSATION_CONTEXT_CHANGED", "saved SDK session differs")


def test_a_stored_checkpoint_pointer_must_match_the_archive(turn):
    key = checkpoint_key(stored_archive(turn).job_id)
    turn.reads.data(key, lambda data: data["reference"].update(sha256=DIGEST))
    refused(turn, 409, "SDK_STATE_INCOMPLETE", "stored checkpoint pointer differs")


def test_a_missing_checkpoint_stays_a_missing_record(turn):
    turn.reads.row(checkpoint_key(stored_archive(turn).job_id), None)
    refused(turn, 409, "OPERATION_RECORD_MISSING", "a required trusted control record is missing")


def test_an_unavailable_archive_reports_its_own_error(turn):
    archive_as(turn, archive=ArchiveUnavailable(error="SDK_ARCHIVE_UNAVAILABLE"))
    blocked_by(turn, "SDK_ARCHIVE_UNAVAILABLE")


def test_a_checkpoint_format_this_profile_cannot_read_blocks_without_failing_the_read(turn):
    def unsupported(*args, **kwargs):
        raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "offline format refusal")
    from shared_context.behavior_assistant_wire import StoredSdkArchive
    turn.patch.setattr(StoredSdkArchive, "require_profile", unsupported)
    blocked_by(turn, "SDK_STATE_UNSUPPORTED_FORMAT")


def test_other_checkpoint_refusals_still_raise(turn):
    def invalid(*args, **kwargs):
        raise Problem(409, "SDK_STATE_INCOMPLETE", "offline other refusal")
    from shared_context.behavior_assistant_wire import StoredSdkArchive
    turn.patch.setattr(StoredSdkArchive, "require_profile", invalid)
    refused(turn, 409, "SDK_STATE_INCOMPLETE", "offline other refusal")


def test_settled_copy_outcomes_are_shown_and_do_not_block(turn):
    archive_as(turn, sop_copy_outcome=sop("saved"), skill_draft_outcome=draft("saved"))
    projection = view(turn)
    only, = projection["turns"]
    assert projection["can_follow_up"] is True
    assert list(only)[-3:] == ["prompt_assembly", "sop_copy_outcome", "skill_draft_outcome"]
    assert only["sop_copy_outcome"]["stage"] == "saved" and only["skill_draft_outcome"]["stage"] == "saved"


def test_an_unknown_sop_copy_blocks(turn):
    archive_as(turn, sop_copy_outcome=sop("create_unknown"))
    blocked_by(turn, "SOP_COPY_OUTCOME_UNKNOWN")


def test_an_unknown_skill_draft_blocks(turn):
    archive_as(turn, skill_draft_outcome=draft("create_unknown"))
    blocked_by(turn, "SKILL_DRAFT_OUTCOME_UNKNOWN")


def test_a_skill_draft_reason_replaces_the_sop_reason(turn):
    archive_as(turn, sop_copy_outcome=sop("create_unknown"), skill_draft_outcome=draft("create_unknown"))
    blocked_by(turn, "SKILL_DRAFT_OUTCOME_UNKNOWN")


def test_a_sop_reason_replaces_an_unavailable_archive(turn):
    archive_as(turn, archive=ArchiveUnavailable(error="SDK_ARCHIVE_UNAVAILABLE"), sop_copy_outcome=sop("create_unknown"))
    blocked_by(turn, "SOP_COPY_OUTCOME_UNKNOWN")


def test_an_active_turn_is_unresolved(turn):
    turn.reads.fields(turn.conversation, active_job_id="an-active-job")
    blocked_by(turn, "turn_unresolved")


def test_no_archive_without_a_no_sdk_record_is_unresolved(turn):
    turn.reads.fields(turn.conversation, archive_id=None)
    blocked_by(turn, "turn_unresolved")


def test_a_reset_conversation_is_archived(turn):
    turn.reads.fields(turn.conversation, **RESET)
    projection = blocked_by(turn, "CONVERSATION_ARCHIVED")
    assert projection["archived"] is True and projection["can_reset"] is False


def test_a_conversation_without_its_session_needs_a_reset(turn):
    turn.reads.fields(turn.conversation, hosted_session_id=None)
    assert blocked_by(turn, "CONVERSATION_SESSION_RESET_REQUIRED")["can_reset"] is True


def test_the_archived_reason_wins_over_a_missing_session(turn):
    turn.reads.fields(turn.conversation, **RESET, hosted_session_id=None)
    blocked_by(turn, "CONVERSATION_ARCHIVED")


def test_a_reset_conversation_never_reads_the_archive(turn):
    turn.reads.fields(turn.conversation, **RESET)
    turn.reads.row(turn.archive, None)
    blocked_by(turn, "CONVERSATION_ARCHIVED")


def failures(t, *, version=None, lost=None, transferred=False):
    from shared_context import behavior_conversation_transfer as transfer, behavior_foundry as foundry
    t.patch.setattr(foundry, "session_version_failure", lambda store, job: version)
    t.patch.setattr(foundry, "lost_session_failure", lambda store, job: lost)
    t.patch.setattr(transfer, "transferred_failure", lambda control, conversation, job: transferred)


def test_a_version_failure_blocks_with_its_code(turn):
    failures(turn, version="SESSION_VERSION_CHANGED")
    blocked_by(turn, "SESSION_VERSION_CHANGED")


def test_a_version_failure_waits_for_its_delivery(turn):
    failures(turn, version="SESSION_VERSION_CHANGED")
    turn.reads.fields(turn.job, result_forwarded=False)
    blocked_by(turn, "CONVERSATION_FAILURE_DELIVERY_PENDING")


def test_a_transferred_version_failure_no_longer_blocks(turn):
    failures(turn, version="SESSION_VERSION_CHANGED", transferred=True)
    assert view(turn)["can_follow_up"] is True


def test_a_transferred_failure_does_not_fall_through_to_the_lost_session_check(turn):
    failures(turn, version="SESSION_VERSION_CHANGED", lost="a-lost-session", transferred=True)
    assert view(turn)["can_follow_up"] is True


def test_a_lost_session_blocks_and_nothing_is_resent(turn):
    failures(turn, lost="a-lost-session")
    blocked_by(turn, "ASSISTANT_SESSION_LOST")


def test_a_lost_session_waits_for_its_delivery(turn):
    failures(turn, lost="a-lost-session")
    turn.reads.fields(turn.job, result_forwarded=False)
    blocked_by(turn, "CONVERSATION_FAILURE_DELIVERY_PENDING")


def test_a_session_failure_replaces_an_archive_reason(turn):
    failures(turn, lost="a-lost-session")
    archive_as(turn, sop_copy_outcome=sop("create_unknown"))
    blocked_by(turn, "ASSISTANT_SESSION_LOST")


def test_a_reset_conversation_ignores_session_failures(turn):
    failures(turn, version="SESSION_VERSION_CHANGED", lost="a-lost-session")
    turn.reads.fields(turn.conversation, **RESET)
    blocked_by(turn, "CONVERSATION_ARCHIVED")
