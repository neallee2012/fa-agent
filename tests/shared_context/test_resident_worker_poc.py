"""ADR-0005 resident worker PoC: real authority/HTTP/Table producers with the pinned SDK I/O double.

Covers the four PoC acceptance items offline: a follow-up turn keeps the released SDK instead of
restoring it, a lost worker falls back to the published checkpoint without resending, cancellation
stops the resident SDK, and a revocation after the light restore still blocks the next model write.
"""
from contextvars import Context

import pytest
from pydantic import ValidationError

from shared_context import behavior_assistant_hosted as hosted
from shared_context import behavior_wire as wire
from shared_context.behavior_assistant_wire import InstructionSubmit, InstructionTask
from shared_context.behavior_hosted import LocalExecution
from shared_context.behavior_instruction import TurnArchive
from shared_context.behavior_assistant import key
from shared_context.errors import Problem
from shared_context.m2_copilot import ResidentHolder, ResidentSdk

from test_behavior_assistant import assistant
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from instruction_flow_helpers import sdk_io, instruction_models
from work_policy_lifecycle_helpers import enabled
from legacy_turn_rows_helpers import outcome_of, stored_turn

POC = wire.RESIDENT_WORKER_PROFILE


def resident_enabled(h):
    from types import MappingProxyType
    from shared_context.behavior_assistant import AssistantProfile
    h.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({**value.model_dump(), "worker_profile": POC})
        for name, value in h.runtime.profiles.items()
    })
    h, control, human, facts = enabled(h)
    h.executor.resident = hosted.ResidentPool()
    return h, control, human


def client_count(monkeypatch):
    from shared_context import m2_copilot
    created = []
    factory = m2_copilot.CopilotClient

    def counted(**kwargs):
        client = factory(**kwargs)
        created.append(client)
        return client
    monkeypatch.setattr(m2_copilot, "CopilotClient", counted)
    return created


def turn(h, control, human, request_id, content, cid=None):
    binding, _ = control.submit(human, InstructionSubmit(request_id=request_id, conversation_id=cid, content=content))
    status = h.runtime.controller.step(binding.binding_id)
    return binding, status


def archive(h, binding):
    from shared_context.behavior_operations import _parse
    row_key = key("text-archive", binding.job_id)
    return _parse(h.store.read_snapshot((row_key,))[row_key].payload["data"], TurnArchive)


def conversation(h, binding):
    return h.runtime.profiles[binding.binding_id].instruction.conversation_id


def phases(caplog, job_id):
    import json
    found = []
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith("FA_TIMING "):
            value = json.loads(message.removeprefix("FA_TIMING "))
            if value.get("job_id") == job_id:
                found.append(value["phase"])
    return found


def test_follow_up_keeps_the_released_sdk_and_history_still_starts(assistant, monkeypatch, caplog):
    h, control, human = resident_enabled(assistant)
    calls = sdk_io(monkeypatch)
    created = client_count(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "first"}, {"content": "second"}])
    first, status = turn(h, control, human, "resident-one", "Say first.")
    assert status.state == "succeeded"
    released = archive(h, first).sdk_stop
    assert released.release == POC and released.exit_code is None and released.graceful
    second, status2 = turn(h, control, human, "resident-two", "Say second.", conversation(h, first))
    assert status2.state == "succeeded"
    assert len(created) == 1, "the follow-up must not start another SDK client"
    steps = phases(caplog, second.job_id)
    assert "resident_restore" in steps and "sdk_restore" not in steps and "sdk_process_start" not in steps
    assert calls[1]["resume"] is False and calls[1]["messages"][:2] == [
        {"role": "user", "content": "Say first."}, {"role": "assistant", "content": "first"}]
    assert archive(h, second).sdk_stop == released
    assert len(h.endpoint.requests) == 2


def test_lost_worker_restores_the_published_turn_without_resending(assistant, monkeypatch, caplog):
    h, control, human = resident_enabled(assistant)
    calls = sdk_io(monkeypatch)
    created = client_count(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "first"}, {"content": "after restart"}])
    first, _ = turn(h, control, human, "lost-one", "Remember apples.")
    # A replaced Hosted process has an empty pool; the old SDK is unreachable.
    h.executor.resident = hosted.ResidentPool()
    second, status = turn(h, control, human, "lost-two", "What did I ask?", conversation(h, first))
    assert status.state == "succeeded" and "sdk_restore" in phases(caplog, second.job_id)
    assert len(created) == 2 and calls[1]["resume"] is True
    assert [c["prompt"] for c in calls] == ["Remember apples.", "What did I ask?"]
    assert len(h.endpoint.requests) == 2


def test_files_changed_after_release_force_the_published_restore(assistant, monkeypatch, caplog):
    from shared_context.behavior_instruction import sdk_session_id
    h, control, human = resident_enabled(assistant)
    calls = sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "first"}, {"content": "second"}])
    first, _ = turn(h, control, human, "moved-one", "First.")
    live = h.executor.resident.holder(sdk_session_id(conversation(h, first))).live
    assert live is not None and live.reusable()
    live.files.files["events.jsonl"] += '{"type":"session.error","data":{}}\n'
    assert not live.reusable()
    second, status = turn(h, control, human, "moved-two", "Second.", conversation(h, first))
    assert status.state == "succeeded" and "sdk_restore" in phases(caplog, second.job_id)
    assert calls[1]["resume"] is True and len(h.endpoint.requests) == 2


def test_saved_release_passes_the_real_native_startup_readers(stored_turn):
    """Coordinator condition: after saving a release the API still starts (no new record kind).

    Both old turns ran on the PoC template, selected the same way the operator will select it on agent B.
    """
    from legacy_turn_rows_helpers import read_startup
    from shared_context.behavior_migration import MigrationStore, installed_origin
    from shared_context.behavior_operations import _parse
    from shared_context.behavior_profile_selection import current_selection, profile_from_template

    def readers(c):
        store = MigrationStore(c.h.composition.clients[0], c.h.config.table.scope)
        return (outcome_of(lambda: read_startup(c)), outcome_of(lambda: installed_origin(store, c.h.config)))
    c = stored_turn("synthetic-exit")
    selected = current_selection(c.control.store, c.control.policy)
    assert profile_from_template(c.control.policy, selected.command.template).worker_profile == POC
    after_exit = readers(c)
    assert after_exit[0] == "ok"
    released = wire.AssistantSdkStop(pid=99, release=POC)
    # The release turn followed the exit turn on the same store.
    c = stored_turn("synthetic-release")
    row_key = key("text-archive", c.turn.binding.job_id)
    saved = _parse(c.h.runtime.store.read_snapshot((row_key,))[row_key].payload["data"], TurnArchive)
    assert saved.sdk_stop == released
    # Release records are classified exactly as ordinary exit records by every startup reader.
    assert readers(c) == after_exit


def test_revocation_after_the_light_restore_blocks_the_next_model_write(assistant, monkeypatch, caplog):
    h, control, human = resident_enabled(assistant)
    calls = sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "first"}, {"content": "must not be sent"}])
    first, _ = turn(h, control, human, "revoke-one", "First.")
    original = h.assistant.resident_restore

    def revoke_after(*args, **kwargs):
        result = original(*args, **kwargs)
        Context().run(control.revoke, human, "revoke-resident-poc")
        return result
    monkeypatch.setattr(h.assistant, "resident_restore", revoke_after)
    binding, _ = control.submit(human, InstructionSubmit(
        request_id="revoke-two", conversation_id=conversation(h, first), content="Second."))
    with pytest.raises(Problem) as stopped:
        h.runtime.controller.step(binding.binding_id)
    # The worker hides the adapter's own error; the refused model write's reason is in the API log.
    assert stopped.value.body["error"]["code"] == "WORKER_START_UNKNOWN"
    assert '"code":"WORK_POLICY_SCOPE_CHANGED"' in caplog.text
    assert len(h.endpoint.requests) == 1, "a revoked follow-up must not reach the model"
    assert len(calls) == 1


def test_a_release_requires_the_captured_profile():
    stop = wire.AssistantSdkStop(pid=7, release=POC)
    base = dict(launch_id="launch", assignment_digest="a" * 64, supervisor_id="sup", child_nonce="nonce",
                phase="sealed", child_pid=7, outcome="succeeded", sdk_stop=stop)
    assert LocalExecution(**base).sdk_stop.release == POC
    for outcome in ("stopped", "failed"):
        assert LocalExecution(**{**base, "outcome": outcome}).outcome == outcome
    for change in ({"outcome": "parked"}, {"child_exited": True}, {"child_pid": 8}, {"exit_code": 0}):
        with pytest.raises(ValidationError):
            LocalExecution(**{**base, **change})
    with pytest.raises(ValidationError):
        wire.AssistantSdkStop(pid=7)
    from shared_context.behavior_sdk_state import CheckpointProfile
    with pytest.raises(ValidationError, match="resident worker PoC"):
        InstructionTask(content="x", conversation_id="c", turn_number=1, worker_profile=POC,
                        checkpoint_profile=CheckpointProfile())
    assert InstructionTask(content="x", conversation_id="c", turn_number=1, worker_profile=POC).worker_profile == POC


def test_a_hosted_release_without_the_profile_cannot_complete_the_job(assistant, monkeypatch):
    h, control, human, _ = enabled(assistant)
    sdk_io(monkeypatch)
    h.executor.resident = hosted.ResidentPool()
    original = hosted.AssistantSession.__init__

    def forced(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self._resident = kwargs.get("resident")  # a faulty Hosted that ignores the captured profile
    monkeypatch.setattr(hosted.AssistantSession, "__init__", forced)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "first"}])
    binding, _ = control.submit(human, InstructionSubmit(request_id="forced-release", content="Hello."))
    try:
        status = h.runtime.controller.step(binding.binding_id)
    except Exception:
        status = None
    assert status is None or status.state != "succeeded"
    row_key = key("text-archive", binding.job_id)
    assert h.store.read_snapshot((row_key,))[row_key] is None


def native_plan(h, binding, stop, *, exited, outcome="succeeded"):
    from types import SimpleNamespace
    from shared_context.behavior_assistant import _Session, _digest
    from shared_context.behavior_assistant_wire import ExecutionResult
    job = h.workers.inspect(binding.job_id)
    sid = key("session-id", job.binding.start_request_id)
    session_key = key("session", sid)
    from shared_context.behavior_operations import _parse
    session = _parse(h.store.read_snapshot((session_key,))[session_key].payload["data"], _Session)
    result = h.assistant.result(job.binding)
    report = SimpleNamespace(sdk_stop=stop, child_pid=stop.pid, exit_code=stop.exit_code, child_exited=exited,
                             outcome=outcome, checkpoint=None,
                             result=ExecutionResult(session_id=sid, result_digest=_digest(result.model_dump(mode="json"))))
    return h.assistant.native_report_plan(SimpleNamespace(start=SimpleNamespace(binding=job.binding)), report, None)


def test_native_report_accepts_a_release_only_for_the_captured_profile(assistant, monkeypatch):
    from shared_context.behavior_foundry import _resident_release
    from shared_context.errors import Problem
    from types import SimpleNamespace
    h, control, human = resident_enabled(assistant)
    sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "first"}])
    first, _ = turn(h, control, human, "native-plan", "Hello.")
    released = archive(h, first).sdk_stop
    assert native_plan(h, first, released, exited=False)[0].state == "succeeded"
    with pytest.raises(Problem) as denied:
        native_plan(h, first, released, exited=True)
    assert denied.value.body["error"]["code"] == "ASSISTANT_NATIVE_IDLE"
    shape = SimpleNamespace(sdk_stop=released, child_exited=False, exit_code=None, child_pid=released.pid)
    assert _resident_release(shape)
    assert not _resident_release(SimpleNamespace(**{**vars(shape), "child_pid": released.pid + 1}))
    assert not _resident_release(SimpleNamespace(**{**vars(shape), "sdk_stop": wire.AssistantSdkStop(pid=7, exit_code=0)}))


def test_native_report_refuses_a_release_for_an_ordinary_task(assistant, monkeypatch):
    from shared_context.errors import Problem
    h, control, human, _ = enabled(assistant)
    sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "first"}])
    first, status = turn(h, control, human, "ordinary-native", "Hello.")
    assert status.state == "succeeded"
    stopped = archive(h, first).sdk_stop
    assert stopped.release is None and stopped.exit_code is not None
    with pytest.raises(Problem) as denied:
        native_plan(h, first, wire.AssistantSdkStop(pid=stopped.pid, release=POC), exited=False)
    assert denied.value.body["error"]["code"] == "ASSISTANT_NATIVE_IDLE"


def test_poc_template_selection_refuses_checkpoint_profiles(assistant):
    from shared_context.behavior_sdk_state import CheckpointProfile
    from conversation_profile_selection_helpers import configured, select, template
    h, _, _ = configured(assistant)
    refused, _ = select(h, request_id="poc-with-checkpoint",
                        value=template(worker_profile=POC, checkpoint_profile=CheckpointProfile()))
    assert refused.status_code == 422
    selected, _ = select(h, request_id="poc", value=template(worker_profile=POC))
    assert selected.status_code == 200, selected.text


def test_an_unbound_resident_sdk_fails_closed():
    import asyncio
    live = ResidentSdk("digest", "sid", files=None)
    denied = live.permission(object(), {})
    assert type(denied).__name__ == "PermissionDecisionReject"
    result = asyncio.run(live.invoke(object()))
    assert result.result_type == "failure"
    with pytest.raises(RuntimeError):
        asyncio.run(live.gateway.send_request(object(), object()))
    assert ResidentHolder().live is None
