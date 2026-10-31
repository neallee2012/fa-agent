"""Build a self-consistent forged authority inventory for startup-history refusal tests.

A plain edit to a retained row is caught by the generic row/receipt hash checks before any history validator's
own rule runs. These helpers rewrite the row *and* every hash that vouches for it (the entity hash and each receipt
write that recorded it), so a test reaches the specific rule it names: the shape of a buggy or hostile writer that
kept its own bookkeeping coherent.
"""

import copy
import hashlib
import json
from dataclasses import replace

from shared_context.behavior_store import InlineMaterial
from shared_context.records import Record

_ROW, _RECEIPT = "behavior:v1:row:", "behavior:v1:receipt:"


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _material(data):
    """The inline material the store keeps for a row: its canonical JSON bytes and their SHA-256.

    Spelled out here instead of importing the writers' private helper, so a fault in that helper is not mirrored.
    """
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return InlineMaterial(data=raw, sha256=hashlib.sha256(raw).hexdigest()).model_dump(mode="json")


class Forgery:
    def __init__(self, inventory):
        self.base = inventory
        self.rows = dict(inventory.rows)
        self.receipts = {key: Record(copy.deepcopy(value.payload), value.etag)
                         for key, value in inventory.receipts.items()}
        self.sha = dict(inventory.entity_sha256)

    def _rewrite_writes(self, old_key, old_hash, new_key, new_hash):
        for rid, record in self.receipts.items():
            writes = record.payload["writes"]
            if [old_key, old_hash] in writes or (old_key, old_hash) in writes:
                record.payload["writes"] = [[new_key, new_hash] if tuple(item) == (old_key, old_hash) else item
                                            for item in writes]

    def data(self, key, change, *, material=False, later=False):
        """Replace the row's data with change(copy of data) and keep every vouching hash coherent.

        material: also rebuild the row's inline material from the new data (rows read with material=True).
        later: leave the original receipts untouched and vouch for the new image with one more receipt,
        as if a later transaction had rewritten the row.
        """
        record = self.rows[key]
        payload = copy.deepcopy(record.payload)
        payload["data"] = change(payload["data"])
        if material:
            payload["material"] = _material(payload["data"])
        return self._replace(key, record, payload, later)

    def material(self, key, change, *, later=True):
        """Replace only the row's inline material with change(copy of material)."""
        record = self.rows[key]
        payload = copy.deepcopy(record.payload)
        payload["material"] = change(payload["material"])
        return self._replace(key, record, payload, later)

    def _replace(self, key, record, payload, later):
        old, new = _hash(record.payload), _hash(payload)
        self.rows[key] = Record(payload, record.etag)
        self.sha[_ROW + key] = new
        if later:
            self._vouch(key, new)
        else:
            self._rewrite_writes(key, old, key, new)
        return self

    def _vouch(self, key, fingerprint):
        template = next(iter(self.receipts.values()))
        receipt = copy.deepcopy(template.payload)
        rid = "forged-write-" + hashlib.sha256((key + fingerprint).encode()).hexdigest()[:16]
        receipt["request"] = {"request_id": rid, "request_digest": "0" * 64, "read_set": []}
        receipt["writes"] = [[key, fingerprint]]
        self.receipts[rid] = Record(receipt, template.etag)

    def drop_receipt(self, request_id):
        """Remove one retained receipt (its rows stay)."""
        del self.receipts[request_id]
        del self.sha[_RECEIPT + request_id]
        return self

    def receipt(self, request_id, change):
        """Rewrite one retained receipt's payload in place (its entity hash follows)."""
        record = self.receipts[request_id]
        self.receipts[request_id] = Record(change(copy.deepcopy(record.payload)), record.etag)
        return self

    def move(self, key, new_key):
        """Retain the same row under another row ID (its envelope and receipts follow)."""
        record = self.rows.pop(key)
        payload = copy.deepcopy(record.payload)
        payload["row_id"] = new_key
        old, new = _hash(record.payload), _hash(payload)
        self.rows[new_key] = Record(payload, record.etag)
        del self.sha[_ROW + key]
        self.sha[_ROW + new_key] = new
        self._rewrite_writes(key, old, new_key, new)
        return self

    def copy(self, key, new_key):
        """Retain a second copy of the row under another row ID, written by its own (unrelated) receipt."""
        record = self.rows[key]
        payload = copy.deepcopy(record.payload)
        payload["row_id"] = new_key
        self.rows[new_key] = Record(payload, record.etag)
        self.sha[_ROW + new_key] = _hash(payload)
        self._vouch(new_key, _hash(payload))
        return self

    def drop(self, key):
        """Remove the row; receipts that once wrote it stay."""
        del self.rows[key]
        del self.sha[_ROW + key]
        return self

    def request(self, request_id, new_request):
        """Replace a receipt's request (rekeying it when the request ID changes)."""
        record = self.receipts.pop(request_id)
        payload = copy.deepcopy(record.payload)
        payload["request"] = new_request.model_dump(mode="json")
        del self.sha[_RECEIPT + request_id]
        self.receipts[new_request.request_id] = Record(payload, record.etag)
        self.sha[_RECEIPT + new_request.request_id] = None
        return self

    def inventory(self):
        for rid, record in self.receipts.items():
            self.sha[_RECEIPT + rid] = _hash(record.payload)
        return replace(self.base, rows=self.rows, receipts=self.receipts, entity_sha256=self.sha)
