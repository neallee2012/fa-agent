"""Public SDK activity exposes only bounded safe excerpts, never private reasoning or raw arguments."""
import json
import pytest
from shared_context.behavior_assistant_progress import public_event
from shared_context.behavior_assistant_checkpoint import export,unpack


def test_only_public_sdk_steps_are_projected_and_archived_private_text_is_removed(tmp_path):
    tools={}
    at="2026-01-01T00:00:00+00:00"
    private={"type":"assistant.reasoning_delta","timestamp":at,
             "data":{"delta_content":"PRIVATE_REASONING_CANARY","reasoning_id":"r"}}
    assert public_event(private,1,tools) is None
    first=public_event({"type":"tool.execution_start","timestamp":at,"data":{
        "tool_name":"read_context","tool_call_id":"call-one","arguments":{"secret":"PRIVATE_TOOL_ARGUMENT"}}},1,tools,
        allowed_tools=("read_context",))
    assert first.tool_name=="read_context" and "PRIVATE" not in first.model_dump_json()
    ended=public_event({"type":"tool.execution_complete","timestamp":at,"data":{
        "tool_call_id":"call-one","success":False,"error":{"message":"PRIVATE_ERROR_CANARY"}}},2,tools,
        allowed_tools=("read_context",))
    assert ended.status=="failed" and "PRIVATE" not in ended.model_dump_json()
    directory=tmp_path/"config"/"session-state"/"sdk-original"
    directory.mkdir(parents=True)
    events=[{"type":"session.start","data":{"sessionId":"sdk-original"}},
            {"type":"user.message","data":{"content":"Keep my exact user text."}},
            private,{"type":"assistant.message","data":{
                "content":"Keep the actual public answer.","reasoning_text":"PRIVATE_REASONING_CANARY",
                "system_prompt":"PRIVATE_SYSTEM_CANARY"}}]
    (directory/"events.jsonl").write_text("\n".join(json.dumps(e) for e in events),encoding="utf-8")
    (directory/"workspace.yaml").write_text("OFFLINE SDK workspace\n",encoding="utf-8")
    saved=export(tmp_path,"sdk-original",public_only=True)
    raw=unpack(saved)["events.jsonl"]
    assert "PRIVATE_REASONING_CANARY" not in raw and "PRIVATE_SYSTEM_CANARY" not in raw
    assert "Keep my exact user text." in raw and "Keep the actual public answer." in raw
    assert "PRIVATE_REASONING_CANARY" not in (directory/"events.jsonl").read_text(encoding="utf-8")


AT = "2026-01-01T00:00:00+00:00"


@pytest.mark.parametrize("code", ["MODEL_CALL_LIMIT_REACHED", "ASSISTANT_MODEL_BUDGET", "AGENT_MESSAGE_ROOT_EXPIRED"])
def test_fixed_model_failure_code_is_public_without_raw_error_material(code):
    event = public_event({"type": "m2.provider_failed", "timestamp": AT,
        "data": {"message": "PRIVATE_RESPONSE", "code": "PRIVATE_UNTRUSTED_CODE"}},
        1, {}, model_failure_code=code)
    assert event.kind == "error" and event.status == "failed" and event.summary == code
    assert "PRIVATE" not in event.model_dump_json()


def test_arbitrary_failure_codes_do_not_enter_public_progress():
    event = public_event({"type": "session.error", "timestamp": AT,
        "data": {"message": "PRIVATE_RESPONSE", "code": "MODEL_CALL_LIMIT_REACHED"}},
        1, {}, model_failure_code="PRIVATE_CODE")
    assert event.status == "unknown" and event.summary == "SDK reported an unresolved error"
    assert "PRIVATE" not in event.model_dump_json()


def test_confirmed_peer_pending_code_requires_the_configured_tool_and_hides_raw_errors():
    value = {"type": "m2.tool_failure", "timestamp": AT, "data": {
        "tool_name": "send_agent_message", "code": "AGENT_MESSAGE_RESULT_UNCONFIRMED",
        "tool_call_id": "PRIVATE_CALL", "error": "PRIVATE_DETAIL"}}
    assert public_event(value, 1, {}) is None
    event = public_event(value, 1, {}, allowed_tools=("send_agent_message",))
    assert event.kind == "error" and event.status == "unknown"
    assert event.summary == "AGENT_MESSAGE_RESULT_UNCONFIRMED" and "PRIVATE" not in event.model_dump_json()


def tool_start(name, arguments):
    return {"type": "tool.execution_start", "timestamp": AT, "data": {
        "tool_name": name, "tool_call_id": "PRIVATE_CALL_ID", "arguments": arguments}}


@pytest.mark.parametrize("name,arguments,summary", [
    ("WebIQ-MCP-web", {"query": "Foundry agent tools", "api_key": "PRIVATE_API_KEY", "body": "PRIVATE_FILE"},
     "Search query: Foundry agent tools"),
    ("WebIQ-MCP-browse", {"url": "https://user:PRIVATE_PASSWORD@example.org/docs?token=PRIVATE_TOKEN#PRIVATE_FRAGMENT"},
     "Open URL: https://example.org/docs"),
    ("playwright-browser_navigate", {"url": "https://example.org/page?sig=PRIVATE_SIGNATURE&sv=version"},
     "Open URL: https://example.org/page"),
    ("read_skill", {"name": "formal-report", "content": "PRIVATE_SKILL_BODY"},
     "Skill name: formal-report"),
    ("read_skill_resource", {"name": "formal-report", "resource": "PRIVATE_RESOURCE_PATH", "text": "PRIVATE_FILE"},
     "Skill name: formal-report"),
])
def test_public_tool_argument_summary_uses_only_named_fields(name, arguments, summary):
    event = public_event(tool_start(name, arguments), 1, {}, allowed_tools=(name,))
    assert event.summary == summary
    assert event.kind == "tool_started" and event.tool_start_seq == 1
    assert "PRIVATE_" not in event.model_dump_json()
    assert set(event.model_dump()) == {
        "seq", "at", "kind", "tool_name", "tool_start_seq", "status", "summary", "source"}


@pytest.mark.parametrize("secret", [
    "Bearer PRIVATE_BEARER_TOKEN",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwcml2YXRlIn0.PRIVATE_SIGNATURE",
    'api_key="PRIVATE_KEY with spaces"',
    "API key: PRIVATE_KEY",
    "aws_secret_access_key=PRIVATE_ACCESS_KEY",
    "password is PRIVATE_PASSWORD",
    "AccountKey=PRIVATE_STORAGE_KEY;EndpointSuffix=example.org",
    "ghp_PRIVATE_GITHUB_TOKEN",
    "sk-PRIVATE_PROVIDER_KEY",
    "-----BEGIN PRIVATE KEY-----\nPRIVATE_KEY_BODY\n-----END PRIVATE KEY-----",
])
def test_public_tool_summary_redacts_credentials_before_applying_the_length_bound(secret):
    value = "Find the service documentation " + secret
    event = public_event(tool_start("WebIQ-MCP-web", {"query": value}), 1, {},
                         allowed_tools=("WebIQ-MCP-web",))
    assert "PRIVATE_" not in event.summary and "eyJ" not in event.summary
    assert event.summary.startswith("Search query: Find the service documentation ")
    assert len(event.summary) <= 256


def test_public_summary_is_bounded_and_omits_file_blocks_and_url_credentials_inside_queries():
    value = 'Find https://user:PRIVATE_PASS@example.org/docs?secret=PRIVATE_QUERY#PRIVATE_FRAGMENT ' \
            '```text\nPRIVATE_FILE_CONTENT\n``` ' + "x" * 1000
    event = public_event(tool_start("WebIQ-MCP-web", {"query": value}), 1, {},
                         allowed_tools=("WebIQ-MCP-web",))
    assert event.summary.startswith("Search query: Find https://example.org/docs [CONTENT OMITTED] ")
    assert len(event.summary) == 256 and event.summary.endswith("...")
    assert "PRIVATE_" not in event.model_dump_json()


@pytest.mark.parametrize("arguments", [
    None, "PRIVATE_RAW_ARGUMENTS", ["PRIVATE_RAW_ARGUMENTS"], {"query": {"body": "PRIVATE_RAW_ARGUMENTS"}},
])
def test_unsupported_argument_shapes_keep_the_original_tool_event_without_dumping_values(arguments, caplog):
    event = public_event(tool_start("WebIQ-MCP-web", arguments), 1, {}, allowed_tools=("WebIQ-MCP-web",))
    assert event.summary == "Configured tool call started"
    assert "INSTRUCTION_PUBLIC_TOOL_DETAIL_UNAVAILABLE" in caplog.text
    assert "PRIVATE_" not in event.model_dump_json() + caplog.text


def test_unknown_tools_and_non_http_urls_do_not_gain_a_raw_argument_fallback(caplog):
    event = public_event(tool_start("execute_operation", {"query": "PRIVATE_OPERATION_BODY"}), 1, {},
                         allowed_tools=("execute_operation",))
    assert event.summary == "Configured tool call started"
    assert public_event(tool_start("WebIQ-MCP-web", {"query": "PRIVATE_DENIED"}), 1, {}) is None
    event = public_event(tool_start("WebIQ-MCP-browse", {"url": "file:///PRIVATE_FILE"}), 1, {},
                         allowed_tools=("WebIQ-MCP-browse",))
    assert event.summary == "Open URL: [URL OMITTED]"
    assert "INSTRUCTION_PUBLIC_URL_UNAVAILABLE" in caplog.text
    assert "PRIVATE_" not in event.model_dump_json() + caplog.text


def assistant_message(**overrides):
    return {"type": "assistant.message", "timestamp": AT, "data": {
        "content": "I will check the approved source first.",
        "tool_requests": [{"name": "read_context", "tool_call_id": "PRIVATE_CALL_ID"}],
        "reasoning_text": "PRIVATE_REASONING", "reasoning_opaque": "PRIVATE_OPAQUE",
        "system_prompt": "PRIVATE_SYSTEM", **overrides}}


def test_public_pretool_message_is_an_excerpt_in_the_existing_stage_shape_not_an_answer():
    event = public_event(assistant_message(), 1, {}, allowed_tools=("read_context",))
    assert event.kind == "stage" and event.status == "started"
    assert event.summary == "Assistant note: I will check the approved source first."
    assert set(event.model_dump()) == {"seq", "at", "kind", "tool_name", "status", "summary", "source"}
    assert "PRIVATE_" not in event.model_dump_json()
    commentary = public_event(assistant_message(phase="commentary", tool_requests=None), 1, {})
    assert commentary.summary == event.summary


@pytest.mark.parametrize("changes", [
    {"tool_requests": None},
    {"tool_requests": []},
    {"tool_requests": [{"name": "unconfigured"}]},
    {"phase": "final_answer"},
    {"phase": "analysis"},
    {"content": ""},
    {"content": {"text": "PRIVATE_NON_TEXT"}},
    {"parent_tool_call_id": "PRIVATE_CHILD_ID"},
])
def test_final_unknown_private_phase_or_child_messages_are_not_intermediate_notes(changes):
    assert public_event(assistant_message(**changes), 1, {}, allowed_tools=("read_context",)) is None


@pytest.mark.parametrize("kind", ["assistant.reasoning", "assistant.reasoning_delta", "assistant.message_delta",
                                 "assistant.reasoning_summary"])
def test_private_or_unclassified_streams_cannot_claim_they_are_a_public_summary(kind):
    event = assistant_message(content="PRIVATE_REASONING", phase="commentary", public=True)
    event["type"] = kind
    assert public_event(event, 1, {}, allowed_tools=("read_context",)) is None


def test_public_note_uses_the_same_redaction_and_length_limit():
    content = "First read the docs.\napi_key=PRIVATE_KEY\n```text\nPRIVATE_FILE\n```\n" + "x" * 500
    event = public_event(assistant_message(content=content), 1, {}, allowed_tools=("read_context",))
    assert len(event.summary) == 256 and event.summary.endswith("...")
    assert "PRIVATE_" not in event.model_dump_json()


@pytest.mark.parametrize("label", ["`api_key`", "**api_key**", "__api_key__", "**API key**"])
def test_markdown_credential_labels_cannot_leak_values_in_public_assistant_notes(label):
    value = "0123456789abcdef0123456789abcdef"
    event = public_event(assistant_message(content=f"I will use the service. {label}: {value}"),
                         1, {}, allowed_tools=("read_context",))
    assert event.summary == "Assistant note: I will use the service. [REDACTED]"
    assert value not in event.model_dump_json()


@pytest.mark.parametrize("separator", ["'", '"', "`"])
def test_inline_urls_remove_complete_userinfo_even_when_password_contains_quote_characters(separator):
    url = f"https://synthetic:PRIVATE{separator}PASSWORD@example.org/docs?sig=PRIVATE_QUERY#PRIVATE_FRAGMENT"
    event = public_event(assistant_message(content=f"I will open ({url}).", phase="commentary", tool_requests=None),
                         1, {})
    assert event.summary == "Assistant note: I will open (https://example.org/docs)."
    assert "PRIVATE" not in event.model_dump_json() and "PASSWORD" not in event.model_dump_json()


@pytest.mark.parametrize("url", [
    "https://example.org:PRIVATE_PORT/docs",
    "https://synthetic:PRIVATE'UNFINISHED",
])
def test_unparseable_inline_url_is_omitted_as_a_whole_without_a_credential_tail(url, caplog):
    event = public_event(assistant_message(content=url, phase="commentary", tool_requests=None), 1, {})
    assert event.summary == "Assistant note: [URL OMITTED]"
    assert "INSTRUCTION_PUBLIC_URL_UNAVAILABLE" in caplog.text
    assert "PRIVATE" not in event.model_dump_json() + caplog.text


def test_url_redaction_keeps_a_valid_ipv6_authority_and_port():
    event = public_event(assistant_message(content="Read https://[::1]:8443/docs then https://[::1]",
                                          phase="commentary", tool_requests=None), 1, {})
    assert event.summary == "Assistant note: Read https://[::1]:8443/docs then https://[::1]"


@pytest.mark.parametrize("separator", ["`,`", "`\uFF0C`", ","])
def test_adjacent_urls_are_omitted_instead_of_hiding_credentials_in_another_urls_path(separator, caplog):
    content = f"`https://example.org/{separator}https://synthetic:PRIVATE_PASSWORD@other.example/docs`"
    event = public_event(assistant_message(content=content, phase="commentary", tool_requests=None), 1, {})
    assert "[URL OMITTED]" in event.summary and "PRIVATE_PASSWORD" not in event.summary
    assert "https://" not in event.summary
    assert "INSTRUCTION_PUBLIC_URL_UNAVAILABLE" in caplog.text


def test_separate_urls_are_each_redacted_and_unsupported_schemes_do_not_expose_credentials():
    content = "https://user:PRIVATE_ONE@example.org/ and https://user:PRIVATE_TWO@other.example/ " \
              "or ftp://user:PRIVATE_THREE@files.example/"
    event = public_event(assistant_message(content=content, phase="commentary", tool_requests=None), 1, {})
    assert event.summary == "Assistant note: https://example.org/ and https://other.example/ or [URL OMITTED]"
    assert "PRIVATE_" not in event.model_dump_json()


@pytest.mark.parametrize("prefix,suffix", [("1.", ""), ("12)", ""), ("1.`", "`"), ("\u53c3\u8003\uff1a", "")])
def test_numbered_or_inline_prose_prefixes_never_skip_url_credential_redaction(prefix, suffix):
    content = f"{prefix}https://synthetic:PRIVATE_PASSWORD@example.org/docs?sig=PRIVATE_QUERY{suffix}"
    event = public_event(assistant_message(content=content, phase="commentary", tool_requests=None), 1, {})
    assert event.summary == f"Assistant note: {prefix}https://example.org/docs{suffix}"
    assert "PRIVATE_" not in event.model_dump_json()


def test_fixed_sdk_public_event_types_flow_through_the_existing_emitter(tmp_path, monkeypatch):
    from copilot.session_events import AssistantMessageData, AssistantMessageToolRequest, ToolExecutionStartData
    from shared_context import behavior_assistant_wire as aw
    from shared_context.m2_copilot import _json_value
    from test_instruction_tool_feedback import run_callbacks

    async def unused(_packet):
        pytest.fail("public projection must not execute a tool")

    async def callback(options, _sid):
        message = AssistantMessageData(
            message_id="PRIVATE_MESSAGE_ID", content="I will read the selected skill.",
            reasoning_text="PRIVATE_REASONING",
            tool_requests=[AssistantMessageToolRequest(name="read_skill", tool_call_id="PRIVATE_CALL_ID")])
        started = ToolExecutionStartData(
            tool_name="read_skill", tool_call_id="PRIVATE_CALL_ID",
            arguments={"name": "formal-report", "api_key": "PRIVATE_KEY"})
        for kind, data in (("assistant.message", message), ("tool.execution_start", started)):
            options["on_event"](_json_value({"type": kind, "timestamp": AT, "data": data}))

    events = run_callbacks(tmp_path, monkeypatch, callback, unused, tool_name="read_skill",
                           parameters=aw.SkillName.model_json_schema())
    public = [event["public_progress"] for event in events if "public_progress" in event]
    assert [event["summary"] for event in public] == [
        "Assistant note: I will read the selected skill.", "Skill name: formal-report"]
    assert [event["seq"] for event in public] == [1, 2]
    assert public[1]["tool_start_seq"] == 2
    assert "PRIVATE_" not in json.dumps(events)


def test_queued_peer_notice_uses_the_existing_public_emitter_without_claiming_delivery(tmp_path, monkeypatch):
    from shared_context.m2_copilot import ToolFailure
    from test_instruction_tool_feedback import run_callbacks, invocation

    async def handler(packet):
        packet["report_status"]("助理 B")
        raise ToolFailure("OFFLINE receiver still waiting")

    async def callback(options, sid):
        reply = await options["tools"][0].handler(invocation(sid, {}, "send_agent_message"))
        assert reply.result_type == "failure"

    events = run_callbacks(tmp_path, monkeypatch, callback, handler, tool_name="send_agent_message")
    public = [event["public_progress"] for event in events if "public_progress" in event]
    queued = [event for event in public if "已排隊" in (event.get("summary") or "")]
    assert len(queued) == 1
    assert queued[0]["summary"] == "已排隊，助理 B 換版完成後處理；尚未確認送達，不會重送。"
    assert queued[0]["kind"] == "stage" and queued[0]["status"] == "started"
    assert "已交給" not in json.dumps(events, ensure_ascii=False)


def test_provider_context_limit_is_explicit_without_raw_error_or_retry():
    import asyncio
    import httpx
    import pytest
    from types import SimpleNamespace
    from shared_context.behavior_assistant_gateway import AssistantProvider
    from shared_context.errors import Problem
    sent=[]
    def response(request):
        sent.append(request)
        return httpx.Response(400,json={"error":{"code":"context_length_exceeded","message":"PRIVATE_PROVIDER_ERROR"}})
    provider=AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _:SimpleNamespace(token="OFFLINE")),transport=httpx.MockTransport(response))
    with pytest.raises(Problem) as failed:
        asyncio.run(provider.send({"model":"configured-model","stream":True,"messages":[{"role":"user","content":"x"}]},
                                 text_reply=True,model="configured-model"))
    assert failed.value.body["error"]["code"]=="ASSISTANT_CONTEXT_LIMIT" and len(sent)==1
    assert "PRIVATE_PROVIDER_ERROR" not in str(failed.value.body)
