"""Current-turn checkpoint publication never accepts a competing conversation turn."""

import json

import pytest

from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_sdk_checkpoint import SdkStateUpload, checkpoint_key, load_state
from shared_context.behavior_sdk_state import SaveSdkState
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import checkpoint_case, settle


def test_followup_waits_for_complete_checkpoint_and_keeps_its_original_input(checkpoint_case, monkeypatch):
    c = checkpoint_case
    cid = c.assignment.task.instruction.conversation_id
    state = c.fs.snapshot()
    command = InstructionSubmit(
        request_id="after-compaction", conversation_id=cid,
        content="  Correction: delivery=2026-10-10; count=8.\r\nKeep this exact line.",
    )
    seen = []
    original_put = c.h.artifacts.store.put

    def during_upload(*args):
        response = c.h.client.post(
            "/work-policy/v1/submit", content=command.model_dump_json(), headers=c.h.headers(),
        )
        seen.append(response.status_code)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONVERSATION_TURN_UNRESOLVED"
        view = c.h.client.get("/work-policy/v1/conversations/" + cid, headers=c.h.headers())
        assert view.status_code == 200 and len(view.json()["turns"]) == 2
        assert view.json()["can_follow_up"] is False
        return original_put(*args)

    monkeypatch.setattr(c.h.artifacts.store, "put", during_upload)
    reference = settle(c, state, compaction=c.fs.compaction)
    assert seen == [409]
    assert load_state(c.h.assistant, c.session, reference, job_id=c.binding.job_id) == state
    assert load_state(c.h.assistant, c.session, c.previous_ref, job_id=c.previous_ref.job_id) == c.previous
    accepted = c.h.client.post(
        "/work-policy/v1/submit", content=command.model_dump_json(), headers=c.h.headers(),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["turn_number"] == 3
    binding_id = accepted.json()["binding_id"]
    task = json.loads(c.h.runtime.job_input(c.h.runtime.bindings[binding_id]))["instruction"]
    assert task["content"] == command.content
    assert task["previous_archive_id"] == key("text-archive", c.binding.job_id)
    repeated = c.h.client.post(
        "/work-policy/v1/submit", content=command.model_dump_json(), headers=c.h.headers(),
    )
    assert repeated.status_code == 200 and repeated.json()["binding_id"] == binding_id
    view = c.h.client.get("/work-policy/v1/conversations/" + cid, headers=c.h.headers())
    assert len(view.json()["turns"]) == 3


def test_revocation_during_part_upload_prevents_checkpoint_publication(checkpoint_case, monkeypatch):
    c = checkpoint_case
    state = c.fs.snapshot()
    prior_key = checkpoint_key(c.previous_ref.job_id)
    original = c.h.store.read_snapshot((prior_key,))[prior_key]
    original_put = c.h.artifacts.store.put
    puts = []

    def revoke_after_put(*args):
        receipt = original_put(*args)
        puts.append(args[2])
        revoked = c.control.revoke(c.human, "revoked-before-checkpoint-publication")
        assert revoked.payload["result"]["revoked"] is True
        return receipt

    monkeypatch.setattr(c.h.artifacts.store, "put", revoke_after_put)
    command = SaveSdkState(request_id="sdk-state", state=state, sdk_stop=c.stop, compaction=c.fs.compaction)
    response = c.h.client.post(
        f"/behavior/v1/bindings/{c.binding.binding_id}/assistant/sessions/{c.assignment.session_id}/sdk-state",
        content=command.model_dump_json(),
        headers={**c.h.headers("agent"), "x-behavior-assistant-ticket": c.client.ticket.get_secret_value()},
    )
    assert response.status_code == 403
    assert response.headers["x-behavior-assistant-error-phase"] == "assistant_authority"
    upload_key = checkpoint_key(c.binding.job_id)
    uploaded = c.h.store.read_snapshot((upload_key,))[upload_key]
    pending = SdkStateUpload.model_validate_json(json.dumps(uploaded.payload["data"]))
    assert pending.reference is None
    assert c.h.store.read_snapshot((prior_key,))[prior_key] == original
    with pytest.raises(Problem):
        c.client.save_sdk_state(state, c.stop, compaction=c.fs.compaction)
    assert len(puts) == 1
