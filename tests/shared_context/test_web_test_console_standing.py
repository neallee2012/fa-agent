"""Standing HTTP reader/submit integration on real offline producers and JWT."""
import copy
import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import TENANT
from test_m3_auth import signing_key, jwk_client
from test_web_test_console_browser_fixture import assistant_harness
from work_policy_lifecycle_helpers import enabled
from shared_context.behavior_assistant import _LocalHttp
from shared_context.behavior_assistant_wire import AssistantSubmit
from shared_context.behavior_work_policy import derive_work, CostLedger
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.behavior_workers import worker_key, ExecutorEvidence
from shared_context.behavior_operations import _row, control_key
from shared_context.source_materials import digest
from shared_context.errors import Problem
from web_test_console.adapter import parse_config, canonical, sha
from web_test_console.bridge import create_app
from web_test_console_standing_helpers import open_ui, configuration


@pytest.fixture
def standing_client(auth, jwk_client, signing_key, tmp_path, monkeypatch):
    import assistant_sdk_fixture
    monkeypatch.setattr(assistant_sdk_fixture, "TENANT", TENANT)
    original = assistant_harness(auth, jwk_client, signing_key)
    h, control, human, observations = enabled(original, after_global=True)
    executor = assistant_sdk_fixture.SdkProtocolExecutor(h)
    h.workers.adapter = executor.adapter()
    composition = SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: control.installed(),
        require_current_native_evidence=lambda: h.delivery._readiness())
    install_work_policy_routes(h.app, composition)
    local = _LocalHttp(h.client)
    case = SimpleNamespace(h=h, control=control, human=human, facts=observations, executor=executor,
        calls=[], preflights=[], mutate=lambda path, response: response, lost=False, tmp=tmp_path)
    def send(request):
        assert request.url.host == "standing.example.org"
        before = copy.deepcopy(h.transport.backing.rows)
        response = local.handle_request(request)
        case.calls.append({"method":request.method,"path":request.url.path,"body":request.content,"status":response.status_code})
        if request.url.path == "/work-policy/v1/preflight":
            assert h.transport.backing.rows == before  # Full exact inventory, not just counts.
            if response.status_code == 200:
                case.preflights.append(response.json())
        if request.url.path == "/work-policy/v1/submit" and case.lost:
            assert response.status_code == 200
            raise httpx.ReadTimeout("OFFLINE original submit response lost")
        return case.mutate(request.url.path, response)
    case.backend = httpx.Client(transport=httpx.MockTransport(send), follow_redirects=False)
    case.config = parse_config(json.dumps(configuration(h, control)).encode())
    case.browser = open_ui(case, tmp_path/"journal.sqlite3")
    yield case
    case.browser.ui.close()
    case.backend.close()
    h.client.close()


def action(c, phase, task="A"):
    return c.browser.ui.post("/api/tasks/"+task+"/"+phase, json={}, headers=c.browser.headers)


@pytest.mark.parametrize("field,value", [("submission", "lifecycle"), ("binding_id", "another-binding")])
def test_a_standing_task_that_departs_from_the_policy_derivation_is_refused(standing_client, field, value):
    raw = configuration(standing_client.h, standing_client.control)
    raw["tasks"][0][field] = value
    with pytest.raises(ValueError, match="standing tasks must exactly match the original deterministic policy derivation"):
        parse_config(json.dumps(raw).encode())
