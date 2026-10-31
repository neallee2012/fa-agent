"""Stage CI accepts the native routine flags with the same rules as Agent sync and the API (README#native-routines-stage).

The checks run in `ci_deploy.py meta`, before any build or write, so a bad value never leaves the API updated and
the Agents not following. Offline: ci_deploy is loaded from its file; Agent sync and the API checks are the real code.
"""

import copy
import importlib.util
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared_context import behavior_agent_versions as versions
from shared_context.errors import Problem

ROOT = Path(__file__).resolve().parents[2]
A, B = "fa-stage-a", "fa-stage-b"
HOSTED_ON = {"FA_NATIVE_ROUTINES_ENABLED": "1", "FA_NATIVE_ROUTINE_PROPOSALS": "1"}
API_ON = {"FA_NATIVE_ROUTINES_ENABLED": "1", "FA_NATIVE_ROUTINE_CLOCK": "api", "FA_NATIVE_ROUTINE_PROPOSALS": "1"}


def load_ci():
    spec = importlib.util.spec_from_file_location("offline_stage_ci_routines", ROOT / "deploy" / "stage" / "ci_deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STAGE = copy.deepcopy(module.STAGE)
    # ADR-0009 2d-ii deploys only with the 2d-i switch on (2d-ii merges after stage has it on); a test of the off
    # profile sets "0" itself.
    module.STAGE["api_environment"]["FA_NATIVE_SESSION_GUARD_ENFORCE"] = "1"
    return module


def routines_off(stage):
    """The stage settings before the enabling PR, so each case starts from "off" whatever stage.json holds now."""
    for place in (stage["api_environment"], stage["console_environment"],
                  *(item.get("hosted_environment") or {} for item in stage["agents"])):
        for key in (*API_ON, *HOSTED_ON):
            place.pop(key, None)
    return stage


@pytest.fixture
def ci(monkeypatch):
    module = load_ci()
    routines_off(module.STAGE)
    monkeypatch.setattr(module, "release", lambda: "test")
    return module


def agent(ci, name):
    return next(item for item in ci.STAGE["agents"] if item["name"] == name)


def turn_on(ci):
    """What the separate enabling PR sets (README#native-routines-stage)."""
    for name in (A, B):
        agent(ci, name)["hosted_environment"].update(HOSTED_ON)
    ci.STAGE["api_environment"].update(API_ON)
    ci.STAGE["console_environment"]["FA_NATIVE_ROUTINES_ENABLED"] = "1"


def stops(call, *args):
    try:
        call(*args)
    except SystemExit:
        return True
    return False


def refused(environment, idle=3600):
    try:
        versions._check_environment(environment, idle)
    except Problem as error:
        assert error.body["error"]["code"] == "HOSTED_ENVIRONMENT_INVALID"
        return True
    return False


def test_ci_reads_the_one_hosted_key_list_that_sync_accepts():
    ci = load_ci()
    assert ci.hosted_flags() == versions._ENVIRONMENT_FLAGS
    assert {"FA_NATIVE_ROUTINES_ENABLED", "FA_NATIVE_ROUTINE_PROPOSALS"} <= ci.hosted_flags()
    assert ci.HOSTED_VALUE_KEYS < ci.hosted_flags()


def test_ci_and_sync_accept_and_refuse_the_same_hosted_overlays():
    ci = load_ci()
    values = ["0", "1", "2", "true", "", "api", "foundry-native-v1", "foundry-native-v2", "foundry-native-v3", "600",
              "0600", "Asia/Taipei", "a\nb", "x" * 2049, "foundry", "cosmos", "gpt-5.4-mini"]
    cases = [{key: value} for key, value in itertools.product(sorted(versions._ENVIRONMENT_FLAGS), values)]
    cases += [{"AZURE_CLIENT_SECRET": "synthetic"}, {"FA_NATIVE_ROUTINE_CLOCK": "api"}, {}, ["not", "a", "dict"],
              {**HOSTED_ON, "FA_NATIVE_HITL_ENABLED": "1"}, {"FA_NATIVE_ROUTINES_ENABLED": 1}]
    for environment in cases:
        assert stops(ci._check_hosted, "agent", environment, 3600) == refused(environment), environment
    for idle in (119, 120, 3600, 3601, "3600"):
        assert stops(ci._check_hosted, "agent", {}, idle) == refused({}, idle), idle


def test_ci_and_sync_accept_the_cosmos_memory_overlay_azd_writes():
    # ADR-0011 (deploy/azd/profile.py): private with memory on gives every assistant these two values.
    ci = load_ci()
    memory = {"FA_MEMORY_BACKEND": "cosmos", "FA_MEMORY_EXTRACTION_MODEL": "gpt-5.4-mini", "FA_TEAM_MEMORY_ENABLED": "1"}
    assert versions._check_environment(memory, 3600) == memory
    assert not stops(ci._check_hosted, "agent", memory, 3600)
    for bad in ({**memory, "FA_MEMORY_BACKEND": "Cosmos"}, {**memory, "FA_MEMORY_BACKEND": "1"},
                {**memory, "FA_MEMORY_EXTRACTION_MODEL": ""}, {**memory, "FA_MEMORY_EXTRACTION_MODEL": "a\nb"}):
        assert refused(bad) and stops(ci._check_hosted, "agent", bad, 3600), bad


def test_ci_accepts_a_v2_hosted_definition_and_keeps_the_api_on_v1(ci, monkeypatch):
    """ADR-0009 2d-i: a Hosted definition may declare v2 (only through the 2d-i switch, P9). The API's own
    FA_NATIVE_TURNS only switches native on, so it stays the v1 string also after 2d-ii (the API then sends v2)."""
    from shared_context.native_responses_wire import ACCEPTED_STATE_PROFILES, UNLABELED_STATE_PROFILE
    assert ci.HOSTED_STATE_PROFILES == ACCEPTED_STATE_PROFILES
    monkeypatch.setattr(ci, "missing_2di", lambda commit=None: [])
    ci.STAGE["api_environment"][ci.SESSION_GUARD] = "1"
    assert ci.hosted_environment()[agent(ci, A)["agent_id"]]["FA_NATIVE_TURNS"] == "foundry-native-v2"
    ci.STAGE["api_environment"].update({**API_ON, "FA_NATIVE_HITL_ENABLED": "1", "FA_NATIVE_TURNS": "foundry-native-v2"})
    with pytest.raises(SystemExit):
        ci.api_routines()
    ci.STAGE["api_environment"]["FA_NATIVE_TURNS"] = UNLABELED_STATE_PROFILE
    assert ci.api_routines() is True


def test_the_enabling_settings_pass_meta_and_reach_sync_api_and_the_workbench(ci, capsys):
    turn_on(ci)
    ci.meta()
    assert "release=test" in capsys.readouterr().out
    overlay = ci.hosted_environment()
    for name in (A, B):
        environment = overlay[agent(ci, name)["agent_id"]]
        assert {key: environment[key] for key in HOSTED_ON} == HOSTED_ON
        assert versions._check_environment(environment, ci.STAGE["hosted_idle_timeout_seconds"]) == environment
    script = ci._sync_script("registry.invalid/hosted@sha256:" + "a" * 64)
    assert "FA_NATIVE_ROUTINES_ENABLED" in script and "FA_NATIVE_ROUTINE_PROPOSALS" in script
    assert ci.api_routines() is True
    assert ci.console_overlay(ci.STAGE["console_environment"])["FA_NATIVE_ROUTINES_ENABLED"] == "1"


def test_the_api_starts_with_the_enabling_settings_and_with_them_set_to_zero(monkeypatch):
    from shared_context.native_routine_auth import routine_clock_enabled
    from shared_context.native_routine_wire import native_routines_enabled, routine_proposals_enabled
    for environment, on in ((API_ON, True), ({key: "0" for key in API_ON}, False)):
        assert routine_clock_enabled(environment) is on and native_routines_enabled(environment) is on
        assert routine_proposals_enabled(environment) is on


def test_whatever_the_api_would_refuse_at_startup_stops_in_meta_first(ci):
    from shared_context.native_routine_auth import routine_clock_enabled
    from shared_context.native_routine_wire import native_routines_enabled, routine_proposals_enabled
    options = (None, "0", "1", "api", "true")
    base = copy.deepcopy(ci.STAGE["api_environment"])
    for routines, clock, proposals in itertools.product(options, repeat=3):
        environment = {key: value for key, value in zip(API_ON, (routines, clock, proposals)) if value is not None}
        ci.STAGE["api_environment"] = {**base, **environment}
        api_refuses = False
        for check in (routine_clock_enabled, native_routines_enabled, routine_proposals_enabled):
            try:
                check(environment)
            except Problem:
                api_refuses = True
        if api_refuses:
            assert stops(ci.api_routines), environment


@pytest.mark.parametrize("change", [
    # Hosted: a value Agent sync refuses, or a schedule tool that would never be offered.
    ("hosted", {"FA_NATIVE_ROUTINES_ENABLED": "true"}),
    ("hosted", {"FA_NATIVE_ROUTINE_PROPOSALS": "yes"}),
    ("hosted", {"FA_NATIVE_ROUTINE_PROPOSALS": "1"}),
    ("hosted", {"FA_NATIVE_ROUTINE_CLOCK": "api"}),
    # API: a value the API refuses at startup, a clock that never starts, or cards that would always be refused.
    ("api", {"FA_NATIVE_ROUTINES_ENABLED": "on"}),
    ("api", {"FA_NATIVE_ROUTINE_CLOCK": "job", "FA_NATIVE_ROUTINES_ENABLED": "1"}),
    ("api", {"FA_NATIVE_ROUTINE_CLOCK": "api"}),
    ("api", {"FA_NATIVE_ROUTINE_PROPOSALS": "1", "FA_NATIVE_ROUTINES_ENABLED": "1"}),
    ("api", {**API_ON, "FA_NATIVE_HITL_ENABLED": "0"}),
    ("api_no_turns", API_ON),
    # Across components: the Agents and the workbench reach routines only through the API.
    ("hosted", {**HOSTED_ON, "FA_NATIVE_HITL_ENABLED": "1"}),
    ("console", {"FA_NATIVE_ROUTINES_ENABLED": "1"}),
    # Workbench: only "0" or "1".
    ("console", {"FA_NATIVE_ROUTINES_ENABLED": "yes"}),
    ("console", {"FA_NATIVE_ROUTINE_CLOCK": "api"}),
])
def test_meta_stops_on_bad_routine_settings_before_any_build(ci, capsys, change):
    where, values = change
    if where == "hosted":
        agent(ci, A)["hosted_environment"].update(values)
    elif where == "console":
        ci.STAGE["console_environment"].update(values)
    else:
        ci.STAGE["api_environment"].update(values)
        if where == "api_no_turns":
            ci.STAGE["api_environment"].pop("FA_NATIVE_TURNS")
    with pytest.raises(SystemExit):
        ci.meta()
    assert capsys.readouterr().out == "", "nothing reaches $GITHUB_OUTPUT, so no build or write starts"


def test_turning_off_means_zero_not_a_deleted_key():
    """Sync keeps a served value the overlay no longer names, and the API update refuses a deployed ENV the repo
    dropped; only an explicit "0" turns a routine flag off."""
    served = {"kind": "hosted", "image": "old", "environment_variables": {**HOSTED_ON, "FA_NATIVE_HITL_ENABLED": "1"},
              "session_configuration": {"idle_timeout_seconds": 3600}}
    source = lambda: (copy.deepcopy(served), "agent:1")  # noqa: E731
    dropped, _ = versions.desired_source(source, {"FA_NATIVE_HITL_ENABLED": "1"}, idle_timeout_seconds=3600)()
    off, _ = versions.desired_source(source, {key: "0" for key in HOSTED_ON}, idle_timeout_seconds=3600)()
    assert dropped["environment_variables"]["FA_NATIVE_ROUTINE_PROPOSALS"] == "1", "a deleted key stays on"
    assert {key: off["environment_variables"][key] for key in HOSTED_ON} == {key: "0" for key in HOSTED_ON}


def test_turning_off_the_api_by_deleting_keys_stops_instead_of_dropping_them(ci, monkeypatch):
    deployed = [{"name": key, "value": value} for key, value in {**ci.STAGE["api_environment"], **API_ON}.items()]

    def rest(method, path, body=None):
        assert method == "GET", "nothing is written"
        return {"properties": {"template": {"containers": [{"name": "app", "image": "old", "env": copy.deepcopy(deployed)}]},
                               "configuration": {"secrets": []}}}

    monkeypatch.setattr(ci, "rest", rest)
    with pytest.raises(SystemExit) as stopped:
        ci._update_container("containerApps", ci.STAGE["api_app"], "img", "s1", environment=ci.STAGE["api_environment"])
    assert "FA_NATIVE_ROUTINE_CLOCK" in str(stopped.value)


@pytest.mark.parametrize("replicas,stopped", [(0, True), ("missing", True), (None, True), (1, False), (2, False)])
def test_guard_stops_when_the_api_clock_would_not_stay_resident(ci, monkeypatch, replicas, stopped):
    turn_on(ci)
    scale = {} if replicas == "missing" else {"minReplicas": replicas}
    monkeypatch.setattr(ci, "rest", lambda method, path, body=None: {"properties": {
        "template": {"scale": scale, "containers": [{"env": [{"name": ci.API_CONFIG_ENV, "value": ci._config_bytes().decode()}]}]},
        "configuration": {"secrets": [{"name": "fa-feed-mcp-signing-key"}]},
        "latestReadyRevisionName": ci.STAGE["api_app"] + "--abcdef1-r1-1"}})

    class PastTheClockCheck(Exception):
        pass

    def run(*args, **kwargs):
        raise PastTheClockCheck()

    monkeypatch.setattr(ci.subprocess, "run", run)
    expected = SystemExit if stopped else PastTheClockCheck
    with pytest.raises(expected) as raised:
        ci.guard()
    if stopped:
        assert "minReplicas" in str(raised.value)


def test_stage_json_turns_routines_on_for_a_b_the_api_and_the_workbench(capsys):
    """The enabling settings (README#native-routines-stage); off later means every key to "0", never a deleted key."""
    stage = json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))
    assert {key: stage["api_environment"][key] for key in API_ON} == API_ON
    assert stage["console_environment"]["FA_NATIVE_ROUTINES_ENABLED"] == "1"
    for item in stage["agents"]:
        hosted = item.get("hosted_environment") or {}
        expected = HOSTED_ON if item["name"] in {A, B} else {}
        assert {key: hosted[key] for key in HOSTED_ON if key in hosted} == expected, item["name"]
    ci = load_ci()
    ci.release = lambda: "test"
    ci.meta()
    assert "release=test" in capsys.readouterr().out and ci.api_routines() is True


def test_turning_stage_off_sets_every_routine_key_to_zero_and_passes_meta(capsys):
    ci = load_ci()
    ci.release = lambda: "test"
    for place in (ci.STAGE["api_environment"], ci.STAGE["console_environment"],
                  *(item.get("hosted_environment") or {} for item in ci.STAGE["agents"])):
        for key in (*API_ON, *HOSTED_ON):
            if key in place:
                place[key] = "0"
    ci.meta()
    assert "release=test" in capsys.readouterr().out and ci.api_routines() is False


def test_stage_refuses_the_dev_only_cut_fault_hook(capsys):
    """ADR-0009 gate 2's fault hook holds every files fence to its bound: dev only, refused in meta for stage."""
    ci = load_ci()
    ci.release = lambda: "test"
    ci.STAGE["api_environment"]["FA_NATIVE_CUT_FAULT"] = "after_pending"
    with pytest.raises(SystemExit, match="FA_NATIVE_CUT_FAULT"):
        ci.meta()
    ci.STAGE["environment"] = "dev"
    ci.api_faults()
