"""A real registered worker start becomes an SDK session in the original VM begin CAS."""

from dataclasses import replace
from functools import partial
import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from conftest import InMemoryRepository
from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_agent_control import AgentControlRelay
from shared_context.behavior_agent_worker import AgentTurnAddress, AgentVmBinding
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_code_identity import code_sha256
from shared_context.behavior_assistant import key
from shared_context.behavior_operations import _row
from shared_context.behavior_sdk_checkpoint import SaveSdkState, save_state, load_state
from shared_context.behavior_sdk_state import SdkState
from shared_context.behavior_turn_receipts import TurnReceiptStore, ticket_handle, validate_sdk_checkpoint
from shared_context.behavior_turn_wire import BeginTurn, TurnTicket
from shared_context.behavior_vm_assignment import prepare_vm_assignment
from shared_context.behavior_vm_session import read_vm_session
from shared_context.behavior_workers import JobRecord, worker_key
from shared_context.errors import Problem
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.team_data_relay import TeamDataRelay
from test_conversation_transfer_startup import read_startup, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def vm_turn(synthetic_standing_v1):
    c = synthetic_standing_v1
    vm = AgentVmBinding(team_id=c.human.team, agent_id=c.native.targets[0].agent_id,
                        hosted_session_id="agent-vm-one", hosted_agent_version="41", vm_generation="generation-one")
    command = aw.InstructionSubmit(request_id="vm-session-bridge", content="OFFLINE original registered turn")
    binding, _ = c.control.submit(c.human, command, agent_vm=vm)
    workers = c.h.runtime.dep.workers
    starts = []
    original = workers.adapter
    workers.adapter = replace(original, start=starts.append)
    try:
        workers.start(binding.job_id, binding.owner)
    finally:
        workers.adapter = original
    start, = starts
    jkey = worker_key("job", binding.job_id)
    job = _row(c.control.store.read_snapshot((jkey,)), jkey, JobRecord)
    record = c.control.records[binding.binding_id]
    assignment = prepare_vm_assignment(record, job, start,
        worker_configuration_digest=workers.configuration_digest(), installed_code_sha256=code_sha256())
    ticket = TurnTicket(tenant_id=binding.tenant_id, vm=vm, address=AgentTurnAddress(
        conversation_id=assignment.task.instruction.conversation_id, sdk_session_id=assignment.sdk_session_id,
        request_id=record.request_id), assignment=assignment, owner_oid=binding.human_oid,
        agent_oid=binding.agent_oid, issuer_oid=c.human.oid, source="human", request_sha256=record.submit_sha256)
    actor = c.h.config.identity.auth().actors[binding.agent_oid]
    agent = BehaviorPrincipal(binding.tenant_id, actor.oid, actor.team, "agent", actor.actor,
                              frozenset({"read", "execute", "submit", "consume"}))
    control = AgentControlRelay(wait_seconds=0)
    context = control.connect(vm, "original-process", agent.oid)
    receipts = TurnReceiptStore(c.control.store,
        TeamDataRelay(InMemoryRepository(), InMemoryArtifactStore(binding.tenant_id), binding.tenant_id),
        validate_checkpoint=partial(validate_sdk_checkpoint, c.authority), assistant=c.authority)
    delivery = receipts.issue(ticket, c.human, expected_vm=vm)
    try:
        yield SimpleNamespace(c=c, binding=binding, start=start, record=record, ticket=ticket,
                              agent=agent, context=context, receipts=receipts, delivery=delivery)
    finally:
        control.close()


def begin(turn):
    return turn.receipts.begin(turn.agent, turn.context,
        BeginTurn(address=turn.ticket.address, ticket=ticket_handle(turn.ticket)))


def session(turn):
    return read_vm_session(turn.c.authority, turn.agent, turn.context, ticket_handle(turn.ticket))


def test_begin_creates_the_real_session_in_its_single_cas_without_a_second_ticket(vm_turn,caplog):
    from shared_context.behavior_timing import phase_timing
    turn=vm_turn
    c=turn.c
    skey=key("session",turn.ticket.assignment.session_id)
    assert c.control.store.read_record(skey) is None
    with phase_timing("vm_begin_with_sdk"):
        started=begin(turn)
    with phase_timing("vm_sdk_session_read"):
        saved=session(turn)
    measured=[json.loads(entry.getMessage().removeprefix("FA_TIMING ")) for entry in caplog.records
              if entry.getMessage().startswith("FA_TIMING ")]
    print(json.dumps({entry["phase"]:{"gets":entry["table_gets"],"rounds":entry["batches"]}
                      for entry in measured if entry["phase"].startswith("vm_")},sort_keys=True))
    assert saved.assignment==started.ticket.assignment
    assert saved.execution==turn.start.binding and saved.state=="running"
    assert saved.vm_turn_ticket_id==started.start.ticket.ticket_id and saved.ticket_digest is None
    with pytest.raises(Problem):
        c.authority.current_session(saved.binding_id,saved.assignment.session_id,
                                    SecretStr(started.start.ticket.ticket_sha256),turn.agent)
    with pytest.raises(Problem) as replay:
        begin(turn)
    assert replay.value.status==409
    read_startup(c)


def test_original_sdk_save_and_restore_helpers_use_the_new_session_without_bearer(vm_turn):
    turn=vm_turn
    begin(turn)
    saved=session(turn)
    sid=saved.assignment.sdk_session_id
    state=SdkState(sdk_session_id=sid, files={
        "events.jsonl": json.dumps({"type":"session.start","data":{"sessionId":sid}})+"\n",
        "workspace.yaml":"id: "+sid+"\n",
    })
    stop=aw.SdkStop(pid=211,exit_code=0,graceful=True)
    reference=save_state(turn.c.authority,saved,SaveSdkState(
        request_id="sdk-state",state=state,sdk_stop=stop))
    assert load_state(turn.c.authority,saved,reference,job_id=turn.binding.job_id)==state
    reply=aw.SaveTextReply(request_id="save-text-reply",reply=aw.TextReply(
        text="OFFLINE complete answer",sdk_session_id=sid,
        conversation_id=turn.ticket.address.conversation_id,turn_number=1),
        archive=reference,sdk_stop=stop)
    result=turn.c.authority.save_text_reply(saved,reply,turn.agent)
    assert result.state=="ready_to_finalize"
    assert session(turn).state=="ready_to_finalize"
    read_startup(turn.c)


def test_changed_process_or_execution_cannot_borrow_the_vm_sdk_session(vm_turn):
    turn=vm_turn
    begin(turn)
    with pytest.raises(Problem):
        read_vm_session(turn.c.authority,turn.agent,
                        turn.context.model_copy(update={"process_id":"another-process"}),ticket_handle(turn.ticket))
    original=session(turn)
    jkey=worker_key("job",turn.binding.job_id)
    row=turn.c.control.store.read_snapshot((jkey,))[jkey]
    from shared_context.behavior_store import ControlRequest,ControlChanges
    from shared_context.behavior_operations import _updated
    from shared_context.source_materials import digest
    job=_row({jkey:row},jkey,JobRecord)
    turn.c.control.store.transact(ControlRequest(request_id="offline-cancel",request_digest=digest("cancel"),
        read_set=(jkey,)),lambda rows:ControlChanges(writes=(
            turn.c.h.runtime.dep.workers._write(jkey,_updated(job,cancel_requested=True),rows),),
            result={"cancelled":True}))
    with pytest.raises(Problem) as denied:
        turn.c.authority._gate(original,turn.c.control.store.read_snapshot(turn.c.authority._keys(original)))
    assert denied.value.body["error"]["code"]=="ASSISTANT_EXECUTION_FENCED"


def test_cancelled_vm_turn_can_still_save_its_stopped_sdk_checkpoint(vm_turn):
    turn=vm_turn
    begin(turn)
    saved=session(turn)
    state=SdkState(sdk_session_id=saved.assignment.sdk_session_id,files={
        "events.jsonl":json.dumps({"type":"session.start","data":{"sessionId":saved.assignment.sdk_session_id}})+"\n",
        "workspace.yaml":"id: "+saved.assignment.sdk_session_id+"\n"})
    turn.receipts.cancel(turn.agent,turn.context,turn.ticket.address)
    jkey=worker_key("job",turn.binding.job_id)
    assert _row(turn.c.control.store.read_snapshot((jkey,)),jkey,JobRecord).cancel_requested is True
    stop=aw.SdkStop(pid=211,exit_code=0,graceful=True)
    reference=save_state(turn.c.authority,saved,SaveSdkState(request_id="sdk-state",state=state,sdk_stop=stop))
    result=turn.c.authority.save_text_reply(saved,aw.SaveTextReply(
        request_id="save-text-reply",reply=None,archive=reference,sdk_stop=stop,failure="cancelled"),turn.agent)
    assert result.work_outcome=="cancelled"
    assert load_state(turn.c.authority,saved,reference,job_id=turn.binding.job_id)==state
    read_startup(turn.c)


def test_vm_begin_replay_cannot_create_another_session_after_lost_ack(vm_turn,monkeypatch):
    turn=vm_turn
    store=turn.c.control.store
    submit=store._submit
    def lost(operations,request):
        submit(operations,request)
        raise Problem(503,"BEHAVIOR_COMMIT_UNKNOWN","OFFLINE lost begin acknowledgement")
    monkeypatch.setattr(store,"_submit",lost)
    with pytest.raises(Problem) as failure:
        begin(turn)
    assert failure.value.body["error"]["code"]=="BEHAVIOR_COMMIT_UNKNOWN"
    monkeypatch.setattr(store,"_submit",submit)
    first=session(turn)
    with pytest.raises(Problem):
        begin(turn)
    assert session(turn)==first


def test_vm_reader_reuses_only_original_start_identity_and_reads_live_session_once(vm_turn,monkeypatch):
    turn=vm_turn
    begin(turn)
    authority=turn.c.authority
    reads=[]
    original=authority.store._read_physical
    def read(row,model):
        reads.append(row)
        return original(row,model)
    monkeypatch.setattr(authority.store,"_read_physical",read)
    current=session(turn)
    assert reads==["behavior:v1:row:"+key("session",current.assignment.session_id)]
    authority._vm_starts.clear()
    rebuilt=session(turn)
    assert rebuilt==current
    reads.clear()
    assert session(turn)==current
    assert reads==["behavior:v1:row:"+key("session",current.assignment.session_id)]


@pytest.mark.parametrize("revoked", [False, True])
def test_vm_model_reservation_reads_the_existing_budget_inside_its_original_cas(vm_turn,revoked):
    from shared_context.behavior_operations import control_key
    from shared_context.source_materials import digest
    turn=vm_turn
    begin(turn)
    saved=session(turn)
    if revoked:
        turn.c.control.revoke(turn.c.human,"revoke-after-vm-admission")
    body={"model":saved.assignment.task.instruction.model,"stream":True,
          "messages":[{"role":"user","content":"OFFLINE reserve only; no provider call"}]}
    identifier=digest(body)
    budget_key=control_key("budget",saved.execution.owner.root_budget_id)
    before=turn.c.control.store.read_record(budget_key)
    assert before is not None
    command=aw.Reservation(request_id=identifier,request_sha256=identifier)
    result=turn.c.authority.reserve_model(saved,command,turn.agent)
    assert result.state=="accepted"
    assert turn.c.control.store.read_record(budget_key)==before
    intent_key=key("model-intent",saved.assignment.session_id,identifier)
    assert turn.c.control.store.read_record(intent_key).payload["data"]["consumed"] is False
    turn.c.authority.consume_model(saved,aw.ModelRequest(request_id=identifier,body=body),turn.agent)
    assert turn.c.control.store.read_record(intent_key).payload["data"]["consumed"] is True
    with pytest.raises(Problem):
        turn.c.authority.consume_model(saved,aw.ModelRequest(request_id=identifier,body=body),turn.agent)
    if revoked:
        with pytest.raises(Problem):
            turn.c.control.submit(turn.c.human,aw.InstructionSubmit(
                request_id="next-vm-admission-after-revoke",content="OFFLINE refused new work"))
    read_startup(turn.c)
