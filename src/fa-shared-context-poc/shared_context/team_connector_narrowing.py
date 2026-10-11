"""Per-Agent connector narrowing and each turn's ``ConnectorCapture``; SDK-independent.

A Team connector is on for every Agent of the Team. The owner can turn one off for one Agent; that choice is one
``AgentConnectorNarrowing`` row per Agent, replaced at each revision, and applies to that Agent's new conversations
only. A conversation's first turn captures the Team's enabled connectors minus the Agent's disabled ones. Each later
turn keeps those of the original connectors the Team still enables, at their current versions, so a conversation
never gains a connector and a Team disable reaches every conversation on its next turn.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from .behavior_capabilities import CAPABILITY_BASE_PROFILES
from .behavior_operations import _Model, _parse, control_key
from .behavior_store import ControlChanges, ControlRequest, ControlWrite
from .behavior_wire import Identifier
from .connector_registry import CONNECTORS, ConnectorId
from .errors import Problem
from .source_materials import digest
from .team_connector_wire import AgentConnectorsView, ConnectorCapture, SelectAgentConnectors, connector_of


class AgentConnectorNarrowing(_Model):
    kind: Literal["agent_connector_narrowing"] = "agent_connector_narrowing"
    team_id: Identifier
    agent_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    disabled_connector_ids: tuple[ConnectorId, ...]
    revision: Annotated[int, Field(ge=1)]
    request_id: Identifier
    selected_by: Identifier
    selected_at: AwareDatetime


def narrowing_key(scope_ids):
    return control_key("agent-connector-narrowing", *scope_ids)


def current_narrowing(store, policy) -> AgentConnectorNarrowing | None:
    key = narrowing_key(policy.scope_ids)
    row = store.read_snapshot((key,))[key]
    if row is None:
        return None
    value = _parse(row.payload["data"], AgentConnectorNarrowing)
    if (value.team_id, value.agent_id) != (policy.template.team_id, policy.agent_id):
        raise Problem(409, "AGENT_CONNECTORS_INVALID", "the saved connector choice belongs to another Agent")
    return value


def _view(value: AgentConnectorNarrowing | None) -> AgentConnectorsView:
    return AgentConnectorsView(revision=0 if value is None else value.revision,
                               disabled_connector_ids=() if value is None else value.disabled_connector_ids)


def _active(policy):
    if not policy.active or not policy.read_enabled:
        raise Problem(403, "WORK_POLICY_REVOKED", "current standing read and work scope is required")
    return policy


def connector_view(control, principal) -> AgentConnectorsView:
    control._human(principal)
    policy = _active(control.installed())
    return _view(current_narrowing(control.store, policy))


def save_narrowing(control, principal, command: SelectAgentConnectors, *, attempts=3) -> dict:
    """Owner only, compared against the revision the owner read. The same request ID returns its original result,
    also after later changes; the same ID with other content is refused."""
    control._human(principal, "read")
    control._human(principal, "approve")
    unknown = sorted(set(command.disabled_connector_ids) - set(CONNECTORS))
    if unknown:
        raise Problem(422, "AGENT_CONNECTOR_UNKNOWN", "no registered connector has this ID", connector=unknown[0])
    policy = _active(control.installed())
    key = narrowing_key(policy.scope_ids)
    request = ControlRequest(
        request_id=control_key("agent-connector-narrowing-request", *policy.scope_ids, command.request_id),
        request_digest=digest([policy.sha256, principal.oid, command.model_dump(mode="json")]),
        read_set=(control.policy_key, control.revocation_key, key),
    )

    def decide(rows):
        if control.installed(rows) != policy:
            raise Problem(403, "WORK_POLICY_REVOKED", "the standing scope changed while saving")
        prior = None if rows[key] is None else _parse(rows[key].payload["data"], AgentConnectorNarrowing)
        if (0 if prior is None else prior.revision) != command.expected_revision:
            raise Problem(409, "AGENT_CONNECTORS_CHANGED", "read the current connector choice and change again")
        value = AgentConnectorNarrowing(
            team_id=policy.template.team_id, agent_id=policy.agent_id,
            disabled_connector_ids=command.disabled_connector_ids, revision=command.expected_revision + 1,
            request_id=command.request_id, selected_by=principal.oid, selected_at=control.runtime._now())
        return ControlChanges(writes=(
            ControlWrite(row_id=key, mode="create" if prior is None else "replace", data=value.model_dump(mode="json")),
        ), result=_view(value).model_dump(mode="json"))

    for attempt in range(attempts):
        try:
            return control.store.transact(request, decide).payload["result"]
        except Problem as error:
            code = error.body["error"]["code"]
            if code == "BEHAVIOR_REQUEST_CONFLICT":
                raise Problem(409, "AGENT_CONNECTORS_CONFLICT",
                              "this request ID is already bound to other input; use a new request ID") from None
            # 412: another write moved the store between the read and the commit; nothing was saved.
            if error.status != 412 or code != "BEHAVIOR_CONFLICT" or attempt == attempts - 1:
                raise


def resolve_connectors(control, policy, template, original) -> ConnectorCapture | None:
    """This turn's connectors, read inside the turn's consistent read. ``original`` is the conversation's first
    capture, or None for a new conversation. None when the deployment has no Team connectors or the selected
    profile cannot compose organization tools."""
    if getattr(control.runtime, "team_connectors", None) is None or template.role.profile not in CAPABILITY_BASE_PROFILES:
        return None
    from .team_connector import current_team_connector_grants

    team = current_team_connector_grants(control.runtime)
    if original is None:
        narrowing = current_narrowing(control.store, policy)
        off = frozenset(() if narrowing is None else narrowing.disabled_connector_ids)
        kept = [grant for grant in team if connector_of(grant.entry_id) not in off]
    else:
        keep = frozenset() if original.connectors is None else original.connectors.connector_ids
        kept = [grant for grant in team if connector_of(grant.entry_id) in keep]
    return ConnectorCapture(grants=tuple(sorted(kept, key=lambda grant: grant.entry_id))) if kept else None
