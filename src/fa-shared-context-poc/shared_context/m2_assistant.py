from __future__ import annotations

from .errors import Problem


class AssistantEngine:
    """Routes the assistant-v1.2 profile to its typed session; every other payload shape is rejected."""

    def __init__(self, *, behavior=None):
        self.behavior = behavior

    def dispatch(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or payload.get("profile") != "assistant-v1.2":
            raise Problem(422, "INVALID_REQUEST", "only the assistant-v1.2 profile is accepted")
        from .behavior_assistant_hosted import AssistantSession
        if not isinstance(self.behavior, AssistantSession):
            raise Problem(403, "ASSISTANT_PROFILE_DISABLED", "v1.2 requires explicit typed authority composition")
        return self.behavior.dispatch(payload)
