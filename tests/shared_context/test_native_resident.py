"""ADR-0005 on the native lane: the real ``run_copilot`` over a pinned-SDK double.

No runtime process, model or network: the double writes the SDK's SessionFs files and reaches idle, so the
tests see exactly when FA keeps, continues, restores or stops the SDK between native turns.
"""
import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import create_autospec

import httpx
import pytest
from starlette.testclient import TestClient

from copilot import CopilotClient
from copilot.client import GetStatusResponse
from copilot.session import CopilotSession
from copilot.session_events import AssistantMessageData, SessionIdleData

from shared_context import m2_copilot, native_sdk
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeCancel, NativeResult, NativeSubmit, NativeTurnCapture
from shared_context.native_turns import NativeTurnRuntime, timing_id
from test_native_turn_wire import capture, submit
from test_native_turns import Harness, IDENTITY, local_env  # noqa: F401

EVENTS = "/session-state/events.jsonl"
MODEL_ROOT = "https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1"


def line(event):
    return json.dumps(event) + "\n"


class PinnedSdk:
    """Pinned SDK 1.0.13 double: one session per client, SessionFs writes, then idle."""

    def __init__(self, monkeypatch, *, during_send=None, on_disconnect=None):
        self.clients, self.sends, self.stopped, self.during_send = [], [], [], during_send
        self.connections = []
        self.on_disconnect = on_disconnect

        def denied(*args, **kwargs):
            pytest.fail("no SDK executable or real subprocess is permitted")

        monkeypatch.setattr(subprocess, "Popen", denied)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", denied)
        monkeypatch.setattr(m2_copilot, "CopilotClient", create_autospec(CopilotClient, side_effect=self.client))

    def client(self, **connection):
        self.connections.append(connection)
        client = create_autospec(CopilotClient, instance=True)
        exited = Event()
        client._cli_process = SimpleNamespace(pid=900 + len(self.clients),
                                              poll=lambda: 0 if exited.is_set() else None, returncode=0)

        def stop():
            exited.set()
            self.stopped.append(client)

        client.stop.side_effect = stop
        client.get_status.return_value = GetStatusResponse(version="1.0.83", protocol_version=2)

        async def create(**options):
            return await self.session(client, options["session_id"], options, False)

        async def resume(sid, **options):
            return await self.session(client, sid, options, True)

        client.create_session.side_effect = create
        client.resume_session.side_effect = resume
        self.clients.append(client)
        return client

    async def session(self, client, sid, options, resume):
        fs = options["create_session_fs_handler"](SimpleNamespace(session_id=sid))
        if not resume:
            await fs.write_file(EVENTS, line({"type": "session.start", "data": {"sessionId": sid}}))
            await fs.write_file("/session-state/workspace.yaml", "id: " + sid + "\n")
        session = create_autospec(CopilotSession, instance=True)
        session.session_id = sid
        session.get_events.return_value = []
        handlers = []
        session.on.side_effect = lambda handler: handlers.append(handler) or (lambda: handlers.remove(handler))

        async def abort():
            for handler in list(handlers):
                handler(SimpleNamespace(data=SessionIdleData(aborted=True)))

        async def disconnect():
            if self.on_disconnect is not None:
                await self.on_disconnect(options, fs)

        async def send(prompt, **kwargs):
            record = SimpleNamespace(client=client, session=session, options=options, resume=resume, fs=fs, sid=sid)
            self.sends.append(record)
            if self.during_send is not None:
                await self.during_send(record)
            text = f"OFFLINE answer {len(self.sends)}"
            await fs.append_file(EVENTS, line({"type": "user.message", "data": {"content": prompt}})
                                 + line({"type": "assistant.message", "data": {"content": text}}))
            for handler in list(handlers):
                handler(SimpleNamespace(data=AssistantMessageData(content=text, message_id=f"m-{len(self.sends)}")))
            for handler in list(handlers):
                handler(SimpleNamespace(data=SessionIdleData()))
            return "offline-message"

        session.abort.side_effect = abort
        session.disconnect.side_effect = disconnect
        session.send.side_effect = send
        return session


def offline_runtime(tmp_path, *, hitl_client=None, capability_providers=()):
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=Path(__file__), api_url="https://api.offline.invalid",
        model_url=MODEL_ROOT, token_provider=lambda: "OFFLINE",
        model_transport=httpx.MockTransport(lambda _: pytest.fail("this double makes no model request")),
        hitl_client=hitl_client, capability_providers=capability_providers)
    executor.recoveries = []
    executor.request_recovery = lambda: executor.recoveries.append("SIGTERM")  # never SIGTERM the test process
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    return executor, runtime


def phases(caplog, rid):
    found = []
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith("FA_TIMING "):
            value = json.loads(message.removeprefix("FA_TIMING "))
            if value.get("job_id") == timing_id(rid):
                found.append(value["phase"])
    return found


def turn(h, rid, cid=None, selected=None):
    h.invoke(NativeSubmit(command=submit(rid, cid).command, capture=selected or capture()))
    return h.wait(rid, "completed")


def text(h, receipt):
    return h.call(h.runtime.result, NativeResult(conversation_id=receipt.conversation_id,
                                                 request_id=receipt.request_id)).text


def serve(runtime):
    client = TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime))
    return client, Harness(runtime, SimpleNamespace(controls={}), client)


def auto_capture():
    from shared_context.behavior_sdk_state import AUTO_PROFILE, CheckpointProfile, local_platform
    value = capture().model_dump(mode="json")
    value["template"]["settings"]["checkpoint_profile"] = CheckpointProfile(
        name=AUTO_PROFILE, platform=local_platform(), provider="openai-responses").model_dump(mode="json")
    return NativeTurnCapture.model_validate_json(json.dumps(value))


def track_hitl(monkeypatch, user_input=None):
    """Each turn's own NativeHitlRuntime, in turn order; its API wait and close are covered offline elsewhere."""
    from shared_context.native_hitl_runtime import NativeHitlRuntime

    hitls = []
    original_init = NativeHitlRuntime.__init__

    def tracked(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        hitls.append(self)

    async def nothing(self):
        return None

    monkeypatch.setattr(NativeHitlRuntime, "__init__", tracked)
    monkeypatch.setattr(NativeHitlRuntime, "close", nothing)
    if user_input is not None:
        monkeypatch.setattr(NativeHitlRuntime, "user_input", user_input)
    return hitls


@pytest.mark.parametrize("auto", [False, True])
def test_second_turn_keeps_the_sdk_with_no_process_start_or_stop(local_env, monkeypatch, tmp_path, caplog, auto):
    sdk = PinnedSdk(monkeypatch)
    executor, runtime = offline_runtime(tmp_path)
    selected = auto_capture() if auto else capture()
    client, h = serve(runtime)
    with client:
        first = turn(h, "rid-1", selected=selected)
        second = turn(h, "rid-2", first.conversation_id, selected=selected)
        assert text(h, second) == "OFFLINE answer 2"
        live = next(iter(executor.resident._holders.values())).live
        assert live is not None and live.released_sha256 == second.result.checkpoint_sha256
    assert len(sdk.clients) == 1, "the second turn must not start another SDK"
    assert "sdk_process_start" in phases(caplog, "rid-1")
    for rid in ("rid-1", "rid-2"):
        assert "sdk_process_stop" not in phases(caplog, rid)
    assert "sdk_process_start" not in phases(caplog, "rid-2")
    assert sdk.sends[1].session is sdk.sends[0].session and not sdk.sends[1].resume
    assert first.result.checkpoint_sha256 != second.result.checkpoint_sha256
    # VM shutdown stops the kept SDK best effort.
    assert sdk.stopped == sdk.clients


def test_second_turn_hooks_and_ask_user_reach_that_turns_own_guards(local_env, monkeypatch, tmp_path):
    from shared_context.native_operations import NativeOperationGuard

    guards, seen = [], []

    async def started(self):
        guards.append(self)

    async def nothing(self):
        return None

    async def pre_tool(self, data, context, original):
        seen.append(("pre_tool", self))

    async def user_input(self, request, invocation):
        seen.append(("user_input", self))
        return {"answer": "OFFLINE", "wasFreeform": True}

    monkeypatch.setattr(NativeOperationGuard, "start", started)
    monkeypatch.setattr(NativeOperationGuard, "close", nothing)
    monkeypatch.setattr(NativeOperationGuard, "pre_tool", pre_tool)
    hitls = track_hitl(monkeypatch, user_input)

    async def second_turn_calls(record):
        if len(sdk.sends) == 2:
            # The hooks and ask_user handler the SDK session got when the first turn created it.
            hooks = record.options["hooks"]
            assert await hooks["on_pre_tool_use"]({"toolName": "read_context", "sessionId": record.sid},
                                                  {"session_id": record.sid}) is None
            await record.options["on_user_input_request"](
                {"question": "Which?", "choices": [], "allowFreeform": True}, {"session_id": record.sid})

    sdk = PinnedSdk(monkeypatch, during_send=second_turn_calls)
    executor, runtime = offline_runtime(tmp_path, hitl_client=SimpleNamespace())

    async def continuation(*args):
        return None

    runtime.on_continuation = continuation
    client, h = serve(runtime)
    with client:
        first = turn(h, "rid-1")
        turn(h, "rid-2", first.conversation_id)
    assert len(sdk.clients) == 1 and len(guards) == len(hitls) == 2
    assert seen == [("pre_tool", guards[1]), ("user_input", hitls[1])]


def test_an_unbound_resident_hook_denies_and_ask_user_fails():
    live = m2_copilot.ResidentSdk("digest", "sid", files=None)
    decision = asyncio.run(live.hook("on_pre_tool_use")({"toolName": "task"}, {}))
    assert decision["permissionDecision"] == "deny"
    assert asyncio.run(live.hook("on_post_tool_use")({}, {})) is None
    with pytest.raises(RuntimeError):
        asyncio.run(live.user_input({"question": "q"}, {}))


def test_files_moved_after_release_stop_the_sdk_and_restore_the_checkpoint(local_env, monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    sdk = PinnedSdk(monkeypatch)
    executor, runtime = offline_runtime(tmp_path)
    client, h = serve(runtime)
    with client:
        first = turn(h, "rid-1")
        live = next(iter(executor.resident._holders.values())).live
        # A late runtime write after the release.
        live.files.files["events.jsonl"] += line({"type": "session.info", "data": {"message": "OFFLINE-late"}})
        second = turn(h, "rid-2", first.conversation_id)
        assert text(h, second) == "OFFLINE answer 2"
    moved = [r.getMessage() for r in caplog.records if r.getMessage().startswith("RESIDENT_FILES_MOVED")]
    assert moved == ["RESIDENT_FILES_MOVED released=" + first.result.checkpoint_sha256[:12]]
    assert len(sdk.clients) == 2 and sdk.stopped[0] is sdk.clients[0]
    assert sdk.sends[1].resume and sdk.sends[1].fs is not sdk.sends[0].fs
    assert "OFFLINE-late" not in sdk.sends[1].fs.files["events.jsonl"]
    assert {"resident_sdk_stop", "sdk_process_start"} <= set(phases(caplog, "rid-2"))


def test_a_kept_sdk_whose_files_failed_while_idle_is_stopped_not_retried(local_env, monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    sdk = PinnedSdk(monkeypatch)
    executor, runtime = offline_runtime(tmp_path)
    client, h = serve(runtime)
    with client:
        first = turn(h, "rid-1")
        # An idle runtime call the SDK filesystem refused: digest unchanged, files unusable.
        next(iter(executor.resident._holders.values())).live.files.failed = True
        second = turn(h, "rid-2", first.conversation_id)
        assert text(h, second) == "OFFLINE answer 2"
    assert "RESIDENT_SDK_DISCARDED reason=files_unusable" in [r.getMessage() for r in caplog.records]
    assert len(sdk.clients) == 2 and sdk.stopped[0] is sdk.clients[0] and sdk.sends[1].resume


def test_an_image_turn_that_would_not_fit_the_live_files_restores_the_smaller_checkpoint():
    from shared_context.backup_models import MAX_BACKUP_BYTES
    from shared_context.native_resident import NativeResidentPool

    stopped = []

    async def stop(on_unexited=None):
        stopped.append(True)

    files = SimpleNamespace(files={"events.jsonl": "x" * (MAX_BACKUP_BYTES // 4)}, checkpoint_profile=None,
                            model_provider=None, continue_from=lambda state: None)
    live = SimpleNamespace(files=files, released_sha256="a" * 64, alive=lambda: True, reusable=lambda: True,
                           stop=stop, turn=None, model="OFFLINE-model", sdk_session_id="sid")

    async def continued(needed):
        pool = NativeResidentPool()
        holder = pool.holder("sid")
        holder.live = live
        found = await pool.continue_live(holder, SimpleNamespace(sha256="a" * 64), checkpoint_profile=None,
                                         model_provider=None, model="OFFLINE-model", needed_bytes=needed)
        return found, holder.live

    assert asyncio.run(continued(0)) == (files, live) and not stopped
    # Live files keep private events the checkpoint drops: images that fit only the published copy restore it.
    assert asyncio.run(continued(MAX_BACKUP_BYTES // 2)) == (None, None) and stopped == [True]


def test_a_compaction_starting_after_the_release_check_stops_with_the_turn_still_bound(
        local_env, monkeypatch, tmp_path, caplog):
    from shared_context.behavior_sdk_files import SdkSessionFiles

    def compaction(kind, sid, extra=None):
        return {"type": kind, "id": f"late-{kind}-{sid}", "timestamp": "2026-10-06T01:00:00.000Z", "parentId": None,
                "data": {"trigger": "threshold", **(extra or {})}}

    original = SdkSessionFiles.snapshot
    late, bound = [], []

    def snapshot(self, *, freeze=True):
        if not freeze and not late:
            # The runtime's own compaction starts right after the release check, before the release snapshot.
            late.append(True)
            self.files["events.jsonl"] += line(compaction("session.compaction_start", self.session_id))
        return original(self, freeze=freeze)

    async def finish(options, fs):
        if late and "session.compaction_complete" not in fs.files["events.jsonl"]:
            # Its summary request needs this turn's model gateway, so the ordinary stop runs while it is bound.
            bound.append(sdk.connections[-1]["request_handler"].resident.turn is not None)
            await fs.append_file(EVENTS, line(compaction("session.compaction_complete", fs.session_id, {
                "success": True, "summaryContent": "OFFLINE summary"})))

    monkeypatch.setattr(SdkSessionFiles, "snapshot", snapshot)
    sdk = PinnedSdk(monkeypatch, on_disconnect=finish)
    executor, runtime = offline_runtime(tmp_path)
    selected = auto_capture()
    client, h = serve(runtime)
    with client:
        first = turn(h, "rid-1", selected=selected)
        assert text(h, first) == "OFFLINE answer 1", "the reply is saved, not left UNKNOWN"
        second = turn(h, "rid-2", first.conversation_id, selected=selected)
        assert text(h, second) == "OFFLINE answer 2"
    assert bound == [True]
    assert "RESIDENT_RELEASE_REFUSED" in [r.getMessage() for r in caplog.records]
    assert "sdk_process_stop" in phases(caplog, "rid-1")
    assert sdk.stopped[0] is sdk.clients[0] and len(sdk.clients) == 2 and sdk.sends[1].resume
    assert "session.compaction_complete" in sdk.sends[1].fs.files["events.jsonl"]


def test_a_cancelled_discard_still_stops_the_kept_sdk():
    from shared_context.native_resident import NativeResidentPool

    started, release, stopped = asyncio.Event(), asyncio.Event(), []

    async def stop(on_unexited=None):
        started.set()
        await release.wait()
        stopped.append(True)

    async def scenario():
        pool = NativeResidentPool()
        holder = pool.holder("sid")
        holder.live = SimpleNamespace(stop=stop, turn=None, sdk_session_id="sid")
        discarding = asyncio.create_task(pool.discard(holder))
        await started.wait()
        discarding.cancel()  # e.g. the Responses stream closed while the next turn discarded a stale SDK
        with pytest.raises(asyncio.CancelledError):
            await discarding
        assert holder.live is None and not stopped
        release.set()
        await pool.aclose()  # VM shutdown still waits for that stop
        return stopped

    assert asyncio.run(scenario()) == [True]


def test_a_third_conversation_stops_the_oldest_idle_sdk(local_env, monkeypatch, tmp_path):
    sdk = PinnedSdk(monkeypatch)
    executor, runtime = offline_runtime(tmp_path)
    stops = []
    original_stop = m2_copilot.ResidentSdk.stop

    async def observed(self, on_unexited=None):
        # Turns run one at a time under execution_slot: the evicted SDK serves no turn at this moment.
        stops.append((self.client, self.turn, runtime.execution_owner))
        await original_stop(self, on_unexited)

    monkeypatch.setattr(m2_copilot.ResidentSdk, "stop", observed)
    client, h = serve(runtime)
    with client:
        receipts = [turn(h, rid) for rid in ("rid-a", "rid-b", "rid-c")]
        assert len({receipt.conversation_id for receipt in receipts}) == 3

        async def settled():
            await asyncio.gather(*executor.resident._stopping)

        h.call(settled)
        live = [holder.live for holder in executor.resident._holders.values() if holder.live is not None]
        assert [item.client for item in live] == sdk.clients[1:]
        assert stops == [(sdk.clients[0], None, "rid-c")] and sdk.stopped == [sdk.clients[0]]


def test_a_turn_that_asked_the_owner_keeps_the_sdk_and_the_next_question_reaches_the_next_turn(
        local_env, monkeypatch, tmp_path, caplog):
    asked = []

    async def user_input(self, request, invocation):
        # The owner's own wait and answer are covered offline; here only which turn's wait the SDK reached.
        self._questions += 1
        asked.append((self, request["question"]))
        return {"answer": "OFFLINE", "wasFreeform": True}

    hitls = track_hitl(monkeypatch, user_input)

    async def ask(record):
        # Each turn asks through the ask_user handler the SDK session got once, when the first turn created it.
        await record.options["on_user_input_request"](
            {"question": f"Q{len(sdk.sends)}", "choices": [], "allowFreeform": True}, {"session_id": record.sid})

    sdk = PinnedSdk(monkeypatch, during_send=ask)
    executor, runtime = offline_runtime(tmp_path, hitl_client=SimpleNamespace())
    client, h = serve(runtime)
    with client:
        first = turn(h, "rid-1")
        # Between turns the kept session reaches no turn: a stray question gets an error, never an answer.
        with pytest.raises(RuntimeError):
            client.portal.call(sdk.sends[0].options["on_user_input_request"],
                               {"question": "late", "choices": [], "allowFreeform": True},
                               {"session_id": sdk.sends[0].sid})
        second = turn(h, "rid-2", first.conversation_id)
        assert text(h, second) == "OFFLINE answer 2"
    assert len(sdk.clients) == 1 and sdk.sends[1].session is sdk.sends[0].session and not sdk.sends[1].resume
    assert "sdk_process_stop" not in phases(caplog, "rid-1")
    assert "sdk_process_start" not in phases(caplog, "rid-2")
    assert not [r for r in caplog.records if r.getMessage().startswith("RESIDENT_SDK_STOPPED")]
    assert asked == [(hitls[0], "Q1"), (hitls[1], "Q2")] and hitls[0]._wait_id != hitls[1]._wait_id


def test_a_turn_that_waited_on_an_approval_card_keeps_the_sdk(local_env, monkeypatch, tmp_path, caplog):
    from datetime import datetime, timezone

    hitls = track_hitl(monkeypatch)

    async def reviewed(record):
        if len(sdk.sends) == 1:
            # The first turn's guarded call waited on the owner's approval card.
            hitls[-1].calls["call-1"] = SimpleNamespace(pending_expires_at=datetime(2026, 10, 6, tzinfo=timezone.utc))

    sdk = PinnedSdk(monkeypatch, during_send=reviewed)
    executor, runtime = offline_runtime(tmp_path, hitl_client=SimpleNamespace())
    client, h = serve(runtime)
    with client:
        first = turn(h, "rid-1")
        turn(h, "rid-2", first.conversation_id)
    # The second turn reviews with a runtime of its own, not the one that held the first turn's card.
    assert len(hitls) == 2 and hitls[0].opened and hitls[1] is not hitls[0]
    stopped = [r.getMessage() for r in caplog.records if r.getMessage().startswith("RESIDENT_SDK_STOPPED")]
    assert stopped == [] and len(sdk.clients) == 1
    assert "sdk_process_stop" not in phases(caplog, "rid-1")
    assert "sdk_process_start" not in phases(caplog, "rid-2")


def delegate_capture():
    from shared_context.behavior_capabilities import CapabilityCapture, available_grants
    from shared_context.behavior_delegation_wire import CAPABILITY_ID
    granted = available_grants(None, (CAPABILITY_ID,))[CAPABILITY_ID]
    values = capture().model_dump()
    values["template"]["settings"]["capabilities"] = (granted.ref,)
    values["capabilities"] = CapabilityCapture(head_selection_id="captured-native-delegate", grants=(granted,))
    return NativeTurnCapture.model_validate(values)


@pytest.mark.parametrize("attempted", [False, True], ids=["no-subtask", "task-called"])
def test_a_delegate_selected_turn_keeps_the_sdk_unless_it_called_task(
        local_env, monkeypatch, tmp_path, caplog, attempted):
    from shared_context.behavior_delegation_wire import CAPABILITY_ID
    from shared_context.native_sdk import native_capability_providers

    grant = delegate_capture().capabilities.grants[0]
    providers = tuple(provider for provider in native_capability_providers("https://api.offline.invalid", lambda: "OFFLINE")
                      if provider.accepts(grant))
    assert len(providers) == 1 and grant.entry_id == CAPABILITY_ID

    async def task_started(record):
        if attempted and len(sdk.sends) == 1:
            record.options["on_event"]({"type": "tool.execution_start", "data": {
                "toolName": "task", "toolCallId": "call-1", "arguments": {"prompt": "OFFLINE"}}})

    sdk = PinnedSdk(monkeypatch, during_send=task_started)
    executor, runtime = offline_runtime(tmp_path, capability_providers=providers)
    selected = delegate_capture()
    client, h = serve(runtime)
    with client:
        first = turn(h, "rid-1", selected=selected)
        second = turn(h, "rid-2", first.conversation_id, selected=selected)
        assert text(h, second) == "OFFLINE answer 2"
    # The delegation child configuration really reached the SDK session.
    assert sdk.sends[0].options.get("custom_agents")
    stopped = [r.getMessage() for r in caplog.records if r.getMessage().startswith("RESIDENT_SDK_STOPPED")]
    if attempted:
        assert stopped == ["RESIDENT_SDK_STOPPED reason=delegation"] and len(sdk.clients) == 2
        assert sdk.sends[1].resume
    else:
        assert stopped == [] and len(sdk.clients) == 1
        assert "sdk_process_start" not in phases(caplog, "rid-2")


def test_an_auto_compaction_still_open_at_idle_stops_the_sdk(local_env, monkeypatch, tmp_path, caplog):
    def compaction(kind, sid, extra=None):
        return {"type": kind, "id": f"{kind}-{sid}", "timestamp": "2026-10-06T01:00:00.000Z", "parentId": None,
                "data": {"trigger": "threshold", **(extra or {})}}

    async def start(record):
        if len(sdk.sends) == 1:
            event = compaction("session.compaction_start", record.sid)
            await record.fs.append_file(EVENTS, line(event))
            record.options["on_event"](event)

    async def finish(options, fs):
        # The runtime finishes its own compaction while the ordinary stop disconnects.
        if not any(item.startswith('{"type": "session.compaction_complete"') for item in fs.files[
                "events.jsonl"].splitlines()) and "session.compaction_start" in fs.files["events.jsonl"]:
            sid = fs.session_id
            await fs.append_file(EVENTS, line(compaction("session.compaction_complete", sid, {
                "success": True, "summaryContent": "OFFLINE summary"})))

    sdk = PinnedSdk(monkeypatch, during_send=start, on_disconnect=finish)
    executor, runtime = offline_runtime(tmp_path)
    selected = auto_capture()
    client, h = serve(runtime)
    with client:
        first = turn(h, "rid-1", selected=selected)
        second = turn(h, "rid-2", first.conversation_id, selected=selected)
        assert text(h, second) == "OFFLINE answer 2"
    assert "sdk_process_stop" in phases(caplog, "rid-1")
    assert len(sdk.clients) == 2 and sdk.sends[1].resume
    assert "session.compaction_complete" in sdk.sends[1].fs.files["events.jsonl"]


def stuck_cli(monkeypatch, *, dies_on_kill):
    """#874 P3 / #903: the first turn's CLI ignores the abort, stop and force_stop (item 2's bounded path); FA's own
    kill of its PID either ends it or (an uninterruptible kernel wait) does not."""
    exited, alive_at_start, kills = Event(), [], []

    async def nothing():
        return None

    async def hang_first(record):
        if len(sdk.sends) == 1:
            record.session.abort.side_effect = nothing  # the runtime never reports idle
            await asyncio.Event().wait()

    sdk = PinnedSdk(monkeypatch, during_send=hang_first)

    def kill():
        kills.append(4242)
        if dies_on_kill:
            exited.set()

    def client(**connection):
        alive_at_start.append([made._cli_process.poll() is None for made in sdk.clients])
        made = sdk.client(**connection)
        if len(sdk.clients) == 1:
            made._cli_process = SimpleNamespace(pid=4242, poll=lambda: 0 if exited.is_set() else None,
                                                returncode=None, kill=kill)
            made.stop.side_effect = RuntimeError("OFFLINE stop ignored")
            made.force_stop.side_effect = TimeoutError  # what wait_for(force_stop, 5) raises, without the 5 s
        return made

    monkeypatch.setattr(m2_copilot, "CopilotClient", create_autospec(CopilotClient, side_effect=client))
    monkeypatch.setattr(native_sdk, "ABORT_IDLE_SECONDS", 0.2)
    monkeypatch.setattr(m2_copilot, "EXIT_CONFIRM_SECONDS", 0.2)
    monkeypatch.setattr(m2_copilot, "KILL_CONFIRM_SECONDS", 0.2, raising=False)  # absent before #903
    return sdk, exited, alive_at_start, kills


def start_and_cancel_first(h, sdk, server):
    h.invoke(NativeSubmit(command=submit("rid-1").command, capture=capture()))
    for _ in range(800):
        if sdk.sends:
            break
        server.portal.call(asyncio.sleep, 0.01)
    cid = h.call(h.runtime.read, "rid-1").conversation_id
    return cid, lambda: h.invoke(NativeCancel(conversation_id=cid, request_id="rid-1"))


def test_a_cli_that_outlives_force_stop_is_killed_again_before_the_next_sdk(local_env, monkeypatch, tmp_path):
    """#903 P2-1: FA kills the force-stopped CLI's own PID once more; it exits, so the next message runs normally (no
    503, no recovery restart) and its SDK starts only after the first CLI exited."""
    sdk, exited, alive_at_start, kills = stuck_cli(monkeypatch, dies_on_kill=True)
    executor, runtime = offline_runtime(tmp_path)
    server, h = serve(runtime)
    with server:
        cid, cancel = start_and_cancel_first(h, sdk, server)
        cancel()
        assert h.wait("rid-1", "unknown").failure_code == "NATIVE_TURN_CANCELLED"
        assert kills == [4242] and exited.is_set() and executor.recoveries == []
        assert text(h, turn(h, "rid-2", cid)) == "OFFLINE answer 2"
    assert alive_at_start == [[], [False]]


@pytest.mark.parametrize("ends", ["cli_exits", "recovery_shutdown", "close_unconfirmed"])
def test_an_unkillable_cli_keeps_queued_work_and_leaves_by_recovery(local_env, monkeypatch, tmp_path, caplog, ends):
    """#903 P2-1/P2-2/P2-3: the CLI survives FA's own kill too. NATIVE_CLI_UNKILLABLE is logged and a recovery restart
    requested (once). A message queued meanwhile stays accepted (no second SDK, never unknown); a new one gets a
    not_dispatched 503 with a retry hint. The queued one then runs once the CLI exits, or the platform shutdown the
    executor asked for settles it as NATIVE_TURN_NOT_STARTED and leaves by exit_for_recovery. close_unconfirmed
    (review P3-1): that settle fails, so core's real re-entry (payload.input=None, steering.active_input=rid-2) settles
    it after the restart."""
    from azure.ai.agentserver.core.tasks._local_provider import LocalFileTaskProvider
    from shared_context.errors import Problem
    from test_native_binding_tasks import observer

    sdk, exited, alive_at_start, kills = stuck_cli(monkeypatch, dies_on_kill=False)
    executor, runtime = offline_runtime(tmp_path)
    left = []
    original_exit, original_close = NativeTurnRuntime._exit_for_recovery, NativeTurnRuntime._close_unstarted

    async def recorded(self, ctx, original=None):
        left.append(None if original is None else original.value.receipt.request_id)
        return await original_exit(self, ctx, original)

    async def close(self, original):
        if self is runtime and ends == "close_unconfirmed":
            raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "OFFLINE close answer lost")
        return await original_close(self, original)

    monkeypatch.setattr(NativeTurnRuntime, "_exit_for_recovery", recorded)
    monkeypatch.setattr(NativeTurnRuntime, "_close_unstarted", close)
    server, h = serve(runtime)
    with server:
        cid, cancel = start_and_cancel_first(h, sdk, server)
        assert h.invoke(NativeSubmit(command=submit("rid-2").command, capture=capture())).status_code == 202
        cancel()
        assert h.wait("rid-1", "unknown").failure_code == "NATIVE_TURN_CANCELLED"
        recovery_requested(server, executor)  # once rid-1's own execution ended (round 11)
        assert kills == [4242] and executor.recoveries == ["SIGTERM"]
        assert "NATIVE_CLI_UNKILLABLE pid=4242" in caplog.text
        server.portal.call(asyncio.sleep, 1)
        assert h.call(runtime.read, "rid-2").status == "accepted" and len(sdk.sends) == 1 and len(sdk.clients) == 1
        refused = h.invoke(NativeSubmit(command=submit("rid-3", cid).command, capture=capture()))
        assert refused.status_code == 503, refused.text
        error = refused.json()["error"]
        assert (error["code"], error["not_dispatched"], error["retry_after_ms"]) == (
            "NATIVE_EXECUTION_UNAVAILABLE", True, 1000)
        if ends == "cli_exits":
            exited.set()
            assert text(h, h.wait("rid-2", "completed")) == "OFFLINE answer 2"
            assert len(sdk.clients) == 2 and alive_at_start == [[], [False]]
    if ends != "cli_exits":
        assert left == ["rid-2"] and len(sdk.sends) == 1
        if ends == "close_unconfirmed":
            assert "FA_NATIVE_UNSTARTED_CLOSE_UNCONFIRMED" in caplog.text  # rid-2 left this lifetime accepted
        # Review P2: core already dropped the drained input, so a restart could never run rid-2; it is settled
        # (resendable) before the recovery exit, or by the restart's re-entry, and stays settled.
        _, restarted = offline_runtime(tmp_path)
        reads = []
        restarted.task_observer = observer(IDENTITY, LocalFileTaskProvider(), reads)
        server, h = serve(restarted)
        with server:
            for _ in range(100):
                settled = h.call(restarted.read, "rid-2")
                if settled.status != "accepted":
                    break
                server.portal.call(asyncio.sleep, 0.1)
        assert (settled.status, settled.failure_code) == ("unknown", "NATIVE_TURN_NOT_STARTED")
        assert len(sdk.sends) == 1
        if ends == "close_unconfirmed":
            assert reads  # the re-entry read core's task record, as Hosted reads it
    assert executor.recoveries == ["SIGTERM"]


def test_a_turn_whose_own_cli_survives_saves_its_result_before_the_recovery_restart(local_env, monkeypatch, tmp_path):
    """Review P2 (round 11) on #903: a cancelled turn's own CLI survives stop, force_stop and FA's own kill. The
    recovery restart it asks for waits for that turn's own execution too: SIGTERM (which sets every active task's
    shutdown, as core's TaskManager.shutdown does first) comes only after the turn saved its final status, so the
    restart finds it settled and never re-runs it as a continuation."""
    from azure.ai.agentserver.core.tasks._manager import get_task_manager

    sdk, _, _, kills = stuck_cli(monkeypatch, dies_on_kill=False)
    executor, runtime = offline_runtime(tmp_path)
    in_flight_at_sigterm = []

    def sigterm():
        in_flight_at_sigterm.append(len(runtime.executing))
        executor.recoveries.append("SIGTERM")
        for active in get_task_manager()._active_tasks.values():
            active.context.shutdown.set()

    executor.request_recovery = sigterm
    server, h = serve(runtime)
    with server:
        _, cancel = start_and_cancel_first(h, sdk, server)
        cancel()
        assert h.wait("rid-1", "unknown").failure_code == "NATIVE_TURN_CANCELLED"
        recovery_requested(server, executor)
        assert kills == [4242] and executor.recoveries == ["SIGTERM"] and in_flight_at_sigterm == [0]
    _, restarted = offline_runtime(tmp_path)
    server, h = serve(restarted)
    with server:
        server.portal.call(asyncio.sleep, 0.5)  # core re-enters any task left active
        settled = h.call(restarted.read, "rid-1")
    assert (settled.status, settled.failure_code) == ("unknown", "NATIVE_TURN_CANCELLED")
    assert len(sdk.sends) == 1 and len(sdk.clients) == 1


@pytest.mark.parametrize("dies_on_kill", [True, False])
def test_an_idle_resident_stop_kills_a_cli_that_outlives_force_stop(monkeypatch, caplog, dies_on_kill):
    """#903 P3: an evicted (idle) SDK whose CLI ignores stop and force_stop is killed by its own PID too; one that
    survives that is logged, never raised (no turn and no slot: no recovery restart from here), and the pool registers
    it as unexited (review P3), so no SDK starts beside it."""
    from shared_context.native_resident import NativeResidentPool

    exited, kills, unexited = Event(), [], []

    def kill():
        kills.append(4243)
        if dies_on_kill:
            exited.set()

    def stop():
        raise RuntimeError("OFFLINE stop ignored")

    async def ignored():
        return None

    live = m2_copilot.ResidentSdk("OFFLINE-digest", "OFFLINE-sdk-session", None)
    live.client = SimpleNamespace(stop=stop, force_stop=ignored)
    live.process = SimpleNamespace(pid=4243, poll=lambda: 0 if exited.is_set() else None, kill=kill)
    monkeypatch.setattr(m2_copilot, "EXIT_CONFIRM_SECONDS", 0.1)
    monkeypatch.setattr(m2_copilot, "KILL_CONFIRM_SECONDS", 0.1, raising=False)  # absent before #903
    pool = NativeResidentPool(on_unexited=unexited.append)
    pool.holder("OFFLINE-sdk-session").live = live
    asyncio.run(pool.discard(pool.holder("OFFLINE-sdk-session")))
    assert kills == [4243]
    assert ("NATIVE_CLI_UNKILLABLE pid=4243 path=resident" in caplog.text) is not dies_on_kill
    assert unexited == ([] if dies_on_kill else [live.process])


def test_an_evicted_idle_sdk_whose_cli_survives_requests_the_recovery_restart(local_env, monkeypatch, tmp_path):
    """Review P2-1 on #903: an idle SDK evicted from the pool whose CLI survives stop, force_stop and FA's own kill is
    registered on the executor and asks for the same recovery restart a turn's survivor does (once), so the 503s it
    causes end with that restart instead of lasting until the CLI exits."""
    from shared_context.errors import Problem

    executor, _ = offline_runtime(tmp_path)
    kills = []

    def stop():
        raise RuntimeError("OFFLINE stop ignored")

    async def ignored():
        return None

    live = m2_copilot.ResidentSdk("OFFLINE-digest", "OFFLINE-evicted", None)
    live.client = SimpleNamespace(stop=stop, force_stop=ignored)
    live.process = SimpleNamespace(pid=4245, poll=lambda: None, kill=lambda: kills.append(4245))
    monkeypatch.setattr(m2_copilot, "EXIT_CONFIRM_SECONDS", 0.1)
    monkeypatch.setattr(m2_copilot, "KILL_CONFIRM_SECONDS", 0.1)
    pool = executor.resident
    pool.limit = 1

    async def evict():
        pool.holder("OFFLINE-evicted").live = live
        assert await pool.continue_live(pool.holder("OFFLINE-next"), None, checkpoint_profile=None,
                                        model_provider=None, model="OFFLINE-model") is None
        await asyncio.gather(*pool._stopping)

    asyncio.run(evict())
    assert kills == [4245] and executor.recoveries == ["SIGTERM"] and executor.runtime_unexited()
    with pytest.raises(Problem) as refused:
        executor.preflight(capture())
    assert refused.value.body["error"]["code"] == "NATIVE_EXECUTION_UNAVAILABLE"


def recovery_requested(server, executor):
    """The recovery restart a registration asked for, once any other started turn's execution ended (round 9)."""
    for _ in range(500):
        if executor.recoveries:
            return
        server.portal.call(asyncio.sleep, 0.01)


@pytest.mark.parametrize("force_stop", ["returns", "times_out"])
def test_a_replaced_kept_sdk_whose_cli_outlives_its_stop_starts_no_second_sdk(local_env, monkeypatch, tmp_path,
                                                                             force_stop):
    """Review P3 on #903: a kept SDK replaced for changed settings whose CLI survives stop, force_stop and FA's own
    kill is registered as unexited; the turn starts no second SDK beside it and asks for the recovery restart."""
    sdk, kills = PinnedSdk(monkeypatch), []

    def client(**connection):
        made = sdk.client(**connection)
        if len(sdk.clients) == 1:
            made._cli_process = SimpleNamespace(pid=4244, poll=lambda: None, returncode=None,
                                                kill=lambda: kills.append(4244))
            made.stop.side_effect = RuntimeError("OFFLINE stop ignored")
            # times_out: what wait_for(force_stop, 5) raises, without the 5 s
            made.force_stop.side_effect = TimeoutError if force_stop == "times_out" else None
        return made

    monkeypatch.setattr(m2_copilot, "CopilotClient", create_autospec(CopilotClient, side_effect=client))
    monkeypatch.setattr(m2_copilot, "EXIT_CONFIRM_SECONDS", 0.1)
    monkeypatch.setattr(m2_copilot, "KILL_CONFIRM_SECONDS", 0.1)
    # The second turn's settings differ from the kept SDK's, so run_copilot replaces it.
    monkeypatch.setattr(m2_copilot, "_resident_digest", lambda **_: f"OFFLINE-digest-{len(sdk.sends)}")
    executor, runtime = offline_runtime(tmp_path)
    server, h = serve(runtime)
    with server:
        first = turn(h, "rid-1")
        h.invoke(NativeSubmit(command=submit("rid-2", first.conversation_id).command, capture=capture()))
        h.wait("rid-2", "unknown")
        recovery_requested(server, executor)  # the stale stop's registration waits for rid-2's own execution
        assert kills == [4244] and executor.runtime_unexited() and executor.recoveries == ["SIGTERM"]
    assert len(sdk.clients) == 1 and len(sdk.sends) == 1


def test_a_turn_cancelled_while_its_replaced_sdk_stops_still_registers_the_surviving_cli(local_env, monkeypatch,
                                                                                         tmp_path):
    """Review P2-2 on #903: the turn's run_copilot is cancelled (e.g. its Responses stream closed) while the replaced
    kept SDK's shielded stop is still running; that stop finds the CLI alive after FA's own kill later, in the
    background, and registers it itself, so no second SDK starts beside it and the recovery restart is requested
    once."""
    entered, release, kills = Event(), Event(), []

    async def held_disconnect(options, fs):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)

    def client(**connection):
        made = sdk.client(**connection)
        if len(sdk.clients) == 1:
            made._cli_process = SimpleNamespace(pid=4246, poll=lambda: None, returncode=None,
                                                kill=lambda: kills.append(4246))
            made.stop.side_effect = RuntimeError("OFFLINE stop ignored")
        return made

    sdk = PinnedSdk(monkeypatch, on_disconnect=held_disconnect)  # the stale stop's disconnect is held

    monkeypatch.setattr(m2_copilot, "CopilotClient", create_autospec(CopilotClient, side_effect=client))
    monkeypatch.setattr(native_sdk, "ABORT_IDLE_SECONDS", 0.2)
    monkeypatch.setattr(m2_copilot, "EXIT_CONFIRM_SECONDS", 0.1)
    monkeypatch.setattr(m2_copilot, "KILL_CONFIRM_SECONDS", 0.1)
    monkeypatch.setattr(m2_copilot, "_resident_digest", lambda **_: f"OFFLINE-digest-{len(sdk.sends)}")
    runs, original_run = [], native_sdk.run_copilot

    async def tracked(**options):
        runs.append(asyncio.current_task())
        return await original_run(**options)

    monkeypatch.setattr(native_sdk, "run_copilot", tracked)
    executor, runtime = offline_runtime(tmp_path)
    server, h = serve(runtime)
    with server:
        first = turn(h, "rid-1")
        h.invoke(NativeSubmit(command=submit("rid-2", first.conversation_id).command, capture=capture()))
        for _ in range(500):
            if entered.is_set():
                break
            server.portal.call(asyncio.sleep, 0.01)
        assert entered.is_set() and len(runs) == 2
        server.portal.call(runs[1].cancel)
        server.portal.call(asyncio.sleep, 0.3)
        assert runs[1].done() and executor.recoveries == [] and not kills  # the stop is still running
        release.set()
        for _ in range(500):
            if executor.recoveries:
                break
            server.portal.call(asyncio.sleep, 0.01)
        assert kills == [4246] and executor.recoveries == ["SIGTERM"] and executor.runtime_unexited()
        refused = h.invoke(NativeSubmit(command=submit("rid-3", first.conversation_id).command, capture=capture()))
        assert refused.status_code == 503 and refused.json()["error"]["code"] == "NATIVE_EXECUTION_UNAVAILABLE"
    assert len(sdk.clients) == 1 and len(sdk.sends) == 1


def test_a_message_sent_while_a_cancelled_turns_replaced_sdk_still_stops_starts_no_second_sdk(local_env, monkeypatch,
                                                                                               tmp_path):
    """Review P2 (round 5) on #903: the turn's run_copilot is cancelled (as in the P2-2 test) while its replaced kept
    SDK's shielded stop is still running. Until that stop ends no SDK starts. Round 6: a new RID is taken (202, no
    resend) and waits accepted; it runs once after the stop ends, its SDK starting only after the first CLI exited."""
    entered, release, alive_at_start = Event(), Event(), []

    async def held_disconnect(options, fs):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)

    def client(**connection):
        alive_at_start.append([made._cli_process.poll() is None for made in sdk.clients])
        return sdk.client(**connection)

    sdk = PinnedSdk(monkeypatch, on_disconnect=held_disconnect)  # the stale stop's disconnect is held
    monkeypatch.setattr(m2_copilot, "CopilotClient", create_autospec(CopilotClient, side_effect=client))
    monkeypatch.setattr(native_sdk, "ABORT_IDLE_SECONDS", 0.2)
    monkeypatch.setattr(m2_copilot, "_resident_digest", lambda **_: f"OFFLINE-digest-{len(sdk.sends)}")
    runs, original_run = [], native_sdk.run_copilot

    async def tracked(**options):
        runs.append(asyncio.current_task())
        return await original_run(**options)

    monkeypatch.setattr(native_sdk, "run_copilot", tracked)
    executor, runtime = offline_runtime(tmp_path)
    server, h = serve(runtime)
    with server:
        cid = turn(h, "rid-1").conversation_id
        h.invoke(NativeSubmit(command=submit("rid-2", cid).command, capture=capture()))
        for _ in range(500):
            if entered.is_set():
                break
            server.portal.call(asyncio.sleep, 0.01)
        assert entered.is_set() and len(runs) == 2
        server.portal.call(runs[1].cancel)
        for _ in range(500):
            if runs[1].done():
                break
            server.portal.call(asyncio.sleep, 0.01)
        assert runs[1].done() and not release.is_set()  # the stop is still running
        taken = h.invoke(NativeSubmit(command=submit("rid-3", cid).command, capture=capture()))
        assert taken.status_code == 202, taken.text
        server.portal.call(asyncio.sleep, 0.3)
        assert h.call(h.runtime.read, "rid-3").status == "accepted" and len(sdk.clients) == 1
        release.set()
        assert text(h, h.wait("rid-3", "completed")) == "OFFLINE answer 2"
    assert executor.recoveries == [] and len(sdk.clients) == 2 and alive_at_start == [[], [False]]


def _held_stops(monkeypatch):
    """A PinnedSdk whose stops hold in disconnect while ``hold`` is set, until ``release``; ``alive_at_start`` lists,
    for each new client, whether every earlier client's CLI was still running."""
    hold, release, alive_at_start = Event(), Event(), []

    async def held_disconnect(options, fs):
        while hold.is_set() and not release.is_set():
            await asyncio.sleep(0.01)

    def client(**connection):
        alive_at_start.append([made._cli_process.poll() is None for made in sdk.clients])
        return sdk.client(**connection)

    sdk = PinnedSdk(monkeypatch, on_disconnect=held_disconnect)
    monkeypatch.setattr(m2_copilot, "CopilotClient", create_autospec(CopilotClient, side_effect=client))
    return sdk, hold, release, alive_at_start


def _evict_idle(server, pool):
    """As ``_evict`` does for the least recently used idle kept SDK over the limit: its stop runs in the background,
    no turn waiting."""
    holder = next(iter(pool._holders.values()))
    assert holder.live is not None and holder.live.turn is None

    async def evict():
        pool._stop_later(holder)

    server.portal.call(evict)
    assert pool.stopping()


def test_messages_sent_while_an_idle_eviction_stops_are_taken_and_answered_in_send_order(local_env, monkeypatch,
                                                                                        tmp_path):
    """Review P2 (round 6) on #903: an ordinary idle eviction's stop no longer refuses a send (it was a 503 that the
    workbench resent 3 s later, so B, sent after A, ran first). A, sent while the stop runs, and B, sent after it,
    are both taken; A waits accepted only until the stop ends, starts no client beside the old CLI, and the replies
    come back in send order."""
    sdk, hold, release, alive_at_start = _held_stops(monkeypatch)
    executor, runtime = offline_runtime(tmp_path)
    server, h = serve(runtime)
    with server:
        cid = turn(h, "rid-1").conversation_id
        hold.set()
        _evict_idle(server, executor.resident)
        first = h.invoke(NativeSubmit(command=submit("rid-a", cid).command, capture=capture()))
        server.portal.call(asyncio.sleep, 0.2)
        second = h.invoke(NativeSubmit(command=submit("rid-b", cid).command, capture=capture()))
        assert (first.status_code, second.status_code) == (202, 202), (first.text, second.text)
        server.portal.call(asyncio.sleep, 0.3)
        assert h.call(h.runtime.read, "rid-a").status == "accepted" and len(sdk.clients) == 1
        released = time.monotonic()
        release.set()
        a = h.wait("rid-a", "completed")
        waited = time.monotonic() - released
        b = h.wait("rid-b", "completed")
        assert (text(h, a), text(h, b)) == ("OFFLINE answer 2", "OFFLINE answer 3")
    # After the stop ends A waits at most one 0.1 s poll; the bound only absorbs a loaded test machine.
    assert waited < 2, waited
    assert executor.recoveries == [] and len(sdk.clients) == 2 and alive_at_start == [[], [False]]


def test_a_conversation_continuing_its_own_kept_sdk_is_not_held_by_another_conversations_stop(local_env, monkeypatch,
                                                                                             tmp_path):
    """Review P3 (round 6) on #903: while conversation X's evicted SDK still stops, conversation Y continues on its own
    live kept SDK and starts no client, so it neither waits for nor is refused by X's stop."""
    sdk, hold, release, _ = _held_stops(monkeypatch)
    executor, runtime = offline_runtime(tmp_path)
    server, h = serve(runtime)
    with server:
        x = turn(h, "rid-x1").conversation_id
        y = turn(h, "rid-y1").conversation_id
        hold.set()
        _evict_idle(server, executor.resident)  # X: the least recently used
        assert text(h, turn(h, "rid-y2", y)) == "OFFLINE answer 3"
        assert executor.resident.stopping() and not release.is_set() and len(sdk.clients) == 2
        release.set()
    assert executor.recoveries == []


def test_a_cold_turn_that_evicts_another_conversations_idle_sdk_starts_at_once(local_env, monkeypatch, tmp_path):
    """Review P2 (round 7) on #903: with the pool full, a new conversation's turn evicts another conversation's idle
    kept SDK in continue_live. That stop shares nothing with the new client (ADR-0005 keeps two CLIs alive), so the
    turn starts and completes while the evicted CLI is still stopping, with no wait."""
    sdk, hold, release, alive_at_start = _held_stops(monkeypatch)
    executor, runtime = offline_runtime(tmp_path)
    executor.resident.limit = 1
    server, h = serve(runtime)
    with server:
        turn(h, "rid-1")
        hold.set()
        assert text(h, turn(h, "rid-2")) == "OFFLINE answer 2"
        assert executor.resident.stopping() and not release.is_set()  # completed while that stop is still held
        release.set()
    assert executor.recoveries == [] and alive_at_start == [[], [True]]


def test_a_cold_turn_whose_eviction_leaves_an_unkillable_cli_finishes_once_before_the_recovery_restart(
        local_env, monkeypatch, tmp_path):
    """Review P3 (round 6) / P2 (round 9) on #903, in the real order: a new conversation's turn B evicts conversation
    A's idle kept SDK in continue_live. settle does not wait for another conversation's stop (round 7), so B's client
    starts while A's stop still runs; only then is A's CLI found to survive stop, force_stop and FA's own kill, and
    registered. The recovery restart it asks for waits for B, which is in flight: B finishes and is saved once (never
    cut, so never re-run as a continuation), and then the restart is requested once. The recovery here does what the
    platform's SIGTERM does first: it sets every active task's shutdown."""
    from azure.ai.agentserver.core.tasks._manager import get_task_manager

    kills, registered_at_b_start = [], []

    async def b_until_a_is_registered(record):
        if len(sdk.sends) == 2:
            registered_at_b_start.append(list(executor._unexited))
            for _ in range(500):
                if executor._unexited:
                    break
                await asyncio.sleep(0.01)

    sdk = PinnedSdk(monkeypatch, during_send=b_until_a_is_registered)
    alive_at_start = []

    def client(**connection):
        alive_at_start.append([made._cli_process.poll() is None for made in sdk.clients])
        made = sdk.client(**connection)
        if len(sdk.clients) == 1:
            made._cli_process = SimpleNamespace(pid=4247, poll=lambda: None, returncode=None,
                                                kill=lambda: kills.append(4247))
            made.stop.side_effect = RuntimeError("OFFLINE stop ignored")
        return made

    monkeypatch.setattr(m2_copilot, "CopilotClient", create_autospec(CopilotClient, side_effect=client))
    # A's stop finds its CLI alive about 1 s after the eviction, well after B's client started.
    monkeypatch.setattr(m2_copilot, "EXIT_CONFIRM_SECONDS", 0.5)
    monkeypatch.setattr(m2_copilot, "KILL_CONFIRM_SECONDS", 0.5)
    executor, runtime = offline_runtime(tmp_path)
    executor.resident.limit = 1

    def sigterm():
        executor.recoveries.append("SIGTERM")
        for active in get_task_manager()._active_tasks.values():
            active.context.shutdown.set()

    executor.request_recovery = sigterm
    server, h = serve(runtime)
    with server:
        turn(h, "rid-1")
        h.invoke(NativeSubmit(command=submit("rid-2").command, capture=capture()))
        assert text(h, h.wait("rid-2", "completed")) == "OFFLINE answer 2"
        recovery_requested(server, executor)
        assert kills == [4247] and executor.runtime_unexited() and executor.recoveries == ["SIGTERM"]
    # B's client started before A's CLI was registered (the documented window), beside it; B ran once.
    assert registered_at_b_start == [[]] and alive_at_start == [[], [True]]
    assert len(sdk.clients) == 2 and len(sdk.sends) == 2


def test_a_recovery_restart_sends_sigterm_only_after_another_started_turn_finishes(monkeypatch, caplog):
    """Review P1 (round 10) on #903: a CLI registered from outside a started turn asks for the recovery restart only
    after that turn's execution ends, however long it runs (a cut turn would re-run as a continuation); with no turn
    in flight it asks at once. Round 11: one registered from the turn itself returns at once (the turn never waits
    for itself) and asks once that turn's own execution ended, or not at all when the CLI exited meanwhile."""
    from shared_context import native_turns

    executor, runtime = offline_runtime(Path("OFFLINE-unused"))
    monkeypatch.setattr(native_turns, "UNEXITED_WAIT_SECONDS", 0.2)  # a short bound would fire within the test
    alive = SimpleNamespace(poll=lambda: None, pid=4247)
    seen = []

    def own_turn(process, exits):
        async def run_started(original, control, signals):
            executor._register_unexited(process)  # returns at once: no wait on this turn itself
            await asyncio.sleep(0.3)
            seen.append(list(executor.recoveries))  # still in flight: no SIGTERM yet
            exits()
            return "OFFLINE saved"
        runtime._run_started = run_started
        return runtime._execute_started(None, None, None)

    async def scenario():
        exited = []
        assert await asyncio.wait_for(own_turn(SimpleNamespace(poll=lambda: None, pid=4248), lambda: None), 2) == (
            "OFFLINE saved")
        await asyncio.sleep(0.05)
        assert seen == [[]] and executor.recoveries == ["SIGTERM"]  # after the turn's own end
        executor.recoveries.clear()
        executor._unexited.clear()
        gone = SimpleNamespace(poll=lambda: 0 if exited else None, pid=4249)
        await asyncio.wait_for(own_turn(gone, lambda: exited.append(True)), 2)
        await asyncio.sleep(0.05)
        assert seen == [[], []] and executor.recoveries == []  # its CLI exited meanwhile: no restart
        executor._unexited.clear()
        executor._register_unexited(SimpleNamespace(poll=lambda: None, pid=4246))
        assert executor.recoveries == ["SIGTERM"]  # nothing else in flight
        executor.recoveries.clear()
        executor._unexited.clear()
        running = asyncio.get_running_loop().create_future()
        runtime.executing[running] = None  # another started turn, longer than any bound
        executor._register_unexited(alive)
        await asyncio.sleep(0.5)
        assert executor.recoveries == [] and executor.runtime_unexited()
        running.set_result(None)  # its execution ended (saved)
        await asyncio.sleep(0.05)
        assert executor.recoveries == ["SIGTERM"]

    asyncio.run(scenario())
    assert "NATIVE_RECOVERY_WAIT_ENDED pid=4248 cli_exited=False" in caplog.text
    assert "NATIVE_RECOVERY_WAIT_ENDED pid=4249 cli_exited=True" in caplog.text


def test_a_vm_serving_many_conversations_keeps_their_sdk_session_ids_only_while_needed():
    """Review P3 (round 9) on #903: a conversation's SDK session ID is kept while its holder or a stop of its SDK is,
    so 1000 conversations leave only those, and an evicted conversation whose stop still runs is still found."""
    from shared_context.native_resident import NativeResidentPool

    pool = NativeResidentPool(limit=1)
    for n in range(1000):
        pool.holder(f"OFFLINE-sdk-{n}", f"OFFLINE-cid-{n}")
    assert len(pool._conversations) == 1
    running = create_autospec(asyncio.Task, instance=True)
    running.done.return_value = False
    pool._stopping[running] = "OFFLINE-sdk-1000"  # its stop still runs
    pool.holder("OFFLINE-sdk-1000", "OFFLINE-cid-1000")
    pool.holder("OFFLINE-sdk-1001", "OFFLINE-cid-1001")
    assert set(pool._conversations) == {"OFFLINE-cid-1000", "OFFLINE-cid-1001"}
    assert pool.stopping("OFFLINE-cid-1000") and not pool.stopping("OFFLINE-cid-1001")
