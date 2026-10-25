"""The existing signed bridge reads scheduled originals and never becomes a producer."""

from datetime import timedelta

import pytest

from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token
from test_native_routine_api import clock_call, daily_definition
from test_native_turn_api import native_api
from test_web_test_console_native import first, native_console, progress, send
from test_web_test_console_hitl import hitl_console, route, ui_post
from test_web_test_console_questions import attention_of, body as answer_body, question_call
from web_test_console.adapter import canonical
from web_test_console.native import RECEIVED_PHASE


@pytest.fixture
def routine_console(request, monkeypatch):
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    monkeypatch.setenv("FA_NATIVE_ROUTINE_CLOCK", "api")
    return request.getfixturevalue("native_console")


@pytest.fixture
def reminder_hitl_console(request, monkeypatch):
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    monkeypatch.delenv("FA_NATIVE_ROUTINE_CLOCK", raising=False)
    return request.getfixturevalue("hitl_console")


def test_progress_finds_the_scheduled_original_without_a_human_submit_or_retry_button(routine_console, signing_key):
    case = routine_console
    draft = first(case)
    assert send(case, draft).status_code == 200
    case.api.remote.completed(draft["request_id"])
    assert progress(case, draft["thread_id"]).status_code == 200
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    prefix = "/native-routines/v1/agents/" + case.api.foundry.target.agent_id
    body = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    owner = case.c.h.headers(signing_key, "Behavior.Read Behavior.Approve")
    assert case.api.client.post(prefix + "/definitions", json=body, headers=owner).status_code == 200
    case.c.h.time = due + timedelta(seconds=2)
    admitted = clock_call(case.api.composition, case.api.foundry.target.agent_id, "trigger", {
        "routine_id": "daily-summary", "scheduled_for": due.isoformat()})
    assert admitted.status_code == 202, admitted.text
    rid = admitted.json()["request_id"]
    # 2026-10-23: the run is its own conversation; the owner's list names it by its scheduled local time.
    listed = case.ui.client.get("/api/instructions/models?thread_id=" + draft["thread_id"])
    assert listed.status_code == 200, listed.text
    assert next(thread["label"] for thread in listed.json()["composer"]["threads"] if thread["thread_id"] == rid) == (
        f"排程 {due:%m-%d %H:%M}：Read the original work.")
    home = progress(case, draft["thread_id"]).json()
    assert not any(item["task_id"] == rid for item in home["tasks"])
    before = len(case.calls)
    updated = progress(case, rid)
    assert updated.status_code == 200, updated.text
    view = updated.json()
    task = next((item for item in view["tasks"] if item["task_id"] == rid), None)
    assert task is not None
    assert task["instruction"] is None and task["routine_text"] == "Read the original work."
    assert task["routine"]["occurrence"]["routine_id"] == "daily-summary"
    assert task["actions"]["submit-retry"]["enabled"] is False
    assert task["actions"]["submit-reconcile"]["enabled"] is False
    assert view["composer"]["append_target"]["request_id"] == rid
    assert view["composer"]["routines"]["enabled"] is True
    assert not any(method == "POST" and path.endswith(("/trigger", "/submit", "/append"))
                   for method, path, _ in case.calls[before:])


def test_a_run_needing_attention_is_listed_in_the_approval_conversation_pointing_to_its_own(routine_console, signing_key):
    """Review 2026-10-23: a run waiting on its approval card (or with no final state yet) shows only inside its own new
    conversation, and the list imports runs only on Agent open. The conversation the routine was approved in keeps a
    reminder naming the run, which also brings the run's conversation into the list."""
    case = routine_console
    draft = first(case)
    assert send(case, draft).status_code == 200
    case.api.remote.completed(draft["request_id"])
    assert progress(case, draft["thread_id"]).status_code == 200
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    prefix = "/native-routines/v1/agents/" + case.api.foundry.target.agent_id
    body = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    owner = case.c.h.headers(signing_key, "Behavior.Read Behavior.Approve")
    assert case.api.client.post(prefix + "/definitions", json=body, headers=owner).status_code == 200
    case.c.h.time = due + timedelta(seconds=2)
    admitted = clock_call(case.api.composition, case.api.foundry.target.agent_id, "trigger", {
        "routine_id": "daily-summary", "scheduled_for": due.isoformat()})
    assert admitted.status_code == 202, admitted.text
    rid = admitted.json()["request_id"]
    # Started and not finished: what a run waiting on an approval card or a question looks like from its receipt.
    case.api.remote.records[rid] = case.api.remote.records[rid].model_copy(update={"status": "started"})
    home = progress(case, draft["thread_id"])
    assert home.status_code == 200, home.text
    label = f"排程 {due:%m-%d %H:%M}：Read the original work."
    assert home.json()["composer"]["routines"]["runs"] == [{
        "thread_id": rid, "status": "started",
        "scheduled_for": due.isoformat(), "local_time": f"{due:%m-%d %H:%M}", "label": label}]
    assert not any(item["task_id"] == rid for item in home.json()["tasks"])
    assert next(thread for thread in home.json()["composer"]["threads"] if thread["thread_id"] == rid)["label"] == label
    # Review 2026-10-23 (P3-4): a row the previous head wrote has no routine_id in its intent; it still lists.
    journal = case.ui.console.journal
    saved = journal.get(rid, RECEIVED_PHASE)
    del saved["intent"]["routine"]["routine_id"]
    with journal.connect() as db:
        db.execute("UPDATE intents SET intent=? WHERE task_id=? AND phase=?",
                   (canonical(saved["intent"]).decode(), rid, RECEIVED_PHASE))
    again = progress(case, draft["thread_id"])
    assert again.status_code == 200, again.text
    assert [run["thread_id"] for run in again.json()["composer"]["routines"]["runs"]] == [rid]
    assert next(thread for thread in again.json()["composer"]["threads"] if thread["thread_id"] == rid)["label"] == label
    run = progress(case, rid).json()
    assert run["composer"]["conversation_id"] == admitted.json()["conversation_id"]
    assert run["composer"]["routines"]["runs"] == []
    case.api.remote.completed(rid)
    assert progress(case, draft["thread_id"]).json()["composer"]["routines"]["runs"] == []


@pytest.mark.parametrize("resolution", ["answered", "expired"])
def test_reminder_uses_the_same_original_question_and_does_not_recreate_closed_attention(reminder_hitl_console, resolution):
    case = reminder_hitl_console
    draft, _ = question_call(case)
    original = attention_of(case, draft)[0]["item"]
    due = case.c.h.time + timedelta(minutes=1)
    prefix = "/native-routines/v1/agents/" + case.api.foundry.target.agent_id
    saved = case.api.client.post(prefix + "/definitions", headers=case.api.headers, json={
        "request_id": "original-question-reminder", "routine_id": "pending-question",
        "conversation_id": original["question"]["call"]["conversation_id"],
        "related_request_id": draft["request_id"], "kind": "reminder", "enabled": True,
        "schedule": {"kind": "once", "time_zone": "UTC", "scheduled_for": due.isoformat()},
    })
    assert saved.status_code == 200, saved.text
    case.c.h.time = due + timedelta(seconds=1)
    current = attention_of(case, draft)
    assert len(current) == 1 and current[0]["item"] == original
    assert current[0]["reminder"]["request_id"] == draft["request_id"]
    if resolution == "answered":
        assert ui_post(case, route(case, "answers"), answer_body(original)).status_code == 200
    else:
        case.c.h.time += timedelta(seconds=601)
    assert attention_of(case, draft) == []
    assert not [call for call in case.calls if call[1].endswith(("/trigger", "/decisions"))]
