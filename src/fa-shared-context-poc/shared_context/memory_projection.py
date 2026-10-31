"""Native Memory is a receipt-linked retrieval projection, never an authority.

Trusted service wiring: NativeMemoryProjection(governance, project_client, setup).
ProjectionSetup is SERVER configuration: existing store, explicit fresh namespace,
BehaviorStore binding, runtime binding, source scope, subject, necessary claim IDs,
context versions, configuration evidence fingerprint and finite budgets. Do not
deserialize setup from model/HTTP fields. No store is created. ``frp1-`` scopes
include the entire binding and exact projection digest, never old M4 ``sc1-`` scopes.

plan(context) durably saves the original plan. Its Record has projection_id.
start/poll/reconcile take that ID; search/assemble additionally accept only a
ProjectionQuery. All service results are Records; errors are Problems. Decode
assemble's data as ProjectionContext (original bytes use base64 JSON encoding).
An HTTP adapter must authenticate/map context and apply its current binding gate;
these methods are trusted-service seams, not additional public authorization.

Every mutation uses governance's SAME guard/snapshot authority. An update or
search intent is consumed before transport, including when the control ack is
lost. Only the invocation that definitely committed a fresh intent can send.
Reconciliation is read-only. There is no retry, repair, supplied update ID,
continuation token, or background poller. A crash before sending can therefore
leave an unknown forever; this is deliberate, not permission to resubmit.
Poll slots are durable and finite.

SDK contract: azure-ai-projects 2.3.0 BetaMemoryStoresOperations public
begin_update_memories(polling=False), search_memories; authenticated send_request
GET uses the same v1 update URL as its shipped _patch_memories poller. Inspecting
that SDK confirms NoPolling.result() can manufacture empty output: never use it.
Native successful receipts are retained losslessly within explicit bounds.
Unknown schema/oversize/secret-bearing success fails, rather than truncating.
Failed-provider text is not retained. Exact reviewed text plus the SAME native
item in the original completion receipt is the only admissible candidate;
similarity, paraphrases, procedural memories and receipts confer no permission.

Local SDK/transport tests are offline_contract only. actual_environment=NOT_RUN.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.core.rest import HttpRequest
from pydantic import Field, JsonValue, TypeAdapter, ValidationError, model_validator

from .behavior_operations import control_key
from .behavior_store import ControlChanges, ControlRequest, Digest
from .errors import Problem
from .memory import FEATURE_HEADERS, UPDATE_ID_PATTERN
from .memory_governance import (
    ClaimReference, ContextManifest, ContextSection, ContextVersions,
    MemoryGovernance, ReviewedClaim,
)
from .models import Identifier
from .records import Record
from .source_materials import (
    AuthenticatedSourceContext, SourceModel, SourceReference, SourceScope,
    authorize, canonical, checked, control_row, control_write, digest, utc,
)

NativeID = Annotated[str, Field(pattern=UPDATE_ID_PATTERN)]
Count = Annotated[int, Field(ge=0, le=2**53 - 1)]
ProjectionPhase = Literal["planned", "outcome_unknown", "accepted", "completed", "failed", "superseded"]
_NO_RETRY = {
    "retry_total": 0, "retry_connect": 0, "retry_read": 0, "retry_status": 0,
    "redirect_max": 0, "logging_enable": False, "connection_timeout": 10, "read_timeout": 30,
}
_SECRET = re.compile(
    r"(?i)(?:bearer\s+\S+|eyJ[\w-]+\.[\w-]+\.[\w-]+|"
    r"(?:accountkey|client_secret|access_token|refresh_token|password)\s*[=:]\s*\S+|[?&]sig=)"
)


class ProjectionSetup(SourceModel):
    control_binding: Digest
    binding_id: Identifier
    endpoint: Annotated[str, Field(min_length=1, max_length=2048)]
    store: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
    projection_namespace: Annotated[str, Field(pattern=r"^fa-reviewed-v1-[A-Za-z0-9_-]{1,48}$")]
    evidence_sha256: Digest
    scope: SourceScope
    subject: Identifier
    claim_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=24)]
    versions: ContextVersions
    max_polls: Annotated[int, Field(ge=1, le=32)]
    max_searches: Annotated[int, Field(ge=1, le=32)]
    max_input_tokens: Annotated[int, Field(ge=1, le=32768)]
    max_query_tokens: Annotated[int, Field(ge=1, le=8192)]
    max_input_bytes: Annotated[int, Field(ge=1, le=16384)]
    max_response_bytes: Annotated[int, Field(ge=1, le=32768)]

    @model_validator(mode="after")
    def configuration(self):
        endpoint = urlsplit(self.endpoint)
        if (endpoint.scheme != "https" or not endpoint.netloc or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment or self.endpoint.endswith("/")):
            raise ValueError("canonical HTTPS project endpoint required")
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError("unique necessary claim IDs required")
        return self

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))


class ProjectionQuery(SourceModel):
    query: Annotated[str, Field(min_length=1, max_length=8192)]


class ProjectionDocument(SourceModel):
    setup: ProjectionSetup
    covered_sequence: Annotated[int, Field(ge=1)]
    claims: Annotated[tuple[ClaimReference, ...], Field(min_length=1, max_length=24)]
    sources: Annotated[tuple[SourceReference, ...], Field(min_length=1, max_length=24)]
    source_fingerprint: Digest
    source_covered_sequences: dict[str, int]
    source_access_revisions: dict[str, int]
    input_text: Annotated[str, Field(min_length=1, max_length=16384)]
    input_tokens: Annotated[int, Field(ge=1)]

    @property
    def projection_id(self):
        return digest(self.model_dump(mode="json"))

    @property
    def native_scope(self):
        return "frp1-" + self.projection_id


class NativeProjectionPlan(SourceModel):
    document: ProjectionDocument
    request: ControlRequest


class ProjectionState(SourceModel):
    projection_id: Digest
    phase: ProjectionPhase
    update_id: NativeID | None = None
    start_consumed: bool = False
    polls_consumed: Annotated[int, Field(ge=0, le=32)] = 0
    searches_consumed: Annotated[int, Field(ge=0, le=32)] = 0
    completion_receipt_key: Identifier | None = None
    last_issue: Literal["response-unknown", "native-schema-invalid", "provider-failed", "provider-superseded"] | None = None
    request: ControlRequest

    @model_validator(mode="after")
    def state_contract(self):
        if (self.phase == "planned") == self.start_consumed:
            raise ValueError("only planned has an unconsumed update intent")
        if self.phase in {"accepted", "completed", "failed", "superseded"} and self.update_id is None:
            raise ValueError("known native status requires a native ID")
        if (self.phase == "completed") != (self.completion_receipt_key is not None):
            raise ValueError("completion needs its original receipt")
        return self


class _InputDetails(SourceModel):
    cached_tokens: Count


class _OutputDetails(SourceModel):
    reasoning_tokens: Count


class NativeUsage(SourceModel):
    embedding_tokens: Count
    input_tokens: Count
    output_tokens: Count
    total_tokens: Count
    input_tokens_details: _InputDetails | None = None
    output_tokens_details: _OutputDetails | None = None


class NativeItem(SourceModel):
    memory_id: NativeID
    updated_at: datetime | Annotated[int, Field(ge=0, le=253402300799)]
    scope: NativeID
    content: Annotated[str, Field(min_length=1, max_length=16384)]
    kind: Literal["user_profile", "chat_summary", "procedural"]

    @model_validator(mode="after")
    def timestamp(self):
        if isinstance(self.updated_at, datetime) and (
            self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None
        ):
            raise ValueError("native timestamp needs an offset")
        return self


class _MemoryOperation(SourceModel):
    # Native receipts use "created"/"updated"; SDK 2.3.0 declares
    # "create"/"update". Retain each supported spelling without normalization.
    kind: Literal["create", "created", "update", "updated", "delete"]
    memory_item: NativeItem


class _Completed(SourceModel):
    memory_operations: Annotated[tuple[_MemoryOperation, ...], Field(max_length=24)]
    usage: NativeUsage


class _NativeUpdate(SourceModel):
    update_id: NativeID
    status: Literal["queued", "in_progress", "completed"]
    result: _Completed | None = None
    superseded_by: None = None
    error: None = None

    @model_validator(mode="after")
    def completion(self):
        if (self.status == "completed") != (self.result is not None):
            raise ValueError("completion must have its real result")
        return self


class _SearchItem(SourceModel):
    memory_item: NativeItem


class _NativeSearch(SourceModel):
    search_id: NativeID
    memories: Annotated[tuple[_SearchItem, ...], Field(max_length=10)]
    usage: NativeUsage
    error: None = None


class NativeReceipt(SourceModel):
    projection_id: Digest
    operation: Literal["start", "poll"]
    ordinal: Annotated[int, Field(ge=0, le=32)]
    native: dict[str, JsonValue]
    native_sha256: Digest
    sanitized: bool
    request: ControlRequest


class ProjectionCandidate(SourceModel):
    memory_item: NativeItem
    claim: ClaimReference
    supporting: tuple[SourceReference, ...]
    completion_receipt_key: Identifier
    trust: Literal["retrieval-candidate-not-authority"] = "retrieval-candidate-not-authority"


class _SearchIntent(SourceModel):
    projection_id: Digest
    query_sha256: Digest
    query_tokens: Annotated[int, Field(ge=1)]
    ordinal: Annotated[int, Field(ge=1, le=32)]
    request: ControlRequest


class ProjectionSearch(SourceModel):
    projection_id: Digest
    query_sha256: Digest
    search_id: NativeID
    candidates: tuple[ProjectionCandidate, ...]
    usage: NativeUsage
    omissions: tuple[str, ...]
    degraded: bool
    request: ControlRequest


class ProjectionContext(SourceModel):
    projection_id: Digest
    native_state: ProjectionPhase
    completion_receipt_key: Identifier | None
    manifest: ContextManifest
    claims: tuple[ReviewedClaim, ...]
    originals: dict[str, bytes]
    summary: str | None
    degraded: bool
    trust: Literal["retrieval-candidate-not-authority"] = "retrieval-candidate-not-authority"


def _key(kind: str, projection_id: str, *parts: str):
    return control_key("native-memory-" + kind, projection_id, *parts)


def _problem(code: str, status=502):
    return Problem(status, code, "native projection unavailable; retain original intent and exact source authority")


def _parse(value, model):
    try:
        return model.model_validate_json(canonical(value))
    except (TypeError, ValueError, UnicodeError, ValidationError):
        raise _problem("MEMORY_NATIVE_SCHEMA") from None


class _SingleRequest:
    def __init__(self, method, url, before_send=None):
        self.method, self.url, self.before_send, self.sent = method, url, before_send, 0

    def __call__(self, response):
        request = response.http_request
        if self.sent or request.method != self.method or request.url != self.url:
            raise _problem("MEMORY_NATIVE_EXTRA_REQUEST")
        if self.before_send:
            self.before_send()
        self.sent += 1


class NativeMemoryProjection:
    def __init__(self, governance: MemoryGovernance, client: AIProjectClient, setup: ProjectionSetup):
        self.setup = checked(setup, ProjectionSetup)
        if not isinstance(governance, MemoryGovernance) or not isinstance(client, AIProjectClient):
            raise _problem("MEMORY_PROJECTION_CONFIGURATION", 503)
        self.governance, self.sources = governance, governance.sources
        self.store, self.read_snapshot = governance.store, governance.read_snapshot
        self.client = client
        if (setup.control_binding != self.store._binding
                or client._config.endpoint.rstrip("/") != setup.endpoint):
            raise _problem("MEMORY_PROJECTION_CONFIGURATION", 503)

    def _authorize(self, context):
        authorize(context, self.setup.scope)

    def _request(self, kind, projection_id, value, keys):
        keys = tuple(dict.fromkeys(keys))
        if len(keys) > 98:
            raise _problem("MEMORY_PROJECTION_PREDICATE_LIMIT", 413)
        return ControlRequest(
            request_id=_key("request", projection_id, kind),
            request_digest=digest(value), read_set=keys,
        )

    def _graph(self, context):
        self._authorize(context)
        g, setup = self.governance, self.setup
        state = g._state(context, setup.scope, setup.subject)
        claims = tuple(g.get_current(context, scope=setup.scope, subject=setup.subject, claim_id=claim_id)
                       for claim_id in sorted(setup.claim_ids))
        originals = g._source_set(context, tuple(ref for claim in claims for ref in claim.candidate.supporting),
                                  setup.scope)
        keys = tuple(dict.fromkeys(
            [state.key] + [claim.reference.key for claim in claims] + g._source_keys(originals)
            + [key for claim in claims for key in g._review_keys(context, claim.candidate, claim.review)]
        ))
        if len(keys) > 98:
            raise _problem("MEMORY_PROJECTION_PREDICATE_LIMIT", 413)
        self._guard_graph(context, self.read_snapshot(keys), state, claims, originals)
        return state, claims, originals, keys

    def _guard_graph(self, context, rows, state, claims, originals):
        g = self.governance
        if g._state(context, self.setup.scope, self.setup.subject, rows) != state:
            raise _problem("MEMORY_PROJECTION_STALE", 409)
        for claim in claims:
            head = g._active(state, claim.reference.claim_id)
            if (head.reference != claim.reference or head.sequence != claim.sequence
                    or control_row(rows, claim.reference.key, ReviewedClaim) != claim):
                raise _problem("MEMORY_PROJECTION_STALE", 409)
            now = utc(g._clock())
            if (now < utc(claim.candidate.effective_from)
                    or claim.candidate.expires_at is not None and now >= utc(claim.candidate.expires_at)):
                raise _problem("MEMORY_PROJECTION_STALE", 409)
            g._review_predicates(context, rows, claim.candidate, claim.review)
        g._source_predicates(rows, originals)

    def _document(self, state, claims, originals):
        text = canonical({"claims": [
            {"reference": c.reference.model_dump(mode="json"), "kind": c.candidate.kind,
             "text": c.candidate.text, "supporting": [r.model_dump(mode="json") for r in c.candidate.supporting]}
            for c in claims
        ]}).decode("utf-8")
        tokens = self.governance._tokens(text.encode("utf-8"))
        if len(text.encode("utf-8")) > self.setup.max_input_bytes or tokens > self.setup.max_input_tokens:
            raise _problem("MEMORY_PROJECTION_INPUT_BUDGET", 413)
        return ProjectionDocument(
            setup=self.setup, covered_sequence=state.sequence, claims=tuple(c.reference for c in claims),
            sources=self.governance._projection_sources(originals),
            source_fingerprint=self.sources.evidence_digest(originals),
            source_covered_sequences={item.event.reference.scope.key: max(
                other.event.sequence for other in originals.values()
                if other.event.reference.scope == item.event.reference.scope) for item in originals.values()},
            source_access_revisions={key: item.access_revision for key, item in originals.items()},
            input_text=text, input_tokens=tokens,
        )

    def _current(self, context, plan):
        graph = self._graph(context)
        if self._document(*graph[:3]) != plan.document:
            raise _problem("MEMORY_PROJECTION_STALE", 409)
        return graph

    def _load(self, context, projection_id):
        self._authorize(context)
        try:
            TypeAdapter(Digest).validate_python(projection_id, strict=True)
        except ValidationError:
            raise _problem("MEMORY_PROJECTION_ID", 422) from None
        pk, sk = _key("plan", projection_id), _key("state", projection_id)
        rows = self.read_snapshot((pk, sk))
        plan = control_row(rows, pk, NativeProjectionPlan)
        state = control_row(rows, sk, ProjectionState)
        if (plan.document.setup != self.setup or plan.document.projection_id != projection_id
                or state.projection_id != projection_id):
            raise _problem("MEMORY_PROJECTION_BINDING", 403)
        self.sources.verify_receipt(rows[pk], plan.request, pk)
        self.sources.verify_receipt(rows[sk], state.request, sk)
        return plan, state, rows[sk]

    @staticmethod
    def _result(state, record):
        return Record(state.model_dump(mode="json"), record.etag)

    def plan(self, context: AuthenticatedSourceContext) -> Record:
        state, claims, originals, keys = self._graph(context)
        document = self._document(state, claims, originals)
        pid = document.projection_id
        pk, sk = _key("plan", pid), _key("state", pid)
        request = self._request("plan", pid, document.model_dump(mode="json"), (pk, sk, *keys))
        plan = NativeProjectionPlan(document=document, request=request)

        def decide(rows):
            self._guard_graph(context, rows, state, claims, originals)
            return ControlChanges(writes=(
                control_write(pk, plan),
                control_write(sk, ProjectionState(projection_id=pid, phase="planned", request=request)),
            ), result={"projection_id": pid, "phase": "planned", "native_scope": document.native_scope})

        return self.store.transact(request, decide)

    def reconcile(self, context: AuthenticatedSourceContext, projection_id: str) -> Record:
        _, state, record = self._load(context, projection_id)
        return self._result(state, record)

    def _reserve(self, context, plan, state, *, kind, extra=()):
        pid, won = plan.document.projection_id, False
        pk, sk = _key("plan", pid), _key("state", pid)
        graph = self._current(context, plan) if kind == "start" else None
        keys = graph[3] if graph else ()
        request = self._request(kind, pid, [plan.document.projection_id, kind], (pk, sk, *keys, *extra))

        def decide(rows):
            nonlocal won
            if control_row(rows, pk, NativeProjectionPlan) != plan or control_row(rows, sk, ProjectionState) != state:
                raise _problem("MEMORY_PROJECTION_CONFLICT", 409)
            if graph:
                self._guard_graph(context, rows, *graph[:3])
            changes = {"request": request}
            if kind == "start":
                if state.start_consumed:
                    raise _problem("MEMORY_NATIVE_UNKNOWN", 409)
                changes.update(start_consumed=True, phase="outcome_unknown", last_issue="response-unknown")
            elif kind.startswith("poll-"):
                if state.polls_consumed >= self.setup.max_polls:
                    raise _problem("MEMORY_POLL_BUDGET_EXHAUSTED", 409)
                changes["polls_consumed"] = state.polls_consumed + 1
            updated = ProjectionState.model_validate({**state.model_dump(), **changes})
            won = True
            return ControlChanges(writes=(control_write(sk, updated, "replace"),),
                                  result={"projection_id": pid, "phase": updated.phase, "intent": kind})

        self.store.transact(request, decide)
        return won

    def _url(self, suffix):
        separator = "/" if suffix.startswith("updates/") else ":"
        return f"{self.setup.endpoint}/memory_stores/{quote(self.setup.store, safe='')}{separator}{suffix}?api-version=v1"

    def _bounded(self, raw):
        try:
            data = canonical(raw)
        except (TypeError, ValueError, UnicodeError):
            raise _problem("MEMORY_NATIVE_SCHEMA") from None
        if len(data) > self.setup.max_response_bytes:
            raise _problem("MEMORY_NATIVE_SCHEMA")
        return data

    def _update(self, raw, expected_id=None, *, initial=False):
        self._bounded(raw)
        if not isinstance(raw, dict):
            raise _problem("MEMORY_NATIVE_SCHEMA")
        try:
            update_id = TypeAdapter(NativeID).validate_python(raw.get("update_id"), strict=True)
        except ValidationError:
            raise _problem("MEMORY_NATIVE_SCHEMA") from None
        if expected_id is not None and update_id != expected_id:
            raise _problem("MEMORY_NATIVE_SCHEMA")
        status = raw.get("status")
        if initial and status not in {"queued", "in_progress"}:
            # A 202 never completes the operation, even if its body says otherwise.
            raise _problem("MEMORY_NATIVE_SCHEMA")
        if status in {"failed", "superseded"}:
            return {"update_id": update_id, "status": status}, True
        native = _parse(raw, _NativeUpdate)
        if native.result and any(_SECRET.search(op.memory_item.content) for op in native.result.memory_operations):
            raise _problem("MEMORY_NATIVE_SCHEMA")
        return raw, False

    def _observe(self, context, plan, operation, ordinal, native=None, sanitized=False, issue=None):
        pid = plan.document.projection_id
        pk, sk, rk = _key("plan", pid), _key("state", pid), _key("receipt", pid, operation, str(ordinal))
        value = {"native": native, "sanitized": sanitized, "issue": issue}
        request = self._request(f"observe-{operation}-{ordinal}", pid, value, (pk, sk, rk))

        def decide(rows):
            if control_row(rows, pk, NativeProjectionPlan) != plan:
                raise _problem("MEMORY_PROJECTION_BINDING", 403)
            prior = control_row(rows, sk, ProjectionState)
            changes = {"request": request, "last_issue": issue}
            writes = []
            if native is not None:
                if prior.update_id is not None and native["update_id"] != prior.update_id:
                    raise _problem("MEMORY_NATIVE_SCHEMA")
                status = native["status"]
                phase = "accepted" if status in {"queued", "in_progress"} else status
                if prior.phase not in {"completed", "failed", "superseded"}:
                    changes.update(phase=phase, update_id=native["update_id"])
                    if phase == "completed":
                        changes["completion_receipt_key"] = rk
                elif phase not in {"accepted", prior.phase}:
                    raise _problem("MEMORY_NATIVE_TERMINAL_CONFLICT")
                writes.append(control_write(rk, NativeReceipt(
                    projection_id=pid, operation=operation, ordinal=ordinal, native=native,
                    native_sha256=digest(native), sanitized=sanitized, request=request,
                )))
            updated = ProjectionState.model_validate({**prior.model_dump(), **changes})
            writes.append(control_write(sk, updated, "replace"))
            return ControlChanges(writes=tuple(writes), result={"projection_id": pid, "phase": updated.phase})

        self._authorize(context)
        self.store.transact(request, decide)
        return self.reconcile(context, pid)

    def _native_error(self, context, plan, operation, ordinal, exc):
        issue = "native-schema-invalid" if isinstance(exc, Problem) and exc.body["error"]["code"] in {
            "MEMORY_NATIVE_SCHEMA", "MEMORY_NATIVE_EXTRA_REQUEST",
        } else "response-unknown"
        self._observe(context, plan, operation, ordinal, issue=issue)
        raise _problem("MEMORY_NATIVE_SCHEMA" if issue == "native-schema-invalid" else "MEMORY_NATIVE_UNKNOWN") from None

    def start(self, context: AuthenticatedSourceContext, projection_id: str) -> Record:
        plan, state, record = self._load(context, projection_id)
        if state.start_consumed:
            return self._result(state, record)
        if not self._reserve(context, plan, state, kind="start"):
            return self.reconcile(context, projection_id)
        initial = []
        gate = _SingleRequest("POST", self._url("update_memories"), lambda: self._current(context, plan))

        def capture(response):
            wire = response.http_response
            if wire.status_code == 202:
                wire.read()
                initial.append(wire.json())

        try:
            self.client.beta.memory_stores.begin_update_memories(
                name=self.setup.store, scope=plan.document.native_scope,
                items=[{"role": "user", "type": "message",
                        "content": [{"type": "input_text", "text": plan.document.input_text}]}],
                update_delay=0, polling=False, headers=FEATURE_HEADERS,
                raw_request_hook=gate, raw_response_hook=capture, **_NO_RETRY,
            )
            if gate.sent != 1 or len(initial) != 1:
                raise _problem("MEMORY_NATIVE_SCHEMA")
            native, sanitized = self._update(initial[0], initial=True)
        except (AzureError, Problem, OSError, ValueError, TypeError) as exc:
            return self._native_error(context, plan, "start", 0, exc)
        return self._observe(context, plan, "start", 0, native, sanitized)

    def poll(self, context: AuthenticatedSourceContext, projection_id: str) -> Record:
        plan, state, record = self._load(context, projection_id)
        if state.phase in {"completed", "failed", "superseded"}:
            return self._result(state, record)
        if state.update_id is None:
            raise _problem("MEMORY_NATIVE_UNKNOWN", 409)
        ordinal = state.polls_consumed + 1
        if not self._reserve(context, plan, state, kind=f"poll-{ordinal}"):
            return self.reconcile(context, projection_id)
        url = self._url("updates/" + quote(state.update_id, safe=""))
        gate = _SingleRequest("GET", url)
        try:
            response = self.client.send_request(HttpRequest("GET", url, headers=FEATURE_HEADERS),
                                                raw_request_hook=gate, **_NO_RETRY)
            if response.status_code != 200 or gate.sent != 1:
                raise _problem("MEMORY_NATIVE_READ_FAILED")
            native, sanitized = self._update(response.json(), state.update_id)
        except (AzureError, Problem, OSError, ValueError, TypeError) as exc:
            return self._native_error(context, plan, "poll", ordinal, exc)
        issue = {"failed": "provider-failed", "superseded": "provider-superseded"}.get(native["status"])
        result = self._observe(context, plan, "poll", ordinal, native, sanitized, issue)
        if issue:
            raise _problem("MEMORY_NATIVE_FAILED")
        return result

    def _receipt(self, state):
        key = state.completion_receipt_key
        rows = self.read_snapshot((key,))
        receipt = control_row(rows, key, NativeReceipt)
        self.sources.verify_receipt(rows[key], receipt.request, key)
        if (receipt.projection_id != state.projection_id or receipt.sanitized
                or receipt.native_sha256 != digest(receipt.native)):
            raise _problem("MEMORY_NATIVE_SCHEMA")
        parsed, _ = self._update(receipt.native, state.update_id)
        native = _parse(parsed, _NativeUpdate)
        if native.status != "completed":
            raise _problem("MEMORY_NATIVE_SCHEMA")
        return native

    def _candidates(self, plan, state, native, claims):
        receipt = self._receipt(state)
        operations = receipt.result.memory_operations
        original_items = [
            op.memory_item for op in operations if op.kind in {"create", "created", "update", "updated"}
            and sum(other.memory_item.memory_id == op.memory_item.memory_id for other in operations) == 1
        ]
        found, omissions = [], []
        for hit in native.memories:
            item = hit.memory_item
            exact = [claim for claim in claims if claim.candidate.text == item.content]
            if item.scope != plan.document.native_scope:
                omissions.append("native-scope-mismatch: candidate excluded")
            elif item not in original_items:
                omissions.append("native-receipt-lineage-mismatch: candidate excluded")
            elif len(exact) != 1:
                omissions.append("native-not-exact-reviewed-claim: candidate excluded; paraphrase needs trusted review")
            elif item.kind == "procedural":
                omissions.append("native-procedural-data: not policy or permission")
            elif not any(candidate.claim == exact[0].reference for candidate in found):
                found.append(ProjectionCandidate(
                    memory_item=item, claim=exact[0].reference, supporting=exact[0].candidate.supporting,
                    completion_receipt_key=state.completion_receipt_key,
                ))
        if {c.claim for c in found} != {c.reference for c in claims}:
            omissions.append("native-required-fact-omitted: using exact reviewed claims and original sources")
        return tuple(found), tuple(dict.fromkeys(omissions))

    def search(self, context: AuthenticatedSourceContext, projection_id: str, query: ProjectionQuery) -> Record:
        query = checked(query, ProjectionQuery)
        plan, state, _ = self._load(context, projection_id)
        graph = self._current(context, plan)
        if state.phase != "completed":
            raise _problem("MEMORY_NATIVE_NOT_COMPLETE", 409)
        tokens = self.governance._tokens(query.query.encode("utf-8"))
        if tokens > self.setup.max_query_tokens:
            raise _problem("MEMORY_QUERY_BUDGET", 413)
        qid = digest(query.model_dump(mode="json"))
        ik, rk = _key("search-intent", projection_id, qid), _key("search", projection_id, qid)
        prior = self.read_snapshot((ik, rk))
        if prior[rk] is not None:
            result = control_row(prior, rk, ProjectionSearch)
            self.sources.verify_receipt(prior[rk], result.request, rk)
            self._current(context, plan)
            return Record(result.model_dump(mode="json"), prior[rk].etag)
        if prior[ik] is not None:
            raise _problem("MEMORY_SEARCH_UNKNOWN", 409)
        sk, pk = _key("state", projection_id), _key("plan", projection_id)
        ordinal, won = state.searches_consumed + 1, False
        request = self._request("search-" + qid, projection_id, [qid, tokens], (pk, sk, ik, rk, *graph[3]))

        def reserve(rows):
            nonlocal won
            if control_row(rows, sk, ProjectionState) != state or control_row(rows, pk, NativeProjectionPlan) != plan:
                raise _problem("MEMORY_PROJECTION_CONFLICT", 409)
            if ordinal > self.setup.max_searches:
                raise _problem("MEMORY_SEARCH_BUDGET_EXHAUSTED", 409)
            self._guard_graph(context, rows, *graph[:3])
            intent = _SearchIntent(projection_id=projection_id, query_sha256=qid, query_tokens=tokens,
                                   ordinal=ordinal, request=request)
            updated = ProjectionState.model_validate({
                **state.model_dump(), "searches_consumed": ordinal, "request": request,
            })
            won = True
            return ControlChanges(writes=(control_write(ik, intent), control_write(sk, updated, "replace")),
                                  result={"projection_id": projection_id, "query_sha256": qid, "phase": "search-unknown"})

        self.store.transact(request, reserve)
        if not won:
            raise _problem("MEMORY_SEARCH_UNKNOWN", 409)
        gate = _SingleRequest("POST", self._url("search_memories"), lambda: self._current(context, plan))
        raw = []

        def capture(response):
            if response.http_response.status_code == 200:
                response.http_response.read()
                raw.append(response.http_response.json())

        try:
            self.client.beta.memory_stores.search_memories(
                name=self.setup.store, scope=plan.document.native_scope, items=query.query,
                options={"max_memories": 10}, headers=FEATURE_HEADERS, raw_request_hook=gate,
                raw_response_hook=capture, **_NO_RETRY,
            )
            if gate.sent != 1 or len(raw) != 1:
                raise _problem("MEMORY_NATIVE_SCHEMA")
            self._bounded(raw[0])
            native = _parse(raw[0], _NativeSearch)
        except (AzureError, Problem, OSError, ValueError, TypeError):
            raise _problem("MEMORY_SEARCH_UNKNOWN") from None
        graph = self._current(context, plan)
        candidates, omissions = self._candidates(plan, state, native, graph[1])
        save = self._request("search-result-" + qid, projection_id, {
            "query": qid, "native_sha256": digest(raw[0]),
        }, (ik, rk, *graph[3]))
        result = ProjectionSearch(
            projection_id=projection_id, query_sha256=qid, search_id=native.search_id,
            candidates=candidates, usage=native.usage, omissions=omissions,
            degraded=bool(omissions), request=save,
        )

        def complete(rows):
            intent = control_row(rows, ik, _SearchIntent)
            if intent.request != request or intent.query_sha256 != qid:
                raise _problem("MEMORY_SEARCH_UNKNOWN")
            self._guard_graph(context, rows, *graph[:3])
            return ControlChanges(writes=(control_write(rk, result),),
                                  result={"projection_id": projection_id, "search_key": rk, "degraded": result.degraded})

        receipt = self.store.transact(save, complete)
        return Record(result.model_dump(mode="json"), receipt.etag)

    def assemble(self, context: AuthenticatedSourceContext, projection_id: str,
                 query: ProjectionQuery | None = None) -> Record:
        plan, state, record = self._load(context, projection_id)
        g, setup = self.governance, self.setup
        exact = g.assemble(context, scope=setup.scope, subject=setup.subject,
                           required_claim_ids=tuple(sorted(setup.claim_ids)), versions=setup.versions)
        manifest, summary = exact.manifest, None
        omissions = list(manifest.omissions)
        current = (manifest.covered_sequence == plan.document.covered_sequence
                   and manifest.claims == plan.document.claims and manifest.sources == plan.document.sources
                   and manifest.source_access_revisions == plan.document.source_access_revisions)
        if not current:
            omissions.append("native-projection-stale: using current exact reviewed claims and original sources")
        elif state.phase != "completed":
            omissions.append("native-" + state.phase + ": using exact reviewed claims and original sources")
        elif query is None:
            omissions.append("native-not-searched: explicit exact-source degraded context")
        else:
            try:
                search = _parse(self.search(context, projection_id, query).payload, ProjectionSearch)
            except Problem as error:
                if error.body["error"]["code"] != "MEMORY_SEARCH_UNKNOWN":
                    raise
                search = None
                omissions.append("native-search-unknown: retained original search intent; using exact reviewed claims and sources")
            if search is not None:
                omissions.extend(search.omissions)
            by_claim = {} if search is None else {candidate.claim.key: candidate for candidate in search.candidates}
            if all(claim.reference.key in by_claim for claim in exact.claims):
                text = "\n".join(by_claim[claim.reference.key].memory_item.content for claim in exact.claims)
                data = text.encode("utf-8")
                tokens = g._tokens(data)
                if manifest.total_tokens + tokens <= g._max_tokens and manifest.total_bytes + len(data) <= g._max_bytes:
                    summary = text
                    section = ContextSection(kind="projection", key=state.completion_receipt_key,
                                             sha256=hashlib.sha256(data).hexdigest(),
                                             byte_length=len(data), token_count=tokens)
                    manifest = ContextManifest.model_validate({
                        **manifest.model_dump(), "sections": (*manifest.sections, section),
                        "total_tokens": manifest.total_tokens + tokens, "total_bytes": manifest.total_bytes + len(data),
                    })
                else:
                    omissions.append("native-context-budget: preserved necessary exact claims and sources")
        # Even a cached exact match cannot survive a correction/revocation during assembly.
        final = g.assemble(context, scope=setup.scope, subject=setup.subject,
                           required_claim_ids=tuple(sorted(setup.claim_ids)), versions=setup.versions)
        if final.manifest != exact.manifest:
            raise _problem("MEMORY_PROJECTION_STALE", 409)
        manifest = ContextManifest.model_validate({
            **manifest.model_dump(), "omissions": tuple(dict.fromkeys(omissions)), "exact_source_fallback": summary is None,
        })
        result = ProjectionContext(
            projection_id=projection_id, native_state=state.phase, completion_receipt_key=state.completion_receipt_key,
            manifest=manifest, claims=exact.claims, originals=dict(exact.originals), summary=summary,
            degraded=summary is None or bool(omissions),
        )
        return Record(result.model_dump(mode="json"), record.etag)
