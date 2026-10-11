"""P05 removes legacy normal_pull from the workbench while keeping old configs loadable."""
import json

from test_m3_auth import jwk_client, signing_key  # noqa: F401 - pytest fixtures
from test_web_test_console import action, console  # noqa: F401 - pytest fixture


def test_normal_pull_config_loads_but_state_offers_no_fixed_task(console):
    state = console.browser.get("/api/state").json()
    assert state["mode"] == "FIXED_TESTS"
    assert state["tasks"] == []
    assert console.backend_requests == []


def test_normal_pull_task_id_is_refused_without_backend_call(console):
    before = len(console.backend_requests)
    response = action(console, "submit")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACTION_NOT_ALLOWED"
    assert len(console.backend_requests) == before
    assert console.bridge_app.state.console.journal.get("proof", "submit") is None


def test_old_normal_pull_journal_rows_do_not_break_state(console):
    console.bridge_app.state.console.journal.begin("proof", "submit", {
        "method": "POST", "path": "/behavior/v1/bindings/binding-one/work/submit",
        "request_id": "fixed-submit", "body_sha256": "a" * 64,
        "created_at": console.time.isoformat(), "configuration_sha256": console.console_config.sha256,
        "body": {"request_id": "fixed-submit"},
    })
    console.bridge_app.state.console.journal.finish("proof", "submit", {
        "state": "UNKNOWN", "code": "BACKEND_OFFLINE", "observed_at": console.time.isoformat(),
        "automatic_retry": False,
    })
    state = console.browser.get("/api/state").json()
    assert state["tasks"] == []
    assert "fixed-submit" not in json.dumps(state)
