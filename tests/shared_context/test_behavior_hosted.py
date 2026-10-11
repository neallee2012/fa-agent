"""Real LOCAL controlled child-process tests; NOT Foundry/native evidence."""

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from shared_context.behavior_hosted import (
    BehaviorExecutorClient, ChecksumCheckpoint, ChecksumSupervisor, HostedInvocationBody, HostedRuntime, LocalAssignment, LocalJournal,
)
from shared_context.errors import Problem
from shared_context.behavior_wire import HostedIdentity
from test_behavior_store import code


def assignment(name="launch-one", *, content=b"one\ntwo\x00\xff\n", **updates):
    return LocalAssignment(
        launch_id=name, claim_id="claim-" + name, invocation_id="invocation-" + name,
        identity=HostedIdentity(agent_id="11111111-1111-1111-1111-111111111111", agent_name="checksum-agent",
                                agent_version="7", session_id="preconfigured-session"),
        process_generation=1, process_id="host-process",
        binding_digest="a" * 64, input=content, input_digest=hashlib.sha256(content).hexdigest(),
        **updates,
    )


@pytest.mark.parametrize("audience", [
    "api://11111111-1111-1111-1111-111111111111",
    "api://11111111-1111-1111-1111-111111111111/fa-context-poc-api",
])
def test_hosted_api_preserves_existing_exact_resource_audience_contract(audience):
    client = BehaviorExecutorClient("https://api.example", audience)
    assert client.audience == audience
    client.close()


@pytest.mark.parametrize("audience", [
    "api://11111111-1111-1111-1111-111111111111/resource/.default",
    "api://11111111-1111-1111-1111-111111111111/resource?extra=1",
    "api://11111111-1111-1111-1111-111111111111/resource/other",
    "https://api.example", "api://invalid", None,
])
def test_hosted_api_rejects_relaxed_or_malformed_audiences(audience):
    with pytest.raises(Problem) as error:
        BehaviorExecutorClient("https://api.example", audience)
    assert code(error) == "HOSTED_API_CONFIGURATION"


def finish(supervisor, launch_id, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = supervisor.observe(launch_id)
        if state.phase == "sealed":
            return state
        time.sleep(0.02)
    child = supervisor.children.get(launch_id)
    if child is not None and child.poll() is None:
        child.terminate()
        child.wait(timeout=5)
    pytest.fail("owned constrained child did not finish within its local test deadline")


def test_real_child_hashes_original_bytes_and_replays_saved_result_without_another_child(tmp_path):
    supervisor = ChecksumSupervisor(tmp_path / "state")
    task = assignment()
    supervisor.start(task)
    result = finish(supervisor, task.launch_id)
    assert result.outcome == "succeeded" and result.child_exited and result.exit_code == 0
    assert result.result.input_digest == hashlib.sha256(task.input).hexdigest()
    assert result.result.byte_count == len(task.input) and result.result.newline_count == 2
    original_pid = result.child_pid
    restarted = ChecksumSupervisor(tmp_path / "state")
    assert restarted.start(task) == result
    assert restarted.children == {} and restarted.observe(task.launch_id).child_pid == original_pid


def test_host_process_restart_does_not_infer_terminal_from_an_unowned_child(tmp_path):
    supervisor = ChecksumSupervisor(tmp_path / "state")
    task = assignment(content=b"x" * 256, chunk_bytes=64)
    supervisor.start(task)
    restarted = ChecksumSupervisor(tmp_path / "state")
    state = restarted.start(task)
    assert state.phase in {"spawn_unknown", "running", "result_saved"} and not state.child_exited
    assert restarted.children == {}
    # Even after the real child exits, the new supervisor does not invent ownership.
    supervisor.children[task.launch_id].wait(timeout=15)
    assert restarted.observe(task.launch_id).phase != "sealed"
    assert not restarted.observe(task.launch_id).child_exited
    assert finish(supervisor, task.launch_id).phase == "sealed"
    assert restarted.observe(task.launch_id).phase == "sealed"


def test_new_control_request_after_sealed_child_keeps_original_terminal_result(tmp_path):
    supervisor = ChecksumSupervisor(tmp_path / "state")
    task = assignment()
    supervisor.start(task)
    result = finish(supervisor, task.launch_id)
    assert supervisor.control(task.launch_id, "cancel", "late-cancel") == result
    assert supervisor.control(task.launch_id, "park", "late-park") == result
    with sqlite3.connect(supervisor.journal.path) as db:
        assert db.execute("SELECT count(*) FROM controls").fetchone()[0] == 0


def test_original_launch_reservation_without_spawn_remains_unknown_after_restart(tmp_path):
    root = tmp_path / "state"
    task = assignment()
    journal = LocalJournal(root)
    assert journal.reserve(task, "lost-original-supervisor")
    restarted = ChecksumSupervisor(root)
    assert restarted.start(task).phase == "spawn_unknown"
    assert restarted.children == {}


def test_parked_checkpoint_reacquires_with_new_generation_and_no_overlapping_processed_bytes(tmp_path):
    supervisor = ChecksumSupervisor(tmp_path / "state")
    content = b"ab\n" * 100
    first = assignment(content=content, checkpoint_after_bytes=99, chunk_bytes=16)
    supervisor.start(first)
    parked = finish(supervisor, first.launch_id)
    assert parked.outcome == "parked" and parked.child_exited
    checkpoint = ChecksumCheckpoint.model_validate_json(parked.checkpoint)
    assert checkpoint.processed_bytes == 99
    assert checkpoint.prefix_digest == hashlib.sha256(content[:99]).hexdigest()
    second = assignment("resume", content=content).model_copy(update={
        "process_generation": 2, "checkpoint": parked.checkpoint,
        "checkpoint_digest": hashlib.sha256(parked.checkpoint).hexdigest(), "previous_launch_id": first.launch_id,
    })
    supervisor.start(second)
    completed = finish(supervisor, second.launch_id)
    assert completed.outcome == "succeeded" and completed.result.byte_count == len(content)
    assert completed.result.newline_count == 100
    with sqlite3.connect(supervisor.journal.path) as db:
        first_ranges = db.execute("SELECT start_offset,end_offset FROM chunks WHERE launch_id=?", (first.launch_id,)).fetchall()
        next_ranges = db.execute("SELECT start_offset,end_offset FROM chunks WHERE launch_id=?", (second.launch_id,)).fetchall()
    assert max(end for _, end in first_ranges) == min(start for start, _ in next_ranges) == 99
    assert sum(end - start for start, end in first_ranges + next_ranges) == len(content)
    assert supervisor.start(first).outcome == "parked"  # Late predecessor cannot resume.


@pytest.mark.parametrize("change", ["version", "session", "generation", "checkpoint"])
def test_reacquire_rejects_wrong_native_identity_or_checkpoint(tmp_path, change):
    supervisor = ChecksumSupervisor(tmp_path / "state")
    first = assignment(content=b"x" * 100, checkpoint_after_bytes=50)
    supervisor.start(first)
    parked = finish(supervisor, first.launch_id)
    second = assignment("resume", content=first.input).model_copy(update={
        "process_generation": 2, "checkpoint": parked.checkpoint,
        "checkpoint_digest": hashlib.sha256(parked.checkpoint).hexdigest(), "previous_launch_id": first.launch_id,
    })
    if change == "checkpoint":
        body = json.loads(second.checkpoint)
        body["processed_bytes"] = 51
        raw = json.dumps(body).encode()
        second = second.model_copy(update={"checkpoint": raw, "checkpoint_digest": hashlib.sha256(raw).hexdigest()})
    elif change == "generation":
        second = second.model_copy(update={"process_generation": 1})
    else:
        second = second.model_copy(update={"identity": second.identity.model_copy(update={
            "agent_version" if change == "version" else "session_id": "8" if change == "version" else "foreign-session",
        })})
    with pytest.raises(Problem) as error:
        supervisor.start(second)
    assert code(error) == "HOSTED_RESUME_DENIED"


def test_bounded_cancel_is_not_terminal_until_original_owned_process_exits(tmp_path):
    supervisor = ChecksumSupervisor(tmp_path / "state")
    task = assignment(content=b"x" * 8192, chunk_bytes=1)
    supervisor.start(task)
    early = supervisor.control(task.launch_id, "cancel", "cancel-request")
    if early.phase != "sealed":
        assert not early.child_exited
    result = finish(supervisor, task.launch_id)
    assert result.outcome == "stopped" and result.child_exited
    assert supervisor.control(task.launch_id, "cancel", "cancel-request") == result


def test_same_launch_cannot_change_original_bytes_or_input_version(tmp_path):
    supervisor = ChecksumSupervisor(tmp_path / "state")
    task = assignment()
    supervisor.start(task)
    finish(supervisor, task.launch_id)
    with pytest.raises(Problem) as error:
        supervisor.start(assignment(content=b"different"))
    assert code(error) == "HOSTED_LAUNCH_CONFLICT"


def test_pinned_executable_mismatch_does_not_spawn_or_retry(tmp_path):
    supervisor = ChecksumSupervisor(tmp_path / "state", python_sha256="0" * 64)
    task = assignment()
    with pytest.raises(Problem) as error:
        supervisor.start(task)
    assert code(error) == "HOSTED_EXECUTABLE_CHANGED" and supervisor.children == {}
    assert ChecksumSupervisor(tmp_path / "state").start(task).phase == "spawn_unknown"


def test_required_resume_material_missing_fails_instead_of_partial_success(tmp_path):
    supervisor = ChecksumSupervisor(tmp_path / "state")
    raw = ChecksumCheckpoint(
        input_digest=hashlib.sha256(b"x").hexdigest(), processed_bytes=0,
        prefix_digest=hashlib.sha256(b"").hexdigest(), newline_count=0,
    ).model_dump_json().encode()
    task = assignment(content=b"x").model_copy(update={
        "checkpoint": raw, "checkpoint_digest": hashlib.sha256(raw).hexdigest(),
        "previous_launch_id": "absent-predecessor", "process_generation": 2,
    })
    with pytest.raises(Problem) as error:
        supervisor.start(task)
    assert code(error) == "HOSTED_MATERIAL_MISSING" and supervisor.children == {}


@pytest.mark.parametrize("field", ["input", "command", "approved", "context", "session_id", "epoch", "process_generation"])
def test_native_invocation_json_cannot_supply_work_or_authority(field):
    with pytest.raises(ValidationError):
        HostedInvocationBody(binding_id="binding", launch_id="launch", **{field: "untrusted"})


def test_native_hosted_entry_is_disabled_without_explicit_configuration(monkeypatch):
    monkeypatch.delenv("BEHAVIOR_HOSTED_ENABLED", raising=False)
    with pytest.raises(Problem) as disabled:
        HostedRuntime.from_environment()
    assert code(disabled) == "HOSTED_RUNTIME_DISABLED"
