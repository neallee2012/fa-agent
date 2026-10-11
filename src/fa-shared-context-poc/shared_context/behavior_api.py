"""Auth-first bounded FastAPI composition, separate from legacy API middleware.

Use create_behavior_app(runtime, verifier), or attach behavior_router to a host
that does NOT pre-read bodies before authentication. In particular, mounting
behind legacy body-buffering middleware does not preserve this auth-first claim.
No production factory, credentials, resource provisioning or readiness is added.

All IDs are selectors within immutable server bindings. JSON cannot supply an
ExecutionContext, source owner/scope, grant identity, policy, adapter or clock.
Pull returns the EXACT saved payload bytes with exchange metadata in headers.
Its explicit client_received acknowledgment is not a human-read/UI receipt.
Controller start/adopt/drain are intentionally absent. Optional assigned Hosted
callbacks live under /hosted/launches/{launch_id}; launch/report capabilities
are private headers, rejected in JSON and never returned in ordinary bodies.
Optional backup prepare/upload/reconcile routes require Behavior.Backup.
create_source_recovery_app is a SEPARATE recovery-only factory with no live
BehaviorRuntime/source Table dependency. It requires Behavior.Restore plus a
current independent recovery catalog decision and returns quarantined base64
originals, never imports/activates state or grants permissions.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, SecretStr, TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from . import behavior_wire as wire
from .behavior_auth import BehaviorJwtVerifier, BehaviorPrincipal
from .behavior_delivery import Level
from .behavior_runtime import BehaviorRuntime
from .errors import Problem
from .behavior_ingress import checked_human_text
from .records import Record
from .source_backup import SourceBackupReference, SourceBackupService

MAX_BODY_BYTES = 24 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
MAX_RECOVERY_RESPONSE_BYTES = 12 * 1024 * 1024
PREFIX = "/behavior/v1/bindings/{binding_id}"
_id = TypeAdapter(wire.Identifier)
_digest = TypeAdapter(wire.Digest)
_levels = TypeAdapter(list[Level])


def _unique(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate JSON field")
    return value


def _nonfinite(_):
    raise ValueError("non-finite JSON value")


async def _body(request: Request, model, *, max_body_bytes=MAX_BODY_BYTES):
    if request.query_params:
        raise Problem(422, "BEHAVIOR_INVALID_REQUEST", "query parameters are not supported")
    for name in ("content-length", "content-type", "content-encoding"):
        if len(request.headers.getlist(name)) > 1:
            raise Problem(422, "BEHAVIOR_INVALID_REQUEST", "duplicate request headers")
    if request.headers.get("content-type") not in {"application/json", "application/json; charset=utf-8"}:
        raise Problem(415, "BEHAVIOR_INVALID_REQUEST", "JSON content type is required")
    if request.headers.get("content-encoding", "identity") != "identity":
        raise Problem(415, "BEHAVIOR_INVALID_REQUEST", "encoded bodies are unsupported")
    length = request.headers.get("content-length")
    if length is not None:
        if not re.fullmatch(r"[0-9]{1,8}", length):
            raise Problem(422, "BEHAVIOR_INVALID_REQUEST", "invalid length")
        if int(length) > max_body_bytes:
            raise Problem(413, "BEHAVIOR_BODY_LIMIT", "request exceeds the byte limit")
    total, chunks = 0, []
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_body_bytes:
                raise Problem(413, "BEHAVIOR_BODY_LIMIT", "request exceeds the byte limit")
            chunks.append(chunk)
    except ClientDisconnect:
        raise Problem(400, "BEHAVIOR_INVALID_REQUEST", "incomplete request") from None
    if length is not None and total != int(length):
        raise Problem(400, "BEHAVIOR_INVALID_REQUEST", "request length differs")
    raw = b"".join(chunks)
    request.state.behavior_body_bytes = total
    try:
        json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite)
        command = model.model_validate_json(raw)
    except (ValueError, TypeError, ValidationError, UnicodeError):
        raise Problem(422, "BEHAVIOR_INVALID_REQUEST", "invalid fields, duplicate keys, types or lengths") from None
    original = getattr(command, "root", command)
    original = getattr(original, "mapping", original)
    request.state.behavior_request_id = _diagnostic_request_id(getattr(original, "request_id", None))
    return command


def _diagnostic_request_id(value):
    if not isinstance(value, str):
        return None
    try:
        _id.validate_python(value, strict=True)
        checked_human_text(value.encode("utf-8"))
    except (ValueError, UnicodeError, Problem):
        return None
    return value


def _log_request_failure(request, code):
    if not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code) is None:
        code = "UNCLASSIFIED"
    logging.getLogger(__name__).warning("BEHAVIOR_REQUEST_FAILED %s", json.dumps({
        "code": code, "request_id": getattr(request.state, "behavior_request_id", None),
    }, ensure_ascii=True, separators=(",", ":")))


def _delivery_result(result):
    if result.get("delivery_state") not in {"fulfilled", "delivered"}:
        return result
    try:
        for name in ("obligation_id", "fulfillment_receipt_id", "channel_version"):
            _id.validate_python(result[name], strict=True)
        _digest.validate_python(result["evidence_digest"], strict=True)
        levels = _levels.validate_python(result["confirmed_levels"], strict=True)
        if (result["fulfillment"] != "fulfilled" or result["required_confirmation_level"] not in levels
                or len(levels) != len(set(levels))):
            raise ValueError("delivery confirmation differs")
    except (KeyError, ValueError, TypeError):
        raise Problem(503, "BEHAVIOR_DELIVERY_RECEIPT_INVALID", "delivered requires a complete confirmation receipt") from None
    # Preserve ledger/receipt hashes and internal fulfillment consumers.
    return {**result, "delivery_state": "delivered"}


def _jsonable(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Record):
        return {
            "result": _delivery_result(value.payload["result"]),
            "control_request_id": value.payload["request"]["request_id"],
            "control_request_digest": value.payload["request"]["request_digest"],
        }
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _response(value, *, max_bytes=MAX_RESPONSE_BYTES):
    raw = json.dumps(_jsonable(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > max_bytes:
        raise Problem(413, "BEHAVIOR_RESPONSE_LIMIT", "complete response exceeds the supported byte limit")
    return Response(raw, media_type="application/json", headers={"Cache-Control": "no-store"})


def _principal_dependency(verifier):
    def identity(request: Request):
        if len(request.headers.getlist("authorization")) != 1:
            raise Problem(401, "BEHAVIOR_UNAUTHENTICATED", "one authorization header is required")
        try:
            return verifier(request)
        except Problem:
            raise
        except Exception:
            raise Problem(503, "BEHAVIOR_DEPENDENCY_UNAVAILABLE", "authentication dependency failed") from None

    return identity


def behavior_router(runtime: BehaviorRuntime, verifier: BehaviorJwtVerifier, *, registered_actor=None) -> APIRouter:
    if not isinstance(verifier, BehaviorJwtVerifier):
        raise Problem(503, "BEHAVIOR_CONFIGURATION", "the explicit behavior JWT verifier is required")
    actors = verifier.auth.actors
    if registered_actor is not None:
        # One Team-registry peer verified by resolve_agent_scope; it never replaces a configured actor.
        if registered_actor.oid in actors:
            raise Problem(503, "BEHAVIOR_CONFIGURATION", "a registered agent cannot reuse a configured actor")
        actors = {**actors, registered_actor.oid: registered_actor}
    for binding in runtime.bindings.values():
        actor = actors.get(binding.agent_oid)
        if (
            binding.tenant_id != verifier.auth.tenant_id or binding.human_oid != verifier.settings.human_oid
            or binding.team_id != verifier.settings.team or actor is None or actor.team != binding.team_id
        ):
            raise Problem(503, "BEHAVIOR_CONFIGURATION", "JWT and runtime principal bindings differ")
    if runtime.dep.native_launches is not None:
        for target in runtime.dep.native_launches.targets:
            actor = actors.get(target.agent_oid)
            if actor is None or actor.team != runtime.dep.native_launches.team_id:
                raise Problem(503, "BEHAVIOR_CONFIGURATION", "native executor MI must be explicitly allowlisted")
    router = APIRouter()
    identity = _principal_dependency(verifier)

    def add(path, model, action, handler, *, method="POST", render=_response):
        async def endpoint(request: Request, binding_id: str, principal: BehaviorPrincipal = Depends(identity)):
            binding, _, _ = runtime.authorize(binding_id, principal, action)
            values = dict(request.path_params)
            try:
                for name, value in values.items():
                    if name == "backup_id":
                        TypeAdapter(wire.BackupId).validate_python(value, strict=True)
                    elif name == "version":
                        if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > 2**31 - 1:
                            raise ValueError("invalid source version")
                        values[name] = int(value)
                    else:
                        _id.validate_python(value, strict=True)
                if "job_id" in values and values["job_id"] != binding.job_id:
                    raise ValueError("job is outside binding")
                if "child_id" in values and values["child_id"] not in {child.job_id for child in binding.child_jobs}:
                    raise ValueError("child is outside binding")
            except (ValueError, ValidationError):
                raise Problem(403, "BEHAVIOR_BINDING_DENIED", "path is outside the authorized binding") from None
            if request.query_params:
                raise Problem(422, "BEHAVIOR_INVALID_REQUEST", "query parameters are unsupported")
            if model is None and (
                request.headers.get("content-length", "0") != "0" or "transfer-encoding" in request.headers
            ):
                raise Problem(422, "BEHAVIOR_INVALID_REQUEST", "read endpoints do not accept a body")
            command = None if model is None else await _body(request, model)
            try:
                result = await run_in_threadpool(handler, values, command, principal)
                return render(result)
            except Problem:
                raise
            except Exception:
                raise Problem(503, "BEHAVIOR_DEPENDENCY_UNAVAILABLE", "required dependency failed") from None

        router.add_api_route(PREFIX + path, endpoint, methods=[method], name="behavior-" + path.replace("/", "-") + method)

    for stage in ("commit", "reconcile"):
        add("/sources/{source_id}/" + stage, wire.Command, "submit" if stage == "commit" else "read",
            lambda p, c, who, reconcile=stage == "reconcile": runtime.commit_source(
                p["binding_id"], p["source_id"], c, who, reconcile=reconcile,
            ))
    add("/sources/{source_id}/prepare", wire.SourcePrepare, "submit",
        lambda p, c, who: runtime.prepare_source(p["binding_id"], p["source_id"], c, who))
    for stage in ("withdraw", "withdrawal-reconcile"):
        add("/sources/{source_id}/" + stage, wire.SourceWithdrawal, "approve",
            lambda p, c, who, reconcile=stage == "withdrawal-reconcile": runtime.withdraw_source(
                p["binding_id"], p["source_id"], c, who, reconcile=reconcile,
            ))

    def exact_source(exact):
        if len(exact.original) > 8192:
            raise Problem(413, "BEHAVIOR_RESPONSE_LIMIT", "source exceeds the supported exact-output bound")
        return Response(exact.original, media_type=exact.event.media_type, headers={
            "Cache-Control": "no-store", "X-Source-SHA256": exact.event.reference.sha256,
            "X-Source-Version": str(exact.event.reference.version),
        })

    add("/sources/{source_id}/versions/{version}", None, "read",
        lambda p, _, who: runtime.read_source(p["binding_id"], p["source_id"], p["version"], who),
        method="GET", render=exact_source)
    add("/snapshots/{snapshot_id}/capture", wire.SnapshotCapture, "submit",
        lambda p, c, who: runtime.capture_snapshot(p["binding_id"], p["snapshot_id"], c, who))
    add("/snapshots/{snapshot_id}/verify", wire.Command, "read",
        lambda p, c, who: runtime.verify_snapshot(p["binding_id"], p["snapshot_id"], c, who),
        render=lambda result: _response({"manifest": result.manifest, "originals": result.originals}))
    if runtime.dep.source_backups is not None:
        add("/backups/{backup_id}/prepare", wire.BackupPrepare, "backup",
            lambda p, c, who: runtime.prepare_backup(p["binding_id"], p["backup_id"], c, who))
        for stage in ("upload", "reconcile"):
            add("/backups/{backup_id}/" + stage, wire.Command, "backup",
                lambda p, c, who, reconcile=stage == "reconcile": runtime.upload_backup(
                    p["binding_id"], p["backup_id"], c, who, reconcile=reconcile,
                ))
    add("/claims/{claim_id}/review", wire.ClaimReview, "approve",
        lambda p, c, who: runtime.review_claim(p["binding_id"], p["claim_id"], c, who))
    add("/claims/{claim_id}/preview", wire.ClaimDraft, "approve",
        lambda p, c, who: runtime.preview_claim(p["binding_id"], p["claim_id"], c, who))
    for stage in ("commit", "reconcile"):
        add("/claims/{claim_id}/" + stage, wire.Command, "submit" if stage == "commit" else "read",
            lambda p, c, who, reconcile=stage == "reconcile": runtime.commit_claim(
                p["binding_id"], p["claim_id"], c, who, reconcile=reconcile,
            ))
    add("/claims/{claim_id}", None, "read",
        lambda p, _, who: runtime.current_claim(p["binding_id"], p["claim_id"], who), method="GET")
    add("/context", wire.ContextRequest, "read",
        lambda p, c, who: runtime.context(p["binding_id"], c, who),
        render=lambda result: _response({"manifest": result.manifest, "claims": result.claims, "originals": result.originals}))
    add("/operation/prepare", wire.OperationPrepare, "read",
        lambda p, c, who: runtime.prepare_operation(p["binding_id"], c, who))
    add("/operation", None, "read", lambda p, _, who: runtime.operation_preview(p["binding_id"], who), method="GET")
    add("/operation/decision-state", None, "read",
        lambda p, _, who: runtime.operation_decision_state(p["binding_id"], who), method="GET")
    add("/operation/approve", wire.OperationApproval, "approve",
        lambda p, c, who: runtime.approve_operation(p["binding_id"], c, who))
    add("/operation/approval-reconcile", wire.OperationApproval, "read",
        lambda p, c, who: runtime.reconcile_operation_approval(p["binding_id"], c, who))
    add("/operation/revoke", wire.OperationRevocation, "approve",
        lambda p, c, who: runtime.revoke_operation(p["binding_id"], c, who))
    add("/operation/revocation-reconcile", wire.OperationRevocation, "read",
        lambda p, c, who: runtime.reconcile_operation_revocation(p["binding_id"], c, who))
    add("/operation/reject", wire.OperationRejection, "approve",
        lambda p, c, who: runtime.reject_operation(p["binding_id"], c, who))
    add("/operation/rejection-reconcile", wire.OperationRejection, "read",
        lambda p, c, who: runtime.reconcile_operation_rejection(p["binding_id"], c, who))
    for stage in ("execute", "reconcile"):
        add("/operation/" + stage, wire.Command, "execute" if stage == "execute" else "read",
            lambda p, c, who, reconcile=stage == "reconcile": runtime.execute_operation(p["binding_id"], c, who, reconcile=reconcile))
    for stage in ("submit", "reconcile"):
        add("/work/" + stage, wire.SubmitWork, "submit" if stage == "submit" else "read",
            lambda p, c, who, reconcile=stage == "reconcile": runtime.submit(p["binding_id"], c, who, reconcile=reconcile))
    add("/jobs/{job_id}", None, "read",
        lambda p, _, who: runtime.job_status(p["binding_id"], who), method="GET")
    for stage in ("enqueue", "reconcile"):
        add("/children/{child_id}/" + stage, wire.Command, "submit" if stage == "enqueue" else "read",
            lambda p, c, who, reconcile=stage == "reconcile": runtime.enqueue_child(
                p["binding_id"], p["child_id"], c, who, reconcile=reconcile,
            ))
    add("/children/{child_id}", None, "read",
        lambda p, _, who: runtime.child_status(p["binding_id"], p["child_id"], who), method="GET")
    if runtime.dep.memory_projections:
        for stage in ("plan", "start", "poll", "reconcile", "search", "context"):
            model = (wire.Command if stage == "plan" else
                     wire.MemoryProjectionQuery if stage in {"search", "context"} else wire.MemoryProjectionCommand)
            action = "submit" if stage in {"plan", "start", "poll"} else "read"
            add("/memory/" + stage, model, action,
                lambda p, c, who, operation=stage: runtime.memory_projection(
                    p["binding_id"], c, who, action=operation,
                ))

    def pull(result):
        metadata = base64.urlsafe_b64encode(result.binding.model_dump_json().encode()).decode()
        return Response(result.payload, media_type="application/json", headers={
            "Cache-Control": "no-store", "X-Behavior-Exchange": result.exchange_id,
            "X-Behavior-Ack-Token": result.acknowledgment_token, "X-Behavior-Binding": metadata,
            "X-Behavior-Payload-SHA256": result.binding.payload_digest,
            "X-Behavior-Confirmation-Level": "client_received",
        })

    add("/delivery/pull", wire.Command, "read", lambda p, c, who: runtime.fetch(p["binding_id"], c, who), render=pull)
    add("/delivery/pull-continue", wire.Command, "read",
        lambda p, c, who: runtime.continue_fetch(p["binding_id"], c, who), render=pull)
    def reconcile_pull(params, command, principal):
        try:
            return pull(runtime.reconcile_fetch(params["binding_id"], command, principal))
        except Problem as error:
            if error.body["error"]["code"] != "INBOX_FETCH_NOT_STARTED":
                raise
            return JSONResponse({"error": {
                "code": "INBOX_FETCH_NOT_STARTED", "message": "No exchange exists for the continued original fetch.",
                "original_request_id": command.request_id, "automatic_retry": False,
            }}, status_code=409, headers={"Cache-Control": "no-store"})
    add("/delivery/pull-reconcile", wire.Command, "read",
        reconcile_pull, render=lambda result: result)
    add("/delivery/acknowledge", wire.PullAcknowledgment, "acknowledge",
        lambda p, c, who: runtime.acknowledge(p["binding_id"], c, who))

    if runtime.dep.native_launches is not None:
        def private_capability(request, name):
            values = request.headers.getlist(name)
            if len(values) != 1 or re.fullmatch(r"[a-f0-9]{64}", values[0]) is None:
                raise Problem(403, "BEHAVIOR_EXECUTOR_DENIED", "one private native capability is required")
            return SecretStr(values[0])

        def callback(path, body_model, credential_model, header, method):
            async def endpoint(
                request: Request, binding_id: str, launch_id: str,
                principal: BehaviorPrincipal = Depends(identity),
            ):
                assistant_claim = (method == "claim" and runtime.mode == "native"
                                   and binding_id in getattr(runtime, "profiles", {})
                                   and runtime.bindings[binding_id].route.channel_id != "msteams")
                try:
                    if assistant_claim:
                        from .behavior_assistant import _read_conflicts
                        native = _read_conflicts(lambda: runtime.executor_callback(
                            binding_id, launch_id, principal, claim=True), phase="hosted_claim_authority")
                    else:
                        native = runtime.executor_callback(binding_id, launch_id, principal, claim=method == "claim")
                    capability = private_capability(request, header)
                    body = await _body(request, body_model)
                    field = "launch_permit" if method == "claim" else "report_token"
                    command = credential_model(**body.model_dump(), **{field: capability})
                    if method == "claim":
                        options = {"assistant_conflicts": True} if assistant_claim else {}
                        result = await run_in_threadpool(native.claim, binding_id, launch_id, command, principal, **options)
                        response = _response(result.model_dump(mode="json", exclude={"report_token"}))
                        if result.claim_state == "fresh" and result.report_token is not None:
                            response.headers["X-Behavior-Report-Token"] = result.report_token.get_secret_value()
                        return response
                    probe_id = request.path_params["probe_id"]
                    _id.validate_python(probe_id, strict=True)
                    operation = native.resolve_probe if method == "resolve" else native.report
                    result = await run_in_threadpool(operation, binding_id, launch_id, probe_id, command, principal)
                    return _response(result)
                except Problem as error:
                    if assistant_claim:
                        from .behavior_assistant_api import acquire_http_error
                        raise acquire_http_error(error, "hosted_claim") from None
                    raise
                except Exception:
                    raise Problem(503, "BEHAVIOR_DEPENDENCY_UNAVAILABLE", "native callback failed") from None

            router.add_api_route(PREFIX + path, endpoint, methods=["POST"], name="assigned-native-" + method)

        callback("/hosted/launches/{launch_id}/claim", wire.HostedClaimBody, wire.HostedClaimRequest,
                 "x-behavior-launch-permit", "claim")
        callback("/hosted/launches/{launch_id}/probes/{probe_id}/resolve", wire.HostedProbeBody, wire.HostedProbeRequest,
                 "x-behavior-report-token", "resolve")
        callback("/hosted/launches/{launch_id}/probes/{probe_id}/report", wire.HostedReportBody, wire.HostedReport,
                 "x-behavior-report-token", "report")
    return router


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def problem(_request, exc):
        if exc.status in {403, 409, 412, 413, 503}:
            _log_request_failure(_request, exc.body["error"]["code"])
        unknown = exc.body["error"]["code"] in {
            "BEHAVIOR_COMMIT_UNKNOWN", "WORK_EFFECT_SEND_UNKNOWN", "WORKER_START_UNKNOWN",
            "OPERATION_TRANSPORT_UNKNOWN", "SOURCE_PREPARATION_UNKNOWN",
            "SOURCE_BACKUP_UPLOAD_UNKNOWN", "SOURCE_BACKUP_RECOVERY_UNKNOWN",
        }
        code = "BEHAVIOR_COMMIT_UNKNOWN" if unknown else {
            400: "BEHAVIOR_INVALID_REQUEST", 401: "BEHAVIOR_UNAUTHENTICATED", 403: "BEHAVIOR_ACCESS_DENIED",
            404: "BEHAVIOR_NOT_FOUND", 409: "BEHAVIOR_CONFLICT", 412: "BEHAVIOR_CONFLICT",
            413: "BEHAVIOR_BODY_LIMIT", 415: "BEHAVIOR_INVALID_REQUEST", 422: "BEHAVIOR_INVALID_REQUEST",
            429: "BEHAVIOR_CAPACITY_EXHAUSTED",
        }.get(exc.status, "BEHAVIOR_DEPENDENCY_UNAVAILABLE")
        return JSONResponse(
            {"error": {"code": code, "message": "The bounded operation could not be completed.",
                       "reconcile_original": unknown, "automatic_retry": False}},
            status_code=exc.status, headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _exc):
        return JSONResponse({"error": {"code": "BEHAVIOR_INVALID_REQUEST", "message": "Invalid request."}}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(_request, _exc):
        _log_request_failure(_request, "UNCLASSIFIED")
        return JSONResponse(
            {"error": {"code": "BEHAVIOR_DEPENDENCY_UNAVAILABLE", "message": "A required dependency failed.",
                       "automatic_retry": False}},
            status_code=503,
        )

def create_behavior_app(runtime: BehaviorRuntime, verifier: BehaviorJwtVerifier) -> FastAPI:
    runtime.validate_startup()
    app = FastAPI(title="FA bounded behavior API", docs_url=None, redoc_url=None, openapi_url=None)
    _install_error_handlers(app)
    app.include_router(behavior_router(runtime, verifier))
    return app


def create_source_recovery_app(service: SourceBackupService, verifier: BehaviorJwtVerifier) -> FastAPI:
    """Independent privileged transfer, no BehaviorRuntime/Table/source access.

    Caller supplies a recovery-only service (sources=None) with a CURRENT
    independent RecoveryAuthorizer. The reference's archived scope/ACL is data.
    The 12 MiB JSON ceiling accommodates the complete <=8 MiB archive as base64;
    every byte is returned or an explicit error, never a partial restore.
    """
    if (
        not isinstance(service, SourceBackupService) or service.sources is not None
        or service._recovery is None or not callable(getattr(service._recovery, "authorize", None))
        or not isinstance(verifier, BehaviorJwtVerifier)
        or service.settings.tenant_id != verifier.auth.tenant_id
    ):
        raise Problem(503, "BEHAVIOR_CONFIGURATION", "independent authenticated recovery dependencies are required")
    app = FastAPI(title="FA quarantined source recovery", docs_url=None, redoc_url=None, openapi_url=None)
    _install_error_handlers(app)
    identity = _principal_dependency(verifier)

    @app.post("/behavior/v1/recovery/restore-quarantined")
    async def restore(request: Request, principal: BehaviorPrincipal = Depends(identity)):
        principal.require(verifier.settings.team, "restore")
        if principal.kind != "human" or principal.oid != verifier.settings.human_oid:
            raise Problem(403, "BEHAVIOR_ACCESS_DENIED", "only the configured delegated human may restore")
        command = await _body(request, wire.QuarantineRestore)
        try:
            reference = SourceBackupReference.model_validate_json(json.dumps(command.reference, allow_nan=False))
        except (ValueError, TypeError, ValidationError):
            raise Problem(422, "BEHAVIOR_INVALID_REQUEST", "invalid independently retained backup reference") from None
        try:
            result = await run_in_threadpool(service.restore_quarantined, principal, reference)
            return _response({
                "status": result.status, "activation_performed": False,
                "reference_sha256": result.reference.sha256, "manifest": result.manifest,
                "originals_base64": result.originals, "recovery_decision_id": result.authorization.decision_id,
            }, max_bytes=MAX_RECOVERY_RESPONSE_BYTES)
        except Problem:
            raise
        except Exception:
            raise Problem(503, "BEHAVIOR_DEPENDENCY_UNAVAILABLE", "independent recovery failed") from None

    return app
