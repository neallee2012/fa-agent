"""Bootstrap refusals and controller bookkeeping of ``behavior_server`` that the broader tests reach only in part."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from shared_context.behavior_server import bootstrap, build_server
from shared_context.behavior_server_config import BootstrapAuthorization
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

