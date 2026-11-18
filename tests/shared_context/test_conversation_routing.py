"""Public conversation and receipt routing with SDK I/O doubles, not native proof."""

import base64
from types import SimpleNamespace

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_work_policy_api import install_work_policy_routes
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_models, runtime_path, sdk_io
from test_m3_auth import jwk_client, signing_key
from work_policy_lifecycle_helpers import enabled


@pytest.fixture
def conversations(assistant_api, monkeypatch):
    h, _, _, _ = enabled(assistant_api)
    calls = sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    return h, calls


def read_reply(h, accepted):
    path = (
        f"/work-policy/v1/conversations/{accepted['conversation_id']}"
        f"/turns/{accepted['turn_number']}/reply"
    )
    response = h.client.get(path, headers=h.headers())
    assert response.status_code == 200, response.text
    return response


def job_status(h, accepted):
    response = h.client.get(
        f"/behavior/v1/bindings/{accepted['binding_id']}/jobs/{accepted['status']['job_id']}",
        headers=h.headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def conversation_view(h, accepted):
    response = h.client.get(
        f"/work-policy/v1/conversations/{accepted['conversation_id']}", headers=h.headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()
