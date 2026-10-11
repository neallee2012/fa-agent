"""Read the original cancellation control receipt without dispatching cancellation again."""

import copy
from contextvars import Context
from dataclasses import replace
import hashlib
import json

import httpx
import pytest

from test_m3_auth import jwk_client, signing_key, token
from test_web_test_console_cancel import admitted, cancellation_path, sent_cancellations
from test_web_test_console_frozen_interface import post, real_interface


def receipt_path(task, generation=0):
    return f"/work-policy/v1/bindings/{task['binding_id']}/cancel-receipt/{generation}"


@pytest.mark.parametrize("active", [False, True])
def test_lost_cancel_response_can_be_read_without_mutation_or_another_dispatch(real_interface, active):
    c = real_interface
    task = admitted(c)
    starts, original = [], []
    if active:
        c.h.workers.adapter = replace(c.h.workers.adapter, start=lambda value: starts.append(value))
        binding = c.h.runtime.bindings[task["binding_id"]]
        c.h.workers.start(binding.job_id, binding.owner)

    def lose_response(request, response):
        if request.url.path.endswith("/cancel"):
            original.append(json.loads(response.content))
            raise httpx.ReadTimeout("synthetic cancellation response loss")
        return response

    c.mutate = lose_response
    assert post(c, cancellation_path(task)).status_code == 503
    c.mutate = lambda request, response: response
    before, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    result = c.h.client.get(receipt_path(task, int(active)), headers=c.h.headers())
    assert result.status_code == 200, result.text
    assert result.json() == original[0]
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["x-content-sha256"] == hashlib.sha256(result.content).hexdigest()
    assert c.h.client.get(receipt_path(task, int(active)), headers=c.h.headers()).content == result.content
    assert c.h.transport.backing.rows == before and c.journal.read_bytes() == journal
    assert len(sent_cancellations(c)) == 1 and len(starts) == int(active)
    assert c.sdk_calls == []
    saved = c.browser.app.state.console.journal.get(task["task_id"], "cancel")
    assert saved["state"] == "UNKNOWN"
    assert result.json()["result"]["executor_stopped"] is not active


def test_missing_cancellation_receipt_is_unknown_and_does_not_create_a_cancellation(real_interface):
    c = real_interface
    task = admitted(c)
    before = copy.deepcopy(c.h.transport.backing.rows)
    response = c.h.client.get(receipt_path(task), headers=c.h.headers())
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "CANCELLATION_RECEIPT_UNKNOWN"
    assert response.json()["error"]["automatic_retry"] is False
    assert c.h.transport.backing.rows == before
    assert not c.h.workers.inspect(task["job_id"]).cancel_requested
    assert sent_cancellations(c) == [] and c.starts == c.sdk_calls == []


def test_receipt_read_does_not_guess_a_new_generation(real_interface):
    c = real_interface
    task = admitted(c)
    binding = c.h.runtime.bindings[task["binding_id"]]
    c.h.workers.adapter = replace(c.h.workers.adapter, start=lambda _: None)
    c.h.workers.start(binding.job_id, binding.owner)
    before = copy.deepcopy(c.h.transport.backing.rows)
    response = c.h.client.get(receipt_path(task, 0), headers=c.h.headers())
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CANCELLATION_RECEIPT_CHANGED"
    assert c.h.transport.backing.rows == before
    assert not c.h.workers.inspect(task["job_id"]).cancel_requested
    assert sent_cancellations(c) == []


def unknown_cancel(c):
    task = admitted(c)

    def lost(request, response):
        if request.url.path.endswith("/cancel"):
            raise httpx.ReadTimeout("synthetic original cancellation response loss")
        return response

    c.mutate = lost
    assert post(c, cancellation_path(task)).status_code == 503
    c.mutate = lambda request, response: response
    return task


def test_bridge_receipt_lookup_is_repeatable_get_only_and_keeps_original_unknown(real_interface):
    c = real_interface
    task = unknown_cancel(c)
    console = c.browser.app.state.console
    original = console.journal.get(task["task_id"], "cancel")
    before, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    c.calls.clear()
    path = "/api/instructions/" + task["task_id"] + "/cancel-receipt"
    for _ in range(2):
        response = c.browser.ui.get(path)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["schema_version"] == "fa-cancellation-observation-v1"
        assert data["task_id"] == task["task_id"] and data["job_id"] == task["job_id"]
        assert data["generation"] == 0 and data["read_only"] is True
        assert data["original_outcome"] == "UNKNOWN"
        assert data["receipt"]["result"]["cancel_accepted"] is True
    assert len(c.calls) == 2 and all(call["method"] == "GET" for call in c.calls)
    assert console.journal.get(task["task_id"], "cancel") == original
    assert c.journal.read_bytes() == journal and c.h.transport.backing.rows == before
    assert c.starts == c.sdk_calls == []


@pytest.mark.parametrize("fault", ["generation", "method", "path", "job", "body", "digest"])
def test_bridge_cannot_change_the_original_cancellation_intent_to_select_a_receipt(real_interface, fault):
    c = real_interface
    task = unknown_cancel(c)
    journal = c.browser.app.state.console.journal
    original = journal.get(task["task_id"], "cancel")
    intent = copy.deepcopy(original["intent"])
    if fault == "generation":
        intent["generation"] = True
    elif fault == "method":
        intent["method"] = "GET"
    elif fault == "path":
        intent["path"] = "/work-policy/v1/bindings/another/cancel"
    elif fault == "job":
        intent["job_id"] = "another-job"
    elif fault == "body":
        intent["body"]["request_id"] = "replacement-request"
    else:
        intent["body_sha256"] = "f" * 64
    with journal.connect() as db:
        db.execute("UPDATE intents SET intent=? WHERE task_id=? AND phase='cancel'",
                   (json.dumps(intent), task["task_id"]))
    c.calls.clear()
    before = copy.deepcopy(c.h.transport.backing.rows)
    response = c.browser.ui.get("/api/instructions/" + task["task_id"] + "/cancel-receipt")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANCELLATION_ORIGINAL_INVALID"
    assert c.calls == [] and c.h.transport.backing.rows == before
    assert journal.get(task["task_id"], "cancel")["state"] == "UNKNOWN"


@pytest.mark.parametrize("fault", ["missing", "confirmed", "query", "body", "logged-out"])
def test_bridge_needs_the_original_unknown_and_current_login_before_reading(real_interface, fault):
    c = real_interface
    task = unknown_cancel(c) if fault not in {"missing", "confirmed"} else admitted(c)
    if fault == "confirmed":
        assert post(c, cancellation_path(task)).status_code == 200
    path = "/api/instructions/" + task["task_id"] + "/cancel-receipt"
    if fault == "query":
        path += "?generation=1"
    if fault == "logged-out":
        c.browser.app.state.console.vault.clear()
    before, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    c.calls.clear()
    response = c.browser.ui.request("GET", path, content=b"{}" if fault == "body" else None)
    assert response.status_code in {401, 403, 409}
    assert c.calls == [] and c.h.transport.backing.rows == before and c.journal.read_bytes() == journal


@pytest.mark.parametrize("fault", ["sha", "wrong-receipt", "duplicate-key", "unavailable"])
def test_bridge_rejects_unverifiable_receipts_without_changing_the_original_unknown(real_interface, fault):
    c = real_interface
    task = unknown_cancel(c)
    original = c.browser.app.state.console.journal.get(task["task_id"], "cancel")

    def change(request, response):
        if "/cancel-receipt/" not in request.url.path:
            return response
        if fault == "unavailable":
            return httpx.Response(404, json={"detail": "not available"})
        data = json.loads(response.content)
        if fault == "wrong-receipt":
            data["control_request_id"] = "f" * 64
        raw = json.dumps(data).encode()
        if fault == "duplicate-key":
            raw = b'{"result":' + json.dumps(data["result"]).encode() + b"," + raw[1:]
        return httpx.Response(200, content=raw, headers={
            "cache-control": "no-store",
            "x-content-sha256": "f" * 64 if fault == "sha" else hashlib.sha256(raw).hexdigest(),
        })

    c.mutate = change
    before, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    response = c.browser.ui.get("/api/instructions/" + task["task_id"] + "/cancel-receipt")
    assert response.status_code == (409 if fault == "unavailable" else 502)
    assert response.json()["error"]["code"] == (
        "CANCELLATION_RECEIPT_UNAVAILABLE" if fault == "unavailable" else "CANCELLATION_INVALID"
    )
    assert c.browser.app.state.console.journal.get(task["task_id"], "cancel") == original
    assert c.h.transport.backing.rows == before and c.journal.read_bytes() == journal
    assert len(sent_cancellations(c)) == 1


@pytest.mark.parametrize("identity", ["agent", "wrong-human", "no-read"])
def test_api_receipt_requires_the_current_original_human_read_scope(real_interface, identity):
    c = real_interface
    task = unknown_cancel(c)
    if identity == "agent":
        headers = c.h.headers("agent")
    else:
        changes = {"scp": "Behavior.Read"}
        if identity == "wrong-human":
            changes["oid"] = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        else:
            changes["scp"] = "Behavior.Approve"
        headers = {"Authorization": "Bearer " + token(c.h.signing, changes)}
    before = copy.deepcopy(c.h.transport.backing.rows)
    response = c.h.client.get(receipt_path(task), headers=headers)
    assert response.status_code == 403
    assert c.h.transport.backing.rows == before and "result" not in response.json()


@pytest.mark.parametrize("fault", ["negative", "future", "query", "body", "foreign-binding"])
def test_api_receipt_does_not_accept_other_generation_scope_or_payload(real_interface, fault):
    c = real_interface
    task = unknown_cancel(c)
    path = receipt_path(task)
    if fault == "negative":
        path = receipt_path(task, -1)
    elif fault == "future":
        path = receipt_path(task, 9)
    elif fault == "query":
        path += "?request_id=replacement"
    elif fault == "foreign-binding":
        path = "/work-policy/v1/bindings/not-original/cancel-receipt/0"
    before = copy.deepcopy(c.h.transport.backing.rows)
    response = c.h.client.request("GET", path, content=b"{}" if fault == "body" else None, headers=c.h.headers())
    assert response.status_code in {403, 422}
    assert "result" not in response.json() and c.h.transport.backing.rows == before


@pytest.mark.parametrize("during_read", [False, True])
def test_receipt_read_rechecks_revocation_before_returning_saved_data(real_interface, monkeypatch, during_read):
    from shared_context.behavior_workers import worker_key

    c = real_interface
    task = unknown_cancel(c)
    human = c.h.verifier(c.h.client.build_request("GET", "/", headers=c.h.headers()))
    journal = c.journal.read_bytes()
    if during_read:
        original, injected = c.h.store._receipt, []

        def revoke_after_receipt(request):
            result = original(request)
            if request.request_id == worker_key("cancel", task["job_id"]) and not injected:
                injected.append(True)
                Context().run(c.control.revoke, human, "revoke-during-cancel-read")
            return result

        monkeypatch.setattr(c.h.store, "_receipt", revoke_after_receipt)
    else:
        c.control.revoke(human, "revoke-cancel-receipt-read")
    response = c.h.client.get(receipt_path(task), headers=c.h.headers())
    assert response.status_code == 403, response.text
    assert "result" not in response.json()
    assert c.journal.read_bytes() == journal and len(sent_cancellations(c)) == 1
    assert c.starts == c.sdk_calls == []
    if during_read:
        assert injected == [True]


def test_receipt_read_does_not_install_a_missing_standing_control(real_interface):
    c = real_interface
    task = unknown_cancel(c)
    before = copy.deepcopy(c.h.transport.backing.rows)
    c.h.runtime.work_policy = None
    response = c.h.client.get(receipt_path(task), headers=c.h.headers())
    assert response.status_code == 503
    assert c.h.runtime.work_policy is None
    assert c.h.transport.backing.rows == before and len(sent_cancellations(c)) == 1


def test_request_reconstruction_mismatch_does_not_replace_an_original_receipt(real_interface, monkeypatch):
    c = real_interface
    task = unknown_cancel(c)
    before = copy.deepcopy(c.h.transport.backing.rows)
    original = c.h.workers._cancellation_request
    monkeypatch.setattr(c.h.workers, "_cancellation_request",
        lambda *args: original(*args).model_copy(update={"request_digest": "f" * 64}))
    response = c.h.client.get(receipt_path(task), headers=c.h.headers())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANCELLATION_RECEIPT_CHANGED"
    assert c.h.transport.backing.rows == before and len(sent_cancellations(c)) == 1
    assert c.starts == c.sdk_calls == []
