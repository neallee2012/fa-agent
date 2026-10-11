"""Opt-in signed-HUMAN retained-result receiver; no server factory or native IO.

create_settlement_app(service, verifier) requires an already explicitly
registered RetainedResultSettlement and the configured BehaviorJwtVerifier.
There is no deployment, evidence upload, clock/identity selector or general API
mount. The installer still must obtain separate explicit approval and fresh
independent receiver/retention evidence before any native use.
"""

import base64

from fastapi import Depends, FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _install_error_handlers, _principal_dependency, _response
from .behavior_auth import BehaviorJwtVerifier
from .behavior_delivery import _Model
from .behavior_settlement import (
    AuthorizeSettlement, RetainedResultSettlement, RevokeSettlement,
    SettlementAcknowledgment, SettlementFetch, SettlementUse,
)
from .errors import Problem


class PrepareSettlement(_Model):
    pass


def create_settlement_app(service: RetainedResultSettlement, verifier: BehaviorJwtVerifier):
    if (type(service) is not RetainedResultSettlement or not isinstance(verifier, BehaviorJwtVerifier)
            or service.inbox._retained_results is not service or service.registration is None):
        raise Problem(403, "SETTLEMENT_DISABLED", "the exact registered settlement and JWT verifier are required")
    service._installed()
    b = service.binding
    if (verifier.auth.tenant_id != b.tenant_id or verifier.settings.human_oid != b.human_oid
            or verifier.settings.team != b.team_id
            or verifier.auth.audience != service.config.identity.audience
            or verifier.settings.client_id != service.config.identity.admin_client_id):
        raise Problem(403, "SETTLEMENT_DISABLED", "receiver JWT configuration differs from the original human binding")
    app = FastAPI(title="FA retained terminal-result settlement", docs_url=None, redoc_url=None, openapi_url=None)
    _install_error_handlers(app)
    identity = _principal_dependency(verifier)
    prefix = "/operator/v1/retained-result"

    def install(action, model, required_action):
        @app.post(prefix + "/" + action)
        async def endpoint(request: Request, principal=Depends(identity)):
            service._operator(principal, required_action, current=action != "revoke")
            command = await _body(request, model)
            arguments = (principal,) if action in {"preflight", "prepare"} else (principal, command)
            result = await run_in_threadpool(getattr(service, action), *arguments)
            if action == "fetch":
                return Response(result.payload, media_type="application/octet-stream", headers={
                    "Cache-Control": "no-store",
                    "X-Behavior-Binding": base64.urlsafe_b64encode(result.binding.model_dump_json().encode()).decode(),
                    "X-Behavior-Exchange": result.exchange_id,
                    "X-Behavior-Ack-Token": result.acknowledgment_token,
                    "X-Behavior-Send-Outcome": "unknown",
                })
            if action == "authorize":
                return _response({
                    "authorization_id": result.authorization_id, "plan_sha256": result.plan.sha256,
                    "approved_at": result.approved_at, "expires_at": result.expires_at, "delivered": False,
                })
            return _response(result)

    for action, model, required in (
        ("preflight", PrepareSettlement, "approve"),
        ("prepare", PrepareSettlement, "approve"), ("authorize", AuthorizeSettlement, "approve"),
        ("fetch", SettlementFetch, "read"), ("acknowledge", SettlementAcknowledgment, "acknowledge"),
        ("reconcile", SettlementUse, "acknowledge"), ("revoke", RevokeSettlement, "approve"),
    ):
        install(action, model, required)
    app.state.settlement = service
    return app
