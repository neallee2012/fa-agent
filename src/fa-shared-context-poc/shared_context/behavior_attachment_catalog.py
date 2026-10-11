"""Source-reference choices only; listing never loads content or grants read permission."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

from .behavior_wire import ExactSourceRef, Identifier, WireModel
from .behavior_operations import control_key
from .errors import Problem
from .source_materials import SourceEvent, SourceHead, control_row


class AttachmentSourceChoice(WireModel):
    source_id: Identifier
    state: Literal["recorded", "missing", "withdrawn", "expired", "unsupported"]
    reference: ExactSourceRef | None = None
    media_type: Literal["text/plain", "text/markdown", "application/json"] | None = None

    @model_validator(mode="after")
    def metadata_matches(self):
        if self.state == "recorded":
            if self.reference is None or self.reference.source_id != self.source_id or self.media_type is None:
                raise ValueError("Recorded source choices require a matching exact reference and media type")
        elif self.reference is not None or self.media_type is not None:
            raise ValueError("Unavailable sources cannot masquerade as attachable references")
        return self


class AttachmentSourceCatalog(WireModel):
    schema_version: Literal["fa-attachment-source-catalog-v1"] = "fa-attachment-source-catalog-v1"
    state: Literal["disabled", "unsupported-scope", "metadata"]
    read_at: AwareDatetime
    conversation_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    entries: Annotated[tuple[AttachmentSourceChoice, ...], Field(max_length=16)] = ()
    authorization: Literal[False] = False
    content_reverified: Literal[False] = False
    dependencies_reverified: Literal[False] = False

    @model_validator(mode="after")
    def bounded_choices(self):
        names = tuple(item.source_id for item in self.entries)
        if names != tuple(sorted(set(names))) or self.state != "metadata" and self.entries:
            raise ValueError("Source choices must be a distinct ordered metadata-only set")
        return self


def conversation_input_scope(control, principal, policy, conversation_id):
    """Resolve the original input scope inside the caller's consistent read."""
    from .behavior_instruction import Conversation, conversation_key
    from .behavior_delivery import _parse
    try:
        TypeAdapter(Identifier).validate_python(conversation_id, strict=True)
    except ValueError:
        raise Problem(422, "ATTACHMENT_FORMAT", "one original conversation ID is required") from None
    binding = policy.template
    key = conversation_key(conversation_id)
    row = control.store.read_snapshot((key,))[key]
    if row is None:
        raise Problem(404, "CONVERSATION_NOT_FOUND", "the original conversation is unavailable")
    conversation = _parse(row.payload["data"], Conversation)
    if (conversation.conversation_id != conversation_id
            or (conversation.human_oid, conversation.team_id, conversation.agent_oid)
            != (binding.human_oid, binding.team_id, binding.agent_oid)):
        raise Problem(403, "CONVERSATION_SCOPE_DENIED", "this conversation belongs to another original scope")
    if conversation.reset_request_id is not None:
        raise Problem(409, "CONVERSATION_ARCHIVED", "an archived conversation cannot accept new attachments")
    if conversation.hosted_session_id is None:
        raise Problem(409, "CONVERSATION_SESSION_RESET_REQUIRED", "the original conversation needs an explicit reset")
    if not conversation.jobs:
        raise Problem(404, "CONVERSATION_NOT_FOUND", "the original conversation has no registered instruction")
    record = control._job_record(conversation.jobs[-1])
    if record is None:
        raise Problem(404, "CONVERSATION_NOT_FOUND", "the original conversation registration is unavailable")
    binding, source_policy, _ = control.runtime.authorize(record.binding.binding_id, principal, "read")
    record = control.records[binding.binding_id]
    task = record.profile.instruction
    if (record.binding.job_id != conversation.jobs[-1] or task is None
            or task.conversation_id != conversation_id or task.turn_number != len(conversation.jobs)):
        raise Problem(409, "CONVERSATION_CONTEXT_CHANGED", "the original conversation registration differs")
    return binding, source_policy, record.profile


def read_attachment_catalog(control, principal, *, conversation_id=None, native_template=None):
    from .behavior_profile_selection import current_selection, profile_from_template
    from .behavior_ingress import METRICS_SCHEMA, TEXT_SCHEMA
    if native_template is not None:
        from .behavior_agent_template import AgentTemplate
        if not isinstance(native_template, AgentTemplate) or conversation_id is None:
            raise Problem(422, "ATTACHMENT_FORMAT", "a server-read native template and its conversation are required")
    if conversation_id is not None and native_template is None:
        control._human(principal, "read")
        control.refresh()

    def read():
        control._human(principal, "read")
        policy = control.installed()
        if not policy.active or not policy.read_enabled:
            raise Problem(403, "WORK_POLICY_REVOKED", "current standing read scope is required")
        binding = policy.template
        source_policy = policy.legacy_template_policy
        if native_template is not None:
            attachment_profile = native_template.settings.text_attachment_profile
        elif conversation_id is None:
            choice = current_selection(control.store, policy)
            profile = policy.profile if choice is None else profile_from_template(policy, choice.command.template)
            attachment_profile = profile.text_attachment_profile
        else:
            binding, source_policy, profile = conversation_input_scope(control, principal, policy, conversation_id)
            attachment_profile = profile.text_attachment_profile
        at = control.runtime._now()
        if attachment_profile is None:
            return AttachmentSourceCatalog(state="disabled", read_at=at, conversation_id=conversation_id)
        if source_policy.source_visibility != "team":
            return AttachmentSourceCatalog(state="unsupported-scope", read_at=at, conversation_id=conversation_id)
        context = control.runtime._source_context(binding, source_policy, principal)
        scope = context.scope("team")
        keys = tuple(control_key("source-head", scope.key, source_id) for source_id in binding.source_ids)
        heads = control.store.read_snapshot(keys)
        entries = []
        for source_id in sorted(binding.source_ids):
            key = control_key("source-head", scope.key, source_id)
            if heads[key] is None:
                entries.append(AttachmentSourceChoice(source_id=source_id, state="missing"))
                continue
            head = control_row(heads, key, SourceHead)
            reference = head.reference
            if reference.scope != scope or reference.source_id != source_id:
                raise Problem(409, "ATTACHMENT_SOURCE_CHANGED", "source catalog metadata escaped the original scope")
            if head.access_state != "active":
                entries.append(AttachmentSourceChoice(source_id=source_id, state="withdrawn"))
                continue
            rows = control.store.read_snapshot((reference.key,))
            if rows[reference.key] is None:
                entries.append(AttachmentSourceChoice(source_id=source_id, state="missing"))
                continue
            event = control_row(rows, reference.key, SourceEvent)
            if event.reference != reference:
                raise Problem(409, "ATTACHMENT_SOURCE_CHANGED", "source metadata differs from its recorded head")
            control.runtime.dep.sources.verify_receipt(rows[reference.key], event.request, reference.key)
            if event.expires_at is not None and event.expires_at <= at:
                entries.append(AttachmentSourceChoice(source_id=source_id, state="expired"))
                continue
            if (event.purpose not in context.allowed_purposes
                    or event.media_type not in {"text/plain", "text/markdown", "application/json"}
                    or event.schema_id not in {METRICS_SCHEMA, TEXT_SCHEMA}
                    or event.schema_id == TEXT_SCHEMA and not source_policy.human_text_enabled
                    or not 0 < reference.byte_length <= 8192):
                entries.append(AttachmentSourceChoice(source_id=source_id, state="unsupported"))
                continue
            entries.append(AttachmentSourceChoice(source_id=source_id, state="recorded",
                reference=ExactSourceRef(source_id=source_id, version=reference.version,
                                         sha256=reference.sha256, byte_length=reference.byte_length),
                media_type=event.media_type))
        control._human(principal, "read")
        return AttachmentSourceCatalog(state="metadata", read_at=at, conversation_id=conversation_id, entries=tuple(entries))
    from .behavior_assistant import _read_conflicts
    return _read_conflicts(lambda: control.store.consistent_read(read), phase="attachment_catalog")
