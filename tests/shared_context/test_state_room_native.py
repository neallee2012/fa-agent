"""Pinned SDK 1.0.13 / runtime 1.0.83: screenshots near the SDK state bound are refused before they run.

The SDK keeps every screenshot's base64 in events.jsonl. Without the refusal, about 8 MiB of screenshots makes
the SDK filesystem write fail: the turn's answer is lost and no checkpoint can be saved, so the conversation
cannot continue. Offline Playwright stand-in and scripted provider; no browser, network or model.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from shared_context.backup_models import MAX_BACKUP_BYTES
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_capabilities import CapabilityCapture, CapabilityGrant
from shared_context.behavior_capability_prompt import session_needs
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.m2_copilot import (
    FILENAME_FEEDBACK, FULL_PAGE_FEEDBACK, PARALLEL_FEEDBACK, STATE_FULL_FEEDBACK, run_copilot,
)
from shared_context.tool_catalog import StdioEntry
from sdk_checkpoint_local import sse
from test_m2_copilot import runtime_path, workspace

SERVER = Path(__file__).with_name("fake_playwright_stdio_server.py")
TOOL = "playwright-browser_take_screenshot"
SHOTS = 10


def _run(workspace, runtime_path, batches, side="480"):
    """Each batch is one assistant message with that many screenshot calls; returns what ran and what was saved."""
    calls = workspace / "screenshot-calls.txt"
    env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP") if name in os.environ}
    # At side 480, about 0.9 MB of base64 per screenshot in events.jsonl; ten of them exceed the 8 MiB bound.
    env.update(FAKE_PLAYWRIGHT_SCREENSHOT_SIDE=side, FAKE_PLAYWRIGHT_CALL_LOG=str(calls))
    grant = CapabilityGrant(entry_id="playwright", entry_sha256="0" * 64, kind="mcp-hosted", tools=(TOOL,),
                            vision=False, launch=StdioEntry(type="stdio", command=sys.executable,
                                                            args=(str(SERVER),), env=tuple(sorted(env.items()))))
    session = session_needs(CapabilityCapture(head_selection_id=None, grants=(grant,)))
    requests = []

    def respond(request):
        requests.append(request.content.decode("utf-8"))
        index = len(requests)
        if index <= len(batches):
            return sse(request, {"tool_calls": [
                {"index": slot, "id": f"shot-{index}-{slot}", "type": "function",
                 "function": {"name": TOOL, "arguments": arguments}}
                for slot, arguments in enumerate(batches[index - 1])]}, index)
        return sse(request, {"content": "FINAL ANSWER with the screenshots taken"}, index)

    async def reserve(_):
        pass

    async def current():
        return True
    sid = "state-room-session"
    files = SdkSessionFiles(sid)
    events = []
    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work", sdk_session_id=sid, resume=False,
        prompt="Take screenshots.", tools=[], api_url="https://offline.invalid", token_provider=lambda: "OFFLINE",
        on_event=events.append, timeout_seconds=None, runtime_path=runtime_path,
        http_transport=httpx.MockTransport(respond), behavior_boundary=AssistantModelBoundary(reserve, text_reply=True),
        instruction=True, session_files=files, system_message="PRIVATE_SYSTEM_MARKER_STATE_ROOM",
        capability_session=session, capability_current=current,
    ))
    assert result["final_message"] == "FINAL ANSWER with the screenshots taken"
    # With a boundary, events keep only their type; the stand-in's call log shows which screenshots actually ran.
    assert sum(1 for e in events if e["type"] == "m2.permission") == sum(len(batch) for batch in batches)
    ran = calls.read_text(encoding="utf-8").splitlines()
    assert ran == ["screenshot fullPage=False"] * len(ran), "a refused screenshot still reached the browser"
    assert not files.failed
    assert sum(len(content.encode("utf-8")) for content in files.files.values()) <= MAX_BACKUP_BYTES
    assert len(files.snapshot().encoded()) <= MAX_BACKUP_BYTES
    return len(ran), requests


def test_screenshots_near_the_state_bound_are_refused_and_the_turn_is_saved(workspace, runtime_path):
    approved, requests = _run(workspace, runtime_path, [['{"fullPage":true}']] + [["{}"]] * SHOTS)
    assert 0 < approved < SHOTS
    assert FULL_PAGE_FEEDBACK in requests[1] and STATE_FULL_FEEDBACK in requests[-1]


def test_parallel_screenshots_each_reserve_room_before_any_result_is_written(workspace, runtime_path):
    # The SDK approves all parallel calls before writing any of their results; without a reservation per call,
    # five parallel screenshots after five single ones pass 8 MiB together and the turn is lost.
    approved, requests = _run(workspace, runtime_path, [["{}"]] * 5 + [["{}"] * 5])
    assert 0 < approved < 10
    assert STATE_FULL_FEEDBACK in requests[-1] or PARALLEL_FEEDBACK in requests[-1]


def test_parallel_small_calls_in_an_empty_conversation_are_not_told_the_state_is_full(workspace, runtime_path):
    approved, requests = _run(workspace, runtime_path, [["{}"] * 5], side="8")
    assert approved == 2
    assert PARALLEL_FEEDBACK in requests[-1] and STATE_FULL_FEEDBACK not in requests[-1]


def test_parallel_screenshots_larger_than_two_mib_still_fit_the_state(workspace, runtime_path):
    # At side 900 one screenshot writes about 3.2 MB into events.jsonl; three of them would pass 8 MiB.
    approved, requests = _run(workspace, runtime_path, [["{}"] * 3], side="900")
    assert approved == 2 and PARALLEL_FEEDBACK in requests[-1]


def test_a_written_screenshot_still_reserved_is_not_counted_twice_as_full(workspace, runtime_path):
    # At side 700 (about 2 MB each) two written screenshots use under half of 8 MiB; the next message is told to
    # wait (retryable), never that the conversation is full.
    approved, requests = _run(workspace, runtime_path, [["{}"] * 3, ["{}"]], side="700")
    assert approved >= 2
    assert not any(STATE_FULL_FEEDBACK in body for body in requests)


def test_a_screenshot_path_outside_the_tool_folder_is_refused_with_how_to_fix_it(workspace, runtime_path):
    # Live 2026-10-01: the model asked to save at /map.png; Playwright refused it outside its allowed roots and
    # the turn learned nothing actionable. FA refuses such a path before the call runs and tells the model to omit
    # filename: with a filename Playwright only saves the file and returns no image to the model or deliver_image.
    approved, requests = _run(workspace, runtime_path, [['{"filename":"/map.png"}'], ["{}"]], side="8")
    assert approved == 1, "the retry without filename ran once"
    assert FILENAME_FEEDBACK in requests[1] and "without filename" in FILENAME_FEEDBACK


def test_a_reservation_counts_one_reserve_while_running_and_its_size_until_written():
    files = SdkSessionFiles("reserve-session")
    events = "/session-state/events.jsonl"
    asyncio.run(files.write_file(events, json.dumps({"type": "session.start", "data": {}}) + "\n"))
    # A 3 MiB reserve per running call: two fit under 8 MiB, a third only after one ends.
    assert [files.reserve_large_result(call) for call in "abc"] == ["reserved"] * 2 + ["busy"]
    files.large_result_completed("a", 100_000)
    assert files.reserve_large_result("c") == "reserved", "a completed call counts only its own size"
    assert files.reserve_large_result("d") == "busy"
    started = json.dumps({"type": "tool.execution_start", "data": {"toolCallId": "b"}}) + "\n"
    asyncio.run(files.append_file(events, started + json.dumps(["tool.execution_complete"]) + "\n"))
    assert files.reserve_large_result("d") == "busy", "a started call still holds its reservation"
    done = json.dumps({"type": "tool.execution_complete", "data": {"toolCallId": "b", "success": True}}) + "\n"
    asyncio.run(files.append_file(events, done))
    assert files.reserve_large_result("d") == "reserved", "a written completion releases its reservation"
    files.reset_reservations()
    assert files.reserve_large_result("e") == "reserved"

    near_full = SdkSessionFiles("full-session")
    asyncio.run(near_full.write_file(events, "x" * (6 * 1024 * 1024)))
    assert near_full.reserve_large_result("f") == "full", "the state itself has no room for one more result"