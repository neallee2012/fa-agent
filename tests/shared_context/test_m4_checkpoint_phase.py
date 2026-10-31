"""Regressions for the observed r3 pre-checkpoint final-write rejection loop."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from shared_context.m2_assistant import AssistantEngine
from shared_context.m2_copilot import ToolFailure, run_copilot
from test_m2_assistant import hosted_runtime
from test_m2_copilot import ScriptedEndpoint, runtime_path, tool_call
from test_m4_assistant import (
    ANSWER, ContextBoundary, command, enabled, outcome, status_until, tools_for,
)


def test_checkpoint_stage_advertises_drafts_not_finalization(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)
    context = ContextBoundary()

    async def sdk(**options):
        tools, _ = tools_for(options)
        assert "checkpoint_and_wait" in tools
        assert "finish_task" not in tools
        write = Draft202012Validator(tools["fa_write_file"].parameters)
        update = Draft202012Validator(tools["update_work"].parameters)
        assert write.is_valid({"name": "draft.txt", "text": ANSWER})
        assert not write.is_valid({"name": "result.txt", "text": ANSWER})
        assert update.is_valid({"step": "draft", "progress": "draft", "artifact_text": ANSWER})
        assert not update.is_valid({"step": "final", "progress": "final", "artifact_text": ANSWER})
        assert "Stage: prepare_checkpoint." in options["prompt"]
        return outcome(options)

    engine = AssistantEngine(context, home=tmp_path, process_marker="before-stop", runner=sdk)
    engine.dispatch(command(require_checkpoint=True))
    result = status_until(engine)
    assert result["status"] == "incomplete", result.get("error")
    assert result["checkpoint_wait_observed"] is False
    assert context.mutations == 0


@pytest.mark.parametrize("tool_name", ["fa_write_file", "update_work"])
def test_observed_premature_final_calls_return_safe_checkpoint_guidance(tmp_path, monkeypatch, tool_name):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)
    context = ContextBoundary()

    async def sdk(**options):
        _, call = tools_for(options)
        await call("read_context")
        args = ({"name": "result.txt", "text": ANSWER} if tool_name == "fa_write_file"
                else {"step": "final", "progress": "final", "artifact_text": ANSWER})
        with pytest.raises(ToolFailure, match="ASSISTANT_CHECKPOINT_REQUIRED") as caught:
            await call(tool_name, args)
        assert "draft.txt" in str(caught.value)
        assert "checkpoint_and_wait" in str(caught.value)
        assert ANSWER not in str(caught.value)
        assert context.mutations == 0
        assert not list(tmp_path.rglob("result.txt"))
        return outcome(options)

    engine = AssistantEngine(context, home=tmp_path, process_marker="before-stop", runner=sdk)
    engine.dispatch(command(require_checkpoint=True))
    result = status_until(engine)
    assert result["status"] == "incomplete", result.get("error")
    assert result["task_verified"] is False


def test_checkpoint_correction_then_resume_uses_saved_draft_once(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)
    context = ContextBoundary()
    sessions = []

    async def before_stop(**options):
        sessions.append(options["sdk_session_id"])
        _, call = tools_for(options)
        await call("read_context")
        with pytest.raises(ToolFailure, match="ASSISTANT_CHECKPOINT_REQUIRED"):
            await call("fa_write_file", {"name": "result.txt", "text": ANSWER})
        await call("fa_write_file", {"name": "draft.txt", "text": ANSWER})
        await call("update_work", {"step": "draft", "progress": "draft", "artifact_text": ANSWER})
        await asyncio.wait_for(call("checkpoint_and_wait"), timeout=0.04)
        pytest.fail("checkpoint must remain pending until interruption")

    first = AssistantEngine(context, home=tmp_path, process_marker="before-stop", runner=before_stop)
    first.dispatch(command(require_checkpoint=True))
    paused = status_until(first)
    assert paused["checkpoint_wait_observed"] is True, paused.get("error")
    assert paused["released"] is False
    assert context.mutations == 1

    async def after_stop(**options):
        assert options["resume"] is True
        assert options["sdk_session_id"] == sessions[0]
        tools, call = tools_for(options)
        assert "finish_task" in tools
        assert "checkpoint_and_wait" not in tools
        assert "Stage: complete_after_checkpoint." in options["prompt"]
        with pytest.raises(ToolFailure, match="M4_CONTEXT_REQUIRED"):
            await call("finish_task")
        await call("read_context")
        draft = await call("fa_read_file", {"name": "draft.txt"})
        assert json.loads(draft.text)["text"] == ANSWER
        await call("fa_write_file", {"name": "result.txt", "text": ANSWER})
        await call("update_work", {"step": "final", "progress": "final", "artifact_text": ANSWER})
        await call("finish_task")
        return outcome(options)

    second = AssistantEngine(context, home=tmp_path, process_marker="after-stop", runner=after_stop)
    second.dispatch(command("resume"))
    complete = status_until(second)
    assert complete["status"] == "completed", complete.get("error")
    assert complete["task_verified"] is True and complete["released"] is True
    assert context.mutations == 2
    evidence = second.dispatch(command("evidence"))
    assert sum(item["name"] == "checkpoint_and_wait" for item in evidence["tool_calls"]) == 1
    assert len([item for item in context.calls if item[0] == "PATCH"]) == 2


def test_non_checkpoint_m4_retains_final_tools(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)
    context = ContextBoundary()

    async def sdk(**options):
        tools, call = tools_for(options)
        assert "finish_task" in tools and "checkpoint_and_wait" not in tools
        assert Draft202012Validator(tools["fa_write_file"].parameters).is_valid(
            {"name": "result.txt", "text": ANSWER},
        )
        await call("read_context")
        await call("fa_write_file", {"name": "result.txt", "text": ANSWER})
        await call("update_work", {"step": "final", "progress": "final", "artifact_text": ANSWER})
        await call("finish_task")
        return outcome(options)

    engine = AssistantEngine(context, home=tmp_path, process_marker="ordinary-final", runner=sdk)
    engine.dispatch(command())
    result = status_until(engine)
    assert result["status"] == "completed", result.get("error")
    assert context.mutations == 1


def test_real_sdk_resumes_with_final_phase_tools_without_replaying_draft(tmp_path, monkeypatch, runtime_path):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)
    context = ContextBoundary()
    context.credential = SimpleNamespace(get_token=lambda *_: SimpleNamespace(token="synthetic-api-token"))
    before = ScriptedEndpoint([
        tool_call("read_context", {}, "cp-context"),
        tool_call("fa_write_file", {"name": "draft.txt", "text": ANSWER}, "cp-draft"),
        tool_call("update_work", {"step": "draft", "progress": "draft", "artifact_text": ANSWER}, "cp-save"),
        tool_call("checkpoint_and_wait", {}, "cp-pending"),
    ])
    after = ScriptedEndpoint([
        tool_call("read_context", {}, "resume-context"),
        tool_call("fa_read_file", {"name": "draft.txt"}, "resume-draft"),
        tool_call("fa_write_file", {"name": "result.txt", "text": ANSWER}, "resume-result"),
        tool_call("update_work", {"step": "final", "progress": "final", "artifact_text": ANSWER}, "resume-save"),
        tool_call("finish_task", {}, "resume-finish"),
    ])
    before.expected_url = after.expected_url = "https://context.invalid/m4/model/v1/chat/completions"

    async def sdk(**options):
        endpoint = after if options["resume"] else before
        options["timeout_seconds"] = 30
        task = asyncio.create_task(run_copilot(
            **options, runtime_path=runtime_path, http_transport=endpoint.transport,
        ))
        if options["resume"]:
            return await task
        state_file = Path(options["state_dir"]).parent / "state.json"
        try:
            deadline = time.monotonic() + 25
            while not task.done():
                if time.monotonic() >= deadline:
                    raise AssertionError("real SDK never reached the durable checkpoint")
                try:
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                except PermissionError:  # Windows: this read met the writer's os.replace.
                    await asyncio.sleep(0.02)
                    continue
                if state["checkpoint_wait_observed"] and state["status"] == "waiting":
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise TimeoutError("simulated owned shutdown after durable checkpoint")
                await asyncio.sleep(0.02)
            return await task
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    def wait(engine):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            value = engine.dispatch(command("status"))
            if value["status"] in ("failed", "completed", "incomplete"):
                return value
            time.sleep(0.02)
        pytest.fail("real SDK did not stop within its bounded test window")

    first = AssistantEngine(context, home=tmp_path, process_marker="sdk-before-stop", runner=sdk)
    first.dispatch(command(require_checkpoint=True))
    paused = wait(first)
    assert paused["checkpoint_wait_observed"] is True, paused.get("error")
    assert context.mutations == 1 and len(before.requests) == 4
    sdk_id = paused["sdk_session_id"]

    second = AssistantEngine(context, home=tmp_path, process_marker="sdk-after-stop", runner=sdk)
    second.dispatch(command("resume"))
    complete = wait(second)
    assert complete["status"] == "completed", complete.get("error")
    assert complete["sdk_session_id"] == sdk_id and complete["task_verified"] is True
    assert complete["released"] is True and context.mutations == 2
    assert len(after.requests) == 5
    evidence = second.dispatch(command("evidence"))
    assert sum(call["name"] == "checkpoint_and_wait" for call in evidence["tool_calls"]) == 1
