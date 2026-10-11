"""Native-mode receipt readers with synthetic platform/storage I/O only."""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_conversation_transfer_startup import read_startup, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


@pytest.fixture(autouse=True)
def routine_loop():
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        if not loop.is_closed():
            loop.close()


@pytest.mark.parametrize("synthetic_standing_v1", ["routine"], indirect=True)
def test_native_history_keeps_the_original_routine_occurrence(synthetic_standing_v1, signing_key, routine_loop):
    c = synthetic_standing_v1
    app = FastAPI()
    _install_error_handlers(app)
    install_work_policy_routes(app, c.h.composition)
    headers = c.h.headers(signing_key, "Behavior.Read")
    body = {"scheduled_for": c.h.time.replace(second=0, microsecond=0).isoformat()}
    with TestClient(app, backend_options={"loop_factory": lambda: routine_loop}) as client:
        accepted = client.post("/work-policy/v1/routines/native-daily/trigger", json=body, headers=headers)
        assert accepted.status_code == 200
        read_startup(c)
        original = client.post(
            "/work-policy/v1/routines/native-daily/trigger-reconcile", json=body, headers=headers,
        )
        assert original.status_code == 200
        assert original.json()["binding_id"] == accepted.json()["binding_id"]
        assert original.json()["status"]["job_id"] == accepted.json()["status"]["job_id"]
    assert c.sessions.calls == []
