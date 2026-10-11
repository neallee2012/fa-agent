"""C0 skillify provider: original effect once, M3 remains draft and approval authority."""

import hashlib
import json

from .behavior_capabilities import BUILTIN_CAPABILITIES, CAPABILITY_TOOL_GUIDANCE, SKILLIFY_ENTRY_ID
from .behavior_native_capability_wire import NativeTurnScope
from .behavior_native_turn_wire import material_digest
from .behavior_skill_draft import skill_draft_bytes, skill_draft_enabled, skill_purpose
from .errors import Problem
from .m2_copilot import ToolFailure, ToolOutput, ToolSpec
from .native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider
from .native_capability_tools import NativeCapabilityClient
from .native_effects import NativeEffects
from .native_skill_draft_wire import (
    SLOT, NativeSkillDraftArgs, NativeSkillDraftIntentRead, NativeSkillDraftProof, NativeSkillDraftReceipt,
    NativeSkillDraftWrite, artifact_id, draft_material, resource_manifest,
)

GUIDANCE = {
    "NATIVE_SKILL_DRAFT_DISABLED": "Only the original human turn with skillify may save a draft; no draft was approved.",
    "NATIVE_SKILL_DRAFT_UNKNOWN": "The original draft is unconfirmed. Read the same draft; never change IDs or resend unknown bytes.",
    "NATIVE_SKILL_DRAFT_CONFLICT": "This original turn already names another draft. Keep the original; do not replace it.",
}
CAPABILITY_TOOL_GUIDANCE.update(GUIDANCE)


async def read_intent(runtime, command: NativeSkillDraftIntentRead):
    receipt = await runtime.read(command.request_id)
    if (receipt.conversation_id != command.conversation_id or not receipt.human_origin
            or receipt.status != "started" or receipt.cancel_requested
            or not skill_draft_enabled(receipt.capture.template.role.profile, receipt.capture.capabilities)):
        raise Problem(403, "SKILL_DRAFT_SCOPE_DENIED", "the original human draft-writing turn is required")
    try:
        record = await NativeEffects(runtime, receipt).read(SLOT, draft_material(command.arguments))
    except Problem as error:
        code = ("SKILL_DRAFT_TARGET_CONFLICT" if error.body["error"]["code"] == "NATIVE_EFFECT_CONFLICT"
                else "SKILL_DRAFT_OUTCOME_UNKNOWN")
        raise Problem(409 if code == "SKILL_DRAFT_TARGET_CONFLICT" else 503, code,
                      "the original native draft effect could not be verified") from None
    if record is None:
        raise Problem(409, "SKILL_DRAFT_SCOPE_DENIED", "the original draft effect was not reserved")
    return NativeSkillDraftProof(
        agent_id=receipt.agent_id, conversation_id=receipt.conversation_id, request_id=receipt.request_id,
        submit_sha256=receipt.submit_digest, input_digest=record.input_digest, effect_status=record.status)


class NativeSkillDraftClient(NativeCapabilityClient):
    async def save(self, scope, arguments, *, reconcile=False):
        command = NativeSkillDraftWrite(**scope.model_dump(), arguments=arguments)
        return await self._post("/skill-draft/" + ("reconcile" if reconcile else "save"),
                                command, NativeSkillDraftReceipt)


def _matches(receipt, original, arguments):
    source = receipt.native_source
    if ((source.agent_id, source.conversation_id, source.request_id, source.submit_sha256) != (
            original.agent_id, original.conversation_id, original.request_id, original.submit_digest)
            or receipt.sha256 != hashlib.sha256(skill_draft_bytes(arguments.body)).hexdigest()
            or receipt.byte_length != len(skill_draft_bytes(arguments.body))
            or receipt.purpose != skill_purpose(arguments.purpose)
            or receipt.resources != resource_manifest(source, arguments)
            or receipt.version_id != material_digest(draft_material(arguments))[:32]
            or receipt.artifact_id != artifact_id(source.tenant_id, source.team_id, source.agent_id,
                                                   source.conversation_id, source.request_id)):
        raise Problem(409, "SKILL_DRAFT_RECEIPT_INVALID", "the M3 draft receipt differs from the original native intent")
    return receipt


def skill_draft_provider(*, transport=None):
    builtin = BUILTIN_CAPABILITIES[SKILLIFY_ENTRY_ID]

    def accepts(grant):
        return (grant.entry_id == SKILLIFY_ENTRY_ID and grant.entry_sha256 == builtin.entry_sha256
                and grant.kind == "fa-builtin" and grant.tools == builtin.tools and not grant.vision)

    async def bind(context, grants):
        if context.api_token_provider is None:
            raise Problem(503, "NATIVE_EXECUTION_UNAVAILABLE", "native skill drafts require the original API credential")
        client = NativeSkillDraftClient(context.api_url, context.api_token_provider, transport=transport)
        scope = NativeTurnScope(conversation_id=context.receipt.conversation_id, request_id=context.receipt.request_id)

        async def handle(packet):
            if not context.receipt.human_origin:
                raise ToolFailure(GUIDANCE["NATIVE_SKILL_DRAFT_DISABLED"], public_code="NATIVE_SKILL_DRAFT_DISABLED")
            try:
                arguments = NativeSkillDraftArgs.model_validate(packet["arguments"])
                effects = context.effects
                claim = await effects.reserve(SLOT, draft_material(arguments))
                if not claim.fresh and claim.record.status == "done":
                    saved = NativeSkillDraftReceipt.model_validate_json(json.dumps(claim.record.result))
                else:
                    saved = await client.save(scope, arguments, reconcile=not claim.fresh)
                    _matches(saved, context.receipt, arguments)
                    if claim.fresh:
                        await effects.finish(claim, saved.model_dump(mode="json"))
                return ToolOutput(_matches(saved, context.receipt, arguments).model_dump_json())
            except (KeyError, ValueError):
                raise ToolFailure("Supply one bounded purpose, complete draft body and at most four small text resources.",
                                  public_code="CAPABILITY_ARGUMENTS") from None
            except Problem as error:
                code = ("NATIVE_SKILL_DRAFT_CONFLICT" if error.body["error"]["code"] in {
                    "NATIVE_EFFECT_CONFLICT", "SKILL_DRAFT_TARGET_CONFLICT", "SKILL_DRAFT_RECEIPT_INVALID"}
                    else "NATIVE_SKILL_DRAFT_UNKNOWN")
                raise ToolFailure(GUIDANCE[code], public_code=code) from None

        return NativeCapabilityBinding(
            tools=(ToolSpec("save_skill_draft",
                "Save one unapproved Team skill draft from this original human request. "
                "Optional resources are at most four complete text/plain, text/markdown or application/json files, "
                "each at most 8192 UTF-8 bytes, with simple unique names. Supply literal text, never paths, scripts "
                "to execute or plugins to install. Resources are data and receive the same original native source. "
                "The existing human approval and selection are still required; never claim the skill is active.",
                NativeSkillDraftArgs.model_json_schema(), handle),),
            result_schemas={"save_skill_draft": NativeSkillDraftReceipt},
        )

    return NativeCapabilityProvider(accepts=accepts, bind=bind, name="skill_draft")
