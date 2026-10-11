"""Opt-in original-result web receiver; no SDK, stop or Connector endpoint."""
import base64
import os
from fastapi import Depends, FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from .behavior_api import _body, _install_error_handlers, _principal_dependency, _response
from .behavior_delivery import _Model
from .behavior_server import _lifespan, build_server
from .behavior_server_config import load_configuration, read_document
from .behavior_web_handoff import WebHandoffScope, WebHandoffObservation, WebHandoffPlan, OriginalFailureWebHandoff
from .behavior_wire import Command, PullAcknowledgment
from .behavior_recovery import ExpiredTeamsCleanupScope
from .models import Identifier


class PreviewRequest(_Model):
    pass


class ApproveWebHandoff(_Model):
    plan:WebHandoffPlan
    decision_id:Identifier


def create_handoff_app(composition,scope,*,observation,current_observation,original_scope):
    composition.installation()
    service=OriginalFailureWebHandoff(composition,scope,observation,current_observation,original_scope=original_scope)
    app=FastAPI(title="Original failure web handoff",docs_url=None,redoc_url=None,openapi_url=None)
    _install_error_handlers(app)
    identity=_principal_dependency(composition.verifier)
    operator="/operator/v1/original-failure-web"
    receiver="/web/v1/original-failure/"+scope.handoff_id

    @app.post(operator+"/preview")
    async def preview(request:Request,principal=Depends(identity)):
        service._human(principal,approve=True)
        await _body(request,PreviewRequest)
        return _response(await run_in_threadpool(service.preview,principal))

    @app.post(operator+"/authorize")
    async def authorize(request:Request,principal=Depends(identity)):
        service._human(principal,approve=True)
        command=await _body(request,ApproveWebHandoff)
        return _response(await run_in_threadpool(service.authorize,principal,command.plan,command.decision_id))

    @app.post(receiver+"/fetch")
    async def fetch(request:Request,principal=Depends(identity)):
        command=await _body(request,Command)
        got=await run_in_threadpool(service.fetch,principal,command)
        return Response(got.payload,media_type="application/json",headers={
            "Cache-Control":"no-store","X-Behavior-Exchange":got.exchange_id,
            "X-Behavior-Ack-Token":got.acknowledgment_token,
            "X-Behavior-Binding":base64.urlsafe_b64encode(got.binding.model_dump_json().encode()).decode(),
            "X-Behavior-Payload-SHA256":got.binding.payload_digest,
            "X-Behavior-Confirmation-Level":"client_received"})

    @app.post(receiver+"/acknowledge")
    async def acknowledge(request:Request,principal=Depends(identity)):
        command=await _body(request,PullAcknowledgment)
        principal.require(composition.config.identity.team_id,"acknowledge")
        return _response(await run_in_threadpool(service.acknowledge,principal,command))

    @app.post(receiver+"/reconcile")
    async def reconcile(request:Request,principal=Depends(identity)):
        command=await _body(request,Command)
        principal.require(composition.config.identity.team_id,"acknowledge")
        return _response(await run_in_threadpool(service.reconcile,principal,command))

    @app.get(receiver+"/status")
    async def status(principal=Depends(identity)):
        return _response(await run_in_threadpool(service.status,principal))
    app.state.handoff=service
    return _lifespan(app,composition,composition.installation)


def create_app():
    scope=WebHandoffScope.model_validate_json(os.environ["FA_ORIGINAL_WEB_HANDOFF_SCOPE_JSON"])
    path=os.environ["FA_ORIGINAL_WEB_HANDOFF_OBSERVATION_FILE"]
    original_scope=read_document(os.environ["FA_ORIGINAL_CLEANUP_SCOPE_FILE"],ExpiredTeamsCleanupScope)
    def observed():return read_document(path,WebHandoffObservation)
    return create_handoff_app(build_server(load_configuration()),scope,observation=observed(),current_observation=observed,
                              original_scope=original_scope)
