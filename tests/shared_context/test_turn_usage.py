"""The per-turn model usage DTO the Hosted end receipt carries (ADR-0006 §5 A2). Pure wire tests."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from shared_context.behavior_turn_usage import MAX_USAGE_GROUPS, TURN_USAGE_SCHEMA, ModelUsage, TurnModelUsage

SRC = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"


def usage(**values):
    base = {"model": "gpt-5.4-mini", "wire_api": "responses", "requests": 3, "measured_requests": 3,
            "input_tokens": 1200, "output_tokens": 300, "reasoning_tokens": 120}
    return ModelUsage(**{**base, **values})


def turn(*groups):
    return TurnModelUsage(schema_version=TURN_USAGE_SCHEMA, usage=groups)


def test_complete_partial_and_none_states():
    assert turn().state == "none"
    assert turn(usage()).state == "complete"
    partial = turn(usage(requests=4))
    assert partial.state == "partial" and partial.usage[0].input_tokens == 1200
    unknown = turn(usage(requests=2, measured_requests=0, input_tokens=None,
                         output_tokens=None, reasoning_tokens=None))
    assert unknown.state == "partial" and unknown.usage[0].input_tokens is None


@pytest.mark.parametrize("values", [
    # A measured request needs both counts; an unmeasured group has none (never a filled-in 0).
    {"input_tokens": None},
    {"output_tokens": None},
    {"measured_requests": 0},
    {"measured_requests": 0, "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": None},
    {"measured_requests": 4},
    {"requests": 0, "measured_requests": 0, "input_tokens": None, "output_tokens": None, "reasoning_tokens": None},
    {"reasoning_tokens": 301},
    {"wire_api": "unknown-protocol", "reasoning_tokens": None},
    {"input_tokens": -1},
    {"model": "../other"},
    {"input_tokens": True},
])
def test_inconsistent_or_inferred_counts_are_refused(values):
    with pytest.raises(ValidationError):
        usage(**values)


def test_groups_are_distinct_bounded_and_strict():
    with pytest.raises(ValidationError):
        turn(usage(), usage())
    with pytest.raises(ValidationError):
        turn(*(usage(model=f"m{i}") for i in range(MAX_USAGE_GROUPS + 1)))
    # A report missing its fields never reads as "no request was sent".
    for missing in ({}, {"schema_version": TURN_USAGE_SCHEMA}, {"usage": []}):
        with pytest.raises(ValidationError):
            TurnModelUsage.model_validate_json(json.dumps(missing))
    with pytest.raises(ValidationError):
        TurnModelUsage.model_validate_json(json.dumps({"schema_version": "fa-turn-model-usage-v1",
                                                       "usage": [], "text": "model output"}))
    two = turn(usage(), usage(model="gpt-5.4", reasoning_tokens=None))
    assert TurnModelUsage.model_validate_json(two.model_dump_json()) == two


def test_native_direct_completions_uses_the_same_usage_contract():
    direct = turn(usage(wire_api="completions", reasoning_tokens=None))
    assert direct.state == "complete" and direct.usage[0].wire_api == "completions"


def test_the_dto_imports_without_api_or_sdk_modules():
    code = ("import sys; import shared_context.behavior_turn_usage; "
            "bad=[m for m in ('fastapi','copilot','shared_context.behavior_assistant','shared_context.m2_copilot') "
            "if m in sys.modules]; print(bad)")
    result = subprocess.run([sys.executable, "-c", code], cwd=SRC, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and result.stdout.strip() == "[]", result.stderr
