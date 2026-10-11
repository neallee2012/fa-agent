"""Four delivery-only operator endpoints; no executor callback or stop hook."""
import os
from fastapi import Depends, FastAPI, Request
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _install_error_handlers, _principal_dependency, _response
from .behavior_delivery import _Model
from .behavior_recovery import ExpiredTeamsCleanupScope
from .behavior_server import _lifespan, build_server
from .behavior_server_config import load_configuration, read_document
from .behavior_teams_continuation import (
    DeliveryContinuationScope, DeliveryContinuationPlan, DeliveryContinuationAuthorization,
    DeliveryContinuationObservation, OriginalFailureDeliveryContinuation,
)
from .models import Identifier


class PreviewRequest(_Model):
    pass


class ApproveContinuation(_Model):
    plan: DeliveryContinuationPlan
    decision_id: Identifier


def create_continuation_app(composition, scope, *, original_scope, observation, current_observation):
    composition.installation()
    service=OriginalFailureDeliveryContinuation(composition,scope,original_scope=original_scope,
        observation=observation,current_observation=current_observation)
    app=FastAPI(title="Original failure delivery-only continuation",docs_url=None,redoc_url=None,openapi_url=None)
    _install_error_handlers(app)
    identity=_principal_dependency(composition.verifier)
    prefix="/operator/v1/original-failure-delivery"

    @app.post(prefix+"/preview")
    async def preview(request:Request,principal=Depends(identity)):
        service._operator(principal)
        await _body(request,PreviewRequest)
        return _response(await run_in_threadpool(service.preview,principal))

    @app.post(prefix+"/authorize")
    async def authorize(request:Request,principal=Depends(identity)):
        service._operator(principal)
        command=await _body(request,ApproveContinuation)
        return _response(await run_in_threadpool(service.authorize,principal,command.plan,command.decision_id))

    def install(action):
        @app.post(prefix+"/"+action)
        async def operation(request:Request,principal=Depends(identity)):
            service._operator(principal,historical=action=="reconcile")
            authorization=await _body(request,DeliveryContinuationAuthorization)
            return _response(await run_in_threadpool(getattr(service,action),principal,authorization))
    for action in ("deliver","reconcile"):
        install(action)
    app.state.continuation=service
    return _lifespan(app,composition,composition.installation)


def create_app():
    scope=DeliveryContinuationScope.model_validate_json(os.environ["FA_ORIGINAL_DELIVERY_CONTINUATION_SCOPE_JSON"])
    original_scope=read_document(os.environ["FA_ORIGINAL_CLEANUP_SCOPE_FILE"],ExpiredTeamsCleanupScope)
    path=os.environ["FA_ORIGINAL_DELIVERY_CONTINUATION_OBSERVATION_FILE"]
    def observed():
        return read_document(path,DeliveryContinuationObservation)
    return create_continuation_app(build_server(load_configuration()),scope,
        original_scope=original_scope,observation=observed(),current_observation=observed)
