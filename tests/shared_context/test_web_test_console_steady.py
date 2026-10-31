"""Compatibility tests that survived removing legacy normal_pull actions."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import TENANT
from test_m3_auth import CLIENT_ID, HUMAN_OID, jwk_client, signing_key
from test_web_test_console import console  # noqa: F401 - pytest fixture
from web_test_console.adapter import ConsoleError, SteadyConsoleConfig, canonical, sha
from web_test_console.bridge import create_app
from web_test_console.instructions import InstructionConnection
from web_test_console.pull_continuation import PullContinuationSidecar


def compatibility_config(legacy):
    data = legacy.model_dump(mode="json")
    for name in ("scope_id", "not_before", "expires_at", "approval_reference", "approval_sha256"):
        data.pop(name)
    data.update(schema_version="fa-web-test-console-steady-v2", console_id="offline-steady",
                authorization_mode="backend-per-work")
    return SteadyConsoleConfig.model_validate_json(json.dumps(data))


def open_bridge(h, tmp_path, *, config=None, clock=None, name="steady"):
    config = config or compatibility_config(h.console_config)
    clock = clock or (lambda: h.time)
    app = create_app(config, tmp_path/(name+".sqlite3"), backend=h.backend_client,
                     token_provider=h.provider, clock=clock)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.get("/")
    state = client.get("/api/state").json()
    headers = {"Origin": "http://127.0.0.1:8765", "X-Console-CSRF": state["csrf"]}
    return SimpleNamespace(app=app, client=client, headers=headers, config=config)


def test_previous_connection_renews_expired_token_once_without_reposting(console, tmp_path, signing_key):
    from test_m3_auth import token
    h = console
    current = [datetime.now(timezone.utc)]
    base = {"scp": "Behavior.Read", "tid": h.console_config.tenant_id, "aud": h.console_config.audience,
            "oid": h.console_config.human_oid, "appid": h.console_config.client_id}
    initial = token(signing_key, {**base, "exp": int(current[0].timestamp()) + 600})
    renewed = token(signing_key, {**base, "exp": int(current[0].timestamp()) + 3600})
    calls = []
    def provider():
        calls.append(len(calls))
        return initial if len(calls) == 1 else renewed
    c = open_bridge(h, tmp_path, clock=lambda: current[0])
    c.app.state.console.token_provider = provider
    assert c.client.get("/api/state").json()["authentication"] == "LOGIN_REQUIRED"
    assert calls == []
    assert c.client.post("/api/authenticate", json={}, headers=c.headers).status_code == 200
    current[0] += timedelta(minutes=11)
    c.client.post("/api/authenticate/refresh", json={}, headers=c.headers)
    assert c.client.get("/api/state").json()["authentication"] == "AVAILABLE"
    for _ in range(3):
        assert c.client.post("/api/authenticate/refresh", json={}, headers=c.headers).status_code == 200
    assert len(calls) == 2, "renewed once, not in a loop"
    assert h.backend_requests == [], "renewing a connection sends nothing to the backend"
    assert initial not in c.client.get("/api/state").text
    assert renewed not in (tmp_path/"steady.sqlite3").read_bytes().decode(errors="ignore")
    c.client.close()


@pytest.mark.parametrize("failure", [None, "cli", "identity"])
def test_refresh_at_five_minutes_is_single_flight_and_failure_requires_manual_connect(
        console, tmp_path, signing_key, caplog, capsys, failure):
    from test_m3_auth import token
    current = [datetime.now(timezone.utc).replace(microsecond=0)]
    base = {"scp": "Behavior.Read", "tid": console.console_config.tenant_id,
            "aud": console.console_config.audience, "oid": console.console_config.human_oid,
            "appid": console.console_config.client_id}
    initial = token(signing_key, {**base, "exp": int(current[0].timestamp()) + 600})
    renewed = token(signing_key, {**base, "exp": int(current[0].timestamp()) + 3600})
    wrong = token(signing_key, {**base, "oid": "another-human"})
    entered, release, second_started = Event(), Event(), Event()
    calls = []
    def provider():
        calls.append(len(calls))
        if len(calls) == 1:
            return initial
        if len(calls) == 2:
            entered.set()
            assert release.wait(5), "test did not release the CLI boundary"
            if failure == "cli":
                raise ConsoleError("LOGIN_REQUIRED", 401)
            if failure == "identity":
                return wrong
        return renewed
    c = open_bridge(console, tmp_path, clock=lambda: current[0])
    c.app.state.console.token_provider = provider
    def refresh():
        return c.client.post("/api/authenticate/refresh", json={}, headers=c.headers)
    assert refresh().json()["authentication"] == "LOGIN_REQUIRED"
    assert calls == []
    assert c.client.post("/api/authenticate", json={}, headers=c.headers).status_code == 200
    current[0] += timedelta(seconds=299)
    assert refresh().json()["authentication"] == "AVAILABLE"
    assert len(calls) == 1, "not refreshed before the five-minute boundary"
    current[0] += timedelta(seconds=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(refresh)
        try:
            assert entered.wait(5), "refresh was not started at the five-minute boundary"
            def another():
                second_started.set()
                return refresh()
            second = pool.submit(another)
            assert second_started.wait(5)
        finally:
            release.set()
        responses = [first.result(timeout=5), second.result(timeout=5)]
    expected = "AVAILABLE" if failure is None else "LOGIN_EXPIRED"
    for _ in range(3):
        assert refresh().json()["authentication"] == expected
    state = c.client.get("/api/state").json()
    assert state["authentication"] == expected and len(calls) == 2
    if failure is not None:
        assert any(r.status_code == 401 and r.json()["error"]["code"] == "LOGIN_EXPIRED" for r in responses)
        assert state["authentication_refresh_at"] is None
        assert c.client.post("/api/authenticate", json={}, headers=c.headers).status_code == 200
        assert c.client.get("/api/state").json()["authentication"] == "AVAILABLE"
        assert len(calls) == 3
    assert console.backend_requests == []
    captured = capsys.readouterr()
    public = json.dumps(state) + caplog.text + captured.out + captured.err
    stored = (tmp_path/"steady.sqlite3").read_bytes().decode(errors="ignore")
    assert all(value not in public + stored for value in (initial, renewed, wrong))
    c.client.close()


def test_cached_token_is_not_refetched_in_a_loop_and_expiry_can_refresh_again(console, tmp_path, signing_key):
    from test_m3_auth import token
    current = [datetime.now(timezone.utc).replace(microsecond=0)]
    expires = int(current[0].timestamp()) + 120
    base = {"scp": "Behavior.Read", "tid": console.console_config.tenant_id,
            "aud": console.console_config.audience, "oid": console.console_config.human_oid,
            "appid": console.console_config.client_id}
    cached = token(signing_key, {**base, "exp": expires})
    renewed = token(signing_key, {**base, "exp": expires + 3600})
    calls = []
    def provider():
        calls.append(len(calls))
        return cached if len(calls) <= 2 else renewed
    c = open_bridge(console, tmp_path, clock=lambda: current[0])
    c.app.state.console.token_provider = provider
    assert c.client.post("/api/authenticate", json={}, headers=c.headers).status_code == 200
    for _ in range(3):
        result = c.client.post("/api/authenticate/refresh", json={}, headers=c.headers)
        assert result.json()["authentication"] == "AVAILABLE"
        assert result.json()["authentication_refresh_at"] == expires
    assert len(calls) == 2
    current[0] += timedelta(seconds=120)
    result = c.client.post("/api/authenticate/refresh", json={}, headers=c.headers)
    assert result.json()["authentication"] == "AVAILABLE" and len(calls) == 3
    assert result.json()["authentication_refresh_at"] == expires + 3300
    c.client.close()


def test_pull_continuation_arguments_remain_startup_compatible_without_actions(console, tmp_path, monkeypatch):
    from web_test_console.__main__ import main
    config = compatibility_config(console.console_config)
    config_path = tmp_path / "steady.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    task = console.console_config.tasks[0]
    sidecar = PullContinuationSidecar(
        schema_version="fa-normal-pull-continuation-client-v1",
        configuration_sha256=config.sha256,
        task_id=task.task_id,
        task_sha256=sha(canonical(task.model_dump(mode="json"))),
        original_intent_sha256="1" * 64,
        original_outcome_sha256="2" * 64,
        original_body_sha256="3" * 64,
        backend_configuration_sha256=config.backend_configuration_sha256,
        backend_core_sha256=config.backend_core_sha256,
        api_image_sha256="4" * 64,
        previous_payload_sha256="5" * 64,
        expected_binding={"obligation_id": task.obligation_id, "delivery_id": "offline-delivery",
            "version": 1, "payload_digest": "6" * 64, "route": task.route.model_dump(mode="json"),
            "channel_version": task.channel_version, "artifacts": ()},
        payload_bytes=1,
        result_evidence_id="offline-evidence")
    sidecar_path = tmp_path / "sidecar.json"
    sidecar_path.write_text(sidecar.model_dump_json(), encoding="utf-8")
    captured = []
    monkeypatch.setattr("web_test_console.__main__.uvicorn.run", lambda app, **options: captured.append((app, options)))
    assert main(["serve", "--config", str(config_path), "--config-sha256", sha(config_path.read_bytes()),
                 "--journal", str(tmp_path / "launcher.sqlite3"),
                 "--pull-continuation", str(sidecar_path),
                 "--pull-continuation-sha256", sha(sidecar_path.read_bytes())]) == 0
    app, _ = captured[0]
    assert "pull_continuation" not in app.state.console.task_view(config.tasks[0])
