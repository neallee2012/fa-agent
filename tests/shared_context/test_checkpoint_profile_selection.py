"""Existing human selection captures checkpoint/start presets without changing old work."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from shared_context.behavior_assistant_wire import ModelRequest
from shared_context.behavior_profile_selection import SelectConversationProfile, validate_profile_history
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.behavior_sdk_state import CheckpointProfile
from shared_context.behavior_work_policy import WorkPolicyControl
from shared_context.source_materials import digest
from test_behavior_assistant import assistant
from test_conversation_profile_selection import PATH, configured, select, template
from test_instruction_flow import check_history, instruction_inventory, instruction_models, runtime_path, sdk_io
from test_instruction_prompt import submit
from test_m3_auth import signing_key, jwk_client
from test_sdk_compaction_product import checkpoint_profile, event, observation, open_turn, ordinary, raw_files, settle


def reconstruct(control):
    from shared_context.behavior_inbox import PullInbox
    inbox = PullInbox(control.store, control.runtime.dep.inbox.registration)
    inbox.attach(control.runtime.dep.delivery)
    control.runtime.dep = replace(control.runtime.dep, inbox=inbox)
    rebuilt = WorkPolicyControl(control.runtime, control.policy, control.cost_reader)
    rebuilt.base_bindings, rebuilt.base_profiles = control.base_bindings, control.base_profiles
    rebuilt.attach()
    return rebuilt


def selection_history(h, control):
    config = SimpleNamespace(table=SimpleNamespace(scope=h.store._scope), sha256="f" * 64,
        workers=h.workers.contract, delivery=h.delivery.contract,
        identity=SimpleNamespace(human_oid=h.binding.human_oid))
    validate_profile_history(instruction_inventory(h), config, control.policy)
    check_history(h)


def test_selected_checkpoint_profile_survives_disable_and_restart(assistant):
    h, control, human = configured(assistant)
    original_policy = control.policy.model_dump_json()
    opened = []
    try:
        old = open_turn(h, control, human, "before-checkpoint-selection")
        opened.append(old)
        old_ref = settle(old, ordinary(old.assignment.sdk_session_id))
        old_record = control.records[old.binding.binding_id].model_dump_json()
        profile = checkpoint_profile()
        selected, _ = select(h, request_id="enable-checkpoint", value=template(checkpoint_profile=profile))
        assert selected.status_code == 200, selected.text
        selection_id = selected.json()["result"]["selection_id"]
        first = open_turn(h, control, human, "selected-checkpoint-first")
        opened.append(first)
        record = control.records[first.binding.binding_id]
        assert record.profile_selection_id == selection_id
        assert record.profile.checkpoint_profile == first.assignment.task.instruction.checkpoint_profile == profile
        previous = ordinary(first.assignment.sdk_session_id)
        settle(first, previous)
        cid = first.assignment.task.instruction.conversation_id
        second = open_turn(h, control, human, "selected-checkpoint-second", cid)
        opened.append(second)
        assert second.client.restore() == previous
        body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "OFFLINE compact"}], "stream": True}
        second.compaction_request_sha256 = digest(body)
        second.client.reserve_model(second.compaction_request_sha256)
        principal = h.verifier(h.client.build_request("GET", "/", headers=h.headers("agent")))
        h.assistant.consume_model(second.session, ModelRequest(request_id=second.compaction_request_sha256,
            body=body, checkpoint_compaction=True), principal)
        fs = SdkSessionFiles(previous.sdk_session_id, previous, checkpoint_profile=profile)
        fs.files = raw_files(previous)
        fs.compaction = observation(second, previous)
        checkpoint = fs.snapshot()
        reference = settle(second, checkpoint, compaction=fs.compaction)
        assert h.client.get("/work-policy/v1/conversations/" + cid, headers=h.headers()).json()["can_follow_up"]
        saved_records = {key: value.model_dump_json() for key, value in control.records.items()}
        disabled, _ = select(h, request_id="new-conversations-without-checkpoint", previous=selection_id,
                             value=template())
        assert disabled.status_code == 200, disabled.text
        control = reconstruct(control)
        assert control.policy.model_dump_json() == original_policy
        assert all(control.records[key].model_dump_json() == value for key, value in saved_records.items())
        assert control.records[old.binding.binding_id].model_dump_json() == old_record
        assert h.client.get("/work-policy/v1/conversations/" + cid, headers=h.headers()).json()["can_follow_up"]
        third = open_turn(h, control, human, "selected-checkpoint-inherited", cid)
        opened.append(third)
        assert control.records[third.binding.binding_id].profile_selection_id == selection_id
        assert third.assignment.task.instruction.checkpoint_profile == profile
        assert third.client.restore() == checkpoint
        continued = SdkSessionFiles(checkpoint.sdk_session_id, checkpoint, checkpoint_profile=profile)
        continued.files["events.jsonl"] += json.dumps(event(
            "user.message", "selected-profile-inherited", {"content": "Continue."}, "compact-complete")) + "\n"
        inherited = settle(third, continued.snapshot())
        assert inherited.source.execution == reference.source.execution
        assert continued.compaction is None
        unselected = open_turn(h, control, human, "checkpoint-disabled-new")
        opened.append(unselected)
        assert unselected.assignment.task.instruction.checkpoint_profile is None
        settle(unselected, ordinary(unselected.assignment.sdk_session_id))
        old_next = open_turn(h, control, human, "old-checkpoint-followup", old.assignment.task.instruction.conversation_id)
        opened.append(old_next)
        assert old_next.assignment.task.instruction.checkpoint_profile is None
        assert old_next.client.restore().sha256 == old_ref.sha256
        selection_history(h, control)
    finally:
        for turn in opened:
            turn.client.close()


def test_selected_sdk_start_v1_uses_original_gate_and_survives_new_default(assistant, monkeypatch):
    from shared_context.behavior_assistant import _Session, key
    from shared_context.behavior_hosted import LocalJournal
    from shared_context.behavior_operations import _row
    from shared_context.behavior_wire import SdkStartRecord
    h, control, _ = configured(assistant)
    sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Selected gate first."}, {"content": "Selected gate follow-up."},
                              {"content": "New conversation without gate."}])
    selected, _ = select(h, request_id="select-start-gate", value=template(sdk_start_profile="fa-sdk-start-gate-v1"))
    assert selected.status_code == 200, selected.text
    selection_id = selected.json()["result"]["selection_id"]
    first, _ = submit(h, "selected-gate-first", "Start with this gate.")
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    disabled, _ = select(h, request_id="deselect-start-gate", previous=selection_id, value=template())
    assert disabled.status_code == 200, disabled.text
    control = reconstruct(control)
    second, _ = submit(h, "selected-gate-followup", "Keep the original gate.", first["conversation_id"])
    assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
    for value in (first, second):
        record = control.records[value["binding_id"]]
        assert record.profile.sdk_start_profile == "fa-sdk-start-gate-v1"
        job = h.workers.inspect(record.binding.job_id)
        sid = key("session-id", job.binding.start_request_id)
        session = _row(h.store.read_snapshot((key("session", sid),)), key("session", sid), _Session)
        assert session.assignment.sdk_start_binding.profile == "fa-sdk-start-gate-v1"
        with LocalJournal(h.executor.root / sid).connect() as db:
            body, = db.execute("SELECT body FROM assistant_sdk_start_gates WHERE session_id=?", (sid,)).fetchone()
        saved = SdkStartRecord.model_validate_json(body)
        assert saved.state == "entry_attempted" and saved.binding == session.assignment.sdk_start_binding
    third, _ = submit(h, "unselected-gate-new", "No newly selected gate.")
    assert control.records[third["binding_id"]].profile.sdk_start_profile is None
    assert h.runtime.controller.step(third["binding_id"]).state == "succeeded"
    selection_history(h, control)


@pytest.mark.parametrize("settings", [
    {"checkpoint_profile": CheckpointProfile(platform="linux-x64")},
    {"sdk_start_profile": "fa-sdk-start-gate-v1"},
])
@pytest.mark.parametrize("identity", ["agent", "read-only"])
def test_nonapprovers_cannot_select_checkpoint_or_start_presets(assistant, settings, identity):
    h, control, _ = configured(assistant)
    before = h.store._guard()
    response, _ = select(h, value=template(**settings), identity=identity)
    assert response.status_code == 403
    assert h.store._guard() == before and not control.records


def test_start_v2_is_not_enabled_without_its_distinct_memory_profile(assistant):
    h, control, _ = configured(assistant)
    before = h.store._guard()
    response, _ = select(h, value=template(sdk_start_profile="fa-sdk-start-gate-v2"))
    assert response.status_code == 422
    assert h.store._guard() == before and not control.records


@pytest.mark.parametrize("setting,value", [
    ("checkpoint_profile", {"name": "manual-no-custom-v1", "platform": "linux-arm64", "provider": "openai-completions"}),
    ("checkpoint_profile", {"name": "future-format", "platform": "linux-x64", "provider": "openai-completions"}),
    ("checkpoint_profile", {"name": "manual-no-custom-v1", "platform": "linux-x64", "provider": "unapproved-provider"}),
    ("checkpoint_profile", {"name": "manual-no-custom-v1", "platform": "linux-x64", "provider": "openai-completions", "safe": True}),
    ("sdk_start_profile", "fa-sdk-start-gate-future"),
])
def test_selection_accepts_only_the_fixed_typed_presets(assistant, setting, value):
    h, control, _ = configured(assistant)
    command = SelectConversationProfile(request_id="unsupported-preset", expected_selection_id=None, template=template())
    packet = command.model_dump(mode="json")
    packet["template"]["settings"][setting] = value
    before = h.store._guard()
    response = h.client.post(PATH + "/select", json=packet, headers=h.headers())
    assert response.status_code == 422
    assert h.store._guard() == before and not control.records
