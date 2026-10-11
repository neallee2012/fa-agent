"""Manual once reconciliation of the original UNKNOWN ACK; no challenge needed."""
import copy
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from test_web_test_console_original_failure import small_client, browser, operate, signing_key
from web_test_console.adapter import canonical, sha


def row_bytes(case, phase):
    with sqlite3.connect(case.tmp/"once.sqlite3") as db:
        return db.execute("SELECT intent,outcome FROM intents WHERE task_id='original' AND phase=?", (phase,)).fetchone()


def original_unknown(case):
    assert operate(case.client, "fetch").status_code == 200
    case.lost = "acknowledge"
    assert operate(case.client, "acknowledge").status_code == 503
    assert case.fulfilled
    record = row_bytes(case, "acknowledge")
    assert json.loads(record[1])["state"] == "UNKNOWN"
    body = next(body for _, path, body in case.calls if path.endswith("/acknowledge"))
    assert json.loads(record[0])["body_sha256"] == sha(body)
    case.client.ui.close()
    case.client = browser(case.config, case.tmp/"once.sqlite3", case.backend, lambda: case.bearer, case.clock)
    assert case.client.app.state.console.results == {}
    return record


def reconcile_transport(case, *, response=None, lose=False):
    original = case.backend._transport.handle_request
    attempts = []
    def send(request):
        if not request.url.path.endswith("/reconcile"):
            return original(request)
        attempts.append(request)
        record = row_bytes(case, "reconcile")
        assert record is not None and json.loads(record[1])["state"] == "UNKNOWN"
        intent = json.loads(record[0])
        assert intent["body"] == json.loads(request.content) == {"request_id": case.config.tasks[0].ack_request_id}
        assert intent["body_sha256"] == sha(request.content)
        ack = json.loads(row_bytes(case, "acknowledge")[0])
        assert intent["original_ack_body_sha256"] == ack["body_sha256"]
        assert intent["original_ack_intent_sha256"] == sha(canonical(ack))
        if lose:
            raise httpx.ReadTimeout("OFFLINE original reconcile response lost")
        return response or httpx.Response(200, json=case.raw["acknowledgment"])
    case.backend._transport = httpx.MockTransport(send)
    return attempts


def test_manual_once_after_restart_uses_original_ack_id_and_preserves_unknown_history(small_client):
    c = small_client
    original = original_unknown(c)
    # GET fulfilled must not produce a local receipt.
    read = operate(c.client, "status")
    assert read.json()["task"]["status"]["data"]["fulfilled"]
    assert read.json()["task"]["acknowledgment"]["state"] == "UNKNOWN"
    requests = reconcile_transport(c)
    prior_http = len(c.calls)
    response = operate(c.client, "reconcile")
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["acknowledgment"]["state"] == "UNKNOWN"
    assert task["reconciliation"]["state"] == "CONFIRMED"
    assert task["reconciliation"]["confirmation_level"] == "client_received"
    assert len(requests) == 1 and row_bytes(c, "acknowledge") == original
    assert len(c.calls) == prior_http+1 and c.calls[-1][0] == "GET"  # exact status gate, never re-fetch/ACK
    summary = c.client.ui.get("/api/tasks/original/summary").json()
    assert summary["confirmation_source"] == "original_ack_reconciliation"
    assert summary["original_ack"]["state"] == "UNKNOWN"
    assert summary["reconciliation"]["receipt"] == c.raw["acknowledgment"]
    assert summary["ack_resent"] is False
    count = len(c.calls)
    assert operate(c.client, "reconcile").status_code == 200
    assert len(requests) == 1 and len(c.calls) == count
    c.client.ui.close()
    c.client = browser(c.config, c.tmp/"once.sqlite3", c.backend, lambda: c.bearer, c.clock)
    assert operate(c.client, "reconcile").json()["task"]["reconciliation"]["historical"]
    assert len(requests) == 1 and row_bytes(c, "acknowledge") == original
    if directory := os.environ.get("ACK_RECONCILE_RESULTS"):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        (path/"manual-reconcile-proof.json").write_text(json.dumps({
            "actual_environment": "OFFLINE_TEST", "challenge_available": False,
            "original_ack_request_id": c.config.tasks[0].ack_request_id,
            "original_ack_unchanged": True, "reconcile_posts": 1,
            "new_fetch_or_ack_posts": 0, "summary": summary,
        }, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("fault", [
    "no-row", "not-unknown", "request-id", "path", "method", "body-hash", "config-hash",
    "console-id", "exchange", "recipient", "payload-sha", "delivery-id", "version", "channel-version",
    "future-created", "unexpected-challenge",
])
def test_original_durable_ack_scope_is_required_before_any_reconcile_request(small_client, fault):
    c = small_client
    original_unknown(c)
    stored = row_bytes(c, "acknowledge")
    intent, outcome = map(json.loads, stored)
    if fault == "not-unknown": outcome["state"] = "CONFIRMED"
    elif fault == "request-id": intent["request_id"] = "another-ack"
    elif fault == "path": intent["path"] = "/web/v1/original-failure/other/acknowledge"
    elif fault == "method": intent["method"] = "GET"
    elif fault == "body-hash": intent["body_sha256"] = "invalid"
    elif fault == "config-hash": intent["configuration_sha256"] = "f"*64
    elif fault == "console-id": intent["console_id"] = "other"
    elif fault == "exchange": intent["exchange_id"] = ""
    elif fault == "recipient": intent["binding"]["route"]["recipient_id"] = "other"
    elif fault == "payload-sha": intent["binding"]["payload_digest"] = "f"*64
    elif fault == "delivery-id": intent["binding"]["delivery_id"] = "f"*64
    elif fault == "version": intent["binding"]["version"] = 2
    elif fault == "channel-version": intent["binding"]["channel_version"] = "other"
    elif fault == "future-created": intent["created_at"] = (c.clock()+timedelta(days=1)).isoformat()
    elif fault == "unexpected-challenge": intent["acknowledgment_token"] = "must-not-be-persisted"
    with sqlite3.connect(c.tmp/"once.sqlite3") as db:
        if fault == "no-row":
            db.execute("DELETE FROM intents WHERE phase='acknowledge'")
        else:
            db.execute("UPDATE intents SET intent=?, outcome=? WHERE phase='acknowledge'",
                       (json.dumps(intent), json.dumps(outcome)))
    before = len(c.calls)
    requests = reconcile_transport(c)
    response = operate(c.client, "reconcile")
    assert response.status_code == 409, fault
    assert requests == [] and len(c.calls) == before and row_bytes(c, "reconcile") is None


@pytest.mark.parametrize("fault", ["control", "digest-format", "handoff", "evidence", "level", "fulfillment", "extra", "missing"])
def test_reconcile_response_must_be_exact_original_receipt(small_client, fault):
    c = small_client
    saved = original_unknown(c)
    receipt = copy.deepcopy(c.raw["acknowledgment"])
    if fault == "control": receipt["control_request_id"] = "f"*64
    elif fault == "digest-format": receipt["control_request_digest"] = "not-sha256"
    elif fault == "handoff": receipt["result"]["channel_handoff_id"] = "other"
    elif fault == "evidence": receipt["result"]["fulfillment_receipt_id"] = "f"*64
    elif fault == "level": receipt["result"]["confirmation_level"] = "provider_accepted"
    elif fault == "fulfillment": receipt["result"]["fulfillment"] = "pending"
    elif fault == "extra": receipt["guard_revision"] = 123
    else: del receipt["result"]["confirmed_levels"]
    requests = reconcile_transport(c, response=httpx.Response(200, json=receipt))
    response = operate(c.client, "reconcile")
    assert response.status_code == 502
    assert row_bytes(c, "acknowledge") == saved
    assert json.loads(row_bytes(c, "reconcile")[1])["state"] == "UNKNOWN"
    summary = c.client.ui.get("/api/tasks/original/summary").json()
    assert summary["confirmation_level"] is None and summary["state"] == "UNKNOWN"
    assert operate(c.client, "reconcile").status_code == 200 and len(requests) == 1


@pytest.mark.parametrize("failure", ["transport-lost", "no-durable-server-ack", "server-expired", "server-revoked"])
def test_reconcile_unknown_or_backend_refusal_never_reposts(small_client, failure):
    c = small_client
    saved = original_unknown(c)
    status = 409 if failure == "no-durable-server-ack" else 403
    requests = reconcile_transport(c, response=httpx.Response(status, json={"error":{"code":"DENIED"}}),
                                    lose=failure == "transport-lost")
    response = operate(c.client, "reconcile")
    assert response.status_code != 200
    assert row_bytes(c, "acknowledge") == saved
    c.client.ui.close()
    c.client = browser(c.config, c.tmp/"once.sqlite3", c.backend, lambda: c.bearer, c.clock)
    assert operate(c.client, "reconcile").json()["task"]["reconciliation"]["state"] == "UNKNOWN"
    assert len(requests) == 1
    view = operate(c.client, "status").json()["task"]
    assert view["status"]["data"]["fulfilled"] and view["reconciliation"]["state"] == "UNKNOWN"


def test_expired_status_wrong_user_token_and_csrf_never_create_reconcile_intent(small_client):
    from test_m3_auth import token
    c = small_client
    original_unknown(c)
    requests = reconcile_transport(c)
    c.status_override = {**c.raw["status"], "expired": True}
    assert operate(c.client, "status").status_code == 200
    assert operate(c.client, "reconcile").json()["error"]["code"] == "HANDOFF_EXPIRED"
    assert row_bytes(c, "reconcile") is None and requests == []
    c.status_override = None
    c.client.app.state.console.vault.clear()
    assert operate(c.client, "reconcile").status_code == 401
    c.client.app.state.console.token_provider = lambda: token(c.token, {**c.claims, "oid": "wrong-human"})
    assert c.client.ui.post("/api/authenticate", json={}, headers=c.client.headers).status_code == 403
    assert requests == []
    c.client.app.state.console.token_provider = lambda: c.bearer
    assert c.client.ui.post("/api/authenticate", json={}, headers=c.client.headers).status_code == 200
    for headers, body in (
        ({}, {}), ({**c.client.headers, "Origin": "https://evil.example"}, {}),
        (c.client.headers, {"request_id": "new-ack"}), (c.client.headers, {"acknowledgment_token": "forged"}),
    ):
        response = c.client.ui.post("/api/tasks/original/reconcile", json=body, headers=headers)
        assert response.status_code == 403
    assert row_bytes(c, "reconcile") is None and requests == []


def test_two_clients_sharing_original_journal_reconcile_only_once(small_client):
    c = small_client
    saved = original_unknown(c)
    other = browser(c.config, c.tmp/"once.sqlite3", c.backend, lambda: c.bearer, c.clock)
    requests = reconcile_transport(c)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda client: operate(client, "reconcile"), (c.client, other)))
    assert all(r.status_code in {200, 409} for r in responses)
    assert len(requests) == 1 and row_bytes(c, "acknowledge") == saved
    assert json.loads(row_bytes(c, "reconcile")[1])["state"] == "CONFIRMED"
    other.ui.close()


def test_reconcile_final_token_check_prevents_write_after_status_wait(small_client):
    c = small_client
    original_unknown(c)
    requests = reconcile_transport(c)
    original = c.backend._transport.handle_request
    def expiry(request):
        response = original(request)
        if request.url.path.endswith("/status"):
            c.client.app.state.console.vault.clear()
        return response
    c.backend._transport = httpx.MockTransport(expiry)
    assert operate(c.client, "reconcile").status_code == 401
    assert requests == [] and row_bytes(c, "reconcile") is None


def test_reconcile_is_not_available_for_normal_pull_or_missing_ack_action(small_client):
    c = small_client
    original_unknown(c)
    from web_test_console.adapter import SteadyConsoleConfig
    for mode in ("normal_pull", "original_failure"):
        raw = c.config.model_dump(mode="json")
        task = raw["tasks"][0]
        task["mode"] = mode
        if mode == "normal_pull":
            task["handoff_id"] = None
            task.pop("original_failure")
        else:
            task["allowed_actions"] = ["status", "fetch"]
        config = SteadyConsoleConfig.model_validate_json(json.dumps(raw))
        test = browser(config, c.tmp/(mode+".sqlite3"), c.backend, lambda: c.bearer, c.clock)
        count = len(c.calls)
        assert operate(test, "reconcile").status_code == 403
        assert len(c.calls) == count
        test.ui.close()


def test_parent_readonly_native_status_and_receipt_have_exact_original_semantics():
    from shared_context.behavior_operations import control_key
    from shared_context.behavior_web_handoff import handoff_key
    from web_test_console.adapter import OriginalFailureStatus, OriginalFailureReceipt
    observations = json.loads((Path(__file__).with_name("web_test_console_ack_reconcile")/"native-readback.json").read_bytes())
    status = OriginalFailureStatus.model_validate_json(json.dumps(observations["status"]))
    receipt = OriginalFailureReceipt.model_validate_json(json.dumps(observations["receipt"]))
    assert status.fulfilled and status.confirmation_level == "client_received"
    assert status.delivery_id == control_key("delivery-final-id", status.obligation_id)
    assert receipt.control_request_id == handoff_key("fulfillment", status.handoff_id)
    assert receipt.result.fulfillment_receipt_id == control_key("inbox-ack-evidence", status.delivery_id)
    assert receipt.result.channel_handoff_id == status.handoff_id
    assert receipt.control_request_digest == "1587f356ec8013de6fc6c2d5ae91e3ed1b7d0f90a0ddc087153d99f7a624a668"
    # This read does not touch the parent's live SQLite or update UI UNKNOWN.


def offline_browser():
    """Local visual test with an existing UNKNOWN journal and no saved challenge."""
    import argparse
    import uvicorn
    from web_test_console.bridge import create_app
    parser = argparse.ArgumentParser(description=offline_browser.__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    fixture = small_client.__wrapped__(args.directory, signing_key.__wrapped__())
    c = next(fixture)
    original_unknown(c)
    reconcile_transport(c)
    transport = c.backend._transport.handle_request
    trace = []
    def traced(request):
        trace.append({"method": request.method, "path": request.url.path})
        (args.directory/"manual-http-trace.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")
        return transport(request)
    c.backend._transport = httpx.MockTransport(traced)
    app = create_app(c.config, c.tmp/"once.sqlite3", backend=c.backend,
        token_provider=lambda: c.bearer, clock=c.clock, port=args.port, environment="OFFLINE_TEST")
    assert app.state.console.results == {}
    print("OFFLINE only: existing UNKNOWN ACK, restarted bridge, no challenge/payload memory; no Azure.")
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False, server_header=False)
    finally:
        fixture.close()


if __name__ == "__main__":
    offline_browser()
