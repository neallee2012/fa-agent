"""Pins NativeAgentRelayGateway's cross-Agent checks and step order, with every collaborator replaced.

The real chain is covered by test_native_agent_api. Here each step records its name, so a moved step fails, and each
refusal is asserted by status, code and message, so another refusal reached first fails the test.
"""

import logging
from types import SimpleNamespace

import pytest

from shared_context import native_agent_api
from shared_context.behavior_native_agent_wire import NativeRelayResult, NativeRelaySend
from shared_context.errors import Problem
from shared_context.native_agent_api import NativeAgentRelayGateway

ROOT_DENIED = "AGENT_MESSAGE_ROOT_DENIED"
REPLY_DENIED = "AGENT_MESSAGE_REPLY_DENIED"
NO_RESERVATION = (403, ROOT_DENIED, "the inherited root has no original relay reservation")
RECEIVED_DIFFERS = (403, ROOT_DENIED, "the received request differs from its original reservation")
WAKE_DIFFERS = (403, ROOT_DENIED, "the wake must retain its original request and result")
UNAVAILABLE = "the recipient cannot currently receive; this attempt added no delivery; prior outcomes remain unconfirmed"
ADDRESS = SimpleNamespace(agent_id="peer-agent", conversation_id="peer-cid", request_id="peer-rid")
ROOT = SimpleNamespace(agent_id="root-agent")


def refused(expected, call, *args, **kwargs):
    with pytest.raises(Problem) as caught:
        call(*args, **kwargs)
    error = caught.value.body["error"]
    assert (caught.value.status, error["code"], error["message"]) == expected
    return caught.value


def source(kind="agent_result", **changes):
    values = dict(kind=kind, message_id="request-message", in_reply_to="replied-message", root_id="root-id", root=ROOT,
                  sender=ADDRESS, continuation=None, result="the-result", outcome="the-outcome")
    return SimpleNamespace(**{**values, **changes})


def receipt(**changes):
    values = dict(continuation=None, source=source(), agent_id="this-agent", conversation_id="this-cid",
                  request_id="this-rid")
    return SimpleNamespace(**{**values, **changes})


class Wake:
    """A valid wake for _original_source; a test breaks one field and expects the one refusal."""

    def __init__(self, monkeypatch, kind="agent_result"):
        self.calls = []
        self.gateway = NativeAgentRelayGateway(SimpleNamespace(), enabled=True)
        self.source = source(kind)
        self.receipt = receipt(source=self.source)
        self.reservation = SimpleNamespace(message_id="replied-message", payload_sha256="request-digest")
        self.result = SimpleNamespace(
            conversation_id="peer-cid", status="completed", delegation=None, result="the-result", outcome="the-outcome")
        self.request = SimpleNamespace(source=SimpleNamespace(
            message_id="replied-message", root_id="root-id", root=ROOT,
            sender=SimpleNamespace(agent_id="this-agent", conversation_id="this-cid")))
        wake = self

        def quota(principal):
            wake.calls.append("quota")
            return SimpleNamespace(read=lambda root_id: SimpleNamespace(
                messages=(SimpleNamespace(message_id="unrelated", payload_sha256="x"), wake.reservation)))

        def recipient(principal, agent_id):
            wake.calls.append(("recipient", agent_id))
            return "peer-scope"

        def client(scope):
            wake.calls.append(("client", scope))
            return "peer-composition", "peer-client"

        def original(client, rid):
            wake.calls.append(("original", client, rid))
            return wake.result

        def reply_request(result, client):
            wake.calls.append(("reply_request", client))
            return wake.request

        self.gateway._quota, self.gateway._recipient, self.gateway._client = quota, recipient, client
        self.gateway._original, self.gateway._reply_request = original, reply_request
        monkeypatch.setattr(native_agent_api, "message_material_digest",
                            lambda value: "request-digest" if value in (self.request, self.receipt) else "another")
        monkeypatch.setattr(native_agent_api, "outcome_reference", lambda value: value.outcome)

    def run(self):
        return self.gateway._original_source("principal", self.receipt)


def test_a_turn_without_a_source_needs_no_reservation(monkeypatch):
    wake = Wake(monkeypatch)
    wake.receipt.source = None
    assert wake.run() is None and wake.calls == []


@pytest.mark.parametrize("kind", ["agent_result", "agent_notice"])
def test_a_wake_is_checked_against_its_peer_in_order(monkeypatch, kind):
    wake = Wake(monkeypatch, kind)
    assert wake.run() is None
    assert wake.calls == ["quota", ("recipient", "peer-agent"), ("client", "peer-scope"),
                          ("original", "peer-client", "peer-rid"), ("reply_request", "peer-client")]


def test_a_request_is_checked_against_its_reservation_only(monkeypatch):
    wake = Wake(monkeypatch)
    wake.source.kind = "agent_request"
    wake.reservation.message_id = "request-message"
    assert wake.run() is None and wake.calls == ["quota"]


def test_a_request_is_found_by_its_own_message_id_and_a_wake_by_the_message_it_answers(monkeypatch):
    wake = Wake(monkeypatch)
    wake.source.kind = "agent_request"
    refused(NO_RESERVATION, wake.run)
    wake = Wake(monkeypatch)
    wake.reservation.message_id = "request-message"
    refused(NO_RESERVATION, wake.run)


def test_a_request_that_differs_from_its_reservation_is_refused(monkeypatch):
    wake = Wake(monkeypatch)
    wake.source.kind = "agent_request"
    wake.reservation.message_id = "request-message"
    wake.reservation.payload_sha256 = "another-digest"
    refused(RECEIVED_DIFFERS, wake.run)


def test_a_wake_whose_peer_turn_is_gone_is_refused_without_walking_it(monkeypatch):
    wake = Wake(monkeypatch)
    wake.result = None
    refused(WAKE_DIFFERS, wake.run)
    assert not any(call[0] == "reply_request" for call in wake.calls)


def test_a_wake_without_a_peer_request_is_refused(monkeypatch):
    wake = Wake(monkeypatch)
    wake.request = None
    refused(WAKE_DIFFERS, wake.run)


@pytest.mark.parametrize("break_it", [
    lambda w: setattr(w.result, "conversation_id", "another-cid"),
    lambda w: setattr(w.request.source, "message_id", "another-message"),
    lambda w: setattr(w.reservation, "payload_sha256", "another-digest"),
    lambda w: setattr(w.request.source.sender, "agent_id", "another-agent"),
    lambda w: setattr(w.request.source.sender, "conversation_id", "another-cid"),
    lambda w: setattr(w.source, "continuation", "another-address"),
    lambda w: setattr(w.request.source, "root_id", "another-root"),
    lambda w: setattr(w.request.source, "root", "another-root"),
], ids=["peer-cid", "message", "digest", "sender-agent", "sender-cid", "continuation", "root-id", "root"])
@pytest.mark.parametrize("kind", ["agent_result", "agent_notice"])
def test_a_wake_that_differs_from_its_request_is_refused(monkeypatch, kind, break_it):
    wake = Wake(monkeypatch, kind)
    break_it(wake)
    refused(WAKE_DIFFERS, wake.run)


def test_a_continuation_matching_the_request_sender_passes(monkeypatch):
    wake = Wake(monkeypatch)
    wake.source.continuation = wake.request.source.sender
    assert wake.run() is None


@pytest.mark.parametrize("break_it", [
    lambda w: setattr(w.result, "status", "unknown"),
    lambda w: setattr(w.result, "delegation", "handed-on"),
    lambda w: setattr(w.result, "result", "another-result"),
], ids=["status", "delegation", "result"])
def test_a_result_wake_needs_the_final_original_result(monkeypatch, break_it):
    wake = Wake(monkeypatch)
    break_it(wake)
    refused(WAKE_DIFFERS, wake.run)
    notice = Wake(monkeypatch, "agent_notice")
    break_it(notice)
    assert notice.run() is None, "a notice does not read the result fields"


def test_a_notice_needs_the_original_outcome_and_a_result_does_not_read_it(monkeypatch):
    notice = Wake(monkeypatch, "agent_notice")
    notice.result.outcome = "another-outcome"
    refused(WAKE_DIFFERS, notice.run)
    result = Wake(monkeypatch)
    result.result.outcome = "another-outcome"
    assert result.run() is None


def test_a_continued_turn_is_checked_from_its_recovery_source_under_the_senders_client(monkeypatch):
    wake = Wake(monkeypatch)
    recovered = receipt(source=None)
    seen = []
    wake.gateway._sender_scope = lambda principal: seen.append(("scope", principal)) or "sender-scope"
    wake.gateway._client = lambda scope: seen.append(("client", scope)) or ("composition", "sender-client")
    wake.gateway._recovery_source = lambda value, client: seen.append(("recover", value, client)) or recovered
    wake.receipt.continuation = "a-link"
    assert wake.run() is None
    assert seen == [("scope", "principal"), ("client", "sender-scope"), ("recover", wake.receipt, "sender-client")]


def test_a_continued_turn_reuses_a_client_it_was_given(monkeypatch):
    wake = Wake(monkeypatch)
    recovered = receipt(source=None)
    wake.gateway._sender_scope = lambda _principal: pytest.fail("the caller already resolved the sender")
    wake.gateway._recovery_source = lambda value, client: recovered if client == "given-client" else pytest.fail(client)
    wake.receipt.continuation = "a-link"
    assert wake.gateway._original_source("principal", wake.receipt, client="given-client") is None


def sender(**changes):
    values = dict(
        conversation_id="this-cid", request_id="this-rid", delegation=None, queued_relay=None, status="completed",
        result="the-result", continuation_available=False)
    return SimpleNamespace(**{**values, **changes})


class Outcome:
    """Every collaborator of _returned, recording its name; a test changes one return value."""

    def __init__(self, monkeypatch, *, completed=True):
        self.steps = []
        self.completed = completed
        self.command = NativeRelayResult(conversation_id="this-cid", request_id="this-rid")
        self.sender = sender() if completed else sender(status="unknown", result=None)
        self.request = SimpleNamespace(source=SimpleNamespace(
            root_id="root-id", message_id="request-message",
            sender=SimpleNamespace(agent_id="asker-agent", conversation_id="asker-cid")))
        self.reservation = SimpleNamespace(message_id="request-message", payload_sha256="request-digest")
        self.original = None
        self.page = SimpleNamespace(result="the-result", complete=True, text="the full text")
        self.pending = False
        self.submit_error = None
        step = self.step
        self.scope = SimpleNamespace(target=SimpleNamespace(agent_id="this-agent"))
        self.sender_composition = SimpleNamespace(runtime=SimpleNamespace(work_policy="sender-policy"))
        self.target = SimpleNamespace(
            runtime=SimpleNamespace(work_policy="target-policy"), assistant=SimpleNamespace(authority="target-authority"))
        self.recipient = SimpleNamespace(team_id="team-one")
        self.gateway = gateway = NativeAgentRelayGateway(SimpleNamespace(), enabled=True)
        gateway._sender_scope = lambda principal: step("sender_scope") or self.scope
        gateway._client = self.client
        gateway._original = self.read_original
        gateway._continuation_pending = lambda value, client: step("continuation_pending", client) or self.pending
        gateway._reply_request = lambda value, client: step("reply_request", client) or self.request
        gateway._quota = lambda principal: step("quota") or SimpleNamespace(read=lambda root: SimpleNamespace(
            messages=(SimpleNamespace(message_id="unrelated", payload_sha256="x"), self.reservation)))
        gateway._recipient = lambda principal, agent_id: step("recipient", agent_id) or self.recipient
        gateway._accepted = lambda received, *, source=None: ("accepted", received, source)
        monkeypatch.setattr(native_agent_api, "message_material_digest",
                            lambda value: "request-digest" if value is self.request else "another")
        monkeypatch.setattr(native_agent_api, "NativeTurnGateway", SimpleNamespace(
            _legacy=lambda control, cid: step("legacy", control, cid),
            _invoke=self.invoke,
            _same_session=lambda client, value: step("same_session", client),
            _accounted=lambda control, submitted, composition: step("accounted", control, submitted[0])))
        monkeypatch.setattr(native_agent_api, "native_sessions", lambda composition: SimpleNamespace(
            admission=lambda client, *, request_id, resolved: step("admission", client, request_id, resolved) or "admitted-client"))
        monkeypatch.setattr(native_agent_api, "resolve_agent_capture", self.capture)
        monkeypatch.setattr(native_agent_api, "prepare_result", self.prepare_result)
        monkeypatch.setattr(native_agent_api, "prepare_outcome", self.prepare_outcome)
        monkeypatch.setattr(native_agent_api, "submit_native_turn", self.submit)
        monkeypatch.setattr(native_agent_api, "follow_older_image", lambda *args: step("follow_older_image", args[0], args[2]))

    def step(self, name, *details):
        self.steps.append((name, *details))

    def names(self):
        return [item[0] for item in self.steps]

    def client(self, scope, *, dispatch=False):
        self.step("client", "target" if scope is self.recipient else "sender", dispatch)
        return (self.target, "target-client") if scope is self.recipient else (self.sender_composition, "sender-client")

    def read_original(self, client, rid):
        self.step("original", client, rid)
        return self.sender if client == "sender-client" else self.original

    def invoke(self, client, query):
        self.step("invoke", client, type(query).__name__)
        if type(query).__name__ == "NativeConversationRead":
            return (SimpleNamespace(capture=SimpleNamespace(model=SimpleNamespace(id="the-model"))), None)
        return (self.page, None)

    def capture(self, control, authority, principal, recipient, command, *, conversation):
        self.step("capture", control, authority, command.request_id, command.conversation_id, command.model, command.content)
        return "fresh-capture"

    def prepare_result(self, principal, scope, value, recipient, capture, result, text, *, request):
        self.step("prepare_result", capture, result, text)
        return SimpleNamespace(command="result-command", capture=capture, source="result-source")

    def prepare_outcome(self, principal, scope, value, recipient, capture, *, request):
        self.step("prepare_outcome", capture)
        return SimpleNamespace(command="outcome-command", capture=capture, source="outcome-source")

    def submit(self, target, client, command, capture, *, source, original, conversation, before_send):
        self.step("submit", client, command, capture, source, original, type(conversation).__name__)
        self.before_send = before_send
        if self.submit_error is not None:
            raise self.submit_error
        return "the-receipt", None

    def run(self, *, principal=None):
        return self.gateway._returned(principal or self.principal, self.command, completed=self.completed)

    principal = SimpleNamespace(require=lambda team, action: None)


FIRST_GATE = (403, REPLY_DENIED, "the original final peer outcome is required")


@pytest.mark.parametrize("completed", [True, False], ids=["result", "outcome"])
def test_the_steps_of_a_first_return_stay_in_order(monkeypatch, completed):
    case = Outcome(monkeypatch, completed=completed)
    assert case.run() == ("accepted", "the-receipt", case.sender)
    expected = ["sender_scope", "client", "legacy", "original"]
    expected += [] if completed else ["continuation_pending"]
    expected += ["reply_request", "quota", "recipient", "client", "legacy", "original", "admission", "invoke",
                 "same_session", "capture"]
    expected += ["invoke", "prepare_result"] if completed else ["prepare_outcome"]
    expected += ["submit", "accounted"]
    assert case.names() == expected
    assert ("client", "sender", False) in case.steps and ("client", "target", True) in case.steps
    assert ("legacy", "sender-policy", "this-cid") in case.steps and ("legacy", "target-policy", "asker-cid") in case.steps
    assert ("original", "target-client", "request-message") in case.steps
    assert ("admission", "target-client", "request-message", True) in case.steps
    assert ("same_session", "admitted-client") in case.steps


def test_a_first_return_captures_for_the_askers_conversation_with_a_fixed_notice(monkeypatch):
    case = Outcome(monkeypatch)
    case.run()
    assert ("capture", "target-policy", "target-authority", "request-message", "asker-cid", "the-model",
            "Original peer result notification.") in case.steps
    assert ("submit", "admitted-client", "result-command", "fresh-capture", "result-source", None,
            "SimpleNamespace") in case.steps


def test_a_resend_keeps_the_original_capture_without_admitting_again(monkeypatch):
    case = Outcome(monkeypatch)
    case.original = SimpleNamespace(capture="original-capture")
    assert case.run() == ("accepted", "the-receipt", case.sender)
    names = case.names()
    assert "admission" not in names and "capture" not in names and "same_session" not in names
    assert ("submit", "target-client", "result-command", "original-capture", "result-source", case.original,
            "NoneType") in case.steps


def test_an_incomplete_page_sends_no_inline_text(monkeypatch):
    case = Outcome(monkeypatch)
    case.page.complete = False
    case.run()
    assert ("prepare_result", "fresh-capture", "the-result", None) in case.steps


def test_a_complete_page_sends_its_text(monkeypatch):
    case = Outcome(monkeypatch)
    case.run()
    assert ("prepare_result", "fresh-capture", "the-result", "the full text") in case.steps


def test_a_result_page_of_another_result_is_refused_before_anything_is_sent(monkeypatch):
    case = Outcome(monkeypatch)
    case.page.result = "another-result"
    refused((409, "NATIVE_RESULT_INCOMPLETE", "the result page differs from the completed original turn"), case.run)
    assert "submit" not in case.names() and "prepare_result" not in case.names()


@pytest.mark.parametrize("completed,changes", [
    (True, {"conversation_id": "another-cid"}), (True, {"delegation": "handed-on"}), (True, {"queued_relay": "queued"}),
    (True, {"status": "unknown"}), (True, {"result": None}),
    (False, {"conversation_id": "another-cid"}), (False, {"delegation": "handed-on"}), (False, {"queued_relay": "queued"}),
    (False, {"status": "completed"}), (False, {"status": "started"}),
], ids=["result-cid", "result-delegated", "result-queued", "result-status", "result-empty", "outcome-cid",
        "outcome-delegated", "outcome-queued", "outcome-completed", "outcome-started"])
def test_only_the_original_final_turn_returns(monkeypatch, completed, changes):
    case = Outcome(monkeypatch, completed=completed)
    vars(case.sender).update(changes)
    refused(FIRST_GATE, case.run)
    assert case.names() == ["sender_scope", "client", "legacy", "original"]


def test_a_missing_sender_turn_is_refused(monkeypatch):
    case = Outcome(monkeypatch)
    case.sender = None
    refused(FIRST_GATE, case.run)


@pytest.mark.parametrize("status", ["unknown", "busy_refused"])
def test_both_unconfirmed_statuses_return_an_outcome(monkeypatch, status):
    case = Outcome(monkeypatch, completed=False)
    case.sender.status = status
    assert case.run() == ("accepted", "the-receipt", case.sender)


def test_a_result_never_asks_about_a_continuation(monkeypatch):
    case = Outcome(monkeypatch)
    case.pending = True
    case.run()
    assert "continuation_pending" not in case.names()


def test_an_outcome_waits_for_its_bounded_continuation(monkeypatch):
    case = Outcome(monkeypatch, completed=False)
    case.pending = True
    refused((409, "NATIVE_CONTINUATION_PENDING", "the original result awaits its bounded continuation"), case.run)
    assert case.names()[-1] == "continuation_pending"


def test_an_outcome_without_an_upstream_request_is_refused(monkeypatch):
    case = Outcome(monkeypatch)
    case.request = None
    refused((403, REPLY_DENIED, "this outcome has no upstream peer request"), case.run)
    assert "quota" not in case.names()


def test_an_outcome_of_a_request_never_relayed_is_refused(monkeypatch):
    case = Outcome(monkeypatch)
    case.reservation.message_id = "another-message"
    refused((403, ROOT_DENIED, "the result must belong to the original relayed request"), case.run)
    assert "recipient" not in case.names()


def test_an_outcome_of_a_different_request_is_refused(monkeypatch):
    case = Outcome(monkeypatch)
    case.reservation.payload_sha256 = "another-digest"
    refused((403, ROOT_DENIED, "the result must belong to the original relayed request"), case.run)
    assert "recipient" not in case.names()


def test_the_group_publication_is_off_by_default(monkeypatch):
    monkeypatch.delenv("FA_NATIVE_GROUPS_ENABLED", raising=False)
    case = Outcome(monkeypatch)
    monkeypatch.setattr("shared_context.native_group_api.NativeGroupGateway", lambda *_, **__: pytest.fail("groups are off"))
    case.run()


def group_gateway(monkeypatch, case, error=None):
    class Gateway:
        def __init__(self, owner, *, enabled, repository):
            case.step("group_gateway", enabled, repository)

        def complete(self, principal, command):
            case.step("group_complete", command)
            if error is not None:
                raise error

    monkeypatch.setenv("FA_NATIVE_GROUPS_ENABLED", "1")
    monkeypatch.setattr("shared_context.native_group_api.NativeGroupGateway", Gateway)


def test_a_group_publishes_before_the_recipient_is_resolved(monkeypatch):
    case = Outcome(monkeypatch)
    group_gateway(monkeypatch, case)
    case.gateway.repository = "the-repository"
    case.run()
    names = case.names()
    assert names.index("quota") < names.index("group_complete") < names.index("recipient")
    assert ("group_gateway", True, "the-repository") in case.steps and ("group_complete", case.command) in case.steps


def test_a_failed_group_publication_is_logged_and_the_outcome_still_returns(monkeypatch, caplog):
    case = Outcome(monkeypatch)
    group_gateway(monkeypatch, case, Problem(503, "NATIVE_GROUP_SYNTHETIC", "synthetic"))
    with caplog.at_level(logging.WARNING, logger=native_agent_api.__name__):
        assert case.run() == ("accepted", "the-receipt", case.sender)
    assert "NATIVE_GROUP_PUBLICATION_PENDING code=NATIVE_GROUP_SYNTHETIC" in caplog.text


def test_a_refused_submit_follows_an_older_image_and_is_not_accounted(monkeypatch):
    case = Outcome(monkeypatch)
    case.submit_error = Problem(409, "NATIVE_SESSION_CHANGED", "synthetic")
    with pytest.raises(Problem) as caught:
        case.run()
    assert caught.value is case.submit_error
    assert ("follow_older_image", case.target, "admitted-client") in case.steps and "accounted" not in case.names()


def test_the_submit_checks_the_recipients_team_right_before_sending(monkeypatch):
    case = Outcome(monkeypatch)
    calls = []
    principal = SimpleNamespace(require=lambda team, action: calls.append((team, action)))
    case.run(principal=principal)
    assert calls == []
    case.before_send()
    assert calls == [("team-one", "submit")]


COMMAND = NativeRelaySend(
    conversation_id="this-cid", request_id="this-rid", tool_call_id="original-send",
    message={"recipient_agent_id": "peer-agent", "kind": "request", "content": "Original public brief."})


class Send:
    def __init__(self, monkeypatch):
        self.steps = []
        self.sender = SimpleNamespace(queued_relay=None, delegation=None)
        self.original = None
        self.recipient_error = self.client_error = self.submit_error = self.conflict = None
        self.queued_matches = True
        self.reservation = SimpleNamespace(message_id="the-message", payload_sha256="the-digest")
        self.peer = SimpleNamespace(
            runtime=SimpleNamespace(work_policy="peer-policy"), assistant=SimpleNamespace(authority="peer-authority"))
        self.recipient = SimpleNamespace(team_id="team-one")
        self.principal = SimpleNamespace(tenant_id="tenant-one", team="team-one", require=lambda *_: None)
        self.message = SimpleNamespace(command="message-command", capture="message-capture", source=SimpleNamespace(root_id="root-id"))
        self.scope = "sender-scope"
        step = self.step
        self.gateway = gateway = NativeAgentRelayGateway(SimpleNamespace(), enabled=True)
        gateway._sender_command = lambda principal, command: step("sender_command") or (self.scope, "sender-client", self.sender)
        gateway._recipient = self.resolve
        gateway._client = self.client
        gateway._original = self.read_original
        gateway._quota = lambda principal: step("quota") or SimpleNamespace(
            repository="the-repository", reserve_message=self.reserve,
            read=lambda root: SimpleNamespace(messages=(SimpleNamespace(message_id="unrelated", payload_sha256="x"), self.reservation)))
        gateway._accepted = lambda value, **_: ("accepted", value)
        monkeypatch.delenv("FA_NATIVE_GROUPS_ENABLED", raising=False)
        monkeypatch.setattr(native_agent_api, "message_identifier", lambda principal, sender, call: "the-message")
        monkeypatch.setattr(native_agent_api, "queued_request_matches", lambda sender, command: self.queued_matches)
        monkeypatch.setattr(native_agent_api, "relay_acceptance_scope", lambda value: value)
        monkeypatch.setattr(native_agent_api, "native_sessions", lambda peer: SimpleNamespace(
            admission=lambda client, *, request_id, resolved: step("admission", client, request_id, resolved) or "admitted-client"))
        monkeypatch.setattr(native_agent_api, "resolve_agent_capture", self.capture)
        monkeypatch.setattr(native_agent_api, "prepare_request", self.prepare)
        monkeypatch.setattr(native_agent_api, "message_material_digest", lambda value: "the-digest")
        monkeypatch.setattr(native_agent_api, "submit_native_turn", self.submit)
        monkeypatch.setattr(native_agent_api, "follow_older_image", lambda *args: step("follow_older_image", args[0], args[2], args[3]))
        monkeypatch.setattr(native_agent_api, "NativeTurnGateway", SimpleNamespace(
            _accounted=lambda control, submitted, composition: step("accounted", control, submitted[0])))

    def step(self, name, *details):
        self.steps.append((name, *details))

    def names(self):
        return [item[0] for item in self.steps]

    def resolve(self, principal, agent_id):
        self.step("recipient", agent_id)
        if self.recipient_error is not None:
            raise self.recipient_error
        return self.recipient

    def client(self, scope, *, dispatch=False):
        self.step("client", dispatch)
        if self.client_error is not None:
            raise self.client_error
        return self.peer, "peer-client"

    def read_original(self, client, rid):
        self.step("original", client, rid)
        return self.original

    def reserve(self, root_id, identifier, material):
        self.step("reserve", root_id, identifier, material)

    def capture(self, policy, authority, principal, recipient, command):
        self.step("capture", policy, authority, command.request_id, command.content)
        return "fresh-capture"

    def prepare(self, principal, scope, sender, recipient, capture, message, *, tool_call_id, original):
        self.step("prepare", scope, capture, tool_call_id, original)
        return self.message

    def submit(self, peer, client, command, capture, *, source, original, before_send):
        self.step("submit", client, command, capture, original)
        self.before_send = before_send
        if self.conflict is not None:
            conflict, self.conflict = self.conflict, None
            raise conflict
        if self.submit_error is not None:
            raise self.submit_error
        return "the-receipt", None

    def run(self):
        return self.gateway._send(self.principal, COMMAND)


def test_a_new_message_takes_its_steps_in_order(monkeypatch):
    case = Send(monkeypatch)
    assert case.run() == ("accepted", "the-receipt")
    assert case.names() == ["sender_command", "recipient", "client", "original", "admission", "quota", "capture",
                            "prepare", "reserve", "submit", "accounted"]
    assert ("client", True) in case.steps and ("original", "peer-client", "the-message") in case.steps
    assert ("admission", "peer-client", "the-message", True) in case.steps
    assert ("reserve", "root-id", "the-message", "the-digest") in case.steps
    assert ("capture", "peer-policy", "peer-authority", "the-message", "Original public brief.") in case.steps
    assert ("prepare", "sender-scope", "fresh-capture", "original-send", None) in case.steps
    assert ("submit", "admitted-client", "message-command", "message-capture", None) in case.steps
    assert ("accounted", "peer-policy", "the-receipt") in case.steps


def test_the_submit_checks_the_recipients_team_right_before_sending_a_message(monkeypatch):
    case = Send(monkeypatch)
    calls = []
    case.principal.require = lambda team, action: calls.append((team, action))
    case.run()
    assert calls == []
    case.before_send()
    assert calls == [("team-one", "submit")]


def test_a_saved_accepted_message_is_resent_under_its_allowance_without_a_new_reservation(monkeypatch):
    case = Send(monkeypatch)
    case.original = SimpleNamespace(status="accepted", capture="saved-capture")
    assert case.run() == ("accepted", "the-receipt")
    assert "admission" not in case.names() and "capture" not in case.names() and "reserve" not in case.names()
    assert ("prepare", "sender-scope", "saved-capture", "original-send", case.original) in case.steps
    assert ("submit", "peer-client", "message-command", "message-capture", case.original) in case.steps


def test_a_finished_message_only_retries_its_usage_write(monkeypatch):
    case = Send(monkeypatch)
    case.original = SimpleNamespace(status="completed", capture="saved-capture")
    assert case.run() == ("accepted", case.original)
    assert "submit" not in case.names() and ("accounted", "peer-policy", case.original) in case.steps


@pytest.mark.parametrize("change", ["another-message", "another-digest"])
def test_a_resend_needs_the_allowance_of_this_exact_message(monkeypatch, change):
    case = Send(monkeypatch)
    case.original = SimpleNamespace(status="accepted", capture="saved-capture")
    if change == "another-message":
        case.reservation.message_id = "another-message"
    else:
        case.reservation.payload_sha256 = "another-digest"
    refused((403, ROOT_DENIED, "the original relay allowance must name this message"), case.run)
    assert "submit" not in case.names()


def test_a_queued_sender_delivers_only_its_recorded_command(monkeypatch):
    case = Send(monkeypatch)
    case.sender.queued_relay = "queued"
    case.queued_matches = False
    refused((403, "NATIVE_AGENT_SOURCE_INVALID", "only the original queued command may be delivered"), case.run)
    assert "quota" not in case.names()


def test_a_queued_sender_with_its_recorded_command_delivers(monkeypatch):
    case = Send(monkeypatch)
    case.sender.queued_relay = "queued"
    assert case.run() == ("accepted", "the-receipt")


def test_a_sender_that_is_not_queued_has_no_recorded_command_to_match(monkeypatch):
    case = Send(monkeypatch)
    case.queued_matches = False
    assert case.run() == ("accepted", "the-receipt")


def test_a_queued_sender_with_a_saved_message_is_not_compared_again(monkeypatch):
    case = Send(monkeypatch)
    case.sender.queued_relay = "queued"
    case.queued_matches = False
    case.original = SimpleNamespace(status="completed", capture="saved-capture")
    assert case.run() == ("accepted", case.original)


def test_a_retained_acceptance_needs_its_receiver_record(monkeypatch):
    case = Send(monkeypatch)
    case.sender.delegation = "the-acceptance"
    refused((503, "NATIVE_RELAY_OUTCOME_UNKNOWN",
             "the retained acceptance requires its original receiver record; never send another message"), case.run)
    assert "quota" not in case.names()


def test_a_retained_acceptance_must_match_its_receiver_record(monkeypatch):
    case = Send(monkeypatch)
    case.sender.delegation = "the-acceptance"
    case.original = SimpleNamespace(status="accepted", capture="saved-capture")
    refused((503, "NATIVE_RELAY_OUTCOME_UNKNOWN",
             "the retained acceptance requires its original receiver record; never send another message"), case.run)
    case.gateway._accepted = lambda value, **_: "the-acceptance"
    assert case.run() == "the-acceptance"


@pytest.mark.parametrize("where", ["recipient", "client"])
def test_a_recipient_that_changed_session_is_following(monkeypatch, where):
    case = Send(monkeypatch)
    setattr(case, where + "_error", Problem(409, "NATIVE_SESSION_CHANGED", "synthetic"))
    refused((503, "AGENT_VERSION_FOLLOWING", "the recipient needs its published environment; retain the original message"), case.run)


@pytest.mark.parametrize("where", ["recipient", "client"])
@pytest.mark.parametrize("status,code", [
    (409, "AGENT_MESSAGE_RECIPIENT_NOT_READY"), (503, "NATIVE_TURNS_DISABLED"), (403, "NATIVE_EXECUTION_UNAVAILABLE"),
])
def test_a_recipient_that_cannot_receive_keeps_its_status_and_adds_no_delivery(monkeypatch, caplog, where, status, code):
    case = Send(monkeypatch)
    setattr(case, where + "_error", Problem(status, code, "synthetic"))
    with caplog.at_level(logging.WARNING, logger=native_agent_api.__name__):
        refused((status, "NATIVE_RELAY_RECIPIENT_UNAVAILABLE", UNAVAILABLE), case.run)
    assert f"NATIVE_RELAY_RECIPIENT_UNAVAILABLE cause={code}" in caplog.text
    assert "quota" not in case.names()


def test_another_recipient_failure_escapes_as_it_is(monkeypatch):
    case = Send(monkeypatch)
    case.recipient_error = Problem(403, "NATIVE_STATE_SCOPE", "synthetic")
    with pytest.raises(Problem) as caught:
        case.run()
    assert caught.value is case.recipient_error


def test_an_earlier_runtime_not_yet_gone_has_the_sender_resend(monkeypatch, caplog):
    case = Send(monkeypatch)
    error = Problem(503, "NATIVE_EXECUTION_UNAVAILABLE", "synthetic")
    error.body["error"]["not_dispatched"] = True
    case.submit_error = error
    with caplog.at_level(logging.WARNING, logger=native_agent_api.__name__):
        refused((503, "NATIVE_RELAY_RECIPIENT_UNAVAILABLE", "the recipient cannot currently receive; this attempt added no delivery"), case.run)
    assert "NATIVE_RELAY_RECIPIENT_UNAVAILABLE cause=NATIVE_EXECUTION_UNAVAILABLE" in caplog.text
    assert "follow_older_image" not in case.names()


@pytest.mark.parametrize("flag", [False, None], ids=["dispatched", "unflagged"])
def test_an_execution_failure_that_may_have_dispatched_follows_and_escapes(monkeypatch, flag):
    case = Send(monkeypatch)
    error = Problem(503, "NATIVE_EXECUTION_UNAVAILABLE", "synthetic")
    if flag is not None:
        error.body["error"]["not_dispatched"] = flag
    case.submit_error = error
    with pytest.raises(Problem) as caught:
        case.run()
    assert caught.value is error
    assert ("follow_older_image", case.peer, "admitted-client", error) in case.steps
    assert "accounted" not in case.names()


def test_only_an_unavailable_execution_can_mean_not_dispatched(monkeypatch):
    case = Send(monkeypatch)
    error = Problem(503, "NATIVE_TURN_UNKNOWN", "synthetic")
    error.body["error"]["not_dispatched"] = True
    case.submit_error = error
    with pytest.raises(Problem) as caught:
        case.run()
    assert caught.value is error


def conflict():
    return Problem(409, "NATIVE_RID_CONFLICT", "synthetic")


def test_a_lost_race_retries_once_with_the_saved_message(monkeypatch):
    case = Send(monkeypatch)
    saved = SimpleNamespace(status="accepted", capture="saved-capture")
    case.conflict = conflict()
    reads = iter((None, saved))
    case.gateway._original = lambda client, rid: next(reads)
    assert case.run() == ("accepted", "the-receipt")
    assert case.names().count("submit") == 2 and case.names().count("reserve") == 1
    assert ("submit", "admitted-client", "message-command", "message-capture", saved) in case.steps
    assert ("prepare", "sender-scope", "saved-capture", "original-send", saved) in case.steps


def test_a_conflict_with_nothing_saved_escapes(monkeypatch):
    case = Send(monkeypatch)
    case.conflict = error = conflict()
    with pytest.raises(Problem) as caught:
        case.run()
    assert caught.value is error and case.names().count("submit") == 1


def test_a_conflict_on_a_saved_message_escapes(monkeypatch):
    case = Send(monkeypatch)
    case.original = SimpleNamespace(status="accepted", capture="saved-capture")
    case.conflict = error = conflict()
    with pytest.raises(Problem) as caught:
        case.run()
    assert caught.value is error and case.names().count("submit") == 1


@pytest.mark.parametrize("status,code", [(409, "NATIVE_SESSION_CHANGED"), (403, "NATIVE_RID_CONFLICT")])
def test_only_a_409_rid_conflict_retries(monkeypatch, status, code):
    case = Send(monkeypatch)
    case.submit_error = Problem(status, code, "synthetic")
    reads = iter((None, SimpleNamespace(status="accepted", capture="saved-capture")))
    case.gateway._original = lambda client, rid: next(reads)
    with pytest.raises(Problem):
        case.run()
    assert case.names().count("submit") == 1


class Groups:
    def __init__(self, case, repository, *, tenant_id, team_id):
        case.step("groups", repository, tenant_id, team_id)
        self.case = case

    def relay_request(self, message):
        self.case.step("relay_request")

    def refused_request(self, message, code):
        self.case.step("refused_request", code)


def groups_on(monkeypatch, case):
    monkeypatch.setenv("FA_NATIVE_GROUPS_ENABLED", "1")
    monkeypatch.setattr("shared_context.native_groups.NativeGroupStore", lambda *args, **options: Groups(case, *args, **options))


def test_a_group_message_is_recorded_after_the_reservation_and_before_the_submit(monkeypatch):
    case = Send(monkeypatch)
    groups_on(monkeypatch, case)
    case.run()
    names = case.names()
    assert names.index("reserve") < names.index("groups") < names.index("relay_request") < names.index("submit")
    assert ("groups", "the-repository", "tenant-one", "team-one") in case.steps


def test_a_finished_message_touches_no_group(monkeypatch):
    case = Send(monkeypatch)
    groups_on(monkeypatch, case)
    case.original = SimpleNamespace(status="completed", capture="saved-capture")
    case.run()
    assert "groups" not in case.names()


@pytest.mark.parametrize("status,code,flag", [
    (409, "NATIVE_TURNS_DISABLED", None), (409, "NATIVE_SESSION_CHANGED", None), (403, "NATIVE_TURNS_DISABLED", False),
], ids=["lane-off", "session", "member-refused"])
def test_a_member_that_refused_before_any_receipt_is_recorded_on_the_group(monkeypatch, status, code, flag):
    case = Send(monkeypatch)
    groups_on(monkeypatch, case)
    error = Problem(status, code, "synthetic")
    if flag is not None:
        error.body["error"]["automatic_retry"] = flag
    case.submit_error = error
    with pytest.raises(Problem):
        case.run()
    assert ("refused_request", code) in case.steps
    assert ("follow_older_image", case.peer, "admitted-client", error) in case.steps
    assert case.names().index("refused_request") < case.names().index("follow_older_image")


@pytest.mark.parametrize("status,code,flag,saved_after", [
    (403, "NATIVE_TURNS_DISABLED", None, False),
    (403, "NATIVE_TURNS_DISABLED", True, False),
    (403, "NATIVE_SESSION_CHANGED", False, False),
    (500, "NATIVE_TURNS_DISABLED", False, False),
    (409, "ANOTHER_REFUSAL", None, False),
    (409, "NATIVE_TURNS_DISABLED", None, True),
], ids=["platform-403", "retryable-403", "403-session", "500", "another", "receipt-exists"])
def test_any_other_failure_is_not_recorded_as_a_member_refusal(monkeypatch, status, code, flag, saved_after):
    case = Send(monkeypatch)
    groups_on(monkeypatch, case)
    error = Problem(status, code, "synthetic")
    if flag is not None:
        error.body["error"]["automatic_retry"] = flag
    case.submit_error = error
    reads = iter((None, SimpleNamespace(status="accepted", capture="x") if saved_after else None))
    case.gateway._original = lambda client, rid: next(reads)
    with pytest.raises(Problem):
        case.run()
    assert "refused_request" not in case.names()


def test_a_retry_of_a_saved_message_is_not_a_member_refusal(monkeypatch):
    case = Send(monkeypatch)
    groups_on(monkeypatch, case)
    reads = iter((SimpleNamespace(status="accepted", capture="saved-capture"), None))
    case.gateway._original = lambda client, rid: next(reads)
    case.submit_error = Problem(409, "NATIVE_TURNS_DISABLED", "synthetic")
    with pytest.raises(Problem):
        case.run()
    assert "refused_request" not in case.names()
