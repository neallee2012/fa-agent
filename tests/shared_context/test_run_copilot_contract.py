"""run_copilot's refusals: which input fails, in which order, and that nothing has started when it does."""
import asyncio
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from copilot import CopilotClient

from shared_context import m2_copilot
from shared_context.m2_copilot import ResidentHolder, ToolSpec


def tool(name="lookup", parameters=None):
    async def handler(_packet):
        raise AssertionError("no tool runs in a refused turn")
    return ToolSpec(name, "Fixture tool", {"type": "object"} if parameters is None else parameters, handler)


async def async_event(_event):
    return None


# In run_copilot's check order; a case must keep failing when the next case's fault is added to it.
REFUSALS = [
    ("timeout-zero", {"timeout_seconds": 0}, ValueError, "timeout_seconds must be positive and finite"),
    ("timeout-infinite", {"timeout_seconds": math.inf}, ValueError, "timeout_seconds must be positive and finite"),
    ("session-id", {"sdk_session_id": "../escape"}, ValueError, "Invalid sdk_session_id"),
    ("blank-prompt", {"prompt": "  "}, ValueError, "A nonempty prompt is required"),
    ("async-on-event", {"on_event": async_event}, TypeError, "on_event must be synchronous"),
    ("untyped-boundary", {"behavior_boundary": object()}, ValueError, "typed boundary and SDK 1.0.13"),
    ("instruction-without-boundary", {"instruction": True}, ValueError, "authority-backed model transport"),
    ("call-limit-without-instruction", {"model_call_limit": 5}, ValueError, "A model call limit requires"),
    ("unknown-provider", {"model_provider": "other"}, ValueError, "public reasoning summary"),
    ("summary-without-provider", {"reasoning_summary": "detailed"}, ValueError, "public reasoning summary"),
    ("untyped-capabilities", {"capability_session": object()}, ValueError, "come from the captured grants"),
    ("untyped-delegation", {"delegation_runtime": object()}, ValueError, "captured fixed child"),
    ("untyped-resident", {"resident": object()}, ValueError, "A resident SDK needs"),
    ("untyped-attachments", {"attached_text": object()}, ValueError, "verified instruction source attachments"),
    ("untyped-user-files", {"user_text_files": ("file",)}, ValueError, "Literal file input"),
    ("untyped-images", {"user_images": (("a.png", "AA=="),)}, ValueError, "Attached images"),
    ("untyped-agent-source", {"agent_source": object()}, ValueError, "Agent provenance"),
    ("untyped-routine-source", {"routine_source": object()}, ValueError, "routine provenance"),
    ("untyped-capacity", {"model_capacity": object()}, ValueError, "Model capacity requires"),
    ("untyped-session-files", {"session_files": object()}, ValueError, "SDK filesystem must belong"),
    ("checkpoint-without-files", {"checkpoint_task": SimpleNamespace(instruction=None)}, ValueError,
     "server-owned task profile"),
    ("untyped-native-checkpoint", {"native_checkpoint_turn": object()}, ValueError, "original RID"),
    ("untyped-start-gate", {"sdk_start_gate": object()}, ValueError, "SDK entry gate"),
    ("unknown-milestone", {"milestone": "m9"}, ValueError, "model milestone"),
    ("missing-runtime", {"runtime_path": Path("missing-runtime")}, FileNotFoundError, "."),
    ("directory-runtime", {"runtime_path": Path(".")}, ValueError, "preprovisioned runtime binary"),
    ("plain-http-api", {"api_url": "http://offline.invalid"}, ValueError, "trusted HTTPS base URL"),
    ("api-with-query", {"api_url": "https://offline.invalid/?key=value"}, ValueError, "trusted HTTPS base URL"),
    ("invalid-tool-name", {"tools": [tool("bad-name")]}, ValueError, "Invalid custom tool name"),
    ("duplicate-tool", {"tools": [tool(), tool()]}, ValueError, "Duplicate custom tool name"),
    ("non-object-schema", {"tools": [tool(parameters={"type": "string"})]}, ValueError, "object JSON schema"),
    ("missing-working-directory", {"working_directory": Path("missing-working-directory")}, FileNotFoundError, "."),
]


@pytest.fixture
def turn(tmp_path, monkeypatch):
    factory = create_autospec(CopilotClient)
    monkeypatch.setattr(m2_copilot, "CopilotClient", factory)
    work = tmp_path / "work"
    work.mkdir()
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"offline fixture")
    return SimpleNamespace(factory=factory, state_dir=tmp_path / "sdk", options=dict(
        state_dir=tmp_path / "sdk", working_directory=work, sdk_session_id="contract", resume=False,
        prompt="Fixture prompt", tools=[], api_url="https://offline.invalid", token_provider=lambda: "token",
        on_event=lambda _event: None, runtime_path=runtime,
    ))


def assert_refused(turn, overrides, exception, message):
    with pytest.raises(exception, match=message):
        asyncio.run(m2_copilot.run_copilot(**{**turn.options, **overrides}))
    assert not turn.state_dir.exists()
    turn.factory.assert_not_called()


@pytest.mark.parametrize("overrides, exception, message", [case[1:] for case in REFUSALS], ids=[case[0] for case in REFUSALS])
def test_a_bad_turn_is_refused_before_anything_starts(turn, overrides, exception, message):
    assert_refused(turn, overrides, exception, message)


FIRST_FAULT_WINS = [
    (earlier, later) for earlier, later in zip(REFUSALS, REFUSALS[1:]) if not earlier[1].keys() & later[1].keys()
]


@pytest.mark.parametrize("earlier, later", FIRST_FAULT_WINS, ids=[f"{a[0]}-before-{b[0]}" for a, b in FIRST_FAULT_WINS])
def test_the_first_fault_in_check_order_decides_the_refusal(turn, earlier, later):
    assert_refused(turn, {**later[1], **earlier[1]}, earlier[2], earlier[3])


def test_only_the_original_instruction_can_keep_a_resident_sdk(turn):
    assert_refused(turn, {"resident": ResidentHolder(), "timeout_seconds": None}, ValueError, "A resident SDK needs")
