"""Read-only inventory view ClaimedSdkRecovery checks receipts and rows through."""
from __future__ import annotations

import hashlib
import json
import weakref
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import behavior_delivery as delivery, behavior_workers as workers
from .behavior_migration import _parsed, _problem
from .behavior_store import _Guard, _Receipt, _Row

if TYPE_CHECKING:
    from .behavior_migration import AuthorityInventory

def _hash(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _require(condition, detail):
    if not condition:
        raise _problem("MIGRATION_RECOVERY_HISTORY_INVALID", detail)


_RECEIPT_OWNERS = {}


def _checked_receipts(inventory, binding, revision):
    # Each startup validator builds its own view (often of a replace()d copy sharing the same receipts);
    # parsing and hashing every receipt again per view multiplied startup time and memory.
    source = inventory.receipts
    owner = _RECEIPT_OWNERS.get(id(source))
    owner = None if owner is None else owner()
    cached = None if owner is None else owner.__dict__.get("_checked_receipts")
    if (cached is not None and cached[0] == (binding, revision) and cached[1] is source
            and cached[2] is inventory.entity_sha256):
        return cached[3], cached[4]
    receipts, coverage = {}, set()
    for key, record in source.items():
        receipt = _parsed(record.payload, _Receipt)
        _require(receipt.request.request_id == key and receipt.binding == binding
                 and receipt.guard_revision <= revision
                 and len(dict(receipt.writes)) == len(receipt.writes)
                 and inventory.entity_sha256.get("behavior:v1:receipt:" + key) == _hash(record.payload),
                 "recovery inventory receipt identity, hash or guard differs")
        receipts[key] = receipt
        coverage.update(receipt.writes)
    receipts, coverage = MappingProxyType(receipts), frozenset(coverage)
    if isinstance(source, MappingProxyType):
        # The checked copy lives exactly as long as the inventory that owns it.
        key = id(source)
        object.__setattr__(inventory, "_checked_receipts",
                           ((binding, revision), source, inventory.entity_sha256, receipts, coverage))

        def forget(ref):
            if _RECEIPT_OWNERS.get(key) is ref:
                del _RECEIPT_OWNERS[key]
        _RECEIPT_OWNERS[key] = weakref.ref(inventory, forget)
    return receipts, coverage


class _Inventory:
    def __init__(self, inventory, config):
        self.inventory, self.config = inventory, config
        self.guard = _parsed(inventory.guard.payload, _Guard)
        _require(inventory.scope == config.table.scope
                 and self.guard.binding == config.workers.control_binding == config.delivery.control_binding
                 and inventory.installation_configuration_sha256 in {None, config.sha256},
                 "recovery history requires the exact supplied authority/configuration")
        self.checked_receipts = set()
        self.receipts, self.coverage = _checked_receipts(inventory, self.guard.binding, self.guard.revision)

    def row(self, key, model, *, material=None):
        record = self.inventory.rows.get(key)
        _require(record is not None, "required recovery history row is missing: " + key)
        envelope = _parsed(record.payload, _Row)
        fingerprint = _hash(record.payload)
        _require(envelope.row_id == key and envelope.binding == self.guard.binding
                 and self.inventory.entity_sha256.get("behavior:v1:row:" + key) == fingerprint
                 and (key, fingerprint) in self.coverage,
                 "recovery history row lacks its exact bound write receipt")
        value = _parsed(envelope.data, model)
        if material is not None:
            expected = value.model_dump(mode="json") if material is True else material
            _require(envelope.material == delivery._material(expected), "recovery history material differs")
        return value

    def image_hash(self, key, value):
        envelope = _parsed(self.inventory.rows[key].payload, _Row)
        return _hash(envelope.model_copy(update={"data": value.model_dump(mode="json")}).model_dump(mode="json"))

    def receipt(self, request, write_keys, *, exact=None, result=None):
        receipt = self.receipts.get(request.request_id)
        _require(receipt is not None and receipt.request == request
                 and set(dict(receipt.writes)) == set(write_keys),
                 "original recovery request/read-set/write-set receipt differs")
        for key, fingerprint in (exact or {}).items():
            _require(dict(receipt.writes)[key] == fingerprint, "recovery receipt write image differs")
        if result is not None:
            _require(receipt.result == result, "original recovery receipt result differs")
        self.checked_receipts.add(request.request_id)
        return receipt

    def hash(self, key):
        return _hash(self.inventory.rows[key].payload)
