"""An exact saved assistant draft is a candidate, not evidence for its own claims."""

import hashlib
import json
import copy
from types import SimpleNamespace

import pytest

from shared_context.memory_bridge_settings import MemoryBridgeSettingsService, SelectMemoryBridgeSettings
from shared_context.memory_note_proposal_api import install_note_proposal_routes
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured
from test_conversation_transfer_startup import read_startup, saved_turn, synthetic_standing_v1
from test_instruction_flow import instruction_models, runtime_path, sdk_io
from test_instruction_prompt import submit
from test_m3_auth import jwk_client, signing_key

FIRST = "The project ships only to Taiwan."
SECOND = "Support is available 09:00-17:00 UTC. Keep the shipping region."
SUMMARY = "The project ships only to Taiwan, with support from 09:00 to 17:00 UTC."


def fragment(text, quote, turn):
    start = text.index(quote)
    begin = len(text[:start].encode("utf-8"))
    return {
        "turn_number": turn, "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "start_byte": begin, "end_byte": begin + len(quote.encode("utf-8")),
    }


@pytest.fixture
def synthesis_case(assistant, monkeypatch, request):
    h, control, human = configured(assistant)
    oversized = getattr(request, "param", "") == "oversized-bundle"
    first_text = "A" * 4096 if oversized else FIRST
    second_text = "B" * 4096 if oversized else SECOND
    sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "The original shipping region is retained."}, {"content": SUMMARY}])
    MemoryBridgeSettingsService(h.runtime).select(human, SelectMemoryBridgeSettings(
        schema_version="fa-memory-bridge-settings-command-v1", request_id="synthesis-memory-enabled",
        expected_settings_id=None, enabled=True))
    first, _ = submit(h, "synthesis-first", first_text)
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    second, _ = submit(h, "synthesis-second", second_text, first["conversation_id"])
    assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
    install_note_proposal_routes(h.app, h.runtime, h.verifier)
    binding = h.runtime.bindings[second["binding_id"]]
    command = {
        "schema_version": "fa-note-synthesis-command-v1", "request_id": "human-synthesis",
        "conversation_id": first["conversation_id"],
        "summary": fragment(SUMMARY, SUMMARY, 2),
        "supporting": [fragment(first_text, first_text, 1),
                       fragment(second_text, second_text if oversized else "Support is available 09:00-17:00 UTC.", 2)],
        "share_team_id": binding.team_id, "share_work_id": binding.work.work_id,
        "share_confirmed": True,
    }
    return SimpleNamespace(h=h, control=control, human=human, binding=binding,
                           first=first, second=second, command=command,
                           path="/behavior/v1/bindings/" + binding.binding_id + "/note-syntheses")


def test_saved_synthesis_keeps_summary_separate_from_exact_original_evidence(synthesis_case):
    c = synthesis_case
    original = c.h.runtime.current_claim(c.binding.binding_id, c.binding.claim_ids[0], c.human)
    before = c.h.store._guard()
    preview = c.h.client.post(c.path + "/preview", json=c.command, headers=c.h.headers("read-only"))
    assert preview.status_code == 200, preview.text
    assert c.h.store._guard() == before
    value = preview.json()
    assert value["text"] == SUMMARY
    assert [item["text"] for item in value["evidence"]["fragments"]] == [
        FIRST, "Support is available 09:00-17:00 UTC."]
    assert all(item["source"]["speaker"] == "user" for item in value["evidence"]["fragments"])
    assert value["summary_source"]["speaker"] == "assistant"
    assert value["effective"] is False
    created = c.h.client.post(c.path, json=c.command, headers=c.h.headers("read-only"))
    assert created.status_code == 200, created.text
    assert created.json()["result"]["state"] == "pending_review"
    read = c.h.client.get(c.path + "/" + c.command["request_id"], headers=c.h.headers("read-only"))
    assert read.status_code == 200 and read.json()["text"] == SUMMARY
    assert read.json()["evidence"] == value["evidence"]
    assert c.h.runtime.current_claim(c.binding.binding_id, c.binding.claim_ids[0], c.human) == original


def test_synthesis_review_uses_original_bundle_not_the_summary_as_evidence(synthesis_case):
    from shared_context.memory_bridge_settings import MemoryRecallChoice, memory_slot_ids
    from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE
    from shared_context.behavior_wire import SourceWithdrawal
    from shared_context.errors import Problem
    from test_conversation_profile_selection import select, template
    from test_memory_note_bridge import review_command
    from test_sdk_compaction_product import open_turn

    c = synthesis_case
    assert c.h.client.post(c.path, json=c.command, headers=c.h.headers("read-only")).status_code == 200
    source_id, claim_id = memory_slot_ids(c.control.records[c.binding.binding_id].admission_id)
    mapping = {
        "schema_version": "fa-note-bridge-v1", "request_id": "human-synthesis-bridge",
        "source_id": source_id, "claim_id": claim_id, "claim_version": 1,
        "supersedes": None, "effective_from": c.h.time.isoformat(), "expires_at": None,
    }
    path = c.path + "/" + c.command["request_id"] + "/bridge"
    prepared = c.h.client.post(path + "/prepare", json=mapping, headers=c.h.headers())
    assert prepared.status_code == 200, prepared.text
    preview = c.h.client.post(path + "/preview", json=mapping, headers=c.h.headers())
    assert preview.status_code == 200, preview.text
    value = preview.json()
    assert value["text"] == SUMMARY
    assert [item["text"] for item in value["evidence"]["fragments"]] == [
        FIRST, "Support is available 09:00-17:00 UTC."]
    original = c.h.client.get(
        "/behavior/v1/bindings/" + c.binding.binding_id + "/sources/" + source_id + "/versions/1",
        headers=c.h.headers("read-only"))
    assert original.status_code == 200
    assert json.loads(original.content) == value["evidence"]
    assert SUMMARY not in original.content.decode("utf-8")
    assert hashlib.sha256(original.content).hexdigest() == value["source"]["sha256"]
    assert c.h.client.post(path + "/commit", json=mapping, headers=c.h.headers()).status_code != 200
    review = review_command(mapping, value)
    assert c.h.client.post(path + "/review", json=review, headers=c.h.headers("read-only")).status_code == 403
    assert c.h.client.post(path + "/review", json=review, headers=c.h.headers()).status_code == 200
    committed = c.h.client.post(path + "/commit", json=mapping, headers=c.h.headers())
    assert committed.status_code == 200, committed.text
    claim = c.h.runtime.current_claim(c.binding.binding_id, claim_id, c.human)
    assert claim.candidate.text == SUMMARY and claim.candidate.supporting[0].source_id == source_id
    assert claim.candidate.supporting[0].sha256 == hashlib.sha256(original.content).hexdigest()
    selected, _ = select(c.h, value=template(
        HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile="fa-user-text-files-v1"))
    assert selected.status_code == 200
    settings = MemoryBridgeSettingsService(c.h.runtime)
    settings.select(c.human, SelectMemoryBridgeSettings(
        schema_version="fa-memory-bridge-settings-command-v1", request_id="recall-synthesis",
        expected_settings_id=settings.current(c.human)["settings_id"], enabled=True,
        recall=MemoryRecallChoice(
            origin_binding_id=c.binding.binding_id, claim_id=claim_id,
            expected_claim_version=claim.reference.version, expected_claim_sha256=claim.reference.sha256,
            use_in_new_conversations=True,
        )))
    next_turn = open_turn(c.h, c.control, c.human, "read-reviewed-synthesis")
    try:
        note = next_turn.client.read_reviewed_note()
        assert note.text == SUMMARY
        assert len(note.originals) == 1
        assert json.loads(note.originals[0].text) == value["evidence"]
        assert note.usage == "knowledge-data-not-permission"
        withdrawn = c.h.client.post(
            "/behavior/v1/bindings/" + c.binding.binding_id + "/sources/" + source_id + "/withdraw",
            headers=c.h.headers(), content=SourceWithdrawal(
                request_id="withdraw-synthesis-source", withdrawal_id="withdraw-synthesis-source",
                reference=note.source.sources[0], expected_access_revision=1).model_dump_json())
        assert withdrawn.status_code == 200
        before = c.h.store._guard()
        with pytest.raises(Problem):
            next_turn.client.read_reviewed_note()
        assert c.h.store._guard() == before
    finally:
        next_turn.client.close()


@pytest.mark.parametrize("change", ["wrong-sha", "future", "same-turn", "other-team", "caller-text", "no-consent"])
def test_invalid_synthesis_does_not_write_or_disclose_a_partial_bundle(synthesis_case, change):
    c = synthesis_case
    changed = copy.deepcopy(c.command)
    if change == "wrong-sha":
        changed["supporting"][0]["source_sha256"] = "0" * 64
    elif change == "future":
        changed["supporting"][1]["turn_number"] = 3
    elif change == "same-turn":
        changed["supporting"][1] = dict(changed["supporting"][0])
    elif change == "other-team":
        changed["share_team_id"] = "other-team"
    elif change == "caller-text":
        changed["text"] = "A fabricated replacement summary."
    else:
        changed["share_confirmed"] = False
    before = c.h.store._guard()
    response = c.h.client.post(c.path, json=changed, headers=c.h.headers("read-only"))
    assert response.status_code in {403, 409, 422}
    assert "evidence" not in response.json()
    assert c.h.store._guard() == before


def test_synthesis_cannot_replace_an_unrelated_allowlisted_claim(synthesis_case):
    from shared_context.memory_bridge_settings import memory_slot_ids

    c = synthesis_case
    assert c.h.client.post(c.path, json=c.command, headers=c.h.headers("read-only")).status_code == 200
    original = c.h.runtime.current_claim(c.binding.binding_id, c.binding.claim_ids[0], c.human)
    source_id, _ = memory_slot_ids(c.control.records[c.binding.binding_id].admission_id)
    mapping = {
        "schema_version": "fa-note-bridge-v1", "request_id": "must-not-replace",
        "source_id": source_id, "claim_id": original.reference.claim_id,
        "claim_version": original.reference.version + 1, "supersedes": original.reference.sha256,
        "effective_from": c.h.time.isoformat(),
    }
    before = c.h.store._guard()
    response = c.h.client.post(c.path + "/" + c.command["request_id"] + "/bridge/prepare",
                               json=mapping, headers=c.h.headers())
    assert response.status_code == 403
    assert c.h.store._guard() == before
    assert c.h.runtime.current_claim(c.binding.binding_id, original.reference.claim_id, c.human) == original


def test_synthesis_record_is_understood_by_the_same_startup_readers(synthetic_standing_v1):
    from shared_context.memory_note_synthesis import NoteSynthesisCommand, NoteSynthesisService

    c = synthetic_standing_v1
    first = saved_turn(c, "synthesis-startup-first")
    second = saved_turn(c, "synthesis-startup-second",
                        first.assignment.task.instruction.conversation_id, first.state)
    first_text, second_text = (turn.assignment.task.instruction.content for turn in (first, second))
    reply = c.control.read_reply(c.human, second.assignment.task.instruction.conversation_id, 2).text
    calls = tuple(c.sessions.calls)
    command = NoteSynthesisCommand.model_validate_json(json.dumps({
        "schema_version": "fa-note-synthesis-command-v1", "request_id": "startup-synthesis",
        "conversation_id": first.assignment.task.instruction.conversation_id,
        "summary": fragment(reply, reply, 2),
        "supporting": [fragment(first_text, first_text, 1), fragment(second_text, second_text, 2)],
        "share_team_id": second.binding.team_id, "share_work_id": second.binding.work.work_id,
        "share_confirmed": True,
    }))
    receipt = NoteSynthesisService(c.h.runtime).create(second.binding.binding_id, command, c.human)
    inventory = read_startup(c)
    key = receipt.payload["result"]["proposal_id"]
    assert inventory.rows[key].payload["data"]["kind"] == "memory_note_synthesis"
    assert tuple(c.sessions.calls) == calls


def test_confirmed_synthesis_replays_only_read_and_changed_selection_is_refused(synthesis_case, monkeypatch):
    c = synthesis_case
    first = c.h.client.post(c.path, json=c.command, headers=c.h.headers("read-only"))
    assert first.status_code == 200
    before = c.h.store._guard()
    monkeypatch.setattr(c.h.store, "transact", lambda *_: pytest.fail("a confirmed replay must only read"))
    for path in (c.path, c.path + "/reconcile"):
        assert c.h.client.post(path, json=c.command, headers=c.h.headers("read-only")).json() == first.json()
    changed = copy.deepcopy(c.command)
    changed["supporting"][0]["end_byte"] -= 1
    assert c.h.client.post(c.path, json=changed, headers=c.h.headers("read-only")).status_code == 409
    assert c.h.store._guard() == before


@pytest.mark.parametrize("after_commit", [False, True])
def test_synthesis_unknown_does_not_create_a_second_proposal(synthesis_case, monkeypatch, after_commit):
    from shared_context.behavior_operations import control_key
    from shared_context.memory_note_synthesis import synthesis_key

    c = synthesis_case
    key = synthesis_key(c.h.store._scope.authority_domain, c.human.oid, c.command["request_id"])
    request_id = control_key("memory-note-synthesis-write", key)
    original, calls = c.h.store._submit, []

    def lost(operations, request):
        if request is not None and request.request_id == request_id:
            calls.append(request.request_id)
            if not after_commit:
                from shared_context.errors import Problem
                raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "synthetic loss before transaction confirmation")
            c.h.transport.after_apply = "lost"
        try:
            return original(operations, request)
        finally:
            c.h.transport.after_apply = None

    monkeypatch.setattr(c.h.store, "_submit", lost)
    response = c.h.client.post(c.path, json=c.command, headers=c.h.headers("read-only"))
    assert response.status_code == 503
    before = c.h.store._guard()
    monkeypatch.setattr(c.h.store, "transact", lambda *_: pytest.fail("unknown lookup must not write"))
    lookup = c.h.client.post(c.path + "/reconcile", json=c.command, headers=c.h.headers("read-only"))
    assert lookup.status_code == (200 if after_commit else 503)
    assert calls == [request_id] and c.h.store._guard() == before


def test_synthesis_requires_human_current_read_at_every_stage(synthesis_case):
    c = synthesis_case
    before = c.h.store._guard()
    assert c.h.client.post(c.path, json=c.command, headers=c.h.headers("agent")).status_code == 403
    assert c.h.store._guard() == before
    assert c.h.client.post(c.path, json=c.command, headers=c.h.headers("read-only")).status_code == 200
    revoked = c.h.client.post("/operator/v1/standing-work-policy/revoke",
                             json={"decision_id": "revoke-synthesis"}, headers=c.h.headers())
    assert revoked.status_code == 200
    before = c.h.store._guard()
    assert c.h.client.get(c.path + "/" + c.command["request_id"], headers=c.h.headers("read-only")).status_code == 403
    assert c.h.client.post(c.path + "/reconcile", json=c.command, headers=c.h.headers("read-only")).status_code == 403
    assert c.h.store._guard() == before


@pytest.mark.parametrize("synthesis_case", ["oversized-bundle"], indirect=True)
def test_complete_evidence_byte_limit_is_checked_before_any_write(synthesis_case):
    c = synthesis_case
    assert [item["end_byte"] - item["start_byte"] for item in c.command["supporting"]] == [4096, 4096]
    before = c.h.store._guard()
    response = c.h.client.post(c.path, json=c.command, headers=c.h.headers("read-only"))
    assert response.status_code == 413
    assert c.h.store._guard() == before
