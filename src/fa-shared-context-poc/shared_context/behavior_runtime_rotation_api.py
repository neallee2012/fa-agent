"""Isolated HUMAN Read+Approve operator plane; no business or bootstrap routes.

POST /operator/v1/runtime-rotations/{rotation_id}/plan with {}.
POST .../authorize with {plan, decision_id, expires_at}.
POST .../apply|reconcile|verify with {plan, authorization}.
Unknown commit: use reconcile with the SAME original plan/authorization, never
resend apply. Apply is disabled unless BOTH service and API are enabled.
Configuration, native grants and observation paths are server-installed only.

Canonical entry: behavior_server_main.py runtime-rotation --old-config ...
--new-config ... --sources ... --backups ... --retirement ... --native-grants ...
The equivalent isolated module entry remains available. Neither entry bootstraps
or activates work. `behavior_server_main.py schema rotation-<kind>` exports the
exact input contracts without opening clients; see SCHEMA_KINDS below.
"""
import argparse
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ConfigDict, Field, RootModel, TypeAdapter, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _principal_dependency, _response
from .behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
from .behavior_foundry import NativeExecutionGrant
from .behavior_migration import MigrationStore
from .behavior_runtime_rotation import (
    CurrentTarget, RotationAuthorization, RotationEvidenceFiles, RotationPlan, RotationRetirement, RuntimeRotation,
)
from .behavior_server_config import ProtectionObservation, ServerConfiguration, read_document
from .behavior_wire import WireModel
from .errors import Problem
from .models import Identifier


class _Empty(WireModel):
    pass


class _Authorize(WireModel):
    plan: RotationPlan
    decision_id: Identifier
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def utc_time(cls, value):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("explicit UTC authorization required")
        return value


class _Apply(WireModel):
    plan: RotationPlan
    authorization: RotationAuthorization


NativeGrants = Annotated[tuple[NativeExecutionGrant, ...], Field(min_length=1, max_length=1)]


class _NativeGrantsDocument(RootModel[NativeGrants]):
    model_config = ConfigDict(strict=True, frozen=True)


SCHEMA_KINDS = (
    "rotation-retirement", "rotation-current-target", "rotation-protection",
    "rotation-native-grants", "rotation-plan", "rotation-authorization",
    "rotation-authorize-request", "rotation-apply-request",
)


def input_schema(kind):
    """Pure JSON Schema export; no configuration/client/evidence access."""
    models = {
        "rotation-retirement": RotationRetirement, "rotation-current-target": CurrentTarget,
        "rotation-protection": ProtectionObservation, "rotation-native-grants": NativeGrants,
        "rotation-plan": RotationPlan, "rotation-authorization": RotationAuthorization,
        "rotation-authorize-request": _Authorize, "rotation-apply-request": _Apply,
    }
    if kind not in models:
        raise ValueError("unknown runtime rotation schema")
    return TypeAdapter(models[kind]).json_schema()


def create_rotation_app(service, verifier, *, allow_apply=False):
    if type(service) is not RuntimeRotation or not isinstance(verifier, BehaviorJwtVerifier):
        raise ValueError("exact runtime rotation and delegated verifier required")
    identity = service.old.identity
    if (verifier.auth.tenant_id != identity.tenant_id or verifier.auth.audience != identity.audience
            or verifier.settings.human_oid != identity.human_oid or verifier.settings.team != identity.team_id
            or verifier.settings.client_id != identity.admin_client_id):
        raise ValueError("operator identity verifier differs from the installed authority")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    authenticate = _principal_dependency(verifier)

    @app.exception_handler(Problem)
    async def problem(_request, error):
        return JSONResponse(error.body, status_code=error.status, headers={"Cache-Control": "no-store"})

    def add(stage, model):
        async def endpoint(request: Request, rotation_id: str, principal=Depends(authenticate)):
            service._operator(principal)
            try:
                TypeAdapter(Identifier).validate_python(rotation_id, strict=True)
            except ValidationError:
                raise Problem(422, "ROTATION_REQUEST_INVALID", "invalid rotation identifier") from None
            command = await _body(request, model)
            if stage == "apply" and not allow_apply:
                raise Problem(403, "ROTATION_APPLY_DISABLED", "operator API is review-only")
            if stage != "plan" and command.plan.rotation_id != rotation_id:
                raise Problem(409, "ROTATION_REQUEST_MISMATCH", "path and approved rotation differ")

            def invoke():
                if stage == "plan":
                    return service.plan(principal, rotation_id=rotation_id)
                if stage == "authorize":
                    return service.authorize(principal, command.plan, decision_id=command.decision_id,
                                             expires_at=command.expires_at)
                return getattr(service, stage)(principal, command.plan, command.authorization)
            try:
                return _response(await run_in_threadpool(invoke))
            except Problem:
                raise
            except (ValueError, TypeError, KeyError):
                raise Problem(409, "ROTATION_INPUT_INVALID", "server evidence or retained typed history is invalid") from None
            except Exception:
                raise Problem(503, "ROTATION_DEPENDENCY_UNKNOWN",
                              "outcome unknown; reconcile original request, never automatically reapply") from None
        app.add_api_route("/operator/v1/runtime-rotations/{rotation_id}/" + stage, endpoint, methods=["POST"])

    add("plan", _Empty)
    add("authorize", _Authorize)
    for stage in ("apply", "reconcile", "verify"):
        add(stage, _Apply)

    @app.get("/healthz")
    def health():
        return {"operator_plane_only": True, "work_admissions": "disabled",
                "rotation_apply": "enabled" if allow_apply and service.allow_apply else "disabled",
                "rotation_depth_limit": 1,
                "rotation_complete": "NOT_EVALUATED"}
    return app


def operator_application(old_config, new_config, sources, backups, retirement, native_grants, *, allow_apply=False):
    from azure.data.tables import TableClient
    from azure.identity import ManagedIdentityCredential

    resources = ExitStack()
    try:
        old = read_document(old_config, ServerConfiguration)
        new = read_document(new_config, ServerConfiguration)
        grants = read_document(native_grants, _NativeGrantsDocument).root
        credential = resources.enter_context(ManagedIdentityCredential(
            client_id=old.identity.managed_identity_client_id, retry_total=0))
        table = resources.enter_context(TableClient(
            old.table.endpoint, old.table.scope.table_name, credential=credential,
            retry_total=0, retry_read=0, retry_connect=0, retry_status=0, retry_to_secondary=False,
            connection_timeout=10, read_timeout=30,
        ))
        service = RuntimeRotation(
            MigrationStore(table, old.table.scope), old, new,
            RotationEvidenceFiles(Path(sources), Path(backups), Path(retirement)), native_grants=grants,
            clock=lambda: datetime.now(timezone.utc), allow_apply=allow_apply,
        )
        verifier = BehaviorJwtVerifier(old.identity.auth(), BehaviorAuthSettings(
            old.identity.human_oid, old.identity.admin_client_id, old.identity.team_id))
        app = create_rotation_app(service, verifier, allow_apply=allow_apply)

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


def add_operator_arguments(parser):
    """Shared canonical/module CLI contract; never accepts an HTTP observation."""
    for name in ("old-config", "new-config", "sources", "backups", "retirement", "native-grants"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--allow-apply", action="store_true")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)


def serve_operator(args):
    app = operator_application(args.old_config, args.new_config, args.sources, args.backups,
                               args.retirement, args.native_grants, allow_apply=args.allow_apply)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


def main():
    parser = argparse.ArgumentParser(description="Review/rotate one SDK configuration without resetting its authority")
    add_operator_arguments(parser)
    serve_operator(parser.parse_args())


if __name__ == "__main__":
    main()
