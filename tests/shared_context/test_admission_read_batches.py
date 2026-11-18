"""GET /jobs/{job_id} over stored old instruction work keeps its reader, configuration and owner checks.

The old work is the state the retired WorkPolicyControl.submit left on main 80ec0865, replayed (legacy_replay_helpers).
"""

from dataclasses import replace
import json

import pytest

from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite, InlineMaterial
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_conversation_transfer_startup import synthetic_standing_v1
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from legacy_replay_helpers import legacy_tokens, pinned_configuration, replayed


def stored_work(c):
    binding = replayed(c.h)  # InstructionSubmit "status-validation" / "status-reader" as the retired submit queued it
    c.control.refresh(binding.binding_id)
    assert c.h.runtime.job_status(binding.binding_id, c.human).state == "queued"
    return binding


@pytest.mark.parametrize("changed,code", [
    ("configuration", "WORKER_CONFIGURATION_MISMATCH"), ("owner", "WORKER_JOB_FENCED"),
])
def test_status_batch_keeps_worker_configuration_and_owner_checks(
        legacy_tokens, pinned_configuration, synthetic_standing_v1, changed, code):
    c = synthetic_standing_v1
    binding = stored_work(c)
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


def test_status_batch_still_authenticates_the_original_reader(
        legacy_tokens, pinned_configuration, synthetic_standing_v1):
    c = synthetic_standing_v1
    binding = stored_work(c)
    other = replace(c.human, oid="99999999-9999-4999-8999-999999999999")
    with pytest.raises(Problem) as denied:
        c.h.runtime.job_status(binding.binding_id, other)
    assert denied.value.status == 403
    assert denied.value.body["error"]["code"] == "BEHAVIOR_BINDING_DENIED"
