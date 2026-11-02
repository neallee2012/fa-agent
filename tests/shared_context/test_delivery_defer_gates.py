"""Each C1 deferral gate, one fault at a time, with its exact refusal and precedence."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from shared_context.behavior_delivery import (
    DeliveryConsumer, VerifiedWorkResult, _ConsumerPage, _digest, _material,
)
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime import RuntimeChildJob, runtime_key
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from test_behavior_assistant_acquire_conflicts import installed_assistant, native_acquire
from test_behavior_settlement import write
from test_behavior_store import code
from test_m3_auth import jwk_client, signing_key

SCOPE = ("DELIVERY_DEFER_SCOPE_DENIED", "one exact native assistant pull binding is required")
OWNER = ("DELIVERY_OWNER_DENIED", "current owner, epoch or scope differs")
POLICY = ("BEHAVIOR_CURRENT_POLICY_DENIED", "current owner, policy or binding is unavailable")
READY = ("DELIVERY_PREREQUISITES_MISSING", "current readiness is unavailable")
ROOT = ("DELIVERY_SCOPE_DENIED", "root index/quota binding differs")
PAGE = ("DELIVERY_SCOPE_DENIED", "consumer page belongs to another root")
JOB = ("DELIVERY_SCOPE_DENIED", "original worker/job binding differs")
SLOT = ("DELIVERY_SCOPE_DENIED", "original C1 reservation differs from its job")
DELIVERY = ("DELIVERY_SCOPE_DENIED", "exact original delivery binding differs")
ENTRY = ("DELIVERY_INDEX_INVALID", "original pending entry differs from its obligation")
EXPIRED = ("BEHAVIOR_CURRENT_POLICY_DENIED", "delivery eligibility expired during validation")


def core_of(c):
    return c.composition.runtime.dep.delivery


def drain(c, binding=None, resolve=None):
    runtime = c.composition.runtime
    return DeliveryConsumer(runtime.dep.delivery).drain(
        c.binding.owner.root_budget_id, resolve or runtime.resolve_owner, c.config.controller_id, limit=1,
        pending_binding=binding or c.binding)


def refusal(c, **kwargs):
    with pytest.raises(Problem) as error:
        drain(c, **kwargs)
    return code(error), error.value.body["error"]["message"]


def after_installation(c, monkeypatch, *faults):
    """Row faults land after the installation check, right before the deferral's own snapshot."""
    settled = []
    def resolve(_):
        for fault in faults:
            fault(c, monkeypatch)
        settled.append(c.store.read_inventory())
        return c.binding.owner
    return resolve, settled


def row(kind):
    def key(c):
        b = c.binding
        return {
            "owner": control_key("owner", b.owner.owner_record_id),
            "policy": control_key("policy", b.owner.policy_id),
            "runtime": runtime_key("policy", b.binding_id),
            "obligation": control_key("delivery-obligation", b.obligation_id),
            "work": control_key("delivery-work-state", b.obligation_id),
            "index": control_key("delivery-index", b.owner.root_budget_id),
            "quota": control_key("delivery-quota", "root", b.owner.root_budget_id),
            "pool": worker_key("pool", c.config.workers.workspace_id),
            "job": worker_key("job", b.job_id),
            "intent": control_key("delivery-intent", control_key("delivery-intent-id", b.event_id)),
        }[kind]
    return key


def edit(c, kind, **updates):
    key = row(kind)(c)
    data = json.loads(json.dumps(c.store.read_snapshot((key,))[key].payload["data"]))
    for field, value in updates.items():
        if isinstance(value, dict):
            data[field].update(value)
        else:
            data[field] = value
    write(c.competitor, key, data)


def edits(kind, **updates):
    return lambda c, _: edit(c, kind, **updates)


def readiness(c, monkeypatch, **offset):
    core = core_of(c)
    original = core.registration.verify_readiness
    start = c.clock[0]
    def verify():
        return original().model_copy(update={field: start + value for field, value in offset.items()})
    monkeypatch.setattr(core, "registration", replace(core.registration, verify_readiness=verify))


def foreign_page(c, _):
    core = core_of(c)
    key = control_key("delivery-consumer-cursor", c.binding.owner.root_budget_id)
    page = _ConsumerPage(configuration_digest=core.configuration_digest(), evidence_mode=core.registration.mode, root_budget_id="other")
    write(c.competitor, key, page.model_dump(mode="json"))


def expired_policy(c, _):
    edit(c, "runtime", valid_until=c.clock[0].isoformat())


def event_moved(c, _):
    edit(c, "obligation", event_id="other")
    edit(c, "work", event_id="other")


def definition_changed(c, _):
    key = row("work")(c)
    data = c.store.read_snapshot((key,))[key].payload["data"]
    definition = {**data["definition"], "goal": "another goal"}
    edit(c, "work", definition=definition, definition_digest=_digest(definition))


def pool_and_job_redigested(c, _):
    edit(c, "pool", configuration_digest="e" * 64)
    edit(c, "job", configuration_digest="e" * 64)


FAULTS = {
    "owner-inactive": (edits("owner", active=False), OWNER),
    "runtime-digest": (edits("runtime", binding_digest="0" * 64), POLICY),
    "runtime-inactive": (edits("runtime", active=False), POLICY),
    "runtime-revision": (edits("runtime", operation_policy_revision=99), POLICY),
    "runtime-expired": (expired_policy, POLICY),
    "policy-inactive": (edits("policy", active=False), POLICY),
    "policy-identity": (edits("policy", policy_id="other"), POLICY),
    "operation-removed": (edits("owner", operation_ids=["other-operation"]), POLICY),
    "quota-scope": (edits("quota", scope_id="other"), ROOT),
    "quota-request": (edits("quota", root_request_id="other"), ROOT),
    "page-root": (foreign_page, PAGE),
    "pool-workspace": (edits("pool", workspace_id="other"), JOB),
    "pool-digest": (pool_and_job_redigested, JOB),
    "job-digest": (edits("job", configuration_digest="e" * 64), JOB),
    "job-id": (edits("job", spec={"job_id": "other"}), JOB),
    "job-work": (edits("job", spec={"work_id": "other"}), JOB),
    "job-obligation": (edits("job", spec={"obligation_id": "other"}), JOB),
    "slot-number": (lambda c, _: edit(c, "pool", slots=[{"number": 1}]), SLOT),
    "obligation-event": (event_moved, DELIVERY),
    "obligation-request": (edits("obligation", root_request_id="other"), DELIVERY),
    "work-definition": (definition_changed, DELIVERY),
    "entry-generation": (lambda c, _: edit(c, "obligation", generation=1), ENTRY),
}


@pytest.mark.parametrize("fault", FAULTS)
def test_each_gate_refuses_with_its_own_code_and_writes_nothing(native_acquire, monkeypatch, fault):
    c = native_acquire
    apply, expected = FAULTS[fault]
    resolve, settled = after_installation(c, monkeypatch, apply)
    assert refusal(c, resolve=resolve) == expected
    assert c.store.read_inventory() == settled[0]


@pytest.mark.parametrize("offset", [{"expires_at": timedelta(0)}, {"observed_at": timedelta(seconds=1)}])
def test_readiness_must_cover_this_moment(native_acquire, monkeypatch, offset):
    c = native_acquire
    readiness(c, monkeypatch, **offset)
    assert refusal(c, resolve=lambda _: c.binding.owner) == READY


@pytest.mark.parametrize("first,second", [
    ("owner-inactive", "runtime-inactive"), ("quota-scope", "page-root"), ("page-root", "pool-workspace"),
    ("pool-workspace", "slot-number"), ("slot-number", "obligation-request"), ("obligation-request", "entry-generation"),
])
def test_when_two_gates_fail_the_earlier_one_answers(native_acquire, monkeypatch, first, second):
    c = native_acquire
    resolve, _ = after_installation(c, monkeypatch, FAULTS[second][0], FAULTS[first][0])
    assert refusal(c, resolve=resolve) == FAULTS[first][1]


def test_policy_answers_before_readiness_and_readiness_before_the_root(native_acquire, monkeypatch):
    c = native_acquire
    readiness(c, monkeypatch, expires_at=timedelta(0))
    resolve, _ = after_installation(c, monkeypatch, FAULTS["quota-scope"][0])
    assert refusal(c, resolve=resolve) == READY
    resolve, _ = after_installation(c, monkeypatch, FAULTS["runtime-inactive"][0])
    assert refusal(c, resolve=resolve) == POLICY


def test_a_non_inbox_route_never_passes_the_binding_contract(native_acquire):
    b = native_acquire.binding
    binding = b.model_copy(update={"route": b.route.model_copy(update={"channel_id": "msteams"})})
    assert refusal(native_acquire, binding=binding)[0] == "DELIVERY_INVALID_DATA"


def test_scope_refuses_a_non_native_registration(native_acquire, monkeypatch):
    c, core = native_acquire, core_of(native_acquire)
    monkeypatch.setattr(core, "registration", replace(core.registration, mode="offline_contract"))
    assert refusal(c) == SCOPE


@pytest.mark.parametrize("change", ["checks", "child", "root", "team"])
def test_scope_needs_the_exact_native_assistant_binding(native_acquire, change):
    c, b = native_acquire, native_acquire.binding
    binding = b.model_copy(update={
        "checks": {"work": b.work.model_copy(update={"required_checks": ("sha256",)})},
        "child": {"child_jobs": (RuntimeChildJob(job_id="child-job", work=b.work.model_copy(update={"work_id": "child-work"})),)},
        "root": {"owner": b.owner.model_copy(update={"root_budget_id": "other-root"})},
        "team": {"team_id": "other-team"},
    }[change])
    assert refusal(c, binding=binding) == SCOPE


def test_scope_answers_before_the_owner_is_resolved(native_acquire):
    c = native_acquire
    def resolve(_):
        pytest.fail("the owner lookup must not run for a refused binding")
    assert refusal(c, binding=c.binding.model_copy(update={"team_id": "other-team"}), resolve=resolve) == SCOPE


def test_the_resolved_owner_must_be_the_configured_one(native_acquire):
    c = native_acquire
    other = c.binding.owner.model_copy(update={"epoch": c.binding.owner.epoch + 1})
    assert refusal(c, resolve=lambda _: other) == (
        "DELIVERY_OWNER_DENIED", "deferral requires the exact configured owner")


def test_published_readiness_must_not_change_under_the_decision(native_acquire):
    c, core = native_acquire, core_of(native_acquire)
    key = control_key("inbox-readiness", core.configuration_digest())
    resolve_owner = c.composition.runtime.resolve_owner
    def resolve(owner):
        data = c.store.read_snapshot((key,))[key].payload["data"]
        write(c.competitor, key, {**data, "protection_scope": "changed-scope"})
        return resolve_owner(owner)
    assert refusal(c, resolve=resolve) == READY


def assert_uses_original_drain(c):
    assert drain(c) != ()


@pytest.mark.parametrize("updates", [
    {"pending": []}, {"completions": ["finished-job"]}, {"slots": [{"number": 1}, {"number": 0}]},
])
def test_any_other_pool_state_uses_the_original_drain(native_acquire, updates):
    edit(native_acquire, "pool", **updates)
    assert_uses_original_drain(native_acquire)


def test_a_job_outside_the_c1_states_uses_the_original_drain(native_acquire):
    edit(native_acquire, "job", state="waiting_dependency")
    assert_uses_original_drain(native_acquire)


@pytest.mark.parametrize("updates", [{"fulfillment": "fulfilled"}, {"final_delivery_id": "already-prepared"}])
def test_a_settled_obligation_uses_the_original_drain(native_acquire, updates):
    edit(native_acquire, "obligation", **updates)
    assert_uses_original_drain(native_acquire)


def test_a_saved_plan_uses_the_original_drain(native_acquire):
    c = native_acquire
    write(c.competitor, control_key("delivery-consumer-plan", c.binding.obligation_id), {"synthetic": True})
    assert_uses_original_drain(c)


def test_a_finished_result_skips_the_intent_read_entirely(native_acquire):
    c, core = native_acquire, core_of(native_acquire)
    key = row("work")(c)
    data = c.store.read_snapshot((key,))[key].payload["data"]
    result = VerifiedWorkResult(
        evidence_id="synthetic-result", verifier_version=core.contract.verifier_version,
        work_id=c.binding.work.work_id, definition_digest=data["definition_digest"], state="failed",
        passed_checks=(), content=b"synthetic failure", proof=b"synthetic proof")
    write(c.competitor, key, {**data, "state": "failed", "result": result.model_dump(mode="json")},
          material=_material(result.model_dump(mode="json")))
    edit(c, "intent", consumer_version="stale-consumer")
    assert_uses_original_drain(c)


def test_two_index_entries_never_compare_the_first_with_this_obligation(native_acquire):
    c = native_acquire
    key = row("index")(c)
    entries = c.store.read_snapshot((key,))[key].payload["data"]["entries"]
    edit(c, "index", entries=[{**entries[0], "generation": 7}, {**entries[0], "obligation_id": "zz-another-pending"}])
    quota = row("quota")(c)
    edit(c, "quota", pending=c.store.read_snapshot((quota,))[quota].payload["data"]["pending"] + 1)
    assert_uses_original_drain(c)


def test_a_lone_foreign_entry_is_not_this_obligation_differing(native_acquire):
    c = native_acquire
    key = row("index")(c)
    entries = c.store.read_snapshot((key,))[key].payload["data"]["entries"]
    edit(c, "index", entries=[{**entries[0], "obligation_id": "zz-another-pending"}])
    assert_uses_original_drain(c)


def clock_moves_during_validation(c, monkeypatch, moment):
    original = DeliveryConsumer._original_work
    def moved(consumer, *args):
        result = original(consumer, *args)
        c.clock[0] = moment
        return result
    monkeypatch.setattr(DeliveryConsumer, "_original_work", moved)


def test_runtime_policy_alone_expiring_during_validation_is_refused(native_acquire, monkeypatch):
    c = native_acquire
    moment = c.clock[0] + timedelta(seconds=1)
    edit(c, "runtime", valid_until=moment.isoformat())
    clock_moves_during_validation(c, monkeypatch, moment)
    assert refusal(c) == EXPIRED


def test_readiness_alone_expiring_during_validation_is_refused(native_acquire, monkeypatch):
    c = native_acquire
    moment = c.clock[0] + timedelta(seconds=1)
    readiness(c, monkeypatch, expires_at=timedelta(seconds=1))
    clock_moves_during_validation(c, monkeypatch, moment)
    assert refusal(c) == EXPIRED


def test_the_clock_is_read_once_per_stage_of_a_deferral(native_acquire, monkeypatch):
    c, core = native_acquire, core_of(native_acquire)
    now, reads = core._now, []
    def counted():
        reads.append(True)
        return now()
    monkeypatch.setattr(core, "_now", counted)
    def resolve(_):
        reads.clear()  # the installation check above has its own clock reads
        return c.binding.owner
    assert drain(c, resolve=resolve) == ()
    assert len(reads) == 2


def test_the_decision_uses_one_snapshot_and_a_later_change_is_a_conflict(native_acquire, monkeypatch):
    c, core = native_acquire, core_of(native_acquire)
    work, changed = core._work, []
    def change_after_snapshot(*args):
        result = work(*args)
        if not changed:
            changed.append(True)
            edit(c, "intent", consumer_version="stale-consumer")
        return result
    monkeypatch.setattr(core, "_work", change_after_snapshot)
    assert refusal(c, resolve=lambda _: c.binding.owner)[0] == "BEHAVIOR_CONFLICT"
