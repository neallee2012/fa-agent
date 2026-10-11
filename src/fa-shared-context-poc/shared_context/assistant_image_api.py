"""The original conversation human may read screenshots delivered with a reply; no public URL is created."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import TypeAdapter

from .assistant_image_wire import MAX_IMAGE_BYTES
from .assistant_images import image_bytes, turn_images
from .behavior_api import _principal_dependency, _response
from .behavior_wire import Identifier
from .errors import Problem


def _checked(request, binding_id, version_id=None):
    try:
        TypeAdapter(Identifier).validate_python(binding_id, strict=True)
        if version_id is not None and (len(version_id) != 32 or any(c not in "0123456789abcdef" for c in version_id)):
            raise ValueError("a delivered screenshot version ID is required")
        if (request.query_params or request.headers.get("content-length", "0") != "0"
                or "transfer-encoding" in request.headers):
            raise ValueError("screenshot reads take no body or query")
    except (ValueError, TypeError):
        raise Problem(422, "IMAGE_DELIVERY_INVALID_REQUEST", "only the original binding and version are accepted") from None


def install_image_routes(app, authority, verifier):
    router = APIRouter()
    identity = _principal_dependency(verifier)

    @router.get("/behavior/v1/bindings/{binding_id}/images")
    async def listing(request: Request, binding_id: str, principal=Depends(identity)):
        _checked(request, binding_id)
        return _response(await run_in_threadpool(turn_images, authority, binding_id, principal))

    @router.get("/behavior/v1/bindings/{binding_id}/images/{version_id}")
    async def image(request: Request, binding_id: str, version_id: str, principal=Depends(identity)):
        _checked(request, binding_id, version_id)
        entry, data = await run_in_threadpool(image_bytes, authority, binding_id, version_id, principal)
        if len(data) > MAX_IMAGE_BYTES:
            raise Problem(502, "M3_FILE_INTEGRITY", "a delivered screenshot exceeds its bound")
        return Response(data, media_type="image/png", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="screenshot-{entry.number}.png"',
            "x-content-sha256": entry.sha256,
        })

    app.include_router(router)
