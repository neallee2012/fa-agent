"""A saved original final wakes a paused observer; no native process or model."""

import asyncio

from fastapi.testclient import TestClient
import pytest

from shared_context import behavior_assistant_wire as aw
from shared_context.sdk_pin import RUNTIME_VERSION
from test_conversation_transfer_startup import claimed_turn, saved_turn, synthetic_standing_v1
from legacy_turn_rows_helpers import read_startup
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def observer_case(request):
    loop = asyncio.new_event_loop()
    try:
        yield request.getfixturevalue("synthetic_standing_v1"), loop
    finally:
        if not loop.is_closed():
            loop.close()


def test_ready_callback_can_observe_only_the_persisted_original_result(observer_case, signing_key):
    from shared_context.behavior_assistant import key
    from shared_context.behavior_assistant_api import create_assistant_app
    from shared_context.behavior_instruction import SavedReply, TurnArchive, unpack_reply
    import json
    c, loop = observer_case
    opened = claimed_turn(c, "saved-before-wake")
    notifications = []
    def ready(binding_id):
        result = c.authority.result(opened.session.execution)
        keys = (key("reply", opened.binding.job_id), key("text-archive", opened.binding.job_id))
        rows = c.h.runtime.store.read_snapshot(keys)
        reply = SavedReply.model_validate_json(json.dumps(rows[keys[0]].payload["data"]))
        archive = TurnArchive.model_validate_json(json.dumps(rows[keys[1]].payload["data"]))
        assert binding_id == opened.binding.binding_id and result.state == "succeeded"
        assert unpack_reply(reply).text == "The original completed answer."
        assert archive.job_id == opened.binding.job_id and archive.sdk_stop.graceful
        notifications.append((binding_id, reply.sha256))
    app = create_assistant_app(c.authority, c.h.composition.verifier,
                               provider=c.h.composition.assistant.provider, on_result_ready=ready)
    from agent_runtime_routing_helpers import agent_headers
    headers = {**agent_headers(c, signing_key, 0),
               "x-behavior-assistant-ticket": opened.ticket.get_secret_value()}
    prefix = f"/behavior/v1/bindings/{opened.binding.binding_id}/assistant/sessions/{opened.assignment.session_id}"
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        early = client.post(prefix + "/execution-ready", json={"request_id": "execution-ready"}, headers=headers)
        assert early.status_code == 409 and notifications == []
        stop = aw.SdkStop(runtime_version=RUNTIME_VERSION, pid=99, exit_code=0, graceful=True)
        command = aw.SaveTextReply(request_id="save-text-reply", sdk_stop=stop,
            archive=aw.ArchiveUnavailable(error="SDK_ARCHIVE_UNAVAILABLE"),
            reply=aw.TextReply(text="The original completed answer.", sdk_session_id=opened.assignment.sdk_session_id,
                conversation_id=opened.assignment.task.instruction.conversation_id, turn_number=1))
        saved = client.post(prefix + "/save-text-reply", content=command.model_dump_json(), headers=headers)
        assert saved.status_code == 200, saved.text
        before = c.h.runtime.store._guard()
        for _ in range(2):
            response = client.post(prefix + "/execution-ready", json={"request_id": "execution-ready"}, headers=headers)
            assert response.status_code == 200, response.text
        assert len(notifications) == 2 and notifications[0] == notifications[1]
        assert c.h.runtime.store._guard() == before
    assert c.sessions.calls == []
    read_startup(c)


def test_saved_result_repairs_missing_final_after_response_loss_without_running_work_again(synthetic_standing_v1, monkeypatch):
    from dataclasses import replace
    from shared_context.behavior_assistant import key
    from shared_context.behavior_delivery import MessageRequest
    from shared_context.errors import Problem
    c = synthetic_standing_v1
    finished = saved_turn(c, "saved-result-missing-final")
    runtime = c.h.runtime
    binding = finished.binding
    delivery, workers = runtime.dep.delivery, runtime.dep.workers
    saved_keys = (key("reply", binding.job_id), key("result", binding.work.work_id),
                  key("text-archive", binding.job_id), c.control.cost_key)
    original = runtime.store.read_snapshot(saved_keys)
    delivery.prepare_message(MessageRequest(obligation_id=binding.obligation_id, delivery_id="stored-ack",
        expected_version=0, message_kind="ack"), binding.owner)
    initial = runtime.job_status(binding.binding_id, c.human)
    assert initial.delivery_id is None and not initial.obligation_fulfilled and initial.result_forwarded
    prepare = delivery.prepare_message
    prepared = []
    def lost(command, context):
        receipt = prepare(command, context)
        prepared.append(command.delivery_id)
        raise Problem(503, "DELIVERY_RESPONSE_LOST", "Injected loss after original final preparation")
    def do_not_run(*args, **kwargs):
        pytest.fail("final recovery must not rerun the worker or model")
    monkeypatch.setattr(workers, "adapter", replace(workers.adapter, start=do_not_run, query=do_not_run, stop=do_not_run))
    with monkeypatch.context() as fault:
        fault.setattr(delivery, "prepare_message", lost)
        with pytest.raises(Problem):
            runtime.controller.step(binding.binding_id)
    result = runtime.controller.step(binding.binding_id)
    assert result.state == "succeeded" and result.delivery_id == prepared[0]
    assert not result.obligation_fulfilled and len(prepared) == 1
    assert runtime.controller.step(binding.binding_id).delivery_id == result.delivery_id
    assert runtime.store.read_snapshot(saved_keys) == original
    assert c.sessions.calls == []
    read_startup(c)
