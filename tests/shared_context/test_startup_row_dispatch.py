"""Startup classifies each late row the same way however it finds the row's model.

Synthetic native startup fixture with local Table I/O; no SDK process, model or Azure call runs.
"""

from types import SimpleNamespace

import pytest

from shared_context import behavior_runtime_rotation_history as history
from shared_context.behavior_instruction_cost import (
    ModelCost, cost_key, record_agent_turn_usage, record_turn_usage, turn_usage_key,
)
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from shared_context.source_materials import canonical
from test_conversation_transfer_startup import read_startup, saved_turn, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_turn_usage_ledger import group, report


@pytest.fixture
def usage_startup(synthetic_standing_v1):
    c = synthetic_standing_v1
    c.job_id = saved_turn(c, "dispatch-turn").binding.job_id
    record_turn_usage(c.control, job_id=c.job_id, rid="dispatch-usage", usage=report(group()))
    record_agent_turn_usage(c.control, rid="dispatch-native", usage=report(group()))
    return c


def late_row(c, row_id, data):
    c.h.runtime.store.transact(ControlRequest(request_id=("late-" + row_id)[:64], request_digest="a" * 64),
                               lambda _: ControlChanges(writes=(ControlWrite(row_id=row_id, mode="create",
                                                                             data=data),), result={}))


def rejected(c):
    with pytest.raises(Problem) as denied:
        read_startup(c)
    assert denied.value.status == 409
    assert denied.value.body["error"]["code"] == "ROTATION_HISTORY_INVALID"
    return denied.value.body["error"]["message"]


def test_rows_of_every_late_kind_and_rows_without_kind_are_accepted(usage_startup):
    inventory = read_startup(usage_startup)
    kinds = {row.payload["data"].get("kind") for row in inventory.rows.values()}
    # Two of the usage kinds missing from _kind_models, and kind-less rows, all pass the late-row loop.
    assert {"instruction_turn_usage", "agent_turn_usage", None} <= kinds


def test_a_valid_model_cost_row_is_accepted(usage_startup):
    c = usage_startup
    cost = ModelCost(
        session_id="dispatch-session", job_id=c.job_id, request_sha256="f" * 64, model="gpt-4.1-mini",
        input_microusd_per_million=1_000_000, output_microusd_per_million=1_000_000, request_token_estimate=10,
        input_token_upper_bound=10, output_token_upper_bound=10, pending_microusd=750_000,
    )
    key = cost_key(cost.session_id, cost.request_sha256)
    late_row(c, key, cost.model_dump(mode="json"))
    # ModelCost is not in _kind_models, so only the late-row loop's fallback models can accept this row.
    assert read_startup(c).rows[key].payload["data"]["kind"] == "instruction_model_cost"


@pytest.mark.parametrize("without_kind", [False, True], ids=["as-written", "without-kind"])
def test_a_typed_usage_row_under_another_key_keeps_its_own_message(usage_startup, without_kind):
    c = usage_startup
    data = read_startup(c).rows[turn_usage_key(c.job_id, "dispatch-usage")].payload["data"]
    # The kind has a default, so a row without it still parses as the usage record and reaches its key check.
    late_row(c, "moved-turn-usage", {k: v for k, v in data.items() if not (without_kind and k == "kind")})
    assert rejected(c) == "instruction turn usage escaped its original work"


@pytest.mark.parametrize("change", [
    pytest.param(lambda data: {k: v for k, v in data.items() if k != "metered_microusd"}, id="usage-missing-field"),
    pytest.param(lambda data: {**data, "unexpected": 1}, id="usage-extra-field"),
    pytest.param(lambda data: {**data, "kind": "agent_turn_usage"}, id="usage-other-kind"),
    pytest.param(lambda data: {**data, "kind": "instruction_model_cost"}, id="usage-cost-kind"),
    pytest.param(lambda data: {**data, "kind": "not_a_known_kind"}, id="unknown-kind"),
    pytest.param(lambda data: {"unexpected": 1}, id="untyped-row"),
])
def test_an_invalid_or_unknown_late_row_is_refused_with_the_original_message(usage_startup, change):
    c = usage_startup
    data = read_startup(c).rows[turn_usage_key(c.job_id, "dispatch-usage")].payload["data"]
    late_row(c, "late-invalid-row", change(data))
    assert rejected(c) == "unclassified late current-runtime row: late-invalid-row"


def test_every_model_the_late_row_loop_skips_would_have_refused_that_row(usage_startup, monkeypatch):
    c = usage_startup
    shape, seen = history._row_shape, set()
    monkeypatch.setattr(history, "_row_shape", lambda model: seen.add(model) or shape(model))
    inventory = read_startup(c)
    assert len(seen) >= 51
    kinds = sorted({kind for model in seen if shape(model) and shape(model)[2] for kind in shape(model)[2]})
    for row in inventory.rows.values():
        data = row.payload["data"]
        variants = (data, {**data, "unexpected": 1}, {k: v for k, v in data.items() if k != "kind"},
                    dict(list(data.items())[1:]), *({**data, "kind": kind} for kind in kinds))
        for variant in variants:
            for model in seen:
                if history._refused(shape(model), variant):
                    with pytest.raises(ValueError):
                        model.model_validate_json(canonical(variant))


@pytest.mark.parametrize("decorators", [object(), SimpleNamespace(validators={}, root_validators={},
                                                                  model_validators=[], field_validators={})],
                         ids=["missing", "other-shape"])
def test_an_unreadable_validator_list_falls_back_to_trying_the_model(monkeypatch, decorators):
    monkeypatch.setattr(ModelCost, "__pydantic_decorators__", decorators)
    assert history._row_shape(ModelCost) is None
    assert history._refused(history._row_shape(ModelCost), {"unexpected": 1}) is False
