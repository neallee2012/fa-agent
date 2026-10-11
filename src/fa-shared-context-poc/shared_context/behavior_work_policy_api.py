"""Stable work entry/read decisions; operator installation remains separate."""
import os
import asyncio
from datetime import datetime
from pathlib import Path
from time import monotonic
from fastapi import Depends,FastAPI,Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from pydantic import Field,RootModel
import hashlib
from .behavior_api import _body,_response,_install_error_handlers,_principal_dependency
from .behavior_assistant_wire import AssistantSubmit,InstructionSubmit
from .behavior_routine_wire import RoutineTrigger
from .behavior_wire import Command
from .behavior_operations import _Model
from .behavior_server import _lifespan,build_server
from .behavior_server_config import load_configuration,read_document
from .behavior_work_policy import SteadyWorkPolicy,CostObservation,WorkPreflightQuery
from .behavior_work_policy_installation import StandingPolicyInstaller,StandingInstallPlan,StandingInstallApproval
from .behavior_runtime_rotation import RotationEvidenceFiles
from .models import Identifier
from .errors import Problem


_SAFE_WORK_ERRORS={
    "AGENT_USAGE_SCOPE":"Only the verified Agent of this policy may report its own turn usage.",
    "AGENT_USAGE_INVALID":"A typed turn usage report or an explicit unknown is required.",
    "AGENT_USAGE_CONFLICT":"This turn's usage is already recorded differently; do not resend another report.",
    "ROUTINE_PRODUCER_DISABLED":"Routine production is disabled; no occurrence was submitted.",
    "ROUTINE_OWNER_DENIED":"The routine is not available to this authenticated owner.",
    "ROUTINE_TIME_ZONE_UNAVAILABLE":"The configured routine time zone is unavailable.",
    "ROUTINE_LOCAL_TIME_MISSING":"The scheduled local time does not exist in the configured time zone.",
    "ROUTINE_LOCAL_TIME_AMBIGUOUS":"The scheduled local time repeats in the configured time zone.",
    "ROUTINE_OCCURRENCE_INVALID":"The submitted time is not an occurrence of the approved routine.",
    "ROUTINE_OCCURRENCE_FUTURE":"This routine occurrence is not due.",
    "ROUTINE_OCCURRENCE_STALE":"Only the most recent due routine occurrence can be triggered.",
    "ROUTINE_SOURCE_CHANGED":"The occurrence no longer matches its original approved routine input.",
    "CONVERSATION_TRANSFER_NOT_READY":"The original version failure must be terminal and forwarded before environment preparation.",
    "CONVERSATION_TRANSFER_SCOPE":"The original human, Team, agent and checkpoint scope must match.",
    "CONVERSATION_TRANSFER_ARCHIVE_INVALID":"The last original checkpoint is incomplete or incompatible; start a new conversation instead.",
    "CONVERSATION_TRANSFER_UNAVAILABLE":"The original environment preparation or checkpoint reader is unavailable.",
    "CONVERSATION_TRANSFER_UNKNOWN":"The original new environment is unconfirmed; only reconcile that same request and session.",
    "CONVERSATION_TRANSFER_CHANGED":"The original conversation, checkpoint or displayed destination changed; no replacement was selected.",
    "CONVERSATION_TRANSFER_CONFLICT":"The failed turn already has its original environment preparation; do not create another.",
    "CONVERSATION_TRANSFER_INVALID":"The original environment transfer or its receipt differs.",
    "CONVERSATION_TRANSFER_BUDGET":"Current work allowance does not permit preparing a new environment.",
    "PROFILE_SELECTION_UNSUPPORTED":"The template does not describe compatible read-only conversation settings.",
    "PROFILE_SELECTION_DISABLED":"The original profile-selection authority is unavailable.",
    "PROFILE_SELECTION_DENIED":"Only the configured human and original Team may select conversation settings.",
    "PROFILE_SELECTION_CONFLICT":"The original selection request or current selection changed; read before retrying.",
    "PROFILE_SELECTION_SOURCES_CHANGED":"Template references differ from currently approved Team skills.",
    "PROFILE_SELECTION_MISSING":"The original profile selection is missing; existing work was not rewritten.",
    "PROFILE_SELECTION_INVALID":"The retained profile selection or its receipt differs.",
    "PROFILE_SELECTION_UNKNOWN":"The original profile selection is not confirmed; reconcile the same request.",
    "AGENT_CONNECTOR_UNKNOWN":"A listed connector ID is not a registered connector. Nothing was saved.",
    "AGENT_CONNECTORS_CHANGED":"This Agent's connector choice changed; read it before changing again.",
    "AGENT_CONNECTORS_CONFLICT":"This request ID already saved another connector choice; use a new request ID.",
    "AGENT_CONNECTORS_INVALID":"The saved connector choice belongs to another Agent.",
    "CAPABILITY_SELECTION_INVALID":"Capability references must be distinct and ordered.",
    "CAPABILITY_BASE_UNSUPPORTED":"This instruction profile does not compose organization capabilities.",
    "CAPABILITY_SOURCES_CHANGED":"A selected capability is not in the current organization catalog.",
    "CAPABILITY_SELECTION_REQUIRED":"Capabilities apply only through a human profile selection.",
    "CAPABILITY_MODEL_UNSUPPORTED":"A selected capability needs a model that accepts images.",
    "ATTACHMENTS_DISABLED":"The original server profile does not enable source attachments.",
    "USER_TEXT_FILES_DISABLED":"The original server profile does not enable literal UTF-8 file input.",
    "USER_FILE_NAME_INVALID":"Use one exact logical filename from the original instruction.",
    "USER_FILE_NOT_FOUND":"That original turn does not contain the requested literal file.",
    "ATTACHMENT_SOURCE_DENIED":"The selected attachment is outside the original source scope.",
    "ATTACHMENT_SOURCE_CHANGED":"The selected attachment differs from its original source.",
    "ATTACHMENT_FORMAT":"The original attachment is not supported complete text.",
    "ATTACHMENT_HISTORY_INVALID":"Original conversation attachment references are unavailable.",
    "ATTACHMENT_SOURCE_LIMIT":"The original attachment references exceed the supported complete-source bound.",
    "SOURCE_WITHDRAWN":"Original source access has been withdrawn; no replacement was selected.",
    "SOURCE_EXPIRED":"The original source has expired; retained bytes do not grant current access.",
    "SOURCE_REFERENCE_MISMATCH":"The original source reference differs from retained material.",
    "SOURCE_GRAPH_LIMIT":"The complete original source dependency graph exceeds its supported bound.",
    "BEHAVIOR_SOURCE_DENIED":"The selected source is outside the original registered scope.",
    "CANCELLATION_RECEIPT_UNKNOWN":"The original cancellation receipt remains unknown. No cancellation was sent again.",
    "CANCELLATION_RECEIPT_CHANGED":"The original cancellation generation or request differs. No replacement request was selected.",
    "CANCELLATION_RECEIPT_INVALID":"A supported original cancellation generation is required.",
    "AGENT_TEMPLATE_DISABLED":"The configured assistant template source is unavailable.",
    "AGENT_TEMPLATE_PROFILE_UNSUPPORTED":"Export requires an explicit versioned instruction profile.",
    "AGENT_TEMPLATE_SOURCE_CHANGED":"Skill sources changed during export; read current references again.",
    "SYSTEM_PROMPT_UNRECONSTRUCTABLE":"This server cannot rebuild the selected prompt from its versioned text; nothing else is shown instead.",
    "NATIVE_TURNS_DISABLED":"The original server cannot confirm that this Native or Responses operation is enabled.",
    "NATIVE_RESPONSE_UNCONFIRMED":"The Responses handoff is not confirmed. Reconcile the same original request.",
    "INSTRUCTION_MODEL_CONFIGURATION":"The original project deployment configuration is unavailable.",
    "INSTRUCTION_MODEL_UNAVAILABLE":"Choose an existing chat deployment from this original project.",
    "INSTRUCTION_DEFAULT_PROMPT_CONFIGURATION":"The server's new-conversation prompt default is unsupported. No work was accepted.",
    "AGENT_ROLE_PROMPT_MISSING":"This agent's role profile has no saved role prompt yet. No work was accepted.",
    "AGENT_ROLE_PROMPT_INVALID":"The saved role prompt could not be verified. No work was accepted.",
    "INSTRUCTION_TASK_TOO_LARGE":"The complete turn (message, files, role and capabilities) is too large to store. No work was accepted.",
    "SKILL_CATALOG_CAPACITY_UNKNOWN":"The configured catalog budget requires declared input capacity.",
    "SKILL_CATALOG_BUDGET_TOO_SMALL":"The declared input capacity is too small for the configured catalog budget.",
    "SKILL_CATALOG_TOKEN_COUNT_UNKNOWN":"The configured catalog text counter is unavailable.",
    "MODEL_CAPACITY_INVALID":"The selected server-declared model capacity is invalid or contradictory.",
    "MODEL_CAPACITY_CHANGED":"The original task's declared model capacity changed; no task or request was replaced.",
    "WORK_SUBMIT_UNKNOWN":"Original submission is not fully admitted. Reconcile the same ID; do not resend.",
    "WORK_POLICY_REQUEST_CONFLICT":"The original request ID has different retained input.",
    "CONVERSATION_TURN_UNRESOLVED":"The previous original turn is still pending or unknown.",
    "SOP_COPY_OUTCOME_UNKNOWN":"The original copy remains unresolved; a new turn is not permission to resend it.",
    "SKILL_DRAFT_OUTCOME_UNKNOWN":"The original skill draft remains unresolved; a new turn is not permission to resend it.",
    "CONVERSATION_SCOPE_DENIED":"The conversation does not belong to this original human and agent.",
    "CONVERSATION_NOT_FOUND":"The original conversation was not found.",
    "CONVERSATION_CONTEXT_MISSING":"The original SDK context is missing; do not restart empty.",
    "CONVERSATION_CONTEXT_CHANGED":"The original saved SDK context differs.",
    "SDK_STATE_UNSUPPORTED_FORMAT":"The saved SDK context requires a supported checkpoint profile; do not restart empty.",
    "CONVERSATION_ARCHIVED":"This conversation was explicitly reset; start a new session.",
    "CONVERSATION_SESSION_RESET_REQUIRED":"This legacy conversation requires an explicit reset or new session.",
    "CONVERSATION_SESSION_UNAVAILABLE":"The original Hosted session is unavailable; no replacement was created.",
    "INSTRUCTION_REPLY_NOT_READY":"No complete original SDK reply has been saved yet.",
    "INSTRUCTION_REPLY_INVALID":"The original saved SDK reply differs from this conversation turn.",
    "TURN_CONTEXT_NOT_READY":"No original saved turn metadata is available yet; no work was restarted.",
    "WORK_POLICY_COST_OBSERVATION_STALE":"A fresh cumulative cost observation is required only for new paid work.",
    "WORK_POLICY_COST_OBSERVATION_SCOPE":"The cost observation does not describe this original scope.",
    "WORK_POLICY_COST_OBSERVATION_REGRESSED":"Previously observed spending or reservations cannot decrease.",
    "WORK_POLICY_CUMULATIVE_BUDGET_EXHAUSTED":"The original cumulative USD allowance including its reserve is insufficient.",
    "WORK_POLICY_CONTROL_BUDGET":"The original abstract root allowance is insufficient.",
    "WORK_POLICY_REVOKED":"The current standing work/read authority is revoked.",
    "WORK_POLICY_POLICY_REVOKED":"The current standing work/read authority is revoked.",
    "WORK_POLICY_RESULT_ACCESS_REVOKED":"The current result read grant is revoked.",
    "WORK_EXECUTION_DEADLINE":"This work's execution permission expired; existing result receipt does not restart it.",
    "DELEGATION_UNKNOWN":"The original child outcome is unconfirmed; this read did not dispatch or resend a task.",
    "DELEGATION_UNAVAILABLE":"The original child reader is unavailable; this is not an absent intent.",
    "DELEGATION_DISABLED":"This original parent did not capture the delegation capability.",
    "DELEGATION_SESSION_READ_ONLY":"The original delegation session configuration does not match.",
    "DELEGATION_PARENT_REQUIRED":"The original registered parent work is required.",
    "DELEGATION_SCOPE_CHANGED":"The saved child belongs to a different parent or attempt.",
    "DELEGATION_RECORD_INVALID":"The original delegation receipt could not be verified.",
    "DELEGATION_RESULT_INVALID":"The complete original child result could not be verified.",
    "AGENT_VERSION_FOLLOWING":"This agent has no Hosted version of the deployed build yet. No work was accepted; try again after the deployment's version sync.",
}


async def work_response(call,*,max_bytes=None):
    try:
        value=await run_in_threadpool(call)
        return _response(value) if max_bytes is None else _response(value,max_bytes=max_bytes)
    except Problem as error:
        code=error.body["error"]["code"]
        if code not in _SAFE_WORK_ERRORS:raise
        refusal=error.body["error"].get("transfer_refusal")
        details=({"transfer_refusal":refusal} if 400<=error.status<500
                 and code.startswith("CONVERSATION_TRANSFER_")
                 and type(refusal) is dict and set(refusal)=={"stage","reserved"}
                 and refusal["stage"]=="before-reservation" and refusal["reserved"] is False else {})
        if code=="INSTRUCTION_TASK_TOO_LARGE":
            details={name:error.body["error"][name] for name in ("max_bytes","actual_bytes")
                     if type(error.body["error"].get(name)) is int}
        return JSONResponse(status_code=error.status,content={"error":{"code":code,"message":_SAFE_WORK_ERRORS[code],
            "reconcile_original":code in {"WORK_SUBMIT_UNKNOWN","PROFILE_SELECTION_UNKNOWN","CONVERSATION_TRANSFER_UNKNOWN"},
            "automatic_retry":False,**details}})


class RevokeStandingPolicy(_Model):
    decision_id:Identifier


_ONBOARDING_ERRORS={
    "AGENT_QUOTA_REACHED","AGENT_NAME_TAKEN","AGENT_ONBOARDING_CONFLICT","AGENT_ONBOARDING_NAME_CONFLICT",
    "AGENT_ONBOARDING_PROFILE_UNSUPPORTED","AGENT_ONBOARDING_SOURCE_UNAVAILABLE","AGENT_ONBOARDING_FOUNDRY_INVALID",
    "AGENT_ONBOARDING_NOT_FOUND","AGENT_ONBOARDING_UNKNOWN","AGENT_ONBOARDING_INVALID",
    "TEAM_RUNTIME_NOT_SERVED","TEAM_OWNER_DENIED","TEAM_REGISTRY_DISABLED","TEAM_AGENT_CONFLICT","TEAM_AGENT_LIMIT",
    "AGENT_ROLE_PROMPT_TOO_LARGE","AGENT_ROLE_PROMPT_INVALID","AGENT_ROLE_PROMPT_CONFLICT",
    "AGENT_ROLE_PROMPT_POLICY_CHANGED","AGENT_CONSOLE_NOT_FOUND","AGENT_CONSOLE_DENIED","AGENT_ONBOARDING_IN_PROGRESS",
    "AGENT_ONBOARDING_SKILLS_TOOL_REQUIRED","AGENT_TEMPLATE_DISABLED","CAPABILITY_SELECTION_INVALID",
    "CAPABILITY_BASE_UNSUPPORTED","CAPABILITY_SOURCES_CHANGED","PROFILE_SELECTION_SOURCES_CHANGED",
    "PROFILE_SELECTION_DENIED","PROFILE_SELECTION_UNSUPPORTED","PROFILE_SELECTION_CONFLICT",
    "PROFILE_SELECTION_DISABLED","SKILL_CATALOG_BUDGET_TOO_SMALL","AGENT_ONBOARDING_SELECTION_STALE",
    "AGENT_VERSION_FOLLOWING",
}
_ONBOARDING_DETAILS=("limit","max_bytes","version")
_ONBOARDING_TEXT_DETAILS=("capability","cause")


async def onboarding_response(call):
    """Onboarding refusals keep their own code, message and bounded numeric details (limit/max_bytes)."""
    try:
        finished,value=await run_in_threadpool(call)
    except Problem as error:
        body=error.body["error"]
        if body["code"] not in _ONBOARDING_ERRORS:raise
        details={name:body[name] for name in _ONBOARDING_DETAILS if type(body.get(name)) is int}
        details.update({name:body[name] for name in _ONBOARDING_TEXT_DETAILS if type(body.get(name)) is str})
        return JSONResponse(status_code=error.status,headers={"Cache-Control":"no-store"},content={"error":{
            "code":body["code"],"message":body["message"],
            "continue_same_request":body["code"]=="AGENT_ONBOARDING_UNKNOWN","automatic_retry":False,**details}})
    response=_response(value)
    response.status_code=200 if finished else 202
    return response


def _install_onboarding_routes(app,composition,identity,prefix,controller):
    from .behavior_agent_onboarding import CreateTeamAgent,onboard,onboarding_status

    @app.post(prefix+"/agents")
    async def create_agent(request:Request,principal=Depends(identity)):
        try:
            body=await _body(request,CreateTeamAgent,max_body_bytes=512*1024)
        except Problem as error:
            if error.status!=422:raise
            return JSONResponse(status_code=422,headers={"Cache-Control":"no-store"},content={"error":{
                "code":"AGENT_ONBOARDING_INVALID","message":(
                    "request_id, display_name and role_prompt are required; capabilities and skills are sent together"),
                "automatic_retry":False}})
        def run():
            controller()
            return onboard(composition,principal,body)
        return await onboarding_response(run)

    @app.get(prefix+"/agents/onboarding/{request_id}")
    async def onboarding(request_id:str,request:Request,principal=Depends(identity)):
        if request.query_params or await request.body():
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","onboarding status accepts no body or query")
        def read():
            return True,onboarding_status(composition,principal,request_id)
        return await onboarding_response(read)

    @app.get(prefix+"/agents/{agent_id}/console-connection")
    async def agent_console_connection(agent_id:str,request:Request,principal=Depends(identity)):
        if request.query_params or await request.body():
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","console connection accepts no body or query")
        def read():
            from .behavior_agent_onboarding import console_connection
            controller()
            return True,console_connection(composition,principal,agent_id)
        return await onboarding_response(read)


class _SubmitBody(RootModel[AssistantSubmit|InstructionSubmit]):
    # Preserve the old undiscriminated body; model/conversation fields select instructions.
    root:AssistantSubmit|InstructionSubmit=Field(union_mode="left_to_right")


def install_work_policy_routes(app,composition,*,start_work=None,wake_work=None):
    identity=_principal_dependency(composition.verifier)
    prefix="/work-policy/v1"
    from .behavior_native_turn_api import configured_native_turns, install_native_turn_routes
    install_native_turn_routes(app, composition, enabled=configured_native_turns())
    from .native_agent_api import install_native_agent_routes
    install_native_agent_routes(app, composition, enabled=configured_native_turns())
    from .native_capability_api import install_native_capability_routes
    install_native_capability_routes(app, composition, enabled=configured_native_turns())
    from .native_skill_draft_api import install_native_skill_draft_routes
    install_native_skill_draft_routes(app, composition, enabled=configured_native_turns())
    from .native_routine_api import install_native_routine_routes
    install_native_routine_routes(app, composition, enabled=configured_native_turns())
    wake_work=start_work if wake_work is None else wake_work
    observed_completions={}
    if start_work is not None:app.state.instruction_runner=True
    def controller(*,refresh=True):
        if composition.runtime.work_policy is None:composition.installation()
        elif refresh:composition.runtime.work_policy.refresh()
        if composition.runtime.mode=="native":
            from .behavior_work_target import activate_target
            activate_target(composition)
        return composition.runtime.work_policy

    def read_current(call):
        from .behavior_assistant import _read_conflicts
        return _read_conflicts(lambda: composition.runtime.store.consistent_read(call,max_keys=None,row_scoped=True),
                               phase="instruction_query")

    @app.get(prefix+"/agents")
    async def agents(request:Request,principal=Depends(identity)):
        if request.query_params or await request.body():
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","agent directory accepts no body or query")
        def read():
            from .behavior_agent_message_api import public_agents
            controller()
            return public_agents(composition,principal)
        return await work_response(read)

    if getattr(composition,"agent_id",None) is None:
        _install_onboarding_routes(app,composition,identity,prefix,controller)

    from .realtime_push import agent_id_of,install_realtime_routes,realtime_publisher
    publisher=realtime_publisher(composition)
    install_realtime_routes(app,composition,identity,prefix,publisher)

    @app.post(prefix+"/turn-usage")
    async def turn_usage(request:Request,principal=Depends(identity)):
        # ADR-0007: the VM sends each final native turn's usage once; N2 may resend the same report.
        from .behavior_turn_usage import AgentTurnUsageReport
        report=await _body(request,AgentTurnUsageReport)
        def record():
            from .behavior_instruction_cost import agent_usage_scope,record_agent_turn_usage
            control=controller(refresh=False)
            agent_usage_scope(control,principal)
            receipt=record_agent_turn_usage(control,rid=report.request_id,usage=report.usage)
            if publisher is not None:
                # The VM reports only after its terminal receipt is saved, so the hint follows the final state.
                publisher.notify(control.policy.template.team_id,"turn.finished",
                                 agent_id=agent_id_of(composition,principal),request_id=report.request_id)
            return {"request_id":report.request_id,**receipt.payload["result"]}
        return await work_response(record)

    @app.get(prefix+"/bindings/{binding_id}/agent-messages")
    async def agent_messages(binding_id:str,request:Request,principal=Depends(identity)):
        if request.query_params or await request.body():
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","message projection accepts no body or query")
        def read():
            from .behavior_agent_message_api import public_messages
            control=controller(refresh=False)
            control.refresh(binding_id)
            return public_messages(composition,principal,binding_id)
        return await work_response(lambda:read_current(read))

    def current_template(principal):
        from .behavior_agent_template import export_template
        control=composition.runtime.work_policy
        if control is None:
            raise Problem(503,"WORK_POLICY_DISABLED","a configured standing scope is required")
        control._human(principal)
        authority=getattr(getattr(composition,"assistant",None),"authority",None)
        if authority is None:
            raise Problem(503,"AGENT_TEMPLATE_DISABLED","the configured assistant authority is required")
        return export_template(control,authority,principal)

    @app.get(prefix+"/agent-template")
    async def agent_template(request:Request,principal=Depends(identity)):
        if request.query_params:
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","template export does not accept query parameters")
        async for chunk in request.stream():
            if chunk:
                raise Problem(422,"BEHAVIOR_INVALID_REQUEST","template export does not accept a body")
        response=await work_response(lambda:current_template(principal))
        response.headers["Cache-Control"]="no-store"
        if response.status_code==200:
            response.headers["Content-Disposition"]='attachment; filename="fa-agent-template.json"'
            response.headers["x-content-sha256"]=hashlib.sha256(response.body).hexdigest()
        return response

    @app.post(prefix+"/agent-template/inspect")
    async def inspect_agent_template(request:Request,principal=Depends(identity)):
        from .behavior_agent_template import AgentTemplate,MAX_TEMPLATE_BYTES,inspect_template
        imported=await _body(request,AgentTemplate,max_body_bytes=MAX_TEMPLATE_BYTES)
        response=await work_response(lambda:inspect_template(imported,current_template(principal)))
        response.headers["Cache-Control"]="no-store"
        return response

    @app.get(prefix+"/agent-template/system-prompt")
    async def agent_system_prompt(request:Request,principal=Depends(identity)):
        # Owner-only read of the text a new native conversation would send; no selection, model or permission change.
        if request.query_params or await request.body():
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","system prompt reads accept no body or query")
        def read():
            from .behavior_native_capture import system_prompt_inputs
            from .behavior_native_turn_api import configured_native_turns
            from .behavior_system_prompt_view import current_view
            control=composition.runtime.work_policy
            if control is None:
                raise Problem(503,"WORK_POLICY_DISABLED","a configured standing scope is required")
            control._human(principal)
            if not configured_native_turns():
                raise Problem(503,"NATIVE_TURNS_DISABLED","the system prompt view follows the native turn path")
            authority=getattr(getattr(composition,"assistant",None),"authority",None)
            return current_view(*system_prompt_inputs(control,authority,principal))
        from .behavior_system_prompt_view import MAX_SYSTEM_PROMPT_VIEW_BYTES
        response=await work_response(read,max_bytes=MAX_SYSTEM_PROMPT_VIEW_BYTES)
        response.headers["Cache-Control"]="no-store"
        return response

    @app.get(prefix+"/agent-template/options")
    async def agent_template_options(request:Request,principal=Depends(identity)):
        if request.query_params or await request.body():
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","template options accept no body or query")
        def read():
            from .behavior_agent_template import template_options
            from .behavior_native_responses_api import template_responses_options
            control=composition.runtime.work_policy
            if control is None:
                raise Problem(503,"WORK_POLICY_DISABLED","a configured standing scope is required")
            control._human(principal)
            authority=getattr(getattr(composition,"assistant",None),"authority",None)
            if authority is None:
                raise Problem(503,"AGENT_TEMPLATE_DISABLED","the configured assistant authority is required")
            options=template_options(control,authority,principal)
            return options.model_copy(update={"responses":template_responses_options(composition,principal)})
        response=await work_response(read)
        response.headers["Cache-Control"]="no-store"
        return response

    @app.get(prefix+"/agent-template/selection")
    async def profile_selection(request:Request,principal=Depends(identity)):
        if request.query_params or await request.body():
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","profile selection reads accept no body or query")
        def read():
            from .behavior_profile_selection import (
                current_selection,selection_result,UnselectedConversationProfile,SelectedConversationProfile,
            )
            control=controller(refresh=False)
            control._human(principal)
            policy=control.installed()
            if not policy.active or not policy.read_enabled:
                raise Problem(403,"WORK_POLICY_REVOKED","current standing scope is revoked")
            value=current_selection(control.store,policy)
            return (UnselectedConversationProfile(
                selection_id=None,applies_to="new-conversations-only",permissions_changed=False,
            ) if value is None else SelectedConversationProfile(
                **selection_result(value),template=value.command.template,
            ))
        response=await work_response(lambda:read_current(read))
        response.headers["Cache-Control"]="no-store"
        return response

    from .behavior_profile_selection import SelectConversationProfile,ConversationProfileReceipt
    def selection_endpoint(reconcile):
        async def select(request:Request,principal=Depends(identity)):
            command=await _body(request,SelectConversationProfile)
            def apply():
                from .behavior_profile_selection import select_profile
                control=controller()
                authority=getattr(getattr(composition,"assistant",None),"authority",None)
                receipt=select_profile(control,authority,principal,command,reconcile=reconcile)
                return ConversationProfileReceipt.model_validate({
                    "result":receipt.payload["result"],
                    "control_request_id":receipt.payload["request"]["request_id"],
                    "control_request_digest":receipt.payload["request"]["request_digest"],
                })
            response=await work_response(apply)
            response.headers["Cache-Control"]="no-store"
            return response
        return select
    app.add_api_route(prefix+"/agent-template/select",selection_endpoint(False),methods=["POST"])
    app.add_api_route(prefix+"/agent-template/selection-reconcile",selection_endpoint(True),methods=["POST"])

    from .team_connector_wire import SelectAgentConnectors,team_connectors_enabled
    if team_connectors_enabled(os.environ):
        from .team_connector_narrowing import connector_view,save_narrowing

        @app.get(prefix+"/agent-connectors")
        async def agent_connectors(request:Request,principal=Depends(identity)):
            if request.query_params or await request.body():
                raise Problem(422,"BEHAVIOR_INVALID_REQUEST","connector choice reads accept no body or query")
            return await work_response(lambda:read_current(lambda:connector_view(controller(refresh=False),principal)))

        @app.post(prefix+"/agent-connectors")
        async def select_agent_connectors(request:Request,principal=Depends(identity)):
            command=await _body(request,SelectAgentConnectors)
            return await work_response(lambda:save_narrowing(controller(),principal,command))

    @app.get(prefix+"/accounting")
    async def accounting(request:Request,principal=Depends(identity)):
        if request.query_params:
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","accounting reads do not accept query parameters")
        async for chunk in request.stream():
            if chunk:
                raise Problem(422,"BEHAVIOR_INVALID_REQUEST","accounting reads do not accept a body")
        def read():
            control=composition.runtime.work_policy
            if control is None:
                raise Problem(503,"WORK_POLICY_DISABLED","a configured standing accounting scope is required")
            return control.accounting(principal)
        return _response(await run_in_threadpool(read))

    @app.get(prefix+"/models")
    async def models(principal=Depends(identity)):
        def read():
            control=controller(refresh=False)
            control.installed()
            control._human(principal)
            return control.models.public()
        return await work_response(lambda: read_current(read))

    @app.get(prefix+"/attachment-sources")
    async def attachment_sources(request:Request,principal=Depends(identity)):
        if request.query_params or await request.body():
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","attachment source catalog accepts no body or query")
        def read():
            from .behavior_attachment_catalog import read_attachment_catalog
            return read_attachment_catalog(controller(),principal)
        response=await work_response(read)
        response.headers["Cache-Control"]="no-store"
        return response

    @app.get(prefix+"/conversations/{conversation_id}/attachment-sources")
    async def conversation_attachment_sources(conversation_id:str,request:Request,principal=Depends(identity)):
        if request.query_params or await request.body():
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","conversation source catalog accepts no body or query")
        def read():
            from .behavior_attachment_catalog import read_attachment_catalog
            return read_attachment_catalog(controller(refresh=False),principal,conversation_id=conversation_id)
        response=await work_response(read)
        response.headers["Cache-Control"]="no-store"
        return response

    @app.get(prefix+"/conversations/{conversation_id}/user-text-file-capability")
    async def user_text_file_capability(conversation_id:str,request:Request,principal=Depends(identity)):
        if request.query_params or await request.body():
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","file capability reads accept no body or query")
        def read():
            from .behavior_text_files import read_user_text_file_capability
            return read_user_text_file_capability(controller(refresh=False),principal,conversation_id)
        response=await work_response(read)
        response.headers["Cache-Control"]="no-store"
        return response

    @app.post(prefix+"/preflight")
    async def preflight(request:Request,principal=Depends(identity)):
        query=await _body(request,WorkPreflightQuery)
        def read():return controller().preflight(principal,query)
        try:
            return _response(await run_in_threadpool(read_current, read))
        except Problem as error:
            if error.body["error"]["code"] not in {"AGENT_ROLE_PROMPT_MISSING","AGENT_ROLE_PROMPT_INVALID"}:raise
            def failed():raise error
            return await work_response(failed)

    @app.post(prefix+"/submit")
    async def submit(request:Request,principal=Depends(identity)):
        command=(await _body(request,_SubmitBody)).root
        loop=asyncio.get_running_loop()
        def accepted():
            from .behavior_timing import phase_timing
            control=controller(refresh=False)
            control._human(principal,"submit")
            with phase_timing("admission",job_id=command.request_id):
                actual,status=control.submit(principal,command)
            if (start_work is not None and control.records[actual.binding_id].profile.instruction is not None
                    and not (status.state in {"succeeded","failed","cancelled"} and status.result_forwarded)):
                loop.call_soon_threadsafe(start_work,actual.binding_id)
            return control.submission_result(actual,status)
        return await work_response(accepted)

    @app.post(prefix+"/routines/{routine_id}/trigger")
    async def routine_trigger(routine_id:str,request:Request,principal=Depends(identity)):
        command=await _body(request,RoutineTrigger)
        loop=asyncio.get_running_loop()
        def accepted():
            from .behavior_routine import trigger
            control=controller(refresh=False)
            actual,status=trigger(control,principal,routine_id,command)
            if (start_work is not None
                    and not (status.state in {"succeeded","failed","cancelled"} and status.result_forwarded)):
                loop.call_soon_threadsafe(start_work,actual.binding_id)
            return control.submission_result(actual,status)
        return await work_response(accepted)

    @app.post(prefix+"/routines/{routine_id}/trigger-reconcile")
    async def routine_reconcile(routine_id:str,request:Request,principal=Depends(identity)):
        command=await _body(request,RoutineTrigger)
        def read():
            from .behavior_routine import reconcile
            from .behavior_work_policy import WorkSubmitObservation
            control=composition.runtime.work_policy
            if control is None:
                raise Problem(503,"WORK_POLICY_DISABLED","a configured standing scope is required")
            original=reconcile(control,principal,routine_id,command)
            if isinstance(original,WorkSubmitObservation):
                return original
            binding,status=original
            return control.submission_result(binding,status)
        return await work_response(lambda:read_current(read))

    @app.post(prefix+"/submit-reconcile")
    async def reconcile(request:Request,principal=Depends(identity)):
        command=await _body(request,InstructionSubmit)
        def read():
            from .behavior_work_policy import WorkSubmitObservation
            control=composition.runtime.work_policy
            if control is None:
                raise Problem(503,"WORK_POLICY_DISABLED","a configured standing scope is required")
            value=control.submit_reconcile(principal,command)
            if isinstance(value,WorkSubmitObservation):return value
            binding,status=value
            return control.submission_result(binding,status)
        return await work_response(lambda:read_current(read))

    @app.get(prefix+"/conversations/{conversation_id}")
    async def conversation(conversation_id:str,principal=Depends(identity)):
        def read():
            control=controller(refresh=False)
            projection=control.conversation(principal,conversation_id)
            live=getattr(app.state,"instruction_live_replies",None)
            if live is not None:
                for turn in projection["turns"]:
                    if turn["state"] in {"starting","running","capacity_uncertain"}:
                        preview=live.read(turn["job_id"])
                        if preview is not None:turn["stream"]=preview
            native=composition.runtime.dep.native_launches
            observers=[]
            if start_work is not None and native is not None:
                for turn in projection["turns"]:
                    if turn["state"] in {"starting","capacity_uncertain","failed"}:
                        job=composition.runtime.dep.workers.inspect(turn["job_id"])
                        if native.pending_version_observation(job) or native.pending_lost_observation(job):
                            observers.append((turn["binding_id"],False))
                        elif native.pending_completion_observation(job):
                            observers.append((turn["binding_id"],True))
            pending[:]=observers
            return projection
        pending=[]
        response=await work_response(lambda:read_current(read))
        if response.status_code==200:
            for binding_id,completion in pending:
                if not completion:
                    start_work(binding_id)
                else:
                    previous=observed_completions.get(binding_id)
                    now=monotonic()
                    interval=composition.runtime.dep.delivery.contract.review_interval_seconds
                    if previous is None or previous[0]<2 and now-previous[1]>=interval:
                        observed_completions[binding_id]=(1 if previous is None else previous[0]+1,now)
                        wake_work(binding_id)
        return response

    @app.get(prefix+"/conversations/{conversation_id}/environment")
    async def environment(conversation_id:str,request:Request,principal=Depends(identity)):
        if request.query_params:
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","environment preview does not accept query parameters")
        async for chunk in request.stream():
            if chunk:raise Problem(422,"BEHAVIOR_INVALID_REQUEST","environment preview does not accept a body")
        def read():
            from .behavior_conversation_transfer import preview
            control=controller(refresh=False)
            authority=getattr(getattr(composition,"assistant",None),"authority",None)
            return preview(control,authority,principal,conversation_id)
        return await work_response(lambda:read_current(read))

    async def prepare_environment(conversation_id,request,principal,*,reconcile):
        from .behavior_conversation_transfer import prepare,TransferCommand
        command=await _body(request,TransferCommand)
        def apply():
            control=controller()
            authority=getattr(getattr(composition,"assistant",None),"authority",None)
            return prepare(control,authority,principal,conversation_id,command,reconcile=reconcile)
        return await work_response(apply)

    @app.post(prefix+"/conversations/{conversation_id}/environment")
    async def new_environment(conversation_id:str,request:Request,principal=Depends(identity)):
        return await prepare_environment(conversation_id,request,principal,reconcile=False)

    @app.post(prefix+"/conversations/{conversation_id}/environment-reconcile")
    async def reconcile_environment(conversation_id:str,request:Request,principal=Depends(identity)):
        return await prepare_environment(conversation_id,request,principal,reconcile=True)

    @app.get(prefix+"/conversations/{conversation_id}/turns/{turn_number}/context")
    async def turn_context(conversation_id:str,turn_number:int,request:Request,principal=Depends(identity)):
        if request.query_params:
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","turn metadata reads do not accept query parameters")
        async for chunk in request.stream():
            if chunk:
                raise Problem(422,"BEHAVIOR_INVALID_REQUEST","turn metadata reads do not accept a body")
        def read():
            from .behavior_turn_context import read_saved_turn_context
            control=composition.runtime.work_policy
            if control is None:
                raise Problem(503,"WORK_POLICY_DISABLED","a configured standing scope is required")
            control._human(principal)
            return read_current(lambda:read_saved_turn_context(control,principal,conversation_id,turn_number))
        response=await work_response(read)
        if response.status_code==200:
            response.headers["x-content-sha256"]=hashlib.sha256(response.body).hexdigest()
        return response

    @app.get(prefix+"/conversations/{conversation_id}/turns/{turn_number}/delegation")
    async def delegation(conversation_id:str,turn_number:int,request:Request,principal=Depends(identity)):
        from .behavior_delegation_wire import MAX_DELEGATION_BYTES
        if request.query_params:
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","delegation reads accept no query parameters")
        async for chunk in request.stream():
            if chunk:
                raise Problem(422,"BEHAVIOR_INVALID_REQUEST","delegation reads accept no body")
        def read():
            control=composition.runtime.work_policy
            if control is None:
                raise Problem(503,"WORK_POLICY_DISABLED","a configured standing scope is required")
            authority=getattr(getattr(composition,"assistant",None),"authority",None)
            return control.read_delegation(principal,conversation_id,turn_number,authority)
        try:
            value=await run_in_threadpool(read_current,read)
        except Problem as error:
            def failed():raise error
            return await work_response(failed)
        if value is None:
            return Response(status_code=204,headers={"Cache-Control":"no-store"})
        response=_response(value,max_bytes=MAX_DELEGATION_BYTES)
        response.headers["x-content-sha256"]=hashlib.sha256(response.body).hexdigest()
        return response

    @app.get(prefix+"/conversations/{conversation_id}/turns/{turn_number}/reply")
    async def reply(conversation_id:str,turn_number:int,principal=Depends(identity)):
        def read():return controller(refresh=False).read_reply(principal,conversation_id,turn_number)
        try:
            value=await run_in_threadpool(read_current, read)
        except Problem as error:
            def failed():raise error
            return await work_response(failed)
        raw=value.model_dump_json().encode("utf-8")
        return Response(raw,media_type="application/json",headers={
            "Cache-Control":"no-store","x-content-sha256":hashlib.sha256(raw).hexdigest()})

    @app.get(prefix+"/conversations/{conversation_id}/turns/{turn_number}/files/{filename}")
    async def original_file(conversation_id:str,turn_number:int,filename:str,request:Request,principal=Depends(identity)):
        if request.query_params:
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","original file reads accept no query parameters")
        async for chunk in request.stream():
            if chunk:
                raise Problem(422,"BEHAVIOR_INVALID_REQUEST","original file reads accept no body")
        def read():
            from .behavior_text_files import read_original_file
            return read_original_file(controller(refresh=False),principal,conversation_id,turn_number,filename)
        try:
            file=await run_in_threadpool(read_current,read)
        except Problem as error:
            def failed():raise error
            return await work_response(failed)
        raw=file.text.encode("utf-8")
        return Response(raw,media_type="text/plain",headers={
            "Cache-Control":"no-store","X-Content-Type-Options":"nosniff",
            "Content-Disposition":f'attachment; filename="{file.name}"',
            "x-content-sha256":hashlib.sha256(raw).hexdigest(),
        })

    @app.post(prefix+"/bindings/{binding_id}/cancel")
    async def cancel(binding_id:str,request:Request,principal=Depends(identity)):
        await _body(request,Command)
        loop=asyncio.get_running_loop()
        def accepted():
            control=controller()
            receipt=control.cancel(principal,binding_id)
            if (wake_work is not None and control.records[binding_id].profile.instruction is not None
                    and receipt.payload["result"].get("cancel_accepted")
                    and not receipt.payload["result"].get("executor_stopped")):
                loop.call_soon_threadsafe(wake_work,binding_id)
            return receipt
        return _response(await run_in_threadpool(accepted))

    @app.get(prefix+"/bindings/{binding_id}/cancel-receipt/{generation}")
    async def cancellation_receipt(binding_id:str,generation:int,request:Request,principal=Depends(identity)):
        if request.query_params:
            raise Problem(422,"BEHAVIOR_INVALID_REQUEST","cancellation receipt reads do not accept query parameters")
        async for chunk in request.stream():
            if chunk:
                raise Problem(422,"BEHAVIOR_INVALID_REQUEST","cancellation receipt reads do not accept a body")
        def read():
            control=composition.runtime.work_policy
            if control is None:
                raise Problem(503,"WORK_POLICY_DISABLED","a configured standing scope is required")
            return control.cancellation_receipt(principal,binding_id,generation)
        response=await work_response(lambda:read_current(read))
        if response.status_code==200:
            response.headers["x-content-sha256"]=hashlib.sha256(response.body).hexdigest()
        return response

    @app.post(prefix+"/conversations/{conversation_id}/reset")
    async def reset(conversation_id:str,request:Request,principal=Depends(identity)):
        command=await _body(request,Command)
        return await work_response(lambda:controller().reset_conversation(principal,conversation_id,command))

    @app.post("/operator/v1/standing-work-policy/revoke")
    async def revoke(request:Request,principal=Depends(identity)):
        command=await _body(request,RevokeStandingPolicy)
        def change():return controller().revoke(principal,command.decision_id)
        return _response(await run_in_threadpool(change))

    @app.post("/operator/v1/standing-work-policy/budget-enforcement")
    async def budget_enforcement(request:Request,principal=Depends(identity)):
        from .behavior_work_budget import BudgetEnforcementCommand,set_budget_enforcement
        command=await _body(request,BudgetEnforcementCommand)
        return await work_response(lambda:set_budget_enforcement(controller(),principal,command))


class PlanRequest(_Model):
    pass


class ApproveStandingPolicy(_Model):
    plan:StandingInstallPlan
    decision_id:Identifier
    expires_at:datetime


class ApplyStandingPolicy(_Model):
    plan:StandingInstallPlan
    approval:StandingInstallApproval


def create_installation_app(composition,policy,evidence,cost_reader,*,installation_id,allow_apply=False):
    service=StandingPolicyInstaller(composition,policy,evidence,cost_reader,allow_apply=allow_apply)
    app=FastAPI(title="Standing work policy installation",docs_url=None,redoc_url=None,openapi_url=None)
    _install_error_handlers(app)
    identity=_principal_dependency(composition.verifier)
    root="/operator/v1/standing-work-policy"

    @app.post(root+"/preview")
    async def preview(request:Request,principal=Depends(identity)):
        service.control._human(principal,"approve")
        await _body(request,PlanRequest)
        return _response(await run_in_threadpool(service.plan,principal,installation_id=installation_id))

    @app.post(root+"/authorize")
    async def authorize(request:Request,principal=Depends(identity)):
        command=await _body(request,ApproveStandingPolicy)
        return _response(await run_in_threadpool(service.authorize,principal,command.plan,
            decision_id=command.decision_id,expires_at=command.expires_at))

    def install(action):
        @app.post(root+"/"+action)
        async def operation(request:Request,principal=Depends(identity)):
            command=await _body(request,ApplyStandingPolicy)
            return _response(await run_in_threadpool(getattr(service,action),principal,command.plan,command.approval))
    for action in ("apply","source","reconcile"):install(action)
    app.state.standing_installer=service
    return _lifespan(app,composition,composition.installation)


def create_app():
    config=load_configuration()
    policy=read_document(os.environ["FA_STANDING_WORK_POLICY_FILE"],SteadyWorkPolicy)
    evidence=RotationEvidenceFiles(Path(os.environ["FA_STANDING_SOURCE_PROTECTION_FILE"]),
        Path(os.environ["FA_STANDING_BACKUP_PROTECTION_FILE"]),Path(os.environ["FA_STANDING_RETIREMENT_FILE"]))
    path=os.environ["FA_WORK_COST_OBSERVATION_FILE"]
    return create_installation_app(build_server(config),policy,evidence,lambda:read_document(path,CostObservation),
        installation_id=os.environ["FA_STANDING_INSTALLATION_ID"],allow_apply=os.environ.get("FA_STANDING_APPLY")=="true")
