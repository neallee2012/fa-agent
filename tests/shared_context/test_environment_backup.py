import hashlib
import gzip
import io
import json
import os
import tarfile
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from shared_context.errors import Problem
from test_environment import A_ID, external_tools, private_home, request
from test_hosted import client


class BackupEndpoint:
    """External HTTP storage endpoint substitute, not a substitute for archive logic."""

    def __init__(self):
        self.objects = {}
        self.downloads = []

    def __call__(self, call):
        backup_id = call.url.path.rsplit("/", 1)[-1]
        assert call.headers["authorization"].startswith("Bearer ")
        if call.method == "PUT":
            if backup_id in self.objects:
                return httpx.Response(409, json={"error": {"code": "BACKUP_EXISTS"}})
            content = call.content
            assert call.headers["x-backup-sha256"] == hashlib.sha256(content).hexdigest()
            receipt = {
                "backup_id": backup_id, "owner_oid": A_ID, "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content), "version_id": "2026-09-14T00:00:00.0000000Z", "etag": '"0x123"',
            }
            self.objects[backup_id] = (content, receipt)
            return httpx.Response(201, json=receipt)
        assert call.method == "GET"
        self.downloads.append(backup_id)
        if backup_id not in self.objects:
            return httpx.Response(404, json={"error": {"code": "BACKUP_NOT_FOUND"}})
        content, receipt = self.objects[backup_id]
        assert call.url.params["version_id"] == receipt["version_id"]
        assert call.headers["if-match"] == receipt["etag"]
        return httpx.Response(200, content=content, headers={
            "X-Backup-SHA256": receipt["sha256"], "X-Backup-Version-Id": receipt["version_id"],
            "ETag": receipt["etag"],
        })


def test_private_environment_restores_from_cloud_into_a_new_session_and_version(private_home, external_tools, monkeypatch):
    remote = BackupEndpoint()
    hosted, credential = client(remote)
    seeded = hosted.dispatch(request("seed", nonce="amber-v1"))["outcome"]
    backup = hosted.dispatch(request(
        "backup", backup_id="1" * 32, checkpoint_sha256=seeded["checkpoint_sha256"],
    ))["outcome"]["backup"]
    replacement = private_home / "replacement"
    replacement.mkdir()
    monkeypatch.setenv("HOME", str(replacement))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "replacement-session")
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", "6")
    assert hosted.dispatch(request("inspect"))["outcome"]["seeded"] is False
    with pytest.raises(Problem) as missing:
        hosted.dispatch(request("read", checkpoint_sha256=seeded["checkpoint_sha256"]))
    assert missing.value.status == 404
    restored = hosted.dispatch(request("restore", backup=backup))["outcome"]
    recalled = hosted.dispatch(request("read", checkpoint_sha256=restored["checkpoint_sha256"]))["outcome"]
    assert recalled["files"] == seeded["files"]
    assert recalled["profile"]["marker_sha256"] == seeded["profile"]["marker_sha256"]
    assert recalled["profile"]["accept_language"].startswith("zh-TW")
    assert recalled["settings_applied"] == {"python_utf8": 1}
    assert recalled["binding"]["session_id"] == "replacement-session"
    assert recalled["binding"]["agent_version"] == "6"
    assert Path(recalled["private_root"]).is_relative_to(replacement)
    assert restored["source_binding"] == seeded["binding"]
    assert restored["checkpoint_sha256"] != seeded["checkpoint_sha256"]
    assert remote.downloads == ["1" * 32]
    assert external_tools["seed_markers"][1] is None
    assert len(credential.scopes) == 2


def test_backup_restore_uses_the_real_invocations_http_adapter(private_home, external_tools, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    import hosted_main

    remote = BackupEndpoint()
    monkeypatch.setattr(hosted_main, "client", client(remote)[0])
    with TestClient(hosted_main.app) as http:
        source = "/invocations?agent_session_id=synthetic-session-a"
        seeded = http.post(source, json=request("seed", nonce="amber-v1"))
        assert seeded.status_code == 200
        checkpoint = seeded.json()["outcome"]["checkpoint_sha256"]
        saved = http.post(source, json=request("backup", backup_id="2" * 32, checkpoint_sha256=checkpoint))
        assert saved.status_code == 200
        receipt = saved.json()["outcome"]["backup"]
        destination = replacement_home(private_home, monkeypatch)
        monkeypatch.setenv("FOUNDRY_AGENT_VERSION", "6")
        target = "/invocations?agent_session_id=replacement-session"
        restored = http.post(target, json=request("restore", backup=receipt))
        assert restored.status_code == 200
        outcome = restored.json()["outcome"]
        assert outcome["binding"]["session_id"] == "replacement-session"
        assert outcome["source_binding"] == seeded.json()["outcome"]["binding"]
        assert Path(outcome["private_root"]).is_relative_to(destination)
        recalled = http.post(target, json=request("read", checkpoint_sha256=outcome["checkpoint_sha256"]))
        assert recalled.status_code == 200
        assert recalled.json()["outcome"]["files"] == seeded.json()["outcome"]["files"]
        refused = http.post(target, json=request("restore", backup=receipt))
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "ENVIRONMENT_ALREADY_SEEDED"


def test_malformed_cloud_archive_is_rejected_before_creating_destination_files(private_home, external_tools, monkeypatch):
    remote = BackupEndpoint()
    hosted, _ = client(remote)
    seeded = hosted.dispatch(request("seed", nonce="amber-v1"))["outcome"]
    backup = hosted.dispatch(request(
        "backup", backup_id="1" * 32, checkpoint_sha256=seeded["checkpoint_sha256"],
    ))["outcome"]["backup"]
    malformed = b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"\xff" * 20
    backup = {**backup, "bytes": len(malformed), "sha256": hashlib.sha256(malformed).hexdigest()}
    remote.objects[backup["backup_id"]] = (malformed, backup)
    replacement = private_home / "replacement"
    replacement.mkdir()
    monkeypatch.setenv("HOME", str(replacement))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "replacement-session")
    with pytest.raises(Problem) as invalid:
        hosted.dispatch(request("restore", backup=backup))
    assert invalid.value.status == 409
    assert list(replacement.iterdir()) == []


@pytest.fixture
def cloud_backup(private_home, external_tools):
    remote = BackupEndpoint()
    hosted, _ = client(remote)
    seeded = hosted.dispatch(request("seed", nonce="amber-v1"))["outcome"]
    backup = hosted.dispatch(request(
        "backup", backup_id="1" * 32, checkpoint_sha256=seeded["checkpoint_sha256"],
    ))["outcome"]["backup"]
    return hosted, remote, seeded, backup


def replacement_home(private_home, monkeypatch):
    destination = private_home / "replacement"
    destination.mkdir()
    monkeypatch.setenv("HOME", str(destination))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "replacement-session")
    return destination


def install_cloud_fixture(remote, receipt, data):
    receipt = {**receipt, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    remote.objects[receipt["backup_id"]] = (data, receipt)
    return receipt


def test_restore_never_downloads_or_overwrites_an_existing_run(cloud_backup):
    hosted, remote, seeded, receipt = cloud_backup
    with pytest.raises(Problem) as duplicate:
        hosted.dispatch(request("restore", backup=receipt))
    assert duplicate.value.status == 409
    assert remote.downloads == []
    recalled = hosted.dispatch(request("read", checkpoint_sha256=seeded["checkpoint_sha256"]))["outcome"]
    assert recalled["files"] == seeded["files"]
    assert recalled["profile"]["marker_sha256"] == seeded["profile"]["marker_sha256"]


def test_backup_collision_does_not_replace_the_committed_cloud_object(cloud_backup):
    hosted, remote, seeded, receipt = cloud_backup
    original = remote.objects[receipt["backup_id"]]
    with pytest.raises(Problem) as duplicate:
        hosted.dispatch(request("backup", backup_id=receipt["backup_id"], checkpoint_sha256=seeded["checkpoint_sha256"]))
    assert duplicate.value.status == 409
    assert remote.objects[receipt["backup_id"]] == original


def test_wrong_retained_hash_rejects_cloud_data_before_local_writes(cloud_backup, private_home, monkeypatch):
    hosted, _, _, receipt = cloud_backup
    destination = replacement_home(private_home, monkeypatch)
    with pytest.raises(Problem) as changed:
        hosted.dispatch(request("restore", backup={**receipt, "sha256": "0" * 64}))
    assert changed.value.status == 409
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("kind", ["traversal", "absolute", "symlink", "hardlink", "device", "setuid", "oversized"])
def test_unsafe_archive_members_never_escape_or_create_a_private_run(cloud_backup, private_home, monkeypatch, kind):
    hosted, remote, _, receipt = cloud_backup
    destination = replacement_home(private_home, monkeypatch)
    member = tarfile.TarInfo("profile/fixture")
    member.mode = 0o600
    if kind == "traversal":
        member.name = "../escape"
    elif kind == "absolute":
        member.name = "/escape"
    elif kind in ("symlink", "hardlink"):
        member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
        member.linkname = "../escape"
    elif kind == "device":
        member.type = tarfile.CHRTYPE
    elif kind == "setuid":
        member.mode = 0o4600
    else:
        member.size = 64 * 1024 * 1024 + 1
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        archive.addfile(member)
    receipt = install_cloud_fixture(remote, receipt, gzip.compress(raw.getvalue()))
    with pytest.raises(Problem) as unsafe:
        hosted.dispatch(request("restore", backup=receipt))
    assert unsafe.value.status == (413 if kind == "oversized" else 409)
    assert list(destination.iterdir()) == []


def test_source_agent_binding_is_checked_before_restore(cloud_backup, private_home, monkeypatch):
    hosted, remote, _, receipt = cloud_backup
    destination = replacement_home(private_home, monkeypatch)
    content, _ = remote.objects[receipt["backup_id"]]
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(content)), mode="r:") as original:
        with tarfile.open(fileobj=output, mode="w") as modified:
            for item in original:
                data = original.extractfile(item).read()
                if item.name == "archive-manifest.json":
                    manifest = json.loads(data)
                    manifest["binding"]["agent_id"] = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
                    data = json.dumps(manifest).encode()
                    item.size = len(data)
                modified.addfile(item, io.BytesIO(data))
    receipt = install_cloud_fixture(remote, receipt, gzip.compress(output.getvalue()))
    with pytest.raises(Problem) as forbidden:
        hosted.dispatch(request("restore", backup=receipt))
    assert forbidden.value.status == 403
    assert list(destination.iterdir()) == []


def test_restore_disk_failure_keeps_source_and_does_not_publish_checkpoint(cloud_backup, private_home, monkeypatch):
    hosted, _, seeded, receipt = cloud_backup
    original_work = (Path(seeded["private_root"]) / "work.txt").read_bytes()
    destination = replacement_home(private_home, monkeypatch)

    def full_disk(_descriptor):
        raise OSError(28, "synthetic full disk")

    monkeypatch.setattr(os, "fsync", full_disk)
    with pytest.raises(Problem) as failed:
        hosted.dispatch(request("restore", backup=receipt))
    assert failed.value.status == 503
    assert not list(destination.rglob("checkpoint.json"))
    assert (Path(seeded["private_root"]) / "work.txt").read_bytes() == original_work
    with pytest.raises(Problem) as duplicate:
        hosted.dispatch(request("restore", backup=receipt))
    assert duplicate.value.status == 409
