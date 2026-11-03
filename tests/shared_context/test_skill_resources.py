"""Approved resource manifests reuse M3 versions and current reads; no filesystem or SDK."""

import copy
import hashlib
import json
from unittest.mock import patch

import pytest

from conftest import A, C
from shared_context.errors import Problem
from shared_context.models import CreateWork
from shared_context.m3_models import (
    ArtifactDocument, CreateArtifact, SkillDeactivationInput, SkillResourceReference, SkillResources, VersionInput,
)
from test_m3_artifacts import V1, V2, m3
from test_skill_purpose import create_skill, decide, human, upload_purpose


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


def propose(artifacts, reference, *, version=V1, artifact="resource-guide", create=True):
    before = (create_skill(artifacts, artifact, title="Resource guide") if create
              else artifacts.get(A, A.team, artifact))
    return upload_purpose(artifacts, before.etag, version=version, name=artifact,
                          resources=SkillResources(entries=[reference]))


def approve(artifacts, uploaded, artifact="resource-guide"):
    version = uploaded.payload["versions"][-1]
    return decide(artifacts, uploaded, version, name=artifact), version


def deactivate(artifacts, approved, version):
    return artifacts.deactivate_skill(human(artifacts), A.team, "resource-guide", approved.etag, SkillDeactivationInput(
        schema_version="fa-skill-deactivation-v1", decision_id=V2, version_id=V1,
        sha256=version["sha256"], metadata_sha256=version["metadata_sha256"],
    ))


def read(artifacts, principal=A, name="reference.json"):
    return artifacts.skill_resource(principal, A.team, "resource-guide", V1, name)


def status(call):
    with pytest.raises(Problem) as denied:
        call()
    return denied.value.status


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


def test_manifest_approval_read_and_deactivation_keep_exact_resource(m3):
    artifacts, _, _ = m3
    reference = resource_fixture(artifacts)
    uploaded = propose(artifacts, reference)
    document = ArtifactDocument.model_validate(artifacts.get(A, A.team, "resource-guide").payload)
    assert ArtifactDocument.model_validate(json.loads(document.model_dump_json())) == document
    assert status(lambda: read(artifacts)) == 404
    approved, version = approve(artifacts, uploaded)
    before = copy.deepcopy(artifacts.repository.data)
    result = read(artifacts)
    assert result.text == '{"value":42}\n'
    assert result.resource == reference
    assert result.skill_metadata_sha256 == version["metadata_sha256"]
    assert artifacts.repository.data == before
    assert status(lambda: read(artifacts, name="unlisted.json")) == 403
    deactivate(artifacts, approved, version)
    assert status(lambda: read(artifacts)) == 404


@pytest.mark.parametrize("change", ["hash", "missing-version"])
def test_invalid_reference_is_refused_before_upload_reservation(m3, change):
    artifacts, _, blobs = m3
    reference = resource_fixture(artifacts)
    reference = reference.model_copy(update={"sha256": "f" * 64} if change == "hash" else {"version_id": V2})
    with patch.object(blobs, "put", side_effect=AssertionError("Invalid manifest must not upload")):
        assert status(lambda: propose(artifacts, reference)) == 403
    assert artifacts.get(A, A.team, "resource-guide").payload["pending"] is None


def test_manifest_changes_cannot_reuse_original_version_or_approval(m3):
    artifacts, _, _ = m3
    first = resource_fixture(artifacts)
    second = resource_fixture(artifacts, name="other-data", body=b'{"value":99}\n')
    _, original = approve(artifacts, propose(artifacts, first))
    assert status(lambda: propose(artifacts, second, create=False)) == 409
    next_version = propose(artifacts, second, create=False, version=V2)
    current = next_version.payload["versions"][-1]
    assert current["sha256"] == original["sha256"]
    assert current["metadata_sha256"] != original["metadata_sha256"]
    assert status(lambda: decide(artifacts, next_version, current, name="resource-guide",
                                 metadata_sha256=original["metadata_sha256"])) == 409


def test_manifest_version_keeps_its_references_and_reads_only_the_named_resource(m3):
    artifacts, _, _ = m3
    reference = resource_fixture(artifacts)
    uploaded = propose(artifacts, reference)
    version = artifacts.version(A, A.team, "resource-guide", V1)
    assert version.skill_resources == SkillResources(entries=[reference])
    approve(artifacts, uploaded)
    assert read(artifacts).text == '{"value":42}\n'


@pytest.mark.parametrize("media_type,body", [
    ("application/octet-stream", b"binary"),
    ("text/plain", b"x" * 8193),
])
def test_unsupported_resource_metadata_is_refused_before_skill_upload(m3, media_type, body):
    artifacts, _, blobs = m3
    reference = resource_fixture(artifacts, body=body, media_type=media_type)
    with patch.object(blobs, "put", side_effect=AssertionError("No upload for an unsupported resource")):
        assert status(lambda: propose(artifacts, reference)) == 403


def test_resource_from_a_different_source_work_is_refused(m3):
    artifacts, _, _ = m3
    artifacts.context.create(A, A.team, "other-work", CreateWork(goal="Other source work"))
    reference = resource_fixture(artifacts, source_work="other-work")
    assert status(lambda: propose(artifacts, reference)) == 403


@pytest.mark.parametrize("body", [b"\xff\xfe", b"text\x00hidden"])
def test_malformed_text_cannot_receive_manifest_approval(m3, body):
    artifacts, _, _ = m3
    reference = resource_fixture(artifacts, body=body, media_type="text/plain")
    uploaded = propose(artifacts, reference)
    with pytest.raises(Problem) as refused:
        approve(artifacts, uploaded)
    assert refused.value.status == 422 and refused.value.body["error"]["code"] == "M3_SKILL_RESOURCE_FORMAT"
    current = artifacts.get(A, A.team, "resource-guide").payload
    assert current["effective_version_id"] is None and current["versions"][-1]["approval"] is None


def test_resource_unicode_data_preserves_bom_spaces_and_joiners(m3):
    artifacts, _, _ = m3
    body = "\ufeff中文\u3000資料\u00a0與\u200d文字\n".encode("utf-8")
    reference = resource_fixture(artifacts, body=body, media_type="text/markdown")
    approve(artifacts, propose(artifacts, reference))
    assert read(artifacts).text.encode("utf-8") == body


def test_resource_reader_rechecks_skill_after_blob_read_and_current_team_access(m3):
    artifacts, _, blobs = m3
    reference = resource_fixture(artifacts)
    approved, version = approve(artifacts, propose(artifacts, reference))
    original = blobs.read

    def changed(*args):
        result = original(*args)
        if args[1] == reference.artifact_id:
            deactivate(artifacts, approved, version)
        return result

    with patch.object(blobs, "read", side_effect=changed):
        with pytest.raises(Problem):
            read(artifacts)
    assert status(lambda: read(artifacts, C)) == 403


def test_resource_integrity_failure_has_no_cached_or_alternative_body(m3):
    artifacts, _, blobs = m3
    reference = resource_fixture(artifacts)
    approve(artifacts, propose(artifacts, reference))
    before = copy.deepcopy(artifacts.repository.data)
    with patch.object(blobs, "read", return_value=b"not the approved resource"):
        with pytest.raises(Problem) as broken:
            read(artifacts)
    assert broken.value.status == 502 and broken.value.body["error"]["code"] == "M3_FILE_INTEGRITY"
    assert artifacts.repository.data == before


@pytest.mark.parametrize("name", ["../secret", "C:\\secret", "/absolute", "https://external.invalid", "hidden/name"])
def test_resource_alias_is_not_a_path_or_network_target(name):
    with pytest.raises(ValueError):
        SkillResourceReference(name=name, artifact_id="data", version_id=V1, sha256="a" * 64)
