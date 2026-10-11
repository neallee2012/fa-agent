"""Isolated delegated-operator transport; no business routes or implicit apply."""
import argparse
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field, TypeAdapter, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _principal_dependency, _response
from .behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
from .behavior_foundry import NativeExecutionGrant
from .behavior_migration import MigrationStore
from .behavior_runtime_transition import RuntimeTransition, RuntimeTransitionAuthorization, RuntimeTransitionPlan
from .behavior_server_config import ServerConfiguration, read_document
from .behavior_wire import WireModel
from .errors import Problem
from .models import Identifier


class _Empty(WireModel):
    pass


class _Authorize(WireModel):
    plan: RuntimeTransitionPlan
    decision_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def explicit_utc(cls, value):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("operator authorization requires explicit UTC")
        return value


class _Apply(WireModel):
    plan: RuntimeTransitionPlan
    authorization: RuntimeTransitionAuthorization


def create_transition_app(service, verifier, *, allow_apply=False):
    if not isinstance(service, RuntimeTransition) or not isinstance(verifier, BehaviorJwtVerifier):
        raise ValueError("exact runtime transition and delegated identity verifier required")
    identity = service.old.identity
    if (verifier.auth.tenant_id != identity.tenant_id or verifier.auth.audience != identity.audience
            or verifier.settings.human_oid != identity.human_oid or verifier.settings.team != identity.team_id
            or verifier.settings.client_id != identity.admin_client_id):
        raise ValueError("operator verifier differs from the pinned transition")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    authenticate = _principal_dependency(verifier)

    @app.exception_handler(Problem)
    async def problem(_request, error):
        return JSONResponse(error.body, status_code=error.status, headers={"Cache-Control": "no-store"})

    def add(stage, model):
        async def endpoint(request: Request, transition_id: str, principal=Depends(authenticate)):
            service._operator(principal)
            try:
                TypeAdapter(Identifier).validate_python(transition_id, strict=True)
            except ValidationError:
                raise Problem(422, "TRANSITION_REQUEST_INVALID", "invalid transition identifier") from None
            command = await _body(request, model)
            if stage == "apply" and not allow_apply:
                raise Problem(403, "TRANSITION_APPLY_DISABLED", "this operator deployment is review-only")
            if stage != "plan" and command.plan.transition_id != transition_id:
                raise Problem(409, "TRANSITION_REQUEST_MISMATCH", "path and approved transition differ")
            def invoke():
                if stage == "plan":
                    return service.plan(principal, transition_id=transition_id)
                if stage == "authorize":
                    return service.authorize(principal, command.plan, decision_id=command.decision_id,
                                             expires_at=command.expires_at)
                return getattr(service, stage)(principal, command.plan, command.authorization)
            try:
                return _response(await run_in_threadpool(invoke))
            except Problem:
                raise
            except Exception:
                raise Problem(503, "TRANSITION_DEPENDENCY_UNKNOWN",
                              "operator outcome is unknown; reconcile the original transition, never blindly reapply") from None
        app.add_api_route("/operator/v1/runtime-transitions/{transition_id}/" + stage, endpoint, methods=["POST"])

    add("plan", _Empty)
    add("authorize", _Authorize)
    for stage in ("apply", "reconcile", "verify"):
        add(stage, _Apply)

    @app.get("/healthz")
    def health():
        return {"operator_plane_only": True, "work_admissions": "disabled",
                "transition_apply": "enabled" if allow_apply else "disabled", "transition_complete": "NOT_EVALUATED"}
    return app


def operator_application(old_config, new_config, sources, backups, retirement, native_grants, *, allow_apply=False):
    from azure.data.tables import TableClient
    from azure.identity import ManagedIdentityCredential

    resources = ExitStack()
    try:
        old = read_document(old_config, ServerConfiguration)
        new = read_document(new_config, ServerConfiguration)
        grants = TypeAdapter(list[NativeExecutionGrant]).validate_json(Path(native_grants).read_bytes())
        credential = resources.enter_context(ManagedIdentityCredential(client_id=old.identity.managed_identity_client_id))
        table = resources.enter_context(TableClient(
            old.table.endpoint, old.table.scope.table_name, credential=credential,
            retry_total=0, retry_read=0, retry_connect=0, retry_status=0, connection_timeout=10, read_timeout=30,
        ))
        service = RuntimeTransition(
            MigrationStore(table, old.table.scope), old, new,
            {"sources": Path(sources).read_bytes(), "backups": Path(backups).read_bytes()},
            retirement=Path(retirement).read_bytes(), native_grants=grants, clock=lambda: datetime.now(timezone.utc),
        )
        verifier = BehaviorJwtVerifier(
            old.identity.auth(), BehaviorAuthSettings(old.identity.human_oid, old.identity.admin_client_id, old.identity.team_id),
        )
        app = create_transition_app(service, verifier, allow_apply=allow_apply)
        @asynccontextmanager
        async def lifespan(_app):
            try:
                yield
            finally:
                resources.close()
        app.router.lifespan_context = lifespan
        return app
    except BaseException:
        resources.close()
        raise


def main():
    parser = argparse.ArgumentParser(description="Review/apply one explicit drained runtime transition; no ordinary work")
    for name in ("old-config", "new-config", "sources", "backups", "retirement", "native-grants"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--allow-apply", action="store_true")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    app = operator_application(args.old_config, args.new_config, args.sources, args.backups,
                               args.retirement, args.native_grants, allow_apply=args.allow_apply)
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
