"""The current turn, not the lifetime of the shared queue thread, owns VM SDK entry."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event

import pytest
from pydantic import ValidationError

from shared_context import behavior_wire as wire
from shared_context.behavior_hosted import LocalJournal
from shared_context.behavior_sdk_start import SdkStartGate
from shared_context.errors import Problem
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_sdk_start_gate import binding


def owner():
    return wire.SdkVmStartOwner(supervisor_id="real-worker-process", ticket_id="a" * 64,
                                start_receipt_sha256="b" * 64)


def test_a_completed_vm_turn_can_prove_closed_entry_while_its_queue_thread_stays_alive(tmp_path):
    journal = LocalJournal(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as queue:
        def run():
            gate = SdkStartGate(journal, binding(), owner())
            gate.close("SKILL_SOURCE_CHANGED")
            assert gate.observe_turn_closed() is None
            gate.finish_turn()
            return gate
        gate = queue.submit(run).result(timeout=5)
        assert gate.thread.is_alive()
        observation = gate.observe_turn_closed()
        assert observation.owned_turn_completed is True
        assert observation.gate.owner.kind == "vm-turn"
        assert "launch_id" not in observation.gate.owner.model_dump()
        with pytest.raises(Problem):
            gate.observe_closed()
        with pytest.raises(ValidationError):
            wire.SdkNoStart(schema_version="fa-sdk-no-start-v1", gate=observation.gate,
                            owned_thread_completed=True)
        with pytest.raises(Problem):
            queue.submit(gate.enter, binding().sdk_session_id).result(timeout=5)


def test_only_the_owned_dispatch_can_finish_the_vm_turn(tmp_path):
    release, ready = Event(), Event()
    captured = {}
    with ThreadPoolExecutor(max_workers=1) as queue:
        def run():
            captured["gate"] = SdkStartGate(LocalJournal(tmp_path), binding(), owner())
            ready.set()
            assert release.wait(5)
            captured["gate"].finish_turn()
        future = queue.submit(run)
        try:
            assert ready.wait(5)
            with pytest.raises(Problem) as outsider:
                captured["gate"].finish_turn()
            assert outsider.value.body["error"]["code"] == "SDK_START_GATE_FENCED"
        finally:
            release.set()
            future.result(timeout=5)


def test_an_attempted_sdk_is_not_a_no_sdk_outcome(tmp_path):
    gate = SdkStartGate(LocalJournal(tmp_path), binding(), owner())
    gate.enter(binding().sdk_session_id)
    gate.finish_turn()
    assert gate.observe_turn_closed() is None


def test_unknown_vm_gate_write_stays_unknown_after_turn_completion(tmp_path, monkeypatch):
    journal = LocalJournal(tmp_path)
    gate = SdkStartGate(journal, binding(), owner())
    connect = journal.connect

    @contextmanager
    def lose_ack():
        with connect() as db:
            yield db
        raise OSError("OFFLINE lost private-journal acknowledgement")

    monkeypatch.setattr(journal, "connect", lose_ack)
    with pytest.raises(OSError):
        gate.close("SKILL_SOURCE_CHANGED")
    gate.finish_turn()
    monkeypatch.setattr(journal, "connect", connect)
    with pytest.raises(Problem) as unknown:
        gate.observe_turn_closed()
    assert unknown.value.body["error"]["code"] == "SDK_START_GATE_UNKNOWN"

