"""Pinned Foundry invocation transport and bounded executor integration.

actual_environment=NOT_RUN. Construction/import never acquires credentials,
creates a session, deploys, invokes a model, or enables native readiness.

Protocol sources (read 2026-09-15):
* controller/run_poc.py: v1 invocations endpoint and explicit existing session.
* Microsoft Learn, manage-hosted-sessions, Sessions versus conversations:
  Invocations binds a sandbox ONLY through the agent_session_id query parameter.
* azure-ai-agentserver-invocations 1.1.0 README and installed _invocation.py:
  POST /invocations; GET /invocations/{id}; POST /invocations/{id}/cancel;
  x-agent-invocation-id and POST x-agent-session-id response headers.

Do not copy the README's RAM task/result dictionaries as durable execution
authority. HTTP 200/202, a session's idle status, or cancel accepted alone is
never an ExecutorEvidence terminal/fencing proof.
The durable start_request_id is a logical FA identity, not a provider invocation
ID. Foundry generates the actual ID; only the first successful exact-permit claim
binds it. All later native requests and reports use that saved ID. A POST reply
alone cannot create the binding or authorize another POST.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import re
import secrets
import ssl
import time
from datetime import datetime, timedelta, timezone
from time import monotonic
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit

import httpx
from azure.core.credentials import TokenCredential
from pydantic import Field, SecretStr, field_validator

from . import behavior_wire as wire
from .behavior_auth import BehaviorPrincipal
from .behavior_delivery import (
    WorkDefinition, VerifiedWorkResult, _Model, _check, _digest, _json, _material, _parse, _update, _utc,
)
from .behavior_operations import BudgetRecord, ExecutionContext, OwnerRecord, PolicyRecord, control_key
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest, ControlWrite, Digest, InlineMaterial
from .behavior_workers import ExecutorAdapter, ExecutorBinding, ExecutorEvidence, ExecutorStart, ExecutorStop, JobRecord, WorkerCore, worker_key
from .errors import Problem
from .models import Identifier

FOUNDRY_TOKEN_SCOPE = "https://ai.azure.com/.default"
INVOCATION_ID_HEADER = "x-agent-invocation-id"
SESSION_ID_HEADER = "x-agent-session-id"
LAUNCH_PERMIT_HEADER = wire.NATIVE_LAUNCH_PERMIT_HEADER
REPORT_TOKEN_HEADER = "X-Behavior-Report-Token"
MAX_NATIVE_RESPONSE_BYTES = 64 * 1024
# A complete native page including JSON escaping, not a limit on a paginated answer.
MAX_NATIVE_TURN_HTTP_BYTES = 8 * 1024 * 1024
SESSION_VERSION_CHANGED = "CONVERSATION_SESSION_VERSION_CHANGED"
SESSION_LOST = "ASSISTANT_SESSION_LOST"
# Platform metadata GETs (agent, version, session) are idempotent: one bounded retry absorbs a single stalled or
# dropped read (stage 2026-10-06 12:40: one 10 s get_session read timeout ended a status poll). Status retries stay
# off because azure-core waits out a server Retry-After without a cap. Platform writes keep no retry.
PLATFORM_READ_RETRY = {"retry_total": 1, "retry_connect": 1, "retry_read": 1, "retry_status": 0,
                       "retry_backoff_factor": 0.5, "retry_backoff_max": 2}
PLATFORM_NO_RETRY = {"retry_total": 0, "retry_connect": 0, "retry_read": 0, "retry_status": 0}
# Official session statuses whose sandbox compute is not running. Only the terminal ones prove it on their own;
# "idle" (auto-suspended) closes an attempt only after the human's cancel and FA's own confirmed session stop.
TERMINAL_SESSION_STATUSES = frozenset({"failed", "deleted", "expired"})
LOST_SESSION_STATUSES = TERMINAL_SESSION_STATUSES | {"idle"}
# FA's own stop after the human's cancel succeeded (204, or 409 session_already_stopped: the platform documents
# both as stopped compute) but the session could not be read back. Never a passive proof.
STOP_ACCEPTED = "stop_accepted"
RUNNING_SESSION_STATUSES = frozenset({"creating", "active", "updating"})


def _proven_stop(session_status, stop_requested):
    return type(stop_requested) is bool and (
        session_status in TERMINAL_SESSION_STATUSES
        or stop_requested and (session_status in LOST_SESSION_STATUSES or session_status == STOP_ACCEPTED))
_START_CODES = frozenset({
    "FOUNDRY_SESSION_UNKNOWN", "FOUNDRY_SESSION_UNAVAILABLE", "FOUNDRY_CREDENTIAL_UNAVAILABLE",
    "FOUNDRY_CREDENTIAL_DISABLED", "FOUNDRY_INVOCATION_UNKNOWN", "FOUNDRY_RESPONSE_INVALID",
    "FOUNDRY_NATIVE_BINDING_MISMATCH", "NATIVE_RESPONSE_INVALID", "NATIVE_INVOCATION_UNKNOWN",
    "BEHAVIOR_CONFLICT", "BEHAVIOR_COMMIT_UNKNOWN",
    "CONVERSATION_SESSION_UNAVAILABLE", "CONVERSATION_SESSION_VERSION_CHANGED",
})
# The native operations Hosted admits a new RID through (``_submit_locked``) and the refusals it raises only before it.
_ADMISSION_OPERATIONS = frozenset({"submit", "agent_receive", "routine_receive", "continue"})
_PREFLIGHT_REFUSALS = frozenset({"NATIVE_CAPTURE_CHANGED", "NATIVE_PROFILE_UNSUPPORTED"})


def _upstream_status(error):
    status = error.body["error"].get("upstream_status") if isinstance(error, Problem) else getattr(error, "status_code", None)
    if type(status) is not int or not 100 <= status <= 599:
        status = getattr(getattr(error, "response", None), "status_code", None)
    return status if type(status) is int and 100 <= status <= 599 else None


def _session_failure(error, phase, *, stopping=False):
    status = _upstream_status(error)
    logging.getLogger(__name__).warning(
        "FOUNDRY_SESSION_UNRESOLVED phase=%s api_version=v1 status=%s", phase, status,
    )
    return Problem(
        503, "FOUNDRY_SESSION_STOP_UNKNOWN" if stopping else "FOUNDRY_SESSION_UNKNOWN",
        "original session stop remains unknown; do not repeat" if stopping else
        "original session create/read is unresolved; do not recreate",
        upstream_phase=phase, upstream_status=status, api_version="v1",
    )


def _valid_invocation_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value) is not None


class _NativeStartResponse(_Model):
    launch_id: Identifier
    invocation_id: Identifier
    status: Literal["pending", "accepted"]


class FoundryTarget(_Model):
    executor_id: Identifier
    project_endpoint: Annotated[str, Field(min_length=1, max_length=512)]
    agent_id: Annotated[str, Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
    agent_name: Identifier
    agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    session_id: Identifier
    agent_oid: Annotated[str, Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]

    @field_validator("project_endpoint")
    @classmethod
    def exact_project_endpoint(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)
            or not re.fullmatch(r"/api/projects/[A-Za-z0-9][A-Za-z0-9_-]{0,63}", parsed.path)
            or value.endswith("/")
        ):
            raise ValueError("an exact HTTPS Foundry project endpoint without credentials/query is required")
        return value


@dataclass(frozen=True)
class NativeResponse:
    status_code: int
    invocation_id: str
    session_id: str | None
    body: bytes


class FoundryHttpClient:
    """Fixed-route, one-request transport; never follows redirects or retries.

    transport is an explicit dependency for OFFLINE protocol tests. Production
    construction uses a TLS-verifying HTTPTransport(retries=0), no ambient proxy
    or credential fallback. The caller supplies a server-owned credential.
    """

    def __init__(
        self, target: FoundryTarget, credential: TokenCredential | None = None, *,
        transport: httpx.BaseTransport | None = None, timeout_seconds: float = 30,
    ):
        self.target = _check(target, FoundryTarget)
        if (
            type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 120
        ):
            raise Problem(422, "FOUNDRY_TIMEOUT_INVALID", "native timeout must be finite and at most 120 seconds")
        self.credential = credential
        self._session_requests = {}
        # ADR-0009 §7a zero-call guard, installed by the composition's native sessions and shared with every client
        # derived through ``for_target`` (see ``native_binding.check_session``).
        self._session_guard = [None]
        self.http = httpx.Client(
            transport=transport or httpx.HTTPTransport(retries=0, verify=ssl.create_default_context()),
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10)),
            follow_redirects=False, trust_env=False,
        )

    def close(self):
        self.http.close()

    def for_target(self,target):
        """Share only the HTTP connection pool/credential, never a live session target."""
        selected=object.__new__(FoundryHttpClient)
        selected.target=_check(target,FoundryTarget)
        selected.credential=self.credential
        selected.http=self.http
        selected._session_requests=self._session_requests
        selected._session_guard=getattr(self,"_session_guard",[None])
        return selected

    def last_session_request(self):
        return self._session_requests.get(self.target)

    def _check_session_version(self, session):
        from azure.ai.projects.models import VersionRefIndicator
        indicator = session.version_indicator
        if (session.agent_session_id == self.target.session_id and isinstance(indicator, VersionRefIndicator)
                and isinstance(indicator.agent_version, str)
                and re.fullmatch(r"[1-9][0-9]{0,15}", indicator.agent_version)
                and indicator.agent_version != self.target.agent_version):
            raise Problem(
                409, SESSION_VERSION_CHANGED,
                "the original session is bound to a different agent version; start a new conversation explicitly",
                expected_agent_version=self.target.agent_version, observed_agent_version=indicator.agent_version,
            )

    def create_session(self,*,before_send):
        from azure.ai.projects import AIProjectClient
        from azure.ai.projects.models import VersionRefIndicator
        from azure.core.exceptions import AzureError
        target=self.target
        phase="client_init"
        try:
            with AIProjectClient(endpoint=target.project_endpoint,credential=self.credential,
                                 retry_total=0,retry_connect=0,retry_read=0,retry_status=0) as project:
                phase="before_send"
                before_send()
                phase="create_session"
                session=project.agents.create_session(target.agent_name,
                    version_indicator=VersionRefIndicator(agent_version=target.agent_version),
                    agent_session_id=target.session_id)
                while True:
                    self._check_session_version(session)
                    if (session.agent_session_id!=target.session_id
                            or not isinstance(session.version_indicator,VersionRefIndicator)
                            or session.version_indicator.agent_version!=target.agent_version):
                        raise Problem(503,"FOUNDRY_SESSION_UNKNOWN","created original session/version differs")
                    if session.status in {"active","idle"}:
                        phase="client_close"
                        return
                    if session.status not in {"creating","updating"}:
                        raise Problem(503,"FOUNDRY_SESSION_UNAVAILABLE","original hosted session could not become ready")
                    phase="before_send"
                    before_send()
                    time.sleep(1)
                    phase="get_session"
                    session=project.agents.get_session(target.agent_name,target.session_id)
        except AzureError as error:
            raise _session_failure(error,phase) from None

    def stop_session(self,*,timeout=None):
        """True when this stop was accepted; False when the platform says the session was already stopped.
        ``timeout``: the transport's connection/read seconds (a background step's own bound) instead of azure-core's."""
        from azure.ai.projects import AIProjectClient
        from azure.core.exceptions import AzureError
        from .native_binding import check_session
        check_session(self,operation="stop_session")
        phase="client_init"
        bounded={} if timeout is None else {"connection_timeout":min(timeout,5),"read_timeout":timeout}
        try:
            with AIProjectClient(endpoint=self.target.project_endpoint,credential=self.credential,
                                 retry_total=0,retry_connect=0,retry_read=0,retry_status=0,**bounded) as project:
                phase="stop_session"
                project.agents.stop_session(self.target.agent_name,self.target.session_id)
                phase="client_close"
        except AzureError as error:
            if (getattr(error,"status_code",None)==409
                    and getattr(getattr(error,"error",None),"code",None)=="session_already_stopped"):
                return False
            raise _session_failure(error,phase,stopping=True) from None
        return True

    def verify_session(self,*,before_send):
        from azure.ai.projects import AIProjectClient
        from azure.ai.projects.models import VersionRefIndicator
        from azure.core.exceptions import AzureError
        try:
            with AIProjectClient(endpoint=self.target.project_endpoint,credential=self.credential,
                                 retry_total=0,retry_connect=0,retry_read=0,retry_status=0) as project:
                before_send()
                session=project.agents.get_session(self.target.agent_name,self.target.session_id)
        except AzureError as error:
            raise Problem(409,"CONVERSATION_SESSION_UNAVAILABLE",
                "the original session cannot be read; no replacement was created",
                upstream_status=_upstream_status(error)) from None
        self._check_session_version(session)
        if (session.agent_session_id!=self.target.session_id
                or not isinstance(session.version_indicator,VersionRefIndicator)
                or session.version_indicator.agent_version!=self.target.agent_version
                or session.status not in {"active","idle"}):
            raise Problem(409,"CONVERSATION_SESSION_UNAVAILABLE",
                          "the original session/version is unavailable; reset or new session must be explicit")
        return session

    def session_status(self):
        """Read the exact session without invoking or interpreting its lifecycle state."""
        from azure.ai.projects import AIProjectClient
        from azure.ai.projects.models import VersionRefIndicator
        from azure.core.exceptions import AzureError
        try:
            with AIProjectClient(endpoint=self.target.project_endpoint, credential=self.credential,
                                 retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
                                 connection_timeout=5, read_timeout=10) as project:
                session = project.agents.get_session(self.target.agent_name, self.target.session_id)
        except AzureError as error:
            raise Problem(503, "CONVERSATION_SESSION_UNAVAILABLE",
                          "the original session status could not be read",
                          upstream_status=_upstream_status(error)) from None
        if (session.agent_session_id != self.target.session_id
                or not isinstance(session.version_indicator, VersionRefIndicator)
                or session.version_indicator.agent_version != self.target.agent_version):
            raise Problem(409, "CONVERSATION_SESSION_CHANGED", "the original session identity or version differs")
        return session

    def session_idle_timeout(self):
        from azure.ai.projects import AIProjectClient
        from azure.core.exceptions import AzureError
        try:
            with AIProjectClient(endpoint=self.target.project_endpoint, credential=self.credential,
                                 retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
                                 connection_timeout=5, read_timeout=10) as project:
                version = project.agents.get_version(self.target.agent_name, self.target.agent_version)
        except AzureError:
            raise Problem(503, "HOSTED_IDLE_TIMEOUT_UNAVAILABLE", "the original version settings could not be read") from None
        if (version.name != self.target.agent_name or version.version != self.target.agent_version
                or version.definition.get("kind") != "hosted"):
            raise Problem(409, "CONVERSATION_SESSION_CHANGED", "the original agent version differs")
        # The pinned 2.3.0 model retains this newer service field as a JSON mapping.
        settings = version.definition.get("session_configuration")
        seconds = 900 if settings is None else settings.get("idle_timeout_seconds", 900) if isinstance(settings, dict) else None
        if type(seconds) is not int or not 120 <= seconds <= 3600:
            raise Problem(503, "HOSTED_IDLE_TIMEOUT_UNAVAILABLE", "the original idle timeout is invalid")
        return seconds

    def _request(
        self, method: Literal["GET", "POST"], invocation_id: str | None, *,
        action: Literal["start", "observe", "cancel", "turn"], body: bytes | None = None,
        probe_id: str | None = None, launch_permit: SecretStr | None = None, before_send=None,
        keepalive_launch_id: str | None = None,
        native_operation: str | None = None, native_stage: str | None = None, timeout: float | None = None,
    ) -> NativeResponse:
        native_turn = action == "turn"
        if native_turn:
            if (method != "POST" or invocation_id is not None or probe_id is not None
                    or launch_permit is not None or keepalive_launch_id is not None):
                raise Problem(422, "NATIVE_REQUEST_INVALID", "a native command cannot supply legacy execution headers")
        elif not _valid_invocation_id(invocation_id):
            raise Problem(422, "FOUNDRY_INVOCATION_INVALID", "an original immutable invocation ID is required")
        if self.credential is None:
            raise Problem(403, "FOUNDRY_CREDENTIAL_DISABLED", "an explicitly configured server credential is required")
        deadline = time.monotonic() + 120
        try:
            token = self.credential.get_token(FOUNDRY_TOKEN_SCOPE).token
        except Exception:
            raise Problem(503, "FOUNDRY_CREDENTIAL_UNAVAILABLE", "native credential acquisition failed") from None
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise Problem(503, "FOUNDRY_CREDENTIAL_UNAVAILABLE", "native credential is invalid")
        target = self.target
        url = f"{target.project_endpoint}/agents/{quote(target.agent_name, safe='')}/endpoint/protocols/invocations"
        params = {"api-version": "v1"}
        if action in {"start", "turn"}:
            params["agent_session_id"] = target.session_id
        else:
            url += "/" + quote(invocation_id, safe="")
            if action == "cancel":
                url += "/cancel"
        if probe_id is not None:
            if re.fullmatch(r"[a-f0-9]{64}", probe_id) is None:
                raise Problem(422, "FOUNDRY_PROBE_INVALID", "a server-issued probe ID is required")
        headers = {
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "Accept": "application/json", "Accept-Encoding": "identity",
        }
        if native_turn:
            from .behavior_native_turn_wire import NATIVE_EXPECTED_VERSION_HEADER, NATIVE_STATE_PROFILE, NATIVE_TURN_HEADER
            headers[NATIVE_TURN_HEADER] = NATIVE_STATE_PROFILE
            headers[NATIVE_EXPECTED_VERSION_HEADER] = target.agent_version
            if getattr(self, "_binding_row", None) is not None:
                # ADR-0009 §2 phase 3: only a row-located call asks Hosted for the stricter ``replaced()``.
                from .behavior_native_turn_wire import NATIVE_LOCATOR_BINDING_ROW, NATIVE_LOCATOR_HEADER
                headers[NATIVE_LOCATOR_HEADER] = NATIVE_LOCATOR_BINDING_ROW
        else:
            headers[INVOCATION_ID_HEADER] = invocation_id
        if probe_id is not None:
            headers[wire.NATIVE_PROBE_HEADER] = probe_id
        if launch_permit is not None:
            headers[LAUNCH_PERMIT_HEADER] = launch_permit.get_secret_value()
        if keepalive_launch_id is not None:
            if (action != "observe" or probe_id is not None
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", keepalive_launch_id) is None):
                raise Problem(422, "FOUNDRY_KEEPALIVE_INVALID", "keepalive must name the original read-only launch")
            headers[wire.NATIVE_KEEPALIVE_HEADER] = keepalive_launch_id
        if before_send is not None:
            before_send()
        sent_at = monotonic()
        response = None
        phase = "request"
        upstream_code = None
        files_check = None
        try:
            with self.http.stream(method, url, params=params, headers=headers, content=body, **(
                    {} if timeout is None else {"timeout": httpx.Timeout(timeout, connect=min(timeout, 10))})) as response:
                phase = "response_status"
                if response.status_code not in (200, 202) and not native_turn:
                    raise Problem(
                        503, "FOUNDRY_INVOCATION_UNKNOWN", "native response is not an execution result; reconcile original identity",
                        upstream_status=response.status_code,
                    )
                phase = "response_headers"
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise Problem(502, "FOUNDRY_RESPONSE_INVALID", "compressed native control responses are not accepted")
                if native_turn:
                    phase = "response_body"
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() > deadline or len(raw) + len(chunk) > MAX_NATIVE_TURN_HTTP_BYTES:
                            raise Problem(502, "FOUNDRY_RESPONSE_INVALID", "complete native page exceeds its transport bound")
                        raw.extend(chunk)
                    if response.status_code not in (200, 202):
                        phase = "response_status"
                        from .behavior_api import _nonfinite, _unique
                        from .behavior_native_turn_wire import NATIVE_TURN_ERRORS, PRE_DISPATCH_REASONS
                        try:
                            error = json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite)["error"]
                            code = error["code"]
                        except (ValueError, TypeError, KeyError):
                            code = None
                        upstream_code = (code if isinstance(code, str) and
                                         re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", code) and
                                         not re.fullmatch(r"[0-9a-fA-F]{32,64}", code) else "UNCLASSIFIED")
                        if (isinstance(code, str) and code in NATIVE_TURN_ERRORS
                                and 400 <= response.status_code <= 599):
                            if native_operation in {"session_files_omission", "session_follow"} and error.get("files_check") is not None:
                                from .behavior_native_turn_wire import NativeFilesCheckDetail
                                try:
                                    files_check = NativeFilesCheckDetail.model_validate(error["files_check"]).model_dump(exclude_none=True)
                                except ValueError:
                                    raise Problem(502, "NATIVE_STATE_INVALID", "the original file check detail is invalid") from None
                            # Only Hosted's submit marks a definite refusal before ``rid:<RID>`` exists; a refusal
                            # before dispatch (ADR-0009 §4) also names why (``reason``), which the phase 3 send flow
                            # uses to tell a same-SID version drift from a moved Agent record.
                            # An earlier SDK runtime still exiting (#903) refuses an Agent message's receive the same way.
                            not_dispatched = (error.get("not_dispatched") is True and (
                                native_operation == "submit" and (400 <= response.status_code < 500
                                                                  or code == "AGENT_VERSION_FOLLOWING")
                                or native_operation in {"submit", "agent_receive"}
                                and code == "NATIVE_EXECUTION_UNAVAILABLE")
                                # ADR-0009 §3 P1: every image since the 9aaa0824 floor raises these
                                # two only before rid:<RID> (preflight, conversation capture check),
                                # also those before 580c980e that never set the field.
                                or native_operation in _ADMISSION_OPERATIONS
                                and response.status_code == 409 and code in _PREFLIGHT_REFUSALS)
                            reason = error.get("reason") if error.get("reason") in PRE_DISPATCH_REASONS else None
                            # ADR-0009 §4 step 6: a newer image names the VM's own version in its version refusal.
                            actual_version = error.get("actual_version")
                            reported = (code in {"NATIVE_SESSION_CHANGED", "NATIVE_TURNS_DISABLED"}
                                        and isinstance(actual_version, str)
                                        and re.fullmatch(r"[1-9][0-9]{0,15}", actual_version) is not None)
                            # ADR-0009 4c-2: a replaced session names the Agent's current SID (error body only).
                            current = error.get("current_hosted_session_id")
                            named = (code == "NATIVE_SESSION_CHANGED" and isinstance(current, str)
                                     and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", current) is not None)
                            # ADR-0009 U3/U9: an image before #906 refuses the G0b-reserved commands with exactly this
                            # message; any other NATIVE_REQUEST_INVALID is a refused request, not an older image.
                            reserved = (code == "NATIVE_REQUEST_INVALID"
                                        and native_operation in {"session_checkpoint_check", "session_prerestore"}
                                        and error.get("message")
                                        == f"{native_operation} is reserved; this image does not run it")
                            # ADR-0009 5b: an image before it refuses a body with ``workspace``/``files_restore`` (its
                            # wire forbids unknown fields) with exactly this message, after this API validated it.
                            untyped = (code == "NATIVE_REQUEST_INVALID" and response.status_code == 422
                                       and native_operation in {"session_read", "session_cut"}
                                       and error.get("message") == "only the typed native request is accepted")
                            raise Problem(response.status_code, code,
                                          "the native agent refused this original command; no automatic retry",
                                          automatic_retry=False, **({} if files_check is None else {"files_check": files_check}),
                                          **({"not_dispatched": True} if not_dispatched else {}),
                                          **({"reason": reason} if reason is not None else {}),
                                          **({"actual_version": actual_version} if reported else {}),
                                          **({"current_hosted_session_id": current} if named else {}),
                                          **({"reserved": True} if reserved else {}),
                                          **({"untyped": True} if untyped else {}))
                        raise Problem(503, "FOUNDRY_INVOCATION_UNKNOWN",
                                      "native invocation outcome is unknown; reconcile the original request",
                                      upstream_status=response.status_code)
                    phase = "response_headers"
                    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                        raise Problem(502, "FOUNDRY_RESPONSE_INVALID", "a complete native JSON page is required")
                actual_id = response.headers.get(INVOCATION_ID_HEADER)
                actual_session = response.headers.get(SESSION_ID_HEADER)
                if not _valid_invocation_id(actual_id) or (
                    action not in {"start", "turn"} and actual_id != invocation_id
                    or len(response.headers.get_list(INVOCATION_ID_HEADER)) != 1
                    or len(response.headers.get_list(SESSION_ID_HEADER)) > 1
                    or action in {"start", "turn"} and actual_session != target.session_id
                    or actual_session is not None and actual_session != target.session_id
                ):
                    raise Problem(502, "FOUNDRY_NATIVE_BINDING_MISMATCH", "native invocation/session echo differs")
                if not native_turn:
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() > deadline or len(raw) + len(chunk) > MAX_NATIVE_RESPONSE_BYTES:
                            raise Problem(502, "FOUNDRY_RESPONSE_INVALID", "native control response exceeds its bound")
                        raw.extend(chunk)
                self._session_requests[target] = sent_at
                if native_turn and native_stage is not None:
                    # Each answered follow/files-omission stage, so a fence's order can be rebuilt from the log;
                    # a failed stage is in NATIVE_INVOCATION_FAILURE below.
                    logging.getLogger(__name__).info("NATIVE_STAGE %s", json.dumps({
                        "native_operation": native_operation, "native_stage": native_stage,
                        "status": response.status_code, "elapsed_ms": round((monotonic() - sent_at) * 1000),
                        "agent_id": target.agent_id, "agent_name": target.agent_name,
                        "session_id": target.session_id}, sort_keys=True))
                phase = "response_close"
                return NativeResponse(response.status_code, actual_id, actual_session, bytes(raw))
        except (Problem, httpx.HTTPError) as error:
            if native_turn:
                from .behavior_http_diagnostics import _exception_type, native_response_diagnostics
                details = {
                    "phase": phase, "status": None if response is None else response.status_code,
                    "exception_type": None if isinstance(error, Problem) else _exception_type(error),
                    "elapsed_ms": round((monotonic() - sent_at) * 1000),
                    "agent_id": target.agent_id, "agent_name": target.agent_name, "session_id": target.session_id,
                    "path": "invocations", "api_version": "v1",
                    "upstream_error_code": upstream_code, "native_operation": native_operation,
                    "native_stage": native_stage, "expected_agent_version": target.agent_version,
                    **({} if files_check is None else {"files_check": files_check}),
                }
                if response is not None:
                    details.update(native_response_diagnostics(response.headers))
                # Reading a `rid:<RID>` that does not exist is an answer (usually the check before a first send),
                # not a failed invocation; the details stay the same for real "original missing" diagnosis.
                absent = (native_operation == "read" and response is not None and response.status_code == 404
                          and upstream_code == "NATIVE_RID_MISSING")
                logging.getLogger(__name__).warning("%s %s", "NATIVE_RID_ABSENT" if absent
                                                    else "NATIVE_INVOCATION_FAILURE", json.dumps(details, sort_keys=True))
            if isinstance(error, Problem):
                raise
            raise Problem(503, "FOUNDRY_INVOCATION_UNKNOWN", "native transport outcome is unknown; do not resend") from None

    def invoke_native(self, command, *, before_send=None, timeout=None) -> NativeResponse:
        """All N1 reads and writes use the fixed invocation protocol; no launch, claim or invented call ID.
        ``timeout``: seconds for this call instead of the client's (a background step's own bound)."""
        from .behavior_native_turn_wire import NATIVE_REQUEST, canonical_bytes
        from .native_binding import check_session
        operation = getattr(command, "operation", None)
        # ADR-0009 §7a zero-call check: before anything about the command is used, so every operation is covered.
        check_session(self, operation=operation if isinstance(operation, str) else "unknown")
        try:
            checked = NATIVE_REQUEST.validate_json(command.model_dump_json())
        except (AttributeError, ValueError):
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original typed native command is required") from None
        body = canonical_bytes(checked)
        if len(body) > MAX_NATIVE_TURN_HTTP_BYTES:
            raise Problem(413, "NATIVE_REQUEST_TOO_LARGE", "the complete native command exceeds its transport bound")
        return self._request("POST", None, action="turn", body=body, before_send=before_send,
                             native_operation=checked.operation, native_stage=getattr(checked, "stage", None),
                             timeout=timeout)

    def start(self, invocation_id: str, launch_id: str, binding_id: str, launch_permit: SecretStr, *, before_send=None) -> NativeResponse:
        if any(
            not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value) is None
            for value in (launch_id, binding_id)
        ):
            raise Problem(422, "FOUNDRY_LAUNCH_INVALID", "an immutable server launch ID is required")
        if (
            not isinstance(launch_permit, SecretStr)
            or re.fullmatch(r"[a-f0-9]{64}", launch_permit.get_secret_value()) is None
        ):
            raise Problem(403, "FOUNDRY_LAUNCH_PERMIT_REQUIRED", "a server-issued one-use native permit is required")
        response = self._request(
            "POST", invocation_id, action="start",
            body=json.dumps({
                "binding_id": binding_id, "launch_id": launch_id,
            }, sort_keys=True, separators=(",", ":")).encode("ascii"),
            launch_permit=launch_permit,
            before_send=before_send,
        )
        try:
            def unique(pairs):
                value = dict(pairs)
                if len(value) != len(pairs):
                    raise ValueError("duplicate native start field")
                return value

            json.loads(response.body, object_pairs_hook=unique)
            acknowledgment = _NativeStartResponse.model_validate_json(response.body)
        except (ValueError, TypeError):
            raise Problem(502, "FOUNDRY_NATIVE_BINDING_MISMATCH", "native start response lacks its exact launch binding") from None
        if acknowledgment.launch_id != launch_id or acknowledgment.invocation_id != response.invocation_id:
            raise Problem(502, "FOUNDRY_NATIVE_BINDING_MISMATCH", "native start response belongs to another launch/invocation")
        return response

    def observe(self, invocation_id: str, probe_id: str) -> NativeResponse:
        return self._request("GET", invocation_id, action="observe", probe_id=probe_id)

    def keepalive(self, invocation_id: str, launch_id: str, *, before_send):
        response = self._request("GET", invocation_id, action="observe", before_send=before_send,
                                 keepalive_launch_id=launch_id)
        try:
            value = wire.HostedKeepalive.model_validate_json(response.body)
        except ValueError:
            raise Problem(502, "FOUNDRY_KEEPALIVE_INVALID", "the original read-only response differs") from None
        if (response.status_code != 200 or value.session_id != self.target.session_id
                or value.launch_id != launch_id or value.invocation_id != invocation_id):
            raise Problem(502, "FOUNDRY_KEEPALIVE_INVALID", "keepalive response belongs to another launch or session")
        return value

    def cancel(self, invocation_id: str, probe_id: str) -> NativeResponse:
        return self._request("POST", invocation_id, action="cancel", probe_id=probe_id, body=b"{}")


SUPPORTED_CHECKS = frozenset({
    "checksum_matches", "byte_count_matches", "newline_count_matches",
    "native_binding_verified", "executor_idle_verified",
})


class NativeJobRoute(_Model):
    job_id: Identifier
    api_binding_id: Identifier
    grant_id: Identifier


class NativeExecutionGrant(_Model):
    """Provision ONLY through trusted server control, never executor/client JSON."""

    kind: Literal["native_execution_grant"] = "native_execution_grant"
    grant_id: Identifier
    controller_id: Identifier
    owner: ExecutionContext
    policy_revision: Annotated[int, Field(ge=0)]
    job_id: Identifier
    definition: WorkDefinition
    input_digest: Digest
    targets: Annotated[tuple[FoundryTarget, ...], Field(min_length=1, max_length=4)]
    active: bool
    remaining_uses: Annotated[int, Field(ge=0, le=8)]
    not_before: datetime
    expires_at: datetime | None
    workload_version: Literal["checksum-count-v1", "assistant-v1.2"] = "checksum-count-v1"
    checkpoint_after_bytes: Annotated[int, Field(ge=1, le=8192)] | None = None

    _times = field_validator("not_before", "expires_at")(lambda v:None if v is None else _utc(v))


class _TargetReservation(_Model):
    kind: Literal["native_target_reservation"] = "native_target_reservation"
    target: FoundryTarget
    configuration_digest: Digest
    generation: Annotated[int, Field(ge=0)] = 0
    active_launch_id: Identifier | None = None
    job_id:Identifier|None=Field(default=None,exclude_if=lambda value:value is None)


def instruction_target(template,job_id,*,agent_version=None,session_id=None):
    target = FoundryTarget.model_validate({**template.model_dump(),
        "session_id":("fa-work-"+native_key("session",job_id)[:48]) if session_id is None else session_id,
        "agent_version":template.agent_version if agent_version is None else agent_version})
    if int(target.agent_version) < int(template.agent_version):
        raise Problem(503, "NATIVE_CONFIGURATION_INVALID", "instruction Hosted version cannot precede its approved template")
    return target


class InstructionSessionStop(_Model):
    kind:Literal["instruction_session_stop"]="instruction_session_stop"
    launch_id:Identifier
    job_id:Identifier
    target:FoundryTarget
    confirmed:bool=False


class _NativeLaunch(_Model):
    kind: Literal["native_launch"] = "native_launch"
    launch_id: Identifier
    configuration_digest: Digest
    api_binding_id: Identifier
    grant_id: Identifier
    start: ExecutorStart
    definition: WorkDefinition
    target: FoundryTarget
    process_generation: Annotated[int, Field(ge=1)]
    permit_digest: Digest
    post_digest: Digest
    phase: Literal["post_unknown", "claimed", "sealed"] = "post_unknown"
    native_invocation_id: Identifier | None = None
    claim_id: Identifier | None = None
    process_id: Identifier | None = None
    report_token_digest: Digest | None = None
    last_report_sequence: Annotated[int, Field(ge=0)] = 0
    evidence_sequence: Annotated[int, Field(ge=0)] = 0
    probes_reserved: Annotated[int, Field(ge=0, le=32)] = 0
    checkpoint_after_bytes: Annotated[int, Field(ge=1, le=8192)] | None = None
    checkpoint: bytes | None = None
    sealed_evidence_id: Identifier | None = None
    child_pid: Annotated[int, Field(ge=1)] | None = None
    pending_control: Literal["observe", "park", "cancel"] = "observe"
    last_report_fingerprint: Digest | None = None
    workload_version: Literal["checksum-count-v1", "assistant-v1.2"] = Field(
        default="checksum-count-v1", exclude_if=lambda value: value == "checksum-count-v1",
    )


class _NativeProbe(_Model):
    kind: Literal["native_probe"] = "native_probe"
    probe_id: Identifier
    launch_id: Identifier
    directive: Literal["observe", "park", "cancel"]
    control_request_id: Identifier | None
    issued_at: datetime
    expires_at: datetime

    _times = field_validator("issued_at", "expires_at")(_utc)


class _NativeEvidence(_Model):
    kind: Literal["native_executor_evidence"] = "native_executor_evidence"
    launch_id: Identifier
    configuration_digest: Digest
    probe_id: Identifier
    evidence: ExecutorEvidence
    observed_at: datetime
    expires_at: datetime
    report_digest: Digest

    _times = field_validator("observed_at", "expires_at")(_utc)


def native_key(kind: str, *ids: str) -> str:
    return control_key("native-" + kind, *ids)


def _secret_digest(value: SecretStr) -> str:
    if not isinstance(value, SecretStr) or re.fullmatch(r"[a-f0-9]{64}", value.get_secret_value()) is None:
        raise Problem(403, "NATIVE_PERMIT_DENIED", "required private execution capability is invalid")
    return hashlib.sha256(value.get_secret_value().encode("ascii")).hexdigest()


def _identity(target: FoundryTarget):
    return wire.HostedIdentity(**{field: getattr(target, field) for field in (
        "agent_id", "agent_name", "agent_version", "session_id",
    )})


def _resident_release(report):
    """Sealed resident-PoC report shape: the owned runtime is idle and unbound, not exited."""
    stop = report.sdk_stop
    return (stop is not None and stop.release is not None and not report.child_exited
            and report.exit_code is None and report.child_pid == stop.pid)


class NativeRecoveryBinding(_Model):
    configuration_sha256: Digest
    operator_oid: Identifier
    credential_client_id: Identifier


def _unused_assignment_keys(binding, operation_ids):
    from .behavior_assistant import key as assistant_key
    from .behavior_sdk_checkpoint import checkpoint_key
    sid = assistant_key("session-id", binding.start_request_id)
    return (
        *(assistant_key(kind, sid) for kind in ("session", "archive", "restore")),
        *(assistant_key(kind, binding.work_id) for kind in ("artifact", "result")),
        *(assistant_key(kind, binding.job_id) for kind in ("text-archive", "reply")),
        checkpoint_key(binding.job_id),
        *(control_key("operation", operation_id) for operation_id in operation_ids),
    )


def _version_refusal_result(launch, proof, verifier_version):
    return VerifiedWorkResult(
        evidence_id=native_key("version-refusal-result", launch.launch_id), verifier_version=verifier_version,
        work_id=launch.start.binding.work_id, definition_digest=_digest(launch.definition.model_dump(mode="json")),
        state="failed", passed_checks=(),
        content=_material({"schema_version": "fa-assistant-notice-v1", "state": "failed",
                           "code": SESSION_VERSION_CHANGED, "sdk_reply_available": False}).data,
        proof=_json(proof),
    )


def _lost_session_result(launch, proof, verifier_version):
    return VerifiedWorkResult(
        evidence_id=native_key("lost-session-result", launch.launch_id), verifier_version=verifier_version,
        work_id=launch.start.binding.work_id, definition_digest=_digest(launch.definition.model_dump(mode="json")),
        state="failed", passed_checks=(),
        content=_material({"schema_version": "fa-assistant-notice-v1", "state": "failed",
                           "code": SESSION_LOST, "sdk_reply_available": False}).data,
        proof=_json(proof),
    )


def lost_session_failure(store, job):
    """The original turn ended because its session compute stopped, per its sealed evidence."""
    if job is None or job.state not in {"failed", "cancelled"} or job.binding is None:
        return None
    launch_id = native_key("launch-id", job.binding.start_request_id)
    launch_key = native_key("launch", launch_id)
    evidence_key = native_key("evidence", native_key("lost-session-evidence", launch_id))
    rows = store.read_snapshot((launch_key, evidence_key))
    if rows[launch_key] is None:
        return None
    launch = _parse(rows[launch_key].payload["data"], _NativeLaunch)
    if launch.sealed_evidence_id != native_key("lost-session-evidence", launch_id):
        return None
    if rows[evidence_key] is None:
        raise Problem(409, "NATIVE_EVIDENCE_INVALID", "the original lost-session evidence is incomplete")
    saved = _parse(rows[evidence_key].payload["data"], _NativeEvidence)
    if _parse(rows[evidence_key].payload["material"], InlineMaterial) != _material(saved.model_dump(mode="json")):
        raise Problem(409, "NATIVE_EVIDENCE_INVALID", "the original lost-session material differs")
    NativeLaunchAuthority._verify_lost_session(None, launch, saved)
    if launch.start.binding != job.binding:
        raise Problem(409, "NATIVE_EVIDENCE_INVALID", "lost-session evidence belongs to another original start")
    return SESSION_LOST


def session_version_failure(store, job):
    """Read the original failed claim-gate fence, not current platform state."""
    if job is None or job.state != "failed" or job.binding is None:
        return None
    launch_id = native_key("launch-id", job.binding.start_request_id)
    fence_key = native_key("unclaimed-fence", launch_id)
    launch_key = native_key("launch", launch_id)
    evidence_key = native_key("evidence", native_key("unclaimed-fence-evidence", launch_id))
    rows = store.read_snapshot((fence_key, launch_key, evidence_key))
    fence = rows[fence_key]
    if fence is None or "failure_code" not in fence.payload["data"]:
        return None
    if rows[launch_key] is None or rows[evidence_key] is None:
        raise Problem(409, "NATIVE_EVIDENCE_INVALID", "the original version refusal is incomplete")
    launch = _parse(rows[launch_key].payload["data"], _NativeLaunch)
    evidence = _parse(rows[evidence_key].payload["data"], _NativeEvidence)
    if _parse(rows[evidence_key].payload["material"], InlineMaterial) != _material(evidence.model_dump(mode="json")):
        raise Problem(409, "NATIVE_EVIDENCE_INVALID", "the original version refusal material differs")
    NativeLaunchAuthority._verify_unclaimed_fence(None, launch, evidence, fence)
    if launch.start.binding != job.binding or evidence.evidence.state != "failed":
        raise Problem(409, "NATIVE_EVIDENCE_INVALID", "version refusal belongs to another original start")
    return SESSION_VERSION_CHANGED


class NativeLaunchAuthority:
    """Server-internal controller plus narrowly assigned executor callbacks.

    prepare/probe are NEVER generic-agent HTTP methods. Only claim,
    resolve_probe and report are suitable for an optional authenticated executor
    route. Every callback checks the assigned MI AND a native-delivered private
    capability; asserting a session/process with an MI is not a fence.
    """

    def __init__(
        self, workers: WorkerCore, *, team_id: str, controller_id: str,
        targets: tuple[FoundryTarget, ...], jobs: tuple[NativeJobRoute, ...],
        verifier_version: str, clock=lambda: datetime.now(timezone.utc),
        recovery_binding: NativeRecoveryBinding | None = None,
    ):
        targets = tuple(_check(target, FoundryTarget) for target in targets)
        jobs = tuple(_check(job, NativeJobRoute) for job in jobs)
        if (
            len(targets) != workers.contract.capacity or not 1 <= len(jobs) <= 32
            or len({target.executor_id for target in targets}) != len(targets)
            or len({target.agent_id for target in targets}) != len(targets)
            or len({target.session_id for target in targets}) != len(targets)
            or len({job.job_id for job in jobs}) != len(jobs)
        ):
            raise Problem(403, "NATIVE_CONFIGURATION_INVALID", "one/four distinct exact executors and bounded job routes are required")
        self.workers, self.store = workers, workers.store
        self.team_id, self.controller_id, self.verifier_version = team_id, controller_id, verifier_version
        self.targets, self.jobs, self.clock = targets, {job.job_id: job for job in jobs}, clock
        self.configured_targets = targets
        self.assistant = None
        self.recovery_binding = None if recovery_binding is None else _check(recovery_binding, NativeRecoveryBinding)

    def _now(self):
        return _utc(self.clock())

    def _row(self, rows, key, model):
        row = rows[key]
        if row is None or row.payload["binding"] != self.workers.contract.control_binding:
            raise Problem(409, "NATIVE_RECORD_MISSING", "required bound native authority record is missing")
        value = _parse(row.payload["data"], model)
        if isinstance(value, (_NativeLaunch, _NativeEvidence, _TargetReservation)) and (
            value.configuration_digest != self.workers.configuration_digest()
        ):
            raise Problem(403, "NATIVE_CONFIGURATION_MISMATCH", "native/offline worker configuration differs")
        return value

    def _write(self, key, value, rows=None, *, material=None):
        previous = None if rows is None else rows.get(key)
        if previous is not None and previous.payload["material"] is not None and material is None:
            material = _parse(previous.payload["material"], InlineMaterial)
        return ControlWrite(
            row_id=key, mode="create" if previous is None else "replace",
            data=value.model_dump(mode="json"), material=material,
        )

    def _request(self, kind, ids, body, keys):
        return ControlRequest(
            request_id=native_key(kind, *ids), request_digest=_digest(body),
            read_set=tuple(sorted(set(keys))),
        )

    def launch(self, launch_id: str) -> _NativeLaunch:
        key = native_key("launch", launch_id)
        return self._row(self.store.read_snapshot((key,)), key, _NativeLaunch)

    def instruction_session_state(self, job):
        """Stored state of this exact execution, not evidence that its process stopped."""
        if job is None or job.binding is None or job.result_forwarded:
            return None
        from .behavior_assistant import _Session, key as assistant_key
        key = assistant_key("session", assistant_key("session-id", job.binding.start_request_id))
        rows = self.store.read_snapshot((key,))
        if rows[key] is None:
            return None
        session = self._row(rows, key, _Session)
        if session.execution == job.binding and session.assignment.task.instruction is not None:
            return session.state
        return None

    def pending_completion_observation(self, job):
        """A retained final can wake the original observer without trusting a notification."""
        return self.instruction_session_state(job) == "ready_to_finalize"

    def instruction_recently_active(self, job):
        return (self.assistant is not None and self.instruction_session_state(job) == "running"
                and self.assistant.instruction_recently_active(job.binding))

    def pending_lost_observation(self, job):
        """A sealed lost-session failure still owes its forwarding; the observer delivers the original notice."""
        return job is not None and not job.result_forwarded and lost_session_failure(self.store, job) is not None

    def pending_version_observation(self, job):
        if job is not None and job.state == "failed":
            return not job.result_forwarded and session_version_failure(self.store, job) == SESSION_VERSION_CHANGED
        if (job is None or job.binding is None or job.generation != 1
                or job.state not in {"starting", "capacity_uncertain"}):
            return False
        key = native_key("launch", native_key("launch-id", job.binding.start_request_id))
        rows = self.store.read_snapshot((key,))
        if rows[key] is None:
            return False
        launch = self._row(rows, key, _NativeLaunch)
        if (launch.start.binding != job.binding or launch.native_invocation_id is not None
                or launch.claim_id is not None or launch.process_id is not None
                or launch.child_pid is not None or launch.report_token_digest is not None):
            return False
        if launch.phase == "post_unknown":
            return not job.cancel_requested
        if launch.phase == "sealed":
            fence_key = native_key("unclaimed-fence", launch.launch_id)
            fence = self.store.read_snapshot((fence_key,))[fence_key]
            return fence is not None and fence.payload["data"].get("failure_code") == SESSION_VERSION_CHANGED
        return False

    def _current(self, rows, launch, *, dispatch=False):
        context = launch.start.binding.owner
        owner = self._row(rows, control_key("owner", context.owner_record_id), OwnerRecord)
        grant = self._row(rows, native_key("grant", launch.grant_id), NativeExecutionGrant)
        policy = self._row(rows, control_key("policy", context.policy_id), PolicyRecord)
        budget = self._row(rows, control_key("budget", context.root_budget_id), BudgetRecord)
        if (
            grant.controller_id != self.controller_id or grant.job_id != launch.start.binding.job_id
            or grant.grant_id != launch.grant_id or grant.input_digest != launch.start.binding.input_digest
            or grant.definition != launch.definition or launch.target not in grant.targets
            or grant.workload_version != launch.workload_version
            or not owner.active
            or any(getattr(owner, name) != getattr(context, name) for name in ExecutionContext.model_fields if name != "epoch")
            or policy.policy_id != context.policy_id
            or budget.root_budget_id != context.root_budget_id
        ):
            raise Problem(403, "NATIVE_AUTHORITY_FENCED", "current owner/grant/policy association differs")
        if dispatch and (
            not grant.active or not policy.active or not budget.active or policy.revision != grant.policy_revision
            or owner.epoch != context.epoch or grant.owner != context
            or grant.not_before>self._now()
            or (grant.expires_at is None and not self.workers.instruction(grant.job_id))
            or grant.expires_at is not None and self._now()>=grant.expires_at
        ):
            raise Problem(403, "NATIVE_AUTHORITY_FENCED", "native dispatch grant is revoked, stale, or expired")
        return grant

    def _keys(self, launch):
        owner = launch.start.binding.owner
        return (
            native_key("launch", launch.launch_id), native_key("grant", launch.grant_id),
            control_key("owner", owner.owner_record_id), control_key("policy", owner.policy_id),
            control_key("budget", owner.root_budget_id),
            worker_key("job", launch.start.binding.job_id),
            self.target_key(launch.target,launch.start.binding.job_id),
        )

    def target_for_owner(self,agent_oid):
        if len(self.targets)!=1 or self.targets[0].agent_oid!=agent_oid:
            raise Problem(403,"NATIVE_CONFIGURATION_INVALID","one C1 target matching the binding owner is required")
        return self.targets[0]

    def targets_for(self,job_id):
        if not self.workers.instruction(job_id):return self.targets
        record=None if self.workers.work_policy is None else self.workers.work_policy._job_record(job_id)
        if record is None:
            raise Problem(403,"NATIVE_JOB_UNASSIGNED","an instruction requires its original registered owner")
        target=self.target_for_owner(record.binding.owner.principal_id)
        return (instruction_target(target,job_id,agent_version=record.profile.instruction.hosted_agent_version,
                                   session_id=record.hosted_session_id),)

    def target_key(self,target,job_id):
        return native_key("target",target.executor_id,*((job_id,) if self.workers.instruction(job_id) else ()))

    def prepare(self, start: ExecutorStart) -> tuple[_NativeLaunch, SecretStr | None]:
        """Internal controller only. A replay returns NO permit and cannot POST."""
        start = _check(start, ExecutorStart)
        self.workers._ready(start.binding.job_id)
        route = self.jobs.get(start.binding.job_id)
        if route is None:
            raise Problem(403, "NATIVE_JOB_UNASSIGNED", "job has no server-configured native route")
        launch_id = native_key("launch-id", start.binding.start_request_id)
        key = native_key("launch", launch_id)
        attempt_key = worker_key("attempt", start.binding.job_id, str(start.binding.generation))
        pool_key = self.workers.pool_key(start.binding.job_id)
        targets = self.targets_for(start.binding.job_id)
        keys = (
            key, attempt_key, pool_key, worker_key("job", start.binding.job_id),
            native_key("grant", route.grant_id), *self.workers._owner_keys(start.binding.owner),
            control_key("budget", start.binding.owner.root_budget_id),
            *(self.target_key(target,start.binding.job_id) for target in targets),
        )
        permit = SecretStr(secrets.token_hex(32))
        post = _json({
            "binding_id": route.api_binding_id, "launch_id": launch_id,
        })
        request = self._request("prepare", (launch_id,), start.model_dump(mode="json"), keys)
        fresh = False

        def decide(rows):
            nonlocal fresh
            self.workers._owner(rows, start.binding.owner)
            current = self.workers._row(rows, worker_key("job", start.binding.job_id), JobRecord)
            if current.binding != start.binding or current.state != "starting" or current.cancel_requested:
                raise Problem(409, "NATIVE_START_FENCED", "original worker start is no longer current")
            attempt = rows[attempt_key]
            if (
                attempt is None or attempt.payload["data"].get("kind") != "worker_attempt"
                or _parse(attempt.payload["data"].get("start"), ExecutorStart) != start
                or _parse(attempt.payload["material"], InlineMaterial) != _material(start.model_dump(mode="json"))
            ):
                raise Problem(403, "NATIVE_INPUT_UNVERIFIED", "only the worker's saved immutable attempt may launch")
            pool = self.workers._pool(rows)
            slots = [slot for slot in pool.slots if slot.binding == start.binding]
            if len(slots) != 1:
                raise Problem(409, "NATIVE_SLOT_FENCED", "one current global capacity reservation is required")
            target = targets[slots[0].number]
            target_key = self.target_key(target,start.binding.job_id)
            reservation = (
                _TargetReservation(target=target, configuration_digest=self.workers.configuration_digest(),
                    job_id=start.binding.job_id if self.workers.instruction(start.binding.job_id) else None) if rows[target_key] is None
                else self._row(rows, target_key, _TargetReservation)
            )
            if reservation.target != target or reservation.active_launch_id is not None:
                raise Problem(409, "NATIVE_TARGET_BUSY", "the predecessor's actual target is not settled")
            grant = self._row(rows, native_key("grant", route.grant_id), NativeExecutionGrant)
            launch = _NativeLaunch(
                launch_id=launch_id, api_binding_id=route.api_binding_id, grant_id=route.grant_id,
                configuration_digest=self.workers.configuration_digest(),
                start=start, definition=grant.definition, target=target,
                process_generation=reservation.generation + 1,
                permit_digest=_secret_digest(permit), post_digest=hashlib.sha256(post).hexdigest(),
                checkpoint_after_bytes=grant.checkpoint_after_bytes if start.checkpoint is None else None,
                workload_version=grant.workload_version,
            )
            self._current(rows, launch, dispatch=True)
            assistant_profile = grant.workload_version == "assistant-v1.2"
            if assistant_profile:
                if self.assistant is None:
                    raise Problem(403, "NATIVE_ASSISTANT_DISABLED", "concrete assistant authority is not registered")
                self.assistant.validate_native_start(start, grant, route.api_binding_id)
            if (
                grant.remaining_uses < 1 or grant.definition.work_id != start.binding.work_id
                or not assistant_profile and not set(grant.definition.required_checks) <= SUPPORTED_CHECKS
                or grant.definition.human_checks or grant.definition.result_kind != "answer"
                or hashlib.sha256(start.input).hexdigest() != grant.input_digest
                or grant.checkpoint_after_bytes is not None and grant.checkpoint_after_bytes > len(start.input)
            ):
                raise Problem(403, "NATIVE_GRANT_DENIED", "constrained checks/input or remaining native grant differs")
            fresh = True
            return ControlChanges(writes=(
                self._write(key, launch),
                self._write(target_key, _update(
                    reservation, generation=launch.process_generation, active_launch_id=launch_id,
                ), rows),
                self._write(native_key("grant", route.grant_id), _update(grant, remaining_uses=grant.remaining_uses - 1), rows),
                ControlWrite(
                    row_id=native_key("post-intent", launch_id), mode="create",
                    data={"request": request.model_dump(mode="json"), "post_digest": launch.post_digest,
                          "target": target.model_dump(mode="json"), "launch_id": launch_id},
                ),
            ), result={"launch_id": launch_id, "actual_environment": "NOT_RUN"})

        self.store.transact(request, decide)
        launch = self.launch(launch_id)
        return launch, permit if fresh else None

    def _agent(self, launch, principal, identity):
        if not isinstance(principal, BehaviorPrincipal):
            raise Problem(403, "NATIVE_EXECUTOR_IDENTITY_DENIED", "a verified assigned agent principal is required")
        principal.require(self.team_id, "consume")
        if principal.kind != "agent" or principal.oid != launch.target.agent_oid or identity != _identity(launch.target):
            raise Problem(403, "NATIVE_EXECUTOR_IDENTITY_DENIED", "agent/native identity differs from the issued assignment")

    def claim(self, binding_id, launch_id, command, principal, *, assistant_conflicts=False):
        """Executor callback; MI alone NEVER consumes a launch."""
        command = _check(command, wire.HostedClaimRequest)
        if assistant_conflicts:
            from .behavior_assistant import _ledger_conflicts, _read_conflicts
        launch = (_read_conflicts(lambda: self.launch(launch_id), phase="hosted_claim")
                  if assistant_conflicts else self.launch(launch_id))
        if assistant_conflicts and (launch.workload_version != "assistant-v1.2" or self.assistant is None):
            raise Problem(403, "NATIVE_ASSISTANT_DISABLED", "bounded claim requires the registered assistant profile")
        self._agent(launch, principal, command.identity)
        permit_digest = _secret_digest(command.launch_permit)
        if (binding_id != launch.api_binding_id or not _valid_invocation_id(command.invocation_id)
                or launch.native_invocation_id is not None and command.invocation_id != launch.native_invocation_id):
            raise Problem(403, "NATIVE_CLAIM_DENIED", "native invocation is outside the assigned job/attempt")
        token = SecretStr(secrets.token_hex(32))
        claim_id = secrets.token_hex(32)
        request = self._request("claim", (launch_id, command.request_id), {
            "launch_id": launch_id, "process_id": command.process_id, "permit_digest": permit_digest,
            "invocation_id": command.invocation_id, "identity": command.identity.model_dump(mode="json"),
        }, self._keys(launch))
        fresh = False

        def decide(rows):
            nonlocal fresh
            current = self._row(rows, native_key("launch", launch_id), _NativeLaunch)
            self._current(rows, current, dispatch=True)
            job = self.workers._row(rows, worker_key("job", current.start.binding.job_id), JobRecord)
            reservation = self._row(rows,self.target_key(current.target,current.start.binding.job_id),_TargetReservation)
            if (
                current.phase != "post_unknown" or current.native_invocation_id is not None
                or not secrets.compare_digest(current.permit_digest, permit_digest)
                or job.cancel_requested or job.binding != current.start.binding
                or reservation.active_launch_id != launch_id or reservation.generation != current.process_generation
            ):
                raise Problem(403, "NATIVE_CLAIM_DENIED", "one-use native launch permit is unavailable")
            current = _update(
                current, phase="claimed", claim_id=claim_id, process_id=command.process_id,
                report_token_digest=_secret_digest(token), native_invocation_id=command.invocation_id,
            )
            fresh = True
            return ControlChanges(writes=(self._write(native_key("launch", launch_id), current, rows),),
                                  result={"claim_id": claim_id})

        if assistant_conflicts:
            _ledger_conflicts(self.store, request, decide)
        else:
            self.store.transact(request, decide)
        def read_current():
            current = self.launch(launch_id)
            self._current(self.store.read_snapshot(self._keys(current)), current, dispatch=True)
            return current
        current = _read_conflicts(read_current, phase="hosted_claim_post_commit") if assistant_conflicts else read_current()
        if (current.process_id != command.process_id or current.permit_digest != permit_digest
                or current.native_invocation_id != command.invocation_id):
            raise Problem(403, "NATIVE_CLAIM_DENIED", "claim replay belongs to another process/permit")
        previous = (
            native_key("launch-id", worker_key("start", current.start.binding.job_id, str(current.start.binding.generation - 1)))
            if current.start.checkpoint is not None else None
        )
        assignment = wire.HostedAssignment(
            launch_id=launch_id, claim_id=current.claim_id, invocation_id=current.native_invocation_id,
            identity=_identity(current.target), process_id=current.process_id,
            process_generation=current.process_generation, binding_digest=_digest(current.start.binding.model_dump(mode="json")),
            input=current.start.input, input_digest=current.start.binding.input_digest,
            checkpoint=current.start.checkpoint, checkpoint_digest=current.start.binding.checkpoint_digest,
            previous_launch_id=previous, checkpoint_after_bytes=current.checkpoint_after_bytes,
            workload_version=current.workload_version,
        )
        return wire.HostedClaimReceipt(assignment=assignment, claim_state="fresh" if fresh else "replay",
                                      report_token=token if fresh else None)

    def probe(self, binding: ExecutorBinding, directive="observe") -> _NativeProbe:
        """Internal controller only; a probe is persisted BEFORE native I/O."""
        launch_id = native_key("launch-id", binding.start_request_id)
        launch = self.launch(launch_id)
        if launch.start.binding != binding or directive not in {"observe", "park", "cancel"}:
            raise Problem(403, "NATIVE_PROBE_DENIED", "original native binding or control differs")
        probe_id = secrets.token_hex(32)
        now = self._now()
        probe = _NativeProbe(
            probe_id=probe_id, launch_id=launch_id, directive=directive,
            control_request_id=native_key("control", launch_id, directive) if directive != "observe" else None,
            issued_at=now, expires_at=now + timedelta(seconds=60),
        )
        request = self._request("probe", (probe_id,), probe.model_dump(mode="json"), self._keys(launch))

        def decide(rows):
            current = self._row(rows, native_key("launch", launch_id), _NativeLaunch)
            self._current(rows, current)
            job = self.workers._row(rows, worker_key("job", binding.job_id), JobRecord)
            if (
                current.probes_reserved >= 32 or job.binding != binding
                or directive == "cancel" and not job.cancel_requested
                or directive == "park" and (not job.children or job.cancel_requested)
            ):
                raise Problem(409, "NATIVE_PROBE_DENIED", "bounded probe/control reservation is unavailable")
            effective = (
                "cancel" if job.cancel_requested or current.pending_control == "cancel" else
                "park" if directive == "park" or current.pending_control == "park" else directive
            )
            issued = _update(
                probe, directive=effective,
                control_request_id=native_key("control", launch_id, effective) if effective != "observe" else None,
            )
            return ControlChanges(writes=(
                self._write(native_key("launch", launch_id), _update(
                    current, probes_reserved=current.probes_reserved + 1, pending_control=effective,
                ), rows),
                self._write(native_key("probe", probe_id), issued, material=_material(issued.model_dump(mode="json"))),
            ), result={"probe": issued.model_dump(mode="json")})

        receipt = self.store.transact(request, decide)
        return _parse(receipt.payload["result"]["probe"], _NativeProbe)

    def _reporter(self, launch, command, principal):
        self._agent(launch, principal, command.identity)
        if (
            launch.claim_id is None or launch.process_id != command.process_id or launch.claim_id != command.claim_id
            or launch.native_invocation_id is None or command.invocation_id != launch.native_invocation_id
            or launch.report_token_digest is None
            or not secrets.compare_digest(launch.report_token_digest, _secret_digest(command.report_token))
        ):
            raise Problem(403, "NATIVE_REPORTER_DENIED", "reporter lacks the original process-bound capability")

    def resolve_probe(self, binding_id, launch_id, probe_id, command, principal):
        command = _check(command, wire.HostedProbeRequest)
        launch = self.launch(launch_id)
        self._reporter(launch, command, principal)
        key = native_key("probe", probe_id)
        probe = self._row(self.store.read_snapshot((key,)), key, _NativeProbe)
        if binding_id != launch.api_binding_id or probe.launch_id != launch_id or not probe.issued_at <= self._now() < probe.expires_at:
            raise Problem(403, "NATIVE_PROBE_DENIED", "fresh assigned probe is unavailable")
        return wire.HostedProbe(
            probe_id=probe_id, launch_id=launch_id, claim_id=launch.claim_id, invocation_id=command.invocation_id,
            identity=_identity(launch.target), process_id=launch.process_id,
            process_generation=launch.process_generation, binding_digest=_digest(launch.start.binding.model_dump(mode="json")),
            directive=probe.directive, control_request_id=probe.control_request_id, expires_at=probe.expires_at,
        )

    def report(self, binding_id, launch_id, probe_id, command, principal):
        command = _check(command, wire.HostedReport)
        launch = self.launch(launch_id)
        self._reporter(launch, command, principal)
        body = command.model_dump(mode="json", exclude={"report_token"})
        fingerprint = _digest({key: value for key, value in body.items() if key not in {"request_id", "probe_id"}})
        probe_key = native_key("probe", probe_id)
        evidence_id = native_key("evidence-id", launch_id, probe_id)
        assistant_plan = None
        no_sdk_context = None
        if launch.workload_version == "assistant-v1.2" and command.phase == "sealed":
            if self.assistant is None:
                raise Problem(403, "NATIVE_ASSISTANT_DISABLED", "assistant verifier is not registered")
            assistant_plan = self.assistant.native_report_plan(launch, command, principal)
        if command.phase == "sdk_not_started":
            if launch.workload_version != "assistant-v1.2" or self.assistant is None:
                raise Problem(403, "SDK_NO_START_SCOPE", "no-SDK disposition requires its enabled assistant verifier")
            no_sdk_context = self.assistant.no_sdk_report_context(launch, command)
        extra_keys = () if assistant_plan is None else tuple(assistant_plan[2])
        if no_sdk_context is not None:
            extra_keys = (*extra_keys, *no_sdk_context[1])
        request = self._request("report", (launch_id, probe_id), body,
                                tuple(dict.fromkeys((*self._keys(launch), probe_key, *extra_keys))))

        def decide(rows):
            current = self._row(rows, native_key("launch", launch_id), _NativeLaunch)
            self._current(rows, current)
            probe = self._row(rows, probe_key, _NativeProbe)
            self._reporter(current, command, principal)
            if (
                binding_id != current.api_binding_id or probe.launch_id != launch_id or command.probe_id != probe_id
                or not probe.issued_at <= self._now() < probe.expires_at
                or command.process_generation != current.process_generation
                or command.binding_digest != _digest(current.start.binding.model_dump(mode="json"))
                or command.supervisor_id != current.process_id or command.sequence < current.last_report_sequence
                or current.child_pid is not None and command.child_pid != current.child_pid
                or command.sequence == current.last_report_sequence
                and current.last_report_fingerprint is not None and current.last_report_fingerprint != fingerprint
            ):
                raise Problem(403, "NATIVE_REPORT_DENIED", "fresh native process/generation/probe binding differs")
            no_sdk = command.phase == "sdk_not_started"
            sealed = command.phase == "sealed" or no_sdk
            # The assistant plan already bound a release to its task's resident worker profile.
            released = assistant_plan is not None and _resident_release(command)
            if command.phase == "sealed" and (not (command.child_exited and command.exit_code is not None or released)
                                               or command.child_pid is None or command.outcome is None):
                raise Problem(403, "NATIVE_IDLE_UNVERIFIED", "owned child exit evidence is missing")
            if current.phase == "sealed":
                raise Problem(409, "NATIVE_ALREADY_SEALED", "reconcile the retained terminal evidence; never execute again")
            state = command.outcome if sealed else "running" if command.phase == "running" else "unknown"
            result = None
            checkpoint = None
            completion_writes = ()
            if no_sdk:
                from .behavior_instruction_no_sdk import changes
                if no_sdk_context is None:
                    raise Problem(403, "SDK_NO_START_SCOPE", "original no-SDK context is required")
                result, completion_writes = changes(
                    self.assistant, no_sdk_context[0], command.sdk_no_start, request, rows)
            elif sealed and current.workload_version == "assistant-v1.2":
                if assistant_plan is None:
                    raise Problem(403, "NATIVE_ASSISTANT_DISABLED", "durable assistant result/checkpoint is required")
                self.assistant.verify_native_report_snapshot(rows, assistant_plan[2])
                result, checkpoint = assistant_plan[:2]
            elif sealed and state == "succeeded":
                expected = {
                    "workload_version": "checksum-count-v1", "input_digest": current.start.binding.input_digest,
                    "byte_count": len(current.start.input), "newline_count": current.start.input.count(b"\n"),
                }
                if command.exit_code != 0 or command.result is None or command.result.model_dump(mode="json") != expected:
                    raise Problem(403, "NATIVE_RESULT_INVALID", "saved checksum/count does not match the original assigned bytes")
                result = VerifiedWorkResult(
                    evidence_id=native_key("result-id", launch_id), verifier_version=self.verifier_version,
                    work_id=current.start.binding.work_id, definition_digest=_digest(current.definition.model_dump(mode="json")),
                    state="succeeded", passed_checks=current.definition.required_checks,
                    content=_json(expected), proof=_json({"native_evidence_id": evidence_id, "report_digest": _digest(body)}),
                )
            elif sealed and state == "parked":
                from .behavior_hosted import ChecksumCheckpoint, NativeCheckpoint

                job = self.workers._row(rows, worker_key("job", current.start.binding.job_id), JobRecord)
                if (
                    command.exit_code != 0 or command.checkpoint is None
                    or not job.children and not job.cancel_requested
                    or probe.directive != "park" and current.checkpoint_after_bytes is None
                    and not job.cancel_requested
                ):
                    raise Problem(403, "NATIVE_CHECKPOINT_INVALID", "parking requires an assigned control and owned exit")
                try:
                    parsed = ChecksumCheckpoint.model_validate_json(command.checkpoint)
                except ValueError:
                    raise Problem(403, "NATIVE_CHECKPOINT_INVALID", "required checkpoint is invalid") from None
                prefix = current.start.input[:parsed.processed_bytes]
                if (
                    parsed.input_digest != current.start.binding.input_digest or parsed.processed_bytes > len(current.start.input)
                    or parsed.prefix_digest != hashlib.sha256(prefix).hexdigest() or parsed.newline_count != prefix.count(b"\n")
                ):
                    raise Problem(403, "NATIVE_CHECKPOINT_INVALID", "checkpoint does not match the assigned original bytes")
                checkpoint = _json(NativeCheckpoint(
                    predecessor_launch_id=launch_id, predecessor_evidence_id=evidence_id,
                    predecessor_binding_digest=_digest(current.start.binding.model_dump(mode="json")),
                    predecessor_identity=_identity(current.target), predecessor_process_generation=current.process_generation,
                    predecessor_process_id=current.process_id, child_pid=command.child_pid,
                    checkpoint=parsed,
                ).model_dump(mode="json"))
                if job.cancel_requested:
                    state, checkpoint = "stopped", None
                    result = VerifiedWorkResult(
                        evidence_id=native_key("result-id", launch_id), verifier_version=self.verifier_version,
                        work_id=current.start.binding.work_id, definition_digest=_digest(current.definition.model_dump(mode="json")),
                        state="cancelled", passed_checks=(),
                        content=b"Constrained execution cancelled after its verified parked checkpoint.",
                        proof=_json({"native_evidence_id": evidence_id, "report_digest": _digest(body)}),
                    )
            elif sealed and state in {"failed", "stopped"}:
                if command.result is not None or command.checkpoint is not None:
                    raise Problem(403, "NATIVE_RESULT_INVALID", "failed/stopped execution cannot claim success material")
                job = self.workers._row(rows, worker_key("job", current.start.binding.job_id), JobRecord)
                if state == "stopped" and not job.cancel_requested:
                    raise Problem(403, "NATIVE_CONTROL_DENIED", "an executor cannot invent cancellation authority")
                result = VerifiedWorkResult(
                    evidence_id=native_key("result-id", launch_id), verifier_version=self.verifier_version,
                    work_id=current.start.binding.work_id, definition_digest=_digest(current.definition.model_dump(mode="json")),
                    state="cancelled" if state == "stopped" else "failed", passed_checks=(),
                    content=b"Constrained checksum execution stopped without a completed result.",
                    proof=_json({"native_evidence_id": evidence_id, "report_digest": _digest(body)}),
                )
            evidence = ExecutorEvidence(
                evidence_id=evidence_id, binding=current.start.binding, sequence=current.evidence_sequence + 1,
                native_execution_id=current.native_invocation_id,
                state=state, entire_tree_idle=sealed, all_inflight_starts_fenced=sealed,
                checkpoint=checkpoint, result=result,
                proof=_json({"launch_id": launch_id, "probe_id": probe_id, "report_digest": _digest(body),
                             "native_identity": _identity(current.target).model_dump(mode="json")}),
            )
            now = self._now()
            saved = _NativeEvidence(
                launch_id=launch_id, probe_id=probe_id, evidence=evidence, observed_at=now,
                configuration_digest=self.workers.configuration_digest(),
                expires_at=now + timedelta(seconds=60), report_digest=_digest(body),
            )
            writes = [
                *completion_writes,
                self._write(native_key("evidence", evidence_id), saved, material=_material(saved.model_dump(mode="json"))),
                ControlWrite(row_id=native_key("report", evidence_id), mode="create",
                             data=body, material=_material(body)),
            ]
            current = _update(
                current, last_report_sequence=command.sequence, evidence_sequence=evidence.sequence,
                child_pid=command.child_pid or current.child_pid,
                last_report_fingerprint=fingerprint,
                **({"phase": "sealed", "sealed_evidence_id": evidence_id, "checkpoint": checkpoint} if sealed else {}),
            )
            if sealed:
                target_key = self.target_key(current.target,current.start.binding.job_id)
                reservation = self._row(rows, target_key, _TargetReservation)
                if reservation.active_launch_id != launch_id or reservation.generation != current.process_generation:
                    raise Problem(403, "NATIVE_PREDECESSOR_UNSETTLED", "the native target belongs to another execution")
                writes.append(self._write(target_key, _update(reservation, active_launch_id=None), rows))
            writes.append(self._write(native_key("launch", launch_id), current, rows))
            return ControlChanges(writes=tuple(writes), result={"launch_id": launch_id, "probe_id": probe_id,
                                                               "evidence_id": evidence_id, "state": state})

        receipt = self.store.transact(request, decide)
        return wire.HostedEvidenceReceipt(**receipt.payload["result"])

    def _verify_unclaimed_fence(self, launch, saved, fence):
        expected = {
            "kind": "terminal_unclaimed_launch_fence", "launch_id": launch.launch_id,
            "original_start_request_id": launch.start.binding.start_request_id, "cancel_requested": True,
            "claim_never_issued": True, "no_provider_stop_claim": True,
        }
        failed = fence is not None and "failure_code" in fence.payload["data"]
        result = None
        if failed:
            observed = fence.payload["data"].get("observed_agent_version")
            if (fence.payload["data"].get("failure_code") != SESSION_VERSION_CHANGED
                    or not isinstance(observed, str) or re.fullmatch(r"[1-9][0-9]{0,15}", observed) is None
                    or observed == launch.target.agent_version or saved.evidence.result is None
                    or self is not None and saved.evidence.result.verifier_version != self.verifier_version):
                raise Problem(403, "NATIVE_EVIDENCE_INVALID", "version refusal lacks its exact original observation")
            expected.update(cancel_requested=False, failure_code=SESSION_VERSION_CHANGED,
                            observed_agent_version=observed)
            result = _version_refusal_result(launch, expected, saved.evidence.result.verifier_version)
        if (launch.phase != "sealed" or launch.native_invocation_id is not None
                or launch.claim_id is not None or launch.process_id is not None
                or launch.report_token_digest is not None or launch.child_pid is not None
                or launch.checkpoint is not None
                or launch.sealed_evidence_id != native_key("unclaimed-fence-evidence", launch.launch_id)
                or fence is None or fence.payload["data"] != expected
                or _parse(fence.payload["material"], InlineMaterial) != _material(expected)
                or saved.launch_id != launch.launch_id
                or saved.probe_id != native_key("unclaimed-fence", launch.launch_id)
                or saved.report_digest != _digest(expected) or saved.evidence.proof != _json(expected)
                or saved.evidence.state != ("failed" if failed else "stopped")
                or saved.evidence.binding != launch.start.binding
                or saved.evidence.native_execution_id != launch.start.binding.start_request_id
                or not saved.evidence.entire_tree_idle or not saved.evidence.all_inflight_starts_fenced
                or saved.evidence.result != result or saved.evidence.checkpoint is not None):
            raise Problem(403, "NATIVE_EVIDENCE_INVALID", "permanent unclaimed gate-fence proof is inconsistent")

    def _verify_lost_session(self, launch, saved):
        """The retained proof names this exact claimed launch and a session status whose compute had stopped."""
        try:
            proof = json.loads(saved.evidence.proof)
        except ValueError:
            proof = None
        cancelled = isinstance(proof, dict) and proof.get("cancel_requested") is True
        expected = None if not isinstance(proof, dict) else {
            "kind": "terminal_lost_session", "launch_id": launch.launch_id,
            "original_start_request_id": launch.start.binding.start_request_id,
            "native_invocation_id": launch.native_invocation_id, "session_id": launch.target.session_id,
            "agent_version": launch.target.agent_version, "session_status": proof.get("session_status"),
            "stop_requested": proof.get("stop_requested"), "cancel_requested": cancelled,
            "failure_code": SESSION_LOST, "no_provider_effect_claim": True,
        }
        result = None if cancelled or expected is None or saved.evidence.result is None else _lost_session_result(
            launch, expected, saved.evidence.result.verifier_version)
        if (expected is None or proof != expected or not _proven_stop(proof["session_status"], proof["stop_requested"])
                or self is not None and result is not None and result.verifier_version != self.verifier_version
                or launch.phase != "sealed" or launch.native_invocation_id is None or launch.claim_id is None
                or launch.sealed_evidence_id != native_key("lost-session-evidence", launch.launch_id)
                or saved.launch_id != launch.launch_id or saved.probe_id != native_key("lost-session", launch.launch_id)
                or saved.report_digest != _digest(expected) or saved.evidence.proof != _json(expected)
                or saved.evidence.state != ("stopped" if cancelled else "failed")
                or saved.evidence.binding != launch.start.binding
                or saved.evidence.native_execution_id != launch.native_invocation_id
                or not saved.evidence.entire_tree_idle or not saved.evidence.all_inflight_starts_fenced
                or saved.evidence.result != result or cancelled != (result is None)
                or saved.evidence.checkpoint is not None):
            raise Problem(403, "NATIVE_EVIDENCE_INVALID", "lost-session proof is inconsistent")

    def seal_lost_session(self, binding: ExecutorBinding, *, session_status: str, stop_requested: bool) -> str:
        """Close a claimed instruction attempt whose session compute is proven stopped.

        The caller has just read the exact original session with a non-running status, or (after the
        human's cancel) FA's own session stop succeeded. Nothing the attempt saved is changed or resent:
        model reservations stay pending, and a saved result or archive refuses this closure so it cannot
        be replaced by a failure.
        """
        from .behavior_assistant import key as assistant_key
        from .behavior_sdk_checkpoint import checkpoint_key
        if not _proven_stop(session_status, stop_requested):
            raise Problem(422, "NATIVE_LOST_SESSION_INVALID", "a proven stopped session status is required")
        launch_id = native_key("launch-id", binding.start_request_id)
        launch = self.launch(launch_id)
        evidence_id = native_key("lost-session-evidence", launch_id)
        retained = (assistant_key("result", binding.work_id), assistant_key("text-archive", binding.job_id),
                    assistant_key("reply", binding.job_id), checkpoint_key(binding.job_id))
        request = self._request("seal-lost-session", (launch_id,),
                                [binding.model_dump(mode="json"), session_status, stop_requested],
                                (*self._keys(launch), *retained))

        def decide(rows):
            current = self._row(rows, native_key("launch", launch_id), _NativeLaunch)
            self._current(rows, current)
            job = self.workers._row(rows, worker_key("job", binding.job_id), JobRecord)
            target_key = self.target_key(current.target, current.start.binding.job_id)
            reservation = self._row(rows, target_key, _TargetReservation)
            if (current.start.binding != binding or current.phase != "claimed"
                    or current.native_invocation_id is None or current.claim_id is None
                    or not self.workers.instruction(binding.job_id)
                    or job.binding != binding or job.state not in {"starting", "running", "capacity_uncertain"}
                    or reservation.active_launch_id != launch_id or reservation.generation != current.process_generation
                    or any(rows[key] is not None for key in retained)):
                raise Problem(409, "NATIVE_LOST_SESSION_UNRESOLVED",
                              "only an unfinished claimed instruction attempt without a saved result can close")
            cancelled = job.cancel_requested
            proof = {
                "kind": "terminal_lost_session", "launch_id": launch_id,
                "original_start_request_id": binding.start_request_id,
                "native_invocation_id": current.native_invocation_id, "session_id": current.target.session_id,
                "agent_version": current.target.agent_version, "session_status": session_status,
                "stop_requested": stop_requested, "cancel_requested": cancelled,
                "failure_code": SESSION_LOST, "no_provider_effect_claim": True,
            }
            now = self._now()
            evidence = ExecutorEvidence(
                evidence_id=evidence_id, binding=binding, sequence=current.evidence_sequence + 1,
                native_execution_id=current.native_invocation_id, state="stopped" if cancelled else "failed",
                entire_tree_idle=True, all_inflight_starts_fenced=True,
                result=None if cancelled else _lost_session_result(current, proof, self.verifier_version),
                proof=_json(proof),
            )
            saved = _NativeEvidence(
                launch_id=launch_id, configuration_digest=self.workers.configuration_digest(),
                probe_id=native_key("lost-session", launch_id), evidence=evidence, observed_at=now,
                expires_at=now + timedelta(seconds=60), report_digest=_digest(proof),
            )
            return ControlChanges(writes=(
                self._write(native_key("launch", launch_id), _update(
                    current, phase="sealed", sealed_evidence_id=evidence_id, evidence_sequence=evidence.sequence,
                ), rows),
                self._write(target_key, _update(reservation, active_launch_id=None), rows),
                self._write(native_key("evidence", evidence_id), saved, material=_material(saved.model_dump(mode="json"))),
            ), result={"evidence_id": evidence_id})

        return self.store.transact(request, decide).payload["result"]["evidence_id"]

    def verify(self, evidence_id: str) -> ExecutorEvidence:
        key = native_key("evidence", evidence_id)
        rows = self.store.read_snapshot((key,))
        saved = self._row(rows, key, _NativeEvidence)
        launch = self.launch(saved.launch_id)
        if launch.native_invocation_id is None:
            fence_key = native_key("unclaimed-fence", launch.launch_id)
            original_key = native_key("evidence", native_key("unclaimed-fence-evidence", launch.launch_id))
            retained = self.store.read_snapshot((fence_key, original_key))
            original = self._row(retained, original_key, _NativeEvidence)
            self._verify_unclaimed_fence(launch, original, retained[fence_key])
            self._verify_unclaimed_fence(launch, saved, retained[fence_key])
            if (original.evidence.evidence_id != launch.sealed_evidence_id
                    or _parse(retained[original_key].payload["material"], InlineMaterial) != _material(original.model_dump(mode="json"))
                    or saved.evidence.sequence > launch.evidence_sequence
                    or evidence_id != launch.sealed_evidence_id and (
                        saved.evidence.sequence <= original.evidence.sequence
                        or evidence_id != native_key("terminal-verification-id", launch.launch_id, str(saved.evidence.sequence))
                    )):
                raise Problem(403, "NATIVE_EVIDENCE_INVALID", "unclaimed fence revalidation differs from its immutable original")
        elif saved.evidence.native_execution_id != launch.native_invocation_id:
            raise Problem(403, "NATIVE_EVIDENCE_INVALID", "executor evidence belongs to another actual native invocation")
        elif launch.sealed_evidence_id == native_key("lost-session-evidence", launch.launch_id):
            self._verify_lost_session(launch, saved)
        elif launch.sealed_evidence_id == native_key("original-stop-close-evidence", launch.launch_id):
            # No deployed producer wrote this closure (#1051); its reader is gone, so never fall through to accept it.
            raise Problem(403, "NATIVE_EVIDENCE_INVALID", "original stop closures are retired and cannot be verified")
        elif launch.sealed_evidence_id == native_key("platform-stop-evidence", launch.launch_id):
            from .behavior_recovery import verify_platform_stop
            proof_key = native_key("platform-stop", launch.launch_id)
            proof = self.store.read_snapshot((proof_key,))[proof_key]
            if proof is None:
                raise Problem(403, "NATIVE_EVIDENCE_INVALID", "original platform-stop provenance is missing")
            verify_platform_stop(self, launch, saved, proof)
        if (
            saved.evidence.evidence_id != evidence_id or saved.evidence.binding != launch.start.binding
            or not saved.observed_at <= self._now() < saved.expires_at
            or _parse(rows[key].payload["material"], InlineMaterial) != _material(saved.model_dump(mode="json"))
            or saved.evidence.entire_tree_idle and launch.phase != "sealed"
        ):
            raise Problem(403, "NATIVE_EVIDENCE_INVALID", "fresh protected native execution evidence is unavailable")
        return saved.evidence

    def fence_unclaimed(self, binding: ExecutorBinding, *, observed_agent_version: str | None = None) -> str:
        """Fence cancellation or a version refusal before any child can start.

        This proves rejection at our controlled effect point, not a provider
        stop acknowledgment. If claim won, this path refuses to free the slot.
        """
        launch_id = native_key("launch-id", binding.start_request_id)
        launch = self.launch(launch_id)
        evidence_id = native_key("unclaimed-fence-evidence", launch_id)
        fence_key = native_key("unclaimed-fence", launch_id)
        failed = observed_agent_version is not None
        owner_key = control_key("owner", binding.owner.owner_record_id)
        operation_ids = ()
        absent = ()
        request_body = binding.model_dump(mode="json")
        if failed:
            if (not isinstance(observed_agent_version, str)
                    or re.fullmatch(r"[1-9][0-9]{0,15}", observed_agent_version) is None
                    or observed_agent_version == launch.target.agent_version):
                raise Problem(409, "NATIVE_VERSION_REFUSAL_INVALID", "a different concrete version is required")
            owner = self._row(self.store.read_snapshot((owner_key,)), owner_key, OwnerRecord)
            operation_ids = owner.operation_ids
            absent = _unused_assignment_keys(binding, operation_ids)
            request_body = [request_body, observed_agent_version]
        request = self._request("fence-unclaimed", (launch_id,), request_body,
                                (*self._keys(launch), fence_key, *absent))

        def decide(rows):
            current = self._row(rows, native_key("launch", launch_id), _NativeLaunch)
            self._current(rows, current)
            job = self.workers._row(rows, worker_key("job", binding.job_id), JobRecord)
            target_key = self.target_key(current.target,current.start.binding.job_id)
            reservation = self._row(rows, target_key, _TargetReservation)
            if (current.start.binding != binding or current.phase != "post_unknown"
                    or current.native_invocation_id is not None
                    or current.claim_id is not None or current.process_id is not None
                    or current.report_token_digest is not None or current.child_pid is not None
                    or job.binding != binding or job.cancel_requested != (not failed)
                    or reservation.active_launch_id != launch_id):
                raise Problem(409, "NATIVE_CLAIM_ALREADY_OWNED", "cannot fence an executor that may have acquired its launch")
            if failed and (job.state not in {"starting", "capacity_uncertain"} or job.generation != 1
                           or job.children or job.checkpoint is not None
                           or self._row(rows, owner_key, OwnerRecord).operation_ids != operation_ids
                           or any(rows[key] is not None for key in absent)):
                raise Problem(409, "NATIVE_VERSION_REFUSAL_UNRESOLVED",
                              "original assignment, operation or result activity prevents a pre-SDK refusal")
            now = self._now()
            proof = {"kind": "terminal_unclaimed_launch_fence", "launch_id": launch_id,
                     "original_start_request_id": binding.start_request_id, "cancel_requested": not failed,
                     "claim_never_issued": True, "no_provider_stop_claim": True}
            if failed:
                proof.update(failure_code=SESSION_VERSION_CHANGED, observed_agent_version=observed_agent_version)
            evidence = ExecutorEvidence(
                evidence_id=evidence_id, binding=binding, sequence=current.evidence_sequence + 1,
                native_execution_id=binding.start_request_id, state="failed" if failed else "stopped",
                entire_tree_idle=True, all_inflight_starts_fenced=True,
                result=_version_refusal_result(current, proof, self.verifier_version) if failed else None,
                proof=_json(proof),
            )
            saved = _NativeEvidence(
                launch_id=launch_id, configuration_digest=self.workers.configuration_digest(),
                probe_id=fence_key, evidence=evidence, observed_at=now, expires_at=now + timedelta(seconds=60),
                report_digest=_digest(proof),
            )
            return ControlChanges(writes=(
                self._write(native_key("launch", launch_id), _update(
                    current, phase="sealed", sealed_evidence_id=evidence_id, evidence_sequence=evidence.sequence,
                ), rows),
                self._write(target_key, _update(reservation, active_launch_id=None), rows),
                ControlWrite(row_id=fence_key, mode="create", data=proof, material=_material(proof)),
                self._write(native_key("evidence", evidence_id), saved, material=_material(saved.model_dump(mode="json"))),
            ), result={"evidence_id": evidence_id})

        return self.store.transact(request, decide).payload["result"]["evidence_id"]

    def reconcile_sealed(self, binding: ExecutorBinding) -> str:
        """Fresh verification of retained positive terminal proof and permanent fence.

        This is not a lease/clock inference. A claimed launch requires its
        original owned-exit report. An unclaimed launch instead requires the
        immutable terminal claim-gate fence; it asserts no provider/child stop.
        """
        launch_id = native_key("launch-id", binding.start_request_id)
        launch = self.launch(launch_id)
        if launch.start.binding != binding or launch.phase != "sealed" or launch.sealed_evidence_id is None:
            raise Problem(409, "NATIVE_TERMINAL_UNKNOWN", "no verified permanently fenced native completion exists")
        original_key = native_key("evidence", launch.sealed_evidence_id)
        unclaimed = launch.native_invocation_id is None
        platform_stopped = launch.sealed_evidence_id == native_key("platform-stop-evidence", launch_id)
        if launch.sealed_evidence_id == native_key("original-stop-close-evidence", launch_id):
            # No deployed producer wrote this closure (#1051); its reader is gone, so never reconcile it as terminal.
            raise Problem(403, "NATIVE_TERMINAL_INVALID", "original stop closures are retired and cannot be reconciled")
        lost = launch.sealed_evidence_id == native_key("lost-session-evidence", launch_id)
        proof_key = (native_key("unclaimed-fence", launch_id) if unclaimed
                     else native_key("platform-stop", launch_id) if platform_stopped
                     else original_key if lost
                     else native_key("report", launch.sealed_evidence_id))
        sequence = launch.evidence_sequence + 1
        evidence_id = native_key("terminal-verification-id", launch_id, str(sequence))
        request = self._request(
            "reconcile-sealed", (launch_id, str(sequence)), [binding.model_dump(mode="json"), launch.sealed_evidence_id],
            (*self._keys(launch), original_key, proof_key),
        )

        def decide(rows):
            current = self._row(rows, native_key("launch", launch_id), _NativeLaunch)
            self._current(rows, current)
            original = self._row(rows, original_key, _NativeEvidence)
            if (
                current.phase != "sealed" or current.sealed_evidence_id != launch.sealed_evidence_id
                or current.evidence_sequence + 1 != sequence or original.evidence.binding != binding
                or original.evidence.evidence_id != current.sealed_evidence_id
                or not original.evidence.entire_tree_idle or not original.evidence.all_inflight_starts_fenced
                or _parse(rows[original_key].payload["material"], InlineMaterial) != _material(original.model_dump(mode="json"))
            ):
                raise Problem(403, "NATIVE_TERMINAL_INVALID", "retained terminal evidence or generation is inconsistent")
            if unclaimed:
                self._verify_unclaimed_fence(current, original, rows[proof_key])
            elif lost:
                self._verify_lost_session(current, original)
            elif platform_stopped:
                from .behavior_recovery import verify_platform_stop
                if rows[proof_key] is None:
                    raise Problem(403, "NATIVE_TERMINAL_INVALID", "platform stop provenance is missing")
                verify_platform_stop(self, current, original, rows[proof_key])
            else:
                report = self._row(rows, proof_key, wire.HostedReportBody)
                no_sdk = report.phase == "sdk_not_started"
                owned_exit = report.phase == "sealed" and report.child_exited and report.child_pid == current.child_pid
                if (not owned_exit and report.phase == "sealed" and _resident_release(report)
                        and report.child_pid == current.child_pid and current.workload_version == "assistant-v1.2"):
                    from .behavior_assistant_wire import Task
                    released_task = Task.model_validate_json(current.start.input).instruction
                    owned_exit = released_task is not None and released_task.worker_profile == report.sdk_stop.release
                if no_sdk:
                    from .behavior_assistant_wire import Task
                    task = Task.model_validate_json(current.start.input)
                    gate = report.sdk_no_start.gate
                    no_sdk = (
                        current.workload_version == "assistant-v1.2" and current.child_pid is None
                        and task.instruction is not None and task.instruction.sdk_start_profile == gate.binding.profile
                        and gate.binding.task_digest == _digest(task.model_dump(mode="json"))
                        and gate.binding.profile_digest == task.profile_digest
                        and gate.binding.execution_digest == _digest(current.start.binding.model_dump(mode="json"))
                        and gate.binding.worker_configuration_digest == current.configuration_digest
                        and gate.owner.kind == "hosted" and gate.owner.launch_id == current.launch_id
                        and gate.owner.claim_id == current.claim_id and gate.owner.supervisor_id == current.process_id
                        and gate.owner.process_generation == current.process_generation
                        and original.evidence.state == "failed"
                    )
                if (current.native_invocation_id is None or report.invocation_id != current.native_invocation_id
                        or original.evidence.native_execution_id != current.native_invocation_id
                        or not (owned_exit or no_sdk)
                        or report.process_id != current.process_id or report.process_generation != current.process_generation
                        or original.report_digest != _digest(report.model_dump(mode="json"))
                        or _parse(rows[proof_key].payload["material"], InlineMaterial) != _material(report.model_dump(mode="json"))):
                    raise Problem(403, "NATIVE_TERMINAL_INVALID", "retained native exit/claim fence is inconsistent")
            now = self._now()
            evidence = _update(original.evidence, evidence_id=evidence_id, sequence=sequence)
            saved = _update(original, evidence=evidence, observed_at=now, expires_at=now + timedelta(seconds=60))
            return ControlChanges(writes=(
                self._write(native_key("launch", launch_id), _update(current, evidence_sequence=sequence), rows),
                self._write(native_key("evidence", evidence_id), saved, material=_material(saved.model_dump(mode="json"))),
            ), result={"evidence_id": evidence_id})

        receipt = self.store.transact(request, decide)
        return receipt.payload["result"]["evidence_id"]


class FoundryExecutor:
    """Concrete HTTP executor; controller methods are not agent API routes."""

    def __init__(self, authority: NativeLaunchAuthority, clients: tuple[FoundryHttpClient, ...]):
        self.authority = authority
        self.clients = {client.target.executor_id: client for client in clients}
        if len(self.clients) != len(authority.targets) or any(
            target.executor_id not in self.clients or self.clients[target.executor_id].target != target
            for target in authority.targets
        ):
            raise Problem(403, "NATIVE_CONFIGURATION_INVALID", "each configured executor needs its exact pinned transport")

    def adapter(self, *, verify_readiness=None):
        def verified_ready(evidence_id):
            self._configured()
            return verify_readiness(evidence_id)

        return ExecutorAdapter(
            self.start, self.query, self.stop, self.authority.verify,
            verified_ready if verify_readiness is not None else None,
            stopped_session=self.stopped_session,
        )

    def stopped_session(self, binding):
        """Read-only check for the observer: whether the next step can close the claimed instruction attempt."""
        launch = self.authority.launch(native_key("launch-id", binding.start_request_id))
        if (launch.start.binding != binding or launch.phase != "claimed"
                or not self.authority.workers.instruction(binding.job_id)):
            return None
        original = self.clients[launch.target.executor_id]
        client = original if original.target == launch.target else original.for_target(launch.target)
        status = self._session_state(client)
        if status in TERMINAL_SESSION_STATUSES:
            return status
        cancelled = self.authority.workers.inspect(binding.job_id).cancel_requested
        # After the human's cancel the next step stops the session itself unless it still reads as running.
        return (status or "unreadable") if cancelled and status not in RUNNING_SESSION_STATUSES else None

    def _configured(self):
        if any(client.credential is None for client in self.clients.values()):
            raise Problem(403, "NATIVE_CREDENTIAL_DISABLED", "all exact native executor transports require server credentials")

    def _cleanup_instruction_session(self,launch,*,retire=False):
        if not self.authority.workers.instruction(launch.start.binding.job_id):return
        if launch.native_invocation_id is None:
            fence_key = native_key("unclaimed-fence", launch.launch_id)
            fence = self.authority.store.read_snapshot((fence_key,))[fence_key]
            if fence is not None and fence.payload["data"].get("failure_code") == SESSION_VERSION_CHANGED:
                return
        record=self.authority.workers.work_policy._job_record(launch.start.binding.job_id)
        if record.hosted_session_id is not None and not retire:return
        import logging
        store=self.authority.store
        key=native_key("instruction-session-stop",launch.launch_id)
        value=InstructionSessionStop(launch_id=launch.launch_id,job_id=launch.start.binding.job_id,target=launch.target)
        request=ControlRequest(request_id=key,request_digest=_digest(value.model_dump(mode="json")),
                               read_set=(native_key("launch",launch.launch_id),key))
        fresh=False
        def decide(rows):
            nonlocal fresh
            current=self.authority._row(rows,native_key("launch",launch.launch_id),_NativeLaunch)
            if current.phase!="sealed" or current.target!=value.target or current.start.binding!=launch.start.binding:
                raise Problem(409,"NATIVE_SESSION_CLEANUP_FENCED","only this completed original work's session may stop")
            fresh=True
            return ControlChanges(writes=(self.authority._write(key,value),),result={"reserved":True})
        store.transact(request,decide)
        if not fresh:return
        original=self.clients[launch.target.executor_id]
        client=original if original.target==launch.target else original.for_target(launch.target)
        try:client.stop_session()
        except Problem:
            logging.getLogger(__name__).warning("INSTRUCTION_SESSION_STOP_UNKNOWN original_intent_retained=true")
            return
        confirm=ControlRequest(request_id=native_key("instruction-session-stopped",launch.launch_id),
            request_digest=_digest(value.model_dump(mode="json")),read_set=(key,))
        def completed(rows):
            if self.authority._row(rows,key,InstructionSessionStop)!=value:
                raise Problem(409,"NATIVE_SESSION_CLEANUP_FENCED","original session cleanup changed")
            return ControlChanges(writes=(self.authority._write(key,_update(value,confirmed=True),rows),),
                                  result={"stopped":True,"deleted":False})
        store.transact(confirm,completed)

    def start(self, command: ExecutorStart):
        phase = "configure"
        launch = None
        try:
            self._configured()
            phase = "prepare"
            from .behavior_timing import phase_timing
            with phase_timing("native_prepare",job_id=command.binding.job_id):
                launch, permit = self.authority.prepare(command)
            if permit is None:
                phase = "reconcile"
                return self.query(command.binding)
            original=self.clients[launch.target.executor_id]
            client=original if original.target==launch.target else original.for_target(launch.target)
            def current():
                if self.authority.workers.work_policy is not None:
                    self.authority.workers.work_policy.check_dispatch(command.binding.job_id,command.binding)
            if self.authority.workers.instruction(command.binding.job_id):
                phase = "session"
                record=self.authority.workers.work_policy._job_record(command.binding.job_id)
                if record.hosted_session_id is not None and record.profile.instruction.turn_number>1:
                    with phase_timing("hosted_verify",job_id=command.binding.job_id):
                        client.verify_session(before_send=current)
                else:
                    with phase_timing("hosted_create",job_id=command.binding.job_id):
                        client.create_session(before_send=current)
            phase = "invocation"
            with phase_timing("hosted_invocation",job_id=command.binding.job_id):
                response = client.start(
                    command.binding.start_request_id, launch.launch_id, launch.api_binding_id, permit,
                    **({} if getattr(self.authority.workers,"work_policy",None) is None else {"before_send":
                        lambda:self.authority.workers.work_policy.check_dispatch(command.binding.job_id,command.binding)}),
                )
            if self.authority.workers.instruction(command.binding.job_id):
                phase = "session_after_invocation"
                client.verify_session(before_send=current)
            phase = "verify_response"
            current = self.authority.launch(launch.launch_id)
            if current.native_invocation_id is not None and current.native_invocation_id != response.invocation_id:
                raise Problem(403, "NATIVE_RESPONSE_INVALID", "native POST response differs from its durable permit claim")
            # Native status/202 is not completion. A fresh authenticated probe follows.
            return None
        except Exception as error:
            from .behavior_http_diagnostics import _exception_type
            code = error.body["error"].get("code") if isinstance(error, Problem) else None
            logging.getLogger(__name__).warning(
                "FOUNDRY_START_UNRESOLVED phase=%s code=%s status=%s exception_type=%s",
                phase, code if type(code) is str and code in _START_CODES else "UNCLASSIFIED",
                _upstream_status(error), _exception_type(error),
            )
            if (launch is not None and phase in {"session", "session_after_invocation"}
                    and code == SESSION_VERSION_CHANGED
                    and error.body["error"].get("expected_agent_version") == launch.target.agent_version
                    and isinstance(error.body["error"].get("observed_agent_version"), str)):
                return self.authority.fence_unclaimed(
                    command.binding, observed_agent_version=error.body["error"]["observed_agent_version"])
            if (launch is not None and phase == "invocation"
                    and self.authority.workers.instruction(command.binding.job_id)):
                evidence_id = self._version_refusal(launch)
                if evidence_id is not None:
                    return evidence_id
            raise

    def _version_refusal(self, launch):
        current = self.authority.launch(launch.launch_id)
        if current.phase != "post_unknown" or current.native_invocation_id is not None:
            return None
        original = self.clients[current.target.executor_id]
        client = original if original.target == current.target else original.for_target(current.target)
        try:
            client.verify_session(before_send=lambda: self.authority._current(
                self.authority.store.read_snapshot(self.authority._keys(current)), current))
        except Problem as error:
            details = error.body["error"]
            if (details.get("code") == SESSION_VERSION_CHANGED
                    and details.get("expected_agent_version") == current.target.agent_version
                    and isinstance(details.get("observed_agent_version"), str)):
                return self.authority.fence_unclaimed(
                    current.start.binding, observed_agent_version=details["observed_agent_version"])
            raise
        return None

    def _observe(self, binding, directive):
        launch = self.authority.launch(native_key("launch-id", binding.start_request_id))
        if launch.start.binding != binding:
            raise Problem(403, "NATIVE_RESPONSE_INVALID", "native launch belongs to another logical attempt")
        if launch.phase == "sealed":
            self._cleanup_instruction_session(launch)
            return self.authority.reconcile_sealed(binding)
        if launch.native_invocation_id is None:
            if directive == "observe" and self.authority.workers.instruction(binding.job_id):
                evidence_id = self._version_refusal(launch)
                if evidence_id is not None:
                    return evidence_id
            raise Problem(503, "NATIVE_INVOCATION_UNKNOWN", "native invocation is not durably claimed; do not resend or infer its ID")
        original=self.clients[launch.target.executor_id]
        client=original if original.target==launch.target else original.for_target(launch.target)
        instruction = launch.phase == "claimed" and self.authority.workers.instruction(binding.job_id)
        cancelled = directive == "cancel" or instruction and self.authority.workers.inspect(binding.job_id).cancel_requested
        # Read the session before any invocation: an invocation would wake a suspended session into new compute.
        status = self._session_state(client) if instruction else None
        if status in TERMINAL_SESSION_STATUSES:
            return self._seal_lost(binding, status, stop_requested=False)
        if status == "idle":
            # "idle" alone is not proof; after the human's cancel, FA stops the session itself and then reads it.
            if cancelled:
                return self._stop_and_seal(binding, client)
            raise Problem(503, "NATIVE_SESSION_IDLE_UNRESOLVED",
                          "the original session is suspended; its outcome stays unknown and it is not woken")
        probe = self.authority.probe(binding, directive)
        try:
            response = (
                client.cancel(launch.native_invocation_id, probe.probe_id) if directive == "cancel"
                else client.observe(launch.native_invocation_id, probe.probe_id)
            )
        except Exception:
            if not (instruction and cancelled):
                raise
            # The human cancelled and the Hosted process does not answer (also on a later observe, once the
            # cancel's own stop reservation was used): stopping the session ends its compute, so the attempt
            # can close instead of staying unknown.
            return self._stop_and_seal(binding, client)
        try:
            receipt = wire.HostedEvidenceReceipt.model_validate_json(response.body)
        except ValueError:
            raise Problem(502, "NATIVE_RESPONSE_INVALID", "native control response is not a verified evidence receipt") from None
        if receipt.launch_id != launch.launch_id or receipt.probe_id != probe.probe_id:
            raise Problem(403, "NATIVE_RESPONSE_INVALID", "native receipt belongs to another original attempt/probe")
        evidence = self.authority.verify(receipt.evidence_id)
        if (evidence.binding != binding or evidence.state != receipt.state
                or evidence.native_execution_id != launch.native_invocation_id):
            raise Problem(403, "NATIVE_RESPONSE_INVALID", "native receipt and authority evidence differ")
        if self.authority.launch(launch.launch_id).phase=="sealed":
            self._cleanup_instruction_session(self.authority.launch(launch.launch_id))
        return receipt.evidence_id

    def _session_state(self, client):
        """The exact original session's official status, lowercased; None when unreadable or not the same session."""
        read = getattr(client, "session_status", None)
        if not callable(read):
            return None
        try:
            session = read()
        except Exception as error:
            from .behavior_http_diagnostics import _exception_type
            logging.getLogger(__name__).warning(
                "INSTRUCTION_SESSION_STATUS_UNREADABLE session_id=%s code=%s upstream_status=%s",
                client.target.session_id,
                error.body["error"]["code"] if isinstance(error, Problem) else _exception_type(error),
                _upstream_status(error))
            return None
        status = getattr(session.status, "value", session.status)
        indicator = getattr(session, "version_indicator", None)
        if (session.agent_session_id != client.target.session_id
                or getattr(indicator, "agent_version", None) != client.target.agent_version
                or not isinstance(status, str)):
            logging.getLogger(__name__).warning(
                "INSTRUCTION_SESSION_STATUS_UNREADABLE session_id=%s code=SESSION_IDENTITY_DIFFERS upstream_status=None",
                client.target.session_id)
            return None
        return status.lower()

    def _stop_and_seal(self, binding, client):
        """After the human's cancel: stop this conversation's own session once, then close unless it still runs."""
        if not callable(getattr(client, "session_status", None)):
            # Without a status read a session still running after the stop could not be told apart.
            raise Problem(503, "NATIVE_CANCEL_UNKNOWN", "original Hosted cancel is unconfirmed")
        try:
            client.stop_session()
        except Exception:
            logging.getLogger(__name__).warning("INSTRUCTION_LOST_SESSION_STOP_UNKNOWN original_intent_retained=true")
            raise Problem(503, "NATIVE_CANCEL_UNKNOWN", "original Hosted cancel and session stop are unconfirmed") from None
        # The stop itself succeeded (the platform documents it as terminating compute, also when already
        # stopped); only a read showing the session still running contradicts it.
        status = self._session_state(client)
        if status in RUNNING_SESSION_STATUSES:
            logging.getLogger(__name__).warning("INSTRUCTION_LOST_SESSION_STILL_RUNNING status=%s", status)
            raise Problem(503, "NATIVE_CANCEL_UNKNOWN", "the session still reads as running after the stop")
        return self._seal_lost(binding, status if status in LOST_SESSION_STATUSES else STOP_ACCEPTED,
                               stop_requested=True)

    def _seal_lost(self, binding, status, *, stop_requested):
        logging.getLogger(__name__).warning("INSTRUCTION_SESSION_LOST job_id=%s status=%s stop_requested=%s",
                                            binding.job_id, status, str(stop_requested).lower())
        try:
            self.authority.seal_lost_session(binding, session_status=status, stop_requested=stop_requested)
        except Problem as error:
            logging.getLogger(__name__).warning("INSTRUCTION_SESSION_LOST_UNSEALED job_id=%s code=%s",
                                                binding.job_id, error.body["error"]["code"])
            raise
        return self.authority.reconcile_sealed(binding)

    def query(self, binding: ExecutorBinding):
        return self._observe(binding, "observe")

    def stop(self, command: ExecutorStop):
        return self._observe(command.binding, "cancel")

    def park(self, binding: ExecutorBinding):
        """Server-internal dependency control, not a generic-agent route."""
        return self._observe(binding, "park")

    def adopt(self, job_id: str, context: ExecutionContext):
        """Server-internal epoch transfer after positively verified native park."""
        job = self.authority.workers.inspect(job_id)
        if job.state != "waiting_dependency" or job.binding is None or job.checkpoint is None:
            raise Problem(409, "NATIVE_ADOPTION_UNCERTAIN", "a lease or missing process is not a native parked checkpoint")
        evidence_id = self.authority.reconcile_sealed(job.binding)
        evidence = self.authority.verify(evidence_id)
        if evidence.state != "parked" or evidence.checkpoint != job.checkpoint:
            raise Problem(403, "NATIVE_ADOPTION_UNCERTAIN", "retained native park/checkpoint binding differs")
        return self.authority.workers.adopt(job_id, context)


def controller_process_entry(factory, *, ticks: int = 1, limit: int = 4, interval_seconds: float = 1):
    """Bounded process entry for a TRUSTED, code-supplied controller factory.

    A parent-owned entrypoint constructs dependencies/credentials in this process
    and returns BehaviorRuntime.controller. No module/command is selected from
    JSON, and no HTTP principal can supply this handle. This entry can be the
    target of a supervised process independent of the API request process.
    """
    from .behavior_runtime import RuntimeController

    if (
        type(ticks) is not int or not 1 <= ticks <= 100 or type(limit) is not int or not 1 <= limit <= 16
        or type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds)
        or not 0 <= interval_seconds <= 30 or not callable(factory)
    ):
        raise Problem(422, "NATIVE_CONTROLLER_LIMIT", "controller process limits/factory are invalid")
    controller = factory()
    if not isinstance(controller, RuntimeController):
        raise Problem(403, "NATIVE_CONTROLLER_REQUIRED", "an opaque configured server controller is required")
    results = []
    for tick in range(ticks):
        results.append(controller.drain(limit=limit))
        if tick + 1 < ticks and interval_seconds:
            time.sleep(interval_seconds)
    return tuple(results)
