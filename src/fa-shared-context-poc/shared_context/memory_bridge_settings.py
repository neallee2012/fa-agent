"""Human-approved future-conversation memory settings; never mutate standing policy."""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_operations import control_key
from .behavior_runtime import material
from .behavior_store import ControlChanges, ControlRequest, ControlWrite
from .behavior_wire import Command, Digest, Identifier, WireModel
from .behavior_work_memory import WorkMemorySource
from .behavior_reviewed_note_source import resolve_reviewed_note
from .errors import Problem
from .source_materials import canonical, digest


class MemoryRecallChoice(WireModel):
    origin_binding_id: Identifier
    claim_id: Identifier
    expected_claim_version: Annotated[int, Field(ge=1)]
    expected_claim_sha256: Digest
    use_in_new_conversations: bool

    @model_validator(mode="after")
    def explicit_consent(self):
        if not self.use_in_new_conversations:
            raise ValueError("the human must explicitly allow this logical note in future conversations")
        return self


class SelectMemoryBridgeSettings(Command):
    schema_version: Literal["fa-memory-bridge-settings-command-v1"]
    expected_settings_id: Identifier | None
    enabled: bool
    recall: MemoryRecallChoice | None = None

    @model_validator(mode="after")
    def enabled_recall(self):
        if not self.enabled and self.recall is not None:
            raise ValueError("disabled settings cannot select a note")
        return self


class MemoryBridgeSettingsRecord(WireModel):
    kind: Literal["new_conversation_memory_settings"] = "new_conversation_memory_settings"
    settings_id: Identifier
    revision: Annotated[int, Field(ge=1)]
    policy_sha256: Digest
    selected_by: Identifier
    selected_at: datetime
    command: SelectMemoryBridgeSettings
    selected_source: WorkMemorySource | None
    request: ControlRequest


class MemoryBridgeSettingsHead(WireModel):
    kind: Literal["new_conversation_memory_settings_head"] = "new_conversation_memory_settings_head"
    settings_id: Identifier


class CapturedReviewedNote(WireModel):
    origin_binding_id: Identifier
    source: WorkMemorySource


class ConversationMemoryCapture(WireModel):
    schema_version: Literal["fa-conversation-memory-v1"] = "fa-conversation-memory-v1"
    settings_id: Identifier
    enabled: bool
    recall: CapturedReviewedNote | None = None

    @model_validator(mode="after")
    def no_disabled_recall(self):
        if not self.enabled and self.recall is not None:
            raise ValueError("disabled captured memory cannot include a note")
        return self


class MemorySettingReference(WireModel):
    admission_id: Digest
    settings_id: Digest
    capture_sha256: Digest
    enabled: Literal[True] = True
    state: Literal["candidate", "captured"]


def memory_setting_reference(admission_id, capture, *, captured):
    if capture is None or not capture.enabled:
        return None
    return MemorySettingReference(admission_id=admission_id, settings_id=capture.settings_id,
        capture_sha256=digest(capture.model_dump(mode="json")), state="captured" if captured else "candidate")


@dataclass(frozen=True)
class MemoryAdmissionCapture:
    value: ConversationMemoryCapture | None
    predicates: dict


class MemoryRecallSelectionView(WireModel):
    origin_binding_id: Identifier
    claim_id: Identifier
    version_policy: Literal["current-human-reviewed-version-at-new-conversation"]


class MemoryBridgeSettingsView(WireModel):
    settings_id: Identifier | None
    revision: Annotated[int, Field(ge=1)] | None = None
    enabled: bool
    recall: MemoryRecallSelectionView | None
    applies_to: Literal["new-conversations-only"]
    data_scope_change: bool

    @model_validator(mode="after")
    def explicit_setting(self):
        if ((self.settings_id is None) != (self.revision is None)
                or self.data_scope_change != (self.settings_id is not None)
                or self.enabled and self.settings_id is None
                or not self.enabled and self.recall is not None):
            raise ValueError("the new-conversation setting is incomplete")
        return self


def _memory_scope_ids(domain, agent_scope):
    return (domain,) if agent_scope is None else (domain, agent_scope.registry_id, agent_scope.agent_id)


def settings_key(domain, request_id, agent_scope=None):
    return control_key("memory-bridge-settings", *_memory_scope_ids(domain, agent_scope), request_id)


def settings_head_key(domain, agent_scope=None):
    return control_key("memory-bridge-settings-head", *_memory_scope_ids(domain, agent_scope))


def settings_result(record):
    choice = record.command.recall
    return {
        "settings_id": record.settings_id, "revision": record.revision,
        "enabled": record.command.enabled, "applies_to": "new-conversations-only",
        "data_scope_change": True,
        "recall": None if choice is None else {
            "origin_binding_id": choice.origin_binding_id, "claim_id": choice.claim_id,
            "version_policy": "current-human-reviewed-version-at-new-conversation",
        },
    }


def read_memory_settings(store, policy, identifier):
    from .source_materials import _payload_hash

    row = store.read_snapshot((identifier,))[identifier]
    if row is None:
        raise Problem(409, "MEMORY_SETTINGS_MISSING", "the original approved memory setting is missing")
    try:
        record = MemoryBridgeSettingsRecord.model_validate_json(canonical(row.payload["data"]))
        if (record.settings_id != identifier
                or identifier != settings_key(policy.authority_domain, record.command.request_id, policy.agent_scope)
                or record.policy_sha256 != policy.sha256 or record.selected_by != policy.template.human_oid
                or record.request.request_id != identifier
                or record.request.request_digest != digest([
                    policy.sha256, record.selected_by, record.command.model_dump(mode="json")])
                or (record.command.recall is None) != (record.selected_source is None)
                or row.payload["material"] != material(record.model_dump(mode="json")).model_dump(mode="json")):
            raise ValueError("original setting differs")
        if record.command.recall is not None:
            choice, source = record.command.recall, record.selected_source
            if (source.claim_id != choice.claim_id or source.claim_version != choice.expected_claim_version
                    or source.claim_sha256 != choice.expected_claim_sha256
                    or source.team_id != policy.template.team_id or source.subject != policy.template.subject
                    or source.purpose != policy.legacy_template_policy.purpose):
                raise ValueError("original note selection differs")
    except (ValueError, TypeError, KeyError):
        raise Problem(409, "MEMORY_SETTINGS_INVALID", "the original memory setting cannot be verified") from None
    receipt = store.reconcile(record.request)
    if dict(receipt.payload["writes"]).get(identifier) != _payload_hash(row.payload):
        raise Problem(409, "MEMORY_SETTINGS_INVALID", "the setting is not covered by its original receipt")
    return record


def current_memory_settings(store, policy):
    from .source_materials import _payload_hash

    key = settings_head_key(policy.authority_domain, policy.agent_scope)
    row = store.read_snapshot((key,))[key]
    if row is None:
        return None
    head = MemoryBridgeSettingsHead.model_validate_json(canonical(row.payload["data"]))
    record = read_memory_settings(store, policy, head.settings_id)
    receipt = store.reconcile(record.request)
    if row.payload["material"] is not None or dict(receipt.payload["writes"]).get(key) != _payload_hash(row.payload):
        raise Problem(409, "MEMORY_SETTINGS_INVALID", "the current pointer differs from its acknowledged setting")
    return record


def resolve_settings_note(runtime, principal, policy, record):
    """Resolve a logical note afresh only for a new conversation, not an old captured session."""
    choice = record.command.recall
    if choice is None:
        return None
    if not record.command.enabled or record.policy_sha256 != policy.sha256:
        raise Problem(403, "MEMORY_SETTINGS_INVALID", "the original selected memory scope is not enabled")
    return resolve_reviewed_note(runtime, principal, choice.origin_binding_id, choice.claim_id,
                                 policy.template, policy.legacy_template_policy.purpose)


def capture_new_conversation_memory(control, principal):
    """Pure admission input; callers must retain it and fence its predicates during the original admission."""
    control._human(principal)
    policy = control.installed()
    if not policy.active or not policy.read_enabled:
        raise Problem(403, "WORK_POLICY_REVOKED", "the original new-conversation scope is unavailable")
    record = current_memory_settings(control.store, policy)
    head_key = settings_head_key(policy.authority_domain, policy.agent_scope)
    if record is None:
        return MemoryAdmissionCapture(None, dict(control.store.read_snapshot((head_key,))))
    selected = resolve_settings_note(control.runtime, principal, policy, record)
    predicates = dict(control.store.read_snapshot((head_key, record.settings_id)))
    if selected is not None:
        predicates.update(selected.predicates)
    return MemoryAdmissionCapture(
        ConversationMemoryCapture(
            settings_id=record.settings_id, enabled=record.command.enabled,
            recall=None if selected is None else CapturedReviewedNote(
                origin_binding_id=selected.origin_binding_id, source=selected.value.source),
        ), predicates,
    )


def memory_slot_ids(admission_id):
    from .behavior_work_policy import work_policy_key
    return (work_policy_key("memory-note-source", admission_id),
            work_policy_key("memory-note-claim", admission_id))


def with_memory_slots(binding, admission_id):
    source_id, claim_id = memory_slot_ids(admission_id)
    return type(binding).model_validate_json(binding.model_copy(update={
        "source_ids": (*binding.source_ids, source_id),
        "claim_ids": (*binding.claim_ids, claim_id),
    }).model_dump_json())


def validate_capture_record(policy, capture, record):
    if (record.settings_id != capture.settings_id or record.policy_sha256 != policy.sha256
            or record.command.enabled != capture.enabled or record.selected_by != policy.template.human_oid
            or (record.command.recall is None) != (capture.recall is None)):
        raise Problem(409, "MEMORY_SETTINGS_INVALID", "the captured setting differs from its original approval")
    if capture.recall is not None:
        choice, source = record.command.recall, capture.recall.source
        if (capture.recall.origin_binding_id != choice.origin_binding_id or source.claim_id != choice.claim_id
                or source.claim_version < choice.expected_claim_version
                or source.claim_version == choice.expected_claim_version and source.claim_sha256 != choice.expected_claim_sha256
                or source.team_id != policy.template.team_id or source.subject != policy.template.subject
                or source.purpose != policy.legacy_template_policy.purpose):
            raise Problem(403, "MEMORY_RECALL_SCOPE_DENIED", "the captured note differs from the approved logical note")


def validate_profile_memory(control, profile, binding):
    capture = profile.memory_scope
    if capture is None or not capture.enabled or capture.recall is None:
        raise Problem(403, "MEMORY_RECALL_DISABLED", "the original conversation has no captured reviewed note")
    record = read_memory_settings(control.store, control.policy, capture.settings_id)
    validate_capture_record(control.policy, capture, record)
    choice = record.command.recall
    source = capture.recall.source
    if (not record.command.enabled or choice is None
            or capture.recall.origin_binding_id != choice.origin_binding_id
            or source.claim_id != choice.claim_id or source.claim_version < choice.expected_claim_version
            or source.team_id != binding.team_id or source.subject != binding.subject
            or source.purpose != control.policy.legacy_template_policy.purpose):
        raise Problem(403, "MEMORY_RECALL_SCOPE_DENIED", "the capture differs from its human-approved note scope")
    return record


def memory_for_conversation(control, principal, conversation):
    if not conversation.jobs:
        return capture_new_conversation_memory(control, principal)
    previous = control._job_record(conversation.jobs[-1])
    if previous is None:
        control.refresh()
        previous = control._job_record(conversation.jobs[-1])
    if previous is None or previous.profile.instruction is None:
        raise Problem(409, "MEMORY_SETTINGS_MISSING", "the original conversation capture is unavailable")
    if previous.agent_scope != control.policy.agent_scope:
        raise Problem(409, "MEMORY_SETTINGS_INVALID", "the original conversation belongs to another agent scope")
    capture = previous.profile.memory_scope
    if capture is None:
        return MemoryAdmissionCapture(None, {})
    record = read_memory_settings(control.store, control.policy, capture.settings_id)
    validate_capture_record(control.policy, capture, record)
    return MemoryAdmissionCapture(capture, dict(control.store.read_snapshot((record.settings_id,))))


def apply_memory_capture(profile, binding, admission_id, capture, *, selected_template):
    from .behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE, HISTORY_SKILL_DRAFT_MEMORY_PROFILE

    if capture is None:
        return profile, binding
    capture = ConversationMemoryCapture.model_validate_json(capture.model_dump_json())
    updates = {"memory_scope": capture}
    if capture.enabled:
        binding = with_memory_slots(binding, admission_id)
    if capture.recall is not None:
        if (selected_template is None or selected_template.role.profile != HISTORY_SKILL_DRAFT_PROFILE
                or profile.prompt_profile != HISTORY_SKILL_DRAFT_PROFILE):
            raise Problem(403, "MEMORY_RECALL_PROFILE_REQUIRED", "select the human-approved history/draft base for note recall")
        updates["prompt_profile"] = HISTORY_SKILL_DRAFT_MEMORY_PROFILE
    profile = type(profile).model_validate_json(profile.model_copy(update=updates).model_dump_json())
    return profile, binding


def captured_policy_fields(capture):
    return ({"human_text_enabled": True, "text_policy_version": "human-text-policy-v1"}
            if capture is not None and capture.enabled else {})


def human_memory_source_write(runtime, binding, source_id, principal):
    control = runtime.work_policy
    record = None if control is None else control.records.get(binding.binding_id)
    capture = None if record is None else record.profile.memory_scope
    if capture is not None and capture.enabled and source_id == memory_slot_ids(record.admission_id)[0]:
        control._human(principal, "approve")


class MemoryBridgeSettingsService:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    def _authorize(self, principal, *, approve=False):
        control = self.runtime.work_policy
        if control is None:
            raise Problem(403, "MEMORY_SETTINGS_DISABLED", "the original standing work authority is required")
        control._human(principal)
        if approve:
            control._human(principal, "approve")
        policy = control.installed()
        if not policy.active or not policy.read_enabled:
            raise Problem(403, "WORK_POLICY_REVOKED", "current standing Read authority is required")
        return control, policy

    def _selection(self, policy, command, principal):
        if command.recall is None:
            return None
        choice = command.recall
        result = resolve_reviewed_note(
            self.runtime, principal, choice.origin_binding_id, choice.claim_id,
            policy.template, policy.legacy_template_policy.purpose,
        )
        source = result.value.source
        if (source.claim_version != choice.expected_claim_version
                or source.claim_sha256 != choice.expected_claim_sha256):
            raise Problem(409, "MEMORY_RECALL_CHANGED", "select the exact current reviewed note, not a stale preview")
        return result

    def current(self, principal):
        def read():
            _, policy = self._authorize(principal)
            record = current_memory_settings(self.store, policy)
            return ({"settings_id": None, "enabled": False, "recall": None,
                     "applies_to": "new-conversations-only", "data_scope_change": False}
                    if record is None else settings_result(record))
        return self.store.consistent_read(read)

    def preview(self, principal, command):
        def read():
            _, policy = self._authorize(principal)
            current = current_memory_settings(self.store, policy)
            if (None if current is None else current.settings_id) != command.expected_settings_id:
                raise Problem(409, "MEMORY_SETTINGS_CONFLICT", "the selected memory setting changed")
            note = self._selection(policy, command, principal)
            return {
                "command": command.model_dump(mode="json"), "applies_to": "new-conversations-only",
                "data_scope_change": True, "submitted": False,
                "note": None if note is None else {
                    "origin_binding_id": note.origin_binding_id, "text": note.value.text,
                    "source": note.value.source.model_dump(mode="json"),
                    "usage": "knowledge-data-not-permission",
                },
            }
        return self.store.consistent_read(read)

    def select(self, principal, command):
        control, policy = self._authorize(principal, approve=True)
        identifier = settings_key(policy.authority_domain, command.request_id, policy.agent_scope)
        if self.store.read_snapshot((identifier,))[identifier] is not None:
            return self.reconcile(principal, command)

        def inspect():
            self._authorize(principal, approve=True)
            current = current_memory_settings(self.store, policy)
            if (None if current is None else current.settings_id) != command.expected_settings_id:
                raise Problem(409, "MEMORY_SETTINGS_CONFLICT", "the selected memory setting changed; read before deciding")
            note = self._selection(policy, command, principal)
            keys = tuple(dict.fromkeys((
                control.policy_key, control.revocation_key, identifier,
                settings_head_key(policy.authority_domain, policy.agent_scope),
                *(() if note is None else note.predicates),
            )))
            return current, note, keys, self.store.read_snapshot(keys)

        previous, note, keys, observed = self.store.consistent_read(inspect)
        request = ControlRequest(
            request_id=identifier, request_digest=digest([policy.sha256, principal.oid, command.model_dump(mode="json")]),
            read_set=keys,
        )
        record = MemoryBridgeSettingsRecord(
            settings_id=identifier, revision=1 if previous is None else previous.revision + 1,
            policy_sha256=policy.sha256, selected_by=principal.oid, selected_at=self.runtime._now(),
            command=command, selected_source=None if note is None else note.value.source, request=request,
        )
        head_key = settings_head_key(policy.authority_domain, policy.agent_scope)

        def decide(rows):
            control._human(principal, "approve")
            current = control.installed(rows)
            if not current.active or not current.read_enabled or current != policy:
                raise Problem(403, "WORK_POLICY_REVOKED", "the original work scope changed")
            if any(rows[key] != observed[key] for key in keys):
                raise Problem(412, "MEMORY_SETTINGS_CONFLICT", "the setting, note or read authority changed")
            return ControlChanges(writes=(
                ControlWrite(row_id=identifier, mode="create", data=record.model_dump(mode="json"),
                             material=material(record.model_dump(mode="json"))),
                ControlWrite(row_id=head_key, mode="create" if previous is None else "replace",
                             data=MemoryBridgeSettingsHead(settings_id=identifier).model_dump(mode="json")),
            ), result=settings_result(record))
        return self.store.transact(request, decide)

    def reconcile(self, principal, command):
        def read():
            _, policy = self._authorize(principal)
            identifier = settings_key(policy.authority_domain, command.request_id, policy.agent_scope)
            if self.store.read_snapshot((identifier,))[identifier] is None:
                raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "the original memory setting is not confirmed")
            record = read_memory_settings(self.store, policy, identifier)
            if record.command != command:
                raise Problem(409, "MEMORY_SETTINGS_CONFLICT", "the original setting request has different content")
            return self.store.reconcile(record.request)
        return self.store.consistent_read(read)
