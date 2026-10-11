"""M4 Hosted contracts with fake SDK and in-memory HTTP boundaries only."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from shared_context.errors import Problem
from shared_context.hosted import HostedClient
from shared_context.m2_assistant import AssistantCommand, AssistantEngine
from shared_context.m2_copilot import ToolFailure
from test_m2_assistant import AGENT, hosted_runtime


RUN = "m4-context-01"
JOB = RUN + "-short-cold"
WORK = RUN + "-work"
FIXTURE_HASH = "8f37192bd3c9aa93d4f7855eb5ec133ba368d136772bba96147c4cf19178c090"
PROGRESS = "Externally seeded current decision; never replace this with a checkpoint label."
POLICY_TEXT = "APPROVED_POLICY=TEST"
POLICY_CITATION = "policy:synthetic-policy@" + "a" * 32
ANSWER = json.dumps({
    name: ({"value": POLICY_TEXT, "citation": POLICY_CITATION} if name == "policy_marker"
           else {"value": "synthetic evidence", "citation": "synthetic source"})
    for name in ("decision_owner", "report_layout", "private_theme", "policy_marker")
})


def command(action="start", **fields):
    payload = {"operation": "assistant", "action": action, "target_agent_id": AGENT, "run_id": JOB}
    if action == "start":
        payload.update(team="team-one", work=WORK, prompt="Read current Project Lyra context.",
                       input_text="", evaluation_run_id=RUN)
    return {**payload, **fields}


def enabled(monkeypatch):
    monkeypatch.setenv("CONTEXT_M4_TOOLS_ENABLED", "true")
    monkeypatch.setenv("CONTEXT_M4_RUN_ID", RUN)


@pytest.mark.parametrize("feature_enabled", [False, True], ids=["m4-disabled", "m4-enabled"])
def test_fresh_hosted_import_rejects_api_table_auth_and_server_m4_dependencies(feature_enabled):
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    program = """
import importlib.abc
import os
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
os.environ['CONTEXT_M4_TOOLS_ENABLED'] = sys.argv[2]
os.environ['CONTEXT_M4_RUN_ID'] = 'm4-context-01'
os.environ['CONTEXT_M3_TOOLS_ENABLED'] = 'true'
blocked = (
    'azure.data', 'azure.storage', 'jwt', 'fastapi',
    'shared_context.api', 'shared_context.auth', 'shared_context.service',
    'shared_context.storage', 'shared_context.production',
    'shared_context.m3_auth', 'shared_context.m3_artifacts', 'shared_context.m3_cli',
    'shared_context.m4_context', 'shared_context.m4_api', 'shared_context.m4_config',
)
class BlockApiOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('Hosted imported an API-only dependency: ' + fullname)
sys.meta_path.insert(0, BlockApiOnly())
import shared_context.hosted
import shared_context.m2_copilot
from shared_context.m2_assistant import AssistantCommand, m4_enabled
assert m4_enabled() == (sys.argv[2] == 'true')
shared_context.hosted.ModelBudgetProbe(operation='model_budget', milestone='m4')
shared_context.hosted.M4SeedProbe(fixture_id='short_team', suite_sha256='a' * 64)
AssistantCommand.model_validate({
    'operation': 'assistant', 'action': 'start',
    'target_agent_id': '00000000-0000-0000-0000-00000000005d',
    'run_id': 'm4-context-01-check', 'team': 'team-one', 'work': 'm4-context-01-work',
    'prompt': 'Read current context.', 'input_text': '',
    'evaluation_run_id': 'm4-context-01', 'require_checkpoint': True,
})
assert not any(fullname == name or fullname.startswith(name + '.')
               for fullname in sys.modules for name in blocked)
print('Hosted M4 client/SDK imports remain independent of API/Table/human-auth modules')
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-I", "-B", "-c", program, str(source),
         "true" if feature_enabled else "false"],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "independent of API/Table/human-auth" in result.stdout


def status_until(engine, terminal=("completed", "failed", "incomplete")):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = engine.dispatch(command("status"))
        if status["status"] in terminal:
            return status
        time.sleep(0.01)
    pytest.fail("M4 fake runner did not reach its terminal state")


class ContextBoundary:
    api_url = "https://context.invalid"
    api_audience = "api://11111111-1111-1111-1111-111111111111/poc"
    credential = None

    def __init__(self):
        self.record = {"owner": "agent-a", "status": "draft", "revision": 1, "etag": '"one"',
                       "progress": PROGRESS, "artifact": {"text": "", "sha256": ""}}
        self.calls = []
        self.mutations = 0
        self.fail_context = False
        self.lose_update = False
        self.extra_context = ""
        digest = hashlib.sha256(POLICY_TEXT.encode()).hexdigest()
        self.policy = {
            "artifact_id": "synthetic-policy", "status": "approved", "text": POLICY_TEXT,
            "version": {"version_id": "a" * 32, "sha256": digest,
                        "approval": {"decision": "approve", "version_id": "a" * 32, "sha256": digest}},
        }

    def request(self, method, url, scope, body=None, headers=None):
        self.calls.append((method, url, copy.deepcopy(body)))
        path = f"/teams/team-one/works/{WORK}"
        if url.endswith("/context"):
            assert method == "GET" and url == self.api_url + "/m4" + path + "/context"
            if self.fail_context:
                return {"http_status": 503, "content": {}, "truncated": False}
            content = {"work": copy.deepcopy(self.record), "team_memory": [],
                       "private_memory": [], "approved_policy_context": [copy.deepcopy(self.policy)],
                       "extra": self.extra_context}
        else:
            assert url == self.api_url + path
            if method == "PATCH":
                assert headers["If-Match"] == self.record["etag"]
                assert set(body) == {"artifact_text"}
                self.mutations += 1
                self.record["revision"] += 1
                self.record["etag"] = f'"revision-{self.record["revision"]}"'
                self.record["artifact"] = {
                    "text": body["artifact_text"], "sha256": hashlib.sha256(body["artifact_text"].encode()).hexdigest(),
                }
                if self.lose_update:
                    raise ConnectionResetError("synthetic accepted update with lost response")
            else:
                assert method == "GET"
            content = copy.deepcopy(self.record)
        return {"http_status": 200, "content": content, "etag": self.record["etag"], "truncated": False}


def tools_for(options):
    tools = {tool.name: tool for tool in options["tools"]}

    async def call(name, args=None):
        return await tools[name].handler({
            "session_id": options["sdk_session_id"], "tool_call_id": uuid4().hex,
            "tool_name": name, "arguments": args or {},
        })

    return tools, call


def outcome(options):
    return {"sdk_session_id": options["sdk_session_id"], "history": [], "events": [], "final_message": ANSWER}


@pytest.mark.parametrize("tool_name", ["fa_write_file", "update_work"])
def test_shortened_policy_marker_is_rejected_before_immutable_output_or_patch(tmp_path, monkeypatch, tool_name):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)
    context = ContextBoundary()
    invalid = json.loads(ANSWER)
    invalid["policy_marker"]["value"] = "TEST"
    invalid_text = json.dumps(invalid)

    async def sdk(**options):
        _, call = tools_for(options)
        await call("read_context")
        arguments = ({"name": "result.txt", "text": invalid_text} if tool_name == "fa_write_file"
                     else {"step": "final", "progress": "final", "artifact_text": invalid_text})
        rejected = False
        try:
            await call(tool_name, arguments)
        except ToolFailure as exc:
            assert "M4_RESULT_INVALID" in str(exc)
            rejected = True
        assert rejected, "abbreviated policy marker reached an immutable output boundary"
        assert context.mutations == 0
        assert not list(tmp_path.rglob("result.txt"))
        await call("fa_write_file", {"name": "result.txt", "text": ANSWER})
        await call("update_work", {"step": "final", "progress": "final", "artifact_text": ANSWER})
        await call("finish_task")
        return outcome(options)

    engine = AssistantEngine(context, home=tmp_path, process_marker="marker-check", runner=sdk)
    engine.dispatch(command())
    result = status_until(engine)
    assert result["status"] == "completed", result.get("error", result)
    assert result["task_verified"] is True
    assert context.mutations == 1


@pytest.mark.parametrize("field,value", [
    ("evaluation_run_id", "m4-context-02"), ("run_id", "m2-unit"), ("work", "legacy-work"),
    ("run_id", RUN + "-"), ("work", RUN), ("handoff_to", "agent-b"), ("allow_publish", True),
    ("artifact_task", {"artifact_id": "report", "kind": "deliverable", "title": "Synthetic",
                       "version_id": "a" * 32, "mode": "create"}),
])
def test_m4_start_rejects_other_runs_and_mixed_tasks(field, value):
    with pytest.raises(ValidationError):
        AssistantCommand.model_validate(command(**{field: value}))


def test_m4_checkpoint_is_allowed_without_relaxing_m3_or_enabling_desktop():
    parsed = AssistantCommand.model_validate(command(require_checkpoint=True))
    assert parsed.require_checkpoint and parsed.evaluation_run_id == RUN
    with pytest.raises(ValidationError):
        AssistantCommand.model_validate(command(desktop_variant=0, desktop_label="sample", desktop_count=1))
    legacy = command(evaluation_run_id=None, require_checkpoint=True, artifact_task={
        "artifact_id": "report", "kind": "deliverable", "title": "Synthetic",
        "version_id": "a" * 32, "mode": "create",
    })
    with pytest.raises(ValidationError):
        AssistantCommand.model_validate(legacy)
    assert "evaluation_run_id" not in AssistantCommand.model_json_schema()["required"]
    for action in ("resume", "status", "evidence"):
        with pytest.raises(ValidationError):
            AssistantCommand.model_validate(command(action, evaluation_run_id=RUN))


@pytest.mark.parametrize("flag,configured", [
    (None, RUN), ("false", RUN), ("True", RUN), ("true", None), ("true", RUN + "-other"),
])
def test_m4_start_cannot_enable_itself_and_writes_no_state(tmp_path, monkeypatch, flag, configured):
    hosted_runtime(monkeypatch, tmp_path)
    for key, value in (("CONTEXT_M4_TOOLS_ENABLED", flag), ("CONTEXT_M4_RUN_ID", configured)):
        monkeypatch.delenv(key, raising=False)
        if value is not None:
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("CONTEXT_M3_TOOLS_ENABLED", "true")
    context = ContextBoundary()
    engine = AssistantEngine(context, home=tmp_path, process_marker="disabled")
    with pytest.raises(Problem) as exc:
        engine.dispatch(command())
    assert exc.value.body["error"]["code"] == "M4_DISABLED"
    assert context.calls == []
    assert not (tmp_path / ".fa-m1" / "m2" / AGENT).exists()


def test_m4_reads_isolated_context_keeps_authoritative_progress_and_native_evidence(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)
    context = ContextBoundary()
    usage = {"type": "assistant.usage", "data": {"input_tokens": 123, "output_tokens": 17, "model": "synthetic"}}
    seen_context = []

    async def sdk(**options):
        assert options["milestone"] == "m4" and not options["resume"]
        prompt = options["prompt"]
        assert "read_context first in this process" in prompt
        for secret in (PROGRESS, "mira", "cobalt", "saffron", "APPROVED_POLICY=V3"):
            assert secret not in prompt
        for field in ("decision_owner", "report_layout", "private_theme", "policy_marker", "citation"):
            assert field in prompt
        tools, call = tools_for(options)
        assert set(tools) == {"read_context", "fa_read_file", "fa_write_file", "update_work", "finish_task"}
        seen_context.append((await call("read_context")).text)
        options["on_event"](usage)
        await call("fa_write_file", {"name": "result.txt", "text": ANSWER})
        update = {"step": "final", "progress": "must not become current facts", "artifact_text": ANSWER}
        await call("update_work", update)
        await call("update_work", {**update, "progress": "different label, identical effect"})
        await call("finish_task")
        return outcome(options)

    engine = AssistantEngine(context, home=tmp_path, process_marker="first", runner=sdk)
    engine.dispatch(command())
    final = status_until(engine)
    assert final["status"] == "completed", final
    assert context.record["progress"] == PROGRESS and context.mutations == 1
    evidence = engine.dispatch(command("evidence"))
    assert evidence["evaluation_run_id"] == RUN and evidence["context_read_process_marker"] == "first"
    assert evidence["usage"] == [usage]
    binding = evidence["usage_bindings"][0]
    assert binding["usage_index"] == 0 and binding["process_marker"] == "first"
    assert binding["sdk_session_id"] == evidence["sdk_session_id"]
    receipt = evidence["context_reads"][0]
    raw = Path(receipt["path"]).read_bytes()
    assert raw == seen_context[0].encode("utf-8")
    assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["bytes"] == len(raw)
    assert receipt["process_marker"] == "first" and receipt["sdk_session_id"] == evidence["sdk_session_id"]
    assert evidence["tool_calls"][0]["id"] == receipt["tool_call_id"]
    assert receipt["duration_ms"] == evidence["tool_calls"][0]["duration_seconds"] * 1000
    assert receipt["duration_source"] == "hosted_tool_handler_monotonic"
    assert receipt["execution_status"] == "completed"
    assert all(call["duration_seconds"] >= 0 and call["completed_at"]
               and call["execution_status"] == "completed" for call in evidence["tool_calls"])
    assert evidence["files"]["result.txt"]["text"] == ANSWER
    assert json.loads(Path(evidence["sdk_outcome"]["path"]).read_bytes())["final_message"] == ANSWER


@pytest.mark.parametrize("with_usage", [False, True])
def test_pending_checkpoint_exposes_one_immutable_native_measurement_file(tmp_path, monkeypatch, with_usage):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)
    context, stop = ContextBoundary(), threading.Event()
    observed_context, event_sinks = [], []
    usage = {"type": "assistant.usage", "data": {"model": "synthetic", "input_tokens": 121, "output_tokens": 17}}

    async def sdk(**options):
        _, call = tools_for(options)
        event_sinks.append(options["on_event"])
        observed_context.append((await call("read_context")).text)
        if with_usage:
            options["on_event"](usage)
        await call("fa_write_file", {"name": "draft.txt", "text": "checkpoint draft"})
        await call("update_work", {"step": "draft", "progress": "private label", "artifact_text": "checkpoint draft"})
        pending = asyncio.create_task(call("checkpoint_and_wait"))
        try:
            while not stop.is_set():
                await asyncio.sleep(0.01)
        finally:
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
        raise RuntimeError("synthetic process stop without an SDK outcome")

    engine = AssistantEngine(context, home=tmp_path, process_marker="before-stop", runner=sdk)
    try:
        engine.dispatch(command(require_checkpoint=True))
        assert status_until(engine, ("waiting", "failed"))["status"] == "waiting"
        evidence = engine.dispatch(command("evidence"))
        assert "sdk_outcome" not in evidence
        assert not (Path(evidence["private_root"]) / "sdk-outcome-0.json").exists()
        receipt = evidence["checkpoint_snapshot"]
        raw = Path(receipt["path"]).read_bytes()
        assert receipt["bytes"] == len(raw) and receipt["sha256"] == hashlib.sha256(raw).hexdigest()
        snapshot = json.loads(raw)
        assert snapshot["schema"] == 1 and snapshot["kind"] == "m4_checkpoint_measurement"
        assert snapshot["evaluation_run_id"] == RUN and snapshot["run_id"] == JOB
        assert snapshot["binding"] == evidence["binding"] and snapshot["sdk_session_id"] == evidence["sdk_session_id"]
        assert snapshot["status"] == "waiting" and snapshot["checkpoint_wait_observed"] and not snapshot["released"]
        assert snapshot["process_marker"] == snapshot["context_read_process_marker"] == "before-stop"
        context_read = snapshot["context_reads"][0]
        context_bytes = context_read["text"].encode("utf-8")
        assert context_read["text"] == observed_context[0]
        assert context_read["bytes"] == len(context_bytes)
        assert context_read["sha256"] == hashlib.sha256(context_bytes).hexdigest()
        read_call = next(call for call in snapshot["tool_calls"] if call["id"] == context_read["tool_call_id"])
        assert context_read["duration_ms"] == read_call["duration_seconds"] * 1000
        assert context_read["duration_source"] == "hosted_tool_handler_monotonic"
        assert context_read["execution_status"] == "completed"
        assert snapshot["pending_tool_call_id"] == snapshot["tool_calls"][-1]["id"]
        assert "duration_seconds" not in snapshot["tool_calls"][-1]
        if with_usage:
            assert snapshot["usage"] == [usage]
            assert snapshot["usage_bindings"][0]["usage_index"] == 0
            assert snapshot["usage_bindings"][0]["process_marker"] == "before-stop"
            assert snapshot["usage_bindings"][0]["sdk_session_id"] == evidence["sdk_session_id"]
        else:
            assert "usage" not in snapshot and "usage_bindings" not in snapshot
        event_sinks[0]({"type": "assistant.usage", "data": {"model": "synthetic", "input_tokens": 122}})
        second = engine.dispatch(command("evidence"))["checkpoint_snapshot"]
        assert second["path"] != receipt["path"] and Path(receipt["path"]).read_bytes() == raw
        second_snapshot = json.loads(Path(second["path"]).read_bytes())
        assert second_snapshot["usage"][-1]["data"]["input_tokens"] == 122
        assert len(second_snapshot["usage"]) == (2 if with_usage else 1)
        Path(context_read["path"]).write_bytes(b"changed context evidence")
        with pytest.raises(Problem) as exc:
            engine.dispatch(command("evidence"))
        assert exc.value.body["error"]["code"] == "M4_CONTEXT_CHANGED"
        assert Path(receipt["path"]).read_bytes() == raw
    finally:
        stop.set()
        if JOB in engine.threads:
            engine.threads[JOB].join(timeout=5)
            assert not engine.threads[JOB].is_alive()


def test_m4_resume_requires_a_new_context_read_and_reuses_same_sdk_and_hosted_session(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)
    context, sessions = ContextBoundary(), []
    draft = {"step": "draft", "progress": "checkpoint draft", "artifact_text": "synthetic draft"}

    async def first_sdk(**options):
        sessions.append(options["sdk_session_id"])
        _, call = tools_for(options)
        await call("read_context")
        await call("fa_write_file", {"name": "draft.txt", "text": draft["artifact_text"]})
        await call("update_work", draft)
        await asyncio.wait_for(call("checkpoint_and_wait"), timeout=0.04)

    first = AssistantEngine(context, home=tmp_path, process_marker="before-stop", runner=first_sdk)
    first.dispatch(command(require_checkpoint=True))
    stopped = status_until(first)
    assert stopped["status"] == "failed" and stopped["checkpoint_wait_observed"]
    assert not stopped["task_verified"]
    with pytest.raises(Problem) as exc:
        first.dispatch(command("resume"))
    assert exc.value.body["error"]["code"] == "ASSISTANT_STOP_REQUIRED"

    async def resumed_sdk(**options):
        assert options["resume"] and options["sdk_session_id"] == sessions[0] and options["milestone"] == "m4"
        assert "Do not resend completed operations or orphaned tool calls" in options["prompt"]
        _, call = tools_for(options)
        replay = json.loads((await call("update_work", draft)).text)
        assert replay["replayed"] and context.mutations == 1
        await call("fa_write_file", {"name": "result.txt", "text": ANSWER})
        await call("update_work", {"step": "final", "progress": "final result", "artifact_text": ANSWER})
        with pytest.raises(ToolFailure, match="M4_CONTEXT_REQUIRED"):
            await call("finish_task")
        context.fail_context = True
        with pytest.raises(Problem):
            await call("read_context")
        with pytest.raises(ToolFailure, match="M4_CONTEXT_REQUIRED"):
            await call("finish_task")
        context.fail_context = False
        await call("read_context")
        await call("finish_task")
        return outcome(options)

    second = AssistantEngine(context, home=tmp_path, process_marker="after-stop", runner=resumed_sdk)
    second.dispatch(command("resume"))
    final = status_until(second)
    assert final["status"] == "completed", final
    assert final["binding"] == stopped["binding"] and final["sdk_session_id"] == stopped["sdk_session_id"]
    assert final["process_marker"] != stopped["process_marker"] and final["released"]
    assert context.mutations == 2 and context.record["progress"] == PROGRESS
    evidence = second.dispatch(command("evidence"))
    assert [item["process_marker"] for item in evidence["context_reads"]] == ["before-stop", "after-stop"]
    assert evidence["context_read_process_marker"] == "after-stop"
    assert evidence["resumes"][0]["continue_pending_work"] is False
    assert evidence["operations"]["update:draft"]["revision"] == 2
    assert evidence["operations"]["update:final"]["revision"] == 3
    assert sum(call["name"] == "checkpoint_and_wait" for call in evidence["tool_calls"]) == 1


@pytest.mark.parametrize("change", ["disabled", "run", "version", "session", "saved-run", "saved-work"])
def test_m4_resume_revalidates_configuration_and_persistent_binding(tmp_path, monkeypatch, change):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)
    context = ContextBoundary()

    async def idle_sdk(**options):
        return outcome(options)

    first = AssistantEngine(context, home=tmp_path, process_marker="first", runner=idle_sdk)
    first.dispatch(command())
    assert status_until(first)["status"] == "incomplete"
    if change == "disabled":
        monkeypatch.setenv("CONTEXT_M4_TOOLS_ENABLED", "false")
    elif change == "run":
        monkeypatch.setenv("CONTEXT_M4_RUN_ID", "m4-context-02")
    elif change in ("version", "session"):
        monkeypatch.setenv("FOUNDRY_AGENT_" + ("VERSION" if change == "version" else "SESSION_ID"), "changed")
    else:
        root = tmp_path / ".fa-m1" / "m2" / AGENT / JOB
        state = first.dispatch(command("evidence"))
        state.pop("files")
        state["evaluation_run_id" if change == "saved-run" else "work"] = "m4-context-02"
        first._save(root, state)
    second = AssistantEngine(context, home=tmp_path, process_marker="second", runner=idle_sdk)
    with pytest.raises(Problem) as exc:
        second.dispatch(command("resume"))
    assert exc.value.body["error"]["code"] == (
        "M4_DISABLED" if change in ("disabled", "run") else "ASSISTANT_RESTORE_REQUIRED"
    )
    assert context.calls == []


def test_m4_unknown_update_is_not_replayed_after_resume(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)
    context = ContextBoundary()
    context.lose_update = True
    update = {"step": "final", "progress": "must remain private", "artifact_text": ANSWER}

    async def first_sdk(**options):
        _, call = tools_for(options)
        await call("update_work", update)

    first = AssistantEngine(context, home=tmp_path, process_marker="first", runner=first_sdk)
    first.dispatch(command())
    assert status_until(first)["status"] == "failed"

    async def second_sdk(**options):
        _, call = tools_for(options)
        with pytest.raises(ToolFailure, match="ASSISTANT_EFFECT_UNKNOWN"):
            await call("update_work", update)
        return outcome(options)

    second = AssistantEngine(context, home=tmp_path, process_marker="second", runner=second_sdk)
    second.dispatch(command("resume"))
    assert status_until(second)["status"] == "incomplete"
    assert context.mutations == 1 and context.record["progress"] == PROGRESS
    assert second.dispatch(command("evidence"))["operations"]["update:final"]["status"] == "pending_or_unknown"


def test_m4_same_process_resume_still_requires_a_new_successful_context_read(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)
    context = ContextBoundary()

    async def first_sdk(**options):
        _, call = tools_for(options)
        await call("read_context")
        return outcome(options)

    engine = AssistantEngine(context, home=tmp_path, process_marker="same-process", runner=first_sdk)
    engine.dispatch(command())
    assert status_until(engine)["status"] == "incomplete"
    engine.threads[JOB].join(timeout=5)

    async def resumed_sdk(**options):
        _, call = tools_for(options)
        with pytest.raises(ToolFailure, match="M4_CONTEXT_REQUIRED"):
            await call("finish_task")
        await call("read_context")
        await call("fa_write_file", {"name": "result.txt", "text": ANSWER})
        await call("update_work", {"step": "final", "progress": "final result", "artifact_text": ANSWER})
        await call("finish_task")
        return outcome(options)

    engine.runner = resumed_sdk
    engine.dispatch(command("resume"))
    assert status_until(engine)["status"] == "completed"
    assert len(engine.dispatch(command("evidence"))["context_reads"]) == 2


def test_m4_cannot_complete_if_runner_changes_sdk_conversation(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)

    async def sdk(**options):
        _, call = tools_for(options)
        await call("read_context")
        await call("fa_write_file", {"name": "result.txt", "text": ANSWER})
        await call("update_work", {"step": "final", "progress": "result saved", "artifact_text": ANSWER})
        await call("finish_task")
        return {**outcome(options), "sdk_session_id": "changed-conversation"}

    engine = AssistantEngine(ContextBoundary(), home=tmp_path, process_marker="one", runner=sdk)
    engine.dispatch(command())
    final = status_until(engine)
    assert final["status"] == "failed"
    assert final["error"]["details"]["error"]["code"] == "ASSISTANT_RESTORE_REQUIRED"


def test_oversized_context_never_satisfies_m4_read_requirement(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)
    enabled(monkeypatch)
    context = ContextBoundary()
    context.extra_context = "大" * 11000

    async def sdk(**options):
        _, call = tools_for(options)
        with pytest.raises(Problem) as exc:
            await call("read_context")
        assert exc.value.body["error"]["code"] == "M4_CONTEXT_TOO_LARGE"
        with pytest.raises(ToolFailure, match="M4_CONTEXT_REQUIRED"):
            await call("finish_task")
        return outcome(options)

    engine = AssistantEngine(context, home=tmp_path, process_marker="one", runner=sdk)
    engine.dispatch(command())
    assert status_until(engine)["status"] == "incomplete"
    evidence = engine.dispatch(command("evidence"))
    assert not evidence.get("context_read_process_marker")
    assert evidence["context_reads"][0]["bytes"] > 32768
    assert "usage" not in evidence and "usage_bindings" not in evidence


@pytest.fixture
def probe_client():
    calls = []

    def send(request):
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(403 if "/private/agent-b/" in request.url.path else 200,
                              json={"synthetic_boundary": True})

    http = httpx.Client(transport=httpx.MockTransport(send))
    client = HostedClient(
        ContextBoundary.api_url, ContextBoundary.api_audience, "https://example.table.core.windows.net", "workstate",
        "https://example.services.ai.azure.com/api/projects/poc", "synthetic-memory",
        SimpleNamespace(get_token=lambda _scope: SimpleNamespace(token="synthetic-token")), http,
    )
    yield client, calls
    http.close()


@pytest.mark.parametrize("method,path,body", [
    ("GET", f"/m4/teams/team-one/works/{WORK}/context", None),
    ("POST", "/m4/teams/team-one/memory/seed", {"fixture_id": "short_team", "suite_sha256": FIXTURE_HASH}),
    ("GET", "/m4/teams/team-one/memory/operations/" + "a" * 32, None),
    ("POST", "/m4/teams/team-one/memory/private/agent-a/search", {"query": "Project Lyra"}),
    ("POST", "/m4/teams/team-one/memory/private/agent-b/search", {"query": "Project Lyra"}),
])
def test_m4_probe_exact_paths_require_flag_and_bound_run(probe_client, monkeypatch, method, path, body):
    client, calls = probe_client
    payload = {"operation": "api", "method": method, "path": path, "body": body}
    monkeypatch.delenv("CONTEXT_M4_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("CONTEXT_M4_RUN_ID", raising=False)
    for flag, run in ((None, None), ("true", "other-run"), ("false", RUN)):
        if flag is not None:
            monkeypatch.setenv("CONTEXT_M4_TOOLS_ENABLED", flag)
            monkeypatch.setenv("CONTEXT_M4_RUN_ID", run)
        with pytest.raises(Problem):
            client.dispatch(payload)
    assert calls == []
    enabled(monkeypatch)
    result = client.dispatch(payload)["outcome"]
    assert calls == [(method, path, body)]
    assert result["http_status"] == (403 if "/agent-b/" in path else 200)


@pytest.mark.parametrize("path", [
    "/m4/teams/team-one/works/one", "/m4/teams/team-one/works/one/publish",
    "/m4/teams/team-one/artifacts/report", "/m4/teams/team-one/memory/team",
    "/m4/teams/team-one/memory/team/search", "/m4/teams/team-one/memory/private/agent-a",
    "/m4/teams/team-one/memory/seed/anything", "/m4/teams/team-one/memory/operations/" + "G" * 32,
    "/m4/teams/team-one/memory/operations/" + "a" * 31,
    "/m4/teams/team-one/works/one/context?extra=true", "/m4/teams/team-one/works/../context",
    "/m4/model/v1/budget", "/m4/model/v1/chat/completions",
])
def test_m4_probe_does_not_expand_legacy_allowlists(probe_client, monkeypatch, path):
    enabled(monkeypatch)
    client, calls = probe_client
    with pytest.raises(Problem):
        client.dispatch({"operation": "api", "method": "GET", "path": path})
    assert calls == []


@pytest.mark.parametrize("body", [
    None, {}, {"fixture_id": "arbitrary", "suite_sha256": FIXTURE_HASH},
    {"fixture_id": "private_a", "suite_sha256": "invalid"},
    {"fixture_id": "short_team", "suite_sha256": FIXTURE_HASH, "items": ["unrestricted upload"]},
    {"fixture_id": "private_b", "suite_sha256": FIXTURE_HASH, "actor": "agent-a"},
])
def test_m4_seed_body_is_strict(probe_client, monkeypatch, body):
    enabled(monkeypatch)
    client, calls = probe_client
    with pytest.raises(Problem):
        client.dispatch({"operation": "api", "method": "POST", "path": "/m4/teams/team-one/memory/seed", "body": body})
    assert calls == []


@pytest.mark.parametrize("fixture", ["short_team", "long_team", "private_a", "private_b"])
def test_m4_probe_transports_only_named_fixture_seeds(probe_client, monkeypatch, fixture):
    enabled(monkeypatch)
    client, calls = probe_client
    body = {"fixture_id": fixture, "suite_sha256": FIXTURE_HASH}
    client.dispatch({"operation": "api", "method": "POST", "path": "/m4/teams/team-one/memory/seed", "body": body})
    assert calls == [("POST", "/m4/teams/team-one/memory/seed", body)]


@pytest.mark.parametrize("method,path,extra", [
    ("POST", f"/m4/teams/team-one/works/{WORK}/context", {}),
    ("PATCH", "/m4/teams/team-one/memory/seed", {}),
    ("GET", "/m4/teams/team-one/memory/private/agent-a/search", {}),
    ("GET", f"/m4/teams/team-one/works/{WORK}/context", {"body": {}}),
    ("GET", f"/m4/teams/team-one/works/{WORK}/context", {"etag": '"one"'}),
])
def test_m4_probe_rejects_wrong_method_body_or_etag(probe_client, monkeypatch, method, path, extra):
    enabled(monkeypatch)
    client, calls = probe_client
    with pytest.raises(Problem):
        client.dispatch({"operation": "api", "method": method, "path": path, **extra})
    assert calls == []


def test_model_budget_and_legacy_probes_keep_explicit_milestone_gates(probe_client, monkeypatch):
    client, calls = probe_client
    monkeypatch.delenv("CONTEXT_M4_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("CONTEXT_M3_TOOLS_ENABLED", raising=False)
    client.dispatch({"operation": "model_budget"})
    with pytest.raises(Problem):
        client.dispatch({"operation": "model_budget", "milestone": "m4"})
    enabled(monkeypatch)
    client.dispatch({"operation": "model_budget", "milestone": "m4"})
    with pytest.raises(Problem):
        client.dispatch({"operation": "model_budget", "milestone": "m3"})
    monkeypatch.setenv("CONTEXT_M3_TOOLS_ENABLED", "true")
    client.dispatch({"operation": "model_budget", "milestone": "m3"})
    with pytest.raises(Problem):
        client.dispatch({"operation": "model_budget", "milestone": "m5"})
    assert [path for _, path, _ in calls] == [
        "/model/v1/budget", "/m4/model/v1/budget", "/m3/model/v1/budget",
    ]
    client.dispatch({"operation": "api", "method": "GET", "path": "/teams/team-one/works/legacy/context"})
    client.dispatch({"operation": "api", "method": "GET", "path": "/teams/team-one/artifacts/legacy"})
    assert calls[-2][1] == "/teams/team-one/works/legacy/context"
    assert calls[-1][1] == "/teams/team-one/artifacts/legacy"
