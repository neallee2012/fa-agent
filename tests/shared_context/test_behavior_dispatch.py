"""Real httpx + local M3 transport evidence only; actual_environment=NOT_RUN."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest
from azure.core.exceptions import ResourceNotFoundError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import A
from shared_context.api import create_app
from shared_context.behavior_dispatch import (
    ArtifactMetrics, ArtifactReadSpec, ArtifactRoute, ContextArtifactAdapter,
    ContextArtifactSettings, DispatchRecord, SafeArtifactResult, SEND_WINDOW_SECONDS,
    _Invocation, _OneSendTransport, _correlation,
)
from shared_context.behavior_operations import (
    AuthorizationRequest, OperationDescription, OutcomeRequest, ResolvedResource,
    ToolBrokerCore, ToolIntent, canonical_material, control_key,
)
from shared_context.behavior_store import BehaviorStore, InlineMaterial
from shared_context.errors import Problem
from test_behavior_operations import CONTEXT, NOW, OTHER, Harness
from test_behavior_store import FakeTable, SCOPE, code
from test_m3_artifacts import V1, V2, create, m3, upload

ORIGIN = "https://context.example"
TOKEN = "SYNTHETIC_CREDENTIAL_MARKER_59ac"
METRICS = ArtifactMetrics(schema_version="fa-artifact-metrics-v1", item_count=3, completed_count=2)
DATA = METRICS.model_dump_json().encode()
ROUTE = ArtifactRoute(team=A.team, artifact_id="report", version_id=V1)
SPEC = ArtifactReadSpec(
    **ROUTE.model_dump(), if_match='"0xABC"', sha256=hashlib.sha256(DATA).hexdigest(), bytes=len(DATA),
)


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.reads = chunks, 0

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


def response(data=DATA, *, spec=SPEC, status=200, headers=None, chunks=None):
    return httpx.Response(status, headers={
        "content-type": "application/octet-stream", "etag": spec.if_match,
        "x-artifact-sha256": spec.sha256,
        **(headers or {}),
    }, stream=Chunks([data] if chunks is None else chunks))


class DispatchHarness(Harness):
    def __init__(self, *, wire=False, spec=SPEC, send=None):
        super().__init__(wire=wire)
        self.requests, self.references = [], {"current": spec}
        self.time = NOW
        self.send = send or (lambda _: response(spec=spec))
        self.secret = TOKEN
        self.settings = ContextArtifactSettings(
            api_origin=ORIGIN, principal_id=CONTEXT.principal_id,
            routes=(ROUTE, ArtifactRoute(**{**ROUTE.model_dump(), "version_id": V2})),
        )
        self.concrete = ContextArtifactAdapter(
            self.settings, references=self.references, token=lambda: self.secret,
            clock=lambda: self.time, transport=httpx.MockTransport(self.transport),
        )
        self.adapter = self.concrete.registration
        self.core = self.broker(self.store)

    def broker(self, store):
        if not hasattr(self, "concrete"):
            return super().broker(store)
        return ToolBrokerCore(
            store, clock=lambda: self.time, adapter=self.adapter, execution_adapter=self.concrete,
        )

    def transport(self, request):
        self.requests.append(request)
        return self.send(request)

    def ready(self, *, uses=1):
        self.prepare()
        self.approve(uses=uses, units=uses)
        return self

    def execute(self, *, core=None, context=CONTEXT, op="op-a", attempt="a"):
        return (core or self.core).execute(op, attempt, context=context, grant_id="grant")

    def command(self):
        return AuthorizationRequest(operation_id="op-a", attempt_id="a", grant_id="grant")

    def completion(self):
        return OutcomeRequest(
            operation_id="op-a", attempt_id="a", evidence_id=control_key("dispatch-result", "op-a", "a"),
        )

    def intercept(self, phase, action):
        original = self.store._submit
        fired = False

        def submit(operations, request):
            nonlocal fired
            data = [json.loads(operation[1]["Payload"]) for operation in operations]
            matches = any(
                item.get("data", {}).get("kind") == "operation_dispatch"
                and item["data"]["phase"] == phase for item in data
            )
            if matches and not fired:
                fired = True
                return action(lambda: original(operations, request))
            return original(operations, request)

        self.store._submit = submit


@pytest.mark.parametrize("wire", [False, True])
def test_real_httpx_single_get_records_result_and_obligation_atomically(wire):
    h = DispatchHarness(wire=wire).ready()
    receipt = h.execute()
    request = h.requests[0]
    assert len(h.requests) == 1
    assert request.method == "GET" and str(request.url) == ORIGIN + ROUTE.path()
    assert request.content == b"" and request.headers["if-match"] == SPEC.if_match
    assert request.headers["authorization"] == "Bearer " + TOKEN
    assert request.headers["x-fa-operation-id"] == "op-a"
    assert request.headers["x-fa-attempt-id"] == "a"
    assert request.headers["x-fa-request-id"] == h.core.dispatcher.reconciliation_request(
        h.command(), CONTEXT, stage="consume",
    ).request_id
    assert request.headers["x-fa-description-sha256"] == h.material("operation", "op-a").sha256
    assert request.headers["accept-encoding"] == "identity"
    assert set(request.extensions["timeout"].values()) == {5.0}
    assert receipt.payload["result"]["operation_business_state"] == "succeeded"
    assert SafeArtifactResult.model_validate_json(
        json.dumps(receipt.payload["result"]["safe_result"]),
    ).data == METRICS
    dispatch = h.data("dispatch", "op-a", "a")
    assert dispatch["phase"] == "settled" and dispatch["effect_state"] == "applied"
    assert dispatch["execution_contract"] == "fa-context-one-send-1"
    assert h.data("dispatch-obligation", "op-a", "a")["state"] == "resolved"
    assert h.data("grant", "grant")["remaining_uses"] == 0
    assert h.data("budget", "root")["remaining_attempts"] == 63
    assert h.data("budget", "root")["remaining_units"] == 127
    material = h.material("dispatch", "op-a", "a")
    assert material.sha256 == dispatch["result_digest"]
    assert TOKEN not in str(receipt) and TOKEN.encode() not in material.data
    if wire:
        batch = h.client.parts[-1]
        assert len(batch) == 7  # guard, receipt, operation, attempt, evidence, dispatch, obligation
    else:
        batch = h.client.submissions[-1]
        assert len(batch) == 7


def test_authorization_receipt_is_never_the_dispatch_ticket_or_double_spend():
    h = DispatchHarness().ready()
    reservation = h.authorize()
    assert reservation.payload["result"]["execution_enabled"] is False
    assert not h.requests and h.data("grant", "grant")["remaining_uses"] == 0
    h.execute()
    count = h.submissions()
    with pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == "OPERATION_DISPATCH_CONSUMED"
    assert h.submissions() == count and len(h.requests) == 1
    assert h.data("budget", "root")["remaining_attempts"] == 63


@pytest.mark.parametrize("phase", ["armed", "consumed"])
def test_concurrent_consume_from_independent_brokers_never_duplicates_send(phase):
    h = DispatchHarness().ready()
    peer = h.broker(BehaviorStore(FakeTable(h.client.backing), SCOPE))
    winners, errors = [], []

    def race(submit):
        try:
            winners.append(h.execute(core=peer))
        except Problem as exc:
            errors.append(exc.body["error"]["code"])
        return submit()

    h.intercept(phase, race)
    if phase == "armed":
        with pytest.raises(Problem) as error:
            h.execute()
        assert code(error) == "BEHAVIOR_CONFLICT"
        assert len(winners) == 1
    else:
        h.execute()
        assert errors == ["OPERATION_DISPATCH_CONSUMED"]
    assert len(h.requests) == 1
    assert h.data("grant", "grant")["remaining_uses"] == 0


@pytest.mark.parametrize("failure", ["before", "after", "malformed"])
def test_uncertain_consume_commit_cannot_dispatch_even_after_receipt_replay(failure):
    h = DispatchHarness().ready()

    def uncertain(submit):
        h.client.failure = failure
        try:
            return submit()
        finally:
            h.client.failure = None

    h.intercept("consumed", uncertain)
    with pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert not h.requests
    original = h.core.dispatcher.reconciliation_request(h.command(), CONTEXT, stage="consume")
    if failure == "before":
        with pytest.raises(Problem) as missing:
            h.store.reconcile(original)
        assert code(missing) == "BEHAVIOR_COMMIT_UNKNOWN"
    else:
        history = h.store.reconcile(original)
        assert history.payload["result"]["dispatch_permission"] is False
    with pytest.raises(Problem) as replay:
        h.execute(core=h.broker(BehaviorStore(FakeTable(h.client.backing), SCOPE)))
    assert code(replay) == "OPERATION_DISPATCH_CONSUMED" and not h.requests
    assert h.data("dispatch-obligation", "op-a", "a")["state"] == "pending_reconciliation"
    assert h.data("grant", "grant")["remaining_uses"] == 0
    assert h.data("operation", "op-a")["unsettled_attempt_ids"] == ["a"]


@pytest.mark.parametrize("failure", ["lost", "incomplete"])
def test_native_table_client_uncertain_consume_never_replay_sends(failure):
    h = DispatchHarness(wire=True).ready()

    def uncertain(submit):
        h.client.after_apply = failure
        return submit()

    h.intercept("consumed", uncertain)
    with pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert not h.requests
    request = h.core.dispatcher.reconciliation_request(h.command(), CONTEXT, stage="consume")
    assert h.store.reconcile(request).payload["result"]["dispatch"]["phase"] == "consumed"
    with pytest.raises(Problem) as replay:
        h.execute()
    assert code(replay) == "OPERATION_DISPATCH_CONSUMED" and not h.requests


def test_destination_read_then_ack_lost_remains_unknown_and_never_queries_or_resends():
    accepted = []

    def lost(request):
        accepted.append(request.url.path)
        raise httpx.ReadTimeout(TOKEN)

    h = DispatchHarness(send=lost).ready()
    with pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == "OPERATION_TRANSPORT_UNKNOWN"
    assert TOKEN not in str(error.value) and TOKEN not in str(error.value.body)
    assert error.value.__suppress_context__
    assert h.data("dispatch", "op-a", "a")["phase"] == "consumed"
    assert h.data("attempt", "op-a", "a")["effect_state"] == "unknown"
    with pytest.raises(Problem):
        h.execute()
    with pytest.raises(Problem) as no_evidence:
        h.core.record_outcome(OutcomeRequest(
            operation_id="op-a", attempt_id="a", evidence_id="negative-query",
        ), CONTEXT)
    assert code(no_evidence) == "OPERATION_EVIDENCE_DISABLED"
    with pytest.raises(Problem) as retry:
        h.core.dispatcher.execute(AuthorizationRequest(
            operation_id="op-a", attempt_id="b", grant_id="grant", previous_attempt_id="a",
        ), CONTEXT)
    assert code(retry) == "OPERATION_RETRY_DISABLED"
    assert len(accepted) == len(h.requests) == 1
    assert h.data("dispatch-obligation", "op-a", "a")["state"] == "pending_reconciliation"


def test_complete_result_applied_but_control_ack_lost_reconciles_original_safe_material():
    h = DispatchHarness().ready()

    def lost(submit):
        h.client.failure = "after"
        try:
            return submit()
        finally:
            h.client.failure = None

    h.intercept("settled", lost)
    with pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == "BEHAVIOR_COMMIT_UNKNOWN" and len(h.requests) == 1
    request = h.core.dispatcher.completion_request(h.completion(), CONTEXT)
    receipt = h.store.reconcile(request)
    assert receipt.payload["result"]["safe_result"]["data"] == METRICS.model_dump()
    assert h.data("operation", "op-a")["business_state"] == "succeeded"
    assert h.data("dispatch-obligation", "op-a", "a")["state"] == "resolved"
    with pytest.raises(Problem):
        h.execute()
    assert len(h.requests) == 1


@pytest.mark.parametrize("kind,key,updates,expected", [
    ("grant", "grant", {"revoked": True, "revocation_revision": 1}, "OPERATION_GRANT_DENIED"),
    ("grant", "grant", {"revocation_revision": 1}, "OPERATION_GRANT_DENIED"),
    ("policy", "policy", {"active": False}, "OPERATION_GRANT_DENIED"),
    ("policy", "policy", {"revision": 2}, "OPERATION_GRANT_DENIED"),
    ("owner", "owner-one", {"epoch": 2}, "OPERATION_OWNER_DENIED"),
    ("owner", "owner-one", {"active": False}, "OPERATION_OWNER_DENIED"),
    ("budget", "root", {"active": False}, "OPERATION_BUDGET_DENIED"),
])
def test_fresh_gate_rechecks_current_controls_not_old_authorization(kind, key, updates, expected):
    h = DispatchHarness().ready()
    h.authorize()
    h.mutate(kind, key, **updates)
    with pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == expected
    assert not h.requests and h.data("grant", "grant")["remaining_uses"] == 0
    assert h.data("budget", "root")["remaining_attempts"] == 63


def test_revocation_between_gate_snapshot_and_consume_commit_conflicts():
    h = DispatchHarness().ready()

    def revoke(submit):
        h.mutate("grant", "grant", revoked=True, revocation_revision=1)
        return submit()

    h.intercept("consumed", revoke)
    with pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == "BEHAVIOR_CONFLICT" and not h.requests
    assert h.data("dispatch", "op-a", "a")["phase"] == "armed"
    assert h.data("dispatch-obligation", "op-a", "a")["state"] == "pending_reconciliation"


def test_last_gate_policy_change_is_in_flight_only_under_explicit_two_second_send_contract():
    h = DispatchHarness().ready()

    def revoke_after_commit(submit):
        receipt = submit()
        h.mutate("policy", "policy", active=False, revision=2)
        return receipt

    h.intercept("consumed", revoke_after_commit)
    result = h.execute()
    assert len(h.requests) == 1
    assert result.payload["result"]["operation_business_state"] == "succeeded"
    claim = DispatchRecord.model_validate_json(json.dumps(h.data("dispatch", "op-a", "a")))
    assert claim.execution_contract == "fa-context-one-send-1" and claim.policy_revision == 1
    assert claim.not_after - claim.gated_at == timedelta(seconds=SEND_WINDOW_SECONDS)
    assert h.data("policy", "policy")["revision"] == 2


@pytest.mark.parametrize("offset", [-1, SEND_WINDOW_SECONDS, 3600])
def test_transport_rechecks_window_after_credential_resolution(offset):
    h = DispatchHarness().ready()

    def token():
        h.time = NOW + timedelta(seconds=offset)
        return TOKEN

    h.concrete._token = token
    with pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == "OPERATION_DISPATCH_WINDOW_CLOSED" and not h.requests
    assert h.data("dispatch", "op-a", "a")["phase"] == "consumed"
    assert h.data("grant", "grant")["remaining_uses"] == 0


def test_exhausted_shared_grant_cannot_be_reserved_in_second_conversation():
    h = DispatchHarness().ready()
    h.prepare("op-b", OTHER)
    h.execute()
    with pytest.raises(Problem) as error:
        h.execute(op="op-b", attempt="b", context=OTHER)
    assert code(error) == "OPERATION_BUDGET_DENIED" and len(h.requests) == 1
    with pytest.raises(ResourceNotFoundError):
        h.raw("dispatch", "op-b", "b")


def test_approval_a_alias_b_then_c_after_gate_still_consumes_saved_a():
    h = DispatchHarness().ready()
    saved = h.material("operation", "op-a")
    h.references["current"] = SPEC.model_copy(update={"version_id": V2})

    def change_after_gate(submit):
        receipt = submit()
        h.references["current"] = SPEC.model_copy(update={"sha256": "f" * 64})
        return receipt

    h.intercept("consumed", change_after_gate)
    h.execute()
    assert str(h.requests[0].url) == ORIGIN + ROUTE.path()
    assert h.material("operation", "op-a") == saved


@pytest.mark.parametrize("updates", [
    {"method": "POST"}, {"method": "PATCH"}, {"kind": "script"},
    {"target": "https://evil.example/target"}, {"adapter_version": "2"},
    {"resources": (ResolvedResource(resource_id=ORIGIN + ROUTE.path(), version=V2),)},
    {"content": b'{"approved":true}'},
])
def test_method_target_version_and_saved_body_mismatch_are_rejected(updates):
    h = DispatchHarness()
    original = h.concrete.resolve("current")
    with pytest.raises(Problem):
        h.concrete.validate(original.model_copy(update=updates), CONTEXT)
    assert not h.requests


@pytest.mark.parametrize("change", ["method", "target", "body", "version", "identity", "second-send"])
def test_real_client_transport_gate_rejects_altered_wire_request(change):
    h = DispatchHarness()
    description = h.concrete.resolve("current")
    material = canonical_material(description)
    from shared_context.behavior_operations import AttemptBinding

    binding = AttemptBinding(
        operation_id="op-a", attempt_id="a", description_digest=material.sha256,
        adapter_contract_digest=h.core._contract_digest(), target=description.target,
        idempotency_key=control_key("idempotency", "op-a", material.sha256),
    )
    invocation = _Invocation(binding, CONTEXT, material, NOW, NOW + timedelta(seconds=2), "a" * 64)
    gate = _OneSendTransport(httpx.MockTransport(h.transport), invocation, SPEC, lambda: NOW)
    method, target, body, etag = "GET", description.target, b"", SPEC.if_match
    if change == "method":
        method = "POST"
    elif change == "target":
        target += "?other=1"
    elif change == "body":
        body = b"unexpected"
    elif change == "version":
        etag = '"0xBAD"'
    headers = {"If-Match": etag, **_correlation(invocation)}
    if change == "identity":
        headers["x-fa-request-id"] = "b" * 64
    with httpx.Client(transport=gate) as client:
        if change == "second-send":
            client.get(target, headers=headers)
        with pytest.raises(Problem) as error:
            client.request(method, target, headers=headers, content=body)
    assert code(error) == "OPERATION_WIRE_DENIED"
    assert len(h.requests) == (1 if change == "second-send" else 0)


@pytest.mark.parametrize("status", [307, 401, 403, 404, 412, 429, 500])
def test_no_redirect_retry_terminal_inference_or_error_secret_output(status):
    h = DispatchHarness(send=lambda _: response(
        status=status, headers={"location": "https://evil.example/" + TOKEN},
        data=json.dumps({"error": {"code": TOKEN, "message": "Bearer " + TOKEN}}).encode(),
    )).ready()
    with pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == "OPERATION_DESTINATION_REJECTED"
    assert TOKEN not in str(error.value.body) and len(h.requests) == 1
    assert h.data("dispatch", "op-a", "a")["effect_state"] == "unknown"
    assert h.data("dispatch-obligation", "op-a", "a")["state"] == "pending_reconciliation"


@pytest.mark.parametrize("raw", [
    ('{"schema_version":"fa-artifact-metrics-v1","item_count":3,"completed_count":2,"access_token":"' + TOKEN + '"}').encode(),
    ('{"schema_version":"' + TOKEN + '","item_count":3,"completed_count":2}').encode(),
    ('{"schema_version":"' + TOKEN + '","schema_version":"fa-artifact-metrics-v1","item_count":3,"completed_count":2}').encode(),
    b'{"schema_version":"fa-artifact-metrics-v1","item_count":true,"completed_count":0}',
    b'{"schema_version":"fa-artifact-metrics-v1","item_count":3,"completed_count":5}',
    b"normal but unsupported text",
])
def test_unsafe_success_or_unrepresentable_result_is_explicit_failure_not_empty_success(raw):
    spec = SPEC.model_copy(update={"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    h = DispatchHarness(spec=spec, send=lambda _: response(raw, spec=spec)).ready()
    with pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == "OPERATION_UNSAFE_RESULT"
    assert TOKEN not in str(error.value) and TOKEN not in str(error.value.body)
    assert TOKEN not in str(h.raw("dispatch", "op-a", "a"))
    assert h.data("operation", "op-a")["business_state"] == "pending"
    assert h.raw("dispatch", "op-a", "a")["material"] is None


def test_secrets_split_across_stream_chunks_never_escape_or_get_checkpointed(caplog):
    raw = ('{"access_token":"' + TOKEN + '"}').encode()
    spec = SPEC.model_copy(update={"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    h = DispatchHarness(spec=spec, send=lambda _: response(
        spec=spec, chunks=[raw[:24], raw[24:31], raw[31:]],
    )).ready()
    with pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == "OPERATION_UNSAFE_RESULT"
    assert TOKEN not in caplog.text
    for value in h.client.backing.rows.values():
        assert TOKEN.encode() not in value["Payload"]
    with pytest.raises(Problem):
        h.execute()
    assert len(h.requests) == 1


def test_partial_stream_and_oversize_body_retain_unknown_without_persisting_fragments():
    for chunks, expected in (
        ([DATA[:10], httpx.ReadError(TOKEN)], "OPERATION_TRANSPORT_UNKNOWN"),
        ([b"x" * (SPEC.bytes + 1)], "OPERATION_RESPONSE_TOO_LARGE"),
        ([DATA[:-1]], "OPERATION_RESPONSE_INVALID"),
    ):
        h = DispatchHarness(send=lambda _: response(chunks=chunks)).ready()
        with pytest.raises(Problem) as error:
            h.execute()
        assert code(error) == expected
        assert TOKEN not in str(error.value.body)
        assert h.raw("dispatch", "op-a", "a")["material"] is None
        assert h.data("attempt", "op-a", "a")["effect_state"] == "unknown"
        assert h.data("grant", "grant")["remaining_uses"] == 0


@pytest.mark.parametrize("source", ["credential", "transport", "stream"])
def test_untrusted_problem_exception_never_forwards_secret_code_message_or_details(source):
    hostile = Problem(502, TOKEN, TOKEN, token=TOKEN, unexpected_field=TOKEN)

    def fail(*_):
        raise hostile

    h = DispatchHarness(send=fail if source == "transport" else None).ready()
    if source == "credential":
        h.concrete._token = fail
    elif source == "stream":
        h.send = lambda _: response(chunks=[hostile])
    with pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == "OPERATION_TRANSPORT_UNKNOWN"
    assert TOKEN not in str(error.value.body) and TOKEN not in str(error.value)
    assert h.raw("dispatch", "op-a", "a")["material"] is None


def test_total_stream_deadline_is_bounded_not_reset_for_each_chunk():
    h = DispatchHarness().ready()
    with patch("shared_context.behavior_dispatch.time.monotonic", side_effect=[0, 11]):
        with pytest.raises(Problem) as error:
            h.execute()
    assert code(error) == "OPERATION_RESPONSE_TIMEOUT"
    assert len(h.requests) == 1 and h.data("dispatch", "op-a", "a")["phase"] == "consumed"


def test_adapter_does_not_claim_unsupported_write_retry_or_evidence_capabilities():
    h = DispatchHarness().ready()
    contract = h.adapter.contract
    assert contract.allowed_methods == ("GET",) and contract.allowed_kinds == ("request",)
    assert contract.terminality_contract_version is None and contract.idempotency_contract_version is None
    assert h.adapter.verify_outcome is None and h.adapter.verify_idempotency is None
    disabled = ToolBrokerCore(h.store, clock=lambda: NOW, adapter=h.adapter)
    with pytest.raises(Problem) as error:
        disabled.execute("op-a", "a")
    assert code(error) == "OPERATION_EXECUTION_DISABLED"
    with pytest.raises(Problem):
        ToolBrokerCore(h.store, clock=lambda: NOW, adapter=replace(h.adapter), execution_adapter=h.concrete)
    for field in ("approved", "shell", "credentials", "url", "retry"):
        with pytest.raises(ValidationError):
            ToolIntent(operation_id="op-a", input_ref="current", **{field: True})


@pytest.mark.parametrize("origin", [
    "http://context.example", "https://user:password@context.example",
    "https://context.example/path", "https://context.example?access_token=secret",
    "https://context.example#fragment", "https://context.example/",
])
def test_only_trusted_exact_origin_and_routes_can_be_configured(origin):
    with pytest.raises(ValidationError):
        ContextArtifactSettings(api_origin=origin, principal_id="principal", routes=(ROUTE,))
    with pytest.raises(ValidationError):
        ArtifactReadSpec(**{**SPEC.model_dump(), "if_match": '"' + TOKEN + '"'})


def test_real_client_creates_native_transport_with_zero_retries_and_no_ambient_environment():
    h = DispatchHarness().ready()
    h.concrete._transport = None
    native = httpx.MockTransport(h.transport)
    with patch("shared_context.behavior_dispatch.httpx.HTTPTransport", return_value=native) as factory:
        h.execute()
    factory.assert_called_once_with(retries=0)
    assert len(h.requests) == 1


def test_exact_download_uses_real_m3_service_and_conditional_blob_read(m3):
    service, _, blob = m3
    create(service)
    upload(service, data=DATA)
    version = service.version(A, A.team, "report", V1)
    spec = ArtifactReadSpec(
        **ROUTE.model_dump(), if_match=version.blob.etag, sha256=version.sha256, bytes=version.blob.bytes,
    )
    app = TestClient(create_app(
        service.context, lambda: A, artifact_service=service,
        verified_artifact_identity_dependency=lambda: A,
    ))

    def local(request):
        result = app.request(request.method, request.url.path, headers=dict(request.headers), content=request.content)
        return httpx.Response(result.status_code, headers=result.headers, stream=Chunks([result.content]))

    h = DispatchHarness(spec=spec, send=local).ready()
    # A new logical version must not redirect the consumed v1 read.
    upload(service, version_id=V2, data=b"new current version B")
    with patch.object(blob, "read", wraps=blob.read) as read:
        result = h.execute()
    assert result.payload["result"]["safe_result"]["data"] == METRICS.model_dump()
    read.assert_called_once_with(A.team, "report", V1, version.blob)


@pytest.mark.parametrize("failure", ["version", "iam"])
def test_real_m3_destination_enforces_version_and_current_actor_before_releasing_bytes(m3, failure):
    service, _, blob = m3
    create(service)
    upload(service, data=DATA)
    version = service.version(A, A.team, "report", V1)
    spec = ArtifactReadSpec(
        **ROUTE.model_dump(), if_match=version.blob.etag, sha256=version.sha256, bytes=version.blob.bytes,
    )
    if failure == "version":
        spec = spec.model_copy(update={"if_match": '"0xBAD"'})
    app = TestClient(create_app(
        service.context, lambda: A, artifact_service=service,
        verified_artifact_identity_dependency=lambda: A,
    ))

    def local(request):
        if failure == "iam":
            service.context.auth.actors.pop(A.oid)
        result = app.request(request.method, request.url.path, headers=dict(request.headers))
        assert result.status_code == (412 if failure == "version" else 403)
        return httpx.Response(result.status_code, headers=result.headers, stream=Chunks([result.content]))

    h = DispatchHarness(spec=spec, send=local).ready()
    with patch.object(blob, "read", wraps=blob.read) as read, pytest.raises(Problem) as error:
        h.execute()
    assert code(error) == "OPERATION_DESTINATION_REJECTED"
    read.assert_not_called()
    assert h.data("dispatch-obligation", "op-a", "a")["state"] == "pending_reconciliation"
