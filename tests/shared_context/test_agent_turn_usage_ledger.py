"""ADR-0007 native turns: usage is written once per (Agent, RID) into the original CostLedger.

No Table job or WorkRegistration exists for these turns. Synthetic native startup fixture with local Table
I/O; no SDK process, model or Azure call runs.
"""

from types import SimpleNamespace

import pytest

from shared_context.behavior_instruction_cost import (
    AgentTurnUsageRecord, agent_turn_usage_key, price, record_agent_turn_usage, record_turn_usage, turn_usage_key,
)
from shared_context.behavior_operations import _row
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_conversation_transfer_startup import claimed_turn, read_startup, synthetic_standing_v1
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_turn_usage_ledger import group, ledger, report

METERED = price(1000, 200, 400000, 1600000)


def saved(c, rid):
    key = agent_turn_usage_key(c.control.policy.template.agent_oid, rid)
    return _row(c.h.runtime.store.read_snapshot((key,)), key, AgentTurnUsageRecord)


def test_native_turn_usage_is_metered_once_per_agent_and_rid_and_replays(synthetic_standing_v1):
    c = synthetic_standing_v1
    before = ledger(c)
    usage = report(group())
    first = record_agent_turn_usage(c.control, rid="native-turn", usage=usage)
    assert first.payload["result"] == {"usage_recorded": True, "usage_known": True,
                                       "metered_microusd": METERED, "unmetered_requests": 1}
    after = ledger(c)
    assert after.model_metered_microusd == before.model_metered_microusd + METERED
    assert after.model_unmetered_requests == before.model_unmetered_requests + 1
    assert after.model_unknown_turns == before.model_unknown_turns

    # The same final report replays its original receipt; the ledger moves only once.
    again = record_agent_turn_usage(c.control, rid="native-turn", usage=usage)
    assert again.payload == first.payload and ledger(c) == after
    with pytest.raises(Problem) as conflict:
        record_agent_turn_usage(c.control, rid="native-turn", usage=report(group(requests=4)))
    assert conflict.value.status == 409 and ledger(c) == after

    record = saved(c, "native-turn")
    assert (record.agent_oid, record.rid, record.source, record.usage) == (
        c.control.policy.template.agent_oid, "native-turn", "model", usage)
    read_startup(c)


def test_an_unknown_native_turn_is_counted_never_zero_and_stays_final(synthetic_standing_v1):
    c = synthetic_standing_v1
    before = ledger(c)
    result = record_agent_turn_usage(c.control, rid="native-unknown", usage=None).payload["result"]
    assert result == {"usage_recorded": True, "usage_known": False, "metered_microusd": 0, "unmetered_requests": 0}
    after = ledger(c)
    assert after.model_unknown_turns == before.model_unknown_turns + 1
    assert (after.model_metered_microusd, after.model_unmetered_requests) == (
        before.model_metered_microusd, before.model_unmetered_requests)
    assert saved(c, "native-unknown").usage is None and saved(c, "native-unknown").prices == ()
    assert record_agent_turn_usage(c.control, rid="native-unknown", usage=None).payload["result"] == result
    # A final unknown is not replaced by a later different report.
    with pytest.raises(Problem) as conflict:
        record_agent_turn_usage(c.control, rid="native-unknown", usage=report(group()))
    assert conflict.value.status == 409 and ledger(c) == after
    # Partial and unpriced requests keep the existing per-request rule.
    partial = record_agent_turn_usage(c.control, rid="native-partial", usage=report(
        group(requests=2, measured=0), group(model="unlisted-deployment", requests=1, measured=1))).payload["result"]
    assert partial == {"usage_recorded": True, "usage_known": True, "metered_microusd": 0, "unmetered_requests": 3}
    read_startup(c)


def test_native_and_job_usage_with_the_same_rid_stay_separate_and_job_path_is_unchanged(synthetic_standing_v1):
    c = synthetic_standing_v1
    job_id = claimed_turn(c, "shared-rid").binding.job_id
    job = record_turn_usage(c.control, job_id=job_id, rid="shared-rid", usage=report(group()))
    native = record_agent_turn_usage(c.control, rid="shared-rid", usage=report(group(requests=5, measured=5)))
    assert job.payload["result"] == {"usage_recorded": True, "metered_microusd": METERED, "unmetered_requests": 1}
    assert native.payload["result"]["usage_known"] is True
    assert turn_usage_key(job_id, "shared-rid") != agent_turn_usage_key(c.control.policy.template.agent_oid,
                                                                        "shared-rid")
    read_startup(c)


def test_a_native_turn_finishing_after_revocation_still_records_and_starts(synthetic_standing_v1):
    c = synthetic_standing_v1
    c.control.revoke(c.human, "revoke-while-native-turn-runs")
    before = ledger(c)
    record_agent_turn_usage(c.control, rid="native-revoked", usage=report(group()))
    assert ledger(c).model_metered_microusd == before.model_metered_microusd + METERED
    read_startup(c)


def test_an_unreadable_catalog_writes_nothing_and_the_same_report_records_later(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    before = ledger(c)

    class Unreadable:
        def select(self, name):
            raise Problem(503, "INSTRUCTION_MODEL_CONFIGURATION", "Synthetic unreadable catalog")
    original = type(c.control).models
    monkeypatch.setattr(type(c.control), "models", property(lambda self: Unreadable()))
    with pytest.raises(Problem) as unreadable:
        record_agent_turn_usage(c.control, rid="native-catalog", usage=report(group()))
    assert unreadable.value.status == 503 and ledger(c) == before
    monkeypatch.setattr(type(c.control), "models", original)
    assert record_agent_turn_usage(c.control, rid="native-catalog",
                                   usage=report(group())).payload["result"]["metered_microusd"] == METERED
    read_startup(c)


def test_only_a_typed_report_or_explicit_unknown_is_accepted(synthetic_standing_v1):
    c = synthetic_standing_v1
    with pytest.raises(Problem) as invalid:
        record_agent_turn_usage(c.control, rid="native-typed", usage={"usage": []})
    assert invalid.value.body["error"]["code"] == "AGENT_USAGE_INVALID"


def test_startup_refuses_usage_of_an_agent_without_an_original_policy(synthetic_standing_v1):
    c = synthetic_standing_v1
    control = c.control
    other = control.policy.model_copy(update={"template": control.policy.template.model_copy(
        update={"agent_oid": "not-a-registered-agent"})})
    stray = SimpleNamespace(policy=other, cost_key=control.cost_key, store=control.store, models=control.models)
    record_agent_turn_usage(stray, rid="native-stray", usage=report(group()))
    with pytest.raises(Problem) as refused:
        read_startup(c)
    assert refused.value.status == 409
    assert refused.value.body["error"]["message"] == "agent turn usage has no original Agent policy"


def test_the_verified_agent_reports_its_own_final_turn_usage_once_over_the_api(assistant):
    from shared_context.behavior_turn_usage import AGENT_TURN_USAGE_PATH, AgentTurnUsageReport
    from test_sdk_compaction_product import configured
    h, control, human = configured(assistant)
    body = AgentTurnUsageReport(request_id="vm-turn", usage=report(group())).model_dump(mode="json")
    first = h.client.post(AGENT_TURN_USAGE_PATH, json=body, headers=h.headers("agent"))
    assert first.status_code == 200, first.text
    expected = {"request_id": "vm-turn", "usage_recorded": True, "usage_known": True,
                "metered_microusd": METERED, "unmetered_requests": 1}
    assert first.json() == expected
    after = ledger(SimpleNamespace(h=h, control=control))
    # The VM resending, or N2 reconciling the same final report in the API, returns the same original result.
    assert h.client.post(AGENT_TURN_USAGE_PATH, json=body, headers=h.headers("agent")).json() == expected
    reconciled = record_agent_turn_usage(control, rid="vm-turn", usage=report(group())).payload["result"]
    assert {"request_id": "vm-turn", **reconciled} == expected
    assert ledger(SimpleNamespace(h=h, control=control)) == after
    different = dict(body, usage=report(group(requests=4)).model_dump(mode="json"))
    assert h.client.post(AGENT_TURN_USAGE_PATH, json=different, headers=h.headers("agent")).status_code == 409
    # Only the policy's own verified Agent may report; the body never names an Agent.
    denied = h.client.post(AGENT_TURN_USAGE_PATH, json=dict(body, request_id="human-turn"), headers=h.headers())
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "AGENT_USAGE_SCOPE"
    named = h.client.post(AGENT_TURN_USAGE_PATH, json=dict(body, agent_oid=control.policy.template.agent_oid),
                          headers=h.headers("agent"))
    assert named.status_code == 422
    # Unknown is explicit; a missing usage field is not read as "no request was sent".
    unknown = h.client.post(AGENT_TURN_USAGE_PATH, json={"request_id": "vm-unknown", "usage": None},
                            headers=h.headers("agent"))
    assert unknown.status_code == 200 and unknown.json()["usage_known"] is False
    missing = h.client.post(AGENT_TURN_USAGE_PATH, json={"request_id": "vm-missing"}, headers=h.headers("agent"))
    assert missing.status_code == 422
    assert ledger(SimpleNamespace(h=h, control=control)).model_unknown_turns == after.model_unknown_turns + 1


def test_lost_cas_races_on_the_shared_cost_row_pause_with_jitter_before_retrying(
        synthetic_standing_v1, monkeypatch, caplog):
    import shared_context.behavior_instruction_cost as cost
    from shared_context.behavior_timing import timing_id
    c = synthetic_standing_v1
    original, races, pauses = c.control.store.transact, [2], []

    def racing(request, decide):
        if races[0]:
            races[0] -= 1
            raise Problem(412, "BEHAVIOR_CONFLICT", "synthetic lost ETag race")
        return original(request, decide)

    monkeypatch.setattr(c.control.store, "transact", racing)
    monkeypatch.setattr(cost.time, "sleep", pauses.append)
    before = ledger(c)
    record_agent_turn_usage(c.control, rid="native-race", usage=None)
    assert ledger(c).model_unknown_turns == before.model_unknown_turns + 1
    assert len(pauses) == 2 and 0.02 <= pauses[0] <= 0.1 and 0.04 <= pauses[1] <= 0.2
    # Each lost race names its turn by the 12-character prefix only, never the RID itself.
    conflicts = [r.getMessage() for r in caplog.records if "AGENT_USAGE_LEDGER_CONFLICT" in r.getMessage()]
    assert len(conflicts) == 2 and all(m.endswith(" rid=" + timing_id("native-race")) for m in conflicts)
    assert "native-race" not in caplog.text
