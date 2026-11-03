"""ArtifactService version, download and policy-context contracts, called in process as production does."""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from conftest import A, B, C
from shared_context.errors import Problem
from shared_context.m3_models import MAX_ARTIFACT_BYTES, ApprovalInput, CreateArtifact, VersionInput
from test_m3_artifacts import HUMAN, V1, V2, m3


def create(service, kind="deliverable"):
    return service.create(A, A.team, "report", CreateArtifact(kind=kind, title="Report", source_work="source-work"))


def metadata(data=b"v1", **changes):
    return VersionInput(**{"filename": "report.txt", "media_type": "text/plain",
                           "sha256": hashlib.sha256(data).hexdigest(), **changes})


def upload(service, etag, data=b"v1", version_id=V1, principal=A, **changes):
    return service.upload(principal, A.team, "report", version_id, etag, metadata(data, **changes), data)


def status(call):
    with pytest.raises(Problem) as denied:
        call()
    return denied.value.status


def test_binary_version_download_and_etag_are_exact(m3):
    service, _, _ = m3
    first = upload(service, create(service).etag)
    upload(service, first.etag, b"v2", V2)
    version = service.version(HUMAN, A.team, "report", V1)
    selected, content = service.download(HUMAN, A.team, "report", V1, version.blob.etag)
    assert content == b"v1" and selected == version
    assert version.sha256 == hashlib.sha256(b"v1").hexdigest() and version.filename == "report.txt"
    assert status(lambda: service.download(HUMAN, A.team, "report", V1, None)) == 428
    assert status(lambda: service.download(HUMAN, A.team, "report", V1, first.etag)) == 412
    assert service.get(B, A.team, "report").payload["artifact_id"] == "report"
    assert status(lambda: service.get(C, A.team, "report")) == 403


@pytest.mark.parametrize("failure", ["version", "iam"])
def test_download_refuses_a_stale_version_or_a_removed_actor_before_reading_the_blob(m3, failure):
    service, _, blob = m3
    upload(service, create(service).etag)
    etag = service.version(A, A.team, "report", V1).blob.etag
    if failure == "version":
        etag = '"0xBAD"'
    else:
        service.context.auth.actors.pop(A.oid)
    with patch.object(blob, "read", wraps=blob.read) as read:
        assert status(lambda: service.download(A, A.team, "report", V1, etag)) == (412 if failure == "version" else 403)
    read.assert_not_called()


def test_policy_context_changes_only_after_verified_human_decision(m3):
    service, _, _ = m3
    first = upload(service, create(service, "sop").etag)
    assert service.policy_context(A, A.team, ("report",)) == [{"artifact_id": "report", "status": "not_approved"}]
    body = {"decision_id": V1, "version_id": V1, "sha256": first.payload["versions"][0]["sha256"], "decision": "approve"}
    with pytest.raises(Problem) as agent:
        service.decide(A, A.team, "report", first.etag, ApprovalInput(**body))
    assert agent.value.status == 403
    with pytest.raises(ValidationError):
        ApprovalInput(**body, approver_oid=HUMAN.oid)
    approved = service.decide(HUMAN, A.team, "report", first.etag, ApprovalInput(**body))
    assert service.effective(HUMAN, A.team, "report").version_id == V1
    assert service.policy_context(A, A.team, ("report",))[0]["text"] == "v1"
    upload(service, approved.etag, b"v2 is not approved", V2)
    assert service.policy_context(A, A.team, ("report",))[0]["text"] == "v1"


@pytest.mark.parametrize("change,expected", [
    ({"sha256": "0" * 64}, 422),
    ({"filename": "../escape"}, ValidationError),
    ({"media_type": "text/html"}, ValidationError),
    ({"etag": "*"}, 412),
])
def test_upload_rejects_invalid_contract_without_creating_versions(m3, change, expected):
    service, _, _ = m3
    initial = create(service)
    etag = change.pop("etag", initial.etag)
    if expected is ValidationError:
        with pytest.raises(ValidationError):
            upload(service, etag, **change)
    else:
        assert status(lambda: upload(service, etag, **change)) == expected
    current = service.get(A, A.team, "report").payload
    assert current["versions"] == [] and current["pending"] is None


def test_binary_limit_is_512k_not_generic_64k_and_never_unbounded(m3):
    service, _, _ = m3
    full = upload(service, create(service).etag, b"x" * MAX_ARTIFACT_BYTES)
    with pytest.raises(Problem) as too_large:
        upload(service, full.etag, b"x" * (MAX_ARTIFACT_BYTES + 1), V2)
    assert too_large.value.status == 413 and too_large.value.body["error"]["code"] == "M3_FILE_TOO_LARGE"
    assert len(service.get(A, A.team, "report").payload["versions"]) == 1
    assert status(lambda: upload(service, full.etag, b"not an agent", V2, principal=HUMAN)) == 403


def test_missing_configured_policy_is_an_explicit_error(m3):
    service, _, _ = m3
    assert status(lambda: service.policy_context(A, A.team, ("missing-policy",))) == 404


def test_another_team_cannot_read_this_teams_policy_context(m3):
    service, _, _ = m3
    upload(service, create(service, "sop").etag)
    with pytest.raises(Problem) as denied:
        service.policy_context(C, C.team, ("report",))
    assert denied.value.status == 403 and denied.value.body["error"]["code"] == "M3_TEAM_DENIED"
