"""C1 peer runtimes share the authority/store, not the other Agent's work or target."""

import pytest
import json
import hashlib
from dataclasses import replace
from types import SimpleNamespace

from shared_context.behavior_agent_registry import register_agent_scope
from shared_context.errors import Problem
from shared_context.behavior_assistant_wire import InstructionSubmit
from test_agent_scope_registry import peer_policy
from assistant_sdk_fixture import review_source
from test_conversation_transfer_startup import synthetic_standing_v1
from legacy_turn_rows_helpers import read_startup
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from hosted_versions_stub import deployed_versions


@pytest.fixture
def peer_runtime(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    a = c.h.composition
    policy = peer_policy(c.h.config, c.control.policy)
    original = c.h.config.targets[0]
    deployed_versions(monkeypatch, {original.agent_id: original.agent_version, policy.agent_id: "47"})
    register_agent_scope(a.runtime.store, a.config, policy, c.human, now=c.h.time)
    actor = c.h.config.identity.auth().actors[policy.template.agent_oid]
    c.h.table.backing.apply("Behavior", [("create", {
        "PartitionKey": actor.team, "RowKey": "work:" + policy.profile.legacy_source_work,
        "Payload": json.dumps({"work_id": policy.profile.legacy_source_work, "team": actor.team,
                              "owner": actor.actor, "status": "published", "revision": 1}).encode(),
    })])
    review_source(a.runtime.store, SimpleNamespace(identity=a.config.identity,
        bindings=(policy.template,), policies=(SimpleNamespace(policy=policy.legacy_template_policy),),
        assistant=SimpleNamespace(profiles=(policy.profile,)), review_policy_version=a.config.review_policy_version),
        review_id="b-original-source-review")
    before = read_startup(c)
    b = a.for_agent(policy.agent_id)
    return SimpleNamespace(c=c, a=a, b=b, policy=policy, before=before)


def test_registered_peer_gets_its_own_c1_runtime_without_replacing_a_or_the_shared_ledger(peer_runtime):
    c, a, b, policy, before = (getattr(peer_runtime, name) for name in ("c", "a", "b", "policy", "before"))
    assert b is not a and b.config is a.config
    assert b.runtime is not a.runtime and b.runtime.store is a.runtime.store
    assert b.runtime.dep.workers.contract.capacity == a.runtime.dep.workers.contract.capacity == 1
    assert b.runtime.dep.native_launches.targets == (a.config.targets[1],)
    assert b.runtime.work_policy.policy == policy
    assert b.runtime.work_policy.cost_key == c.control.cost_key
    assert a.runtime.work_policy is c.control and a.runtime.dep.native_launches.targets == a.config.active_targets
    assert a.for_agent(policy.agent_id) is b
    assert a.for_agent(a.config.targets[0].agent_id) is a
    assert read_startup(c).entity_sha256 == before.entity_sha256
    assert c.sessions.calls == []


def test_unregistered_or_unknown_peer_never_falls_back_to_a_runtime(synthetic_standing_v1):
    c = synthetic_standing_v1
    before = read_startup(c)
    for identifier in (c.h.config.targets[1].agent_id, "unknown-agent"):
        with pytest.raises(Problem):
            c.h.composition.for_agent(identifier)
    assert read_startup(c).entity_sha256 == before.entity_sha256


def test_new_b_conversation_follows_until_its_build_version_without_affecting_a_or_old_b(peer_runtime, monkeypatch):
    from shared_context.behavior_work_target import instruction_hosted_version
    c, b = peer_runtime.c, peer_runtime.b
    original = c.h.config.targets[0]
    deployed_versions(monkeypatch, {original.agent_id: "41"})
    conversation = SimpleNamespace(hosted_agent_version="43")
    with pytest.raises(Problem) as denied:
        instruction_hosted_version(b.runtime.work_policy, conversation, existing=False)
    assert denied.value.body["error"]["code"] == "AGENT_VERSION_FOLLOWING"
    assert instruction_hosted_version(b.runtime.work_policy, conversation, existing=True) == "43"
    assert instruction_hosted_version(c.control, conversation, existing=False) == "41"


def test_b_new_version_is_captured_without_borrowing_a_version(peer_runtime, monkeypatch):
    from shared_context.behavior_work_target import instruction_hosted_version
    c, b = peer_runtime.c, peer_runtime.b
    deployed_versions(monkeypatch, {c.h.config.targets[0].agent_id: "999", b.agent_id: "47"})
    assert instruction_hosted_version(b.runtime.work_policy, None, existing=False) == "47"
    assert instruction_hosted_version(c.control, None, existing=False) == "999"

