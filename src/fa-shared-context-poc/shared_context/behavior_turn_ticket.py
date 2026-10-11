"""Admitted turn identity and an in-process index of one-use issued credentials."""

from dataclasses import dataclass
import hashlib
from secrets import compare_digest
from threading import Lock
from typing import Literal

from pydantic import Field, SecretStr, model_validator

from .behavior_auth import BehaviorPrincipal
from .behavior_operations import _Model, _parse
from .behavior_server_config import ServerAgentScope
from .errors import Problem
from .source_materials import digest


class TurnAuthorization(_Model):
    schema_version: Literal["fa-admitted-turn-v1"] = "fa-admitted-turn-v1"
    registry_scopes: tuple[ServerAgentScope, ...] = Field(default=(), exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def distinct(self):
        if len({scope.target.agent_id for scope in self.registry_scopes}) != len(self.registry_scopes):
            raise ValueError("an admitted turn cannot contain duplicate agent scopes")
        return self


@dataclass(frozen=True)
class _TicketBinding:
    binding_id: str
    session_id: str
    tenant_id: str
    team_id: str
    agent_oid: str
    authorization_digest: str
    assignment_digest: str
    execution_digest: str
    ticket_digest: str

    def matches(self, session):
        if (session.binding_id != self.binding_id or session.assignment.session_id != self.session_id
                or session.turn_authorization_digest != self.authorization_digest
                or digest(session.assignment.model_dump(mode="json")) != self.assignment_digest
                or digest(session.execution.model_dump(mode="json")) != self.execution_digest
                or not compare_digest(session.ticket_digest, self.ticket_digest)):
            raise Problem(403, "ASSISTANT_TICKET_DENIED", "the original admitted ticket and assignment differ")


class TurnTicketIndex:
    """Only credential identity is cached; mutable session/job state is never cached."""

    def __init__(self):
        self._tickets: dict[str, _TicketBinding] = {}
        self._lock = Lock()

    @staticmethod
    def binding(record, session):
        task = session.assignment.task
        original = record.binding
        if (record.turn_authorization is None or record.profile.instruction is None or session.ticket_digest is None
                or session.turn_authorization_digest != digest(record.model_dump(mode="json"))
                or session.binding_id != original.binding_id or session.assignment.binding_id != original.binding_id
                or task.job_id != original.job_id or task.work_id != original.work.work_id
                or task.definition_digest != digest(original.work.model_dump(mode="json"))
                or task.profile_digest != digest(record.profile.model_dump(mode="json"))
                or task.instruction != record.profile.instruction or task.source != record.profile.source
                or task.artifact_id != record.profile.artifact_id or task.artifact_version != record.profile.artifact_version
                or task.operation_id != original.operation_id or session.execution.job_id != original.job_id
                or session.execution.input_digest != hashlib.sha256(task.model_dump_json().encode()).hexdigest()):
            raise Problem(403, "ASSISTANT_TICKET_DENIED", "a ticket must retain its original admitted task")
        return _TicketBinding(
            binding_id=original.binding_id, session_id=session.assignment.session_id,
            tenant_id=original.tenant_id, team_id=original.team_id, agent_oid=original.agent_oid,
            authorization_digest=session.turn_authorization_digest,
            assignment_digest=digest(session.assignment.model_dump(mode="json")),
            execution_digest=digest(session.execution.model_dump(mode="json")), ticket_digest=session.ticket_digest,
        )

    def publish(self, record, session):
        captured = self.binding(record, session)
        with self._lock:
            previous = self._tickets.get(captured.session_id)
            if previous is not None and previous != captured:
                raise Problem(409, "ASSISTANT_TICKET_CHANGED", "an issued turn ticket cannot be replaced")
            self._tickets[captured.session_id] = captured

    def contains(self, session_id):
        with self._lock:
            return session_id in self._tickets

    def authenticate(self, binding_id, session_id, ticket, principal, *, action="execute"):
        with self._lock:
            captured = self._tickets.get(session_id)
        if (captured is None or not isinstance(ticket, SecretStr)
                or not compare_digest(hashlib.sha256(ticket.get_secret_value().encode()).hexdigest(),
                                      captured.ticket_digest)):
            raise Problem(403, "ASSISTANT_TICKET_DENIED", "the original private admitted turn ticket is required")
        if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "agent"
                or captured.binding_id != binding_id or principal.tenant_id != captured.tenant_id
                or principal.oid != captured.agent_oid):
            raise Problem(403, "ASSISTANT_BINDING_DENIED", "the ticket belongs to another agent or work")
        principal.require(captured.team_id, action)
        return captured


def ticket_index(store):
    # Created during server construction, before request/worker concurrency begins.
    index = getattr(store, "_turn_ticket_index", None)
    if index is None:
        index = TurnTicketIndex()
        store._turn_ticket_index = index
    return index


def restore_turn_tickets(store, inventory):
    """Use the already validated startup inventory, not per-request Table lookups."""
    from .behavior_assistant import _Session, key
    from .behavior_work_policy import WorkRegistration

    records = {}
    for row in inventory.rows.values():
        data = row.payload["data"]
        if data.get("kind") == "steady_work_registration" and data.get("turn_authorization") is not None:
            record = _parse(data, WorkRegistration)
            records[record.binding.binding_id] = record
    captured = []
    for row_id, row in inventory.rows.items():
        data = row.payload["data"]
        if data.get("turn_authorization_digest") is None or data.get("vm_turn_ticket_id") is not None:
            continue
        session = _parse(data, _Session)
        record = records.get(session.binding_id)
        if record is None or row_id != key("session", session.assignment.session_id):
            raise Problem(403, "ASSISTANT_TICKET_DENIED", "the retained turn ticket has no original admission")
        captured.append((record, session))
    index = ticket_index(store)
    for record, session in captured:
        index.publish(record, session)
