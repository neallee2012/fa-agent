"""Client-only schema checks; no server authority comes from a wire DTO."""

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from shared_context import behavior_wire as wire


def test_wire_has_only_standard_library_and_pydantic_imports():
    tree = ast.parse(Path(wire.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module in {"__future__", "datetime", "typing", "pydantic"}
        elif isinstance(node, ast.Import):
            assert all(name.name.split(".")[0] in {"json", "base64", "hashlib"} for name in node.names)


@pytest.mark.parametrize("authority", ["actor", "approved", "context", "grant_id", "owner", "root_budget_id", "clock", "adapter"])
def test_wire_commands_refuse_authority_fields(authority):
    with pytest.raises(ValidationError):
        wire.Command(request_id="original", **{authority: True})


def test_executor_payload_is_bounded_business_data_not_arbitrary_code():
    body = wire.ExecutorJobInput(job_id="job", work_id="work", definition_digest="a" * 64, required_checks=("complete",))
    assert wire.ExecutorJobInput.model_validate_json(body.model_dump_json()) == body
    for field in ("shell", "url", "python", "token", "environment", "execution_context"):
        with pytest.raises(ValidationError):
            wire.ExecutorJobInput(**body.model_dump(), **{field: "untrusted"})
    with pytest.raises(ValidationError):
        wire.ExecutorJobInput(**{**body.model_dump(), "result_text": "SYNTHETIC_SECRET"})


def test_source_bytes_round_trip_without_reserialization():
    raw = b'{  "schema_version": "fa-artifact-metrics-v1", "item_count": 0, "completed_count":0 }\r\n'
    body = wire.SourcePrepare(request_id="original", version=1, original=raw)
    assert wire.SourcePrepare.model_validate_json(body.model_dump_json()).original == raw
    for bad in (True, 0, -1):
        with pytest.raises(ValidationError):
            wire.SourcePrepare.model_validate_json(json.dumps({"request_id": "x", "version": bad, "original": "eA=="}))


def test_approval_expiry_cannot_supply_a_naive_clock_or_larger_spend():
    fields = {
        "request_id": "approval", "decision_id": "human-decision",
        "description_digest": "a" * 64, "adapter_contract_digest": "b" * 64,
        "expires_at": "2026-09-15T20:00:00Z",
    }
    assert wire.OperationApproval.model_validate_json(json.dumps(fields)).uses == 1
    for updates in ({"expires_at": "2026-09-15T20:00:00"}, {"uses": 2}, {"units": 2}, {"approved": True}):
        with pytest.raises(ValidationError):
            wire.OperationApproval.model_validate_json(json.dumps({**fields, **updates}))
