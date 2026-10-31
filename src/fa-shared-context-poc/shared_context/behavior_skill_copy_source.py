"""One copy intent in the original artifact-effect slot; M3 remains the file authority."""

import hashlib
import logging
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_operations import BudgetRecord, _Model, _parse, _row, _write, control_key
from .behavior_runtime import material
from .behavior_skill_copy import SOP_COPY_PROFILE, SopCopyOutcome, SopCopyReceipt, TeamSopCopy
from .behavior_store import ControlChanges
from .behavior_wire import Command, Digest, Identifier
from .errors import Problem
from .m3_models import ArtifactDocument, ArtifactVersion, CreateArtifact, VersionId, VersionInput, skill_metadata_sha256
from .source_materials import digest

TITLE = "Unapproved copy of assigned Team SOP"


class CopyIntent(_Model):
    schema_version: Literal["fa-sop-skill-copy-intent-v1"]
    binding_id: Identifier
    job_id: Identifier
    work_id: Identifier
    session_id: Identifier
    task_sha256: Digest
    execution_sha256: Digest
    registration_id: Identifier
    registration_sha256: Digest
    actor_oid: Identifier
    scope: TeamSopCopy
    source_version: ArtifactVersion
    content: Annotated[bytes, Field(min_length=1, max_length=8192)]
    source_work: Identifier
    source_work_revision: Annotated[int, Field(ge=1)]
    artifact_id: Identifier
    version_id: VersionId

    @model_validator(mode="after")
    def exact(self):
        if (hashlib.sha256(self.content).hexdigest() != self.scope.source_sha256
                or self.source_version.version_id != self.scope.source_version_id
                or self.source_version.sha256 != self.scope.source_sha256
                or self.source_version.blob.bytes != len(self.content)
                or self.source_version.approval is None or self.source_version.approval.decision != "approve"
                or digest(self.source_version.approval.model_dump(mode="json")) != self.scope.source_approval_sha256
                or self.artifact_id == self.scope.source_artifact_id):
            raise ValueError("Copy intent must retain its exact approved source and separate target")
        return self

    def metadata(self):
        return VersionInput(filename="copied-sop.md", media_type=self.source_version.media_type,
                            sha256=self.scope.source_sha256, source_work=self.source_work,
                            skill_purpose=self.scope.purpose)


class CopyEffect(_Model):
    kind: Literal["assistant_sop_skill_copy"] = "assistant_sop_skill_copy"
    schema_version: Literal["fa-sop-skill-copy-effect-v1"]
    intent: CopyIntent
    intent_sha256: Digest
    stage: Literal["create_unknown", "created", "upload_unknown", "saved"]
    receipt: SopCopyReceipt | None = None
    target_version: ArtifactVersion | None = None

    @model_validator(mode="after")
    def immutable(self):
        if self.intent_sha256 != digest(self.intent.model_dump(mode="json")):
            raise ValueError("Original copy material changed")
        if ((self.stage == "saved") != (self.receipt is not None)
                or (self.receipt is None) != (self.target_version is None)):
            raise ValueError("Only a verified saved copy has a public receipt")
        if self.receipt is not None:
            ref, intent, version = self.receipt, self.intent, self.target_version
            if (ref.job_id != intent.job_id or ref.artifact_id != intent.artifact_id
                    or ref.version_id != intent.version_id or ref.source_artifact_id != intent.scope.source_artifact_id
                    or ref.source_version_id != intent.scope.source_version_id
                    or ref.source_sha256 != intent.scope.source_sha256 or ref.byte_length != len(intent.content)
                    or ref.purpose != intent.scope.purpose
                    or version.version_id != intent.version_id or version.sha256 != intent.scope.source_sha256
                    or version.approval is not None or version.source_work != intent.source_work
                    or version.created_by_oid != intent.actor_oid or version.source_work_revision != intent.source_work_revision
                    or version.skill_purpose != intent.scope.purpose or version.blob.bytes != len(intent.content)
                    or ref.metadata_sha256 != version.metadata_sha256
                    or version.metadata_sha256 != skill_metadata_sha256(
                        intent.scope.team_id, intent.artifact_id, version.model_dump(mode="json"))):
                raise ValueError("Copy receipt differs from its immutable original intent")
        return self

    def outcome(self):
        return SopCopyOutcome(schema_version="fa-sop-copy-outcome-v1", receipt_id=effect_key(self.intent.work_id),
                              intent_sha256=self.intent_sha256, stage=self.stage)


def effect_key(work_id):
    from .behavior_assistant import key
    return key("artifact", work_id)


def _active_root(rows, session):
    if not _row(rows, control_key("budget", session.execution.owner.root_budget_id), BudgetRecord).active:
        raise Problem(403, "SOP_COPY_SCOPE_DENIED", "the original root must remain active")


def _scope(authority, session, principal, *, write=True):
    from .behavior_work_policy import work_policy_key
    binding, _, _ = authority.runtime.authorize(session.binding_id, principal, "read")
    if write:
        executable, _, _ = authority.runtime.authorize(session.binding_id, principal, "execute")
        if binding != executable:
            raise Problem(403, "SOP_COPY_SCOPE_DENIED", "current read and write authority must bind the same work")
    task, profile = session.assignment.task, authority.runtime.profiles[session.binding_id]
    control = authority.runtime.work_policy
    scope = None if task.instruction is None else task.instruction.sop_copy
    if (scope is None or task.instruction.prompt_profile != SOP_COPY_PROFILE
            or profile.prompt_profile != SOP_COPY_PROFILE or profile.sop_copy != scope
            or task.profile_digest != digest(profile.model_dump(mode="json"))
            or session.state != "running" or control is None):
        raise Problem(403, "SOP_COPY_DISABLED", "only the exact original server-authorized copy profile may write this draft")
    rows = authority.store.read_snapshot(authority._keys(session))
    if write:
        authority._gate(session, rows)
        _active_root(rows, session)
    installed = control.installed(rows)
    registered = control.records.get(binding.binding_id)
    if (registered is None or registered.binding != binding or registered.profile != profile
            or installed.profile.sop_copy != scope or scope.team_id != binding.team_id
            or scope.source_artifact_id == task.artifact_id
            or task.artifact_id != profile.artifact_id or task.artifact_version != profile.artifact_version
            or principal.kind != "agent" or principal.oid != binding.agent_oid):
        raise Problem(403, "SOP_COPY_SCOPE_DENIED", "original installation, Team, job and single draft slot must agree")
    actor = authority.actors.get(binding.agent_oid)
    if actor is None or actor.team != binding.team_id or actor.actor != principal.source_actor_id:
        raise Problem(403, "SOP_COPY_SCOPE_DENIED", "the actual verified server-bound owning Actor is required")
    authority.artifacts.authorize(actor, binding.team_id)
    revision = None
    if write:
        legacy = authority.artifacts.context.get(actor, binding.team_id, profile.legacy_source_work)
        if legacy.payload["owner"] != actor.actor:
            raise Problem(403, "SOP_COPY_SCOPE_DENIED", "the configured existing legacy source work must still be owned")
        revision = legacy.payload["revision"]
    registration_id = work_policy_key("registration", registered.admission_id)
    if rows[registration_id].payload["material"] != material(registered.model_dump(mode="json")).model_dump(mode="json"):
        raise Problem(403, "SOP_COPY_SCOPE_DENIED", "original immutable registration material differs")
    return binding, profile, scope, actor, revision, registration_id, digest(registered.model_dump(mode="json"))


def _approved(authority, actor, scope):
    try:
        document = ArtifactDocument.model_validate(
            authority.artifacts.get(actor, scope.team_id, scope.source_artifact_id).payload)
    except Problem as error:
        raise Problem(error.status, "SOP_COPY_SOURCE_DENIED", "the assigned approved Team source is unavailable") from None
    except ValueError:
        raise Problem(502, "SOP_COPY_SOURCE_DENIED", "the original source metadata is invalid") from None
    if document.kind != "sop" or document.effective_version_id != scope.source_version_id:
        raise Problem(403, "SOP_COPY_SOURCE_CHANGED", "only the exact currently effective SOP version is copyable")
    version = next(value for value in document.versions if value.version_id == scope.source_version_id)
    if (version.sha256 != scope.source_sha256 or version.approval is None
            or version.approval.decision != "approve"
            or version.approval.approver_oid != authority.artifacts.approval.approver_oid
            or digest(version.approval.model_dump(mode="json")) != scope.source_approval_sha256
            or version.media_type not in {"text/plain", "text/markdown"} or version.blob.bytes > 8192):
        raise Problem(403, "SOP_COPY_SOURCE_DENIED", "the exact human-approved bounded SOP and its approval are required")
    return version


def _source(authority, actor, scope):
    before = _approved(authority, actor, scope)
    try:
        policy = authority.artifacts.policy_context(actor, scope.team_id, (scope.source_artifact_id,))[0]
        version = ArtifactVersion.model_validate(policy["version"])
        content = policy["text"].encode("utf-8")
        if (policy["status"] != "approved" or policy["kind"] != "sop" or version != before
                or hashlib.sha256(content).hexdigest() != scope.source_sha256
                or len(content) != version.blob.bytes or _approved(authority, actor, scope) != before):
            raise ValueError("original SOP differs")
    except (Problem, ValueError, KeyError, UnicodeError):
        raise Problem(403, "SOP_COPY_SOURCE_CHANGED", "the entire approved SOP must remain exact while copying") from None
    return version, content


def _current(authority, session, principal, intent, *, write=True):
    binding, profile, scope, actor, revision, registration, registration_sha = _scope(
        authority, session, principal, write=write)
    if (intent.binding_id != binding.binding_id or intent.job_id != binding.job_id
            or intent.work_id != binding.work.work_id or intent.session_id != session.assignment.session_id
            or intent.task_sha256 != digest(session.assignment.task.model_dump(mode="json"))
            or intent.execution_sha256 != digest(session.execution.model_dump(mode="json"))
            or intent.registration_id != registration or intent.registration_sha256 != registration_sha
            or intent.actor_oid != actor.oid or intent.scope != scope or intent.source_work != profile.legacy_source_work
            or intent.artifact_id != profile.artifact_id or intent.version_id != profile.artifact_version
            or (write and (intent.source_work_revision != revision
                           or _approved(authority, actor, scope) != intent.source_version))):
        raise Problem(403, "SOP_COPY_SCOPE_DENIED", "original intent, source, scope and work cannot be replaced")
    return actor


def _read_effect(authority, row_id):
    row = authority.store.read_snapshot((row_id,))[row_id]
    if row is None:
        return None
    try:
        effect = _parse(row.payload["data"], CopyEffect)
        if row.payload["material"] != material(effect.intent.model_dump(mode="json")).model_dump(mode="json"):
            raise ValueError("intent material differs")
    except (Problem, ValueError):
        raise Problem(409, "SOP_COPY_RECEIPT_INVALID", "the original slot or immutable copy intent is not verifiable") from None
    return effect


def _target(authority, actor, intent, *, allow_later_state=False):
    try:
        record = authority.artifacts.get(actor, intent.scope.team_id, intent.artifact_id)
    except Problem as error:
        if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
            return None
        raise
    document = ArtifactDocument.model_validate(record.payload)
    if (document.kind != "skill" or document.owner_oid != intent.actor_oid or document.owner != actor.actor
            or document.team != intent.scope.team_id or document.artifact_id != intent.artifact_id
            or document.source_work != intent.source_work or document.title != TITLE
            or (not allow_later_state and (document.effective_version_id is not None or len(document.versions) > 1))):
        raise Problem(409, "SOP_COPY_TARGET_CONFLICT", "the original target identity or expected write state differs")
    return record, document


def _unknown():
    return Problem(503, "SOP_COPY_OUTCOME_UNKNOWN", "original copy intent remains unresolved; never change IDs or resend bytes")


def _transition(authority, session, principal, effect, stage, receipt=None, version=None):
    _current(authority, session, principal, effect.intent, write=stage != "saved")
    row_id = effect_key(effect.intent.work_id)
    fresh = False
    updated = effect.model_copy(update={"stage": stage, "receipt": receipt, "target_version": version})
    updated = CopyEffect.model_validate_json(updated.model_dump_json())

    def decide(rows):
        nonlocal fresh
        if stage != "saved":
            _active_root(rows, session)
        current = _row(rows, row_id, CopyEffect)
        if current != effect:
            raise Problem(409, "SOP_COPY_OUTCOME_UNKNOWN", "original draft stage changed; read the original result")
        fresh = True
        return ControlChanges(writes=(_write(row_id, updated, "replace", material(effect.intent.model_dump(mode="json"))),),
                              result={"stage": stage, "intent_sha256": effect.intent_sha256})

    authority._command(session, "sop-copy-" + stage, Command(request_id="copy-sop-draft"), decide, extra=(row_id,))
    if not fresh:
        raise _unknown()
    return updated


def _verify_saved(authority, actor, effect):
    intent = effect.intent
    target = _target(authority, actor, intent, allow_later_state=True)
    if target is None:
        raise _unknown()
    _, document = target
    version = next((value for value in document.versions if value.version_id == intent.version_id), None)
    if version is None:
        raise _unknown()
    if (version.number != 1
            or version.created_by_oid != intent.actor_oid or version.source_work_revision != intent.source_work_revision
            or VersionInput.model_validate(version.model_dump(include=set(VersionInput.model_fields))) != intent.metadata()
            or version.metadata_sha256 is None):
        raise Problem(409, "SOP_COPY_TARGET_CONFLICT", "the saved draft version differs from the original exact copy")
    _, body = authority.artifacts.download(actor, intent.scope.team_id, intent.artifact_id, intent.version_id, version.blob.etag)
    if body != intent.content:
        raise Problem(409, "SOP_COPY_TARGET_CONFLICT", "the saved target is not a byte-exact SOP copy")
    receipt = SopCopyReceipt(
        schema_version="fa-sop-skill-draft-receipt-v1", state_scope="at-original-copy-write",
        receipt_id=effect_key(intent.work_id),
        job_id=intent.job_id, artifact_id=intent.artifact_id, version_id=intent.version_id, visibility="team",
        source_artifact_id=intent.scope.source_artifact_id, source_version_id=intent.scope.source_version_id,
        source_sha256=intent.scope.source_sha256, sha256=version.sha256, byte_length=len(body),
        purpose=intent.scope.purpose, metadata_sha256=version.metadata_sha256, approved=False, effective_version_id=None,
    )
    # M3 approval is a later decision, not part of immutable version metadata.
    return receipt, version.model_copy(update={"approval": None})


def copy_draft(authority, session, command, principal):
    binding, profile, scope, actor, _, registration, registration_sha = _scope(authority, session, principal, write=False)
    row_id = effect_key(binding.work.work_id)
    effect = _read_effect(authority, row_id)
    fresh = False
    if effect is None:
        binding, profile, scope, actor, revision, registration, registration_sha = _scope(authority, session, principal)
        version, content = _source(authority, actor, scope)
        intent = CopyIntent(
            schema_version="fa-sop-skill-copy-intent-v1", binding_id=binding.binding_id,
            job_id=binding.job_id, work_id=binding.work.work_id, session_id=session.assignment.session_id,
            task_sha256=digest(session.assignment.task.model_dump(mode="json")),
            execution_sha256=digest(session.execution.model_dump(mode="json")),
            registration_id=registration, registration_sha256=registration_sha, actor_oid=actor.oid,
            scope=scope, source_version=version, content=content, source_work=profile.legacy_source_work,
            source_work_revision=revision, artifact_id=profile.artifact_id, version_id=profile.artifact_version)
        if _target(authority, actor, intent) is not None:
            raise Problem(409, "SOP_COPY_TARGET_CONFLICT", "a fresh work slot cannot adopt an existing target")
        effect = CopyEffect(schema_version="fa-sop-skill-copy-effect-v1", intent=intent,
                            intent_sha256=digest(intent.model_dump(mode="json")), stage="create_unknown")
        _current(authority, session, principal, intent)
        def reserve(rows):
            nonlocal fresh
            _active_root(rows, session)
            if rows[row_id] is not None:
                raise _unknown()
            fresh = True
            return ControlChanges(writes=(_write(row_id, effect, "create", material(intent.model_dump(mode="json"))),),
                                  result={"stage": "create_unknown", "intent_sha256": effect.intent_sha256})
        authority._command(session, "sop-copy-intent", command, reserve, extra=(row_id,))
        if not fresh:
            raise _unknown()
    actor = _current(authority, session, principal, effect.intent, write=effect.stage in {"create_unknown", "created"})
    if effect.stage == "create_unknown":
        if fresh:
            try:
                authority.artifacts.create(actor, scope.team_id, effect.intent.artifact_id,
                    CreateArtifact(kind="skill", title=TITLE, source_work=effect.intent.source_work))
            except Exception:
                logging.getLogger(__name__).warning("SOP_COPY_CREATE_UNKNOWN original_intent_retained=true")
                raise _unknown() from None
        target = _target(authority, actor, effect.intent)
        if target is None:
            raise _unknown()
        if target[1].pending is not None or target[1].versions:
            raise Problem(409, "SOP_COPY_TARGET_CONFLICT", "an original create acknowledgement cannot adopt a prefilled target")
        effect = _transition(authority, session, principal, effect, "created")
    if effect.stage == "created":
        target = _target(authority, actor, effect.intent)
        if target is None or target[1].pending is not None or target[1].versions:
            raise _unknown()
        effect = _transition(authority, session, principal, effect, "upload_unknown")
        actor = _current(authority, session, principal, effect.intent)
        try:
            authority.artifacts.upload(actor, scope.team_id, effect.intent.artifact_id, effect.intent.version_id,
                                       target[0].etag, effect.intent.metadata(), effect.intent.content)
        except Exception:
            logging.getLogger(__name__).warning("SOP_COPY_UPLOAD_UNKNOWN original_intent_retained=true")
            raise _unknown() from None
    if effect.stage == "upload_unknown":
        actor = _current(authority, session, principal, effect.intent, write=False)
        target = _target(authority, actor, effect.intent, allow_later_state=True)
        if target is None:
            raise _unknown()
        if (not any(value.version_id == effect.intent.version_id for value in target[1].versions)
                and target[1].pending is not None):
            pending = target[1].pending
            if (pending.version_id != effect.intent.version_id or pending.created_by_oid != effect.intent.actor_oid
                    or pending.bytes != len(effect.intent.content) or pending.source_work_revision != effect.intent.source_work_revision
                    or VersionInput.model_validate(pending.model_dump(include=set(VersionInput.model_fields))) != effect.intent.metadata()):
                raise Problem(409, "SOP_COPY_TARGET_CONFLICT", "pending original metadata differs; never resend")
            try:
                authority.artifacts.reconcile(actor, scope.team_id, effect.intent.artifact_id,
                                             effect.intent.version_id, target[0].etag)
            except Exception:
                logging.getLogger(__name__).warning("SOP_COPY_RECONCILE_UNKNOWN original_version_retained=true")
                raise _unknown() from None
        receipt, version = _verify_saved(authority, actor, effect)
        effect = _transition(authority, session, principal, effect, "saved", receipt, version)
    actor = _current(authority, session, principal, effect.intent, write=False)
    if _verify_saved(authority, actor, effect) != (effect.receipt, effect.target_version):
        raise Problem(409, "SOP_COPY_RECEIPT_INVALID", "original saved receipt is no longer exact")
    _current(authority, session, principal, effect.intent, write=False)
    return effect.receipt


def current(authority, session, principal):
    binding, _, scope, actor, _, _, _ = _scope(authority, session, principal, write=False)
    row_id = effect_key(binding.work.work_id)
    effect = _read_effect(authority, row_id)
    if effect is None:
        _scope(authority, session, principal)
        _approved(authority, actor, scope)
    else:
        _current(authority, session, principal, effect.intent, write=False)
        if effect.stage != "saved":
            raise _unknown()
        if _verify_saved(authority, actor, effect) != (effect.receipt, effect.target_version):
            raise Problem(409, "SOP_COPY_RECEIPT_INVALID", "the original copy receipt differs from its exact target")
        _current(authority, session, principal, effect.intent, write=False)
    return authority.store.read_snapshot((row_id,))


def saved_copy(session, rows, *, allow_unresolved=False):
    task = session.assignment.task
    if task.instruction is None or task.instruction.sop_copy is None:
        return None, None
    row = rows[effect_key(session.execution.work_id)]
    if row is None:
        return None, None
    effect = _parse(row.payload["data"], CopyEffect)
    if (effect.intent.task_sha256 != digest(task.model_dump(mode="json"))
            or effect.intent.execution_sha256 != digest(session.execution.model_dump(mode="json"))
            or effect.intent.session_id != session.assignment.session_id
            or row.payload["material"] != material(effect.intent.model_dump(mode="json")).model_dump(mode="json")):
        raise Problem(409, "SOP_COPY_RECEIPT_INVALID", "saved copy does not belong to this original execution")
    if effect.stage != "saved" and not allow_unresolved:
        raise _unknown()
    return effect.receipt, effect.outcome()

