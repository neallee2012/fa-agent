"""Local journal and real signed N2 API, with only the Hosted transport replaced."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context.behavior_assistant import _LocalHttp
from shared_context.behavior_inbox import INBOX_VERSION
from shared_context.behavior_native_turn_wire import NativeReceipt
from web_test_console.instructions import NATIVE_LANE as NATIVE_STATE_PROFILE  # the lane label
from shared_context.behavior_assistant_wire import InstructionSubmit
from web_test_console.adapter import ConsoleError, canonical, parse_config, sha
from web_test_console.bridge import Journal, create_app
from web_test_console.instructions import InstructionConnection, InstructionStore
from test_web_test_console_standing import configuration
from test_native_turn_api import native_api
from test_native_turn_wire import agent_receive
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


def store_at(tmp_path):
    journal = Journal(tmp_path / "lanes.sqlite3", SimpleNamespace(sha256="a" * 64))
    clock = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    return InstructionStore(journal, clock), journal, clock


@pytest.mark.parametrize("old", ["command", "unknown-intent", "original-cid"])
def test_old_or_uncertain_threads_never_change_lane(tmp_path, old):
    store, journal, clock = store_at(tmp_path)
    first = store.current()
    body = {"request_id": first["request_id"], "content": "Original"}
    if old == "original-cid":
        body["conversation_id"] = "old-cid"
    if old != "unknown-intent":
        store.prepare(body, freeze=True)
    else:
        journal.begin(first["request_id"], "submit", {"created_at": clock().isoformat()})
    restarted = InstructionStore(journal, clock)
    assert restarted.lane(first["thread_id"], preferred=NATIVE_STATE_PROFILE) is None
    with pytest.raises(ConsoleError) as error:
        restarted.prepare(body, freeze=True, state_profile=NATIVE_STATE_PROFILE)
    assert error.value.code == "INSTRUCTION_ROUTE_CHANGED"
    fresh = restarted.new_thread()
    assert restarted.lane(fresh["thread_id"], preferred=NATIVE_STATE_PROFILE) == NATIVE_STATE_PROFILE


def test_native_lane_is_saved_with_original_command_even_before_send_intent(tmp_path):
    store, journal, clock = store_at(tmp_path)
    draft = store.current()
    body = {"request_id": draft["request_id"], "content": "Original native"}
    store.prepare(body, freeze=True, state_profile=NATIVE_STATE_PROFILE)
    restarted = InstructionStore(journal, clock)
    assert restarted.lane(draft["thread_id"], preferred=None) == NATIVE_STATE_PROFILE
    with pytest.raises(ConsoleError):
        restarted.prepare(body, freeze=True)
    assert restarted.command(draft["request_id"]) == body
    assert journal.get(draft["request_id"], "submit") is None


def test_existing_command_without_lane_row_is_never_upgraded(tmp_path):
    store, journal, _ = store_at(tmp_path)
    draft = store.current()
    with journal.connect() as db:
        db.execute("INSERT INTO instruction_commands VALUES (?,?)",
                   (draft["request_id"], canonical({"request_id": draft["request_id"], "content": "Before lane rollout"}).decode()))
    assert store.lane(draft["thread_id"], preferred=NATIVE_STATE_PROFILE) is None


@pytest.fixture
def native_console(request, tmp_path, signing_key):
    loop = asyncio.new_event_loop()
    api = request.getfixturevalue("native_api")
    c = api.c
    harness = SimpleNamespace(config=SimpleNamespace(channel=SimpleNamespace(version=INBOX_VERSION)),
                              runtime=c.h.runtime, verifier=c.h.composition.verifier)
    config = parse_config(json.dumps(configuration(harness, c.control)).encode("utf-8"))
    connection = InstructionConnection(schema_version="fa-console-instructions-connection-v1",
        configuration_sha256=config.sha256, backend_core_sha256=config.backend_core_sha256,
        state_profile=NATIVE_STATE_PROFILE)
    transport = _LocalHttp(api.client)
    calls = []
    mutate = [lambda request, response: response]
    before = [lambda request: None]

    def forward(request):
        calls.append((request.method, request.url.path, request.content))
        before[0](request)
        return mutate[0](request, transport.handle_request(request))

    backend = httpx.Client(transport=httpx.MockTransport(forward))
    apps, clients = [], []

    def open_ui(*, enabled=True, path=None, loop_factory=None):
        sidecar = connection.model_copy(update={"state_profile": NATIVE_STATE_PROFILE if enabled else None})
        app = create_app(config, path or tmp_path / "native.sqlite3", backend=backend, port=8877,
            token_provider=lambda: api.headers["Authorization"].removeprefix("Bearer "),
            clock=lambda: c.h.time, environment="OFFLINE_TEST", instruction_connection=sidecar)
        client = TestClient(app, base_url="http://127.0.0.1:8877",
                            backend_options={"loop_factory": loop_factory or (lambda: loop)})
        # TestClient without a context opens a short portal per request; keep one portal for the shared loop.
        client.__enter__()
        clients.append(client); apps.append(app)
        client.get("/")
        headers = {"Origin": "http://127.0.0.1:8877",
                   "X-Console-CSRF": client.get("/api/state").json()["csrf"]}
        auth = client.post("/api/authenticate", json={}, headers=headers)
        assert auth.status_code == 200, auth.text
        return SimpleNamespace(app=app, client=client, headers=headers, console=app.state.console)

    try:
        ui = open_ui()
        yield SimpleNamespace(api=api, c=c, ui=ui, calls=calls, config=config, connection=connection,
                              open=open_ui, mutate=mutate, before=before, tmp=tmp_path)
    finally:
        for client in reversed(clients):
            client.__exit__(None, None, None)
        backend.close()
        if not loop.is_closed():
            loop.close()


def post(c, path, body=None):
    return c.ui.client.post(path, json=body or {}, headers=c.ui.headers)


def first(c):
    return c.ui.client.get("/api/state").json()["composer"]


def send(c, draft, text="Original native instruction"):
    return post(c, "/api/instructions/" + draft["request_id"] + "/submit",
                {"content": text, "model": "gpt-4.1-mini"})


def progress(c, thread):
    return c.ui.client.get("/api/instructions/progress?thread_id=" + thread)


def test_native_ui_uses_no_fixed_work_and_reads_complete_results(native_console, monkeypatch):
    c = native_console
    draft = first(c)
    assert draft["state_profile"] == NATIVE_STATE_PROFILE
    monkeypatch.setattr("web_test_console.freeform.derive_work_binding",
                        lambda *_a, **_k: pytest.fail("native UI must not make a FixedTask or job"))
    accepted = send(c, draft)
    assert accepted.status_code == 200, accepted.text
    task = accepted.json()["task"]
    assert task["mode"] == "native_turn" and "job_id" not in task and "binding_id" not in task
    c.api.remote.completed(draft["request_id"])
    update = progress(c, draft["thread_id"])
    assert update.status_code == 200, update.text
    task = next(t for t in update.json()["tasks"] if t["task_id"] == draft["request_id"])
    assert task["reply"]["data"]["text"] == c.api.remote.text
    assert task["reply"]["byte_length"] > 32 * 1024
    assert update.json()["composer"]["can_follow_up"]
    downloaded = c.ui.client.get("/api/instructions/" + draft["request_id"] + "/reply.txt")
    assert downloaded.status_code == 200 and downloaded.content == c.api.remote.text.encode("utf-8")
    assert not any(path in {"/work-policy/v1/submit", "/work-policy/v1/preflight"}
                   for _, path, _ in c.calls)
    assert c.c.control.records == {}


def test_old_pending_environment_journal_does_not_block_refresh(native_console):
    c = native_console
    draft = first(c)
    accepted = send(c, draft)
    assert accepted.status_code == 200, accepted.text
    c.api.remote.completed(draft["request_id"])
    key = "native-environment-" + sha(canonical([c.ui.console.config.console_id, c.ui.console.api_prefix]))
    body = {"request_id": "old-follow", "expected_agent_version": "42", "idle_only": True}
    receipt = {
        "schema_version": "fa-native-environment-v1", "agent_id": c.api.remote.records[draft["request_id"]].agent_id,
        "request_id": "old-follow", "previous_hosted_session_id": "old-sid", "previous_agent_version": "41",
        "hosted_session_id": "new-sid", "hosted_agent_version": "42", "state": "pending",
        "reason": "scan_pending", "scope": "entire-agent", "model_started": False,
        "original_work_replayed": False,
    }
    c.ui.console.journal.begin(key, "environment-transfer", {
        "scope": "entire-agent", "path": "/work-policy/v1/native-turns/environment/advance",
        "body": body, "body_sha256": sha(canonical(body)), "created_at": c.c.h.time.isoformat(),
        "system_managed": True,
    })
    c.ui.console.journal.finish(key, "environment-transfer", {
        "state": "CONFIRMED", "receipt": receipt, "observed_at": c.c.h.time.isoformat(),
        "automatic_retry": True,
    })
    before = len(c.calls)
    update = progress(c, draft["thread_id"])
    assert update.status_code == 200, update.text
    task = next(item for item in update.json()["tasks"] if item["task_id"] == draft["request_id"])
    assert task["reply"]["data"]["text"] == c.api.remote.text
    assert not any("/environment" in path for _, path, _ in c.calls[before:])


@pytest.mark.parametrize("outcome", ["not_delivered", "unknown"])
def test_a_finished_sender_remains_editable_while_queued_delivery_is_observed(native_console, outcome):
    from shared_context.behavior_native_agent_wire import NativePeerRequest, NativeRelaySend
    from shared_context.native_relay_delivery import reference_for

    c, draft = native_console, first(native_console)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    original = c.api.remote.records[draft["request_id"]]
    ref = reference_for(original, NativeRelaySend(
        conversation_id=original.conversation_id, request_id=original.request_id, tool_call_id="queued-call",
        message=NativePeerRequest(recipient_agent_id="peer", kind="request", content="Original brief.")))
    c.api.remote.records[original.request_id] = original.model_copy(update={"queued_relay": ref})
    before = len(c.calls)
    waiting = progress(c, draft["thread_id"])
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["composer"]["can_follow_up"] is True
    assert waiting.json()["composer"]["awaiting_agent_reply"] is True
    c.api.remote.records[original.request_id] = original.model_copy(update={
        "queued_relay": ref.model_copy(update={"status": outcome, "reason": "NATIVE_RELAY_WAIT_EXPIRED"})})
    settled = progress(c, draft["thread_id"])
    assert settled.status_code == 200, settled.text
    assert settled.json()["composer"]["awaiting_agent_reply"] is False
    task = next(row for row in settled.json()["tasks"] if row["task_id"] == original.request_id)
    assert task["native_receipt"]["queued_relay"]["status"] == outcome
    assert task["native_receipt"]["status"] == "completed" and task["reply"]["data"]["text"] == c.api.remote.text
    assert all(method == "GET" for method, _path, _body in c.calls[before:])


def test_a_delegated_reply_past_its_review_deadline_is_marked_unconfirmed_and_still_awaited(native_console):
    """Spec 2026-10-01 「等待到期」: a peer that never answers ends in 「結果尚未確認」, not an endless silent wait."""
    from datetime import timedelta
    from shared_context.behavior_native_agent_wire import NativeRelayAcceptance
    from web_test_console.native import PEER_REPLY_REVIEW

    c, draft = native_console, first(native_console)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    original = c.api.remote.records[draft["request_id"]]
    c.api.remote.records[original.request_id] = original.model_copy(update={"delegation": NativeRelayAcceptance(
        message_id="peer-reply-never", recipient_agent_id="peer", conversation_id="peer-cid",
        submit_digest="b" * 64, status="accepted")})
    waiting = progress(c, draft["thread_id"])
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["composer"]["awaiting_agent_reply"] is True
    assert waiting.json()["composer"]["peer_reply_overdue"] is False
    c.c.h.time += PEER_REPLY_REVIEW - timedelta(seconds=1)
    assert progress(c, draft["thread_id"]).json()["composer"]["peer_reply_overdue"] is False
    c.c.h.time += timedelta(seconds=1)
    before = len(c.calls)
    overdue = progress(c, draft["thread_id"])
    assert overdue.status_code == 200, overdue.text
    assert overdue.json()["composer"]["awaiting_agent_reply"] is True, "the reply is still read when it comes"
    assert overdue.json()["composer"]["peer_reply_overdue"] is True
    assert any(path.endswith("/requests/peer-reply-never") for _method, path, _body in c.calls[before:])
    assert all(method == "GET" for method, _path, _body in c.calls[before:])


def test_native_draft_read_and_download_retain_the_original_m3_bytes_without_a_write(native_console):
    from shared_context.m3_models import NativeArtifactSource, SkillPurpose
    from shared_context.native_skill_draft_wire import NativeSkillDraftReceipt, NativeSkillDraftView

    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    original = c.api.remote.records[draft["request_id"]]
    body = "Original unapproved draft.\r\n<script>plain text</script>\n"
    raw = body.encode()
    binding = c.c.control.policy.template
    source = NativeArtifactSource(
        tenant_id=binding.tenant_id, team_id=binding.team_id,
        agent_id=original.agent_id, agent_oid=binding.agent_oid,
        conversation_id=original.conversation_id, request_id=original.request_id, submit_sha256=original.submit_digest)
    receipt = NativeSkillDraftReceipt(
        native_source=source, artifact_id="a" * 64, version_id="b" * 32, sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw), purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description="Original purpose"),
        metadata_sha256="c" * 64)
    value = NativeSkillDraftView(
        agent_id=original.agent_id, conversation_id=original.conversation_id, request_id=original.request_id,
        state="awaiting_decision", receipt=receipt, body=body)

    def original_draft(request, response):
        if request.url.path.endswith("/skill-draft"):
            return httpx.Response(200, content=value.model_dump_json().encode(), request=request)
        return response

    c.mutate[0] = original_draft
    before = len([call for call in c.calls if call[0] == "POST"])
    read = c.ui.client.get("/api/instructions/" + original.request_id + "/skill-draft")
    assert read.status_code == 200 and read.json()["body"] == body
    downloaded = c.ui.client.get("/api/instructions/" + original.request_id + "/skill-draft.md")
    assert downloaded.status_code == 200 and downloaded.content == raw
    assert downloaded.headers["x-content-sha256"] == receipt.sha256
    assert len([call for call in c.calls if call[0] == "POST"]) == before


def test_notification_failure_is_projected_without_overwriting_the_original_native_reply(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    original = c.api.remote.records[draft["request_id"]]
    c.api.remote.records[draft["request_id"]] = NativeReceipt.model_validate({
        **original.model_dump(), "notification_error": "NATIVE_TURN_BUSY"})
    update = progress(c, draft["thread_id"])
    assert update.status_code == 200
    task = next(item for item in update.json()["tasks"] if item["task_id"] == draft["request_id"])
    assert task["native_receipt"]["notification_error"] == "NATIVE_TURN_BUSY"
    assert task["reply"]["data"]["text"] == c.api.remote.text
    assert c.api.remote.records[draft["request_id"]].result == original.result


def test_a_tool_source_the_turn_ran_without_is_projected_on_its_reply(native_console):
    from shared_context.native_assembly import AssemblyUnavailableSource, NativeAssembly

    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    original = c.api.remote.records[draft["request_id"]]
    plain = progress(c, draft["thread_id"])
    task = next(item for item in plain.json()["tasks"] if item["task_id"] == draft["request_id"])
    assert "unavailable_sources" not in task["native_receipt"]
    # Hosted saved, with the original turn, that WebIQ could not list its tools and the turn ran without it.
    c.api.remote.records[draft["request_id"]] = original.model_copy(update={"assembly": NativeAssembly(
        state="recorded", unavailable_sources=(
            AssemblyUnavailableSource(entry_id="WebIQ", source="WebIQ", reason="upstream_auth"),))})
    update = progress(c, draft["thread_id"])
    assert update.status_code == 200, update.text
    task = next(item for item in update.json()["tasks"] if item["task_id"] == draft["request_id"])
    assert task["native_receipt"]["unavailable_sources"] == [
        {"entry_id": "WebIQ", "source": "WebIQ", "code": "TOOLBOX_LIST_FAILED", "reason": "upstream_auth"}]
    assert task["native_receipt"]["status"] == "completed" and task["reply"]["data"]["text"] == c.api.remote.text


def test_unknown_submit_can_only_read_original_receipt(native_console):
    c = native_console
    draft = first(c)
    c.api.remote.lose_submit = True
    response = send(c, draft)
    assert response.status_code == 503 and response.json()["error"]["code"] == "FOUNDRY_INVOCATION_UNKNOWN"
    assert "沒有送出" not in response.json()["error"]["message"]
    assert "未確認" in response.json()["error"]["message"] or "核對" in response.json()["error"]["message"]
    original_unknown = c.ui.console.journal.get(draft["request_id"], "submit")
    before = sum(command.operation == "submit" for _, command in c.api.remote.calls)
    repeated = send(c, draft)
    assert repeated.status_code == 409
    c.api.remote.lose_submit = False
    observed = progress(c, draft["thread_id"])
    assert observed.status_code == 200, observed.text
    assert sum(command.operation == "submit" for _, command in c.api.remote.calls) == before
    assert c.ui.console.native_instructions.receipts[draft["request_id"]].status == "accepted"
    assert c.ui.console.journal.get(draft["request_id"], "submit") == original_unknown
    assert c.ui.console.journal.get(draft["request_id"], "native-read")["state"] == "VERIFIED"


def test_followup_uses_original_cid_and_only_new_explicit_rid(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    assert progress(c, draft["thread_id"]).status_code == 200
    reads_before = len(c.api.remote.calls)
    next_ = post(c, "/api/instructions/" + draft["thread_id"] + "/next")
    assert next_.status_code == 200, next_.text
    # 2026-10-05 slimming: progress already settled the turn, so /next takes the next RID without re-reading the
    # whole conversation (dev v15 measured 7.7-10 s for that re-read); the API checks again at submit.
    assert c.api.remote.calls[reads_before:] == []
    next_draft = next_.json()["composer"]
    assert next_draft["request_id"] != draft["request_id"]
    assert next_draft["thread_id"] == draft["thread_id"]
    accepted = send(c, next_draft, text="Explicit followup")
    assert accepted.status_code == 200, accepted.text
    new = c.api.remote.records[next_draft["request_id"]]
    assert new.command.conversation_id == c.api.remote.records[draft["request_id"]].conversation_id
    submits = [command for _, command in c.api.remote.calls if command.operation == "submit"]
    assert [value.command.request_id for value in submits] == [draft["request_id"], next_draft["request_id"]]


def test_only_explicit_accepted_resume_reuses_the_same_request(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    original = c.api.remote.records[draft["request_id"]]
    assert progress(c, draft["thread_id"]).status_code == 200
    resumed = post(c, "/api/tasks/" + draft["request_id"] + "/submit-retry")
    assert resumed.status_code == 200, resumed.text
    submissions = [command for _, command in c.api.remote.calls if command.operation == "submit"]
    assert len(submissions) == 2 and submissions[0] == submissions[1]
    assert c.api.remote.records[draft["request_id"]].capture == original.capture
    c.api.remote.records[draft["request_id"]] = original.model_copy(update={"status": "started", "turn_number": 1})
    assert post(c, "/api/tasks/" + draft["request_id"] + "/submit-retry").status_code == 409
    assert sum(command.operation == "submit" for _, command in c.api.remote.calls) == 2


def test_restart_without_sidecar_flag_keeps_original_native_lane(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    before = sum(command.operation == "submit" for _, command in c.api.remote.calls)
    # Reconstruct the control on the same journal without opening a second running event loop.
    from web_test_console.bridge import Console
    restarted = Console(c.config, c.tmp / "native.sqlite3", backend=c.ui.console.backend,
        clock=lambda: c.c.h.time, token_provider=lambda: c.api.headers["Authorization"].removeprefix("Bearer "),
        instruction_connection=c.connection.model_copy(update={"state_profile": None}))
    restarted.authenticate()
    assert restarted.native_thread(draft["thread_id"])
    assert restarted.state(draft["thread_id"])["composer"]["state_profile"] == NATIVE_STATE_PROFILE
    restarted.instruction_progress(draft["thread_id"])
    assert sum(command.operation == "submit" for _, command in c.api.remote.calls) == before
    assert restarted.instructions.tasks(draft["thread_id"]) == []


@pytest.mark.parametrize("code,status", [
    ("NATIVE_STATE_STORE_MISSING", 404), ("NATIVE_STATE_STORE_TTL", 409), ("NATIVE_STATE_STORE_UNKNOWN", 503),
])
def test_native_errors_remain_visible_and_do_not_switch_lane(native_console, code, status):
    c = native_console
    draft = first(c)
    c.api.remote.error = status, code
    failed = send(c, draft)
    assert failed.status_code == status and failed.json()["error"]["code"] == code
    assert c.ui.console.native_thread(draft["thread_id"])
    # Since 7ade5fc9 (#553) the API's submit reads the Agent's current session before it reads the RID, so the store
    # error refuses the turn at that first read; nothing is submitted (same as test_submit_does_not_turn_store_errors_*).
    assert [command.operation for _, command in c.api.remote.calls] == ["session_read"]
    assert not any(path == "/work-policy/v1/submit" for _, path, _ in c.calls)


def test_b_lists_and_reads_a_conversation_another_agent_opened_without_sending(native_console):
    """Stage 14, 2026-10-05: A's handoff to B showed B's reply under A, but B's own workbench listed nothing."""
    from shared_context.behavior_native_turn_wire import NativeAgentReceive
    c = native_console
    own = first(c)
    target = c.api.remote.target

    def received(rid):
        message = agent_receive(rid=rid, recipient=target.agent_id)
        c.api.remote.records[rid] = NativeReceipt(state_profile=message.state_profile,
            agent_id=target.agent_id, hosted_session_id=target.session_id, request_id=rid,
            conversation_id="received-cid-" + rid, command=message.command, capture=message.capture,
            source=message.source, submit_digest=message.sha256, capture_digest=message.capture.sha256,
            status="accepted")
        return message

    received("message-from-a")
    c.api.remote.completed("message-from-a")
    # Progress reads of the owner's own conversation never list received conversations (2026-10-05 slimming).
    assert progress(c, own["thread_id"]).status_code == 200
    assert not any(command.operation == "received" for _, command in c.api.remote.calls)
    listed = c.ui.client.get("/api/instructions/models?thread_id=" + own["thread_id"])
    assert listed.status_code == 200, listed.text
    composer = listed.json()["composer"]
    assert composer["thread_id"] == own["thread_id"] and composer["agent_conversation"] is None
    # The owner's own new draft stays sendable; only the received conversation is read-only.
    assert composer["can_submit"] is True
    assert [thread for thread in composer["threads"] if "sender_agent_id" in thread] == [{
        "thread_id": "message-from-a", "label": "Original Agent message", "sender_agent_id": "sender-agent"}]

    opened = progress(c, "message-from-a")
    assert opened.status_code == 200, opened.text
    composer = opened.json()["composer"]
    assert composer["agent_conversation"] == {"sender_agent_id": "sender-agent", "read_only": True}
    assert (composer["can_submit"], composer["can_follow_up"], composer["can_append"]) == (False, False, False)
    assert composer["follow_up_blocker"] == "AGENT_CONVERSATION_READ_ONLY"
    task = next(task for task in opened.json()["tasks"] if task["task_id"] == "message-from-a")
    assert task["instruction"] is None and task["agent_text"] == "Original Agent message"
    assert task["source"]["kind"] == "agent_request" and task["submission"]["state"] == "OBSERVED"
    assert task["reply"]["data"]["text"] == c.api.remote.text

    refused = post(c, "/api/instructions/message-from-a/next")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "AGENT_CONVERSATION_READ_ONLY"
    appended = post(c, "/api/instructions/message-from-a/append", {"append_id": "append-x", "content": "more"})
    assert appended.status_code == 409 and appended.json()["error"]["code"] == "AGENT_CONVERSATION_READ_ONLY"

    # A later conversation is opened by its own original RID before the next listing is due.
    received("message-later")
    later = progress(c, "message-later")
    assert later.status_code == 200, later.text
    assert later.json()["composer"]["agent_conversation"]["sender_agent_id"] == "sender-agent"
    assert progress(c, "not-a-received-rid").json()["error"]["code"] == "INSTRUCTION_SCOPE"
    assert sum(command.operation == "received" for _, command in c.api.remote.calls) == 1
    assert not any(command.operation in {"submit", "agent_receive", "append"} or isinstance(command, NativeAgentReceive)
                   for _, command in c.api.remote.calls)


def routine_run(c, capture, day):
    """A completed run of routine_origin()'s routine ``day`` days later, opened in its own conversation (2026-10-23)."""
    from datetime import timedelta
    from shared_context.behavior_native_turn_wire import NativeAgentCommand, NativeRoutineSubmit
    from test_native_routine_wire import routine_origin

    target, origin = c.api.remote.target, routine_origin()
    source = origin.model_copy(update={
        "agent_id": target.agent_id, "conversation_id": f"run-cid-{day}", "occurrence": origin.occurrence.model_copy(
            update={"scheduled_for": origin.occurrence.scheduled_for + timedelta(days=day)})})
    run = NativeRoutineSubmit(command=NativeAgentCommand(
        request_id=source.request_id, content="Read the original work."), capture=capture, routine=source)
    c.api.remote.records[source.request_id] = NativeReceipt(
        state_profile=run.state_profile, agent_id=target.agent_id, hosted_session_id=target.session_id,
        request_id=source.request_id, conversation_id=source.conversation_id, command=run.command,
        capture=run.capture, routine=source, submit_digest=run.sha256, capture_digest=run.capture.sha256,
        status="accepted")
    c.api.remote.completed(source.request_id)
    return source.request_id


def test_owner_finds_each_routine_run_as_its_own_dated_conversation_and_can_follow_up(native_console):
    """2026-10-23 (Neal: 不需要接續前一天): each run opens its own conversation; the owner's list names it by the
    routine's local scheduled time, and it is the owner's own conversation, so a follow-up goes there."""
    c = native_console
    own = first(c)
    assert send(c, own).status_code == 200
    # The capture this API resolves for the Team's new conversations, as the routine clock would.
    resolved = c.api.remote.records[own["request_id"]].capture
    rids = [routine_run(c, resolved, day) for day in (0, 1)]
    listed = c.ui.client.get("/api/instructions/models?thread_id=" + own["thread_id"])
    assert listed.status_code == 200, listed.text
    threads = {thread["thread_id"]: thread for thread in listed.json()["composer"]["threads"]}
    # routine_origin() is scheduled 2026-10-04 01:00 UTC in Asia/Taipei.
    assert [threads[rid]["label"] for rid in rids] == [
        "排程 10-04 09:00：Read the original work.", "排程 10-05 09:00：Read the original work."]
    assert all("sender_agent_id" not in threads[rid] for rid in rids)

    opened = progress(c, rids[1])
    assert opened.status_code == 200, opened.text
    composer = opened.json()["composer"]
    assert composer["agent_conversation"] is None and composer["conversation_id"] == "run-cid-1"
    assert (composer["can_submit"], composer["can_follow_up"]) == (False, True)
    task = next(task for task in opened.json()["tasks"] if task["task_id"] == rids[1])
    assert task["routine"]["conversation_id"] == "run-cid-1" and task["submission"]["state"] == "OBSERVED"
    assert task["reply"]["data"]["text"] == c.api.remote.text
    following = post(c, "/api/instructions/" + rids[1] + "/next").json()["composer"]
    sent = send(c, following, text="Explain the second item")
    assert sent.status_code == 200, sent.text
    assert c.api.remote.records[following["request_id"]].conversation_id == "run-cid-1"
    assert not any(command.operation == "routine_receive" for _, command in c.api.remote.calls)


def test_received_import_follows_pages_to_a_known_run_and_lists_the_newest_30_runs(native_console):
    """Review 2026-10-23: a daily run adds a conversation a day, so the first 50-key received page is not enough. The
    import reads on until a page holds a run it already has; the list shows the newest 30 runs (and the open one)."""
    from datetime import timedelta
    from web_test_console.native import RECEIVED_SECONDS

    c = native_console
    own = first(c)
    assert send(c, own).status_code == 200
    resolved = c.api.remote.records[own["request_id"]].capture
    # Saved first, so last in Hosted's newest-first listing: on its second page, behind 55 newer keys.
    latest, last_year = routine_run(c, resolved, 60), routine_run(c, resolved, -365)
    for day in range(55):
        routine_run(c, resolved, day)

    def received():
        return sum(path.endswith("/native-turns/received") for _, path, _ in c.calls)

    before = received()
    listed = c.ui.client.get("/api/instructions/models?thread_id=" + own["thread_id"])
    assert listed.status_code == 200, listed.text
    assert received() - before == 2, (received() - before, listed.json()["composer"]["received_error"])
    threads = listed.json()["composer"]["threads"]
    runs = [thread for thread in threads if "routine" in thread]
    assert threads[0]["thread_id"] == own["thread_id"] and len(runs) == 30
    # routine_origin() is 2026-10-04 01:00 UTC in Asia/Taipei; the run 60 days later is on 12-03.
    assert runs[0]["thread_id"] == latest and runs[0]["label"] == "排程 12-03 09:00：Read the original work."
    assert [run["routine"]["scheduled_for"] for run in runs] == sorted(
        (run["routine"]["scheduled_for"] for run in runs), reverse=True)
    assert last_year not in {run["thread_id"] for run in runs}

    # The next listing stops at the first page: it already holds imported runs.
    newer = routine_run(c, resolved, 61)
    c.c.h.time += timedelta(seconds=RECEIVED_SECONDS + 1)
    before = received()
    again = c.ui.client.get("/api/instructions/models?thread_id=" + own["thread_id"])
    assert again.status_code == 200 and received() - before == 1
    assert next(thread for thread in again.json()["composer"]["threads"] if "routine" in thread)["thread_id"] == newer

    # An older run still opens by its RID and stays listed while it is the open one, with its year.
    opened = progress(c, last_year)
    assert opened.status_code == 200, opened.text
    shown = [thread for thread in opened.json()["composer"]["threads"] if "routine" in thread]
    assert len(shown) == 31 and shown[-1]["thread_id"] == last_year
    assert shown[-1]["label"] == "排程 2025-10-04 09:00：Read the original work."


def test_received_import_reads_one_page_for_an_agent_without_received_conversations(native_console):
    """Review 2026-10-23 (P3-6): own turns fill the first page and none opened here, so nothing behind it is read."""
    from datetime import timedelta
    from web_test_console.native import RECEIVED_SECONDS

    c = native_console
    own = first(c)
    assert send(c, own).status_code == 200
    original = c.api.remote.records[own["request_id"]]
    for index in range(60):  # More than one 50-key page of this Agent's own first messages.
        rid = f"own-request-{index:02d}"
        c.api.remote.records[rid] = original.model_copy(update={"request_id": rid})
    c.c.h.time += timedelta(seconds=RECEIVED_SECONDS + 1)
    before = sum(path.endswith("/native-turns/received") for _, path, _ in c.calls)
    listed = c.ui.client.get("/api/instructions/models?thread_id=" + own["thread_id"])
    assert listed.status_code == 200, listed.text
    assert sum(path.endswith("/native-turns/received") for _, path, _ in c.calls) - before == 1
    assert listed.json()["composer"]["received_error"] is None


def test_received_import_imports_again_a_run_whose_finish_was_lost(native_console):
    """Review 2026-10-23 (P3-2): a row begun but never finished (no ``native_receipt``) is not imported yet."""
    from datetime import timedelta
    from web_test_console.native import RECEIVED_PHASE, RECEIVED_SECONDS

    c = native_console
    own = first(c)
    assert send(c, own).status_code == 200
    rid = routine_run(c, c.api.remote.records[own["request_id"]].capture, 0)
    assert c.ui.client.get("/api/instructions/models?thread_id=" + own["thread_id"]).status_code == 200
    journal = c.ui.console.journal
    assert "native_receipt" in journal.get(rid, RECEIVED_PHASE)
    with journal.connect() as db:
        db.execute("UPDATE intents SET outcome=? WHERE task_id=? AND phase=?", (canonical(
            {"state": "UNKNOWN", "code": "RECONCILE_REQUIRED", "observed_at": c.c.h.time.isoformat()}).decode(),
            rid, RECEIVED_PHASE))
    c.c.h.time += timedelta(seconds=RECEIVED_SECONDS + 1)
    listed = c.ui.client.get("/api/instructions/models?thread_id=" + own["thread_id"])
    assert listed.status_code == 200, listed.text
    assert journal.get(rid, RECEIVED_PHASE)["native_receipt"]["request_id"] == rid
    assert any(thread["thread_id"] == rid for thread in listed.json()["composer"]["threads"])


def test_an_api_without_the_received_page_never_blocks_the_owner_conversation(native_console):
    c = native_console
    own = first(c)
    c.mutate[0] = lambda request, response: (
        httpx.Response(404, json={"error": {"code": "BEHAVIOR_NOT_FOUND"}}) if request.url.path.endswith("/received")
        else response)
    shown = c.ui.client.get("/api/instructions/models?thread_id=" + own["thread_id"])
    assert shown.status_code == 200, shown.text
    assert shown.json()["composer"]["received_error"]["code"]
    assert shown.json()["composer"]["can_submit"] is True
    accepted = send(c, own)
    assert accepted.status_code == 200, accepted.text


def test_native_cancel_and_delivered_are_explicit_and_not_repeated(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    stopped = post(c, "/api/instructions/" + draft["request_id"] + "/cancel")
    assert stopped.status_code == 200, stopped.text
    assert c.api.remote.records[draft["request_id"]].cancel_requested
    assert post(c, "/api/instructions/" + draft["request_id"] + "/cancel").status_code == 200
    assert sum(command.operation == "cancel" for _, command in c.api.remote.calls) == 1
    c.api.remote.completed(draft["request_id"])
    assert progress(c, draft["thread_id"]).status_code == 200
    assert not any(command.operation == "delivered" for _, command in c.api.remote.calls)
    for _ in range(2):
        acknowledged = post(c, "/api/instructions/" + draft["request_id"] + "/confirm-reply")
        assert acknowledged.status_code == 200, acknowledged.text
    assert sum(command.operation == "delivered" for _, command in c.api.remote.calls) == 1
    summary = c.ui.console.summary(draft["request_id"])
    assert summary["acknowledgment"]["state"] == "CONFIRMED"


def test_lost_native_delivery_ack_keeps_unknown_and_only_reads_back(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    assert progress(c, draft["thread_id"]).status_code == 200
    def lose(request, response):
        if request.url.path.endswith("/native-turns/delivered"):
            raise httpx.ReadTimeout("synthetic ack loss", request=request)
        return response
    c.mutate[0] = lose
    lost = post(c, "/api/instructions/" + draft["request_id"] + "/confirm-reply")
    assert lost.status_code == 503
    original = c.ui.console.journal.get(draft["request_id"], "acknowledge")
    assert original["state"] == "UNKNOWN"
    c.mutate[0] = lambda request, response: response
    assert progress(c, draft["thread_id"]).status_code == 200
    assert c.ui.console.native_instructions.receipts[draft["request_id"]].delivered
    assert post(c, "/api/instructions/" + draft["request_id"] + "/confirm-reply").status_code == 200
    assert sum(command.operation == "delivered" for _, command in c.api.remote.calls) == 1
    assert c.ui.console.journal.get(draft["request_id"], "acknowledge") == original


def test_native_source_and_file_options_use_captured_cid_without_old_job(native_console):
    from shared_context.behavior_agent_template import AgentTemplate, TemplateSettings, export_template
    from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
    c = native_console
    control, authority, human = c.c.control, c.c.authority, c.c.human
    original = export_template(control, authority, human)
    template = AgentTemplate.model_validate({**original.model_dump(), "settings": TemplateSettings(
        **{**original.settings.model_dump(), "text_attachment_profile": "fa-source-text-attachments-v1",
           "user_text_file_profile": "fa-user-text-files-v1"})})
    select_profile(control, authority, human, SelectConversationProfile(
        request_id="native-ui-inputs", expected_selection_id=None, template=template))
    draft = first(c)
    submitted = post(c, "/api/instructions/" + draft["request_id"] + "/submit", {
        "content": "Keep the input file.", "model": "gpt-4.1-mini",
        "files": [{"name": "original.txt", "text": "Raw user bytes\n"}],
    })
    assert submitted.status_code == 200, submitted.text
    cid = c.api.remote.records[draft["request_id"]].conversation_id
    files = c.ui.console.instruction_file_capability(draft["request_id"])
    assert files["conversation_id"] == cid and files["user_text_file_profile"] == "fa-user-text-files-v1"
    sources = c.ui.console.instruction_attachment_sources(draft["request_id"])
    assert sources["conversation_id"] == cid and sources["catalog"]["state"] == "metadata"
    assert sources["catalog"]["entries"] and control.records == {}
    download = c.ui.client.get("/api/instructions/" + draft["request_id"] + "/files/original.txt")
    assert download.status_code == 200 and download.content == b"Raw user bytes\n"


def test_unset_sidecar_keeps_fresh_threads_legacy(native_console):
    from web_test_console.bridge import Console
    c = native_console
    offline = Console(c.config, c.tmp / "not-enabled.sqlite3", backend=c.ui.console.backend,
        clock=lambda: c.c.h.time, token_provider=lambda: c.api.headers["Authorization"].removeprefix("Bearer "),
        instruction_connection=c.connection.model_copy(update={"state_profile": None}))
    offline.authenticate()
    current = offline.instructions.store.current()
    assert not offline.native_thread(current["thread_id"])
    assert "state_profile" not in offline.instructions.connection.model_dump(mode="json")
    assert "state_profile" not in offline.state()["composer"]


def test_partial_or_changed_native_result_is_not_shown(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    def change(request, response):
        if request.url.path.endswith("/result") and request.url.params.get("part") == "1":
            body = response.json()
            body["text"] += "changed"
            return httpx.Response(response.status_code, headers=dict(response.headers), json=body)
        return response
    c.mutate[0] = change
    result = progress(c, draft["thread_id"])
    assert result.status_code == 502 and result.json()["error"]["code"] == "NATIVE_RESULT_INCOMPLETE"
    assert draft["request_id"] not in c.ui.console.native_instructions.answers


def test_native_result_after_login_changes_is_not_published(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    def revoke(request, response):
        if request.url.path.endswith("/result"):
            c.ui.console.vault.clear()
        return response
    c.mutate[0] = revoke
    result = progress(c, draft["thread_id"])
    assert result.status_code == 401
    assert draft["request_id"] not in c.ui.console.native_instructions.answers


@pytest.mark.parametrize("fail_middle", [False, True])
def test_agent_history_is_not_human_text_and_no_local_fake_request_is_created(native_console, fail_middle):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    assert progress(c, draft["thread_id"]).status_code == 200
    original = c.api.remote.records[draft["request_id"]]
    # A subsequent publicly saved Agent turn in this CID, not a browser submission.
    receive = agent_receive()
    receive = type(receive).model_validate({
        **receive.model_dump(), "command": receive.command.model_copy(update={"conversation_id": original.conversation_id}),
        "source": receive.source.model_copy(update={"recipient_agent_id": original.agent_id}),
    })
    reference = original.result.model_copy(update={"turn_number": 2, "turn_key": "turn:2"})
    value = NativeReceipt(state_profile=receive.state_profile,
        agent_id=original.agent_id, hosted_session_id=original.hosted_session_id,
        conversation_id=original.conversation_id, request_id=receive.command.request_id,
        command=receive.command, capture=receive.capture, source=receive.source,
        submit_digest=receive.sha256, capture_digest=receive.capture.sha256,
        status="completed", turn_number=2, result=reference,
    )
    c.api.remote.records[value.request_id] = value
    other_input = agent_receive(rid="agent-later", cid=original.conversation_id, recipient=original.agent_id)
    later = NativeReceipt(state_profile=other_input.state_profile, agent_id=original.agent_id, hosted_session_id=original.hosted_session_id,
        conversation_id=original.conversation_id, request_id=other_input.command.request_id,
        command=other_input.command, capture=other_input.capture, source=other_input.source,
        submit_digest=other_input.sha256, capture_digest=other_input.capture.sha256,
        status="completed", turn_number=3,
        result=original.result.model_copy(update={"turn_number": 3, "turn_key": "turn:3"}))
    c.api.remote.records[later.request_id] = later
    from shared_context.behavior_native_turn_wire import NativeConversation, NativeHistoryPage, NativePublicTurn
    def handler(command):
        if command.operation == "read":
            return c.api.remote.records[command.request_id]
        if command.operation == "history":
            conv = NativeConversation(agent_id=original.agent_id, hosted_session_id=original.hosted_session_id,
                conversation_id=original.conversation_id, last_turn=3, last_request_id=later.request_id,
                capture=original.capture, capture_digest=original.capture_digest)
            return NativeHistoryPage(conversation=conv, turns=tuple(NativePublicTurn(
                conversation_id=entry.conversation_id, request_id=entry.request_id, turn_number=entry.turn_number,
                user_text=None, agent_text=entry.command.content, source=entry.source, result=entry.result)
                for entry in (value, later) if entry.turn_number > command.after_turn),
                next_after_turn=None)
        from shared_context.behavior_native_turn_wire import NativeResultPage
        split = len(c.api.remote.text) // 2
        result = c.api.remote.records[command.request_id].result
        return NativeResultPage(conversation_id=value.conversation_id, request_id=command.request_id,
            result=result, part=command.part, text=c.api.remote.text[:split] if command.part == 0 else c.api.remote.text[split:],
            next_part=1 if command.part == 0 else None, complete=False)
    c.api.remote.reply_override = handler
    lost = False
    def once(request, response):
        nonlocal lost
        if fail_middle and value.request_id + "/result" in request.url.path and not lost:
            lost = True
            raise httpx.ReadTimeout("synthetic middle Agent result unavailable once", request=request)
        return response
    c.mutate[0] = once
    observed = progress(c, draft["thread_id"])
    if fail_middle:
        assert observed.status_code == 503
        assert value.request_id not in c.ui.console.native_instructions.answers
        observed = progress(c, draft["thread_id"])
    assert observed.status_code == 200, observed.text
    turn = next(t for t in observed.json()["tasks"] if t["task_id"] == value.request_id)
    assert turn["instruction"] is None and turn["agent_text"] == value.command.content
    assert turn["reply"] is not None and turn["reply"]["data"]["text"] == c.api.remote.text
    assert turn["actions"]["read-reply"]["enabled"]
    result_reads = [command for _, command in c.api.remote.calls if command.operation == "result"]
    assert len([command for command in result_reads if command.request_id == value.request_id]) == (
        3 if fail_middle else 2)
    assert progress(c, draft["thread_id"]).status_code == 200
    assert [command for _, command in c.api.remote.calls if command.operation == "result"] == result_reads
    assert c.ui.console.instructions.store.command(value.request_id) is None
    assert sum(command.operation == "submit" for _, command in c.api.remote.calls) == 1


def test_attached_images_are_stored_first_and_the_send_carries_only_their_references(native_console):
    """B1 PA-08 slice 2: the workbench uploads one converted PNG, then submits name, digest and length only."""
    from test_user_images_native import encoded

    c = native_console
    draft = first(c)
    upload = "/api/instructions/" + draft["request_id"] + "/image-upload"
    stored = post(c, upload, {"name": "photo.png", "png_base64": encoded()})
    assert stored.status_code == 200, stored.text
    reference = stored.json()
    raw = __import__("base64").b64decode(encoded())
    assert reference == {"name": "photo.png", "sha256": hashlib.sha256(raw).hexdigest(),
                         "byte_length": len(raw), "media_type": "image/png"}
    assert [command.operation for _, command in c.api.remote.calls][-1] == "user-image-upload"
    for body, status, code in (
        ({"name": "photo.jpg", "png_base64": encoded()}, 400, "USER_IMAGE_INPUT_INVALID"),
        ({"name": "photo.png", "png_base64": "/9j/4AAQSkZJRgABAQAAAQABAAD/"}, 400, "USER_IMAGE_INPUT_INVALID"),
        ({"name": "photo.png", "png_base64": encoded(), "extra": "x"}, 403, "LOCAL_REQUEST_DENIED"),
    ):
        refused = post(c, upload, body)
        assert (refused.status_code, refused.json()["error"]["code"]) == (status, code)
    sent = post(c, "/api/instructions/" + draft["request_id"] + "/submit",
                {"content": "What is in this picture?", "model": "gpt-4.1-mini", "images": [reference]})
    assert sent.status_code == 200, sent.text
    original = c.api.remote.records[draft["request_id"]]
    assert [image.model_dump(mode="json") for image in original.command.images] == [reference]
    assert sent.json()["task"]["user_images"] == [reference]
    submitted = [json.loads(body) for method, path, body in c.calls
                 if method == "POST" and path.endswith("/native-turns/submit")]
    assert len(submitted) == 1 and "png_base64" not in json.dumps(submitted[0])


def test_a_declared_image_model_reaches_the_composer_and_others_stay_unchanged(native_console):
    """The picker depends on this flag: it must survive the real /models response and the workbench DTO."""
    c = native_console
    models = c.c.control.models.models
    before = c.ui.client.get("/api/instructions/models").json()["composer"]["models"]["models"]
    assert all("supports_image_input" not in item for item in before)
    models["gpt-4.1-mini"] = models["gpt-4.1-mini"].model_copy(update={"supports_image_input": True})
    after = c.ui.client.get("/api/instructions/models")
    assert after.status_code == 200, after.text
    listed = {item["id"]: item for item in after.json()["composer"]["models"]["models"]}
    assert listed["gpt-4.1-mini"]["supports_image_input"] is True
    assert "supports_image_input" not in listed["configured-second-model"]
    assert {key: value for key, value in listed["configured-second-model"].items()} == next(
        item for item in before if item["id"] == "configured-second-model")


def test_images_are_refused_locally_on_the_job_bound_lane_before_any_write(native_console, monkeypatch):
    from test_user_images_native import encoded

    c = native_console
    draft = first(c)
    # The job-bound lane: this request is not routed to a native thread.
    monkeypatch.setattr(c.ui.console, "native_request", lambda _rid: None)
    monkeypatch.setattr(c.ui.console.instructions, "prepare",
                        lambda *_a, **_k: pytest.fail("images must be refused before the job-bound lane writes"))
    before = len(c.calls)
    upload = post(c, "/api/instructions/" + draft["request_id"] + "/image-upload",
                  {"name": "photo.png", "png_base64": encoded()})
    assert upload.status_code == 409 and upload.json()["error"]["code"] == "USER_IMAGES_UNSUPPORTED"
    raw = __import__("base64").b64decode(encoded())
    reference = {"name": "photo.png", "sha256": hashlib.sha256(raw).hexdigest(), "byte_length": len(raw),
                 "media_type": "image/png"}
    sent = post(c, "/api/instructions/" + draft["request_id"] + "/submit",
                {"content": "Describe it.", "model": "gpt-4.1-mini", "images": [reference]})
    assert sent.status_code == 409 and sent.json()["error"]["code"] == "USER_IMAGES_UNSUPPORTED"
    assert not [call for call in c.calls[before:] if call[0] == "POST"]


def test_native_turn_system_prompt_is_a_read_of_the_api_rebuild_and_checks_scope(native_console):
    from shared_context.behavior_system_prompt_view import TurnSystemPrompt
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    assert progress(c, draft["thread_id"]).status_code == 200
    task = next(t for t in progress(c, draft["thread_id"]).json()["tasks"] if t["task_id"] == draft["request_id"])
    assert task["actions"]["read-system-prompt"]["enabled"] is True
    posts = len([call for call in c.calls if call[0] == "POST" and not call[1].endswith("/invocations")])
    path = "/api/instructions/" + draft["request_id"] + "/system-prompt"
    read = c.ui.client.get(path)
    assert read.status_code == 200, read.text
    assert read.headers["cache-control"] == "no-store"
    view = TurnSystemPrompt.model_validate_json(json.dumps(read.json()["system_prompt"]))
    assert view.request_id == draft["request_id"] and view.state == "unreconstructable"
    assert view.reasons == ("assembly_not_recorded",) and view.text is None
    assert len([call for call in c.calls if call[0] == "POST" and not call[1].endswith("/invocations")]) == posts
    assert c.ui.client.get(path, params={"x": "1"}).status_code == 403

    def other_conversation(request, response):
        if request.url.path.endswith("/system-prompt") and response.status_code == 200:
            data = response.json()
            data["conversation_id"] = "another-cid"
            return httpx.Response(200, content=json.dumps(data).encode(), request=request,
                                  headers={"content-type": "application/json"})
        return response

    c.mutate[0] = other_conversation
    moved = c.ui.client.get(path)
    assert moved.status_code == 502 and moved.json()["error"]["code"] == "NATIVE_STATE_SCOPE"
    c.mutate[0] = lambda request, response: (httpx.Response(404, json={"detail": "Not Found"})
                                             if request.url.path.endswith("/system-prompt") else response)
    old = c.ui.client.get(path)
    assert old.status_code == 409 and old.json()["error"]["code"] == "SYSTEM_PROMPT_UNAVAILABLE"


# 2026-10-05 slimming: dev v15 read the whole conversation every 11.5-14.8 s while a turn ran, and the reply came
# one more round (plus a 2.6 s reply read) after the turn had already finished.
def test_a_running_turn_is_read_alone_and_its_reply_arrives_with_the_finish(native_console, monkeypatch):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200

    def unavailable(rid, token, **_):  # this transport double has no public progress, like an older image
        raise ConsoleError("NATIVE_PROGRESS_UNCONFIRMED", 502)
    monkeypatch.setattr(c.ui.console.native_instructions, "read_progress", unavailable)
    status = lambda: c.ui.client.get("/api/instructions/" + draft["request_id"] + "/status")
    before = len(c.calls)
    running = status()
    assert running.status_code == 200, running.text
    task = next(t for t in running.json()["tasks"] if t["task_id"] == draft["request_id"])
    # Neal 2026-10-06: the workbench no longer decides whether the owner may write; the server answers the submit.
    assert task["reply"] is None and running.json()["composer"]["can_follow_up"] is True
    c.api.remote.completed(draft["request_id"])
    done = status()
    assert done.status_code == 200, done.text
    task = next(t for t in done.json()["tasks"] if t["task_id"] == draft["request_id"])
    assert task["reply"]["data"]["text"] == c.api.remote.text
    assert done.json()["composer"]["can_follow_up"] is True
    paths = [path for _, path, _ in c.calls[before:]]
    assert not any(path.endswith("/history") for path in paths), "never the whole conversation"
    assert not any(method == "POST" for method, _, _ in c.calls[before:])
    assert c.ui.client.get("/api/instructions/not-a-native-rid/status").status_code in {403, 409}


def test_after_a_bridge_restart_the_old_conversation_can_continue_at_once(request, monkeypatch):
    """fdbb on dev, 2026-10-05/06: after the workbench bridge restarted (every stage deploy does), the old conversation
    said it had no follow-up state yet and stayed blocked. The follow-up no longer depends on this process having read
    the conversation (Neal 2026-10-06 09:58): it is open at once, and one light read refreshes the latest turn."""
    reopened_loop = asyncio.new_event_loop()
    request.addfinalizer(lambda: reopened_loop.close()
                         if not reopened_loop.is_closed() and not reopened_loop.is_running() else None)
    c = request.getfixturevalue("native_console")
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    assert progress(c, draft["thread_id"]).status_code == 200
    # A new bridge process on the same journal: its memory of the conversation is empty.
    restarted = c.open(loop_factory=lambda: reopened_loop)

    def unavailable(rid, token, **_):
        raise ConsoleError("NATIVE_PROGRESS_UNCONFIRMED", 502)
    monkeypatch.setattr(restarted.console.native_instructions, "read_progress", unavailable)
    shown = restarted.client.get("/api/state?thread_id=" + draft["thread_id"]).json()["composer"]
    assert shown["observed_at"] is None and shown["can_follow_up"] is True and shown["follow_up_blocker"] is None
    before = len(c.calls)
    read = restarted.client.get("/api/instructions/" + draft["request_id"] + "/status")
    assert read.status_code == 200, read.text
    composer = read.json()["composer"]
    assert composer["observed_at"] is not None and composer["can_follow_up"] is True
    assert not any(path.endswith("/history") for _, path, _ in c.calls[before:])
    following = restarted.client.post("/api/instructions/" + draft["thread_id"] + "/next", json={},
                                      headers=restarted.headers)
    assert following.status_code == 200, following.text
    assert following.json()["composer"]["request_id"] != draft["request_id"]


# Neal 2026-10-06 09:58 「FA portal為什麼要控制user能否傳送訊息、能否續問?」: the workbench is a thin client. The
# owner may always write; the server answers each submit (runs, queues, busy_refused or refuses with a code). The only
# local rule left is that one RID is never sent twice.
def _submits(c, rid):
    return sum(command.operation == "submit" and command.command.request_id == rid for _, command in c.api.remote.calls)


def test_after_a_bridge_restart_the_owner_can_write_without_any_read_first(request):
    reopened_loop = asyncio.new_event_loop()
    request.addfinalizer(lambda: reopened_loop.close()
                         if not reopened_loop.is_closed() and not reopened_loop.is_running() else None)
    c = request.getfixturevalue("native_console")
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    assert progress(c, draft["thread_id"]).status_code == 200
    restarted = c.open(loop_factory=lambda: reopened_loop)
    before = len(c.calls)
    following = restarted.client.post("/api/instructions/" + draft["thread_id"] + "/next", json={},
                                      headers=restarted.headers)
    assert following.status_code == 200, following.text
    assert len(c.calls) == before, "a new RID is local; no read is needed first"
    nxt = following.json()["composer"]
    assert nxt["editable"] and nxt["can_submit"] and nxt["request_id"] != draft["request_id"]
    sent = restarted.client.post("/api/instructions/" + nxt["request_id"] + "/submit", headers=restarted.headers,
                                 json={"content": "Continue after the restart", "model": "gpt-4.1-mini"})
    assert sent.status_code == 200, sent.text
    assert _submits(c, draft["request_id"]) == 1 and _submits(c, nxt["request_id"]) == 1


def test_a_turn_that_ended_unknown_does_not_block_a_new_message_and_is_never_resent(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    original = c.api.remote.records[draft["request_id"]]
    c.api.remote.records[draft["request_id"]] = NativeReceipt.model_validate(
        {**original.model_dump(), "status": "unknown"})
    assert progress(c, draft["thread_id"]).status_code == 200
    shown = first(c)
    assert shown["can_follow_up"] is True and shown["follow_up_blocker"] is None
    nxt = post(c, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]
    assert nxt["request_id"] != draft["request_id"]
    assert send(c, nxt, text="A new message after an unknown turn").status_code == 200
    assert _submits(c, draft["request_id"]) == 1, "the unknown turn is not sent again"
    assert _submits(c, nxt["request_id"]) == 1
    assert c.api.remote.records[nxt["request_id"]].conversation_id == original.conversation_id


def test_a_later_message_whose_send_is_unknown_keeps_its_rid_while_a_new_message_goes_out(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    assert progress(c, draft["thread_id"]).status_code == 200
    second = post(c, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]
    c.api.remote.lose_submit = True
    lost = send(c, second, text="Second message, answer lost")
    assert lost.status_code == 503
    c.api.remote.lose_submit = False
    unknown = c.ui.console.journal.get(second["request_id"], "submit")
    assert unknown["state"] == "UNKNOWN"
    # The same RID is never sent again from the composer; only its own reconcile may read it back.
    assert send(c, second, text="Second message, answer lost").status_code == 409
    third = post(c, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]
    assert third["request_id"] not in {draft["request_id"], second["request_id"]}
    assert send(c, third, text="Third message").status_code == 200
    assert _submits(c, second["request_id"]) == 1, "the unknown send is not repeated"
    assert c.ui.console.journal.get(second["request_id"], "submit") == unknown


def test_while_the_first_message_is_unknown_the_next_one_waits_for_its_conversation(native_console):
    """Kept on purpose: with no receipt yet the conversation ID is unknown, so a follow-up would open a second
    conversation and lose the context. It is checked only at submit; the composer stays open."""
    c = native_console
    draft = first(c)
    c.api.remote.lose_submit = True
    assert send(c, draft).status_code == 503
    c.api.remote.lose_submit = False
    nxt = post(c, "/api/instructions/" + draft["thread_id"] + "/next")
    assert nxt.status_code == 200, nxt.text
    composer = nxt.json()["composer"]
    assert composer["editable"] and composer["can_submit"]
    refused = send(c, composer, text="Second before the first is known")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "INSTRUCTION_PENDING"
    assert _submits(c, composer["request_id"]) == 0 and _submits(c, draft["request_id"]) == 1


def test_a_refusal_the_server_marks_not_dispatched_is_final_and_the_conversation_still_reads(native_console):
    """#679 review: a submit refused before rid:<RID> exists (here the API's continuation_blocker 409) used to be stored
    as UNKNOWN, and every whole read then failed on NATIVE_RID_MISSING. The server now says not_dispatched (API line,
    released with this PR); a 5xx or uncertain answer never carries it and stays UNKNOWN."""
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    assert progress(c, draft["thread_id"]).status_code == 200
    second = post(c, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]

    def refused_before_create(request, response):
        if request.url.path.endswith("/native-turns/submit") and response.status_code == 409:
            body = response.json()
            body["error"].update(not_dispatched=True, request_id=second["request_id"])
            return httpx.Response(409, json=body, headers=response.headers)
        return response

    c.mutate[0] = refused_before_create
    c.api.remote.error = (409, "NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE")
    refused = send(c, second, text="Refused before it was created")
    c.api.remote.error, c.mutate[0] = None, (lambda request, response: response)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE"
    saved = c.ui.console.journal.get(second["request_id"], "submit")
    assert saved["state"] == "REFUSED" and saved["not_dispatched"] is True
    assert progress(c, draft["thread_id"]).status_code == 200, "the whole read does not ask Hosted for a RID it never made"
    shown = first(c)
    assert shown["request_id"] == second["request_id"]
    assert shown["follow_up_blocker"] == "NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE"
    task = next(item for item in c.ui.client.get("/api/state?thread_id=" + draft["thread_id"]).json()["tasks"]
                if item["task_id"] == second["request_id"])
    assert task["actions"]["submit-retry"]["enabled"] is False, "final: only a version refusal goes out again"
    sent = _submits(c, second["request_id"])
    assert post(c, "/api/tasks/" + second["request_id"] + "/submit-retry").status_code == 403
    third = post(c, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]
    assert third["request_id"] not in {draft["request_id"], second["request_id"]}
    assert send(c, third, text="Refused before it was created").status_code == 200
    assert c.api.remote.records[third["request_id"]].conversation_id == c.api.remote.records[draft["request_id"]].conversation_id
    assert _submits(c, second["request_id"]) == sent and second["request_id"] not in c.api.remote.records, \
        "the same text runs once, under the new RID"


@pytest.mark.parametrize("status,code", [(403, "ATTACHMENTS_DISABLED"), (503, "NATIVE_FILES_UNKNOWN")])
def test_a_not_dispatched_refusal_keeps_its_own_code_even_outside_the_native_list(native_console, status, code):
    """API #680 sends not_dispatched with attachment/source codes (e.g. ATTACHMENTS_DISABLED, a _SAFE_WORK_ERRORS code)
    that used to be folded into BACKEND_DENIED and stored as UNKNOWN, and with a 5xx whose uncertainty is the version
    follow, not this message (e.g. NATIVE_FILES_UNKNOWN). Both are final for this RID: the owner sends again later."""
    c = native_console
    draft = first(c)

    def refused_with(request, response):
        if request.url.path.endswith("/native-turns/submit") and response.status_code >= 400:
            return httpx.Response(status, json={"error": {"code": code, "message": "fixed",
                "automatic_retry": False, "not_dispatched": True, "request_id": draft["request_id"]}})
        return response

    c.mutate[0] = refused_with
    c.api.remote.error = (409, "NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE")
    refused = send(c, draft)
    c.api.remote.error, c.mutate[0] = None, (lambda request, response: response)
    assert refused.status_code == status and refused.json()["error"]["code"] == code
    assert refused.json()["error"]["message"]
    saved = c.ui.console.journal.get(draft["request_id"], "submit")
    assert saved["state"] == "REFUSED" and saved["code"] == code and saved["not_dispatched"] is True
    shown = c.ui.client.get("/api/state").json()
    assert shown["composer"]["can_follow_up"] is True and shown["composer"]["follow_up_blocker"] == code
    task = next(item for item in shown["tasks"] if item["task_id"] == draft["request_id"])
    assert task["actions"]["submit-retry"]["enabled"] is False


@pytest.mark.parametrize("status,code,flag,expected", [
    (503, "AGENT_VERSION_FOLLOWING", True, True),
    (409, "NATIVE_SESSION_CHANGED", True, True),
    (503, "NATIVE_EXECUTION_UNAVAILABLE", True, True),  # #903: an earlier SDK runtime still exiting
    (503, "AGENT_VERSION_FOLLOWING", False, False),
    (503, "NATIVE_FILES_UNKNOWN", True, False),
])
def test_server_following_is_exposed_only_for_proven_version_refusals(native_console, status, code, flag, expected):
    c = native_console
    draft = first(c)

    def refused_with(request, response):
        if request.url.path.endswith("/native-turns/submit") and response.status_code >= 400:
            body = {"error": {"code": code, "automatic_retry": False,
                "not_dispatched": True, "request_id": draft["request_id"]}}
            if flag:
                body["error"]["server_following"] = True
            return httpx.Response(status, json=body)
        return response

    c.mutate[0] = refused_with
    c.api.remote.error = (status, code)
    refused = send(c, draft)
    c.api.remote.error, c.mutate[0] = None, (lambda request, response: response)
    assert refused.status_code == status and refused.json()["error"]["code"] == code
    assert (refused.json()["error"].get("server_following") is True) is expected
    saved = c.ui.console.journal.get(draft["request_id"], "submit")
    assert "server_following" not in saved


@pytest.mark.parametrize("code", ["AGENT_VERSION_FOLLOWING", "NATIVE_EXECUTION_UNAVAILABLE"])  # #903: CLI still exiting
def test_server_following_same_rid_retry_uses_the_original_body(native_console, code):
    c = native_console
    draft = first(c)

    def refused_once(request, response):
        if request.url.path.endswith("/native-turns/submit") and response.status_code == 503:
            return httpx.Response(503, json={"error": {"code": code,
                "automatic_retry": False, "not_dispatched": True, "server_following": True,
                "request_id": draft["request_id"]}})
        return response

    c.mutate[0] = refused_once
    c.api.remote.error = (503, code)
    refused = send(c, draft, text="Original body for retry")
    assert refused.status_code == 503 and refused.json()["error"].get("server_following") is True
    c.api.remote.error, c.mutate[0] = None, (lambda request, response: response)
    retried = post(c, "/api/tasks/" + draft["request_id"] + "/submit-retry")
    assert retried.status_code == 200, retried.text
    bodies = [json.loads(raw) for method, path, raw in c.calls
              if method == "POST" and path.endswith("/native-turns/submit")]
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]
    assert bodies[1]["content"] == "Original body for retry"


@pytest.mark.parametrize("status,code,expected", [
    (403, "ATTACHMENTS_DISABLED", "這位助理目前不能附加來源，請移除附件後再送。"),
    (503, "NATIVE_FILES_UNKNOWN", "傳送結果尚未確認；保留原請求，不會自動重送。"),
])
def test_bridge_returns_plain_refusal_sentences(native_console, status, code, expected):
    c = native_console
    draft = first(c)

    def refused_with(request, response):
        if request.url.path.endswith("/native-turns/submit") and response.status_code >= 400:
            return httpx.Response(status, json={"error": {"code": code, "message": "technical",
                "automatic_retry": False, "not_dispatched": True, "request_id": draft["request_id"]}})
        return response

    c.mutate[0] = refused_with
    c.api.remote.error = (409, "NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE")
    refused = send(c, draft)
    c.api.remote.error, c.mutate[0] = None, (lambda request, response: response)
    assert refused.status_code == status
    assert refused.json()["error"]["message"] == expected



@pytest.mark.parametrize("status,code", [(503, "NATIVE_FILES_UNKNOWN"), (409, "NATIVE_SESSION_CHANGED")])
def test_unflagged_native_errors_do_not_claim_the_message_was_not_sent(native_console, status, code):
    c = native_console
    draft = first(c)
    def strip_dispatch_flag(request, response):
        if request.url.path.endswith("/native-turns/submit") and response.status_code == status:
            body = response.json()
            body.get("error", {}).pop("not_dispatched", None)
            body.get("error", {}).pop("request_id", None)
            return httpx.Response(status, json=body, headers=response.headers)
        return response

    c.mutate[0] = strip_dispatch_flag
    c.api.remote.error = (status, code)
    response = send(c, draft)
    c.api.remote.error, c.mutate[0] = None, (lambda request, response: response)
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert "沒有送出" not in response.json()["error"]["message"]
    assert "未確認" in response.json()["error"]["message"] or "核對" in response.json()["error"]["message"]
    saved = c.ui.console.journal.get(draft["request_id"], "submit")
    assert saved["state"] == "UNKNOWN" and saved["code"] == code and saved.get("not_dispatched") is None

def test_a_same_rid_resend_lost_before_hosted_created_it_does_not_break_later_reads(native_console):
    """#679 review round 3: after a version refusal (REFUSED), the same-RID resend got an unflagged 5xx (UNKNOWN) and
    Hosted never created the RID; every later whole read then failed on NATIVE_RID_MISSING."""
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    assert progress(c, draft["thread_id"]).status_code == 200
    second = post(c, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]
    c.api.remote.error = (503, "AGENT_VERSION_FOLLOWING")
    assert send(c, second, text="Sent while the Agent changes version").status_code == 503
    c.api.remote.error = (503, "NATIVE_STATE_STORE_UNKNOWN")
    assert post(c, "/api/tasks/" + second["request_id"] + "/submit-retry").status_code == 503
    c.api.remote.error = None
    shown = first(c)
    assert shown["request_id"] == second["request_id"] and shown["can_follow_up"] is True, "the resend is unknown: not held"
    sent = _submits(c, second["request_id"])
    third = post(c, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]
    assert send(c, third, text="A different message").status_code == 200
    read = progress(c, draft["thread_id"])
    assert read.status_code == 200, read.text
    task = next(item for item in read.json()["tasks"] if item["task_id"] == second["request_id"])
    assert task["error"] == {"code": "NATIVE_RID_MISSING"} and task["actions"]["submit-retry"]["enabled"] is False
    assert _submits(c, second["request_id"]) == sent, "the unknown resend is not repeated"


def test_a_version_refusal_holds_the_composer_until_its_same_rid_resend(native_console):
    """#657 resends a version refusal with the same RID; a new RID meanwhile would send the same text twice."""
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    assert progress(c, draft["thread_id"]).status_code == 200
    second = post(c, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]
    c.api.remote.error = (503, "AGENT_VERSION_FOLLOWING")
    assert send(c, second, text="Sent while the Agent changes version").status_code == 503
    c.api.remote.error = None
    shown = c.ui.client.get("/api/state?thread_id=" + draft["thread_id"]).json()
    assert shown["composer"]["request_id"] == second["request_id"] and shown["composer"]["can_follow_up"] is False
    task = next(item for item in shown["tasks"] if item["task_id"] == second["request_id"])
    assert task["actions"]["submit-retry"]["enabled"] is True


def test_a_send_lost_before_hosted_created_it_stays_unknown_and_the_conversation_still_reads(native_console):
    """#679 review round 2: a 5xx before rid:<RID> exists stays UNKNOWN (never resent); reading it back says missing,
    which is shown on that turn instead of failing every later whole read of the conversation."""
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])
    assert progress(c, draft["thread_id"]).status_code == 200
    second = post(c, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]
    c.api.remote.error = (503, "NATIVE_STATE_STORE_UNKNOWN")
    assert send(c, second, text="Lost before it was created").status_code == 503
    c.api.remote.error = None
    unknown = c.ui.console.journal.get(second["request_id"], "submit")
    assert unknown["state"] == "UNKNOWN"
    sent = _submits(c, second["request_id"])
    third = post(c, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]
    assert send(c, third, text="A different message").status_code == 200
    read = progress(c, draft["thread_id"])
    assert read.status_code == 200, read.text
    task = next(item for item in read.json()["tasks"] if item["task_id"] == second["request_id"])
    assert task["error"] == {"code": "NATIVE_RID_MISSING"}
    assert c.ui.console.journal.get(second["request_id"], "submit") == unknown
    assert _submits(c, second["request_id"]) == sent, "the unknown send is not repeated"


def test_a_blocker_the_server_reported_for_the_conversation_is_shown_and_holds_the_composer(native_console):
    """Thin client, not blind: when the conversation read says it cannot continue, that answer is shown."""
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    c.api.remote.completed(draft["request_id"])

    def blocked(request, response):
        if request.method == "GET" and request.url.path.endswith("/history") and response.status_code == 200:
            body = response.json()
            body["conversation"]["continuation_blocker"] = "NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE"
            headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
            return httpx.Response(200, json=body, headers=headers)
        return response

    c.mutate[0] = blocked
    assert progress(c, draft["thread_id"]).status_code == 200
    c.mutate[0] = lambda request, response: response
    shown = first(c)
    assert shown["can_follow_up"] is False and shown["can_submit"] is False
    assert shown["follow_up_blocker"] == "NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE"


# r33 dev (H15): a progress read timed out at 30 s on the API side, which answered 503 FOUNDRY_INVOCATION_UNKNOWN with
# `automatic_retry: true` (behavior_native_turn_api.py reply(read=True)); the next read was 200.
@pytest.mark.parametrize("code", ["NATIVE_BOOTSTRAP_UNKNOWN", "FOUNDRY_INVOCATION_UNKNOWN"])
def test_a_read_the_api_marks_retryable_is_passed_to_the_page_but_a_write_never_is(native_console, code):
    """stage v15 12:40: a transient 503 on a read stopped the page's polling. The API marks such a read
    `automatic_retry: true`; the bridge passes that on for GETs only, so an unknown write is still never resent."""
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    posts = []

    def transient(request, response):
        if request.method == "POST":
            posts.append(request.url.path)
        if request.method == "GET" and "/requests/" in request.url.path:
            return httpx.Response(503, json={"error": {"code": code, "message": "fixed",
                                                       "automatic_retry": True}})
        if request.method == "POST" and request.url.path.endswith("/native-turns/submit"):
            return httpx.Response(503, json={"error": {"code": code, "message": "fixed",
                                                       "automatic_retry": True}})
        return response

    c.mutate[0] = transient
    # The page's two polls: a running turn's /status and the whole conversation's progress.
    for path in ("/api/instructions/" + rid + "/status", "/api/instructions/progress?thread_id=" + draft["thread_id"]):
        read = c.ui.client.get(path)
        error = read.json()["error"]
        assert read.status_code == 503 and (error["code"], error["upstream_status"], error["automatic_retry"]) == (
            code, 503, True)
    assert posts == [], "a failed read sends nothing"
    c.mutate[0] = lambda request, response: response
    task = next(task for task in c.ui.client.get("/api/instructions/" + rid + "/status").json()["tasks"]
                if task["task_id"] == rid)
    assert task["error"] is None and task["submission"]["state"] == "CONFIRMED", "the turn is not marked failed"
    c.mutate[0] = transient
    second = post(c, "/api/instructions/" + draft["thread_id"] + "/next").json()["composer"]
    write = send(c, second, text="A write the API calls retryable")
    c.mutate[0] = lambda request, response: response
    assert write.status_code == 503 and write.json()["error"]["automatic_retry"] is False
    assert c.ui.console.journal.get(second["request_id"], "submit")["state"] == "UNKNOWN"


def test_a_running_turn_reads_its_own_approvals_only_while_a_tool_runs_or_progress_waits(native_console, monkeypatch):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    native, rid = c.ui.console.native_instructions, draft["request_id"]
    reads = []
    monkeypatch.setattr(native.hitl, "refresh", lambda thread, token, only=None: reads.append(("hitl", only)))
    monkeypatch.setattr(native.questions, "refresh", lambda thread, token, only=None: reads.append(("questions", only)))
    event = lambda seq, kind="stage": SimpleNamespace(seq=seq, kind=kind, tool_start_seq=seq if kind != "stage" else None,
                                                      tool_name="run_command")
    steps = iter([[event(1)], [event(1), event(2)], [event(1), event(2)], [event(1), event(2), event(3, "tool_started")]])
    monkeypatch.setattr(native, "read_progress", lambda rid, token, **_: SimpleNamespace(
        finished=False, progress=SimpleNamespace(events=next(steps))))
    both = [("hitl", rid), ("questions", rid)]
    for expected in ([], [], both, both):  # moving, moving, stalled (a wait), a tool in flight
        reads.clear()
        native.turn_status(rid, c.ui.console.vault.get())
        assert reads == expected


def test_the_read_that_sees_the_end_closes_this_turn_cards_once(native_console, monkeypatch):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    native, rid = c.ui.console.native_instructions, draft["request_id"]
    monkeypatch.setattr(native, "read_progress", lambda rid, token, **_: SimpleNamespace(
        finished=True, progress=SimpleNamespace(events=[])))
    reads = []
    monkeypatch.setattr(native.hitl, "refresh", lambda thread, token, only=None: reads.append(("hitl", only)))
    monkeypatch.setattr(native.questions, "refresh", lambda thread, token, only=None: reads.append(("questions", only)))
    c.api.remote.completed(rid)
    native.hitl.pages[rid] = {"thread_id": draft["thread_id"], "references": [], "attention": ["mid-turn card"], "closed": False}
    native.questions.pages[rid] = {"thread_id": draft["thread_id"], "attention": ["mid-turn question"]}
    native.turn_status(rid, c.ui.console.vault.get())
    assert reads == [("hitl", rid), ("questions", rid)]
    reads.clear()
    native.hitl.pages[rid]["closed"] = True
    native.questions.pages.pop(rid)
    native.turn_status(rid, c.ui.console.vault.get())
    assert reads == []


def _journal_writes(c, monkeypatch):
    journal, writes, commits = c.ui.console.journal, [], []
    for name in ("begin", "finish"):
        original = getattr(journal, name)
        monkeypatch.setattr(journal, name, lambda task, phase, *a, _o=original, _n=name, **k: (
            writes.append((_n, task, phase)), _o(task, phase, *a, **k))[1])
    # In remote mode each journal commit uploads every journal to Blob (remote.py _keep).
    monkeypatch.setattr(Journal, "after_commit", lambda path: commits.append(path))
    return writes, commits


# Audit #3 (2026-10-14): every /status poll (1 s, 3 s with push) rewrote the same native-read row; in remote mode each
# write re-packed and uploaded all journals, and a failed upload stops the replica mid-turn.
def test_polling_an_unchanged_receipt_writes_and_uploads_nothing(native_console, monkeypatch):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    status = lambda: c.ui.client.get("/api/instructions/" + rid + "/status")
    assert status().status_code == 200
    kept = c.ui.console.journal.get(rid, "native-read")
    writes, commits = _journal_writes(c, monkeypatch)
    for _ in range(3):
        assert status().status_code == 200
    assert writes == [] and commits == []
    assert c.ui.console.journal.get(rid, "native-read") == kept
    c.api.remote.completed(rid)
    assert status().status_code == 200
    assert [w for w in writes if w[2] == "native-read"] == [("finish", rid, "native-read")]
    assert c.ui.console.journal.get(rid, "native-read")["native_receipt"]["status"] == "completed"


# Audit #12 (2026-10-14): each poll re-read every message of the conversation from the journal (up to four SQLite reads
# and JSON parses per message) although the receipts were already held in memory.
def test_a_second_poll_reads_no_journal_rows_for_messages_already_held(native_console, monkeypatch):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid, thread = draft["request_id"], draft["thread_id"]
    c.api.remote.completed(rid)
    before = progress(c, thread)
    assert before.status_code == 200, before.text
    native, journal = c.ui.console.native_instructions, c.ui.console.journal
    held = (dict(native.receipts), dict(native.request_threads))
    reads, original = [], journal.get
    monkeypatch.setattr(journal, "get", lambda task, phase: (reads.append((task, phase)), original(task, phase))[1])
    native._saved(thread)
    assert reads == []
    assert (native.receipts, native.request_threads) == held
    after = progress(c, thread)
    assert after.status_code == 200, after.text
    assert after.json()["tasks"] == before.json()["tasks"] and after.json()["composer"] == before.json()["composer"]


def _single_page(c, rid):
    remote = c.api.remote
    remote.completed(rid)
    text = remote.single_text.encode("utf-8")
    original = remote.records[rid]
    remote.records[rid] = original.model_copy(update={"result": original.result.model_copy(update={
        "text_parts": 1, "text_bytes": len(text), "text_sha256": hashlib.sha256(text).hexdigest()})})
    return remote.single_text


def _turn_path(c, rid):
    return "/work-policy/v1/native-turns/conversations/" + c.api.remote.records[rid].conversation_id + "/requests/" + rid


# v17 dev 10-06 (A warm turns): after the finish push, the reply took three API reads in a row (receipt 0.61 s,
# progress 0.46 s, reply 0.70 s, about 2 s). The API's one /status read (#664) carries all three for a one-page reply.
def test_a_finished_one_page_reply_arrives_with_one_status_read(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    text = _single_page(c, rid)
    before = len(c.calls)
    read = c.ui.client.get("/api/instructions/" + rid + "/status")
    assert read.status_code == 200, read.text
    task = next(t for t in read.json()["tasks"] if t["task_id"] == rid)
    assert task["reply"]["data"]["text"] == text
    assert [call[:2] for call in c.calls[before:]] == [("GET", _turn_path(c, rid) + "/status")]


def test_a_longer_reply_is_still_read_page_by_page_after_the_status_read(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    c.api.remote.completed(rid)
    before = len(c.calls)
    read = c.ui.client.get("/api/instructions/" + rid + "/status")
    assert read.status_code == 200, read.text
    task = next(t for t in read.json()["tasks"] if t["task_id"] == rid)
    assert task["reply"]["data"]["text"] == c.api.remote.text
    path = _turn_path(c, rid)
    assert [call[:2] for call in c.calls[before:]] == [
        ("GET", path + "/status"), ("GET", path + "/result"), ("GET", path + "/result")]


def test_when_the_status_read_fails_the_separate_reads_still_deliver_the_reply(native_console, monkeypatch):
    """An image without the /status read, or a progress record it cannot return, fails that one read. The receipt and
    reply are then read on their own as before; nothing is sent again."""
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    c.api.remote.completed(rid)

    def unavailable(rid, token, **_):
        raise ConsoleError("NATIVE_PROGRESS_UNCONFIRMED", 502)
    monkeypatch.setattr(c.ui.console.native_instructions, "read_progress", unavailable)
    c.mutate[0] = lambda request, response: (
        httpx.Response(422, json={"error": {"code": "NATIVE_REQUEST_INVALID", "message": "older image"}})
        if request.url.path.endswith("/status") else response)
    before = len(c.calls)
    read = c.ui.client.get("/api/instructions/" + rid + "/status")
    assert read.status_code == 200, read.text
    task = next(t for t in read.json()["tasks"] if t["task_id"] == rid)
    assert task["reply"]["data"]["text"] == c.api.remote.text
    path = _turn_path(c, rid)
    assert [call[:2] for call in c.calls[before:]] == [
        ("GET", path + "/status"), ("GET", "/work-policy/v1/native-turns/requests/" + rid),
        ("GET", path + "/result"), ("GET", path + "/result")]


def test_a_status_reply_that_differs_from_its_receipt_is_never_shown(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    _single_page(c, rid)

    def altered(request, response):
        if request.url.path.endswith("/status") and response.status_code == 200:
            data = response.json()
            data["text"] = "\u5225\u7684\u5167\u5bb9"
            return httpx.Response(200, content=json.dumps(data).encode(), request=request,
                                  headers={"content-type": "application/json"})
        return response

    c.mutate[0] = altered
    read = c.ui.client.get("/api/instructions/" + rid + "/status")
    # The turn's receipt still reaches the page; the reply is not shown and its failure stays on that turn.
    assert read.status_code == 200, read.text
    task = next(t for t in read.json()["tasks"] if t["task_id"] == rid)
    assert task["reply"] is None and task["error"] == {"code": "NATIVE_RESULT_INCOMPLETE"}
    assert rid not in c.ui.console.native_instructions.answers


# stage/dev 2026-10-12: a finished turn kept showing 「回覆中，尚未確認保存。」 and 「正在準備」; 「更新回覆」 (this same
# /status read) changed nothing, and only 「更新工作狀態」 (POST /api/tasks/<rid>/status, a receipt-only refresh) showed
# it saved. The /status read had already seen the completed receipt, but a failed reply read failed the whole read, so
# the page never received the receipt. The reply's failure now stays on that turn; the receipt still reaches the page.
@pytest.mark.parametrize("failure", ["altered-single-page", "result-page-error"])
def test_a_reply_read_failure_still_returns_the_completed_receipt(native_console, failure):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    status = lambda: c.ui.client.get("/api/instructions/" + rid + "/status")
    running = status()
    assert running.status_code == 200, running.text
    assert next(t for t in running.json()["tasks"] if t["task_id"] == rid)["native_receipt"]["status"] != "completed"
    if failure == "altered-single-page":
        _single_page(c, rid)
    else:
        c.api.remote.completed(rid)

    def broken(request, response):
        if failure == "altered-single-page" and request.url.path.endswith("/status") and response.status_code == 200:
            data = response.json()
            data["text"] = "別的內容"
            return httpx.Response(200, content=json.dumps(data).encode(), request=request,
                                  headers={"content-type": "application/json"})
        if failure == "result-page-error" and request.url.path.endswith("/result"):
            return httpx.Response(503, json={"error": {"code": "NATIVE_STATE_STORE_UNKNOWN", "message": "fixed"}})
        return response

    c.mutate[0] = broken
    before = len(c.calls)
    read = status()
    assert read.status_code == 200, read.text
    task = next(t for t in read.json()["tasks"] if t["task_id"] == rid)
    assert task["native_receipt"]["status"] == "completed" and task["status"]["data"]["state"] == "completed"
    assert task["reply"] is None and task["error"]["code"]
    assert rid not in c.ui.console.native_instructions.answers
    assert all(method == "GET" for method, *_ in c.calls[before:]), "a read never sends anything"
    # Once the reply can be read, the same /status read brings it; nothing was sent again.
    c.mutate[0] = lambda request, response: response
    task = next(t for t in status().json()["tasks"] if t["task_id"] == rid)
    expected = c.api.remote.single_text if failure == "altered-single-page" else c.api.remote.text
    assert task["reply"]["data"]["text"] == expected and task["error"] is None
    assert all(method == "GET" for method, *_ in c.calls[before:])


# stage 2026-10-10 15:17: after a reload the page's first status read queued behind an earlier read of the same
# conversation that had not come back, so no read reached the API until 15:24. A read now waits for that lock only for
# a bounded time and answers as retryable, so the page backs off and reads again instead of hanging.
def read_behind_a_stuck_read(c, monkeypatch, thread, path):
    """Hold the conversation's progress lock like a read that never came back, then GET `path` from another thread.

    Fails when that GET is still waiting after 5 s (the unbounded wait); otherwise returns its response and the API
    calls made while the lock was held."""
    import threading
    import web_test_console.bridge as bridge

    # raising=False: a bridge without the bound must fail this by waiting, not by a missing name.
    monkeypatch.setattr(bridge, "PROGRESS_LOCK_WAIT_SECONDS", 0.2, raising=False)
    lock = c.ui.console.message_lock("progress:" + thread)
    held, release, answers = threading.Event(), threading.Event(), []

    def stuck():
        with lock:
            held.set()
            release.wait(30)

    holder = threading.Thread(target=stuck, daemon=True)
    holder.start()
    reader = threading.Thread(target=lambda: answers.append(c.ui.client.get(path)), daemon=True)
    try:
        assert held.wait(5)
        before = len(c.calls)
        reader.start()
        reader.join(5)
        waiting, calls = reader.is_alive(), c.calls[before:]
    finally:
        release.set()
        holder.join(5)
        if reader.ident is not None:
            reader.join(30)
    assert not waiting, "the read still waits behind the stuck read of the same conversation"
    return answers[0], calls


@pytest.mark.parametrize("route", ["status", "progress"])
def test_a_read_behind_a_stuck_read_of_the_same_conversation_answers_retryable(native_console, monkeypatch, route):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid, thread = draft["request_id"], draft["thread_id"]
    path = "/api/instructions/" + rid + "/status" if route == "status" else "/api/instructions/progress?thread_id=" + thread
    read, calls = read_behind_a_stuck_read(c, monkeypatch, thread, path)
    assert read.status_code == 409, read.text
    assert read.json()["error"]["code"] == "PROGRESS_READ_BUSY" and read.json()["error"]["automatic_retry"] is True
    assert calls == [], "nothing reaches the API while the other read holds the conversation"
    assert c.ui.client.get(path).status_code == 200


# stage 2026-10-10 15:13-15:24: 「停止這次回覆」 sent no stop to the API while the turn waited for an approval. The bridge
# first re-read the receipt (and continuations) and only then posted the stop, so a read that failed or hung kept the
# stop from going out. A turn this bridge already knows is running now posts its stop at once; the API answers it.
def test_stopping_a_known_running_turn_posts_the_stop_without_reading_first(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    assert c.ui.console.native_instructions.receipts[rid].status in {"accepted", "started"}
    c.mutate[0] = lambda request, response: (
        httpx.Response(503, json={"error": {"code": "NATIVE_STATE_STORE_UNKNOWN", "message": "fixed"}})
        if request.method == "GET" else response)
    before = len(c.calls)
    stop = post(c, "/api/instructions/" + rid + "/cancel")
    assert stop.status_code == 200, stop.text
    sent = [(method, path) for method, path, _ in c.calls[before:]]
    assert sent == [("POST", "/work-policy/v1/native-turns/cancel")], sent
    assert stop.json()["task"]["native_receipt"]["cancel_requested"] is True
    assert c.ui.console.journal.get(rid, "cancel")["state"] == "CONFIRMED"
    # Pressing it again sends nothing more (the original stop is kept, never resent).
    post(c, "/api/instructions/" + rid + "/cancel")
    assert [call for call in c.calls[before:] if call[0] == "POST"] == [c.calls[before]]


# The bridge still held the turn as running, but it had already finished: the API answers the stop with the original,
# unchanged receipt (native_turns.cancel). That answer is recorded as "already finished", never as an unknown stop.
def test_a_stop_for_a_turn_that_already_finished_records_the_api_answer(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    assert c.ui.console.native_instructions.receipts[rid].status in {"accepted", "started"}
    c.api.remote.completed(rid)
    finished = c.api.remote.records[rid]

    def unchanged(request, response):
        if request.method == "POST" and request.url.path.endswith("/native-turns/cancel"):
            c.api.remote.records[rid] = finished
            headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
            return httpx.Response(200, content=finished.model_dump_json().encode("utf-8"), headers=headers)
        return response

    c.mutate[0] = unchanged
    before = len(c.calls)
    stop = post(c, "/api/instructions/" + rid + "/cancel")
    assert stop.status_code == 200, stop.text
    assert [(method, path) for method, path, _ in c.calls[before:]] == [("POST", "/work-policy/v1/native-turns/cancel")]
    task = stop.json()["task"]
    assert task["native_receipt"]["status"] == "completed"
    assert task["native_receipt"]["cancel_requested"] is False
    assert task["cancellation"]["state"] == "CONFIRMED"
    assert task["cancellation"]["receipt"] == {"result": {"cancel_accepted": False, "status": "completed"}}
    assert c.ui.console.journal.get(rid, "cancel")["state"] == "CONFIRMED"


# #804 review: only a completed receipt means "the work already finished, no stop needed". An unchanged receipt in
# another state (busy_refused here) is not recorded as finished; the stop stays unknown and the page offers the check.
def test_a_stop_answered_with_a_busy_refused_receipt_is_not_recorded_as_finished(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    assert c.ui.console.native_instructions.receipts[rid].status in {"accepted", "started"}
    refused = c.api.remote.records[rid].model_copy(update={"status": "busy_refused", "failure_code": "NATIVE_TURN_BUSY"})

    def unchanged(request, response):
        if request.method == "POST" and request.url.path.endswith("/native-turns/cancel"):
            c.api.remote.records[rid] = refused
            headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
            return httpx.Response(200, content=refused.model_dump_json().encode("utf-8"), headers=headers)
        return response

    c.mutate[0] = unchanged
    before = len(c.calls)
    stop = post(c, "/api/instructions/" + rid + "/cancel")
    assert stop.status_code == 200, stop.text
    assert [(method, path) for method, path, _ in c.calls[before:]] == [("POST", "/work-policy/v1/native-turns/cancel")]
    task = stop.json()["task"]
    assert task["native_receipt"]["status"] == "busy_refused"
    assert task["native_receipt"]["cancel_requested"] is False
    assert task["cancellation"]["state"] == "UNKNOWN"
    assert "receipt" not in task["cancellation"]
    assert c.ui.console.journal.get(rid, "cancel")["state"] == "UNKNOWN"


# #813 review round 3: during a deploy window the API may invoke no session for a stop (the turn's session is not
# active). It says so (not_dispatched); the stop is recorded as not sent, and pressing it again sends the same stop.
def test_a_stop_the_api_sent_nowhere_is_recorded_refused_and_the_same_stop_is_sent_again(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    cid = c.ui.console.native_instructions.receipts[rid].conversation_id
    assert c.ui.console.native_instructions.receipts[rid].status in {"accepted", "started"}
    running = c.api.remote.records[rid]

    def nowhere(request, response):
        if request.method == "POST" and request.url.path.endswith("/native-turns/cancel"):
            c.api.remote.records[rid] = running  # The API invoked no session: the turn is unchanged.
            return httpx.Response(409, json={"error": {
                "code": "NATIVE_SESSION_CHANGED", "message": "fixed", "automatic_retry": False,
                "not_dispatched": True, "request_id": rid}})
        return response

    c.mutate[0] = nowhere
    before = len(c.calls)
    stop = post(c, "/api/instructions/" + rid + "/cancel")
    assert stop.status_code == 200, stop.text
    cancellation = stop.json()["task"]["cancellation"]
    assert (cancellation["state"], cancellation["code"], cancellation["not_dispatched"]) == (
        "REFUSED", "NATIVE_SESSION_CHANGED", True)
    # The next progress read still offers 「停止這次回覆」: the stop went nowhere.
    update = progress(c, draft["thread_id"])
    task = next(row for row in update.json()["tasks"] if row["task_id"] == rid)
    assert task["actions"]["cancel"]["enabled"] is True and task["cancellation"]["state"] == "REFUSED"
    first_stop = [call for call in c.calls[before:] if call[0] == "POST"]
    assert [(method, path) for method, path, _ in first_stop] == [("POST", "/work-policy/v1/native-turns/cancel")]
    assert json.loads(first_stop[0][2]) == {"operation": "cancel", "conversation_id": cid, "request_id": rid}
    # Pressed again (the API now reaches the turn's session): the same stop, same IDs, goes out once more.
    c.mutate[0] = lambda request, response: response
    again = post(c, "/api/instructions/" + rid + "/cancel")
    assert again.status_code == 200, again.text
    posts = [call for call in c.calls[before:] if call[0] == "POST"]
    assert [(method, path) for method, path, _ in posts] == [("POST", "/work-policy/v1/native-turns/cancel")] * 2
    assert posts[1][2] == posts[0][2]
    assert again.json()["task"]["native_receipt"]["cancel_requested"] is True
    assert c.ui.console.journal.get(rid, "cancel")["state"] == "CONFIRMED"


# #813 review round 4: the delivery acknowledgement the API sent nowhere is recorded REFUSED on its journal row, and
# pressing 「確認收到」 again sends the same NativeDelivered (same RID, CID and digest) on that same row.
def test_a_delivery_acknowledgement_the_api_sent_nowhere_is_recorded_refused_and_the_same_one_is_sent_again(
        native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    c.api.remote.completed(rid)
    assert progress(c, draft["thread_id"]).status_code == 200
    completed = c.api.remote.records[rid]
    assert not completed.delivered

    def nowhere(request, response):
        if request.method == "POST" and request.url.path.endswith("/native-turns/delivered"):
            c.api.remote.records[rid] = completed  # The API invoked no session: the turn is unchanged.
            return httpx.Response(409, json={"error": {
                "code": "NATIVE_SESSION_CHANGED", "message": "fixed", "automatic_retry": False,
                "not_dispatched": True, "request_id": rid}})
        return response

    c.mutate[0] = nowhere
    before = len(c.calls)
    refused = post(c, "/api/instructions/" + rid + "/confirm-reply")
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["not_dispatched"]) == ("NATIVE_SESSION_CHANGED", True)
    row = c.ui.console.journal.get(rid, "acknowledge")
    assert (row["state"], row["code"], row["not_dispatched"]) == ("REFUSED", "NATIVE_SESSION_CHANGED", True)
    first_ack = [call for call in c.calls[before:] if call[0] == "POST"]
    assert [(method, path) for method, path, _ in first_ack] == [("POST", "/work-policy/v1/native-turns/delivered")]
    assert not c.ui.console.native_instructions.receipts[rid].delivered
    # Pressed again (the API now reaches the turn's session): the same acknowledgement goes out once more.
    c.mutate[0] = lambda request, response: response
    again = post(c, "/api/instructions/" + rid + "/confirm-reply")
    assert again.status_code == 200, again.text
    posts = [call for call in c.calls[before:] if call[0] == "POST"]
    assert [(method, path) for method, path, _ in posts] == [("POST", "/work-policy/v1/native-turns/delivered")] * 2
    assert posts[1][2] == posts[0][2]
    assert json.loads(posts[0][2])["request_id"] == rid
    assert c.ui.console.native_instructions.receipts[rid].delivered
    final = c.ui.console.journal.get(rid, "acknowledge")
    assert final["state"] == "CONFIRMED" and final["intent"] == row["intent"]


def test_a_sign_in_failure_on_the_status_read_is_reported_without_other_reads(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    c.mutate[0] = lambda request, response: (
        httpx.Response(401, json={"error": {"code": "LOGIN_REQUIRED", "message": "fixed"}})
        if request.url.path.endswith("/status") else response)
    before = len(c.calls)
    read = c.ui.client.get("/api/instructions/" + rid + "/status")
    assert read.json()["error"]["code"] == "LOGIN_REQUIRED" and read.json()["error"]["upstream_status"] == 401, read.text
    assert [call[:2] for call in c.calls[before:]] == [("GET", _turn_path(c, rid) + "/status")]


def test_progress_brought_by_the_status_read_is_checked_like_its_own_read(native_console):
    """The public progress inside /status must name this turn's own session, like a /progress read."""
    from shared_context.behavior_assistant_wire import PublicProgress
    from shared_context.native_progress_wire import progress_snapshot

    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    rid = draft["request_id"]
    native = c.ui.console.native_instructions
    session = [None]

    def with_progress(request, response):
        if request.url.path.endswith("/status") and response.status_code == 200:
            data = response.json()
            receipt = c.api.remote.records[rid]
            snapshot = progress_snapshot(receipt, PublicProgress()).model_dump(mode="json")
            if session[0] is not None:
                original = snapshot["hosted_session_id"]
                snapshot["hosted_session_id"] = original[:-1] + ("1" if original[-1] != "1" else "2")
            data["progress"] = snapshot
            return httpx.Response(200, content=json.dumps(data).encode(), request=request,
                                  headers={"content-type": "application/json"})
        return response

    c.mutate[0] = with_progress
    assert c.ui.client.get("/api/instructions/" + rid + "/status").status_code == 200
    assert native.progress_snapshots[rid].request_id == rid and rid not in native.progress_errors
    native.progress_snapshots.pop(rid)
    session[0] = "another"
    assert c.ui.client.get("/api/instructions/" + rid + "/status").status_code == 200
    assert native.progress_errors[rid] == "NATIVE_STATE_SCOPE" and rid not in native.progress_snapshots


def test_responses_progress_rereads_only_the_latest_or_unfinished_turns(native_console, monkeypatch):
    native = native_console.ui.console.native_instructions
    read = []
    monkeypatch.setattr(native, "_saved", lambda thread: None)
    monkeypatch.setattr(native.store, "all", lambda thread: [
        {"request_id": rid} for rid in ("old", "relayed", "open", "new")])
    monkeypatch.setattr(native.journal, "get", lambda rid, phase: {"state": "CONFIRMED"} if phase == "submit" else None)
    monkeypatch.setattr(native, "_not_accepted", lambda rid: False)
    monkeypatch.setattr(native, "_read_original", lambda thread, rid, token: read.append(rid))
    monkeypatch.setattr(native, "_cid", lambda thread: None)
    monkeypatch.setattr(native, "_refresh_peer_replies", lambda thread, token: None)
    # Since 3397dc64 (#692) an older finished turn that still holds a queued relay is read again (its relay can change);
    # the receipts carry `queued_relay` like a real NativeReceipt.
    monkeypatch.setattr(native, "receipts", {"old": SimpleNamespace(status="completed", queued_relay=None),
                                             "relayed": SimpleNamespace(status="completed", queued_relay=object()),
                                             "open": SimpleNamespace(status="accepted", queued_relay=None),
                                             "new": SimpleNamespace(status="completed", queued_relay=None)})
    native._refresh_responses("thread", "token")
    assert read == ["relayed", "open", "new"]


def test_closed_finished_turn_approvals_are_not_reread(native_console, monkeypatch):
    hitl = native_console.ui.console.native_instructions.hitl
    read = []
    monkeypatch.setattr(hitl, "enabled", True)
    monkeypatch.setattr(hitl, "_read_turn", lambda thread, rid, original, token: read.append(rid))
    monkeypatch.setattr(hitl.native, "receipts", {rid: SimpleNamespace(status=status) for rid, status in (
        ("closed-done", "completed"), ("open-done", "completed"), ("running", "started"), ("other", "completed"))})
    monkeypatch.setattr(hitl.native, "request_threads", {"closed-done": "t", "open-done": "t", "running": "t", "other": "x"})
    monkeypatch.setattr(hitl, "pages", {"closed-done": {"closed": True}, "running": {"closed": True}})
    hitl.refresh("t", "token")
    assert read == ["open-done", "running"]
    read.clear()
    hitl.refresh("t", "token", only="running")
    assert read == ["running"]


def with_notice(notice):
    """The API's answers (a submit's receipt, a receipt read, a status read) as an API that adds ``notice`` sends them."""

    def mutate(request, response):
        path = request.url.path
        if response.status_code >= 300 or not (path.endswith("/submit") or "/requests/" in path):
            return response
        response.read()
        body = json.loads(response.content)
        receipt = body.get("receipt") if path.endswith("/status") else body
        if not isinstance(receipt, dict) or "submit_digest" not in receipt:
            return response
        receipt["environment_notice"] = notice
        headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
        return httpx.Response(response.status_code, headers=headers, content=json.dumps(body).encode("utf-8"))

    return mutate


def test_a_section_8_notice_on_the_submit_answer_is_projected_for_the_page(native_console):
    c = native_console
    draft = first(c)
    c.mutate[0] = with_notice({"kind": "environment_switched_without_files"})
    accepted = send(c, draft)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["task"]["native_receipt"]["environment_notice"] == {
        "kind": "environment_switched_without_files"}


@pytest.mark.parametrize("notice", [
    {"kind": "environment_later_kind", "to_version": "39"},  # a kind an API released after this workbench adds
    {"kind": "platform_version_changed", "from_version": "41", "to_version": "40", "reason": "later field"},
])
def test_a_notice_this_workbench_does_not_know_is_ignored_and_never_breaks_the_turn(native_console, notice):
    c = native_console
    draft = first(c)
    c.mutate[0] = with_notice(notice)
    accepted = send(c, draft)
    assert accepted.status_code == 200, accepted.text  # not 502 NATIVE_STATE_INVALID
    assert "environment_notice" not in accepted.json()["task"]["native_receipt"]
    c.api.remote.completed(draft["request_id"])
    update = progress(c, draft["thread_id"])  # receipt and status reads carry it too
    assert update.status_code == 200, update.text
    task = next(item for item in update.json()["tasks"] if item["task_id"] == draft["request_id"])
    assert task["native_receipt"]["status"] == "completed" and "environment_notice" not in task["native_receipt"]
    assert task["reply"]["data"]["text"] == c.api.remote.text


def test_a_receipt_whose_own_material_is_wrong_is_still_refused(native_console):
    c = native_console
    draft = first(c)

    def broken(request, response):
        if not request.url.path.endswith("/submit") or response.status_code >= 300:
            return response
        response.read()
        body = json.loads(response.content)
        body["environment_notice"] = {"kind": "environment_rolled_back"}
        body["unexpected"] = True  # tolerance covers the notice only; the receipt stays strict
        return httpx.Response(response.status_code, content=json.dumps(body).encode("utf-8"),
                              headers={"content-type": "application/json"})

    c.mutate[0] = broken
    refused = send(c, draft)
    assert refused.status_code == 502 and refused.json()["error"]["code"] == "NATIVE_STATE_INVALID"



def switched(c, monkeypatch, *, rollback=False):
    """The API's binding row holds an undelivered "moved without its work files" notice for the Agent (ADR-0009 §8);
    ``rollback``: that switch was phase 4d's rollback (已退回, without its work files)."""
    from conftest import InMemoryRepository
    from shared_context import native_binding
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView

    repository, target = InMemoryRepository(), c.api.remote.target
    monkeypatch.setattr(native_binding, "TableRepository", lambda _client: repository)
    store = native_binding.binding_store(c.api.composition)
    store.record_switch(target.agent_id, NativeEnvironmentView(
        agent_id=target.agent_id, request_id="follow-switch", state="ready", previous_hosted_session_id="older",
        previous_agent_version="40", hosted_session_id=target.session_id, hosted_agent_version=target.agent_version,
        files_migrated=False, files_skip_request_id="skip"), rollback=rollback)
    return lambda: store.read(target.agent_id).switch_notice


def test_this_bridge_says_it_knows_the_new_notice_kinds_and_gets_the_switch_notice_once(native_console, monkeypatch):
    c = native_console
    pending = switched(c, monkeypatch)
    seen = []
    c.before[0] = lambda request: seen.append(
        (request.headers.get("X-FA-Notice-Kinds"), request.headers.get("X-FA-Upgrade-Waiting"),
         request.headers.get("X-FA-Rollback-Notice"), request.headers.get("X-FA-Upgrade-Vetoed-Notice")))
    draft = first(c)
    accepted = send(c, draft)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["task"]["native_receipt"]["environment_notice"] == {
        "kind": "environment_switched_without_files"}
    # On every API request of this bridge: the phase 2c value an older API knows, and 6a (c)'s, 4d's and 4b-4's own
    # headers.
    assert seen and set(seen) == {("2", "1", "1", "1")}
    assert pending() is None  # shown once: the API cleared it after the answer was written


def test_a_bridge_deployed_before_the_new_kinds_never_gets_them_and_nothing_is_cleared(native_console, monkeypatch):
    """A deploy updates the API before the console: the older bridge sends no header and decodes strictly."""
    from web_test_console import native

    c = native_console
    pending = switched(c, monkeypatch)
    monkeypatch.setattr(native, "_without_unknown_notice", lambda _receipt: False)  # the older, strict decode

    def older(request):
        del request.headers["X-FA-Notice-Kinds"]
        del request.headers["X-FA-Upgrade-Waiting"]
        del request.headers["X-FA-Rollback-Notice"]
        del request.headers["X-FA-Upgrade-Vetoed-Notice"]

    c.before[0] = older
    draft = first(c)
    accepted = send(c, draft)
    assert accepted.status_code == 200, accepted.text  # not 502 NATIVE_STATE_INVALID
    assert "environment_notice" not in accepted.json()["task"]["native_receipt"]
    assert pending() == "without_files"  # still pending for the updated console's next answer


def vetoed(c, monkeypatch):
    """The API's binding row holds an undelivered 4b-4 "沒有更新" notice: U3's third failure vetoed 42 for this Agent."""
    pending = switched(c, monkeypatch)
    from shared_context import native_binding

    store, target = native_binding.binding_store(c.api.composition), c.api.remote.target
    store.clear_notice(target.agent_id, store.read(target.agent_id).notice())
    store.record_veto(target.agent_id, hosted_session_id=target.session_id, agent_version="42", notice=True)
    assert pending() == "vetoed"
    return pending


def test_this_bridge_passes_the_vetoed_notice_to_the_page_once(native_console, monkeypatch):
    c = native_console
    pending = vetoed(c, monkeypatch)
    accepted = send(c, first(c))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["task"]["native_receipt"]["environment_notice"] == {"kind": "environment_upgrade_vetoed"}
    assert pending() is None  # shown once: the API cleared it after the answer was written


def test_a_bridge_before_4b4_on_this_api_never_gets_the_vetoed_notice_and_nothing_is_cleared(native_console, monkeypatch):
    """The API may go live before the console: a bridge before 4b-4 sends no ``X-FA-Upgrade-Vetoed-Notice`` and has no
    sentence for 沒有更新, so it is not sent (the drift notice only, none pending here) and stays for the next console."""
    c = native_console
    pending = vetoed(c, monkeypatch)

    def before_4b4(request):  # a 4d bridge
        del request.headers["X-FA-Upgrade-Vetoed-Notice"]

    c.before[0] = before_4b4
    accepted = send(c, first(c))
    assert accepted.status_code == 200, accepted.text
    assert "environment_notice" not in accepted.json()["task"]["native_receipt"]
    assert pending() == "vetoed"


def test_this_bridge_on_an_api_before_4b4_still_gets_and_clears_the_switch_notice(native_console, monkeypatch):
    """The console may go live before the API. That API ignores the 4b-4 header (it never writes 沒有更新), so the
    switch notice is shown and cleared as in phase 2c."""
    from shared_context import behavior_native_turn_api as api

    c = native_console
    pending = switched(c, monkeypatch)
    monkeypatch.setattr(api, "_knows_vetoed_notice", lambda request: False)  # no such check before 4b-4
    accepted = send(c, first(c))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["task"]["native_receipt"]["environment_notice"] == {
        "kind": "environment_switched_without_files"}
    assert pending() is None


def test_this_bridge_passes_the_rollback_notice_to_the_page_once(native_console, monkeypatch):
    c = native_console
    pending = switched(c, monkeypatch, rollback=True)
    assert pending() == "rolled_back_without_files"
    accepted = send(c, first(c))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["task"]["native_receipt"]["environment_notice"] == {
        "kind": "environment_rolled_back", "without_files": True}
    assert pending() is None  # shown once: the API cleared it after the answer was written


def test_a_bridge_before_4d_on_this_api_never_gets_the_rollback_notice_and_nothing_is_cleared(native_console, monkeypatch):
    """The API may go live before the console: a bridge before 4d sends no ``X-FA-Rollback-Notice`` and has no
    sentence for 已退回, so it is not sent (the drift notice only, none pending here) and stays for the next console."""
    c = native_console
    pending = switched(c, monkeypatch, rollback=True)

    def before_4d(request):  # a phase 2c or 6a (c) bridge
        del request.headers["X-FA-Rollback-Notice"]

    c.before[0] = before_4d
    accepted = send(c, first(c))
    assert accepted.status_code == 200, accepted.text
    assert "environment_notice" not in accepted.json()["task"]["native_receipt"]
    assert pending() == "rolled_back_without_files"


def test_this_bridge_on_an_api_before_4d_still_gets_and_clears_the_switch_notice(native_console, monkeypatch):
    """The console may go live before the API. That API knows exactly ``X-FA-Notice-Kinds: 2`` and ignores the 4d
    header (it never writes 已退回), so the switch notice is shown and cleared as in phase 2c."""
    from shared_context import behavior_native_turn_api as api

    c = native_console
    pending = switched(c, monkeypatch)
    monkeypatch.setattr(api, "_knows_rollback_notice", lambda request: False)  # no such check before 4d
    monkeypatch.setattr(api, "_notice_level", lambda request: 2 if request.headers.getlist(
        "X-FA-Notice-Kinds") == ["2"] else 0)  # the phase 2c API's exact check
    accepted = send(c, first(c))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["task"]["native_receipt"]["environment_notice"] == {
        "kind": "environment_switched_without_files"}
    assert pending() is None


def test_this_bridge_on_an_api_before_6a_c_still_gets_and_clears_the_switch_notice(native_console, monkeypatch):
    """#908 review P2: the console may go live before the API. That API knows exactly ``X-FA-Notice-Kinds: 2`` and
    ignores the 6a (c) header, so the switch notice is shown and cleared as in phase 2c."""
    from shared_context import behavior_native_turn_api as api

    c = native_console
    pending = switched(c, monkeypatch)
    monkeypatch.setattr(api, "_notice_level", lambda request: 2 if request.headers.getlist(
        "X-FA-Notice-Kinds") == ["2"] else 0)  # the phase 2c API's exact check
    accepted = send(c, first(c))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["task"]["native_receipt"]["environment_notice"] == {
        "kind": "environment_switched_without_files"}
    assert pending() is None


def waiting(c, monkeypatch):
    """Phase 6a (c): the API's binding row holds the pending "有新版本 v42" note and no slot notice."""
    from shared_context import native_binding
    from shared_context.native_binding import NativeBindingIdleUpgrade

    switched(c, monkeypatch)
    store, agent_id = native_binding.binding_store(c.api.composition), c.api.remote.target.agent_id
    note = NativeBindingIdleUpgrade(target_version="42", deferred_since="2026-10-13T00:00:00+00:00", waiting="pending")
    store._update(agent_id, lambda row: row.model_copy(update={"switch_notice": None, "idle_upgrade": note}))
    return lambda: store.read(agent_id).idle_upgrade.waiting


def test_this_bridge_passes_the_waiting_note_to_the_page_once(native_console, monkeypatch):
    c = native_console
    state = waiting(c, monkeypatch)
    accepted = send(c, first(c))
    assert accepted.status_code == 200, accepted.text
    receipt = accepted.json()["task"]["native_receipt"]
    assert receipt["upgrade_waiting_version"] == "42" and "environment_notice" not in receipt
    assert state() == "shown"


def test_a_phase_2c_bridge_on_this_api_never_gets_the_waiting_note_and_nothing_is_cleared(native_console, monkeypatch):
    """#908 review P2: the API may go live before the console. A phase 2c bridge sends ``X-FA-Notice-Kinds: 2`` only
    and decodes the receipt strictly, so the note is not sent to it and stays pending."""
    c = native_console
    state = waiting(c, monkeypatch)

    def phase_2c(request):
        del request.headers["X-FA-Upgrade-Waiting"]

    c.before[0] = phase_2c
    accepted = send(c, first(c))
    assert accepted.status_code == 200, accepted.text
    assert "upgrade_waiting_version" not in accepted.json()["task"]["native_receipt"]
    assert state() == "pending"
