"""Original-human settings for future conversation memory; no model or memory writer."""

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool

from .behavior_api import _body, _principal_dependency, _response
from .errors import Problem
from .memory_bridge_settings import MemoryBridgeSettingsService, SelectMemoryBridgeSettings


def install_memory_bridge_settings_routes(app, runtime, verifier):
    router = APIRouter()
    identity = _principal_dependency(verifier)
    service = MemoryBridgeSettingsService(runtime)
    prefix = "/operator/v1/memory-bridge-settings"

    @router.get(prefix)
    async def current(request: Request, principal=Depends(identity)):
        if request.query_params or await request.body():
            raise Problem(422, "BEHAVIOR_INVALID_REQUEST", "settings lookup accepts no body or query")
        return _response(await run_in_threadpool(service.current, principal))

    def add(path, operation):
        async def endpoint(request: Request, principal=Depends(identity)):
            command = await _body(request, SelectMemoryBridgeSettings)
            return _response(await run_in_threadpool(operation, principal, command))
        router.add_api_route(prefix + path, endpoint, methods=["POST"])

    add("", service.select)
    add("/preview", service.preview)
    add("/reconcile", service.reconcile)
    app.include_router(router)
