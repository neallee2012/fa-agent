"""The original human may inspect suggestions; reading never shares or activates them."""

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import TypeAdapter

from .behavior_api import _principal_dependency, _response
from .behavior_wire import Identifier
from .errors import Problem
from .memory_candidates import MemoryCandidates


def install_memory_candidate_routes(app, runtime, verifier):
    router = APIRouter()
    identity = _principal_dependency(verifier)
    service = MemoryCandidates(runtime)

    @router.get("/behavior/v1/bindings/{binding_id}/memory-candidate")
    async def read(request: Request, binding_id: str, principal=Depends(identity)):
        try:
            TypeAdapter(Identifier).validate_python(binding_id, strict=True)
            if (request.query_params or request.headers.get("content-length", "0") != "0"
                    or "transfer-encoding" in request.headers):
                raise ValueError("candidate lookup takes no body or query")
        except (ValueError, TypeError):
            raise Problem(422, "MEMORY_CANDIDATE_INVALID_REQUEST",
                          "only the original binding is accepted") from None
        return _response(await run_in_threadpool(service.read, binding_id, principal))

    app.include_router(router)
