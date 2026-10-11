from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Callable

from fastapi import Depends, FastAPI, Header, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .auth import Actor
from .backup_api import add_backup_routes, is_backup_upload
from .backup_store import BackupStore
from .errors import Problem
from .m2_gateway import M2Gateway, add_model_routes, is_model_upload
from .models import CreateWork, EmptyBody, Handoff, SearchMemory, SyntheticMemory, UpdateWork
from .service import ContextService
from .storage import Record

if TYPE_CHECKING:
    from .m3_artifacts import ArtifactService
    from .m4_context import M4ContextService

MAX_REQUEST_BYTES = 64 * 1024


class BoundedBody:
    def __init__(self, app, backup_enabled: bool = False, model_enabled: bool = False,
                 artifact_enabled: bool = False, m3_model_enabled: bool = False,
                 m4_enabled: bool = False, m4_model_enabled: bool = False):
        self.app = app
        self.backup_enabled = backup_enabled
        self.model_enabled = model_enabled
        self.artifact_enabled = artifact_enabled
        self.m3_model_enabled = m3_model_enabled
        self.m4_enabled = m4_enabled
        self.m4_model_enabled = m4_model_enabled

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        original_send = send
        model_upload = (self.model_enabled and is_model_upload(scope)) or (
            self.m3_model_enabled and is_model_upload(scope, milestone="m3")
        ) or (
            self.m4_model_enabled and is_model_upload(scope, milestone="m4")
        )
        m4_upload = False
        if self.m4_enabled:
            from .m4_api import is_m4_upload

            m4_upload = is_m4_upload(scope)
        artifact_upload = False
        if self.artifact_enabled:
            from .m3_api import is_artifact_upload

            artifact_upload = is_artifact_upload(scope)

        async def send(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message["headers"] = headers + [
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                ]
                if model_upload or artifact_upload or m4_upload:
                    message["headers"].append((b"x-should-retry", b"false"))
            await original_send(message)

        if (self.backup_enabled and is_backup_upload(scope)) or model_upload or artifact_upload or m4_upload:
            # These routes authenticate before their own bounded body readers.
            await self.app(scope, receive, send)
            return

        parts = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_REQUEST_BYTES:
                await JSONResponse(status_code=413, content={
                    "error": {"code": "REQUEST_TOO_LARGE", "message": "request exceeds 64 KiB"},
                })(scope, receive, send)
                return
            parts.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(parts)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(service: ContextService, verified_identity_dependency: Callable, close: Callable | None = None,
               backup_store: BackupStore | None = None, *, model_gateway: M2Gateway | None = None,
               artifact_service: ArtifactService | None = None,
               verified_artifact_identity_dependency: Callable | None = None,
               artifact_policy_ids: tuple[str, ...] = (), m3_model_gateway: M2Gateway | None = None,
               m4_service: M4ContextService | None = None, m4_model_gateway: M2Gateway | None = None) -> FastAPI:
    """Internal composition seam for unit tests; production always installs JwtVerifier."""
    if (artifact_service is None) != (verified_artifact_identity_dependency is None):
        raise ValueError("artifact storage and its explicit identity verifier must be configured together")
    if artifact_policy_ids and artifact_service is None:
        raise ValueError("active policy configuration requires the M3 module")
    if m3_model_gateway is not None and (artifact_service is None or m3_model_gateway.milestone != "m3"):
        raise ValueError("M3 model inference requires its own gateway and artifact module")
    if model_gateway is not None and model_gateway.milestone != "m2":
        raise ValueError("the existing model gateway slot is reserved for M2")
    if (m4_service is None) != (m4_model_gateway is None):
        raise ValueError("M4 context and its model gateway must be configured together")
    if m4_service is not None and (
        artifact_service is None or m4_service.context is not service or m4_service.artifacts is not artifact_service
        or m4_model_gateway.milestone != "m4" or m4_model_gateway.service is not service
        or m4_model_gateway.settings != m4_service.settings.model_settings
    ):
        raise ValueError("M4 requires the existing context, pinned artifact module and its own matching gateway")

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            try:
                if model_gateway is not None:
                    await model_gateway.aclose()
            finally:
                try:
                    if m3_model_gateway is not None:
                        await m3_model_gateway.aclose()
                finally:
                    try:
                        if m4_model_gateway is not None:
                            await m4_model_gateway.aclose()
                    finally:
                        if close:
                            close()

    app = FastAPI(title="Shared Context POC", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(BoundedBody, backup_enabled=backup_store is not None, model_enabled=model_gateway is not None,
                       artifact_enabled=artifact_service is not None, m3_model_enabled=m3_model_gateway is not None,
                       m4_enabled=m4_service is not None, m4_model_enabled=m4_model_gateway is not None)
    identity = Depends(verified_identity_dependency)

    @app.exception_handler(Problem)
    async def problem_handler(_request, exc: Problem):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else {}
        return JSONResponse(status_code=exc.status, content=exc.body, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request, _exc):
        return JSONResponse(status_code=422, content={
            "error": {"code": "INVALID_REQUEST", "message": "invalid fields, types, IDs, or lengths"},
        })

    @app.get("/healthz")
    def healthz():
        return {"status": "healthy"}

    def result(record: Record, response: Response):
        response.headers["ETag"] = record.etag
        response.headers["Cache-Control"] = "no-store"
        return record.response()

    @app.post("/teams/{team}/works/{work}", status_code=201)
    def create_work(team: str, work: str, body: CreateWork, response: Response, actor: Actor = identity):
        return result(service.create(actor, team, work, body), response)

    @app.get("/teams/{team}/works/{work}")
    def get_work(team: str, work: str, response: Response, actor: Actor = identity):
        return result(service.get(actor, team, work), response)

    @app.patch("/teams/{team}/works/{work}")
    def update_work(team: str, work: str, body: UpdateWork, response: Response,
                    if_match: Annotated[str | None, Header()] = None, actor: Actor = identity):
        return result(service.update(actor, team, work, if_match, body), response)

    @app.post("/teams/{team}/works/{work}/handoff")
    def handoff(team: str, work: str, body: Handoff, response: Response,
                if_match: Annotated[str | None, Header()] = None, actor: Actor = identity):
        return result(service.handoff(actor, team, work, if_match, body), response)

    @app.post("/teams/{team}/works/{work}/publish")
    def publish(team: str, work: str, response: Response, body: EmptyBody | None = None,
                if_match: Annotated[str | None, Header()] = None, actor: Actor = identity):
        return result(service.publish(actor, team, work, if_match), response)

    @app.post("/teams/{team}/memory/team", status_code=202)
    def write_team_memory(team: str, body: SyntheticMemory, actor: Actor = identity):
        return service.start_memory(actor, team, None, body)

    @app.post("/teams/{team}/memory/private/{target}", status_code=202)
    def write_private_memory(team: str, target: str, body: SyntheticMemory, actor: Actor = identity):
        return service.start_memory(actor, team, target, body)

    @app.post("/teams/{team}/memory/team/search")
    def read_team_memory(team: str, body: SearchMemory, actor: Actor = identity):
        return service.search(actor, team, None, body.query)

    @app.post("/teams/{team}/memory/private/{target}/search")
    def read_private_memory(team: str, target: str, body: SearchMemory, actor: Actor = identity):
        return service.search(actor, team, target, body.query)

    @app.get("/teams/{team}/memory/operations/{operation}")
    def poll_memory(team: str, operation: str, actor: Actor = identity):
        return service.poll_memory(actor, team, operation)

    @app.get("/teams/{team}/works/{work}/context")
    def context(team: str, work: str, actor: Actor = identity):
        context = service.assemble(actor, team, work)
        if artifact_service is not None and artifact_policy_ids and team == artifact_service.approval.team:
            context["approved_policy_context"] = artifact_service.policy_context(actor, team, artifact_policy_ids)
        return context

    if backup_store is not None:
        add_backup_routes(app, service, verified_identity_dependency, backup_store)
    if model_gateway is not None:
        add_model_routes(app, verified_identity_dependency, model_gateway)
    if m3_model_gateway is not None:
        add_model_routes(app, verified_identity_dependency, m3_model_gateway)
    if m4_service is not None and m4_model_gateway is not None:
        from .m4_api import add_m4_routes

        add_m4_routes(app, m4_service, verified_identity_dependency)
        add_model_routes(app, verified_identity_dependency, m4_model_gateway)
    if artifact_service is not None and verified_artifact_identity_dependency is not None:
        from .m3_api import add_artifact_routes

        add_artifact_routes(app, artifact_service, verified_artifact_identity_dependency)

    return app
