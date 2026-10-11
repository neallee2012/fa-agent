"""Full Hosted-to-m2 tool adapter, pinned SDK types; no runtime/model/network."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import httpx
import pytest
from copilot import CopilotClient
from copilot.client import GetStatusResponse
from copilot.session import CopilotSession
from copilot.tools import ToolInvocation

from shared_context import m2_copilot
from shared_context.behavior_assistant_hosted import AssistantSession
from shared_context.behavior_assistant_wire import ContextResult, ToolReceipt
from shared_context.behavior_wire import ExactSourceRef
from shared_context.errors import Problem


@pytest.mark.parametrize("fault", ["none", "finish-error"])
def test_hosted_normal_tool_sequence_requires_finish_and_owned_sdk_stop(tmp_path, monkeypatch, fault):
    import subprocess
    def denied(*args, **kwargs):
        pytest.fail("no real runtime, model, token or network")
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", denied)
    monkeypatch.setattr(httpx.AsyncClient, "send", denied)
    runtime_path = tmp_path / "OFFLINE-not-executable"
    runtime_path.write_bytes(b"OFFLINE path validation only")
    work = tmp_path / "work"
    work.mkdir()
    methods, stopped, result_saved, finished = [], [], [], []
    sid = "offline-normal-0922"
    def context():
        methods.append("read_context")
        return ContextResult(source=ExactSourceRef(source_id="offline", version=1, sha256="a" * 64,
                                                   byte_length=1), item_count=12, completed_count=5)
    def save(answer):
        methods.append("save_result")
        assert answer.remaining_count == 7
        result_saved.append(True)
        return ToolReceipt(state="saved", artifact_sha256="b" * 64)
    def finish():
        methods.append("finish_task")
        assert result_saved
        if fault == "finish-error":
            raise Problem(409, "ASSISTANT_EFFECT_UNKNOWN", "OFFLINE private error")
        finished.append(True)
        return ToolReceipt(state="ready_to_finalize", artifact_sha256="b" * 64, result_digest="c" * 64)
    caller = SimpleNamespace(
        assignment=SimpleNamespace(sdk_session_id=sid, resume=False, session_id="d" * 64,
                                   task=SimpleNamespace(instruction=None)),
        api_url="https://offline.invalid", token_provider=denied, reserve_model=denied,
        context=context, save=save, finish=finish, operation=denied,
        status=lambda: ToolReceipt(state="ready_to_finalize", result_digest="c" * 64),
        stopped=lambda reason: stopped.append(reason),
    )
    sdk_session = create_autospec(CopilotSession, instance=True)
    sdk_session.session_id = sid
    sdk_session.get_events.return_value = []
    sdk = create_autospec(CopilotClient, instance=True)
    sdk._cli_process = SimpleNamespace(pid=999, poll=lambda: 0, returncode=0)
    sdk.get_status.return_value = GetStatusResponse(version="1.0.83", protocol_version=2)
    async def create(**options):
        tools = {tool.name: tool for tool in options["tools"]}
        assert tools["finish_task"].is_terminal
        async def send(prompt, **kwargs):
            assert kwargs["timeout"] == 240
            for index, (name, args) in enumerate((
                ("read_context", {}),
                ("save_result", {"item_count": 12, "completed_count": 5, "remaining_count": 7}),
                ("finish_task", {}),
            )):
                value = await tools[name].handler(ToolInvocation(
                    session_id=sid, tool_call_id=f"offline-{index}", tool_name=name, arguments=args))
                if name == "finish_task" and fault == "finish-error":
                    assert value.result_type == "failure" and "OFFLINE private error" not in value.error
                    raise RuntimeError("OFFLINE unresolved SDK turn")
                assert value.result_type == "success"
            return None
        sdk_session.send_and_wait.side_effect = send
        return sdk_session
    sdk.create_session.side_effect = create
    monkeypatch.setattr(m2_copilot, "CopilotClient", create_autospec(CopilotClient, return_value=sdk))
    session = AssistantSession(caller, root=tmp_path, runtime_path=runtime_path,
                               model_transport=httpx.MockTransport(denied))
    if fault == "none":
        outcome = asyncio.run(session._run(work))
        assert outcome["state"] == "ready_to_finalize" and outcome["result_digest"] == "c" * 64
        assert outcome["sdk_stop"]["graceful"] and finished == [True] and not stopped
    else:
        with pytest.raises(Problem) as unknown:
            asyncio.run(session._run(work))
        assert unknown.value.body["error"]["code"] == "ASSISTANT_SDK_UNKNOWN"
        assert not finished and stopped == ["sdk_failed"]
    assert methods == ["read_context", "save_result", "finish_task"]
    sdk.stop.assert_awaited_once()
    sdk.force_stop.assert_not_awaited()
    sdk.resume_session.assert_not_awaited()
