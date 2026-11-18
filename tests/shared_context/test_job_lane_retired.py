"""The legacy Hosted job lane is retired (2026-11-16): what its old entry points answer now.

Owner entries of /work-policy/v1 that started, moved, reset or cancelled legacy jobs answer 410 JOB_LANE_RETIRED
after authentication and before any body is read. The legacy agent callbacks and the generic job routes are not
served at all. Nothing is written and nothing is sent to Foundry. Offline: the real server factory over a fresh
installation, without its startup lifespan.
"""
import pytest
from fastapi.testclient import TestClient

from shared_context.behavior_server import application
from test_fresh_installation import fresh_case, initialize, writes
from test_m3_auth import jwk_client, signing_key

WP = "/work-policy/v1"
RETIRED = (
    WP + "/submit",
    WP + "/preflight",
    WP + "/submit-reconcile",
    WP + "/routines/native-daily/trigger",
    WP + "/routines/native-daily/trigger-reconcile",
    WP + "/conversations/legacy-cid/environment",
    WP + "/conversations/legacy-cid/environment-reconcile",
    WP + "/conversations/legacy-cid/reset",
    WP + "/bindings/{binding}/cancel",
)
UNSERVED = (
    ("POST", "/assistant/submit"),
    ("POST", "/assistant/launches/legacy-launch/claim"),
    ("POST", "/assistant/sessions/legacy-session/acquire"),
    ("POST", "/hosted/launches/legacy-launch/claim"),
    ("POST", "/hosted/launches/legacy-launch/probes/legacy-probe/resolve"),
    ("POST", "/hosted/launches/legacy-launch/probes/legacy-probe/report"),
    ("POST", "/work/submit"),
    ("POST", "/work/reconcile"),
    ("POST", "/children/legacy-child/enqueue"),
    ("POST", "/children/legacy-child/reconcile"),
    ("GET", "/children/legacy-child"),
    ("POST", "/operation/execute"),
    ("POST", "/operation/reconcile"),
    ("POST", "/operation/prepare"),
    ("POST", "/operation/approve"),
    ("POST", "/operation/approval-reconcile"),
    ("POST", "/operation/revoke"),
    ("POST", "/operation/revocation-reconcile"),
    ("POST", "/operation/reject"),
    ("POST", "/operation/rejection-reconcile"),
)
REFUSAL = {"error": {"code": "JOB_LANE_RETIRED",
                     "message": "The legacy job lane is retired; this request was not processed.",
                     "reconcile_original": False, "automatic_retry": False}}


@pytest.fixture
def served(fresh_case, signing_key):
    h = fresh_case
    initialize(h)
    h.client = TestClient(application(h.composition))
    h.owner = h.headers(signing_key, "Behavior.Read Behavior.Submit")
    h.binding = h.config.bindings[0].binding_id
    return h


def _unchanged(h, call):
    before, written = h.runtime.store._guard(), writes(h)
    response = call()
    assert (h.runtime.store._guard(), writes(h)) == (before, written)
    assert h.calls == []
    return response


@pytest.mark.parametrize("path", RETIRED)
def test_a_retired_job_entry_refuses_before_reading_the_body(served, path):
    h = served
    response = _unchanged(h, lambda: h.client.post(path.format(binding=h.binding), content=b"{not json",
                                                    headers={**h.owner, "Content-Type": "application/json"}))
    assert response.status_code == 410, response.text
    assert response.json() == REFUSAL


@pytest.mark.parametrize("path", RETIRED)
def test_a_retired_job_entry_still_requires_a_signed_caller(served, path):
    h = served
    response = _unchanged(h, lambda: h.client.post(path.format(binding=h.binding), json={"request_id": "r"}))
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("method,suffix", UNSERVED)
def test_legacy_callbacks_and_generic_job_routes_are_not_served(served, method, suffix):
    h = served
    path = "/behavior/v1/bindings/" + h.binding + suffix
    response = _unchanged(h, lambda: h.client.request(method, path, json={"request_id": "r"}, headers=h.owner))
    assert response.status_code == 404, response.text


def test_the_kept_readers_on_the_same_paths_still_reach_their_handlers(served):
    h = served
    environment = h.client.get(WP + "/conversations/legacy-cid/environment", headers=h.owner)
    receipt = h.client.get(WP + "/bindings/" + h.binding + "/cancel-receipt/1", headers=h.owner)
    assert (environment.status_code, environment.json()["error"]["code"]) == (404, "CONVERSATION_NOT_FOUND")
    assert (receipt.status_code, receipt.json()["error"]["code"]) == (403, "BEHAVIOR_ACCESS_DENIED")
