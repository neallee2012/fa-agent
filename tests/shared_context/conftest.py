from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"))


def _retry_windows_local_state_replace():
    """Windows only: the pinned core local State store's ``_write`` (``os.replace``) meets WinError 5
    when another process (antivirus/indexer) briefly holds the just-replaced file; POSIX never does.
    Retry only that error, a fixed 20 x 25 ms, then raise the original. Upstream should do this itself."""
    try:
        from azure.ai.agentserver.core.storage._local_state import LocalStateStoreBackend
    except ImportError:
        return
    pinned_write = LocalStateStoreBackend._write

    def write(self, document):
        for attempt in range(20):
            try:
                return pinned_write(self, document)
            except PermissionError as error:
                if getattr(error, "winerror", None) != 5 or attempt == 19:
                    raise
                time.sleep(0.025)

    LocalStateStoreBackend._write = write


def _cache_windows_local_state_reads():
    """Windows only: the pinned core re-reads its whole JSON file on every operation, and opening it right after
    ``os.replace`` costs 13-18 ms there (about half of a native test file's time). Keep the JSON text this
    process last read or wrote, and reuse it while the file's (inode, mtime, size) is unchanged, so the file stays
    the source of truth: a restart, a second runtime or a hand-edited file is read again. A hit is parsed from that
    text, so it round-trips like the file (tuples become lists, keys become strings). Both calls run under the
    core's per-path lock, so it holds for one writing process; a second process writing the same file is unlocked in
    the core itself, and its write between our replace and our stat would go unseen."""
    try:
        from azure.ai.agentserver.core.storage._local_state import LocalStateStoreBackend
    except ImportError:
        return
    pinned_read, pinned_write, documents = LocalStateStoreBackend._read, LocalStateStoreBackend._write, {}

    def signature(path):
        try:
            found = os.stat(path)
        except FileNotFoundError:
            return None
        # Without an inode (FAT, some network drives) a same-size write in one clock tick would look unchanged.
        return (found.st_ino, found.st_mtime_ns, found.st_size) if found.st_ino else None

    def remember(path, sign, document):
        if sign is None or document is None:
            documents.pop(path, None)
            return
        if len(documents) >= 64:  # one entry per tmp_path store; keep the session's memory flat
            documents.clear()
        documents[path] = sign, json.dumps(document, sort_keys=True)  # sort_keys, as the core writes it

    def read(self):
        path = str(self._path)
        sign, known = signature(path), documents.get(path)
        if sign is not None and known is not None and known[0] == sign:
            return json.loads(known[1])
        document = pinned_read(self)
        remember(path, sign, document)  # a write between the stat and the read leaves an old sign: a miss next time
        return document

    def write(self, document):
        path = str(self._path)
        pinned_write(self, document)
        remember(path, signature(path), document)

    LocalStateStoreBackend._read, LocalStateStoreBackend._write = read, write


def require_cold_resume_paths(root):
    """The offline SDK cold-resume checkpoint nests about 200 characters below the test root; the local State store's
    temporary files about 130 (measured on runtime 1.0.103)."""
    import os
    if os.name != "nt" or len(str(root)) + 200 < 260:
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            if winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1:
                return
    except OSError:
        pass
    pytest.skip("needs Windows LongPathsEnabled=1 or a short test root (--basetemp or N1_NATIVE_WORKSPACE, e.g. "
                "under C:\\t): the SDK checkpoint and State store paths below it exceed MAX_PATH 260")


if os.name == "nt":
    _retry_windows_local_state_replace()
    _cache_windows_local_state_reads()

from shared_context.auth import Actor, AuthSettings
from shared_context.errors import Problem
from shared_context.models import CreateWork
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
    return ContextService(repository, memory, auth), repository, memory


@pytest.fixture
def work(system):
    return system[0].create(A, A.team, "task-1", CreateWork(
        goal="Verify synthetic amber fixture", artifact_text="draft v1",
    )).response()
