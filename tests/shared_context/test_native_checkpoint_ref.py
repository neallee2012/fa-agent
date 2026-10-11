"""ADR-0007 N3: the State store checkpoint reference is its own exact type (shared with N1's turn record)."""
import pytest
from pydantic import ValidationError

from shared_context.behavior_sdk_state import (
    NATIVE_MAX_SEGMENTS, NATIVE_SEGMENT_BYTES, NativeCheckpointRef, NativeCheckpointScope, StoredSdkArchive,
)
from shared_context.backup_models import MAX_BACKUP_BYTES

SHA = "a" * 64


def ref(**changes):
    value = {"codec": "native-session-fs-v1", "agent_id": "fa-agent-cc96", "conversation_id": "cid-1",
             "sdk_session_id": "sdk-1", "sha256": SHA, "byte_length": NATIVE_SEGMENT_BYTES + 1, "segments": 2}
    value.update(changes)
    return NativeCheckpointRef.model_validate(value)


def test_reference_round_trips_and_names_its_scope():
    original = ref()
    assert NativeCheckpointRef.model_validate_json(original.model_dump_json()) == original
    assert original.scope == NativeCheckpointScope(agent_id="fa-agent-cc96", conversation_id="cid-1",
                                                   sdk_session_id="sdk-1")
    assert NATIVE_MAX_SEGMENTS * NATIVE_SEGMENT_BYTES >= MAX_BACKUP_BYTES


@pytest.mark.parametrize("changes", [
    {"segments": 1},
    {"segments": 3},
    {"codec": "native-session-fs-responses-v1"},
    {"model_provider": "openai-responses"},
    {"codec": "artifact-session-fs-v1"},
    {"byte_length": MAX_BACKUP_BYTES + 1, "segments": NATIVE_MAX_SEGMENTS + 1},
])
def test_reference_refuses_partial_coverage_or_a_foreign_codec(changes):
    with pytest.raises(ValidationError):
        ref(**changes)


def test_responses_reference_needs_the_responses_provider():
    assert ref(codec="native-session-fs-responses-v1", model_provider="openai-responses").scope.model_provider \
        == "openai-responses"


def test_reference_refuses_extra_fields_and_is_not_a_blob_archive():
    raw = ref().model_dump(mode="json")
    with pytest.raises(ValidationError):
        NativeCheckpointRef.model_validate({**raw, "store": "fa/v1/conv/other"})
    with pytest.raises(ValidationError):
        StoredSdkArchive.model_validate(raw)
