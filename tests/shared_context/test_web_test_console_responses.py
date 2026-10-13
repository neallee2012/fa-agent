"""N6 console lane contracts; local journal only, before the published N6-2 HTTP integration."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from web_test_console.adapter import ConsoleError, canonical
from web_test_console.bridge import Journal
from web_test_console.instructions import InstructionStore
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_native_turn_wire import NativeCancel
from test_native_responses_api import responses_api
from test_native_stream_api import platform
from test_web_test_console_native import (
    first, instruction_models, jwk_client, native_api, native_console, progress, read_behind_a_stuck_read, send,
    signing_key, synthetic_standing_v1,
)

RESPONSES_PROFILE = "foundry-responses-v1"


@pytest.fixture
def responses_console(native_console, responses_api, monkeypatch):
    from conftest import InMemoryRepository
    from shared_context.m3_artifacts import ArtifactService
    from shared_context.m3_skills import ApprovedSkillSource
    from shared_context.m3_store import InMemoryArtifactStore
    from shared_context.service import ContextService

    authority = native_console.api.composition.assistant.authority
    original = authority.artifacts
    context = ContextService(InMemoryRepository(), original.context.memory, original.context.auth)
    artifacts = ArtifactService(context, InMemoryArtifactStore(context.auth.tenant_id), original.approval)
    monkeypatch.setattr(authority, "artifacts", artifacts)
    monkeypatch.setattr(authority, "skills", ApprovedSkillSource(artifacts))
    return native_console, responses_api


@pytest.fixture
def peer_return_console(request):
    loop = asyncio.new_event_loop()
    try:
        c, env = request.getfixturevalue("responses_console")
        c.peer_reopen_loop = loop
        yield c, env
    finally:
        if not loop.is_running() and not loop.is_closed():
            loop.close()


def test_responses_lane_extends_old_journal_without_rebinding_original_threads(tmp_path):
    journal = Journal(tmp_path / "n6-lanes.sqlite3", SimpleNamespace(sha256="a" * 64))
    clock = lambda: datetime(2026, 10, 2, tzinfo=timezone.utc)
    with journal.connect() as db:
        db.execute("""CREATE TABLE instruction_lanes (
            thread_id TEXT PRIMARY KEY, state_profile TEXT
            CHECK(state_profile IS NULL OR state_profile='foundry-native-v1'))""")
        db.execute("""CREATE TABLE instructions (
            request_id TEXT PRIMARY KEY, content TEXT, created_at TEXT NOT NULL, thread_id TEXT NOT NULL)""")
        db.execute("CREATE TABLE instruction_threads (thread_id TEXT PRIMARY KEY, draft_request_id TEXT NOT NULL)")
        db.execute("CREATE TABLE instruction_commands (request_id TEXT PRIMARY KEY, command TEXT NOT NULL)")
        for rid, profile in (("old-table-rid", None), ("old-native-rid", "foundry-native-v1")):
            body = {"request_id": rid, "conversation_id": rid + "-cid", "content": "Original retained input."}
            db.execute("INSERT INTO instructions VALUES (?,?,?,?)", (rid, body["content"], clock().isoformat(), rid))
            db.execute("INSERT INTO instruction_threads VALUES (?,?)", (rid, rid))
            db.execute("INSERT INTO instruction_commands VALUES (?,?)", (rid, canonical(body).decode()))
            db.execute("INSERT INTO instruction_lanes VALUES (?,?)", (rid, profile))
    journal.begin("old-native-rid", "submit", {
        "state_profile": "foundry-native-v1", "body_sha256": "b" * 64, "created_at": clock().isoformat()})
    original_unknown = journal.get("old-native-rid", "submit")
    store = InstructionStore(journal, clock)
    assert store.lane("old-table-rid", preferred=RESPONSES_PROFILE) is None
    assert store.lane("old-native-rid", preferred=RESPONSES_PROFILE) == "foundry-native-v1"
    assert store.has_fixed_lane("old-table-rid") and store.has_fixed_lane("old-native-rid")
    draft = store.new_thread()
    assert not store.has_fixed_lane(draft["thread_id"])
    command = {"request_id": draft["request_id"], "content": "New Responses input."}
    store.prepare(command, freeze=True, state_profile=RESPONSES_PROFILE)
    restarted = InstructionStore(journal, clock)
    assert restarted.lane(draft["thread_id"], preferred=None) == RESPONSES_PROFILE
    assert restarted.has_fixed_lane(draft["thread_id"])
    assert restarted.command(draft["request_id"]) == command
    assert restarted.command("old-table-rid")["conversation_id"] == "old-table-rid-cid"
    assert restarted.command("old-native-rid")["conversation_id"] == "old-native-rid-cid"
    assert journal.get("old-native-rid", "submit") == original_unknown


@pytest.mark.parametrize("prior", ["command", "unknown-intent"])
def test_responses_preference_never_upgrades_an_unpinned_original_submission(tmp_path, prior):
    journal = Journal(tmp_path / "old-submission.sqlite3", SimpleNamespace(sha256="a" * 64))
    clock = lambda: datetime(2026, 10, 2, tzinfo=timezone.utc)
    store = InstructionStore(journal, clock)
    draft = store.current()
    body = {"request_id": draft["request_id"], "content": "Original input."}
    if prior == "command":
        with journal.connect() as db:
            db.execute("INSERT INTO instruction_commands VALUES (?,?)", (draft["request_id"], canonical(body).decode()))
    else:
        journal.begin(draft["request_id"], "submit", {"created_at": clock().isoformat()})
    original_command, original_intent = store.command(draft["request_id"]), journal.get(draft["request_id"], "submit")
    assert store.lane(draft["thread_id"], preferred=RESPONSES_PROFILE) is None
    assert store.has_fixed_lane(draft["thread_id"])
    with pytest.raises(ConsoleError) as refused:
        store.prepare(body, freeze=True, state_profile=RESPONSES_PROFILE)
    assert refused.value.code == "INSTRUCTION_ROUTE_CHANGED"
    assert store.command(draft["request_id"]) == original_command
    assert journal.get(draft["request_id"], "submit") == original_intent


def test_responses_pin_and_complete_original_command_survive_a_changed_preference(tmp_path):
    journal = Journal(tmp_path / "responses-original.sqlite3", SimpleNamespace(sha256="a" * 64))
    clock = lambda: datetime(2026, 10, 2, tzinfo=timezone.utc)
    store = InstructionStore(journal, clock)
    draft = store.current()
    command = {"request_id": draft["request_id"], "content": "Original input.", "conversation_id": "original-cid"}
    store.prepare(command, freeze=True, state_profile=RESPONSES_PROFILE)
    for preference in (None, "foundry-native-v1", RESPONSES_PROFILE):
        assert store.lane(draft["thread_id"], preferred=preference) == RESPONSES_PROFILE
    with pytest.raises(ConsoleError):
        store.prepare({**command, "content": "Changed input."}, state_profile=RESPONSES_PROFILE)
    with pytest.raises(ConsoleError):
        store.prepare(command, state_profile="foundry-native-v1")
    assert store.command(draft["request_id"]) == command


def test_pinned_invocations_login_and_models_do_not_depend_on_template_availability(native_console):
    c = native_console
    draft = first(c)
    assert send(c, draft).status_code == 200

    def unavailable(request, response):
        if request.url.path.endswith("/agent-template"):
            return httpx.Response(503, json={"error": {"code": "OFFLINE_TEMPLATE_UNAVAILABLE"}})
        return response

    c.mutate[0] = unavailable
    before = len(c.calls)
    authenticated = c.ui.client.post("/api/authenticate", json={}, headers=c.ui.headers)
    assert authenticated.status_code == 200, authenticated.text
    models = c.ui.client.get("/api/instructions/models")
    assert models.status_code == 200, models.text
    assert models.json()["composer"]["state_profile"] == "foundry-native-v1"
    assert not any(path.endswith("/agent-template") for _method, path, _body in c.calls[before:])

    fresh = c.ui.console.instructions.store.new_thread()
    before = len(c.calls)
    refused = send(c, fresh)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert refused.json()["error"]["upstream_status"] == 503
    assert c.calls[before:][0][:2] == ("GET", "/work-policy/v1/agent-template")
    assert all(method == "GET" for method, _path, _body in c.calls[before:])
    assert c.ui.console.instructions.store.command(fresh["request_id"]) is None


def test_responses_adapter_uses_only_server_selected_submit_reconcile_and_cancel_routes(native_console, responses_api):
    from web_test_console.responses import ResponsesAdapter

    console = native_console.ui.console
    adapter = ResponsesAdapter(console.config, console.backend, api_prefix=console.api_prefix)
    token = console.vault.get()
    command = InstructionSubmit(request_id="original-response-rid", content="Keep this original request.")
    before = len(native_console.calls)
    receipt = adapter.submit(command, token)
    assert receipt.state_profile == RESPONSES_PROFILE and receipt.command == command
    resolved = adapter.reconcile(command, token)
    assert resolved.submit_digest == receipt.submit_digest and resolved.conversation_id == receipt.conversation_id
    stopped = adapter.change(NativeCancel(
        request_id=receipt.request_id, conversation_id=receipt.conversation_id), token)
    assert stopped.cancel_requested and stopped.status == "unknown"
    posts = [path for method, path, _ in native_console.calls[before:] if method == "POST"]
    assert posts == [
        "/work-policy/v1/responses-turns/submit",
        "/work-policy/v1/responses-turns/reconcile",
        "/work-policy/v1/responses-turns/cancel",
    ]
    assert adapter.receipt(receipt.request_id, token).request_id == receipt.request_id
    assert native_console.calls[-1][1] == "/work-policy/v1/native-turns/requests/" + receipt.request_id


def test_response_history_page_uses_platform_cursor_without_invoking_the_vm_or_guessing_fa_turns(
    native_console, responses_api,
):
    from web_test_console.responses import ResponsesAdapter

    env = responses_api
    env.state["history_items"] = [
        {"type": "message", "id": "item-user", "role": "user",
         "content": [{"type": "input_text", "text": "Platform input, not proof of a human sender."}]},
        {"type": "message", "id": "item-progress", "role": "assistant", "phase": "commentary",
         "content": [{"type": "output_text", "text": "Temporary tool status"}]},
        {"type": "message", "id": "item-final", "role": "assistant", "phase": "final_answer", "status": "completed",
         "content": [{"type": "output_text", "text": "Original saved answer."}]},
        {"type": "message", "id": "item-unconfirmed", "role": "assistant",
         "content": [{"type": "output_text", "text": "Do not present this as the original answer."}]},
    ]
    console = native_console.ui.console
    adapter = ResponsesAdapter(console.config, console.backend, api_prefix=console.api_prefix)
    before = len(env.calls)
    page = adapter.history_page("conversation", "conv-original", console.vault.get(), after="item-before", limit=20)
    assert [message.item_id for message in page.messages] == ["item-user", "item-final", "item-unconfirmed"]
    assert page.messages[1].text == "Original saved answer."
    assert page.messages[2].state == "unconfirmed" and page.messages[2].text is None
    assert page.fallback == "native-result"
    calls = env.calls[before:]
    assert len(calls) == 2 and all(call.method == "GET" and "/protocols/openai/" in call.url.path for call in calls)
    assert calls[-1].url.params["after"] == "item-before" and calls[-1].url.params["limit"] == "20"
    assert not any("turn_number" in message.model_dump() or "request_id" in message.model_dump() for message in page.messages)


def test_response_chain_history_returns_the_previous_selector_without_scanning_it(native_console, responses_api):
    from web_test_console.responses import ResponsesAdapter

    env = responses_api
    env.state["response_snapshot"] = {
        "previous_response_id": "resp-before",
        "output": [{"type": "message", "id": "saved-final", "role": "assistant", "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Saved result only."}]}],
    }
    console = native_console.ui.console
    adapter = ResponsesAdapter(console.config, console.backend, api_prefix=console.api_prefix)
    before = len(env.calls)
    page = adapter.history_page("response", "resp-original", console.vault.get())
    assert page.previous_response_id == "resp-before" and page.next_after is None
    assert page.messages[0].text == "Saved result only."
    calls = env.calls[before:]
    assert len(calls) == 2 and all(call.method == "GET" for call in calls)
    assert not any("resp-before" in call.url.path for call in calls)


def test_platform_scope_denial_never_falls_back_to_invocations_or_a_different_history_route(native_console, responses_api):
    from web_test_console.responses import ResponsesAdapter

    env = responses_api
    env.state["history_metadata"] = {"fa_agent_id": "another-agent", "fa_team_id": "another-team"}
    console = native_console.ui.console
    adapter = ResponsesAdapter(console.config, console.backend, api_prefix=console.api_prefix)
    before = len(env.calls)
    with pytest.raises(ConsoleError) as denied:
        adapter.history_page("conversation", "conv-original", console.vault.get())
    assert denied.value.code == "NATIVE_STATE_SCOPE"
    assert len(env.calls[before:]) == 1 and "/conversations/" in env.calls[-1].url.path


@pytest.mark.parametrize("selector", [{"after": "../another"}, {"limit": 101}, {"limit": True}])
def test_platform_history_cursor_validation_precedes_any_backend_request(native_console, responses_api, selector):
    from web_test_console.responses import ResponsesAdapter

    console = native_console.ui.console
    adapter = ResponsesAdapter(console.config, console.backend, api_prefix=console.api_prefix)
    before = len(native_console.calls)
    with pytest.raises(ConsoleError) as denied:
        adapter.history_page("conversation", "conv-original", console.vault.get(), **selector)
    assert denied.value.code == "ORIGIN_DENIED" and len(native_console.calls) == before


def test_console_pins_the_server_selected_responses_lane_not_the_sidecar(responses_console):
    c, _env = responses_console
    draft = first(c)
    assert c.connection.state_profile == "foundry-native-v1"
    response = send(c, draft, "Use the server-selected lane.")
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["state_profile"] == RESPONSES_PROFILE
    assert c.ui.console.instructions.store.lane(draft["thread_id"]) == RESPONSES_PROFILE
    assert c.ui.console.journal.get(draft["request_id"], "submit")["intent"]["state_profile"] == RESPONSES_PROFILE
    assert any(method == "POST" and path.endswith("/responses-turns/submit") for method, path, _ in c.calls)
    assert not any(method == "POST" and path.endswith("/native-turns/submit") for method, path, _ in c.calls)


def _completed_follow_up(c, draft):
    from shared_context.behavior_native_turn_wire import NativeReceipt, NativeResultReference

    original = c.api.remote.records[draft["request_id"]]
    raw = c.api.remote.text.encode()
    result = NativeResultReference(
        turn_number=1, turn_key="turn:1", result_digest="1" * 64, text_sha256=hashlib.sha256(raw).hexdigest(),
        text_bytes=len(raw), text_parts=2, checkpoint_sha256="a" * 64)
    c.api.remote.records[draft["request_id"]] = NativeReceipt.model_validate_json(original.model_copy(update={
        "status": "completed", "turn_number": 1, "result": result, "response_id": "resp-1",
        "dispatch": None}).model_dump_json())
    assert progress(c, draft["thread_id"]).status_code == 200
    following = c.ui.client.post("/api/instructions/" + draft["thread_id"] + "/next", json={}, headers=c.ui.headers)
    assert following.status_code == 200, following.text
    return following.json()["composer"]


# v17 dev 10-06 (A warm turns): every follow-up first read /agent-template/options (about 0.9 s) although its
# conversation's lane was already pinned by the first message. Only that first message reads the template now; the
# server answers each follow-up's submit.
def test_a_follow_up_in_a_pinned_responses_conversation_goes_straight_to_its_submit(responses_console):
    c, _env = responses_console
    draft = first(c)
    before = len(c.calls)
    assert send(c, draft, "The first message picks the lane.").status_code == 200
    assert any(path.endswith("/agent-template") for _method, path, _body in c.calls[before:])
    latest = _completed_follow_up(c, draft)
    before = len(c.calls)
    sent = send(c, latest, "A follow-up in the same conversation.")
    assert sent.status_code == 200, sent.text
    assert sent.json()["task"]["state_profile"] == RESPONSES_PROFILE
    assert not any("/agent-template" in path for _method, path, _body in c.calls[before:])
    assert c.calls[before][:2] == ("POST", "/work-policy/v1/responses-turns/submit")


def test_a_pinned_responses_follow_up_takes_the_server_refusal_as_its_answer(responses_console, monkeypatch):
    """With no read before it, a follow-up the server cannot run now is refused by its own submit (not_dispatched):
    the RID is kept as not sent, it is never sent again, and the owner can write again under a new RID. The options
    read now say Responses is off too, so before this change the bridge stopped it locally and never asked."""
    from shared_context import behavior_native_responses_api
    from shared_context.errors import Problem

    c, _env = responses_console
    draft = first(c)
    assert send(c, draft, "The first message picks the lane.").status_code == 200
    latest = _completed_follow_up(c, draft)

    def responses_off(*_args, **_kwargs):
        raise Problem(409, "NATIVE_TURNS_DISABLED", "OFFLINE selected build disabled Responses")

    monkeypatch.setattr(behavior_native_responses_api, "current_hosted_version", responses_off)

    def refused(request, response):
        if request.url.path.endswith("/responses-turns/submit") and response.status_code >= 400:
            return httpx.Response(403, json={"error": {"code": "NATIVE_TURNS_DISABLED", "message": "fixed",
                "automatic_retry": False, "not_dispatched": True, "request_id": latest["request_id"]}})
        return response

    c.mutate[0] = refused
    c.api.remote.error = (403, "NATIVE_TURNS_DISABLED")
    answer = send(c, latest, "Not runnable now.")
    c.api.remote.error, c.mutate[0] = None, (lambda request, response: response)
    assert answer.status_code == 403 and answer.json()["error"]["code"] == "NATIVE_TURNS_DISABLED"
    saved = c.ui.console.journal.get(latest["request_id"], "submit")
    assert saved["state"] == "REFUSED" and saved["not_dispatched"] is True
    retried = c.ui.client.post("/api/tasks/" + latest["request_id"] + "/submit-retry", json={}, headers=c.ui.headers)
    assert retried.status_code == 403
    assert sum(method == "POST" and path.endswith("/responses-turns/submit") for method, path, _ in c.calls) == 2
    shown = c.ui.client.get("/api/state").json()["composer"]
    assert shown["can_follow_up"] is True


@pytest.mark.parametrize("outcome", ["not_delivered", "unknown"])
def test_responses_refreshes_an_older_queued_delivery_after_the_owner_keeps_chatting(responses_console, outcome):
    from shared_context.behavior_native_agent_wire import NativePeerRequest, NativeRelaySend
    from shared_context.behavior_native_turn_wire import NativeReceipt, NativeResultReference
    from shared_context.native_relay_delivery import reference_for

    c, env = responses_console
    draft = first(c)
    assert send(c, draft, "Queue an original message.").status_code == 200

    def complete(rid, number):
        original = c.api.remote.records[rid]
        raw = c.api.remote.text.encode()
        result = NativeResultReference(
            turn_number=number, turn_key=f"turn:{number}", result_digest=str(number) * 64,
            text_sha256=hashlib.sha256(raw).hexdigest(), text_bytes=len(raw), text_parts=2,
            checkpoint_sha256="a" * 64)
        value = NativeReceipt.model_validate_json(original.model_copy(update={
            "status": "completed", "turn_number": number, "result": result,
            "response_id": f"resp-{number}", "dispatch": None}).model_dump_json())
        c.api.remote.records[rid] = value
        return value

    original = complete(draft["request_id"], 1)
    queued = reference_for(original, NativeRelaySend(
        conversation_id=original.conversation_id, request_id=original.request_id, tool_call_id="old-queued-call",
        message=NativePeerRequest(recipient_agent_id="peer", kind="request", content="Same original brief.")))
    c.api.remote.records[original.request_id] = original.model_copy(update={"queued_relay": queued})
    assert progress(c, draft["thread_id"]).status_code == 200
    next_ = c.ui.client.post("/api/instructions/" + draft["thread_id"] + "/next", json={}, headers=c.ui.headers)
    assert next_.status_code == 200, next_.text
    latest = next_.json()["composer"]
    assert send(c, latest, "An independent next message.").status_code == 200
    complete(latest["request_id"], 2)
    env.state["holder"] = latest["request_id"]
    assert progress(c, draft["thread_id"]).status_code == 200
    c.api.remote.records[original.request_id] = original.model_copy(update={
        "queued_relay": queued.model_copy(update={"status": outcome, "reason": "NATIVE_RELAY_WAIT_EXPIRED"})})
    before = len(c.calls)
    observed = progress(c, draft["thread_id"])
    assert observed.status_code == 200, observed.text
    shown = next(task for task in observed.json()["tasks"] if task["task_id"] == original.request_id)
    assert shown["native_receipt"]["queued_relay"]["status"] == outcome
    assert shown["native_receipt"]["status"] == "completed"
    assert all(method == "GET" for method, _path, _body in c.calls[before:])
    assert any(path.endswith("/requests/" + original.request_id) for _method, path, _body in c.calls[before:])


def test_disabled_server_responses_selection_never_falls_back_or_freezes_an_unsent_draft(
    responses_console, monkeypatch,
):
    from shared_context import behavior_native_responses_api

    c, _env = responses_console
    monkeypatch.setattr(behavior_native_responses_api, "RESPONSES_CONVERSATION_MODE", None)
    draft = first(c)
    before = len(c.calls)
    response = send(c, draft, "Do not silently send through Invocations.")
    assert response.status_code in {403, 409, 503}
    assert response.json()["error"]["code"] == "NATIVE_TURNS_DISABLED"
    assert c.ui.console.instructions.store.command(draft["request_id"]) is None
    assert c.ui.console.journal.get(draft["request_id"], "submit") is None
    assert not any(method == "POST" for method, _path, _ in c.calls[before:])


def test_existing_invocations_cid_keeps_its_route_when_the_server_selects_responses(native_console, request):
    c = native_console
    first_draft = first(c)
    assert send(c, first_draft, "Original Invocations conversation.").status_code == 200
    old = c.api.remote.records[first_draft["request_id"]]
    c.api.remote.completed(first_draft["request_id"])
    assert progress(c, first_draft["thread_id"]).status_code == 200
    request.getfixturevalue("responses_console")
    next_draft = c.ui.client.post("/api/instructions/" + first_draft["thread_id"] + "/next",
                                 json={}, headers=c.ui.headers)
    assert next_draft.status_code == 200, next_draft.text
    before = len(c.calls)
    following = send(c, next_draft.json()["composer"], "Continue original Invocations CID.")
    assert following.status_code == 200, following.text
    assert following.json()["task"]["state_profile"] == "foundry-native-v1"
    assert following.json()["task"]["admission"]["conversation_id"] == old.conversation_id
    paths = [path for method, path, _body in c.calls[before:] if method == "POST"]
    assert paths == ["/work-policy/v1/native-turns/submit"]


def test_response_progress_offers_an_explicit_auto_reconcile_post_for_the_original_request(responses_console):
    c, env = responses_console
    draft = first(c)
    env.state["response_failure"] = "timeout"
    # The API replies once Hosted has the accepted receipt; its Responses POST runs after that reply.
    initial = send(c, draft, "Only this original request may continue.")
    assert initial.status_code == 200, initial.text
    submitted = c.ui.console.journal.get(draft["request_id"], "submit")
    original = c.api.remote.records[draft["request_id"]]
    assert original.status == "accepted" and original.response_id is None
    assert original.dispatch.state == "needs_response"
    env.state["response_failure"] = None
    c.c.h.time += timedelta(seconds=2)
    before = len(c.calls)
    observed = progress(c, draft["thread_id"])
    assert observed.status_code == 200, observed.text
    assert not any(method == "POST" for method, _path, _body in c.calls[before:]), "progress GET remains read-only"
    task = next(task for task in observed.json()["tasks"] if task["task_id"] == draft["request_id"])
    assert task["actions"]["submit-reconcile"]["enabled"]
    reconciled = c.ui.client.post("/api/tasks/" + draft["request_id"] + "/submit-reconcile",
                                 json={}, headers=c.ui.headers)
    assert reconciled.status_code == 200, reconciled.text
    posts = [(path, body) for method, path, body in c.calls[before:] if method == "POST"]
    assert len(posts) == 1 and posts[0][0].endswith("/responses-turns/reconcile")
    assert json.loads(posts[0][1]) == original.command.model_dump(mode="json")
    assert c.ui.console.journal.get(draft["request_id"], "submit") == submitted
    assert c.api.remote.records[draft["request_id"]].dispatch.state == "already_queued"
    c.c.h.time += timedelta(seconds=65)
    again = len(c.calls)
    assert progress(c, draft["thread_id"]).status_code == 200
    assert not any(method == "POST" for method, _path, _ in c.calls[again:])


def test_response_reconcile_error_budget_survives_controller_reload_and_keeps_the_rid_cancellable(responses_console):
    from web_test_console.native import NativeInstructionControl

    c, env = responses_console
    draft = first(c)
    env.state["response_failure"] = 502
    assert send(c, draft, "Keep the same request after real errors.").status_code == 200
    submitted = c.ui.console.journal.get(draft["request_id"], "submit")
    for _ in range(12):
        c.c.h.time += timedelta(seconds=65)
        response = progress(c, draft["thread_id"])
        assert response.status_code == 200, response.text
        task = next(task for task in response.json()["tasks"] if task["task_id"] == draft["request_id"])
        if task["actions"]["submit-reconcile"]["enabled"]:
            result = c.ui.client.post("/api/tasks/" + draft["request_id"] + "/submit-reconcile",
                                     json={}, headers=c.ui.headers)
            assert result.status_code == 200, result.text
            task = result.json()["task"]
    posts = [call for call in env.calls if call.method == "POST" and call.url.path.endswith("/responses")]
    assert len(posts) == 11, "the one after-reply POST and the client's finite reconcile budget of ten"
    bodies = [json.loads(call.content) for call in posts]
    # RTT B: only the owner's after-reply send streams; every reconcile resends the same material without it.
    assert [body.pop("stream") for body in bodies] == [True] + [False] * 10
    assert all(body == bodies[0] for body in bodies)
    value = c.api.remote.records[draft["request_id"]]
    assert value.status == "accepted" and value.response_id is None and value.failure_code is None
    assert task["response_reconciliation"]["state"] == "stopped"
    assert task["response_reconciliation"]["failures"] == 10
    assert task["actions"]["cancel"]["enabled"]
    assert not task["actions"]["submit-retry"]["enabled"]
    c.ui.console.native_instructions = NativeInstructionControl(c.ui.console, c.ui.console.instructions)
    before = len(c.calls)
    assert progress(c, draft["thread_id"]).status_code == 200
    assert not any(method == "POST" for method, _path, _ in c.calls[before:])
    assert c.ui.console.journal.get(draft["request_id"], "submit") == submitted


@pytest.mark.parametrize("status", [401, 403])
def test_initial_responses_permission_refusal_never_becomes_an_automatic_retry(responses_console, status):
    c, env = responses_console
    draft = first(c)
    env.state["response_failure"] = status
    response = send(c, draft, "Retain the original permission refusal.")
    assert response.status_code == 200, response.text
    original = c.api.remote.records[draft["request_id"]]
    assert original.status == "accepted"
    # The after-reply POST's refusal is only logged; the first reconcile surfaces it and stops for good.
    c.c.h.time += timedelta(seconds=2)
    observed = progress(c, draft["thread_id"])
    task = next(item for item in observed.json()["tasks"] if item["task_id"] == draft["request_id"])
    assert task["actions"]["submit-reconcile"]["enabled"]
    refused = c.ui.client.post("/api/tasks/" + draft["request_id"] + "/submit-reconcile",
                              json={}, headers=c.ui.headers)
    assert refused.status_code == 200, refused.text
    assert refused.json()["task"]["response_reconciliation"]["state"] == "stopped"
    c.c.h.time += timedelta(seconds=65)
    before = len(c.calls)
    observed = progress(c, draft["thread_id"])
    assert observed.status_code == 200, observed.text
    task = next(item for item in observed.json()["tasks"] if item["task_id"] == draft["request_id"])
    assert not task["actions"]["submit-reconcile"]["enabled"]
    assert task["actions"]["cancel"]["enabled"]
    assert not any(method == "POST" for method, _path, _body in c.calls[before:])
    assert sum(call.method == "POST" and call.url.path.endswith("/responses") for call in env.calls) == 2
    assert c.api.remote.records[draft["request_id"]].status == "accepted"


@pytest.mark.parametrize("status", ["started", "unknown", "completed"])
def test_responses_auto_reconcile_never_replays_a_started_or_terminal_receipt(responses_console, status):
    from shared_context.behavior_native_turn_wire import NativeReceipt

    c, env = responses_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    original = c.api.remote.records[draft["request_id"]]
    update = {"status": status, "dispatch": None}
    if status in {"started", "completed"}:
        update.update(response_id="resp-original", turn_number=1)
    if status == "completed":
        from shared_context.behavior_native_turn_wire import NativeResultReference
        update["result"] = NativeResultReference(
            turn_number=1, turn_key="turn:1", result_digest="a" * 64, text_sha256="b" * 64,
            text_bytes=4, text_parts=1, checkpoint_sha256="c" * 64)
    if status == "unknown":
        update["failure_code"] = "NATIVE_TURN_UNKNOWN"
    c.api.remote.records[draft["request_id"]] = NativeReceipt.model_validate_json(
        original.model_copy(update=update).model_dump_json())
    c.c.h.time += timedelta(seconds=65)
    before = len(c.calls)
    response = progress(c, draft["thread_id"])
    assert response.status_code == 200, response.text
    assert not any(method == "POST" for method, _path, _ in c.calls[before:])


def test_responses_progress_never_reads_platform_history_until_it_is_requested(responses_console):
    c, env = responses_console
    draft = first(c)
    assert send(c, draft, "Original local input.").status_code == 200
    env.state["history_items"] = [
        {"id": "platform-input", "type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "Platform input has no FA speaker proof."}]},
        {"id": "platform-final", "type": "message", "role": "assistant", "phase": "final_answer",
         "content": [{"type": "output_text", "text": "Original saved platform answer."}]},
    ]
    before = len(c.calls)
    response = progress(c, draft["thread_id"])
    assert response.status_code == 200, response.text
    # P04: the platform chain is a support view, read only when someone opens it.
    assert response.json()["composer"]["platform_history"]["page"] is None
    assert not any(path.endswith("/history") for _method, path, _body in c.calls[before:])
    before = len(c.calls)
    opened = c.ui.client.get("/api/instructions/" + draft["thread_id"] + "/platform-history")
    assert opened.status_code == 200, opened.text
    history = opened.json()["platform_history"]
    assert history["page"]["source_kind"] == "conversation"
    assert [item["item_id"] for item in history["page"]["messages"]] == ["platform-input", "platform-final"]
    assert history["error"] is None
    assert any("/responses-turns/conversations/" in path and path.endswith("/history")
               for _method, path, _body in c.calls[before:])
    assert not any("/native-turns/" in path and path.endswith(("/history", "/result"))
                   for _method, path, _body in c.calls[before:])
    assert not any(method == "POST" for method, _path, _body in c.calls[before:])
    again = progress(c, draft["thread_id"])
    assert again.json()["composer"]["platform_history"]["page"]["source_kind"] == "conversation"


# The platform-history read takes the same per-conversation progress lock as the status and progress reads, so it is
# bounded the same way: behind a read that never came back it answers 409 PROGRESS_READ_BUSY (automatic_retry), and
# nothing reaches the API, instead of hanging until that read returns.
def test_a_platform_history_read_behind_a_stuck_read_answers_retryable(responses_console, monkeypatch):
    c, _env = responses_console
    draft = first(c)
    assert send(c, draft, "Original local input.").status_code == 200
    path = "/api/instructions/" + draft["thread_id"] + "/platform-history"
    read, calls = read_behind_a_stuck_read(c, monkeypatch, draft["thread_id"], path)
    assert read.status_code == 409, read.text
    assert read.json()["error"]["code"] == "PROGRESS_READ_BUSY" and read.json()["error"]["automatic_retry"] is True
    assert calls == [], "nothing reaches the API while the other read holds the conversation"
    assert c.ui.client.get(path).status_code == 200


def test_responses_peer_return_is_read_on_the_original_thread_without_console_restart(peer_return_console, monkeypatch):
    from shared_context.behavior_native_agent_wire import NativeRelayAcceptance
    from shared_context.behavior_native_turn_wire import (
        NATIVE_REQUEST, NativeAgentCommand, NativeAgentOrigin, NativeAgentReceive, NativeConversation,
        NativeReceipt, NativeResultReference, NativeTurnAddress,
    )

    c, _env = peer_return_console
    draft = first(c)
    assert send(c, draft, "Ask the peer to reply.").status_code == 200
    original = c.api.remote.records[draft["request_id"]]
    c.api.remote.text = "A handling of the peer's original result."

    def complete(value, number, **fields):
        raw = c.api.remote.text.encode()
        reference = NativeResultReference(
            turn_number=number, turn_key=f"turn:{number}", result_digest=str(number) * 64,
            text_sha256=hashlib.sha256(raw).hexdigest(), text_bytes=len(raw), text_parts=2,
            checkpoint_sha256="a" * 64)
        return NativeReceipt.model_validate_json(value.model_copy(update={
            "status": "completed", "turn_number": number, "result": reference,
            "response_id": f"resp-{number}", "dispatch": None, "failure_code": None, **fields}).model_dump_json())

    delegated = NativeRelayAcceptance(
        message_id="peer-return", recipient_agent_id="peer-b", conversation_id="peer-cid",
        submit_digest="b" * 64, status="accepted")
    original = complete(original, 1, delegation=delegated)
    c.api.remote.records[original.request_id] = original
    latest = [original]
    transport = c.api.foundry.http._transport

    def original_reads(request):
        if request.url.path.endswith("/invocations"):
            command = NATIVE_REQUEST.validate_json(request.content)
            if command.operation == "conversation":
                value = latest[0]
                conversation = NativeConversation(
                    state_profile=RESPONSES_PROFILE, agent_id=value.agent_id,
                    hosted_session_id=value.hosted_session_id, execution_hosted_session_id=value.hosted_session_id,
                    conversation_id=value.conversation_id, last_turn=value.turn_number,
                    last_request_id=value.request_id, capture=value.capture, capture_digest=value.capture_digest)
                return httpx.Response(200, content=conversation.model_dump_json(), headers={
                    "content-type": "application/json", "x-agent-session-id": value.hosted_session_id,
                    "x-agent-invocation-id": "offline-conversation-read"})
        return transport.handle_request(request)

    monkeypatch.setattr(c.api.foundry.http, "_transport", httpx.MockTransport(original_reads))
    waiting = progress(c, draft["thread_id"])
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["composer"].get("awaiting_agent_reply") is True
    address = NativeTurnAddress(agent_id=original.agent_id, conversation_id=original.conversation_id,
                                request_id=original.request_id)
    peer_reference = NativeResultReference(
        turn_number=1, turn_key="turn:1", result_digest="c" * 64,
        text_sha256=hashlib.sha256("收到".encode()).hexdigest(), text_bytes=len("收到".encode()),
        text_parts=1, checkpoint_sha256="d" * 64)
    source = NativeAgentOrigin(
        kind="agent_result", message_kind="result", message_id=delegated.message_id,
        sender=NativeTurnAddress(agent_id=delegated.recipient_agent_id,
                                conversation_id=delegated.conversation_id, request_id=delegated.message_id),
        recipient_agent_id=original.agent_id, root_id="original-root", root=address, hop=0,
        in_reply_to=delegated.message_id, result=peer_reference, continuation=address)
    received = NativeAgentReceive(
        state_profile=RESPONSES_PROFILE, command=NativeAgentCommand(
            request_id=delegated.message_id, conversation_id=original.conversation_id, content="收到"),
        capture=original.capture, source=source)
    returned = complete(NativeReceipt(
        state_profile=RESPONSES_PROFILE, agent_id=original.agent_id, hosted_session_id=original.hosted_session_id,
        conversation_id=original.conversation_id, request_id=received.command.request_id,
        command=received.command, capture=received.capture, source=source,
        submit_digest=received.sha256, capture_digest=received.capture.sha256, status="unknown",
        failure_code="NATIVE_TURN_UNKNOWN"), 2)
    matches = c.ui.console.native_instructions._peer_return_matches
    assert matches(original, returned)
    assert matches(original, returned.model_copy(update={"source": source.model_copy(update={"continuation": None})}))
    assert not matches(original, returned.model_copy(update={"conversation_id": "unrelated-cid"}))
    assert not matches(original, returned.model_copy(update={"source": source.model_copy(update={
        "sender": source.sender.model_copy(update={"agent_id": "another-agent"})})}))
    assert not matches(original, returned.model_copy(update={"source": source.model_copy(update={
        "continuation": address.model_copy(update={"request_id": "another-original"})})}))
    c.api.remote.records[returned.request_id] = returned
    later = c.ui.console.instructions.store.new(draft["thread_id"])
    assert send(c, later, "A later human message.").status_code == 200
    latest[0] = complete(c.api.remote.records[later["request_id"]], 3)
    c.api.remote.records[later["request_id"]] = latest[0]
    before = len(c.calls)
    refreshed = progress(c, draft["thread_id"])
    assert refreshed.status_code == 200, refreshed.text
    value = refreshed.json()
    row = next(item for item in value["tasks"] if item["task_id"] == returned.request_id)
    assert row["instruction"] is None and row["agent_text"] == "收到"
    assert row["reply"]["data"]["text"] == c.api.remote.text
    assert [item["task_id"] for item in value["tasks"] if item.get("thread_id") == draft["thread_id"]] == [
        original.request_id, returned.request_id, later["request_id"]]
    assert value["composer"]["awaiting_agent_reply"] is False
    assert all(method == "GET" for method, _path, _body in c.calls[before:])
    assert c.ui.client.get("/api/instructions/" + returned.request_id + "/reply").status_code == 200
    assert c.ui.console.instructions.store.command(returned.request_id) is None
    c.ui.client.__exit__(None, None, None)
    c.ui = c.open(loop_factory=lambda: c.peer_reopen_loop)
    reopened = progress(c, draft["thread_id"])
    assert reopened.status_code == 200, reopened.text
    assert any(item["task_id"] == returned.request_id for item in reopened.json()["tasks"])
    assert c.ui.console.instructions.store.command(returned.request_id) is None

def test_a_responses_received_conversation_carries_its_saved_reply_only_through_reply(responses_console, monkeypatch):
    """dev v15 (main 601aca90): B's turn for A's handoff is on B's Responses lane. Its progress read carries no saved
    answer, so the sidebar preview fell back to A's brief; the existing pure /reply read returns B's answer."""
    from shared_context.behavior_native_turn_wire import (
        NATIVE_REQUEST, NativeAgentReceive, NativeConversation, NativeReceipt,
    )
    from test_native_turn_wire import agent_receive

    c, _env = responses_console
    first(c)
    target, rid = c.api.remote.target, "message-from-a"
    plain = agent_receive(rid=rid, recipient=target.agent_id)
    message = NativeAgentReceive(state_profile=RESPONSES_PROFILE, new_conversation_id="received-rsp-cid",
                                 command=plain.command, capture=plain.capture, source=plain.source)
    c.api.remote.records[rid] = NativeReceipt(
        state_profile=RESPONSES_PROFILE, agent_id=target.agent_id, hosted_session_id=target.session_id,
        request_id=rid, conversation_id="received-rsp-cid", command=message.command, capture=message.capture,
        source=message.source, submit_digest=message.sha256, capture_digest=message.capture.sha256, status="accepted")
    record = c.api.remote.records[rid]
    c.api.remote.records[rid] = record.model_copy(update={"response_id": "resp-received-1"})
    c.api.remote.completed(rid)
    transport = c.api.foundry.http._transport

    def conversation_reads(request):
        if request.url.path.endswith("/invocations"):
            command = NATIVE_REQUEST.validate_json(request.content)
            if command.operation == "conversation":
                value = c.api.remote.records[rid]
                conversation = NativeConversation(
                    state_profile=RESPONSES_PROFILE, agent_id=value.agent_id,
                    hosted_session_id=value.hosted_session_id, conversation_id=value.conversation_id,
                    last_turn=value.turn_number, last_request_id=rid,
                    capture=value.capture, capture_digest=value.capture_digest)
                return httpx.Response(200, content=conversation.model_dump_json(), headers={
                    "content-type": "application/json", "x-agent-session-id": value.hosted_session_id,
                    "x-agent-invocation-id": "offline-received-conversation"})
        return transport.handle_request(request)

    monkeypatch.setattr(c.api.foundry.http, "_transport", httpx.MockTransport(conversation_reads))
    before = len(c.calls)
    opened = progress(c, rid)
    assert opened.status_code == 200, opened.text
    task = next(item for item in opened.json()["tasks"] if item["task_id"] == rid)
    assert opened.json()["composer"]["agent_conversation"]["read_only"] is True
    assert task["state_profile"] == RESPONSES_PROFILE and task["native_receipt"]["status"] == "completed"
    assert task["reply"] is None and task["actions"]["read-reply"]["enabled"] is True
    replied = c.ui.client.get("/api/instructions/" + rid + "/reply")
    assert replied.status_code == 200, replied.text
    assert replied.json()["composer"]["thread_id"] == rid
    task = next(item for item in replied.json()["tasks"] if item["task_id"] == rid)
    assert task["reply"]["data"]["text"] == c.api.remote.text
    assert all(method == "GET" for method, _path, _body in c.calls[before:])


def test_manual_platform_history_pages_never_accept_arbitrary_source_or_wake_vm(responses_console):
    c, env = responses_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    assert progress(c, draft["thread_id"]).status_code == 200
    before = len(env.calls)
    base = "/api/instructions/" + draft["thread_id"] + "/platform-history"
    page = c.ui.client.get(base)
    assert page.status_code == 200, page.text
    assert all(call.method == "GET" and "/protocols/openai/" in call.url.path for call in env.calls[before:])
    for suffix in ("?after=not-returned", "?previous=1", "?source_id=another", "?after=one&after=two"):
        start = len(env.calls)
        assert c.ui.client.get(base + suffix).status_code in {403, 409}
        assert len(env.calls) == start


def test_template_selection_cannot_enable_responses_when_server_reports_disabled(responses_console, monkeypatch):
    from shared_context import behavior_native_responses_api
    from shared_context.behavior_profile_selection import SelectConversationProfile
    from shared_context.behavior_agent_template import AgentTemplate

    c, _env = responses_console
    console = c.ui.console
    template = AgentTemplate.model_validate_json(console.download_template())
    monkeypatch.setattr(behavior_native_responses_api, "RESPONSES_CONVERSATION_MODE", None)
    before = len(c.calls)
    command = SelectConversationProfile(
        request_id="cannot-enable-disabled-response", expected_selection_id=None, template=template)
    with pytest.raises(ConsoleError) as refused:
        console.select_template(command)
    assert refused.value.code == "NATIVE_TURNS_DISABLED"
    assert not any(method == "POST" for method, _path, _body in c.calls[before:])


def test_captured_response_history_remains_readable_when_new_responses_are_disabled(responses_console, monkeypatch):
    from shared_context import behavior_native_responses_api
    from shared_context.errors import Problem

    c, env = responses_console
    draft = first(c)
    assert send(c, draft).status_code == 200
    assert progress(c, draft["thread_id"]).status_code == 200

    def no_new_turns(*_args, **_kwargs):
        raise Problem(409, "NATIVE_TURNS_DISABLED", "OFFLINE newer build disabled new Responses")

    monkeypatch.setattr(behavior_native_responses_api, "current_hosted_version", no_new_turns)
    c.ui.console.native_instructions.refresh_routing(c.ui.console.vault.get(), thread_id=draft["thread_id"])
    before = len(env.calls)
    response = c.ui.client.get("/api/instructions/" + draft["thread_id"] + "/platform-history")
    assert response.status_code == 200, response.text
    assert response.json()["platform_history"]["page"]["source_kind"] == "conversation"
    assert all(call.method == "GET" and "/protocols/openai/" in call.url.path for call in env.calls[before:])


def test_console_response_chain_uses_verified_response_id_and_previous_link_only(responses_console, monkeypatch):
    from shared_context import behavior_native_responses_api
    from shared_context.behavior_native_turn_wire import NativeReceipt

    c, env = responses_console
    monkeypatch.setattr(behavior_native_responses_api, "RESPONSES_CONVERSATION_MODE", "previous_response_id")
    draft = first(c)
    assert send(c, draft).status_code == 200
    original = c.api.remote.records[draft["request_id"]]
    c.api.remote.records[draft["request_id"]] = NativeReceipt.model_validate_json(original.model_copy(update={
        "status": "started", "turn_number": 1, "response_id": "resp-server-original", "dispatch": None,
    }).model_dump_json())
    env.state["response_snapshot"] = {"previous_response_id": "resp-server-previous"}
    observed = progress(c, draft["thread_id"])
    assert observed.status_code == 200, observed.text
    assert observed.json()["composer"]["platform_history"]["page"] is None
    opened = c.ui.client.get("/api/instructions/" + draft["thread_id"] + "/platform-history")
    assert opened.status_code == 200, opened.text
    page = opened.json()["platform_history"]["page"]
    assert page["source_kind"] == "response" and page["source_id"] == "resp-server-original"
    assert not any("resp-server-previous" in call.url.path for call in env.calls)
    env.state["response_snapshot"] = {}
    before = len(env.calls)
    previous = c.ui.client.get("/api/instructions/" + draft["thread_id"] + "/platform-history?previous=1")
    assert previous.status_code == 200, previous.text
    assert previous.json()["platform_history"]["page"]["source_id"] == "resp-server-previous"
    assert all("/responses/resp-server-previous" in call.url.path and call.method == "GET"
               for call in env.calls[before:])
    assert not any(call.url.path.endswith("/conversations") for call in env.calls)


def test_missing_platform_phase_keeps_unconfirmed_items_and_reads_only_an_explicit_known_rid(responses_console):
    from shared_context.behavior_native_turn_wire import NativeReceipt, NativeResultReference

    c, env = responses_console
    draft = first(c)
    assert send(c, draft, "Keep original result provenance.").status_code == 200
    original = c.api.remote.records[draft["request_id"]]
    text = c.api.remote.text
    reference = NativeResultReference(
        turn_number=1, turn_key="turn:1", result_digest="a" * 64,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(), text_bytes=len(text.encode()),
        text_parts=2, checkpoint_sha256="b" * 64)
    c.api.remote.records[draft["request_id"]] = NativeReceipt.model_validate_json(original.model_copy(update={
        "status": "completed", "turn_number": 1, "response_id": "resp-original", "dispatch": None, "result": reference,
    }).model_dump_json())
    env.state["history_items"] = [{
        "id": "unconfirmed-platform-item", "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "Do not guess this is the saved answer."}],
    }]
    observed = progress(c, draft["thread_id"])
    assert observed.status_code == 200, observed.text
    opened = c.ui.client.get("/api/instructions/" + draft["thread_id"] + "/platform-history")
    assert opened.status_code == 200, opened.text
    page = opened.json()["platform_history"]["page"]
    assert page["fallback"] == "native-result" and page["messages"][0]["text"] is None
    before = len(c.calls)
    original_reply = c.ui.client.get("/api/instructions/" + draft["request_id"] + "/reply.txt")
    assert original_reply.status_code == 200 and original_reply.content == text.encode()
    assert any(path.endswith("/result") for _method, path, _body in c.calls[before:])
    assert not any(method == "POST" for method, _path, _body in c.calls[before:])
    refused = c.ui.client.get("/api/instructions/unconfirmed-platform-item/reply.txt")
    assert refused.status_code != 200


def test_attached_images_use_the_server_selected_responses_lane(responses_console):
    """B1 PA-08 on N6: the same upload, then the Responses submit carries only the image references."""
    from test_user_images_native import encoded
    from test_web_test_console_native import post

    c, _env = responses_console
    draft = first(c)
    stored = post(c, "/api/instructions/" + draft["request_id"] + "/image-upload",
                  {"name": "chart.png", "png_base64": encoded()})
    assert stored.status_code == 200, stored.text
    reference = stored.json()
    sent = post(c, "/api/instructions/" + draft["request_id"] + "/submit",
                {"content": "Describe the chart.", "model": "gpt-4.1-mini", "images": [reference]})
    assert sent.status_code == 200, sent.text
    task = sent.json()["task"]
    assert task["state_profile"] == RESPONSES_PROFILE and task["user_images"] == [reference]
    submits = [json.loads(body) for method, path, body in c.calls
               if method == "POST" and path.endswith("/responses-turns/submit")]
    assert len(submits) == 1 and submits[0]["images"] == [reference]
    assert "png_base64" not in json.dumps(submits[0])

# RTT B live text through the bridge: the page reads the original turn's relayed preview; the bridge only passes the
# API's bytes on, over its usual backend pool, and never sends, retries or stores anything.
def _started(c, draft, response_id="resp-original"):
    rid = draft["request_id"]
    c.api.remote.records[rid] = c.api.remote.records[rid].model_copy(update={
        "status": "started", "response_id": response_id, "dispatch": None})
    return rid


def test_the_bridge_relays_the_original_turns_live_text_and_its_cursor(responses_console, platform):
    from shared_context.behavior_native_responses_api import PREFIX
    from test_native_stream_api import frames

    c, _env = responses_console
    seen, state = platform
    draft = first(c)
    assert send(c, draft, "Stream the reply.").status_code == 200
    rid = _started(c, draft)
    state["events"] = [
        {"type": "response.output_item.added", "sequence_number": 4,
         "item": {"type": "message", "id": "msg_c", "phase": "commentary", "content": [{"text": "PRIVATE"}]}},
        {"type": "response.output_text.delta", "sequence_number": 5, "item_id": "msg_c", "delta": "預覽"},
        {"type": "response.completed", "sequence_number": 6, "response": {"status": "completed", "output": "PRIVATE"}},
    ]
    before = len(c.calls)
    live = c.ui.client.get("/api/instructions/" + rid + "/stream?starting_after=3", headers=c.ui.headers)
    assert live.status_code == 200, live.text
    assert live.headers["content-type"].startswith("text/event-stream")
    assert live.headers["cache-control"] == "no-store" and "PRIVATE" not in live.text
    assert frames(live.text) == [
        {"type": "response.output_item.added", "sequence_number": 4, "item_id": "msg_c", "phase": "commentary"},
        {"type": "response.output_text.delta", "sequence_number": 5, "item_id": "msg_c", "delta": "預覽"},
        {"type": "response.completed", "sequence_number": 6, "status": "completed"},
    ]
    assert len(seen) == 1 and dict(seen[0].url.params)["starting_after"] == "3"
    cid = c.ui.console.native_instructions.receipts[rid].conversation_id
    assert [(method, path) for method, path, _body in c.calls[before:]] == [
        ("GET", PREFIX + "/conversations/" + cid + "/requests/" + rid + "/stream")]


@pytest.mark.parametrize("started,platform_status,code,retry", [
    (False, 200, "NATIVE_STREAM_NOT_STARTED", True), (True, 404, "NATIVE_STREAM_UNAVAILABLE", False)])
def test_a_turn_without_live_text_tells_the_page_whether_to_ask_again(
        responses_console, platform, started, platform_status, code, retry):
    c, _env = responses_console
    _seen, state = platform
    draft = first(c)
    assert send(c, draft, "Stream the reply.").status_code == 200
    rid = _started(c, draft) if started else draft["request_id"]
    state["status"] = platform_status
    before = len(c.calls)
    live = c.ui.client.get("/api/instructions/" + rid + "/stream", headers=c.ui.headers)
    assert live.status_code == 409, live.text
    assert live.json()["error"]["code"] == code and live.json()["error"]["automatic_retry"] is retry
    assert not any(method == "POST" for method, _path, _body in c.calls[before:])


@pytest.mark.parametrize("query", ["?starting_after=-1", "?starting_after=x", "?starting_after=1234567890123",
                                   "?starting_after=1&starting_after=2", "?other=1"])
def test_the_bridge_accepts_only_one_short_cursor_before_any_backend_read(responses_console, query):
    c, _env = responses_console
    draft = first(c)
    assert send(c, draft, "Stream the reply.").status_code == 200
    rid = _started(c, draft)
    before = len(c.calls)
    live = c.ui.client.get("/api/instructions/" + rid + "/stream" + query, headers=c.ui.headers)
    assert live.status_code == 403 and live.json()["error"]["code"] == "LOCAL_REQUEST_DENIED"
    assert c.calls[before:] == []


def test_live_text_is_only_for_a_known_turn(responses_console):
    c, _env = responses_console
    before = len(c.calls)
    unknown = c.ui.client.get("/api/instructions/not-a-turn/stream", headers=c.ui.headers)
    assert unknown.status_code == 403 and c.calls[before:] == []
    draft = first(c)
    assert send(c, draft, "Not sent to the stream yet.").status_code == 200
    c.ui.console.native_instructions.receipts.pop(draft["request_id"])
    before = len(c.calls)
    waiting = c.ui.client.get("/api/instructions/" + draft["request_id"] + "/stream", headers=c.ui.headers)
    assert waiting.status_code == 409 and waiting.json()["error"]["code"] == "NATIVE_STREAM_NOT_STARTED"
    assert waiting.json()["error"]["automatic_retry"] is True and c.calls[before:] == []


def _live_adapter(c, draft, server):
    from web_test_console.responses import ResponsesAdapter

    console = c.ui.console
    client = httpx.Client(transport=httpx.MockTransport(server))
    adapter = ResponsesAdapter(console.config, client, api_prefix=console.api_prefix)
    return adapter, client, console.native_instructions.receipts[draft["request_id"]].conversation_id


@pytest.mark.parametrize("split", [False, True])
def test_the_adapter_stops_a_live_stream_that_echoes_the_login(responses_console, split):
    c, _env = responses_console
    draft = first(c)
    assert send(c, draft, "Stream the reply.").status_code == 200
    token = c.ui.console.vault.get()
    leak = b"data: " + token.encode() + b"\n\n"
    body = [b'data: {"type":"response.in_progress","sequence_number":1}\n\n',
            *([leak[:12], leak[12:]] if split else [leak]),
            b'data: {"type":"response.completed","sequence_number":2}\n\n']
    adapter, client, cid = _live_adapter(c, draft, lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=iter(body)))
    try:
        chunks = list(adapter.live_text(cid, draft["request_id"], token))
    finally:
        client.close()
    assert b"".join(chunks) == body[0]


@pytest.mark.parametrize("status,code,expected,retry", [
    (404, ["NATIVE_STREAM_NOT_STARTED"], "NO_RESULT", False),
    (404, "NATIVE_RID_MISSING", "NATIVE_RID_MISSING", True),
])
def test_the_adapter_maps_a_live_refusal_by_its_string_code_or_its_status(
        responses_console, status, code, expected, retry):
    c, _env = responses_console
    draft = first(c)
    assert send(c, draft, "Stream the reply.").status_code == 200
    adapter, client, cid = _live_adapter(c, draft, lambda request: httpx.Response(
        status, json={"error": {"code": code}}))
    try:
        with pytest.raises(ConsoleError) as refused:
            adapter.live_text(cid, draft["request_id"], c.ui.console.vault.get())
    finally:
        client.close()
    assert (refused.value.code, refused.value.automatic_retry) == (expected, retry)