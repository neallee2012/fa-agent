from __future__ import annotations

import re
from typing import Protocol
from urllib.parse import quote

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError, HttpResponseError
from azure.core.rest import HttpRequest

from .config import native_memory_scope
from .errors import Problem, safe_json, upstream_problem

FEATURE_HEADERS = {"Foundry-Features": "MemoryStores=V1Preview"}
UPDATE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,256}$"


class Memory(Protocol):
    def start(self, scope: str, items: list[dict]) -> dict: ...
    def poll(self, update_id: str) -> dict: ...
    def search(self, scope: str, query: str) -> dict: ...


class FoundryMemory:
    def __init__(self, client: AIProjectClient, endpoint: str, store: str):
        self.client = client
        self.endpoint = endpoint.rstrip("/")
        self.store = store

    def start(self, scope: str, items: list[dict]) -> dict:
        initial = {}

        def capture(response):
            if response.http_response.status_code == 202:
                response.http_response.read()
                initial.update(response.http_response.json())

        try:
            # No SDK polling thread survives the HTTP request / Container App scale-to-zero.
            # NoPolling.result() can fabricate an empty result; never call it.
            self.client.beta.memory_stores.begin_update_memories(
                name=self.store, scope=native_memory_scope(scope), items=items, update_delay=0,
                polling=False, raw_response_hook=capture, headers=FEATURE_HEADERS,
            )
            update_id = initial.get("update_id")
            if not isinstance(update_id, str) or not re.fullmatch(UPDATE_ID_PATTERN, update_id):
                raise Problem(502, "MEMORY_PROTOCOL_ERROR", "missing or invalid native update_id")
            return safe_json(initial)
        except AzureError as exc:
            raise upstream_problem("memory", exc) from exc

    def poll(self, update_id: str) -> dict:
        if not re.fullmatch(UPDATE_ID_PATTERN, update_id):
            raise Problem(502, "MEMORY_PROTOCOL_ERROR", "invalid stored native update_id")
        # 2.3.0 exposes begin_update_memories but not a public get-update operation.
        # Use its authenticated pipeline and the same v1 operation URL as the SDK poller.
        url = (
            f"{self.endpoint}/memory_stores/{quote(self.store, safe='')}"
            f"/updates/{quote(update_id, safe='')}?api-version=v1"
        )
        try:
            response = self.client.send_request(HttpRequest("GET", url, headers=FEATURE_HEADERS))
            if response.status_code != 200:
                raise HttpResponseError(response=response)
            result = response.json()
            if result.get("update_id") != update_id:
                raise Problem(502, "MEMORY_PROTOCOL_ERROR", "native operation ID mismatch")
            return safe_json(result)
        except AzureError as exc:
            raise upstream_problem("memory", exc) from exc

    def search(self, scope: str, query: str) -> dict:
        try:
            result = self.client.beta.memory_stores.search_memories(
                name=self.store, scope=native_memory_scope(scope), items=query,
                options={"max_memories": 10}, headers=FEATURE_HEADERS,
            )
            data = result.as_dict()
            if not isinstance(data.get("memories"), list):
                raise Problem(502, "MEMORY_PROTOCOL_ERROR", "memory search lacks memories array")
            return safe_json(data)
        except AzureError as exc:
            raise upstream_problem("memory", exc) from exc
