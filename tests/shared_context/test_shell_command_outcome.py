"""The real shell tool must distinguish an unconfirmed result from an unstarted command."""

import asyncio
from types import SimpleNamespace

import pytest

from shared_context import behavior_shell_capability as shell
from shared_context.behavior_capabilities import available_grants
from shared_context.m2_copilot import ToolFailure


@pytest.mark.parametrize("still_running", [False, True], ids=["exited", "still-running"])
def test_started_command_with_a_failed_output_reader_never_returns_a_complete_result(
    tmp_path, monkeypatch, still_running, caplog,
):
    started, closed, signals = [], [], []
    process = SimpleNamespace(pid=4242, returncode=None)
    reads = []

    async def read(_limit):
        reads.append(True)
        if len(reads) == 1:
            return b"Original command already produced output.\n"
        if not still_running:
            process.returncode = 0
        raise OSError("PRIVATE_PIPE_ERROR_DETAIL")

    async def create(*args, **kwargs):
        started.append((args, kwargs))
        return process

    process.stdout = SimpleNamespace(read=read)
    process._transport = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(shell, "prerequisites", lambda: "/bin/bash")
    monkeypatch.setattr(shell, "shell_directories", lambda _: (tmp_path, tmp_path, tmp_path))
    def signal_group(pid, sig):
        signals.append((pid, sig))
        if sig == shell.signal.SIGTERM:
            process.returncode = -15

    monkeypatch.setattr(shell, "_signal_group", signal_group)
    monkeypatch.setattr(shell.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(shell, "POLL_SECONDS", 0)
    grant = available_grants(None, ("fa-shell-v1",))["fa-shell-v1"]
    specs, _ = shell.shell_capability(SimpleNamespace(root=tmp_path), grant)
    packet = {"arguments": {"command": "synthetic-original-command"}, "tool_call_id": "original-long-tool"}

    async def invoke():
        return await asyncio.wait_for(specs[0].handler(packet), timeout=1)

    with pytest.raises(ToolFailure) as failed:
        asyncio.run(invoke())
    assert failed.value.public_code == "CAPABILITY_UNAVAILABLE"
    assert "nothing ran" not in str(failed.value)
    assert "PRIVATE_PIPE_ERROR_DETAIL" not in str(failed.value)
    assert "PRIVATE_PIPE_ERROR_DETAIL" not in caplog.text and "Task exception was never retrieved" not in caplog.text
    assert len(started) == 1 and len(reads) == 2
    expected = [shell.signal.SIGTERM, shell.signal.SIGKILL] if still_running else [shell.signal.SIGKILL]
    assert closed == [True] and signals == [(process.pid, sig) for sig in expected]


@pytest.mark.parametrize("pipe_closes", [True, False], ids=["drained", "unconfirmed"])
def test_exited_command_drains_output_after_stopping_background_pipe_holders(
    tmp_path, monkeypatch, pipe_closes,
):
    stopped = asyncio.Event()
    started, closed, signals, reads = [], [], [], []
    process = SimpleNamespace(pid=4343, returncode=0)

    async def read(_limit):
        reads.append(True)
        if len(reads) == 1:
            return b"head\n"
        if len(reads) == 2:
            await stopped.wait()
            return b"tail\n"
        return b""

    async def create(*args, **kwargs):
        started.append((args, kwargs))
        return process

    def signal_group(pid, sig):
        signals.append((pid, sig))
        if pipe_closes:
            stopped.set()

    process.stdout = SimpleNamespace(read=read)
    process._transport = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(shell, "prerequisites", lambda: "/bin/bash")
    monkeypatch.setattr(shell, "shell_directories", lambda _: (tmp_path, tmp_path, tmp_path))
    monkeypatch.setattr(shell, "_signal_group", signal_group)
    monkeypatch.setattr(shell.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(shell, "DRAIN_SECONDS", 0.01)
    spec = shell.shell_capability(SimpleNamespace(root=tmp_path),
        available_grants(None, ("fa-shell-v1",))["fa-shell-v1"])[0][0]
    packet = {"arguments": {"command": "synthetic-with-pipe-holder"}, "tool_call_id": "original-long-tool"}
    if pipe_closes:
        result = shell.CommandResult.model_validate_json(asyncio.run(spec.handler(packet)).text)
        assert result.output == "head\ntail\n" and result.output_bytes == 10
        assert result.exit_code == 0 and result.truncated is False
    else:
        with pytest.raises(ToolFailure) as unknown:
            asyncio.run(spec.handler(packet))
        assert unknown.value.public_code == "CAPABILITY_UNAVAILABLE"
        assert "nothing ran" not in str(unknown.value)
    assert len(started) == 1 and closed == [True]
    assert signals and all(pid == process.pid and sig == shell.signal.SIGKILL for pid, sig in signals)


@pytest.mark.parametrize("cancelled", [False, True], ids=["normal-exit", "cancelled"])
def test_output_eof_does_not_end_a_running_command_and_its_heartbeat_stops_with_it(
    tmp_path, monkeypatch, cancelled,
):
    process = SimpleNamespace(pid=4444, returncode=None)
    started, closed, signals, beats, reads = [], [], [], [], []
    active = asyncio.Event()

    async def read(_limit):
        reads.append(True)
        return b"owned\n" if len(reads) == 1 else b""

    async def create(*args, **kwargs):
        started.append((args, kwargs))
        return process

    async def beat():
        beats.append(True)
        active.set()

    def signal_group(pid, sig):
        signals.append((pid, sig))
        if sig == shell.signal.SIGTERM:
            process.returncode = -15

    process.stdout = SimpleNamespace(read=read)
    process._transport = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(shell, "prerequisites", lambda: "/bin/bash")
    monkeypatch.setattr(shell, "shell_directories", lambda _: (tmp_path, tmp_path, tmp_path))
    monkeypatch.setattr(shell, "_signal_group", signal_group)
    monkeypatch.setattr(shell.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(shell, "POLL_SECONDS", 0)
    monkeypatch.setattr(shell, "KEEPALIVE_SECONDS", 0.001)
    spec = shell.shell_capability(SimpleNamespace(root=tmp_path, keep_active=beat),
        available_grants(None, ("fa-shell-v1",))["fa-shell-v1"])[0][0]

    async def scenario():
        task = asyncio.create_task(spec.handler({
            "arguments": {"command": "synthetic-running-after-eof"}, "tool_call_id": "original-long-tool",
        }))
        try:
            await asyncio.wait_for(active.wait(), timeout=1)
            assert len(reads) == 2 and not task.done()
            if cancelled:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                process.returncode = 3
                result = shell.CommandResult.model_validate_json((await task).text)
                assert result.exit_code == 3 and result.output == "owned\n"
                assert result.output_bytes == 6 and result.truncated is False
            count = len(beats)
            await asyncio.sleep(0.01)
            assert len(beats) == count
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(started) == 1 and closed == [True]
    expected = [shell.signal.SIGTERM, shell.signal.SIGKILL] if cancelled else [shell.signal.SIGKILL]
    assert signals == [(process.pid, sig) for sig in expected]
