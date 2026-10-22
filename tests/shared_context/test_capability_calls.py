"""P3b: API capability routes (schemas, read-only calls, same-process dedupe) and gateway options from grants."""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from shared_context import mcp_client
from shared_context.behavior_capabilities import (
    CapabilityCapture, McpToolList, McpToolResult, McpToolSchema, available_grants, environment_secret,
    secret_environment_name,
)
from shared_context.behavior_capability_calls import (
    CapabilityCall, _Once, capability_call, capability_catalog, gateway_options, list_grant_tools,
)
from shared_context.errors import Problem
from shared_context.tool_catalog import build_catalog, parse_entry_file
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key

KEY = "Zx9Qm2Lp7Rt4Vw8Ky3Nb6Hc1Jd5Fg0Sa"
# A fictional mcp-api entry: the generic broker's coverage never depends on a shipped catalog file.
WEBIQ = parse_entry_file("websearch.json", json.dumps({"mcpServers": {"WebSearch-MCP": {
    "type": "http", "url": "https://websearch.example.test/v3/mcp",
    "headers": {"x-apikey": "${secret:websearch-api-key}"}, "tools": ["web", "browse"]}}}).encode())
CATALOG = build_catalog([WEBIQ])
GRANTS = available_grants(CATALOG, ("fa-desktop-v1",))


def session(*names, job="job-1", sid="a" * 64):
    capture = CapabilityCapture(head_selection_id=None, grants=tuple(GRANTS[name] for name in names)) if names else None
    task = SimpleNamespace(instruction=SimpleNamespace(capabilities=capture))
    return SimpleNamespace(binding_id="binding-1", assignment=SimpleNamespace(task=task, session_id=sid),
                           execution=SimpleNamespace(job_id=job))


def authority(catalog=CATALOG, denied=False):
    def authorize(binding_id, principal, action):
        if denied:
            raise Problem(403, "BEHAVIOR_CURRENT_POLICY_DENIED", "revoked")
    return SimpleNamespace(runtime=SimpleNamespace(authorize=authorize, capability_catalog=catalog))


def listed():
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    return McpToolList(entry_id="WebSearch-MCP", entry_sha256=WEBIQ.entry_sha256, tools=tuple(
        McpToolSchema(name=name, description=name, input_schema=schema, input_schema_sha256="1" * 64)
        for name in ("web", "browse")))


def result(text="found"):
    return McpToolResult(entry_id="WebSearch-MCP", entry_sha256=WEBIQ.entry_sha256, tool="web",
                         is_error=False, text=text, truncated=False)


def call(tool_call_id="call-1", tool="WebSearch-MCP-web", **extra):
    return CapabilityCall(request_id="capability-call", tool_call_id=tool_call_id, tool=tool,
                          arguments={"query": "fa"}, input_schema_sha256="1" * 64, **extra)


def test_gateway_options_come_only_from_captured_grants():
    assert gateway_options(session()) == ({}, False)
    options, images = gateway_options(session("WebSearch-MCP"))
    assert options == {"capability_tools": frozenset({"WebSearch-MCP-web", "WebSearch-MCP-browse"})} and images is False
    options, images = gateway_options(session("WebSearch-MCP", "fa-desktop-v1"))
    assert options["allow_images"] is True and images is True and "desktop_observe" in options["capability_tools"]
    legacy = SimpleNamespace(assignment=SimpleNamespace(task=SimpleNamespace(instruction=None)))
    assert gateway_options(legacy) == ({}, False)


def test_catalog_lists_only_current_api_grants(monkeypatch):
    seen = []

    async def fake_list(entry, lookup, **_):
        seen.append(entry.entry_id)
        assert lookup is environment_secret
        return listed()
    monkeypatch.setattr(mcp_client, "list_tools", fake_list)
    out = capability_catalog(authority(), session("WebSearch-MCP", "fa-desktop-v1"), None, None)
    assert [item.entry_id for item in out.entries] == ["WebSearch-MCP"] and seen == ["WebSearch-MCP"]
    assert capability_catalog(authority(), session("fa-desktop-v1"), None, None).entries == ()
    with pytest.raises(Problem) as error:
        capability_catalog(authority(catalog=build_catalog([])), session("WebSearch-MCP"), None, None)
    assert error.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    with pytest.raises(Problem):
        capability_catalog(authority(denied=True), session("WebSearch-MCP"), None, None)


def test_call_is_limited_to_the_grant_and_deduplicated_per_tool_call(monkeypatch, caplog):
    calls = []

    async def fake_call(entry, lookup, tool, arguments, *, expected_schema_sha256=None, **_):
        calls.append((tool, arguments, expected_schema_sha256))
        return result()
    monkeypatch.setattr(mcp_client, "call_tool", fake_call)
    once = _Once()
    with caplog.at_level(logging.INFO, logger="shared_context.behavior_capability_calls"):
        first = capability_call(authority(), session("WebSearch-MCP"), call(), None, once=once)
        again = capability_call(authority(), session("WebSearch-MCP"), call(), None, once=once)
    assert first == again == result() and calls == [("web", {"query": "fa"}, "1" * 64)]
    capability_call(authority(), session("WebSearch-MCP"), call("call-2"), None, once=once)
    capability_call(authority(), session("WebSearch-MCP", sid="b" * 64), call(), None, once=once)
    assert len(calls) == 3
    logged = [r for r in caplog.records if "FA_CAPABILITY_CALL" in r.getMessage()]
    assert logged and all(r.levelno == logging.WARNING for r in logged)
    records = [json.loads(r.getMessage().split(" ", 1)[1]) for r in logged]
    assert records[0] == {"capability": "WebSearch-MCP", "counts_against_shared_budget": False, "job_id": "job-1",
                          "outcome": "ok", "session_id": "a" * 64, "tool": "web", "tool_call_id": "call-1"}
    assert KEY not in caplog.text

    for tool, code in (("desktop_observe", "CAPABILITY_TOOL_DENIED"), ("WebSearch-MCP-images", "CAPABILITY_TOOL_DENIED")):
        with pytest.raises(Problem) as error:
            capability_call(authority(), session("WebSearch-MCP", "fa-desktop-v1"), call("x", tool=tool), None, once=once)
        assert error.value.body["error"]["code"] == code
    with pytest.raises(Problem) as error:
        capability_call(authority(), session("WebSearch-MCP"), CapabilityCall(
            request_id="capability-call", tool_call_id="big", tool="WebSearch-MCP-web",
            arguments={"query": "x" * 20000}, input_schema_sha256="1" * 64), None, once=once)
    assert error.value.body["error"]["code"] == "CAPABILITY_ARGUMENTS"
    assert len(calls) == 3


def test_concurrent_duplicates_call_the_service_once_and_share_errors(monkeypatch):
    started, release, calls = threading.Event(), threading.Event(), []

    async def slow_fail(entry, lookup, tool, arguments, **_):
        calls.append(tool)
        started.set()
        release.wait(5)
        raise Problem(503, "CAPABILITY_NEEDS_AUTH", "credential missing")
    monkeypatch.setattr(mcp_client, "call_tool", slow_fail)
    once, errors = _Once(), []

    def worker():
        try:
            capability_call(authority(), session("WebSearch-MCP"), call("dup"), None, once=once)
        except Problem as error:
            errors.append(error.body["error"]["code"])
    threads = [threading.Thread(target=worker) for _ in range(3)]
    threads[0].start()
    started.wait(5)
    for thread in threads[1:]:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(5)
    assert calls == ["web"] and errors == ["CAPABILITY_NEEDS_AUTH"] * 3


def test_inflight_tool_keeps_original_id_when_other_calls_fill_the_cache(monkeypatch):
    from shared_context import behavior_capability_calls
    monkeypatch.setattr(behavior_capability_calls, "MAX_REMEMBERED_CALLS", 2)
    entered, release = threading.Event(), threading.Event()
    invoked = []

    async def slow(entry, lookup, tool, arguments, **_):
        invoked.append(arguments["query"])
        if arguments["query"] == "original-running":
            entered.set()
            assert release.wait(5)
        return result(arguments["query"])

    monkeypatch.setattr(mcp_client, "call_tool", slow)
    once = _Once()
    original = call("original-id").model_copy(update={"arguments": {"query": "original-running"}})
    with ThreadPoolExecutor(max_workers=2) as clients:
        first = clients.submit(capability_call, authority(), session("WebSearch-MCP"), original, None, once=once)
        try:
            assert entered.wait(5)
            for identity in ("other-1", "other-2"):
                command = call(identity).model_copy(update={"arguments": {"query": identity}})
                capability_call(authority(), session("WebSearch-MCP"), command, None, once=once)
            repeated = clients.submit(capability_call, authority(), session("WebSearch-MCP"), original, None, once=once)
        finally:
            release.set()
        assert first.result(timeout=5) == repeated.result(timeout=5) == result("original-running")
    assert invoked == ["original-running", "other-1", "other-2"]


def test_full_inflight_cache_refuses_new_call_without_losing_original_unknown(monkeypatch):
    from shared_context import behavior_capability_calls
    monkeypatch.setattr(behavior_capability_calls, "MAX_REMEMBERED_CALLS", 1)
    entered, release = threading.Event(), threading.Event()
    invoked = []

    async def lost_reply(entry, lookup, tool, arguments, **_):
        invoked.append(arguments)
        entered.set()
        assert release.wait(5)
        raise TimeoutError("synthetic response loss after tool started")

    monkeypatch.setattr(mcp_client, "call_tool", lost_reply)
    once = _Once()
    with ThreadPoolExecutor(max_workers=1) as clients:
        original = clients.submit(capability_call, authority(), session("WebSearch-MCP"), call(), None, once=once)
        try:
            assert entered.wait(5)
            with pytest.raises(Problem) as refused:
                capability_call(authority(), session("WebSearch-MCP"), call("new-call"), None, once=once)
            assert refused.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE"
            assert invoked == [{"query": "fa"}]
            with pytest.raises(Problem) as changed:
                capability_call(authority(), session("WebSearch-MCP"),
                    call().model_copy(update={"arguments": {"query": "different"}}), None, once=once)
            assert changed.value.body["error"]["code"] == "CAPABILITY_CALL_CONFLICT"
        finally:
            release.set()
        with pytest.raises(Problem) as lost:
            original.result(timeout=5)
    with pytest.raises(Problem) as replayed:
        capability_call(authority(), session("WebSearch-MCP"), call(), None, once=once)
    assert replayed.value is lost.value
    assert invoked == [{"query": "fa"}]


def test_unexpected_failures_are_unknown_and_results_must_match_the_entry(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("socket details " + KEY)
    monkeypatch.setattr(mcp_client, "call_tool", boom)
    with pytest.raises(Problem) as error:
        capability_call(authority(), session("WebSearch-MCP"), call("boom"), None, once=_Once())
    assert error.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE" and KEY not in json.dumps(error.value.body)

    async def other(*args, **kwargs):
        return result().model_copy(update={"entry_sha256": "2" * 64})
    monkeypatch.setattr(mcp_client, "call_tool", other)
    with pytest.raises(Problem) as error:
        capability_call(authority(), session("WebSearch-MCP"), call("other"), None, once=_Once())
    assert error.value.body["error"]["code"] == "CAPABILITY_RESULT_INVALID"


def test_secret_lookup_uses_the_deploy_line_environment_name(monkeypatch):
    assert secret_environment_name("websearch-api-key") == "FA_CAPABILITY_SECRET_WEBSEARCH_API_KEY"
    monkeypatch.delenv("FA_CAPABILITY_SECRET_WEBSEARCH_API_KEY", raising=False)
    assert environment_secret("websearch-api-key") is None
    monkeypatch.setenv("FA_CAPABILITY_SECRET_WEBSEARCH_API_KEY", KEY)
    assert environment_secret("websearch-api-key") == KEY


def test_same_tool_call_id_with_different_content_is_refused_not_resent(monkeypatch):
    calls = []

    async def fake_call(entry, lookup, tool, arguments, **_):
        calls.append(arguments)
        return result()
    monkeypatch.setattr(mcp_client, "call_tool", fake_call)
    once = _Once()
    capability_call(authority(), session("WebSearch-MCP"), call("same-id"), None, once=once)
    for changed in (CapabilityCall(request_id="capability-call", tool_call_id="same-id", tool="WebSearch-MCP-web",
                                   arguments={"query": "other"}, input_schema_sha256="1" * 64),
                    call("same-id", tool="WebSearch-MCP-browse"),
                    CapabilityCall(request_id="capability-call", tool_call_id="same-id", tool="WebSearch-MCP-web",
                                   arguments={"query": "fa"}, input_schema_sha256="2" * 64)):
        with pytest.raises(Problem) as error:
            capability_call(authority(), session("WebSearch-MCP"), changed, None, once=once)
        assert error.value.status == 409 and error.value.body["error"]["code"] == "CAPABILITY_CALL_CONFLICT"
    assert capability_call(authority(), session("WebSearch-MCP"), call("same-id"), None, once=once) == result()
    assert calls == [{"query": "fa"}]


def test_hosted_proxy_reaches_the_api_route_once_per_tool_call_and_keeps_refusal_codes(assistant, monkeypatch):
    from shared_context import behavior_assistant_hosted as hosted
    from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES
    from shared_context.m2_copilot import ToolFailure
    from test_capability_capture import WEB_TOOLS, choose, enabled, turn
    h, control = enabled(assistant, monkeypatch, tools=(*HISTORY_CAPABILITIES, *WEB_TOOLS))
    calls = []

    async def service(entry, lookup, tool, arguments, **_):
        calls.append((tool, arguments))
        if arguments["query"] == "no-key":
            raise Problem(503, "CAPABILITY_NEEDS_AUTH", "credential missing")
        return McpToolResult(entry_id=entry.entry_id, entry_sha256=entry.entry_sha256, tool=tool,
                             is_error=False, text="offline " + arguments["query"], truncated=False)
    monkeypatch.setattr(mcp_client, "call_tool", service)
    original, seen = hosted.run_copilot, []

    async def call_then_reply(**options):
        tool = next(spec for spec in options["tools"] if spec.name == "WebSearch-MCP-web")
        packet = {"session_id": options["sdk_session_id"], "tool_call_id": "proxy-call", "arguments": {"query": "fa"}}
        seen.append((await tool.handler(packet)).text)
        seen.append((await tool.handler(packet)).text)
        with pytest.raises(ToolFailure) as refused:
            await tool.handler({**packet, "tool_call_id": "proxy-no-key", "arguments": {"query": "no-key"}})
        seen.append(refused.value.public_code)
        return await original(**options)
    monkeypatch.setattr(hosted, "run_copilot", call_then_reply)
    choice, _ = choose(h, "choose-web", ("WebSearch-MCP",))
    assert choice.status_code == 200, choice.text
    turn(h, control, "proxy-call")
    assert calls == [("web", {"query": "fa"}), ("web", {"query": "no-key"})]
    assert seen[0] == seen[1] and McpToolResult.model_validate_json(seen[0]).text == "offline fa"
    assert seen[2] == "CAPABILITY_NEEDS_AUTH"


def test_mcp_permission_rechecks_current_work_and_a_cancelled_job_is_refused(assistant, monkeypatch):
    import inspect
    from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
    from shared_context import m2_copilot
    from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES
    from test_capability_capture import choose, enabled
    from test_instruction_prompt import submit
    h, control = enabled(assistant, monkeypatch, tools=HISTORY_CAPABILITIES)
    choice, _ = choose(h, "choose-browser", ("playwright",))
    assert choice.status_code == 200, choice.text
    offline_client, decisions, binding = m2_copilot.CopilotClient, [], {}

    def client(**connection):
        sdk = offline_client(**connection)
        opened = sdk.create_session.side_effect

        async def checked(*args, **options):
            permission = options["on_permission_request"]
            invocation = {"session_id": options["session_id"]}

            def request(call_id, tool="playwright-browser_navigate"):
                return SimpleNamespace(kind="mcp", server_name="playwright", tool_name=tool,
                                       tool_call_id=call_id, managed_approval_required=False)
            outside = permission(request("hidden", "playwright-browser_click"), invocation)
            assert not inspect.isawaitable(outside)
            decisions.append(outside)
            pending = permission(request("current"), invocation)
            assert inspect.isawaitable(pending)
            decisions.append(await pending)
            cancelled = h.client.post(f"/work-policy/v1/bindings/{binding['id']}/cancel",
                                      json={"request_id": "cancel-during-mcp"}, headers=h.headers())
            assert cancelled.status_code == 200, cancelled.text
            decisions.append(await permission(request("after-cancel"), invocation))
            return await opened(*args, **options)
        sdk.create_session.side_effect = checked
        return sdk
    monkeypatch.setattr(m2_copilot, "CopilotClient", client)
    accepted, _ = submit(h, "mcp-current-work", "Open the page.")
    binding["id"] = accepted["binding_id"]
    try:
        h.runtime.controller.step(accepted["binding_id"])
    except Problem:
        pass
    assert [type(item) for item in decisions] == [
        PermissionDecisionReject, PermissionDecisionApproveOnce, PermissionDecisionReject]
    assert "no longer current" in decisions[2].feedback


def test_mcp_tools_cannot_start_without_a_current_work_check(tmp_path):
    import asyncio
    from shared_context.behavior_capability_prompt import session_needs
    from shared_context.m2_copilot import run_copilot
    playwright = parse_entry_file("playwright.json", json.dumps({"mcpServers": {"playwright": {
        "type": "local", "command": "/opt/fa-tools/bin/playwright-mcp", "tools": ["browser_navigate"]}}}).encode())
    grant = available_grants(build_catalog([playwright]))["playwright"]
    needs = session_needs(CapabilityCapture(head_selection_id=None, grants=(grant,)))
    with pytest.raises(ValueError, match="current-work check"):
        asyncio.run(run_copilot(
            state_dir=tmp_path / "state", working_directory=tmp_path, sdk_session_id="no-current-check",
            resume=False, prompt="Open the page.", tools=[], api_url="https://context.invalid",
            token_provider=lambda: "offline", on_event=lambda _: None, capability_session=needs))


def test_builtin_capability_handlers_run_only_while_the_work_is_current():
    import asyncio
    from shared_context import behavior_assistant_hosted as hosted
    from shared_context.m2_copilot import ToolFailure, ToolOutput, ToolSpec
    effects = []

    async def act(packet):
        effects.append(packet["tool_call_id"])
        return ToolOutput("done")
    for current, expected in ((True, ["call-1"]), (False, [])):
        effects.clear()

        async def check(current=current):
            return current
        owner = SimpleNamespace(_capability_current=check)
        wrapped = hosted.AssistantSession._current_first(owner, ToolSpec("desktop_act", "Act.", {"type": "object"}, act))
        if current:
            assert asyncio.run(wrapped.handler({"tool_call_id": "call-1"})).text == "done"
        else:
            with pytest.raises(ToolFailure):
                asyncio.run(wrapped.handler({"tool_call_id": "call-1"}))
        assert effects == expected


def test_hosted_waits_longer_than_the_api_route_bound_so_completed_calls_are_not_reported_unknown(monkeypatch):
    from shared_context import behavior_assistant_hosted as hosted
    from shared_context.behavior_capabilities import CAPABILITY_CLIENT_SECONDS, CAPABILITY_ROUTE_SECONDS
    seen, timeouts = [], []

    def post(action, command, result, **options):
        seen.append((action, options["timeout"]))
    owner = SimpleNamespace(_post=post)
    hosted.AssistantClient.capability_catalog(owner)
    hosted.AssistantClient.call_capability(owner, "call-1", "WebSearch-MCP-web", {"query": "fa"}, "1" * 64)
    assert [action for action, _ in seen] == ["capability-catalog", "capability-call"]
    assert all(timeout.read == CAPABILITY_CLIENT_SECONDS > CAPABILITY_ROUTE_SECONDS for _, timeout in seen)

    async def fake_call(entry, lookup, tool, arguments, *, timeout, **_):
        timeouts.append(timeout)
        return result()
    monkeypatch.setattr(mcp_client, "call_tool", fake_call)
    capability_call(authority(), session("WebSearch-MCP"), call("bounded"), None, once=_Once())
    assert timeouts == [CAPABILITY_ROUTE_SECONDS]


def test_catalog_reads_share_one_route_bound(monkeypatch):
    second = parse_entry_file("other.json", json.dumps({"mcpServers": {"Other-MCP": {
        "type": "http", "url": "https://other.test/mcp", "headers": {"x-apikey": "${secret:other-key}"},
        "tools": ["find"]}}}).encode())
    catalog = build_catalog([WEBIQ, second])
    grants = available_grants(catalog)
    capture = CapabilityCapture(head_selection_id=None, grants=tuple(grants[name] for name in sorted(grants)))
    both = SimpleNamespace(binding_id="binding-1", execution=SimpleNamespace(job_id="job-1"), assignment=SimpleNamespace(
        session_id="a" * 64, task=SimpleNamespace(instruction=SimpleNamespace(capabilities=capture))))
    clock, budgets = [0.0], []

    async def slow_list(entry, lookup, *, timeout, **_):
        budgets.append(timeout)
        clock[0] += 12.0
        return McpToolList(entry_id=entry.entry_id, entry_sha256=entry.entry_sha256, tools=())
    monkeypatch.setattr(mcp_client, "list_tools", slow_list)
    monkeypatch.setattr("shared_context.behavior_capability_calls.time.monotonic", lambda: clock[0])
    capability_catalog(authority(catalog=catalog), both, None, None, seconds=30.0)
    assert budgets == [30.0, 18.0]
    clock[0], budgets[:] = 0.0, []
    with pytest.raises(Problem) as error:
        capability_catalog(authority(catalog=catalog), both, None, None, seconds=10.0)
    assert error.value.body["error"]["code"] == "CAPABILITY_UNAVAILABLE" and budgets == [10.0]


def test_the_native_listing_names_a_grant_that_cannot_list_and_still_lists_the_others(monkeypatch, caplog):
    second = parse_entry_file("other.json", json.dumps({"mcpServers": {"Other-MCP": {
        "type": "http", "url": "https://other.test/mcp", "headers": {"x-apikey": "${secret:other-key}"},
        "tools": ["find"]}}}).encode())
    runtime = SimpleNamespace(capability_catalog=build_catalog([WEBIQ, second]))
    grants = available_grants(runtime.capability_catalog)
    both = tuple(grants[name] for name in sorted(grants))
    failure = {"WebSearch-MCP": ("CAPABILITY_NEEDS_AUTH", 503)}

    async def fake_list(entry, lookup, **_):
        if entry.entry_id in failure:
            code, status = failure[entry.entry_id]
            raise Problem(status, code, f"rejected {KEY}", capability=entry.entry_id)
        return McpToolList(entry_id=entry.entry_id, entry_sha256=entry.entry_sha256, tools=())
    monkeypatch.setattr(mcp_client, "list_tools", fake_list)
    caplog.set_level(logging.WARNING)
    out = list_grant_tools(runtime, both, degrade=True)
    assert [item.entry_id for item in out.entries] == ["Other-MCP"]
    assert [item.model_dump() for item in out.unavailable] == [{"entry_id": "WebSearch-MCP", "reason": "upstream_auth"}]
    assert KEY not in caplog.text and KEY not in out.model_dump_json()
    failure["WebSearch-MCP"] = ("CAPABILITY_UNAVAILABLE", 503)
    assert list_grant_tools(runtime, both, degrade=True).unavailable[0].reason == "service_error"
    # A full listing is the same wire as before, so an older Hosted still reads it.
    failure.clear()
    assert '"unavailable"' not in list_grant_tools(runtime, both, degrade=True).model_dump_json()
    # A definite answer still refuses, and the legacy route never degrades.
    for code, status, kwargs in (("CAPABILITY_TOOL_UNAVAILABLE", 503, {"degrade": True}),
                                 ("CAPABILITY_EXECUTION_DENIED", 403, {"degrade": True}),
                                 ("CAPABILITY_NEEDS_AUTH", 503, {})):
        failure["WebSearch-MCP"] = (code, status)
        with pytest.raises(Problem) as refused:
            list_grant_tools(runtime, both, **kwargs)
        assert refused.value.body["error"]["code"] == code


def test_a_browser_grant_on_an_image_without_its_command_is_refused_before_the_sdk_starts(assistant, monkeypatch):
    from shared_context import behavior_assistant_hosted as hosted
    from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES
    from test_capability_capture import choose, enabled
    from test_instruction_prompt import submit
    h, control = enabled(assistant, monkeypatch, tools=HISTORY_CAPABILITIES, image_tools=False)
    choice, _ = choose(h, "choose-browser-on-slim", ("playwright",))
    assert choice.status_code == 200, choice.text
    started = []

    async def never(**options):
        started.append(options)
        raise AssertionError("the SDK must not start without the captured tool's command")
    monkeypatch.setattr(hosted, "run_copilot", never)
    refusals = []
    original = hosted.AssistantSession._run_turn

    async def observed(self, work):
        try:
            return await original(self, work)
        except Problem as error:
            refusals.append(error.body["error"])
            raise
    monkeypatch.setattr(hosted.AssistantSession, "_run_turn", observed)
    accepted, _ = submit(h, "browser-on-slim", "Open the page.")
    try:
        h.runtime.controller.step(accepted["binding_id"])
    except Problem:
        pass
    assert started == [] and [item["code"] for item in refusals] == ["CAPABILITY_UNAVAILABLE"]
    assert refusals[0]["capability"] == "playwright"


def test_an_unexpected_route_failure_logs_its_action_and_exception_type_only(assistant, monkeypatch, caplog):
    from shared_context import behavior_assistant_api as api
    from shared_context import behavior_assistant_hosted as hosted
    from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES
    from shared_context.m2_copilot import ToolFailure
    from test_capability_capture import WEB_TOOLS, choose, enabled, turn
    h, control = enabled(assistant, monkeypatch, tools=(*HISTORY_CAPABILITIES, *WEB_TOOLS))

    def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE_FAILURE_DETAIL " + KEY)
    monkeypatch.setattr(api, "capability_call", broken)
    original, codes = hosted.run_copilot, []

    async def call_then_reply(**options):
        tool = next(spec for spec in options["tools"] if spec.name == "WebSearch-MCP-web")
        with pytest.raises(ToolFailure) as refused:
            await tool.handler({"session_id": options["sdk_session_id"], "tool_call_id": "broken-call",
                                "arguments": {"query": "fa"}})
        codes.append(refused.value.public_code)
        return await original(**options)
    monkeypatch.setattr(hosted, "run_copilot", call_then_reply)
    choice, _ = choose(h, "choose-web", ("WebSearch-MCP",))
    assert choice.status_code == 200, choice.text
    with caplog.at_level(logging.WARNING, logger="shared_context.behavior_assistant_api"):
        turn(h, control, "broken-route")
    logged = [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records
              if r.getMessage().startswith("ASSISTANT_DEPENDENCY_FAILED ")]
    assert logged == [{"action": "capability-call", "exception_type": "RuntimeError"}]
    assert "PRIVATE_FAILURE_DETAIL" not in caplog.text and KEY not in caplog.text and codes
