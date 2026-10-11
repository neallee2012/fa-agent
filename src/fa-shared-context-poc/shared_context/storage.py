from __future__ import annotations

import json
from typing import Protocol

from azure.core import MatchConditions
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode

from .errors import Problem, upstream_problem
from .records import Record

MAX_PAYLOAD_BYTES = 48 * 1024


def serialize(payload: dict) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise Problem(413, "STATE_TOO_LARGE", "serialized authoritative payload exceeds 48 KiB")
    return raw


class Repository(Protocol):
    def get(self, team: str, row: str) -> Record: ...
    def create(self, team: str, row: str, payload: dict) -> Record: ...
    def replace(self, team: str, row: str, payload: dict, etag: str) -> Record: ...
    def list_prefix(self, team: str, prefix: str, limit: int, *,
                    after: str | None = None) -> tuple[tuple[tuple[str, Record], ...], bool]: ...


def prefix_end(prefix: str) -> str:
    if not prefix or prefix[-1] == "\uffff":
        raise ValueError("row prefix must end with an incrementable character")
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


class TableRepository:
    """One team partition; disjoint work and memory-operation row namespaces."""

    def __init__(self, client: TableClient):
        self.client = client

    def get(self, team: str, row: str) -> Record:
        try:
            entity = self.client.get_entity(partition_key=team, row_key=row)
            return Record(json.loads(entity["Payload"]), entity.metadata["etag"])
        except ResourceNotFoundError as exc:
            raise Problem(404, "NOT_FOUND", "record does not exist") from exc
        except AzureError as exc:
            raise upstream_problem("table", exc) from exc

    def list_prefix(self, team: str, prefix: str, limit: int, *,
                    after: str | None = None) -> tuple[tuple[tuple[str, Record], ...], bool]:
        """Read one page of a row-key range; the flag reports whether more rows follow the page."""
        if after is not None and not after.startswith(prefix):
            raise ValueError("page cursor must stay inside the requested row prefix")
        start, operator = (prefix, "ge") if after is None else (after, "gt")
        rows = []
        try:
            entities = self.client.query_entities(
                f"PartitionKey eq @team and RowKey {operator} @start and RowKey lt @end",
                parameters={"team": team, "start": start, "end": prefix_end(prefix)},
                select=["RowKey", "Payload"], results_per_page=limit + 1,
            )
            for entity in entities:
                if len(rows) == limit:
                    return tuple(rows), True
                rows.append((entity["RowKey"], Record(json.loads(entity["Payload"]), entity.metadata["etag"])))
        except AzureError as exc:
            raise upstream_problem("table", exc) from exc
        return tuple(rows), False

    def create(self, team: str, row: str, payload: dict) -> Record:
        entity = {"PartitionKey": team, "RowKey": row, "Payload": serialize(payload)}
        try:
            result = self.client.create_entity(entity)
            return Record(payload, result["etag"])
        except ResourceExistsError as exc:
            raise Problem(409, "ALREADY_EXISTS", "record already exists") from exc
        except AzureError as exc:
            raise upstream_problem("table", exc) from exc

    def replace(self, team: str, row: str, payload: dict, etag: str) -> Record:
        entity = {"PartitionKey": team, "RowKey": row, "Payload": serialize(payload)}
        try:
            result = self.client.update_entity(
                entity=entity, mode=UpdateMode.REPLACE, etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
            return Record(payload, result["etag"])
        except AzureError as exc:
            if getattr(exc, "status_code", None) == 412:
                raise Problem(412, "ETAG_CONFLICT", "state changed; GET and reconcile before retry") from exc
            if getattr(exc, "status_code", None) == 404:
                raise Problem(404, "NOT_FOUND", "record does not exist") from exc
            raise upstream_problem("table", exc) from exc
