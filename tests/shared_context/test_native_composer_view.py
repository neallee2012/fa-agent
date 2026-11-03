"""Pins NativeInstructionControl.view, the composer the page reads: one real send, then the control's state is varied."""

from types import SimpleNamespace

import pytest

from shared_context.native_continuation_wire import MAX_AUTOMATIC_CONTINUATIONS
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE
from web_test_console.adapter import ConsoleError
from web_test_console.native import RECEIVED_PHASE
from test_native_task_view import Dump, Journal, receipt, sent
from test_web_test_console_native import first, native_console, send
from test_native_turn_api import native_api
from test_native_turn_wire import agent_receive
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key

KEYS = ["request_id", "content", "created_at", "thread_id", "state_profile", "editable", "can_append", "append_target",
        "command_incomplete", "can_submit", "can_follow_up", "can_new_conversation", "can_reset", "conversation_id",
        "conversation", "hosted_session_id", "hosted_agent_version", "threads", "agent_conversation", "received_error",
        "progress_available", "awaiting_agent_reply", "peer_reply_overdue", "observed_at", "follow_up_blocker", "model",
        "models", "models_observed_at", "attachments", "user_files", "input_capacity", "message", "progress_polling",
        "connection_metadata", "connection_metadata_is_not_live_attestation", "attention", "platform_history",
        "responses"]
NATIVE_MESSAGE = "原生回合由 Agent 保存；失聯只核對原請求，不會自動重送。"
RESPONSES_MESSAGE = "Responses尚未確認接手時，工作台以原RID有限核回；已開始或unknown不重送。"
SUBMIT_REFUSED = "NATIVE_SUBMISSION_NOT_ACCEPTED"


@pytest.fixture
def blank(native_console, monkeypatch):
    """A new conversation: the first draft, nothing sent."""
    c = native_console
    draft = first(c)
    n = c.ui.console.native_instructions
    journal = Journal(n.journal)
    monkeypatch.setattr(n, "journal", journal)
    monkeypatch.setattr(n.legacy, "models", Dump("models", default_model="gpt-default"))
    return SimpleNamespace(n=n, rid=draft["request_id"], thread=draft["thread_id"], journal=journal)


def conversation(**fields):
    base = dict(conversation_id="cid-held", last_request_id="none", continuation_blocker=None, last_turn=0,
                execution_hosted_session_id=None, hosted_session_id="vm-conversation")
    return SimpleNamespace(**{**base, **fields})


def other(s, rid, *, thread=None, **update):
    """A receipt beside the sent one; ``thread`` also makes it this thread's own."""
    s.n.receipts[rid] = s.base.model_copy(update={"request_id": rid, **update})
    if thread is not None:
        s.n.request_threads[rid] = thread
    return s.n.receipts[rid]


def draft_apart_from_thread(n, monkeypatch):
    """The first draft's request ID equals its thread ID; give the draft its own, so a mix-up of the two shows."""
    real = n.store.current
    monkeypatch.setattr(n.store, "current", lambda thread=None: {**real(thread), "request_id": "rid-apart"})


def proposals(n, monkeypatch):
    monkeypatch.setattr(n.appends, "proposal", lambda original: {"request_id": original.request_id, "pending": False})


def logged_out(n, monkeypatch):
    monkeypatch.setattr(n.console.vault, "state", lambda: "LOGIN_REQUIRED")


def follow(view):
    return view["can_follow_up"], view["follow_up_blocker"]


def test_a_sent_turn_full_view(sent):
    s, n = sent, sent.n
    view = n.view(s.thread)
    assert list(view) == KEYS
    assert view.pop("threads") == n.threads(s.thread, n.store.threads())
    assert view.pop("created_at") == n.store.current(s.thread)["created_at"]
    assert view.pop("connection_metadata") == n.legacy.connection.model_dump(mode="json")
    assert view == {
        "request_id": s.rid, "content": "Original native instruction", "thread_id": s.thread,
        "state_profile": "foundry-native-v1", "editable": False, "can_append": False, "append_target": None,
        "command_incomplete": False, "can_submit": False, "can_follow_up": True, "can_new_conversation": True,
        "can_reset": False, "conversation_id": "native-cid",
        "conversation": {"state_profile": "foundry-native-v1", "conversation_id": "native-cid", "can_follow_up": True,
                         "turns": [{"request_id": s.rid, "state": "accepted"}]},
        "hosted_session_id": s.base.hosted_session_id, "hosted_agent_version": None, "agent_conversation": None,
        "received_error": None, "progress_available": True, "awaiting_agent_reply": False, "peer_reply_overdue": False,
        "observed_at": None, "follow_up_blocker": None, "model": "gpt-4.1-mini",
        "models": view["models"], "models_observed_at": n.legacy.models_observed_at, "attachments": [], "user_files": [],
        "input_capacity": {"unit": "quoted-utf8-bytes", "maximum": 8192}, "message": NATIVE_MESSAGE,
        "progress_polling": {"method": "GET", "interval_ms": 1000, "model_or_reserve": False},
        "connection_metadata_is_not_live_attestation": True, "attention": view["attention"],
        "platform_history": None, "responses": None,
    }


def test_a_new_draft(blank):
    s, n = blank, blank.n
    view = n.view(s.thread)
    assert list(view) == KEYS
    assert (view["content"], view["editable"], view["can_submit"], view["can_follow_up"]) == (None, True, True, False)
    assert (view["conversation_id"], view["conversation"], view["hosted_session_id"]) == (None, None, None)
    assert (view["progress_available"], view["follow_up_blocker"], view["command_incomplete"]) == (False, None, False)
    assert (view["model"], view["models"], view["attachments"], view["user_files"]) == (
        "gpt-default", {"tag": "models"}, [], [])


def test_optional_panels_sit_before_platform_history_in_page_order(sent, monkeypatch):
    s, n = sent, sent.n
    runs = []
    monkeypatch.setattr(n.routines, "enabled", True)
    monkeypatch.setattr(n.routines, "view", lambda thread, value: runs.append((thread, value)) or {"r": 1})
    monkeypatch.setattr(n.hitl, "enabled", True)
    monkeypatch.setattr(n.hitl, "view", lambda thread: {"h": thread})
    monkeypatch.setattr(n.questions, "view", lambda thread: {"q": thread})
    monkeypatch.setattr(n.consents, "view", lambda thread: {"c": thread})
    read, reads = {"run-1": {"label": "run"}}, []
    monkeypatch.setattr(n, "routine_runs", lambda values=None: reads.append(values) or read)
    view = n.view(s.thread)
    at = KEYS.index("platform_history")
    assert list(view) == [*KEYS[:at], "routines", "native_hitl", "native_questions", "native_consents", *KEYS[at:]]
    assert (view["routines"], view["native_hitl"], view["native_questions"], view["native_consents"]) == (
        {"r": 1}, {"h": s.thread}, {"q": s.thread}, {"c": s.thread})
    assert len(runs) == 1 and runs[0][0] == s.thread and runs[0][1] is read
    # One read for the routines panel and one inside ``threads``; both get the one list of threads read for the page.
    assert len(reads) == 2 and reads[0] is reads[1] and reads[0] == n.store.threads()
    monkeypatch.setattr(n.hitl, "enabled", False)
    assert "native_hitl" not in n.view(s.thread) and "routines" in n.view(s.thread)


def test_attention_collects_every_card_kind_through_the_routines_panel(sent, monkeypatch):
    s, n = sent, sent.n
    monkeypatch.setattr(n.hitl, "attention", lambda: ["h"])
    monkeypatch.setattr(n.questions, "attention", lambda: ["q"])
    monkeypatch.setattr(n.consents, "attention", lambda: ["c"])
    monkeypatch.setattr(n.routines, "attention", lambda items: {"seen": items})
    assert n.view(s.thread)["attention"] == {"seen": ["h", "q", "c"]}


# ---------------------------------------------------------------- follow-up and its blocker


def test_logged_out_composer_offers_nothing(sent, monkeypatch):
    s, n = sent, sent.n
    n.observed[s.thread] = "now"
    proposals(n, monkeypatch)
    other(s, "rid-open", thread=s.thread, status="started")
    logged_out(n, monkeypatch)
    view = n.view(s.thread)
    assert (view["can_follow_up"], view["can_new_conversation"], view["can_append"], view["append_target"]) == (
        False, False, False, None)
    assert (view["editable"], view["can_submit"], view["progress_available"]) == (False, False, False)
    assert view["conversation"]["can_follow_up"] is False


def test_logged_out_blank_draft_is_not_editable(blank, monkeypatch):
    s, n = blank, blank.n
    logged_out(n, monkeypatch)
    view = n.view(s.thread)
    assert (view["editable"], view["can_submit"]) == (False, False)


def test_a_refused_send_names_its_own_code_only_when_never_dispatched(sent, monkeypatch):
    s, n = sent, sent.n
    monkeypatch.setattr(n, "_not_accepted", lambda rid: rid == s.rid)
    s.journal.extra[(s.rid, "submit")] = {"code": "ITS_OWN_CODE", "not_dispatched": True, "intent": {}}
    assert follow(n.view(s.thread)) == (True, "ITS_OWN_CODE")
    s.journal.extra[(s.rid, "submit")] = {"code": "ITS_OWN_CODE", "not_dispatched": 1, "intent": {}}
    assert follow(n.view(s.thread)) == (True, SUBMIT_REFUSED)
    s.journal.extra[(s.rid, "submit")] = {"code": "ITS_OWN_CODE", "intent": {}}
    assert follow(n.view(s.thread)) == (True, SUBMIT_REFUSED)


def test_a_refusal_seen_while_the_send_is_still_being_journaled_has_no_prior(sent, monkeypatch):
    s, n = sent, sent.n
    monkeypatch.setattr(n, "_not_accepted", lambda rid: rid == s.rid)
    s.journal.extra[(s.rid, "submit")] = None
    assert follow(n.view(s.thread)) == (True, SUBMIT_REFUSED)


def test_a_retryable_draft_cannot_follow_up_and_shows_no_blocker(sent, monkeypatch):
    s, n = sent, sent.n
    monkeypatch.setattr(n, "_retryable", lambda rid: rid == s.rid)
    view = n.view(s.thread)
    assert follow(view) == (False, None)
    assert view["conversation"]["can_follow_up"] is False


def test_the_conversations_blocker_beats_the_refusal_and_closes_follow_up(sent, monkeypatch):
    s, n = sent, sent.n
    n.conversations[s.thread] = conversation(continuation_blocker="SERVER_BLOCKER")
    assert follow(n.view(s.thread)) == (False, "SERVER_BLOCKER")
    monkeypatch.setattr(n, "_not_accepted", lambda rid: rid == s.rid)
    assert follow(n.view(s.thread)) == (False, "SERVER_BLOCKER")


def test_a_received_conversation_is_read_only_above_every_other_blocker(sent, monkeypatch):
    s, n = sent, sent.n
    draft_apart_from_thread(n, monkeypatch)
    n.observed[s.thread] = "now"
    proposals(n, monkeypatch)
    s.journal.extra[(s.thread, RECEIVED_PHASE)] = {"intent": {"sender_agent_id": "agent-b"}}
    n.conversations[s.thread] = conversation(continuation_blocker="SERVER_BLOCKER")
    other(s, "rid-open", thread=s.thread, status="started")
    view = n.view(s.thread)
    assert follow(view) == (False, "AGENT_CONVERSATION_READ_ONLY")
    assert view["agent_conversation"] == {"sender_agent_id": "agent-b", "read_only": True}
    assert (view["append_target"], view["can_append"]) == (None, False)


def test_being_received_alone_closes_follow_up(sent, monkeypatch):
    s, n = sent, sent.n
    draft_apart_from_thread(n, monkeypatch)
    s.journal.extra[(s.thread, RECEIVED_PHASE)] = {"intent": {"sender_agent_id": "agent-b"}}
    view = n.view(s.thread)
    assert follow(view) == (False, "AGENT_CONVERSATION_READ_ONLY")
    assert view["conversation"]["can_follow_up"] is False


def test_a_routine_runs_conversation_stays_the_owners(sent):
    s, n = sent, sent.n
    s.journal.extra[(s.thread, RECEIVED_PHASE)] = {"intent": {"sender_agent_id": None, "routine": {
        "time_zone": "UTC", "scheduled_for": "2026-01-01T00:00:00+00:00"}}}
    view = n.view(s.thread)
    assert view["agent_conversation"] is None and follow(view) == (True, None)


# ---------------------------------------------------------------- what may be submitted


def test_each_condition_alone_closes_submit(blank, monkeypatch):
    s, n = blank, blank.n
    assert n.view(s.thread)["can_submit"] is True
    s.journal.extra[(s.rid, "submit")] = {"state": "UNKNOWN", "intent": {}}
    view = n.view(s.thread)
    assert (view["can_submit"], view["command_incomplete"], view["progress_available"]) == (False, True, True)
    del s.journal.extra[(s.rid, "submit")]
    # The first draft's RID is also its thread ID, so a received import here is a routine run, the owner's own.
    run = {"sender_agent_id": None, "routine": {"time_zone": "UTC", "scheduled_for": "2026-01-01T00:00:00+00:00"}}
    for phase, intent in (("group-observation", {}), (RECEIVED_PHASE, run)):
        s.journal.extra[(s.rid, phase)] = {"state": "OBSERVED", "intent": intent}
        assert n.view(s.thread)["can_submit"] is False, phase
        del s.journal.extra[(s.rid, phase)]
    n.conversations[s.thread] = conversation(continuation_blocker="SERVER_BLOCKER")
    assert n.view(s.thread)["can_submit"] is False
    n.conversations[s.thread] = conversation()
    assert n.view(s.thread)["can_submit"] is True
    monkeypatch.setattr(n.legacy, "models", None)
    view = n.view(s.thread)
    assert (view["can_submit"], view["model"], view["models"]) == (False, None, None)


def test_a_command_keeps_its_model_and_attachments(blank, monkeypatch):
    s, n = blank, blank.n
    monkeypatch.setattr(n.store, "command", lambda rid: {"model": "gpt-chosen", "attachments": ["a"]})
    view = n.view(s.thread)
    assert (view["model"], view["attachments"], view["command_incomplete"]) == ("gpt-chosen", ["a"], False)
    monkeypatch.setattr(n.store, "command", lambda rid: {"content": "text"})
    view = n.view(s.thread)
    assert (view["model"], view["attachments"]) == (None, [])
    monkeypatch.setattr(n.store, "command", lambda rid: {})
    assert n.view(s.thread)["model"] == "gpt-default", "an empty command names no model"


def test_the_drafts_receipt_lists_its_files(sent):
    s, n = sent, sent.n
    command = s.base.command.model_copy(update={"files": (SimpleNamespace(name="a.txt", text="hé"),)})
    receipt(s, command=command)
    files = n.view(s.thread)["user_files"]
    assert [item["name"] for item in files] == ["a.txt"] and files[0]["utf8_bytes"] == 3


# ---------------------------------------------------------------- the conversation


def test_the_conversation_id_comes_from_the_server_then_a_receipt_that_got_in(sent):
    s, n = sent, sent.n
    receipt(s, status="completed")
    n.conversations[s.thread] = conversation()
    view = n.view(s.thread)
    assert view["conversation_id"] == view["conversation"]["conversation_id"] == "cid-held"
    assert view["hosted_session_id"] == "vm-conversation"
    del n.conversations[s.thread]
    receipt(s, status="busy_refused")
    other(s, "rid-second", thread=s.thread, conversation_id="cid-second", status="completed")
    assert n.view(s.thread)["conversation_id"] == "cid-second"
    del n.receipts["rid-second"]
    view = n.view(s.thread)
    assert (view["conversation_id"], view["conversation"]) == (None, None)
    assert view["progress_available"] is True, "the send itself is on record"


def test_progress_needs_a_login_and_a_send_or_a_conversation(blank):
    s, n = blank, blank.n
    assert n.view(s.thread)["progress_available"] is False
    n.conversations[s.thread] = conversation()
    assert n.view(s.thread)["progress_available"] is True


def test_turns_are_listed_in_task_order_with_their_state(sent, monkeypatch):
    s, n = sent, sent.n
    other(s, "rid-second", thread=s.thread, status="completed")
    assert n.view(s.thread)["conversation"]["turns"] == [{"request_id": s.rid, "state": "accepted"},
                                                         {"request_id": "rid-second", "state": "completed"}]


def test_the_page_reads_the_servers_observed_stamp_and_received_error(sent):
    s, n = sent, sent.n
    n.observed[s.thread] = "2026-01-01T00:00:00+00:00"
    n.received_error = "RECEIVED_FAILED"
    view = n.view(s.thread)
    assert (view["observed_at"], view["received_error"]) == ("2026-01-01T00:00:00+00:00", {"code": "RECEIVED_FAILED"})


# ---------------------------------------------------------------- the turn an append would join


def ended(s):
    receipt(s, status="completed")
    s.n.observed[s.thread] = "now"


def test_the_append_target_prefers_the_drafts_own_open_turn(sent, monkeypatch):
    s, n = sent, sent.n
    n.observed[s.thread] = "now"
    proposals(n, monkeypatch)
    other(s, "rid-last", status="started")
    n.conversations[s.thread] = conversation(last_request_id="rid-last")
    view = n.view(s.thread)
    assert view["append_target"] == {"request_id": s.rid, "pending": False} and view["can_append"] is True
    receipt(s, cancel_requested=True)
    assert n.view(s.thread)["append_target"]["request_id"] == "rid-last"


def test_the_append_target_is_the_last_turn_past_its_continuations(sent, monkeypatch):
    s, n = sent, sent.n
    ended(s)
    proposals(n, monkeypatch)
    other(s, "rid-last", status="completed")
    other(s, "rid-c1", status="completed")
    other(s, "rid-c2", status="started")
    n.conversations[s.thread] = conversation(last_request_id="rid-last")
    assert n.view(s.thread)["append_target"] is None, "no continuation yet"
    n.continuations["rid-last"] = SimpleNamespace(request_id="rid-c1")
    n.continuations["rid-c1"] = SimpleNamespace(request_id="rid-c2")
    assert n.view(s.thread)["append_target"]["request_id"] == "rid-c2"


def test_the_chase_stops_at_the_automatic_limit(sent, monkeypatch):
    s, n = sent, sent.n
    assert MAX_AUTOMATIC_CONTINUATIONS == 2
    ended(s)
    proposals(n, monkeypatch)
    for rid in ("rid-last", "rid-c1", "rid-c2"):
        other(s, rid, status="completed")
    other(s, "rid-c3", status="started")
    n.conversations[s.thread] = conversation(last_request_id="rid-last")
    n.continuations.update({"rid-last": SimpleNamespace(request_id="rid-c1"),
                            "rid-c1": SimpleNamespace(request_id="rid-c2"),
                            "rid-c2": SimpleNamespace(request_id="rid-c3")})
    assert n.view(s.thread)["append_target"] is None
    other(s, "rid-c2", status="started")
    assert n.view(s.thread)["append_target"]["request_id"] == "rid-c2"


def test_a_continuation_without_a_receipt_ends_the_chase(sent, monkeypatch):
    s, n = sent, sent.n
    ended(s)
    proposals(n, monkeypatch)
    other(s, "rid-last", status="started")
    n.conversations[s.thread] = conversation(last_request_id="rid-last")
    n.continuations["rid-last"] = SimpleNamespace(request_id="rid-not-here")
    assert n.view(s.thread)["append_target"]["request_id"] == "rid-last"


def test_without_a_conversation_the_chase_starts_at_the_draft(sent, monkeypatch):
    s, n = sent, sent.n
    ended(s)
    proposals(n, monkeypatch)
    other(s, "rid-c1", status="started")
    n.continuations[s.rid] = Dump("handoff", request_id="rid-c1", continuation=None, cancel_requested=False)
    assert n.view(s.thread)["append_target"]["request_id"] == "rid-c1"
    n.conversations[s.thread] = conversation(last_request_id="rid-gone")
    assert n.view(s.thread)["append_target"]["request_id"] == "rid-c1", "a last request not held falls back the same"


def test_the_append_target_falls_back_to_the_newest_open_receipt(sent, monkeypatch):
    s, n = sent, sent.n
    ended(s)
    proposals(n, monkeypatch)
    other(s, "rid-older", thread=s.thread, status="accepted")
    other(s, "rid-newer", thread=s.thread, status="started")
    other(s, "rid-elsewhere", thread="another-thread", status="started")
    assert n.view(s.thread)["append_target"]["request_id"] == "rid-newer"
    other(s, "rid-newer", thread=s.thread, status="started", cancel_requested=True)
    assert n.view(s.thread)["append_target"]["request_id"] == "rid-older"
    other(s, "rid-older", thread=s.thread, status="completed")
    assert n.view(s.thread)["append_target"] is None


def test_an_append_needs_an_observed_conversation(sent, monkeypatch):
    s, n = sent, sent.n
    proposals(n, monkeypatch)
    view = n.view(s.thread)
    assert (view["append_target"], view["can_append"]) == (None, False)
    n.observed[s.thread] = "now"
    assert n.view(s.thread)["can_append"] is True


def test_a_pending_append_is_shown_and_blocks_another(sent, monkeypatch):
    s, n = sent, sent.n
    n.observed[s.thread] = "now"
    pending = {"request_id": s.rid, "append_id": "a", "pending": True}
    monkeypatch.setattr(n.appends, "proposal", lambda original: pending)
    view = n.view(s.thread)
    assert (view["append_target"], view["can_append"]) == (pending, False)


# ---------------------------------------------------------------- waiting on another Agent


def test_awaiting_an_agent_reply(sent, monkeypatch):
    s, n = sent, sent.n
    assert n.view(s.thread)["awaiting_agent_reply"] is False
    original = SimpleNamespace(request_id="delegating")
    monkeypatch.setattr(n, "_peer_requests", lambda thread: (original,) if thread == s.thread else ())
    monkeypatch.setattr(n, "_peer_waiting", lambda value: value is original)
    monkeypatch.setattr(n, "_peer_overdue", lambda value: False)
    view = n.view(s.thread)
    assert (view["awaiting_agent_reply"], view["peer_reply_overdue"]) == (True, False)
    monkeypatch.setattr(n, "_peer_overdue", lambda value: value is original)
    assert n.view(s.thread)["peer_reply_overdue"] is True
    monkeypatch.setattr(n, "_peer_waiting", lambda value: False)
    view = n.view(s.thread)
    assert (view["awaiting_agent_reply"], view["peer_reply_overdue"]) == (False, False)


def test_a_queued_relay_on_this_thread_also_waits(sent):
    s, n = sent, sent.n
    other(s, "rid-elsewhere", thread="another-thread", queued_relay=SimpleNamespace(status="queued"))
    assert n.view(s.thread)["awaiting_agent_reply"] is False
    receipt(s, queued_relay=Dump("relay", status="delivered"))
    assert n.view(s.thread)["awaiting_agent_reply"] is False
    receipt(s, queued_relay=Dump("relay", status="queued"))
    assert n.view(s.thread)["awaiting_agent_reply"] is True


# ---------------------------------------------------------------- the Responses lane


def test_the_responses_lane_shows_its_message_history_and_options(sent, monkeypatch):
    s, n = sent, sent.n
    monkeypatch.setattr(n, "profile", lambda thread: NATIVE_RESPONSES_PROFILE)
    monkeypatch.setattr(n, "response_reconciliation", lambda rid: None)
    monkeypatch.setattr(n, "response_reconcile_allowed", lambda value: False)
    monkeypatch.setattr(n, "platform_history_view", lambda thread: {"history": thread})
    monkeypatch.setattr(n, "responses_options", Dump("options"))
    view = n.view(s.thread)
    assert view["state_profile"] == view["conversation"]["state_profile"] == NATIVE_RESPONSES_PROFILE
    assert (view["message"], view["platform_history"], view["responses"]) == (
        RESPONSES_MESSAGE, {"history": s.thread}, {"tag": "options"})


# ---------------------------------------------------------------- read order


def test_reads_happen_once_and_in_the_order_the_page_depends_on(sent, monkeypatch):
    s, n = sent, sent.n
    order = []

    def spy(owner, name):
        real = getattr(owner, name)

        def called(*args, **kwargs):
            order.append(name)
            result = real(*args, **kwargs)
            order.append(name + " done")
            return result

        monkeypatch.setattr(owner, name, called)

    for owner, name in ((n.store, "current"), (n, "profile"), (n, "_saved"), (n, "views"), (n.console.vault, "state"),
                        (n, "received_thread"), (n, "_not_accepted"), (n, "_retryable"), (n.store, "threads"),
                        (n, "routine_runs"), (n, "threads"), (n.routines, "attention")):
        spy(owner, name)
    n.view(s.thread)
    head = order[:order.index("views")]
    assert head[:2] == ["current", "current done"]
    assert [name for name in head[2:] if name.endswith("done") and name != "current done"] == [
        "profile done", "_saved done"]
    assert order[order.index("views done") + 1:] == [
        "state", "state done", "received_thread", "received_thread done", "_not_accepted", "_not_accepted done",
        "_retryable", "_retryable done", "threads", "threads done", "routine_runs", "routine_runs done",
        "threads", "routine_runs", "routine_runs done", "threads done", "attention", "attention done"]


def test_the_models_are_read_once_for_one_composer(blank, monkeypatch):
    s, n = blank, blank.n
    reads = []

    class Legacy:
        connection = n.legacy.connection
        models_observed_at = "seen"

        @property
        def models(self):
            reads.append(1)
            return Dump("models", default_model="gpt-default")

    monkeypatch.setattr(n, "legacy", Legacy())
    view = n.view(s.thread)
    assert len(reads) == 1
    assert (view["model"], view["models_observed_at"]) == ("gpt-default", "seen")


def test_an_unknown_thread_is_refused_by_the_store(sent, monkeypatch):
    s, n = sent, sent.n
    monkeypatch.setattr(n, "_saved", lambda thread: pytest.fail("nothing is read for an unknown thread"))
    with pytest.raises(ConsoleError) as error:
        n.view("no-such-thread")
    assert error.value.code == "INSTRUCTION_SCOPE"
