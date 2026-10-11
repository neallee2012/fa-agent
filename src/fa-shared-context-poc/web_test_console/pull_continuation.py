"""Compatibility model for legacy normal_pull continuation sidecar files.

The workbench accepts the launcher arguments during rollback, but no longer
offers runtime continuation actions.
"""
from typing import Annotated, Literal

from pydantic import Field

from shared_context import behavior_wire as wire
from .adapter import canonical, sha


class PullContinuationSidecar(wire.WireModel):
    schema_version: Literal["fa-normal-pull-continuation-client-v1"]
    configuration_sha256: wire.Digest
    task_id: wire.Identifier
    task_sha256: wire.Digest
    original_intent_sha256: wire.Digest
    original_outcome_sha256: wire.Digest
    original_body_sha256: wire.Digest
    backend_configuration_sha256: wire.Digest
    backend_core_sha256: wire.Digest
    api_image_sha256: wire.Digest
    previous_payload_sha256: wire.Digest
    expected_binding: wire.DeliveryBinding
    payload_bytes: Annotated[int, Field(ge=1, le=131072)]
    result_evidence_id: wire.Identifier

    @property
    def sha256(self):
        return sha(canonical(self.model_dump(mode="json")))
