"""OFFLINE localhost bridge compatibility and generic safety checks."""
import json
import base64
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import TENANT
from test_behavior_api import RuntimeHarness
from test_m3_auth import CLIENT_ID, HUMAN_OID, jwk_client, signing_key, token
from web_test_console.adapter import ConsoleConfig, SteadyConsoleConfig, TokenVault
from web_test_console.bridge import create_app


@pytest.fixture
def console(auth, jwk_client, signing_key, tmp_path):
    """Legacy normal_pull config kept loadable for rollback compatibility only."""
    h = RuntimeHarness(auth, jwk_client, signing_key)
    raw = {
        "schema_version": "fa-web-test-console-v1", "scope_id": "offline-console",
        "backend_origin": "https://backend.example.org",
        "tenant_id": TENANT, "human_oid": HUMAN_OID, "client_id": CLIENT_ID, "audience": auth.audience,
        "not_before": h.time.isoformat(), "expires_at": (h.time+timedelta(minutes=30)).isoformat(),
        "approval_reference": "OFFLINE-fixture-only", "approval_sha256": "a"*64,
        "backend_configuration_sha256": "b"*64, "backend_core_sha256": "c"*64,
        "tasks": [{
            "task_id": "proof", "label": "離線固定測試", "description": "相容 fixture；P05 後不列出也不執行",
            "mode": "normal_pull", "submission": "lifecycle",
            "binding_id": h.binding.binding_id, "job_id": h.binding.job_id, "work_id": h.binding.work.work_id,
            "event_id": h.binding.event_id, "root_request_id": h.binding.root_request_id,
            "obligation_id": h.binding.obligation_id, "owner_record_id": h.binding.owner.owner_record_id,
            "route": h.binding.route.model_dump(mode="json"), "channel_version": h.config.channel.version,
            "submit_request_id": "fixed-submit", "fetch_request_id": "fixed-fetch", "ack_request_id": "fixed-ack",
            "allowed_actions": ["status", "submit", "fetch", "acknowledge"],
        }],
    }
    config = ConsoleConfig.model_validate_json(json.dumps(raw))
    vault = TokenVault(config, clock=lambda: h.time)
    provider = lambda: h.headers()["Authorization"].removeprefix("Bearer ")
    requests = []
    def send(request):
        requests.append(request)
        raise AssertionError("legacy normal_pull should not reach the backend")
    backend = httpx.Client(transport=httpx.MockTransport(send), follow_redirects=False)
    app = create_app(config, tmp_path/"journal.sqlite3", token_provider=provider, vault=vault,
                     backend=backend, clock=lambda: h.time, port=8765, environment="OFFLINE_TEST")
    browser = TestClient(app, base_url="http://127.0.0.1:8765")
    assert browser.get("/").status_code == 200
    state = browser.get("/api/state").json()
    headers = {"Origin": "http://127.0.0.1:8765", "X-Console-CSRF": state["csrf"]}
    h.console_config, h.browser, h.bridge_app = config, browser, app
    h.bridge_headers, h.backend_requests, h.backend_client = headers, requests, backend
    h.journal = tmp_path/"journal.sqlite3"
    h.provider, h.vault = provider, vault
    yield h
    browser.close()
    backend.close()
    h.client.close()


def action(h, name):
    return h.browser.post("/api/tasks/proof/"+name, json={}, headers=h.bridge_headers)


@pytest.fixture
def small_client(tmp_path, signing_key):
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
    claims = {"tid": cfg.tenant_id, "iss": f"https://sts.windows.net/{cfg.tenant_id}/", "aud": cfg.audience,
              "oid": cfg.human_oid, "appid": cfg.client_id, "scp": "Behavior.Read"}
    bearer = token(signing_key, claims)
    headers = {
        "Cache-Control": "no-store", "Content-Type": "application/json",
        "X-Behavior-Exchange": "offline-original-exchange", "X-Behavior-Ack-Token": "offline-memory-challenge",
        "X-Behavior-Binding": base64.urlsafe_b64encode(json.dumps(raw["binding"]).encode()).decode(),
        "X-Behavior-Payload-SHA256": raw["binding"]["payload_digest"], "X-Behavior-Confirmation-Level": "client_received",
    }
    pending = {**raw["status"], "fulfilled": False, "fulfillment": "pending",
               "confirmation_level": None, "confirmed_levels": [], "expired": False}
    case = SimpleNamespace(raw=raw, config=cfg, bearer=bearer, claims=claims, calls=[], fulfilled=False,
                           token=signing_key, tmp=tmp_path, status_override=None, deny=None)
    prefix = "/web/v1/original-failure/" + task["handoff_id"]
    def send(request):
        phase = request.url.path.rsplit("/", 1)[-1]
        assert request.url.host == "original.example" and request.url.path == prefix + "/" + phase
        case.calls.append((request.method, request.url.path, request.content))
        if case.deny:
            return httpx.Response(case.deny, json={"error": {"code": "DENIED"}})
        if phase == "status":
            return httpx.Response(200, json=case.status_override or ({**raw["status"], "expired": False} if case.fulfilled else pending))
        if phase == "fetch":
            return httpx.Response(200, content=base64.b64decode(raw["payload_base64"]), headers=headers)
        command = json.loads(request.content)
        assert command["exchange_id"] == headers["X-Behavior-Exchange"]
        case.fulfilled = True
        return httpx.Response(200, json=raw["acknowledgment"])
    backend = httpx.Client(transport=httpx.MockTransport(send), follow_redirects=False)
    case.clock = lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    app = create_app(cfg, tmp_path/"once.sqlite3", backend=backend, token_provider=lambda: bearer,
                     clock=case.clock,
                     environment="OFFLINE_TEST")
    ui = TestClient(app, base_url="http://127.0.0.1:8765")
    ui.get("/")
    csrf = ui.get("/api/state").json()["csrf"]
    case.client = SimpleNamespace(app=app, ui=ui, headers={"Origin": "http://127.0.0.1:8765", "X-Console-CSRF": csrf})
    case.backend = backend
    assert ui.post("/api/authenticate", json={}, headers=case.client.headers).status_code == 200
    yield case
    ui.close()
    backend.close()


def original_operate(client, action):
    return client.ui.post("/api/tasks/original/" + action, json={}, headers=client.headers)


def test_legacy_normal_pull_config_loads_but_is_not_a_workbench_action(console):
    state = console.browser.get("/api/state").json()
    assert state["tasks"] == []
    response = action(console, "submit")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACTION_NOT_ALLOWED"
    assert console.backend_requests == []


def test_the_api_connection_stays_open_between_warm_turns(tmp_path):
    """dev 10-07: warm turns are about 12 s apart and the 5 s keep-alive re-did TCP+TLS before each send (+~240 ms).
    httpx ignores a client's `limits` when a transport is given, so the effective pool is the one checked here."""
    from web_test_console.bridge import Console

    console = Console(None, tmp_path / "keepalive.sqlite3")
    try:
        pool = console.backend._transport._pool
        assert pool._keepalive_expiry == 120
        assert (pool._max_connections, pool._max_keepalive_connections) == (100, 20), "the pool size in effect so far"
    finally:
        console.backend.close()


def test_original_failure_transport_redirect_is_never_followed_and_is_not_success(small_client):
    c = small_client
    seen = []
    def redirected(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://other.example.org/collect"})
    c.backend._transport = httpx.MockTransport(redirected)
    assert original_operate(c.client, "status").status_code != 200
    task = c.raw["task"]
    assert seen == ["https://original.example/web/v1/original-failure/" + task["handoff_id"] + "/status"]


def test_original_failure_wrong_identity_csrf_and_backend_signature_stay_real_gates(small_client):
    c = small_client
    c.client.app.state.console.vault.clear()
    c.client.app.state.console.token_provider = lambda: token(c.token, {**c.claims, "oid": "wrong-human"})
    assert c.client.ui.post("/api/authenticate", json={}, headers=c.client.headers).status_code == 403
    assert original_operate(c.client, "fetch").status_code == 401 and c.calls == []
    c.client.app.state.console.token_provider = lambda: c.bearer
    assert c.client.ui.post("/api/authenticate", json={}, headers=c.client.headers).status_code == 200
    assert c.client.ui.post("/api/tasks/original/fetch", json={}, headers={
        **c.client.headers, "Origin": "https://other.example"}).status_code == 403


def test_original_failure_config_cannot_be_replaced_to_reset_old_once_journal(small_client):
    from web_test_console.adapter import ConsoleError
    c = small_client
    assert original_operate(c.client, "status").status_code == 200
    raw = c.config.model_dump(mode="json")
    raw["tasks"][0]["fetch_request_id"] = "another-fetch"
    changed = type(c.config).model_validate_json(json.dumps(raw))
    with pytest.raises(ConsoleError, match="CONFIG_CHANGED"):
        create_app(changed, c.tmp/"once.sqlite3", backend=c.backend, clock=c.clock)


def test_cli_uses_only_fixed_isolated_get_token_and_never_logs_or_returns_it(small_client, tmp_path, monkeypatch, capsys):
    from web_test_console.adapter import CliTokenProvider, ConsoleError
    c = small_client
    exe = tmp_path/"az.cmd"
    exe.write_text("not executed", encoding="ascii")
    isolated = tmp_path/"project-azure"
    isolated.mkdir()
    raw = c.config.model_dump(mode="json")
    raw["cli"] = {"executable": str(exe), "azure_config_dir": str(isolated),
                  "subscription_id": "00000000-0000-0000-0000-000000000001"}
    config = type(c.config).model_validate_json(json.dumps(raw))
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "accessToken": c.bearer, "tenant": c.config.tenant_id,
            "subscription": raw["cli"]["subscription_id"], "tokenType": "Bearer", "expires_on": 9999999999,
        }))
    monkeypatch.setattr("web_test_console.adapter.subprocess.run", run)
    assert CliTokenProvider(config)() == c.bearer
    args, options = calls[0]
    assert args[1:3] == ["account", "get-access-token"] and "login" not in args
    assert "--tenant" not in args
    assert args[args.index("--subscription")+1] == raw["cli"]["subscription_id"]
    assert args[args.index("--resource")+1] == config.audience
    assert options["env"]["AZURE_CONFIG_DIR"] == str(isolated)
    assert options["env"]["AZURE_LOGGING_ENABLE_LOG_FILE"] == "false"
    assert options["shell"] is False and options["capture_output"] is True
    assert c.bearer not in json.dumps(args)
    assert capsys.readouterr().out == ""
    def wrong(_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "accessToken": c.bearer, "tenant": "wrong-authority",
            "subscription": raw["cli"]["subscription_id"], "tokenType": "Bearer", "expires_on": 9999999999,
        }))
    monkeypatch.setattr("web_test_console.adapter.subprocess.run", wrong)
    with pytest.raises(ConsoleError, match="IDENTITY_MISMATCH"):
        CliTokenProvider(config)()


def test_preview_has_no_tasks_credentials_effects_and_configuration_is_not_a_proxy(tmp_path):
    from web_test_console.__main__ import main
    app = create_app()
    browser = TestClient(app, base_url="http://127.0.0.1:8765")
    page = browser.get("/")
    assert page.status_code == 200
    state = browser.get("/api/state").json()
    assert state["mode"] == "PREVIEW_ONLY" and state["tasks"] == []
    assert browser.get("/favicon.ico").status_code == 204
    headers = {"Origin": "http://127.0.0.1:8765", "X-Console-CSRF": state["csrf"]}
    assert browser.post("/api/authenticate", json={}, headers=headers).json()["error"]["code"] == "PREVIEW_ONLY"
    assert not list(tmp_path.iterdir())
    assert main(["serve", "--config", str(tmp_path/"absent.json"), "--config-sha256", "f"*64,
                 "--journal", str(tmp_path/"not-created.sqlite3")]) == 2
