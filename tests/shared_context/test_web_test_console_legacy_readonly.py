"""A conversation on the retired job-bound lane stays readable in the workbench and takes no write."""
import pytest

from legacy_console_rows_helpers import stored_console
from test_m3_auth import jwk_client, signing_key  # fixture
from test_web_test_console_frozen_interface import real_interface  # fixture
from web_test_console_frozen_interface_helpers import post

LEGACY_TEXT = "這是舊版對話，只能查看，不能再送出。"


def legacy_thread(c):
    """A job-bound turn the workbench saved before the lane was retired (original text and lane, never sent)."""
    draft = c.browser.ui.get("/api/state").json()["composer"]
    control = c.browser.app.state.console.instructions
    command = control.validate_input(draft["request_id"], "舊對話的原文", "gpt-4.1-mini")
    control.store.prepare(command.model_dump(mode="json"), freeze=True)
    c.calls.clear()
    return draft


def refused(response):
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "ACTION_NOT_ALLOWED"


def test_a_new_draft_on_the_job_bound_lane_cannot_submit(real_interface):
    c = real_interface
    draft = c.browser.ui.get("/api/state").json()["composer"]
    c.calls.clear()
    refused(post(c, "/api/instructions/" + draft["request_id"] + "/submit",
                 {"content": "這則不會送出", "model": "gpt-4.1-mini"}))
    assert not [call for call in c.calls if call["method"] == "POST"]
    assert c.browser.app.state.console.instructions.store.command(draft["request_id"]) is None


@pytest.mark.parametrize("path", [
    "/api/instructions/{rid}/submit", "/api/tasks/{rid}/submit", "/api/tasks/{rid}/submit-reconcile",
    "/api/tasks/{rid}/submit-retry", "/api/instructions/{rid}/cancel", "/api/instructions/{thread}/reset",
    "/api/instructions/{thread}/next"])
def test_legacy_writes_are_refused_without_a_backend_call(real_interface, path):
    c = real_interface
    draft = legacy_thread(c)
    refused(post(c, path.format(rid=draft["request_id"], thread=draft["thread_id"]),
                 {"content": "x", "model": "gpt-4.1-mini"} if path.endswith("/submit") and "instructions" in path else None))
    assert c.calls == []


def test_legacy_conversation_is_shown_read_only(real_interface):
    c = real_interface
    draft = legacy_thread(c)
    state = c.browser.ui.get("/api/state?thread_id=" + draft["thread_id"]).json()
    composer = state["composer"]
    assert composer["read_only_legacy"] is True and composer["message"] == LEGACY_TEXT
    assert not any(composer[name] for name in ("editable", "can_submit", "can_follow_up", "can_reset"))
    task = next(item for item in state["tasks"] if item["task_id"] == draft["request_id"])
    assert not any(task["actions"][name]["enabled"] for name in ("submit", "submit-reconcile", "submit-retry", "cancel"))
    assert task["instruction"] == "舊對話的原文"
    assert not [call for call in c.calls if call["method"] != "GET"]


@pytest.mark.parametrize("name,reads", [
    # A turn that ran and saved its reply; and one whose cancel response was lost (its receipt is readable).
    ("console-reply-saved", ("context", "delegation", "reply", "reply.txt")),
    ("console-cancel-unknown", ("cancel-receipt",)),
])
def test_legacy_conversation_reads_still_work(real_interface, name, reads):
    c = real_interface
    task = stored_console(c, name)["task"]
    assert c.browser.ui.get("/api/state").json()["composer"]["read_only_legacy"] is True
    status = post(c, "/api/tasks/" + task["task_id"] + "/status")
    assert status.status_code == 200, status.text
    assert c.browser.ui.get("/api/instructions/progress").status_code == 200
    for read in reads:
        response = c.browser.ui.get("/api/instructions/" + task["task_id"] + "/" + read)
        assert response.status_code == 200, (read, response.text)
    assert not [call for call in c.calls if call["method"] != "GET"]
    assert not any(call["path"].endswith(("/preflight", "/submit", "/submit-reconcile", "/reset")) for call in c.calls)
