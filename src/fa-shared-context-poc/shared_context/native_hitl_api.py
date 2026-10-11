"""Default-off API control routes; no dispatch through an Agent's occupied turn queue."""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _log_request_failure, _principal_dependency, _response
from .behavior_team_registry import resolve_agent_scope
from .errors import Problem
from .native_hitl import NativeHitlService
from .native_hitl_attention import owner_origin_reader, read_team_attention
from .native_hitl_wire import (
    NATIVE_HITL_ERRORS, NativeHitlClose, NativeHitlDecision, NativeHitlPendingCreate, NativeHitlRuleChange,
    NativeHitlAuthorize, NativeHitlEnd, NativeHitlPendingQuery, NATIVE_HITL_PREFIX, ENABLED_ENV, TTL_ENV,
    hitl_settings, NativeHitlAnswer, NativeHitlQuestionCreate, ConnectorConsentCreate, ConnectorConsentDecision,
    NATIVE_HITL_BODY_BYTES, NATIVE_HITL_HTTP_BYTES,
)
from .native_routine_wire import NATIVE_ROUTINE_ERRORS
from .records import Record

PREFIX = NATIVE_HITL_PREFIX


def _ids(call):
    return {"agent_id": call.agent_id, "conversation_id": call.conversation_id, "request_id": call.request_id}


def _hint(publisher, service, path, principal, command, value):
    """Realtime hint after an acknowledged write: IDs only. A decision, observation or answer reads its original
    card on the publisher thread; if that read fails, the hint still names the card."""
    result = value.payload["result"] if isinstance(value, Record) else {}
    team, status = principal.team, result.get("status")
    if path == "/pending":
        publisher.notify(team, "hitl.approval", **_ids(command.action.call),
                         pending_id=result.get("pending_id"), status=status)
    elif path == "/questions":
        publisher.notify(team, "hitl.question", **_ids(command.question.call),
                         question_id=result.get("question_id"), status=status)
    elif path == "/end":
        # Hosted sends /end once per turn; only a turn that had a card or question has anything to close.
        publisher.notify(team, "hitl.closed", **_ids(command.turn),
                         build=lambda: {} if service.had_waits(principal, command.turn) else None)
    elif path in {"/decisions", "/observations"}:
        publisher.notify(team, "hitl.approval", pending_id=command.pending_id, status=status,
                         build=lambda: _ids(service.original_turn(command.pending_id)))
    elif path == "/answers":
        publisher.notify(team, "hitl.question", question_id=command.question_id, status=status,
                         build=lambda: _ids(service.original_turn(command.question_id, question=True)))
    elif path == "/consents":
        publisher.notify(team, "hitl.consent", **_ids(command.consent.call),
                         consent_id=result.get("consent_id"), status=status)
    elif path == "/consent-decisions":
        publisher.notify(team, "hitl.consent", consent_id=command.consent_id, status=status,
                         build=lambda: _ids(service.original_turn(command.consent_id, consent=True)))


def install_native_hitl_routes(app, composition, *, environ=None):
    enabled, ttl, interactive_ttl = hitl_settings(environ)
    if not enabled:
        return
    store = composition.runtime.store
    service = NativeHitlService(
        store, composition.verifier, clock=composition.runtime.clock, ttl_seconds=ttl,
        interactive_ttl_seconds=interactive_ttl,
        resolve_agent=lambda agent_id: resolve_agent_scope(store, composition.config, agent_id))
    app.state.native_hitl = service
    from .native_routine_proposals import RoutineProposalDecisions
    proposals = RoutineProposalDecisions(service, composition, environ=environ)
    router = APIRouter()
    identity = _principal_dependency(composition.verifier)
    from .realtime_push import realtime_publisher
    publisher = realtime_publisher(composition, environ=environ)

    async def reply(request, call, *, after=None):
        try:
            value = await run_in_threadpool(call)
        except Problem as error:
            code = error.body["error"]["code"]
            if code not in NATIVE_HITL_ERRORS and code not in NATIVE_ROUTINE_ERRORS:
                raise
            _log_request_failure(request, code)
            return JSONResponse(status_code=error.status, headers={"Cache-Control": "no-store"}, content={"error": {
                "code": code, "message": "The original approval command could not be completed; read its saved state.",
                "reconcile_original": False, "automatic_retry": False,
            }})
        response = _response(value, max_bytes=NATIVE_HITL_HTTP_BYTES)
        if hasattr(value, "etag") and value.etag is not None:
            response.headers["ETag"] = value.etag
        if after is not None:
            after(value)
        return response

    async def no_body(request, *, queries=()):
        if (set(request.query_params) - set(queries)
                or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params)):
            raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "approval reads take no query or body")
        async for chunk in request.stream():
            if chunk:
                raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "approval reads take no query or body")

    @router.get(PREFIX + "/attention")
    async def attention(
        request: Request, cursor: str | None = Query(None), limit: int = Query(20, ge=1, le=20),
        principal=Depends(identity),
    ):
        service._authorize(principal, owner=True)
        await no_body(request, queries=("cursor", "limit"))
        return await reply(request, lambda: read_team_attention(
            service, principal, cursor=cursor, limit=limit,
            resolve_origin=owner_origin_reader(composition, principal)))

    @router.get(PREFIX + "/rules")
    async def rules(request: Request, principal=Depends(identity)):
        service._authorize(principal)
        await no_body(request)
        return await reply(request, lambda: service.rules(principal))

    @router.get(PREFIX + "/pending/{pending_id}")
    async def pending(request: Request, pending_id: str, principal=Depends(identity)):
        service._authorize(principal)
        await no_body(request)
        return await reply(request, lambda: service.read(principal, pending_id))

    @router.post(PREFIX + "/turn-pending")
    async def turn_pending(request: Request, principal=Depends(identity)):
        service._authorize(principal)
        command = await _body(request, NativeHitlPendingQuery)
        return await reply(request, lambda: service.pending(principal, command))

    @router.get(PREFIX + "/questions/{question_id}")
    async def question(request: Request, question_id: str, principal=Depends(identity)):
        service._authorize(principal)
        await no_body(request)
        return await reply(request, lambda: service.read_question(principal, question_id))

    @router.post(PREFIX + "/turn-questions")
    async def turn_questions(request: Request, principal=Depends(identity)):
        service._authorize(principal)
        command = await _body(request, NativeHitlPendingQuery)
        return await reply(request, lambda: service.questions(principal, command))

    @router.get(PREFIX + "/consents/{consent_id}")
    async def consent(request: Request, consent_id: str, principal=Depends(identity)):
        service._authorize(principal)
        await no_body(request)
        return await reply(request, lambda: service.read_consent(principal, consent_id))

    @router.post(PREFIX + "/turn-consents")
    async def turn_consents(request: Request, principal=Depends(identity)):
        service._authorize(principal)
        command = await _body(request, NativeHitlPendingQuery)
        return await reply(request, lambda: service.consents(principal, command))

    def add(path, model, *, owner, approve=None, before=None):
        def endpoint(reconcile):
            async def handler(request: Request, principal=Depends(identity)):
                service._authorize(principal, owner=owner, write=not reconcile, approve=approve)
                if not owner and principal.kind != "agent":
                    raise Problem(403, "NATIVE_HITL_AGENT_ONLY", "only the original Agent can propose or close a call")
                command = await _body(request, model, max_body_bytes=NATIVE_HITL_BODY_BYTES)
                operation = service.reconcile if reconcile else service.submit
                after = (None if reconcile or publisher is None
                         else lambda value: _hint(publisher, service, path, principal, command, value))

                def run():
                    if before is not None and not reconcile:
                        before(principal, command)
                    return operation(principal, command)
                return await reply(request, run, after=after)
            return handler
        for reconcile in (False, True):
            router.add_api_route(PREFIX + path + ("/reconcile" if reconcile else ""),
                                 endpoint(reconcile), methods=["POST"])

    add("/rules", NativeHitlRuleChange, owner=True)
    add("/pending", NativeHitlPendingCreate, owner=False)
    # An allow on a routine card first writes that card's own daily definition (native_routine_proposals).
    add("/decisions", NativeHitlDecision, owner=True, before=proposals.before)
    add("/observations", NativeHitlClose, owner=False)
    add("/authorize", NativeHitlAuthorize, owner=False)
    add("/end", NativeHitlEnd, owner=False)
    add("/questions", NativeHitlQuestionCreate, owner=False)
    # B1 PA-11: the owner's ordinary reply, not an approval; /decisions still requires Behavior.Approve.
    add("/answers", NativeHitlAnswer, owner=True, approve=False)
    # C3: the owner's own Connect card click (resume once or decline); like an answer, not an approval.
    add("/consents", ConnectorConsentCreate, owner=False)
    add("/consent-decisions", ConnectorConsentDecision, owner=True, approve=False)
    app.include_router(router)
