"""T5 item 3: the lane-independent same-Team recipient resolver for peer messages."""

import pytest

from shared_context.behavior_team_registry import resolve_team_recipient, team_recipient_ids, team_recipient_ready
from shared_context.errors import Problem
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_team_agent_routing import _register_scope, register_more_agents, team_agent
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


def _counting(store, monkeypatch):
    calls = []
    for name in ("read_record", "read_snapshot"):
        original = getattr(store, name)

        def counted(*args, _original=original, _name=name, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(store, name, counted)
    return calls


def test_one_recipient_is_checked_with_its_own_reads_however_large_the_team(team_agent, monkeypatch):
    case = team_agent
    store, config = case.a.runtime.store, case.config
    team, a = config.identity.team_id, config.targets[0]
    registered = case.target.agent_id
    _register_scope(case)
    from types import SimpleNamespace
    from shared_context.native_agent_api import NativeAgentRelayGateway
    relay = NativeAgentRelayGateway(case.a, enabled=True)
    sender = SimpleNamespace(oid=a.agent_oid, team=team)
    calls = _counting(store, monkeypatch)
    scope = relay._recipient(sender, registered)
    assert (scope.identity.oid, scope.team_id) == (AGENT_OID, team)
    small = len(calls)

    added = register_more_agents(case, 5)
    listed = team_recipient_ids(store, config, sender_oid=a.agent_oid, team_id=team)
    assert {registered, *added} <= {agent_id for agent_id, _name in listed}
    del calls[:]
    assert relay._recipient(sender, registered) == scope
    assert len(calls) == small <= 5, "a 7-Agent Team costs one /send the same reads as a 2-Agent Team"
    del calls[:]
    team_recipient_ids(store, config, sender_oid=a.agent_oid, team_id=team)
    assert len(calls) > 2 * small, "the full directory still reads every Agent"


def test_one_recipient_check_keeps_the_route_and_readiness_refusals(team_agent):
    case = team_agent
    store, config = case.a.runtime.store, case.config
    team, a = config.identity.team_id, config.targets[0]
    registered = case.target.agent_id
    # Registered but its runtime scope is not: resolvable, not ready.
    with pytest.raises(Problem) as waiting:
        team_recipient_ready(store, config, registered, sender_oid=a.agent_oid, team_id=team)
    assert waiting.value.status == 409 and waiting.value.body["error"]["code"] == "AGENT_MESSAGE_RECIPIENT_NOT_READY"
    _register_scope(case)
    _denied(lambda: team_recipient_ready(store, config, a.agent_id, sender_oid=a.agent_oid, team_id=team))
    _denied(lambda: team_recipient_ready(store, config, registered, sender_oid=AGENT_OID, team_id=team))
    for unknown in ("unregistered-agent", "bad/agent"):
        _denied(lambda: team_recipient_ready(store, config, unknown, sender_oid=a.agent_oid, team_id=team))
    _denied(lambda: team_recipient_ready(store, config, registered, sender_oid=a.agent_oid, team_id="another-team"))
    _denied(lambda: team_recipient_ready(store, config, registered, sender_oid=OTHER_AGENT_OID, team_id="another-team"))
    assert team_recipient_ready(store, config, a.agent_id, sender_oid=AGENT_OID, team_id=team).identity.oid == a.agent_oid
