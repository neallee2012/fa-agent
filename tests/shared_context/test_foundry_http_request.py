"""FoundryHttpClient._request, offline: the order of its checks, how each reply is classified, what the log keeps."""

import json
from types import SimpleNamespace

import httpx
import pytest

from shared_context import behavior_foundry as foundry
from shared_context import behavior_wire as wire
from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.behavior_native_turn_wire import (
    NATIVE_EXPECTED_VERSION_HEADER, NATIVE_LOCATOR_BINDING_ROW, NATIVE_LOCATOR_HEADER, NATIVE_STATE_PROFILE,
    NATIVE_TURN_HEADER,
)
from shared_context.errors import Problem
from test_behavior_foundry import ACTUAL_INVOCATION, Credential, INVOCATION, PERMIT, PROBE, TARGET
from test_behavior_store import code

LAUNCH = "original-launch"
ECHO = {"x-agent-invocation-id": ACTUAL_INVOCATION, "x-agent-session-id": TARGET.session_id}
OBSERVED = {"x-agent-invocation-id": INVOCATION, "x-agent-session-id": TARGET.session_id}
JSON = {"content-type": "application/json"}
ROUTE = TARGET.project_endpoint + "/agents/checksum-agent/endpoint/protocols/invocations"
MOVED = TARGET.model_copy(update={"session_id": "moved-session", "agent_version": "99"})


class Token:
    def __init__(self, value=None, *, error=None, then=None):
        self.value, self.error, self.then, self.calls = value, error, then, 0

    def get_token(self, scope):
        self.calls += 1
        if self.then is not None:
            self.then()
        if self.error is not None:
            raise self.error
        return SimpleNamespace(token=self.value)


@pytest.fixture
def make():
    clients = []

    def build(handler, credential=None, **options):
        clients.append(FoundryHttpClient(TARGET, credential or Credential(),
                                         transport=httpx.MockTransport(handler), **options))
        return clients[-1]
    yield build
    for client in clients:
        client.close()


def unsent(request):
    pytest.fail("nothing may be sent")


def turn(client, operation="read", stage=None, **options):
    return client._request("POST", None, action="turn", body=b"{}", native_operation=operation, native_stage=stage,
                           **options)


def refused(make, status, operation="session_read", **error):
    with pytest.raises(Problem) as caught:
        turn(make(lambda _request: httpx.Response(status, json={"error": error})), operation)
    return caught.value


def events(caplog, name="NATIVE_INVOCATION_FAILURE"):
    return [json.loads(record.getMessage().removeprefix(name + " ")) for record in caplog.records
            if record.getMessage().startswith(name + " ")]


def turn_failure(make, caplog, handler, operation="read"):
    with pytest.raises(Problem) as caught:
        turn(make(handler), operation)
    event, = events(caplog)
    return caught.value, event


class Chunks(httpx.SyncByteStream):
    def __init__(self, *chunks, between=None, closing=None):
        self.chunks, self.between, self.closing = chunks, between, closing

    def __iter__(self):
        for chunk in self.chunks:
            yield chunk
            if self.between is not None:
                self.between()

    def close(self):
        if self.closing is not None:
            raise self.closing


# Local checks: their order, and that nothing leaves before they all pass.

@pytest.mark.parametrize("credential,error_code,status,message", [
    (Token(error=RuntimeError("private")), "FOUNDRY_CREDENTIAL_UNAVAILABLE", 503, "native credential acquisition failed"),
    (Token(123), "FOUNDRY_CREDENTIAL_UNAVAILABLE", 503, "native credential is invalid"),
    (Token(""), "FOUNDRY_CREDENTIAL_UNAVAILABLE", 503, "native credential is invalid"),
    (Token("a\rb"), "FOUNDRY_CREDENTIAL_UNAVAILABLE", 503, "native credential is invalid"),
    (Token("a\nb"), "FOUNDRY_CREDENTIAL_UNAVAILABLE", 503, "native credential is invalid"),
    (Token("good"), "FOUNDRY_PROBE_INVALID", 422, "a server-issued probe ID is required"),
])
def test_a_token_problem_is_reported_before_a_bad_probe(make, credential, error_code, status, message):
    with pytest.raises(Problem) as caught:
        make(unsent, credential).observe(INVOCATION, "not-a-server-probe")
    assert code(caught) == error_code and caught.value.status == status
    assert caught.value.body["error"]["message"] == message


def test_a_bad_invocation_is_refused_before_the_credential_is_looked_at():
    credential = Token("good")
    for configured in (credential, None):
        client = FoundryHttpClient(TARGET, configured, transport=httpx.MockTransport(unsent))
        with pytest.raises(Problem) as caught:
            client.observe("not/an/id", PROBE)
        client.close()
        assert code(caught) == "FOUNDRY_INVOCATION_INVALID" and caught.value.status == 422
    assert credential.calls == 0


def test_a_missing_credential_is_refused_before_a_bad_probe():
    client = FoundryHttpClient(TARGET, None, transport=httpx.MockTransport(unsent))
    with pytest.raises(Problem) as caught:
        client.observe(INVOCATION, "not-a-server-probe")
    client.close()
    assert code(caught) == "FOUNDRY_CREDENTIAL_DISABLED" and caught.value.status == 403


@pytest.mark.parametrize("launch", ["", "not/a/launch", "x" * 65])
def test_a_bad_keepalive_launch_stops_before_before_send(make, launch):
    gate = []
    with pytest.raises(Problem) as caught:
        make(unsent).keepalive(INVOCATION, launch, before_send=lambda: gate.append(1))
    assert code(caught) == "FOUNDRY_KEEPALIVE_INVALID" and caught.value.status == 422 and not gate


def test_before_send_runs_once_after_the_local_checks_and_can_stop_the_send(make, caplog):
    order = []
    client = make(lambda _request: order.append("sent") or httpx.Response(200, json={}, headers=ECHO))

    def blocked():
        order.append("gate")
        raise Problem(409, "ORIGINAL_GATE", "closed")

    with pytest.raises(Problem) as caught:
        turn(client, before_send=blocked)
    assert code(caught) == "ORIGINAL_GATE" and order == ["gate"]
    assert client.last_session_request() is None and not events(caplog)
    order.clear()
    turn(client, before_send=lambda: order.append("gate"))
    assert order == ["gate", "sent"]


# What goes out.

def names(request):
    return [name.decode().lower() for name, _ in request.headers.raw
            if name.decode().lower() not in {"host", "user-agent", "connection", "content-length"}]


def test_each_action_sends_its_exact_url_and_headers_in_order(make):
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(200 if request.method == "GET" else 202, content=b"{}", headers=OBSERVED)

    client = make(reply)
    client.observe(INVOCATION, PROBE)
    client.cancel(INVOCATION, PROBE)
    client._request("GET", INVOCATION, action="observe", keepalive_launch_id=LAUNCH)
    client._request("POST", INVOCATION, action="start", body=b"{}", launch_permit=PERMIT)
    base = names(calls[0])[:4]
    assert sorted(base) == ["accept", "accept-encoding", "authorization", "content-type"]
    observe, cancel, keepalive, start = calls
    assert str(observe.url) == f"{ROUTE}/{INVOCATION}?api-version=v1"
    assert str(cancel.url) == f"{ROUTE}/{INVOCATION}/cancel?api-version=v1"
    assert str(keepalive.url) == f"{ROUTE}/{INVOCATION}?api-version=v1"
    assert str(start.url) == f"{ROUTE}?api-version=v1&agent_session_id={TARGET.session_id}"
    assert names(observe) == [*base, "x-agent-invocation-id", wire.NATIVE_PROBE_HEADER.lower()]
    assert names(keepalive) == [*base, "x-agent-invocation-id", wire.NATIVE_KEEPALIVE_HEADER.lower()]
    assert names(start)[:len(base) + 2] == [*base, "x-agent-invocation-id", wire.NATIVE_LAUNCH_PERMIT_HEADER.lower()]
    assert keepalive.headers[wire.NATIVE_KEEPALIVE_HEADER] == LAUNCH
    assert observe.headers["authorization"] == "Bearer synthetic-test-token-never-persist"
    assert observe.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("located", [False, True])
def test_a_native_turn_names_its_profile_version_and_locator_and_no_invocation(make, located):
    calls = []
    client = make(lambda request: calls.append(request) or httpx.Response(200, json={}, headers=ECHO))
    if located:
        client._binding_row = object()
    turn(client)
    request, = calls
    assert str(request.url) == f"{ROUTE}?api-version=v1&agent_session_id={TARGET.session_id}"
    assert request.headers[NATIVE_TURN_HEADER] == NATIVE_STATE_PROFILE
    assert request.headers[NATIVE_EXPECTED_VERSION_HEADER] == TARGET.agent_version
    assert request.headers.get(NATIVE_LOCATOR_HEADER) == (NATIVE_LOCATOR_BINDING_ROW if located else None)
    assert "x-agent-invocation-id" not in request.headers
    expected = [NATIVE_TURN_HEADER, NATIVE_EXPECTED_VERSION_HEADER, *([NATIVE_LOCATOR_HEADER] if located else [])]
    assert names(request)[-len(expected):] == expected


@pytest.mark.parametrize("timeout,expected", [
    (None, {"connect": 7, "read": 7, "write": 7, "pool": 7}),
    (30, {"connect": 10, "read": 30, "write": 30, "pool": 30}),
    (4, {"connect": 4, "read": 4, "write": 4, "pool": 4}),
])
def test_a_call_timeout_replaces_the_clients_for_that_request(make, timeout, expected):
    calls = []
    turn(make(lambda request: calls.append(request) or httpx.Response(200, json={}, headers=ECHO), timeout_seconds=7),
         timeout=timeout)
    assert calls[0].extensions["timeout"] == expected


# Control replies: status, then encoding, then echo, then the bounded body.

@pytest.mark.parametrize("status", [200, 202])
def test_a_control_reply_returns_its_exact_ids_and_bytes(make, status):
    client = make(lambda _request: httpx.Response(status, content=b'{"a":1}', headers=OBSERVED))
    reply = client.observe(INVOCATION, PROBE)
    assert reply == foundry.NativeResponse(status, INVOCATION, TARGET.session_id, b'{"a":1}')
    assert type(reply.body) is bytes


@pytest.mark.parametrize("status", [201, 204, 301, 404, 500])
def test_a_control_status_is_checked_before_its_encoding_and_echo(make, status):
    with pytest.raises(Problem) as caught:
        make(lambda _request: httpx.Response(status, stream=Chunks(b"private"), headers={"content-encoding": "gzip"})
             ).observe(INVOCATION, PROBE)
    assert caught.value.status == 503 and caught.value.body["error"] == {
        "code": "FOUNDRY_INVOCATION_UNKNOWN", "upstream_status": status,
        "message": "native response is not an execution result; reconcile original identity"}


def test_a_compressed_control_reply_is_refused_before_its_echo(make):
    with pytest.raises(Problem) as caught:
        make(lambda _request: httpx.Response(200, stream=Chunks(b"x"), headers={"content-encoding": "gzip"})
             ).observe(INVOCATION, PROBE)
    assert code(caught) == "FOUNDRY_RESPONSE_INVALID" and caught.value.status == 502
    assert caught.value.body["error"]["message"] == "compressed native control responses are not accepted"


def test_identity_encoding_is_accepted_in_any_case(make):
    client = make(lambda _request: httpx.Response(200, content=b"{}", headers={**OBSERVED, "content-encoding": "IDENTITY"}))
    assert client.observe(INVOCATION, PROBE).status_code == 200


def test_a_control_body_is_bounded_only_after_its_echo_passes(make):
    limit = foundry.MAX_NATIVE_RESPONSE_BYTES
    assert len(make(lambda _request: httpx.Response(200, stream=Chunks(b"x" * limit), headers=OBSERVED))
               .observe(INVOCATION, PROBE).body) == limit
    oversized = make(lambda _request: httpx.Response(200, stream=Chunks(b"x" * limit, b"y"), headers=OBSERVED))
    with pytest.raises(Problem) as caught:
        oversized.observe(INVOCATION, PROBE)
    assert caught.value.status == 502 and code(caught) == "FOUNDRY_RESPONSE_INVALID"
    assert caught.value.body["error"]["message"] == "native control response exceeds its bound"
    assert oversized.last_session_request() is None
    with pytest.raises(Problem) as caught:
        make(lambda _request: httpx.Response(200, stream=Chunks(b"x" * (limit + 1)), headers=ECHO)
             ).observe(INVOCATION, PROBE)
    assert code(caught) == "FOUNDRY_NATIVE_BINDING_MISMATCH"


@pytest.mark.parametrize("call,headers,accepted", [
    ("observe", OBSERVED, True),
    ("observe", {"x-agent-invocation-id": INVOCATION}, True),
    ("observe", {"x-agent-session-id": TARGET.session_id}, False),
    ("observe", {**OBSERVED, "x-agent-invocation-id": ACTUAL_INVOCATION}, False),
    ("observe", {**OBSERVED, "x-agent-session-id": "another-session"}, False),
    ("observe", [("x-agent-invocation-id", INVOCATION), ("x-agent-invocation-id", INVOCATION)], False),
    ("observe", [("x-agent-invocation-id", INVOCATION), ("x-agent-session-id", TARGET.session_id),
                 ("x-agent-session-id", TARGET.session_id)], False),
    ("turn", ECHO, True),
    ("turn", {"x-agent-invocation-id": ACTUAL_INVOCATION}, False),
    ("turn", {**ECHO, "x-agent-session-id": "another-session"}, False),
    ("turn", {"x-agent-session-id": TARGET.session_id}, False),
    ("turn", {**ECHO, "x-agent-invocation-id": "bad/id"}, False),
])
def test_only_the_exact_invocation_and_session_echo_is_a_receipt(make, call, headers, accepted):
    client = make(lambda _request: httpx.Response(200, content=b"{}", headers=[*httpx.Headers(headers).items(), *JSON.items()]
                                                  if call == "turn" else headers))
    invoke = (lambda: turn(client)) if call == "turn" else lambda: client.observe(INVOCATION, PROBE)
    if accepted:
        assert invoke().status_code == 200
        return
    with pytest.raises(Problem) as caught:
        invoke()
    assert code(caught) == "FOUNDRY_NATIVE_BINDING_MISMATCH" and caught.value.status == 502
    assert client.last_session_request() is None


def test_the_read_deadline_starts_before_the_token_is_fetched(make, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(foundry.time, "monotonic", lambda: clock[0])
    slow = Token("good", then=lambda: clock.__setitem__(0, clock[0] + 121))
    for call in (lambda c: c.observe(INVOCATION, PROBE), turn):
        client = make(lambda _request: httpx.Response(200, stream=Chunks(b"{}"), headers={**OBSERVED, **JSON}), slow)
        with pytest.raises(Problem) as caught:
            call(client)
        assert code(caught) == "FOUNDRY_RESPONSE_INVALID" and caught.value.status == 502


@pytest.mark.parametrize("seconds,refused_read", [(120, False), (121, True)])
def test_a_slow_body_ends_at_the_deadline(make, monkeypatch, seconds, refused_read):
    clock = [1000.0]
    monkeypatch.setattr(foundry.time, "monotonic", lambda: clock[0])
    client = make(lambda _request: httpx.Response(200, stream=Chunks(
        b"{", b"}", between=lambda: clock.__setitem__(0, 1000.0 + seconds)), headers={**ECHO, **JSON}))
    if not refused_read:
        assert turn(client).body == b"{}"
        return
    with pytest.raises(Problem) as caught:
        turn(client)
    assert code(caught) == "FOUNDRY_RESPONSE_INVALID"
    assert caught.value.body["error"]["message"] == "complete native page exceeds its transport bound"


# Native turn replies: encoding, then the bounded body, then status, then type, then echo.

def test_a_turn_page_at_its_bound_is_returned_and_one_byte_more_is_refused_before_its_echo(make, caplog):
    limit = foundry.MAX_NATIVE_TURN_HTTP_BYTES
    assert len(turn(make(lambda _request: httpx.Response(200, stream=Chunks(b"x" * limit), headers={**ECHO, **JSON}))
                    ).body) == limit
    error, event = turn_failure(make, caplog, lambda _request: httpx.Response(200, stream=Chunks(b"x" * limit, b"y")))
    assert error.body["error"]["code"] == "FOUNDRY_RESPONSE_INVALID" and error.status == 502
    assert (event["phase"], event["status"]) == ("response_body", 200)


def test_a_compressed_turn_refusal_stops_at_its_headers(make, caplog):
    error, event = turn_failure(make, caplog, lambda _request: httpx.Response(
        409, stream=Chunks(b'{"error":{"code":"NATIVE_SESSION_CHANGED"}}'), headers={"content-encoding": "gzip"}))
    assert error.status == 502 and error.body["error"]["code"] == "FOUNDRY_RESPONSE_INVALID"
    assert (event["phase"], event["status"], event["upstream_error_code"]) == ("response_headers", 409, None)


@pytest.mark.parametrize("content_type,accepted", [
    ("application/json", True), ("Application/JSON; charset=utf-8", True), (" application/json ;x=y", True),
    ("text/plain", False), ("application/jsonl", False), (None, False),
])
def test_a_turn_page_must_be_json(make, caplog, content_type, accepted):
    def reply(_request):
        return httpx.Response(200, stream=Chunks(b"{}"), headers={
            **ECHO, **({} if content_type is None else {"content-type": content_type})})
    if accepted:
        assert turn(make(reply)).body == b"{}"
        return
    error, event = turn_failure(make, caplog, reply)
    assert error.status == 502 and error.body["error"] == {
        "code": "FOUNDRY_RESPONSE_INVALID", "message": "a complete native JSON page is required"}
    assert event["phase"] == "response_headers"


def test_the_type_of_a_page_is_checked_before_its_echo_and_a_refusal_needs_neither(make):
    with pytest.raises(Problem) as caught:
        turn(make(lambda _request: httpx.Response(200, content=b"{}", headers={"content-type": "text/plain"})))
    assert code(caught) == "FOUNDRY_RESPONSE_INVALID"
    with pytest.raises(Problem) as caught:
        turn(make(lambda _request: httpx.Response(409, content=b'{"error":{"code":"NATIVE_TURN_BUSY"}}',
                                                  headers={"content-type": "text/plain"})))
    assert code(caught) == "NATIVE_TURN_BUSY"


@pytest.mark.parametrize("raw,logged", [
    (b"not json", "UNCLASSIFIED"), (b"[]", "UNCLASSIFIED"), (b'{"other":1}', "UNCLASSIFIED"),
    (b'{"error":[]}', "UNCLASSIFIED"), (b'{"error":"text"}', "UNCLASSIFIED"), (b'{"error":{}}', "UNCLASSIFIED"),
    (b'{"error":{"code":7}}', "UNCLASSIFIED"), (b'{"error":{"code":"A","code":"B"}}', "UNCLASSIFIED"),
    (b'{"error":{"code":"A","x":NaN}}', "UNCLASSIFIED"), (b'{"error":{"code":"OTHER_ERROR"}}', "OTHER_ERROR"),
    (b'{"error":{"code":"' + b"a" * 32 + b'"}}', "UNCLASSIFIED"), (b'{"error":{"code":"' + b"a" * 65 + b'"}}', "UNCLASSIFIED"),
    (b'{"error":{"code":"' + b"g" * 64 + b'"}}', "g" * 64),
])
def test_an_unrecognized_refusal_stays_unknown_and_logs_only_a_safe_code(make, caplog, raw, logged):
    error, event = turn_failure(make, caplog, lambda _request: httpx.Response(500, content=raw, headers=JSON))
    assert error.status == 503 and error.body["error"] == {
        "code": "FOUNDRY_INVOCATION_UNKNOWN", "upstream_status": 500,
        "message": "native invocation outcome is unknown; reconcile the original request"}
    assert (event["phase"], event["status"], event["upstream_error_code"]) == ("response_status", 500, logged)


@pytest.mark.parametrize("status,kept", [(201, False), (304, False), (399, False), (400, True), (599, True), (600, False)])
def test_a_known_code_is_only_trusted_from_a_4xx_or_5xx_status(make, status, kept):
    error = refused(make, status, code="NATIVE_TURN_BUSY")
    assert (error.status, error.body["error"]["code"]) == (
        (status, "NATIVE_TURN_BUSY") if kept else (503, "FOUNDRY_INVOCATION_UNKNOWN"))


def test_a_refusal_carries_no_upstream_text_and_its_fields_in_a_fixed_order(make):
    error = refused(make, 409, "session_follow", code="NATIVE_SESSION_CHANGED", message="private text", not_dispatched=True,
                    reason="version", actual_version="8", current_hosted_session_id="current-session",
                    files_check={"category": "active_waiting"})
    assert list(error.body["error"].items()) == [
        ("code", "NATIVE_SESSION_CHANGED"),
        ("message", "the native agent refused this original command; no automatic retry"),
        ("automatic_retry", False), ("files_check", {"category": "active_waiting"}), ("reason", "version"),
        ("actual_version", "8"), ("current_hosted_session_id", "current-session"),
    ]


def test_a_refusal_names_why_before_its_old_image_mark(make):
    error = refused(make, 422, "session_prerestore", code="NATIVE_REQUEST_INVALID", reason="version",
                    message="session_prerestore is reserved; this image does not run it")
    assert list(error.body["error"])[-2:] == ["reason", "reserved"]


SUBMIT, RECEIVE = "submit", "agent_receive"
OTHER_CODE = "NATIVE_RID_CONFLICT"


@pytest.mark.parametrize("status,operation,error_code,flag,marked", [
    (409, SUBMIT, OTHER_CODE, True, True), (400, SUBMIT, OTHER_CODE, True, True), (499, SUBMIT, OTHER_CODE, True, True),
    (500, SUBMIT, OTHER_CODE, True, False), (409, SUBMIT, OTHER_CODE, False, False),
    (409, SUBMIT, OTHER_CODE, 1, False), (409, SUBMIT, OTHER_CODE, "true", False), (409, SUBMIT, OTHER_CODE, None, False),
    (503, SUBMIT, "AGENT_VERSION_FOLLOWING", True, True), (503, SUBMIT, "AGENT_VERSION_FOLLOWING", False, False),
    (503, RECEIVE, "AGENT_VERSION_FOLLOWING", True, False),
    (503, SUBMIT, "NATIVE_EXECUTION_UNAVAILABLE", True, True), (503, RECEIVE, "NATIVE_EXECUTION_UNAVAILABLE", True, True),
    (503, RECEIVE, "NATIVE_EXECUTION_UNAVAILABLE", False, False), (503, "read", "NATIVE_EXECUTION_UNAVAILABLE", True, False),
    (503, "continue", "NATIVE_EXECUTION_UNAVAILABLE", True, False), (409, RECEIVE, OTHER_CODE, True, False),
    (409, "read", OTHER_CODE, True, False),
    *[(409, operation, error_code, flag, True)
      for operation in ("submit", "agent_receive", "routine_receive", "continue")
      for error_code in ("NATIVE_CAPTURE_CHANGED", "NATIVE_PROFILE_UNSUPPORTED") for flag in (None, False)],
    (422, "continue", "NATIVE_CAPTURE_CHANGED", None, False), (409, "read", "NATIVE_CAPTURE_CHANGED", None, False),
    (409, "continue", OTHER_CODE, None, False), (503, "continue", "NATIVE_PROFILE_UNSUPPORTED", True, False),
])
def test_not_dispatched_is_only_kept_where_hosted_can_promise_it(make, status, operation, error_code, flag, marked):
    error = refused(make, status, operation, code=error_code, **({} if flag is None else {"not_dispatched": flag}))
    assert error.status == status and error.body["error"].get("not_dispatched") is (True if marked else None)


@pytest.mark.parametrize("reason,kept", [
    ("profile", True), ("runtime", True), ("version", True), ("replaced", True), ("private text", False), (None, False),
])
def test_only_a_known_pre_dispatch_reason_is_kept(make, reason, kept):
    error = refused(make, 409, code=OTHER_CODE, reason=reason)
    assert error.body["error"].get("reason") == (reason if kept else None)


@pytest.mark.parametrize("error_code,value,kept", [
    ("NATIVE_SESSION_CHANGED", "8", True), ("NATIVE_TURNS_DISABLED", "1234567890123456", True),
    (OTHER_CODE, "8", False), ("NATIVE_SESSION_CHANGED", "08", False), ("NATIVE_SESSION_CHANGED", 8, False),
    ("NATIVE_SESSION_CHANGED", "12345678901234567", False), ("NATIVE_SESSION_CHANGED", "8\n", False),
])
def test_only_a_version_refusal_names_the_actual_version(make, error_code, value, kept):
    error = refused(make, 409, code=error_code, actual_version=value)
    assert error.body["error"].get("actual_version") == (value if kept else None)


@pytest.mark.parametrize("error_code,value,kept", [
    ("NATIVE_SESSION_CHANGED", "current-session", True), ("NATIVE_TURNS_DISABLED", "current-session", False),
    ("NATIVE_SESSION_CHANGED", "x" * 64, True), ("NATIVE_SESSION_CHANGED", "x" * 65, False),
    ("NATIVE_SESSION_CHANGED", "bad/session", False), ("NATIVE_SESSION_CHANGED", 7, False),
])
def test_only_a_replaced_session_names_the_current_session(make, error_code, value, kept):
    error = refused(make, 409, code=error_code, current_hosted_session_id=value)
    assert error.body["error"].get("current_hosted_session_id") == (value if kept else None)


@pytest.mark.parametrize("operation,error_code,message,marked", [
    ("session_checkpoint_check", "NATIVE_REQUEST_INVALID", "session_checkpoint_check is reserved; this image does not run it", True),
    ("session_prerestore", "NATIVE_REQUEST_INVALID", "session_prerestore is reserved; this image does not run it", True),
    ("session_prerestore", "NATIVE_REQUEST_INVALID", "session_checkpoint_check is reserved; this image does not run it", False),
    ("session_read", "NATIVE_REQUEST_INVALID", "session_read is reserved; this image does not run it", False),
    ("session_prerestore", "NATIVE_TURN_BUSY", "session_prerestore is reserved; this image does not run it", False),
])
def test_only_the_exact_old_image_refusal_is_reserved(make, operation, error_code, message, marked):
    assert refused(make, 422, operation, code=error_code, message=message).body["error"].get("reserved") is (
        True if marked else None)


@pytest.mark.parametrize("status,operation,error_code,message,marked", [
    (422, "session_read", "NATIVE_REQUEST_INVALID", "only the typed native request is accepted", True),
    (422, "session_cut", "NATIVE_REQUEST_INVALID", "only the typed native request is accepted", True),
    (400, "session_cut", "NATIVE_REQUEST_INVALID", "only the typed native request is accepted", False),
    (422, "read", "NATIVE_REQUEST_INVALID", "only the typed native request is accepted", False),
    (422, "session_cut", "NATIVE_REQUEST_INVALID", "another refusal", False),
    (422, "session_cut", "NATIVE_TURN_BUSY", "only the typed native request is accepted", False),
])
def test_only_the_exact_old_image_refusal_is_untyped(make, status, operation, error_code, message, marked):
    assert refused(make, status, operation, code=error_code, message=message).body["error"].get("untyped") is (
        True if marked else None)


@pytest.mark.parametrize("operation,detail,status", [
    ("session_follow", "invalid", 502), ("session_files_omission", "invalid", 502), ("read", "invalid", 409),
    ("session_read", {"category": "active_waiting"}, 409), ("session_follow", None, 409),
])
def test_a_files_check_is_only_read_from_the_commands_that_ask_for_it(make, caplog, operation, detail, status):
    error, event = turn_failure(make, caplog, lambda _request: httpx.Response(409, json={"error": {
        "code": "NATIVE_FILES_CONFIRMATION_REQUIRED", "files_check": detail}}), operation)
    assert error.status == status and "files_check" not in error.body["error"] and "files_check" not in event
    assert event["upstream_error_code"] == "NATIVE_FILES_CONFIRMATION_REQUIRED" and event["phase"] == "response_status"


# What success records, and what the failure log says.

@pytest.mark.parametrize("call", [lambda c: c.observe(INVOCATION, PROBE), turn])
def test_only_a_valid_reply_marks_the_session_and_with_the_send_time(make, monkeypatch, call):
    times = iter(range(100, 200))
    monkeypatch.setattr(foundry, "monotonic", lambda: next(times))
    client = make(lambda _request: httpx.Response(200, content=b"{}", headers={**OBSERVED, **JSON}))
    call(client)
    assert client.last_session_request() == 100
    failed = make(lambda _request: httpx.Response(200, content=b"{}", headers=JSON))
    with pytest.raises(Problem):
        call(failed)
    assert failed.last_session_request() is None


def repointed_mid_request(make, reply):
    def handler(_request):
        client.target = MOVED
        return reply()
    client = make(handler)
    return client


@pytest.mark.parametrize(("call", "headers"), [
    (lambda client: turn(client, "session_follow", "prepare"), ECHO),
    (lambda client: client._request("GET", INVOCATION, action="observe"), OBSERVED),
], ids=["turn", "control"])
def test_a_request_stays_with_the_target_it_started_on_when_the_client_is_repointed(make, caplog, call, headers):
    caplog.set_level("INFO", logger="shared_context.behavior_foundry")
    client = repointed_mid_request(make, lambda: httpx.Response(200, json={}, headers={**headers, **JSON}))
    assert call(client).session_id == TARGET.session_id
    assert [event["session_id"] for event in events(caplog, "NATIVE_STAGE")] in ([], [TARGET.session_id])
    assert client.last_session_request() is None
    client.target = TARGET
    assert client.last_session_request() is not None


def test_a_repoint_in_before_send_does_not_change_the_session_the_reply_is_checked_against(make):
    sent = []

    def handler(request):
        sent.append((request.url.params["agent_session_id"], request.headers[NATIVE_EXPECTED_VERSION_HEADER]))
        return httpx.Response(200, json={}, headers={**ECHO, **JSON})
    client = make(handler)
    reply = turn(client, before_send=lambda: setattr(client, "target", MOVED))
    assert reply.session_id == TARGET.session_id
    assert sent == [(TARGET.session_id, TARGET.agent_version)]


def test_a_repoint_while_the_token_is_fetched_is_the_target_the_request_uses(make):
    sent = []

    def handler(request):
        sent.append((request.url.params["agent_session_id"], request.headers[NATIVE_EXPECTED_VERSION_HEADER]))
        return httpx.Response(200, json={}, headers={
            "x-agent-invocation-id": ACTUAL_INVOCATION, "x-agent-session-id": MOVED.session_id, **JSON})
    client = make(handler, Token("token", then=lambda: setattr(client, "target", MOVED)))
    assert turn(client).session_id == MOVED.session_id
    assert sent == [(MOVED.session_id, MOVED.agent_version)]


def test_a_failure_is_logged_on_the_target_the_request_started_on(make, caplog):
    client = repointed_mid_request(make, lambda: httpx.Response(409, json={"error": {"code": "NATIVE_TURN_BUSY"}}))
    with pytest.raises(Problem):
        turn(client)
    event, = events(caplog)
    assert (event["session_id"], event["expected_agent_version"]) == (TARGET.session_id, TARGET.agent_version)


def test_a_refused_turn_does_not_mark_the_session(make):
    client = make(lambda _request: httpx.Response(409, json={"error": {"code": "NATIVE_TURN_BUSY"}}))
    with pytest.raises(Problem):
        turn(client)
    assert client.last_session_request() is None


def test_an_answered_stage_logs_every_field_and_a_failed_stage_logs_none(make, monkeypatch, caplog):
    caplog.set_level("INFO", logger="shared_context.behavior_foundry")
    times = iter([1.0, 1.25])
    monkeypatch.setattr(foundry, "monotonic", lambda: next(times))
    turn(make(lambda _request: httpx.Response(202, json={}, headers=ECHO)), "session_follow", "prepare")
    assert events(caplog, "NATIVE_STAGE") == [{
        "native_operation": "session_follow", "native_stage": "prepare", "status": 202, "elapsed_ms": 250,
        "agent_id": TARGET.agent_id, "agent_name": TARGET.agent_name, "session_id": TARGET.session_id}]
    caplog.clear()
    times = iter([1.0, 1.25])
    with pytest.raises(Problem):
        turn(make(lambda _request: httpx.Response(200, json={})), "session_follow", "prepare")
    assert events(caplog, "NATIVE_STAGE") == []


def test_a_turn_failure_logs_every_field_once(make, monkeypatch, caplog):
    times = iter([1.0, 1.25])
    monkeypatch.setattr(foundry, "monotonic", lambda: next(times))

    def reply(_request):
        return httpx.Response(409, json={"error": {"code": "NATIVE_FILES_CONFIRMATION_REQUIRED",
                                                   "files_check": {"category": "active_waiting"}}},
                              headers={"x-request-id": "12345678-1234-1234-1234-123456789abc"})
    with pytest.raises(Problem):
        turn(make(reply), "session_follow", "prepare")
    assert events(caplog) == [{
        "phase": "response_status", "status": 409, "exception_type": None, "elapsed_ms": 250,
        "agent_id": TARGET.agent_id, "agent_name": TARGET.agent_name, "session_id": TARGET.session_id,
        "path": "invocations", "api_version": "v1",
        "upstream_error_code": "NATIVE_FILES_CONFIRMATION_REQUIRED", "native_operation": "session_follow",
        "native_stage": "prepare", "expected_agent_version": TARGET.agent_version,
        "files_check": {"category": "active_waiting"}, "request_id": "12345678-1234-1234-1234-123456789abc",
    }]


@pytest.mark.parametrize("operation,status,error_code,name", [
    ("read", 404, "NATIVE_RID_MISSING", "NATIVE_RID_ABSENT"),
    ("read", 409, "NATIVE_RID_MISSING", "NATIVE_INVOCATION_FAILURE"),
    ("read", 404, "NATIVE_TURN_UNKNOWN", "NATIVE_INVOCATION_FAILURE"),
    ("submit", 404, "NATIVE_RID_MISSING", "NATIVE_INVOCATION_FAILURE"),
])
def test_only_a_missing_rid_read_is_an_answer(make, caplog, operation, status, error_code, name):
    refused(make, status, operation, code=error_code)
    assert len(events(caplog, name)) == 1
    assert len(events(caplog, "NATIVE_RID_ABSENT")) + len(events(caplog)) == 1


def test_a_stream_that_fails_as_it_closes_is_a_body_failure_with_its_status(make, caplog):
    error, event = turn_failure(make, caplog, lambda _request: httpx.Response(
        200, stream=Chunks(b"{}", closing=httpx.ReadError("private")), headers={**ECHO, **JSON}))
    assert error.status == 503 and error.body["error"] == {
        "code": "FOUNDRY_INVOCATION_UNKNOWN", "message": "native transport outcome is unknown; do not resend"}
    assert (event["phase"], event["status"], event["exception_type"]) == ("response_body", 200, "ReadError")


def test_a_control_failure_is_classified_but_not_logged_as_a_native_turn(make, caplog):
    def lost(request):
        raise httpx.ConnectError("private", request=request)

    with pytest.raises(Problem) as caught:
        make(lost).observe(INVOCATION, PROBE)
    assert caught.value.status == 503 and caught.value.body["error"] == {
        "code": "FOUNDRY_INVOCATION_UNKNOWN", "message": "native transport outcome is unknown; do not resend"}
    assert not events(caplog)


@pytest.mark.parametrize("call", [lambda c: c.observe(INVOCATION, PROBE), turn])
def test_an_unexpected_error_is_not_turned_into_an_unknown_outcome(make, caplog, call):
    def broken(_request):
        raise RuntimeError("a bug, not a transport outcome")

    with pytest.raises(RuntimeError):
        call(make(broken))
    assert not events(caplog)
