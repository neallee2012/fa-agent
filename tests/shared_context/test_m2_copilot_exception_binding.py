"""Real FA adapter, autospecced SDK 1.0.13 failures; no runtime or model I/O."""
import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import create_autospec

import httpx
import pytest
from copilot import CopilotClient
from copilot.client import GetStatusResponse
from copilot.session import CopilotSession

from shared_context import m2_copilot
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_assistant_hosted import AssistantClient, AssistantSession
from shared_context.behavior_http_diagnostics import AssistantModelFailure
from shared_context.errors import Problem


CANARY = "OFFLINE-PRIVATE-EXCEPTION-AND-TOKEN"
SESSION = "offline-d002"
PUBLIC_ERROR = "Copilot turn failed; inspect session.error events"


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No process, token, model, or network call is allowed")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    work = tmp_path / "work"
    work.mkdir()
    runtime = tmp_path / "not-a-runtime"
    runtime.write_bytes(b"OFFLINE non-executable path-validation fixture")
    session = create_autospec(CopilotSession, instance=True)
    session.session_id = SESSION
    client = create_autospec(CopilotClient, instance=True)
    client.get_status.return_value = GetStatusResponse(version="1.0.83", protocol_version=2)
    client.create_session.return_value = session
    client.resume_session.return_value = session
    process = SimpleNamespace(pid=4242, returncode=0, poll=lambda: 0)
    client._cli_process = process
    factory = create_autospec(CopilotClient, return_value=client)
    monkeypatch.setattr(m2_copilot, "CopilotClient", factory)
    boundary = AssistantModelBoundary(forbidden)
    events = []
    options = dict(
        state_dir=tmp_path / "sdk", working_directory=work,
        sdk_session_id=SESSION, resume=False, prompt="Offline ordinary failure.",
        tools=[], api_url="https://offline.invalid", token_provider=forbidden,
        on_event=events.append, timeout_seconds=3, runtime_path=runtime,
        http_transport=httpx.MockTransport(forbidden), behavior_boundary=boundary,
    )
    return SimpleNamespace(
        client=client, session=session, process=process, factory=factory,
        boundary=boundary, events=events, options=options, root=tmp_path,
        forbidden=forbidden,
    )


def fail_at(adapter, stage):
    method = (getattr(adapter.session, stage) if stage == "send_and_wait"
              else getattr(adapter.client, stage))
    method.side_effect = ValueError(CANARY)
    adapter.options["resume"] = stage == "resume_session"


def assert_no_disclosure(adapter, error, capsys, caplog):
    captured = capsys.readouterr()
    public = json.dumps({
        "exception": str(error), "diagnostic": adapter.boundary.failure,
        "events": adapter.events, "stdout": captured.out, "stderr": captured.err,
        "logs": caplog.text,
    })
    assert CANARY not in public
    assert "UnboundLocalError" not in public
    assert error.__suppress_context__
    assert not any("sdk_stop" in event or "graceful" in event for event in adapter.events)
    for path in adapter.root.rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes()


@pytest.mark.parametrize("stage", ["get_status", "create_session", "resume_session", "send_and_wait"])
@pytest.mark.parametrize("prior_failure", [False, True], ids=["first-sdk-error", "keep-first-provider-error"])
def test_ordinary_sdk_failure_keeps_first_safe_diagnostic_and_stops_once(
    adapter, stage, prior_failure, capsys, caplog,
):
    expected = {"upstream_error_phase": "sdk_turn", "exception_type": "ValueError"}
    if prior_failure:
        expected = adapter.boundary.record_failure(AssistantModelFailure(CANARY, {
            "provider_status": 429, "upstream_error_phase": "provider_response",
            "exception_type": "ReadError", "message": CANARY, "token": CANARY,
        }), phase="provider_response")
    fail_at(adapter, stage)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(m2_copilot.run_copilot(**adapter.options))
    assert type(caught.value) is RuntimeError and str(caught.value) == PUBLIC_ERROR
    assert adapter.boundary.failure == expected
    adapter.factory.assert_called_once()
    adapter.client.start.assert_awaited_once_with()
    adapter.client.get_status.assert_awaited_once_with()
    adapter.client.stop.assert_awaited_once_with()
    adapter.client.force_stop.assert_not_awaited()
    adapter.session.abort.assert_not_awaited()
    adapter.session.get_events.assert_not_awaited()
    if stage == "get_status":
        adapter.client.create_session.assert_not_awaited()
        adapter.client.resume_session.assert_not_awaited()
    elif stage == "resume_session":
        adapter.client.create_session.assert_not_awaited()
        assert adapter.client.resume_session.await_count == 1
        assert adapter.client.resume_session.await_args.args == (SESSION,)
        assert adapter.client.resume_session.await_args.kwargs["continue_pending_work"] is False
    else:
        adapter.client.resume_session.assert_not_awaited()
        assert adapter.client.create_session.await_count == 1
        assert adapter.client.create_session.await_args.kwargs["session_id"] == SESSION
    if stage == "send_and_wait":
        adapter.session.send_and_wait.assert_awaited_once_with(
            adapter.options["prompt"], timeout=adapter.options["timeout_seconds"])
    else:
        adapter.session.send_and_wait.assert_not_awaited()
    assert_no_disclosure(adapter, caught.value, capsys, caplog)


@pytest.mark.parametrize("shutdown", ["stop-error", "exit-unverified"])
def test_ordinary_error_then_unverified_shutdown_preserves_first_diagnostic(
    adapter, shutdown, capsys, caplog,
):
    fail_at(adapter, "send_and_wait")
    if shutdown == "stop-error":
        adapter.client.stop.side_effect = RuntimeError(CANARY)
    else:
        adapter.process.poll = lambda: None
        adapter.process.returncode = None
    with pytest.raises(RuntimeError, match="^Assistant runtime shutdown is unverified$") as caught:
        asyncio.run(m2_copilot.run_copilot(**adapter.options))
    assert adapter.boundary.failure == {
        "upstream_error_phase": "sdk_turn", "exception_type": "ValueError",
    }
    adapter.client.stop.assert_awaited_once_with()
    adapter.client.force_stop.assert_awaited_once_with()
    adapter.session.send_and_wait.assert_awaited_once()
    adapter.session.abort.assert_not_awaited()
    adapter.session.get_events.assert_not_awaited()
    assert_no_disclosure(adapter, caught.value, capsys, caplog)


@pytest.mark.parametrize("exit_verified", [True, False])
def test_hosted_caller_keeps_unknown_without_turn_or_shutdown_success(
    adapter, exit_verified, capsys, caplog,
):
    fail_at(adapter, "send_and_wait")
    if not exit_verified:
        adapter.process.poll = lambda: None
        adapter.process.returncode = None
    caller = create_autospec(AssistantClient, instance=True)
    caller.assignment = SimpleNamespace(sdk_session_id=SESSION, resume=False,
                                        task=SimpleNamespace(instruction=None))
    caller.api_url = adapter.options["api_url"]
    caller.token_provider = adapter.forbidden
    runner = AssistantSession(
        caller, root=adapter.root, runtime_path=adapter.options["runtime_path"],
        model_transport=adapter.options["http_transport"],
    )
    with pytest.raises(Problem) as caught:
        asyncio.run(runner._run(adapter.options["working_directory"]))
    assert caught.value.status == 503
    assert caught.value.body["error"] == {
        "code": "ASSISTANT_SDK_UNKNOWN",
        "message": "SDK execution/shutdown is unresolved",
        "upstream_error_phase": "sdk_turn", "exception_type": "ValueError",
    }
    caller.stopped.assert_called_once_with("sdk_failed")
    for name in ("acquire", "reserve_model", "send_model", "context", "save",
                 "finish", "operation", "status", "checkpoint", "restore"):
        getattr(caller, name).assert_not_called()
    adapter.client.stop.assert_awaited_once_with()
    assert adapter.client.force_stop.await_count == (0 if exit_verified else 1)
    adapter.session.send_and_wait.assert_awaited_once()
    assert "sdk_stop" not in caught.value.body and "graceful" not in json.dumps(caught.value.body)
    assert CANARY not in json.dumps(caught.value.body)
    assert_no_disclosure(adapter, caught.value, capsys, caplog)


def test_non_assistant_profile_still_returns_original_public_error(adapter, capsys, caplog):
    fail_at(adapter, "create_session")
    adapter.options["behavior_boundary"] = None
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(m2_copilot.run_copilot(**adapter.options))
    assert str(caught.value) == PUBLIC_ERROR
    assert adapter.boundary.failure is None
    adapter.client.get_status.assert_not_awaited()
    adapter.client.stop.assert_awaited_once_with()
    adapter.client.force_stop.assert_not_awaited()
    assert_no_disclosure(adapter, caught.value, capsys, caplog)


def test_timeout_still_uses_abort_and_original_timeout_branch(adapter):
    adapter.session.send_and_wait.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        asyncio.run(m2_copilot.run_copilot(**adapter.options))
    adapter.session.abort.assert_awaited_once_with()
    adapter.client.stop.assert_awaited_once_with()
    adapter.client.force_stop.assert_not_awaited()
    assert adapter.boundary.failure == {
        "upstream_error_phase": "sdk_turn", "exception_type": "TimeoutError",
    }
    assert adapter.events == [{"type": "m2.aborted"}]
