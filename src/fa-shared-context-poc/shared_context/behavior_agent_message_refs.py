"""Current read checks for explicitly shared, fixed-version Team references."""
import hashlib

from .behavior_agent_message_wire import PeerTeamReference, TeamReference
from .behavior_auth import BehaviorPrincipal
from .errors import Problem


def _references(values):
    if not isinstance(values, tuple) or len(values) > 8:
        raise Problem(422, "AGENT_MESSAGE_REFS_INVALID", "at most eight explicit Team references are required")
    try:
        if any(not isinstance(value, TeamReference) for value in values):
            raise ValueError("typed references required")
        refs = tuple(PeerTeamReference.model_validate_json(value.model_dump_json()) for value in values)
        if len(set(refs)) != len(refs):
            raise ValueError("distinct references required")
        for ref in refs:
            if ref.resource_kind == "source" and int(ref.version) > 2**31 - 1:
                raise ValueError("an exact supported source version is required")
        return refs
    except (ValueError, TypeError):
        raise Problem(422, "AGENT_MESSAGE_REFS_INVALID", "reference kind, version or Team identity is invalid") from None


def _gate(authority, binding, principal):
    if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "agent"
            or not principal.actor_id):
        raise Problem(403, "AGENT_MESSAGE_REFS_DENIED", "the original server-verified agent identity is required")
    current, policy, _ = authority.runtime.authorize(binding.binding_id, principal, "read")
    if current != binding:
        raise Problem(409, "AGENT_MESSAGE_REFS_CHANGED", "the original work binding changed")
    return policy


def _read_source(authority, binding, principal, policy, reference):
    if policy.source_visibility != "team":
        raise Problem(403, "AGENT_MESSAGE_REFS_DENIED", "private source scopes cannot be shared")
    source = authority.runtime.read_source(binding.binding_id, reference.resource_id, int(reference.version), principal)
    exact = source.event.reference
    if (exact.scope.visibility != "team" or (exact.scope.tenant_id, exact.scope.team_id) !=
            (reference.tenant_id, reference.team_id) or exact.source_id != reference.resource_id
            or str(exact.version) != reference.version or exact.sha256 != reference.sha256):
        raise Problem(409, "AGENT_MESSAGE_REFS_CHANGED", "the exact shared source differs")
    return source


def _read_artifact(authority, binding, principal, reference):
    actor = authority.actors.get(principal.oid)
    service = authority.artifacts
    if (actor is None or (actor.oid, actor.actor, actor.team) !=
            (principal.oid, principal.actor_id, binding.team_id)
            or service.context.auth.tenant_id != binding.tenant_id):
        raise Problem(403, "AGENT_MESSAGE_REFS_DENIED", "the original artifact reader identity is unavailable")
    version = service.version(actor, binding.team_id, reference.resource_id, reference.version)
    if version.sha256 != reference.sha256:
        raise Problem(409, "AGENT_MESSAGE_REFS_CHANGED", "the shared artifact version differs")
    observed, content = service.download(
        actor, binding.team_id, reference.resource_id, reference.version, version.blob.etag)
    if (observed != version or hashlib.sha256(content).hexdigest() != reference.sha256
            or service.version(actor, binding.team_id, reference.resource_id, reference.version) != version
            or authority.actors.get(principal.oid) != actor):
        raise Problem(409, "AGENT_MESSAGE_REFS_CHANGED", "the shared artifact changed during its current read")


def preflight_team_refs(*, sender_authority, sender_binding, sender_principal,
                        receiver_authority, receiver_policy, refs, allowed_refs):
    """Deny known-invalid input before registration; never substitute for the receiver's work gate."""
    references, allowed = _references(refs), _references(allowed_refs)
    receiver = receiver_policy.template
    scope = (sender_binding.tenant_id, sender_binding.team_id)
    policy = receiver_policy.legacy_template_policy
    if (scope != (receiver.tenant_id, receiver.team_id) or policy.source_visibility != "team"
            or any((ref.tenant_id, ref.team_id) != scope or ref not in allowed
                   or ref.resource_kind != "source" or ref.resource_id not in receiver.source_ids for ref in references)):
        raise Problem(403, "AGENT_MESSAGE_SHARE_DENIED", "the original receiver cannot read these root references")
    store = sender_authority.runtime.store
    if store is not receiver_authority.runtime.store:
        raise Problem(403, "AGENT_MESSAGE_REFS_DENIED", "the original shared authority is required")

    def read():
        sender_policy = _gate(sender_authority, sender_binding, sender_principal)
        observed = []
        for reference in references:
            source = _read_source(sender_authority, sender_binding, sender_principal, sender_policy, reference)
            if source.event.purpose not in policy.allowed_purposes:
                raise Problem(403, "AGENT_MESSAGE_REFS_DENIED", "the receiver policy does not allow the source purpose")
            receiver_authority.runtime._validate_source_output(source, policy)
            observed.append(source.event.expires_at)
        _gate(sender_authority, sender_binding, sender_principal)
        for expiry in observed:
            sender_authority.runtime.dep.sources._require_unexpired(expiry)
    return read() if store._read_view() is not None else store.consistent_read(read, max_keys=None)


def validate_team_refs(*, sender_authority, sender_binding, sender_principal,
                       receiver_authority, receiver_binding, receiver_principal, refs, allowed_refs):
    """No writes or cached grants; receiver identity must come from the trusted assignment."""
    references, allowed = _references(refs), _references(allowed_refs)
    scope = (sender_binding.tenant_id, sender_binding.team_id)
    if (scope != (receiver_binding.tenant_id, receiver_binding.team_id)
            or any((ref.tenant_id, ref.team_id) != scope for ref in (*references, *allowed))
            or any(ref not in allowed for ref in references)):
        raise Problem(403, "AGENT_MESSAGE_SHARE_DENIED", "only the root's fixed Team references may be shared")
    store = sender_authority.runtime.store
    if store is not receiver_authority.runtime.store:
        raise Problem(403, "AGENT_MESSAGE_REFS_DENIED", "peer read checks require the original shared authority")

    def read():
        sender_policy = _gate(sender_authority, sender_binding, sender_principal)
        receiver_policy = _gate(receiver_authority, receiver_binding, receiver_principal)
        expiries = []
        for reference in references:
            if reference.resource_kind == "source":
                for authority, binding, principal, policy in (
                    (sender_authority, sender_binding, sender_principal, sender_policy),
                    (receiver_authority, receiver_binding, receiver_principal, receiver_policy),
                ):
                    source = _read_source(authority, binding, principal, policy, reference)
                    expiries.append((authority.runtime.dep.sources, source.event.expires_at))
            else:
                _read_artifact(sender_authority, sender_binding, sender_principal, reference)
                _read_artifact(receiver_authority, receiver_binding, receiver_principal, reference)
        _gate(sender_authority, sender_binding, sender_principal)
        _gate(receiver_authority, receiver_binding, receiver_principal)
        for sources, expiry in expiries:
            sources._require_unexpired(expiry)
        return references

    return read() if store._read_view() is not None else store.consistent_read(read, max_keys=None)
