"""Hosted assistant entry runtime: the native lane runs in it; the legacy assistant session is retired."""

from __future__ import annotations

import os
from pathlib import Path

from . import behavior_assistant_wire as aw
from . import behavior_wire as wire
from .errors import Problem

# Keep these imports: they register CAPABILITY_TOOL_GUIDANCE entries that native turns read, and
# behavior_agent_message_tools is imported nowhere else in the Hosted process.
from .behavior_desktop_capability import ENTRY_ID as DESKTOP_ENTRY_ID
from .memory_candidate_capability import memory_candidate_capability, ENTRY_ID as MEMORY_CANDIDATE_ENTRY_ID
from .assistant_image_capability import image_delivery_capability, ENTRY_ID as IMAGE_DELIVERY_ENTRY_ID
from .behavior_agent_message_tools import agent_message_capability
from .behavior_agent_message_wire import CAPABILITY_ID as AGENT_MESSAGE_ENTRY_ID
from .behavior_shell_capability import ENTRY_ID as SHELL_ENTRY_ID
from .native_local_capabilities import LOCAL_HANDLERS
CAPABILITY_HANDLERS = {
    DESKTOP_ENTRY_ID: LOCAL_HANDLERS[DESKTOP_ENTRY_ID],
    MEMORY_CANDIDATE_ENTRY_ID: memory_candidate_capability,
    IMAGE_DELIVERY_ENTRY_ID: image_delivery_capability,
    AGENT_MESSAGE_ENTRY_ID: agent_message_capability,
    SHELL_ENTRY_ID: LOCAL_HANDLERS[SHELL_ENTRY_ID],
}


from .behavior_hosted import HostedRuntime


class AssistantHostedRuntime(HostedRuntime):

    @classmethod
    def from_environment(cls):
        from azure.identity import ManagedIdentityCredential
        from .behavior_hosted import BehaviorExecutorClient, ChecksumSupervisor
        if os.environ.get("BEHAVIOR_ASSISTANT_PROFILE") != aw.PROFILE:
            raise Problem(403, "ASSISTANT_PROFILE_DISABLED", "assistant-v1.2 requires explicit opt-in")
        try:
            identity = wire.HostedIdentity(
                agent_id=os.environ["FOUNDRY_AGENT_ID"], agent_name=os.environ["FOUNDRY_AGENT_NAME"],
                agent_version=os.environ["FOUNDRY_AGENT_VERSION"], session_id=os.environ["FOUNDRY_AGENT_SESSION_ID"],
            )
            root = Path(os.environ["HOME"]) / ".fa-assistant-v12"
            if not root.is_absolute():
                raise ValueError()
            os.environ["BEHAVIOR_ASSISTANT_RUNTIME_PATH"]  # the native SDK reads it; still required here
            supervisor = ChecksumSupervisor(root)
            credential = ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID") or None)
            api = BehaviorExecutorClient(os.environ["BEHAVIOR_API_URL"], os.environ["BEHAVIOR_API_AUDIENCE"], credential)
        except (ValueError, KeyError, OSError):
            raise Problem(403, "ASSISTANT_PROFILE_DISABLED", "exact private runtime and API configuration required") from None
        from .behavior_model_direct_probe import start as start_model_probe
        start_model_probe(credential)
        return cls(identity, supervisor, api)
