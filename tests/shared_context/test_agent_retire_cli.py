"""The retire operator command: read-only preview, confirmed Foundry/ARM cleanup, convergent reruns. Local fakes."""

from datetime import timedelta
import logging
from types import SimpleNamespace

import httpx
import pytest

import shared_context.behavior_agent_onboarding as onboarding
from shared_context.behavior_agent_message_api import public_agents
from shared_context.behavior_agent_onboarding import FoundryCallError, derived_names, onboarding_status, pending_key
from shared_context.behavior_agent_retire import (
    STUCK_ONBOARDING_SECONDS, AgentRoleAssignments, preview, preview_failed, retire, retire_failed,
)
from shared_context.behavior_agent_setup import FOUNDRY_USER, OPENAI_USER
from shared_context.behavior_migration import MigrationStore, _quiescence, installed_origin
from shared_context.behavior_team_registry import registered_agent_ids, retired_agent_ids, validate_team_registry_history
from shared_context.errors import Problem
from test_agent_onboarding import FakeFoundry as OnboardingFoundry, team  # noqa: F401  (fixture)
from test_agent_retire import NOW, _code
from test_conversation_transfer_startup import read_startup
from test_resident_worker_poc import outcome_of
from test_behavior_server import workspace  # noqa: F401  (fixture)
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401  (fixture)
from test_instruction_flow import instruction_models  # noqa: F401  (fixture)
from test_m3_auth import jwk_client, signing_key  # noqa: F401  (fixtures)
from test_team_agent_routing import register_more_agents, team_agent  # noqa: F401  (fixture)
from test_team_registry import AGENT_OID, OTHER_AGENT_OID

SCOPE = ("/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-fa"
         "/providers/Microsoft.CognitiveServices/accounts/fa-account/projects/project")

class FakeFoundry:
    def __init__(self, present=True):
        self.present, self.deleted = present, []

    def get_agent(self, name):
        if not self.present:
            raise FoundryCallError(404)
        return {"name": name}

    def list_versions(self, name):
        return [{"version": "2"}, {"version": "10"}, {"version": "1"}]

    def list_sessions(self, name):
        return [{"agent_session_id": "fa-session-1", "status": "idle"}]

    def delete_agent(self, name):
        self.deleted.append(name)
        self.present = False
        return {"object": "agent.deleted", "name": name, "deleted": True}


def _assignment(name, oid, role, scope=SCOPE):
    return {"id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}", "properties": {
        "principalId": oid, "scope": scope,
        "roleDefinitionId": f"/subscriptions/1/providers/Microsoft.Authorization/roleDefinitions/{role}"}}


class FakeArm:
    """One principal-filtered list and per-ID deletes; ``fail`` refuses the next DELETE of those IDs (the API identity)."""

    def __init__(self, items, fail=()):
        self.items, self.fail, self.deletes = list(items), set(fail), []

    def __call__(self, request):
        if request.method == "GET":
            oid = request.url.params["$filter"].split("'")[1]
            return httpx.Response(200, json={"value": [item for item in self.items
                                                       if item["properties"]["principalId"] == oid]})
        path = request.url.path
        self.deletes.append(path)
        if path in self.fail:
            self.fail.discard(path)
            return httpx.Response(403, json={"error": {"code": "AuthorizationFailed"}})
        before = len(self.items)
        self.items = [item for item in self.items if item["id"] != path]
        return httpx.Response(200 if len(self.items) < before else 204)


def _operator(case):
    return SimpleNamespace(runtime=case.a.runtime, config=case.config, verifier=SimpleNamespace(teams=case.teams),
                           installation=lambda: None)


def _roles(arm):
    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="arm-token"))
    return AgentRoleAssignments(credential, SCOPE, transport=httpx.MockTransport(arm))


def _arm_items():
    return [_assignment("fa-user", AGENT_OID, FOUNDRY_USER), _assignment("fa-openai", AGENT_OID, OPENAI_USER),
            _assignment("other-agent", OTHER_AGENT_OID, FOUNDRY_USER),
            _assignment("reader", AGENT_OID, "acdd72a7-3385-48ef-bd42-f606fba81ae7"),
            _assignment("parent", AGENT_OID, FOUNDRY_USER, scope=SCOPE.rsplit("/providers/", 1)[0])]


def test_preview_lists_exactly_the_targets_and_writes_or_deletes_nothing(team_agent):
    case = team_agent
    composition = _operator(case)
    foundry, arm = FakeFoundry(), FakeArm(_arm_items())
    guard = case.a.runtime.store._guard()
    result = preview(composition, case.target.agent_id, foundry=foundry, roles=_roles(arm))
    assert result["tombstone"] is None and result["agent_oid"] == AGENT_OID
    assert result["foundry_agent"] == {"name": case.target.agent_name, "state": "present", "versions": ["1", "2", "10"],
                                       "sessions": [{"id": "fa-session-1", "status": "idle"}]}
    assert [item["id"].rsplit("/", 1)[-1] for item in result["role_assignments"]] == ["fa-user", "fa-openai"]
    assert sorted(item["id"].rsplit("/", 1)[-1] for item in result["kept_role_assignments"]) == ["parent", "reader"]
    assert foundry.deleted == [] and arm.deletes == [] and case.a.runtime.store._guard() == guard
    configured = case.config.targets[1].agent_id
    with pytest.raises(Problem) as refused:
        preview(composition, configured, foundry=foundry, roles=_roles(arm))
    assert _code(refused) == (404, "TEAM_AGENT_NOT_FOUND")


def test_retire_needs_the_exact_confirmation(team_agent):
    case = team_agent
    composition = _operator(case)
    guard = case.a.runtime.store._guard()
    with pytest.raises(Problem) as unconfirmed:
        retire(composition, case.c.human, agent_id=case.target.agent_id, request_id="retire-cli", reason="dev test",
               confirm=case.config.targets[1].agent_id, foundry=FakeFoundry(), roles=_roles(FakeArm(_arm_items())))
    assert _code(unconfirmed) == (409, "RETIRE_CONFIRMATION_REQUIRED") and case.a.runtime.store._guard() == guard


def test_a_rerun_after_partial_deletion_converges_and_touches_no_other_agent(team_agent):
    case = team_agent
    composition = _operator(case)
    other = register_more_agents(case, 1)[0]
    foundry = FakeFoundry()
    arm = FakeArm(_arm_items(), fail={_assignment("fa-openai", AGENT_OID, OPENAI_USER)["id"]})
    kwargs = dict(agent_id=case.target.agent_id, request_id="retire-cli", reason="dev test agent",
                  confirm=case.target.agent_id, foundry=foundry, roles=_roles(arm), now=lambda: NOW)
    first = retire(composition, case.c.human, **kwargs)
    assert first["state"] == "partial" and first["tombstone"]["outcome"] == "written"
    assert first["foundry_agent"]["outcome"] == "deleted" and foundry.deleted == [case.target.agent_name]
    assert [item["outcome"].split(" ")[0] for item in first["role_assignments"]] == ["deleted", "refused"]
    assert [item["id"].rsplit("/", 1)[-1] for item in first["remaining"]["role_assignments"]] == ["fa-openai"]
    second = retire(composition, case.c.human, **kwargs)
    assert second["state"] == "ok" and second["tombstone"]["outcome"] == "already_retired"
    assert second["tombstone"]["request_id"] == "retire-cli"
    assert second["foundry_agent"] == {"name": case.target.agent_name, "state": "absent", "outcome": "already_deleted"}
    assert [(item["id"].rsplit("/", 1)[-1], item["outcome"]) for item in second["role_assignments"]] == [
        ("fa-openai", "deleted")]
    assert foundry.deleted == [case.target.agent_name]
    assert {item["id"].rsplit("/", 1)[-1] for item in arm.items} == {"other-agent", "reader", "parent"}
    assert all("other-agent" not in path for path in arm.deletes)
    store, config = case.a.runtime.store, case.config
    assert registered_agent_ids(store, config) == (other,) and retired_agent_ids(store, config) == (case.target.agent_id,)


def test_an_agent_already_gone_on_foundry_gets_its_tombstone(team_agent):
    """The 2026-10-15 dev case: the Foundry agent and its roles were removed by hand; the API still lists it."""
    case = team_agent
    composition = _operator(case)
    foundry, arm = FakeFoundry(present=False), FakeArm([_assignment("other-agent", OTHER_AGENT_OID, FOUNDRY_USER)])
    result = retire(composition, case.c.human, agent_id=case.target.agent_id, request_id="retire-dev-cc96",
                    reason="dev test agent deleted on Foundry 2026-10-15", confirm=case.target.agent_id,
                    foundry=foundry, roles=_roles(arm), now=lambda: NOW)
    assert result["state"] == "ok" and result["tombstone"]["outcome"] == "written"
    assert result["foundry_agent"] == {"name": case.target.agent_name, "state": "absent", "outcome": "already_deleted"}
    assert result["role_assignments"] == [] and foundry.deleted == [] and arm.deletes == []
    tombstone_only = retire(composition, case.c.human, agent_id=case.target.agent_id, request_id="retire-dev-cc96",
                            reason="dev test agent deleted on Foundry 2026-10-15", confirm=case.target.agent_id)
    assert tombstone_only == {"agent_id": case.target.agent_id, "state": "ok",
                              "tombstone": {**result["tombstone"], "outcome": "already_retired"}}


def test_foundry_cleanup_is_refused_when_another_roster_agent_has_the_same_project_and_name(team_agent):
    from test_team_registry import _target
    case = team_agent
    composition = _operator(case)
    twin = _target(2, OTHER_AGENT_OID, agent_name=case.target.agent_name)
    case.teams.register_agent(case.c.human, "routing-agent-twin", twin.agent_id, twin)
    foundry, arm = FakeFoundry(), FakeArm(_arm_items())
    kwargs = dict(request_id="retire-cli", reason="dev test agent", foundry=foundry, roles=_roles(arm), now=lambda: NOW)
    for _ in ("live twin", "retired twin"):
        guard = case.a.runtime.store._guard()
        with pytest.raises(Problem) as previewed:
            preview(composition, case.target.agent_id, foundry=foundry, roles=_roles(arm))
        with pytest.raises(Problem) as refused:
            retire(composition, case.c.human, agent_id=case.target.agent_id, confirm=case.target.agent_id, **kwargs)
        assert _code(previewed) == _code(refused) == (409, "TEAM_AGENT_CONFLICT")
        assert foundry.deleted == [] and arm.deletes == [] and case.a.runtime.store._guard() == guard
        # Tombstone only (no Foundry delete) stays allowed; afterwards the retired twin still blocks the delete.
        retire(composition, case.c.human, agent_id=twin.agent_id, request_id="retire-twin", reason="twin",
               confirm=twin.agent_id, now=lambda: NOW)
    assert retired_agent_ids(case.a.runtime.store, case.config) == (twin.agent_id,)


def _crash_before_registered(team, monkeypatch):
    """A real onboarding whose process dies after register_agent committed and before the 'registered' step."""
    real = onboarding._advance

    def crash(store, current, state, **kwargs):
        if state == "registered":
            raise RuntimeError("process died")
        return real(store, current, state, **kwargs)
    monkeypatch.setattr(onboarding, "_advance", crash)
    with pytest.raises(RuntimeError):
        team.run()
    monkeypatch.setattr(onboarding, "_advance", real)
    (agent_id,) = registered_agent_ids(team.a.runtime.store, team.config)
    assert onboarding_status(team.a, team.c.human, "onboard-one")["state"] == "creating"
    return agent_id


def test_an_agent_whose_onboarding_is_still_creating_is_not_retired_even_with_the_flag(team, monkeypatch):
    agent_id = _crash_before_registered(team, monkeypatch)
    composition, store = _operator(team), team.a.runtime.store
    assert _pending(team) == ["onboard-one"]
    guard = store._guard()
    for stuck in (False, True):
        with pytest.raises(Problem) as refused:
            retire(composition, team.c.human, agent_id=agent_id, request_id="retire-cli", reason="dev test",
                   confirm=agent_id, stuck=stuck, now=lambda: team.clock.wall() + timedelta(days=1))
        assert _code(refused) == (409, "AGENT_ONBOARDING_IN_PROGRESS") and store._guard() == guard
    # Without a tombstone the registry itself never releases a 'creating' onboarding, and its pending entry stays.
    ids = team.config.identity
    creating = onboarding._read(store, onboarding.onboarding_key(ids.tenant_id, ids.team_id, "onboard-one"))
    with pytest.raises(Problem) as direct:
        team.teams.retire_agent(team.c.human, "retire-cli", agent_id, "dev test", now=team.clock.wall(),
                                onboarding=creating)
    assert _code(direct) == (409, "TEAM_AGENT_CONFLICT") and store._guard() == guard
    assert _pending(team) == ["onboard-one"]
    # The same request resumes from the committed registration and finishes; then it retires the ordinary way.
    finished, done = team.run()
    assert finished and done["state"] == "ready" and done["agent_id"] == agent_id and _pending(team) == []
    assert retire(composition, team.c.human, agent_id=agent_id, request_id="retire-cli",
                  reason="dev test", confirm=agent_id)["tombstone"]["outcome"] == "written"


# ----- audit item 6: an onboarding stuck in 'registered'; item 5: a failed onboarding's leftover --------------------


def _stuck(team, monkeypatch):
    """A real onboarding left in 'registered': S5 refuses on every same-ID retry (e.g. the Team approved skill v2)."""
    real = onboarding._select_tools

    def stale(*args, **kwargs):
        raise Problem(409, "AGENT_ONBOARDING_SELECTION_STALE", "stale", cause="PROFILE_SELECTION_SOURCES_CHANGED")
    monkeypatch.setattr(onboarding, "_select_tools", stale)
    for _ in range(2):
        with pytest.raises(Problem):
            team.run()
    monkeypatch.setattr(onboarding, "_select_tools", real)
    status = onboarding_status(team.a, team.c.human, "onboard-one")
    assert status["state"] == "registered"
    return status["agent_id"]


def _pending(team):
    ids = team.config.identity
    return team.a.runtime.store.read_record(pending_key(ids.tenant_id, ids.team_id)).payload["data"]["request_ids"]


def test_a_stuck_registered_onboarding_is_retired_and_frees_its_pending_slot(team, monkeypatch):
    monkeypatch.setattr(onboarding, "MAX_TEAM_AGENTS", len(team.config.targets) + 2)
    agent_id = _stuck(team, monkeypatch)
    store, config = team.a.runtime.store, team.config
    assert _pending(team) == ["onboard-one"] and registered_agent_ids(store, config) == (agent_id,)
    with pytest.raises(Problem) as full:  # roster + pending: the stuck onboarding holds two slots
        team.run(rid="onboard-two", name="Writer")
    assert _code(full) == (409, "AGENT_QUOTA_REACHED")
    composition = _operator(team)
    kwargs = dict(agent_id=agent_id, request_id="retire-stuck", reason="onboarding stuck in registered",
                  confirm=agent_id)
    guard = store._guard()
    with pytest.raises(Problem) as recent:  # its create was just claimed: it may still be finishing
        retire(composition, team.c.human, now=team.clock.wall, **kwargs)
    assert _code(recent) == (409, "AGENT_ONBOARDING_IN_PROGRESS") and store._guard() == guard
    later = lambda: team.clock.wall() + timedelta(seconds=STUCK_ONBOARDING_SECONDS)  # noqa: E731
    assert preview(composition, agent_id, now=later)["stuck_onboarding"] == {
        "request_id": "onboard-one", "state": "registered"}
    assert store._guard() == guard

    def readers():  # the real startup readers, as in test_agent_retire.py: the retire changes none of their outcomes
        migration = MigrationStore(team.a.clients[0], config.table.scope)
        return (outcome_of(lambda: read_startup(team.c)), outcome_of(lambda: _quiescence(migration.read_inventory(),
                                                                                        config)),
                outcome_of(lambda: installed_origin(migration, config)))
    baseline = readers()
    assert baseline[0] == "ok"
    result = retire(composition, team.c.human, now=later, **kwargs)
    assert result["tombstone"]["outcome"] == "written" and result["tombstone"]["onboarding_request_id"] == "onboard-one"
    assert _pending(team) == [] and onboarding_status(team.a, team.c.human, "onboard-one")["state"] == "registered"
    assert (registered_agent_ids(store, config), retired_agent_ids(store, config)) == ((), (agent_id,))
    assert agent_id not in {entry.agent_id for entry in public_agents(team.a, team.c.human).agents}
    validate_team_registry_history(store.read_inventory(), config)
    assert readers() == baseline
    # A rerun (the explicit flag, any time) returns the original tombstone; the same onboarding body is now refused.
    again = retire(composition, team.c.human, stuck=True, now=team.clock.wall, **kwargs)
    assert again["tombstone"] == {**result["tombstone"], "outcome": "already_retired"}
    with pytest.raises(Problem) as resumed:
        team.run()
    assert _code(resumed) == (410, "AGENT_RETIRED") and _pending(team) == []
    # One slot is back (the roster slot stays, as ADR-0006 says): the next onboarding is admitted and finishes.
    finished, value = team.run(rid="onboard-two", name="Writer")
    assert finished and value["state"] == "ready"
    read_startup(team.c)


def test_the_explicit_flag_retires_a_stuck_onboarding_whose_create_is_recent(team, monkeypatch):
    agent_id = _stuck(team, monkeypatch)
    composition = _operator(team)
    result = retire(composition, team.c.human, agent_id=agent_id, request_id="retire-stuck", reason="stuck",
                    confirm=agent_id, stuck=True, now=team.clock.wall)
    assert result["state"] == "ok" and result["tombstone"]["onboarding_request_id"] == "onboard-one"
    assert _pending(team) == []
    validate_team_registry_history(team.a.runtime.store.read_inventory(), team.config)


def test_a_stuck_retire_of_an_already_tombstoned_agent_still_frees_its_pending_slot(team, monkeypatch):
    agent_id = _crash_before_registered(team, monkeypatch)
    store, config = team.a.runtime.store, team.config
    # An earlier CLI (before this check) tombstoned it the ordinary way while it was 'creating'.
    old, _ = team.teams.retire_agent(team.c.human, "retire-old", agent_id, "old retire", now=team.clock.wall())
    with pytest.raises(Problem) as resumed:  # the owner's resend reaches 'registered', then the retired Agent 410s
        team.run()
    assert _code(resumed) == (410, "AGENT_RETIRED") and _pending(team) == ["onboard-one"]
    assert onboarding_status(team.a, team.c.human, "onboard-one")["state"] == "registered"
    validate_team_registry_history(store.read_inventory(), config)
    composition = _operator(team)
    kwargs = dict(agent_id=agent_id, request_id="retire-stuck", reason="stuck", confirm=agent_id, stuck=True)
    result = retire(composition, team.c.human, **kwargs)
    assert result["tombstone"] == {"outcome": "already_retired", **old.model_dump(mode="json")}
    assert _pending(team) == []
    guard = store._guard()
    assert retire(composition, team.c.human, **kwargs) == result and store._guard() == guard  # rerun writes nothing
    validate_team_registry_history(store.read_inventory(), config)
    read_startup(team.c)
    finished, value = team.run(rid="onboard-two", name="Writer")
    assert finished and value["state"] == "ready"


def test_an_agent_tombstoned_while_creating_reads_as_retired_and_frees_its_pending_slot_once(team, monkeypatch):
    agent_id = _crash_before_registered(team, monkeypatch)
    store, config = team.a.runtime.store, team.config
    # An earlier CLI (before the onboarding check) tombstoned it while it was 'creating'; no resend since.
    old, _ = team.teams.retire_agent(team.c.human, "retire-old", agent_id, "old retire", now=team.clock.wall())
    assert _pending(team) == ["onboard-one"]
    composition = _operator(team)
    guard = store._guard()
    shown = preview(composition, agent_id)
    assert shown["tombstone"] == old.model_dump(mode="json") and store._guard() == guard
    assert shown["stuck_onboarding"] == {"request_id": "onboard-one", "state": "creating"}
    kwargs = dict(agent_id=agent_id, request_id="retire-new", reason="free the slot", confirm=agent_id)
    result = retire(composition, team.c.human, **kwargs)
    assert result["tombstone"] == {"outcome": "already_retired", **old.model_dump(mode="json")}
    assert _pending(team) == [] and onboarding_status(team.a, team.c.human, "onboard-one")["state"] == "creating"
    guard = store._guard()
    assert retire(composition, team.c.human, **kwargs) == result and store._guard() == guard  # rerun writes nothing
    validate_team_registry_history(store.read_inventory(), config)
    read_startup(team.c)
    with pytest.raises(Problem) as resumed:  # the owner's resend still ends at the retired Agent
        team.run()
    assert _code(resumed) == (410, "AGENT_RETIRED") and _pending(team) == []
    validate_team_registry_history(store.read_inventory(), config)
    finished, value = team.run(rid="onboard-two", name="Writer")
    assert finished and value["state"] == "ready"


def test_the_stuck_hour_counts_from_entering_registered_not_from_the_create_claim(team, monkeypatch):
    from test_agent_onboarding import SetupSpy
    spy = SetupSpy("step=roles reason=refused role=r status=403 code=AuthorizationFailed")
    finished, value = team.run(setup=spy)
    assert not finished and value["reason"] == "AGENT_SETUP_PENDING"
    team.clock.now += STUCK_ONBOARDING_SECONDS + 60  # setup stays pending for over an hour, then completes
    monkeypatch.setattr(onboarding, "default_setup", lambda composition: spy)
    agent_id = _stuck(team, monkeypatch)
    composition, store = _operator(team), team.a.runtime.store
    kwargs = dict(agent_id=agent_id, request_id="retire-stuck", reason="stuck", confirm=agent_id)
    guard = store._guard()
    with pytest.raises(Problem) as recent:  # its create claim is over an hour old, but it registered just now
        retire(composition, team.c.human, now=team.clock.wall, **kwargs)
    assert _code(recent) == (409, "AGENT_ONBOARDING_IN_PROGRESS") and store._guard() == guard
    later = lambda: team.clock.wall() + timedelta(seconds=STUCK_ONBOARDING_SECONDS)  # noqa: E731
    assert retire(composition, team.c.human, now=later, **kwargs)["tombstone"]["outcome"] == "written"
    assert _pending(team) == []
    validate_team_registry_history(store.read_inventory(), team.config)


class LeftoverFoundry(OnboardingFoundry):
    """The onboarding fake plus what the retire command reads and deletes."""

    def __init__(self, a_target):
        super().__init__(a_target)
        self.deleted = []

    def get_agent(self, name):
        if name not in self.versions:
            raise FoundryCallError(404)
        return {"name": name}

    def list_sessions(self, name):
        return []

    def delete_agent(self, name):
        self.deleted.append(name)
        self.versions.pop(name)
        return {"name": name, "deleted": True}


def test_a_failed_onboardings_leftover_agent_is_previewed_and_deleted_alone(team, caplog):
    foundry = LeftoverFoundry(team.config.targets[0])
    team.foundry.versions = foundry.versions  # one shared Foundry
    team.foundry.final_status = "failed"
    finished, failed = team.run()
    assert finished and (failed["state"], failed["reason"]) == ("failed", "FOUNDRY_VERSION_FAILED")
    team.foundry.final_status = "active"
    finished, ready = team.run(rid="onboard-two", name="Writer")
    assert finished and ready["state"] == "ready"
    composition, store = _operator(team), team.a.runtime.store
    guard = store._guard()
    name = failed["agent_name"]
    previewed = preview_failed(composition, name, foundry=foundry)
    assert previewed == {"agent_name": name, "onboarding": {"request_id": "onboard-one", "state": "failed",
                                                            "reason": "FOUNDRY_VERSION_FAILED"},
                         "foundry_agent": {"name": name, "state": "present", "versions": ["1"], "sessions": []}}
    for other in (ready["agent_name"], team.config.targets[0].agent_name):
        with pytest.raises(Problem) as refused:
            preview_failed(composition, other, foundry=foundry)
        assert _code(refused) == (409, "TEAM_AGENT_CONFLICT")
    with pytest.raises(Problem) as unconfirmed:
        retire_failed(composition, team.c.human, agent_name=name, confirm=ready["agent_name"], foundry=foundry)
    assert _code(unconfirmed) == (409, "RETIRE_CONFIRMATION_REQUIRED") and foundry.deleted == []
    with caplog.at_level(logging.WARNING):
        done = retire_failed(composition, team.c.human, agent_name=name, confirm=name, foundry=foundry)
    assert done["state"] == "ok" and done["foundry_agent"]["outcome"] == "deleted" and foundry.deleted == [name]
    assert f"FA_RETIRE_FAILED_LEFTOVER agent_name={name} request_id=onboard-one outcome=deleted" in caplog.text
    again = retire_failed(composition, team.c.human, agent_name=name, confirm=name, foundry=foundry)
    assert again == {"agent_name": name, "state": "ok",
                     "foundry_agent": {"name": name, "state": "absent", "outcome": "already_deleted"}}
    assert foundry.deleted == [name] and ready["agent_name"] in foundry.versions and store._guard() == guard


def test_a_leftover_whose_versions_name_another_onboarding_is_refused(team):
    foundry = LeftoverFoundry(team.config.targets[0])
    team.foundry.versions = foundry.versions
    finished, ready = team.run()
    assert finished
    name = derived_names(team.config.identity.tenant_id, team.config.identity.team_id, "onboard-gone")["agent_name"]
    foundry.versions[name] = [{**foundry.versions[ready["agent_name"]][0], "name": name}]
    with pytest.raises(Problem) as mismatch:
        preview_failed(_operator(team), name, foundry=foundry)
    assert _code(mismatch) == (409, "FAILED_ONBOARDING_MISMATCH") and foundry.deleted == []