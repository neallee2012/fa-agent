"""Readable Unicode spaces preserve literal policy bytes; hidden controls stay rejected."""

import hashlib

import pytest

from conftest import A
from shared_context.errors import Problem
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import ApprovalInput, CreateArtifact, SkillPurpose, VersionInput
from shared_context.m3_skills import ApprovedSkillSource, SkillEntry, SkillReadResult
from test_m3_artifacts import V1, m3


@pytest.mark.parametrize("kind", ["skill", "sop"])
@pytest.mark.parametrize("space", ["\u3000", "\u00a0"])
def test_unicode_spaces_survive_upload_approval_and_current_policy_read(m3, kind, space):
    artifacts, _, _ = m3
    body = ("中文" + space + "指引\n保留原文。\n").encode("utf-8")
    created = artifacts.create(A, A.team, "unicode-policy", CreateArtifact(
        kind=kind, title="Unicode policy", source_work="source-work",
    ))
    uploaded = artifacts.upload(A, A.team, "unicode-policy", V1, created.etag, VersionInput(
        filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(body).hexdigest(),
    ), body)
    artifacts.decide(HumanApprover(artifacts.approval.approver_oid, A.team), A.team, "unicode-policy",
        uploaded.etag, ApprovalInput(decision_id=V1, version_id=V1, sha256=hashlib.sha256(body).hexdigest(),
                                    decision="approve"))
    assert artifacts.policy_context(A, A.team, ("unicode-policy",))[0]["text"].encode() == body
    if kind == "skill":
        source = ApprovedSkillSource(artifacts)
        entry = source.catalog(A, A.team, ("unicode-policy",)).entries[0]
        assert source.read(A, A.team, ("unicode-policy",), entry).text.encode() == body


@pytest.mark.parametrize("space", ["\u3000", "\u00a0"])
def test_literal_purpose_and_title_accept_unicode_space_without_normalizing(space):
    value = "核對" + space + "原回條"
    purpose = SkillPurpose(schema_version="fa-skill-purpose-v1", description=value)
    entry = SkillEntry(name="unicode-guide", title=value, version_id=V1, sha256="a" * 64,
                       byte_length=1, team="team-one", source_work="source-work", source_work_revision=1)
    assert purpose.description == entry.title == value
    assert SkillPurpose.from_header(purpose.header()).description.encode() == value.encode()


@pytest.mark.parametrize("control", ["\x00", "\x1b", "\x7f", "\u200b", "\u202e", "\ufeff"])
def test_hidden_controls_remain_rejected_for_policy_body_and_metadata(m3, control):
    artifacts, _, _ = m3
    value = "Guidance" + control + "text"
    with pytest.raises(ValueError):
        SkillPurpose(schema_version="fa-skill-purpose-v1", description=value)
    fields = {"name": "guide", "title": "Visible title", "version_id": V1,
              "sha256": hashlib.sha256(value.encode()).hexdigest(), "byte_length": len(value.encode()),
              "team": A.team, "source_work": "source-work", "source_work_revision": 1}
    with pytest.raises(ValueError):
        SkillEntry(**{**fields, "title": value})
    with pytest.raises(ValueError):
        SkillReadResult(skill=SkillEntry(**fields), text=value)
    created = artifacts.create(A, A.team, "hidden-policy", CreateArtifact(
        kind="skill", title="Visible title", source_work="source-work",
    ))
    body = value.encode("utf-8")
    with pytest.raises(Problem) as denied:
        artifacts.upload(A, A.team, "hidden-policy", V1, created.etag, VersionInput(
            filename="guide.md", media_type="text/plain", sha256=hashlib.sha256(body).hexdigest(),
        ), body)
    assert denied.value.body["error"]["code"] == "M3_POLICY_FORMAT"
    assert artifacts.get(A, A.team, "hidden-policy").payload["pending"] is None
