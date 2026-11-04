"""The model path as the SDK sees it: what reaches the wire, what is refused, in which order, and what ends a turn."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import create_autospec

import httpx
import pytest
from copilot.session_events import (
    AssistantMessageData, AssistantMessageToolRequest, SessionErrorData, SessionIdleData, SessionMode,
    SessionShutdownData, ToolExecutionCompleteData, ToolExecutionStartData, UserMessageData,
)

from shared_context import sdk_telemetry
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_delegation_wire import DelegationTask
from shared_context.behavior_http_diagnostics import AssistantModelFailure
from shared_context.errors import Problem
from shared_context.m2_copilot import (
    CAPABILITY_IMAGE_BODY_MIB, MAX_OUTPUT_TOKENS, MAX_PROMPT_TOKENS, MODEL, MODEL_IMAGE_URL_CHARS, SdkUserCancelled,
    _ModelGateway, _omit_unsendable_images, _retain_latest_inline_png, _send_until_idle, show_on_desktop,
)

COMPLETIONS = httpx.URL("https://context.invalid/model/v1/chat/completions")
RESPONSES = httpx.URL("https://context.invalid/model/v1/responses")
CTX = SimpleNamespace(request_id="sdk-request", session_id="sdk-session")
PNG = "data:image/png;base64,AAAA"
JPEG = "data:image/jpeg;base64,AAAA"


class Wire:
    """One gateway and everything it did, in order."""

    def __init__(self, endpoint=COMPLETIONS, reply=None, error=None, **options):
        self.endpoint, self.reply, self.error = endpoint, reply, error
        self.log, self.sent, self.events = [], [], []
        self.gateway = _ModelGateway(endpoint, SimpleNamespace(send=self.send), self.emit, **options)

    async def send(self, request, stream=False):
        self.log.append("http")
        self.sent.append((request, stream))
        if self.error is not None:
            raise self.error
        return self.reply or httpx.Response(200, json={})

    def emit(self, event):
        self.log.append(event["type"])
        self.events.append(event)

    def post(self, body, content=None, ctx=CTX):
        return asyncio.run(self.gateway.send_request(self.request(body, content), ctx))

    def request(self, body, content=None, **options):
        if content is not None:
            return httpx.Request("POST", self.endpoint, content=content, **options)
        return httpx.Request("POST", self.endpoint, json=body, **options)

    def failures(self):
        return [event["data"]["phase"] for event in self.events if event["type"] == "m2.provider_failed"]

    @property
    def body(self):
        return json.loads(self.sent[-1][0].content)


def boundary_double(log, sender=None, error=None, delivered=None):
    boundary = create_autospec(AssistantModelBoundary, instance=True)
    boundary.failure, boundary.sender = None, sender

    def before_request(digest):
        log.append(("before_request", digest))
        if error is not None:
            raise error

    boundary.before_request.side_effect = before_request
    if delivered is not None:
        boundary.response.side_effect = lambda response: delivered
    return boundary


def body(**fields):
    return {"model": MODEL, "messages": [{"role": "user", "content": "hello"}], **fields}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


# Admission: every refusal happens before the body is even parsed, in this order.

def test_a_prior_failure_refuses_before_anything_else_and_reports_nothing_new():
    wire = Wire(model_call_limit=0)
    wire.gateway.failure = {"phase": "transport"}
    with pytest.raises(RuntimeError, match="^Model transport halted after prior failure; retry refused$"):
        wire.post(None, content=b"not json")
    assert wire.events == [] and wire.gateway.attempts == 0


def test_a_boundary_failure_also_halts_the_gateway():
    boundary = boundary_double([])
    boundary.failure = {"upstream_error_code": "earlier"}
    wire = Wire(boundary=boundary, session_files=SimpleNamespace(checkpoint_error="SEEN"))
    with pytest.raises(RuntimeError, match="retry refused"):
        wire.post(None, content=b"not json")
    boundary.record_failure.assert_not_called()


def test_an_unapproved_compaction_refuses_with_its_own_code():
    boundary = boundary_double([])
    wire = Wire(boundary=boundary, model_call_limit=0, session_files=SimpleNamespace(checkpoint_error="SDK_STATE_X"))
    with pytest.raises(RuntimeError, match="^Model transport halted after unapproved compaction$"):
        wire.post(None, content=b"not json")
    assert wire.failures() == ["authority"]
    problem = boundary.record_failure.call_args.args[0]
    assert isinstance(problem, Problem) and problem.status == 409
    assert problem.body["error"]["code"] == "SDK_STATE_X"
    assert problem.body["error"]["message"] == "unapproved compaction cannot dispatch a model request"


@pytest.mark.parametrize("instruction, attempts, refused", [(False, 119, False), (False, 120, True), (True, 500, False)])
def test_the_legacy_request_budget_stops_at_120_and_not_for_instructions(instruction, attempts, refused):
    wire = Wire(instruction=instruction)
    wire.gateway.attempts = attempts
    if refused:
        with pytest.raises(RuntimeError, match="^Model request budget exceeded$"):
            wire.post(None, content=b"not json")
        assert wire.failures() == ["request_budget"] and wire.sent == []
    else:
        wire.post(body())
        assert wire.failures() == [] and wire.gateway.attempts == attempts + 1


def test_an_unapproved_compaction_wins_over_the_legacy_budget():
    wire = Wire(session_files=SimpleNamespace(checkpoint_error="SDK_STATE_X"))
    wire.gateway.attempts = 120
    with pytest.raises(RuntimeError, match="unapproved compaction"):
        wire.post(None, content=b"not json")
    assert wire.failures() == ["authority"]


def test_the_legacy_budget_wins_over_the_call_limit():
    wire = Wire(model_call_limit=5)
    wire.gateway.attempts = 120
    with pytest.raises(RuntimeError, match="budget exceeded") as refused:
        wire.post(None, content=b"not json")
    assert not isinstance(refused.value, AssistantModelFailure)


@pytest.mark.parametrize("attempts, refused", [(1, False), (2, True)])
def test_the_call_limit_raises_its_own_failure_with_its_diagnostic(attempts, refused):
    boundary = boundary_double([], delivered=httpx.Response(200))
    wire = Wire(instruction=True, model_call_limit=2, boundary=boundary, private_token=_token("t"))
    wire.gateway.attempts = attempts
    if not refused:
        wire.post(body())
        return
    with pytest.raises(AssistantModelFailure, match="Model call limit reached") as error:
        wire.post(None, content=b"not json")
    assert wire.failures() == ["request_budget"]
    assert boundary.record_failure.call_args.args[0] is error.value
    assert error.value.diagnostics["upstream_error_code"] == "MODEL_CALL_LIMIT_REACHED"
    assert error.value.diagnostics["upstream_error_phase"] == "request_budget"


def _token(value, log=None):
    async def token(scope):
        assert scope is None
        if log is not None:
            log.append("token")
        return value
    return token


def test_unparsable_json_escapes_unreported_and_uncounted():
    wire = Wire()
    with pytest.raises(json.JSONDecodeError):
        wire.post(None, content=b"not json")
    assert wire.failures() == [] and wire.gateway.attempts == 0


def test_another_model_is_refused():
    wire = Wire()
    with pytest.raises(RuntimeError, match="^Model is not allowed$"):
        wire.post(body(model="other"))
    assert wire.failures() == ["model"] and wire.sent == [] and wire.gateway.attempts == 0


# Body shaping.

def test_a_legacy_body_gets_the_fixed_output_limit_and_loses_copilot_only_fields():
    wire = Wire()
    wire.post(body(snippy=True, reasoning_effort="high", max_completion_tokens=9, max_tokens=1, reasoning={"x": 1},
                   include=["reasoning.encrypted_content"]))
    assert wire.body == body(max_tokens=MAX_OUTPUT_TOKENS, reasoning={"x": 1}, include=["reasoning.encrypted_content"])


@pytest.mark.parametrize("supports", [None, True])
def test_an_instruction_body_keeps_its_reasoning_unless_the_deployment_declares_none(supports):
    fields = dict(reasoning_effort="high", max_completion_tokens=9, reasoning={"effort": "low"},
                  include=["reasoning.encrypted_content"])
    wire = Wire(instruction=True, supports_reasoning_effort=supports)
    wire.post(body(snippy=True, **fields))
    assert wire.body == body(**fields)


@pytest.mark.parametrize("supports, limit", [(True, "max_completion_tokens"), (None, "max_tokens")])
def test_a_reasoning_model_instruction_gets_its_output_limit_as_max_completion_tokens(supports, limit):
    # Runtime 1.0.103 sends the declared max output as max_tokens for every model on this wire API.
    wire = Wire(instruction=True, supports_reasoning_effort=supports)
    wire.post(body(max_tokens=128000, reasoning_effort="medium", temperature=1))
    assert wire.body == body(**{limit: 128000}, reasoning_effort="medium", temperature=1)


@pytest.mark.parametrize("include, kept", [
    (["reasoning.encrypted_content"], None),
    (["reasoning.encrypted_content", "other"], ["other"]),
    (["other"], ["other"]),
    ([], None),
    ("reasoning.encrypted_content", "reasoning.encrypted_content"),
])
def test_a_deployment_without_reasoning_loses_the_runtime_defaults(include, kept):
    wire = Wire(instruction=True, supports_reasoning_effort=False)
    wire.post(body(snippy=True, max_completion_tokens=9, reasoning_effort="high", reasoning={"effort": "low"},
                   include=include))
    expected = body(max_completion_tokens=9)
    if kept is not None:
        expected["include"] = kept
    assert wire.body == expected


def tools(wrapped, name="task"):
    return [{"type": "function", "function": {"name": name, "parameters": {"original": True}}} if wrapped
            else {"type": "function", "name": name, "parameters": {"original": True}}]


def delegation(max_children=1, read_user_files=False, reference=None, log=None):
    async def model_ref(ctx):
        assert ctx is CTX
        if log is not None:
            log.append("model_ref")
        return reference
    return SimpleNamespace(max_children=max_children, read_user_files=read_user_files, model_ref=model_ref)


@pytest.mark.parametrize("max_children, read_files, first, second", [
    (1, False, "Run one subtask in the server-configured synchronous zero-tool child. ",
     "Only one task call can run in this turn. "),
    (1, True, "Run one subtask in the server-configured synchronous child with only read_user_file. ",
     "Only one task call can run in this turn. "),
    (3, False, "Run one subtask in the server-configured synchronous zero-tool child. ",
     "Separate subtasks may be separate task calls, up to 3 in this turn. "),
])
def test_the_task_tool_is_narrowed_on_both_wire_shapes(max_children, read_files, first, second):
    description = (first + second + "Use agent_type=fa-delegate-v1 and mode=sync; the model is inherited. "
                   "Do not supply model, reasoning_effort, context_tier or other overrides.")
    runtime = delegation(max_children, read_files)
    wire = Wire(instruction=True, delegation_runtime=runtime)
    wire.post(body(tools=tools(True) + tools(True, "other")))
    assert wire.body["tools"] == [
        {"type": "function", "function": {
            "name": "task", "parameters": DelegationTask.model_json_schema(), "description": description}},
        *tools(True, "other")]
    wire = Wire(RESPONSES, instruction=True, delegation_runtime=runtime)
    wire.post(body(tools=tools(False) + tools(False, "other")))
    assert wire.body["tools"] == [
        {"type": "function", "name": "task", "parameters": DelegationTask.model_json_schema(),
         "description": description}, *tools(False, "other")]


def test_without_delegation_the_task_tool_is_left_alone():
    wire = Wire(instruction=True)
    wire.post(body(tools=tools(True)))
    assert wire.body["tools"] == tools(True)


# Images.

def chat_images(*urls):
    return [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}} for url in urls]}]


def test_each_instruction_request_reports_both_kinds_of_omitted_image():
    wire = Wire(instruction=True, image_limit=2)
    wire.post(body(messages=chat_images(PNG + "1", JPEG, PNG + "2", PNG + "3")))
    event = next(item for item in wire.events if item["type"] == "m2.context_images")
    assert event["data"] == {"request_id": "sdk-request", "omitted_inline_pngs": 2,
                             "unsendable_inline_pngs": 1, "older_inline_pngs": 1}
    assert wire.log == ["m2.context_images", "m2.provider_request", "http"]
    assert [part["type"] for part in wire.body["messages"][0]["content"]] == ["text", "text", "image_url", "image_url"]


def test_a_legacy_request_keeps_an_unsendable_image_and_counts_only_older_ones():
    wire = Wire()
    wire.post(body(messages=chat_images(PNG + "1", JPEG, PNG + "2")))
    event = next(item for item in wire.events if item["type"] == "m2.context_images")
    assert event["data"] == {"request_id": "sdk-request", "omitted_inline_pngs": 1,
                             "unsendable_inline_pngs": 0, "older_inline_pngs": 1}
    assert wire.body["messages"][0]["content"][1]["image_url"]["url"] == JPEG


def test_a_request_with_nothing_omitted_reports_no_image_event():
    wire = Wire(instruction=True)
    wire.post(body(messages=chat_images(PNG)))
    assert wire.log == ["m2.provider_request", "http"]


OMITTED = ("[Screenshot omitted from model context: not a PNG within the model image bound. "
           "If deliver_image is available, the latest PNG screenshot can still be delivered to the user.]")


def test_unsendable_images_are_replaced_on_the_chat_shape_and_everything_else_stays():
    largest = "data:image/png;base64,".ljust(MODEL_IMAGE_URL_CHARS, "A")
    text = {"type": "text", "text": "keep"}
    wrong_type = {"type": "input_image", "image_url": JPEG}
    request = {"messages": [
        {"role": "system", "content": "plain"},
        {"role": "tool", "content": None, "output": [{"type": "image_url", "image_url": JPEG}]},
        {"role": "user", "output": [{"type": "image_url", "image_url": largest + "A"}]},
        {"role": "user", "content": ["string", text, wrong_type, {"type": "image_url", "image_url": {"url": largest}},
                                     {"type": "image_url", "image_url": {"url": 7}}, {"type": "image_url"}]},
    ]}
    assert _omit_unsendable_images(request) == 3
    replaced = {"type": "text", "text": OMITTED}
    assert request["messages"][1]["output"] == [{"type": "image_url", "image_url": JPEG}]
    assert request["messages"][2]["output"] == [replaced]
    assert request["messages"][3]["content"] == [
        "string", text, wrong_type, {"type": "image_url", "image_url": {"url": largest}}, replaced, replaced]


def test_unsendable_images_are_replaced_on_the_responses_shape():
    request = {"input": [{"output": [{"type": "input_image", "image_url": JPEG}]},
                         {"content": [{"type": "input_image", "image_url": PNG},
                                      {"type": "image_url", "image_url": JPEG}]}],
               "messages": [{"content": [{"type": "image_url", "image_url": JPEG}]}]}
    assert _omit_unsendable_images(request) == 1
    assert request["input"][0]["output"] == [{"type": "input_text", "text": OMITTED}]
    assert request["input"][1]["content"][1]["image_url"] == JPEG
    assert request["messages"][0]["content"][0]["image_url"] == JPEG


def test_nothing_to_scan_is_nothing_omitted():
    assert _omit_unsendable_images({}) == 0 and _retain_latest_inline_png({}) == 0


ONE = ("[Earlier inline PNG omitted from model context; only the latest "
       "desktop screenshot is included. Original image remains in session history.]")
THREE = ("[Earlier inline PNG omitted from model context; only the latest 3 "
         "screenshots are included. Original image remains in session history.]")


def urls(content):
    return [(part.get("image_url") or {"url": part["text"]})["url"] for part in content]


def test_the_latest_png_is_the_last_part_of_the_last_message():
    request = {"messages": [*chat_images(PNG + "1", PNG + "2"), {"content": "plain"}, *chat_images(PNG + "3", PNG + "4")]}
    assert _retain_latest_inline_png(request) == 3
    assert urls(request["messages"][0]["content"]) == [ONE, ONE]
    assert urls(request["messages"][2]["content"]) == [ONE, PNG + "4"]


def test_keep_spans_messages_and_other_images_are_not_counted():
    request = {"messages": [*chat_images(PNG + "1", PNG + "2"), *chat_images(JPEG, PNG + "3", PNG + "4")]}
    assert _retain_latest_inline_png(request, 3) == 1
    assert urls(request["messages"][0]["content"]) == [THREE, PNG + "2"]
    assert urls(request["messages"][1]["content"]) == [JPEG, PNG + "3", PNG + "4"]


def test_retention_reads_the_responses_shape_and_its_string_urls():
    request = {"input": [{"output": [{"type": "input_image", "image_url": PNG}, "x", {"type": "input_image"},
                                     {"type": "image_url", "image_url": PNG}]},
                         {"content": [{"type": "input_image", "image_url": {"url": PNG}}]}]}
    assert _retain_latest_inline_png(request) == 1
    assert request["input"][0]["output"][0] == {"type": "input_text", "text": ONE}
    assert request["input"][0]["output"][3] == {"type": "image_url", "image_url": PNG}


# The outbound request.

def test_the_outbound_request_keeps_headers_but_recomputes_the_length():
    wire = Wire(instruction=True)
    original = wire.request(body(messages=chat_images(PNG, JPEG)), headers={"x-keep": "yes"})
    asyncio.run(wire.gateway.send_request(original, CTX))
    sent, stream = wire.sent[0]
    assert sent.url == COMPLETIONS and sent.method == "POST" and stream is True
    assert sent.headers["x-keep"] == "yes"
    assert sent.headers["content-length"] == str(len(sent.content)) != original.headers["content-length"]


@pytest.mark.parametrize("image_limit, mib", [(1, 4), (3, CAPABILITY_IMAGE_BODY_MIB)])
def test_the_body_budget_is_inclusive_and_follows_the_image_limit(image_limit, mib):
    limit = mib * 1024 * 1024
    empty = len(httpx.Request("POST", COMPLETIONS, json=body(pad="")).content)
    wire = Wire(instruction=True, image_limit=image_limit)
    wire.post(body(pad="x" * (limit - empty)))
    assert wire.gateway.attempts == 1 and len(wire.sent[0][0].content) == limit
    wire = Wire(instruction=True, image_limit=image_limit)
    with pytest.raises(RuntimeError, match="^Model request body budget exceeded$"):
        wire.post(body(pad="x" * (limit - empty + 1)))
    assert wire.failures() == ["body_budget"] and wire.sent == [] and wire.gateway.attempts == 0
    assert wire.gateway.request_fingerprints == set()


def test_a_replay_is_the_same_body_in_any_key_order_and_is_not_counted():
    wire = Wire(instruction=True)
    wire.post({"model": MODEL, "messages": []})
    with pytest.raises(RuntimeError, match="^Model request replay refused$"):
        wire.post({"messages": [], "model": MODEL})
    assert wire.failures() == ["replayed_request"] and len(wire.sent) == 1 and wire.gateway.attempts == 1


def test_each_new_body_is_a_numbered_attempt():
    wire = Wire(instruction=True)
    wire.post(body())
    wire.post(body(extra=True))
    assert wire.gateway.attempts == 2 and len(wire.gateway.request_fingerprints) == 2
    assert [event["data"]["attempt"] for event in wire.events] == [1, 2]
    assert wire.events[0] == {"type": "m2.provider_request", "data": {
        "request_id": "sdk-request", "session_id": "sdk-session", "attempt": 1}}


class Inference:
    def __init__(self):
        self.prepared, self.purposes = [], []

    def prepare(self, *args, **options):
        self.prepared.append((args, options))
        return "prepared-token"

    def purpose(self, token, value):
        self.purposes.append((token, value))


@pytest.mark.parametrize("endpoint, wire_api", [(COMPLETIONS, "completions"), (RESPONSES, "responses")])
@pytest.mark.parametrize("compacting, purpose", [(False, "unknown"), (True, "compaction")])
def test_inference_is_prepared_with_the_outbound_bytes(endpoint, wire_api, compacting, purpose):
    inference = Inference()
    files = SimpleNamespace(checkpoint_error=None, compacting=compacting)
    wire = Wire(endpoint, instruction=True, inference=inference, session_files=files)
    wire.post(body(), ctx=SimpleNamespace(request_id="sdk-request", session_id="sdk-session", interaction_type="chat"))
    sent = wire.sent[0][0]
    assert sent.extensions["fa_inference"] == "prepared-token"
    assert inference.prepared == [((sent.content, MODEL, wire_api), {
        "source": "gateway", "sdk_request_id": "sdk-request", "sdk_session_id": "sdk-session",
        "fingerprint": digest(body()), "interaction_type": "chat", "purpose": purpose})]
    assert inference.purposes == []


def test_the_fingerprint_hashes_text_as_it_is():
    inference = Inference()
    original = body(messages=[{"role": "user", "content": "你好"}])
    Wire(instruction=True, inference=inference).post(original)
    assert inference.prepared[0][1]["fingerprint"] == digest(original)


def test_inference_without_files_or_an_interaction_type():
    inference = Inference()
    wire = Wire(instruction=True, inference=inference)
    wire.post(body())
    assert inference.prepared[0][1]["purpose"] == "unknown"
    assert inference.prepared[0][1]["interaction_type"] is None


# Reservation, transport and delivery.

def test_a_boundary_request_reserves_in_order_before_the_provider_event():
    log = []
    files = SimpleNamespace(checkpoint_error=None, compacting=False, observe_model_request=lambda value: log.append(
        ("observed", value)))
    inference = Inference()

    async def sender(request):
        log.append(("sender", request.headers.get("authorization"), request.extensions["fa_delegation"]))
        return httpx.Response(200, json={})

    boundary = boundary_double(log, sender=sender, delivered=httpx.Response(200, json={}))
    wire = Wire(instruction=True, boundary=boundary, session_files=files, inference=inference,
                private_token=_token("t", log), delegation_runtime=delegation(reference="child", log=log))
    wire.gateway.emit = lambda event: log.append(event["type"])
    wire.post(body())
    assert log == ["model_ref", ("observed", digest(body())), ("before_request", digest(body())),
                   "m2.provider_request", ("sender", None, "child")]
    assert inference.purposes == [("prepared-token", "delegation")]
    assert wire.sent == []


def test_a_delegated_request_without_inference_still_carries_its_reference():
    seen = []

    async def sender(request):
        seen.append(request)
        return httpx.Response(200, json={})

    wire = Wire(instruction=True, boundary=boundary_double([], sender=sender, delivered=httpx.Response(200)),
                delegation_runtime=delegation(reference="child"))
    wire.post(body())
    assert seen[0].extensions["fa_delegation"] == "child" and "fa_inference" not in seen[0].extensions


def test_no_delegated_child_means_no_reference_and_no_purpose_change():
    log = []
    inference = Inference()
    seen = []

    async def sender(request):
        seen.append(request)
        return httpx.Response(200, json={})

    wire = Wire(instruction=True, boundary=boundary_double(log, sender=sender, delivered=httpx.Response(200)),
                inference=inference, delegation_runtime=delegation())
    wire.post(body())
    assert "fa_delegation" not in seen[0].extensions and inference.purposes == []


def test_without_a_sender_the_boundary_authorizes_the_gateway_transport():
    log = []
    wire = Wire(instruction=True, boundary=boundary_double(log, delivered=httpx.Response(200)),
                private_token=_token("private", log))
    wire.post(body())
    assert log == [("before_request", digest(body())), "token"]
    assert wire.sent[0][0].headers["authorization"] == "Bearer private"


def test_no_boundary_sends_with_the_authorization_the_sdk_gave():
    wire = Wire(instruction=True)
    asyncio.run(wire.gateway.send_request(
        wire.request(body(), headers={"authorization": "Bearer sdk"}), CTX))
    assert wire.sent[0][0].headers["authorization"] == "Bearer sdk"


@pytest.mark.parametrize("where", ["model_ref", "observe", "before_request", "token"])
def test_any_reservation_error_is_an_authority_failure_without_its_text(where):
    log = []
    secret = RuntimeError("SECRET")

    def explode(*_):
        raise secret

    async def explode_async(*_):
        raise secret

    files = SimpleNamespace(checkpoint_error=None, compacting=False,
                            observe_model_request=explode if where == "observe" else lambda _: None)
    boundary = boundary_double(log, error=secret if where == "before_request" else None)
    runtime = delegation()
    if where == "model_ref":
        runtime.model_ref = explode_async
    wire = Wire(instruction=True, boundary=boundary, session_files=files, delegation_runtime=runtime,
                private_token=explode_async if where == "token" else _token("t"))
    with pytest.raises(RuntimeError, match="^Assistant request reservation denied$") as refused:
        wire.post(body())
    assert refused.value.__cause__ is None and refused.value.__suppress_context__
    assert wire.failures() == ["authority"] and wire.sent == []
    assert boundary.record_failure.call_args.args[0] is secret
    # The attempt was reserved locally before the first await and stays spent.
    assert wire.gateway.attempts == 1
    assert "m2.provider_request" not in wire.log


def test_a_transport_error_hides_its_text():
    wire = Wire(instruction=True, error=RuntimeError("SECRET header"))
    with pytest.raises(RuntimeError, match="^Context API model transport failed$") as refused:
        wire.post(body())
    assert refused.value.__cause__ is None and refused.value.__suppress_context__
    assert wire.failures() == ["transport"]


def test_a_boundary_sender_error_is_a_transport_failure_too():
    secret = RuntimeError("SECRET")

    async def sender(_):
        raise secret

    boundary = boundary_double([], sender=sender)
    wire = Wire(instruction=True, boundary=boundary)
    with pytest.raises(RuntimeError, match="transport failed"):
        wire.post(body())
    assert boundary.record_failure.call_args.args[0] is secret and wire.sent == []


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks, error=None):
        self.chunks, self.error, self.closed = chunks, error, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closed = True


def watched(monkeypatch):
    calls = []

    def watch(response, wire_api):
        calls.append((response, wire_api))
        return "watched"

    monkeypatch.setattr(sdk_telemetry, "watch_model_response", watch)
    return calls


@pytest.mark.parametrize("endpoint, wire_api", [(COMPLETIONS, "completions"), (RESPONSES, "responses")])
def test_a_consumed_response_is_watched_as_it_is(monkeypatch, endpoint, wire_api):
    calls = watched(monkeypatch)
    reply = httpx.Response(200, json={})
    assert reply.is_stream_consumed
    wire = Wire(endpoint, reply=reply, instruction=True)
    assert wire.post(body()) == "watched"
    assert calls == [(reply, wire_api)]


@pytest.mark.parametrize("endpoint, wire_api", [(COMPLETIONS, "completions"), (RESPONSES, "responses")])
def test_a_boundary_response_is_watched_after_the_boundary_took_it(monkeypatch, endpoint, wire_api):
    calls = watched(monkeypatch)
    reply, delivered = httpx.Response(200, json={}), httpx.Response(200, json={"delivered": True})
    boundary = boundary_double([], delivered=delivered)
    wire = Wire(endpoint, reply=reply, instruction=True, boundary=boundary, private_token=_token("t"))
    assert wire.post(body()) == "watched"
    boundary.response.assert_awaited_once_with(reply)
    assert calls == [(delivered, wire_api)]


@pytest.mark.parametrize("endpoint, wire_api", [(COMPLETIONS, "completions"), (RESPONSES, "responses")])
def test_a_live_stream_is_rewrapped_and_keeps_status_headers_and_request(monkeypatch, endpoint, wire_api):
    calls = watched(monkeypatch)
    stream = Chunks(b"one", b"two")
    wire = Wire(endpoint, reply=httpx.Response(207, headers={"x-reply": "kept"}, stream=stream), instruction=True)
    assert wire.post(body()) == "watched"
    response, seen = calls[0]
    assert seen == wire_api and response.status_code == 207 and response.headers["x-reply"] == "kept"
    assert response.request is wire.sent[0][0]

    async def read():
        return b"".join([chunk async for chunk in response.aiter_raw()])

    assert asyncio.run(read()) == b"onetwo" and wire.failures() == []
    assert stream.closed


def test_a_broken_stream_latches_a_stream_failure_without_its_text():
    stream = Chunks(b"one", error=RuntimeError("SECRET"))
    wire = Wire(reply=httpx.Response(200, stream=stream), instruction=True)
    response = wire.post(body())

    async def read():
        async for _ in response.aiter_raw():
            pass

    with pytest.raises(RuntimeError, match="^Context API model stream failed$"):
        asyncio.run(read())
    assert wire.failures() == ["stream"]


def test_a_rejected_status_is_latched_but_still_handed_back():
    wire = Wire(reply=httpx.Response(503, json={"error": "x"}), instruction=True)
    response = wire.post(body())
    assert response.status_code == 503
    assert wire.events[-1] == {"type": "m2.provider_failed", "data": {
        "request_id": "sdk-request", "phase": "http_status", "status_code": 503}}
    with pytest.raises(RuntimeError, match="retry refused"):
        wire.post(body(extra=True))


def test_a_boundary_refusal_is_an_output_failure_and_a_bad_status_is_reported_first():
    secret = RuntimeError("SECRET")
    boundary = boundary_double([])
    boundary.response.side_effect = secret
    wire = Wire(reply=httpx.Response(200, json={}), instruction=True, boundary=boundary, private_token=_token("t"))
    with pytest.raises(RuntimeError, match="^Assistant provider output refused$") as refused:
        wire.post(body())
    assert refused.value.__cause__ is None and refused.value.__suppress_context__
    assert wire.failures() == ["output_boundary"]
    assert boundary.record_failure.call_args.args[0] is secret

    boundary = boundary_double([])
    boundary.response.side_effect = secret
    wire = Wire(reply=httpx.Response(500, json={}), instruction=True, boundary=boundary, private_token=_token("t"))
    with pytest.raises(RuntimeError, match="output refused"):
        wire.post(body())
    assert wire.failures() == ["http_status"]


# Anything else the runtime tries.

def metadata_request(method="GET", url="https://api.githubcopilot.com/models"):
    return httpx.Request(method, url)


def test_the_local_model_list_never_reaches_a_network():
    wire = Wire()
    response = asyncio.run(wire.gateway.send_request(metadata_request(), CTX))
    assert response.status_code == 200 and wire.sent == [] and wire.gateway.attempts == 0
    assert wire.events == [{"type": "m2.local_metadata", "data": {"path": "/models"}}]
    assert response.json() == {"data": [{
        "id": MODEL, "name": MODEL, "object": "model", "vendor": "OpenAI",
        "version": "1", "preview": False, "model_picker_enabled": True,
        "supported_endpoints": ["/chat/completions"],
        "capabilities": {
            "type": "chat", "family": MODEL, "tokenizer": "o200k_base",
            "limits": {"max_context_window_tokens": MAX_PROMPT_TOKENS + 1024, "max_output_tokens": MAX_OUTPUT_TOKENS},
            "supports": {"streaming": True, "tool_calls": True, "parallel_tool_calls": False, "vision": True},
        },
    }]}


@pytest.mark.parametrize("request_, options", [
    (metadata_request(), {"instruction": True}),
    (metadata_request("POST"), {}),
    (metadata_request(url="http://api.githubcopilot.com/models"), {}),
    (metadata_request(url="https://api.githubcopilot.com/other"), {}),
    (metadata_request(url="https://other.invalid/models"), {}),
    (httpx.Request("GET", COMPLETIONS), {}),
])
def test_every_other_destination_is_denied_and_latched(request_, options):
    wire = Wire(**options)
    with pytest.raises(RuntimeError, match="^Model-layer destination is not allowed$"):
        asyncio.run(wire.gateway.send_request(request_, CTX))
    assert wire.events[0] == {"type": "m2.network_denied", "data": {
        "method": request_.method, "host": request_.url.host, "path": request_.url.path}}
    assert wire.failures() == ["destination"]


# What ends a turn.

class Session:
    session_id = "parent"

    def __init__(self, *script):
        self.script, self.handler, self.sent, self.log = script, None, [], []

    def on(self, handler):
        self.handler = handler
        return lambda: self.log.append("unsubscribed")

    async def send(self, prompt, **options):
        self.sent.append((prompt, options))
        for value in self.script:
            self.handler(value)


class Steering:
    def __init__(self, log, idle=True):
        self.log, self.idle = log, idle
        self.seen = []

    def observe(self, value):
        self.seen.append(value)

    def open(self, session):
        self.log.append("open")

    def close(self):
        self.log.append("close")

    async def wait_idle(self):
        self.log.append("wait_idle")
        if not self.idle:
            await asyncio.Event().wait()


LIVE = SimpleNamespace(poll=lambda: None)


def turn(*script, process=LIVE, **options):
    session = Session(*script)

    async def run():
        return await asyncio.wait_for(_send_until_idle(session, "prompt", process, **options), 2)

    return session, run


def finish(*script, **options):
    return asyncio.run(turn(*script, **options)[1]())


def event(data, **fields):
    return SimpleNamespace(data=data, **fields)


def said(text, *tool_ids, **fields):
    return event(AssistantMessageData(content=text, message_id=text, tool_requests=[
        AssistantMessageToolRequest(name="tool", tool_call_id=item) for item in tool_ids]), **fields)


def asked(**fields):
    return event(UserMessageData(content="next"), **fields)


def started(call="c", name="done", **fields):
    return event(ToolExecutionStartData(tool_call_id=call, tool_name=name), **fields)


def completed(call="c", success=True, **fields):
    return event(ToolExecutionCompleteData(success=success, tool_call_id=call), **fields)


IDLE = event(SessionIdleData())
AUTOPILOT = event(SessionIdleData(mode=SessionMode.AUTOPILOT))
ABORTED = event(SessionIdleData(aborted=True))
ERROR = event(SessionErrorData(error_type="x", message="SECRET"))
SHUTDOWN = event(SessionShutdownData(code_changes=None, model_metrics={}, session_start_time=0, shutdown_type="routine",
                                     total_api_duration=0))
TERMINAL = frozenset({"done"})


def test_the_final_message_is_the_one_without_tool_requests():
    first, second = said("first", "t1"), said("second")
    assert finish(first, second, said("later", "t2"), IDLE) == [second]


def test_with_only_tool_requests_the_last_message_is_the_answer():
    assert finish(said("first", "t1"), said("second", "t2"), IDLE)[0].data.content == "second"


def test_a_turn_with_no_messages_has_no_rounds():
    assert finish(IDLE) == []


def test_an_appended_user_message_starts_a_new_round_and_drops_a_tool_only_round():
    one, two = said("one"), said("two")
    assert finish(one, asked(), said("tools", "t1"), asked(), two, IDLE) == [one, two]


def test_a_terminal_tool_marks_its_round_and_keeps_the_models_words():
    intro, after = said("intro", "c"), said("after")
    assert finish(intro, started(), completed(), after, IDLE, terminal_tools=TERMINAL) == [None, intro, after]


def test_a_terminal_tool_keeps_final_and_last_in_order():
    answer, tools = said("answer"), said("tools", "c")
    assert finish(answer, tools, started(), completed(), IDLE, terminal_tools=TERMINAL) == [None, answer, tools]


def test_a_terminal_tool_without_words_is_only_its_marker_and_clears_the_round():
    final = said("final")
    assert finish(final, started(), completed(), IDLE, terminal_tools=TERMINAL) == [None, final]
    assert finish(started(), completed(), IDLE, terminal_tools=TERMINAL) == [None]


@pytest.mark.parametrize("script", [
    (started(name="other"), completed()),
    (started(), completed(success=False)),
    (started(call="a"), completed(call="b")),
    (started(agent_id="child"), completed()),
    (started(), completed(agent_id="child")),
    (completed(),),
])
def test_only_a_parent_terminal_tool_that_succeeded_ends_a_round(script):
    message = said("words")
    assert finish(message, *script, IDLE, terminal_tools=TERMINAL) == [message]


def test_a_failed_terminal_tool_stays_open_for_its_success():
    message = said("words")
    script = (message, started(), completed(success=False), completed(), IDLE)
    assert finish(*script, terminal_tools=TERMINAL) == [None, message]


def test_each_terminal_tool_call_counts_once():
    message, after = said("words"), said("after")
    script = (message, started(), completed(), after, completed(), IDLE)
    assert finish(*script, terminal_tools=TERMINAL) == [None, message, after]


@pytest.mark.parametrize("parent", [{"agent_id": None}, {"agent_id": "parent"}, {}])
def test_a_parent_message_may_name_the_session_or_nobody(parent):
    message = said("words", **parent)
    assert finish(message, IDLE) == [message]


def test_a_child_agent_message_and_user_message_change_no_round():
    message = said("parent")
    assert finish(message, said("child", agent_id="child"), asked(agent_id="child"), IDLE) == [message]


def test_without_steering_a_childs_idle_or_error_still_ends_the_turn():
    message = said("parent")
    assert finish(message, event(SessionIdleData(), agent_id="child"), said("late"), IDLE) == [message]
    with pytest.raises(RuntimeError, match="^SDK session failed or disconnected before its turn completed$"):
        finish(event(ERROR.data, agent_id="child"), IDLE)


def test_autopilot_idle_is_not_the_end():
    message = said("after")
    assert finish(AUTOPILOT, message, IDLE) == [message]


def test_an_aborted_autopilot_idle_is_ignored_too():
    message = said("after")
    assert finish(event(SessionIdleData(mode=SessionMode.AUTOPILOT, aborted=True)), message, IDLE) == [message]


def test_an_aborted_idle_is_the_users_cancel():
    with pytest.raises(SdkUserCancelled, match="^Original SDK turn was cancelled$"):
        finish(said("words"), ABORTED)


@pytest.mark.parametrize("failure", [ERROR, SHUTDOWN], ids=["error", "shutdown"])
def test_a_session_failure_ends_the_turn_and_hides_its_text(failure):
    with pytest.raises(RuntimeError, match="^SDK session failed or disconnected before its turn completed$"):
        finish(said("words"), failure)


def test_the_first_ending_wins_over_later_events():
    message = said("words")
    assert finish(message, IDLE, ERROR, ABORTED, said("late")) == [message]
    with pytest.raises(SdkUserCancelled):
        finish(ABORTED, IDLE, ERROR)


def test_the_prompt_goes_out_with_exactly_the_send_options():
    session, run = turn(IDLE)
    asyncio.run(run())
    assert session.sent == [("prompt", {})] and session.log == ["unsubscribed"]
    session, run = turn(IDLE, attachments=[{"type": "blob"}])
    asyncio.run(run())
    assert session.sent == [("prompt", {"attachments": [{"type": "blob"}]})]


def test_a_dead_process_is_an_error_and_not_an_idle():
    session, run = turn(said("words"), process=SimpleNamespace(poll=lambda: 1))
    with pytest.raises(RuntimeError, match="^Owned SDK process exited before session idle$"):
        asyncio.run(run())
    assert session.log == ["unsubscribed"]


def test_a_finished_turn_stops_watching_the_process():
    polls = []

    def poll():
        polls.append(1)

    async def run():
        await _send_until_idle(Session(IDLE), "prompt", SimpleNamespace(poll=poll))
        seen = len(polls)
        await asyncio.sleep(0.25)
        return seen

    assert asyncio.run(run()) == len(polls)


def test_a_send_failure_escapes_and_still_unsubscribes():
    class Failing(Session):
        async def send(self, prompt, **options):
            raise ValueError("send")

    session = Failing()
    with pytest.raises(ValueError, match="^send$"):
        asyncio.run(_send_until_idle(session, "prompt", LIVE))
    assert session.log == ["unsubscribed"]


def test_steering_ends_the_turn_at_its_own_idle_not_at_the_sessions():
    log = []
    steering = Steering(log)
    message = said("words")
    session, run = turn(message, IDLE, steering=steering)
    assert asyncio.run(run()) == [message]
    assert log == ["open", "wait_idle", "close"] and session.log == ["unsubscribed"]
    assert steering.seen == [message, IDLE]


def test_a_steered_turn_outlives_a_plain_idle_until_the_steering_goes_idle():
    first, second = said("first"), said("second")
    session = Session(first, IDLE)

    class Appending(Steering):
        async def wait_idle(self):
            await asyncio.sleep(0)
            session.handler(asked())
            session.handler(second)

    async def run():
        return await _send_until_idle(session, "prompt", LIVE, steering=Appending([]))

    assert asyncio.run(run()) == [first, second]


def test_steering_never_observes_a_child_and_a_childs_ending_is_ignored():
    log = []
    steering = Steering(log)
    message = said("words")
    child = [event(ERROR.data, agent_id="child"), event(SessionIdleData(aborted=True), agent_id="child")]
    assert finish(message, *child, steering=steering) == [message]
    assert steering.seen == [message]


def test_steering_observes_before_the_turn_settles_and_not_after():
    log = []
    steering = Steering(log, idle=False)
    with pytest.raises(SdkUserCancelled):
        finish(ABORTED, said("late"), steering=steering)
    assert len(steering.seen) == 1 and steering.seen[0] is ABORTED
    assert log == ["open", "wait_idle", "close"]


def test_a_steered_turn_fails_on_a_session_error_and_cancels_its_wait():
    log = []
    with pytest.raises(RuntimeError, match="failed or disconnected"):
        finish(ERROR, steering=Steering(log, idle=False))
    assert log == ["open", "wait_idle", "close"]


def test_a_steering_wait_that_ends_after_the_turn_settled_changes_nothing():
    log = []
    steering = Steering(log)
    with pytest.raises(SdkUserCancelled):
        finish(ABORTED, steering=steering)
    assert log == ["open", "wait_idle", "close"]


# The desktop display.

@pytest.mark.parametrize("display", [
    {"DISPLAY": 5}, {"DISPLAY": None}, {"DISPLAY": ":123456"}, {"DISPLAY": ":"}, {"CDP_ENDPOINT": "http://127.0.0.1:9222"},
])
def test_only_one_local_display_number_is_a_desktop(display):
    with pytest.raises(ValueError, match="^a desktop display is one local X display number$"):
        show_on_desktop({}, display)


@pytest.mark.parametrize("endpoint", [9222, "http://127.0.0.1:0", "http://127.0.0.1:123456", "https://127.0.0.1:9222"])
def test_only_one_local_cdp_endpoint_is_a_desktop_browser(endpoint):
    servers = {"playwright": {"args": ["--headless"], "env": {}}}
    with pytest.raises(ValueError, match="^a desktop browser is one local CDP endpoint$"):
        show_on_desktop(servers, {"DISPLAY": ":1", "CDP_ENDPOINT": endpoint})
    assert servers == {"playwright": {"args": ["--headless"], "env": {}}}


def test_no_endpoint_keeps_isolated_and_launches_headed():
    servers = {"playwright": {"args": ["--headless", "--isolated", "--cdp-endpoint"], "env": {}}}
    show_on_desktop(servers, {"DISPLAY": ":1", "CDP_ENDPOINT": None})
    assert servers["playwright"]["args"] == ["--isolated", "--cdp-endpoint"]


def test_a_server_entry_without_args_or_env_gets_both():
    servers = {"playwright": {}}
    show_on_desktop(servers, {"DISPLAY": ":1"})
    assert servers == {"playwright": {"args": [], "env": {"DISPLAY": ":1"}}}
    servers = {"playwright": {}}
    show_on_desktop(servers, {"DISPLAY": ":1", "CDP_ENDPOINT": "http://127.0.0.1:9222"})
    assert servers["playwright"]["args"] == ["--cdp-endpoint", "http://127.0.0.1:9222"]


def test_no_playwright_server_changes_nothing():
    servers = {"other": {"args": ["--headless"], "env": {}}}
    show_on_desktop(servers, {"DISPLAY": ":1", "CDP_ENDPOINT": "http://127.0.0.1:9222"})
    assert servers == {"other": {"args": ["--headless"], "env": {}}}
