"""Exact reviewed-note resolution for an already verified destination scope."""

from dataclasses import dataclass
import hashlib

from .behavior_operations import control_key
from .behavior_wire import ContextRequest, ExactSourceRef
from .behavior_work_memory import WorkMemoryApplicability, WorkMemoryCatalog, WorkMemoryOriginal, WorkMemoryRead, WorkMemorySource
from .behavior_workers import worker_key
from .errors import Problem
from .source_materials import digest


@dataclass(frozen=True)
class ResolvedNote:
    origin_binding_id: str
    value: WorkMemoryRead
    predicates: dict


def resolve_reviewed_note(runtime, principal, origin_binding_id, claim_id, destination, purpose):
    """Call inside one existing consistent-read scope; never copy or grant source authority."""
    binding, policy, _ = runtime.authorize(origin_binding_id, principal, "read")
    if (binding.tenant_id != destination.tenant_id or binding.team_id != destination.team_id
            or binding.agent_oid != destination.agent_oid or binding.human_oid != destination.human_oid
            or binding.subject != destination.subject or policy.source_visibility != "team"
            or policy.purpose != purpose or claim_id not in binding.claim_ids):
        raise Problem(403, "MEMORY_RECALL_SCOPE_DENIED", "the original note is outside this human, agent or work purpose")
    context = runtime.context(origin_binding_id, ContextRequest(
        request_id="selected-reviewed-note", required_claim_ids=(claim_id,)), principal)
    if len(context.claims) != 1 or context.summary is not None:
        raise Problem(409, "MEMORY_RECALL_UNAVAILABLE", "one exact reviewed note is required")
    claim = context.claims[0]
    scope = claim.reference.scope
    if (not claim.human_reviewed or scope.visibility != "team"
            or claim.candidate.purpose != purpose or claim.reference.claim_id != claim_id
            or claim.reference.subject != binding.subject or claim.review.authority_record_sha256 is None
            or any(source.scope != scope or source.source_id not in binding.source_ids
                   for source in context.manifest.sources)):
        raise Problem(403, "MEMORY_RECALL_SCOPE_DENIED", "only the original human-reviewed Team note is supported")
    references = tuple(sorted(context.manifest.sources, key=lambda ref: (ref.source_id, ref.version)))
    try:
        source = WorkMemorySource(
            schema_version="fa-work-memory-source-v1", team_id=binding.team_id, subject=binding.subject,
            purpose=purpose, claim_id=claim_id, claim_version=claim.reference.version,
            claim_sha256=claim.reference.sha256,
            text_sha256=hashlib.sha256(claim.candidate.text.encode("utf-8")).hexdigest(),
            text_utf8_bytes=len(claim.candidate.text.encode("utf-8")),
            review_sha256=claim.review.authority_record_sha256,
            review_policy_version=claim.review.policy_version, manifest_sha256=context.manifest.sha256,
            sources=tuple(ExactSourceRef(**ref.model_dump(exclude={"scope"})) for ref in references),
        )
        result = WorkMemoryRead(
            schema_version="fa-work-memory-read-v1", source=source, text=claim.candidate.text,
            applicability=WorkMemoryApplicability(kind=claim.candidate.kind,
                effective_from=claim.candidate.effective_from, expires_at=claim.candidate.expires_at),
            originals=tuple(WorkMemoryOriginal(source=metadata, text=context.originals[ref.key].decode("utf-8"))
                            for ref, metadata in zip(references, source.sources, strict=True)),
        )
    except (ValueError, KeyError, UnicodeError):
        raise Problem(409, "MEMORY_RECALL_UNAVAILABLE", "the complete reviewed note and its originals are unavailable") from None
    source_context = runtime._source_context(binding, policy, principal)
    keys = tuple(dict.fromkeys((
        *runtime._gate_keys(binding),
        control_key("memory-subject", scope.key, binding.subject),
        claim.reference.key,
        *(key for reference in references for key in (reference.key, reference.head_key)),
        *runtime.dep.memory._review_keys(source_context, claim.candidate, claim.review),
    )))
    return ResolvedNote(origin_binding_id, result, dict(runtime.store.read_snapshot(keys)))


def read_captured_note(runtime, principal, origin_binding_id, captured_source, destination, purpose):
    result = resolve_reviewed_note(
        runtime, principal, origin_binding_id, captured_source.claim_id, destination, purpose)
    if result.value.source != captured_source:
        raise Problem(409, "MEMORY_RECALL_CHANGED", "the captured note is no longer current; no historical text was returned")
    return result


def load_session_note(authority, session, principal):
    from .behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
    from .behavior_reviewed_note_prompt import REVIEWED_NOTE_PROFILE
    from .memory_bridge_settings import validate_profile_memory

    def read():
        runtime = authority.runtime
        binding, policy, _ = runtime.authorize(session.binding_id, principal, "read")
        profile = runtime.profiles[session.binding_id]
        task = session.assignment.task
        control = runtime.work_policy
        if (control is None or task.instruction is None or task.instruction.prompt_profile != REVIEWED_NOTE_PROFILE
                or profile.prompt_profile != REVIEWED_NOTE_PROFILE
                or task.profile_digest != digest(profile.model_dump(mode="json"))
                or session.state != "running" or principal.kind != "agent" or principal.oid != binding.agent_oid):
            raise Problem(403, "MEMORY_RECALL_DISABLED", "only the original running memory-enabled profile may read")
        record = control.records.get(binding.binding_id)
        choice = None if record is None else control.profile_selections.get(record.profile_selection_id)
        if (record is None or record.binding != binding or record.profile != profile
                or choice is None or choice.command.template.role.profile != HISTORY_SKILL_DRAFT_PROFILE):
            raise Problem(403, "MEMORY_RECALL_SCOPE_DENIED", "the original human-selected history/draft profile is required")
        setting = validate_profile_memory(control, profile, binding)
        snapshot = dict(authority.store.read_snapshot(tuple(dict.fromkeys((*authority._keys(session), setting.settings_id)))))
        authority._gate(session, snapshot)
        # The atomic command still gates the current job; observer updates are not note changes.
        snapshot.pop(worker_key("job", binding.job_id))
        captured = profile.memory_scope.recall
        result = read_captured_note(runtime, principal, captured.origin_binding_id, captured.source, binding, policy.purpose)
        snapshot.update(result.predicates)
        return ResolvedNote(result.origin_binding_id, result.value, snapshot)
    return authority.store.consistent_read(read)


def catalog(authority, session, command, principal):
    if command.request_id != "current-reviewed-note":
        raise Problem(422, "ASSISTANT_COMMAND_ID", "the original reviewed-note catalog ID is required")
    return WorkMemoryCatalog(schema_version="fa-work-memory-catalog-v1",
                             source=load_session_note(authority, session, principal).value.source)


def read(authority, session, command, principal):
    if command.request_id != "read-reviewed-note":
        raise Problem(422, "ASSISTANT_COMMAND_ID", "the original reviewed-note read ID is required")
    return load_session_note(authority, session, principal).value


def current_sources(authority, session, principal):
    return load_session_note(authority, session, principal).predicates
