"""ADR-0009 phase 5a offline: the real Blob SDK against a local stateful Blob fake; no Azure."""

from __future__ import annotations

import hashlib
from contextlib import ExitStack
from urllib.parse import unquote, urlparse

import pytest
from azure.core.exceptions import ServiceResponseError
from azure.core.pipeline.transport import HttpTransport
from azure.storage.blob import ContainerClient

from conftest import TENANT
from test_backup_store import BlobResponse, Credential
from shared_context.errors import Problem
from shared_context.native_files_wire import MAX_FILE_BYTES, MAX_FILE_ENTRIES, MAX_MANIFEST_BYTES, NativeFileEntry
from shared_context.upgrade_snapshot_store import (CONTAINER_URL_ENV, MANIFEST_NAME, UpgradeSnapshotManifest,
                                                   UpgradeSnapshotStore, snapshot_container_url)

TEAM, AGENT, UPGRADE = "team-one", "agent-a", "upgrade-0001"
CONTAINER = "team-artifacts"
PREFIX = f"fa-upgrade-snapshots/{TENANT}/{TEAM}/{AGENT}/{UPGRADE}/"
DATE = "Mon, 14 Sep 2026 14:00:00 GMT"


class BlobFake(HttpTransport):
    """Create-only Put Blob and (ranged) Get Blob over an in-memory container; ``fail_put`` breaks the Nth PUT."""

    def __init__(self):
        self.blobs, self.requests, self.fail_put = {}, [], None

    def open(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def send(self, request, **kwargs):
        self.requests.append(request)
        name = unquote(urlparse(request.url).path).removeprefix(f"/{CONTAINER}/")
        if request.method == "PUT":
            if self.fail_put == sum(item.method == "PUT" for item in self.requests):
                raise ServiceResponseError("connection dropped mid-snapshot")
            assert request.headers.get("If-None-Match") == "*"
            if name in self.blobs:
                return BlobResponse(request, 409, {"x-ms-error-code": "BlobAlreadyExists"})
            body = request.body if isinstance(request.body, bytes) else b"".join(request.body or [])
            meta = {key: value for key, value in request.headers.items() if key.lower().startswith("x-ms-meta-")}
            self.blobs[name] = (body, meta)
            return BlobResponse(request, 201, {"ETag": '"0x1"', "Last-Modified": DATE})
        assert request.method == "GET", request.method
        if name not in self.blobs:
            return BlobResponse(request, 404, {"x-ms-error-code": "BlobNotFound"})
        data, meta = self.blobs[name]
        headers = {"ETag": '"0x1"', "Last-Modified": DATE, "x-ms-blob-type": "BlockBlob",
                   "Content-Type": "application/octet-stream", **meta}
        wanted = request.headers.get("x-ms-range")
        if wanted is None:
            return BlobResponse(request, 200, {**headers, "Content-Length": str(len(data))}, data)
        if not data:
            return BlobResponse(request, 416, {"x-ms-error-code": "InvalidRange", "Content-Range": "bytes */0"})
        start, end = (int(part) for part in wanted.removeprefix("bytes=").split("-"))
        part = data[start:end + 1]
        return BlobResponse(request, 206, {**headers, "Content-Length": str(len(part)),
                                           "Content-Range": f"bytes {start}-{start + len(part) - 1}/{len(data)}"}, part)

    def puts(self):
        return [unquote(urlparse(item.url).path) for item in self.requests if item.method == "PUT"]


@pytest.fixture
def store():
    with ExitStack() as clients:
        fake = BlobFake()
        container = clients.enter_context(ContainerClient(
            "https://synthetic.blob.core.windows.net", CONTAINER, credential=Credential(), transport=fake,
            retry_total=0, max_single_put_size=MAX_FILE_BYTES, max_single_get_size=MAX_FILE_BYTES,
        ))
        yield UpgradeSnapshotStore(container, TENANT), fake


def file(path, data):
    return NativeFileEntry(path=path, kind="file", mode=0o644, byte_length=len(data),
                           sha256=hashlib.sha256(data).hexdigest())


WORKSPACE = {"docs/a.txt": b"alpha", "docs/empty": b"", "z.bin": bytes(range(256)) * 4}
ENTRIES = (NativeFileEntry(path="docs", kind="directory", mode=0o755),
           *(file(path, data) for path, data in sorted(WORKSPACE.items())))


def source(entry):
    return WORKSPACE[entry.path]


def save(snapshots, entries=ENTRIES, read=source, upgrade=UPGRADE):
    return snapshots.save(TEAM, AGENT, upgrade, 0o755, entries, read)


def test_files_are_written_first_and_the_manifest_last_then_restored_byte_for_byte(store):
    snapshots, fake = store
    result = save(snapshots)
    assert result.published and result.reason is None
    assert fake.puts() == [f"/{CONTAINER}/{PREFIX}files/{index:04d}" for index in (1, 2, 3)] + [
        f"/{CONTAINER}/{PREFIX}{MANIFEST_NAME}"]
    assert result.manifest_sha256 == hashlib.sha256(fake.blobs[PREFIX + MANIFEST_NAME][0]).hexdigest()
    manifest = snapshots.manifest(TEAM, AGENT, UPGRADE)
    assert manifest.entries == ENTRIES
    restored = {entry.path: snapshots.read_file(manifest, index)
                for index, entry in enumerate(manifest.entries) if entry.kind == "file"}
    assert restored == WORKSPACE


def test_a_prefix_without_a_manifest_is_never_restored(store):
    snapshots, fake = store
    data = b"half"
    fake.blobs[PREFIX + "files/0000"] = (data, {"x-ms-meta-tenant_id": TENANT, "x-ms-meta-team": TEAM,
                                                "x-ms-meta-sha256": hashlib.sha256(data).hexdigest(),
                                                "x-ms-meta-bytes": "4"})
    assert snapshots.manifest(TEAM, AGENT, UPGRADE) is None
    with pytest.raises(Problem) as error:
        snapshots.read_file(object(), 0)
    assert error.value.status == 422


def test_another_teams_manifest_is_not_restored(store):
    snapshots, _ = store
    assert save(snapshots).published
    assert snapshots.manifest("team-two", AGENT, UPGRADE) is None


@pytest.mark.parametrize("entries", [
    tuple(file(f"f{index}", b"") for index in range(MAX_FILE_ENTRIES + 1)),
    tuple(NativeFileEntry(path=f"f{index}", kind="file", mode=0o644, byte_length=MAX_FILE_BYTES, sha256="0" * 64)
          for index in range(3)),
], ids=["entries", "total-bytes"])
def test_over_the_limit_is_a_whole_fallback_with_a_reason_and_no_write(store, entries):
    snapshots, fake = store
    result = save(snapshots, entries, read=lambda entry: pytest.fail("no file is read over the limit"))
    assert (result.published, result.reason) == (False, "NATIVE_FILES_LIMIT")
    assert fake.requests == []


def stored(data, team=TEAM, tenant=TENANT):
    """A blob as ``_put`` writes it: content plus consistent tenant/Team/digest metadata."""
    return data, {"x-ms-meta-tenant_id": tenant, "x-ms-meta-team": team,
                  "x-ms-meta-sha256": hashlib.sha256(data).hexdigest(), "x-ms-meta-bytes": str(len(data))}


# Root-level empty files with long names: inside the entry and byte limits, over the manifest byte bound.
WIDE = tuple(file(f"{index:04d}" + "x" * 900, b"") for index in range(300))
WIDE_MANIFEST = UpgradeSnapshotManifest(team=TEAM, agent_id=AGENT, upgrade_id=UPGRADE, root_mode=0o755,
                                        entries=WIDE).encoded()


def test_a_manifest_over_its_byte_bound_is_a_whole_fallback_with_a_reason_and_no_write(store):
    snapshots, fake = store
    assert len(WIDE_MANIFEST) > MAX_MANIFEST_BYTES
    result = save(snapshots, WIDE, read=lambda entry: pytest.fail("no file is read over the limit"))
    assert (result.published, result.reason) == (False, "NATIVE_FILES_LIMIT")
    assert fake.requests == []


def test_a_stored_manifest_over_its_byte_bound_is_refused(store):
    snapshots, fake = store
    fake.blobs[PREFIX + MANIFEST_NAME] = stored(WIDE_MANIFEST)
    with pytest.raises(Problem) as error:
        snapshots.manifest(TEAM, AGENT, UPGRADE)
    assert error.value.status == 502


def test_a_rewritten_manifest_under_its_old_metadata_is_refused(store):
    snapshots, fake = store
    assert save(snapshots).published
    data, meta = fake.blobs[PREFIX + MANIFEST_NAME]
    changed = UpgradeSnapshotManifest.model_validate_json(data).model_copy(update={"root_mode": 0o700}).encoded()
    fake.blobs[PREFIX + MANIFEST_NAME] = (changed, meta)
    with pytest.raises(Problem) as error:
        snapshots.manifest(TEAM, AGENT, UPGRADE)
    assert error.value.status == 502


def test_a_manifest_copied_to_another_prefix_is_refused(store):
    """Its files prefix comes from its own IDs, so a copied manifest would restore another upgrade's files."""
    snapshots, fake = store
    assert save(snapshots).published
    other = PREFIX.replace(UPGRADE, "upgrade-0002")
    fake.blobs[other + MANIFEST_NAME] = fake.blobs[PREFIX + MANIFEST_NAME]
    with pytest.raises(Problem) as error:
        snapshots.manifest(TEAM, AGENT, "upgrade-0002")
    assert error.value.status == 502


@pytest.mark.parametrize("owner", [{"team": "team-two"}, {"tenant": "00000000-0000-4000-8000-000000000000"}])
def test_a_blob_whose_metadata_names_another_owner_is_not_restored(store, owner):
    snapshots, fake = store
    assert save(snapshots).published
    fake.blobs[PREFIX + MANIFEST_NAME] = stored(fake.blobs[PREFIX + MANIFEST_NAME][0], **owner)
    assert snapshots.manifest(TEAM, AGENT, UPGRADE) is None


def test_a_file_rewritten_with_matching_metadata_is_refused_against_the_manifest(store):
    snapshots, fake = store
    assert save(snapshots).published
    fake.blobs[PREFIX + "files/0001"] = stored(b"omega")
    with pytest.raises(Problem) as error:
        snapshots.read_file(snapshots.manifest(TEAM, AGENT, UPGRADE), 1)
    assert error.value.status == 502


def test_a_hash_mismatch_writes_no_manifest(store):
    snapshots, fake = store
    result = save(snapshots, read=lambda entry: b"ALPHA" if entry.path == "docs/a.txt" else source(entry))
    assert (result.published, result.reason) == (False, "NATIVE_FILES_SOURCE_CHANGED")
    assert not any(path.endswith(MANIFEST_NAME) for path in fake.puts())
    assert snapshots.manifest(TEAM, AGENT, UPGRADE) is None


def test_a_source_that_stops_midway_writes_no_manifest(store):
    snapshots, fake = store
    result = save(snapshots, read=lambda entry: None if entry.path == "z.bin" else source(entry))
    assert (result.published, result.reason) == (False, "NATIVE_FILES_SOURCE_UNAVAILABLE")
    assert snapshots.manifest(TEAM, AGENT, UPGRADE) is None


def test_a_crash_after_a_partial_write_leaves_no_manifest_and_the_prefix_is_never_resumed(store):
    snapshots, fake = store
    fake.fail_put = 2
    with pytest.raises(Problem):
        save(snapshots)
    assert list(fake.blobs) == [PREFIX + "files/0001"]
    assert snapshots.manifest(TEAM, AGENT, UPGRADE) is None
    fake.fail_put = None
    with pytest.raises(Problem) as error:
        save(snapshots)
    assert error.value.body["error"]["code"] == "UPGRADE_SNAPSHOT_EXISTS"
    assert snapshots.manifest(TEAM, AGENT, UPGRADE) is None
    assert save(snapshots, upgrade="upgrade-0002").published


def test_a_tampered_file_is_refused_on_restore(store):
    snapshots, fake = store
    assert save(snapshots).published
    _, meta = fake.blobs[PREFIX + "files/0001"]
    fake.blobs[PREFIX + "files/0001"] = (b"omega", meta)
    with pytest.raises(Problem) as error:
        snapshots.read_file(snapshots.manifest(TEAM, AGENT, UPGRADE), 1)
    assert error.value.status == 502


@pytest.mark.parametrize("key", [("../x", AGENT, UPGRADE), (TEAM, "a/b", UPGRADE), (TEAM, AGENT, "..")])
def test_identifiers_cannot_escape_the_snapshot_prefix(store, key):
    snapshots, fake = store
    with pytest.raises(Problem) as error:
        snapshots.save(*key, 0o755, ENTRIES, source)
    assert error.value.status == 422 and fake.requests == []


def test_the_store_has_no_delete_path():
    assert not [name for name in dir(UpgradeSnapshotStore) if "delete" in name.lower()]


# Neal 2026-10-17: snapshots live on their own storage account; never the no-auto-gc sources/backup account.
SNAPSHOT_URL = "https://stsnaptok3n.blob.core.windows.net/upgrade-snapshots"


def _config():
    from shared_context.behavior_server_config import synthetic_configuration
    config = synthetic_configuration()
    backups = config.backups.model_copy(update={
        "account_resource_id": config.backups.account_resource_id.rsplit("/", 1)[0] + "/backupacct",
        "protection": config.backups.protection.model_copy(
            update={"container_url": "https://backupacct.blob.core.windows.net/source-backups"})})
    return config.model_copy(update={"backups": backups})


def test_snapshot_container_is_off_when_unset_and_read_from_its_setting():
    config = _config()
    assert snapshot_container_url(config, {}) is None
    assert snapshot_container_url(config, {CONTAINER_URL_ENV: ""}) is None
    assert snapshot_container_url(config, {CONTAINER_URL_ENV: SNAPSHOT_URL}) == SNAPSHOT_URL


@pytest.mark.parametrize("url", [
    "https://synthetic.blob.core.windows.net/upgrade-snapshots",  # the sources account
    "https://synthetic.blob.core.windows.net/sources",
    "https://backupacct.blob.core.windows.net/upgrade-snapshots",  # the backup account
    "https://backupacct.blob.core.windows.net/source-backups",
])
def test_snapshot_container_refuses_the_sources_and_backup_accounts(url):
    with pytest.raises(ValueError, match="never the sources or backup account"):
        snapshot_container_url(_config(), {CONTAINER_URL_ENV: url})


@pytest.mark.parametrize("url", [
    "http://stsnaptok3n.blob.core.windows.net/upgrade-snapshots",
    "https://stsnaptok3n.blob.core.windows.net/",
    "https://stsnaptok3n.blob.core.windows.net/upgrade-snapshots?sv=2025-01-01&sig=x",
    "https://stsnaptok3n.blob.core.windows.net/upgrade--snapshots",
    "https://stsnaptok3n.example.com/upgrade-snapshots",
    "https://user@stsnaptok3n.blob.core.windows.net/upgrade-snapshots",
])
def test_snapshot_container_refuses_anything_but_an_exact_container_url(url):
    with pytest.raises(ValueError, match="must be https://<account>"):
        snapshot_container_url(_config(), {CONTAINER_URL_ENV: url})


def test_server_startup_stops_on_a_snapshot_url_on_the_sources_account(monkeypatch):
    from shared_context.behavior_server import build_server
    from shared_context.behavior_server_config import synthetic_configuration
    monkeypatch.setenv(CONTAINER_URL_ENV, "https://synthetic.blob.core.windows.net/upgrade-snapshots")
    with pytest.raises(ValueError, match=CONTAINER_URL_ENV):
        build_server(synthetic_configuration(), credential=Credential())