"""Signed human ledger reads and the console consumer; no SDK process or model."""

import copy
from contextvars import Context
from dataclasses import replace

import httpx
import pytest

from shared_context.behavior_assistant import _LocalHttp
from shared_context.behavior_assistant_hosted import AssistantClient
from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_delegation import delegation_key, state_key
from shared_context.behavior_delegation_wire import DelegationObservation, DelegationReply
from test_behavior_assistant import assistant
from test_conversation_profile_selection import select, template
from test_delegation_api import preparation
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_web_test_console_attachments import draft, picker, submit
from test_web_test_console_frozen_interface import open_ui
from web_test_console.adapter import ConsoleError, canonical, sha


@pytest.fixture
def delegated_console(picker):
    c = picker
    grants = available_grants(None, ("fa-delegate-v1",))
    c.h.runtime.capability_grants = grants
    selected, _ = select(c.h, value=template("fa-instruction-history-v1",
        user_text_file_profile="fa-user-text-files-v1", capabilities=(grants["fa-delegate-v1"].ref,)))
    assert selected.status_code == 200, selected.text
    c.selection_id = selected.json()["result"]["selection_id"]
    c.original = draft(c)
    accepted = submit(c)
    assert accepted.status_code == 200, accepted.text
    c.task = accepted.json()["task"]
    c.binding = c.h.runtime.bindings[c.task["binding_id"]]
    c.cid = c.task["admission"]["conversation_id"]
    c.remote = f"/work-policy/v1/conversations/{c.cid}/turns/1/delegation"
    c.local = f"/api/instructions/{c.task['task_id']}/delegation"
    starts = []
    c.h.workers.adapter = replace(c.h.workers.adapter, start=lambda value: starts.append(value))
    c.h.workers.start(c.binding.job_id, c.binding.owner)
    c.assignment, ticket = c.h.assistant.issue(starts[0])
    c.agent = AssistantClient("https://context.invalid", c.assignment, ticket,
        token_provider=lambda: c.h.headers("agent")["Authorization"][7:], transport=_LocalHttp(c.h.client))
    c.agent.acquire()
    try:
        yield c
    finally:
        c.agent.close()


def prepare(c):
    return c.agent.delegation("prepare", preparation())


def observe(c, event_id, kind, **fields):
    return c.agent.delegation("observe", DelegationObservation(event_id=event_id,
        tool_call_id=preparation().tool_call_id, kind=kind, **fields))


def started(c):
    prepare(c)
    return observe(c, "child-start", "started", child_agent_id="original-child")


def read(c, browser=None):
    return (browser or c.browser).ui.get(c.local)


def test_no_intent_is_204_but_original_reserved_unknown_is_not_absent(delegated_console):
    c = delegated_console
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    absent = c.h.client.get(c.remote, headers=c.h.headers("read-only"))
    assert absent.status_code == 204 and absent.content == b""
    assert absent.headers["cache-control"] == "no-store"
    c.calls.clear()
    local = read(c)
    assert local.status_code == 200 and local.json()["delegation"] is None
    assert c.calls == [("GET", c.remote, b"")]
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
    first = prepare(c)
    assert first.fresh
    rows = copy.deepcopy(c.h.transport.backing.rows)
    value = read(c).json()["delegation"]
    assert value["status"] == "unknown" and not value["fresh"]
    assert value["delegation_id"] == first.delegation_id
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal


@pytest.mark.parametrize("status", ["running", "completed", "failed", "incomplete", "unknown", "cancelled"])
def test_original_child_projection_preserves_result_and_does_not_finish_parent(delegated_console, status):
    c = delegated_console
    expected = started(c)
    public_text = "\ufeff  <b>原子題只是文字</b>🙂\r\n" + "公開片段\r\n" * 15000
    if status == "completed":
        expected = observe(c, "child-result", "result", child_agent_id="original-child",
                           outcome="completed", text=public_text)
    elif status == "failed":
        expected = observe(c, "child-failed", "result", child_agent_id="original-child",
                           outcome="failed", text="A public child failure.", failure_code="CHILD_FAILED")
    elif status == "incomplete":
        expected = observe(c, "tool-only", "result", child_agent_id="original-child",
                           outcome="completed", text="Unfinished public output.", ended_in_tool_call=True)
    elif status == "unknown":
        expected = observe(c, "transport-lost", "unknown", child_agent_id="original-child",
                           failure_code="CHILD_TRANSPORT_UNKNOWN")
    elif status == "cancelled":
        observe(c, "cancel-intent", "cancel")
        partial = read(c).json()["delegation"]
        assert partial["cancel_requested"] and not partial["stop_confirmed"]
        observe(c, "child-stopped", "result", child_agent_id="original-child", outcome="cancelled")
        partial = read(c).json()["delegation"]
        assert partial["child_cancelled"] and not partial["stop_confirmed"]
        expected = observe(c, "parent-idle", "parent_idle", child_agent_id="original-child")
    assert expected.status == status
    before, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    conversation_before = c.h.client.get(f"/work-policy/v1/conversations/{c.cid}", headers=c.h.headers()).content
    c.calls.clear()
    response = read(c)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_id"] == c.task["task_id"] and body["job_id"] == c.binding.job_id
    assert body["conversation_id"] == c.cid and body["turn_number"] == 1
    assert DelegationReply.model_validate_json(canonical(body["delegation"])) == expected
    if status == "completed":
        assert len(response.content) > 131072 and body["delegation"]["result"] == public_text
    assert c.calls == [("GET", c.remote, b"")]
    assert c.h.client.get(f"/work-policy/v1/conversations/{c.cid}", headers=c.h.headers()).content == conversation_before
    assert c.h.workers.inspect(c.binding.job_id).state not in {"succeeded", "failed", "cancelled"}
    assert c.h.transport.backing.rows == before and c.journal.read_bytes() == journal


def test_human_reader_uses_original_capture_after_selection_and_console_restart(delegated_console):
    c = delegated_console
    expected = started(c)
    selected, _ = select(c.h, request_id="next-conversation-without-delegation",
                         previous=c.selection_id, value=template())
    assert selected.status_code == 200, selected.text
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    restarted = open_ui(c)
    try:
        response = read(c, restarted)
        assert response.status_code == 200 and response.json()["delegation"] == expected.model_dump(mode="json")
        assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
        c.control.revoke(c.human, "current-read-revoked")
        denied = read(c, restarted)
        assert denied.status_code == 409 and denied.json()["error"]["upstream_status"] == 403
        assert "delegation" not in denied.json()
    finally:
        restarted.ui.close()


def test_reader_rejects_wrong_identity_turn_body_and_query_without_writes(delegated_console):
    c = delegated_console
    started(c)
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    assert c.h.client.get(c.remote, headers=c.h.headers("agent")).status_code == 403
    assert c.h.client.get(c.remote.replace(c.cid, "other-conversation"), headers=c.h.headers()).status_code == 404
    assert c.h.client.get(c.remote.replace("/turns/1/", "/turns/2/"), headers=c.h.headers()).status_code == 404
    assert c.h.client.get(c.remote + "?dispatch=true", headers=c.h.headers()).status_code == 422
    assert c.h.client.request("GET", c.remote, content=b"{}", headers=c.h.headers()).status_code == 422
    c.calls.clear()
    assert c.browser.ui.get(c.local + "?task=other").status_code == 403
    assert c.browser.ui.request("GET", c.local, content=b"{}").status_code == 403
    assert not c.calls
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal


def test_partial_ledger_stays_unknown_and_never_becomes_no_intent(delegated_console, monkeypatch):
    c = delegated_console
    started(c)
    key = delegation_key(c.binding.work.work_id)
    original = c.h.store.read_snapshot
    hidden = {key}

    def missing(keys):
        return {name: None if name in hidden else row for name, row in original(keys).items()}

    monkeypatch.setattr(c.h.store, "read_snapshot", missing)
    for name in (key, state_key(key)):
        hidden.clear()
        hidden.add(name)
        response = read(c)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "DELEGATION_UNKNOWN"
        assert response.json()["error"]["upstream_status"] == 503
        assert "delegation" not in response.json()


def test_current_read_withdrawal_during_ledger_read_cannot_publish_child(delegated_console, monkeypatch):
    c = delegated_console
    started(c)
    original, withdrawn = c.h.store.read_snapshot, []
    key = delegation_key(c.binding.work.work_id)

    def revoke_after_snapshot(keys):
        rows = original(keys)
        if key in keys and not withdrawn:
            withdrawn.append(True)
            Context().run(c.control.revoke, c.human, "withdraw-during-child-read")
        return rows

    monkeypatch.setattr(c.h.store, "read_snapshot", revoke_after_snapshot)
    response = read(c)
    assert withdrawn and response.status_code == 409
    assert response.json()["error"]["upstream_status"] in {403, 409, 412}
    assert "delegation" not in response.json()


def test_malformed_or_old_reader_cannot_become_a_saved_or_absent_child(delegated_console):
    c = delegated_console
    original = started(c).model_dump(mode="json")
    malformed = [
        {**original, "parent_job_id": "another-parent"},
        {**original, "sdk_session_id": "another-session"},
        {**original, "schema_version": "unknown-schema"},
        {key: value for key, value in original.items() if key != "schema_version"},
        {**original, "status": "completed", "result": None},
        {**original, "status": "cancelled", "stop_confirmed": False},
        {**original, "fresh": True},
    ]
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    for value in malformed:
        def changed(request, response):
            if request.url.path != c.remote:
                return response
            raw = canonical(value)
            return httpx.Response(200, content=raw,
                headers={"cache-control": "no-store", "x-content-sha256": sha(raw)})
        c.mutate = changed
        response = read(c)
        assert response.status_code == 502 and response.json()["error"]["code"] == "DELEGATION_RESPONSE_INVALID"
    raw = canonical(original)
    for invalid_raw, headers in [
        (b'{"status":"running",' + raw[1:], {"cache-control": "no-store"}),
        (raw, {"cache-control": "public", "x-content-sha256": sha(raw)}),
        (raw, {"cache-control": "no-store", "x-content-sha256": "0" * 64}),
        (b"", {"cache-control": "no-store", "x-content-sha256": sha(b"")}),
    ]:
        headers.setdefault("x-content-sha256", sha(invalid_raw))
        c.mutate = lambda request, response: httpx.Response(200, content=invalid_raw, headers=headers)
        assert read(c).status_code == 502
    for invalid_raw, headers in [(b"unexpected", {"cache-control": "no-store"}), (b"", {})]:
        c.mutate = lambda request, response: httpx.Response(204, content=invalid_raw, headers=headers)
        assert read(c).status_code == 502
    for status in (404, 405):
        c.mutate = lambda request, response: httpx.Response(status, json={"detail": "Not Found"})
        response = read(c)
        assert response.status_code == 409 and response.json()["error"]["code"] == "DELEGATION_UNAVAILABLE"
        assert response.json()["error"]["upstream_status"] == status
    c.mutate = lambda request, response: httpx.Response(204, headers={"cache-control": "no-store"})
    console = c.browser.app.state.console
    with pytest.raises(ConsoleError) as other:
        console.instructions.adapter.send("GET", "/work-policy/v1/models", None, console.vault.get())
    assert other.value.upstream_status == 204
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal


def test_lost_read_response_only_repeats_original_get(delegated_console):
    c = delegated_console
    expected = started(c)
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()

    def lost(request, response):
        raise httpx.ReadError("Synthetic response loss.", request=request)

    c.mutate = lost
    c.calls.clear()
    assert read(c).status_code == 503
    c.mutate = lambda request, response: response
    assert read(c).json()["delegation"] == expected.model_dump(mode="json")
    assert c.calls == [("GET", c.remote, b""), ("GET", c.remote, b"")]
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
