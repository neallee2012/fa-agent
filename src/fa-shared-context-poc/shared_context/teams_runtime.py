"""Teams producer/consumer bridge into the existing BehaviorStore authority.

Composition (trusted server only):
  bridge = TeamsRuntime(store, identity, clock=..., resolve_admission=...)
  registration = replace(base, verify_ingress=bridge.verify_ingress,
                         channel_adapter=teams_adapter)
  core = DeliveryCore(store, clock=..., registration=registration)
  bridge.attach(core); teams_adapter.attach(core)
  app.include_router(teams_router(bridge))

resolve_admission chooses the existing server-owned context/root/work contract.
It never receives JWTs, untrusted owner/grant fields or a delegated approval.
process_work, if installed, asks the parent to consume the original durable work
intent using its existing idempotent owner/job/AssistantEngine seam. It must not
blindly invoke a model on each callback. prepare() then uses the verified saved
result; consume()/drain() call only the registered guarded sender.

Immutable teams-source records protect pre-admission plans and original requests,
not a second work/obligation/outbox ledger. At most 32 source plans per entry are
retained in this initial no-GC profile; no automatic counter reset/tombstone GC.
Failed staging is not admission. Unknown commits must use the original request.
Native enablement remains CLOSED until the parent installs proven prerequisites;
native composition uses TeamsDeliveryCore to fence the original admission EGT.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .behavior_delivery import (
    AdmissionRequest, DeliveryConsumer, DeliveryCore, IncomingEvent, Route,
    VerifiedIngress, WorkDefinition, _Model, _Obligation, _check, _json, _material,
    _parse, _utc, admission_digest, authority_snapshot,
)
from .behavior_operations import ExecutionContext, PolicyRecord, control_key
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest, ControlWrite, InlineMaterial
from .errors import Problem
from .models import Identifier
from .teams_identity import ConnectorIdentity, TEAMS_VERSION, TeamsRouteRecord, VerifiedTeamsActivity, digest


class TeamsAdmission(_Model):
    context: ExecutionContext
    root_request_id: Identifier
    work: WorkDefinition


class _Source(_Model):
    body_digest: str
    route_id: str
    provider_activity_id: str
    command: AdmissionRequest
    context: ExecutionContext
    evidence: VerifiedIngress
    request: ControlRequest


def retained(store: BehaviorStore, key: str, model):
    row = authority_snapshot(store, (key,))[key]
    if row is None:
        raise Problem(409, "TEAMS_RECORD_MISSING", "required retained Teams evidence is missing")
    value = _parse(row.payload["data"], model)
    if _parse(row.payload["material"], InlineMaterial) != _material(value.model_dump(mode="json")):
        raise Problem(409, "TEAMS_RECORD_CHANGED", "Teams evidence differs from its immutable material")
    return value


class TeamsRuntime:
    def __init__(self, store: BehaviorStore, identity: ConnectorIdentity, *,
                 clock: Callable[[], datetime],
                 resolve_admission: Callable[[VerifiedTeamsActivity], TeamsAdmission],
                 resolve_context: Callable[[str], ExecutionContext] | None = None,
                 process_work: Callable[[str, ExecutionContext], None] | None = None):
        self.store, self.identity, self.clock = store, identity, clock
        self.resolve_admission, self.resolve_context = resolve_admission, resolve_context
        self.process_work = process_work
        self.core: DeliveryCore | None = None
        self.admission_enabled = False

    def attach(self, core: DeliveryCore):
        if (core.store is not self.store or core.contract is None
                or core.contract.channel.channel_id != "msteams"
                or core.contract.channel.version != TEAMS_VERSION
                or core.contract.team_id != self.identity.settings.team_id
                or core.contract.channel.supported_levels != ("provider_accepted",)
                or core.contract.channel.max_confirmation_level != "provider_accepted"):
            raise Problem(403, "TEAMS_REGISTRATION_DENIED", "Teams must use the exact registered authority and channel contract")
        self.core = core

    def _core(self) -> DeliveryCore:
        from .teams_delivery import TeamsDeliveryAdapter

        if self.core is None:
            raise Problem(503, "TEAMS_NOT_ATTACHED", "Teams has no registered delivery authority")
        registration, _ = self.core._enabled()
        adapter = registration.channel_adapter
        if type(adapter) is not TeamsDeliveryAdapter or adapter.settings != self.identity.settings:
            raise Problem(403, "TEAMS_REGISTRATION_DENIED", "ingress and sender must share the exact registered identity/route profile")
        adapter.check_structure(self.core)
        return self.core

    def route(self, route_id: str) -> TeamsRouteRecord:
        route = retained(self.store, control_key("teams-route", route_id), TeamsRouteRecord)
        if route.route_id != route_id:
            raise Problem(409, "TEAMS_ROUTE_CHANGED", "retained route identity differs")
        return route

    def enable_admission(self) -> None:
        core = self._core()
        proof = core._readiness()
        if not proof.observed_at <= _utc(self.clock()) < proof.expires_at:
            raise Problem(403, "TEAMS_ADMISSION_DISABLED", "current installed readiness has expired")
        self.admission_enabled = True

    def verify_ingress(self, evidence_id: str) -> VerifiedIngress:
        source = retained(self.store, control_key("teams-source", evidence_id), _Source)
        if (source.evidence.evidence_id != evidence_id
                or source.evidence.admission_digest != admission_digest(source.command)
                or source.command.route.thread_id != source.route_id):
            raise Problem(403, "TEAMS_SOURCE_CONFLICT", "retained source binding differs")
        self.route(source.route_id)
        if self._core().registration.mode == "native":
            from .teams_native import TeamsNativeEvidence
            try:
                proof = json.loads(source.evidence.proof)
                native_id = proof["native_evidence_id"]
                native = retained(self.store, control_key("teams-native-evidence", native_id), TeamsNativeEvidence)
                if (proof["evidence_mode"] != "connector_http"
                        or proof["native_evidence_digest"] != digest(native.model_dump(mode="json"))
                        or native.configuration_digest != self.core.configuration_digest()
                        or native.control_binding != self.core.contract.control_binding
                        or native.settings_digest != digest(self.identity.settings.model_dump(mode="json"))
                        or not native.observed_at <= source.evidence.observed_at < native.expires_at):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise Problem(403, "TEAMS_SOURCE_CONFLICT", "native source must retain its original installation provenance") from None
        return source.evidence

    def receive(self, authorization: str, body: bytes) -> dict:
        core = self._core()
        adapter = core.registration.channel_adapter
        adapter.check_installation(core)
        if not 0 < len(body) <= 32768:
            raise Problem(413, "TEAMS_BODY_LIMIT", "Teams input exceeds the fixed byte bound")
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            activity = json.loads(body, object_pairs_hook=unique,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(activity, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            raise Problem(422, "TEAMS_BODY_INVALID", "a bounded JSON activity without duplicate fields is required") from None
        if core.registration.mode == "native" and activity.get("type") == "message":
            # The observed personal client includes inert clientInfo/HTML mirrors.
            # Preserve original bytes as evidence; never execute or add HTML text.
            from .teams_handshake import normalize_handshake
            activity = normalize_handshake(activity, activity.get("text"))
        incoming = self.identity.verify(authorization, activity, body_digest=hashlib.sha256(body).hexdigest())
        adapter.check_installation(core)
        incoming.principal.require(core.contract.team_id, "submit")
        if incoming.disposition == "silence":
            return {"status": "silence", "reason": "transport_event_no_work", "actual_environment": "NOT_RUN"}
        source_key = control_key("teams-source", incoming.event_id)
        previous = authority_snapshot(self.store, (source_key,))[source_key]
        if previous is not None:
            source = retained(self.store, source_key, _Source)
            if source.body_digest != incoming.body_digest or source.route_id != incoming.route.route_id:
                raise Problem(409, "TEAMS_SOURCE_CONFLICT", "provider activity ID is already bound to different bytes or route")
            if not self.admission_enabled:
                return self.store.reconcile(source.request).payload["result"]
            return core.admit(source.command, source.context).payload["result"]
        if not self.admission_enabled:
            raise Problem(403, "TEAMS_ADMISSION_DISABLED", "new Teams admission is disabled; existing work remains drainable")
        core._readiness()
        admission = _check(self.resolve_admission(incoming), TeamsAdmission)
        context = admission.context
        policy_key = control_key("policy", context.policy_id)
        conv_key = control_key("delivery-conversation", context.conversation_id)
        counter_key = control_key("teams-source-capacity", core.contract.entry_id)
        route_key = control_key("teams-route", incoming.route.route_id)
        root_key = control_key("delivery-quota", "root", context.root_budget_id)
        keys = (source_key, route_key, counter_key, policy_key, conv_key, root_key, *adapter.installation_keys())
        request = ControlRequest(
            request_id=control_key("teams-stage-source", incoming.event_id),
            request_digest=digest([incoming.body_digest, incoming.route.route_id, core.configuration_digest()]),
            read_set=keys,
        )

        def stage(rows):
            native = adapter.check_installation(core, rows)
            count = 0 if rows[counter_key] is None else rows[counter_key].payload["data"]["count"]
            if type(count) is not int or not 0 <= count < 32:
                raise Problem(429, "TEAMS_SOURCE_CAPACITY", "the finite retained-source quota is exhausted")
            root = rows[root_key]
            if (root is None or root.payload["data"]["pending"] >= root.payload["data"]["pending_limit"]
                    or root.payload["data"]["root_request_id"] != admission.root_request_id):
                raise Problem(429, "TEAMS_SOURCE_CAPACITY", "root admission capacity is unavailable")
            policy = core._row(rows, policy_key, PolicyRecord)
            if not policy.active:
                raise Problem(403, "TEAMS_POLICY_DENIED", "current policy does not permit intake")
            sequence = 0 if rows[conv_key] is None else rows[conv_key].payload["data"]["sequence"]
            now = _utc(self.clock())
            command = AdmissionRequest(
                event=IncomingEvent(event_id=incoming.event_id, root_request_id=admission.root_request_id,
                                    conversation_id=context.conversation_id, content=incoming.text),
                work=admission.work, route=Route(
                    tenant_id=incoming.route.tenant_id, account_id=incoming.route.bot_app_id,
                    recipient_id=incoming.route.aad_object_id, channel_id="msteams",
                    thread_id=incoming.route.route_id,
                ),
                required_confirmation_level=(
                    core.shared_channels.config.channels.required_confirmation_level
                    if getattr(core, "shared_channels", None) is not None else "provider_accepted"
                ),
                deadline=now + timedelta(seconds=core.contract.max_queue_seconds),
                expected_sequence=sequence, ingress_evidence_id=incoming.event_id,
            )
            proof = {"contract": TEAMS_VERSION, "authentication": "connector_service_only",
                     "route_id": incoming.route.route_id, "provider_body_digest": incoming.body_digest,
                     "delegated_approval": False}
            if native is not None:
                proof.update(evidence_mode="connector_http", native_evidence_id=native.evidence_id,
                             native_evidence_digest=digest(native.model_dump(mode="json")))
            evidence = VerifiedIngress(
                evidence_id=incoming.event_id, admission_digest=admission_digest(command),
                verifier_version=core.contract.verifier_version, actor_id=incoming.route.aad_object_id,
                on_behalf_of_principal_id=context.principal_id, source_kind="human",
                policy_revision=policy.revision, observed_at=now, expires_at=command.deadline,
                proof=_json(proof),
            )
            source = _Source(
                body_digest=incoming.body_digest, route_id=incoming.route.route_id,
                provider_activity_id=incoming.provider_activity_id, command=command, context=context,
                evidence=evidence, request=core.request_for(command, context),
            )
            writes = [
                ControlWrite(row_id=source_key, mode="create", data=source.model_dump(mode="json"),
                             material=_material(source.model_dump(mode="json"))),
                ControlWrite(row_id=counter_key, mode="create" if rows[counter_key] is None else "replace",
                             data={"count": count + 1}),
            ]
            if rows[route_key] is None:
                data = incoming.route.model_dump(mode="json")
                writes.append(ControlWrite(row_id=route_key, mode="create", data=data, material=_material(data)))
            elif (_parse(rows[route_key].payload["data"], TeamsRouteRecord) != incoming.route
                  or _parse(rows[route_key].payload["material"], InlineMaterial)
                  != _material(incoming.route.model_dump(mode="json"))):
                raise Problem(409, "TEAMS_ROUTE_CHANGED", "an existing route cannot be replaced")
            return ControlChanges(writes=tuple(writes), result={"status": "not_admitted", "event_id": incoming.event_id})

        self.store.transact(request, stage)
        source = retained(self.store, source_key, _Source)
        return core.admit(source.command, source.context).payload["result"]

    def context(self, obligation_id: str) -> ExecutionContext:
        core = self._core()
        key = control_key("delivery-obligation", obligation_id)
        ob = core._row(authority_snapshot(self.store, (key,)), key, _Obligation)
        if self.resolve_context is not None:
            return _check(self.resolve_context(ob.owner_record_id), ExecutionContext)
        return retained(self.store, control_key("teams-source", ob.event_id), _Source).context

    def inspect(self, obligation_id: str) -> dict:
        context = self.context(obligation_id)
        keys = (control_key("delivery-obligation", obligation_id),
                control_key("owner", context.owner_record_id),
                control_key("delivery-conversation", context.conversation_id))
        return self._core()._obligation(authority_snapshot(self.store, keys), obligation_id, context).model_dump(mode="json")

    def prepare(self, obligation_id: str, worker_id: str = "teams-sender") -> dict:
        core = self._core()
        core.registration.channel_adapter.check_installation(core)
        context = self.context(obligation_id)
        self.inspect(obligation_id)
        key = control_key("delivery-work-state", obligation_id)
        row = authority_snapshot(self.store, (key,))[key]
        if row is None:
            raise Problem(409, "TEAMS_WORK_MISSING", "durable admitted work is missing")
        if row.payload["data"]["state"] == "queued" and self.process_work is not None:
            self.process_work(obligation_id, context)
        return DeliveryConsumer(self._core()).consume(obligation_id, context, worker_id)

    def _deliver(self, result: dict) -> dict:
        core = self._core()
        if result.get("status") == "fulfilled":
            return result
        delivery_id = result.get("delivery_id") or result.get("binding", {}).get("delivery_id")
        if delivery_id is None:
            raise Problem(409, "TEAMS_OUTBOX_NOT_READY", "no immutable outbox is available")
        receipt = core.send(delivery_id)
        return {**receipt.payload["result"],
                "problem_code": core.registration.channel_adapter.inspect(delivery_id)["problem_code"]}

    def consume(self, obligation_id: str, worker_id: str = "teams-sender") -> dict:
        ob = self.inspect(obligation_id)
        delivery_id = ob.get("final_delivery_id")
        adapter = self._core().registration.channel_adapter
        if delivery_id is not None and adapter._send_row(delivery_id) is not None:
            return self._deliver({"delivery_id": delivery_id})
        return self._deliver(self.prepare(obligation_id, worker_id))

    def drain(self, root_budget_id: str, worker_id: str = "teams-sender", *, limit: int = 4) -> tuple[dict, ...]:
        if self.resolve_context is None:
            raise Problem(503, "TEAMS_RESOLVER_MISSING", "bounded drain requires the server current-owner resolver")
        self._core().registration.channel_adapter.check_installation(self._core())
        prepared = DeliveryConsumer(self._core()).drain(root_budget_id, self.resolve_context, worker_id, limit=limit)
        results = []
        for result in prepared:
            if "problem" in result:
                results.append(result)
                continue
            try:
                results.append(self._deliver(result))
            except Problem as error:
                results.append({"problem": error.body["error"]["code"], "actual_environment": "NOT_RUN"})
        return tuple(results)


async def activity_response(request: Request, target, limit_message: str) -> JSONResponse:
    """One JSON activity of at most 32768 bytes to target.receive; limit_message is each route's HTTP 413 text."""
    try:
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise Problem(415, "TEAMS_CONTENT_TYPE", "application/json is required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 32768:
                raise Problem(413, "TEAMS_BODY_LIMIT", limit_message)
        result = await run_in_threadpool(target.receive, request.headers.get("authorization", ""), bytes(body))
        return JSONResponse(result)
    except Problem as error:
        return JSONResponse(error.body, status_code=error.status)


def teams_router(runtime: TeamsRuntime) -> APIRouter:
    router = APIRouter()

    @router.post("/behavior/v1/teams/activities")
    async def activities(request: Request):
        return await activity_response(request, runtime, "Teams input exceeds the fixed byte bound")

    return router
