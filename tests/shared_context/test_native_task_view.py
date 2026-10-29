"""Pins NativeInstructionControl.task_view: one real signed native send, then the control's own state is varied."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE
from web_test_console.adapter import ConsoleError
from web_test_console.native import RECEIVED_PHASE
from test_web_test_console_native import first, native_console, send  # noqa: F401
from test_native_turn_api import native_api  # noqa: F401
from test_native_turn_wire import agent_receive  # noqa: F401
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401

REASON = "此原生回合目前不能執行這個動作。"
ACTIONS = ["status", "submit-reconcile", "submit-retry", "read-reply", "cancel", "read-cancel-receipt",
           "confirm-reply", "read-files", "read-skill-draft", "read-images", "read-appends", "read-progress",
           "read-system-prompt", "submit", "fetch", "acknowledge", "reconcile", "read-context", "read-delegation"]
KEYS = ["state_profile", "task_id", "submit_request_id", "thread_id", "label", "description", "mode", "instruction",
        "agent_text", "continuation", "continuation_text", "continuation_state", "continuation_error",
        "continuation_usage", "source", "admission", "requested_model", "status", "native_receipt", "not_accepted",
        "progress", "progress_error", "result", "reply", "submission", "submission_observation", "cancellation",
        "acknowledgment", "user_files", "user_images", "attachments", "actions", "error", "response_reconciliation",
        "appends", "append_error"]


class Dump:
    def __init__(self, tag, **attrs):
        self.tag = tag
        self.__dict__.update(attrs)

    def model_dump(self, mode=None):
        assert mode == "json"
        return {"tag": self.tag}


class Journal:
    """The console journal with chosen entries on top; everything else reads the real one."""

    def __init__(self, real):
        self.real, self.extra = real, {}

    def get(self, key, phase):
        return self.extra[(key, phase)] if (key, phase) in self.extra else self.real.get(key, phase)

    def __getattr__(self, name):
        return getattr(self.real, name)


def enabled(view):
    return [name for name, action in view["actions"].items() if action["enabled"]]


def check_reasons(view):
    assert list(view["actions"]) == ACTIONS
    for action in view["actions"].values():
        assert action == {"enabled": action["enabled"], "reason": "" if action["enabled"] else REASON}


@pytest.fixture
def sent(native_console, monkeypatch):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    n = c.ui.console.native_instructions
    journal = Journal(n.journal)
    monkeypatch.setattr(n, "journal", journal)
    rid, thread = draft["request_id"], draft["thread_id"]
    return SimpleNamespace(n=n, rid=rid, thread=thread, base=n.receipts[rid], journal=journal)


def receipt(s, **update):
    s.n.receipts[s.rid] = s.base.model_copy(update=update)
    return s.n.receipts[s.rid]


def test_accepted_receipt_full_view(sent):
    s, n = sent, sent.n
    view = n.task_view(s.rid, s.thread)
    assert list(view) == KEYS
    body = {"authorize_own_result": True, "content": "Original native instruction", "model": "gpt-4.1-mini",
            "request_id": s.rid}
    body_sha256 = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
    assert view == {
        "state_profile": "foundry-native-v1", "task_id": s.rid, "submit_request_id": s.rid, "thread_id": s.thread,
        "label": "Original native instruction", "description": "原生回合；狀態以 Agent 保存的回條為準。",
        "mode": "native_turn", "instruction": "Original native instruction", "agent_text": None,
        "continuation": None, "continuation_text": None, "continuation_state": None, "continuation_error": None,
        "continuation_usage": None, "source": None,
        "admission": {"conversation_id": "native-cid", "turn_number": None, "model": "gpt-4.1-mini"},
        "requested_model": "gpt-4.1-mini",
        "status": {"data": {"state": "accepted"}, "observed_at": None},
        "native_receipt": {"status": "accepted", "cancel_requested": False, "delivered": False, "failure_code": None,
                           "notification_error": None, "hosted_session_id": "fa-native-b1b63a7240c87d49a0cd334942bdd6b39440c6779f916430",
                           "response_id": None, "dispatch": None},
        "not_accepted": False, "progress": None, "progress_error": None, "result": None, "reply": None,
        # The journal keeps the receipt exactly as the Agent returned it.
        "submission": {"state": "CONFIRMED", "observed_at": "2026-09-16T01:00:00+00:00",
                       "native_receipt": s.base.model_dump(mode="json"),
                       "intent": {"body": body, "body_sha256": body_sha256, "created_at": "2026-09-16T01:00:00+00:00",
                                  "method": "POST", "path": "/work-policy/v1/native-turns/submit",
                                  "state_profile": "foundry-native-v1"}},
        "submission_observation": None, "cancellation": None, "acknowledgment": None,
        "user_files": [], "user_images": [], "attachments": [],
        "actions": view["actions"], "error": None, "response_reconciliation": None, "appends": [],
        "append_error": None,
    }
    check_reasons(view)
    assert enabled(view) == ["status", "submit-reconcile", "read-appends", "read-progress", "read-system-prompt"]


def test_observed_receipt_actions(sent):
    s, n = sent, sent.n
    n.observed[s.thread] = "2026-01-01T00:00:00+00:00"
    view = n.task_view(s.rid, s.thread)
    assert view["status"] == {"data": {"state": "accepted"}, "observed_at": "2026-01-01T00:00:00+00:00"}
    assert enabled(view) == ["status", "submit-retry", "cancel", "read-appends", "read-progress",
                             "read-system-prompt"]
    receipt(s, status="started")
    assert enabled(n.task_view(s.rid, s.thread)) == ["status", "cancel", "read-appends", "read-progress",
                                                     "read-system-prompt"]
    receipt(s, status="completed")
    assert enabled(n.task_view(s.rid, s.thread)) == ["status", "read-appends", "read-progress", "read-system-prompt"]
    receipt(s, cancel_requested=True)
    view = n.task_view(s.rid, s.thread)
    assert enabled(view) == ["status", "submit-retry", "read-appends", "read-progress", "read-system-prompt"]
    assert view["native_receipt"]["cancel_requested"] is True
    # The reply: readable, and confirmable until delivered.
    receipt(s)
    n.answers[s.rid] = (SimpleNamespace(text_sha256="f" * 64, text_bytes=5), "héllo".encode("utf-8"))
    view = n.task_view(s.rid, s.thread)
    assert view["reply"] == {"data": {"text": "héllo"}, "sha256": "f" * 64, "byte_length": 5,
                             "observed_at": "2026-01-01T00:00:00+00:00"}
    assert view["result"] is None
    assert enabled(view) == ["status", "submit-retry", "read-reply", "cancel", "confirm-reply", "read-appends",
                             "read-progress", "read-system-prompt"]
    receipt(s, delivered=True)
    view = n.task_view(s.rid, s.thread)
    assert "confirm-reply" not in enabled(view) and "read-reply" in enabled(view)
    del n.answers[s.rid]
    receipt(s, result=Dump("result"))
    view = n.task_view(s.rid, s.thread)
    assert view["reply"] is None
    assert enabled(view) == ["status", "submit-retry", "read-reply", "cancel", "read-appends", "read-progress",
                             "read-system-prompt"]


def test_unauthenticated_has_no_action_and_reads_no_retry(sent, monkeypatch):
    s, n = sent, sent.n
    n.observed[s.thread] = "now"
    n.answers[s.rid] = (SimpleNamespace(text_sha256="f" * 64, text_bytes=1), b"x")
    s.journal.extra[(s.rid, "cancel")] = {"state": "CONFIRMED"}
    monkeypatch.setattr(n.console.vault, "state", lambda: "LOGIN_REQUIRED")
    monkeypatch.setattr(n, "_retryable", lambda rid: pytest.fail("not read without a login"))
    monkeypatch.setattr(n, "profile", lambda thread: NATIVE_RESPONSES_PROFILE)
    monkeypatch.setattr(n, "response_reconcile_allowed", lambda value: pytest.fail("not read without a login"))
    monkeypatch.setattr(n, "response_reconciliation", lambda rid: Dump("reconciliation"))
    view = n.task_view(s.rid, s.thread)
    check_reasons(view)
    assert enabled(view) == []
    assert view["state_profile"] == NATIVE_RESPONSES_PROFILE
    assert view["response_reconciliation"] == {"tag": "reconciliation"}
    assert view["cancellation"] == {"state": "CONFIRMED"}


def test_journal_entries_and_errors(sent, monkeypatch):
    s, n = sent, sent.n
    n.observed[s.thread] = "now"
    s.journal.extra[(s.rid, "native-version-refusal-observation")] = {"observation": {"seen": 1}}
    s.journal.extra[(s.rid, "cancel")] = {"cancel": 1}
    s.journal.extra[(s.rid, "acknowledge")] = {"ack": 1}
    n.errors[s.rid] = "SOME_CODE"
    n.continuation_errors[s.rid] = "CONT_CODE"
    n.progress_errors[s.rid] = "PROG_CODE"
    n.appends.errors[s.rid] = "APPEND_CODE"
    usage = []
    monkeypatch.setattr(n, "_continuation_usage", lambda rid, thread: usage.append((rid, thread)) or {"u": 1})
    view = n.task_view(s.rid, s.thread)
    assert enabled(view) == ["status", "cancel", "read-cancel-receipt", "read-appends", "read-progress",
                             "read-system-prompt"]
    assert view["submission_observation"] == {"seen": 1}
    assert (view["cancellation"], view["acknowledgment"]) == ({"cancel": 1}, {"ack": 1})
    assert view["error"] == {"code": "SOME_CODE"}
    assert (view["continuation_error"], view["progress_error"], view["append_error"]) == (
        "CONT_CODE", "PROG_CODE", "APPEND_CODE")
    assert view["continuation_usage"] == {"u": 1} and usage == [(s.rid, s.thread)]
    s.journal.extra[(s.rid, RECEIVED_PHASE)] = {"state": "CONFIRMED"}
    assert n.task_view(s.rid, s.thread)["submission"] == {"state": "OBSERVED", "method": "GET"}


def test_progress_snapshot(sent):
    s, n = sent, sent.n
    n.progress_snapshots[s.rid] = SimpleNamespace(progress=Dump("progress"), recorded="r", finished=True,
                                                  truncated=False, unconfirmed=True, byte_length=9)
    assert n.task_view(s.rid, s.thread)["progress"] == {
        "data": {"state": "accepted", "progress": {"tag": "progress"}}, "recorded": "r", "finished": True,
        "truncated": False, "unconfirmed": True, "byte_length": 9}


def test_receipt_optional_fields(sent):
    s, n = sent, sent.n
    receipt(s, queued_relay=Dump("relay"), assembly=SimpleNamespace(unavailable_sources=[Dump("u1"), Dump("u2")]),
            dispatch=Dump("dispatch"), environment_notice=Dump("notice"), upgrade_waiting_version="7",
            response_id="resp-1", failure_code="F", notification_error="N", turn_number=3)
    native = n.task_view(s.rid, s.thread)["native_receipt"]
    assert list(native) == ["status", "cancel_requested", "delivered", "failure_code", "notification_error",
                            "hosted_session_id", "response_id", "queued_relay", "unavailable_sources", "dispatch",
                            "environment_notice", "upgrade_waiting_version"]
    assert native["queued_relay"] == {"tag": "relay"}
    assert native["unavailable_sources"] == [{"tag": "u1"}, {"tag": "u2"}]
    assert native["dispatch"] == {"tag": "dispatch"} and native["environment_notice"] == {"tag": "notice"}
    assert (native["upgrade_waiting_version"], native["response_id"], native["failure_code"],
            native["notification_error"]) == ("7", "resp-1", "F", "N")
    assert n.task_view(s.rid, s.thread)["admission"]["turn_number"] == 3
    receipt(s, assembly=SimpleNamespace(unavailable_sources=[]))
    assert "unavailable_sources" not in n.task_view(s.rid, s.thread)["native_receipt"]


def test_grants_files_and_requested_model(sent, monkeypatch):
    s, n = sent, sent.n

    def granted(*entries, **update):
        capture = s.base.capture.model_copy(update={"capabilities": SimpleNamespace(
            grants=[SimpleNamespace(entry_id=entry) for entry in entries])})
        return receipt(s, capture=capture, **update)

    granted("fa-skillify-v1")
    assert "read-skill-draft" in enabled(n.task_view(s.rid, s.thread))
    granted("other", "fa-image-delivery-v1")
    assert "read-images" not in enabled(n.task_view(s.rid, s.thread)), "no turn number yet"
    granted("other", "fa-image-delivery-v1", turn_number=2)
    view = n.task_view(s.rid, s.thread)
    assert "read-images" in enabled(view) and "read-skill-draft" not in enabled(view)
    granted("fa-skillify-v1", source=Dump("agent"))
    view = n.task_view(s.rid, s.thread)
    assert "read-skill-draft" not in enabled(view), "only a human message"
    assert view["source"] == {"tag": "agent"}
    granted()
    assert not {"read-skill-draft", "read-images"} & set(enabled(n.task_view(s.rid, s.thread)))

    command = s.base.command.model_copy(update={
        "files": (SimpleNamespace(name="a.txt", text="hé"),), "images": (Dump("img"),),
        "attachments": (Dump("att"),)})
    receipt(s, command=command)
    view = n.task_view(s.rid, s.thread)
    assert "read-files" in enabled(view)
    assert view["user_files"] == [{"name": "a.txt", "sha256": hashlib.sha256("hé".encode()).hexdigest(),
                                   "utf8_bytes": 3}]
    assert (view["user_images"], view["attachments"]) == ([{"tag": "img"}], [{"tag": "att"}])

    # A command type without model or images: neither named, and the local command is not read for a receipt.
    receipt(s, command=SimpleNamespace(content="plain", files=(), attachments=()))
    monkeypatch.setattr(n.store, "command", lambda rid: pytest.fail("a receipt names its own model"))
    view = n.task_view(s.rid, s.thread)
    assert "requested_model" not in view and view["user_images"] == [] and view["instruction"] == "plain"
    assert "read-files" not in enabled(view)


def test_texts_label_and_local(sent):
    s, n = sent, sent.n
    view = n.task_view(s.rid, s.thread, {"content": "local text"})
    assert (view["instruction"], view["label"]) == ("local text", "local text")
    long = s.base.command.model_copy(update={"content": "x" * 70})
    receipt(s, command=long)
    view = n.task_view(s.rid, s.thread)
    assert view["label"] == "x" * 60 and view["instruction"] == "x" * 70
    receipt(s, command=long, source=Dump("agent"))
    view = n.task_view(s.rid, s.thread, {"content": "local text"})
    assert (view["instruction"], view["agent_text"], view["label"]) == (None, "x" * 70, "x" * 60)


def test_continuation_receipt(sent):
    s, n = sent, sent.n
    n.observed[s.thread] = "now"
    receipt(s, continuation=Dump("continuation", root_request_id="root"))
    view = n.task_view(s.rid, s.thread, {"content": "local text"})
    assert (view["instruction"], view["agent_text"], view["continuation_text"]) == (
        None, None, "Original native instruction")
    assert view["label"] == s.rid, "a continuation's text is not its label"
    assert view["continuation"] == {"tag": "continuation"}
    assert view["submission"] == {"state": "OBSERVED", "method": "GET"}
    assert "routine" not in view
    assert enabled(view) == ["status", "cancel", "read-appends", "read-progress", "read-system-prompt"]


def test_handoff_cancel_and_state(sent):
    s, n = sent, sent.n
    n.observed[s.thread] = "now"
    receipt(s, status="unknown")
    assert "cancel" not in enabled(n.task_view(s.rid, s.thread))
    n.continuations[s.rid] = Dump("handoff", continuation=object(), cancel_requested=False)
    view = n.task_view(s.rid, s.thread)
    assert "cancel" in enabled(view) and view["continuation_state"] == {"tag": "handoff"}
    n.continuations[s.rid] = Dump("handoff", continuation=object(), cancel_requested=True)
    assert "cancel" not in enabled(n.task_view(s.rid, s.thread))
    n.continuations[s.rid] = Dump("handoff", continuation=None, cancel_requested=False)
    assert "cancel" not in enabled(n.task_view(s.rid, s.thread))
    receipt(s, status="accepted")
    n.continuations[s.rid] = Dump("handoff", continuation=None, cancel_requested=False)
    assert "cancel" in enabled(n.task_view(s.rid, s.thread)), "a handoff only decides an unknown turn"
    del n.observed[s.thread]
    receipt(s, status="unknown")
    n.continuations[s.rid] = Dump("handoff", continuation=object(), cancel_requested=False)
    assert "cancel" not in enabled(n.task_view(s.rid, s.thread)), "only an observed conversation"


def test_routine_receipt(sent):
    s, n = sent, sent.n
    n.observed[s.thread] = "now"
    receipt(s, routine=Dump("routine"))
    view = n.task_view(s.rid, s.thread)
    assert list(view)[7:12] == ["instruction", "agent_text", "routine", "routine_text", "continuation"]
    assert (view["instruction"], view["agent_text"], view["routine"], view["routine_text"]) == (
        None, None, {"tag": "routine"}, "Original native instruction")
    assert view["label"] == "Original native instruction"
    assert view["submission"] == {"state": "OBSERVED", "method": "GET"}
    assert enabled(view) == ["status", "cancel", "read-appends", "read-progress", "read-system-prompt"]


def test_responses_profile(sent, monkeypatch):
    s, n = sent, sent.n
    monkeypatch.setattr(n, "profile", lambda thread: NATIVE_RESPONSES_PROFILE)
    asked = []
    monkeypatch.setattr(n, "response_reconcile_allowed", lambda value: asked.append(value.request_id) or True)
    monkeypatch.setattr(n, "response_reconciliation", lambda rid: Dump("reconciliation"))
    n.observed[s.thread] = "now"
    view = n.task_view(s.rid, s.thread)
    assert view["state_profile"] == NATIVE_RESPONSES_PROFILE
    assert view["response_reconciliation"] == {"tag": "reconciliation"}
    assert enabled(view) == ["status", "submit-reconcile", "cancel", "read-appends", "read-progress",
                             "read-system-prompt"]
    assert asked == [s.rid]
    monkeypatch.setattr(n, "response_reconcile_allowed", lambda value: False)
    assert enabled(n.task_view(s.rid, s.thread)) == ["status", "cancel", "read-appends", "read-progress",
                                                     "read-system-prompt"]
    monkeypatch.setattr(n, "response_reconcile_allowed", lambda value: True)
    receipt(s, routine=Dump("routine"))
    assert "submit-reconcile" not in enabled(n.task_view(s.rid, s.thread)), "a routine is never sent again"
    receipt(s, continuation=Dump("continuation", root_request_id="root"))
    assert "submit-reconcile" in enabled(n.task_view(s.rid, s.thread)), "the profile answer stands after a continuation"

    def broken(rid):
        raise ConsoleError("NATIVE_STATE_INVALID", 502)

    monkeypatch.setattr(n, "response_reconciliation", broken)
    with pytest.raises(ConsoleError) as error:
        n.task_view(s.rid, s.thread)
    assert error.value.code == "NATIVE_STATE_INVALID"


def test_other_profile_reads_no_reconciliation(sent, monkeypatch):
    s, n = sent, sent.n
    monkeypatch.setattr(n, "response_reconciliation", lambda rid: pytest.fail("only the responses lane"))
    monkeypatch.setattr(n, "response_reconcile_allowed", lambda value: pytest.fail("only the responses lane"))
    assert n.task_view(s.rid, s.thread)["response_reconciliation"] is None


def test_unknown_and_refused_rid(sent, monkeypatch):
    s, n = sent, sent.n
    n.observed[s.thread] = "now"
    asked = []
    monkeypatch.setattr(n.store, "command", lambda rid: asked.append(rid) or None)
    view = n.task_view("rid-none", s.thread)
    assert asked == ["rid-none"]
    assert list(view) == [key for key in KEYS if key != "requested_model"]
    assert (view["label"], view["instruction"], view["agent_text"], view["admission"], view["native_receipt"]) == (
        "rid-none", None, None, None, None)
    assert view["status"] == {"data": {"state": "unknown"}, "observed_at": "now"}
    assert view["submission"] == {"state": "NOT_SENT"} and view["not_accepted"] is False
    assert enabled(view) == ["status", "submit-reconcile"]
    monkeypatch.setattr(n.store, "command", lambda rid: {"model": "gpt-x"})
    assert n.task_view("rid-none", s.thread)["requested_model"] == "gpt-x"
    monkeypatch.setattr(n.store, "command", lambda rid: {"model": ""})
    assert "requested_model" not in n.task_view("rid-none", s.thread)
    monkeypatch.setattr(n, "_not_accepted", lambda rid: True)
    monkeypatch.setattr(n, "_retryable", lambda rid: True)
    monkeypatch.setattr(n, "_latest_submission", lambda rid: {"state": "REFUSED"})
    view = n.task_view("rid-none", s.thread)
    assert view["status"]["data"] == {"state": "not_accepted"} and view["not_accepted"] is True
    assert view["submission"] == {"state": "REFUSED"}
    assert enabled(view) == ["status", "submit-reconcile", "submit-retry"]
    s.journal.extra[("rid-none", "native-version-refusal-observation")] = {"observation": "o"}
    view = n.task_view("rid-none", s.thread)
    assert enabled(view) == ["status", "submit-reconcile"] and view["submission_observation"] == "o"


def turn(rid, **fields):
    base = dict(request_id=rid, user_text=None, agent_text=None, routine=None, routine_text=None,
                continuation=None, continuation_text=None, source=None)
    return SimpleNamespace(**{**base, **fields})


def test_history_turn_without_receipt(sent, monkeypatch):
    s, n = sent, sent.n
    monkeypatch.setattr(n.store, "command", lambda rid: None)
    n.turns[s.thread] = [turn("other"), turn("rid-t", user_text="U", agent_text="A")]
    view = n.task_view("rid-t", s.thread)
    assert (view["instruction"], view["agent_text"], view["label"]) == ("U", "A", "U")
    assert view["status"]["data"] == {"state": "completed"}
    assert view["submission"] == {"state": "CONFIRMED"}
    assert enabled(view) == ["status", "submit-reconcile", "read-progress", "read-system-prompt"]
    assert n.task_view("rid-t", s.thread, {"content": "L"})["instruction"] == "L"
    n.turns[s.thread] = [turn("rid-t", agent_text="A", source=Dump("src"))]
    view = n.task_view("rid-t", s.thread)
    assert (view["label"], view["source"]) == ("A", {"tag": "src"})
    n.turns[s.thread] = [turn("rid-t", user_text="U", agent_text="A", routine=Dump("routine"), routine_text="R")]
    view = n.task_view("rid-t", s.thread)
    assert (view["instruction"], view["agent_text"], view["routine_text"], view["label"]) == (None, None, "R", "R")
    assert view["submission"] == {"state": "OBSERVED", "method": "GET"}
    n.turns[s.thread] = [turn("rid-t", user_text="U", continuation=Dump("c"), continuation_text="C")]
    view = n.task_view("rid-t", s.thread)
    assert (view["instruction"], view["continuation_text"], view["continuation"], view["label"]) == (
        None, "C", {"tag": "c"}, "rid-t")
    assert enabled(view) == ["status", "read-progress", "read-system-prompt"]
    n.turns[s.thread] = [turn("rid-t")]
    assert n.task_view("rid-t", s.thread)["label"] == "rid-t"


def test_receipt_fields_win_over_the_history_turn(sent):
    s, n = sent, sent.n
    n.turns[s.thread] = [turn(s.rid, user_text="U", agent_text="A", source=Dump("src"), routine=Dump("routine"),
                              routine_text="R", continuation=Dump("c"), continuation_text="C")]
    view = n.task_view(s.rid, s.thread)
    assert (view["source"], view["continuation"], view["continuation_text"]) == (None, None, None)
    assert "routine" not in view and (view["instruction"], view["agent_text"]) == ("U", "A")


def test_responses_lane_without_receipt_and_acknowledge_only(sent, monkeypatch):
    s, n = sent, sent.n
    monkeypatch.setattr(n, "profile", lambda thread: NATIVE_RESPONSES_PROFILE)
    monkeypatch.setattr(n, "response_reconcile_allowed", lambda value: pytest.fail("only a receipt is reconciled"))
    monkeypatch.setattr(n, "response_reconciliation", lambda rid: None)
    monkeypatch.setattr(n.store, "command", lambda rid: None)
    assert enabled(n.task_view("rid-none", s.thread)) == ["status", "submit-reconcile"]
    s.journal.extra[("rid-none", "acknowledge")] = {"ack": 1}
    view = n.task_view("rid-none", s.thread)
    assert view["acknowledgment"] == {"ack": 1} and "read-cancel-receipt" not in enabled(view)
