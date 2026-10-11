"""Verified native RID intake for M3's existing Team skill/approval path."""

import hashlib
import logging

from azure.core.exceptions import AzureError
from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .behavior_api import _body, _principal_dependency, _response
from .behavior_capabilities import skillify_granted
from .behavior_native_capability_wire import NATIVE_CAPABILITY_PREFIX, NativeTurnScope
from .behavior_native_turn_api import NativeTurnGateway
from .behavior_native_turn_wire import material_digest
from .behavior_skill_draft import SKILL_DRAFT_ERRORS, skill_draft_bytes, skill_draft_enabled, skill_purpose
from .errors import Problem
from .m3_models import ArtifactDocument, NativeArtifactSource, SkillReviewEntry, VersionInput
from .native_capability_api import NativeCapabilityGateway
from .native_skill_draft_wire import (
    LEGACY_DRAFT_REQUEST_BYTES, RESOURCE_DRAFT_REQUEST_BYTES, NativeSkillDraftIntentRead, NativeSkillDraftReceipt,
    NativeSkillDraftView, NativeSkillDraftWrite, artifact_id, draft_material, resource_manifest, resource_reference,
)

TITLE = "Unapproved skill draft from user procedure"
PREFIX = NATIVE_CAPABILITY_PREFIX + "/skill-draft"


def _unknown():
    return Problem(503, "SKILL_DRAFT_OUTCOME_UNKNOWN", "read the original draft; never change its IDs or resend unknown bytes")


class NativeSkillDraftGateway:
    def __init__(self, composition, *, enabled):
        self.composition, self.enabled = composition, enabled
        self.capabilities = NativeCapabilityGateway(composition, enabled=enabled)

    def _write_source(self, principal, command):
        composition, client, receipt = self.capabilities.current_turn(principal, command)
        capture = receipt.capture
        if (not receipt.human_origin or not skillify_granted(capture.capabilities)
                or not skill_draft_enabled(capture.template.role.profile, capture.capabilities)):
            raise Problem(403, "SKILL_DRAFT_DISABLED", "only the original human turn with captured skillify may write a draft")
        authority = composition.assistant.authority
        actor = authority.actors.get(principal.oid)
        if actor is None or actor.team != principal.team or actor.actor != principal.source_actor_id:
            raise Problem(403, "SKILL_DRAFT_SCOPE_DENIED", "the verified native Agent and Team must own the draft")
        proof, _ = NativeTurnGateway._invoke(client, NativeSkillDraftIntentRead(**command.model_dump()))
        if (proof.submit_sha256 != receipt.submit_digest
                or proof.input_digest != material_digest(draft_material(command.arguments))):
            raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the original RID effect names different draft material")
        source = NativeArtifactSource(
            tenant_id=composition.config.identity.tenant_id, team_id=actor.team, agent_id=receipt.agent_id,
            agent_oid=actor.oid, conversation_id=receipt.conversation_id, request_id=receipt.request_id,
            submit_sha256=receipt.submit_digest)
        return authority.artifacts, actor, source, proof

    @staticmethod
    def _target(service, actor, source):
        identifier = artifact_id(source.tenant_id, source.team_id, source.agent_id,
                                 source.conversation_id, source.request_id)
        try:
            record = service.get(actor, source.team_id, identifier)
        except Problem as error:
            if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
                return identifier, None, None
            raise
        document = ArtifactDocument.model_validate(record.payload)
        if (document.kind != "skill" or document.title != TITLE or document.native_source != source
                or document.owner_oid != source.agent_oid or len(document.versions) > 1):
            raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the original native draft slot has another source")
        return identifier, record, document

    @staticmethod
    def _receipt(service, actor, identifier, document, arguments=None):
        if document is None or not document.versions:
            raise _unknown()
        version = document.versions[0]
        if version.native_source != document.native_source or version.skill_purpose is None:
            raise Problem(409, "SKILL_DRAFT_RECEIPT_INVALID", "the original draft has no exact native source and purpose")
        _, data = service.download(actor, document.team, identifier, version.version_id, version.blob.etag)
        skill_draft_bytes(data.decode("utf-8"))
        if arguments is not None and (data != skill_draft_bytes(arguments.body)
                or version.skill_purpose != skill_purpose(arguments.purpose)
                or version.version_id != material_digest(draft_material(arguments))[:32]
                or version.skill_resources != resource_manifest(document.native_source, arguments)):
            raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the original RID already has different draft content")
        if version.skill_resources is not None:
            service._validate_resources(actor, document.team, version, verify_content=True,
                                        native_source=document.native_source)
        return NativeSkillDraftReceipt(
            schema_version=("fa-native-skill-draft-receipt-v1" if version.skill_resources is None else
                            "fa-native-skill-draft-receipt-v2"),
            native_source=document.native_source, artifact_id=identifier, version_id=version.version_id,
            sha256=version.sha256, byte_length=len(data), purpose=version.skill_purpose,
            metadata_sha256=version.metadata_sha256, resources=version.skill_resources), data

    @staticmethod
    def _resources(service, actor, source, arguments, *, save):
        for resource in arguments.resources:
            ref = resource_reference(source, resource)

            def current():
                try:
                    record = service.get(actor, source.team_id, ref.artifact_id)
                except Problem as error:
                    if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
                        return None, None
                    raise
                document = ArtifactDocument.model_validate(record.payload)
                if (document.kind != "deliverable" or document.native_source != source
                        or document.owner_oid != source.author_oid or len(document.versions) > 1):
                    raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the resource has another original source")
                return record, document

            record, document = current()
            if record is None:
                if not save:
                    raise _unknown()
                try:
                    service.create_native_resource(actor, source.team_id, ref.artifact_id,
                                                   title=resource.name, native_source=source)
                except (Problem, AzureError, OSError) as error:
                    if isinstance(error, Problem) and error.status < 500 and error.status not in {409, 412}:
                        raise
                    logging.getLogger(__name__).warning(
                        "NATIVE_SKILL_DRAFT_RESOURCE_UNCONFIRMED stage=create exception_type=%s", type(error).__name__)
                record, document = current()
                if record is None:
                    raise _unknown()
            raw = resource.text.encode("utf-8")
            pending = document.pending
            if pending is not None and (
                    pending.native_source != source or pending.version_id != ref.version_id
                    or pending.sha256 != ref.sha256 or pending.bytes != len(raw)
                    or pending.filename != resource.name or pending.media_type != resource.media_type):
                raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the resource pending upload has different material")
            if not document.versions:
                try:
                    if pending is not None:
                        service.reconcile(actor, source.team_id, ref.artifact_id, pending.version_id, record.etag)
                    elif save:
                        service.upload(actor, source.team_id, ref.artifact_id, ref.version_id, record.etag,
                            VersionInput(filename=resource.name, media_type=resource.media_type, sha256=ref.sha256),
                            raw, native_source=source)
                    else:
                        raise _unknown()
                except (Problem, AzureError, OSError) as error:
                    if (pending is None and isinstance(error, Problem)
                            and error.status < 500 and error.status not in {409, 412}):
                        raise
                    logging.getLogger(__name__).warning(
                        "NATIVE_SKILL_DRAFT_RESOURCE_UNCONFIRMED stage=content exception_type=%s", type(error).__name__)
                _, document = current()
                if document is None or not document.versions:
                    raise _unknown()
            version = document.versions[0]
            if (version.native_source != source or version.version_id != ref.version_id
                    or version.sha256 != ref.sha256 or version.blob.bytes != len(raw)
                    or version.filename != resource.name or version.media_type != resource.media_type):
                raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the original resource version differs")
            _, data = service.download(actor, source.team_id, ref.artifact_id, ref.version_id, version.blob.etag)
            if data != raw:
                raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the original resource bytes differ")

    def save(self, principal, command):
        command = NativeSkillDraftWrite.model_validate_json(command.model_dump_json())
        service, actor, source, proof = self._write_source(principal, command)
        identifier, record, document = self._target(service, actor, source)
        if proof.effect_status == "done":
            return self._receipt(service, actor, identifier, document, command.arguments)[0]
        if record is None:
            try:
                service.create_native_skill(actor, source.team_id, identifier, title=TITLE, native_source=source)
            except (Problem, AzureError, OSError) as error:
                if isinstance(error, Problem) and error.status < 500 and error.status not in {409, 412}:
                    raise
                logging.getLogger(__name__).warning("NATIVE_SKILL_DRAFT_CREATE_UNCONFIRMED exception_type=%s",
                                                    type(error).__name__)
            _, record, document = self._target(service, actor, source)
            if record is None:
                raise _unknown()
        if document.versions:
            return self._receipt(service, actor, identifier, document, command.arguments)[0]
        if document.pending is not None:
            return self._reconcile(service, actor, source, identifier, record, document, command.arguments)
        self._resources(service, actor, source, command.arguments, save=True)
        data = skill_draft_bytes(command.arguments.body)
        metadata = VersionInput(filename="skill-draft.md", media_type="text/markdown",
                                sha256=hashlib.sha256(data).hexdigest(), skill_purpose=skill_purpose(command.arguments.purpose),
                                skill_resources=resource_manifest(source, command.arguments))
        try:
            service.upload(actor, source.team_id, identifier, material_digest(draft_material(command.arguments))[:32],
                           record.etag, metadata, data, native_source=source)
        except (Problem, AzureError, OSError) as error:
            if isinstance(error, Problem) and error.status < 500 and error.status not in {409, 412}:
                raise
            logging.getLogger(__name__).warning("NATIVE_SKILL_DRAFT_UPLOAD_UNCONFIRMED exception_type=%s",
                                                type(error).__name__)
        _, _, document = self._target(service, actor, source)
        return self._receipt(service, actor, identifier, document, command.arguments)[0]

    @staticmethod
    def _reconcile(service, actor, source, identifier, record, document, arguments):
        pending = document.pending
        data = skill_draft_bytes(arguments.body)
        if (pending.native_source != source or pending.version_id != material_digest(draft_material(arguments))[:32]
                or pending.sha256 != hashlib.sha256(data).hexdigest() or pending.bytes != len(data)
                or pending.skill_purpose != skill_purpose(arguments.purpose)
                or pending.skill_resources != resource_manifest(source, arguments)):
            raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the original pending draft has different material")
        try:
            reconciled = service.reconcile(actor, source.team_id, identifier, pending.version_id, record.etag)
        except (Problem, AzureError, OSError) as error:
            logging.getLogger(__name__).warning("NATIVE_SKILL_DRAFT_RECONCILE_UNCONFIRMED exception_type=%s",
                                                type(error).__name__)
            raise _unknown() from None
        return NativeSkillDraftGateway._receipt(
            service, actor, identifier, ArtifactDocument.model_validate(reconciled.payload), arguments)[0]

    def reconcile(self, principal, command):
        command = NativeSkillDraftWrite.model_validate_json(command.model_dump_json())
        scope = self.capabilities.relay._sender_scope(principal)
        composition = self.capabilities.relay.owner.for_agent(scope.target.agent_id)
        authority = composition.assistant.authority
        actor = authority.actors.get(principal.oid)
        if actor is None or actor.team != scope.team_id or actor.actor != principal.source_actor_id:
            raise Problem(403, "SKILL_DRAFT_SCOPE_DENIED", "only the verified original Agent may reconcile its draft")
        service = authority.artifacts
        identifier = artifact_id(composition.config.identity.tenant_id, actor.team, scope.target.agent_id,
                                 command.conversation_id, command.request_id)
        try:
            original = service.get(actor, actor.team, identifier)
        except Problem as error:
            if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
                raise _unknown() from None
            raise
        source = ArtifactDocument.model_validate(original.payload).native_source
        if source is None or (
                source.tenant_id, source.team_id, source.agent_id, source.agent_oid,
                source.conversation_id, source.request_id) != (
                    composition.config.identity.tenant_id, actor.team, scope.target.agent_id, principal.oid,
                    command.conversation_id, command.request_id):
            raise Problem(409, "SKILL_DRAFT_TARGET_CONFLICT", "the retained draft has another original native source")
        identifier, record, document = self._target(service, actor, source)
        if document is None:
            raise _unknown()
        if document.versions:
            return self._receipt(service, actor, identifier, document, command.arguments)[0]
        self._resources(service, actor, source, command.arguments, save=False)
        if document.pending is None:
            raise _unknown()
        return self._reconcile(service, actor, source, identifier, record, document, command.arguments)

    def read(self, principal, command: NativeTurnScope):
        control, client = NativeTurnGateway(self.composition, enabled=self.enabled)._context(principal, "read", prepared=False)
        NativeTurnGateway._legacy(control, command.conversation_id)
        binding = control.policy.template
        service = self.composition.assistant.authority.artifacts
        actor = self.composition.assistant.authority.actors.get(binding.agent_oid)
        if actor is None or actor.team != binding.team_id:
            raise Problem(403, "SKILL_DRAFT_SCOPE_DENIED", "the original Agent's Team is required")
        identifier = artifact_id(binding.tenant_id, binding.team_id, client.target.agent_id,
                                 command.conversation_id, command.request_id)
        base = dict(agent_id=client.target.agent_id, conversation_id=command.conversation_id, request_id=command.request_id)
        try:
            record = service.get(actor, binding.team_id, identifier)
        except Problem as error:
            if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
                return NativeSkillDraftView(**base, state="not_recorded")
            raise
        document = ArtifactDocument.model_validate(record.payload)
        source = document.native_source
        if (source is None or document.kind != "skill" or (source.agent_id, source.conversation_id, source.request_id) != (
                client.target.agent_id, command.conversation_id, command.request_id)):
            raise Problem(409, "SKILL_DRAFT_RECEIPT_INVALID", "the original artifact has another native source")
        state = SkillReviewEntry.from_document(document).status
        if not document.versions:
            return NativeSkillDraftView(**base, state=state)
        receipt, data = self._receipt(service, actor, identifier, document)
        return NativeSkillDraftView(**base, state=state, receipt=receipt, body=data.decode("utf-8"))


def install_native_skill_draft_routes(app, composition, *, enabled):
    gateway = NativeSkillDraftGateway(composition, enabled=enabled)
    identity = _principal_dependency(composition.verifier)

    async def body(request):
        command = await _body(request, NativeSkillDraftWrite, max_body_bytes=RESOURCE_DRAFT_REQUEST_BYTES)
        if not command.arguments.resources and request.state.behavior_body_bytes > LEGACY_DRAFT_REQUEST_BYTES:
            raise Problem(413, "BEHAVIOR_BODY_LIMIT", "the original draft request exceeds 32 KiB")
        return command

    async def reply(call):
        try:
            result = await run_in_threadpool(call)
        except Problem as error:
            code = error.body["error"]["code"]
            if code in SKILL_DRAFT_ERRORS:
                return JSONResponse({"error": {"code": code, "message": "The original native draft was not confirmed."}},
                                    status_code=error.status, headers={"Cache-Control": "no-store"})
            raise
        response = _response(result)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post(PREFIX + "/save")
    async def save(request: Request, principal=Depends(identity)):
        command = await body(request)
        return await reply(lambda: gateway.save(principal, command))

    @app.post(PREFIX + "/reconcile")
    async def reconcile(request: Request, principal=Depends(identity)):
        command = await body(request)
        return await reply(lambda: gateway.reconcile(principal, command))

    @app.get("/work-policy/v1/native-turns/conversations/{conversation_id}/requests/{request_id}/skill-draft")
    async def read(conversation_id: str, request_id: str, request: Request, principal=Depends(identity)):
        if request.query_params or await request.body():
            raise Problem(422, "NATIVE_REQUEST_INVALID", "the draft read takes only its original CID and RID")
        try:
            command = NativeTurnScope(conversation_id=conversation_id, request_id=request_id)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original CID and RID are required") from None
        return await reply(lambda: gateway.read(principal, command))
