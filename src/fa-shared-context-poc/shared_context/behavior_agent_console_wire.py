"""SDK-independent, secret-free console connection of one onboarded Agent (read by the local 8765 console)."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_wire import Digest, Identifier, WireModel
from .behavior_work_policy import SteadyWorkPolicy

MAX_CONSOLE_CONNECTION_BYTES = 64 * 1024


class AgentConsoleConnection(WireModel):
    schema_version: Literal["fa-agent-console-connection-v1"] = "fa-agent-console-connection-v1"
    agent_id: Identifier
    display_name: Annotated[str, Field(min_length=1, max_length=64)]
    backend_configuration_sha256: Digest
    standing_policy: SteadyWorkPolicy
    standing_policy_sha256: Digest

    @model_validator(mode="after")
    def exact_policy(self):
        p = self.standing_policy
        if (p.agent_id != self.agent_id or p.sha256 != self.standing_policy_sha256
                or p.configuration_sha256 != self.backend_configuration_sha256):
            raise ValueError("the connection carries exactly the Agent's own installed policy")
        return self
