"""The original SDK relay task, called the way the SDK calls it: order of checks, reads and effects stays recoverable."""

import asyncio
from types import SimpleNamespace

import pytest
from azure.ai.agentserver.core.tasks import TaskContext
from pydantic import ValidationError

from shared_context import native_relay_delivery
from shared_context.behavior_native_agent_wire import NativePeerRequest, NativeRelayAcceptance, NativeRelaySend
from shared_context.errors import Problem
from shared_context.native_agent_tools import (
    NativeAgentRelayClient, NativeRecipientFollowing, NativeRecipientNotReady, NativeResponseFollowFailure,
    _AcceptedFollow, _WaitingEnvironment,
)
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE, NativeResponseDispatch

COMMAND = NativeRelaySend(conversation_id="cid", request_id="rid", tool_call_id="call", message=NativePeerRequest(
    recipient_agent_id="peer", kind="request", content="Original work."))
ACCEPTED = NativeRelayAcceptance(
    message_id="message", recipient_agent_id="peer", conversation_id="peer-cid", submit_digest="a" * 64, status="accepted")
RESPONSE = NativeRelayAcceptance(
    message_id="message", recipient_agent_id="peer", conversation_id="peer-cid", submit_digest="a" * 64, status="accepted",
    state_profile=NATIVE_RESPONSES_PROFILE, dispatch=NativeResponseDispatch(state="already_queued", inflight_request_id="message"))
OTHER_MESSAGE = ACCEPTED.model_copy(update={"message_id": "another"})
OTHER_RECIPIENT = ACCEPTED.model_copy(update={"recipient_agent_id": "someone"})
SENTINEL = object()


class Probe(NativeAgentRelayClient):
    """Logs every ``self.runtime`` read: Hosted binds it after ``__init__``, so each read is its own observation."""

    @property
    def runtime(self):
        self.log.append("runtime")
        return self._bound

    @runtime.setter
    def runtime(self, value):
        self._bound = value


class Flag:
    def __init__(self, name, log, fires):
        self.name, self.log, self.fires, self.calls, self.fired = name, log, fires, 0, False

    def is_set(self):
        self.calls += 1
        self.log.append(self.name)
        self.fired = self.fired or self.calls in self.fires
        return self.fired


class Scene:
    def __init__(self, monkeypatch, *, bound=True, shutdown=(), cancel=(), attempt=0, expired=False,
                 sends=(ACCEPTED,), before=(), sender_cid="cid", delegation=None, rid_cid="cid", saved=True,
                 environment=(True,), waits=(True,), value=COMMAND):
        self.log = log = []
        self.sends, self.before, self.environment, self.waits = list(sends), list(before), list(environment), list(waits)
        Probe.log = log
        self.client = Probe("https://api.invalid", None)
        self.client._directories["cid"] = (0.0, None)
        self.runtime = None
        if bound:
            async def read(request_id):
                log.append(("read", request_id))
                return SimpleNamespace(conversation_id=sender_cid, delegation=delegation)

            async def rid(request_id):
                log.append(("rid", request_id))
                return SimpleNamespace(value=SimpleNamespace(receipt=SimpleNamespace(conversation_id=rid_cid)))

            self.runtime = self.client.runtime = SimpleNamespace(read=read, _rid=rid)
        log.clear()

        async def before_send(runtime, command):
            log.append(("before_send", runtime is self.runtime, command))
            step = self.before.pop(0) if self.before else "attempt"
            if isinstance(step, Exception):
                raise step
            return step

        async def refused(runtime, command, attempt):
            log.append(("refused", runtime is self.runtime, command, attempt))

        async def accepted(runtime, command, receipt):
            log.append(("save", runtime is self.runtime, command, receipt))
            return saved

        async def stopped(runtime, command, *, expired, reason):
            log.append(("stopped", runtime is self.runtime, command, expired, reason, "cid" in self.client._directories))

        monkeypatch.setattr(native_relay_delivery, "before_send", before_send)
        monkeypatch.setattr(native_relay_delivery, "refused_before_delivery", refused)
        monkeypatch.setattr(native_relay_delivery, "accepted", accepted)
        monkeypatch.setattr(native_relay_delivery, "stopped", stopped)

        async def request(suffix, model, command):
            log.append(("request", suffix, model, command))
            step = self.sends.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        async def retain(runtime, original, receipt):
            log.append(("retain", runtime is self.runtime, receipt))

        async def follow_environment(ctx, command):
            log.append(("environment", command))
            step = self.environment.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        async def wait(ctx, attempt):
            log.append(("wait", attempt))
            return self.waits.pop(0)

        async def follow_accepted(ctx, original):
            log.append(("follow_accepted", original))
            return original.accepted

        async def start_follow(route, command, accepted):
            log.append(("start_follow", route, command, accepted))

        self.client._request = request
        self.client._retain_queued_acceptance = retain
        self.client._follow_environment = follow_environment
        self.client._wait = wait
        self.client._follow_accepted = follow_accepted
        self.client._start_response_follow = start_follow

        async def exit_for_recovery():
            log.append("exit")
            return SENTINEL

        self.ctx = SimpleNamespace(
            input=value, retry_attempt=attempt, timeout_exceeded=expired, exit_for_recovery=exit_for_recovery,
            shutdown=Flag("shutdown", log, shutdown), cancel=Flag("cancel", log, cancel))

    def run(self):
        return asyncio.run(self.client._following._fn(self.ctx))

    def effects(self):
        return [entry for entry in self.log if entry not in {"shutdown", "cancel", "runtime"}]


def problem(status, code, message="OFFLINE"):
    return Problem(status, code, message)


def stopped_entry(reason, *, expired=False):
    return ("stopped", True, COMMAND, expired, reason, False)


GOLDEN_SEND = [
    "shutdown", "cancel",
    "shutdown", "cancel", "runtime", "runtime", ("before_send", True, COMMAND),
    "shutdown", "cancel", ("request", "/send", NativeRelayAcceptance, COMMAND),
    "runtime", "runtime", ("rid", "rid"), "runtime", ("save", True, COMMAND, ACCEPTED),
    ("start_follow", "/send", COMMAND, ACCEPTED),
]


def test_the_sdk_contract_of_the_queued_task_stays_exact():
    client = NativeAgentRelayClient("https://api.invalid", None)
    task = client._following
    assert task.name == "fa-native-version-message-v1"
    # The private fields are read on purpose: a .start() run cannot show a changed retry policy or input annotation.
    assert task._opts.retry is client._follow_retry and task._opts.steerable is False
    assert task._input_type == NativeRelaySend | _AcceptedFollow | _WaitingEnvironment
    assert task._output_type is NativeRelayAcceptance
    policy = client._follow_retry
    assert (policy.max_attempts, policy.initial_delay, policy.max_delay) == (10, 1, 60)
    assert policy.retry_on == (NativeRecipientFollowing, type(NativeResponseFollowFailure()))


def test_a_real_task_context_reaches_the_delivery(monkeypatch):
    scene = Scene(monkeypatch)
    context = TaskContext(task_id="task", session_id="session", input_id="task", entry_mode="fresh", input=COMMAND)
    assert asyncio.run(scene.client._following._fn(context)) == ACCEPTED


@pytest.mark.parametrize("api_url", [
    "http://api.invalid", "https://", "https://user@api.invalid", "https://:secret@api.invalid",
    "https://api.invalid?x=1", "https://api.invalid#x", "https://api.invalid/path", "https://api.invalid:8443",
])
def test_only_the_original_https_api_is_accepted(api_url):
    with pytest.raises(Problem) as refused:
        NativeAgentRelayClient(api_url, None)
    assert refused.value.status == 403
    assert refused.value.body["error"]["code"] == "HOSTED_API_CONFIGURATION"
    assert refused.value.body["error"]["message"] == "the original HTTPS behavior API is required"


@pytest.mark.parametrize("api_url", ["https://api.invalid", "https://api.invalid/", "https://api.invalid:443"])
def test_the_original_https_api_keeps_its_unslashed_form(api_url):
    client = NativeAgentRelayClient(api_url, "token", transport="transport")
    assert (client.api_url, client.token_provider, client.transport, client.runtime) == (
        api_url.rstrip("/"), "token", "transport", None)


def test_an_unparsable_port_is_reported_only_after_the_other_url_checks():
    with pytest.raises(ValueError):
        NativeAgentRelayClient("https://api.invalid:bad", None)
    with pytest.raises(Problem):
        NativeAgentRelayClient("http://api.invalid:bad", None)


def test_a_new_send_keeps_its_checks_reads_and_effects_in_order(monkeypatch):
    scene = Scene(monkeypatch)
    assert scene.run() == ACCEPTED
    assert scene.log == GOLDEN_SEND


def test_an_unsaved_acceptance_is_retained_on_the_sender_after_a_new_runtime_read(monkeypatch):
    scene = Scene(monkeypatch, saved=False)
    assert scene.run() == ACCEPTED
    assert scene.log == [*GOLDEN_SEND[:-1], "runtime", ("retain", True, ACCEPTED), GOLDEN_SEND[-1]]


def test_a_busy_refusal_is_never_retained_on_the_sender(monkeypatch):
    busy = ACCEPTED.model_copy(update={"status": "busy_refused"})
    scene = Scene(monkeypatch, saved=False, sends=[busy])
    assert scene.run() == busy
    assert [entry[0] for entry in scene.effects()] == ["before_send", "request", "rid", "save", "start_follow"]


def test_a_saved_busy_refusal_still_starts_only_its_own_follow(monkeypatch):
    busy = ACCEPTED.model_copy(update={"status": "busy_refused"})
    scene = Scene(monkeypatch, sends=[busy])
    assert scene.run() == busy
    assert scene.effects()[-1] == ("start_follow", "/send", COMMAND, busy)


def test_an_unbound_client_sends_without_sender_records(monkeypatch):
    scene = Scene(monkeypatch, bound=False)
    assert scene.run() == ACCEPTED
    assert scene.log == [
        "shutdown", "cancel", "shutdown", "cancel", "runtime", "shutdown", "cancel",
        ("request", "/send", NativeRelayAcceptance, COMMAND), "runtime", ("start_follow", "/send", COMMAND, ACCEPTED)]


@pytest.mark.parametrize("value", [
    COMMAND.model_dump(mode="json"), _WaitingEnvironment(command=COMMAND).model_dump(mode="json"),
], ids=["send", "waiting"])
def test_a_persisted_json_input_resumes_as_its_original_command(monkeypatch, value):
    scene = Scene(monkeypatch, value=value)
    assert scene.run() == ACCEPTED
    assert ("request", "/send", NativeRelayAcceptance, COMMAND) in scene.log


@pytest.mark.parametrize("value", ["model", "json"])
def test_an_accepted_follow_only_follows_and_sends_nothing(monkeypatch, value):
    pending = _AcceptedFollow(route="/send", command=COMMAND, accepted=RESPONSE)
    scene = Scene(monkeypatch, value=pending if value == "model" else pending.model_dump(mode="json"))
    assert scene.run() == RESPONSE
    assert scene.log == ["shutdown", "cancel", ("follow_accepted", pending)]


def test_an_unserializable_input_refuses_in_delivery_then_escapes_from_the_stop_path(monkeypatch):
    scene = Scene(monkeypatch, value=object())
    with pytest.raises(TypeError) as failed:
        scene.run()
    refused = failed.value.__context__
    assert isinstance(refused, Problem) and refused.status == 409
    assert refused.body["error"]["code"] == "NATIVE_RELAY_REFUSED"
    assert refused.body["error"]["message"] == "the original relay task input is invalid"
    assert refused.__cause__ is None and refused.__suppress_context__ is True
    assert scene.log == ["shutdown", "cancel"]


def test_an_unknown_input_shape_escapes_as_the_stop_path_validation_error(monkeypatch):
    scene = Scene(monkeypatch, value={"not": "original"})
    with pytest.raises(ValidationError):
        scene.run()
    assert scene.log == ["shutdown", "cancel"]


def test_a_nan_input_is_refused_by_delivery_and_escapes_as_the_stop_path_validation_error(monkeypatch):
    scene = Scene(monkeypatch, value={"not": float("nan")})
    with pytest.raises(ValidationError):
        scene.run()


def test_shutdown_before_any_work_leaves_the_task_for_recovery(monkeypatch):
    scene = Scene(monkeypatch, shutdown={1}, value={"not": "original"})
    assert scene.run() is SENTINEL
    assert scene.log == ["shutdown", "exit"]


def test_cancel_before_any_work_stops_with_an_unknown_outcome(monkeypatch):
    scene = Scene(monkeypatch, cancel={1})
    with pytest.raises(asyncio.CancelledError):
        scene.run()
    assert scene.log == [
        "shutdown", "cancel", "runtime", "runtime", ("stopped", True, COMMAND, False, "NATIVE_RELAY_OUTCOME_UNKNOWN", False)]


@pytest.mark.parametrize("nth, effects", [(2, []), (3, [("before_send", True, COMMAND)])])
def test_shutdown_inside_the_send_loop_never_posts(monkeypatch, nth, effects):
    scene = Scene(monkeypatch, shutdown={nth})
    assert scene.run() is SENTINEL
    assert scene.effects() == [*effects, "exit"]
    assert scene.log[-2:] == ["shutdown", "exit"]


@pytest.mark.parametrize("nth, effects", [(2, []), (3, [("before_send", True, COMMAND)])])
def test_cancel_inside_the_send_loop_never_posts(monkeypatch, nth, effects):
    scene = Scene(monkeypatch, cancel={nth})
    with pytest.raises(asyncio.CancelledError):
        scene.run()
    assert scene.effects() == [*effects, stopped_entry("NATIVE_RELAY_OUTCOME_UNKNOWN")]


def test_waiting_environment_follows_then_sends_the_same_command(monkeypatch):
    scene = Scene(monkeypatch, value=_WaitingEnvironment(command=COMMAND))
    assert scene.run() == ACCEPTED
    assert scene.log == [
        "shutdown", "cancel", "runtime", "runtime", ("read", "rid"), ("environment", COMMAND), *GOLDEN_SEND[2:]]


def test_waiting_environment_without_a_runtime_only_reads_nothing_and_follows(monkeypatch):
    scene = Scene(monkeypatch, bound=False, value=_WaitingEnvironment(command=COMMAND))
    assert scene.run() == ACCEPTED
    assert scene.log[:4] == ["shutdown", "cancel", "runtime", ("environment", COMMAND)]


def test_waiting_environment_shutdown_during_the_follow_keeps_the_task(monkeypatch):
    scene = Scene(monkeypatch, environment=[False], value=_WaitingEnvironment(command=COMMAND))
    assert scene.run() is SENTINEL
    assert scene.effects() == [("read", "rid"), ("environment", COMMAND), "exit"]


def test_waiting_environment_refuses_another_sender_conversation(monkeypatch):
    scene = Scene(monkeypatch, sender_cid="another", value=_WaitingEnvironment(command=COMMAND), attempt=9)
    with pytest.raises(Problem) as refused:
        scene.run()
    assert refused.value.status == 409
    assert refused.value.body["error"]["code"] == "NATIVE_STATE_SCOPE"
    assert refused.value.body["error"]["message"] == "the queued sender conversation differs"
    assert scene.effects() == [("read", "rid"), stopped_entry("NATIVE_STATE_SCOPE")]


def test_waiting_environment_refuses_a_retained_acceptance_for_another_recipient(monkeypatch):
    scene = Scene(monkeypatch, delegation=OTHER_RECIPIENT, value=_WaitingEnvironment(command=COMMAND))
    with pytest.raises(Problem) as refused:
        scene.run()
    assert refused.value.status == 409
    assert refused.value.body["error"]["message"] == "the retained acceptance names another recipient"
    assert scene.effects() == [("read", "rid"), stopped_entry("NATIVE_STATE_SCOPE")]


def test_the_sender_conversation_is_refused_before_the_retained_recipient(monkeypatch):
    scene = Scene(monkeypatch, sender_cid="another", delegation=OTHER_RECIPIENT, value=_WaitingEnvironment(command=COMMAND))
    with pytest.raises(Problem) as refused:
        scene.run()
    assert refused.value.status == 409
    assert refused.value.body["error"]["code"] == "NATIVE_STATE_SCOPE"
    assert refused.value.body["error"]["message"] == "the queued sender conversation differs"
    assert scene.effects() == [("read", "rid"), stopped_entry("NATIVE_STATE_SCOPE")]


def test_a_retained_acceptance_resends_without_following_the_environment(monkeypatch):
    scene = Scene(monkeypatch, delegation=ACCEPTED, value=_WaitingEnvironment(command=COMMAND))
    assert scene.run() == ACCEPTED
    assert ("environment", COMMAND) not in scene.log


def test_a_retained_acceptance_refuses_a_different_answer_from_the_same_send(monkeypatch):
    scene = Scene(monkeypatch, delegation=ACCEPTED, sends=[OTHER_MESSAGE], value=_WaitingEnvironment(command=COMMAND))
    with pytest.raises(Problem) as refused:
        scene.run()
    assert refused.value.status == 409
    assert refused.value.body["error"]["code"] == "NATIVE_STATE_SCOPE"
    assert refused.value.body["error"]["message"] == "the original send returned a different acceptance"
    assert ("rid", "rid") not in scene.log


def test_a_receipt_for_another_recipient_is_unknown_before_any_save(monkeypatch):
    scene = Scene(monkeypatch, sends=[OTHER_RECIPIENT])
    with pytest.raises(Problem) as refused:
        scene.run()
    assert refused.value.status == 502
    assert refused.value.body["error"]["code"] == "NATIVE_RELAY_OUTCOME_UNKNOWN"
    assert refused.value.body["error"]["message"] == "the receipt belongs to another recipient"
    assert scene.effects() == [
        ("before_send", True, COMMAND), ("request", "/send", NativeRelayAcceptance, COMMAND),
        stopped_entry("NATIVE_RELAY_OUTCOME_UNKNOWN")]


def test_another_recipient_outranks_a_different_retained_acceptance(monkeypatch):
    scene = Scene(monkeypatch, delegation=ACCEPTED, sends=[OTHER_RECIPIENT], value=_WaitingEnvironment(command=COMMAND))
    with pytest.raises(Problem) as refused:
        scene.run()
    assert refused.value.status == 502


def test_a_rebound_sender_conversation_refuses_before_the_acceptance_is_saved(monkeypatch):
    scene = Scene(monkeypatch, rid_cid="another")
    with pytest.raises(Problem) as refused:
        scene.run()
    assert refused.value.status == 409
    assert refused.value.body["error"]["message"] == "the queued sender conversation differs"
    assert not [entry for entry in scene.effects() if entry[0] in {"save", "start_follow"}]


@pytest.mark.parametrize("error", [NativeRecipientFollowing(), NativeRecipientNotReady()], ids=["following", "not-ready"])
def test_a_refused_send_is_marked_before_its_wait_and_retried_with_a_new_attempt(monkeypatch, error):
    scene = Scene(monkeypatch, sends=[error, ACCEPTED], before=["first", "second"])
    assert scene.run() == ACCEPTED
    waiting = ("environment", COMMAND) if isinstance(error, NativeRecipientFollowing) else ("wait", 1)
    assert scene.log[:16] == [
        "shutdown", "cancel", "shutdown", "cancel", "runtime", "runtime", ("before_send", True, COMMAND),
        "shutdown", "cancel", ("request", "/send", NativeRelayAcceptance, COMMAND),
        "runtime", "runtime", ("refused", True, COMMAND, "first"), waiting,
        "shutdown", "cancel"]
    assert scene.log[16:18] == ["runtime", "runtime"]


def test_an_unconfirmed_admission_is_not_marked_refused(monkeypatch):
    scene = Scene(monkeypatch, sends=[NativeRecipientFollowing(admission_unconfirmed=True), ACCEPTED])
    assert scene.run() == ACCEPTED
    assert not [entry for entry in scene.effects() if entry[0] == "refused"]
    assert ("environment", COMMAND) in scene.log


@pytest.mark.parametrize("error", [NativeRecipientFollowing(), NativeRecipientNotReady()], ids=["following", "not-ready"])
def test_a_refusal_from_before_send_has_no_attempt_to_mark(monkeypatch, error):
    scene = Scene(monkeypatch, before=[error])
    assert scene.run() == ACCEPTED
    assert ("refused", True, COMMAND, None) in scene.log


def test_a_later_refusal_from_before_send_never_marks_the_earlier_attempt(monkeypatch):
    scene = Scene(monkeypatch, sends=[NativeRecipientNotReady(), ACCEPTED], before=["first", NativeRecipientNotReady()],
                  waits=[True, True])
    assert scene.run() == ACCEPTED
    assert [entry[3] for entry in scene.effects() if entry[0] == "refused"] == ["first", None]


@pytest.mark.parametrize("error", [NativeRecipientFollowing(), NativeRecipientNotReady()], ids=["following", "not-ready"])
def test_a_refused_send_without_a_runtime_only_waits(monkeypatch, error):
    scene = Scene(monkeypatch, bound=False, sends=[error, ACCEPTED])
    assert scene.run() == ACCEPTED
    assert [entry for entry in scene.effects() if entry[0] == "refused"] == []
    assert sum(entry[0] == "request" for entry in scene.effects()) == 2


@pytest.mark.parametrize("error, kind", [
    (NativeRecipientFollowing(), "environment"), (NativeRecipientNotReady(), "wait")], ids=["following", "not-ready"])
def test_shutdown_during_a_refused_send_keeps_the_task(monkeypatch, error, kind):
    scene = Scene(monkeypatch, sends=[error], environment=[False], waits=[False])
    assert scene.run() is SENTINEL
    assert [entry if entry == "exit" else entry[0] for entry in scene.effects()] == [
        "before_send", "request", "refused", kind, "exit"]


def test_a_retained_acceptance_never_waits_for_a_refused_send(monkeypatch):
    scene = Scene(monkeypatch, delegation=ACCEPTED, sends=[NativeRecipientNotReady()],
                  value=_WaitingEnvironment(command=COMMAND))
    with pytest.raises(NativeResponseFollowFailure) as failed:
        scene.run()
    assert failed.value.__context__ is not None and failed.value.__suppress_context__ is True
    effects = [entry[0] for entry in scene.effects()]
    assert effects == ["read", "before_send", "request", "refused"]


def test_an_environment_failure_keeps_its_own_code_through_the_stop_path(monkeypatch):
    failure = problem(503, "NATIVE_FILES_UNKNOWN")
    scene = Scene(monkeypatch, sends=[NativeRecipientFollowing()], environment=[failure])
    with pytest.raises(Problem) as failed:
        scene.run()
    assert failed.value is failure
    assert scene.effects()[-1] == stopped_entry("NATIVE_FILES_UNKNOWN")


@pytest.mark.parametrize("error", [
    problem(503, "NATIVE_RELAY_OUTCOME_UNKNOWN"), problem(500, "NATIVE_RELAY_OUTCOME_UNKNOWN")], ids=["503", "500"])
def test_an_unknown_post_is_the_retryable_follow_failure(monkeypatch, error):
    scene = Scene(monkeypatch, sends=[error])
    with pytest.raises(NativeResponseFollowFailure) as failed:
        scene.run()
    assert failed.value.__suppress_context__ is True
    assert not [entry for entry in scene.effects() if entry[0] == "stopped"]


@pytest.mark.parametrize("error", [
    problem(409, "NATIVE_RELAY_OUTCOME_UNKNOWN"), problem(499, "NATIVE_RELAY_OUTCOME_UNKNOWN"),
    problem(503, "NATIVE_RELAY_REFUSED"),
], ids=["409-unknown", "499-unknown", "503-other"])
def test_any_other_refused_post_stops_with_its_own_code(monkeypatch, error):
    scene = Scene(monkeypatch, sends=[error])
    with pytest.raises(Problem) as failed:
        scene.run()
    assert failed.value is error
    assert scene.effects()[-1] == stopped_entry(error.body["error"]["code"])


@pytest.mark.parametrize("options, escapes", [
    ({"sends": [NativeRecipientFollowing()], "environment": [NativeRecipientFollowing()]}, NativeRecipientFollowing),
    ({"sends": [problem(503, "NATIVE_RELAY_OUTCOME_UNKNOWN")]}, NativeResponseFollowFailure),
], ids=["following", "unknown"])
@pytest.mark.parametrize("attempt, stops", [(0, False), (8, False), (9, True), (10, True)])
def test_only_the_last_attempt_of_a_retryable_failure_stops_the_message(monkeypatch, options, escapes, attempt, stops):
    scene = Scene(monkeypatch, attempt=attempt, **options)
    with pytest.raises(escapes):
        scene.run()
    assert bool([entry for entry in scene.effects() if entry[0] == "stopped"]) is stops
    assert ("cid" not in scene.client._directories) is stops


def test_an_official_timeout_outranks_the_failure_code(monkeypatch):
    late = Scene(monkeypatch, sends=[problem(409, "NATIVE_RELAY_REFUSED")], expired=True)
    with pytest.raises(Problem):
        late.run()
    assert late.effects()[-1] == stopped_entry("NATIVE_RELAY_WAIT_EXPIRED", expired=True)


def test_a_cancelled_task_at_the_timeout_reports_the_official_expiry(monkeypatch):
    scene = Scene(monkeypatch, cancel={1}, expired=True)
    with pytest.raises(asyncio.CancelledError):
        scene.run()
    assert scene.effects()[-1] == stopped_entry("NATIVE_RELAY_WAIT_EXPIRED", expired=True)


def test_a_stopped_waiting_message_records_its_original_command(monkeypatch):
    scene = Scene(monkeypatch, sends=[problem(409, "NATIVE_RELAY_REFUSED")], value=_WaitingEnvironment(command=COMMAND))
    with pytest.raises(Problem):
        scene.run()
    assert scene.effects()[-1] == stopped_entry("NATIVE_RELAY_REFUSED")


def test_a_stopped_json_input_records_its_original_command(monkeypatch):
    scene = Scene(monkeypatch, sends=[problem(409, "NATIVE_RELAY_REFUSED")], value=COMMAND.model_dump(mode="json"))
    with pytest.raises(Problem):
        scene.run()
    assert scene.effects()[-1] == stopped_entry("NATIVE_RELAY_REFUSED")


def test_a_failed_accepted_follow_has_no_sender_record_to_stop(monkeypatch):
    failure = problem(409, "NATIVE_RELAY_REFUSED")
    pending = _AcceptedFollow(route="/send", command=COMMAND, accepted=RESPONSE)
    scene = Scene(monkeypatch, value=pending)

    async def follow(ctx, original):
        raise failure

    scene.client._follow_accepted = follow
    with pytest.raises(Problem) as failed:
        scene.run()
    assert failed.value is failure
    assert scene.log == ["shutdown", "cancel", "runtime"]
    assert "cid" in scene.client._directories


def test_an_unbound_client_stops_without_touching_the_directory(monkeypatch):
    scene = Scene(monkeypatch, bound=False, sends=[problem(409, "NATIVE_RELAY_REFUSED")])
    with pytest.raises(Problem):
        scene.run()
    assert scene.log[-1] == "runtime"
    assert "cid" in scene.client._directories


def test_a_failing_stop_record_replaces_the_original_failure(monkeypatch):
    original = problem(409, "NATIVE_RELAY_REFUSED")
    scene = Scene(monkeypatch, sends=[original])
    failure = RuntimeError("OFFLINE stop record")

    async def stopped(runtime, command, *, expired, reason):
        raise failure

    monkeypatch.setattr(native_relay_delivery, "stopped", stopped)
    with pytest.raises(RuntimeError) as failed:
        scene.run()
    assert failed.value is failure and failed.value.__context__ is original


def test_a_runtime_bound_while_the_task_runs_is_seen_at_the_next_read(monkeypatch):
    scene = Scene(monkeypatch, bound=False, sends=[NativeRecipientNotReady(), ACCEPTED])
    bound = scene.runtime = SimpleNamespace(read=None, _rid=None)

    async def rid(request_id):
        scene.log.append(("rid", request_id))
        return SimpleNamespace(value=SimpleNamespace(receipt=SimpleNamespace(conversation_id="cid")))

    bound._rid = rid

    async def wait(ctx, attempt):
        scene.client.runtime = bound
        return True

    scene.client._wait = wait
    assert scene.run() == ACCEPTED
    effects = [entry[0] for entry in scene.effects()]
    assert effects == ["request", "before_send", "request", "rid", "save", "start_follow"]
