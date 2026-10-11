"""Server-declared capacity snapshots; no SDK dependency or token estimation."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_wire import Digest, ModelDeployment, WireModel

ModelCapacityProfile = Literal["fa-model-capacity-v1"]
TokenLimit = Annotated[int, Field(gt=0, le=2**31 - 1)]
MODEL_CAPACITY_ERRORS = frozenset({
    "MODEL_CAPACITY_DISABLED", "MODEL_CAPACITY_INVALID", "MODEL_CAPACITY_CHANGED",
})


class DeclaredModelLimits(WireModel):
    context_window_tokens: TokenLimit | None = None
    max_prompt_tokens: TokenLimit | None = None
    max_output_tokens: TokenLimit | None = None

    @model_validator(mode="after")
    def coherent_limits(self):
        if self.context_window_tokens is not None and any(
            value is not None and value > self.context_window_tokens
            for value in (self.max_prompt_tokens, self.max_output_tokens)
        ):
            raise ValueError("An individual input or output maximum cannot exceed the declared total context window")
        return self

    def sdk_values(self):
        return {name: value for name, value in (
            ("max_context_window_tokens", self.context_window_tokens),
            ("max_prompt_tokens", self.max_prompt_tokens),
            ("max_output_tokens", self.max_output_tokens),
        ) if value is not None}


def capacity_fact_digest(model, limits):
    data = {"schema_version": "fa-model-capacity-facts-v1", "model": model,
            "limits": limits.model_dump(mode="json")}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


class ModelCapacitySnapshot(WireModel):
    schema_version: Literal["fa-model-capacity-snapshot-v1"]
    profile: ModelCapacityProfile
    source: Literal["server-curated-model-facts"]
    model: ModelDeployment
    limits: DeclaredModelLimits
    source_sha256: Digest

    @model_validator(mode="after")
    def original_facts(self):
        if self.source_sha256 != capacity_fact_digest(self.model, self.limits):
            raise ValueError("Model capacity snapshot differs from its selected model facts")
        return self

    @classmethod
    def from_declared(cls, model, limits):
        limits = DeclaredModelLimits.model_validate(limits.model_dump())
        return cls(schema_version="fa-model-capacity-snapshot-v1", profile="fa-model-capacity-v1",
                   source="server-curated-model-facts", model=model, limits=limits,
                   source_sha256=capacity_fact_digest(model, limits))
