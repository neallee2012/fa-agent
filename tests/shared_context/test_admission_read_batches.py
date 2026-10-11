"""Admission's physical reads, classified without logging identifiers or values."""

from collections import Counter
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import sys

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_store import (
    BehaviorStore, ControlChanges, ControlRequest, ControlWrite, GUARD_KEY, InlineMaterial,
)
from shared_context.behavior_timing import phase_timing
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


def callers():
    frame = sys._getframe(1)
    result = []
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module.startswith("shared_context.") and module not in {
            "shared_context.behavior_store", "shared_context.behavior_migration",
        }:
            result.append((f"{module.rsplit('.', 1)[-1]}.{frame.f_code.co_name}", frame.f_lineno))
        frame = frame.f_back
    return result[:8]


def test_admission_physical_read_rounds_keep_the_original_request(synthetic_standing_v1, monkeypatch, caplog):
    c = synthetic_standing_v1
    rounds = []
    aliases = {
        c.control.policy_key: "standing-policy", c.control.revocation_key: "standing-revocation",
        c.control.cost_key: "cost-ledger", c.control.registry_key: "work-registry",
    }
    installation = c.h.runtime.store._installation_fence
    if installation is not None:
        aliases[installation[0]] = "installation"

    def category(store, key):
        if key == GUARD_KEY:
            return "guard"
        if key.startswith("behavior:v1:receipt:"):
            return "receipt"
        name = key.removeprefix("behavior:v1:row:")
        if name in aliases:
            return aliases[name]
        entity = c.h.table.backing.rows.get((store._scope.table_name, store._scope.partition_key, key))
        if entity is None:
            return "absent-row"
        data = json.loads(entity["Payload"]).get("data", {})
        kind = data.get("kind", data.get("schema_version"))
        if isinstance(kind, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,95}", kind):
            return kind
        return "row"

    def record(store, keys):
        counts = Counter(category(store, key) for key in keys)
        chain = callers()
        handler = "entry" if not chain else f"{chain[0][0]}:{chain[0][1]}"
        rounds.append({"round": len(rounds) + 1, "handler": handler, "callers": [name for name, _ in chain],
                       "table": store._scope.table_name, "keys": dict(sorted(counts.items())),
                       "gets": len(keys)})

    fetch = BehaviorStore._fetch_many
    physical = BehaviorStore._physical

    def fetched(store, keys):
        if len(keys) >= 2:
            record(store, [key for key, _ in keys])
        return fetch(store, keys)

    def read(store, key, model, pending):
        if key not in pending:
            record(store, [key])
        return physical(store, key, model, pending)

    monkeypatch.setattr(BehaviorStore, "_fetch_many", fetched)
    monkeypatch.setattr(BehaviorStore, "_physical", read)
    command = InstructionSubmit(request_id="admission-round-probe", content="Keep the original request unchanged.")
    with phase_timing("measured-admission"):
        binding, status = c.control.submit(c.human, command)
    assert status.state == "queued"
    assert c.control.records[binding.binding_id].profile.instruction.content == command.content
    measurement = next(json.loads(record.getMessage().removeprefix("FA_TIMING "))
                       for record in caplog.records
                       if record.getMessage().startswith('FA_TIMING {"phase":"measured-admission"'))
    assert sum(item["gets"] for item in rounds) == measurement["table_gets"]
    assert len(rounds) == measurement["batches"]
    report = {"physical_gets": measurement["table_gets"], "sequential_rounds": len(rounds), "rounds": rounds}
    destination = os.environ.get("ADMISSION_READ_TRACE")
    if destination:
        path = Path(destination)
        assert not path.exists(), "do not overwrite a previous measurement"
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = {}
    for item in rounds:
        totals = summary.setdefault(item["handler"], {"gets": 0, "rounds": 0})
        totals["gets"] += item["gets"]
        totals["rounds"] += 1
    print(json.dumps({"physical_gets": report["physical_gets"], "sequential_rounds": report["sequential_rounds"],
                      "handlers": summary}, sort_keys=True))
    assert any(
        "behavior_runtime.job_status" in item["callers"]
        and item["keys"] == {"delivery_obligation": 1, "worker_job": 1}
        for item in rounds
    ), "independent job and obligation reads still use separate guard-bracketed rounds"


@pytest.mark.parametrize("changed,code", [
    ("configuration", "WORKER_CONFIGURATION_MISMATCH"), ("owner", "WORKER_JOB_FENCED"),
])
def test_status_batch_keeps_worker_configuration_and_owner_checks(synthetic_standing_v1, changed, code):
    c = synthetic_standing_v1
    binding, _ = c.control.submit(c.human, InstructionSubmit(
        request_id="status-validation", content="Validate the original work."))
    key = worker_key("job", binding.job_id)
    prior = c.h.runtime.store.read_snapshot((key,))[key]
    data = json.loads(json.dumps(prior.payload["data"]))
    if changed == "configuration":
        data["configuration_digest"] = "f" * 64
    else:
        data["owner"]["conversation_id"] = "another-conversation"
    c.h.runtime.store.transact(
        ControlRequest(request_id="synthetic-status-change", request_digest=digest(data), read_set=(key,)),
        lambda _: ControlChanges(writes=(ControlWrite(
            row_id=key, mode="replace", data=data,
            material=None if prior.payload["material"] is None else
            InlineMaterial.model_validate_json(json.dumps(prior.payload["material"])),
        ),), result={}),
    )
    with pytest.raises(Problem) as denied:
        c.h.runtime.job_status(binding.binding_id, c.human)
    assert denied.value.body["error"]["code"] == code


def test_status_batch_still_authenticates_the_original_reader(synthetic_standing_v1):
    c = synthetic_standing_v1
    binding, _ = c.control.submit(c.human, InstructionSubmit(
        request_id="status-reader", content="Keep this work with its original reader."))
    other = replace(c.human, oid="99999999-9999-4999-8999-999999999999")
    with pytest.raises(Problem) as denied:
        c.h.runtime.job_status(binding.binding_id, other)
    assert denied.value.status == 403
    assert denied.value.body["error"]["code"] == "BEHAVIOR_BINDING_DENIED"
