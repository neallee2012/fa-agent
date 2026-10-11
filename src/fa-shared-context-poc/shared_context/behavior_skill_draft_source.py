"""One user-procedure skill draft intent in the original artifact-effect slot."""

import hashlib
import logging
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_capabilities import SKILLIFY_ENTRY_ID, runtime_grants, skillify_granted
from .behavior_operations import BudgetRecord, _Model, _parse, _row, _write, control_key
from .behavior_runtime import material
from .behavior_skill_draft import (
    HISTORY_SKILL_DRAFT_MEMORY_PROFILE, HISTORY_SKILL_DRAFT_PROFILE, SKILL_DRAFT_PROFILE, SaveSkillDraftCommand, SkillDraftOutcome,
    SkillDraftReceipt, skill_draft_bytes, skill_draft_enabled, skill_purpose,
)
from .behavior_store import ControlChanges
from .behavior_wire import Command, Digest, Identifier
from .errors import Problem
from .m3_models import ArtifactDocument, ArtifactVersion, CreateArtifact, SkillPurpose, VersionId, VersionInput
from .m3_models import skill_metadata_sha256
from .source_materials import digest

TITLE = "Unapproved skill draft from user procedure"


class SkillDraftIntent(_Model):
    schema_version: Literal["fa-skill-draft-intent-v1"]
    binding_id: Identifier
    job_id: Identifier
    work_id: Identifier
    session_id: Identifier
    task_sha256: Digest
    execution_sha256: Digest
    registration_id: Identifier
    registration_sha256: Digest
    actor_oid: Identifier
    team_id: Identifier
    source_turn_number: Annotated[int, Field(ge=1)]
    source_user_text_sha256: Digest
    purpose: SkillPurpose
    content: Annotated[bytes, Field(min_length=1, max_length=8192)]
    source_work: Identifier
    source_work_revision: Annotated[int, Field(ge=1)]
    artifact_id: Identifier
    version_id: VersionId

    def metadata(self):
        return VersionInput(filename="skill-draft.md", media_type="text/markdown",
                            sha256=hashlib.sha256(self.content).hexdigest(),
                            source_work=self.source_work, skill_purpose=self.purpose)


class SkillDraftEffect(_Model):
    kind: Literal["assistant_skill_draft"] = "assistant_skill_draft"
    schema_version: Literal["fa-skill-draft-effect-v1"]
    intent: SkillDraftIntent
    intent_sha256: Digest
    stage: Literal["create_unknown", "created", "upload_unknown", "saved"]
    receipt: SkillDraftReceipt | None = None
    target_version: ArtifactVersion | None = None

    @model_validator(mode="after")
    def immutable(self):
        if self.intent_sha256 != digest(self.intent.model_dump(mode="json")):
            raise ValueError("Original skill draft material changed")
        if ((self.stage == "saved") != (self.receipt is not None)
                or (self.receipt is None) != (self.target_version is None)):
            raise ValueError("Only a verified saved draft has a public receipt")
        if self.receipt is not None:
            ref, intent, version = self.receipt, self.intent, self.target_version
            sha = hashlib.sha256(intent.content).hexdigest()
            if (ref.job_id != intent.job_id or ref.artifact_id != intent.artifact_id
                    or ref.version_id != intent.version_id
                    or ref.source_turn_number != intent.source_turn_number
                    or ref.source_user_text_sha256 != intent.source_user_text_sha256
                    or ref.sha256 != sha or ref.byte_length != len(intent.content)
                    or ref.purpose != intent.purpose
                    or version.version_id != intent.version_id or version.sha256 != sha
                    or version.approval is not None or version.source_work != intent.source_work
                    or version.created_by_oid != intent.actor_oid
                    or version.source_work_revision != intent.source_work_revision
                    or version.skill_purpose != intent.purpose or version.blob.bytes != len(intent.content)
                    or ref.metadata_sha256 != version.metadata_sha256
                    or version.metadata_sha256 != skill_metadata_sha256(
                        intent.team_id, intent.artifact_id, version.model_dump(mode="json"))):
                raise ValueError("Skill draft receipt differs from its immutable original intent")
        return self

    def outcome(self):
        return SkillDraftOutcome(schema_version="fa-skill-draft-outcome-v1", receipt_id=effect_key(self.intent.work_id),
                                 intent_sha256=self.intent_sha256, stage=self.stage)


def effect_key(work_id):
    from .behavior_assistant import key
    return key("artifact", work_id)


def _active_root(rows, session):
    if not _row(rows, control_key("budget", session.execution.owner.root_budget_id), BudgetRecord).active:
        raise Problem(403, "SKILL_DRAFT_SCOPE_DENIED", "the original root must remain active")


def _content(command: SaveSkillDraftCommand):
    try:
        purpose = skill_purpose(command.purpose)
        body = skill_draft_bytes(command.body)
    except ValueError as exc:
        raise Problem(422, "SKILL_DRAFT_CONTENT_INVALID", "skill draft purpose and body must fit the bounded visible text contract") from exc
    return purpose, body


def _profile_authorized(control, registered, installed, prompt_profile):
    """The legacy profile needs its installation; other writers need their exact original human selection."""
    if prompt_profile == SKILL_DRAFT_PROFILE:
        return installed.profile.prompt_profile == SKILL_DRAFT_PROFILE
    selection_id = registered.profile_selection_id
    choice = None if selection_id is None else control.profile_selections.get(selection_id)
    if prompt_profile in {"fa-instruction-v2", "fa-instruction-v3", "fa-instruction-v4", "fa-instruction-agent-role-v2",
                          "fa-instruction-agent-role-v3", "fa-instruction-agent-role-v4"}:
        task = registered.profile.instruction
        if task is None or not skillify_granted(task.capabilities):
            return False
        grant = next(item for item in task.capabilities.grants if item.entry_id == SKILLIFY_ENTRY_ID)
        return (choice is not None and choice.selection_id == selection_id
                and choice.command.template.role.profile == prompt_profile
                and grant.ref in choice.command.template.settings.capabilities
                and runtime_grants(control.runtime).get(SKILLIFY_ENTRY_ID) == grant)
    selected_base = (choice is not None and choice.selection_id == selection_id
                     and choice.command.template.role.profile == HISTORY_SKILL_DRAFT_PROFILE)
    if prompt_profile == HISTORY_SKILL_DRAFT_MEMORY_PROFILE:
        if not selected_base:
            return False
        from .memory_bridge_settings import validate_profile_memory
        validate_profile_memory(control, registered.profile, registered.binding)
        return True
    return prompt_profile == HISTORY_SKILL_DRAFT_PROFILE and selected_base


def _scope(authority, session, principal, *, write=True):
    from .behavior_work_policy import work_policy_key
    binding, _, _ = authority.runtime.authorize(session.binding_id, principal, "read")
    if write:
        executable, _, _ = authority.runtime.authorize(session.binding_id, principal, "execute")
        if binding != executable:
            raise Problem(403, "SKILL_DRAFT_SCOPE_DENIED", "current read and write authority must bind the same work")
    task, profile = session.assignment.task, authority.runtime.profiles[session.binding_id]
    control = authority.runtime.work_policy
    if (task.instruction is None or not skill_draft_enabled(
            task.instruction.prompt_profile, task.instruction.capabilities)
            or profile.prompt_profile != task.instruction.prompt_profile
            or task.profile_digest != digest(profile.model_dump(mode="json"))
            or session.state != "running" or control is None):
        raise Problem(403, "SKILL_DRAFT_DISABLED", "only the exact original server-authorized draft profile may write this draft")
    rows = authority.store.read_snapshot(authority._keys(session))
    if write:
        authority._gate(session, rows)
        _active_root(rows, session)
    installed = control.installed(rows)
    registered = control.records.get(binding.binding_id)
    if (registered is None or registered.binding != binding or registered.profile != profile
            or not _profile_authorized(control, registered, installed, task.instruction.prompt_profile)
            or task.artifact_id != profile.artifact_id or task.artifact_version != profile.artifact_version
            or principal.kind != "agent" or principal.oid != binding.agent_oid):
        raise Problem(403, "SKILL_DRAFT_SCOPE_DENIED", "original installation, Team, job and single draft slot must agree")
    actor = authority.actors.get(binding.agent_oid)
    if actor is None or actor.team != binding.team_id or actor.actor != principal.source_actor_id:
        raise Problem(403, "SKILL_DRAFT_SCOPE_DENIED", "the actual verified server-bound owning Actor is required")
    authority.artifacts.authorize(actor, binding.team_id)
    revision = None
    if write:
        legacy = authority.artifacts.context.get(actor, binding.team_id, profile.legacy_source_work)
        if legacy.payload["owner"] != actor.actor:
            raise Problem(403, "SKILL_DRAFT_SCOPE_DENIED", "the configured existing legacy source work must still be owned")
        revision = legacy.payload["revision"]
    registration_id = work_policy_key("registration", registered.admission_id)
    if rows[registration_id].payload["material"] != material(registered.model_dump(mode="json")).model_dump(mode="json"):
        raise Problem(403, "SKILL_DRAFT_SCOPE_DENIED", "original immutable registration material differs")
    return binding, profile, actor, revision, registration_id, digest(registered.model_dump(mode="json"))


def _current(authority, session, principal, intent, *, write=True):
    binding, profile, actor, revision, registration, registration_sha = _scope(
        authority, session, principal, write=write)
    task = session.assignment.task.instruction
    if (intent.binding_id != binding.binding_id or intent.job_id != binding.job_id
            or intent.work_id != binding.work.work_id or intent.session_id != session.assignment.session_id
            or intent.task_sha256 != digest(session.assignment.task.model_dump(mode="json"))
            or intent.execution_sha256 != digest(session.execution.model_dump(mode="json"))
            or intent.registration_id != registration or intent.registration_sha256 != registration_sha
            or intent.actor_oid != actor.oid or intent.team_id != binding.team_id
            or intent.source_turn_number != task.turn_number
            or intent.source_user_text_sha256 != hashlib.sha256(task.content.encode("utf-8")).hexdigest()
            or intent.source_work != profile.legacy_source_work
            or intent.artifact_id != profile.artifact_id or intent.version_id != profile.artifact_version
            or (write and intent.source_work_revision != revision)):
        raise Problem(403, "SKILL_DRAFT_SCOPE_DENIED", "original intent, source, scope and work cannot be replaced")
    return actor


def _read_effect(authority, row_id):
    row = authority.store.read_snapshot((row_id,))[row_id]
    if row is None:
        return None
    try:
        effect = _parse(row.payload["data"], SkillDraftEffect)
        if row.payload["material"] != material(effect.intent.model_dump(mode="json")).model_dump(mode="json"):
            raise ValueError("intent material differs")
    except (Problem, ValueError):
        raise Problem(409, "SKILL_DRAFT_RECEIPT_INVALID", "the original slot or immutable draft intent is not verifiable") from None
    return effect


def _target(authority, actor, intent, *, allow_later_state=False):
    try:
        record = authority.artifacts.get(actor, intent.team_id, intent.artifact_id)
    except Problem as error:
        if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
            return None
        raise
    document = ArtifactDocument.model_validate(record.payload)
    if (document.kind != "skill" or document.owner_oid != intent.actor_oid or document.owner != actor.actor
            or document.team != intent.team_id or document.artifact_id != intent.artifact_id
            or document.source_work != intent.source_work or document.title != TITLE
            or (not allow_later_state and (document.effective_version_id is not None or len(document.versions) > 1))):
        raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the original target identity or expected write state differs")
    return record, document


def _unknown():
    return Problem(503, "SKILL_DRAFT_OUTCOME_UNKNOWN",
                   "original skill draft intent remains unresolved; never change IDs or resend bytes")


def _transition(authority, session, principal, effect, stage, receipt=None, version=None):
    _current(authority, session, principal, effect.intent, write=stage != "saved")
    row_id = effect_key(effect.intent.work_id)
    fresh = False
    updated = effect.model_copy(update={"stage": stage, "receipt": receipt, "target_version": version})
    updated = SkillDraftEffect.model_validate_json(updated.model_dump_json())

    def decide(rows):
        nonlocal fresh
        if stage != "saved":
            _active_root(rows, session)
        current = _row(rows, row_id, SkillDraftEffect)
        if current != effect:
            raise Problem(409, "SKILL_DRAFT_OUTCOME_UNKNOWN", "original draft stage changed; read the original result")
        fresh = True
        return ControlChanges(writes=(_write(row_id, updated, "replace", material(effect.intent.model_dump(mode="json"))),),
                              result={"stage": stage, "intent_sha256": effect.intent_sha256})

    authority._command(session, "skill-draft-" + stage, Command(request_id="save-skill-draft"), decide, extra=(row_id,))
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
        raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the saved draft version differs from the original write")
    _, body = authority.artifacts.download(actor, intent.team_id, intent.artifact_id, intent.version_id, version.blob.etag)
    if body != intent.content:
        raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the saved target is not the original skill draft body")
    receipt = SkillDraftReceipt(
        schema_version="fa-skill-draft-receipt-v1", state_scope="at-original-draft-write",
        receipt_id=effect_key(intent.work_id), job_id=intent.job_id, artifact_id=intent.artifact_id,
        version_id=intent.version_id, visibility="team", source_turn_number=intent.source_turn_number,
        source_user_text_sha256=intent.source_user_text_sha256, sha256=version.sha256,
        byte_length=len(body), purpose=intent.purpose, metadata_sha256=version.metadata_sha256,
        approved=False, effective_version_id=None,
    )
    return receipt, version.model_copy(update={"approval": None})


def save_draft(authority, session, command, principal):
    purpose, content = _content(command)
    binding, profile, actor, _, registration, registration_sha = _scope(authority, session, principal, write=False)
    row_id = effect_key(binding.work.work_id)
    effect = _read_effect(authority, row_id)
    fresh = False
    if effect is not None and (effect.intent.purpose != purpose or effect.intent.content != content):
        raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "one work can retain only its original skill draft content")
    if effect is None:
        binding, profile, actor, revision, registration, registration_sha = _scope(authority, session, principal)
        task = session.assignment.task.instruction
        intent = SkillDraftIntent(
            schema_version="fa-skill-draft-intent-v1", binding_id=binding.binding_id,
            job_id=binding.job_id, work_id=binding.work.work_id, session_id=session.assignment.session_id,
            task_sha256=digest(session.assignment.task.model_dump(mode="json")),
            execution_sha256=digest(session.execution.model_dump(mode="json")),
            registration_id=registration, registration_sha256=registration_sha, actor_oid=actor.oid,
            team_id=binding.team_id, source_turn_number=task.turn_number,
            source_user_text_sha256=hashlib.sha256(task.content.encode("utf-8")).hexdigest(),
            purpose=purpose, content=content, source_work=profile.legacy_source_work,
            source_work_revision=revision, artifact_id=profile.artifact_id, version_id=profile.artifact_version)
        if _target(authority, actor, intent) is not None:
            raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "a fresh work slot cannot adopt an existing target")
        effect = SkillDraftEffect(schema_version="fa-skill-draft-effect-v1", intent=intent,
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

        authority._command(session, "skill-draft-intent", command, reserve, extra=(row_id,))
        if not fresh:
            raise _unknown()
    actor = _current(authority, session, principal, effect.intent, write=effect.stage in {"create_unknown", "created"})
    if effect.stage == "create_unknown":
        if fresh:
            try:
                authority.artifacts.create(actor, effect.intent.team_id, effect.intent.artifact_id,
                    CreateArtifact(kind="skill", title=TITLE, source_work=effect.intent.source_work))
            except Exception:
                logging.getLogger(__name__).warning("SKILL_DRAFT_CREATE_UNKNOWN original_intent_retained=true")
                raise _unknown() from None
        target = _target(authority, actor, effect.intent)
        if target is None:
            raise _unknown()
        if target[1].pending is not None or target[1].versions:
            raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "an original create acknowledgement cannot adopt a prefilled target")
        effect = _transition(authority, session, principal, effect, "created")
    if effect.stage == "created":
        target = _target(authority, actor, effect.intent)
        if target is None or target[1].pending is not None or target[1].versions:
            raise _unknown()
        effect = _transition(authority, session, principal, effect, "upload_unknown")
        actor = _current(authority, session, principal, effect.intent)
        try:
            authority.artifacts.upload(actor, effect.intent.team_id, effect.intent.artifact_id, effect.intent.version_id,
                                       target[0].etag, effect.intent.metadata(), effect.intent.content)
        except Exception:
            logging.getLogger(__name__).warning("SKILL_DRAFT_UPLOAD_UNKNOWN original_intent_retained=true")
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
                    or pending.bytes != len(effect.intent.content)
                    or pending.source_work_revision != effect.intent.source_work_revision
                    or VersionInput.model_validate(pending.model_dump(include=set(VersionInput.model_fields))) != effect.intent.metadata()):
                raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "pending original metadata differs; never resend")
            try:
                authority.artifacts.reconcile(actor, effect.intent.team_id, effect.intent.artifact_id,
                                             effect.intent.version_id, target[0].etag)
            except Exception:
                logging.getLogger(__name__).warning("SKILL_DRAFT_RECONCILE_UNKNOWN original_version_retained=true")
                raise _unknown() from None
        receipt, version = _verify_saved(authority, actor, effect)
        effect = _transition(authority, session, principal, effect, "saved", receipt, version)
    actor = _current(authority, session, principal, effect.intent, write=False)
    if _verify_saved(authority, actor, effect) != (effect.receipt, effect.target_version):
        raise Problem(409, "SKILL_DRAFT_RECEIPT_INVALID", "original saved receipt is no longer exact")
    _current(authority, session, principal, effect.intent, write=False)
    return effect.receipt


def current(authority, session, principal):
    binding, _, actor, _, _, _ = _scope(authority, session, principal, write=False)
    row_id = effect_key(binding.work.work_id)
    effect = _read_effect(authority, row_id)
    if effect is None:
        _scope(authority, session, principal)
    else:
        _current(authority, session, principal, effect.intent, write=False)
        if effect.stage != "saved":
            raise _unknown()
        if _verify_saved(authority, actor, effect) != (effect.receipt, effect.target_version):
            raise Problem(409, "SKILL_DRAFT_RECEIPT_INVALID", "the original skill draft receipt differs from its exact target")
        _current(authority, session, principal, effect.intent, write=False)
    return authority.store.read_snapshot((row_id,))


def saved_draft(session, rows, *, allow_unresolved=False):
    task = session.assignment.task
    if task.instruction is None or not skill_draft_enabled(
            task.instruction.prompt_profile, task.instruction.capabilities):
        return None, None
    row = rows[effect_key(session.execution.work_id)]
    if row is None:
        return None, None
    effect = _parse(row.payload["data"], SkillDraftEffect)
    if (effect.intent.task_sha256 != digest(task.model_dump(mode="json"))
            or effect.intent.execution_sha256 != digest(session.execution.model_dump(mode="json"))
            or effect.intent.session_id != session.assignment.session_id
            or row.payload["material"] != material(effect.intent.model_dump(mode="json")).model_dump(mode="json")):
        raise Problem(409, "SKILL_DRAFT_RECEIPT_INVALID", "saved draft does not belong to this original execution")
    if effect.stage != "saved" and not allow_unresolved:
        raise _unknown()
    return effect.receipt, effect.outcome()


def validate_history(view, record):
    from types import SimpleNamespace
    from .behavior_assistant import AssistantAuthority, _Session, key
    from .behavior_runtime import BehaviorRuntime
    from .behavior_runtime_rotation_history import require
    from .behavior_store import ControlRequest
    from .behavior_work_policy import WorkPolicyControl, registration_receipt_id, work_policy_key
    from .behavior_workers import worker_key
    binding, task = record.binding, record.profile.instruction
    row_id = effect_key(binding.work.work_id)
    if row_id not in view.inventory.rows:
        return None, set()
    effect = view.row(row_id, SkillDraftEffect)
    intent = effect.intent
    sid = key("session-id", worker_key("start", binding.job_id, "1"))
    session = view.row(key("session", sid), _Session)
    require(skill_draft_enabled(task.prompt_profile, task.capabilities) and record.profile.prompt_profile == task.prompt_profile
            and (task.prompt_profile not in {HISTORY_SKILL_DRAFT_PROFILE, "fa-instruction-v2", "fa-instruction-v3",
                                             "fa-instruction-v4", "fa-instruction-agent-role-v2",
                                             "fa-instruction-agent-role-v3", "fa-instruction-agent-role-v4"}
                 or record.profile_selection_id is not None)
            and intent.session_id == sid and intent.job_id == binding.job_id and intent.work_id == binding.work.work_id
            and intent.binding_id == binding.binding_id and intent.actor_oid == binding.agent_oid
            and intent.team_id == binding.team_id and intent.artifact_id == record.profile.artifact_id
            and intent.version_id == record.profile.artifact_version
            and intent.source_work == record.profile.legacy_source_work
            and intent.source_turn_number == task.turn_number
            and intent.source_user_text_sha256 == hashlib.sha256(task.content.encode("utf-8")).hexdigest()
            and intent.registration_id == work_policy_key("registration", record.admission_id)
            and intent.registration_sha256 == digest(record.model_dump(mode="json"))
            and intent.task_sha256 == digest(session.assignment.task.model_dump(mode="json"))
            and intent.execution_sha256 == digest(session.execution.model_dump(mode="json"))
            and view.inventory.rows[row_id].payload["material"] == material(intent.model_dump(mode="json")).model_dump(mode="json"),
            "skill draft intent lost its original authorized task, source or immutable material")
    control = object.__new__(WorkPolicyControl)
    control.policy = SimpleNamespace(authority_domain=view.config.table.scope.authority_domain)
    control.records = {binding.binding_id: record}
    runtime = SimpleNamespace(teams_metrics=None, work_policy=control, bindings={binding.binding_id: binding},
        refresh_bindings=lambda binding_id=None: None, dep=SimpleNamespace(workers=SimpleNamespace(
            pool_key=lambda job: worker_key("pool", view.config.workers.workspace_id, job))))
    runtime._gate_keys = lambda current: BehaviorRuntime._gate_keys(runtime, current)
    authority = object.__new__(AssistantAuthority)
    authority.runtime = runtime
    reads = tuple(dict.fromkeys((*authority._keys(session), row_id)))
    intent_command = SaveSkillDraftCommand(
        request_id="save-skill-draft", purpose=intent.purpose.description, body=intent.content.decode("utf-8"))
    stage_command = Command(request_id="save-skill-draft")
    sequence = ("create_unknown", "created", "upload_unknown", "saved")
    previous = view.receipts[registration_receipt_id(record)].guard_revision
    for stage in sequence[:sequence.index(effect.stage) + 1]:
        image = effect.model_copy(update={"stage": stage, "receipt": effect.receipt if stage == "saved" else None,
                                          "target_version": effect.target_version if stage == "saved" else None})
        name = "skill-draft-intent" if stage == "create_unknown" else "skill-draft-" + stage
        command = intent_command if stage == "create_unknown" else stage_command
        request = ControlRequest(request_id=key(name, sid, command.request_id),
                                 request_digest=digest(command.model_dump(mode="json")), read_set=reads)
        receipt = view.receipt(request, (row_id,), exact={row_id: view.image_hash(row_id, image)},
                              result={"stage": stage, "intent_sha256": effect.intent_sha256})
        require(receipt.guard_revision > previous, "skill draft stage precedes its original intent or registration")
        previous = receipt.guard_revision
    return effect, {row_id}
