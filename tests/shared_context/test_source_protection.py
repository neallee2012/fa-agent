"""Replay the observed ARM response shapes with synthetic scope/time/inventory.

The real findings have no ETags and an incomplete OTHER-deleter inventory.
These offline tests do not assert that the deployed account is protection-ready.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from test_source_materials import CONFIG, NOW, code, native_state
from shared_context.errors import Problem
from shared_context.source_materials import (
    InventoryProtectionVerifier, NativeConfigurationEvidence, configuration_evidence_fingerprint,
)

ACCOUNT = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/synthetic/providers/Microsoft.Storage/storageAccounts/synthetic"
BLOB_ID = ACCOUNT + "/blobServices/default"
POLICY_ID = ACCOUNT + "/managementPolicies/default"
BLOB_PROPERTIES = {
    "containerDeleteRetentionPolicy": {"enabled": True, "days": 7},
    "cors": {"corsRules": []},
    "deleteRetentionPolicy": {"allowPermanentDelete": False, "enabled": True, "days": 7},
    "isVersioningEnabled": True,
}


def evidence(resource_kind, *, status_code=None, body=None, resource_id=None, **changes):
    resource_id = resource_id or (BLOB_ID if resource_kind == "blob-service" else POLICY_ID)
    if status_code is None:
        status_code = 200 if resource_kind == "blob-service" else 404
    if body is None:
        body = ({"id": resource_id, "properties": BLOB_PROPERTIES} if resource_kind == "blob-service"
                else {"error": {"code": "ManagementPolicyNotFound", "message": "No ManagementPolicy found"}})
    return NativeConfigurationEvidence.from_response(**{
        "resource_kind": resource_kind, "resource_id": resource_id, "api_version": "2025-06-01",
        "status_code": status_code, "response_body": body, "request_id": "synthetic-native-request",
        "observed_at": NOW - timedelta(minutes=1), "valid_until": NOW + timedelta(hours=1), **changes,
    })


def no_etag_state(**changes):
    blob, policy = evidence("blob-service"), evidence("management-policy")
    state = native_state(**{
        "blob_service_etag": None, "management_policy_etag": None,
        "blob_service_evidence": blob, "management_policy_evidence": policy, **changes,
    })
    fingerprint = configuration_evidence_fingerprint(
        blob_service_etag=state.blob_service_etag, management_policy_etag=state.management_policy_etag,
        blob_service_evidence=state.blob_service_evidence, management_policy_evidence=state.management_policy_evidence,
        automatic_delete_consumers=state.automatic_delete_consumers, inventory_version=state.inventory_version,
    )
    return replace(state, evidence_sha256=fingerprint)


def verify(state, config=None):
    config = config or CONFIG.model_copy(update={"evidence_sha256": state.evidence_sha256})
    InventoryProtectionVerifier(lambda: state, clock=lambda: NOW).verify(config)


def test_real_response_shapes_without_etags_use_typed_property_and_explicit_absence_fingerprints():
    state = no_etag_state()
    assert state.blob_service_evidence.status_code == 200
    assert state.management_policy_evidence.status_code == 404
    assert state.management_policy_evidence.error_code == "ManagementPolicyNotFound"
    assert state.management_policy_evidence.management_policy() == {"rules": []}
    verify(state)
    assert state.blob_service_etag is None and state.management_policy_etag is None


def test_observation_fingerprints_do_not_turn_incomplete_deleter_inventory_into_ready():
    state = no_etag_state(automatic_delete_consumers=None, inventory_version=None)
    with pytest.raises(Problem) as incomplete:
        verify(state)
    assert code(incomplete) == "SOURCE_PROTECTION_UNVERIFIED"


def test_existing_policy_without_etag_preserves_the_complete_native_policy():
    document = {"rules": [{"name": "disabled-delete", "enabled": False, "definition": {
        "actions": {"baseBlob": {"delete": {"daysAfterModificationGreaterThan": 7}}},
    }}]}
    observation = evidence("management-policy", status_code=200, body={
        "id": POLICY_ID, "properties": {"policy": document, "lastModifiedTime": "2026-09-15T12:00:00Z"},
    })
    state = no_etag_state(management_policy_evidence=observation, management_policy=document)
    verify(state)
    assert observation.management_policy() == document
    assert observation.kind == "properties"


@pytest.mark.parametrize("status,body", [
    (403, {"error": {"code": "AuthorizationFailed"}}),
    (500, {"error": {"code": "ManagementPolicyNotFound"}}),
    (404, {"error": {"code": "ResourceNotFound"}}),
    (404, {}),
    (204, {}),
    ("404", {"error": {"code": "ManagementPolicyNotFound"}}),
])
def test_only_exact_status_and_native_error_code_can_establish_policy_absence(status, body):
    with pytest.raises(Problem) as unknown:
        evidence("management-policy", status_code=status, body=body)
    assert code(unknown) == "SOURCE_PROTECTION_UNVERIFIED"


def test_blob_service_absence_is_not_a_configuration_or_versioning_success():
    with pytest.raises(Problem):
        evidence("blob-service", status_code=404, body={"error": {"code": "ManagementPolicyNotFound"}})


@pytest.mark.parametrize("updates", [
    {"blob_service_evidence": None},
    {"management_policy_evidence": None},
    {"blob_service_etag": ""},
    {"management_policy_etag": "*"},
    {"blob_service_etag": '"fabricated-etag"'},
    {"automatic_delete_consumers": None},
    {"automatic_delete_consumers": ("unknown-expiry-worker",)},
    {"inventory_version": None},
    {"inventory_version": " "},
])
def test_missing_unknown_or_fabricated_native_evidence_never_enables_protection(updates):
    state = no_etag_state(**updates)
    with pytest.raises(Problem) as invalid:
        verify(state)
    assert code(invalid) == "SOURCE_PROTECTION_UNVERIFIED"


def test_property_evidence_cannot_override_disabled_versioning_or_hide_delete_rules():
    disabled = evidence("blob-service", body={"id": BLOB_ID, "properties": {
        **BLOB_PROPERTIES, "isVersioningEnabled": False,
    }})
    with pytest.raises(Problem) as mismatch:
        verify(no_etag_state(blob_service_evidence=disabled))
    assert code(mismatch) == "SOURCE_PROTECTION_UNVERIFIED"
    rules = {"rules": [{"enabled": True, "definition": {"actions": {
        "version": {"delete": {"daysAfterCreationGreaterThan": 7}},
    }}}]}
    policy = evidence("management-policy", status_code=200, body={"id": POLICY_ID, "properties": {"policy": rules}})
    for normalized in ({"rules": []}, rules):
        with pytest.raises(Problem) as unsafe:
            verify(no_etag_state(management_policy_evidence=policy, management_policy=normalized))
        assert code(unsafe) == "SOURCE_PROTECTION_UNVERIFIED"


def test_proof_fingerprint_target_and_freshness_are_revalidated():
    original = no_etag_state()
    blob = original.blob_service_evidence
    for changes in (
        {"fingerprint": "f" * 64},
        {"properties": {**BLOB_PROPERTIES, "isVersioningEnabled": False}},
        {"resource_id": BLOB_ID.replace("storageAccounts/synthetic", "storageAccounts/different")},
        {"valid_until": NOW},
        {"observed_at": NOW + timedelta(seconds=1)},
    ):
        invalid = replace(original, blob_service_evidence=blob.model_copy(update=changes))
        with pytest.raises(Problem) as denied:
            verify(invalid)
        assert code(denied) == "SOURCE_PROTECTION_UNVERIFIED"


def test_native_body_id_and_requested_resource_must_agree():
    with pytest.raises(Problem) as wrong:
        evidence("blob-service", body={"id": BLOB_ID.replace("synthetic", "other"), "properties": BLOB_PROPERTIES})
    assert code(wrong) == "SOURCE_PROTECTION_UNVERIFIED"


def test_valid_fingerprint_for_another_account_or_subscription_cannot_match_container_binding():
    foreign = BLOB_ID.replace("storageAccounts/synthetic", "storageAccounts/other")
    with pytest.raises(Problem) as target:
        verify(no_etag_state(blob_service_evidence=evidence("blob-service", resource_id=foreign)))
    assert code(target) == "SOURCE_PROTECTION_UNVERIFIED"
    moved_policy = POLICY_ID.replace("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222")
    with pytest.raises(Problem) as account:
        verify(no_etag_state(management_policy_evidence=evidence("management-policy", resource_id=moved_policy)))
    assert code(account) == "SOURCE_PROTECTION_UNVERIFIED"


def test_configuration_version_generation_and_bundle_hash_are_not_replaced_by_observation_hashes():
    state = no_etag_state()
    for key in ("generation", "configuration_version"):
        with pytest.raises(Problem) as binding:
            verify(state, CONFIG.model_copy(update={"evidence_sha256": state.evidence_sha256, key: "changed"}))
        assert code(binding) == "SOURCE_PROTECTION_CHANGED"
    unbound = replace(state, evidence_sha256="a" * 64)
    with pytest.raises(Problem) as fingerprint:
        verify(unbound)
    assert code(fingerprint) == "SOURCE_PROTECTION_CHANGED"


def test_observation_times_are_not_atomic_versions_and_do_not_change_property_fingerprints():
    first = evidence("blob-service")
    later = evidence("blob-service", request_id="a-different-native-request",
                     observed_at=NOW, valid_until=NOW + timedelta(hours=2))
    assert later.fingerprint == first.fingerprint
    assert later.observed_at != first.observed_at and later.request_id != first.request_id
    # Separate observations may be fresh together, without claiming an atomic ARM read.
    verify(no_etag_state(blob_service_evidence=later))


def test_genuine_etag_can_coexist_with_property_evidence_but_must_match_it():
    observed = evidence("blob-service", etag='"actual-provider-etag"')
    verify(no_etag_state(blob_service_etag=observed.etag, blob_service_evidence=observed))
    with pytest.raises(Problem):
        verify(no_etag_state(blob_service_evidence=observed))
