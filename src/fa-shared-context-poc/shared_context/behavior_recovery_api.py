"""Standalone exact-human recovery plane; no model/admission or supplied receipts."""
import os
from datetime import datetime, timezone

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import HttpResponseError
from fastapi import Depends, FastAPI, Request
from pydantic import TypeAdapter
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _install_error_handlers, _principal_dependency, _response
from .behavior_delivery import _Model
from .behavior_recovery import (
    ClaimedSdkRecovery, PlatformStopObservation, PreSdkRecoveryPlan, PreSdkRecoveryScope,
    PreAssignmentRecoveryPlan, PreAssignmentRecoveryScope,
    MultiModelRecoveryPlan, MultiModelRecoveryScope,
    FinishedResultRecoveryPlan, FinishedResultRecoveryScope,
    TeamsPreAcquireRecoveryScope, TeamsPreAcquireRecoveryPlan, TeamsPreAcquireRecoveryAuthorization,
    ExpiredTeamsCleanupScope, ExpiredTeamsCleanupPlan, ExpiredTeamsCleanupAuthorization, ExpiredTeamsCleanupObservation,
    RecoveryAuthorization, RecoveryPlan, RecoveryScope, stop_endpoint,
)
from .behavior_server import _lifespan, build_server
from .behavior_server_config import load_configuration
from .errors import Problem
from .models import Identifier


class AuthorizeRecovery(_Model):
    plan: RecoveryPlan | PreSdkRecoveryPlan | PreAssignmentRecoveryPlan | MultiModelRecoveryPlan | FinishedResultRecoveryPlan | TeamsPreAcquireRecoveryPlan | ExpiredTeamsCleanupPlan
    decision_id: Identifier


class PlanRequest(_Model):
    pass


def create_recovery_app(composition, scope, *, stop=None, cleanup_observation=None, current_cleanup_observation=None):
    composition.installation()
    config = composition.config
    if stop is None:
        def stop(target, deadline, *, before_send=None):
            observed = {}

            def response_hook(response):
                observed.update(status_code=response.http_response.status_code,
                                endpoint=str(response.http_request.url),
                                provider_request_id=response.http_response.headers.get("x-ms-request-id"))

            with AIProjectClient(
                endpoint=target.project_endpoint, credential=composition.clients[0].credential,
                api_version="v1", credential_scopes=["https://ai.azure.com/.default"],
                retry_total=0, connection_timeout=10, read_timeout=60, redirect_max=0,
            ) as project:
                session = project.agents.get_session(target.agent_name, target.session_id, retry_total=0).as_dict()
                if (session["agent_session_id"] != target.session_id
                        or session["version_indicator"]["agent_version"] != target.agent_version):
                    raise Problem(409, "RECOVERY_PROVIDER_TARGET_CHANGED", "the provider session/version differs")
                if datetime.now(timezone.utc) >= deadline:
                    raise Problem(403, "RECOVERY_PERMISSION_EXPIRED", "stop permission expired before dispatch")
                if before_send is not None:
                    before_send()
                    if datetime.now(timezone.utc) >= deadline:
                        raise Problem(403, "RECOVERY_PERMISSION_EXPIRED", "stop permission expired at dispatch")
                error_code = None
                try:
                    project.agents.stop_session(target.agent_name, target.session_id, retry_total=0,
                                                raw_response_hook=response_hook)
                except HttpResponseError as error:
                    error_code = getattr(getattr(error, "error", None), "code", None)
                    if error.status_code != 409 or error_code != "session_already_stopped":
                        raise Problem(503, "RECOVERY_STOP_UNKNOWN", "original provider stop outcome is unconfirmed") from None
                if observed.get("status_code") not in {204, 409}:
                    raise Problem(503, "RECOVERY_STOP_UNKNOWN", "terminal provider stop response was not observed")
                if observed.get("endpoint") != stop_endpoint(target):
                    raise Problem(503, "RECOVERY_STOP_UNKNOWN", "provider stop response endpoint differs")
                return PlatformStopObservation(
                    target=target, credential_client_id=config.identity.managed_identity_client_id,
                    error_code=error_code, observed_at=datetime.now(timezone.utc), **observed,
                )

    if isinstance(scope, ExpiredTeamsCleanupScope):
        from .behavior_teams_cleanup import ExpiredTeamsCleanup
        recovery = ExpiredTeamsCleanup(composition, scope, stop, observation=cleanup_observation,
                                       current_observation=current_cleanup_observation)
    elif isinstance(scope, TeamsPreAcquireRecoveryScope):
        from .behavior_teams_recovery import TeamsPreAcquireRecovery
        recovery = TeamsPreAcquireRecovery(composition, scope, stop)
    else:
        recovery = ClaimedSdkRecovery(config, composition.runtime.dep.native_launches, scope, stop,
                                      delivery=composition.runtime.dep.delivery, clock=composition.runtime.clock)
    app = FastAPI(title="FA exact claimed SDK recovery", docs_url=None, redoc_url=None, openapi_url=None)
    _install_error_handlers(app)
    identity = _principal_dependency(composition.verifier)
    prefix = "/operator/v1/sdk-recovery"

    @app.get("/healthz")
    def health():
        return {"operator_only": True, "admissions": "CLOSED", "models": "DISABLED",
                "recovery_id": scope.recovery_id, "operator_expires_at": scope.expires_at.isoformat()}

    @app.post(prefix + "/plan")
    async def plan(request: Request, principal=Depends(identity)):
        recovery._operator(principal)
        await _body(request, PlanRequest)
        return _response(await run_in_threadpool(recovery.plan, principal))

    @app.post(prefix + "/authorize")
    async def authorize(request: Request, principal=Depends(identity)):
        recovery._operator(principal)
        body = await _body(request, AuthorizeRecovery)
        return _response(await run_in_threadpool(recovery.authorize, principal, body.plan, body.decision_id))

    def install(action):
        @app.post(prefix + "/" + action)
        async def invoke(request: Request, principal=Depends(identity)):
            recovery._operator(principal, historical=action == "reconcile")
            body = await _body(request, ExpiredTeamsCleanupAuthorization if isinstance(scope, ExpiredTeamsCleanupScope)
                               else TeamsPreAcquireRecoveryAuthorization if isinstance(scope, TeamsPreAcquireRecoveryScope)
                               else RecoveryAuthorization)
            return _response(await run_in_threadpool(getattr(recovery, action), principal, body))

    for action in ("execute", "finish", "reconcile"):
        install(action)
    if isinstance(scope, TeamsPreAcquireRecoveryScope):
        install("deliver")
    app.state.recovery = recovery
    return _lifespan(app, composition, composition.installation)


def create_app():
    scope = TypeAdapter(RecoveryScope | PreSdkRecoveryScope | PreAssignmentRecoveryScope | MultiModelRecoveryScope |
                        FinishedResultRecoveryScope | TeamsPreAcquireRecoveryScope | ExpiredTeamsCleanupScope).validate_json(
        os.environ["FA_CLAIMED_RECOVERY_SCOPE_JSON"])
    observation = None
    observer = None
    if isinstance(scope, ExpiredTeamsCleanupScope):
        from .behavior_server_config import read_document
        path = os.environ["FA_TEAMS_CLEANUP_OBSERVATION_FILE"]
        def observer():
            return read_document(path, ExpiredTeamsCleanupObservation)
        observation = observer()
    return create_recovery_app(build_server(load_configuration()), scope,
                               cleanup_observation=observation, current_cleanup_observation=observer)
