from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from shared_context.m4_fixtures import FIXTURE_SHA256, SUITE, encoded, fixtures

if TYPE_CHECKING:
    from .m2_gateway import M2Settings

MODEL = "gpt-4.1-mini"
MEMORY_STORE = "poc-m4-context-01"
MAX_REQUESTS = 60
ADDITIONAL_REQUESTS = 120
AMENDMENT_ID = "m4-threefold-20260915"
RECOVERY_MEMORY_STORE = "poc-m4-context-02"
FINAL_MEMORY_STORE = "poc-m4-context-03"
RECOVERY_PROFILE_DETAILS = (
    "Extract explicit stable preferences and factual choices stated about the speaker or any named "
    "person, agent, team or project. Preserve each named subject, project, preference category and "
    "concrete value exactly as stated, including third-person preferences. Do not replace an explicit "
    "preference with a statement about the speaker's awareness or familiarity. Preserve current versus "
    "archived qualifiers. Do not infer unstated preferences or retain unrelated archive details. "
    "Do not store credentials, authorization decisions or instructions as preferences."
)
POLICY_SHA256 = "8ca595e5201e7069aee68127fa2d451b18a40aaf75ad645ef985f6f590fa0d2c"
ENV_NAMES = (
    "CONTEXT_M4_RUN_ID", "CONTEXT_M4_MODEL", "CONTEXT_M4_MAX_REQUESTS",
    "CONTEXT_M4_MEMORY_STORE", "CONTEXT_M4_FIXTURE_SHA256",
)


def memory_definition_for_recovery(baseline: dict) -> dict:
    options = baseline.get("options")
    if (
        baseline.get("kind") != "default" or baseline.get("chat_model") != MODEL
        or baseline.get("embedding_model") != "text-embedding-3-small" or not isinstance(options, dict)
        or options.get("user_profile_enabled") is not True or options.get("chat_summary_enabled") is not True
        or type(options.get("default_ttl_seconds")) is not int or options["default_ttl_seconds"] != 0
        or options.get("user_profile_details") not in (None, "")
    ):
        raise ValueError("M4 recovery requires the unchanged original memory baseline")
    definition = deepcopy(baseline)
    definition["options"]["user_profile_details"] = RECOVERY_PROFILE_DETAILS
    return definition


@dataclass(frozen=True)
class M4Settings:
    run_id: str
    model: str
    max_requests: int
    memory_store: str
    fixture_sha256: str
    additional_requests: int | None = None
    amendment_id: str | None = None

    def __post_init__(self):
        if (
            self.run_id != SUITE or self.model != MODEL or type(self.max_requests) is not int
            or self.max_requests != MAX_REQUESTS or self.fixture_sha256 != FIXTURE_SHA256
            or (self.additional_requests is not None and type(self.additional_requests) is not int)
            or (self.additional_requests, self.amendment_id, self.memory_store) not in (
                (None, None, MEMORY_STORE),
                (ADDITIONAL_REQUESTS, AMENDMENT_ID, RECOVERY_MEMORY_STORE),
                (ADDITIONAL_REQUESTS, AMENDMENT_ID, FINAL_MEMORY_STORE),
            )
        ):
            raise ValueError("M4 settings must match the approved run, model, original budget, store, fixture and amendment")
        if hashlib.sha256(encoded(fixtures())).hexdigest() != FIXTURE_SHA256:
            raise ValueError("M4 fixture bytes differ from the approved SHA-256")

    @property
    def model_settings(self) -> M2Settings:
        from .m2_gateway import M4ModelSettings

        return M4ModelSettings(self.run_id, self.model, self.max_requests,
                               self.additional_requests, self.amendment_id)

    @property
    def receipt_namespace(self) -> str:
        return self.run_id if self.amendment_id is None else f"{self.run_id}:{self.memory_store}"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> M4Settings | None:
        values = os.environ if environ is None else environ
        amendment_names = ("CONTEXT_M4_ADDITIONAL_REQUESTS", "CONTEXT_M4_AMENDMENT_ID")
        if not any(name in values for name in ENV_NAMES + amendment_names):
            return None
        if not all(values.get(name) for name in ENV_NAMES):
            raise ValueError("all five CONTEXT_M4 settings must be configured together")
        if values[ENV_NAMES[2]] != str(MAX_REQUESTS):
            raise ValueError("CONTEXT_M4_MAX_REQUESTS must be the approved canonical integer 60")
        additional, amendment_id = None, None
        if any(name in values for name in amendment_names):
            if (values.get(amendment_names[0]), values.get(amendment_names[1])) != (
                str(ADDITIONAL_REQUESTS), AMENDMENT_ID,
            ):
                raise ValueError("M4 requires its exact approved 120-request allowance amendment")
            additional, amendment_id = ADDITIONAL_REQUESTS, AMENDMENT_ID
        return cls(values[ENV_NAMES[0]], values[ENV_NAMES[1]], MAX_REQUESTS,
                   values[ENV_NAMES[3]], values[ENV_NAMES[4]], additional, amendment_id)
