"""Reset stops a finished conversation's Hosted session once; ordinary turns keep their conversation session.

Synthetic native-mode startup fixture with local Table I/O: the Foundry client is a local double that records
each session stop. No SDK process, model or Azure call runs.
"""

import logging

import pytest

from shared_context.behavior_foundry import InstructionSessionStop, native_key
from shared_context.behavior_operations import _parse
from shared_context.behavior_wire import Command
from shared_context.errors import Problem
from test_conversation_transfer_startup import SyntheticSessionClient, failed_turn, saved_turn, synthetic_standing_v1
from test_instruction_flow import instruction_models
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


def test_reset_stops_the_finished_conversations_session_once_and_replays_without_another_stop(stopping):
    c = stopping
    saved = saved_turn(c, "stop-on-reset")
    assert _stops(c) == [] and _stop_row(c, saved.binding.job_id)[2] is None  # a finished turn keeps its session
    reset = c.control.reset_conversation(c.human, _cid(c, saved), Command(request_id="reset-and-stop"))
    launch, _, row = _stop_row(c, saved.binding.job_id)
    assert reset["session_stop"] == "confirmed" and _stops(c) == [("stop", launch.target)]
    assert row == InstructionSessionStop(launch_id=launch.launch_id, job_id=saved.binding.job_id, target=launch.target,
                                         confirmed=True)
    guard = c.h.runtime.store._guard()
    assert c.control.reset_conversation(c.human, _cid(c, saved), Command(request_id="reset-and-stop")) == reset
    assert _stops(c) == [("stop", launch.target)] and c.h.runtime.store._guard() == guard


def test_a_stop_without_an_answer_keeps_its_intent_and_is_not_sent_again(stopping, caplog):
    c = stopping
    saved = saved_turn(c, "stop-unknown")

    def unanswered():
        raise Problem(503, "FOUNDRY_SESSION_UNKNOWN", "Synthetic stop response loss")
    c.sessions.stop = unanswered
    with caplog.at_level(logging.WARNING):
        reset = c.control.reset_conversation(c.human, _cid(c, saved), Command(request_id="reset-stop-unknown"))
    launch, _, row = _stop_row(c, saved.binding.job_id)
    assert reset["session_stop"] == "unknown" and row.confirmed is False and row.target == launch.target
    assert "INSTRUCTION_SESSION_STOP_UNKNOWN original_intent_retained=true" in caplog.text
    c.sessions.stop = None
    again = c.control.reset_conversation(c.human, _cid(c, saved), Command(request_id="reset-stop-unknown"))
    assert again == reset and _stops(c) == [("stop", launch.target)] and not _stop_row(c, saved.binding.job_id)[2].confirmed


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


def test_reset_after_a_version_refusal_leaves_the_session_alone(stopping):
    c = stopping
    saved = saved_turn(c, "before-version-refusal")
    cid = _cid(c, saved)
    refused = failed_turn(c, cid)
    reset = c.control.reset_conversation(c.human, cid, Command(request_id="reset-after-refusal"))
    assert reset["session_stop"] == "unknown" and _stops(c) == []
    assert _stop_row(c, refused.job_id)[2] is None


def test_a_stop_row_changed_while_stopping_is_refused_and_stays_unconfirmed(stopping):
    c = stopping
    saved = saved_turn(c, "stop-row-changed")
    _, row_key, _ = _stop_row(c, saved.binding.job_id)
    c.sessions.stop = lambda: c.h.mutate(row_key, job_id="another-job")
    with pytest.raises(Problem) as fenced:
        c.control.reset_conversation(c.human, _cid(c, saved), Command(request_id="reset-row-changed"))
    assert fenced.value.body["error"]["code"] == "NATIVE_SESSION_CLEANUP_FENCED"
    row = _stop_row(c, saved.binding.job_id)[2]
    assert row.job_id == "another-job" and row.confirmed is False and len(_stops(c)) == 1


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
