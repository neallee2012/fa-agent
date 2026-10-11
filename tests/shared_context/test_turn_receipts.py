"""Real receipt core/API with local Table, Blob, token and SDK-execution fakes; no live calls."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial
import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import A, B, C, InMemoryRepository, TENANT
from shared_context.behavior_agent_control import AgentControlRelay, CONNECTION_HEADER
from shared_context.behavior_agent_worker import AgentTurnAddress, AgentVmBinding, AgentWorkerControl
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_assistant_wire import ArchiveUnavailable, Assignment, InstructionSubmit, InstructionTask, SdkStop, Task
from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier, BehaviorPrincipal
from shared_context.behavior_operations import control_key
from shared_context.behavior_store import BehaviorStore, ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_turn_api import PREFIX, install_turn_routes
from shared_context.behavior_turn_client import TurnReceiptClient, TurnReceiptConsumer
from shared_context.behavior_turn_receipts import (
    TurnReceiptStore, consumption_key, prepare_turn, start_key, ticket_handle, turn_key,
    validate_sdk_checkpoint, validate_turn_receipt_history,
)
from shared_context.behavior_turn_usage import TurnModelUsage
from shared_context.behavior_turn_wire import (
    BeginTurn, TurnEnd, TurnEndReceipt, TurnHandle, TurnObjectReference, TurnRelayContext, TurnTicket,
)
from shared_context.behavior_wire import ExactSourceRef
from shared_context.errors import Problem
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.source_materials import digest
from test_behavior_store import FakeTable, SCOPE
from test_conversation_transfer_startup import read_startup, saved_turn, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import CLIENT_ID, HUMAN_OID, jwk_client, signing_key, token
from shared_context.team_data_relay import TeamDataRelay

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
VERSION = "0123456789abcdef0123456789abcdef"


def ticket_for(vm, *, rid="rid-one", oid=A.oid, cid="conversation-one", issuer=HUMAN_OID, source="human",
               owner=HUMAN_OID, tenant_id=TENANT):
    instruction = InstructionTask(content="OFFLINE original instruction", conversation_id=cid, turn_number=1,
                                  hosted_agent_version=vm.hosted_agent_version, model="gpt-4.1-mini")
    task = Task(job_id="job-" + rid, work_id="work-" + rid, definition_digest="a" * 64, profile_digest="b" * 64,
                source=ExactSourceRef(source_id="source-one", version=1, sha256="c" * 64, byte_length=12),
                artifact_id="result-one", artifact_version=VERSION, operation_id="operation-one", instruction=instruction)
    address = AgentTurnAddress(conversation_id=cid, sdk_session_id=digest(cid), request_id=rid)
    return TurnTicket(
        tenant_id=tenant_id, vm=vm, address=address, owner_oid=owner, agent_oid=oid, issuer_oid=issuer,
        source=source, request_sha256=digest(["original-input", rid]),
        assignment=Assignment(binding_id="binding-" + rid, session_id=digest(["assignment", rid]),
                              sdk_session_id=address.sdk_session_id, task=task),
        relay=None if source == "human" else TurnRelayContext(
            root="relay-root", hop=1, chain=("sender",), deadline=NOW + timedelta(minutes=5)),
        model_call_limit=None if source == "human" else 100,
    )


@pytest.fixture
def case(auth, jwk_client, signing_key):
    table = FakeTable()
    store = BehaviorStore(table, SCOPE)
    store.initialize()
    relay = TeamDataRelay(InMemoryRepository(), InMemoryArtifactStore(TENANT), TENANT)
    receipts = TurnReceiptStore(store, relay, clock=lambda: NOW,
        validate_checkpoint=lambda *_: pytest.fail("this fixture did not publish a checkpoint"))
    vm = AgentVmBinding(team_id=A.team, agent_id="agent-a", hosted_session_id="agent-session",
                        hosted_agent_version="41", vm_generation="session-generation-one")
    control = AgentControlRelay(wait_seconds=0)
    context = control.connect(vm, "process-one", A.oid)
    human = BehaviorPrincipal(TENANT, HUMAN_OID, A.team, "human", None, frozenset({"read", "submit"}))
    agent = BehaviorPrincipal(TENANT, A.oid, A.team, "agent", A.actor, frozenset({"read", "submit", "execute"}))
    verifier = BehaviorJwtVerifier(auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, A.team), jwk_client)

    @contextmanager
    def connection_context(principal):
        with control._lock:
            principal.require(vm.team_id, "execute")
            yield control.peer(vm, caller_oid=principal.oid).context

    app = FastAPI()
    _install_error_handlers(app)
    install_turn_routes(app, receipts, verifier, connection_context)
    client = TestClient(app)
    headers = {
        "Authorization": "Bearer " + token(signing_key, {"oid": A.oid, "roles": ["Behavior.Agent"]}, remove=("scp",)),
        CONNECTION_HEADER: context.control_connection_id,
        "Content-Type": "application/json",
    }
    c = SimpleNamespace(table=table, store=store, relay=relay, receipts=receipts, vm=vm, control=control,
                        context=context, human=human, agent=agent, client=client, headers=headers)
    c.ticket = ticket_for(vm)
    c.delivery = receipts.issue(c.ticket, human, expected_vm=vm)
    c.handle = ticket_handle(c.ticket)
    yield c
    client.close()
    control.close()


def begin(c, *, context=None):
    return c.receipts.begin(c.agent, context or c.context, BeginTurn(address=c.ticket.address, ticket=c.handle))


def end_for(c, started, *, usage=None):
    data = b"OFFLINE saved result"
    receipt = c.relay.put_object(c.agent, "result", "result-one", VERSION, data, hashlib.sha256(data).hexdigest())
    return TurnEnd(
        ticket=c.handle, address=c.ticket.address, start_sha256=digest(started.start.model_dump(mode="json")),
        outcome="succeeded", result=TurnObjectReference(
            kind="result", object_id="result-one", version_id=VERSION, receipt=receipt),
        checkpoint=ArchiveUnavailable(error="SDK_STATE_INCOMPLETE"),
        sdk_stop=SdkStop(pid=17, exit_code=0, graceful=True), model_usage=usage,
    )


def test_pure_producer_composes_into_the_original_relay_transaction(case):
    c = case
    sender = replace(c.agent, oid=B.oid, actor_id=B.actor)
    ticket = ticket_for(c.vm, rid="peer-turn", issuer=B.oid, source="agent")
    prepared = prepare_turn(ticket, sender, expected_vm=c.vm)
    assert prepared.delivery.address == ticket.address
    request = ControlRequest(request_id="original-relay", request_digest=digest("original-relay"),
                             read_set=prepared.request.read_set)
    c.store.transact(request, lambda _: ControlChanges(
        writes=(*prepared.writes, ControlWrite(row_id="original-message", data={"kind": "offline-message"}, mode="create")),
        result={"receiver_rid": ticket.address.request_id}))
    handle = TurnHandle.model_validate_json(prepared.delivery.payload.get_secret_value())
    started = c.receipts.begin(c.agent, c.context, BeginTurn(address=ticket.address, ticket=handle))
    assert started.ticket.model_call_limit == 100
    assert started.start.worker.caller_oid == A.oid


def test_original_ticket_is_reused_but_never_grants_a_second_start(case):
    c = case
    count = len(c.table.submissions)
    again = c.receipts.issue(c.ticket, c.human, expected_vm=c.vm)
    assert again == c.delivery and len(c.table.submissions) == count
    first = begin(c)
    assert first.start.worker == c.context and first.start.actor_id == A.actor
    after = len(c.table.submissions)
    with pytest.raises(Problem) as replay:
        begin(c)
    assert replay.value.status == 409 and len(c.table.submissions) == after
    status = c.receipts.read(c.agent, c.context, c.ticket.address)
    assert status.state == "started_or_unknown" and status.start == first.start


def test_same_rid_with_different_content_cannot_change_the_ticket(case):
    c = case
    changed = c.ticket.model_copy(update={"request_sha256": "f" * 64})
    with pytest.raises(Problem) as error:
        c.receipts.issue(changed, c.human, expected_vm=c.vm)
    assert error.value.body["error"]["code"] == "BEHAVIOR_REQUEST_CONFLICT"
    assert c.receipts.read(c.agent, c.context, c.ticket.address).state == "issued"


def test_lost_start_ack_remains_unknown_and_does_not_grant_execution_on_retry(case):
    c = case
    c.table.failure = "after"
    with pytest.raises(Problem) as unknown:
        begin(c)
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    c.table.failure = None
    with pytest.raises(Problem) as replay:
        begin(c)
    assert replay.value.status == 409
    assert c.receipts.read(c.agent, c.context, c.ticket.address).state == "started_or_unknown"


def test_cancel_before_start_keeps_the_original_ticket_and_prevents_start(case):
    c = case
    assert c.receipts.cancel(c.agent, c.context, c.ticket.address).state == "cancel_requested"
    count = len(c.table.submissions)
    assert c.receipts.cancel(c.agent, c.context, c.ticket.address).state == "cancel_requested"
    assert len(c.table.submissions) == count
    with pytest.raises(Problem) as denied:
        begin(c)
    assert denied.value.status == 409
    assert c.store.read_record(start_key(c.handle.ticket_id)) is None


@pytest.mark.parametrize("field,value", [
    ("team_id", "team-other"), ("agent_id", "agent-other"),
    ("hosted_session_id", "different-session"), ("hosted_agent_version", "42"), ("vm_generation", "different-generation"),
])
def test_wrong_vm_binding_never_consumes_the_ticket(case, field, value):
    c = case
    bad_vm = c.vm.model_copy(update={field: value})
    bad = c.context.model_copy(update={"vm": bad_vm})
    guard = c.store._guard()
    with pytest.raises(Problem):
        begin(c, context=bad)
    assert c.store._guard() == guard


def test_bad_digest_or_address_cannot_consume_an_existing_ticket(case):
    c = case
    for command in (
        BeginTurn(address=c.ticket.address, ticket=c.handle.model_copy(update={"ticket_sha256": "f" * 64})),
        BeginTurn(address=c.ticket.address.model_copy(update={"conversation_id": "different-cid"}), ticket=c.handle),
    ):
        with pytest.raises(Problem):
            c.receipts.begin(c.agent, c.context, command)
    assert c.receipts.read(c.agent, c.context, c.ticket.address).state == "issued"


def test_end_is_immutable_and_none_usage_is_not_zero_or_a_blocker(case, caplog):
    c = case
    end = end_for(c, begin(c))
    first = c.receipts.complete(c.agent, c.context, end)
    assert c.receipts.complete(c.agent, c.context, end) == first
    with pytest.raises(Problem) as changed:
        c.receipts.complete(c.agent, c.context, end.model_copy(update={"outcome": "failed"}))
    assert changed.value.status == 409
    assert c.receipts.read(c.agent, c.context, c.ticket.address).end == end
    c.receipts.record_usage = lambda **_: pytest.fail("None is unknown, not zero usage")
    consumed = c.receipts.consume_completion(c.agent, c.context, c.ticket.address)
    assert consumed.payload["result"]["usage_recorded"] is False
    assert "INSTRUCTION_TURN_USAGE_UNKNOWN" in caplog.text


def test_end_requires_original_process_and_actual_saved_blob(case):
    c = case
    end = end_for(c, begin(c))
    with pytest.raises(Problem):
        c.receipts.complete(c.agent, c.context.model_copy(update={"process_id": "replacement-process"}), end)
    bad_ref = end.result.model_copy(update={"receipt": end.result.receipt.model_copy(update={"sha256": "e" * 64})})
    with pytest.raises(Problem):
        c.receipts.complete(c.agent, c.context, end.model_copy(update={"result": bad_ref}))
    assert c.receipts.read(c.agent, c.context, c.ticket.address).end is None


def test_another_team_member_cannot_forge_the_started_agents_end(case):
    c = case
    end = end_for(c, begin(c))
    peer = replace(c.agent, oid=B.oid, actor_id=B.actor)
    c.relay.write_record(peer, "turn-complete", c.handle.ticket_id, end.model_dump(mode="json"))
    with pytest.raises(Problem) as forged:
        c.receipts.read(c.agent, c.context, c.ticket.address)
    assert forged.value.body["error"]["code"] == "TURN_RECEIPT_INVALID"
    assert c.store.read_record(consumption_key(c.handle.ticket_id)) is None


def test_usage_failure_keeps_completion_unconsumed_and_replay_records_once(case):
    c = case
    usage = TurnModelUsage(schema_version="fa-turn-model-usage-v1", usage=())
    end = end_for(c, begin(c), usage=usage)
    c.receipts.complete(c.agent, c.context, end)
    calls = []

    def recorder(*, job_id, rid, usage):
        calls.append((job_id, rid))
        key = control_key("instruction-turn-usage", job_id, rid)
        receipt = c.store.transact(ControlRequest(
            request_id=key, request_digest=digest(usage.model_dump(mode="json")), read_set=(key,),
        ), lambda _: ControlChanges(writes=(ControlWrite(row_id=key, data={"kind": "offline-usage"}, mode="create"),),
                                    result={"recorded": True}))
        if len(calls) == 1:
            raise Problem(503, "OFFLINE_ACK_LOST", "the original usage receipt is retained")
        return receipt

    c.receipts.record_usage = recorder
    with pytest.raises(Problem):
        c.receipts.consume_completion(c.agent, c.context, c.ticket.address)
    assert c.store.read_record(consumption_key(c.handle.ticket_id)) is None
    consumed = c.receipts.consume_completion(c.agent, c.context, c.ticket.address)
    assert consumed.payload["result"]["usage_recorded"] is True and len(calls) == 2
    c.receipts.record_usage = None
    assert c.receipts.consume_completion(c.agent, c.context, c.ticket.address) == consumed


def test_signed_begin_uses_handshake_context_not_caller_selected_fields(case):
    c = case
    command = BeginTurn(address=c.ticket.address, ticket=c.handle)
    count = len(c.table.submissions)
    denied = c.client.post(PREFIX + "/begin", content=command.model_dump_json(),
                          headers={**c.headers, CONNECTION_HEADER: "forged-connection"})
    assert denied.status_code == 403 and len(c.table.submissions) == count
    started = c.client.post(PREFIX + "/begin", content=command.model_dump_json(), headers=c.headers)
    assert started.status_code == 200, started.text
    assert started.json()["start"]["worker"]["caller_oid"] == A.oid
    replay = c.client.post(PREFIX + "/begin", content=command.model_dump_json(), headers=c.headers)
    assert replay.status_code == 409


def test_t2_consumer_executes_once_and_read_never_reexecutes(case):
    c = case
    invoked = []

    def transport(request):
        response = c.client.request(request.method, request.url.path, content=request.content,
                                    headers=dict(request.headers))
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    client = TurnReceiptClient("https://context.invalid", token_provider=lambda: c.headers["Authorization"][7:],
                               transport=httpx.MockTransport(transport))

    def execute(context, started):
        invoked.append(started.ticket.address.request_id)
        return end_for(c, started)

    consumer = TurnReceiptConsumer(client, execute=execute, abort=lambda _: None)
    worker = AgentWorkerControl(c.vm, consumer, process_id=c.context.process_id, caller_oid=c.context.caller_oid)
    connection = worker.connect(c.vm, context=c.context)
    try:
        result = connection.deliver(c.delivery).result(timeout=10)
        assert TurnEndReceipt.model_validate_json(result).ticket == c.handle
        with pytest.raises(Problem):
            connection.deliver(c.delivery).result(timeout=10)
        assert invoked == [c.ticket.address.request_id]
        assert json.loads(connection.read(c.ticket.address))["state"] == "completed"
        assert invoked == [c.ticket.address.request_id]
    finally:
        worker.close()
        client.close()


def test_incomplete_usage_and_checkpoints_cannot_be_claimed_as_success(case):
    c = case
    end = end_for(c, begin(c)).model_dump(mode="json")
    end["model_usage"] = {}
    with pytest.raises(ValidationError):
        TurnEnd.model_validate_json(json.dumps(end))
    end["model_usage"] = None
    end["safe_manual_checkpoint"] = True
    with pytest.raises(ValidationError):
        TurnEnd.model_validate_json(json.dumps(end))


def test_begin_has_one_atomic_metadata_write_and_reports_its_actual_read_rounds(case, caplog):
    from shared_context.behavior_timing import phase_timing
    c = case
    with phase_timing("t1b-begin"):
        begin(c)
    measured = [json.loads(record.getMessage().removeprefix("FA_TIMING ")) for record in caplog.records
                if record.getMessage().startswith("FA_TIMING ")]
    value, = measured
    assert (value["table_gets"], value["batches"]) == (5, 4)
    print(json.dumps({"phase": value["phase"], "gets": value["table_gets"], "rounds": value["batches"]}))


def test_unknown_end_does_not_record_zero_usage_or_allow_another_start(case):
    c = case
    begin(c)
    c.receipts.record_usage = lambda **_: pytest.fail("no end means no measured usage")
    with pytest.raises(Problem) as unknown:
        c.receipts.consume_completion(c.agent, c.context, c.ticket.address)
    assert unknown.value.body["error"]["code"] == "TURN_END_UNKNOWN"
    assert c.store.read_record(consumption_key(c.handle.ticket_id)) is None
    with pytest.raises(Problem):
        begin(c)


def test_checkpoint_namespace_is_not_silently_replaced_by_the_team_object_namespace(case):
    c = case
    end = end_for(c, begin(c))
    with pytest.raises(ValidationError):
        TurnObjectReference(kind="checkpoint", object_id=end.result.object_id,
                            version_id=end.result.version_id, receipt=end.result.receipt)


def test_relay_limit_does_not_add_a_limit_to_human_turns(case):
    c = case
    data = c.ticket.model_dump(mode="json")
    data["model_call_limit"] = 100
    with pytest.raises(ValidationError):
        TurnTicket.model_validate_json(json.dumps(data))
    assert c.ticket.model_call_limit is None


def test_same_raw_request_id_in_another_team_has_a_distinct_ticket(case):
    c = case
    other_vm = c.vm.model_copy(update={"team_id": C.team, "agent_id": "agent-c"})
    owner = replace(c.human, oid="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", team=C.team)
    ticket = ticket_for(other_vm, oid=C.oid, issuer=owner.oid, owner=owner.oid)
    delivery = c.receipts.issue(ticket, owner, expected_vm=other_vm)
    assert delivery.address.request_id == c.delivery.address.request_id
    assert ticket_handle(ticket).ticket_id != c.handle.ticket_id
    assert c.receipts.read(c.agent, c.context, c.ticket.address).state == "issued"


def test_ticket_start_cancel_and_consumption_are_readable_by_the_real_startup_classifier(synthetic_standing_v1):
    c = synthetic_standing_v1
    ids = c.h.config.identity
    target = c.h.config.active_targets[0]
    vm = AgentVmBinding(team_id=ids.team_id, agent_id=target.agent_id, hosted_session_id="logical-agent-session",
                        hosted_agent_version="41", vm_generation="logical-generation")
    ticket = ticket_for(vm, oid=target.agent_oid, issuer=ids.human_oid, owner=ids.human_oid, tenant_id=ids.tenant_id)
    relay = TeamDataRelay(InMemoryRepository(), InMemoryArtifactStore(ids.tenant_id), ids.tenant_id)
    receipts = TurnReceiptStore(c.control.store, relay, validate_checkpoint=lambda *_: pytest.fail("no checkpoint"))
    control = AgentControlRelay(wait_seconds=0)
    context = control.connect(vm, "original-process", target.agent_oid)
    actor = ids.auth().actors[target.agent_oid]
    agent = BehaviorPrincipal(ids.tenant_id, actor.oid, actor.team, "agent", actor.actor,
                              frozenset({"read", "execute", "submit"}))
    try:
        receipts.issue(ticket, c.human, expected_vm=vm)
        started = receipts.begin(agent, context, BeginTurn(address=ticket.address, ticket=ticket_handle(ticket)))
        receipts.cancel(agent, context, ticket.address)
        end = TurnEnd(
            ticket=ticket_handle(ticket), address=ticket.address,
            start_sha256=digest(started.start.model_dump(mode="json")), outcome="cancelled",
            result=None, checkpoint=None, sdk_stop=None, model_usage=None)
        receipts.complete(agent, context, end)
        receipts.consume_completion(agent, context, ticket.address)
        inventory = read_startup(c)
        assert len(validate_turn_receipt_history(inventory, c.h.config)) == 3
    finally:
        control.close()


def test_hosted_receipt_modules_do_not_import_api_storage_or_the_sdk():
    code = """
import importlib.abc
import sys
class Forbidden(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in
               ('fastapi', 'azure.data.tables', 'azure.storage.blob', 'copilot')):
            raise ImportError('forbidden dependency: ' + fullname)
sys.meta_path.insert(0, Forbidden())
import shared_context.behavior_turn_wire
import shared_context.behavior_turn_client
"""
    completed = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr


def test_checkpoint_adapter_uses_original_published_parts_and_exact_stop(synthetic_standing_v1):
    from shared_context.behavior_sdk_checkpoint import published_record

    c = synthetic_standing_v1
    original = saved_turn(c, "original-checkpoint-for-ticket")
    binding, assignment = original.binding, original.assignment
    vm = AgentVmBinding(
        team_id=binding.team_id, agent_id=c.h.config.active_targets[0].agent_id,
        hosted_session_id="logical-session", hosted_agent_version=assignment.task.instruction.hosted_agent_version,
        vm_generation="logical-generation",
    )
    ticket = TurnTicket(
        tenant_id=binding.tenant_id, vm=vm, address=AgentTurnAddress(
            conversation_id=assignment.task.instruction.conversation_id,
            sdk_session_id=assignment.sdk_session_id, request_id="original-checkpoint-for-ticket"),
        assignment=assignment, owner_oid=binding.human_oid, agent_oid=binding.agent_oid,
        issuer_oid=binding.human_oid, source="human", request_sha256=digest("original admitted input"),
    )
    published = published_record(c.authority, original.archive, job_id=binding.job_id)
    validate_sdk_checkpoint(c.authority, ticket, original.archive, published.sdk_stop)
    with pytest.raises(Problem) as stop_changed:
        validate_sdk_checkpoint(c.authority, ticket, original.archive,
                                published.sdk_stop.model_copy(update={"pid": published.sdk_stop.pid + 1}))
    assert stop_changed.value.body["error"]["code"] == "SDK_STATE_INCOMPLETE"
    with pytest.raises(Problem):
        validate_sdk_checkpoint(c.authority, ticket, original.archive.model_copy(update={
            "artifact_id": "td-checkpoint-not-the-original"}), published.sdk_stop)


def test_actual_usage_recorder_commits_before_completion_and_is_not_called_on_replay(synthetic_standing_v1):
    from shared_context.behavior_instruction import sdk_session_id
    from shared_context.behavior_instruction_cost import record_turn_usage, turn_usage_key

    c = synthetic_standing_v1
    rid = "real-recorder-no-model-sent"
    binding, _ = c.control.submit(c.human, InstructionSubmit(request_id=rid, content="OFFLINE receipt integration."))
    task = Task.model_validate_json(c.h.runtime.job_input(binding))
    vm = AgentVmBinding(
        team_id=binding.team_id, agent_id=c.h.config.active_targets[0].agent_id,
        hosted_session_id="logical-session", hosted_agent_version=task.instruction.hosted_agent_version,
        vm_generation="logical-generation")
    address = AgentTurnAddress(conversation_id=task.instruction.conversation_id,
                               sdk_session_id=sdk_session_id(task.instruction.conversation_id), request_id=rid)
    ticket = TurnTicket(
        tenant_id=binding.tenant_id, vm=vm, address=address, owner_oid=binding.human_oid,
        agent_oid=binding.agent_oid, issuer_oid=binding.human_oid, source="human",
        request_sha256=c.control.records[binding.binding_id].submit_sha256,
        assignment=Assignment(binding_id=binding.binding_id, session_id=digest(["new-ticket-session", rid]),
                              sdk_session_id=address.sdk_session_id, task=task))
    actor = c.h.config.identity.auth().actors[binding.agent_oid]
    agent = BehaviorPrincipal(binding.tenant_id, actor.oid, actor.team, "agent", actor.actor,
                              frozenset({"read", "execute", "submit"}))
    control = AgentControlRelay(wait_seconds=0)
    context = control.connect(vm, "original-process", actor.oid)
    relay = TeamDataRelay(InMemoryRepository(), InMemoryArtifactStore(binding.tenant_id), binding.tenant_id)
    receipts = TurnReceiptStore(c.control.store, relay, record_usage=partial(record_turn_usage, c.control),
                               validate_checkpoint=partial(validate_sdk_checkpoint, c.authority))
    try:
        receipts.issue(ticket, c.human, expected_vm=vm)
        started = receipts.begin(agent, context, BeginTurn(address=address, ticket=ticket_handle(ticket)))
        end = TurnEnd(
            ticket=ticket_handle(ticket), address=address, start_sha256=started.start.sha256, outcome="cancelled",
            result=None, checkpoint=None, sdk_stop=None,
            model_usage=TurnModelUsage(schema_version="fa-turn-model-usage-v1", usage=()))
        receipts.complete(agent, context, end)
        consumed = receipts.consume_completion(agent, context, address)
        usage = c.control.store.read_record(turn_usage_key(binding.job_id, rid))
        assert usage.payload["data"]["rid"] == rid
        assert usage.payload["data"]["job_id"] == binding.job_id
        assert consumed.payload["result"]["usage_recorded"] is True
        receipts.record_usage = lambda **_: pytest.fail("a retained completion needs no new usage operation")
        assert receipts.consume_completion(agent, context, address) == consumed
        read_startup(c)
    finally:
        control.close()
