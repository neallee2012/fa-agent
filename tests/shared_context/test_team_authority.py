"""T6a/T6b Team settings/ledger rows; all storage and tokens are local fakes, not Azure proof."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import TENANT
from shared_context.behavior_auth import APPROVAL_SCOPE, READ_SCOPE, BehaviorAuthSettings, BehaviorJwtVerifier
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_store import BehaviorStore
from shared_context.behavior_server_config import TeamDefaults
from shared_context.behavior_team_authority import MAX_TEAM_NARROWINGS, TeamAuthority, TeamUsageLedger, initialize_team, narrow_team, read_team_authority, team_authority_key, team_ledger_key, team_root_budget_id
from shared_context.behavior_team_authority_operator import operate
from shared_context.behavior_team_registry import TeamRegistry
from shared_context.errors import Problem
from test_behavior_store import FakeTable, SCOPE
from test_conversation_transfer_startup import read_startup, synthetic_standing_v1
from test_m3_auth import CLIENT_ID, HUMAN_OID, jwk_client, signing_key, token

SECOND_OWNER = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
# Organization defaults (T6b): an operator and an owner can only keep or lower them.
DEFAULTS = TeamDefaults(model_cap_microusd=10_000_000, max_agents=8, connectors=("crm", "mail"))
SETTINGS = {"defaults": DEFAULTS, "model_cap_microusd": 5_000_000, "max_agents": 4}


@pytest.fixture
def teams(auth):
    table = FakeTable()
    store = BehaviorStore(table, SCOPE)
    store.initialize()
    registry = TeamRegistry(store, tenant_id=TENANT, operator_oid=HUMAN_OID,
                            legacy_team_id="team-one", legacy_owner_oid=HUMAN_OID, agent_oids=tuple(auth.actors))
    operator = BehaviorPrincipal(TENANT, HUMAN_OID, "team-one", "human", None,
                                 frozenset({"read", "submit", "approve"}))
    registry.register_owner(operator, "register-owner-two", "team-two", SECOND_OWNER)
    return SimpleNamespace(store=store, table=table, registry=registry, operator=operator, auth=auth)


def test_initialization_creates_settings_and_empty_ledger_once(teams):
    c = teams
    receipt = initialize_team(c.store, c.registry, c.operator, "init-team-two", "team-two", **SETTINGS)
    akey, lkey = team_authority_key(SCOPE.authority_domain, "team-two"), team_ledger_key(SCOPE.authority_domain, "team-two")
    assert set(dict(receipt.payload["writes"])) == {akey, lkey}
    assert receipt.payload["result"] == {"team_id": "team-two", "runtime_activated": False,
                                         "root_budget_id": team_root_budget_id(TENANT, "team-two")}
    value, ledger = read_team_authority(c.store, SCOPE.authority_domain, "team-two")
    # Connectors not named inherit the organization's approved list (Q5 B).
    assert (value.owner_oid, value.model_cap_microusd, value.max_agents, value.connectors) == (
        SECOND_OWNER, 5_000_000, 4, ("crm", "mail"))
    assert ledger == TeamUsageLedger(tenant_id=TENANT, team_id="team-two", authority_domain=SCOPE.authority_domain)
    count = len(c.table.submissions)
    assert initialize_team(c.store, c.registry, c.operator, "init-team-two", "team-two", **SETTINGS) == receipt
    assert len(c.table.submissions) == count
    guard = c.store._guard()
    with pytest.raises(Problem) as again:
        initialize_team(c.store, c.registry, c.operator, "init-team-two-again", "team-two", **SETTINGS)
    assert again.value.body["error"]["code"] == "TEAM_AUTHORITY_EXISTS" and c.store._guard() == guard
    with pytest.raises(Problem) as changed:
        initialize_team(c.store, c.registry, c.operator, "init-team-two", "team-two", defaults=DEFAULTS,
                        model_cap_microusd=9_000_000, max_agents=4)
    assert changed.value.body["error"]["code"] == "BEHAVIOR_REQUEST_CONFLICT" and len(c.table.submissions) == count


def test_original_team_unregistered_or_inactive_owner_and_bad_settings_write_nothing(teams):
    c = teams
    guard = c.store._guard()
    for index, (team, settings, code) in enumerate((
        ("team-one", SETTINGS, "TEAM_AUTHORITY_LEGACY"),
        ("team-three", SETTINGS, "TEAM_OWNER_REQUIRED"),
        ("team-two", {**SETTINGS, "model_cap_microusd": 100_000_001}, "TEAM_AUTHORITY_COMMAND"),
        ("team-two", {**SETTINGS, "model_cap_microusd": 0}, "TEAM_AUTHORITY_COMMAND"),
        ("team-two", {**SETTINGS, "max_agents": 17}, "TEAM_AUTHORITY_COMMAND"),
        ("team-two", {**SETTINGS, "connectors": ("mail", "mail")}, "TEAM_AUTHORITY_COMMAND"),
        ("bad team", SETTINGS, "TEAM_AUTHORITY_COMMAND"),
    )):
        with pytest.raises(Problem) as refused:
            initialize_team(c.store, c.registry, c.operator, f"init-bad-{index}", team, **settings)
        assert refused.value.body["error"]["code"] == code
    assert c.store._guard() == guard
    c.registry.deactivate_owner(c.operator, "deactivate-owner-two", SECOND_OWNER)
    guard = c.store._guard()
    with pytest.raises(Problem) as inactive:
        initialize_team(c.store, c.registry, c.operator, "init-inactive", "team-two", **SETTINGS)
    assert inactive.value.body["error"]["code"] == "TEAM_OWNER_REQUIRED" and c.store._guard() == guard
    assert read_team_authority(c.store, SCOPE.authority_domain, "team-two") is None


@pytest.mark.parametrize("change", [
    {"oid": SECOND_OWNER}, {"team": "team-two"}, {"kind": "agent", "actor_id": "agent-a"},
    {"actions": frozenset({"read"})},
])
def test_only_the_original_operator_can_initialize(teams, change):
    c = teams
    count = len(c.table.submissions)
    with pytest.raises(Problem) as denied:
        initialize_team(c.store, c.registry, replace(c.operator, **change), "init-denied", "team-two", **SETTINGS)
    assert denied.value.status == 403 and len(c.table.submissions) == count


def test_lost_ack_reconciles_the_original_request_without_writing_again(teams):
    c = teams
    c.table.failure = "after"
    with pytest.raises(Problem) as lost:
        initialize_team(c.store, c.registry, c.operator, "init-team-two", "team-two", **SETTINGS)
    assert lost.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    c.table.failure = None
    count = len(c.table.submissions)
    receipt = initialize_team(c.store, c.registry, c.operator, "init-team-two", "team-two", reconcile=True, **SETTINGS)
    assert receipt.payload["result"]["team_id"] == "team-two" and len(c.table.submissions) == count


def test_operator_cli_initializes_shows_and_reconciles(teams, jwk_client, signing_key):
    c = teams
    installed = []
    verifier = BehaviorJwtVerifier(c.auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, "team-one"),
                                   jwk_client, teams=c.registry)
    composition = SimpleNamespace(verifier=verifier, installation=lambda: installed.append(True),
                                  config=SimpleNamespace(team_defaults=DEFAULTS))
    authorization = "Bearer " + token(signing_key, {"scp": READ_SCOPE + " " + APPROVAL_SCOPE})
    assert operate(composition, "show", authorization, team_id="team-two")["state"] == "missing"
    cli = {key: value for key, value in SETTINGS.items() if key != "defaults"}
    saved = operate(composition, "init", authorization, team_id="team-two", request_id="init-via-cli",
                    connectors=("mail",), **cli)
    assert saved["result"]["team_id"] == "team-two" and installed == [True]
    assert operate(composition, "reconcile", authorization, team_id="team-two", request_id="init-via-cli",
                   connectors=("mail",), **cli) == saved and installed == [True]
    shown = operate(composition, "show", authorization, team_id="team-two")
    assert shown["team"]["connectors"] == ["mail"] and shown["ledger"]["model_metered_microusd"] == 0
    owner = "Bearer " + token(signing_key, {"oid": SECOND_OWNER, "scp": READ_SCOPE + " " + APPROVAL_SCOPE})
    with pytest.raises(Problem) as denied:
        operate(composition, "init", owner, team_id="team-two", request_id="owner-cannot", **cli)
    assert denied.value.status == 403


OWNER = BehaviorPrincipal(TENANT, SECOND_OWNER, "team-two", "human", None, frozenset({"read", "submit", "approve"}))


def _initialized(c, **settings):
    initialize_team(c.store, c.registry, c.operator, "init-team-two", "team-two", **{"defaults": DEFAULTS, **settings})
    return read_team_authority(c.store, SCOPE.authority_domain, "team-two")


def test_a_new_team_starts_from_the_organization_defaults_and_can_only_lower_them(teams):
    c = teams
    guard = c.store._guard()
    for index, (settings, code) in enumerate((
        ({"defaults": None}, "TEAM_DEFAULTS_UNCONFIGURED"),
        ({"defaults": DEFAULTS, "model_cap_microusd": 10_000_001}, "TEAM_SETTINGS_ABOVE_DEFAULT"),
        ({"defaults": DEFAULTS, "max_agents": 9}, "TEAM_SETTINGS_ABOVE_DEFAULT"),
        ({"defaults": DEFAULTS, "connectors": ("crm", "billing")}, "TEAM_SETTINGS_ABOVE_DEFAULT"),
    )):
        with pytest.raises(Problem) as refused:
            initialize_team(c.store, c.registry, c.operator, f"init-above-{index}", "team-two", **settings)
        assert refused.value.body["error"]["code"] == code
    assert c.store._guard() == guard
    value, _ = _initialized(c)
    assert value.limits.model_dump(mode="json") == DEFAULTS.model_dump(mode="json")
    assert "narrowings" not in value.model_dump(mode="json")


def test_an_explicit_empty_connector_list_is_kept(teams):
    value, _ = _initialized(teams, connectors=(), max_agents=2)
    assert (value.connectors, value.max_agents, value.model_cap_microusd) == ((), 2, DEFAULTS.model_cap_microusd)


def test_the_owner_lowers_absolute_values_once_per_request_id(teams):
    c = teams
    _initialized(c)
    receipt = narrow_team(c.store, c.registry, OWNER, "owner-lower-1", "team-two", model_cap_microusd=3_000_000,
                          connectors=("mail",))
    assert receipt.payload["result"] == {"team_id": "team-two", "narrowings": 1, "model_cap_microusd": 3_000_000,
                                         "max_agents": 8, "connectors": ["mail"]}
    count = len(c.table.submissions)
    # Same ID and content: the original receipt, no second write. Absolute values never compound.
    assert narrow_team(c.store, c.registry, OWNER, "owner-lower-1", "team-two", model_cap_microusd=3_000_000,
                       connectors=("mail",)) == receipt
    assert len(c.table.submissions) == count
    with pytest.raises(Problem) as changed:
        narrow_team(c.store, c.registry, OWNER, "owner-lower-1", "team-two", model_cap_microusd=2_000_000)
    assert changed.value.body["error"]["code"] == "BEHAVIOR_REQUEST_CONFLICT" and len(c.table.submissions) == count
    value, ledger = read_team_authority(c.store, SCOPE.authority_domain, "team-two")
    assert value.granted.model_dump(mode="json") == DEFAULTS.model_dump(mode="json")
    assert value.limits.connectors == ("mail",) and value.limits.model_cap_microusd == 3_000_000
    assert ledger == TeamUsageLedger(tenant_id=TENANT, team_id="team-two", authority_domain=SCOPE.authority_domain)


@pytest.mark.parametrize("raised", [
    {"model_cap_microusd": 3_000_001}, {"max_agents": 9}, {"connectors": ("mail", "crm")}, {"connectors": ("billing",)},
])
def test_the_owner_can_never_raise_a_value(teams, raised):
    c = teams
    _initialized(c)
    narrow_team(c.store, c.registry, OWNER, "owner-lower-1", "team-two", model_cap_microusd=3_000_000,
                connectors=("mail",))
    guard = c.store._guard()
    with pytest.raises(Problem) as refused:
        narrow_team(c.store, c.registry, OWNER, "owner-raise", "team-two", **raised)
    assert refused.value.body["error"]["code"] == "TEAM_SETTINGS_RAISED" and c.store._guard() == guard


def test_only_the_active_owner_of_that_team_lowers_it(teams):
    c = teams
    _initialized(c)
    count = len(c.table.submissions)
    for principal in (c.operator, replace(OWNER, oid=HUMAN_OID), replace(OWNER, kind="agent", actor_id="agent-a"),
                      replace(OWNER, actions=frozenset({"read"}))):
        with pytest.raises(Problem) as denied:
            narrow_team(c.store, c.registry, principal, "owner-denied", "team-two", max_agents=1)
        assert denied.value.status == 403
    with pytest.raises(Problem) as empty:
        narrow_team(c.store, c.registry, OWNER, "owner-empty", "team-two")
    assert empty.value.body["error"]["code"] == "TEAM_AUTHORITY_COMMAND"
    c.registry.deactivate_owner(c.operator, "deactivate-owner-two", SECOND_OWNER)
    count = len(c.table.submissions)
    with pytest.raises(Problem) as inactive:
        narrow_team(c.store, c.registry, OWNER, "owner-inactive", "team-two", max_agents=1)
    assert inactive.value.status == 403 and len(c.table.submissions) == count


def test_the_seventeenth_owner_step_is_refused_and_keeps_every_earlier_step(teams):
    c = teams
    _initialized(c)
    for step in range(MAX_TEAM_NARROWINGS):
        narrow_team(c.store, c.registry, OWNER, f"owner-step-{step}", "team-two",
                    model_cap_microusd=DEFAULTS.model_cap_microusd - step - 1)
    before, _ = read_team_authority(c.store, SCOPE.authority_domain, "team-two")
    with pytest.raises(Problem) as full:
        narrow_team(c.store, c.registry, OWNER, "owner-step-extra", "team-two", max_agents=1)
    assert full.value.body["error"]["code"] == "TEAM_NARROWING_LIMIT"
    after, _ = read_team_authority(c.store, SCOPE.authority_domain, "team-two")
    assert after == before and len(after.narrowings) == MAX_TEAM_NARROWINGS


def test_a_lost_owner_ack_is_read_back_without_writing_again(teams):
    c = teams
    _initialized(c)
    c.table.failure = "after"
    with pytest.raises(Problem) as lost:
        narrow_team(c.store, c.registry, OWNER, "owner-lost", "team-two", max_agents=2)
    assert lost.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    c.table.failure = None
    count = len(c.table.submissions)
    receipt = narrow_team(c.store, c.registry, OWNER, "owner-lost", "team-two", max_agents=2, reconcile=True)
    assert receipt.payload["result"]["max_agents"] == 2 and len(c.table.submissions) == count


def test_owner_cli_lowers_and_reconciles_but_the_operator_token_cannot(teams, jwk_client, signing_key):
    c = teams
    verifier = BehaviorJwtVerifier(c.auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, "team-one"),
                                   jwk_client, teams=c.registry)
    composition = SimpleNamespace(verifier=verifier, installation=lambda: None,
                                  config=SimpleNamespace(team_defaults=DEFAULTS))
    _initialized(c)
    owner = "Bearer " + token(signing_key, {"oid": SECOND_OWNER, "scp": READ_SCOPE + " " + APPROVAL_SCOPE})
    saved = operate(composition, "narrow", owner, team_id="team-two", request_id="cli-lower", max_agents=3)
    assert saved["result"]["max_agents"] == 3
    assert operate(composition, "narrow-reconcile", owner, team_id="team-two", request_id="cli-lower",
                   max_agents=3) == saved
    assert operate(composition, "show", "Bearer " + token(signing_key, {"scp": READ_SCOPE}),
                   team_id="team-two")["limits"]["max_agents"] == 3
    operator = "Bearer " + token(signing_key, {"scp": READ_SCOPE + " " + APPROVAL_SCOPE})
    with pytest.raises(Problem) as denied:
        operate(composition, "narrow", operator, team_id="team-two", request_id="operator-cannot", max_agents=1)
    assert denied.value.status == 403


def test_server_configurations_without_team_defaults_keep_their_digest(synthetic_standing_v1):
    from shared_context.behavior_server_config import ServerConfigurationDocument
    config = synthetic_standing_v1.h.config
    assert config.team_defaults is None and "team_defaults" not in config.model_dump(mode="json")
    with_defaults = type(config).model_validate_json(
        config.model_copy(update={"team_defaults": DEFAULTS}).model_dump_json())
    assert with_defaults.team_defaults == DEFAULTS and with_defaults.sha256 != config.sha256
    assert ServerConfigurationDocument.model_validate_json(with_defaults.model_dump_json()) == with_defaults


def test_owner_steps_narrow_the_stored_limits(synthetic_standing_v1):
    c = synthetic_standing_v1
    ids = c.h.config.identity
    registry = TeamRegistry(c.control.store, tenant_id=ids.tenant_id, operator_oid=ids.human_oid,
                            legacy_team_id=ids.team_id, legacy_owner_oid=ids.human_oid,
                            agent_oids=tuple(agent.oid for agent in ids.agents))
    registry.register_owner(c.human, "startup-owner", "another-team", SECOND_OWNER)
    initialize_team(c.control.store, registry, c.human, "startup-team", "another-team", **SETTINGS)
    owner = BehaviorPrincipal(ids.tenant_id, SECOND_OWNER, "another-team", "human", None,
                              frozenset({"read", "approve"}))
    narrow_team(c.control.store, registry, owner, "startup-lower-1", "another-team", connectors=("crm",))
    narrow_team(c.control.store, registry, owner, "startup-lower-2", "another-team", model_cap_microusd=1)
    value, _ = read_team_authority(c.control.store, c.h.config.table.scope.authority_domain, "another-team")
    assert value.limits.model_dump(mode="json") == {"model_cap_microusd": 1, "max_agents": 4, "connectors": ["crm"]}


def test_owners_of_two_teams_may_use_the_same_request_id(teams):
    c = teams
    third = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    c.registry.register_owner(c.operator, "register-owner-three", "team-three", third)
    _initialized(c)
    initialize_team(c.store, c.registry, c.operator, "init-team-three", "team-three", defaults=DEFAULTS)
    two = narrow_team(c.store, c.registry, OWNER, "lower-1", "team-two", max_agents=2)
    owner_three = BehaviorPrincipal(TENANT, third, "team-three", "human", None, frozenset({"read", "approve"}))
    three = narrow_team(c.store, c.registry, owner_three, "lower-1", "team-three", max_agents=3)
    assert (two.payload["result"]["max_agents"], three.payload["result"]["max_agents"]) == (2, 3)
    assert narrow_team(c.store, c.registry, owner_three, "lower-1", "team-three", max_agents=3,
                       reconcile=True) == three
