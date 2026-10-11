"""Approved resource manifests reuse M3 versions and current reads; no filesystem or SDK."""

import copy
import hashlib
import json
from unittest.mock import patch

import pytest

from conftest import A, C
from shared_context.errors import Problem
from shared_context.m3_client import ArtifactClient
from shared_context.models import CreateWork
from shared_context.m3_models import (
    ArtifactDocument, CreateArtifact, SkillPurpose, SkillResourceReference, SkillResources, VersionInput,
)
from test_m3_artifacts import V1, V2, m3
from test_m3_auth import jwk_client, signing_key
from test_skill_purpose import BODY, PURPOSE, signed_m3


def resource_fixture(artifacts, *, name="reference-data", body=b'{"value":42}\n', source_work="source-work",
                     media_type="application/json"):
    created = artifacts.create(A, A.team, name, CreateArtifact(
        kind="deliverable", title="Reference data", source_work=source_work,
    ))
    artifacts.upload(A, A.team, name, V1, created.etag, VersionInput(
        filename="reference.json", media_type=media_type, sha256=hashlib.sha256(body).hexdigest(),
    ), body)
    return SkillResourceReference(name="reference.json", artifact_id=name, version_id=V1,
                                  sha256=hashlib.sha256(body).hexdigest())


def propose(http, agent, reference, *, version=V1, artifact="resource-guide", create=True):
    path = "/teams/team-one/artifacts/" + artifact
    before = (http.post(path, headers=agent, json={
        "kind": "skill", "title": "Resource guide", "source_work": "source-work",
    }) if create else http.get(path, headers=agent))
    assert before.status_code in {200, 201}, before.text
    response = http.put(path + "/versions/" + version, content=BODY, headers={
        **agent, "If-Match": before.headers["etag"], "Content-Type": "application/octet-stream",
        "X-Artifact-Filename": "guide.md", "X-Artifact-Media-Type": "text/markdown",
        "X-Artifact-SHA256": hashlib.sha256(BODY).hexdigest(),
        "X-Artifact-Skill-Purpose": SkillPurpose(schema_version="fa-skill-purpose-v1", description=PURPOSE).header(),
        "X-Artifact-Skill-Resources": SkillResources(entries=[reference]).header(),
    })
    return path, response


def approve(http, human, path, uploaded):
    version = uploaded.json()["versions"][-1]
    response = http.post(path + "/decisions", headers={**human, "If-Match": uploaded.headers["etag"]}, json={
        "decision_id": version["version_id"], "version_id": version["version_id"], "decision": "approve",
        "sha256": version["sha256"], "metadata_sha256": version["metadata_sha256"],
    })
    assert response.status_code == 200, response.text
    return response, version


def test_manifest_header_roundtrip_preserves_typed_references_and_bytes():
    manifest = SkillResources(entries=[SkillResourceReference(
        name="reference.json", artifact_id="reference-data", version_id=V1, sha256="a" * 64,
    )])
    assert SkillResources.from_header(manifest.header()) == manifest
    assert SkillResources.from_header(manifest.header()).header() == manifest.header()


def test_manifest_requires_purpose_and_unique_ordered_resource_aliases():
    reference = SkillResourceReference(name="reference.json", artifact_id="reference-data",
                                       version_id=V1, sha256="a" * 64)
    with pytest.raises(ValueError):
        SkillResources(entries=[reference, reference])
    with pytest.raises(ValueError):
        SkillResources(entries=[reference, reference.model_copy(update={"name": "earlier.json"})])
    with pytest.raises(ValueError):
        VersionInput(filename="guide.md", media_type="text/markdown", sha256="a" * 64,
                     skill_resources=SkillResources(entries=[reference]))
    legacy = VersionInput(filename="guide.md", media_type="text/markdown", sha256="a" * 64)
    assert "skill_resources" not in legacy.model_dump_json()


def test_signed_manifest_approval_read_and_deactivation_keep_exact_resource(m3, signing_key, jwk_client):
    artifacts, _, _ = m3
    reference = resource_fixture(artifacts)
    http, agent, human = signed_m3(artifacts, signing_key, jwk_client)
    with http:
        path, uploaded = propose(http, agent, reference)
        assert uploaded.status_code == 200, uploaded.text
        document = ArtifactDocument.model_validate(artifacts.get(A, A.team, "resource-guide").payload)
        assert ArtifactDocument.model_validate(json.loads(document.model_dump_json())) == document
        resource_path = path + "/versions/" + V1 + "/resources/reference.json"
        assert http.get(resource_path, headers=agent).status_code == 404
        approved, version = approve(http, human, path, uploaded)
        before = copy.deepcopy(artifacts.repository.data)
        response = http.get(resource_path, headers=agent)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["text"] == '{"value":42}\n'
        assert response.json()["resource"] == reference.model_dump(mode="json")
        assert response.json()["skill_metadata_sha256"] == version["metadata_sha256"]
        assert artifacts.repository.data == before
        denied = http.get(path + "/versions/" + V1 + "/resources/unlisted.json", headers=agent)
        assert denied.status_code == 403
        deactivated = http.post(path + "/deactivations", headers={**human, "If-Match": approved.headers["etag"]}, json={
            "schema_version": "fa-skill-deactivation-v1", "decision_id": V2, "version_id": V1,
            "sha256": version["sha256"], "metadata_sha256": version["metadata_sha256"],
        })
        assert deactivated.status_code == 200, deactivated.text
        assert http.get(resource_path, headers=agent).status_code == 404


@pytest.mark.parametrize("change", ["hash", "missing-version"])
def test_invalid_reference_is_refused_before_upload_reservation(m3, signing_key, jwk_client, change):
    artifacts, _, blobs = m3
    reference = resource_fixture(artifacts)
    reference = reference.model_copy(update={"sha256": "f" * 64} if change == "hash" else {"version_id": V2})
    http, agent, _ = signed_m3(artifacts, signing_key, jwk_client)
    with http, patch.object(blobs, "put", side_effect=AssertionError("Invalid manifest must not upload")):
        _, response = propose(http, agent, reference)
    assert response.status_code == 403
    assert artifacts.get(A, A.team, "resource-guide").payload["pending"] is None


def test_manifest_changes_cannot_reuse_original_version_or_approval(m3, signing_key, jwk_client):
    artifacts, _, _ = m3
    first = resource_fixture(artifacts)
    second = resource_fixture(artifacts, name="other-data", body=b'{"value":99}\n')
    http, agent, human = signed_m3(artifacts, signing_key, jwk_client)
    with http:
        path, uploaded = propose(http, agent, first)
        assert uploaded.status_code == 200, uploaded.text
        _, original = approve(http, human, path, uploaded)
        _, reused = propose(http, agent, second, create=False)
        assert reused.status_code == 409
        _, next_version = propose(http, agent, second, create=False, version=V2)
        assert next_version.status_code == 200, next_version.text
        current = next_version.json()["versions"][-1]
        assert current["sha256"] == original["sha256"]
        assert current["metadata_sha256"] != original["metadata_sha256"]
        invalid = http.post(path + "/decisions", headers={**human, "If-Match": next_version.headers["etag"]}, json={
            "decision_id": V2, "version_id": V2, "decision": "approve",
            "sha256": current["sha256"], "metadata_sha256": original["metadata_sha256"],
        })
        assert invalid.status_code == 409


def test_client_transports_manifest_and_reads_only_the_named_resource(m3, signing_key, jwk_client):
    artifacts, _, _ = m3
    reference = resource_fixture(artifacts)
    http, agent, human = signed_m3(artifacts, signing_key, jwk_client)
    with http:
        client = ArtifactClient(http, A.team, lambda: agent["Authorization"].removeprefix("Bearer "))
        created = client.create("client-resource-guide", CreateArtifact(
            kind="skill", title="Resource guide", source_work="source-work",
        ))
        metadata = VersionInput(
            filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(BODY).hexdigest(),
            skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description=PURPOSE),
            skill_resources=SkillResources(entries=[reference]),
        )
        uploaded = client.upload("client-resource-guide", V1, created.etag, metadata, BODY)
        version = client.version(uploaded, V1)
        assert version.skill_resources == metadata.skill_resources
        approved = http.post("/teams/team-one/artifacts/client-resource-guide/decisions", headers={
            **human, "If-Match": uploaded.etag,
        }, json={"decision_id": V1, "version_id": V1, "decision": "approve",
                 "sha256": version.sha256, "metadata_sha256": version.metadata_sha256})
        assert approved.status_code == 200, approved.text
        assert client.skill_resource("client-resource-guide", V1, "reference.json").text == '{"value":42}\n'


@pytest.mark.parametrize("media_type,body", [
    ("application/octet-stream", b"binary"),
    ("text/plain", b"x" * 8193),
])
def test_unsupported_resource_metadata_is_refused_before_skill_upload(
    m3, signing_key, jwk_client, media_type, body,
):
    artifacts, _, blobs = m3
    reference = resource_fixture(artifacts, body=body, media_type=media_type)
    http, agent, _ = signed_m3(artifacts, signing_key, jwk_client)
    with http, patch.object(blobs, "put", side_effect=AssertionError("No upload for an unsupported resource")):
        _, response = propose(http, agent, reference)
        assert response.status_code == 403


def test_resource_from_a_different_source_work_is_refused(m3, signing_key, jwk_client):
    artifacts, _, _ = m3
    artifacts.context.create(A, A.team, "other-work", CreateWork(goal="Other source work"))
    reference = resource_fixture(artifacts, source_work="other-work")
    http, agent, _ = signed_m3(artifacts, signing_key, jwk_client)
    with http:
        _, response = propose(http, agent, reference)
        assert response.status_code == 403


@pytest.mark.parametrize("body", [b"\xff\xfe", b"text\x00hidden"])
def test_malformed_text_cannot_receive_manifest_approval(m3, signing_key, jwk_client, body):
    artifacts, _, _ = m3
    reference = resource_fixture(artifacts, body=body, media_type="text/plain")
    http, agent, human = signed_m3(artifacts, signing_key, jwk_client)
    with http:
        path, uploaded = propose(http, agent, reference)
        assert uploaded.status_code == 200
        version = uploaded.json()["versions"][-1]
        response = http.post(path + "/decisions", headers={**human, "If-Match": uploaded.headers["etag"]}, json={
            "decision_id": V1, "version_id": V1, "decision": "approve",
            "sha256": version["sha256"], "metadata_sha256": version["metadata_sha256"],
        })
        assert response.status_code == 422 and response.json()["error"]["code"] == "M3_SKILL_RESOURCE_FORMAT"
        current = artifacts.get(A, A.team, "resource-guide").payload
        assert current["effective_version_id"] is None and current["versions"][-1]["approval"] is None


def test_resource_unicode_data_preserves_bom_spaces_and_joiners(m3, signing_key, jwk_client):
    artifacts, _, _ = m3
    body = "\ufeff中文\u3000資料\u00a0與\u200d文字\n".encode("utf-8")
    reference = resource_fixture(artifacts, body=body, media_type="text/markdown")
    http, agent, human = signed_m3(artifacts, signing_key, jwk_client)
    with http:
        path, uploaded = propose(http, agent, reference)
        assert uploaded.status_code == 200, uploaded.text
        approve(http, human, path, uploaded)
        response = http.get(path + "/versions/" + V1 + "/resources/reference.json", headers=agent)
        assert response.status_code == 200, response.text
        assert response.json()["text"].encode("utf-8") == body


def test_resource_reader_rechecks_skill_after_blob_read_and_current_team_access(m3, signing_key, jwk_client):
    artifacts, _, blobs = m3
    reference = resource_fixture(artifacts)
    http, agent, human = signed_m3(artifacts, signing_key, jwk_client)
    with http:
        path, uploaded = propose(http, agent, reference)
        approved, version = approve(http, human, path, uploaded)
        original = blobs.read
        def changed(*args):
            result = original(*args)
            if args[1] == reference.artifact_id:
                deactivated = http.post(path + "/deactivations", headers={
                    **human, "If-Match": approved.headers["etag"],
                }, json={"schema_version": "fa-skill-deactivation-v1", "decision_id": V2,
                         "version_id": V1, "sha256": version["sha256"],
                         "metadata_sha256": version["metadata_sha256"]})
                assert deactivated.status_code == 200
            return result
        with patch.object(blobs, "read", side_effect=changed):
            with pytest.raises(Problem):
                artifacts.skill_resource(A, A.team, "resource-guide", V1, "reference.json")
        with pytest.raises(Problem) as denied:
            artifacts.skill_resource(C, A.team, "resource-guide", V1, "reference.json")
        assert denied.value.status == 403


def test_resource_integrity_failure_has_no_cached_or_alternative_body(m3, signing_key, jwk_client):
    artifacts, _, blobs = m3
    reference = resource_fixture(artifacts)
    http, agent, human = signed_m3(artifacts, signing_key, jwk_client)
    with http:
        path, uploaded = propose(http, agent, reference)
        approve(http, human, path, uploaded)
        before = copy.deepcopy(artifacts.repository.data)
        with patch.object(blobs, "read", return_value=b"not the approved resource"):
            response = http.get(path + "/versions/" + V1 + "/resources/reference.json", headers=agent)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "M3_FILE_INTEGRITY"
        assert artifacts.repository.data == before


@pytest.mark.parametrize("name", ["../secret", "C:\\secret", "/absolute", "https://external.invalid", "hidden/name"])
def test_resource_alias_is_not_a_path_or_network_target(name):
    with pytest.raises(ValueError):
        SkillResourceReference(name=name, artifact_id="data", version_id=V1, sha256="a" * 64)
