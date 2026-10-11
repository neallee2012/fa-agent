"""ADR-0006 §5 A2: a turn's reported model usage is written once, before its completion is consumed.

Synthetic native startup fixture with local Table I/O; no SDK process, model or Azure call runs.
"""

import pytest

from shared_context.behavior_instruction_cost import (
    TurnUsageRecord, price, record_turn_usage, turn_usage_key,
)
from shared_context.behavior_operations import _row
from shared_context.behavior_turn_usage import TURN_USAGE_SCHEMA, ModelUsage, TurnModelUsage
from shared_context.behavior_work_policy import CostLedger
from shared_context.errors import Problem
from test_conversation_transfer_startup import claimed_turn, read_startup, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


def report(*groups):
    return TurnModelUsage(schema_version=TURN_USAGE_SCHEMA, usage=groups)


def group(model="gpt-4.1-mini", requests=3, measured=2, input_tokens=1000, output_tokens=200):
    return ModelUsage(model=model, wire_api="responses", requests=requests, measured_requests=measured,
                      input_tokens=input_tokens if measured else None,
                      output_tokens=output_tokens if measured else None)


def ledger(c):
    rows = c.h.runtime.store.read_snapshot((c.control.cost_key,))
    return _row(rows, c.control.cost_key, CostLedger)


def test_turn_usage_is_metered_once_and_replays_through_startup(synthetic_standing_v1):
    c = synthetic_standing_v1
    job_id = claimed_turn(c, "usage-turn").binding.job_id
    read_startup(c)
    before = ledger(c)
    usage = report(group())
    first = record_turn_usage(c.control, job_id=job_id, rid="usage-turn", usage=usage)
    metered = price(1000, 200, 400000, 1600000)
    assert first.payload["result"] == {"usage_recorded": True, "metered_microusd": metered, "unmetered_requests": 1}
    after = ledger(c)
    assert after.model_metered_microusd == before.model_metered_microusd + metered
    assert after.model_unmetered_requests == before.model_unmetered_requests + 1
    assert after.model_pending_microusd == before.model_pending_microusd

    # The same report replays its original receipt; the ledger moves only once.
    again = record_turn_usage(c.control, job_id=job_id, rid="usage-turn", usage=usage)
    assert again.payload["result"] == first.payload["result"] and ledger(c) == after
    # A different report for the same turn is refused rather than overwriting the first.
    with pytest.raises(Problem) as conflict:
        record_turn_usage(c.control, job_id=job_id, rid="usage-turn", usage=report(group(requests=4)))
    assert conflict.value.status == 409 and ledger(c) == after

    saved = c.h.runtime.store.read_snapshot((turn_usage_key(job_id, "usage-turn"),))
    record = _row(saved, turn_usage_key(job_id, "usage-turn"), TurnUsageRecord)
    assert record.usage == usage and record.prices[0].input_microusd_per_million == 400000
    # Startup replays the exact ledger change of this new writer.
    read_startup(c)


def test_unknown_or_unpriced_requests_are_counted_never_given_an_amount(synthetic_standing_v1):
    c = synthetic_standing_v1
    job_id = claimed_turn(c, "usage-unknown").binding.job_id
    before = ledger(c)
    # Nothing measured, and a measured deployment without a configured rate.
    result = record_turn_usage(c.control, job_id=job_id, rid="usage-unknown", usage=report(
        group(requests=2, measured=0), group(model="unlisted-deployment", requests=1, measured=1))).payload["result"]
    assert result == {"usage_recorded": True, "metered_microusd": 0, "unmetered_requests": 3}
    after = ledger(c)
    assert after.model_metered_microusd == before.model_metered_microusd
    assert after.model_unmetered_requests == before.model_unmetered_requests + 3
    # A turn that sent no model request still writes its (empty) report once.
    none_job = claimed_turn(c, "usage-none").binding.job_id
    assert record_turn_usage(c.control, job_id=none_job, rid="usage-none", usage=report()).payload["result"] == {
        "usage_recorded": True, "metered_microusd": 0, "unmetered_requests": 0}
    read_startup(c)


def test_the_same_request_id_in_another_work_is_a_separate_record(synthetic_standing_v1):
    c = synthetic_standing_v1
    first = claimed_turn(c, "usage-a").binding.job_id
    second = claimed_turn(c, "usage-b").binding.job_id
    record_turn_usage(c.control, job_id=first, rid="r1", usage=report(group()))
    # Same caller request ID, another original work: its own key, not a conflict or a replay.
    record_turn_usage(c.control, job_id=second, rid="r1", usage=report(group(requests=5, measured=5)))
    assert turn_usage_key(first, "r1") != turn_usage_key(second, "r1")
    read_startup(c)


def test_a_turn_running_when_the_policy_is_revoked_still_records_usage_and_starts(synthetic_standing_v1):
    # The VM no longer asks the API before each model call, so a turn can run past a revocation.
    c = synthetic_standing_v1
    job_id = claimed_turn(c, "usage-revoked").binding.job_id
    c.control.revoke(c.human, "revoke-while-turn-runs")
    before = ledger(c)
    record_turn_usage(c.control, job_id=job_id, rid="usage-revoked", usage=report(group()))
    assert ledger(c).model_metered_microusd == before.model_metered_microusd + price(1000, 200, 400000, 1600000)
    # Spend that already happened is recorded; it is not a new admission or dispatch after revocation.
    read_startup(c)


def test_an_unreadable_catalog_is_not_recorded_as_unpriced(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    job_id = claimed_turn(c, "usage-catalog").binding.job_id
    before = ledger(c)

    class Unreadable:
        def select(self, name):
            raise Problem(503, "INSTRUCTION_MODEL_CONFIGURATION", "Synthetic unreadable catalog")
    original = type(c.control).models
    monkeypatch.setattr(type(c.control), "models", property(lambda self: Unreadable()))
    with pytest.raises(Problem) as unreadable:
        record_turn_usage(c.control, job_id=job_id, rid="usage-catalog", usage=report(group()))
    assert unreadable.value.status == 503 and ledger(c) == before
    # Once the catalog reads again, the same report is recorded with its rates; nothing was written before.
    monkeypatch.setattr(type(c.control), "models", original)
    result = record_turn_usage(c.control, job_id=job_id, rid="usage-catalog", usage=report(group())).payload["result"]
    assert result["metered_microusd"] == price(1000, 200, 400000, 1600000)
    read_startup(c)


def test_a_request_usage_recorded_after_revocation_still_starts(synthetic_standing_v1):
    # The existing per-request path: dispatched before the revocation, its usage written after it.
    from shared_context import behavior_assistant_wire as aw
    from shared_context.behavior_instruction_cost import record_usage
    from shared_context.source_materials import digest
    c = synthetic_standing_v1
    opened = claimed_turn(c, "request-usage-revoked")
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "Synthetic request."}], "stream": True}
    request_sha256 = digest(body)
    c.authority.reserve_model(opened.session, aw.Reservation(request_id=request_sha256, request_sha256=request_sha256),
                              opened.agent)
    c.authority.consume_model(opened.session, aw.ModelRequest(request_id=request_sha256, body=body), opened.agent)
    c.control.revoke(c.human, "revoke-while-stream-runs")
    record_usage(c.authority, opened.session, request_sha256, (37, 11))
    read_startup(c)


def test_only_a_registered_instruction_work_or_a_typed_report_is_accepted(synthetic_standing_v1):
    c = synthetic_standing_v1
    with pytest.raises(Problem) as scope:
        record_turn_usage(c.control, job_id="not-a-registered-job", rid="r1", usage=report(group()))
    assert scope.value.body["error"]["code"] == "INSTRUCTION_USAGE_SCOPE"
    job_id = claimed_turn(c, "usage-typed").binding.job_id
    with pytest.raises(Problem) as invalid:
        record_turn_usage(c.control, job_id=job_id, rid="usage-typed", usage={"usage": []})
    assert invalid.value.body["error"]["code"] == "INSTRUCTION_USAGE_INVALID"
