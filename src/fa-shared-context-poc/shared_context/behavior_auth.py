"""Server-derived v1.2 principals; existing M3 consent grants no new access."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fastapi import Request

from .auth import AuthSettings, JwtVerifier
from .errors import Problem
from .m3_auth import ApprovalSettings

if TYPE_CHECKING:
    from .behavior_team_registry import TeamRegistry

READ_SCOPE = "Behavior.Read"
APPROVAL_SCOPE = "Behavior.Approve"
AGENT_ROLE = "Behavior.Agent"
BACKUP_SCOPE = "Behavior.Backup"
RESTORE_SCOPE = "Behavior.Restore"
Action = Literal["read", "submit", "acknowledge", "approve", "execute", "consume", "backup", "restore"]
AgentDenial = Literal["delegated_token", "not_app_only", "agent_unregistered", "agent_inactive"]


def _agent_denied(reason: AgentDenial, message: str) -> Problem:
    # Fixed reason code only: never the token, oid or any claim value.
    logging.getLogger(__name__).warning("BEHAVIOR_AGENT_AUTH_DENIED reason=%s", reason)
    return Problem(403, "BEHAVIOR_ACTOR_NOT_ALLOWED", message)


@dataclass(frozen=True)
class BehaviorAuthSettings:
    human_oid: str
    client_id: str
    team: str
    # ADR-0008 remote console: one more delegated client, same human/scope/Team checks; never a new grant.
    extra_client_id: str | None = None

    def __post_init__(self):
        ApprovalSettings(self.human_oid, self.client_id, self.team)
        if self.extra_client_id is not None:
            ApprovalSettings(self.human_oid, self.extra_client_id, self.team)
            if self.extra_client_id == self.client_id:
                raise ValueError("the extra delegated client must differ from the configured client")

    @property
    def delegated_clients(self) -> tuple[str, ...]:
        return (self.client_id,) if self.extra_client_id is None else (self.client_id, self.extra_client_id)


@dataclass(frozen=True)
class BehaviorPrincipal:
    tenant_id: str
    oid: str
    team: str
    kind: Literal["human", "agent"]
    actor_id: str | None
    actions: frozenset[Action]

    @property
    def source_actor_id(self) -> str:
        if self.kind == "agent" and isinstance(self.actor_id, str) and self.actor_id:
            return self.actor_id
        if self.kind == "human" and self.actor_id is None:
            return "human-" + self.oid
        raise Problem(503, "BEHAVIOR_IDENTITY_INVALID", "server identity has no valid source binding")

    def require(self, team: str, action: Action) -> None:
        if team != self.team or action not in self.actions:
            raise Problem(403, "BEHAVIOR_ACCESS_DENIED", "principal cannot perform this action in the requested team")


class BehaviorJwtVerifier:
    def __init__(self, auth: AuthSettings, settings: BehaviorAuthSettings, key_client=None, *,
                 teams: TeamRegistry | None = None):
        if settings.human_oid in auth.actors:
            raise ValueError("human and agent identities must be distinct")
        if any(actor.actor.startswith("human-") for actor in auth.actors.values()):
            raise ValueError("agent names cannot occupy the reserved human source namespace")
        if not any(actor.team == settings.team for actor in auth.actors.values()):
            raise ValueError("the configured human team must exist in the trusted actor map")
        self.auth = auth
        self.settings = settings
        self.tokens = JwtVerifier(auth, key_client)
        if teams is not None and (teams.tenant_id != auth.tenant_id
                or teams.legacy_owner.oid != settings.human_oid or teams.legacy_owner.team_id != settings.team):
            raise ValueError("Team registry must retain the original configured authority")
        self.teams = teams

    def _owner(self, request, oid):
        cached = request.scope.get("_behavior_team_owner")
        if cached is not None and cached[0] is self.teams and cached[1] == oid:
            return cached[2]
        owner = self.teams.owner(oid)
        # Peer routing and its mounted handler share this HTTP scope, never the next request.
        request.scope["_behavior_team_owner"] = self.teams, oid, owner
        return owner

    def __call__(self, request: Request) -> BehaviorPrincipal:
        claims = self.tokens.verified_claims(request)
        actor = self.auth.actors.get(claims["oid"])
        if actor is not None:
            # ADR-0006 gate C applies to configured actors too: the app-only oid admits them, no app role.
            if "scp" in claims:
                raise _agent_denied("delegated_token", "an app-only token is required for this agent identity")
            if claims.get("idtyp", "app") != "app":
                raise _agent_denied("not_app_only", "an app-only token is required for this agent identity")
            return BehaviorPrincipal(
                self.auth.tenant_id, actor.oid, actor.team, "agent", actor.actor,
                frozenset({"read", "submit", "execute", "consume"}),
            )

        scopes = claims.get("scp")
        v2 = claims["iss"] == f"https://login.microsoftonline.com/{self.auth.tenant_id}/v2.0"
        version, client_claim = ("2.0", "azp") if v2 else ("1.0", "appid")
        if "scp" not in claims and self.teams is not None and claims.get("idtyp", "app") == "app":
            # ADR-0006 gate C: an app-only token is admitted only by its registered Agent oid, no app role.
            try:
                agent = self._owner(request, claims["oid"])
            except Problem as error:
                if error.body["error"]["code"] != "TEAM_IDENTITY_DENIED":
                    raise
                agent = None
            if agent is None or agent.kind != "agent":
                raise _agent_denied("agent_unregistered", "the agent identity is not registered")
            if not agent.active:
                raise _agent_denied("agent_inactive", "the agent identity is not registered")
            return BehaviorPrincipal(
                self.auth.tenant_id, agent.oid, agent.team_id, "agent", agent.agent_id,
                frozenset({"read", "submit", "execute", "consume"}),
            )
        if "scp" not in claims and (claims.get("idtyp") == "app" or "roles" in claims):
            reason = "agent_unregistered" if claims.get("idtyp", "app") == "app" else "not_app_only"
            raise _agent_denied(reason, "the agent identity is not registered")
        client = claims.get(client_claim)
        if (not isinstance(scopes, str) or READ_SCOPE not in scopes.split(" ")
                or ("idtyp" in claims and claims["idtyp"] != "user")
                or claims.get("ver", version) != version
                or not isinstance(client, str) or client not in self.settings.delegated_clients
                or any(key in claims and claims[key] != client for key in ("appid", "azp"))):
            raise Problem(403, "BEHAVIOR_HUMAN_NOT_ALLOWED", "a configured delegated human and behavior scope are required")
        if self.teams is None:
            if claims["oid"] != self.settings.human_oid:
                raise Problem(403, "BEHAVIOR_HUMAN_NOT_ALLOWED", "the configured delegated human is required")
            team = self.settings.team
        else:
            try:
                owner = self._owner(request, claims["oid"])
            except Problem as error:
                if error.body["error"]["code"] != "TEAM_IDENTITY_DENIED":
                    raise
                raise Problem(403, "BEHAVIOR_HUMAN_NOT_ALLOWED", "the original Team identity is unavailable") from None
            if owner is None or not owner.active or owner.kind != "owner":
                raise Problem(403, "BEHAVIOR_HUMAN_NOT_ALLOWED", "an active registered Team owner is required")
            team = owner.team_id
        actions: set[Action] = {"read", "submit", "acknowledge"}
        if APPROVAL_SCOPE in scopes.split(" "):
            actions.add("approve")
        if BACKUP_SCOPE in scopes.split(" "):
            actions.add("backup")
        if RESTORE_SCOPE in scopes.split(" "):
            actions.add("restore")
        return BehaviorPrincipal(
            self.auth.tenant_id, claims["oid"], team,
            "human", None, frozenset(actions),
        )
