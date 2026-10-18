"""ADR-0009 2d-i P7: an Agent without a binding row is claimed when it is first located, never refused (M2).

With ``FA_NATIVE_SESSION_GUARD_ENFORCE`` on: a compatible session of another version is claimed and served (no
follow); an incompatible one is claimed as incompatible and switched by the §7a abandon, never invoked; no session at
all bootstraps the selected version. A version created before the compatibility floor (9aaa0824) is incompatible even
when it declares v1. Without the flag the submit follows (``test_native_reader_cache``'s P7 case) and abandons an
incompatible session the same way. An Agent that already has a row makes no more platform reads or session calls than
before. Platform and Hosted are offline doubles; the ``file_api`` cases run the real source and destination Hosted
runtimes."""

from datetime import datetime, timezone

import pytest

from shared_context import behavior_native_sessions
from shared_context.behavior_native_sessions import NativeSessions, native_sessions
from shared_context.native_responses_wire import NATIVE_V2_PROFILE
from test_native_binding import CountingRepository, bound, claim, row  # noqa: F401
from test_native_turn_api import PREFIX, native_api, submit  # noqa: F401
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key, token  # noqa: F401
import test_native_abandon_source as abandon
from test_native_abandon_source import binding_rows  # noqa: F401
from test_native_files_api import file_api, file_loops, local_env  # noqa: F401
from test_native_follow_no_wake import source_calls
from test_native_reader_cache import Platform
from test_native_session_prepare_api import new_environment

BEFORE_FLOOR = datetime(2026, 10, 1, 19, 8, 15, tzinfo=timezone.utc)  # one second before 9aaa0824
AFTER_FLOOR = datetime(2026, 10, 1, 19, 8, 17, tzinfo=timezone.utc)


def enforce(composition, monkeypatch, value=True):
    monkeypatch.setattr(native_sessions(composition).guard, "enforce", value)


def no_follow(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("a claimed compatible session is served, not followed")

    monkeypatch.setattr(NativeSessions, "advance", forbidden)


def test_an_unclaimed_compatible_session_of_another_version_is_claimed_and_served_without_a_follow(
        bound, monkeypatch):
    case = bound
    platform = Platform(case, monkeypatch)
    enforce(case.composition, monkeypatch)
    no_follow(monkeypatch)
    # The located version's definition declares v2 (any supported profile is served; the API still sends v1).
    monkeypatch.setattr(behavior_native_sessions, "_native_profile", lambda client, **_: NATIVE_V2_PROFILE)
    monkeypatch.setattr(NativeSessions, "_row_profile", lambda self, target, version: NATIVE_V2_PROFILE)
    platform.version = "42"  # The selector moved; only the 41 session exists and this Agent has no binding row.
    assert row(case) is None
    served = submit(case, rid="p7-served")
    assert served.status_code == 202, served.text
    assert served.json()["hosted_session_id"] == case.sid
    assert platform.invoked == [(case.sid, "session_read"), (case.sid, "submit")]
    claimed = row(case)
    assert (claimed.hosted_session_id, claimed.created_agent_version, claimed.observed.agent_version) == (
        case.sid, "41", "41")
    assert (claimed.observed.source, claimed.state_profile, claimed.drift) == ("claim", NATIVE_V2_PROFILE, False)
    assert [kind for kind, _ in case.repository.writes] == ["create"]


def switched_without_a_call(fixture, monkeypatch, enforced):
    listed = []
    monkeypatch.setattr(behavior_native_sessions, "_latest_version_reader",
                        lambda *_a, **_k: listed.append(enforced) or fixture.source)
    enforce(fixture.case.composition, monkeypatch, enforced)
    assert abandon.row(fixture) is None
    body = {"request_id": "p7-incompatible", "content": "A message to an unclaimed Agent"}
    moving = fixture.case.client.post(PREFIX + "/submit", headers=fixture.case.headers, json=body)
    if enforced:
        # P6: the switch and the same RID's resend both happen inside this request (the in-request wait).
        assert moving.status_code == 202, moving.text
    else:
        error = moving.json()["error"]
        assert error["not_dispatched"] is True and error["server_following"] is True, moving.text
    assert len(listed) == 1  # the claim's listing is reused by the submit's locate (flag on)
    switched = abandon.row(fixture)
    assert (switched.hosted_session_id, switched.created_agent_version) == (fixture.destination.session_id, "42")
    assert switched.earlier(fixture.source.session_id)
    assert (switched.last_switch.reason, switched.last_switch.basis) == ("abandon_source", "incompatible")
    sent = fixture.case.client.post(PREFIX + "/submit", headers=fixture.case.headers, json=body)
    assert sent.status_code == 202, sent.text
    assert sent.json()["hosted_session_id"] == fixture.destination.session_id
    assert source_calls(fixture) == ([], [], [])


@pytest.mark.parametrize("enforced", [False, True])
@pytest.mark.parametrize("file_api", [{"lane": "foundry-native-v1"}], indirect=True)
def test_an_unclaimed_incompatible_session_is_abandoned_for_a_new_sid_and_never_invoked(
        file_api, monkeypatch, enforced):
    """A pre-H0 image (dev C/D's 09-16 sessions): its version's definition declares no supported profile. Without
    the flag the submit's own follow (the real ``advance``) decides the same §7a abandon."""
    abandon.incompatible_idle(file_api, monkeypatch)
    switched_without_a_call(file_api, monkeypatch, enforced)


@pytest.mark.parametrize("enforced", [False, True])
@pytest.mark.parametrize("file_api", [{"lane": "foundry-native-v1"}], indirect=True)
def test_a_v1_version_created_before_the_compatibility_floor_is_abandoned_and_never_invoked(
        file_api, monkeypatch, enforced):
    """It declares ``foundry-native-v1`` but predates Hosted's pending/CAS checks (ADR-0009 A2d floor, 9aaa0824):
    the claim (flag on) and the §7a source check (the follow) both read it as incompatible."""
    abandon.incompatible_idle(file_api, monkeypatch, profile="foundry-native-v1", created_at=BEFORE_FLOOR)
    switched_without_a_call(file_api, monkeypatch, enforced)


@pytest.mark.parametrize("file_api", [{"lane": "foundry-native-v1"}], indirect=True)
def test_a_v1_version_created_after_the_compatibility_floor_is_claimed_and_served(file_api, monkeypatch):
    fixture = file_api
    abandon.platform(fixture, monkeypatch, created_at=AFTER_FLOOR)
    monkeypatch.setattr(behavior_native_sessions, "_latest_version_reader", lambda *_a, **_k: fixture.source)
    enforce(fixture.case.composition, monkeypatch)
    no_follow(monkeypatch)
    body = {"request_id": "p7-after-floor", "content": "A message to an unclaimed Agent"}
    sent = fixture.case.client.post(PREFIX + "/submit", headers=fixture.case.headers, json=body)
    assert sent.status_code == 202, sent.text
    assert sent.json()["hosted_session_id"] == fixture.source.session_id
    claimed = abandon.row(fixture)
    assert (claimed.hosted_session_id, claimed.created_agent_version, claimed.observed.source) == (
        fixture.source.session_id, fixture.source.agent_version, "claim")


def test_an_unclaimed_agent_without_any_session_bootstraps_the_selected_version(bound, monkeypatch):
    case = bound
    created, calls = new_environment(case, monkeypatch)
    enforce(case.composition, monkeypatch)
    waiting = submit(case, rid="p7-bootstrap")
    # P6: the bootstrap and the same RID's resend both happen inside this request (the in-request wait).
    assert waiting.status_code == 202, waiting.text
    assert [target.agent_version for target in calls] == ["41"]
    assert [command.operation for _, command in case.remote.calls].count("session_prepare") == 1
    sent = submit(case, rid="p7-bootstrap")
    assert sent.status_code == 202, sent.text
    assert sent.json()["hosted_session_id"] == calls[0].session_id
    assert len(calls) == 1 and [command.operation for _, command in case.remote.calls].count("session_prepare") == 1


@pytest.mark.parametrize("enforced", [False, True])
def test_an_agent_with_a_row_makes_the_same_requests_as_before(bound, monkeypatch, enforced):
    case = bound
    platform = Platform(case, monkeypatch)
    enforce(case.composition, monkeypatch, enforced)
    claim(case)
    located = []
    monkeypatch.setattr(behavior_native_sessions, "_latest_version_reader", lambda *args: located.append(args))
    monkeypatch.setattr(behavior_native_sessions, "_native_profile", lambda client, **_: located.append(client))
    assert submit(case, rid="with-a-row").status_code == 202
    assert platform.invoked == [(case.sid, "session_read"), (case.sid, "submit")]
    assert (platform.version_lookups, platform.session_lookups, located) == (1, 1, [])
