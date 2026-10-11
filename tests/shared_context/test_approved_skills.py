"""Controlled approved-skill source access, not native skill loading."""

import copy
import hashlib

import pytest

from conftest import A, B, C
from shared_context.errors import Problem
from shared_context.m3_skills import ApprovedSkillSource, SkillReadResult
from test_m3_artifacts import HUMAN, V2, create, decision, m3, upload


def test_approved_catalog_discloses_metadata_then_reads_exact_body_without_writes(m3):
    artifacts, repository, blobs = m3
    create(artifacts, "skill")
    upload(artifacts, data=b"SKILL_BODY_CANARY: verify the receipt before reporting success.\n")
    decision(artifacts)
    source = ApprovedSkillSource(artifacts)
    before = copy.deepcopy(repository.data)

    catalog = source.catalog(A, A.team, ("report",))
    assert [entry.name for entry in catalog.entries] == ["report"]
    entry = catalog.entries[0]
    assert entry.title == "Synthetic report"
    assert entry.version_id == "1" * 32
    assert entry.team == A.team and entry.source_work == "source-work"
    assert entry.kind == "skill" and catalog.tokens is None
    assert "SKILL_BODY_CANARY" not in catalog.model_dump_json()

    result = source.read(A, A.team, ("report",), entry)
    assert result.skill == entry
    assert result.text == "SKILL_BODY_CANARY: verify the receipt before reporting success.\n"
    assert repository.data == before


@pytest.mark.parametrize("kind,choice", [("skill", None), ("skill", "reject"), ("sop", "approve"), ("deliverable", None)])
def test_catalog_omits_unapproved_versions_and_non_skill_artifacts(m3, kind, choice):
    artifacts, _, _ = m3
    create(artifacts, kind)
    upload(artifacts)
    if choice:
        decision(artifacts, choice=choice)
    catalog = ApprovedSkillSource(artifacts).catalog(A, A.team, ("report", "absent"))
    assert catalog.entries == () and catalog.unavailable_count == 2
    assert "report" not in catalog.model_dump_json() and "absent" not in catalog.model_dump_json()


@pytest.mark.parametrize("principal,team", [(C, A.team), (A, C.team), (HUMAN, A.team)])
def test_source_never_manufactures_an_actor_or_cross_team_right(m3, principal, team):
    source = ApprovedSkillSource(m3[0])
    with pytest.raises(Problem) as denied:
        source.catalog(principal, team, ())
    assert denied.value.status == 403


def test_new_pending_or_rejected_version_does_not_replace_prior_human_approved_body(m3):
    artifacts, _, _ = m3
    create(artifacts, "skill")
    upload(artifacts, data=b"approved v1")
    decision(artifacts)
    source = ApprovedSkillSource(artifacts)
    selected = source.catalog(B, A.team, ("report",)).entries[0]
    upload(artifacts, V2, b"UNAPPROVED_CANARY")
    assert source.read(B, A.team, ("report",), selected).text == "approved v1"
    decision(artifacts, V2, "reject")
    assert source.read(B, A.team, ("report",), selected).text == "approved v1"
    assert "UNAPPROVED_CANARY" not in source.catalog(B, A.team, ("report",)).model_dump_json()


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("version_id", "2" * 32),
    ("source_work", "different-source"), ("source_work_revision", 2), ("team", "team-two"), ("name", "different-name")])
def test_forged_receipt_or_removed_selector_cannot_choose_another_read(m3, field, value):
    artifacts, _, _ = m3
    create(artifacts, "skill")
    upload(artifacts)
    decision(artifacts)
    source = ApprovedSkillSource(artifacts)
    entry = source.catalog(A, A.team, ("report",)).entries[0]
    with pytest.raises(Problem) as denied:
        source.read(A, A.team, ("report",), entry.model_copy(update={field: value}))
    assert denied.value.body["error"]["code"] == "SKILL_SOURCE_CHANGED"
    with pytest.raises(Problem):
        source.read(A, A.team, (), entry)


def test_client_body_validator_does_not_accept_replaced_text_under_an_old_hash(m3):
    artifacts, _, _ = m3
    create(artifacts, "skill")
    upload(artifacts, data=b"approved")
    decision(artifacts)
    entry = ApprovedSkillSource(artifacts).catalog(A, A.team, ("report",)).entries[0]
    assert entry.sha256 == hashlib.sha256(b"approved").hexdigest()
    with pytest.raises(ValueError):
        SkillReadResult(skill=entry, text="replaced")
