"""ADR-0011 phase 2: ``private`` mode long-term memory in Cosmos DB, reached only through the FA API memory relay.

``PrivateMemory.from_environment`` returns this for ``FA_MEMORY_BACKEND=cosmos`` with the native lane's API connection.
Only the Foundry-specific parts change; ``Recalled``, ``search``, claims, receipts, redaction and prompts are shared.

- Hosted never connects to Cosmos. Reads and writes go to the FA API with the Agent's API token; the API derives the
  allowed scopes and item IDs from the caller, so the ``scope`` sent here is only compared there (ADR-0011 §4).
- Model and embedding calls use the Agent identity token, only to the project's own account (``_ENDPOINT``):
  ``/openai/v1/responses`` on the project path, ``/openai/v1/embeddings`` on the account root, because the project
  path answers 404 for embeddings (read on the dev account 2026-10-24; the account root accepts the same token).
- Search: the shared ``search`` sends the Memory Store body to ``_post``, which embeds the message (contextual) and
  sends the relay search; the relay answers with the Memory Store shape, so bounds, order and labels are unchanged.
- Update (private profile and episode): under one ``asyncio.Lock`` (one Agent per process): embed the human words,
  read the newest 10 profile items plus the profile items among the 5 nearest, extract with the model (30 s), check
  the output (``memory_extraction_prompt.checked``), embed the kept items and write them in one relay request. Every
  step runs at most once; nothing is resent for any status, so an extraction can be lost, never duplicated.
- ``updated_at`` is the turn's completion time from the caller. Without it the extraction time is used and no item
  names ``supersedes``, so a late extraction cannot replace a newer turn's item.
- The extraction's model usage is returned with the status (``Updated.usage``) for the caller's claim receipt and
  one ``source="memory"`` report; embedding tokens are only logged (the ADR's known gap).
- Wire: ``_relay`` is the only HTTP call to the API; the bodies it sends and the answers it reads sit next to it, so
  a route or shape change in the API relay (phase 3) is a change here only.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import re
import time

import httpx

from .behavior_http_diagnostics import _exception_type
from .behavior_private_memory import (
    _ENDPOINT, _OFFICIAL_ID, MAX_REPLY_CHARS, MAX_RESPONSE_BYTES, MAX_SENT_CHARS, PrivateMemory, _token, minute_utc,
    scope_for,
)
from .behavior_turn_usage import TURN_USAGE_SCHEMA, ModelUsage, TurnModelUsage
from .memory_extraction_prompt import INSTRUCTIONS, SCHEMA, ExtractionInvalid, checked, turn_data

# The Team data relay (ADR-0006 薄轉送; routes in memory_relay, API phase 3).
MEMORY_PREFIX = "/team-data/v1"
SEARCH_PATH = "/memory/search"
ITEMS_PATH = "/memory/items"
EMBEDDING_MODEL = "text-embedding-3-small"
DIMENSIONS = 1536
# Only the embedding input is cut (the model still reads the whole turn): a token is at least one UTF-8 byte and a
# char at most four, so 2000 chars stay under text-embedding-3-small's 8191-token input limit for any text.
MAX_EMBEDDED_CHARS = 2000
STATIC_INPUT = 10
CONTEXTUAL_INPUT = 5
TIMEOUT = httpx.Timeout(15, connect=5)
MODEL_TIMEOUT = httpx.Timeout(30, connect=5)
# Up to four 1536-number vectors as JSON text.
MAX_EMBEDDING_BYTES = 1024 * 1024
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
UPDATE = "FA_PRIVATE_MEMORY_UPDATE"
log = logging.getLogger(__name__)


class Updated(tuple):
    """``(status, update_id)`` as every backend returns, plus the extraction's model usage (None before any call)."""

    usage: TurnModelUsage | None = None

    def __new__(cls, status, update_id=None, usage=None):
        value = super().__new__(cls, (status, update_id))
        value.usage = usage
        return value


def _error_code(response):
    try:
        return json.loads(response.content)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def _search_body(scope, vector, top):
    body = {"scope": scope, "mode": "static" if vector is None else "contextual", "top": top}
    if vector is not None:
        body["vector"] = vector
    return body


def _items_body(scope, request_id, items):
    return {"scope": scope, "source_request_id": request_id, "items": items}


def _found(response):
    """The relay search answer's items (the Memory Store shape) as ``(memory_id, kind, content)``."""
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError()
    found = []
    for entry in json.loads(response.content)["memories"]:
        item = entry["memory_item"]
        memory_id, content = item.get("memory_id"), item.get("content")
        if isinstance(memory_id, str) and _OFFICIAL_ID.fullmatch(memory_id) and isinstance(content, str):
            found.append((memory_id, item.get("kind"), content))
    return found


def _written(response, slots):
    """The relay items answer: the batch status (201 created, 409 already written), ``update_id`` and item IDs."""
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError()
    value = json.loads(response.content)
    items, update_id = value["items"], value.get("update_id")
    statuses = {item["status"] for item in items}
    if ([item["slot"] for item in items] != slots or len(statuses) != 1 or not statuses <= {201, 409}
            or not all(isinstance(item["id"], str) and _OFFICIAL_ID.fullmatch(item["id"]) for item in items)
            or not (update_id is None or isinstance(update_id, str) and _OFFICIAL_ID.fullmatch(update_id))):
        raise ValueError()
    corrections = value.get("corrections", [])
    results = [correction["result"] for correction in corrections]
    if not set(results) <= {"superseded", "skipped"}:
        raise ValueError()
    return str(statuses.pop()), update_id, [item["id"] for item in items], results


def _vectors(response, count):
    if len(response.content) > MAX_EMBEDDING_BYTES:
        raise ValueError()
    value = json.loads(response.content)
    data = sorted(value["data"], key=lambda entry: entry["index"])
    if [entry["index"] for entry in data] != list(range(count)):
        raise ValueError()
    vectors = []
    for entry in data:
        vector = entry["embedding"]
        if (not isinstance(vector, list) or len(vector) != DIMENSIONS
                or any(type(number) not in (int, float) or not math.isfinite(number) for number in vector)):
            raise ValueError()
        vectors.append([float(number) for number in vector])
    usage = value.get("usage")
    tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    return vectors, tokens if type(tokens) is int else None


def _model_usage(model, response):
    """This one extraction request's usage; unknown counts (no answer, no valid usage) are never 0."""
    counts = None
    try:
        usage = json.loads(response.content).get("usage") if response is not None else None
        if isinstance(usage, dict) and all(type(usage.get(name)) is int and usage[name] >= 0
                                           for name in ("input_tokens", "output_tokens")):
            counts = (usage["input_tokens"], usage["output_tokens"])
            reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
            if type(reasoning) is int and 0 <= reasoning <= counts[1]:
                counts += (reasoning,)
    except (ValueError, AttributeError):
        counts = None
    return TurnModelUsage(schema_version=TURN_USAGE_SCHEMA, usage=(ModelUsage(
        model=model, wire_api="responses", requests=1, measured_requests=int(counts is not None),
        input_tokens=counts[0] if counts else None, output_tokens=counts[1] if counts else None,
        reasoning_tokens=counts[2] if counts and len(counts) == 3 else None),))


def _output_text(response):
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError()
    return "".join(part["text"] for item in json.loads(response.content)["output"] if item.get("type") == "message"
                   for part in item["content"] if part.get("type") == "output_text")


class CosmosPrivateMemory(PrivateMemory):
    # Foundry chains a turn's update to the previous one; here the previous turn's profile is already in the data.
    chains_updates = False

    def __init__(self, endpoint, agent_name, model, token_provider, api, *, transport=None):
        api_url, api_token_provider = api if isinstance(api, tuple) and len(api) == 2 else (None, None)
        if (not isinstance(endpoint, str) or _ENDPOINT.fullmatch(endpoint) is None
                or not isinstance(agent_name, str) or not agent_name
                or not isinstance(model, str) or _MODEL.fullmatch(model) is None
                or not isinstance(api_url, str) or not api_url.startswith("https://") or not callable(api_token_provider)):
            raise ValueError("cosmos memory needs the project's own endpoint, the agent name, a model and the API")
        self.endpoint, self.model, self.token_provider, self.transport = endpoint, model, token_provider, transport
        self.account = endpoint.split("/api/projects/", 1)[0]
        self.api_url, self.api_token_provider = api_url.rstrip("/"), api_token_provider
        self.store = None
        self.scope = scope_for(agent_name)
        # ADR-0011 §2: one Agent per process, so one lock orders every extraction from its profile read to its write.
        self._lock = asyncio.Lock()

    async def _send(self, url, body, token_provider, event, stage, on_failure=None, timeout=TIMEOUT):
        def failed(reason, error):
            log.warning("%s status=%s stage=%s exception_type=%s", event, reason, stage, _exception_type(error))
            if on_failure is not None:
                on_failure(reason)
        try:
            token = await _token(token_provider)
        except Exception as error:
            failed("token_unavailable", error)
            return None
        try:
            async with httpx.AsyncClient(transport=self.transport or httpx.AsyncHTTPTransport(retries=0),
                                         trust_env=False, follow_redirects=False, timeout=timeout) as client:
                return await client.post(url, json=body, headers={"Authorization": "Bearer " + token})
        except Exception as error:
            failed("transport_error", error)
            return None

    async def _relay(self, path, body, event, on_failure=None):
        """The only call to the FA API memory relay (ADR-0011 §4), with the Agent's API token.

        A 503 ``MEMORY_BACKEND_UNAVAILABLE`` (Cosmos 429/5xx or no answer; the API does not retry) is a lost answer:
        a read is unavailable with ``http_error`` (ADR-0011 §7), a write is ``unsent_or_unknown``, and nothing is sent
        again.
        """
        response = await self._send(self.api_url + MEMORY_PREFIX + path, body, self.api_token_provider, event, "api",
                                    on_failure)
        if (response is not None and response.status_code == 503
                and _error_code(response) == "MEMORY_BACKEND_UNAVAILABLE"):
            log.warning("%s status=http_error stage=api reason=backend_unavailable", event)
            if on_failure is not None:
                on_failure("http_error")
            return None
        return response

    async def _embed(self, texts, event, on_failure=None):
        """One ``text-embedding-3-small`` vector per text (its first ``MAX_EMBEDDED_CHARS``), or None (logged with
        ``stage=embedding``)."""
        response = await self._send(self.account + "/openai/v1/embeddings",
                                    {"model": EMBEDDING_MODEL, "input": [text[:MAX_EMBEDDED_CHARS] for text in texts]},
                                    self.token_provider, event, "embedding", on_failure)
        if response is None:
            return None
        reason = "http_error"
        if response.status_code == 200:
            try:
                vectors, tokens = _vectors(response, len(texts))
                # Embedding tokens are not in the ledger (ADR-0011 §2); this line is their only record.
                log.warning("%s status=200 stage=embedding embedding_tokens=%s", event, tokens)
                return vectors
            except (ValueError, KeyError, TypeError, AttributeError):
                reason = "response_invalid"
        log.warning("%s status=%s stage=embedding", event, response.status_code if reason == "http_error" else reason)
        if on_failure is not None:
            on_failure(reason)
        return None

    async def _post(self, path, body, event, *, on_failure=None):
        """The shared ``search`` sends the Memory Store body; it becomes the relay search (ADR-0011 §3)."""
        if path != ":search_memories":
            # Only the old lane's ``update``/``remember`` would get here, and the old lane has no Cosmos memory.
            log.warning("%s status=not_sent reason=unsupported_operation", event)
            return None
        vector = None
        if "items" in body:
            vectors = await self._embed([body["items"][0]["content"][0]["text"]], event, on_failure)
            if vectors is None:
                return None
            vector = vectors[0]
        return await self._relay(SEARCH_PATH, _search_body(body["scope"], vector, body["options"]["max_memories"]),
                                 event, on_failure)

    async def update_chained(self, text, previous_update_id, *, reply=None, request_id=None, completed_at=None):
        """Extract this turn locally and write it through the API once; never resent for any status.

        ``previous_update_id`` is Foundry's chain and is ignored. ``completed_at`` is the turn's completion time (unix
        seconds). Returns ``Updated``: the status, the API's ``update_id`` and the extraction's model usage.
        """
        if not isinstance(request_id, str) or not request_id:
            log.warning("%s status=skipped reason=no_request_id", UPDATE)
            return Updated("skipped")
        async with self._lock:
            return await self._extract_and_write(text[:MAX_SENT_CHARS], reply[:MAX_REPLY_CHARS] if reply else None,
                                                 request_id, completed_at)

    async def _extract_and_write(self, text, reply, request_id, completed_at):
        vectors = await self._embed([text], UPDATE)
        if vectors is None:
            log.warning("%s status=skipped reason=embedding_unavailable", UPDATE)
            return Updated("skipped")
        existing = await self._profiles(vectors[0])
        if existing is None:
            log.warning("%s status=skipped reason=profiles_unavailable", UPDATE)
            return Updated("skipped")
        supersedes = type(completed_at) is int and completed_at >= 0
        if not supersedes:
            completed_at = int(time.time())
            log.warning("%s updated_at_source=extraction_time supersedes=disabled", UPDATE)
        codes = {f"p{index}": memory_id for index, (memory_id, _) in enumerate(existing, 1)}
        output, usage = await self._extract(
            [(code, content) for code, (_, content) in zip(codes, existing)], text, reply,
            minute_utc(datetime.datetime.fromtimestamp(completed_at, datetime.timezone.utc)))
        if output is None:
            return Updated("skipped", None, usage)
        try:
            kept, dropped = checked(output, set(codes), text)
        except ExtractionInvalid:
            log.warning("%s status=skipped reason=response_invalid stage=model", UPDATE)
            return Updated("skipped", None, usage)
        for reason in dropped:
            log.warning("%s dropped=%s", UPDATE, reason)
        if not kept:
            log.warning("%s status=skipped reason=nothing_to_remember", UPDATE)
            return Updated("skipped", None, usage)
        vectors = await self._embed([content for _, _, content, _ in kept], UPDATE)
        if vectors is None:
            log.warning("%s status=skipped reason=embedding_unavailable", UPDATE)
            return Updated("skipped", None, usage)
        items = [{"slot": slot, "kind": kind, "content": content, "embedding": vector, "updated_at": completed_at,
                  **({"supersedes": codes[code]} if code is not None and supersedes else {})}
                 for (slot, kind, content, code), vector in zip(kept, vectors)]
        status, update_id, _ = await self._write(_items_body(self.scope, request_id, items), UPDATE)
        return Updated(status, update_id, usage)

    async def _profiles(self, vector):
        """The newest 10 unsuperseded profile items, then the profile items among the 5 nearest; None on a failure."""
        responses = await asyncio.gather(
            self._relay(SEARCH_PATH, _search_body(self.scope, None, STATIC_INPUT), UPDATE),
            self._relay(SEARCH_PATH, _search_body(self.scope, vector, CONTEXTUAL_INPUT), UPDATE))
        found = {}
        for response in responses:
            if response is None:
                return None
            if response.status_code != 200:
                log.warning("%s status=%s stage=api mode=profiles", UPDATE, response.status_code)
                return None
            try:
                items = _found(response)
            except (ValueError, KeyError, TypeError, AttributeError):
                log.warning("%s status=response_invalid stage=api mode=profiles", UPDATE)
                return None
            for memory_id, kind, content in items:
                if kind == "user_profile":
                    found.setdefault(memory_id, content)
        return list(found.items())

    async def _extract(self, existing, text, reply, completed_at):
        """The model's output text (None on any failure) and the call's usage (None only when nothing was sent)."""
        body = {"model": self.model, "instructions": INSTRUCTIONS, "temperature": 0, "store": False,
                "input": [{"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": turn_data(existing, text, reply, completed_at)}]}],
                "text": {"format": {"type": "json_schema", "name": "fa_memory_extraction", "strict": True,
                                    "schema": SCHEMA}}}
        failures = []
        response = await self._send(self.endpoint + "/openai/v1/responses", body, self.token_provider, UPDATE,
                                    "model", failures.append, MODEL_TIMEOUT)
        if response is None:
            log.warning("%s status=skipped reason=model_unavailable", UPDATE)
            # A request whose send started counts with unknown tokens; one refused for want of a token never ran.
            return None, None if failures == ["token_unavailable"] else _model_usage(self.model, None)
        usage = _model_usage(self.model, response)
        if response.status_code != 200:
            log.warning("%s status=skipped reason=model_unavailable model_status=%s", UPDATE, response.status_code)
            return None, usage
        try:
            return _output_text(response), usage
        except (ValueError, KeyError, TypeError, AttributeError):
            log.warning("%s status=skipped reason=response_invalid stage=model", UPDATE)
            return None, usage

    async def _write(self, body, event):
        """Send the items once; the status, ``update_id`` and the item IDs (None when unconfirmed).

        Only a 4xx is a definite refusal and keeps its status; a 5xx (an ingress 502/504, a 503 without
        ``MEMORY_BACKEND_UNAVAILABLE``) or a 200 of the wrong shape may follow a landed write, so it is
        ``unsent_or_unknown``.
        """
        response = await self._relay(ITEMS_PATH, body, event)
        if response is None:
            return "unsent_or_unknown", None, None
        if response.status_code != 200:
            log.warning("%s status=%s stage=api", event, response.status_code)
            return "unsent_or_unknown" if response.status_code >= 500 else str(response.status_code), None, None
        try:
            status, update_id, ids, corrections = _written(response, [item["slot"] for item in body["items"]])
        except (ValueError, KeyError, TypeError, AttributeError):
            log.warning("%s status=response_invalid stage=api", event)
            return "unsent_or_unknown", None, None
        # 409: this RID's batch was written before; the API sends no correction for it.
        log.warning("%s status=%s update_id=%s items=%d corrections=%s", event, status, update_id, len(ids),
                    json.dumps({result: corrections.count(result) for result in sorted(set(corrections))}))
        return status, update_id, ids

    async def create(self, scope, content, kind, *, event, request_id=None):
        """One verbatim Team note, embedded and written once through the API; the status and its ``memory_id``."""
        if not isinstance(request_id, str) or not request_id:
            log.warning("%s status=skipped reason=no_request_id", event)
            return "skipped", None
        vectors = await self._embed([content], event)
        if vectors is None:
            log.warning("%s status=skipped reason=embedding_unavailable", event)
            return "skipped", None
        status, _, ids = await self._write(_items_body(scope, request_id, [{
            "slot": "note", "kind": kind, "content": content, "embedding": vectors[0],
            "updated_at": int(time.time())}]), event)
        return status, ids[0] if ids else None
