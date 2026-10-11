"""Internal error enums and validated command IDs only; public projections stay opaque."""

import json
import logging

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import RootModel

from shared_context.behavior_api import _body, _install_error_handlers
from shared_context.behavior_wire import Command, WireModel
from shared_context.errors import Problem
from shared_context.behavior_wire import SourcePrepare
from test_behavior_api import METRICS, h
from test_m3_auth import jwk_client, signing_key


LOGGER = "shared_context.behavior_api"
SECRET = "SYNTHETIC_PRIVATE_ERROR_CONTENT"


class MappedCommand(WireModel):
    mapping: Command


class RootCommand(RootModel[Command]):
    pass


def app_for(status, code, *, model=Command, parse=True, unexpected=False):
    app = FastAPI()
    _install_error_handlers(app)

    @app.post("/probe")
    async def failed(request: Request):
        if parse:
            await _body(request, model)
        if unexpected:
            raise RuntimeError(SECRET)
        raise Problem(status, code, SECRET, detail=SECRET, token=SECRET)
    return app


def diagnostics(caplog):
    rows = [record for record in caplog.records if record.name == LOGGER]
    assert len(rows) == 1
    prefix, raw = rows[0].getMessage().split(" ", 1)
    assert prefix == "BEHAVIOR_REQUEST_FAILED"
    value = json.loads(raw)
    assert set(value) == {"code", "request_id"}
    assert SECRET not in rows[0].getMessage()
    return value


@pytest.mark.parametrize("status,code,public", [
    (403, "BEHAVIOR_SOURCE_DENIED", "BEHAVIOR_ACCESS_DENIED"),
    (409, "OPERATION_RECORD_MISSING", "BEHAVIOR_CONFLICT"),
    (412, "WORK_POLICY_COST_CHANGED", "BEHAVIOR_CONFLICT"),
    (413, "BEHAVIOR_ENTITY_TOO_LARGE", "BEHAVIOR_BODY_LIMIT"),
    (503, "BEHAVIOR_COMMIT_UNKNOWN", "BEHAVIOR_COMMIT_UNKNOWN"),
])
def test_logs_internal_enum_and_validated_id_without_changing_public_error(caplog, status, code, public):
    with caplog.at_level(logging.WARNING, logger=LOGGER), TestClient(app_for(status, code)) as client:
        response = client.post("/probe", json={"request_id": "original-memory-bridge"},
                               headers={"Authorization": "Bearer " + SECRET, "x-request-id": SECRET})
    assert response.status_code == status
    assert response.json() == {"error": {
        "code": public, "message": "The bounded operation could not be completed.",
        "reconcile_original": code == "BEHAVIOR_COMMIT_UNKNOWN", "automatic_retry": False,
    }}
    assert diagnostics(caplog) == {"code": code, "request_id": "original-memory-bridge"}
    assert SECRET not in response.text


@pytest.mark.parametrize("model,body", [
    (RootCommand, {"request_id": "original-submit"}),
    (MappedCommand, {"mapping": {"request_id": "original-bridge-mapping"}}),
])
def test_root_and_bridge_models_retain_only_their_validated_request_id(caplog, model, body):
    with caplog.at_level(logging.WARNING, logger=LOGGER), TestClient(app_for(403, "BEHAVIOR_SOURCE_DENIED", model=model)) as client:
        response = client.post("/probe", json=body)
    assert response.status_code == 403
    assert diagnostics(caplog)["request_id"] == (body.get("request_id") or body["mapping"]["request_id"])


def test_unparsed_request_body_and_headers_cannot_supply_log_identifiers(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER), TestClient(app_for(403, "BEHAVIOR_ACCESS_DENIED", parse=False)) as client:
        response = client.post("/probe", json={"request_id": SECRET, "token": SECRET}, headers={"x-request-id": SECRET})
    assert response.status_code == 403
    assert diagnostics(caplog) == {"code": "BEHAVIOR_ACCESS_DENIED", "request_id": None}


def test_credential_shaped_valid_identifier_is_redacted(caplog):
    credential = "ghp_" + "A" * 30
    with caplog.at_level(logging.WARNING, logger=LOGGER), TestClient(app_for(409, "BEHAVIOR_CONFLICT")) as client:
        response = client.post("/probe", json={"request_id": credential})
    assert response.status_code == 409
    assert diagnostics(caplog)["request_id"] is None
    assert credential not in caplog.text


def test_unexpected_failure_does_not_log_exception_text(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER), TestClient(
            app_for(503, "UNUSED", unexpected=True), raise_server_exceptions=False) as client:
        response = client.post("/probe", json={"request_id": "original-unexpected"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BEHAVIOR_DEPENDENCY_UNAVAILABLE"
    assert diagnostics(caplog) == {"code": "UNCLASSIFIED", "request_id": "original-unexpected"}
    assert SECRET not in caplog.text


def test_non_enum_code_is_not_copied_into_the_log(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER), TestClient(
            app_for(403, "SOURCE_DENIED\nBearer " + SECRET)) as client:
        response = client.post("/probe", json={"request_id": "original-invalid-code"})
    assert response.status_code == 403
    assert diagnostics(caplog) == {"code": "UNCLASSIFIED", "request_id": "original-invalid-code"}
    assert SECRET not in caplog.text


def test_source_commit_missing_plan_logs_409_with_original_id(h, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        response = h.post("/sources/metrics/commit", Command(request_id="original-missing-plan"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BEHAVIOR_CONFLICT"
    assert diagnostics(caplog) == {"code": "OPERATION_RECORD_MISSING", "request_id": "original-missing-plan"}


@pytest.mark.parametrize("stage", ["commit", "reconcile"])
def test_source_commit_or_reconcile_unknown_logs_503_without_body_or_retry(h, caplog, stage):
    command = SourcePrepare(request_id="original-pending-source", version=1, original=METRICS)
    assert h.post("/sources/metrics/prepare", command).status_code == 200
    if stage == "commit":
        h.transport.after_apply = "lost"
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        response = h.post("/sources/metrics/" + stage, Command(request_id=command.request_id))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    assert response.json()["error"]["automatic_retry"] is False
    assert diagnostics(caplog) == {"code": "BEHAVIOR_COMMIT_UNKNOWN", "request_id": command.request_id}
    assert METRICS.decode("utf-8") not in caplog.text and h.executor.starts == []
