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
    SESSION_LOST, SESSION_VERSION_CHANGED, FoundryExecutor, _NativeLaunch, _TargetReservation,
    _lost_session_result, _version_refusal_result, lost_session_failure, native_key,
)
from shared_context.behavior_delivery import _digest, _json
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_operations import _parse
from shared_context.errors import Problem
from test_conversation_transfer_startup import (
    SyntheticSessionClient, claimed_turn, saved_turn, synthetic_standing_v1,
)
from legacy_turn_rows_helpers import read_startup
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key


@pytest.mark.parametrize("build,kind,code", [
    (_lost_session_result, "lost-session-result", SESSION_LOST),
    (_version_refusal_result, "version-refusal-result", SESSION_VERSION_CHANGED),
])
def test_a_closed_turn_keeps_its_own_evidence_id_and_failure_notice(build, kind, code):
    launch = SimpleNamespace(launch_id="launch-1", start=SimpleNamespace(binding=SimpleNamespace(work_id="work-1")),
                             definition=SimpleNamespace(model_dump=lambda mode: {"mode": mode}))
    result = build(launch, {"proof": 1}, "verifier-1")
    assert result.evidence_id == native_key(kind, "launch-1") and result.verifier_version == "verifier-1"
    assert (result.work_id, result.state, result.passed_checks) == ("work-1", "failed", ())
    assert result.definition_digest == _digest({"mode": "json"}) and result.proof == _json({"proof": 1})
    assert json.loads(result.content) == {"schema_version": "fa-assistant-notice-v1", "state": "failed",
                                          "code": code, "sdk_reply_available": False}


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

