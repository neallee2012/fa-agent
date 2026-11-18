"""The current turn, not the lifetime of the shared queue thread, owns VM SDK entry."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event

import pytest
from pydantic import ValidationError

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant_hosted import AssistantSession
from shared_context.behavior_hosted import LocalJournal
from shared_context.behavior_sdk_start import SdkStartGate
from shared_context.errors import Problem
from test_conversation_transfer_startup import synthetic_standing_v1
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_sdk_start_gate import binding
from shared_context.behavior_agent_worker import AgentVmBinding


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


def test_existing_dispatch_finalizes_the_turn_without_claiming_the_queue_thread_ended(tmp_path):
    with ThreadPoolExecutor(max_workers=1) as queue:
        def run():
            session = object.__new__(AssistantSession)
            session._sdk_start_gate = SdkStartGate(LocalJournal(tmp_path), binding(), owner())
            session._acquired = True
            session._dispatch = lambda _: (_ for _ in ()).throw(
                Problem(403, "SKILL_SOURCE_CHANGED", "OFFLINE refusal before SDK entry"))
            with pytest.raises(Problem):
                session.dispatch({"action": "start"})
            return session._sdk_start_gate
        gate = queue.submit(run).result(timeout=5)
        assert gate.thread.is_alive()
        assert gate.observe_turn_closed().gate.state == "closed_before_entry"


def test_new_conversations_capture_one_agent_vm_before_their_first_write(synthetic_standing_v1):
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.behavior_instruction import Conversation, conversation_key
    from shared_context.behavior_operations import _row
    from legacy_turn_rows_helpers import read_startup

    c = synthetic_standing_v1
    target = c.native.targets[0]
    vm = AgentVmBinding(team_id=c.human.team, agent_id=target.agent_id,
                        hosted_session_id="server-agent-session", hosted_agent_version="41", vm_generation="generation-one")
    entries=[]
    for rid in ("vm-cid-one", "vm-cid-two"):
        command=InstructionSubmit(request_id=rid, content="OFFLINE new VM conversation")
        binding,_=c.control.submit(c.human,command,agent_vm=vm)
        saved=c.control.records[binding.binding_id]
        assert saved.agent_vm==vm and saved.hosted_session_id==vm.hosted_session_id
        ckey=conversation_key(saved.profile.instruction.conversation_id)
        conversation=_row(c.control.store.read_snapshot((ckey,)),ckey,Conversation)
        assert conversation.agent_vm==vm and conversation.hosted_session_id==vm.hosted_session_id
        entries.append(conversation)
        original=c.control.store.read_record(ckey)
        replay,_=c.control.submit(c.human,command,agent_vm=vm)
        assert replay==binding and c.control.store.read_record(ckey)==original
        with pytest.raises(Problem):
            c.control.submit(c.human,command,agent_vm=vm.model_copy(update={"hosted_session_id":"different"}))
        assert c.control.store.read_record(ckey)==original
    assert entries[0].conversation_id!=entries[1].conversation_id
    assert entries[0].sdk_session_id!=entries[1].sdk_session_id
    c.control.refresh()
    read_startup(c)


def test_new_vm_capture_rejects_another_team_or_agent_before_admission_writes(synthetic_standing_v1):
    from shared_context.behavior_assistant_wire import InstructionSubmit
    c=synthetic_standing_v1
    vm=AgentVmBinding(team_id=c.human.team,agent_id=c.native.targets[0].agent_id,
                      hosted_session_id="server-agent-session",hosted_agent_version="41",vm_generation="generation-one")
    for field,value in (("team_id","another-team"),("agent_id","another-agent")):
        before=c.control.store._guard()
        with pytest.raises(Problem) as denied:
            c.control.submit(c.human,InstructionSubmit(request_id="denied-"+field,content="OFFLINE"),
                             agent_vm=vm.model_copy(update={field:value}))
        assert denied.value.body["error"]["code"]=="CONVERSATION_VM_BINDING_DENIED"
        assert c.control.store._guard()==before
