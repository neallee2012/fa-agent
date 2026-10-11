"""Runtime dependency/startup/policy tests over actual components, OFFLINE only."""

from dataclasses import replace

import pytest

from shared_context.behavior_api import create_behavior_app
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime import BehaviorRuntime, runtime_key
from shared_context.errors import Problem
from test_behavior_api import CTX, RuntimeHarness
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def h(auth, jwk_client, signing_key):
    return RuntimeHarness(auth, jwk_client, signing_key)


@pytest.mark.parametrize("missing", ["sources", "memory", "delivery", "inbox", "workers", "broker", "evidence"])
def test_missing_concrete_dependency_fails_closed_at_construction(h, missing):
    with pytest.raises(Problem) as error:
        BehaviorRuntime(replace(h.dep, **{missing: None}), (h.binding,), clock=h.runtime.clock, mode="offline_contract")
    assert error.value.body["error"]["code"] == "BEHAVIOR_CONFIGURATION"


def test_policy_revision_mismatch_cannot_enable_routes_or_supervision(h):
    h.mutate(control_key("policy", "policy"), revision=2)
    with pytest.raises(Problem):
        create_behavior_app(h.runtime, h.verifier)
    with pytest.raises(Problem):
        h.runtime.resolve_owner(CTX.owner_record_id)


def test_binding_configuration_is_immutable_and_owner_epoch_is_read_from_authority(h):
    with pytest.raises(TypeError):
        h.runtime.bindings["arbitrary"] = h.binding
    assert h.runtime.resolve_owner(CTX.owner_record_id) == CTX
    h.mutate(control_key("owner", "owner"), epoch=2)
    assert h.runtime.resolve_owner(CTX.owner_record_id).epoch == 2
    with pytest.raises(Problem):
        h.runtime.resolve_owner("other-owner")


def test_bootstrap_is_explicit_and_cannot_relabel_the_same_binding(h):
    assert h.store.read_snapshot((control_key("grant", "grant"),))[control_key("grant", "grant")] is None
    with pytest.raises(Problem):
        h.runtime.bootstrap({})
    with pytest.raises(Problem):
        h.runtime.bootstrap({"binding-one": h.policy.model_copy(update={"binding_digest": "f" * 64})})
    with pytest.raises(Problem):
        h.runtime.bootstrap({"binding-one": h.policy.model_copy(update={"max_commands": 63})})


def test_bounded_command_inventory_keeps_existing_reconciliation_available(h):
    from shared_context import behavior_wire as wire
    from test_behavior_api import METRICS

    h.mutate(runtime_key("policy", "binding-one"), max_commands=1)
    command = wire.SourcePrepare(request_id="one", version=1, original=METRICS)
    assert h.post("/sources/metrics/prepare", command).status_code == 200
    assert h.post("/sources/metrics/commit", wire.Command(request_id="one")).status_code == 200
    assert h.post("/sources/metrics/prepare", command.model_copy(update={"request_id": "two", "version": 2})).status_code == 429
    assert h.post("/sources/metrics/reconcile", wire.Command(request_id="one")).status_code == 200


def test_saved_source_plan_cannot_bypass_a_current_visibility_change(h):
    from shared_context import behavior_wire as wire
    from test_behavior_api import METRICS

    prepared = wire.SourcePrepare(request_id="old-scope", version=1, original=METRICS)
    assert h.post("/sources/metrics/prepare", prepared).status_code == 200
    h.mutate(control_key("policy", "policy"), revision=2)
    h.mutate(runtime_key("policy", "binding-one"), operation_policy_revision=2, source_visibility="private")
    assert h.post("/sources/metrics/commit", wire.Command(request_id="old-scope")).status_code == 403
