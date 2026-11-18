"""Original launch rejection over local authority/HTTP doubles; no SDK or cloud."""

import socket
import subprocess

import pytest

from shared_context.behavior_assistant import key as assistant_key
from shared_context.behavior_foundry import native_key
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from test_behavior_delivery import CONTEXT
from test_behavior_foundry import NativeHarness
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def unclaimed(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("no process or real network may start")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    h = NativeHarness()
    h.hold_start = True
    job_id = h.specs[0].job_id
    receipt = h.workers.start(job_id, CONTEXT)
    binding = h.workers.inspect(job_id).binding
    launch = h.native.launch(native_key("launch-id", binding.start_request_id))
    try:
        yield h, job_id, receipt, binding, launch
    finally:
        h.close()


def test_retained_assignment_material_prevents_pre_sdk_failure(unclaimed):
    h, job_id, _, binding, launch = unclaimed
    sid = assistant_key("session-id", binding.start_request_id)
    key = assistant_key("session", sid)
    request = ControlRequest(request_id="retain-unknown-assignment", request_digest="a" * 64, read_set=(key,))
    h.store.transact(request, lambda _: ControlChanges(
        writes=(ControlWrite(row_id=key, mode="create", data={"kind": "retained-opaque-assignment"}),),
        result={"retained": True},
    ))
    with pytest.raises(Problem) as refused:
        h.native.fence_unclaimed(binding, observed_agent_version="999")
    assert refused.value.body["error"]["code"] == "NATIVE_VERSION_REFUSAL_UNRESOLVED"
    assert h.workers.inspect(job_id).state == "starting" and len(h.occupied()) == 1
    assert h.native.launch(launch.launch_id).phase == "post_unknown"
    assert h.store.read_snapshot((key,))[key] is not None

