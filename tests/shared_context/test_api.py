from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from conftest import A, B, C, InMemoryRepository
from legacy_m2_m4.api import create_app
from shared_context.auth import Actor
from shared_context.errors import Problem
from shared_context.models import UpdateWork

WORK = "/teams/team-one/works/task-1"


def test_shared_read_and_single_editor(system, work):
    a, b = system[3][A.actor], system[3][B.actor]
    assert b.get(WORK).json() == work
    denied = b.patch(WORK, headers={"If-Match": work["etag"]}, json={"progress": "overwrite"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "NOT_OWNER"
    updated = a.patch(WORK, headers={"If-Match": work["etag"]}, json={"artifact_text": "v2", "progress": "done"})
    assert updated.status_code == 200
    data = updated.json()
    assert data["revision"] == 2
    assert data["artifact"] == {"text": "v2", "sha256": hashlib.sha256(b"v2").hexdigest(), "version": 2}
    assert updated.headers["etag"] == data["etag"] != work["etag"]
    assert b.get(WORK).json() == data


def test_create_is_not_upsert(system, work):
    response = system[3][A.actor].post(WORK, json={"goal": "replacement"})
    assert response.status_code == 409
    assert system[3][A.actor].get(WORK).json() == work


@pytest.mark.parametrize("etag,status", [(None, 428), ("*", 412), ('W/"stale"', 412), ("x,y", 412)])
def test_requires_exact_etag(system, work, etag, status):
    headers = {"If-Match": etag} if etag else {}
    response = system[3][A.actor].patch(WORK, headers=headers, json={"progress": "attempt"})
    assert response.status_code == status


def test_handoff_moves_edit_permission(system, work):
    a, b = system[3][A.actor], system[3][B.actor]
    no_owner = b.post(WORK + "/handoff", headers={"If-Match": work["etag"]}, json={"to_actor": A.actor})
    assert no_owner.status_code == 403
    bad_target = a.post(WORK + "/handoff", headers={"If-Match": work["etag"]}, json={"to_actor": C.actor})
    assert bad_target.status_code == 403
    moved = a.post(WORK + "/handoff", headers={"If-Match": work["etag"]}, json={"to_actor": B.actor})
    assert moved.status_code == 200
    state = moved.json()
    assert state["owner"] == B.actor and state["revision"] == 2
    assert state["handoff"]["from_actor"] == A.actor
    assert a.patch(WORK, headers={"If-Match": state["etag"]}, json={"goal": "try"}).status_code == 403
    assert b.patch(WORK, headers={"If-Match": state["etag"]}, json={"progress": "resumed"}).status_code == 200


def test_published_is_immutable_through_every_work_mutation(system, work):
    a, b = system[3][A.actor], system[3][B.actor]
    published = a.post(WORK + "/publish", headers={"If-Match": work["etag"]})
    assert published.status_code == 200
    data = published.json()
    assert data["status"] == "published" and data["published_by"] == A.actor
    for client in (a, b):
        headers = {"If-Match": data["etag"]}
        assert client.patch(WORK, headers=headers, json={"artifact_text": "tamper"}).status_code == 409
        assert client.post(WORK + "/handoff", headers=headers, json={"to_actor": B.actor}).status_code == 409
        assert client.post(WORK + "/publish", headers=headers).status_code == 409
        assert client.post(WORK, json={"goal": "overwrite"}).status_code == 409
        assert client.delete(WORK).status_code == 405
        assert client.put(WORK, json={"goal": "overwrite"}).status_code == 405
    memory = a.post("/teams/team-one/memory/team", json={"marker": "published", "value": "unapproved"})
    assert memory.status_code == 202
    assert a.get(WORK).json() == data


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/teams/team-two/works/task-1", None),
    ("POST", "/teams/team-two/works/task-1", {"goal": "bad"}),
    ("POST", "/teams/team-two/memory/team", {"marker": "fixture", "value": "bad"}),
    ("POST", "/teams/team-one/memory/private/agent-b", {"marker": "fixture", "value": "bad"}),
    ("POST", "/teams/team-one/memory/private/agent-b/search", {"query": "bad"}),
    ("POST", "/teams/team-two/memory/team/search", {"query": "bad"}),
    ("GET", "/teams/team-two/works/task-1/context", None),
])
def test_forbidden_targets_precede_storage_or_memory(system, method, path, body):
    result = system[3][A.actor].request(method, path, json=body)
    assert result.status_code == 403
    assert system[1].data == {} and system[2].calls == []


@pytest.mark.parametrize("extra", [
    {"owner": "agent-b"}, {"actor": "agent-b"}, {"team": "team-two"},
    {"status": "published"}, {"revision": 999}, {"artifact": {"text": "bad"}},
])
def test_no_caller_owned_authority_fields(system, extra):
    result = system[3][A.actor].post(WORK, json={"goal": "fixture", **extra})
    assert result.status_code == 422
    assert system[1].data == {}


def test_unregistered_internal_identity_cannot_use_service(system):
    actor = Actor("dddddddd-dddd-dddd-dddd-dddddddddddd", "intruder", "team-one")
    with pytest.raises(Problem) as exc:
        system[0].get(actor, "team-one", "task-1")
    assert exc.value.status == 403


@pytest.mark.parametrize("body", [{}, {"goal": None}, {"revision": 3}, {"progress": None}])
def test_invalid_patch(system, work, body):
    assert system[3][A.actor].patch(WORK, headers={"If-Match": work["etag"]}, json=body).status_code == 422


def test_limits_and_invalid_ids(system):
    a = system[3][A.actor]
    assert a.post(WORK, content=b"x" * 65537, headers={"Content-Type": "application/json"}).status_code == 413
    assert a.post(WORK, json={"goal": "g" * 4097}).status_code == 422
    assert a.post(WORK, json={"goal": "g", "artifact_text": "界" * 17000}).status_code == 413
    assert a.post("/teams/team-one/works/bad:scope", json={"goal": "g"}).status_code == 422
    assert a.post(WORK, json={"goal": 123}).status_code == 422


def test_two_concurrent_updates_only_one_wins(system, work, monkeypatch):
    service, repository, _, _ = system
    original_get = repository.get
    barrier = Barrier(2)

    def simultaneous_get(team, row):
        record = original_get(team, row)
        barrier.wait(timeout=5)
        return record

    monkeypatch.setattr(repository, "get", simultaneous_get)

    def update(value):
        try:
            return service.update(A, A.team, "task-1", work["etag"], UpdateWork(progress=value))
        except Problem as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["first", "second"]))
    assert sum(not isinstance(r, Problem) for r in results) == 1
    assert [r.status for r in results if isinstance(r, Problem)] == [412]
    assert original_get(A.team, "work:task-1").payload["revision"] == 2


def test_only_health_is_anonymous(system):
    def reject():
        raise Problem(401, "UNAUTHENTICATED", "missing token")
    client = TestClient(create_app(system[0], reject))
    assert client.get("/healthz").json() == {"status": "healthy"}
    assert client.get(WORK).status_code == 401
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
