"""Remote-mode pieces with an in-memory Blob stand-in; no Azure, Entra or Container Apps is contacted."""
from __future__ import annotations

import sqlite3
import tarfile
import io
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from azure.core.exceptions import HttpResponseError, ResourceExistsError

from web_test_console.adapter import ConsoleError
from web_test_console.remote import (
    EasyAuthTokenProvider, LeasedArchive, RemoteBoundary, pack, unpack,
)
from test_m3_auth import jwk_client, signing_key  # noqa: F401 - fixtures used by real_interface
from test_web_test_console_frozen_interface import real_interface  # noqa: F401

OWNER = "00000000-0000-0000-0000-00000000001b"
ORIGIN = "https://ca-fa-console-stage.example.westus3.azurecontainerapps.io"


def _http_error(status):
    error = HttpResponseError(message=str(status))
    error.status_code = status
    return error


class FakeBlob:
    """Enough of BlobClient/BlobLeaseClient semantics to show who may write.

    Like Azure Storage, a write to a leased blob without its lease ID is 412 even when the blob already exists.
    """

    def __init__(self):
        self.data, self.lease_id, self.uploads, self.next_lease = None, None, 0, 0

    def exists(self):
        return self.data is not None

    def upload_blob(self, data, overwrite=False, lease=None):
        if self.lease_id is not None and (lease is None or lease.id != self.lease_id):
            raise _http_error(412)
        if self.data is not None and not overwrite:
            raise ResourceExistsError(message="exists")
        self.data, self.uploads = bytes(data), self.uploads + 1

    def download_blob(self, lease=None, max_concurrency=1):
        return SimpleNamespace(readall=lambda: self.data)


class FakeLease:
    def __init__(self, blob):
        blob.next_lease += 1
        self.blob, self.id = blob, f"lease-{blob.next_lease}"

    def acquire(self, lease_duration):
        if self.blob.lease_id is not None:
            raise _http_error(409)
        self.blob.lease_id = self.id

    def break_lease(self, lease_break_period):
        self.blob.lease_id = None

    def renew(self):
        if self.blob.lease_id != self.id:
            raise _http_error(409)

    def release(self):
        if self.blob.lease_id == self.id:
            self.blob.lease_id = None


def _journal(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS intents (task_id TEXT PRIMARY KEY, outcome TEXT)")
        db.execute("INSERT OR REPLACE INTO intents VALUES ('rid-1', ?)", (value,))


def _read(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT outcome FROM intents WHERE task_id='rid-1'").fetchone()[0]


def _replica(blob, root, lost):
    archive = LeasedArchive(blob, root, on_lost=lambda: lost.append(root), lease_factory=FakeLease)
    archive.start(renew=False)
    return archive


def test_new_replica_restores_the_newest_commit_and_fences_the_old_one(tmp_path):
    blob, lost = FakeBlob(), []
    old = _replica(blob, tmp_path / "old", lost)
    _journal(tmp_path / "old" / "console.sqlite3", "UNKNOWN")
    old.persist(tmp_path / "old" / "console.sqlite3")
    _journal(tmp_path / "old" / "agents" / "b" / "journal.sqlite3", "UNKNOWN")
    old.persist(tmp_path / "old" / "agents" / "b" / "journal.sqlite3")

    new = _replica(blob, tmp_path / "new", lost)
    assert _read(tmp_path / "new" / "console.sqlite3") == "UNKNOWN"
    assert _read(tmp_path / "new" / "agents" / "b" / "journal.sqlite3") == "UNKNOWN"

    _journal(tmp_path / "old" / "console.sqlite3", "CONFIRMED")
    with pytest.raises(ConsoleError) as refused:
        old.persist(tmp_path / "old" / "console.sqlite3")
    assert (refused.value.code, refused.value.status) == ("REMOTE_STATE_UNAVAILABLE", 503)
    assert lost == [tmp_path / "old"]
    with pytest.raises(ConsoleError):
        old.persist(tmp_path / "old" / "console.sqlite3")
    assert lost == [tmp_path / "old"]
    assert set(unpack(blob.data)) == {"console.sqlite3", "agents/b/journal.sqlite3"}

    _journal(tmp_path / "new" / "console.sqlite3", "CONFIRMED")
    new.persist(tmp_path / "new" / "console.sqlite3")
    third = _replica(blob, tmp_path / "third", lost)
    assert _read(tmp_path / "third" / "console.sqlite3") == "CONFIRMED"
    third.release()
    assert blob.lease_id is None


def test_first_start_creates_an_empty_archive_and_refuses_paths_outside_its_root(tmp_path):
    blob, lost = FakeBlob(), []
    archive = _replica(blob, tmp_path / "state", lost)
    assert unpack(blob.data) == {} and blob.lease_id is not None
    _journal(tmp_path / "elsewhere.sqlite3", "UNKNOWN")
    with pytest.raises(ConsoleError):
        archive.persist(tmp_path / "elsewhere.sqlite3")
    assert lost == [] and unpack(blob.data) == {}


def test_failed_upload_is_reported_before_the_caller_can_continue(tmp_path):
    blob, lost = FakeBlob(), []
    archive = _replica(blob, tmp_path / "state", lost)
    _journal(tmp_path / "state" / "console.sqlite3", "UNKNOWN")
    blob.upload_blob = lambda *args, **kwargs: (_ for _ in ()).throw(_http_error(503))
    with pytest.raises(ConsoleError) as refused:
        archive.persist(tmp_path / "state" / "console.sqlite3")
    assert refused.value.code == "REMOTE_STATE_UNAVAILABLE" and len(lost) == 1


def test_archive_warns_at_half_the_cap_and_never_writes_one_restore_would_refuse(tmp_path, monkeypatch, capsys):
    from web_test_console import remote

    monkeypatch.setattr(remote, "MAX_ARCHIVE_BYTES", 64 * 1024)
    blob, lost = FakeBlob(), []
    archive = _replica(blob, tmp_path / "state", lost)
    journal = tmp_path / "state" / "console.sqlite3"
    _journal(journal, "x" * 100)
    archive.persist(journal)
    assert "REMOTE_STATE_NEAR_LIMIT" not in capsys.readouterr().err
    for _ in range(2):
        _journal(journal, "x" * 36 * 1024)
        archive.persist(journal)
    assert capsys.readouterr().err.count("REMOTE_STATE_NEAR_LIMIT") == 1
    saved, uploads = blob.data, blob.uploads
    _journal(journal, "x" * 80 * 1024)
    with pytest.raises(ConsoleError) as refused:
        archive.persist(journal)
    assert (refused.value.code, refused.value.status) == ("REMOTE_STATE_FULL", 503)
    assert (blob.data, blob.uploads) == (saved, uploads)
    assert not archive.lost and lost == []  # a full archive is a local refusal; the replica keeps serving reads
    restored = _replica(blob, tmp_path / "restored", lost)
    assert _read(tmp_path / "restored" / "console.sqlite3") == "x" * 36 * 1024
    restored.release()


def test_a_new_agent_connection_file_survives_a_replica_takeover(tmp_path):
    blob, lost = FakeBlob(), []
    old = _replica(blob, tmp_path / "old", lost)
    link = tmp_path / "old" / "agents" / "abc" / "config.json"
    link.parent.mkdir(parents=True)
    link.write_bytes(b'{"agent":"new"}')
    old.persist_file(link)
    new = _replica(blob, tmp_path / "new", lost)
    assert (tmp_path / "new" / "agents" / "abc" / "config.json").read_bytes() == b'{"agent":"new"}'
    big = tmp_path / "new" / "agents" / "abc" / "big.json"
    big.write_bytes(b"x" * 65537)
    with pytest.raises(ConsoleError):
        new.persist_file(big)
    assert not new.lost and lost == []  # a refused local file never stops the replica
    new.persist_file(tmp_path / "new" / "agents" / "abc" / "config.json")
    with pytest.raises(ConsoleError):
        old.persist_file(link)
    assert old.lost and lost == [tmp_path / "old"]


def test_restore_never_overwrites_files_already_in_the_container(tmp_path):
    blob, lost = FakeBlob(), []
    blob.data = pack({"console.sqlite3": b"saved"})
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "console.sqlite3").write_bytes(b"local")
    with pytest.raises(ConsoleError):
        LeasedArchive(blob, tmp_path / "state", on_lost=lambda: lost.append(1), lease_factory=FakeLease).start(renew=False)
    assert (tmp_path / "state" / "console.sqlite3").read_bytes() == b"local"


@pytest.mark.parametrize("name,kind", [
    ("../escape.sqlite3", tarfile.REGTYPE), ("/abs.sqlite3", tarfile.REGTYPE), ("a/./b", tarfile.REGTYPE),
    ("link", tarfile.SYMTYPE), ("folder", tarfile.DIRTYPE), ("a/b/c/d/e", tarfile.REGTYPE),
])
def test_unpack_accepts_only_plain_relative_files(name, kind):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.type = kind
        archive.addfile(info, io.BytesIO(b"") if kind == tarfile.REGTYPE else None)
    with pytest.raises(ConsoleError):
        unpack(buffer.getvalue())


def test_unpack_rejects_duplicates_and_round_trips_plain_files():
    assert unpack(pack({"a.sqlite3": b"1", "agents/x/journal.sqlite3": b"2"})) == {
        "a.sqlite3": b"1", "agents/x/journal.sqlite3": b"2"}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for _ in range(2):
            info = tarfile.TarInfo("a.sqlite3")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"1"))
    with pytest.raises(ConsoleError):
        unpack(buffer.getvalue())


def test_only_the_configured_platform_principal_is_admitted_and_its_token_is_used():
    boundary = RemoteBoundary(ORIGIN, OWNER)
    provider = EasyAuthTokenProvider()
    assert boundary.host == "ca-fa-console-stage.example.westus3.azurecontainerapps.io"
    for headers in ({}, {"x-ms-client-principal-id": OWNER},
                    {"x-ms-client-principal-id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                     "x-ms-client-principal-idp": "aad"},
                    {"x-ms-client-principal-id": OWNER, "x-ms-client-principal-idp": "github"}):
        with pytest.raises(ConsoleError) as denied:
            boundary.admit(headers)
        assert (denied.value.code, denied.value.status) == ("REMOTE_PRINCIPAL_DENIED", 403)
    boundary.admit({"x-ms-client-principal-id": OWNER, "x-ms-client-principal-idp": "aad"})
    with pytest.raises(ConsoleError) as missing:
        provider()
    assert (missing.value.code, missing.value.status) == ("LOGIN_REQUIRED", 401)
    boundary.admit({"x-ms-client-principal-id": OWNER, "x-ms-client-principal-idp": "aad",
                    "x-ms-token-aad-access-token": "header.payload.signature"})
    assert provider() == "header.payload.signature"


@pytest.mark.parametrize("origin,owner", [
    ("http://ca.example.io", OWNER), ("https://ca.example.io/", OWNER), ("https://ca.example.io:8443", OWNER),
    ("https://ca.example.io", OWNER.upper()), ("https://ca.example.io", "not-a-uuid"),
])
def test_remote_boundary_requires_one_https_origin_and_a_canonical_owner(origin, owner):
    with pytest.raises(ConsoleError):
        RemoteBoundary(origin, owner)


REMOTE_CLIENT = "00000000-0000-0000-0000-000000000026"


def _remote_ui(c, monkeypatch, persisted, journal, *, backend=None, prewarm=False):
    from fastapi.testclient import TestClient
    from web_test_console.bridge import Journal, create_app

    monkeypatch.setattr(Journal, "after_commit", lambda path: persisted.append(path))
    app = create_app(c.config, journal, backend=backend or c.backend, token_provider=EasyAuthTokenProvider(),
                     clock=lambda: c.h.time, environment="OFFLINE_TEST", instruction_connection=c.connection,
                     remote=RemoteBoundary(ORIGIN, c.config.human_oid), prewarm=prewarm)
    return TestClient(app, base_url=ORIGIN)


def test_remote_console_needs_the_platform_principal_and_reads_its_token_per_request(
        real_interface, monkeypatch, tmp_path):
    c, persisted, journal = real_interface, [], tmp_path / "remote" / "console.sqlite3"
    ui = _remote_ui(c, monkeypatch, persisted, journal)
    assert set(persisted) == {journal.resolve()}
    signed_in = {"x-ms-client-principal-id": c.config.human_oid, "x-ms-client-principal-idp": "aad"}
    for headers in ({}, {**signed_in, "x-ms-client-principal-id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}):
        for path in ("/", "/api/state", "/novnc/core/rfb.js"):
            denied = ui.get(path, headers=headers)
            assert denied.status_code == 403 and denied.json()["error"]["code"] == "REMOTE_PRINCIPAL_DENIED"

    page = ui.get("/", headers=signed_in)
    assert page.status_code == 200
    assert page.headers["referrer-policy"] == "same-origin"
    landing = ui.get("/", headers={**signed_in, "sec-fetch-site": "cross-site"})
    assert landing.status_code == 200
    assert ui.get("/api/state", headers={**signed_in, "sec-fetch-site": "cross-site"}).status_code == 403
    named = ui.get("/api/state", headers={**signed_in, "x-ms-client-principal-name": "neal@example.org"}).json()
    assert named["remote_user"] == "neal@example.org"
    assert ui.get("/api/state", headers=signed_in).json()["remote_user"] == "已指派的帳號"
    cookie = page.headers["set-cookie"]
    assert cookie.startswith("__Host-fa_console=") and "Secure" in cookie and "SameSite=strict" in cookie
    from fastapi.testclient import TestClient
    restarted = TestClient(ui.app, base_url=ORIGIN).get("/api/state", headers=signed_in)
    assert restarted.status_code == 403 and restarted.json()["error"]["code"] == "REMOTE_SESSION_CHANGED"
    assert "connect-src 'self' wss://ca-fa-console-stage.example.westus3.azurecontainerapps.io;" in (
        page.headers["content-security-policy"])
    csrf = ui.get("/api/state", headers=signed_in).json()["csrf"]
    post = {**signed_in, "Origin": ORIGIN, "X-Console-CSRF": csrf}

    missing = ui.post("/api/authenticate", json={}, headers=post)
    assert missing.status_code == 401 and missing.json()["error"]["code"] == "LOGIN_REQUIRED"
    token = c.h.headers()["Authorization"][7:]
    ok = ui.post("/api/authenticate", json={}, headers={**post, "x-ms-token-aad-access-token": token})
    assert ok.status_code == 200, ok.text
    assert ui.get("/api/state", headers=signed_in).json()["authentication"] == "AVAILABLE"

    assert ui.post("/api/instructions/conversations/new", json={},
                   headers={**post, "Origin": "http://127.0.0.1:8765"}).status_code == 403
    persisted.clear()
    assert ui.post("/api/instructions/conversations/new", json={}, headers=post).status_code == 200
    assert set(persisted) == {journal.resolve()}


def test_remote_console_sends_a_signed_in_owner_without_a_stored_token_to_re_sign_in(
        real_interface, monkeypatch, tmp_path):
    # The token-store entry is gone (rotated or cleared) while the sign-in cookie still holds: the page still loads
    # and answers 401 LOGIN_REQUIRED, which shows 重新登入; only another identity is 403.
    c = real_interface
    ui = _remote_ui(c, monkeypatch, [], tmp_path / "remote" / "console.sqlite3")
    signed_in = {"x-ms-client-principal-id": c.config.human_oid, "x-ms-client-principal-idp": "aad"}
    assert ui.get("/", headers=signed_in).status_code == 200
    post = {**signed_in, "Origin": ORIGIN, "X-Console-CSRF": ui.get("/api/state", headers=signed_in).json()["csrf"]}
    for gone in (signed_in, {**signed_in, "x-ms-token-aad-access-token": ""}):
        assert ui.get("/", headers=gone).status_code == 200
        assert ui.get("/api/state", headers=gone).json()["authentication"] == "LOGIN_REQUIRED"
        again = ui.post("/api/authenticate", json={}, headers={**post, **gone})
        assert again.status_code == 401 and again.json()["error"]["code"] == "LOGIN_REQUIRED"
    other = {**post, "x-ms-token-aad-access-token": c.h.headers()["Authorization"][7:],
             "x-ms-client-principal-id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}
    for path in ("/", "/api/state"):
        denied = ui.get(path, headers=other)
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "REMOTE_PRINCIPAL_DENIED"
    wrong = ui.post("/api/authenticate", json={}, headers=other)
    assert wrong.status_code == 403 and wrong.json()["error"]["code"] == "REMOTE_PRINCIPAL_DENIED"


def test_remote_console_stops_when_a_commit_cannot_be_kept(real_interface, monkeypatch, tmp_path):
    from web_test_console.bridge import Journal

    c, persisted = real_interface, []
    ui = _remote_ui(c, monkeypatch, persisted, tmp_path / "remote" / "console.sqlite3")
    signed_in = {"x-ms-client-principal-id": c.config.human_oid, "x-ms-client-principal-idp": "aad"}
    ui.get("/", headers=signed_in)
    post = {**signed_in, "Origin": ORIGIN, "X-Console-CSRF": ui.get("/api/state", headers=signed_in).json()["csrf"]}
    assert ui.post("/api/authenticate", json={}, headers={
        **post, "x-ms-token-aad-access-token": c.h.headers()["Authorization"][7:]}).status_code == 200

    def refuse(path):
        raise ConsoleError("REMOTE_STATE_UNAVAILABLE", 503)

    monkeypatch.setattr(Journal, "after_commit", refuse)
    stopped = ui.post("/api/instructions/conversations/new", json={}, headers=post)
    assert stopped.status_code == 503 and stopped.json()["error"]["code"] == "REMOTE_STATE_UNAVAILABLE"
    assert stopped.json()["error"]["automatic_retry"] is False


def test_remote_console_forwards_the_typing_prewarm_with_the_owners_saved_sign_in_token(
        real_interface, monkeypatch, tmp_path):
    # README#typing-prewarm-stage: the same Easy Auth admission as every other POST, and like them it forwards the
    # token the console kept at sign-in (TokenVault), not a token this request names.
    import httpx

    c, seen, persisted = real_interface, [], []

    def backend(request):
        if request.url.path == "/work-policy/v1/native-turns/prewarm":
            seen.append(request)
            return httpx.Response(200, json={"state": "sent", "toolboxes": 1}, headers={"cache-control": "no-store"})
        return c.backend.send(httpx.Request(request.method, request.url, headers=request.headers,
                                            content=request.content))

    upstream = httpx.Client(transport=httpx.MockTransport(backend))
    try:
        ui = _remote_ui(c, monkeypatch, persisted, tmp_path / "remote" / "console.sqlite3", backend=upstream,
                        prewarm=True)
        signed_in = {"x-ms-client-principal-id": c.config.human_oid, "x-ms-client-principal-idp": "aad"}
        page = ui.get("/", headers=signed_in)
        assert "const PREWARM_CONFIGURED=true;" in page.text
        token = c.h.headers()["Authorization"][7:]
        post = {**signed_in, "Origin": ORIGIN, "X-Console-CSRF": ui.get("/api/state", headers=signed_in).json()["csrf"]}
        assert ui.post("/api/authenticate", json={}, headers={**post, "x-ms-token-aad-access-token": token}).status_code == 200
        persisted.clear()
        # No token header on the prewarm itself: the kept sign-in token is what reaches the API.
        sent = ui.post("/api/instructions/prewarm", json={}, headers=post)
        assert sent.status_code == 200 and sent.json() == {"prewarm": "sent"}
        assert len(seen) == 1 and seen[0].headers["authorization"] == "Bearer " + token
        assert seen[0].content == b"{}" and persisted == []
        # Without the platform principal, or from another origin, nothing reaches the API.
        other = {**post, "x-ms-client-principal-id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}
        denied = ui.post("/api/instructions/prewarm", json={}, headers=other)
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "REMOTE_PRINCIPAL_DENIED"
        assert ui.post("/api/instructions/prewarm", json={},
                       headers={**post, "Origin": "http://127.0.0.1:8765"}).status_code == 403
        assert len(seen) == 1
    finally:
        upstream.close()


def test_local_console_keeps_its_loopback_cookie_and_never_calls_the_remote_hook(real_interface):
    from fastapi.testclient import TestClient
    from web_test_console.bridge import Journal, create_app

    c = real_interface
    assert Journal.after_commit is None
    app = create_app(c.config, c.journal, backend=c.backend, token_provider=lambda: c.h.headers()["Authorization"][7:],
                     clock=lambda: c.h.time, environment="OFFLINE_TEST", instruction_connection=c.connection)
    ui = TestClient(app, base_url="http://127.0.0.1:8765")
    page = ui.get("/")
    assert page.headers["set-cookie"].startswith("fa_console=") and "Secure" not in page.headers["set-cookie"]
    assert page.headers["referrer-policy"] == "no-referrer"
    assert ui.get("/", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert "remote_user" not in ui.get("/api/state").json()
    assert "connect-src 'self' ws://127.0.0.1:8765;" in page.headers["content-security-policy"]
    assert ui.get("/api/state", headers={"x-ms-client-principal-id": c.config.human_oid}).status_code == 200
    remote_host = TestClient(app, base_url=ORIGIN)
    assert remote_host.get("/").status_code == 403


def test_serve_remote_pins_owner_and_state_root_before_it_starts(real_interface, tmp_path, monkeypatch):
    from web_test_console import __main__ as launcher
    from web_test_console import remote
    from web_test_console.adapter import canonical, sha
    from web_test_console.bridge import Journal

    c = real_interface
    config = tmp_path / "config" / "console.json"
    config.parent.mkdir()
    config.write_bytes(canonical(c.config.model_dump(mode="json")))
    events = []

    class Archive:
        def __init__(self, blob, root, **kwargs):
            self.root = Path(root).resolve()
            events.append(("archive", str(blob.url)))

        def contains(self, path):
            return Path(path).resolve().parent == self.root

        def start(self):
            events.append("start")

        def persist(self, path):
            events.append(("persist", path))

        def persist_file(self, path):
            events.append(("persist_file", path))

        def release(self):
            events.append("release")

    calls = []
    monkeypatch.setattr(remote, "LeasedArchive", Archive)
    monkeypatch.setattr(launcher, "create_app", lambda *args, **kwargs: calls.append((args, kwargs)) or object())
    monkeypatch.setattr(launcher.uvicorn, "run", lambda *args, **kwargs: events.append(("run", kwargs["host"])))
    monkeypatch.setattr(Journal, "after_commit", None)
    monkeypatch.setattr(Journal, "after_file", None)
    monkeypatch.delenv("FA_CONSOLE_PREWARM", raising=False)
    state = tmp_path / "state"
    args = ["serve-remote", "--config", str(config), "--config-sha256", sha(config.read_bytes()),
            "--journal", str(state / "console.sqlite3"), "--state-root", str(state),
            "--state-blob-url", "https://stconsole.blob.core.windows.net/journal/" + c.config.human_oid + "/state.tar",
            "--public-origin", ORIGIN, "--owner-oid", c.config.human_oid,
            "--managed-identity-client-id", "11111111-2222-3333-4444-555555555555"]
    assert launcher.main(args) == 0
    assert events[1:] == ["start", ("run", "0.0.0.0"), "release"]
    assert Journal.after_commit.__self__.root == state.resolve()
    assert Journal.after_file.__self__ is Journal.after_commit.__self__
    kwargs = calls[0][1]
    assert kwargs["remote"] == RemoteBoundary(ORIGIN, c.config.human_oid) and kwargs["port"] == 8080
    assert isinstance(kwargs["token_provider"], EasyAuthTokenProvider)
    # The stage console's typing prewarm comes from its container ENV (deploy/stage/stage.json console_environment).
    assert kwargs["prewarm"] is False
    monkeypatch.setenv("FA_CONSOLE_PREWARM", "1")
    assert launcher.main(args) == 0
    assert calls.pop()[1]["prewarm"] is True
    monkeypatch.delenv("FA_CONSOLE_PREWARM")

    events.clear()
    other = list(args)
    other[other.index("--owner-oid") + 1] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    assert launcher.main(other) == 2
    outside = list(args)
    outside[outside.index("--journal") + 1] = str(tmp_path / "elsewhere" / "console.sqlite3")
    assert launcher.main(outside) == 2
    assert "start" not in events and len(calls) == 1

    def refused(self):
        events.append("start")
        raise _http_error(412)

    monkeypatch.setattr(Archive, "start", refused)
    import io as _io, contextlib
    err = _io.StringIO()
    with contextlib.redirect_stderr(err):
        assert launcher.main(args) == 2
    assert "REMOTE_CONFIGURATION_REJECTED (HttpResponseError 412)" in err.getvalue()
    assert events[-2:] == ["start", "release"] and len(calls) == 1


def test_remote_bundle_changes_only_the_client_and_paths_and_pins_new_bytes(real_interface, tmp_path):
    import json
    from web_test_console import __main__ as launcher
    from web_test_console.adapter import parse_config, sha
    from web_test_console.instructions import InstructionConnection
    from test_web_test_console_agents import connection_file, supplied_peer

    c = real_interface
    manifest = connection_file(c, tmp_path)
    peer = supplied_peer(c, manifest)
    out = tmp_path / "bundle"
    args = ["remote-bundle", "--agents", str(manifest.path), "--agents-sha256", sha(manifest.path.read_bytes()),
            "--client-id", REMOTE_CLIENT, "--out", str(out)]
    assert launcher.main(args) == 0

    agents = json.loads((out / "agents.json").read_bytes())
    assert agents["default_agent_id"] == "agent-one"
    for entry, original in zip(agents["agents"], (c.config, peer)):
        raw = (out / entry["config_path"].removeprefix("/config/")).read_bytes()
        config = parse_config(raw)
        assert sha(raw) == entry["config_sha256"] == config.sha256
        assert config.client_id == REMOTE_CLIENT != original.client_id
        assert config.model_dump(mode="json") | {"client_id": original.client_id} == original.model_dump(mode="json")
        raw = (out / entry["instructions_path"].removeprefix("/config/")).read_bytes()
        assert sha(raw) == entry["instructions_sha256"]
        assert InstructionConnection.model_validate_json(raw).configuration_sha256 == config.sha256
    assert [entry["journal_path"] for entry in agents["agents"]] == [
        "/data/state/console.sqlite3", "/data/state/agents/" + agents["agents"][1]["config_path"][8:-5] + ".sqlite3"]
    assert agents["agents"][0]["config_path"] == "/config/console.json"
    parameters = json.loads((out / "configFiles.json").read_text(encoding="utf-8"))
    assert {name: value.encode() for name, value in parameters.items()} == {
        path.name: path.read_bytes() for path in out.iterdir() if path.name != "configFiles.json"}

    assert launcher.main(args) == 2
    assert launcher.main(args[:4] + ["0" * 64] + args[5:-1] + [str(tmp_path / "other")]) == 2
    assert launcher.main(args[:6] + ["not-a-uuid"] + args[7:-1] + [str(tmp_path / "third")]) == 2
