from __future__ import annotations

from typing import Protocol

FEATURE_HEADERS = {"Foundry-Features": "MemoryStores=V1Preview"}
UPDATE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,256}$"


class Memory(Protocol):
    def start(self, scope: str, items: list[dict]) -> dict: ...
    def poll(self, update_id: str) -> dict: ...
    def search(self, scope: str, query: str) -> dict: ...

