"""Team ownership and signed identity checks; all storage and tokens are local fakes."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from conftest import A, TENANT
from shared_context.behavior_auth import (
    AGENT_ROLE, APPROVAL_SCOPE, READ_SCOPE, BehaviorAuthSettings, BehaviorJwtVerifier, BehaviorPrincipal,
)
from shared_context.behavior_agent_message_wire import MAX_TEAM_AGENTS
from shared_context.behavior_foundry import FoundryTarget
from shared_context.behavior_operations import _row
from shared_context.behavior_team_registry import (
    IdentityRegistration, TeamAgent, TeamAgents, TeamOwnership, TeamRegistry, agent_key, identity_key,
    roster_key, team_key, validate_team_registry_history,
)
from shared_context.behavior_team_registry_operator import operate
from shared_context.errors import Problem
from test_behavior_store import FakeTable, SCOPE
from shared_context.behavior_store import BehaviorStore
from test_behavior_server import workspace
from test_conversation_transfer_startup import read_startup, synthetic_standing_v1
from test_m3_auth import CLIENT_ID, HUMAN_OID, UNKNOWN_OID, jwk_client, request, signing_key, token

SECOND_OWNER = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


@pytest.fixture
def registry(auth):
    table = FakeTable()
    store = BehaviorStore(table, SCOPE)
    store.initialize()
    value = TeamRegistry(store, tenant_id=TENANT, operator_oid=HUMAN_OID,
                         legacy_team_id="team-one", legacy_owner_oid=HUMAN_OID, agent_oids=tuple(auth.actors))
    operator = BehaviorPrincipal(TENANT, HUMAN_OID, "team-one", "human", None,
                                 frozenset({"read", "submit", "approve"}))
    return SimpleNamespace(value=value, store=store, table=table, operator=operator, auth=auth)


def _verifier(c, jwk_client):
    return BehaviorJwtVerifier(c.auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, "team-one"),
                               jwk_client, teams=c.value)


def test_registration_is_two_create_only_rows_in_one_original_cas(registry):
    c = registry
    receipt = c.value.register_owner(c.operator, "register-owner-two", "team-two", SECOND_OWNER)
    tkey, ikey = team_key(TENANT, "team-two"), identity_key(TENANT, SECOND_OWNER)
    assert set(dict(receipt.payload["writes"])) == {tkey, ikey}
    current = c.store.read_snapshot((tkey, ikey))
    assert _row(current, tkey, TeamOwnership).owner_oid == SECOND_OWNER
    assert _row(current, ikey, IdentityRegistration).team_id == "team-two"
    count = len(c.table.submissions)
    assert c.value.register_owner(c.operator, "register-owner-two", "team-two", SECOND_OWNER) == receipt
    assert len(c.table.submissions) == count


@pytest.mark.parametrize("owner,team", [
    (SECOND_OWNER, "team-three"), (UNKNOWN_OID, "team-two"),
    (HUMAN_OID, "team-two"), (SECOND_OWNER, "team-one"), (A.oid, "team-three"),
])
def test_owner_team_and_configured_agent_cannot_be_reassigned(registry, owner, team):
    c = registry
    c.value.register_owner(c.operator, "register-owner-two", "team-two", SECOND_OWNER)
    guard = c.store._guard()
    with pytest.raises(Problem) as failure:
        c.value.register_owner(c.operator, "conflicting-owner", team, owner)
    assert failure.value.status == 409 and c.store._guard() == guard


def test_lost_registration_ack_reconciles_original_id_without_writing_again(registry):
    c = registry
    c.table.failure = "after"
    with pytest.raises(Problem) as failure:
        c.value.register_owner(c.operator, "register-owner-two", "team-two", SECOND_OWNER)
    assert failure.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    count = len(c.table.submissions)
    c.table.failure = None
    receipt = c.value.register_owner(c.operator, "register-owner-two", "team-two", SECOND_OWNER, reconcile=True)
    assert receipt.payload["result"]["owner_oid"] == SECOND_OWNER
    assert len(c.table.submissions) == count
    with pytest.raises(Problem):
        c.value.register_owner(c.operator, "not-the-original-id", "team-two", SECOND_OWNER, reconcile=True)


def test_same_request_cannot_change_owner_or_leave_half_of_a_competing_pair(registry):
    c = registry
    c.table.before_submit = lambda: c.value.register_owner(c.operator, "winning-request", "team-two", SECOND_OWNER)
    with pytest.raises(Problem) as conflict:
        c.value.register_owner(c.operator, "losing-request", "team-two", UNKNOWN_OID)
    assert conflict.value.status == 412
    assert c.value.lookup(UNKNOWN_OID) is None
    assert c.value.lookup(SECOND_OWNER).team_id == "team-two"
    count = len(c.table.submissions)
    with pytest.raises(Problem) as changed:
        c.value.register_owner(c.operator, "winning-request", "team-three", SECOND_OWNER)
    assert changed.value.body["error"]["code"] == "BEHAVIOR_REQUEST_CONFLICT"
    assert len(c.table.submissions) == count


@pytest.mark.parametrize("change", [
    {"oid": SECOND_OWNER}, {"team": "team-two"}, {"tenant_id": SECOND_OWNER},
    {"kind": "agent", "actor_id": "agent-a"}, {"actions": frozenset({"read"})},
])
def test_only_original_verified_operator_can_register_or_deactivate(registry, change):
    c = registry
    principal = replace(c.operator, **change)
    count = len(c.table.submissions)
    with pytest.raises(Problem) as failure:
        c.value.register_owner(principal, "denied", "team-two", SECOND_OWNER)
    assert failure.value.status == 403 and len(c.table.submissions) == count


@pytest.mark.parametrize("version", ["1.0", "2.0"])
def test_signed_owner_uses_one_oid_read_and_not_untrusted_team_claim(registry, jwk_client, signing_key, version):
    c = registry
    c.value.register_owner(c.operator, "register-owner-two", "team-two", SECOND_OWNER)
    changes = {"oid": SECOND_OWNER, "team": "team-one", "scp": READ_SCOPE, "ver": version}
    if version == "2.0":
        changes.update(iss=f"https://login.microsoftonline.com/{TENANT}/v2.0", azp=CLIENT_ID)
    bearer = request(token(signing_key, changes, remove=("appid",) if version == "2.0" else ()))
    c.table.reads.clear()
    principal = _verifier(c, jwk_client)(bearer)
    assert (principal.oid, principal.team, principal.kind) == (SECOND_OWNER, "team-two", "human")
    assert c.table.reads == [(SCOPE.partition_key, "behavior:v1:row:" + identity_key(TENANT, SECOND_OWNER))]
    principal.require("team-two", "submit")
    with pytest.raises(Problem):
        principal.require("team-one", "submit")
    with pytest.raises(Problem):
        principal.require("team-two", "execute")


def test_unknown_inactive_and_bad_tokens_never_fall_back_to_original_owner(registry, jwk_client, signing_key):
    c = registry
    verifier = _verifier(c, jwk_client)
    bearer = token(signing_key, {"oid": SECOND_OWNER, "scp": READ_SCOPE})
    with pytest.raises(Problem) as unknown:
        verifier(request(bearer))
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_HUMAN_NOT_ALLOWED"
    c.value.register_owner(c.operator, "register-owner-two", "team-two", SECOND_OWNER)
    assert verifier(request(bearer)).team == "team-two"
    c.value.deactivate_owner(c.operator, "deactivate-owner-two", SECOND_OWNER)
    c.table.reads.clear()
    with pytest.raises(Problem) as inactive:
        verifier(request(bearer))
    assert inactive.value.body["error"]["code"] == "BEHAVIOR_HUMAN_NOT_ALLOWED"
    assert len(c.table.reads) == 1
    c.table.reads.clear()
    with pytest.raises(Problem) as expired:
        verifier(request(token(signing_key, {"oid": SECOND_OWNER, "scp": READ_SCOPE, "exp": 1})))
    assert expired.value.status == 401 and c.table.reads == []
    with pytest.raises(Problem) as malformed:
        verifier(request(token(signing_key, {"oid": "not-a-canonical-oid", "scp": READ_SCOPE})))
    assert malformed.value.body["error"]["code"] == "BEHAVIOR_HUMAN_NOT_ALLOWED"
    assert c.table.reads == []


def test_legacy_owner_fallback_is_explicit_and_never_overrides_deactivation(registry, jwk_client, signing_key):
    c = registry
    verifier = _verifier(c, jwk_client)
    bearer = token(signing_key, {"scp": READ_SCOPE})
    assert verifier(request(bearer)).team == "team-one"
    c.value.register_owner(c.operator, "capture-original-owner", "team-one", HUMAN_OID)
    c.value.deactivate_owner(c.operator, "deactivate-original-owner", HUMAN_OID)
    with pytest.raises(Problem) as inactive:
        verifier(request(bearer))
    assert inactive.value.status == 403


def test_mounted_route_reuses_one_lookup_but_the_next_request_sees_deactivation(registry, jwk_client, signing_key):
    c = registry
    c.value.register_owner(c.operator, "register-owner-two", "team-two", SECOND_OWNER)
    verifier = _verifier(c, jwk_client)
    bearer = token(signing_key, {"oid": SECOND_OWNER, "scp": READ_SCOPE})
    incoming = request(bearer)
    c.table.reads.clear()
    first = verifier(incoming)
    second = verifier(Request({**incoming.scope, "path": "/mounted"}))
    assert first == second and len(c.table.reads) == 1
    c.value.deactivate_owner(c.operator, "deactivate-owner-two", SECOND_OWNER)
    c.table.reads.clear()
    with pytest.raises(Problem) as inactive:
        verifier(request(bearer))
    assert inactive.value.status == 403 and len(c.table.reads) == 1


@pytest.mark.parametrize("role", [None, "Behavior.Agent", ["Behavior.Agent"]])
def test_unknown_app_identity_is_explicitly_denied(registry, jwk_client, signing_key, role):
    with pytest.raises(Problem) as denied:
        _verifier(registry, jwk_client)(request(token(
            signing_key, {"oid": SECOND_OWNER, "idtyp": "app", "roles": role}, remove=("scp",))))
    assert denied.value.body["error"]["code"] == "BEHAVIOR_ACTOR_NOT_ALLOWED"


@pytest.mark.parametrize("changes", [{"roles": [AGENT_ROLE]}, {}, {"idtyp": "app"}])
def test_configured_agent_path_stays_zero_gets(registry, jwk_client, signing_key, changes):
    c = registry
    c.table.reads.clear()
    principal = _verifier(c, jwk_client)(request(token(
        signing_key, {"oid": A.oid, **changes}, remove=("scp",))))
    assert principal.kind == "agent" and principal.team == A.team and c.table.reads == []


def test_identity_read_counts_compare_legacy_and_registered_entry(registry, jwk_client, signing_key, caplog):
    from shared_context.behavior_timing import phase_timing
    c = registry
    legacy = BehaviorJwtVerifier(c.auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, "team-one"), jwk_client)
    bearer = request(token(signing_key, {"scp": READ_SCOPE}))
    with phase_timing("owner-entry-before"):
        before = legacy(bearer)
    with phase_timing("owner-entry-after"):
        after = _verifier(c, jwk_client)(bearer)
    assert before == after
    timings = [json.loads(record.getMessage().removeprefix("FA_TIMING ")) for record in caplog.records
               if record.getMessage().startswith("FA_TIMING ")]
    observed = {item["phase"]: [item["table_gets"], item["batches"]] for item in timings}
    assert observed == {"owner-entry-before": [0, 0], "owner-entry-after": [1, 1]}
    print(json.dumps(observed, sort_keys=True))


def test_operator_cli_uses_verified_principal_and_reconciles_without_new_install(registry, jwk_client, signing_key):
    c = registry
    installed = []
    composition = SimpleNamespace(verifier=_verifier(c, jwk_client), installation=lambda: installed.append(True))
    authorization = "Bearer " + token(signing_key, {"scp": READ_SCOPE + " " + APPROVAL_SCOPE})
    saved = operate(composition, "register", authorization, owner_oid=SECOND_OWNER,
                    team_id="team-two", request_id="register-via-cli")
    assert saved["result"] == {"team_id": "team-two", "owner_oid": SECOND_OWNER} and installed == [True]
    result = operate(composition, "reconcile", authorization, owner_oid=SECOND_OWNER,
                     team_id="team-two", request_id="register-via-cli")
    assert result == saved and installed == [True]


@pytest.mark.parametrize("identifier", ["disable:second", "deactivate/2026-10-01", ""])
def test_invalid_deactivation_id_never_replaces_the_last_valid_identity(registry, jwk_client, signing_key, identifier):
    c = registry
    c.value.register_owner(c.operator, "register-owner-two", "team-two", SECOND_OWNER)
    composition = SimpleNamespace(verifier=_verifier(c, jwk_client), installation=lambda: None)
    authorization = "Bearer " + token(signing_key, {"scp": READ_SCOPE + " " + APPROVAL_SCOPE})
    guard = c.store._guard()
    with pytest.raises(Problem) as invalid:
        operate(composition, "deactivate", authorization, owner_oid=SECOND_OWNER, request_id=identifier)
    assert invalid.value.status == 422
    assert c.store._guard() == guard
    assert c.value.lookup(SECOND_OWNER).active is True


def test_deactivated_original_operator_can_reconcile_lost_ack_but_cannot_regain_team_access(
        registry, jwk_client, signing_key):
    c = registry
    c.value.register_owner(c.operator, "register-original-owner", "team-one", HUMAN_OID)
    composition = SimpleNamespace(verifier=_verifier(c, jwk_client), installation=lambda: None)
    bearer = token(signing_key, {"scp": READ_SCOPE + " " + APPROVAL_SCOPE})
    c.table.failure = "after"
    with pytest.raises(Problem) as lost:
        operate(composition, "deactivate", "Bearer " + bearer,
                owner_oid=HUMAN_OID, request_id="deactivate-original-owner")
    assert lost.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    c.table.failure = None
    writes = len(c.table.submissions)
    with pytest.raises(Problem) as denied:
        composition.verifier(request(bearer))
    assert denied.value.body["error"]["code"] == "BEHAVIOR_HUMAN_NOT_ALLOWED"
    result = operate(composition, "deactivate-reconcile", "Bearer " + bearer,
                     owner_oid=HUMAN_OID, request_id="deactivate-original-owner")
    assert result["result"]["active"] is False and len(c.table.submissions) == writes
    with pytest.raises(Problem):
        operate(composition, "register", "Bearer " + bearer,
                owner_oid=SECOND_OWNER, team_id="team-two", request_id="not-a-new-permission")
    with pytest.raises(Problem):
        operate(composition, "deactivate-reconcile", "Bearer " + token(signing_key, {
            "oid": SECOND_OWNER, "scp": READ_SCOPE,
        }), owner_oid=HUMAN_OID, request_id="deactivate-original-owner")
    with pytest.raises(Problem):
        operate(composition, "deactivate-reconcile", "Bearer " + token(signing_key, {
            "scp": READ_SCOPE, "exp": 1,
        }), owner_oid=HUMAN_OID, request_id="deactivate-original-owner")
    assert len(c.table.submissions) == writes


AGENT_OID = "abababab-abab-abab-abab-abababababab"
OTHER_AGENT_OID = "cdcdcdcd-cdcd-cdcd-cdcd-cdcdcdcdcdcd"


def _target(n, oid, **changes):
    return FoundryTarget(**{
        "executor_id": f"registered-executor-{n}",
        "project_endpoint": "https://synthetic.services.ai.azure.com/api/projects/project",
        "agent_id": f"{n:08d}-2222-2222-2222-222222222222", "agent_name": f"registered-{n}",
        "agent_version": "3", "session_id": f"registered-session-{n}", "agent_oid": oid, **changes,
    })


def _app(signing_key, oid, **changes):
    return request(token(signing_key, {"oid": oid, "idtyp": "app", **changes}, remove=("scp",)))


def test_owner_registers_one_agent_as_three_create_only_rows_in_one_cas(registry):
    c = registry
    target = _target(1, AGENT_OID)
    receipt = c.value.register_agent(c.operator, "register-agent-one", target.agent_id, target)
    ikey, akey, rkey = (identity_key(TENANT, AGENT_OID), agent_key(TENANT, target.agent_id),
                        roster_key(TENANT, "team-one"))
    assert set(dict(receipt.payload["writes"])) == {ikey, akey, rkey}
    assert receipt.payload["result"] == {"team_id": "team-one", "agent_id": target.agent_id, "agent_oid": AGENT_OID}
    current = c.store.read_snapshot((ikey, akey, rkey))
    identity = _row(current, ikey, IdentityRegistration)
    assert (identity.kind, identity.agent_id, identity.team_id, identity.active) == (
        "agent", target.agent_id, "team-one", True)
    assert _row(current, akey, TeamAgent).target == target
    count = len(c.table.submissions)
    assert c.value.register_agent(c.operator, "register-agent-one", target.agent_id, target) == receipt
    assert len(c.table.submissions) == count
    second = _target(2, OTHER_AGENT_OID)
    c.value.register_agent(c.operator, "register-agent-two", second.agent_id, second)
    roster = _row(c.store.read_snapshot((rkey,)), rkey, TeamAgents)
    assert roster.agent_ids == (target.agent_id, second.agent_id)


@pytest.mark.parametrize("case", [
    "configured-oid", "owner-oid", "registered-owner", "same-oid", "same-agent-id",
    "configured-target", "configured-session",
])
def test_conflicting_agent_registration_writes_nothing(registry, case):
    c = registry
    configured = _target(9, A.oid)
    c.value.targets = (configured,)
    c.value.register_owner(c.operator, "register-owner-two", "team-two", SECOND_OWNER)
    first = _target(1, AGENT_OID)
    c.value.register_agent(c.operator, "register-agent-one", first.agent_id, first)
    target = {
        "configured-oid": _target(2, A.oid), "owner-oid": _target(2, HUMAN_OID),
        "registered-owner": _target(2, SECOND_OWNER),
        "same-oid": _target(2, AGENT_OID), "same-agent-id": _target(1, OTHER_AGENT_OID),
        "configured-target": _target(9, OTHER_AGENT_OID),
        "configured-session": _target(2, OTHER_AGENT_OID, session_id=configured.session_id),
    }[case]
    guard, count = c.store._guard(), len(c.table.submissions)
    with pytest.raises(Problem) as failure:
        c.value.register_agent(c.operator, "conflicting-agent", target.agent_id, target)
    assert failure.value.status == 409 and failure.value.body["error"]["code"] == "TEAM_AGENT_CONFLICT"
    assert c.store._guard() == guard and len(c.table.submissions) == count


def test_full_roster_and_other_team_are_refused_without_writes(registry):
    c = registry
    for n in range(1, MAX_TEAM_AGENTS + 1):
        target = _target(n, f"{n:08d}-3333-3333-3333-333333333333")
        c.value.register_agent(c.operator, f"register-{n}", target.agent_id, target)
    extra = _target(99, AGENT_OID)
    guard = c.store._guard()
    with pytest.raises(Problem) as full:
        c.value.register_agent(c.operator, "register-extra", extra.agent_id, extra)
    assert full.value.body["error"]["code"] == "TEAM_AGENT_LIMIT" and c.store._guard() == guard
    c.value.register_owner(c.operator, "register-owner-two", "team-two", SECOND_OWNER)
    guard = c.store._guard()
    other = replace(c.operator, oid=SECOND_OWNER, team="team-two")
    with pytest.raises(Problem) as unserved:
        c.value.register_agent(other, "register-other-team", extra.agent_id, extra)
    assert unserved.value.body["error"]["code"] == "TEAM_RUNTIME_NOT_SERVED" and c.store._guard() == guard


@pytest.mark.parametrize("change", [
    {"oid": UNKNOWN_OID}, {"actions": frozenset({"read", "submit"})},
    {"kind": "agent", "actor_id": "agent-a"}, {"tenant_id": SECOND_OWNER},
])
def test_only_the_active_owner_with_approval_registers_an_agent(registry, change):
    c = registry
    target = _target(1, AGENT_OID)
    count = len(c.table.submissions)
    with pytest.raises(Problem) as denied:
        c.value.register_agent(replace(c.operator, **change), "denied-agent", target.agent_id, target)
    assert denied.value.status == 403 and len(c.table.submissions) == count


def test_mismatched_agent_id_and_changed_retry_are_refused(registry):
    c = registry
    target = _target(1, AGENT_OID)
    with pytest.raises(Problem) as invalid:
        c.value.register_agent(c.operator, "register-agent-one", _target(2, AGENT_OID).agent_id, target)
    assert invalid.value.status == 422
    c.value.register_agent(c.operator, "register-agent-one", target.agent_id, target)
    count = len(c.table.submissions)
    changed = _target(1, AGENT_OID, agent_version="4")
    with pytest.raises(Problem) as conflict:
        c.value.register_agent(c.operator, "register-agent-one", changed.agent_id, changed)
    assert conflict.value.body["error"]["code"] == "BEHAVIOR_REQUEST_CONFLICT"
    assert len(c.table.submissions) == count


def test_lost_agent_registration_ack_reconciles_without_writing_again(registry):
    c = registry
    target = _target(1, AGENT_OID)
    c.table.failure = "after"
    with pytest.raises(Problem) as lost:
        c.value.register_agent(c.operator, "register-agent-one", target.agent_id, target)
    assert lost.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    c.table.failure = None
    count = len(c.table.submissions)
    receipt = c.value.register_agent(c.operator, "register-agent-one", target.agent_id, target, reconcile=True)
    assert receipt.payload["result"]["agent_oid"] == AGENT_OID and len(c.table.submissions) == count


def test_gate_c_admits_registered_agent_app_token_without_role_in_one_read(registry, jwk_client, signing_key):
    c = registry
    target = _target(1, AGENT_OID)
    c.value.register_agent(c.operator, "register-agent-one", target.agent_id, target)
    verifier = _verifier(c, jwk_client)
    for incoming in (_app(signing_key, AGENT_OID), _app(signing_key, AGENT_OID, roles=["Other.Role"])):
        c.table.reads.clear()
        principal = verifier(incoming)
        assert (principal.kind, principal.actor_id, principal.team, principal.oid) == (
            "agent", target.agent_id, "team-one", AGENT_OID)
        assert principal.actions == frozenset({"read", "submit", "execute", "consume"})
        assert c.table.reads == [(SCOPE.partition_key, "behavior:v1:row:" + identity_key(TENANT, AGENT_OID))]
    incoming = _app(signing_key, AGENT_OID)
    c.table.reads.clear()
    assert verifier(incoming) == verifier(Request({**incoming.scope, "path": "/mounted"}))
    assert len(c.table.reads) == 1


def test_gate_c_refuses_unregistered_inactive_and_owner_oids(registry, jwk_client, signing_key):
    from shared_context.behavior_operations import _write
    from shared_context.behavior_store import ControlChanges, ControlRequest
    from shared_context.source_materials import digest
    c = registry
    target = _target(1, AGENT_OID)
    c.value.register_agent(c.operator, "register-agent-one", target.agent_id, target)
    c.value.register_owner(c.operator, "register-owner-two", "team-two", SECOND_OWNER)
    verifier = _verifier(c, jwk_client)
    for oid in (OTHER_AGENT_OID, SECOND_OWNER, HUMAN_OID):
        with pytest.raises(Problem) as denied:
            verifier(_app(signing_key, oid))
        assert denied.value.body["error"]["code"] == "BEHAVIOR_ACTOR_NOT_ALLOWED"
    ikey = identity_key(TENANT, AGENT_OID)
    current = _row(c.store.read_snapshot((ikey,)), ikey, IdentityRegistration)
    c.store.transact(ControlRequest(request_id="test-deactivate-agent", request_digest=digest({"t": 1}),
                                    read_set=(ikey,)),
                     lambda rows: ControlChanges(writes=(_write(ikey, current.model_copy(update={
                         "active": False, "deactivation_id": "test-deactivate-agent"}), "replace"),), result={}))
    with pytest.raises(Problem) as inactive:
        verifier(_app(signing_key, AGENT_OID))
    assert inactive.value.body["error"]["code"] == "BEHAVIOR_ACTOR_NOT_ALLOWED"


def test_user_token_with_registered_agent_oid_is_not_a_human(registry, jwk_client, signing_key):
    c = registry
    target = _target(1, AGENT_OID)
    c.value.register_agent(c.operator, "register-agent-one", target.agent_id, target)
    with pytest.raises(Problem) as denied:
        _verifier(c, jwk_client)(request(token(signing_key, {"oid": AGENT_OID, "scp": READ_SCOPE})))
    assert denied.value.body["error"]["code"] == "BEHAVIOR_HUMAN_NOT_ALLOWED"


def test_owner_rows_survive_the_real_startup_history_classifier(synthetic_standing_v1):
    c = synthetic_standing_v1
    ids = c.h.config.identity
    registry = TeamRegistry(c.control.store, tenant_id=ids.tenant_id, operator_oid=ids.human_oid,
                            legacy_team_id=ids.team_id, legacy_owner_oid=ids.human_oid,
                            agent_oids=tuple(agent.oid for agent in ids.agents))
    registry.register_owner(c.human, "startup-owner", "another-team", SECOND_OWNER)
    registry.deactivate_owner(c.human, "startup-owner-deactivate", SECOND_OWNER)
    inventory = read_startup(c)
    classified = validate_team_registry_history(inventory, c.h.config)
    assert len(classified) == 2
    assert classified[identity_key(ids.tenant_id, SECOND_OWNER)].active is False


def test_agent_rows_survive_real_startup_readers_and_bad_rows_are_refused(synthetic_standing_v1):
    c = synthetic_standing_v1
    config = c.h.config
    ids = config.identity
    registry = TeamRegistry(c.control.store, tenant_id=ids.tenant_id, operator_oid=ids.human_oid,
                            legacy_team_id=ids.team_id, legacy_owner_oid=ids.human_oid,
                            agent_oids=tuple(agent.oid for agent in ids.agents), targets=config.targets)
    first, second = _target(1, AGENT_OID), _target(2, OTHER_AGENT_OID)
    registry.register_agent(c.human, "startup-agent-one", first.agent_id, first)
    registry.register_agent(c.human, "startup-agent-two", second.agent_id, second)
    inventory = read_startup(c)
    classified = validate_team_registry_history(inventory, config)
    rkey = roster_key(ids.tenant_id, ids.team_id)
    assert set(classified) == {
        identity_key(ids.tenant_id, AGENT_OID), identity_key(ids.tenant_id, OTHER_AGENT_OID),
        agent_key(ids.tenant_id, first.agent_id), agent_key(ids.tenant_id, second.agent_id), rkey,
    }
    akey = agent_key(ids.tenant_id, second.agent_id)
    row = inventory.rows[akey]
    data = {**row.payload["data"], "owner_oid": SECOND_OWNER}
    tampered = replace(inventory, rows={**inventory.rows, akey: replace(row, payload={**row.payload, "data": data})})
    with pytest.raises(Problem):
        validate_team_registry_history(tampered, config)
    missing = replace(inventory, rows={key: value for key, value in inventory.rows.items() if key != akey})
    with pytest.raises(Problem):
        validate_team_registry_history(missing, config)

@pytest.fixture
def activated(workspace, jwk_client, monkeypatch):
    from test_behavior_rotation_settlement_history import page_inventory
    from test_behavior_server import Harness
    h = Harness(workspace, jwk_client)
    try:
        inventory_table = SimpleNamespace(
            backing=h.table.backing, table_name=h.config.table.scope.table_name, url=h.config.table.endpoint,
        )
        page_inventory(inventory_table)
        monkeypatch.setattr(h.composition.clients[0], "query_entities", inventory_table.query_entities)
        h.activate()
        yield h
    finally:
        h.composition.close()


@pytest.mark.parametrize("reader", ["migration-quiescence", "rotation-additions"])
def test_registered_agents_pass_both_restart_readers_and_tampered_roster_is_refused(activated, reader):
    from shared_context.behavior_migration import _quiescence
    from shared_context.behavior_runtime_rotation_history import validate_additions
    h = activated
    ids, store = h.config.identity, h.runtime.store
    baseline = store.read_inventory()
    registry = TeamRegistry(store, tenant_id=ids.tenant_id, operator_oid=ids.human_oid,
                            legacy_team_id=ids.team_id, legacy_owner_oid=ids.human_oid,
                            agent_oids=tuple(agent.oid for agent in ids.agents), targets=h.config.targets)
    human = BehaviorPrincipal(ids.tenant_id, ids.human_oid, ids.team_id, "human", None,
                              frozenset({"read", "approve", "submit"}))
    for n, oid in ((1, AGENT_OID), (2, OTHER_AGENT_OID)):
        target = _target(n, oid)
        registry.register_agent(human, f"restart-agent-{n}", target.agent_id, target)

    def read(inventory):
        if reader == "migration-quiescence":
            _quiescence(inventory, h.config)
        else:
            validate_additions(inventory, h.config, h.config, set(baseline.rows), set())

    inventory = store.read_inventory()
    read(inventory)
    rkey = roster_key(ids.tenant_id, ids.team_id)
    row = inventory.rows[rkey]
    data = {**row.payload["data"], "agent_ids": list(reversed(row.payload["data"]["agent_ids"]))}
    tampered = replace(inventory, rows={**inventory.rows, rkey: replace(row, payload={**row.payload, "data": data})})
    with pytest.raises(Problem):
        read(tampered)
