"""Pinned public-cloud Connector identity, not delegated human authentication.

Uses the supported Connector REST contract with the repository's pinned
httpx/PyJWT versions, NOT the retired Bot Framework SDK. Exact server-registered
routes avoid SDK conversation truncation. No discovery from JWT jku/x5u/iss.
Native installation, SSO, approval actions, attachments and read receipts are
disabled. Local MockTransport evidence is always OFFLINE / NOT_RUN.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import socket
import threading
import weakref
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Callable, Literal
from urllib.parse import quote, urlsplit

import httpx
import jwt
from pydantic import Field, ValidationError, field_validator, model_validator

from .auth import STALE_JWKS_RETRY_SECONDS, STALE_JWKS_SECONDS, ForcedRefreshWindow
from .behavior_auth import BehaviorPrincipal
from .behavior_delivery import _Model, _json, _utc
from .behavior_ingress import checked_human_text
from .errors import Problem
from .models import Identifier

CONNECTOR_ISSUER = "https://api.botframework.com"
CONNECTOR_JWKS = "https://login.botframework.com/v1/.well-known/keys"
BOT_SCOPE = "https://api.botframework.com/.default"
TEAMS_VERSION = "fa-teams-connector-v1"
# The shared Microsoft signing catalog is much larger than an individual activity.
MAX_CONNECTOR_JWKS_BYTES = 2 * 1024 * 1024
# The key set is fetched before any signature check, so an unauthenticated request must not cause a download:
# one cached set per key source for the whole process (as the Entra JWKS in auth.py), and a kid it lacks forces
# at most one refresh per window. teams_personal builds a ConnectorIdentity per request, so this is module state.
CONNECTOR_JWKS_TTL_SECONDS = 300.0
_FETCH_ERRORS = (httpx.HTTPError, OSError, ValueError, TypeError, KeyError, jwt.PyJWTError)
_log = logging.getLogger(__name__)
Opaque = Annotated[str, Field(min_length=1, max_length=4096)]


def digest(value: object) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def service_url(value: str) -> str:
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https" or url.netloc != "smba.trafficmanager.net"
            or url.port not in (None, 443) or url.username or url.password
            or url.query or url.fragment or not url.path.startswith("/")
            or not url.path.endswith("/") or url.path == "/"
            or any(c not in "/abcdefghijklmnopqrstuvwxyz0123456789-" for c in url.path)
            or "//" in url.path
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise Problem(403, "TEAMS_SERVICE_URL_DENIED", "an exact registered public-cloud service URL is required") from None
    return value


class PublicCloudTransport(httpx.BaseTransport):
    """Pin a public resolved IP for this TLS connection; keep original Host/SNI.

    No proxy/environment trust and no DNS re-resolution between validation and
    connect. Only Microsoft Connector/JWKS hosts are supported by this profile.
    """
    def __init__(self):
        self.transport = httpx.HTTPTransport(retries=0, trust_env=False)

    def handle_request(self, request):
        host = request.url.host
        if (request.url.scheme != "https" or request.url.port not in (None, 443)
                or host not in {"smba.trafficmanager.net", "login.botframework.com"}):
            raise Problem(403, "TEAMS_NETWORK_DENIED", "destination is outside the pinned cloud profile")
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise Problem(403, "TEAMS_NETWORK_DENIED", "destination resolution is not exclusively public")
        pinned = httpx.Request(
            request.method, request.url.copy_with(host=addresses[0][4][0]),
            headers=request.headers, content=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return self.transport.handle_request(pinned)

    def close(self):
        self.transport.close()


class TeamsRouteRecord(_Model):
    schema_version: Literal["fa-teams-route-v1"] = "fa-teams-route-v1"
    cloud: Literal["public"] = "public"
    tenant_id: Identifier
    bot_app_id: Identifier
    bot_id: Opaque
    user_id: Opaque
    aad_object_id: Identifier
    conversation_id: Opaque
    conversation_type: Literal["personal", "groupChat", "channel"]
    thread_id: Opaque | None = None
    service_url: Opaque

    @field_validator("bot_id", "user_id", "conversation_id", "thread_id")
    @classmethod
    def safe_opaque(cls, value):
        if value is not None:
            checked_human_text(value.encode("utf-8"))
            if value in {".", ".."} or any(c.isspace() for c in value):
                raise ValueError("opaque IDs must be complete and nonblank")
        return value

    @field_validator("service_url")
    @classmethod
    def exact_service(cls, value):
        return service_url(value)

    @model_validator(mode="after")
    def thread_scope(self):
        if (self.conversation_type == "channel") != (self.thread_id is not None):
            raise ValueError("only channel routes require an exact thread root")
        return self

    @property
    def route_id(self) -> str:
        return digest(self.model_dump(mode="json"))

    def target(self) -> str:
        path = "v3/conversations/" + quote(self.conversation_id, safe="") + "/activities"
        if self.thread_id is not None:
            path += "/" + quote(self.thread_id, safe="")
        return self.service_url + path


class TeamsSettings(_Model):
    tenant_id: Identifier
    bot_app_id: Identifier
    team_id: Identifier
    routes: Annotated[tuple[TeamsRouteRecord, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def scope(self):
        if (any(r.tenant_id != self.tenant_id or r.bot_app_id != self.bot_app_id for r in self.routes)
                or len({r.route_id for r in self.routes}) != len(self.routes)):
            raise ValueError("routes must have the configured tenant and bot, without duplicates")
        if any(len(route.target()) > 1024 for route in self.routes):
            raise Problem(403, "TEAMS_ROUTE_LIMIT", "the complete encoded route exceeds the existing operation URL bound; truncation is forbidden")
        return self


@dataclass(frozen=True)
class VerifiedTeamsActivity:
    route: TeamsRouteRecord
    principal: BehaviorPrincipal
    provider_activity_id: str
    event_id: str
    body_digest: str
    text: bytes | None
    observed_at: datetime
    disposition: Literal["submit", "silence"]


class _ConnectorKeySet:
    def __init__(self):
        self.lock = threading.Lock()
        self.keys: list[dict] | None = None
        self.fetched_at = 0.0
        self.forced = ForcedRefreshWindow()
        self.stale_retry = ForcedRefreshWindow(STALE_JWKS_RETRY_SECONDS)


_key_sets: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_key_sets_lock = threading.Lock()
_public_key_set = _ConnectorKeySet()


def _key_set(transport: httpx.BaseTransport | None) -> _ConnectorKeySet:
    """Production (no injected transport) shares one set; an offline transport is its own key source."""
    if transport is None:
        return _public_key_set
    with _key_sets_lock:
        return _key_sets.setdefault(transport, _ConnectorKeySet())


def _match(keys: list[dict], kid: str):
    """The one RS256 Teams signing key for ``kid``; None when the set has no such key."""
    candidates = [k for k in keys if k.get("kid") == kid and k.get("kty") == "RSA"
                  and k.get("use", "sig") == "sig" and k.get("alg", "RS256") == "RS256"
                  and isinstance(k.get("endorsements"), list)
                  and "msteams" in k.get("endorsements", [])]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise ValueError
    key = jwt.PyJWK.from_dict(candidates[0], algorithm="RS256").key
    if key.key_size < 2048:
        raise ValueError
    return key


class ConnectorIdentity:
    def __init__(self, settings: TeamsSettings, *, clock: Callable[[], datetime],
                 jwks_transport: httpx.BaseTransport | None = None):
        self.settings = TeamsSettings.model_validate(settings.model_dump())
        self.clock = clock
        self.jwks_transport = jwks_transport

    def _fetch_keys(self) -> list[dict]:
        transport = self.jwks_transport or PublicCloudTransport()
        with httpx.Client(transport=transport, follow_redirects=False, trust_env=False, timeout=5) as client:
            with client.stream("GET", CONNECTOR_JWKS) as response:
                if response.status_code != 200:
                    raise ValueError
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_CONNECTOR_JWKS_BYTES:
                        raise ValueError
        keys = json.loads(data)["keys"]
        if not isinstance(keys, list) or not all(isinstance(k, dict) for k in keys):
            raise ValueError
        return keys

    def _key(self, kid: str):
        cached = _key_set(self.jwks_transport)
        try:
            now = _utc(self.clock()).timestamp()
            with cached.lock:
                age = now - cached.fetched_at
                if cached.keys is None or not 0 <= age < CONNECTOR_JWKS_TTL_SECONDS:
                    # Stale-if-error (as auth.py): Teams does not resend a refused activity, so a failed refresh
                    # keeps the last set for a bounded time and is retried once per window, not per activity.
                    stale = cached.keys is not None and 0 <= age < STALE_JWKS_SECONDS
                    if not stale or cached.stale_retry.claim(now):
                        try:
                            cached.keys, cached.fetched_at = self._fetch_keys(), now
                        except _FETCH_ERRORS:
                            if not stale:
                                raise
                            _log.warning("TEAMS_JWKS_STALE age_seconds=%d", age)
                keys = cached.keys
            key = _match(keys, kid)
            if key is None and cached.forced.claim(now):
                # Outside the lock: requests with a known kid keep the cached set during this download.
                keys = self._fetch_keys()
                with cached.lock:
                    cached.keys, cached.fetched_at = keys, now
                key = _match(keys, kid)
            if key is None:
                raise ValueError
            return key
        except _FETCH_ERRORS:
            raise Problem(401, "TEAMS_SERVICE_AUTH_DENIED", "Connector signing evidence is unavailable or invalid") from None

    def verify(self, authorization: str, activity: dict, *, body_digest: str | None = None) -> VerifiedTeamsActivity:
        try:
            now = _utc(self.clock())
            if not isinstance(authorization, str) or not authorization.startswith("Bearer ") or len(authorization) > 16384:
                raise ValueError
            token = authorization[7:]
            header = jwt.get_unverified_header(token)
            if (header.get("alg") != "RS256" or not isinstance(header.get("kid"), str)
                    or not 0 < len(header["kid"]) <= 256 or "crit" in header):
                raise ValueError
            claims = jwt.decode(token, self._key(header["kid"]), algorithms=["RS256"],
                                audience=self.settings.bot_app_id, issuer=CONNECTOR_ISSUER,
                                options={"require": ["iss", "aud", "exp", "nbf", "serviceurl"],
                                         "verify_exp": False, "verify_nbf": False, "verify_iat": False,
                                         "strict_aud": True})
            if (any(type(claims[k]) not in (int, float) or not math.isfinite(claims[k])
                    for k in ("exp", "nbf", "iat") if k in claims)
                    or not claims["nbf"] <= now.timestamp() < claims["exp"]
                    or "iat" in claims and claims["iat"] > now.timestamp()
                    or claims["serviceurl"] != activity.get("serviceUrl")):
                raise ValueError
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise Problem(401, "TEAMS_SERVICE_AUTH_DENIED", "valid scoped Connector authentication is required") from None
        try:
            if activity["channelId"] != "msteams" or activity["channelData"]["tenant"]["id"] != self.settings.tenant_id:
                raise ValueError
            conversation = activity["conversation"]
            if conversation.get("tenantId", self.settings.tenant_id) != self.settings.tenant_id:
                raise ValueError
            thread_id = activity.get("replyToId")
            if conversation["conversationType"] == "channel" and "replyToId" not in activity and activity.get("type") == "message":
                thread_id = activity["id"]
            route = TeamsRouteRecord(
                tenant_id=self.settings.tenant_id, bot_app_id=self.settings.bot_app_id,
                bot_id=activity["recipient"]["id"], user_id=activity["from"]["id"],
                aad_object_id=activity["from"]["aadObjectId"], conversation_id=conversation["id"],
                conversation_type=conversation["conversationType"],
                thread_id=thread_id, service_url=activity["serviceUrl"],
            )
            if route not in self.settings.routes:
                raise ValueError
            provider_id = activity["id"]
            if not isinstance(provider_id, str) or not 0 < len(provider_id) <= 4096:
                raise ValueError
            checked_human_text(provider_id.encode("utf-8"))
        except (ValidationError, KeyError, ValueError, TypeError):
            raise Problem(403, "TEAMS_IDENTITY_ROUTE_DENIED", "Teams tenant, actor, bot or thread is not registered") from None
        kind = activity.get("type")
        text = None
        if kind == "message":
            if (not isinstance(activity.get("text"), str) or activity.get("attachments")
                    or activity.get("value") or activity.get("entities")
                    or activity.get("textFormat", "plain") != "plain"):
                raise Problem(422, "TEAMS_CONTENT_UNSUPPORTED", "only complete plain human text is enabled")
            text = checked_human_text(activity["text"].encode("utf-8")).encode("utf-8")
        elif kind not in {"messageReaction", "event", "typing", "conversationUpdate"}:
            raise Problem(422, "TEAMS_ACTIVITY_UNSUPPORTED", "this activity cannot create work or approval")
        event_id = digest(["fa-teams-source-v1", route.tenant_id, route.bot_app_id,
                           route.conversation_id, provider_id])
        return VerifiedTeamsActivity(
            route, BehaviorPrincipal(route.tenant_id, route.aad_object_id, self.settings.team_id,
                                     "human", None, frozenset({"submit"})),
            provider_id, event_id, body_digest or digest(activity), text, now,
            "submit" if text is not None else "silence",
        )
