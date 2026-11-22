"""Deleting a Team skill (Neal 2026-11-22): it leaves every list and choice for good; nothing stored is rewritten."""

import copy

import pytest

from types import SimpleNamespace

from conftest import A, TENANT
from shared_context.errors import Problem
from shared_context.m3_skills import ApprovedSkillSource
from shared_context.m3_models import ArtifactDocument, NativeArtifactSource, SkillDeactivationInput, SkillDeletionInput
from test_m3_artifacts import HUMAN, V1, V2, m3
from test_m3_skill_reviews import add_version, decide, review_fixture
from test_skill_deactivation import (
    BODY, NAME, approved_skill, deactivate, deactivation_body, skill_version,
)

D1, D2 = "d" * 32, "e" * 32


def deletion(decision_id=D1):
    return SkillDeletionInput(schema_version="fa-skill-deletion-v1", decision_id=decision_id)


def delete(service, name, decision_id=D1, etag=None):
    etag = etag or service.get(HUMAN, A.team, name).etag
    return service.delete_skill(HUMAN, A.team, name, etag, deletion(decision_id))


def listed(service):
    names, after = [], None
    while True:
        page = service.skill_reviews(HUMAN, A.team, after)
        names.extend(entry.artifact_id for entry in page.entries)
        if page.next_after is None:
            return names
        after = page.next_after


def code(error):
    return error.value.status, error.value.body["error"]["code"]


def test_skills_in_any_state_leave_every_review_page_while_their_records_stay(m3, monkeypatch):
    service, repository, store = m3
    review_fixture(service, store)
    # "rejected-update" shows as rejected but its earlier version is still effective: the page deactivates it first.
    for name, decision_id in (("effective", "c" * 32), ("rejected-update", "b" * 32)):
        current = service.get(HUMAN, A.team, name)
        version = current.payload["versions"][0]
        service.deactivate_skill(HUMAN, A.team, name, current.etag, SkillDeactivationInput.model_validate(
            deactivation_body(version, decision_id=decision_id)))
    gone = ("awaiting", "deactivated", "effective", "empty", "pending", "rejected-update")
    before = {name: copy.deepcopy(service.get(HUMAN, A.team, name).payload) for name in gone}
    for index, name in enumerate(gone):
        delete(service, name, f"{index:032x}")
    # One entry per page: deleted rows, like deliverables, only advance the cursor, so nothing is skipped.
    monkeypatch.setattr("shared_context.m3_artifacts.MAX_SKILL_REVIEW_ENTRIES", 1)
    assert listed(service) == ["redraft"]
    for name in gone:
        stored = service.get(HUMAN, A.team, name).payload
        assert stored["deletion"]["deleted_by_oid"] == HUMAN.oid
        assert {key: stored.get(key) for key in ("versions", "pending", "deactivations")} == {
            key: before[name].get(key) for key in ("versions", "pending", "deactivations")}


def test_an_effective_skill_is_deactivated_first_and_then_no_agent_reads_it(approved_skill):
    service, _, approved = approved_skill
    skills = ApprovedSkillSource(service)

    def catalog():
        # The same reader that exports an Agent's template and captures a new conversation's skills.
        return skills.catalog(A, A.team, (NAME,), schema_version="fa-approved-skill-catalog-v2").entries

    assert [entry.name for entry in catalog()] == [NAME]
    with pytest.raises(Problem) as effective:
        delete(service, NAME)
    assert code(effective) == (409, "M3_SKILL_EFFECTIVE")
    version = approved.payload["versions"][0]
    inactive = deactivate(service, approved.etag, deactivation_body(version))
    deleted = delete(service, NAME, D2, inactive.etag)
    assert deleted.payload["effective_version_id"] is None and catalog() == ()
    assert service.policy_context(A, A.team, (NAME,)) == [{"artifact_id": NAME, "status": "deactivated"}]
    with pytest.raises(Problem) as missing:
        service.effective(A, A.team, NAME)
    assert missing.value.status == 404
    # Unlike a deactivation, no later version can bring a deleted skill back.
    later = BODY + b"later\n"
    with pytest.raises(Problem) as upload:
        service.upload(A, A.team, NAME, V2, deleted.etag, skill_version(later, None), later)
    assert code(upload) == (409, "M3_SKILL_DELETED")
    assert service.get(HUMAN, A.team, NAME) == deleted


def test_the_same_deletion_id_is_recorded_once_even_after_a_lost_reply(m3, monkeypatch):
    service, _, _ = m3
    from test_m3_skill_reviews import artifact
    artifact(service, "draft")
    add_version(service, "draft")
    before = service.get(HUMAN, A.team, "draft")
    original, writes = service.repository.replace, []

    def lost_ack(*args):
        writes.append(original(*args))
        raise Problem(502, "M3_STATE_UNCERTAIN", "synthetic acknowledgement loss")

    monkeypatch.setattr(service.repository, "replace", lost_ack)
    with pytest.raises(Problem):
        delete(service, "draft", etag=before.etag)
    monkeypatch.setattr(service.repository, "replace", original)
    saved = service.get(HUMAN, A.team, "draft")
    # The page resends the same ID with the ETag it read before; the original record comes back unchanged.
    assert delete(service, "draft", etag=before.etag) == saved and len(writes) == 1
    assert saved.payload["revision"] == before.payload["revision"] + 1
    with pytest.raises(Problem) as another:
        delete(service, "draft", D2, saved.etag)
    assert code(another) == (409, "M3_SKILL_DELETED")
    with pytest.raises(Problem) as reused:
        decide(service, "draft", V1, "approve")
    assert code(reused) == (409, "M3_SKILL_DELETED")
    assert service.get(HUMAN, A.team, "draft") == saved


def test_a_deletion_id_cannot_reuse_another_decision(approved_skill):
    service, _, approved = approved_skill
    inactive = deactivate(service, approved.etag, deactivation_body(approved.payload["versions"][0]))
    for used in (V1, D1):
        with pytest.raises(Problem) as reused:
            delete(service, NAME, used, inactive.etag)
        assert code(reused) == (409, "M3_DECISION_CONFLICT")
    assert service.get(HUMAN, A.team, NAME) == inactive


def test_only_the_configured_human_deletes(m3):
    service, _, _ = m3
    from test_m3_skill_reviews import artifact
    created = artifact(service, "draft")
    with pytest.raises(Problem) as denied:
        service.delete_skill(A, A.team, "draft", created.etag, deletion())
    assert code(denied) == (403, "M3_HUMAN_REQUIRED")


def test_a_saved_deletion_cannot_hide_an_effective_version_or_be_absent_from_legacy_bytes(approved_skill):
    service, _, approved = approved_skill
    data = approved.payload
    assert "deletion" not in ArtifactDocument.model_validate(data).model_dump()
    record = {"schema_version": "fa-skill-deletion-v1", "decision_id": D1, "deleted_by_oid": HUMAN.oid,
              "deleted_at": "t", "applied_revision": data["revision"]}
    with pytest.raises(ValueError):
        ArtifactDocument.model_validate({**data, "deletion": record})


@pytest.mark.parametrize("saved", [True, False], ids=["saved-draft", "draft-without-version"])
def test_an_agent_resending_its_original_rid_never_rebuilds_a_deleted_draft(m3, saved):
    from shared_context.behavior_skill_draft import SkillDraftToolArgs
    from shared_context.native_skill_draft_api import TITLE, NativeSkillDraftGateway
    from shared_context.native_skill_draft_wire import NativeSkillDraftWrite, artifact_id

    service, _, _ = m3
    source = NativeArtifactSource(tenant_id=TENANT, team_id=A.team, agent_id="agent-one", agent_oid=A.oid,
                                  conversation_id="conversation-one", request_id="request-one", submit_sha256="a" * 64)
    proof = SimpleNamespace(effect_status="pending")
    gateway = object.__new__(NativeSkillDraftGateway)
    # Only the verified turn lookup is replaced; the draft slot, upload and receipt run as in production.
    gateway._write_source = lambda principal, command: (service, A, source, proof)
    command = NativeSkillDraftWrite(conversation_id="conversation-one", request_id="request-one",
                                    arguments=SkillDraftToolArgs(purpose="Follow the procedure.", body="Do the steps.\n"))
    identifier = artifact_id(TENANT, A.team, "agent-one", "conversation-one", "request-one")
    if saved:
        receipt = gateway.save(A, command)
    else:
        service.create_native_skill(A, A.team, identifier, title=TITLE, native_source=source)
    delete(service, identifier)
    stored = service.get(HUMAN, A.team, identifier)
    for status in ("pending", "done") if saved else ("pending",):
        proof.effect_status = status
        if saved:
            assert gateway.save(A, command) == receipt
        else:
            with pytest.raises(Problem) as unknown:
                gateway.save(A, command)
            assert code(unknown) == (503, "SKILL_DRAFT_OUTCOME_UNKNOWN")
    assert listed(service) == [] and service.get(HUMAN, A.team, identifier) == stored
