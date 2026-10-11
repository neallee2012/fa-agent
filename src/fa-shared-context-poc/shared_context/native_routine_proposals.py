"""An owner's allow on a routine approval card writes that card's one daily definition before the decision is recorded.

The assistant only drafts (``propose_routine`` raises the existing approval card); nothing it says and no chat reply
counts as approval. The write happens only in the owner's own ``/decisions`` request, as that owner, from the card's
saved draft. Its IDs come from the card, so a resent decision reads the same definition back instead of writing a
second one, and a decision is recorded only after the definition is saved.
"""

from .errors import Problem
from .native_routine_auth import routine_clock_enabled
from .native_routine_wire import routine_proposal_change, routine_proposals_enabled


class RoutineProposalDecisions:
    def __init__(self, hitl, composition, *, environ=None):
        self.hitl, self.composition, self.environ = hitl, composition, environ

    def enabled(self):
        from .behavior_native_turn_api import configured_native_turns
        return (routine_proposals_enabled(self.environ) and routine_clock_enabled(self.environ)
                and configured_native_turns())

    def before(self, principal, command):
        """Runs in the owner's decision request before it is recorded; other cards and choices pass unchanged."""
        if command.choice != "allow_once" or self.hitl.category(command.pending_id) != "routine":
            return
        view = self.hitl.read(principal, command.pending_id)
        if view.decision is not None:
            # The first recorded decision replays, or refuses a different one, without another write.
            return
        if not self.enabled():
            raise Problem(409, "NATIVE_ROUTINE_PROPOSALS_DISABLED",
                          "routine proposals need native routines, the API clock and native turns")
        # The decision's own checks, made before the write so a refused decision leaves no schedule behind.
        if view.approval.pending_digest != command.pending_digest:
            raise Problem(409, "NATIVE_HITL_ACTION_CHANGED", "the decision must bind the original complete pending action")
        if view.status != "pending":
            raise Problem(409, "NATIVE_HITL_STATE_CONFLICT", "the original card already has a decision or observation")
        if view.authorization is not None:
            raise Problem(409, "NATIVE_HITL_ALREADY_AUTHORIZED", "the original call already crossed its authorization point")
        if view.expired:
            raise Problem(410, "NATIVE_HITL_EXPIRED", "an expired card cannot schedule a routine")
        if view.turn_closed:
            raise Problem(409, "NATIVE_HITL_TURN_CLOSED", "the original SDK wait has ended")
        rules = self.hitl.rules(principal)
        if (rules.revision, rules.etag) != (command.expected_rules_revision, command.expected_rules_etag):
            raise Problem(409, "NATIVE_HITL_RULES_CHANGED", "read the current rules and decide again")
        from .behavior_native_turn_api import configured_native_turns
        from .native_routines import NativeRoutineService
        action = view.pending.command.action
        NativeRoutineService(self.composition, enabled=configured_native_turns()).publish(
            principal, action.call.agent_id, routine_proposal_change(action, view.pending.pending_id))
