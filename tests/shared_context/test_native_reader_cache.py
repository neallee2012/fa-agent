"""The API keeps an Agent's known reader binding; Hosted or an environment change says when to read it again."""

import json

import httpx
import pytest

from shared_context import behavior_native_sessions
from shared_context.behavior_native_sessions import READER_IDLE_SECONDS, native_session_id
from shared_context.behavior_native_turn_wire import NativeEnvironmentView
from shared_context.native_turns import _RECORD_READS
from test_native_turn_api import PREFIX, NativeTransport, native_api, submit
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
        self.invoked = []
        monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", self.selected)
        monkeypatch.setattr(behavior_native_sessions, "_read_platform_session", self.session)
        monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(self.route))

    def selected(self, runtime, target, **_):
        self.version_lookups += 1
        return self.version

    def session(self, client):
        self.session_lookups += 1
        if client.target.session_id in self.retired:
            raise behavior_native_sessions._RetiredSession(503, "NATIVE_BOOTSTRAP_UNKNOWN", "retired")
        return client.target if client.target.session_id in self.hosted else None

    def route(self, request):
        sid = request.url.params["agent_session_id"]
        operation = json.loads(request.content)["operation"]
        self.invoked.append((sid, operation))
        if sid in self.unreachable:
            return httpx.Response(self.unreachable[sid], json={"error": {"code": "session_unavailable"}})
        if sid in self.replaced and operation in _RECORD_READS:
            # Hosted's replaced-session precondition (native_turns.native_response).
            return httpx.Response(409, json={"error": {"code": "NATIVE_SESSION_CHANGED", "message": "replaced"}})
        return self.hosted[sid](request)

    def follow_elsewhere(self, version="42"):
        """Another API process (or the deploy job) moved the whole Agent to a newer version's session."""
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
        self.replaced.add(source.target.session_id)
        self.version = version
        return destination


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


def test_submit_after_another_process_followed_checks_once_more_and_never_submits_to_the_old_session(
        native_api, monkeypatch):
    case = native_api
    platform = Platform(case, monkeypatch)
    assert submit(case).status_code == 202
    source = case.remote.target.session_id
    destination = platform.follow_elsewhere()
    platform.invoked.clear()
    response = submit(case, rid="after-follow")
    assert response.status_code == 202, response.text
    assert platform.invoked == [
        (source, "session_read"), (destination.target.session_id, "session_read"),
        (destination.target.session_id, "submit")]
    assert destination.records["after-follow"].hosted_session_id == destination.target.session_id


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
