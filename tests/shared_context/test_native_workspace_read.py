"""ADR-0009 5b: ``NativeSessionRead(workspace=True)``, the Hosted helper U1 (source) and U4 (destination) read."""

import hashlib

from shared_context.behavior_native_turn_wire import NativeSessionRead, NativeSessionState
from shared_context.native_workspace_files import NativeWorkspaceFiles
from test_native_turns import harness, local_env, native_retry_policy  # noqa: F401


def test_the_workspace_read_answers_the_tree_and_creates_nothing(harness, tmp_path):  # noqa: F811
    plain = NativeSessionState.model_validate_json(harness.invoke(NativeSessionRead()).content)
    assert harness.invoke(NativeSessionRead(workspace=True)).json()["error"]["code"] == "NATIVE_FILES_UNSUPPORTED"
    harness.runtime.workspace_files = workspace = NativeWorkspaceFiles(tmp_path / "home")
    empty = NativeSessionState.model_validate_json(harness.invoke(NativeSessionRead(workspace=True)).content)
    # No root, none made: a restore's publish renames its staged tree into place, which needs the root absent.
    assert not workspace.root.exists() and empty.workspace_entries == () and empty.workspace_root_mode is None
    workspace.root.mkdir(parents=True)
    (workspace.root / "notes").mkdir()
    (workspace.root / "notes" / "a.txt").write_bytes(b"alpha")
    state = NativeSessionState.model_validate_json(harness.invoke(NativeSessionRead(workspace=True)).content)
    assert [(entry.path, entry.kind, entry.sha256) for entry in state.workspace_entries] == [
        ("notes", "directory", None), ("notes/a.txt", "file", hashlib.sha256(b"alpha").hexdigest())]
    assert state.etag == plain.etag  # the same Agent record: the read writes nothing
    # A plain read is byte-for-byte what it was: the new fields are left out.
    assert "workspace_entries" not in harness.invoke(NativeSessionRead()).json()
