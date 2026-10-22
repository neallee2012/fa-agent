"""Hosted C2/C3 providers bound by C0's real ``bind_capabilities`` against the real native capability gateway.

Offline transport only: no SDK, model, network or MCP server runs.
"""

import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import pytest

from shared_context import behavior_capability_calls, mcp_client
from shared_context.behavior_capabilities import (
    BUILTIN_CAPABILITIES, CAPABILITY_TOOL_GUIDANCE, TOOLBOX_UNAVAILABLE, McpToolResult, _grant,
)
from shared_context.native_assembly import NativeAssemblyCollector
from shared_context.behavior_native_capability_wire import NativeCapabilityCall, NativeSkillRead, NativeTurnScope
from shared_context.behavior_sdk_state import NativeCheckpointScope
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure
from shared_context.m3_skills import PurposeSkillReadResult
from shared_context.native_capabilities import NativeCapabilityContext, bind_capabilities, selected_providers
from shared_context.native_capability_tools import REFUSED, UNKNOWN, approved_skills_provider, mcp_api_provider
from shared_context.native_sdk import native_capability_providers
from shared_context.native_turns import NativeTurnControl
from test_agent_runtime import peer_runtime  # noqa: F401
from test_capability_calls import KEY, listed, result
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_agent_api import relay_case  # noqa: F401
from test_native_capability_api import BODY, SKILLS, WEB, skilled, skilled_http, started  # noqa: F401

SDK_SESSION = "native-capability-sdk"


class Upstream:
    """The real gateway behind an offline transport; ``status`` simulates an unanswered API."""

    def __init__(self, case):
        from shared_context.native_capability_api import NativeCapabilityGateway
        gateway = NativeCapabilityGateway(case.base.a, enabled=True)
        self.routes = {"/skill-catalog": (NativeTurnScope, gateway.skill_catalog),
                       "/read-skill": (NativeSkillRead, gateway.read_skill),
                       "/catalog": (NativeTurnScope, gateway.capability_catalog),
                       "/call": (NativeCapabilityCall, gateway.capability_call)}
        self.case, self.calls, self.status = case, [], None
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        suffix = request.url.path.removeprefix("/work-policy/v1/native-capabilities")
        self.calls.append((suffix, json.loads(request.content), request.headers["authorization"]))
        if self.status is not None:
            return httpx.Response(self.status, json={"error": {"code": "BEHAVIOR_DEPENDENCY_UNAVAILABLE"}})
        model, call = self.routes[suffix]
        try:
            value = call(self.case.principal, model.model_validate_json(request.content))
        except Problem as error:
            return httpx.Response(error.status, json={"error": {"code": "BEHAVIOR_ACCESS_DENIED"}})
        return httpx.Response(200, content=value.model_dump_json().encode())


async def agent_token():
    return "agent-token"


def context_for(case, scope, *, token=agent_token):
    receipt = case.remotes[0].records[scope.request_id]
    return NativeCapabilityContext(
        receipt=receipt, control=NativeTurnControl(), root=Path("unused"), turn_root=Path("unused"),
        scope=NativeCheckpointScope(agent_id=receipt.agent_id, conversation_id=receipt.conversation_id,
                                    sdk_session_id=SDK_SESSION),
        runtime=None, model=None, api_url="https://api.example.test", api_token_provider=token,
        model_token_provider=lambda: "model-token",
    )


def bind(loop, context, upstream):
    providers = (approved_skills_provider(transport=upstream.transport),
                 mcp_api_provider(transport=upstream.transport))

    async def run():
        async with AsyncExitStack() as stack:
            return await bind_capabilities(context, providers, stack)
    return {spec.name: (spec, binding.result_schemas[spec.name])
            for binding in loop.run_until_complete(run()) for spec in binding.tools}


def packet(name, arguments, call_id, *, session=SDK_SESSION):
    return {"session_id": session, "tool_call_id": call_id, "tool_name": name, "arguments": arguments}


@pytest.fixture
def offline_mcp(monkeypatch):
    upstream = []

    async def fake_list(entry, lookup, **_):
        assert lookup("websearch-api-key") == KEY
        return listed()

    async def fake_call(entry, lookup, tool, arguments, *, expected_schema_sha256=None, **_):
        upstream.append((tool, arguments, expected_schema_sha256))
        return result()

    def run_without_loop(async_call):
        coroutine = async_call()
        try:
            coroutine.send(None)
        except StopIteration as done:
            return done.value
        raise AssertionError("the offline MCP fake must not await")
    monkeypatch.setenv("FA_CAPABILITY_SECRET_WEBSEARCH_API_KEY", KEY)
    monkeypatch.setattr(mcp_client, "list_tools", fake_list)
    monkeypatch.setattr(mcp_client, "call_tool", fake_call)
    monkeypatch.setattr(behavior_capability_calls, "_run", run_without_loop)
    return upstream


def test_registered_providers_are_the_single_consumers_of_captured_skills_and_mcp_api(skilled):
    case, refs = skilled
    providers = native_capability_providers("https://api.example.test", agent_token)
    chosen = selected_providers(case.remotes[0].records[started(case, (SKILLS, WEB), skills=refs).request_id].capture,
                                providers)
    assert [(provider.approved_skills, [grant.entry_id for grant in grants]) for provider, grants in chosen] == [
        (True, [SKILLS.entry_id]), (False, [WEB.entry_id])]
    refs_without_grant = case.remotes[0].records[started(case, (WEB,), skills=refs, rid="refs-rid").request_id].capture
    with pytest.raises(Problem) as missing_reader:
        selected_providers(refs_without_grant, providers)
    assert missing_reader.value.body["error"]["code"] == "NATIVE_PROFILE_UNSUPPORTED"
    # Only these two providers are checked here; other registered consumers (C1 shell, ...) may accept it.
    shell = case.remotes[0].records[started(
        case, (_grant(BUILTIN_CAPABILITIES["fa-shell-v1"]),), rid="shell-rid").request_id].capture
    with pytest.raises(Problem) as unsupported:
        selected_providers(shell, (approved_skills_provider(), mcp_api_provider()))
    assert unsupported.value.body["error"]["code"] == "NATIVE_PROFILE_UNSUPPORTED"


def test_bound_tools_serve_only_this_rids_captured_skill_and_grant(skilled_http, offline_mcp):
    (case, refs), (loop, _) = skilled_http
    upstream = Upstream(case)
    context = context_for(case, started(case, (SKILLS, WEB), skills=refs), token=lambda: "sync-agent-token")
    tools = bind(loop, context, upstream)
    assert {name: schema for name, (_, schema) in tools.items()} == {
        "read_skill": PurposeSkillReadResult, **{name: McpToolResult for name in WEB.tools}}
    skill = tools["read_skill"][0]
    assert skill.parameters["properties"]["name"]["enum"] == ["receipt-guide"] and BODY not in skill.description
    read = loop.run_until_complete(skill.handler(packet("read_skill", {"name": "receipt-guide"}, "skill-1")))
    assert PurposeSkillReadResult.model_validate_json(read.text).text == BODY
    web = tools[WEB.tools[0]][0]
    first = loop.run_until_complete(web.handler(packet(web.name, {"query": "fa"}, "web-1")))
    again = loop.run_until_complete(web.handler(packet(web.name, {"query": "fa"}, "web-1")))
    assert McpToolResult.model_validate_json(first.text) == result() and first.text == again.text
    assert offline_mcp == [("web", {"query": "fa"}, "1" * 64)]
    receipt = context.receipt
    bodies = [body for suffix, body, _ in upstream.calls if suffix == "/call"]
    assert bodies[0] == {"conversation_id": receipt.conversation_id, "request_id": receipt.request_id,
                         "tool_call_id": "web-1", "tool": WEB.tools[0], "arguments": {"query": "fa"},
                         "input_schema_sha256": "1" * 64}
    assert all(set(body) <= {"conversation_id", "request_id", "name", "tool_call_id", "tool", "arguments",
                             "input_schema_sha256"} for _, body, _ in upstream.calls)
    assert {token for _, _, token in upstream.calls} == {"Bearer sync-agent-token"}
    assert KEY not in json.dumps(upstream.calls)


def test_tools_stop_with_the_turn_and_never_report_a_refusal_or_unknown_as_success(skilled_http, offline_mcp):
    (case, refs), (loop, _) = skilled_http
    upstream = Upstream(case)
    context = context_for(case, started(case, (SKILLS, WEB), skills=refs))
    tools = bind(loop, context, upstream)
    skill, web = tools["read_skill"][0], tools[WEB.tools[0]][0]
    served = len(upstream.calls)
    for spec, arguments in ((skill, {"name": "receipt-guide"}), (web, {"query": "fa"})):
        with pytest.raises(ToolFailure):
            loop.run_until_complete(spec.handler(packet(spec.name, arguments, "other-session", session="old-sdk")))
        context.control.cancelled = True
        with pytest.raises(ToolFailure):
            loop.run_until_complete(spec.handler(packet(spec.name, arguments, "after-stop")))
        context.control.cancelled = False
    assert offline_mcp == [] and len(upstream.calls) == served
    with pytest.raises(ToolFailure) as refused:
        loop.run_until_complete(skill.handler(packet("read_skill", {"name": "not-selected"}, "s")))
    assert refused.value.public_code == REFUSED and REFUSED in CAPABILITY_TOOL_GUIDANCE
    upstream.status = 503
    with pytest.raises(ToolFailure) as unconfirmed:
        loop.run_until_complete(web.handler(packet(web.name, {"query": "fa"}, "web-unknown")))
    assert unconfirmed.value.public_code == UNKNOWN and UNKNOWN in CAPABILITY_TOOL_GUIDANCE
    with pytest.raises(Problem) as listing:
        bind(loop, context_for(case, started(case, (WEB,), rid="unknown-native-rid")), upstream)
    assert listing.value.body["error"]["code"] == UNKNOWN


def test_a_changed_skill_version_or_missing_credential_stops_binding(skilled_http, offline_mcp):
    (case, refs), (loop, _) = skilled_http
    upstream = Upstream(case)
    scope = started(case, (SKILLS,), skills=refs)
    case.skills.approve("2" * 32, "A later approved body.\n")
    with pytest.raises(Problem) as changed:
        bind(loop, context_for(case, scope), upstream)
    assert changed.value.body["error"]["code"] == REFUSED
    with pytest.raises(Problem) as missing:
        bind(loop, context_for(case, scope, token=None), upstream)
    assert missing.value.body["error"]["code"] == "NATIVE_EXECUTION_UNAVAILABLE"


def test_an_empty_skill_selection_binds_read_skill_without_reading_any_skill(skilled_http, offline_mcp):
    (case, _), (loop, _) = skilled_http
    upstream = Upstream(case)
    tools = bind(loop, context_for(case, started(case, (SKILLS,))), upstream)
    skill = tools["read_skill"][0]
    assert '"entries":[]' in skill.description and "enum" not in skill.parameters["properties"]["name"]
    with pytest.raises(ToolFailure) as refused:
        loop.run_until_complete(skill.handler(packet("read_skill", {"name": "receipt-guide"}, "s")))
    assert refused.value.public_code == REFUSED and upstream.calls == []


def test_unserved_and_unreadable_api_answers_log_a_fixed_reason(skilled_http, offline_mcp, caplog):
    import logging
    (case, refs), (loop, _) = skilled_http
    caplog.set_level(logging.WARNING)
    upstream = Upstream(case)
    upstream.status = 503
    with pytest.raises(Problem) as unserved:
        bind(loop, context_for(case, started(case, (WEB,), rid="logged-rid")), upstream)
    assert unserved.value.body["error"]["code"] == UNKNOWN
    assert (f"NATIVE_CAPABILITY_FAILED route=/catalog stage=response_status code={UNKNOWN} reason=http_status=503"
            in caplog.text)
    assert f"FA_NATIVE_CAPABILITY_BIND_FAILED entries=WebSearch-MCP code={UNKNOWN} exception_type=Problem" in caplog.text
    caplog.clear()
    upstream.status = None
    upstream.transport = httpx.MockTransport(lambda request: httpx.Response(200, text="CANARY-NOT-JSON"))
    with pytest.raises(Problem):
        bind(loop, context_for(case, started(case, (SKILLS,), skills=refs, rid="invalid-rid")), upstream)
    assert ("NATIVE_CAPABILITY_FAILED route=/skill-catalog stage=response_decode "
            f"code={UNKNOWN} reason=http_status=200;model_invalid") in caplog.text
    assert "CANARY-NOT-JSON" not in caplog.text and "agent-token" not in caplog.text


def test_a_kept_mcp_api_catalog_skips_the_bind_read_until_a_call_fails_or_it_expires(
        skilled_http, offline_mcp, monkeypatch):
    from shared_context import native_capability_tools
    (case, _), (loop, _) = skilled_http
    upstream = Upstream(case)
    providers = (mcp_api_provider(transport=upstream.transport),)

    def bind_turn(rid):
        async def run():
            async with AsyncExitStack() as stack:
                return await bind_capabilities(context_for(case, started(case, (WEB,), rid=rid)), providers, stack)
        return {spec.name: spec for binding in loop.run_until_complete(run()) for spec in binding.tools}

    def reads():
        return sum(suffix == "/catalog" for suffix, _, _ in upstream.calls)

    first = bind_turn("kept-1")
    second = bind_turn("kept-2")
    assert reads() == 1 and second.keys() == first.keys() == set(WEB.tools)
    web = second[WEB.tools[0]]
    loop.run_until_complete(web.handler(packet(web.name, {"query": "fa"}, "kept-call")))
    calls = [body for suffix, body, _ in upstream.calls if suffix == "/call"]
    # The kept schemas still call with this turn's own scope; the broker re-checks the grant and the digest.
    assert [(body["request_id"], body["input_schema_sha256"]) for body in calls] == [("kept-2", "1" * 64)]
    upstream.status = 409
    with pytest.raises(ToolFailure) as refused:
        loop.run_until_complete(web.handler(packet(web.name, {"query": "fa"}, "refused-call")))
    assert refused.value.public_code == REFUSED
    upstream.status = None
    bind_turn("kept-3")
    bind_turn("kept-4")
    assert reads() == 2
    now = native_capability_tools.monotonic()
    monkeypatch.setattr(native_capability_tools, "monotonic",
                        lambda: now + native_capability_tools.CATALOG_SECONDS + 1)
    bind_turn("kept-5")
    assert reads() == 3

def test_a_kept_skill_catalog_skips_the_bind_read_only_for_the_same_still_approved_versions(skilled_http, offline_mcp):
    """v17: binding approved skills cost about 0.63 s per warm turn (Hosted -> API -> Hosted receipt read-back)."""
    from shared_context.behavior_native_capture import _skills
    (case, refs), (loop, _) = skilled_http
    upstream = Upstream(case)
    providers = (approved_skills_provider(transport=upstream.transport),)

    def bind_turn(rid):
        async def run():
            async with AsyncExitStack() as stack:
                context = context_for(case, started(case, (SKILLS,), skills=refs, rid=rid))
                return await bind_capabilities(context, providers, stack)
        return {spec.name: spec for binding in loop.run_until_complete(run()) for spec in binding.tools}

    def reads():
        return sum(suffix == "/skill-catalog" for suffix, _, _ in upstream.calls)

    first = bind_turn("skills-1")
    second = bind_turn("skills-2")
    assert reads() == 1 and second["read_skill"].description == first["read_skill"].description
    read = loop.run_until_complete(second["read_skill"].handler(packet("read_skill", {"name": "receipt-guide"}, "s-1")))
    assert PurposeSkillReadResult.model_validate_json(read.text).text == BODY
    # A later approval: the next submit's capture is refused before any turn is admitted ...
    case.skills.approve("2" * 32, "A later approved body.\n")
    template = case.sender.capture.template.model_copy(update={"skills": refs})
    with pytest.raises(Problem) as submit_refused:
        _skills(case.base.a.assistant.authority, case.base.a.runtime.work_policy.policy, template)
    assert submit_refused.value.body["error"]["code"] == "PROFILE_SELECTION_SOURCES_CHANGED"
    # ... and a turn already holding the kept catalog cannot read the old version, which drops the kept catalog.
    with pytest.raises(ToolFailure) as refused:
        loop.run_until_complete(second["read_skill"].handler(packet("read_skill", {"name": "receipt-guide"}, "s-2")))
    assert refused.value.public_code == REFUSED
    with pytest.raises(Problem) as changed:
        bind_turn("skills-3")
    assert changed.value.body["error"]["code"] == REFUSED and reads() == 2


def failing_list(monkeypatch, status, code):
    async def failed(entry, lookup, **_):
        raise Problem(status, code, "the capability service did not list its tools", capability=entry.entry_id)
    monkeypatch.setattr(mcp_client, "list_tools", failed)


def test_an_mcp_api_grant_that_cannot_list_leaves_only_itself_out_of_the_turn(
        skilled_http, offline_mcp, monkeypatch, caplog):
    # dev 2026-10-22: WebIQ rejected the key, the API answered 503 CAPABILITY_NEEDS_AUTH and every turn was refused.
    (case, refs), (loop, _) = skilled_http
    caplog.set_level(logging.WARNING)
    failing_list(monkeypatch, 503, "CAPABILITY_NEEDS_AUTH")
    upstream = Upstream(case)
    context = context_for(case, started(case, (SKILLS, WEB), skills=refs, rid="needs-auth-rid"))
    context.assembly = NativeAssemblyCollector(context.receipt)
    tools = bind(loop, context, upstream)
    assert set(tools) == {"read_skill", *WEB.tools}
    read = loop.run_until_complete(tools["read_skill"][0].handler(packet("read_skill", {"name": "receipt-guide"}, "s")))
    assert PurposeSkillReadResult.model_validate_json(read.text).text == BODY
    web = tools[WEB.tools[0]][0]
    assert web.description == ("Unavailable this turn: WebSearch-MCP could not list its tools. "
                               "Calling it only reports that.")
    with pytest.raises(ToolFailure) as unavailable:
        loop.run_until_complete(web.handler(packet(web.name, {"query": "fa"}, "web-1")))
    assert unavailable.value.public_code == TOOLBOX_UNAVAILABLE
    assert offline_mcp == [] and not any(suffix == "/call" for suffix, _, _ in upstream.calls)
    assert [item.model_dump() for item in context.assembly.unavailable_sources.values()] == [{
        "entry_id": "WebSearch-MCP", "source": "WebSearch-MCP", "code": "TOOLBOX_LIST_FAILED", "reason": "upstream_auth"}]
    lines = [record.getMessage() for record in caplog.records]
    assert "FA_CAPABILITY_LIST_UNAVAILABLE entry=WebSearch-MCP code=CAPABILITY_NEEDS_AUTH" in lines
    assert (f"FA_NATIVE_TOOL_SOURCE_UNAVAILABLE agent_id={context.receipt.agent_id} request_id=needs-auth-rid "
            "entry=WebSearch-MCP code=TOOLBOX_LIST_FAILED reason=upstream_auth") in lines
    assert not any(line.startswith("FA_NATIVE_CAPABILITY_BIND_FAILED") for line in lines), lines
    assert KEY not in caplog.text
    # Nothing is kept: the next turn lists the grant again and binds it as listed.
    async def listed_again(entry, lookup, **_):
        return listed()
    monkeypatch.setattr(mcp_client, "list_tools", listed_again)
    again = bind(loop, context_for(case, started(case, (WEB,), rid="listed-again-rid")), upstream)
    assert sum(suffix == "/catalog" for suffix, _, _ in upstream.calls) == 2
    assert again[WEB.tools[0]][0].description != web.description


@pytest.mark.parametrize("status,code,refused", [
    (403, "CAPABILITY_EXECUTION_DENIED", REFUSED),
    (503, "CAPABILITY_TOOL_UNAVAILABLE", UNKNOWN),
    (502, "CAPABILITY_SCHEMA_INVALID", UNKNOWN),
])
def test_a_definite_mcp_api_listing_refusal_still_refuses_the_turn(
        skilled_http, offline_mcp, monkeypatch, status, code, refused):
    (case, refs), (loop, _) = skilled_http
    failing_list(monkeypatch, status, code)
    with pytest.raises(Problem) as failed:
        bind(loop, context_for(case, started(case, (SKILLS, WEB), skills=refs, rid="refused-rid")), Upstream(case))
    assert failed.value.body["error"]["code"] == refused


def test_an_mcp_api_scope_refusal_still_refuses_the_turn(skilled_http, offline_mcp):
    # The route's own 403 (team boundary, scope) is not a listing that merely failed.
    (case, _), (loop, _) = skilled_http
    upstream = Upstream(case)
    upstream.status = 403
    with pytest.raises(Problem) as failed:
        bind(loop, context_for(case, started(case, (WEB,), rid="scope-rid")), upstream)
    assert failed.value.body["error"]["code"] == REFUSED
