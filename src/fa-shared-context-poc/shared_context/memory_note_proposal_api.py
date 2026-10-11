"""Configured-human routes for pending note proposals; no effective-memory writer."""

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import TypeAdapter

from .behavior_api import _body, _principal_dependency, _response
from .behavior_wire import Identifier
from .errors import Problem
from .memory_note_proposals import NoteProposalCommand, NoteProposalService
from .memory_note_proposal_bridge import NoteBridgeCommand, NoteProposalBridge, ReconcileNoteBridge, ReviewNoteBridge


def install_note_proposal_routes(app, runtime, verifier):
    router = APIRouter()
    identity = _principal_dependency(verifier)
    service = NoteProposalService(runtime)
    bridge = NoteProposalBridge(runtime)
    prefix = "/behavior/v1/bindings/{binding_id}/note-proposals"

    def path_identity(request):
        try:
            for value in request.path_params.values():
                TypeAdapter(Identifier).validate_python(value, strict=True)
            if request.query_params:
                raise ValueError("query parameters are unsupported")
        except (ValueError, TypeError):
            raise Problem(422, "NOTE_PROPOSAL_INVALID_REQUEST", "only the original identifiers are supported") from None

    def add(path, operation):
        async def endpoint(request: Request, binding_id: str, principal=Depends(identity)):
            path_identity(request)
            command = await _body(request, NoteProposalCommand)
            return _response(await run_in_threadpool(operation, binding_id, command, principal))
        router.add_api_route(prefix + path, endpoint, methods=["POST"])

    add("/preview", service.preview)
    add("/reconcile", service.reconcile)
    add("", service.create)

    def add_bridge(stage, model, operation):
        async def endpoint(request: Request, binding_id: str, proposal_request_id: str, principal=Depends(identity)):
            path_identity(request)
            command = await _body(request, model)
            return _response(await run_in_threadpool(operation, binding_id, proposal_request_id, command, principal))
        router.add_api_route(prefix + "/{proposal_request_id}/bridge/" + stage, endpoint, methods=["POST"])

    add_bridge("prepare", NoteBridgeCommand, bridge.prepare)
    add_bridge("preview", NoteBridgeCommand, bridge.preview)
    add_bridge("review", ReviewNoteBridge, bridge.review)
    add_bridge("commit", NoteBridgeCommand, bridge.commit)
    add_bridge("reconcile", ReconcileNoteBridge, bridge.reconcile)

    @router.get(prefix + "/{request_id}")
    async def read(request: Request, binding_id: str, request_id: str, principal=Depends(identity)):
        path_identity(request)
        if request.headers.get("content-length", "0") != "0" or "transfer-encoding" in request.headers:
            raise Problem(422, "NOTE_PROPOSAL_INVALID_REQUEST", "proposal reads do not accept a body")
        return _response(await run_in_threadpool(service.read, binding_id, request_id, principal))

    app.include_router(router)
    install_note_synthesis_routes(app, runtime, verifier)


def install_note_synthesis_routes(app, runtime, verifier):
    from .memory_note_synthesis import NoteSynthesisBridge, NoteSynthesisCommand, NoteSynthesisService

    router = APIRouter()
    identity = _principal_dependency(verifier)
    service = NoteSynthesisService(runtime)
    bridge = NoteSynthesisBridge(runtime)
    prefix = "/behavior/v1/bindings/{binding_id}/note-syntheses"

    def path_identity(request):
        try:
            for value in request.path_params.values():
                TypeAdapter(Identifier).validate_python(value, strict=True)
            if request.query_params:
                raise ValueError("query parameters are unsupported")
        except (ValueError, TypeError):
            raise Problem(422, "NOTE_SYNTHESIS_INVALID_REQUEST", "only the original identifiers are supported") from None

    def add(path, operation):
        async def endpoint(request: Request, binding_id: str, principal=Depends(identity)):
            path_identity(request)
            command = await _body(request, NoteSynthesisCommand)
            return _response(await run_in_threadpool(operation, binding_id, command, principal))
        router.add_api_route(prefix + path, endpoint, methods=["POST"])

    add("/preview", service.preview)
    add("/reconcile", service.reconcile)
    add("", service.create)

    def add_bridge(stage, model, operation):
        async def endpoint(request: Request, binding_id: str, proposal_request_id: str, principal=Depends(identity)):
            path_identity(request)
            command = await _body(request, model)
            return _response(await run_in_threadpool(operation, binding_id, proposal_request_id, command, principal))
        router.add_api_route(prefix + "/{proposal_request_id}/bridge/" + stage, endpoint, methods=["POST"])

    add_bridge("prepare", NoteBridgeCommand, bridge.prepare)
    add_bridge("preview", NoteBridgeCommand, bridge.preview)
    add_bridge("review", ReviewNoteBridge, bridge.review)
    add_bridge("commit", NoteBridgeCommand, bridge.commit)
    add_bridge("reconcile", ReconcileNoteBridge, bridge.reconcile)

    @router.get(prefix + "/{request_id}")
    async def read(request: Request, binding_id: str, request_id: str, principal=Depends(identity)):
        path_identity(request)
        if request.headers.get("content-length", "0") != "0" or "transfer-encoding" in request.headers:
            raise Problem(422, "NOTE_SYNTHESIS_INVALID_REQUEST", "synthesis reads do not accept a body")
        return _response(await run_in_threadpool(service.read, binding_id, request_id, principal))

    app.include_router(router)
