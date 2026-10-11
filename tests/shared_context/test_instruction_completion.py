"""Completion ordering and wakeups; no network, runtime process, or model call."""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
from threading import Event, Timer
from types import SimpleNamespace

import pytest

from shared_context import behavior_assistant_hosted as hosted
from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_server import ServerComposition


@pytest.mark.parametrize("failed", [False, True])
def test_final_save_waits_for_inflight_public_progress(tmp_path, monkeypatch, failed):
    entered, release, finished = Event(), Event(), Event()
    stop = aw.SdkStop(runtime_version="1.0.83", pid=42, exit_code=0, graceful=True)
    saved = []
    def progress(_):
        entered.set()
        assert release.wait(2)
        finished.set()
    def persist(*args):
        assert finished.is_set(), "public progress must stop writing before the final save"
        saved.append(args)
        return aw.ToolReceipt(state="ready_to_finalize", result_digest="a" * 64)
    client = SimpleNamespace(
        assignment=SimpleNamespace(task=SimpleNamespace(instruction=aw.InstructionTask(
            content="OFFLINE", conversation_id="conversation", turn_number=2, model="gpt-4.1-mini")),
            sdk_session_id="sdk", resume=True),
        api_url="https://offline.invalid", token_provider=lambda: "OFFLINE",
        save=lambda _: None, progress=progress, save_text_reply=persist, finish_failed=persist,
    )
    timers = []
    async def sdk(**options):
        options["on_event"]({"public_progress": aw.PublicProgressEvent(
            seq=1, at=datetime.now(timezone.utc), kind="stage", status="started",
            summary="SDK turn started").model_dump(mode="json")})
        options["on_sdk_stopped"](stop.model_dump())
        assert await asyncio.to_thread(entered.wait, 1)
        timer = Timer(0.1, release.set)
        timers.append(timer)
        timer.start()
        if failed:
            raise RuntimeError("OFFLINE completed SDK failure")
        return {"final_message": "OFFLINE answer", "sdk_stop": stop.model_dump()}
    monkeypatch.setattr(hosted, "run_copilot", sdk)
    session = hosted.AssistantSession(client, root=tmp_path, runtime_path=tmp_path / "not-executed")
    monkeypatch.setattr(session, "_archive", lambda: aw.ArchiveUnavailable(error="SDK_ARCHIVE_UNAVAILABLE"))
    try:
        result = asyncio.run(session._run(tmp_path))
        assert result["state"] == "ready_to_finalize" and len(saved) == 1
    finally:
        release.set()
        for timer in timers:
            timer.join(2)


def test_ready_notification_wakes_one_existing_observer_without_timer_delay(monkeypatch):
    import starlette.concurrency
    calls = []
    async def inline(call, *args, **kwargs):
        return call(*args, **kwargs)
    monkeypatch.setattr(starlette.concurrency, "run_in_threadpool", inline)
    async def run():
        started = asyncio.Event()
        def step(binding_id):
            calls.append(binding_id)
            started.set()
            return SimpleNamespace(state="starting" if len(calls) == 1 else "succeeded",
                                   result_forwarded=len(calls) > 1)
        runtime = SimpleNamespace(controller=SimpleNamespace(step=step), work_policy=None,
            dep=SimpleNamespace(delivery=SimpleNamespace(contract=SimpleNamespace(review_interval_seconds=60))))
        composition = ServerComposition(None, runtime, None, None, (), ExitStack())
        composition.launch_instruction("original")
        task = composition._instruction_tasks["original"]
        try:
            await started.wait()
            composition.instruction_ready("original")
            composition.instruction_ready("original")
            await asyncio.wait_for(asyncio.shield(task), 0.5)
            assert calls == ["original", "original"]
        finally:
            await composition.stop_instruction_observers()
            composition.close()
    asyncio.run(run())


def test_ready_during_an_uncertain_query_is_not_lost_when_the_observer_pauses(monkeypatch):
    import starlette.concurrency
    calls = []

    async def inline(call, *args, **kwargs):
        return call(*args, **kwargs)

    monkeypatch.setattr(starlette.concurrency, "run_in_threadpool", inline)

    async def run():
        def step(binding_id):
            calls.append(binding_id)
            if len(calls) == 1:
                composition.instruction_ready(binding_id)
            return SimpleNamespace(state="capacity_uncertain" if len(calls) == 1 else "succeeded",
                                   result_forwarded=len(calls) > 1)

        runtime = SimpleNamespace(controller=SimpleNamespace(step=step), work_policy=None,
            dep=SimpleNamespace(delivery=SimpleNamespace(contract=SimpleNamespace(review_interval_seconds=60))))
        composition = ServerComposition(None, runtime, None, None, (), ExitStack())
        try:
            composition.launch_instruction("original")
            await asyncio.wait_for(asyncio.shield(composition._instruction_tasks["original"]), 0.5)
            assert calls == ["original", "original"]
        finally:
            await composition.stop_instruction_observers()
            composition.close()

    asyncio.run(run())


def test_pending_progress_snapshots_are_coalesced_without_losing_events(tmp_path):
    entered, release = Event(), Event()
    snapshots = []
    def progress(snapshot):
        snapshots.append(snapshot)
        if len(snapshots) == 1:
            entered.set()
            assert release.wait(2)
    session = hosted.AssistantSession(SimpleNamespace(
        progress=progress, assignment=SimpleNamespace(task=SimpleNamespace(instruction=None))), root=tmp_path,
                                      runtime_path=tmp_path / "not-executed")
    def event(seq):
        return {"public_progress": aw.PublicProgressEvent(seq=seq, at=datetime.now(timezone.utc),
                kind="stage", status="started", summary="OFFLINE").model_dump(mode="json")}
    async def run():
        session._public_event(event(1))
        assert await asyncio.to_thread(entered.wait, 1)
        session._public_event(event(2))
        session._public_event(event(3))
        release.set()
        await session._progress_tail
    try:
        asyncio.run(run())
        assert [snapshot.events[-1].seq for snapshot in snapshots] == [1, 3]
        assert [event.seq for event in snapshots[-1].events] == [1, 2, 3]
    finally:
        release.set()


def test_turn_end_progress_joins_the_final_save_without_a_separate_write(tmp_path):
    session=hosted.AssistantSession(SimpleNamespace(progress=lambda _:pytest.fail("no final progress POST"),
        assignment=SimpleNamespace(task=SimpleNamespace(instruction=None))),
        root=tmp_path,runtime_path=tmp_path/"not-executed")
    event=aw.PublicProgressEvent(seq=1,at=datetime.now(timezone.utc),kind="stage",
                                status="succeeded",summary="SDK turn ended")
    session._public_event({"public_progress":event.model_dump(mode="json")})
    assert session._progress_tail is None and session._final_progress.events==(event,)


@pytest.mark.parametrize("operation", ["reserve", "tool"])
def test_progress_cannot_overlap_next_model_or_tool_authority(tmp_path, monkeypatch, operation):
    entered, release, finished = Event(), Event(), Event()
    calls = []
    stop = aw.SdkStop(runtime_version="1.0.83", pid=42, exit_code=0, graceful=True)
    source = aw.ExactSourceRef(source_id="reviewed-source", version=1, sha256="a"*64, byte_length=2)
    def progress(_):
        entered.set()
        assert release.wait(2)
        finished.set()
    def authority(*_):
        assert finished.is_set(), "progress is still writing the same authority"
        calls.append(operation)
        return (aw.ToolReceipt(state="accepted") if operation == "reserve"
                else aw.InstructionContext(source=source, content="{}"))
    def failed(*_):
        pytest.fail("progress contention must not turn a valid SDK continuation into failure")
    client = SimpleNamespace(
        assignment=SimpleNamespace(task=SimpleNamespace(instruction=aw.InstructionTask(
            content="OFFLINE", conversation_id="conversation", turn_number=3, model="gpt-4.1-mini")),
            sdk_session_id="sdk", resume=True),
        api_url="https://offline.invalid", token_provider=lambda: "OFFLINE",
        save=lambda _: None, progress=progress, reserve_model=authority, context=authority,
        finish_failed=failed, save_text_reply=lambda *_: aw.ToolReceipt(
            state="ready_to_finalize", result_digest="a"*64))
    timers = []
    async def sdk(**options):
        options["on_event"]({"public_progress": aw.PublicProgressEvent(
            seq=1, at=datetime.now(timezone.utc), kind="stage", status="started",
            summary="SDK turn started").model_dump(mode="json")})
        options["on_sdk_stopped"](stop.model_dump())
        assert await asyncio.to_thread(entered.wait, 1)
        timer = Timer(0.1, release.set)
        timers.append(timer)
        timer.start()
        if operation == "reserve":
            await options["behavior_boundary"].before_request("b"*64)
        else:
            read = next(tool for tool in options["tools"] if tool.name == "read_context")
            await read.handler({"arguments": {}})
        return {"final_message": "OFFLINE", "sdk_stop": stop.model_dump()}
    monkeypatch.setattr(hosted, "run_copilot", sdk)
    session = hosted.AssistantSession(client, root=tmp_path, runtime_path=tmp_path / "not-executed")
    monkeypatch.setattr(session, "_archive", lambda: aw.ArchiveUnavailable(error="SDK_ARCHIVE_UNAVAILABLE"))
    try:
        assert asyncio.run(session._run(tmp_path))["state"] == "ready_to_finalize"
        assert calls == [operation]
    finally:
        release.set()
        for timer in timers:
            timer.join(2)
