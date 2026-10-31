"""Routes behavior_work_policy_api itself installs, pinned literally.

Order only matters where two patterns can match one path, so only that pair is pinned.
"""

from types import SimpleNamespace

from fastapi import FastAPI
import pytest

from shared_context import behavior_work_policy_api
from shared_context.behavior_work_policy_api import install_work_policy_routes


WP = "/work-policy/v1"
OPERATOR = "/operator/v1/standing-work-policy"

DEFAULT_ROUTES = [
    ("GET", WP + "/agents"),
    ("POST", WP + "/agents"),
    ("GET", WP + "/agents/onboarding/{request_id}"),
    ("GET", WP + "/agents/{agent_id}/console-connection"),
    ("POST", WP + "/turn-usage"),
    ("GET", WP + "/bindings/{binding_id}/agent-messages"),
    ("GET", WP + "/agent-template"),
    ("POST", WP + "/agent-template/inspect"),
    ("GET", WP + "/agent-template/system-prompt"),
    ("GET", WP + "/agent-template/options"),
    ("GET", WP + "/agent-template/selection"),
    ("POST", WP + "/agent-template/select"),
    ("POST", WP + "/agent-template/selection-reconcile"),
    ("GET", WP + "/accounting"),
    ("GET", WP + "/models"),
    ("GET", WP + "/attachment-sources"),
    ("GET", WP + "/conversations/{conversation_id}/attachment-sources"),
    ("GET", WP + "/conversations/{conversation_id}/user-text-file-capability"),
    ("POST", WP + "/preflight"),
    ("POST", WP + "/submit"),
    ("POST", WP + "/routines/{routine_id}/trigger"),
    ("POST", WP + "/routines/{routine_id}/trigger-reconcile"),
    ("POST", WP + "/submit-reconcile"),
    ("GET", WP + "/conversations/{conversation_id}"),
    ("GET", WP + "/conversations/{conversation_id}/environment"),
    ("POST", WP + "/conversations/{conversation_id}/environment"),
    ("POST", WP + "/conversations/{conversation_id}/environment-reconcile"),
    ("GET", WP + "/conversations/{conversation_id}/turns/{turn_number}/context"),
    ("GET", WP + "/conversations/{conversation_id}/turns/{turn_number}/delegation"),
    ("GET", WP + "/conversations/{conversation_id}/turns/{turn_number}/reply"),
    ("GET", WP + "/conversations/{conversation_id}/turns/{turn_number}/files/{filename}"),
    ("POST", WP + "/bindings/{binding_id}/cancel"),
    ("GET", WP + "/bindings/{binding_id}/cancel-receipt/{generation}"),
    ("POST", WP + "/conversations/{conversation_id}/reset"),
    ("POST", OPERATOR + "/revoke"),
    ("POST", OPERATOR + "/budget-enforcement"),
]
ONBOARDING_ROUTES = [
    ("POST", WP + "/agents"),
    ("GET", WP + "/agents/onboarding/{request_id}"),
    ("GET", WP + "/agents/{agent_id}/console-connection"),
]
CONNECTOR_ROUTES = [
    ("GET", WP + "/agent-connectors"),
    ("POST", WP + "/agent-connectors"),
]


def installed_routes(**composition):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_work_policy_routes(app, SimpleNamespace(verifier=object(), **composition))
    return [(method, route.path) for route in app.routes for method in sorted(route.methods)
            if route.endpoint.__module__ == behavior_work_policy_api.__name__]


@pytest.fixture(autouse=True)
def default_flags(monkeypatch):
    monkeypatch.delenv("FA_TEAM_CONNECTORS_ENABLED", raising=False)


def test_default_routes_are_exactly_these():
    assert sorted(installed_routes()) == sorted(DEFAULT_ROUTES)


def test_the_onboarding_status_route_is_tried_before_the_console_connection_pattern():
    # Both match /agents/onboarding/console-connection; the first one registered wins.
    routes = installed_routes()
    assert (routes.index(("GET", WP + "/agents/onboarding/{request_id}"))
            < routes.index(("GET", WP + "/agents/{agent_id}/console-connection")))


def test_a_single_agent_app_omits_only_onboarding():
    expected = [route for route in DEFAULT_ROUTES if route not in ONBOARDING_ROUTES]
    assert sorted(installed_routes(agent_id="agent-a")) == sorted(expected)


def test_connector_routes_are_added_when_enabled(monkeypatch):
    monkeypatch.setenv("FA_TEAM_CONNECTORS_ENABLED", "1")
    assert sorted(installed_routes()) == sorted(DEFAULT_ROUTES + CONNECTOR_ROUTES)
