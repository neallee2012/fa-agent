"""Mid-conversation model switch behind FA_NATIVE_MODEL_SWITCH (README#native-model-switch).

Real core local task/store and host; SDK and model I/O are synthetic (OfflineSdk, or the pinned-SDK double).
"""

import dataclasses
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_instruction_models import InstructionModel
from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture, native_model_switch_enabled
from shared_context.errors import Problem
from shared_context.native_assembly import NativeAssembly
from shared_context.native_inference import NativeInference, SdkContext
from shared_context.native_turns import NativeTurnRuntime
from test_native_turn_wire import capture, submit
from test_native_turns import Harness, IDENTITY, OfflineSdk, local_env, native_retry_policy  # noqa: F401

FLAG = "FA_NATIVE_MODEL_SWITCH"


def selected(model, prompt=None, **settings):
    """The API's capture for ``model``, with its declared prompt limit as the capacity snapshot."""
    values = capture().model_dump(mode="json")
    values["template"]["settings"].update(model_capacity_profile="fa-model-capacity-v1", **settings)
    chosen = InstructionModel(id=model, display_name=model, max_prompt_tokens=prompt)
    values["model"] = chosen.model_dump(mode="json")
    values["model_capacity"] = chosen.capacity_snapshot().model_dump(mode="json")
    return NativeTurnCapture.model_validate_json(json.dumps(values))


def request(rid, cid, chosen):
    return NativeSubmit(command=submit(rid, cid).command, capture=chosen)


class MeasuredSdk(OfflineSdk):
    """OfflineSdk whose turns also report the SDK's context-window snapshot (session.usage_info), as PA-16 B saves it."""

    def __init__(self):
        super().__init__()
        self.context_tokens = None

    async def __call__(self, receipt, scope, previous, control):
        result = await super().__call__(receipt, scope, previous, control)
        if self.context_tokens is None:
            return result
        snapshot = SdkContext(event_id=None, current_tokens=self.context_tokens, token_limit=None, system_tokens=None,
                              conversation_tokens=None, tool_definitions_tokens=None, messages_length=None)
        inference = NativeInference(state="observed", logical_prepared=1, transport_started=1,
                                    sdk_context=(snapshot,), sdk_context_reason=None)
        return dataclasses.replace(result, assembly=NativeAssembly(
            state="recorded", capture_digest=receipt.capture_digest, inference=inference))


@pytest.fixture
def harness(local_env, tmp_path, native_retry_policy, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    sdk = MeasuredSdk()
    cache = str((tmp_path / "OFFLINE-cache").resolve())
    runtime = NativeTurnRuntime(IDENTITY, sdk, cache_root=Path("\\\\?\\" + cache if os.name == "nt" else cache),
                                delivery_retry=native_retry_policy)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        yield Harness(runtime, sdk, client)


def completed(h, rid, cid, chosen):
    accepted = h.invoke(request(rid, cid, chosen))
    assert accepted.status_code == 202, accepted.text
    return h.wait(rid, "completed")


def refused(h, rid, cid, chosen, code):
    answer = h.invoke(request(rid, cid, chosen))
    assert answer.status_code == 409, answer.text
    error = answer.json()["error"]
    assert error["code"] == code and error["not_dispatched"] is True
    with pytest.raises(Problem) as missing:
        h.call(h.runtime.read, rid)
    assert missing.value.body["error"]["code"] == "NATIVE_RID_MISSING"
    return error


@pytest.mark.parametrize("flag", [None, "0"])
def test_flag_off_still_refuses_a_model_change_after_a_completed_turn(harness, monkeypatch, flag):
    if flag is not None:
        monkeypatch.setenv(FLAG, flag)
    first = completed(harness, "rid-1", None, selected("OFFLINE-model-a"))
    refused(harness, "rid-2", first.conversation_id, selected("OFFLINE-model-b"), "NATIVE_CAPTURE_CHANGED")
    assert harness.call(harness.runtime.conversation, first.conversation_id).capture.model.id == "OFFLINE-model-a"
    assert len(harness.sdk.calls) == 1


def test_flag_on_admits_a_model_only_change_and_keeps_every_other_capture_rule(harness, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    original = selected("OFFLINE-model-a")
    first = completed(harness, "rid-1", None, original)
    cid = first.conversation_id
    switched = completed(harness, "rid-2", cid, selected("OFFLINE-model-b"))
    assert switched.turn_number == 2 and switched.capture.model.id == "OFFLINE-model-b"
    # The switching turn resumed the first turn's published checkpoint, not an empty session.
    assert harness.sdk.calls[1][2] is not None
    assert harness.call(harness.runtime.conversation, cid).capture.model.id == "OFFLINE-model-b"
    # Boundary 7: the earlier receipt keeps its own capture and digest.
    earlier = harness.call(harness.runtime.read, "rid-1")
    assert earlier.capture == original and earlier.capture_digest == original.sha256
    assert earlier.capture.model.id == "OFFLINE-model-a"
    # Anything else in the conversation capture still has to match.
    refused(harness, "rid-3", cid, selected("OFFLINE-model-a", user_text_file_profile=None), "NATIVE_CAPTURE_CHANGED")
    back = completed(harness, "rid-4", cid, selected("OFFLINE-model-a"))
    assert back.turn_number == 3 and harness.call(harness.runtime.read, "rid-2").capture.model.id == "OFFLINE-model-b"


def test_a_switch_on_a_turn_with_fewer_connectors_keeps_the_conversations_first_connector_set(harness, monkeypatch):
    """The first Team connector set stays the conversation's ceiling; adopting a new model must not shrink it."""
    from shared_context.behavior_capabilities import CapabilityGrant
    from shared_context.team_connector_wire import ConnectorCapture
    from shared_context.tool_catalog import ToolboxEntry

    def connected(model):
        values = selected(model).model_dump(mode="json")
        values["connectors"] = ConnectorCapture(grants=(CapabilityGrant(
            entry_id="connector-teams_read", entry_sha256="a" * 64, kind="mcp-toolbox", tools=("read_messages",),
            vision=False, toolbox=ToolboxEntry(type="toolbox", toolbox="fa-conn-offline", server_label="teams_read")),
        )).model_dump(mode="json")
        return NativeTurnCapture.model_validate_json(json.dumps(values))

    monkeypatch.setenv(FLAG, "1")
    first = completed(harness, "rid-1", None, connected("OFFLINE-model-a"))
    cid = first.conversation_id
    # The Team disabled the connector, then the owner switched model on that turn.
    assert completed(harness, "rid-2", cid, selected("OFFLINE-model-b")).turn_number == 2
    assert harness.call(harness.runtime.conversation, cid).capture.connectors == first.capture.connectors
    # Re-enabled: the connector comes back within the original set.
    assert completed(harness, "rid-3", cid, connected("OFFLINE-model-b")).turn_number == 3


def test_a_switch_the_new_model_cannot_hold_is_refused_before_dispatch(harness, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    harness.sdk.context_tokens = 300_000
    first = completed(harness, "rid-1", None, selected("OFFLINE-large", 1_000_000))
    cid = first.conversation_id
    error = refused(harness, "rid-2", cid, selected("OFFLINE-small", 272_000), "NATIVE_MODEL_CONTEXT_EXCEEDED")
    assert error["model"] == "OFFLINE-small"
    assert len(harness.sdk.calls) == 1, "nothing ran and nothing was compacted"
    assert harness.call(harness.runtime.conversation, cid).capture.model.id == "OFFLINE-large"
    # A limit above the measure, or an undeclared one (unknown is not over), is admitted.
    assert completed(harness, "rid-3", cid, selected("OFFLINE-roomy", 400_000)).turn_number == 2
    assert completed(harness, "rid-4", cid, selected("OFFLINE-undeclared")).turn_number == 3


def test_after_a_turn_that_ended_unknown_the_restored_checkpoints_measure_still_applies(harness, monkeypatch):
    """The next turn restores the last completed turn's checkpoint (meta.base_ckpt), so that turn's measure counts."""
    from shared_context.backup_models import MAX_BACKUP_BYTES

    monkeypatch.setenv(FLAG, "1")
    harness.sdk.context_tokens = 300_000
    first = completed(harness, "rid-1", None, selected("OFFLINE-large", 1_000_000))
    cid = first.conversation_id
    # A switched turn that ends unknown leaves no checkpoint; the conversation is on its model from allocation.
    harness.sdk.text = "x" * (MAX_BACKUP_BYTES + 1)
    assert harness.invoke(request("rid-2", cid, selected("OFFLINE-roomy", 400_000))).status_code == 202
    assert harness.wait("rid-2", "unknown").turn_number == 2
    assert harness.call(harness.runtime.conversation, cid).capture.model.id == "OFFLINE-roomy"
    harness.sdk.text = "Complete public answer"
    refused(harness, "rid-3", cid, selected("OFFLINE-small", 272_000), "NATIVE_MODEL_CONTEXT_EXCEEDED")
    assert len(harness.sdk.calls) == 2


def test_an_unmeasured_turn_does_not_refuse_a_switch(harness, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    first = completed(harness, "rid-1", None, selected("OFFLINE-large", 1_000_000))
    assert first.assembly is None
    assert completed(harness, "rid-2", first.conversation_id, selected("OFFLINE-small", 10)).turn_number == 2


def test_the_flag_accepts_only_0_or_1():
    assert native_model_switch_enabled({}) is False
    assert native_model_switch_enabled({FLAG: "0"}) is False
    assert native_model_switch_enabled({FLAG: "1"}) is True
    with pytest.raises(Problem) as invalid:
        native_model_switch_enabled({FLAG: "true"})
    assert invalid.value.status == 503


def test_the_hosted_sync_overlay_accepts_the_flag():
    from shared_context.behavior_agent_versions import _check_environment

    assert _check_environment({FLAG: "1"}, 900) == {FLAG: "1"}
    with pytest.raises(Problem):
        _check_environment({FLAG: "yes"}, 900)


def test_a_kept_sdk_on_the_old_model_is_discarded_and_the_checkpoint_resumes_with_the_new_model(
        local_env, monkeypatch, tmp_path, caplog):
    from test_native_resident import PinnedSdk, offline_runtime, serve, text, turn

    def renamed(model):
        values = capture().model_dump(mode="json")
        values["model"].update(id=model, display_name=model)
        return NativeTurnCapture.model_validate_json(json.dumps(values))

    caplog.set_level(logging.INFO)
    monkeypatch.setenv(FLAG, "1")
    sdk = PinnedSdk(monkeypatch)
    executor, runtime = offline_runtime(tmp_path)
    client, h = serve(runtime)
    with client:
        first = turn(h, "rid-1", selected=renamed("OFFLINE-model-a"))
        assert next(iter(executor.resident._holders.values())).live.model == "OFFLINE-model-a"
        second = turn(h, "rid-2", first.conversation_id, selected=renamed("OFFLINE-model-b"))
        assert text(h, second) == "OFFLINE answer 2"
        assert next(iter(executor.resident._holders.values())).live.model == "OFFLINE-model-b"
    messages = [record.getMessage() for record in caplog.records]
    assert "RESIDENT_SDK_DISCARDED reason=model_changed" in messages
    assert not any(message.startswith("RESIDENT_SDK_RESTARTED") for message in messages)
    # Cold: a new client resumes the published checkpoint (fresh files, not the old SDK's live files).
    assert len(sdk.clients) == 2 and sdk.stopped[0] is sdk.clients[0]
    assert sdk.sends[1].resume and sdk.sends[1].fs is not sdk.sends[0].fs
    sdk.clients[1].create_session.assert_not_called()
    assert sdk.clients[1].resume_session.call_args.kwargs["model"] == "OFFLINE-model-b"
    assert sdk.sends[1].options["provider"]["model_id"] == "OFFLINE-model-b"
    assert sdk.sends[0].options["model"] == "OFFLINE-model-a"
    # The experimental in-session switch is never used.
    for record in sdk.sends:
        record.session.set_model.assert_not_called()


def _compaction(kind, trigger, number=1):
    data = {"trigger": trigger}
    if kind == "session.compaction_complete":
        data.update(success=True, summaryContent="OFFLINE summary", checkpointNumber=number)
    return {"type": kind, "id": f"{kind}-{trigger}-{number}", "timestamp": "2026-10-13T01:00:00.000Z",
            "parentId": None, "data": data}


def test_the_auto_profile_never_accepts_a_model_switch_compaction():
    """CTX-03: model-switch compaction is not enabled, while the auto profile takes the runtime's own threshold one."""
    from shared_context.behavior_sdk_compaction import automatic_pairs
    from shared_context.behavior_sdk_files import SdkSessionFiles
    from shared_context.behavior_sdk_state import AUTO_PROFILE, CheckpointProfile, local_platform

    pair = [_compaction("session.compaction_start", "threshold"), _compaction("session.compaction_complete", "threshold")]
    assert automatic_pairs(pair) == pair
    with pytest.raises(Problem) as switched:
        automatic_pairs([_compaction("session.compaction_start", "model_switch"),
                         _compaction("session.compaction_complete", "model_switch")])
    assert switched.value.body["error"]["code"] == "SDK_STATE_SOURCE_UNVERIFIED"

    def observed(trigger):
        files = SdkSessionFiles("OFFLINE-sid", None, checkpoint_profile=CheckpointProfile(
            name=AUTO_PROFILE, platform=local_platform(), provider="openai-responses"), model_provider="openai-responses")
        files.observe_compaction(_compaction("session.compaction_start", trigger))
        return files.checkpoint_error

    assert observed("threshold") is None
    assert observed("model_switch") == "SDK_STATE_SOURCE_UNVERIFIED"

