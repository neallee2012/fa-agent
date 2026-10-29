"""Pins StandingPolicyReader.checked with the inputs a real instruction status read hands it."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from shared_context.behavior_work_policy import WorkPreflight
from test_behavior_assistant import assistant  # noqa: F401
from test_conversation_note_memory import enable_memory, memory_conversations  # noqa: F401
from test_instruction_flow import instruction_models, runtime_path  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_memory_preflight import memory_console  # noqa: F401
from web_test_console.adapter import ConsoleError

T0 = datetime(2026, 9, 15, 10, tzinfo=timezone.utc)
DROP = object()
REPLAY = "original_admission_replay_only"


def at(seconds, suffix="Z"):
    return (T0 + timedelta(seconds=seconds)).replace(tzinfo=None).isoformat() + suffix


class Pin:
    """The exact (query, task, binding, raw) one real read passed to checked, replayable with edits."""

    def __init__(self, c):
        enable_memory(c.h)
        draft = c.browser.ui.get("/api/state").json()["composer"]
        request_id = draft["request_id"]
        response = c.browser.ui.post(f"/api/instructions/{request_id}/submit",
            json={"content": "Pin the standing check.", "model": "gpt-4.1-mini"}, headers=c.browser.headers)
        assert response.status_code == 200, response.text
        self.reader = c.browser.app.state.console.instructions.reader
        calls, original = [], self.reader.checked
        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)
        self.reader.checked = spy
        assert c.browser.ui.post(f"/api/tasks/{request_id}/status", json={},
                                 headers=c.browser.headers).status_code == 200
        del self.reader.checked
        (self.query, self.task, self.binding, raw), kwargs = calls[-1]
        assert kwargs == {"instruction": True, "previous_guard": None}
        self.data = json.loads(raw)
        assert self.data["already_admitted"] is True and self.data["memory_setting_ref"]["state"] == "captured"
        assert self.data["observed_at"] == at(0) and self.data["guard_revision"] == 22
        self.reader.clock = lambda: T0
        self.reader.last_guard_revision = None

    def raw(self, changes):
        data = json.loads(json.dumps(self.data))
        for path, value in changes.items():
            *parents, leaf = path.split(".")
            node = data
            for key in parents:
                node = node[key]
            if value is DROP:
                del node[leaf]
            else:
                node[leaf] = value
        return json.dumps(data).encode()

    def check(self, changes=(), *, raw=None, task=None, binding=None, instruction=True, previous_guard=None):
        return self.reader.checked(self.query, task or self.task, binding or self.binding,
                                   raw if raw is not None else self.raw(dict(changes)),
                                   instruction=instruction, previous_guard=previous_guard)

    def rejects(self, changes=(), **kwargs):
        with pytest.raises(ConsoleError) as caught:
            self.check(changes, **kwargs)
        assert (caught.value.code, caught.value.status) == ("WORK_POLICY_INVALID", 502)


# Turns the captured replay into a consistent new (not yet admitted) read.
FRESH = {"already_admitted": False, "reason": "allowed", "monetary_budget.reason": "allowed"}
# The captured replay as a non-instruction standing read: no memory reference, a queue deadline.
STANDING = {"memory_setting_ref": DROP, "queue_deadline": at(300)}
STANDING_FRESH = {**STANDING, **FRESH, "queue_deadline": at(10), "monetary_budget.required_microusd": 2_000_000}
UNENFORCED = {"monetary_budget.budget_enforced": False, "monetary_budget.remaining_microusd": None,
              "monetary_budget.safety_reserve_microusd": None, "monetary_budget.cumulative_ceiling_microusd": None}


def over_budget(high_water):
    return {"monetary_budget.observed_high_water_microusd": high_water,
            "monetary_budget.remaining_microusd": max(0, 110_000_000 - 10_000_000 - high_water - 2_000_000)}


def test_instruction_replay_returns_the_exact_observation(memory_console):
    pin = Pin(memory_console)
    observation = pin.check()
    assert observation.query == pin.query
    assert (observation.decision, observation.reason, observation.revision) == ("allow", "within_budget", "22")
    assert (observation.observed_at, observation.execution_deadline) == (T0, None)
    budget = observation.budget
    assert (budget.budget_id, budget.unit, budget.remaining, budget.required, budget.enforced) == (
        pin.data["monetary_budget"]["budget_id"], "USD-micro", 52_520_000, 0, True)
    assert observation.preflight == WorkPreflight.model_validate_json(pin.raw({}))
    assert pin.reader.last_guard_revision is None
    pin.reader.last_guard_revision = 23  # A standing guard never gates an instruction read.
    assert pin.check(previous_guard=22).revision == "22"
    assert pin.check(over_budget(100_000_000)).budget.remaining == 0
    assert pin.check(UNENFORCED).reason == "accounting_only"
    assert pin.check(UNENFORCED).budget.remaining is None
    assert pin.check({**FRESH, "memory_setting_ref.state": "candidate"}).reason == "within_budget"
    later = pin.check({"observed_at": at(5), "monetary_budget.observed_at": at(5)})
    assert later.observed_at == T0 + timedelta(seconds=5)
    assert pin.check({"monetary_budget.observed_at": at(-5)}).observed_at == T0  # The read time, not the money time.
    slotted = observation.preflight.binding
    assert pin.check({"memory_setting_ref": DROP}, binding=slotted).preflight.memory_setting_ref is None


def test_instruction_rejects_every_drift(memory_console):
    pin = Pin(memory_console)
    pin.rejects(raw=b"not json")
    pin.rejects(raw=b"[]")
    pin.rejects({"authorization": DROP})
    pin.rejects({"monetary_budget.unit": DROP})
    pin.rejects({"monetary_budget.budget_enforced": True})
    pin.rejects({"memory_setting_ref": None})
    pin.rejects({"extra": 1})
    pin.rejects({"memory_setting_ref.admission_id": "0" * 64})
    pin.rejects({"memory_setting_ref.state": "candidate"})
    pin.rejects({"memory_setting_ref": DROP})  # The supplied binding lacks the memory slots then.
    pin.rejects({"queue_deadline": at(10)})
    pin.rejects({"execution_deadline": at(10), "queue_deadline": at(10)})
    pin.rejects({"monetary_budget.observed_at": at(1)})
    pin.rejects({"read_id": "other-read"})
    pin.rejects({"monetary_budget.read_id": "other-read"})
    pin.rejects({"read_id": "other-read", "monetary_budget.read_id": "other-read"})
    pin.rejects({"request_id": "other-request"})
    pin.rejects({"submit_body_sha256": "0" * 64})
    pin.rejects({"configuration_sha256": "0" * 64})
    pin.rejects({"policy_sha256": "0" * 64})
    for key in ("tenant_id", "team_id", "human_oid", "template_binding_id"):
        pin.rejects({key: "other-value"})
    pin.rejects({"binding.subject": "other-subject"})
    pin.rejects(task=pin.task.model_copy(update={"job_id": "0" * 64}))
    pin.rejects({"monetary_budget.policy_id": "other-policy"})
    pin.rejects({"monetary_budget.policy_revision": 2})
    pin.rejects({"monetary_budget.budget_id": "other-budget"})
    pin.rejects({"monetary_budget.remaining_microusd": 52_520_001})
    pin.rejects({"monetary_budget.budget_enforced": False})
    pin.rejects({**UNENFORCED, "monetary_budget.remaining_microusd": 52_520_000})
    pin.rejects({**UNENFORCED, "monetary_budget.safety_reserve_microusd": 10_000_000})
    pin.rejects({**UNENFORCED, "monetary_budget.cumulative_ceiling_microusd": 110_000_000})
    pin.rejects(over_budget(45_479_999))
    pin.rejects({"monetary_budget.required_microusd": 2_000_000})
    pin.rejects({**FRESH, "monetary_budget.required_microusd": 2_000_000})
    pin.rejects(previous_guard=23)
    pin.rejects({"decision": "deny"})
    pin.rejects({"monetary_budget.decision": "deny"})
    pin.rejects({"reason": "allowed"})
    pin.rejects({"monetary_budget.reason": "allowed"})
    pin.rejects({**FRESH, "reason": REPLAY, "monetary_budget.reason": REPLAY})
    pin.rejects({**FRESH, "reason": REPLAY})
    pin.rejects({**FRESH, "monetary_budget.decision": "deny"})
    pin.rejects({**FRESH, "reason": "other", "monetary_budget.reason": "other"})
    pin.rejects(instruction=False)
    # A memory reference belongs to instruction reads only, even on an otherwise valid standing read.
    pin.rejects({"queue_deadline": at(300)}, instruction=False)


def test_standing_read_bounds_deadlines_clock_guard_and_cost(memory_console):
    pin = Pin(memory_console)
    slotted = pin.check().preflight.binding
    def standing(changes=(), **kw):
        return pin.check({**STANDING, **dict(changes)}, binding=slotted, instruction=False, **kw)
    def rejected(changes=(), **kw):
        pin.rejects({**STANDING, **dict(changes)}, binding=slotted, instruction=False, **kw)
    observation = standing({"execution_deadline": at(240), "queue_deadline": at(10)})
    assert (observation.reason, observation.execution_deadline) == ("within_budget", T0 + timedelta(seconds=240))
    assert pin.reader.last_guard_revision == 22
    assert standing().revision == "22"
    assert standing(previous_guard=23).decision == "allow"  # previous_guard is for instruction reads only.
    rejected({"queue_deadline": DROP})
    rejected({"queue_deadline": None})
    rejected({"queue_deadline": at(301)})
    rejected({"queue_deadline": at(300, "+01:00")})
    rejected({"queue_deadline": at(300, "")})
    rejected({"execution_deadline": at(241), "queue_deadline": at(10)})
    rejected({"execution_deadline": at(200), "queue_deadline": at(-100)})
    rejected({"execution_deadline": at(10), "queue_deadline": None})
    rejected({"execution_deadline": at(200, "+01:00"), "queue_deadline": at(10)})
    rejected({"observed_at": at(1), "monetary_budget.observed_at": at(1), "queue_deadline": at(10)})
    pin.reader.last_guard_revision = 23
    rejected()
    pin.reader.last_guard_revision = 22

    def fresh(changes=()):
        return pin.check({**STANDING_FRESH, **dict(changes)}, binding=slotted, instruction=False)
    def rejected_fresh(changes=()):
        pin.rejects({**STANDING_FRESH, **dict(changes)}, binding=slotted, instruction=False)
    assert fresh().budget.required == 2_000_000
    assert fresh(over_budget(96_000_000)).budget.remaining == 2_000_000
    assert fresh({**over_budget(97_000_000), **UNENFORCED}).reason == "accounting_only"
    rejected_fresh(over_budget(97_000_000))
    rejected_fresh({"monetary_budget.required_microusd": 0})
    rejected_fresh({"execution_deadline": at(100)})
    deny = {"decision": "deny", "monetary_budget.decision": "deny"}
    for reason, mapped in (("cumulative_budget_exhausted", "budget_exhausted"),
                           ("abstract_root_budget_exhausted", "budget_exhausted"),
                           ("grant_revoked", "revoked"), ("capacity_full", "not_ready")):
        denied = fresh({**deny, "reason": reason})
        assert (denied.decision, denied.reason) == ("deny", mapped)
    assert fresh({**deny, **over_budget(97_000_000)}).reason == "not_ready"
