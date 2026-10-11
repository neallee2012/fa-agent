"""Original M3 API/storage with a substituted identity dependency; no live upload."""

import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from conftest import A, B
from shared_context.errors import Problem
from shared_context.m3_client import ArtifactClient
from shared_context.m3_models import (
    ApprovalInput, CreateArtifact, SkillPurpose, SkillResourceReference, SkillResources, VersionInput,
    skill_metadata_sha256,
)
from shared_context.records import Record
from test_m3_api import client
from test_m3_artifacts import HUMAN, V1, V2, V3, m3


@pytest.fixture
def pending_upload(m3, monkeypatch, request):
    service, repository, store = m3
    http = client(service, A)
    consumer = ArtifactClient(http, A.team, lambda: "synthetic-agent-token")
    variant = getattr(request, "param", "deliverable")
    kind = "skill" if variant in {"skill-resources", "legacy-skill"} else variant
    resources = None
    if variant == "skill-resources":
        reference = consumer.create("reference", CreateArtifact(
            kind="deliverable", title="Reference data", source_work="source-work",
        ))
        data = b"Exact reference data.\n"
        digest = hashlib.sha256(data).hexdigest()
        consumer.upload("reference", V1, reference.etag, VersionInput(
            filename="reference.txt", media_type="text/plain", sha256=digest,
        ), data)
        resources = SkillResources(entries=[
            SkillResourceReference(name="reference.txt", artifact_id="reference", version_id=V1, sha256=digest),
        ])
    created = consumer.create("report", CreateArtifact(
        kind=kind, title="Versioned report", source_work="source-work",
    ))
    original = b"Original published bytes.\n"
    first = consumer.upload("report", V1, created.etag, VersionInput(
        filename="report.txt", media_type="text/plain", sha256=hashlib.sha256(original).hexdigest(),
    ), original)
    if kind != "deliverable":
        with client(service, HUMAN) as approver_http:
            approver = ArtifactClient(approver_http, A.team, lambda: "synthetic-human-token")
            first = approver.decide("report", first, ApprovalInput(
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
        consumer.upload("report", V2, first.etag, VersionInput(
            filename="revised.txt", media_type="text/plain", sha256=hashlib.sha256(content).hexdigest(),
            skill_purpose=(SkillPurpose(schema_version="fa-skill-purpose-v1", description="Describe the revised report.")
                           if kind == "skill" and variant != "legacy-skill" else None),
            skill_resources=resources,
        ), content)
    assert failure.value.body["error"]["code"] == "M3_UPLOAD_UNKNOWN"
    snapshot = consumer.get("report")
    assert snapshot.payload["pending"]["version_id"] == V2
    yield SimpleNamespace(service=service, repository=repository, store=store, consumer=consumer, http=http,
                          first=first, snapshot=snapshot, original=original, content=content, puts=puts)
    http.close()


def test_reconcile_finishes_only_the_original_pending_version_without_resending_bytes(pending_upload, monkeypatch):
    c = pending_upload
    previous = copy.deepcopy(c.snapshot.payload)
    stream, requests = c.http.stream, []

    def observe(method, path, **kwargs):
        requests.append((method, path, kwargs))
        return stream(method, path, **kwargs)

    monkeypatch.setattr(c.http, "stream", observe)
    result = c.consumer.reconcile("report", c.snapshot)
    assert len(requests) == 1
    method, path, options = requests[0]
    assert method == "POST" and path.endswith("/versions/" + V2 + "/reconcile")
    assert options["headers"]["If-Match"] == c.snapshot.etag
    assert options.get("content") is None and options.get("json") == {}
    assert c.puts == [True]
    assert c.snapshot.payload == previous
    assert result.payload["pending"] is None
    assert result.payload["versions"][:-1] == c.first.payload["versions"]
    assert result.payload["revision"] == c.snapshot.payload["revision"] + 1
    assert c.consumer.content("report", c.consumer.version(result, V1)) == c.original
    assert c.consumer.content("report", c.consumer.version(result, V2)) == c.content
    assert c.puts == [True]


@pytest.mark.parametrize("pending_upload", ["sop", "skill", "skill-resources"], indirect=True)
def test_reconcile_preserves_prior_approval_and_does_not_activate_a_new_policy_version(pending_upload):
    c = pending_upload
    result = c.consumer.reconcile("report", c.snapshot)
    assert result.payload["effective_version_id"] == V1
    assert result.payload["versions"][:-1] == c.first.payload["versions"]
    new = c.consumer.version(result, V2)
    assert new.approval is None
    assert (new.skill_purpose is not None) == (result.payload["kind"] == "skill")
    assert new.skill_purpose == (
        SkillPurpose.model_validate(c.snapshot.payload["pending"]["skill_purpose"])
        if result.payload["kind"] == "skill" else None
    )
    assert new.skill_resources == (
        SkillResources.model_validate(c.snapshot.payload["pending"]["skill_resources"])
        if "skill_resources" in c.snapshot.payload["pending"] else None
    )
    assert c.puts == [True]


@pytest.mark.parametrize("change, expected", [
    ("completed-snapshot", "M3_UPLOAD_NOT_PENDING"), ("wrong-artifact", "M3_RECONCILE_INVALID"),
    ("wrong-team", "M3_RECONCILE_INVALID"), ("wildcard-etag", "M3_RECONCILE_INVALID"),
    ("invalid-document", "M3_RECONCILE_INVALID"),
])
def test_invalid_or_nonpending_snapshot_is_rejected_before_any_request(pending_upload, monkeypatch, change, expected):
    c = pending_upload
    payload, etag = copy.deepcopy(c.snapshot.payload), c.snapshot.etag
    if change == "completed-snapshot":
        payload, etag = c.first.payload, c.first.etag
    elif change == "wrong-artifact":
        payload["artifact_id"] = "another-artifact"
    elif change == "wrong-team":
        payload["team"] = "another-team"
    elif change == "wildcard-etag":
        etag = "*"
    else:
        payload = {}

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid original snapshots must not make an HTTP request")

    monkeypatch.setattr(c.http, "stream", forbidden)
    with pytest.raises(Problem) as failure:
        c.consumer.reconcile("report", Record(payload, etag))
    assert failure.value.body["error"]["code"] == expected
    assert c.puts == [True]


@pytest.mark.parametrize("principal", [B, HUMAN])
def test_only_the_current_owning_agent_can_complete_the_pending_version(pending_upload, principal):
    c = pending_upload
    before = copy.deepcopy(c.repository.data)
    with client(c.service, principal) as http:
        other = ArtifactClient(http, A.team, lambda: "synthetic-other-token")
        with pytest.raises(Problem) as failure:
            other.reconcile("report", c.snapshot)
    assert failure.value.status == 403
    assert c.repository.data == before and c.puts == [True]


def test_stale_snapshot_does_not_rewrite_the_completed_version(pending_upload, monkeypatch):
    c = pending_upload
    c.service.reconcile(A, A.team, "report", V2, c.snapshot.etag)
    before = copy.deepcopy(c.repository.data)
    stream, calls = c.http.stream, []

    def observe(*args, **kwargs):
        calls.append(True)
        return stream(*args, **kwargs)

    monkeypatch.setattr(c.http, "stream", observe)
    with pytest.raises(Problem) as failure:
        c.consumer.reconcile("report", c.snapshot)
    assert failure.value.status == 412
    assert calls == [True] and c.puts == [True] and c.repository.data == before


def test_unavailable_blob_receipt_remains_pending_without_retrying_upload(pending_upload, monkeypatch):
    c = pending_upload
    before = copy.deepcopy(c.repository.data)
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(True)
        raise Problem(503, "M3_BLOB_UNKNOWN", "synthetic blob observation unavailable")

    monkeypatch.setattr(c.store, "stat", unavailable)
    with pytest.raises(Problem) as failure:
        c.consumer.reconcile("report", c.snapshot)
    assert failure.value.body["error"]["code"] == "M3_BLOB_UNKNOWN"
    assert calls == [True] and c.puts == [True] and c.repository.data == before


def test_lost_reconcile_response_never_repeats_the_post_or_upload(pending_upload, monkeypatch):
    c = pending_upload
    stream, calls = c.http.stream, []

    def lose_ack(method, path, **kwargs):
        if path.endswith("/reconcile"):
            calls.append(True)
            with stream(method, path, **kwargs) as response:
                assert response.status_code == 200
                response.read()
            raise httpx.ReadTimeout("synthetic reconcile response lost after metadata commit")
        return stream(method, path, **kwargs)

    monkeypatch.setattr(c.http, "stream", lose_ack)
    with pytest.raises(Problem) as failure:
        c.consumer.reconcile("report", c.snapshot)
    assert failure.value.body["error"]["code"] == "M3_REQUEST_UNKNOWN"
    assert calls == [True] and c.puts == [True]
    assert c.snapshot.payload["pending"]["version_id"] == V2
    observed = c.consumer.get("report")
    assert observed.payload["pending"] is None
    assert c.consumer.version(observed, V2).sha256 == hashlib.sha256(c.content).hexdigest()
    assert observed.payload["versions"][:-1] == c.first.payload["versions"]
    assert calls == [True]


@pytest.mark.parametrize("pending_upload, injected", [
    ("legacy-skill", "purpose"), ("skill", "resources"),
], indirect=["pending_upload"])
def test_acknowledgment_cannot_add_optional_metadata_absent_from_the_pending_version(
    pending_upload, monkeypatch, injected,
):
    c = pending_upload
    request = c.consumer._request

    def inject(method, path, **kwargs):
        headers, raw = request(method, path, **kwargs)
        if not path.endswith("/reconcile"):
            return headers, raw
        payload = json.loads(raw)
        version = payload["versions"][-1]
        if injected == "purpose":
            assert "skill_purpose" not in c.snapshot.payload["pending"]
            version["skill_purpose"] = SkillPurpose(
                schema_version="fa-skill-purpose-v1", description="Purpose that the original pending upload did not contain.",
            ).model_dump(mode="json")
        else:
            assert "skill_resources" not in c.snapshot.payload["pending"]
            version["skill_resources"] = SkillResources(entries=[
                SkillResourceReference(name="injected.txt", artifact_id="another-resource",
                                       version_id=V3, sha256="a" * 64),
            ]).model_dump(mode="json")
        version["metadata_sha256"] = skill_metadata_sha256(payload["team"], payload["artifact_id"], version)
        return headers, json.dumps(payload).encode()

    monkeypatch.setattr(c.consumer, "_request", inject)
    with pytest.raises(Problem) as failure:
        c.consumer.reconcile("report", c.snapshot)
    assert failure.value.body["error"]["code"] == "M3_RESPONSE_INVALID"
    assert c.puts == [True]
    current = c.consumer.get("report")
    assert current.payload["versions"][:-1] == c.first.payload["versions"]
    pending = c.snapshot.payload["pending"]
    actual = c.consumer.version(current, V2)
    assert actual.skill_purpose == (
        SkillPurpose.model_validate(pending["skill_purpose"]) if "skill_purpose" in pending else None
    )
    assert actual.skill_resources is None


@pytest.mark.parametrize("change", [
    "version", "filename", "digest", "size", "creator", "title", "revision", "older-version",
])
def test_mismatched_acknowledgment_is_not_presented_as_the_original_completed_upload(
    pending_upload, monkeypatch, change,
):
    c = pending_upload
    request = c.consumer._request

    def corrupted(method, path, **kwargs):
        headers, raw = request(method, path, **kwargs)
        if not path.endswith("/reconcile"):
            return headers, raw
        payload = json.loads(raw)
        version = payload["versions"][-1]
        if change == "version":
            version["version_id"] = V3
        elif change == "filename":
            version["filename"] = "another.txt"
        elif change == "digest":
            version["sha256"] = version["blob"]["sha256"] = "0" * 64
        elif change == "size":
            version["blob"]["bytes"] += 1
        elif change == "creator":
            version["created_by_oid"] = B.oid
        elif change == "title":
            payload["title"] = "An unrelated edit"
        elif change == "revision":
            payload["revision"] += 1
        else:
            payload["versions"][0]["filename"] = "rewritten-history.txt"
        return headers, json.dumps(payload).encode()

    monkeypatch.setattr(c.consumer, "_request", corrupted)
    with pytest.raises(Problem) as failure:
        c.consumer.reconcile("report", c.snapshot)
    assert failure.value.body["error"]["code"] == "M3_RESPONSE_INVALID"
    assert c.puts == [True]
    observed = c.consumer.get("report")
    assert observed.payload["versions"][:-1] == c.first.payload["versions"]
    assert c.snapshot.payload["pending"]["version_id"] == V2
