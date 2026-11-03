"""Bootstrap refusals and controller bookkeeping of ``behavior_server`` that the broader tests reach only in part."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from shared_context.behavior_server import bootstrap, build_server, run_controller
from shared_context.behavior_server_config import BootstrapAuthorization, SupervisorLimits
from shared_context.errors import Problem
from test_behavior_server import NOW, Harness, h, workspace
from test_m3_auth import jwk_client, signing_key


def grant_edits():
    later = (NOW + timedelta(hours=2)).isoformat()
    earlier = (NOW - timedelta(hours=1)).isoformat()
    return {
        "grant-id": lambda g: g.update(grant_id="other-grant"),
        "controller": lambda g: g.update(controller_id="other-controller"),
        "policy-revision": lambda g: g.update(policy_revision=g["policy_revision"] + 1),
        "workload": lambda g: g.update(workload_version="assistant-v1.2"),
        "inactive": lambda g: g.update(active=False),
        "no-uses": lambda g: g.update(remaining_uses=0),
        "too-many-uses": lambda g: g.update(remaining_uses=8),
        "starts-before-approval": lambda g: g.update(not_before=earlier),
        "ends-after-approval": lambda g: g.update(expires_at=later),
        "starts-after-now": lambda g: g.update(not_before=(NOW + timedelta(minutes=5)).isoformat()),
        "checkpoint-beyond-input": lambda g: g.update(checkpoint_after_bytes=8192),
    }


@pytest.mark.parametrize("change", list(grant_edits()))
def test_bootstrap_refuses_each_grant_that_leaves_the_exact_authorization(h, change):
    approval = h.approval.model_dump(mode="json")
    grant_edits()[change](approval["native_grants"][0])
    approval = BootstrapAuthorization.model_validate_json(json.dumps(approval))  # the edit alone is a valid wire value
    with pytest.raises(Problem) as denied:
        bootstrap(h.composition, approval)
    assert denied.value.status == 403
    assert denied.value.body["error"]["code"] == "BEHAVIOR_BOOTSTRAP_DENIED"
    assert denied.value.body["error"]["message"] == "native grant exceeds the exact workload/input/agent authorization"
    assert not h.table.requests


def test_bootstrap_refuses_a_grant_for_another_owner(h):
    approval = h.approval.model_dump(mode="json")
    owner = approval["native_grants"][0]["owner"]
    name = next(key for key, value in owner.items() if isinstance(value, str))
    owner[name] = "other-" + owner[name]
    approval = BootstrapAuthorization.model_validate_json(json.dumps(approval))
    with pytest.raises(Problem) as denied:
        bootstrap(h.composition, approval)
    assert denied.value.body["error"]["code"] == "BEHAVIOR_BOOTSTRAP_DENIED" and not h.table.requests


@pytest.mark.parametrize("change", ["missing", "duplicate", "unknown"])
def test_bootstrap_needs_exactly_one_grant_for_each_registered_job(h, change):
    approval = h.approval.model_dump(mode="json")
    grants = approval["native_grants"]
    if change == "duplicate":
        grants.append(dict(grants[0]))
    elif change == "unknown":
        grants.append({**grants[0], "job_id": "unregistered-job"})
    else:
        grants[0]["job_id"] = "unregistered-job"
    approval = BootstrapAuthorization.model_validate_json(json.dumps(approval))
    with pytest.raises(Problem) as denied:
        bootstrap(h.composition, approval)
    assert denied.value.body["error"]["message"] == "every exact job needs separate launch authorization"
    assert not h.table.requests


class Controller:
    def __init__(self, batches):
        self.batches = iter(batches)

    def drain(self, *, limit):
        return next(self.batches, [])


def controller_composition(h, batches, monkeypatch=None):
    h.composition.runtime.controller = Controller(batches)
    return h.composition


def preactivated(composition, monkeypatch):
    """Skip the evidence checks a preactivation start makes; the loop under test is the same worker-only loop."""
    monkeypatch.setattr(composition, "installation", lambda: None)
    monkeypatch.setattr(composition.catalog, "worker", lambda *a, **k: None)
    monkeypatch.setattr(composition.runtime.dep.workers, "_ready", lambda: None)
    monkeypatch.setattr(composition.runtime.dep.workers, "pending", lambda: None)


def test_problem_codes_are_counted_bounded_and_named_only_when_well_formed(h, monkeypatch):
    composition = controller_composition(h, [[
        {"problem": "FIRST_CODE"}, {"problem": "FIRST_CODE"}, {"problem": "lower-case"}, {"problem": 5},
        {"problem": None}, {}, {"problem": "X" * 97},
    ] + [{"problem": f"CODE_{index}"} for index in range(40)]])
    preactivated(composition, monkeypatch)
    result = run_controller(composition, limits=SupervisorLimits(ticks=1, page_limit=3, interval_seconds=0.0),
                            preactivation=True)
    counts = result["problem_counts"]
    assert counts["FIRST_CODE"] == 2
    assert counts["BEHAVIOR_DEPENDENCY_UNAVAILABLE"] == 3  # lower-case, non-string, over-long
    assert counts["OTHER_PROBLEMS"] == 40 - (32 - 2)  # 32 distinct names were kept, the rest are pooled
    assert len(counts) == 33 and sum(counts.values()) == 2 + 3 + 40
    assert result["worker_items"] == 47 and result["delivery_items"] == 0 and result["ticks"] == 1


def test_a_stop_request_ends_the_loop_before_any_drain_and_never_sleeps(h, monkeypatch):
    composition = controller_composition(h, [[{"problem": "NEVER"}]])
    preactivated(composition, monkeypatch)
    slept = []
    result = run_controller(composition, limits=SupervisorLimits(ticks=3, page_limit=3, interval_seconds=1.0),
                            stop_requested=lambda: True, sleep=slept.append, preactivation=True)
    assert (result["ticks"], result["worker_items"], result["problem_counts"]) == (0, 0, {})
    assert slept == []


def test_the_pause_between_ticks_is_the_interval_capped_by_the_remaining_wall_time(h, monkeypatch):
    composition = controller_composition(h, [])
    preactivated(composition, monkeypatch)
    clock = iter([0.0] + [0.0] * 20)
    slept = []
    limits = SupervisorLimits(ticks=3, page_limit=3, interval_seconds=30.0, max_run_seconds=10)
    result = run_controller(composition, limits=limits, monotonic=lambda: next(clock), sleep=slept.append,
                            preactivation=True)
    assert result["ticks"] == 3
    assert slept == [10.0, 10.0]  # min(interval, remaining wall time); there is no pause after the last tick


def test_a_stop_request_after_the_worker_drain_skips_every_delivery_read(h, monkeypatch):
    composition = controller_composition(h, [])
    monkeypatch.setattr(composition, "installation", lambda: None)
    monkeypatch.setattr(composition.catalog, "require_activation", lambda: None)
    monkeypatch.setattr(composition.runtime, "validate_startup", lambda: None)
    reads = []
    real = composition.runtime.store.read_snapshot
    monkeypatch.setattr(composition.runtime.store, "read_snapshot", lambda keys: reads.append(keys) or real(keys))
    answers = iter([False])  # not stopped at the tick's start, stopped from then on (before the first root)
    result = run_controller(composition, limits=SupervisorLimits(ticks=2, page_limit=3, interval_seconds=0.0),
                            stop_requested=lambda: next(answers, True))
    assert (result["ticks"], result["delivery_items"]) == (1, 0)
    assert reads == []


def test_build_server_refuses_offline_transports_that_miss_a_native_target_or_a_projection(h):
    with pytest.raises(ValueError, match="offline transports must cover the exact native target set"):
        build_server(h.config, **{**h.options, "native_transports": {}})
    with pytest.raises(ValueError, match="offline memory transports must cover"):
        build_server(h.config, **{**h.options, "memory_transports": {"unconfigured-binding": h.memory}})


def test_build_server_refuses_a_control_binding_that_is_not_its_table_endpoint(h):
    workers = h.config.workers.model_copy(update={"control_binding": "another-table"})
    with pytest.raises(ValueError, match="control_binding"):
        build_server(h.config.model_copy(update={"workers": workers}), **h.options)


from shared_context import behavior_server


def pull_case(**changes):
    """A composition, runtime and root where the one authenticated-pull binding may defer a nonready delivery page."""
    binding = SimpleNamespace(binding_id="binding-1", route=SimpleNamespace(channel_id="authenticated-pull"),
                              owner=SimpleNamespace(root_budget_id="root-1"))
    composition = SimpleNamespace(
        config=SimpleNamespace(workload_version="assistant-v1.2", workers=SimpleNamespace(capacity=1)),
        assistant=object())
    runtime = SimpleNamespace(bindings={"binding-1": binding}, profiles={"binding-1": object()})
    root = SimpleNamespace(root_budget_id="root-1")
    for name, value in changes.items():
        if name == "workload":
            composition.config.workload_version = value
        elif name == "assistant":
            composition.assistant = value
        elif name == "capacity":
            composition.config.workers.capacity = value
        elif name == "bindings":
            runtime.bindings = value
        elif name == "profiles":
            runtime.profiles = value
        elif name == "channel":
            binding.route.channel_id = value
        elif name == "owner":
            binding.owner.root_budget_id = value
    return composition, runtime, root, binding


def test_only_the_one_authenticated_pull_binding_of_the_root_may_defer_a_nonready_page():
    composition, runtime, root, binding = pull_case()
    assert behavior_server._pending_binding(composition, runtime, root) is binding
    for change in ({"workload": "checksum-count-v1"}, {"assistant": None}, {"capacity": 2}, {"profiles": {}},
                   {"channel": "other-channel"}, {"owner": "root-2"}):
        assert behavior_server._pending_binding(*pull_case(**change)[:3]) is None, change
    composition, runtime, root, binding = pull_case()
    runtime.bindings = {"binding-1": binding, "binding-2": binding}
    assert behavior_server._pending_binding(composition, runtime, root) is None


def grant_case(*, child_jobs=True, parent=True, checkpoint=100, uses=2):
    binding = SimpleNamespace(child_jobs=("child",) if child_jobs else (), job_id="parent")
    job = SimpleNamespace(job_id="parent" if parent else "child")
    grant = SimpleNamespace(checkpoint_after_bytes=checkpoint, remaining_uses=uses)
    return grant, binding, job


def test_a_registered_child_parent_needs_a_partial_checkpoint_and_a_resume_grant():
    ok = grant_case(checkpoint=100, uses=2)
    behavior_server._check_checkpoint_grant(*ok, 200)
    for case in (grant_case(checkpoint=None), grant_case(checkpoint=200), grant_case(checkpoint=300),
                 grant_case(uses=1)):
        with pytest.raises(Problem) as denied:
            behavior_server._check_checkpoint_grant(*case, 200)
        assert denied.value.status == 403
        assert denied.value.body["error"]["message"] == (
            "registered-child parent requires a partial checkpoint and a resume grant")
    behavior_server._check_checkpoint_grant(*grant_case(child_jobs=False, checkpoint=None, uses=1), 200)


def test_a_registered_leaf_child_never_parks_without_its_own_dependency_contract():
    behavior_server._check_checkpoint_grant(*grant_case(parent=False, checkpoint=None), 200)
    with pytest.raises(Problem) as denied:
        behavior_server._check_checkpoint_grant(*grant_case(parent=False, checkpoint=10), 200)
    assert denied.value.body["error"]["message"] == (
        "registered leaf child cannot park without its own dependency contract")


def test_the_controller_start_checks_depend_on_preactivation():
    calls = []
    composition = SimpleNamespace(
        installation=lambda: calls.append("installation"),
        catalog=SimpleNamespace(worker=lambda: calls.append("worker"),
                                require_activation=lambda: calls.append("require_activation")))
    runtime = SimpleNamespace(
        dep=SimpleNamespace(workers=SimpleNamespace(_ready=lambda: calls.append("ready"),
                                                    pending=lambda: calls.append("pending"))),
        validate_startup=lambda: calls.append("validate_startup"))
    behavior_server._start_controller(composition, runtime, True)
    assert calls == ["installation", "worker", "ready", "pending"]
    calls.clear()
    behavior_server._start_controller(composition, runtime, False)
    assert calls == ["installation", "require_activation", "validate_startup"]
