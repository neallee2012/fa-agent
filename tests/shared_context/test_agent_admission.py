"""The caller's CAS admits a queued Agent work; only the original WorkerCore later starts it."""

from dataclasses import replace
from datetime import timedelta
from functools import partial
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from conftest import InMemoryRepository
from shared_context.behavior_agent_admission import (
    agent_admission_anchors, agent_admission_keys, prepare_agent_admission, ticket_for_agent_admission,
)
from shared_context.behavior_agent_control import AgentControlRelay
from shared_context.behavior_agent_message_wire import PeerTeamReference
from shared_context.behavior_agent_worker import AgentVmBinding
from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_code_identity import code_sha256
from shared_context.behavior_instruction_cost import record_turn_usage
from shared_context.behavior_sdk_checkpoint import SaveSdkState, save_state
from shared_context.behavior_sdk_state import SdkState
from shared_context.behavior_operations import _row, control_key
from shared_context.behavior_store import ControlChanges, ControlRequest
from shared_context.behavior_turn_origin import instruction_turn_origin
from shared_context.behavior_turn_receipts import TurnReceiptStore, start_key, ticket_handle, validate_sdk_checkpoint
from shared_context.behavior_turn_usage import TurnModelUsage
from shared_context.behavior_turn_wire import BeginTurn, TurnEnd, TurnRelayContext
from shared_context.behavior_vm_assignment import prepare_vm_assignment
from shared_context.behavior_vm_session import read_vm_session
from shared_context.behavior_workers import JobRecord, worker_key
from shared_context.behavior_work_policy import WorkRegistration, work_policy_key
from shared_context.behavior_work_policy import registration_receipt_id
from shared_context.errors import Problem
from shared_context.records import Record
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.source_materials import digest
from shared_context.team_data_relay import TeamDataRelay
from test_conversation_transfer_startup import read_startup, saved_turn, synthetic_standing_v1
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_inventory, instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_turn_receipts import ticket_for


@pytest.fixture
def admission_case(synthetic_standing_v1):
    c = synthetic_standing_v1
    ids = c.h.config.identity
    sender_actor = ids.auth().actors[ids.agents[1].oid]
    sender_vm = AgentVmBinding(team_id=ids.team_id, agent_id=c.h.config.targets[1].agent_id,
                               hosted_session_id="original-sender-vm", hosted_agent_version="41",
                               vm_generation="sender-generation")
    sender_principal = BehaviorPrincipal(ids.tenant_id, sender_actor.oid, sender_actor.team, "agent",
                                        sender_actor.actor, frozenset({"submit", "read", "execute"}))
    sender_ticket = ticket_for(sender_vm, rid="sender-turn", oid=sender_actor.oid,
                               issuer=ids.human_oid, owner=ids.human_oid, tenant_id=ids.tenant_id)
    relay = TeamDataRelay(InMemoryRepository(), InMemoryArtifactStore(ids.tenant_id), ids.tenant_id)
    receipts = TurnReceiptStore(c.control.store, relay, validate_checkpoint=lambda *_: None, clock=lambda: c.h.time)
    control = AgentControlRelay(wait_seconds=0)
    sender_context = control.connect(sender_vm, "sender-process", sender_actor.oid)
    receipts.issue(sender_ticket, c.human, expected_vm=sender_vm)
    sender = receipts.begin(sender_principal, sender_context, BeginTurn(
        address=sender_ticket.address, ticket=ticket_handle(sender_ticket)))
    recipient_vm = AgentVmBinding(team_id=ids.team_id, agent_id=c.native.targets[0].agent_id,
                                  hosted_session_id="original-receiver-vm", hosted_agent_version="41",
                                  vm_generation="receiver-generation")
    command = InstructionSubmit(request_id="message-one", content="OFFLINE original Agent brief", model="gpt-4.1-mini")
    source = TurnRelayContext(root=ticket_handle(sender_ticket).ticket_id, hop=1,
                              chain=(sender_vm.agent_id,), deadline=c.h.time + timedelta(minutes=5))
    try:
        yield SimpleNamespace(c=c, principal=sender_principal, sender=sender, vm=recipient_vm,
                              command=command, source=source, relay=relay)
    finally:
        control.close()


def prepared_call(case, *, command=None, principal=None, refs=(), relay=None):
    c = case.c
    command = command or case.command
    anchors = c.control.store.read_snapshot(agent_admission_anchors(c.control, command, case.sender))
    reads = agent_admission_keys(c.control, command, case.sender, anchors)
    request = ControlRequest(
        request_id=control_key("offline-inbox-admission", command.request_id),
        request_digest=digest([command.model_dump(mode="json"), [ref.model_dump(mode="json") for ref in refs]]),
        read_set=reads,
    )
    prepare = partial(prepare_agent_admission, c.control, principal=principal or case.principal,
                      sender=case.sender, command=command, recipient_vm=case.vm, relay=relay or case.source,
                      message_id="original-message", model=c.control.models.select(command.model),
                      observation=c.control.cost_reader(), request=request, now=c.h.time, refs=refs)
    return request, prepare


def admit(case, **kwargs):
    request, prepare = prepared_call(case, **kwargs)
    ready = []

    def changes(rows):
        result = prepare(rows)
        ready.append(result)
        return ControlChanges(writes=result.writes,
                              result={"binding_id": result.registration.binding.binding_id})

    receipt = case.c.control.store.transact(request, changes)
    return receipt, ready[0] if ready else None


def test_pure_builder_does_not_write_read_or_publish_ram_bindings(admission_case, monkeypatch):
    c = admission_case.c
    request, prepare = prepared_call(admission_case)
    rows = c.control.store.read_snapshot(request.read_set)
    records, bindings = dict(c.control.records), dict(c.h.runtime.bindings)
    monkeypatch.setattr(c.control.store, "_read_physical", lambda *_: pytest.fail("the decision must not read"))
    monkeypatch.setattr(c.control.store, "_submit", lambda *_: pytest.fail("the decision must not write"))
    result = prepare(rows)
    assert result.registration.principal_oid == admission_case.principal.oid != c.human.oid
    assert result.job.state == "queued" and result.job.binding is None
    assert result.job.spec.obligation_id is None and result.job.spec.parent_job_id is None
    assert result.read_set == request.read_set
    assert c.control.records == records and dict(c.h.runtime.bindings) == bindings
    assert result.registration.profile.instruction.peer_work is None
    assert "original agent message" in result.registration.binding.work.goal


def test_admission_is_one_outer_cas_and_replay_does_not_readmit(admission_case, caplog):
    from shared_context.behavior_timing import phase_timing
    c = admission_case.c
    before = c.control.store._guard()[0].revision
    request, prepare = prepared_call(admission_case)
    ready = []

    def changes(rows):
        result = prepare(rows)
        ready.append(result)
        return ControlChanges(writes=result.writes, result={"binding_id": result.registration.binding.binding_id})

    with phase_timing("agent-admission-one-cas"):
        receipt = c.control.store.transact(request, changes)
    prepared, = ready
    observed = [json.loads(value.getMessage().removeprefix("FA_TIMING ")) for value in caplog.records
                if value.getMessage().startswith("FA_TIMING ")]
    counts = next(value for value in observed if value["phase"] == "agent-admission-one-cas")
    print(json.dumps({"phase": counts["phase"], "gets": counts["table_gets"], "rounds": counts["batches"]}))
    record = prepared.registration
    assert registration_receipt_id(record) == request.request_id
    assert c.control.store._guard()[0].revision == before + 1
    assert record.binding.binding_id not in c.control.records
    assert c.control.store.read_record(control_key("delivery-obligation", record.binding.obligation_id)) is None
    request = ControlRequest.model_validate_json(json.dumps(receipt.payload["request"]))
    assert c.control.store.transact(request, lambda _: pytest.fail("an original receipt must not admit again")) == receipt
    c.control.refresh()
    assert c.control.records[record.binding.binding_id] == record
    assert _row(c.control.store.read_snapshot((worker_key("job", record.binding.job_id),)),
                worker_key("job", record.binding.job_id), JobRecord).binding is None
    read_startup(c)


def test_revoked_recipient_policy_cannot_accept_new_agent_work(admission_case):
    case = admission_case
    case.c.control.revoke(case.c.human, "revoke-before-agent-admission")
    request, prepare = prepared_call(case)
    before = case.c.control.store._guard()
    with pytest.raises(Problem) as denied:
        case.c.control.store.transact(request, lambda rows: ControlChanges(writes=prepare(rows).writes, result={}))
    assert denied.value.status == 403
    assert case.c.control.store._guard() == before


def test_only_real_worker_start_can_prepare_the_assignment_and_ticket(admission_case):
    case = admission_case
    c = case.c
    _, prepared = admit(case)
    record = prepared.registration
    c.control.refresh()
    workers = c.h.runtime.dep.workers
    starts = []
    original = workers.adapter
    workers.adapter = replace(original, start=starts.append)
    try:
        workers.start(record.binding.job_id, record.binding.owner)
    finally:
        workers.adapter = original
    start, = starts
    jkey = worker_key("job", record.binding.job_id)
    job = _row(c.control.store.read_snapshot((jkey,)), jkey, JobRecord)
    assert job.binding == start.binding and job.state == "starting"
    assignment = prepare_vm_assignment(record, job, start, worker_configuration_digest=workers.configuration_digest(),
                                       installed_code_sha256=code_sha256())
    ticket = ticket_for_agent_admission(record, assignment)
    assert ticket.source == "agent" and ticket.issuer_oid == case.principal.oid
    assert ticket.address.request_id == case.command.request_id and ticket.relay == case.source
    assert ticket.model_call_limit == 100
    actor = c.h.config.identity.auth().actors[record.binding.agent_oid]
    agent = BehaviorPrincipal(record.binding.tenant_id, actor.oid, actor.team, "agent", actor.actor,
                              frozenset({"read", "submit", "execute"}))
    control = AgentControlRelay(wait_seconds=0)
    context = control.connect(case.vm, "receiver-process", actor.oid)
    receipts = TurnReceiptStore(c.control.store, case.relay, assistant=c.authority,
                               validate_checkpoint=partial(validate_sdk_checkpoint, c.authority),
                               record_usage=partial(record_turn_usage, c.control), clock=lambda: c.h.time)
    try:
        receipts.issue(ticket, case.principal, expected_vm=case.vm)
        started = receipts.begin(agent, context, BeginTurn(address=ticket.address, ticket=ticket_handle(ticket)))
        session = read_vm_session(c.authority, agent, context, ticket_handle(ticket))
        assert session.ticket_digest is None and session.assignment == assignment
        origin = instruction_turn_origin(assignment.task, record.binding, record)
        assert origin.source_kind == "agent" and origin.actor_role == "sending-agent"
        state = SdkState(sdk_session_id=assignment.sdk_session_id, files={
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": assignment.sdk_session_id}}) + "\n",
            "workspace.yaml": "id: " + assignment.sdk_session_id + "\n",
        })
        stop = aw.SdkStop(pid=71, exit_code=0, graceful=True)
        checkpoint = save_state(c.authority, session, SaveSdkState(request_id="sdk-state", state=state, sdk_stop=stop))
        saved = c.authority.save_text_reply(session, aw.SaveTextReply(
            request_id="save-text-reply", reply=None, archive=checkpoint, sdk_stop=stop,
            failure="model_call_limit_reached"), agent)
        assert saved.work_outcome == "failed"
        end = TurnEnd(ticket=ticket_handle(ticket), address=ticket.address, start_sha256=started.start.sha256,
                      outcome="failed", result=None, checkpoint=checkpoint, sdk_stop=stop,
                      model_usage=TurnModelUsage(schema_version="fa-turn-model-usage-v1", usage=()))
        receipts.complete(agent, context, end)
        receipts.consume_completion(agent, context, ticket.address)
        read_startup(c)
    finally:
        control.close()


@pytest.mark.parametrize("fault", ["other-principal", "other-team", "human", "wrong-root", "expired"])
def test_unverified_or_expanded_source_cannot_admit_any_job(admission_case, fault):
    case = admission_case
    principal, relay = case.principal, case.source
    if fault == "other-principal":
        principal = replace(principal, oid=case.c.human.oid)
    elif fault == "other-team":
        principal = replace(principal, team="another-team")
    elif fault == "human":
        principal = case.c.human
    elif fault == "wrong-root":
        relay = relay.model_copy(update={"root": "different-root"})
    else:
        relay = relay.model_copy(update={"deadline": case.c.h.time})
    request, prepare = prepared_call(case, principal=principal, relay=relay)
    guard = case.c.control.store._guard()
    with pytest.raises(Problem):
        case.c.control.store.transact(request, lambda rows: ControlChanges(writes=prepare(rows).writes, result={}))
    assert case.c.control.store._guard() == guard


def test_reference_data_is_retained_but_never_crosses_team(admission_case):
    case = admission_case
    ref = PeerTeamReference(resource_kind="artifact", tenant_id=case.principal.tenant_id, team_id=case.principal.team,
                            resource_id="original-result", version="a" * 32, sha256="b" * 64)
    _, prepared = admit(case, refs=(ref,))
    assert prepared.registration.agent_origin.refs == (ref,)
    other = ref.model_copy(update={"team_id": "another-team"})
    command = case.command.model_copy(update={"request_id": "other-ref"})
    guard = case.c.control.store._guard()
    with pytest.raises(Problem):
        admit(case, command=command, refs=(other,))
    assert case.c.control.store._guard() == guard


def test_busy_cid_leaves_the_message_unadmitted_without_borrowing_old_history(admission_case):
    case = admission_case
    _, first = admit(case)
    case.c.control.refresh()
    command = case.command.model_copy(update={
        "request_id": "another-message", "conversation_id": first.registration.profile.instruction.conversation_id})
    request, prepare = prepared_call(case, command=command)
    guard = case.c.control.store._guard()
    with pytest.raises(Problem) as pending:
        case.c.control.store.transact(request, lambda rows: ControlChanges(writes=prepare(rows).writes, result={}))
    assert pending.value.body["error"]["code"] == "AGENT_ADMISSION_NOT_READY"
    assert case.c.control.store._guard() == guard


def test_ready_cid_keeps_the_original_captured_prompt_and_checkpoint(admission_case, monkeypatch):
    case = admission_case
    original_submit = case.c.control.submit
    monkeypatch.setattr(case.c.control, "submit",
                        lambda *args, **kwargs: original_submit(*args, **kwargs, agent_vm=case.vm))
    prior = saved_turn(case.c, "human-turn-before-agent-message")
    previous = case.c.control.records[prior.binding.binding_id]
    command = case.command.model_copy(update={
        "request_id": "agent-continuation", "conversation_id": prior.assignment.task.instruction.conversation_id})
    _, prepared = admit(case, command=command)
    instruction = prepared.registration.profile.instruction
    assert instruction.turn_number == 2 and instruction.previous_archive_id == key("text-archive", prior.binding.job_id)
    assert prepared.registration.profile.prompt_profile == previous.profile.prompt_profile
    assert prepared.registration.agent_origin is not None and previous.agent_origin is None
    assert prepared.registration.binding.binding_id != previous.binding.binding_id
    case.c.control.refresh()
    read_startup(case.c)


@pytest.mark.parametrize("field,schema", [
    ("sop_copy_outcome", "fa-sop-copy-outcome-v1"),
    ("skill_draft_outcome", "fa-skill-draft-outcome-v1"),
])
def test_pending_prior_effect_is_not_a_ready_recipient_cid(admission_case, monkeypatch, field, schema):
    case = admission_case
    original_submit = case.c.control.submit
    monkeypatch.setattr(case.c.control, "submit",
                        lambda *args, **kwargs: original_submit(*args, **kwargs, agent_vm=case.vm))
    prior = saved_turn(case.c, "prior-saved-but-unresolved")
    command = case.command.model_copy(update={
        "request_id": "must-remain-in-inbox",
        "conversation_id": prior.assignment.task.instruction.conversation_id})
    request, prepare = prepared_call(case, command=command)
    rows = dict(case.c.control.store.read_snapshot(request.read_set))
    archive_key = key("text-archive", prior.binding.job_id)
    archive = rows[archive_key]
    rows[archive_key] = Record({**archive.payload, "data": {
        **archive.payload["data"], "failure": "sdk_failed",
        field: {"schema_version": schema, "receipt_id": "original-pending-effect",
                "intent_sha256": "a" * 64, "stage": "upload_unknown"},
    }}, archive.etag)
    job_key = worker_key("job", prior.binding.job_id)
    job = rows[job_key]
    rows[job_key] = Record({**job.payload, "data": {**job.payload["data"], "state": "failed"}}, job.etag)
    before = case.c.control.store._guard()
    with pytest.raises(Problem) as not_ready:
        prepare(rows)
    assert not_ready.value.body["error"]["code"] == "AGENT_ADMISSION_NOT_READY"
    assert case.c.control.store._guard() == before


def test_changed_input_under_same_request_keeps_the_original_receipt(admission_case):
    case = admission_case
    receipt, prepared = admit(case)
    changed = case.command.model_copy(update={"content": "Different bytes"})
    original = ControlRequest.model_validate_json(json.dumps(receipt.payload["request"]))
    changed_request = original.model_copy(update={"request_digest": digest([changed.model_dump(mode="json"), []])})
    with pytest.raises(Problem) as conflict:
        case.c.control.store.transact(changed_request, lambda _: pytest.fail("changed input must not reach admission"))
    assert conflict.value.body["error"]["code"] == "BEHAVIOR_REQUEST_CONFLICT"
    rkey = work_policy_key("registration", prepared.registration.admission_id)
    assert _row(case.c.control.store.read_snapshot((rkey,)), rkey, WorkRegistration) == prepared.registration


def test_registry_page_rollover_uses_the_same_atomic_admission(admission_case, monkeypatch):
    from shared_context import behavior_work_registry
    case = admission_case
    monkeypatch.setenv("FA_WORK_REGISTRY_WRITER", "true")
    monkeypatch.setattr(behavior_work_registry, "PAGE_SIZE", 1)
    admitted = []
    for rid in ("first-page-message", "second-page-message", "third-page-message"):
        _, result = admit(case, command=case.command.model_copy(update={"request_id": rid}))
        admitted.append(result.registration.admission_id)
    current = behavior_work_registry.read_registry(case.c.control.store, case.c.control.policy)
    assert current.entries == tuple(admitted)
    case.c.control.refresh()
    read_startup(case.c)


def test_lost_outer_ack_keeps_the_same_work_and_never_runs_the_builder_twice(admission_case, monkeypatch):
    case = admission_case
    request, prepare = prepared_call(case)
    store = case.c.control.store
    original = store._submit
    ready = []

    def decide(rows):
        result = prepare(rows)
        ready.append(result)
        return ControlChanges(writes=result.writes, result={"binding_id": result.registration.binding.binding_id})

    def lost(operations, command):
        original(operations, command)
        raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "OFFLINE lost outer receipt acknowledgement")

    monkeypatch.setattr(store, "_submit", lost)
    with pytest.raises(Problem) as unknown:
        store.transact(request, decide)
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    monkeypatch.setattr(store, "_submit", original)
    retained = store.reconcile(request)
    assert retained.payload["result"]["binding_id"] == ready[0].registration.binding.binding_id
    assert store.transact(request, lambda _: pytest.fail("never readmit an acknowledged or unknown work")) == retained
    case.c.control.refresh()
    assert len(ready) == 1
    read_startup(case.c)


@pytest.mark.parametrize("operation", ["draft", "copy"])
def test_draft_history_uses_the_registration_receipt_selector(assistant, signing_key, jwk_client, monkeypatch, operation):
    """Isolate receipt selection in the real readers; operation data comes from the existing signed API fixtures."""
    from shared_context.behavior_recovery_history import _Inventory
    from shared_context import behavior_work_policy
    from test_sdk_compaction_product import open_turn

    source_client = None
    if operation == "draft":
        from shared_context.behavior_skill_draft_source import validate_history
        from test_skill_draft import configured_draft, post_draft
        h, control, human = configured_draft(assistant)
        write = post_draft
    else:
        from shared_context.behavior_skill_copy_source import validate_history
        from test_sop_skill_copy import approved_sop, configured_copy, copy_scope, post_copy
        source_client, _, _, source = approved_sop(assistant, signing_key, jwk_client)
        h, control, human = configured_copy(assistant, copy_scope(source))
        write = post_copy
    turn = open_turn(h, control, human, "outer-receipt-" + operation)
    try:
        response = write(turn)
        assert response.status_code == 200, response.text
        record = control.records[turn.binding.binding_id]
        inventory = instruction_inventory(h)
        config = SimpleNamespace(table=SimpleNamespace(scope=h.store._scope), sha256="f" * 64,
                                 workers=h.workers.contract, delivery=h.delivery.contract)
        view = _Inventory(inventory, config)
        old_id = work_policy_key("registration", record.admission_id)
        outer_id = control_key("outer-inbox-admission", record.request_id)
        # Checked receipts are shared read-only across startup views; swap the key on this view's own copy.
        receipts = dict(view.receipts)
        receipts[outer_id] = receipts.pop(old_id)
        view.receipts = MappingProxyType(receipts)
        selected = []

        def select_receipt(actual):
            assert actual == record
            selected.append(actual)
            return outer_id

        monkeypatch.setattr(behavior_work_policy, "registration_receipt_id", select_receipt)
        result, _ = validate_history(view, record)
        assert result.stage == "saved" and selected == [record]
    finally:
        turn.client.close()
        if source_client is not None:
            source_client.close()
