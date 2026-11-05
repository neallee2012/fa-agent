"""ArtifactService.reconcile of a lost-acknowledgement upload, called in process; no live upload."""

import copy
import hashlib
from types import SimpleNamespace

import pytest

from conftest import A, B
from shared_context.errors import Problem
from shared_context.m3_models import (
    ApprovalInput, CreateArtifact, PendingVersion, SkillPurpose, SkillResourceReference, SkillResources, VersionInput,
)
from test_m3_artifacts import HUMAN, V1, V2, m3


def upload(service, artifact, version_id, etag, metadata, data):
    return service.upload(A, A.team, artifact, version_id, etag, metadata, data)


@pytest.fixture
def pending_upload(m3, monkeypatch, request):
    service, repository, store = m3
    variant = getattr(request, "param", "deliverable")
    kind = "skill" if variant in {"skill-resources", "legacy-skill"} else variant
    resources = None
    if variant == "skill-resources":
        reference = service.create(A, A.team, "reference", CreateArtifact(
            kind="deliverable", title="Reference data", source_work="source-work",
        ))
        data = b"Exact reference data.\n"
        digest = hashlib.sha256(data).hexdigest()
        upload(service, "reference", V1, reference.etag, VersionInput(
            filename="reference.txt", media_type="text/plain", sha256=digest,
        ), data)
        resources = SkillResources(entries=[
            SkillResourceReference(name="reference.txt", artifact_id="reference", version_id=V1, sha256=digest),
        ])
    created = service.create(A, A.team, "report", CreateArtifact(
        kind=kind, title="Versioned report", source_work="source-work",
    ))
    original = b"Original published bytes.\n"
    first = upload(service, "report", V1, created.etag, VersionInput(
        filename="report.txt", media_type="text/plain", sha256=hashlib.sha256(original).hexdigest(),
    ), original)
    if kind != "deliverable":
        first = service.decide(HUMAN, A.team, "report", first.etag, ApprovalInput(
            decision_id=V1, version_id=V1, sha256=hashlib.sha256(original).hexdigest(), decision="approve",
        ))
    content = "第二版成果🙂\r\n保留原版本。\n".encode("utf-8")
    put, puts = store.put, []

    def lose_upload_ack(*args, **kwargs):
        puts.append(True)
        put(*args, **kwargs)
        raise Problem(503, "M3_UPLOAD_UNKNOWN", "synthetic upload acknowledgement loss")

    monkeypatch.setattr(store, "put", lose_upload_ack)
    with pytest.raises(Problem) as failure:
        upload(service, "report", V2, first.etag, VersionInput(
            filename="revised.txt", media_type="text/plain", sha256=hashlib.sha256(content).hexdigest(),
            skill_purpose=(SkillPurpose(schema_version="fa-skill-purpose-v1", description="Describe the revised report.")
                           if kind == "skill" and variant != "legacy-skill" else None),
            skill_resources=resources,
        ), content)
    assert failure.value.body["error"]["code"] == "M3_UPLOAD_UNKNOWN"
    snapshot = service.get(A, A.team, "report")
    assert snapshot.payload["pending"]["version_id"] == V2
    return SimpleNamespace(service=service, repository=repository, store=store,
                           first=first, snapshot=snapshot, original=original, content=content, puts=puts)


def version(record, version_id):
    return next(item for item in record.payload["versions"] if item["version_id"] == version_id)


def content(c, version_id):
    selected = c.service.version(A, A.team, "report", version_id)
    return c.service.download(A, A.team, "report", version_id, selected.blob.etag)[1]


def test_reconcile_finishes_only_the_original_pending_version_without_resending_bytes(pending_upload):
    c = pending_upload
    previous = copy.deepcopy(c.snapshot.payload)
    result = c.service.reconcile(A, A.team, "report", V2, c.snapshot.etag)
    assert c.puts == [True]
    assert c.snapshot.payload == previous
    assert result.payload["pending"] is None
    assert result.payload["versions"][:-1] == c.first.payload["versions"]
    assert result.payload["revision"] == c.snapshot.payload["revision"] + 1
    finished, pending = result.payload["versions"][-1], previous["pending"]
    assert finished["number"] == len(previous["versions"]) + 1 and finished["approval"] is None
    assert finished["blob"]["bytes"] == pending["bytes"]
    assert all(finished.get(name) == pending.get(name) for name in PendingVersion.model_fields.keys() - {"bytes", "status"})
    assert content(c, V1) == c.original
    assert content(c, V2) == c.content
    assert c.puts == [True]


@pytest.mark.parametrize("pending_upload", ["sop", "skill", "skill-resources"], indirect=True)
def test_reconcile_preserves_prior_approval_and_does_not_activate_a_new_policy_version(pending_upload):
    c = pending_upload
    result = c.service.reconcile(A, A.team, "report", V2, c.snapshot.etag)
    assert result.payload["effective_version_id"] == V1
    assert result.payload["versions"][:-1] == c.first.payload["versions"]
    new = version(result, V2)
    assert new["approval"] is None
    assert (new.get("skill_purpose") is not None) == (result.payload["kind"] == "skill")
    assert new.get("skill_purpose") == (
        c.snapshot.payload["pending"]["skill_purpose"] if result.payload["kind"] == "skill" else None
    )
    assert new.get("skill_resources") == c.snapshot.payload["pending"].get("skill_resources")
    assert c.puts == [True]


@pytest.mark.parametrize("principal", [B, HUMAN])
def test_only_the_current_owning_agent_can_complete_the_pending_version(pending_upload, principal):
    c = pending_upload
    before = copy.deepcopy(c.repository.data)
    with pytest.raises(Problem) as failure:
        c.service.reconcile(principal, A.team, "report", V2, c.snapshot.etag)
    assert failure.value.status == 403
    assert c.repository.data == before and c.puts == [True]


def test_stale_snapshot_does_not_rewrite_the_completed_version(pending_upload):
    c = pending_upload
    c.service.reconcile(A, A.team, "report", V2, c.snapshot.etag)
    before = copy.deepcopy(c.repository.data)
    with pytest.raises(Problem) as failure:
        c.service.reconcile(A, A.team, "report", V2, c.snapshot.etag)
    assert failure.value.status == 412
    assert c.puts == [True] and c.repository.data == before


def test_unavailable_blob_receipt_remains_pending_without_retrying_upload(pending_upload, monkeypatch):
    c = pending_upload
    before = copy.deepcopy(c.repository.data)
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(True)
        raise Problem(503, "M3_BLOB_UNKNOWN", "synthetic blob observation unavailable")

    monkeypatch.setattr(c.store, "stat", unavailable)
    with pytest.raises(Problem) as failure:
        c.service.reconcile(A, A.team, "report", V2, c.snapshot.etag)
    assert failure.value.body["error"]["code"] == "M3_BLOB_UNKNOWN"
    assert calls == [True] and c.puts == [True] and c.repository.data == before


@pytest.mark.parametrize("finished", [False, True], ids=["other-version", "nothing-pending"])
def test_only_the_pending_version_id_can_be_reconciled(pending_upload, monkeypatch, finished):
    c = pending_upload
    asked, etag = V1, c.snapshot.etag
    if finished:
        asked, etag = V2, c.service.reconcile(A, A.team, "report", V2, etag).etag
    before = copy.deepcopy(c.repository.data)
    stats = []
    monkeypatch.setattr(c.store, "stat", lambda *args, **kwargs: stats.append(args))
    with pytest.raises(Problem) as failure:
        c.service.reconcile(A, A.team, "report", asked, etag)
    assert failure.value.status == 409
    assert failure.value.body["error"]["code"] == "M3_UPLOAD_NOT_PENDING"
    assert stats == [] and c.repository.data == before and c.puts == [True]
