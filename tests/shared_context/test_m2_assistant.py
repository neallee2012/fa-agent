from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from shared_context.errors import Problem
from shared_context.m2_assistant import AssistantEngine, DesktopAction
from shared_context.m2_copilot import ToolFailure

AGENT = "00000000-0000-0000-0000-00000000005d"


@pytest.mark.parametrize("arguments,accepted", [
    ({"type": "click", "x": 124, "y": 199}, True),
    ({"type": "type", "text": "amber sample"}, True),
    ({"type": "key", "key": "Tab"}, True),
    ({"type": "type", "x": 75, "y": 182, "text": "amber sample"}, False),
    ({"type": "click", "x": 1, "y": 1, "text": None}, False),
    ({"type": "click", "x": 1}, False),
    ({"type": "click", "x": 1280, "y": 1}, False),
    ({"type": "key", "key": "Tab", "x": 1}, False),
    ({"type": "key", "key": "Enter"}, False),
    ({"type": "type", "text": ""}, False),
])
def test_desktop_public_schema_and_handler_accept_the_same_action(arguments, accepted):
    schema = DesktopAction.model_json_schema()
    assert schema["type"] == "object"
    assert Draft202012Validator(schema).is_valid(arguments) is accepted
    if accepted:
        assert DesktopAction.model_validate(arguments).model_dump(exclude_none=True) == arguments
    else:
        with pytest.raises(ValidationError):
            DesktopAction.model_validate(arguments)


class ContextBoundary:
    api_url = "https://context.example"
    api_audience = "api://11111111-1111-1111-1111-111111111111/poc"

    def __init__(self):
        self.record = {"owner": "agent-a", "status": "draft", "revision": 1,
                       "etag": '"one"', "progress": "", "artifact": {"text": "", "sha256": ""}}
        self.mutations = 0
        self.credential = None

    def request(self, method, url, scope, body=None, headers=None):
        if method == "POST":
            assert headers["If-Match"] == self.record["etag"]
            self.mutations += 1
            revision = self.record["revision"] + 1
            self.record = {**self.record, "revision": revision, "etag": f'"v{revision}"'}
            if url.endswith("/handoff"):
                self.record["owner"] = body["to_actor"]
            elif url.endswith("/publish"):
                self.record["status"] = "published"
            else:
                pytest.fail("unexpected write route")
        if method == "PATCH":
            assert headers["If-Match"] == self.record["etag"]
            self.mutations += 1
            revision = self.record["revision"] + 1
            self.record = {**self.record, "revision": revision, "etag": f'"v{revision}"',
                           "progress": body["progress"],
                           "artifact": {"text": body["artifact_text"],
                                        "sha256": hashlib.sha256(body["artifact_text"].encode()).hexdigest()}}
        content = {"work": self.record} if url.endswith("/context") else self.record
        return {"http_status": 200, "content": content, "etag": self.record["etag"], "truncated": False}


def hosted_runtime(monkeypatch, home):
    monkeypatch.setenv("FOUNDRY_AGENT_ID", AGENT)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "fa-context-a")
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", "10")
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "synthetic-native-session")
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("os.geteuid", lambda: 10001, raising=False)
    (home / ".fa-m1").mkdir(mode=0o700)


def command(action, **fields):
    return {"operation": "assistant", "action": action, "target_agent_id": AGENT,
            "run_id": "m2-unit", **fields}


def status_until(engine, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = engine.dispatch(command("status"))
        if status["status"] in expected:
            return status
        time.sleep(0.01)
    pytest.fail("assistant did not reach the expected observable state")


def test_assistant_saves_model_selected_result_without_repeating_an_accepted_update(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)
    context = ContextBoundary()

    async def sdk_boundary(**options):
        tools = {tool.name: tool for tool in options["tools"]}

        async def call(name, arguments):
            return await tools[name].handler({
                "session_id": options["sdk_session_id"], "tool_call_id": uuid4().hex,
                "tool_name": name, "arguments": arguments,
            })

        await call("read_context", {})
        await call("fa_write_file", {"name": "result.txt", "text": "Synthetic total: 40"})
        args = {"step": "final", "progress": "finished", "artifact_text": "Synthetic total: 40"}
        await call("update_work", args)
        await call("update_work", args)
        await call("finish_task", {})
        return {"sdk_session_id": options["sdk_session_id"], "final_message": "Done", "history": []}

    engine = AssistantEngine(context, home=tmp_path, process_marker="process-one", runner=sdk_boundary)
    engine.dispatch(command("start", team="poc-team", work="work-one",
                            prompt="Calculate the synthetic total.", input_text="12 + 20 + 8"))
    final = status_until(engine, {"completed", "failed"})
    assert final["status"] == "completed", final
    assert context.mutations == 1
    evidence = engine.dispatch(command("evidence"))
    assert evidence["files"]["result.txt"]["text"] == "Synthetic total: 40"
    assert evidence["binding"]["agent_id"] == AGENT
    assert evidence["sdk_session_id"] == final["sdk_session_id"]


def test_new_process_resumes_unfinished_work_with_the_same_sdk_session_and_existing_receipts(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)
    context, sessions = ContextBoundary(), []

    async def first_runtime(**options):
        sessions.append(options["sdk_session_id"])
        tools = {tool.name: tool for tool in options["tools"]}

        async def call(name, args):
            return await tools[name].handler({"session_id": sessions[0], "tool_call_id": uuid4().hex,
                                               "tool_name": name, "arguments": args})

        await call("fa_write_file", {"name": "draft.txt", "text": "Draft total 40"})
        await call("update_work", {"step": "draft", "progress": "draft saved", "artifact_text": "Draft total 40"})
        # The SDK boundary interrupts a still-pending tool, rather than a completed task.
        await asyncio.wait_for(call("checkpoint_and_wait", {}), timeout=0.05)

    first = AssistantEngine(context, home=tmp_path, process_marker="process-one", runner=first_runtime)
    first.dispatch(command("start", team="poc-team", work="work-one", require_checkpoint=True,
                           prompt="Save a draft, checkpoint and wait, then finish the result.", input_text="12 + 20 + 8"))
    stopped = status_until(first, {"failed"})
    assert stopped["checkpoint_wait_observed"] is True and stopped["task_verified"] is False

    async def second_runtime(**options):
        assert options["resume"] is True and options["sdk_session_id"] == sessions[0]
        tools = {tool.name: tool for tool in options["tools"]}
        for name, args in [
            ("fa_write_file", {"name": "result.txt", "text": "Final total 40"}),
            ("update_work", {"step": "final", "progress": "finished", "artifact_text": "Final total 40"}),
            ("finish_task", {}),
        ]:
            await tools[name].handler({"session_id": sessions[0], "tool_call_id": uuid4().hex,
                                      "tool_name": name, "arguments": args})
        return {"sdk_session_id": sessions[0], "history": [], "final_message": "Finished remaining work"}

    second = AssistantEngine(context, home=tmp_path, process_marker="process-two", runner=second_runtime)
    second.dispatch(command("resume"))
    final = status_until(second, {"completed", "failed"})
    assert final["status"] == "completed", final
    assert context.mutations == 2 and final["sdk_session_id"] == stopped["sdk_session_id"]
    evidence = second.dispatch(command("evidence"))
    assert evidence["operations"]["update:draft"]["revision"] == 2
    assert evidence["operations"]["update:final"]["revision"] == 3
    assert evidence["files"]["draft.txt"]["text"] == "Draft total 40"


def test_accepted_but_lost_context_response_is_not_resent_after_restart(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)

    class LostResponse(ContextBoundary):
        def request(self, method, *args, **kwargs):
            response = super().request(method, *args, **kwargs)
            if method == "PATCH":
                raise ConnectionResetError("synthetic accepted request, lost response")
            return response

    context = LostResponse()
    update = {"step": "final", "progress": "finished", "artifact_text": "Synthetic total: 40"}

    async def sdk_boundary(**options):
        tool = next(tool for tool in options["tools"] if tool.name == "update_work")
        await tool.handler({"session_id": options["sdk_session_id"], "tool_call_id": uuid4().hex,
                            "tool_name": "update_work", "arguments": update})

    first = AssistantEngine(context, home=tmp_path, process_marker="process-one", runner=sdk_boundary)
    first.dispatch(command("start", team="poc-team", work="work-one",
                           prompt="Save the total.", input_text="12 + 20 + 8"))
    status_until(first, {"failed"})
    second = AssistantEngine(context, home=tmp_path, process_marker="process-two", runner=sdk_boundary)
    second.dispatch(command("resume"))
    final = status_until(second, {"failed"})
    assert context.mutations == 1
    assert "reconcile" in final["error"]["message"]
    evidence = second.dispatch(command("evidence"))
    assert evidence["operations"]["update:final"]["status"] == "pending_or_unknown"


def test_handoff_and_publish_are_explicit_capabilities_and_completion_checks_the_authoritative_state(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)
    context = ContextBoundary()

    def runtime_for(actions):
        async def sdk_boundary(**options):
            tools = {tool.name: tool for tool in options["tools"]}
            for name, args in actions:
                await tools[name].handler({"session_id": options["sdk_session_id"], "tool_call_id": uuid4().hex,
                                          "tool_name": name, "arguments": args})
            return {"sdk_session_id": options["sdk_session_id"], "history": [], "final_message": "Done"}
        return sdk_boundary

    a = AssistantEngine(context, home=tmp_path, process_marker="process-a", runner=runtime_for([
        ("fa_write_file", {"name": "result.txt", "text": "A total 40"}),
        ("update_work", {"step": "final", "progress": "ready for review", "artifact_text": "A total 40"}),
        ("handoff", {}),
        ("finish_task", {}),
    ]))
    a.dispatch(command("start", team="poc-team", work="work-one", handoff_to="agent-b",
                       prompt="Summarize and hand off to B.", input_text="12 + 20 + 8"))
    assert status_until(a, {"completed", "failed"})["status"] == "completed"
    assert context.record["owner"] == "agent-b" and context.mutations == 2
    b_home = tmp_path / "b"
    b_home.mkdir()
    hosted_runtime(monkeypatch, b_home)
    b_id = "00000000-0000-0000-0000-00000000005e"
    monkeypatch.setenv("FOUNDRY_AGENT_ID", b_id)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "fa-context-b")
    b = AssistantEngine(context, home=b_home, process_marker="process-b", runner=runtime_for([
        ("read_context", {}),
        ("fa_write_file", {"name": "result.txt", "text": "B verified total 40"}),
        ("update_work", {"step": "review", "progress": "review complete", "artifact_text": "B verified total 40"}),
        ("publish", {}),
        ("finish_task", {}),
    ]))
    with pytest.raises(Problem) as denied:
        b.dispatch(command("evidence"))
    assert denied.value.status == 403
    b.dispatch(command("start", target_agent_id=b_id, team="poc-team", work="work-one", allow_publish=True,
                       prompt="Review the shared work and publish.", input_text="Use shared context."))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        final = b.dispatch(command("status", target_agent_id=b_id))
        if final["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)
    assert final["status"] == "completed", final
    assert context.record["status"] == "published" and context.mutations == 4


@pytest.mark.parametrize("actual_label,expected_status", [("amber sample", "completed"), ("wrong field", "failed")])
def test_desktop_steps_deliver_pixels_and_finish_checks_actual_fields_and_cleanup(
    tmp_path, monkeypatch, actual_label, expected_status,
):
    hosted_runtime(monkeypatch, tmp_path)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2qWQAAAAASUVORK5CYII="
    )
    instances = []

    class DesktopBoundary:
        def __init__(self, root, run_id, variant):
            self.closed, self.actions, self.observations = False, [], 0
            self.fixture_url = "http://127.0.0.1:12345/synthetic/"
            self.upload_path = root / "synthetic-upload.txt"
            self.task_description = "Fill the local synthetic form and download the receipt."
            instances.append(self)

        def start(self):
            return self

        def observe(self):
            self.observations += 1
            return {"png_base64": base64.b64encode(png).decode(), "width": 1, "height": 1,
                    "sha256": hashlib.sha256(png).hexdigest(), "size": len(png),
                    "observation_id": self.observations, "mime_type": "image/png",
                    "windows": [], "active_window": None}

        def act(self, action):
            self.actions.append(action)
            return {"accepted": True}

        def close(self):
            self.closed = True

        def result(self):
            return {
                "completed": self.closed, "cleanup": {"success": self.closed},
                "submission": {"accepted": True, "fields": {"label": actual_label, "count": 7},
                               "native_file_dialog_seen": True,
                               "upload": {"synthetic_match": True,
                                          "sha256": "7b713892dd26a61b1303007e3b092559c49ca5c37e3d87932c87195e1aa77c4b"}},
                "native_file_dialog_seen": True,
                "download": {"verified": True, "sha256": "abc", "requests": [{"sha256": "abc"}]},
            }

    async def sdk_boundary(**options):
        assert "amber sample" in options["prompt"]
        tools = {tool.name: tool for tool in options["tools"]}
        for name, args in [
            ("desktop_observe", {}),
            ("desktop_act", {"type": "click", "x": 33, "y": 44}),
            ("finish_task", {}),
        ]:
            response = await tools[name].handler({
                "session_id": options["sdk_session_id"], "tool_call_id": uuid4().hex,
                "tool_name": name, "arguments": args,
            })
            if name != "finish_task":
                assert response.png_base64 == base64.b64encode(png).decode()
                assert "png_base64" not in response.text
            if name == "desktop_observe":
                try:
                    await tools["desktop_act"].handler({
                        "session_id": options["sdk_session_id"], "tool_call_id": uuid4().hex,
                        "tool_name": "desktop_act", "arguments": {"type": "type", "text": "htt"},
                    })
                except ToolFailure as denied:
                    assert "ASSISTANT_DESKTOP_INPUT_DENIED" in str(denied)
                    assert "htt" not in str(denied)
                else:
                    raise AssertionError("unapproved text fragments reached the desktop")
        return {"sdk_session_id": options["sdk_session_id"], "history": [], "final_message": "Done"}

    engine = AssistantEngine(ContextBoundary(), home=tmp_path, process_marker="process-ui",
                             runner=sdk_boundary, desktop_factory=DesktopBoundary)
    engine.dispatch(command("start", team="poc-team", work="work-one", prompt="Complete the local form.",
                            input_text="synthetic", desktop_variant=1, desktop_label="amber sample", desktop_count=7))
    final = status_until(engine, {"completed", "failed"})
    assert final["status"] == expected_status, final
    assert instances[0].closed is True
    assert instances[0].actions == [{"type": "click", "x": 33, "y": 44}]
    evidence = engine.dispatch(command("evidence"))
    assert len(evidence["screenshots"]) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing transport boundary")
def test_transient_windows_checkpoint_lock_does_not_lose_the_initial_task(tmp_path, monkeypatch):
    hosted_runtime(monkeypatch, tmp_path)
    replace, failures = os.replace, []

    def sharing_violation_once(source, destination):
        if Path(destination).name == "state.json" and not failures:
            failures.append(True)
            error = PermissionError("synthetic sharing violation")
            error.winerror = 32
            raise error
        return replace(source, destination)

    async def sdk_boundary(**options):
        return {"sdk_session_id": options["sdk_session_id"], "history": [], "final_message": "No completed task claimed"}

    monkeypatch.setattr(os, "replace", sharing_violation_once)
    engine = AssistantEngine(ContextBoundary(), home=tmp_path, process_marker="process-one", runner=sdk_boundary)
    engine.dispatch(command("start", team="poc-team", work="work-one", prompt="Read synthetic input.", input_text="retained"))
    final = status_until(engine, {"incomplete", "failed"})
    assert final["status"] == "incomplete"
    assert engine.dispatch(command("evidence"))["files"]["input.txt"]["text"] == "retained"
    assert failures == [True]
