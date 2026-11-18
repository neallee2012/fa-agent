"""Original accepted user instructions behind current conversation read authority."""

import hashlib
import json

from .behavior_store import _Receipt, _Row, _json as stored_json
from .errors import Problem


def _original_write(store, request_id, originals, *, request_digest=None):
    saved = store._read("behavior:v1:receipt:" + request_id, _Receipt)
    if saved is None:
        raise Problem(409, "USER_TURN_SOURCE_INVALID", "original user source lacks its committed write receipt")
    receipt = saved[0]
    if (receipt.request.request_id != request_id
            or request_digest is not None and receipt.request.request_digest != request_digest
            or any(dict(receipt.writes).get(key) != hashlib.sha256(
                stored_json(_Row.model_validate_json(json.dumps(row.payload)))).hexdigest()
                for key, row in originals.items())):
        raise Problem(409, "USER_TURN_SOURCE_CHANGED", "original user source differs from its immutable write receipt")

