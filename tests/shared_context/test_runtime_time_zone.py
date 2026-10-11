"""The runtime's per-message local time follows only an explicit, plain deployment time zone."""

import asyncio
from pathlib import Path

import httpx
import pytest

from shared_context.m2_copilot import ToolSpec, run_copilot, runtime_time_zone

SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


@pytest.mark.parametrize("value", ["UTC", "Asia/Taipei", "America/Argentina/Buenos_Aires", "Etc/GMT-8"])
def test_plain_iana_names_and_utc_are_forwarded(value):
    assert runtime_time_zone(value) == {"TZ": value}


@pytest.mark.parametrize("value", [
    None, "", "CST-8", ":Asia/Taipei", "/usr/share/zoneinfo/Asia/Taipei", "Asia/../Taipei", "../Asia/Taipei",
    "Asia/Taipei\n", "asia/taipei", "Asia/" + "X" * 64, "Asia Taipei",
])
def test_missing_or_non_plain_values_leave_the_runtime_environment_unchanged(value):
    assert runtime_time_zone(value) == {}


class _Captured(Exception):
    pass


@pytest.mark.parametrize("value,expected", [("Asia/Taipei", "Asia/Taipei"), ("CST-8", None), (None, None)])
def test_runtime_process_receives_only_the_accepted_time_zone(monkeypatch, tmp_path, value, expected):
    captured = []

    class FakeClient:
        def __init__(self, **kwargs):
            captured.append(kwargs["env"])
            raise _Captured()

    monkeypatch.setattr("shared_context.m2_copilot.CopilotClient", FakeClient)
    if value is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", value)
    monkeypatch.setenv("FA_UNRELATED_SECRET", "must-not-reach-runtime")
    work = tmp_path / "work"
    work.mkdir()

    async def unused(_):
        raise AssertionError("no tool is called")

    with pytest.raises(_Captured):
        asyncio.run(run_copilot(
            state_dir=tmp_path / "sdk", working_directory=work, sdk_session_id="time-zone-session", resume=False,
            prompt="OFFLINE", tools=[ToolSpec("read_context", "Read context.", SCHEMA, unused)],
            api_url="https://context.invalid", token_provider=lambda: "OFFLINE", on_event=lambda _: None,
            runtime_path=Path(__file__), http_transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        ))
    env = captured[0]
    assert env.get("TZ") == expected
    assert "FA_UNRELATED_SECRET" not in env
    assert set(env) - {"TZ"} <= {"HOME", "SYSTEMROOT", "WINDIR", "COPILOT_SKIP_CLI_DOWNLOAD",
                                 "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME"}
