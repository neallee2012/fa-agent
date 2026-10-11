"""ADR-0006 §5 T4b: the default-off direct Responses sender and its per-request routing in the Hosted turn.

Offline: the credential, provider and API are local doubles; no network, runtime process or model call.
"""

import asyncio
import json
import logging
import socket
from types import SimpleNamespace

import httpx
import pytest

from shared_context import behavior_assistant_hosted as hosted
from shared_context import behavior_assistant_wire as aw
from shared_context import behavior_model_direct as direct
from shared_context.errors import Problem

URL = "https://fa-acct.services.ai.azure.com/api/projects/fa-project/openai/v1/responses"
TOKEN = "direct-secret-token"
MODEL = "gpt-5.4-mini"


def sse(*events):
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()


def completed(input_tokens=120, output_tokens=30, reasoning=None):
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if reasoning is not None:
        usage["output_tokens_details"] = {"reasoning_tokens": reasoning}
    return sse({"type": "response.output_text.delta", "delta": "hi"},
               {"type": "response.completed", "response": {"usage": usage}})


def provider(sent, *, status=200, content=None):
    def handle(request):
        sent.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"code": content or "server_error", "message": TOKEN}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=completed() if content is None else content)
    return httpx.MockTransport(handle)


def body(**values):
    return {"model": MODEL, "input": [{"role": "user", "content": "hi"}], "stream": True, "store": True, **values}


async def send(sender, payload):
    response = await sender.send(httpx.Request("POST", "https://api.offline.invalid/model", json=payload))
    return b"".join([chunk async for chunk in response.aiter_bytes()])


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import _socket

    def forbidden(sock, address):
        # asyncio's own event-loop self-pipe on Windows uses loopback; any other connection fails.
        if address[0] not in {"127.0.0.1", "::1"}:
            pytest.fail("direct model tests must not reach the network")
        return _socket.socket.connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def test_inactive_unless_configured_valid_and_its_usage_is_reported(monkeypatch, caplog):
    token = lambda: TOKEN
    assert direct.DirectResponses.from_environment(token, MODEL, environ={}) is None
    with caplog.at_level(logging.WARNING):
        assert direct.DirectResponses.from_environment(
            token, MODEL, environ={direct.URL_ENV: "https://evil.example/openai/v1/responses"}) is None
        # A direct call would be spend with no ledger entry until the end receipt carries its usage.
        assert direct.DirectResponses.from_environment(token, MODEL, environ={direct.URL_ENV: URL}) is None
    assert "reason=config_invalid" in caplog.text and "reason=usage_not_reported" in caplog.text
    monkeypatch.setattr(direct, "TURN_USAGE_REPORTED", True)
    assert isinstance(direct.DirectResponses.from_environment(token, MODEL, environ={direct.URL_ENV: URL}),
                      direct.DirectResponses)


def test_one_send_to_the_project_endpoint_and_its_usage(caplog):
    sent = []
    sender = direct.DirectResponses(URL, lambda: TOKEN, MODEL, transport=provider(sent))
    with caplog.at_level(logging.INFO):
        assert b"response.completed" in asyncio.run(send(sender, body()))
    assert len(sent) == 1 and str(sent[0].url) == URL and sent[0].headers["authorization"] == "Bearer " + TOKEN
    assert json.loads(sent[0].content)["store"] is False
    usage = sender.usage()
    assert usage.state == "complete" and usage.usage[0].requests == 1
    assert (usage.usage[0].input_tokens, usage.usage[0].output_tokens) == (120, 30)
    assert TOKEN not in caplog.text


def test_unknown_outcomes_are_counted_but_never_measured():
    sent = []
    sender = direct.DirectResponses(URL, lambda: TOKEN, MODEL, transport=provider(sent, status=500))
    with pytest.raises(Problem) as failed:
        asyncio.run(send(sender, body()))
    assert failed.value.status == 503 and failed.value.body["error"]["provider_status"] == 500
    context = direct.DirectResponses(URL, lambda: TOKEN, MODEL,
                                     transport=provider(sent, status=400, content="context_length_exceeded"))
    with pytest.raises(Problem) as limit:
        asyncio.run(send(context, body()))
    assert limit.value.body["error"]["code"] == "ASSISTANT_CONTEXT_LIMIT"
    # A stream without usage, and a stream abandoned before its end.
    no_usage = direct.DirectResponses(URL, lambda: TOKEN, MODEL,
                                      transport=provider(sent, content=sse({"type": "response.output_text.delta"})))
    asyncio.run(send(no_usage, body()))

    async def abandon():
        response = await no_usage.send(httpx.Request("POST", "https://api.offline.invalid/model", json=body(n=1)))
        async for _ in response.aiter_bytes():
            break
        await response.aclose()
    asyncio.run(abandon())
    for sender, requests in ((sender, 1), (context, 1), (no_usage, 2)):
        usage = sender.usage().usage[0]
        assert usage.requests == requests and usage.measured_requests == 0 and usage.input_tokens is None


def test_each_direct_request_times_its_first_byte_and_end_under_the_turn_timing_id(caplog):
    from shared_context.behavior_timing import work_timing

    sent = []
    # Built inside the turn; the runtime then reads the stream on a task outside that context.
    with work_timing("job-model"):
        ok = direct.DirectResponses(URL, lambda: TOKEN, MODEL, transport=provider(sent))
        refused = direct.DirectResponses(URL, lambda: TOKEN, MODEL, transport=provider(sent, status=500))
    caplog.set_level(logging.WARNING, logger="shared_context.behavior_timing")
    asyncio.run(send(ok, body()))
    with pytest.raises(Problem):
        asyncio.run(send(refused, body()))

    async def abandoned():
        response = await ok.send(httpx.Request("POST", "https://api.offline.invalid/model", json=body(n=1)))
        async for _ in response.aiter_bytes():
            break
        await response.aclose()

    async def unread():
        response = await ok.send(httpx.Request("POST", "https://api.offline.invalid/model", json=body(n=2)))
        await response.aclose()
    asyncio.run(abandoned())
    asyncio.run(unread())
    timings = [json.loads(record.getMessage().removeprefix("FA_TIMING ")) for record in caplog.records
               if record.getMessage().startswith("FA_TIMING ")]
    assert [(value["phase"], value["outcome"]) for value in timings] == [
        ("model_first_byte", "returned"), ("model_request", "returned"),
        ("model_first_byte", "failed"), ("model_request", "failed"),
        ("model_first_byte", "returned"), ("model_request", "failed"),
        ("model_first_byte", "failed"), ("model_request", "failed"),
    ]
    assert {value["job_id"] for value in timings} == {"job-model"}
    assert all(set(value) == {"phase", "job_id", "elapsed_ms", "outcome"} for value in timings)
    assert TOKEN not in caplog.text and "response.completed" not in caplog.text


def test_another_model_or_an_unstreamed_request_is_refused_before_sending():
    sent = []
    sender = direct.DirectResponses(URL, lambda: TOKEN, MODEL, transport=provider(sent))
    for payload in (body(model="other-model"), body(stream=False)):
        with pytest.raises(Problem) as refused:
            asyncio.run(send(sender, payload))
        assert refused.value.status == 403
    assert sent == [] and sender.usage().state == "none"


def test_reasoning_is_summed_only_when_every_measured_request_reported_it():
    sent = []
    both = direct.DirectResponses(URL, lambda: TOKEN, MODEL,
                                  transport=provider(sent, content=completed(100, 40, reasoning=10)))
    asyncio.run(send(both, body(n=1)))
    asyncio.run(send(both, body(n=2)))
    assert both.usage().usage[0].reasoning_tokens == 20
    assert direct.responses_usage(completed(5, 3, reasoning=4)) is None
    assert direct.responses_usage(completed(5, 3) + completed(5, 4)) is None


def _turn(tmp_path, monkeypatch, calls, *, enabled, peer=False):
    stop = aw.SdkStop(runtime_version="1.0.83", pid=42, exit_code=0, graceful=True)

    async def send_model(request, **options):
        calls.append(("api", options))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=completed(),
                              request=request)
    peer_work = None if not peer else aw.PeerWorkReference(
        root_id="root", message_id="message", message_key="message-key", message_sha256="a" * 64,
        content_sha256="b" * 64, sender_agent_id="agent-a", recipient_agent_id="agent-b",
        recipient_work_id="work-b", conversation_id="conversation")
    client = SimpleNamespace(
        assignment=SimpleNamespace(task=SimpleNamespace(instruction=aw.InstructionTask(
            content="OFFLINE", conversation_id="conversation", turn_number=2, model=MODEL,
            model_provider="openai-responses", peer_work=peer_work)), sdk_session_id="sdk", resume=True),
        api_url="https://offline.invalid", token_provider=lambda: "OFFLINE", model_token=lambda: TOKEN,
        reserve_model=lambda digest: calls.append(("reserve", digest)), send_model=send_model,
        save=lambda _: None, progress=lambda _: None,
        save_text_reply=lambda *args: aw.ToolReceipt(state="ready_to_finalize", result_digest="a" * 64),
        finish_failed=lambda *args: aw.ToolReceipt(state="ready_to_finalize", result_digest="a" * 64),
    )
    sent = []
    monkeypatch.setenv(direct.URL_ENV, URL)
    monkeypatch.setattr(direct, "TURN_USAGE_REPORTED", enabled)
    original = direct.DirectResponses.from_environment.__func__
    monkeypatch.setattr(direct.DirectResponses, "from_environment", classmethod(
        lambda cls, token, model, **kw: original(cls, token, model, transport=provider(sent))))
    session = hosted.AssistantSession(client, root=tmp_path, runtime_path=tmp_path / "not-executed")

    async def sdk(**options):
        boundary = options["behavior_boundary"]
        for compacting in (False, True):
            session._session_files = SimpleNamespace(compacting=compacting) if compacting else None
            payload = body(n=int(compacting))
            request = httpx.Request("POST", "https://offline.invalid/model/v1/responses", json=payload)
            await boundary.before_request(direct.request_fingerprint(payload))
            response = await boundary.response(await boundary.sender(request))
            async for _ in response.aiter_bytes():
                pass
        session._session_files = None
        options["on_sdk_stopped"](stop.model_dump())
        return {"final_message": "OFFLINE answer", "sdk_stop": stop.model_dump()}
    monkeypatch.setattr(hosted, "run_copilot", sdk)
    monkeypatch.setattr(session, "_archive", lambda: aw.ArchiveUnavailable(error="SDK_ARCHIVE_UNAVAILABLE"))
    asyncio.run(session._run(tmp_path))
    return session, sent


def test_the_turn_sends_ordinary_requests_direct_and_compaction_through_the_api(tmp_path, monkeypatch):
    calls = []
    session, sent = _turn(tmp_path, monkeypatch, calls, enabled=True)
    # The first request went direct with no API reserve or consume; the compaction kept the API path.
    assert len(sent) == 1 and json.loads(sent[0].content)["n"] == 0
    assert [kind for kind, _ in calls] == ["reserve", "api"] and calls[1][1] == {"checkpoint_compaction": True}
    usage = session.turn_model_usage()
    assert usage.state == "complete" and usage.usage[0].requests == 1


def test_without_the_usage_report_the_whole_turn_keeps_the_api_path(tmp_path, monkeypatch):
    calls = []
    session, sent = _turn(tmp_path, monkeypatch, calls, enabled=False)
    assert sent == [] and [kind for kind, _ in calls] == ["reserve", "api", "reserve", "api"]
    assert session.turn_model_usage() is None


def test_a_peer_turn_keeps_the_api_path_and_its_combined_allowance(tmp_path, monkeypatch):
    # The API reserve still enforces the A/B combined model allowance until T5 replaces it.
    calls = []
    session, sent = _turn(tmp_path, monkeypatch, calls, enabled=True, peer=True)
    assert sent == [] and [kind for kind, _ in calls] == ["reserve", "api", "reserve", "api"]
    assert session.turn_model_usage() is None


def test_only_responses_turns_outside_the_combined_allowance_are_eligible():
    grant = SimpleNamespace(entry_id="fa-agent-collaboration-v1")
    other = SimpleNamespace(entry_id="playwright")

    def task(provider="openai-responses", peer=None, grants=None):
        return SimpleNamespace(model_provider=provider, peer_work=peer,
                               capabilities=None if grants is None else SimpleNamespace(grants=grants))
    assert direct.direct_eligible(task())
    assert direct.direct_eligible(task(grants=(other,)))
    assert not direct.direct_eligible(task(provider=None))
    assert not direct.direct_eligible(task(peer=SimpleNamespace()))
    # The originating A turn holding the collaboration capability is also under the combined allowance.
    assert not direct.direct_eligible(task(grants=(other, grant)))


def test_native_completions_direct_transport_reports_explicit_usage_without_legacy_double_counting():
    sent = []
    raw = sse({"choices": [{"delta": {"content": "original"}, "finish_reason": None}], "usage": None},
              {"choices": [], "usage": {"prompt_tokens": 23, "completion_tokens": 9}}) + b"data: [DONE]\n\n"
    url = URL.removesuffix("responses") + "chat/completions"
    sender = direct.DirectCompletions(url, lambda: TOKEN, MODEL, transport=provider(sent, content=raw))
    asyncio.run(send(sender, {"model": MODEL, "messages": [{"role": "user", "content": "original"}], "stream": True}))
    assert str(sent[0].url) == url
    assert json.loads(sent[0].content)["stream_options"]["include_usage"] is True
    usage = sender.usage().usage[0]
    assert (usage.wire_api, usage.requests, usage.input_tokens, usage.output_tokens) == ("completions", 1, 23, 9)
    assert direct.completions_usage(raw.removesuffix(b"data: [DONE]\n\n")) is None
    assert direct.completions_usage(raw + sse({"usage": {"prompt_tokens": 24, "completion_tokens": 9}})) is None


def test_native_async_managed_identity_token_stays_in_transport_not_request_body():
    sent = []

    async def token():
        return TOKEN

    sender = direct.DirectResponses(URL, token, MODEL, transport=provider(sent))
    asyncio.run(send(sender, body()))
    assert sent[0].headers["authorization"] == "Bearer " + TOKEN
    assert TOKEN.encode() not in sent[0].content


def test_a_provider_refusal_logs_fixed_identifiers_and_request_shape_only(caplog):
    canary = "CANARY prompt text"

    def handle(request):
        return httpx.Response(400, json={"error": {
            "code": "content_filter", "type": "invalid_request_error", "param": "messages[1].content",
            "message": canary + " " + TOKEN, "innererror": {"code": "ResponsibleAIPolicyViolation",
                                                           "content_filter_result": {"jailbreak": {"filtered": True}}}}})
    url = URL.removesuffix("responses") + "chat/completions"
    sender = direct.DirectCompletions(url, lambda: TOKEN, MODEL, transport=httpx.MockTransport(handle))
    payload = {"model": MODEL, "stream": True, "temperature": 0, "tools": [{"type": "function"}] * 3, "messages": [
        {"role": "system", "content": canary},
        {"role": "user", "content": [{"type": "text", "text": canary},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]}
    with caplog.at_level(logging.WARNING), pytest.raises(Problem) as failed:
        asyncio.run(send(sender, payload))
    assert failed.value.status == 503 and failed.value.body["error"]["provider_status"] == 400
    assert (failed.value.body["error"]["provider_error_code"], failed.value.body["error"]["provider_error_param"]) == (
        "content_filter", "messages[1].content")
    line = next(record.getMessage() for record in caplog.records
                if record.getMessage().startswith("FA_MODEL_DIRECT_HTTP_FAILURE"))
    assert line == ("FA_MODEL_DIRECT_HTTP_FAILURE status=400 wire_api=completions model=gpt-5.4-mini "
                    "error_code=content_filter error_type=invalid_request_error error_param=messages[1].content "
                    "inner_code=ResponsibleAIPolicyViolation "
                    "items=2 tools=3 images=1 keys=messages,model,stream,stream_options,temperature,tools "
                    "attempts=1 before_output=True")
    assert canary not in caplog.text and TOKEN not in caplog.text and "base64" not in caplog.text
    caplog.clear()
    odd = direct.DirectCompletions(url, lambda: TOKEN, MODEL, transport=httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": {"code": canary, "param": None}})))
    with caplog.at_level(logging.WARNING), pytest.raises(Problem) as odd_failed:
        asyncio.run(send(odd, {"model": MODEL, "stream": True, "messages": []}))
    assert "error_code=invalid error_type=none error_param=none inner_code=none" in caplog.text
    assert canary not in caplog.text
    assert not {"provider_error_code", "provider_error_param"} & set(odd_failed.value.body["error"])
    caplog.clear()
    html = direct.DirectCompletions(url, lambda: TOKEN, MODEL, transport=httpx.MockTransport(
        lambda request: httpx.Response(400, text="<html>" + canary + "</html>")))
    with caplog.at_level(logging.WARNING), pytest.raises(Problem):
        asyncio.run(send(html, {"model": MODEL, "stream": True, "messages": []}))
    assert "error_code=none error_type=none error_param=none inner_code=none items=0" in caplog.text
    assert canary not in caplog.text


def throttled(statuses, sent, *, headers=None):
    """A provider answering each request with the next status (429/4xx/5xx/200)."""
    remaining = list(statuses)

    def handle(request):
        sent.append(request)
        status = remaining.pop(0)
        if status == 200:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=completed())
        return httpx.Response(status, headers=headers if status == 429 else None,
                              json={"error": {"code": "429" if status == 429 else "bad_request", "message": TOKEN}})
    return httpx.MockTransport(handle)


@pytest.fixture
def waits(monkeypatch):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(direct.asyncio, "sleep", sleep)
    return slept


def test_a_throttled_request_is_resent_after_retry_after_and_then_succeeds(waits, caplog):
    sent = []
    sender = direct.DirectResponses(URL, lambda: TOKEN, MODEL,
                                    transport=throttled([429, 429, 200], sent, headers={"retry-after-ms": "1500"}))
    with caplog.at_level(logging.WARNING):
        assert b"response.completed" in asyncio.run(send(sender, body()))
    assert waits == [1.5, 1.5] and len(sent) == 3 and sent[0].content == sent[2].content
    assert sender.delivered == 1 and sender.refused_before_output is False
    usage = sender.usage().usage[0]
    assert (usage.requests, usage.measured_requests) == (3, 1)
    assert caplog.text.count("FA_MODEL_DIRECT_RETRY status=429") == 2 and "source=retry-after-ms" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("statuses,headers,expected_waits,refused", [
    ([429, 429, 429], None, 2, True),                     # bounded: two retries, then a definite refusal
    ([429], {"retry-after": "120"}, 0, True),            # a wait over the bound is not slept
    ([400], None, 0, True),                              # rejected before processing
    ([500], None, 0, False),                             # server error: the outcome stays unknown
])
def test_a_definite_refusal_before_any_output_is_marked_and_never_resent_beyond_the_bound(
        waits, statuses, headers, expected_waits, refused):
    sent = []
    sender = direct.DirectCompletions(URL.removesuffix("responses") + "chat/completions", lambda: TOKEN, MODEL,
                                      transport=throttled(statuses, sent, headers=headers))
    with pytest.raises(Problem) as failed:
        asyncio.run(send(sender, {"model": MODEL, "stream": True, "messages": []}))
    assert failed.value.status == 503 and len(waits) == expected_waits and len(sent) == len(statuses)
    assert sender.refused_before_output is refused and sender.usage().usage[0].requests == len(statuses)
    assert all(0 < wait <= direct.MAX_THROTTLE_WAIT_SECONDS for wait in waits)


def test_a_refusal_after_model_output_was_delivered_stays_unknown(waits):
    sent = []
    sender = direct.DirectResponses(URL, lambda: TOKEN, MODEL, transport=throttled([200, 400], sent))
    asyncio.run(send(sender, body()))
    with pytest.raises(Problem):
        asyncio.run(send(sender, body(n=2)))
    assert sender.delivered == 1 and sender.refused_before_output is False


def test_a_refusal_recorded_before_another_request_of_the_turn_delivered_output_stays_unknown(waits):
    # e.g. a background compaction exhausts its retries while the turn's own request is still retrying and then succeeds
    from shared_context.native_sdk import native_failure_code
    sent = []
    sender = direct.DirectResponses(URL, lambda: TOKEN, MODEL, transport=throttled([429, 429, 429, 200], sent))
    with pytest.raises(Problem):
        asyncio.run(send(sender, body()))
    assert sender.refused_before_output is True
    asyncio.run(send(sender, body(n=2)))
    assert sender.delivered == 1
    assert native_failure_code(RuntimeError("Model transport halted"), sender) == "NATIVE_TURN_UNKNOWN"


def test_only_a_refusal_before_output_turns_an_unknown_turn_into_a_resendable_one():
    from types import SimpleNamespace as N
    from shared_context.native_sdk import native_failure_code
    refused, ran = N(refused_before_output=True, delivered=0), N(refused_before_output=False, delivered=0)
    unknown = Problem(503, "ASSISTANT_MODEL_OUTCOME_UNKNOWN", "x")
    assert native_failure_code(RuntimeError("halted"), refused) == "NATIVE_MODEL_REFUSED"
    assert native_failure_code(unknown, refused) == "NATIVE_MODEL_REFUSED"
    assert native_failure_code(Problem(413, "ASSISTANT_CONTEXT_LIMIT", "x"), refused) == "ASSISTANT_CONTEXT_LIMIT"
    assert native_failure_code(RuntimeError("halted"), ran) == "NATIVE_TURN_UNKNOWN"
    assert native_failure_code(unknown, ran) == "ASSISTANT_MODEL_OUTCOME_UNKNOWN"
    assert native_failure_code(RuntimeError("halted"), None) == "NATIVE_TURN_UNKNOWN"


def test_a_provider_refusal_reaches_the_public_steps_as_fixed_identifiers_only():
    # The 2026-10-04 stage case: Azure Responses rejected gpt-6-luna's reasoning.effort before any output.
    from shared_context.behavior_assistant_progress import provider_refusal, public_event
    from shared_context.behavior_http_diagnostics import model_failure
    canary = "CANARY provider message"
    sender = direct.DirectResponses(URL, lambda: TOKEN, MODEL, transport=httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": {
            "code": "unsupported_parameter", "type": "invalid_request_error", "param": "reasoning.effort",
            "message": canary + " " + TOKEN}})))
    with pytest.raises(Problem) as failed:
        asyncio.run(send(sender, body()))
    failure = model_failure(failed.value, phase="transport")
    at = "2026-10-04T09:43:59+00:00"
    for kind in ("m2.provider_failed", "session.error"):
        event = public_event({"type": kind, "timestamp": at, "data": {}}, 1, {}, model_failure=failure)
        assert (event.kind, event.status) == ("error", "failed")
        assert event.summary == "Model provider refused: status=400 code=unsupported_parameter param=reasoning.effort"
    assert canary not in json.dumps(failure) and TOKEN not in json.dumps(failure)
    # A 5xx, a 408 or a transport failure keeps the existing unresolved step; only a definite refusal is named.
    server = model_failure(Problem(503, "ASSISTANT_MODEL_OUTCOME_UNKNOWN", "x",
                                   upstream_error_code="ASSISTANT_PROVIDER_HTTP_FAILURE", provider_status=500,
                                   provider_error_code="server_error"), phase="transport")
    timeout = model_failure(Problem(503, "ASSISTANT_MODEL_OUTCOME_UNKNOWN", "x",
                                    upstream_error_code="ASSISTANT_PROVIDER_HTTP_FAILURE", provider_status=408),
                            phase="transport")
    assert provider_refusal(server) is None and provider_refusal(timeout) is None
    assert provider_refusal(model_failure(RuntimeError("x"), phase="transport")) is None
    assert public_event({"type": "session.error", "timestamp": at, "data": {}}, 2, {},
                        model_failure=server).summary == "SDK reported an unresolved error"
