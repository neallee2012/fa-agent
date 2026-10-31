"""run_copilot's SDK handlers, reached the way the SDK reaches them: from the options the session was created with."""
import asyncio
import inspect
import logging
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from copilot import CopilotClient
from copilot.client import GetStatusResponse
from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.session import CopilotSession

from shared_context import behavior_assistant_progress, live_text, m2_copilot
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_capability_prompt import CapabilitySession
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.m2_copilot import ToolFailure, ToolOutput, ToolSpec
from shared_context.native_hitl_wire import native_hitl_failure_feedback

SID = "handler-contract"
PNG = "iVBORw0KGgo="
NOT_CURRENT = "The work is no longer current; do not retry or claim a result"
ALLOWLIST = "Only the captured organization MCP tools are allowed"
CUSTOM_ONLY = "Only validated registered custom tools are allowed"


class Operations:
    def __init__(self, prepared=None, dispatch_failure=None):
        self.prepared, self.dispatch_failure = prepared, dispatch_failure
        self.sdk_session_id, self.direct, self.refusals, self.observed = None, set(), [], []

    async def prepare(self, _packet):
        if isinstance(self.prepared, ToolFailure):
            raise self.prepared
        return self.prepared

    async def cached_feedback(self, packet):
        return f"cached {packet['tool_call_id']}"

    async def refused(self, packet, reason):
        self.refusals.append((packet["tool_call_id"], reason))

    async def dispatched(self, _packet):
        if self.dispatch_failure is not None:
            raise self.dispatch_failure

    async def invoke(self, packet, handler):
        return await handler(packet)

    def observe(self, value):
        self.observed.append(value)

    async def pre_tool(self, data, context, original):
        return None


class Hitl:
    def __init__(self, answer=(True, None)):
        self.answer, self.asked, self.observed = answer, 0, []

    async def permission(self, _request, _invocation):
        self.asked += 1
        return self.answer

    def observe(self, value):
        self.observed.append(value)

    async def user_input(self, _request, _invocation):
        return {}


class Assembly:
    inference = None

    def __init__(self):
        self.seen = []

    def event(self, value):
        self.seen.append(value)

    def sdk_bound(self, *_args):
        pass


def mcp_request(**changes):
    return SimpleNamespace(**{**dict(
        kind="mcp", tool_name="shot", tool_call_id="call-1", server_name="srv", args={},
        managed_approval_required=False), **changes})


def custom_request(name="lookup", **changes):
    return SimpleNamespace(**{**dict(
        kind="custom-tool", tool_name=name, tool_call_id="call-1", args={"text": "x"},
        managed_approval_required=False), **changes})


def invocation(name="lookup", arguments=None, session_id=SID):
    return SimpleNamespace(tool_name=name, tool_call_id="call-1", session_id=session_id,
                           arguments={"text": "x"} if arguments is None else arguments)


async def current_work():
    return True


@pytest.fixture
def harness(tmp_path, monkeypatch):
    session = create_autospec(CopilotSession, instance=True)
    session.session_id = SID
    session.get_events.return_value = []
    client = create_autospec(CopilotClient, instance=True)
    client.get_status.return_value = GetStatusResponse(version="1.0.83", protocol_version=2)
    client._cli_process = SimpleNamespace(pid=7, returncode=0, poll=lambda: 0, kill=lambda: None)
    monkeypatch.setattr(m2_copilot, "CopilotClient", create_autospec(CopilotClient, return_value=client))
    monkeypatch.setattr(AssistantModelBoundary, "tool_result", lambda _self, name, text: f"{name}: {text}")
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"offline fixture")
    work = tmp_path / "work"
    work.mkdir()
    state = SimpleNamespace(handler=None, events=[])

    async def lookup(_packet):
        return state.handler() if state.handler else ToolOutput("looked up")

    async def snap(_packet):
        return ToolOutput("snapped", png_base64=PNG)

    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "additionalProperties": False}
    tools = [ToolSpec("lookup", "Fixture", parameters, lookup), ToolSpec("snap", "Fixture", {"type": "object"}, snap)]
    capabilities = CapabilitySession(
        custom_tools=("snap",), mcp_servers={}, mcp_tools=("srv-shot",), builtin_tools=(), custom_agents=(),
        vision=False, image_tools=frozenset({"snap"}), permitted_mcp=frozenset({("srv", "shot")}))

    def run(scenario, **overrides):
        captured = {}

        def create(**options):
            captured.update(options)
            return session

        async def send_and_wait(_prompt, **_options):
            captured["result"] = await scenario(captured)

        client.create_session.side_effect = create
        session.send_and_wait.side_effect = send_and_wait
        options = dict(
            state_dir=tmp_path / "sdk", working_directory=work, sdk_session_id=SID, resume=False, prompt="Fixture",
            tools=tools, api_url="https://offline.invalid", token_provider=lambda: "token",
            on_event=state.events.append, timeout_seconds=30, runtime_path=runtime,
            behavior_boundary=AssistantModelBoundary(lambda *_: "token"), instruction=True,
            capability_session=capabilities, capability_current=current_work)
        outcome = asyncio.run(m2_copilot.run_copilot(**{**options, **overrides}))
        return captured["result"], outcome

    state.run = run
    state.capabilities = capabilities
    return state


async def settled(value):
    return await value if inspect.isawaitable(value) else value


def permission(request):
    async def scenario(options):
        return await settled(options["on_permission_request"](request, {"session_id": SID}))
    return scenario


def invoked(call):
    async def scenario(options):
        tool = next(item for item in options["tools"] if item.name == call.tool_name)
        return await tool.handler(call)
    return scenario


def permission_data(observer):
    return [item["data"] for item in observer.observed if item.get("type") == "m2.permission"]


def assert_rejected(decision, feedback):
    assert isinstance(decision, PermissionDecisionReject) and decision.feedback == feedback


@pytest.mark.parametrize("failure", [False, True], ids=["not-current", "check-raises"])
def test_mcp_call_that_is_no_longer_current_never_reaches_review(harness, failure):
    async def current():
        if failure:
            raise RuntimeError("private")
        return False

    operations, hitl = Operations(), Hitl()
    decision, _ = harness.run(permission(mcp_request()), capability_current=current,
                              native_operations=operations, native_hitl=hitl)
    assert_rejected(decision, NOT_CURRENT)
    assert hitl.asked == 0 and operations.direct == set()
    assert permission_data(hitl)[-1]["checks"]["work_current"] is False


def test_cached_mcp_operation_is_refused_with_its_saved_result(harness):
    hitl = Hitl()
    decision, _ = harness.run(permission(mcp_request()), native_operations=Operations("cached"), native_hitl=hitl)
    assert_rejected(decision, "cached call-1")
    assert hitl.asked == 0


def test_mcp_prepare_failure_refuses_before_the_human_is_asked(harness):
    hitl = Hitl()
    decision, _ = harness.run(permission(mcp_request()),
                              native_operations=Operations(ToolFailure("prepare refused")), native_hitl=hitl)
    assert_rejected(decision, "prepare refused")
    assert hitl.asked == 0


def test_mcp_call_the_human_refuses_is_recorded_and_not_dispatched(harness):
    operations, hitl = Operations(), Hitl((False, "denied"))
    decision, _ = harness.run(permission(mcp_request()), native_operations=operations, native_hitl=hitl)
    assert_rejected(decision, native_hitl_failure_feedback("denied"))
    assert operations.refusals == [("call-1", "denied")] and operations.direct == set()
    assert permission_data(hitl)[-1]["checks"]["hitl"] == "denied"
    assert permission_data(hitl)[-1]["approved"] is False


def test_mcp_dispatch_failure_refuses_and_leaves_the_call_undirected(harness):
    operations = Operations(dispatch_failure=ToolFailure("dispatch refused"))
    decision, _ = harness.run(permission(mcp_request()), native_operations=operations, native_hitl=Hitl())
    assert_rejected(decision, "dispatch refused")
    assert operations.direct == set()


def test_mcp_call_that_passes_every_review_runs_once_directly(harness):
    operations, hitl = Operations(), Hitl()
    decision, _ = harness.run(permission(mcp_request()), native_operations=operations, native_hitl=hitl)
    assert isinstance(decision, PermissionDecisionApproveOnce)
    assert operations.direct == {"call-1"}
    assert permission_data(hitl)[-1]["checks"]["hitl"] == "allowed"
    assert permission_data(hitl)[-1]["approved"] is True
    assert operations.sdk_session_id == SID


@pytest.mark.parametrize("reviewers", [dict, lambda: {"native_hitl": Hitl()}, lambda: {"native_operations": Operations()}],
                         ids=["no-review", "human-only", "operations-only"])
def test_mcp_call_with_nothing_to_refuse_it_is_approved(harness, reviewers):
    decision, _ = harness.run(permission(mcp_request()), **reviewers())
    assert isinstance(decision, PermissionDecisionApproveOnce)


def test_mcp_refusal_from_a_human_stands_without_operations(harness):
    decision, _ = harness.run(permission(mcp_request()), native_hitl=Hitl((False, "denied")))
    assert_rejected(decision, native_hitl_failure_feedback("denied"))


@pytest.mark.parametrize("request_changes, feedback", [
    ({"tool_name": "other"}, ALLOWLIST),
    ({"tool_call_id": ""}, ALLOWLIST),
    ({"managed_approval_required": True}, ALLOWLIST),
    ({"args": {"fullPage": True}}, m2_copilot.FULL_PAGE_FEEDBACK),
    ({"args": {"filename": "a/b.png"}}, m2_copilot.FILENAME_FEEDBACK),
    ({"args": {"filename": "a\\b.png"}}, m2_copilot.FILENAME_FEEDBACK),
    ({"args": {"filename": ".."}}, m2_copilot.FILENAME_FEEDBACK),
], ids=["not-allowlisted", "no-call-id", "managed-approval", "full-page", "slash", "backslash", "dots"])
def test_mcp_call_failing_a_fixed_check_is_refused_with_its_own_feedback(harness, request_changes, feedback):
    hitl = Hitl()
    decision, _ = harness.run(permission(mcp_request(**request_changes)), native_hitl=hitl)
    assert_rejected(decision, feedback)
    assert hitl.asked == 0


@pytest.mark.parametrize("room, feedback", [
    ("busy", m2_copilot.PARALLEL_FEEDBACK), ("full", m2_copilot.STATE_FULL_FEEDBACK),
])
@pytest.mark.parametrize("request_", [mcp_request(), custom_request("snap", args={})], ids=["mcp", "custom"])
def test_call_with_no_state_room_is_refused_with_the_reason(harness, room, feedback, request_):
    files = create_autospec(SdkSessionFiles, instance=True)
    files.session_id, files.restored, files.model_provider = SID, False, None
    files.checkpoint_profile, files.automatic = None, False
    files.reserve_large_result.return_value = room
    assembly = Assembly()
    decision, _ = harness.run(permission(request_), session_files=files, native_assembly=assembly)
    assert_rejected(decision, feedback)
    data = next(item["data"] for item in assembly.seen if item.get("type") == "m2.permission")
    assert data["approved"] is False
    assert (data["checks"]["state_room"] if request_.kind == "mcp" else data["state_room"]) == room


def test_custom_tool_with_bad_arguments_is_refused_and_reported(harness):
    assembly = Assembly()
    decision, _ = harness.run(permission(custom_request(args={"text": 1})), native_assembly=assembly)
    assert isinstance(decision, PermissionDecisionReject)
    types = [item.get("type") for item in assembly.seen]
    assert "m2.tool_failure" in types and types.index("m2.tool_failure") < types.index("m2.permission")


@pytest.mark.parametrize("request_", [
    custom_request("missing"), custom_request(kind="shell"), custom_request(tool_call_id=""),
    custom_request(managed_approval_required=True),
], ids=["unregistered", "wrong-kind", "no-call-id", "managed-approval"])
def test_custom_request_outside_the_registered_tools_is_refused(harness, request_):
    decision, _ = harness.run(permission(request_))
    assert_rejected(decision, CUSTOM_ONLY)


def test_cached_custom_operation_is_approved_without_asking_the_human(harness):
    hitl = Hitl()
    decision, _ = harness.run(permission(custom_request()), native_operations=Operations("cached"), native_hitl=hitl)
    assert isinstance(decision, PermissionDecisionApproveOnce)
    assert hitl.asked == 0


def test_custom_prepare_failure_refuses_with_its_message(harness):
    decision, _ = harness.run(permission(custom_request()), native_operations=Operations(ToolFailure("refused")))
    assert_rejected(decision, "refused")


def test_custom_call_the_human_refuses_is_recorded(harness):
    operations, hitl = Operations(), Hitl((False, "denied"))
    decision, _ = harness.run(permission(custom_request()), native_operations=operations, native_hitl=hitl)
    assert_rejected(decision, native_hitl_failure_feedback("denied"))
    assert operations.refusals == [("call-1", "denied")]
    assert permission_data(hitl)[-1]["hitl"] == "denied"


def test_custom_call_without_a_human_step_is_approved_through_operations(harness):
    operations = Operations()
    decision, _ = harness.run(permission(custom_request()), native_operations=operations)
    assert isinstance(decision, PermissionDecisionApproveOnce)
    assert permission_data(operations)[-1]["hitl"] == "allowed"


def test_unbound_custom_tool_approval_without_review_emits_the_decision(harness):
    assembly = Assembly()
    decision, _ = harness.run(permission(custom_request()), native_assembly=assembly)
    assert isinstance(decision, PermissionDecisionApproveOnce)
    assert any(item.get("type") == "m2.permission" and item["data"]["approved"] is True for item in assembly.seen)


def test_tool_returning_the_wrong_type_fails_without_details(harness):
    harness.handler = lambda: "plain string"
    result, _ = harness.run(invoked(invocation()))
    assert result.result_type == "failure"


def test_image_is_refused_from_a_tool_without_image_capability(harness):
    harness.handler = lambda: ToolOutput("shot", png_base64=PNG)
    result, _ = harness.run(invoked(invocation()))
    assert result.result_type == "failure"


def test_image_tool_returns_its_png_to_the_sdk(harness):
    result, _ = harness.run(invoked(invocation("snap", {})))
    assert result.result_type != "failure"
    assert result.text_result_for_llm == "snap: snapped"
    assert [item.mime_type for item in result.binary_results_for_llm] == ["image/png"]


def test_plain_tool_result_passes_through_the_boundary(harness):
    result, _ = harness.run(invoked(invocation()))
    assert (result.result_type, result.text_result_for_llm) != ("failure", "")
    assert result.text_result_for_llm == "lookup: looked up"
    assert not result.binary_results_for_llm


def test_image_that_is_not_a_png_is_refused(harness):
    async def snap(_packet):
        return ToolOutput("snapped", png_base64="AAAA")

    parameters = {"type": "object"}
    result, _ = harness.run(invoked(invocation("snap", {})), tools=[
        ToolSpec("lookup", "Fixture", parameters, snap), ToolSpec("snap", "Fixture", parameters, snap)])
    assert result.result_type == "failure"


def test_tool_call_from_another_session_is_not_run(harness):
    harness.handler = lambda: pytest.fail("handler must not run")
    result, _ = harness.run(invoked(invocation(session_id="other")))
    assert result.result_type == "failure"


@pytest.mark.parametrize("name, code", [
    ("read_skill_resource", "SKILL_RESOURCE_ARGUMENTS"), ("send_agent_message", "AGENT_MESSAGE_ARGUMENTS"),
])
def test_malformed_arguments_of_catalog_tools_log_their_fixed_code(harness, name, code, caplog):
    async def handler(_packet):
        return ToolOutput("unused")
    parameters = {"type": "object", "required": ["text"]}
    tools = [ToolSpec(name, "Fixture", parameters, handler), ToolSpec("snap", "Fixture", {"type": "object"}, handler)]
    with caplog.at_level(logging.WARNING):
        result, _ = harness.run(invoked(invocation(name, {})), tools=tools)
    assert result.result_type == "failure"
    assert f'"code": "{code}"' in caplog.text


def test_argument_errors_name_at_most_three_schema_rules(harness, caplog):
    async def handler(_packet):
        return ToolOutput("unused")
    parameters = {"type": "object", "required": ["a", "b"],
                  "properties": {"c": {"type": "string"}, "d": {"type": "string"}}}
    tools = [ToolSpec("lookup", "Fixture", parameters, handler), ToolSpec("snap", "Fixture", {"type": "object"}, handler)]
    result, _ = harness.run(invoked(invocation("lookup", {"c": 1, "d": 2})), tools=tools, behavior_boundary=None,
                            instruction=False, capability_session=None, capability_current=None)
    assert result.error.count("#/") == 3
    assert "; expected string" in result.error


def test_latest_tool_image_reaches_the_image_callback(harness):
    images = []

    async def scenario(options):
        options["on_event"]({
            "type": "tool.execution_complete", "data": {"success": True, "result": {"binary_results_for_llm": [
                {"mime_type": "image/png", "data": "first"}, {"mime_type": "image/jpeg", "data": "other"},
                {"mime_type": "image/png", "data": "last"}]}}})
        options["on_event"]({"type": "tool.execution_complete", "data": {"success": False, "result": {
            "binary_results_for_llm": [{"mime_type": "image/png", "data": "failed"}]}}})

    harness.run(scenario, on_tool_image=images.append)
    assert images == ["last"]


def test_unprojectable_event_is_kept_without_public_progress(harness, monkeypatch, caplog):
    def refuse(*_args, **_kwargs):
        raise ValueError("private")

    async def scenario(options):
        options["on_event"]({"type": "fixture.event"})

    monkeypatch.setattr(behavior_assistant_progress, "public_event", refuse)
    with caplog.at_level(logging.WARNING):
        harness.run(scenario)
    assert {"type": "fixture.event"} in harness.events
    assert "INSTRUCTION_PUBLIC_EVENT_UNAVAILABLE" in caplog.text


def test_failed_preview_stops_previews_and_not_the_turn(harness, monkeypatch, caplog):
    calls = []

    def fail(_self, _value):
        calls.append(1)
        raise RuntimeError("private")

    async def scenario(options):
        options["on_event"]({"type": "first"})
        options["on_event"]({"type": "second"})

    monkeypatch.setattr(live_text.LiveText, "observe", fail)
    with caplog.at_level(logging.WARNING):
        _, outcome = harness.run(scenario, native_assembly=Assembly())
    assert len(calls) == 1
    assert [item["type"] for item in harness.events[:2]] == ["first", "second"]
    assert "live_text" not in harness.events[1]
    assert "FA_LIVE_TEXT_STOPPED exception_type=RuntimeError" in caplog.text
    assert outcome["final_message"] == ""


def test_token_is_learned_as_a_secret_and_redacted_from_later_events(harness):
    async def provider():
        return "secret-token"

    async def scenario(options):
        token = await options["provider"]["bearer_token_provider"](None)
        options["on_event"]({"type": "fixture", "data": {"text": f"has {token}", "api_key": "x"}})
        return token

    token, _ = harness.run(scenario, token_provider=provider, behavior_boundary=None, instruction=False,
                           capability_session=None, capability_current=None)
    assert token == "secret-token"
    assert {"type": "fixture", "data": {"text": "has [REDACTED]", "api_key": "[REDACTED]"}} in harness.events


@pytest.mark.parametrize("value", ["", "two\nlines", None], ids=["empty", "newline", "none"])
def test_unusable_token_fails_without_its_value(harness, value):
    async def scenario(options):
        try:
            await options["provider"]["bearer_token_provider"](None)
        except RuntimeError as error:
            return str(error)

    message, _ = harness.run(scenario, token_provider=lambda: value, behavior_boundary=None, instruction=False,
                             capability_session=None, capability_current=None)
    assert message == "Context API token unavailable"
