from __future__ import annotations

import copy
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"))

from shared_context.api import create_app
from shared_context.auth import Actor, AuthSettings
from shared_context.errors import Problem
from shared_context.service import ContextService
from shared_context.storage import Record, serialize

TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "api://22222222-2222-2222-2222-222222222222"
A = Actor("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "agent-a", "team-one", "Synthetic planner role.")
B = Actor("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "agent-b", "team-one", "Synthetic reviewer role.")
C = Actor("cccccccc-cccc-cccc-cccc-cccccccccccc", "agent-c", "team-two")


class InMemoryRepository:
    """Thread-safe fake, deliberately confined to tests; not Azure proof."""

    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()
        self.sequence = 0

    def get(self, team, row):
        with self.lock:
            if (team, row) not in self.data:
                raise Problem(404, "NOT_FOUND", "missing fake record")
            return copy.deepcopy(self.data[team, row])

    def _record(self, team, row, payload):
        serialize(payload)
        self.sequence += 1
        result = Record(copy.deepcopy(payload), f'W/"fake-{self.sequence}"')
        self.data[team, row] = result
        return copy.deepcopy(result)

    def create(self, team, row, payload):
        with self.lock:
            if (team, row) in self.data:
                raise Problem(409, "ALREADY_EXISTS", "existing fake record")
            return self._record(team, row, payload)

    def replace(self, team, row, payload, etag):
        with self.lock:
            if self.data[team, row].etag != etag:
                raise Problem(412, "ETAG_CONFLICT", "fake conditional conflict")
            return self._record(team, row, payload)

    def list_prefix(self, team, prefix, limit, *, after=None):
        with self.lock:
            rows = sorted(((row, copy.deepcopy(record)) for (key, row), record in self.data.items()
                           if key == team and row.startswith(prefix) and (after is None or row > after)),
                          key=lambda item: item[0])
        return tuple(rows[:limit]), len(rows) > limit


class FakeMemory:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.search_results = {}
        self.failure = None

    def start(self, scope, items):
        self.calls.append(("start", scope, items))
        if self.failure:
            raise self.failure
        update_id = f"update-{len(self.calls)}"
        self.results[update_id] = {"update_id": update_id, "status": "in_progress"}
        return {"update_id": update_id, "status": "queued"}

    def poll(self, update_id):
        self.calls.append(("poll", update_id))
        if self.failure:
            raise self.failure
        return self.results[update_id]

    def search(self, scope, query):
        self.calls.append(("search", scope, query))
        if self.failure:
            raise self.failure
        return {"search_id": "synthetic-search", "memories": self.search_results.get(scope, [])}


@pytest.fixture
def auth():
    return AuthSettings(TENANT, AUDIENCE, (
        f"https://sts.windows.net/{TENANT}/",
        f"https://login.microsoftonline.com/{TENANT}/v2.0",
    ), {a.oid: a for a in (A, B, C)})


@pytest.fixture
def system(auth):
    repository, memory = InMemoryRepository(), FakeMemory()
    service = ContextService(repository, memory, auth)
    clients = {}
    def dependency(selected):
        def verified_identity():
            return selected
        return verified_identity
    for actor in (A, B, C):
        # Zero public-header identity overrides. Tests inject only this internal dependency.
        clients[actor.actor] = TestClient(create_app(service, dependency(actor)))
    return service, repository, memory, clients


@pytest.fixture
def work(system):
    client = system[3][A.actor]
    response = client.post("/teams/team-one/works/task-1", json={
        "goal": "Verify synthetic amber fixture", "artifact_text": "draft v1",
    })
    assert response.status_code == 201
    return response.json()
