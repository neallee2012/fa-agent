"""One-consume execution over BR-01; actual_environment=NOT_RUN.

Initialization (trusted server code, never tool arguments):
  ContextArtifactAdapter(settings, references=..., token=..., clock=...)
  ToolBrokerCore(store, clock=..., adapter=adapter.registration,
                 execution_adapter=adapter)

The existing authorize receipt is only a reservation. execute additionally arms
once, then consumes that arm with a fresh guard-protected gate. Only the caller
that observed BOTH fresh commits may send. A replay, crash, or uncertain consume
never grants another send, even when read-back finds nothing. Reservations and
the dispatch-obligation survive every failure. No automatic retry/refund/GC.

The implemented capability is GET of an EXACT M3 artifact logical version, with
the saved Blob ETag as If-Match and a saved content SHA-256/size. M3 download
enforces the immutable logical->native receipt and Blob version/ETag. Route
allowlists and the credential/principal binding are server configuration.
Response content is restricted to ArtifactMetrics, not arbitrary text/files.
All bytes/chunks/errors stay in the credential boundary until completely bounded,
verified and projected. Unknown formats are Problems, not empty successes.

FA admission contract fa-context-one-send-1: a consume is the revocation
linearization point; one transport entry is allowed within TWO trusted UTC
seconds (also before grant expiry). Subsequent changes are in-flight, NOT
retroactive cancellation. The transport rechecks that window after acquiring
credentials. This is a SEND window, not a bound on remote completion. Destination
JWT/actor/Team authorization still runs independently on the actual M3 request;
this is not instantaneous Entra revocation or cross-service atomic IAM.

This HTTP adapter has no write capabilities. behavior_effects separately supports
a conditional update of the existing v1.2 work authority, not an HTTP mutation.
Existing Context PATCH and artifact upload/
decision routes do not atomically check FA grant/epoch/deadline at their effect
point; a preflight cannot repair that. They need a controlled destination contract
and original-request receipts before being enabled here. Query-only, cancellation,
terminality, whole-operation idempotency and stream-to-caller are also disabled.
M3 does not expose an original-request evidence query: a lost download response
stays unknown. Reconciliation reads ORIGINAL durable receipts, never GETs again.

Data rows use control_key("dispatch", operation, attempt) and
control_key("dispatch-obligation", operation, attempt), under the SAME authority
as owner/policy/grant/budget. All their writers must use BehaviorStore's guard.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, ValidationError, field_validator, model_validator

from .behavior_operations import (
    AdapterContract, AttemptBinding, AuthorizationRequest, BudgetRecord, Count,
    ExecutionContext, GrantRecord, GrantSubject, OperationDescription, OutcomeRequest,
    PolicyRecord, ReceiptResult, RegisteredAdapter, ResolvedResource, Snapshot, Text,
    ToolBrokerCore, VerifiedOutcome, _Attempt, _Model, _checked, _digest, _json,
    _parse, _row, _updated, _utc, _write, canonical_material, control_key,
)
from .behavior_store import ControlChanges, ControlRequest, Digest, InlineMaterial
from .errors import Problem, safe_text
from .m3_models import ETag, VersionId
from .models import Identifier
from .records import Record

EXECUTION_CONTRACT = "fa-context-one-send-1"
SEND_WINDOW_SECONDS = 2
MAX_RESULT_BYTES = 4096
MAX_RESPONSE_SECONDS = 10
_SAFE_EXECUTION_ERRORS = {
    "OPERATION_CLOCK_INVALID": (503, "trusted UTC is unavailable"),
    "OPERATION_DISPATCH_WINDOW_CLOSED": (409, "the one-send window closed; reconcile the original dispatch"),
    "OPERATION_WIRE_DENIED": (403, "request differs from consumed method, target or version"),
    "OPERATION_DESTINATION_REJECTED": (502, "destination did not return a verified result"),
    "OPERATION_RESPONSE_INVALID": (502, "destination result differs from the saved content contract"),
    "OPERATION_RESPONSE_TOO_LARGE": (502, "result exceeds the saved byte bound"),
    "OPERATION_RESPONSE_TIMEOUT": (502, "response deadline exceeded; reconcile the original dispatch"),
    "OPERATION_UNSAFE_RESULT": (502, "artifact is outside the safe metrics output schema"),
    "OPERATION_TRANSPORT_UNKNOWN": (502, "no complete safe result; reconcile the original request without another send"),
}


class ArtifactRoute(_Model):
    team: Identifier
    artifact_id: Identifier
    version_id: VersionId

    @field_validator("version_id")
    @classmethod
    def fixed_version(cls, value):
        if value.lower() in {"current", "latest", "head", "unknown"}:
            raise ValueError("a fixed logical version is required")
        return value

    def path(self) -> str:
        return f"/teams/{self.team}/artifacts/{self.artifact_id}/versions/{self.version_id}/content"


class ArtifactReadSpec(ArtifactRoute):
    """Server-resolved request preconditions, retained in OperationDescription."""

    if_match: ETag
    sha256: Digest
    bytes: Annotated[int, Field(ge=1, le=MAX_RESULT_BYTES)]

    @field_validator("if_match")
    @classmethod
    def nonsecret_etag(cls, value):
        if (
            re.fullmatch(r'"(?:0x[0-9A-Fa-f]{1,32}|memory-[0-9a-f]{32})"', value) is None
            or safe_text(value, len(value)) != value
        ):
            raise ValueError("a native Blob ETag is required")
        return value


class ArtifactMetrics(_Model):
    """The entire supported artifact output schema; no arbitrary string fields."""

    schema_version: Literal["fa-artifact-metrics-v1"]
    item_count: Count
    completed_count: Count

    @model_validator(mode="after")
    def counts(self):
        if self.completed_count > self.item_count:
            raise ValueError("completed count exceeds item count")
        return self


class ContextArtifactSettings(_Model):
    api_origin: Text
    principal_id: Identifier
    routes: Annotated[tuple[ArtifactRoute, ...], Field(min_length=1, max_length=16)]
    execution_contract: Literal["fa-context-one-send-1"] = EXECUTION_CONTRACT

    @field_validator("api_origin")
    @classmethod
    def fixed_origin(cls, value):
        url = urlsplit(value)
        if (
            url.scheme != "https" or not url.netloc or url.username or url.password
            or url.path or url.query or url.fragment or value != f"https://{url.netloc}"
            or any(char.isspace() for char in value) or "%" in value or "\\" in value
        ):
            raise ValueError("one exact HTTPS origin without credentials, path or query is required")
        return value

    @model_validator(mode="after")
    def unique_routes(self):
        if len({route.path() for route in self.routes}) != len(self.routes):
            raise ValueError("duplicate allowlisted route")
        return self


class SafeArtifactResult(_Model):
    schema_version: Literal["fa-context-read-result-v1"] = "fa-context-read-result-v1"
    binding: AttemptBinding
    resource: ResolvedResource
    sha256: Digest
    bytes: Annotated[int, Field(ge=1, le=MAX_RESULT_BYTES)]
    etag_sha256: Digest
    data: ArtifactMetrics


class _ReadProof(_Model):
    request_id: Digest
    description_digest: Digest
    method: Literal["GET"] = "GET"
    http_status: Literal[200] = 200
    content_sha256: Digest
    etag_sha256: Digest
    bytes: Annotated[int, Field(ge=1, le=MAX_RESULT_BYTES)]


class DispatchRecord(_Model):
    kind: Literal["operation_dispatch"] = "operation_dispatch"
    binding: AttemptBinding
    context: ExecutionContext
    grant_id: Identifier
    consume_request_id: Digest
    execution_contract: Literal["fa-context-one-send-1", "fa-table-work-effect-1"] = EXECUTION_CONTRACT
    phase: Literal["armed", "consumed", "settled"] = "armed"
    responsibility: Literal["reconcile_original", "resolved"] = "reconcile_original"
    armed_at: datetime
    gated_at: datetime | None = None
    not_after: datetime | None = None
    policy_revision: Count | None = None
    grant_revocation_revision: Count | None = None
    effect_state: Literal["unknown", "not_applied", "applied", "partial"] = "unknown"
    result_digest: Digest | None = None

    @field_validator("armed_at", "gated_at", "not_after")
    @classmethod
    def utc_times(cls, value):
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def lifecycle(self):
        gates = (self.gated_at, self.not_after, self.policy_revision, self.grant_revocation_revision)
        if self.phase == "armed":
            if any(value is not None for value in gates):
                raise ValueError("arming is not a completed gate")
        else:
            if any(value is None for value in gates):
                raise ValueError("consume requires the entire saved gate")
            if not self.armed_at <= self.gated_at < self.not_after:
                raise ValueError("invalid gate interval")
            if self.not_after - self.gated_at > timedelta(seconds=SEND_WINDOW_SECONDS):
                raise ValueError("gate exceeds the supported send window")
        if self.phase == "settled":
            if self.effect_state != "applied" or self.responsibility != "resolved" or self.result_digest is None:
                raise ValueError("settled reads require a protected result")
        elif self.effect_state != "unknown" or self.responsibility != "reconcile_original" or self.result_digest is not None:
            raise ValueError("unsettled reads retain unknown responsibility")
        return self


class DispatchObligation(_Model):
    kind: Literal["operation_dispatch_obligation"] = "operation_dispatch_obligation"
    binding: AttemptBinding
    owner_record_id: Identifier
    root_budget_id: Identifier
    consume_request_id: Digest
    state: Literal["pending_reconciliation", "resolved"] = "pending_reconciliation"


@dataclass(frozen=True)
class _Invocation:
    binding: AttemptBinding
    context: ExecutionContext
    material: InlineMaterial
    gated_at: datetime
    not_after: datetime
    request_id: str


def _within(clock: Callable[[], datetime], start: datetime, end: datetime) -> None:
    try:
        current = _utc(clock())
    except Exception:
        raise Problem(503, "OPERATION_CLOCK_INVALID", "trusted UTC is unavailable") from None
    if not start <= current < end:
        raise Problem(
            409, "OPERATION_DISPATCH_WINDOW_CLOSED",
            "the one-send window closed; reservation and reconciliation responsibility remain",
        )


class _OneSendTransport(httpx.BaseTransport):
    def __init__(self, transport, invocation: _Invocation, spec: ArtifactReadSpec, clock):
        self.transport, self.invocation, self.spec, self.clock = transport, invocation, spec, clock
        self.sent = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        invocation = self.invocation
        if (
            self.sent or request.method != "GET" or str(request.url) != invocation.binding.target
            or request.content != b"" or request.headers.get("if-match") != self.spec.if_match
            or any(request.headers.get(name) != value for name, value in _correlation(invocation).items())
        ):
            raise Problem(403, "OPERATION_WIRE_DENIED", "request differs from consumed method, target or version")
        _within(self.clock, invocation.gated_at, invocation.not_after)
        self.sent = True
        return self.transport.handle_request(request)


def _correlation(invocation: _Invocation) -> dict[str, str]:
    # Correlation only: these headers do not claim destination idempotency or IAM.
    return {
        "x-fa-operation-id": invocation.binding.operation_id,
        "x-fa-attempt-id": invocation.binding.attempt_id,
        "x-fa-request-id": invocation.request_id,
        "x-fa-description-sha256": invocation.binding.description_digest,
    }


class ContextArtifactAdapter:
    """One owned, zero-retry HTTP client per invocation; no ambient proxies/hooks."""

    execution_contract = EXECUTION_CONTRACT

    def __init__(
        self, settings: ContextArtifactSettings, *,
        references: Mapping[str, ArtifactReadSpec], token: Callable[[], str],
        clock: Callable[[], datetime], transport: httpx.BaseTransport | None = None,
    ):
        self.settings = _checked(settings, ContextArtifactSettings)
        self._references, self._token, self._clock = references, token, clock
        # An injected transport is server-owned and only intended for local tests.
        self._transport = transport
        self.contract = AdapterContract(
            adapter_id="fa-context-artifact", adapter_version="1",
            allowed_kinds=("request",), allowed_methods=("GET",),
            allowed_targets=tuple(settings.api_origin + route.path() for route in settings.routes),
            reservation_units=1, evidence_source="fa-context-m3",
            outcome_contract_version="fa-context-read-1",
        )
        self.registration = RegisteredAdapter(contract=self.contract, resolve=self.resolve)

    def resolve(self, reference: str) -> OperationDescription:
        try:
            spec = _checked(self._references[reference], ArtifactReadSpec)
        except Exception:
            raise Problem(422, "OPERATION_REFERENCE_DENIED", "no compliant server-resolved artifact reference") from None
        description = OperationDescription(
            adapter_id=self.contract.adapter_id, adapter_version=self.contract.adapter_version,
            kind="request", method="GET", target=self.settings.api_origin + spec.path(),
            content=_json(spec.model_dump(mode="json")),
            resources=(ResolvedResource(resource_id=self.settings.api_origin + spec.path(), version=spec.version_id),),
        )
        self.validate(description)
        return description

    def validate(self, description: OperationDescription, context: ExecutionContext | None = None) -> ArtifactReadSpec:
        description = _checked(description, OperationDescription)
        try:
            spec = ArtifactReadSpec.model_validate_json(description.content)
        except (ValidationError, ValueError):
            raise Problem(422, "OPERATION_CONTENT_DENIED", "unsupported saved artifact request") from None
        target = self.settings.api_origin + spec.path()
        if (
            description.adapter_id != self.contract.adapter_id
            or description.adapter_version != self.contract.adapter_version
            or description.kind != "request" or description.method != "GET"
            or description.target != target or target not in self.contract.allowed_targets
            or description.resources != (ResolvedResource(resource_id=target, version=spec.version_id),)
            or description.dependencies or description.content != _json(spec.model_dump(mode="json"))
            or context is not None and context.principal_id != self.settings.principal_id
        ):
            raise Problem(403, "OPERATION_CONTENT_DENIED", "method, target, resource version or credential identity differs")
        return spec

    def _execute(self, invocation: _Invocation) -> tuple[VerifiedOutcome, InlineMaterial]:
        """Credential boundary: nothing raw is returned, checkpointed or logged."""
        description = OperationDescription.model_validate_json(invocation.material.data)
        spec = self.validate(description, invocation.context)
        if (
            canonical_material(description) != invocation.material
            or invocation.binding.description_digest != invocation.material.sha256
            or invocation.binding.target != description.target
        ):
            raise Problem(403, "OPERATION_CONTENT_DENIED", "consumed bytes or destination binding differs")
        native = self._transport
        owned = native is None
        try:
            token = self._token()
            if not isinstance(token, str) or not token or len(token) > 16384 or any(c.isspace() for c in token):
                raise ValueError("invalid credential")
            if token.encode() in invocation.material.data:
                raise ValueError("credential in operation material")
            if native is None:
                native = httpx.HTTPTransport(retries=0)
            gate = _OneSendTransport(native, invocation, spec, self._clock)
            started = time.monotonic()
            with httpx.Client(
                transport=gate, trust_env=False, follow_redirects=False, timeout=5.0,
                headers={"Accept-Encoding": "identity"},
            ) as client:
                with client.stream(
                    "GET", description.target,
                    headers={
                        "Authorization": "Bearer " + token, "If-Match": spec.if_match,
                        **_correlation(invocation),
                    },
                ) as response:
                    if response.status_code != 200:
                        # No remote code/message/body/header leaves this boundary.
                        raise Problem(502, "OPERATION_DESTINATION_REJECTED", "destination did not return a verified result")
                    if (
                        response.headers.get("content-encoding", "identity") != "identity"
                        or response.headers.get("content-type", "").split(";")[0] != "application/octet-stream"
                        or response.headers.get("etag") != spec.if_match
                        or response.headers.get("x-artifact-sha256") != spec.sha256
                    ):
                        raise Problem(502, "OPERATION_RESPONSE_INVALID", "destination content contract differs")
                    length = response.headers.get("content-length")
                    if length is not None and length != str(spec.bytes):
                        raise Problem(502, "OPERATION_RESPONSE_INVALID", "destination length differs")
                    chunks, total = [], 0
                    for chunk in response.iter_raw():
                        if time.monotonic() - started >= MAX_RESPONSE_SECONDS:
                            raise Problem(502, "OPERATION_RESPONSE_TIMEOUT", "bounded response deadline exceeded")
                        total += len(chunk)
                        if total > min(MAX_RESULT_BYTES, spec.bytes):
                            raise Problem(502, "OPERATION_RESPONSE_TOO_LARGE", "result exceeds the saved byte bound")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
            if len(raw) != spec.bytes or hashlib.sha256(raw).hexdigest() != spec.sha256:
                raise Problem(502, "OPERATION_RESPONSE_INVALID", "downloaded bytes differ from the saved version")
            # The strict schema is the safety guarantee, not a regex secret detector.
            if token.encode() in raw:
                raise Problem(502, "OPERATION_UNSAFE_RESULT", "result contains credential-channel material")
            try:
                def unique_fields(pairs):
                    value = dict(pairs)
                    if len(value) != len(pairs):
                        raise ValueError("duplicate output field")
                    return value

                metrics = ArtifactMetrics.model_validate(json.loads(raw, object_pairs_hook=unique_fields))
            except (ValidationError, ValueError):
                raise Problem(502, "OPERATION_UNSAFE_RESULT", "artifact is outside the safe metrics output schema") from None
            result = SafeArtifactResult(
                binding=invocation.binding, resource=description.resources[0],
                sha256=spec.sha256, bytes=spec.bytes,
                etag_sha256=hashlib.sha256(spec.if_match.encode()).hexdigest(), data=metrics,
            )
            data = _json(result.model_dump(mode="json"))
            material = InlineMaterial(data=data, sha256=hashlib.sha256(data).hexdigest())
            proof = _ReadProof(
                request_id=invocation.request_id, description_digest=invocation.material.sha256,
                content_sha256=result.sha256, etag_sha256=result.etag_sha256, bytes=result.bytes,
            )
            evidence = VerifiedOutcome(
                evidence_id=control_key("dispatch-result", invocation.binding.operation_id, invocation.binding.attempt_id),
                source=self.contract.evidence_source, contract_version=self.contract.outcome_contract_version,
                binding=invocation.binding, observation="applied", sequence=1, observed_at=_utc(self._clock()),
                proof=_json(proof.model_dump(mode="json")), business_result="succeeded",
                result=ReceiptResult(
                    receipt_id=control_key("dispatch-result", invocation.binding.operation_id, invocation.binding.attempt_id),
                    resource=result.resource, status="completed",
                ),
            )
            return evidence, material
        except Problem as exc:
            # An injected credential/transport may itself raise Problem. Never
            # trust its message, code, details, status or chained exception.
            code = exc.body["error"]["code"]
            if code not in _SAFE_EXECUTION_ERRORS:
                code = "OPERATION_TRANSPORT_UNKNOWN"
            status, message = _SAFE_EXECUTION_ERRORS[code]
            raise Problem(status, code, message) from None
        except Exception:
            raise Problem(
                502, "OPERATION_TRANSPORT_UNKNOWN",
                "no complete safe result; reconcile only the original request without another send",
            ) from None
        finally:
            if owned and native is not None:
                try:
                    native.close()
                except Exception:
                    # Cleanup cannot invalidate a verified read or expose a credential.
                    pass


class BehaviorDispatcher:
    def __init__(self, core: ToolBrokerCore, adapter: ContextArtifactAdapter):
        from .behavior_effects import WorkStateEffectAdapter

        if type(adapter) not in {ContextArtifactAdapter, WorkStateEffectAdapter} or core._enabled()[0] is not adapter.registration:
            raise Problem(403, "OPERATION_EXECUTION_DISABLED", "the concrete adapter and registered contract must be identical")
        if core._contract_digest() != _digest(adapter.contract.model_dump(mode="json")):
            raise Problem(403, "OPERATION_EXECUTION_DISABLED", "execution registration differs")
        self.core, self.adapter, self.store = core, adapter, core.store

    @staticmethod
    def _keys(command) -> tuple[str, str]:
        return (
            control_key("dispatch", command.operation_id, command.attempt_id),
            control_key("dispatch-obligation", command.operation_id, command.attempt_id),
        )

    def reconciliation_request(
        self, command: AuthorizationRequest, context: ExecutionContext, *,
        stage: Literal["arm", "consume"],
    ) -> ControlRequest:
        command, context = _checked(command, AuthorizationRequest), _checked(context, ExecutionContext)
        if stage not in {"arm", "consume"}:
            raise Problem(422, "OPERATION_INVALID_DATA", "unknown dispatch stage")
        base = self.core.reconciliation_request(command, context)
        return ControlRequest(
            request_id=control_key("dispatch-" + stage, command.operation_id, command.attempt_id),
            request_digest=_digest([
                self.adapter.execution_contract, stage, base.model_dump(mode="json"),
                self.adapter.settings.model_dump(mode="json"),
            ]),
            read_set=tuple(sorted(set((*base.read_set, *self._keys(command))))),
        )

    def completion_request(self, command: OutcomeRequest, context: ExecutionContext) -> ControlRequest:
        request = self.core.reconciliation_request(command, context)
        return _updated(request, read_set=tuple(sorted(set((*request.read_set, *self._keys(command))))))

    def _attempt(self, snapshot, command, context):
        self.core._owner(snapshot, context, command.operation_id)
        operation, material, description = self.core._operation(snapshot, command.operation_id, context)
        attempt = _row(snapshot, control_key("attempt", command.operation_id, command.attempt_id), _Attempt)
        self.adapter.validate(description, context)
        if (
            operation.current_attempt_id != command.attempt_id or attempt.context != context
            or attempt.grant_id != command.grant_id or attempt.observation_sequence
            or attempt.effect_state != "unknown" or operation.effect_state != "unknown"
            or attempt.binding.operation_id != command.operation_id or attempt.binding.attempt_id != command.attempt_id
            or attempt.binding.description_digest != material.sha256
            or attempt.binding.adapter_contract_digest != self.core._contract_digest()
            or attempt.binding.target != description.target
            or attempt.binding.idempotency_key != control_key("idempotency", command.operation_id, material.sha256)
            or operation.unsettled_attempt_ids != (command.attempt_id,)
        ):
            raise Problem(409, "OPERATION_DISPATCH_DENIED", "attempt is not the original unresolved reservation")
        return attempt, material

    def _arm(self, command, context) -> None:
        request = self.reconciliation_request(command, context, stage="arm")
        dispatch_key, obligation_key = self._keys(command)
        fresh = False

        def decide(snapshot):
            nonlocal fresh
            fresh = True
            attempt, _ = self._attempt(snapshot, command, context)
            if snapshot[dispatch_key] is not None or snapshot[obligation_key] is not None:
                raise Problem(409, "OPERATION_DISPATCH_CONSUMED", "the original dispatch is already owned")
            consume_id = self.reconciliation_request(command, context, stage="consume").request_id
            dispatch = DispatchRecord(
                binding=attempt.binding, context=context, grant_id=command.grant_id,
                consume_request_id=consume_id, armed_at=self.core._now(),
                execution_contract=self.adapter.execution_contract,
            )
            obligation = DispatchObligation(
                binding=attempt.binding, owner_record_id=context.owner_record_id,
                root_budget_id=context.root_budget_id, consume_request_id=consume_id,
            )
            return ControlChanges(writes=(
                _write(dispatch_key, dispatch, "create"),
                _write(obligation_key, obligation, "create"),
            ), result={"dispatch": dispatch.model_dump(mode="json"), "dispatch_permission": False})

        self.store.transact(request, decide)
        if not fresh:
            raise Problem(
                409, "OPERATION_DISPATCH_CONSUMED",
                "historical arming is not permission; reconcile the original dispatch without another send",
            )

    def _consume(self, command, context) -> _Invocation:
        request = self.reconciliation_request(command, context, stage="consume")
        dispatch_key, obligation_key = self._keys(command)
        invocation = None

        def decide(snapshot):
            nonlocal invocation
            attempt, material = self._attempt(snapshot, command, context)
            dispatch = _row(snapshot, dispatch_key, DispatchRecord)
            obligation = _row(snapshot, obligation_key, DispatchObligation)
            if (
                dispatch.phase != "armed" or dispatch.binding != attempt.binding
                or dispatch.context != context or dispatch.grant_id != command.grant_id
                or obligation.binding != attempt.binding or obligation.state != "pending_reconciliation"
                or obligation.owner_record_id != context.owner_record_id
                or obligation.root_budget_id != context.root_budget_id
                or dispatch.consume_request_id != request.request_id or obligation.consume_request_id != request.request_id
            ):
                raise Problem(409, "OPERATION_DISPATCH_CONSUMED", "dispatch ownership or responsibility differs")
            grant = _row(snapshot, control_key("grant", command.grant_id), GrantRecord)
            policy = _row(snapshot, control_key("policy", context.policy_id), PolicyRecord)
            budget = _row(snapshot, control_key("budget", context.root_budget_id), BudgetRecord)
            now = self.core._now()
            if (
                grant.grant_id != command.grant_id or grant.revoked
                or grant.revocation_revision != attempt.grant_revocation_revision
                or GrantSubject(principal_id=context.principal_id, conversation_id=context.conversation_id) not in grant.subjects
                or grant.root_budget_id != context.root_budget_id or grant.description_digest != material.sha256
                or grant.adapter_contract_digest != self.core._contract_digest()
                or grant.policy_id != context.policy_id or policy.policy_id != context.policy_id
                or not policy.active or policy.revision != grant.policy_revision or policy.revision != attempt.policy_revision
                or not grant.not_before <= now < min(grant.expires_at, attempt.not_after)
                or now < dispatch.armed_at or now < attempt.authorized_at
            ):
                raise Problem(403, "OPERATION_GRANT_DENIED", "current human grant, revision or execution window differs")
            # authorize already reserved this exact attempt; zero REMAINING uses
            # is valid, not a reason to charge twice or reuse another reservation.
            if (
                not budget.active or budget.root_budget_id != context.root_budget_id
                or attempt.reserved_units != self.adapter.contract.reservation_units
            ):
                raise Problem(403, "OPERATION_BUDGET_DENIED", "the reserved root budget is not available")
            deadline = min(attempt.not_after, grant.expires_at, now + timedelta(seconds=SEND_WINDOW_SECONDS))
            consumed = _updated(
                dispatch, phase="consumed", gated_at=now, not_after=deadline,
                policy_revision=policy.revision, grant_revocation_revision=grant.revocation_revision,
            )
            invocation = _Invocation(attempt.binding, context, material, now, deadline, request.request_id)
            changes = ControlChanges(writes=(
                _write(dispatch_key, consumed, "replace"),
                _write(obligation_key, obligation, "replace"),
            ), result={"dispatch": consumed.model_dump(mode="json"), "dispatch_permission": False})
            return self._consume_changes(invocation, attempt, changes)

        self.store.transact(request, decide)
        if invocation is None:
            raise Problem(409, "OPERATION_DISPATCH_CONSUMED", "a consumed receipt never permits another dispatch")
        _within(self.core.clock, invocation.gated_at, invocation.not_after)
        return invocation

    def _consume_changes(self, invocation: _Invocation, attempt: _Attempt, changes: ControlChanges) -> ControlChanges:
        return changes

    def _complete(self, evidence: VerifiedOutcome, context: ExecutionContext, material: InlineMaterial) -> Record:
        evidence = _checked(evidence, VerifiedOutcome)
        material = _checked(material, InlineMaterial)
        result = _parse(json.loads(material.data), SafeArtifactResult)
        try:
            proof = _ReadProof.model_validate_json(evidence.proof)
        except (ValidationError, ValueError):
            raise Problem(502, "OPERATION_RESPONSE_INVALID", "unsupported native read evidence") from None
        if (
            result.binding != evidence.binding or evidence.result is None
            or result.resource != evidence.result.resource or evidence.business_result != "succeeded"
            or evidence.observation != "applied"
            or proof.description_digest != result.binding.description_digest
            or proof.content_sha256 != result.sha256 or proof.bytes != result.bytes
            or proof.etag_sha256 != result.etag_sha256
        ):
            raise Problem(502, "OPERATION_RESPONSE_INVALID", "safe result and verified receipt differ")
        command = OutcomeRequest(
            operation_id=evidence.binding.operation_id, attempt_id=evidence.binding.attempt_id,
            evidence_id=evidence.evidence_id,
        )
        dispatch_key, obligation_key = self._keys(command)

        def extend(snapshot: Snapshot, changes: ControlChanges) -> ControlChanges:
            dispatch = _row(snapshot, dispatch_key, DispatchRecord)
            obligation = _row(snapshot, obligation_key, DispatchObligation)
            _, _, description = self.core._operation(snapshot, command.operation_id, context)
            spec = self.adapter.validate(description)
            if (
                dispatch.phase != "consumed" or dispatch.binding != evidence.binding
                or obligation.binding != evidence.binding or obligation.state != "pending_reconciliation"
                or proof.request_id != dispatch.consume_request_id
                or obligation.consume_request_id != dispatch.consume_request_id
                or result.sha256 != spec.sha256 or result.bytes != spec.bytes
                or result.etag_sha256 != hashlib.sha256(spec.if_match.encode()).hexdigest()
            ):
                raise Problem(409, "OPERATION_EVIDENCE_DENIED", "receipt is not for the consumed dispatch")
            return ControlChanges(writes=(*changes.writes,
                _write(dispatch_key, _updated(
                    dispatch, phase="settled", responsibility="resolved", effect_state="applied",
                    result_digest=material.sha256,
                ), "replace", material),
                _write(obligation_key, _updated(obligation, state="resolved"), "replace"),
            ), result={
                **changes.result, "execution_enabled": True,
                "safe_result": result.model_dump(mode="json"), "result_digest": material.sha256,
            })

        return self.core._record_verified_outcome(command, context, evidence, extra_reads=self._keys(command), extend=extend)

    def execute(self, command: AuthorizationRequest, context: ExecutionContext) -> Record:
        command, context = _checked(command, AuthorizationRequest), _checked(context, ExecutionContext)
        if command.previous_attempt_id is not None or command.idempotency_evidence_id is not None:
            raise Problem(403, "OPERATION_RETRY_DISABLED", "this adapter has no retry or terminality contract")
        # This may return historical accounting; ONLY the fresh arm+consume below
        # confer one in-process send opportunity, never this receipt.
        self.core.authorize(command, context)
        self._arm(command, context)
        invocation = self._consume(command, context)
        evidence, result = self.adapter._execute(invocation)
        return self._complete(evidence, context, result)

    def record_outcome(self, command: OutcomeRequest, context: ExecutionContext) -> Record:
        _checked(command, OutcomeRequest)
        _checked(context, ExecutionContext)
        raise Problem(
            403, "OPERATION_EVIDENCE_DISABLED",
            "M3 has no original-request evidence query; reconcile saved completion receipts, never redownload",
        )
