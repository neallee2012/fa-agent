"""Original copy outcomes survive cancellation; signed API, synthetic SDK archive/stop."""

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import A, B
from shared_context.behavior_assistant import key
from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn, ordinary, settle
from test_sop_skill_copy import approved_sop, change_source, copy_scope, configured_copy, post_copy, BODY, LEGACY_WORK


def record_case(name, values):
    if os.environ.get("FA_COPY_RESULT_EVIDENCE_DIR"):
        target = Path(os.environ["FA_COPY_RESULT_EVIDENCE_DIR"])
        target.mkdir(parents=True, exist_ok=True)
        (target / (name + ".json")).write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")


def post_failed(turn, archive, failure="cancelled"):
    return turn.h.client.post(
        f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant/sessions/{turn.assignment.session_id}/save-text-reply",
        headers={**turn.h.headers("agent"), "x-behavior-assistant-ticket": turn.client.ticket.get_secret_value()},
        content=aw.SaveTextReply(request_id="save-text-reply", reply=None, archive=archive,
                                sdk_stop=turn.stop, failure=failure).model_dump_json())


def cancel(turn):
    response = turn.h.client.post("/work-policy/v1/bindings/" + turn.binding.binding_id + "/cancel",
        headers=turn.h.headers(), json={"request_id": "cancel-original-copy-work"})
    assert response.status_code == 200, response.text


def change_target(http, agent, human, receipt, change):
    path = "/teams/" + A.team + "/artifacts/" + receipt["artifact_id"]
    snapshot = http.get(path, headers=agent)
    assert snapshot.status_code == 200
    if change in {"approve", "reject"}:
        response = http.post(path + "/decisions", headers={**human, "If-Match": snapshot.headers["etag"]},
            json={"decision_id": "b" * 32, "version_id": receipt["version_id"], "decision": change,
                  "sha256": receipt["sha256"], "metadata_sha256": receipt["metadata_sha256"]})
    else:
        import hashlib
        later = b"Another version submitted after the original confirmed copy."
        response = http.put(path + "/versions/" + "e" * 32, content=later, headers={
            **agent, "If-Match": snapshot.headers["etag"], "Content-Type": "application/octet-stream",
            "X-Artifact-Filename": "later.md", "X-Artifact-Media-Type": "text/markdown",
            "X-Artifact-SHA256": hashlib.sha256(later).hexdigest()})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("copied", [False, True], ids=["no-copy", "known-copy"])
def test_signed_cancel_preserves_original_copy_ledger_and_failure_delivery(
        assistant, signing_key, jwk_client, copied):
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "cancel-copy-result")
    try:
        receipt = None
        if copied:
            created = post_copy(turn)
            assert created.status_code == 200
            receipt = created.json()
        archive = turn.client.save_sdk_state(ordinary(turn.assignment.sdk_session_id), turn.stop)
        ekey = key("artifact", turn.binding.work.work_id)
        original = h.store.read_snapshot((ekey,))[ekey]
        cancel(turn)
        with patch.object(h.artifacts, "create", wraps=h.artifacts.create) as creates, \
                patch.object(h.artifacts, "upload", wraps=h.artifacts.upload) as uploads, \
                patch.object(h.artifacts, "reconcile", wraps=h.artifacts.reconcile) as reconciles:
            saved = post_failed(turn, archive)
        record_case("cancel-" + ("known-copy" if copied else "no-copy"), {
            "runtime_mode": h.runtime.mode,
            "scope": "Real signed cancel/save API and original ledger; SDK state/stop are explicit test substitutes",
            "copy_was_saved": copied, "failure_save_http": saved.status_code,
            "failure_code": saved.headers.get("x-behavior-assistant-error-code"),
            "create_calls_during_failure": creates.call_count, "upload_calls_during_failure": uploads.call_count,
            "reconcile_calls_during_failure": reconciles.call_count,
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["work_outcome"] == "cancelled"
        assert creates.call_count == uploads.call_count == reconciles.call_count == 0
        assert h.store.read_snapshot((ekey,))[ekey] == original
        result = h.assistant.result(turn.start.binding)
        assert result.state == "cancelled"
        assert result.known_effects == (() if receipt is None else (receipt["receipt_id"],))
        assert result.unresolved_operations == ()
        h.executor._complete(turn.start, saved.json())
        done = h.runtime.controller.step(turn.binding.binding_id)
        assert done.state == "cancelled" and done.result_forwarded
    finally:
        turn.client.close()


@pytest.mark.parametrize("change", ["approve", "reject", "new-version"])
def test_later_target_decision_or_version_keeps_the_original_historical_copy_receipt(
        assistant, signing_key, jwk_client, change):
    h = assistant
    http, agent, human_headers, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-target-history")
    try:
        first = post_copy(turn)
        assert first.status_code == 200
        receipt = first.json()
        change_target(http, agent, human_headers, receipt, change)
        ekey = key("artifact", turn.binding.work.work_id)
        original = h.store.read_snapshot((ekey,))[ekey]
        target = h.artifacts.get(A, A.team, receipt["artifact_id"])
        guard = h.store._guard()
        with patch.object(h.artifacts, "create", side_effect=AssertionError("No new create")), \
                patch.object(h.artifacts, "upload", side_effect=AssertionError("No new upload")), \
                patch.object(h.artifacts, "reconcile", side_effect=AssertionError("No new reconcile")):
            observed = post_copy(turn)
        record_case("target-" + change, {
            "runtime_mode": h.runtime.mode, "mutation": change,
            "scope": "Signed human decision/new version after the original confirmed copy",
            "receipt_read_http": observed.status_code,
            "receipt_error": observed.headers.get("x-behavior-assistant-error-code"),
            "later_target_state_preserved": h.artifacts.get(A, A.team, receipt["artifact_id"]) == target,
        })
        assert observed.status_code == 200, observed.text
        assert observed.json() == receipt and h.store._guard() == guard
        assert receipt["state_scope"] == "at-original-copy-write"
        assert receipt["approved"] is False and receipt["effective_version_id"] is None
        assert h.store.read_snapshot((ekey,))[ekey] == original
        archive = turn.client.save_sdk_state(ordinary(turn.assignment.sdk_session_id), turn.stop)
        saved = turn.client.save_text_reply("The receipt describes the original copy, not current approval.", archive, turn.stop)
        h.executor._complete(turn.start, saved.model_dump(mode="json"))
        assert h.runtime.controller.step(turn.binding.binding_id).state == "succeeded"
    finally:
        turn.client.close()


@pytest.mark.parametrize("failure", ["sdk_failed", "cancelled"])
def test_lost_upload_then_changed_source_can_save_original_unresolved_copy_failure(
        assistant, signing_key, jwk_client, monkeypatch, failure):
    h = assistant
    http, agent, human_headers, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "failed-unknown-copy")
    try:
        archive = turn.client.save_sdk_state(ordinary(turn.assignment.sdk_session_id), turn.stop)
        original_upload = h.artifacts.upload
        writes = []

        def lost(*args, **kwargs):
            writes.append(True)
            original_upload(*args, **kwargs)
            raise TimeoutError("SYNTHETIC_UPLOAD_ACK_LOST")

        monkeypatch.setattr(h.artifacts, "upload", lost)
        assert post_copy(turn).status_code == 503 and writes == [True]
        monkeypatch.setattr(h.artifacts, "upload", original_upload)
        change_source(http, agent, human_headers)
        ekey = key("artifact", turn.binding.work.work_id)
        before = h.store.read_snapshot((ekey,))[ekey]
        assert before.payload["data"]["stage"] == "upload_unknown"
        if failure == "cancelled":
            cancel(turn)
        with patch.object(h.artifacts, "create", side_effect=AssertionError("Saving failure cannot create")), \
                patch.object(h.artifacts, "upload", side_effect=AssertionError("Saving failure cannot upload")), \
                patch.object(h.artifacts, "reconcile", side_effect=AssertionError("Saving failure cannot reconcile")):
            response = post_failed(turn, archive, failure)
        record_case("upload-ack-source-change-" + failure, {
            "runtime_mode": h.runtime.mode, "scope": "Actual M3 upload then injected ACK loss; signed source version change",
            "status": response.status_code, "code": response.headers.get("x-behavior-assistant-error-code"),
            "original_stage": before.payload["data"]["stage"], "upload_calls": len(writes),
            "failure": failure, "no_m3_mutation_on_save": True,
        })
        assert response.status_code == 200, response.text
        assert h.store.read_snapshot((ekey,))[ekey] == before
        result = h.assistant.result(turn.start.binding)
        assert result.state == ("cancelled" if failure == "cancelled" else "failed")
        assert result.known_effects == (ekey,) and result.unresolved_operations == (ekey,)
        assert not result.passed_checks
        archived = h.store.read_snapshot((key("text-archive", turn.binding.job_id),))[key("text-archive", turn.binding.job_id)]
        assert archived.payload["data"]["sop_copy_outcome"]["stage"] == "upload_unknown"
        assert "sop_copy_receipt" not in archived.payload["data"]
        h.executor._complete(turn.start, response.json())
        done = h.runtime.controller.step(turn.binding.binding_id)
        assert done.state == result.state and done.result_forwarded
        view = h.client.get("/work-policy/v1/conversations/" + turn.assignment.task.instruction.conversation_id,
                            headers=h.headers())
        assert view.status_code == 200 and not view.json()["can_follow_up"]
        assert view.json()["follow_up_blocker"] == "SOP_COPY_OUTCOME_UNKNOWN"
        from web_test_console.freeform import ConversationProjection
        parsed = ConversationProjection.model_validate_json(view.content)
        assert parsed.turns[0].sop_copy_outcome.stage == "upload_unknown"
        invalid = view.json()
        invalid["turns"][0]["sop_copy_outcome"]["stage"] = "complete"
        with pytest.raises(ValueError):
            ConversationProjection.model_validate_json(json.dumps(invalid))
        follow = h.client.post("/work-policy/v1/submit", headers=h.headers(), json={
            "request_id": "not-a-copy-retry", "content": "Continue original work.",
            "conversation_id": turn.assignment.task.instruction.conversation_id})
        assert follow.status_code == 409 and follow.json()["error"]["code"] == "SOP_COPY_OUTCOME_UNKNOWN"
        assert follow.json()["error"]["automatic_retry"] is False
        assert follow.json()["error"]["reconcile_original"] is False
    finally:
        turn.client.close()


@pytest.mark.parametrize("change", ["source-version", "source-owner", "m3-access", "overall-read-revoked"])
def test_failure_save_uses_original_ledger_not_new_copy_permission(assistant, signing_key, jwk_client, monkeypatch, change):
    h = assistant
    http, agent, human_headers, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "historical-copy-failure")
    try:
        first = post_copy(turn)
        assert first.status_code == 200
        receipt = first.json()
        archive = turn.client.save_sdk_state(ordinary(turn.assignment.sdk_session_id), turn.stop)
        ekey = key("artifact", turn.binding.work.work_id)
        original = h.store.read_snapshot((ekey,))[ekey]
        if change == "source-version":
            change_source(http, agent, human_headers)
        elif change == "source-owner":
            path = "/teams/" + A.team + "/works/" + LEGACY_WORK
            original_work = http.get(path, headers=agent)
            handed = http.post(path + "/handoff", headers={**agent, "If-Match": original_work.headers["etag"]},
                               json={"to_actor": B.actor})
            assert handed.status_code == 200, handed.text
        elif change == "m3-access":
            monkeypatch.setattr(h.artifacts.context, "auth", replace(h.artifacts.context.auth, actors={B.oid: B}))
        else:
            revoked = h.client.post("/operator/v1/standing-work-policy/revoke", headers=h.headers(),
                json={"decision_id": "revoke-original-policy"})
            assert revoked.status_code == 200, revoked.text
        with patch.object(h.artifacts, "create", side_effect=AssertionError("No M3 create for saving failure")), \
                patch.object(h.artifacts, "upload", side_effect=AssertionError("No M3 upload for saving failure")), \
                patch.object(h.artifacts, "reconcile", side_effect=AssertionError("No M3 reconcile for saving failure")), \
                patch.object(h.artifacts, "get", side_effect=AssertionError("Failure consumes saved ledger, not current M3")):
            response = post_failed(turn, archive, "sdk_failed")
        assert response.status_code == (403 if change == "overall-read-revoked" else 200), response.text
        assert h.store.read_snapshot((ekey,))[ekey] == original
        if change != "overall-read-revoked":
            result = h.assistant.result(turn.start.binding)
            assert result.state == "failed" and result.known_effects == (receipt["receipt_id"],)
            assert not result.unresolved_operations
            h.executor._complete(turn.start, response.json())
            done = h.runtime.controller.step(turn.binding.binding_id)
            assert done.state == "failed" and done.result_forwarded
        else:
            assert h.store.read_snapshot((key("result", turn.binding.work.work_id),))[key("result", turn.binding.work.work_id)] is None
        record_case("failure-read-boundary-" + change, {
            "runtime_mode": h.runtime.mode, "mutation": change,
            "failure_save_http": response.status_code,
            "copy_effect_retained_unchanged": True,
            "m3_access_change": "test config allowlist removal" if change == "m3-access" else None,
            "new_m3_actions_on_save": 0,
        })
    finally:
        turn.client.close()


@pytest.mark.parametrize("changed", ["source-version", "target-approved", "target-rejected", "target-new-version"])
def test_confirmed_effect_survives_model_gates_and_never_replays_copy(assistant, signing_key, jwk_client, changed):
    from sdk_checkpoint_local import sse
    h = assistant
    http, agent, human_headers, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    requests = []
    def provider(request):
        requests.append(True)
        return sse(request, {"content": "The historical copy is recorded; current approval is not asserted."}, 1)
    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_HISTORICAL_COPY")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    turn = open_turn(h, control, human, "historical-copy-model")
    try:
        first = post_copy(turn)
        assert first.status_code == 200
        receipt = first.json()
        if changed == "source-version":
            change_source(http, agent, human_headers)
        else:
            change_target(http, agent, human_headers, receipt, {
                "target-approved": "approve", "target-rejected": "reject", "target-new-version": "new-version"}[changed])
        body = {"model": "gpt-4.1-mini", "stream": True, "messages": [
            {"role": "user", "content": "Report the original operation only."},
            {"role": "tool", "tool_call_id": "original-copy", "content": first.text}]}
        request_sha = digest(body)
        with patch.object(h.artifacts, "create", side_effect=AssertionError("No second copy")), \
                patch.object(h.artifacts, "upload", side_effect=AssertionError("No second upload")):
            assert turn.client.copy_sop_draft().model_dump(mode="json") == receipt
            turn.client.reserve_model(request_sha)
            response = turn.client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
        assert response.status_code == 200 and requests == [True]
        ikey = key("model-intent", turn.assignment.session_id, request_sha)
        retained = h.store.read_snapshot((ikey,))[ikey]
        assert retained.payload["data"]["consumed"] is True
        with pytest.raises(Problem) as replay:
            turn.client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
        assert replay.value.body["error"]["upstream_error_code"] == "ASSISTANT_MODEL_UNKNOWN"
        assert requests == [True] and h.store.read_snapshot((ikey,))[ikey] == retained
        reference = turn.client.save_sdk_state(ordinary(turn.assignment.sdk_session_id), turn.stop)
        saved = turn.client.save_text_reply("Historical effect retained; no approval claim.", reference, turn.stop)
        h.executor._complete(turn.start, saved.model_dump(mode="json"))
        assert h.runtime.controller.step(turn.binding.binding_id).state == "succeeded"
        record_case("historical-model-" + changed, {
            "runtime_mode": h.runtime.mode, "mutation": changed, "offline_provider_requests": len(requests),
            "model_replay_denied": True, "receipt_state_scope": receipt["state_scope"],
            "source_body_in_model": False, "current_approval_not_reported": True,
        })
    finally:
        turn.client.close()


@pytest.mark.parametrize("failure", ["completed-ack", "pending-blob-ack"])
def test_original_upload_reconciles_after_source_changes_without_second_put(
        assistant, signing_key, jwk_client, monkeypatch, failure):
    h = assistant
    http, agent, human_headers, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-original-upload-readback")
    upload = h.artifacts.upload
    put = h.artifacts.store.put
    counts = {"upload": 0, "put": 0}
    def uploading(*args, **kwargs):
        target = args[2] == turn.assignment.task.artifact_id
        if target:
            counts["upload"] += 1
        result = upload(*args, **kwargs)
        if target and failure == "completed-ack":
            raise TimeoutError("SYNTHETIC_COMPLETED_UPLOAD_ACK_LOST")
        return result
    def putting(*args, **kwargs):
        target = args[1] == turn.assignment.task.artifact_id
        if target:
            counts["put"] += 1
        result = put(*args, **kwargs)
        if target and failure == "pending-blob-ack":
            raise TimeoutError("SYNTHETIC_BLOB_ACK_LOST")
        return result
    monkeypatch.setattr(h.artifacts, "upload", uploading)
    monkeypatch.setattr(h.artifacts.store, "put", putting)
    try:
        assert post_copy(turn).status_code == 503
        change_source(http, agent, human_headers)
        before = dict(counts)
        assert before == {"upload": 1, "put": 1}
        recovered = post_copy(turn)
        assert recovered.status_code == 200, recovered.text
        assert counts == before
        assert recovered.json()["state_scope"] == "at-original-copy-write"
        ekey = key("artifact", turn.binding.work.work_id)
        ledger = h.store.read_snapshot((ekey,))[ekey]
        assert ledger.payload["data"]["stage"] == "saved"
        assert post_copy(turn).json() == recovered.json() and counts == before
    finally:
        turn.client.close()


def test_unfinalizable_original_blob_stays_unresolved_but_failure_can_be_saved(
        assistant, signing_key, jwk_client, monkeypatch):
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-no-m3-finalize-authority")
    put = h.artifacts.store.put
    writes = []
    def lost(*args, **kwargs):
        writes.append(True)
        put(*args, **kwargs)
        raise TimeoutError("SYNTHETIC_BLOB_ACK_LOST")
    try:
        archive = turn.client.save_sdk_state(ordinary(turn.assignment.sdk_session_id), turn.stop)
        monkeypatch.setattr(h.artifacts.store, "put", lost)
        assert post_copy(turn).status_code == 503 and writes == [True]
        monkeypatch.setattr(h.artifacts.context, "auth", replace(h.artifacts.context.auth, actors={B.oid: B}))
        assert post_copy(turn).status_code == 403 and writes == [True]
        ekey = key("artifact", turn.binding.work.work_id)
        original = h.store.read_snapshot((ekey,))[ekey]
        assert original.payload["data"]["stage"] == "upload_unknown"
        response = post_failed(turn, archive, "sdk_failed")
        assert response.status_code == 200, response.text
        result = h.assistant.result(turn.start.binding)
        assert result.state == "failed" and result.unresolved_operations == (ekey,)
        assert h.store.read_snapshot((ekey,))[ekey] == original and writes == [True]
    finally:
        turn.client.close()


def test_separate_admissions_have_distinct_slots_not_one_installation_lifetime_slot(
        assistant, signing_key, jwk_client):
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    receipts, targets = [], []
    for number in range(2):
        turn = open_turn(h, control, human, "separate-copy-work-" + str(number))
        try:
            reply = post_copy(turn)
            assert reply.status_code == 200
            receipt = reply.json()
            receipts.append(receipt)
            targets.append(h.artifacts.get(A, A.team, receipt["artifact_id"]))
            assert post_copy(turn).json() == receipt
            settle(turn, ordinary(turn.assignment.sdk_session_id))
        finally:
            turn.client.close()
    assert receipts[0]["job_id"] != receipts[1]["job_id"] and receipts[0]["artifact_id"] != receipts[1]["artifact_id"]
    assert all(h.artifacts.get(A, A.team, value["artifact_id"]) == target for value, target in zip(receipts, targets))


@pytest.mark.parametrize("stage", ["create_unknown", "created"])
def test_unfinished_metadata_stage_is_preserved_in_failed_result(assistant, signing_key, jwk_client, monkeypatch, stage):
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-failed-metadata-stage")
    command = h.assistant._command

    def lose_ack(session, name, *args, **kwargs):
        response = command(session, name, *args, **kwargs)
        if name == ("sop-copy-intent" if stage == "create_unknown" else "sop-copy-created"):
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "Synthetic original ledger ACK loss.")
        return response

    try:
        archive = turn.client.save_sdk_state(ordinary(turn.assignment.sdk_session_id), turn.stop)
        monkeypatch.setattr(h.assistant, "_command", lose_ack)
        assert post_copy(turn).status_code == 503
        ekey = key("artifact", turn.binding.work.work_id)
        original = h.store.read_snapshot((ekey,))[ekey]
        assert original.payload["data"]["stage"] == stage
        with pytest.raises(Problem):
            turn.client.save_text_reply("This cannot be reported as a completed copy.", archive, turn.stop)
        with patch.object(h.artifacts, "create", side_effect=AssertionError("No resend")), \
                patch.object(h.artifacts, "upload", side_effect=AssertionError("No resend")), \
                patch.object(h.artifacts, "reconcile", side_effect=AssertionError("No implicit reconciliation")):
            saved = post_failed(turn, archive, "sdk_failed")
        assert saved.status_code == 200, saved.text
        result = h.assistant.result(turn.start.binding)
        assert result.known_effects == (() if stage == "create_unknown" else (ekey,))
        assert result.unresolved_operations == (ekey,) and result.state == "failed"
        assert h.store.read_snapshot((ekey,))[ekey] == original
    finally:
        turn.client.close()


def test_historical_receipt_still_rejects_corrupted_original_blob(assistant, signing_key, jwk_client, monkeypatch):
    h = assistant
    http, agent, human_headers, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-immutable-readback")
    try:
        copied = post_copy(turn)
        assert copied.status_code == 200
        receipt = copied.json()
        change_target(http, agent, human_headers, receipt, "approve")
        read = h.artifacts.store.read
        def corrupt(team, artifact_id, version_id, version):
            return b"SYNTHETIC_CORRUPTED_COPY" if artifact_id == receipt["artifact_id"] else read(team, artifact_id, version_id, version)
        monkeypatch.setattr(h.artifacts.store, "read", corrupt)
        guard = h.store._guard()
        response = post_copy(turn)
        assert response.status_code == 502 and h.store._guard() == guard
        assert "SYNTHETIC_CORRUPTED_COPY" not in response.text
    finally:
        turn.client.close()
