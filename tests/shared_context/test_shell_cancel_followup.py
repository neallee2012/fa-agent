"""A cancelled long run_command keeps its conversation: the stopped SDK state is saved, and the next turn continues.

Pinned SDK 1.0.13 / runtime 1.0.83 behind the real API app, Hosted session and checkpoint store. On Linux (the
desktop image as UID10001) the real shell runs; elsewhere a same-named stand-in writes runs.log and blocks.
"""

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context import behavior_assistant_hosted as hosted
from shared_context import behavior_shell_capability as shell
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_wire import StoredSdkArchive
from shared_context.behavior_assistant import key
from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_operations import control_key
from shared_context.behavior_sdk_checkpoint import SdkCheckpointStore, checkpoint_key
from shared_context.errors import Problem
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.m2_copilot import ToolOutput, ToolSpec
from sdk_checkpoint_local import sse
from test_behavior_assistant import assistant
from shared_context.behavior_history_prompt import HISTORY_PROFILE
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models
from test_instruction_prompt import saved_turn, submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key

REAL_SHELL = sys.platform.startswith("linux") and os.geteuid() != 0
LONG = "echo start >> runs.log; sleep 60"
CHECK = "cat runs.log; echo sleeping=$(pgrep -xc sleep || true)"


def stand_in(started, cancelled):
    """Same tool name, schema and result model; the files directory is the one the real shell uses."""

    def factory(session, grant):
        files = Path(session.root).parent / "desktop" / "files"
        files.mkdir(parents=True, exist_ok=True)

        async def handle(packet):
            command = packet["arguments"]["command"]
            if command == LONG:
                with open(files / "runs.log", "a", encoding="utf-8") as log:
                    log.write("start\n")
                started.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise
                text = ""
            else:
                text = (files / "runs.log").read_text(encoding="utf-8") + "sleeping=0\n"
            return ToolOutput(shell.CommandResult(exit_code=0, output=text, output_bytes=len(text),
                                                  truncated=False).model_dump_json())
        tool = ToolSpec("run_command", "Run one bash command.", shell.RunCommandArguments.model_json_schema(), handle)
        return (tool,), {tool.name: shell.CommandResult}
    return factory


@pytest.mark.parametrize("real, race", [(False, None), (False, "epoch-after-cancel"), (False, "epoch-after-put"),
    pytest.param(True, None, marks=pytest.mark.skipif(
        not REAL_SHELL, reason="the real shell needs a non-root Linux Hosted runtime"))])
def test_cancelled_long_command_keeps_the_conversation_and_its_sandbox(assistant, monkeypatch, real, race):
    """A newer owner epoch fences the cancelled execution: no checkpoint is published and no PUT is repeated."""
    h, control, human = configured(assistant)
    h.runtime.capability_grants = available_grants(None, ("fa-shell-v1",))
    chosen, _ = select(h, request_id="choose-shell", previous=None, value=template(
        HISTORY_PROFILE, user_text_file_profile="fa-user-text-files-v1",
        capabilities=(h.runtime.capability_grants["fa-shell-v1"].ref,)))
    assert chosen.status_code == 200, chosen.text
    started, cancelled, sessions = threading.Event(), [], []
    if real:
        monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost:42356/msi/token")
        monkeypatch.setenv("IDENTITY_HEADER", "fa-cancel-canary")
        monkeypatch.setattr(shell, "is_dumpable", lambda: False)
        base = shell.shell_capability
    else:
        base = stand_in(started, cancelled)

    def factory(session, grant):
        sessions.append(session)
        return base(session, grant)
    monkeypatch.setitem(hosted.CAPABILITY_HANDLERS, "fa-shell-v1", factory)
    requests, outputs, puts = [], [], []
    advanced = []

    def advance_epoch():
        owner_key = control_key("owner", control.records[accepted["binding_id"]].binding.owner.owner_record_id)
        before = h.store.read_snapshot((owner_key,))[owner_key].payload["data"]["epoch"]
        for attempt in range(20):
            try:
                h.mutate(owner_key, epoch=before + 1)
                break
            except Problem as error:
                # Hosted writes progress concurrently; the synthetic owner change retries its own write only.
                if error.status != 412 or attempt == 19:
                    raise
                time.sleep(0.05)
        advanced.append((owner_key, before, h.store.read_snapshot((owner_key,))[owner_key].payload["data"]["epoch"]))
    original_save = SdkCheckpointStore.save

    def save(self, *args, **options):
        puts.append(True)
        reference = original_save(self, *args, **options)
        if race == "epoch-after-put":
            advance_epoch()
        return reference
    monkeypatch.setattr(SdkCheckpointStore, "save", save)

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        tool_messages = [item for item in body["messages"] if item["role"] == "tool"]
        user = [item for item in body["messages"] if item["role"] == "user"]
        text = json.dumps(user[-1]["content"])
        if "Start the long command" in text and not tool_messages[-1:]:
            delta = {"tool_calls": [{"index": 0, "id": "call-long", "type": "function",
                                     "function": {"name": "run_command", "arguments": json.dumps({"command": LONG})}}]}
        elif "Check runs.log" in text and not any(item.get("tool_call_id") == "call-check" for item in tool_messages):
            delta = {"tool_calls": [{"index": 0, "id": "call-check", "type": "function",
                                     "function": {"name": "run_command", "arguments": json.dumps({"command": CHECK})}}]}
        else:
            if tool_messages:
                outputs.append(tool_messages[-1]["content"])
            delta = {"content": "Done."}
        return sse(request, delta, len(requests))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_SHELL_PROVIDER")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: None, require_current_native_evidence=lambda: None))
    parent = Path(__file__).resolve().parents[2] / ".azure"
    parent.mkdir(exist_ok=True)
    with TestClient(h.app) as client, TemporaryDirectory(prefix="shell-cancel-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory)
        accepted, _ = submit(h, "long-command", "Start the long command.")

        def cancel_when_running():
            deadline = time.monotonic() + 60
            while not (started.is_set() or list(Path(directory).rglob("desktop/files/runs.log"))):
                assert time.monotonic() < deadline
                time.sleep(0.05)
            time.sleep(0.5)
            control.cancel(human, accepted["binding_id"])
            try:
                if race == "epoch-after-cancel":
                    advance_epoch()
            finally:
                sessions[0].request_cancel()
        watcher = threading.Thread(target=cancel_when_running)
        watcher.start()
        began = time.monotonic()
        try:
            first = h.runtime.controller.step(accepted["binding_id"])
        except Exception:
            if race is None:
                raise
            first = None
        watcher.join()
        assert time.monotonic() - began < 45, "the command was not stopped by the cancellation"
        job_id = control.records[accepted["binding_id"]].binding.job_id
        if not real:
            assert cancelled == [True]
        upload = h.store.read_snapshot((checkpoint_key(job_id),))[checkpoint_key(job_id)]
        if race is not None:
            assert advanced and advanced[0][2] == advanced[0][1] + 1, advanced
            reference = None if upload is None else upload.payload["data"].get("reference")
            assert reference is None and len(puts) == (1 if race == "epoch-after-put" else 0)
            archive_row = h.store.read_snapshot((key("text-archive", job_id),))[key("text-archive", job_id)]
            assert archive_row is None or not isinstance(saved_turn(h, job_id).archive, StoredSdkArchive)
            assert not control.conversation(human, accepted["conversation_id"])["can_follow_up"]
            return
        assert first.state == "cancelled" and first.result_forwarded, first
        assert len(puts) == 1 and upload.payload["data"]["reference"] is not None
        archived = saved_turn(h, job_id)
        assert isinstance(archived.archive, StoredSdkArchive), archived.archive
        progress = control.conversation(human, accepted["conversation_id"])
        assert progress["can_follow_up"], progress["follow_up_blocker"]

        followed, _ = submit(h, "check-log", "Check runs.log.", accepted["conversation_id"])
        second = h.runtime.controller.step(followed["binding_id"])
        assert second.state == "succeeded" and second.result_forwarded
    result = shell.CommandResult.model_validate_json(outputs[-1])
    assert result.output.splitlines() == ["start", "sleeping=0"]
