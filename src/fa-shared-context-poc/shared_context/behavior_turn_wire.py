"""Admitted VM turns and their receipts; no storage, SDK runtime or transport."""

from datetime import timezone
import hashlib
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .behavior_agent_worker import AgentTurnAddress, AgentVmBinding, AgentWorkerContext
from .behavior_agent_message_wire import PeerTeamReference
from .behavior_assistant_wire import Assignment
from .behavior_wire import Digest, Identifier, WireModel

DEFAULT_RELAY_MODEL_REQUESTS = 100


def wire_fingerprint(value):
    raw = json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


class TurnRelayContext(WireModel):
    root: Identifier
    hop: Annotated[int, Field(ge=0, le=2)]
    chain: tuple[Identifier, ...]
    deadline: AwareDatetime
    in_reply_to: Identifier | None = None

    @field_validator("deadline")
    @classmethod
    def utc(cls, value):
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def exact_chain(self):
        if len(self.chain) != self.hop or len(set(self.chain)) != len(self.chain):
            raise ValueError("the relay chain must contain each waiting ancestor exactly once")
        return self


class TurnTicket(WireModel):
    schema_version: Literal["fa-vm-turn-ticket-v1"] = "fa-vm-turn-ticket-v1"
    tenant_id: Identifier
    vm: AgentVmBinding
    address: AgentTurnAddress
    assignment: Assignment
    owner_oid: Identifier
    agent_oid: Identifier
    issuer_oid: Identifier
    source: Literal["human", "agent"]
    request_sha256: Digest
    relay: TurnRelayContext | None = None
    model_call_limit: Annotated[int, Field(ge=1)] | None = None
    refs: Annotated[tuple[PeerTeamReference, ...], Field(max_length=8)] = Field(
        default=(), exclude_if=lambda value: not value)

    @property
    def sha256(self):
        return wire_fingerprint(self)

    @model_validator(mode="after")
    def original_turn(self):
        instruction = self.assignment.task.instruction
        if (instruction is None or instruction.conversation_id != self.address.conversation_id
                or self.assignment.sdk_session_id != self.address.sdk_session_id
                or instruction.hosted_agent_version != self.vm.hosted_agent_version):
            raise ValueError("the ticket must retain the original instruction, SDK session and Hosted version")
        if self.source == "human":
            if self.issuer_oid != self.owner_oid or self.model_call_limit is not None:
                raise ValueError("a human turn retains its owner and does not inherit a relay model limit")
        elif self.relay is None or self.relay.hop == 0 or self.model_call_limit is None:
            raise ValueError("an agent-issued turn requires its original relay context and model limit")
        if self.relay is not None and self.vm.agent_id in self.relay.chain:
            raise ValueError("a relay cannot call an ancestor waiting for its result")
        if len(set(self.refs)) != len(self.refs) or any(
                (ref.tenant_id, ref.team_id) != (self.tenant_id, self.vm.team_id) for ref in self.refs):
            raise ValueError("message references must remain distinct and inside the original Team")
        return self


class TurnHandle(WireModel):
    ticket_id: Digest
    ticket_sha256: Digest


class TurnStartReceipt(WireModel):
    schema_version: Literal["fa-vm-turn-start-v1"] = "fa-vm-turn-start-v1"
    ticket: TurnHandle
    address: AgentTurnAddress
    job_id: Identifier
    worker: AgentWorkerContext
    actor_id: Identifier
    request_id: Identifier

    @property
    def sha256(self):
        return wire_fingerprint(self)


class StartedTurn(WireModel):
    ticket: TurnTicket
    start: TurnStartReceipt

