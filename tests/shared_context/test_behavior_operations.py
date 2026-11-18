"""Operation description canonicalization (read by the kept GET /operation preview). The prepare/approve/execute
broker was retired with the legacy job lane (DECISIONS §12)."""

import pytest
from pydantic import ValidationError

from shared_context.behavior_operations import (
    AdapterContract, ContentDependency, OperationDescription, ResolvedResource, canonical_material,
)
from shared_context.errors import Problem

RESOURCE = ResolvedResource(resource_id="synthetic-resource", version="v1")
DESCRIPTION = OperationDescription(
    adapter_id="synthetic-tool", adapter_version="1", kind="request", method="POST",
    target="synthetic-resource", content=b"actual request A", resources=(RESOURCE,),
)

CONTRACT = AdapterContract(
    adapter_id="synthetic-tool", adapter_version="1", allowed_kinds=("request", "script", "plan"),
    allowed_methods=("POST",), allowed_targets=("synthetic-resource",),
    reservation_units=2, evidence_source="synthetic-destination", outcome_contract_version="outcome-1",
)


def test_canonicalization_binds_dependencies_resource_versions_and_adapter_version():
    one = ContentDependency(resource_id="one", version="v1", content=b"dependency one")
    two = ContentDependency(resource_id="two", version="v2", content=b"dependency two")
    first = DESCRIPTION.model_copy(update={"dependencies": (one, two)})
    assert canonical_material(first) == canonical_material(first.model_copy(update={"dependencies": (two, one)}))
    for updates in (
        {"content": b"different"}, {"method": "DELETE"}, {"target": "another-target"},
        {"adapter_version": "2"}, {"resources": (RESOURCE.model_copy(update={"version": "v2"}),)},
        {"dependencies": (one.model_copy(update={"content": b"changed"}), two)},
    ):
        assert canonical_material(first).sha256 != canonical_material(first.model_copy(update=updates)).sha256
    with pytest.raises(Problem):
        canonical_material(DESCRIPTION.model_copy(update={"canonicalization_version": "unknown"}))
    for version in ("current", "latest", "unknown", "HEAD"):
        with pytest.raises(ValidationError):
            ResolvedResource(resource_id="target", version=version)
        with pytest.raises(ValidationError):
            AdapterContract.model_validate({**CONTRACT.model_dump(), "adapter_version": version})
