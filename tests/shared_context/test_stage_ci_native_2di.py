"""ADR-0009 2d-i's one deploy switch (P9, src README#native-2di-switch): api_environment
FA_NATIVE_SESSION_GUARD_ENFORCE="1" also makes every assistant's Hosted definition declare foundry-native-v2, the
deploy follow only reports, and guard, sync, api and follow let it on only over one serving revision that already
runs P5+P6.

Offline: ci_deploy is loaded from its file; ARM reads and job runs are replaced."""

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared_context import behavior_agent_versions as versions
from test_stage_ci_routines import load_ci

ROOT = Path(__file__).resolve().parents[2]
V1, V2 = "foundry-native-v1", "foundry-native-v2"
GUARD = "FA_NATIVE_SESSION_GUARD_ENFORCE"
IDLE = "FA_NATIVE_IDLE_UPGRADE"


@pytest.fixture
def ci(monkeypatch):
    module = load_ci()
    monkeypatch.setattr(module, "release", lambda: "test")
    monkeypatch.setattr(module, "missing_2di", lambda commit=None: [])
    return module


def switch_on(ci):
    ci.STAGE["api_environment"][GUARD] = "1"


def guard_off_profile(ci):
    """A profile with the switch off (a new azd environment, or dev before its turn-on): the off branch's rules.
    stage.json itself is on and never goes off again."""
    ci.STAGE["api_environment"][GUARD] = "0"


def assistants(ci):
    return [item for item in ci.STAGE["agents"] if item["kind"] == "assistant"]


def test_stage_is_on_and_a_new_azd_environment_starts_off(ci):
    for path, guard in ((ROOT / "deploy" / "stage" / "stage.json", "1"),
                        (ROOT / "deploy" / "azd" / "profile.defaults.json", "0")):
        profile = json.loads(path.read_text(encoding="utf-8"))
        assert profile["api_environment"][GUARD] == guard and profile["api_environment"][IDLE] == "0", path
        # The files keep v1: ci_deploy derives the assistants' v2 from the switch, never from the profile.
        assert profile["assistant_environment"]["FA_NATIVE_TURNS"] == V1
        assert all("FA_NATIVE_TURNS" not in (agent.get("hosted_environment") or {})
                   for agent in profile.get("agents", []))
        assert "FA_NATIVE_TURNS" not in profile.get("assistant_hosted_environment", {})
    assert ci.native_2di() is True


def test_a_profile_with_the_switch_off_declares_no_v2_and_the_follow_still_follows(ci):
    """The off profile (a new azd environment, dev before its turn-on): not on, no assistant (or any Agent) declares
    v2, and the sync's follow step has no --verify-follow."""
    guard_off_profile(ci)
    assert ci.native_2di() is False
    assert all(environment.get("FA_NATIVE_TURNS") != V2 for environment in ci.hosted_environment().values())
    assert "--verify-follow" not in ci._sync_script("registry/hosted@sha256:" + "a" * 64, follow=True)


def test_the_real_stage_profile_passes_every_offline_turn_on_precondition(monkeypatch):
    """The real stage.json and this checkout, with ARM, ``serving_commit``, ``run``, ``realtime_ready`` and ``release``
    replaced and nothing else (``missing_2di`` is the real one): meta's checks pass, only 2d-i is on (IDLE, the
    reconciler and the upgrade snapshots stay off), and guard lets the first turn-on through over one serving revision
    whose commit (this checkout) declares NATIVE_2DI_SCHEMA 2."""
    ci = load_ci()
    ci.release = lambda: "test"
    api = ci.STAGE["api_environment"]
    assert ci.native_2di() is True and ci.missing_2di() == []
    assert api[IDLE] == "0" and api["FA_NATIVE_UPGRADE_RECONCILER"] == "0" and api["FA_NATIVE_TURNS"] == V1
    assert "FA_UPGRADE_SNAPSHOT_CONTAINER_URL" not in api
    ci.check()
    overlay = ci.hosted_environment()
    for agent in ci.STAGE["agents"]:
        environment = overlay.get(agent["agent_id"]) or {}
        assert (environment.get("FA_NATIVE_TURNS") == V2) is (agent["kind"] == "assistant"), agent["name"]
        if environment:
            assert versions._check_environment(environment, ci.STAGE["hosted_idle_timeout_seconds"]) == environment
    assert "'--verify-follow'" in ci._sync_script("registry/hosted@sha256:" + "a" * 64, follow=True)
    for deployed in (None, "0", "1"):  # the first turn-on (stage serves "0" or no key), then every later deploy
        api_app(ci, monkeypatch, deployed=deployed)
        monkeypatch.setattr(ci, "serving_commit", lambda revision: "HEAD")
        ci.guard()


def test_switch_on_sets_both_and_the_follow_only_reports(ci):
    switch_on(ci)
    ci.check()
    overlay = ci.hosted_environment()
    assert {agent_id for agent_id, env in overlay.items() if env.get("FA_NATIVE_TURNS") == V2} == {
        agent["agent_id"] for agent in assistants(ci)}
    for agent in assistants(ci):
        environment = overlay[agent["agent_id"]]
        assert versions._check_environment(environment, ci.STAGE["hosted_idle_timeout_seconds"]) == environment
    assert ci.STAGE["api_environment"][GUARD] == "1" and ci.STAGE["api_environment"]["FA_NATIVE_TURNS"] == V1
    script = ci._sync_script("registry/hosted@sha256:" + "a" * 64, follow=True)
    assert "'--verify-follow'" in script and "--defer-follow" not in script
    assert "--verify-follow" not in ci._sync_script("registry/hosted@sha256:" + "a" * 64)  # the sync step


@pytest.mark.parametrize("case", ["v2_hosted_without_switch", "v2_seed_without_switch", "v1_hosted_with_switch",
                                  "bad_value"])
def test_one_without_the_other_is_refused(ci, case):
    a = assistants(ci)[0]
    if case == "v2_hosted_without_switch":
        guard_off_profile(ci)
        a["hosted_environment"]["FA_NATIVE_TURNS"] = V2
    elif case == "v2_seed_without_switch":
        guard_off_profile(ci)
        ci.STAGE["assistant_environment"]["FA_NATIVE_TURNS"] = V2
    elif case == "v1_hosted_with_switch":
        switch_on(ci)
        a["hosted_environment"]["FA_NATIVE_TURNS"] = V1
    else:
        ci.STAGE["api_environment"][GUARD] = "true"
    with pytest.raises(SystemExit, match="2d-i switch"):
        ci.check()


def test_an_executor_never_declares_v2_even_with_the_switch_on(ci):
    switch_on(ci)
    executor = next(item for item in ci.STAGE["agents"] if item["kind"] != "assistant")
    executor["hosted_environment"] = {**(executor.get("hosted_environment") or {}), "FA_NATIVE_TURNS": V2}
    with pytest.raises(SystemExit, match="assistants only"):
        ci.check()


def test_switch_on_refuses_a_checkout_without_p5_and_p6(monkeypatch):
    ci = load_ci()
    ci.release = lambda: "test"
    switch_on(ci)
    sources = {"native_binding.py": "NATIVE_2DI_SCHEMA = 2\n"}
    monkeypatch.setattr(ci, "_api_source", lambda name, commit=None: sources.get(name))
    ci.check()
    for source in ("NATIVE_2DI_SCHEMA = 1\n", "# no schema yet\n", "NATIVE_2DI_SCHEMA = '2'\n", "def (:\n"):
        sources["native_binding.py"] = source
        with pytest.raises(SystemExit, match="NATIVE_2DI_SCHEMA=., needs 2"):
            ci.check()


def api_app(ci, monkeypatch, *, deployed=None, mode="Single", active=1, revision=None, ready_active=True, idle=None):
    """The API app guard/sync/api read; ``ready_active`` False: the one active revision is not the latest ready."""
    env = [{"name": "PYTHONUTF8", "value": "1"}] + ([{"name": GUARD, "value": deployed}] if deployed else [])
    env += [{"name": IDLE, "value": idle}] if idle else []
    ready = revision or f"{ci.STAGE['api_app']}--abcdef1-r1-1"
    names = [ready, "r1", "r2"] if ready_active else ["r1", ready, "r2"]
    revisions = {"value": [{"name": name, "properties": {"active": index < active}}
                           for index, name in enumerate(names)]}
    secrets = [{"name": ref} for ref in ci.api_secrets().values()]
    app = {"properties": {"configuration": {"activeRevisionsMode": mode, "secrets": secrets},
                          "template": {"containers": [{"env": env}], "scale": {"minReplicas": 1}},
                          "latestReadyRevisionName": ready}}
    monkeypatch.setattr(ci, "rest", lambda method, path, body=None: revisions if "/revisions?" in path else app)
    monkeypatch.setattr(ci, "realtime_ready", lambda: None)
    monkeypatch.setattr(ci, "run", lambda *args, **kw: "")


@pytest.fixture
def served(ci, monkeypatch):
    """The serving commit is known and contained; which commits carry P5+P6 is set per test."""
    monkeypatch.setattr(ci, "serving_commit", lambda revision: None if "--portal" in revision else "base")
    old = []
    monkeypatch.setattr(ci, "missing_2di", lambda commit=None: ["IN_REQUEST_WAIT_SECONDS"] if commit in old else [])
    monkeypatch.setattr(ci, "missing_idle", lambda commit=None: ["NATIVE_IDLE_SCHEMA"] if commit == PRE_4B1 else [],
                        raising=False)
    return old


PRE_4B1 = "serving-without-4b-1"


def test_turning_on_passes_over_one_revision_already_on_p6(ci, monkeypatch, served):
    switch_on(ci)
    api_app(ci, monkeypatch)
    ci.guard()


@pytest.mark.parametrize("mode,active,ready_active,message", [
    ("Multiple", 1, True, "single revision mode"), ("Single", 2, True, "one active revision"),
    ("Single", 1, False, "one active revision, the latest ready one")])
def test_turning_on_refuses_more_than_one_revision(ci, monkeypatch, served, mode, active, ready_active, message):
    switch_on(ci)
    api_app(ci, monkeypatch, mode=mode, active=active, ready_active=ready_active)
    with pytest.raises(SystemExit, match=message):
        ci.guard()
    api_app(ci, monkeypatch, deployed="1", active=active, ready_active=ready_active)
    ci.guard()  # already on: a later deploy's old revision may still be deactivating
    api_app(ci, monkeypatch, deployed="1", mode="Multiple")
    with pytest.raises(SystemExit, match="single revision mode"):
        ci.guard()


@pytest.mark.parametrize("guard,idle", [("0", "1"), ("1", "yes")])
def test_the_idle_upgrade_needs_the_2di_switch(ci, guard, idle):
    ci.STAGE["api_environment"].update({GUARD: guard, IDLE: idle})
    with pytest.raises(SystemExit, match=f"{IDLE} accepts only"):
        ci.check()


def test_turning_the_idle_upgrade_on_needs_one_serving_revision(ci, monkeypatch, served):
    """ADR-0009 §12: 2d-i is already on; turning the idle upgrade on checks the revisions as turning 2d-i on does."""
    switch_on(ci)
    ci.STAGE["api_environment"][IDLE] = "1"
    ci.check()
    api_app(ci, monkeypatch, deployed="1", active=2)
    with pytest.raises(SystemExit, match="idle upgrade on needs one active revision"):
        ci.guard()
    api_app(ci, monkeypatch, deployed="1")
    ci.guard()
    api_app(ci, monkeypatch, deployed="1", active=2, idle="1")
    ci.guard()  # already on: only the mode is checked
    api_app(ci, monkeypatch, deployed="1", mode="Multiple", idle="1")
    with pytest.raises(SystemExit, match="single revision mode"):
        ci.guard()


@pytest.mark.parametrize("deployed", ["1", None])
def test_turning_the_idle_upgrade_on_needs_a_serving_api_with_4b1(ci, monkeypatch, served, deployed):
    """IDLE=1 in the deploy that first ships 4b-1: the draining revision would read the new revision's ``idle_upgrade``
    rows as 409 NATIVE_BINDING_INVALID, so the serving commit must already declare NATIVE_IDLE_SCHEMA."""
    switch_on(ci)
    ci.STAGE["api_environment"][IDLE] = "1"
    api_app(ci, monkeypatch, deployed=deployed)
    monkeypatch.setattr(ci, "serving_commit", lambda revision: PRE_4B1)
    with pytest.raises(SystemExit, match="the idle upgrade \\(4b-1\\) on needs .*NATIVE_IDLE_SCHEMA"):
        ci.guard()
    monkeypatch.setattr(ci, "serving_commit", lambda revision: "base")
    ci.guard()

def test_every_deploy_with_the_idle_upgrade_on_needs_a_serving_api_at_its_schema(ci, monkeypatch, served):
    """IDLE already on: a later phase raises NATIVE_IDLE_SCHEMA (6a (c) 2, 4d 3, 4b-4 4) while it stays on, so the draining
    revision must decode the new rows too; an old revision still deactivating is not refused for that."""
    switch_on(ci)
    ci.STAGE["api_environment"][IDLE] = "1"
    monkeypatch.setattr(ci, "serving_commit", lambda revision: PRE_4B1)
    api_app(ci, monkeypatch, deployed="1", idle="1")
    with pytest.raises(SystemExit, match="the idle upgrade is on and the serving API lacks .*NATIVE_IDLE_SCHEMA.*"
                                         f"{IDLE}=\"0\" first"):
        ci.guard()
    monkeypatch.setattr(ci, "serving_commit", lambda revision: None)
    with pytest.raises(SystemExit, match="the serving commit is unknown"):
        ci.guard()
    monkeypatch.setattr(ci, "serving_commit", lambda revision: "base")
    api_app(ci, monkeypatch, deployed="1", idle="1", active=2)
    ci.guard()  # at the schema: the one-active-revision check is for turning on only
    ci.STAGE["api_environment"][IDLE] = "0"
    monkeypatch.setattr(ci, "serving_commit", lambda revision: PRE_4B1)
    ci.guard()  # the first step of the two: this commit with IDLE "0"


@pytest.mark.parametrize("deployed", ["1", None])
def test_with_the_idle_upgrade_off_the_serving_4b1_is_never_checked(ci, monkeypatch, served, deployed):
    switch_on(ci)
    monkeypatch.setattr(ci, "serving_commit", lambda revision: PRE_4B1)
    monkeypatch.setattr(ci, "missing_idle", lambda commit=None: pytest.fail("IDLE=0 reads no 4b-1 schema"),
                        raising=False)
    api_app(ci, monkeypatch, deployed=deployed)
    ci.guard()


HOSTED = "/fa-behavior-assistant-desktop@sha256:" + "a" * 64


def test_sync_follow_and_api_check_the_switch_too(ci, monkeypatch, served):
    """The dev line runs sync -> api -> follow without guard (deploy/README.md): each step checks before any write."""
    writes = []
    monkeypatch.setattr(ci, "_run_job", lambda *args, **kw: writes.append("job") or ("Succeeded", "x"))
    monkeypatch.setattr(ci, "_update_container", lambda *args, **kw: writes.append("api") or "rev")
    monkeypatch.setattr(ci, "_wait_revision", lambda *args: None)
    monkeypatch.setattr(ci, "_healthz", lambda url: None)
    hosted = ci.STAGE["registry"] + HOSTED
    steps = (lambda: ci.sync("api:ref", hosted), lambda: ci.sync("api:ref", hosted, follow=True),
             lambda: ci.api("api:ref"))
    guard_off_profile(ci)
    api_app(ci, monkeypatch, deployed="1")  # on, and this profile turns it off
    for step in steps:
        with pytest.raises(SystemExit, match="cannot be turned off"):
            step()
    switch_on(ci)
    served.append("base")  # turning on over a serving API without P6
    api_app(ci, monkeypatch)
    for step in steps:
        with pytest.raises(SystemExit, match="2d-i \\(P5\\+P6\\) on needs"):
            step()
    served.clear()
    api_app(ci, monkeypatch, active=2)
    for step in steps:
        with pytest.raises(SystemExit, match="one active revision"):
            step()
    assert writes == []
    api_app(ci, monkeypatch)
    for step in steps:
        step()
    assert writes == ["job", "job", "api"]


def test_sync_with_the_switch_off_refuses_a_served_v2_and_says_rerun_with_1(ci, monkeypatch, served):
    """A turn-on that stopped after sync (v2 published, API PATCH failed), then a profile back to "0": the sync job
    gets --refuse-native-v2, and its failure says to rerun with "1"."""
    guard_off_profile(ci)
    api_app(ci, monkeypatch)
    scripts = []
    monkeypatch.setattr(ci, "_run_job", lambda api_ref, script, **kw: scripts.append(script) or ("Failed", "x"))
    with pytest.raises(SystemExit, match=f'NATIVE_V2_SERVED_SWITCH_OFF.*{GUARD}="1"'):
        ci.sync("api:ref", ci.STAGE["registry"] + HOSTED, follow=True)
    assert "'--refuse-native-v2'" in scripts[0]
    switch_on(ci)
    assert "--refuse-native-v2" not in ci._sync_script("registry" + HOSTED, follow=True)


@pytest.mark.parametrize("served,refused", [(V2, True), (V1, False)])
def test_the_sync_job_refuses_every_agent_already_serving_v2_with_the_switch_off(monkeypatch, served, refused):
    """Every kind, not only assistants: with the switch off no Agent may carry v2 on (hosted_environment() already
    refuses v2 on any kind then). The refused Agent fails before any route or create write."""
    definition = {"kind": "hosted", "image": "old", "environment_variables": {"FA_NATIVE_TURNS": served}}
    writes = []
    foundry = SimpleNamespace(
        get_agent=lambda name: {"name": name, "state": "enabled", "agent_endpoint": {"version_selector": {
            "version_selection_rules": [{"type": "FixedRatio", "agent_version": "3", "traffic_percentage": 100}]}}},
        get_version=lambda name, version: {"name": name, "version": version, "status": "active",
                                           "definition": definition},
        update_route=lambda *args: writes.append("route"), create_version=lambda *args, **kw: writes.append("create"))
    target = SimpleNamespace(agent_id="agent-a", agent_name="fa-a", project_endpoint="https://p")
    monkeypatch.setattr(versions, "_agents", lambda composition: (target, [(target, "configured")]))
    result = versions.sync_all(None, lambda endpoint: foundry, image="new", set_route=True, refuse_native_v2=True)[0]
    if refused:
        assert (result["state"], result["reason"], writes) == ("failed", "NATIVE_V2_SERVED_SWITCH_OFF", [])
    else:
        assert result.get("reason") != "NATIVE_V2_SERVED_SWITCH_OFF"


def test_turning_on_refuses_an_old_serving_api(ci, monkeypatch, served):
    switch_on(ci)
    served.append("base")
    api_app(ci, monkeypatch)
    with pytest.raises(SystemExit, match="2d-i \\(P5\\+P6\\) on needs"):
        ci.guard()
    api_app(ci, monkeypatch, deployed="1")  # already on: later deploys roll from P6+ to newer
    ci.guard()


def test_turning_on_refuses_an_unknown_serving_commit(ci, monkeypatch, served):
    switch_on(ci)
    api_app(ci, monkeypatch, revision=f"{ci.STAGE['api_app']}--portal")
    with pytest.raises(SystemExit, match="serving commit is unknown"):
        ci.guard()


def test_it_never_goes_off_again(ci, monkeypatch, served):
    guard_off_profile(ci)
    api_app(ci, monkeypatch, deployed="1")
    with pytest.raises(SystemExit, match="cannot be turned off"):
        ci.guard()
    api_app(ci, monkeypatch, deployed="0")
    ci.guard()


def test_stage_refuses_the_fault_hook_with_the_switch_on(ci):
    switch_on(ci)
    ci.STAGE["api_environment"]["FA_NATIVE_CUT_FAULT"] = "after_pending"
    with pytest.raises(SystemExit, match="FA_NATIVE_CUT_FAULT"):
        ci.meta()


def test_the_schema_check_reads_the_real_checkout():
    """The AST read of this checkout equals the imported constant, and it is 2 exactly when P6's verify-only follow
    is here: with P6 (#862) in main this checkout passes the switch's code check."""
    from shared_context import native_binding, native_environment_sync
    real = load_ci()
    assert real.api_2di_schema() == native_binding.NATIVE_2DI_SCHEMA == 2
    assert "verify_only" in inspect.signature(native_environment_sync.follow_all).parameters
    assert real.missing_2di() == []
    assert real.missing_2di("0" * 40) == ["native_binding.NATIVE_2DI_SCHEMA=0, needs 2"]  # no such commit
    # 2 = 6a (c) (``idle_upgrade.waiting``); 3 = 4d (the rolled-back slot values and the fence's ``rollback``); 4 = 4b-4
    # (the ``vetoed`` slot value): an API that decodes them must serve before the idle upgrade goes on.
    assert real.api_2di_schema(name="NATIVE_IDLE_SCHEMA") == native_binding.NATIVE_IDLE_SCHEMA == 4
    assert real.missing_idle() == []
    assert real.missing_idle("0" * 40) == ["native_binding.NATIVE_IDLE_SCHEMA=0, needs 4"]


def test_verify_follow_excludes_defer_follow(capsys):
    with pytest.raises(SystemExit):
        versions.main(["sync", "--config", "x.json", "--route", "--defer-follow", "--verify-follow"])
    assert "exclude each other" in capsys.readouterr().err


# ----- registered (onboarded) Agents: not in STAGE["agents"], so sync itself gives the assistants v2 ----------------

SERVED = {"KEEP": "1", "FA_NATIVE_TURNS": V1, "FA_NATIVE_HITL_ENABLED": "1", "TZ": "Asia/Taipei"}


def _sync_registered(monkeypatch, served, **options):
    """One configured and one registered Agent, both serving ``served``; the definition sync creates for each."""
    from test_agent_versions import A, Clock, Foundry, R
    foundry = Foundry(A, R)
    for target in (A, R):
        foundry.add(target.agent_name, 1, "old", definition={
            "kind": "hosted", "image": "old:image", "environment_variables": dict(served)})
    monkeypatch.setattr(versions, "_agents", lambda _: (A, [(A, "configured"), (R, "registered")]))
    clock = Clock()
    results = versions.sync_all(SimpleNamespace(), lambda _: foundry, image="new:image", build="new", set_route=True,
                                sleep=clock.sleep, clock=clock, **options)
    assert [item["state"] for item in results] == ["ok", "ok"]
    return foundry, R, {target.agent_name: foundry.creates(target.agent_name)[0][2] for target in (A, R)}


def test_with_the_switch_on_a_registered_assistant_declares_v2_and_keeps_its_other_env(monkeypatch):
    _, _, created = _sync_registered(monkeypatch, SERVED, native_v2_registered=True)
    assert created["agent-r"]["environment_variables"] == {**SERVED, "FA_NATIVE_TURNS": V2}
    # Configured Agents get v2 only from ci_deploy's overlay (hosted_environment()), not from this path.
    assert created["agent-a"]["environment_variables"] == SERVED


@pytest.mark.parametrize("options", [{}, {"refuse_native_v2": True}])
def test_with_the_switch_off_a_registered_agent_keeps_v1(monkeypatch, options):
    _, _, created = _sync_registered(monkeypatch, SERVED, **options)
    assert created["agent-r"]["environment_variables"] == created["agent-a"]["environment_variables"] == SERVED


def test_a_registered_agent_declaring_no_native_profile_is_untouched(monkeypatch):
    """Not an assistant (an executor's definition declares no FA_NATIVE_TURNS, seed_agents._env): no v2 is added."""
    served = {key: value for key, value in SERVED.items() if key != "FA_NATIVE_TURNS"}
    _, _, created = _sync_registered(monkeypatch, served, native_v2_registered=True)
    assert created["agent-r"]["environment_variables"] == served


@pytest.mark.parametrize("on", [True, False])
def test_after_the_release_the_2dii_gate_no_longer_lists_the_registered_agent_as_v1(monkeypatch, on):
    """2d-ii supports v2 only. The 2d-i cut binds the Agent to the version sync published and records the profile
    that version declares (``record_switch``); with the switch's v2 the gate finds no v1 row."""
    from shared_context import native_deploy_gate as gate
    from test_native_deploy_gate import read_with, row, store_with
    foundry, registered, created = _sync_registered(monkeypatch, SERVED, native_v2_registered=on)
    version = foundry.selectors[registered.agent_name]
    store, _ = store_with(row(registered.agent_id, "sid-r", version,
                              profile=gate.definition_profile(created[registered.agent_name])))
    report = gate.evaluate([registered], store.rows, read_with(foundry), armed=True, supported=frozenset({V2}))
    if on:
        assert (report["decision"], report["refused"]) == ("pass", [])
    else:
        assert report["refused"] == [{"agent_id": registered.agent_id, "reason": "row_profile_unsupported"}]


def test_the_sync_job_gets_the_registered_v2_flag_only_with_the_switch_on(ci, capsys):
    switch_on(ci)
    for follow in (False, True):
        assert "'--native-v2-registered'" in ci._sync_script("registry/hosted@sha256:" + "a" * 64, follow=follow)
    guard_off_profile(ci)
    script = ci._sync_script("registry/hosted@sha256:" + "a" * 64, follow=True)
    assert "--native-v2-registered" not in script and "'--refuse-native-v2'" in script
    with pytest.raises(SystemExit):
        versions.main(["sync", "--config", "x.json", "--native-v2-registered", "--refuse-native-v2"])
    assert "exclude each other" in capsys.readouterr().err
