"""Human-selected history readers plus one unapproved user-procedure draft; local signed API and SDK-I/O doubles."""

import hashlib

import pytest

from conftest import A
from shared_context.behavior_assistant import AssistantProfile, key
from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES, HISTORY_PROFILE
from shared_context.behavior_history_skill_draft_prompt import (
    HISTORY_SKILL_DRAFT_CAPABILITIES, HISTORY_SKILL_DRAFT_PROMPT,
)
from shared_context.behavior_profile_selection import READ_ONLY_PROFILES
from shared_context.behavior_skill_draft import (
    HISTORY_SKILL_DRAFT_PROFILE, SKILL_DRAFT_PROFILE, SkillDraftReceipt, SkillDraftToolArgs,
)
from shared_context.behavior_skill_draft_prompt import SKILL_DRAFT_CAPABILITIES
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_read_history_profile import BODY as FILE_BODY, REPLY, history_turn
from test_sdk_compaction_product import open_turn, ordinary, settle
from test_skill_draft import BODY, OLD_PROMPTS, OTHER, PURPOSE, post_draft

FILES = "fa-user-text-files-v1"


def draft(turn, body=BODY):
    return turn.client.save_skill_draft(SkillDraftToolArgs(purpose=PURPOSE, body=body))


def test_new_prompt_is_separate_and_every_older_profile_keeps_its_bytes_and_tools():
    raw = HISTORY_SKILL_DRAFT_PROMPT.encode("utf-8")
    assert len(raw) == 4917
    assert hashlib.sha256(raw).hexdigest() == "173f7608565cdcf38824f2d7f33396ff11b14d01a8dcb6b3e152241549af5b3e"
    assembled = assemble_prompt(HISTORY_SKILL_DRAFT_PROFILE, capabilities=HISTORY_SKILL_DRAFT_CAPABILITIES)
    assert assembled.record.declared_capabilities == (*HISTORY_CAPABILITIES, "save_skill_draft")
    assert assembled.record.capability_catalog_version == "fa-instruction-tools-history-skill-draft-v1"
    for subset in (HISTORY_CAPABILITIES, SKILL_DRAFT_CAPABILITIES):
        with pytest.raises(ValueError):
            assemble_prompt(HISTORY_SKILL_DRAFT_PROFILE, capabilities=subset)

    for profile, size, sha, capabilities in (*OLD_PROMPTS, (
            SKILL_DRAFT_PROFILE, 2638, "f47c84ffb238af98c9082d7e060f622052f70bc2bb796e8c845964c7fd508ad2",
            SKILL_DRAFT_CAPABILITIES)):
        old = assemble_prompt(profile)
        data = old.system_message.encode("utf-8")
        assert (len(data), hashlib.sha256(data).hexdigest()) == (size, sha)
        assert (() if old.record is None else old.record.declared_capabilities) == capabilities
        if profile is not None:
            with pytest.raises(ValueError):
                assemble_prompt(profile, capabilities=HISTORY_SKILL_DRAFT_CAPABILITIES)
    assert HISTORY_SKILL_DRAFT_PROFILE not in READ_ONLY_PROFILES and SKILL_DRAFT_PROFILE not in READ_ONLY_PROFILES


def test_selected_new_conversation_reads_history_and_saves_one_unapproved_draft(assistant, monkeypatch):
    h, control, human, first = history_turn(
        assistant, monkeypatch, profile=HISTORY_SKILL_DRAFT_PROFILE, tools=HISTORY_SKILL_DRAFT_CAPABILITIES)
    record = control.records[first["binding_id"]]
    assert record.profile.prompt_profile == HISTORY_SKILL_DRAFT_PROFILE and record.profile_selection_id is not None
    assert control.installed().profile.prompt_profile is None
    turn = open_turn(h, control, human, "history-draft-follow-up", first["conversation_id"])
    try:
        assert turn.client.read_user_file(1, "input.txt").text == FILE_BODY
        assert turn.client.read_assistant_reply(1).reply.text == REPLY
        receipt = draft(turn)
        assert type(receipt) is SkillDraftReceipt and receipt.approved is False and receipt.effective_version_id is None
        assert receipt.visibility == "team" and receipt.source_turn_number == 2
        assert receipt.source_user_text_sha256 == hashlib.sha256(
            turn.assignment.task.instruction.content.encode("utf-8")).hexdigest()
        version = h.artifacts.version(A, A.team, receipt.artifact_id, receipt.version_id)
        assert version.approval is None and h.artifacts.get(A, A.team, receipt.artifact_id).payload["effective_version_id"] is None
        assert h.assistant.skills.catalog(A, A.team, (receipt.artifact_id,)).entries == ()
        reviews = h.artifacts.skill_reviews(A, A.team).model_dump(mode="json")
        assert next(item for item in reviews["entries"] if item["artifact_id"] == receipt.artifact_id)["status"] == "awaiting_decision"

        before = h.store._guard()
        assert draft(turn) == receipt and h.store._guard() == before
        conflict = post_draft(turn, body=OTHER)
        assert conflict.status_code == 409 and h.store._guard() == before
        settle(turn, ordinary(turn.assignment.sdk_session_id))
        archived = h.store.read_snapshot((key("text-archive", turn.binding.job_id),))[key("text-archive", turn.binding.job_id)]
        assert archived.payload["data"]["skill_draft_receipt"] == receipt.model_dump(mode="json")
    finally:
        turn.client.close()


def test_selected_draft_upload_unknown_reconciles_only_the_original_ids(assistant, monkeypatch):
    h, control, human, first = history_turn(
        assistant, monkeypatch, profile=HISTORY_SKILL_DRAFT_PROFILE, tools=HISTORY_SKILL_DRAFT_CAPABILITIES)
    turn = open_turn(h, control, human, "history-draft-unknown", first["conversation_id"])
    native_create, native_upload = h.artifacts.create, h.artifacts.upload
    counts = {"create": 0, "upload": 0}

    def create(*args, **kwargs):
        counts["create"] += 1
        return native_create(*args, **kwargs)

    def upload(*args, **kwargs):
        counts["upload"] += 1
        result = native_upload(*args, **kwargs)
        if counts["upload"] == 1:
            raise TimeoutError("SYNTHETIC_UPLOAD_ACK")
        return result

    monkeypatch.setattr(h.artifacts, "create", create)
    monkeypatch.setattr(h.artifacts, "upload", upload)
    try:
        assert post_draft(turn).status_code == 503
        again = post_draft(turn)
        assert again.status_code == 200, again.text
        assert counts == {"create": 1, "upload": 1}
    finally:
        turn.client.close()


def test_history_v1_and_conversations_before_the_selection_cannot_save_drafts(assistant, monkeypatch):
    h, control, human, first = history_turn(assistant, monkeypatch)
    selected, _ = select(h, request_id="choose-history-draft", previous=control.records[first["binding_id"]].profile_selection_id,
                         value=template(HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile=FILES))
    assert selected.status_code == 200, selected.text
    turn = open_turn(h, control, human, "history-before-draft-selection", first["conversation_id"])
    try:
        assert turn.assignment.task.instruction.prompt_profile == HISTORY_PROFILE
        assert turn.client.read_user_turn(1).text
        before = h.store._guard()
        response = post_draft(turn)
        assert response.status_code == 403, response.text
        with pytest.raises(Problem) as denied:
            draft(turn)
        assert denied.value.body["error"]["code"] == "SKILL_DRAFT_DISABLED"
        assert h.store._guard() == before
    finally:
        turn.client.close()


def test_policy_profile_without_a_human_selection_cannot_write(assistant):
    from types import MappingProxyType, SimpleNamespace
    from shared_context.behavior_work_policy_api import install_work_policy_routes
    from test_work_policy_lifecycle import enabled
    h = assistant
    h.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({
            **profile.model_dump(), "prompt_profile": HISTORY_SKILL_DRAFT_PROFILE, "user_text_file_profile": FILES,
        }) for name, profile in h.runtime.profiles.items()
    })
    h, control, human, _ = enabled(h)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    turn = open_turn(h, control, human, "history-draft-policy-only")
    try:
        assert control.installed().profile.prompt_profile == HISTORY_SKILL_DRAFT_PROFILE
        assert control.records[turn.binding.binding_id].profile_selection_id is None
        before = h.store._guard()
        response = post_draft(turn)
        assert response.status_code == 403, response.text
        with pytest.raises(Problem) as denied:
            draft(turn)
        assert denied.value.body["error"]["code"] == "SKILL_DRAFT_SCOPE_DENIED"
        assert h.store._guard() == before
    finally:
        turn.client.close()