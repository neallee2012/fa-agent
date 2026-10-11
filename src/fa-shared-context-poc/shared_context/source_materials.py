"""Immutable accepted originals and bounded, verified snapshot graphs.

All identifiers below are control_key() inputs, not Table partition keys. Bind
ONE BehaviorStore to this service; reads default to its public guard-bracketed
read_snapshot. An explicitly injected reader must preserve that contract. The
authenticated context, clock, Blob client and protection verifier are SERVER
dependencies, never HTTP/model fields. The ingestion adapter must already have
applied its consent/secret-output policy; bytes here are exactly that boundary,
not a promise to preserve an upstream prompt the adapter never received.

Small originals and their event/head/receipt commit in one Table EGT. Large
originals use a durable preparation BEFORE one create-only Blob upload. Repeated
preparation only reconciles; a missing upload stays unknown. Attachment checks
the SAME protection generation in the control transaction. Unknown does not
release protection. There is deliberately no delete, GC, pin or retry API.

Snapshot boundary: capture_snapshot stores a manifest in this SAME authority;
it does not copy the referenced bytes into an independent backup. restore_snapshot
rehydrates and verifies that graph from the SAME live Table/Blob stores. Existing
guard, source/head rows, receipts and exact Blob versions must still be available.
It cannot recover a lost Table, recreate missing rows/objects or perform disaster
recovery. Successful graph reads do not complete gap 2's backup/DR portion.
For an HTTP/worker transition, use plan_snapshot first and durably retain its
SourceSnapshotPlan before commit_snapshot. Unknown commits only reconcile that
original plan; do not reconstruct a new plan/ID after a timeout.

The remaining backup/restore seam is a consistent authority export (binding,
guard revision, schema, control/receipt rows and complete necessary byte/version
set), independently retained/protected storage, and a controlled recovery adapter.
Recovery must verify completeness and preserve original request IDs, unknown
outcomes and current withdrawal/access restrictions rather than replay effects or
reactivate revoked access. No export/import, recovery cutover, RPO/RTO guarantee
or disaster-recovery evidence is implemented by this module.

Production initialization requires NativeProtectionState from a trusted
management-plane/configuration inventory reader: Blob versioning properties,
the complete management policy, inventory of OTHER automatic deletion consumers,
and evidence for the bound authority/container. Preserve real ETags when supplied.
When the provider has none, use typed NativeConfigurationEvidence with canonical,
status/target/API-bound fingerprints; never fabricate ETags. Only an explicit
404 ManagementPolicyNotFound may normalize to empty rules. All deletion paths
must be disabled and the OTHER-consumer inventory must be complete.
Configuration changes must be governed with this authority; enabling GC requires
a separate drain/migration protocol, NOT changing a flag here. This no-auto-GC
mode does not protect against a privileged administrator deleting data, and
does not claim Blob+Table ACID, WORM, native Azure PASS, or Hosted persistence.
ARM observations are separate, non-atomic reads, even with matching fingerprints.
They establish no lock, durable lease or continuous-protection guarantee.
Configuration changes still require the governed generation/migration contract.

Call plan() and durably retain its original bytes/identity before prepare/commit.
After uncertain control commit use reconcile(plan), never a new source/version.
Preparation is not admission. The accepted source's receipt is the commit point.
Source-head withdrawal is permanent for that source identity, covers historical
versions and archived snapshots, and retains bytes/receipts. It is a deny-only
access revision, not a consent grant, physical delete or permission assignment.

Optional SourceInput.expires_at is an immutable per-version UTC applicability
deadline, not a deletion schedule. It must be future at planning/admission;
the injected SERVER clock (UTC wall clock by default) gates exact reads and
every necessary-source guard, including after I/O and before composed writes.
None is omitted from input/event serialization to preserve existing request,
receipt and version hashes. A new version does not extend an older deadline.
Historical reconciliation returns receipts, never permission to read expired
bytes. Separately authorized backup/quarantine recovery does not reactivate them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Annotated, Literal, Protocol, TypeVar
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, JsonValue, ValidationError, field_validator, model_validator

from .behavior_operations import control_key
from .behavior_auth import BehaviorPrincipal
from .behavior_store import (
    MAX_INLINE_BYTES, BehaviorStore, ControlChanges, ControlRequest,
    ControlWrite, Digest, InlineMaterial,
)
from .errors import Problem
from .m3_models import BlobReceipt, MAX_ARTIFACT_BYTES
from .m3_store import BlobArtifactStore
from .models import Identifier, StrictModel
from .records import Record

MAX_GRAPH_NODES = 24
MAX_RESTORE_BYTES = 8 * 1024 * 1024
SnapshotReader = Callable[[tuple[str, ...]], Mapping[str, Record | None]]

__all__ = [
    "AuthenticatedSourceContext", "BlobLocator", "ExactSource", "InventoryProtectionVerifier",
    "NativeProtectionState", "NativeConfigurationEvidence", "NoAutoGCConfiguration", "ProtectionVerifier", "RestoredSnapshot",
    "SnapshotReader", "SourceEvent", "SourceHead", "SourceInput", "SourceMaterials",
    "SourcePlan", "SourceReference", "SourceScope", "SourceSnapshot", "SourceSnapshotPlan", "SnapshotNode", "SourceComposition",
    "SourceWithdrawalInput", "require_human_context", "require_source_head", "configuration_evidence_fingerprint",
]


class SourceModel(StrictModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False,
        ser_json_bytes="base64", val_json_bytes="base64",
    )


M = TypeVar("M", bound=SourceModel)


def canonical(value: object) -> bytes:
    """Canonical metadata only; NEVER use this to reserialize original bytes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def checked(value: M, model: type[M]) -> M:
    try:
        if not isinstance(value, model):
            raise ValueError("typed model required")
        return model.model_validate(value.model_dump())
    except (ValueError, TypeError, ValidationError):
        raise Problem(422, "SOURCE_INVALID_INPUT", "a validated source contract is required") from None


def parsed(value: object, model: type[M]) -> M:
    try:
        return model.model_validate_json(canonical(value))
    except (ValueError, TypeError, ValidationError, UnicodeError):
        raise Problem(502, "SOURCE_RECORD_INVALID", "stored source contract is invalid") from None


def control_row(snapshot: Mapping[str, Record | None], key: str, model: type[M]) -> M:
    row = snapshot.get(key)
    if row is None:
        raise Problem(409, "SOURCE_REQUIRED_MISSING", "a necessary authoritative record is missing",
                      record_id=key)
    try:
        if row.payload["row_id"] != key:
            raise KeyError(key)
        return parsed(row.payload["data"], model)
    except (KeyError, TypeError):
        raise Problem(502, "SOURCE_RECORD_INVALID", "invalid control row envelope") from None


def control_write(key: str, model: SourceModel, mode="create", material=None) -> ControlWrite:
    return ControlWrite(row_id=key, mode=mode, data=model.model_dump(mode="json"), material=material)


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise Problem(422, "SOURCE_INVALID_TIME", "a trusted timezone-aware time is required")
    return value.astimezone(timezone.utc)


def _source_expiry_utc(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
        raise ValueError("source expiry must be explicit UTC")
    return value


class SourceScope(SourceModel):
    tenant_id: Identifier
    team_id: Identifier
    visibility: Literal["team", "private"]
    owner_actor: Identifier | None = None

    @model_validator(mode="after")
    def owner_matches(self):
        if (self.visibility == "private") != (self.owner_actor is not None):
            raise ValueError("only private scope has an owner")
        return self

    @property
    def key(self) -> str:
        return control_key("source-scope", digest(self.model_dump(mode="json")))


@dataclass(frozen=True)
class AuthenticatedSourceContext:
    """Construct only after server authentication and consent/purpose mapping."""

    tenant_id: str
    team_id: str
    actor_id: str
    ingress_id: str
    allowed_purposes: tuple[str, ...]

    def scope(self, visibility: Literal["team", "private"]) -> SourceScope:
        return SourceScope(tenant_id=self.tenant_id, team_id=self.team_id,
                           visibility=visibility,
                           owner_actor=self.actor_id if visibility == "private" else None)


def authorize(context: AuthenticatedSourceContext, scope: SourceScope, purpose: str | None = None) -> None:
    if not isinstance(context, AuthenticatedSourceContext):
        raise Problem(403, "SOURCE_SCOPE_DENIED", "server-authenticated source context is required")
    scope = checked(scope, SourceScope)
    if (scope.tenant_id != context.tenant_id or scope.team_id != context.team_id
            or scope.visibility == "private" and scope.owner_actor != context.actor_id
            or purpose is not None and purpose not in context.allowed_purposes):
        raise Problem(403, "SOURCE_SCOPE_DENIED", "source scope or consented purpose does not match")


def require_human_context(principal: BehaviorPrincipal, context: AuthenticatedSourceContext,
                          scope: SourceScope) -> None:
    if not isinstance(principal, BehaviorPrincipal) or principal.kind != "human":
        raise Problem(403, "SOURCE_HUMAN_REQUIRED", "an authenticated human decision is required")
    principal.require(scope.team_id, "approve")
    principal.require(scope.team_id, "read")
    if (not isinstance(context, AuthenticatedSourceContext) or principal.tenant_id != context.tenant_id
            or principal.team != context.team_id or principal.source_actor_id != context.actor_id):
        raise Problem(403, "SOURCE_SCOPE_DENIED", "human principal and source context must match without impersonation")
    authorize(context, scope)


class SourceReference(SourceModel):
    scope: SourceScope
    source_id: Identifier
    version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    sha256: Digest
    byte_length: Annotated[int, Field(ge=0, le=MAX_ARTIFACT_BYTES)]

    @property
    def key(self) -> str:
        return control_key("source-version", self.scope.key, self.source_id, str(self.version))

    @property
    def head_key(self) -> str:
        return control_key("source-head", self.scope.key, self.source_id)


class SourceInput(SourceModel):
    source_id: Identifier
    version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    visibility: Literal["team", "private"] = "team"
    purpose: Identifier
    schema_id: Identifier
    media_type: Literal["text/plain", "text/markdown", "application/json", "application/octet-stream"]
    necessary: Annotated[tuple[SourceReference, ...], Field(max_length=MAX_GRAPH_NODES - 1)] = ()
    expires_at: datetime | None = Field(default=None, exclude_if=lambda value: value is None)

    _expiry = field_validator("expires_at")(_source_expiry_utc)

    @model_validator(mode="after")
    def unique_dependencies(self):
        if len({item.key for item in self.necessary}) != len(self.necessary):
            raise ValueError("duplicate necessary source")
        return self


class NoAutoGCConfiguration(SourceModel):
    mode: Literal["no-auto-gc"] = "no-auto-gc"
    authority_domain: Identifier
    generation: Identifier
    container_url: Annotated[str, Field(min_length=1, max_length=2048)]
    protected_prefix: Annotated[str, Field(min_length=1, max_length=512)]
    configuration_version: Identifier
    evidence_sha256: Digest

    @property
    def key(self) -> str:
        return control_key("source-protection", self.authority_domain, self.container_url)


class NativeConfigurationEvidence(SourceModel):
    """Typed SERVER observation when a provider has no native ETag.

    Fingerprints cover target, API/status and canonical properties or explicit
    absence, not volatile request IDs/times. They are not signatures, ETags,
    native versions or atomic snapshots. The trusted transport/loader owns
    provenance; HTTP/model input is not an admissible loader.
    """

    schema_version: Literal[1] = 1
    kind: Literal["properties", "absence"]
    resource_kind: Literal["blob-service", "management-policy"]
    resource_id: Annotated[str, Field(min_length=1, max_length=2048)]
    api_version: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}(?:-preview)?$")]
    status_code: Literal[200, 404]
    properties: dict[str, JsonValue] | None
    error_code: Literal["ManagementPolicyNotFound"] | None
    etag: str | None
    request_id: Annotated[str, Field(min_length=1, max_length=256)]
    observed_at: datetime
    valid_until: datetime
    fingerprint: Digest

    def canonical_payload(self):
        return self.model_dump(mode="json", exclude={"fingerprint", "request_id", "observed_at", "valid_until"})

    @property
    def account_resource_id(self) -> str:
        return self.resource_id.rsplit("/", 2)[0]

    @model_validator(mode="after")
    def valid_observation(self):
        parts = self.resource_id.split("/")
        expected = "blobservices" if self.resource_kind == "blob-service" else "managementpolicies"
        if (len(parts) != 11 or parts[0] != "" or parts[1].lower() != "subscriptions"
                or parts[3].lower() != "resourcegroups" or parts[5].lower() != "providers"
                or parts[6].lower() != "microsoft.storage" or parts[7].lower() != "storageaccounts"
                or parts[9].lower() != expected or parts[10].lower() != "default"
                or not parts[2] or not parts[4] or not parts[8]):
            raise ValueError("exact ARM configuration resource ID required")
        if self.kind == "properties":
            if self.status_code != 200 or self.properties is None or self.error_code is not None:
                raise ValueError("properties require a successful native response")
        elif (self.resource_kind != "management-policy" or self.status_code != 404
              or self.error_code != "ManagementPolicyNotFound" or self.properties is not None or self.etag is not None):
            raise ValueError("only explicit ManagementPolicyNotFound is policy absence")
        if utc(self.valid_until) <= utc(self.observed_at):
            raise ValueError("native observation interval is invalid")
        if digest(self.canonical_payload()) != self.fingerprint:
            raise ValueError("native configuration fingerprint differs")
        return self

    @classmethod
    def from_response(cls, *, resource_kind, resource_id, api_version, status_code,
                      response_body, request_id, observed_at, valid_until, etag=None):
        """Use actual request metadata and native body, never infer 404 from failure."""
        if not isinstance(response_body, dict) or type(status_code) is not int:
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "native configuration response is missing")
        if status_code == 200 and isinstance(response_body.get("properties"), dict):
            if (not isinstance(resource_id, str) or not isinstance(response_body.get("id"), str)
                    or response_body["id"].lower() != resource_id.lower()):
                raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "native property resource differs from the request")
            kind, properties, error_code = "properties", response_body["properties"], None
        elif (status_code == 404 and resource_kind == "management-policy"
              and isinstance(response_body.get("error"), dict)
              and response_body["error"].get("code") == "ManagementPolicyNotFound"):
            kind, properties, error_code = "absence", None, "ManagementPolicyNotFound"
        else:
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "native configuration is neither verified properties nor policy absence")
        payload = {"schema_version": 1, "kind": kind, "resource_kind": resource_kind,
                   "resource_id": resource_id, "api_version": api_version, "status_code": status_code,
                   "properties": properties, "error_code": error_code, "etag": etag}
        try:
            return cls(**payload, request_id=request_id, observed_at=observed_at, valid_until=valid_until,
                       fingerprint=digest(payload))
        except (ValidationError, ValueError, TypeError, UnicodeError) as exc:
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "native configuration evidence is invalid") from exc

    def management_policy(self) -> dict[str, JsonValue]:
        """Normalize ONLY a verified absence; other missing policy data is unknown."""
        value = checked(self, NativeConfigurationEvidence)
        if value.resource_kind != "management-policy":
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "management-policy evidence is required")
        if value.kind == "absence":
            return {"rules": []}
        policy = value.properties.get("policy")
        if not isinstance(policy, dict) or not isinstance(policy.get("rules"), list):
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "complete native management policy is required")
        return policy


def configuration_evidence_fingerprint(*, blob_service_etag, management_policy_etag,
                                       blob_service_evidence, management_policy_evidence,
                                       automatic_delete_consumers, inventory_version) -> str:
    """Stable observation/inventory hash, NOT a claim that inventory is complete.

    Use this as state/config evidence_sha256 whenever typed evidence is supplied.
    None explicitly preserves unknown inventory; verify() still rejects it.
    Request IDs and observation times remain audited separately from this hash.
    """
    proofs = []
    for evidence in (blob_service_evidence, management_policy_evidence):
        proofs.append(None if evidence is None else checked(evidence, NativeConfigurationEvidence).fingerprint)
    return digest({
        "schema_version": 1, "kind": "native-retention-observations",
        "blob_service": {"etag": blob_service_etag, "fingerprint": proofs[0]},
        "management_policy": {"etag": management_policy_etag, "fingerprint": proofs[1]},
        "automatic_delete_consumers": automatic_delete_consumers, "inventory_version": inventory_version,
    })


@dataclass(frozen=True)
class NativeProtectionState:
    """Trusted inventory output, NOT a request body or a model judgement.

    management_policy is the full native ``properties.policy`` object, including
    every rule, not a filtered list for a caller-supplied prefix. Evidence hashes
    retained native configuration bodies and the complete consumer inventory.
    The loader owns credentials, freshness and provenance of that evidence.
    Missing provider ETags remain None and require typed configuration evidence.
    Unknown OTHER-consumer inventory is None, never an invented empty tuple or
    version. An observed missing ARM lifecycle policy does not fill that gap.
    """

    authority_domain: str
    generation: str
    container_url: str
    protected_prefix: str
    configuration_version: str
    evidence_sha256: str
    observed_at: datetime
    valid_until: datetime
    blob_service_etag: str | None
    management_policy_etag: str | None
    versioning_enabled: bool
    management_policy: Mapping
    automatic_delete_consumers: tuple[str, ...] | None
    inventory_version: str | None
    blob_service_evidence: NativeConfigurationEvidence | None = None
    management_policy_evidence: NativeConfigurationEvidence | None = None


class ProtectionVerifier(Protocol):
    def verify(self, configuration: NoAutoGCConfiguration) -> None: ...


class InventoryProtectionVerifier:
    """Fail-closed verifier over SERVER observations, not an atomic ARM snapshot."""

    def __init__(self, load_state: Callable[[], NativeProtectionState], *, clock: Callable[[], datetime]):
        self._load_state, self._clock = load_state, clock

    def _observation(self, evidence, kind, config, now):
        if evidence is None:
            return None
        try:
            evidence = checked(evidence, NativeConfigurationEvidence)
        except Problem as exc:
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "typed native configuration evidence is invalid") from exc
        endpoint = urlsplit(config.container_url)
        account = evidence.account_resource_id.rsplit("/", 1)[-1].lower()
        suffixes = ("blob.core.windows.net", "blob.core.usgovcloudapi.net",
                    "blob.core.chinacloudapi.cn", "blob.core.cloudapi.de")
        hosts = {account + "." + suffix for suffix in suffixes}
        hosts.update(account + ".privatelink." + suffix for suffix in suffixes)
        if (evidence.resource_kind != kind or endpoint.scheme != "https"
                or endpoint.username or endpoint.password or endpoint.hostname not in hosts
                or not utc(evidence.observed_at) <= now < utc(evidence.valid_until)):
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "native observation target or freshness differs")
        return evidence

    def _native_evidence(self, state, config, now):
        blob = self._observation(state.blob_service_evidence, "blob-service", config, now)
        policy = self._observation(state.management_policy_evidence, "management-policy", config, now)
        for etag, proof in ((state.blob_service_etag, blob), (state.management_policy_etag, policy)):
            if proof is not None and proof.etag != etag:
                raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "native ETag differs from the actual observation")
            if etag is None:
                if proof is None:
                    raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "missing native ETag requires explicit typed evidence")
            elif (not isinstance(etag, str) or not etag.strip() or etag != etag.strip() or etag == "*"
                  or len(etag) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in etag)):
                raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "native ETag is invalid; absent ETags must remain None")
        if blob is not None and (blob.kind != "properties"
                                 or blob.properties.get("isVersioningEnabled") is not state.versioning_enabled):
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "versioning state differs from native properties")
        if policy is not None and policy.management_policy() != state.management_policy:
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "policy state differs from its explicit native observation")
        if blob is not None and policy is not None and blob.account_resource_id.lower() != policy.account_resource_id.lower():
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "native observations refer to different ARM accounts")
        if blob is not None or policy is not None:
            expected = configuration_evidence_fingerprint(
                blob_service_etag=state.blob_service_etag, management_policy_etag=state.management_policy_etag,
                blob_service_evidence=blob, management_policy_evidence=policy,
                automatic_delete_consumers=state.automatic_delete_consumers, inventory_version=state.inventory_version,
            )
            if state.evidence_sha256 != expected:
                raise Problem(409, "SOURCE_PROTECTION_CHANGED", "configured fingerprint differs from native observations and inventory")

    def verify(self, configuration: NoAutoGCConfiguration) -> None:
        config = checked(configuration, NoAutoGCConfiguration)
        try:
            state = self._load_state()
        except Exception as exc:
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "native protection inventory read failed") from exc
        now = utc(self._clock())
        if not isinstance(state, NativeProtectionState):
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "native protection inventory is unavailable")
        for name in ("authority_domain", "generation", "container_url", "protected_prefix",
                     "configuration_version", "evidence_sha256"):
            if getattr(state, name) != getattr(config, name):
                raise Problem(409, "SOURCE_PROTECTION_CHANGED", "protection inventory binding changed")
        if (not utc(state.observed_at) <= now < utc(state.valid_until)
                or state.versioning_enabled is not True or not isinstance(state.inventory_version, str)
                or not state.inventory_version.strip() or state.automatic_delete_consumers != ()):
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "retention inventory is incomplete or unsafe")
        self._native_evidence(state, config, now)
        rules = state.management_policy.get("rules") if isinstance(state.management_policy, Mapping) else None
        if not isinstance(rules, list):
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "complete native lifecycle rules are required")
        # Deliberately conservative: any enabled lifecycle deletion blocks this
        # slice, including version/snapshot/tag-filtered deletion outside a prefix.
        for rule in rules:
            if not isinstance(rule, dict) or type(rule.get("enabled")) is not bool:
                raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "invalid native lifecycle rule")
            if not rule["enabled"]:
                continue
            definition = rule.get("definition")
            actions = definition.get("actions") if isinstance(definition, dict) else None
            if not isinstance(actions, dict):
                raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "native lifecycle actions are missing")
            if any(not isinstance(value, dict) or "delete" in value for value in actions.values()):
                raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "automatic lifecycle deletion is enabled")


class SourceComposition(SourceModel):
    """SERVER-computed transition binding for a larger atomic admission."""

    transition_digest: Digest
    additional_reads: Annotated[tuple[Identifier, ...], Field(max_length=90)] = ()
    required_write_keys: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=90)]

    @model_validator(mode="after")
    def unique_keys(self):
        if (len(set(self.additional_reads)) != len(self.additional_reads)
                or len(set(self.required_write_keys)) != len(self.required_write_keys)):
            raise ValueError("composition keys must be unique")
        return self


class SourcePlan(SourceModel):
    schema_version: Literal[1] = 1
    source: SourceInput
    reference: SourceReference
    source_actor: Identifier
    ingress_id: Identifier
    original: Annotated[bytes, Field(max_length=MAX_ARTIFACT_BYTES)]
    protection: NoAutoGCConfiguration | None = None
    composition: SourceComposition | None = None
    dependency_versions: Annotated[tuple[SourceReference, ...], Field(max_length=MAX_GRAPH_NODES)] = ()
    request: ControlRequest

    @model_validator(mode="after")
    def original_matches(self):
        if (hashlib.sha256(self.original).hexdigest() != self.reference.sha256
                or len(self.original) != self.reference.byte_length
                or self.source.source_id != self.reference.source_id
                or self.source.version != self.reference.version
                or self.source.visibility != self.reference.scope.visibility
                or (len(self.original) > MAX_INLINE_BYTES) != (self.protection is not None)):
            raise ValueError("original source binding differs")
        return self


class BlobLocator(SourceModel):
    container_url: str
    blob_name: str
    artifact_id: Identifier
    logical_version: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    receipt: BlobReceipt


class _Preparation(SourceModel):
    request: ControlRequest
    reference: SourceReference
    plan_sha256: Digest
    protection: NoAutoGCConfiguration
    state: Literal["pending_or_unknown", "verified"]
    blob: BlobLocator | None = None


class SourceHead(SourceModel):
    reference: SourceReference
    source_actor: Identifier
    sequence: Annotated[int, Field(ge=1)]
    access_revision: Annotated[int, Field(ge=1)] = 1
    access_state: Literal["active", "withdrawn"] = "active"


def require_source_head(rows, reference: SourceReference, *, require_current=False,
                        expected_access_revision: int | None = None) -> SourceHead:
    head = control_row(rows, reference.head_key, SourceHead)
    if head.reference.scope != reference.scope or head.reference.source_id != reference.source_id:
        raise Problem(403, "SOURCE_SCOPE_DENIED", "source head identity differs")
    if head.access_state != "active":
        raise Problem(403, "SOURCE_WITHDRAWN", "source access was withdrawn; retained data is not readable")
    if expected_access_revision is not None and head.access_revision != expected_access_revision:
        raise Problem(409, "SOURCE_ACCESS_CHANGED", "source access revision changed")
    if require_current and head.reference != reference:
        raise Problem(409, "SOURCE_STALE", "source version is no longer current")
    return head


class SourceWithdrawalInput(SourceModel):
    withdrawal_id: Identifier
    reference: SourceReference
    expected_access_revision: Annotated[int, Field(ge=1)]
    reason: Annotated[str, Field(min_length=1, max_length=2048)]


class _SourceWithdrawal(SourceModel):
    command: SourceWithdrawalInput
    human_oid: Identifier
    source_actor: Identifier
    access_revision: Annotated[int, Field(ge=2)]
    request: ControlRequest


class _Cursor(SourceModel):
    sequence: Annotated[int, Field(ge=0)]


class SourceEvent(SourceModel):
    schema_version: Literal[1] = 1
    reference: SourceReference
    source_actor: Identifier
    ingress_id: Identifier
    purpose: Identifier
    schema_id: Identifier
    media_type: str
    codec: Literal["identity"] = "identity"
    sequence: Annotated[int, Field(ge=1)]
    necessary: tuple[SourceReference, ...]
    request: ControlRequest
    protection: NoAutoGCConfiguration | None = None
    blob: BlobLocator | None = None
    expires_at: datetime | None = Field(default=None, exclude_if=lambda value: value is None)

    _expiry = field_validator("expires_at")(_source_expiry_utc)


@dataclass(frozen=True)
class ExactSource:
    event: SourceEvent
    original: bytes
    receipt: Record
    access_revision: int = 1


class SnapshotNode(SourceModel):
    reference: SourceReference
    sequence: Annotated[int, Field(ge=1)]
    necessary: tuple[SourceReference, ...]
    access_revision: Annotated[int, Field(ge=1)] = 1


class SourceSnapshot(SourceModel):
    schema_version: Literal[1] = 1
    snapshot_id: Identifier
    scope: SourceScope
    conversation_id: Identifier
    runtime_version: Identifier
    roots: Annotated[tuple[SourceReference, ...], Field(min_length=1, max_length=MAX_GRAPH_NODES)]
    nodes: Annotated[tuple[SnapshotNode, ...], Field(min_length=1, max_length=MAX_GRAPH_NODES)]
    covered_sequence: Annotated[int, Field(ge=1)]


class SourceSnapshotPlan(SourceModel):
    """Retain this exact server-computed manifest/request before committing."""

    manifest: SourceSnapshot
    request: ControlRequest


@dataclass(frozen=True)
class RestoredSnapshot:
    """Verified live-authority graph, not the result of Table disaster recovery."""

    manifest: SourceSnapshot
    originals: Mapping[str, bytes]
    receipt: Record


class SourceMaterials:
    def __init__(
        self, store: BehaviorStore, *, read_snapshot: SnapshotReader | None = None,
        blob_store: BlobArtifactStore | None = None,
        protection: NoAutoGCConfiguration | None = None,
        protection_verifier: ProtectionVerifier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.store = store
        self._clock = clock
        self.read_snapshot = store.read_snapshot if read_snapshot is None else read_snapshot
        self._blobs, self._protection, self._verifier = blob_store, protection, protection_verifier
        if protection is not None:
            self._protection = checked(protection, NoAutoGCConfiguration)
            if blob_store is None or protection_verifier is None:
                raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "trusted Blob protection dependencies are required")
            url = urlsplit(protection.container_url)
            if (url.scheme != "https" or not url.netloc or url.query or url.fragment
                    or url.username or url.password
                    or blob_store.container.url.rstrip("/") != protection.container_url
                    or protection.protected_prefix != f"m3/v1/{blob_store.tenant_id}/"):
                raise Problem(422, "SOURCE_PROTECTION_BINDING", "protection must cover the exact bound container namespace")

    def _require_unexpired(self, expires_at: datetime | None) -> None:
        if expires_at is not None and utc(self._clock()) >= utc(expires_at):
            raise Problem(409, "SOURCE_EXPIRED", "source applicability expired; retained bytes are not readable")

    def _verify_protection(self, config: NoAutoGCConfiguration) -> None:
        if self._verifier is None or self._protection != config:
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "large originals require verified no-auto-GC configuration")
        self._verifier.verify(config)

    def initialize_protection(self) -> Record:
        """Create-only trusted initialization; no generation rotation/GC toggle."""
        config = self._protection
        if config is None:
            raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "no Blob protection configuration")
        self._verify_protection(config)
        request = ControlRequest(request_id=control_key("source-protection-init", config.key),
                                 request_digest=digest(config.model_dump(mode="json")),
                                 read_set=(config.key,))

        def decide(rows):
            if rows[config.key] is not None:
                raise Problem(409, "SOURCE_PROTECTION_CHANGED", "protection is already initialized differently")
            return ControlChanges(writes=(control_write(config.key, config),),
                                  result={"generation": config.generation})

        return self.store.transact(request, decide)

    def plan(self, context: AuthenticatedSourceContext, source: SourceInput, original: bytes, *,
             composition: SourceComposition | None = None) -> SourcePlan:
        source = checked(source, SourceInput)
        self._require_unexpired(source.expires_at)
        graph = self.read_graph(context, source.necessary)
        return self._build_plan(context, source, original, composition=composition, verify_protection=True,
                                dependency_versions=tuple(item.event.reference for _, item in sorted(graph.items())))

    def _build_plan(self, context, source, original, *, composition=None, verify_protection,
                    dependency_versions=()):
        if not isinstance(context, AuthenticatedSourceContext):
            raise Problem(403, "SOURCE_SCOPE_DENIED", "server-authenticated source context is required")
        source = checked(source, SourceInput)
        if composition is not None:
            composition = checked(composition, SourceComposition)
        scope = context.scope(source.visibility)
        authorize(context, scope, source.purpose)
        if not isinstance(original, bytes):
            raise Problem(422, "SOURCE_INVALID_BYTES", "original must be exact bytes, not parsed JSON or text")
        if len(original) > MAX_ARTIFACT_BYTES:
            raise Problem(413, "SOURCE_TOO_LARGE", "original exceeds the 512 KiB bound")
        reference = SourceReference(scope=scope, source_id=source.source_id, version=source.version,
                                    sha256=hashlib.sha256(original).hexdigest(), byte_length=len(original))
        for dependency in source.necessary:
            authorize(context, dependency.scope, source.purpose)
            if dependency.scope.visibility == "private" and dependency.scope != scope:
                raise Problem(403, "SOURCE_SCOPE_DENIED", "shared originals cannot cite private material")
            if dependency.key == reference.key:
                raise Problem(422, "SOURCE_GRAPH_INVALID", "a source cannot depend on itself")
        protection = self._protection if len(original) > MAX_INLINE_BYTES else None
        if len(original) > MAX_INLINE_BYTES:
            if protection is None:
                raise Problem(503, "SOURCE_PROTECTION_UNVERIFIED", "large original admission is disabled")
            if self._blobs.tenant_id != scope.tenant_id:
                raise Problem(403, "SOURCE_SCOPE_DENIED", "original tenant differs from the bound Blob namespace")
            if verify_protection:
                self._verify_protection(protection)
        metadata = {"source": source.model_dump(mode="json"), "reference": reference.model_dump(mode="json"),
                    "source_actor": context.actor_id, "ingress_id": context.ingress_id,
                    "protection": None if protection is None else protection.model_dump(mode="json"),
                    "composition": None if composition is None else composition.model_dump(mode="json"),
                    "dependency_versions": [item.model_dump(mode="json") for item in dependency_versions]}
        reads = [reference.key, reference.head_key, control_key("source-cursor", scope.key)]
        reads += [key for item in dependency_versions for key in (item.key, item.head_key)]
        if protection:
            reads += [protection.key, control_key("source-preparation", reference.key)]
        if composition:
            reads += list(composition.additional_reads)
        request = ControlRequest(request_id=control_key("source-admit", reference.key),
                                 request_digest=digest(metadata), read_set=tuple(dict.fromkeys(reads)))
        return SourcePlan(source=source, reference=reference, source_actor=context.actor_id,
                          ingress_id=context.ingress_id, original=original, protection=protection,
                          composition=composition, request=request, dependency_versions=dependency_versions)

    def _plan(self, context, plan):
        plan = checked(plan, SourcePlan)
        expected = self._build_plan(context, plan.source, plan.original, composition=plan.composition,
                                    verify_protection=False, dependency_versions=plan.dependency_versions)
        if expected != plan:
            raise Problem(409, "SOURCE_REQUEST_MISMATCH", "retain the original source identity and request")
        return plan

    def _blob_ids(self, plan: SourcePlan) -> tuple[str, str]:
        return (control_key("source-object", plan.reference.scope.key, plan.reference.source_id),
                control_key("source-object-version", plan.reference.key)[:32])

    def _locator(self, plan, receipt):
        artifact, version = self._blob_ids(plan)
        return BlobLocator(
            container_url=plan.protection.container_url,
            blob_name=f"m3/v1/{plan.reference.scope.tenant_id}/{plan.reference.scope.team_id}/{artifact}/{version}",
            artifact_id=artifact, logical_version=version, receipt=receipt,
        )

    def _preparation(self, plan, rows):
        config = control_row(rows, plan.protection.key, NoAutoGCConfiguration)
        if config != plan.protection:
            raise Problem(409, "SOURCE_PROTECTION_CHANGED", "prepared authority generation is no longer current")
        key = control_key("source-preparation", plan.reference.key)
        preparation = control_row(rows, key, _Preparation)
        if (preparation.request != plan.request or preparation.plan_sha256 != plan.request.request_digest
                or preparation.reference != plan.reference or preparation.protection != plan.protection):
            raise Problem(409, "SOURCE_REQUEST_MISMATCH", "source ID is bound to a different preparation")
        return preparation

    def prepare_large(self, context: AuthenticatedSourceContext, plan: SourcePlan) -> Record:
        """One permitted PUT. Existing/uncertain preparation uses reconciliation."""
        plan = self._plan(context, plan)
        self._require_unexpired(plan.source.expires_at)
        if plan.protection is None:
            raise Problem(422, "SOURCE_INLINE_REQUIRED", "small necessary originals commit inline")
        self._verify_protection(plan.protection)
        key = control_key("source-preparation", plan.reference.key)
        request = ControlRequest(
            request_id=control_key("source-prepare", plan.reference.key),
            request_digest=plan.request.request_digest, read_set=(plan.protection.key, key),
        )
        fresh = False

        def decide(rows):
            nonlocal fresh
            self._require_unexpired(plan.source.expires_at)
            if control_row(rows, plan.protection.key, NoAutoGCConfiguration) != plan.protection:
                raise Problem(409, "SOURCE_PROTECTION_CHANGED", "protection generation changed")
            if rows[key] is not None:
                raise Problem(409, "SOURCE_PREPARATION_UNKNOWN", "existing preparation must be reconciled")
            fresh = True
            prepared = _Preparation(request=plan.request, reference=plan.reference,
                                    plan_sha256=plan.request.request_digest, protection=plan.protection,
                                    state="pending_or_unknown")
            return ControlChanges(writes=(control_write(key, prepared),), result={"state": "pending_or_unknown"})

        self.store.transact(request, decide)
        if not fresh:
            return self.reconcile_preparation(context, plan)
        self._require_unexpired(plan.source.expires_at)
        artifact, version = self._blob_ids(plan)
        try:
            receipt = self._blobs.put(plan.reference.scope.team_id, artifact, version,
                                      plan.original, plan.reference.sha256)
        except Problem as exc:
            raise Problem(503, "SOURCE_PREPARATION_UNKNOWN",
                          "original upload is uncertain; reconcile this preparation without another PUT",
                          request_id=plan.request.request_id,
                          request_digest=plan.request.request_digest) from exc
        return self._confirm_blob(plan, receipt)

    def _confirm_blob(self, plan, receipt):
        if receipt.sha256 != plan.reference.sha256 or receipt.bytes != plan.reference.byte_length:
            raise Problem(502, "SOURCE_INTEGRITY_ERROR", "prepared Blob differs from original bytes")
        artifact, version = self._blob_ids(plan)
        data = self._blobs.read(plan.reference.scope.team_id, artifact, version, receipt)
        if data != plan.original:
            raise Problem(502, "SOURCE_INTEGRITY_ERROR", "prepared Blob is not the exact original")
        self._verify_protection(plan.protection)
        key = control_key("source-preparation", plan.reference.key)
        request = ControlRequest(request_id=control_key("source-prepared", plan.reference.key),
                                 request_digest=digest([plan.request.request_digest, receipt.model_dump(mode="json")]),
                                 read_set=(plan.protection.key, key))

        def decide(rows):
            preparation = self._preparation(plan, rows)
            updated = _Preparation(**{**preparation.model_dump(), "state": "verified",
                                      "blob": self._locator(plan, receipt)})
            return ControlChanges(writes=(control_write(key, updated, "replace"),),
                                  result={"state": "verified", "reference": plan.reference.model_dump(mode="json")})

        return self.store.transact(request, decide)

    def reconcile_preparation(self, context: AuthenticatedSourceContext, plan: SourcePlan) -> Record:
        plan = self._plan(context, plan)
        if plan.protection is None:
            raise Problem(422, "SOURCE_INLINE_REQUIRED", "inline originals have no Blob preparation")
        key = control_key("source-preparation", plan.reference.key)
        preparation = self._preparation(plan, self.read_snapshot((plan.protection.key, key)))
        artifact, version = self._blob_ids(plan)
        try:
            receipt = (preparation.blob.receipt if preparation.blob is not None
                       else self._blobs.stat(plan.reference.scope.team_id, artifact, version))
        except Problem as exc:
            raise Problem(503, "SOURCE_PREPARATION_UNKNOWN", "no verified original upload; protection retained",
                          request_id=plan.request.request_id) from exc
        return self._confirm_blob(plan, receipt)

    def attachment(self, context: AuthenticatedSourceContext, plan: SourcePlan,
                   rows: Mapping[str, Record | None]) -> ControlChanges:
        """Side-effect-free same-EGT composition seam. Include plan.request.read_set.

        The trusted composer must preserve these writes AND the exact plan request
        receipt. Additional work/obligation writes may share that receipt; never
        attach in a second unconditional transaction. Call validate_attachment()
        before entering the decision callback, including for inline dependencies.
        """
        plan = self._plan(context, plan)
        self._require_unexpired(plan.source.expires_at)
        authorize(context, plan.reference.scope, plan.source.purpose)
        if context.actor_id != plan.source_actor or context.ingress_id != plan.ingress_id:
            raise Problem(403, "SOURCE_SCOPE_DENIED", "only the original authenticated ingress may commit")
        reference, source = plan.reference, plan.source
        if rows.get(reference.key) is not None:
            raise Problem(409, "SOURCE_VERSION_EXISTS", "source versions are immutable")
        head_row = rows.get(reference.head_key)
        if head_row is None:
            if reference.version != 1:
                raise Problem(409, "SOURCE_VERSION_CONFLICT", "first source version must be one")
        else:
            head = control_row(rows, reference.head_key, SourceHead)
            require_source_head(rows, head.reference)
            if head.source_actor != plan.source_actor:
                raise Problem(403, "SOURCE_SCOPE_DENIED", "source identity belongs to a different authenticated actor")
            if (head.reference.scope != reference.scope or head.reference.source_id != reference.source_id
                    or reference.version != head.reference.version + 1):
                raise Problem(409, "SOURCE_VERSION_CONFLICT", "source versions must advance the current head")
        for dependency in source.necessary:
            event = control_row(rows, dependency.key, SourceEvent)
            if event.reference != dependency:
                raise Problem(409, "SOURCE_REFERENCE_MISMATCH", "necessary source differs from the accepted version")
        evidence = self.snapshot_evidence(context, source.necessary, rows, require_current=False)
        if set(evidence) != {item.key for item in plan.dependency_versions}:
            raise Problem(409, "SOURCE_GRAPH_INCOMPLETE", "admission must retain its complete necessary graph")
        for dependency in plan.dependency_versions:
            if evidence[dependency.key][0].reference != dependency:
                raise Problem(409, "SOURCE_REFERENCE_MISMATCH", "prepared dependency version changed")
        blob = None
        if plan.protection:
            preparation = self._preparation(plan, rows)
            if preparation.state != "verified" or preparation.blob is None:
                raise Problem(409, "SOURCE_PREPARATION_UNKNOWN", "original Blob has not been verified")
            blob = preparation.blob
        cursor_key = control_key("source-cursor", reference.scope.key)
        cursor = _Cursor(sequence=0) if rows.get(cursor_key) is None else control_row(rows, cursor_key, _Cursor)
        sequence = cursor.sequence + 1
        event = SourceEvent(reference=reference, source_actor=plan.source_actor, ingress_id=plan.ingress_id,
                            purpose=source.purpose, schema_id=source.schema_id, media_type=source.media_type,
                            sequence=sequence, necessary=source.necessary, request=plan.request,
                            protection=plan.protection, blob=blob, expires_at=source.expires_at)
        material = None if blob else InlineMaterial(data=plan.original, sha256=reference.sha256)
        return ControlChanges(writes=(
            control_write(reference.key, event, material=material),
            control_write(reference.head_key, SourceHead(reference=reference, source_actor=plan.source_actor,
                                                        sequence=sequence,
                                                        access_revision=1 if head_row is None else head.access_revision),
                          "create" if head_row is None else "replace"),
            control_write(cursor_key, _Cursor(sequence=sequence),
                          "create" if rows.get(cursor_key) is None else "replace"),
        ), result={"reference": reference.model_dump(mode="json"), "sequence": sequence})

    def validate_attachment(self, context: AuthenticatedSourceContext, plan: SourcePlan) -> SourcePlan:
        """Verify necessary bytes before an EGT; attachment rechecks generation."""
        plan = self._plan(context, plan)
        self._require_unexpired(plan.source.expires_at)
        self.read_graph(context, plan.source.necessary)
        if plan.protection:
            self._verify_protection(plan.protection)
            key = control_key("source-preparation", plan.reference.key)
            preparation = self._preparation(plan, self.read_snapshot((plan.protection.key, key)))
            if preparation.state != "verified" or preparation.blob is None:
                raise Problem(409, "SOURCE_PREPARATION_UNKNOWN", "original Blob has not been verified")
            if preparation.blob != self._locator(plan, preparation.blob.receipt):
                raise Problem(502, "SOURCE_INTEGRITY_ERROR", "prepared locator differs from the exact source")
            artifact, version = self._blob_ids(plan)
            data = self._blobs.read(plan.reference.scope.team_id, artifact, version, preparation.blob.receipt)
            if data != plan.original:
                raise Problem(502, "SOURCE_INTEGRITY_ERROR", "prepared material no longer matches the original")
        return plan

    def commit(self, context: AuthenticatedSourceContext, plan: SourcePlan) -> Record:
        plan = self.validate_attachment(context, plan)
        if plan.composition is not None:
            raise Problem(409, "SOURCE_COMPOSITION_REQUIRED", "use the bound combined authority transition")
        return self.store.transact(plan.request, lambda rows: self.attachment(context, plan, rows))

    def compose_attachment(self, context: AuthenticatedSourceContext, plan: SourcePlan,
                           rows: Mapping[str, Record | None], changes: ControlChanges) -> ControlChanges:
        """Merge trusted transition writes with the source in ONE plan.request EGT.

        The composer owns the transition_digest canonicalization and decision;
        initialize SourceComposition from its validated server transition intent,
        not an HTTP-supplied digest. Call validate_attachment before transact.
        """
        source = self.attachment(context, plan, rows)
        if plan.composition is None or set(write.row_id for write in changes.writes) != set(
                plan.composition.required_write_keys):
            raise Problem(409, "SOURCE_COMPOSITION_INCOMPLETE", "combined transition omits required authority writes")
        if set(write.row_id for write in source.writes) & set(write.row_id for write in changes.writes):
            raise Problem(409, "SOURCE_COMPOSITION_CONFLICT", "combined transition cannot replace source-owned rows")
        return ControlChanges(writes=source.writes + changes.writes,
                              result={**source.result, "transition": changes.result})

    def reconcile(self, context: AuthenticatedSourceContext, plan: SourcePlan) -> Record:
        # Reconciliation must still work when native configuration is unavailable
        # or a generation was retired: it is a historical read, not permission.
        plan = checked(plan, SourcePlan)
        authorize(context, plan.reference.scope, plan.source.purpose)
        if context.actor_id != plan.source_actor or context.ingress_id != plan.ingress_id:
            raise Problem(403, "SOURCE_SCOPE_DENIED", "original authenticated source binding differs")
        return self.store.reconcile(plan.request)

    def verify_receipt(self, row: Record, request: ControlRequest, key: str) -> Record:
        receipt = self.store.reconcile(request)
        # BehaviorStore fingerprints the actual binary Payload, not sorted JSON.
        wire = json.dumps(row.payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        expected = (key, hashlib.sha256(wire).hexdigest())
        if expected not in [tuple(item) for item in receipt.payload.get("writes", ())]:
            raise Problem(502, "SOURCE_RECEIPT_MISMATCH", "source row is not covered by its exact control receipt")
        return receipt

    def withdrawal_request(self, principal: BehaviorPrincipal, context: AuthenticatedSourceContext,
                           command: SourceWithdrawalInput) -> ControlRequest:
        command = checked(command, SourceWithdrawalInput)
        require_human_context(principal, context, command.reference.scope)
        key = control_key("source-withdrawal", command.reference.head_key, command.withdrawal_id)
        return ControlRequest(
            request_id=control_key("source-withdraw-request", command.reference.scope.key, command.withdrawal_id),
            request_digest=digest([command.model_dump(mode="json"), principal.oid, context.actor_id]),
            read_set=(command.reference.key, command.reference.head_key, key),
        )

    def withdraw_source(self, principal: BehaviorPrincipal, context: AuthenticatedSourceContext,
                        command: SourceWithdrawalInput) -> Record:
        """Explicit human deny-only transition; retain the command before calling."""
        command = checked(command, SourceWithdrawalInput)
        request = self.withdrawal_request(principal, context, command)
        key = control_key("source-withdrawal", command.reference.head_key, command.withdrawal_id)

        def decide(rows):
            event = control_row(rows, command.reference.key, SourceEvent)
            authorize(context, event.reference.scope, event.purpose)
            if event.reference != command.reference:
                raise Problem(409, "SOURCE_REFERENCE_MISMATCH", "withdrawal names a different original")
            head = require_source_head(rows, command.reference, require_current=True,
                                       expected_access_revision=command.expected_access_revision)
            updated = SourceHead(**{**head.model_dump(), "access_revision": head.access_revision + 1,
                                    "access_state": "withdrawn"})
            withdrawn = _SourceWithdrawal(command=command, human_oid=principal.oid, source_actor=context.actor_id,
                                          access_revision=updated.access_revision, request=request)
            material = canonical(withdrawn.model_dump(mode="json"))
            return ControlChanges(writes=(
                control_write(command.reference.head_key, updated, "replace"),
                control_write(key, withdrawn, material=InlineMaterial(
                    data=material, sha256=hashlib.sha256(material).hexdigest())),
            ), result={"source_key": command.reference.key, "access_state": "withdrawn",
                       "access_revision": updated.access_revision})

        return self.store.transact(request, decide)

    def reconcile_withdrawal(self, principal: BehaviorPrincipal, context: AuthenticatedSourceContext,
                             command: SourceWithdrawalInput) -> Record:
        return self.store.reconcile(self.withdrawal_request(principal, context, command))

    def read_exact(self, context: AuthenticatedSourceContext, reference: SourceReference, *,
                   require_current: bool = False) -> ExactSource:
        reference = checked(reference, SourceReference)
        authorize(context, reference.scope)
        keys = (reference.key, reference.head_key)
        rows = self.read_snapshot(keys)
        event = control_row(rows, reference.key, SourceEvent)
        authorize(context, reference.scope, event.purpose)
        if event.reference != reference:
            raise Problem(409, "SOURCE_REFERENCE_MISMATCH", "citation does not match the exact accepted source")
        head = require_source_head(rows, reference, require_current=require_current)
        self._require_unexpired(event.expires_at)
        row = rows[reference.key]
        receipt = self.verify_receipt(row, event.request, reference.key)
        self._require_unexpired(event.expires_at)
        if event.blob is None:
            if event.protection is not None or row.payload.get("material") is None:
                raise Problem(502, "SOURCE_INTEGRITY_ERROR", "necessary inline original is missing")
            try:
                material = InlineMaterial.model_validate_json(canonical(row.payload["material"]))
            except ValidationError:
                raise Problem(502, "SOURCE_INTEGRITY_ERROR", "inline original is corrupt") from None
            data = material.data
        else:
            if self._blobs is None or event.protection is None:
                raise Problem(503, "SOURCE_BLOB_UNAVAILABLE", "exact source Blob reader is not configured")
            blob = event.blob
            artifact = control_key("source-object", reference.scope.key, reference.source_id)
            version = control_key("source-object-version", reference.key)[:32]
            expected_name = f"m3/v1/{reference.scope.tenant_id}/{reference.scope.team_id}/{artifact}/{version}"
            if (blob.container_url != self._blobs.container.url.rstrip("/")
                    or blob.artifact_id != artifact or blob.logical_version != version
                    or blob.blob_name != expected_name or reference.scope.tenant_id != self._blobs.tenant_id
                    or blob.receipt.sha256 != reference.sha256 or blob.receipt.bytes != reference.byte_length):
                raise Problem(502, "SOURCE_INTEGRITY_ERROR", "exact Blob locator differs from the source binding")
            data = self._blobs.read(reference.scope.team_id, artifact, version, blob.receipt)
        if len(data) != reference.byte_length or hashlib.sha256(data).hexdigest() != reference.sha256:
            raise Problem(502, "SOURCE_INTEGRITY_ERROR", "original hash or length differs")
        require_source_head(self.read_snapshot((reference.head_key,)), reference, require_current=require_current,
                            expected_access_revision=head.access_revision)
        self._require_unexpired(event.expires_at)
        return ExactSource(event=event, original=data, receipt=receipt, access_revision=head.access_revision)

    def read_graph(self, context, roots, *, require_current=False):
        found, pending, total = {}, list(roots), 0
        while pending:
            reference = pending.pop()
            if reference.key in found:
                if found[reference.key].event.reference != reference:
                    raise Problem(409, "SOURCE_GRAPH_INVALID", "inconsistent graph reference")
                continue
            if len(found) >= MAX_GRAPH_NODES:
                raise Problem(413, "SOURCE_GRAPH_LIMIT", "necessary source graph exceeds its node bound")
            exact = self.read_exact(context, reference, require_current=require_current)
            total += len(exact.original)
            if total > MAX_RESTORE_BYTES:
                raise Problem(413, "SOURCE_GRAPH_LIMIT", "necessary source graph exceeds its byte bound")
            found[reference.key] = exact
            pending.extend(exact.event.necessary)
        if found:
            self.guard_sources(self.read_snapshot(self.source_read_keys(found)), found, require_current=require_current)
        return found

    @staticmethod
    def source_read_keys(originals) -> tuple[str, ...]:
        return tuple(dict.fromkeys(key for item in originals.values()
                                   for key in (item.event.reference.key, item.event.reference.head_key)))

    def guard_sources(self, rows, originals, *, require_current=False) -> None:
        for key, exact in originals.items():
            if control_row(rows, key, SourceEvent) != exact.event:
                raise Problem(409, "SOURCE_REFERENCE_MISMATCH", "necessary event changed")
            require_source_head(rows, exact.event.reference, require_current=require_current,
                                expected_access_revision=exact.access_revision)
            self._require_unexpired(exact.event.expires_at)

    @staticmethod
    def evidence_digest(originals: Mapping[str, ExactSource]) -> str:
        return digest([[key, item.event.model_dump(mode="json"), item.access_revision]
                       for key, item in sorted(originals.items())])

    def snapshot_evidence(self, context, roots, rows, *, require_current=True):
        found, pending = {}, list(roots)
        while pending:
            reference = pending.pop()
            if reference.key in found:
                if found[reference.key][0].reference != reference:
                    raise Problem(409, "SOURCE_GRAPH_INVALID", "conflicting graph reference")
                continue
            if len(found) >= MAX_GRAPH_NODES:
                raise Problem(413, "SOURCE_GRAPH_LIMIT", "necessary graph exceeds its bound")
            event = control_row(rows, reference.key, SourceEvent)
            authorize(context, reference.scope, event.purpose)
            if event.reference != reference:
                raise Problem(409, "SOURCE_REFERENCE_MISMATCH", "necessary source differs")
            head = require_source_head(rows, reference, require_current=require_current)
            self._require_unexpired(event.expires_at)
            found[reference.key] = (event, head.access_revision)
            pending.extend(event.necessary)
        return found

    @staticmethod
    def snapshot_evidence_digest(evidence) -> str:
        return digest([[key, event.model_dump(mode="json"), revision]
                       for key, (event, revision) in sorted(evidence.items())])

    def _snapshot_plan_and_graph(self, context: AuthenticatedSourceContext, *, snapshot_id: str,
                                 scope: SourceScope, conversation_id: str, runtime_version: str,
                                 roots: tuple[SourceReference, ...]):
        authorize(context, scope)
        if not roots or len(roots) > MAX_GRAPH_NODES or len({item.key for item in roots}) != len(roots):
            raise Problem(422, "SOURCE_GRAPH_INVALID", "snapshot needs a bounded unique root set")
        graph = self.read_graph(context, roots)
        if any(exact.event.reference.scope != scope for exact in graph.values()):
            raise Problem(403, "SOURCE_SCOPE_DENIED", "snapshot graph must have one exact scope")
        nodes = tuple(SnapshotNode(reference=item.event.reference, sequence=item.event.sequence,
                                   necessary=item.event.necessary, access_revision=item.access_revision)
                      for _, item in sorted(graph.items()))
        manifest = SourceSnapshot(snapshot_id=snapshot_id, scope=scope, conversation_id=conversation_id,
                                  runtime_version=runtime_version, roots=roots, nodes=nodes,
                                  covered_sequence=max(item.sequence for item in nodes))
        key = control_key("source-snapshot", scope.key, snapshot_id)
        request = ControlRequest(request_id=control_key("source-snapshot-commit", key),
                                 request_digest=digest(manifest.model_dump(mode="json")),
                                 read_set=(key,) + self.source_read_keys(graph))
        return SourceSnapshotPlan(manifest=manifest, request=request), graph

    def _commit_snapshot(self, plan, graph):
        manifest, request = plan.manifest, plan.request
        key = control_key("source-snapshot", manifest.scope.key, manifest.snapshot_id)

        def decide(rows):
            self.guard_sources(rows, graph)
            for node in manifest.nodes:
                event = control_row(rows, node.reference.key, SourceEvent)
                if (event.reference != node.reference or event.necessary != node.necessary
                        or event.sequence != node.sequence):
                    raise Problem(409, "SOURCE_GRAPH_INVALID", "snapshot graph changed")
            return ControlChanges(writes=(control_write(key, manifest),), result={
                "snapshot_key": key, "manifest_sha256": request.request_digest,
                "request": request.model_dump(mode="json"),
            })

        return self.store.transact(request, decide)

    def plan_snapshot(self, context: AuthenticatedSourceContext, *, snapshot_id: str,
                      scope: SourceScope, conversation_id: str, runtime_version: str,
                      roots: tuple[SourceReference, ...]) -> SourceSnapshotPlan:
        """Read/verify the real graph without writing; retain returned plan durably."""
        plan, _ = self._snapshot_plan_and_graph(
            context, snapshot_id=snapshot_id, scope=scope, conversation_id=conversation_id,
            runtime_version=runtime_version, roots=roots,
        )
        return plan

    def commit_snapshot(self, context: AuthenticatedSourceContext, plan: SourceSnapshotPlan) -> Record:
        """Recheck original bytes/access, then commit the retained request in one EGT."""
        plan = checked(plan, SourceSnapshotPlan)
        manifest = plan.manifest
        actual, graph = self._snapshot_plan_and_graph(
            context, snapshot_id=manifest.snapshot_id, scope=manifest.scope,
            conversation_id=manifest.conversation_id, runtime_version=manifest.runtime_version,
            roots=manifest.roots,
        )
        if actual != plan:
            raise Problem(409, "SOURCE_SNAPSHOT_PLAN_MISMATCH", "snapshot plan differs from its complete current source graph")
        return self._commit_snapshot(plan, graph)

    def reconcile_snapshot(self, context: AuthenticatedSourceContext, plan: SourceSnapshotPlan) -> Record:
        """Historical receipt only; no graph rebuild, writes, retries or access grant."""
        plan = checked(plan, SourceSnapshotPlan)
        manifest, request = plan.manifest, plan.request
        authorize(context, manifest.scope)
        key = control_key("source-snapshot", manifest.scope.key, manifest.snapshot_id)
        reads = {key} | {item for node in manifest.nodes for item in (node.reference.key, node.reference.head_key)}
        if (request.request_id != control_key("source-snapshot-commit", key)
                or request.request_digest != digest(manifest.model_dump(mode="json"))
                or set(request.read_set) != reads):
            raise Problem(409, "SOURCE_SNAPSHOT_PLAN_MISMATCH", "reconcile the exact original snapshot request")
        return self.store.reconcile(request)

    def capture_snapshot(self, context: AuthenticatedSourceContext, *, snapshot_id: str,
                         scope: SourceScope, conversation_id: str, runtime_version: str,
                         roots: tuple[SourceReference, ...]) -> Record:
        """Compatibility convenience; durable API callers use plan/commit_snapshot."""
        plan, graph = self._snapshot_plan_and_graph(
            context, snapshot_id=snapshot_id, scope=scope, conversation_id=conversation_id,
            runtime_version=runtime_version, roots=roots,
        )
        return self._commit_snapshot(plan, graph)

    def restore_snapshot(self, context: AuthenticatedSourceContext, *, scope: SourceScope,
                         snapshot_id: str, receipt: Record) -> RestoredSnapshot:
        """Rehydrate from the same live authority, not from an independent backup.

        Require ALL necessary bytes, exact versions, receipts and current access.
        Missing data fails; this method neither recreates rows/objects nor imports
        an earlier authority or access state. No side effects or operation replay.
        """
        authorize(context, scope)
        key = control_key("source-snapshot", scope.key, snapshot_id)
        rows = self.read_snapshot((key,))
        manifest = control_row(rows, key, SourceSnapshot)
        if manifest.scope != scope or manifest.snapshot_id != snapshot_id:
            raise Problem(403, "SOURCE_SCOPE_DENIED", "snapshot scope or identity differs")
        try:
            request = ControlRequest.model_validate_json(canonical(receipt.payload["result"]["request"]))
        except (KeyError, TypeError, ValidationError):
            raise Problem(502, "SOURCE_RECEIPT_MISMATCH", "snapshot requires its confirmed control receipt") from None
        confirmed = self.verify_receipt(rows[key], request, key)
        if (confirmed != receipt or request.request_digest != digest(rows[key].payload["data"])
                or request.request_id != control_key("source-snapshot-commit", key)):
            raise Problem(502, "SOURCE_RECEIPT_MISMATCH", "snapshot receipt does not match its manifest")
        graph = self.read_graph(context, manifest.roots)
        expected_nodes = {node.reference.key: node for node in manifest.nodes}
        if len(expected_nodes) != len(manifest.nodes) or set(expected_nodes) != set(graph):
            raise Problem(502, "SOURCE_GRAPH_INCOMPLETE", "snapshot does not contain the complete necessary graph")
        for key, exact in graph.items():
            node = expected_nodes[key]
            if (exact.event.reference.scope != scope or exact.event.reference != node.reference
                    or exact.event.necessary != node.necessary or exact.event.sequence != node.sequence
                    or exact.access_revision != node.access_revision):
                raise Problem(502, "SOURCE_GRAPH_INCOMPLETE", "snapshot source graph differs")
        if manifest.covered_sequence != max(node.sequence for node in manifest.nodes):
            raise Problem(502, "SOURCE_GRAPH_INCOMPLETE", "snapshot sequence coverage differs")
        return RestoredSnapshot(manifest, MappingProxyType({key: item.original for key, item in graph.items()}),
                                confirmed)
