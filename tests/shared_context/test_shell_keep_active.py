"""A long run_command must not let the timer observer rewrite its running job (#212 activity window).

While a command runs, Hosted keep_active replays the last saved public progress under its original request ID.
The replay is answered from the original receipt without any write, and refreshes the API activity hint.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import get_ident
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from shared_context import behavior_assistant
from shared_context import behavior_assistant_hosted as hosted
from shared_context import behavior_assistant_api as api
from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_assistant_progress import public_event
from shared_context.behavior_history_prompt import HISTORY_PROFILE
from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_conversation_profile_selection import template
from test_conversation_transfer_startup import claimed_turn, synthetic_standing_v1
from test_instruction_observer_resume import observer_case
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token
from test_model_observer_overlap import report_unknown


def _client(c, loop, opened, signing_key):
    app = api.create_assistant_app(c.authority, c.h.composition.verifier, provider=c.h.composition.assistant.provider)
    headers = {"Authorization": "Bearer " + token(signing_key, {
        "oid": opened.agent.oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",)), api.TICKET_HEADER: opened.ticket.get_secret_value()}
    root = f"/behavior/v1/bindings/{opened.binding.binding_id}/assistant/sessions/{opened.assignment.session_id}"
    return TestClient(app, backend_options={"loop_factory": lambda: loop}), headers, root


def _progress(c, seq=1):
    event = public_event({"type": "tool.execution_start", "timestamp": c.h.time.isoformat(),
        "data": {"tool_name": "run_command", "tool_call_id": "original-command"}}, seq, {},
        allowed_tools=("run_command",))
    return aw.ProgressUpdate(request_id=f"progress-{seq}",
                             progress=aw.PublicProgress(events=(event,))).model_dump(mode="json")


def _rows(c, opened):
    store = c.h.runtime.store
    keys = (behavior_assistant.key("progress", opened.assignment.session_id),
            behavior_assistant.key("session", opened.assignment.session_id),
            worker_key("job", opened.binding.job_id))
    return store._guard()[0].revision, store.read_snapshot(keys)


def test_replayed_progress_writes_nothing_and_its_expiry_resumes_the_query(observer_case, signing_key, monkeypatch):
    c, loop = observer_case
    clock = [0.0]
    monkeypatch.setattr(behavior_assistant, "monotonic", lambda: clock[0])
    opened = claimed_turn(c, "keep-active-expiry")
    workers = c.h.runtime.dep.workers
    queried = []

    def query(binding):
        queried.append(binding.job_id)
        return report_unknown(c, opened, binding)

    monkeypatch.setattr(workers, "adapter", replace(workers.adapter, query=query, verify=c.native.verify))
    client, headers, root = _client(c, loop, opened, signing_key)
    body = _progress(c)
    with client:
        clock[0] = 10.0
        assert client.post(root + "/progress", json=body, headers=headers).status_code == 200
        before = _rows(c, opened)
        for beat in (70.0, 130.0, 190.0, 250.0):
            clock[0] = beat
            response = client.post(root + "/progress", json=body, headers=headers)
            assert response.status_code == 200, response.text
        assert _rows(c, opened) == before
    clock[0] = 549.0
    c.h.runtime.controller.step(opened.binding.binding_id)
    assert queried == []
    clock[0] = 550.0
    c.h.time += timedelta(seconds=550)
    c.h.runtime.controller.step(opened.binding.binding_id)
    assert queried == [opened.binding.job_id]
    assert c.sessions.calls == []



@pytest.mark.parametrize("keep_active", [True, False])
def test_model_call_after_a_six_minute_command_survives_a_timer_tick(observer_case, signing_key, monkeypatch,
                                                                     keep_active):
    """RED without keep_active: six minutes without activity, the tick rewrites the running job."""
    c, loop = observer_case
    clock = [0.0]
    monkeypatch.setattr(behavior_assistant, "monotonic", lambda: clock[0])
    monkeypatch.setenv("FA_DELIVERY_PENDING_INDEX_WRITER", "true")
    monkeypatch.setenv("FA_WORK_REGISTRY_WRITER", "true")
    select_profile(c.control, c.authority, c.human, SelectConversationProfile(
        request_id="history-read-profile", expected_selection_id=None,
        template=template(HISTORY_PROFILE, user_text_file_profile="fa-user-text-files-v1")))
    opened = claimed_turn(c, "six-minute-command")
    workers, store = c.h.runtime.dep.workers, c.h.runtime.store
    start_time = c.h.time
    sent, observations = [], []
    active_source_thread, injected, before_request, calling = [None], [False], [None], [None]
    entry, read_record = c.authority.model_session, store.read_record

    def timer_tick(activity=None):
        before = workers.inspect(opened.binding.job_id)
        original = workers.adapter
        workers.adapter = replace(original, query=lambda binding: report_unknown(c, opened, binding),
                                  verify=c.native.verify)
        current = c.authority._instruction_activity
        if activity is not None:
            # This tick started just before the model request arrived and recorded its own activity.
            c.authority._instruction_activity = activity
        try:
            c.h.runtime.controller.step(opened.binding.binding_id)
        finally:
            workers.adapter = original
            c.authority._instruction_activity = current
        after = workers.inspect(opened.binding.job_id)
        observations.append([name for name in type(before).model_fields if getattr(before, name) != getattr(after, name)])

    def current_entry(*args, **kwargs):
        # The third model call's route authority read (progress also enters here); nothing is re-read after consume.
        if calling[0] == 3:
            active_source_thread[0] = get_ident()
        try:
            return entry(*args, **kwargs)
        finally:
            active_source_thread[0] = None

    def overlap(row_key):
        # Since 2093b072 an admitted turn's model_session reads its session row once with read_record.
        value = read_record(row_key)
        if get_ident() == active_source_thread[0] and not injected[0]:
            injected[0] = True
            with ThreadPoolExecutor(max_workers=1) as observer:
                observer.submit(timer_tick, before_request[0]).result(timeout=30)
        return value

    monkeypatch.setattr(c.authority, "model_session", current_entry)
    monkeypatch.setattr(store, "read_record", overlap)

    async def send(body, **options):
        sent.append(body)
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"step"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"}, request=httpx.Request("POST", "https://synthetic.invalid/model"))

    provider = c.h.composition.assistant.provider
    monkeypatch.setattr(provider, "send", send)
    client, headers, root = _client(c, loop, opened, signing_key)

    def model(number, elapsed):
        clock[0] = float(elapsed)
        c.h.time = start_time + timedelta(seconds=elapsed)
        body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": f"Step {number}"}], "stream": True}
        identifier = digest(body)
        c.authority.reserve_model(opened.session, aw.Reservation(
            request_id=identifier, request_sha256=identifier), opened.agent)
        calling[0] = number
        try:
            return client.post(root + "/model", json={"request_id": identifier, "body": body}, headers=headers)
        finally:
            calling[0] = None

    with client:
        assert model(1, 20).status_code == 200
        assert model(2, 42).status_code == 200
        # The model starts run_command; Hosted saves the public tool-start progress, then the command runs 6 minutes.
        clock[0] = 43.0
        saved = _progress(c)
        assert client.post(root + "/progress", json=saved, headers=headers).status_code == 200
        before = _rows(c, opened)
        for beat in range(103, 406, 60):
            clock[0] = float(beat)
            if keep_active:
                response = client.post(root + "/progress", json=saved, headers=headers)
                assert response.status_code == 200, response.text
        assert _rows(c, opened) == before
        before_request[0] = dict(c.authority._instruction_activity)
        third = model(3, 406)
    assert injected[0]
    if keep_active:
        assert third.status_code == 200, (third.text, observations)
        assert observations == [[]] and len(sent) == 3
    else:
        # Nothing is re-read after consume, so the call itself survives; the RED signal is the job rewrite.
        assert observations != [[]], (third.status_code, observations)
    assert c.sessions.calls == []


def test_keep_active_replays_only_the_saved_progress_while_current():
    sent = []
    client = SimpleNamespace(progress=lambda value: sent.append(value),
                             assignment=SimpleNamespace(task=SimpleNamespace(instruction=object())))
    session = hosted.AssistantSession.__new__(hosted.AssistantSession)
    session.client, session._authority_lock = client, None
    session._progress_saved, session._progress_failed = None, False
    session._cancel_requested = threading.Event()

    async def main():
        session._authority_lock = asyncio.Lock()
        await session.keep_active()
        assert sent == []
        saved = object()
        session._progress_saved = saved
        await session.keep_active()
        await session.keep_active()
        assert sent == [saved, saved]
        session._progress_failed = True
        await session.keep_active()
        session._progress_failed = False
        session._cancel_requested.set()
        await session.keep_active()
        assert sent == [saved, saved]
        session._cancel_requested.clear()
        client.progress = lambda value: (_ for _ in ()).throw(Problem(503, "ASSISTANT_API_UNKNOWN", "lost"))
        await session.keep_active()
        assert session._progress_saved is saved and not session._progress_failed
    asyncio.run(main())


@pytest.mark.parametrize("saved", [True, False])
def test_only_a_confirmed_progress_save_becomes_the_keep_active_snapshot(saved):
    posted = []

    def progress(value):
        posted.append(value)
        if not saved:
            raise Problem(503, "ASSISTANT_API_UNKNOWN", "response lost")
    session = hosted.AssistantSession.__new__(hosted.AssistantSession)
    session.client = SimpleNamespace(progress=progress)
    session._progress_tail = session._progress_pending = session._progress_saved = session._final_progress = None
    session._progress_events, session._progress_failed = [], False
    session._reasoning_summary = hosted.aw.PublicReasoningSummary()
    event = public_event({"type": "tool.execution_start", "timestamp": "2026-09-30T00:00:00+00:00",
                          "data": {"tool_name": "run_command", "tool_call_id": "call-1"}}, 1, {},
                         allowed_tools=("run_command",))

    async def main():
        session._authority_lock = asyncio.Lock()
        session._public_event({"public_progress": event.model_dump(mode="json")})
        await session._progress_tail
    asyncio.run(main())
    assert len(posted) == 1
    assert (session._progress_saved is posted[0]) if saved else (session._progress_saved is None and session._progress_failed)
