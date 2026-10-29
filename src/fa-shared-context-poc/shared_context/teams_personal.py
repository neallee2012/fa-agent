"""Personal-only, signed Connector route enrollment and parent composition.

The setup marker is a control handshake, NOT business admission or a bot reply.
Only the configured same-tenant admin can establish one exact immutable route.
Operator JSON, license metadata and installation metadata cannot supply it.
Injected JWKS transports are always OFFLINE evidence, never native route proof.
Native admission/send require explicit current installed Teams evidence.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Literal

import httpx
from azure.identity import ManagedIdentityCredential
from fastapi import APIRouter, Request
from pydantic import ValidationError, field_validator

from .behavior_delivery import (
    ArtifactVersion, DeliveryCore, RuntimeRegistration, _Model, _material, _utc, authority_snapshot,
)
from .behavior_operations import ExecutionContext, control_key
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest, ControlWrite, Digest
from .errors import Problem
from .teams_deployment import CALLBACK_PATH, INTENDED_USER, PersonalTeamsConfig, TENANT
from .teams_identity import BOT_SCOPE, TEAMS_VERSION, ConnectorIdentity, TeamsRouteRecord, TeamsSettings, VerifiedTeamsActivity, digest
from .teams_runtime import TeamsAdmission, TeamsRuntime, activity_response, retained
from .teams_delivery import TeamsArtifactLink, TeamsDeliveryAdapter, TeamsSendContext
from .teams_native import TeamsDeliveryCore, TeamsNativeRegistration
from .teams_handshake import normalize_handshake

ROUTE_HANDSHAKE = "FA Teams route enrollment"


def _activity(body: bytes) -> dict:
    try:
        if not isinstance(body, bytes) or not 0 < len(body) <= 32768:
            raise ValueError
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError
                value[key] = item
            return value
        value = json.loads(body, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise Problem(422, "TEAMS_PERSONAL_ACTIVITY_INVALID", "one complete bounded JSON activity without duplicate fields is required") from None


class _RouteWitness(_Model):
    schema_version: Literal["fa-teams-personal-route-v1"] = "fa-teams-personal-route-v1"
    configuration_digest: Digest
    verifier_version: Literal["fa-teams-connector-v1"] = TEAMS_VERSION
    route: TeamsRouteRecord
    event_id: Digest
    body_digest: Digest
    observed_at: datetime
    evidence_mode: Literal["offline_contract", "connector_http"]

    _time = field_validator("observed_at")(_utc)


class PersonalRouteEnrollment:
    def __init__(self, store: BehaviorStore, config: PersonalTeamsConfig, *,
                 clock: Callable[[], datetime], jwks_transport: httpx.BaseTransport | None = None):
        self.store = store
        self.config = PersonalTeamsConfig.model_validate(config.model_dump())
        self.clock, self.jwks_transport = clock, jwks_transport
        self.key = control_key("teams-personal-route", self.config.configuration_digest)

    def _witness(self):
        if authority_snapshot(self.store, (self.key,))[self.key] is None:
            raise Problem(403, "TEAMS_PERSONAL_ROUTE_REQUIRED", "a true signed personal Connector activity must establish the exact route")
        witness = retained(self.store, self.key, _RouteWitness)
        route = witness.route
        if (witness.configuration_digest != self.config.configuration_digest
                or route.tenant_id != TENANT or route.aad_object_id != INTENDED_USER
                or route.bot_app_id != self.config.uami_client_id
                or route.bot_id != "28:" + self.config.uami_client_id
                or route.conversation_type != "personal" or route.thread_id is not None):
            raise Problem(403, "TEAMS_PERSONAL_ROUTE_DENIED", "retained route differs from the pinned personal identity profile")
        return witness

    def settings(self, *, require_live: bool = True) -> TeamsSettings:
        witness = self._witness()
        if require_live and witness.evidence_mode != "connector_http":
            raise Problem(403, "TEAMS_PERSONAL_LIVE_ROUTE_REQUIRED", "offline signing fixtures are not a native Connector route")
        return TeamsSettings(
            tenant_id=TENANT, bot_app_id=self.config.uami_client_id,
            team_id=self.config.team_id, routes=(witness.route,),
        )

    def capture(self, authorization: str, body: bytes) -> dict:
        activity = _activity(body)
        try:
            if (activity["type"] != "message" or activity["text"] != ROUTE_HANDSHAKE
                    or activity["channelId"] != "msteams"
                    or activity["channelData"]["tenant"]["id"] != TENANT
                    or activity["from"]["aadObjectId"] != INTENDED_USER
                    or activity["recipient"]["id"] != "28:" + self.config.uami_client_id
                    or activity["conversation"]["conversationType"] != "personal"
                    or "replyToId" in activity):
                raise ValueError
            candidate = TeamsRouteRecord(
                tenant_id=TENANT, bot_app_id=self.config.uami_client_id,
                bot_id=activity["recipient"]["id"], user_id=activity["from"]["id"],
                aad_object_id=INTENDED_USER, conversation_id=activity["conversation"]["id"],
                conversation_type="personal", service_url=activity["serviceUrl"],
            )
            settings = TeamsSettings(
                tenant_id=TENANT, bot_app_id=self.config.uami_client_id,
                team_id=self.config.team_id, routes=(candidate,),
            )
        except (KeyError, ValueError, TypeError, ValidationError):
            raise Problem(403, "TEAMS_PERSONAL_ENROLLMENT_DENIED", "only the designated personal-chat setup handshake is accepted") from None
        identity = ConnectorIdentity(settings, clock=self.clock, jwks_transport=self.jwks_transport)
        verified = identity.verify(authorization, normalize_handshake(activity, ROUTE_HANDSHAKE),
                                   body_digest=hashlib.sha256(body).hexdigest())
        prior = authority_snapshot(self.store, (self.key,))[self.key]
        if prior is not None:
            witness = self._witness()
            if (witness.route != verified.route or witness.event_id == verified.event_id
                    and witness.body_digest != verified.body_digest):
                raise Problem(409, "TEAMS_PERSONAL_ROUTE_CONFLICT", "an enrolled route or original event body cannot be replaced")
            return self._result(witness)
        witness = _RouteWitness(
            configuration_digest=self.config.configuration_digest, route=verified.route,
            event_id=verified.event_id, body_digest=verified.body_digest, observed_at=_utc(self.clock()),
            evidence_mode="connector_http" if self.jwks_transport is None else "offline_contract",
        )
        data = witness.model_dump(mode="json")
        request = ControlRequest(
            request_id=control_key("teams-personal-enroll", self.config.configuration_digest),
            request_digest=digest(data), read_set=(self.key,),
        )
        self.store.transact(request, lambda _: ControlChanges(
            writes=(ControlWrite(row_id=self.key, mode="create", data=data, material=_material(data)),),
            result=self._result(witness),
        ))
        return self._result(self._witness())

    @staticmethod
    def _result(witness):
        return {
            "status": "route_recorded_not_admitted", "route_id": witness.route.route_id,
            "evidence_mode": witness.evidence_mode, "work_admitted": False,
            "native_gate": "closed", "actual_environment": "NOT_RUN",
        }


@dataclass(frozen=True)
class BotTokenLease:
    token: str = field(repr=False)
    expires_at: datetime


class UamiBotToken:
    """Lazy server dependency; no DefaultAzureCredential or user-token fallback."""
    def __init__(self, config: PersonalTeamsConfig, *, clock: Callable[[], datetime],
                 credential=None):
        self.config = PersonalTeamsConfig.model_validate(config.model_dump())
        self.clock = clock
        self.native_credential = credential is None
        self.credential = credential if credential is not None else ManagedIdentityCredential(
            client_id=config.uami_client_id, logging_enable=False,
        )

    def __call__(self, scope: str) -> str:
        return self.acquire(scope).token

    def acquire(self, scope: str) -> BotTokenLease:
        if scope != BOT_SCOPE:
            raise Problem(403, "TEAMS_BOT_SCOPE_DENIED", "only the registered Connector bot scope may be acquired")
        try:
            token = self.credential.get_token(BOT_SCOPE)
            if (not isinstance(token.token, str) or not 0 < len(token.token) <= 32768
                    or any(c.isspace() for c in token.token)
                    or type(token.expires_on) is not int or token.expires_on <= _utc(self.clock()).timestamp()):
                raise ValueError
            return BotTokenLease(token.token, datetime.fromtimestamp(token.expires_on, timezone.utc))
        except Exception:
            # A credential-provider exception can itself contain credentials.
            raise Problem(502, "TEAMS_BOT_CREDENTIAL_UNAVAILABLE", "the bound UAMI did not provide a usable bot credential") from None

    def close(self):
        self.credential.close()


@dataclass(frozen=True)
class TeamsParentSeams:
    resolve_admission: Callable[[VerifiedTeamsActivity], TeamsAdmission]
    resolve_context: Callable[[str], ExecutionContext]
    process_work: Callable[[str, ExecutionContext], None]
    resolve_send: Callable[[str], TeamsSendContext]

    def __post_init__(self):
        if not all(callable(value) for value in (
            self.resolve_admission, self.resolve_context, self.process_work, self.resolve_send,
        )):
            raise Problem(503, "TEAMS_PARENT_SEAM_REQUIRED", "all trusted parent work/ownership/grant resolvers must be registered")


class PersonalTeamsEntry:
    """One callback path for capture-first setup and later parent runtime wiring."""
    def __init__(self, enrollment: PersonalRouteEnrollment):
        self.enrollment, self.runtime = enrollment, None

    def attach_runtime(self, runtime: TeamsRuntime) -> None:
        if type(runtime) is not TeamsRuntime or runtime.store is not self.enrollment.store or runtime.core is None:
            raise Problem(403, "TEAMS_PERSONAL_COMPOSITION_DENIED", "the parent runtime must share the enrollment authority")
        registration = runtime.core.registration
        if registration is None:
            raise Problem(403, "TEAMS_PERSONAL_COMPOSITION_DENIED", "a server runtime registration is required")
        settings = self.enrollment.settings(require_live=registration.mode == "native")
        if runtime.identity.settings != settings:
            raise Problem(403, "TEAMS_PERSONAL_COMPOSITION_DENIED", "runtime routes differ from the enrolled personal route")
        runtime._core()
        self.runtime = runtime

    def receive(self, authorization: str, body: bytes) -> dict:
        activity = _activity(body)
        if activity.get("text") == ROUTE_HANDSHAKE:
            if self.runtime is not None:
                raise Problem(403, "TEAMS_PERSONAL_ENROLLMENT_CLOSED", "the existing route is fixed; work entry cannot restart enrollment")
            return self.enrollment.capture(authorization, body)
        if self.runtime is None:
            raise Problem(403, "TEAMS_PERSONAL_NATIVE_CLOSED", "only signed route enrollment is installed; no business work is admitted")
        return self.runtime.receive(authorization, body)

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.post(CALLBACK_PATH)
        async def activity(request: Request):
            return await activity_response(request, self, "the complete activity exceeds the fixed byte bound")

        return router


@dataclass(frozen=True)
class PersonalChannel:
    core: DeliveryCore
    runtime: TeamsRuntime
    adapter: TeamsDeliveryAdapter
    entry: PersonalTeamsEntry


def compose_personal_channel(
    enrollment: PersonalRouteEnrollment, registration: RuntimeRegistration, seams: TeamsParentSeams, *,
    transport: httpx.BaseTransport | None = None, bot_token: Callable[[str], str] | None = None,
    artifact_links: Callable[[tuple[ArtifactVersion, ...], TeamsRouteRecord], tuple[TeamsArtifactLink, ...]] | None = None,
    artifact_origins: tuple[str, ...] = (),
    native_registration: TeamsNativeRegistration | None = None,
    shared_channels=None,
) -> PersonalChannel:
    """Wire the real parent resolvers; never manufacture readiness or enable flags.

    Parent supplies its existing registered result/readiness verifiers and
    durable/idempotent process_work. Native requires live route proof AND current
    installed native evidence. Publishing and enabling remain explicit actions.
    """
    if registration.contract.channel.channel_id != "msteams":
        raise Problem(403, "TEAMS_PARENT_CHANNEL_INCOMPATIBLE",
                      "the installed single-channel parent contract is not Teams; changing its channel would invalidate the shared quota/index binding")
    if registration.mode == "native" and (
        enrollment.jwks_transport is not None or native_registration is None
        or native_registration.personal != enrollment.config
    ):
        raise Problem(403, "TEAMS_NATIVE_DISABLED", "native requires live Connector validation and explicit matching server registration")
    settings = enrollment.settings(require_live=registration.mode == "native")
    if registration.contract.team_id != enrollment.config.team_id:
        raise Problem(403, "TEAMS_PERSONAL_COMPOSITION_DENIED", "parent registration belongs to another FA team")
    if registration.mode == "native" and bot_token is not None and (
        type(bot_token) is not UamiBotToken or bot_token.config != enrollment.config
    ):
        raise Problem(403, "TEAMS_BOT_IDENTITY_REQUIRED", "native composition requires the exact configured UAMI dependency")
    identity = ConnectorIdentity(settings, clock=enrollment.clock, jwks_transport=enrollment.jwks_transport)
    runtime = TeamsRuntime(
        enrollment.store, identity, clock=enrollment.clock,
        resolve_admission=seams.resolve_admission, resolve_context=seams.resolve_context,
        process_work=seams.process_work,
    )
    adapter = TeamsDeliveryAdapter(
        enrollment.store, settings, clock=enrollment.clock, resolve_send=seams.resolve_send,
        bot_token=bot_token if bot_token is not None else UamiBotToken(enrollment.config, clock=enrollment.clock),
        transport=transport, artifact_links=artifact_links, artifact_origins=artifact_origins,
        native_registration=native_registration,
    )
    core = TeamsDeliveryCore(
        enrollment.store, clock=enrollment.clock,
        shared_channels=shared_channels,
        registration=replace(registration, verify_ingress=runtime.verify_ingress, channel_adapter=adapter),
    )
    runtime.attach(core)
    adapter.attach(core)
    entry = PersonalTeamsEntry(enrollment)
    entry.attach_runtime(runtime)
    return PersonalChannel(core, runtime, adapter, entry)
