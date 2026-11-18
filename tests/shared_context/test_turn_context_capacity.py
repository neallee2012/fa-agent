"""Historical model capacity in saved-turn metadata; no native model execution."""

import copy
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from shared_context.behavior_model_capacity import DeclaredModelLimits, ModelCapacitySnapshot
from shared_context.behavior_turn_context import TurnContext
from assistant_api_helpers import assistant_api
from test_instruction_capacity import configured_capacity, declared_facts
from instruction_flow_helpers import instruction_models, runtime_path
from instruction_prompt_helpers import sdk_options
from test_m3_auth import jwk_client, signing_key
from test_web_test_console_frozen_interface import real_interface
from stored_turn_context_helpers import load_case, stored_saved_turn


def context_path(conversation_id, turn):
    return f"/work-policy/v1/conversations/{conversation_id}/turns/{turn}/context"


def test_context_reports_each_turns_registered_capacity_not_the_latest_model(assistant_api, monkeypatch):
    declared_facts(monkeypatch)
    h, control, _ = configured_capacity(assistant_api)
    calls, _ = sdk_options(monkeypatch)
    facts = load_case(h, "context-capacity-two-turns")
    first, second = facts["first"], facts["second"]
    control.refresh()
    before = copy.deepcopy(h.transport.backing.rows)
    originals = [
        control.records[item["binding_id"]].profile.instruction.model_capacity.model_dump(mode="json")
        for item in (first, second)
    ]
    Path(os.environ["FA_INSTRUCTION_MODEL_FACTS_FILE"]).write_text("{}", encoding="utf-8")
    control._models = None
    for number, original in enumerate(originals, 1):
        response = h.client.get(context_path(first["conversation_id"], number), headers=h.headers())
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["model_capacity"] == original
        assert data["model"] == original["model"]
        assert data["token_usage"]["provider_input_tokens"] is None
        assert response.headers["x-content-sha256"] == hashlib.sha256(response.content).hexdigest()
    assert originals[0]["limits"]["context_window_tokens"] == 64000
    assert originals[1]["limits"]["context_window_tokens"] == 16000
    assert not calls
    assert h.transport.backing.rows == before


PARTIAL_LIMITS = ({}, {"max_prompt_tokens": 10000}, {"context_window_tokens": 12000, "max_output_tokens": 1024})


@pytest.mark.parametrize("limits", PARTIAL_LIMITS)
def test_context_keeps_missing_declared_limits_unknown_without_estimation(assistant_api, monkeypatch, limits):
    Path(os.environ["FA_INSTRUCTION_MODEL_FACTS_FILE"]).write_text(
        json.dumps({"gpt-4.1-mini": limits}), encoding="utf-8",
    )
    h, control, _ = configured_capacity(assistant_api)
    accepted = load_case(h, f"context-capacity-partial-{PARTIAL_LIMITS.index(limits)}")["accepted"]
    control.refresh()
    response = h.client.get(context_path(accepted["conversation_id"], 1), headers=h.headers())
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["model_capacity"] == (
        control.records[accepted["binding_id"]].profile.instruction.model_capacity.model_dump(mode="json")
    )
    assert data["model_capacity"]["limits"] == {
        name: limits.get(name) for name in ("context_window_tokens", "max_prompt_tokens", "max_output_tokens")
    }
    assert data["token_usage"] == {
        "full_request_tokens": None, "provider_input_tokens": None, "provider_output_tokens": None,
        "reason": "complete-turn-usage-not-recorded",
    }


def test_legacy_context_does_not_add_a_capacity_field_or_invent_current_facts(real_interface):
    c = real_interface
    _, task, path = stored_saved_turn(c)
    before = copy.deepcopy(c.h.transport.backing.rows)
    response = c.h.client.get(path, headers=c.h.headers())
    assert response.status_code == 200
    assert "model_capacity" not in response.json()
    bridge = c.browser.ui.get("/api/instructions/" + task["task_id"] + "/context")
    assert bridge.status_code == 200
    assert "model_capacity" not in bridge.json()["context"]
    assert c.h.transport.backing.rows == before


@pytest.mark.parametrize("change", ["none", "wrong-model", "wrong-digest"])
def test_context_consumer_validates_the_recorded_model_capacity(real_interface, change):
    c = real_interface
    _, task, path = stored_saved_turn(c)
    raw = c.h.client.get(path, headers=c.h.headers()).json()
    snapshot = ModelCapacitySnapshot.from_declared(
        raw["model"] if change != "wrong-model" else "another-model",
        DeclaredModelLimits(context_window_tokens=64000, max_prompt_tokens=60000, max_output_tokens=2048),
    ).model_dump(mode="json")
    if change == "wrong-digest":
        snapshot["source_sha256"] = "f" * 64
    raw["model_capacity"] = snapshot
    encoded = json.dumps(raw).encode("utf-8")

    def response(request, original):
        if request.url.path.endswith("/context"):
            return httpx.Response(200, content=encoded, headers={
                "cache-control": "no-store", "x-content-sha256": hashlib.sha256(encoded).hexdigest(),
            })
        return original

    c.mutate = response
    before, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    result = c.browser.ui.get("/api/instructions/" + task["task_id"] + "/context")
    assert result.status_code == (200 if change == "none" else 502), result.text
    if change == "none":
        assert result.json()["context"]["model_capacity"] == snapshot
        assert TurnContext.model_validate_json(encoded).model_capacity == ModelCapacitySnapshot.model_validate_json(
            json.dumps(snapshot),
        )
    else:
        assert result.json()["error"]["code"] == "TURN_CONTEXT_INVALID"
    assert c.h.transport.backing.rows == before and c.journal.read_bytes() == journal
