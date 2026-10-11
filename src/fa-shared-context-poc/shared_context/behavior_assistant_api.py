"""Assistant composition with an absolute model-route lifetime; legacy defaults stay unchanged.

Server composition (all arguments are trusted server objects, never HTTP JSON):
  runtime = AssistantRuntime(dependencies, bindings, profiles=profiles,
                             clock=clock, mode=mode, controller_id=controller_id)
  authority = AssistantAuthority(runtime, existing_m3_service, configured_actors)
  app = create_assistant_app(authority, existing_verifier, provider=provider)

The supplied dependencies retain the SAME WorkerCore, FoundryExecutor,
NativeLaunchAuthority, delivery, inbox, source/review authority and root budgets.
AssistantAuthority registers its concrete result verifier with that native
authority. Provision explicit NativeExecutionGrant.workload_version="assistant-v1.2"
and a fixed AssistantProfile mapping NEW work -> EXISTING owned legacy M3 source.
No server or Hosted endpoint provisions a grant, changes policy or activates a
profile. Native requires the new EXECUTOR_VERSION/PROTECTION_VERSION readiness;
checksum C1/C4 and M4 A18 evidence is deliberately not accepted for these versions.
The new protection witness must cover inline recovery AND M3 versions/receipts.
This first native assistant profile deliberately supports C=1 only. The existing
workspace pool still arbitrates it; this is not a second per-process capacity.

Hosted selects behavior_assistant_main.py plus BEHAVIOR_ASSISTANT_PROFILE,
BEHAVIOR_ASSISTANT_RUNTIME_PATH and the existing fixed BEHAVIOR_API_URL/AUDIENCE.
It receives neither backend storage privileges nor server JWT dependencies.
Human entry uses /assistant/submit; status/pull/ack use existing behavior routes.
Authentication precedes body reads; binding tickets stay in private headers.
"""

import asyncio
import json
import re
import logging
from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response, StreamingResponse

from . import behavior_assistant_wire as aw
from . import behavior_wire as wire
from .behavior_api import _body, _diagnostic_request_id, _log_request_failure, _principal_dependency, _response, create_behavior_app
from .behavior_assistant import AssistantAuthority, _Operation, _row, _digest, control_key
from .errors import Problem
from .behavior_http_diagnostics import model_failure, _ACQUIRE_CODES, _MODEL_CODES, _exception_type, safe_key_error
from .behavior_assistant_deadlines import SERVER_MODEL_SECONDS, ModelCall, _server_call, model_sync
from .behavior_instruction_progress import save_progress
from .behavior_sdk_checkpoint import SaveSdkState, save_state
from .behavior_sdk_state import SdkState, SDK_STATE_HTTP_BYTES, SDK_STATE_ERRORS
from .behavior_timing import phase_timing, work_timing, elapsed_timing
from .behavior_instruction_stream import LiveReplies, AnswerEvents
from .m3_skills import SKILL_ERRORS
from .behavior_skill_catalog import CATALOG_BUDGET_ERRORS
from .behavior_attachments import ATTACHMENT_ERRORS, ATTACHMENT_RESPONSE_BYTES, AttachedTexts
from .behavior_skill_resources_prompt import SKILL_RESOURCES_PROFILE
from .behavior_user_turn import ReadUserTurn
from .behavior_text_files import USER_FILE_READ_ERRORS, ReadUserFile
from .behavior_user_file_prompt import FILE_READ_PROFILES, USER_INPUT_READ_PROFILES
from .behavior_assistant_reply import ASSISTANT_REPLY_READ_ERRORS, ReadAssistantReply
from .behavior_history_prompt import REPLY_READ_PROFILES
from .behavior_model_capacity import MODEL_CAPACITY_ERRORS
from .behavior_work_memory import ReadWorkMemory, WORK_MEMORY_ERRORS, REVIEWED_NOTE_ERRORS
from .behavior_work_memory_prompt import WORK_MEMORY_PROFILE
from .behavior_reviewed_note_prompt import REVIEWED_NOTE_PROFILE
from .behavior_skill_copy import SOP_COPY_ERRORS, SOP_COPY_PROFILE
from .behavior_skill_draft import SKILL_DRAFT_ERRORS, SaveSkillDraftCommand, skill_draft_enabled
from .behavior_delegation_wire import (
    DelegationPrepare, DelegationObservation, MAX_DELEGATION_BYTES, DELEGATION_PREPARE_BYTES, DELEGATION_SIZE_REFUSAL,
)
from . import behavior_delegation_api as delegation
from . import behavior_agent_message_api as agent_messages
from .behavior_agent_message_wire import (
    AgentMessageSend, AgentMessageReadCommand, AGENT_MESSAGE_HTTP_BYTES, AGENT_MESSAGE_ERRORS,
)
from .behavior_assistant_gateway import CAPABILITY_IMAGE_BODY_BYTES
from .behavior_capability_calls import (
    CapabilityCall, capability_call, capability_catalog, capability_current, gateway_options,
)
from .behavior_capabilities import CAPABILITY_ERRORS
from .memory_candidate_wire import CandidateCommand, MEMORY_CANDIDATE_ERRORS
from .memory_candidates import stage_candidate, reconcile_candidate
from .assistant_image_wire import DeliverImageCommand, IMAGE_DELIVERY_ERRORS, MAX_IMAGE_BYTES
from .behavior_capability_prompt import skill_access

TICKET_HEADER = "x-behavior-assistant-ticket"
PREFIX = "/behavior/v1/bindings/{binding_id}/assistant"
CAPABILITY_RESPONSE_BYTES = 256 * 1024


class _ModelRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def bounded(request):
            call = ModelCall(SERVER_MODEL_SECONDS, "server_model_deadline")
            token = _server_call.set(call)
            try:
                async with asyncio.timeout(call.remaining()) as timer:
                    response = await original(request)
                    call.check()
                    return response
            except TimeoutError:
                if not timer.expired():
                    raise
                return await model_http_error(request, call.problem(), "server_model_deadline")
            except Problem as error:
                if error.body["error"]["code"] != "ASSISTANT_MODEL_DEADLINE":
                    raise
                return await model_http_error(request, error, "server_model_deadline")
            finally:
                if not isinstance(locals().get("response"), _ModelStream):
                    call.cancel()
                _server_call.reset(token)
        return bounded


class _ModelStream(StreamingResponse):
    def __init__(self, content, call):
        super().__init__(content, media_type="text/event-stream", headers={"Cache-Control":"no-store"})
        self.call = call

    async def __call__(self, scope, receive, send):
        token = _server_call.set(self.call)
        try:
            async with asyncio.timeout(self.call.remaining()):
                await super().__call__(scope, receive, send)
        finally:
            self.call.cancel()
            _server_call.reset(token)
            await self.body_iterator.aclose()



def acquire_http_error(error, phase, *, sdk_start_binding=None):
    code = error.body["error"].get("code")
    allowed = _ACQUIRE_CODES
    if sdk_start_binding is not None and sdk_start_binding.profile == "fa-sdk-start-gate-v2":
        allowed |= wire.SDK_MEMORY_REFUSAL_CODES
    code = code if type(code) is str and code in allowed else "UNCLASSIFIED"
    phase = phase if phase in {"native_evidence", "acquire_authority", "native_claim", "hosted_claim", "assistant_authority"} else "unknown"
    return HTTPException(
        status_code=error.status, detail="The original assistant acquire could not be completed.",
        headers={"Cache-Control": "no-store", "x-behavior-assistant-error-code": code,
                 "x-behavior-assistant-error-phase": phase},
    )


def _assistant_diagnostic_keys(session):
    if session is None:
        return ()
    return tuple(value for value in (
        _diagnostic_request_id(session.binding_id), _diagnostic_request_id(session.execution.work_id),
        _diagnostic_request_id(session.execution.job_id), _diagnostic_request_id(session.assignment.session_id),
    ) if value is not None)


def _log_assistant_exception(error, phase, session):
    if isinstance(error, Problem):
        return
    work_id = None if session is None else _diagnostic_request_id(session.execution.work_id)
    missing = safe_key_error(error, _assistant_diagnostic_keys(session))
    logging.getLogger(__name__).warning("ASSISTANT_EXCEPTION %s", json.dumps({
        "exception_type": _exception_type(error), "key": missing, "phase": phase, "work_id": work_id,
    }, ensure_ascii=True, separators=(",", ":")))


def _log_model_fence(session, current, condition, phase, consumed_at):
    fields = {"condition": condition, "phase": phase}
    if session is not None:
        work_id = _diagnostic_request_id(session.execution.work_id)
        if work_id is not None:
            fields["work_id"] = work_id
        if current is not None:
            fields.update(state=current.state, generation=current.execution.generation,
                          same_execution=current.execution == session.execution)
    if consumed_at is not None:
        fields["since_model_consume_ms"] = round((perf_counter() - consumed_at) * 1000, 3)
    logging.getLogger(__name__).warning("ASSISTANT_MODEL_FENCE %s",
                                      json.dumps(fields, ensure_ascii=True, separators=(",", ":")))


async def model_http_error(request, error, phase):
    details = model_failure(error, phase=phase)
    if isinstance(error, Problem):
        code = error.body["error"].get("code")
        if type(code) is str and code in _MODEL_CODES:
            details.setdefault("upstream_error_code", code)
    handler = request.app.exception_handlers[Problem if isinstance(error, Problem) else Exception]
    response = await handler(request, error)
    response.headers["Cache-Control"] = "no-store"
    response.headers.update({"x-behavior-model-" + key.replace("_", "-"): str(value)
                             for key, value in details.items()})
    return response


def create_assistant_app(authority: AssistantAuthority, verifier, *, provider=None, on_result_ready=None):
    app = create_behavior_app(authority.runtime, verifier)
    install_assistant_routes(app, authority, verifier, provider=provider, on_result_ready=on_result_ready)
    return app


def _deliver_image(authority, session, command, principal):
    from .assistant_images import deliver_image
    return deliver_image(authority, session, command, principal)


def install_assistant_routes(app, authority: AssistantAuthority, verifier, *, provider=None, on_result_ready=None):
    if not isinstance(authority, AssistantAuthority):
        raise Problem(503, "ASSISTANT_CONFIGURATION", "concrete assistant authority is required")
    identity = _principal_dependency(verifier)
    if authority.runtime.mode == "native" and provider is None:
        raise Problem(503, "ASSISTANT_MODEL_DISABLED", "native assistant requires an explicit one-send model provider")

    @app.post(PREFIX + "/submit")
    async def submit(request: Request, binding_id: str, principal=Depends(identity)):
        authority.runtime.authorize(binding_id, principal, "submit")
        command = await _body(request, aw.AssistantSubmit)
        result = await run_in_threadpool(authority.runtime.submit, binding_id, command, principal)
        return _response(result)

    @app.post(PREFIX + "/launches/{launch_id}/claim")
    async def native_claim(request: Request, binding_id: str, launch_id: str, principal=Depends(identity)):
        values = request.headers.getlist("x-behavior-report-token")
        if len(values) != 1 or re.fullmatch(r"[a-f0-9]{64}", values[0]) is None:
            raise Problem(403, "ASSISTANT_TICKET_DENIED", "original native process capability is required")
        body = await _body(request, wire.HostedProbeBody)
        command = wire.HostedProbeRequest(**body.model_dump(), report_token=SecretStr(values[0]))
        try:
            assignment, ticket = await run_in_threadpool(authority.native_claim, binding_id, launch_id, command, principal)
        except Problem as error:
            _log_request_failure(request, error.body["error"].get("code"))
            raise acquire_http_error(error, "native_claim") from None
        response = _response(aw.NativeClaimReceipt(assignment=assignment))
        response.headers[TICKET_HEADER] = ticket.get_secret_value()
        return response

    def operation(session, command, principal):
        binding = authority.runtime.bindings[session.binding_id]
        authority.runtime.execute_operation(session.binding_id, wire.Command(request_id="assistant-operation"), principal)
        op_key = control_key("operation", binding.operation_id)
        saved = _row(authority.store.read_snapshot((op_key,)), op_key, _Operation)
        return aw.EffectResult(operation_id=binding.operation_id, effect_state=saved.effect_state,
                               business_state=saved.business_state)

    def status(session, command, principal):
        digest = None
        outcome=None
        if session.state == "ready_to_finalize":
            result=authority.result(session.execution)
            digest = _digest(result.model_dump(mode="json"))
            if session.assignment.task.instruction is not None:outcome=result.state
        return aw.ToolReceipt(state=session.state, reason=session.reason, result_digest=digest,work_outcome=outcome)

    @app.post(PREFIX + "/sessions/{session_id}/acquire")
    async def acquire(request: Request, binding_id: str, session_id: str, principal=Depends(identity)):
        try:
            values = request.headers.getlist(TICKET_HEADER)
            if (len(values) != 1 or re.fullmatch(r"[a-f0-9]{64}", values[0]) is None
                    or re.fullmatch(r"[a-f0-9]{64}", session_id) is None):
                raise Problem(403, "ASSISTANT_TICKET_DENIED", "one private server-issued binding ticket is required")
            command = await _body(request, wire.Command)
            result = await run_in_threadpool(
                authority.acquire_current, binding_id, session_id, SecretStr(values[0]), command, principal)
            return _response(result)
        except Problem as error:
            raise acquire_http_error(error, "acquire_authority") from None

    if provider is not None:
        from .behavior_assistant_gateway import AssistantProvider
        if not isinstance(provider, AssistantProvider):
            raise Problem(503, "ASSISTANT_MODEL_DISABLED", "concrete bounded model provider required")

        model_routes = APIRouter(route_class=_ModelRoute)

        @model_routes.post(PREFIX + "/sessions/{session_id}/model")
        async def model(request: Request, binding_id: str, session_id: str, principal=Depends(identity)):
            phase = "model_authority"
            session = None
            consumed_at = None
            started=perf_counter()
            try:
                values = request.headers.getlist(TICKET_HEADER)
                if len(values) != 1 or re.fullmatch(r"[a-f0-9]{64}", values[0]) is None:
                    raise Problem(403, "ASSISTANT_TICKET_DENIED", "private assistant ticket is required")
                ticket = SecretStr(values[0])
                with phase_timing("model_authority",job_id=binding_id):
                    session = await model_sync(authority.model_session, binding_id, session_id, ticket, principal)
                if session.binding_id != binding_id:
                    raise Problem(403, "ASSISTANT_BINDING_DENIED", "model request is outside its assignment")
                authority.record_instruction_activity(session)
                phase = "model_request"
                free = session.assignment.task.instruction is not None
                capability_options, images = gateway_options(session)
                command = await _body(request, aw.ModelRequest, **(
                    {"max_body_bytes": CAPABILITY_IMAGE_BODY_BYTES + 64*1024} if images
                    else {"max_body_bytes": 192*1024} if free else {}))
                selected=session.assignment.task.instruction.model if free else None
                options = {"text_reply": True, "model": selected} if free else {}
                if free and session.assignment.task.instruction.model_provider is not None:
                    options["model_provider"] = session.assignment.task.instruction.model_provider
                if free and skill_access(session.assignment.task.instruction.prompt_profile,
                                         session.assignment.task.instruction.capabilities):
                    options["skill_access"] = True
                if free and session.assignment.task.instruction.prompt_profile == SKILL_RESOURCES_PROFILE:
                    options["skill_resource_access"] = True
                if free and session.assignment.task.instruction.prompt_profile in USER_INPUT_READ_PROFILES:
                    options["user_turn_access"] = True
                if free and session.assignment.task.instruction.prompt_profile in FILE_READ_PROFILES:
                    options["user_file_access"] = True
                if free and session.assignment.task.instruction.prompt_profile in REPLY_READ_PROFILES:
                    options["assistant_reply_access"] = True
                if free and session.assignment.task.instruction.prompt_profile == WORK_MEMORY_PROFILE:
                    options["work_memory_access"] = True
                if free and session.assignment.task.instruction.prompt_profile == REVIEWED_NOTE_PROFILE:
                    options["reviewed_note_access"] = True
                if free and session.assignment.task.instruction.prompt_profile == SOP_COPY_PROFILE:
                    options["sop_copy_access"] = True
                if free and skill_draft_enabled(session.assignment.task.instruction.prompt_profile,
                                                session.assignment.task.instruction.capabilities):
                    options["skill_draft_access"] = True
                options.update(capability_options)
                provider.validate(command.body, **options)
                if command.delegation is not None:
                    if command.body.get("tools"):
                        raise Problem(403, "DELEGATION_MODEL_DENIED", "the fixed child has zero tools")
                    await model_sync(delegation.validate_model, authority, session, command.delegation)
                phase = "model_consume"
                with phase_timing("model_consume",job_id=session.execution.job_id):
                    await model_sync(authority.consume_model, session, command, principal)
                    consumed_at = perf_counter()
                    if command.delegation is not None:
                        await model_sync(delegation.link_model, authority, session, command.delegation, command.request_id)
                phase = "provider_request"
                # consume's CAS gate is the last authority check before send: a revocation, cancel or source
                # change after it is refused at this job's next reserve, never by re-reading it here.
                with phase_timing("model_to_headers",job_id=session.execution.job_id):
                    result = await provider.send(command.body,
                        diagnostic_keys=_assistant_diagnostic_keys(session),
                        **options, **({"stream_response": True} if free else {}))
                if free:
                    from .behavior_instruction_cost import usage_facts,usage_from_sse,record_usage
                    wire_api = "responses" if session.assignment.task.instruction.model_provider == "openai-responses" else "completions"
                    job_id = session.execution.job_id
                    first=True
                    def append(text):
                        nonlocal first
                        if first:
                            elapsed_timing("model_first_content",started,job_id=job_id)
                            first=False
                        live.append(job_id,command.request_id,text)
                    events = None
                    if not command.checkpoint_compaction and command.delegation is None:
                        if not hasattr(request.app.state,"instruction_live_replies"):
                            request.app.state.instruction_live_replies=LiveReplies()
                        live = request.app.state.instruction_live_replies
                        live.begin(job_id, command.request_id)
                        events = AnswerEvents(append, wire_api=wire_api)
                    async def chunks():
                        raw=bytearray()
                        try:
                            with work_timing(job_id),phase_timing("model_stream"):
                                async for chunk in result.aiter_bytes():
                                    raw.extend(chunk)
                                    if events is not None:
                                        events.feed(chunk)
                                    yield chunk
                            # Observability only (TOOL-07): tokens, cached prompt tokens and tool calls per request.
                            logging.getLogger(__name__).warning("FA_MODEL_USAGE %s", json.dumps({
                                "job_id": job_id, "session_id": session.assignment.session_id,
                                "request_sha256": command.request_id, **usage_facts(bytes(raw), wire_api=wire_api),
                            }, sort_keys=True))
                            usage=usage_from_sse(bytes(raw), wire_api=wire_api)
                            if usage is not None:
                                try:
                                    with phase_timing("model_usage_save",job_id=job_id):
                                        await model_sync(record_usage,authority,session,command.request_id,usage)
                                except Problem as error:
                                    logging.getLogger(__name__).warning(
                                        "INSTRUCTION_USAGE_UNRECORDED code=%s pending_cost_not_refunded=true",
                                        error.body["error"]["code"])
                            else:
                                logging.getLogger(__name__).warning("INSTRUCTION_USAGE_UNKNOWN pending_cost_retained=true")
                        except Problem as error:
                            logging.getLogger(__name__).warning(
                                "INSTRUCTION_STREAM_UNRESOLVED code=%s pending_cost_not_refunded=true",
                                error.body["error"]["code"])
                            raise
                        finally:
                            await result.aclose()
                    return _ModelStream(chunks(),_server_call.get())
                return Response(result.content, media_type="text/event-stream", headers={"Cache-Control": "no-store"})
            except HTTPException:
                raise
            except Exception as error:
                if isinstance(error, Problem) and error.body["error"]["code"] == "ASSISTANT_EXECUTION_FENCED":
                    _log_model_fence(session, None, "authority_gate", phase, consumed_at)
                _log_assistant_exception(error, phase, session)
                return await model_http_error(request, error, phase)

        app.include_router(model_routes)

    routes = {
        "context": (wire.Command, authority.context),
        "context-origin": (wire.Command, authority.context_origin),
        "attached-text": (wire.Command, authority.attached_text),
        "skill-catalog": (wire.Command, authority.skill_catalog),
        "skill-catalog-presentation": (wire.Command, authority.skill_catalog_presentation),
        "read-skill": (aw.ReadSkill, authority.read_skill),
        "read-skill-resource": (aw.ReadSkillResource, authority.read_skill_resource),
        "read-user-turn": (ReadUserTurn, authority.read_user_turn),
        "read-user-file": (ReadUserFile, authority.read_user_file),
        "read-assistant-reply": (ReadAssistantReply, authority.read_assistant_reply),
        "work-memory-catalog": (wire.Command, authority.work_memory_catalog),
        "read-work-memory": (ReadWorkMemory, authority.read_work_memory),
        "reviewed-note-catalog": (wire.Command, authority.reviewed_note_catalog),
        "read-reviewed-note": (wire.Command, authority.read_reviewed_note),
        "copy-sop-draft": (wire.Command, authority.copy_sop_draft),
        "save-skill-draft": (SaveSkillDraftCommand, authority.save_skill_draft),
        "capability-catalog": (wire.Command, lambda s, c, p: capability_catalog(authority, s, c, p)),
        "capability-call": (CapabilityCall, lambda s, c, p: capability_call(authority, s, c, p)),
        "capability-current": (wire.Command, capability_current),
        "stage-memory-candidate": (CandidateCommand, lambda s, c, p: stage_candidate(authority, s, c, p)),
        "memory-candidate-reconcile": (CandidateCommand, lambda s, c, p: reconcile_candidate(authority, s, c, p)),
        "deliver-image": (DeliverImageCommand, lambda s, c, p: _deliver_image(authority, s, c, p)),
        "delegation-prepare": (DelegationPrepare, lambda s, c, p: delegation.prepare(authority, s, c, p)),
        "delegation-observe": (DelegationObservation, lambda s, c, p: delegation.observe(authority, s, c, p)),
        "delegation-read": (wire.Command, lambda s, c, p: delegation.read(authority, s, c, p)),
        "agent-message-send": (AgentMessageSend, lambda s, c, p: agent_messages.send(authority, s, c, p)),
        "agent-message-read": (AgentMessageReadCommand, lambda s, c, p: agent_messages.read(authority, s, c, p)),
        "reserve-model": (aw.Reservation, authority.reserve_model),
        "save": (aw.Save, authority.save),
        "reconcile": (wire.Command, lambda s, c, p: authority.reconcile_artifact(s, p)),
        "finish": (wire.Command, authority.finish),
        "save-text-reply": (aw.SaveTextReply, authority.save_text_reply),
        "progress": (aw.ProgressUpdate,lambda s,c,p:save_progress(authority,s,c)),
        "operation": (wire.Command, operation),
        "stopped": (aw.TurnStopped, lambda s, c, p: authority.stopped(s, c)),
        "status": (wire.Command, status),
        "checkpoint": (aw.SaveCheckpoint, lambda s, c, p: authority.checkpoint(s, c)),
        "restore": (wire.Command, lambda s, c, p: authority.restore(s, p)),
        "resident-restore": (aw.ResidentRestore, lambda s, c, p: authority.resident_restore(s, c, p)),
        "sdk-state": (SaveSdkState, lambda s,c,p:save_state(authority,s,c)),
    }
    if on_result_ready is not None:
        def execution_ready(session, command, principal):
            if session.assignment.task.instruction is None or session.state != "ready_to_finalize":
                raise Problem(409, "INSTRUCTION_RESULT_FENCED", "only the saved original result can signal completion")
            return status(session, command, principal)
        routes["execution-ready"] = (wire.Command, execution_ready)
    command_ids = {
        "context": "current-context", "save": "own-result",
        "context-origin": "current-context",
        "attached-text": "current-attachments",
        "reconcile": "artifact-reconcile", "finish": "finish-task", "operation": "operation",
        "stopped": "turn-stopped", "status": "status", "checkpoint": "sdk-checkpoint", "restore": "sdk-restore",
        "resident-restore": "sdk-resident-restore",
        "save-text-reply": "save-text-reply",
        "execution-ready": "execution-ready",
        "sdk-state": "sdk-state",
        "skill-catalog": "current-skills", "read-skill": "read-skill",
        "read-skill-resource": "read-skill-resource",
        "skill-catalog-presentation": "skill-catalog-presentation",
        "read-user-turn": "read-user-turn",
        "read-user-file": "read-user-file",
        "read-assistant-reply": "read-assistant-reply",
        "work-memory-catalog": "current-work-memory", "read-work-memory": "read-work-memory",
        "copy-sop-draft": "copy-sop-draft",
        "save-skill-draft": "save-skill-draft",
        "capability-catalog": "capability-catalog", "capability-call": "capability-call",
        "capability-current": "capability-current",
        "stage-memory-candidate": "stage-memory-candidate", "memory-candidate-reconcile": "stage-memory-candidate",
        "deliver-image": "deliver-image",
        "delegation-prepare": "delegation-prepare", "delegation-observe": "delegation-observe",
        "delegation-read": "delegation-read",
        "agent-message-send": "agent-message-send", "agent-message-read": "agent-message-read",
    }
    for suffix, (model, handler) in routes.items():
        # sdk-state gates inside its own transactions, where a cancelled turn may still save its stopped SDK state.
        def endpoint(model=model, handler=handler, active=suffix not in {
                         "status","save-text-reply","execution-ready","delegation-observe","delegation-read",
                         "agent-message-read", "sdk-state"},
                     expected_id=command_ids.get(suffix), notify=suffix=="execution-ready", suffix_name=suffix):
            async def invoke(request: Request, binding_id: str, session_id: str, principal=Depends(identity)):
                values = request.headers.getlist(TICKET_HEADER)
                if (len(values) != 1 or re.fullmatch(r"[a-f0-9]{64}", values[0]) is None
                        or re.fullmatch(r"[a-f0-9]{64}", session_id) is None):
                    raise Problem(403, "ASSISTANT_TICKET_DENIED", "one private server-issued binding ticket is required")
                try:
                    if suffix_name in {"reserve-model", "progress"}:
                        # Both handlers write through _command, whose CAS _gate is the full check.
                        session = await run_in_threadpool(authority.model_session, binding_id, session_id,
                                                         SecretStr(values[0]), principal)
                    else:
                        session = await run_in_threadpool(authority.current_session, binding_id, session_id,
                                                         SecretStr(values[0]), principal, active=active)
                except Exception as error:
                    if suffix_name in {"reserve-model", "restore"}:
                        _log_assistant_exception(error, "session_authority", None)
                    raise
                if session.binding_id != binding_id:
                    raise Problem(403, "ASSISTANT_BINDING_DENIED", "ticket is bound to another assignment")
                limits=({"max_body_bytes":SDK_STATE_HTTP_BYTES} if model is SaveSdkState else
                        {"max_body_bytes":AGENT_MESSAGE_HTTP_BYTES} if model in {AgentMessageSend,AgentMessageReadCommand} else
                        {"max_body_bytes":DELEGATION_PREPARE_BYTES} if model is DelegationPrepare else
                        {"max_body_bytes":MAX_DELEGATION_BYTES} if model is DelegationObservation else
                        {"max_body_bytes":4*1024*1024} if model is aw.SaveTextReply else
                        {"max_body_bytes":MAX_IMAGE_BYTES*4//3+4096} if model is DeliverImageCommand else {})
                try:
                    command = await _body(request, model, **limits)
                except Problem as error:
                    if (model is DelegationPrepare and error.status == 413
                            and error.body["error"]["code"] == "BEHAVIOR_BODY_LIMIT"):
                        raise HTTPException(413, detail="Delegation material size exceeds 192 KiB; no intent was reserved.",
                            headers={"Cache-Control": "no-store", "x-behavior-assistant-error-code": DELEGATION_SIZE_REFUSAL,
                                     "x-behavior-delegation-stage": "before-reservation"}) from None
                    if model is SaveSdkState and error.body["error"]["code"] in SDK_STATE_ERRORS:
                        raise acquire_http_error(error, "assistant_authority") from None
                    raise
                if expected_id is not None and command.request_id != expected_id:
                    raise Problem(422, "ASSISTANT_COMMAND_ID", "the fixed bounded command ID is required")
                try:
                    result = await run_in_threadpool(handler, session, command, principal)
                    if suffix_name in {"progress", "agent-message-send", "agent-message-read"}:
                        authority.record_instruction_activity(session)
                    if notify:on_result_ready(binding_id)
                    if (on_result_ready is not None and suffix_name in {"agent-message-send", "agent-message-read"}
                            and result.message.kind in {"request", "handoff"}):
                        on_result_ready(binding_id)
                    response_limits = ({"max_bytes": SDK_STATE_HTTP_BYTES} if isinstance(result, SdkState)
                                       else {"max_bytes": AGENT_MESSAGE_HTTP_BYTES} if suffix_name.startswith("agent-message-")
                                       else {"max_bytes": MAX_DELEGATION_BYTES} if suffix_name.startswith("delegation-")
                                       else {"max_bytes": ATTACHMENT_RESPONSE_BYTES} if isinstance(result, AttachedTexts)
                                       else {"max_bytes": CAPABILITY_RESPONSE_BYTES} if suffix_name in {
                                           "capability-catalog", "capability-call"} else {})
                    return _response(result, **response_limits)
                except Problem as error:
                    response_error = acquire_http_error(error, "assistant_authority",
                                                        sdk_start_binding=session.assignment.sdk_start_binding)
                    if suffix_name in {"agent-message-send", "agent-message-read", "reserve-model"}:
                        _log_request_failure(request, error.body["error"].get("code"))
                    raise response_error from None
                except Exception as error:
                    if suffix_name in {"reserve-model", "restore"}:
                        _log_assistant_exception(error, suffix_name, session)
                    # Only the route and exception class; messages may carry request data.
                    logging.getLogger(__name__).warning("ASSISTANT_DEPENDENCY_FAILED %s", json.dumps(
                        {"action": suffix_name, "exception_type": type(error).__name__}, sort_keys=True))
                    raise Problem(503, "ASSISTANT_DEPENDENCY_FAILED", "typed assistant operation failed") from None
            return invoke
        app.add_api_route(PREFIX + "/sessions/{session_id}/" + suffix, endpoint(), methods=["POST"])
