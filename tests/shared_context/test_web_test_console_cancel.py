"""Original signed cancellation producer through the thin client; no live SDK or model."""

import copy
from dataclasses import replace
from datetime import datetime, timezone
import json

import httpx
import pytest

from test_m3_auth import jwk_client, signing_key, token
from test_web_test_console_frozen_interface import complete, open_ui, post, real_interface


def admitted(c):
    composer = c.browser.ui.get("/api/state").json()["composer"]
    result = post(c, "/api/instructions/" + composer["request_id"] + "/submit",
                  {"content": "Keep this original work and its reply.", "model": "gpt-4.1-mini"})
    assert result.status_code == 200, result.text
    task = result.json()["task"]
    c.calls.clear()
    return task


def cancellation_path(task):
    return "/api/instructions/" + task["task_id"] + "/cancel"


def sent_cancellations(c):
    return [call for call in c.calls if call["path"].endswith("/cancel")]


def test_queued_cancellation_is_once_and_survives_client_restart(real_interface):
    c = real_interface
    task = admitted(c)
    console = c.browser.app.state.console
    original = console.journal.get(task["task_id"], "submit")
    text = console.instructions.command(task["task_id"]).model_dump_json()
    response = post(c, cancellation_path(task))
    assert response.status_code == 200, response.text
    cancelled = response.json()["task"]
    assert cancelled["cancellation"]["state"] == "CONFIRMED"
    result = cancelled["cancellation"]["receipt"]["result"]
    assert result["cancel_accepted"] is True
    assert result["executor_stopped"] is True and result["status"] == "cancelled"
    assert c.h.workers.inspect(task["job_id"]).state == "cancelled"
    assert c.starts == c.sdk_calls == []
    assert len(sent_cancellations(c)) == 1
    assert not cancelled["actions"]["cancel"]["enabled"]
    assert cancelled["acknowledgment"]["state"] == "NOT_SENT"
    assert console.journal.get(task["task_id"], "submit") == original
    assert console.instructions.command(task["task_id"]).model_dump_json() == text
    retained = copy.deepcopy(c.h.transport.backing.rows)
    assert post(c, cancellation_path(task)).status_code == 200
    restarted = open_ui(c)
    try:
        replay = restarted.ui.post(cancellation_path(task), json={}, headers=restarted.headers)
        assert replay.status_code == 200, replay.text
        assert replay.json()["task"]["cancellation"]["state"] == "CONFIRMED"
        assert len(sent_cancellations(c)) == 1
        assert c.h.transport.backing.rows == retained
    finally:
        restarted.ui.close()


def test_active_cancel_receipt_does_not_claim_the_executor_has_stopped(real_interface):
    c = real_interface
    task = admitted(c)
    starts = []
    c.h.workers.adapter = replace(c.h.workers.adapter, start=lambda value: starts.append(value))
    binding = c.h.runtime.bindings[task["binding_id"]]
    c.h.workers.start(binding.job_id, binding.owner)
    before = c.h.workers.inspect(binding.job_id)
    assert before.state == "starting"
    response = post(c, cancellation_path(task))
    assert response.status_code == 200, response.text
    value = response.json()["task"]["cancellation"]
    assert value["state"] == "CONFIRMED"
    assert value["receipt"]["result"]["cancel_accepted"] is True
    assert value["receipt"]["result"]["executor_stopped"] is False
    current = c.h.workers.inspect(binding.job_id)
    assert current.cancel_requested and current.state == before.state
    assert current.binding == before.binding
    assert len(starts) == 1 and c.sdk_calls == []
    assert len(sent_cancellations(c)) == 1


def test_lost_cancel_response_preserves_unknown_and_never_posts_again(real_interface):
    c = real_interface
    task = admitted(c)

    def lose_reply(request, response):
        if request.url.path.endswith("/cancel"):
            raise httpx.ReadTimeout("synthetic cancellation response loss")
        return response

    c.mutate = lose_reply
    response = post(c, cancellation_path(task))
    assert response.status_code == 503
    stored = c.browser.app.state.console.journal.get(task["task_id"], "cancel")
    assert stored["state"] == "UNKNOWN" and stored["automatic_retry"] is False
    assert c.h.workers.inspect(task["job_id"]).state == "cancelled"
    c.mutate = lambda request, response: response
    assert post(c, cancellation_path(task)).status_code == 200
    updated = c.browser.ui.get("/api/instructions/progress").json()
    current = next(item for item in updated["tasks"] if item["task_id"] == task["task_id"])
    assert current["progress"]["data"]["state"] == "cancelled"
    assert current["cancellation"]["state"] == "UNKNOWN"
    assert len(sent_cancellations(c)) == 1 and c.starts == c.sdk_calls == []
    assert c.browser.app.state.console.journal.get(task["task_id"], "cancel") == stored


def test_completed_work_is_not_cancelled_or_rewritten(real_interface):
    c = real_interface
    task = admitted(c)
    complete(c, task["binding_id"])
    assert c.browser.ui.get("/api/instructions/progress").status_code == 200
    before = copy.deepcopy(c.h.transport.backing.rows)
    journal = c.journal.read_bytes()
    response = post(c, cancellation_path(task))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANCELLATION_NOT_NEEDED"
    assert sent_cancellations(c) == []
    assert c.h.transport.backing.rows == before and c.journal.read_bytes() == journal
    assert c.browser.app.state.console.journal.get(task["task_id"], "cancel") is None


def test_fresh_terminal_status_disables_cancel_even_with_an_older_running_projection(real_interface):
    c = real_interface
    task = admitted(c)
    assert c.browser.ui.get("/api/instructions/progress").status_code == 200
    complete(c, task["binding_id"])
    response = post(c, cancellation_path(task))
    assert response.status_code == 409
    state = c.browser.ui.get("/api/state").json()
    current = next(item for item in state["tasks"] if item["task_id"] == task["task_id"])
    assert current["progress"]["data"]["state"] == "queued"
    assert current["status"]["data"]["state"] == "succeeded"
    assert current["actions"]["cancel"]["enabled"] is False
    assert sent_cancellations(c) == []


@pytest.mark.parametrize("change", [
    "wrong-receipt", "invalid-digest", "unknown-status", "accepted-string",
    "contradictory-stop", "missing-accepted", "extra-field", "duplicate-key",
])
def test_bad_cancel_receipt_stays_unknown_without_overwriting_or_resending(real_interface, change):
    c = real_interface
    task = admitted(c)
    console = c.browser.app.state.console
    original = console.journal.get(task["task_id"], "submit")

    def corrupt(request, response):
        if not request.url.path.endswith("/cancel"):
            return response
        data = json.loads(response.content)
        if change == "wrong-receipt":
            data["control_request_id"] = "f" * 64
        elif change == "invalid-digest":
            data["control_request_digest"] = "invalid"
        elif change == "unknown-status":
            data["result"]["status"] = "unknown-future-state"
        elif change == "accepted-string":
            data["result"]["cancel_accepted"] = "true"
        elif change == "contradictory-stop":
            data["result"]["executor_stopped"] = False
        elif change == "missing-accepted":
            data["result"].pop("cancel_accepted")
        elif change == "extra-field":
            data["result"]["refunded"] = True
        raw = json.dumps(data).encode()
        if change == "duplicate-key":
            raw = b'{"control_request_id":"' + data["control_request_id"].encode() + b'",' + raw[1:]
        return httpx.Response(200, content=raw, headers={"cache-control": "no-store"})

    c.mutate = corrupt
    response = post(c, cancellation_path(task))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "CANCELLATION_INVALID"
    intent = console.journal.get(task["task_id"], "cancel")
    assert intent["state"] == "UNKNOWN" and intent["automatic_retry"] is False
    assert console.journal.get(task["task_id"], "submit") == original
    c.mutate = lambda request, response: response
    assert post(c, cancellation_path(task)).status_code == 200
    assert len(sent_cancellations(c)) == 1
    assert console.journal.get(task["task_id"], "cancel") == intent
    assert c.starts == c.sdk_calls == []


@pytest.mark.parametrize("fault", ["logged-out", "foreign-task", "fixed-task", "body", "query"])
def test_cancel_needs_original_admission_login_and_empty_local_input(real_interface, fault):
    c = real_interface
    task = admitted(c)
    before = copy.deepcopy(c.h.transport.backing.rows)
    journal = c.journal.read_bytes()
    path, body = cancellation_path(task), {}
    if fault == "logged-out":
        c.browser.app.state.console.vault.clear()
    elif fault == "foreign-task":
        path = "/api/instructions/not-an-original-task/cancel"
    elif fault == "fixed-task":
        path = "/api/instructions/" + c.config.tasks[0].task_id + "/cancel"
    elif fault == "body":
        body = {"binding_id": "another-binding"}
    else:
        path += "?job_id=another-job"
    response = post(c, path, body)
    assert response.status_code in {401, 403}
    assert c.h.transport.backing.rows == before and c.journal.read_bytes() == journal
    assert c.calls == [] and c.starts == c.sdk_calls == []


def test_cancel_cannot_use_an_unconfirmed_submission_as_an_admission(real_interface):
    c = real_interface
    composer = c.browser.ui.get("/api/state").json()["composer"]
    c.lost = True
    response = post(c, "/api/instructions/" + composer["request_id"] + "/submit",
                    {"content": "Original unconfirmed admission", "model": "gpt-4.1-mini"})
    assert response.status_code == 503
    c.calls.clear()
    before = copy.deepcopy(c.h.transport.backing.rows)
    stopped = post(c, "/api/instructions/" + composer["request_id"] + "/cancel")
    assert stopped.status_code == 409
    assert stopped.json()["error"]["code"] == "INSTRUCTION_PENDING"
    assert c.calls == [] and c.h.transport.backing.rows == before
    assert c.browser.app.state.console.journal.get(composer["request_id"], "cancel") is None


def test_cancellation_already_won_by_another_console_keeps_its_original_outcome(real_interface):
    c = real_interface
    task = admitted(c)
    other = open_ui(c)
    entered, winner = False, None

    def cancel_between_read_and_local_intent(request, response):
        nonlocal entered, winner
        if request.method == "GET" and "/jobs/" in request.url.path and not entered:
            entered = True
            result = other.ui.post(cancellation_path(task), json={}, headers=other.headers)
            assert result.status_code == 200, result.text
            winner = c.browser.app.state.console.journal.get(task["task_id"], "cancel")
        return response

    c.mutate = cancel_between_read_and_local_intent
    try:
        response = post(c, cancellation_path(task))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RECONCILE_REQUIRED"
        assert winner is not None and winner["state"] == "CONFIRMED"
        assert c.browser.app.state.console.journal.get(task["task_id"], "cancel") == winner
        assert len(sent_cancellations(c)) == 1
    finally:
        other.ui.close()


def test_completion_between_status_and_cancel_preserves_success_and_original_reply(real_interface):
    c = real_interface
    task = admitted(c)
    binding = c.h.runtime.bindings[task["binding_id"]]
    start = c.h.workers.adapter.start
    pending = []
    c.h.workers.adapter = replace(c.h.workers.adapter, start=lambda value: pending.append(value))
    c.h.workers.start(binding.job_id, binding.owner)
    assert c.h.workers.inspect(binding.job_id).state == "starting"
    finished = False

    def finish_after_read(request, response):
        nonlocal finished
        if request.method == "GET" and "/jobs/" in request.url.path and not finished:
            finished = True
            evidence = start(pending[0])
            c.h.workers.observe(binding.job_id, evidence, binding.owner)
            complete(c, task["binding_id"])
        return response

    c.mutate = finish_after_read
    response = post(c, cancellation_path(task))
    assert response.status_code == 200, response.text
    value = response.json()["task"]["cancellation"]
    assert value["state"] == "CONFIRMED"
    assert value["receipt"]["result"]["cancel_accepted"] is False
    assert value["receipt"]["result"]["status"] == "succeeded"
    assert "executor_stopped" not in value["receipt"]["result"]
    assert c.h.workers.inspect(task["job_id"]).state == "succeeded"
    assert len(sent_cancellations(c)) == 1 and len(c.starts) == len(c.sdk_calls) == 1
    assert post(c, cancellation_path(task)).status_code == 200
    assert c.browser.ui.get("/api/instructions/progress").status_code == 200
    current = c.browser.app.state.console.instructions.replies[task["task_id"]]
    assert current["data"]["text"] == "OFFLINE SDK I/O substitute reply 1"


def test_generation_change_keeps_an_unmatched_cancel_receipt_unknown(real_interface):
    c = real_interface
    task = admitted(c)
    binding = c.h.runtime.bindings[task["binding_id"]]
    c.h.workers.adapter = replace(c.h.workers.adapter, start=lambda _: None)
    started = False

    def start_after_read(request, response):
        nonlocal started
        if request.method == "GET" and "/jobs/" in request.url.path and not started:
            started = True
            c.h.workers.start(binding.job_id, binding.owner)
        return response

    c.mutate = start_after_read
    response = post(c, cancellation_path(task))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "CANCELLATION_INVALID"
    saved = c.browser.app.state.console.journal.get(task["task_id"], "cancel")
    assert saved["intent"]["generation"] == 0 and saved["state"] == "UNKNOWN"
    current = c.h.workers.inspect(binding.job_id)
    assert current.generation == 1 and current.cancel_requested and current.state == "starting"
    assert post(c, cancellation_path(task)).status_code == 200
    assert len(sent_cancellations(c)) == 1


@pytest.mark.parametrize("during_read", [False, True])
def test_cancel_rechecks_current_backend_read_authority_without_local_escalation(real_interface, during_read):
    c = real_interface
    task = admitted(c)
    human = c.h.verifier(c.h.client.build_request("GET", "/", headers=c.h.headers()))
    revoked = False
    if not during_read:
        c.control.revoke(human, "revoke-cancel-access")

    def revoke_after_status(request, response):
        nonlocal revoked
        if during_read and request.method == "GET" and "/jobs/" in request.url.path and not revoked:
            revoked = True
            c.control.revoke(human, "revoke-before-cancel")
        return response

    c.mutate = revoke_after_status
    response = post(c, cancellation_path(task))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BACKEND_DENIED"
    current = c.h.workers.inspect(task["job_id"])
    assert not current.cancel_requested and current.state == "queued"
    if during_read:
        saved = c.browser.app.state.console.journal.get(task["task_id"], "cancel")
        assert saved["state"] == "UNKNOWN" and saved["automatic_retry"] is False
        assert post(c, cancellation_path(task)).status_code == 200
        assert len(sent_cancellations(c)) == 1
    else:
        assert c.browser.app.state.console.journal.get(task["task_id"], "cancel") is None
        assert sent_cancellations(c) == []
    assert c.starts == c.sdk_calls == []


def test_token_expiring_during_status_read_does_not_consume_the_cancel_intent(real_interface):
    c = real_interface
    task = admitted(c)
    console = c.browser.app.state.console
    expired = False

    def expire_after_read(request, response):
        nonlocal expired
        if request.method == "GET" and "/jobs/" in request.url.path and not expired:
            expired = True
            c.h.time = datetime.fromtimestamp(console.vault._expires + 1, timezone.utc)
        return response

    c.mutate = expire_after_read
    response = post(c, cancellation_path(task))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "LOGIN_EXPIRED"
    assert console.journal.get(task["task_id"], "cancel") is None
    assert sent_cancellations(c) == []
    assert not c.h.workers.inspect(task["job_id"]).cancel_requested
    c.mutate = lambda request, response: response
    console.token_provider = lambda: token(c.h.signing, {
        "scp": "Behavior.Read Behavior.Approve", "exp": int(c.h.time.timestamp()) + 300,
    })
    assert post(c, "/api/authenticate").status_code == 200
    stopped = post(c, cancellation_path(task))
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["task"]["cancellation"]["state"] == "CONFIRMED"
    assert len(sent_cancellations(c)) == 1 and c.starts == c.sdk_calls == []
