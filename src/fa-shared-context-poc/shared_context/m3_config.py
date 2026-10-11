from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping

from pydantic import TypeAdapter

from .config import BackupSettings
from .m3_auth import ApprovalSettings
from .m2_gateway import M2Settings
from .models import Identifier

MAX_POLICY_ARTIFACTS = 4


def model_settings_from_env(environ: Mapping[str, str] | None = None) -> M2Settings | None:
    values = os.environ if environ is None else environ
    names = ("CONTEXT_M3_RUN_ID", "CONTEXT_M3_MODEL", "CONTEXT_M3_MAX_REQUESTS")
    if any(name in values for name in ("CONTEXT_M3_ADDITIONAL_REQUESTS", "CONTEXT_M3_AMENDMENT_ID")):
        raise ValueError("M3 does not support an allowance amendment")
    if not any(name in values for name in names):
        return None
    if (not all(values.get(name) for name in names) or not values[names[0]].startswith("m3-")
            or values[names[1]] != "gpt-4.1-mini" or re.fullmatch(r"[1-9][0-9]{0,2}", values[names[2]]) is None
            or int(values[names[2]]) > 120):
        raise ValueError("M3 model settings require their own m3- run ID, gpt-4.1-mini and a 1..120 attempt limit")
    return M2Settings(values[names[0]], values[names[1]], int(values[names[2]]))


@dataclass(frozen=True)
class M3Settings:
    storage: BackupSettings
    approval: ApprovalSettings
    policy_artifacts: tuple[str, ...] = ()

    def __post_init__(self):
        if len(self.policy_artifacts) > MAX_POLICY_ARTIFACTS or len(set(self.policy_artifacts)) != len(self.policy_artifacts):
            raise ValueError("M3 policy artifact IDs must be unique and at most four")
        for artifact in self.policy_artifacts:
            TypeAdapter(Identifier).validate_python(artifact, strict=True)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> M3Settings | None:
        values = os.environ if environ is None else environ
        names = (
            "CONTEXT_M3_BLOB_ENDPOINT", "CONTEXT_M3_CONTAINER", "CONTEXT_M3_APPROVER_OID",
            "CONTEXT_M3_CLIENT_ID", "CONTEXT_M3_TEAM",
        )
        policy_name = "CONTEXT_M3_POLICY_ARTIFACTS"
        if not any(name in values for name in (*names, policy_name)):
            return None
        if not all(values.get(name) for name in names):
            raise ValueError("all five M3 storage and human approval settings must be configured together")
        return cls(
            BackupSettings(values[names[0]].removesuffix("/"), values[names[1]]),
            ApprovalSettings(values[names[2]], values[names[3]], values[names[4]]),
            tuple(values[policy_name].split(",")) if values.get(policy_name) else (),
        )
