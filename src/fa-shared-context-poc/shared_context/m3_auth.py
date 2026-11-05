"""M3 approval settings and the delegated human principal.

Decision services must require HumanApprover and recheck its OID and team.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from .models import ID_PATTERN


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

