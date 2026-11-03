"""The read-side gates of ``WorkPolicyControl``: ``check``, ``preflight`` and ``refresh``.

``check`` runs on small fakes so each of its refusals is one line. ``preflight`` and ``refresh`` run on real synthetic
turns; only what a case names is replaced, and no gate is reached through a private helper."""
import hashlib
import sys
from types import SimpleNamespace

import pytest

from shared_context import behavior_work_policy as work
from shared_context import behavior_work_registry as registry
from shared_context.behavior_agent_worker import AgentVmBinding
from shared_context.behavior_assistant_wire import AssistantSubmit
from shared_context.behavior_operations import control_key
from shared_context.behavior_profile_selection import selection_key
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_work_policy import (
    BudgetRecord, CostLedger, PerWorkRuntimePolicy, WorkPolicyControl, WorkPreflightQuery, refresh_once, work_policy_key,
)
from shared_context.behavior_workers import _Pool
from shared_context.errors import Problem
from test_conversation_transfer_startup import synthetic_standing_v1  # fixture
from test_instruction_flow import instruction_models  # fixture
from test_m3_auth import jwk_client, signing_key  # fixture
from test_work_registration_gates import admitted, code, instruction, register, structured  # fixture
from test_work_result_access import Reads


# check

class Row:
    def __init__(self, value, material="same"):
        self.value, self.payload = value, {"material": material}


class Record(SimpleNamespace):
    def model_dump(self, mode):
        return {}


BINDING = SimpleNamespace(binding_id="binding", job_id="job")


def policy(**changes):
    return SimpleNamespace(**{"policy_id": "policy", "admission_id": "admission", "job_id": "job", "instruction": False,
                              "valid_until": 100, **changes})


def record(*, instruction=None, memory_scope=None, selection=None, agent_scope=None):
    return Record(binding=BINDING, admission_id="admission", request_id="request", agent_scope=agent_scope,
                  profile=SimpleNamespace(instruction=instruction, memory_scope=memory_scope),
                  profile_selection_id=selection)


KEY = work_policy_key("registration", "admission")


def gated(monkeypatch, saved, *, now=50, **current):
    """A control with only what ``check`` reads; ``_row`` hands back the row's value, ``material`` a fixed one."""
    control = WorkPolicyControl.__new__(WorkPolicyControl)
    control.records = {} if saved is None else {"binding": saved}
    control.peer_roots, control.profile_selections = {}, {}
    control.policy = SimpleNamespace(authority_domain="domain")
    control.runtime = SimpleNamespace(_now=lambda: now)
    active = SimpleNamespace(**{"policy_id": "policy", "active": True, "read_enabled": True, **current})
    control.installed = lambda rows: active
    monkeypatch.setattr(work, "_row", lambda rows, key, model: rows[key].value)
    monkeypatch.setattr(work, "material", lambda data: SimpleNamespace(model_dump=lambda mode: "same"))
    return control, {KEY: Row(saved)}


def refused(control, rows, value=None, *, use="execute"):
    with pytest.raises(Problem) as denied:
        control.check(BINDING, value or policy(), rows, use=use)
    return denied.value.status, denied.value.body["error"]["code"], denied.value.body["error"]["message"]


SCOPE = (403, "WORK_POLICY_SCOPE_CHANGED", "current original work/read scope changed")


def test_the_current_registration_passes(monkeypatch):
    control, rows = gated(monkeypatch, record())
    assert control.check(BINDING, policy(), rows, use="execute") is None


def test_an_uncached_work_is_refused(monkeypatch):
    control, rows = gated(monkeypatch, None)
    assert refused(control, rows) == SCOPE


@pytest.mark.parametrize("value", [
    policy(policy_id="another"), policy(admission_id="another"), policy(job_id="another"), policy(instruction=True)])
def test_a_runtime_policy_of_other_work_is_refused(monkeypatch, value):
    control, rows = gated(monkeypatch, record())
    assert refused(control, rows, value) == SCOPE


def test_a_binding_other_than_the_registered_one_is_refused(monkeypatch):
    saved = record()
    saved.binding = SimpleNamespace(binding_id="binding", job_id="another")
    control, rows = gated(monkeypatch, saved)
    assert refused(control, rows, policy()) == SCOPE


def test_an_instruction_policy_must_have_a_registered_instruction(monkeypatch):
    saved = record(instruction=SimpleNamespace(peer_work=None))
    control, rows = gated(monkeypatch, saved)
    assert refused(control, rows, policy(instruction=False)) == SCOPE
    assert control.check(BINDING, policy(instruction=True), rows, use="execute") is None


@pytest.mark.parametrize("use", ["read", "execute", "control"])
def test_a_revoked_policy_refuses_every_use(monkeypatch, use):
    control, rows = gated(monkeypatch, record(), active=False)
    assert refused(control, rows, use=use) == SCOPE


def test_only_a_read_needs_read_rights(monkeypatch):
    control, rows = gated(monkeypatch, record(), read_enabled=False)
    assert refused(control, rows, use="read") == SCOPE
    assert control.check(BINDING, policy(), rows, use="execute") is None


def test_a_transaction_row_that_differs_from_the_cached_registration_is_refused(monkeypatch):
    control, rows = gated(monkeypatch, record())
    rows[KEY] = Row(record(selection="changed"))
    assert refused(control, rows) == (403, "WORK_POLICY_SCOPE_CHANGED", "current transaction registration differs")


def test_a_row_with_other_material_is_refused(monkeypatch):
    control, rows = gated(monkeypatch, record())
    rows[KEY] = Row(rows[KEY].value, material="other")
    assert refused(control, rows) == (403, "WORK_POLICY_SCOPE_CHANGED", "current transaction registration differs")


def peer(monkeypatch):
    from shared_context import behavior_agent_peer_work, behavior_delegation

    reference = SimpleNamespace(root_id="root")
    saved = record(instruction=SimpleNamespace(peer_work=reference))
    control, rows = gated(monkeypatch, saved)
    root = SimpleNamespace(origin="origin")
    control.peer_roots["root"] = root
    rows["root"] = Row(root)
    calls = []
    monkeypatch.setattr(work, "registered_instruction", lambda value: "the-instruction")
    monkeypatch.setattr(behavior_agent_peer_work, "validate_reference", lambda *args: calls.append(("reference", *args)))
    monkeypatch.setattr(behavior_delegation, "_current_parent",
                        lambda rows, origin, *, executing: calls.append(("parent", origin, executing)))
    return control, rows, reference, root, calls


@pytest.mark.parametrize("use,executing", [("execute", True), ("settle", True), ("read", False), ("control", False)])
def test_peer_work_checks_its_reference_then_its_parent(monkeypatch, use, executing):
    control, rows, reference, root, calls = peer(monkeypatch)
    control.check(BINDING, policy(instruction=True, valid_until=0), rows, use=use)
    assert calls[0][:2] == ("reference", root) and calls[0][3:] == (reference, "request", "the-instruction")
    assert calls[1] == ("parent", "origin", executing)


def test_a_peer_root_that_changed_is_refused_before_its_reference(monkeypatch):
    control, rows, _, _, calls = peer(monkeypatch)
    rows["root"] = Row(SimpleNamespace(origin="another"))
    assert refused(control, rows, policy(instruction=True)) == (
        403, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "the immutable original peer root changed")
    assert calls == []


def selected(monkeypatch, *, agent_scope=None, cached="choice", material="same"):
    saved = record(selection="selection", agent_scope=agent_scope)
    control, rows = gated(monkeypatch, saved)
    key = selection_key("domain", "selection", None if agent_scope is None else agent_scope.agent_id)
    choice = Record(name="choice")
    rows[key] = Row(choice, material)
    control.profile_selections["selection"] = choice if cached == "choice" else cached
    return control, rows


@pytest.mark.parametrize("agent_scope", [None, SimpleNamespace(agent_id="agent")])
def test_the_saved_selection_passes_under_its_agents_key(monkeypatch, agent_scope):
    control, rows = selected(monkeypatch, agent_scope=agent_scope)
    assert control.check(BINDING, policy(), rows, use="execute") is None


@pytest.mark.parametrize("changes", [{"cached": None}, {"material": "other"}])
def test_a_selection_that_changed_is_refused(monkeypatch, changes):
    control, rows = selected(monkeypatch, **changes)
    assert refused(control, rows) == (403, "PROFILE_SELECTION_INVALID", "the original approved profile selection changed")


def remembered(monkeypatch, *, material="same"):
    from shared_context import memory_bridge_settings

    scope = SimpleNamespace(settings_id="memory-settings")
    control, rows = gated(monkeypatch, record(memory_scope=scope))
    setting = Record(name="setting")
    rows["memory-settings"] = Row(setting, material)
    seen = []
    monkeypatch.setattr(memory_bridge_settings, "validate_capture_record", lambda *args: seen.append(args))
    return control, rows, scope, setting, seen


def test_the_memory_setting_is_validated_and_passes(monkeypatch):
    control, rows, scope, setting, seen = remembered(monkeypatch)
    assert control.check(BINDING, policy(), rows, use="execute") is None
    assert seen == [(control.policy, scope, setting)]


def test_a_memory_setting_with_other_material_is_refused_after_validation(monkeypatch):
    control, rows, _, _, seen = remembered(monkeypatch, material="other")
    assert refused(control, rows) == (403, "MEMORY_SETTINGS_INVALID", "the original memory setting changed")
    assert len(seen) == 1


@pytest.mark.parametrize("use,at,limited", [("execute", 99, False), ("execute", 100, True), ("read", 100, False),
                                            ("control", 100, False)])
def test_only_execution_of_fixed_work_has_a_deadline(monkeypatch, use, at, limited):
    control, rows = gated(monkeypatch, record(), now=at)
    if limited:
        assert refused(control, rows, use=use) == (
            403, "WORK_EXECUTION_DEADLINE", "only this work's execution permission has expired")
    else:
        assert control.check(BINDING, policy(), rows, use=use) is None


def test_an_instruction_has_no_execution_deadline(monkeypatch):
    saved = record(instruction=SimpleNamespace(peer_work=None))
    control, rows = gated(monkeypatch, saved, now=1000)
    assert control.check(BINDING, policy(instruction=True, valid_until=None), rows, use="execute") is None


# preflight

def sha(command):
    return hashlib.sha256(command.model_dump_json().encode()).hexdigest()


def fixed(rid="fixed-work", **changes):
    command = AssistantSubmit(request_id=rid)
    return WorkPreflightQuery(read_id="read", request_id=rid, submit_body_sha256=sha(command), **changes)


def asked(command, **changes):
    return WorkPreflightQuery(read_id="read", request_id=command.request_id, submit_body_sha256=sha(command),
                              content=command.content, **{**({"conversation_id": command.conversation_id}
                                                             if command.conversation_id else {}), **changes})


def replaced(monkeypatch, **models):
    """``_row`` of one model answers with these fields changed."""
    real = work._row
    changes = dict(zip(models["models"], models["fields"]))

    def row(rows, key, model):
        value = real(rows, key, model)
        return value.model_copy(update=changes[model]) if model in changes else value

    monkeypatch.setattr(work, "_row", row)


def test_preflight_asks_for_the_human_before_it_reads_the_query(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1

    def refused_human(principal, *args):
        raise Problem(403, "OFFLINE_HUMAN", "OFFLINE")

    monkeypatch.setattr(c.control, "_human", refused_human)
    assert code(c.control.preflight, c.human, "not a query")[:2] == (403, "OFFLINE_HUMAN")


def test_preflight_needs_the_exact_typed_query(synthetic_standing_v1):
    c = synthetic_standing_v1

    class Other(WorkPreflightQuery):
        pass

    assert code(c.control.preflight, c.human, Other(**fixed().model_dump()))[:2] == (422, "WORK_POLICY_QUERY")


def test_preflight_refuses_attachments_without_an_instruction_before_comparing_bytes(synthetic_standing_v1):
    c = synthetic_standing_v1
    query = WorkPreflightQuery.model_construct(
        read_id="read", request_id="fixed-work", submit_body_sha256="f" * 64, content=None, conversation_id=None,
        model=None, attachments=("anything",), files=())
    assert code(c.control.preflight, c.human, query)[:2] == (422, "ATTACHMENT_FORMAT")


def test_preflight_with_other_bytes_is_refused_before_any_refresh(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    monkeypatch.setattr(c.control, "refresh", lambda: pytest.fail("no refresh for other bytes"))
    query = fixed().model_copy(update={"submit_body_sha256": "f" * 64})
    assert code(c.control.preflight, c.human, query) == (
        409, "WORK_POLICY_REQUEST_CONFLICT", "canonical fixed submission bytes differ")


def test_preflight_hashes_the_instruction_with_all_its_fields(synthetic_standing_v1):
    c = synthetic_standing_v1
    command = instruction("instruction-bytes", conversation_id="some-conversation", model="gpt-4o")
    query = asked(command, model="gpt-4o")
    assert c.control.preflight(c.human, query).submit_body_sha256 == sha(command)
    other = query.model_copy(update={"model": None})
    assert code(c.control.preflight, c.human, other)[:2] == (409, "WORK_POLICY_REQUEST_CONFLICT")


def test_a_snapshot_that_moved_during_preflight_is_not_permission(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    store = c.control.store
    real, seen = store._guard, []

    def guard():
        value, etag = real()
        if not sys._getframe(1).f_code.co_filename.endswith("behavior_work_policy.py"):
            return value, etag  # The store's own reads keep their guard.
        seen.append(True)
        return value, etag + "-moved" if len(seen) == 2 else etag

    monkeypatch.setattr(store, "_guard", guard)
    assert code(c.control.preflight, c.human, asked(instruction("guard-moved"))) == (
        412, "WORK_POLICY_READ_CONFLICT", "pure preflight snapshot changed; it is not permission")


def test_new_fixed_work_is_denied_by_a_source_that_is_not_ready(structured, monkeypatch):
    c = structured

    def not_ready(principal, policy):
        raise Problem(403, "WORK_POLICY_SOURCE_UNAVAILABLE", "OFFLINE")

    monkeypatch.setattr(c.control, "source_ready", not_ready)
    result = c.control.preflight(c.human, fixed())
    assert (result.decision, result.reason) == ("deny", "WORK_POLICY_SOURCE_UNAVAILABLE")
    assert result.monetary_budget.decision == "allow"


def test_a_money_refusal_stands_without_asking_the_source(structured, monkeypatch):
    c = structured
    monkeypatch.setattr(c.control, "source_ready", lambda *_: pytest.fail("the money decision already refused"))

    def decision(*args, **kwargs):
        return work.WorkPolicyDecision(
            read_id="read", policy_id=c.control.policy.policy_id, policy_revision=c.control.policy.revision,
            decision="deny", reason="cumulative_budget_exhausted", observed_at=c.h.time, budget_id=c.control.cost_key,
            remaining_microusd=None, required_microusd=0, observed_high_water_microusd=0, reserved_microusd=0,
            budget_enforced=False)

    monkeypatch.setattr(work, "cumulative_decision", decision)
    result = c.control.preflight(c.human, fixed())
    assert (result.decision, result.reason) == ("deny", "cumulative_budget_exhausted")


def root_fields(c):
    worker = c.control.runtime.dep.workers.contract.reservation_units
    requests = c.control.policy.max_model_requests
    return requests, worker


@pytest.mark.parametrize("attempts,units,active,denied", [
    (3, 2, True, False), (2, 2, True, True), (3, 1, True, True), (3, 2, False, True)])
def test_the_abstract_root_budget_refuses_new_fixed_work(structured, monkeypatch, attempts, units, active, denied):
    c = structured
    requests, worker = root_fields(c)
    replaced(monkeypatch, models=(BudgetRecord,), fields=({
        "active": active, "remaining_attempts": requests + attempts, "remaining_units": requests + units + worker},))
    result = c.control.preflight(c.human, fixed())
    assert (result.decision, result.reason) == (
        ("deny", "abstract_root_budget_exhausted") if denied else ("allow", "allowed"))
    assert result.abstract_attempts_remaining == requests + attempts


def test_the_root_budget_outranks_the_source_reason(structured, monkeypatch):
    c = structured

    def not_ready(principal, policy):
        raise Problem(403, "WORK_POLICY_SOURCE_UNAVAILABLE", "OFFLINE")

    monkeypatch.setattr(c.control, "source_ready", not_ready)
    replaced(monkeypatch, models=(BudgetRecord,), fields=({"active": False},))
    assert c.control.preflight(c.human, fixed()).reason == "abstract_root_budget_exhausted"


def test_an_instruction_is_not_held_to_the_root_budget_or_the_source(structured, monkeypatch):
    c = structured
    monkeypatch.setattr(c.control, "source_ready", lambda *_: pytest.fail("an instruction has no reviewed source"))
    replaced(monkeypatch, models=(BudgetRecord,), fields=({"active": False},))
    assert c.control.preflight(c.human, asked(instruction("free-of-root"))).decision == "allow"


@pytest.fixture
def replay(structured, monkeypatch):
    """Fixed work already admitted: only its replay decision counts."""
    c = structured
    c.command = AssistantSubmit(request_id="fixed-work")
    c.bound, _ = c.control.submit(c.human, c.command)
    monkeypatch.setattr(work, "cumulative_decision", lambda *_a, **_k: pytest.fail("a replay needs no new cost decision"))
    monkeypatch.setattr(c.control, "source_ready", lambda *_: pytest.fail("a replay needs no source"))
    c.authorized = []
    authorize = c.control.runtime.authorize

    def spy(binding_id, principal, use, **kwargs):
        c.authorized.append((binding_id, use))
        return authorize(binding_id, principal, use, **kwargs)

    monkeypatch.setattr(c.control.runtime, "authorize", spy)
    return c


def test_an_admitted_work_replays_without_a_new_decision(replay, monkeypatch):
    c = replay
    replaced(monkeypatch, models=(BudgetRecord,), fields=({"active": False},))
    result = c.control.preflight(c.human, fixed())
    money = result.monetary_budget
    assert (result.decision, result.reason, result.already_admitted) == ("allow", "original_admission_replay_only", True)
    assert (money.required_microusd, money.budget_id) == (0, c.control.cost_key)
    assert c.authorized == [(c.bound.binding_id, "read")]


def test_an_enforced_replay_reports_what_remains(replay, monkeypatch):
    c = replay
    values = {"budget_enforced": True, "observed_high_water_microusd": 10, "legacy_unresolved_reserve_microusd": 20,
              "work_reservations_microusd": 30}
    replaced(monkeypatch, models=(CostLedger,), fields=(values,))
    money = c.control.preflight(c.human, fixed()).monetary_budget
    policy_now = c.control.policy
    assert money.remaining_microusd == max(0, policy_now.cumulative_ceiling_microusd - policy_now.safety_reserve_microusd - 60)
    assert (money.reserved_microusd, money.observed_high_water_microusd) == (50, 10)
    assert money.safety_reserve_microusd == policy_now.safety_reserve_microusd
    assert money.cumulative_ceiling_microusd == policy_now.cumulative_ceiling_microusd


def test_an_overspent_replay_has_nothing_left_not_a_negative(replay, monkeypatch):
    c = replay
    replaced(monkeypatch, models=(CostLedger,), fields=({
        "budget_enforced": True, "observed_high_water_microusd": 10 ** 15},))
    assert c.control.preflight(c.human, fixed()).monetary_budget.remaining_microusd == 0


def test_an_unenforced_replay_reports_no_budget(replay, monkeypatch):
    c = replay
    replaced(monkeypatch, models=(CostLedger,), fields=({"budget_enforced": False},))
    money = c.control.preflight(c.human, fixed()).monetary_budget
    assert (money.remaining_microusd, money.safety_reserve_microusd, money.cumulative_ceiling_microusd,
            money.budget_enforced) == (None, None, None, False)


def test_a_preview_reports_the_queue_of_its_work(replay):
    c = replay
    result = c.control.preflight(c.human, fixed())
    saved = c.control.records[c.bound.binding_id].runtime_policy
    assert (result.queue_deadline, result.execution_deadline) == (saved.queue_deadline, None)


def test_a_dispatched_work_reports_its_execution_deadline(replay, monkeypatch):
    c = replay
    saved = c.control.records[c.bound.binding_id].runtime_policy
    replaced(monkeypatch, models=(PerWorkRuntimePolicy,), fields=({"stage": "dispatched"},))
    assert c.control.preflight(c.human, fixed()).execution_deadline == saved.valid_until


def test_work_with_no_runtime_policy_reports_no_deadline(structured):
    result = structured.control.preflight(structured.human, fixed())
    assert (result.queue_deadline, result.execution_deadline, result.already_admitted) == (None, None, False)
    assert (result.conversation_id, result.turn_number, result.model) == (None, None, None)


@pytest.mark.parametrize("busy,available", [(True, False), (False, True)])
def test_a_preview_reports_whether_a_slot_is_free(structured, monkeypatch, busy, available):
    c = structured
    real = work._row

    def row(rows, key, model):
        value = real(rows, key, model)
        if model is _Pool:
            return SimpleNamespace(slots=tuple(SimpleNamespace(binding="taken" if busy else None) for _ in value.slots))
        return value

    monkeypatch.setattr(work, "_row", row)
    assert c.control.preflight(c.human, fixed()).capacity_available is available


def test_a_new_instruction_previews_its_turn_and_model(synthetic_standing_v1):
    c = synthetic_standing_v1
    result = c.control.preflight(c.human, asked(instruction("first-preview")))
    assert (result.decision, result.turn_number, result.already_admitted) == ("allow", 1, False)
    assert result.conversation_id
    assert result.model == c.control.models.select(None).id


def test_a_saved_instruction_previews_what_was_saved(admitted, monkeypatch):
    from shared_context import behavior_instruction

    c = admitted
    monkeypatch.setattr(behavior_instruction, "instruction_state", lambda *_: pytest.fail("a saved turn reads no conversation"))
    result = c.control.preflight(c.human, asked(c.command))
    assert (result.binding, result.already_admitted) == (c.record.binding, False)
    assert result.conversation_id == c.record.profile.instruction.conversation_id
    assert result.turn_number == c.record.profile.instruction.turn_number


def test_a_saved_instruction_with_other_bytes_is_a_conflict(admitted, monkeypatch):
    c = admitted
    monkeypatch.setattr(c.control, "refresh", lambda: None)
    saved = c.control.records[c.record.binding.binding_id]
    c.control.records[c.record.binding.binding_id] = saved.model_copy(update={"submit_sha256": "f" * 64})
    assert code(c.control.preflight, c.human, asked(c.command)) == (
        409, "WORK_POLICY_REQUEST_CONFLICT", "original input differs")


def test_a_saved_instruction_whose_job_is_not_there_yet_is_still_authorized(admitted, monkeypatch):
    from shared_context.behavior_workers import worker_key

    c = admitted
    binding = c.record.binding
    Reads(c.control.store, monkeypatch).row(worker_key("job", binding.job_id), None)
    authorized = []
    authorize = c.control.runtime.authorize

    def spy(binding_id, principal, use, **kwargs):
        authorized.append((binding_id, use))
        return authorize(binding_id, principal, use, **kwargs)

    monkeypatch.setattr(c.control.runtime, "authorize", spy)
    result = c.control.preflight(c.human, asked(c.command))
    assert authorized == [(binding.binding_id, "read")] and result.already_admitted is False


def test_an_admitted_instruction_is_authorized_once(admitted, monkeypatch):
    from shared_context.behavior_workers import worker_key

    c = admitted
    binding = c.record.binding
    Reads(c.control.store, monkeypatch).row(worker_key("job", binding.job_id), SimpleNamespace())
    authorized = []
    monkeypatch.setattr(c.control.runtime, "authorize", lambda binding_id, _principal, use: authorized.append((binding_id, use)))
    result = c.control.preflight(c.human, asked(c.command))
    assert authorized == [(binding.binding_id, "read")] and result.already_admitted is True
    assert result.reason == "original_admission_replay_only"


@pytest.mark.parametrize("failure", ["AGENT_ROLE_PROMPT_MISSING", "AGENT_ROLE_PROMPT_INVALID"])
def test_a_role_agent_with_no_saved_role_is_a_clear_refusal(synthetic_standing_v1, monkeypatch, failure):
    from shared_context import behavior_agent_role_prompt_versions as versions

    c = synthetic_standing_v1

    def missing(*_args):
        raise Problem(409, failure, "OFFLINE")

    monkeypatch.setattr(versions, "captured_role_prompt", missing)
    assert code(c.control.preflight, c.human, asked(instruction("no-role")))[:2] == (409, failure)


def test_a_turn_that_can_not_be_derived_is_a_deny_with_its_code(synthetic_standing_v1, monkeypatch):
    from shared_context import behavior_instruction

    c = synthetic_standing_v1

    def refused_state(*_args):
        raise Problem(409, "OFFLINE_CONVERSATION", "OFFLINE")

    monkeypatch.setattr(behavior_instruction, "instruction_state", refused_state)
    result = c.control.preflight(c.human, asked(instruction("no-conversation")))
    assert (result.decision, result.reason) == ("deny", "OFFLINE_CONVERSATION")
    assert (result.conversation_id, result.turn_number, result.model, result.memory_setting_ref) == (None, None, None, None)


def test_a_failure_late_in_the_derivation_keeps_the_binding_it_reached(synthetic_standing_v1, monkeypatch):
    from shared_context.behavior_work_policy import derive_work_binding

    c = synthetic_standing_v1
    command = instruction("late-failure")
    expected = derive_work_binding(c.control.policy, command.request_id, instruction=command)[1]

    def refused_work(*_args, **_kwargs):
        raise Problem(409, "OFFLINE_DERIVE", "OFFLINE")

    monkeypatch.setattr(work, "derive_work", refused_work)
    result = c.control.preflight(c.human, asked(command))
    assert (result.reason, result.binding) == ("OFFLINE_DERIVE", expected)


# refresh

def reads_of(control, monkeypatch):
    real, seen = control.store.read_snapshot, []

    def read(keys):
        seen.append(tuple(keys))
        return real(keys)

    monkeypatch.setattr(control.store, "read_snapshot", read)
    return seen


def test_one_phase_refreshes_one_binding_once(admitted, monkeypatch):
    c = admitted
    binding_id = c.record.binding.binding_id
    with refresh_once():
        c.control.refresh(binding_id)
        seen = reads_of(c.control, monkeypatch)
        c.control.refresh(binding_id)
        assert seen == []
        c.control.refresh()
        assert seen != []


def test_a_binding_missing_from_the_cache_is_refreshed_again_in_its_phase(admitted, monkeypatch):
    c = admitted
    binding_id = c.record.binding.binding_id
    with refresh_once():
        c.control.refresh(binding_id)
        saved = c.control.records.pop(binding_id)
        seen = reads_of(c.control, monkeypatch)
        c.control.refresh(binding_id)
    assert seen != [] and c.control.records[binding_id] == saved


def test_outside_a_phase_every_refresh_reads(admitted, monkeypatch):
    c = admitted
    binding_id = c.record.binding.binding_id
    c.control.refresh(binding_id)
    seen = reads_of(c.control, monkeypatch)
    c.control.refresh(binding_id)
    assert seen != []


def test_a_scoped_refresh_reads_only_its_own_registration(admitted, monkeypatch):
    c = admitted
    other = register(c, instruction("second-registered"))
    seen = reads_of(c.control, monkeypatch)
    c.control.refresh(c.record.binding.binding_id)
    registrations = [keys for keys in seen if keys == (c.registration,)]
    assert len(registrations) == 1
    assert (work_policy_key("registration", other.admission_id),) not in seen


def test_a_binding_the_index_does_not_list_reads_every_registration(admitted, monkeypatch):
    c = admitted
    other = register(c, instruction("second-registered"))
    c.control.records.pop(other.binding.binding_id)
    seen = reads_of(c.control, monkeypatch)
    c.control.refresh(other.binding.binding_id)
    assert (c.registration,) in seen and (work_policy_key("registration", other.admission_id),) in seen
    assert other.binding.binding_id in c.control.records


def test_a_registry_of_another_policy_is_refused(admitted, monkeypatch):
    c = admitted
    monkeypatch.setattr(registry, "read_registry", lambda *_: SimpleNamespace(policy_id="another", entries=()))
    assert code(c.control.refresh)[:2] == (409, "WORK_POLICY_REGISTRY_INVALID")


def test_a_listed_registration_that_is_gone_is_refused(admitted, monkeypatch):
    c = admitted
    Reads(c.control.store, monkeypatch).row(c.registration, None)
    assert code(c.control.refresh) == (409, "WORK_POLICY_REGISTRY_INVALID", "original registered work is missing")


def test_a_row_with_other_material_is_refused_by_the_refresh(admitted, monkeypatch):
    c = admitted
    real = c.control.store.read_snapshot

    def tampered(keys):
        rows = dict(real(keys))
        if c.registration in rows:
            row = rows[c.registration]
            rows[c.registration] = SimpleNamespace(payload={**row.payload, "material": {"tampered": True}})
        return rows

    monkeypatch.setattr(c.control.store, "read_snapshot", tampered)
    assert code(c.control.refresh) == (409, "WORK_POLICY_REGISTRY_INVALID", "registered original work differs")


@pytest.mark.parametrize("wrong", ["admission", "binding", "profile"])
def test_a_registration_that_does_not_derive_again_is_refused(admitted, monkeypatch, wrong):
    c = admitted
    real = work.derive_registered_work

    def other(*args, **kwargs):
        admission, binding, profile = real(*args, **kwargs)
        return (("another" if wrong == "admission" else admission),
                (binding.model_copy(update={"job_id": "another"}) if wrong == "binding" else binding),
                (profile.model_copy(update={"prompt_profile": "another"}) if wrong == "profile" else profile))

    monkeypatch.setattr(work, "derive_registered_work", other)
    assert code(c.control.refresh) == (409, "WORK_POLICY_REGISTRY_INVALID", "registered original work differs")


def test_a_turn_with_another_capacity_profile_is_refused(admitted, monkeypatch):
    c = admitted
    real_parse = work._parse

    def parsed(value, model):
        record = real_parse(value, model)
        if model is not work.WorkRegistration:
            return record
        profile = record.profile.model_copy(update={"model_capacity_profile": "another-capacity"})
        return record.model_copy(update={"profile": profile})

    monkeypatch.setattr(work, "_parse", parsed)
    monkeypatch.setattr(work, "derive_registered_work", lambda _policy, record, _mode, **_k: (
        record.admission_id, record.binding, record.profile))
    monkeypatch.setattr(work, "material", lambda data: SimpleNamespace(model_dump=lambda mode: None))
    real = c.control.store.read_snapshot

    def same_material(keys):
        rows = dict(real(keys))
        if c.registration in rows:
            rows[c.registration] = SimpleNamespace(payload={**rows[c.registration].payload, "material": None})
        return rows

    monkeypatch.setattr(c.control.store, "read_snapshot", same_material)
    assert code(c.control.refresh) == (409, "WORK_POLICY_REGISTRY_INVALID", "registered original work differs")


def session_case(c, monkeypatch, *, hosted="another-session", agent_vm=None, transfer=None):
    from shared_context import behavior_conversation_transfer as transfers

    real_parse = work._parse

    def parsed(value, model):
        record = real_parse(value, model)
        if model is not work.WorkRegistration:
            return record
        return record.model_copy(update={"hosted_session_id": hosted, "agent_vm": agent_vm})

    monkeypatch.setattr(work, "_parse", parsed)
    monkeypatch.setattr(transfers, "registration_transfer", lambda _control, _record: transfer)
    monkeypatch.setattr(work, "derive_registered_work", lambda _policy, record, _mode, **_k: (
        record.admission_id, record.binding, record.profile))
    monkeypatch.setattr(work, "material", lambda data: SimpleNamespace(model_dump=lambda mode: None))
    real = c.control.store.read_snapshot

    def same_material(keys):
        rows = dict(real(keys))
        if c.registration in rows:
            rows[c.registration] = SimpleNamespace(payload={**rows[c.registration].payload, "material": None})
        return rows

    monkeypatch.setattr(c.control.store, "read_snapshot", same_material)


VM = AgentVmBinding(team_id="team", agent_id="agent", vm_generation="generation-one",
                    hosted_session_id="another-session", hosted_agent_version="41")


def test_a_session_outside_its_conversation_is_refused(admitted, monkeypatch):
    c = admitted
    session_case(c, monkeypatch)
    assert code(c.control.refresh) == (
        409, "WORK_POLICY_REGISTRY_INVALID", "Hosted session is outside this original conversation")


@pytest.mark.parametrize("changes", [{"agent_vm": VM}, {"transfer": object()}, {"hosted": None}])
def test_a_vm_or_a_transfer_explains_another_session(admitted, monkeypatch, changes):
    c = admitted
    session_case(c, monkeypatch, **changes)
    assert c.control.refresh() is None


def test_a_session_on_a_turn_with_no_conversation_is_refused(structured, monkeypatch):
    c = structured
    bound, _ = c.control.submit(c.human, AssistantSubmit(request_id="fixed-work"))
    c.registration = work_policy_key("registration", c.control.records[bound.binding_id].admission_id)
    session_case(c, monkeypatch, transfer=object())
    assert code(c.control.refresh) == (
        409, "WORK_POLICY_REGISTRY_INVALID", "Hosted session is outside this original conversation")


def test_a_saved_selection_is_cached_and_derives_the_turn(admitted, monkeypatch):
    from shared_context import behavior_profile_selection as selection

    c = admitted
    real_parse = work._parse

    def parsed(value, model):
        record = real_parse(value, model)
        return record.model_copy(update={"profile_selection_id": "selection"}) if model is work.WorkRegistration else record

    monkeypatch.setattr(work, "_parse", parsed)
    choice = SimpleNamespace(selection_id="selection", command=SimpleNamespace(template="the-template"))
    monkeypatch.setattr(selection, "read_selection", lambda _store, _policy, selection_id: choice)
    seen = []

    def derive(_policy, record, _mode, *, profile_template):
        seen.append(profile_template)
        raise Problem(409, "OFFLINE_STOP", "OFFLINE")

    monkeypatch.setattr(work, "derive_registered_work", derive)
    assert code(c.control.refresh)[1] == "OFFLINE_STOP"
    assert seen == ["the-template"] and c.control.profile_selections["selection"] is choice


def test_no_selection_derives_with_no_template(admitted, monkeypatch):
    c = admitted
    seen = []

    def derive(_policy, record, _mode, *, profile_template):
        seen.append(profile_template)
        raise Problem(409, "OFFLINE_STOP", "OFFLINE")

    monkeypatch.setattr(work, "derive_registered_work", derive)
    assert code(c.control.refresh)[1] == "OFFLINE_STOP" and seen == [None]


def test_a_captured_memory_setting_is_read_and_validated_before_the_derivation(admitted, monkeypatch):
    from shared_context import memory_bridge_settings as settings

    c = admitted
    real_parse = work._parse
    scope = SimpleNamespace(settings_id="memory-settings")

    def parsed(value, model):
        record = real_parse(value, model)
        return _with_memory(record, scope) if model is work.WorkRegistration else record

    monkeypatch.setattr(work, "_parse", parsed)
    order = []
    monkeypatch.setattr(settings, "read_memory_settings", lambda _store, _policy, key: order.append(("read", key)) or "setting")
    monkeypatch.setattr(settings, "validate_capture_record", lambda _policy, seen, value: order.append(("check", seen, value)))

    def derive(*_args, **_kwargs):
        order.append("derive")
        raise Problem(409, "OFFLINE_STOP", "OFFLINE")

    monkeypatch.setattr(work, "derive_registered_work", derive)
    assert code(c.control.refresh)[1] == "OFFLINE_STOP"
    assert order == [("read", "memory-settings"), ("check", scope, "setting"), "derive"]


def _with_memory(record, scope):
    class Wrapped:
        def __getattr__(self, name):
            return getattr(record, name)

    wrapped = Wrapped()
    wrapped.profile = SimpleNamespace(instruction=record.profile.instruction, memory_scope=scope)
    return wrapped


def test_work_the_index_dropped_leaves_the_cache_and_an_admitted_turn_stays(admitted, monkeypatch):
    c = admitted
    binding_id = c.record.binding.binding_id
    dropped = SimpleNamespace(admission_id="dropped", turn_authorization=None,
                              binding=c.record.binding, profile=c.record.profile)
    c.control.records["dropped"] = dropped
    monkeypatch.setattr(registry, "read_registry", lambda *_: SimpleNamespace(
        policy_id=c.control.policy.policy_id, entries=()))
    c.control.refresh()
    assert "dropped" not in c.control.records
    assert binding_id in c.control.records and binding_id in c.control.runtime.bindings
    assert "dropped" not in c.control.runtime.profiles


def test_work_cached_after_the_registry_read_is_never_dropped_by_it(admitted, monkeypatch):
    c = admitted
    late = SimpleNamespace(admission_id="late", turn_authorization=None, binding=c.record.binding, profile=c.record.profile)
    real = c.control.store.read_snapshot

    def read(keys):
        rows = real(keys)
        if c.control.registry_key in keys:
            # Another thread caches its new work right after this refresh read the (older) registry.
            c.control.records["late"] = late
        return rows

    monkeypatch.setattr(c.control.store, "read_snapshot", read)
    c.control.refresh()
    assert c.control.records["late"] is late


def test_the_base_bindings_stay_in_the_runtimes_view(admitted):
    c = admitted
    c.control.refresh()
    assert set(c.control.base_bindings) <= set(c.control.runtime.bindings)
    assert set(c.control.base_profiles) <= set(c.control.runtime.profiles)
    binding_id = c.record.binding.binding_id
    assert c.control.runtime.bindings[binding_id] == c.record.binding
    assert c.control.runtime.profiles[binding_id] == c.record.profile


def test_a_native_runtime_routes_every_cached_job(admitted):
    from shared_context.behavior_foundry import native_key

    c = admitted
    launches = c.control.runtime.dep.native_launches
    assert launches is not None
    job = c.record.binding.job_id
    launches.jobs.pop(job, None)
    c.control.refresh()
    route = launches.jobs[job]
    assert (route.job_id, route.api_binding_id, route.grant_id) == (
        job, c.record.binding.binding_id, native_key("bootstrap-grant", job))


def test_the_phase_mark_is_set_only_for_a_finished_scoped_refresh(admitted, monkeypatch):
    c = admitted
    binding_id = c.record.binding.binding_id
    with refresh_once():
        real = c.control.installed
        monkeypatch.setattr(c.control, "installed", lambda rows=None: (_ for _ in ()).throw(Problem(403, "OFFLINE", "OFFLINE")))
        assert code(c.control.refresh, binding_id)[1] == "OFFLINE"
        monkeypatch.setattr(c.control, "installed", real)
        seen = reads_of(c.control, monkeypatch)
        c.control.refresh(binding_id)
        assert seen != []


def test_runtime_key_is_the_policy_row_preflight_reads(replay, monkeypatch):
    c = replay
    seen = reads_of(c.control, monkeypatch)
    c.control.preflight(c.human, fixed())
    read = next(keys for keys in seen if c.control.cost_key in keys and len(keys) == 8)
    binding = c.bound
    assert read == (c.control.policy_key, c.control.revocation_key, c.control.cost_key,
                    control_key("budget", binding.owner.root_budget_id), read[4], read[5],
                    work_policy_key("registration", c.control.records[binding.binding_id].admission_id),
                    runtime_key("policy", binding.binding_id))
