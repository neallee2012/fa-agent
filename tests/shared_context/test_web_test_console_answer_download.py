"""Saved reply text download through current authority; no live model or browser."""

import copy
import hashlib
import json

import httpx
import pytest

from test_m3_auth import jwk_client, signing_key
from test_web_test_console_frozen_interface import complete, open_ui, post, real_interface


def saved(c):
    composer = c.browser.ui.get("/api/state").json()["composer"]
    response = post(c, "/api/instructions/" + composer["request_id"] + "/submit",
                    {"content": "Return the original saved answer.", "model": "gpt-4.1-mini"})
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    complete(c, task["binding_id"])
    assert c.browser.ui.get("/api/instructions/progress").status_code == 200
    return task


def download_path(task):
    return "/api/instructions/" + task["task_id"] + "/reply.txt"


def test_saved_answer_download_is_original_utf8_and_only_reads(real_interface):
    c = real_interface
    c.reply_text = "  原始答案🙂\r\n下一行保留空白。\n<script>這只是文字</script>  "
    task = saved(c)
    control = c.browser.app.state.console.instructions
    text = control.replies[task["task_id"]]["data"]["text"]
    before, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    calls, starts = len(c.sdk_calls), len(c.starts)
    c.calls.clear()
    response = c.browser.ui.get(download_path(task))
    assert response.status_code == 200, response.text
    assert response.content == text.encode("utf-8")
    assert response.content == c.reply_text.encode("utf-8")
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["content-disposition"] == 'attachment; filename="fa-reply-1.txt"'
    assert response.headers["x-content-sha256"] == hashlib.sha256(response.content).hexdigest()
    assert response.headers["cache-control"] == "no-store"
    assert c.calls and all(call["method"] == "GET" for call in c.calls)
    assert c.h.transport.backing.rows == before and c.journal.read_bytes() == journal
    assert len(c.sdk_calls) == calls and len(c.starts) == starts
    assert c.browser.app.state.console.journal.get(task["task_id"], "acknowledge") is None
    assert c.browser.ui.get(download_path(task)).content == response.content


def test_reply_download_after_restart_reloads_the_saved_answer_without_a_new_submission(real_interface):
    c = real_interface
    task = saved(c)
    expected = c.browser.app.state.console.instructions.replies[task["task_id"]]["data"]["text"].encode()
    before, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    restarted = open_ui(c)
    try:
        assert restarted.app.state.console.instructions.reply_bytes == {}
        c.calls.clear()
        response = restarted.ui.get(download_path(task))
        assert response.status_code == 200, response.text
        assert response.content == expected
        assert all(call["method"] == "GET" for call in c.calls)
        assert c.h.transport.backing.rows == before and c.journal.read_bytes() == journal
        assert len(c.starts) == len(c.sdk_calls) == 1
    finally:
        restarted.ui.close()


def test_download_does_not_use_cached_reply_after_current_read_access_is_revoked(real_interface):
    c = real_interface
    task = saved(c)
    assert task["task_id"] in c.browser.app.state.console.instructions.replies
    human = c.h.verifier(c.h.client.build_request("GET", "/", headers=c.h.headers()))
    c.control.revoke(human, "revoke-answer-download")
    before, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    response = c.browser.ui.get(download_path(task))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BACKEND_DENIED"
    assert "content-disposition" not in response.headers
    assert c.h.transport.backing.rows == before and c.journal.read_bytes() == journal
    assert len(c.starts) == len(c.sdk_calls) == 1


def test_an_unsaved_preview_cannot_be_downloaded_as_an_answer(real_interface):
    c = real_interface
    composer = c.browser.ui.get("/api/state").json()["composer"]
    response = post(c, "/api/instructions/" + composer["request_id"] + "/submit",
                    {"content": "An unexecuted work.", "model": "gpt-4.1-mini"})
    task = response.json()["task"]
    before, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    result = c.browser.ui.get(download_path(task))
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "NO_RESULT"
    assert "content-disposition" not in result.headers
    assert c.h.transport.backing.rows == before and c.journal.read_bytes() == journal
    assert c.starts == c.sdk_calls == []


@pytest.mark.parametrize("fault", ["query", "body", "logged-out", "fixed-task"])
def test_download_requires_original_instruction_login_and_no_scope_overrides(real_interface, fault):
    c = real_interface
    task = saved(c)
    path = download_path(task)
    if fault == "query":
        path += "?conversation_id=other"
    elif fault == "logged-out":
        c.browser.app.state.console.vault.clear()
    elif fault == "fixed-task":
        path = "/api/instructions/" + c.config.tasks[0].task_id + "/reply.txt"
    c.calls.clear()
    response = c.browser.ui.request("GET", path, content=b"{}" if fault == "body" else None)
    assert response.status_code in {401, 403}
    assert "content-disposition" not in response.headers
    assert c.calls == []


@pytest.mark.parametrize("fault", ["hash", "scope", "malformed"])
def test_unverified_backend_reply_never_becomes_a_text_download(real_interface, fault):
    c = real_interface
    task = saved(c)
    original = c.browser.app.state.console.instructions.replies[task["task_id"]].copy()

    def corrupt(request, response):
        if not request.url.path.endswith("/reply"):
            return response
        data = json.loads(response.content)
        if fault == "scope":
            data["conversation_id"] = "other-conversation"
        raw = b"not-json" if fault == "malformed" else json.dumps(data).encode()
        return httpx.Response(200, content=raw, headers={
            "content-type": "application/json", "cache-control": "no-store",
            "x-content-sha256": "f" * 64 if fault == "hash" else hashlib.sha256(raw).hexdigest(),
        })

    c.mutate = corrupt
    before, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    result = c.browser.ui.get(download_path(task))
    assert result.status_code == 502
    assert result.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in result.headers
    assert c.browser.app.state.console.instructions.replies[task["task_id"]] == original
    assert c.h.transport.backing.rows == before and c.journal.read_bytes() == journal
    assert len(c.starts) == len(c.sdk_calls) == 1
