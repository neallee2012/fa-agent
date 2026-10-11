"""ADR-0006 §5 A1/A2 (T4b): the Hosted sends Responses requests directly to the project endpoint. Off by default.

The SDK's model layer still goes through ``_ModelGateway``: its per-turn call limit, replay refusal and model
check run first. This module only replaces the upstream transport after them, for one turn:

- Only Responses requests of a free instruction go direct. Delegated child requests and checkpoint compaction
  keep the API path (reserve, consume, ``record_usage``), so nothing is counted twice.
- The identity is the Hosted agent's own managed identity (scope ``https://ai.azure.com/.default``); the probe
  confirmed it may call inference on the project endpoint. The token is only ever sent to the project's
  ``/openai/v1/responses`` URL; no retries, no redirects, no proxy settings from the environment.
- Each request whose send started is counted. Provider usage from the completed stream is summed; a request
  without valid usage (error status, broken or cancelled stream) stays unmeasured, never 0. The turn reports
  ``usage()`` once, in its end receipt; the API records it with ``record_turn_usage`` before consuming the
  completion.

Until the end receipt carries this usage (``TURN_USAGE_REPORTED``), a direct call would be spend with no
ledger entry, so the switch stays inert even when the variable is set.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import random
import re
from time import perf_counter

import httpx

from .behavior_assistant_deadlines import hosted_sync
from .behavior_instruction_stream import BoundedStream, SseDataEvents
from .behavior_model_direct_probe import SCOPE, _URL
from .behavior_timing import current_work, elapsed_timing
from .behavior_turn_usage import TURN_USAGE_SCHEMA, ModelUsage, TurnModelUsage
from .errors import Problem
from .native_inference import InferenceLink

URL_ENV = "FA_MODEL_DIRECT_URL"
# Set by the end-receipt slice once the turn's usage reaches record_turn_usage (ADR-0006 §5 A2).
TURN_USAGE_REPORTED = False
MAX_ERROR_BYTES = 8192
# https://learn.microsoft.com/azure/foundry/openai/how-to/quota#understanding-rate-limits: retry a 429 after
# retry-after-ms (else backoff with jitter); the official SDK defaults to two retries. Bounded so a turn never stalls.
MAX_THROTTLE_RETRIES = 2
MAX_THROTTLE_WAIT_SECONDS = 30.0
# Statuses the provider returns before processing the request; 408 and 5xx stay unknown.
DEFINITE_REFUSALS = frozenset({400, 401, 403, 404, 405, 409, 413, 415, 422, 429})


def throttle_wait(response: httpx.Response, attempt: int):
    """Seconds to wait before resending a throttled request, and the source; None if over the bound."""
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = response.headers.get(name)
        if value is None:
            continue
        try:
            seconds = float(value) * scale
        except ValueError:
            continue
        if not 0 <= seconds <= MAX_THROTTLE_WAIT_SECONDS:
            return None
        return seconds, name
    return min(MAX_THROTTLE_WAIT_SECONDS, 2.0 ** attempt + random.random()), "backoff"
# Provider error identifiers (code, type, param path) are standard names, never prompt or answer text.
_ERROR_FIELD = re.compile(r"^[A-Za-z0-9_.\-\[\]]{1,96}$")


def _field(value) -> str:
    return value if isinstance(value, str) and _ERROR_FIELD.fullmatch(value) else ("none" if value is None else "invalid")


def _request_shape(body) -> str:
    """Counts only: how many messages/input items, tools and image parts the request carried."""
    items = body.get("messages", body.get("input"))
    items = items if isinstance(items, list) else []
    images = sum(1 for item in items if isinstance(item, dict) and isinstance(item.get("content"), list)
                 for part in item["content"] if isinstance(part, dict) and "image" in str(part.get("type", "")))
    tools = body.get("tools")
    return (f"items={len(items)} tools={len(tools) if isinstance(tools, list) else 0} images={images} "
            f"keys={','.join(sorted(name for name in body if _ERROR_FIELD.fullmatch(name)))}")


def _error_fields(value) -> str:
    error = value.get("error") if isinstance(value, dict) else None
    if not isinstance(error, dict):
        return "error_code=none error_type=none error_param=none inner_code=none"
    inner = error.get("innererror") if isinstance(error.get("innererror"), dict) else {}
    return (f"error_code={_field(error.get('code'))} error_type={_field(error.get('type'))} "
            f"error_param={_field(error.get('param'))} inner_code={_field(inner.get('code'))}")


def _provider_error(value) -> dict:
    """The provider's own code and parameter path, only when each is a standard identifier."""
    error = value.get("error") if isinstance(value, dict) else None
    if not isinstance(error, dict):
        return {}
    found = {"provider_error_code": error.get("code"), "provider_error_param": error.get("param")}
    return {key: item for key, item in found.items() if isinstance(item, str) and _ERROR_FIELD.fullmatch(item)}


def request_fingerprint(body) -> str:
    """The same digest ``_ModelGateway`` reserves for this request body."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode()).hexdigest()


def direct_eligible(instruction) -> bool:
    """A Responses instruction outside the A/B combined model allowance.

    The API reserve still enforces that allowance for a peer turn and for a turn holding the collaboration
    capability (``behavior_agent_message_runtime.model_allowance``) until T5 replaces it, so those keep the API path.
    """
    collaborates = instruction.peer_work is not None or (
        instruction.capabilities is not None
        and any(grant.entry_id == "fa-agent-collaboration-v1" for grant in instruction.capabilities.grants))
    return instruction.model_provider == "openai-responses" and not collaborates


def responses_usage(raw: bytes):
    """Standard Responses usage counts (input, output[, reasoning]) from one stream, or None when unknown."""
    found = None
    try:
        for data in SseDataEvents().feed(raw):
            if data.strip() == b"[DONE]":
                continue
            try:
                value = json.loads(data)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(value, dict) or value.get("type") not in {"response.completed", "response.incomplete"}:
                continue
            response = value.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
            if not isinstance(usage, dict):
                return None
            counts = (usage.get("input_tokens"), usage.get("output_tokens"))
            if any(type(n) is not int or n < 0 for n in counts):
                return None
            details = usage.get("output_tokens_details")
            if details is not None:
                if not isinstance(details, dict):
                    return None
                reasoning = details.get("reasoning_tokens")
                if reasoning is not None:
                    if type(reasoning) is not int or not 0 <= reasoning <= counts[1]:
                        return None
                    counts = (*counts, reasoning)
            if found is not None and found != counts:
                return None
            found = counts
    except Exception:
        return None
    return found


def completions_usage(raw: bytes):
    """Only a complete Chat Completions stream's explicit usage is measurable."""
    found, done = None, False
    for data in SseDataEvents().feed(raw):
        if data.strip() == b"[DONE]":
            done = True
            continue
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            return None
        usage = value.get("usage") if isinstance(value, dict) else None
        if usage is None:
            continue
        if not isinstance(usage, dict):
            return None
        counts = (usage.get("prompt_tokens"), usage.get("completion_tokens"))
        if any(type(n) is not int or n < 0 for n in counts) or found is not None and found != counts:
            return None
        found = counts
    return found if done else None


class DirectResponses:
    wire_api = "responses"

    def __init__(self, url, token_provider, model, *, transport=None, inference=None):
        checked_url = url
        if self.wire_api == "completions":
            checked_url = (url[:-len("chat/completions")] + "responses"
                           if isinstance(url, str) and url.endswith("/chat/completions") else None)
        if not isinstance(checked_url, str) or _URL.fullmatch(checked_url) is None:
            raise Problem(503, "MODEL_DIRECT_CONFIGURATION", "only the project's selected inference endpoint is accepted")
        self.url, self.token_provider, self.model, self.transport = url, token_provider, model, transport
        self.requests = 0
        self.measured = []
        # Model responses handed to the runtime; any delivered output means the turn may have acted.
        self.delivered = 0
        self.refused_before_output = False
        self.inference = inference
        if inference is not None:
            inference.attach_transport()
        from .sdk_telemetry import current_traceparent
        # Tracing on (the default): every model request of this turn joins the turn's trace on the service side.
        self.traceparent = current_traceparent()
        # The runtime reads the stream on its own task, so the turn's FA_TIMING ID is kept from here.
        self.timing_job = current_work()

    @classmethod
    def from_environment(cls, token_provider, model, *, environ=None, transport=None):
        environ = os.environ if environ is None else environ
        url = environ.get(URL_ENV)
        if not url:
            return None
        log = logging.getLogger(__name__)
        if _URL.fullmatch(url) is None:
            log.warning("FA_MODEL_DIRECT_INACTIVE reason=config_invalid")
            return None
        if not TURN_USAGE_REPORTED:
            log.warning("FA_MODEL_DIRECT_INACTIVE reason=usage_not_reported")
            return None
        return cls(url, token_provider, model, transport=transport)

    async def send(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("model") != self.model or body.get("stream") is not True:
            raise Problem(403, "ASSISTANT_MODEL_REQUEST_INVALID", "only this turn's streamed model may be sent")
        if self.wire_api == "responses":
            body["store"] = False
        else:
            body["stream_options"] = {**body.get("stream_options", {}), "include_usage": True}
        observation, link = self.inference, request.extensions.get("fa_inference")
        if observation is not None and not (isinstance(link, InferenceLink) and link.owner is observation):
            link = observation.prepare(
                request.content, self.model, self.wire_api, source="direct",
                purpose="review" if request.extensions.get("fa_model_purpose") == "review" else "unknown")
        client = httpx.AsyncClient(transport=self.transport or httpx.AsyncHTTPTransport(retries=0), trust_env=False,
                                   follow_redirects=False, timeout=httpx.Timeout(60, connect=5))
        handed_off = False
        log = logging.getLogger(__name__)
        attempt_id = None
        started, recorded = perf_counter(), set()

        def timed(phase, outcome):
            # FA_TIMING once per phase and request: elapsed and outcome only, never body, token or headers.
            if phase not in recorded:
                recorded.add(phase)
                elapsed_timing(phase, started, job_id=self.timing_job, outcome=outcome)

        def ended(outcome):
            # A request that ends before any byte arrived has no first byte.
            timed("model_first_byte", "failed")
            timed("model_request", outcome)
        try:
            for attempt in range(MAX_THROTTLE_RETRIES + 1):
                token = await hosted_sync(self.token_provider)
                if inspect.isawaitable(token):
                    token = await token
                if not isinstance(token, str) or not token:
                    raise Problem(503, "HOSTED_MI_UNAVAILABLE", "the original Hosted model credential is unavailable")
                headers = {"Authorization": "Bearer " + token, "Accept-Encoding": "identity"}
                if self.traceparent is not None:
                    headers["traceparent"] = self.traceparent
                outgoing = client.build_request("POST", self.url, json=body, headers=headers)
                # Counted once its send starts: an unknown outcome may still have been charged.
                self.requests += 1
                if observation is not None:
                    attempt_id = observation.started(link, outgoing, self.model, self.wire_api)
                response = await client.send(outgoing, stream=True)
                if observation is not None:
                    observation.response(attempt_id, response)
                if response.status_code == 200:
                    break
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_ERROR_BYTES:
                        break
                await response.aclose()
                if observation is not None:
                    try:
                        error_payload = json.loads(raw) if len(raw) <= MAX_ERROR_BYTES else None
                    except (ValueError, UnicodeError):
                        error_payload = None
                    observation.provider_payload(attempt_id, error_payload)
                    observation.ended(attempt_id, state="failed")
                wait = throttle_wait(response, attempt) if response.status_code == 429 else None
                if wait is not None and attempt < MAX_THROTTLE_RETRIES:
                    # The same request, refused before any processing (official 429 guidance); never a new turn.
                    log.warning("FA_MODEL_DIRECT_RETRY status=429 attempt=%s wait_ms=%s source=%s",
                                attempt + 1, int(wait[0] * 1000), wait[1])
                    await asyncio.sleep(wait[0])
                    continue
                code = None
                value = None
                try:
                    value = json.loads(raw) if len(raw) <= MAX_ERROR_BYTES else None
                    code = value.get("error", {}).get("code") if isinstance(value, dict) else None
                except (ValueError, TypeError, AttributeError):
                    code = None
                # A definite provider refusal before this turn handed any model output to the runtime: nothing ran.
                self.refused_before_output = response.status_code in DEFINITE_REFUSALS and self.delivered == 0
                # Fixed identifiers and counts only; never the provider message, prompt or token.
                log.warning(
                    "FA_MODEL_DIRECT_HTTP_FAILURE status=%s wire_api=%s model=%s %s %s attempts=%s "
                    "before_output=%s", response.status_code, self.wire_api, _field(self.model),
                    _error_fields(value), _request_shape(body), attempt + 1, self.refused_before_output)
                if code in {"context_length_exceeded", "context_window_exceeded"}:
                    raise Problem(413, "ASSISTANT_CONTEXT_LIMIT",
                                  "selected provider rejected this context; no truncation or retry",
                                  upstream_error_code="ASSISTANT_CONTEXT_LIMIT", provider_status=response.status_code,
                                  upstream_error_phase="provider_response")
                raise Problem(503, "ASSISTANT_MODEL_OUTCOME_UNKNOWN", "original model HTTP request is unresolved",
                              upstream_error_code="ASSISTANT_PROVIDER_HTTP_FAILURE",
                              provider_status=response.status_code, upstream_error_phase="provider_response",
                              **_provider_error(value))
            sender = self

            class MeteredStream(BoundedStream):
                reading = False

                async def aclose(self):
                    try:
                        await super().aclose()
                    finally:
                        if observation is not None:
                            observation.closed(attempt_id)
                        if not self.reading:
                            ended("failed")

                async def __aiter__(self):
                    self.reading = True
                    raw = bytearray()
                    try:
                        async for chunk in super().__aiter__():
                            timed("model_first_byte", "returned")
                            raw.extend(chunk)
                            if observation is not None:
                                observation.chunk(attempt_id, chunk)
                            yield chunk
                    except BaseException:
                        ended("failed")
                        if observation is not None:
                            observation.ended(attempt_id, state="interrupted")
                        raise
                    # Reached only when the whole stream was read; an abandoned stream stays unmeasured.
                    ended("returned")
                    counts = (responses_usage(bytes(raw)) if sender.wire_api == "responses"
                              else completions_usage(bytes(raw)))
                    if counts is not None:
                        sender.measured.append(counts)
                    if observation is not None:
                        observation.ended(attempt_id, state="complete", complete=True, measured=counts is not None)

            result = httpx.Response(200, request=request,
                                    headers={"content-type": response.headers.get("content-type", "")},
                                    stream=MeteredStream(response, close=client.aclose))
            handed_off = True
            self.delivered += 1
            return result
        except BaseException:
            ended("failed")
            if observation is not None:
                observation.ended(attempt_id, state="failed")
            raise
        finally:
            if not handed_off:
                await client.aclose()
    def usage(self) -> TurnModelUsage:
        if not self.requests:
            return TurnModelUsage(schema_version=TURN_USAGE_SCHEMA, usage=())
        measured = len(self.measured)
        reasoning = (sum(counts[2] for counts in self.measured)
                     if measured and all(len(counts) == 3 for counts in self.measured) else None)
        return TurnModelUsage(schema_version=TURN_USAGE_SCHEMA, usage=(ModelUsage(
            model=self.model, wire_api=self.wire_api, requests=self.requests, measured_requests=measured,
            input_tokens=sum(counts[0] for counts in self.measured) if measured else None,
            output_tokens=sum(counts[1] for counts in self.measured) if measured else None,
            reasoning_tokens=reasoning),))


class DirectCompletions(DirectResponses):
    """N1 direct transport only; legacy Completions still uses the original API ledger."""

    wire_api = "completions"
