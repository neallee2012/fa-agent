"""PA-16 B observations, not a model transport or a second billing ledger."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import re
from typing import Annotated, Literal

from pydantic import Field

from .behavior_instruction_stream import SseDataEvents
from .behavior_wire import Digest, WireModel

Count = Annotated[int, Field(ge=0, le=2**53 - 1)]
ObservedId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")]
Purpose = Literal["execution", "review", "delegation", "compaction", "unknown"]
MAX_INFERENCE_BYTES = 12 * 1024
MAX_REQUESTS = 32
MAX_ATTEMPTS = 64
MAX_SDK_USAGE = 32
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}")


def observed_id(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def count(value):
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


class TokenObservation(WireModel):
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    reasoning_tokens: Count | None = None
    cache_read_tokens: Count | None = None
    cache_write_tokens: Count | None = None
    total_tokens: Count | None = None
    unknown_reason: Literal["not_reported_or_invalid"] = "not_reported_or_invalid"


class LogicalRequest(WireModel):
    logical_id: Count
    source: Literal["gateway", "direct"]
    sdk_request_id: ObservedId | None
    sdk_session_id: ObservedId | None
    identity_reason: Literal["not_reported"] | None
    purpose: Purpose
    purpose_reason: Literal["not_reported"] | None
    sdk_interaction_type: ObservedId | None = None
    model: ObservedId | None
    wire_api: Literal["responses", "completions"]
    body_bytes: Count
    body_sha256: Digest
    gateway_fingerprint: Digest | None = None
    unknown_reason: Literal["not_reported_or_invalid"] = "not_reported_or_invalid"


class TransportAttempt(WireModel):
    attempt_id: Count
    logical_id: Count | None
    model: ObservedId | None
    wire_api: Literal["responses", "completions"]
    body_bytes: Count
    body_sha256: Digest
    state: Literal["started", "response", "complete", "interrupted", "failed"]
    http_status: Annotated[int, Field(ge=100, le=599)] | None = None
    provider_request_id: ObservedId | None = None
    provider_id_reason: Literal["not_reported"] | None = "not_reported"
    provider_usage: TokenObservation | None = None
    usage_reason: Literal["not_reported", "conflicting_observations"] | None = "not_reported"
    stream_complete: bool | None = None
    ledger_measured: bool | None = None
    unknown_reason: Literal["not_observed_or_unverified"] = "not_observed_or_unverified"


class SdkUsage(WireModel):
    event_id: ObservedId | None
    sdk_session_id: ObservedId
    sdk_agent_id: ObservedId | None
    model: ObservedId | None
    api_call_id: ObservedId | None
    provider_call_id: ObservedId | None
    service_request_id: ObservedId | None
    tokens: TokenObservation
    request_match: None = None
    match_reason: Literal["no_verified_cross_source_id"] = "no_verified_cross_source_id"
    dedup_reason: Literal["event_id_missing"] | None = None
    identity_reason: Literal["not_reported_or_invalid"] = "not_reported_or_invalid"


class SdkContext(WireModel):
    event_id: ObservedId | None
    current_tokens: Count | None
    token_limit: Count | None
    system_tokens: Count | None
    conversation_tokens: Count | None
    tool_definitions_tokens: Count | None
    messages_length: Count | None
    scope: Literal["context-window-snapshot-not-turn-usage"] = "context-window-snapshot-not-turn-usage"
    unknown_reason: Literal["not_reported_or_invalid"] = "not_reported_or_invalid"


class NativeInference(WireModel):
    schema_version: Literal["fa-native-inference-observation-v1"] = "fa-native-inference-observation-v1"
    state: Literal["observed", "partial", "unknown"]
    reason: Literal["observer_failed", "summary_bound", "conflicting_event", "source_unconfirmed", "not_recorded"] | None = None
    logical_prepared: Count | None
    transport_started: Count | None
    counter_reason: Literal["observer_not_attached", "not_recorded"] | None = None
    requests: Annotated[tuple[LogicalRequest, ...], Field(max_length=MAX_REQUESTS)] = ()
    attempts: Annotated[tuple[TransportAttempt, ...], Field(max_length=MAX_ATTEMPTS)] = ()
    sdk_usage: Annotated[tuple[SdkUsage, ...], Field(max_length=MAX_SDK_USAGE)] = ()
    sdk_context: Annotated[tuple[SdkContext, ...], Field(max_length=4)] = ()
    sdk_usage_reason: Literal["no_event_observed"] | None = "no_event_observed"
    sdk_context_reason: Literal["no_event_observed"] | None = "no_event_observed"
    full_request_tokens: None = None
    full_request_tokens_reason: Literal["counter_unavailable"] = "counter_unavailable"
    provider_usage_ref: Literal["receipt.usage"] = "receipt.usage"
    billing_effect: Literal["none-observation-only"] = "none-observation-only"
    omitted_observations: Count = 0


@dataclass(frozen=True)
class InferenceLink:
    owner: NativeInferenceCollector
    logical_id: int


class NativeInferenceCollector:
    def __init__(self, sdk_session_id):
        self.sdk_session_id = sdk_session_id
        self.logical_prepared = 0
        self.transport_started = 0
        self.logical_attached = False
        self.transport_attached = False
        self.requests: list[LogicalRequest] = []
        self.attempts: dict[int, TransportAttempt] = {}
        self.sdk_usage: list[SdkUsage] = []
        self.sdk_context: list[SdkContext] = []
        self._events: dict[str, WireModel] = {}
        self._streams: dict[int, SseDataEvents] = {}
        self.reason = None
        self.omitted = 0

    def attach_transport(self):
        self.logical_attached = self.transport_attached = True

    @contextmanager
    def observation(self):
        try:
            yield
        except Exception as error:
            self.reason = "observer_failed"
            logging.getLogger(__name__).warning(
                "FA_NATIVE_INFERENCE_PARTIAL exception_type=%s", type(error).__name__)

    def prepare(self, raw, model, wire_api, *, source, sdk_request_id=None, sdk_session_id=None,
                purpose: Purpose = "unknown", interaction_type=None, fingerprint=None):
        with self.observation():
            if source == "gateway" and sdk_session_id != self.sdk_session_id:
                self.reason = "source_unconfirmed"
                return None
            self.logical_attached = True
            self.logical_prepared += 1
            link = InferenceLink(self, self.logical_prepared)
            if len(self.requests) >= MAX_REQUESTS:
                self.omitted += 1
                self.reason = "summary_bound"
                return link
            request_id, session_id = observed_id(sdk_request_id), observed_id(sdk_session_id)
            self.requests.append(LogicalRequest(
                logical_id=link.logical_id, source=source, sdk_request_id=request_id, sdk_session_id=session_id,
                identity_reason=None if request_id is not None and session_id is not None else "not_reported",
                purpose=purpose, purpose_reason="not_reported" if purpose == "unknown" else None,
                sdk_interaction_type=observed_id(interaction_type),
                model=observed_id(model), wire_api=wire_api, body_bytes=len(raw),
                body_sha256=hashlib.sha256(raw).hexdigest(),
                gateway_fingerprint=fingerprint,
            ))
            return link
        return None

    def purpose(self, link, purpose):
        with self.observation():
            if not isinstance(link, InferenceLink) or link.owner is not self:
                return
            for index, request in enumerate(self.requests):
                if request.logical_id == link.logical_id:
                    self.requests[index] = request.model_copy(update={"purpose": purpose, "purpose_reason": None})
                    return

    def started(self, link, outgoing, model, wire_api):
        with self.observation():
            self.transport_started += 1
            self.transport_attached = True
            number = self.transport_started
            if len(self.attempts) >= MAX_ATTEMPTS:
                self.omitted += 1
                self.reason = "summary_bound"
                return None
            self.attempts[number] = TransportAttempt(
                attempt_id=number, logical_id=link.logical_id if isinstance(link, InferenceLink)
                and link.owner is self else None, model=observed_id(model), wire_api=wire_api,
                body_bytes=len(outgoing.content), body_sha256=hashlib.sha256(outgoing.content).hexdigest(),
                state="started",
            )
            self._streams[number] = SseDataEvents()
            return number
        return None

    def response(self, number, response):
        with self.observation():
            if number not in self.attempts:
                return
            identifier = observed_id(response.headers.get("x-request-id"))
            self.attempts[number] = self.attempts[number].model_copy(update={
                "state": "response", "http_status": response.status_code,
                "provider_request_id": identifier, "provider_id_reason": "not_reported" if identifier is None else None,
            })

    def provider_payload(self, number, value):
        with self.observation():
            if number not in self.attempts or not isinstance(value, dict):
                return
            record = self.attempts[number]
            response = value.get("response")
            payload = response if isinstance(response, dict) else value
            usage = payload.get("usage")
            if not isinstance(usage, dict):
                return
            inputs = usage.get("input_tokens_details" if record.wire_api == "responses" else "prompt_tokens_details")
            outputs = usage.get("output_tokens_details" if record.wire_api == "responses" else "completion_tokens_details")
            inputs, outputs = inputs if isinstance(inputs, dict) else {}, outputs if isinstance(outputs, dict) else {}
            tokens = TokenObservation(
                input_tokens=count(usage.get("input_tokens" if record.wire_api == "responses" else "prompt_tokens")),
                output_tokens=count(usage.get("output_tokens" if record.wire_api == "responses" else "completion_tokens")),
                reasoning_tokens=count(outputs.get("reasoning_tokens")), cache_read_tokens=count(inputs.get("cached_tokens")),
                cache_write_tokens=count(inputs.get("cache_write_tokens")), total_tokens=count(usage.get("total_tokens")),
            )
            if record.provider_usage is not None and record.provider_usage != tokens:
                self.attempts[number] = record.model_copy(update={"usage_reason": "conflicting_observations"})
            elif record.usage_reason != "conflicting_observations":
                self.attempts[number] = record.model_copy(update={"provider_usage": tokens, "usage_reason": None})

    def chunk(self, number, chunk):
        with self.observation():
            parser = self._streams.get(number)
            if parser is None:
                return
            for event in parser.feed(chunk):
                if event.strip() == b"[DONE]":
                    continue
                try:
                    value = json.loads(event)
                except (ValueError, UnicodeError):
                    continue
                if (self.attempts[number].wire_api == "responses" and (
                        not isinstance(value, dict) or value.get("type") not in {
                            "response.completed", "response.incomplete", "response.failed"})):
                    continue
                self.provider_payload(number, value)

    def closed(self, number):
        with self.observation():
            if number in self.attempts and self.attempts[number].state in {"started", "response"}:
                self.ended(number, state="interrupted")

    def ended(self, number, *, state, complete=False, measured=False):
        with self.observation():
            if number in self.attempts:
                self.attempts[number] = self.attempts[number].model_copy(update={
                    "state": state, "stream_complete": complete, "ledger_measured": measured,
                })
            self._streams.pop(number, None)

    def event(self, value):
        with self.observation():
            kind = value.get("type")
            if kind not in {"assistant.usage", "session.usage_info"}:
                return
            data = value.get("data")
            if not isinstance(data, dict):
                self.reason = "source_unconfirmed"
                return
            if (data.get("session_id", self.sdk_session_id) != self.sdk_session_id
                    or value.get("session_id", self.sdk_session_id) != self.sdk_session_id):
                self.reason = "source_unconfirmed"
                return
            identifier = observed_id(value.get("id"))
            if kind == "assistant.usage":
                record = SdkUsage(
                    event_id=identifier, sdk_session_id=self.sdk_session_id,
                    sdk_agent_id=observed_id(value.get("agent_id")), model=observed_id(data.get("model")),
                    api_call_id=observed_id(data.get("api_call_id")), provider_call_id=observed_id(data.get("provider_call_id")),
                    service_request_id=observed_id(data.get("service_request_id")),
                    tokens=TokenObservation(**{name: count(data.get(name)) for name in (
                        "input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens")}),
                    dedup_reason="event_id_missing" if identifier is None else None,
                )
                target, limit = self.sdk_usage, MAX_SDK_USAGE
            else:
                record = SdkContext(event_id=identifier, **{name: count(data.get(name)) for name in (
                    "current_tokens", "token_limit", "system_tokens", "conversation_tokens",
                    "tool_definitions_tokens", "messages_length")})
                target, limit = self.sdk_context, 4
            if identifier is not None and identifier in self._events:
                if self._events[identifier] != record:
                    self.reason = "conflicting_event"
                return
            if len(target) >= limit:
                self.omitted += 1
                self.reason = "summary_bound"
                return
            target.append(record)
            if identifier is not None:
                self._events[identifier] = record

    def snapshot(self):
        with self.observation():
            result = NativeInference(
                state="partial" if self.reason else "observed", reason=self.reason,
                logical_prepared=self.logical_prepared if self.logical_attached else None,
                transport_started=self.transport_started if self.transport_attached else None,
                counter_reason=None if self.logical_attached and self.transport_attached else "observer_not_attached",
                requests=tuple(self.requests), attempts=tuple(self.attempts.values()),
                sdk_usage=tuple(self.sdk_usage), sdk_context=tuple(self.sdk_context),
                sdk_usage_reason=None if self.sdk_usage else "no_event_observed",
                sdk_context_reason=None if self.sdk_context else "no_event_observed", omitted_observations=self.omitted,
            )
            for field in ("attempts", "requests", "sdk_context", "sdk_usage"):
                while len(result.model_dump_json().encode("utf-8")) > MAX_INFERENCE_BYTES and getattr(result, field):
                    result = result.model_copy(update={
                        field: getattr(result, field)[:-1], "state": "partial", "reason": "summary_bound",
                        "omitted_observations": result.omitted_observations + 1,
                    })
            return result
        return NativeInference(state="partial", reason="observer_failed", logical_prepared=None, transport_started=None)
