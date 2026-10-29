from unittest.mock import patch
import json

import httpx
import pytest

from conftest import A
from shared_context.errors import Problem
from shared_context.m3_client import ArtifactClient
from shared_context.m3_models import SkillDeactivationInput
from test_m3_artifacts import m3
from test_m3_auth import jwk_client, signing_key
from test_skill_deactivation import D1, deactivation_api, deactivation_body


def consumer_parts(case):
    client, _, human, _, _, _ = case
    consumer = ArtifactClient(client, A.team, lambda: human["Authorization"].removeprefix("Bearer "))
    snapshot = consumer.get("deactivation-skill")
    body = SkillDeactivationInput.model_validate(deactivation_body(snapshot.payload["versions"][0]))
    return consumer, snapshot, body


def test_deactivation_client_requires_the_original_decision_receipt(deactivation_api):
    consumer, snapshot, body = consumer_parts(deactivation_api)
    with patch.object(consumer.http, "stream", wraps=consumer.http.stream) as calls:
        saved = consumer.deactivate("deactivation-skill", snapshot, body)
    assert calls.call_count == 1 and saved.payload["effective_version_id"] is None
    assert saved.payload["versions"] == snapshot.payload["versions"]
    assert saved.payload["deactivations"][0]["decision_id"] == D1
    assert consumer.deactivate("deactivation-skill", snapshot, body) == saved


def test_deactivation_client_never_replays_an_unknown_post(deactivation_api):
    consumer, snapshot, body = consumer_parts(deactivation_api)
    original = consumer.http.stream
    calls = []

    def lost_response(method, path, **kwargs):
        calls.append((method, path))
        with original(method, path, **kwargs) as response:
            response.read()
            assert response.status_code == 200
        raise httpx.ReadTimeout("synthetic acknowledgement loss")

    with patch.object(consumer.http, "stream", side_effect=lost_response):
        with pytest.raises(Problem) as unknown:
            consumer.deactivate("deactivation-skill", snapshot, body)
    assert unknown.value.body["error"]["code"] == "M3_REQUEST_UNKNOWN"
    assert len(calls) == 1 and calls[0][0] == "POST"
    readback = consumer.get("deactivation-skill")
    assert readback.payload["effective_version_id"] is None
    assert readback.payload["versions"] == snapshot.payload["versions"]
    assert len(readback.payload["deactivations"]) == 1


def test_deactivation_client_does_not_accept_a_missing_effect_receipt(deactivation_api):
    consumer, snapshot, body = consumer_parts(deactivation_api)
    original = consumer._request

    def missing_receipt(*args, **kwargs):
        headers, data = original(*args, **kwargs)
        parsed = json.loads(data)
        parsed.pop("deactivations")
        return headers, json.dumps(parsed).encode("utf-8")

    with patch.object(consumer, "_request", side_effect=missing_receipt):
        with pytest.raises(Problem) as error:
            consumer.deactivate("deactivation-skill", snapshot, body)
    assert error.value.body["error"]["code"] == "M3_DEACTIVATION_UNKNOWN"
    assert consumer.get("deactivation-skill").payload["deactivations"][0]["decision_id"] == D1


