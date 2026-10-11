"""Bounded native session pagination, not a point-in-time snapshot."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


class InventoryError(ValueError):
    pass


def collect_sessions(
    fetch_page: Callable[[str | None], dict[str, Any]], *, max_pages: int = 30,
) -> list[dict[str, Any]]:
    """Follow the service's last_id/after cursor, never the CLI's lossy envelope."""
    if type(max_pages) is not int or not 1 <= max_pages <= 30:
        raise InventoryError("Invalid inventory page bound")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = None
    for _ in range(max_pages):
        page = fetch_page(cursor)
        if (not isinstance(page, dict) or type(page.get("has_more")) is not bool
                or not isinstance(page.get("data"), list)
                or not {"first_id", "last_id"} <= page.keys()
                or page.keys() - {"data", "first_id", "last_id", "has_more", "object"}):
            raise InventoryError("Incomplete or unknown native pagination envelope")
        batch = page["data"]
        if len(batch) > 100:
            raise InventoryError("Native page exceeded the documented limit")
        if not batch:
            if page["has_more"] or page["first_id"] is not None or page["last_id"] is not None:
                raise InventoryError("Empty native page has a continuation")
            return rows
        for row in batch:
            identity = row.get("agent_session_id") if isinstance(row, dict) else None
            if (not isinstance(identity, str) or not 1 <= len(identity) <= 8192
                    or identity in seen):
                raise InventoryError("Missing or repeated native session identity")
            seen.add(identity)
        if (page["first_id"] != batch[0]["agent_session_id"]
                or page["last_id"] != batch[-1]["agent_session_id"]):
            raise InventoryError("Native cursor does not match its page")
        rows.extend(batch)
        if not page["has_more"]:
            return rows
        cursor = page["last_id"]
    raise InventoryError("Native inventory exceeded its page bound")
