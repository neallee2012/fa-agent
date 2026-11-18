"""Human-selected memory types keep the original claim wire."""
import json

import pytest

from shared_context import behavior_wire as wire


def test_omitted_and_explicit_note_keep_the_original_claim_wire_bytes():
    source = wire.ExactSourceRef(source_id="source", version=1, sha256="a"*64, byte_length=4)
    original = {
        "request_id": "claim", "version": 1, "text": "Original note.", "supporting": [source.model_dump(mode="json")],
        "effective_from": "2026-09-30T00:00:00Z", "expires_at": None, "supersedes": None,
    }
    parsed = wire.ClaimDraft.model_validate_json(json.dumps(original))
    assert parsed.kind == "note" and json.loads(parsed.model_dump_json()) == original
    assert parsed.model_copy(update={"kind": "note"}).model_dump_json() == parsed.model_dump_json()


@pytest.mark.parametrize("kind", ["system", "approval", "PRIVATE_SCOPE", None, {"kind":"profile"}])
def test_arbitrary_or_untyped_memory_classification_is_rejected(kind):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        wire.ClaimDraft.model_validate_json(json.dumps({
            "request_id": "bad-kind", "version": 1, "text": "Not an authorization.",
            "supporting": [{"source_id":"source","version":1,"sha256":"a"*64,"byte_length":1}],
            "effective_from": "2026-09-30T00:00:00Z", "kind": kind,
        }))
