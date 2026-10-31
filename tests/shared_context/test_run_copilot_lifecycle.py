"""run_copilot's SDK session: what the client and session receive, seen from the autospecced SDK's side."""
import asyncio
import hashlib
import logging
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from copilot import CopilotClient
from copilot.client import GetStatusResponse
from copilot.generated.rpc import PermissionDecisionReject
from copilot.session import CopilotSession
from copilot.session_events import AssistantMessageData, SessionIdleData

from shared_context import m2_copilot, sdk_telemetry
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_capability_prompt import CapabilitySession
from shared_context.behavior_delegation_runtime import SdkDelegationRuntime
from shared_context.behavior_delegation_wire import child_configuration
from shared_context.behavior_model_capacity import DeclaredModelLimits, ModelCapacitySnapshot
from shared_context.behavior_sdk_files import SESSION_FS, SdkSessionFiles
from shared_context.behavior_sdk_state import DIAG_CODECS
from shared_context.errors import Problem
from shared_context.m2_copilot import MAX_OUTPUT_TOKENS, MAX_PROMPT_TOKENS, ResidentHolder, ToolOutput, ToolSpec

SID = "lifecycle-contract"
MODEL = "fixture-model"


async def current_work():
    return True


def capabilities(**changes):
    return CapabilitySession(**{**dict(
        custom_tools=(), mcp_servers={}, mcp_tools=(), builtin_tools=(), custom_agents=(), vision=False,
        image_tools=frozenset(), permitted_mcp=frozenset()), **changes})


class Hitl:
    def observe(self, _value):
        pass

    async def permission(self, _request, _invocation):
        return True, None

    async def user_input(self, _request, _invocation):
        return {}


class Operations:
    sdk_session_id = None

    def observe(self, _value):
        pass

    async def pre_tool(self, data, context, original):
        return {"seen": data}


def session_files(**changes):
    files = create_autospec(SdkSessionFiles, instance=True)
    for name, value in {**dict(
            session_id=SID, restored=False, model_provider=None, checkpoint_profile=None, automatic=False,
            previous=None, checkpoint_error=None, failed=False, compaction=None, automatic_open=False,
            files={}), **changes}.items():
        setattr(files, name, value)
    return files


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    session = create_autospec(CopilotSession, instance=True)
    session.session_id = SID
    session.get_events.return_value = []
    session.send_and_wait.return_value = None
    client = create_autospec(CopilotClient, instance=True)
    client.get_status.return_value = GetStatusResponse(version="1.0.83", protocol_version=2)
    client.create_session.return_value = session
    client.resume_session.return_value = session
    client._cli_process = SimpleNamespace(pid=7, returncode=0, poll=lambda: 0, kill=lambda: None)
    factory = create_autospec(CopilotClient, return_value=client)
    monkeypatch.setattr(m2_copilot, "CopilotClient", factory)
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"offline fixture")
    work = tmp_path / "work"
    work.mkdir()
    events = []

    async def lookup(_packet):
        return ToolOutput("looked up")

    defaults = dict(
        state_dir=tmp_path / "sdk", working_directory=work, sdk_session_id=SID, resume=False, prompt="Fixture",
        tools=[ToolSpec("lookup", "Fixture", {"type": "object"}, lookup)], api_url="https://offline.invalid",
        token_provider=lambda: "token", on_event=events.append, timeout_seconds=30, runtime_path=runtime,
        model=MODEL)

    def instruction(**overrides):
        return {"behavior_boundary": AssistantModelBoundary(lambda *_: "token"), "instruction": True, **overrides}

    def run(**overrides):
        return asyncio.run(m2_copilot.run_copilot(**{**defaults, **overrides}))

    def options():
        return client.create_session.await_args.kwargs

    return SimpleNamespace(client=client, session=session, factory=factory, run=run, options=options,
                           defaults=defaults, instruction=instruction, events=events, state_dir=tmp_path / "sdk", work=work)


def test_a_plain_turn_gets_the_fixed_prompt_limits_and_no_extras(sdk):
    sdk.run()
    options = sdk.options()
    assert options["session_id"] == SID and options["model"] == MODEL
    assert options["system_message"]["mode"] == "replace"
    assert list(options["available_tools"]) == ["custom:lookup"]
    assert list(options["excluded_tools"]) == ["builtin:*", "mcp:*"]
    assert options["tools"][0].name == "lookup" and options["tools"][0].skip_permission is False
    provider = options["provider"]
    assert (provider["max_output_tokens"], provider["max_prompt_tokens"]) == (MAX_OUTPUT_TOKENS, MAX_PROMPT_TOKENS)
    assert provider["base_url"] == "https://offline.invalid/model/v1"
    assert provider["wire_api"] == "completions" and provider["wire_model"] == MODEL
    assert options["model_capabilities"].supports.vision is True
    assert options["model_capabilities"].limits.max_context_window_tokens == MAX_PROMPT_TOKENS + 1024
    assert options["working_directory"] == str(sdk.work.resolve())
    assert options["config_directory"] == str(sdk.state_dir.resolve() / "config")
    assert options["mcp_servers"] == {} and options["custom_agents"] == []
    for absent in ("hooks", "on_user_input_request", "create_session_fs_handler", "infinite_sessions",
                   "on_mcp_auth_request", "reasoning_summary"):
        assert absent not in options


def test_the_plain_provider_token_is_the_turns_own_token(sdk):
    sdk.run(token_provider=lambda: "rotated")
    assert asyncio.run(sdk.options()["provider"]["bearer_token_provider"](None)) == "rotated"


def test_an_assistant_provider_never_holds_a_credential(sdk):
    sdk.run(**sdk.instruction())
    assert sdk.options()["provider"]["bearer_token_provider"](None) == "fa-runtime-no-credential"


def test_an_instruction_leaves_limits_to_its_snapshot(sdk):
    sdk.run(**sdk.instruction())
    options = sdk.options()
    assert "model_capabilities" not in options
    assert "max_output_tokens" not in options["provider"] and "max_prompt_tokens" not in options["provider"]


def test_an_instruction_capacity_snapshot_sets_the_limits(sdk):
    snapshot = ModelCapacitySnapshot.from_declared(MODEL, DeclaredModelLimits(
        context_window_tokens=2000, max_prompt_tokens=1000, max_output_tokens=100))
    sdk.run(**sdk.instruction(model_capacity=snapshot))
    options = sdk.options()
    assert (options["provider"]["max_prompt_tokens"], options["provider"]["max_output_tokens"]) == (1000, 100)
    assert options["model_capabilities"].limits.max_context_window_tokens == 2000
    assert options["model_capabilities"].supports is None
    listed = sdk.factory.call_args.kwargs["on_list_models"]()[0]
    assert listed.capabilities.limits.max_prompt_tokens == 1000
    assert listed.capabilities.limits.max_context_window_tokens == 2000


def test_a_reasoning_summary_turn_asks_for_low_effort_on_responses(sdk):
    sdk.run(**sdk.instruction(model_provider="openai-responses", reasoning_summary="detailed"))
    options = sdk.options()
    assert options["reasoning_summary"] == "detailed" and options["reasoning_effort"] == "low"
    assert options["model_capabilities"].supports.reasoning_effort is True
    assert options["provider"]["wire_api"] == "responses"
    assert options["provider"]["base_url"] == "https://offline.invalid/model/v1"


def test_only_a_vision_grant_tells_the_model_about_images(sdk):
    sdk.run(**sdk.instruction(capability_session=capabilities(vision=True)))
    assert sdk.options()["model_capabilities"].supports.vision is True
    assert sdk.factory.call_args.kwargs["on_list_models"]()[0].capabilities.supports.vision is True
    sdk.run(**sdk.instruction())
    assert sdk.factory.call_args.kwargs["on_list_models"]()[0].capabilities.supports.vision is False


def test_mcp_servers_get_their_own_home_and_the_catalog_env_wins(sdk, tmp_path, caplog):
    catalog = {"playwright": {"args": ["--headless"], "env": {"XDG_DATA_HOME": "/catalog", "TEMP": "/catalog-temp"}}}
    grant = capabilities(mcp_servers=catalog, mcp_tools=("playwright-shot",))
    with caplog.at_level(logging.WARNING):
        sdk.run(**sdk.instruction(capability_session=grant, capability_current=current_work,
                                  mcp_temp_directory=tmp_path / "t", mcp_display_env={"DISPLAY": ":1"}))
    options = sdk.options()
    server = options["mcp_servers"]["playwright"]
    home = sdk.state_dir.resolve() / "mcp-home" / "playwright"
    assert home.is_dir()
    assert server["env"] == {
        "HOME": str(home), "XDG_CACHE_HOME": str(home / ".cache"), "XDG_DATA_HOME": "/catalog", "DISPLAY": ":1",
        "TMPDIR": str(tmp_path / "t"), "TMP": str(tmp_path / "t"), "TEMP": "/catalog-temp"}
    assert server["args"] == []
    assert catalog["playwright"]["args"] == ["--headless"] and "HOME" not in catalog["playwright"]["env"]
    assert list(options["excluded_tools"]) == ["builtin:*"]
    assert list(options["available_tools"]) == ["custom:lookup", "mcp:playwright-shot"]
    assert options["on_mcp_auth_request"](None, None) == {"kind": "cancelled"}
    assert '"headed": true' in caplog.text and '"browser": "launched"' in caplog.text


def test_custom_agents_are_copied_for_the_session(sdk):
    agents = ({"name": "helper"},)
    sdk.run(**sdk.instruction(capability_session=capabilities(custom_agents=agents)))
    assert sdk.options()["custom_agents"] == [{"name": "helper"}]
    assert sdk.options()["custom_agents"][0] is not agents[0]


def test_a_human_review_turn_opens_only_ask_user_among_builtins(sdk):
    hitl = Hitl()
    sdk.run(**sdk.instruction(native_hitl=hitl))
    options = sdk.options()
    assert list(options["available_tools"]) == ["custom:lookup", "builtin:ask_user"]
    assert options["excluded_tools"] == ["mcp:*"]
    assert options["on_user_input_request"] == hitl.user_input


def test_operations_guard_the_pre_tool_hook(sdk):
    operations = Operations()
    sdk.run(**sdk.instruction(native_operations=operations))
    assert operations.sdk_session_id == SID
    hooks = sdk.options()["hooks"]
    assert list(hooks) == ["on_pre_tool_use"]
    assert asyncio.run(hooks["on_pre_tool_use"]({"a": 1}, None)) == {"seen": {"a": 1}}


def test_session_files_replace_the_disk_for_the_session_and_the_client(sdk):
    files = session_files()
    sdk.run(**sdk.instruction(session_files=files))
    options = sdk.options()
    assert options["working_directory"] == SESSION_FS["initial_working_directory"]
    assert options["create_session_fs_handler"](None) is files
    assert sdk.factory.call_args.kwargs["session_fs"] == SESSION_FS
    assert "infinite_sessions" not in options


def test_the_client_runs_the_pinned_runtime_alone_in_the_turns_directories(sdk, monkeypatch):
    monkeypatch.setenv("HOME", "/fixture-home")
    monkeypatch.setenv("PRIVATE_VALUE", "not-for-the-runtime")
    sdk.run()
    client = sdk.factory.call_args.kwargs
    state = sdk.state_dir.resolve()
    assert client["env"]["HOME"] == "/fixture-home" and "PRIVATE_VALUE" not in client["env"]
    assert client["env"]["COPILOT_SKIP_CLI_DOWNLOAD"] == "1"
    assert client["env"]["XDG_CONFIG_HOME"] == str(state / "config")
    for name in ("TMPDIR", "TMP", "TEMP"):
        assert client["env"][name] == str(state / "runtime-work")
    assert (state / "config").is_dir() and (state / "runtime-work").is_dir()
    assert client["working_directory"] == str(sdk.work.resolve()) and client["base_directory"] == str(state)
    assert client["use_logged_in_user"] is False and client["mode"] == "empty"
    assert "session_fs" not in client
    assert client["on_list_models"]()[0].capabilities.limits.max_prompt_tokens == MAX_PROMPT_TOKENS


def test_a_resumed_turn_continues_no_pending_work(sdk):
    sdk.run(resume=True)
    sdk.client.create_session.assert_not_awaited()
    assert sdk.client.resume_session.await_args.args == (SID,)
    assert sdk.client.resume_session.await_args.kwargs["continue_pending_work"] is False


class Process:
    pid = 7

    def __init__(self):
        self.returncode, self.killed = None, 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed += 1


@pytest.fixture
def running(sdk, monkeypatch):
    """The SDK as a resident turn sees it: a CLI that lives until stopped, a timeline, and an answer at idle."""
    process, listeners, sdk.timeline = Process(), [], []
    sdk.client._cli_process = sdk.process = process

    def record(name, effect=None):
        def step(*_args, **_kwargs):
            sdk.timeline.append(name)
            return None if effect is None else effect()
        return step

    def listen(handler):
        listeners.append(handler)
        return lambda: None

    async def send(_prompt, **_options):
        for data in (AssistantMessageData(content="answer", message_id="m-1"), SessionIdleData()):
            for handler in list(listeners):
                handler(SimpleNamespace(data=data))
        return "message"

    sdk.session.on.side_effect = listen
    sdk.session.send.side_effect = send
    sdk.session.send_and_wait.side_effect = None
    sdk.session.disconnect.side_effect = record("disconnect")
    sdk.session.abort.side_effect = record("abort")
    sdk.client.stop.side_effect = record("stop", lambda: setattr(process, "returncode", 0))
    sdk.client.force_stop.side_effect = record("force_stop")
    monkeypatch.setattr(sdk_telemetry, "forward", lambda _span, *, final: sdk.timeline.append(f"forward final={final}"))
    return sdk


def resident_turn(sdk, files=None, **overrides):
    holder, files = ResidentHolder(), session_files() if files is None else files
    return holder, sdk.instruction(session_files=files, resident=holder, timeout_seconds=None, **overrides)


def stop_reasons(caplog):
    return [record.getMessage().removeprefix("RESIDENT_SDK_STOPPED reason=") for record in caplog.records
            if record.getMessage().startswith("RESIDENT_SDK_STOPPED")]


def test_a_clean_idle_turn_keeps_its_sdk_and_publishes_the_files_it_checked(running):
    stopped, files = [], session_files()
    holder, options = resident_turn(running, files, on_sdk_stopped=stopped.append)
    outcome = running.run(**options)
    assert running.timeline == ["forward final=False"]
    live = holder.live
    assert (live.client, live.session, live.process, live.turn) == (running.client, running.session, running.process, None)
    expected = {"runtime_version": "1.0.83", "pid": 7, "graceful": True, "release": "fa-resident-worker-poc-v1"}
    assert outcome["sdk_stop"] == expected and stopped == [expected]
    assert stopped[0] is not outcome["sdk_stop"]
    assert outcome["release_state"] == (files.snapshot.return_value, hashlib.sha256(b"[]").hexdigest())
    files.snapshot.assert_called_once_with(freeze=False)
    assert outcome["final_message"] == "answer"


@pytest.mark.parametrize("changes, reason", [
    ({"checkpoint_profile": "manual"}, "manual_profile"),
    ({"previous": SimpleNamespace(codec=next(iter(DIAG_CODECS)), sha256="0" * 64)}, "diag_codec"),
    ({"checkpoint_error": "failed"}, "checkpoint_failed"),
    ({"failed": True}, "checkpoint_failed"),
    ({"compaction": "open"}, "compaction_running"),
    ({"automatic_open": True}, "compaction_running"),
    ({"checkpoint_profile": "auto", "automatic": True, "failed": True}, "checkpoint_failed"),
], ids=["manual", "diag", "error", "failed", "compacting", "auto-open", "auto-failed"])
def test_a_turn_with_unsafe_files_stops_its_sdk_flushing_them_first(running, caplog, changes, reason):
    stopped = []
    holder, options = resident_turn(running, session_files(**changes), on_sdk_stopped=stopped.append)
    with caplog.at_level(logging.WARNING):
        outcome = running.run(**options)
    assert stop_reasons(caplog) == [reason]
    assert running.timeline == ["disconnect", "stop", "forward final=True"]
    assert holder.live is None
    expected = {"runtime_version": "1.0.83", "pid": 7, "exit_code": 0, "graceful": True}
    assert outcome["sdk_stop"] == expected and stopped == [expected]
    assert "release_state" not in outcome


def test_an_automatic_profile_with_nothing_open_keeps_the_sdk(running):
    holder, options = resident_turn(running, session_files(checkpoint_profile="auto", automatic=True))
    running.run(**options)
    assert holder.live is not None and running.timeline == ["forward final=False"]


def test_files_that_move_right_after_idle_stop_the_sdk(running, caplog):
    files = session_files()

    async def on_reply(_parts):
        asyncio.get_running_loop().call_soon(files.files.update, {"events": "late write"})

    holder, options = resident_turn(running, files, on_reply=on_reply)
    with caplog.at_level(logging.WARNING):
        running.run(**options)
    assert stop_reasons(caplog) == ["files_moving"] and holder.live is None


def test_a_cancel_during_the_release_check_stops_the_sdk_and_still_cancels(running, caplog):
    async def on_reply(_parts):
        asyncio.current_task().cancel()

    holder, options = resident_turn(running, on_reply=on_reply)
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        running.run(**options)
    assert stop_reasons(caplog) == ["cancelled"] and holder.live is None
    assert running.timeline == ["disconnect", "stop", "forward final=True"]
    running.session.abort.assert_not_awaited()


def test_a_snapshot_that_cannot_publish_stops_the_sdk_with_the_turn_still_bound(running, caplog):
    files = session_files()
    files.snapshot.side_effect = Problem(409, "REFUSED", "refused")

    def stop():
        running.options()["on_event"]({"type": "late.runtime.event"})
        running.process.returncode = 0

    running.client.stop.side_effect = stop
    holder, options = resident_turn(running, files)
    with caplog.at_level(logging.WARNING):
        outcome = running.run(**options)
    assert stop_reasons(caplog) == ["release_refused"] and holder.live is None
    assert {"type": "late.runtime.event"} in running.events
    assert "release_state" not in outcome and outcome["sdk_stop"]["exit_code"] == 0


def test_a_failed_turn_gives_up_its_sdk_and_reports_only_the_public_error(running, caplog):
    running.session.send.side_effect = ValueError("private")
    holder, options = resident_turn(running)
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="^Copilot turn failed"):
        running.run(**options)
    assert stop_reasons(caplog) == ["turn_incomplete"] and holder.live is None
    assert running.timeline == ["disconnect", "stop", "forward final=True"]


def test_a_cli_that_exited_during_the_turn_is_never_kept(running, caplog):
    running.client.stop.side_effect = None

    async def on_reply(_parts):
        running.process.returncode = 1

    holder, options = resident_turn(running, on_reply=on_reply)
    with caplog.at_level(logging.WARNING):
        outcome = running.run(**options)
    assert stop_reasons(caplog) == ["turn_incomplete"] and holder.live is None
    assert outcome["sdk_stop"]["exit_code"] == 1


def test_a_plain_turn_logs_no_resident_stop_and_claims_no_verified_exit(running, caplog):
    stopped = []
    with caplog.at_level(logging.WARNING):
        outcome = running.run(on_sdk_stopped=stopped.append)
    assert running.timeline == ["stop", "forward final=True"]
    assert stop_reasons(caplog) == [] and stopped == [] and "sdk_stop" not in outcome


def test_a_plain_turn_survives_a_stop_failure_by_forcing_the_exit(running):
    def fail():
        running.timeline.append("stop")
        raise ValueError("private")

    def forced():
        running.timeline.append("force_stop")
        running.process.returncode = 0

    running.client.stop.side_effect, running.client.force_stop.side_effect = fail, forced
    running.session.send_and_wait.return_value = SimpleNamespace(data=SimpleNamespace(content="answer"))
    outcome = running.run()
    assert running.timeline == ["stop", "force_stop", "forward final=True"]
    assert "sdk_stop" not in outcome
    assert outcome["final_message"] == "answer"


def test_a_cli_that_survives_every_stop_is_reported_after_telemetry_and_unbinding(running, monkeypatch):
    monkeypatch.setattr(m2_copilot, "EXIT_CONFIRM_SECONDS", 0)
    monkeypatch.setattr(m2_copilot, "KILL_CONFIRM_SECONDS", 0)
    running.client.stop.side_effect = ValueError("private")
    with pytest.raises(m2_copilot.SdkRuntimeStillRunning) as caught:
        running.run(**running.instruction())
    assert caught.value.process is running.process and running.process.killed == 1
    assert running.timeline == ["force_stop", "forward final=True"]


@pytest.mark.parametrize("abort_fails, event", [(False, "m2.aborted"), (True, "m2.abort_failed")])
def test_a_cancelled_turn_aborts_then_stops_and_stays_cancelled(running, abort_fails, event):
    options = running.instruction()

    async def scenario():
        reached = asyncio.Event()

        async def hang(_prompt, **_options):
            reached.set()
            await asyncio.Event().wait()

        running.session.send_and_wait.side_effect = hang
        if abort_fails:
            running.session.abort.side_effect = ValueError("private")
        task = asyncio.ensure_future(m2_copilot.run_copilot(**{**running.defaults, **options}))
        await reached.wait()
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())
    assert running.timeline[-2:] == ["stop", "forward final=True"]
    assert [item["type"] for item in running.events][-1] == event
    assert options["behavior_boundary"].failure["exception_type"] == "CancelledError"


def test_a_cancel_before_any_session_stops_the_client_without_an_abort(running):
    async def scenario():
        reached = asyncio.Event()

        async def hang():
            reached.set()
            await asyncio.Event().wait()

        running.client.start.side_effect = hang
        task = asyncio.ensure_future(m2_copilot.run_copilot(**running.defaults))
        await reached.wait()
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())
    assert running.timeline == ["stop", "forward final=True"]


def test_a_real_timeout_aborts_and_reports_a_timeout(running):
    async def hang(_prompt, **_options):
        await asyncio.Event().wait()

    running.session.send_and_wait.side_effect = hang
    with pytest.raises(TimeoutError):
        running.run(timeout_seconds=0.05)
    assert running.timeline == ["abort", "stop", "forward final=True"]


def test_a_cancel_during_the_stop_still_seals_telemetry_and_forces_nothing(running):
    async def scenario():
        reached = asyncio.Event()

        async def hang():
            running.timeline.append("stop")
            reached.set()
            await asyncio.Event().wait()

        running.client.stop.side_effect = hang
        task = asyncio.ensure_future(m2_copilot.run_copilot(**running.defaults))
        await reached.wait()
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())
    assert running.timeline == ["stop", "forward final=True"]


@pytest.mark.parametrize("first_true", [True, False], ids=["before-compaction", "after-compaction"])
def test_a_cancel_noticed_before_send_sends_nothing(running, first_true):
    answers = iter([True] if first_true else [False, True])
    with pytest.raises(RuntimeError, match="^Copilot turn failed"):
        running.run(cancel_requested=lambda: next(answers))
    running.session.send_and_wait.assert_not_awaited()
    assert running.timeline == ["stop", "forward final=True"]


def test_a_reply_preview_runs_before_the_stop_and_its_failure_changes_nothing(running):
    async def on_reply(parts):
        running.timeline.append(("reply", parts))
        raise ValueError("private")

    message = SimpleNamespace(data=SimpleNamespace(content="answer"))
    running.session.send_and_wait.return_value = message
    outcome = running.run(on_reply=on_reply)
    assert running.timeline == [("reply", ["answer"]), "stop", "forward final=True"]
    assert outcome["final_message"] == "answer" and outcome["final_parts"] == ["answer"]
    assert running.events == [{"type": "m2.reply_preview_failed", "data": {"exception_type": "ValueError"}}]


def test_a_model_call_limit_fails_the_turn_even_with_an_answer(running):
    options = running.instruction()

    async def send_and_wait(_prompt, **_options):
        options["behavior_boundary"].failure = {"upstream_error_code": "MODEL_CALL_LIMIT_REACHED"}

    running.session.send_and_wait.side_effect = send_and_wait
    with pytest.raises(RuntimeError, match="^Copilot turn failed"):
        running.run(**options)
    running.session.get_events.assert_not_awaited()


def test_an_assistant_history_keeps_only_event_types(running):
    running.session.get_events.return_value = [{"type": "user.message", "data": {"content": "private"}}]
    outcome = running.run(**running.instruction(on_sdk_stopped=None))
    assert outcome["history"] == [{"type": "user.message"}]


def delegation(sdk_session_id=SID):
    runtime = create_autospec(SdkDelegationRuntime, instance=True)
    runtime.sdk_session_id, runtime.read_user_files, runtime.attempted = sdk_session_id, False, False
    return runtime


def delegated(running, runtime):
    grant = capabilities(builtin_tools=("task",), custom_agents=(child_configuration(),))
    return running.instruction(capability_session=grant, delegation_runtime=runtime)


def test_a_delegating_turn_finishes_its_children_after_the_sdk_stops(running):
    runtime = delegation()
    runtime.finish.side_effect = lambda: running.timeline.append("finish")
    outcome = running.run(**delegated(running, runtime))
    assert running.timeline == ["stop", "forward final=True", "finish"]
    assert outcome["sdk_stop"]["exit_code"] == 0
    assert running.options()["custom_agents"][0]["model"] == MODEL
    assert list(running.options()["excluded_tools"]) == ["mcp:*"]
    assert set(running.options()["hooks"]) == {"on_pre_tool_use", "on_post_tool_use"}
    runtime.require_complete.assert_awaited_once_with()


def test_an_unverified_stop_leaves_delegation_unfinished(running):
    runtime = delegation()
    running.client.stop.side_effect = lambda: None
    with pytest.raises(RuntimeError, match="^Assistant runtime shutdown is unverified$"):
        running.run(**delegated(running, runtime))
    runtime.finish.assert_not_awaited()
    assert "force_stop" in running.timeline


def test_a_resident_turn_that_attempted_delegation_does_not_keep_the_sdk(running, caplog):
    runtime = delegation()
    runtime.attempted = True
    files = session_files()
    holder = ResidentHolder()
    options = delegated(running, runtime)
    with caplog.at_level(logging.WARNING):
        running.run(**{**options, "session_files": files, "resident": holder, "timeout_seconds": None})
    assert stop_reasons(caplog) == ["delegation"] and holder.live is None


def test_a_compacting_task_turns_on_infinite_sessions(sdk):
    profile = object()
    task = SimpleNamespace(instruction=SimpleNamespace(checkpoint_profile=profile))
    sdk.run(**sdk.instruction(session_files=session_files(checkpoint_profile=profile), checkpoint_task=task))
    assert sdk.options()["infinite_sessions"] == {"enabled": True}


def test_the_next_turn_reaches_a_kept_sdk_through_its_own_handlers(running):
    holder, options = resident_turn(running)
    running.run(**options)
    first_events = list(running.events)
    registered = running.options()
    later = []
    running.run(**{**options, "on_event": later.append})
    running.client.create_session.assert_awaited_once()
    running.client.start.assert_awaited_once()
    registered["on_event"]({"type": "after.release"})
    assert running.events == first_events and later == []
    assert isinstance(registered["on_permission_request"](None, {}), PermissionDecisionReject)


def test_a_bound_kept_sdk_forwards_its_events_to_the_turn_running_now(running):
    holder, options = resident_turn(running)
    running.run(**options)
    registered, later = running.options(), []
    original = running.session.send.side_effect

    async def send(prompt, **kwargs):
        registered["on_event"]({"type": "second.turn"})
        return await original(prompt, **kwargs)

    running.session.send.side_effect = send
    running.run(**{**options, "on_event": later.append})
    assert {"type": "second.turn"} in later


def test_changed_tools_restart_a_kept_sdk_from_its_live_files(running, caplog):
    holder, options = resident_turn(running)
    running.run(**options)
    kept = holder.live
    running.client.stop.side_effect = None
    tools = running.defaults["tools"] + [ToolSpec("extra", "Fixture", {"type": "object"}, running.defaults["tools"][0].handler)]
    with caplog.at_level(logging.WARNING):
        running.run(**{**options, "tools": tools})
    assert "RESIDENT_SDK_RESTARTED reason=settings_changed" in caplog.text
    running.session.disconnect.assert_awaited()
    assert running.client.resume_session.await_args.kwargs["continue_pending_work"] is False
    running.client.create_session.assert_awaited_once()
    assert holder.live is not kept


def test_a_cli_that_outlives_its_replacement_stop_starts_no_second_sdk(running):
    holder, options = resident_turn(running)
    running.run(**options)
    kept = holder.live

    async def stop(_on_unexited=None):
        kept.unexited = running.process

    kept.stop = stop
    kept.digest = "other settings"
    with pytest.raises(m2_copilot.SdkRuntimeStillRunning):
        running.run(**options)
    running.client.start.assert_awaited_once()
    assert holder.live is None


def test_a_new_client_waits_for_the_conversations_earlier_stops(running):
    order = []

    async def settle():
        order.append("settle")

    running.client.start.side_effect = lambda: order.append("start")
    holder, options = resident_turn(running)
    holder.settle = settle
    running.run(**options)
    assert order == ["settle", "start"]


def test_a_compaction_starting_right_after_idle_stops_the_sdk(running, caplog):
    files = session_files()

    async def on_reply(_parts):
        asyncio.get_running_loop().call_soon(setattr, files, "automatic_open", True)

    holder, options = resident_turn(running, files, on_reply=on_reply)
    with caplog.at_level(logging.WARNING):
        running.run(**options)
    assert stop_reasons(caplog) == ["compaction_running"] and holder.live is None


def test_a_later_turn_that_fails_empties_the_slot_of_its_kept_sdk(running, caplog):
    holder, options = resident_turn(running)
    running.run(**options)
    assert holder.live is not None
    running.session.send.side_effect = ValueError("private")
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="^Copilot turn failed"):
        running.run(**options)
    assert stop_reasons(caplog) == ["turn_incomplete"] and holder.live is None
    running.client.stop.assert_awaited_once()
