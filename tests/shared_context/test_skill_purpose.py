"""Version-bound skill purpose through the signed M3 public producer/read interfaces."""

import base64
import copy
import hashlib
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from conftest import A, B, C
from legacy_m2_m4.api import create_app
from shared_context.auth import JwtVerifier
from shared_context.m3_auth import M3JwtVerifier
from shared_context.errors import Problem
from shared_context.m3_client import ArtifactClient
from shared_context.m3_models import ApprovalInput, CreateArtifact, SkillPurpose, VersionInput
from shared_context.m3_skills import ApprovedSkillSource
from test_m3_artifacts import V1, V2, m3
from test_m3_auth import jwk_client, signing_key, token

PURPOSE = "  核對已保存回條，再說明哪些結果仍待確認。  "
BODY = b"PURPOSE_BODY_CANARY: verify the immutable receipt before reporting success.\r\n"
PATH = "/teams/team-one/artifacts/report"
PROFILE = "fa-instruction-skill-purpose-v1"


def signed_m3(artifacts, signing_key, jwk_client):
    auth = artifacts.context.auth
    http = TestClient(create_app(
        artifacts.context, JwtVerifier(auth, jwk_client), artifact_service=artifacts,
        verified_artifact_identity_dependency=M3JwtVerifier(auth, artifacts.approval, jwk_client),
    ))
    agent = {"Authorization": "Bearer " + token(signing_key, {"oid": A.oid})}
    human = {"Authorization": "Bearer " + token(signing_key)}
    return http, agent, human


def purpose_header(description):
    return base64.urlsafe_b64encode(json.dumps({
        "schema_version": "fa-skill-purpose-v1", "description": description,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii")


def upload_purpose(http, headers, etag, *, version=V1, description=PURPOSE, body=BODY, path=PATH, resources=None):
    return http.put(path + "/versions/" + version, content=body, headers={
        **headers, "If-Match": etag, "Content-Type": "application/octet-stream",
        "X-Artifact-Filename": "guide.md", "X-Artifact-Media-Type": "text/markdown",
        "X-Artifact-SHA256": hashlib.sha256(body).hexdigest(),
        "X-Artifact-Skill-Purpose": purpose_header(description),
        **({} if resources is None else {"X-Artifact-Skill-Resources": resources.header()}),
    })


def test_signed_version_and_human_approval_pin_purpose_before_catalog_or_body_read(m3, signing_key, jwk_client):
    artifacts, _, _ = m3
    http, agent, human = signed_m3(artifacts, signing_key, jwk_client)
    created = http.post(PATH, headers=agent, json={
        "kind": "skill", "title": "Not the approved purpose", "source_work": "source-work",
    })
    assert created.status_code == 201
    uploaded = upload_purpose(http, agent, created.headers["etag"])
    assert uploaded.status_code == 200, uploaded.text
    version = http.get(PATH + "/versions/" + V1, headers=human).json()
    assert version.get("skill_purpose") == {
        "schema_version": "fa-skill-purpose-v1", "description": PURPOSE,
    }, "the immutable skill version did not retain the author's exact purpose"
    source = ApprovedSkillSource(artifacts)
    assert source.catalog(A, A.team, ("report",), schema_version="fa-approved-skill-catalog-v2").entries == ()
    approved = http.post(PATH + "/decisions", headers={**human, "If-Match": uploaded.headers["etag"]}, json={
        "decision_id": V1, "version_id": V1, "decision": "approve",
        "sha256": version["sha256"], "metadata_sha256": version["metadata_sha256"],
    })
    assert approved.status_code == 200, approved.text
    catalog = source.catalog(A, A.team, ("report",), schema_version="fa-approved-skill-catalog-v2")
    entry = catalog.entries[0]
    assert entry.description == PURPOSE and entry.metadata_sha256 == version["metadata_sha256"]
    assert "PURPOSE_BODY_CANARY" not in catalog.model_dump_json()
    result = source.read(A, A.team, ("report",), entry)
    assert result.skill == entry and result.text.encode("utf-8") == BODY
    assert result.skill.sha256 == hashlib.sha256(BODY).hexdigest()


def draft(m3, signing_key, jwk_client, *, description=PURPOSE, kind="skill"):
    artifacts = m3[0]
    http, agent, human = signed_m3(artifacts, signing_key, jwk_client)
    created = http.post(PATH, headers=agent, json={
        "kind": kind, "title": "Title is not approved purpose", "source_work": "source-work",
    })
    assert created.status_code == 201
    uploaded = upload_purpose(http, agent, created.headers["etag"], description=description)
    assert uploaded.status_code == 200, uploaded.text
    version = uploaded.json()["versions"][0]
    return http, agent, human, uploaded, version


def decide(http, human, uploaded, version, **overrides):
    body = {"decision_id": version["version_id"], "version_id": version["version_id"], "decision": "approve",
            "sha256": version["sha256"], "metadata_sha256": version.get("metadata_sha256"), **overrides}
    return http.post(PATH + "/decisions", headers={**human, "If-Match": uploaded.headers["etag"]}, json=body)


@pytest.mark.parametrize("description", [
    "", "   ", "x" * 513, "界" * 171, "line\nbreak", "carriage\rreturn", "tab\t",
    "\x00hidden", "\x7fhidden", "\u202ehidden", "\u200bhidden",
])
def test_invalid_purpose_rejected_before_reservation_or_blob(m3, signing_key, jwk_client, description):
    artifacts, _, blobs = m3
    http, agent, _ = signed_m3(artifacts, signing_key, jwk_client)
    initial = http.post(PATH, headers=agent, json={
        "kind": "skill", "title": "Literal purpose", "source_work": "source-work",
    })
    with patch.object(blobs, "put", wraps=blobs.put) as put:
        response = upload_purpose(http, agent, initial.headers["etag"], description=description)
    assert response.status_code == 422 and response.json()["error"]["code"] == "M3_INVALID_HEADERS"
    put.assert_not_called()
    assert http.get(PATH, headers=agent).json() == initial.json()


@pytest.mark.parametrize("description", ["x" * 512, "界" * 170 + "  ", '"' * 512, "\\" * 512],
                         ids=["ascii", "unicode", "quotes", "backslashes"])
def test_exact_512_utf8_byte_purpose_is_not_trimmed(m3, signing_key, jwk_client, description):
    http, _, human, uploaded, version = draft(m3, signing_key, jwk_client, description=description)
    assert version["skill_purpose"]["description"] == description
    assert len(description.encode("utf-8")) == 512
    assert decide(http, human, uploaded, version).status_code == 200
    entry = ApprovedSkillSource(m3[0]).catalog(A, A.team, ("report",),
                                              schema_version="fa-approved-skill-catalog-v2").entries[0]
    assert entry.description == description


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
def test_unknown_or_malformed_purpose_schema_has_no_upload_fallback(m3, signing_key, jwk_client, raw):
    artifacts = m3[0]
    http, agent, _ = signed_m3(artifacts, signing_key, jwk_client)
    initial = http.post(PATH, headers=agent, json={
        "kind": "skill", "title": "Schema rejection", "source_work": "source-work",
    })
    from test_m3_api import upload
    response = upload(http, initial.headers["etag"], headers={
        **agent, "X-Artifact-Skill-Purpose": base64.urlsafe_b64encode(raw).decode("ascii"),
    })
    assert response.status_code == 422
    assert http.get(PATH, headers=agent).json() == initial.json()


@pytest.mark.parametrize("kind", ["sop", "deliverable"])
def test_purpose_metadata_cannot_turn_another_artifact_kind_into_a_skill(m3, signing_key, jwk_client, kind):
    http, agent, _ = signed_m3(m3[0], signing_key, jwk_client)
    initial = http.post(PATH, headers=agent, json={"kind": kind, "title": "Not a skill", "source_work": "source-work"})
    response = upload_purpose(http, agent, initial.headers["etag"])
    assert response.status_code == 422 and response.json()["error"]["code"] == "M3_POLICY_FORMAT"
    assert http.get(PATH, headers=agent).json() == initial.json()


@pytest.mark.parametrize("metadata_digest", [None, "0" * 64])
def test_body_hash_only_or_different_metadata_cannot_approve_purpose(m3, signing_key, jwk_client, metadata_digest):
    http, _, human, uploaded, version = draft(m3, signing_key, jwk_client)
    response = decide(http, human, uploaded, version, metadata_sha256=metadata_digest)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "M3_APPROVAL_METADATA_MISMATCH"
    assert http.get(PATH, headers=human).json() == uploaded.json()
    assert decide(http, human, uploaded, version).status_code == 200


def test_purpose_only_revision_requires_new_version_and_new_exact_approval(m3, signing_key, jwk_client):
    http, agent, human, uploaded, first = draft(m3, signing_key, jwk_client)
    approved = decide(http, human, uploaded, first)
    assert approved.status_code == 200
    original = http.get(PATH + "/versions/" + V1, headers=human).json()
    changed = "  Inspect the original receipt when comparing revisions.  "
    reused = upload_purpose(http, agent, approved.headers["etag"], description=changed)
    assert reused.status_code == 409 and reused.json()["error"]["code"] == "M3_VERSION_CONFLICT"
    assert http.get(PATH, headers=human).json() == approved.json()
    second_upload = upload_purpose(http, agent, approved.headers["etag"], version=V2, description=changed)
    assert second_upload.status_code == 200
    second = second_upload.json()["versions"][-1]
    assert first["sha256"] == second["sha256"] == hashlib.sha256(BODY).hexdigest()
    assert first["metadata_sha256"] != second["metadata_sha256"]
    source = ApprovedSkillSource(m3[0])
    current = lambda: source.catalog(A, A.team, ("report",), schema_version="fa-approved-skill-catalog-v2").entries[0]
    assert current().description == PURPOSE
    assert decide(http, human, second_upload, second, metadata_sha256=first["metadata_sha256"]).status_code == 409
    assert decide(http, human, second_upload, second, decision_id=V1).status_code == 409
    assert decide(http, human, second_upload, first).status_code == 200
    assert current().description == PURPOSE
    assert decide(http, human, second_upload, second).status_code == 200
    assert current().description == changed and current().version_id == V2
    assert http.get(PATH + "/versions/" + V1, headers=human).json() == original


@pytest.mark.parametrize("field", ["purpose", "source_work", "source_work_revision", "filename", "blob_etag", "approval"])
def test_changed_metadata_cannot_reuse_a_persisted_approval_or_receipt(m3, signing_key, jwk_client, field):
    artifacts, repository, _ = m3
    http, _, human, uploaded, version = draft(m3, signing_key, jwk_client)
    approved = decide(http, human, uploaded, version)
    assert approved.status_code == 200
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
    denied = http.get(PATH, headers=human)
    assert denied.status_code == 502 and denied.json()["error"]["code"] == "M3_STATE_INVALID"
    with pytest.raises(Problem, match="could not be verified"):
        ApprovedSkillSource(artifacts).catalog(A, A.team, ("report",), schema_version="fa-approved-skill-catalog-v2")
    assert PURPOSE not in denied.text and "PURPOSE_BODY_CANARY" not in denied.text


def test_legacy_version_has_unknown_purpose_and_original_v1_catalog_remains_unchanged(m3, signing_key, jwk_client):
    artifacts = m3[0]
    http, agent, human = signed_m3(artifacts, signing_key, jwk_client)
    from test_m3_api import upload
    initial = http.post(PATH, headers=agent, json={
        "kind": "skill", "title": "Cannot become purpose", "source_work": "source-work",
    })
    uploaded = upload(http, initial.headers["etag"], b"Purpose: cannot infer this body as approved metadata.", headers=agent)
    version = uploaded.json()["versions"][0]
    assert "skill_purpose" not in version and "metadata_sha256" not in version
    assert decide(http, human, uploaded, version).status_code == 200
    source = ApprovedSkillSource(artifacts)
    v1 = source.catalog(A, A.team, ("report",)).model_dump_json()
    v2 = source.catalog(A, A.team, ("report",), schema_version="fa-approved-skill-catalog-v2")
    assert v2.entries[0].description is None and v2.entries[0].metadata_sha256 is None
    assert "Purpose: cannot" not in v2.model_dump_json()
    assert source.catalog(A, A.team, ("report",)).model_dump_json() == v1
    assert '"description":null' in v1 and "metadata_sha256" not in v1


def test_signed_author_team_expiry_and_human_scope_are_still_required(m3, signing_key, jwk_client):
    http, agent, human, uploaded, version = draft(m3, signing_key, jwk_client)
    for identity, status in ((agent, 403), ({"Authorization": "Bearer " + token(signing_key, {"scp": "Artifacts.Read"})}, 403),
                             ({"Authorization": "Bearer " + token(signing_key, {"exp": 1})}, 401)):
        assert decide(http, identity, uploaded, version).status_code == status
    for actor in (B, C):
        peer = {"Authorization": "Bearer " + token(signing_key, {"oid": actor.oid})}
        assert upload_purpose(http, peer, uploaded.headers["etag"], version=V2).status_code == 403
    other_team = http.get("/teams/team-two/artifacts/report", headers=agent)
    assert other_team.status_code == 403 and PURPOSE not in other_team.text
    assert http.get(PATH, headers=human).json() == uploaded.json()


def test_client_transmits_exact_unicode_and_lost_upload_reconciles_original_purpose(m3, signing_key, jwk_client):
    artifacts, _, blobs = m3
    http, agent, human = signed_m3(artifacts, signing_key, jwk_client)
    client = ArtifactClient(http, A.team, lambda: agent["Authorization"].removeprefix("Bearer "))
    created = client.create("report", CreateArtifact(kind="skill", title="Review purpose", source_work="source-work"))
    metadata = VersionInput(filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(BODY).hexdigest(),
                            skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description=PURPOSE))
    original_put = blobs.put
    def lost(*args):
        original_put(*args)
        raise Problem(502, "SYNTHETIC_LOST_ACK", "offline accepted write with lost acknowledgement")
    with patch.object(blobs, "put", side_effect=lost) as writes:
        with pytest.raises(Problem):
            client.upload("report", V1, created.etag, metadata, BODY)
        pending = client.get("report")
        assert pending.payload["pending"]["skill_purpose"]["description"] == PURPOSE
        with pytest.raises(Problem):
            client.upload("report", V1, pending.etag, metadata, BODY)
        reconciled = http.post(PATH + "/versions/" + V1 + "/reconcile",
                              json={}, headers={**agent, "If-Match": pending.etag})
        assert reconciled.status_code == 200
        writes.assert_called_once()
    current = client.get("report")
    receipt = client.version(current, V1)
    assert receipt.skill_purpose.description == PURPOSE and receipt.approval is None
    assert client.upload("report", V1, current.etag, metadata, BODY).payload == current.payload
    assert decide(http, human, reconciled, receipt.model_dump(mode="json")).status_code == 200


@pytest.mark.parametrize("headers", [
    [("X-Artifact-Skill-Purpose", ""), ("X-Artifact-Skill-Purpose", "")],
    [("X-Artifact-Skill-Purpose", "!not-base64!")],
    [("X-Artifact-Skill-Purpose", "x" * 2049)],
])
def test_purpose_header_is_single_bounded_and_not_silently_ignored(m3, signing_key, jwk_client, headers):
    http, agent, _ = signed_m3(m3[0], signing_key, jwk_client)
    initial = http.post(PATH, headers=agent, json={"kind": "skill", "title": "Header test", "source_work": "source-work"})
    response = http.put(PATH + "/versions/" + V1, content=BODY, headers=[
        *agent.items(), ("If-Match", initial.headers["etag"]), ("Content-Type", "application/octet-stream"),
        ("X-Artifact-Filename", "guide.md"), ("X-Artifact-Media-Type", "text/markdown"),
        ("X-Artifact-SHA256", hashlib.sha256(BODY).hexdigest()), *headers,
    ])
    assert response.status_code == 422
    assert http.get(PATH, headers=agent).json() == initial.json()


def test_metadata_acknowledgement_cannot_substitute_different_purpose(m3, signing_key, jwk_client):
    from shared_context.m3_models import skill_metadata_sha256
    http, agent, _ = signed_m3(m3[0], signing_key, jwk_client)
    client = ArtifactClient(http, A.team, lambda: agent["Authorization"].removeprefix("Bearer "))
    initial = client.create("report", CreateArtifact(kind="skill", title="Original", source_work="source-work"))
    original_request = client._request
    def replaced_ack(method, path, **kwargs):
        headers, data = original_request(method, path, **kwargs)
        if method == "PUT":
            payload = json.loads(data)
            version = payload["versions"][-1]
            version["skill_purpose"]["description"] = "Unexpected acknowledgement purpose."
            version["metadata_sha256"] = skill_metadata_sha256(A.team, "report", version)
            data = json.dumps(payload).encode()
        return headers, data
    metadata = VersionInput(filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(BODY).hexdigest(),
                            skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description=PURPOSE))
    with patch.object(client, "_request", side_effect=replaced_ack):
        with pytest.raises(Problem) as refused:
            client.upload("report", V1, initial.etag, metadata, BODY)
    assert refused.value.body["error"]["code"] == "M3_RESPONSE_INVALID"
    assert client.get("report").payload["versions"][0]["skill_purpose"]["description"] == PURPOSE


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
        (TurnArchive(conversation_id="conversation", job_id="job", turn_number=1,
            archive=ArchiveUnavailable(error="SDK_ARCHIVE_UNAVAILABLE"), sdk_stop=SdkStop(pid=7, exit_code=0, graceful=True),
            prompt_assembly=assemble_prompt("fa-instruction-skills-v1").record, skill_catalog=catalog), 1658,
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


def test_same_body_and_version_id_cannot_transfer_purpose_approval_to_another_artifact(m3, signing_key, jwk_client):
    http, agent, human, uploaded, original = draft(m3, signing_key, jwk_client)
    assert decide(http, human, uploaded, original).status_code == 200
    other = "/teams/team-one/artifacts/other-guide"
    created = http.post(other, headers=agent, json={
        "kind": "skill", "title": "Other artifact", "source_work": "source-work",
    })
    copied = upload_purpose(http, agent, created.headers["etag"], path=other)
    assert copied.status_code == 200
    candidate = copied.json()["versions"][0]
    assert candidate["sha256"] == original["sha256"] and candidate["version_id"] == original["version_id"]
    assert candidate["metadata_sha256"] != original["metadata_sha256"]
    response = http.post(other + "/decisions", headers={**human, "If-Match": copied.headers["etag"]}, json={
        "decision_id": V1, "version_id": V1, "decision": "approve",
        "sha256": original["sha256"], "metadata_sha256": original["metadata_sha256"],
    })
    assert response.status_code == 409 and response.json()["error"]["code"] == "M3_APPROVAL_METADATA_MISMATCH"
    assert http.get(other, headers=human).json() == copied.json()
