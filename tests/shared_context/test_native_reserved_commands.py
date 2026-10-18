"""ADR-0009 phase 4 U3/U9 (G0b): two commands reserved in the v2 wire before release 31; this image refuses both.

The destination session runs them before it is the current SID, so neither is a ``_RECORD_READS`` operation. Until
an image implements them, Hosted answers ``NATIVE_REQUEST_INVALID`` and opens no State store.
"""

import pytest

from shared_context.behavior_native_turn_wire import NativeSessionCheckpointCheck, NativeSessionPrerestore
from shared_context.native_turns import _RECORD_READS
from test_native_profile_snapshot import _v2_baseline
from test_native_turns import harness, local_env, native_retry_policy  # noqa: F401

RESERVED = (NativeSessionCheckpointCheck(), NativeSessionPrerestore())


def test_the_v2_baseline_holds_both_commands_outside_the_record_reads():
    mapping = _v2_baseline()["native_request"]["discriminator"]["mapping"]
    for command in RESERVED:
        assert command.operation in mapping and command.operation not in _RECORD_READS


@pytest.mark.parametrize("command", RESERVED, ids=lambda command: command.operation)
def test_a_reserved_command_is_refused_and_reads_nothing(harness, monkeypatch, command):  # noqa: F811
    opened, store = [], harness.runtime._store
    monkeypatch.setattr(harness.runtime, "_store", lambda *args, **kwargs: opened.append(args) or store(*args, **kwargs))
    response = harness.invoke(command)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "NATIVE_REQUEST_INVALID" and "reserved" in error["message"]
    assert opened == [] and harness.sdk.calls == []
