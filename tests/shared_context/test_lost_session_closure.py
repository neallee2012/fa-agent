"""A claimed instruction turn whose Hosted stopped answering closes only on its stopped-session status.

Synthetic native-mode startup fixture with local Table I/O: the Foundry client is a local double whose
invocations fail and whose session status is set by the test. No SDK process, model or Azure call runs.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_foundry import (
    SESSION_LOST, STOP_ACCEPTED, FoundryExecutor, _NativeLaunch, _TargetReservation, lost_session_failure, native_key,
)
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_operations import _parse
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from test_conversation_transfer_startup import (
    SyntheticSessionClient, claimed_turn, read_startup, saved_turn, synthetic_standing_v1,
)
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


class LostHostedClient(SyntheticSessionClient):
    """The original Hosted process no longer answers; only the platform session status is readable."""

    def for_target(self, target):
        return LostHostedClient(target, self.observations)

    def _invocation(self, action):
        self.observations.calls.append((action, self.target))
        raise Problem(503, "FOUNDRY_INVOCATION_UNKNOWN", "Synthetic Hosted process does not answer")

    def observe(self, invocation_id, probe_id):
        self._invocation("observe")

    def cancel(self, invocation_id, probe_id):
        self._invocation("cancel")

    def session_status(self):
        self.observations.calls.append(("status", self.target))
        if self.observations.status is None:
            raise Problem(503, "CONVERSATION_SESSION_UNAVAILABLE", "Synthetic metadata read is unavailable")
        if self.observations.status == "drifted":
            # FoundryHttpClient.session_status() once the platform restarted the session on another version.
            raise Problem(409, "CONVERSATION_SESSION_CHANGED", "Synthetic session reports another version")
        return SimpleNamespace(agent_session_id=self.observations.session_id or self.target.session_id,
                               version_indicator=SimpleNamespace(agent_version=self.target.agent_version),
                               status=self.observations.status)

    def stop_session(self):
        self.observations.calls.append(("stop", self.target))
        self.observations.status = getattr(self.observations, "after_stop", "idle")


def lost_hosted(c, status, after_stop="idle"):
    c.sessions.status, c.sessions.session_id, c.sessions.after_stop = status, None, after_stop
    workers = c.h.runtime.dep.workers
    executor = FoundryExecutor(c.native, tuple(LostHostedClient(target, c.sessions) for target in c.native.targets))
    workers.adapter = replace(workers.adapter, query=executor.query, stop=executor.stop,
                              stopped_session=executor.stopped_session)
    c.sessions.calls.clear()
    return executor


def readers(c):
    def outcome(call):
        try:
            call()
            return "ok"
        except Problem as error:
            return error.body["error"]["code"]
    store = MigrationStore(c.h.composition.clients[0], c.h.config.table.scope)
    return (outcome(lambda: read_startup(c)), outcome(lambda: installed_origin(store, c.h.config)))


def launch_rows(c, binding):
    launch_id = native_key("launch-id", binding.start_request_id)
    launch = c.native.launch(launch_id)
    target_key = c.native.target_key(launch.target, binding.job_id)
    reservation = _parse(c.h.runtime.store.read_snapshot((target_key,))[target_key].payload["data"], _TargetReservation)
    return launch, reservation


@pytest.mark.parametrize("terminal", ["failed", "deleted", "expired"])
def test_a_terminal_session_closes_the_turn_with_an_explicit_blocker_and_readable_history(synthetic_standing_v1, terminal):
    c = synthetic_standing_v1
    first = saved_turn(c, "lost-first")
    cid = first.assignment.task.instruction.conversation_id
    baseline = readers(c)
    opened = claimed_turn(c, "lost-second", cid)
    workers, job_id, owner = c.h.runtime.dep.workers, opened.binding.job_id, opened.binding.owner
    executor = lost_hosted(c, "active")

    # While the session is active, an unanswered observation stays unknown: no failure is inferred.
    with pytest.raises(Problem) as unknown:
        workers.reconcile(job_id, owner)
    assert unknown.value.body["error"]["code"] == "WORKER_QUERY_UNKNOWN"
    assert [call for call, _ in c.sessions.calls] == ["status", "observe"]
    assert workers.inspect(job_id).state in {"starting", "running"}
    assert executor.stopped_session(workers.inspect(job_id).binding) is None

    # The platform reports a terminal session: only now does the attempt close, without another invocation.
    c.sessions.status = terminal
    c.sessions.calls.clear()
    assert executor.stopped_session(workers.inspect(job_id).binding) == terminal
    workers.reconcile(job_id, owner)
    workers.forward_result(job_id, owner, c.h.runtime.dep.delivery)
    assert [call for call, _ in c.sessions.calls] == ["status", "status"]
    job = workers.inspect(job_id)
    assert job.state == "failed" and job.result_forwarded and not job.cancel_requested
    assert lost_session_failure(c.h.runtime.store, job) == SESSION_LOST
    launch, reservation = launch_rows(c, workers.inspect(job_id).binding)
    assert launch.phase == "sealed" and reservation.active_launch_id is None
    evidence = c.native.verify(launch.sealed_evidence_id)
    assert evidence.state == "failed" and json.loads(evidence.result.content)["code"] == SESSION_LOST
    proof = json.loads(evidence.proof)
    assert proof["session_status"] == terminal and proof["stop_requested"] is False

    view = c.control.conversation(c.human, cid)
    assert view["can_follow_up"] is False and view["follow_up_blocker"] == SESSION_LOST and view["can_reset"]
    with pytest.raises(Problem) as blocked:
        c.control.submit(c.human, InstructionSubmit(
            request_id="lost-follow-up", conversation_id=cid, content="Continue."))
    assert blocked.value.body["error"]["code"] == SESSION_LOST
    # The earlier saved turn is untouched, and every startup reader gives the same outcome as before.
    assert c.control.read_reply(c.human, cid, 1)
    assert readers(c) == baseline


@pytest.mark.parametrize("change", ["unknown", "active", "other-session", "idle"])
def test_no_proof_of_a_stopped_session_keeps_the_attempt_unknown(synthetic_standing_v1, change):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "lost-unproven")
    workers, job_id, owner = c.h.runtime.dep.workers, opened.binding.job_id, opened.binding.owner
    status = {"unknown": None, "active": "active", "other-session": "expired", "idle": "idle"}[change]
    executor = lost_hosted(c, status)
    if change == "other-session":
        c.sessions.session_id = "another-session"
    with pytest.raises(Problem) as unknown:
        workers.reconcile(job_id, owner)
    assert unknown.value.body["error"]["code"] == "WORKER_QUERY_UNKNOWN"
    if change == "idle":
        # A suspended session alone is not proof, and no invocation wakes it into new compute.
        assert [call for call, _ in c.sessions.calls] == ["status"]
        assert executor.stopped_session(workers.inspect(job_id).binding) is None
    launch, reservation = launch_rows(c, workers.inspect(job_id).binding)
    assert launch.phase == "claimed" and reservation.active_launch_id == launch.launch_id
    assert workers.inspect(job_id).state in {"starting", "running"}
    assert ("stop", c.native.targets[0]) not in [(call, target) for call, target in c.sessions.calls]


@pytest.mark.parametrize("before", ["active", "idle"])
def test_a_cancel_the_hosted_cannot_answer_stops_the_session_once_and_closes_cancelled(synthetic_standing_v1, before):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "lost-cancel")
    workers, job_id, owner = c.h.runtime.dep.workers, opened.binding.job_id, opened.binding.owner
    lost_hosted(c, before)
    c.control.cancel(c.human, opened.binding.binding_id)
    workers.stop(job_id, owner)
    workers.forward_result(job_id, owner, c.h.runtime.dep.delivery)
    # An already suspended session (the live case) is stopped by FA itself without waking it by an invocation.
    expected = ["status", "cancel", "stop", "status"] if before == "active" else ["status", "stop", "status"]
    assert [call for call, _ in c.sessions.calls] == expected
    job = workers.inspect(job_id)
    assert job.state == "cancelled" and job.result_forwarded
    launch, reservation = launch_rows(c, workers.inspect(job_id).binding)
    assert launch.phase == "sealed" and reservation.active_launch_id is None
    proof = json.loads(c.native.verify(launch.sealed_evidence_id).proof)
    assert proof["cancel_requested"] is True and proof["stop_requested"] is True
    assert proof["session_status"] == "idle"
    assert lost_session_failure(c.h.runtime.store, job) == SESSION_LOST
    assert readers(c)[0] == "ok"


def test_a_client_without_a_status_read_never_stops_the_session(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "lost-no-status")
    workers, job_id, owner = c.h.runtime.dep.workers, opened.binding.job_id, opened.binding.owner
    monkeypatch.delattr(LostHostedClient, "session_status")
    lost_hosted(c, "active")
    c.control.cancel(c.human, opened.binding.binding_id)
    with pytest.raises(Problem) as unknown:
        workers.stop(job_id, owner)
    assert unknown.value.body["error"]["code"] == "WORKER_STOP_UNKNOWN"
    assert [call for call, _ in c.sessions.calls] == ["cancel"]
    launch, _ = launch_rows(c, workers.inspect(job_id).binding)
    assert launch.phase == "claimed"


def test_a_cancel_whose_session_status_cannot_be_read_closes_on_the_accepted_stop(synthetic_standing_v1, caplog):
    # Live text-abb9b897: the session now reports another agent version, so the exact read fails before
    # and after FA's own stop.
    c = synthetic_standing_v1
    opened = claimed_turn(c, "lost-unreadable")
    workers, job_id, owner = c.h.runtime.dep.workers, opened.binding.job_id, opened.binding.owner
    lost_hosted(c, "drifted", after_stop="drifted")
    c.control.cancel(c.human, opened.binding.binding_id)
    with caplog.at_level("WARNING"):
        workers.stop(job_id, owner)
    assert "INSTRUCTION_SESSION_STATUS_UNREADABLE" in caplog.text and "CONVERSATION_SESSION_CHANGED" in caplog.text
    workers.forward_result(job_id, owner, c.h.runtime.dep.delivery)
    assert [call for call, _ in c.sessions.calls] == ["status", "cancel", "stop", "status"]
    job = workers.inspect(job_id)
    assert job.state == "cancelled" and job.result_forwarded
    launch, reservation = launch_rows(c, workers.inspect(job_id).binding)
    assert launch.phase == "sealed" and reservation.active_launch_id is None
    proof = json.loads(c.native.verify(launch.sealed_evidence_id).proof)
    assert proof["session_status"] == STOP_ACCEPTED and proof["stop_requested"] is True
    assert lost_session_failure(c.h.runtime.store, job) == SESSION_LOST
    assert readers(c)[0] == "ok"
    # Without FA's own stop after a cancel, an accepted stop is never proof.
    with pytest.raises(Problem) as invalid:
        c.native.seal_lost_session(workers.inspect(job_id).binding, session_status=STOP_ACCEPTED, stop_requested=False)
    assert invalid.value.body["error"]["code"] == "NATIVE_LOST_SESSION_INVALID"


def test_a_session_still_running_after_the_stop_stays_unknown(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "lost-still-running")
    workers, job_id, owner = c.h.runtime.dep.workers, opened.binding.job_id, opened.binding.owner
    lost_hosted(c, "active", after_stop="active")
    c.control.cancel(c.human, opened.binding.binding_id)
    with pytest.raises(Problem) as unknown:
        workers.stop(job_id, owner)
    assert unknown.value.body["error"]["code"] == "WORKER_STOP_UNKNOWN"
    assert [call for call, _ in c.sessions.calls] == ["status", "cancel", "stop", "status"]
    launch, _ = launch_rows(c, workers.inspect(job_id).binding)
    assert launch.phase == "claimed" and workers.inspect(job_id).state == "capacity_uncertain"


def test_a_cancel_whose_stop_reservation_was_used_closes_on_the_next_observation(synthetic_standing_v1):
    # Live text-abb9b897 after its cancel: capacity_uncertain, stop reservation used, status unreadable.
    c = synthetic_standing_v1
    opened = claimed_turn(c, "lost-after-cancel")
    workers, job_id, owner = c.h.runtime.dep.workers, opened.binding.job_id, opened.binding.owner
    executor = lost_hosted(c, "active", after_stop="active")
    c.control.cancel(c.human, opened.binding.binding_id)
    with pytest.raises(Problem):
        workers.stop(job_id, owner)
    assert workers.inspect(job_id).stop_credit == 0
    c.sessions.status, c.sessions.after_stop = None, None
    c.sessions.calls.clear()
    # The observer's read-only check lets the next step run instead of waiting forever.
    assert executor.stopped_session(workers.inspect(job_id).binding) == "unreadable"
    workers.reconcile(job_id, owner)
    workers.forward_result(job_id, owner, c.h.runtime.dep.delivery)
    assert [call for call, _ in c.sessions.calls] == ["status", "status", "observe", "stop", "status"]
    job = workers.inspect(job_id)
    assert job.state == "cancelled" and job.result_forwarded
    proof = json.loads(c.native.verify(launch_rows(c, job.binding)[0].sealed_evidence_id).proof)
    assert proof["session_status"] == STOP_ACCEPTED and proof["cancel_requested"] is True
    assert readers(c)[0] == "ok"


def test_the_observer_reads_only_the_session_after_an_unknown_and_closes_once_it_stops(synthetic_standing_v1, monkeypatch):
    import _socket
    import asyncio
    import socket

    def loopback_only(sock, address):
        # asyncio's own event-loop self-pipe on Windows; any external connection still fails.
        if address[0] not in {"127.0.0.1", "::1"}:
            pytest.fail("this startup fixture must not perform external I/O")
        return _socket.socket.connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    c = synthetic_standing_v1
    opened = claimed_turn(c, "lost-observer")
    workers, job_id, binding_id = c.h.runtime.dep.workers, opened.binding.job_id, opened.binding.binding_id
    lost_hosted(c, "active")
    # More than the #212 activity window has passed since the Hosted's last call.
    monkeypatch.setattr(c.native, "instruction_recently_active", lambda job: False)
    server = c.h.composition
    delivery = c.h.runtime.dep.delivery
    job_key = worker_key("job", job_id)

    async def run():
        # The observer reads its interval once at launch; the original contract is restored for forwarding.
        original = delivery.contract
        delivery.contract = original.model_copy(update={"review_interval_seconds": 0.05})
        try:
            server.launch_instruction(binding_id)
        finally:
            delivery.contract = original
        for _ in range(500):
            await asyncio.sleep(0.02)
            if binding_id in server._instruction_lost:
                break
        assert binding_id in server._instruction_lost, c.sessions.calls
        assert [call for call, _ in c.sessions.calls].count("observe") == 1
        before = c.h.runtime.store.read_snapshot((job_key,))[job_key].etag
        reads = [call for call, _ in c.sessions.calls].count("status")
        await asyncio.sleep(1.0)
        # While waiting it reads only the session: no invocation, probe or job write.
        assert [call for call, _ in c.sessions.calls].count("observe") == 1
        assert {call for call, _ in c.sessions.calls} <= {"status", "observe"}
        assert c.h.runtime.store.read_snapshot((job_key,))[job_key].etag == before
        # The reads back off (0.05, 0.1, 0.2, 0.4 s ...) instead of polling every interval (about 20 here).
        assert 1 <= [call for call, _ in c.sessions.calls].count("status") - reads <= 6
        c.sessions.status = "expired"
        await asyncio.wait_for(server._instruction_tasks[binding_id], 30)

    asyncio.run(run())
    job = workers.inspect(job_id)
    assert job.state == "failed" and job.result_forwarded
    assert binding_id not in server._instruction_lost
    assert [call for call, _ in c.sessions.calls].count("observe") == 1
