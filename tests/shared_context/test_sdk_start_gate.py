"""The journal gate, not an exception or a missing PID, proves closed entry."""

from contextlib import contextmanager
import json
from threading import Barrier, Event, Thread

import pytest

from shared_context.behavior_code_identity import code_sha256
from shared_context.behavior_hosted import LocalJournal
from shared_context.behavior_sdk_start import SDK_START_GATE_ERRORS, SdkStartGate
from shared_context.behavior_wire import SdkStartBinding, SdkStartOwner
from shared_context.errors import Problem


def binding():
    return SdkStartBinding(schema_version="fa-sdk-start-binding-v1", profile="fa-sdk-start-gate-v1",
        session_id="1" * 64, sdk_session_id="original-sdk", job_id="original-job",
        task_digest="2" * 64, profile_digest="3" * 64, execution_digest="4" * 64,
        worker_configuration_digest="5" * 64, code_sha256=code_sha256())


@pytest.mark.parametrize("winner", ["entry", "close", "race"])
def test_entry_and_close_compete_for_the_same_irreversible_gate(tmp_path, winner):
    journal = LocalJournal(tmp_path)
    ready, proceed, entered = Event(), Event(), Event()
    race = Barrier(2)
    result = {}

    def run():
        gate = SdkStartGate(journal, binding(), SdkStartOwner(kind="local-offline", supervisor_id="original-owner"))
        result["gate"] = gate
        ready.set()
        assert proceed.wait(5)
        if winner == "race":
            race.wait(timeout=5)
        try:
            gate.enter("original-sdk")
            result["entered"] = True
        except Problem as error:
            result["entry_error"] = error.body["error"]["code"]
        finally:
            entered.set()

    thread = Thread(target=run)
    thread.start()
    try:
        assert ready.wait(5)
        gate = result["gate"]
        if winner == "entry":
            proceed.set()
            assert entered.wait(5)
        elif winner == "race":
            proceed.set()
            race.wait(timeout=5)
        try:
            gate.close("SKILL_SOURCE_CHANGED")
            result["closed"] = True
        except Problem as error:
            result["close_error"] = error.body["error"]["code"]
        if winner == "close":
            assert gate.observe_closed() is None
            proceed.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert bool(result.get("entered")) != bool(result.get("closed"))
        if result.get("closed"):
            assert result["entry_error"] == "SDK_START_GATE_FENCED"
            observation = gate.observe_closed()
            assert observation.gate.state == "closed_before_entry" and observation.owned_thread_completed
        else:
            assert result["close_error"] == "SDK_START_GATE_FENCED"
            assert gate.observe_closed() is None
        with pytest.raises(Problem) as replay:
            SdkStartGate(journal, binding(), SdkStartOwner(kind="local-offline", supervisor_id="original-owner"))
        assert replay.value.body["error"]["code"] == "SDK_START_GATE_REPLAY"
    finally:
        proceed.set()
        thread.join(timeout=5)


@pytest.mark.parametrize("transition", ["entry", "close"])
@pytest.mark.parametrize("lost_at", ["before-commit", "after-commit"])
def test_lost_gate_write_never_becomes_a_never_started_observation(tmp_path, monkeypatch, transition, lost_at):
    journal = LocalJournal(tmp_path)
    ready, proceed = Event(), Event()
    result = {}

    def run():
        gate = SdkStartGate(journal, binding(), SdkStartOwner(kind="local-offline", supervisor_id="original-owner"))
        result["gate"] = gate
        ready.set()
        assert proceed.wait(5)
        try:
            if transition == "entry":
                gate.enter("original-sdk")
            else:
                gate.close("SKILL_SOURCE_CHANGED")
        except OSError:
            result["unknown"] = True

    thread = Thread(target=run)
    thread.start()
    original = journal.connect

    @contextmanager
    def lost_write():
        with original() as db:
            yield db
            if lost_at == "before-commit":
                raise OSError("synthetic commit outcome unavailable")
        raise OSError("synthetic commit acknowledgement lost")

    try:
        assert ready.wait(5)
        monkeypatch.setattr(journal, "connect", lost_write)
        proceed.set()
        thread.join(timeout=5)
        assert not thread.is_alive() and result["unknown"]
        monkeypatch.setattr(journal, "connect", original)
        with pytest.raises(Problem) as denied:
            result["gate"].observe_closed()
        assert denied.value.body["error"]["code"] == "SDK_START_GATE_UNKNOWN"
        with pytest.raises(Problem) as replay:
            SdkStartGate(journal, binding(), SdkStartOwner(kind="local-offline", supervisor_id="original-owner"))
        assert replay.value.body["error"]["code"] == "SDK_START_GATE_REPLAY"
    finally:
        proceed.set()
        thread.join(timeout=5)


@pytest.mark.parametrize("field,value", [
    ("schema_version", "fa-sdk-start-gate-record-future"),
    ("owner", {"kind": "local-offline", "supervisor_id": "other-owner"}),
])
def test_unknown_or_rebound_closed_journal_record_is_not_an_attestation(tmp_path, field, value):
    journal = LocalJournal(tmp_path)
    result = {}

    def run():
        gate = SdkStartGate(journal, binding(), SdkStartOwner(kind="local-offline", supervisor_id="original-owner"))
        result["gate"] = gate
        gate.close("SKILL_SOURCE_CHANGED")

    thread = Thread(target=run)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    with journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_sdk_start_gates").fetchone()
        document = json.loads(raw)
        document[field] = value
        db.execute("UPDATE assistant_sdk_start_gates SET body=?", (json.dumps(document),))
    with pytest.raises(Problem) as denied:
        result["gate"].observe_closed()
    assert denied.value.body["error"]["code"] in {"SDK_START_GATE_INVALID", "SDK_START_GATE_FENCED"}


def test_changed_installed_code_is_logged_with_its_exact_gate_code(tmp_path, caplog):
    from types import SimpleNamespace
    from shared_context.behavior_assistant_hosted import AssistantSupervisor
    supervisor = AssistantSupervisor(tmp_path, runtime_path=tmp_path / "never-started")
    changed = binding().model_copy(update={"code_sha256": "6" * 64})
    client = SimpleNamespace(assignment=SimpleNamespace(sdk_start_binding=changed, task=SimpleNamespace(
        instruction=SimpleNamespace(sdk_start_profile="fa-sdk-start-gate-v1"))))
    assignment = SimpleNamespace(launch_id="original-launch", claim_id="original-claim", process_generation=1)
    caplog.set_level("WARNING")
    supervisor._execute(assignment, client)
    assert "phase=create_session code=SDK_START_GATE_CODE_CHANGED " in caplog.text
    assert "UNCLASSIFIED" not in caplog.text
    with supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id='original-launch'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='assistant_sdk_start_gates'").fetchone() is None
    assert json.loads(raw)["code"] == "SDK_START_GATE_CODE_CHANGED"
    assert json.loads(raw)["phase"] == "create_session"


@pytest.mark.parametrize("code", sorted(SDK_START_GATE_ERRORS))
def test_every_start_gate_refusal_keeps_only_its_code(tmp_path, caplog, code):
    from shared_context.behavior_assistant_hosted import AssistantSupervisor
    supervisor = AssistantSupervisor(tmp_path, runtime_path=tmp_path / "never-started")
    private = "SYNTHETIC_PRIVATE_GATE_CANARY"
    assert supervisor.record_failure("original-launch", "create_session", Problem(403, code, private))
    with supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id='original-launch'").fetchone()
    assert json.loads(raw)["code"] == code and "code=" + code + " " in caplog.text
    assert private not in raw and private not in caplog.text
