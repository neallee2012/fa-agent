"""PA-16 B: native model observations, with explicit SDK and HTTP doubles only."""

import json
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from shared_context import behavior_model_direct as direct
from shared_context.behavior_native_turn_wire import NativeSubmit
from shared_context.m2_copilot import _ModelGateway
from shared_context.native_inference import NativeInferenceCollector
from test_native_assembly import install_executor, offline_sdk, run_turn
from test_native_reasoning_facts import completions_stream
from test_native_responses import response_submit, responses  # noqa: F401
from test_native_turn_wire import submit
from test_native_turns import local_env  # noqa: F401


@pytest.mark.parametrize("lane", ["invocations", "responses"])
def test_logical_requests_and_transport_attempts_are_separate_without_changing_the_original_bill(
        responses, monkeypatch, tmp_path, lane):
    sent = []

    def provider(request):
        sent.append(request)
        if len(sent) < 3:
            return httpx.Response(429, headers={"retry-after-ms": "0"}, json={
                "error": {"code": "429", "message": "SECRET_PROVIDER_MESSAGE"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=completions_stream("OFFLINE-model"))

    async def sdk(**options):
        observer = getattr(options["native_assembly"], "inference", None)
        kwargs = {} if observer is None else {"inference": observer}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: pytest.fail("Only the original Direct sender may send this request"))) as http:
            endpoint = httpx.URL("https://offline.invalid/chat/completions")
            gateway = _ModelGateway(
                endpoint, http, options["on_event"], boundary=options["behavior_boundary"],
                model=options["model"], instruction=True, **kwargs)
            response = await gateway.send_request(httpx.Request("POST", endpoint, json={
                "model": options["model"], "stream": True,
                "messages": [{"role": "user", "content": "PRIVATE_PROMPT_CANARY"}],
            }), SimpleNamespace(request_id="sdk-request-one", session_id=options["sdk_session_id"]))
            await response.aread()
            await response.aclose()
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk, model_transport=httpx.MockTransport(provider))
    values = (response_submit() if lane == "responses" else submit()).model_dump(mode="json")
    values["capture"]["template"]["settings"]["model_provider"] = None
    request = NativeSubmit.model_validate_json(json.dumps(values))
    receipt = run_turn(responses, request)
    observation = receipt.assembly.model_dump(mode="json").get("inference")
    assert observation is not None, "B must persist the original request/attempt observations"
    assert observation["logical_prepared"] == 1 and observation["transport_started"] == 3
    assert len(observation["requests"]) == 1 and len(observation["attempts"]) == 3
    assert observation["requests"][0]["sdk_request_id"] == "sdk-request-one"
    assert observation["requests"][0]["gateway_fingerprint"] is not None
    assert [item["http_status"] for item in observation["attempts"]] == [429, 429, 200]
    assert [item["logical_id"] for item in observation["attempts"]] == [1, 1, 1]
    assert observation["full_request_tokens"] is None
    assert observation["full_request_tokens_reason"] == "counter_unavailable"
    assert observation["provider_usage_ref"] == "receipt.usage"
    usage = receipt.usage.usage[0]
    assert (usage.requests, usage.measured_requests, usage.input_tokens, usage.output_tokens) == (3, 1, 1, 1)
    assert len(sent) == 3
    assert "PRIVATE_PROMPT_CANARY" not in json.dumps(observation)
    assert "SECRET_PROVIDER_MESSAGE" not in json.dumps(observation)


@pytest.mark.parametrize("wire", ["responses", "completions"])
@pytest.mark.parametrize("failure", ["http-error", "broken-after-usage", "no-usage", "invalid-usage", "token"])
def test_provider_observations_keep_failed_request_values_without_marking_the_original_ledger_measured(
        wire, failure):
    from test_model_direct import URL, completed, sse
    collector = NativeInferenceCollector("sdk-one")
    sender_type = direct.DirectResponses if wire == "responses" else direct.DirectCompletions
    url = URL if wire == "responses" else URL.removesuffix("responses") + "chat/completions"
    usage = ({"input_tokens": 7, "output_tokens": 2} if wire == "responses" else
             {"prompt_tokens": 7, "completion_tokens": 2})
    raw = (completed(7, 2) if wire == "responses" else
           sse({"usage": usage}) + b"data: [DONE]\n\n")
    if failure == "no-usage":
        raw = sse({"type": "response.output_text.delta", "delta": "PRIVATE_ANSWER_CANARY"})
    if failure == "invalid-usage":
        invalid = {"input_tokens": True, "output_tokens": -1} if wire == "responses" else {
            "prompt_tokens": True, "completion_tokens": -1}
        raw = (sse({"type": "response.completed", "response": {"usage": invalid}}) if wire == "responses" else
               sse({"usage": invalid}) + b"data: [DONE]\n\n")

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw
            raise httpx.ReadError("PRIVATE_STREAM_FAILURE_CANARY")

    def provider(request):
        if failure == "http-error":
            return httpx.Response(500, json={"usage": usage, "error": {"message": "SECRET_ERROR_CANARY"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=Broken() if failure == "broken-after-usage" else httpx.ByteStream(raw))

    def token():
        if failure == "token":
            raise RuntimeError("SECRET_CREDENTIAL_CANARY")
        return "SECRET_TOKEN_CANARY"

    sender = sender_type(url, token, "model-one", inference=collector, transport=httpx.MockTransport(provider))

    async def run():
        response = await sender.send(httpx.Request("POST", url, json={
            "model": "model-one", "stream": True, "input" if wire == "responses" else "messages": []}))
        try:
            await response.aread()
        finally:
            await response.aclose()

    if failure in {"http-error", "broken-after-usage", "token"}:
        with pytest.raises(Exception):
            asyncio.run(run())
    else:
        asyncio.run(run())
    observation = collector.snapshot()
    assert observation.logical_prepared == 1
    if failure == "token":
        assert observation.transport_started == 0 and observation.attempts == ()
        assert sender.usage().usage == ()
    else:
        assert observation.transport_started == 1
        assert sender.usage().usage[0].measured_requests == 0
        assert sender.usage().usage[0].input_tokens is None
        attempt = observation.attempts[0]
        assert attempt.ledger_measured is False
        if failure in {"http-error", "broken-after-usage"}:
            assert (attempt.provider_usage.input_tokens, attempt.provider_usage.output_tokens) == (7, 2)
        elif failure == "no-usage":
            assert attempt.provider_usage is None and attempt.usage_reason == "not_reported"
        else:
            assert attempt.provider_usage.input_tokens is None and attempt.provider_usage.output_tokens is None
    assert "CANARY" not in observation.model_dump_json()


def test_sdk_reports_are_not_provider_bills_and_unverified_ids_are_never_joined():
    collector = NativeInferenceCollector("current-sdk")
    collector.prepare(b"{}", "model-one", "responses", source="gateway",
                      sdk_request_id="coincidental-id", sdk_session_id="current-sdk")
    event = {"type": "assistant.usage", "id": "usage-one", "data": {
        "model": "model-one", "api_call_id": "coincidental-id", "input_tokens": 8,
        "output_tokens": 3, "cache_read_tokens": 4, "reasoning_tokens": 2,
        "content": "SYSTEM_CANARY", "encrypted_content": "ENCRYPTED_CANARY", "_quota_snapshots": "SECRET_CANARY",
    }}
    collector.event(event)
    collector.event(event)
    collector.event({"type": "assistant.usage", "id": "foreign-event",
                     "data": {**event["data"], "session_id": "other-sdk", "input_tokens": 999}})
    collector.event({"type": "session.usage_info", "id": "context-one", "data": {
        "current_tokens": 500, "system_tokens": 100, "conversation_tokens": 380,
        "tool_definitions_tokens": 20, "token_limit": 1000, "messages_length": 7}})
    collector.event({"type": "session.shutdown", "data": {"input_tokens": 999}})
    result = collector.snapshot()
    assert len(result.sdk_usage) == 1
    usage = result.sdk_usage[0]
    assert usage.tokens.input_tokens == 8 and usage.tokens.reasoning_tokens == 2
    assert usage.request_match is None and usage.match_reason == "no_verified_cross_source_id"
    assert result.sdk_context[0].current_tokens == 500 and result.full_request_tokens is None
    assert result.transport_started is None and result.counter_reason == "observer_not_attached"
    assert result.provider_usage_ref == "receipt.usage"
    assert result.state == "partial" and result.reason == "source_unconfirmed"
    assert "CANARY" not in result.model_dump_json()


def test_sdk_missing_or_conflicting_event_ids_remain_explicit_not_zero():
    collector = NativeInferenceCollector("current-sdk")
    assert collector.snapshot().sdk_usage_reason == "no_event_observed"
    collector.event({"type": "assistant.usage", "data": {"model": "model-one", "input_tokens": None,
                                                      "output_tokens": False}})
    collector.event({"type": "assistant.usage", "id": "one", "data": {"model": "model-one", "input_tokens": 2}})
    collector.event({"type": "assistant.usage", "id": "one", "data": {"model": "model-one", "input_tokens": 9}})
    observed = collector.snapshot()
    assert len(observed.sdk_usage) == 2
    assert observed.sdk_usage[0].tokens.input_tokens is None and observed.sdk_usage[0].tokens.output_tokens is None
    assert observed.sdk_usage[0].dedup_reason == "event_id_missing"
    assert observed.sdk_usage[1].tokens.input_tokens == 2
    assert observed.reason == "conflicting_event" and observed.billing_effect == "none-observation-only"


def test_old_a_metadata_and_result_digest_do_not_gain_inference_on_roundtrip():
    from shared_context.native_assembly import NativeAssembly
    from shared_context.behavior_native_turn_wire import material_digest
    old = ('{"schema_version":"fa-native-assembly-v1","state":"partial","reason":"not_reached",'
           '"tools_scope":"fa-bound-tool-specs","tools_count":null,"tools_reason":"not_recorded",'
           '"sdk_binding_observed":null,"sdk_binding_reason":"not_recorded",'
           '"event_scope":"observed-metadata-not-exhaustive-history"}')
    parsed = NativeAssembly.model_validate_json(old)
    assert parsed.inference is None and parsed.model_dump_json() == old
    assert parsed.observed_inference.state == "unknown" and parsed.observed_inference.reason == "not_recorded"
    assert "inference" not in parsed.model_dump(mode="json")
    import hashlib
    assert material_digest(parsed) == hashlib.sha256(json.dumps(
        json.loads(old), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_real_runner_usage_projection_is_separate_from_public_progress(tmp_path, monkeypatch, caplog):
    from shared_context.native_assembly import NativeAssemblyCollector
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from test_instruction_tool_feedback import run_callbacks
    request = submit()
    receipt = NativeReceipt(
        agent_id="agent-one", hosted_session_id="host-one", request_id="rid-1", conversation_id="cid-one",
        command=request.command, capture=request.capture, capture_digest=request.capture.sha256,
        submit_digest=request.sha256, status="started")
    collector = NativeAssemblyCollector(receipt)
    collector.inference = NativeInferenceCollector("offline-instruction-feedback")
    event = {"type": "assistant.usage", "id": "usage-one", "data": {
        "input_tokens": 13, "output_tokens": 5, "model": "model-one", "api_call_id": "sdk-api-one",
        "content": "SYSTEM_CANARY", "reasoning": "REASONING_CANARY", "token": "SECRET_CANARY"}}

    async def callback(options, _sid):
        options["on_event"](event)
        options["on_event"](event)

    async def no_tool(_):
        pytest.fail("metadata observation cannot invoke a tool")

    public = run_callbacks(tmp_path, monkeypatch, callback, no_tool, native_assembly=collector)
    observed = collector.snapshot(complete=True).inference
    assert len(observed.sdk_usage) == 1 and observed.sdk_usage[0].tokens.input_tokens == 13
    assert observed.transport_started is None and observed.logical_prepared is None
    assert observed.counter_reason == "observer_not_attached"
    assert all("data" not in item for item in public)
    assert "CANARY" not in observed.model_dump_json() + json.dumps(public) + caplog.text


@pytest.mark.parametrize("failure", ["sdk", "metadata", "bound"])
def test_original_turn_persists_partial_observations_without_reexecuting_or_losing_a_successful_answer(
        responses, monkeypatch, tmp_path, failure):
    from shared_context import native_inference
    calls = []

    if failure == "bound":
        monkeypatch.setattr(native_inference, "MAX_INFERENCE_BYTES", 1500)
    elif failure == "metadata":
        def invalid(**_):
            raise ValueError("SECRET_OBSERVER_CANARY")
        monkeypatch.setattr(native_inference, "LogicalRequest", invalid)

    async def sdk(**options):
        calls.append(True)
        collector = options["native_assembly"].inference
        for index in range(36 if failure == "bound" else 1):
            collector.prepare(b'{"content":"PRIVATE_CANARY"}', "model-one", "responses",
                              source="gateway", sdk_request_id="request-" + str(index),
                              sdk_session_id=options["sdk_session_id"])
        collector.event({"type": "assistant.usage", "id": "failed-call-usage",
                         "data": {"model": "model-one", "input_tokens": 2, "output_tokens": 1}})
        if failure == "sdk":
            raise RuntimeError("PRIVATE_FAILURE_CANARY")
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk)
    request = response_submit()
    assert responses.invoke(request).status_code == 202
    assert responses.respond(responses.call(responses.runtime.read, "rid-1")).status_code == 200
    result = responses.wait("rid-1", "unknown" if failure == "sdk" else "completed")
    observed = result.assembly.inference
    assert observed.sdk_usage[0].tokens.input_tokens == 2
    assert observed.transport_started == 0 and result.usage.usage == ()
    if failure == "metadata":
        assert observed.state == "partial" and observed.reason == "observer_failed"
    elif failure == "bound":
        assert observed.state == "partial" and observed.omitted_observations > 0
        assert len(observed.model_dump_json().encode()) <= 1500
    before = result.assembly.model_dump_json()
    from shared_context.behavior_native_turn_wire import NativeReceipt
    assert NativeReceipt.model_validate_json(responses.invoke(request).content).assembly.model_dump_json() == before
    assert calls == [True] and "CANARY" not in before


@pytest.mark.parametrize("purpose", ["compaction", "delegation", "unknown"])
def test_gateway_purpose_uses_only_its_original_verified_execution_context(purpose):
    from test_native_reasoning_facts import completions_stream
    collector = NativeInferenceCollector("sdk-one")

    class Delegation:
        async def model_ref(self, context):
            assert context.session_id == "sdk-one"
            return SimpleNamespace(child_agent_id="child-one")

    class Files:
        checkpoint_error = None
        compacting = purpose == "compaction"

        def observe_model_request(self, digest):
            assert len(digest) == 64

    async def run():
        async def reserve(_):
            return None
        async def token(_):
            return "TOKEN_CANARY"
        from shared_context.behavior_assistant_boundary import AssistantModelBoundary
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, request=request, headers={"content-type": "text/event-stream"},
                content=completions_stream("model-one")))) as client:
            endpoint = httpx.URL("https://offline.invalid/chat/completions")
            gateway = _ModelGateway(
                endpoint, client, lambda _: None, model="model-one", instruction=True, inference=collector,
                session_files=Files(), delegation_runtime=Delegation() if purpose == "delegation" else None,
                private_token=token, boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True))
            response = await gateway.send_request(httpx.Request("POST", endpoint, json={
                "model": "model-one", "stream": True, "messages": [{"role": "user", "content": "compaction"}],
            }), SimpleNamespace(request_id="request-one", session_id="sdk-one"))
            await response.aread()
            await response.aclose()
    asyncio.run(run())
    request = collector.snapshot().requests[0]
    assert request.purpose == purpose
    assert request.purpose_reason == ("not_reported" if purpose == "unknown" else None)
    # Merely writing "compaction" in the prompt never assigns a purpose.
    assert collector.snapshot().transport_started is None
