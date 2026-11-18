"""Reset stops a finished conversation's Hosted session once; ordinary turns keep their conversation session.

Synthetic native-mode startup fixture with local Table I/O: the Foundry client is a local double that records
each session stop. No SDK process, model or Azure call runs.
"""


import pytest

from shared_context.behavior_foundry import InstructionSessionStop, native_key
from shared_context.behavior_operations import _parse
from shared_context.errors import Problem
from test_conversation_transfer_startup import SyntheticSessionClient, saved_turn, synthetic_standing_v1
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key


class StoppingClient(SyntheticSessionClient):
    def for_target(self, target):
        return StoppingClient(target, self.observations)

    def stop_session(self):
        self.observations.calls.append(("stop", self.target))
        stop = getattr(self.observations, "stop", None)
        if stop is not None:
            stop()


@pytest.fixture
def stopping(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    executor = c.h.runtime.dep.workers.adapter.start.__self__
    monkeypatch.setattr(executor, "clients", {
        executor_id: StoppingClient(client.target, c.sessions) for executor_id, client in executor.clients.items()})
    c.executor = executor
    return c


def _stops(c):
    return [call for call in c.sessions.calls if call[0] == "stop"]


def _stop_row(c, job_id):
    binding = c.h.runtime.dep.workers.inspect(job_id).binding
    launch = c.native.launch(native_key("launch-id", binding.start_request_id))
    row_key = native_key("instruction-session-stop", launch.launch_id)
    row = c.h.runtime.store.read_snapshot((row_key,))[row_key]
    return launch, row_key, None if row is None else _parse(row.payload["data"], InstructionSessionStop)


def _cid(c, saved):
    return c.control.records[saved.binding.binding_id].profile.instruction.conversation_id


def test_a_turn_without_a_conversation_session_stops_its_session_when_it_is_observed_finished(stopping, monkeypatch):
    # An instruction registered without a conversation-owned session (hosted_session_id None) stops its own
    # session once its work is observed finished; the record is changed after the turn so its start is unchanged.
    c = stopping
    saved = saved_turn(c, "per-turn-session")
    assert _stops(c) == []
    real = c.control._job_record

    def without_session(job_id):
        record = real(job_id)
        return None if record is None else record.model_copy(update={"hosted_session_id": None})
    monkeypatch.setattr(c.control, "_job_record", without_session)
    binding = c.h.runtime.dep.workers.inspect(saved.binding.job_id).binding
    for _ in range(2):
        c.executor.query(binding)
    launch, _, row = _stop_row(c, saved.binding.job_id)
    assert _stops(c) == [("stop", launch.target)] and row.confirmed is True


@pytest.mark.parametrize("change", ["other-target", "not-sealed"])
def test_only_the_sealed_original_launch_may_reserve_its_session_stop(stopping, change):
    # Private entry: the refusal guards a launch row that changed after the caller read it, which the public
    # reset and observe paths cannot produce (they read the launch right before this call).
    c = stopping
    saved = saved_turn(c, "stop-fenced-" + change)
    launch, _, _ = _stop_row(c, saved.binding.job_id)
    stale = (launch.model_copy(update={"target": launch.target.model_copy(update={"agent_version": "999"})})
             if change == "other-target" else launch)
    if change == "not-sealed":
        c.h.mutate(native_key("launch", launch.launch_id), phase="claimed")
    with pytest.raises(Problem) as fenced:
        c.executor._cleanup_instruction_session(stale, retire=True)
    assert fenced.value.body["error"]["code"] == "NATIVE_SESSION_CLEANUP_FENCED"
    assert _stops(c) == [] and _stop_row(c, saved.binding.job_id)[2] is None
