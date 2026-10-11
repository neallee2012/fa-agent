"""Adapter from the original reviewed-context authority to bounded instruction data."""

import hashlib

from .behavior_store import ControlChanges
from .behavior_wire import ContextRequest, ExactSourceRef
from .behavior_work_memory import WorkMemoryApplicability, WorkMemoryCatalog, WorkMemoryOriginal, WorkMemoryRead, WorkMemorySource
from .behavior_work_memory_prompt import WORK_MEMORY_PROFILE
from .behavior_workers import worker_key
from .errors import Problem
from .source_materials import digest


def _scope(authority, session, principal):
    binding, policy, _ = authority.runtime.authorize(session.binding_id, principal, "read")
    profile = authority.runtime.profiles[session.binding_id]
    task = session.assignment.task
    if (task.instruction is None or task.instruction.prompt_profile != WORK_MEMORY_PROFILE
            or profile.prompt_profile != WORK_MEMORY_PROFILE or session.state != "running"
            or task.profile_digest != digest(profile.model_dump(mode="json"))):
        raise Problem(403, "WORK_MEMORY_READ_DISABLED", "only the original running profile enables this reviewed note")
    if policy.source_visibility != "team" or binding.claim_ids != (profile.claim_id,):
        raise Problem(403, "WORK_MEMORY_SCOPE_DENIED", "this capability supports only the single assigned Team claim")
    authority._gate(session, authority.store.read_snapshot(authority._keys(session)))
    return binding, policy, profile


def load(authority, session, principal):
    binding, policy, profile = _scope(authority, session, principal)
    gates = authority.store.read_snapshot(authority._keys(session))
    # Job observations are live execution state, not knowledge identity. _scope
    # rechecks the job before returning; _command keeps it in the atomic read set.
    gates = {key: value for key, value in gates.items() if key != worker_key("job", binding.job_id)}
    try:
        _, _, snapshot = authority._reviewed(session, principal)
    except Problem as error:
        if error.body["error"]["code"] != "BEHAVIOR_READ_SCOPE_LIMIT":
            raise
        raise Problem(413, "WORK_MEMORY_SOURCE_LIMIT", "complete reviewed-memory receipts exceed their read bound") from None
    snapshot = {**gates, **snapshot}
    context = authority.runtime.context(session.binding_id, ContextRequest(
        request_id="work-memory-context", required_claim_ids=(profile.claim_id,)), principal)
    claims = context.claims
    if (len(claims) != 1 or not claims[0].human_reviewed
            or claims[0].reference.claim_id != profile.claim_id
            or context.manifest.subject != binding.subject or context.manifest.scope.visibility != "team"
            or context.manifest.scope.team_id != binding.team_id
            or context.manifest.scope.tenant_id != binding.tenant_id
            or claims[0].candidate.purpose != policy.purpose
            or any(ref.scope != context.manifest.scope or ref.source_id not in binding.source_ids
                   for ref in context.manifest.sources)):
        raise Problem(403, "WORK_MEMORY_SCOPE_DENIED", "reviewed material does not match this assigned Team knowledge view")
    claim = claims[0]
    if claim.review.authority_record_sha256 is None or context.summary is not None:
        raise Problem(409, "WORK_MEMORY_NOT_AVAILABLE", "an exact durable human-reviewed claim is required, not a projection")
    refs = tuple(sorted(context.manifest.sources, key=lambda ref: (ref.source_id, ref.version)))
    try:
        originals = tuple(context.originals[reference.key].decode("utf-8") for reference in refs)
    except (KeyError, UnicodeError):
        raise Problem(502, "WORK_MEMORY_SOURCE_INVALID", "a necessary verified original is missing or not valid UTF-8") from None
    try:
        source = WorkMemorySource(
            schema_version="fa-work-memory-source-v1", team_id=binding.team_id, subject=binding.subject,
            purpose=policy.purpose, claim_id=profile.claim_id, claim_version=claim.reference.version,
            claim_sha256=claim.reference.sha256, text_sha256=hashlib.sha256(claim.candidate.text.encode("utf-8")).hexdigest(),
            text_utf8_bytes=len(claim.candidate.text.encode("utf-8")),
            review_sha256=claim.review.authority_record_sha256, review_policy_version=claim.review.policy_version,
            manifest_sha256=context.manifest.sha256,
            sources=tuple(ExactSourceRef(**ref.model_dump(exclude={"scope"})) for ref in refs),
        )
        result = WorkMemoryRead(
            schema_version="fa-work-memory-read-v1", source=source, text=claim.candidate.text,
            applicability=WorkMemoryApplicability(kind=claim.candidate.kind,
                effective_from=claim.candidate.effective_from, expires_at=claim.candidate.expires_at),
            originals=tuple(WorkMemoryOriginal(
                source=metadata, text=text,
            ) for text, metadata in zip(originals, source.sources, strict=True)),
        )
    except ValueError as error:
        raise Problem(413, "WORK_MEMORY_SOURCE_LIMIT", "complete reviewed text, provenance and originals cannot fit the read contract") from error
    authority._current_sources(authority.store.read_snapshot(tuple(snapshot)), snapshot)
    if _scope(authority, session, principal) != (binding, policy, profile):
        raise Problem(409, "WORK_MEMORY_SOURCE_CHANGED", "current read policy changed while assembling reviewed knowledge")
    if len(set((*authority._keys(session), *snapshot))) > 96:
        raise Problem(413, "WORK_MEMORY_SOURCE_LIMIT", "complete reviewed-memory predicates exceed the supported effect read set")
    return result, snapshot


def current_sources(authority, session, principal):
    result, snapshot = load(authority, session, principal)
    if session.work_memory_source is None or session.work_memory_source != result.source:
        raise Problem(403, "WORK_MEMORY_SOURCE_CHANGED", "retained reviewed memory is not the current exact claim and sources")
    return snapshot


def catalog(authority, session, command, principal):
    from .behavior_assistant import _Session, _updated, _write, key
    from .behavior_operations import _row

    result, snapshot = load(authority, session, principal)
    if session.work_memory_source is not None and session.work_memory_source != result.source:
        raise Problem(403, "WORK_MEMORY_SOURCE_CHANGED", "original memory provenance cannot adopt a later claim or source")
    if session.work_memory_source is None:
        sid = session.assignment.session_id
        def decide(rows):
            authority._current_sources(rows, snapshot)
            current = _row(rows, key("session", sid), _Session)
            if current.work_memory_source is not None and current.work_memory_source != result.source:
                raise Problem(403, "WORK_MEMORY_SOURCE_CHANGED", "original memory provenance changed before capture")
            return ControlChanges(writes=(_write(key("session", sid),
                _updated(current, work_memory_source=result.source), "replace"),), result={"state": "accepted"})
        authority._command(session, "work-memory", command, decide, extra=tuple(snapshot))
    again, _ = load(authority, session, principal)
    if again.source != result.source:
        raise Problem(403, "WORK_MEMORY_SOURCE_CHANGED", "reviewed memory changed before returning its metadata")
    return WorkMemoryCatalog(schema_version="fa-work-memory-catalog-v1", source=result.source)


def read(authority, session, command, principal):
    _, _, profile = _scope(authority, session, principal)
    if command.claim_id != profile.claim_id:
        raise Problem(403, "WORK_MEMORY_SCOPE_DENIED", "only the server-selected claim alias is available")
    if session.work_memory_source is None:
        raise Problem(409, "WORK_MEMORY_NOT_AVAILABLE", "original source metadata has not been captured")
    result, _ = load(authority, session, principal)
    if result.source != session.work_memory_source:
        raise Problem(403, "WORK_MEMORY_SOURCE_CHANGED", "original reviewed memory changed; no substitute is authorized")
    return result
