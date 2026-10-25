"""ADR-0011 phase 3: the API's thin relay for ``private`` long-term memory in Cosmos DB for NoSQL. Off by default.

- Off (the routes are not installed, so 404) unless ``FA_MEMORY_BACKEND=cosmos`` and ``FA_COSMOS_ENDPOINT`` names a
  ``*.documents.azure.com`` account; ``public`` keeps the Foundry Memory Store and never reaches this module.
- Hosted never connects to Cosmos (ADR-0011 §4 option b). Only a verified Agent token is served. The API resolves the
  caller's Agent name from its own registry (cached per ``oid``) and allows exactly two scopes: that Agent's private
  ``scope_for(agent_name)`` and, when ``FA_TEAM_MEMORY_ENABLED=1``, its Team's ``team_memory_scope``. The body's scope
  is only compared with them; item IDs and the update ID are computed here from that scope, the RID and the slot.
- Two routes, no delete: search (static or contextual) and items (one transactional batch of creates, then one
  conditional patch per correction). The Cosmos wrapper exposes only create (in a batch), query, patch and a
  metadata read; ``items/replace`` is granted for patch, so a whole-item replace is prevented only by this code.
- Nothing is retried here, and no SDK path resends a data operation after a 429, 408, 5xx or a lost connection
  (azure-cosmos 4.17.1): the throttle retries (``RetryOptions`` 0), the pipeline's connect and read retries
  (``retry_total=0``) and the endpoint-discovery failover retries, which also resend a 503 write once
  (``EnableEndpointDiscovery`` off), are off; write retries are off by default (no ``retry_write``). The SDK still
  re-reads routing on a 410 split or a stale container RID (both refused before they run), resends a read whose
  status is a legacy OS socket error code, and retries the account read while the client is built. A 429 or 5xx
  is a 503 and the caller records the outcome as unknown. A correction patch that does not apply (412, 404,
  unknown) is logged and skipped; the new items stay.
- One ``CosmosClient`` per process (every peer app shares it), built on first use or by the owner app's start-up
  warm-up, closed with the owner app unless a build is still running (then process exit frees it). `azure.cosmos`
  is imported only then.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from itertools import islice
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _principal_dependency, _response
from .behavior_auth import BehaviorPrincipal
from .behavior_native_capture import team_memory_capture
from .behavior_private_memory import BACKEND_ENV, scope_for
from .behavior_wire import Identifier, WireModel
from .errors import Problem

PREFIX = "/team-data/v1/memory"
ENDPOINT_ENV = "FA_COSMOS_ENDPOINT"
EPISODE_DAYS_ENV = "FA_MEMORY_EPISODE_DAYS"
DATABASE, CONTAINER = "fa-memory", "memories"
ITEM_SCHEMA = "fa-memory-item-v1"
DIMENSIONS = 1536
MAX_TOP = 10
MAX_ITEMS = 4
MAX_PRIVATE_CHARS = 600
# A Team note is the user's quote (at most 4096 characters, memory_candidate_wire) plus its header.
MAX_TEAM_CHARS = 4608
MAX_SECONDS = 2**53 - 1
CONNECT_SECONDS = 5
REBUILD_SECONDS = 30
SEARCH_BODY_BYTES = 64 * 1024
WRITE_BODY_BYTES = 512 * 1024
# The Hosted reader refuses a memory response above 256 KiB (behavior_private_memory.MAX_RESPONSE_BYTES).
RESPONSE_BYTES = 256 * 1024
MEMORY_ERRORS = frozenset({"MEMORY_AGENT_ONLY", "MEMORY_SCOPE_DENIED", "MEMORY_INVALID_REQUEST",
                           "MEMORY_BACKEND_UNAVAILABLE"})
_ENDPOINT = re.compile(r"https://[a-z0-9][a-z0-9-]{1,49}\.documents\.azure\.com(?::443)?/?")
_PROJECTION = "SELECT TOP @n c.id, c.kind, c.content, c.updated_at FROM c"
STATIC_QUERY = (_PROJECTION + " WHERE c.kind = 'user_profile' AND NOT IS_DEFINED(c.superseded_by)"
                " ORDER BY c.updated_at DESC")
TEAM_CONTEXTUAL_QUERY = (_PROJECTION + " WHERE NOT IS_DEFINED(c.superseded_by)"
                         " ORDER BY VectorDistance(c.embedding, @vector)")
# Only a private scope ages out its episodes: a Team "durable fact" note is also a chat_summary.
PRIVATE_CONTEXTUAL_QUERY = (_PROJECTION + " WHERE NOT IS_DEFINED(c.superseded_by)"
                            " AND (c.kind != 'chat_summary' OR c.updated_at >= @episode_since)"
                            " ORDER BY VectorDistance(c.embedding, @vector)")
log = logging.getLogger(__name__)

Scope = Annotated[str, Field(pattern=r"^fa[pt]1-[0-9a-f]{48}$")]
MemoryId = Annotated[str, Field(pattern=r"^fam1-[0-9a-f]{40}$")]
Vector = Annotated[tuple[float, ...], Field(min_length=DIMENSIONS, max_length=DIMENSIONS)]
Seconds = Annotated[int, Field(ge=0, le=MAX_SECONDS)]
_SLOT_KINDS = {"p0": "user_profile", "p1": "user_profile", "p2": "user_profile", "e0": "chat_summary"}


class MemorySearch(WireModel):
    scope: Scope
    mode: Literal["static", "contextual"]
    # Required for contextual and absent for static; computed by Hosted (text-embedding-3-small).
    vector: Vector | None = None
    top: Annotated[int, Field(ge=1, le=MAX_TOP)]

    @model_validator(mode="after")
    def _vector(self):
        if (self.mode == "contextual") != (self.vector is not None):
            raise ValueError("a contextual search needs a vector and a static one takes none")
        return self


class MemoryItemWrite(WireModel):
    slot: Literal["p0", "p1", "p2", "e0", "note"]
    kind: Literal["user_profile", "chat_summary"]
    content: Annotated[str, Field(min_length=1, max_length=MAX_TEAM_CHARS)]
    embedding: Vector
    # The turn's completion time (unix seconds); Hosted reads it, the API cannot.
    updated_at: Seconds
    supersedes: MemoryId | None = Field(default=None, exclude_if=lambda value: value is None)


class MemoryWrite(WireModel):
    scope: Scope
    source_request_id: Identifier
    items: Annotated[tuple[MemoryItemWrite, ...], Field(min_length=1, max_length=MAX_ITEMS)]

    @model_validator(mode="after")
    def _slots(self):
        slots = [item.slot for item in self.items]
        if len(set(slots)) != len(slots):
            raise ValueError("each slot is written once")
        private = self.scope.startswith("fap1-")
        for item in self.items:
            if private:
                if (_SLOT_KINDS.get(item.slot) != item.kind or len(item.content) > MAX_PRIVATE_CHARS
                        or item.supersedes is not None and item.slot == "e0"):
                    raise ValueError("a private item is a profile p0-p2 or one episode e0; only a profile corrects")
            elif item.slot != "note" or item.supersedes is not None:
                # A Team note is create-only; its correction is named in the new note's content.
                raise ValueError("a Team scope takes one note and never supersedes")
        return self


def update_id_for(scope: str, request_id: str) -> str:
    return "fau1-" + hashlib.sha256(f"fa-memory-update-v1\0{scope}\0{request_id}".encode("utf-8")).hexdigest()[:40]


def item_id_for(scope: str, request_id: str, slot: str) -> str:
    return "fam1-" + hashlib.sha256(
        f"fa-memory-item-v1\0{scope}\0{request_id}\0{slot}".encode("utf-8")).hexdigest()[:40]


def supersede_predicate(updated_at) -> str:
    """The correction precondition: constant text plus one validated integer.

    The SDK's ``filter_predicate`` takes a string with no parameter binding; an unbound ``@n`` is not an error, the
    condition just fails as 412 (ADR-0011 phase 0c), so the integer is inlined, never bound.
    """
    if type(updated_at) is not int or not 0 <= updated_at <= MAX_SECONDS:
        raise ValueError("updated_at must be a non-negative integer of unix seconds")
    return ("FROM c WHERE c.kind = 'user_profile' AND NOT IS_DEFINED(c.superseded_by)"
            f" AND c.updated_at <= {updated_at:d}")


def _status(error):
    responses, index = getattr(error, "operation_responses", None), getattr(error, "error_index", None)
    if isinstance(responses, list) and type(index) is int and 0 <= index < len(responses):
        status = responses[index].get("statusCode") if isinstance(responses[index], dict) else None
        if type(status) is int:
            return status
    status = getattr(error, "status_code", None)
    return status if type(status) is int else None


def _backend_problem():
    return Problem(503, "MEMORY_BACKEND_UNAVAILABLE", "the memory backend did not answer; nothing is retried")


def _unavailable(stage, error):
    status = _status(error)
    if status == 429:
        log.warning("FA_MEMORY_RELAY_THROTTLED stage=%s", stage)
    log.warning("FA_MEMORY_RELAY_FAILED stage=%s status=%s exception_type=%s", stage, status, type(error).__name__)
    return _backend_problem()


class CosmosMemoryContainer:
    """The only Cosmos operations the relay may use: no replace, upsert or delete (ADR-0011 §4)."""

    def __init__(self, container):
        self._container = container

    def read_metadata(self):
        self._container.read()

    def create_batch(self, scope: str, documents: list[dict]) -> int:
        """201 when every item was created, 409 when one already existed (the whole batch rolled back)."""
        try:
            self._container.execute_item_batch([("create", (document,)) for document in documents],
                                               partition_key=scope)
        except Exception as error:
            if _status(error) == 409:
                return 409
            raise _unavailable("batch", error) from None
        return 201

    def query(self, scope: str, query: str, parameters: list[dict], limit: int) -> list[dict]:
        try:
            return list(islice(self._container.query_items(query=query, parameters=parameters,
                                                           partition_key=scope), limit))
        except Exception as error:
            raise _unavailable("query", error) from None

    def patch(self, scope: str, item_id: str, operations: list[dict], predicate: str) -> int | None:
        """The patch's status (200, 412, 404, ...) or None when its outcome is unknown; never resent."""
        try:
            self._container.patch_item(item=item_id, partition_key=scope, patch_operations=operations,
                                       filter_predicate=predicate)
        except Exception as error:
            status = _status(error)
            if status == 429:
                log.warning("FA_MEMORY_RELAY_THROTTLED stage=patch")
            return status
        return 200


class SharedCosmos:
    """One client per process and account endpoint; the first use builds it, a failure leaves a later use to try.

    Building reads the account, which the SDK retries for up to about a minute when the backend is down. A request
    never waits behind a build in progress or retries one that failed less than ``REBUILD_SECONDS`` ago: both are an
    immediate 503, so a down backend cannot fill the shared threadpool.
    """

    def __init__(self, endpoint, credential, clock=time.monotonic):
        self.endpoint, self.credential, self.clock = endpoint, credential, clock
        self._lock, self._client, self._container, self._failed_at = threading.Lock(), None, None, None

    def container(self) -> CosmosMemoryContainer:
        if self._container is not None:
            return self._container
        if not self._lock.acquire(blocking=False):
            log.warning("FA_MEMORY_RELAY_FAILED stage=connect reason=build_in_progress")
            raise _backend_problem()
        try:
            if self._container is None:
                if self._failed_at is not None and self.clock() - self._failed_at < REBUILD_SECONDS:
                    log.warning("FA_MEMORY_RELAY_FAILED stage=connect reason=recent_failure")
                    raise _backend_problem()
                try:
                    from azure.cosmos import CosmosClient
                    from azure.cosmos.documents import ConnectionPolicy
                    policy = ConnectionPolicy()
                    # The client kwargs cannot set 0 (they are or-chained), so the throttle retries are off here.
                    policy.RetryOptions = type(policy.RetryOptions)(max_retry_attempt_count=0,
                                                                    max_wait_time_in_seconds=0)
                    # Single region behind a private endpoint, so there is nothing to discover; it gates the 503 resend.
                    policy.EnableEndpointDiscovery = False
                    # The Python SDK is Gateway mode only: HTTPS 443 to the account's private endpoint.
                    client = CosmosClient(self.endpoint, credential=self.credential, connection_policy=policy,
                                          retry_total=0, connection_timeout=CONNECT_SECONDS, read_timeout=15)
                    self._container = CosmosMemoryContainer(
                        client.get_database_client(DATABASE).get_container_client(CONTAINER))
                except Exception as error:
                    self._failed_at = self.clock()
                    raise _unavailable("connect", error) from None
                self._client, self._failed_at = client, None
            return self._container
        finally:
            self._lock.release()

    def warm(self, wait=CONNECT_SECONDS):
        """Token, client and one container metadata read before the first turn needs them, in a daemon thread that
        start-up waits for at most one connect timeout; failure only logs."""
        thread = threading.Thread(target=self._warm, name="fa-memory-warmup", daemon=True)
        thread.start()
        thread.join(wait)
        if thread.is_alive():
            log.warning("FA_MEMORY_RELAY_WARMUP status=pending")

    def _warm(self):
        try:
            self.container().read_metadata()
        except Exception as error:
            log.warning("FA_MEMORY_RELAY_WARMUP status=failed exception_type=%s", type(error).__name__)
        else:
            log.warning("FA_MEMORY_RELAY_WARMUP status=ready")

    def close(self):
        # A build in progress holds the lock for up to about a minute; shutdown does not wait for it.
        if not self._lock.acquire(blocking=False):
            log.warning("FA_MEMORY_RELAY_CLOSE status=skipped reason=build_in_progress")
            return
        try:
            client, self._client, self._container = self._client, None, None
        finally:
            self._lock.release()
        if client is not None:
            client.close()


_SHARED: dict[str, SharedCosmos] = {}
# oid -> Agent name. A name never changes once registered; whether the identity is active is checked on every
# request by the JWT verifier, which this cache never replaces.
_AGENT_NAMES: dict[str, str] = {}


class MemoryRelay:
    def __init__(self, connect, *, targets, resolve_agent, episode_days=90, clock=time.time, environ=None):
        self.connect, self.targets, self.resolve_agent = connect, tuple(targets), resolve_agent
        self.episode_seconds, self.clock = episode_days * 86400, clock
        self.environ = os.environ if environ is None else environ

    def agent_name(self, principal: BehaviorPrincipal) -> str:
        name = _AGENT_NAMES.get(principal.oid)
        if name is not None:
            return name
        # A configured actor's actor_id is its actor name, not its agent_id (as in the peer router).
        target = next((target for target in self.targets if target.agent_oid == principal.oid), None)
        scope = self.resolve_agent(principal.actor_id if target is None else target.agent_id)
        if (str(scope.tenant_id), scope.team_id, str(scope.identity.oid)) != (
                principal.tenant_id, principal.team, principal.oid):
            log.warning("FA_MEMORY_RELAY_DENIED agent_id=%s reason=agent_mismatch", principal.actor_id)
            raise Problem(403, "MEMORY_SCOPE_DENIED", "the credential has no registered Agent target")
        _AGENT_NAMES[principal.oid] = scope.target.agent_name
        return scope.target.agent_name

    def allowed(self, principal, requested: str) -> str:
        if not isinstance(principal, BehaviorPrincipal) or principal.kind != "agent" or not principal.actor_id:
            raise Problem(403, "MEMORY_AGENT_ONLY", "only a verified Team Agent may use memory")
        scopes = {scope_for(self.agent_name(principal))}
        team = team_memory_capture(principal.tenant_id, principal.team, environ=self.environ)
        if team is not None:
            scopes.add(team.scope)
        if requested not in scopes:
            log.warning("FA_MEMORY_RELAY_DENIED agent_id=%s reason=scope_mismatch", principal.actor_id)
            raise Problem(403, "MEMORY_SCOPE_DENIED", "the scope is not this Agent's own or its Team's")
        return requested

    def search(self, principal, command: MemorySearch) -> dict:
        scope = self.allowed(principal, command.scope)
        parameters = [{"name": "@n", "value": command.top}]
        if command.mode == "static":
            query = STATIC_QUERY
        else:
            parameters.append({"name": "@vector", "value": list(command.vector)})
            query = TEAM_CONTEXTUAL_QUERY
            if scope.startswith("fap1-"):
                query = PRIVATE_CONTEXTUAL_QUERY
                parameters.append({"name": "@episode_since", "value": int(self.clock()) - self.episode_seconds})
        rows = self.connect().query(scope, query, parameters, command.top)
        # The Memory Store's search shape, so the Hosted reader is the same code for both backends; no usage.
        return {"memories": [{"memory_item": {"memory_id": row.get("id"), "kind": row.get("kind"),
                                              "content": row.get("content"), "updated_at": row.get("updated_at")}}
                             for row in rows]}

    def write(self, principal, command: MemoryWrite) -> dict:
        scope = self.allowed(principal, command.scope)
        private, rid = scope.startswith("fap1-"), command.source_request_id
        ids = {item.slot: item_id_for(scope, rid, item.slot) for item in command.items}
        if any(item.supersedes in ids.values() for item in command.items):
            raise Problem(422, "MEMORY_INVALID_REQUEST", "an item cannot supersede an item of the same write")
        update_id = update_id_for(scope, rid) if private else None
        documents = []
        for item in command.items:
            document = {"id": ids[item.slot], "scope": scope, "kind": item.kind, "content": item.content,
                        "updated_at": item.updated_at, "embedding": list(item.embedding), "source_request_id": rid}
            if private:
                document["update_id"] = update_id
            if item.supersedes is not None:
                document["supersedes"] = item.supersedes
            document["schema"] = ITEM_SCHEMA
            documents.append(document)
        container = self.connect()
        status = container.create_batch(scope, documents)
        log.warning("FA_MEMORY_RELAY_WRITE status=%s scope_kind=%s items=%d", status,
                    "private" if private else "team", len(documents))
        corrections = []
        for item in command.items:
            if item.supersedes is None:
                continue
            # A 409 batch was written before, with its corrections; nothing is sent again.
            result = None if status == 409 else container.patch(scope, item.supersedes, [
                {"op": "add", "path": "/superseded_by", "value": ids[item.slot]},
                {"op": "add", "path": "/superseded_at", "value": item.updated_at},
            ], supersede_predicate(item.updated_at))
            if result != 200:
                reason = {None: "already_written" if status == 409 else "unknown", 412: "precondition_failed",
                          404: "not_found"}.get(result, "status_" + str(result))
                log.warning("FA_MEMORY_RELAY_SUPERSEDE status=skipped reason=%s slot=%s", reason, item.slot)
            corrections.append({"slot": item.slot, "supersedes": item.supersedes,
                                "result": "superseded" if result == 200 else "skipped"})
        return {"update_id": update_id,
                "items": [{"slot": item.slot, "id": ids[item.slot], "status": status} for item in command.items],
                "corrections": corrections}


def relay_settings(environ) -> tuple[str, int] | None:
    """The account endpoint and episode age, or None when memory is not on Cosmos or its settings are invalid."""
    if environ.get(BACKEND_ENV) != "cosmos":
        return None
    endpoint, days = environ.get(ENDPOINT_ENV) or "", environ.get(EPISODE_DAYS_ENV) or "90"
    if _ENDPOINT.fullmatch(endpoint) is None or re.fullmatch(r"[1-9][0-9]{0,3}", days) is None:
        log.warning("FA_MEMORY_RELAY_INACTIVE reason=config_invalid")
        return None
    return endpoint, int(days)


def install_memory_relay_routes(app, composition, *, environ=None, connect=None, clock=time.time) -> None:
    environ = os.environ if environ is None else environ
    settings = relay_settings(environ)
    if settings is None:
        return
    endpoint, days = settings
    from .behavior_team_registry import resolve_agent_scope
    owner = getattr(composition, "_agent_message_owner", composition)
    store, config = owner.runtime.store, owner.config
    if connect is None:
        shared = _SHARED.setdefault(endpoint, SharedCosmos(endpoint, owner.clients[0].credential))
        # The owner app's lifespan warms it up and closes it; peer apps share it.
        app.state.memory_cosmos, connect = shared, shared.container
    relay = MemoryRelay(connect, targets=config.targets, episode_days=days, clock=clock, environ=environ,
                        resolve_agent=lambda agent_id: resolve_agent_scope(store, config, agent_id))
    app.state.memory_relay = relay
    router = APIRouter()
    identity = _principal_dependency(composition.verifier)

    async def reply(call):
        try:
            value = await run_in_threadpool(call)
        except Problem as error:
            if error.body["error"]["code"] not in MEMORY_ERRORS:
                raise
            return JSONResponse(error.body, status_code=error.status, headers={"Cache-Control": "no-store"})
        return _response(value, max_bytes=RESPONSE_BYTES)

    @router.post(PREFIX + "/search")
    async def search(request: Request, principal=Depends(identity)):
        body = await _body(request, MemorySearch, max_body_bytes=SEARCH_BODY_BYTES)
        return await reply(lambda: relay.search(principal, body))

    @router.post(PREFIX + "/items")
    async def items(request: Request, principal=Depends(identity)):
        body = await _body(request, MemoryWrite, max_body_bytes=WRITE_BODY_BYTES)
        return await reply(lambda: relay.write(principal, body))

    app.include_router(router)
