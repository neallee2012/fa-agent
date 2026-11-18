"""One admitted ticket, one start CAS and one immutable end; no scheduler or agent loop."""

from typing import Literal


from .behavior_operations import _Model, control_key
from .behavior_store import ControlRequest
from .behavior_turn_wire import TurnHandle, TurnStartReceipt, TurnTicket
from .source_materials import digest


def turn_key(tenant_id, team_id, agent_id, request_id):
    return control_key("vm-turn-ticket", tenant_id, team_id, agent_id, request_id)


def start_key(ticket_id):
    return control_key("vm-turn-start", ticket_id)


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

