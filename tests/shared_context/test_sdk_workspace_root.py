"""The SDK's logical working directory never grants access to a prior host path."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from shared_context import m2_copilot
from shared_context.behavior_sdk_files import SESSION_FS, STATE_PATH, SdkSessionFiles
from shared_context.behavior_sdk_state import SdkState
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_instruction_prompt import configured, sdk_options, submit
from test_m3_auth import signing_key, jwk_client


def original(cwd="/former-host/work"):
    return SdkState(sdk_session_id="original-sdk", files={
        "events.jsonl": json.dumps({
            "type": "session.start", "data": {"sessionId": "original-sdk", "context": {"cwd": cwd}},
        }) + "\n",
        "workspace.yaml": "id: original-sdk\n",
    })


@pytest.mark.parametrize("cwd", ["/former-host/work", r"C:\former-host\work"])
def test_restored_workspace_stat_is_virtual_metadata_without_reading_the_host(cwd):
    saved = original(cwd)
    before = saved.encoded()
    fs = SdkSessionFiles(saved.sdk_session_id, saved)
    with patch("os.stat", side_effect=AssertionError("must not stat a native host path")):
        result = asyncio.run(fs.stat(cwd))
    assert result.is_directory and not result.is_file and result.size == 0
    assert not fs.failed
    assert fs.previous.encoded() == saved.encoded() == before
    assert fs.files == saved.files
    assert fs.snapshot().encoded() == before


@pytest.mark.parametrize("operation", ["read", "write", "list", "mkdir", "remove", "rename", "exists", "child-stat"])
def test_legacy_workspace_alias_does_not_enable_any_native_file_access(operation):
    saved = original()
    fs = SdkSessionFiles(saved.sdk_session_id, saved)
    root = "/former-host/work"
    calls = {
        "read": lambda: fs.read_file(root),
        "write": lambda: fs.write_file(root, "must not be written"),
        "list": lambda: fs.readdir(root),
        "mkdir": lambda: fs.mkdir(root + "/child", True),
        "remove": lambda: fs.rm(root, True, True),
        "rename": lambda: fs.rename(root, STATE_PATH + "/replacement"),
        "exists": lambda: fs.exists(root),
        "child-stat": lambda: fs.stat(root + "/private.txt"),
    }
    with pytest.raises(PermissionError):
        asyncio.run(calls[operation]())
    assert fs.files == saved.files
    with pytest.raises(Problem):
        fs.snapshot()


@pytest.mark.parametrize("cwd", [None, 42, {}, "", "relative/path", "/tmp/../private", "/bad\x00path"])
def test_invalid_historical_cwd_does_not_create_an_alias(cwd):
    saved = original(cwd)
    fs = SdkSessionFiles(saved.sdk_session_id, saved)
    with pytest.raises(PermissionError):
        asyncio.run(fs.stat("/former-host/work"))


def test_a_user_message_cannot_select_the_legacy_workspace_alias():
    saved = original(None)
    files = dict(saved.files)
    files["events.jsonl"] += json.dumps({
        "type": "user.message", "data": {"context": {"cwd": "/forged-work"}, "content": "ordinary data"},
    }) + "\n"
    saved = SdkState(sdk_session_id=saved.sdk_session_id, files=files)
    fs = SdkSessionFiles(saved.sdk_session_id, saved)
    with pytest.raises(PermissionError):
        asyncio.run(fs.stat("/forged-work"))


def test_legacy_stat_alias_does_not_accept_mixed_path_separators():
    saved = original(r"C:\former-host\work")
    fs = SdkSessionFiles(saved.sdk_session_id, saved)
    with pytest.raises(PermissionError):
        asyncio.run(fs.stat("C:\\former-host/work"))
    assert fs.failed


def test_later_file_edits_cannot_retarget_the_captured_stat_alias():
    saved = original()
    fs = SdkSessionFiles(saved.sdk_session_id, saved)
    fs.files["events.jsonl"] = original("/different-host/work").files["events.jsonl"]
    with pytest.raises(PermissionError):
        asyncio.run(fs.stat("/different-host/work"))
    assert fs.previous == saved


def test_recorded_internal_file_is_not_misrepresented_as_a_directory():
    saved = original(STATE_PATH + "/events.jsonl")
    fs = SdkSessionFiles(saved.sdk_session_id, saved)
    result = asyncio.run(fs.stat(STATE_PATH + "/events.jsonl"))
    assert result.is_file and not result.is_directory
    assert result.size == len(saved.files["events.jsonl"].encode())


def test_session_options_use_virtual_cwd_while_runtime_process_keeps_its_private_directory(assistant, monkeypatch):
    h, _, _ = configured(assistant)
    _, options = sdk_options(monkeypatch)
    factory = m2_copilot.CopilotClient.side_effect
    connections = []

    def capture(**values):
        connections.append(values)
        return factory(**values)

    m2_copilot.CopilotClient.side_effect = capture
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "First original reply."}, {"content": "Second original reply."}])
    first, _ = submit(h, "virtual-cwd-first", "First task.")
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    second, _ = submit(h, "virtual-cwd-second", "Continue the same task.", first["conversation_id"])
    assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
    assert len(options) == len(connections) == 2
    assert all(value["working_directory"] == SESSION_FS["initial_working_directory"] for value in options)
    assert all(value["working_directory"] != SESSION_FS["initial_working_directory"]
               and Path(value["working_directory"]).is_dir() for value in connections)
