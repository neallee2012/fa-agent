"""T5 item 3: the lane-independent same-Team recipient resolver for peer messages."""

import pytest

from shared_context.behavior_team_registry import resolve_team_recipient, team_recipient_ids
from shared_context.errors import Problem
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_team_agent_routing import _register_scope, team_agent
from test_team_registry import AGENT_OID, OTHER_AGENT_OID


def _denied(call):
    with pytest.raises(Problem) as refused:
        call()
    assert refused.value.status == 403 and refused.value.body["error"]["code"] == "AGENT_MESSAGE_ROUTE_DENIED"


def test_registered_agents_and_the_original_a_are_recipients_but_never_the_sender(team_agent):
    case = team_agent
    store, config = case.a.runtime.store, case.config
    team, a = config.identity.team_id, config.targets[0]
    registered = case.target.agent_id
    # Before its runtime scope exists, a registered Agent is not offered; the original A always is.
    assert team_recipient_ids(store, config, sender_oid=a.agent_oid, team_id=team) == ()
    assert team_recipient_ids(store, config, sender_oid=AGENT_OID, team_id=team) == ((a.agent_id, a.agent_name),)
    _register_scope(case)
    assert team_recipient_ids(store, config, sender_oid=a.agent_oid, team_id=team) == (
        (registered, case.target.agent_name),)
    assert team_recipient_ids(store, config, sender_oid=AGENT_OID, team_id=team) == ((a.agent_id, a.agent_name),)
    scope = resolve_team_recipient(store, config, registered, sender_oid=a.agent_oid, team_id=team)
    assert (scope.identity.oid, scope.team_id, scope.legacy_keys) == (AGENT_OID, team, False)
    back = resolve_team_recipient(store, config, a.agent_id, sender_oid=AGENT_OID, team_id=team)
    assert (back.identity.oid, back.legacy_keys) == (a.agent_oid, True)


def test_self_unknown_and_other_team_recipients_are_refused(team_agent):
    case = team_agent
    store, config = case.a.runtime.store, case.config
    team, a = config.identity.team_id, config.targets[0]
    registered = case.target.agent_id
    _register_scope(case)
    _denied(lambda: resolve_team_recipient(store, config, a.agent_id, sender_oid=a.agent_oid, team_id=team))
    _denied(lambda: resolve_team_recipient(store, config, registered, sender_oid=AGENT_OID, team_id=team))
    for unknown in ("unregistered-agent", "bad/agent"):
        _denied(lambda: resolve_team_recipient(store, config, unknown, sender_oid=a.agent_oid, team_id=team))
    _denied(lambda: resolve_team_recipient(store, config, registered, sender_oid=a.agent_oid, team_id="another-team"))
    assert team_recipient_ids(store, config, sender_oid=a.agent_oid, team_id="another-team") == ()
    # An identity outside the served Team never gains a recipient by naming its own Team.
    assert team_recipient_ids(store, config, sender_oid=OTHER_AGENT_OID, team_id="another-team") == ()


def test_store_failures_are_not_reported_as_a_refused_recipient(team_agent, monkeypatch):
    from shared_context.behavior_team_registry import agent_key
    case = team_agent
    store, config = case.a.runtime.store, case.config
    team, a = config.identity.team_id, config.targets[0]
    _register_scope(case)
    original, target = store.read_record, agent_key(config.identity.tenant_id, case.target.agent_id)

    def outage(key):
        if key == target:
            raise Problem(503, "BEHAVIOR_READ_FAILED", "synthetic store outage")
        return original(key)
    monkeypatch.setattr(store, "read_record", outage)
    for call in (lambda: resolve_team_recipient(store, config, case.target.agent_id, sender_oid=a.agent_oid,
                                                team_id=team),
                 lambda: team_recipient_ids(store, config, sender_oid=a.agent_oid, team_id=team)):
        with pytest.raises(Problem) as failed:
            call()
        assert failed.value.status == 503 and failed.value.body["error"]["code"] == "BEHAVIOR_READ_FAILED"
