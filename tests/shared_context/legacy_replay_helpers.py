"""Seed calls of the retired job lane, replayed from what main 80ec0865 stored when that test ran them.

A test that only reads, settles or refuses over old work once built it through the retired submit, register, claim,
save and report code. That code is gone. fixtures/legacy_stores/replay/<test file>.json keeps, per test node and per
seed call in order, the state 80ec0865 left right after that call: every Behavior row (with its ETag), the batch
sequence, every artifact Blob and M3 record when the test passes its ArtifactService, the token counter and the value
the call returned. `replayed` makes the harness hold that state and returns that value; no retired code runs.

Rows, Blobs and records are stored once per file in `pool` and named by digest in each state.
Tokens are counters in both runs (`legacy_tokens`, requested first), so whatever a test builds before or after a
seed call matches the stored rows; harnesses whose configuration digest covers tmp paths also pin it
(`pinned_configuration`).
"""
import base64
import importlib
import json
import os
import secrets
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from shared_context import m3_store
from shared_context.behavior_server_config import ServerConfiguration
from shared_context.m3_models import BlobReceipt
from shared_context.records import Record
from test_behavior_store import NativeEntity

REPLAY = Path(__file__).parent / "fixtures" / "legacy_stores" / "replay"
TABLE = "Behavior"
TOKENS = {"next": 0}
PINNED_SHA256 = "5e" * 32
_CALLS = {}


def _next():
    TOKENS["next"] += 1
    return TOKENS["next"] - 1


@pytest.fixture
def legacy_tokens(monkeypatch):
    """Counter tokens from here on; a replaying test requests it first, so both runs count from its start."""
    TOKENS["next"] = 0
    _CALLS.clear()
    monkeypatch.setattr(secrets, "token_hex", lambda k=None: ("%064x" % (10 ** 9 + _next()))[-2 * (32 if k is None else k):])
    monkeypatch.setattr(m3_store, "uuid4", lambda: uuid.UUID(int=_next()))


@pytest.fixture
def pinned_configuration(monkeypatch):
    """Rows of a workspace harness bind ServerConfiguration.sha256, which covers its tmp paths; both runs pin it."""
    monkeypatch.setattr(ServerConfiguration, "sha256", property(lambda _: PINNED_SHA256))


def node():
    return os.environ["PYTEST_CURRENT_TEST"].rsplit(" (", 1)[0]


def encode(value):
    if isinstance(value, BaseModel):
        cls = type(value)
        return {"__model__": f"{cls.__module__}.{cls.__qualname__}", "data": value.model_dump(mode="json")}
    if isinstance(value, tuple):
        return {"__tuple__": [encode(item) for item in value]}
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, SimpleNamespace):
        return {"__ns__": {k: encode(v) for k, v in vars(value).items()}}
    if isinstance(value, dict):
        return {"__dict__": {k: encode(v) for k, v in value.items()}}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"cannot store a {type(value).__name__} seed result")


def decode(value):
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "__model__" in value:
        module, _, name = value["__model__"].rpartition(".")
        return getattr(importlib.import_module(module), name).model_validate_json(json.dumps(value["data"]))
    if "__tuple__" in value:
        return tuple(decode(item) for item in value["__tuple__"])
    if "__ns__" in value:
        return SimpleNamespace(**{k: decode(v) for k, v in value["__ns__"].items()})
    return {k: decode(v) for k, v in value["__dict__"].items()}


def backing(h):
    transport = getattr(h, "transport", None)
    return (transport if transport is not None else h.table).backing


def replayed(h, artifacts=None):
    """The next stored seed call of this test: `h` (and `artifacts`) hold its state; returns its value."""
    name = node()
    file, test = name.split("::", 1)
    index = _CALLS[name] = _CALLS.get(name, -1) + 1
    stored = json.loads((REPLAY / (Path(file).stem + ".json")).read_text(encoding="utf-8"))
    pool = stored["pool"]
    saved = stored["states"][stored["calls"][test][index]]
    table = backing(h)
    table.rows.clear()
    # Written directly, not through Backing.apply: apply would mint new ETags and sequence numbers.
    for digest in saved["rows"]:
        row = pool[digest]
        entity = {**row["entity"], "Payload": row["entity"]["Payload"].encode("utf-8")}
        table.rows[(TABLE, entity["PartitionKey"], entity["RowKey"])] = NativeEntity(entity, row["etag"])
    table.sequence = saved["sequence"]
    if artifacts is not None:
        artifacts.store._versions.clear()
        for digest in saved["blobs"]:
            blob = pool[digest]
            artifacts.store._versions[(blob["team"], blob["artifact_id"], blob["version_id"])] = (
                BlobReceipt.model_validate(blob["receipt"]), base64.b64decode(blob["data_base64"]))
        if "records" in saved:  # an in-memory M3 repository; a TableRepository keeps its records in the rows above
            repository = artifacts.repository
            repository.data.clear()
            for digest in saved["records"]:
                record = pool[digest]
                repository.data[record["team"], record["row"]] = Record(record["payload"], record["etag"])
            repository.sequence = saved["record_sequence"]
    TOKENS["next"] = saved["counter"]
    return decode(saved["result"])
