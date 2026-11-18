"""One admitted ticket, one start CAS and one immutable end; no scheduler or agent loop."""

from typing import Literal


from .behavior_operations import _Model, control_key
from .behavior_store import ControlRequest
from .behavior_wire import Digest
from .behavior_turn_wire import TurnHandle, TurnStartReceipt, TurnTicket
from .errors import Problem
from .source_materials import digest


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

