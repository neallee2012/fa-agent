"""One admitted ticket, one start CAS and one immutable end; no scheduler or agent loop."""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Callable, Literal

from pydantic import SecretBytes

from .behavior_agent_worker import AgentTurnAddress, AgentTurnDelivery, AgentVmBinding, AgentWorkerContext
from .behavior_auth import BehaviorPrincipal
from .behavior_operations import _Model, _parse, _row, _updated, _write, control_key
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest, ControlWrite
from .behavior_wire import Digest
from .behavior_turn_wire import (
    BeginTurn, StartedTurn, TurnEnd, TurnEndReceipt, TurnHandle, TurnObservation, TurnStartReceipt, TurnTicket,
)
from .behavior_sdk_state import StoredSdkArchive
from .errors import Problem
from .records import Record
from .source_materials import digest
from .team_data_relay import TeamDataRelay


def turn_key(tenant_id, team_id, agent_id, request_id):
    return control_key("vm-turn-ticket", tenant_id, team_id, agent_id, request_id)


def start_key(ticket_id):
    return control_key("vm-turn-start", ticket_id)


def consumption_key(ticket_id):
    return control_key("vm-turn-consumed", ticket_id)


def ticket_handle(ticket):
    return TurnHandle(
        ticket_id=turn_key(ticket.tenant_id, ticket.vm.team_id, ticket.vm.agent_id, ticket.address.request_id),
        ticket_sha256=digest(ticket.model_dump(mode="json")),
    )


class TurnTicketRecord(_Model):
    kind: Literal["vm_turn_ticket"] = "vm_turn_ticket"
    ticket: TurnTicket
    started: bool = False
    cancel_requested: bool = False


class TurnStartRecord(_Model):
    kind: Literal["vm_turn_start"] = "vm_turn_start"
    start: TurnStartReceipt
    request: ControlRequest


class TurnConsumption(_Model):
    kind: Literal["vm_turn_consumption"] = "vm_turn_consumption"
    ticket: TurnHandle
    end_sha256: Digest
    usage_recorded: bool


@dataclass(frozen=True)
class PreparedTurn:
    """The caller may compose these predicates/writes into its existing admission or relay CAS."""

    request: ControlRequest
    writes: tuple[ControlWrite, ...]
    delivery: AgentTurnDelivery


def prepare_turn(ticket: TurnTicket, principal: BehaviorPrincipal, *, expected_vm: AgentVmBinding) -> PreparedTurn:
    ticket = _parse(ticket.model_dump(mode="json"), TurnTicket)
    if (not isinstance(principal, BehaviorPrincipal) or principal.kind != ticket.source
            or principal.tenant_id != ticket.tenant_id or principal.oid != ticket.issuer_oid
            or ticket.vm != expected_vm):
        raise Problem(403, "TURN_TICKET_DENIED", "the verified issuer and server-resolved Agent VM are required")
    principal.require(ticket.vm.team_id, "submit")
    handle = ticket_handle(ticket)
    request = ControlRequest(
        request_id=control_key("vm-turn-issue", handle.ticket_id),
        request_digest=handle.ticket_sha256,
        read_set=(handle.ticket_id,),
    )
    return PreparedTurn(
        request=request,
        writes=(_write(handle.ticket_id, TurnTicketRecord(ticket=ticket), "create"),),
        delivery=AgentTurnDelivery(address=ticket.address, payload=SecretBytes(handle.model_dump_json().encode())),
    )


def _require_worker(principal, context):
    if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "agent" or not principal.actor_id
            or principal.oid != context.caller_oid or principal.team != context.vm.team_id):
        raise Problem(403, "TURN_WORKER_DENIED", "the original verified Agent handshake is required")
    principal.require(context.vm.team_id, "execute")


def validate_sdk_checkpoint(authority, ticket, reference, stop):
    """Adapter to the original CTX-03 publication/part reader, never the td-checkpoint namespace."""
    from .behavior_sdk_checkpoint import SdkCheckpointStore, published_record

    binding = authority.runtime.bindings.get(ticket.assignment.binding_id)
    task = ticket.assignment.task
    instruction = task.instruction
    if (binding is None or instruction is None or (binding.tenant_id, binding.team_id, binding.agent_oid, binding.job_id) !=
            (ticket.tenant_id, ticket.vm.team_id, ticket.agent_oid, task.job_id)
            or authority.runtime.job_input(binding) != task.model_dump_json().encode()):
        raise Problem(403, "ASSISTANT_CHECKPOINT_SCOPE", "the original registered checkpoint job is required")
    published = published_record(authority, reference, job_id=task.job_id)
    if published.sdk_stop != stop:
        raise Problem(409, "SDK_STATE_INCOMPLETE", "the checkpoint must retain its exact original SDK stop")
    SdkCheckpointStore(authority.artifacts.store).load(
        reference, binding, conversation_id=ticket.address.conversation_id,
        sdk_session_id=ticket.address.sdk_session_id, job_id=task.job_id,
        checkpoint_profile=instruction.checkpoint_profile, model_provider=instruction.model_provider,
    )


class TurnReceiptStore:
    def __init__(self, store: BehaviorStore, relay: TeamDataRelay, *,
                 validate_checkpoint: Callable, record_usage: Callable | None = None,
                 assistant=None,
                 clock=lambda: datetime.now(timezone.utc)):
        if not callable(validate_checkpoint):
            raise ValueError("the original checkpoint publication validator is required")
        self.store, self.relay = store, relay
        self.validate_checkpoint, self.record_usage, self.clock = validate_checkpoint, record_usage, clock
        if assistant is not None and assistant.store is not store:
            raise ValueError("the SDK session and begin must share the original atomic store")
        self.assistant = assistant

    def issue(self, ticket, principal, *, expected_vm):
        prepared = prepare_turn(ticket, principal, expected_vm=expected_vm)

        def create(rows):
            if rows[prepared.request.read_set[0]] is not None:
                raise Problem(409, "TURN_TICKET_CONFLICT", "the original turn already has an admission")
            return ControlChanges(writes=prepared.writes, result={"ticket_id": prepared.request.read_set[0]})

        self.store.transact(prepared.request, create)
        return prepared.delivery

    @staticmethod
    def _scope(record, principal, context, address, *, starting=False):
        _require_worker(principal, context)
        ticket = record.ticket
        if (ticket.tenant_id != principal.tenant_id or ticket.vm.team_id != principal.team
                or ticket.vm.agent_id != context.vm.agent_id or ticket.agent_oid != principal.oid
                or ticket.address != address or starting and ticket.vm != context.vm):
            raise Problem(403, "TURN_TICKET_DENIED", "the original Team, Agent, turn and VM generation must match")
        return ticket

    def _rows(self, principal, context, address):
        _require_worker(principal, context)
        key = turn_key(principal.tenant_id, context.vm.team_id, context.vm.agent_id, address.request_id)
        keys = key, start_key(key)
        rows = self.store.read_snapshot(keys)
        if rows[key] is None:
            raise Problem(404, "TURN_TICKET_UNKNOWN", "the original turn is not recorded; this is not dispatch permission")
        record = _row(rows, key, TurnTicketRecord)
        self._scope(record, principal, context, address)
        start = None if rows[keys[1]] is None else _row(rows, keys[1], TurnStartRecord)
        if record.started != (start is not None):
            raise Problem(409, "TURN_RECEIPT_INVALID", "the ticket and original start receipt disagree")
        if start is not None and (
                start.start.ticket != ticket_handle(record.ticket) or start.start.address != address
                or start.start.job_id != record.ticket.assignment.task.job_id
                or start.start.worker.vm != record.ticket.vm
                or start.start.worker.caller_oid != record.ticket.agent_oid):
            raise Problem(409, "TURN_RECEIPT_INVALID", "the original start belongs to a different admitted turn")
        return record, start

    def begin(self, principal, context, command: BeginTurn):
        _require_worker(principal, context)
        expected = turn_key(principal.tenant_id, context.vm.team_id, context.vm.agent_id, command.address.request_id)
        if command.ticket.ticket_id != expected:
            raise Problem(403, "TURN_TICKET_DENIED", "a caller cannot select another turn's storage key")
        skey = start_key(expected)
        prepared = None
        extra_reads = ()
        if self.assistant is not None:
            from .behavior_vm_session import prepare_vm_session, vm_session_keys
            rows = self.store.read_snapshot((expected,))
            original = _row(rows, expected, TurnTicketRecord)
            self._scope(original, principal, context, command.address, starting=True)
            if original.started or original.cancel_requested:
                raise Problem(409, "TURN_ALREADY_STARTED", "the original start is retained; read its receipt")
            if command.ticket != ticket_handle(original.ticket):
                raise Problem(403, "TURN_TICKET_DENIED", "the original admitted ticket bytes differ")
            prepared = prepare_vm_session(self.assistant, original.ticket)
            extra_reads = vm_session_keys(self.assistant, prepared.session)
        request = ControlRequest(
            request_id=skey,
            request_digest=digest([command.model_dump(mode="json"), context.model_dump(mode="json"), principal.actor_id]),
            read_set=tuple(dict.fromkeys((expected, skey, *extra_reads))),
        )
        started = None
        saved_ticket = None

        def start(rows):
            nonlocal started, saved_ticket
            record = _row(rows, expected, TurnTicketRecord)
            saved_ticket = self._scope(record, principal, context, command.address, starting=True)
            if command.ticket != ticket_handle(saved_ticket):
                raise Problem(403, "TURN_TICKET_DENIED", "the original admitted ticket bytes differ")
            if record.started or record.cancel_requested or rows[skey] is not None:
                raise Problem(409, "TURN_ALREADY_STARTED", "the original turn is started or cancelled; read its receipt")
            if saved_ticket.relay is not None and self.clock() >= saved_ticket.relay.deadline:
                raise Problem(409, "TURN_DEADLINE_EXPIRED", "the original relay turn deadline has passed")
            started = TurnStartReceipt(
                ticket=command.ticket, address=command.address, job_id=saved_ticket.assignment.task.job_id,
                worker=context, actor_id=principal.actor_id, request_id=request.request_id,
            )
            session_writes = ()
            if prepared is not None:
                from .behavior_vm_session import vm_begin_writes
                if saved_ticket.assignment != prepared.session.assignment:
                    raise Problem(409, "TURN_TICKET_CONFLICT", "the prepared SDK assignment changed")
                session_writes = vm_begin_writes(self.assistant, prepared, rows)
            return ControlChanges(
                writes=(_write(expected, _updated(record, started=True), "replace"),
                        _write(skey, TurnStartRecord(start=started, request=request), "create"), *session_writes),
                result={"start_sha256": digest(started.model_dump(mode="json"))},
            )

        self.store.transact(request, start)
        if started is None:
            raise Problem(409, "TURN_ALREADY_STARTED", "a retained start receipt is not permission to execute again")
        if self.assistant is not None:
            self.assistant._vm_starts[expected] = saved_ticket, started
        return StartedTurn(ticket=saved_ticket, start=started)

    def _read_end(self, principal, ticket, start):
        handle = ticket_handle(ticket)
        try:
            row = self.relay.read_record(principal, "turn-complete", handle.ticket_id)
        except Problem as error:
            if error.status != 404:
                raise
            return None
        end = _parse(row["data"], TurnEnd)
        self._validate_end(principal, ticket, start, end, writer=row["writer"])
        return end, row["etag"]

    def _validate_end(self, principal, ticket, start, end, *, writer):
        if (start is None or start.start.worker.caller_oid != ticket.agent_oid
                or start.start.actor_id != writer or end.ticket != ticket_handle(ticket)
                or end.address != ticket.address
                or end.start_sha256 != digest(start.start.model_dump(mode="json"))):
            raise Problem(409, "TURN_RECEIPT_INVALID", "the end must retain the verified original starter and turn")
        for reference in (() if end.result is None else (end.result,)) + end.memory:
            self.relay.read_object(principal, reference.kind, reference.object_id,
                                   reference.version_id, reference.receipt)
        if isinstance(end.checkpoint, StoredSdkArchive):
            if end.checkpoint.sdk_session_id != ticket.address.sdk_session_id:
                raise Problem(409, "TURN_RECEIPT_INVALID", "the original checkpoint belongs to another SDK session")
            self.validate_checkpoint(ticket, end.checkpoint, end.sdk_stop)

    def complete(self, principal, context, end: TurnEnd):
        end = _parse(end.model_dump(mode="json"), TurnEnd)
        record, start = self._rows(principal, context, end.address)
        ticket = record.ticket
        if (start is None or start.start.worker.vm != context.vm
                or start.start.worker.process_id != context.process_id):
            raise Problem(409, "TURN_RECEIPT_INVALID", "only the original started process may complete this turn")
        self._validate_end(principal, ticket, start, end, writer=principal.actor_id)
        existing = self._read_end(principal, ticket, start)
        if existing is not None:
            if existing[0] != end:
                raise Problem(409, "TURN_END_CONFLICT", "the original end receipt cannot be replaced")
            etag = existing[1]
        else:
            saved = self.relay.write_record(principal, "turn-complete", end.ticket.ticket_id,
                                            end.model_dump(mode="json"))
            etag = saved.etag
        return TurnEndReceipt(ticket=end.ticket, address=end.address,
                              end_sha256=digest(end.model_dump(mode="json")), etag=etag)

    def read(self, principal, context, address):
        record, start = self._rows(principal, context, address)
        ended = self._read_end(principal, record.ticket, start)
        return TurnObservation(
            ticket=ticket_handle(record.ticket), address=address,
            state=("completed" if ended is not None else "cancel_requested" if record.cancel_requested
                   else "started_or_unknown" if start is not None else "issued"),
            start=None if start is None else start.start, end=None if ended is None else ended[0],
        )

    def cancel(self, principal, context, address):
        record, _ = self._rows(principal, context, address)
        handle = ticket_handle(record.ticket)
        job_key = None
        if self.assistant is not None:
            from .behavior_workers import worker_key
            job_key = worker_key("job", record.ticket.assignment.task.job_id)
        request = ControlRequest(
            request_id=control_key("vm-turn-cancel", handle.ticket_id),
            request_digest=handle.ticket_sha256, read_set=(handle.ticket_id, *((job_key,) if job_key else ())),
        )

        def cancel(rows):
            current = _row(rows, handle.ticket_id, TurnTicketRecord)
            if current.ticket != record.ticket:
                raise Problem(409, "TURN_TICKET_CONFLICT", "the original admitted turn changed")
            extra = ()
            if job_key is not None:
                from .behavior_workers import JobRecord
                job = _row(rows, job_key, JobRecord)
                if job.spec.input != record.ticket.assignment.task.model_dump_json().encode():
                    raise Problem(409, "TURN_RECEIPT_INVALID", "the cancelled job must match its original admitted task")
                extra = (self.assistant.runtime.dep.workers._write(
                    job_key, _updated(job, cancel_requested=True), rows),)
            return ControlChanges(writes=(_write(
                handle.ticket_id, _updated(current, cancel_requested=True), "replace"), *extra),
                result={"cancel_requested": True})

        self.store.transact(request, cancel)
        return self.read(principal, context, address)

    def consume_completion(self, principal, context, address):
        record, start = self._rows(principal, context, address)
        ended = self._read_end(principal, record.ticket, start)
        if ended is None:
            raise Problem(409, "TURN_END_UNKNOWN", "no original end is saved; do not resend or infer zero usage")
        end = ended[0]
        handle = ticket_handle(record.ticket)
        ckey = consumption_key(handle.ticket_id)
        consumed = TurnConsumption(ticket=handle, end_sha256=digest(end.model_dump(mode="json")),
                                   usage_recorded=end.model_usage is not None)
        request = ControlRequest(request_id=ckey, request_digest=digest(consumed.model_dump(mode="json")),
                                 read_set=(handle.ticket_id, start_key(handle.ticket_id), ckey))
        prior = self.store._receipt(request)
        if prior is not None:
            return prior
        if end.model_usage is not None:
            if self.record_usage is None:
                raise Problem(503, "TURN_USAGE_UNAVAILABLE", "usage recording must complete before consuming the end")
            receipt = self.record_usage(job_id=record.ticket.assignment.task.job_id,
                                        rid=address.request_id, usage=end.model_usage)
            if not isinstance(receipt, Record):
                raise Problem(503, "TURN_USAGE_UNAVAILABLE", "usage recording did not return its original receipt")
        else:
            logging.getLogger(__name__).warning("INSTRUCTION_TURN_USAGE_UNKNOWN job_id=%s rid=%s",
                                                record.ticket.assignment.task.job_id, address.request_id)

        def consume(rows):
            current = _row(rows, handle.ticket_id, TurnTicketRecord)
            original_start = _row(rows, start_key(handle.ticket_id), TurnStartRecord)
            if current.ticket != record.ticket or original_start != start or not current.started:
                raise Problem(409, "TURN_RECEIPT_INVALID", "the original start changed before completion consumption")
            if rows[ckey] is not None:
                raise Problem(409, "TURN_END_CONFLICT", "the original completion already has a different receipt")
            return ControlChanges(writes=(_write(ckey, consumed, "create"),),
                                  result={"end_sha256": consumed.end_sha256,
                                          "usage_recorded": consumed.usage_recorded})

        return self.store.transact(request, consume)


def validate_turn_receipt_history(inventory, config):
    """Classify the new control rows without moving or rewriting old checkpoints/results."""
    from .behavior_recovery_history import _Inventory

    view = _Inventory(inventory, config)
    records = {
        key: view.row(key, TurnTicketRecord) for key, row in inventory.rows.items()
        if row.payload["data"].get("kind") == "vm_turn_ticket"
    }
    creation_hashes = {key: set() for key in records}
    for receipt in inventory.receipts.values():
        for key, fingerprint in receipt.payload["writes"]:
            if key in creation_hashes:
                creation_hashes[key].add(fingerprint)
    classified = {}
    for key, record in records.items():
        ticket = record.ticket
        handle = ticket_handle(ticket)
        if (key != handle.ticket_id or ticket.tenant_id != config.identity.tenant_id
                or view.image_hash(key, TurnTicketRecord(ticket=ticket)) not in creation_hashes[key]):
            raise Problem(409, "TURN_RECEIPT_INVALID", "the turn has no original immutable admission write")
        classified[key] = record
        skey = start_key(key)
        if record.started != (skey in inventory.rows):
            raise Problem(409, "TURN_RECEIPT_INVALID", "the retained ticket and start disagree")
        if record.started:
            original = view.row(skey, TurnStartRecord)
            started = original.start
            session_reads = ()
            session_writes = ()
            from .behavior_assistant import _Session, key as assistant_key
            from .behavior_workers import JobRecord, worker_key
            session_key = assistant_key("session", ticket.assignment.session_id)
            if session_key in inventory.rows:
                session = view.row(session_key, _Session)
                job_key = worker_key("job", ticket.assignment.task.job_id)
                job = view.row(job_key, JobRecord)
                if (session.vm_turn_ticket_id != key or session.ticket_digest is not None
                        or session.assignment != ticket.assignment or session.execution != job.binding
                        or job.spec.input != ticket.assignment.task.model_dump_json().encode()):
                    raise Problem(409, "TURN_RECEIPT_INVALID", "the original begin did not create its SDK session")
                session_reads = (session_key, job_key,
                    control_key("owner", session.execution.owner.owner_record_id),
                    worker_key("pool", config.workers.workspace_id, session.execution.job_id))
                session_writes = (session_key,)
                classified[session_key] = session
            expected = ControlRequest(
                request_id=skey,
                request_digest=digest([
                    BeginTurn(address=ticket.address, ticket=handle).model_dump(mode="json"),
                    started.worker.model_dump(mode="json"), started.actor_id,
                ]),
                read_set=(key, skey, *session_reads),
            )
            if (started.ticket != handle or started.address != ticket.address or started.worker.vm != ticket.vm
                    or started.worker.caller_oid != ticket.agent_oid or started.job_id != ticket.assignment.task.job_id
                    or started.request_id != skey or original.request != expected):
                raise Problem(409, "TURN_RECEIPT_INVALID", "the original start belongs to another turn or worker")
            view.receipt(expected, (key, skey, *session_writes), exact={
                key: view.image_hash(key, TurnTicketRecord(ticket=ticket, started=True)),
                skey: view.hash(skey),
            }, result={"start_sha256": digest(started.model_dump(mode="json"))})
            classified[skey] = original
        if record.cancel_requested:
            cancel_id = control_key("vm-turn-cancel", key)
            from .behavior_workers import JobRecord, worker_key
            job_key = worker_key("job", ticket.assignment.task.job_id)
            cancel_receipt = view.receipts.get(cancel_id)
            extra = (job_key,) if cancel_receipt is not None and job_key in dict(cancel_receipt.writes) else ()
            if extra:
                job = view.row(job_key, JobRecord)
                if not job.cancel_requested or job.spec.input != ticket.assignment.task.model_dump_json().encode():
                    raise Problem(409, "TURN_RECEIPT_INVALID", "cancelled SDK job no longer matches its original ticket")
            request = ControlRequest(request_id=cancel_id,
                                     request_digest=handle.ticket_sha256, read_set=(key, *extra))
            view.receipt(request, (key, *extra), exact={key: view.hash(key)}, result={"cancel_requested": True})
        ckey = consumption_key(key)
        if ckey in inventory.rows:
            consumed = view.row(ckey, TurnConsumption)
            if not record.started or consumed.ticket != handle:
                raise Problem(409, "TURN_RECEIPT_INVALID", "an end was consumed without its original start")
            view.receipt(ControlRequest(
                request_id=ckey, request_digest=digest(consumed.model_dump(mode="json")),
                read_set=(key, skey, ckey),
            ), (ckey,), exact={ckey: view.hash(ckey)}, result={
                "end_sha256": consumed.end_sha256, "usage_recorded": consumed.usage_recorded,
            })
            classified[ckey] = consumed
    for key, row in inventory.rows.items():
        if row.payload["data"].get("kind") in {"vm_turn_ticket", "vm_turn_start", "vm_turn_consumption"} \
                and key not in classified:
            raise Problem(409, "TURN_RECEIPT_INVALID", "a VM receipt has no original admitted ticket")
    return classified
