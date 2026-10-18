"""8765 uses the same owner API to follow the entire Agent without submitting a turn."""

import contextlib
import json
from types import SimpleNamespace

import httpx
import pytest

from test_native_environment_api import environment_at
from test_web_test_console_native import native_console, first, post
from test_native_turn_api import native_api
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


@pytest.mark.parametrize("follow_ready", [False, True])
def test_version_refusal_is_only_observed_after_original_rid_and_admission_are_checked(native_console, follow_ready):
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView
    from test_web_test_console_native import send, progress

    case = native_console
    draft = first(case)
    def legacy_version_reply(request, response):
        if request.url.path.endswith("/submit") and response.status_code == 409:
            body = response.json()
            body["error"].pop("not_dispatched", None)
            body["error"].pop("request_id", None)
            return httpx.Response(409, json=body, headers=response.headers)
        return response
    case.mutate[0] = legacy_version_reply
    case.api.remote.error = (409, "NATIVE_SESSION_CHANGED")
    assert send(case, draft, "Hi").status_code == 409
    original = case.ui.console.journal.get(draft["request_id"], "submit")
    assert original["state"] == "UNKNOWN"
    case.api.remote.error = None
    if follow_ready:
        case.api.remote.environment = NativeEnvironmentView(
            agent_id=case.api.remote.target.agent_id, request_id="original-follow",
            previous_hosted_session_id="original-old-sid", previous_agent_version="40",
            hosted_session_id=case.api.remote.target.session_id, hosted_agent_version="41", state="ready")
    observed = progress(case, draft["thread_id"])
    assert observed.status_code == (200 if follow_ready else 404), observed.text
    state = case.ui.client.get("/api/state?thread_id=" + draft["thread_id"]).json()
    task = next(item for item in state["tasks"] if item["task_id"] == draft["request_id"])
    assert task["status"]["data"]["state"] == ("not_accepted" if follow_ready else "unknown")
    assert task["actions"]["submit-retry"]["enabled"] is False
    assert case.ui.console.journal.get(draft["request_id"], "submit") == original
    assert not any(command.operation == "submit" for _, command in case.api.remote.calls)
    if follow_ready:
        assert task["submission_observation"]["state"] == "not_accepted"
        assert task["submission_observation"]["environment_request_id"] == "original-follow"
        assert state["composer"]["follow_up_blocker"] == "NATIVE_SUBMISSION_NOT_ACCEPTED"
        assert state["composer"]["can_new_conversation"] is True
        assert state["composer"]["can_submit"] is False
        assert state["composer"]["request_id"] == draft["request_id"]


def test_confirmed_pre_admission_refusal_is_not_stored_as_a_transport_unknown(native_console, monkeypatch):
    from shared_context.behavior_native_sessions import NativeSessions
    from test_web_test_console_native import send

    case = native_console
    draft = first(case)
    case.api.composition._native_sessions = NativeSessions(case.api.composition)
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", lambda _: None)
    monkeypatch.setattr("shared_context.behavior_native_sessions._latest_version_reader", lambda *_: None)
    response = send(case, draft, "A new message")
    assert response.status_code == 409, response.text
    state = case.ui.client.get("/api/state?thread_id=" + draft["thread_id"]).json()
    task = next(item for item in state["tasks"] if item["task_id"] == draft["request_id"])
    assert task["submission"]["state"] == "REFUSED"
    assert task["submission"]["not_dispatched"] is True
    assert task["not_accepted"] is True
    # A refusal with no receipt still names the model that message asked for (the workbench's refusal sentence).
    assert task["requested_model"] == "gpt-4.1-mini"
    assert state["composer"]["follow_up_blocker"] == "NATIVE_SESSION_CHANGED"
    assert not case.api.remote.records and not case.api.remote.calls


@contextlib.contextmanager
def _followed_agent(case, monkeypatch):
    """A first turn finished on the source session and this bridge read it; then the whole Agent followed a new
    version in another process. Yields while API reads of the conversation name the followed session."""
    from shared_context.behavior_native_sessions import native_session_id, native_sessions
    from shared_context.behavior_native_turn_wire import NativeConversation, NativeEnvironmentView, NativeHistoryPage
    from test_web_test_console_native import send, progress

    draft = first(case)
    assert send(case, draft).status_code == 200
    remote = case.api.remote
    remote.completed(draft["request_id"])
    assert progress(case, draft["thread_id"]).status_code == 200
    original = remote.records[draft["request_id"]]
    source = remote.target
    destination = source.model_copy(update={"agent_version": "42"})
    sid = native_session_id(destination, case.api.composition.config.identity.managed_identity_client_id, version="42")
    remote.target = destination.model_copy(update={"session_id": sid})
    remote.session_binding = sid
    remote.environment = NativeEnvironmentView(
        agent_id=source.agent_id, request_id="deployment-follow",
        previous_hosted_session_id=source.session_id, previous_agent_version=source.agent_version,
        hosted_session_id=sid, hosted_agent_version="42", state="ready")
    sessions = native_sessions(case.api.composition)
    # The deployment follow ran in another process; stage then serves from a new API revision (empty cache).
    sessions.forget()
    sessions.binding = None
    sessions.prepared = None
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_: "42")

    def reply(command):
        if command.operation in {"conversation", "history"}:
            conversation = NativeConversation(
                agent_id=original.agent_id, hosted_session_id=original.hosted_session_id,
                execution_hosted_session_id=sid, conversation_id=original.conversation_id,
                last_turn=1, last_request_id=original.request_id, capture=original.capture,
                capture_digest=original.capture_digest)
            return conversation if command.operation == "conversation" else NativeHistoryPage(
                conversation=conversation, turns=(), next_after_turn=None)
        return None

    old_reply = remote.reply_override
    remote.reply_override = reply
    original_call = type(remote).__call__

    def transport(request):
        import json
        if json.loads(request.content)["operation"] not in {"history", "conversation"}:
            remote.reply_override = old_reply
            try:
                return original_call(remote, request)
            finally:
                remote.reply_override = reply
        return original_call(remote, request)

    old_http = case.api.foundry.http
    try:
        with httpx.Client(transport=httpx.MockTransport(transport)) as http:
            case.api.foundry.http = http
            yield SimpleNamespace(draft=draft, original=original, source=source, sid=sid)
    finally:
        case.api.foundry.http = old_http


def test_same_cid_keeps_old_receipt_when_new_turn_uses_followed_agent(native_console, monkeypatch):
    from test_web_test_console_native import send

    case = native_console
    remote = case.api.remote
    with _followed_agent(case, monkeypatch) as follow:
        draft, original, source, sid = follow.draft, follow.original, follow.source, follow.sid
        followed = post(case, "/api/instructions/" + draft["thread_id"] + "/next")
        assert followed.status_code == 200, followed.text
        next_draft = followed.json()["composer"]
        accepted = send(case, next_draft, "A new explicitly submitted follow-up")
        assert accepted.status_code == 200, accepted.text
        received = remote.records[next_draft["request_id"]]
        assert received.conversation_id == original.conversation_id
        assert received.hosted_session_id == sid
        assert remote.records[original.request_id] == original
        assert case.ui.console.native_instructions.receipts[original.request_id].hosted_session_id == source.session_id


# Neal 2026-10-07 13:12 「桌面無法連線」(stage v20): the first turn after a version follow ran on the new session, but
# the desktop asked for the conversation's old, idle one (DESKTOP_VM_NOT_RUNNING) until the turn ended and the page
# read the whole conversation again.
def test_the_desktop_names_the_session_the_first_turn_after_a_follow_runs_on(native_console, monkeypatch):
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from test_web_test_console_native import send, _turn_path

    case = native_console
    remote = case.api.remote
    native = case.ui.console.native_instructions
    with _followed_agent(case, monkeypatch) as follow:
        thread, sid = follow.draft["thread_id"], follow.sid
        composer = lambda: case.ui.client.get("/api/state?thread_id=" + thread).json()["composer"]
        assert composer()["hosted_session_id"] == follow.source.session_id
        next_draft = post(case, "/api/instructions/" + thread + "/next").json()["composer"]
        rid = next_draft["request_id"]
        assert send(case, next_draft, "Use the browser").status_code == 200
        # This bridge read the conversation before the follow; only the new turn's receipt names the new session.
        assert native.conversations[thread].hosted_session_id == follow.source.session_id
        assert native.conversations[thread].execution_hosted_session_id is None
        assert remote.records[rid].hosted_session_id == sid
        assert composer()["hosted_session_id"] == sid
        status = lambda: case.ui.client.get("/api/instructions/" + rid + "/status")
        before = len(case.calls)
        remote.records[rid] = remote.records[rid].model_copy(update={"status": "started", "turn_number": 2})
        running = status()
        assert running.status_code == 200, running.text
        assert running.json()["composer"]["hosted_session_id"] == sid
        remote.completed(rid)
        finished = remote.records[rid].model_dump()
        finished["result"].update(turn_number=2, turn_key="turn:2")
        remote.records[rid] = NativeReceipt.model_validate({**finished, "turn_number": 2})
        done = status()
        assert done.status_code == 200, done.text
        # The page read only this turn, so the conversation is still the one from before; the desktop stays put.
        assert native.conversations[thread].hosted_session_id == follow.source.session_id
        assert done.json()["composer"]["hosted_session_id"] == sid
        assert all(path.startswith(_turn_path(case, rid)) for _, path, _ in case.calls[before:]), "no extra reads"


def test_the_running_session_prefers_the_turn_that_runs_then_any_turn_newer_than_the_conversation():
    from web_test_console.native import running_session

    receipt = lambda sid, status, turn=None: SimpleNamespace(hosted_session_id=sid, status=status, turn_number=turn)
    conversation = lambda sid, execution, last: SimpleNamespace(
        hosted_session_id=sid, execution_hosted_session_id=execution, last_turn=last)
    read_after_follow = conversation("old", "new", 3)
    # A turn still running on the old session (the follow came mid-turn) is where the VM runs now.
    assert running_session(read_after_follow, [receipt("old", "completed", 2), receipt("old", "started", 3)]) == "old"
    assert running_session(read_after_follow, [receipt("old", "completed", 3)]) == "new"
    read_before_follow = conversation("old", None, 1)
    assert running_session(read_before_follow, [receipt("old", "completed", 1), receipt("new", "accepted")]) == "new"
    assert running_session(read_before_follow, [receipt("new", "completed", 2), receipt("old", "completed", 1)]) == "new"
    assert running_session(read_before_follow, [receipt("new", "busy_refused")]) == "old"
    assert running_session(None, [receipt("old", "completed", 1), receipt("new", "completed", 2)]) == "new"
    assert running_session(None, [receipt("only", "busy_refused")]) == "only"
    assert running_session(None, []) is None
    # One Agent runs one turn at a time: still "started" behind a later turn is an old read, not where the VM runs.
    assert running_session(read_after_follow, [receipt("old", "started", 2)]) == "new"
    assert running_session(read_before_follow, [receipt("old", "started", 2), receipt("new", "completed", 3)]) == "new"
    # Two not yet started: the one remembered last (the later send) wins.
    assert running_session(read_before_follow, [receipt("old", "accepted"), receipt("new", "accepted")]) == "new"


def test_owner_can_make_a_new_draft_after_follow_without_replaying_old_unknown(native_console):
    from shared_context.behavior_native_turn_wire import NativeConversation, NativeHistoryPage
    from test_web_test_console_native import send, progress

    case = native_console
    draft = first(case)
    assert send(case, draft).status_code == 200
    case.api.remote.completed(draft["request_id"])
    assert progress(case, draft["thread_id"]).status_code == 200
    next_draft = post(case, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]
    assert send(case, next_draft, "Original input whose outcome is unknown").status_code == 200
    remote = case.api.remote
    accepted = remote.records[next_draft["request_id"]]
    unknown = accepted.model_copy(update={"status": "unknown", "turn_number": 2, "failure_code": "NATIVE_TURN_UNKNOWN"})
    remote.records[unknown.request_id] = unknown

    def reply(command):
        if command.operation == "read":
            return remote.records[command.request_id]
        conversation = NativeConversation(
            agent_id=unknown.agent_id, hosted_session_id=unknown.hosted_session_id,
            execution_hosted_session_id="new-explicit-environment", conversation_id=unknown.conversation_id,
            last_turn=2, last_request_id=unknown.request_id, capture=unknown.capture,
            capture_digest=unknown.capture_digest, continuation_blocker=None)
        assert command.operation == "history"
        return NativeHistoryPage(conversation=conversation, turns=(), next_after_turn=None)

    remote.reply_override = reply
    before = sum(command.operation == "submit" for _, command in remote.calls)
    continued = post(case, "/api/instructions/" + draft["thread_id"] + "/next")
    assert continued.status_code == 200, continued.text
    assert continued.json()["composer"]["request_id"] != unknown.request_id
    assert continued.json()["composer"]["conversation_id"] == unknown.conversation_id
    assert remote.records[unknown.request_id] == unknown
    assert sum(command.operation == "submit" for _, command in remote.calls) == before


def test_never_started_unknown_allows_a_new_rid_with_the_existing_checkpoint(native_console):
    from shared_context.behavior_native_turn_wire import NativeConversation, NativeHistoryPage
    from test_web_test_console_native import send, progress

    case = native_console
    first_draft = first(case)
    assert send(case, first_draft).status_code == 200
    case.api.remote.completed(first_draft["request_id"])
    assert progress(case, first_draft["thread_id"]).status_code == 200
    completed = case.api.remote.records[first_draft["request_id"]]
    next_draft = post(case, "/api/instructions/" + first_draft["thread_id"] + "/next").json()["composer"]
    assert send(case, next_draft, "Original instruction that did not start").status_code == 200
    remote = case.api.remote
    closed = remote.records[next_draft["request_id"]].model_copy(update={
        "status": "unknown", "turn_number": None, "failure_code": "NATIVE_TURN_NOT_STARTED"})
    remote.records[closed.request_id] = closed

    def reply(command):
        if command.operation == "read":
            return remote.records[command.request_id]
        if command.operation == "history":
            conversation = NativeConversation(
                agent_id=completed.agent_id, hosted_session_id=completed.hosted_session_id,
                execution_hosted_session_id=completed.hosted_session_id,
                conversation_id=completed.conversation_id, last_turn=1, last_request_id=completed.request_id,
                capture=completed.capture, capture_digest=completed.capture_digest, continuation_blocker=None)
            return NativeHistoryPage(conversation=conversation, turns=(), next_after_turn=None)
        return None

    remote.reply_override = reply
    before = sum(command.operation == "submit" for _, command in remote.calls)
    observed = progress(case, first_draft["thread_id"])
    assert observed.status_code == 200, observed.text
    task = next(task for task in observed.json()["tasks"] if task["task_id"] == closed.request_id)
    assert task["actions"]["submit-retry"]["enabled"] is False
    continued = post(case, "/api/instructions/" + first_draft["thread_id"] + "/next")
    assert continued.status_code == 200, continued.text
    draft = continued.json()["composer"]
    assert draft["request_id"] != closed.request_id and draft["conversation_id"] == completed.conversation_id
    assert remote.records[closed.request_id] == closed
    assert sum(command.operation == "submit" for _, command in remote.calls) == before


@pytest.mark.parametrize("existing_conversation", [False, True])
def test_explicit_version_refusal_can_retry_same_input_without_changing_its_history(native_console, existing_conversation):
    from test_web_test_console_native import send, progress

    case = native_console
    draft = first(case)
    cid = None
    if existing_conversation:
        assert send(case, draft).status_code == 200
        case.api.remote.completed(draft["request_id"])
        assert progress(case, draft["thread_id"]).status_code == 200
        cid = case.api.remote.records[draft["request_id"]].conversation_id
        draft = post(case, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]
    case.api.remote.error = (503, "AGENT_VERSION_FOLLOWING")
    refused = send(case, draft, "Keep this exact original input")
    assert refused.status_code == 503
    journal = case.ui.console.journal
    original = journal.get(draft["request_id"], "submit")
    assert original["state"] == "REFUSED"
    assert draft["request_id"] not in case.api.remote.records
    case.api.remote.error = None
    observed = progress(case, draft["thread_id"])
    assert observed.status_code == 200, observed.text
    task = next(value for value in observed.json()["tasks"] if value["task_id"] == draft["request_id"])
    assert task["actions"]["submit-retry"]["enabled"]
    assert task["status"]["data"]["state"] == "not_accepted" and task["native_receipt"] is None
    retried = post(case, "/api/tasks/" + draft["request_id"] + "/submit-retry")
    assert retried.status_code == 200, retried.text
    received = case.api.remote.records[draft["request_id"]]
    assert received.command.content == "Keep this exact original input"
    assert received.request_id == draft["request_id"]
    assert cid is None or received.conversation_id == cid
    assert journal.get(draft["request_id"], "submit") == original
    assert len([command for _, command in case.api.remote.calls
                if command.operation == "submit" and command.command.request_id == received.request_id]) == 1
    case.api.remote.completed(draft["request_id"])
    following = post(case, "/api/instructions/" + draft["thread_id"] + "/next")
    assert following.status_code == 200, following.text
    assert following.json()["composer"]["conversation_id"] == received.conversation_id


def test_a_refused_send_retried_with_its_rid_reads_as_confirmed_with_the_switch_notice(native_console):
    """The page shows the notice and clears the sent text only on a CONFIRMED submission; the first submit row stays
    REFUSED in the journal, so the task reports the resend's answer."""
    from test_web_test_console_native import send, with_notice

    case = native_console
    draft = first(case)
    case.api.remote.error = (503, "AGENT_VERSION_FOLLOWING")
    assert send(case, draft, "After the source session was deleted").status_code == 503
    original = case.ui.console.journal.get(draft["request_id"], "submit")
    assert original["state"] == "REFUSED"
    case.api.remote.error = None
    case.mutate[0] = with_notice({"kind": "environment_source_retired"})
    retried = post(case, "/api/tasks/" + draft["request_id"] + "/submit-retry")
    assert retried.status_code == 200, retried.text
    task = retried.json()["task"]
    assert task["submission"]["state"] == "CONFIRMED"
    assert task["submission"]["intent"] == original["intent"]
    assert task["native_receipt"]["environment_notice"] == {"kind": "environment_source_retired"}
    assert case.ui.console.journal.get(draft["request_id"], "submit") == original
    state = case.ui.client.get("/api/state?thread_id=" + draft["thread_id"]).json()
    task = next(item for item in state["tasks"] if item["task_id"] == draft["request_id"])
    assert task["submission"]["state"] == "CONFIRMED" and task["not_accepted"] is False


def test_unknown_manual_retry_cannot_reuse_an_older_definite_refusal(native_console):
    from test_web_test_console_native import send

    case = native_console
    draft = first(case)
    case.api.remote.error = (503, "AGENT_VERSION_FOLLOWING")
    assert send(case, draft).status_code == 503
    refused = case.ui.console.journal.get(draft["request_id"], "submit")
    case.api.remote.error = None
    case.api.remote.lose_submit = True
    assert post(case, "/api/tasks/" + draft["request_id"] + "/submit-retry").status_code == 503
    state = case.ui.client.get("/api/state").json()
    task = next(value for value in state["tasks"] if value["task_id"] == draft["request_id"])
    assert task["not_accepted"] is False and task["status"]["data"]["state"] == "unknown"
    assert task["actions"]["submit-retry"]["enabled"] is False
    assert case.ui.console.journal.get(draft["request_id"], "native-refused-retry-1")["state"] == "UNKNOWN"
    assert case.ui.console.journal.get(draft["request_id"], "submit") == refused
    before = sum(command.operation == "submit" for _, command in case.api.remote.calls)
    assert send(case, draft).status_code == 409
    assert sum(command.operation == "submit" for _, command in case.api.remote.calls) == before


def test_a_model_refusal_before_output_allows_a_new_rid_in_the_same_conversation(native_console):
    from shared_context.behavior_native_turn_wire import NativeConversation, NativeHistoryPage
    from test_web_test_console_native import send, progress

    case = native_console
    first_draft = first(case)
    assert send(case, first_draft).status_code == 200
    case.api.remote.completed(first_draft["request_id"])
    assert progress(case, first_draft["thread_id"]).status_code == 200
    completed = case.api.remote.records[first_draft["request_id"]]
    next_draft = post(case, "/api/instructions/" + first_draft["thread_id"] + "/next").json()["composer"]
    assert send(case, next_draft, "Instruction the model refused before replying").status_code == 200
    remote = case.api.remote
    # Unlike NOT_STARTED, the refused turn had its turn number; nothing ran, so the thread may still continue.
    refused = remote.records[next_draft["request_id"]].model_copy(update={
        "status": "unknown", "turn_number": 2, "failure_code": "NATIVE_MODEL_REFUSED"})
    remote.records[refused.request_id] = refused

    def reply(command):
        if command.operation == "read":
            return remote.records[command.request_id]
        if command.operation == "history":
            conversation = NativeConversation(
                agent_id=completed.agent_id, hosted_session_id=completed.hosted_session_id,
                execution_hosted_session_id=completed.hosted_session_id,
                conversation_id=completed.conversation_id, last_turn=1, last_request_id=completed.request_id,
                capture=completed.capture, capture_digest=completed.capture_digest, continuation_blocker=None)
            return NativeHistoryPage(conversation=conversation, turns=(), next_after_turn=None)
        return None

    remote.reply_override = reply
    before = sum(command.operation == "submit" for _, command in remote.calls)
    assert progress(case, first_draft["thread_id"]).status_code == 200
    continued = post(case, "/api/instructions/" + first_draft["thread_id"] + "/next")
    assert continued.status_code == 200, continued.text
    draft = continued.json()["composer"]
    assert draft["request_id"] != refused.request_id and draft["conversation_id"] == completed.conversation_id
    assert remote.records[refused.request_id] == refused
    assert sum(command.operation == "submit" for _, command in remote.calls) == before


def test_a_refused_first_turn_of_a_new_conversation_does_not_block_it(native_console):
    """2026-10-04 stage A: the first turn's model refused before any output; the conversation must not get stuck."""
    from shared_context.behavior_native_turn_wire import NativeConversation, NativeHistoryPage
    from test_web_test_console_native import send, progress

    case = native_console
    draft = first(case)
    assert send(case, draft, "Hello").status_code == 200
    remote = case.api.remote
    refused = remote.records[draft["request_id"]].model_copy(update={
        "status": "unknown", "turn_number": 1, "failure_code": "NATIVE_MODEL_REFUSED"})
    remote.records[refused.request_id] = refused

    def reply(command):
        if command.operation == "read":
            return remote.records[command.request_id]
        if command.operation == "history":
            conversation = NativeConversation(
                agent_id=refused.agent_id, hosted_session_id=refused.hosted_session_id,
                execution_hosted_session_id=refused.hosted_session_id,
                conversation_id=refused.conversation_id, last_turn=1, last_request_id=refused.request_id,
                capture=refused.capture, capture_digest=refused.capture_digest, continuation_blocker=None)
            return NativeHistoryPage(conversation=conversation, turns=(), next_after_turn=None)
        return None

    remote.reply_override = reply
    before = sum(command.operation == "submit" for _, command in remote.calls)
    observed = progress(case, draft["thread_id"])
    assert observed.status_code == 200, observed.text
    task = next(value for value in observed.json()["tasks"] if value["task_id"] == refused.request_id)
    assert task["native_receipt"]["failure_code"] == "NATIVE_MODEL_REFUSED"
    assert observed.json()["composer"]["can_follow_up"] is True
    continued = post(case, "/api/instructions/" + draft["thread_id"] + "/next")
    assert continued.status_code == 200, continued.text
    following = continued.json()["composer"]
    assert following["request_id"] != refused.request_id and following["conversation_id"] == refused.conversation_id
    assert following["follow_up_blocker"] is None
    assert sum(command.operation == "submit" for _, command in remote.calls) == before


def test_native_environment_metadata_route_is_removed(native_console):
    c = native_console
    root = "/api/instructions/" + first(c)["thread_id"]
    response = c.ui.client.get(root + "/environment")
    assert response.status_code == 404
    assert not any(path.endswith("/environment") for _, path, _ in c.calls)
