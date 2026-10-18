"""ADR-0009 M1 2d-i (P3): the API accepts Hosted versions whose definition declares foundry-native-v2.

The supported set is {v1, v2}; the API itself still sends v1 until 2d-ii. Offline doubles only."""

import logging
from types import SimpleNamespace

import pytest

import shared_context.behavior_agent_versions as versions
from shared_context import behavior_native_sessions
from shared_context.behavior_agent_versions import FOLLOWING, HostedVersions
from shared_context.behavior_native_sessions import NativeSessions, _require_native_version
from shared_context.errors import Problem
from shared_context.native_binding import NativeBindingObservation, NativeBindingRow, SessionGuard
from shared_context.native_responses_wire import NATIVE_STATE_PROFILE, NATIVE_V2_PROFILE
from test_agent_versions import Foundry, R, _code

V1, V2 = NATIVE_STATE_PROFILE, NATIVE_V2_PROFILE


def hosted(profile, **environment):
    return {"kind": "hosted", "image": "agent-r:other", "environment_variables": {"FA_NATIVE_TURNS": profile, **environment}}


def test_a_v2_selected_version_of_another_build_is_a_follow_target_and_a_v1_one_is_not():
    foundry = Foundry(R)
    foundry.add(R.agent_name, 3, "other-build", definition=hosted(V1))
    foundry.add(R.agent_name, 4, "other-build", definition=hosted(V2))
    resolver = HostedVersions(lambda endpoint: foundry, build="this-build")
    foundry.selectors[R.agent_name] = "3"
    with pytest.raises(Problem) as refused:
        resolver.current(R)
    assert _code(refused) == (503, FOLLOWING)
    foundry.selectors[R.agent_name] = "4"
    assert resolver.current(R) == "4"


def test_a_v2_definition_still_needs_its_identity_and_active_status():
    foundry = Foundry(R)
    foundry.add(R.agent_name, 4, "other-build", definition=hosted(V2), guid="00000000-0000-0000-0000-000000000000")
    foundry.add(R.agent_name, 5, "other-build", definition=hosted(V2), status="failed")
    for selected in ("4", "5"):
        foundry.selectors[R.agent_name] = selected
        with pytest.raises(Problem) as refused:
            HostedVersions(lambda endpoint: foundry, build="this-build").current(R)
        assert _code(refused) == (503, FOLLOWING)


@pytest.mark.parametrize("profile, accepted", [(V1, True), (V2, True), ("foundry-native-v3", False), (None, False)])
def test_the_selected_version_must_declare_a_supported_profile(monkeypatch, profile, accepted):
    monkeypatch.setattr(behavior_native_sessions, "_native_profile", lambda _client: profile)
    if accepted:
        _require_native_version(object())
    else:
        with pytest.raises(Problem) as refused:
            _require_native_version(object())
        assert refused.value.body["error"]["code"] == "NATIVE_TURNS_DISABLED"


def test_sync_and_the_responses_lane_accept_a_v2_definition():
    assert versions._check_environment({"FA_NATIVE_TURNS": V2}, 900) == {"FA_NATIVE_TURNS": V2}
    with pytest.raises(Problem):
        versions._check_environment({"FA_NATIVE_TURNS": "foundry-native-v3"}, 900)
    definition = hosted(V2, FA_RESPONSES_ENABLED="1")
    definition["protocol_versions"] = [{"protocol": "responses", "version": "2.0.0"}]
    assert versions._responses_enabled(definition)
    assert versions._carried_environment({"FA_NATIVE_TURNS": V2, "FA_NATIVE_GROUPS_ENABLED": "1"})[
        "FA_RESPONSES_ENABLED"] == "1"


def test_the_declared_profile_is_read_once_per_version_and_an_unreadable_one_is_not_kept():
    foundry = Foundry(R)
    foundry.add(R.agent_name, 4, "other-build", definition=hosted(V2))
    foundry.add(R.agent_name, 5, "this-build", definition={"kind": "hosted", "image": "agent-r:this"})
    resolver = HostedVersions(lambda endpoint: foundry, build="this-build")
    foundry.selectors[R.agent_name] = "4"
    assert resolver.current(R) == "4"
    reads = len(foundry.calls)
    assert resolver.profile(R, "4") == V2 and len(foundry.calls) == reads  # the selection read already holds it
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
def test_a_v2_row_is_supported_and_the_guard_neither_logs_nor_refuses(monkeypatch, caplog, enforce):
    v2 = row(V2)
    assert v2.supported() and not row("foundry-native-v3").supported()
    guard = SessionGuard(lambda: SimpleNamespace(read=lambda _agent_id: v2), enforce=enforce)
    # The source check before an abandon decision marks incompatible SIDs; a v2 row and definition are neither.
    monkeypatch.setattr(behavior_native_sessions, "_native_profile", lambda _client, **_: V2)
    source = SimpleNamespace(target=SimpleNamespace(agent_id="agent-a", session_id="sid-a"))
    with caplog.at_level(logging.WARNING):
        assert NativeSessions._source_supported(SimpleNamespace(guard=guard), source, v2)
        guard.check("agent-a", "sid-a", operation="invoke")
    assert guard.refused == set()
    assert "NATIVE_SESSION_CALL_INCOMPATIBLE" not in caplog.text
