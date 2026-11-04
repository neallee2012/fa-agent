"""Version-bound skill purpose through ArtifactService and the approved-skill readers, called in process."""

import base64
import copy
import hashlib
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from conftest import A, B, C
from shared_context.errors import Problem
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import ApprovalInput, CreateArtifact, SkillDeactivationInput, SkillPurpose, VersionInput
from shared_context.m3_skills import ApprovedSkillSource
from test_m3_artifacts import V1, V2, m3

PURPOSE = "  核對已保存回條，再說明哪些結果仍待確認。  "
BODY = b"PURPOSE_BODY_CANARY: verify the immutable receipt before reporting success.\r\n"
PROFILE = "fa-instruction-skill-purpose-v1"


def human(artifacts):
    """The configured human approver of this ArtifactService."""
    return HumanApprover(artifacts.approval.approver_oid, artifacts.approval.team)


def purpose_metadata(description=PURPOSE, body=BODY, *, resources=None, tools=None):
    return VersionInput(
        filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(body).hexdigest(),
        skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description=description),
        skill_resources=resources, skill_tools=tools,
    )


def create_skill(artifacts, name="report", *, kind="skill", title="Not the approved purpose",
                 source_work="source-work", principal=A):
    return artifacts.create(principal, A.team, name, CreateArtifact(kind=kind, title=title, source_work=source_work))


def upload_purpose(artifacts, etag, *, version=V1, description=PURPOSE, body=BODY, name="report",
                   resources=None, tools=None, principal=A):
    return artifacts.upload(principal, A.team, name, version, etag,
                            purpose_metadata(description, body, resources=resources, tools=tools), body)


def decide(artifacts, uploaded, version, *, name="report", principal=None, **overrides):
    body = {"decision_id": version["version_id"], "version_id": version["version_id"], "decision": "approve",
            "sha256": version["sha256"], "metadata_sha256": version.get("metadata_sha256"), **overrides}
    return artifacts.decide(principal or human(artifacts), A.team, name, uploaded.etag, ApprovalInput(**body))


def deactivate(artifacts, name, version, decision_id="d" * 32):
    before = artifacts.get(human(artifacts), A.team, name)
    return artifacts.deactivate_skill(human(artifacts), A.team, name, before.etag, SkillDeactivationInput(
        schema_version="fa-skill-deactivation-v1", decision_id=decision_id, version_id=version["version_id"],
        sha256=version["sha256"], metadata_sha256=version["metadata_sha256"],
    ))


def current(artifacts, name="report"):
    return artifacts.get(human(artifacts), A.team, name).response()


def code(error):
    return error.value.status, error.value.body["error"]["code"]


def catalog(artifacts):
    return ApprovedSkillSource(artifacts).catalog(A, A.team, ("report",), schema_version="fa-approved-skill-catalog-v2")


def test_version_and_human_approval_pin_purpose_before_catalog_or_body_read(m3):
    artifacts, _, _ = m3
    uploaded = upload_purpose(artifacts, create_skill(artifacts).etag)
    version = artifacts.version(human(artifacts), A.team, "report", V1).model_dump()
    assert version.get("skill_purpose") == {
        "schema_version": "fa-skill-purpose-v1", "description": PURPOSE,
    }, "the immutable skill version did not retain the author's exact purpose"
    source = ApprovedSkillSource(artifacts)
    assert catalog(artifacts).entries == ()
    decide(artifacts, uploaded, version)
    listed = catalog(artifacts)
    entry = listed.entries[0]
    assert entry.description == PURPOSE and entry.metadata_sha256 == version["metadata_sha256"]
    assert "PURPOSE_BODY_CANARY" not in listed.model_dump_json()
    result = source.read(A, A.team, ("report",), entry)
    assert result.skill == entry and result.text.encode("utf-8") == BODY
    assert result.skill.sha256 == hashlib.sha256(BODY).hexdigest()


def draft(artifacts, *, description=PURPOSE, kind="skill"):
    uploaded = upload_purpose(artifacts, create_skill(artifacts, kind=kind, title="Title is not approved purpose").etag,
                              description=description)
    return uploaded, uploaded.payload["versions"][0]


@pytest.mark.parametrize("description", [
    "", "   ", "x" * 513, "界" * 171, "line\nbreak", "carriage\rreturn", "tab\t",
    "\x00hidden", "\x7fhidden", "‮hidden", "​hidden",
])
def test_invalid_purpose_rejected_before_reservation_or_blob(m3, description):
    artifacts, _, blobs = m3
    initial = create_skill(artifacts, title="Literal purpose")
    with patch.object(blobs, "put", wraps=blobs.put) as put, pytest.raises(ValidationError):
        upload_purpose(artifacts, initial.etag, description=description)
    put.assert_not_called()
    assert current(artifacts) == initial.response()


@pytest.mark.parametrize("description", ["x" * 512, "界" * 170 + "  ", '"' * 512, "\\" * 512],
                         ids=["ascii", "unicode", "quotes", "backslashes"])
def test_exact_512_utf8_byte_purpose_is_not_trimmed(m3, description):
    artifacts = m3[0]
    uploaded, version = draft(artifacts, description=description)
    assert version["skill_purpose"]["description"] == description
    assert len(description.encode("utf-8")) == 512
    decide(artifacts, uploaded, version)
    assert catalog(artifacts).entries[0].description == description


@pytest.mark.parametrize("raw", [
    b'{"schema_version":"fa-skill-purpose-v99","description":"purpose"}',
    b'{"description":"no version"}',
    b'{"schema_version":"fa-skill-purpose-v1","description":"purpose","priority":"system"}',
    b'{"schema_version":"fa-skill-purpose-v1","description":"purpose","approved":true}',
    b'{"schema_version":"fa-skill-purpose-v1","description":"\\ud800"}',
    b'{"schema_version":"fa-skill-purpose-v1","description":true}',
    b'{"schema_version":"fa-skill-purpose-v99","schema_version":"fa-skill-purpose-v1","description":"purpose"}',
    b'null',
])
def test_unknown_or_malformed_purpose_schema_has_no_fallback(raw):
    with pytest.raises(ValueError):
        SkillPurpose.from_header(base64.urlsafe_b64encode(raw).decode("ascii"))


@pytest.mark.parametrize("kind", ["sop", "deliverable"])
def test_purpose_metadata_cannot_turn_another_artifact_kind_into_a_skill(m3, kind):
    artifacts = m3[0]
    initial = create_skill(artifacts, kind=kind, title="Not a skill")
    with pytest.raises(Problem) as refused:
        upload_purpose(artifacts, initial.etag)
    assert code(refused) == (422, "M3_POLICY_FORMAT")
    assert current(artifacts) == initial.response()


@pytest.mark.parametrize("metadata_digest", [None, "0" * 64])
def test_body_hash_only_or_different_metadata_cannot_approve_purpose(m3, metadata_digest):
    artifacts = m3[0]
    uploaded, version = draft(artifacts)
    with pytest.raises(Problem) as refused:
        decide(artifacts, uploaded, version, metadata_sha256=metadata_digest)
    assert code(refused) == (409, "M3_APPROVAL_METADATA_MISMATCH")
    assert current(artifacts) == uploaded.response()
    decide(artifacts, uploaded, version)


def test_purpose_only_revision_requires_new_version_and_new_exact_approval(m3):
    artifacts = m3[0]
    uploaded, first = draft(artifacts)
    approved = decide(artifacts, uploaded, first)
    original = artifacts.version(human(artifacts), A.team, "report", V1)
    changed = "  Inspect the original receipt when comparing revisions.  "
    with pytest.raises(Problem) as reused:
        upload_purpose(artifacts, approved.etag, description=changed)
    assert code(reused) == (409, "M3_VERSION_CONFLICT")
    assert current(artifacts) == approved.response()
    second_upload = upload_purpose(artifacts, approved.etag, version=V2, description=changed)
    second = second_upload.payload["versions"][-1]
    assert first["sha256"] == second["sha256"] == hashlib.sha256(BODY).hexdigest()
    assert first["metadata_sha256"] != second["metadata_sha256"]
    entry = lambda: catalog(artifacts).entries[0]
    assert entry().description == PURPOSE
    for overrides in ({"metadata_sha256": first["metadata_sha256"]}, {"decision_id": V1}):
        with pytest.raises(Problem) as refused:
            decide(artifacts, second_upload, second, **overrides)
        assert refused.value.status == 409
    decide(artifacts, second_upload, first)
    assert entry().description == PURPOSE
    decide(artifacts, second_upload, second)
    assert entry().description == changed and entry().version_id == V2
    assert artifacts.version(human(artifacts), A.team, "report", V1) == original


@pytest.mark.parametrize("field", ["purpose", "source_work", "source_work_revision", "filename", "blob_etag", "approval"])
def test_changed_metadata_cannot_reuse_a_persisted_approval_or_receipt(m3, field):
    artifacts, repository, _ = m3
    uploaded, version = draft(artifacts)
    decide(artifacts, uploaded, version)
    record = artifacts.get(A, A.team, "report")
    altered = copy.deepcopy(record.payload)
    selected = altered["versions"][0]
    if field == "purpose":
        selected["skill_purpose"]["description"] = "Unapproved different purpose."
    elif field == "source_work":
        selected["source_work"] = "different-source"
    elif field == "source_work_revision":
        selected["source_work_revision"] += 1
    elif field == "filename":
        selected["filename"] = "other.md"
    elif field == "blob_etag":
        selected["blob"]["etag"] = '"other-receipt"'
    else:
        selected["approval"].pop("metadata_sha256")
    repository.replace("m3-" + artifacts.context.auth.tenant_id + "-" + A.team, "artifact:report", altered, record.etag)
    with pytest.raises(Problem) as denied:
        artifacts.get(human(artifacts), A.team, "report")
    assert code(denied) == (502, "M3_STATE_INVALID")
    with pytest.raises(Problem, match="could not be verified"):
        catalog(artifacts)
    assert PURPOSE not in str(denied.value.body) and "PURPOSE_BODY_CANARY" not in str(denied.value.body)


def test_legacy_version_has_unknown_purpose_and_original_v1_catalog_remains_unchanged(m3):
    artifacts = m3[0]
    initial = create_skill(artifacts, title="Cannot become purpose")
    data = b"Purpose: cannot infer this body as approved metadata."
    uploaded = artifacts.upload(A, A.team, "report", V1, initial.etag, VersionInput(
        filename="report.txt", media_type="text/plain", sha256=hashlib.sha256(data).hexdigest()), data)
    version = uploaded.payload["versions"][0]
    assert "skill_purpose" not in version and "metadata_sha256" not in version
    decide(artifacts, uploaded, version)
    source = ApprovedSkillSource(artifacts)
    v1 = source.catalog(A, A.team, ("report",)).model_dump_json()
    v2 = catalog(artifacts)
    assert v2.entries[0].description is None and v2.entries[0].metadata_sha256 is None
    assert "Purpose: cannot" not in v2.model_dump_json()
    assert source.catalog(A, A.team, ("report",)).model_dump_json() == v1
    assert '"description":null' in v1 and "metadata_sha256" not in v1


def test_only_the_configured_human_decides_and_only_the_author_revises(m3):
    """Token expiry and missing approval scope stop a principal earlier; test_m3_auth pins those."""
    artifacts = m3[0]
    uploaded, version = draft(artifacts)
    with pytest.raises(Problem) as agent:
        decide(artifacts, uploaded, version, principal=A)
    assert agent.value.status == 403
    for actor in (B, C):
        with pytest.raises(Problem) as peer:
            upload_purpose(artifacts, uploaded.etag, version=V2, principal=actor)
        assert peer.value.status == 403
    with pytest.raises(Problem) as other_team:
        artifacts.get(A, "team-two", "report")
    assert other_team.value.status == 403 and PURPOSE not in str(other_team.value.body)
    assert current(artifacts) == uploaded.response()


def test_exact_unicode_purpose_survives_a_lost_upload_and_reconciles_once(m3):
    artifacts, _, blobs = m3
    created = create_skill(artifacts, title="Review purpose")
    original_put = blobs.put

    def lost(*args):
        original_put(*args)
        raise Problem(502, "SYNTHETIC_LOST_ACK", "offline accepted write with lost acknowledgement")

    with patch.object(blobs, "put", side_effect=lost) as writes:
        with pytest.raises(Problem):
            upload_purpose(artifacts, created.etag)
        pending = artifacts.get(A, A.team, "report")
        assert pending.payload["pending"]["skill_purpose"]["description"] == PURPOSE
        with pytest.raises(Problem):
            upload_purpose(artifacts, pending.etag)
        reconciled = artifacts.reconcile(A, A.team, "report", V1, pending.etag)
        writes.assert_called_once()
    receipt = artifacts.version(A, A.team, "report", V1)
    assert receipt.skill_purpose.description == PURPOSE and receipt.approval is None
    assert upload_purpose(artifacts, reconciled.etag).payload == reconciled.payload
    decide(artifacts, reconciled, receipt.model_dump(mode="json"))


@pytest.mark.parametrize("value", ["!not-base64!", "x" * 2049])
def test_purpose_header_is_bounded_and_canonical(value):
    with pytest.raises(ValueError):
        SkillPurpose.from_header(value)


def test_legacy_m3_catalog_and_archive_bytes_match_exact_2f33_base():
    from shared_context.m3_models import ApprovalRecord, ArtifactDocument, ArtifactVersion, BlobReceipt, PendingVersion
    from shared_context.m3_skills import SkillCatalog, SkillEntry, SkillReadResult
    from shared_context.behavior_instruction import TurnArchive
    from shared_context.behavior_assistant_wire import ArchiveUnavailable, SdkStop
    from shared_context.behavior_skill_prompt import assemble_prompt

    body = b"Legacy skill.\r\n"
    sha = hashlib.sha256(body).hexdigest()
    metadata = VersionInput(filename="guide.md", media_type="text/markdown", sha256=sha, source_work="source-work")
    decision = ApprovalInput(decision_id=V1, version_id=V1, sha256=sha, decision="approve")
    approval = ApprovalRecord(**decision.model_dump(), approver_oid="dddddddd-dddd-dddd-dddd-dddddddddddd",
                              decided_at="2026-09-25T00:00:00+00:00")
    provenance = dict(version_id=V1, created_by_oid=A.oid, source_work_revision=1, created_at="2026-09-25T00:00:00+00:00")
    version = ArtifactVersion(**metadata.model_dump(), **provenance, number=1, approval=approval,
                               blob=BlobReceipt(native_version="memory-v1", etag='"receipt"', sha256=sha, bytes=len(body)))
    document = ArtifactDocument(kind="skill", title="Legacy title", source_work="source-work", artifact_id="guide",
                                team=A.team, owner=A.actor, owner_oid=A.oid, revision=4, versions=[version], effective_version_id=V1)
    entry = SkillEntry(name="guide", title="Legacy title", version_id=V1, sha256=sha, byte_length=len(body),
                       team=A.team, source_work="source-work", source_work_revision=1)
    catalog = SkillCatalog(entries=(entry,))
    cases = (
        (metadata, 156, "91fb5d89c8601f70a2b536bf04631646f6735082ff8267edef1deaffd7013655"),
        (decision, 195, "12b7fcf6da52ebf41821645b4314b3442c9fd9fcafee39110961b5d3d1a70378"),
        (approval, 290, "1868ba8034b8ffb05c04583e1908eb154fd9d07443e82133d22e3c8c0303ce7a"),
        (version, 785, "d8980bf349657835621429a7b56b0b7d1de13ba8767aef9e0006746bb5b926ba"),
        (PendingVersion(**metadata.model_dump(), **provenance, bytes=len(body)), 367,
         "21d8baa279488911a691e061077d9225c6fd19a9d82e25cfa611c6a531d7959a"),
        (document, 1080, "a7f5dc7f0621d373b72fce641080b59b09457f3510b345fa045bceea4c94f176"),
        (entry, 323, "3a96f69dd973c032cd1b5c943a0be76aa7192fb322f16381bb8a2404bad6b323"),
        (catalog, 459, "cf2e5a4fbbd1c2f6f21c6170790532cb7ee9b0185953d32dbe27be5224e3ec17"),
        (SkillReadResult(skill=entry, text=body.decode()), 360,
         "47f082617f21c99e3b1d3bad7fa8afa42c5a32f7a7d4a0366cb4642df497baa8"),
        # The 2f33 base ran SDK 1.0.13 / runtime 1.0.83 (still a known release): pinned so the bytes do not move with
        # sdk_pin or the send profile's pair (SENT_RELEASE).
        (TurnArchive(conversation_id="conversation", job_id="job", turn_number=1,
            archive=ArchiveUnavailable(error="SDK_ARCHIVE_UNAVAILABLE"),
            sdk_stop=SdkStop(runtime_version="1.0.83", pid=7, exit_code=0, graceful=True),
            prompt_assembly=assemble_prompt("fa-instruction-skills-v1").record.model_copy(
                update={"sdk_version": "1.0.13", "runtime_version": "1.0.83"}), skill_catalog=catalog), 1658,
         "564c9e68c989857b22ba2e9fd7a10412e9fe35581f356481e8f4ac04f9453cfa"),
    )
    for value, length, expected_sha in cases:
        raw = value.model_dump_json().encode("utf-8")
        assert len(raw) == length and hashlib.sha256(raw).hexdigest() == expected_sha
        assert "skill_purpose" not in raw.decode() and "metadata_sha256" not in raw.decode()
    for profile, length, expected_sha in (
        (None, 552, "f2d312f2cab688a03d035adb971ead55e4fd5057c69bdc0eede97c5cb084799f"),
        ("fa-instruction-v1", 2094, "8e24b5291f5a46d3795e9c9b56c8e4dcde3af705deb23eaa08899ce0613963cb"),
        ("fa-instruction-skills-v1", 2572, "14a9d2a605706db98e0e8cea877beec5ef9a86646dd21bc544049e0dabf4ab3e"),
        ("fa-instruction-user-turns-v1", 2822, "a79684eab440d77a92c2b3e19f3753718a486fbda4c4889c64029a23014c1372"),
    ):
        raw = assemble_prompt(profile).system_message.encode("utf-8")
        assert len(raw) == length and hashlib.sha256(raw).hexdigest() == expected_sha


def test_same_body_and_version_id_cannot_transfer_purpose_approval_to_another_artifact(m3):
    artifacts = m3[0]
    uploaded, original = draft(artifacts)
    decide(artifacts, uploaded, original)
    created = create_skill(artifacts, "other-guide", title="Other artifact")
    copied = upload_purpose(artifacts, created.etag, name="other-guide")
    candidate = copied.payload["versions"][0]
    assert candidate["sha256"] == original["sha256"] and candidate["version_id"] == original["version_id"]
    assert candidate["metadata_sha256"] != original["metadata_sha256"]
    with pytest.raises(Problem) as refused:
        decide(artifacts, copied, original, name="other-guide")
    assert code(refused) == (409, "M3_APPROVAL_METADATA_MISMATCH")
    assert current(artifacts, "other-guide") == copied.response()
