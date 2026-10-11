"""Explicit operator-only bounded SDK-to-SDK rotation in an existing C1 authority.

plan/authorize are read-only. apply requires an independently enabled operator
deployment and sends one guarded EGT; reconcile never sends it again. No
bootstrap, readiness activation, claim approval, model diagnostic/send, native
stop or budget refill is implemented here. All observations are server files,
re-read at plan, approval and final-send time, never HTTP-supplied booleans.

The retired catalog is a bounded immutable hash manifest. Normal startup checks
its rows/receipts and replays only receipt-verified installation preimages into
a read-only store. The old pin and checksum-to-SDK pointers are never rewritten.
Additional immutable receipt-covered history is allowed after installation;
it is not an authorization to run an old job. Existing source/claim/policy rows
are protected along with the retired work. A fresh logical memory subject is
allowed only in the same source scope, with both subject IDs pinned in the plan;
its predecessor index remains protected. If the subject is unchanged, that
index may append newly authorized claims. Old immutable claims cannot be edited.
The worker/delivery contracts remain unchanged; image/version pins live in the
target observation. A single fresh native grant can carry bounded multiple uses.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from . import behavior_migration as m
from .behavior_foundry import FoundryTarget, NativeExecutionGrant
from .behavior_recovery import FinishedResultProof, ModelIntentProof, _ordered_intents
from .behavior_runtime_rotation_history import (
    binding_scopes, historical_shared_row, receipt_catalog, require, shared_keys, validate_additions, validate_retirement,
)
from .behavior_runtime_transition import (
    RuntimeRetirement, RuntimeTransition, _Entity, _Image, _SnapshotClient, _counter_hash, _payload_hash,
)
from .behavior_server_config import ServerConfiguration, UUID
from .behavior_store import GUARD_KEY, ControlChanges, ControlRequest, ControlWrite, InlineMaterial
from .errors import Problem
from .models import Identifier
from .records import Record
from .source_materials import Digest, canonical, digest, utc


def rotation_key(kind, identity):
    return m.control_key("runtime-rotation-" + kind, identity)


def rotation_code_sha256():
    """Include the canonical CLI without changing any legacy code fingerprint."""
    entry = Path(__file__).resolve().parent.parent / "behavior_server_main.py"
    return digest({"shared_context": m.code_sha256(),
                   "behavior_server_main.py": hashlib.sha256(entry.read_bytes()).hexdigest()})


class _InventorySnapshotClient(_SnapshotClient):
    def query_entities(self, *, query_filter, parameters, raw_request_hook, **_):
        require(query_filter == "PartitionKey eq @partition"
                and parameters == {"partition": self.scope.partition_key}, "historical inventory escaped its partition")
        url = self.url if urlsplit(self.url).path.strip("/") else self.url.rstrip("/") + "/" + self.table_name
        raw_request_hook(SimpleNamespace(http_request=SimpleNamespace(method="GET", url=url, body=None)))
        rows = []
        guard = {**self.inventory.guard.payload, "schema_version": 2, "configuration_sha256": self.epoch}
        values = [(GUARD_KEY, Record(guard, self.inventory.guard.etag))]
        values.extend(("behavior:v1:row:" + key, value) for key, value in self.inventory.rows.items())
        values.extend(("behavior:v1:receipt:" + key, value) for key, value in self.inventory.receipts.items())
        for key, record in values:
            rows.append(_Entity({"PartitionKey": self.scope.partition_key, "RowKey": key,
                                 "Payload": json.dumps(record.payload, ensure_ascii=False,
                                                       separators=(",", ":")).encode()}, record.etag))
        return SimpleNamespace(by_page=lambda: iter((rows,)))


def _snapshot_store(store, inventory, config):
    return m.MigrationStore(_InventorySnapshotClient(store, inventory, guard_epoch=config.sha256), store._scope)


class CurrentTarget(m._Model):
    """Independent provider + API-MI observations, not Hosted process ownership.

    `target.agent_oid` remains the Hosted agent's MI. The session owner is the
    principal behind the unchanged API `managed_identity_client_id`; those are
    different identities. The identity observation binds tenant/client/principal
    without retaining an access token. All digests identify observer-held bytes.
    """
    target: FoundryTarget
    session_owner_oid: UUID
    credential_client_id: UUID
    credential_principal_oid: UUID
    credential_tenant_id: UUID
    identity_observation_sha256: Digest
    agent_image_sha256: Digest
    provider_session_status: Literal["idle"]
    provider_observation_sha256: Digest


class RotationRetirement(RuntimeRetirement):
    schema_version: Literal["fa-runtime-rotation-retirement-v1"] = "fa-runtime-rotation-retirement-v1"
    observer_oid: Identifier
    current_targets: Annotated[tuple[CurrentTarget, ...], Field(min_length=1, max_length=1)]


@dataclass(frozen=True)
class RotationEvidenceFiles:
    """Trusted installer paths; constructing this catalog performs no I/O."""
    sources: Path
    backups: Path
    retirement: Path

    def read(self):
        values = {}
        for lane in ("sources", "backups", "retirement"):
            path = getattr(self, lane)
            try:
                if path.stat().st_size > 1024 * 1024:
                    raise Problem(413, "ROTATION_EVIDENCE_TOO_LARGE", "bounded server observation file required")
                with path.open("rb") as stream:
                    values[lane] = stream.read(1024 * 1024 + 1)
                if len(values[lane]) > 1024 * 1024:
                    raise Problem(413, "ROTATION_EVIDENCE_TOO_LARGE", "server observation grew beyond its bound")
            except OSError as exc:
                raise Problem(503, "ROTATION_EVIDENCE_UNAVAILABLE", "trusted server observation is unavailable") from exc
        return values


class ModelLiability(m._Model):
    configuration_sha256: Digest
    job_id: Identifier
    work_id: Identifier
    root_budget_id: Identifier
    model_request_sha256: Digest
    model_outcome: Literal["unknown"]
    final_delivery_id: Identifier
    receipt_evidence_id: Identifier


class PreSdkLiability(m._Model):
    configuration_sha256: Digest
    job_id: Identifier
    work_id: Identifier
    root_budget_id: Identifier
    model_request_sha256: None = None
    model_outcome: Literal["not_requested"]
    final_delivery_id: Identifier
    receipt_evidence_id: Identifier


class MultiModelLiability(m._Model):
    liability_kind: Literal["multiple_model_requests"]
    configuration_sha256: Digest
    job_id: Identifier
    work_id: Identifier
    root_budget_id: Identifier
    model_outcome: Literal["unknown"]
    model_intents: Annotated[tuple[ModelIntentProof, ...], Field(min_length=2, max_length=6)]
    final_delivery_id: Identifier
    receipt_evidence_id: Identifier
    _ordered = field_validator("model_intents")(_ordered_intents)


class FinishedResultLiability(m._Model):
    liability_kind: Literal["finished_result_platform_stop"]
    configuration_sha256: Digest
    job_id: Identifier
    work_id: Identifier
    root_budget_id: Identifier
    business_result: Literal["succeeded"]
    model_outcome: Literal["unknown"]
    model_intents: Annotated[tuple[ModelIntentProof, ...], Field(min_length=1, max_length=6)]
    finished_result: FinishedResultProof
    final_delivery_id: Identifier
    receipt_evidence_id: Identifier
    _ordered = field_validator("model_intents")(_ordered_intents)


class SubjectLineage(m._Model):
    previous_subject: Identifier
    successor_subject: Identifier
    source_scopes_sha256: Digest


def _subject_lineage(old, new):
    before = binding_scopes(old, old.bindings[0])
    after = binding_scopes(new, new.bindings[0])
    require(before == after, "a logical subject rotation cannot move the source namespace")
    return SubjectLineage(
        previous_subject=old.bindings[0].subject, successor_subject=new.bindings[0].subject,
        source_scopes_sha256=digest([scope.model_dump(mode="json") for scope in before]),
    )


class RotationPlan(m._Model):
    schema_version: Literal["fa-runtime-rotation-plan-v1", "fa-runtime-rotation-plan-v2"] = "fa-runtime-rotation-plan-v1"
    rotation_id: Identifier
    old_configuration_sha256: Digest
    new_configuration_sha256: Digest
    authority_domain: Identifier
    control_binding: Digest
    code_sha256: Digest
    inventory_sha256: Digest
    untouched_sha256: Digest
    counter_sha256: Digest
    previous_history_sha256: Digest
    observations_sha256: dict[str, Digest]
    grants_sha256: Digest
    images: Annotated[tuple[_Image, ...], Field(min_length=1, max_length=80)]
    liability: ModelLiability | PreSdkLiability | MultiModelLiability | FinishedResultLiability
    subject_lineage: SubjectLineage
    successor_binding_id: Identifier
    guard: m.GuardAnchor
    valid_until: datetime

    _utc = field_validator("valid_until")(utc)

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))


class RotationAuthorization(m._Model):
    schema_version: Literal["fa-runtime-rotation-authorization-v1"] = "fa-runtime-rotation-authorization-v1"
    decision_id: Identifier
    operator_oid: Identifier
    plan_sha256: Digest
    action: Literal[
        "rotate-sdk-retain-cancelled-model-liability", "rotate-sdk-retain-cancelled-pre-sdk-result",
    ] = "rotate-sdk-retain-cancelled-model-liability"
    not_before: datetime
    expires_at: datetime

    _utc = field_validator("not_before", "expires_at")(utc)


class CatalogEntry(m._Model):
    kind: Literal["row", "receipt"]
    key: Identifier
    sha256: Digest


class CatalogChunk(m._Model):
    schema_version: Literal["fa-runtime-rotation-catalog-v1"] = "fa-runtime-rotation-catalog-v1"
    rotation_id: Identifier
    index: Annotated[int, Field(ge=0, le=42)]
    entries: Annotated[tuple[CatalogEntry, ...], Field(min_length=1, max_length=96)]


class CatalogReference(m._Model):
    row_id: Identifier
    sha256: Digest


class SharedPreimage(m._Model):
    row_id: Identifier
    payload: str
    etag: str


class SharedPreimageArchive(m._Model):
    schema_version: Literal["fa-runtime-shared-preimages-v1"] = "fa-runtime-shared-preimages-v1"
    entries: Annotated[tuple[SharedPreimage, ...], Field(max_length=80)]


class SharedPreimageChunk(m._Model):
    schema_version: Literal["fa-runtime-shared-preimage-chunk-v1"] = "fa-runtime-shared-preimage-chunk-v1"
    rotation_id: Identifier
    index: Annotated[int, Field(ge=0, lt=98)]
    count: Annotated[int, Field(ge=1, le=98)]
    configuration_sha256: Digest
    inventory_sha256: Digest
    archive_sha256: Digest
    material_sha256: Digest
    archive_bytes: Annotated[int, Field(ge=1, le=4 * 1024 * 1024)]
    bytes: Annotated[int, Field(ge=1, le=24 * 1024)]


class RotationCurrent(m._Model):
    schema_version: Literal["fa-runtime-rotation-current-v1"] = "fa-runtime-rotation-current-v1"
    configuration_sha256: Digest
    history_key: Identifier
    history_sha256: Digest
    authorization_key: Identifier


class RotationHistory(m._Model):
    schema_version: Literal[
        "fa-runtime-rotation-history-v1", "fa-runtime-rotation-history-v2", "fa-runtime-rotation-history-v3",
    ] = "fa-runtime-rotation-history-v1"
    plan: RotationPlan
    authorization: RotationAuthorization
    previous_configuration: dict
    before_guard: dict
    request: ControlRequest
    applied_at: datetime
    catalog: Annotated[tuple[CatalogReference, ...], Field(min_length=1, max_length=43)]
    previous_current: RotationCurrent | None = Field(default=None, exclude_if=lambda value: value is None)
    shared_preimages: Annotated[tuple[_Image, ...], Field(max_length=80)] = Field(
        default=(), exclude_if=lambda value: not value)
    shared_preimage_chunks: Annotated[tuple[CatalogReference, ...], Field(max_length=98)] = Field(
        default=(), exclude_if=lambda value: not value)

    _utc = field_validator("applied_at")(utc)


def _shared_preimage_writes(inventory, config, plan):
    changed = {image.row_id for image in plan.images}
    shared = shared_keys(config)
    archive = SharedPreimageArchive(entries=tuple(SharedPreimage(
        row_id=key, payload=json.dumps(row.payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        etag=row.etag,
    ) for key, row in sorted(inventory.rows.items()) if key in shared and key not in changed))
    raw = archive.model_dump_json().encode("utf-8")
    pieces = tuple(raw[offset:offset + 24 * 1024] for offset in range(0, len(raw), 24 * 1024))
    if len(raw) > 4 * 1024 * 1024 or len(pieces) > 98:
        raise Problem(413, "BEHAVIOR_BATCH_TOO_LARGE", "complete shared preimages cannot fit one bounded cutover")
    writes, references = [], []
    for index, piece in enumerate(pieces):
        material = InlineMaterial(data=piece, sha256=hashlib.sha256(piece).hexdigest())
        chunk = SharedPreimageChunk(
            rotation_id=plan.rotation_id, index=index, count=len(pieces),
            configuration_sha256=config.sha256, inventory_sha256=plan.inventory_sha256,
            archive_sha256=hashlib.sha256(raw).hexdigest(), archive_bytes=len(raw),
            material_sha256=material.sha256, bytes=len(piece),
        )
        key = rotation_key("preimages", plan.rotation_id + "-" + str(index))
        references.append(CatalogReference(row_id=key, sha256=digest(chunk.model_dump(mode="json"))))
        writes.append(ControlWrite(row_id=key, mode="create", data=chunk.model_dump(mode="json"), material=material))
    return tuple(writes), tuple(references)


def _read_shared_preimages(inventory, history):
    if history.schema_version != "fa-runtime-rotation-history-v3":
        require(not history.shared_preimage_chunks, "legacy history cannot introduce chunk references")
        return history.shared_preimages
    require(not history.shared_preimages and history.shared_preimage_chunks,
            "chunked history must retain exactly its external preimage archive")
    raw, archive_sha, archive_bytes = bytearray(), None, None
    for index, ref in enumerate(history.shared_preimage_chunks):
        row = m._row(inventory, ref.row_id)
        value = m._parsed(row.payload["data"], SharedPreimageChunk)
        material = m._parsed(row.payload["material"], InlineMaterial)
        require(ref.row_id == rotation_key("preimages", history.plan.rotation_id + "-" + str(index))
                and value.rotation_id == history.plan.rotation_id and value.index == index
                and value.count == len(history.shared_preimage_chunks)
                and value.configuration_sha256 == history.plan.old_configuration_sha256
                and value.inventory_sha256 == history.plan.inventory_sha256
                and ref.sha256 == digest(value.model_dump(mode="json"))
                and value.material_sha256 == material.sha256 and value.bytes == len(material.data)
                and (index == 0 or (value.archive_sha256, value.archive_bytes) == (archive_sha, archive_bytes)),
                "shared preimage chunk scope, order, material or hash differs")
        archive_sha, archive_bytes = value.archive_sha256, value.archive_bytes
        raw.extend(material.data)
        require(len(raw) <= archive_bytes, "shared preimage archive exceeds its declared bound")
    require(len(raw) == archive_bytes and hashlib.sha256(raw).hexdigest() == archive_sha,
            "shared preimage archive is incomplete or corrupt")
    archive = SharedPreimageArchive.model_validate_json(raw)
    require(archive.model_dump_json().encode("utf-8") == raw, "shared preimage archive has noncanonical fields")
    return tuple(_Image(row_id=entry.row_id, before_payload=entry.payload, after_payload=entry.payload,
                        before_etag=entry.etag) for entry in archive.entries)


class _WireMeasured(Exception):
    pass


def _measure_layout(store, inventory, old, new, request, changes):
    """Run the unchanged store and public Table SDK serializer without transport I/O."""
    from azure.core.credentials import AzureSasCredential
    from azure.core.pipeline.transport import HttpTransport
    from azure.data.tables import TableClient
    if len(changes.writes) + 2 > store._scope.max_operations:
        raise Problem(413, "BEHAVIOR_BATCH_TOO_LARGE", "whole rotation exceeds its operation limit")

    class NoNetwork(HttpTransport):
        def open(self):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def send(self, *args, **kwargs):
            raise Problem(500, "ROTATION_LAYOUT_NETWORK_DENIED", "wire layout validation cannot send a request")

    class PreviewClient(_InventorySnapshotClient):
        measurements = None

        def submit_transaction(self, operations, *, raw_request_hook, **kwargs):
            def inspect(request):
                raw_request_hook(request)
                body = request.http_request.body
                sizes = {GUARD_KEY: len(json.dumps(m._MigratedGuard(
                    binding=inventory.guard.payload["binding"], revision=inventory.guard.payload["revision"],
                    configuration_sha256=old.sha256,
                ).model_dump(mode="json"), separators=(",", ":")).encode())}
                sizes.update({"behavior:v1:row:" + key: len(json.dumps(
                    row.payload, ensure_ascii=False, separators=(",", ":")).encode())
                    for key, row in inventory.rows.items()})
                sizes.update({"behavior:v1:receipt:" + key: len(json.dumps(
                    row.payload, ensure_ascii=False, separators=(",", ":")).encode())
                    for key, row in inventory.receipts.items()})
                sizes.update({operation[1]["RowKey"]: len(operation[1]["Payload"]) for operation in operations})
                if (len(sizes) > m.DEFAULT_INVENTORY_MAX_ENTITIES
                        or sum(sizes.values()) > m.DEFAULT_INVENTORY_MAX_BYTES):
                    raise Problem(413, "ROTATION_INVENTORY_LIMIT", "complete cutover would exceed retained inventory bounds")
                self.measurements = {
                    "operations": len(operations), "multipart_bytes": len(body.encode() if isinstance(body, str) else body),
                    "max_payload_bytes": max(len(operation[1]["Payload"]) for operation in operations),
                    "inventory_entities": len(sizes), "inventory_bytes": sum(sizes.values()),
                }
                raise _WireMeasured()
            # The inert SAS avoids all token acquisition; the request hook
            # always terminates after the real multipart serializer, before send.
            with TableClient("://".join(store._origin), store._scope.table_name,
                             credential=AzureSasCredential("sig=offline-layout-only"),
                             transport=NoNetwork()) as client:
                client.submit_transaction(operations, raw_request_hook=inspect, **kwargs)

    client = PreviewClient(store, inventory, guard_epoch=old.sha256)
    preview = m.MigrationStore(client, store._scope)
    try:
        preview.transact(request, lambda _: changes, expected_guard=inventory.guard,
                         install_configuration_sha256=new.sha256)
    except _WireMeasured:
        return client.measurements
    raise Problem(500, "ROTATION_LAYOUT_UNVERIFIED", "wire layout did not reach its bounded serialization gate")


def _rotation_action(plan):
    return ("rotate-sdk-retain-cancelled-pre-sdk-result" if plan.liability.model_outcome == "not_requested"
            else "rotate-sdk-retain-cancelled-model-liability")


def _configuration_change(old, new):
    require(old.workload_version == new.workload_version == "assistant-v1.2"
            and old.workers.capacity == new.workers.capacity == 1
            and len(old.bindings) == len(new.bindings) == 1,
            "only one same-authority C1 SDK binding may rotate")
    before, after = old.model_dump(mode="json"), new.model_dump(mode="json")
    for field in ("deployment_id", "bindings", "policies", "operations"):
        before.pop(field)
        after.pop(field)
    for data in (before, after):
        data["assistant"].pop("profiles")
        for field in ("path", "catalog_id", "minimum_revision"):
            data["evidence"].pop(field)
        data["targets"][0].pop("agent_version")
        data["targets"][0].pop("session_id")
    require(before == after, "authority, identity, storage, root budget, pool or contract change is prohibited")
    require(new.evidence.minimum_revision >= old.evidence.minimum_revision, "evidence revision cannot regress")
    require(old.deployment_id != new.deployment_id
            and old.targets[0].session_id != new.targets[0].session_id
            and int(old.targets[0].agent_version) < int(new.targets[0].agent_version),
            "a new deployment, immutable agent version and MI-owned session are required")
    old_binding, new_binding = old.bindings[0], new.bindings[0]
    for field in ("binding_id", "event_id", "job_id", "operation_id", "attempt_id", "grant_id"):
        require(getattr(old_binding, field) != getattr(new_binding, field), "new work identifiers must be unique")
    for field in ("owner_record_id", "conversation_id", "policy_id"):
        require(getattr(old_binding.owner, field) != getattr(new_binding.owner, field),
                "old owner and policy records cannot be renewed in place")
    require(old_binding.work.work_id != new_binding.work.work_id
            and not set(old_binding.claim_ids) & set(new_binding.claim_ids)
            and old.assistant.profiles[0].artifact_id != new.assistant.profiles[0].artifact_id,
            "new work, claim and artifact identifiers are required")
    require(old_binding.owner.root_budget_id == new_binding.owner.root_budget_id
            and old_binding.root_request_id == new_binding.root_request_id,
            "the successor must use the original root budget/request")
    require(old_binding.source_ids == new_binding.source_ids
            and old.policies[0].policy.source_visibility == new.policies[0].policy.source_visibility
            and old.assistant.profiles[0].source == new.assistant.profiles[0].source
            and old.assistant.profiles[0].legacy_source_work == new.assistant.profiles[0].legacy_source_work,
            "rotation cannot hide history in a new source namespace")
    _subject_lineage(old, new)


class RuntimeRotation:
    _parse = staticmethod(RuntimeTransition._parse)
    _operator = RuntimeTransition._operator
    _changes = RuntimeTransition._changes

    def __init__(self, store, old, new, evidence: RotationEvidenceFiles, *, native_grants, clock,
                 allow_apply=False):
        self.old = ServerConfiguration.model_validate_json(old.model_dump_json())
        self.new = ServerConfiguration.model_validate_json(new.model_dump_json())
        _configuration_change(self.old, self.new)
        require(isinstance(store, m.MigrationStore) and store._installation_fence is None
                and store._scope == old.table.scope and store._binding == old.workers.control_binding,
                "an isolated unbound operator store in the original authority is required")
        require(type(evidence) is RotationEvidenceFiles and type(allow_apply) is bool,
                "explicit server observation files and apply switch required")
        self.store, self.evidence, self.clock, self.allow_apply = store, evidence, clock, allow_apply
        self.grants = tuple(NativeExecutionGrant.model_validate_json(item.model_dump_json()) for item in native_grants)
        require(all(item.active for item in self.grants), "new native grants must be explicitly active")

    def _evidence(self):
        from .behavior_server_config import ProtectionObservation
        from .behavior_api import _nonfinite, _unique
        raw = self.evidence.read()
        try:
            for content in raw.values():
                json.loads(content, object_pairs_hook=_unique, parse_constant=_nonfinite)
            retirement = RotationRetirement.model_validate_json(raw["retirement"])
            observations = {lane: ProtectionObservation.model_validate_json(raw[lane]) for lane in ("sources", "backups")}
        except (ValueError, TypeError):
            raise Problem(409, "ROTATION_EVIDENCE_INVALID", "server observations must match exact typed schemas") from None
        # Reuse the existing protection/retirement/time checks without changing
        # checksum transition behavior or manufacturing an installed transition.
        checked = SimpleNamespace(
            old=self.old, new=self.new, clock=self.clock, retirement=retirement,
            observations=observations,
        )
        until = RuntimeTransition._fresh(checked)
        require(retirement.observer_oid == self.old.evidence.observer_oid
                and tuple(item.target for item in retirement.current_targets) == self.new.active_targets,
                "fresh independently observed current target is required")
        for item in retirement.current_targets:
            require(item.session_owner_oid == item.credential_principal_oid
                    and item.credential_client_id == self.new.identity.managed_identity_client_id
                    and item.credential_tenant_id == self.new.identity.tenant_id
                    and item.credential_principal_oid not in {
                        self.new.identity.human_oid, *(agent.oid for agent in self.new.identity.agents),
                    },
                    "new version-pinned session must belong to the original API MI, not the human or Hosted MI")
        return retirement, {lane: hashlib.sha256(value).hexdigest() for lane, value in raw.items()}, until

    def plan(self, principal, *, rotation_id):
        self._operator(principal)
        retired, observations, until = self._evidence()
        inventory = self.store.read_inventory()
        # Installation verification is read-only and must not bind the operator.
        original = m.installed_origin(_snapshot_store(self.store, inventory, self.old), self.old)
        predecessors = [ServerConfiguration.model_validate_json(canonical(row.payload["data"]["previous_configuration"]))
                        for row in inventory.rows.values()
                        if row.payload["data"].get("schema_version") in {
                            "fa-runtime-rotation-history-v1", "fa-runtime-rotation-history-v2",
                            "fa-runtime-rotation-history-v3"}]
        require(len(predecessors) < 16, "rotation lineage exceeds its bounded replay depth")
        prior, liability = validate_retirement(inventory, self.old)
        subject = _subject_lineage(self.old, self.new)
        if subject.previous_subject != subject.successor_subject:
            require(all(m.control_key("memory-subject", scope.key, subject.successor_subject) not in inventory.rows
                        for scope in binding_scopes(self.new, self.new.bindings[0])),
                    "successor subject already exists; rotation cannot overwrite a prior claim index")
        require(prior.plan.code_sha256 == retired.old_code_sha256, "retirement must identify the installed predecessor code")
        old_ids = {b.work.work_id for b in original.bindings}
        old_ids.update(b.job_id for b in original.bindings)
        require(self.new.bindings[0].work.work_id not in old_ids and self.new.bindings[0].job_id not in old_ids,
                "new work cannot reuse pre-SDK jobs")
        for predecessor in (original, *predecessors, self.old):
            old_binding_ids = {getattr(binding, field) for binding in predecessor.bindings
                               for field in ("binding_id", "event_id", "job_id", "operation_id", "attempt_id", "grant_id")}
            old_owner_ids = {getattr(binding.owner, field) for binding in predecessor.bindings
                             for field in ("owner_record_id", "conversation_id", "policy_id")}
            successor = self.new.bindings[0]
            require(not old_binding_ids & {getattr(successor, field) for field in (
                "binding_id", "event_id", "job_id", "operation_id", "attempt_id", "grant_id")}
                and not old_owner_ids & {getattr(successor.owner, field) for field in (
                    "owner_record_id", "conversation_id", "policy_id")}
                and not set(successor.claim_ids) & {claim for binding in predecessor.bindings for claim in binding.claim_ids},
                "successor identifiers collide with retained lineage")
        images = self._changes(inventory)
        excluded = {GUARD_KEY, *("behavior:v1:row:" + image.row_id for image in images)}
        excluded.add("behavior:v1:row:" + rotation_key("current", self.old.table.scope.authority_domain))
        plan = RotationPlan(
            schema_version=("fa-runtime-rotation-plan-v2" if
                            rotation_key("current", self.old.table.scope.authority_domain) in inventory.rows
                            else "fa-runtime-rotation-plan-v1"),
            rotation_id=rotation_id, old_configuration_sha256=self.old.sha256,
            new_configuration_sha256=self.new.sha256, authority_domain=self.old.table.scope.authority_domain,
            control_binding=self.store._binding, code_sha256=rotation_code_sha256(),
            inventory_sha256=digest(dict(inventory.entity_sha256)),
            untouched_sha256=digest({key: value for key, value in inventory.entity_sha256.items() if key not in excluded}),
            counter_sha256=_counter_hash(inventory.rows), previous_history_sha256=digest(prior.model_dump(mode="json")),
            observations_sha256=observations, grants_sha256=digest([item.model_dump(mode="json") for item in self.grants]),
            images=images, liability=liability, subject_lineage=subject,
            successor_binding_id=self.new.bindings[0].binding_id,
            guard=m.GuardAnchor(binding=self.store._binding, revision=inventory.guard.payload["revision"],
                                etag=inventory.guard.etag), valid_until=until,
        )
        for key in (rotation_key("history", rotation_id), rotation_key("apply", rotation_id)):
            require(key not in inventory.rows and key not in inventory.receipts, "rotation ID has already been used")
        # Reserve the longest legal decision ID and timestamp encodings before
        # the operator can approve a plan which cannot fit one atomic cutover.
        timestamp = plan.valid_until.replace(microsecond=999999)
        preview_approval = RotationAuthorization(
            decision_id="x" * 64, operator_oid=principal.oid, plan_sha256=plan.sha256,
            action=_rotation_action(plan), not_before=timestamp - timedelta(seconds=1), expires_at=timestamp)
        changes = self._layout(plan, preview_approval, inventory, timestamp)
        _measure_layout(self.store, inventory, self.old, self.new, self._request(plan, preview_approval), changes)
        return plan

    def authorize(self, principal, plan, *, decision_id, expires_at):
        self._operator(principal)
        require(plan == self.plan(principal, rotation_id=plan.rotation_id), "approved rotation preimage changed")
        now = utc(self.clock())
        require(now < utc(expires_at) <= min(plan.valid_until, now + timedelta(minutes=15)),
                "fresh independently bounded human authorization required")
        approval = RotationAuthorization(decision_id=decision_id, operator_oid=principal.oid,
                                         plan_sha256=plan.sha256, action=_rotation_action(plan),
                                         not_before=now, expires_at=expires_at)
        inventory = self.store.read_inventory()
        require(digest(dict(inventory.entity_sha256)) == plan.inventory_sha256, "approved inventory changed")
        _measure_layout(self.store, inventory, self.old, self.new, self._request(plan, approval),
                        self._layout(plan, approval, inventory, now))
        return approval

    def _authorized(self, principal, plan, approval, *, historical=False):
        self._operator(principal)
        require(approval.operator_oid == principal.oid and approval.plan_sha256 == plan.sha256
                and approval.action == _rotation_action(plan)
                and plan.old_configuration_sha256 == self.old.sha256
                and plan.new_configuration_sha256 == self.new.sha256
                and plan.control_binding == self.store._binding
                and approval.not_before < approval.expires_at <= min(plan.valid_until,
                                                                     approval.not_before + timedelta(minutes=15)),
                "exact rotation authorization/configuration differs")
        if not historical:
            _, observations, until = self._evidence()
            require(plan.code_sha256 == rotation_code_sha256() and observations == plan.observations_sha256
                    and plan.grants_sha256 == digest([item.model_dump(mode="json") for item in self.grants])
                    and approval.not_before <= utc(self.clock()) < min(approval.expires_at, until),
                    "code, server observations, grants or authorization window changed")

    @staticmethod
    def _request(plan, approval):
        return ControlRequest(request_id=rotation_key("apply", plan.rotation_id),
                              request_digest=digest([plan.sha256, approval.model_dump(mode="json")]),
                              read_set=(*(image.row_id for image in plan.images),
                                        *((rotation_key("current", plan.authority_domain),)
                                          if plan.schema_version == "fa-runtime-rotation-plan-v2" else ())))

    def _layout(self, plan, approval, inventory, applied_at):
        request = self._request(plan, approval)
        hkey, akey = rotation_key("history", plan.rotation_id), rotation_key("authorization", approval.decision_id)
        current_key = rotation_key("current", plan.authority_domain)
        previous_current = (None if current_key not in inventory.rows else
                            m._parsed(inventory.rows[current_key].payload["data"], RotationCurrent))
        entries = tuple(CatalogEntry(kind=kind, key=key, sha256=_payload_hash(row.payload))
                        for kind, records in (("row", inventory.rows), ("receipt", inventory.receipts))
                        for key, row in sorted(records.items()))
        chunks = tuple(CatalogChunk(rotation_id=plan.rotation_id, index=index // 96,
                                    entries=entries[index:index + 96]) for index in range(0, len(entries), 96))
        catalog_rows = tuple((rotation_key("catalog", plan.rotation_id + "-" + str(chunk.index)), chunk) for chunk in chunks)
        preimage_writes, preimage_refs = _shared_preimage_writes(inventory, self.old, plan)
        if len(catalog_rows) + len(preimage_writes) + len(plan.images) + 5 > self.store._scope.max_operations:
            raise Problem(413, "BEHAVIOR_BATCH_TOO_LARGE", "whole rotation exceeds one bounded EGT")
        history = RotationHistory(
            schema_version="fa-runtime-rotation-history-v3",
            plan=plan, authorization=approval, previous_configuration=self.old.model_dump(mode="json"),
            before_guard={**inventory.guard.payload, "etag": inventory.guard.etag}, request=request,
            applied_at=applied_at,
            catalog=tuple(CatalogReference(row_id=key, sha256=digest(chunk.model_dump(mode="json")))
                          for key, chunk in catalog_rows),
            previous_current=previous_current,
            shared_preimage_chunks=preimage_refs,
        )
        pointer = RotationCurrent(configuration_sha256=self.new.sha256, history_key=hkey,
                                  history_sha256=digest(history.model_dump(mode="json")), authorization_key=akey)
        writes = [ControlWrite(row_id=image.row_id, mode="create" if image.before is None else "replace",
                               data=image.after["data"]) for image in plan.images]
        writes.extend(ControlWrite(
            row_id=key, mode="replace" if key == current_key and previous_current is not None else "create",
            data=value.model_dump(mode="json"))
            for key, value in ((hkey, history), (akey, approval), (current_key, pointer), *catalog_rows))
        writes.extend(preimage_writes)
        for write in writes:
            require(write.mode != "create" or write.row_id not in inventory.rows, "cutover immutable row already exists")
        return ControlChanges(writes=tuple(writes), result={
            "rotation_id": plan.rotation_id, "new_configuration_sha256": self.new.sha256,
            "history_key": hkey, "history_sha256": pointer.history_sha256,
            "counter_sha256": plan.counter_sha256, "activation_performed": False,
            "liability": plan.liability.model_dump(mode="json"),
            "subject_lineage": plan.subject_lineage.model_dump(mode="json"),
        })

    def apply(self, principal, plan, approval):
        if not self.allow_apply:
            raise Problem(403, "ROTATION_APPLY_DISABLED", "operator deployment is review-only")
        self._authorized(principal, plan, approval)
        require(self.plan(principal, rotation_id=plan.rotation_id) == plan, "approved inventory changed")
        inventory = self.store.read_inventory()
        require(digest(dict(inventory.entity_sha256)) == plan.inventory_sha256, "approved inventory changed")
        request = self._request(plan, approval)
        changes = self._layout(plan, approval, inventory, utc(self.clock()))
        _measure_layout(self.store, inventory, self.old, self.new, request, changes)

        def decide(rows):
            self._authorized(principal, plan, approval)
            for image in plan.images:
                row = rows[image.row_id]
                require((row is None) == (image.before is None) and (row is None or (
                    row.payload == image.before and row.etag == image.before_etag)), "exact rotation preimage changed")
            return changes

        return self.store.transact(request, decide, expected_guard=inventory.guard,
                                   before_send=lambda: self._authorized(principal, plan, approval),
                                   install_configuration_sha256=self.new.sha256)

    def reconcile(self, principal, plan, approval):
        self._authorized(principal, plan, approval, historical=True)
        # Reconcile the original request even when its outcome is still unknown.
        receipt = self.store.reconcile(self._request(plan, approval))
        key = rotation_key("history", plan.rotation_id)
        row = self.store.read_snapshot((key,))[key]
        require(row is not None, "receipt has no original rotation history")
        history = m._parsed(row.payload["data"], RotationHistory)
        require(history.plan == plan and history.authorization == approval, "original rotation decision differs")
        return receipt

    def verify(self, principal, plan, approval):
        receipt = self.reconcile(principal, plan, approval)
        inventory = self.store.read_inventory()
        writes = dict(receipt.payload["writes"])
        require(all(inventory.entity_sha256.get("behavior:v1:row:" + key) == value for key, value in writes.items()),
                "rotation postimages changed; immediate cutover verification is no longer possible")
        excluded = {GUARD_KEY, "behavior:v1:receipt:" + receipt.payload["request"]["request_id"],
                    *("behavior:v1:row:" + key for key in writes)}
        require(digest({key: value for key, value in inventory.entity_sha256.items() if key not in excluded})
                == plan.untouched_sha256 and _counter_hash(inventory.rows) == plan.counter_sha256,
                "retained data/receipts/counters changed during cutover")
        installed_rotation_origin(self.store, self.new)
        return {**receipt.payload["result"], "preserved_sha256": plan.untouched_sha256}


def installed_rotation_origin(store, config, *, _depth=0):
    """Read-only lineage replay; late current-runtime history is not old authority."""
    current_key = rotation_key("current", config.table.scope.authority_domain)
    require(_depth < 16, "rotation lineage exceeds its bounded replay depth")
    inventory = store.read_inventory()
    receipt_catalog(inventory)
    pointer = m._parsed(m._row(inventory, current_key).payload["data"], RotationCurrent)
    history = m._parsed(m._row(inventory, pointer.history_key).payload["data"], RotationHistory)
    plan, approval = history.plan, history.authorization
    previous = ServerConfiguration.model_validate_json(canonical(history.previous_configuration))
    _configuration_change(previous, config)
    require(pointer.configuration_sha256 == config.sha256 == inventory.installation_configuration_sha256
            and m._row(inventory, m.installation_record(config)[0]).payload["data"] == m.installation_record(config)[1]
            and pointer.history_sha256 == digest(history.model_dump(mode="json"))
            and plan.old_configuration_sha256 == previous.sha256 and plan.new_configuration_sha256 == config.sha256
            and plan.authority_domain == config.table.scope.authority_domain and plan.control_binding == store._binding
            and pointer.history_key == rotation_key("history", plan.rotation_id)
            and pointer.authorization_key == rotation_key("authorization", approval.decision_id)
            and m._row(inventory, pointer.authorization_key).payload["data"] == approval.model_dump(mode="json")
            and approval.plan_sha256 == plan.sha256 and approval.operator_oid == config.identity.human_oid
            and approval.action == _rotation_action(plan)
            and approval.not_before <= history.applied_at < min(approval.expires_at, plan.valid_until)
            and approval.expires_at <= approval.not_before + timedelta(minutes=15)
            and history.request == RuntimeRotation._request(plan, approval),
            "rotation history is not an exact approved installation")
    receipt = store.reconcile(history.request)
    expected = {image.row_id: _payload_hash(image.after) for image in plan.images}
    expected.update({key: _payload_hash(inventory.rows[key].payload)
                     for key in (current_key, pointer.history_key, pointer.authorization_key)})
    retained_rows, retained_receipts = {}, {}
    for index, reference in enumerate(history.catalog):
        row = m._row(inventory, reference.row_id)
        chunk = m._parsed(row.payload["data"], CatalogChunk)
        require(reference.row_id == rotation_key("catalog", plan.rotation_id + "-" + str(index))
                and chunk.index == index and chunk.rotation_id == plan.rotation_id
                and reference.sha256 == digest(chunk.model_dump(mode="json")), "catalog chunk identity/hash differs")
        expected[reference.row_id] = _payload_hash(row.payload)
        for entry in chunk.entries:
            values = retained_rows if entry.kind == "row" else retained_receipts
            require(entry.key not in values, "duplicate retained catalog entry")
            values[entry.key] = entry.sha256
    preimages = _read_shared_preimages(inventory, history)
    expected.update({reference.row_id: _payload_hash(m._row(inventory, reference.row_id).payload)
                     for reference in history.shared_preimage_chunks})
    require(dict(receipt.payload["writes"]) == expected
            and receipt.payload["guard_revision"] == plan.guard.revision + 1,
            "single rotation EGT receipt differs")
    require(len(retained_rows) + len(retained_receipts) <= m.DEFAULT_INVENTORY_MAX_ENTITIES,
            "retained catalog exceeds its bound")
    if previous.bindings[0].subject != config.bindings[0].subject:
        require(all(m.control_key("memory-subject", scope.key, config.bindings[0].subject) not in retained_rows
                    for scope in binding_scopes(config, config.bindings[0])),
                "rotation history reused an existing successor subject")
    require((current_key in retained_rows) == (history.previous_current is not None)
            == (plan.schema_version == "fa-runtime-rotation-plan-v2"),
            "previous rotation pointer preimage is missing or unexpected")
    changed = {image.row_id: image for image in plan.images}
    shared_preimages = {image.row_id: image for image in preimages}
    expected_shared = set(retained_rows) & shared_keys(previous) - set(changed)
    require((not preimages if history.schema_version == "fa-runtime-rotation-history-v1" else
             set(shared_preimages) == expected_shared)
            and len(shared_preimages) == len(preimages),
            "versioned shared cutoff preimages are incomplete or duplicated")
    historical_rows = {}
    mutable = shared_keys(config)
    for key, fingerprint in retained_rows.items():
        row = m._row(inventory, key)
        if key == current_key:
            from .behavior_store import _Row
            row = Record(_Row(binding=store._binding, row_id=key,
                              data=history.previous_current.model_dump(mode="json")).model_dump(mode="json"), row.etag)
        if key not in mutable:
            require(_payload_hash(row.payload) == fingerprint, "retired immutable record was rewritten")
        image = changed.get(key)
        captured = shared_preimages.get(key)
        if captured is not None:
            require(captured.before is not None and captured.before == captured.after
                    and _payload_hash(captured.before) == fingerprint,
                    "captured shared preimage differs from its approved catalog")
        if image is not None:
            require(image.before is not None and _payload_hash(image.before) == fingerprint,
                    "rotation preimage differs from the approved retained catalog")
        historical_rows[key] = (Record(image.before, image.before_etag) if image is not None else
                                Record(captured.before, captured.before_etag) if captured is not None else
                                historical_shared_row(inventory, config, key, fingerprint, plan.guard.revision)
                                if key in mutable else row)
    require(all(image.row_id in retained_rows if image.before is not None else image.row_id not in retained_rows
                for image in plan.images), "rotation create/replace preimage classification differs")
    historical_receipts = {}
    for key, fingerprint in retained_receipts.items():
        row = inventory.receipts.get(key)
        require(row is not None and _payload_hash(row.payload) == fingerprint, "old receipt disappeared or changed")
        historical_receipts[key] = row
    guard = dict(history.before_guard)
    etag = guard.pop("etag")
    require(guard == {"schema_version": 1, "kind": "guard", "binding": plan.guard.binding,
                      "revision": plan.guard.revision} and etag == plan.guard.etag,
            "original rotation guard anchor differs")
    hashes = {**{"behavior:v1:row:" + k: v for k, v in retained_rows.items()},
              **{"behavior:v1:receipt:" + k: v for k, v in retained_receipts.items()},
              GUARD_KEY: _payload_hash(m._MigratedGuard(binding=plan.guard.binding, revision=plan.guard.revision,
                                                       configuration_sha256=previous.sha256).model_dump(mode="json"))}
    require(digest(hashes) == plan.inventory_sha256, "retained catalog is not the approved whole inventory")
    historical = m.AuthorityInventory(inventory.scope, Record(guard, etag), historical_rows,
                                      historical_receipts, hashes, previous.sha256)
    grants = tuple(m._parsed(image.after["data"], NativeExecutionGrant) for image in plan.images
                   if image.after["data"].get("kind") == "native_execution_grant")
    require(plan.grants_sha256 == digest([grant.model_dump(mode="json") for grant in grants])
            and all(grant.active for grant in grants), "approved native grants differ")
    replay = SimpleNamespace(store=store, old=previous, new=config, grants=grants,
                             clock=lambda: history.applied_at, _parse=RuntimeTransition._parse)
    require(RuntimeTransition._changes(replay, historical) == plan.images,
            "rotation images are not the exact allowed configuration-only changes")
    from .behavior_runtime_transition import RuntimeTransitionHistory, transition_key
    if history.previous_current is None:
        prior_key = transition_key("current", previous.table.scope.authority_domain)
        prior_pointer = historical_rows[prior_key].payload["data"]
        prior = m._parsed(historical_rows[prior_pointer["history_key"]].payload["data"], RuntimeTransitionHistory)
    else:
        prior = m._parsed(historical_rows[history.previous_current.history_key].payload["data"], RotationHistory)
    require(digest(prior.model_dump(mode="json")) == plan.previous_history_sha256
            and plan.subject_lineage == _subject_lineage(previous, config)
            and plan.successor_binding_id == config.bindings[0].binding_id
            and plan.liability.configuration_sha256 == previous.sha256
            and plan.liability.job_id == previous.bindings[0].job_id
            and plan.liability.work_id == previous.bindings[0].work.work_id
            and plan.liability.root_budget_id == previous.bindings[0].owner.root_budget_id,
            "rotation liability/successor is not linked to the exact predecessor")
    # The predecessor verifier checks only exact installation/protection images;
    # it never re-applies its transition or treats old grants as current.
    historical_store = _snapshot_store(store, historical, previous)
    original = (m.installed_origin(historical_store, previous) if history.previous_current is None else
                installed_rotation_origin(historical_store, previous, _depth=_depth + 1))
    validate_additions(inventory, config, previous, retained_rows, {
        current_key, pointer.history_key, pointer.authorization_key, *(ref.row_id for ref in history.catalog),
        *(ref.row_id for ref in history.shared_preimage_chunks),
    })
    protected = set(retained_rows) - mutable
    protected.update(reference.row_id for reference in history.catalog)
    protected.update(reference.row_id for reference in history.shared_preimage_chunks)
    protected.update((current_key, pointer.history_key, pointer.authorization_key, *m.protection_keys(config)))
    store.bind_installation(m.installation_record(config)[0], config.sha256, protected_rows=protected)
    store._verified_migration_epoch = config.sha256
    return original
