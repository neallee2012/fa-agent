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
from test_conversation_transfer_startup import synthetic_standing_v1
from legacy_turn_rows_helpers import read_startup
from legacy_turn_rows_helpers import stored_turn
from instruction_flow_helpers import instruction_models
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


def test_turn_usage_is_metered_once_and_replays_through_startup(stored_turn):
    c = stored_turn("usage-turn")
    job_id = c.turn.binding.job_id
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

