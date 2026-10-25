"""FA_SDK_TELEMETRY (on by default): pinned SDK/runtime spans reach Application Insights through Microsoft's exporter.

Offline only: synthetic model HTTP, a loopback stand-in for the ingestion endpoint, no Azure call.
The real-runtime cases need the pinned runtime 1.0.83 (``M2_TEST_RUNTIME``), like test_m2_copilot.
"""
import asyncio
import gzip
import json
import logging
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry import context as otel_context, trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from shared_context import behavior_assistant_wire as aw
from shared_context import behavior_model_direct as direct
from shared_context import sdk_telemetry
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.m2_copilot import ToolOutput, ToolSpec, run_copilot
from test_instruction_responses_native import OPAQUE, SUMMARY, response_bytes
from test_m2_copilot import ScriptedEndpoint, runtime_path, workspace  # noqa: F401 (fixtures)
from test_native_responses import responses  # noqa: F401 (fixture)
from test_native_turns import local_env  # noqa: F401 (fixture)

TURN_TRACE, TURN_PARENT = "1f" * 16, "2e" * 8
RAW_REASONING = "RAW_PROVIDER_REASONING_CANARY"
CONNECTOR_SECRET = "CONNECTOR_SECRET_CANARY"
URL = "https://fa-acct.services.ai.azure.com/api/projects/fa-project/openai/v1/responses"


class Ingestion:
    """Loopback stand-in for the Application Insights ingestion endpoint; records envelopes."""

    def __init__(self):
        self.items = []
        received = self.items

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                if self.headers.get("content-encoding") == "gzip":
                    raw = gzip.decompress(raw)
                batch = json.loads(raw)
                received.extend(batch)
                body = json.dumps({"itemsReceived": len(batch), "itemsAccepted": len(batch), "errors": []}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def connection_string(self):
        port = self.server.server_address[1]
        return (f"InstrumentationKey=00000000-0000-0000-0000-000000000000;"
                f"IngestionEndpoint=http://127.0.0.1:{port}/")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def telemetry(monkeypatch):
    monkeypatch.delenv(sdk_telemetry.FLAG, raising=False)  # the default: on wherever App Insights is linked
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "fa-test-agent")
    monkeypatch.setenv("APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL", "true")
    monkeypatch.setenv("APPLICATIONINSIGHTS_SDKSTATS_DISABLED", "true")
    monkeypatch.setenv("FA_TEST_CONNECTOR_TOKEN", CONNECTOR_SECRET)  # Hosted process env; the runtime env is an allowlist
    ingestion = Ingestion()
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", ingestion.connection_string)
    monkeypatch.setattr(sdk_telemetry, "_processor_state", None)
    monkeypatch.setattr(sdk_telemetry, "_remembered", type(sdk_telemetry._remembered)())
    monkeypatch.setattr(sdk_telemetry, "_offsets", {})
    monkeypatch.setattr(sdk_telemetry, "_logged", set())
    monkeypatch.setattr(sdk_telemetry, "_content_suppressed", False)
    try:
        yield ingestion
    finally:
        state = sdk_telemetry._processor_state
        if state:
            state.shutdown()
        ingestion.close()


def spans_of(ingestion):
    """Span envelopes only; span events are separate MessageData items, the resource is a MetricData item."""
    return [item for item in ingestion.items
            if item["data"]["baseType"] in {"RemoteDependencyData", "RequestData"}]


def wait_forwarded(state_dir, ingestion, names, timeout=20):
    deadline = time.monotonic() + timeout
    seen = set()
    while time.monotonic() < deadline:
        seen = {item["data"]["baseData"]["name"] for item in spans_of(ingestion)}
        if not list(state_dir.glob("otel-spans-*.jsonl")) and names <= seen:
            return
        time.sleep(0.1)
    pytest.fail(f"spans not forwarded: {sorted(seen)}")


def ambient(trace_hex, span_hex):
    parent = SpanContext(int(trace_hex, 16), int(span_hex, 16), is_remote=True,
                         trace_flags=TraceFlags(TraceFlags.SAMPLED))
    return otel_context.attach(trace.set_span_in_context(NonRecordingSpan(parent)))


@pytest.mark.parametrize("flag,connection", [("0", True), (None, False), ("1", False), ("true", True)])
def test_explicit_zero_unlinked_project_or_unreadable_flag_change_nothing(monkeypatch, tmp_path, caplog, flag, connection):
    if flag is None:
        monkeypatch.delenv(sdk_telemetry.FLAG, raising=False)
    else:
        monkeypatch.setenv(sdk_telemetry.FLAG, flag)
    if connection:
        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=00000000-0000-0000-0000-000000000000")
    else:
        monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(sdk_telemetry, "_logged", set())
    token = ambient(TURN_TRACE, TURN_PARENT)
    try:
        with caplog.at_level(logging.INFO, logger=sdk_telemetry.log.name):
            assert sdk_telemetry.current_traceparent() is None
            assert sdk_telemetry.client_telemetry(tmp_path, "responses") is None
            with sdk_telemetry.turn_context("rid") as traceparent:
                assert traceparent is None
            assert direct.DirectResponses(URL, lambda: "t", "m").traceparent is None
            untouched = httpx.Response(200, stream=httpx.ByteStream(b"data: {}\n\n"))
            assert sdk_telemetry.watch_model_response(untouched, "responses") is untouched
    finally:
        otel_context.detach(token)
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    if flag == "true":
        assert [record.getMessage() for record in warnings] == ["FA_SDK_TELEMETRY_INACTIVE reason=flag_invalid"]
    else:
        # An explicit 0 or a project without monitoring is the quiet, expected case.
        assert not warnings
    if not connection and flag != "0":
        assert "reason=no_connection_string" in caplog.text


def test_default_without_opentelemetry_installed_never_breaks_a_model_sender(monkeypatch, caplog):
    import sys
    monkeypatch.delenv(sdk_telemetry.FLAG, raising=False)  # on by default, e.g. a process without the Hosted packages
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=00000000-0000-0000-0000-000000000000")
    monkeypatch.setattr(sdk_telemetry, "_logged", set())
    monkeypatch.setitem(sys.modules, "opentelemetry.trace.propagation.tracecontext", None)
    with caplog.at_level(logging.WARNING):
        assert direct.DirectResponses(URL, lambda: "t", "m").traceparent is None
        with sdk_telemetry.turn_context("rid") as traceparent:
            assert traceparent is None
    assert "reason=opentelemetry_unavailable" in caplog.text


def test_turn_trace_is_the_admitting_request_never_a_stale_ambient_one(telemetry):
    admitting = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    token = ambient("ab" * 16, "cd" * 8)
    try:
        sdk_telemetry.remember("rid-admitted")
    finally:
        otel_context.detach(token)
    retry = ambient("77" * 16, "66" * 8)  # a same-RID retry keeps the first admission's trace
    try:
        sdk_telemetry.remember("rid-admitted")
    finally:
        otel_context.detach(retry)
    stale = ambient("99" * 16, "88" * 8)  # e.g. the first input of a long-lived multi-turn task
    try:
        with sdk_telemetry.turn_context("rid-admitted") as traceparent:
            assert traceparent == admitting == sdk_telemetry.current_traceparent()
            assert direct.DirectResponses(URL, lambda: "t", "m").traceparent == admitting
        with sdk_telemetry.turn_context("rid-recovered") as fresh:
            assert fresh.split("-")[1] not in {"99" * 16, "ab" * 16}
        assert sdk_telemetry.current_traceparent() == "00-" + "99" * 16 + "-" + "88" * 8 + "-01"
    finally:
        otel_context.detach(stale)


def test_every_model_request_of_a_turn_carries_the_turn_traceparent(telemetry):
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'))

    async def two_requests():
        sender = direct.DirectResponses(URL, lambda: "t", "gpt-5.4-mini", transport=httpx.MockTransport(handle))
        for _ in range(2):
            response = await sender.send(httpx.Request("POST", "https://api.offline.invalid/model", json={
                "model": "gpt-5.4-mini", "input": "hi", "stream": True}))
            await response.aread()

    with sdk_telemetry.turn_context("rid") as traceparent:
        asyncio.run(two_requests())
    assert [request.headers.get("traceparent") for request in sent] == [traceparent, traceparent]


def record(name, *, scope="fa.copilot-sdk.completions", **attributes):
    return {"type": "span", "traceId": TURN_TRACE, "spanId": "3c" * 8, "parentSpanId": TURN_PARENT,
            "name": name, "kind": 2, "startTime": [1, 0], "endTime": [1, 500_000_000],
            "attributes": attributes, "status": {"code": 2, "message": "failed"}, "events": [],
            "resource": {"attributes": {"service.name": "github-copilot"}},
            "instrumentationScope": {"name": scope, "version": "0.0.0"}}


def test_span_conversion_drops_timing_noise_and_non_summary_reasoning(telemetry):
    from opentelemetry.trace import SpanKind, StatusCode
    messages = json.dumps([{"role": "assistant", "parts": [
        {"type": "reasoning", "content": RAW_REASONING}, {"type": "text", "content": "Answer."}]}])
    assert sdk_telemetry.to_span(record("session.timing.construction_input")) is None
    raw = sdk_telemetry.to_span(record("chat m", **{"gen_ai.output.messages": messages, "gen_ai.usage.input_tokens": 3,
                                                    "gen_ai.response.finish_reasons": ["stop"]}))
    assert RAW_REASONING not in json.dumps(dict(raw.attributes)) and "Answer." in raw.attributes["gen_ai.output.messages"]
    assert raw.attributes["gen_ai.usage.input_tokens"] == 3 and raw.attributes["gen_ai.response.finish_reasons"] == ("stop",)
    assert raw.kind is SpanKind.CLIENT and raw.status.status_code is StatusCode.ERROR
    assert (raw.context.trace_id, raw.parent.span_id) == (int(TURN_TRACE, 16), int(TURN_PARENT, 16))
    assert raw.end_time - raw.start_time == 500_000_000 and raw.resource.attributes["service.name"] == "fa-test-agent"
    unreadable = sdk_telemetry.to_span(record("chat m", **{"gen_ai.input.messages": "not json"}))
    assert "gen_ai.input.messages" not in unreadable.attributes
    summary = sdk_telemetry.to_span(record("chat m", scope=sdk_telemetry.SUMMARY_SOURCE,
                                           **{"gen_ai.output.messages": messages}))
    assert RAW_REASONING in summary.attributes["gen_ai.output.messages"]  # Responses wire: the public summary
    with pytest.raises(ValueError):
        sdk_telemetry.to_span({**record("chat m"), "traceId": "0" * 32})


def test_exporter_failure_never_reaches_the_turn_and_the_file_is_removed(telemetry, tmp_path, caplog):
    class Broken:
        def on_end(self, span):
            raise RuntimeError("exporter down")

        def force_flush(self, timeout):
            return False

        def shutdown(self):
            pass

    sdk_telemetry._processor_state = Broken()
    path = tmp_path / "otel-spans-x.jsonl"
    path.write_text(json.dumps(record("chat m")) + "\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        sdk_telemetry._forward(path, True)
    assert not path.exists() and "FA_SDK_TELEMETRY_DROPPED exception_type=RuntimeError" in caplog.text


def with_raw_reasoning(raw: bytes) -> bytes:
    """The same Responses stream with the provider's raw reasoning_text added to its reasoning item."""
    events = [json.loads(block.split("data: ", 1)[1]) for block in raw.decode().split("\n\n") if block.strip()]
    content = [{"type": "reasoning_text", "text": RAW_REASONING}]
    stream = []
    for event in events:
        stream.append(event)
        item = event.get("item") or {}
        if event["type"] == "response.output_item.added" and item.get("type") == "reasoning":
            stream += [{"type": kind, "item_id": item["id"], "output_index": event["output_index"], "content_index": 0,
                        **field} for kind, field in (("response.reasoning_text.delta", {"delta": RAW_REASONING}),
                                                     ("response.reasoning_text.done", {"text": RAW_REASONING}))]
        if event["type"] == "response.output_item.done" and item.get("type") == "reasoning":
            item["content"] = content
        for output in (event.get("response") or {}).get("output") or ():
            if output.get("type") == "reasoning":
                output["content"] = content
    for number, event in enumerate(stream):
        event["sequence_number"] = number
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in stream).encode()


def real_turn(workspace, runtime_path, *, rid="rid-turn", bearer="t", raw_reasoning=False):
    """One real-runtime Responses turn with a tool call through the production DirectResponses sender."""
    turn = SimpleNamespace(sent=[], requests=[], tools=[], result=None, traceparent=None)

    def provider(request):
        assert str(request.url) == URL and request.headers["authorization"] == "Bearer " + bearer
        turn.sent.append(request.headers.get("traceparent"))
        turn.requests.append(json.loads(request.content))
        stream = response_bytes(len(turn.requests), tool="read_context" if len(turn.requests) == 1 else None)
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                              content=with_raw_reasoning(stream) if raw_reasoning else stream)

    async def read(packet):
        turn.tools.append(packet["tool_call_id"])
        return ToolOutput(aw.InstructionContext(source={
            "source_id": "source", "version": 1, "sha256": "a" * 64, "byte_length": 4},
            content="TOOL_RESULT_TEXT").model_dump_json())

    async def reserve(identity):
        return None

    async def run():
        with sdk_telemetry.turn_context(rid) as traceparent:
            # The production native sender; it carries the turn's traceparent.
            sender = direct.DirectResponses(URL, lambda: bearer, "gpt-5.4-mini", transport=httpx.MockTransport(provider))
            turn.result = await run_copilot(
                state_dir=workspace / "state", working_directory=workspace / "work", sdk_session_id="telemetry",
                resume=False, prompt="Read the source, then answer.",
                tools=[ToolSpec("read_context", "Read the source.", aw.Empty.model_json_schema(), read)],
                api_url="https://context.invalid", token_provider=lambda: "UNUSED", on_event=lambda _event: None,
                timeout_seconds=60, runtime_path=runtime_path, instruction=True, model="gpt-5.4-mini",
                model_provider="openai-responses", reasoning_summary="detailed",
                session_files=SdkSessionFiles("telemetry", model_provider="openai-responses"),
                behavior_boundary=AssistantModelBoundary(reserve, sender.send, text_reply=True, streaming=True))
            turn.traceparent = traceparent

    asyncio.run(run())
    return turn


@pytest.mark.parametrize("raw_reasoning", [False, True])
def test_real_sdk_turn_reaches_application_insights_in_the_turn_trace(
        telemetry, workspace, runtime_path, caplog, raw_reasoning):
    bearer = "BEARER_TOKEN_CANARY"
    token = ambient(TURN_TRACE, TURN_PARENT)
    try:
        sdk_telemetry.remember("rid-real")
    finally:
        otel_context.detach(token)
    turn = real_turn(workspace, runtime_path, rid="rid-real", bearer=bearer, raw_reasoning=raw_reasoning)
    traceparent = turn.traceparent
    assert turn.result["final_message"] == "Original public answer 2." and len(turn.requests) == 2 and len(turn.tools) == 1
    assert turn.sent == [traceparent, traceparent] and traceparent.split("-")[1:3] == [TURN_TRACE, TURN_PARENT]
    wait_forwarded(workspace / "state", telemetry, {"invoke_agent", "chat gpt-5.4-mini", "execute_tool read_context"})
    payload = json.dumps(telemetry.items)
    # Only the public summary may be exported; never the provider's raw reasoning text or encrypted reasoning.
    for secret in (bearer, OPAQUE, CONNECTOR_SECRET, RAW_REASONING):
        assert secret not in payload
    items = spans_of(telemetry)
    names = {item["data"]["baseData"]["name"] for item in items}
    assert not [name for name in names if name.startswith("session.")]
    assert {item["tags"]["ai.operation.id"] for item in items} == {TURN_TRACE}
    root = next(item for item in items if item["data"]["baseData"]["name"] == "invoke_agent")
    assert root["tags"]["ai.operation.parentId"] == TURN_PARENT and root["tags"]["ai.cloud.role"] == "fa-test-agent"
    chats = [item["data"]["baseData"]["properties"] for item in items
             if item["data"]["baseData"]["name"] == "chat gpt-5.4-mini"]
    tool = next(item["data"]["baseData"]["properties"] for item in items
                if item["data"]["baseData"]["name"] == "execute_tool read_context"
                and "gen_ai.tool.call.result" in item["data"]["baseData"]["properties"])
    assert "TOOL_RESULT_TEXT" in tool["gen_ai.tool.call.result"] and tool["gen_ai.tool.name"] == "read_context"
    if raw_reasoning:
        # Runtime 1.0.83 surfaces raw reasoning_text as assistant text; message content stops, tools still flow.
        assert not [chat for chat in chats if "gen_ai.output.messages" in chat or "gen_ai.input.messages" in chat]
        assert "FA_SDK_TELEMETRY_CONTENT_SUPPRESSED reason=raw_reasoning" in caplog.text
    else:
        assert any(SUMMARY in chat.get("gen_ai.output.messages", "") for chat in chats)
        assert any("Read the source" in chat.get("gen_ai.input.messages", "") for chat in chats)
        assert "CONTENT_SUPPRESSED" not in caplog.text
    assert "Failed to derive Resource" not in caplog.text


def test_raw_reasoning_detector_trips_only_on_raw_text():
    assert not sdk_telemetry._RAW_REASONING["responses"].search(response_bytes(1))
    assert not sdk_telemetry._RAW_REASONING["responses"].search(response_bytes(1, observed_reasoning=True))
    assert sdk_telemetry._RAW_REASONING["responses"].search(with_raw_reasoning(response_bytes(1)))
    completions = sdk_telemetry._RAW_REASONING["completions"]
    assert completions.search(b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}')
    assert not completions.search(b'data: {"choices":[{"delta":{"reasoning_content":null,"content":"hi"}}]}')
    assert not completions.search(b'data: {"usage":{"completion_tokens_details":{"reasoning_tokens":3}}}')


def test_watch_model_response_fails_closed_on_any_route(telemetry, monkeypatch):
    clean = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    raw = b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n\n'

    async def read(response):
        return b"".join([chunk async for chunk in response.aiter_raw()])

    streamed = sdk_telemetry.watch_model_response(httpx.Response(200, stream=httpx.ByteStream(clean)), "completions")
    assert asyncio.run(read(streamed)) == clean and not sdk_telemetry._content_suppressed
    already_read = httpx.Response(200, content=clean)
    assert sdk_telemetry.watch_model_response(already_read, "completions") is already_read
    assert not sdk_telemetry._content_suppressed
    asyncio.run(read(sdk_telemetry.watch_model_response(httpx.Response(200, stream=httpx.ByteStream(raw)), "completions")))
    assert sdk_telemetry._content_suppressed
    monkeypatch.setattr(sdk_telemetry, "_content_suppressed", False)
    compressed = httpx.Response(200, headers={"content-encoding": "gzip"}, content=gzip.compress(raw))
    sdk_telemetry.watch_model_response(compressed, "completions")
    assert sdk_telemetry._content_suppressed  # unreadable: fail closed
    monkeypatch.setenv(sdk_telemetry.FLAG, "0")
    untouched = httpx.Response(200, stream=httpx.ByteStream(raw))
    assert sdk_telemetry.watch_model_response(untouched, "completions") is untouched


@pytest.mark.parametrize("raw_reasoning", [False, True])
def test_real_sdk_api_route_raw_reasoning_and_runtime_bearer_never_exported(
        telemetry, workspace, runtime_path, caplog, raw_reasoning):
    # No DirectResponses here: the gateway's own HTTP route, like the legacy assistant's API-ledger model calls.
    reply = {"content": "COMPLETIONS_ANSWER", **({"reasoning_content": RAW_REASONING} if raw_reasoning else {})}
    endpoint = ScriptedEndpoint([reply])
    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work", sdk_session_id="telemetry-completions",
        resume=False, prompt="Answer.", tools=[], api_url="https://context.invalid",
        token_provider=endpoint.token, on_event=lambda _event: None, timeout_seconds=60,
        runtime_path=runtime_path, http_transport=endpoint.transport))
    assert result["final_message"] == "COMPLETIONS_ANSWER" and endpoint.token_count == 1
    wait_forwarded(workspace / "state", telemetry, {"invoke_agent"})
    payload = json.dumps(telemetry.items)
    # The runtime itself held this bearer for the request; neither it nor any raw reasoning is exported.
    for secret in ("synthetic-offline-", RAW_REASONING, CONNECTOR_SECRET):
        assert secret not in payload
    assert ("COMPLETIONS_ANSWER" in payload) is not raw_reasoning
    assert ("FA_SDK_TELEMETRY_CONTENT_SUPPRESSED reason=raw_reasoning" in caplog.text) is raw_reasoning


def test_default_without_application_insights_runs_the_turn_quietly(monkeypatch, workspace, runtime_path, caplog):
    monkeypatch.delenv(sdk_telemetry.FLAG, raising=False)
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(sdk_telemetry, "_logged", set())
    monkeypatch.setattr(sdk_telemetry, "_processor_state", None)
    with caplog.at_level(logging.INFO):
        turn = real_turn(workspace, runtime_path)
    assert turn.result["final_message"] == "Original public answer 2." and turn.result["sdk_stop"]["graceful"] is True
    assert turn.sent == [None, None]  # no traceparent header
    assert not list((workspace / "state").rglob("otel-spans-*"))
    assert sdk_telemetry._processor_state is None  # no exporter was ever built
    assert not [record for record in caplog.records
                if record.name == sdk_telemetry.log.name and record.levelno >= logging.WARNING]


@pytest.mark.parametrize("failure", ["ingestion_unreachable", "exporter_unavailable"])
def test_telemetry_failure_never_changes_the_turn(telemetry, monkeypatch, workspace, runtime_path, caplog, failure):
    if failure == "ingestion_unreachable":
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING",
                           f"InstrumentationKey=00000000-0000-0000-0000-000000000000;IngestionEndpoint=http://127.0.0.1:{port}/")
    else:
        import azure.monitor.opentelemetry.exporter as exporter

        def broken(**_options):
            raise RuntimeError("synthetic exporter construction failure")
        monkeypatch.setattr(exporter, "AzureMonitorTraceExporter", broken)
    with caplog.at_level(logging.WARNING):
        turn = real_turn(workspace, runtime_path)
    assert turn.result["final_message"] == "Original public answer 2." and turn.result["sdk_stop"]["graceful"] is True
    assert len(turn.sent) == 2 and turn.sent[0] is not None and turn.sent[0] == turn.sent[1]
    deadline = time.monotonic() + 20
    while list((workspace / "state").rglob("otel-spans-*")) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not list((workspace / "state").rglob("otel-spans-*"))  # forwarded or dropped, never left behind
    if failure == "exporter_unavailable":
        assert "reason=exporter_unavailable exception_type=RuntimeError" in caplog.text
        assert not telemetry.items  # the linked endpoint received nothing
    else:
        assert "Retrying due to server request error" in caplog.text  # a send was attempted and failed

def test_native_turn_carries_the_admitting_trace_to_the_sdk_and_its_model_sender(responses, monkeypatch, tmp_path):
    from shared_context.behavior_native_turn_wire import NATIVE_STATE_PROFILE, NATIVE_TURN_HEADER
    from test_native_assembly import install_executor, offline_sdk
    from test_native_turn_wire import submit
    from test_native_turns import IDENTITY

    monkeypatch.delenv(sdk_telemetry.FLAG, raising=False)  # the default
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=00000000-0000-0000-0000-000000000000")
    monkeypatch.setattr(sdk_telemetry, "_remembered", type(sdk_telemetry._remembered)())
    seen = []

    async def sdk(**options):
        seen.append((sdk_telemetry.current_traceparent(), options["behavior_boundary"].sender.__self__.traceparent))
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk)
    request = submit()
    admitting = "00-" + "5a" * 16 + "-" + "6b" * 8 + "-01"
    accepted = responses.client.post("/invocations", json=request.model_dump(mode="json"), headers={
        NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE, "x-agent-session-id": IDENTITY.session_id,
        "x-agent-invocation-id": "platform-telemetry", "x-agent-foundry-call-id": "OFFLINE-original-call",
        "traceparent": admitting})
    assert accepted.status_code == 202, accepted.text
    responses.wait(request.command.request_id, "completed")
    assert seen == [(admitting, admitting)]


LOG_CANARY = "USER_CONTENT_CANARY"


def test_hosted_log_export_sends_only_the_listed_prefixes_of_the_listed_loggers(telemetry):
    from shared_context.behavior_timing import elapsed_timing

    loggers = [logging.getLogger(name) for name in sdk_telemetry.LOG_LOGGERS]
    saved = [(logger, list(logger.handlers), logger.level) for logger in loggers]
    provider = sdk_telemetry.attach_log_export()
    try:
        assert provider is not None
        attached = {name for name, logger in [("", logging.getLogger()), *logging.root.manager.loggerDict.items()]
                    if isinstance(logger, logging.Logger)
                    and any(sdk_telemetry._exportable in handler.filters for handler in logger.handlers)}
        assert attached == set(sdk_telemetry.LOG_LOGGERS)

        elapsed_timing("sdk_process_start", time.perf_counter(), job_id="0123456789ab")
        logging.getLogger("shared_context.native_resident").info("RESIDENT_SDK_CONTINUED released=%s", "abcdef012345")
        sdk = logging.getLogger("shared_context.m2_copilot")
        sdk.warning("FA_MCP_SERVER_STATUS %s", LOG_CANARY)  # same logger, a line that may carry runtime text
        sdk.warning("%s", "RESIDENT_SDK_STOPPED " + LOG_CANARY)  # an argument cannot pass the prefix check
        sdk.warning("RESIDENT_SDK_STOPPED reason=%s", "cancelled", exc_info=ValueError(LOG_CANARY))
        # The lines that explain a turn's unknown failure and an upgrade cut, as their modules format them.
        turns = logging.getLogger("shared_context.native_turns")
        turns.warning("FA_NATIVE_TURN_UNKNOWN code=%s exception_type=%s", "NATIVE_TURN_UNKNOWN", "NativeSdkFailure")
        direct = logging.getLogger("shared_context.behavior_model_direct")
        direct.warning("FA_MODEL_DIRECT_RETRY status=429 attempt=%s wait_ms=%s source=%s", 1, 1500, "retry-after-ms")
        direct.warning("FA_MODEL_DIRECT_HTTP_FAILURE status=%s wire_api=%s model=%s %s %s attempts=%s "
                       "before_output=%s", 429, "responses", "gpt-test",
                       "error_code=rate_limit_exceeded error_type=none error_param=none inner_code=none",
                       "items=3 tools=2 images=0 keys=input,model", 3, False)
        cut = logging.getLogger("shared_context.native_environment_cut")
        cut.warning("NATIVE_UPGRADE_CUT outcome=%s reason=%s scan_ms=%d", "ready", "none", 518)
        turns.warning("FA_NATIVE_HTTP %s", json.dumps({"canary": LOG_CANARY}))  # same logger, an unlisted prefix
        turns.info("FA_NATIVE_ENQUEUE_RETAINED rid=%s", LOG_CANARY)  # INFO is on now; unlisted INFO lines stay out
        turns.warning("FA_NATIVE_POST_TURN_UNKNOWN exception_type=%s", LOG_CANARY)  # the prefix ends with its space
        cut.warning("NATIVE_UPGRADE_WITHDRAWN request_id_prefix=%s", LOG_CANARY)
        logging.getLogger("shared_context.behavior_model_direct_probe").warning(
            "FA_MODEL_DIRECT_PROBE status=%s", LOG_CANARY)  # a listed prefix on an unlisted logger
        logging.getLogger("shared_context.behavior_native_sessions").warning("FA_TIMING %s", LOG_CANARY)
        # Memory status lines, as their modules format them: most name the event as the first argument.
        cosmos = logging.getLogger("shared_context.cosmos_private_memory")
        cosmos.warning("%s status=%s stage=embedding", "FA_PRIVATE_MEMORY_UPDATE", 401)
        cosmos.warning("%s status=skipped reason=embedding_unavailable", "FA_TEAM_MEMORY_CREATE")
        private = logging.getLogger("shared_context.behavior_private_memory")
        private.warning("%s status=%s mode=%s", "FA_TEAM_MEMORY_SEARCH", 404, "contextual")
        private.warning("FA_PRIVATE_MEMORY_INACTIVE reason=%s", "api_unavailable")
        native = logging.getLogger("shared_context.native_private_memory")
        native.warning("FA_PRIVATE_MEMORY_USAGE_UNREPORTED rid_prefix=%s reason=%s", "0123abcd", "no_reporter")
        team = logging.getLogger("shared_context.native_team_memory")
        team.warning("FA_TEAM_MEMORY_INACTIVE reason=store_unavailable")
        private.warning("%s status=skipped_size", "FA_PRIVATE_MEMORY_SEARCH " + LOG_CANARY)  # not an exact event name
        private.warning("%s status=unavailable mode=%s", LOG_CANARY, "static")
        private.warning("%s", "FA_PRIVATE_MEMORY_SEARCH")  # the event alone with no fixed text after it
        private.warning("FA_NATIVE_ASSEMBLY_PARTIAL reason=observer_failed exception_type=%s", LOG_CANARY)
        cosmos.warning("%s status=transport_error", "FA_PRIVATE_MEMORY_UPDATE", exc_info=ValueError(LOG_CANARY))
        logging.getLogger("shared_context.memory_relay").warning("FA_PRIVATE_MEMORY_UPDATE %s", LOG_CANARY)  # API side
        assert provider.force_flush(10000)
    finally:
        provider and provider.shutdown()
        for logger, handlers, level in saved:
            logger.handlers[:] = handlers
            logger.setLevel(level)

    logs = [item for item in telemetry.items if item["data"]["baseType"] == "MessageData"]
    messages = [item["data"]["baseData"]["message"] for item in logs]
    assert len(messages) == 12, messages
    timing = json.loads(messages[0].removeprefix("FA_TIMING "))
    assert (timing["phase"], timing["job_id"], timing["outcome"]) == ("sdk_process_start", "0123456789ab", "returned")
    assert messages[1:] == [
        "RESIDENT_SDK_CONTINUED released=abcdef012345",
        "FA_NATIVE_TURN_UNKNOWN code=NATIVE_TURN_UNKNOWN exception_type=NativeSdkFailure",
        "FA_MODEL_DIRECT_RETRY status=429 attempt=1 wait_ms=1500 source=retry-after-ms",
        "FA_MODEL_DIRECT_HTTP_FAILURE status=429 wire_api=responses model=gpt-test error_code=rate_limit_exceeded "
        "error_type=none error_param=none inner_code=none items=3 tools=2 images=0 keys=input,model attempts=3 "
        "before_output=False",
        "NATIVE_UPGRADE_CUT outcome=ready reason=none scan_ms=518",
        "FA_PRIVATE_MEMORY_UPDATE status=401 stage=embedding",
        "FA_TEAM_MEMORY_CREATE status=skipped reason=embedding_unavailable",
        "FA_TEAM_MEMORY_SEARCH status=404 mode=contextual",
        "FA_PRIVATE_MEMORY_INACTIVE reason=api_unavailable",
        "FA_PRIVATE_MEMORY_USAGE_UNREPORTED rid_prefix=0123abcd reason=no_reporter",
        "FA_TEAM_MEMORY_INACTIVE reason=store_unavailable"]
    assert {item["tags"]["ai.cloud.role"] for item in logs} == {"fa-test-agent"}
    assert LOG_CANARY not in json.dumps(telemetry.items)


def test_hosted_log_export_that_cannot_be_built_attaches_nothing(telemetry, monkeypatch, caplog):
    import azure.monitor.opentelemetry.exporter as exporter

    def broken(**_options):
        raise RuntimeError("synthetic exporter construction failure")
    monkeypatch.setattr(exporter, "AzureMonitorLogExporter", broken)
    loggers = [logging.getLogger(name) for name in sdk_telemetry.LOG_LOGGERS]
    before = [(list(logger.handlers), logger.level) for logger in loggers]
    with caplog.at_level(logging.WARNING):
        assert sdk_telemetry.attach_log_export() is None
    assert "FA_SDK_TELEMETRY_INACTIVE reason=log_exporter_unavailable exception_type=RuntimeError" in caplog.text
    assert [(list(logger.handlers), logger.level) for logger in loggers] == before


def test_hosted_log_export_keeps_hosted_stderr_and_respects_the_flag(telemetry, monkeypatch):
    # A fresh process: the Hosted entry's root logger has no handlers (pytest adds its own to this one).
    script = (
        "import logging, sys\n"
        "from shared_context import sdk_telemetry\n"
        "from shared_context.behavior_timing import elapsed_timing\n"
        "attached = sdk_telemetry.attach_log_export() if sdk_telemetry.enabled() else None\n"
        "print('attached' if attached else 'off')\n"
        "elapsed_timing('sdk_process_start', 0.0)\n"
        "logging.getLogger('shared_context.native_resident').info('RESIDENT_SDK_CONTINUED released=abc')\n"
        "logging.getLogger('shared_context.native_resident').warning('RESIDENT_SDK_DISCARDED reason=exited')\n"
    )
    source = Path(sdk_telemetry.__file__).resolve().parents[1]
    outputs = {}
    for flag in ("1", "0"):
        monkeypatch.setenv(sdk_telemetry.FLAG, flag)
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", script], cwd=source, capture_output=True,
                                encoding="utf-8", timeout=60)
        assert result.returncode == 0, result.stderr
        fa_lines = [line for line in result.stderr.splitlines() if line.startswith(("FA_TIMING", "RESIDENT_SDK_"))]
        outputs[flag] = (result.stdout.strip(), [line.split(" ", 1)[0] for line in fa_lines])
    # Same stderr lines with and without export: WARNING lines print, INFO lines do not, as before.
    assert outputs == {"1": ("attached", ["FA_TIMING", "RESIDENT_SDK_DISCARDED"]),
                       "0": ("off", ["FA_TIMING", "RESIDENT_SDK_DISCARDED"])}
    assert any(item["data"]["baseType"] == "MessageData" for item in telemetry.items)  # shutdown flushed at exit
