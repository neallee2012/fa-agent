"""Isolated operator transport for a server-pinned protection migration.

This is not an agent tool or a general behavior entry. Configuration, code and
native observations are constructor dependencies, never request-body fields.
The verified delegated operator is required again for every stage. There is
no automatic apply retry, workload admission, policy renewal or TTL override.

Optional --expired-source-permission PATH loads a separate, short-lived server
permission for POST .../{migration_id}/verify-expired-source with body {}.
It pins the NEW configuration, installed history digest, frozen code and exact
source/expiry. It enables neither normal behavior routes nor recovery.
"""
from __future__ import annotations

import argparse
import json
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta
from datetime import timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _principal_dependency, _response
from .behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier, BehaviorPrincipal
from .behavior_migration import MigrationAuthorization, MigrationPlan, MigrationStore, ProtectionMigration, migration_key
from .behavior_migration_verification import (
    ExpiredSourceVerification, ExpiredSourceVerificationPermission, permission_example,
)
from .behavior_server_config import ServerConfiguration
from .behavior_wire import Identifier, WireModel
from .errors import Problem
from .source_materials import canonical


class PlanRequest(WireModel):
    pass


class AuthorizeRequest(WireModel):
    plan: MigrationPlan
    decision_id: Identifier
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def explicit_utc(cls, value):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("operator authorization requires explicit UTC")
        return value


class ApplyRequest(WireModel):
    plan: MigrationPlan
    authorization: MigrationAuthorization


def create_migration_app(service: ProtectionMigration, verifier: BehaviorJwtVerifier, *,
                         allow_apply: bool = False,
                         expired_source_permission: ExpiredSourceVerificationPermission | None = None) -> FastAPI:
    if not isinstance(service, ProtectionMigration) or not isinstance(verifier, BehaviorJwtVerifier):
        raise ValueError("Explicit migration and delegated-operator verifier dependencies are required")
    identity = service.old.identity
    if (verifier.auth.tenant_id != identity.tenant_id or verifier.auth.audience != identity.audience
            or verifier.settings.human_oid != identity.human_oid
            or verifier.settings.client_id != identity.admin_client_id or verifier.settings.team != identity.team_id):
        raise ValueError("Operator verifier and fixed migration identity differ")
    app = FastAPI(title="FA operator-only protection migration", docs_url=None, redoc_url=None, openapi_url=None)
    authenticate = _principal_dependency(verifier)
    identifier = TypeAdapter(Identifier)
    verification = None if expired_source_permission is None else ExpiredSourceVerification(
        service.store, service.new, expired_source_permission, clock=service.clock,
    )

    @app.exception_handler(Problem)
    async def problem(_request, error):
        return JSONResponse(error.body, status_code=error.status, headers={"Cache-Control": "no-store"})

    def add(stage, model):
        async def endpoint(request: Request, migration_id: str,
                           principal: BehaviorPrincipal = Depends(authenticate)):
            service._operator(principal)
            try:
                identifier.validate_python(migration_id, strict=True)
            except ValidationError:
                raise Problem(422, "MIGRATION_REQUEST_INVALID", "invalid bounded migration identifier") from None
            command = await _body(request, model)
            if stage == "apply" and not allow_apply:
                raise Problem(403, "MIGRATION_APPLY_DISABLED", "this operator deployment is review-only")
            if stage != "plan" and command.plan.migration_id != migration_id:
                raise Problem(409, "MIGRATION_REQUEST_MISMATCH", "path and retained plan differ")

            def invoke():
                if stage == "plan":
                    return service.plan(principal, migration_id=migration_id)
                if stage == "authorize":
                    return service.authorize(principal, command.plan, decision_id=command.decision_id,
                                             expires_at=command.expires_at)
                if stage == "apply":
                    return service.apply(principal, command.plan, command.authorization)
                if stage == "reconcile":
                    return service.reconcile(principal, command.plan, command.authorization)
                return service.verify_applied(principal, command.plan, command.authorization)

            try:
                result = await run_in_threadpool(invoke)
            except Problem:
                raise
            except Exception:
                raise Problem(503, "MIGRATION_DEPENDENCY_UNKNOWN",
                              "operator outcome is unknown; inspect the original plan, never automatically reapply") from None
            return _response(result)

        app.add_api_route("/operator/v1/protection-migrations/{migration_id}/" + stage, endpoint,
                          methods=["POST"], name="migration-" + stage)

    add("plan", PlanRequest)
    add("authorize", AuthorizeRequest)
    for stage in ("apply", "reconcile", "verify"):
        add(stage, ApplyRequest)

    if verification is not None:
        @app.post("/operator/v1/protection-migrations/{migration_id}/verify-expired-source")
        async def verify_expired_source(request: Request, migration_id: str,
                                        principal: BehaviorPrincipal = Depends(authenticate)):
            verification.require_operator(principal)
            try:
                identifier.validate_python(migration_id, strict=True)
            except ValidationError:
                raise Problem(422, "MIGRATION_REQUEST_INVALID", "invalid bounded migration identifier") from None
            await _body(request, PlanRequest)
            permission = verification.permission
            if migration_key(permission.authority_domain, "history", migration_id) != permission.migration_history_key:
                raise Problem(409, "MIGRATION_REQUEST_MISMATCH", "path differs from the fixed verification history")
            return _response(await run_in_threadpool(verification.verify, principal))

    @app.get("/healthz")
    def health():
        return {"status": "alive", "operator_plane_only": True, "work_admissions": "disabled",
                "migration_apply": "enabled" if allow_apply else "disabled", "migration_completed": "NOT_EVALUATED",
                "expired_source_verification": "enabled" if verification is not None else "disabled"}

    return app


def operator_application(old_config: Path, new_config: Path, old_evidence: Path,
                         sources: Path, backups: Path, *, allow_apply: bool = False,
                         expired_source_permission: Path | None = None) -> FastAPI:
    """Trusted process configuration; no endpoint accepts paths or native witnesses."""
    from azure.data.tables import TableClient
    from azure.identity import ManagedIdentityCredential

    resources = ExitStack()
    try:
        old = ServerConfiguration.model_validate_json(old_config.read_bytes())
        new = ServerConfiguration.model_validate_json(new_config.read_bytes())
        permission = None if expired_source_permission is None else (
            ExpiredSourceVerificationPermission.model_validate_json(expired_source_permission.read_bytes())
        )
        previous = json.loads(old_evidence.read_bytes())
        credential = resources.enter_context(ManagedIdentityCredential(
            client_id=old.identity.managed_identity_client_id,
        ))
        table = resources.enter_context(TableClient(
            old.table.endpoint, old.table.scope.table_name, credential=credential,
            retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
            connection_timeout=10, read_timeout=30,
        ))
        service = ProtectionMigration(
            MigrationStore(table, old.table.scope), old, new,
            {"sources": sources.read_bytes(), "backups": backups.read_bytes()},
            previous_observations={lane: canonical(previous[lane]) for lane in ("sources", "backups")},
            clock=lambda: datetime.now(timezone.utc),
        )
        verifier = BehaviorJwtVerifier(
            old.identity.auth(), BehaviorAuthSettings(old.identity.human_oid, old.identity.admin_client_id,
                                                      old.identity.team_id),
        )
        app = create_migration_app(service, verifier, allow_apply=allow_apply, expired_source_permission=permission)

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
    parser = argparse.ArgumentParser(description="Isolated delegated-operator migration plane; no work routes")
    config_names = ("old-config", "new-config", "old-evidence", "sources", "backups")
    for name in config_names:
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--allow-apply", action="store_true")
    parser.add_argument("--expired-source-permission", type=Path,
                        help="server-only short-lived exact expired-source verification permission JSON; default off")
    parser.add_argument("--print-expired-source-permission-example", action="store_true",
                        help="print invalid configuration-shape placeholders only; no approval or server startup")
    args = parser.parse_args()
    if args.print_expired_source_permission_example:
        if (any(getattr(args, name.replace("-", "_")) is not None for name in config_names)
                or args.expired_source_permission is not None or args.allow_apply):
            parser.error("permission example mode cannot be combined with server configuration or apply")
        print(json.dumps(permission_example(), ensure_ascii=False, indent=2))
        return
    missing = [name for name in config_names if getattr(args, name.replace("-", "_")) is None]
    if missing:
        parser.error("the following arguments are required: " + ", ".join("--" + name for name in missing))
    app = operator_application(args.old_config, args.new_config, args.old_evidence, args.sources, args.backups,
                               allow_apply=args.allow_apply, expired_source_permission=args.expired_source_permission)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
