"""Client-only original failure adapter over the frozen real handoff producer."""
import base64
import copy
import json
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from test_web_first_backend import (
    stopped_original, web_handoff_for, original_teams_gap, original_finite_worker_contract,
    next_window, next_review, original_version, bounded_initial_control_quota, closed_v2,
    original_completed_nonempty_page, installed_empty, operator, exact_source_initialization,
    fixed_original_high_water, expired_claim, source_contract, closed_case, offline_socket_guard,
    original_captured_route_producer, closed_finished, sufficient_initial_budget, durable_result,
    native_acquire, current_rotation, two_rotations, current_cancelled, rotated, jwk_client, signing_key,
)
from shared_context.behavior_assistant import _LocalHttp
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime_rotation_history import receipt_catalog
from shared_context.behavior_web_handoff import handoff_key, WebHandoffFulfillment
from shared_context.behavior_web_handoff_api import create_handoff_app
from shared_context.source_materials import digest
from test_m3_auth import token
from web_test_console.adapter import SteadyConsoleConfig
from web_test_console.bridge import create_app


def client_config(c, service, plan):
    b = c.work.binding
    return SteadyConsoleConfig.model_validate_json(json.dumps({
        "schema_version": "fa-web-test-console-steady-v2", "authorization_mode": "backend-per-work",
        "console_id": "offline-original-failure-client", "backend_origin": service.observation.receiver_origin,
        "tenant_id": c.config.identity.tenant_id, "human_oid": c.config.identity.human_oid,
        "client_id": c.config.identity.admin_client_id, "audience": c.config.identity.audience,
        "backend_configuration_sha256": c.config.sha256, "backend_core_sha256": plan.code_sha256,
        "tasks": [{
            "task_id": "original", "label": "OFFLINE｜原已停止工作結果", "description": "合成資料；正式後端 producer，不是原生收件。",
            "mode": "original_failure", "binding_id": b.binding_id, "job_id": b.job_id, "work_id": b.work.work_id,
            "event_id": b.event_id, "root_request_id": b.root_request_id, "obligation_id": b.obligation_id,
            "owner_record_id": b.owner.owner_record_id, "route": plan.web_delivery.route.model_dump(mode="json"),
            "channel_version": plan.web_delivery.channel_version, "handoff_id": service.scope.handoff_id,
            "submit_request_id": "unused-no-submit", "fetch_request_id": "original-web-fetch",
            "ack_request_id": "original-web-ack", "allowed_actions": ["status", "fetch", "acknowledge"],
            "original_failure": {"contract": "fa-original-web-handoff-status-v1",
                "payload_sha256": plan.payload.sha256, "payload_bytes": len(plan.payload.data),
                "delivery_entry_id": c.config.delivery.entry_id, "owner_conversation_id": b.owner.conversation_id},
        }],
    }))


def browser(config, journal, backend, provider, clock):
    app = create_app(config, journal, backend=backend, token_provider=provider, clock=clock, environment="OFFLINE_TEST")
    ui = TestClient(app, base_url="http://127.0.0.1:8765")
    ui.get("/")
    headers = {"Origin": "http://127.0.0.1:8765", "X-Console-CSRF": ui.get("/api/state").json()["csrf"]}
    login = ui.post("/api/authenticate", json={}, headers=headers)
    assert login.status_code == 200, login.text
    return SimpleNamespace(app=app, ui=ui, headers=headers)


def operate(client, action):
    return client.ui.post("/api/tasks/original/"+action, json={}, headers=client.headers)


def save(name, value):
    if folder := os.environ.get("FAILURE_CLIENT_RESULTS"):
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        (path/(name+".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("source_contract", ["claim"], indirect=True)
def test_formal_handoff_same_readonly_human_exact_failure_manual_ack_and_history(stopped_original, tmp_path, monkeypatch):
    c, old, _, stops = stopped_original
    service = web_handoff_for(stopped_original)
    plan = service.preview(c.principal)
    service.authorize(c.principal, plan, "offline-separate-operator-approved")
    app = create_handoff_app(c.parent, service.scope, observation=service.observation,
        current_observation=service.current_observation, original_scope=old.scope)
    api = TestClient(app)
    identity = c.config.identity
    claims = {"tid": identity.tenant_id, "iss": f"https://sts.windows.net/{identity.tenant_id}/",
        "aud": identity.audience, "oid": identity.human_oid, "appid": identity.admin_client_id, "scp": "Behavior.Read"}
    bearer = token(c.h.c.signing_key, claims)
    cfg = client_config(c, service, plan)
    calls = []
    local = _LocalHttp(api)
    def send(request):
        calls.append((request.method, request.url.path))
        return local.handle_request(request)
    backend = httpx.Client(transport=httpx.MockTransport(send), follow_redirects=False)
    client = browser(cfg, tmp_path/"client.sqlite3", backend, lambda: bearer, lambda: c.clock[0])
    b, inv0 = c.work.binding, c.store.read_inventory()
    backing = c.transport.backing
    baseline, sequence, now = copy.deepcopy(backing.rows), backing.sequence, c.clock[0]
    root = control_key("budget", b.owner.root_budget_id)
    quota = control_key("delivery-quota", "root", b.owner.root_budget_id)
    obkey = control_key("delivery-obligation", b.obligation_id)
    assert inv0.rows[obkey].payload["data"]["fulfillment"] == "pending"
    status = operate(client, "status")
    assert status.status_code == 200, status.text
    assert c.store.read_inventory() == inv0
    assert status.json()["task"]["status"]["data"]["work_state"] == "cancelled"
    assert operate(client, "submit").status_code == 403
    fetched = operate(client, "fetch")
    assert fetched.status_code == 200, fetched.text
    shown = fetched.json()["task"]
    assert shown["result"]["message_kind"] == "failure" and shown["result"]["work_state"] == "cancelled"
    assert shown["acknowledgment"]["state"] == "NOT_SENT"
    raw = client.ui.get("/api/tasks/original/payload")
    assert raw.content == plan.payload.data
    inv1 = c.store.read_inventory()
    assert inv1.rows[obkey].payload["data"]["fulfillment"] == "pending"
    assert inv1.rows[quota].payload["data"]["pending"] == inv0.rows[quota].payload["data"]["pending"]
    installed_origin(MigrationStore(c.parent.clients[0], c.config.table.scope), c.config)
    receipt_catalog(inv1)
    # The formal backend, not a client retry, guarantees another request label
    # cannot mint a second exchange or grant. Only this offline test exercises it.
    primary = "/web/v1/original-failure/"+service.scope.handoff_id
    original_exchange = client.app.state.console.results["original"].exchange_id
    repeat_fetch = api.post(primary+"/fetch", json={"request_id": "different-fixture-label"},
                            headers={"Authorization": "Bearer "+bearer})
    assert repeat_fetch.status_code == 200 and repeat_fetch.headers["x-behavior-exchange"] == original_exchange
    count = len(calls)
    assert operate(client, "fetch").status_code == 200 and len(calls) == count
    acknowledged = operate(client, "acknowledge")
    assert acknowledged.status_code == 200, acknowledged.text
    receipt = acknowledged.json()["task"]["acknowledgment"]["receipt"]
    assert set(receipt) == {"result", "control_request_id", "control_request_digest"}
    original = c.store.read_inventory().receipts[receipt["control_request_id"]]
    assert receipt["control_request_id"] == handoff_key("fulfillment", service.scope.handoff_id)
    assert receipt["control_request_digest"] == original.payload["request"]["request_digest"]
    fulfillment = c.store.read_snapshot((receipt["control_request_id"],))[receipt["control_request_id"]]
    value = WebHandoffFulfillment.model_validate_json(json.dumps(fulfillment.payload["data"]))
    assert receipt["control_request_digest"] == digest(value.receipt.model_dump(mode="json"))
    assert receipt["result"] == original.payload["result"]
    inv2 = c.store.read_inventory()
    assert inv2.rows[quota].payload["data"]["pending"] == inv0.rows[quota].payload["data"]["pending"]-1
    assert inv2.rows[root] == inv0.rows[root]
    assert inv2.rows[quota].payload["data"]["scans_remaining"] == inv0.rows[quota].payload["data"]["scans_remaining"]
    assert inv2.rows[obkey].payload["data"]["route"] == inv0.rows[obkey].payload["data"]["route"]
    assert all(inv2.receipts[k] == v for k, v in inv0.receipts.items())
    installed_origin(MigrationStore(c.parent.clients[0], c.config.table.scope), c.config)
    receipt_catalog(inv2)
    count = len(calls)
    assert operate(client, "acknowledge").status_code == 200 and len(calls) == count
    assert c.sends == [] and len(stops) == 1
    assert all("/operator/" not in path and not path.endswith("/reconcile") for _, path in calls)
    save("formal-http-client-proof", {
        "actual_environment": "NOT_RUN", "same_human_scope": "Behavior.Read",
        "result": shown["result"], "backend_response": receipt, "original_request": original.payload["request"],
        "original_write_set": original.payload["writes"], "proof_canonical_digest": digest(value.receipt.model_dump(mode="json")),
        "http_calls": calls, "history_after_fetch_and_ack": "PASS", "root_budget_unchanged": True,
        "scans_unchanged": True, "pending_only_released_after_ack": True, "additional_stop_or_teams": 0,
    })
    client.ui.close()
    # Restore only isolated fixture snapshots; never a production row rewrite.
    from azure.core.exceptions import ServiceRequestError
    from test_behavior_store import parse_batch
    from test_behavior_settlement import write
    denied = {}
    original_send = c.transport.send
    for fault in ("expired", "owner-revoked", "inbox-ack-lost", "fulfillment-lost"):
        backing.rows, backing.sequence, c.clock[0] = copy.deepcopy(baseline), sequence, now
        fresh = browser(cfg, tmp_path/(fault+".sqlite3"), backend, lambda: bearer, lambda: c.clock[0])
        if fault == "expired":
            c.clock[0] = service.scope.expires_at
            response = operate(fresh, "status")
            assert response.status_code == 200 and response.json()["task"]["status"]["data"]["expired"] is True
            frozen = c.store.read_inventory()
            assert operate(fresh, "fetch").status_code == 403
            assert c.store.read_inventory() == frozen
            denied[fault] = {"read_status": 200, "fetch": 403, "no_write": True}
        elif fault == "owner-revoked":
            rid = control_key("owner", b.owner.owner_record_id)
            row = c.store.read_snapshot((rid,))[rid]
            write(c.store, rid, {**row.payload["data"], "active": False})
            frozen = c.store.read_inventory()
            response = operate(fresh, "fetch")
            assert response.status_code >= 400 and c.store.read_inventory() == frozen
            denied[fault] = {"fetch": response.status_code, "no_write": True}
        else:
            assert operate(fresh, "fetch").status_code == 200
            target = (control_key("inbox-acknowledge", plan.web_delivery.delivery_id) if fault == "inbox-ack-lost"
                      else handoff_key("fulfillment", service.scope.handoff_id))
            writes = []
            def lose(request, **kwargs):
                response = original_send(request, **kwargs)
                if request.method == "POST":
                    ops, _ = parse_batch(request)
                    receipt = next(json.loads(op[1]["Payload"]) for op in ops
                                   if "behavior:v1:receipt:" in op[1]["RowKey"])
                    if receipt["request"]["request_id"] == target:
                        writes.append(receipt)
                        raise ServiceRequestError("OFFLINE original EGT response lost")
                return response
            with monkeypatch.context() as patch:
                patch.setattr(c.transport, "send", lose)
                response = operate(fresh, "acknowledge")
            assert response.status_code != 200 and len(writes) == 1
            assert target in c.store.read_inventory().receipts
            inv_unknown = c.store.read_inventory()
            count = len(calls)
            assert operate(fresh, "acknowledge").status_code == 200 and len(calls) == count
            view = operate(fresh, "status").json()["task"]
            assert view["acknowledgment"]["state"] == "UNKNOWN"
            assert view["status"]["data"]["fulfilled"] is (fault == "fulfillment-lost")
            assert c.store.read_inventory() == inv_unknown
            restarted = browser(cfg, tmp_path/(fault+".sqlite3"), backend, lambda: bearer, lambda: c.clock[0])
            count = len(calls)
            assert operate(restarted, "acknowledge").json()["task"]["acknowledgment"]["state"] == "UNKNOWN"
            assert operate(restarted, "fetch").json()["error"]["code"] == "CHALLENGE_LOST"
            assert len(calls) == count
            installed_origin(MigrationStore(c.parent.clients[0], c.config.table.scope), c.config)
            receipt_catalog(c.store.read_inventory())
            denied[fault] = {"response_status": response.status_code, "exact_egt": target, "writes": 1,
                "ui_ack": "UNKNOWN", "status_fulfilled": view["status"]["data"]["fulfilled"],
                "no_retry_or_reconcile": True, "history": "PASS"}
            restarted.ui.close()
        fresh.ui.close()
    assert c.sends == [] and len(stops) == 1
    assert all(not path.endswith("/reconcile") for _, path in calls)
    save("formal-backend-denials-ackloss", {"actual_environment": "NOT_RUN", "cases": denied})
    backend.close()
    api.close()


@pytest.fixture
def small_client(tmp_path, signing_key):
    """Replay SHA-bound official producer examples, not a second fake authority."""
    from datetime import datetime, timezone
    raw = json.loads((Path(__file__).with_name("web_test_console_original_failure")/"producer-example.json").read_bytes())
    task = raw["task"]
    cfg = SteadyConsoleConfig.model_validate_json(json.dumps({
        "schema_version": "fa-web-test-console-steady-v2", "authorization_mode": "backend-per-work",
        "console_id": "offline-replayed-contract", "backend_origin": "https://original.example",
        "tenant_id": task["route"]["tenant_id"], "human_oid": task["route"]["recipient_id"],
        "client_id": "offline-delegated-client", "audience": "api://offline-receiver",
        "backend_configuration_sha256": raw["configuration_sha256"],
        "backend_core_sha256": "9a7c972abbcc2f5613e25f70f3d857e1a8a665e9d17fd8714025464a26eb438c",
        "tasks": [task],
    }))
    claim = {"tid": cfg.tenant_id, "iss": f"https://sts.windows.net/{cfg.tenant_id}/", "aud": cfg.audience,
             "oid": cfg.human_oid, "appid": cfg.client_id, "scp": "Behavior.Read"}
    bearer = token(signing_key, claim)
    headers = {
        "Cache-Control": "no-store", "Content-Type": "application/json",
        "X-Behavior-Exchange": "offline-original-exchange", "X-Behavior-Ack-Token": "offline-memory-challenge",
        "X-Behavior-Binding": base64.urlsafe_b64encode(json.dumps(raw["binding"]).encode()).decode(),
        "X-Behavior-Payload-SHA256": raw["binding"]["payload_digest"], "X-Behavior-Confirmation-Level": "client_received",
    }
    pending = {**raw["status"], "fulfilled": False, "fulfillment": "pending",
               "confirmation_level": None, "confirmed_levels": [], "expired": False}
    case = SimpleNamespace(raw=raw, config=cfg, bearer=bearer, claims=claim, calls=[], mutate=lambda phase, response: response,
        fulfilled=False, lost=None, status_override=None, deny=None, token=signing_key, tmp=tmp_path)
    prefix = "/web/v1/original-failure/"+task["handoff_id"]
    def send(request):
        phase = request.url.path.rsplit("/", 1)[-1]
        assert request.url.host == "original.example" and request.url.path == prefix+"/"+phase
        assert phase in {"status", "fetch", "acknowledge"}
        case.calls.append((request.method, request.url.path, request.content))
        if case.deny:
            return httpx.Response(case.deny, json={"error": {"code": "DENIED"}})
        if phase == "status":
            status = {**raw["status"], "expired": False} if case.fulfilled else pending
            response = httpx.Response(200, json=case.status_override or status)
        elif phase == "fetch":
            response = httpx.Response(200, content=base64.b64decode(raw["payload_base64"]), headers=headers)
        else:
            command = json.loads(request.content)
            assert command["exchange_id"] == headers["X-Behavior-Exchange"]
            assert command["acknowledgment_token"] == headers["X-Behavior-Ack-Token"]
            assert command["binding"] == raw["binding"] and command["confirmation_level"] == "client_received"
            case.fulfilled = True
            response = httpx.Response(200, json=raw["acknowledgment"])
        if case.lost == phase:
            raise httpx.ReadTimeout("OFFLINE result unknown")
        return case.mutate(phase, response)
    backend = httpx.Client(transport=httpx.MockTransport(send), follow_redirects=False)
    clock = lambda: datetime.now(timezone.utc)
    case.client = browser(cfg, tmp_path/"once.sqlite3", backend, lambda: case.bearer, clock)
    case.backend, case.clock = backend, clock
    yield case
    case.client.ui.close()
    backend.close()


@pytest.mark.parametrize("fault", ["recipient", "payload-sha", "handoff-id", "job-id", "level", "inconsistent-fulfilled",
                                   "dispatch", "extra-status", "version", "payload-work", "payload-owner", "ack-guard",
                                   "ack-id", "ack-handoff", "ack-evidence", "ack-digest", "ack-fullfillment", "ack-level"])
def test_formal_exact_response_contract_denies_wrong_scope_or_receipt(small_client, fault):
    c = small_client
    target = "acknowledge" if fault.startswith("ack-") else "status" if fault in {
        "handoff-id", "job-id", "inconsistent-fulfilled", "dispatch", "extra-status"} else "fetch"
    def mutate(phase, response):
        if phase != target:
            return response
        if phase in {"status", "acknowledge"}:
            data = response.json()
            if fault == "handoff-id": data["handoff_id"] = "wrong"
            elif fault == "job-id": data["job_id"] = "wrong"
            elif fault == "inconsistent-fulfilled": data["fulfilled"] = True
            elif fault == "dispatch": data["dispatch_performed"] = True
            elif fault == "extra-status": data["extra"] = True
            elif fault == "ack-guard": data["guard_revision"] = 1
            elif fault == "ack-id": data["control_request_id"] = "wrong"
            elif fault == "ack-handoff": data["result"]["channel_handoff_id"] = "wrong"
            elif fault == "ack-evidence": data["result"]["fulfillment_receipt_id"] = "wrong"
            elif fault == "ack-digest": data["control_request_digest"] = "not-a-digest"
            elif fault == "ack-fullfillment": data["result"]["fulfillment"] = "pending"
            elif fault == "ack-level": data["result"]["confirmation_level"] = "provider_accepted"
            return httpx.Response(200, json=data)
        headers = dict(response.headers)
        body = response.content
        binding = json.loads(base64.urlsafe_b64decode(headers["x-behavior-binding"]))
        if fault == "recipient": binding["route"]["recipient_id"] = "wrong"
        elif fault == "version": binding["version"] = 2
        elif fault == "payload-sha": headers["x-behavior-payload-sha256"] = "f"*64
        elif fault == "level": headers["x-behavior-confirmation-level"] = "provider_accepted"
        else:
            payload = json.loads(body)
            payload["work_id" if fault == "payload-work" else "responsibility_owner"] = "wrong"
            body = json.dumps(payload).encode()
            binding["payload_digest"] = headers["x-behavior-payload-sha256"] = __import__("hashlib").sha256(body).hexdigest()
        headers["x-behavior-binding"] = base64.urlsafe_b64encode(json.dumps(binding).encode()).decode()
        return httpx.Response(200, content=body, headers=headers)
    c.mutate = mutate
    if target == "acknowledge":
        assert operate(c.client, "fetch").status_code == 200
    response = operate(c.client, target)
    assert response.status_code == 502, (fault, response.status_code)
    view = c.client.ui.get("/api/state").json()["tasks"][0]
    assert view["acknowledgment"]["state"] != "CONFIRMED"
    if target == "fetch":
        assert view["result"] is None


def test_acklost_statusfulfilled_restart_never_washes_unknown_or_posts_again(small_client):
    c = small_client
    assert operate(c.client, "fetch").status_code == 200
    c.lost = "acknowledge"
    assert operate(c.client, "acknowledge").status_code == 503
    assert c.fulfilled
    count = len(c.calls)
    assert operate(c.client, "acknowledge").json()["task"]["acknowledgment"]["state"] == "UNKNOWN"
    assert len(c.calls) == count
    view = operate(c.client, "status").json()["task"]
    assert view["status"]["data"]["fulfilled"] and view["acknowledgment"]["state"] == "UNKNOWN"
    restarted = browser(c.config, c.tmp/"once.sqlite3", c.backend, lambda: c.bearer, c.clock)
    count = len(c.calls)
    assert operate(restarted, "fetch").json()["error"]["code"] == "CHALLENGE_LOST"
    assert operate(restarted, "acknowledge").json()["task"]["acknowledgment"]["state"] == "UNKNOWN"
    assert len(c.calls) == count
    assert all(not path.endswith("/reconcile") for _, path, _ in c.calls)
    restarted.ui.close()


def test_wrong_user_csrf_unsupported_actions_and_expired_read_do_not_write(small_client):
    c = small_client
    client = c.client
    client.app.state.console.vault.clear()
    client.app.state.console.token_provider = lambda: token(c.token, {**c.claims, "oid": "wrong-human"})
    assert client.ui.post("/api/authenticate", json={}, headers=client.headers).status_code == 403
    assert operate(client, "fetch").status_code == 401 and c.calls == []
    client.app.state.console.token_provider = lambda: c.bearer
    assert client.ui.post("/api/authenticate", json={}, headers=client.headers).status_code == 200
    assert client.ui.post("/api/tasks/original/fetch", json={}, headers={
        **client.headers, "Origin": "https://other.example"}).status_code == 403
    for action in ("submit", "approve", "authorize", "stop", "model", "reconcile"):
        assert operate(client, action).status_code == 403
    assert c.calls == []
    c.status_override = {**c.raw["status"], "expired": True}
    assert operate(client, "status").status_code == 200
    view = client.ui.get("/api/state").json()["tasks"][0]
    assert view["actions"]["status"]["enabled"] and not view["actions"]["fetch"]["enabled"]
    assert operate(client, "fetch").status_code == 403
    assert all(method == "GET" for method, _, _ in c.calls)


def test_client_verifies_public_semantics_but_does_not_invent_server_proof_digest(small_client):
    c = small_client
    assert operate(c.client, "fetch").status_code == 200
    response = operate(c.client, "acknowledge")
    assert response.status_code == 200
    receipt = response.json()["task"]["acknowledgment"]["receipt"]
    # Golden response is bound to the actual backend proof by the formal
    # integration test. The proof is NOT included in the three-field response.
    assert receipt == c.raw["acknowledgment"]
    assert receipt["control_request_digest"] != digest(receipt["result"])
    count = len(c.calls)
    assert operate(c.client, "acknowledge").status_code == 200 and len(c.calls) == count
    summary = c.client.ui.get("/api/tasks/original/summary").json()
    assert summary["confirmation_level"] == "client_received"
    assert "acknowledgment_token" not in json.dumps(summary)


@pytest.mark.parametrize("fault", ["mode", "contract", "entry", "conversation", "bytes", "submit", "channel"])
def test_formal_receiver_configuration_cannot_broaden_original_binding(small_client, fault):
    c = small_client
    data = c.config.model_dump(mode="json")
    task = data["tasks"][0]
    if fault == "mode":
        task["mode"] = "normal_pull"
    elif fault == "contract":
        task["original_failure"]["contract"] = "unknown"
    elif fault == "entry":
        task["original_failure"]["delivery_entry_id"] = "another"
    elif fault == "conversation":
        task["original_failure"]["owner_conversation_id"] = "another"
    elif fault == "bytes":
        task["original_failure"]["payload_bytes"] = task["max_payload_bytes"]+1
    elif fault == "submit":
        task["allowed_actions"].append("submit")
    else:
        task["route"]["channel_id"] = "msteams"
    with pytest.raises(ValueError):
        SteadyConsoleConfig.model_validate_json(json.dumps(data))
    assert c.calls == []


def offline_browser():
    """Visual smoke with SHA-bound producer response examples; not native/ASGI proof."""
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description=offline_browser.__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    args.directory.mkdir(exist_ok=False, parents=True)
    key = signing_key.__wrapped__()
    fixture = small_client.__wrapped__(args.directory, key)
    c = next(fixture)
    original = c.backend._transport.handle_request
    def logged(request):
        response = original(request)
        safe = [{"method": method, "path": path} for method, path, _ in c.calls]
        (args.directory/"http-summary.json").write_text(json.dumps(safe, indent=2), encoding="utf-8")
        return response
    c.backend._transport = httpx.MockTransport(logged)
    app = create_app(c.config, args.directory/"browser.sqlite3", backend=c.backend, token_provider=lambda: c.bearer,
                     clock=c.clock, port=args.port, environment="OFFLINE_TEST")
    print("OFFLINE saved formal producer response fixture only; no live backend, model, Teams, or CLI.")
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False, server_header=False)
    finally:
        fixture.close()


if __name__ == "__main__":
    offline_browser()
