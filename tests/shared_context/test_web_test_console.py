"""OFFLINE localhost bridge compatibility and generic safety checks."""
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import TENANT
from test_behavior_api import RuntimeHarness
from test_m3_auth import CLIENT_ID, HUMAN_OID, jwk_client, signing_key
from web_test_console.adapter import ConsoleConfig, TokenVault
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


def test_legacy_normal_pull_config_loads_but_is_not_a_workbench_action(console):
    state = console.browser.get("/api/state").json()
    assert state["tasks"] == []
    response = action(console, "submit")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACTION_NOT_ALLOWED"
    assert console.backend_requests == []


def test_a_changed_config_cannot_reopen_the_journal_bound_to_the_original(console):
    """The journal keeps the original config's SHA: a new config must not reuse it to settle an old unknown intent."""
    from web_test_console.adapter import ConsoleError
    raw = console.console_config.model_dump(mode="json")
    raw["tasks"][0]["fetch_request_id"] = "another-fetch"
    changed = type(console.console_config).model_validate_json(json.dumps(raw))
    with pytest.raises(ConsoleError) as refused:
        create_app(changed, console.journal, backend=console.backend_client, clock=lambda: console.time)
    assert (refused.value.code, refused.value.status) == ("CONFIG_CHANGED", 409)
    create_app(console.console_config, console.journal, backend=console.backend_client, clock=lambda: console.time)


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


def test_cli_uses_only_fixed_isolated_get_token_and_never_logs_or_returns_it(console, tmp_path, monkeypatch, capsys):
    from web_test_console.adapter import CliTokenProvider, ConsoleError
    c = SimpleNamespace(config=console.console_config, bearer=console.provider())
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
