"""Azure Web PubSub hints so the workbench reads once on change instead of polling (v16).

Only the API (and the deploy sync job, which runs the API image as the API identity) publishes; Hosted VMs never
do, because Web PubSub Service Owner may send to any group and would cross the Team boundary (ADR-0006 §1).
A hint names IDs only: never conversation text, secrets or tool output. The authority stays the original
receipts and reads; publishing is best effort (one attempt, one log line on failure) and never changes or delays
the original write or response. The workbench keeps polling as its fallback (Free_F1 has no SLA).

Browsers get a short-lived client URL from ``negotiate``: user = owner oid, joined only to its Team's group,
no client roles (cannot join other groups or send).
https://learn.microsoft.com/azure/azure-web-pubsub/concept-service-internals
https://learn.microsoft.com/azure/azure-web-pubsub/concept-azure-ad-authorization
"""

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from .models import ID_PATTERN

ENDPOINT_ENV = "FA_REALTIME_ENDPOINT"
HUB_ENV = "FA_REALTIME_HUB"
DEFAULT_HUB = "fa"
EVENT_SCHEMA = "fa-realtime-event-v1"
NEGOTIATE_SCHEMA = "fa-realtime-negotiate-v1"
TOKEN_MINUTES = 60
MAX_BACKLOG = 64
ENDPOINT_PATTERN = re.compile(r"https://[a-z][a-z0-9-]{1,61}[a-z0-9]\.webpubsub\.azure\.com")
HUB_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,126}")
_ID = re.compile(ID_PATTERN)
# The only names a hint may carry besides the common fields; every value must be one ID-shaped string.
EVENT_FIELDS = {
    "turn.finished": ("request_id",),
    "hitl.approval": ("conversation_id", "request_id", "pending_id", "status"),
    "hitl.question": ("conversation_id", "request_id", "question_id", "status"),
    "hitl.consent": ("conversation_id", "request_id", "consent_id", "status"),
    "hitl.closed": ("conversation_id", "request_id"),
    "agent.received": ("conversation_id", "request_id", "sender_agent_id", "status"),
    "agent.replied": ("conversation_id", "request_id", "sender_agent_id", "source_request_id"),
    "agent.environment": ("version",),
}
log = logging.getLogger(__name__)


def group_name(team_id: str) -> str:
    if not isinstance(team_id, str) or _ID.fullmatch(team_id) is None:
        raise ValueError("a Team ID is required")
    return "team." + team_id


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def hint(team_id: str, event_type: str, fields: dict) -> dict:
    """One ID-only event body; unknown names or non-ID values are a programming error, never sent."""
    allowed = EVENT_FIELDS.get(event_type)
    if allowed is None:
        raise ValueError("unknown realtime event type")
    present = {name: value for name, value in fields.items() if value is not None}
    if set(present) - {"agent_id", *allowed}:
        raise ValueError("realtime hints carry only their declared IDs")
    if any(not isinstance(value, str) or _ID.fullmatch(value) is None for value in present.values()):
        raise ValueError("realtime hint values must be IDs")
    group_name(team_id)
    return {"schema_version": EVENT_SCHEMA, "type": event_type, "team_id": team_id,
            "at": _utc(datetime.now(timezone.utc)), **present}


def agent_id_of(composition, principal) -> str | None:
    """The server's Agent ID for a verified Agent principal (configured target first, else its registered ID)."""
    config = getattr(composition, "config", None)
    for target in getattr(config, "targets", ()) or ():
        if getattr(target, "agent_oid", None) == principal.oid:
            return target.agent_id
    return principal.actor_id if principal.kind == "agent" else None


class RealtimePublisher:
    def __init__(self, endpoint: str, hub: str, client):
        self.endpoint, self.hub, self.client = endpoint, hub, client
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fa-realtime")
        self._lock = threading.Lock()
        self._pending = 0

    def notify(self, team_id: str, event_type: str, *, build=None, **fields) -> None:
        """Queue one hint and return at once; never raises. ``build`` runs on the publisher thread: it may add
        IDs, or return None when the hint is not needed; if it fails, the hint is still sent with ``fields``."""
        with self._lock:
            if self._pending >= MAX_BACKLOG:
                log.warning("FA_REALTIME_PUBLISH_DROPPED type=%s reason=backlog", event_type)
                return
            self._pending += 1
        try:
            self._executor.submit(self._send, team_id, event_type, build, fields)
        except RuntimeError:
            with self._lock:
                self._pending -= 1
            log.warning("FA_REALTIME_PUBLISH_DROPPED type=%s reason=stopped", event_type)

    def _send(self, team_id, event_type, build, fields):
        try:
            if build is not None:
                try:
                    extra = build()
                except Exception as exc:
                    extra = {}
                    log.info("FA_REALTIME_IDS_UNAVAILABLE type=%s exception_type=%s", event_type, type(exc).__name__)
                if extra is None:
                    return
                fields = {**extra, **fields}
            self.client.send_to_group(group_name(team_id), hint(team_id, event_type, fields),
                                      content_type="application/json")
        except Exception as exc:
            log.warning("FA_REALTIME_PUBLISH_FAILED type=%s exception_type=%s", event_type, type(exc).__name__)
        finally:
            with self._lock:
                self._pending -= 1

    def drain(self, timeout: float = 30) -> None:
        """Wait until every hint queued so far was attempted (single worker, FIFO)."""
        self._executor.submit(lambda: None).result(timeout=timeout)

    def client_access(self, owner_oid: str, team_id: str) -> dict:
        """A short-lived client URL: the owner joins only its Team's group and gets no client roles."""
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_MINUTES)
        value = self.client.get_client_access_token(
            user_id=owner_oid, groups=[group_name(team_id)], minutes_to_expire=TOKEN_MINUTES)
        url = value["url"]
        parts = urlsplit(url)
        if (parts.scheme != "wss" or parts.hostname != urlsplit(self.endpoint).hostname or parts.port is not None
                or parts.path != "/client/hubs/" + self.hub or set(parse_qs(parts.query)) != {"access_token"}
                or len(parse_qs(parts.query)["access_token"]) != 1 or parts.fragment):
            raise ValueError("the client access URL is not for this endpoint and hub")
        return {"schema_version": NEGOTIATE_SCHEMA, "url": url, "expires_at": _utc(expires_at), "team_id": team_id}


def _default_client(endpoint, hub, client_id):
    from azure.identity import ManagedIdentityCredential
    from azure.messaging.webpubsubservice import WebPubSubServiceClient

    credential = ManagedIdentityCredential(client_id=client_id or None)
    return WebPubSubServiceClient(endpoint=endpoint, hub=hub, credential=credential,
                                  retry_total=0, connection_timeout=5, read_timeout=10)


_client_factory = _default_client
_publishers: dict[tuple, RealtimePublisher] = {}
_publishers_lock = threading.Lock()


def realtime_settings(environ=None) -> tuple[str, str] | None:
    """(endpoint, hub), or None when unset; a malformed value stops startup instead of failing every hint."""
    environ = os.environ if environ is None else environ
    endpoint = environ.get(ENDPOINT_ENV) or ""
    if not endpoint:
        return None
    hub = environ.get(HUB_ENV) or DEFAULT_HUB
    if ENDPOINT_PATTERN.fullmatch(endpoint) is None or HUB_PATTERN.fullmatch(hub) is None:
        raise ValueError(f"{ENDPOINT_ENV} must be https://<name>.webpubsub.azure.com and {HUB_ENV} a hub name")
    return endpoint, hub


def realtime_publisher(composition=None, *, environ=None) -> RealtimePublisher | None:
    """One process-wide publisher per endpoint/hub/identity; None when realtime hints are not configured."""
    settings = realtime_settings(environ)
    if settings is None:
        return None
    identity = getattr(getattr(composition, "config", None), "identity", None)
    client_id = (getattr(identity, "managed_identity_client_id", None)
                 or (os.environ if environ is None else environ).get("AZURE_CLIENT_ID") or None)
    key = (*settings, client_id)
    with _publishers_lock:
        publisher = _publishers.get(key)
        if publisher is None:
            publisher = _publishers[key] = RealtimePublisher(*settings, _client_factory(*settings, client_id))
        return publisher


def install_realtime_routes(app, composition, identity, prefix: str, publisher: RealtimePublisher | None) -> None:
    """``GET {prefix}/realtime/negotiate``: the policy's designated human only; the Team comes from the server."""
    from fastapi import Depends, Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    from .behavior_api import _response
    from .errors import Problem

    def refused(code, message):
        return JSONResponse(status_code=503, headers={"Cache-Control": "no-store"}, content={"error": {
            "code": code, "message": message, "automatic_retry": False}})

    @app.get(prefix + "/realtime/negotiate")
    async def negotiate(request: Request, principal=Depends(identity)):
        if request.query_params or await request.body():
            raise Problem(422, "BEHAVIOR_INVALID_REQUEST", "negotiate accepts no body or query")
        control = composition.runtime.work_policy
        if control is None:
            raise Problem(503, "WORK_POLICY_DISABLED", "a configured standing scope is required")
        control._human(principal, "read")
        if publisher is None:
            return refused("REALTIME_DISABLED", "Realtime hints are not configured; keep polling.")
        try:
            value = await run_in_threadpool(publisher.client_access, principal.oid, control.policy.template.team_id)
        except Exception as exc:
            log.warning("FA_REALTIME_NEGOTIATE_FAILED exception_type=%s", type(exc).__name__)
            return refused("REALTIME_UNAVAILABLE", "No realtime connection is available now; keep polling.")
        return _response(value)
