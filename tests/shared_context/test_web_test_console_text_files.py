"""Literal file UI seams on signed APIs and synthetic journals; no runtime or real network."""

import copy
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
from threading import Event

import httpx
import pytest

from shared_context.behavior_instruction import conversation_key
from shared_context.behavior_text_files import UserTextFileCapability
from test_behavior_assistant import assistant
from test_conversation_profile_selection import select, template
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_web_test_console_attachments import draft, picker, submit as submit_reference
from test_web_test_console_frozen_interface import open_ui


def test_scoped_file_capability_reads_captured_profile_without_writes_or_content(picker, monkeypatch):
    c = picker
    selected, _ = select(c.h, value=template(user_text_file_profile="fa-user-text-files-v1"))
    assert selected.status_code == 200
    accepted = submit_reference(c)
    assert accepted.status_code == 200
    cid = accepted.json()["task"]["admission"]["conversation_id"]
    changed, _ = select(c.h, request_id="change-current-file-profile",
                        previous=selected.json()["result"]["selection_id"], value=template())
    assert changed.status_code == 200
    for name in ("read_exact", "read_graph"):
        monkeypatch.setattr(c.h.sources, name, lambda *args, **kwargs: pytest.fail("Capability must not read source content"))
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    response = c.h.client.get(f"/work-policy/v1/conversations/{cid}/user-text-file-capability",
                              headers=c.h.headers("read-only"))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "schema_version": "fa-user-text-file-capability-v1", "conversation_id": cid,
        "read_at": c.h.time.isoformat().replace("+00:00", "Z"),
        "user_text_file_profile": "fa-user-text-files-v1", "authorization": False,
    }
    assert response.headers["cache-control"] == "no-store"
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
    assert not c.h.endpoint.requests


def enable_files(c):
    response, _ = select(c.h, value=template(
        user_text_file_profile="fa-user-text-files-v1", text_attachment_profile="fa-source-text-attachments-v1",
    ))
    assert response.status_code == 200, response.text
    return response.json()["result"]["selection_id"]


def capability(c, request_id=None, browser=None):
    browser = browser or c.browser
    rid = request_id or draft(c, browser)["request_id"]
    return browser.ui.get(f"/api/instructions/{rid}/user-text-file-capability")


def send_files(c, files, *, content="Read only this input.", attachments=(), request_id=None, browser=None):
    browser = browser or c.browser
    rid = request_id or draft(c, browser)["request_id"]
    return browser.ui.post(f"/api/instructions/{rid}/submit", json={
        "content": content, "model": "gpt-4.1-mini", "files": files, "attachments": list(attachments),
    }, headers=browser.headers)


def test_selected_literal_bytes_freeze_with_original_command_and_download_through_current_read(picker):
    c = picker
    enable_files(c)
    rid = draft(c)["request_id"]
    text = "\ufeff  name,value\r\nliteral,\ufffd🙂\r\n"
    files = [{"name": "selected.csv", "text": text}]
    response = send_files(c, files)
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    with sqlite3.connect(c.journal) as db:
        body = json.loads(db.execute("SELECT command FROM instruction_commands WHERE request_id=?", (rid,)).fetchone()[0])
        intent = json.loads(db.execute("SELECT intent FROM intents WHERE task_id=? AND phase='submit'", (rid,)).fetchone()[0])
    assert body["files"] == files == intent["body"]["files"]
    wire = next(raw for _, path, raw in c.calls if path == "/work-policy/v1/submit")
    assert intent["body_sha256"] == hashlib.sha256(wire).hexdigest()
    assert task["user_files"] == [{
        "name": "selected.csv", "sha256": hashlib.sha256(text.encode()).hexdigest(), "utf8_bytes": len(text.encode()),
    }]
    assert "literal," not in c.browser.ui.get("/api/state").text
    c.calls.clear()
    result = c.browser.ui.get(f"/api/instructions/{rid}/files/selected.csv")
    assert result.status_code == 200, result.text
    assert result.content == text.encode("utf-8")
    assert result.headers["x-content-sha256"] == task["user_files"][0]["sha256"]
    assert result.headers["content-disposition"] == 'attachment; filename="selected.csv"'
    assert c.calls == [("GET",
        f"/work-policy/v1/conversations/{task['admission']['conversation_id']}/turns/1/files/selected.csv", b"")]
    changed = send_files(c, [{"name": "selected.csv", "text": "different"}], request_id=rid)
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "INSTRUCTION_CHANGED"
    c.control.revoke(c.human, "withdraw-read-after-original-file-download")
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    denied = c.browser.ui.get(f"/api/instructions/{rid}/files/selected.csv")
    assert denied.status_code == 409 and denied.json()["error"]["upstream_status"] == 403
    assert denied.json()["error"]["code"] == "BACKEND_DENIED"
    assert "content-disposition" not in denied.headers
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal


@pytest.mark.parametrize("enabled", [False, True])
def test_bridge_capability_separates_effective_new_template_and_captured_conversation(picker, enabled):
    c = picker
    chosen, _ = select(c.h, value=template(**({"user_text_file_profile": "fa-user-text-files-v1"} if enabled else {})))
    assert chosen.status_code == 200
    before = c.journal.read_bytes()
    first = capability(c)
    assert first.status_code == 200, first.text
    assert first.json()["conversation_id"] is None
    assert first.json()["user_text_file_profile"] == ("fa-user-text-files-v1" if enabled else None)
    assert c.calls == [("GET", "/work-policy/v1/agent-template", b"")] and c.journal.read_bytes() == before
    admitted = submit_reference(c).json()["task"]
    cid = admitted["admission"]["conversation_id"]
    changed, _ = select(c.h, request_id="replace-global-setting", previous=chosen.json()["result"]["selection_id"],
                        value=template(**({} if enabled else {"user_text_file_profile": "fa-user-text-files-v1"})))
    assert changed.status_code == 200
    c.calls.clear()
    response = capability(c)
    assert response.status_code == 200, response.text
    assert response.json()["conversation_id"] == cid
    assert response.json()["user_text_file_profile"] == ("fa-user-text-files-v1" if enabled else None)
    assert response.json()["authorization"] is False
    assert c.calls == [("GET", f"/work-policy/v1/conversations/{cid}/user-text-file-capability", b"")]


def test_new_capability_can_observe_policy_setting_without_any_selection_record(picker):
    c = picker
    assert c.h.client.get("/work-policy/v1/agent-template/selection", headers=c.h.headers()).json()["selection_id"] is None
    result = capability(c)
    assert result.status_code == 200 and result.json()["user_text_file_profile"] is None
    assert c.calls == [("GET", "/work-policy/v1/agent-template", b"")]


def test_new_capability_observes_enabled_policy_without_a_selection_record(assistant, request):
    from types import MappingProxyType
    from shared_context.behavior_assistant import AssistantProfile

    assistant.runtime.profiles = MappingProxyType({
        key: AssistantProfile.model_validate({
            **profile.model_dump(), "prompt_profile": "fa-instruction-v1",
            "user_text_file_profile": "fa-user-text-files-v1",
        }) for key, profile in assistant.runtime.profiles.items()
    })
    c = request.getfixturevalue("picker")
    assert c.control.policy.profile.user_text_file_profile == "fa-user-text-files-v1"
    selection = c.h.client.get("/work-policy/v1/agent-template/selection", headers=c.h.headers())
    assert selection.status_code == 200 and selection.json()["selection_id"] is None
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    response = capability(c)
    assert response.status_code == 200, response.text
    assert response.json()["conversation_id"] is None
    assert response.json()["user_text_file_profile"] == "fa-user-text-files-v1"
    assert response.json()["authorization"] is False
    assert c.calls == [("GET", "/work-policy/v1/agent-template", b"")]
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal


@pytest.mark.parametrize("field", ["schema_version", "conversation_id", "read_at", "user_text_file_profile", "authorization"])
def test_capability_required_fields_cannot_default_missing_data_to_disabled(field):
    body = {"schema_version": "fa-user-text-file-capability-v1", "conversation_id": "original-cid",
            "read_at": "2026-09-27T12:00:00Z", "user_text_file_profile": None, "authorization": False}
    assert UserTextFileCapability.model_validate_json(json.dumps(body)).user_text_file_profile is None
    del body[field]
    with pytest.raises(ValueError):
        UserTextFileCapability.model_validate_json(json.dumps(body))

@pytest.mark.parametrize("authorization", [True, 0, "false"])
def test_capability_authorization_is_an_exact_false_boolean(authorization):
    with pytest.raises(ValueError):
        UserTextFileCapability.model_validate_json(json.dumps({
            "schema_version": "fa-user-text-file-capability-v1", "conversation_id": "original-cid",
            "read_at": "2026-09-27T12:00:00Z", "user_text_file_profile": None, "authorization": authorization,
        }))


@pytest.mark.parametrize("damage", ["missing-profile", "missing-auth", "auth", "schema", "cid", "profile", "404"])
def test_bridge_rejects_missing_or_mismatched_capability_without_global_fallback(picker, damage):
    c = picker
    task = submit_reference(c).json()["task"]
    cid = task["admission"]["conversation_id"]
    c.calls.clear()

    def malformed(request, response):
        if damage == "404":
            return httpx.Response(404, json={"detail": "Not Found"})
        body = response.json()
        if damage == "missing-profile":
            del body["user_text_file_profile"]
        elif damage == "missing-auth":
            del body["authorization"]
        elif damage == "auth":
            body["authorization"] = True
        elif damage == "schema":
            body["schema_version"] = "unrecognized"
        elif damage == "cid":
            body["conversation_id"] = "another-cid"
        else:
            body["user_text_file_profile"] = "unknown-profile"
        return httpx.Response(200, json=body, headers={"Cache-Control": "no-store"})

    c.mutate = malformed
    before = c.journal.read_bytes()
    response = capability(c)
    assert response.status_code == (409 if damage == "404" else 502)
    assert response.json()["error"]["code"] == (
        "USER_FILE_CAPABILITY_UNAVAILABLE" if damage == "404" else "USER_FILE_CAPABILITY_INVALID")
    assert c.calls == [("GET", f"/work-policy/v1/conversations/{cid}/user-text-file-capability", b"")]
    assert c.journal.read_bytes() == before


@pytest.mark.parametrize("damage,expected", [
    ("archived", 409), ("no-sid", 409), ("wrong-human", 403), ("unknown-cid", 404), ("revoked", 403),
])
def test_scoped_capability_preserves_archived_legacy_owner_and_current_read_checks(picker, damage, expected):
    c = picker
    task = submit_reference(c).json()["task"]
    cid = task["admission"]["conversation_id"]
    if damage == "archived":
        c.h.mutate(conversation_key(cid), reset_request_id="synthetic-archive",
                   archived_at=c.h.time.isoformat(), active_job_id=None)
    elif damage == "no-sid":
        c.h.mutate(conversation_key(cid), hosted_session_id=None)
    elif damage == "wrong-human":
        c.h.mutate(conversation_key(cid), human_oid="another-human")
    elif damage == "unknown-cid":
        cid = "not-this-original"
    else:
        c.control.revoke(c.human, "revoke-capability-read")
    rows = copy.deepcopy(c.h.transport.backing.rows)
    response = c.h.client.get(f"/work-policy/v1/conversations/{cid}/user-text-file-capability",
                              headers=c.h.headers("read-only"))
    assert response.status_code == expected, response.text
    assert "user_text_file_profile" not in response.json()
    assert c.h.transport.backing.rows == rows


def test_scoped_capability_rechecks_a_concurrent_revocation_and_does_not_nest_reads(picker, monkeypatch):
    c = picker
    task = submit_reference(c).json()["task"]
    cid = task["admission"]["conversation_id"]
    original = c.control.runtime.authorize
    changed = False

    def revoke_after_read(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            Context().run(c.control.revoke, c.human, "concurrent-file-revocation")
        return result

    monkeypatch.setattr(c.control.runtime, "authorize", revoke_after_read)
    response = c.h.client.get(f"/work-policy/v1/conversations/{cid}/user-text-file-capability", headers=c.h.headers())
    assert changed and response.status_code in {403, 409, 412}
    assert "user_text_file_profile" not in response.json()


@pytest.mark.parametrize("variant", ["query", "body", "agent"])
def test_scoped_capability_is_only_the_existing_human_read_route(picker, variant):
    c = picker
    cid = submit_reference(c).json()["task"]["admission"]["conversation_id"]
    url = f"/work-policy/v1/conversations/{cid}/user-text-file-capability"
    response = c.h.client.request("GET", url + ("?profile=other" if variant == "query" else ""),
        content=b'{"profile":"other"}' if variant == "body" else None,
        headers=c.h.headers("agent" if variant == "agent" else "read-only"))
    assert response.status_code == (403 if variant == "agent" else 422)


@pytest.mark.parametrize("invalid", ["path", "extension", "nul", "surrogate", "five", "collision", "json-over"])
def test_literal_invalid_batch_never_freezes_a_partial_command(picker, invalid):
    c = picker
    enable_files(c)
    file = {"name": "one.txt", "text": "safe"}
    refs = []
    if invalid == "path":
        file["name"] = "../one.txt"
    elif invalid == "extension":
        file["name"] = "one.exe"
    elif invalid == "nul":
        file["text"] = "\0"
    elif invalid == "surrogate":
        file["text"] = "\ud800"
    elif invalid == "collision":
        file["name"] = c.attachment.name
        refs = [c.attachment.model_dump(mode="json")]
    elif invalid == "json-over":
        file["text"] = "\n" * 4096
    files = [file] if invalid != "five" else [{"name": f"file{n}.txt", "text": ""} for n in range(5)]
    rid = draft(c)["request_id"]
    before = c.journal.read_bytes()
    # ASCII encoding represents even the deliberately invalid scalar without a transport-side shortcut.
    response = c.browser.ui.post(f"/api/instructions/{rid}/submit", content=json.dumps({
        "content": "Read.", "model": "gpt-4.1-mini", "files": files, "attachments": refs,
    }), headers={**c.browser.headers, "Content-Type": "application/json"})
    assert response.status_code == 400 and not c.calls
    assert c.journal.read_bytes() == before and draft(c)["content"] is None


def test_literal_complete_json_limit_includes_escaping_and_zero_byte_file_is_retained(picker):
    c = picker
    enable_files(c)
    overhead = len(b'{"content":"Read.","files":[{"name":"one.txt","text":""}]}')
    text = "x" * (8192 - overhead)
    response = send_files(c, [{"name": "one.txt", "text": text}], content="Read.")
    assert response.status_code == 200, response.text
    command = c.browser.app.state.console.instructions.command(response.json()["task"]["task_id"])
    assert command.files[0].text == text
    assert len(json.dumps({"content": command.content, "files": [{"name": "one.txt", "text": text}]},
                          separators=(",", ":")).encode()) == 8192
    assert c.browser.ui.post("/api/instructions/conversations/new", json={}, headers=c.browser.headers).status_code == 200
    empty = send_files(c, [{"name": "empty.txt", "text": ""}])
    assert empty.status_code == 200
    download = c.browser.ui.get(f"/api/instructions/{empty.json()['task']['task_id']}/files/empty.txt")
    assert download.status_code == 200 and download.content == b""
    assert download.headers["x-content-sha256"] == hashlib.sha256(b"").hexdigest()


def test_total_four_refs_and_files_then_cold_unknown_reconcile_keeps_the_original_command(picker):
    c = picker
    enable_files(c)
    files = [{"name": f"literal-{n}.txt", "text": f"original-{n}"} for n in range(3)]
    refs = [c.attachment.model_dump(mode="json")]

    def lose(request, response):
        if request.url.path == "/work-policy/v1/submit":
            assert response.status_code == 200
            raise httpx.ReadTimeout("OFFLINE literal submit response lost")
        return response

    c.mutate = lose
    rid = draft(c)["request_id"]
    assert send_files(c, files, attachments=refs).status_code == 503
    with sqlite3.connect(c.journal) as db:
        original = db.execute("SELECT intent,outcome FROM intents WHERE task_id=? AND phase='submit'", (rid,)).fetchone()
    reopened = open_ui(c)
    try:
        c.calls.clear()
        changed = send_files(c, [], attachments=refs, browser=reopened)
        assert changed.json()["error"]["code"] == "INSTRUCTION_CHANGED" and not c.calls
        response = reopened.ui.post(f"/api/tasks/{rid}/submit-reconcile", json={}, headers=reopened.headers)
        assert response.status_code == 200, response.text
        assert json.loads(c.calls[-1][2]) == json.loads(original[0])["body"]
        assert json.loads(c.calls[-1][2])["files"] == files
        assert json.loads(c.calls[-1][2])["attachments"] == refs
        with sqlite3.connect(c.journal) as db:
            assert db.execute("SELECT intent,outcome FROM intents WHERE task_id=? AND phase='submit'", (rid,)).fetchone() == original
    finally:
        reopened.ui.close()


def test_literal_second_client_cannot_mutate_frozen_text_while_first_submit_is_inflight(picker):
    c = picker
    enable_files(c)
    other = open_ui(c)
    entered, release = Event(), Event()

    def wait(request):
        if request.url.path == "/work-policy/v1/submit":
            entered.set()
            assert release.wait(10)

    c.before = wait
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(send_files, c, [{"name": "a.txt", "text": "original"}])
            try:
                assert entered.wait(10)
                rejected = send_files(c, [{"name": "a.txt", "text": "changed"}], browser=other)
                assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "INSTRUCTION_CHANGED"
            finally:
                release.set()
            assert first.result(timeout=10).status_code == 200
    finally:
        other.ui.close()
    assert sum(path == "/work-policy/v1/submit" for _, path, _ in c.calls) == 1


@pytest.mark.parametrize("damage", ["denied", "404", "bytes", "hash", "filename"])
def test_original_file_read_requires_current_authority_and_exact_content(picker, damage):
    c = picker
    enable_files(c)
    task = send_files(c, [{"name": "original.json", "text": "\ufeff{}\r\n"}]).json()["task"]
    c.calls.clear()
    before = c.journal.read_bytes()
    if damage == "denied":
        c.control.revoke(c.human, "withdraw-literal-read")
    else:
        def malformed(request, response):
            if damage == "404":
                return httpx.Response(404, json={"detail": "Not Found"})
            headers = dict(response.headers)
            raw = response.content
            if damage == "bytes":
                raw = b'{}\r\n'
                headers["x-content-sha256"] = hashlib.sha256(raw).hexdigest()
            elif damage == "hash":
                headers["x-content-sha256"] = "f" * 64
            elif damage == "filename":
                headers["content-disposition"] = 'attachment; filename="wrong.json"'
            return httpx.Response(200, content=raw, headers=headers)
        c.mutate = malformed
    response = c.browser.ui.get(f"/api/instructions/{task['task_id']}/files/original.json")
    assert response.status_code in {409, 502}
    assert "content-disposition" not in response.headers
    assert len(c.calls) == 1 and c.calls[0][0] == "GET"
    assert c.journal.read_bytes() == before


@pytest.mark.parametrize("fault", ["query", "body", "cookie", "csrf", "login", "unknown-name"])
def test_literal_local_routes_preserve_host_cookie_body_and_login_boundaries(picker, fault):
    c = picker
    enable_files(c)
    task = send_files(c, [{"name": "original.txt", "text": "safe"}]).json()["task"]
    c.calls.clear()
    headers = dict(c.browser.headers)
    if fault == "cookie":
        headers["Cookie"] = "fa_console=wrong"
    if fault == "csrf":
        headers["X-Console-CSRF"] = "wrong"
    if fault == "login":
        c.browser.app.state.console.vault.clear()
    url = f"/api/instructions/{task['task_id']}/files/" + ("absent.txt" if fault == "unknown-name" else "original.txt")
    if fault == "csrf":
        response = c.browser.ui.post(f"/api/instructions/{task['task_id']}/submit",
            json={"content": "wrong", "model": "gpt-4.1-mini", "files": []}, headers=headers)
    else:
        response = c.browser.ui.request("GET", url + ("?cid=other" if fault == "query" else ""),
            content=b'{"other":"scope"}' if fault == "body" else None, headers=headers)
    assert response.status_code in {401, 403, 404}
    assert not c.calls


def test_literal_local_next_draft_starts_empty_and_does_not_change_prior_files(picker):
    c = picker
    enable_files(c)
    task = send_files(c, [{"name": "prior.md", "text": "original"}]).json()["task"]
    controller = c.browser.app.state.console.instructions
    old = controller.store.command(task["task_id"])
    following = controller.store.new(draft(c)["thread_id"])
    prepared = controller.prepare(following["request_id"], "Follow up.", "gpt-4.1-mini")
    new = controller.command(prepared.task_id)
    assert not new.files and "files" not in new.model_dump(mode="json")
    assert new.conversation_id == task["admission"]["conversation_id"]
    assert controller.store.command(task["task_id"]) == old
