"""Authenticated pull delivery, not an outbound sender. actual_environment=NOT_RUN.

The server authenticates each HTTP client and mints a verified access handle.
Never deserialize VerifiedInboxAccess, deployment evidence, or ExecutionContext
from request JSON. fetch releases EXACT retained bytes after the current policy
gate; an exchange alone remains unknown. acknowledge requires the authenticated
client, server exchange challenge, exact digest/version/route and the sole
supported level, client_received. This proves a client's explicit acknowledgment,
not UI rendering or human reading. Challenges are not authentication credentials.

Integration: construct PullInbox -> RuntimeRegistration(channel_adapter=inbox)
-> DeliveryCore -> inbox.attach(core). Run DeliveryConsumer in a supervised
process; call reconcile for persisted acknowledgments after process loss.
All hooks verify pre-acquired evidence OUTSIDE store decision callbacks.
Native admission is closed until verified deployment evidence is persisted in
inbox-readiness(configuration_digest), including real protection/worker proofs.
No deletion/GC, network, Azure, email, Teams, or model calls are made here.

Public Interfaces: InboxRegistration, VerifiedInboxAccess, InboxDeploymentEvidence,
InboxFetch, InboxAcknowledgment, InboxDelivery; PullInbox.attach(core),
fetch(InboxFetch, ExecutionContext) -> InboxDelivery,
acknowledge(InboxAcknowledgment, ExecutionContext) -> Record,
reconcile(delivery_id, ExecutionContext) -> Record, verify_receipt(evidence_id).
publish_readiness(evidence_id) verifies and persists an independently acquired
deployment witness; it is server-only, not a native-evidence acquisition API.
HTTP integration MUST return InboxDelivery.payload as the saved bytes (not a
re-serialized object); convey binding/exchange/token metadata without claiming
delivery. Authentication is required again on every fetch and acknowledgment.
Never expose an endpoint accepting a Verified* model or ExecutionContext.

Persisted keys, via control_key:
  inbox-readiness(configuration_digest): current verified deployment pointer
  inbox-readiness-evidence(evidence_id): immutable deployment witness
  inbox-exchange(delivery_id): original read gate, challenge, unknown/ack state
  inbox-receipt-evidence(evidence_id): immutable server receipt
  inbox-ack-access(evidence_id): immutable authenticated acknowledgment + request
evidence_id is server-derived as control_key("inbox-ack-evidence", delivery_id).
Fetch and acknowledge also retain their original BehaviorStore request receipts.
Eight distinct fetch authorizations per delivery are supported; historical reads
still recheck current access. This limit cannot spend the acknowledgment lane.

Native installation order: obtain/verify retention and WorkerReadiness; persist
that evidence and initialize ONE WorkerCore pool; start supervised consumers and
the authenticated API with admissions closed; independently verify and retain
InboxDeploymentEvidence, including those worker/pool bindings; then enable native
admissions. Missing evidence keeps native closed. No such native setup was run.

An optional register_retained_results(RetainedResultSettlement) installs a
separate exact terminal-result gate. Its opaque permits share this exchange/ACK
engine, never ordinary access verification or worker readiness. No registration
changes fetch/acknowledge, admission, or deployment readiness.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator

from .behavior_delivery import (
    Content, Count, DeliveryBinding, DeliveryCore, ReadinessEvidence, ReceiptRequest,
    VerifiedDeliveryReceipt, _Model, _Obligation, _Outbox, _check, _digest, _material,
    _parse, _update, _utc, authority_snapshot,
)
from .behavior_operations import ExecutionContext, PolicyRecord, control_key
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest, Digest, InlineMaterial
from .errors import Problem
from .models import Identifier
from .records import Record

INBOX_CHANNEL_ID = "authenticated-pull"
INBOX_VERSION = "fa-pull-inbox-v1"


class InboxDeploymentEvidence(_Model):
    evidence_id: Identifier
    kind: Literal["inbox_deployment_evidence"] = "inbox_deployment_evidence"
    configuration_digest: Digest
    control_binding: Digest
    channel_version: Literal["fa-pull-inbox-v1"] = INBOX_VERSION
    authentication_version: Identifier
    protection_scope: Identifier
    protection_evidence_id: Identifier
    worker_evidence_id: Identifier
    worker_workspace_id: Identifier
    worker_configuration_digest: Digest
    readiness: ReadinessEvidence
    proof: Content


class VerifiedInboxAccess(_Model):
    evidence_id: Identifier
    principal_id: Identifier
    authentication_version: Identifier
    binding: DeliveryBinding
    action: Literal["fetch", "acknowledge"]
    policy_revision: Count
    observed_at: datetime
    expires_at: datetime
    proof: Content

    _times = field_validator("observed_at", "expires_at")(_utc)


class CurrentWorkInboxAccess(_Model):
    schema_version: Literal["fa-current-work-result-access-v1"] = "fa-current-work-result-access-v1"
    evidence_id: Identifier
    principal_id: Identifier
    authentication_version: Identifier
    binding: DeliveryBinding
    action: Literal["fetch", "acknowledge"]
    policy_revision: Count
    work_policy_id: Identifier
    runtime_binding_id: Identifier
    authenticated_at: datetime
    proof: Content
    _time = field_validator("authenticated_at")(_utc)


@dataclass(frozen=True)
class InboxRegistration:
    authentication_version: str
    verify_access: Callable[[str], VerifiedInboxAccess | CurrentWorkInboxAccess]
    verify_deployment: Callable[[str], InboxDeploymentEvidence] | None = None


class InboxFetch(_Model):
    obligation_id: Identifier
    delivery_id: Identifier
    access_evidence_id: Identifier


class InboxAcknowledgment(InboxFetch):
    exchange_id: Identifier
    acknowledgment_token: Identifier
    binding: DeliveryBinding
    confirmation_level: Literal["client_received"] = "client_received"


class InboxDelivery(_Model):
    exchange_id: Identifier
    acknowledgment_token: Identifier
    binding: DeliveryBinding
    payload: bytes
    supported_confirmation_level: Literal["client_received"] = "client_received"
    send_outcome: Literal["unknown"] = "unknown"
    actual_environment: Literal["NOT_RUN"] = "NOT_RUN"


class _Exchange(_Model):
    kind: Literal["inbox_exchange"] = "inbox_exchange"
    exchange_id: Identifier
    acknowledgment_token: Identifier
    binding: DeliveryBinding
    principal_id: Identifier
    fetch_request: ControlRequest
    authorization_evidence: VerifiedInboxAccess | CurrentWorkInboxAccess
    acknowledged: bool = False
    receipt_evidence_id: Identifier | None = None
    fetch_count: Annotated[int, Field(ge=1, le=8)] = 1


class _Acknowledged(_Model):
    kind: Literal["inbox_client_acknowledgment"] = "inbox_client_acknowledgment"
    exchange_id: Identifier
    access: VerifiedInboxAccess | CurrentWorkInboxAccess
    request: ControlRequest


def _ack_proof(exchange: _Exchange, acknowledged: _Acknowledged) -> bytes:
    return _material({
        "exchange_id": exchange.exchange_id,
        "acknowledgment_digest": acknowledged.request.request_digest,
        "authenticated_client_digest": _digest(acknowledged.access.model_dump(mode="json")),
    }).data


class PullInbox:
    def __init__(self, store: BehaviorStore, registration: InboxRegistration):
        self.store, self.registration = store, registration
        self.core: DeliveryCore | None = None
        self._retained_results = None
        self.work_policy = None

    def register_work_policy(self, control):
        from .behavior_work_policy import WorkPolicyControl
        if (type(control) is not WorkPolicyControl or control.runtime.dep.inbox is not self
                or control.store is not self.store or self.work_policy is not None):
            raise Problem(403,"INBOX_CURRENT_READ_DISABLED","one exact standing work authority is required")
        control.installed()
        self.work_policy=control

    def _current_access(self, access):
        if type(access) is CurrentWorkInboxAccess:
            if self.work_policy is None:
                raise Problem(403,"INBOX_CURRENT_READ_DISABLED","standing result read authority is not registered")
            self.work_policy.verify_result_access(access)
        elif not access.observed_at <= self._core()._now() < access.expires_at:
            raise Problem(403,"INBOX_ACCESS_DENIED","bounded original access permission expired")

    def register_retained_results(self, service) -> None:
        from .behavior_settlement import RetainedResultSettlement
        from .behavior_web_handoff import OriginalFailureWebHandoff

        if (type(service) not in {RetainedResultSettlement, OriginalFailureWebHandoff} or service.inbox is not self
                or service.registration is None or self._retained_results is not None):
            raise Problem(403, "SETTLEMENT_DISABLED", "one explicit retained-result verifier must own this inbox")
        self._retained_results = service

    def _retained_access(self, permit, command, context, action):
        from .behavior_settlement import _RetainedAccess
        from .behavior_web_handoff import _WebHandoffAccess, OriginalFailureWebHandoff

        if (type(permit) not in {_RetainedAccess, _WebHandoffAccess} or self._retained_results is None
                or permit.service is not self._retained_results
                or (type(permit) is _WebHandoffAccess) != (type(self._retained_results) is OriginalFailureWebHandoff)):
            raise Problem(403, "SETTLEMENT_DISABLED", "no registered terminal-result authorization")
        return self._retained_results._check_permit(permit, command, context, action)

    def fetch_retained(self, command: InboxFetch, context: ExecutionContext, permit) -> InboxDelivery:
        self._retained_access(permit, command, context, "fetch")
        return self._fetch(command, context, permit)

    def acknowledge_retained(self, command: InboxAcknowledgment, context: ExecutionContext, permit) -> Record:
        self._retained_access(permit, command, context, "acknowledge")
        return self._acknowledge(command, context, permit)

    def attach(self, core: DeliveryCore) -> None:
        if core.store is not self.store or core.registration is None:
            raise Problem(403, "INBOX_AUTHORITY_MISMATCH", "inbox must share the exact delivery authority")
        self.core = core

    def check_structure(self, core: DeliveryCore) -> None:
        config = core.contract
        if (
            self.core is not core or self.store is not core.store or config is None
            or config.channel.channel_id != INBOX_CHANNEL_ID
            or config.channel.version != INBOX_VERSION
            or config.channel.supported_levels != ("client_received",)
            or config.channel.max_confirmation_level != "client_received"
            or self.registration.verify_deployment is None
        ):
            raise Problem(403, "DELIVERY_NATIVE_DISABLED", "authenticated inbox/native prerequisites are not installed")

    def check_installation(self, core: DeliveryCore) -> None:
        self.check_structure(core)
        config = core.contract
        digest = core.configuration_digest()
        key = control_key("inbox-readiness", digest)
        row = authority_snapshot(self.store, (key,))[key]
        if row is None or row.payload["binding"] != config.control_binding:
            raise Problem(403, "DELIVERY_NATIVE_DISABLED", "persisted native readiness is missing")
        evidence = _parse(row.payload["data"], InboxDeploymentEvidence)
        verified = _check(self.registration.verify_deployment(evidence.evidence_id), InboxDeploymentEvidence)
        retained_key = control_key("inbox-readiness-evidence", evidence.evidence_id)
        retained = authority_snapshot(self.store, (retained_key,))[retained_key]
        if (
            verified != evidence or evidence.configuration_digest != digest
            or evidence.control_binding != config.control_binding
            or evidence.authentication_version != self.registration.authentication_version
            or evidence.readiness.configuration_digest != digest
            or evidence.readiness.control_binding != config.control_binding
            or evidence.readiness.protection_version != config.protection_version
            or evidence.readiness.consumer_version != config.consumer_version
            or evidence.readiness.timer_version != config.timer_version
            or not evidence.readiness.observed_at <= core._now() < evidence.readiness.expires_at
            or retained is None or retained.payload["binding"] != config.control_binding
            or _parse(retained.payload["data"], InboxDeploymentEvidence) != evidence
            or _parse(retained.payload["material"], InlineMaterial) != _material(evidence.model_dump(mode="json"))
        ):
            raise Problem(403, "DELIVERY_NATIVE_DISABLED", "native readiness/protection/worker binding is unverified")
        from .behavior_workers import WorkerReadiness, worker_key

        worker_key_id = worker_key("readiness", evidence.worker_workspace_id)
        pool_key = worker_key("pool", evidence.worker_workspace_id)
        workers = authority_snapshot(self.store, (worker_key_id, pool_key))
        worker_row, pool_row = workers[worker_key_id], workers[pool_key]
        if worker_row is None or pool_row is None:
            raise Problem(403, "DELIVERY_NATIVE_DISABLED", "persisted worker readiness/pool is missing")
        worker = _parse(worker_row.payload["data"], WorkerReadiness)
        worker_evidence_key = worker_key("readiness-evidence", worker.evidence_id)
        worker_evidence_row = authority_snapshot(self.store, (worker_evidence_key,))[worker_evidence_key]
        if (
            worker_row.payload["binding"] != config.control_binding or pool_row.payload["binding"] != config.control_binding
            or worker.evidence_id != evidence.worker_evidence_id
            or worker.configuration_digest != evidence.worker_configuration_digest
            or worker.control_binding != config.control_binding
            or worker.protection_version != config.protection_version
            or not worker.observed_at <= core._now() < worker.expires_at
            or worker_evidence_row is None or worker_evidence_row.payload["binding"] != config.control_binding
            or _parse(worker_evidence_row.payload["data"], WorkerReadiness) != worker
            or _parse(worker_evidence_row.payload["material"], InlineMaterial) != _material(worker.model_dump(mode="json"))
            or pool_row.payload["data"].get("configuration_digest") != worker.configuration_digest
            or pool_row.payload["data"].get("workspace_id") != evidence.worker_workspace_id
        ):
            raise Problem(403, "DELIVERY_NATIVE_DISABLED", "worker pool/readiness is not bound to this protected authority")

    def publish_readiness(self, evidence_id: str) -> Record:
        core = self._core()
        if core.registration.mode != "native" or self.registration.verify_deployment is None:
            raise Problem(403, "DELIVERY_NATIVE_DISABLED", "independent native deployment verification is missing")
        proof = _check(self.registration.verify_deployment(evidence_id), InboxDeploymentEvidence)
        if (
            proof.evidence_id != evidence_id or proof.configuration_digest != core.configuration_digest()
            or proof.control_binding != core.contract.control_binding
            or proof.authentication_version != self.registration.authentication_version
            or proof.readiness.configuration_digest != core.configuration_digest()
            or proof.readiness.control_binding != core.contract.control_binding
        ):
            raise Problem(403, "DELIVERY_NATIVE_DISABLED", "deployment witness binding differs")
        key = control_key("inbox-readiness", core.configuration_digest())
        request = ControlRequest(
            request_id=control_key("inbox-publish-readiness", evidence_id),
            request_digest=_digest(proof.model_dump(mode="json")), read_set=(key,),
        )

        def decide(rows):
            if not proof.readiness.observed_at <= core._now() < proof.readiness.expires_at:
                raise Problem(403, "DELIVERY_NATIVE_DISABLED", "deployment witness has expired")
            if rows[key] is not None:
                prior = _parse(rows[key].payload["data"], InboxDeploymentEvidence)
                if prior.readiness.observed_at >= proof.readiness.observed_at:
                    raise Problem(409, "INBOX_READINESS_STALE", "readiness cannot move backward")
            return ControlChanges(writes=(
                core._write(key, proof, "create" if rows[key] is None else "replace"),
                core._write(control_key("inbox-readiness-evidence", evidence_id), proof, "create",
                            _material(proof.model_dump(mode="json"))),
            ), result=core._result(readiness_evidence_id=evidence_id))

        receipt = self.store.transact(request, decide)
        self.check_installation(core)
        return receipt

    def _core(self) -> DeliveryCore:
        if self.core is None:
            raise Problem(403, "INBOX_NOT_ATTACHED", "inbox is not attached to delivery authority")
        self.core._enabled()
        config = self.core.contract.channel
        if (
            config.channel_id != INBOX_CHANNEL_ID or config.version != INBOX_VERSION
            or config.supported_levels != ("client_received",)
            or config.max_confirmation_level != "client_received"
        ):
            raise Problem(403, "INBOX_CHANNEL_UNSUPPORTED", "only the pinned authenticated pull contract is supported")
        return self.core

    def _access(self, command: InboxFetch, action: str) -> VerifiedInboxAccess | CurrentWorkInboxAccess:
        candidate=self.registration.verify_access(command.access_evidence_id)
        access = _check(candidate, CurrentWorkInboxAccess if type(candidate) is CurrentWorkInboxAccess else VerifiedInboxAccess)
        core = self._core()
        if (
            access.evidence_id != command.access_evidence_id or access.action != action
            or access.authentication_version != self.registration.authentication_version
            or access.binding.delivery_id != command.delivery_id
            or access.binding.obligation_id != command.obligation_id
            or access.binding.route.recipient_id != access.principal_id
        ):
            raise Problem(403, "INBOX_ACCESS_DENIED", "authenticated client evidence does not match the request")
        self._current_access(access)
        return access

    def _keys(self, command: InboxFetch, context: ExecutionContext):
        return (
            control_key("owner", context.owner_record_id),
            control_key("delivery-conversation", context.conversation_id),
            control_key("delivery-obligation", command.obligation_id),
            control_key("delivery-outbox", command.delivery_id),
            control_key("policy", context.policy_id),
            control_key("inbox-exchange", command.delivery_id),
        )

    def _bound(self, rows, command, context, access, retained=None):
        if retained is not None:
            from .behavior_web_handoff import OriginalFailureWebHandoff
            if type(retained.service) is OriginalFailureWebHandoff:
                return retained.service._bound(rows, command, context, access)
        core = self._core()
        ob = core._obligation(rows, command.obligation_id, context)
        outbox = core._row(rows, control_key("delivery-outbox", command.delivery_id), _Outbox)
        material = _parse(rows[control_key("delivery-outbox", command.delivery_id)].payload["material"], InlineMaterial)
        if (
            access.binding != outbox.binding or outbox.binding.obligation_id != ob.obligation_id
            or outbox.binding.route != ob.route or outbox.binding.channel_version != ob.channel_version
            or material.sha256 != outbox.binding.payload_digest
            or ob.channel_version != INBOX_VERSION
        ):
            raise Problem(403, "INBOX_BINDING_DENIED", "saved payload, recipient, account, channel or version differs")
        return ob, outbox, material

    def fetch(self, command: InboxFetch, context: ExecutionContext) -> InboxDelivery:
        return self._fetch(command, context)

    def reconcile_fetch(self, command: InboxFetch, context: ExecutionContext) -> InboxDelivery:
        """Read the original exchange only; absence never authorizes a blind resend."""
        return self._fetch(command, context, reconcile_only=True)

    def _fetch(self, command: InboxFetch, context: ExecutionContext, retained=None, *, reconcile_only=False) -> InboxDelivery:
        command, context = _check(command, InboxFetch), _check(context, ExecutionContext)
        core = self._core()
        access = (self._access(command, "fetch") if retained is None
                  else self._retained_access(retained, command, context, "fetch"))
        if retained is None and core.registration.mode == "native" and type(access) is not CurrentWorkInboxAccess:
            self.check_installation(core)
        keys = tuple(dict.fromkeys((*self._keys(command, context),
                                    *(() if retained is None else retained.read_set))))
        exchange_id, token = (None, None) if reconcile_only else (secrets.token_hex(32), secrets.token_hex(32))
        request = ControlRequest(
            request_id=control_key("inbox-fetch", command.delivery_id, access.evidence_id),
            request_digest=_digest([command.model_dump(mode="json"), context.model_dump(mode="json"), access.model_dump(mode="json")]),
            read_set=keys,
        )

        def gate(rows):
            ob, outbox, material = self._bound(rows, command, context, access, retained)
            if retained is not None:
                self._retained_access(retained, command, context, "fetch")
                self._retained_results._gate(rows, retained)
            policy = core._row(rows, control_key("policy", context.policy_id), PolicyRecord)
            if (
                not policy.active or policy.policy_id != context.policy_id
                or policy.revision != access.policy_revision
            ):
                raise Problem(403, "INBOX_POLICY_DENIED", "current read authorization is revoked or expired")
            self._current_access(access)
            return ob, outbox, material

        def decide(rows):
            ob, outbox, material = gate(rows)
            writes = []
            if outbox.send_outcome is None:
                writes.append(core._write(
                    control_key("delivery-outbox", command.delivery_id), _update(outbox, send_outcome="unknown"),
                    material=material,
                ))
            if ob.fulfillment == "pending" and ob.final_delivery_id == command.delivery_id:
                writes.append(core._write(
                    control_key("delivery-obligation", ob.obligation_id), _update(ob, delivery_state="delivery_unknown"),
                ))
            key = control_key("inbox-exchange", command.delivery_id)
            prior = rows[key]
            if prior is not None:
                exchange = _parse(prior.payload["data"], _Exchange)
                if exchange.binding != access.binding or exchange.principal_id != access.principal_id:
                    raise Problem(403, "INBOX_EXCHANGE_DENIED", "exchange belongs to another client or payload")
                if exchange.fetch_count >= 8:
                    raise Problem(429, "INBOX_FETCH_LIMIT", "bounded pull attempts are exhausted; acknowledgment stays available")
                writes.append(core._write(
                    key, _update(exchange, fetch_count=exchange.fetch_count + 1),
                    material=_parse(prior.payload["material"], InlineMaterial),
                ))
                return ControlChanges(writes=tuple(writes), result={"exchange_id": exchange.exchange_id})
            exchange = _Exchange(
                exchange_id=exchange_id, acknowledgment_token=token, binding=access.binding,
                principal_id=access.principal_id, fetch_request=request, authorization_evidence=access,
            )
            writes.append(core._write(
                key, exchange, "create", _material(access.model_dump(mode="json")),
            ))
            return ControlChanges(writes=tuple(writes), result={"exchange_id": exchange_id})

        # This commit is the read authorization effectiveness point, not delivery.
        if reconcile_only:
            saved = authority_snapshot(self.store, (control_key("inbox-exchange", command.delivery_id),))
            if saved[control_key("inbox-exchange", command.delivery_id)] is None:
                raise Problem(409, "INBOX_FETCH_NOT_STARTED", "no retained exchange exists for this original result")
            self.store.reconcile(request)
        else:
            self.store.transact(request, decide)
        rows = authority_snapshot(self.store, keys)
        _, _, material = gate(rows)  # Historical fetch receipts never bypass current access.
        exchange = _parse(rows[control_key("inbox-exchange", command.delivery_id)].payload["data"], _Exchange)
        if exchange.binding != access.binding or exchange.principal_id != access.principal_id:
            raise Problem(403, "INBOX_EXCHANGE_DENIED", "exchange binding differs")
        return InboxDelivery(
            exchange_id=exchange.exchange_id, acknowledgment_token=exchange.acknowledgment_token,
            binding=exchange.binding, payload=material.data,
        )

    def acknowledge(self, command: InboxAcknowledgment, context: ExecutionContext) -> Record:
        return self._acknowledge(command, context)

    def _acknowledge(self, command: InboxAcknowledgment, context: ExecutionContext, retained=None) -> Record:
        command, context = _check(command, InboxAcknowledgment), _check(context, ExecutionContext)
        core = self._core()
        access = (self._access(command, "acknowledge") if retained is None
                  else self._retained_access(retained, command, context, "acknowledge"))
        if command.binding != access.binding:
            raise Problem(403, "INBOX_ACK_DENIED", "client acknowledgment digest/version/binding differs")
        key = control_key("inbox-exchange", command.delivery_id)
        evidence_id = control_key("inbox-ack-evidence", command.delivery_id)
        evidence_key = control_key("inbox-receipt-evidence", evidence_id)
        request = ControlRequest(
            request_id=control_key("inbox-acknowledge", command.delivery_id),
            request_digest=_digest([command.delivery_id, command.exchange_id, command.acknowledgment_token,
                                    command.binding.model_dump(mode="json"), access.principal_id]),
            read_set=tuple(dict.fromkeys((*self._keys(command, context), evidence_key,
                                         *(() if retained is None else retained.read_set)))),
        )

        def decide(rows):
            self._bound(rows, command, context, access, retained)
            if retained is not None:
                self._retained_access(retained, command, context, "acknowledge")
                self._retained_results._gate(rows, retained)
            if rows[key] is None:
                raise Problem(403, "INBOX_ACK_DENIED", "no server-issued delivery exchange exists")
            exchange = _parse(rows[key].payload["data"], _Exchange)
            if (
                exchange.exchange_id != command.exchange_id
                or not secrets.compare_digest(exchange.acknowledgment_token, command.acknowledgment_token)
                or exchange.binding != command.binding or exchange.principal_id != access.principal_id
            ):
                raise Problem(403, "INBOX_ACK_DENIED", "acknowledgment does not bind the authenticated exchange")
            self._current_access(access)
            acknowledged = _Acknowledged(exchange_id=exchange.exchange_id, access=access, request=request)
            proof = VerifiedDeliveryReceipt(
                evidence_id=evidence_id, verifier_version=core.contract.verifier_version,
                binding=exchange.binding, send_outcome="emitted", confirmed_levels=("client_received",),
                dispatch_authorization_ref=exchange.fetch_request.request_id,
                proof=_ack_proof(exchange, acknowledged),
            )
            return ControlChanges(writes=(
                core._write(key, _update(exchange, acknowledged=True, receipt_evidence_id=evidence_id),
                            material=_parse(rows[key].payload["material"], InlineMaterial)),
                core._write(evidence_key, proof, "create", _material(proof.model_dump(mode="json"))),
                core._write(control_key("inbox-ack-access", evidence_id), acknowledged, "create",
                            _material(acknowledged.model_dump(mode="json"))),
            ), result={"evidence_id": evidence_id, "confirmation_level": "client_received"})

        if retained is not None:
            self._retained_results._gate(authority_snapshot(self.store, request.read_set), retained)
        self.store.transact(request, decide)
        if retained is not None:
            self._retained_results._gate(authority_snapshot(self.store, request.read_set), retained)
        from .behavior_web_handoff import OriginalFailureWebHandoff
        receipt = (retained.service._complete(command.delivery_id, context, retained)
                   if retained is not None and type(retained.service) is OriginalFailureWebHandoff
                   else self.reconcile(command.delivery_id, context))
        if retained is not None:
            self._retained_results._gate(authority_snapshot(self.store, request.read_set), retained)
        return receipt

    def verify_receipt(self, evidence_id: str) -> VerifiedDeliveryReceipt:
        self._core()
        key = control_key("inbox-receipt-evidence", evidence_id)
        row = authority_snapshot(self.store, (key,))[key]
        if row is None:
            raise Problem(403, "INBOX_RECEIPT_MISSING", "server acknowledgment evidence does not exist")
        proof = _parse(row.payload["data"], VerifiedDeliveryReceipt)
        exchange_key = control_key("inbox-exchange", proof.binding.delivery_id)
        access_key = control_key("inbox-ack-access", evidence_id)
        rows = authority_snapshot(self.store, (key, exchange_key, access_key))
        if any(rows[value] is None for value in (key, exchange_key, access_key)):
            raise Problem(403, "INBOX_RECEIPT_INVALID", "the authenticated acknowledgment authority is incomplete")
        row = rows[key]
        proof = _parse(row.payload["data"], VerifiedDeliveryReceipt)
        exchange = _parse(rows[exchange_key].payload["data"], _Exchange)
        acknowledged = _parse(rows[access_key].payload["data"], _Acknowledged)
        if (
            proof.evidence_id != evidence_id or proof.confirmed_levels != ("client_received",)
            or proof.send_outcome != "emitted"
            or _parse(row.payload["material"], InlineMaterial) != _material(proof.model_dump(mode="json"))
            or not exchange.acknowledged or exchange.receipt_evidence_id != evidence_id
            or exchange.binding != proof.binding or exchange.exchange_id != acknowledged.exchange_id
            or exchange.principal_id != acknowledged.access.principal_id
            or acknowledged.access.binding != proof.binding or acknowledged.access.action != "acknowledge"
            or proof.dispatch_authorization_ref != exchange.fetch_request.request_id
            or proof.proof != _ack_proof(exchange, acknowledged)
            or _parse(rows[access_key].payload["material"], InlineMaterial) != _material(acknowledged.model_dump(mode="json"))
            or _parse(rows[exchange_key].payload["material"], InlineMaterial) != _material(exchange.authorization_evidence.model_dump(mode="json"))
        ):
            raise Problem(403, "INBOX_RECEIPT_INVALID", "retained acknowledgment evidence differs")
        return proof

    def reconcile(self, delivery_id: str, context: ExecutionContext) -> Record:
        core = self._core()
        key = control_key("inbox-exchange", delivery_id)
        row = authority_snapshot(self.store, (key,))[key]
        if row is None:
            raise Problem(409, "INBOX_DELIVERY_UNKNOWN", "no authenticated exchange is saved")
        exchange = _parse(row.payload["data"], _Exchange)
        if not exchange.acknowledged or exchange.receipt_evidence_id is None:
            raise Problem(409, "INBOX_DELIVERY_UNKNOWN", "client receipt is unknown; do not manufacture delivery")
        return core.record_receipt(ReceiptRequest(
            obligation_id=exchange.binding.obligation_id, delivery_id=delivery_id,
            evidence_id=exchange.receipt_evidence_id,
        ), context)
