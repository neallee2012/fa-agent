from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Record:
    payload: dict[str, Any]
    etag: str

    def response(self) -> dict:
        return {**self.payload, "etag": self.etag}
