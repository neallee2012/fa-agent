"""An explicit human effectivity decision never rewrites approved skill material."""

import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from conftest import A, B, C
from shared_context.errors import Problem
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import (
    MAX_ARTIFACT_VERSIONS, ApprovalInput, ApprovalRecord, ArtifactDocument, ArtifactVersion, BlobReceipt,
    CreateArtifact, SkillDeactivationInput, SkillDeactivationRecord, SkillPurpose, VersionInput, skill_metadata_sha256,
)
from shared_context.storage import MAX_PAYLOAD_BYTES, serialize
from test_m3_artifacts import HUMAN, create, decision, m3, upload
from test_m3_auth import UNKNOWN_OID

V1, V2 = "1" * 32, "2" * 32
D1 = "d" * 32
BODY = "原始已批准技能。\r\n只讀合成資料。\t\n".encode("utf-8")
NAME = "deactivation-skill"


def skill_version(data, purpose):
    return VersionInput(filename="skill.md", media_type="text/markdown", sha256=hashlib.sha256(data).hexdigest(),
                        skill_purpose=purpose)


def approve(service, etag, version):
    return service.decide(HUMAN, A.team, NAME, etag, ApprovalInput(
        decision_id=version["version_id"], version_id=version["version_id"], sha256=version["sha256"],
        decision="approve", metadata_sha256=version["metadata_sha256"],
    ))


@pytest.fixture
def approved_skill(m3):
    service, _, store = m3
    created = service.create(A, A.team, NAME, CreateArtifact(
        kind="skill", title="Synthetic skill effectivity", source_work="source-work",
    ))
    purpose = SkillPurpose(schema_version="fa-skill-purpose-v1", description="保留原件與獨立停用決定。")
    uploaded = service.upload(A, A.team, NAME, V1, created.etag, skill_version(BODY, purpose), BODY)
    approved = approve(service, uploaded.etag, service.version(HUMAN, A.team, NAME, V1).model_dump())
    return service, store, approved


def deactivation_body(version, *, decision_id=D1):
    return {
        "schema_version": "fa-skill-deactivation-v1", "decision_id": decision_id,
        "version_id": version["version_id"], "sha256": version["sha256"],
        "metadata_sha256": version.get("metadata_sha256"),
    }


def deactivate(service, etag, body, principal=HUMAN, team=A.team):
    return service.deactivate_skill(principal, team, NAME, etag, SkillDeactivationInput.model_validate(body))


def current(service):
    return service.get(HUMAN, A.team, NAME).response()


def test_human_deactivation_retains_approval_versions_and_original_bytes(approved_skill):
    service, store, before = approved_skill
    original = copy.deepcopy(before.payload)
    version = original["versions"][0]
    with patch.object(store, "put", wraps=store.put) as put:
        after = deactivate(service, before.etag, deactivation_body(version)).payload
    put.assert_not_called()
    assert after["effective_version_id"] is None
    assert after["versions"] == original["versions"]
    assert after["revision"] == original["revision"] + 1
    assert after["deactivations"][0]["version_id"] == V1
    assert after["deactivations"][0]["decision_id"] == D1
    with pytest.raises(Problem) as inactive:
        service.effective(A, A.team, NAME)
    assert inactive.value.status == 404
    assert service.download(A, A.team, NAME, V1, version["blob"]["etag"])[1] == BODY
    assert service.policy_context(A, A.team, (NAME,)) == [
        {"artifact_id": NAME, "status": "deactivated"},
    ]


@pytest.mark.parametrize("principal", [
    A, B, C, HumanApprover(UNKNOWN_OID, A.team), HumanApprover(HUMAN.oid, C.team),
], ids=["agent-a", "agent-b", "other-team-agent", "unknown-human", "human-of-another-team"])
def test_only_the_configured_human_can_deactivate(approved_skill, principal):
    """Token claims (appid, scp) that stop a principal being produced are pinned in test_m3_auth."""
    service, _, before = approved_skill
    with pytest.raises(Problem) as denied:
        deactivate(service, before.etag, deactivation_body(before.payload["versions"][0]), principal)
    assert denied.value.status == 403
    assert current(service) == before.response()


@pytest.mark.parametrize("etag,status", [(None, 428), ("*", 412), ('"stale"', 412)])
def test_deactivation_requires_the_exact_artifact_etag(approved_skill, etag, status):
    service, _, before = approved_skill
    with pytest.raises(Problem) as denied:
        deactivate(service, etag, deactivation_body(before.payload["versions"][0]))
    assert denied.value.status == status
    assert current(service) == before.response()


@pytest.mark.parametrize("change,status", [
    ({"sha256": "0" * 64}, 409), ({"metadata_sha256": "0" * 64}, 409),
    ({"version_id": V2}, 409), ({"decision_id": V1}, 409),
    ({"schema_version": "future"}, 422), ({"deactivated_by_oid": A.oid}, 422),
    ({"applied_revision": 99}, 422), ({"approved": True}, 422),
])
def test_deactivation_cannot_rewrite_the_pinned_version_or_claim_authority(approved_skill, change, status):
    service, _, before = approved_skill
    body = {**deactivation_body(before.payload["versions"][0]), **change}
    if status == 422:
        with pytest.raises(ValidationError):
            deactivate(service, before.etag, body)
    else:
        with pytest.raises(Problem) as denied:
            deactivate(service, before.etag, body)
        assert denied.value.status == status
    assert current(service) == before.response()


def test_original_deactivation_receipt_does_not_disable_a_later_approved_version(approved_skill):
    service, _, first = approved_skill
    version = first.payload["versions"][0]
    body = deactivation_body(version)
    later = BODY + b"version two\n"
    uploaded = service.upload(A, A.team, NAME, V2, first.etag,
                              skill_version(later, SkillPurpose.model_validate(version["skill_purpose"])), later)
    assert uploaded.payload["effective_version_id"] == V1
    disabled = deactivate(service, uploaded.etag, body)
    assert disabled.payload["effective_version_id"] is None
    original_receipt = copy.deepcopy(disabled.payload["deactivations"][0])
    old_approval = approve(service, first.etag, version)
    assert old_approval.response() == disabled.response()
    second = disabled.payload["versions"][1]
    approved = approve(service, disabled.etag, second)
    assert approved.payload["effective_version_id"] == V2
    repeated = deactivate(service, first.etag, body)
    assert repeated.response() == approved.response()
    assert repeated.payload["deactivations"] == [original_receipt]
    assert repeated.payload["versions"][0] == version
    with pytest.raises(Problem) as changed:
        deactivate(service, approved.etag, deactivation_body(second))
    assert changed.value.status == 409 and changed.value.body["error"]["code"] == "M3_DECISION_CONFLICT"
    assert current(service) == approved.response()
    with pytest.raises(Problem) as stale_target:
        deactivate(service, approved.etag, {**body, "decision_id": "e" * 32})
    assert stale_target.value.status == 409


def test_unknown_deactivation_response_is_read_back_without_replacing_again(approved_skill, monkeypatch):
    service, store, before = approved_skill
    original = service.repository.replace
    replaces = []

    def lost_ack(*args):
        value = original(*args)
        replaces.append(value)
        raise Problem(502, "M3_STATE_UNCERTAIN", "synthetic acknowledgement loss")

    monkeypatch.setattr(service.repository, "replace", lost_ack)
    body = deactivation_body(before.payload["versions"][0])
    with patch.object(store, "read", side_effect=AssertionError("deactivation must not require blob access")):
        with pytest.raises(Problem) as unknown:
            deactivate(service, before.etag, body)
    assert unknown.value.status == 502 and len(replaces) == 1
    saved = current(service)
    assert saved["effective_version_id"] is None
    repeated = deactivate(service, before.etag, body)
    assert repeated.response() == saved and len(replaces) == 1
    assert saved["versions"] == before.payload["versions"]


def test_deactivation_preserves_an_unknown_upload_for_original_reconciliation(approved_skill):
    service, store, before = approved_skill
    native_put = store.put
    version = before.payload["versions"][0]

    def lost_blob_ack(*args):
        native_put(*args)
        raise Problem(502, "M3_UPLOAD_UNKNOWN", "synthetic blob acknowledgement loss")

    with patch.object(store, "put", side_effect=lost_blob_ack):
        with pytest.raises(Problem) as pending:
            service.upload(A, A.team, NAME, V2, before.etag,
                           skill_version(BODY, SkillPurpose.model_validate(version["skill_purpose"])), BODY)
    assert pending.value.status == 502
    reserved = service.get(HUMAN, A.team, NAME)
    assert reserved.payload["pending"]["version_id"] == V2 and reserved.payload["effective_version_id"] == V1
    with patch.object(store, "read", side_effect=AssertionError("deactivation must not depend on a blob read")):
        inactive = deactivate(service, reserved.etag, deactivation_body(version))
    assert inactive.payload["pending"] == reserved.payload["pending"]
    assert inactive.payload["versions"] == reserved.payload["versions"]
    with patch.object(store, "put", wraps=store.put) as put:
        reconciled = service.reconcile(A, A.team, NAME, V2, inactive.etag).payload
    put.assert_not_called()
    assert reconciled["pending"] is None and reconciled["effective_version_id"] is None
    assert reconciled["deactivations"] == inactive.payload["deactivations"]
    assert reconciled["versions"][0] == version
    assert reconciled["versions"][1]["approval"] is None


def test_competing_human_deactivations_keep_only_one_original_receipt(approved_skill):
    service, _, before = approved_skill
    principal = HumanApprover(service.approval.approver_oid, A.team)
    version = before.payload["versions"][0]

    def attempt(identity):
        try:
            deactivate(service, before.etag, deactivation_body(version, decision_id=identity), principal)
            return "accepted"
        except Problem as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (D1, "e" * 32)))
    assert sorted(outcomes, key=str) == [412, "accepted"]
    saved = current(service)
    assert len(saved["deactivations"]) == 1 and saved["effective_version_id"] is None
    assert saved["versions"] == before.payload["versions"]


@pytest.mark.parametrize("change", [
    {"schema_version": "future"}, {"sha256": "0" * 64}, {"metadata_sha256": "0" * 64},
    {"version_id": V2}, {"decision_id": V1}, {"applied_revision": 0},
    {"applied_revision": False}, {"applied_revision": 999999},
])
def test_deactivation_material_cannot_be_rewritten_in_a_saved_document(approved_skill, change):
    service, _, before = approved_skill
    data = copy.deepcopy(deactivate(service, before.etag, deactivation_body(before.payload["versions"][0])).payload)
    data["deactivations"][0].update(change)
    with pytest.raises(ValueError):
        ArtifactDocument.model_validate(data)


def test_deactivated_version_cannot_be_reactivated_or_duplicate_its_decision(approved_skill):
    service, _, before = approved_skill
    data = copy.deepcopy(deactivate(service, before.etag, deactivation_body(before.payload["versions"][0])).payload)
    with pytest.raises(ValueError):
        ArtifactDocument.model_validate({**data, "effective_version_id": V1})
    with pytest.raises(ValueError):
        ArtifactDocument.model_validate({**data, "deactivations": data["deactivations"] * 2})


@pytest.mark.parametrize("kind", ["deliverable", "sop"])
def test_skill_deactivation_never_changes_other_artifact_kinds(m3, kind):
    service, _, store = m3
    create(service, kind)
    saved = upload(service)
    if kind == "sop":
        saved = decision(service)
    version = saved.payload["versions"][0]
    with patch.object(store, "read", side_effect=AssertionError("no blob access")), pytest.raises(Problem) as error:
        service.deactivate_skill(HUMAN, A.team, "report", saved.etag,
            SkillDeactivationInput.model_validate(deactivation_body(version)))
    assert error.value.status == 409 and error.value.body["error"]["code"] == "M3_NOT_SKILL"
    assert service.get(HUMAN, A.team, "report") == saved


def test_cross_team_and_unapproved_versions_cannot_be_deactivated(approved_skill, m3):
    service, _, before = approved_skill
    with pytest.raises(Problem) as denied:
        deactivate(service, before.etag, deactivation_body(before.payload["versions"][0]), team=C.team)
    assert denied.value.status == 403
    assert current(service) == before.response()
    create(service, "skill")
    saved = upload(service)
    with pytest.raises(Problem) as error:
        service.deactivate_skill(HUMAN, A.team, "report", saved.etag,
            SkillDeactivationInput.model_validate(deactivation_body(saved.payload["versions"][0])))
    assert error.value.status == 409 and service.get(HUMAN, A.team, "report") == saved


def test_absent_deactivation_history_preserves_the_legacy_document_bytes():
    document = ArtifactDocument(
        kind="skill", title="Legacy skill", source_work="source-work",
        artifact_id="legacy-skill", team=A.team, owner=A.actor, owner_oid=A.oid, revision=1,
    )
    expected = (
        '{"kind":"skill","title":"Legacy skill","source_work":"source-work","schema_version":1,'
        '"artifact_id":"legacy-skill","team":"team-one","owner":"agent-a",'
        '"owner_oid":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","revision":1,"versions":[],'
        '"pending":null,"effective_version_id":null}'
    )
    assert document.model_dump_json() == expected
    assert ArtifactDocument.model_validate({**document.model_dump(), "deactivations": []}).model_dump_json() == expected


def test_twelve_bounded_skill_versions_and_deactivation_receipts_fit_one_cas_record():
    versions, receipts = [], []
    team, artifact = "t" * 64, "a" * 64
    purpose = SkillPurpose(schema_version="fa-skill-purpose-v1", description="漢" * 170 + "ab")
    for number in range(1, MAX_ARTIFACT_VERSIONS + 1):
        identity, digest = f"{number:032x}", f"{number:064x}"
        fields = {
            "filename": "x" * 128, "media_type": "text/markdown", "sha256": digest,
            "version_id": identity, "number": number, "created_by_oid": A.oid,
            "created_at": "t" * 64, "source_work_revision": 999999999999, "source_work": "w" * 64,
            "skill_purpose": purpose.model_dump(),
            "blob": BlobReceipt(native_version="v" * 256, etag='"' + "e" * 254 + '"',
                                sha256=digest, bytes=8192).model_dump(),
        }
        metadata = skill_metadata_sha256(team, artifact, fields)
        versions.append(ArtifactVersion(**fields, metadata_sha256=metadata, approval=ApprovalRecord(
            decision_id=identity, version_id=identity, sha256=digest, metadata_sha256=metadata,
            decision="approve", approver_oid=HUMAN.oid, decided_at="t" * 64,
        )))
        receipts.append(SkillDeactivationRecord(
            schema_version="fa-skill-deactivation-v1", decision_id=f"{number + 100:032x}",
            version_id=identity, sha256=digest, metadata_sha256=metadata,
            deactivated_by_oid=HUMAN.oid, deactivated_at="t" * 64, applied_revision=number + 1,
        ))
    document = ArtifactDocument(
        kind="skill", title="漢" * 256, source_work="w" * 64, artifact_id=artifact, team=team,
        owner="o" * 64, owner_oid=A.oid, revision=999999999999,
        versions=versions, deactivations=receipts,
    )
    assert len(serialize(document.model_dump())) < MAX_PAYLOAD_BYTES
