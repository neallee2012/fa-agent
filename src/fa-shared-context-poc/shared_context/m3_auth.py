"""M3 principals: allowlisted agents or one explicitly configured delegated human.

The client restriction supplements, never replaces, the human OID. This verifier
does not register users/clients or grant access to the existing agent-only routes.
Decision services must require HumanApprover and recheck its OID and team.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from fastapi import Request

from .auth import Actor, AuthSettings, JwtVerifier
from .errors import Problem
from .models import ID_PATTERN


APPROVAL_SCOPE = "Artifacts.Approve"


@dataclass(frozen=True)
class HumanApprover:
    oid: str
    team: str


@dataclass(frozen=True)
class ApprovalSettings:
    approver_oid: str
    client_id: str
    team: str

    def __post_init__(self):
        for field in ("approver_oid", "client_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or str(UUID(value)) != value:
                raise ValueError(f"{field} must be a canonical UUID")
        if not isinstance(self.team, str) or not re.fullmatch(ID_PATTERN, self.team):
            raise ValueError("team must be a valid Identifier")


class M3JwtVerifier:
    def __init__(self, auth: AuthSettings, approval: ApprovalSettings, key_client=None):
        if approval.approver_oid in auth.actors:
            raise ValueError("human approver OID must not overlap any agent OID")
        self.auth = auth
        self.approval = approval
        self._tokens = JwtVerifier(auth, key_client)

    def __call__(self, request: Request) -> Actor | HumanApprover:
        claims = self._tokens.verified_claims(request)
        actor = self.auth.actors.get(claims["oid"])
        if actor is not None:
            return actor

        scopes = claims.get("scp")
        v2 = claims["iss"] == f"https://login.microsoftonline.com/{self.auth.tenant_id}/v2.0"
        version, client_claim = ("2.0", "azp") if v2 else ("1.0", "appid")
        if (
            claims["oid"] != self.approval.approver_oid
            or not isinstance(scopes, str)
            or APPROVAL_SCOPE not in scopes.split(" ")
            or ("idtyp" in claims and claims["idtyp"] != "user")
            or claims.get("ver", version) != version
            or claims.get(client_claim) != self.approval.client_id
            or any(claim in claims and claims[claim] != self.approval.client_id
                   for claim in ("appid", "azp"))
        ):
            raise Problem(403, "APPROVER_NOT_ALLOWED", "a configured delegated human approver is required")
        return HumanApprover(self.approval.approver_oid, self.approval.team)
