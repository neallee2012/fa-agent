"""The fixed workspace boundary, with synthetic Session Files writes and no SDK process."""

from pathlib import Path
import hashlib
import json
import os
import stat
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from shared_context.behavior_native_turn_wire import NativeEnvironmentCommand, NativeSessionFollow, canonical_bytes
from shared_context.errors import Problem
from shared_context.native_files_wire import (
    MAX_FILE_BYTES, MAX_FILE_ENTRIES, MAX_MANIFEST_BYTES, MAX_TOTAL_BYTES,
    NativeFileEntry, NativeFilesManifest,
)
from shared_context.native_workspace_files import NativeWorkspaceFiles


SCOPE = dict(agent_id="offline-agent", request_id="same-follow", source_session_id="source-sid",
             source_agent_version="1", target_session_id="target-sid", target_agent_version="2")


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX directory modes")
@pytest.mark.parametrize("step", ["initialize", "prepare"])
def test_new_workspace_directories_stay_private_for_the_shell(tmp_path, step):
    """`mkdir(parents=True)` ignores `mode` for parents: a 0755 `desktop/` failed every later shell bind."""
    from shared_context.behavior_shell_capability import shell_directories
    workspace = NativeWorkspaceFiles(tmp_path)
    previous = os.umask(0o022)
    try:
        if step == "initialize":
            workspace.initialize()
        else:
            workspace.prepare(NativeFilesManifest(**SCOPE, root_mode=0o700, entries=()))
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / ".fa-assistant-v12" / "desktop").stat().st_mode) == 0o700
    shell_directories(SimpleNamespace(root=tmp_path / ".fa-assistant-v12" / "native-turns"))


def test_workspace_publish_preserves_text_binary_hidden_files_and_empty_directories(tmp_path):
    source = NativeWorkspaceFiles(tmp_path / "source")
    destination = NativeWorkspaceFiles(tmp_path / "destination")
    source.root.mkdir(parents=True)
    (source.root / "empty").mkdir()
    (source.root / "nested").mkdir()
    (source.root / "nested" / "uncommitted.txt").write_bytes(b"local edit\n")
    (source.root / ".gitignore").write_bytes(b"scratch\n")
    (source.root / "binary.bin").write_bytes(b"\x00\xff\x80\x01")
    manifest = source.snapshot(
        agent_id="offline-agent", request_id="same-follow", source_session_id="source-sid",
        source_agent_version="1", target_session_id="target-sid", target_agent_version="2")
    destination.prepare(manifest)
    for index, entry in enumerate(manifest.entries):
        if entry.kind == "file":
            destination.staging_path(manifest, index).write_bytes(
                (source.root / Path(entry.path)).read_bytes())
        destination.confirm(manifest, index)
    assert not destination.root.exists()
    source.verify(manifest)
    destination.publish(manifest)
    destination.verify(manifest)
    assert (destination.root / "empty").is_dir()
    assert (destination.root / "nested" / "uncommitted.txt").read_bytes() == b"local edit\n"
    assert (destination.root / "binary.bin").read_bytes() == b"\x00\xff\x80\x01"
    assert (destination.root / ".gitignore").read_bytes() == b"scratch\n"
    destination.publish(manifest)


@pytest.mark.parametrize("path", [
    "../escape", "/absolute", "C:/outside", "nested\\file", "./alias", "double//alias", "tail/", "a/../b",
])
def test_manifest_rejects_noncanonical_or_escaping_paths(path):
    with pytest.raises(ValidationError):
        NativeFileEntry(path=path, kind="directory", mode=0o700)


@pytest.mark.parametrize("relative,data", [
    (".env", b"secret=synthetic"), (".ssh/config", b"Host example"),
    ("profile/cookies.sqlite", b"synthetic"), (".git-credentials", b"synthetic"),
    (".azure/config", b"synthetic"), ("native-turns/state", b"private raw state"),
    ("private.txt", b"-----BEGIN PRIVATE KEY-----\nsynthetic"),
    (".git", b"gitdir: ../outside"),
    (".git/objects/info/alternates", b"../../other-objects"),
    (".git/config", b'[include]\npath=../../credentials\n'),
    (".git/config", b'[credential]\nhelper=synthetic-helper\n'),
    (".git/config", b'[remote "origin"]\nurl=https://synthetic-user:synthetic-pass@example.invalid/repo\n'),
    (".git/config", b'[remote "origin"]\npushurl=https://synthetic-user:synthetic-pass@example.invalid/repo\n'),
])
def test_sensitive_or_external_workspace_is_refused_not_silently_skipped(tmp_path, relative, data):
    source = NativeWorkspaceFiles(tmp_path)
    path = source.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    with pytest.raises(Problem) as error:
        source.snapshot(**SCOPE)
    assert error.value.body["error"]["code"] == "NATIVE_FILES_UNSAFE"
    assert path.read_bytes() == data


def test_git_objects_refs_index_and_uncommitted_dotfiles_are_included(tmp_path):
    source = NativeWorkspaceFiles(tmp_path)
    files = {
        ".git/config": b'[core]\nrepositoryformatversion=0\n[remote "origin"]\nurl=https://example.invalid/repo\n',
        ".git/HEAD": b"ref: refs/heads/main\n", ".git/refs/heads/main": b"0" * 40,
        ".git/objects/ab/cdef": b"\x00\xff\x01", ".git/index": b"DIRC\x00\x01",
        ".gitignore": b"scratch\n", ".hidden": b"uncommitted",
    }
    for relative, data in files.items():
        path = source.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = source.snapshot(**SCOPE)
    assert {entry.path for entry in manifest.entries if entry.kind == "file"} == set(files)


def test_hardlinked_file_is_not_downloaded_as_regular_private_material(tmp_path):
    source = NativeWorkspaceFiles(tmp_path)
    source.root.mkdir(parents=True)
    (source.root / "one").write_bytes(b"synthetic")
    os.link(source.root / "one", source.root / "two")
    with pytest.raises(Problem) as error:
        source.snapshot(**SCOPE)
    assert error.value.body["error"]["code"] == "NATIVE_FILES_UNSAFE"


@pytest.mark.parametrize("kind", ["link", "fifo", "device", "unknown-mode"])
def test_source_type_or_mode_requires_real_supported_metadata(tmp_path, monkeypatch, kind):
    source = NativeWorkspaceFiles(tmp_path)
    source.root.mkdir(parents=True)
    path = source.root / "unsupported"
    path.write_bytes(b"synthetic")
    original = Path.lstat
    if kind == "link":
        original_link = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda node: node == path or original_link(node))
    else:
        original_info = path.stat()
        mode = {"fifo": stat.S_IFIFO, "device": stat.S_IFCHR, "unknown-mode": stat.S_IFREG | stat.S_ISUID}[kind]
        monkeypatch.setattr(Path, "lstat", lambda node: SimpleNamespace(
            st_mode=mode, st_nlink=1, st_file_attributes=0, st_size=original_info.st_size)
            if node == path else original(node))
    with pytest.raises(Problem) as error:
        source.snapshot(**SCOPE)
    assert error.value.body["error"]["code"] == "NATIVE_FILES_UNSAFE"


def test_missing_source_is_not_an_empty_workspace(tmp_path):
    with pytest.raises(Problem) as error:
        NativeWorkspaceFiles(tmp_path).snapshot(**SCOPE)
    assert error.value.body["error"]["code"] == "NATIVE_FILES_UNAVAILABLE"


def test_all_four_exact_manifest_bounds():
    assert (MAX_FILE_ENTRIES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, MAX_MANIFEST_BYTES) == (
        2000, 32 * 1024 * 1024, 64 * 1024 * 1024, 256 * 1024)
    directories = tuple(NativeFileEntry(path=f"d{n:04}", kind="directory", mode=0o700) for n in range(2000))
    assert len(NativeFilesManifest(**SCOPE, root_mode=0o700, entries=directories).entries) == 2000
    with pytest.raises(ValidationError):
        NativeFilesManifest(**SCOPE, root_mode=0o700, entries=directories + (
            NativeFileEntry(path="extra", kind="directory", mode=0o700),))
    first = NativeFileEntry(path="a", kind="file", mode=0o600, byte_length=32 * 1024 * 1024, sha256="a" * 64)
    second = first.model_copy(update={"path": "b"})
    accepted = NativeFilesManifest(**SCOPE, root_mode=0o700, entries=(first, second))
    assert sum(entry.byte_length for entry in accepted.entries) == 64 * 1024 * 1024
    with pytest.raises(ValidationError):
        NativeFileEntry(path="a", kind="file", mode=0o600, byte_length=32 * 1024 * 1024 + 1, sha256="a" * 64)
    with pytest.raises(ValidationError):
        NativeFilesManifest(**SCOPE, root_mode=0o700, entries=(first, second, NativeFileEntry(
            path="c", kind="file", mode=0o600, byte_length=1, sha256="c" * 64)))

    body = {**SCOPE, "profile": "fa-workspace-files-v1", "root": ".fa-assistant-v12/desktop/files",
            "root_mode": 0o700, "entries": [entry.model_dump() for entry in directories[:300]]}
    encode = lambda: json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    remaining = 256 * 1024 - len(encode())
    for entry in body["entries"]:
        count = min(remaining, 1010)
        entry["path"] += "a" * count
        remaining -= count
    assert remaining == 0 and len(encode()) == 256 * 1024
    assert len(NativeFilesManifest.model_validate_json(encode()).encoded()) == 256 * 1024
    body["entries"][-1]["path"] += "z"
    assert len(encode()) == 256 * 1024 + 1
    with pytest.raises(ValidationError):
        NativeFilesManifest.model_validate_json(encode())


def test_real_stream_at_32_mib_and_complete_64_mib_are_not_truncated(tmp_path):
    source = NativeWorkspaceFiles(tmp_path)
    source.root.mkdir(parents=True)
    for name in ("a", "b"):
        with (source.root / name).open("wb") as stream:
            stream.truncate(32 * 1024 * 1024)
    manifest = source.snapshot(**SCOPE)
    assert [entry.byte_length for entry in manifest.entries] == [33554432, 33554432]
    assert all(entry.sha256 == hashlib.sha256(bytes(33554432)).hexdigest() for entry in manifest.entries)
    with (source.root / "a").open("ab") as stream:
        stream.write(b"x")
    with pytest.raises(Problem) as error:
        source.snapshot(**SCOPE)
    assert error.value.body["error"]["code"] == "NATIVE_FILES_LIMIT"


def test_source_change_destination_conflict_and_late_upload_cannot_replace_live_files(tmp_path):
    source, target = NativeWorkspaceFiles(tmp_path / "source"), NativeWorkspaceFiles(tmp_path / "target")
    source.root.mkdir(parents=True)
    (source.root / "work").write_bytes(b"original")
    manifest = source.snapshot(**SCOPE)
    target.prepare(manifest)
    upload = target.staging_path(manifest, 0)
    upload.write_bytes(b"original")
    target.confirm(manifest, 0)
    (source.root / "work").write_bytes(b"changed")
    with pytest.raises(Problem) as error:
        source.verify(manifest)
    assert error.value.body["error"]["code"] == "NATIVE_FILES_SOURCE_CHANGED"
    target.publish(manifest)
    upload.write_bytes(b"late PUT")
    assert (target.root / "work").read_bytes() == b"original"
    (target.root / "work").write_bytes(b"later user edit")
    with pytest.raises(Problem):
        target.publish(manifest)
    assert (target.root / "work").read_bytes() == b"later user edit"


def test_empty_directory_tree_publish_can_be_observed_without_recopy(tmp_path):
    source, target = NativeWorkspaceFiles(tmp_path / "source"), NativeWorkspaceFiles(tmp_path / "target")
    source.root.mkdir(parents=True)
    manifest = source.snapshot(**SCOPE)
    target.prepare(manifest)
    target.publish(manifest)
    assert target.published(manifest)
    target.publish(manifest)


def test_legacy_follow_commands_keep_original_serialized_bytes():
    command = NativeEnvironmentCommand(request_id="rid", expected_agent_version="2")
    assert canonical_bytes(command) == b'{"expected_agent_version":"2","request_id":"rid"}'
    internal = NativeSessionFollow(request_id="rid", expected_session_id="source", expected_etag="etag", stage="prepare")
    assert canonical_bytes(internal) == (
        b'{"expected_etag":"etag","expected_session_id":"source","operation":"session_follow",'
        b'"request_id":"rid","stage":"prepare"}')


@pytest.mark.parametrize("fault", ["empty-continuation", "duplicate", "escape", "missing"])
def test_official_file_pages_are_bounded_and_must_cover_the_manifest(monkeypatch, fault):
    from azure.ai.projects.models import SessionDirectoryEntry
    from azure.core.paging import ItemPaged
    from shared_context.behavior_session_files import SessionFiles
    from shared_context.native_files_wire import NativeFilesListing, advance_listing
    from test_native_session_prepare_api import project_client

    entry = NativeFileEntry(path="a", kind="file", mode=0o600, byte_length=1, sha256="a" * 64)
    manifest = NativeFilesManifest(**SCOPE, root_mode=0o700, entries=(entry,))
    calls = []

    def next_page(cursor=None):
        calls.append(cursor)
        assert len(calls) <= 2
        if fault == "empty-continuation":
            return "same", []
        if fault == "missing":
            return None, []
        item = SessionDirectoryEntry({"name": "../escape" if fault == "escape" else "a",
                                      "is_directory": False, "size": 1, "modified_time": 0})
        return ("same" if fault == "duplicate" else None), [item]

    project_client(monkeypatch, list_session_files=lambda *args, **kwargs:
                   ItemPaged(next_page, lambda value: (value[0], iter(value[1]))))
    client = SimpleNamespace(target=SimpleNamespace(project_endpoint="https://offline.invalid", agent_name="offline"),
                             credential=object())
    with pytest.raises((Problem, ValueError)):
        listing = NativeFilesListing()
        while not listing.complete:
            result = SessionFiles(client).list_page(manifest, listing)
            listing = advance_listing(manifest, listing, result)
    assert len(calls) <= 2


@pytest.mark.parametrize("chunks", [(b"a", b"b"), (b"",), (b"b",)])
def test_official_download_rejects_excess_incomplete_or_different_bytes(monkeypatch, chunks):
    from shared_context.behavior_session_files import SessionFiles
    from test_native_session_prepare_api import project_client

    entry = NativeFileEntry(path="a", kind="file", mode=0o600, byte_length=1,
                            sha256=hashlib.sha256(b"a").hexdigest())
    project_client(monkeypatch, download_session_file=lambda *args, **kwargs: iter(chunks))
    client = SimpleNamespace(target=SimpleNamespace(project_endpoint="https://offline.invalid", agent_name="offline"),
                             credential=object())
    with pytest.raises(Problem) as error:
        SessionFiles(client).download("original-sid", ".fa-assistant-v12/desktop/files/a", entry)
    assert error.value.body["error"]["code"] == "NATIVE_FILES_CONFLICT"
