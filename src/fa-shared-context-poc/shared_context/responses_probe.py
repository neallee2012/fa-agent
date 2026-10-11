"""N6-0 synthetic test-agent probe, not the production Responses executor."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
import re
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from pydantic import Field, TypeAdapter

from .behavior_foundry_probe import PROBE_AGENT_ID, PROBE_AGENT_NAME, PROBE_PROJECT_ENDPOINT, AgentVersion
from .behavior_wire import Digest, HostedIdentity, Identifier, WireModel
from .errors import Problem

PROFILE = "fa-foundry-responses-n6-v1"
HEADER = "x-client-fa-n6-probe"
STORE = "fa/v1/n6-probe"
INPUTS = {"observe": "FA N6 synthetic observation v1", "hold": "FA N6 synthetic interruption v1"}
HOLD_SECONDS = 120
RID = Annotated[str, Field(pattern=r"^n6-[0-9a-f]{32}$")]
PlatformId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")]
RejectionField = Literal[
    "probe_header", "input", "background", "store", "stream",
    "cid", "digest", "identity", "metadata", "record",
]
RejectionReason = Literal[
    "missing_or_mismatch", "fixed_literal_required", "string_required", "true_required", "false_required",
    "header_mismatch", "digest_mismatch", "invalid_shape",
]
log = logging.getLogger(__name__)


def digest_input(text: str, conversation_id: str) -> str:
    raw = json.dumps({"input": text, "conversation": conversation_id}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def identifier_compatible(value: str) -> bool:
    try:
        TypeAdapter(Identifier).validate_python(value)
    except ValueError:
        return False
    return True


class ProbeMetadata(WireModel):
    fa_agent_id: Literal["00000000-0000-0000-0000-00000000004f"]
    fa_team_id: Identifier


class ProbeObservation(WireModel):
    agent_id: Identifier
    agent_version: AgentVersion
    session_id: Identifier
    request_session_id: PlatformId | None
    session_guid_present: bool
    session_guid_sha256: Digest | None
    process_id: Annotated[int, Field(gt=0)]
    call_id_present: bool


class ProbeRecord(WireModel):
    schema_version: Literal["fa-foundry-responses-n6-v1"] = PROFILE
    request_id: RID
    client_cid: PlatformId
    submit_digest: Digest
    conversation_id: PlatformId
    response_id: PlatformId
    phase: Literal["observe", "hold"]
    metadata: ProbeMetadata
    agent_id: Identifier
    agent_version: AgentVersion
    session_id: Identifier
    request_session_id: PlatformId
    session_guid_sha256: Digest | None
    process_id: Annotated[int, Field(gt=0)]
    synthetic_effect_count: Literal[1] = 1


class ProbeRead(WireModel):
    operation: Literal["read_receipt", "read_conversation"]
    request_id: RID


class ProbeInterrupt(WireModel):
    operation: Literal["interrupt"]
    request_id: RID
    session_id: Identifier
    process_id: Annotated[int, Field(gt=0)]


Command = TypeAdapter(Annotated[ProbeRead | ProbeInterrupt, Field(discriminator="operation")])


class ProbeRequestFieldsMiddleware:
    """Drop unknown probe body values before the SDK can interpret transport extensions."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/responses":
            await self.app(scope, receive, send)
            return
        from azure.ai.agentserver.responses import CreateResponse

        messages = []
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request" or not message.get("more_body", False):
                break
        payload = None
        if messages[-1]["type"] == "http.request":
            try:
                payload = json.loads(b"".join(message.get("body", b"") for message in messages))
            except ValueError:
                pass  # Replay the original bytes so malformed JSON keeps the SDK's own error response.
        if isinstance(payload, dict):
            known = set(CreateResponse.__annotations__) | {"agent_session_id"}
            unknown = sorted(set(payload) - known)
            if unknown:
                log.warning("FA_N6_PROBE_IGNORED unknown_keys=%s", json.dumps(unknown[:20], separators=(",", ":")))
                raw = json.dumps({key: value for key, value in payload.items() if key in known}).encode("utf-8")
                scope = {**scope, "headers": [
                    (key, value) for key, value in scope["headers"] if key.lower() != b"content-length"
                ] + [(b"content-length", str(len(raw)).encode("ascii"))]}
                messages = [{"type": "http.request", "body": raw, "more_body": False}]
        replay = iter(messages)

        async def body():
            message = next(replay, None)
            return message if message is not None else await receive()

        await self.app(scope, body, send)


class ProbeSessionOnlyMiddleware:
    """The explicit interruption sandbox must not also host a desktop WebSocket."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403, "reason": "N6_PROBE_SESSION_ONLY"})
            return
        await self.app(scope, receive, send)


def observation(identity: HostedIdentity) -> dict:
    from azure.ai.agentserver.core import get_request_context

    context = get_request_context()
    guid = os.environ.get("FOUNDRY_AGENT_SESSION_GUID", "")
    return ProbeObservation.model_validate({
        "agent_id": identity.agent_id, "agent_version": identity.agent_version,
        "session_id": identity.session_id, "request_session_id": context.session_id,
        "session_guid_present": bool(guid),
        "session_guid_sha256": hashlib.sha256(guid.encode("utf-8")).hexdigest() if guid else None,
        "process_id": os.getpid(), "call_id_present": bool(context.call_id),
    }).model_dump(mode="json")


def http_summary(response: httpx.Response) -> dict:
    """Only hashes, status, safe IDs and the two declared scope fields leave this boundary."""
    summary = {"http_status": response.status_code, "body_sha256": hashlib.sha256(response.content).hexdigest()}
    for name in ("x-ms-request-id", "apim-request-id", "x-request-id"):
        value = response.headers.get(name)
        if value and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value):
            summary[name] = value
    try:
        body = response.json()
    except ValueError:
        return summary
    if not isinstance(body, dict):
        return summary
    for name in ("id", "status", "version"):
        value = body.get(name)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value):
            summary[name] = value
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        summary["metadata"] = {
            key: value for key in ("fa_agent_id", "fa_team_id")
            if isinstance((value := metadata.get(key)), str) and identifier_compatible(value)
        }
    if isinstance(body.get("data"), list):
        summary["item_count"] = len(body["data"])
        summary["has_more"] = body.get("has_more") is True
    return summary


@dataclass
class N6ResponsesProbe:
    identity: HostedIdentity
    interrupt_session: bool = False
    holding: ProbeRecord | None = field(default=None, init=False)
    _hold_claimed: bool = field(default=False, init=False)

    @classmethod
    def from_environment(cls, identity: HostedIdentity):
        profile = os.environ.get("FA_N6_RESPONSES_PROBE", "")
        interrupt_sid = os.environ.get("FA_N6_RESPONSES_INTERRUPT_SESSION_ID", "")
        if not profile:
            if interrupt_sid:
                raise Problem(422, "N6_PROBE_DISABLED", "interruption requires the dedicated test probe")
            return None
        if profile != PROFILE:
            raise Problem(422, "N6_PROBE_PROFILE_INVALID", "unknown N6 probe profile")
        if (
            identity.agent_id != PROBE_AGENT_ID or identity.agent_name != PROBE_AGENT_NAME
            or not os.environ.get("FOUNDRY_HOSTING_ENVIRONMENT")
            or os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/") != PROBE_PROJECT_ENDPOINT
            or os.environ.get("AGENTSERVER_TASKS_BACKEND", "").strip().lower() not in {"", "hosted"}
        ):
            raise Problem(403, "N6_PROBE_TARGET_INVALID", "N6 requires the approved test agent and Hosted backend")
        if interrupt_sid and not identifier_compatible(interrupt_sid):
            raise Problem(422, "N6_PROBE_SESSION_INVALID", "the interrupt session must be an explicit valid SID")
        return cls(identity, interrupt_session=bool(interrupt_sid and identity.session_id == interrupt_sid))

    @asynccontextmanager
    async def store(self, *, create: bool):
        from azure.ai.agentserver.core.storage import FoundryStateStore
        from azure.identity.aio import ManagedIdentityCredential

        async with ManagedIdentityCredential() as credential:
            store = (await FoundryStateStore.get_or_create(
                STORE, credential=credential, user_isolation=False, item_ttl_seconds=-1,
            ) if create else FoundryStateStore(STORE, credential=credential))
            async with store:
                descriptor = await store.get()
                if (descriptor.item_ttl_seconds != -1 or descriptor.name != STORE
                        or descriptor.user_isolation is not False):
                    raise Problem(409, "N6_PROBE_STORE_INVALID", "the existing synthetic store differs")
                yield store

    async def read(self, request_id: str) -> ProbeRecord:
        async with self.store(create=False) as store:
            return self.decode(await store.get_item("rid:" + request_id), request_id)

    def decode(self, item, request_id: str) -> ProbeRecord:
        if item is None:
            raise Problem(404, "N6_PROBE_RID_MISSING", "the original synthetic RID is missing; do not recreate")
        try:
            record = ProbeRecord.model_validate(item.value)
        except (ValueError, TypeError):
            raise Problem(409, "N6_PROBE_RECORD_INVALID", "the original synthetic record is invalid") from None
        if item.key != "rid:" + request_id or record.request_id != request_id or record.agent_id != self.identity.agent_id:
            raise Problem(409, "N6_PROBE_RECORD_CHANGED", "the original synthetic record differs")
        return record

    def request_record(self, request, context) -> ProbeRecord:
        field: RejectionField = "probe_header"
        reason: RejectionReason = "missing_or_mismatch"
        try:
            headers = context.client_headers
            if headers.get(HEADER) != PROFILE:
                raise ValueError()
            text = request.get("input")
            field = "input"
            reason = "fixed_literal_required" if isinstance(text, str) else "string_required"
            phase = next((key for key, value in INPUTS.items() if value == text), None)
            if phase is None:
                raise ValueError()
            field, reason = "background", "true_required"
            if request.get("background") is not True:
                raise ValueError()
            field = "store"
            if request.get("store") is not True:
                raise ValueError()
            field, reason = "stream", "false_required"
            if request.get("stream", False) is not False:
                raise ValueError()
            cid = context.conversation_id
            field, reason = "cid", "string_required"
            if not isinstance(cid, str):
                raise ValueError()
            reason = "header_mismatch"
            if headers.get("x-client-fa-cid") != cid:
                raise ValueError()
            field, reason = "digest", "digest_mismatch"
            if headers.get("x-client-fa-submit-digest") != digest_input(text, cid):
                raise ValueError()
            field, reason = "identity", "invalid_shape"
            observed = observation(self.identity)
            field = "metadata"
            metadata = ProbeMetadata.model_validate(request.get("metadata"))
            field = "record"
            return ProbeRecord(
                request_id=headers.get("x-client-fa-rid"), client_cid=headers.get("x-client-fa-cid"),
                submit_digest=headers.get("x-client-fa-submit-digest"), conversation_id=cid,
                response_id=context.response_id, phase=phase,
                metadata=metadata,
                **{key: observed[key] for key in (
                    "agent_id", "agent_version", "session_id", "request_session_id", "session_guid_sha256", "process_id")},
            )
        except (ValueError, TypeError):
            raise Problem(422, "N6_PROBE_REQUEST_INVALID", "only the fixed synthetic input and exact probe headers are accepted",
                          field=field, reason=reason) from None

    async def record_once(self, command: ProbeRecord, *, recovery: bool):
        from azure.ai.agentserver.core.storage import FoundryStorageConflictError

        async with self.store(create=not recovery) as store:
            key = "rid:" + command.request_id
            item = await store.get_item(key)
            fresh = False
            if item is None and not recovery:
                try:
                    await store.create_item(key, command.model_dump(mode="json"))
                    fresh = True
                except FoundryStorageConflictError:
                    pass  # A competing first entry won; read it, never repeat the synthetic effect.
                item = await store.get_item(key)
            original = self.decode(item, command.request_id)
            if any(getattr(original, name) != getattr(command, name) for name in (
                "client_cid", "submit_digest", "conversation_id", "phase", "metadata", "session_id",
            )):
                raise Problem(409, "N6_PROBE_RID_CHANGED", "this RID belongs to another input or session")
            return original, fresh

    async def handler(self, request, context, cancellation_signal):
        from azure.ai.agentserver.core.storage import FoundryStorageError
        from azure.ai.agentserver.responses import ResponseEventStream
        from azure.core.exceptions import AzureError

        invalid = None
        try:
            command = self.request_record(request, context)
        except Problem as error:
            invalid = error
        seed = {}
        reference = {"type": "agent_reference", "name": PROBE_AGENT_NAME}
        if invalid is None:
            for name in ("tools", "instructions"):
                if request.get(name):
                    log.warning("FA_N6_PROBE_IGNORED field=%s nonempty=true", name)
            seed = {
                "metadata": command.metadata.model_dump(mode="json"),
                "background": True, "conversation": command.conversation_id,
            }
            reference["version"] = command.agent_version
        stream = ResponseEventStream(
            response_id=context.response_id, request=seed, model="n6-probe",
            agent_reference=reference,
        )
        yield stream.emit_created()
        yield stream.emit_in_progress()
        owns_hold = False
        try:
            if invalid is not None:
                raise invalid
            if self._hold_claimed:
                raise Problem(409, "N6_PROBE_SESSION_BUSY", "the dedicated interruption probe is already running")
            if command.phase == "hold" and not context.is_recovery:
                if not self.interrupt_session:
                    raise Problem(403, "N6_PROBE_INTERRUPT_DISABLED", "hold requires the dedicated synthetic session")
                self._hold_claimed = owns_hold = True
            original, fresh = await self.record_once(command, recovery=context.is_recovery)
            result = {
                "profile": PROFILE, "status": "recovered_read_only" if context.is_recovery else "recorded",
                "record": original.model_dump(mode="json"), "observed": observation(self.identity),
                "response_id": context.response_id, "is_recovery": context.is_recovery,
                "cid_fa_identifier": identifier_compatible(original.conversation_id),
                "state_write_performed": fresh,
            }
            log.warning("FA_N6_PROBE %s", json.dumps(result, sort_keys=True))
            if command.phase == "hold" and fresh:
                self.holding = original
                cancel = asyncio.create_task(cancellation_signal.wait())
                shutdown = asyncio.create_task(context.shutdown.wait())
                try:
                    await asyncio.wait({cancel, shutdown}, timeout=HOLD_SECONDS, return_when=asyncio.FIRST_COMPLETED)
                    if context.shutdown.is_set():
                        await context.exit_for_recovery()
                    raise Problem(409, "N6_PROBE_CANCELLED" if cancellation_signal.is_set()
                                  else "N6_PROBE_INTERRUPT_NOT_OBSERVED", "the interruption probe did not recover")
                finally:
                    for pending in (cancel, shutdown):
                        pending.cancel()
                    await asyncio.gather(cancel, shutdown, return_exceptions=True)
            for event in stream.output_item_message(json.dumps(result, sort_keys=True)):
                yield event
            yield stream.emit_completed()
        except Problem as error:
            code = error.body["error"]["code"]
            if code == "N6_PROBE_REQUEST_INVALID":
                log.warning("FA_N6_PROBE_FAILED code=%s status=%s field=%s reason=%s",
                            code, error.status, error.body["error"]["field"], error.body["error"]["reason"])
            else:
                log.warning("FA_N6_PROBE_FAILED code=%s status=%s", code, error.status)
            yield stream.emit_failed(code="server_error", message=code)
        except (FoundryStorageError, AzureError, OSError) as error:
            status = getattr(error, "status_code", None)
            code = "N6_PROBE_ACCESS_DENIED" if status in {401, 403} else "N6_PROBE_STATE_UNKNOWN"
            log.warning("FA_N6_PROBE_FAILED code=%s status=%s", code, status)
            yield stream.emit_failed(code="server_error", message=code)
        finally:
            if owns_hold:
                self.holding, self._hold_claimed = None, False

    async def read_conversation(self, record: ProbeRecord):
        from azure.ai.agentserver.core import get_request_context
        from azure.identity.aio import ManagedIdentityCredential

        context = get_request_context()
        url = (PROBE_PROJECT_ENDPOINT + "/agents/" + PROBE_AGENT_NAME
               + "/endpoint/protocols/openai/conversations/" + quote(record.conversation_id, safe="") + "/items")
        async with ManagedIdentityCredential() as credential:
            token = await credential.get_token("https://ai.azure.com/.default")
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0), timeout=30,
                                         follow_redirects=False, trust_env=False) as client:
                response = await client.get(url, params={"api-version": "v1", "limit": 1}, headers={
                    "Authorization": "Bearer " + token.token, **context.platform_headers(),
                })
        result = {
            **http_summary(response), "request_id": record.request_id, "conversation_id": record.conversation_id,
            "credential_kind": "agent_managed_identity", "identity_kind": "agent_bearer_with_platform_caller_context",
            "call_id_present": bool(context.call_id), "target_metadata": record.metadata.model_dump(mode="json"),
            "target_metadata_source": "synthetic_probe_record",
            "code": "N6_D3_READ_OBSERVED" if response.status_code == 200 else "N6_D3_STOP",
        }
        return response.status_code if response.status_code in {200, 401, 403, 404} else 502, result

    def require_interrupt(self, command: ProbeInterrupt):
        if (not self.interrupt_session or self.holding is None or self.holding.phase != "hold"
                or command.session_id != self.identity.session_id
                or command.request_id != self.holding.request_id
                or command.process_id != self.holding.process_id or command.process_id != os.getpid()):
            raise Problem(409, "N6_PROBE_INTERRUPT_REFUSED", "only this process's one held synthetic RID may be interrupted")

    async def interrupt(self, command: ProbeInterrupt):
        self.require_interrupt(command)
        os._exit(86)


async def probe_response(request, probe: N6ResponsesProbe | None):
    from azure.ai.agentserver.core import get_request_context
    from azure.ai.agentserver.core.storage import FoundryStorageError
    from azure.core.exceptions import (
        AzureError, ClientAuthenticationError, HttpResponseError, ServiceRequestError, ServiceResponseError,
    )
    from azure.identity import CredentialUnavailableError
    from starlette.background import BackgroundTask
    from starlette.responses import JSONResponse

    from .behavior_http_diagnostics import _exception_type

    try:
        if probe is None or request.headers.getlist(HEADER) != [PROFILE]:
            raise Problem(403, "N6_PROBE_DISABLED", "this dedicated test probe is not enabled")
        if request.state.session_id != probe.identity.session_id:
            raise Problem(403, "N6_PROBE_SESSION_CHANGED", "the probe request targets another session")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 2048:
                raise Problem(413, "N6_PROBE_BODY_LIMIT", "the synthetic selector exceeds its bound")
            raw.extend(chunk)
        try:
            def unique(pairs):
                result = dict(pairs)
                if len(result) != len(pairs):
                    raise ValueError()
                return result
            command = Command.validate_python(json.loads(raw, object_pairs_hook=unique))
        except (ValueError, TypeError):
            raise Problem(422, "N6_PROBE_COMMAND_INVALID", "only a typed original RID selector is accepted") from None
        if isinstance(command, ProbeInterrupt):
            probe.require_interrupt(command)
            return JSONResponse({"status": "interrupt_scheduled", **command.model_dump(mode="json")},
                                status_code=202, background=BackgroundTask(probe.interrupt, command))
        record = await probe.read(command.request_id)
        if command.operation == "read_conversation":
            status, result = await probe.read_conversation(record)
            return JSONResponse(result, status_code=status)
        return JSONResponse({"status": "read_only", "record": record.model_dump(mode="json"),
                             "observed": observation(probe.identity)})
    except Problem as error:
        return JSONResponse(error.body, status_code=error.status)
    except (FoundryStorageError, AzureError, httpx.HTTPError, OSError) as error:
        status = getattr(error, "status_code", None)
        if status in {401, 403, 404}:
            return JSONResponse({"error": {"code": "N6_PROBE_STOP"}}, status_code=status)
        error_type = _exception_type(error)
        for known in (FoundryStorageError, AzureError, ClientAuthenticationError, HttpResponseError,
                      ServiceRequestError, ServiceResponseError, CredentialUnavailableError):
            if type(error) is known:
                error_type = known.__name__
                break
        reason = (f"http_status={status}" if type(status) is int and 100 <= status <= 599
                  else f"exception_type={error_type}")
        details = {
            "code": "N6_PROBE_READ_UNKNOWN", "reason": reason,
            "call_id_present": bool(get_request_context().call_id), "identity_kind": "not_observed",
        }
        # The operator already preserves safe error.message, but not arbitrary error fields.
        details["message"] = (reason + "; call_id_present=" + str(details["call_id_present"]).lower()
                              + "; identity_kind=not_observed")
        log.warning("FA_N6_PROBE_READ_FAILED code=%s %s", details["code"], details["message"])
        return JSONResponse({"error": details}, status_code=503)
