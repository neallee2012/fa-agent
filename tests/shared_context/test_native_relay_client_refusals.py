"""Pins NativeAgentRelayClient's transport, environment follow and queued-send recovery, without a Hosted runtime.

Every refusal is asserted by status, code and message, so another refusal reached first fails the test.
"""

import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from azure.core.exceptions import AzureError

from shared_context import native_agent_tools
from shared_context.behavior_native_agent_wire import (
    NATIVE_AGENT_PREFIX, NativePeerRequest, NativeRelayAcceptance, NativeRelaySend, relay_task_id,
)
from shared_context.behavior_native_turn_wire import NATIVE_TURN_ERRORS, NativeEnvironmentView
from shared_context.errors import Problem
from shared_context.native_agent_tools import (
    _MAX_RESPONSE_BYTES, NativeAgentRelayClient, NativeRecipientFollowing, NativeRecipientNotReady, _WaitingEnvironment,
)
from shared_context.native_continuation_wire import CONTINUATION_STOP_STATUS

UNKNOWN = "NATIVE_RELAY_OUTCOME_UNKNOWN"
REFUSED = "NATIVE_RELAY_REFUSED"
NO_RECEIPT = "the original relay request did not return its receipt"
NO_CREDENTIAL = (503, UNKNOWN, "the original relay credential is unavailable")
ACCEPTED = NativeRelayAcceptance(
    message_id="original-message", recipient_agent_id="peer", conversation_id="peer-cid",
    submit_digest="a" * 64, status="accepted")
COMMAND = NativeRelaySend(
    conversation_id="sender-cid", request_id="sender-rid", tool_call_id="original-send",
    message=NativePeerRequest(recipient_agent_id="peer", kind="request", content="Original public brief."))


async def token():
    return "synthetic-relay-token"


def failure(error):
    return (error.status, error.body["error"]["code"], error.body["error"]["message"])


def run(call):
    with pytest.raises(Problem) as caught:
        asyncio.run(call)
    return caught.value


def relay(respond, *, provider=token, runtime=None):
    client = NativeAgentRelayClient("https://api.invalid", provider, transport=httpx.MockTransport(respond))
    client.runtime = runtime
    return client


def refused_by(status, upstream, suffix="/send", *, runtime=None, content=None):
    body = {"error": {"code": upstream, "message": "PRIVATE_UPSTREAM"}} if content is None else content

    def respond(_request):
        return httpx.Response(status, content=body) if isinstance(body, bytes) else httpx.Response(status, json=body)

    return run(relay(respond, runtime=runtime)._request(suffix, NativeRelayAcceptance, COMMAND))


@pytest.mark.parametrize("error", [OSError("offline"), AzureError("offline")], ids=["os", "azure"])
def test_an_unavailable_credential_is_unconfirmed_and_never_reaches_the_api(error, caplog):
    async def provider():
        raise error

    with caplog.at_level(logging.WARNING, logger=native_agent_tools.__name__):
        problem = run(relay(lambda _: pytest.fail("no request without a credential"), provider=provider)._request(
            "/send", NativeRelayAcceptance, COMMAND))
    assert failure(problem) == NO_CREDENTIAL
    assert f"NATIVE_RELAY_TOKEN_UNAVAILABLE exception_type={type(error).__name__}" in caplog.text


def test_another_credential_error_escapes_as_it_is():
    async def provider():
        raise LookupError("a programming error")

    with pytest.raises(LookupError):
        asyncio.run(relay(lambda _: pytest.fail("no request"), provider=provider)._request("/recipients", NativeRelayAcceptance))


@pytest.mark.parametrize("value", [None, 7, "", "line\nbreak", "line\rbreak"],
                         ids=["none", "number", "empty", "newline", "return"])
def test_a_token_that_cannot_be_a_header_is_refused_before_the_request(value):
    async def provider():
        return value

    problem = run(relay(lambda _: pytest.fail("no request"), provider=provider)._request("/send", NativeRelayAcceptance, COMMAND))
    assert failure(problem) == NO_CREDENTIAL


def test_a_command_is_posted_with_the_exact_headers_and_body():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(202, content=ACCEPTED.model_dump_json().encode())

    assert asyncio.run(relay(respond)._request("/send", NativeRelayAcceptance, COMMAND)) == ACCEPTED
    request = seen[0]
    assert (request.method, str(request.url)) == ("POST", "https://api.invalid" + NATIVE_AGENT_PREFIX + "/send")
    assert request.content == COMMAND.model_dump_json().encode()
    assert (request.headers["authorization"], request.headers["content-type"], request.headers["accept"],
            request.headers["accept-encoding"]) == (
                "Bearer synthetic-relay-token", "application/json", "application/json", "identity")


def test_a_read_is_a_get_without_a_body():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, content=ACCEPTED.model_dump_json().encode())

    asyncio.run(relay(respond)._request("/recipients", NativeRelayAcceptance))
    assert seen[0].method == "GET" and seen[0].content == b""


@pytest.mark.parametrize("suffix,read", [("/send", 20), ("/environment", 90), ("/environment/observe", 90)])
def test_only_the_environment_routes_wait_longer(suffix, read):
    seen = []

    def respond(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, content=ACCEPTED.model_dump_json().encode())

    asyncio.run(relay(respond)._request(suffix, NativeRelayAcceptance, COMMAND))
    assert seen[0] == {"connect": 5, "read": read, "write": read, "pool": read}


def test_a_redirect_is_not_followed():
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(302, headers={"location": "https://other.invalid/"})

    problem = run(relay(respond)._request("/send", NativeRelayAcceptance, COMMAND))
    assert failure(problem) == (302, UNKNOWN, NO_RECEIPT) and len(calls) == 1


def test_the_proxy_settings_of_the_environment_are_ignored(monkeypatch):
    seen = {}

    class Client(httpx.AsyncClient):
        def __init__(self, **options):
            seen.update(options)
            super().__init__(**options)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    run(relay(lambda _request: httpx.Response(500))._request("/send", NativeRelayAcceptance, COMMAND))
    assert seen["trust_env"] is False


def test_an_encoded_response_is_refused_before_its_status_is_read():
    def respond(_request):
        return httpx.Response(403, headers={"content-encoding": "x-synthetic"}, content=b"{}")

    assert failure(run(relay(respond)._request("/send", NativeRelayAcceptance, COMMAND))) == (
        502, UNKNOWN, "the relay encoding is unsupported")


def test_a_response_at_its_bound_is_read_and_one_byte_more_is_refused():
    fit = ACCEPTED.model_dump_json().encode()
    padded = fit + b" " * (_MAX_RESPONSE_BYTES - len(fit))
    assert asyncio.run(relay(lambda _: httpx.Response(200, content=padded))._request(
        "/send", NativeRelayAcceptance, COMMAND)) == ACCEPTED
    assert failure(run(relay(lambda _: httpx.Response(200, content=padded + b" "))._request(
        "/send", NativeRelayAcceptance, COMMAND))) == (502, UNKNOWN, "the relay response exceeds its bound")


def test_an_oversized_refusal_is_refused_by_its_size():
    body = json.dumps({"error": {"code": REFUSED}}).encode() + b" " * _MAX_RESPONSE_BYTES
    assert failure(refused_by(409, None, content=body)) == (502, UNKNOWN, "the relay response exceeds its bound")


@pytest.mark.parametrize("status", [200, 202])
def test_a_receipt_that_does_not_parse_is_unconfirmed(status):
    assert failure(run(relay(lambda _: httpx.Response(status, content=b"{}"))._request(
        "/send", NativeRelayAcceptance, COMMAND))) == (502, UNKNOWN, "the original relay response is invalid")


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
def test_a_transport_failure_is_unconfirmed(caplog, error):
    def respond(request):
        raise error("lost", request=request)

    with caplog.at_level(logging.WARNING, logger=native_agent_tools.__name__):
        problem = run(relay(respond)._request("/send", NativeRelayAcceptance, COMMAND))
    assert failure(problem) == (503, UNKNOWN, "the original relay outcome is unconfirmed")
    assert f"NATIVE_RELAY_UNCONFIRMED exception_type={error.__name__}" in caplog.text


@pytest.mark.parametrize("upstream,unconfirmed", [("AGENT_VERSION_FOLLOWING", False), ("NATIVE_ADMISSION_PENDING", True)])
@pytest.mark.parametrize("runtime", [None, object()], ids=["unbound", "bound"])
def test_a_following_recipient_is_named(upstream, unconfirmed, runtime):
    problem = refused_by(503, upstream, runtime=runtime)
    assert isinstance(problem, NativeRecipientFollowing) and problem.admission_unconfirmed is unconfirmed
    assert failure(problem) == (
        503, "AGENT_VERSION_FOLLOWING", "the recipient is changing environments; retain the original message")


@pytest.mark.parametrize("status,code", [(409, REFUSED), (500, UNKNOWN)])
def test_following_needs_a_503(status, code):
    problem = refused_by(status, "AGENT_VERSION_FOLLOWING")
    assert not isinstance(problem, NativeRecipientFollowing) and failure(problem) == (status, code, NO_RECEIPT)


@pytest.mark.parametrize("suffix", ["/send", "/environment", "/environment/observe"])
@pytest.mark.parametrize("status", [403, 409, 503])
@pytest.mark.parametrize("upstream", ["AGENT_MESSAGE_RECIPIENT_NOT_READY", "NATIVE_RELAY_RECIPIENT_UNAVAILABLE"])
def test_a_bound_client_waits_for_a_recipient_that_is_not_ready(suffix, status, upstream):
    problem = refused_by(status, upstream, suffix, runtime=object())
    assert isinstance(problem, NativeRecipientNotReady) and failure(problem) == (
        503, "NATIVE_RELAY_RECIPIENT_UNAVAILABLE", "the original registered recipient is not ready; retain the same queued message")


@pytest.mark.parametrize("suffix,status,upstream,code", [
    ("/send", 403, "AGENT_MESSAGE_RECIPIENT_NOT_READY", REFUSED),
    ("/send", 503, "AGENT_MESSAGE_RECIPIENT_NOT_READY", UNKNOWN),
    ("/environment", 503, "NATIVE_RELAY_RECIPIENT_UNAVAILABLE", UNKNOWN),
    ("/send", 409, "NATIVE_RELAY_RECIPIENT_UNAVAILABLE", "NATIVE_RELAY_RECIPIENT_UNAVAILABLE"),
])
def test_an_unbound_client_never_waits_for_a_recipient(suffix, status, upstream, code):
    problem = refused_by(status, upstream, suffix)
    assert not isinstance(problem, NativeRecipientNotReady) and failure(problem) == (status, code, NO_RECEIPT)


@pytest.mark.parametrize("suffix,status,upstream", [
    ("/result", 409, "NATIVE_RELAY_RECIPIENT_UNAVAILABLE"),
    ("/send", 422, "NATIVE_RELAY_RECIPIENT_UNAVAILABLE"),
    ("/send", 409, "ANOTHER_REFUSAL"),
])
def test_only_the_named_recipient_refusals_wait(suffix, status, upstream):
    problem = refused_by(status, upstream, suffix, runtime=object())
    assert not isinstance(problem, NativeRecipientNotReady) and failure(problem) == (status, REFUSED, NO_RECEIPT)


@pytest.mark.parametrize("suffix", ["/environment", "/environment/observe"])
def test_an_environment_route_keeps_a_native_code_at_any_status(suffix):
    upstream = sorted(NATIVE_TURN_ERRORS)[0]
    assert failure(refused_by(500, upstream, suffix)) == (500, upstream, NO_RECEIPT)


@pytest.mark.parametrize("status,upstream,code", [
    (409, "NATIVE_SESSION_CHANGED", "NATIVE_SESSION_CHANGED"),
    (403, "WORK_POLICY_REVOKED", "WORK_POLICY_REVOKED"),
    (403, "NATIVE_SESSION_CHANGED", REFUSED),
    (409, "WORK_POLICY_REVOKED", REFUSED),
    (503, "NATIVE_SESSION_CHANGED", UNKNOWN),
])
def test_waiting_dispatch_keeps_only_its_two_named_refusals(status, upstream, code):
    assert failure(refused_by(status, upstream, "/waiting-dispatch")) == (status, code, NO_RECEIPT)


@pytest.mark.parametrize("upstream,status", sorted(CONTINUATION_STOP_STATUS.items()))
def test_continue_keeps_a_stop_only_at_its_own_status(upstream, status):
    assert failure(refused_by(status, upstream, "/continue")) == (status, upstream, NO_RECEIPT)
    other = 409 if status == 403 else 403
    assert failure(refused_by(other, upstream, "/continue")) == (other, REFUSED, NO_RECEIPT)
    assert failure(refused_by(status, "ANOTHER_REFUSAL", "/continue")) == (status, REFUSED, NO_RECEIPT)


def test_send_keeps_a_group_refusal_only_at_409():
    assert failure(refused_by(409, "NATIVE_GROUP_REPLY_REQUIRED")) == (409, "NATIVE_GROUP_REPLY_REQUIRED", NO_RECEIPT)
    assert failure(refused_by(403, "NATIVE_GROUP_REPLY_REQUIRED")) == (403, REFUSED, NO_RECEIPT)
    assert failure(refused_by(409, "NATIVE_GROUP_REPLY_REQUIRED", "/result")) == (409, REFUSED, NO_RECEIPT)


@pytest.mark.parametrize("status", [403, 409, 503])
def test_send_keeps_an_unavailable_recipient_at_its_three_statuses(status):
    upstream = "NATIVE_RELAY_RECIPIENT_UNAVAILABLE"
    assert failure(refused_by(status, upstream)) == (status, upstream, NO_RECEIPT)


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422])
def test_a_reviewed_refusal_status_is_a_refusal(status):
    assert failure(refused_by(status, "ANOTHER_REFUSAL", "/result")) == (status, REFUSED, NO_RECEIPT)


@pytest.mark.parametrize("status", [404, 429, 500, 503])
def test_any_other_status_is_unconfirmed(status):
    assert failure(refused_by(status, "ANOTHER_REFUSAL", "/result")) == (status, UNKNOWN, NO_RECEIPT)


@pytest.mark.parametrize("upstream", [
    "NATIVE_RELAY_QUOTA_UNKNOWN", "FOUNDRY_INVOCATION_UNKNOWN", "NATIVE_RESPONSE_UNCONFIRMED", "NATIVE_TURN_UNKNOWN",
    "NATIVE_POST_TURN_UNKNOWN", "NATIVE_STATE_STORE_UNKNOWN",
])
def test_an_uncertain_code_is_never_a_refusal(upstream):
    assert failure(refused_by(409, upstream, "/result")) == (409, UNKNOWN, NO_RECEIPT)


@pytest.mark.parametrize("upstream", [["NATIVE_RELAY_REFUSED"], {"code": "NATIVE_RELAY_REFUSED"}], ids=["list", "dict"])
@pytest.mark.parametrize("suffix,status", [
    ("/environment", 500), ("/continue", 500), ("/result", 409), ("/send", 503), ("/waiting-dispatch", 409),
])
def test_a_code_that_is_not_text_is_unconfirmed_on_a_route_that_names_codes(suffix, status, upstream):
    assert failure(refused_by(status, upstream, suffix)) == (status, UNKNOWN, NO_RECEIPT)


@pytest.mark.parametrize("content", [
    b"not json", b"[]", b'"text"', b'{"error": "text"}', b'{"error": {}}', b'{"error": {"code": 7}}', b"\xff",
], ids=["text", "list", "string", "flat-error", "no-code", "number-code", "bytes"])
def test_a_refusal_without_a_named_code_is_unconfirmed(content):
    assert failure(refused_by(409, None, "/result", content=content)) == (409, UNKNOWN, NO_RECEIPT)


@pytest.mark.parametrize("suffix,route", [
    ("/send", "/send"), ("/environment", "/environment"), ("/environment/observe", "/environment/observe"),
    ("/continue", "/continue"), ("/result", "/result"), ("/outcome", "/outcome"), ("/read-result", "/read-result"),
    ("/recipients", "/recipients"), ("/waiting-dispatch", "/waiting-dispatch"), ("/anything-else", "unclassified"),
])
def test_a_refusal_is_logged_by_its_route_status_and_code(suffix, route, caplog):
    with caplog.at_level(logging.WARNING, logger=native_agent_tools.__name__):
        refused_by(409, "ANOTHER_REFUSAL", suffix)
    assert f"NATIVE_RELAY_REQUEST_FAILED route={route} status=409 code={REFUSED}" in caplog.text
    assert "PRIVATE_UPSTREAM" not in caplog.text


@pytest.mark.parametrize("problem", ["following", "not-ready"])
def test_a_named_recipient_state_is_not_logged_as_a_failed_request(problem, caplog):
    upstream = "AGENT_VERSION_FOLLOWING" if problem == "following" else "NATIVE_RELAY_RECIPIENT_UNAVAILABLE"
    with caplog.at_level(logging.WARNING, logger=native_agent_tools.__name__):
        refused_by(503, upstream, runtime=object())
    assert "NATIVE_RELAY_REQUEST_FAILED" not in caplog.text


def view(state="pending", **changes):
    values = dict(agent_id="peer", request_id="peer-follow", previous_hosted_session_id="old-peer", previous_agent_version="41",
                  hosted_session_id="new-peer", hosted_agent_version="42", state=state)
    return NativeEnvironmentView(**{**values, **changes})


TOKEN_ONE, TOKEN_TWO = "1" * 64, "2" * 64
ENV, OBS = "/environment", "/environment/observe"
PEER = (502, "NATIVE_STATE_SCOPE", "the environment belongs to another recipient")
FILES = (503, "NATIVE_FILES_UNKNOWN", "the original file effect requires reconciliation")
DENIED = (403, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the original task observation was denied")


class Follow:
    """Replays one scripted reply per request and records the requests and waits."""

    def __init__(self, replies, *, waits=None, on_reply=None):
        self.ctx = SimpleNamespace(shutdown=asyncio.Event(), cancel=asyncio.Event())
        self.client = NativeAgentRelayClient("https://api.invalid", token)
        self.replies, self.routes, self.waited = list(replies), [], []
        self.waits, self.on_reply = None if waits is None else list(waits), on_reply
        self.client._request = self.request
        self.client._wait = self.wait

    async def request(self, suffix, model, command=None):
        assert model is NativeEnvironmentView and command is COMMAND
        self.routes.append(suffix)
        reply = self.replies.pop(0)
        if self.on_reply is not None:
            self.on_reply(self, len(self.routes))
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def wait(self, ctx, attempt):
        assert ctx is self.ctx
        self.waited.append(attempt)
        return True if self.waits is None else self.waits.pop(0)

    def run(self):
        return asyncio.run(self.client._follow_environment(self.ctx, COMMAND))


@pytest.mark.parametrize("state", ["ready", "current"])
def test_a_ready_environment_ends_the_follow_at_once(state):
    follow = Follow([view(state)])
    assert follow.run() is True and follow.routes == [ENV] and follow.waited == []


def test_the_first_pending_view_is_observed_once_for_a_baseline():
    follow = Follow([view(), view(progress_token=TOKEN_ONE), view(), view("ready")])
    assert follow.run() is True
    assert follow.routes == [ENV, OBS, ENV, ENV] and follow.waited == [1, 2]


@pytest.mark.parametrize("state", ["ready", "current"])
def test_a_ready_observation_ends_the_follow(state):
    follow = Follow([view(), view(state)])
    assert follow.run() is True and follow.routes == [ENV, OBS] and follow.waited == []


BAD_VIEWS = [
    (dict(agent_id="another-peer"), PEER), (dict(files_reconcile_required=True), FILES),
    (dict(reason="task_access_denied"), DENIED),
    (dict(agent_id="another-peer", files_reconcile_required=True, reason="task_access_denied"), PEER),
    (dict(files_reconcile_required=True, reason="task_access_denied"), FILES),
]


@pytest.mark.parametrize("changes,expected", BAD_VIEWS, ids=["peer", "files", "denied", "peer-first", "files-first"])
@pytest.mark.parametrize("state", ["pending", "current"])
def test_a_view_of_another_recipient_or_effect_is_refused_before_it_counts_as_ready(changes, expected, state):
    assert failure(run_follow(Follow([view(state, **changes)]))) == expected


@pytest.mark.parametrize("changes,expected", BAD_VIEWS, ids=["peer", "files", "denied", "peer-first", "files-first"])
@pytest.mark.parametrize("state", ["pending", "current"])
def test_an_observation_is_held_to_the_same_refusals(changes, expected, state):
    assert failure(run_follow(Follow([view(), view(state, **changes)]))) == expected


def run_follow(follow):
    with pytest.raises(Problem) as caught:
        follow.run()
    return caught.value


def test_a_recipient_not_ready_waits_without_counting_an_attempt():
    follow = Follow([NativeRecipientNotReady(), NativeRecipientNotReady(), view("ready")])
    assert follow.run() is True and follow.routes == [ENV, ENV, ENV] and follow.waited == [0, 0]


def test_a_recipient_not_ready_during_the_baseline_observation_keeps_following_without_a_second_one():
    follow = Follow([view(), NativeRecipientNotReady(), view(), view("ready")])
    assert follow.run() is True
    assert follow.routes == [ENV, OBS, ENV, ENV] and follow.waited == [1, 2]


def test_another_refusal_escapes_the_follow():
    original = Problem(409, "NATIVE_RELAY_REFUSED", "refused")
    with pytest.raises(Problem) as caught:
        Follow([original]).run()
    assert caught.value is original
    with pytest.raises(Problem) as caught:
        Follow([view(), original]).run()
    assert caught.value is original


def pending_to_ten():
    return [view(progress_token=TOKEN_ONE), view(progress_token=TOKEN_ONE), *(view() for _ in range(9))]


def test_after_ten_attempts_the_follow_only_observes_until_progress_changes():
    follow = Follow([*pending_to_ten(), view(progress_token=TOKEN_ONE), view(), NativeRecipientNotReady(),
                     view(progress_token=TOKEN_TWO), view(), view("ready")])
    assert follow.run() is True
    assert follow.routes == [ENV, OBS, *[ENV] * 9, OBS, OBS, OBS, OBS, ENV, ENV]
    assert follow.waited == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 0, 1]


def test_a_stalled_observer_never_returns_to_advancing():
    follow = Follow([*pending_to_ten(), view(progress_token=TOKEN_ONE), view(progress_token=TOKEN_ONE), view("ready")])
    assert follow.run() is True and follow.routes[-3:] == [OBS, OBS, OBS]


def test_a_progress_token_becomes_the_next_baseline():
    follow = Follow([*pending_to_ten(), view(progress_token=TOKEN_TWO), *(view() for _ in range(10)),
                     view(progress_token=TOKEN_TWO), view("ready")])
    assert follow.run() is True and follow.routes[-2:] == [OBS, OBS]


def test_a_baseline_without_a_token_is_replaced_by_the_first_token_seen():
    follow = Follow([view(), view(), *(view() for _ in range(9)), view(progress_token=TOKEN_ONE), view("ready")])
    assert follow.run() is True
    assert follow.routes[-2:] == [OBS, ENV] and follow.waited[-1] == 0


def test_a_view_that_refuses_while_only_observing_is_still_refused():
    follow = Follow([*pending_to_ten(), view(reason="task_access_denied")])
    assert failure(run_follow(follow)) == DENIED


@pytest.mark.parametrize("replies,stop_after", [
    ([view("ready")], 1), ([view(), view("ready")], 2),
], ids=["first-view", "baseline-observation"])
def test_shutdown_during_a_request_leaves_for_recovery_even_when_ready(replies, stop_after):
    def stop(follow, count):
        if count == stop_after:
            follow.ctx.shutdown.set()

    follow = Follow(replies, on_reply=stop)
    assert follow.run() is False and len(follow.routes) == stop_after


def test_shutdown_before_the_first_request_sends_nothing():
    follow = Follow([])
    follow.ctx.shutdown.set()
    assert follow.run() is False and follow.routes == []


def test_cancel_during_a_request_raises():
    def cancel(follow, _count):
        follow.ctx.cancel.set()

    with pytest.raises(asyncio.CancelledError):
        Follow([view("ready")], on_reply=cancel).run()


@pytest.mark.parametrize("replies,waits,calls", [
    ([NativeRecipientNotReady()], [False], 1),
    ([view(), NativeRecipientNotReady()], [False], 2),
    ([view(), view()], [False], 2),
    ([view(), view(), view()], [True, False], 3),
], ids=["not-ready", "baseline-not-ready", "pending", "second-pending"])
def test_a_wait_ended_by_shutdown_ends_the_follow(replies, waits, calls):
    follow = Follow(replies, waits=waits)
    assert follow.run() is False and len(follow.routes) == calls


AGENT, SESSION = "sender-agent", "sender-session"
SCOPE_CODE = "NATIVE_STATE_SCOPE"


class Recovery:
    def __init__(self, monkeypatch, records=None, saved=None, accepted=ACCEPTED, run_found=True, **receipt):
        self.receipt = SimpleNamespace(**{**dict(
            status="started", cancel_requested=False, delegation=None, conversation_id=COMMAND.conversation_id,
            request_id=COMMAND.request_id, agent_id=AGENT, hosted_session_id=SESSION), **receipt})
        self.records = [operation()] if records is None else records
        self.saved = {relay_task_id(AGENT, COMMAND): task()} if saved is None else saved
        self.runtime = SimpleNamespace(identity=SimpleNamespace(agent_name="sender-name", session_id=SESSION))
        self.original = SimpleNamespace(value=SimpleNamespace(receipt=self.receipt))
        self.read, self.joined, self.retained, self.opened = [], [], [], []
        self.client = NativeAgentRelayClient("https://api.invalid", token)
        recovery = self

        class Effects:
            def __init__(self, runtime, receipt):
                recovery.opened.append((runtime, receipt))

            async def operation_records(self):
                if isinstance(recovery.records, Exception):
                    raise recovery.records
                return recovery.records

        async def provider_get(identifier):
            self.read.append(identifier)
            return self.saved.get(identifier)

        class Run:
            async def result(self):
                return accepted

        async def active(identifier):
            self.joined.append(identifier)
            return Run() if run_found else None

        async def retain(runtime, original, value):
            self.retained.append((runtime, original, value))

        monkeypatch.setattr("shared_context.native_effects.NativeEffects", Effects)
        monkeypatch.setattr(native_agent_tools, "get_task_manager",
                            lambda: SimpleNamespace(provider=SimpleNamespace(get=provider_get)))
        self.client._following = SimpleNamespace(get_active_run=active)
        self.client._retain_queued_acceptance = retain

    def run(self):
        return asyncio.run(self.client.recover_queued(self.runtime, self.original))


def operation(tool_name="send_agent_message", arguments=None, refusal_code=None, dispatch_started=True,
              tool_call_id=COMMAND.tool_call_id):
    return SimpleNamespace(
        operation=SimpleNamespace(tool_name=tool_name, arguments=COMMAND.message.model_dump(mode="json")
                                  if arguments is None else arguments),
        refusal_code=refusal_code, dispatch_started=dispatch_started, tool_call_id=tool_call_id)


def task(**changes):
    values = dict(agent_name="sender-name", session_id=SESSION,
                  source={"type": "agentserver.task", "name": "fa-native-version-message-v1"},
                  payload={"input": COMMAND.model_dump(mode="json")})
    return SimpleNamespace(**{**values, **changes})


@pytest.mark.parametrize("changes", [
    {"status": "completed"}, {"status": "accepted"}, {"cancel_requested": True}, {"delegation": ACCEPTED},
], ids=["completed", "accepted", "cancelled", "delegated"])
def test_only_a_live_undelegated_sender_is_recovered(monkeypatch, changes):
    recovery = Recovery(monkeypatch, **changes)
    assert recovery.run() is None and recovery.opened == [] and recovery.read == []


def test_a_retained_original_task_is_joined_and_its_acceptance_retained(monkeypatch):
    recovery = Recovery(monkeypatch)
    assert recovery.run() is None
    identifier = relay_task_id(AGENT, COMMAND)
    assert recovery.opened == [(recovery.runtime, recovery.receipt)]
    assert recovery.read == recovery.joined == [identifier]
    assert recovery.retained == [(recovery.runtime, recovery.original, ACCEPTED)]


def test_a_task_waiting_for_the_environment_is_the_same_original(monkeypatch):
    identifier = relay_task_id(AGENT, COMMAND)
    waiting = task(payload={"input": _WaitingEnvironment(command=COMMAND).model_dump(mode="json")})
    recovery = Recovery(monkeypatch, saved={identifier: waiting})
    recovery.run()
    assert recovery.retained


def test_leaving_a_recovery_does_not_cancel_the_original_send(monkeypatch):
    recovery = Recovery(monkeypatch)
    cancelled = []

    async def leave():
        started = asyncio.Event()

        class Run:
            async def result(self):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise

        async def active(_identifier):
            return Run()

        recovery.client._following = SimpleNamespace(get_active_run=active)
        waiting = asyncio.create_task(recovery.client.recover_queued(recovery.runtime, recovery.original))
        await started.wait()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert cancelled == []

    asyncio.run(leave())


def test_records_unavailable_before_the_watermark_recover_nothing(monkeypatch):
    recovery = Recovery(monkeypatch, records=Problem(409, "NATIVE_CONTINUATION_WATERMARK_UNAVAILABLE", "synthetic"))
    assert recovery.run() is None and recovery.read == []


def test_another_record_failure_escapes(monkeypatch):
    original = Problem(409, "NATIVE_STATE_INVALID", "synthetic")
    with pytest.raises(Problem) as caught:
        Recovery(monkeypatch, records=original).run()
    assert caught.value is original


@pytest.mark.parametrize("skipped", [
    SimpleNamespace(operation=None, refusal_code=None, dispatch_started=True, tool_call_id="x"),
    operation(tool_name="another_tool"), operation(refusal_code="NATIVE_RELAY_REFUSED"), operation(dispatch_started=False),
    operation(dispatch_started=None),
], ids=["no-operation", "another-tool", "refused", "not-dispatched", "unknown-dispatch"])
def test_only_a_dispatched_unrefused_send_is_looked_up(monkeypatch, skipped):
    recovery = Recovery(monkeypatch, records=[skipped])
    assert recovery.run() is None and recovery.read == [] and recovery.retained == []
    # A skipped record does not end the scan.
    following = Recovery(monkeypatch, records=[skipped, operation()])
    following.run()
    assert following.retained


@pytest.mark.parametrize("arguments", [{"kind": "request"}, {"content": object()}], ids=["partial", "not-json"])
def test_an_indexed_send_that_is_not_a_message_is_refused(monkeypatch, arguments):
    recovery = Recovery(monkeypatch, records=[operation(arguments=arguments)])
    assert failure(run_recovery(recovery)) == (409, SCOPE_CODE, "the indexed send input is invalid")


def run_recovery(recovery):
    with pytest.raises(Problem) as caught:
        recovery.run()
    return caught.value


def test_a_send_without_a_saved_task_moves_on_to_the_next_record(monkeypatch):
    other = operation(tool_call_id="another-send")
    recovery = Recovery(monkeypatch, records=[other, operation()])
    recovery.run()
    assert len(recovery.read) == 2 and recovery.joined == [relay_task_id(AGENT, COMMAND)] and recovery.retained


def test_only_the_first_retained_task_is_recovered(monkeypatch):
    second = COMMAND.model_copy(update={"tool_call_id": "another-send"})
    recovery = Recovery(
        monkeypatch, records=[operation(), operation(tool_call_id="another-send")],
        saved={relay_task_id(AGENT, COMMAND): task(),
               relay_task_id(AGENT, second): task(payload={"input": second.model_dump(mode="json")})})
    recovery.run()
    assert len(recovery.retained) == 1 and len(recovery.joined) == 1


@pytest.mark.parametrize("changes", [
    {"agent_name": "another-name"}, {"session_id": "another-session"}, {"source": None}, {"source": "text"},
    {"source": {"type": "another", "name": "fa-native-version-message-v1"}},
    {"source": {"type": "agentserver.task", "name": "another-task"}},
    {"payload": None}, {"payload": "text"}, {"payload": {}}, {"payload": {"input": {"another": "input"}}},
], ids=["agent", "session", "no-source", "text-source", "source-type", "source-name", "no-payload", "text-payload",
        "no-input", "another-input"])
def test_a_task_that_differs_from_the_indexed_operation_is_never_joined(monkeypatch, changes):
    recovery = Recovery(monkeypatch, saved={relay_task_id(AGENT, COMMAND): task(**changes)})
    assert failure(run_recovery(recovery)) == (409, SCOPE_CODE, "the queued send differs from its indexed original operation")
    assert recovery.joined == [] and recovery.retained == []


def test_the_receipts_session_must_be_the_tasks_even_when_the_runtime_agrees(monkeypatch):
    recovery = Recovery(monkeypatch, hosted_session_id="an-earlier-session")
    assert failure(run_recovery(recovery)) == (409, SCOPE_CODE, "the queued send differs from its indexed original operation")


def test_a_task_of_another_runtime_session_is_refused(monkeypatch):
    recovery = Recovery(monkeypatch)
    recovery.runtime.identity.session_id = "another-runtime-session"
    assert failure(run_recovery(recovery)) == (409, SCOPE_CODE, "the queued send differs from its indexed original operation")


def test_a_task_no_longer_active_is_unconfirmed(monkeypatch):
    recovery = Recovery(monkeypatch, run_found=False)
    assert failure(run_recovery(recovery)) == (503, UNKNOWN, "the original queued send result is unconfirmed")
    assert recovery.retained == []


def test_an_acceptance_of_another_recipient_is_refused(monkeypatch):
    other = ACCEPTED.model_copy(update={"recipient_agent_id": "another-peer"})
    recovery = Recovery(monkeypatch, accepted=other)
    assert failure(run_recovery(recovery)) == (409, SCOPE_CODE, "the recovered acceptance names another recipient")
    assert recovery.retained == []


def test_a_busy_refused_acceptance_is_not_retained_and_ends_the_scan(monkeypatch):
    busy = ACCEPTED.model_copy(update={"status": "busy_refused"})
    recovery = Recovery(monkeypatch, accepted=busy, records=[operation(), operation(tool_call_id="another-send")])
    assert recovery.run() is None and recovery.retained == [] and len(recovery.read) == 1


def active_send(monkeypatch, saved, runtime=None):
    client = NativeAgentRelayClient("https://api.invalid", token)
    client.runtime = runtime

    async def provider_get(_identifier):
        return saved

    async def active(identifier):
        return "the-run:" + identifier

    monkeypatch.setattr(native_agent_tools, "get_task_manager",
                        lambda: SimpleNamespace(provider=SimpleNamespace(get=provider_get)))
    client._following = SimpleNamespace(get_active_run=active)
    return client._active_send("task-id", COMMAND)


BOUND = SimpleNamespace(identity=SimpleNamespace(agent_name="sender-name", session_id=SESSION))


def test_an_active_send_joins_its_own_task(monkeypatch):
    assert asyncio.run(active_send(monkeypatch, task(), BOUND)) == "the-run:task-id"
    assert asyncio.run(active_send(monkeypatch, task(agent_name="unbound-may-differ"), None)) == "the-run:task-id"
    waiting = task(payload={"input": _WaitingEnvironment(command=COMMAND).model_dump(mode="json")})
    assert asyncio.run(active_send(monkeypatch, waiting, BOUND)) == "the-run:task-id"


def test_an_active_send_without_a_task_is_none(monkeypatch):
    assert asyncio.run(active_send(monkeypatch, None, BOUND)) is None


@pytest.mark.parametrize("changes", [
    {"agent_name": "another-name"}, {"session_id": "another-session"}, {"source": "text"},
    {"source": {"type": "another", "name": "fa-native-version-message-v1"}},
    {"source": {"type": "agentserver.task", "name": "another-task"}},
    {"payload": "text"}, {"payload": {"input": {"another": "input"}}},
], ids=["agent", "session", "text-source", "source-type", "source-name", "text-payload", "another-input"])
def test_an_active_send_refuses_a_task_of_another_scope_or_input(monkeypatch, changes):
    assert failure(run(active_send(monkeypatch, task(**changes), BOUND))) == (
        409, SCOPE_CODE, "the original delivery task has different scope or input")
