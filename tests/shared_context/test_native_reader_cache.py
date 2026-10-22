"""The API keeps an Agent's known reader binding; Hosted or an environment change says when to read it again."""

import json

import httpx
import pytest

from shared_context import behavior_native_sessions
from shared_context.behavior_agent_versions import FOLLOWING, SelectorUnreadable
from shared_context.behavior_native_sessions import (
    READER_IDLE_SECONDS, SELECTOR_CHECK_SECONDS, SELECTOR_STALE_READ_SECONDS, native_session_id,
    _active_session as sdk_active_session, _latest_version_reader as sdk_latest_reader,
)
from shared_context.behavior_native_turn_api import PREFIX as NATIVE_PREFIX
from shared_context.behavior_native_turn_wire import NativeEnvironmentView, NativePrewarmView
from shared_context.native_turns import _RECORD_READS
from test_native_routine_api import routine_api
from test_native_responses_api import PREFIX as RESPONSES_PREFIX, responses_api
from test_native_turn_api import PREFIX, NativeTransport, in_flight, native_api, submit
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token


class Platform:
    """The endpoint selector, the platform sessions and one Hosted process per session over one State store."""

    def __init__(self, case, monkeypatch):
        self.case, self.version = case, case.remote.target.agent_version
        self.version_lookups, self.session_lookups = 0, 0
        self.hosted = {case.remote.target.session_id: case.remote}
        self.replaced, self.retired, self.unreachable = set(), set(), {}
        self.invoked, self.selector = [], None
        # Platform metadata status per SID (get_session, wakes nothing); a listed session is active unless set here.
        self.statuses, self.metadata = {}, []
        monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", self.selected)
        monkeypatch.setattr(behavior_native_sessions, "_read_platform_session", self.session)
        monkeypatch.setattr(behavior_native_sessions, "_latest_version_reader", self.locate)
        monkeypatch.setattr(behavior_native_sessions, "_active_session", self.active)
        monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(self.route))

    def active(self, client):
        sid = client.target.session_id
        self.metadata.append(sid)
        return (sid in self.hosted and self.statuses.get(sid, "active") == "active"
                and self.hosted[sid].target.agent_version == client.target.agent_version)

    def selected(self, runtime, target, **_):
        self.version_lookups += 1
        if self.selector is not None:
            raise self.selector
        return self.version

    def session(self, client, **_options):
        self.session_lookups += 1
        if client.target.session_id in self.retired:
            raise behavior_native_sessions._RetiredSession(503, "NATIVE_BOOTSTRAP_UNKNOWN", "retired")
        return client.target if client.target.session_id in self.hosted else None

    def locate(self, client, client_id, selected=None):
        """The session listing (metadata only): ``selected``'s session, else any other version's."""
        found = {hosted.target.agent_version: hosted.target for hosted in self.hosted.values()}
        if not found:
            return None
        return found[selected if selected in found else next(iter(found))]

    def route(self, request):
        sid = request.url.params["agent_session_id"]
        operation = json.loads(request.content)["operation"]
        self.invoked.append((sid, operation))
        if operation == "prewarm":
            view = NativePrewarmView(agent_id=self.case.remote.target.agent_id, hosted_session_id=sid, toolboxes=0)
            return httpx.Response(200, content=view.model_dump_json().encode(), headers={
                "content-type": "application/json", "x-agent-invocation-id": "platform-prewarm",
                "x-agent-session-id": sid})
        if sid in self.unreachable:
            return httpx.Response(self.unreachable[sid], json={"error": {"code": "session_unavailable"}})
        if sid in self.replaced and operation in _RECORD_READS:
            # Hosted's replaced-session precondition (native_turns.native_response).
            return httpx.Response(409, json={"error": {"code": "NATIVE_SESSION_CHANGED", "message": "replaced"}})
        return self.hosted[sid](request)

    def follow_elsewhere(self, version="42", *, replace=True):
        """Another API process (or the deploy job) moved the whole Agent to a newer version's session.

        ``replace=False`` leaves the old session answering normally, so any call that still reaches it is visible
        in ``invoked`` rather than hidden behind Hosted's replaced-session refusal."""
        source = self.case.remote
        target = source.target.model_copy(update={"agent_version": version, "session_id": native_session_id(
            source.target, self.case.composition.config.identity.managed_identity_client_id, version=version)})
        destination = NativeTransport(target)
        destination.records, destination.preparation = source.records, source.preparation
        environment = NativeEnvironmentView(
            agent_id=target.agent_id, request_id="follow-elsewhere", state="ready",
            previous_hosted_session_id=source.target.session_id, previous_agent_version=source.target.agent_version,
            hosted_session_id=target.session_id, hosted_agent_version=version)
        for hosted in (source, destination):
            hosted.session_binding, hosted.environment = target.session_id, environment
        self.hosted[target.session_id] = destination
        if replace:
            self.replaced.add(source.target.session_id)
        self.version = version
        return destination


def past_selector_check(monkeypatch, seconds=SELECTOR_CHECK_SECONDS + 1):
    """Later than the selector check window, far earlier than the reader's idle expiry."""
    assert seconds < READER_IDLE_SECONDS
    now = behavior_native_sessions.monotonic()
    monkeypatch.setattr(behavior_native_sessions, "monotonic", lambda: now + seconds)


def read(case, rid="native-rid"):
    return case.client.get(PREFIX + "/requests/" + rid, headers=case.headers)


def test_reads_invoke_the_known_binding_without_platform_lookups(native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    for _ in range(3):
        assert read(case).status_code == 200
    assert (platform.version_lookups, platform.session_lookups) == (1, 1)
    assert [operation for _, operation in platform.invoked] == ["session_read", "submit", "read", "read", "read"]


def test_a_read_refused_by_a_replaced_session_reads_the_platform_once_and_lands_on_the_new_one(native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    destination = platform.follow_elsewhere().target.session_id
    platform.invoked.clear()
    response = read(case)
    assert response.status_code == 200 and response.json()["request_id"] == "native-rid"
    assert platform.invoked == [(source, "read"), (destination, "read")]
    assert (platform.version_lookups, platform.session_lookups) == (2, 2)
    assert read(case).status_code == 200
    assert platform.invoked[-1] == (destination, "read") and platform.version_lookups == 2


def test_the_status_poll_uses_the_known_binding_and_reads_again_after_a_replacement(native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    path = PREFIX + "/conversations/native-cid/requests/native-rid/status"
    for _ in range(2):
        assert case.client.get(path, headers=case.headers).status_code == 200
    assert platform.version_lookups == 1
    source = case.remote.target.session_id
    destination = platform.follow_elsewhere().target.session_id
    platform.invoked.clear()
    response = case.client.get(path, headers=case.headers)
    assert response.status_code == 200, response.text
    assert platform.invoked == [(source, "turn_status"), (destination, "turn_status")]
    assert platform.version_lookups == 2


def test_submit_after_another_process_followed_reads_the_selector_and_never_invokes_the_old_session(
        native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    destination = platform.follow_elsewhere()
    platform.invoked.clear()
    response = submit(case, rid="after-follow")
    assert response.status_code == 202, response.text
    assert platform.invoked == [
        (destination.target.session_id, "session_read"), (destination.target.session_id, "submit")]
    assert destination.records["after-follow"].hosted_session_id == destination.target.session_id
    assert platform.version_lookups == 2


def test_an_environment_operation_in_this_process_reads_the_binding_again(native_api, monkeypatch):
    from shared_context.behavior_native_sessions import native_sessions

    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    sessions = native_sessions(case.composition)
    assert sessions.reader_cached()
    with sessions._changing():
        # Reads during the operation do not resume the cached (possibly stopping) source.
        assert not sessions.reader_cached()
    refused = case.client.post(PREFIX + "/environment", headers=case.headers, json={
        "request_id": "follow-another-version", "expected_agent_version": "99"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_ENVIRONMENT_CONFLICT"
    lookups = platform.version_lookups
    assert read(case).status_code == 200
    assert platform.version_lookups == lookups + 1


@pytest.mark.parametrize("status", [404, 500])
def test_a_retired_cached_session_is_not_invoked_again(native_api, monkeypatch, status):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    platform.retired.add(source)
    platform.unreachable[source] = status
    first, second = read(case), read(case)
    # 404 says the binding moved: the platform is read in the same request; another failure waits for the next.
    assert first.json()["error"]["code"] == ("NATIVE_BOOTSTRAP_UNKNOWN" if status == 404 else "FOUNDRY_INVOCATION_UNKNOWN")
    assert second.status_code == 503 and second.json()["error"]["code"] == "NATIVE_BOOTSTRAP_UNKNOWN"
    assert platform.invoked.count((source, "read")) == 1


def test_a_binding_unused_for_a_day_is_read_again(native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    now = behavior_native_sessions.monotonic()
    monkeypatch.setattr(behavior_native_sessions, "monotonic", lambda: now + READER_IDLE_SECONDS + 1)
    assert read(case).status_code == 200
    assert platform.version_lookups == 2


def test_a_new_conversation_after_the_selector_moved_never_invokes_the_kept_old_session(native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    destination = platform.follow_elsewhere(replace=False)
    platform.invoked.clear()
    past_selector_check(monkeypatch)
    response = submit(case, rid="after-move")
    assert response.status_code == 202, response.text
    assert platform.invoked == [
        (destination.target.session_id, "session_read"), (destination.target.session_id, "submit")]
    assert source not in {sid for sid, _ in platform.invoked}
    assert destination.records["after-move"].hosted_session_id == destination.target.session_id
    assert platform.version_lookups == 2


def test_a_follow_up_never_reads_the_conversation_on_the_kept_old_session(native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    platform.follow_elsewhere(replace=False)
    platform.invoked.clear()
    past_selector_check(monkeypatch)
    response = submit(case, rid="follow-up", conversation_id="native-cid")
    # The conversation stays on the old session: the selected one refuses it before admission.
    error = response.json()["error"]
    assert (response.status_code, error["code"]) == (409, "NATIVE_SESSION_CHANGED"), response.text
    assert error["not_dispatched"] is True and error["request_id"] == "follow-up"
    assert platform.invoked and source not in {sid for sid, _ in platform.invoked}


def test_a_receipt_read_after_the_selector_moved_never_invokes_the_kept_old_session(native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    destination = platform.follow_elsewhere(replace=False)
    platform.invoked.clear()
    past_selector_check(monkeypatch)
    response = read(case)
    assert response.status_code == 200, response.text
    assert platform.invoked == [(destination.target.session_id, "read")]


def test_a_prewarm_after_the_selector_moved_wakes_only_the_selected_session(native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    destination = platform.follow_elsewhere(replace=False)
    platform.invoked.clear()
    # No clock change: the prewarm reads the selector itself, so even a just-moved selector never wakes the old one.
    response = case.client.post(NATIVE_PREFIX + "/prewarm", content=b"{}",
                                headers={**case.headers, "Content-Type": "application/json"})
    assert response.status_code == 200 and response.json()["state"] == "sent", response.text
    assert platform.invoked == [(destination.target.session_id, "prewarm")]


def test_an_unmoved_selector_keeps_the_binding_reads_reuse_the_window_and_each_submit_reads_it(
        native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    source = case.remote.target.session_id
    assert submit(case).status_code == 202
    for _ in range(2):
        assert read(case).status_code == 200
    assert (platform.version_lookups, platform.session_lookups) == (1, 1)
    assert submit(case, rid="second").status_code == 202
    assert (platform.version_lookups, platform.session_lookups) == (2, 1)
    past_selector_check(monkeypatch)
    for _ in range(2):
        assert read(case).status_code == 200
    # Past the window a read reads the selector once more; the same version keeps the binding (no session lookup).
    assert (platform.version_lookups, platform.session_lookups) == (3, 1)
    assert {sid for sid, _ in platform.invoked} == {source}


def test_the_version_locator_confirms_the_selected_version_never_the_first_listed(native_api, monkeypatch):
    from azure.ai.projects.models import AgentSessionResource
    from shared_context.behavior_native_sessions import NativeSessions
    from test_native_session_prepare_api import project_client

    case = native_api
    platform = Platform(case, monkeypatch)
    client_id = case.composition.config.identity.managed_identity_client_id
    selected = platform.follow_elsewhere("42", replace=False)
    # A deploy-time follow made 42's session, but this Agent was never prepared on it.
    selected.preparation = None
    case.composition._native_sessions = NativeSessions(case.composition)

    def listed(version):
        return AgentSessionResource({
            "agent_session_id": native_session_id(case.foundry.target, client_id, version=version), "status": "idle",
            "version_indicator": {"type": "version_ref", "agent_version": version}})

    # Neither the listing order nor the version numbers say which version is selected.
    project_client(monkeypatch, list_sessions=lambda name, **_: iter((listed("40"), listed("42"), listed("43"))))
    monkeypatch.setattr(behavior_native_sessions, "_latest_version_reader", sdk_latest_reader)
    platform.invoked.clear()
    refused = submit(case, rid="never-prepared")
    error = refused.json()["error"]
    assert (refused.status_code, error["code"]) == (409, "NATIVE_SESSION_CHANGED"), refused.text
    assert error["not_dispatched"] is True and "server_following" not in error
    assert platform.invoked == [(selected.target.session_id, "session_read")] * 2


def test_the_version_locator_keeps_its_bounded_page_when_only_other_versions_are_listed(native_api, monkeypatch):
    from azure.ai.projects.models import AgentSessionResource
    from shared_context.errors import Problem
    from test_native_session_prepare_api import project_client

    case = native_api
    client_id = case.composition.config.identity.managed_identity_client_id

    def listed(version, status="idle"):
        return AgentSessionResource({
            "agent_session_id": native_session_id(case.foundry.target, client_id, version=version), "status": status,
            "version_indicator": {"type": "version_ref", "agent_version": version}})

    def locate(*versions, status="idle"):
        project_client(monkeypatch, list_sessions=lambda name, **_: iter(
            [listed(version, status) for version in versions[:-1]] + [listed(versions[-1])]))
        return sdk_latest_reader(case.foundry, client_id, "42")

    assert locate(*["40"] * 99, "42").agent_version == "42"
    assert locate("43", "40").agent_version == "43"
    # The selected session may be past the bounded page: other versions' sessions inside it do not answer for it.
    # The page counts every listed session, whatever its status.
    for status in ("idle", "deleted"):
        with pytest.raises(Problem) as beyond:
            locate(*["40"] * 100, "42", status=status)
        assert (beyond.value.status, beyond.value.body["error"]["code"]) == (503, "NATIVE_ENVIRONMENT_UNAVAILABLE")
    assert locate(*["40"] * 99, "42", status="deleted").agent_version == "42"


def test_a_follow_up_after_the_selector_check_expired_reads_its_conversation_on_the_confirmed_reader(
        native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    source = case.remote.target.session_id
    past_selector_check(monkeypatch)
    platform.invoked.clear()
    most = in_flight(monkeypatch)
    assert submit(case, rid="native-rid-2", conversation_id="native-cid").status_code == 202
    # The conversation is read once, after the session read (never alongside it: README#rtt-a-submit-prefetch).
    assert most == {source: 1}
    assert platform.invoked == [(source, "session_read"), (source, "conversation"), (source, "submit")]


def test_a_selected_version_without_a_session_never_invokes_an_older_one_and_the_same_rid_waits(native_api, monkeypatch):
    from shared_context.behavior_native_sessions import NativeSessions

    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    platform.version = "42"  # Another process moved the selector; nobody created the 42 session yet.
    # 2d-i P7: the submit follows (not README ⓐ's refusal); the follow itself is ``advance``'s own tests' subject.
    monkeypatch.setattr(NativeSessions, "advance", lambda self, client, command, *, before_send: NativeEnvironmentView(
        agent_id=case.remote.target.agent_id, request_id=command.request_id, state="pending", reason="source_active",
        previous_hosted_session_id=source, previous_agent_version="41",
        hosted_session_id="fa-native-destination", hosted_agent_version="42"))
    platform.invoked.clear()
    past_selector_check(monkeypatch)
    waiting = submit(case, rid="before-42-session")
    error = waiting.json()["error"]
    assert error["not_dispatched"] is True and error["request_id"] == "before-42-session", waiting.text
    assert error["server_following"] is True
    assert platform.invoked == []
    destination = platform.follow_elsewhere("42", replace=False)
    accepted = submit(case, rid="before-42-session")
    assert accepted.status_code == 202, accepted.text
    assert source not in {sid for sid, _ in platform.invoked}
    assert destination.records["before-42-session"].hosted_session_id == destination.target.session_id


def test_p7_a_rowless_agent_with_only_another_versions_session_follows_from_the_submit(native_api, monkeypatch):
    from shared_context.behavior_native_sessions import NativeSessions

    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    platform.version = "42"  # The selector moved; only the 41 session exists and this Agent has no binding row.
    followed = []

    def advance(self, client, command, *, before_send):
        followed.append(command.expected_agent_version)
        return NativeEnvironmentView(
            agent_id=case.remote.target.agent_id, request_id=command.request_id, state="pending",
            reason="source_active", previous_hosted_session_id=case.remote.target.session_id,
            previous_agent_version=case.remote.target.agent_version, hosted_session_id="fa-native-destination",
            hosted_agent_version="42")

    monkeypatch.setattr(NativeSessions, "advance", advance)
    platform.invoked.clear()
    past_selector_check(monkeypatch)
    moving = submit(case, rid="p7-moves")
    error = moving.json()["error"]
    assert error["not_dispatched"] is True and error.get("server_following") is True, moving.text
    assert followed == ["42"] and platform.invoked == []


def test_a_submit_whose_session_listing_is_unavailable_is_not_called_never_prepared(native_api, monkeypatch):
    from shared_context.errors import Problem

    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    platform.version = "42"  # Nobody created the 42 session yet, and the session listing cannot be confirmed.

    def unavailable(client, client_id, selected=None):
        raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the native locator is outside the bounded session page")

    monkeypatch.setattr(behavior_native_sessions, "_latest_version_reader", unavailable)
    platform.invoked.clear()
    waiting = submit(case, rid="listing-unavailable")
    error = waiting.json()["error"]
    assert (waiting.status_code, error["code"], error["not_dispatched"], error["request_id"]) == (
        503, "NATIVE_ENVIRONMENT_UNAVAILABLE", True, "listing-unavailable"), waiting.text
    assert platform.invoked == []


def unreadable_selector():
    """What HostedVersions.current raises for a transient FoundryCallError (timeout, 429, 5xx)."""
    return SelectorUnreadable(503, FOLLOWING, "this Agent's Hosted version for the deployed build is not readable yet")


def test_a_poll_keeps_the_recently_confirmed_reader_while_the_selector_is_briefly_unreadable(native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    platform.invoked.clear()
    platform.selector = unreadable_selector()
    past_selector_check(monkeypatch, SELECTOR_STALE_READ_SECONDS - 1)
    path = PREFIX + "/conversations/native-cid/requests/native-rid/status"
    for _ in range(2):
        assert read(case).status_code == 200
        assert case.client.get(path, headers=case.headers).status_code == 200
    assert platform.invoked == [(source, "read"), (source, "turn_status")] * 2
    # Each read tried the selector once; the kept reader was never dropped, so no session lookup either.
    assert (platform.version_lookups, platform.session_lookups) == (5, 1)
    platform.selector = None
    assert read(case).status_code == 200
    assert (platform.version_lookups, platform.session_lookups) == (6, 1)


def test_a_poll_past_the_stale_bound_answers_following_and_keeps_the_reader(native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    platform.invoked.clear()
    platform.selector = unreadable_selector()
    past_selector_check(monkeypatch, SELECTOR_STALE_READ_SECONDS + 1)
    refused = read(case)
    assert (refused.status_code, refused.json()["error"]["code"]) == (503, "AGENT_VERSION_FOLLOWING")
    assert platform.invoked == []
    platform.selector = None
    assert read(case).status_code == 200
    assert platform.invoked == [(source, "read")] and platform.session_lookups == 1


def test_a_submit_never_proceeds_on_an_unreadable_selector(native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    platform.invoked.clear()
    platform.selector = unreadable_selector()
    past_selector_check(monkeypatch)
    waiting = submit(case, rid="while-unreadable")
    error = waiting.json()["error"]
    assert (waiting.status_code, error["code"]) == (503, "AGENT_VERSION_FOLLOWING"), waiting.text
    assert error["not_dispatched"] is True and error["request_id"] == "while-unreadable"
    assert platform.invoked == []
    platform.selector = None
    assert submit(case, rid="while-unreadable").status_code == 202
    assert [operation for _, operation in platform.invoked] == ["session_read", "submit"]


def test_a_submit_inside_the_selector_window_still_never_invokes_the_kept_old_session(native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    destination = platform.follow_elsewhere(replace=False)
    platform.invoked.clear()
    # No clock change: the selector read by the first submit is still inside SELECTOR_CHECK_SECONDS.
    response = submit(case, rid="right-after-move")
    assert response.status_code == 202, response.text
    assert platform.invoked == [
        (destination.target.session_id, "session_read"), (destination.target.session_id, "submit")]
    assert source not in {sid for sid, _ in platform.invoked}


def test_the_selector_window_counts_from_before_the_selector_read(native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    clock = [behavior_native_sessions.monotonic()]
    monkeypatch.setattr(behavior_native_sessions, "monotonic", lambda: clock[0])
    selected = platform.selected

    def slow(runtime, target, **kwargs):
        clock[0] += SELECTOR_CHECK_SECONDS - 2  # The selector read itself takes 8 seconds.
        return selected(runtime, target, **kwargs)

    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", slow)
    assert submit(case).status_code == 202
    assert platform.version_lookups == 1
    clock[0] += 3  # 11 seconds after that read started, 3 seconds after it returned.
    assert read(case).status_code == 200
    assert platform.version_lookups == 2


def test_a_selector_read_that_started_earlier_never_replaces_one_that_started_later(native_api, monkeypatch):
    from shared_context.behavior_native_sessions import native_sessions

    case = native_api
    sessions = native_sessions(case.composition)
    clock = [1000.0]
    monkeypatch.setattr(behavior_native_sessions, "monotonic", lambda: clock[0])
    answers = []

    def selected(client):
        if answers:
            answers.append("42")
            return "42"
        answers.append("41")
        clock[0] += 1
        # Another request starts its own selector read meanwhile, sees the moved selector and returns first.
        assert sessions._checked_version(client, clock[0] - 0.5) == "42"
        return "41"

    monkeypatch.setattr(sessions, "selected_version", selected)
    assert sessions._checked_version(case.foundry, clock[0] - 1) == "41"
    # The read inside the window is answered by the later-started read, not the one that returned last.
    assert sessions._checked_version(case.foundry) == "42"
    assert answers == ["41", "42"]


def owner_write(case, kind):
    from test_user_images_native import encoded

    if kind == "delivered":
        case.remote.completed("native-rid")
    path, body = {
        "cancel": ("/cancel", {"conversation_id": "native-cid", "request_id": "native-rid"}),
        "delivered": ("/delivered", {"conversation_id": "native-cid", "request_id": "native-rid",
                                     "result_digest": "a" * 64}),
        "images": ("/images", {"png_base64": encoded()}),
        "append": ("/append", {"request_id": "native-rid", "conversation_id": "native-cid", "append_id": "append-one",
                               "expected_submit_digest": case.remote.records["native-rid"].submit_digest,
                               "content": "one more line"}),
    }[kind]
    return case.client.post(PREFIX + path, headers=case.headers, json=body)


@pytest.mark.parametrize("kind, operation, status, code", [
    ("cancel", "cancel", 200, None), ("delivered", "delivered", 200, None),
    ("images", "user-image-upload", 200, None),
    # The turn was accepted on the old session: the selected session refuses it as before (no silent rebinding).
    ("append", "read", 409, "NATIVE_SESSION_CHANGED"),
])
def test_an_owner_write_inside_the_selector_window_never_invokes_the_kept_old_session(
        native_api, monkeypatch, kind, operation, status, code):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    destination = platform.follow_elsewhere(replace=False).target.session_id
    platform.invoked.clear()
    # No clock change: the submit's selector read is still inside SELECTOR_CHECK_SECONDS.
    response = owner_write(case, kind)
    assert response.status_code == status, response.text
    assert code is None or response.json()["error"]["code"] == code
    assert platform.invoked == [(destination, "session_read"), (destination, operation)]
    assert source not in {sid for sid, _ in platform.invoked}


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_a_routine_trigger_inside_the_selector_window_never_invokes_the_kept_old_session(routine_api, monkeypatch):
    from datetime import timedelta
    from test_native_routine_api import daily_definition

    case = routine_api
    platform = Platform(case, monkeypatch)
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    definition = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    assert case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers).status_code == 200
    case.c.h.time = due + timedelta(seconds=2)
    source = case.remote.target.session_id
    destination = platform.follow_elsewhere(replace=False).target.session_id
    platform.invoked.clear()
    # No clock change: the selector read by the definition save is still inside SELECTOR_CHECK_SECONDS.
    refused = case.trigger({"routine_id": "daily-summary", "scheduled_for": due.isoformat()})
    # The routine's conversation stays on the old session: the selected one refuses it, nothing is admitted.
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "NATIVE_SESSION_CHANGED"), refused.text
    assert platform.invoked == [(destination, "session_read"), (destination, "read"), (destination, "conversation")]
    assert source not in {sid for sid, _ in platform.invoked}


@pytest.mark.parametrize("kind", ["images", "append"])
def test_an_owner_write_after_the_selector_moved_to_a_version_without_a_session_invokes_nothing(
        native_api, monkeypatch, kind):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    platform.version = "42"  # Another process moved the selector; nobody created the 42 session yet.
    platform.invoked.clear()
    response = owner_write(case, kind)
    assert (response.status_code, response.json()["error"]["code"]) == (409, "NATIVE_SESSION_CHANGED"), response.text
    assert platform.invoked == []


def test_a_ready_handoff_inside_the_selector_window_reads_the_selector_now(native_api, monkeypatch):
    from shared_context.behavior_auth import BehaviorPrincipal
    from shared_context.behavior_native_turn_api import NativeTurnGateway
    from shared_context.behavior_team_registry import resolve_agent_scope

    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    destination = platform.follow_elsewhere(replace=False).target
    sent = []
    monkeypatch.setattr(NativeTurnGateway, "_signal_ready_handoffs",
                        staticmethod(lambda client, value: sent.append(client.target.session_id)))
    scope = resolve_agent_scope(case.composition.runtime.store, case.composition.config, destination.agent_id)
    owner = BehaviorPrincipal(str(scope.tenant_id), scope.human_oid, scope.team_id, "human", None,
                              frozenset({"read", "submit"}))
    view = NativeEnvironmentView(
        agent_id=destination.agent_id, request_id="follow-ready", state="ready", files_manifest_sha256="c" * 64,
        previous_hosted_session_id=case.remote.target.session_id, previous_agent_version="41",
        hosted_session_id=destination.session_id, hosted_agent_version="42")
    lookups = platform.version_lookups
    # No clock change: the submit's selector read (41) is still inside SELECTOR_CHECK_SECONDS.
    NativeTurnGateway(case.composition, enabled=True)._ready_handoffs(owner, view)
    assert platform.version_lookups == lookups + 1
    assert sent == [destination.session_id]


@pytest.mark.parametrize("kind", ["cancel", "delivered"])
def test_a_stop_while_the_selected_version_has_no_session_reaches_the_active_session_holding_the_turn(
        native_api, monkeypatch, kind):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    platform.version = "42"  # Another process moved the selector; nobody created the 42 session yet.
    platform.invoked.clear()
    response = owner_write(case, kind)
    assert response.status_code == 200, response.text
    assert response.json()["cancel_requested" if kind == "cancel" else "delivered"] is True
    # One metadata read (it wakes nothing) confirmed the turn's session is active; only then was it invoked, once.
    assert platform.metadata == [source]
    assert platform.invoked == [(source, kind)]


@pytest.mark.parametrize("kind", ["cancel", "delivered"])
def test_a_stop_whose_turn_session_is_not_active_sends_nothing_and_the_same_command_is_sent_again(
        native_api, monkeypatch, kind):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    platform.version = "42"
    platform.statuses[source] = "idle"
    platform.invoked.clear()
    refused = owner_write(case, kind)
    error = refused.json()["error"]
    assert (refused.status_code, error["code"], error["not_dispatched"], error["request_id"]) == (
        409, "NATIVE_SESSION_CHANGED", True, "native-rid"), refused.text
    assert error["automatic_retry"] is False
    assert platform.metadata == [source]
    assert platform.invoked == []
    # The same command, sent again once its session is active (a running turn's session is), reaches it once.
    platform.statuses[source] = "active"
    again = owner_write(case, kind)
    assert again.status_code == 200, again.text
    assert platform.invoked == [(source, kind)]


def platform_metadata(case, platform, monkeypatch, outcome):
    """The real ``_active_session`` over a fixed ``get_session`` answer (``_session_metadata``, it wakes nothing)."""
    from types import SimpleNamespace

    monkeypatch.setattr(behavior_native_sessions, "_active_session", sdk_active_session)
    recorded = case.remote.target.agent_version
    answer = {"active": ("active", recorded), "idle": ("idle", recorded), "other_version": ("active", "42"),
              "not_found": None}.get(outcome, outcome)

    def metadata(client):
        platform.metadata.append(client.target.session_id)
        if isinstance(answer, Exception):
            raise answer
        return None if answer is None else SimpleNamespace(
            status=answer[0], version_indicator=SimpleNamespace(agent_version=answer[1]))

    monkeypatch.setattr(behavior_native_sessions, "_session_metadata", metadata)


# ADR-0009 section 10: the platform's own metadata decides; only an active session of the recorded version is invoked.
@pytest.mark.parametrize("kind", ["cancel", "delivered"])
@pytest.mark.parametrize("outcome", ["idle", "other_version", "not_found", "active"])
def test_a_stop_reaches_the_turn_session_only_when_its_metadata_says_active_on_the_recorded_version(
        native_api, monkeypatch, kind, outcome):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    platform.version = "42"  # Another process moved the selector; nobody created the 42 session yet.
    platform_metadata(case, platform, monkeypatch, outcome)
    platform.invoked.clear()
    response = owner_write(case, kind)
    assert platform.metadata == [source]
    if outcome == "active":
        assert response.status_code == 200, response.text
        assert platform.invoked == [(source, kind)]
        return
    error = response.json()["error"]
    assert (response.status_code, error["code"], error["not_dispatched"], error["request_id"]) == (
        409, "NATIVE_SESSION_CHANGED", True, "native-rid"), response.text
    assert platform.invoked == []


@pytest.mark.parametrize("kind", ["cancel", "delivered"])
@pytest.mark.parametrize("status, code, dispatched_flag", [
    (403, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE", False), (401, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE", False),
    # A transient or unknown platform read stays a stop that was sent nowhere and may be sent again.
    (503, "NATIVE_BOOTSTRAP_UNKNOWN", True),
])
def test_a_stop_whose_turn_session_metadata_read_is_denied_reports_the_denial_unchanged(
        native_api, monkeypatch, kind, status, code, dispatched_flag):
    from shared_context.errors import Problem

    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    platform.version = "42"
    platform_metadata(case, platform, monkeypatch, Problem(status, code, "the original platform session could not be read"))
    platform.invoked.clear()
    response = owner_write(case, kind)
    error = response.json()["error"]
    assert platform.metadata == [source] and platform.invoked == []
    if dispatched_flag:
        assert (response.status_code, error["code"], error["not_dispatched"]) == (
            409, "NATIVE_SESSION_CHANGED", True), response.text
    else:
        assert (response.status_code, error["code"]) == (status, code), response.text
        assert "not_dispatched" not in error


def test_a_stop_while_pending_work_blocks_the_pointer_switch_reaches_the_session_holding_the_turn(
        native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target
    # The 42 session exists, but the Agent record still names the 41 session: its accepted turn blocks the switch.
    target = source.model_copy(update={"agent_version": "42", "session_id": native_session_id(
        source, case.composition.config.identity.managed_identity_client_id, version="42")})
    destination = NativeTransport(target)
    destination.records, destination.preparation = case.remote.records, case.remote.preparation
    pending = NativeEnvironmentView(
        agent_id=target.agent_id, request_id="follow-blocked", state="pending", reason="accepted_or_started",
        previous_hosted_session_id=source.session_id, previous_agent_version="41",
        hosted_session_id=target.session_id, hosted_agent_version="42")
    for hosted in (case.remote, destination):
        hosted.session_binding, hosted.environment = source.session_id, pending
    platform.hosted[target.session_id] = destination
    platform.version = "42"
    platform.invoked.clear()
    response = owner_write(case, "cancel")
    assert response.status_code == 200, response.text
    assert response.json()["cancel_requested"] is True
    # The selected session is read (twice: the kept 41 reader is dropped and the platform read once more); both
    # reads name the 41 session as current, and that session, active, receives the cancel.
    assert platform.invoked == [
        (target.session_id, "session_read"), (target.session_id, "session_read"), (source.session_id, "cancel")]
    assert platform.metadata == [source.session_id]


def responses_platform(env, monkeypatch):
    """``Platform`` over the Responses harness: Hosted calls are recorded as ``(sid, operation)`` and still answered by
    the Responses transport, so the original turn keeps its Responses profile."""
    inner = env.case.foundry.http._transport.handler
    platform = Platform(env.case, monkeypatch)

    def route(request):
        if "/protocols/openai/" not in request.url.path:
            platform.invoked.append((request.url.params["agent_session_id"], json.loads(request.content)["operation"]))
        return inner(request)

    monkeypatch.setattr(env.case.foundry.http, "_transport", httpx.MockTransport(route))
    return platform


def responses_cancel(env, rid="responses-stop"):
    submitted = env.case.client.post(RESPONSES_PREFIX + "/submit", headers=env.case.headers,
                                     json={"request_id": rid, "content": "Original"})
    assert submitted.status_code == 202, submitted.text
    original = env.case.remote.records[rid]
    return lambda: env.case.client.post(RESPONSES_PREFIX + "/cancel", headers=env.case.headers, json={
        "operation": "cancel", "request_id": rid, "conversation_id": original.conversation_id})


def test_a_responses_stop_while_the_selected_version_has_no_session_reaches_the_active_session_holding_the_turn(
        responses_api, monkeypatch):
    env = responses_api
    platform = responses_platform(env, monkeypatch)
    cancel = responses_cancel(env)
    source = env.case.remote.target.session_id
    platform.version = "42"  # Another process moved the selector; nobody created the 42 session yet.
    platform.invoked.clear()
    before = len(env.calls)
    response = cancel()
    assert response.status_code == 200, response.text
    assert (response.json()["cancel_requested"], response.json()["status"]) == (True, "unknown")
    # One metadata read (it wakes nothing) confirmed the turn's session is active; the original was read there and
    # the cancel sent there, once. No platform Responses call: the turn had not started.
    assert platform.metadata == [source]
    assert platform.invoked == [(source, "read"), (source, "cancel")]
    assert not any("/protocols/openai/" in call.url.path for call in env.calls[before:])


def test_a_responses_stop_whose_turn_session_is_not_active_sends_nothing_with_not_dispatched_and_its_rid(
        responses_api, monkeypatch):
    env = responses_api
    platform = responses_platform(env, monkeypatch)
    cancel = responses_cancel(env)
    source = env.case.remote.target.session_id
    platform.version = "42"
    platform.statuses[source] = "idle"
    platform.invoked.clear()
    before = len(env.calls)
    refused = cancel()
    error = refused.json()["error"]
    assert (refused.status_code, error["code"], error["not_dispatched"], error["request_id"]) == (
        409, "NATIVE_SESSION_CHANGED", True, "responses-stop"), refused.text
    assert error["automatic_retry"] is False
    assert platform.metadata == [source]
    assert platform.invoked == []
    assert env.calls[before:] == []
