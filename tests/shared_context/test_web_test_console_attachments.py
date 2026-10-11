"""Published signed metadata/admission APIs and local journals; no runtime or real network."""

import asyncio
import copy
import hashlib
import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context.behavior_assistant import _LocalHttp
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_instruction_attachments import configured_attachments
from test_conversation_profile_selection import select, template
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_web_test_console_frozen_interface import open_ui
from test_web_test_console_standing import configuration
from web_test_console.adapter import parse_config
from web_test_console.instructions import InstructionConnection

GLOBAL = "/work-policy/v1/attachment-sources"


@pytest.fixture
def picker(assistant, monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Attachment picker must not start runtime, model or real network")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    h, control, human, attachment = configured_attachments(assistant)
    h.client.close()
    h.app = create_assistant_app(h.assistant, h.verifier)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: control.installed(), require_current_native_evidence=lambda: None,
    ))
    h.client = TestClient(h.app)
    monkeypatch.setattr(type(h.runtime.controller), "step", forbidden)
    local = _LocalHttp(h.client)
    c = SimpleNamespace(h=h, control=control, human=human, attachment=attachment, calls=[],
                        mutate=lambda request, response: response, before=lambda request: None,
                        journal=tmp_path / "picker.sqlite3")

    def send(request):
        c.calls.append((request.method, request.url.path, request.content))
        c.before(request)
        return c.mutate(request, local.handle_request(request))

    c.config = parse_config(json.dumps(configuration(h, control)).encode())
    c.connection = InstructionConnection(schema_version="fa-console-instructions-connection-v1",
        configuration_sha256=c.config.sha256, backend_core_sha256="c" * 64)
    c.backend = httpx.Client(transport=httpx.MockTransport(send))
    c.browser = open_ui(c)
    c.calls.clear()
    try:
        yield c
    finally:
        c.browser.ui.close()
        c.backend.close()
        assert not h.endpoint.requests


def draft(c, browser=None):
    return (browser or c.browser).ui.get("/api/state").json()["composer"]


def catalog(c, request_id=None, browser=None):
    browser = browser or c.browser
    return browser.ui.get(f"/api/instructions/{request_id or draft(c, browser)['request_id']}/attachment-sources")


def submit(c, attachments=None, *, request_id=None, browser=None, content="  Read this exact source.\n"):
    browser = browser or c.browser
    body = {"content": content, "model": "gpt-4.1-mini"}
    if attachments is not None:
        body["attachments"] = attachments
    return browser.ui.post(f"/api/instructions/{request_id or draft(c, browser)['request_id']}/submit",
                           json=body, headers=browser.headers)


def test_new_draft_reads_recorded_metadata_only_and_keeps_journal_unchanged(picker, monkeypatch):
    c = picker
    before, rows = c.journal.read_bytes(), copy.deepcopy(c.h.transport.backing.rows)
    for name in ("read_exact", "read_graph"):
        monkeypatch.setattr(c.h.sources, name, lambda *args, **kwargs: pytest.fail("Metadata must not read content"))
    original = draft(c)
    response = catalog(c)
    assert response.status_code == 200, response.text
    value = response.json()
    assert (value["thread_id"], value["request_id"], value["conversation_id"]) == (
        original["thread_id"], original["request_id"], None,
    )
    assert value["catalog"]["state"] == "metadata"
    recorded = next(item for item in value["catalog"]["entries"] if item["source_id"] == c.attachment.source.source_id)
    assert recorded["reference"] == c.attachment.source.model_dump(mode="json")
    assert value["catalog"]["authorization"] is value["catalog"]["content_reverified"] is False
    assert value["catalog"]["dependencies_reverified"] is False
    assert c.calls == [("GET", GLOBAL, b"")]
    assert c.journal.read_bytes() == before and c.h.transport.backing.rows == rows


def test_full_attachment_command_is_frozen_before_submit_and_cannot_change_afterward(picker):
    c = picker
    original = draft(c)
    attachments = [c.attachment.model_dump(mode="json")]
    observed = []

    def before(request):
        if request.url.path == "/work-policy/v1/submit":
            with sqlite3.connect(c.journal) as db:
                frozen = db.execute("SELECT content FROM instructions WHERE request_id=?", (original["request_id"],)).fetchone()
                command = db.execute("SELECT command FROM instruction_commands WHERE request_id=?", (original["request_id"],)).fetchone()
                intent = db.execute("SELECT intent FROM intents WHERE task_id=? AND phase='submit'", (original["request_id"],)).fetchone()
            body = json.loads(command[0])
            assert body == json.loads(request.content) and body["attachments"] == attachments
            assert frozen[0] == body["content"]
            assert json.loads(intent[0])["body_sha256"] == hashlib.sha256(request.content).hexdigest()
            observed.append(command[0])

    c.before = before
    response = submit(c, attachments)
    assert response.status_code == 200, response.text
    assert len(observed) == 1
    record = c.control.records[response.json()["task"]["binding_id"]]
    assert record.profile.instruction.attachments == (c.attachment,)
    saved = draft(c)
    assert saved["attachments"] == attachments and saved["editable"] is False
    changed = [{**attachments[0], "name": "other.txt"}]
    denied = submit(c, changed)
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "INSTRUCTION_CHANGED"
    assert submit(c, []).status_code == 409
    assert sum(path == "/work-policy/v1/submit" for _, path, _ in c.calls) == 1


@pytest.mark.parametrize("enabled_before", [False, True])
def test_existing_catalog_uses_captured_profile_not_the_new_global_selection(picker, enabled_before, monkeypatch):
    c = picker
    first, _ = select(c.h, value=template(**({"text_attachment_profile": "fa-source-text-attachments-v1"} if enabled_before else {})))
    assert first.status_code == 200
    response = submit(c)
    assert response.status_code == 200, response.text
    cid = response.json()["task"]["admission"]["conversation_id"]
    saved = c.control.records[response.json()["task"]["binding_id"]].model_dump_json()
    second, _ = select(c.h, request_id="change-global-profile", previous=first.json()["result"]["selection_id"],
                       value=template(**({} if enabled_before else {"text_attachment_profile": "fa-source-text-attachments-v1"})))
    assert second.status_code == 200
    c.calls.clear()
    for name in ("read_exact", "read_graph"):
        monkeypatch.setattr(c.h.sources, name, lambda *args, **kwargs: pytest.fail("Catalogue must not read content"))
    response = catalog(c)
    assert response.status_code == 200, response.text
    assert response.json()["conversation_id"] == response.json()["catalog"]["conversation_id"] == cid
    assert response.json()["catalog"]["state"] == ("metadata" if enabled_before else "disabled")
    assert c.calls == [("GET", f"/work-policy/v1/conversations/{cid}/attachment-sources", b"")]
    assert c.h.client.get(GLOBAL, headers=c.h.headers()).json()["state"] == ("disabled" if enabled_before else "metadata")
    assert c.control.records[next(iter(c.control.records))].model_dump_json() == saved


@pytest.mark.parametrize("damage", ["wrong-cid", "missing-cid", "authorization", "content", "dependencies", "cache", "duplicate", "404"])
def test_catalog_faults_never_fall_back_to_global_or_create_intents(picker, damage):
    c = picker
    submitted = submit(c)
    assert submitted.status_code == 200
    cid = submitted.json()["task"]["admission"]["conversation_id"]
    c.calls.clear()

    def damaged(request, response):
        if damage == "404":
            return httpx.Response(404, json={"detail": "Not Found"})
        body = response.json()
        if damage == "wrong-cid":
            body["conversation_id"] = "not-this-conversation"
        if damage == "missing-cid":
            del body["conversation_id"]
        if damage in {"authorization", "content", "dependencies"}:
            body[{"authorization": "authorization", "content": "content_reverified", "dependencies": "dependencies_reverified"}[damage]] = True
        raw = json.dumps(body).encode()
        if damage == "duplicate":
            raw = b'{"authorization":false,' + raw[1:]
        return httpx.Response(200, content=raw, headers={
            "Content-Type": "application/json", "Cache-Control": "public" if damage == "cache" else "no-store",
        })

    c.mutate = damaged
    before = c.journal.read_bytes()
    response = catalog(c)
    assert response.status_code == (409 if damage == "404" else 502)
    assert response.json()["error"]["code"] == ("ATTACHMENT_CATALOG_UNAVAILABLE" if damage == "404" else "ATTACHMENT_CATALOG_INVALID")
    assert c.calls == [("GET", f"/work-policy/v1/conversations/{cid}/attachment-sources", b"")]
    assert c.journal.read_bytes() == before


@pytest.mark.parametrize("boundary", ["csrf", "host", "cookie", "origin", "query", "body", "login"])
def test_picker_keeps_original_local_and_login_boundaries(picker, boundary):
    c = picker
    current = draft(c)
    headers = dict(c.browser.headers)
    if boundary == "csrf":
        headers["X-Console-CSRF"] = "wrong"
    if boundary == "host":
        headers["Host"] = "other.example"
    if boundary == "cookie":
        headers["Cookie"] = "fa_console=wrong"
    if boundary == "origin":
        headers["Origin"] = "https://other.example"
    if boundary == "login":
        c.browser.app.state.console.vault.clear()
    before = c.journal.read_bytes()
    if boundary == "csrf":
        response = c.browser.ui.post(f"/api/instructions/{current['request_id']}/submit",
            json={"content": "Exact.", "model": "gpt-4.1-mini", "attachments": []}, headers=headers)
    else:
        response = c.browser.ui.request("GET", f"/api/instructions/{current['request_id']}/attachment-sources"
            + ("?conversation_id=another" if boundary == "query" else ""), headers=headers,
            content=b'{"source":"other"}' if boundary == "body" else None)
    assert response.status_code == (401 if boundary == "login" else 403)
    assert c.calls == [] and c.journal.read_bytes() == before


@pytest.mark.parametrize("invalid", ["../path.txt", "https://example.test/a", "a\\b.txt", "a" * 129, "duplicate", "five", "null"])
def test_shared_attachment_bounds_and_filename_reject_before_any_freeze(picker, invalid):
    c = picker
    item = c.attachment.model_dump(mode="json")
    if invalid in {"duplicate", "five"}:
        attachments = [dict(item, name="same.txt" if invalid == "duplicate" else f"ref-{n}.txt")
                       for n in range(2 if invalid == "duplicate" else 5)]
    else:
        attachments = None if invalid == "null" else [dict(item, name=invalid)]
    original = draft(c)
    response = c.browser.ui.post(f"/api/instructions/{original['request_id']}/submit",
        json={"content": "Original.", "model": "gpt-4.1-mini", "attachments": attachments}, headers=c.browser.headers)
    assert response.status_code == 400
    assert c.calls == []
    assert draft(c)["content"] is None and draft(c)["attachments"] == []
    with sqlite3.connect(c.journal) as db:
        assert db.execute("SELECT COUNT(*) FROM instruction_commands").fetchone()[0] == 0


def test_four_complete_references_fit_and_empty_attachments_preserve_original_wire_bytes(picker):
    c = picker
    original = draft(c)
    response = submit(c, [])
    assert response.status_code == 200
    body = next(raw for _, path, raw in c.calls if path == "/work-policy/v1/submit")
    expected = ('{"authorize_own_result":true,"content":"  Read this exact source.\\n",'
                '"model":"gpt-4.1-mini","request_id":' + json.dumps(original["request_id"]) + '}').encode()
    assert body == expected and b'"attachments"' not in body
    created = c.browser.ui.post("/api/instructions/conversations/new", json={}, headers=c.browser.headers)
    assert created.status_code == 200
    attachments = [dict(c.attachment.model_dump(mode="json"), name=f"source-{n}.txt") for n in range(4)]
    result = submit(c, attachments)
    assert result.status_code == 200, result.text
    assert result.json()["task"]["attachments"] == attachments


@pytest.mark.parametrize("phase", [None, "submit", "submit-reconcile-00000001"])
def test_legacy_content_only_rows_are_never_reconstructed_over_a_prior_intent(picker, phase):
    c = picker
    current = draft(c)
    store = c.browser.app.state.console.instructions.store
    content = "  Read this exact source.\n"
    store.freeze(current["request_id"], content)
    if phase is not None:
        store.journal.begin(current["request_id"], phase, {
            "created_at": c.h.time.isoformat(), "body_sha256": "a" * 64,
        })
    before = c.journal.read_bytes()
    response = submit(c, [c.attachment.model_dump(mode="json")])
    if phase is None:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 409 and response.json()["error"]["code"] == "INSTRUCTION_INCOMPLETE"
        assert store.command(current["request_id"]) is None
        assert c.journal.read_bytes() == before and not c.calls
        state = draft(c)
        assert state["can_submit"] is False and "缺少完整 command" in state["message"]


def test_preparation_rolls_back_text_if_full_command_cannot_be_saved(picker):
    c = picker
    current = draft(c)
    with sqlite3.connect(c.journal) as db:
        db.execute("CREATE TRIGGER fail_command BEFORE INSERT ON instruction_commands "
                   "BEGIN SELECT RAISE(ABORT, 'OFFLINE command storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        submit(c, [c.attachment.model_dump(mode="json")])
    assert draft(c)["content"] is None
    assert c.calls == [("GET", "/work-policy/v1/agent-template", b"")]
    with sqlite3.connect(c.journal) as db:
        assert db.execute("SELECT COUNT(*) FROM instruction_commands").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM intents WHERE phase='submit'").fetchone()[0] == 0


def test_response_loss_restart_uses_complete_original_command_without_resubmit(picker):
    c = picker
    original = draft(c)
    attachments = [c.attachment.model_dump(mode="json")]

    def lose(request, response):
        if request.url.path == "/work-policy/v1/submit":
            assert response.status_code == 200
            raise httpx.ReadTimeout("OFFLINE response lost after original admission")
        return response

    c.mutate = lose
    assert submit(c, attachments).status_code == 503
    with sqlite3.connect(c.journal) as db:
        original_row = db.execute("SELECT intent,outcome FROM intents WHERE phase='submit'").fetchone()
    reopened = open_ui(c)
    try:
        c.calls.clear()
        blocked = catalog(c, browser=reopened)
        assert blocked.json()["error"]["code"] == "INSTRUCTION_PENDING" and not c.calls
        assert submit(c, [], browser=reopened).status_code == 409 and not c.calls
        result = reopened.ui.post(f"/api/tasks/{original['request_id']}/submit-reconcile", json={}, headers=reopened.headers)
        assert result.status_code == 200, result.text
        method, path, raw = c.calls[-1]
        assert (method, path) == ("POST", "/work-policy/v1/submit-reconcile")
        assert json.loads(raw) == json.loads(original_row[0])["body"]
        assert json.loads(raw)["attachments"] == attachments
        with sqlite3.connect(c.journal) as db:
            assert db.execute("SELECT intent,outcome FROM intents WHERE phase='submit'").fetchone() == original_row
    finally:
        reopened.ui.close()


def test_two_clients_cannot_replace_prepared_attachments_or_send_twice(picker):
    c = picker
    second = open_ui(c)
    entered, release = Event(), Event()

    def pause(request):
        if request.url.path == "/work-policy/v1/submit":
            entered.set()
            assert release.wait(10)

    c.before = pause
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(submit, c, [c.attachment.model_dump(mode="json")])
            try:
                assert entered.wait(10)
                wrong = submit(c, [], browser=second)
                assert wrong.json()["error"]["code"] == "INSTRUCTION_CHANGED"
            finally:
                release.set()
            assert first.result(timeout=10).status_code == 200
    finally:
        second.ui.close()
    assert sum(path == "/work-policy/v1/submit" for _, path, _ in c.calls) == 1


def test_new_local_draft_keeps_original_cid_and_does_not_inherit_prior_attachments(picker):
    c = picker
    response = submit(c, [c.attachment.model_dump(mode="json")])
    assert response.status_code == 200
    old = draft(c)
    store = c.browser.app.state.console.instructions.store
    original = store.command(old["request_id"])
    # Local draft seam only: API /next eligibility and UI transfer are tested separately.
    following = store.new(old["thread_id"])
    assert following["request_id"] != old["request_id"]
    metadata = catalog(c, following["request_id"])
    assert metadata.status_code == 200 and metadata.json()["conversation_id"] == old["conversation_id"]
    task = c.browser.app.state.console.instructions.prepare(following["request_id"], "Next exact turn.", "gpt-4.1-mini")
    new = store.command(task.task_id)
    assert new["conversation_id"] == old["conversation_id"] and "attachments" not in new
    assert store.command(old["request_id"]) == original


def test_catalog_inflight_draft_change_cannot_publish_an_old_observation(picker):
    c = picker
    current = draft(c)
    store = c.browser.app.state.console.instructions.store

    def change(request, response):
        if request.url.path == GLOBAL:
            store.freeze(current["request_id"], "Other client froze this draft.")
            store.new(current["thread_id"])
        return response

    c.mutate = change
    result = catalog(c, current["request_id"])
    assert result.status_code == 409 and result.json()["error"]["code"] == "INSTRUCTION_CHANGED"
    assert c.calls == [("GET", GLOBAL, b"")]


def test_old_journal_schema_config_and_unrelated_intents_are_unchanged(picker):
    c = picker
    with sqlite3.connect(c.journal) as db:
        db.execute("INSERT INTO intents VALUES ('legacy', 'acknowledge', '{}', '{\"state\":\"UNKNOWN\"}')")
        schema = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        config = db.execute("SELECT * FROM config").fetchall()
        legacy = db.execute("SELECT * FROM intents WHERE task_id='legacy'").fetchall()
    assert submit(c, [c.attachment.model_dump(mode="json")]).status_code == 200
    with sqlite3.connect(c.journal) as db:
        assert db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() == schema
        assert db.execute("SELECT * FROM config").fetchall() == config
        assert db.execute("SELECT * FROM intents WHERE task_id='legacy'").fetchall() == legacy


def test_manual_preflight_preserves_the_original_attachment_wire_hash(picker):
    c = picker
    response = submit(c, [c.attachment.model_dump(mode="json")])
    assert response.status_code == 200
    task = response.json()["task"]
    raw = next(body for _, path, body in c.calls if path == "/work-policy/v1/submit")
    command = InstructionSubmit.model_validate_json(raw)
    c.calls.clear()
    observed = c.browser.ui.post(f"/api/tasks/{task['task_id']}/status", json={}, headers=c.browser.headers)
    assert observed.status_code == 200, observed.text
    preflight = json.loads(next(body for _, path, body in c.calls if path == "/work-policy/v1/preflight"))
    assert preflight["attachments"] == [c.attachment.model_dump(mode="json")]
    assert preflight["submit_body_sha256"] == hashlib.sha256(command.model_dump_json().encode()).hexdigest()
    assert preflight["submit_body_sha256"] != hashlib.sha256(raw).hexdigest()
    assert not any(path == "/work-policy/v1/submit" for _, path, _ in c.calls)


@pytest.mark.parametrize("code", ["CONVERSATION_ARCHIVED", "CONVERSATION_SESSION_RESET_REQUIRED"])
def test_original_conversation_409_is_not_retried_as_a_global_catalog(picker, code):
    c = picker
    assert submit(c).status_code == 200
    c.calls.clear()
    c.mutate = lambda request, response: httpx.Response(409, json={"error": {"code": code}})
    response = catalog(c)
    assert response.status_code == 409 and response.json()["error"]["code"] == code
    assert len(c.calls) == 1 and c.calls[0][1] != GLOBAL
