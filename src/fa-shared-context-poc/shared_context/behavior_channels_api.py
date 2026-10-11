"""Explicit operator plan/authorize/apply/reconcile/verify for channel v2."""
from __future__ import annotations

import argparse
import json
from contextlib import ExitStack, asynccontextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import field_validator
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _principal_dependency, _response
from .behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
from .behavior_channels import TeamsChannelConfiguration
from .behavior_channels_migration import (
    ChannelApproval, ChannelEvidenceFiles, ChannelMigration, ChannelPlan, ChannelRetirement,
)
from .behavior_channels_pin_refresh import (
    ChannelPinRefresh, PinRefreshPlan, PinRefreshApproval, PinRefreshEvidenceFiles, OperatorCodeDirectory,
    propose_protected_channels,
)
from .behavior_channels_window import (
    ChannelWindow, WindowScope, WindowPlan, WindowApproval, WindowEvidenceFiles, propose_window,
)
from .behavior_migration import MigrationStore
from .behavior_server_config import ProtectionObservation, ServerConfiguration, ServerConfigurationV2, read_document
from .behavior_wire import WireModel
from .errors import Problem
from .models import Identifier


class Empty(WireModel):
    pass


class Authorize(WireModel):
    plan: ChannelPlan | PinRefreshPlan | WindowPlan
    decision_id: Identifier
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def utc(cls, value):
        from .behavior_delivery import _utc
        return _utc(value)


class Apply(WireModel):
    plan: ChannelPlan | PinRefreshPlan | WindowPlan
    authorization: ChannelApproval | PinRefreshApproval | WindowApproval


def create_channels_app(service, verifier, *, allow_apply=False):
    if type(service) not in (ChannelMigration, ChannelPinRefresh, ChannelWindow) or not isinstance(verifier, BehaviorJwtVerifier):
        raise ValueError("exact channel migration and delegated verifier required")
    ids = service.old.identity
    if (verifier.auth.tenant_id != ids.tenant_id or verifier.auth.audience != ids.audience
            or verifier.settings.human_oid != ids.human_oid or verifier.settings.client_id != ids.admin_client_id
            or verifier.settings.team != ids.team_id):
        raise ValueError("the original operator verifier is required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    authenticated = _principal_dependency(verifier)

    @app.exception_handler(Problem)
    async def error(_request, problem):
        return JSONResponse(problem.body, status_code=problem.status, headers={"Cache-Control": "no-store"})

    def route(stage, model):
        async def endpoint(transition_id: str, request: Request, principal=Depends(authenticated)):
            service.operator(principal)
            command = await _body(request, model)
            if stage != "plan" and (type(command.plan) is not service.plan_type
                    or (stage != "authorize" and type(command.authorization) is not service.approval_type)):
                raise Problem(409, "CHANNELS_REQUEST_MISMATCH", "明確遷移版本與批准版本不吻合")
            if stage != "plan" and command.plan.transition_id != transition_id:
                raise Problem(409, "CHANNELS_REQUEST_MISMATCH", "路徑必須是原批准的 transition ID")
            if stage == "apply" and not allow_apply:
                raise Problem(403, "CHANNELS_APPLY_DISABLED", "此 operator API 只允許審核")
            def invoke():
                if stage == "plan":
                    return service.plan(principal, transition_id=transition_id)
                if stage == "authorize":
                    return service.authorize(principal, command.plan, decision_id=command.decision_id,
                                             expires_at=command.expires_at)
                return getattr(service, stage)(principal, command.plan, command.authorization)
            try:
                context = (service.authenticated(lambda: verifier(request))
                           if type(service) in (ChannelPinRefresh, ChannelWindow) else nullcontext())
                with context:
                    return _response(await run_in_threadpool(invoke))
            except Problem:
                raise
            except (ValueError, KeyError, TypeError):
                raise Problem(409, "CHANNELS_INPUT_INVALID", "受信任觀察或原 typed history 不吻合") from None
            except Exception:
                raise Problem(503, "CHANNELS_OUTCOME_UNKNOWN", "結果未知；只能核對原請求，不可自動重 apply") from None
        app.add_api_route("/operator/v2/channels/{transition_id}/" + stage, endpoint, methods=["POST"])

    route("plan", Empty)
    route("authorize", Authorize)
    for stage in ("apply", "reconcile", "verify"):
        route(stage, Apply)

    @app.get("/healthz")
    def health():
        return {"operator_only": True, "business_admission": False, "native_sender": False,
                "apply_enabled": allow_apply and service.allow_apply}
    return app


def operator_application(old_path, new_path, sources, backups, retirement, *, allow_apply=False,
                         previous_sources=None, previous_backups=None, operator_code_directory=None, scope_check=None,
                         window_scope=None):
    from azure.data.tables import TableClient
    from azure.identity import ManagedIdentityCredential

    resources = ExitStack()
    try:
        old = read_document(old_path, ServerConfigurationV2 if window_scope is not None else ServerConfiguration)
        new = read_document(new_path, ServerConfigurationV2)
        credential = resources.enter_context(ManagedIdentityCredential(client_id=old.identity.managed_identity_client_id, retry_total=0))
        table = resources.enter_context(TableClient(
            old.table.endpoint, old.table.scope.table_name, credential=credential,
            retry_total=0, retry_read=0, retry_connect=0, retry_status=0, retry_to_secondary=False,
            logging_enable=False, connection_timeout=10, read_timeout=30,
        ))
        pin_inputs = (previous_sources, previous_backups, operator_code_directory)
        if window_scope is not None:
            if previous_sources is not None or previous_backups is not None or operator_code_directory is None:
                raise ValueError("window mode cannot refresh storage pins and requires actual operator code")
            window = read_document(window_scope, WindowScope)
            def current_window(historical):
                if (read_document(old_path, ServerConfigurationV2) != old
                        or read_document(new_path, ServerConfigurationV2) != new
                        or read_document(window_scope, WindowScope) != window):
                    raise Problem(409, "CHANNELS_REQUEST_MISMATCH", "trusted window/configuration changed")
                if scope_check is not None:
                    scope_check(historical)
            service = ChannelWindow(MigrationStore(table, old.table.scope), old, new,
                WindowEvidenceFiles(Path(sources), Path(backups), Path(retirement)),
                OperatorCodeDirectory(Path(operator_code_directory)), window,
                clock=lambda: datetime.now(timezone.utc), allow_apply=allow_apply, scope_check=current_window)
        elif any(pin_inputs):
            if not all(pin_inputs):
                raise ValueError("pin refresh requires both historical witnesses and actual operator code")
            def current_scope(historical):
                if read_document(old_path, ServerConfiguration) != old or read_document(new_path, ServerConfigurationV2) != new:
                    raise Problem(409, "CHANNELS_REQUEST_MISMATCH", "trusted installed/proposed configuration changed")
                if scope_check is not None:
                    scope_check(historical)
            service = ChannelPinRefresh(
                MigrationStore(table, old.table.scope), old, new,
                PinRefreshEvidenceFiles(Path(sources), Path(backups), Path(retirement),
                                        Path(previous_sources), Path(previous_backups)),
                OperatorCodeDirectory(Path(operator_code_directory)),
                clock=lambda: datetime.now(timezone.utc), allow_apply=allow_apply, scope_check=current_scope,
            )
        else:
            service = ChannelMigration(MigrationStore(table, old.table.scope), old, new,
                                       ChannelEvidenceFiles(Path(sources), Path(backups), Path(retirement)),
                                       clock=lambda: datetime.now(timezone.utc), allow_apply=allow_apply)
        verifier = BehaviorJwtVerifier(
            old.identity.auth(), BehaviorAuthSettings(old.identity.human_oid, old.identity.admin_client_id, old.identity.team_id))
        app = create_channels_app(service, verifier, allow_apply=allow_apply)
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
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest="action", required=True)
    schema = actions.add_parser("schema")
    schema.add_argument("kind", choices=("configuration", "channels", "retirement", "plan", "pin-plan",
                                        "window-scope", "window-plan", "authorize", "apply"))
    propose = actions.add_parser("propose")
    propose.add_argument("--old-config", required=True)
    propose.add_argument("--channels", required=True)
    propose.add_argument("--output", required=True)
    propose.add_argument("--pin-refresh", action="store_true")
    propose.add_argument("--window-scope")
    propose.add_argument("--native-evidence-id")
    for name in ("sources", "backups", "previous-sources", "previous-backups"):
        propose.add_argument("--" + name)
    serve = actions.add_parser("serve")
    for name in ("old-config", "new-config", "sources", "backups", "retirement"):
        serve.add_argument("--" + name, required=True)
    for name in ("previous-sources", "previous-backups", "operator-code-directory", "window-scope"):
        serve.add_argument("--" + name)
    serve.add_argument("--allow-apply", action="store_true")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if args.action == "schema":
        model = {"configuration": ServerConfigurationV2, "channels": TeamsChannelConfiguration,
                 "retirement": ChannelRetirement, "plan": ChannelPlan, "pin-plan": PinRefreshPlan,
                 "window-scope": WindowScope, "window-plan": WindowPlan,
                 "authorize": Authorize, "apply": Apply}[args.kind]
        print(json.dumps(model.model_json_schema(), ensure_ascii=False))
    elif args.action == "propose":
        old = read_document(args.old_config, ServerConfigurationV2 if args.window_scope else ServerConfiguration)
        channels = read_document(args.channels, TeamsChannelConfiguration)
        values = (args.sources, args.backups, args.previous_sources, args.previous_backups)
        if args.window_scope:
            if args.pin_refresh or any(values) or args.native_evidence_id is None:
                parser.error("window proposal requires exact scope/native evidence ID; cannot combine pin refresh")
            new = propose_window(old, read_document(args.window_scope, WindowScope), args.native_evidence_id)
            if channels != new.channels:
                parser.error("explicit proposed channels differ from the bounded window")
        elif args.pin_refresh:
            if not all(values):
                parser.error("pin-refresh proposal requires all four original protection witnesses")
            new = propose_protected_channels(
                old, channels,
                {lane: read_document(getattr(args, "previous_" + lane), ProtectionObservation) for lane in ("sources", "backups")},
                {lane: read_document(getattr(args, lane), ProtectionObservation) for lane in ("sources", "backups")},
            )
        else:
            if any(values):
                parser.error("protection witnesses require explicit --pin-refresh")
            new = ServerConfigurationV2.model_validate_json(json.dumps({
                **old.model_dump(mode="json"), "schema_version": "fa-behavior-server-v2",
                "channels": channels.model_dump(mode="json"),
            }))
        with Path(args.output).open("x", encoding="utf-8") as output:
            output.write(new.model_dump_json(indent=2))
        extra = {"ready": False, "needs_fresh_retirement": True} if args.pin_refresh or args.window_scope else {}
        print(json.dumps({"old_sha256": old.sha256, "new_sha256": new.sha256, "applied": False, **extra}))
    else:
        import uvicorn
        app = operator_application(args.old_config, args.new_config, args.sources, args.backups, args.retirement,
                                   allow_apply=args.allow_apply, previous_sources=args.previous_sources,
                                   previous_backups=args.previous_backups, operator_code_directory=args.operator_code_directory,
                                   window_scope=args.window_scope)
        uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
