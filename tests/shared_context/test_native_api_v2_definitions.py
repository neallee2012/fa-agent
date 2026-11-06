"""ADR-0009 M1 2d-ii: the API accepts only Hosted versions whose definition declares a supported profile.

From 3d-ii the supported set is {v3} and the API sends v3; a v1 or v2 definition or row is incompatible. Offline
doubles only."""

import logging
from types import SimpleNamespace

import pytest

import shared_context.behavior_agent_versions as versions
from shared_context import behavior_native_sessions
from shared_context.behavior_agent_versions import FOLLOWING, HostedVersions
from shared_context.behavior_native_sessions import NativeSessions, _require_native_version
from shared_context.errors import Problem
from shared_context.native_binding import NativeBindingObservation, NativeBindingRow, SessionGuard
from shared_context.native_responses_wire import NATIVE_V2_PROFILE, NATIVE_V3_PROFILE, UNLABELED_STATE_PROFILE
from test_agent_versions import Foundry, R, _code

V1, V2, V3 = UNLABELED_STATE_PROFILE, NATIVE_V2_PROFILE, NATIVE_V3_PROFILE


def hosted(profile, **environment):
    return {"kind": "hosted", "image": "agent-r:other", "environment_variables": {"FA_NATIVE_TURNS": profile, **environment}}


def test_a_v3_selected_version_of_another_build_is_a_follow_target_and_a_v1_or_v2_one_is_not():
    """2d-ii drops the v1 ``fa_build`` branch: a v1 definition is refused even when it is this API's build; 3d-ii
    refuses v2 the same way."""
    foundry = Foundry(R)
    foundry.add(R.agent_name, 3, "other-build", definition=hosted(V1))
    foundry.add(R.agent_name, 4, "other-build", definition=hosted(V2))
    foundry.add(R.agent_name, 5, "this-build", definition=hosted(V1))
    resolver = HostedVersions(lambda endpoint: foundry, build="this-build")
    foundry.add(R.agent_name, 6, "other-build", definition=hosted(V3))
    for selected in ("3", "4", "5"):
        foundry.selectors[R.agent_name] = selected
        with pytest.raises(Problem) as refused:
            resolver.current(R)
        assert _code(refused) == (503, FOLLOWING)
    foundry.selectors[R.agent_name] = "6"
    assert resolver.current(R) == "6"


def test_a_v3_definition_still_needs_its_identity_and_active_status():
    foundry = Foundry(R)
    foundry.add(R.agent_name, 4, "other-build", definition=hosted(V3), guid="00000000-0000-0000-0000-000000000000")
    foundry.add(R.agent_name, 5, "other-build", definition=hosted(V3), status="failed")
    for selected in ("4", "5"):
        foundry.selectors[R.agent_name] = selected
        with pytest.raises(Problem) as refused:
            HostedVersions(lambda endpoint: foundry, build="this-build").current(R)
        assert _code(refused) == (503, FOLLOWING)


@pytest.mark.parametrize("profile, accepted", [(V1, False), (V2, False), (V3, True), ("foundry-native-v4", False),
                                               (None, False)])
def test_the_selected_version_must_declare_a_supported_profile(monkeypatch, profile, accepted):
    monkeypatch.setattr(behavior_native_sessions, "_native_profile", lambda _client: profile)
    if accepted:
        _require_native_version(object())
    else:
        with pytest.raises(Problem) as refused:
            _require_native_version(object())
        assert refused.value.body["error"]["code"] == "NATIVE_TURNS_DISABLED"


def test_sync_and_the_responses_lane_accept_a_v3_definition_only():
    assert versions._check_environment({"FA_NATIVE_TURNS": V3}, 900) == {"FA_NATIVE_TURNS": V3}
    for profile in (V2, "foundry-native-v4"):
        with pytest.raises(Problem):
            versions._check_environment({"FA_NATIVE_TURNS": profile}, 900)
    for profile, enabled in ((V3, True), (V2, False)):
        definition = hosted(profile, FA_RESPONSES_ENABLED="1")
        definition["protocol_versions"] = [{"protocol": "responses", "version": "2.0.0"}]
        assert versions._responses_enabled(definition) is enabled
    assert versions._carried_environment({"FA_NATIVE_TURNS": V3, "FA_NATIVE_GROUPS_ENABLED": "1"})[
        "FA_RESPONSES_ENABLED"] == "1"


def test_the_declared_profile_is_read_once_per_version_and_an_unreadable_one_is_not_kept():
    foundry = Foundry(R)
    foundry.add(R.agent_name, 4, "other-build", definition=hosted(V3))
    foundry.add(R.agent_name, 5, "this-build", definition={"kind": "hosted", "image": "agent-r:this"})
    resolver = HostedVersions(lambda endpoint: foundry, build="this-build")
    foundry.selectors[R.agent_name] = "4"
    assert resolver.current(R) == "4"
    reads = len(foundry.calls)
    assert resolver.profile(R, "4") == V3 and len(foundry.calls) == reads  # the selection read already holds it
    assert resolver.profile(R, "5") is None and resolver.profile(R, "5") is None
    assert foundry.calls[reads:] == [("get", R.agent_name, "5")]
    failing = HostedVersions(lambda endpoint: SimpleNamespace(get_version=lambda *_: (_ for _ in ()).throw(
        versions.FoundryCallError(503))), build="this-build")
    with pytest.raises(Problem) as unread:
        failing.profile(R, "4")
    assert _code(unread) == (503, FOLLOWING) and failing._profiles == {}


def row(profile):
    return NativeBindingRow(
        agent_id="agent-a", hosted_session_id="sid-a", created_agent_version="150", state_profile=profile,
        observed=NativeBindingObservation(agent_version="150", observed_at="2026-10-15T01:00:00+00:00", source="vm"))


@pytest.mark.parametrize("enforce", [False, True])
def test_a_v3_row_is_supported_and_the_guard_neither_logs_nor_refuses(monkeypatch, caplog, enforce):
    v3 = row(V3)
    assert v3.supported() and not row("foundry-native-v4").supported()
    assert not row(V1).supported() and not row(V2).supported()
    guard = SessionGuard(lambda: SimpleNamespace(read=lambda _agent_id: v3), enforce=enforce)
    # The source check before an abandon decision marks incompatible SIDs; a v3 row and definition are neither.
    monkeypatch.setattr(behavior_native_sessions, "_native_profile", lambda _client, **_: V3)
    source = SimpleNamespace(target=SimpleNamespace(agent_id="agent-a", session_id="sid-a"))
    with caplog.at_level(logging.WARNING):
        assert NativeSessions._source_supported(SimpleNamespace(guard=guard), source, v3)
        guard.check("agent-a", "sid-a", operation="invoke")
    assert guard.refused == set()
    assert "NATIVE_SESSION_CALL_INCOMPATIBLE" not in caplog.text


@pytest.mark.parametrize("declared,readable", [("1.0.99/1.0.199", False), ("1.0.26/1.0.103", True), (None, True)])
def test_a_version_running_a_release_this_api_cannot_read_decides_nothing(monkeypatch, declared, readable):
    """ADR-0009 §3 (c) Guard B on the session-side definition read: a pair outside this API's ``KNOWN_RELEASES`` is
    "not readable yet" (503), which every caller treats as deciding nothing, never as incompatible: an older API
    revision still serving during a deploy must not mark a newer image's SID incompatible for the newer API."""
    from test_native_session_prepare_api import project_client

    environment = {} if declared is None else {"FA_SDK_RELEASE": declared}
    project_client(monkeypatch, get_version=lambda name, version: SimpleNamespace(
        name=name, version=version, created_at=None, definition=hosted(V3, **environment)))
    client = SimpleNamespace(credential=None, target=SimpleNamespace(
        project_endpoint="https://project.example", agent_name="agent-r", agent_version="5"))
    if readable:
        assert behavior_native_sessions._native_profile(client) == V3
        return
    with pytest.raises(Problem) as unread:
        behavior_native_sessions._native_profile(client)
    assert unread.value.status == 503
