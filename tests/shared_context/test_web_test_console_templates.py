"""Template references pass through the real signed API, without creating work."""

import copy
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_assistant import _LocalHttp
from shared_context.behavior_agent_template import AgentTemplate, TemplateRole
from shared_context.behavior_profile_selection import SelectConversationProfile
from shared_context.behavior_skill_prompt import assemble_prompt
from test_agent_template import PATH, template_case
from test_behavior_assistant import assistant
from test_instruction_flow import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_web_test_console_standing import configuration, open_ui
from web_test_console.adapter import parse_config


@pytest.fixture
def template_console(template_case, tmp_path):
    h, control, human, source, _ = template_case
    local = _LocalHttp(h.client)
    case = SimpleNamespace(h=h, control=control, human=human, source=source,
                           calls=[], mutate=lambda request, response: response,
                           before_send=lambda request: None,
                           journal=tmp_path / "console.sqlite3")

    def send(request):
        case.calls.append((request.method, request.url.path))
        case.before_send(request)
        return case.mutate(request, local.handle_request(request))

    case.backend = httpx.Client(transport=httpx.MockTransport(send))
    case.config = parse_config(json.dumps(configuration(h, control)).encode())
    case.browser = open_ui(case, case.journal)
    try:
        yield case
    finally:
        case.browser.ui.close()
        case.backend.close()


def test_download_and_inspection_use_original_bytes_without_changing_state(template_console):
    c = template_console
    original = c.h.client.get(PATH, headers=c.h.headers())
    assert original.status_code == 200
    rows, artifacts = copy.deepcopy(c.h.transport.backing.rows), copy.deepcopy(c.h.artifacts.repository.data)
    journal = c.journal.read_bytes()
    exported = c.browser.ui.get("/api/agent-template")
    assert exported.status_code == 200, exported.text
    assert exported.content == original.content
    assert exported.headers["content-disposition"] == 'attachment; filename="fa-agent-template.json"'
    assert exported.headers["x-content-sha256"] == hashlib.sha256(exported.content).hexdigest()
    assert exported.headers["cache-control"] == "no-store"
    inspected = c.browser.ui.post("/api/agent-template/inspect", content=exported.content,
                                 headers={**c.browser.headers, "Content-Type": "application/json"})
    assert inspected.status_code == 200, inspected.text
    result = inspected.json()
    assert result["references_match"] is True
    assert result["authorization"] is False and result["activated"] is False
    assert c.calls == [("GET", PATH), ("POST", PATH + "/inspect")]
    assert c.h.transport.backing.rows == rows and c.h.artifacts.repository.data == artifacts
    assert c.journal.read_bytes() == journal and c.control.records == {}
    assert c.h.endpoint.requests == []


def test_current_selection_is_a_pure_signed_read_without_changing_config_or_journal(template_console):
    c = template_console
    rows, journal, config = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes(), c.config.model_dump_json()
    response = c.browser.ui.get("/api/agent-template/selection")
    assert response.status_code == 200, response.text
    assert response.json()["current"] == {
        "selection_id": None, "applies_to": "new-conversations-only", "permissions_changed": False,
    }
    assert response.json()["selections"] == []
    assert response.headers["cache-control"] == "no-store"
    assert c.calls == [("GET", PATH + "/selection")]
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
    assert c.config.model_dump_json() == config and not c.control.records and not c.h.endpoint.requests


def selection_command(c, *, request_id="ui-select-original", previous=None, profile="fa-instruction-v1"):
    exported = c.h.client.get(PATH, headers=c.h.headers())
    current = AgentTemplate.model_validate_json(exported.content)
    role = assemble_prompt(profile).record
    candidate = current.model_copy(update={
        "role": TemplateRole(profile=role.profile, prefix_sha256=role.prefix_sha256,
                             capability_catalog_version=role.capability_catalog_version),
        "skills": (), "unavailable_skills": (),
    })
    return SelectConversationProfile(request_id=request_id, expected_selection_id=previous, template=candidate)


def choose(c, command, browser=None):
    browser = browser or c.browser
    return browser.ui.post("/api/agent-template/select", content=command.model_dump_json(),
                           headers={**browser.headers, "Content-Type": "application/json"})


def test_a_different_readonly_profile_is_selected_once_with_its_full_original_command(template_console):
    c = template_console
    command = selection_command(c)
    original_policy, config = c.control.policy.model_dump_json(), c.config.model_dump_json()
    inspected = c.browser.ui.post("/api/agent-template/inspect",
        content=command.template.model_dump_json(), headers={**c.browser.headers, "Content-Type": "application/json"})
    assert inspected.status_code == 200 and inspected.json()["references_match"] is False
    saved = []

    def before_send(request):
        if request.url.path == PATH + "/select":
            with sqlite3.connect(c.journal) as db:
                intent, outcome = db.execute(
                    "SELECT intent, outcome FROM intents WHERE phase='template-select'",
                ).fetchone()
            intent, outcome = json.loads(intent), json.loads(outcome)
            assert outcome["state"] == "UNKNOWN"
            assert intent["body"] == json.loads(request.content) == command.model_dump(mode="json")
            assert intent["body"]["expected_selection_id"] is None
            assert intent["body_sha256"] == hashlib.sha256(request.content).hexdigest()
            assert intent["template_sha256"] == inspected.json()["template_sha256"]
            assert intent["config_sha256"] == c.config.sha256
            saved.append(intent)

    c.before_send = before_send
    selected = choose(c, command)
    assert selected.status_code == 200, selected.text
    assert selected.json()["receipt"]["result"]["permissions_changed"] is False
    current = c.browser.ui.get("/api/agent-template/selection").json()
    assert current["current"]["template"] == command.template.model_dump(mode="json")
    assert current["selections"][0]["request_id"] == command.request_id
    assert current["selections"][0]["state"] == "CONFIRMED"
    assert choose(c, command).status_code == 409
    reopened = open_ui(c, c.journal)
    try:
        duplicate = command.model_copy(update={"request_id": "different-client-id"})
        assert choose(c, duplicate, reopened).status_code == 409
        assert reopened.ui.get("/api/state").json()["template_selections"] == current["selections"]
    finally:
        reopened.ui.close()
    assert len(saved) == 1 and c.calls.count(("POST", PATH + "/select")) == 1
    assert c.control.policy.model_dump_json() == original_policy and c.config.model_dump_json() == config
    assert not c.control.records and not c.h.endpoint.requests


def test_lost_selection_response_reconciles_saved_command_after_restart_without_rewriting_unknown(template_console):
    c = template_console
    command = selection_command(c)
    bodies = []
    c.before_send = lambda request: bodies.append((request.url.path, request.content))

    def lose(request, response):
        if request.url.path == PATH + "/select":
            assert response.status_code == 200
            raise httpx.ReadTimeout("OFFLINE response lost after selection committed")
        return response

    c.mutate = lose
    response = choose(c, command)
    assert response.status_code == 503
    with sqlite3.connect(c.journal) as db:
        original = db.execute("SELECT intent, outcome FROM intents WHERE phase='template-select'").fetchone()
    assert json.loads(original[1])["state"] == "UNKNOWN"
    rows = copy.deepcopy(c.h.transport.backing.rows)
    reopened = open_ui(c, c.journal)
    try:
        current = reopened.ui.get("/api/agent-template/selection")
        assert current.status_code == 200 and current.json()["current"]["selection_id"] is not None
        assert current.json()["selections"][0]["state"] == "UNKNOWN"
        assert choose(c, command.model_copy(update={"request_id": "do-not-replace-original"}), reopened).status_code == 409
        response = reopened.ui.post("/api/agent-template/selection-reconcile",
                                    json={"request_id": command.request_id}, headers=reopened.headers)
        assert response.status_code == 200, response.text
        selected = reopened.ui.get("/api/state").json()["template_selections"][0]
        assert selected["state"] == "UNKNOWN" and selected["receipt"] is None
        assert selected["reconciliation"] == response.json()["receipt"]
        with sqlite3.connect(c.journal) as db:
            assert db.execute("SELECT intent, outcome FROM intents WHERE phase='template-select'").fetchone() == original
    finally:
        reopened.ui.close()
    original_body = next(body for path, body in bodies if path == PATH + "/select")
    assert [body for path, body in bodies if path == PATH + "/selection-reconcile"] == [original_body]
    assert c.calls.count(("POST", PATH + "/select")) == 1
    assert c.h.transport.backing.rows == rows and not c.h.endpoint.requests and not c.control.records


def test_unknown_uncommitted_selection_never_becomes_permission_to_resubmit(template_console):
    c = template_console
    command = selection_command(c)

    def lose(request):
        if request.url.path == PATH + "/select":
            raise httpx.ReadTimeout("OFFLINE request has no confirmed server receipt")

    c.before_send = lose
    rows = copy.deepcopy(c.h.transport.backing.rows)
    assert choose(c, command).status_code == 503
    with sqlite3.connect(c.journal) as db:
        original = db.execute("SELECT intent, outcome FROM intents WHERE phase='template-select'").fetchone()
    current = c.browser.ui.get("/api/agent-template/selection").json()
    assert current["current"]["selection_id"] is None and current["selections"][0]["state"] == "UNKNOWN"
    response = c.browser.ui.post("/api/agent-template/selection-reconcile",
        json={"request_id": command.request_id}, headers=c.browser.headers)
    assert response.json()["error"]["code"] == "PROFILE_SELECTION_UNKNOWN"
    assert choose(c, command.model_copy(update={"request_id": "not-a-new-intent"})).status_code == 409
    with sqlite3.connect(c.journal) as db:
        assert db.execute("SELECT intent, outcome FROM intents WHERE phase='template-select'").fetchone() == original
    assert c.calls.count(("POST", PATH + "/select")) == 1 and c.h.transport.backing.rows == rows


def test_two_clients_racing_the_same_selection_cannot_send_twice(template_console):
    c = template_console
    command = selection_command(c)
    other = open_ui(c, c.journal)
    entered, release = Event(), Event()

    def pause(request):
        if request.url.path == PATH + "/select":
            entered.set()
            assert release.wait(10), "Synthetic selection was not released"

    c.before_send = pause
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(choose, c, command)
            try:
                assert entered.wait(10)
                second = choose(c, command.model_copy(update={"request_id": "racing-client"}), other)
                assert second.status_code == 409
            finally:
                release.set()
            assert first.result(timeout=10).status_code == 200
    finally:
        other.ui.close()
    assert c.calls.count(("POST", PATH + "/select")) == 1
    assert len(c.browser.ui.get("/api/state").json()["template_selections"]) == 1


def test_late_client_reconcile_error_cannot_erase_another_clients_confirmed_observation(template_console):
    c = template_console
    command = selection_command(c)

    def lose(request, response):
        if request.url.path in {PATH + "/select", PATH + "/selection-reconcile"}:
            raise httpx.ReadTimeout("OFFLINE lost original response")
        return response

    c.mutate = lose
    assert choose(c, command).status_code == 503
    url, body = "/api/agent-template/selection-reconcile", {"request_id": command.request_id}
    assert c.browser.ui.post(url, json=body, headers=c.browser.headers).status_code == 503
    c.mutate = lambda request, response: response
    other = open_ui(c, c.journal)
    entered, release = Event(), Event()

    def late_error(request):
        if request.url.path == PATH + "/selection-reconcile" and not entered.is_set():
            entered.set()
            assert release.wait(10)
            raise httpx.ReadTimeout("OFFLINE late client error after another confirmed read")

    c.before_send = late_error
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            late = pool.submit(c.browser.ui.post, url, json=body, headers=c.browser.headers)
            try:
                assert entered.wait(10)
                confirmed = other.ui.post(url, json=body, headers=other.headers)
                assert confirmed.status_code == 200, confirmed.text
                with sqlite3.connect(c.journal) as db:
                    saved = db.execute(
                        "SELECT intent, outcome FROM intents WHERE phase='template-selection-reconcile'",
                    ).fetchone()
            finally:
                release.set()
            assert late.result(timeout=10).status_code == 503
    finally:
        other.ui.close()
    with sqlite3.connect(c.journal) as db:
        assert db.execute(
            "SELECT intent, outcome FROM intents WHERE phase='template-selection-reconcile'",
        ).fetchone() == saved
    observed = c.browser.ui.get("/api/state").json()["template_selections"][0]
    assert observed["state"] == "UNKNOWN" and observed["reconciliation"] == confirmed.json()["receipt"]
    assert c.calls.count(("POST", PATH + "/select")) == 1


def test_generic_refusal_or_lost_reply_stays_unknown_and_reconcilable(template_console):
    c = template_console
    command = selection_command(c, request_id="generic-refusal")

    def generic(request, response):
        if request.url.path == PATH + "/select":
            return httpx.Response(409, json={"error": {"code": "SOMETHING_ELSE"}})
        return response

    c.mutate = generic
    refused = choose(c, command)
    assert refused.json()["error"]["code"] == "RESULT_UNKNOWN"
    entry = c.browser.ui.get("/api/state").json()["template_selections"][0]
    assert (entry["request_id"], entry["state"]) == (command.request_id, "UNKNOWN")
    assert choose(c, command).json()["error"]["code"] == "TEMPLATE_SELECTION_ALREADY_ATTEMPTED"
    assert c.calls.count(("POST", PATH + "/select")) == 1


def test_server_cas_rejects_stale_choice_and_fresh_distinct_choice_keeps_old_intents(template_console):
    c = template_console
    first = selection_command(c)
    selected = choose(c, first)
    assert selected.status_code == 200
    sid = selected.json()["receipt"]["result"]["selection_id"]
    stale = selection_command(c, request_id="stale-human-choice", profile="fa-instruction-skill-resources-v1")
    before = copy.deepcopy(c.h.transport.backing.rows)
    refused = choose(c, stale)
    assert refused.json()["error"]["code"] == "PROFILE_SELECTION_CONFLICT"
    assert refused.json()["error"]["upstream_status"] == 409
    assert c.h.transport.backing.rows == before
    observed = c.browser.ui.get("/api/agent-template/selection").json()
    assert observed["current"]["selection_id"] == sid
    states = {item["request_id"]: item["state"] for item in observed["selections"]}
    assert states == {first.request_id: "CONFIRMED", stale.request_id: "REJECTED"}
    with sqlite3.connect(c.journal) as db:
        outcome = json.loads(db.execute("SELECT outcome FROM intents WHERE phase='template-select' AND outcome LIKE ?",
                                        ("%PROFILE_SELECTION_CONFLICT%",)).fetchone()[0])
    assert (outcome["state"], outcome["code"], outcome["upstream_status"]) == ("REJECTED", "PROFILE_SELECTION_CONFLICT", 409)
    # An older journal recorded the same refusal as UNKNOWN; it is read back as rejected without rewriting the row.
    with sqlite3.connect(c.journal) as db:
        db.execute("UPDATE intents SET outcome=? WHERE phase='template-select' AND outcome LIKE ?",
                   (json.dumps({**outcome, "state": "UNKNOWN"}), "%PROFILE_SELECTION_CONFLICT%"))
    reread = {item["request_id"]: item["state"] for item in c.browser.ui.get("/api/state").json()["template_selections"]}
    assert reread[stale.request_id] == "REJECTED"
    with sqlite3.connect(c.journal) as db:
        originals = db.execute("SELECT task_id, phase, intent, outcome FROM intents ORDER BY rowid").fetchall()
    assert choose(c, stale).json()["error"]["code"] == "TEMPLATE_SELECTION_ALREADY_ATTEMPTED"
    fresh = stale.model_copy(update={"request_id": "fresh-human-choice", "expected_selection_id": sid})
    assert choose(c, fresh).status_code == 200
    with sqlite3.connect(c.journal) as db:
        assert db.execute("SELECT task_id, phase, intent, outcome FROM intents ORDER BY rowid").fetchall()[:2] == originals
    assert len(c.browser.ui.get("/api/state").json()["template_selections"]) == 3
    assert not c.control.records and not c.h.endpoint.requests


@pytest.mark.parametrize("authority", ["read-only", "revoked", "forged-role", "missing-skill"])
def test_inspection_does_not_replace_current_server_selection_authority(template_console, authority):
    c = template_console
    command = selection_command(c)
    assert c.browser.ui.post("/api/agent-template/inspect", json=command.template.model_dump(mode="json"),
                             headers=c.browser.headers).status_code == 200
    if authority == "read-only":
        c.browser.app.state.console.vault.set(c.h.headers("read-only")["Authorization"].removeprefix("Bearer "))
    elif authority == "revoked":
        c.control.revoke(c.human, "revoke-ui-selection")
    elif authority == "forged-role":
        command = command.model_copy(update={"template": command.template.model_copy(update={
            "role": command.template.role.model_copy(update={"prefix_sha256": "f" * 64}),
        })})
    else:
        from shared_context.behavior_agent_template import TemplateSkill
        command = selection_command(c, profile="fa-instruction-skill-purpose-v1")
        command = command.model_copy(update={"template": command.template.model_copy(update={"skills": (
            TemplateSkill(name="not-approved", version_id="f" * 32, sha256="f" * 64),
        )})})
    before = copy.deepcopy(c.h.transport.backing.rows)
    response = choose(c, command)
    assert response.status_code == 409
    assert response.json()["error"]["upstream_status"] in {403, 409, 422}
    # Profile-selection refusals and every status /select raises before its commit read back as rejected.
    from web_test_console.agent_template import rejected_before_write
    error = response.json()["error"]
    expected = "REJECTED" if rejected_before_write(error["code"], error["upstream_status"]) else "UNKNOWN"
    assert expected == "REJECTED"
    assert c.browser.ui.get("/api/state").json()["template_selections"][0]["state"] == expected
    assert c.h.transport.backing.rows == before and not c.h.endpoint.requests
    assert choose(c, command).status_code == 409 and c.calls.count(("POST", PATH + "/select")) == 1


def test_a_masked_pre_write_refusal_reads_back_as_rejected_even_from_an_older_journal(template_console, monkeypatch):
    """dev 2026-10-06: a 422 whose code the adapter masked as BACKEND_UNAVAILABLE was kept UNKNOWN for days, and the
    API's reconcile (no row was ever written) can only answer PROFILE_SELECTION_UNKNOWN."""
    from web_test_console import agent_template
    c = template_console
    command = selection_command(c, request_id="ui-template-masked-422")

    def refused(request, response):
        if request.url.path == PATH + "/select":
            return httpx.Response(422, json={"error": {"code": "BEHAVIOR_INVALID_REQUEST", "message": "invalid"}},
                                  headers={"Cache-Control": "no-store"})
        return response
    c.mutate = refused
    # The console that wrote this entry only knew the listed codes, as the dev journal's did.
    monkeypatch.setattr(agent_template, "rejected_before_write",
                        lambda code, status: code in agent_template.REJECTED_BEFORE_WRITE and status in {403, 409, 422})
    response = choose(c, command)
    assert response.status_code == 409 and response.json()["error"]["code"] == "BACKEND_UNAVAILABLE"
    with sqlite3.connect(c.journal) as db:
        (outcome,) = db.execute("SELECT outcome FROM intents WHERE phase='template-select'").fetchone()
    assert json.loads(outcome)["state"] == "UNKNOWN" and json.loads(outcome)["upstream_status"] == 422
    monkeypatch.undo()
    entry = c.browser.ui.get("/api/state").json()["template_selections"][0]
    assert entry["request_id"] == "ui-template-masked-422" and entry["state"] == "REJECTED"
    assert c.calls.count(("POST", PATH + "/selection-reconcile")) == 0


@pytest.mark.parametrize("status,expected", [(400, "REJECTED"), (401, "REJECTED"), (403, "REJECTED"),
                                             (409, "UNKNOWN"), (412, "UNKNOWN"), (429, "UNKNOWN"), (503, "UNKNOWN")])
def test_only_statuses_raised_before_the_commit_mark_an_unlisted_refusal_rejected(status, expected):
    from web_test_console.agent_template import rejected_before_write
    assert ("REJECTED" if rejected_before_write("BACKEND_UNAVAILABLE", status) else "UNKNOWN") == expected


@pytest.mark.parametrize("damage,status", [
    ("extra-field", 400), ("duplicate-key", 400), ("missing-expected", 400),
    ("grant", 400), ("oversized", 413), ("nonfinite", 400),
])
def test_invalid_selection_commands_never_reach_backend_or_journal(template_console, damage, status):
    c = template_console
    body = selection_command(c).model_dump(mode="json")
    if damage == "extra-field":
        body["credentials"] = "PRIVATE_SELECTION_CANARY"
    if damage == "missing-expected":
        del body["expected_selection_id"]
    if damage == "grant":
        body["template"]["authorization"] = True
    raw = json.dumps(body).encode()
    if damage == "duplicate-key":
        raw = b'{"expected_selection_id":null,' + raw[1:]
    if damage == "oversized":
        raw = raw.ljust(8705, b" ")
    if damage == "nonfinite":
        raw = raw.replace(b'"expected_selection_id": null', b'"expected_selection_id": NaN')
    journal = c.journal.read_bytes()
    response = c.browser.ui.post("/api/agent-template/select", content=raw,
        headers={**c.browser.headers, "Content-Type": "application/json"})
    assert response.status_code == status and response.json()["error"]["code"] == "TEMPLATE_SELECTION_INVALID"
    assert "PRIVATE_SELECTION_CANARY" not in response.text
    assert c.calls == [] and c.journal.read_bytes() == journal


def test_selection_command_accepts_the_exact_local_byte_limit(template_console):
    c = template_console
    raw = selection_command(c).model_dump_json().encode().ljust(8704, b" ")
    assert len(raw) == 8704
    response = c.browser.ui.post("/api/agent-template/select", content=raw,
        headers={**c.browser.headers, "Content-Type": "application/json"})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("boundary", ["host", "origin", "cookie", "csrf", "query", "suffix", "get-body", "get-query"])
def test_selection_routes_preserve_exact_local_boundaries(template_console, boundary):
    c = template_console
    headers = {**c.browser.headers, "Content-Type": "application/json"}
    path = "/api/agent-template/select"
    body = selection_command(c).model_dump_json().encode()
    if boundary == "host":
        headers["Host"] = "another.example"
    elif boundary == "origin":
        headers["Origin"] = "https://another.example"
    elif boundary == "cookie":
        headers["Cookie"] = "fa_console=wrong"
    elif boundary == "csrf":
        headers["X-Console-CSRF"] = "wrong"
    elif boundary == "query":
        path += "?select=true"
    elif boundary == "suffix":
        path += "/extra"
    journal = c.journal.read_bytes()
    if boundary.startswith("get-"):
        response = c.browser.ui.request("GET", "/api/agent-template/selection" + ("?current=true" if boundary == "get-query" else ""),
                                        content=body if boundary == "get-body" else None)
    else:
        response = c.browser.ui.post(path, content=body, headers=headers)
    assert response.status_code == 403 and response.json()["error"]["code"] == "LOCAL_REQUEST_DENIED"
    assert c.calls == [] and c.journal.read_bytes() == journal


@pytest.mark.parametrize("damage", ["control-id", "control-digest", "template-digest", "permissions", "missing-field", "duplicate-key"])
def test_invalid_selection_receipt_is_not_presented_as_confirmed(template_console, damage):
    c = template_console
    command = selection_command(c)

    def malformed(request, response):
        if request.url.path != PATH + "/select":
            return response
        assert response.status_code == 200
        body = response.json()
        if damage == "control-id":
            body["control_request_id"] = "f" * 64
        if damage == "control-digest":
            body["control_request_digest"] = "f" * 64
        if damage == "template-digest":
            body["result"]["template_sha256"] = "f" * 64
        if damage == "permissions":
            body["result"]["permissions_changed"] = True
        if damage == "missing-field":
            del body["result"]["existing_conversations_changed"]
        raw = json.dumps(body).encode()
        if damage == "duplicate-key":
            raw = b'{"control_request_id":"' + body["control_request_id"].encode() + b'",' + raw[1:]
        return httpx.Response(200, content=raw, headers={"Content-Type": "application/json", "Cache-Control": "no-store"})

    c.mutate = malformed
    response = choose(c, command)
    assert response.status_code == 502 and response.json()["error"]["code"] == "TEMPLATE_SELECTION_RESPONSE_INVALID"
    entry = c.browser.ui.get("/api/state").json()["template_selections"][0]
    assert entry["state"] == "UNKNOWN" and entry["receipt"] is None
    assert c.calls.count(("POST", PATH + "/select")) == 1


@pytest.mark.parametrize("damage", ["template-digest", "missing-field", "cache"])
def test_invalid_current_selection_cannot_be_used_as_a_fresh_observation(template_console, damage):
    c = template_console
    assert choose(c, selection_command(c)).status_code == 200
    c.calls.clear()

    def malformed(request, response):
        if request.url.path != PATH + "/selection":
            return response
        body = response.json()
        if damage == "template-digest":
            body["template_sha256"] = "f" * 64
        if damage == "missing-field":
            del body["permissions_changed"]
        return httpx.Response(200, json=body, headers={"Cache-Control": "public" if damage == "cache" else "no-store"})

    c.mutate = malformed
    journal = c.journal.read_bytes()
    response = c.browser.ui.get("/api/agent-template/selection")
    assert response.status_code == 502 and response.json()["error"]["code"] == "TEMPLATE_SELECTION_RESPONSE_INVALID"
    assert c.calls == [("GET", PATH + "/selection")] and c.journal.read_bytes() == journal


@pytest.mark.parametrize("reader", ["selection", "selection-reconcile"])
def test_older_selection_readers_are_unavailable_without_fallback(template_console, reader):
    c = template_console
    command = selection_command(c)
    if reader == "selection-reconcile":
        assert choose(c, command).status_code == 200
    c.calls.clear()
    rows = copy.deepcopy(c.h.transport.backing.rows)
    c.mutate = lambda request, response: httpx.Response(404, json={"detail": "Not Found"})
    if reader == "selection":
        response = c.browser.ui.get("/api/agent-template/selection")
    else:
        response = c.browser.ui.post("/api/agent-template/selection-reconcile",
            json={"request_id": command.request_id}, headers=c.browser.headers)
    assert response.status_code == 409 and response.json()["error"]["code"] == "TEMPLATE_SELECTION_UNAVAILABLE"
    assert response.json()["error"]["upstream_status"] == 404
    assert c.calls == [("GET" if reader == "selection" else "POST", PATH + "/" + reader)]
    assert c.h.transport.backing.rows == rows and not c.h.endpoint.requests


def test_reconciliation_never_accepts_a_replacement_command_from_the_browser(template_console):
    c = template_console
    command = selection_command(c)
    assert choose(c, command).status_code == 200
    c.calls.clear()
    journal = c.journal.read_bytes()
    response = c.browser.ui.post("/api/agent-template/selection-reconcile",
        json={"request_id": command.request_id, "expected_selection_id": None, "template": {}}, headers=c.browser.headers)
    assert response.status_code == 400 and c.calls == [] and c.journal.read_bytes() == journal
    missing = c.browser.ui.post("/api/agent-template/selection-reconcile",
        json={"request_id": "not-saved-here"}, headers=c.browser.headers)
    assert missing.json()["error"]["code"] == "TEMPLATE_SELECTION_ORIGINAL_REQUIRED" and c.calls == []


def test_incomplete_original_command_cannot_be_reconstructed_and_other_controls_stay_available(template_console):
    c = template_console
    command = selection_command(c)
    assert choose(c, command).status_code == 200
    with sqlite3.connect(c.journal) as db:
        key, encoded = db.execute("SELECT task_id, intent FROM intents WHERE phase='template-select'").fetchone()
        intent = json.loads(encoded)
        del intent["body"]["template"]["authorization"]
        db.execute("UPDATE intents SET intent=? WHERE task_id=? AND phase='template-select'", (json.dumps(intent), key))
    c.calls.clear()
    response = c.browser.ui.post("/api/agent-template/selection-reconcile",
        json={"request_id": command.request_id}, headers=c.browser.headers)
    assert response.status_code == 502 and response.json()["error"]["code"] == "TEMPLATE_SELECTION_ORIGINAL_INVALID"
    assert c.calls == []
    state = c.browser.ui.get("/api/state")
    assert state.status_code == 200 and state.json()["tasks"] == []
    assert state.json()["template_selection_error"]["code"] == "TEMPLATE_SELECTION_ORIGINAL_INVALID"


def test_selection_keeps_legacy_intent_rows_and_config_bytes(template_console):
    c = template_console
    command = selection_command(c)
    with sqlite3.connect(c.journal) as db:
        db.execute("INSERT INTO intents VALUES (?, ?, ?, ?)",
                   ("legacy-original", "acknowledge", '{"request_id":"legacy-ack"}', '{"state":"UNKNOWN"}'))
        legacy = db.execute("SELECT * FROM intents").fetchall()
        config = db.execute("SELECT * FROM config").fetchall()
        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    assert choose(c, command).status_code == 200
    with sqlite3.connect(c.journal) as db:
        assert db.execute("SELECT * FROM intents WHERE task_id='legacy-original'").fetchall() == legacy
        assert db.execute("SELECT * FROM config").fetchall() == config
        assert db.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() == schema


def test_inspection_reports_changed_references_without_applying_them(template_console):
    c = template_console
    original = c.h.client.get(PATH, headers=c.h.headers()).json()
    original["skills"][0]["version_id"] = "f" * 32
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    result = c.browser.ui.post("/api/agent-template/inspect", json=original, headers=c.browser.headers)
    assert result.status_code == 200, result.text
    assert result.json()["references_match"] is False
    assert result.json()["skills"] == [{"name": "receipt-guide", "status": "version_changed"}]
    assert result.json()["authorization"] is False and result.json()["activated"] is False
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal


@pytest.mark.parametrize("change,status", [
    ("unknown_schema", 400), ("credential", 400), ("duplicate_key", 400), ("oversized", 413),
])
def test_invalid_files_do_not_reach_the_backend(template_console, change, status):
    c = template_console
    template = c.h.client.get(PATH, headers=c.h.headers()).json()
    if change == "unknown_schema":
        template["schema_version"] = "future"
    if change == "credential":
        template["credential"] = "PRIVATE_TEMPLATE_INPUT"
    raw = json.dumps(template).encode()
    if change == "duplicate_key":
        raw = b'{"authorization":false,' + raw[1:]
    if change == "oversized":
        raw += b" " * 8192
    before = c.journal.read_bytes()
    result = c.browser.ui.post("/api/agent-template/inspect", content=raw,
        headers={**c.browser.headers, "Content-Type": "application/json"})
    assert result.status_code == status and result.json()["error"]["code"] == "TEMPLATE_INVALID"
    assert "PRIVATE_TEMPLATE_INPUT" not in result.text
    assert c.calls == [] and c.journal.read_bytes() == before


@pytest.mark.parametrize("boundary", ["csrf", "origin", "query", "download_body"])
def test_template_routes_keep_the_original_local_boundary(template_console, boundary):
    c = template_console
    template = c.h.client.get(PATH, headers=c.h.headers()).json()
    headers = dict(c.browser.headers)
    path = "/api/agent-template/inspect"
    if boundary == "csrf":
        headers["X-Console-CSRF"] = "wrong"
    elif boundary == "origin":
        headers["Origin"] = "https://another.example"
    elif boundary == "query":
        path += "?team_id=another-team"
    if boundary == "download_body":
        result = c.browser.ui.request("GET", "/api/agent-template", content=b'{"include_history":true}')
    else:
        result = c.browser.ui.post(path, json=template, headers=headers)
    assert result.status_code == 403 and result.json()["error"]["code"] == "LOCAL_REQUEST_DENIED"
    assert c.calls == []


def test_login_and_current_read_authority_are_not_replaced_by_an_exported_file(template_console):
    c = template_console
    response = c.browser.ui.get("/api/agent-template")
    assert response.status_code == 200
    template = response.content
    c.browser.app.state.console.vault.clear()
    c.calls.clear()
    assert c.browser.ui.get("/api/agent-template").status_code == 401
    assert c.calls == []
    assert c.browser.ui.post("/api/authenticate", json={}, headers=c.browser.headers).status_code == 200
    c.control.revoke(c.human, "revoke-template-read")
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    denied = c.browser.ui.post("/api/agent-template/inspect", content=template,
        headers={**c.browser.headers, "Content-Type": "application/json"})
    assert denied.json()["error"]["code"] == "BACKEND_DENIED"
    assert denied.json()["error"]["upstream_status"] == 403
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal


@pytest.mark.parametrize("status", [404, 409])
def test_older_or_changed_template_source_is_explicitly_unavailable(template_console, status):
    c = template_console
    c.mutate = lambda request, response: httpx.Response(status, json={"error": {"code": "AGENT_TEMPLATE_PROFILE_UNSUPPORTED"}})
    response = c.browser.ui.get("/api/agent-template")
    assert response.status_code == 409 and response.json()["error"]["code"] == "TEMPLATE_UNAVAILABLE"
    assert response.json()["error"]["upstream_status"] == status
    assert "content-disposition" not in response.headers


@pytest.mark.parametrize("change", ["digest", "private_content", "grant", "wrong_input", "false_match", "missing_skill"])
def test_invalid_backend_templates_or_inspections_are_not_presented_as_valid(template_console, change):
    c = template_console
    template = c.h.client.get(PATH, headers=c.h.headers()).json()
    export = change in {"digest", "private_content", "grant"}
    target = PATH if export else PATH + "/inspect"

    def malformed(request, response):
        if request.url.path != target or response.status_code != 200:
            return response
        data = response.json()
        if change == "private_content":
            data["history"] = "PRIVATE_TEMPLATE_RESPONSE"
        elif change == "grant":
            data["authorization"] = True
        elif change == "wrong_input":
            data["template_sha256"] = "f" * 64
        elif change == "false_match":
            data["references_match"] = False
        elif change == "missing_skill":
            data["skills"] = []
        raw = json.dumps(data).encode()
        return httpx.Response(200, content=raw, headers={
            "Content-Type": "application/json", "Cache-Control": "no-store",
            "x-content-sha256": "f" * 64 if change == "digest" else hashlib.sha256(raw).hexdigest(),
        })

    c.mutate = malformed
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    if export:
        response = c.browser.ui.get("/api/agent-template")
    else:
        response = c.browser.ui.post("/api/agent-template/inspect", json=template, headers=c.browser.headers)
    assert response.status_code == 502 and response.json()["error"]["code"] == "TEMPLATE_RESPONSE_INVALID"
    assert "PRIVATE_TEMPLATE_RESPONSE" not in response.text
    assert "content-disposition" not in response.headers
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal


def test_template_options_are_a_pure_signed_read_through_the_local_console(template_console):
    from shared_context.behavior_capabilities import available_grants
    c = template_console
    c.h.runtime.capability_grants = available_grants(None, ("fa-skills-v1",))
    rows, artifacts = copy.deepcopy(c.h.transport.backing.rows), copy.deepcopy(c.h.artifacts.repository.data)
    journal, config = c.journal.read_bytes(), c.config.model_dump_json()
    response = c.browser.ui.get("/api/agent-template/options")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["schema_version"] == "fa-agent-template-options-v1"
    assert value["authorization"] is False and value["activated"] is False
    assert [item["entry_id"] for item in value["capabilities"]] == ["fa-skills-v1"]
    assert value["capabilities"][0]["entry_sha256"] == c.h.runtime.capability_grants["fa-skills-v1"].entry_sha256
    exported = c.h.client.get(PATH, headers=c.h.headers()).json()
    assert {item["name"] for item in exported["skills"]} <= {item["name"] for item in value["skills"]}
    assert c.calls == [("GET", PATH + "/options")]
    assert c.h.transport.backing.rows == rows and c.h.artifacts.repository.data == artifacts
    assert c.journal.read_bytes() == journal and c.config.model_dump_json() == config
    assert not c.control.records and not c.h.endpoint.requests
    for bad in ({"params": {"x": "1"}},):
        assert c.browser.ui.get("/api/agent-template/options", **bad).status_code == 403


def test_template_options_larger_than_a_template_are_still_readable(template_console):
    from shared_context.behavior_capabilities import available_grants
    from shared_context.tool_catalog import build_catalog, parse_entry_file
    c = template_console
    entries = [parse_entry_file(f"tool{index}.json", json.dumps({"mcpServers": {f"tool{index:02d}": {
        "type": "stdio", "command": f"/opt/fa-tools/bin/tool{index:02d}",
        "tools": [f"action_number_{tool:02d}" for tool in range(30)]}}}).encode()) for index in range(12)]
    c.h.runtime.capability_grants = available_grants(build_catalog(entries), ("fa-skills-v1",))
    response = c.browser.ui.get("/api/agent-template/options")
    assert response.status_code == 200, response.text
    assert len(response.content) > 8192 and len(response.json()["capabilities"]) == 13


def test_system_prompt_is_a_pure_signed_read_of_the_server_rebuild(template_console, monkeypatch):
    from shared_context.behavior_native_turn_wire import NATIVE_STATE_PROFILE
    from shared_context.behavior_system_prompt_view import check_current_view, CurrentSystemPrompt
    c = template_console
    monkeypatch.setenv("FA_NATIVE_TURNS", NATIVE_STATE_PROFILE)
    original = c.h.client.get(PATH + "/system-prompt", headers=c.h.headers())
    assert original.status_code == 200, original.text
    rows, artifacts = copy.deepcopy(c.h.transport.backing.rows), copy.deepcopy(c.h.artifacts.repository.data)
    journal, config = c.journal.read_bytes(), c.config.model_dump_json()
    response = c.browser.ui.get("/api/agent-template/system-prompt")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    view = check_current_view(CurrentSystemPrompt.model_validate_json(json.dumps(response.json()["system_prompt"])))
    assert view.model_dump(mode="json") == original.json()
    assert view.authorization is False and view.permissions_changed is False
    assert view.profile == c.h.client.get(PATH, headers=c.h.headers()).json()["role"]["profile"]
    assert c.calls == [("GET", PATH + "/system-prompt")]
    assert c.h.transport.backing.rows == rows and c.h.artifacts.repository.data == artifacts
    assert c.journal.read_bytes() == journal and c.config.model_dump_json() == config
    assert not c.control.records and not c.h.endpoint.requests
    assert c.browser.ui.get("/api/agent-template/system-prompt", params={"x": "1"}).status_code == 403


@pytest.mark.parametrize("change", ["text", "segment", "grant", "old_api", "unreconstructable"])
def test_system_prompt_is_not_shown_when_the_server_view_does_not_check_out(template_console, monkeypatch, change):
    from shared_context.behavior_native_turn_wire import NATIVE_STATE_PROFILE
    c = template_console
    monkeypatch.setenv("FA_NATIVE_TURNS", NATIVE_STATE_PROFILE)

    def changed(request, response):
        if request.url.path != PATH + "/system-prompt":
            return response
        if change == "old_api":
            return httpx.Response(404, json={"detail": "Not Found"})
        if change == "unreconstructable":
            return httpx.Response(409, json={"error": {"code": "SYSTEM_PROMPT_UNRECONSTRUCTABLE"}})
        data = response.json()
        if change == "text":
            data["text"] += "PRIVATE_EXTRA_TEXT"
        elif change == "segment":
            data["segments"][0]["text"] += "PRIVATE_EXTRA_TEXT"
        else:
            data["authorization"] = True
        return httpx.Response(200, content=json.dumps(data).encode(), headers={
            "Content-Type": "application/json", "Cache-Control": "no-store"})

    c.mutate = changed
    response = c.browser.ui.get("/api/agent-template/system-prompt")
    expected = {"old_api": (409, "SYSTEM_PROMPT_UNAVAILABLE"),
                "unreconstructable": (409, "SYSTEM_PROMPT_UNRECONSTRUCTABLE")}.get(
        change, (502, "SYSTEM_PROMPT_RESPONSE_INVALID"))
    assert (response.status_code, response.json()["error"]["code"]) == expected
    assert "PRIVATE_EXTRA_TEXT" not in response.text
