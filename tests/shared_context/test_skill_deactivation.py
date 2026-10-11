"""An explicit human effectivity decision never rewrites approved skill material."""

import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from conftest import A, B, C
from shared_context.api import create_app
from shared_context.auth import JwtVerifier
from shared_context.errors import Problem
from shared_context.m3_auth import HumanApprover, M3JwtVerifier
from shared_context.m3_models import (
    MAX_ARTIFACT_VERSIONS, ApprovalRecord, ArtifactDocument, ArtifactVersion, BlobReceipt,
    SkillDeactivationInput, SkillDeactivationRecord, SkillPurpose, skill_metadata_sha256,
)
from shared_context.storage import MAX_PAYLOAD_BYTES, serialize
from test_m3_artifacts import HUMAN, create, decision, m3, upload
from test_m3_auth import UNKNOWN_OID, jwk_client, signing_key, token

V1, V2 = "1" * 32, "2" * 32
D1 = "d" * 32
BODY = "原始已批准技能。\r\n只讀合成資料。\t\n".encode("utf-8")
PATH = "/teams/" + A.team + "/artifacts/deactivation-skill"


@pytest.fixture
def deactivation_api(m3, signing_key, jwk_client):
    service, _, store = m3
    app = create_app(
        service.context, JwtVerifier(service.context.auth, jwk_client), artifact_service=service,
        verified_artifact_identity_dependency=M3JwtVerifier(service.context.auth, service.approval, jwk_client),
    )
    with TestClient(app) as client:
        agent = {"Authorization": "Bearer " + token(signing_key, {"oid": A.oid})}
        human = {"Authorization": "Bearer " + token(signing_key)}
        created = client.post(PATH, headers=agent, json={
            "kind": "skill", "title": "Synthetic skill effectivity", "source_work": "source-work",
        })
        assert created.status_code == 201
        purpose = SkillPurpose(schema_version="fa-skill-purpose-v1", description="保留原件與獨立停用決定。")
        uploaded = client.put(PATH + "/versions/" + V1, content=BODY, headers={
            **agent, "If-Match": created.headers["etag"], "Content-Type": "application/octet-stream",
            "X-Artifact-Filename": "skill.md", "X-Artifact-Media-Type": "text/markdown",
            "X-Artifact-SHA256": hashlib.sha256(BODY).hexdigest(), "X-Artifact-Skill-Purpose": purpose.header(),
        })
        assert uploaded.status_code == 200
        version = client.get(PATH + "/versions/" + V1, headers=human).json()
        approved = client.post(PATH + "/decisions", headers={**human, "If-Match": uploaded.headers["etag"]}, json={
            "decision_id": V1, "version_id": V1, "sha256": version["sha256"], "decision": "approve",
            "metadata_sha256": version["metadata_sha256"],
        })
        assert approved.status_code == 200
        yield client, agent, human, service, store, approved


def deactivation_body(version, *, decision_id=D1):
    return {
        "schema_version": "fa-skill-deactivation-v1", "decision_id": decision_id,
        "version_id": version["version_id"], "sha256": version["sha256"],
        "metadata_sha256": version.get("metadata_sha256"),
    }


def test_signed_human_deactivation_retains_approval_versions_and_original_bytes(deactivation_api):
    client, agent, human, service, store, before = deactivation_api
    original = copy.deepcopy(before.json())
    version = original["versions"][0]
    with patch.object(store, "put", wraps=store.put) as put:
        response = client.post(PATH + "/deactivations",
            headers={**human, "If-Match": before.headers["etag"]}, json=deactivation_body(version))
    assert response.status_code == 200, response.text
    put.assert_not_called()
    after = response.json()
    assert after["effective_version_id"] is None
    assert after["versions"] == original["versions"]
    assert after["revision"] == original["revision"] + 1
    assert after["deactivations"][0]["version_id"] == V1
    assert after["deactivations"][0]["decision_id"] == D1
    assert client.get(PATH + "/effective", headers=agent).status_code == 404
    saved = client.get(PATH + "/versions/" + V1 + "/content",
        headers={**agent, "If-Match": version["blob"]["etag"]})
    assert saved.status_code == 200 and saved.content == BODY
    assert service.policy_context(A, A.team, ("deactivation-skill",)) == [
        {"artifact_id": "deactivation-skill", "status": "deactivated"},
    ]


@pytest.mark.parametrize("claims", [
    {"oid": A.oid}, {"oid": B.oid}, {"oid": C.oid}, {"oid": UNKNOWN_OID},
    {"appid": UNKNOWN_OID}, {"scp": "Artifacts.Read"},
])
def test_only_the_verified_configured_human_can_deactivate(deactivation_api, signing_key, claims):
    client, _, human, _, _, before = deactivation_api
    response = client.post(PATH + "/deactivations", json=deactivation_body(before.json()["versions"][0]),
        headers={"Authorization": "Bearer " + token(signing_key, claims), "If-Match": before.headers["etag"]})
    assert response.status_code == 403
    assert client.get(PATH, headers=human).json() == before.json()


@pytest.mark.parametrize("etag,status", [(None, 428), ("*", 412), ('"stale"', 412)])
def test_deactivation_requires_the_exact_artifact_etag(deactivation_api, etag, status):
    client, _, human, _, _, before = deactivation_api
    headers = dict(human)
    if etag is not None:
        headers["If-Match"] = etag
    response = client.post(PATH + "/deactivations", headers=headers,
        json=deactivation_body(before.json()["versions"][0]))
    assert response.status_code == status
    assert client.get(PATH, headers=human).json() == before.json()


@pytest.mark.parametrize("change,status", [
    ({"sha256": "0" * 64}, 409), ({"metadata_sha256": "0" * 64}, 409),
    ({"version_id": V2}, 409), ({"decision_id": V1}, 409),
    ({"schema_version": "future"}, 422), ({"deactivated_by_oid": A.oid}, 422),
    ({"applied_revision": 99}, 422), ({"approved": True}, 422),
])
def test_deactivation_cannot_rewrite_the_pinned_version_or_claim_authority(deactivation_api, change, status):
    client, _, human, _, _, before = deactivation_api
    response = client.post(PATH + "/deactivations",
        headers={**human, "If-Match": before.headers["etag"]},
        json={**deactivation_body(before.json()["versions"][0]), **change})
    assert response.status_code == status
    assert client.get(PATH, headers=human).json() == before.json()


def test_original_deactivation_receipt_does_not_disable_a_later_approved_version(deactivation_api):
    client, agent, human, _, _, first = deactivation_api
    version = first.json()["versions"][0]
    body = deactivation_body(version)
    uploaded = client.put(PATH + "/versions/" + V2, content=BODY + b"version two\n", headers={
        **agent, "If-Match": first.headers["etag"], "Content-Type": "application/octet-stream",
        "X-Artifact-Filename": "skill.md", "X-Artifact-Media-Type": "text/markdown",
        "X-Artifact-SHA256": hashlib.sha256(BODY + b"version two\n").hexdigest(),
        "X-Artifact-Skill-Purpose": SkillPurpose.model_validate(version["skill_purpose"]).header(),
    })
    assert uploaded.status_code == 200 and uploaded.json()["effective_version_id"] == V1
    disabled = client.post(PATH + "/deactivations", json=body,
        headers={**human, "If-Match": uploaded.headers["etag"]})
    assert disabled.status_code == 200 and disabled.json()["effective_version_id"] is None
    original_receipt = copy.deepcopy(disabled.json()["deactivations"][0])
    old_approval = client.post(PATH + "/decisions", json={
        "decision_id": V1, "version_id": V1, "sha256": version["sha256"], "decision": "approve",
        "metadata_sha256": version["metadata_sha256"],
    }, headers={**human, "If-Match": first.headers["etag"]})
    assert old_approval.status_code == 200 and old_approval.json() == disabled.json()
    second = disabled.json()["versions"][1]
    approved = client.post(PATH + "/decisions", json={
        "decision_id": V2, "version_id": V2, "sha256": second["sha256"], "decision": "approve",
        "metadata_sha256": second["metadata_sha256"],
    }, headers={**human, "If-Match": disabled.headers["etag"]})
    assert approved.status_code == 200 and approved.json()["effective_version_id"] == V2
    repeated = client.post(PATH + "/deactivations", json=body,
        headers={**human, "If-Match": first.headers["etag"]})
    assert repeated.status_code == 200 and repeated.json() == approved.json()
    assert repeated.json()["deactivations"] == [original_receipt]
    assert repeated.json()["versions"][0] == version
    changed = client.post(PATH + "/deactivations",
        json=deactivation_body(second), headers={**human, "If-Match": approved.headers["etag"]})
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "M3_DECISION_CONFLICT"
    assert client.get(PATH, headers=human).json() == approved.json()
    stale_target = client.post(PATH + "/deactivations", json={**body, "decision_id": "e" * 32},
        headers={**human, "If-Match": approved.headers["etag"]})
    assert stale_target.status_code == 409


def test_unknown_deactivation_response_is_read_back_without_replacing_again(deactivation_api, monkeypatch):
    client, _, human, service, store, before = deactivation_api
    original = service.repository.replace
    replaces = []

    def lost_ack(*args):
        value = original(*args)
        replaces.append(value)
        raise Problem(502, "M3_STATE_UNCERTAIN", "synthetic acknowledgement loss")

    monkeypatch.setattr(service.repository, "replace", lost_ack)
    body = deactivation_body(before.json()["versions"][0])
    with patch.object(store, "read", side_effect=AssertionError("deactivation must not require blob access")):
        response = client.post(PATH + "/deactivations", json=body,
            headers={**human, "If-Match": before.headers["etag"]})
    assert response.status_code == 502 and len(replaces) == 1
    saved = client.get(PATH, headers=human)
    assert saved.status_code == 200 and saved.json()["effective_version_id"] is None
    repeated = client.post(PATH + "/deactivations", json=body,
        headers={**human, "If-Match": before.headers["etag"]})
    assert repeated.status_code == 200 and repeated.json() == saved.json() and len(replaces) == 1
    assert saved.json()["versions"] == before.json()["versions"]


def test_deactivation_preserves_an_unknown_upload_for_original_reconciliation(deactivation_api):
    client, agent, human, _, store, before = deactivation_api
    native_put = store.put
    version = before.json()["versions"][0]

    def lost_blob_ack(*args):
        native_put(*args)
        raise Problem(502, "M3_UPLOAD_UNKNOWN", "synthetic blob acknowledgement loss")

    with patch.object(store, "put", side_effect=lost_blob_ack):
        pending = client.put(PATH + "/versions/" + V2, content=BODY, headers={
            **agent, "If-Match": before.headers["etag"], "Content-Type": "application/octet-stream",
            "X-Artifact-Filename": "skill.md", "X-Artifact-Media-Type": "text/markdown",
            "X-Artifact-SHA256": version["sha256"],
            "X-Artifact-Skill-Purpose": SkillPurpose.model_validate(version["skill_purpose"]).header(),
        })
    assert pending.status_code == 502
    reserved = client.get(PATH, headers=human)
    assert reserved.json()["pending"]["version_id"] == V2 and reserved.json()["effective_version_id"] == V1
    with patch.object(store, "read", side_effect=AssertionError("deactivation must not depend on a blob read")):
        inactive = client.post(PATH + "/deactivations", json=deactivation_body(version),
            headers={**human, "If-Match": reserved.headers["etag"]})
    assert inactive.status_code == 200
    assert inactive.json()["pending"] == reserved.json()["pending"]
    assert inactive.json()["versions"] == reserved.json()["versions"]
    with patch.object(store, "put", wraps=store.put) as put:
        reconciled = client.post(PATH + "/versions/" + V2 + "/reconcile", json={},
            headers={**agent, "If-Match": inactive.headers["etag"]})
    assert reconciled.status_code == 200
    put.assert_not_called()
    assert reconciled.json()["pending"] is None and reconciled.json()["effective_version_id"] is None
    assert reconciled.json()["deactivations"] == inactive.json()["deactivations"]
    assert reconciled.json()["versions"][0] == version
    assert reconciled.json()["versions"][1]["approval"] is None


def test_competing_human_deactivations_keep_only_one_original_receipt(deactivation_api):
    client, _, human, service, _, before = deactivation_api
    principal = HumanApprover(service.approval.approver_oid, A.team)
    version = before.json()["versions"][0]

    def attempt(identity):
        try:
            service.deactivate_skill(principal, A.team, "deactivation-skill", before.headers["etag"],
                SkillDeactivationInput.model_validate(deactivation_body(version, decision_id=identity)))
            return "accepted"
        except Problem as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (D1, "e" * 32)))
    assert sorted(outcomes, key=str) == [412, "accepted"]
    current = client.get(PATH, headers=human).json()
    assert len(current["deactivations"]) == 1 and current["effective_version_id"] is None
    assert current["versions"] == before.json()["versions"]


@pytest.mark.parametrize("change", [
    {"schema_version": "future"}, {"sha256": "0" * 64}, {"metadata_sha256": "0" * 64},
    {"version_id": V2}, {"decision_id": V1}, {"applied_revision": 0},
    {"applied_revision": False}, {"applied_revision": 999999},
])
def test_deactivation_material_cannot_be_rewritten_in_a_saved_document(deactivation_api, change):
    client, _, human, _, _, before = deactivation_api
    response = client.post(PATH + "/deactivations", json=deactivation_body(before.json()["versions"][0]),
        headers={**human, "If-Match": before.headers["etag"]})
    assert response.status_code == 200
    data = response.json()
    data.pop("etag")
    data["deactivations"][0].update(change)
    with pytest.raises(ValueError):
        ArtifactDocument.model_validate(data)


def test_deactivated_version_cannot_be_reactivated_or_duplicate_its_decision(deactivation_api):
    client, _, human, _, _, before = deactivation_api
    response = client.post(PATH + "/deactivations", json=deactivation_body(before.json()["versions"][0]),
        headers={**human, "If-Match": before.headers["etag"]})
    data = response.json()
    data.pop("etag")
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


def test_cross_team_and_unapproved_versions_cannot_be_deactivated(deactivation_api, m3):
    client, _, human, _, _, before = deactivation_api
    denied = client.post(PATH.replace(A.team, C.team) + "/deactivations",
        headers={**human, "If-Match": before.headers["etag"]},
        json=deactivation_body(before.json()["versions"][0]))
    assert denied.status_code == 403
    assert client.get(PATH, headers=human).json() == before.json()
    service, _, _ = m3
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
