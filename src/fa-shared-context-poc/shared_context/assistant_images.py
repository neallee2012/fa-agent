"""Screenshots an assistant delivers with one reply, kept as immutable PNG versions of one M3 deliverable.

Each work (job) owns one deterministic deliverable; each screenshot is a version whose ID is derived from its
bytes, so delivering the same screenshot again never adds a copy. Reads go through the original work's read
authority; nothing here creates a public URL.
"""

import logging

from .assistant_image_wire import (
    ENTRY_ID, MAX_TURN_IMAGES, TITLE, DeliverImageCommand, ImageEntry, ImageReceipt, TurnImages,
)
from .behavior_capabilities import BUILTIN_CAPABILITIES, runtime_grants
from .behavior_operations import control_key
from .errors import Problem
from .m3_models import ArtifactDocument, CreateArtifact, VersionInput

IMAGE_CAPABILITY = BUILTIN_CAPABILITIES[ENTRY_ID]


def image_artifact_id(job_id):
    return control_key("assistant-turn-images", job_id)


def _unknown():
    return Problem(503, "IMAGE_DELIVERY_UNKNOWN",
                   "the original screenshot delivery is unresolved; the same screenshot may be delivered again")


def _write_scope(authority, session, principal):
    binding, _, _ = authority.runtime.authorize(session.binding_id, principal, "execute")
    task = session.assignment.task.instruction
    grants = () if task is None or task.capabilities is None else task.capabilities.grants
    grant = next((item for item in grants if item.entry_id == ENTRY_ID), None)
    if (principal.kind != "agent" or principal.oid != binding.agent_oid or session.state != "running"
            or grant is None or grant.kind != "fa-builtin" or grant.entry_sha256 != IMAGE_CAPABILITY.entry_sha256
            or grant.tools != IMAGE_CAPABILITY.tools or grant.vision
            or runtime_grants(authority.runtime).get(ENTRY_ID) != grant):
        raise Problem(403, "IMAGE_DELIVERY_DISABLED", "this original turn has no current screenshot delivery capability")
    authority._gate(session, authority.store.read_snapshot(authority._keys(session)))
    actor = authority.actors.get(binding.agent_oid)
    if actor is None or actor.team != binding.team_id or actor.actor != principal.source_actor_id:
        raise Problem(403, "IMAGE_DELIVERY_DISABLED", "the actual verified server-bound owning Actor is required")
    return binding, actor, authority.runtime.profiles[session.binding_id].legacy_source_work


def _document(authority, actor, team, artifact, source_work=None):
    try:
        record = authority.artifacts.get(actor, team, artifact)
    except Problem as error:
        if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
            return None
        raise
    document = ArtifactDocument.model_validate(record.payload)
    if (document.kind != "deliverable" or document.title != TITLE or document.owner_oid != actor.oid
            or document.owner != actor.actor or document.effective_version_id is not None
            or (source_work is not None and document.source_work != source_work)
            or len(document.versions) > MAX_TURN_IMAGES
            or any(version.media_type != "image/png" for version in document.versions)):
        raise Problem(409, "IMAGE_DELIVERY_CONFLICT", "the screenshot slot of this work differs from its original shape")
    return record, document


def _entry(version):
    return ImageEntry(version_id=version.version_id, number=version.number, sha256=version.sha256,
                      byte_length=version.blob.bytes)


def deliver_image(authority, session, command, principal):
    command = DeliverImageCommand.model_validate_json(command.model_dump_json())
    data, sha = command.data, command.sha256
    binding, actor, source_work = _write_scope(authority, session, principal)
    team, artifact, version_id = binding.team_id, image_artifact_id(binding.job_id), sha[:32]

    def receipt(document):
        version = next((item for item in document.versions if item.version_id == version_id), None)
        if version is None:
            return None
        if version.sha256 != sha or version.blob.bytes != len(data) or version.source_work != source_work:
            raise Problem(409, "IMAGE_DELIVERY_CONFLICT", "a different screenshot already holds this version")
        return ImageReceipt(artifact_id=artifact, **_entry(version).model_dump())

    current = _document(authority, actor, team, artifact, source_work)
    if current is None:
        try:
            authority.artifacts.create(actor, team, artifact,
                                       CreateArtifact(kind="deliverable", title=TITLE, source_work=source_work))
        except Exception:
            logging.getLogger(__name__).warning("IMAGE_DELIVERY_CREATE_UNKNOWN read_before_continue=true")
        current = _document(authority, actor, team, artifact, source_work)
        if current is None:
            raise _unknown()
    record, document = current
    done = receipt(document)
    if done is not None:
        return done
    if document.pending is not None:
        # Only this work's own earlier upload can be pending; finish it from its stored blob, never resend it.
        try:
            record = authority.artifacts.reconcile(actor, team, artifact, document.pending.version_id, record.etag)
        except Exception:
            logging.getLogger(__name__).warning("IMAGE_DELIVERY_RECONCILE_UNKNOWN original_version_retained=true")
            raise _unknown() from None
        document = ArtifactDocument.model_validate(record.payload)
        done = receipt(document)
        if done is not None:
            return done
    if len(document.versions) >= MAX_TURN_IMAGES:
        raise Problem(409, "IMAGE_DELIVERY_LIMIT", "this reply already has its maximum number of screenshots")
    metadata = VersionInput(filename=f"screenshot-{version_id[:12]}.png", media_type="image/png",
                            sha256=sha, source_work=source_work)
    try:
        record = authority.artifacts.upload(actor, team, artifact, version_id, record.etag, metadata, data)
        document = ArtifactDocument.model_validate(record.payload)
    except Exception:
        logging.getLogger(__name__).warning("IMAGE_DELIVERY_UPLOAD_UNKNOWN read_before_continue=true")
        current = _document(authority, actor, team, artifact, source_work)
        if current is None:
            raise _unknown() from None
        document = current[1]
    done = receipt(document)
    if done is None:
        raise _unknown()
    return done


def _read_scope(authority, binding_id, principal):
    control = authority.runtime.work_policy
    if control is None:
        raise Problem(403, "IMAGE_DELIVERY_DISABLED", "the original instruction policy is required")
    control._human(principal)
    binding, _, _ = authority.runtime.authorize(binding_id, principal, "read")
    actor = authority.actors.get(binding.agent_oid)
    if actor is None or actor.team != binding.team_id:
        raise Problem(403, "IMAGE_DELIVERY_DISABLED", "the original server-bound Actor is required")
    return binding, actor


def turn_images(authority, binding_id, principal):
    binding, actor = _read_scope(authority, binding_id, principal)
    current = _document(authority, actor, binding.team_id, image_artifact_id(binding.job_id))
    entries = () if current is None else tuple(_entry(version) for version in current[1].versions)
    return TurnImages(binding_id=binding.binding_id, job_id=binding.job_id, entries=entries)


def image_bytes(authority, binding_id, version_id, principal):
    binding, actor = _read_scope(authority, binding_id, principal)
    artifact = image_artifact_id(binding.job_id)
    if _document(authority, actor, binding.team_id, artifact) is None:
        raise Problem(404, "M3_VERSION_NOT_FOUND", "no delivered screenshot has that ID")
    version = authority.artifacts.version(actor, binding.team_id, artifact, version_id)
    version, data = authority.artifacts.download(actor, binding.team_id, artifact, version_id, version.blob.etag)
    return _entry(version), data
