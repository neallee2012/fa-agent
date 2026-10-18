"""Endpoint-selected versions and immutable desired definitions replace PR7 build-order selection."""

import copy
import json
from types import SimpleNamespace

import pytest

import shared_context.behavior_agent_versions as versions
from shared_context.behavior_agent_message_api import public_agents
from shared_context.behavior_agent_onboarding import FoundryCallError
from shared_context.behavior_agent_versions import (
    FOLLOWING, HostedVersions, SelectorUnreadable, sync_agent_version, sync_all,
)
from shared_context.behavior_work_target import instruction_hosted_version
from shared_context.errors import Problem
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_team_registry import AGENT_OID, OTHER_AGENT_OID, _target

REAL_CURRENT = versions.current_hosted_version
A = _target(1, AGENT_OID, agent_name="agent-a")
R = _target(2, OTHER_AGENT_OID, agent_name="agent-r")


class Foundry:
    """Versions keyed by agent name; one guid/identity per name. ``creating`` turns ``final`` after ``ready_after`` lists."""

    def __init__(self, *targets, ready_after=1, final="active"):
        self.identity = {t.agent_name: (t.agent_id.upper(), t.agent_oid) for t in targets}
        self.versions = {t.agent_name: [] for t in targets}
        self.ready_after, self.final, self.calls, self.fail_create, self.fail_list = ready_after, final, [], [], []
        self.routes, self.get_lag = [], {}
        self.selectors = {target.agent_name: "@latest" for target in targets}

    def add(self, name, version, build, status="active", definition=None, guid=None):
        own_guid, principal = self.identity[name]
        self.versions[name].append({
            "name": name, "version": str(version), "status": status, "metadata": {"fa_build": build},
            "definition": definition or {"kind": "hosted", "image": f"{name}:{build}",
                                         "environment_variables": {"KEEP": "1"}},
            "agent_guid": guid or own_guid, "instance_identity": {"principal_id": principal}})

    def list_versions(self, name):
        self.calls.append(("list", name))
        if self.fail_list:
            raise self.fail_list.pop(0)
        for item in self.versions.get(name, []):
            if item["status"] == "creating":
                item["_reads"] = item.get("_reads", 0) + 1
                if item["_reads"] > self.ready_after:
                    item["status"] = self.final
        return [{k: v for k, v in item.items() if k != "_reads"} for item in self.versions.get(name, [])]

    def get_version(self, name, version):
        self.calls.append(("get", name, version))
        item = json.loads(json.dumps(next(item for item in self.versions[name] if item["version"] == version)))
        if self.get_lag.get((name, version)):
            self.get_lag[(name, version)] -= 1
            item["status"] = "creating"
        return item

    def create_version(self, name, *, definition, metadata):
        self.calls.append(("create", name, json.loads(json.dumps(definition)), dict(metadata)))
        mode = self.fail_create.pop(0) if self.fail_create else None
        if isinstance(mode, int):
            raise FoundryCallError(mode)
        items = self.versions[name]
        number = 1 + max((int(item["version"]) for item in items), default=0)
        self.add(name, number, metadata["fa_build"], status="creating", definition=definition)
        items[-1]["metadata"] = dict(metadata)
        if mode == "unknown-after-commit":
            raise FoundryCallError(None)
        return dict(items[-1])

    def get_agent(self, name):
        self.calls.append(("agent", name))
        latest = max(self.versions[name], key=lambda item: int(item["version"]), default={})
        return {"name": name, "state": "enabled", "versions": {"latest": latest},
                "agent_endpoint": {"protocol_configuration": {"protocol": "responses"},
                                   "version_selector": {"version_selection_rules": [{
                                       "type": "FixedRatio", "agent_version": self.selectors[name],
                                       "traffic_percentage": 100}]}}}

    def update_route(self, name, endpoint):
        self.routes.append((name, endpoint))
        self.calls.append(("route", name))
        self.selectors[name] = endpoint["version_selector"]["version_selection_rules"][0]["agent_version"]

    def creates(self, name=None):
        return [call for call in self.calls if call[0] == "create" and name in (None, call[1])]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _sync(foundry, target, build, source=None, **kw):
    clock = Clock()
    source = source or (lambda: ({"kind": "hosted", "image": f"{target.agent_name}:{build}",
                                "environment_variables": {"KEEP": "1"}}, "src:1"))
    return sync_agent_version(foundry, target, build=build, source=source, sleep=clock.sleep, clock=clock, **kw)


def _code(raised):
    return raised.value.status, raised.value.body["error"]["code"]


def test_a_new_build_gets_its_own_version_and_the_selection_follows_it():
    foundry = Foundry(R)
    foundry.add(R.agent_name, 3, "old")
    new = HostedVersions(lambda endpoint: foundry, build="new")
    with pytest.raises(Problem) as following:
        new.current(R)
    assert _code(following) == (503, FOLLOWING)
    result = _sync(foundry, R, "new")
    assert (result["state"], result["version"]) == ("ok", "4")
    created = foundry.creates()
    assert len(created) == 1
    assert {key: created[0][3][key] for key in ("fa_build", "fa_source")} == {"fa_build": "new", "fa_source": "src:1"}
    assert len(created[0][3]["fa_definition_sha256"]) == 64
    assert new.current(R) == "4"
    foundry.selectors[R.agent_name] = "3"
    assert HostedVersions(lambda endpoint: foundry, build="old").current(R) == "3"
    assert _sync(foundry, R, "new") == result and len(foundry.creates()) == 1


def test_identical_definitions_are_reused_but_only_the_selector_controls_served_version():
    foundry = Foundry(R)
    foundry.add(R.agent_name, 5, "new")
    foundry.add(R.agent_name, 4, "new")
    foundry.add(R.agent_name, 6, "new", status="failed")
    assert _sync(foundry, R, "new")["version"] == "4"
    foundry.selectors[R.agent_name] = "5"
    assert HostedVersions(lambda endpoint: foundry, build="new").current(R) == "5"
    foundry.selectors[R.agent_name] = "4"
    assert HostedVersions(lambda endpoint: foundry, build="new").current(R) == "4"
    assert foundry.creates() == []


def test_a_creating_candidate_does_not_block_a_different_published_version():
    foundry = Foundry(R, ready_after=3)
    foundry.add(R.agent_name, 4, "new", status="creating")
    foundry.add(R.agent_name, 5, "new")
    early = HostedVersions(lambda endpoint: foundry, build="new")
    foundry.selectors[R.agent_name] = "5"
    assert early.current(R) == "5"
    assert _sync(foundry, R, "new")["version"] == "4" and foundry.creates() == []
    assert early.current(R) == "5"


def test_a_version_still_creating_is_awaited_not_created_again():
    foundry = Foundry(R, ready_after=2)
    foundry.add(R.agent_name, 4, "new", status="creating")
    assert _sync(foundry, R, "new")["version"] == "4" and foundry.creates() == []


def test_unknown_post_is_never_repeated_and_the_next_run_lists_first():
    foundry = Foundry(R)
    foundry.fail_create.append("unknown-after-commit")
    first = _sync(foundry, R, "new")
    assert (first["state"], first["version"]) == ("ok", "1")
    assert len(foundry.creates()) == 1
    foundry.calls.clear()
    second = _sync(foundry, R, "new")
    assert (second["state"], second["version"]) == ("ok", "1")
    assert foundry.calls[0] == ("list", R.agent_name) and foundry.creates() == []

    lost = Foundry(R)
    lost.fail_create.append(503)
    assert _sync(lost, R, "new")["reason"] == "VERSION_NOT_ACTIVE_YET"
    assert len(lost.creates()) == 1
    lost.calls.clear()
    assert _sync(lost, R, "new")["version"] == "1"
    assert lost.calls[0] == ("list", R.agent_name) and len(lost.creates()) == 1

    retried = Foundry(R)
    retried.add(R.agent_name, 5, "new", status="failed")
    retried.fail_create.append(503)
    assert _sync(retried, R, "new")["reason"] == "VERSION_NOT_ACTIVE_YET"
    assert len(retried.creates()) == 1


def test_explicit_failures_and_unreadable_lists_are_reported_not_retried():
    rejected = Foundry(R)
    rejected.fail_create.append(400)
    assert _sync(rejected, R, "new")["reason"] == "FOUNDRY_CREATE_REJECTED"
    failed = Foundry(R, final="failed")
    assert _sync(failed, R, "new")["reason"] == "FOUNDRY_VERSION_FAILED" and len(failed.creates()) == 1
    unreadable = Foundry(R)
    unreadable.fail_list.append(FoundryCallError(500))
    assert _sync(unreadable, R, "new")["reason"] == "SOURCE_READ_UNKNOWN" and unreadable.creates() == []


def test_another_identity_under_the_name_is_never_selected():
    foundry = Foundry(R)
    foundry.add(R.agent_name, 7, "new", guid="99999999-9999-9999-9999-999999999999")
    assert _sync(foundry, R, "new")["reason"] == "AGENT_VERSION_IDENTITY_MISMATCH" and foundry.creates() == []
    with pytest.raises(Problem) as following:
        HostedVersions(lambda endpoint: foundry, build="new").current(R)
    assert _code(following) == (503, FOLLOWING)


def test_a_listed_active_version_is_not_used_by_the_api_until_get_says_active():
    foundry = Foundry(R)
    foundry.add(R.agent_name, 4, "new")
    foundry.get_lag[(R.agent_name, "4")] = 2
    resolver = HostedVersions(lambda endpoint: foundry, build="new")
    for _ in range(2):
        with pytest.raises(Problem) as following:
            resolver.current(R)
        assert _code(following) == (503, FOLLOWING)
    assert resolver.current(R) == "4"
    foundry.calls.clear()
    assert resolver.current(R) == "4" and foundry.calls == [("agent", R.agent_name), ("get", R.agent_name, "4")]


def test_responses_lane_rechecks_the_published_definition_without_a_write():
    foundry = Foundry(R)
    foundry.add(R.agent_name, 4, "new", definition={
        "kind": "hosted",
        "protocol_versions": [{"protocol": "invocations", "version": "2.0.0"},
                              {"protocol": "responses", "version": "2.0.0"}],
        "environment_variables": {"FA_NATIVE_TURNS": "foundry-native-v1", "FA_RESPONSES_ENABLED": "1"},
    })
    resolver = HostedVersions(lambda endpoint: foundry, build="new")
    assert resolver.current(R) == "4"
    assert resolver.current(R, require_responses=True) == "4"
    assert resolver.current(R, require_responses=True) == "4"
    assert foundry.calls == [("agent", R.agent_name), ("get", R.agent_name, "4")] * 3
    assert not foundry.creates() and not foundry.routes


@pytest.mark.parametrize("flag,expected", [("1", True), ("0", False), (None, False), (True, False)])
def test_the_selected_definition_reports_model_switching_without_another_read(flag, expected):
    from shared_context.behavior_agent_template import TemplateResponsesOptions

    foundry = Foundry(R)
    environment = {"FA_NATIVE_TURNS": "foundry-native-v1", "FA_RESPONSES_ENABLED": "1",
                   **({} if flag is None else {"FA_NATIVE_MODEL_SWITCH": flag})}
    foundry.add(R.agent_name, 4, "new", definition={
        "kind": "hosted", "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}],
        "environment_variables": environment})
    resolver = HostedVersions(lambda endpoint: foundry, build="new")
    runtime = SimpleNamespace(agent_message_composition=SimpleNamespace(hosted_versions=resolver))
    assert versions.hosted_model_switch(runtime, R) is False, "nothing read yet"
    assert resolver.current(R, require_responses=True) == "4"
    calls = len(foundry.calls)
    assert versions.hosted_model_switch(runtime, R) is expected
    assert len(foundry.calls) == calls
    # Display only, and omitted when off so the options bytes stay as before.
    shown = TemplateResponsesOptions(enabled=True, model_switch=expected).model_dump(mode="json")
    assert ("model_switch" in shown) is expected


@pytest.mark.parametrize("protocols,environment", [
    ([], {"FA_RESPONSES_ENABLED": "1"}),
    ([{"protocol": "invocations", "version": "2.0.0"}], {"FA_RESPONSES_ENABLED": "1"}),
    ([{"protocol": "responses", "version": "1.0.0"}], {"FA_RESPONSES_ENABLED": "1"}),
    ([{"protocol": "responses", "version": "2.0.0"}], {}),
    ([{"protocol": "responses", "version": "2.0.0"}], {"FA_RESPONSES_ENABLED": "0"}),
    ([{"protocol": "responses", "version": "2.0.0"}], {"FA_RESPONSES_ENABLED": True}),
    ([{"protocol": "responses", "version": "2.0.0"}], {"FA_N6_PROBE": "1"}),
    ([{"protocol": "responses", "version": "2.0.0"}], {"FA_RESPONSES_PROBE": "1"}),
])
def test_responses_requires_both_real_enabling_conditions_on_the_selected_version(protocols, environment):
    foundry = Foundry(R)
    foundry.add(R.agent_name, 4, "new", definition={
        "kind": "hosted", "protocol_versions": protocols,
        "environment_variables": {"FA_NATIVE_TURNS": "foundry-native-v1", **environment},
    })
    # Neither a later matching build nor another build may silently replace the chosen version.
    enabled = {"kind": "hosted", "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}],
               "environment_variables": {"FA_NATIVE_TURNS": "foundry-native-v1", "FA_RESPONSES_ENABLED": "1"}}
    foundry.add(R.agent_name, 5, "new", definition=enabled)
    foundry.add(R.agent_name, 6, "other-build", definition=enabled)
    foundry.selectors[R.agent_name] = "4"
    resolver = HostedVersions(lambda endpoint: foundry, build="new")
    for _ in range(2):
        with pytest.raises(Problem) as disabled:
            resolver.current(R, require_responses=True)
        assert _code(disabled) == (409, "NATIVE_TURNS_DISABLED")
    assert resolver.current(R) == "4"
    assert foundry.calls == [("agent", R.agent_name), ("get", R.agent_name, "4")] * 3
    assert not foundry.creates() and not foundry.routes


def test_responses_gate_checks_the_definition_returned_by_the_existing_active_get(monkeypatch):
    foundry = Foundry(R)
    foundry.add(R.agent_name, 4, "new", definition={
        "kind": "hosted", "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}],
        "environment_variables": {"FA_NATIVE_TURNS": "foundry-native-v1", "FA_RESPONSES_ENABLED": "1"},
    })
    get = foundry.get_version

    def actual(name, version):
        value = get(name, version)
        value["definition"]["environment_variables"] = {"FA_NATIVE_TURNS": "foundry-native-v1", "FA_N6_PROBE": "1"}
        return value

    monkeypatch.setattr(foundry, "get_version", actual)
    with pytest.raises(Problem) as disabled:
        HostedVersions(lambda endpoint: foundry, build="new").current(R, require_responses=True)
    assert _code(disabled) == (409, "NATIVE_TURNS_DISABLED")
    assert foundry.calls == [("agent", R.agent_name), ("get", R.agent_name, "4")]


def test_responses_gate_has_no_unverified_configured_version_fallback():
    runtime = SimpleNamespace()
    assert REAL_CURRENT(runtime, R) == R.agent_version
    with pytest.raises(Problem) as disabled:
        REAL_CURRENT(runtime, R, require_responses=True)
    assert _code(disabled) == (503, "NATIVE_TURNS_DISABLED")


@pytest.mark.parametrize("native_profile", [None, "foundry-responses-v1", "0"])
def test_responses_flag_cannot_implicitly_enable_the_native_runtime(native_profile):
    foundry = Foundry(R)
    environment = {"FA_RESPONSES_ENABLED": "1"}
    if native_profile is not None:
        environment["FA_NATIVE_TURNS"] = native_profile
    foundry.add(R.agent_name, 4, "new", definition={
        "kind": "hosted", "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}],
        "environment_variables": environment,
    })
    with pytest.raises(Problem) as disabled:
        HostedVersions(lambda endpoint: foundry, build="new").current(R, require_responses=True)
    assert _code(disabled) == (409, "NATIVE_TURNS_DISABLED")
    assert foundry.calls == [("agent", R.agent_name), ("get", R.agent_name, "4")]


@pytest.mark.parametrize("probe", ["FA_N6_RESPONSES_PROBE", "FA_N6_RESPONSES_INTERRUPT_SESSION_ID"])
def test_probe_version_is_not_a_production_lane_even_with_both_runtime_flags(probe):
    foundry = Foundry(R)
    foundry.add(R.agent_name, 4, "new", definition={
        "kind": "hosted", "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}],
        "environment_variables": {
            "FA_NATIVE_TURNS": "foundry-native-v1", "FA_RESPONSES_ENABLED": "1", probe: "probe-only",
        },
    })
    resolver = HostedVersions(lambda endpoint: foundry, build="new")
    with pytest.raises(Problem) as disabled:
        resolver.current(R, require_responses=True)
    assert _code(disabled) == (409, "NATIVE_TURNS_DISABLED")
    assert resolver.current(R) == "4" and foundry.calls == [("agent", R.agent_name), ("get", R.agent_name, "4")] * 2


def test_a_versions_fa_build_is_read_once_with_its_profile_and_only_as_a_code_identity():
    """ADR-0009 2d-i (2026-10-17): the SDK start pin of a capture sent to a version is that version's fa_build."""
    core = "0" * 64
    foundry = Foundry(R)
    foundry.add(R.agent_name, 41, core)
    foundry.add(R.agent_name, 42, "not-a-code-identity")
    resolver = HostedVersions(lambda endpoint: foundry, build="new")
    assert resolver.build_of(R, "41") == core and resolver.build_of(R, "41") == core
    assert resolver.build_of(R, "42") is None
    assert [call for call in foundry.calls if call[0] == "get"] == [("get", R.agent_name, "41"), ("get", R.agent_name, "42")]

    def unreadable(*_args):
        raise FoundryCallError(503)

    # An unreadable definition gives no build (the API keeps its own pin) and is never cached.
    assert HostedVersions(lambda endpoint: SimpleNamespace(get_version=unreadable)).build_of(R, "41") is None


@pytest.mark.parametrize("changed", [
    {"metadata": {"fa_build": "another-build"}}, {"name": "another-agent"}, {"version": "5"},
    {"agent_guid": "99999999-9999-9999-9999-999999999999"},
    {"instance_identity": {"principal_id": "99999999-9999-9999-9999-999999999999"}},
])
def test_responses_eligibility_keeps_the_selected_get_build_and_identity(monkeypatch, changed):
    foundry = Foundry(R)
    foundry.add(R.agent_name, 4, "new", definition={
        "kind": "hosted", "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}],
        "environment_variables": {"FA_NATIVE_TURNS": "foundry-native-v1", "FA_RESPONSES_ENABLED": "1"},
    })
    get = foundry.get_version
    monkeypatch.setattr(foundry, "get_version", lambda name, version: {**get(name, version), **changed})
    with pytest.raises(Problem) as disabled:
        HostedVersions(lambda endpoint: foundry, build="new").current(R, require_responses=True)
    assert _code(disabled) == (503, FOLLOWING)
    assert foundry.calls == [("agent", R.agent_name), ("get", R.agent_name, "4")]


def test_responses_endpoint_read_denial_stops_without_creating_or_rerouting():
    foundry = Foundry(R)
    def denied(_name):
        foundry.calls.append(("agent", R.agent_name))
        raise FoundryCallError(403)
    foundry.get_agent = denied
    with pytest.raises(Problem) as denied:
        HostedVersions(lambda endpoint: foundry, build="new").current(R, require_responses=True)
    assert _code(denied) == (403, "NATIVE_TURNS_DISABLED")
    assert foundry.calls == [("agent", R.agent_name)]
    assert not foundry.creates() and not foundry.routes


def test_sync_reports_ok_only_after_get_says_active_and_never_creates_meanwhile():
    foundry = Foundry(R)
    foundry.add(R.agent_name, 4, "new")
    foundry.get_lag[(R.agent_name, "4")] = 3
    result = _sync(foundry, R, "new")
    assert (result["state"], result["version"]) == ("ok", "4") and foundry.creates() == []
    assert [call for call in foundry.calls if call[0] == "get"] == [("get", R.agent_name, "4")] * 4

    slow = Foundry(R)
    slow.add(R.agent_name, 4, "new")
    slow.get_lag[(R.agent_name, "4")] = 10 ** 6
    assert _sync(slow, R, "new", wait_seconds=30)["reason"] == "VERSION_NOT_ACTIVE_YET" and slow.creates() == []


def test_the_version_resolver_reads_the_selector_on_every_call():
    # Environment operations need the live selection; only the API's reader binding is kept (native sessions).
    foundry = Foundry(R)
    resolver = HostedVersions(lambda endpoint: foundry, build="new")
    for _ in range(2):
        with pytest.raises(Problem):
            resolver.current(R)
    assert len(foundry.calls) == 2
    foundry.add(R.agent_name, 2, "new")
    assert resolver.current(R) == "2"
    foundry.versions[R.agent_name].clear()
    with pytest.raises(Problem) as unknown:
        resolver.current(R)
    assert _code(unknown) == (503, FOLLOWING)


@pytest.mark.parametrize("responses", [None, "0", "1"])
def test_sources_copy_a_build_version_with_only_the_original_formal_flag(monkeypatch, responses):
    from shared_context import behavior_work_target

    foundry = Foundry(A, R)
    foundry.add(A.agent_name, 9, "new")
    foundry.versions[A.agent_name][0]["definition"]["environment_variables"]["FA_N0_STATE_PROBE"] = (
        "fa-foundry-native-n0-v1")
    foundry.versions[A.agent_name][0]["definition"]["environment_variables"].update({
        "FA_MODEL_DIRECT_PROBE_URL": "https://example.invalid/probe", "FA_MODEL_DIRECT_PROBE_MODEL": "probe-model",
        "FA_N6_RESPONSES_PROBE": "fa-foundry-responses-n6-v1",
        "FA_N6_RESPONSES_INTERRUPT_SESSION_ID": "n6-synthetic-session",
    })
    expected = {"KEEP": "1"}
    if responses is not None:
        foundry.versions[A.agent_name][0]["definition"]["environment_variables"]["FA_RESPONSES_ENABLED"] = responses
        expected["FA_RESPONSES_ENABLED"] = responses
    definition, label = versions.copy_source(foundry, A, "new")()
    assert label == "agent-a:9" and definition["environment_variables"] == expected
    monkeypatch.setattr(behavior_work_target, "selected_targets", lambda store, config: [A])
    monkeypatch.setattr(versions, "current_hosted_version", lambda runtime, target: "9")
    composition = SimpleNamespace(runtime=SimpleNamespace(store=object()), config=object())
    definition, label = versions.original_source(composition, foundry)
    assert label == "agent-a:9" and definition["environment_variables"] == expected
    with pytest.raises(Problem) as missing:
        versions.copy_source(foundry, A, "other")()
    assert _code(missing) == (409, "SOURCE_NOT_SYNCED")
    foundry.add(A.agent_name, 10, "old", definition={"container_configuration": {"image": "a:old"}, "kind": "hosted"})
    definition, label = versions.image_source(foundry, A, "a:new")()
    assert (definition["container_configuration"]["image"], label) == ("a:new", "agent-a:10")
    with pytest.raises(Problem) as no_image:
        versions.image_source(foundry, A, None)()
    assert _code(no_image) == (400, "IMAGE_REQUIRED")


def test_configured_agents_declare_ws_and_responses_once_and_copies_inherit_them():
    foundry = Foundry(A, R)
    invocations = {"protocol": "invocations", "version": "2.0.0"}
    websocket = {"protocol": "invocations_ws", "version": "2.0.0"}
    responses = {"protocol": "responses", "version": "2.0.0"}
    foundry.add(A.agent_name, 1, "old", definition={
        "kind": "hosted", "container_configuration": {"image": "a:old"}, "protocol_versions": [invocations]})
    definition, _ = versions.image_source(foundry, A, "a:new")()
    assert definition["protocol_versions"] == [invocations, websocket, responses]
    assert foundry.versions[A.agent_name][0]["definition"]["protocol_versions"] == [invocations]
    foundry.add(A.agent_name, 2, "new", definition=definition)
    again, _ = versions.image_source(foundry, A, "a:next")()
    assert again["protocol_versions"] == [invocations, websocket, responses]
    copied, _ = versions.copy_source(foundry, A, "new")()
    assert copied["protocol_versions"] == [invocations, websocket, responses]
    foundry.add(A.agent_name, 3, "bare", definition={"kind": "hosted", "image": "a:bare"})
    bare, _ = versions.image_source(foundry, A, "a:newer")()
    assert "protocol_versions" not in bare


def test_protocol_declaration_preserves_other_protocols_and_existing_order():
    definition = {"protocol_versions": [
        {"protocol": "responses", "version": "2.0.0"},
        {"protocol": "invocations", "version": "2.0.0"},
        {"protocol": "invocations_ws", "version": "2.0.0"},
        {"protocol": "another-supported-protocol", "version": "2.0.0"},
    ]}
    original = json.loads(json.dumps(definition))
    versions._declare_host_protocols(definition)
    assert definition == original


def test_existing_probe_version_does_not_hide_a_clean_requested_definition():
    foundry = Foundry(A, R)
    definition = {"kind": "hosted", "image": "OFFLINE-main:new",
                  "environment_variables": {"FA_N0_STATE_PROBE": "fa-foundry-native-n0-v1"}}
    foundry.add(R.agent_name, 4, "new", definition=definition)
    result = _sync(foundry, R, build="new")
    assert result["state"] == "ok" and result["version"] == "5"
    assert len(foundry.creates()) == 1
    assert foundry.versions[R.agent_name][0]["definition"] == definition


def _composition(monkeypatch, *agents):
    monkeypatch.setattr(versions, "_agents", lambda composition: (A, list(agents)))
    return SimpleNamespace()


def test_one_agent_failing_never_stops_the_others_and_routes_are_optional(monkeypatch):
    other = _target(3, "efefefef-efef-efef-efef-efefefefefef", agent_name="agent-x")
    foundry = Foundry(A, R, other)
    foundry.add(A.agent_name, 1, "old")
    foundry.add(A.agent_name, 2, "new")
    foundry.add(R.agent_name, 1, "old")
    foundry.add(other.agent_name, 1, "old")
    composition = _composition(monkeypatch, (A, "configured"), (other, "registered"), (R, "registered"))
    real_list = foundry.list_versions

    def broken(name):
        if name == other.agent_name:
            raise RuntimeError("boom")
        return real_list(name)
    foundry.list_versions = broken
    clock = Clock()
    results = sync_all(composition, lambda endpoint: foundry, image="new:image", build="new", set_route=True,
                       sleep=clock.sleep, clock=clock)
    by_name = {result["agent_name"]: result for result in results}
    assert (by_name["agent-a"]["state"], by_name["agent-a"]["version"], by_name["agent-a"]["route"]) == ("ok", "3", "ok")
    assert by_name["agent-x"] == {"agent_id": other.agent_id, "agent_name": "agent-x", "build": "new",
                                  "state": "failed", "reason": "RuntimeError"}
    assert (by_name["agent-r"]["state"], by_name["agent-r"]["version"]) == ("ok", "2")
    assert foundry.creates("agent-r")[0][3]["fa_source"] == "agent-r:1"
    rule = foundry.routes[-1][1]["version_selector"]["version_selection_rules"]
    assert rule == [{"type": "FixedRatio", "agent_version": "2", "traffic_percentage": 100}]
    assert foundry.routes[-1][1]["protocol_configuration"] == {"protocol": "responses"}
    only = sync_all(composition, lambda endpoint: foundry, image="new:image", build="new", agent_id=R.agent_id)
    assert [result["agent_id"] for result in only] == [R.agent_id]
    assert sync_all(composition, lambda endpoint: foundry, build="new", agent_id="missing")[0]["reason"] == "AGENT_NOT_FOUND"


@pytest.mark.parametrize("deferred,follow_state,verify",
                         [(False, "ok", False), (True, "ok", False), (False, "pending", False), (False, "pending", True)])
def test_the_cli_runs_the_same_sync_for_every_agent(monkeypatch, capsys, deferred, follow_state, verify):
    """``verify``: ADR-0009 2d-i P9's verify-only follow; an Agent not moved yet is reported, not a failure."""
    import shared_context.behavior_server as server
    import shared_context.behavior_server_config as server_config
    from shared_context import native_environment_sync
    foundry = Foundry(A, R)
    foundry.add(A.agent_name, 2, versions.code_sha256())
    foundry.add(R.agent_name, 1, "old")
    composition = _composition(monkeypatch, (A, "configured"), (R, "registered"))
    composition.hosted_versions = HostedVersions(lambda endpoint: foundry)
    installations = []
    composition.installation = lambda: installations.append(True)

    class Built:
        def __enter__(self):
            return composition

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(server_config, "load_configuration", lambda path: path)
    monkeypatch.setattr(server, "build_server", lambda config: Built())
    seen, real = [], versions.sync_agent_version

    def spy(foundry_, target, **kw):
        seen.append(target.agent_name)
        return real(foundry_, target, sleep=lambda s: None, **{k: v for k, v in kw.items() if k != "sleep"})
    monkeypatch.setattr(versions, "sync_agent_version", spy)
    monkeypatch.setattr(versions, "_POLL_SECONDS", 0)
    follows = []

    def followed(owner, **options):
        assert owner is composition
        follows.append(options)
        return [{"agent_id": target.agent_id, "state": follow_state} for target in (A, R)]

    monkeypatch.setattr(native_environment_sync, "follow_all", followed)
    args = ["sync", "--config", "x.json", "--image", "new:image", "--route"]
    if deferred:
        args.append("--defer-follow")
    if verify:
        args.append("--verify-follow")
    failed = not deferred and not verify and follow_state == "pending"
    assert versions.main(args) == (1 if failed else 0)
    assert seen == ["agent-a", "agent-r"]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    state = "failed" if failed else "ok"
    assert [(line["agent_name"], line["state"]) for line in lines] == [("agent-a", state), ("agent-r", state)]
    if state == "failed":
        assert all(line["version_state"] == "ok" and line["reason"] == "NATIVE_ENVIRONMENT_PENDING" for line in lines)
    assert composition.hosted_versions.current(R) == "2"
    assert len(follows) == (0 if deferred else 1)
    assert len(installations) == (0 if deferred else 1)
    if not deferred:
        assert follows[0]["published"] == {A.agent_id, R.agent_id}
        assert follows[0].get("verify_only", False) is verify
        assert all(line["environment_follow"]["state"] == follow_state for line in lines)


@pytest.mark.parametrize("core", ["g" * 64, "a" * 63, "sha256:" + "a" * 64, "d" * 64])
def test_sync_cli_rejects_invalid_or_non_runtime_core_before_configuration(monkeypatch, capsys, core):
    import shared_context.behavior_server as server
    import shared_context.behavior_server_config as server_config

    monkeypatch.setattr(server_config, "load_configuration",
                        lambda _path: pytest.fail("--core must be checked before configuration"))
    monkeypatch.setattr(server, "build_server", lambda _config: pytest.fail("no server or credentials on invalid core"))
    with pytest.raises(SystemExit) as stopped:
        versions.main(["sync", "--config", "unused.json", "--image", "registry.invalid/hosted@sha256:" + "d" * 64,
                       "--core", core])
    assert stopped.value.code == 2
    assert "code identity" in capsys.readouterr().err


def test_sync_cli_replaces_image_digest_metadata_with_a_new_code_compatible_version(monkeypatch, capsys):
    import shared_context.behavior_server as server
    import shared_context.behavior_server_config as server_config

    core = "86f7b85ccf276941cc9e665504b25cfa66e837eef8fff28164e4352bbc1c9c07"
    digest = "df4cd1c9" + "d" * 56
    image = "registry.invalid/hosted@sha256:" + digest
    monkeypatch.setattr(versions, "code_sha256", lambda: core)
    foundry = Foundry(R)
    foundry.add(R.agent_name, 1, digest, definition={
        "kind": "hosted", "container_configuration": {"image": image}, "container_protocol_versions": [],
        "protocol_versions": [{"protocol": name, "version": "2.0.0"} for name in
                              ("invocations", "invocations_ws", "responses")],
        "environment_variables": {"FA_NATIVE_TURNS": "foundry-native-v1", "FA_RESPONSES_ENABLED": "1"},
        "session_configuration": {"idle_timeout_seconds": 3600}})
    original = copy.deepcopy(foundry.versions[R.agent_name][0])
    composition = _composition(monkeypatch, (R, "configured"))
    composition.hosted_versions = HostedVersions(lambda _endpoint: foundry)

    class Built:
        def __enter__(self):
            return composition

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(server_config, "load_configuration", lambda path: path)
    monkeypatch.setattr(server, "build_server", lambda config: Built())
    monkeypatch.setattr(versions, "_POLL_SECONDS", 0)
    with pytest.raises(Problem) as incompatible:
        composition.hosted_versions.current(R)
    assert _code(incompatible) == (503, FOLLOWING)
    args = ["sync", "--config", "offline.json", "--image", image, "--core", core, "--route", "--defer-follow"]
    for _ in range(2):
        assert versions.main(args) == 0
        [result] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert (result["state"], result["version"], result["build"], result["route"]) == ("ok", "2", core, "ok")
        assert composition.hosted_versions.current(R, require_responses=True) == "2"
    assert len(foundry.creates()) == 1
    assert foundry.creates()[0][3]["fa_build"] == core != digest
    assert foundry.versions[R.agent_name][0] == original


def test_a_conversation_pins_the_build_version_ignores_the_environment_and_the_directory_waits(
        synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    monkeypatch.setattr(versions, "current_hosted_version", REAL_CURRENT)
    a, config = c.h.composition, c.h.config
    original = config.targets[0]
    foundry = Foundry(original)
    foundry.add(original.agent_name, 41, "old")
    a.hosted_versions = HostedVersions(lambda endpoint: foundry, build="new")
    monkeypatch.setenv("FA_INSTRUCTION_HOSTED_VERSION", "41")
    monkeypatch.setenv("FA_INSTRUCTION_HOSTED_VERSIONS", json.dumps({original.agent_id: "41"}))
    with pytest.raises(Problem) as following:
        instruction_hosted_version(c.control, None, existing=False)
    assert _code(following) == (503, FOLLOWING)
    entry = next(item for item in public_agents(a, c.human).agents if item.agent_id == original.agent_id)
    assert (entry.state, entry.reason) == ("unavailable", FOLLOWING)
    assert instruction_hosted_version(c.control, SimpleNamespace(hosted_agent_version="41"), existing=True) == "41"
    foundry.add(original.agent_name, 42, "new")
    assert instruction_hosted_version(c.control, None, existing=False) == "42"
    entry = next(item for item in public_agents(a, c.human).agents if item.agent_id == original.agent_id)
    assert entry.state == "ready"


@pytest.mark.parametrize("status, transient", [(None, True), (408, True), (429, True), (500, True), (503, True),
                                               (401, False), (403, False), (404, False)])
def test_an_unreadable_selector_is_told_apart_from_a_confirmed_selection(status, transient):
    foundry = Foundry(R)
    foundry.add(R.agent_name, 4, "new")

    def unreadable(_name):
        raise FoundryCallError(status)

    foundry.get_agent = unreadable
    with pytest.raises(Problem) as refused:
        HostedVersions(lambda endpoint: foundry, build="new").current(R)
    # Same answer either way; only a transient platform failure is marked as saying nothing about the selection.
    assert _code(refused) == (503, FOLLOWING)
    assert isinstance(refused.value, SelectorUnreadable) is transient


def test_a_selected_version_this_build_cannot_serve_is_not_an_unreadable_selector():
    foundry = Foundry(R)
    foundry.add(R.agent_name, 3, "old")
    with pytest.raises(Problem) as incompatible:
        HostedVersions(lambda endpoint: foundry, build="new").current(R)
    assert _code(incompatible) == (503, FOLLOWING)
    assert not isinstance(incompatible.value, SelectorUnreadable)
