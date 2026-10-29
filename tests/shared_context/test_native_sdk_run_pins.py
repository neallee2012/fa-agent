"""NativeSdkExecutor._run: behaviour pins through the real turn entry (the executor call), stubbed run_copilot only."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shared_context import behavior_sdk_state, native_sdk
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeSubmit
from shared_context.m2_copilot import ToolSpec
from shared_context.native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider
from shared_context.native_turns import NativeTurnRuntime
from shared_context.m3_skills import PurposeSkillReadResult
from test_native_capabilities import selected_capture
from test_native_assembly import install_executor, offline_sdk, run_turn, skill_request
from test_native_responses import responses  # noqa: F401
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, harness, local_env, native_retry_policy  # noqa: F401


def probe(name="probe", entry=None, tools=True, **binding):
    async def bind(context, grants):
        names = {tool for grant in grants for tool in grant.tools} if tools else set()
        return NativeCapabilityBinding(
            tools=tuple(ToolSpec(tool, "OFFLINE probe", {"type": "object"}, lambda packet: None) for tool in names),
            result_schemas={tool: PurposeSkillReadResult for tool in names}, **binding)

    return NativeCapabilityProvider(accepts=lambda grant: entry in {None, grant.entry_id}, bind=bind, approved_skills=True, name=name)


def test_a_binding_feeds_its_addendum_options_events_and_images_into_the_sdk_call(responses, monkeypatch, tmp_path):
    events, images, seen = [], [], []

    async def sdk(**options):
        seen.append(options)
        options["on_event"]({"type": "OFFLINE-event"})
        options["on_tool_image"]("OFFLINE-image")
        return await offline_sdk(**options)

    request, _, _ = skill_request(submit())
    binding = probe(system_addendum="ADDENDUM-LITERAL", mcp_temp_directory=tmp_path / "mcp-tmp",
                    mcp_display_env={"DISPLAY": ":9"}, on_event=events.append, on_tool_image=images.append)
    install_executor(responses, monkeypatch, tmp_path, sdk, capability_providers=(binding,))
    receipt = run_turn(responses, request)
    options = seen[0]
    assert options["system_message"].endswith("ADDENDUM-LITERAL")
    assert options["mcp_temp_directory"] == tmp_path / "mcp-tmp" and options["mcp_display_env"] == {"DISPLAY": ":9"}
    assert "delegation_runtime" not in options and "native_operations" not in options
    assert events == [{"type": "OFFLINE-event"}] and images == ["OFFLINE-image"]
    assert {part.name: part.state for part in receipt.observed_assembly.parts}["providers"] == "included"


@pytest.mark.parametrize("adapter", ["mcp_temp_directory", "mcp_display_env"])
def test_two_bindings_cannot_each_bring_their_own_sdk_adapter(responses, monkeypatch, tmp_path, adapter):
    seen = []

    async def sdk(**options):
        seen.append(options)
        return await offline_sdk(**options)

    request = NativeSubmit(command=submit().command, capture=selected_capture("fa-memory-candidate-v1", "fa-image-delivery-v1"))
    one, two = (tmp_path / "one", tmp_path / "two") if adapter == "mcp_temp_directory" else ({"DISPLAY": ":1"}, {"DISPLAY": ":2"})
    providers = (probe("first", "fa-memory-candidate-v1", **{adapter: one}),
                 probe("second", "fa-image-delivery-v1", **{adapter: two}))
    install_executor(responses, monkeypatch, tmp_path, sdk, capability_providers=providers)
    assert responses.invoke(request).status_code == 202
    refused = responses.wait(request.command.request_id, "unknown")
    assert refused.failure_code == "NATIVE_EXECUTION_UNAVAILABLE" and seen == []


def test_a_checkpoint_turn_that_is_not_the_original_one_refuses_before_the_sdk(responses, monkeypatch, tmp_path):
    seen = []

    async def sdk(**options):
        seen.append(options)
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk)
    original = native_sdk.NativeSdkExecutor._run
    values = submit().model_dump(mode="json")
    values["capture"]["template"]["settings"]["checkpoint_profile"] = {
        "name": "auto-no-custom-v1", "platform": behavior_sdk_state.local_platform(), "provider": values["capture"]["template"]["settings"]["model_provider"]}
    request = NativeSubmit.model_validate_json(json.dumps(values))

    async def without_turn(self, receipt, scope, previous, control, *rest):
        control.checkpoint_turn = None
        return await original(self, receipt, scope, previous, control, *rest)

    monkeypatch.setattr(native_sdk.NativeSdkExecutor, "_run", without_turn)
    assert responses.invoke(request).status_code == 202
    refused = responses.wait("rid-1", "unknown")
    assert refused.failure_code == "SDK_STATE_SOURCE_UNVERIFIED" and seen == []


@pytest.mark.parametrize("released", [False, True])
def test_a_result_that_cannot_be_saved_discards_the_kept_sdk(responses, monkeypatch, tmp_path, caplog, released):
    discarded = []

    async def sdk(**options):
        result = await offline_sdk(**options)
        del result["final_message"]
        if released:
            result["sdk_stop"] = {"pid": 123, "release": "fa-resident-worker-poc-v1", "graceful": True}
        return result

    executor = install_executor(responses, monkeypatch, tmp_path, sdk)

    async def discard(holder):
        discarded.append(holder)

    monkeypatch.setattr(executor.resident, "discard", discard)
    caplog.set_level(logging.WARNING, logger=native_sdk.__name__)
    assert responses.invoke(submit()).status_code == 202
    failed = responses.wait("rid-1", "unknown")
    assert failed.failure_code == "NATIVE_TURN_UNKNOWN" and len(discarded) == 1
    assert ("RESIDENT_SDK_DISCARDED reason=save_failed" in caplog.text) is released


def test_the_images_of_a_turn_need_the_original_native_store(local_env, monkeypatch, tmp_path):
    from shared_context import behavior_assistant_wire as aw
    from shared_context.behavior_user_images import NativeUserImageUpload
    from test_user_images_native import _native_case, encoded, reference

    runtime, capture, seen = _native_case(local_env, monkeypatch, tmp_path)
    runtime.executor.reader = None
    command = aw.InstructionSubmit(request_id="rid-image", content="See the chart", images=(reference(),))
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        h = Harness(runtime, SimpleNamespace(controls={}), client)
        assert h.invoke(NativeUserImageUpload(png_base64=encoded())).status_code == 200
        assert h.invoke(NativeSubmit(command=command, capture=capture)).status_code == 202
        refused = h.wait("rid-image", "unknown")
    assert refused.failure_code == "NATIVE_EXECUTION_UNAVAILABLE" and seen == []


def recover(local_env, monkeypatch, tmp_path, sdk, *, entries=(), providers=(), final="completed", capture=None,
            **executor_options):
    """The original turn stays started in one runtime; a second one, with the real executor, recovers it."""
    import asyncio
    from azure.ai.agentserver.core.tasks import RetryPolicy
    from shared_context.behavior_native_turn_wire import NativeContinuationRead, NativeContinuationSubmit
    from shared_context.native_effects import NativeEffects
    from shared_context.native_turns import NativePostTurnPending
    from test_native_turns import OfflineSdk

    def asked(rid, cid=None, text="Hello."):
        return NativeSubmit(command=submit(rid, cid, text).command,
                            capture=selected_capture(*entries) if capture is None else capture)

    first_sdk = OfflineSdk()
    first_runtime = NativeTurnRuntime(IDENTITY, first_sdk)

    async def tracked(receipt, scope, previous, control):
        await NativeEffects(first_runtime, receipt, control).start_operations()
        return await first_sdk(receipt, scope, previous, control)

    async def unexpected(_view):
        raise AssertionError("No continuation is dispatched before official recovery")

    first_runtime.executor, first_runtime.on_continuation = tracked, unexpected
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first_runtime)) as client:
        first = Harness(first_runtime, first_sdk, client)
        first.invoke(asked("completed-base"))
        base = first.wait("completed-base", "completed")
        first_sdk.block = True
        first.invoke(asked("interrupted-root", base.conversation_id, "Continue the original work."))
        source = first.wait("interrupted-root", "started")

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "recovery", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE", capability_providers=providers, **executor_options)
    second_runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight, delivery_retry=RetryPolicy(
        max_attempts=3, initial_delay=0.01, max_delay=0.01, jitter=False, retry_on=(NativePostTurnPending,)))
    executor.reader = second_runtime
    accepted = asyncio.Event()

    async def deliver(view):
        original = await second_runtime.read(view.continuation.continuation_of)
        command = original.command.model_copy(update={
            "request_id": view.request_id, "conversation_id": original.conversation_id})
        result = await second_runtime.continue_turn(NativeContinuationSubmit(
            command=command, capture=original.capture, source=original.source,
            state_profile=original.state_profile, continuation=view.continuation))
        accepted.set()
        return result

    second_runtime.on_continuation = deliver
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=second_runtime)) as client:
        second = Harness(second_runtime, OfflineSdk(), client)
        second.wait(source.request_id, "unknown")
        second.call(asyncio.wait_for, accepted.wait(), 2)
        state = second.invoke(NativeContinuationRead(request_id=source.request_id)).json()
        return source, second.wait(state["request_id"], final)


def recovery_tool(options):
    return next(tool for tool in options["tools"] if tool.name == "read_continuation_context")


def test_a_recovery_turn_gets_the_continuation_reader_and_the_operation_guard(local_env, monkeypatch, tmp_path):
    seen = []

    async def sdk(**options):
        seen.append(options)
        result = await recovery_tool(options).handler({
            "session_id": options["sdk_session_id"], "tool_call_id": "read-recovery",
            "arguments": {"section": "request"}})
        seen.append(json.loads(result.text))
        return await offline_sdk(**options)

    source, derived = recover(local_env, monkeypatch, tmp_path, sdk)
    options, page = seen
    assert "## Original work continuation" in options["system_message"]
    assert "Never resend append commands or treat unknown appends as already applied." in options["system_message"]
    assert derived.continuation.continuation_of == source.request_id
    assert sorted(options["native_operations"].bindings) == [
        "execute_operation", "read_context", "read_continuation_context", "read_tool_output"]
    assert json.loads(page["text"])["command"]["content"] == "Continue the original work."
    assert page["authorization"] is False


def test_the_recovery_reader_refuses_a_stopped_turn_and_reads_human_answers_with_the_agent_credential(
        local_env, monkeypatch, tmp_path):
    from shared_context import native_hitl_runtime
    from shared_context.m2_copilot import ToolFailure

    clients, outcome = [], []

    class FakeHitl:
        def __init__(self, url, provider):
            clients.append((url, provider))

    async def human(reader, receipt, query, *, hitl_client):
        outcome.append((query.section, hitl_client))
        return SimpleNamespace(model_dump_json=lambda: "HUMAN-PAGE")

    monkeypatch.setattr(native_hitl_runtime, "NativeHitlClient", FakeHitl)
    monkeypatch.setattr("shared_context.native_continuations.read_continuation_context", human)

    async def sdk(**options):
        tool = recovery_tool(options)
        packet = {"session_id": options["sdk_session_id"], "tool_call_id": "read-human", "arguments": {"section": "human"}}
        outcome.append((await tool.handler(packet)).text)
        options["cancel_requested"].__self__.cancelled = True
        try:
            await tool.handler(packet)
        except ToolFailure as error:
            outcome.append(str(error))
        return await offline_sdk(**options)

    credential = lambda: "OFFLINE-agent"  # noqa: E731
    recover(local_env, monkeypatch, tmp_path, sdk, final="unknown", api_token_provider=credential)
    assert clients == [("https://api.offline.invalid", credential)]
    assert outcome[0][0] == "human" and isinstance(outcome[0][1], FakeHitl)
    assert outcome[1] == "HUMAN-PAGE"
    assert outcome[2] == "原接續回合已停止，保留原UNKNOWN與操作狀態。"


class Delegation:
    def __init__(self):
        self.cancels = 0

    async def read(self, *_args):
        return None

    async def request_cancel(self):
        self.cancels += 1


def test_a_recovery_with_delegation_binds_task_and_its_completion_check(local_env, monkeypatch, tmp_path):
    seen, delegation = [], Delegation()

    async def sdk(**options):
        seen.append(options)
        return await offline_sdk(**options)

    providers = (probe("delegate", "fa-delegate-v1", tools=False, delegation_runtime=delegation),)
    recover(local_env, monkeypatch, tmp_path, sdk, entries=("fa-delegate-v1",), providers=providers)
    options = seen[0]
    assert options["delegation_runtime"] is delegation
    assert sorted(options["native_operations"].bindings) == [
        "execute_operation", "read_context", "read_continuation_context", "read_tool_output", "task"]
    assert options["native_operations"].completion_checks == {"task": delegation.read}
    assert delegation.cancels == 0


async def _false():
    return False


@pytest.mark.parametrize("reason", ["unknown", "cancelled"])
def test_a_recovery_model_call_is_refused_once_the_turn_or_the_root_stopped(local_env, monkeypatch, tmp_path, reason):
    from shared_context.errors import Problem
    from shared_context import native_capabilities

    refused = []

    async def stopped(*_args):
        return True

    async def sdk(**options):
        if reason == "unknown":
            monkeypatch.setattr(native_capabilities.NativeCapabilityContext, "current", lambda self: _false())
        else:
            monkeypatch.setattr("shared_context.native_continuations.stop_requested", stopped)
        try:
            await options["behavior_boundary"].before_request("OFFLINE-digest")
        except Problem as error:
            refused.append((error.status, error.body["error"]["code"]))
        return await offline_sdk(**options)

    recover(local_env, monkeypatch, tmp_path, sdk)
    assert refused == [(409, "NATIVE_TURN_UNKNOWN" if reason == "unknown" else "NATIVE_TURN_CANCELLED")]


def test_a_progress_observer_that_fails_is_dropped_not_fatal(responses, monkeypatch, tmp_path, caplog):
    from shared_context import native_progress

    def broken(self, value):
        raise RuntimeError("OFFLINE observer failure")

    monkeypatch.setattr(native_progress.NativeProgressRecorder, "observe", broken)

    async def sdk(**options):
        options["on_event"]({"type": "OFFLINE-event"})
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk)
    caplog.set_level(logging.WARNING, logger=native_sdk.__name__)
    run_turn(responses, submit())
    assert "FA_NATIVE_PROGRESS_DROPPED exception_type=RuntimeError" in caplog.text


def test_a_stop_before_the_abort_watcher_polls_still_asks_the_delegate_to_cancel(responses, monkeypatch, tmp_path):
    delegation = Delegation()

    async def sdk(**options):
        await asyncio.sleep(0.01)  # after the watcher's first look (it polls every 0.05 s), before the turn ends
        options["cancel_requested"].__self__.cancelled = True
        raise RuntimeError("OFFLINE stop after the watcher polled")

    request = NativeSubmit(command=submit().command, capture=selected_capture("fa-delegate-v1"))
    providers = (probe("delegate", "fa-delegate-v1", tools=False, delegation_runtime=delegation),)
    install_executor(responses, monkeypatch, tmp_path, sdk, capability_providers=providers)
    assert responses.invoke(request).status_code == 202
    assert responses.wait("rid-1", "unknown").failure_code == "NATIVE_TURN_CANCELLED"
    assert delegation.cancels == 1


class FakeRoutines:
    """Replaces NativeRoutineClient, which would otherwise call the Agent API."""

    made = []
    reminders_reply = None

    def __init__(self, url, provider):
        FakeRoutines.made.append((url, provider))

    async def reminders(self, conversation_id):
        if isinstance(FakeRoutines.reminders_reply, Exception):
            raise FakeRoutines.reminders_reply
        return FakeRoutines.reminders_reply

    async def check(self, source):
        return None


def reminders(agent_id, status="unknown"):
    from shared_context.native_routine_wire import NativeWorkReminders

    return NativeWorkReminders.model_validate_json(json.dumps({"reminders": [{
        "routine_id": "offline-routine", "occurrence_id": "offline-occurrence", "scheduled_for": "2026-10-04T01:00:00Z",
        "agent_id": agent_id, "conversation_id": "offline-cid", "request_id": "earlier-rid", "status": status}],
        "next_after": None}))


@pytest.mark.parametrize("owner", ["own", "other"])
def test_reminders_use_the_agent_credential_and_only_the_agents_own_work(responses, monkeypatch, tmp_path, owner):
    from shared_context import native_routine_client

    FakeRoutines.made, prompts = [], []
    FakeRoutines.reminders_reply = reminders(IDENTITY.agent_id if owner == "own" else "other-agent")
    monkeypatch.setattr(native_routine_client, "NativeRoutineClient", FakeRoutines)
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")

    async def sdk(**options):
        prompts.append(options["system_message"])
        return await offline_sdk(**options)

    credential = lambda: "OFFLINE-agent"  # noqa: E731
    install_executor(responses, monkeypatch, tmp_path, sdk, api_token_provider=credential)
    assert responses.invoke(submit()).status_code == 202
    if owner == "own":
        receipt = responses.wait("rid-1", "completed")
        assert "## Original work reminders" in prompts[0] and "earlier-rid" in prompts[0]
        assert {part.name: part.state for part in receipt.observed_assembly.parts}["reminders"] == "configured"
    else:
        assert responses.wait("rid-1", "unknown").failure_code == "NATIVE_ROUTINE_SCOPE" and prompts == []
    assert FakeRoutines.made == [("https://api.offline.invalid", credential)]


def watch_related(monkeypatch):
    from shared_context import native_work_context

    seen, original = [], native_work_context.work_context_tool

    def spy(runtime, receipt, control, *, related=(), hitl_client=None):
        seen.append(set(related))
        return original(runtime, receipt, control, related=related, hitl_client=hitl_client)

    monkeypatch.setattr(native_work_context, "work_context_tool", spy)
    return seen


def test_a_scheduled_turn_may_read_the_work_it_names_as_related(harness, monkeypatch, tmp_path):
    from test_native_routine_context import routine_client
    from test_native_routine_turns import routine_request

    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    related = watch_related(monkeypatch)
    install_executor(harness, monkeypatch, tmp_path, routine_client=routine_client())
    assert harness.invoke(submit("human-first")).status_code == 202
    first = harness.wait("human-first", "completed")
    request = routine_request(first.conversation_id)
    request = request.model_copy(update={"routine": request.routine.model_copy(
        update={"related_request_id": "offline-related"})})
    assert harness.invoke(request).status_code == 202
    harness.wait(request.command.request_id, "completed")
    assert related == [set(), {"offline-related"}]


def test_a_recovery_turn_may_read_the_root_and_the_turn_it_continues(local_env, monkeypatch, tmp_path):
    from test_native_routine_context import routine_client

    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    related = watch_related(monkeypatch)
    source, _ = recover(local_env, monkeypatch, tmp_path, offline_sdk, routine_client=routine_client())
    assert related == [{source.request_id}]


def test_routines_without_any_client_report_the_reminders_as_not_configured(responses, monkeypatch, tmp_path):
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    install_executor(responses, monkeypatch, tmp_path)
    receipt = run_turn(responses, submit())
    part = next(part for part in receipt.observed_assembly.parts if part.name == "reminders")
    assert (part.state, part.reason) == ("omitted", "not_configured")


def guarded_names(monkeypatch):
    names, original = [], native_sdk.guard_native_tool
    monkeypatch.setattr(native_sdk, "guard_native_tool", lambda context, spec: names.append(spec.name) or original(context, spec))
    return names


@pytest.mark.parametrize("review", [False, True])
def test_the_turn_tools_are_guarded_only_when_a_human_review_runs(responses, monkeypatch, tmp_path, review):
    import httpx
    from shared_context.native_hitl_runtime import NativeHitlClient

    names = guarded_names(monkeypatch)
    client = NativeHitlClient("https://api.offline.invalid", lambda: "OFFLINE-agent",
                              transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    install_executor(responses, monkeypatch, tmp_path, **({"hitl_client": client} if review else {}))
    run_turn(responses, submit())
    assert ("read_user_turn" in names) is review and names[-1] == "read_tool_output"


def test_the_tools_of_a_scheduled_turn_are_guarded_without_a_human_review(harness, monkeypatch, tmp_path):
    from test_native_routine_context import routine_client
    from test_native_routine_turns import routine_request

    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    names = guarded_names(monkeypatch)
    install_executor(harness, monkeypatch, tmp_path, routine_client=routine_client())
    assert harness.invoke(submit("human-first")).status_code == 202
    first = harness.wait("human-first", "completed")
    human = {part.name: part for part in first.observed_assembly.parts}
    assert "read_user_turn" not in names and human["writing_style"].reason == "no_read_observed"
    request = routine_request(first.conversation_id)
    assert harness.invoke(request).status_code == 202
    scheduled = harness.wait(request.command.request_id, "completed")
    assert "read_user_turn" in names
    assert {part.name: part for part in scheduled.observed_assembly.parts}["writing_style"].reason == "scope_not_applicable"


def test_unreadable_reminders_leave_an_unknown_part_and_a_prompt_note(responses, monkeypatch, tmp_path):
    from shared_context import native_routine_client
    from shared_context.errors import Problem

    FakeRoutines.made, prompts = [], []
    FakeRoutines.reminders_reply = Problem(503, "OFFLINE_UNAVAILABLE", "OFFLINE reminders unavailable")
    monkeypatch.setattr(native_routine_client, "NativeRoutineClient", FakeRoutines)
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")

    async def sdk(**options):
        prompts.append(options["system_message"])
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk, api_token_provider=lambda: "OFFLINE-agent")
    receipt = run_turn(responses, submit())
    part = next(part for part in receipt.observed_assembly.parts if part.name == "reminders")
    assert (part.state, part.reason) == ("unknown", "unavailable")
    assert prompts[0].endswith("Original work reminders could not be read; do not assume earlier work is closed.")


def test_a_model_call_of_a_turn_without_a_continuation_never_asks_whether_a_root_stopped(
        responses, monkeypatch, tmp_path):
    async def stopped(*_args):
        raise AssertionError("only a continuation has a root to ask about")

    monkeypatch.setattr("shared_context.native_continuations.stop_requested", stopped)

    async def sdk(**options):
        await options["behavior_boundary"].before_request("OFFLINE-digest")
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk)
    run_turn(responses, submit())


def test_a_recovery_may_read_the_root_and_the_turn_it_continues_even_when_they_differ(local_env, monkeypatch, tmp_path):
    from test_native_routine_context import routine_client

    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    related = watch_related(monkeypatch)
    entry = native_sdk.NativeSdkExecutor.__call__

    async def deeper(self, receipt, scope, previous, control):
        # As if it continued an earlier recovery; the chain check refuses this root later, after the tool is built.
        forged = receipt.continuation.model_copy(update={"root_request_id": "offline-root"})
        return await entry(self, receipt.model_copy(update={"continuation": forged}), scope, previous, control)

    monkeypatch.setattr(native_sdk.NativeSdkExecutor, "__call__", deeper)
    source, _ = recover(local_env, monkeypatch, tmp_path, offline_sdk, final="unknown", routine_client=routine_client())
    assert related[0] == {"offline-root", source.request_id}


@pytest.mark.parametrize(("listed", "needed"), [
    ((), ("stage_memory_candidate",)),
    (("20000000-0000-0000-0000-000000000002",), ("send_agent_message", "stage_memory_candidate")),
], ids=["no-teammate", "teammate"])
def test_a_missing_directory_removes_only_its_own_send_agent_message_tool(responses, monkeypatch, tmp_path, listed, needed):
    from shared_context.behavior_agent_message_wire import CAPABILITY_ID, AgentMessageRecipient
    from shared_context.behavior_native_agent_wire import NativePeerDirectory
    from shared_context.native_agent_tools import NativeAgentTools

    class Directory:
        async def directory(self, _conversation_id=None):
            return NativePeerDirectory(agents=tuple(
                AgentMessageRecipient(agent_id=agent_id, display_name="OFFLINE peer") for agent_id in listed))

    seen = []

    async def sdk(**options):
        seen.append((options["capability_session"].custom_tools,
                     "send_agent_message" in options["behavior_boundary"].capability_results))
        return await offline_sdk(**options)

    request = NativeSubmit(command=submit().command, capture=selected_capture(CAPABILITY_ID, "fa-memory-candidate-v1"))
    install_executor(responses, monkeypatch, tmp_path, sdk, capability_providers=(probe("memory"),),
                     agent_tools=lambda receipt, control: NativeAgentTools(Directory(), receipt, control))
    run_turn(responses, request)
    assert seen == [(needed, bool(listed))]


def test_the_review_and_the_model_boundary_get_the_turns_inputs_results_and_origin(responses, monkeypatch, tmp_path):
    import httpx
    from shared_context.native_sdk import NativeHistoricalText
    from shared_context.copilot_steering import SdkSteeringChannel
    from shared_context.native_hitl_runtime import NativeHitlClient, NativeHitlRuntime

    seen, closed, close = [], [], NativeHitlRuntime.close

    async def counted(self):
        closed.append(self)
        await close(self)

    monkeypatch.setattr(NativeHitlRuntime, "close", counted)
    monkeypatch.setattr(SdkSteeringChannel, "human_inputs", lambda self: ("OFFLINE append",))

    async def sdk(**options):
        boundary = options["behavior_boundary"]
        seen.append((options["native_hitl"].human_inputs(), boundary.capability_results["read_user_turn"],
                     boundary.native_context, closed[:]))
        return await offline_sdk(**options)

    client = NativeHitlClient("https://api.offline.invalid", lambda: "OFFLINE-agent",
                              transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    install_executor(responses, monkeypatch, tmp_path, sdk, hitl_client=client)
    run_turn(responses, submit())
    [(inputs, historical, native_context, closed_during)] = seen
    assert inputs == ("OFFLINE append",) and historical is NativeHistoricalText and closed_during == []
    assert native_context is (submit().capture.template.settings.turn_origin_profile is not None)
    assert len(closed) == 1


def test_a_resumed_turn_after_a_compaction_starts_with_the_notice(responses, monkeypatch, tmp_path):
    prompts = []

    async def sdk(**options):
        prompts.append(options["prompt"])
        result = await offline_sdk(**options)
        options["session_files"].files["events.jsonl"] += json.dumps({
            "type": "session.compaction_complete", "id": "OFFLINE-compaction", "timestamp": "2026-10-27T00:00:00Z",
            "parentId": None, "data": {"success": True}}) + "\n"
        return result

    install_executor(responses, monkeypatch, tmp_path, sdk)
    first = run_turn(responses, submit("rid-1", text="First."))
    run_turn(responses, submit("rid-2", first.conversation_id, "Second."))
    assert prompts[0] == "First." and prompts[1].startswith("FA note: earlier parts of this conversation were summarized")
    assert prompts[1].endswith("\n\nSecond.")


def test_a_group_turn_declares_its_group_tool_results_to_the_model_boundary(responses, monkeypatch, tmp_path):
    from conftest import InMemoryRepository
    from shared_context.behavior_native_turn_wire import native_turn_root
    from shared_context.native_group_wire import NativeGroupContext, NativeGroupOpen
    from shared_context.native_groups import NativeGroupStore

    class Groups:
        async def context(self, receipt):
            group = NativeGroupStore(InMemoryRepository(), tenant_id="tenant", team_id="team").open(NativeGroupOpen(
                request_id="offline-group", orchestrator_agent_id=receipt.agent_id,
                member_agent_ids=(receipt.agent_id, "20000000-0000-0000-0000-000000000002"), content="OFFLINE question"))
            return NativeGroupContext(group=group, root=native_turn_root(receipt), role="orchestrator")

    seen = []

    async def sdk(**options):
        seen.append(sorted(set(options["behavior_boundary"].capability_results) & {
            "read_group", "share_group_fyi", "next_group_round"}))
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk, group_client=Groups())
    run_turn(responses, submit())
    assert seen == [["next_group_round", "read_group", "share_group_fyi"]]


ROLE_TEXT = "ROLE-TEXT-LITERAL"


def role_capture(*entries):
    """An agent-role v2 capture with role text, which _run adds after the provider addenda."""
    import hashlib
    from shared_context.behavior_agent_role_prompt import RolePromptCapture
    from shared_context.behavior_native_turn_wire import NativeTurnCapture

    values = selected_capture(*entries, profile="fa-instruction-agent-role-v2").model_dump(mode="json")
    values["role_prompt"] = RolePromptCapture(
        version=1, sha256=hashlib.sha256(ROLE_TEXT.encode()).hexdigest(), text=ROLE_TEXT).model_dump(mode="json")
    return NativeTurnCapture.model_validate_json(json.dumps(values))


class FixedMemory:
    async def recall(self, receipt, *, first_turn):
        return ("MEMORY-LITERAL",)

    async def completed(self, receipt, reader=None):
        return None


def test_a_resumed_turn_puts_the_compaction_notice_before_the_recalled_memory(responses, monkeypatch, tmp_path):
    from shared_context.native_private_memory import PREFIX

    prompts = []

    async def sdk(**options):
        prompts.append(options["prompt"])
        result = await offline_sdk(**options)
        options["session_files"].files["events.jsonl"] += json.dumps({
            "type": "session.compaction_complete", "id": "OFFLINE-compaction", "timestamp": "2026-10-27T00:00:00Z",
            "parentId": None, "data": {"success": True}}) + "\n"
        return result

    install_executor(responses, monkeypatch, tmp_path, sdk, private_memory=FixedMemory())
    first = run_turn(responses, submit("rid-1", text="First."))
    run_turn(responses, submit("rid-2", first.conversation_id, "Second."))
    memory = PREFIX + "\n- MEMORY-LITERAL\n\n"
    assert prompts[0] == memory + "First."
    assert prompts[1].startswith("FA note: earlier parts of this conversation were summarized")
    assert prompts[1].endswith("\n\n" + memory + "Second.")


def test_agent_role_v2_text_follows_the_provider_addenda(responses, monkeypatch, tmp_path):
    from shared_context.behavior_agent_role_prompt import ROLE_SECTION

    seen = []

    async def sdk(**options):
        seen.append(options["system_message"])
        return await offline_sdk(**options)

    request = NativeSubmit(command=submit().command, capture=role_capture("fa-memory-candidate-v1"))
    install_executor(responses, monkeypatch, tmp_path, sdk,
                     capability_providers=(probe(system_addendum="ADDENDUM-LITERAL"),))
    run_turn(responses, request)
    [message] = seen
    assert message.endswith("ADDENDUM-LITERAL" + ROLE_SECTION + "\n\n" + ROLE_TEXT)


def test_a_recovery_puts_the_continuation_after_the_agent_role_v2_text(local_env, monkeypatch, tmp_path):
    seen = []

    async def sdk(**options):
        seen.append(options["system_message"])
        return await offline_sdk(**options)

    recover(local_env, monkeypatch, tmp_path, sdk, capture=role_capture())
    [message] = seen
    role_at, continuation_at = message.index("\n\n" + ROLE_TEXT), message.index("## Original work continuation")
    assert role_at < continuation_at and message.count(ROLE_TEXT) == 1
