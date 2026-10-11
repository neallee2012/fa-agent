"""Profile routing is explicit; original Hosted defaults stay unchanged."""
import os
from unittest.mock import Mock

import pytest

import behavior_server_main as entry


def test_checksum_bootstrap_profile_remains_the_default(monkeypatch):
    monkeypatch.delenv("BEHAVIOR_ASSISTANT_PROFILE", raising=False)
    assert entry._hosted_profile("checksum-count-v1") == (
        ".fa-behavior-checksum-v1", ["-m", "shared_context.behavior_hosted", "serve"],
    )


def test_assistant_bootstrap_requires_opt_in_and_preserves_canonical_module_identity(monkeypatch):
    monkeypatch.delenv("BEHAVIOR_ASSISTANT_PROFILE", raising=False)
    with pytest.raises(ValueError):
        entry._hosted_profile("assistant-v1.2")
    monkeypatch.setenv("BEHAVIOR_ASSISTANT_PROFILE", "assistant-v1.2")
    directory, command = entry._hosted_profile("assistant-v1.2")
    assert directory == ".fa-assistant-v12"
    assert command == ["-c", "import importlib; importlib.import_module('behavior_assistant_main').main()"]
    assert "runpy" not in command[-1]


@pytest.mark.parametrize("profile", ["../../other", "latest", "assistant-v1.3"])
def test_unknown_profile_cannot_choose_a_private_directory_or_executable(profile, monkeypatch):
    monkeypatch.setattr(os, "open", Mock(side_effect=AssertionError("no privileged I/O")))
    with pytest.raises(ValueError):
        entry.hosted_bootstrap(profile)
