from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from .auth import Actor
from .errors import Problem
from .m3_auth import ApprovalSettings, HumanApprover
from .m3_models import (
    MAX_ARTIFACT_BYTES, MAX_ARTIFACT_VERSIONS, MAX_SKILL_RESOURCE_BYTES, MAX_SKILL_REVIEW_ENTRIES, OWNER_AUTHOR,
    PNG_SIGNATURE, ApprovalInput,
    ApprovalRecord, ArtifactDocument, ArtifactSource, ArtifactVersion, BlobReceipt, CreateArtifact, ETag, PendingVersion,
    NativeArtifactSource, OwnerArtifactSource,
    SkillDeactivationInput, SkillDeactivationRecord, SkillResourceRead, SkillResourceReference, SkillReviewEntry,
    SkillReviewList, VersionInput, policy_text_is_visible, skill_metadata_sha256, skill_resource_text, validate_id,
)
from .service import ContextService, now
from .storage import Record

if TYPE_CHECKING:
    from .m3_store import ArtifactStore

Principal = Actor | HumanApprover
_etag = TypeAdapter(ETag)
MAX_POLICY_BYTES = 8192


class ArtifactService:
    """Immutable file history and explicit policy activation, separate from legacy published work."""

    def __init__(self, context: ContextService, store: ArtifactStore, approval: ApprovalSettings):
        self.context, self.repository, self.store, self.approval = context, context.repository, store, approval
        if approval.approver_oid in context.auth.actors:
            raise ValueError("M3 approver cannot also be an agent identity")

    def authorize(self, principal: Principal, team: str) -> None:
        validate_id(team)
        if team != self.approval.team:
            raise Problem(403, "M3_TEAM_DENIED", "M3 is restricted to its configured POC team")
        if isinstance(principal, Actor):
            self.context.authorize(principal, team)
        elif not isinstance(principal, HumanApprover) or principal != HumanApprover(self.approval.approver_oid, team):
            raise Problem(403, "M3_IDENTITY_DENIED", "a verified agent or configured human is required")

    def _partition(self, team: str) -> str:
        return "m3-" + self.context.auth.tenant_id + "-" + team

    def _key(self, team: str, artifact: str) -> tuple[str, str]:
        return self._partition(team), "artifact:" + validate_id(artifact)

    def _load(self, principal: Principal, team: str, artifact: str) -> tuple[Record, ArtifactDocument]:
        self.authorize(principal, team)
        try:
            record = self.repository.get(*self._key(team, artifact))
            document = ArtifactDocument.model_validate(record.payload)
            _etag.validate_python(record.etag, strict=True)
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            raise Problem(502, "M3_STATE_INVALID", "artifact state could not be verified") from exc
        if document.team != team or document.artifact_id != artifact:
            raise Problem(502, "M3_STATE_INVALID", "artifact state does not match its requested identity")
        return record, document

    def get(self, principal: Principal, team: str, artifact: str) -> Record:
        return self._load(principal, team, artifact)[0]

    def skill_reviews(self, principal: Principal, team: str, after: str | None = None) -> SkillReviewList:
        """One page of current team skill review states; metadata only, no bodies, resources or writes."""
        self.authorize(principal, team)
        cursor = None if after is None else self._key(team, after)[1]
        # Deliverables share the artifact row range; they only advance the cursor.
        rows, more = self.repository.list_prefix(
            self._partition(team), "artifact:", MAX_SKILL_REVIEW_ENTRIES, after=cursor,
        )
        entries, last = [], None
        for row, record in rows:
            try:
                document = ArtifactDocument.model_validate(record.payload)
                _etag.validate_python(record.etag, strict=True)
                if row != self._key(team, document.artifact_id)[1] or document.team != team:
                    raise ValueError("artifact row does not match its stored identity")
                if document.kind == "skill":
                    entries.append(SkillReviewEntry.from_document(document))
            except (ValidationError, ValueError, KeyError, TypeError, Problem) as exc:
                raise Problem(502, "M3_STATE_INVALID", "artifact state could not be verified") from exc
            last = document.artifact_id
        return SkillReviewList(team=team, after=after, entries=tuple(entries), next_after=last if more else None)

    def _confirmed(self, result: Record, expected: dict) -> Record:
        try:
            _etag.validate_python(result.etag, strict=True)
        except (ValidationError, AttributeError) as exc:
            raise Problem(502, "M3_STATE_UNCERTAIN", "artifact write acknowledgement is uncertain; read before continuing") from exc
        if result.payload != expected:
            raise Problem(502, "M3_STATE_UNCERTAIN", "artifact write acknowledgement differs; do not replay")
        return result

    def _save(self, record: Record, document: ArtifactDocument) -> Record:
        payload = ArtifactDocument.model_validate({
            **document.model_dump(), "revision": document.revision + 1,
        }).model_dump()
        saved = self.repository.replace(*self._key(document.team, document.artifact_id), payload, record.etag)
        return self._confirmed(saved, payload)

    @staticmethod
    def _match(record: Record, etag: str | None) -> None:
        if etag is None:
            raise Problem(428, "ETAG_REQUIRED", "If-Match is required")
        try:
            _etag.validate_python(etag, strict=True)
        except ValidationError as exc:
            raise Problem(412, "ETAG_CONFLICT", "supply one exact, non-wildcard artifact ETag") from exc
        if etag != record.etag:
            raise Problem(412, "ETAG_CONFLICT", "artifact changed; read and reconcile before continuing")

    @staticmethod
    def _owner(principal: Principal, document: ArtifactDocument) -> Principal:
        if isinstance(document.native_source, OwnerArtifactSource):
            # Text the Team owner typed in: only that same configured human continues its own draft.
            if not isinstance(principal, HumanApprover) or principal.oid != document.owner_oid:
                raise Problem(403, "M3_NOT_OWNER", "only the Team owner who wrote this draft can continue it")
            return principal
        if not isinstance(principal, Actor) or (principal.oid, principal.actor) != (document.owner_oid, document.owner):
            raise Problem(403, "M3_NOT_OWNER", "only the artifact's owning agent can create or reconcile versions")
        return principal

    @staticmethod
    def _available(document: ArtifactDocument) -> None:
        if document.pending is not None:
            raise Problem(409, "M3_UPLOAD_UNKNOWN", "an upload is pending or unknown; reconcile it without resending bytes")

    def create(self, principal: Principal, team: str, artifact: str, body: CreateArtifact) -> Record:
        self.authorize(principal, team)
        if not isinstance(principal, Actor):
            raise Problem(403, "M3_AGENT_REQUIRED", "only an agent can submit a new artifact")
        source = self.context.get(principal, team, body.source_work)
        if source.payload["owner"] != principal.actor:
            raise Problem(403, "NOT_OWNER", "only the source work owner can create this artifact")
        document = ArtifactDocument(
            **body.model_dump(), artifact_id=validate_id(artifact), team=team,
            owner=principal.actor, owner_oid=principal.oid, revision=1,
        )
        payload = document.model_dump()
        return self._confirmed(self.repository.create(*self._key(team, artifact), payload), payload)

    def _native_owner(self, principal, team, source):
        self.authorize(principal, team)
        if isinstance(source, OwnerArtifactSource):
            # authorize() already pinned a HumanApprover to the configured owner and Team.
            if (not isinstance(principal, HumanApprover)
                    or (source.tenant_id, source.team_id, source.owner_oid) != (
                        self.context.auth.tenant_id, team, principal.oid)):
                raise Problem(403, "M3_NATIVE_SOURCE_DENIED", "the server-resolved owner source belongs to another owner")
            return OwnerArtifactSource.model_validate_json(source.model_dump_json())
        if (not isinstance(principal, Actor) or not isinstance(source, NativeArtifactSource)
                or (source.tenant_id, source.team_id, source.agent_oid) != (
                    self.context.auth.tenant_id, team, principal.oid)):
            raise Problem(403, "M3_NATIVE_SOURCE_DENIED", "the server-resolved native source belongs to another owner")
        return NativeArtifactSource.model_validate_json(source.model_dump_json())

    def create_native_skill(self, principal: Principal, team: str, artifact: str, *,
                            title: str, native_source: ArtifactSource) -> Record:
        """Server-only intake after the gateway verified the actual author (Agent turn or Team owner)."""
        return self._create_native_artifact(principal, team, artifact, "skill", title, native_source)

    def create_native_resource(self, principal: Principal, team: str, artifact: str, *,
                               title: str, native_source: ArtifactSource) -> Record:
        return self._create_native_artifact(principal, team, artifact, "deliverable", title, native_source)

    def _create_native_artifact(self, principal, team, artifact, kind, title, native_source):
        source = self._native_owner(principal, team, native_source)
        document = ArtifactDocument(
            kind=kind, title=title, artifact_id=validate_id(artifact), team=team,
            owner=OWNER_AUTHOR if isinstance(source, OwnerArtifactSource) else principal.actor,
            owner_oid=principal.oid, revision=1, native_source=source)
        payload = document.model_dump()
        return self._confirmed(self.repository.create(*self._key(team, artifact), payload), payload)

    @staticmethod
    def _content(document: ArtifactDocument, metadata: VersionInput, data: bytes) -> None:
        if metadata.skill_purpose is not None and document.kind != "skill":
            raise Problem(422, "M3_POLICY_FORMAT", "only skill versions may carry a versioned purpose")
        if not isinstance(data, bytes) or not data:
            raise Problem(422, "M3_EMPTY_FILE", "a nonempty binary file is required")
        if len(data) > MAX_ARTIFACT_BYTES:
            raise Problem(413, "M3_FILE_TOO_LARGE", "artifact exceeds 512 KiB")
        if hashlib.sha256(data).hexdigest() != metadata.sha256:
            raise Problem(422, "M3_HASH_MISMATCH", "file bytes do not match the submitted SHA-256")
        if metadata.media_type == "image/png" and (
                document.kind != "deliverable" or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR"):
            raise Problem(422, "M3_IMAGE_FORMAT", "only PNG deliverables may use image/png")
        if document.kind in ("sop", "skill"):
            if metadata.media_type not in ("text/plain", "text/markdown") or len(data) > MAX_POLICY_BYTES:
                raise Problem(422, "M3_POLICY_FORMAT", "this POC accepts policy text up to 8 KiB")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise Problem(422, "M3_POLICY_FORMAT", "policy content must be UTF-8") from exc
            if not text.strip() or not policy_text_is_visible(text, multiline=True):
                raise Problem(422, "M3_POLICY_FORMAT", "policy text must not contain hidden control characters")

    def _verify_blob(self, document: ArtifactDocument, version_id: str, receipt: BlobReceipt,
                     sha256: str, size: int) -> bytes:
        if receipt.sha256 != sha256 or receipt.bytes != size:
            raise Problem(502, "M3_FILE_INTEGRITY", "stored file receipt differs from the reserved content")
        data = self.store.read(document.team, document.artifact_id, version_id, receipt)
        if not isinstance(data, bytes) or len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise Problem(502, "M3_FILE_INTEGRITY", "stored bytes do not match the immutable receipt")
        return data

    def _resource_version(self, principal: Principal, team: str, source_work: str | None,
                          reference: SkillResourceReference, *,
                          native_source: ArtifactSource | None = None) -> ArtifactVersion:
        _, document = self._load(principal, team, reference.artifact_id)
        version = next((item for item in document.versions if item.version_id == reference.version_id), None)
        source_matches = version is not None and (
            source_work is not None and version.source_work == source_work and version.native_source is None
            if native_source is None else
            source_work is None and document.native_source == native_source and version.native_source == native_source)
        if (document.kind != "deliverable" or version is None or not source_matches
                or version.sha256 != reference.sha256
                or version.media_type not in {"text/plain", "text/markdown", "application/json"}
                or version.blob.bytes > MAX_SKILL_RESOURCE_BYTES):
            raise Problem(403, "M3_SKILL_RESOURCE_DENIED",
                          "skill resources must name an exact small text deliverable from the same original source")
        return version

    def _validate_resources(self, principal: Principal, team: str, metadata: VersionInput,
                            *, verify_content: bool = False, native_source: ArtifactSource | None = None) -> None:
        if metadata.skill_tools is not None:
            from .behavior_capabilities import BUILTIN_CAPABILITIES

            for requirement in metadata.skill_tools.entries:
                entry = BUILTIN_CAPABILITIES[requirement.entry_id]
                if requirement.entry_sha256 != entry.entry_sha256 or requirement.tool not in entry.tools:
                    raise Problem(422, "M3_SKILL_TOOL_DENIED", "the requirement must name the fixed existing tool")
        if metadata.skill_resources is not None:
            if (metadata.source_work is None) == (native_source is None):
                raise Problem(422, "M3_SKILL_RESOURCE_DENIED", "resource metadata requires one original source")
            for reference in metadata.skill_resources.entries:
                resource = self._resource_version(principal, team, metadata.source_work, reference,
                                                  native_source=native_source)
                if verify_content:
                    verified, raw = self.download(principal, team, reference.artifact_id, reference.version_id, resource.blob.etag)
                    if (verified != resource
                            or self._resource_version(principal, team, metadata.source_work, reference,
                                                      native_source=native_source) != resource):
                        raise Problem(409, "M3_SKILL_RESOURCE_CHANGED", "resource changed while validating manifest approval")
                    try:
                        skill_resource_text(raw, reference.sha256)
                    except (UnicodeError, ValueError):
                        raise Problem(422, "M3_SKILL_RESOURCE_FORMAT", "resource text is not valid complete UTF-8 data") from None

    def _finish_upload(self, record: Record, document: ArtifactDocument, receipt: BlobReceipt) -> Record:
        pending = document.pending
        if pending is None:
            raise Problem(409, "M3_UPLOAD_NOT_PENDING", "there is no reserved upload to finalize")
        self._verify_blob(document, pending.version_id, receipt, pending.sha256, pending.bytes)
        fields = {**pending.model_dump(mode="json", exclude={"bytes", "status"}),
                  "blob": receipt.model_dump(mode="json"), "number": len(document.versions) + 1}
        version = ArtifactVersion(**fields, metadata_sha256=skill_metadata_sha256(
            document.team, document.artifact_id, fields))
        document.versions.append(version)
        document.pending = None
        return self._save(record, document)

    def upload(self, principal: Principal, team: str, artifact: str, version_id: str,
               etag: str | None, metadata: VersionInput, data: bytes, *,
               native_source: ArtifactSource | None = None) -> Record:
        validate_id(version_id, version=True)
        record, document = self._load(principal, team, artifact)
        actor = self._owner(principal, document)
        if (document.native_source is None) != (native_source is None):
            raise Problem(403, "M3_NATIVE_SOURCE_DENIED", "a native version requires its verified server-side source")
        if native_source is not None:
            native_source = self._native_owner(principal, team, native_source)
            if metadata.source_work is not None or native_source.author != document.native_source.author:
                raise Problem(403, "M3_NATIVE_SOURCE_DENIED", "native and legacy provenance cannot be mixed")
        source_work = metadata.source_work or document.source_work
        metadata = VersionInput.model_validate({**metadata.model_dump(), "source_work": source_work})
        self._content(document, metadata, data)
        existing = next((item for item in document.versions if item.version_id == version_id), None)
        if existing is not None:
            if (existing.filename, existing.media_type, existing.sha256, existing.blob.bytes,
                    existing.source_work, existing.native_source, existing.skill_purpose, existing.skill_resources,
                    existing.skill_tools) != (
                metadata.filename, metadata.media_type, metadata.sha256, len(data), source_work,
                native_source, metadata.skill_purpose, metadata.skill_resources, metadata.skill_tools,
            ):
                raise Problem(409, "M3_VERSION_CONFLICT", "a version ID cannot be reused for different content")
            return record
        self._match(record, etag)
        self._available(document)
        if len(document.versions) >= MAX_ARTIFACT_VERSIONS:
            raise Problem(429, "M3_VERSION_LIMIT", "this POC retains at most 12 versions per artifact")
        source_revision = None
        if native_source is None:
            source = self.context.get(actor, team, source_work)
            if source.payload["owner"] != actor.actor:
                raise Problem(403, "NOT_OWNER", "only the source work owner can submit its artifact version")
            source_revision = source.payload["revision"]
        self._validate_resources(principal, team, metadata, native_source=native_source)
        document.pending = PendingVersion(
            **metadata.model_dump(), version_id=version_id, bytes=len(data), created_by_oid=actor.oid,
            source_work_revision=source_revision, native_source=native_source, created_at=now(),
        )
        reserved = self._save(record, document)
        document = ArtifactDocument.model_validate(reserved.payload)
        receipt = self.store.put(team, artifact, version_id, data, metadata.sha256)
        return self._finish_upload(reserved, document, receipt)

    def reconcile(self, principal: Principal, team: str, artifact: str, version_id: str, etag: str | None) -> Record:
        validate_id(version_id, version=True)
        record, document = self._load(principal, team, artifact)
        self._owner(principal, document)
        self._match(record, etag)
        if document.pending is None or document.pending.version_id != version_id:
            raise Problem(409, "M3_UPLOAD_NOT_PENDING", "the requested upload is not the pending operation")
        receipt = self.store.stat(team, artifact, version_id)
        return self._finish_upload(record, document, receipt)

    def version(self, principal: Principal, team: str, artifact: str, version_id: str) -> ArtifactVersion:
        validate_id(version_id, version=True)
        _, document = self._load(principal, team, artifact)
        value = next((item for item in document.versions if item.version_id == version_id), None)
        if value is None:
            raise Problem(404, "M3_VERSION_NOT_FOUND", "no completed version has that ID")
        return value

    def download(self, principal: Principal, team: str, artifact: str, version_id: str,
                 etag: str | None) -> tuple[ArtifactVersion, bytes]:
        value = self.version(principal, team, artifact, version_id)
        self._match(Record({}, value.blob.etag), etag)
        data = self.store.read(team, artifact, version_id, value.blob)
        if not isinstance(data, bytes) or len(data) != value.blob.bytes or hashlib.sha256(data).hexdigest() != value.sha256:
            raise Problem(502, "M3_FILE_INTEGRITY", "downloaded bytes differ from their immutable version")
        return value, data

    def decide(self, principal: Principal, team: str, artifact: str, etag: str | None, body: ApprovalInput) -> Record:
        record, document = self._load(principal, team, artifact)
        if not isinstance(principal, HumanApprover):
            raise Problem(403, "M3_HUMAN_REQUIRED", "agent publication is not human approval")
        if document.kind == "deliverable":
            raise Problem(409, "M3_NOT_POLICY", "ordinary deliverables do not require policy activation")
        if any(item.decision_id == body.decision_id for item in document.deactivations):
            raise Problem(409, "M3_DECISION_CONFLICT", "a decision ID cannot be reused for another decision")
        prior = next((item.approval for item in document.versions
                      if item.approval and item.approval.decision_id == body.decision_id), None)
        if prior:
            if prior.model_dump(include=set(ApprovalInput.model_fields)) != body.model_dump():
                raise Problem(409, "M3_DECISION_CONFLICT", "a decision ID cannot be reused for another decision")
            return record
        self._match(record, etag)
        self._available(document)
        if not document.versions or document.versions[-1].version_id != body.version_id:
            raise Problem(409, "M3_STALE_PROPOSAL", "only the latest proposed version can receive a new decision")
        version = document.versions[-1]
        if version.sha256 != body.sha256:
            raise Problem(409, "M3_APPROVAL_HASH_MISMATCH", "approval must pin the exact version SHA-256")
        if version.metadata_sha256 != body.metadata_sha256:
            raise Problem(409, "M3_APPROVAL_METADATA_MISMATCH", "approval must pin the complete skill version metadata")
        if version.approval is not None:
            raise Problem(409, "M3_ALREADY_DECIDED", "a decided version cannot be changed; submit a new version")
        self._verify_blob(document, version.version_id, version.blob, version.sha256, version.blob.bytes)
        if body.decision == "approve":
            self._validate_resources(principal, team, version, verify_content=True, native_source=version.native_source)
        version.approval = ApprovalRecord(**body.model_dump(), approver_oid=principal.oid, decided_at=now())
        if body.decision == "approve":
            document.effective_version_id = version.version_id
        return self._save(record, document)

    def deactivate_skill(self, principal: Principal, team: str, artifact: str,
                         etag: str | None, body: SkillDeactivationInput) -> Record:
        record, document = self._load(principal, team, artifact)
        if not isinstance(principal, HumanApprover):
            raise Problem(403, "M3_HUMAN_REQUIRED", "skill deactivation requires the configured human")
        if document.kind != "skill":
            raise Problem(409, "M3_NOT_SKILL", "only an approved skill can be deactivated")
        prior = next((item for item in document.deactivations if item.decision_id == body.decision_id), None)
        if prior is not None:
            if prior.model_dump(include=set(SkillDeactivationInput.model_fields)) != body.model_dump():
                raise Problem(409, "M3_DECISION_CONFLICT", "a decision ID cannot be reused for another decision")
            return record
        if any(item.approval and item.approval.decision_id == body.decision_id for item in document.versions):
            raise Problem(409, "M3_DECISION_CONFLICT", "a decision ID cannot be reused for another decision")
        self._match(record, etag)
        if document.effective_version_id != body.version_id:
            raise Problem(409, "M3_EFFECTIVE_VERSION_CHANGED", "deactivation must name the current effective version")
        version = next(item for item in document.versions if item.version_id == body.version_id)
        if version.sha256 != body.sha256:
            raise Problem(409, "M3_DEACTIVATION_HASH_MISMATCH", "deactivation must pin the exact version SHA-256")
        if version.metadata_sha256 != body.metadata_sha256:
            raise Problem(409, "M3_DEACTIVATION_METADATA_MISMATCH", "deactivation must pin the exact version metadata")
        document.deactivations.append(SkillDeactivationRecord(
            **body.model_dump(), deactivated_by_oid=principal.oid, deactivated_at=now(),
            applied_revision=document.revision + 1,
        ))
        document.effective_version_id = None
        return self._save(record, document)

    def effective(self, principal: Principal, team: str, artifact: str) -> ArtifactVersion:
        _, document = self._load(principal, team, artifact)
        if document.kind == "deliverable":
            raise Problem(409, "M3_NOT_POLICY", "ordinary deliverables are not active behavior policies")
        if document.effective_version_id is None:
            raise Problem(404, "M3_NO_EFFECTIVE_VERSION", "no approved version is currently effective")
        return next(item for item in document.versions if item.version_id == document.effective_version_id)

    def policy_context(self, principal: Principal, team: str, artifact_ids: tuple[str, ...]) -> list[dict]:
        self.authorize(principal, team)
        policies = []
        for artifact_id in artifact_ids:
            _, document = self._load(principal, team, artifact_id)
            if document.kind not in ("sop", "skill"):
                raise Problem(409, "M3_NOT_POLICY", "configured policy artifact is an ordinary deliverable")
            if document.effective_version_id is None:
                policies.append({"artifact_id": artifact_id,
                                 "status": "deactivated" if document.deactivations else "not_approved"})
                continue
            version = next(item for item in document.versions if item.version_id == document.effective_version_id)
            data = self._verify_blob(document, version.version_id, version.blob, version.sha256, version.blob.bytes)
            self._content(document, version, data)
            policies.append({
                "artifact_id": artifact_id, "status": "approved", "kind": document.kind,
                "version": version.model_dump(), "text": data.decode("utf-8"),
            })
        return policies

    def skill_resource(self, principal: Principal, team: str, artifact: str, version_id: str,
                       resource_name: str) -> SkillResourceRead:
        _, document = self._load(principal, team, artifact)
        if document.effective_version_id is None:
            raise Problem(404, "M3_NO_EFFECTIVE_VERSION", "no approved skill version is currently effective")
        version = next(item for item in document.versions if item.version_id == document.effective_version_id)
        if (document.kind != "skill" or version.version_id != version_id
                or version.skill_resources is None or version.metadata_sha256 is None
                or version.approval is None or version.approval.approver_oid != self.approval.approver_oid):
            raise Problem(403, "M3_SKILL_RESOURCE_DENIED", "the current approved skill manifest is required")
        reference = next((item for item in version.skill_resources.entries if item.name == resource_name), None)
        if reference is None:
            raise Problem(403, "M3_SKILL_RESOURCE_DENIED", "only the current approved manifest's resource names are readable")
        resource = self._resource_version(principal, team, version.source_work, reference, native_source=version.native_source)
        verified, raw = self.download(principal, team, reference.artifact_id, reference.version_id, resource.blob.etag)
        if verified != resource:
            raise Problem(409, "M3_SKILL_RESOURCE_CHANGED", "resource version changed during the read")
        try:
            result = SkillResourceRead(
                skill_artifact_id=artifact, skill_version_id=version_id,
                skill_metadata_sha256=version.metadata_sha256, resource=reference,
                media_type=resource.media_type, text=skill_resource_text(raw, reference.sha256),
            )
        except (UnicodeError, ValueError):
            raise Problem(422, "M3_SKILL_RESOURCE_FORMAT", "resource must be complete valid UTF-8 data without control characters") from None
        if (self.effective(principal, team, artifact) != version
                or self._resource_version(principal, team, version.source_work, reference,
                                          native_source=version.native_source) != resource):
            raise Problem(409, "M3_SKILL_RESOURCE_CHANGED", "skill or resource changed during the read")
        return result
