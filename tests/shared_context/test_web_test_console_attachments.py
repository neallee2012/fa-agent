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
from instruction_attachments_helpers import configured_attachments
from conversation_profile_selection_helpers import select, template
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from web_test_console_frozen_interface_helpers import open_ui
from web_test_console_standing_helpers import configuration
from web_test_console.adapter import parse_config
from web_test_console.instructions import InstructionConnection
from web_test_console_attachments_helpers import submit, draft
from legacy_console_rows_helpers import stored_console

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


def catalog(c, request_id=None, browser=None):
    browser = browser or c.browser
    return browser.ui.get(f"/api/instructions/{request_id or draft(c, browser)['request_id']}/attachment-sources")


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


@pytest.mark.parametrize("enabled_before", [False, True])
def test_existing_catalog_uses_captured_profile_not_the_new_global_selection(picker, enabled_before, monkeypatch):
    c = picker
    first, _ = select(c.h, value=template(**({"text_attachment_profile": "fa-source-text-attachments-v1"} if enabled_before else {})))
    assert first.status_code == 200
    # The retired submit admitted one work under this selection.
    task = stored_console(c, "console-picker-catalog-" + ("enabled" if enabled_before else "disabled"))["task"]
    cid = task["admission"]["conversation_id"]
    saved = c.control.records[task["binding_id"]].model_dump_json()
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
    cid = stored_console(c, "console-picker-submitted")["task"]["admission"]["conversation_id"]
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


@pytest.mark.parametrize("code", ["CONVERSATION_ARCHIVED", "CONVERSATION_SESSION_RESET_REQUIRED"])
def test_original_conversation_409_is_not_retried_as_a_global_catalog(picker, code):
    c = picker
    stored_console(c, "console-picker-submitted")
    c.calls.clear()
    c.mutate = lambda request, response: httpx.Response(409, json={"error": {"code": code}})
    response = catalog(c)
    assert response.status_code == 409 and response.json()["error"]["code"] == code
    assert len(c.calls) == 1 and c.calls[0][1] != GLOBAL
