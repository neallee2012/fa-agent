"""ADR-0005 on the native lane: the real ``run_copilot`` over a pinned-SDK double.

No runtime process, model or network: the double writes the SDK's SessionFs files and reaches idle, so the
tests see exactly when FA keeps, continues, restores or stops the SDK between native turns.
"""
import asyncio
import json
import logging
import subprocess
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
from shared_context.behavior_native_turn_wire import NativeResult, NativeSubmit, NativeTurnCapture
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

    async def stop():
        stopped.append(True)

    files = SimpleNamespace(files={"events.jsonl": "x" * (MAX_BACKUP_BYTES // 4)}, checkpoint_profile=None,
                            model_provider=None, continue_from=lambda state: None)
    live = SimpleNamespace(files=files, released_sha256="a" * 64, alive=lambda: True, reusable=lambda: True,
                           stop=stop, turn=None)

    async def continued(needed):
        pool = NativeResidentPool()
        holder = pool.holder("sid")
        holder.live = live
        found = await pool.continue_live(holder, SimpleNamespace(sha256="a" * 64), checkpoint_profile=None,
                                         model_provider=None, needed_bytes=needed)
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

    async def stop():
        started.set()
        await release.wait()
        stopped.append(True)

    async def scenario():
        pool = NativeResidentPool()
        holder = pool.holder("sid")
        holder.live = SimpleNamespace(stop=stop, turn=None)
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

    async def observed(self):
        # Turns run one at a time under execution_slot: the evicted SDK serves no turn at this moment.
        stops.append((self.client, self.turn, runtime.execution_owner))
        await original_stop(self)

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
