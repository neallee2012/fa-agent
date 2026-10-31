"""Human-selected memory types through existing signed APIs and the same reader."""
from datetime import timedelta
import json

import pytest

from shared_context import behavior_wire as wire
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_conversation_note_memory import SETTINGS, commit_note, enable_memory, memory_conversations
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn, ordinary, settle


@pytest.mark.parametrize("kind", ["profile", "episode", "note"])
def test_human_selected_type_and_validity_reach_the_same_reader_without_changing_scope(memory_conversations, kind):
    h, control, human = memory_conversations
    settings_id = enable_memory(h)
    origin = open_turn(h, control, human, "typed-origin")
    reader = None
    try:
        settle(origin, ordinary(origin.assignment.sdk_session_id))
        expiry = h.time + timedelta(hours=1)
        reference = commit_note(origin, kind=kind, expires_at=expiry)
        selected = h.client.post(SETTINGS, headers=h.headers(), json={
            "schema_version": "fa-memory-bridge-settings-command-v1", "request_id": "select-typed",
            "expected_settings_id": settings_id, "enabled": True, "recall": {
                "origin_binding_id": origin.binding.binding_id, "claim_id": reference["claim_id"],
                "expected_claim_version": reference["version"], "expected_claim_sha256": reference["sha256"],
                "use_in_new_conversations": True,
            },
        })
        assert selected.status_code == 200, selected.text
        reader = open_turn(h, control, human, "typed-reader")
        capture = control.records[reader.binding.binding_id].profile.memory_scope.recall.source
        result = reader.client.read_reviewed_note()
        assert result.text == "original inventory"
        assert result.applicability.kind == kind
        assert result.applicability.effective_from == h.time and result.applicability.expires_at == expiry
        assert result.source == capture and result.source.team_id == origin.binding.team_id
        assert result.source.subject == origin.binding.subject and result.source.visibility == "team"
        assert result.source.claim_id not in reader.binding.claim_ids
        assert result.usage == "knowledge-data-not-permission"
        assert all(original.text == "original inventory" for original in result.originals)
        settle(reader, ordinary(reader.assignment.sdk_session_id))
        assert h.endpoint.requests == []
    finally:
        if reader is not None:
            reader.client.close()
        origin.client.close()


def test_correction_binds_type_to_review_and_new_readers_never_reactivate_the_old_version(memory_conversations):
    h, control, human = memory_conversations
    settings_id = enable_memory(h)
    origin = open_turn(h, control, human, "corrected-origin")
    old_reader = new_reader = None
    try:
        settle(origin, ordinary(origin.assignment.sdk_session_id))
        first = commit_note(origin, kind="profile")
        path = f"/behavior/v1/bindings/{origin.binding.binding_id}/claims/{first['claim_id']}"
        original = h.client.get(path, headers=h.headers()).json()
        selected = h.client.post(SETTINGS, headers=h.headers(), json={
            "schema_version": "fa-memory-bridge-settings-command-v1", "request_id": "select-before-correction",
            "expected_settings_id": settings_id, "enabled": True, "recall": {
                "origin_binding_id": origin.binding.binding_id, "claim_id": first["claim_id"],
                "expected_claim_version": first["version"], "expected_claim_sha256": first["sha256"],
                "use_in_new_conversations": True,
            },
        })
        assert selected.status_code == 200, selected.text
        old_reader = open_turn(h, control, human, "reader-before-correction")
        assert old_reader.client.read_reviewed_note().applicability.kind == "profile"
        expiry = h.time + timedelta(minutes=20)
        draft = wire.ClaimDraft(
            request_id="human-correction", version=2, kind="episode", text="original inventory for this completed task",
            supporting=tuple(wire.ExactSourceRef.model_validate({k:v for k,v in ref.items() if k != "scope"})
                             for ref in original["candidate"]["supporting"]),
            effective_from=h.time, expires_at=expiry, supersedes=first["sha256"])
        preview = h.client.post(path + "/preview", content=draft.model_dump_json(), headers=h.headers())
        assert preview.status_code == 200, preview.text
        checked = preview.json()
        review = wire.ClaimReview(**draft.model_dump(), decision_id="typed-human-review",
            candidate_sha256=checked["candidate_sha256"], evidence_sha256=checked["evidence_sha256"],
            policy_version=checked["policy_version"],
            reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"))
        forged = review.model_copy(update={"kind": "profile"})
        assert h.client.post(path + "/review", content=forged.model_dump_json(), headers=h.headers()).status_code == 409
        assert h.client.get(path, headers=h.headers()).json() == original
        assert h.client.post(path + "/review", content=review.model_dump_json(), headers=h.headers()).status_code == 200
        assert h.client.get(path, headers=h.headers()).json() == original
        committed = h.client.post(path + "/commit",
            content=wire.Command(request_id=draft.request_id).model_dump_json(), headers=h.headers())
        assert committed.status_code == 200, committed.text
        with pytest.raises(Problem):
            old_reader.client.read_reviewed_note()
        new_reader = open_turn(h, control, human, "reader-after-correction")
        current = new_reader.client.read_reviewed_note()
        assert current.source.claim_version == 2 and current.source.claim_sha256 != first["sha256"]
        assert current.applicability.kind == "episode" and current.applicability.expires_at == expiry
        assert current.text == draft.text
        h.time = expiry
        with pytest.raises(Problem) as expired:
            new_reader.client.read_reviewed_note()
        assert expired.value.body["error"]["code"] == "MEMORY_CLAIM_EXPIRED"
        assert h.endpoint.requests == []
    finally:
        for turn in (new_reader, old_reader, origin):
            if turn is not None:
                turn.client.close()


def test_omitted_and_explicit_note_keep_the_original_claim_and_bridge_wire_bytes():
    from shared_context.memory_note_proposal_bridge import NoteBridgeCommand
    source = wire.ExactSourceRef(source_id="source", version=1, sha256="a"*64, byte_length=4)
    original = {
        "request_id": "claim", "version": 1, "text": "Original note.", "supporting": [source.model_dump(mode="json")],
        "effective_from": "2026-09-30T00:00:00Z", "expires_at": None, "supersedes": None,
    }
    parsed = wire.ClaimDraft.model_validate_json(json.dumps(original))
    assert parsed.kind == "note" and json.loads(parsed.model_dump_json()) == original
    assert parsed.model_copy(update={"kind": "note"}).model_dump_json() == parsed.model_dump_json()
    mapping = NoteBridgeCommand.model_validate_json(json.dumps({
        "schema_version": "fa-note-bridge-v1", "request_id": "map", "source_id": "source", "claim_id": "claim",
        "claim_version": 1, "supersedes": None, "effective_from": original["effective_from"], "expires_at": None,
    }))
    assert mapping.kind == "note" and "kind" not in mapping.model_dump()
    assert mapping.model_copy(update={"kind": "note"}).model_dump_json() == mapping.model_dump_json()


@pytest.mark.parametrize("kind", ["system", "approval", "PRIVATE_SCOPE", None, {"kind":"profile"}])
def test_arbitrary_or_untyped_memory_classification_is_rejected(kind):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        wire.ClaimDraft.model_validate_json(json.dumps({
            "request_id": "bad-kind", "version": 1, "text": "Not an authorization.",
            "supporting": [{"source_id":"source","version":1,"sha256":"a"*64,"byte_length":1}],
            "effective_from": "2026-09-30T00:00:00Z", "kind": kind,
        }))
