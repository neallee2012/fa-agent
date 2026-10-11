"""One-send Connector REST Adapter over EXISTING operations and delivery ledgers.

Public server interfaces: TeamsDeliveryAdapter.attach(core), operation_id(id),
prepare(id) -> immutable TeamsSendPlan, send(id), reconcile(id), inspect(id).
resolve_send is a mandatory server-registered CURRENT owner/grant resolver.
TeamsRouteAccess is maintained by the server through the same BehaviorStore
guard; it is never accepted from activity text, a model, or this HTTP router.
The separate human GrantRecord still comes from the delegated approval path.

prepare fixes exact POST/URL/body/artifacts before approval. send exclusively
claims one invocation, reserves via ToolBrokerCore.authorize, then consumes a
fresh guard gate checking current route/access/owner/epoch/policy/grant and that
same immutable description. Only the fresh caller may enter HTTP within two
trusted UTC seconds. A late revocation is in-flight, not retroactive recall.
No POST retries, redirects, update/delete, rerouting or idempotency claims.

teams-send is transport ownership, not a second operation/obligation ledger.
Operation/attempt/budget/grant and delivery outbox/receipt are the existing rows.
Retained HTTP witnesses survive receipt-transaction failure; restart reconciles
them without network. Missing/empty/lost evidence remains unknown without refunds.
Only provider_accepted is supported, never client/UI/human-read confirmation.
The existing inline no-GC authority protects plans, routes and receipts. Artifact
download/retention/current read authorization remains the parent's artifact seam.

REST contract: learn.microsoft.com/azure/bot-service/rest-api/
bot-framework-rest-connector-api-reference (Send/Reply to Activity,
ResourceResponse). Pinned dependencies are in requirements.txt; no retired SDK.
All local tests are OFFLINE. Native admission/send installation is NOT_RUN.
Offline registrations require an explicit MockTransport; they cannot fall back
to a real network send using synthetic readiness. Native requires an explicit
TeamsNativeRegistration, retained signed route and current installed evidence.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, ValidationError, model_serializer

from .behavior_delivery import (
    ArtifactVersion, DeliveryBinding, DeliveryCore, ReceiptRequest, VerifiedDeliveryReceipt,
    _Model, _Obligation, _Outbox, _Payload, _check, _json, _material, _parse,
    _utc, authority_snapshot,
)
from .behavior_ingress import checked_human_text
from .behavior_dispatch import ArtifactRoute
from .behavior_operations import (
    AdapterContract, AuthorizationRequest, BudgetRecord, ExecutionContext, GrantRecord,
    GrantSubject, OperationDescription, OutcomeRequest, PolicyRecord, ReceiptResult,
    RegisteredAdapter, ResolvedResource, ToolBrokerCore, ToolIntent, VerifiedOutcome,
    _Attempt, canonical_material, control_key,
)
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest, ControlWrite, Digest, InlineMaterial
from .errors import Problem
from .models import Identifier
from .records import Record
from .teams_identity import BOT_SCOPE, TEAMS_VERSION, PublicCloudTransport, TeamsRouteRecord, TeamsSettings, digest
from .teams_runtime import retained
from .teams_native import TeamsDeliveryCore, TeamsNativeEvidence, TeamsNativeGate, TeamsNativeRegistration


class TeamsSendContext(_Model):
    context: ExecutionContext
    grant_id: Identifier


class TeamsRouteAccess(_Model):
    """Server authorization for the ENTIRE route audience, including a group/channel."""
    schema_version: Literal["fa-teams-route-access-v1"] = "fa-teams-route-access-v1"
    route_id: Digest
    recipient_id: Identifier
    active: bool
    revision: Annotated[int, Field(ge=1)] = 1
    owner_record_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=16)]
    artifacts: Annotated[tuple[ArtifactVersion, ...], Field(max_length=16)]


class TeamsArtifactLink(_Model):
    artifact: ArtifactVersion
    url: Annotated[str, Field(min_length=1, max_length=2048)]


class TeamsSendPlan(_Model):
    schema_version: Literal["fa-teams-send-plan-v1"] = "fa-teams-send-plan-v1"
    binding: DeliveryBinding
    route: TeamsRouteRecord
    operation_id: Identifier
    description: OperationDescription
    evidence_mode: Literal["offline_contract", "native"] = "offline_contract"

    @model_serializer(mode="wrap")
    def compatible_material(self, serialize):
        data = serialize(self)
        if self.evidence_mode == "offline_contract":
            data.pop("evidence_mode", None)
        return data

    @property
    def description_digest(self):
        return canonical_material(self.description).sha256


class _Send(_Model):
    schema_version: Literal["fa-teams-attempt-v1"] = "fa-teams-attempt-v1"
    delivery_id: Identifier
    operation_id: Identifier
    attempt_id: Identifier
    owner: TeamsSendContext
    description_digest: Digest
    claim_request: ControlRequest
    phase: Literal["claimed", "consumed"] = "claimed"
    gate_request: ControlRequest | None = None
    gated_at: datetime | None = None
    not_after: datetime | None = None
    access_revision: int | None = None
    native_evidence_id: Identifier | None = None
    native_evidence_digest: Digest | None = None
    native_installation_data_sha256: Digest | None = None

    @model_serializer(mode="wrap")
    def compatible_material(self, serialize):
        data = serialize(self)
        if (self.native_evidence_id is None and self.native_evidence_digest is None
                and self.native_installation_data_sha256 is None):
            for key in ("native_evidence_id", "native_evidence_digest", "native_installation_data_sha256"):
                data.pop(key, None)
        return data


class _ProviderReceipt(_Model):
    schema_version: Literal["fa-teams-http-receipt-v1"] = "fa-teams-http-receipt-v1"
    binding: DeliveryBinding
    operation_id: Identifier
    attempt_id: Identifier
    request_id: Digest
    description_digest: Digest
    method: Literal["POST"] = "POST"
    target: str
    body_digest: Digest
    observed_at: datetime
    http_status: int | None
    provider_activity_id: str | None
    evidence_mode: Literal["offline_contract", "connector_http"] = "offline_contract"
    native_evidence_id: Identifier | None = None
    native_evidence_digest: Digest | None = None
    problem_code: Literal[
        "TEAMS_CREDENTIAL_UNAVAILABLE", "TEAMS_SEND_GATE_DENIED", "TEAMS_REDIRECT_DENIED",
        "TEAMS_HTTP_UNCONFIRMED", "TEAMS_PROVIDER_RESPONSE_INVALID", "TEAMS_TRANSPORT_UNKNOWN",
    ] | None = None

    @model_serializer(mode="wrap")
    def compatible_material(self, serialize):
        data = serialize(self)
        if (self.evidence_mode == "offline_contract"
                and self.native_evidence_id is None and self.native_evidence_digest is None):
            for key in ("evidence_mode", "native_evidence_id", "native_evidence_digest"):
                data.pop(key, None)
        return data


class _OneSend(httpx.BaseTransport):
    def __init__(self, transport, plan, send, clock, *, check_current=None, token_expires_at=None):
        self.transport, self.plan, self.send, self.clock = transport, plan, send, clock
        self.check_current, self.token_expires_at = check_current, token_expires_at
        self.used = False

    def handle_request(self, request):
        if self.check_current is not None:
            self.check_current()
        now = _utc(self.clock())
        if (self.used or self.send.gated_at is None or self.send.not_after is None
                or not self.send.gated_at <= now < self.send.not_after
                or self.token_expires_at is not None and now >= self.token_expires_at
                or request.method != self.plan.description.method
                or str(request.url) != self.plan.description.target
                or request.content != self.plan.description.content):
            raise Problem(403, "TEAMS_SEND_GATE_DENIED", "wire request differs from the consumed immutable send")
        self.used = True
        return self.transport.handle_request(request)

    def close(self):
        self.transport.close()


class TeamsDeliveryAdapter:
    def __init__(self, store: BehaviorStore, settings: TeamsSettings, *,
                 clock: Callable[[], datetime],
                 resolve_send: Callable[[str], TeamsSendContext],
                 bot_token: Callable[[str], str],
                 transport: httpx.BaseTransport | None = None,
                 artifact_links: Callable[[tuple[ArtifactVersion, ...], TeamsRouteRecord], tuple[TeamsArtifactLink, ...]] | None = None,
                 artifact_origins: tuple[str, ...] = (),
                 native_registration: TeamsNativeRegistration | None = None, cleanup_authority=None):
        self.store, self.settings, self.clock = store, TeamsSettings.model_validate(settings.model_dump()), clock
        self.resolve_send, self.bot_token, self.transport = resolve_send, bot_token, transport
        self.artifact_links, self.artifact_origins = artifact_links, artifact_origins
        self.core: DeliveryCore | None = None
        if native_registration is not None and type(native_registration) is not TeamsNativeRegistration:
            raise Problem(403, "TEAMS_NATIVE_DISABLED", "native registration must be a trusted server dependency")
        self.native_registration = native_registration
        if cleanup_authority is not None:
            from .behavior_teams_cleanup import CleanupDelivery
            if type(cleanup_authority) is not CleanupDelivery:
                raise Problem(403, "TEAMS_CLEANUP_DELIVERY_DENIED", "trusted original-result capability required")
        self.cleanup_authority = cleanup_authority
        self.native = None if native_registration is None else TeamsNativeGate(self, native_registration)
        self.contract = AdapterContract(
            adapter_id="teams-connector", adapter_version=TEAMS_VERSION, allowed_kinds=("request",),
            allowed_methods=("POST",), allowed_targets=tuple(dict.fromkeys(r.target() for r in settings.routes)),
            reservation_units=1, evidence_source="teams-http", outcome_contract_version=TEAMS_VERSION,
        )
        self.contract_digest = digest(self.contract.model_dump(mode="json"))
        self.operations = ToolBrokerCore(store, clock=clock, adapter=RegisteredAdapter(
            contract=self.contract, resolve=lambda key: self.plan(key).description,
            verify_outcome=self._outcome,
        ))

    @staticmethod
    def operation_id(delivery_id: str) -> str:
        return control_key("teams-send-operation", delivery_id)

    def attach(self, core: DeliveryCore):
        self.core = core
        self.check_structure(core)

    def check_structure(self, core):
        if (core is not self.core or core.store is not self.store or core.registration is None
                or core.registration.channel_adapter is not self or core.contract is None
                or core.contract.team_id != self.settings.team_id
                or core.contract.channel.channel_id != "msteams" or core.contract.channel.version != TEAMS_VERSION
                or core.contract.channel.supported_levels != ("provider_accepted",)
                or core.contract.channel.max_confirmation_level != "provider_accepted"):
            raise Problem(403, "TEAMS_REGISTRATION_DENIED", "exact concrete Teams channel registration is required")
        if core.registration.mode == "native":
            from .teams_personal import UamiBotToken

            if type(self.native_registration) is not TeamsNativeRegistration or self.native is None:
                raise Problem(403, "TEAMS_NATIVE_DISABLED", "explicit native Teams registration is not installed")
            if core.registration.verify_readiness is None or core.registration.verify_work_result is None:
                raise Problem(403, "TEAMS_NATIVE_DISABLED", "native requires registered readiness and saved-work verification")
            if type(core) is not TeamsDeliveryCore:
                raise Problem(403, "TEAMS_NATIVE_DISABLED", "native requires installation predicates in the admission transaction")
            if (self.transport is not None or type(self.bot_token) is not UamiBotToken
                    or not self.bot_token.native_credential
                    or self.bot_token.config != self.native_registration.personal):
                raise Problem(403, "TEAMS_NATIVE_DEPENDENCY_DENIED", "native requires the pinned network transport and registered bot UAMI")
        elif core.registration.mode != "offline_contract":
            raise Problem(403, "TEAMS_NATIVE_DISABLED", "unknown Teams registration mode")
        elif type(self.transport) is not httpx.MockTransport:
            raise Problem(403, "TEAMS_OFFLINE_TRANSPORT_REQUIRED", "offline evidence permits only the explicit local HTTP test transport")

    def check_installation(self, core, rows=None):
        self.check_structure(core)
        if self.cleanup_authority is not None:
            return self.cleanup_authority.check(rows)
        if getattr(core, "shared_channels", None) is not None:
            core.shared_channels.check_current(core, rows)
        return self.native.check(rows) if core.registration.mode == "native" else None

    def publish_installation(self):
        core = self._core()
        if core.registration.mode != "native":
            raise Problem(403, "TEAMS_NATIVE_DISABLED", "offline evidence cannot publish a native installation")
        return self.native.publish()

    def installation_keys(self):
        core = self._core()
        keys = self.native.keys if core.registration.mode == "native" else ()
        if getattr(core, "shared_channels", None) is not None:
            keys = (*keys, *core.shared_channels.current_keys)
        if self.cleanup_authority is not None:
            keys = (*keys, self.cleanup_authority.authorization_key)
        return keys

    def _core(self):
        if self.core is None:
            raise Problem(503, "TEAMS_NOT_ATTACHED", "Teams sender has no delivery authority")
        self.check_structure(self.core)
        return self.core

    def plan(self, delivery_id: str) -> TeamsSendPlan:
        plan = retained(self.store, control_key("teams-send-plan", delivery_id), TeamsSendPlan)
        if (plan.binding.delivery_id != delivery_id or plan.operation_id != self.operation_id(delivery_id)
                or plan.description.target != plan.route.target()
                or plan.description.method != "POST"
                or plan.evidence_mode != self._core().registration.mode
                or plan.route not in self.settings.routes or plan.route.route_id != plan.binding.route.thread_id):
            raise Problem(409, "TEAMS_PLAN_CHANGED", "retained plan no longer matches its exact route and operation")
        return plan

    def _body(self, payload: _Payload, route: TeamsRouteRecord) -> bytes:
        if payload.content is None:
            if payload.message_kind != "failure":
                raise Problem(409, "TEAMS_RESULT_MISSING", "required saved result is missing")
            text = "成果尚未確認交付，需要後續處理。"
        else:
            text = checked_human_text(payload.content)
        if payload.message_kind == "failure":
            known = "尚未確認" if payload.known_effects is None else ", ".join(payload.known_effects) or "未回報"
            unresolved = "尚未確認" if payload.unresolved_operations is None else ", ".join(payload.unresolved_operations) or "未回報"
            text += (f"\n工作狀態：{payload.work_state}"
                     f"\n已知效果：{known}\n待核對操作：{unresolved}"
                     f"\n後續負責人：{payload.responsibility_owner}")
        if payload.artifacts:
            if self.artifact_links is None:
                raise Problem(403, "TEAMS_ARTIFACTS_DISABLED", "trusted fixed-version artifact links are not installed")
            links = tuple(_check(link, TeamsArtifactLink) for link in self.artifact_links(payload.artifacts, route))
            if tuple(link.artifact for link in links) != payload.artifacts:
                raise Problem(403, "TEAMS_ARTIFACT_SCOPE_DENIED", "every required immutable artifact must be present in order")
            for link in links:
                try:
                    url = urlsplit(link.url)
                    expected_path = ArtifactRoute(
                        team=self.settings.team_id, artifact_id=link.artifact.artifact_id,
                        version_id=link.artifact.version,
                    ).path()
                except (ValidationError, ValueError):
                    raise Problem(403, "TEAMS_ARTIFACT_SCOPE_DENIED", "an exact valid M3 artifact version route is required") from None
                if (url.scheme != "https" or url.username or url.password or url.query or url.fragment
                        or url.path != expected_path
                        or f"https://{url.netloc}" not in self.artifact_origins):
                    raise Problem(403, "TEAMS_ARTIFACT_SCOPE_DENIED", "artifact link is outside the trusted download route")
                text += (f"\n成果 {link.artifact.artifact_id} 版本 {link.artifact.version}"
                         f" SHA-256 {link.artifact.sha256}\n{link.url}")
        checked_human_text(text.encode("utf-8"))
        body = {
            "type": "message", "textFormat": "plain", "text": text,
            "from": {"id": route.bot_id}, "recipient": {"id": route.user_id},
            "conversation": {"id": route.conversation_id},
        }
        if route.thread_id is not None:
            body["replyToId"] = route.thread_id
        return _json(body)

    def _delivery(self, rows, delivery_id, owner):
        core = self._core()
        outbox = core._row(rows, control_key("delivery-outbox", delivery_id), _Outbox)
        ob = core._obligation(rows, outbox.binding.obligation_id, owner.context)
        if (outbox.binding.route != ob.route or outbox.binding.channel_version != TEAMS_VERSION
                or ob.required_confirmation_level != "provider_accepted"
                or outbox.binding.delivery_id != delivery_id or ob.final_delivery_id != delivery_id
                or ob.final_version != outbox.binding.version or outbox.message_kind not in {"final", "failure"}):
            raise Problem(403, "TEAMS_DELIVERY_SCOPE_DENIED", "only the exact current final/failure may be sent")
        material = _parse(rows[control_key("delivery-outbox", delivery_id)].payload["material"], InlineMaterial)
        payload = _Payload.model_validate_json(material.data)
        if (material.sha256 != outbox.binding.payload_digest or payload.artifacts != outbox.binding.artifacts
                or payload.work_id != ob.work_id or payload.root_request_id != ob.root_request_id
                or payload.event_id != ob.event_id or payload.message_kind != outbox.message_kind):
            raise Problem(409, "TEAMS_PAYLOAD_CHANGED", "outbox payload and obligation binding differ")
        return ob, outbox, payload

    def _keys(self, binding, owner):
        return (
            control_key("delivery-outbox", binding.delivery_id),
            control_key("delivery-obligation", binding.obligation_id),
            control_key("owner", owner.context.owner_record_id),
            control_key("delivery-conversation", owner.context.conversation_id),
            control_key("teams-route", binding.route.thread_id),
            control_key("teams-route-access", binding.route.thread_id),
        )

    def _route_access(self, rows, binding, owner):
        route_key = control_key("teams-route", binding.route.thread_id)
        access_key = control_key("teams-route-access", binding.route.thread_id)
        route = self._core()._row(rows, route_key, TeamsRouteRecord)
        access = self._core()._row(rows, access_key, TeamsRouteAccess)
        if (route.route_id != binding.route.thread_id or route not in self.settings.routes
                or _parse(rows[route_key].payload["material"], InlineMaterial) != _material(route.model_dump(mode="json"))
                or route.tenant_id != binding.route.tenant_id or route.bot_app_id != binding.route.account_id
                or route.aad_object_id != binding.route.recipient_id
                or access.route_id != route.route_id or access.recipient_id != route.aad_object_id
                or not access.active or owner.context.owner_record_id not in access.owner_record_ids
                or not set(binding.artifacts) <= set(access.artifacts)):
            raise Problem(403, "TEAMS_ROUTE_ACCESS_DENIED", "current recipient, sender or artifact scope is not authorized")
        return route, access

    def prepare(self, delivery_id: str) -> TeamsSendPlan:
        core = self._core()
        self.check_installation(core)
        owner = _check(self.resolve_send(delivery_id), TeamsSendContext)
        plan_key = control_key("teams-send-plan", delivery_id)
        outbox_key = control_key("delivery-outbox", delivery_id)
        initial = authority_snapshot(self.store, (outbox_key, plan_key))
        if initial[plan_key] is not None:
            plan = self.plan(delivery_id)
            rows = authority_snapshot(self.store, self._keys(plan.binding, owner))
            _, current, _ = self._delivery(rows, delivery_id, owner)
            self._route_access(rows, current.binding, owner)
            if current.binding != plan.binding:
                raise Problem(409, "TEAMS_PLAN_CHANGED", "saved plan no longer binds the current outbox")
            self.operations.prepare(ToolIntent(operation_id=plan.operation_id, input_ref=delivery_id), owner.context)
            return plan
        outbox = core._row(initial, outbox_key, _Outbox)
        keys = (*self._keys(outbox.binding, owner), plan_key, *self.installation_keys())
        rows = authority_snapshot(self.store, keys)
        _, box, payload = self._delivery(rows, delivery_id, owner)
        route, _ = self._route_access(rows, box.binding, owner)
        body = self._body(payload, route)
        description = OperationDescription(
            adapter_id=self.contract.adapter_id, adapter_version=TEAMS_VERSION,
            kind="request", method="POST", target=route.target(), content=body,
            resources=(ResolvedResource(resource_id=route.route_id, version=TEAMS_VERSION),),
        )
        plan = TeamsSendPlan(binding=box.binding, route=route, operation_id=self.operation_id(delivery_id),
                             description=description, evidence_mode=core.registration.mode)
        request = ControlRequest(request_id=control_key("teams-prepare", delivery_id),
                                 request_digest=digest(plan.model_dump(mode="json")), read_set=keys)

        def decide(snapshot):
            self.check_installation(core, snapshot)
            ob, current, _ = self._delivery(snapshot, delivery_id, owner)
            current_route, _ = self._route_access(snapshot, current.binding, owner)
            if ob.fulfillment != "pending" or current.binding != plan.binding or current_route != route:
                raise Problem(409, "TEAMS_PLAN_CHANGED", "outbox or route changed while preparing")
            data = plan.model_dump(mode="json")
            return ControlChanges(writes=(ControlWrite(
                row_id=plan_key, mode="create", data=data, material=_material(data),
            ),), result={"plan_id": delivery_id, "status": "not_sent"})

        self.store.transact(request, decide)
        self.operations.prepare(ToolIntent(operation_id=plan.operation_id, input_ref=delivery_id), owner.context)
        return self.plan(delivery_id)

    def _send_row(self, delivery_id):
        key = control_key("teams-send", delivery_id)
        rows = authority_snapshot(self.store, (key,))
        return None if rows[key] is None else self._core()._row(rows, key, _Send)

    def _claim(self, plan, owner):
        key = control_key("teams-send", plan.binding.delivery_id)
        request = ControlRequest(request_id=control_key("teams-claim", plan.binding.delivery_id),
                                 request_digest=digest([plan.description_digest, owner.model_dump(mode="json")]),
                                 read_set=(key,))
        fresh = False
        send = _Send(
            delivery_id=plan.binding.delivery_id, operation_id=plan.operation_id,
            attempt_id=control_key("teams-attempt", plan.binding.delivery_id),
            owner=owner, description_digest=plan.description_digest, claim_request=request,
        )

        def decide(rows):
            nonlocal fresh
            fresh = True
            if rows[key] is not None:
                raise Problem(409, "TEAMS_SEND_UNKNOWN", "the original send is already owned")
            return ControlChanges(writes=(ControlWrite(row_id=key, mode="create", data=send.model_dump(mode="json")),),
                                  result={"status": "claimed_unknown", "send_permission": False})
        self.store.transact(request, decide)
        if not fresh:
            raise Problem(409, "TEAMS_SEND_UNKNOWN", "historical ownership is not another send permission")
        return send

    def _consume(self, plan, send, command):
        core, context = self._core(), send.owner.context
        key = control_key("teams-send", send.delivery_id)
        base = self.operations.reconciliation_request(command, context)
        request = ControlRequest(
            request_id=control_key("teams-consume", send.delivery_id),
            request_digest=digest([base.model_dump(mode="json"), plan.model_dump(mode="json")]),
            read_set=tuple(sorted(set((*base.read_set, *self._keys(plan.binding, send.owner), key,
                                      control_key("teams-send-plan", send.delivery_id), *self.installation_keys())))),
        )
        consumed = None

        def decide(rows):
            nonlocal consumed
            native = self.check_installation(core, rows)
            current = core._row(rows, key, _Send)
            if current != send or current.phase != "claimed":
                raise Problem(409, "TEAMS_SEND_UNKNOWN", "original send ownership differs")
            ob, box, _ = self._delivery(rows, send.delivery_id, send.owner)
            route, access = self._route_access(rows, box.binding, send.owner)
            plan_key = control_key("teams-send-plan", send.delivery_id)
            if (core._row(rows, plan_key, TeamsSendPlan) != plan
                    or _parse(rows[plan_key].payload["material"], InlineMaterial) != _material(plan.model_dump(mode="json"))
                    or box.binding != plan.binding or route != plan.route or ob.fulfillment != "pending"):
                raise Problem(409, "TEAMS_PLAN_CHANGED", "approved payload or route changed before the final gate")
            self.operations._owner(rows, context, plan.operation_id)
            operation, material, description = self.operations._operation(rows, plan.operation_id, context)
            attempt = core._row(rows, control_key("attempt", plan.operation_id, command.attempt_id), _Attempt)
            grant = core._row(rows, control_key("grant", command.grant_id), GrantRecord)
            policy = core._row(rows, control_key("policy", context.policy_id), PolicyRecord)
            budget = core._row(rows, control_key("budget", context.root_budget_id), BudgetRecord)
            now = _utc(self.clock())
            if (description != plan.description or material.sha256 != plan.description_digest
                    or operation.current_attempt_id != command.attempt_id or operation.effect_state != "unknown"
                    or operation.unsettled_attempt_ids != (command.attempt_id,) or attempt.context != context
                    or attempt.binding.description_digest != plan.description_digest
                    or attempt.binding.operation_id != plan.operation_id or attempt.binding.attempt_id != command.attempt_id
                    or attempt.binding.target != description.target or attempt.binding.adapter_contract_digest != self.contract_digest
                    or attempt.observation_sequence or attempt.effect_state != "unknown"
                    or attempt.grant_id != grant.grant_id or grant.grant_id != command.grant_id or grant.revoked
                    or grant.revocation_revision != attempt.grant_revocation_revision
                    or grant.description_digest != material.sha256 or grant.adapter_contract_digest != self.contract_digest
                    or grant.root_budget_id != context.root_budget_id
                    or GrantSubject(principal_id=context.principal_id, conversation_id=context.conversation_id) not in grant.subjects
                    or grant.policy_id != context.policy_id or policy.policy_id != context.policy_id or not policy.active
                    or grant.policy_revision != policy.revision or policy.revision != attempt.policy_revision
                    or not grant.not_before <= now < min(grant.expires_at, attempt.not_after)
                    or now < attempt.authorized_at or not budget.active or budget.root_budget_id != context.root_budget_id
                    or attempt.reserved_units != self.contract.reservation_units):
                raise Problem(403, "TEAMS_FINAL_GATE_DENIED", "current approval, owner, epoch or reserved attempt is invalid")
            consumed = _Send.model_validate({
                **send.model_dump(), "phase": "consumed", "gate_request": request, "gated_at": now,
                "not_after": min(grant.expires_at, attempt.not_after, now + timedelta(seconds=2),
                                 self.cleanup_authority.expires_at if self.cleanup_authority is not None else
                                 min(native.expires_at, native.readiness.expires_at) if native else grant.expires_at),
                "access_revision": access.revision,
                "native_evidence_id": None if native is None else native.evidence_id,
                "native_evidence_digest": None if native is None else digest(native.model_dump(mode="json")),
                "native_installation_data_sha256": None if native is None else native.installation_data_sha256,
            })
            return ControlChanges(writes=(ControlWrite(row_id=key, mode="replace", data=consumed.model_dump(mode="json")),),
                                  result={"status": "delivery_unknown", "send_permission": False,
                                          "send_digest": digest(consumed.model_dump(mode="json"))})
        options = {}
        if self.cleanup_authority is not None:
            options["decision_final_check"] = lambda: self.cleanup_authority.check_consumption_deadline(consumed)
        receipt = self.store.transact(request, decide, **options)
        if consumed is None:
            raise Problem(409, "TEAMS_SEND_UNKNOWN", "historical consumption is not another send permission")
        if self.cleanup_authority is not None:
            self.cleanup_authority.record_consumed(receipt, consumed)
        return consumed

    def _warm_token(self):
        try:
            if self._core().registration.mode == "native":
                lease = self.bot_token.acquire(BOT_SCOPE)
                return lease.token, lease.expires_at
            token = self.bot_token(BOT_SCOPE)
            if not isinstance(token, str) or not 0 < len(token) <= 32768 or any(c.isspace() for c in token):
                raise ValueError
            return token, None
        except Exception:
            return None, None

    def _wire_installation(self, send):
        try:
            if self.cleanup_authority is not None:
                self.cleanup_authority.check_wire(send)
                return
            if getattr(self._core(), "shared_channels", None) is not None:
                self.core.shared_channels.check_current(self.core)
            if self._core().registration.mode == "native":
                self.native.check_wire(send)
        except Exception:
            raise Problem(403, "TEAMS_SEND_GATE_DENIED", "native installation changed after consumption") from None

    def _http(self, plan, send, token, token_expires_at):
        status, provider_id = None, None
        problem_code = "TEAMS_TRANSPORT_UNKNOWN"
        try:
            if token is None:
                raise Problem(502, "TEAMS_CREDENTIAL_UNAVAILABLE", "registered bot credentials are unavailable") from None
            transport = _OneSend(
                self.transport or PublicCloudTransport(), plan, send, self.clock,
                check_current=lambda: self._wire_installation(send), token_expires_at=token_expires_at,
            )
            with httpx.Client(transport=transport, follow_redirects=False, trust_env=False, timeout=10) as client:
                started = time.monotonic()
                with client.stream("POST", plan.description.target, content=plan.description.content,
                                   headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}) as response:
                    status = response.status_code
                    problem_code = "TEAMS_REDIRECT_DENIED" if 300 <= status < 400 else "TEAMS_HTTP_UNCONFIRMED"
                    if status in (200, 201):
                        problem_code = "TEAMS_PROVIDER_RESPONSE_INVALID"
                        if response.headers.get("content-type", "").split(";")[0] != "application/json":
                            raise ValueError
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            data.extend(chunk)
                            if len(data) > 8192 or time.monotonic() - started > 10:
                                raise ValueError
                        value = json.loads(data)
                        candidate = value.get("id") if isinstance(value, dict) else None
                        if (isinstance(candidate, str) and 0 < len(candidate) <= 4096
                                and not any(c.isspace() for c in candidate)
                                and candidate != plan.binding.delivery_id):
                            checked_human_text(candidate.encode("utf-8"))
                            if candidate == token or token in candidate:
                                raise ValueError
                            provider_id = candidate
                            problem_code = None
        except Problem as error:
            provider_id = None
            code = error.body["error"]["code"]
            problem_code = code if code in {"TEAMS_CREDENTIAL_UNAVAILABLE", "TEAMS_SEND_GATE_DENIED"} else "TEAMS_TRANSPORT_UNKNOWN"
        except (httpx.HTTPError, OSError, ValueError, TypeError, RecursionError):
            provider_id = None
        return _ProviderReceipt(
            binding=plan.binding, operation_id=plan.operation_id, attempt_id=send.attempt_id,
            request_id=send.gate_request.request_id, description_digest=plan.description_digest,
            target=plan.description.target, body_digest=hashlib.sha256(plan.description.content).hexdigest(),
            observed_at=_utc(self.clock()), http_status=status, provider_activity_id=provider_id,
            evidence_mode="connector_http" if plan.evidence_mode == "native" else "offline_contract",
            native_evidence_id=send.native_evidence_id, native_evidence_digest=send.native_evidence_digest,
            problem_code=problem_code,
        )

    def _save_provider(self, proof):
        key = control_key("teams-provider", proof.binding.delivery_id)
        data = proof.model_dump(mode="json")
        request = ControlRequest(request_id=control_key("teams-provider-receipt", proof.binding.delivery_id),
                                 request_digest=digest(data), read_set=(key,))
        return self.store.transact(request, lambda _: ControlChanges(
            writes=(ControlWrite(row_id=key, mode="create", data=data, material=_material(data)),),
            result={"status": "receipt_retained", "actual_environment": "NOT_RUN"},
        ))

    def _provider(self, delivery_id):
        plan = self.plan(delivery_id)
        send = self._send_row(delivery_id)
        proof = retained(self.store, control_key("teams-provider", delivery_id), _ProviderReceipt)
        if (send is None or send.phase != "consumed" or send.gate_request is None
                or send.description_digest != plan.description_digest or proof.binding != plan.binding
                or proof.operation_id != plan.operation_id or proof.attempt_id != send.attempt_id
                or proof.request_id != send.gate_request.request_id
                or proof.description_digest != plan.description_digest or proof.target != plan.description.target
                or proof.body_digest != hashlib.sha256(plan.description.content).hexdigest()
                or proof.observed_at < send.gated_at
                or proof.evidence_mode != ("connector_http" if plan.evidence_mode == "native" else "offline_contract")
                or proof.native_evidence_id != send.native_evidence_id
                or proof.native_evidence_digest != send.native_evidence_digest
                or proof.provider_activity_id is not None and (proof.http_status not in (200, 201) or proof.problem_code is not None)
                or self.store.reconcile(send.gate_request).payload["result"]["send_digest"] != digest(send.model_dump(mode="json"))):
            raise Problem(403, "TEAMS_RECEIPT_DENIED", "HTTP receipt does not bind the original consumed request")
        if plan.evidence_mode == "native":
            if proof.native_evidence_id is None or proof.native_evidence_digest is None:
                raise Problem(403, "TEAMS_RECEIPT_DENIED", "native receipt installation provenance is absent")
            evidence = retained(self.store, control_key("teams-native-evidence", proof.native_evidence_id), TeamsNativeEvidence)
            if (digest(evidence.model_dump(mode="json")) != proof.native_evidence_digest
                    or evidence.configuration_digest != self._core().configuration_digest()
                    or evidence.control_binding != self._core().contract.control_binding
                    or evidence.settings_digest != digest(self.settings.model_dump(mode="json"))
                    or evidence.installation_data_sha256 != send.native_installation_data_sha256
                    or self.cleanup_authority is None and (
                        not evidence.observed_at <= send.gated_at < evidence.expires_at
                        or not evidence.readiness.observed_at <= send.gated_at < evidence.readiness.expires_at)):
                raise Problem(403, "TEAMS_RECEIPT_DENIED", "native receipt installation provenance differs")
            if self.cleanup_authority is not None:
                self.cleanup_authority.check()
                auth = self.cleanup_authority.authorization
                if not auth.approved_at <= send.gated_at <= proof.observed_at < auth.expires_at:
                    raise Problem(403, "TEAMS_RECEIPT_DENIED", "original cleanup receipt is outside its independent approval")
        return plan, send, proof

    def verify_receipt(self, evidence_id):
        # Handles are delivery IDs, resolved ONLY against retained Adapter evidence.
        plan, send, proof = self._provider(evidence_id)
        return VerifiedDeliveryReceipt(
            evidence_id=evidence_id, verifier_version=self._core().contract.verifier_version,
            binding=plan.binding, send_outcome="emitted" if proof.provider_activity_id is not None else "unknown",
            confirmed_levels=("provider_accepted",) if proof.provider_activity_id is not None else (),
            dispatch_authorization_ref=send.gate_request.request_id,
            proof=_json(proof.model_dump(mode="json")),
        )

    def _outcome(self, evidence_id):
        plan, send, proof = self._provider(evidence_id)
        key = control_key("attempt", plan.operation_id, send.attempt_id)
        attempt = self._core()._row(authority_snapshot(self.store, (key,)), key, _Attempt)
        accepted = proof.provider_activity_id is not None
        return VerifiedOutcome(
            evidence_id=evidence_id, source="teams-http", contract_version=TEAMS_VERSION,
            binding=attempt.binding, observation="applied" if accepted else "unknown", sequence=1,
            observed_at=proof.observed_at, proof=_json(proof.model_dump(mode="json")),
            business_result="succeeded" if accepted else "unknown",
            result=ReceiptResult(receipt_id=evidence_id, resource=plan.description.resources[0], status="completed") if accepted else None,
        )

    def send(self, delivery_id: str) -> Record:
        self._core()
        if self._send_row(delivery_id) is not None:
            return self.reconcile(delivery_id)
        plan = self.prepare(delivery_id)
        owner = _check(self.resolve_send(delivery_id), TeamsSendContext)
        # A crashed claimant is never replaced, even if no POST can be observed.
        send = self._claim(plan, owner)
        command = AuthorizationRequest(operation_id=plan.operation_id, attempt_id=send.attempt_id, grant_id=owner.grant_id)
        self.operations.authorize(command, owner.context)
        # MI cold start happens BEFORE the two-second gate; current authorization
        # is re-read by _consume after acquisition. Tokens never enter the ledger.
        token, token_expires_at = self._warm_token()
        consumed = self._consume(plan, send, command)
        proof = self._http(plan, consumed, token, token_expires_at)
        self._save_provider(proof)
        return self.reconcile(delivery_id)

    def reconcile(self, delivery_id: str) -> Record:
        core = self._core()
        send = self._send_row(delivery_id)
        if send is None or send.phase != "consumed" or send.gate_request is None:
            raise Problem(409, "TEAMS_SEND_UNKNOWN", "no complete consumed send evidence; hold the original attempt without POST")
        key = control_key("teams-provider", delivery_id)
        if authority_snapshot(self.store, (key,))[key] is None:
            raise Problem(409, "TEAMS_SEND_UNKNOWN", "original HTTP receipt is absent or in flight; hold without POST",
                          operation_id=send.operation_id, attempt_id=send.attempt_id)
        owner = _check(self.resolve_send(delivery_id), TeamsSendContext)
        self.operations.record_outcome(OutcomeRequest(operation_id=send.operation_id, attempt_id=send.attempt_id,
                                                     evidence_id=delivery_id), owner.context)
        return core.record_receipt(ReceiptRequest(obligation_id=self.plan(delivery_id).binding.obligation_id,
                                                 delivery_id=delivery_id, evidence_id=delivery_id), owner.context)

    def inspect(self, delivery_id: str) -> dict:
        plan = self.plan(delivery_id)
        send = self._send_row(delivery_id)
        provider_key = control_key("teams-provider", delivery_id)
        if send is None or authority_snapshot(self.store, (provider_key,))[provider_key] is None:
            return {
                "delivery_id": delivery_id, "operation_id": plan.operation_id,
                "attempt_id": None if send is None else send.attempt_id,
                "provider_activity_id": None, "http_status": None, "confirmation_level": None,
                "send_outcome": None if send is None else "unknown",
                "problem_code": None if send is None else "TEAMS_SEND_UNKNOWN",
                "actual_environment": "NOT_RUN",
            }
        plan, send, proof = self._provider(delivery_id)
        return {"delivery_id": delivery_id, "operation_id": plan.operation_id, "attempt_id": send.attempt_id,
                "provider_activity_id": proof.provider_activity_id, "http_status": proof.http_status,
                "send_outcome": "emitted" if proof.provider_activity_id else "unknown",
                "confirmation_level": "provider_accepted" if proof.provider_activity_id else None,
                "problem_code": proof.problem_code,
                "evidence_mode": proof.evidence_mode,
                "native_evidence_id": proof.native_evidence_id,
                "native_evidence_digest": proof.native_evidence_digest,
                "actual_environment": "NOT_RUN"}
