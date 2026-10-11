"""Independent, bounded source-material backup; NOT database/region DR.

Archive v1 is MAGIC + unsigned big-endian header length + canonical UTF-8 JSON
header + raw byte segments in key order. No compression, paths, extraction or
activation instructions exist. The 8 MiB limit includes ALL metadata and bytes;
the header is independently capped at 64 KiB. Missing/duplicate/inconsistent
graph nodes or segments fail, never truncate into a "complete" backup.

Initialization: use an existing BackupStore (normally make_blob_backup_store,
which constructs BlobBackupStore with 8 MiB single-PUT/single-GET bounds and
all retries disabled); bind SourceBackupSettings to that exact tenant/container.
For new uploads supply SourceMaterials and trusted NoAutoGCConfiguration plus
InventoryProtectionVerifier, then initialize_protection on the same BehaviorStore.
The native inventory must cover v1/<tenant>/, every lifecycle deletion rule and
other automatic deletion consumer. Configuration generations use the same guard;
there is no GC, protection-release or configuration-switch API.

plan() reads the REAL live snapshot and exact bytes. Durably retain that plan,
then upload(). The control intent consumes permission for ONE create call before
Blob I/O. An existing/uncertain intent NEVER calls create again: use explicit
reconcile_upload with the same plan. Absence stays unknown. Once a native receipt
is known, verification uses only its exact version/ETag, not a current alias.
Keep the completed SourceBackupReference independently of the source authority.

Restore can run with sources=None and no original Table/source Blob access.
It requires human Behavior.Restore plus fresh RecoveryAuthorizer evidence from
CURRENT trusted server state, bound to the exact independently retained reference.
The authorizer must consult a trusted backup catalog/current recovery decision;
accepting the input reference, old archive ACL or an HTTP boolean is NOT enough.
Missing/unknown authority denies. Returned bytes are QUARANTINED only; no claims,
permissions, control rows, credentials, budgets, owners or jobs are restored.
No RPO/RTO, cross-region durability, native Azure or Hosted proof is asserted.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import Field, JsonValue, ValidationError

from .auth import Actor
from .backup_models import BackupId, BackupReceipt, MAX_BACKUP_BYTES, OwnerOid
from .backup_store import BackupStore, BlobBackupStore
from .behavior_auth import BehaviorPrincipal
from .behavior_operations import control_key
from .behavior_store import ControlChanges, ControlRequest, Digest
from .errors import Problem
from .models import Identifier
from .records import Record
from .source_materials import (
    AuthenticatedSourceContext, NoAutoGCConfiguration, ProtectionVerifier,
    SourceEvent, SourceMaterials, SourceModel, SourceScope, SourceSnapshot, authorize, canonical,
    checked, control_row, control_write, digest, require_source_head, utc,
)

MAGIC = b"FA-SRC1\x00"
MAX_HEADER_BYTES = 64 * 1024

__all__ = [
    "SourceBackupService", "SourceBackupSettings", "SourceBackupPlan", "SourceBackupReference",
    "SourceArchiveHeader", "ArchiveSegment", "RecoveryAuthorization", "RecoveryAuthorizer",
    "QuarantinedSourceRecovery", "encode_archive", "decode_archive", "make_blob_backup_store",
]


class ArchiveSegment(SourceModel):
    key: Digest
    byte_length: Annotated[int, Field(ge=0, le=512 * 1024)]
    sha256: Digest


class SourceArchiveHeader(SourceModel):
    format_version: Literal[1] = 1
    backup_id: BackupId
    owner_oid: OwnerOid
    manifest: SourceSnapshot
    snapshot_receipt_sha256: Digest
    authority_binding: Digest
    guard_revision: Annotated[int, Field(ge=1)]
    segments: Annotated[tuple[ArchiveSegment, ...], Field(min_length=1, max_length=24)]


@dataclass(frozen=True)
class _DecodedArchive:
    header: SourceArchiveHeader
    originals: Mapping[str, bytes]


def _header_graph(header: SourceArchiveHeader) -> None:
    nodes, segments = {}, {}
    for node in header.manifest.nodes:
        key = node.reference.key
        if key in nodes or node.reference.scope != header.manifest.scope:
            raise Problem(422, "SOURCE_BACKUP_GRAPH_INVALID", "duplicate or cross-scope snapshot node")
        nodes[key] = node
    for segment in header.segments:
        if segment.key in segments:
            raise Problem(422, "SOURCE_BACKUP_GRAPH_INVALID", "duplicate archive segment")
        segments[segment.key] = segment
    if set(nodes) != set(segments) or list(segments) != sorted(segments):
        raise Problem(422, "SOURCE_BACKUP_GRAPH_INCOMPLETE", "archive segments must exactly cover the sorted node set")
    if len({ref.key for ref in header.manifest.roots}) != len(header.manifest.roots):
        raise Problem(422, "SOURCE_BACKUP_GRAPH_INVALID", "duplicate graph root")
    seen, pending = set(), list(header.manifest.roots)
    while pending:
        reference = pending.pop()
        node = nodes.get(reference.key)
        if node is None or node.reference != reference:
            raise Problem(422, "SOURCE_BACKUP_GRAPH_INCOMPLETE", "necessary graph reference is missing or inconsistent")
        if reference.key in seen:
            continue
        if len({ref.key for ref in node.necessary}) != len(node.necessary):
            raise Problem(422, "SOURCE_BACKUP_GRAPH_INVALID", "duplicate necessary graph reference")
        seen.add(reference.key)
        pending.extend(node.necessary)
    if seen != set(nodes) or header.manifest.covered_sequence != max(node.sequence for node in nodes.values()):
        raise Problem(422, "SOURCE_BACKUP_GRAPH_INCOMPLETE", "manifest closure or sequence coverage is incomplete")
    for key, node in nodes.items():
        if (segments[key].sha256 != node.reference.sha256
                or segments[key].byte_length != node.reference.byte_length):
            raise Problem(422, "SOURCE_BACKUP_GRAPH_INVALID", "segment digest or size differs from its exact source")
    if (header.manifest.scope.visibility == "private"
            and header.manifest.scope.owner_actor != "human-" + header.owner_oid):
        raise Problem(403, "SOURCE_BACKUP_SCOPE_DENIED", "private backup owner must match its source scope")


def encode_archive(header: SourceArchiveHeader, originals: Mapping[str, bytes]) -> bytes:
    header = checked(header, SourceArchiveHeader)
    _header_graph(header)
    raw = canonical(header.model_dump(mode="json"))
    if len(raw) > MAX_HEADER_BYTES:
        raise Problem(413, "SOURCE_BACKUP_HEADER_TOO_LARGE", "archive header exceeds 64 KiB")
    total = len(MAGIC) + 4 + len(raw) + sum(segment.byte_length for segment in header.segments)
    if total > MAX_BACKUP_BYTES:
        raise Problem(413, "SOURCE_BACKUP_TOO_LARGE", "complete archive including metadata exceeds 8 MiB")
    if set(originals) != {segment.key for segment in header.segments}:
        raise Problem(422, "SOURCE_BACKUP_GRAPH_INCOMPLETE", "all and only necessary originals are required")
    parts = [MAGIC, struct.pack(">I", len(raw)), raw]
    for segment in header.segments:
        data = originals[segment.key]
        if (not isinstance(data, bytes) or len(data) != segment.byte_length
                or hashlib.sha256(data).hexdigest() != segment.sha256):
            raise Problem(422, "SOURCE_BACKUP_INTEGRITY_ERROR", "original bytes differ from the manifest")
        parts.append(data)
    return b"".join(parts)


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def decode_archive(data: bytes) -> _DecodedArchive:
    if not isinstance(data, bytes) or len(data) > MAX_BACKUP_BYTES:
        raise Problem(413, "SOURCE_BACKUP_TOO_LARGE", "backup must be bounded exact bytes")
    if len(data) < len(MAGIC) + 4 or data[:len(MAGIC)] != MAGIC:
        raise Problem(422, "SOURCE_BACKUP_FORMAT_INVALID", "unsupported or truncated source archive")
    header_size = struct.unpack(">I", data[len(MAGIC):len(MAGIC) + 4])[0]
    if not 0 < header_size <= MAX_HEADER_BYTES:
        raise Problem(413, "SOURCE_BACKUP_HEADER_TOO_LARGE", "archive header exceeds its explicit bound")
    offset = len(MAGIC) + 4 + header_size
    if offset > len(data):
        raise Problem(422, "SOURCE_BACKUP_FORMAT_INVALID", "archive header is incomplete")
    raw = data[len(MAGIC) + 4:offset]
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
        header = SourceArchiveHeader.model_validate_json(canonical(value))
        if canonical(header.model_dump(mode="json")) != raw:
            raise ValueError("noncanonical header")
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        raise Problem(422, "SOURCE_BACKUP_FORMAT_INVALID", "invalid, duplicate or noncanonical archive header") from None
    _header_graph(header)
    if offset + sum(segment.byte_length for segment in header.segments) != len(data):
        raise Problem(422, "SOURCE_BACKUP_INTEGRITY_ERROR", "missing segment bytes or unexpected trailing data")
    originals = {}
    for segment in header.segments:
        part = data[offset:offset + segment.byte_length]
        if hashlib.sha256(part).hexdigest() != segment.sha256:
            raise Problem(422, "SOURCE_BACKUP_INTEGRITY_ERROR", "archive segment digest differs")
        originals[segment.key] = part
        offset += segment.byte_length
    return _DecodedArchive(header, MappingProxyType(originals))


class SourceBackupSettings(SourceModel):
    tenant_id: OwnerOid
    authority_domain: Identifier
    container_url: Annotated[str, Field(min_length=1, max_length=2048)]


def make_blob_backup_store(settings: SourceBackupSettings, credential, *, transport=None) -> BlobBackupStore:
    """No startup I/O. Caller owns closing store.container and its credential."""
    from azure.storage.blob import ContainerClient

    settings = checked(settings, SourceBackupSettings)
    options = {} if transport is None else {"transport": transport}
    container = ContainerClient.from_container_url(
        settings.container_url, credential=credential, retry_total=0, retry_connect=0,
        retry_read=0, retry_status=0, retry_to_secondary=False, logging_enable=False,
        connection_timeout=10, read_timeout=20,
        max_single_put_size=MAX_BACKUP_BYTES, max_single_get_size=MAX_BACKUP_BYTES, **options,
    )
    return BlobBackupStore(container, settings.tenant_id)


class _SavedReceipt(SourceModel):
    payload: dict[str, JsonValue]
    etag: Annotated[str, Field(min_length=1, max_length=1024)]

    def record(self) -> Record:
        return Record(self.model_dump(mode="json")["payload"], self.etag)


class _BackupDescription(SourceModel):
    backup_id: BackupId
    owner_oid: OwnerOid
    scope: SourceScope
    snapshot_id: Identifier
    archive_sha256: Digest
    byte_length: Annotated[int, Field(ge=1, le=MAX_BACKUP_BYTES)]
    header_sha256: Digest
    manifest_sha256: Digest
    snapshot_receipt_sha256: Digest
    storage: SourceBackupSettings
    protection: NoAutoGCConfiguration


class SourceBackupPlan(SourceModel):
    description: _BackupDescription
    manifest: SourceSnapshot
    snapshot_receipt: _SavedReceipt
    request: ControlRequest


class SourceBackupReference(SourceModel):
    schema_version: Literal[1] = 1
    description: _BackupDescription
    blob_name: Annotated[str, Field(min_length=1, max_length=512)]
    blob: BackupReceipt

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


class _UploadIntent(SourceModel):
    state: Literal["upload_pending_or_unknown"] = "upload_pending_or_unknown"
    description: _BackupDescription
    request: ControlRequest


class _Completion(SourceModel):
    reference: SourceBackupReference
    request: ControlRequest


class RecoveryAuthorization(SourceModel):
    decision_id: Identifier
    policy_version: Identifier
    principal_oid: OwnerOid
    reference_sha256: Digest
    scope: SourceScope
    current_access_revision: Annotated[int, Field(ge=1)]
    expires_at: datetime
    action: Literal["restore-to-quarantine"] = "restore-to-quarantine"


class RecoveryAuthorizer(Protocol):
    def authorize(self, principal: BehaviorPrincipal, reference: SourceBackupReference) -> RecoveryAuthorization:
        """Read CURRENT server authorization/catalog; absence or unknown MUST deny."""
        ...


@dataclass(frozen=True)
class QuarantinedSourceRecovery:
    reference: SourceBackupReference
    manifest: SourceSnapshot
    originals: Mapping[str, bytes]
    authorization: RecoveryAuthorization
    status: Literal["quarantined"] = "quarantined"


class SourceBackupService:
    def __init__(
        self, backup_store: BackupStore, settings: SourceBackupSettings, *,
        sources: SourceMaterials | None = None,
        protection: NoAutoGCConfiguration | None = None,
        protection_verifier: ProtectionVerifier | None = None,
        recovery_authorizer: RecoveryAuthorizer | None = None,
        clock: Callable[[], datetime],
    ):
        self.backups, self.settings = backup_store, checked(settings, SourceBackupSettings)
        self.sources = sources
        self.protection = None if protection is None else checked(protection, NoAutoGCConfiguration)
        self._verifier, self._recovery, self._clock = protection_verifier, recovery_authorizer, clock
        endpoint = urlsplit(self.settings.container_url)
        if (endpoint.scheme != "https" or not endpoint.netloc or endpoint.username or endpoint.password
                or not endpoint.path.strip("/") or endpoint.query or endpoint.fragment
                or self.settings.container_url.endswith("/")):
            raise Problem(422, "SOURCE_BACKUP_BINDING_INVALID", "an exact HTTPS backup container is required")
        self._check_store()
        if self.protection is not None and (
            self.protection.authority_domain != settings.authority_domain
            or self.protection.container_url != settings.container_url
            or self.protection.protected_prefix != f"v1/{settings.tenant_id}/"
        ):
            raise Problem(422, "SOURCE_BACKUP_BINDING_INVALID", "protection must cover the entire backup namespace")

    def _check_store(self):
        if getattr(self.backups, "tenant_id", None) != self.settings.tenant_id:
            raise Problem(409, "SOURCE_BACKUP_BINDING_INVALID", "backup adapter tenant differs from its trusted binding")
        if isinstance(self.backups, BlobBackupStore) and self.backups.container.url.rstrip("/") != self.settings.container_url:
            raise Problem(409, "SOURCE_BACKUP_BINDING_INVALID", "backup adapter container differs from its binding")

    def _store(self):
        if self.sources is None:
            raise Problem(503, "SOURCE_BACKUP_CONTROL_UNAVAILABLE", "live source authority is required for upload reconciliation")
        return self.sources.store

    def _protection_key(self):
        return control_key("source-backup-protection", self.settings.authority_domain, self.settings.container_url)

    def _verify_protection(self):
        if self.protection is None or self._verifier is None:
            raise Problem(503, "SOURCE_BACKUP_PROTECTION_UNVERIFIED", "trusted current backup protection is required")
        self._check_store()
        self._verifier.verify(self.protection)

    def initialize_protection(self) -> Record:
        self._verify_protection()
        key = self._protection_key()
        request = ControlRequest(request_id=control_key("source-backup-protection-init", key),
                                 request_digest=digest(self.protection.model_dump(mode="json")), read_set=(key,))
        return self._store().transact(request, lambda _: ControlChanges(
            writes=(control_write(key, self.protection),), result={"generation": self.protection.generation}))

    def _human(self, principal, scope, action):
        if not isinstance(principal, BehaviorPrincipal) or principal.kind != "human":
            raise Problem(403, "SOURCE_BACKUP_HUMAN_REQUIRED", "an authenticated delegated human is required")
        principal.require(scope.team_id, action)
        if principal.source_actor_id != "human-" + principal.oid:
            raise Problem(403, "SOURCE_BACKUP_HUMAN_REQUIRED", "canonical human source identity is required")
        if principal.tenant_id != self.settings.tenant_id or scope.tenant_id != self.settings.tenant_id:
            raise Problem(403, "SOURCE_BACKUP_SCOPE_DENIED", "backup tenant differs")

    def _upload_context(self, principal, context, scope):
        self._human(principal, scope, "backup")
        if (not isinstance(context, AuthenticatedSourceContext) or context.tenant_id != principal.tenant_id
                or context.team_id != principal.team or context.actor_id != principal.source_actor_id):
            raise Problem(403, "SOURCE_BACKUP_SCOPE_DENIED", "backup principal and source context must match")
        authorize(context, scope)

    def _actor(self, owner, scope):
        return Actor(owner, "human-" + owner, scope.team_id)

    def _keys(self, description):
        identity = (self.settings.tenant_id, description.owner_oid, description.backup_id)
        return control_key("source-backup-intent", *identity), control_key("source-backup-completion", *identity)

    def _request(self, description, manifest):
        intent, completion = self._keys(description)
        keys = [self._protection_key(), intent, completion,
                control_key("source-snapshot", description.scope.key, description.snapshot_id)]
        keys += [key for node in manifest.nodes for key in (node.reference.key, node.reference.head_key)]
        return ControlRequest(
            request_id=control_key("source-backup-upload-request", self.settings.tenant_id,
                                   description.owner_oid, description.backup_id),
            request_digest=digest(description.model_dump(mode="json")), read_set=tuple(dict.fromkeys(keys)),
        )

    def _archive(self, backup_id, owner_oid, restored):
        saved = _SavedReceipt(payload=restored.receipt.payload, etag=restored.receipt.etag)
        header = SourceArchiveHeader(
            backup_id=backup_id, owner_oid=owner_oid, manifest=restored.manifest,
            snapshot_receipt_sha256=digest(saved.model_dump(mode="json")),
            authority_binding=restored.receipt.payload["binding"],
            guard_revision=restored.receipt.payload["guard_revision"],
            segments=tuple(ArchiveSegment(key=node.reference.key, byte_length=node.reference.byte_length,
                                          sha256=node.reference.sha256)
                           for node in sorted(restored.manifest.nodes, key=lambda node: node.reference.key)),
        )
        return header, encode_archive(header, restored.originals)

    def plan(self, principal: BehaviorPrincipal, context: AuthenticatedSourceContext, *, backup_id: str,
             scope: SourceScope, snapshot_id: str, snapshot_receipt: Record) -> SourceBackupPlan:
        scope = checked(scope, SourceScope)
        self._upload_context(principal, context, scope)
        self._verify_protection()
        self._store()
        restored = self.sources.restore_snapshot(context, scope=scope, snapshot_id=snapshot_id, receipt=snapshot_receipt)
        header, archive = self._archive(backup_id, principal.oid, restored)
        description = _BackupDescription(
            backup_id=backup_id, owner_oid=principal.oid, scope=scope, snapshot_id=snapshot_id,
            archive_sha256=hashlib.sha256(archive).hexdigest(), byte_length=len(archive),
            header_sha256=digest(header.model_dump(mode="json")),
            manifest_sha256=digest(restored.manifest.model_dump(mode="json")),
            snapshot_receipt_sha256=header.snapshot_receipt_sha256, storage=self.settings, protection=self.protection,
        )
        return SourceBackupPlan(description=description, manifest=restored.manifest,
                                snapshot_receipt=_SavedReceipt(payload=snapshot_receipt.payload, etag=snapshot_receipt.etag),
                                request=self._request(description, restored.manifest))

    def _plan(self, principal, context, plan):
        plan = checked(plan, SourceBackupPlan)
        description = plan.description
        self._upload_context(principal, context, description.scope)
        if (description.owner_oid != principal.oid or description.storage != self.settings
                or description.protection != self.protection or plan.manifest.scope != description.scope
                or plan.manifest.snapshot_id != description.snapshot_id
                or digest(plan.manifest.model_dump(mode="json")) != description.manifest_sha256
                or digest(plan.snapshot_receipt.model_dump(mode="json")) != description.snapshot_receipt_sha256
                or plan.request != self._request(description, plan.manifest)):
            raise Problem(409, "SOURCE_BACKUP_PLAN_MISMATCH", "retain the exact original backup plan and identity")
        return plan

    def _generation(self, rows, description):
        config = control_row(rows, self._protection_key(), NoAutoGCConfiguration)
        if config != description.protection:
            raise Problem(409, "SOURCE_BACKUP_PROTECTION_CHANGED", "backup authority protection generation changed")

    def upload(self, principal: BehaviorPrincipal, context: AuthenticatedSourceContext, plan: SourceBackupPlan) -> Record:
        plan = self._plan(principal, context, plan)
        self._verify_protection()
        self._store()
        restored = self.sources.restore_snapshot(context, scope=plan.description.scope,
                                                snapshot_id=plan.description.snapshot_id,
                                                receipt=plan.snapshot_receipt.record())
        header, archive = self._archive(plan.description.backup_id, principal.oid, restored)
        self._archive_matches(plan.description, header, archive)
        intent_key, _ = self._keys(plan.description)
        fresh = False

        def decide(rows):
            nonlocal fresh
            self._generation(rows, plan.description)
            manifest_key = control_key("source-snapshot", plan.description.scope.key, plan.description.snapshot_id)
            if control_row(rows, manifest_key, SourceSnapshot) != plan.manifest:
                raise Problem(409, "SOURCE_BACKUP_PLAN_MISMATCH", "live snapshot differs from the retained plan")
            for node in plan.manifest.nodes:
                event = control_row(rows, node.reference.key, SourceEvent)
                if (event.reference != node.reference or event.necessary != node.necessary
                        or event.sequence != node.sequence):
                    raise Problem(409, "SOURCE_BACKUP_PLAN_MISMATCH", "necessary source graph changed before upload")
                require_source_head(rows, node.reference, expected_access_revision=node.access_revision)
            fresh = True
            return ControlChanges(writes=(control_write(intent_key, _UploadIntent(
                description=plan.description, request=plan.request)),), result={
                    "state": "upload_pending_or_unknown", "backup_id": plan.description.backup_id})

        self._store().transact(plan.request, decide)
        if not fresh:
            raise Problem(409, "SOURCE_BACKUP_RECONCILE_REQUIRED", "upload was consumed; reconcile without another create",
                          request_id=plan.request.request_id)
        try:
            receipt = self.backups.create(self._actor(principal.oid, plan.description.scope),
                                           plan.description.backup_id, archive, plan.description.archive_sha256)
        except Exception as exc:
            raise Problem(503, "SOURCE_BACKUP_UPLOAD_UNKNOWN", "backup upload is uncertain; retain plan and reconcile",
                          request_id=plan.request.request_id, backup_id=plan.description.backup_id,
                          sha256=plan.description.archive_sha256, bytes=plan.description.byte_length) from exc
        return self._complete(plan, receipt)

    def _reference(self, description, receipt):
        try:
            receipt = BackupReceipt.model_validate(receipt.model_dump())
        except (AttributeError, ValueError, TypeError, ValidationError):
            raise Problem(502, "SOURCE_BACKUP_RECEIPT_INVALID", "exact native backup receipt is required") from None
        if (receipt.backup_id != description.backup_id or receipt.owner_oid != description.owner_oid
                or receipt.sha256 != description.archive_sha256 or receipt.bytes != description.byte_length):
            raise Problem(502, "SOURCE_BACKUP_INTEGRITY_ERROR", "native backup differs from the original ID/hash/size")
        return SourceBackupReference(
            description=description, blob=receipt,
            blob_name=f"v1/{description.scope.tenant_id}/{description.owner_oid}/{description.backup_id}",
        )

    def _archive_matches(self, description, header, data):
        if (header.backup_id != description.backup_id or header.owner_oid != description.owner_oid
                or header.manifest.scope != description.scope or header.manifest.snapshot_id != description.snapshot_id
                or header.snapshot_receipt_sha256 != description.snapshot_receipt_sha256
                or digest(header.manifest.model_dump(mode="json")) != description.manifest_sha256
                or digest(header.model_dump(mode="json")) != description.header_sha256
                or len(data) != description.byte_length or hashlib.sha256(data).hexdigest() != description.archive_sha256):
            raise Problem(502, "SOURCE_BACKUP_INTEGRITY_ERROR", "archive is not the exact independently retained snapshot")

    def _download(self, reference):
        self._check_store()
        if reference.description.storage != self.settings or self._reference(
                reference.description, reference.blob) != reference:
            raise Problem(409, "SOURCE_BACKUP_BINDING_INVALID", "backup reference locator/binding differs")
        result = self.backups.download(
            self._actor(reference.description.owner_oid, reference.description.scope),
            reference.blob.backup_id, reference.blob.version_id, reference.blob.etag,
        )
        if result.receipt != reference.blob:
            raise Problem(502, "SOURCE_BACKUP_INTEGRITY_ERROR", "download returned a different native backup version")
        decoded = decode_archive(result.data)
        self._archive_matches(reference.description, decoded.header, result.data)
        return decoded

    def _complete(self, plan, receipt):
        reference = self._reference(plan.description, receipt)
        self._download(reference)
        self._verify_protection()
        intent_key, completion_key = self._keys(plan.description)
        request = ControlRequest(
            request_id=control_key("source-backup-complete-request", completion_key),
            request_digest=digest(reference.model_dump(mode="json")),
            read_set=(self._protection_key(), intent_key, completion_key),
        )

        def decide(rows):
            self._generation(rows, plan.description)
            intent = control_row(rows, intent_key, _UploadIntent)
            if intent.description != plan.description or intent.request != plan.request:
                raise Problem(409, "SOURCE_BACKUP_PLAN_MISMATCH", "stored upload intent differs")
            return ControlChanges(writes=(control_write(completion_key, _Completion(
                reference=reference, request=request)),), result={"reference": reference.model_dump(mode="json")})

        return self._store().transact(request, decide)

    def reconcile_upload(self, principal: BehaviorPrincipal, context: AuthenticatedSourceContext,
                         plan: SourceBackupPlan) -> Record:
        plan = self._plan(principal, context, plan)
        self._verify_protection()
        intent_key, completion_key = self._keys(plan.description)
        rows = self._store().read_snapshot((self._protection_key(), intent_key, completion_key))
        self._generation(rows, plan.description)
        if rows[intent_key] is None:
            raise Problem(503, "SOURCE_BACKUP_UPLOAD_UNKNOWN", "original upload intent is not confirmed; do not recreate it")
        intent = control_row(rows, intent_key, _UploadIntent)
        if intent.request != plan.request or intent.description != plan.description:
            raise Problem(409, "SOURCE_BACKUP_PLAN_MISMATCH", "backup ID is already bound to another plan")
        self.sources.verify_receipt(rows[intent_key], plan.request, intent_key)
        if rows[completion_key] is not None:
            complete = control_row(rows, completion_key, _Completion)
            if complete.reference.description != plan.description:
                raise Problem(409, "SOURCE_BACKUP_PLAN_MISMATCH", "completion refers to a different backup")
            receipt = self.sources.verify_receipt(rows[completion_key], complete.request, completion_key)
            self._download(complete.reference)
            return receipt
        try:
            native = self.backups.receipt(self._actor(principal.oid, plan.description.scope), plan.description.backup_id)
        except Problem as exc:
            raise Problem(503, "SOURCE_BACKUP_UPLOAD_UNKNOWN", "original backup upload is not verified; no replacement upload") from exc
        return self._complete(plan, native)

    def _recovery_authorization(self, principal, reference):
        self._human(principal, reference.description.scope, "restore")
        if reference.description.owner_oid != principal.oid:
            raise Problem(403, "SOURCE_BACKUP_SCOPE_DENIED", "backup belongs to another human")
        if self._recovery is None:
            raise Problem(403, "SOURCE_BACKUP_RECOVERY_DENIED", "current explicit server recovery authorization is required")
        try:
            proof = self._recovery.authorize(principal, reference)
        except Problem:
            raise
        except Exception as exc:
            raise Problem(503, "SOURCE_BACKUP_RECOVERY_UNKNOWN", "current recovery authority is unavailable") from exc
        if not isinstance(proof, RecoveryAuthorization):
            raise Problem(403, "SOURCE_BACKUP_RECOVERY_DENIED", "a trusted recovery decision, not a boolean, is required")
        proof = checked(proof, RecoveryAuthorization)
        if (proof.principal_oid != principal.oid or proof.reference_sha256 != reference.sha256
                or proof.scope != reference.description.scope or utc(proof.expires_at) <= utc(self._clock())):
            raise Problem(403, "SOURCE_BACKUP_RECOVERY_DENIED", "current recovery decision does not cover this exact reference")
        return proof

    def restore_quarantined(self, principal: BehaviorPrincipal, reference: SourceBackupReference) -> QuarantinedSourceRecovery:
        reference = checked(reference, SourceBackupReference)
        authorization = self._recovery_authorization(principal, reference)
        decoded = self._download(reference)
        if self._recovery_authorization(principal, reference) != authorization:
            raise Problem(409, "SOURCE_BACKUP_RECOVERY_CHANGED", "recovery access changed while reading backup data")
        return QuarantinedSourceRecovery(reference, decoded.header.manifest, decoded.originals, authorization)
