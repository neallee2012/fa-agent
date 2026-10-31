from __future__ import annotations

import asyncio
import hashlib

import pytest
from fastapi.testclient import TestClient

from conftest import A, B, C
from legacy_m2_m4.api import create_app
from shared_context.errors import Problem
from shared_context.m3_models import MAX_ARTIFACT_BYTES
from shared_context.models import CreateWork
from test_m3_artifacts import HUMAN, V1, V2, m3

PATH = "/teams/team-one/artifacts/report"


def client(service, principal=A, policies=()):
    return TestClient(create_app(
        service.context, lambda: A, artifact_service=service,
        verified_artifact_identity_dependency=lambda: principal, artifact_policy_ids=policies,
    ))


def create(http, kind="deliverable"):
    response = http.post(PATH, json={"kind": kind, "title": "Report", "source_work": "source-work"})
    assert response.status_code == 201, response.text
    return response.json()


def upload(http, etag, data=b"v1", version_id=V1, headers=None):
    return http.put(PATH + "/versions/" + version_id, content=data, headers={
        "Content-Type": "application/octet-stream", "X-Artifact-Filename": "report.txt",
        "X-Artifact-Media-Type": "text/plain", "X-Artifact-SHA256": hashlib.sha256(data).hexdigest(),
        "If-Match": etag, **(headers or {}),
    })


def test_http_binary_version_download_and_etag_are_exact(m3):
    service, _, _ = m3
    http = client(service)
    initial = create(http)
    first = upload(http, initial["etag"])
    assert first.status_code == 200
    assert first.headers["x-should-retry"] == "false"
    assert first.headers["etag"] == first.json()["etag"]
    second = upload(http, first.json()["etag"], b"v2", V2)
    assert second.status_code == 200
    human = client(service, HUMAN)
    version = human.get(PATH + "/versions/" + V1).json()
    content = human.get(PATH + "/versions/" + V1 + "/content", headers={"If-Match": version["blob"]["etag"]})
    assert content.content == b"v1"
    assert content.headers["x-artifact-sha256"] == hashlib.sha256(b"v1").hexdigest()
    assert content.headers["etag"] == version["blob"]["etag"]
    assert content.headers["content-disposition"] == 'attachment; filename="report.txt"'
    assert content.headers["cache-control"] == "no-store"
    assert content.headers["x-content-type-options"] == "nosniff"
    assert human.get(PATH + "/versions/" + V1 + "/content").status_code == 428
    assert human.get(PATH + "/versions/" + V1 + "/content", headers={"If-Match": first.json()["etag"]}).status_code == 412
    assert client(service, B).get(PATH).status_code == 200
    assert client(service, C).get(PATH).status_code == 403


def test_http_policy_context_changes_only_after_verified_human_decision(m3):
    service, _, _ = m3
    http = client(service, policies=("report",))
    initial = create(http, "sop")
    first = upload(http, initial["etag"]).json()
    context_path = "/teams/team-one/works/source-work/context"
    assert http.get(context_path).json()["approved_policy_context"] == [{"artifact_id": "report", "status": "not_approved"}]
    body = {"decision_id": V1, "version_id": V1, "sha256": first["versions"][0]["sha256"], "decision": "approve"}
    headers = {"If-Match": first["etag"], "X-Actor": "admin", "X-Approver-Oid": HUMAN.oid}
    assert http.post(PATH + "/decisions", headers=headers, json=body).status_code == 403
    human = client(service, HUMAN)
    forged = human.post(PATH + "/decisions", headers=headers, json={**body, "approver_oid": HUMAN.oid})
    assert forged.status_code == 422
    approved = human.post(PATH + "/decisions", headers=headers, json=body)
    assert approved.status_code == 200, approved.text
    assert human.get(PATH + "/effective").json()["version_id"] == V1
    assert http.get(context_path).json()["approved_policy_context"][0]["text"] == "v1"
    upload(http, approved.json()["etag"], b"v2 is not approved", V2)
    assert http.get(context_path).json()["approved_policy_context"][0]["text"] == "v1"
    assert "approved_policy_context" not in client(service).get(context_path).json()


@pytest.mark.parametrize("headers,expected", [
    ({"X-Artifact-SHA256": "0" * 64}, 422),
    ({"X-Artifact-Filename": "../escape"}, 422),
    ({"X-Artifact-Media-Type": "text/html"}, 422),
    ({"Content-Type": "application/json"}, 415),
    ({"Content-Encoding": "gzip"}, 415),
    ({"If-Match": "*"}, 412),
])
def test_upload_rejects_invalid_contract_without_creating_versions(m3, headers, expected):
    service, _, _ = m3
    http = client(service)
    initial = create(http)
    response = upload(http, initial["etag"], headers=headers)
    assert response.status_code == expected
    assert http.get(PATH).json()["versions"] == []
    assert http.get(PATH).json()["pending"] is None


def test_binary_limit_is_512k_not_generic_64k_and_never_unbounded(m3):
    service, _, _ = m3
    http = client(service)
    initial = create(http)
    full = upload(http, initial["etag"], b"x" * MAX_ARTIFACT_BYTES)
    assert full.status_code == 200
    too_large = upload(http, full.json()["etag"], b"x" * (MAX_ARTIFACT_BYTES + 1), V2)
    assert too_large.status_code == 413
    assert len(http.get(PATH).json()["versions"]) == 1
    assert client(service, HUMAN).put(PATH + "/versions/" + V2, content=b"not an agent").status_code == 403


def test_large_upload_authenticates_before_receiving_any_bytes(m3):
    service, _, _ = m3

    def deny():
        raise Problem(401, "INVALID_TOKEN", "synthetic denied identity")

    app = create_app(service.context, deny, artifact_service=service, verified_artifact_identity_dependency=deny)

    async def execute():
        messages = []

        async def receive():
            raise AssertionError("unauthenticated artifact body was consumed")

        async def send(message):
            messages.append(message)

        await app({
            "type": "http", "http_version": "1.1", "method": "PUT", "scheme": "https",
            "path": PATH + "/versions/" + V1, "raw_path": (PATH + "/versions/" + V1).encode(),
            "root_path": "", "query_string": b"", "server": ("testserver", 443), "client": ("127.0.0.1", 1),
            "headers": [(b"content-length", b"999999999"), (b"content-type", b"application/octet-stream")],
        }, receive, send)
        assert next(message for message in messages if message["type"] == "http.response.start")["status"] == 401

    asyncio.run(execute())


def test_missing_configured_policy_is_an_explicit_error(m3):
    service, _, _ = m3
    response = client(service, policies=("missing-policy",)).get("/teams/team-one/works/source-work/context")
    assert response.status_code == 404
    assert "approved_policy_context" not in response.json()


def test_m3_policy_configuration_does_not_change_another_teams_legacy_context(m3):
    service, _, _ = m3
    service.context.create(C, C.team, "peer-work", CreateWork(goal="Another team's legacy work"))
    peer = TestClient(create_app(
        service.context, lambda: C, artifact_service=service,
        verified_artifact_identity_dependency=lambda: C, artifact_policy_ids=("report",),
    ))
    response = peer.get("/teams/" + C.team + "/works/peer-work/context")
    assert response.status_code == 200
    assert "approved_policy_context" not in response.json()
