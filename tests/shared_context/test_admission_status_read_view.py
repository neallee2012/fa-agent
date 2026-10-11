"""Only admission's final status read may share a view; every write stays outside."""

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_operations import control_key
from shared_context.behavior_store import GUARD_KEY, _READ_VIEW
from shared_context.behavior_timing import phase_timing
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from test_behavior_store import NativeEntity
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


def test_admission_status_is_pure_after_all_writes(synthetic_standing_v1, monkeypatch, caplog):
    c = synthetic_standing_v1
    observed = []
    writes = []
    status = c.h.runtime.job_status
    submit = c.h.runtime.store._submit

    def submitted(*args, **kwargs):
        assert _READ_VIEW.get() is None
        writes.append(True)
        return submit(*args, **kwargs)

    def read_status(binding_id, principal):
        observed.append(_READ_VIEW.get() is not None)
        previous_writes = len(writes)
        result = status(binding_id, principal)
        assert len(writes) == previous_writes
        return result

    monkeypatch.setattr(c.h.runtime.store, "_submit", submitted)
    monkeypatch.setattr(c.h.runtime, "job_status", read_status)
    command = InstructionSubmit(request_id="status-view-admission", content="Keep this original request.")
    with phase_timing("admission-status-view"):
        binding, result = c.control.submit(c.human, command)
    assert result.state == "queued" and result.job_id == binding.job_id
    assert c.control.records[binding.binding_id].profile.instruction.content == command.content
    assert writes and observed == [True]
    assert _READ_VIEW.get() is None
    value = next(json.loads(record.getMessage().removeprefix("FA_TIMING "))
                 for record in caplog.records
                 if record.getMessage().startswith('FA_TIMING {"phase":"admission-status-view"'))
    print(json.dumps({key: value[key] for key in ("table_gets", "batches")}))


@pytest.mark.parametrize("change,code", [
    ("owner-revoked", "BEHAVIOR_CONFLICT"),
    ("guard-binding", "BEHAVIOR_AUTHORITY_MISMATCH"),
    ("installation", "BEHAVIOR_CONFLICT"),
])
def test_final_status_rejects_changed_authority_without_undoing_admission(
    synthetic_standing_v1, monkeypatch, change, code,
):
    c = synthetic_standing_v1
    original = c.h.runtime.job_status
    observed = []

    def status(binding_id, principal):
        value = original(binding_id, principal)
        binding = c.h.runtime.bindings[binding_id]
        store = c.h.runtime.store
        job_key = (store._scope.table_name, store._scope.partition_key,
                   "behavior:v1:row:" + worker_key("job", binding.job_id))
        job_bytes = c.h.table.backing.rows[job_key]["Payload"]

        def change_authority():
            assert _READ_VIEW.get() is None
            if change == "owner-revoked":
                c.h.mutate(control_key("owner", binding.owner.owner_record_id), active=False)
            else:
                key = (GUARD_KEY if change == "guard-binding" else
                       "behavior:v1:row:" + store._installation_fence[0])
                physical = (store._scope.table_name, store._scope.partition_key, key)
                entity = c.h.table.backing.rows[physical]
                payload = json.loads(entity["Payload"])
                if change == "guard-binding":
                    payload["binding"] = "f" * 64
                else:
                    payload["data"]["configuration_sha256"] = "f" * 64
                c.h.table.backing.rows[physical] = NativeEntity({
                    **entity, "Payload": json.dumps(payload, separators=(",", ":")).encode(),
                }, 'W/"synthetic-late-change"')

        with ThreadPoolExecutor(max_workers=1) as writer:
            writer.submit(change_authority).result()
        observed.append((job_key, job_bytes))
        return value

    monkeypatch.setattr(c.h.runtime, "job_status", status)
    with pytest.raises(Problem) as denied:
        c.control.submit(c.human, InstructionSubmit(
            request_id="late-status-change", content="Retain the original admitted work."))
    assert denied.value.body["error"]["code"] == code
    assert len(observed) == 1
    job_key, job_bytes = observed[0]
    assert c.h.table.backing.rows[job_key]["Payload"] == job_bytes
    assert json.loads(job_bytes)["data"]["state"] == "queued"
    assert _READ_VIEW.get() is None


def test_status_view_is_not_a_grant_for_the_next_request(synthetic_standing_v1):
    c = synthetic_standing_v1
    binding, result = c.control.submit(c.human, InstructionSubmit(
        request_id="status-before-revoke", content="Only this original admission."))
    assert result.state == "queued"
    revoked = c.control.revoke(c.human, "revoke-after-status")
    assert revoked.payload["result"]["revoked"] is True
    with pytest.raises(Problem) as denied:
        c.h.runtime.job_status(binding.binding_id, c.human)
    assert denied.value.status == 403
    assert _READ_VIEW.get() is None
