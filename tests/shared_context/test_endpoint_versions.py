"""Endpoint routing, not build-order heuristics, selects new Hosted sessions."""

import pytest

from shared_context.behavior_agent_versions import HostedVersions, route, sync_all
from shared_context.behavior_agent_onboarding import FoundryCallError
from shared_context.errors import Problem
from test_agent_versions import A, Clock, Foundry, R, _sync


@pytest.mark.parametrize("recorded", [False, True])
def test_sync_reuses_service_normalized_definition_without_changing_its_saved_hash(recorded):
    from shared_context.behavior_agent_versions import METADATA_DEFINITION, definition_sha256

    foundry = Foundry(R)
    desired = {"kind": "hosted", "image": "registry.invalid/hosted@sha256:" + "a" * 64}
    returned = {**desired, "container_protocol_versions": []}
    foundry.add(R.agent_name, 1, "new", definition=returned)
    if recorded:
        foundry.versions[R.agent_name][0]["metadata"][METADATA_DEFINITION] = definition_sha256(desired)
    original = foundry.get_version(R.agent_name, "1")
    result = _sync(foundry, R, "new", source=lambda: (desired, "seed:1"))
    assert (result["state"], result["version"]) == ("ok", "1")
    assert not foundry.creates() and not foundry.routes
    assert foundry.get_version(R.agent_name, "1") == original


def test_sync_reuses_the_seed_hash_when_the_service_default_is_carried_from_the_served_source():
    from shared_context.behavior_agent_versions import METADATA_DEFINITION, definition_sha256

    foundry = Foundry(R)
    desired = {"kind": "hosted", "image": "registry.invalid/hosted@sha256:" + "a" * 64}
    returned = {**desired, "container_protocol_versions": []}
    foundry.add(R.agent_name, 1, "new", definition=returned)
    foundry.versions[R.agent_name][0]["metadata"][METADATA_DEFINITION] = definition_sha256(desired)
    original = foundry.get_version(R.agent_name, "1")
    result = _sync(foundry, R, "new", source=lambda: (returned, "served:1"))
    assert (result["state"], result["version"]) == ("ok", "1")
    assert not foundry.creates() and not foundry.routes
    assert foundry.get_version(R.agent_name, "1") == original


@pytest.mark.parametrize("change", [
    {"image": "registry.invalid/changed@sha256:" + "b" * 64},
    {"container_protocol_versions": [{"protocol": "invocations", "version": "2.0.0"}]},
    {"unknown_service_field": []},
])
def test_sync_readback_rejects_meaningful_changes_even_with_the_expected_metadata(change):
    from shared_context.behavior_agent_versions import METADATA_DEFINITION, definition_sha256

    foundry = Foundry(R)
    desired = {"kind": "hosted", "image": "registry.invalid/hosted@sha256:" + "a" * 64}
    foundry.add(R.agent_name, 1, "new", definition={**desired, "container_protocol_versions": [], **change})
    foundry.versions[R.agent_name][0]["metadata"][METADATA_DEFINITION] = definition_sha256(desired)
    result = _sync(foundry, R, "new", source=lambda: (desired, "seed:1"))
    assert (result["state"], result["reason"]) == ("failed", "HOSTED_DEFINITION_READBACK_CONFLICT")
    assert not foundry.creates() and not foundry.routes


def test_sync_does_not_ignore_an_unrelated_recorded_hash_when_the_service_default_is_empty():
    from shared_context.behavior_agent_versions import METADATA_DEFINITION, definition_sha256

    foundry = Foundry(R)
    desired = {"kind": "hosted", "image": "registry.invalid/hosted@sha256:" + "a" * 64}
    returned = {**desired, "container_protocol_versions": []}
    foundry.add(R.agent_name, 1, "new", definition=returned)
    foundry.versions[R.agent_name][0]["metadata"][METADATA_DEFINITION] = definition_sha256({
        **desired, "image": "registry.invalid/another@sha256:" + "b" * 64})
    original = foundry.get_version(R.agent_name, "1")
    result = _sync(foundry, R, "new", source=lambda: (returned, "served:1"))
    assert (result["state"], result["version"]) == ("ok", "2")
    assert len(foundry.creates()) == 1
    assert foundry.get_version(R.agent_name, "1") == original


def test_sync_does_not_revalidate_an_unrelated_recorded_definition():
    from shared_context.behavior_agent_versions import METADATA_DEFINITION

    foundry = Foundry(R)
    foundry.add(R.agent_name, 1, "new", definition={"kind": "not-hosted", "container_protocol_versions": []})
    foundry.versions[R.agent_name][0]["metadata"][METADATA_DEFINITION] = "d" * 64
    result = _sync(foundry, R, "new")
    assert (result["state"], result["version"]) == ("ok", "2")
    assert len(foundry.creates()) == 1


@pytest.mark.parametrize("selector", ["5", "@latest"])
def test_selector_controls_the_version_and_is_read_again_after_publication(selector):
    foundry = Foundry(R)
    for version in (4, 5):  # from ADR-0009 2d-ii only a v2 definition is followed, whatever its build
        foundry.add(R.agent_name, version, "same-build", definition={
            "kind": "hosted", "image": f"{R.agent_name}:same-build",
            "environment_variables": {"FA_NATIVE_TURNS": "foundry-native-v2"}})
    route = {"version": selector, "latest": "5"}

    def get_agent(name):
        assert name == R.agent_name
        return {
            "name": name, "state": "enabled", "versions": {"latest": {"version": route["latest"]}},
            "agent_endpoint": {"protocol_configuration": {"invocations": {}}, "version_selector": {
                "version_selection_rules": [
                    {"type": "FixedRatio", "agent_version": route["version"], "traffic_percentage": 100}]}}}

    foundry.get_agent = get_agent
    resolver = HostedVersions(lambda endpoint: foundry, build="same-build")
    assert resolver.current(R) == "5"
    route.update(version="4", latest="5")
    assert resolver.current(R) == "4"
    assert not foundry.creates() and not foundry.routes


def test_changing_only_one_agents_flag_creates_one_immutable_definition_of_the_same_build():
    foundry = Foundry(R)
    original = {"kind": "hosted", "image": "registry.invalid/agent@sha256:" + "a" * 64,
                "environment_variables": {"FA_NATIVE_HITL_ENABLED": "0"}}
    desired = {**original, "environment_variables": {"FA_NATIVE_HITL_ENABLED": "1"}}
    foundry.add(R.agent_name, 4, "same-build", definition=original)
    source = lambda: (desired, "original-agent:4")

    changed = _sync(foundry, R, "same-build", source=source)

    assert changed["state"] == "ok" and changed["version"] == "5"
    assert foundry.versions[R.agent_name][0]["definition"] == original
    assert foundry.creates()[0][2] == desired
    assert len(foundry.creates()[0][3]["fa_definition_sha256"]) == 64
    assert _sync(foundry, R, "same-build", source=source) == changed
    assert len(foundry.creates()) == 1


def test_sync_preserves_each_agents_flags_and_applies_explicit_overlay_without_rewriting_targets(monkeypatch):
    from types import SimpleNamespace
    from shared_context import behavior_agent_versions as versions

    foundry = Foundry(A, R)
    for target, flags in ((A, {"FA_NATIVE_HITL_ENABLED": "1"}), (R, {"FA_NATIVE_GROUPS_ENABLED": "1"})):
        foundry.add(target.agent_name, 1, "old", definition={
            "kind": "hosted", "image": "old:image", "environment_variables": flags,
            "protocol_versions": [{"protocol": "invocations", "version": "2.0.0"}],
            "session_configuration": {"idle_timeout_seconds": 300}})
    monkeypatch.setattr(versions, "_agents", lambda _: (A, [(A, "configured"), (R, "registered")]))
    clock = Clock()
    before_a, before_r = A.model_dump_json(), R.model_dump_json()
    result = sync_all(
        SimpleNamespace(), lambda _: foundry, image="new:image", build="same-code",
        hosted_environment={R.agent_id: {"FA_NATIVE_HITL_ENABLED": "1", "FA_NATIVE_HITL_TTL_SECONDS": "600"}},
        sleep=clock.sleep, clock=clock)
    assert [item["state"] for item in result] == ["ok", "ok"]
    a = foundry.creates(A.agent_name)[0][2]
    r = foundry.creates(R.agent_name)[0][2]
    assert a["environment_variables"] == {"FA_NATIVE_HITL_ENABLED": "1"}
    assert r["environment_variables"] == {
        "FA_NATIVE_GROUPS_ENABLED": "1", "FA_NATIVE_HITL_ENABLED": "1", "FA_NATIVE_HITL_TTL_SECONDS": "600"}
    assert a["session_configuration"]["idle_timeout_seconds"] == r["session_configuration"]["idle_timeout_seconds"] == 900
    assert (A.model_dump_json(), R.model_dump_json()) == (before_a, before_r)


@pytest.mark.parametrize("lost", [False, True])
def test_publishing_reads_the_real_route_back_and_never_repeats_an_unknown_patch(lost):
    foundry = Foundry(R)
    state = {"version": "4"}
    calls = []

    def get_agent(name):
        return {"name": name, "agent_endpoint": {
            "protocol_configuration": {"invocations": {}, "responses": {}},
            "version_selector": {"version_selection_rules": [
                {"type": "FixedRatio", "agent_version": state["version"], "traffic_percentage": 100}]}}}

    def update(name, endpoint):
        calls.append(endpoint)
        state["version"] = endpoint["version_selector"]["version_selection_rules"][0]["agent_version"]
        if lost:
            raise FoundryCallError(None)

    foundry.get_agent, foundry.update_route = get_agent, update
    route(foundry, R.agent_name, "5")
    route(foundry, R.agent_name, "5")
    assert state["version"] == "5" and len(calls) == 1
    assert calls[0]["protocol_configuration"] == {"invocations": {}, "responses": {}}


def test_an_acknowledged_but_unconfirmed_route_is_not_reported_as_published():
    foundry = Foundry(R)
    foundry.update_route = lambda name, endpoint: foundry.routes.append((name, endpoint))
    with pytest.raises(Problem) as uncertain:
        route(foundry, R.agent_name, "5")
    assert uncertain.value.body["error"]["code"] == "ROUTE_UNKNOWN"


@pytest.mark.parametrize("invalid", ["other-agent", "credential", "invalid-flag"])
def test_invalid_deployment_overlay_is_rejected_before_any_agent_is_changed(monkeypatch, invalid):
    from types import SimpleNamespace
    from shared_context import behavior_agent_versions as versions

    foundry = Foundry(A, R)
    for target in (A, R):
        foundry.add(target.agent_name, 1, "old", definition={"kind": "hosted", "image": "old:image"})
    monkeypatch.setattr(versions, "_agents", lambda _: (A, [(A, "configured"), (R, "registered")]))
    overlay = ({
        "other-agent": {"not-a-served-agent": {"FA_NATIVE_HITL_ENABLED": "1"}},
        "credential": {R.agent_id: {"AZURE_CLIENT_SECRET": "synthetic-not-a-credential"}},
        "invalid-flag": {R.agent_id: {"FA_NATIVE_HITL_ENABLED": "maybe"}},
    })[invalid]
    clock = Clock()
    with pytest.raises(Problem) as rejected:
        sync_all(SimpleNamespace(), lambda _: foundry, image="new:image", build="new",
                 hosted_environment=overlay, sleep=clock.sleep, clock=clock)
    assert rejected.value.body["error"]["code"] == "HOSTED_ENVIRONMENT_INVALID"
    assert not foundry.creates() and not foundry.routes


def test_new_definition_inherits_the_agents_served_flags_not_an_unpublished_candidate():
    from shared_context.behavior_agent_versions import desired_source, image_source

    foundry = Foundry(R)
    original = {"kind": "hosted", "image": "old:image", "environment_variables": {"FA_NATIVE_HITL_ENABLED": "0"}}
    candidate = {**original, "environment_variables": {"FA_NATIVE_HITL_ENABLED": "1"}}
    foundry.add(R.agent_name, 4, "old", definition=original)
    foundry.add(R.agent_name, 5, "candidate", definition=candidate)
    foundry.selectors[R.agent_name] = "4"
    desired, label = desired_source(image_source(foundry, R, "next:image"))()
    assert desired["environment_variables"] == {"FA_NATIVE_HITL_ENABLED": "0"}
    assert label == "agent-r:4"
    assert foundry.versions[R.agent_name][1]["definition"] == candidate


NATIVE_GROUPS = {"FA_NATIVE_TURNS": "foundry-native-v1", "FA_NATIVE_GROUPS_ENABLED": "1"}


@pytest.mark.parametrize("served,expected", [
    # A "+" Agent cloned before its source had the Responses lane (stage 2026-10-04): groups need it.
    (NATIVE_GROUPS, {**NATIVE_GROUPS, "FA_RESPONSES_ENABLED": "1"}),
    ({**NATIVE_GROUPS, "FA_RESPONSES_ENABLED": "0"}, {**NATIVE_GROUPS, "FA_RESPONSES_ENABLED": "0"}),
    ({**NATIVE_GROUPS, "FA_NATIVE_GROUPS_ENABLED": "0"}, {**NATIVE_GROUPS, "FA_NATIVE_GROUPS_ENABLED": "0"}),
    ({"FA_NATIVE_GROUPS_ENABLED": "1"}, {"FA_NATIVE_GROUPS_ENABLED": "1"}),
    ({"FA_NATIVE_TURNS": "foundry-native-v1"}, {"FA_NATIVE_TURNS": "foundry-native-v1"}),
])
def test_registered_group_agent_gets_the_responses_lane_once_and_explicit_values_stay(monkeypatch, served, expected):
    from types import SimpleNamespace
    from shared_context import behavior_agent_versions as versions

    foundry = Foundry(A, R)
    for target in (A, R):
        foundry.add(target.agent_name, 1, "old", definition={
            "kind": "hosted", "image": "old:image", "environment_variables": dict(served),
            "session_configuration": {"idle_timeout_seconds": 900}})
    monkeypatch.setattr(versions, "_agents", lambda _: (A, [(A, "configured"), (R, "registered")]))
    clock = Clock()
    for _ in range(2):
        result = sync_all(SimpleNamespace(), lambda _: foundry, image="new:image", build="new",
                          sleep=clock.sleep, clock=clock)
        assert [item["state"] for item in result] == ["ok", "ok"]
    assert len(foundry.creates(R.agent_name)) == 1
    assert foundry.creates(R.agent_name)[0][2]["environment_variables"] == expected


@pytest.mark.parametrize("responses,expected", [(None, "1"), ("0", "0"), ("1", "1")])
def test_onboarding_copy_of_a_group_source_carries_the_responses_lane(monkeypatch, responses, expected):
    from types import SimpleNamespace
    from shared_context import behavior_agent_versions as versions
    from shared_context import behavior_work_target

    served = {**NATIVE_GROUPS, "FA_N6_RESPONSES_PROBE": "fa-foundry-responses-n6-v1"}
    if responses is not None:
        served["FA_RESPONSES_ENABLED"] = responses
    foundry = Foundry(A)
    foundry.add(A.agent_name, 9, "new", definition={"kind": "hosted", "environment_variables": served})
    monkeypatch.setattr(behavior_work_target, "selected_targets", lambda store, config: [A])
    monkeypatch.setattr(versions, "current_hosted_version", lambda runtime, target: "9")
    composition = SimpleNamespace(runtime=SimpleNamespace(store=object()), config=object())
    for definition, _label in (versions.original_source(composition, foundry), versions.copy_source(foundry, A, "new")()):
        assert definition["environment_variables"] == {**NATIVE_GROUPS, "FA_RESPONSES_ENABLED": expected}


@pytest.mark.parametrize("served,overlay,expected", [
    # Tracing is on when FA_SDK_TELEMETRY is unset (Neal 2026-10-05): neither sync nor onboarding adds the key.
    ({"FA_NATIVE_TURNS": "foundry-native-v1"}, None, {"FA_NATIVE_TURNS": "foundry-native-v1"}),
    # An explicit off stays off on later builds and "+" copies of this source.
    ({"FA_SDK_TELEMETRY": "0"}, None, {"FA_SDK_TELEMETRY": "0"}),
    # The deployment overlay can turn it off (or back on) like the other switches.
    ({}, {"FA_SDK_TELEMETRY": "0"}, {"FA_SDK_TELEMETRY": "0"}),
    ({"FA_SDK_TELEMETRY": "0"}, {"FA_SDK_TELEMETRY": "1"}, {"FA_SDK_TELEMETRY": "1"}),
])
def test_tracing_default_is_never_overwritten_by_sync_or_onboarding(monkeypatch, served, overlay, expected):
    from types import SimpleNamespace
    from shared_context import behavior_agent_versions as versions
    from shared_context import behavior_work_target

    foundry = Foundry(A, R)
    for target in (A, R):
        foundry.add(target.agent_name, 1, "old", definition={
            "kind": "hosted", "image": "old:image", "environment_variables": dict(served),
            "session_configuration": {"idle_timeout_seconds": 900}})
    monkeypatch.setattr(versions, "_agents", lambda _: (A, [(A, "configured"), (R, "registered")]))
    clock = Clock()
    sync_all(SimpleNamespace(), lambda _: foundry, image="new:image", build="new", sleep=clock.sleep, clock=clock,
             hosted_environment=None if overlay is None else {R.agent_id: overlay})
    assert foundry.creates(R.agent_name)[0][2]["environment_variables"] == expected
    monkeypatch.setattr(behavior_work_target, "selected_targets", lambda store, config: [R])
    monkeypatch.setattr(versions, "current_hosted_version", lambda runtime, target: "2")
    composition = SimpleNamespace(runtime=SimpleNamespace(store=object()), config=object())
    copied, _label = versions.original_source(composition, foundry)
    assert copied["environment_variables"] == expected


def test_tracing_overlay_accepts_only_zero_or_one(monkeypatch):
    from types import SimpleNamespace
    from shared_context import behavior_agent_versions as versions

    foundry = Foundry(A, R)
    for target in (A, R):
        foundry.add(target.agent_name, 1, "old", definition={"kind": "hosted", "image": "old:image"})
    monkeypatch.setattr(versions, "_agents", lambda _: (A, [(A, "configured"), (R, "registered")]))
    clock = Clock()
    with pytest.raises(Problem) as rejected:
        sync_all(SimpleNamespace(), lambda _: foundry, image="new:image", build="new", sleep=clock.sleep, clock=clock,
                 hosted_environment={R.agent_id: {"FA_SDK_TELEMETRY": "true"}})
    assert rejected.value.body["error"]["code"] == "HOSTED_ENVIRONMENT_INVALID"
    assert not foundry.creates() and not foundry.routes

def test_publication_freezes_latest_before_creating_the_candidate(monkeypatch):
    from types import SimpleNamespace
    from shared_context import behavior_agent_versions as versions

    foundry = Foundry(R)
    foundry.add(R.agent_name, 4, "old", definition={"kind": "hosted", "image": "old:image"})
    monkeypatch.setattr(versions, "_agents", lambda _: (R, [(R, "configured")]))
    clock = Clock()
    result = sync_all(SimpleNamespace(), lambda _: foundry, image="new:image", build="new", set_route=True,
                      sleep=clock.sleep, clock=clock)
    assert result[0]["state"] == result[0]["route"] == "ok"
    actions = [call[0] for call in foundry.calls if call[0] in {"create", "route"}]
    assert actions == ["route", "create", "route"]
    assert foundry.routes[0][1]["version_selector"]["version_selection_rules"][0]["agent_version"] == "4"
    assert foundry.selectors[R.agent_name] == "5"


def test_identical_historical_definition_does_not_switch_away_from_the_served_version(monkeypatch):
    from types import SimpleNamespace
    from shared_context import behavior_agent_versions as versions

    foundry = Foundry(R)
    desired = {"kind": "hosted", "image": "same:image", "environment_variables": {},
               "session_configuration": {"idle_timeout_seconds": 900}}
    foundry.add(R.agent_name, 4, "same", definition=desired)
    foundry.add(R.agent_name, 5, "same", definition=desired)
    foundry.selectors[R.agent_name] = "5"
    monkeypatch.setattr(versions, "_agents", lambda _: (R, [(R, "configured")]))
    clock = Clock()
    result = sync_all(SimpleNamespace(), lambda _: foundry, image="same:image", build="same", set_route=True,
                      sleep=clock.sleep, clock=clock)
    assert result[0]["version"] == "5"
    assert not foundry.creates() and not foundry.routes
