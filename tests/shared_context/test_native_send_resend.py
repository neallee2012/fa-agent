"""ADR-0009 4c: a send whose SID can not take it before admission still reaches the Agent's session, same RID, once.

Real API and source (41) / destination (42) Hosted runtimes over one shared store (``file_api``); the platform is an
offline double, and the selector names 42."""

import threading

import pytest

from shared_context import behavior_native_responses_api, behavior_native_turn_api, native_cut
from shared_context.behavior_native_sessions import NativeSessions
from shared_context.errors import Problem
from test_agent_runtime import peer_runtime  # noqa: F401
from test_native_abandon_source import binding_rows  # noqa: F401
import test_native_abandon_source as abandon
from test_native_binding import bound, declared  # noqa: F401
from test_native_cut import (  # noqa: F401 - replace_retried: the cut module's autouse Windows store retry
    _moved_with_files, _resent_until_admitted, cut, enforcing, finished, on_source_version, ready, replace_retried,
    sessions, submit_on_source,
)
from test_native_files_api import (  # noqa: F401
    file_api, file_loops, instruction_models, jwk_client, local_env, native_api, signing_key, synthetic_standing_v1,
    token,
)
from test_native_send_wait import admitted, fake_time, following, no_sleep, warm_metadata


def submits(fixture, since=0):
    return [sid for sid, _version, operation in fixture.control_versions[since:] if operation == "submit"]


# --- 4c-2: a ``replaced`` 409 names the current SID -----------------------------------------------------------------

def test_a_replaced_409_is_resent_once_to_the_named_sid_and_the_row_is_repaired(file_api, monkeypatch):
    """The cut commits after the send's session read and before its submit, and the row misses it: the old SID answers
    409 ``replaced`` with ``current_hosted_session_id``, and the same RID goes there once (M3 repair), no wait."""
    fixture = ready(file_api, monkeypatch, files=False)
    enforcing(fixture, monkeypatch)
    slept = no_sleep(monkeypatch)
    real, committed = behavior_native_responses_api.submit_native_turn, []

    def committed_first(*args, **kwargs):
        if not committed:
            with monkeypatch.context() as unrecorded:
                unrecorded.setattr(NativeSessions, "_confirm_environment", lambda self, client, value: value)
                committed.append(cut(fixture))
        return real(*args, **kwargs)

    monkeypatch.setattr(behavior_native_responses_api, "submit_native_turn", committed_first)
    rows, bound_row = [], NativeSessions.bound_row
    monkeypatch.setattr(NativeSessions, "bound_row", lambda self, *a, **k: rows.append(k.get("fresh")) or bound_row(
        self, *a, **k))
    start = len(fixture.control_versions)
    admitted(submit_on_source(fixture, "after-replaced"), fixture.destination.session_id)
    assert committed == [("ready", None, None)]
    assert submits(fixture, start) == [fixture.source.session_id, fixture.destination.session_id]
    assert True not in rows  # straight to the named SID: the row is not read again to find it
    assert abandon.row(fixture).hosted_session_id == fixture.destination.session_id and slept == []


# --- §3 P1: an older image refuses a changed prompt or an unknown profile before admission -------------------------

OLDER_IMAGE_CODES = pytest.mark.parametrize("code", ["NATIVE_CAPTURE_CHANGED", "NATIVE_PROFILE_UNSUPPORTED"])


def older_image(fixture, code):
    """The source (41) runs an older image: its ``preflight`` refuses every new RID's capture, as ``native_sdk`` does
    for a changed prompt prefix under the same profile or a profile ID it does not know. Returns the refused RIDs."""
    refused = []

    def preflight(capture, **_options):
        refused.append(1)
        raise Problem(409, code, "OFFLINE this image runs another prompt or profile")

    fixture.original.preflight = preflight
    return refused


def cut_before_resend(fixture, monkeypatch):
    """The in-request wait sleeps until the background cut attempt has finished, so the resend sees its outcome."""
    driver, slept = native_cut.native_cut_driver(fixture.case.composition), []
    monkeypatch.setattr(behavior_native_turn_api, "_resend_sleep", lambda seconds: slept.append(seconds) or finished(
        driver, fixture.source.agent_id))
    return slept


@OLDER_IMAGE_CODES
def test_with_the_flag_an_older_images_refusal_is_cut_in_request_and_served_on_the_new_sid(file_api, monkeypatch, code):
    fixture = ready(file_api, monkeypatch)  # the source has workspace files: the cut copies them
    enforcing(fixture, monkeypatch)
    refused = older_image(fixture, code)
    slept = cut_before_resend(fixture, monkeypatch)
    start = len(fixture.control_versions)
    admitted(submit_on_source(fixture, "after-prompt-change"), fixture.destination.session_id)
    assert refused == [1] and slept == [behavior_native_turn_api.RESEND_SECONDS]
    assert submits(fixture, start) == [fixture.source.session_id, fixture.destination.session_id]
    _moved_with_files(fixture)


@OLDER_IMAGE_CODES
def test_with_the_flag_a_cut_that_is_not_done_within_w_is_server_following_and_the_resend_is_served(
        file_api, monkeypatch, code):
    fixture = ready(file_api, monkeypatch, files=False)
    enforcing(fixture, monkeypatch)
    older_image(fixture, code)
    _, slept = fake_time(monkeypatch)  # no resend in this request sees the cut
    warm_metadata(fixture, monkeypatch)
    hold = threading.Event()
    real_cut = NativeSessions.cut
    monkeypatch.setattr(NativeSessions, "cut", lambda self, client, **k: hold.wait(30) and real_cut(self, client, **k))
    try:
        following(submit_on_source(fixture, "slow-cut"), "slow-cut")
    finally:
        hold.set()
    finished(native_cut.native_cut_driver(fixture.case.composition), fixture.source.agent_id)
    start = len(fixture.control_versions)
    admitted(submit_on_source(fixture, "slow-cut"), fixture.destination.session_id)  # the workbench's same-RID resend
    assert submits(fixture, start) == [fixture.destination.session_id] and slept


@OLDER_IMAGE_CODES
def test_without_the_flag_the_follow_moves_the_agent_before_the_older_image_is_called(file_api, monkeypatch, code):
    """Stage (GUARD=0): a moved selector makes the row's SID unusable before any call, so the send's follow moves the
    Agent with its files and the workbench's same-RID resend is served on the new SID; the old image sees nothing."""
    fixture = ready(file_api, monkeypatch)
    assert sessions(fixture).guard.enforce is False
    refused = older_image(fixture, code)
    start = len(fixture.control_versions)
    _resent_until_admitted(fixture, "flag-off-prompt-change")
    assert refused == [] and submits(fixture, start) == [fixture.destination.session_id]
    _moved_with_files(fixture)


def test_on_the_selected_versions_own_sid_the_refusal_stands_and_nothing_is_cut(file_api, monkeypatch):
    fixture = ready(file_api, monkeypatch)
    on_source_version(fixture, monkeypatch)
    enforcing(fixture, monkeypatch)
    older_image(fixture, "NATIVE_CAPTURE_CHANGED")
    cuts = []
    monkeypatch.setattr(native_cut, "send_refused", lambda *args: cuts.append(args))
    start = len(fixture.control_versions)
    refused = submit_on_source(fixture, "genuine-refusal")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_CAPTURE_CHANGED", refused.text
    assert cuts == [] and submits(fixture, start) == [fixture.source.session_id]


def test_a_common_send_never_consults_the_older_image_rule(file_api, monkeypatch):
    """No extra call on the common path: the rule runs only inside a refusal's handler (the call counts of a send
    that meets no switch are pinned in ``test_native_send_wait``)."""
    fixture = ready(file_api, monkeypatch)
    enforcing(fixture, monkeypatch)
    monkeypatch.setattr(behavior_native_turn_api, "older_image", lambda *a, **k: pytest.fail(
        "consulted on a common send"))
    start = len(fixture.control_versions)
    admitted(submit_on_source(fixture, "common"), fixture.source.session_id)
    assert submits(fixture, start) == [fixture.source.session_id]


# --- images from the 9aaa0824 floor to 580c980e: the two refusals carry no ``not_dispatched`` ----------------------

@pytest.mark.parametrize("status, code, not_dispatched", [
    (409, "NATIVE_CAPTURE_CHANGED", True), (409, "NATIVE_PROFILE_UNSUPPORTED", True),
    (503, "NATIVE_PROFILE_UNSUPPORTED", False), (409, "NATIVE_SESSION_CHANGED", False)])
def test_an_older_images_preflight_refusal_without_the_field_is_read_as_not_dispatched(
        enforced_relay_case, monkeypatch, status, code, not_dispatched):
    """Every image since the floor raises these two only before ``rid:<RID>`` (preflight and the conversation capture
    check in ``_submit_locked``), so the client marks them as the field would; nothing else changes."""
    import httpx

    from shared_context.behavior_native_turn_api import NativeTurnGateway
    from shared_context.behavior_native_turn_wire import NativeSubmit

    case = enforced_relay_case
    foundry = case.clients[0]
    monkeypatch.setattr(foundry.http, "_transport", httpx.MockTransport(lambda request: httpx.Response(
        status, json={"error": {"code": code, "message": "OFFLINE older image"}})))
    with pytest.raises(Problem) as refused:
        NativeTurnGateway._invoke(foundry, NativeSubmit(command=case.sender.command, capture=case.sender.capture))
    assert refused.value.body["error"]["code"] == code
    assert (refused.value.body["error"].get("not_dispatched") is True) is not_dispatched


# --- Agent relays: no owner wait; ``_waiting`` resends the same relay RID after the cut ------------------------------

@pytest.fixture
def enforced_relay_case(peer_runtime, monkeypatch):
    from test_native_agent_api import relay_transports

    monkeypatch.setenv("FA_NATIVE_SESSION_GUARD_ENFORCE", "1")
    with relay_transports(peer_runtime, monkeypatch, select_receiver=True) as case:
        yield case


@OLDER_IMAGE_CODES
def test_a_handoff_an_older_image_refuses_is_delivered_after_the_cut_once_with_its_relay_rid(
        enforced_relay_case, monkeypatch, code):
    """The recipient's row-located SID runs an image from before 580c980e (its refusal carries no ``not_dispatched``)
    while the selector names a newer version. The handoff hands the recipient to the cut (a double here that moves it
    to an image that takes the capture) and ``_waiting`` resends the same relay RID once; it is never lost as
    ``NATIVE_RELAY_REFUSED``."""
    from types import SimpleNamespace

    import httpx

    from shared_context.behavior_native_sessions import native_sessions
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
    from shared_context.native_agent_api import NativeAgentRelayGateway
    from test_native_agent_api import send_command

    case = enforced_relay_case
    peer, remote, foundry = case.base.b, case.remotes[1], case.clients[1]
    sessions_b = native_sessions(peer)
    assert sessions_b.guard.enforce
    located = NativeSessions.current

    def row_located(self, client, **options):
        value = located(self, client, **options)
        if self is sessions_b and getattr(value, "_binding_row", None) is None:
            value._binding_row = SimpleNamespace(upgrade=None)  # a claimed row named it (this double has none)
        return value

    monkeypatch.setattr(NativeSessions, "current", row_located)
    selected = NativeSessions._selected_or_none  # the selector names a newer version than the located SID's
    monkeypatch.setattr(NativeSessions, "_selected_or_none", lambda self, client, *a, **k: (
        "42" if self is sessions_b else selected(self, client, *a, **k)))
    cuts, receives = [], []
    monkeypatch.setattr(native_cut, "send_refused", lambda composition, control, agent_id: cuts.append(
        (composition is peer, agent_id)))
    monkeypatch.setattr(behavior_native_turn_api, "_resend_sleep", lambda seconds: None)
    transport = foundry.http._transport

    def older_until_cut(request):
        command = NATIVE_REQUEST.validate_json(request.content)
        if command.operation == "agent_receive":
            receives.append(command.command.request_id)
            if not cuts:
                return httpx.Response(409, json={"error": {"code": code, "message": "OFFLINE older image"}})
        return transport.handle_request(request)

    monkeypatch.setattr(foundry.http, "_transport", httpx.MockTransport(older_until_cut))
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    receipt = gateway.send(case.principal, send_command(case))
    assert receipt.status == "accepted" and cuts == [(True, peer.agent_id)]
    assert receives == [receipt.message_id, receipt.message_id]
    assert list(remote.records) == [receipt.message_id] and len(case.repository.data) == 1


# --- routines: no owner wait; the refused tick cuts and a later tick sends the same occurrence RID -----------------

def routine_on(fixture, monkeypatch):
    """A routine occurrence in the Agent's completed conversation (``_routine_trigger``'s stubs, with a saved intent
    so the occurrence reaches Hosted's ``routine_receive``); returns the trigger."""
    from datetime import datetime, timezone
    import hashlib
    from types import SimpleNamespace

    from shared_context.behavior_native_turn_wire import NativeAgentCommand, NativeRoutineSubmit
    from test_native_routine_wire import routine_origin
    from test_native_send_wait import _routine_trigger

    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    service, trigger = _routine_trigger(fixture, monkeypatch)
    binding, content = fixture.case.composition.runtime.work_policy.policy.template, "Read the original work."
    template = routine_origin()
    origin = template.model_copy(update={
        "tenant_id": binding.tenant_id, "team_id": binding.team_id, "agent_id": fixture.source.agent_id,
        "conversation_id": fixture.saved.conversation_id, "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "occurrence": template.occurrence.model_copy(update={
            "scheduled_for": datetime(2026, 10, 16, 9, tzinfo=timezone.utc)})})
    submission = NativeRoutineSubmit(routine=origin, capture=fixture.saved.capture, command=NativeAgentCommand(
        request_id=origin.request_id, conversation_id=origin.conversation_id, content=content))
    definition = SimpleNamespace(kind="routine", conversation_id=origin.conversation_id)
    monkeypatch.setattr(service, "_intent", lambda control, agent_id, rid: SimpleNamespace(submission=submission))
    monkeypatch.setattr(service, "_version", lambda control, agent_id, version: definition)
    monkeypatch.setattr(service, "_settle", lambda control, receipt: None)
    return trigger, origin.request_id


def routine_receives(fixture, since=0):
    return [sid for sid, _version, operation in fixture.control_versions[since:] if operation == "routine_receive"]


@OLDER_IMAGE_CODES
def test_a_routine_tick_an_older_image_refuses_cuts_and_a_later_tick_is_served_on_the_new_sid(
        file_api, monkeypatch, code):
    fixture = ready(file_api, monkeypatch, files=False)
    enforcing(fixture, monkeypatch)
    refused = older_image(fixture, code)
    trigger, rid = routine_on(fixture, monkeypatch)
    start = len(fixture.control_versions)
    with pytest.raises(Problem) as first:
        trigger()
    assert (first.value.status, first.value.body["error"]["code"]) == (503, "NATIVE_ROUTINE_UNCONFIRMED")
    assert first.value.body["error"]["request_id"] == rid and refused == [1]
    finished(native_cut.native_cut_driver(fixture.case.composition), fixture.source.agent_id)
    assert abandon.row(fixture).hosted_session_id == fixture.destination.session_id
    dispatched = trigger()  # the next minute's tick
    assert dispatched.request_id == rid and refused == [1]
    assert routine_receives(fixture, start) == [fixture.source.session_id, fixture.destination.session_id]
