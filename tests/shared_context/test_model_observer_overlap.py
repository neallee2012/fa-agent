"""A timer observation must not disrupt the third model call of an active instruction."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import get_ident

from fastapi.testclient import TestClient
import httpx
import pytest

from shared_context import behavior_assistant
from shared_context import behavior_assistant_api as api
from shared_context import behavior_assistant_wire as aw
from shared_context import behavior_wire as wire
from shared_context.behavior_history_prompt import HISTORY_PROFILE
from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
from shared_context.behavior_workers import worker_key
from shared_context.source_materials import digest
from test_conversation_profile_selection import template
from test_conversation_transfer_startup import claimed_turn, synthetic_standing_v1
from test_instruction_observer_resume import observer_case
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token


def report_unknown(c, opened, binding):
    probe = c.native.probe(binding)
    return c.native.report(opened.binding.binding_id, opened.launch.launch_id, probe.probe_id,
        wire.HostedReport(**opened.claim.model_dump(), probe_id=probe.probe_id,
            process_generation=opened.launch.process_generation,
            binding_digest=digest(binding.model_dump(mode="json")),
            supervisor_id=opened.claimed.assignment.process_id, sequence=1, phase="spawn_unknown"),
        opened.agent).evidence_id


def test_third_model_call_survives_timer_tick_during_its_authority_read(observer_case, signing_key, monkeypatch):
    c, loop = observer_case
    clock = [0.0]
    monkeypatch.setattr(behavior_assistant, "monotonic", lambda: clock[0], raising=False)
    monkeypatch.setenv("FA_DELIVERY_PENDING_INDEX_WRITER", "true")
    monkeypatch.setenv("FA_WORK_REGISTRY_WRITER", "true")
    select_profile(c.control, c.authority, c.human, SelectConversationProfile(
        request_id="history-read-profile", expected_selection_id=None,
        template=template(HISTORY_PROFILE, user_text_file_profile="fa-user-text-files-v1")))
    opened = claimed_turn(c, "three-model-steps")
    workers, store = c.h.runtime.dep.workers, c.h.runtime.store
    start_time = c.h.time
    sent, observations = [], []
    active_source_thread = [None]
    injected = [False]
    entry = c.authority.model_session
    consistent_read, read_record = store.consistent_read, store.read_record

    def timer_tick():
        before = workers.inspect(opened.binding.job_id)
        guard = store._guard()
        original = workers.adapter
        workers.adapter = replace(original, query=lambda binding: report_unknown(c, opened, binding),
                                  verify=c.native.verify)
        try:
            c.h.runtime.controller.step(opened.binding.binding_id)
        finally:
            workers.adapter = original
        after = workers.inspect(opened.binding.job_id)
        changed = [name for name in type(before).model_fields if getattr(before, name) != getattr(after, name)]
        observations.append({"row": worker_key("job", opened.binding.job_id), "changed": changed,
                             "guard_delta": store._guard()[0].revision - guard[0].revision})

    def current_entry(*args, **kwargs):
        # The third model call's route authority read; nothing is re-read after consume.
        if len(sent) == 2:
            active_source_thread[0] = get_ident()
        try:
            return entry(*args, **kwargs)
        finally:
            active_source_thread[0] = None

    def overlap(callback, **options):
        if get_ident() != active_source_thread[0] or injected[0]:
            return consistent_read(callback, **options)

        def read_then_observe():
            value = callback()
            injected[0] = True
            with ThreadPoolExecutor(max_workers=1) as observer:
                observer.submit(timer_tick).result(timeout=30)
            return value

        return consistent_read(read_then_observe, **options)

    def ticket_overlap(row_key, *args, **kwargs):
        # An admitted turn ticket (2093b072) authenticates without storage and reads its session row once.
        value = read_record(row_key, *args, **kwargs)
        if get_ident() == active_source_thread[0] and not injected[0]:
            injected[0] = True
            with ThreadPoolExecutor(max_workers=1) as observer:
                observer.submit(timer_tick).result(timeout=30)
        return value

    monkeypatch.setattr(c.authority, "model_session", current_entry)
    monkeypatch.setattr(store, "consistent_read", overlap)
    monkeypatch.setattr(store, "read_record", ticket_overlap)

    async def send(body, **options):
        sent.append(body)
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"step"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"}, request=httpx.Request("POST", "https://synthetic.invalid/model"))

    provider = c.h.composition.assistant.provider
    monkeypatch.setattr(provider, "send", send)
    app = api.create_assistant_app(c.authority, c.h.composition.verifier, provider=provider)
    headers = {"Authorization": "Bearer " + token(signing_key, {
        "oid": opened.agent.oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",)), api.TICKET_HEADER: opened.ticket.get_secret_value()}
    root = f"/behavior/v1/bindings/{opened.binding.binding_id}/assistant/sessions/{opened.assignment.session_id}"
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        for number, elapsed in enumerate((20, 42, 66), 1):
            clock[0] = float(elapsed)
            c.h.time = start_time + timedelta(seconds=elapsed)
            body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": f"Step {number}"}], "stream": True}
            identifier = digest(body)
            c.authority.reserve_model(opened.session, aw.Reservation(
                request_id=identifier, request_sha256=identifier), opened.agent)
            response = client.post(root + "/model", json={"request_id": identifier, "body": body}, headers=headers)
            assert response.status_code == 200, (number, response.text, observations)
    assert injected[0] and len(sent) == 3
    assert observations[0]["changed"] == [] and observations[0]["guard_delta"] == 0
    assert c.sessions.calls == []


@pytest.mark.parametrize("missing", [False, True])
def test_observer_queries_again_after_five_minutes_or_without_activity(synthetic_standing_v1, monkeypatch, missing):
    c = synthetic_standing_v1
    clock = [0.0]
    monkeypatch.setattr(behavior_assistant, "monotonic", lambda: clock[0], raising=False)
    opened = claimed_turn(c, "inactive-running-sdk")
    workers = c.h.runtime.dep.workers
    queried = []

    def query(binding):
        queried.append(binding.job_id)
        return report_unknown(c, opened, binding)

    monkeypatch.setattr(workers, "adapter", replace(workers.adapter, query=query, verify=c.native.verify))
    clock[0] = 299.0
    c.h.runtime.controller.step(opened.binding.binding_id)
    assert queried == []
    if missing:
        c.authority._instruction_activity.clear()
    else:
        clock[0] = 300.0
    c.h.time += timedelta(seconds=300)
    status = c.h.runtime.controller.step(opened.binding.binding_id)
    assert queried == [opened.binding.job_id]
    assert status.state == "capacity_uncertain" and not status.result_forwarded
    assert c.sessions.calls == []


def test_cancelled_active_sdk_still_uses_its_original_stop(synthetic_standing_v1):
    c = synthetic_standing_v1
    opened = claimed_turn(c, "cancel-active-sdk")
    workers = c.h.runtime.dep.workers
    stopped = []
    original = workers.adapter
    workers.adapter = replace(original, stop=lambda command: stopped.append(command) or None)
    try:
        workers.cancel(opened.binding.job_id, opened.binding.owner, expected_binding=opened.session.execution)
        c.h.runtime.controller.step(opened.binding.binding_id)
    finally:
        workers.adapter = original
    assert len(stopped) == 1 and stopped[0].binding == opened.session.execution
    assert workers.inspect(opened.binding.job_id).stop_credit == 0
    from shared_context.errors import Problem
    with pytest.raises(Problem) as fenced:
        c.authority.current_session(opened.binding.binding_id, opened.assignment.session_id,
                                    opened.ticket, opened.agent)
    assert fenced.value.status == 403 and c.sessions.calls == []


def test_valid_progress_refreshes_activity_without_making_it_authority(observer_case, signing_key, monkeypatch):
    c, loop = observer_case
    clock = [0.0]
    monkeypatch.setattr(behavior_assistant, "monotonic", lambda: clock[0])
    opened = claimed_turn(c, "active-progress-sdk")
    workers = c.h.runtime.dep.workers
    queried = []

    def query(binding):
        queried.append(binding.job_id)
        return report_unknown(c, opened, binding)

    monkeypatch.setattr(workers, "adapter", replace(workers.adapter, query=query, verify=c.native.verify))
    app = api.create_assistant_app(c.authority, c.h.composition.verifier, provider=c.h.composition.assistant.provider)
    headers = {"Authorization": "Bearer " + token(signing_key, {
        "oid": opened.agent.oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",)), api.TICKET_HEADER: opened.ticket.get_secret_value()}
    path = f"/behavior/v1/bindings/{opened.binding.binding_id}/assistant/sessions/{opened.assignment.session_id}/progress"
    from shared_context.behavior_assistant_progress import public_event
    event = public_event({"type": "tool.execution_start", "timestamp": c.h.time.isoformat(),
        "data": {"tool_name": "read_context", "tool_call_id": "original"}}, 1, {}, allowed_tools=("read_context",))
    clock[0] = 290.0
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        response = client.post(path, json=aw.ProgressUpdate(request_id="progress-one",
            progress=aw.PublicProgress(events=(event,))).model_dump(mode="json"), headers=headers)
        assert response.status_code == 200, response.text
    clock[0] = 350.0
    c.h.runtime.controller.step(opened.binding.binding_id)
    assert queried == []
    changed = opened.session.execution.model_copy(update={"generation": opened.session.execution.generation + 1})
    assert not c.authority.instruction_recently_active(changed)
    c.control.revoke(c.human, "revoke-even-if-active")
    from shared_context.errors import Problem
    with pytest.raises(Problem) as denied:
        c.authority.current_session(opened.binding.binding_id, opened.assignment.session_id,
                                    opened.ticket, opened.agent)
    assert denied.value.status == 403
    assert c.sessions.calls == []
