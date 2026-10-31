"""Wire types naming one Agent VM and its turn address; never a ticket or receipt issuer."""

from typing import Annotated

from pydantic import Field, SecretBytes

from . import behavior_wire as wire


class AgentVmBinding(wire.WireModel):
    """API-managed logical session generation, not a PID, turn claim, or liveness proof."""

    team_id: wire.Identifier
    agent_id: wire.Identifier
    hosted_session_id: wire.Identifier
    hosted_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    vm_generation: wire.Identifier


class AgentWorkerContext(wire.WireModel):
    """Observed by the authenticated bootstrap/connection, never chosen by a turn payload."""

    vm: AgentVmBinding
    process_id: wire.Identifier
    control_connection_id: wire.Identifier
    caller_oid: wire.Identifier


class AgentTurnAddress(wire.WireModel):
    conversation_id: wire.Identifier
    sdk_session_id: wire.Identifier
    request_id: wire.Identifier


class AgentTurnDelivery(wire.WireModel):
    address: AgentTurnAddress
    payload: Annotated[SecretBytes, Field(min_length=1, max_length=65536)]
