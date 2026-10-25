"""Hosted tracing for Copilot SDK turns: on by default (Neal 2026-10-05); ``FA_SDK_TELEMETRY=0`` turns it off.

It sends only where Foundry linked an Application Insights resource to the project; without the
platform-injected ``APPLICATIONINSIGHTS_CONNECTION_STRING`` nothing changes and nothing is logged as a warning.

Official pieces, joined by a thin seam because none of them reaches the other:

* The pinned Copilot SDK 1.0.13 / runtime 1.0.83 ``TelemetryConfig`` makes the runtime emit its own
  GenAI spans (``invoke_agent``, ``chat``, ``execute_tool``). It exports only OTLP/HTTP or a JSON-lines
  file, and its OTLP batch is not flushed before the SDK stops the runtime, so the file is used.
* Microsoft's Azure Monitor trace exporter, already in the Hosted image through
  azure-ai-agentserver-core, sends those spans to the Application Insights resource that Foundry
  links to the project (the platform-injected ``APPLICATIONINSIGHTS_CONNECTION_STRING``).

Every model request of one turn carries that turn's W3C ``traceparent`` so Foundry's own server-side
``invoke_agent``/``chat`` spans join the same trace. Content capture follows this flag; only the
Responses wire keeps the runtime's reasoning parts (the provider's public summary), and a provider stream
with raw reasoning text stops message export for the process. Telemetry failures are logged and never
fail, delay or retry a turn. Under the same flag, the Hosted entry also exports FA's own content-free
``FA_TIMING`` and ``RESIDENT_SDK_*`` log lines (``export_logs``); Hosted stderr output stays as it was.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import re
import secrets
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

import httpx

FLAG = "FA_SDK_TELEMETRY"
CONNECTION_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"
SOURCE_PREFIX = "fa.copilot-sdk."
# On the Responses wire the runtime fills reasoning parts only from the provider's public summary_text;
# on Chat Completions the same parts carry the provider's raw reasoning text, so they are removed.
SUMMARY_SOURCE = SOURCE_PREFIX + "responses"
_MESSAGE_ATTRIBUTES = ("gen_ai.input.messages", "gen_ai.output.messages")
# Raw reasoning text in a provider stream. Runtime 1.0.83 can surface Responses reasoning_text as assistant
# text, which no part-type filter can tell apart, so seeing it stops message export (FA's models never send it).
_RAW_REASONING = {
    "responses": re.compile(rb'"type"\s*:\s*"(?:response\.)?reasoning_text'),
    "completions": re.compile(rb'"(?:reasoning_content|reasoning_text|reasoning)"\s*:\s*"[^"]'),
}
_REMEMBERED_LIMIT = 1024
_MAX_READ_BYTES = 64 * 1024 * 1024
_FLUSH_MILLIS = 5000

log = logging.getLogger(__name__)
_lock = threading.Lock()
_build_lock = threading.Lock()
_forward_lock = threading.Lock()
_remembered: OrderedDict[str, str] = OrderedDict()
_offsets: dict[str, int] = {}
_logged: set[str] = set()
_processor_state = None  # None: not built yet; False: unavailable; otherwise the span processor
_content_suppressed = False


def _suppress_content(reason: str) -> None:
    global _content_suppressed
    if not _content_suppressed:
        _content_suppressed = True
        log.warning("FA_SDK_TELEMETRY_CONTENT_SUPPRESSED reason=%s", reason)


def observe_model_stream(raw: bytes, wire_api: str) -> None:
    """Fail closed: once a provider stream carries raw reasoning text, this process exports no message content."""
    pattern = _RAW_REASONING.get(wire_api)
    if pattern is None or pattern.search(raw):
        _suppress_content("raw_reasoning")


class _WatchedStream(httpx.AsyncByteStream):
    def __init__(self, response: httpx.Response, wire_api: str):
        self.response, self.wire_api = response, wire_api

    async def __aiter__(self):
        raw = bytearray()
        try:
            async for chunk in self.response.aiter_raw():
                raw.extend(chunk)
                yield chunk
        finally:
            observe_model_stream(bytes(raw), self.wire_api)

    async def aclose(self):
        await self.response.aclose()


def watch_model_response(response: httpx.Response, wire_api: str) -> httpx.Response:
    """Every model response the runtime reads, on any route, is checked for raw reasoning (telemetry on only)."""
    if not enabled():
        return response
    try:
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            _suppress_content("unreadable_stream")
            return response
        try:
            content = response.content
        except httpx.ResponseNotRead:
            try:
                request = response.request
            except RuntimeError:
                request = None
            return httpx.Response(response.status_code, headers=response.headers, request=request,
                                  extensions=response.extensions, stream=_WatchedStream(response, wire_api))
        observe_model_stream(content, wire_api)
    except Exception:
        _suppress_content("unreadable_stream")
    return response


def _inactive_once(reason: str, level: int = logging.WARNING) -> None:
    if reason not in _logged:
        _logged.add(reason)
        log.log(level, "FA_SDK_TELEMETRY_INACTIVE reason=%s", reason)


def enabled() -> bool:
    """On unless FA_SDK_TELEMETRY=0, and only where Foundry linked an Application Insights resource."""
    value = os.environ.get(FLAG, "")
    if value == "0":
        return False
    if value not in {"", "1"}:
        # Recording content follows this flag, so an unreadable value stays off.
        _inactive_once("flag_invalid")
        return False
    if not os.environ.get(CONNECTION_ENV):
        # Project monitoring is off: the expected quiet case, not a fault.
        _inactive_once("no_connection_string", logging.INFO)
        return False
    return True


def _tracecontext():
    """The OpenTelemetry context module and W3C propagator, or None where the API is not installed."""
    try:
        from opentelemetry import context
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    except ImportError:
        _inactive_once("opentelemetry_unavailable")
        return None
    return context, TraceContextTextMapPropagator()


def current_traceparent() -> str | None:
    """The current W3C traceparent when telemetry is on and a valid trace is current."""
    loaded = _tracecontext() if enabled() else None
    if loaded is None:
        return None
    context, propagator = loaded
    carrier: dict[str, str] = {}
    propagator.inject(carrier, context=context.get_current())
    return carrier.get("traceparent")


def remember(request_id: str) -> None:
    """At admission, keep the first admitting request's trace for that turn (this process, best effort)."""
    traceparent = current_traceparent()
    if traceparent is None:
        return
    with _lock:
        if request_id in _remembered:
            return  # a same-RID retry or refused duplicate keeps the original admission's trace
        _remembered[request_id] = traceparent
        while len(_remembered) > _REMEMBERED_LIMIT:
            _remembered.popitem(last=False)


@contextmanager
def turn_context(request_id: str):
    """One trace per turn: the admitting request's trace, else a new one, never a stale ambient trace."""
    loaded = _tracecontext() if enabled() else None
    if loaded is None:
        yield None
        return
    context, propagator = loaded
    with _lock:
        traceparent = _remembered.get(request_id)
    if traceparent is None:
        traceparent = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
    token = context.attach(propagator.extract({"traceparent": traceparent}, context=context.Context()))
    try:
        yield traceparent
    finally:
        try:
            context.detach(token)
        except ValueError:
            pass


class SdkTelemetry(NamedTuple):
    config: dict
    span_file: Path


def client_telemetry(state_dir: Path, wire_api: str) -> SdkTelemetry | None:
    """SDK ``TelemetryConfig`` writing to a private file; the exporter is built later, off the turn's path."""
    if not enabled():
        return None
    path = Path(state_dir) / f"otel-spans-{secrets.token_hex(8)}.jsonl"
    return SdkTelemetry({"exporter_type": "file", "file_path": str(path), "capture_content": True,
                         "source_name": SOURCE_PREFIX + wire_api}, path)


def forward(path: Path | None, *, final: bool) -> None:
    """Hand the runtime's newly written spans to the exporter in the background; the turn never waits."""
    if path is None:
        return
    try:
        threading.Thread(target=_forward, args=(Path(path), final), name="fa-sdk-telemetry", daemon=True).start()
    except RuntimeError as error:
        log.warning("FA_SDK_TELEMETRY_DROPPED exception_type=%s", type(error).__name__)


def _forward(path: Path, final: bool) -> None:
    key = str(path)
    with _forward_lock:
        try:
            start = _offsets.get(key, 0)
            with open(path, "rb") as file:
                size = file.seek(0, os.SEEK_END)
                if size - start > _MAX_READ_BYTES:
                    log.warning("FA_SDK_TELEMETRY_DROPPED reason=file_too_large")
                    start = size
                file.seek(start)
                data = file.read(size - start)
            complete = data[:data.rfind(b"\n") + 1]
            _offsets[key] = start + len(complete)
            processor = _processor()
            if processor is not None:
                dropped = 0
                for line in complete.splitlines():
                    try:
                        span = to_span(json.loads(line))
                    except (ValueError, TypeError, KeyError, AttributeError):
                        dropped += 1
                        continue
                    if span is not None:
                        processor.on_end(span)
                if dropped:
                    log.warning("FA_SDK_TELEMETRY_DROPPED reason=span_invalid count=%s", dropped)
                if final:
                    # A Hosted sandbox may be suspended once idle; send this turn's spans now.
                    processor.force_flush(_FLUSH_MILLIS)
        except FileNotFoundError:
            pass
        except Exception as error:
            log.warning("FA_SDK_TELEMETRY_DROPPED exception_type=%s", type(error).__name__)
        finally:
            if final:
                _offsets.pop(key, None)
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.warning("FA_SDK_TELEMETRY_FILE_RETAINED")


def to_span(record: dict):
    """One runtime file-exporter record as an SDK span; None for the runtime's session.* timing spans."""
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

    name = record["name"]
    if record.get("type", "span") != "span" or not isinstance(name, str) or name.startswith("session."):
        return None
    scope = record.get("instrumentationScope") or {}
    summary = scope.get("name") == SUMMARY_SOURCE
    trace_id = _hex_id(record["traceId"], 32)
    flags = TraceFlags(TraceFlags.SAMPLED)
    parent = record.get("parentSpanId")
    status = record.get("status") or {}
    code = {1: StatusCode.OK, 2: StatusCode.ERROR}.get(status.get("code"), StatusCode.UNSET)
    kinds = {0: SpanKind.INTERNAL, 1: SpanKind.SERVER, 2: SpanKind.CLIENT, 3: SpanKind.PRODUCER,
             4: SpanKind.CONSUMER}  # the runtime's JavaScript SpanKind numbering
    return ReadableSpan(
        name=name,
        context=SpanContext(trace_id, _hex_id(record["spanId"], 16), is_remote=False, trace_flags=flags),
        parent=None if not parent else SpanContext(trace_id, _hex_id(parent, 16), is_remote=False, trace_flags=flags),
        resource=_resource(),
        attributes=_attributes(record.get("attributes") or {}, summary=summary),
        events=tuple(Event(event["name"], _attributes(event.get("attributes") or {}, summary=summary),
                           _nanos(event["time"])) for event in record.get("events") or ()),
        kind=kinds.get(record.get("kind"), SpanKind.INTERNAL),
        status=Status(code, status.get("message") if code is StatusCode.ERROR else None),
        start_time=_nanos(record["startTime"]), end_time=_nanos(record["endTime"]),
        instrumentation_scope=InstrumentationScope(scope.get("name") or "github-copilot", scope.get("version")),
    )


def _hex_id(value, length: int) -> int:
    if not isinstance(value, str) or len(value) != length:
        raise ValueError("invalid trace identifier")
    number = int(value, 16)
    if number == 0:
        raise ValueError("invalid trace identifier")
    return number


def _nanos(value) -> int:
    seconds, nanos = value
    return int(seconds) * 1_000_000_000 + int(nanos)


def _attributes(values: dict, *, summary: bool) -> dict:
    result = {}
    for key, value in values.items():
        if key in _MESSAGE_ATTRIBUTES:
            if _content_suppressed:
                continue
            if not summary:
                value = _without_reasoning(value)
                if value is None:
                    continue
        result[key] = _value(value)
    return result


def _without_reasoning(value):
    """Remove reasoning parts; an unreadable message list is not exported at all."""
    try:
        messages = json.loads(value)
        for message in messages:
            message["parts"] = [part for part in message.get("parts", []) if part.get("type") != "reasoning"]
        return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError, AttributeError):
        return None


def _value(value):
    if isinstance(value, (str, bool, int, float)):
        return value
    if (isinstance(value, list) and value and len({type(item) for item in value}) == 1
            and isinstance(value[0], (str, bool, int, float))):
        return tuple(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _resource():
    # Foundry documents service.name (cloud_RoleName) as the agent's name for hosted agent telemetry.
    from opentelemetry.sdk.resources import Resource
    return Resource({"service.name": os.environ.get("FOUNDRY_AGENT_NAME") or "github-copilot"})


def _processor():
    global _processor_state
    with _build_lock:
        if _processor_state is None:
            _processor_state = _build_processor() or False
        return _processor_state or None


def _exporter_options(connection: str) -> dict:
    options = {"connection_string": connection, "disable_offline_storage": True}
    # Same rule as azure-ai-agentserver-core's own distro setup.
    if os.environ.get("APPLICATIONINSIGHTS_AUTH_MODE", "").strip().lower() == "entra":
        from azure.identity import ManagedIdentityCredential
        options["credential"] = ManagedIdentityCredential()
    return options


def _build_processor():
    connection = os.environ.get(CONNECTION_ENV)
    if not connection:
        _inactive_once("no_connection_string", logging.INFO)
        return None
    try:
        from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # Not installed globally: it only names this exporter's resource (the platform's
        # protocol-runtime observability stays as the host configured it).
        processor = BatchSpanProcessor(AzureMonitorTraceExporter(
            **_exporter_options(connection), tracer_provider=TracerProvider(resource=_resource())))
    except Exception as error:
        log.warning("FA_SDK_TELEMETRY_INACTIVE reason=exporter_unavailable exception_type=%s", type(error).__name__)
        return None
    atexit.register(processor.shutdown)
    return processor


# FA's own Hosted latency and failure lines, sent to the same Application Insights. Only these loggers and these
# fixed prefixes: FA_TIMING carries phase, hashed turn or binding ID, milliseconds and outcome; RESIDENT_SDK_* carries a
# fixed reason code or a hash prefix; FA_NATIVE_TURN_UNKNOWN a fixed code and exception type; FA_MODEL_DIRECT_* status,
# fixed provider codes, counts and wait ms; NATIVE_UPGRADE_CUT codes, counts, ms, a RID prefix and session IDs;
# FA_PRIVATE_MEMORY_* / FA_TEAM_MEMORY_* status, stage and reason codes, exception types, counts, official update or
# memory IDs and a RID prefix. Other lines of the same loggers (FA_MCP_SERVER_STATUS error text, FA_NATIVE_HTTP, ...)
# stay out.
LOG_LOGGERS = ("shared_context.behavior_timing", "shared_context.behavior_hosted", "shared_context.m2_copilot",
               "shared_context.native_resident", "shared_context.native_sdk", "shared_context.native_turns",
               "shared_context.behavior_model_direct", "shared_context.native_environment_cut",
               "shared_context.behavior_private_memory", "shared_context.cosmos_private_memory",
               "shared_context.native_private_memory", "shared_context.native_team_memory",
               "shared_context.native_memory_candidate")
# Lines with these prefixes leave the container: their arguments must stay fixed codes, hashes or numbers, never text.
LOG_PREFIXES = ("FA_TIMING ", "RESIDENT_SDK_", "FA_NATIVE_TURN_UNKNOWN ", "FA_MODEL_DIRECT_", "NATIVE_UPGRADE_CUT ",
                "FA_PRIVATE_MEMORY_", "FA_TEAM_MEMORY_")
# The memory clients name their event as the first argument ("%s status=..."); only these exact names count.
LOG_EVENTS = ("FA_PRIVATE_MEMORY_SEARCH", "FA_PRIVATE_MEMORY_UPDATE", "FA_TEAM_MEMORY_SEARCH", "FA_TEAM_MEMORY_CREATE")


def _exportable(record: logging.LogRecord) -> bool:
    # The format string, not the rendered line, so no argument can make a line exportable (a "%s "-led one only by
    # an exact event name); no exception text.
    if not isinstance(record.msg, str) or record.exc_info:
        return False
    return record.msg.startswith(LOG_PREFIXES) or (
        record.msg.startswith("%s ") and isinstance(record.args, tuple) and bool(record.args)
        and type(record.args[0]) is str and record.args[0] in LOG_EVENTS)


def export_logs() -> None:
    """Hosted startup: attach the filtered log export in the background, so the import never delays startup."""
    if enabled():
        threading.Thread(target=attach_log_export, name="fa-log-export", daemon=True).start()


def attach_log_export():
    """The LoggerProvider now exporting ``LOG_PREFIXES`` lines of ``LOG_LOGGERS``; None when it cannot be built."""
    try:
        from azure.monitor.opentelemetry.exporter import AzureMonitorLogExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        try:
            from opentelemetry.instrumentation.logging.handler import LoggingHandler
        except ImportError:  # an older resolved opentelemetry-instrumentation-logging; the SDK still has one
            from opentelemetry.sdk._logs import LoggingHandler

        # Private, like the span exporter: the process's global OpenTelemetry providers stay untouched.
        provider = LoggerProvider(resource=_resource())
        provider.add_log_record_processor(BatchLogRecordProcessor(
            AzureMonitorLogExporter(**_exporter_options(os.environ[CONNECTION_ENV]))))
        handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
    except Exception as error:
        log.warning("FA_SDK_TELEMETRY_INACTIVE reason=log_exporter_unavailable exception_type=%s",
                    type(error).__name__)
        return None
    handler.addFilter(_exportable)
    atexit.register(provider.shutdown)
    # Any handler on a logger stops logging.lastResort, which prints these WARNING lines to Hosted stderr today.
    # Decided once: the Hosted entry never gives root a handler (a later root handler would print them twice).
    keep_stderr = logging.lastResort is not None and not logging.getLogger().handlers
    for name in LOG_LOGGERS:
        logger = logging.getLogger(name)
        if keep_stderr:  # first, so no WARNING line slips between the two additions
            logger.addHandler(logging.lastResort)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)  # RESIDENT_SDK_CONTINUED (a kept SDK reused) is INFO
    return provider
