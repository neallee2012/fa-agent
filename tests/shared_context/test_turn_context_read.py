"""Per-turn saved metadata through the signed API; SDK/model I/O stays offline."""

import copy
from contextvars import Context
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from conftest import A
from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_checkpoint import export
from shared_context.behavior_assistant_wire import ArchiveUnavailable, SdkArchive, StoredSdkArchive
from shared_context.behavior_instruction import SavedReply, TurnArchive, turn_material
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.records import Record
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import UNKNOWN_OID, token
from test_reviewed_work_memory import NOTE, configured_memory, reviewed_note
from test_sdk_compaction_product import checkpoint_case, open_turn, ordinary, settle
from test_skill_purpose import BODY, PURPOSE
from test_skill_purpose_api import add_purpose_skill, configured_purpose
from test_web_test_console_frozen_interface import complete, post, real_interface, signing_key, jwk_client
from test_web_test_console_prompt_assembly import ASSEMBLY_PROFILES, profile_interface
from test_work_policy_lifecycle import enabled


def saved_turn(case):
    draft = case.browser.ui.get("/api/state").json()["composer"]
    sent = post(case, "/api/instructions/" + draft["request_id"] + "/submit",
                {"content": "Read only this turn's saved metadata. 原文🙂", "model": draft["model"]})
    assert sent.status_code == 200, sent.text
    task = sent.json()["task"]
    complete(case, task["binding_id"])
    cid = task["admission"]["conversation_id"]
    path = f"/work-policy/v1/conversations/{cid}/turns/1/context"
    return draft, task, path


@pytest.mark.parametrize("profile_interface", [None, *ASSEMBLY_PROFILES], indirect=True)
def test_context_read_reports_recorded_bytes_and_explicit_unknown_tokens(profile_interface):
    c = profile_interface
    _, task, path = saved_turn(c)
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    result = c.h.client.get(path, headers=c.h.headers())
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["x-content-sha256"] == hashlib.sha256(result.content).hexdigest()
    data = result.json()
    assert data["schema_version"] == "fa-turn-context-v1"
    assert (data["request_id"], data["job_id"], data["conversation_id"], data["turn_number"]) == (
        task["task_id"], task["job_id"], task["admission"]["conversation_id"], 1,
    )
    assert data["source"] == "saved-turn-metadata"
    assert data["authorization"] is False and data["read_is_acknowledgment"] is False
    assert data["token_usage"] == {
        "full_request_tokens": None, "provider_input_tokens": None, "provider_output_tokens": None,
        "reason": "complete-turn-usage-not-recorded",
    }
    assert data["wall_clock_seconds"] is None and data["ui_rendered_bytes"] is None
    akey, rkey = key("text-archive", task["job_id"]), key("reply", task["job_id"])
    stored = c.h.store.read_snapshot((akey, rkey))
    archive = TurnArchive.model_validate_json(json.dumps(stored[akey].payload["data"]))
    reply = SavedReply.model_validate_json(json.dumps(stored[rkey].payload["data"]))
    public_reply = c.h.client.get(path.removesuffix("context") + "reply", headers=c.h.headers())
    assert public_reply.status_code == 200
    assert data["reply"] == {
        "json_utf8_bytes": len(public_reply.content),
        "text_utf8_bytes": len(public_reply.json()["text"].encode("utf-8")),
        "stored_zlib_bytes": len(reply.data), "sha256": reply.sha256,
    }
    assert data["archive"]["sha256"] == archive.archive.sha256
    assert data["archive"]["sdk_version"] == archive.archive.sdk_version
    assert data["archive"]["runtime_version"] == archive.archive.runtime_version
    if isinstance(archive.archive, StoredSdkArchive):
        assert data["archive"]["encoded_state_bytes"] == archive.archive.byte_length
        assert data["archive"]["part_count"] == len(archive.archive.parts)
        assert data["archive"]["inline_zlib_bytes"] is None
    else:
        assert isinstance(archive.archive, SdkArchive)
        assert data["archive"]["inline_zlib_bytes"] == len(archive.archive.data)
        assert data["archive"]["encoded_state_bytes"] is None
        assert data["archive"]["part_count"] is None
    assert data["archive"]["content_reverified"] is False
    expected = assemble_prompt(c.control.policy.profile.prompt_profile or "fa-instruction-v2").record
    assert data["prompt_assembly"] == (None if expected is None else expected.model_dump(mode="json"))
    assert c.h.client.get(path, headers=c.h.headers()).content == result.content
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
    assert len(c.starts) == len(c.sdk_calls) == 1
    assert "OFFLINE SDK I/O substitute reply" not in result.text
    assert "原文" not in result.text and "events.jsonl" not in result.text


def test_bridge_reads_only_the_original_admitted_turn_without_acknowledgment(real_interface):
    c = real_interface
    _, task, _ = saved_turn(c)
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    c.calls.clear()
    response = c.browser.ui.get("/api/instructions/" + task["task_id"] + "/context")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["task_id"] == task["task_id"]
    assert data["context"]["job_id"] == task["job_id"]
    assert data["context"]["read_is_acknowledgment"] is False
    assert len(c.calls) == 1 and c.calls[0]["method"] == "GET" and c.calls[0]["path"].endswith("/context")
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
    assert len(c.starts) == len(c.sdk_calls) == 1


@pytest.mark.parametrize("claims,remove,status", [
    ({"oid": A.oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, ("scp",), 403),
    ({"oid": UNKNOWN_OID}, (), 403),
    ({"appid": UNKNOWN_OID}, (), 403),
    ({"scp": "Behavior.Approve"}, (), 403),
    ({"tid": UNKNOWN_OID}, (), 401),
    ({"exp": 1}, (), 401),
])
def test_context_read_requires_the_current_designated_human(real_interface, signing_key, claims, remove, status):
    c = real_interface
    _, _, path = saved_turn(c)
    assert c.h.client.get(path, headers=c.h.headers("read-only")).status_code == 200
    rows = copy.deepcopy(c.h.transport.backing.rows)
    encoded = token(signing_key, {"scp": "Behavior.Read", **claims}, remove=remove)
    denied = c.h.client.get(path, headers={"Authorization": "Bearer " + encoded})
    assert denied.status_code == status, denied.text
    assert "token_usage" not in denied.json()
    assert c.h.transport.backing.rows == rows


@pytest.mark.parametrize("query,body", [
    ("?team_id=other", b""), ("?include_body=true", b""),
    ("", b'{"authorization":true,"secret":"CONTEXT_INPUT_CANARY"}'),
])
def test_context_read_cannot_select_scope_or_return_bodies(real_interface, query, body):
    c = real_interface
    _, _, path = saved_turn(c)
    assert c.h.client.get(path, headers=c.h.headers()).status_code == 200
    c.h.transport.requests.clear()
    denied = c.h.client.request("GET", path + query, content=body, headers=c.h.headers())
    assert denied.status_code == 422
    assert c.h.transport.requests == [] and "CONTEXT_INPUT_CANARY" not in denied.text


def test_context_authentication_precedes_input_and_storage(real_interface):
    c = real_interface
    c.h.transport.requests.clear()
    denied = c.h.client.request("GET", "/work-policy/v1/conversations/unknown/turns/1/context?include_body=true",
                               content=b"CONTEXT_INPUT_CANARY")
    assert denied.status_code == 401 and c.h.transport.requests == []


@pytest.mark.parametrize("query,body", [
    ("?conversation_id=another", b""), ("", b'{"include_body":true}'),
])
def test_bridge_context_inputs_cannot_select_another_scope(real_interface, query, body):
    c = real_interface
    _, task, _ = saved_turn(c)
    path = "/api/instructions/" + task["task_id"] + "/context"
    assert c.browser.ui.get(path).status_code == 200
    c.calls.clear()
    denied = c.browser.ui.request("GET", path + query, content=body)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "LOCAL_REQUEST_DENIED"
    assert c.calls == []


def test_unsaved_context_is_not_zero_usage_or_a_new_execution(real_interface):
    c = real_interface
    draft = c.browser.ui.get("/api/state").json()["composer"]
    sent = post(c, "/api/instructions/" + draft["request_id"] + "/submit",
                {"content": "This turn is still queued.", "model": draft["model"]})
    assert sent.status_code == 200
    cid = sent.json()["task"]["admission"]["conversation_id"]
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    read = c.h.client.get(f"/work-policy/v1/conversations/{cid}/turns/1/context", headers=c.h.headers())
    assert read.status_code == 404 and read.json()["error"]["code"] == "TURN_CONTEXT_NOT_READY"
    assert "token_usage" not in read.json()
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
    assert c.starts == c.sdk_calls == []


@pytest.mark.parametrize("during_read", [False, True])
def test_context_read_rechecks_revocation_in_the_consistent_snapshot(real_interface, monkeypatch, during_read):
    c = real_interface
    _, task, path = saved_turn(c)
    assert c.h.client.get(path, headers=c.h.headers()).status_code == 200
    human = c.h.verifier(c.h.client.build_request("GET", "/", headers=c.h.headers()))
    if not during_read:
        c.control.revoke(human, "revoke-context-access")
    else:
        original, injected = c.h.store.read_snapshot, []

        def interleave(keys):
            value = original(keys)
            if key("text-archive", task["job_id"]) in keys and not injected:
                injected.append(True)
                Context().run(c.control.revoke, human, "revoke-during-context-read")
            return value

        monkeypatch.setattr(c.h.store, "read_snapshot", interleave)
    denied = c.h.client.get(path, headers=c.h.headers())
    assert denied.status_code == 403, denied.text
    assert "token_usage" not in denied.json() and denied.json()["error"]["automatic_retry"] is False
    assert len(c.starts) == len(c.sdk_calls) == 1
    if during_read:
        assert injected == [True]


@pytest.mark.parametrize("change", ["material", "turn", "conversation", "archive_scope", "reply_hash"])
def test_changed_saved_material_is_not_projected_as_metrics(real_interface, monkeypatch, change):
    c = real_interface
    _, task, path = saved_turn(c)
    assert c.h.client.get(path, headers=c.h.headers()).status_code == 200
    akey = key("text-archive", task["job_id"])
    if change == "reply_hash":
        c.h.mutate(key("reply", task["job_id"]), sha256="f" * 64)
    elif change == "material":
        c.h.mutate(akey, context_read=True)
    else:
        row = c.h.store.read_snapshot((akey,))[akey]
        saved = TurnArchive.model_validate_json(json.dumps(row.payload["data"]))
        if change == "turn":
            changed = saved.model_copy(update={"turn_number": 2})
        elif change == "conversation":
            changed = saved.model_copy(update={"conversation_id": "another-conversation"})
        else:
            assert isinstance(saved.archive, StoredSdkArchive)
            changed = saved.model_copy(update={"archive": saved.archive.model_copy(update={"team_id": "another-team"})})
        snapshot = c.h.store.read_snapshot

        def wrong_returned_record(keys):
            result = dict(snapshot(keys))
            if akey in result and result[akey] is not None:
                original = result[akey]
                payload = {**original.payload, "data": changed.model_dump(mode="json"),
                           "material": turn_material(changed).model_dump(mode="json")}
                result[akey] = Record(payload, original.etag)
            return result

        monkeypatch.setattr(c.h.store, "read_snapshot", wrong_returned_record)
    before = copy.deepcopy(c.h.transport.backing.rows)
    denied = c.h.client.get(path, headers=c.h.headers())
    assert denied.status_code == 409, denied.text
    assert "token_usage" not in denied.json() and c.h.transport.backing.rows == before


@pytest.mark.parametrize("change", [
    "schema", "job", "request", "conversation", "turn", "model", "future", "unknown_tokens",
    "mixed_bytes", "missing_compaction", "authorization", "private_body", "duplicate", "missing_digest",
])
def test_bridge_rejects_bad_context_without_losing_saved_results(real_interface, change):
    c = real_interface
    draft, task, api_path = saved_turn(c)
    progress = c.browser.ui.get("/api/instructions/progress", params={"thread_id": draft["thread_id"]})
    assert progress.status_code == 200
    bridge_path = "/api/instructions/" + task["task_id"] + "/context"
    assert c.browser.ui.get(bridge_path).status_code == 200
    instructions = c.browser.app.state.console.instructions
    replies, projections = copy.deepcopy(instructions.replies), copy.deepcopy(instructions.projections)
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()

    def malformed(request, response):
        if request.url.path != api_path or response.status_code != 200:
            return response
        data = response.json()
        if change == "schema": data["schema_version"] = "future"
        elif change == "job": data["job_id"] = "another-job"
        elif change == "request": data["request_id"] = "another-request"
        elif change == "conversation": data["conversation_id"] = "another-conversation"
        elif change == "turn": data["turn_number"] = 2
        elif change == "model": data["model"] = "configured-second-model"
        elif change == "future": data["read_at"] = (c.h.time + timedelta(seconds=1)).isoformat()
        elif change == "unknown_tokens": data["token_usage"]["provider_input_tokens"] = 0
        elif change == "mixed_bytes": data["archive"]["inline_zlib_bytes"] = 1
        elif change == "missing_compaction": data["archive"]["codec"] = "artifact-session-fs-diag-empty-v1"
        elif change == "authorization": data["authorization"] = True
        elif change == "private_body": data["body"] = "PRIVATE_CONTEXT_BODY_CANARY"
        raw = json.dumps(data, ensure_ascii=False).encode()
        if change == "duplicate":
            raw = b'{"schema_version":"fa-turn-context-v1",' + raw[1:]
        headers = {"Cache-Control": "no-store", "x-content-sha256": hashlib.sha256(raw).hexdigest()}
        if change == "missing_digest":
            del headers["x-content-sha256"]
        return httpx.Response(200, content=raw, headers=headers)

    c.mutate = malformed
    denied = c.browser.ui.get(bridge_path)
    assert denied.status_code == 502 and denied.json()["error"]["code"] == "TURN_CONTEXT_INVALID"
    assert "PRIVATE_CONTEXT_BODY_CANARY" not in denied.text
    assert instructions.replies == replies and instructions.projections == projections
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
    assert len(c.starts) == len(c.sdk_calls) == 1


@pytest.mark.parametrize("kind", ["inline", "unavailable"])
def test_legacy_and_unavailable_archives_keep_distinct_measurements(assistant, tmp_path, kind):
    h, control, human, _ = enabled(assistant)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    turn = open_turn(h, control, human, "context-" + kind)
    try:
        if kind == "inline":
            original = ordinary(turn.assignment.sdk_session_id)
            folder = tmp_path / "config" / "session-state" / original.sdk_session_id
            folder.mkdir(parents=True)
            for name, text in original.files.items():
                (folder / name).write_text(text, encoding="utf-8")
            archive = export(tmp_path, original.sdk_session_id)
        else:
            archive = ArchiveUnavailable(error="SDK_ARCHIVE_UNAVAILABLE")
        turn.client.save_text_reply("Public reply keeps exact UTF-8 bytes. 原文🙂", archive, turn.stop)
        path = f"/work-policy/v1/conversations/{turn.assignment.task.instruction.conversation_id}/turns/1/context"
        before = copy.deepcopy(h.transport.backing.rows)
        result = h.client.get(path, headers=h.headers())
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["archive"]["encoded_state_bytes"] is None and data["archive"]["part_count"] is None
        assert data["reply"]["text_utf8_bytes"] == len("Public reply keeps exact UTF-8 bytes. 原文🙂".encode())
        if kind == "inline":
            assert data["archive"]["inline_zlib_bytes"] == len(archive.data)
            assert data["archive"]["unavailable"] is None
        else:
            assert data["archive"]["unavailable"] == {"error": "SDK_ARCHIVE_UNAVAILABLE"}
            assert data["archive"]["inline_zlib_bytes"] is None and data["archive"]["sha256"] is None
            assert data["archive"]["sdk_version"] is None and data["archive"]["runtime_version"] is None
        assert h.transport.backing.rows == before and "原文" not in result.text
    finally:
        turn.client.close()


def test_saved_catalog_is_references_not_body_purpose_or_load_proof(assistant, signing_key, jwk_client, monkeypatch):
    version = add_purpose_skill(assistant, signing_key, jwk_client)
    h, control, human = configured_purpose(assistant)
    turn = open_turn(h, control, human, "catalog-context")
    try:
        settle(turn, ordinary(turn.assignment.sdk_session_id))
        before = copy.deepcopy(h.transport.backing.rows)

        def no_blob(*args, **kwargs):
            raise AssertionError("Context metadata must not download checkpoint or skill bodies")

        monkeypatch.setattr(h.assistant.artifacts.store, "read", no_blob)
        path = f"/work-policy/v1/conversations/{turn.assignment.task.instruction.conversation_id}/turns/1/context"
        result = h.client.get(path, headers=h.headers())
        assert result.status_code == 200, result.text
        assert result.json()["skill_catalog"] == {
            "catalog_schema": "fa-approved-skill-catalog-v2",
            "entries": [{"name": "receipt-guide", "version_id": version["version_id"],
                         "sha256": version["sha256"], "metadata_sha256": version["metadata_sha256"]}],
            "unavailable_count": 0, "usage": "historical-catalog-not-load-or-permission-proof",
        }
        for private in (BODY.decode(), PURPOSE, "Not the approved purpose", h.binding.tenant_id, h.binding.agent_oid):
            assert private not in result.text
        assert h.transport.backing.rows == before
    finally:
        turn.client.close()


def test_saved_compaction_observation_is_not_a_new_execution_or_provider_usage(checkpoint_case, monkeypatch):
    c = checkpoint_case
    reference = settle(c, c.fs.snapshot(), compaction=c.fs.compaction)
    before = copy.deepcopy(c.h.transport.backing.rows)

    def no_blob(*args, **kwargs):
        raise AssertionError("Reading metadata must not load SDK state")

    monkeypatch.setattr(c.h.assistant.artifacts.store, "read", no_blob)
    task = c.assignment.task.instruction
    response = c.h.client.get(
        f"/work-policy/v1/conversations/{task.conversation_id}/turns/{task.turn_number}/context",
        headers=c.h.headers())
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["compaction"] == {
        "source": "stored-checkpoint-observation", "observation_job_id": c.binding.job_id,
        "trigger": "manual", "checkpoint_number": 1,
        "summary_sha256": c.fs.compaction.summary_sha256, "messages_removed": 2, "tokens_removed": 20,
        "previous_state_sha256": c.previous.sha256,
    }
    assert data["archive"]["encoded_state_bytes"] == reference.byte_length
    assert data["token_usage"]["provider_input_tokens"] is None and data["wall_clock_seconds"] is None
    assert "PRIVATE_FIXTURE_SYSTEM" not in response.text and "summaryContent" not in response.text
    assert c.h.transport.backing.rows == before


@pytest.mark.parametrize("followups", [1, 2])
def test_later_turn_context_retains_inherited_compaction_without_claiming_a_new_execution(
        checkpoint_case, monkeypatch, followups):
    c = checkpoint_case
    settle(c, c.fs.snapshot(), compaction=c.fs.compaction)
    opened = []
    try:
        for index in range(followups):
            turn = open_turn(c.h, c.control, c.human, "context-followup-" + str(index),
                             c.assignment.task.instruction.conversation_id)
            opened.append(turn)
            turn.stop = turn.stop.model_copy(update={"pid": 100 + index})
            reference = settle(turn, turn.client.restore())
        assert reference.source.execution == c.fs.compaction
        assert reference.job_id != reference.source.execution.job_id
        def no_blob(*args, **kwargs):
            raise AssertionError("Inherited metadata must not download or decode SDK state")
        monkeypatch.setattr(c.h.assistant.artifacts.store, "read", no_blob)
        before = copy.deepcopy(c.h.transport.backing.rows)
        task = turn.assignment.task.instruction
        response = c.h.client.get(
            f"/work-policy/v1/conversations/{task.conversation_id}/turns/{task.turn_number}/context",
            headers=c.h.headers())
        assert response.status_code == 200, response.text
        observed = response.json()["compaction"]
        assert observed["source"] == "inherited-checkpoint-observation"
        assert observed["observation_job_id"] == c.binding.job_id
        assert observed["summary_sha256"] == c.fs.compaction.summary_sha256
        assert observed["tokens_removed"] == c.fs.compaction.tokens_removed
        assert c.h.transport.backing.rows == before
    finally:
        for turn in opened:
            turn.client.close()


@pytest.mark.parametrize("change", [
    "previous_job", "previous_hash", "execution", "previous_material", "previous_identity",
])
def test_inherited_compaction_requires_the_registered_original_source(checkpoint_case, monkeypatch, change):
    c = checkpoint_case
    settle(c, c.fs.snapshot(), compaction=c.fs.compaction)
    third = open_turn(c.h, c.control, c.human, "inherited-context-negative",
                      c.assignment.task.instruction.conversation_id)
    try:
        reference = settle(third, third.client.restore())
        task = third.assignment.task.instruction
        path = f"/work-policy/v1/conversations/{task.conversation_id}/turns/{task.turn_number}/context"
        assert c.h.client.get(path, headers=c.h.headers()).status_code == 200
        current_key = key("text-archive", third.binding.job_id)
        previous_key = task.previous_archive_id
        snapshot = c.h.store.read_snapshot

        def incoherent_source(keys):
            result = dict(snapshot(keys))
            target = previous_key if change in {"previous_material", "previous_identity"} else current_key
            if target not in result or result[target] is None:
                return result
            original = result[target]
            saved = TurnArchive.model_validate_json(json.dumps(original.payload["data"]))
            if change == "previous_identity":
                changed = saved.model_copy(update={"turn_number": saved.turn_number - 1})
            elif change == "previous_material":
                result[target] = Record({**original.payload, "material": None}, original.etag)
                return result
            else:
                source = reference.source
                if change == "previous_job":
                    source = source.model_copy(update={"previous_job_id": "other-job"})
                elif change == "previous_hash":
                    source = source.model_copy(update={"previous_sha256": "f" * 64})
                else:
                    source = source.model_copy(update={
                        "execution": source.execution.model_copy(update={"summary_sha256": "f" * 64}),
                    })
                changed = saved.model_copy(update={"archive": reference.model_copy(update={"source": source})})
            result[target] = Record({
                **original.payload, "data": changed.model_dump(mode="json"),
                "material": turn_material(changed).model_dump(mode="json"),
            }, original.etag)
            return result

        before = copy.deepcopy(c.h.transport.backing.rows)
        monkeypatch.setattr(c.h.store, "read_snapshot", incoherent_source)
        denied = c.h.client.get(path, headers=c.h.headers())
        assert denied.status_code == 409 and denied.json()["error"]["code"] == "CONVERSATION_CONTEXT_CHANGED"
        assert "compaction" not in denied.json() and c.h.transport.backing.rows == before
    finally:
        third.client.close()


def test_reviewed_memory_metadata_never_returns_the_note_or_its_private_scope(assistant):
    h = assistant
    reviewed_note(h)
    h, control, human = configured_memory(h)
    turn = open_turn(h, control, human, "context-work-memory")
    try:
        catalog = turn.client.work_memory_catalog()
        settle(turn, ordinary(turn.assignment.sdk_session_id))
        before = copy.deepcopy(h.transport.backing.rows)
        task = turn.assignment.task.instruction
        response = h.client.get(
            f"/work-policy/v1/conversations/{task.conversation_id}/turns/{task.turn_number}/context",
            headers=h.headers())
        assert response.status_code == 200, response.text
        source = catalog.source
        assert response.json()["work_memory_source"] == {
            "claim_id": source.claim_id, "claim_version": source.claim_version,
            "claim_sha256": source.claim_sha256, "text_sha256": source.text_sha256,
            "text_utf8_bytes": len(NOTE.encode("utf-8")), "manifest_sha256": source.manifest_sha256,
            "usage": "historical-reference-not-current-authority",
        }
        for private in (NOTE, "TEAM_MEMORY_CANARY", h.binding.tenant_id, h.binding.agent_oid):
            assert private not in response.text
        assert h.transport.backing.rows == before
    finally:
        turn.client.close()


@pytest.mark.parametrize("profile_interface", ["fa-instruction-user-turns-v1"], indirect=True)
def test_user_source_metadata_refers_to_the_previous_exact_turn_not_its_body(profile_interface, monkeypatch):
    c = profile_interface
    original_io, reads = hosted.run_copilot, []

    async def read_then_reply(**options):
        if options["resume"]:
            tool = next(value for value in options["tools"] if value.name == "read_user_turn")
            result = await tool.handler({"arguments": {"turn_number": 1}})
            reads.append(json.loads(result.text))
        return await original_io(**options)

    monkeypatch.setattr(hosted, "run_copilot", read_then_reply)
    draft, first, first_path = saved_turn(c)
    assert c.browser.ui.get("/api/instructions/progress", params={"thread_id": draft["thread_id"]}).status_code == 200
    next_draft = post(c, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]
    follow = post(c, "/api/instructions/" + next_draft["request_id"] + "/submit",
                  {"content": "A second saved turn.", "model": next_draft["model"]})
    assert follow.status_code == 200, follow.text
    second = follow.json()["task"]
    complete(c, second["binding_id"])
    cid = first["admission"]["conversation_id"]
    rows = copy.deepcopy(c.h.transport.backing.rows)
    response = c.h.client.get(f"/work-policy/v1/conversations/{cid}/turns/2/context", headers=c.h.headers())
    assert response.status_code == 200, response.text
    original = "Read only this turn's saved metadata. 原文🙂".encode("utf-8")
    assert len(reads) == 1 and reads[0]["text"].encode("utf-8") == original
    assert response.json()["user_turn_sources"] == [{
        "turn_number": 1, "sha256": hashlib.sha256(original).hexdigest(), "utf8_bytes": len(original),
    }]
    assert c.h.client.get(first_path, headers=c.h.headers()).json()["user_turn_sources"] is None
    assert "原文" not in response.text and c.h.transport.backing.rows == rows
    assert len(c.starts) == len(c.sdk_calls) == 2


def test_missing_control_does_not_install_or_enable_a_context_source(real_interface):
    c = real_interface
    _, _, path = saved_turn(c)
    c.h.runtime.work_policy = None
    c.h.transport.requests.clear()
    response = c.h.client.get(path, headers=c.h.headers())
    assert response.status_code == 503 and c.h.transport.requests == []


def test_context_dto_and_thin_client_import_without_hosted_sdk():
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    program = r"""
import importlib.abc, sys
class NoHostedSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "copilot" or fullname.startswith("copilot."):
            raise ModuleNotFoundError("Hosted SDK forbidden in API", name=fullname)
sys.meta_path.insert(0, NoHostedSdk())
sys.path.insert(0, sys.argv[1])
from shared_context.behavior_turn_context import TurnContext, read_saved_turn_context
from shared_context.behavior_work_policy_api import install_work_policy_routes
from web_test_console.freeform import InstructionControl
assert callable(read_saved_turn_context) and callable(InstructionControl)
assert not any(name == "copilot" or name.startswith("copilot.") for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", program, str(source)],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
